"""Liain — a config-driven body for AI personas. Bring your own soul.

Lain shapes it. Your persona is the soul that fills it. Name your own.

핵심 모듈:
  liain.config    — persona.yaml / contacts.yaml / llm.yaml 로더
  liain.persona   — config → 시스템 프롬프트 빌더
  liain.llm       — 역할별 LLM 라우터 (claude_cli / ollama / api, 3프로필)
  liain.channels  — 메시징 채널 추상화 (Telegram 기본, iMessage 옵션)

빠른 시작: examples/quickstart 참조.
"""
__version__ = "0.1.0"

from liain import config, persona, secrets  # noqa: F401
