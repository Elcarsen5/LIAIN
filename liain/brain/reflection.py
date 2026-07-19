"""Reflection engine (P2.4) — Park 2023 importance-sum trigger.

자료(agent_memory_strategy_2026-04-27.md, memory_5layer_research) 권고:
- Generative Agents (Park 2023): 누적 importance가 임계 도달하면 reflection 발동.
- Reflection은 raw fact를 high-level insight로 합성 → semantic store에 저장.
- 24시간 주기 무조건 1회 + 즉시 트리거 둘 다 표준.

이 모듈:
1) accumulate(importance) — Mem0 add 시점에 호출
2) should_reflect_now() — 임계 도달 체크
3) trigger_reflection(reason) — LLM 호출해 insight record 생성 + form_opinions 연쇄
4) maybe_reflect() — daemon hook (체크 + 트리거 통합)

State: brain/reflection_state.json
{
  "accumulated_importance": 32,
  "last_reflection_ts": "2026-04-28T18:00:00",
  "reflections": [{ts, trigger, n_insights, accumulated_at_trigger}]
}

Insight record schema (entity_records 그대로, tags 만 다름):
  text, entities, tags=["insight", "reflection"], importance, sources=[evidence_record_ids]
"""
from __future__ import annotations
import os
import json
import threading
from datetime import datetime, timedelta
from typing import Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "brain", "reflection_state.json")

# 임계: 가족 4명 규모니까 작게. 너무 자주 reflect하면 노이즈.
REFLECT_THRESHOLD = 50
REFLECT_MAX_INTERVAL_HOURS = 24  # 무조건 1일 1회

_LOCK = threading.Lock()


# ── state ──────────────────────────────────────────────


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {
            "accumulated_importance": 0,
            "last_reflection_ts": None,
            "reflections": [],
        }
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {
            "accumulated_importance": 0,
            "last_reflection_ts": None,
            "reflections": [],
        }


