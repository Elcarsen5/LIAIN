"""채널 추상화 — 메시징 채널의 통일 인터페이스 (M2).

Liain 프레임워크의 채널 계약. 새 채널(Discord/Slack/SMS 등)을 추가하려면
이 `Channel`을 구현하면 된다. 기본 제공: Telegram(크로스플랫폼), iMessage(macOS).

설계 원칙:
- `send_role(role, text)` — 역할(dad/son1/son2/group …)로 송신. role→native target은
  채널이 config(liain_config)에서 해석.
- `available` — 이 채널이 현 환경에서 쓸 수 있나 (예: iMessage는 macOS만).
- `poll(callback)` — 수신 폴링 루프 (옵션; 채널이 지원하면).
- `normalize(raw)` — 수신 원본 → IncomingMessage (옵션).

기존 리안 코드(imessage_bot/telegram_bot)를 wrapping하는 어댑터로 구현 →
검증된 송수신 로직을 그대로 재사용하면서 통일 계약만 제공.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class IncomingMessage:
    """수신 메시지 정규화 형태 — 채널 무관."""
    text: str
    sender: str = ""          # 발신자 native id/handle
    chat_id: str = ""         # 대화 id (1:1 또는 그룹)
    role: str = "unknown"     # dad/son1/son2/group/unknown
    entity: str = ""          # 내부 식별자 (contacts.yaml의 people 키)
    is_group: bool = False
    is_from_me: bool = False
    channel: str = ""         # "telegram" | "imessage" | ...
    raw: dict = field(default_factory=dict)


class Channel(ABC):
    """메시징 채널 계약. 새 채널은 이걸 구현."""

    name: str = "base"

    @property
    @abstractmethod
    def available(self) -> bool:
        """현 환경에서 사용 가능한가 (토큰 존재, 플랫폼 일치 등)."""
        ...

    @abstractmethod
    def send_role(self, role: str, text: str) -> bool:
        """역할(dad/son1/son2/group)로 송신. 성공 True."""
        ...

    @abstractmethod
    def send_target(self, target: str, text: str) -> bool:
        """채널 native target(handle/chat_id)으로 직접 송신."""
        ...

    def poll(self, callback) -> None:
        """수신 폴링 루프 시작 (지원 채널만 override). 기본 no-op."""
        raise NotImplementedError(f"{self.name} 채널은 poll 미지원")
