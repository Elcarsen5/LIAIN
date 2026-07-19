"""Entity-tagged record store (P2.1).

자료(agent_memory_strategy_2026-04-27.md) 권고:
- 사람별 partition({person: {key: val}}) 구조는 단일 인격성과 충돌.
- entity-tagged record list가 표준 (Memoria, MIRIX, AdaptiveFriend 등).
- record 1개에 sources[] 배열로 통합 카운트 — 단톡·1:1·다른 자리에서 안 같은
  사실은 한 record에 합쳐짐 (사용자 핵심 요구).

Schema (brain/records.json):
{
  "records": [
    {
      "id": "fact_<12-hex>",
      "text": "A가 시바견을 키운다 (검정색)",
      "entities": ["member1", "pet"],   # 관련 entity ID들
      "tags": ["pet", "family"],         # 자유 태그
      "importance": 7,
      "pinned": false,                    # always-on slice
      "sources": [                        # 통합 카운트
        {"channel": "1on1_member1", "ts": "...", "msg_id": "..."},
        {"channel": "group_family", "ts": "...", "msg_id": "..."}
      ],
      "valid_from": "2026-04-11T00:00:00",  # P2.3 활용
      "valid_to": null,
      "deleted_at": null,                  # Tombstone (cascade_design)
      "created_at": "2026-04-11T16:42:24",
      "updated_at": "2026-04-27T07:12:33"
    },
    ...
  ]
}

P2.1a 범위: API + storage. 마이그레이션은 P2.1b. 호출자 전환은 P2.1c.
"""
from __future__ import annotations
import os
import json
import uuid
import threading
from datetime import datetime
from typing import Optional, Iterable

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RECORDS_FILE = os.path.join(BASE_DIR, "brain", "records.json")

_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_id() -> str:
    return f"fact_{uuid.uuid4().hex[:12]}"


def _ensure_dir():
    os.makedirs(os.path.dirname(RECORDS_FILE), exist_ok=True)


def _load() -> list[dict]:
    if not os.path.exists(RECORDS_FILE):
        return []
    try:
        with open(RECORDS_FILE) as f:
            data = json.load(f)
    except Exception:
        return []
    if isinstance(data, dict):
        return data.get("records", [])
    return data if isinstance(data, list) else []


def _save(records: list[dict]) -> None:
    _ensure_dir()
    payload = {"_description": "Entity-tagged records (P2.1+)", "records": records}
    tmp = RECORDS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, RECORDS_FILE)


# ── public API ──────────────────────────────────────────────


def all_records(include_deleted: bool = False, valid_at: Optional[str] = None) -> list[dict]:
    """모든 record 반환. tombstone(deleted_at)은 default 제외.

    valid_at (ISO timestamp): 그 시점에 유효했던 record만.
        - valid_from이 None이거나 valid_at 이전
        - valid_to가 None이거나 valid_at 이후 (즉 그 시점에 유효)
        None이면 시간 필터 안 함 (모든 활성 record).

    P2.3 temporal validity — "지금 무엇이 참 / X 시점에 무엇이 참"
    """
    recs = _load()
    if not include_deleted:
        recs = [r for r in recs if not r.get("deleted_at")]
    if valid_at is None:
        return recs
    return [r for r in recs if _valid_at_time(r, valid_at)]


def _valid_at_time(rec: dict, t: str) -> bool:
    """rec가 시점 t에 유효한가."""
    vf = rec.get("valid_from")
    vt = rec.get("valid_to")
    if vf and vf > t:
        return False
    if vt and vt <= t:
        return False
    return True


def get(record_id: str) -> Optional[dict]:
    for r in _load():
        if r.get("id") == record_id:
            return r
    return None


def by_entity(entity: str, include_deleted: bool = False) -> list[dict]:
    """entity 포함 record. (entity는 OR-매치, 여럿 필터하려면 caller에서 chain.)"""
    return [
        r for r in all_records(include_deleted=include_deleted)
        if entity in (r.get("entities") or [])
    ]


