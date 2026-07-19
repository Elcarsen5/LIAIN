"""Liain 기억 계층 — 5계층 기억 + 회고 + 일기.

    from liain.brain import Brain
    b = Brain()
    b.process_conversation(messages)      # 대화 → 기억 추출·축적
    b.get_unified_context()               # 프롬프트용 기억 컨텍스트
    b.consolidate_daily()                 # 단기→중기→장기 승격, 패턴 인식

데이터는 작업 디렉토리의 brain/ 에 쌓인다 ([[paths]]).
"""
from .paths import BRAIN_DIR, DIARY_DIR, ensure_dirs, data_root  # noqa: F401

__all__ = ["Brain", "BRAIN_DIR", "DIARY_DIR", "ensure_dirs", "data_root"]


def __getattr__(name):
    if name == "Brain":
        from .manager import Brain
        return Brain
    raise AttributeError(name)