def _save_state(s: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


# ── public API ─────────────────────────────────────────


def accumulate(importance: int) -> dict:
    """fact ADD 시점에 호출. 누적 importance 증가."""
    with _LOCK:
        s = _load_state()
        s["accumulated_importance"] = s.get("accumulated_importance", 0) + max(0, int(importance))
        _save_state(s)
        return s


def should_reflect_now() -> tuple[bool, str]:
    """(Y/N, reason). 임계 또는 24h+ 경과면 True."""
    s = _load_state()
    acc = s.get("accumulated_importance", 0)
    if acc >= REFLECT_THRESHOLD:
        return True, f"importance_sum>={REFLECT_THRESHOLD} (acc={acc})"
    last = s.get("last_reflection_ts")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if datetime.now() - last_dt > timedelta(hours=REFLECT_MAX_INTERVAL_HOURS):
                return True, f"24h_passed (last={last})"
        except Exception:
            pass
    else:
        # 한 번도 안 한 상태 + 임계 미달이면 일단 대기.
        return False, "never_reflected_yet"
    return False, f"acc={acc}<{REFLECT_THRESHOLD}"


def state() -> dict:
    """현재 누적/이력 조회 (Console 표시용)."""
    return _load_state()


def recent_reflections(limit: int = 10) -> list[dict]:
    s = _load_state()
    return list(reversed(s.get("reflections", [])))[:limit]


# ── LLM helpers ────────────────────────────────────────


def _call_sonnet(system: str, user_content: str, max_tokens: int = 1200) -> str:
    from liain import llm as _llm
    return _llm.complete("reasoning", system=system,
        user_prompt=user_content,
        model_alias="sonnet",
        api_model="claude-sonnet-4-20250514",
        max_tokens=max_tokens,
        timeout=180,
        allow_api_fallback=False,  # background reflection — CLI 실패 시 skip
    )


def _fix_truncated_json(text: str):
    """LLM 출력에서 JSON만 추출. fenced ``` 제거."""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        # ``` 제거 후 첫 ``` 까지
        t = t[3:]
        if t.lstrip().startswith("json"):
            t = t.lstrip()[4:]
        end = t.rfind("```")
        if end != -1:
            t = t[:end]
    t = t.strip()
    # { 부터 마지막 } 까지
    s = t.find("{")
    e = t.rfind("}")
    if s == -1 or e == -1 or e < s:
        return None
    try:
        return json.loads(t[s:e + 1])
    except Exception:
        return None


# ── reflection core ────────────────────────────────────


_REFLECT_SYSTEM = """\
너는 가족 비서 AI '리안'의 reflection engine이다.
최근 누적된 fact / 에피소드를 보고, raw 사실을 넘어선 high-level insight를 합성하라.

좋은 insight 예:
- "A가 한 주에 학교 스트레스 신호를 3번 보냄. 패턴이 형성됨."
- "사용자가 출퇴근 시 가장 활발히 대화함 — 이 시간대에 가벼운 대화가 효과적."
- "B는 칭찬보다 호기심 묻는 질문에 더 길게 반응."

피해야 할 것:
- raw fact 그대로 복사 (이미 records에 있음)
- 근거 부족한 추측 ("아마도", "어쩌면")
- 너무 일반적 ("가족이 소중하다")

출력은 JSON one object:
{
  "insights": [
    {
      "text": "한 문장 통찰. 패턴/연결/메타.",
      "entities": ["member1", ...],   // 관련 entity id들
      "tags": ["insight", "pattern"], // pattern/risk/preference 등 자유
      "importance": 6,                 // 5~9
      "evidence_record_ids": ["fact_xxx", "fact_yyy"]  // 근거가 된 record id
    }
  ],
  "no_new_insight": false   // 새 insight 없으면 true + insights=[]
}

규칙:
- insight는 0~5개. 무리해서 만들지 말 것.
- evidence_record_ids는 반드시 입력 records에서. 빈 리스트면 안 됨.
- 동일/유사한 insight가 입력에 이미 표시되어 있으면(태그 'insight'가 있는 레코드) 새로 만들지 말 것."""


def _gather_inputs(days: int = 3, importance_min: int = 5) -> dict:
    """최근 records (active, importance>=min) + episodes + 기존 insights."""
    out = {"records": [], "episodes": [], "existing_insights": []}
    try:
        from . import records as er
        all_recs = er.all_records()
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        for r in all_recs:
            if not r.get("text"):
                continue
            tags = r.get("tags") or []
            if "insight" in tags or "reflection" in tags:
                # 기존 insight는 별도 집합 (LLM에 prior로 전달)
                out["existing_insights"].append({
                    "id": r.get("id"),
                    "text": r["text"][:200],
                    "tags": tags,
                })
                continue
            ts = r.get("updated_at") or r.get("created_at") or ""
            if ts < cutoff:
                continue
            if int(r.get("importance", 0)) < importance_min:
                continue
            out["records"].append({
                "id": r.get("id"),
                "text": r["text"][:240],
                "entities": r.get("entities") or [],
                "tags": tags,
                "importance": int(r.get("importance", 5)),
                "ts": ts,
            })
    except Exception as e:
        print(f"[reflection] records load 실패: {e}", flush=True)

    # episodes (raw log) — 최근 3일
    ep_dir = os.path.join(BASE_DIR, "brain", "episodes")
    if os.path.isdir(ep_dir):
        try:
            files = sorted(os.listdir(ep_dir), reverse=True)
            for fn in files[:days]:
                if not fn.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(ep_dir, fn)) as f:
                        ep = json.load(f)
                    out["episodes"].append({
                        "date": ep.get("date") or fn.replace(".json", ""),
                        "title": ep.get("title", ""),
                        "summary": (ep.get("summary") or ep.get("content") or "")[:300],
                    })
                except Exception:
                    continue
        except Exception:
            pass

    # 너무 많이 보내지 말 것 — 토큰 예산
    out["records"] = out["records"][:60]
    out["existing_insights"] = out["existing_insights"][-20:]
    return out


