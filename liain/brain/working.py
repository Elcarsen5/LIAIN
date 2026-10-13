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

import re as _re

# ── 감정/주체 선별 (base-level 보강 + 오귀속 방지) ───────────────────
# 취약한 한국어 임베딩이 놓치는 '인물+감정' 질의를, about=인물 + struggle/emotion 태그로
# 기호 선별해 base-level(importance×recency×insight) 랭킹. 관련도가 게이트 — 감정 질의가
# 아니면 발동 안 함(무관 대화에 지배테마 spillover 방지).
_WM_EMO_QUERY = _re.compile(
    r"힘들|힘드|걱정|어때|근황|요즘|기분|고생|스트레스|우울|번아웃|지쳐|마음|감정|괜찮|"
    r"그동안|무슨 ?일|왜 그|속상|외로|불안|어떻게 지")
# 감정 보강에서 제외할 noise 태그 — 루틴/도메인성 기록. 페르소나별로 확장 가능.
_WM_EXCLUDE = {"routine"}
_WM_INSIGHT = {"insight", "pattern", "reflection", "risk"}
_WM_EMO_TAGS = {"struggle", "emotion", "relationship"}


def _wm_alias_to_entity() -> dict:
    """config contacts의 alias→entity 맵 (가족 하드코딩 대체)."""
    try:
        from liain import config as _lc
        return _lc.alias_to_entity()
    except Exception:
        return {}


def _wm_entity_display(entity: str) -> str:
    """entity → 표시명 (config people의 display/name, 없으면 entity 그대로)."""
    try:
        from liain import config as _lc
        p = _lc.people().get(entity) or {}
        return p.get("display") or p.get("name") or entity
    except Exception:
        return entity


def _wm_turn_re():
    """대화 턴으로 저장된 레코드를 거르는 패턴 — 페르소나 이름 기반(하드코딩 대체)."""
    try:
        from liain import config as _lc
        name = (_lc.persona().get("name") or "").strip()
    except Exception:
        name = ""
    if name:
        n = _re.escape(name)
        return _re.compile(rf"말함\s*→\s*{n}이?\s*답|말함:|→\s*{n}이?\s*답|브리핑 발신|일기 발신")
    return _re.compile(r"말함:|브리핑 발신|일기 발신")


