"""Mem0 패턴 ADD/UPDATE/DELETE/NOOP + 자가 승격(sources 기반 auto-pin).

자료(agent_memory_strategy_2026-04-27.md §4.2) 적용:
- 새 fact가 들어올 때마다 기존 record와 비교해 4-action 결정
- last-writer-wins 안티패턴 회피 — UPDATE 시 source provenance 보존
- NOOP 시 sources[] dedupe append → 통합 카운트 (사용자 핵심 요구)

자가 승격(P2.1d 보완):
- record.sources가 임계 도달 → pinned 자동 ON
- 임계: AUTO_PIN_SOURCES (≥3 confirm) OR AUTO_PIN_CHANNELS (≥2 자리 동시)
- 단톡 + 1:1 양쪽에서 confirm된 fact는 단일 인격 통합의 자연스러운 강화

API:
- find_similar(text, entities, top_k) — FTS5 retrieval로 유사 record 검색
- judge_action(new_fact, similar) → {action, target_id, reason}
- apply_action(decision, new_fact) → 갱신된 record (or None)
- maybe_auto_pin(record_id) → bool (승격됐으면 True)
- process_message(text, channel, ts, msg_id, speaker, entities_hint) — full pipeline

extract_facts는 P2.2a에선 stub(휴리스틱), P2.2b에서 LLM 연결.
"""
from __future__ import annotations
import os
import re
from typing import Optional, Sequence

from . import records as er

# 자가 승격 임계
AUTO_PIN_SOURCES = 3       # sources count 임계
AUTO_PIN_CHANNELS = 2      # distinct channel 임계


# ── P2.4 reflection bump ──────────────────────────────────


def _bump_reflection(importance: int) -> None:
    """fact ADD 시점에 호출. accumulate(sync) + maybe_reflect(background thread).

    LLM 호출이 일어나도 daemon main thread를 막지 않게 thread spawn.
    """
    try:
        from . import reflection as _ref
        _ref.accumulate(int(importance))
    except Exception as _e:
        print(f"[memory_manager] reflection accumulate 실패: {_e}", flush=True)
        return

    def _bg():
        try:
            from . import reflection as _ref2
            res = _ref2.maybe_reflect()
            if res:
                print(f"[memory_manager] reflection fired: "
                      f"insights={res.get('n_insights')}, "
                      f"opinions={res.get('opinions_changes')}", flush=True)
        except Exception as _e:
            print(f"[memory_manager] maybe_reflect 실패: {_e}", flush=True)

    import threading as _t
    _t.Thread(target=_bg, daemon=True).start()


# ── 자가 승격 ─────────────────────────────────────────────


def maybe_auto_pin(record_id: str) -> bool:
    """sources 카운트 또는 channel 다양성이 임계 도달 시 pinned 자동 ON.

    이미 pinned면 no-op. 사용자가 수동 unpin한 후엔 다시 자동 pin되지 않게 하려면
    unpin 시 별도 플래그 필요 — 현재는 단순 임계 체크만.
    """
    rec = er.get(record_id)
    if not rec or rec.get("pinned"):
        return False
    sources = rec.get("sources") or []
    if len(sources) >= AUTO_PIN_SOURCES:
        er.update(record_id, pinned=True)
        return True
    distinct_channels = {s.get("channel") for s in sources if s.get("channel")}
    if len(distinct_channels) >= AUTO_PIN_CHANNELS:
        er.update(record_id, pinned=True)
        return True
    return False


# ── 유사 검색 ─────────────────────────────────────────────


