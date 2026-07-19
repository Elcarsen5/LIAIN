"""Working Memory Assembler — 매 LLM 호출 직전 시스템 프롬프트(working memory)를 조립.

자료 권고(CoALA / MemGPT) 적용:
- Working Memory는 "한 LLM call 동안" 유효한 휘발성 슬라이스.
- 페르소나·정체 카드·최근 대화·오늘 단기·retrieval 결과·스크래치패드를 슬롯으로
  조립하고, 토큰 예산 안에서 우선순위 낮은 것부터 자른다.
- "어제 점심 뭐 먹었지?" 같은 질의는 retrieval(P1.4)이 episodic store에서
  관련 fact를 끌어와 working memory에 임시 주입하는 패턴.

P0.5 단계에서는 모듈·클래스만 신설. build_dad_system / build_son_system /
build_group_system 의 호출자 전환은 P1.4 (MemoryRetriever 도입)와 함께.
"""
from __future__ import annotations
import json
from datetime import datetime
from typing import Optional, Sequence

# Anthropic API context window는 200K지만, working memory에 박는 양은 보수적으로
# 잡는 게 비용·정확도 양쪽에서 유리 (Lost in the Middle 회피).
# 16K 토큰 ≈ 64KB. 초과 시 우선순위 낮은 슬롯부터 cut.
DEFAULT_TOKEN_BUDGET = 16000


