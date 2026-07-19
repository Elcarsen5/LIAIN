"""채널 레지스트리 + 역할 라우터.

기본 제공: Telegram(크로스플랫폼). iMessage 등 플랫폼 종속 채널은 옵션 추가.
route(role, text) — 사용 가능한 채널 우선순위대로 송신, 성공 시 반환.
"""
from __future__ import annotations

from liain.channels.base import Channel, IncomingMessage

# Telegram은 requests 의존 — 없으면 채널만 비활성(기억·일기 등 코어는 계속 동작).
try:
    from liain.channels.telegram import TelegramChannel
    _TELEGRAM = TelegramChannel()
except Exception as _e:  # pragma: no cover
    TelegramChannel = None
    _TELEGRAM = None
    print(f"[liain.channels] Telegram 비활성: {_e}", flush=True)


def all_channels() -> list[Channel]:
    chans: list[Channel] = []
    # iMessage(옵션, macOS) — 설치돼 있으면 우선 추가
    try:
        from liain.channels.imessage import IMessageChannel
        chans.append(IMessageChannel())
    except Exception:
        pass
    chans.append(_TELEGRAM) if _TELEGRAM else None
    return chans


def available_channels() -> list[Channel]:
    return [c for c in all_channels() if c.available]


def _short(t: str) -> str:
    s = (t or "").replace("\n", " ")
    return s[:40] + ("…" if len(s) > 40 else "")


def route(role: str, text: str) -> bool:
    order = available_channels() or [_TELEGRAM]
    for i, ch in enumerate(order):
        try:
            ok = bool(ch.send_role(role, text))
        except Exception as e:
            print(f"[liain.messenger] {role} → {ch.name} 예외: {e}", flush=True)
            ok = False
        tag = f"{ch.name} FALLBACK" if i > 0 else ch.name
        print(f"[liain.messenger] {role} → {tag} {'OK' if ok else 'FAIL'} ({_short(text)})",
              flush=True)
        if ok:
            return True
    return False