def find_similar(text: str, entities: Optional[Sequence[str]] = None, top_k: int = 5) -> list[dict]:
    """의미적 유사 record 후보 — entity_records 직접 read (in-memory 일관).

    Retrieval index(FTS5)는 ingest 주기 의존이라 방금 add된 record는 못 잡음 →
    judge가 잘못 ADD로 떨어지는 bug. Mem0 manage는 fact-compare가 본질이라
    entity_records.by_entity + 단어 overlap score로 직접 검색.

    candidate scope: entities 매칭되는 active record. 한 entity당 수십~수백 건 규모라
    Python-side score로도 빠름.
    """
    if not text:
        return []
    candidates: list[dict] = []
    seen: set[str] = set()
    if entities:
        for ent in entities:
            for r in er.by_entity(ent):
                rid = r.get("id")
                if rid and rid not in seen:
                    seen.add(rid)
                    candidates.append(r)
    else:
        candidates = er.all_records()

    if not candidates:
        return []

    # 단어 overlap score (한국어 + 영어). 유사도 0이면 제외.
    new_words = set(re.findall(r"[\w가-힣]+", text))
    if not new_words:
        return []
    scored = []
    for c in candidates:
        c_text = c.get("text") or ""
        c_words = set(re.findall(r"[\w가-힣]+", c_text))
        if not c_words:
            continue
        overlap = len(new_words & c_words)
        if overlap == 0:
            continue
        # Jaccard-like — 짧은 fact가 우대받게
        score = overlap / max(1, len(new_words | c_words))
        scored.append((score, c))

    scored.sort(reverse=True, key=lambda x: x[0])
    return [c for _, c in scored[:top_k]]


# ── Mem0 4-action judge ───────────────────────────────────


def _sanitize_input(text: str) -> str:
    """fact 추출 입력 정제 — 사진 첨부 placeholder(￼/￼)·[사진:..] 래퍼 제거.

    iMessage 사진 첨부 텍스트 '￼ [사진: ...설명...]'이 통째로 fact가 되고
    key='￼ [사진'으로 매번 CONFLICT 나던 버그 차단. 래퍼를 벗겨 실제 설명만 남김.
    """
    import re as _re
    t = (text or "").replace("￼", "").replace("�", "").strip()
    m = _re.match(r"^\s*\[\s*사진\s*[:：]\s*(.+?)\s*\]\s*$", t, _re.S)
    if m:
        t = m.group(1).strip()
    return t


def _extract_key(text: str) -> Optional[str]:
    """fact text의 'key: value' 형식에서 key 추출. 없으면 None."""
    if ":" not in text:
        return None
    k = text.split(":", 1)[0].strip()
    # 쓰레기 key 가드: placeholder·[사진 래퍼·대괄호 시작은 key로 보지 않음 (false CONFLICT 방지)
    if not k or "￼" in k or "�" in k or k.startswith("[") or "사진" in k:
        return None
    return k if 0 < len(k) <= 50 else None


def judge_action(new_fact: dict, similar: list[dict]) -> dict:
    """ADD / UPDATE / DELETE / NOOP 결정. 룰 기반 (LLM judge는 후속).

    Returns: {"action": str, "target_id": Optional[str], "reason": str}
    """
    new_text = (new_fact.get("text") or "").strip()
    if not new_text:
        return {"action": "NOOP", "target_id": None, "reason": "빈 fact"}

    if not similar:
        return {"action": "ADD", "target_id": None, "reason": "유사 record 없음"}

    # 가장 유사한 candidate
    candidate = similar[0]
    cand_text = (candidate.get("text") or "").strip()
    cand_id = candidate.get("id")

    # NOOP — 정확 일치
    if new_text == cand_text:
        return {"action": "NOOP", "target_id": cand_id,
                "reason": "동일 fact (source 추가만)"}

    new_key = _extract_key(new_text)
    cand_key = _extract_key(cand_text)

    # 부분 substring 먼저 체크 — 풍부 ↔ 부분집합 관계는 UPDATE/NOOP (CONFLICT 아님)
    if len(new_text) > len(cand_text) and cand_text in new_text:
        return {"action": "UPDATE", "target_id": cand_id,
                "reason": "기존이 새 fact의 부분집합 — 풍부한 표현으로 갱신"}
    if len(cand_text) > len(new_text) and new_text in cand_text:
        return {"action": "NOOP", "target_id": cand_id,
                "reason": "새 fact가 기존의 부분 — source 추가만"}

    # 같은 key 다른 value → CONFLICT (P2.3 temporal validity).
    # 옛 record는 valid_to로 invalidate, 새 record를 ADD해 이력 보존.
    # last-writer-wins로 옛 사실을 잃지 않게 — "X 시점엔 이게 참이었다" 질의 가능.
    if new_key and cand_key and new_key == cand_key:
        return {"action": "CONFLICT", "target_id": cand_id,
                "reason": f"같은 key '{new_key}', 모순 value — 옛 fact invalidate + 새 fact ADD"}

    # 그 외: 다른 fact로 ADD
    return {"action": "ADD", "target_id": None, "reason": "유사하지만 다른 fact"}