def by_tag(tag: str, include_deleted: bool = False) -> list[dict]:
    return [
        r for r in all_records(include_deleted=include_deleted)
        if tag in (r.get("tags") or [])
    ]


def add(
    text: str,
    *,
    entities: Optional[Iterable[str]] = None,
    tags: Optional[Iterable[str]] = None,
    importance: int = 5,
    pinned: bool = False,
    sources: Optional[Iterable[dict]] = None,
    valid_from: Optional[str] = None,
    valid_to: Optional[str] = None,
    record_id: Optional[str] = None,
) -> dict:
    """새 record 추가. caller가 이미 동등 record 존재 여부를 체크했다고 가정.

    중복 검출(Mem0 NOOP)은 P2.2에서 add_or_update()에 흡수. 여기는 raw add.
    """
    rec = {
        "id": record_id or _new_id(),
        "text": text,
        "entities": list(entities or []),
        "tags": list(tags or []),
        "importance": int(importance),
        "pinned": bool(pinned),
        "sources": list(sources or []),
        "valid_from": valid_from,
        "valid_to": valid_to,
        "deleted_at": None,
        "created_at": _now(),
        "updated_at": _now(),
        "embedding": None,  # P3.1: SBERT vector (background embed 후 채움)
    }
    with _LOCK:
        recs = _load()
        recs.append(rec)
        _save(recs)
    # P3.1: 백그라운드 embedding (response latency 0)
    _spawn_embed(rec["id"], text)
    return rec


def _spawn_embed(record_id: str, text: str) -> None:
    """background thread로 embedding 계산 후 record에 set. 실패 시 silently skip."""
    def _bg():
        try:
            from . import embedder as _me
            if not _me.is_ready():
                return
            vec = _me.embed(text)
            if vec is None:
                return
            with _LOCK:
                recs = _load()
                for i, r in enumerate(recs):
                    if r.get("id") == record_id:
                        r["embedding"] = vec.tolist()
                        recs[i] = r
                        _save(recs)
                        break
        except Exception as e:
            print(f"[entity_records] embed 실패 {record_id}: {e}", flush=True)
    threading.Thread(target=_bg, daemon=True).start()


def update(record_id: str, **fields) -> Optional[dict]:
    """record 일부 필드 갱신. text·importance·tags·entities·valid_to 등."""
    text_changed = False
    new_text = None
    with _LOCK:
        recs = _load()
        for i, r in enumerate(recs):
            if r.get("id") == record_id:
                if "text" in fields and fields["text"] != r.get("text"):
                    text_changed = True
                    new_text = fields["text"]
                    r["embedding"] = None  # 재계산 위해 invalidate
                for k, v in fields.items():
                    if k in ("id", "created_at"):
                        continue
                    r[k] = v
                r["updated_at"] = _now()
                recs[i] = r
                _save(recs)
                if text_changed and new_text:
                    _spawn_embed(record_id, new_text)
                return r
    return None


def add_source(record_id: str, source: dict) -> Optional[dict]:
    """sources[] append. 통합 카운트의 핵심 — 같은 fact가 다른 자리에서 confirm될 때.

    동일 channel+ts+msg_id 중복은 자동 dedupe.
    """
    with _LOCK:
        recs = _load()
        for i, r in enumerate(recs):
            if r.get("id") == record_id:
                src_list = r.setdefault("sources", [])
                key = (source.get("channel"), source.get("ts"), source.get("msg_id"))
                exists = any(
                    (s.get("channel"), s.get("ts"), s.get("msg_id")) == key
                    for s in src_list
                )
                if not exists:
                    src_list.append(source)
                    r["updated_at"] = _now()
                    recs[i] = r
                    _save(recs)
                return r
    return None