def estimate_tokens(text: str) -> int:
    """대략적 토큰 수 추정 (한글 포함, char/3 휴리스틱).
    정확하지 않지만 cut 결정에는 충분."""
    if not text:
        return 0
    return max(1, len(text) // 3)


# 슬롯 우선순위 — 작은 숫자가 높은 우선순위 (cut 마지막).
# 1: persona — 페르소나 정체성. 절대 cut 안 됨.
# 2: identity_card — 사용자 정체(pinned facts). 거의 cut 안 됨.
# 3: brain_unified — 통합 brain context (사람들·세계관·신념·에피소드).
# 4: recent_turns — 직전 대화 N건. 짧지만 가장 신선한 컨텍스트.
# 5: today_chunks — 오늘 short-term (chunked + raw 일부).
# 6: retrieved — 질의 기반 retrieval 결과 (P1.4 도입 후).
# 7: scratchpad — 현재 task 진행 상태.
# 8: extra — 호출자별 부가 (tool 안내, 응답 제약 등).
SLOT_PRIORITY = {
    "persona": 1,
    "identity_card": 2,
    "brain_unified": 3,
    "recent_turns": 4,
    "today_chunks": 5,
    "retrieved": 6,
    "scratchpad": 7,
    "extra": 8,
}

# 우선순위 1, 2는 budget 초과해도 cut 안 함.
NEVER_CUT = {1, 2}


class WorkingMemoryAssembler:
    """매 prompt마다 호출되어 working memory(시스템 프롬프트)를 조립.

    사용 예:
        wm = WorkingMemoryAssembler(brain)
        prompt = wm.assemble(
            persona=SYSTEM_DAD.format(...),
            recent_turns=history[-10:],
            today_chunks=brain.get_short_term_context(),
            retrieved=retriever.retrieve(query) if retriever else None,
            extra="## 도구 안내\\n...",
        )
    """

    def __init__(self, brain, token_budget: int = DEFAULT_TOKEN_BUDGET, retriever=None):
        self.brain = brain
        self.token_budget = token_budget
        # P1.4c — retriever 의존성 주입. None이면 retrieve 슬롯 비활성.
        # 호출자 측에서 SqliteFts5Retriever() 또는 향후 HybridRetriever 주입.
        self.retriever = retriever

    def retrieve_for_query(self, query: str, top_k: int = 5,
                            sources: Sequence[str] = ("record", "episode")) -> list[str]:
        """query에 맞는 retrieval 결과를 사람이 읽을 텍스트 라인 list로.

        sources default = ("record", "episode") — semantic fact + episodic 사건만.
        short_term은 routine 자질구레한 메시지가 많아 noise. 별도 슬롯(today_chunks)에
        이미 노출되므로 retrieve에선 제외 (Tulving 모델: working memory 별도 슬롯).
        """
        if not self.retriever or not query:
            return []
        try:
            hits = self.retriever.retrieve(query, top_k=top_k, sources=list(sources))
        except Exception as e:
            print(f"[WorkingMemory] retrieve 실패: {e}", flush=True)
            return []
        lines = []
        for h in hits:
            ts = (h.get("ts") or "")[:19]
            src = h.get("source", "?")
            text = (h.get("text") or "")[:200]
            lines.append(f"[{src} {ts}] {text}")
        return lines

    def assemble(
        self,
        persona: str,
        identity_card: Optional[str] = None,
        brain_unified: Optional[str] = None,
        recent_turns: Optional[Sequence[dict]] = None,
        today_chunks: Optional[str] = None,
        retrieved: Optional[Sequence[str]] = None,
        scratchpad: Optional[str] = None,
        extra: Optional[str] = None,
        include_brain: bool = True,
    ) -> str:
        """working memory 조립.

        - persona: 필수. 페르소나 시스템 프롬프트(SYSTEM_DAD/SON/GROUP).
        - identity_card: 선택. 호출자가 따로 만드는 사용자 정체 압축본.
                          None이면 brain에서 자동 추출 (P2.1 pinned record로 격상 예정).
        - brain_unified: 선택. None이면 brain.get_unified_context()를 자동 호출.
                         include_brain=False면 강제 skip.
        - recent_turns: 선택. [{"role": "user/assistant", "content": "..."}, ...]
        - today_chunks: 선택. 오늘 short-term 요약 (보통 brain.get_short_term_context()).
                        None이면 brain_unified 안에 포함되어 있을 가능성 큼.
        - retrieved: 선택. P1.4 이후 retrieval 결과 fact 리스트.
        - scratchpad: 선택. 현재 task 진행 상태.
        - extra: 선택. 호출자별 부가 안내 (도구·응답 제약).
        """
        slots: list[tuple[str, str]] = []

        slots.append(("persona", persona))

        if identity_card:
            slots.append(("identity_card", identity_card))

        if include_brain:
            ctx = brain_unified
            if ctx is None and self.brain is not None:
                try:
                    ctx = self.brain.get_unified_context()
                except Exception as e:
                    ctx = f"(brain context 로드 실패: {e})"
            if ctx:
                slots.append(("brain_unified", ctx))

        if recent_turns:
            turn_lines = []
            for t in recent_turns:
                role = t.get("role", "?")
                content = (t.get("content") or "")[:300]  # 한 turn 길이 cap
                turn_lines.append(f"- {role}: {content}")
            if turn_lines:
                slots.append(("recent_turns", "## 최근 대화\n" + "\n".join(turn_lines)))

        if today_chunks:
            slots.append(("today_chunks", today_chunks))

        if retrieved:
            ret_lines = [f"- {r}" for r in retrieved if r]
            if ret_lines:
                slots.append(("retrieved", "## 관련 기억 (질의 맥락)\n" + "\n".join(ret_lines)))

        if scratchpad:
            slots.append(("scratchpad", "## 현재 작업\n" + scratchpad))

        if extra:
            slots.append(("extra", extra))

        # 우선순위 정렬 후 budget 안에서 cut.
        slots_with_prio = [
            (name, content, SLOT_PRIORITY.get(name, 99)) for name, content in slots
        ]
        slots_with_prio.sort(key=lambda s: s[2])

        kept: list[tuple[str, str]] = []
        cumulative = 0
        for name, content, prio in slots_with_prio:
            tokens = estimate_tokens(content)
            if cumulative + tokens > self.token_budget and prio not in NEVER_CUT:
                # cut. 다만 우선순위 1, 2는 절대 cut 안 함.
                # 더 낮은 우선순위(=숫자 큰)는 모두 skip.
                continue
            kept.append((name, content))
            cumulative += tokens

        # 원래 호출 순서대로 재배열 (시각적 일관성).
        # 다만 persona는 항상 최상단, extra는 항상 최하단이 자연.
        order = {"persona": 0, "identity_card": 1, "brain_unified": 2,
                 "recent_turns": 3, "today_chunks": 4, "retrieved": 5,
                 "scratchpad": 6, "extra": 7}
        kept.sort(key=lambda s: order.get(s[0], 99))

        return "\n\n".join(content for _, content in kept)


__all__ = [
    "WorkingMemoryAssembler",
    "DEFAULT_TOKEN_BUDGET",
    "SLOT_PRIORITY",
    "estimate_tokens",
]
