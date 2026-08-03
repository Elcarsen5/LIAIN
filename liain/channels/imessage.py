"""iMessage 채널 — macOS 전용 (Messages 앱 + chat.db).

애플 생태계에서만 동작: 송신은 AppleScript(osascript)로 Messages 앱을 구동하고,
수신은 `~/Library/Messages/chat.db`(SQLite)를 read-only 폴링한다. 다른 OS에서는
`available`이 False라 자동 비활성(Telegram 등 다른 채널로 폴백).

전제 조건 (macOS):
- Messages 앱에 iMessage 계정 로그인.
- 터미널/실행 프로세스에 **전체 디스크 접근 권한**(chat.db 읽기)과
  **자동화 권한**(Messages 제어). 시스템 설정 > 개인정보 보호 및 보안에서 부여.

역할↔핸들 매핑은 contacts.yaml의 각 사람 `imessage` 필드(전화/이메일)에서 온다:
    people:
      owner:
        role: dad
        imessage: "+1555..."      # 또는 email
    groups:
      family:
        imessage: "iMessage;+;chat123..."   # 단톡 chat identifier
"""
from __future__ import annotations
import os
import re
import sys
import json
import sqlite3
import subprocess
from contextlib import closing

from liain.channels.base import Channel, IncomingMessage

_DB = os.path.expanduser("~/Library/Messages/chat.db")


def _data_root() -> str:
    return os.environ.get("LIAIN_DATA_DIR") or os.getcwd()


def _rowid_file() -> str:
    return os.path.join(_data_root(), "imessage_last_rowid.json")


# ── AppleScript 송신 ────────────────────────────────────

def _osa_escape(line: str) -> str:
    return line.replace("\\", "\\\\").replace('"', '\\"')


def _osa_string_literal(text: str) -> str:
    # multi-line은 linefeed concat — Messages가 개행 포함 send에서 hang하는 것 방지
    return " & linefeed & ".join(f'"{_osa_escape(l)}"' for l in text.split("\n"))


def _send(target: str, text: str) -> bool:
    """target: 1:1 handle(전화/이메일) 또는 단톡 chat id('iMessage;+;chat…')."""
    if not target or not text:
        return False
    msg = _osa_string_literal(text)
    # 단톡 chat identifier: 'iMessage;+;…' / 'any;+;…' / 'chat<숫자>'.
    # (그냥 startswith("chat")면 'chat@x.com' 같은 이메일 핸들을 오분류 → 정규식으로)
    if re.match(r"^(iMessage;|any;|chat\d)", target):
        chat_id = target
        if chat_id.startswith("chat"):
            chat_id = f"any;+;{chat_id}"
        elif chat_id.startswith("iMessage;+;"):
            chat_id = "any;+;" + chat_id[len("iMessage;+;"):]
        script = (f'tell application "Messages"\n'
                  f'  set targetChat to a reference to chat id "{chat_id}"\n'
                  f'  send ({msg}) to targetChat\n'
                  f'end tell')
    else:
        script = (f'tell application "Messages"\n'
                  f'  set targetService to 1st service whose service type = iMessage\n'
                  f'  set targetBuddy to buddy "{target}" of targetService\n'
                  f'  send ({msg}) to targetBuddy\n'
                  f'end tell')
    try:
        subprocess.run(["osascript", "-e", script], check=True, timeout=15,
                       capture_output=True, text=True)
        return True
    except subprocess.TimeoutExpired:
        print("[liain.imessage] send 타임아웃 (Messages 앱 hung?)", flush=True)
    except subprocess.CalledProcessError as e:
        print(f"[liain.imessage] send 실패: {(e.stderr or '')[:160]}", flush=True)
    except Exception as e:
        print(f"[liain.imessage] send 오류: {e}", flush=True)
    return False


# ── chat.db 수신 (attributedBody 디코딩 포함) ──────────

def _ts_length(body: bytes, pos: int):
    """NSArchiver typedstream length encoding (0x81/0x82/0x83 marker)."""
    if pos >= len(body):
        return 0, pos
    b = body[pos]
    if b < 0x81:
        return b, pos + 1
    if b == 0x81 and pos + 2 < len(body):
        return int.from_bytes(body[pos + 1:pos + 3], "little"), pos + 3
    if b == 0x82 and pos + 4 < len(body):
        return int.from_bytes(body[pos + 1:pos + 5], "little"), pos + 5
    if b == 0x83 and pos + 8 < len(body):
        return int.from_bytes(body[pos + 1:pos + 9], "little"), pos + 9
    return 0, pos + 1