def soft_delete(record_id: str, reason: str = "", actor: str = "user:lain") -> Optional[dict]:
    """deleted_at 표시 (Tombstone과 통합). tombstones.add_tombstone도 함께 호출.

    호출자(Console 등)가 cascade preview 후에만 호출.
    """
    rec = get(record_id)
    if not rec:
        return None
    with _LOCK:
        recs = _load()
        for i, r in enumerate(recs):
            if r.get("id") == record_id:
                r["deleted_at"] = _now()
                r["updated_at"] = _now()
                recs[i] = r
                _save(recs)
                break
    # tombstones.jsonl에 audit record 동시 append.
    try:
        from tombstones import add_tombstone
        add_tombstone(
            path=f"record.{record_id}",
            snapshot=rec,
            actor=actor,
            reason=reason,
        )
    except Exception as e:
        print(f"[entity_records] tombstone audit 실패: {e}", flush=True)
    return get(record_id)


def expired_records() -> list[dict]:
    """valid_to가 설정되어 만료된 active record (P2.3 — 옛 사실 이력)."""
    from datetime import datetime
    now = datetime.now().isoformat(timespec="seconds")
    out = []
    for r in _load():
        if r.get("deleted_at"):
            continue
        vt = r.get("valid_to")
        if vt and vt <= now:
            out.append(r)
    return out


