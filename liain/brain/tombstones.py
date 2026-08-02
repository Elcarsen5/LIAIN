"""Tombstone 기반 soft delete + 감사 로그.

설계:
- 모든 메모리 삭제는 "마크"만 — brain/tombstones.jsonl에 append-only.
- retrieval 단계에서 tombstoned path를 강제 skip.
- N일 retention 후 vacuum(실제 hard delete).
- 되돌리기 가능(vacuum 전에는 tombstone 제거로 복원).

모든 delete/conflict 변경은 brain/audit_log.jsonl에 함께 기록돼 추적 가능하다.

Path 표현:
  "record.{id}"      — entity record (uuid)
  "episode.{date}"   — 에피소드
"""
from __future__ import annotations
import os
import json
import threading
from datetime import datetime, timedelta
from typing import Optional

from .paths import BRAIN_DIR

TOMBSTONES_FILE = os.path.join(BRAIN_DIR, "tombstones.jsonl")
AUDIT_LOG_FILE = os.path.join(BRAIN_DIR, "audit_log.jsonl")

DEFAULT_VACUUM_DAYS = 90

_LOCK = threading.Lock()


def _ensure_dir():
    os.makedirs(os.path.dirname(TOMBSTONES_FILE), exist_ok=True)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def add_tombstone(
    path: str,
    snapshot,
    actor: str = "user",
    reason: str = "",
    vacuum_days: int = DEFAULT_VACUUM_DAYS,
) -> dict:
    """fact를 tombstone(soft delete) 표시. 멱등: 같은 path 재호출 시 최근 entry가 effective.

    snapshot은 복원용 원래 값. JSON-직렬화 가능해야.
    """
    _ensure_dir()
    record = {
        "ts": _now_iso(),
        "action": "tombstone",
        "path": path,
        "actor": actor,
        "reason": reason,
        "snapshot": snapshot,
        "vacuum_after": (datetime.now() + timedelta(days=vacuum_days)).isoformat(timespec="seconds"),
    }
    with _LOCK:
        with open(TOMBSTONES_FILE, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        with open(AUDIT_LOG_FILE, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return record


def revert_tombstone(path: str, actor: str = "user", reason: str = "") -> Optional[dict]:
    """tombstone 취소. snapshot 복원은 호출자가 처리. audit log엔 'revert' record append."""
    _ensure_dir()
    record = {
        "ts": _now_iso(),
        "action": "revert_tombstone",
        "path": path,
        "actor": actor,
        "reason": reason,
    }
    with _LOCK:
        with open(TOMBSTONES_FILE, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        with open(AUDIT_LOG_FILE, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return record


def _load_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def active_tombstones() -> set[str]:
    """현재 활성 tombstone path 집합. 같은 path의 tombstone→revert→tombstone은 최근 action 기준."""
    state: dict[str, str] = {}
    for r in _load_jsonl(TOMBSTONES_FILE):
        p = r.get("path")
        a = r.get("action")
        if p and a in ("tombstone", "revert_tombstone"):
            state[p] = a
    return {p for p, a in state.items() if a == "tombstone"}


def is_tombstoned(path: str) -> bool:
    return path in active_tombstones()


def get_snapshot(path: str) -> Optional[dict]:
    """가장 최근 tombstone entry의 snapshot 반환 (복원용)."""
    snaps = [r for r in _load_jsonl(TOMBSTONES_FILE)
             if r.get("path") == path and r.get("action") == "tombstone"]
    return snaps[-1] if snaps else None


def list_recent(limit: int = 50) -> list[dict]:
    """최근 audit log entries (모든 action)."""
    return _load_jsonl(AUDIT_LOG_FILE)[-limit:]


__all__ = [
    "add_tombstone",
    "revert_tombstone",
    "active_tombstones",
    "is_tombstoned",
    "get_snapshot",
    "list_recent",
    "AUDIT_LOG_FILE",
    "DEFAULT_VACUUM_DAYS",
]