# ── Apply action ──────────────────────────────────────────


def apply_action(decision: dict, new_fact: dict) -> Optional[dict]:
    """결정 적용 → entity_records 갱신. apply 후 maybe_auto_pin 자동 호출."""
    action = decision.get("action")
    target_id = decision.get("target_id")
    src = new_fact.get("source")  # {channel, ts, msg_id}

    if action == "NOOP":
        if target_id and src:
            er.add_source(target_id, src)
            maybe_auto_pin(target_id)
        return er.get(target_id) if target_id else None

    if action == "UPDATE":
        if not target_id:
            return None
        er.update(target_id, text=new_fact["text"])
        if src:
            er.add_source(target_id, src)
        maybe_auto_pin(target_id)
        return er.get(target_id)

    if action == "ADD":
        sources = [src] if src else []
        rec = er.add(
            text=new_fact["text"],
            entities=new_fact.get("entities") or [],
            tags=new_fact.get("tags") or [],
            importance=int(new_fact.get("importance", 5)),
            sources=sources,
        )
        # P2.4: Reflection importance accumulator + 임계 도달 시 background trigger
        _bump_reflection(int(new_fact.get("importance", 5)))
        return rec

    if action == "CONFLICT":
        # P2.3 temporal validity — 옛 record를 valid_to로 invalidate (이력 보존),
        # 새 record를 ADD하며 valid_from 설정.
        from datetime import datetime
        now_iso = datetime.now().isoformat(timespec="seconds")
        old_record = er.get(target_id) if target_id else None
        if target_id:
            er.update(target_id, valid_to=now_iso)
        sources = [src] if src else []
        new_record = er.add(
            text=new_fact["text"],
            entities=new_fact.get("entities") or [],
            tags=new_fact.get("tags") or [],
            importance=int(new_fact.get("importance", 5)),
            sources=sources,
            valid_from=now_iso,
        )
        # P2.4: Reflection importance accumulator (새 record 분만)
        _bump_reflection(int(new_fact.get("importance", 5)))
        # P2.3 보강 — CONFLICT는 audit_log에 기록 (의도치 않은 invalidate rollback 용이)
        try:
            from tombstones import _ensure_dir, AUDIT_LOG_FILE
            import json as _json
            _ensure_dir()
            audit_record = {
                "ts": now_iso,
                "action": "conflict_invalidate",
                "old_record_id": target_id,
                "new_record_id": new_record["id"] if new_record else None,
                "old_text": (old_record or {}).get("text", "")[:200],
                "new_text": new_fact.get("text", "")[:200],
                "reason": decision.get("reason", ""),
                "actor": "auto",
            }
            with open(AUDIT_LOG_FILE, "a") as f:
                f.write(_json.dumps(audit_record, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[memory_manager] CONFLICT audit 실패: {e}", flush=True)
        return new_record

    if action == "DELETE":
        if target_id:
            er.soft_delete(target_id,
                           reason=decision.get("reason", "auto"),
                           actor="auto")
        return None

    return None


# ── Fact extractor (P2.2a stub — LLM 연결은 P2.2b) ──────


_FACT_EXTRACT_SYSTEM = """\
너는 가족 비서 AI '리안'의 기억 시스템에서 fact extractor 역할을 맡았다.
사용자 메시지에서 사람·관계·일정·선호 같은 의미 있는 사실(fact)을 추출해
JSON 배열로만 응답한다.

형식: [{"text": "...", "entities": ["{primary}"], "importance": 5}, ...]

규칙:
- text: 한국어 자연 문장 ("거주지: 인천 영종도" 같은 key:value 형식 권장)
- entities: {entity_list} 중 매칭.
{entity_mapping}
- importance: 1~10
  - 9~10: 정체성·핵심 (이름/거주지/직장/가족 구성)
  - 6~8: 의미 있는 사실 (선호·일정·관계 변화)
  - 3~5: 일상 사실 (오늘 점심·운동·날씨 코멘트)
  - 1~2: 잡담 (단답·이모지·인사)

추출 안 할 것:
- "ㅋㅋ" "응" "ㅇㅇ" 같은 단답·반응
- 이미 완료된 일회성 인사 (잘자/굿모닝)
- 의미 없는 메시지 → 빈 배열 []

JSON만 출력. 다른 설명·코드블록·접두사 금지.
"""


def _fill_fact_extract_entities():
    """fact 추출 프롬프트의 엔티티 매핑을 contacts.yaml에서 렌더.
    config 없으면 하드코딩 fallback — 동작 동일."""
    try:
        from liain import config as _lc
        aliases = _lc.entity_aliases()        # {entity: [alias,...]} — contacts.yaml
        primary = _lc.primary_entity() or "user"
    except Exception:
        aliases, primary = {}, "user"
    if not aliases:
        # config 미설정 — 중립 기본값. 실제 사람/별칭은 contacts.yaml에서만 온다.
        aliases = {primary: [primary]}
    entity_list = list(aliases.keys()) + ["group"]
    entity_list_str = "[" + ", ".join(f'"{e}"' for e in entity_list) + "]"
    lines = [f'  - {"/".join(al)} → "{ent}"' for ent, al in aliases.items()]
    lines.append('  - 가족 단톡 자체 → "group"')
    return (_FACT_EXTRACT_SYSTEM
            .replace("{primary}", primary)
            .replace("{entity_list}", entity_list_str)
            .replace("{entity_mapping}", "\n".join(lines)))


_FACT_EXTRACT_SYSTEM = _fill_fact_extract_entities()


def _try_llm_extract(text: str, speaker: Optional[str], entities_hint: Optional[Sequence[str]]) -> Optional[list[dict]]:
    """Local Llama(Ollama qwen3:14b) 우선 + Claude CLI fallback로 fact 추출.

    qwen3:14b로 통일 — 도구 호출과 같은 모델 공유로 RAM/관리 단순화.
    fact 추출은 단순 작업이지만 8b도 충분히 빠름 (~2-3초). 1.7b는 정확도 부족으로 폐기.
    """
    hint = ""
    if speaker:
        hint += f"speaker: {speaker}\n"
    if entities_hint:
        hint += f"이미 알려진 entities: {list(entities_hint)}\n"
    user_prompt = f"/no_think {hint}메시지: {text}\n\nfact JSON 배열만 (예: [{{\"text\":\"...\",\"entities\":[\"...\"],\"importance\":5}}]):"

    response = None
    try:
        from local_llm import call_local, is_ready
        if is_ready("qwen3:14b"):
            # 타임아웃 8s: qwen3:14b가 이 하드웨어에서 ~10s+빈 응답으로 불안정 →
            # 빠르게 Claude(구독, 신뢰)로 폴백. 로컬이 빨라지면 다시 올린다.
            response = call_local(
                system="/no_think " + _FACT_EXTRACT_SYSTEM,
                prompt=user_prompt,
                model="qwen3:14b",
                timeout=8,
                temperature=0.2,
                caller="memory_manager._try_llm_extract",
            )
    except Exception as e:
        print(f"[memory_manager] local LLM 실패: {e}", flush=True)

    # local 실패 시 Claude CLI fallback
    if not response:
        try:
            from liain import llm as _llm
            # /no_think 프리픽스는 qwen3 전용 — Claude는 슬래시 명령으로 오인("Unknown command").
            # Claude 폴백엔 제거한 깨끗한 프롬프트 사용.
            claude_prompt = user_prompt.replace("/no_think ", "", 1)
            response = _llm.complete("reasoning", "", claude_prompt, max_turns=1, timeout=30,
                system=_FACT_EXTRACT_SYSTEM,
            )
        except Exception as e:
            print(f"[memory_manager] Claude CLI fallback 실패: {e}", flush=True)
            return None

    if not response:
        return None
    return _parse_llm_facts(response)


def _parse_llm_facts(response: str) -> Optional[list[dict]]:
    """LLM 응답을 JSON list로 파싱. 코드블록·접두사·빈 배열 모두 처리."""
    import json
    s = response.strip()
    # 코드블록 제거
    s = re.sub(r"^```(?:json)?\s*\n?", "", s)
    s = re.sub(r"\n?```\s*$", "", s)
    # 첫 [ ~ 마지막 ] 추출
    m = re.search(r"\[.*\]", s, re.DOTALL)
    if m:
        s = m.group(0)
    try:
        data = json.loads(s)
    except Exception as e:
        print(f"[memory_manager] LLM JSON parse 실패: {e}, raw[:120]={s[:120]!r}", flush=True)
        return None
    if not isinstance(data, list):
        return None
    # validate fields
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        text = (item.get("text") or "").strip()
        if not text or len(text) < 3:
            continue
        ents = item.get("entities") or []
        if not isinstance(ents, list):
            ents = [str(ents)]
        importance = int(item.get("importance", 5)) if isinstance(item.get("importance"), (int, float)) else 5
        out.append({
            "text": text[:300],
            "entities": [str(e) for e in ents][:5],
            "importance": max(1, min(10, importance)),
        })
    return out


def extract_facts(text: str, *, speaker: Optional[str] = None,
                  entities_hint: Optional[Sequence[str]] = None,
                  use_llm: bool = True) -> list[dict]:
    """text에서 fact 추출. LLM 시도 → 실패/disabled 시 stub.

    use_llm=False: 강제 stub (테스트용).
    LLM extract는 정확도 높음 — 잡담은 빈 배열, 의미 fact만 추출.
    Stub은 메시지 전체를 1 fact로 — judge의 NOOP/UPDATE가 후처리.
    """
    text = (text or "").strip()
    if len(text) < 5:
        return []
    if use_llm:
        llm_result = _try_llm_extract(text, speaker, entities_hint)
        if llm_result is not None:  # 빈 list도 LLM 판단이라 그대로 사용
            return llm_result
    # Stub fallback
    return [{
        "text": text[:300],
        "entities": list(entities_hint) if entities_hint else (
            [speaker] if speaker else []
        ),
        "importance": 5,
    }]


# ── Full pipeline ─────────────────────────────────────────


def process_message(
    text: str,
    *,
    channel: str,
    ts: str,
    msg_id: str = "",
    speaker: Optional[str] = None,
    entities_hint: Optional[Sequence[str]] = None,
) -> list[dict]:
    """메시지 1건 → fact 추출 → judge → apply 파이프라인.

    Returns: list of {fact, decision, record} per extracted fact.
    daemon hook(P2.2b)에서 background thread로 호출.
    """
    text = _sanitize_input(text)  # 사진 첨부 ￼·[사진:] 래퍼 제거 (false CONFLICT 차단)
    facts = extract_facts(text, speaker=speaker, entities_hint=entities_hint)
    results = []
    for f in facts:
        f["source"] = {"channel": channel, "ts": ts, "msg_id": msg_id}
        similar = find_similar(f["text"], entities=f.get("entities"))
        decision = judge_action(f, similar)
        record = apply_action(decision, f)
        results.append({"fact": f, "decision": decision, "record": record})
    return results


__all__ = [
    "find_similar",
    "judge_action",
    "apply_action",
    "maybe_auto_pin",
    "extract_facts",
    "process_message",
    "AUTO_PIN_SOURCES",
    "AUTO_PIN_CHANNELS",
]
