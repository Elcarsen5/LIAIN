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
    def send_target(self, target: str, text: str, raw: bool = False) -> bool:
        """채널 native target(handle/chat_id)으로 직접 송신.

        raw=True: 발신 정화(sanitize_outgoing) 우회 — 에이전트 자기발송(생각 로그) 전용.
                 상대에게 안 가는 내부 기록이라 원본 보존이 필요. 외부 상대엔 절대 쓰지 말 것.
        """
        ...

    def send_to_self(self, text: str) -> bool:
        """에이전트 자기 계정으로 발송 (2단 파이프라인 THINK 결과 보존 로그).

        목적지 = config.self_handle(채널명). 미설정이면 no-op(생각 보존만 생략, 대화 정상).
        raw=True로 정화 우회(생각 원본 보존). 자기발송은 is_from_me라 폴러가 응답 안 함(루프 없음).
        """
        from liain import config
        target = config.self_handle(self.name)
        if not target:
            return False
        try:
            return self.send_target(target, text, raw=True)
        except Exception as e:
            print(f"[liain.{self.name}] 자기발송 오류: {e}", flush=True)
            return False

    def poll(self, callback) -> None:
        """수신 폴링 루프 시작 (지원 채널만 override). 기본 no-op."""
        raise NotImplementedError(f"{self.name} 채널은 poll 미지원")