def _wm_subject(rec) -> str:
    """record → 주체 표시("아빠·친구"). 무-엔티티는 빈 문자열(오귀속 차단 라벨)."""
    if not rec:
        return ""
    ents = [_wm_entity_display(e) for e in (rec.get("entities") or []) if e]
    return "·".join(ents[:2])


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
                            sources: Sequence[str] = ("record", "episode"),
                            speaking_with: Optional[str] = None) -> list[str]:
        """query에 맞는 retrieval 결과를 사람이 읽을 텍스트 라인 list로.

        1) 인물+감정 질의면 그 사람 struggle/emotion 레코드를 base-level로 보강(관련도 게이트).
        2) 하이브리드 인출 — BM25(어휘) + SBERT vector(의미) RRF 융합. 어휘 안 겹치는
           패러프레이즈(예: "다친 적"↔"골절")를 dense가 잡는다.
        sources default=("record","episode"). short_term은 noise라 제외(today_chunks 슬롯에 이미).
        """
        if not query:
            return []
        # 1) 기호 선별 + base-level 보강 (인물+감정 질의만 — 관련도가 게이트).
        aug = self._emotion_theme_augment(query, speaking_with, top_k=4)
        # 2) 하이브리드 인출(BM25 + dense RRF).
        fts_lines = []
        try:
            hits = self._hybrid_hits(query, top_k, sources)
            fts_lines = self._format_hits(hits)
        except Exception as e:
            print(f"[WorkingMemory] hybrid retrieve 실패: {e}", flush=True)
        # 3) 병합 + dedup (감정테마 → FTS 순으로 위).
        return self._dedup_lines(aug + fts_lines, limit=top_k + len(aug))

    def _hybrid_hits(self, query, top_k, sources):
        """BM25(어휘) + SBERT vector(의미)를 RRF로 융합한 hit dict 리스트.

        - BM25(retrieval.py FTS5): 정확 토큰/구문 매칭. episode도 커버.
        - vector(records.search, multilingual-e5): 의미 유사. record만(임베딩 보유).
        RRF(score=Σ 1/(K+rank), K=60)로 (source,id) 키 융합 — 한쪽에만 뜬 것도 살아남고
        둘 다 뜬 건 상위로. dense 미가용(임베딩 없음)이면 BM25 동작을 그대로 보존.
        반환형은 retriever hit와 동일(source/source_id/ts/text)이라 _format_hits가 그대로 처리.
        """
        pool = max(top_k * 2, 10)
        bm25 = []
        if self.retriever:
            try:
                bm25 = self.retriever.retrieve(query, top_k=pool, sources=list(sources)) or []
            except Exception as e:
                print(f"[WorkingMemory] bm25 실패: {e}", flush=True)
        vec_hits = []
        if "record" in sources:
            try:
                from . import records as _er
                for r in _er.search(query, top_k=pool, mode="vector"):
                    vec_hits.append({
                        "source": "record", "source_id": r.get("id"),
                        "ts": r.get("updated_at") or r.get("created_at") or "",
                        "text": r.get("text") or "",
                    })
            except Exception as e:
                print(f"[WorkingMemory] dense 실패: {e}", flush=True)
        if not vec_hits:
            return bm25[:top_k]                       # dense 미가용 → 기존 BM25 동작 보존
        K = 60
        scores, hitmap = {}, {}
        for ranked in (bm25, vec_hits):
            for rank, h in enumerate(ranked):
                key = (h.get("source"), h.get("source_id"))
                if not key[1]:
                    continue
                scores[key] = scores.get(key, 0.0) + 1.0 / (K + rank)
                hitmap.setdefault(key, h)
        top = sorted(scores.items(), key=lambda x: -x[1])[:top_k]
        return [hitmap[k] for k, _ in top]

    def _format_hits(self, hits) -> list[str]:
        """retriever hit → 표시 라인. event_date 있으면 '지금 기준' 경과를 프리픽스로.

        주체 라벨(_wm_subject)을 앞에 못박아 '누구에 관한' 기억인지 고정 — BM25가 인물무관으로
        다른 사람 레코드를 끌어와도 라벨이 있으면 안 섞인다(오귀속 차단).
        """
        _ev_map = {}
        try:
            from . import records as _er
            _ids = {h.get("source_id") for h in hits if h.get("source") == "record"}
            if _ids:
                _ev_map = {r["id"]: r for r in _er.all_records() if r.get("id") in _ids}
        except Exception:
            _ev_map = {}
        try:
            from . import temporal as _tr
            from datetime import datetime as _dt
            _now = _dt.now()
        except Exception:
            _tr = None
            _now = None
        lines = []
        for h in hits:
            ts = (h.get("ts") or "")[:19]
            src = h.get("source", "?")
            text = (h.get("text") or "")[:200]
            rec = _ev_map.get(h.get("source_id"))
            subj = _wm_subject(rec)
            pre = f"{subj}: " if subj else ""
            ev = rec.get("event_date") if rec else None
            if ev and _tr and _now:
                lines.append(f"[{_tr.recall_phrase(ev, now=_now, ongoing=_tr.is_ongoing(text))}] {pre}{text}")
            else:
                lines.append(f"[{src} {ts}] {pre}{text}")
        return lines

    @staticmethod
    def _dedup_lines(lines, limit=None) -> list[str]:
        """텍스트 40%+ 겹치면 제거(같은 사건 중복 방지)."""
        out, picked = [], []
        for ln in lines:
            if not ln:
                continue
            w = set(_re.findall(r"[가-힣A-Za-z]{2,}", ln[-120:]))
            if any((len(w & p) / max(1, len(w | p))) > 0.4 for p in picked):
                continue
            out.append(ln)
            picked.append(w)
            if limit and len(out) >= limit:
                break
        return out

    def _emotion_theme_augment(self, query, speaking_with, top_k=4):
        """인물+감정 질의면 그 사람 struggle/emotion/relationship 레코드를 base-level로 보강.

        관련도 게이트: 감정 질의가 아니면 [] (무관 대화에 지배테마 spillover 방지).
        인물 = 질의 속 별칭(config contacts) 우선, 없으면 감정 질의 한정 speaking_with(1:1 상대).
        랭킹 = importance×1.6×insight×recency^-0.35, 주당 상한으로 시간축 spread.
        """
        from datetime import datetime as _dt
        try:
            from . import records as _er
        except Exception:
            return []
        aliases = _wm_alias_to_entity()
        alias = next((v for k, v in aliases.items() if k and k in query), None)
        emo_q = bool(_WM_EMO_QUERY.search(query))
        person = alias or (speaking_with if emo_q else None)
        if not person or not emo_q:          # 게이트: 인물 + 감정질의 둘 다여야
            return []
        turn_re = _wm_turn_re()
        now = _dt.now()
        cands = []
        for r in _er.by_entity(person):
            if r.get("deleted_at") or r.get("pinned") or r.get("importance", 5) < 7:
                continue
            tags = set(r.get("tags") or [])
            if not (tags & _WM_EMO_TAGS) or (tags & _WM_EXCLUDE):
                continue
            txt = r.get("text") or ""
            if turn_re.search(txt):
                continue
            try:
                days = max(0.5, (now - _dt.fromisoformat(r.get("created_at"))).total_seconds() / 86400)
            except Exception:
                continue
            ins = 1.4 if (tags & _WM_INSIGHT) else 1.0
            score = r.get("importance", 5) * 1.6 * ins * (days ** -0.35)
            cands.append((score, days, int(days // 7), txt))
        cands.sort(key=lambda x: -x[0])
        out, picked, wc = [], [], {}
        for s, days, week, txt in cands:
            if wc.get(week, 0) >= 2:                 # 주당 상한(시간축 spread)
                continue
            w = set(_re.findall(r"[가-힣A-Za-z]{2,}", txt[:120]))
            if any((len(w & p) / max(1, len(w | p))) > 0.35 for p in picked):
                continue
            lbl = f"{days/7:.0f}주 전" if days >= 7 else ("어제" if days >= 1 else "오늘")
            out.append(f"[{lbl}] {txt[:200]}")
            picked.append(w)
            wc[week] = wc.get(week, 0) + 1
            if len(out) >= top_k:
                break
        return out

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