def _persist_insights(insights: list[dict]) -> int:
    """insight를 entity_records에 add. evidence id를 sources로."""
    from . import records as er
    added = 0
    for ins in insights:
        text = (ins.get("text") or "").strip()
        if not text:
            continue
        ent = list(ins.get("entities") or [])
        tags = list({"insight", "reflection", *(ins.get("tags") or [])})
        importance = int(ins.get("importance", 6))
        evidence = ins.get("evidence_record_ids") or []
        sources = [{"channel": "reflection", "ts": _now(), "msg_id": rid}
                   for rid in evidence if isinstance(rid, str)]
        try:
            er.add(
                text=text,
                entities=ent,
                tags=tags,
                importance=importance,
                sources=sources,
                valid_from=_now(),
            )
            added += 1
        except Exception as e:
            print(f"[reflection] insight add 실패: {e}", flush=True)
    return added


def trigger_reflection(reason: str = "manual",
                       run_form_opinions: bool = True) -> dict:
    """LLM 호출 → insight record 생성 → form_opinions 연쇄 → state reset.

    반환: {ok, n_insights, opinions_changes, reason, ts}
    """
    started = _now()
    inputs = _gather_inputs()
    n_recs = len(inputs["records"])
    n_eps = len(inputs["episodes"])

    if n_recs == 0 and n_eps == 0:
        # 입력 없음 — state만 reset (idle).
        with _LOCK:
            s = _load_state()
            s["accumulated_importance"] = 0
            s["last_reflection_ts"] = started
            s.setdefault("reflections", []).append({
                "ts": started, "trigger": reason,
                "n_insights": 0, "skipped": "no_input",
            })
            s["reflections"] = s["reflections"][-50:]
            _save_state(s)
        return {"ok": True, "n_insights": 0, "opinions_changes": 0,
                "reason": reason, "ts": started, "skipped": "no_input"}

    user_payload = json.dumps(inputs, ensure_ascii=False, indent=2)
    n_insights = 0
    try:
        raw = _call_sonnet(system=_REFLECT_SYSTEM, user_content=user_payload, max_tokens=1500)
        parsed = _fix_truncated_json(raw)
        if not parsed:
            print(f"[reflection] LLM JSON 파싱 실패: {(raw or '')[:120]}", flush=True)
        elif parsed.get("no_new_insight"):
            print(f"[reflection] no_new_insight (records={n_recs}, eps={n_eps})", flush=True)
        else:
            n_insights = _persist_insights(parsed.get("insights") or [])
            print(f"[reflection] {n_insights}개 insight 추가", flush=True)
    except Exception as e:
        print(f"[reflection] LLM 호출 실패: {e}", flush=True)

    # form_opinions로 worldview/beliefs 진화 연쇄 (LLM 1회 추가)
    opinions_changes = 0
    if run_form_opinions:
        try:
            from brain.thinker import form_opinions
            opinions_changes = form_opinions() or 0
        except Exception as e:
            print(f"[reflection] form_opinions 실패: {e}", flush=True)

    finished = _now()
    with _LOCK:
        s = _load_state()
        s["accumulated_importance"] = 0
        s["last_reflection_ts"] = finished
        s.setdefault("reflections", []).append({
            "ts": finished,
            "trigger": reason,
            "n_insights": n_insights,
            "opinions_changes": opinions_changes,
            "input_records": n_recs,
            "input_episodes": n_eps,
        })
        s["reflections"] = s["reflections"][-50:]
        _save_state(s)

    return {
        "ok": True,
        "n_insights": n_insights,
        "opinions_changes": opinions_changes,
        "reason": reason,
        "ts": finished,
        "input_records": n_recs,
        "input_episodes": n_eps,
    }


def maybe_reflect() -> Optional[dict]:
    """daemon hook. 임계 도달하면 트리거. 아니면 None."""
    yes, why = should_reflect_now()
    if not yes:
        return None
    print(f"[reflection] trigger: {why}", flush=True)
    return trigger_reflection(reason=why)


__all__ = [
    "accumulate",
    "should_reflect_now",
    "trigger_reflection",
    "maybe_reflect",
    "state",
    "recent_reflections",
    "REFLECT_THRESHOLD",
    "REFLECT_MAX_INTERVAL_HOURS",
]
