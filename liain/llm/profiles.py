"""LLM 프로필 프리셋 — 하드웨어×구독 상황별 역할→백엔드 매핑 (M3).

사용자는 llm.yaml에서 `profile:` 한 줄로 고르고, `backends:`로 개별 오버라이드.

역할(role):
  chat       — 대화 응답
  reasoning  — 일기·회고·분석 등 깊은 추론
  vision     — 사진·영상 묘사 (없으면 graceful degradation)
  classify   — 메시지 분류 (항상 키워드, LLM-free — API Zero 코어)
  embed      — 검색 임베딩 (항상 로컬 SBERT)

백엔드 표기:
  claude_cli            — 구독 CLI (무과금)
  ollama:<model>        — 로컬 Ollama 모델
  api:<model>           — 유료 API (Anthropic/OpenAI)
  none                  — 비활성 (vision 없음 등)
  keyword / sbert       — LLM-free (classify/embed)
"""

PROFILES = {
    # 저사양(라파)+구독: 대화=구독, vision=스킵(graceful), 무거운 로컬 모델 X
    "lite-subscription": {
        "chat": "claude_cli",
        "reasoning": "claude_cli",
        "vision": "none",
        "classify": "keyword",
        "embed": "sbert",
    },
    # 고사양(맥미니)+구독: 대화=구독, vision=로컬 (리안 디폴트, 프라이버시)
    "full-subscription": {
        "chat": "claude_cli",
        "reasoning": "claude_cli",
        "vision": "ollama:qwen2.5vl:7b",
        "classify": "keyword",
        "embed": "sbert",
    },
    # 고사양+완전로컬: 대화도 로컬, 무과금 완전체
    "full-local": {
        "chat": "ollama:qwen3:14b",
        "reasoning": "ollama:qwen3:14b",
        "vision": "ollama:qwen2.5vl:7b",
        "classify": "keyword",
        "embed": "sbert",
    },
}

DEFAULT_PROFILE = "full-subscription"


def resolve(profile: str | None, overrides: dict | None = None) -> dict:
    """프로필명 + 오버라이드 → 역할별 백엔드 dict."""
    base = dict(PROFILES.get(profile or DEFAULT_PROFILE, PROFILES[DEFAULT_PROFILE]))
    if overrides:
        base.update({k: v for k, v in overrides.items() if v})
    return base