def restore_validity(record_id: str, reason: str = "", actor: str = "user:lain") -> Optional[dict]:
    """valid_to를 None으로 복원 — 만료 record를 다시 valid 상태로.

    audit log에 기록. CONFLICT로 의도치 않게 invalidate된 record 복원에 사용.
    """
    rec = update(record_id, valid_to=None)
    if rec is None:
        return None
    try:
        from tombstones import _ensure_dir, AUDIT_LOG_FILE
        import json as _json
        from datetime import datetime as _dt
        _ensure_dir()
        with open(AUDIT_LOG_FILE, "a") as f:
            f.write(_json.dumps({
                "ts": _dt.now().isoformat(timespec="seconds"),
                "action": "restore_validity",
                "record_id": record_id,
                "reason": reason,
                "actor": actor,
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[entity_records] restore_validity audit 실패: {e}", flush=True)
    return rec


def revert_delete(record_id: str, reason: str = "", actor: str = "user:lain") -> Optional[dict]:
    """deleted_at 제거 (vacuum 전에만 의미)."""
    with _LOCK:
        recs = _load()
        for i, r in enumerate(recs):
            if r.get("id") == record_id:
                r["deleted_at"] = None
                r["updated_at"] = _now()
                recs[i] = r
                _save(recs)
                break
    try:
        from tombstones import revert_tombstone
        revert_tombstone(path=f"record.{record_id}", actor=actor, reason=reason)
    except Exception as e:
        print(f"[entity_records] revert audit 실패: {e}", flush=True)
    return get(record_id)


def stats() -> dict:
    """전체/활성/엔티티별 카운트."""
    recs = _load()
    active = [r for r in recs if not r.get("deleted_at")]
    by_ent: dict[str, int] = {}
    for r in active:
        for e in r.get("entities") or []:
            by_ent[e] = by_ent.get(e, 0) + 1
    return {
        "total": len(recs),
        "active": len(active),
        "deleted": len(recs) - len(active),
        "pinned": sum(1 for r in active if r.get("pinned")),
        "by_entity": by_ent,
    }


# ── P3.2 Forgetting policy ──────────────────────────────

import math as _math

# decay rate (per day). 0.01/day → ~100일이면 weight × e^-1 (~0.37)
DECAY_LAMBDA = 0.01
RETENTION_DAYS = 90  # soft delete 후 이 기간 지나면 vacuum


def decay_weight(rec: dict, now_iso: Optional[str] = None,
                 lam: float = DECAY_LAMBDA) -> float:
    """retrieval/ranking용 effective weight.

    weight = importance × exp(-λ × age_days) × log(recall_count + 2)
    - importance가 본질, decay로 노화, recall로 자주 불리는 것 보강
    - pinned record는 caller가 별도 우선순위 처리 (이 함수는 weight만)
    """
    try:
        imp = float(rec.get("importance", 5))
    except Exception:
        imp = 5.0
    base_ts = rec.get("updated_at") or rec.get("created_at") or now_iso or _now()
    try:
        from datetime import datetime as _dt
        base_dt = _dt.fromisoformat(base_ts)
        ref_dt = _dt.fromisoformat(now_iso) if now_iso else _dt.now()
        age_days = max(0.0, (ref_dt - base_dt).total_seconds() / 86400.0)
    except Exception:
        age_days = 0.0
    decay = _math.exp(-lam * age_days)
    rc = int(rec.get("recall_count", 0) or 0)
    boost = _math.log(rc + 2)  # +2: 새 record(rc=0)도 log(2)≈0.69의 base 갖게
    return round(imp * decay * boost, 4)


def bump_recall(record_ids) -> int:
    """retrieval 결과 또는 unified_context 노출 시 recall_count +1.

    record_ids: str 또는 iterable. 한 번 호출에 여러 id 처리(disk write 1회).
    return: 갱신된 record 수.
    """
    if isinstance(record_ids, str):
        ids = {record_ids}
    else:
        ids = set(record_ids or [])
    if not ids:
        return 0
    with _LOCK:
        recs = _load()
        n = 0
        for r in recs:
            if r.get("id") in ids:
                r["recall_count"] = int(r.get("recall_count", 0) or 0) + 1
                # updated_at은 의도적으로 안 건드림 (decay age 보존).
                n += 1
        if n:
            _save(recs)
        return n


def vacuum_old(retention_days: int = RETENTION_DAYS,
               dry_run: bool = False) -> dict:
    """soft delete 후 retention_days 지난 record 영구 제거.

    Tombstone snapshot은 이미 brain/tombstones.jsonl + audit_log에 영구 보존되니
    records.json에서 제거해도 audit trail은 남음. audit_log에 vacuum entry 추가.
    """
    from datetime import datetime as _dt, timedelta as _td
    cutoff_iso = (_dt.now() - _td(days=retention_days)).isoformat(timespec="seconds")
    with _LOCK:
        recs = _load()
        keep, removed = [], []
        for r in recs:
            d_at = r.get("deleted_at")
            if d_at and d_at < cutoff_iso:
                removed.append(r)
            else:
                keep.append(r)
        if removed and not dry_run:
            _save(keep)
            # audit_log에 vacuum entry
            try:
                from tombstones import _ensure_dir, AUDIT_LOG_FILE
                import json as _json
                _ensure_dir()
                with open(AUDIT_LOG_FILE, "a") as f:
                    for r in removed:
                        f.write(_json.dumps({
                            "ts": _now(),
                            "action": "vacuum",
                            "record_id": r.get("id"),
                            "deleted_at": r.get("deleted_at"),
                            "text": (r.get("text") or "")[:200],
                            "reason": f"retention {retention_days}일 경과",
                            "actor": "auto",
                        }, ensure_ascii=False) + "\n")
            except Exception as e:
                print(f"[entity_records] vacuum audit 실패: {e}", flush=True)
        return {
            "retention_days": retention_days,
            "cutoff": cutoff_iso,
            "removed_count": len(removed),
            "remaining": len(keep),
            "removed_ids": [r.get("id") for r in removed[:20]],
            "dry_run": dry_run,
        }


def search(query: str, *, top_k: int = 10, mode: str = "hybrid",
           entity: Optional[str] = None, include_deleted: bool = False) -> list[dict]:
    """records 검색. P3.1 hybrid retrieval.

    mode:
      "vector"  — cosine only (embedding 있는 record만)
      "keyword" — substring + entity 필터 (embedding 무관)
      "hybrid"  — RRF fuse of vector + keyword (default, 권장)
    """
    if not query or not query.strip():
        return []
    recs = all_records(include_deleted=include_deleted)
    if entity:
        recs = [r for r in recs if entity in (r.get("entities") or [])]
    if not recs:
        return []

    # keyword (substring + token overlap)
    import re as _re
    q_tokens = set(t for t in _re.findall(r"[\w가-힣]+", query.lower()) if len(t) >= 1)
    keyword_scores: list[tuple[int, float]] = []
    for i, r in enumerate(recs):
        text = (r.get("text") or "").lower()
        if not text:
            continue
        if query.lower() in text:
            keyword_scores.append((i, 2.0))  # 정확 substring 보너스
            continue
        r_tokens = set(_re.findall(r"[\w가-힣]+", text))
        overlap = len(q_tokens & r_tokens)
        if overlap > 0:
            keyword_scores.append((i, float(overlap)))
    keyword_scores.sort(key=lambda x: -x[1])
    keyword_top = keyword_scores[:30]

    # vector (cosine)
    vector_top: list[tuple[int, float]] = []
    if mode in ("hybrid", "vector"):
        try:
            from . import embedder as _me
            if _me.is_ready():
                qv = _me.embed_query(query)
                if qv is not None:
                    cands = [r.get("embedding") for r in recs]
                    # list → np.array (cosine_topk이 처리)
                    import numpy as _np
                    np_cands = [
                        _np.array(c) if c is not None else None
                        for c in cands
                    ]
                    vector_top = _me.cosine_topk(qv, np_cands, k=30)
        except Exception as e:
            print(f"[search] vector 실패: {e}", flush=True)

    if mode == "vector":
        ranked = vector_top
    elif mode == "keyword":
        ranked = keyword_top
    else:
        # RRF fuse
        ranked = _rrf_fuse(vector_top, keyword_top, k=60)
    ranked = ranked[:top_k]

    out = []
    for idx, score in ranked:
        if 0 <= idx < len(recs):
            r = dict(recs[idx])
            r["_score"] = round(score, 4)
            r.pop("embedding", None)  # 응답 사이즈 줄임
            out.append(r)
    return out


def _rrf_fuse(*ranked_lists, k: int = 60) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion. 각 list는 [(idx, score), ...] (score 무관, rank만 사용).

    score = Σ 1/(k + rank_i). k=60 (standard).
    """
    rrf: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, (idx, _) in enumerate(ranked):
            rrf[idx] = rrf.get(idx, 0.0) + 1.0 / (k + rank)
    return sorted(rrf.items(), key=lambda x: -x[1])


def backfill_embeddings(batch_size: int = 32, only_missing: bool = True) -> dict:
    """기존 records에 embedding이 없는(또는 모두) 것을 batch로 embed.

    P3.1d — 마이그 후 1회 실행 또는 필요 시 재계산.
    Returns: {processed, skipped, total}
    """
    try:
        from . import embedder as _me
        if not _me.is_ready():
            return {"error": "memory_embedder not ready", "processed": 0}
    except Exception as e:
        return {"error": str(e), "processed": 0}

    with _LOCK:
        recs = _load()
    targets = [(i, r) for i, r in enumerate(recs)
               if r.get("text") and (not only_missing or not r.get("embedding"))]
    total = len(targets)
    if not targets:
        return {"processed": 0, "skipped": 0, "total": 0}
    print(f"[backfill] embedding {total}건 시작 (batch={batch_size})", flush=True)

    processed = 0
    for chunk_start in range(0, total, batch_size):
        chunk = targets[chunk_start:chunk_start + batch_size]
        texts = [r["text"] for _, r in chunk]
        vecs = _me.embed_batch(texts)
        if vecs is None:
            continue
        # update in place
        with _LOCK:
            recs = _load()
            for (orig_idx, _), vec in zip(chunk, vecs):
                if orig_idx < len(recs):
                    recs[orig_idx]["embedding"] = vec.tolist()
            _save(recs)
        processed += len(chunk)
        print(f"[backfill] {processed}/{total}", flush=True)

    return {"processed": processed, "total": total, "skipped": total - processed}


__all__ = [
    "all_records",
    "get",
    "by_entity",
    "by_tag",
    "add",
    "update",
    "add_source",
    "soft_delete",
    "revert_delete",
    "restore_validity",
    "expired_records",
    "stats",
    "decay_weight",
    "bump_recall",
    "vacuum_old",
    "backfill_embeddings",
    "search",
    "DECAY_LAMBDA",
    "RETENTION_DAYS",
    "RECORDS_FILE",
]
