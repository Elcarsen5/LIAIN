"""Telegram 채널 — Telegram Bot API 직접 사용 (의존성: requests).

레퍼런스 채널. 토큰(env TELEGRAM_BOT_TOKEN)만 있으면 어디서나 동작.
role→chat_id는 config 로스터에서 해석.
"""
from __future__ import annotations
import os
import requests

from liain.channels.base import Channel, IncomingMessage


def _token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{_token()}/{method}"


class TelegramChannel(Channel):
    name = "telegram"

    @property
    def available(self) -> bool:
        return bool(_token())

    def _target_for_role(self, role: str) -> str:
        from liain import config
        if role in ("group", "family", "family_group"):
            return str((config.groups().get("family") or {}).get("telegram") or "")
        return str(config.person_by_role(role).get("telegram") or "")

    def send_target(self, target: str, text: str, raw: bool = False) -> bool:
        # raw=True: 정화 우회 — 자기발송(생각 로그) 전용(원본 보존). 외부 상대엔 쓰지 말 것.
        if not raw:
            from liain.guard import sanitize_outgoing
            text, _stripped = sanitize_outgoing(text)   # <생각>·혼잣말 최종 제거(발신 공통 chokepoint)
            if _stripped:
                print("[liain.telegram] <생각>/혼잣말 발신 정화됨(생성단 정리 누락 방어)", flush=True)
        if not target or not (text or "").strip():
            return False
        try:
            r = requests.post(_api("sendMessage"),
                              json={"chat_id": target, "text": text}, timeout=20)
            return r.status_code == 200 and r.json().get("ok", False)
        except Exception as e:
            print(f"[liain.telegram] send 실패: {e}", flush=True)
            return False

    def send_role(self, role: str, text: str) -> bool:
        return self.send_target(self._target_for_role(role), text)

    def poll(self, callback, interval: float = 1.5) -> None:
        """long-poll로 수신 → IncomingMessage 정규화 후 callback(msg)."""
        import time
        from liain import config
        offset = None
        tg_map = {str(p.get("telegram")): (e, p) for e, p in config.people().items()}
        print(f"[liain.telegram] 폴링 시작 (역할 {len(tg_map)}명)", flush=True)
        while True:
            try:
                r = requests.get(_api("getUpdates"),
                                 params={"offset": offset, "timeout": 30}, timeout=40)
                for upd in (r.json().get("result") or []):
                    offset = upd["update_id"] + 1
                    m = upd.get("message") or {}
                    text = m.get("text", "")
                    if not text:
                        continue
                    chat = m.get("chat", {})
                    chat_id = str(chat.get("id", ""))
                    sender_id = str((m.get("from") or {}).get("id", ""))
                    is_group = chat.get("type") in ("group", "supergroup")
                    ent, p = tg_map.get(sender_id, ("", {}))
                    callback(IncomingMessage(
                        text=text, sender=sender_id, chat_id=chat_id,
                        role=p.get("role", "unknown"), entity=ent,
                        is_group=is_group, channel="telegram", raw=upd))
            except Exception as e:
                print(f"[liain.telegram] poll 오류: {e}", flush=True)
                time.sleep(3)
            time.sleep(interval)