def _extract_text(blob) -> str:
    """attributedBody(NSAttributedString typedstream) → text."""
    if not blob:
        return ""
    body = bytes(blob)
    idx = body.find(b"\x01\x94\x84\x01\x2B")
    if idx < 0:
        return ""
    pos = idx + 5
    if pos >= len(body):
        return ""
    length, start = _ts_length(body, pos)
    if length <= 0 or start + length > len(body):
        return ""
    return body[start:start + length].decode("utf-8", errors="replace")


def _new_messages(last_rowid: int):
    """chat.db ROWID > last_rowid 신규 메시지.
    → [(rowid, text, sender, chat_id, is_from_me), ...]"""
    if not os.path.exists(_DB):
        return []
    try:
        # ORDER BY rowid — watermark(last_rowid)와 처리 순서를 일치시킨다.
        # (date 정렬이면 지연 도착 메시지가 낮은 rowid로 와서 watermark가 뒤로
        #  밀려 재처리=중복 발송될 수 있음)
        with closing(sqlite3.connect(f"file:{_DB}?mode=ro", uri=True)) as conn:
            rows = conn.execute("""
                SELECT m.rowid, m.attributedBody, m.text, h.id, c.chat_identifier, m.is_from_me
                FROM message m
                LEFT JOIN handle h ON m.handle_id = h.rowid
                LEFT JOIN chat_message_join cmj ON m.rowid = cmj.message_id
                LEFT JOIN chat c ON cmj.chat_id = c.rowid
                WHERE m.rowid > ?
                ORDER BY m.rowid ASC
            """, (last_rowid,)).fetchall()
        out = []
        for rowid, body, text_col, sender, chat_id, is_from_me in rows:
            text = (_extract_text(body) or (text_col or "").strip()).replace("\x00", "")
            out.append((int(rowid), text, sender or "", chat_id or "", bool(is_from_me)))
        return out
    except Exception as e:
        print(f"[liain.imessage] DB 오류: {e}", flush=True)
        return []


def _load_rowid() -> int:
    try:
        return int(json.load(open(_rowid_file())).get("last_rowid", 0))
    except Exception:
        return 0


def _save_rowid(v: int):
    try:
        json.dump({"last_rowid": int(v)}, open(_rowid_file(), "w"))
    except OSError:
        pass


def _max_rowid() -> int:
    """폴링 시작점 — 지금까지의 최대 rowid (과거 메시지 폭주 방지)."""
    if not os.path.exists(_DB):
        return 0
    try:
        with closing(sqlite3.connect(f"file:{_DB}?mode=ro", uri=True)) as conn:
            r = conn.execute("SELECT MAX(rowid) FROM message").fetchone()
        return int(r[0] or 0)
    except Exception:
        return 0


# ── 채널 ────────────────────────────────────────────────

class IMessageChannel(Channel):
    name = "imessage"

    @property
    def available(self) -> bool:
        return sys.platform == "darwin" and os.path.exists(_DB)

    def _handle_for_role(self, role: str) -> str:
        from liain import config
        if role in ("group", "family", "family_group"):
            return str((config.groups().get("family") or {}).get("imessage") or "")
        p = config.person_by_role(role)
        return config._primary_imessage(p) if p else ""

    def send_target(self, target: str, text: str) -> bool:
        return _send(target, text)

    def send_role(self, role: str, text: str) -> bool:
        return _send(self._handle_for_role(role), text)

    def poll(self, callback, interval: float = 2.0) -> None:
        """chat.db 폴링 → IncomingMessage 정규화 → callback."""
        import time
        from liain import config
        # handle→(entity, person) 역매핑
        by_handle = {}
        for ent, p in config.people().items():
            im = p.get("imessage")
            for h in (im if isinstance(im, (list, tuple)) else [im]):
                if h:
                    by_handle[str(h)] = (ent, p)
        group_id = str((config.groups().get("family") or {}).get("imessage") or "")

        last = _load_rowid() or _max_rowid()
        _save_rowid(last)
        print(f"[liain.imessage] 폴링 시작 (last_rowid={last}, 등록 {len(by_handle)}명)",
              flush=True)
        while True:
            try:
                for rowid, text, sender, chat_id, is_from_me in _new_messages(last):
                    last = rowid
                    _save_rowid(last)
                    if is_from_me or not text:
                        continue
                    is_group = bool(chat_id) and chat_id.startswith("chat")
                    ent, p = by_handle.get(sender, ("", {}))
                    callback(IncomingMessage(
                        text=text, sender=sender,
                        chat_id=chat_id or sender,
                        role=(p.get("role") or ("group" if is_group else "unknown")),
                        entity=ent, is_group=is_group,
                        is_from_me=False, channel="imessage",
                        raw={"rowid": rowid}))
            except Exception as e:
                print(f"[liain.imessage] poll 오류: {e}", flush=True)
                time.sleep(3)
            time.sleep(interval)
