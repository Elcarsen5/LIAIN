"""MemoryRetriever 인터페이스 + SQLite FTS5 BM25 구현.

질의 기반 인출(retrieval) 레이어. `brain/records.py`(JSON 기억)·에피소드·단기기억을
SQLite FTS5 인덱스로 미러링하고 BM25 + 시간 anchor + entity 메타로 검색한다.
`working.py`의 하이브리드 융합(BM25 + dense)이 이 레이어를 한 축으로 쓴다.

구조:
- 인터페이스(`MemoryRetriever`)는 hybrid 기준으로 추상화 — 구현 교체 시 호출부 무수정.
- 구현(`SqliteFts5Retriever`)은 BM25(FTS5) + 한국어 시간 anchor + entity 필터.

DB schema (brain/retrieval.db):
  records       — id, source, source_id, ts, speaker, entities(JSON), text, importance, deleted_at
  records_fts   — records.text + records.entities 가상 테이블(BM25)

source 종류:
  - record     : brain/records.py 의 fact (source_id = record id)
  - episode    : brain/episodes/*.json (source_id = "{date}")
  - short_term : brain/short_term.json items (source_id = "{date}#{idx}")

ingest_all()은 멱등 — 같은 (source, source_id)를 INSERT or UPDATE. 변경 감지는
text·entities·importance 모두 보므로 actor/about 재태깅이 FTS에 전파된다.
deleted_at(tombstone)은 ingest 시 동기화되고 retrieve에서 제외된다.

FTS 인덱스는 마지막 ingest_all() 시점의 스냅샷 — 호출부가 주기적으로 갱신해야 한다.
"""
from __future__ import annotations
import os
import json
import sqlite3
import glob
from datetime import datetime, date, timedelta
from typing import Optional, Sequence

from .paths import BRAIN_DIR

RETRIEVAL_DB = os.path.join(BRAIN_DIR, "retrieval.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    speaker TEXT,
    entities TEXT,
    text TEXT NOT NULL,
    importance INTEGER DEFAULT 5,
    deleted_at TEXT,
    UNIQUE(source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_records_ts ON records(ts);
CREATE INDEX IF NOT EXISTS idx_records_source ON records(source);
CREATE INDEX IF NOT EXISTS idx_records_deleted ON records(deleted_at);

CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
    text, entities,
    content='records',
    content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS records_ai AFTER INSERT ON records BEGIN
    INSERT INTO records_fts(rowid, text, entities) VALUES (new.id, new.text, new.entities);
END;
CREATE TRIGGER IF NOT EXISTS records_ad AFTER DELETE ON records BEGIN
    INSERT INTO records_fts(records_fts, rowid, text, entities)
        VALUES('delete', old.id, old.text, old.entities);
END;
CREATE TRIGGER IF NOT EXISTS records_au AFTER UPDATE ON records BEGIN
    INSERT INTO records_fts(records_fts, rowid, text, entities)
        VALUES('delete', old.id, old.text, old.entities);
    INSERT INTO records_fts(rowid, text, entities) VALUES (new.id, new.text, new.entities);
END;
"""


class MemoryRetriever:
    """추상 인터페이스. HybridRetriever(vector + BM25 + KG) 확장 여지.

    호출자(WorkingMemoryAssembler 등)는 이 인터페이스만 의존 — 구현 교체 시 무수정.
    """

    def retrieve(
        self,
        query: str,
        *,
        time_range: Optional[tuple[str, str]] = None,
        entities: Optional[Sequence[str]] = None,
        sources: Optional[Sequence[str]] = None,
        top_k: int = 5,
    ) -> list[dict]:
        """질의 → 관련 record 리스트.

        time_range: (start_iso, end_iso) — 절대 시간 범위.
        entities: 필터 entity 이름들 (any-match)
        sources: 필터 source 종류들. None이면 모든 source.
        """
        raise NotImplementedError

    def ingest_all(self) -> dict:
        """기존 기억(records/episodes/short_term)을 모두 인덱스에 적재 (멱등).

        Returns: {"inserted_or_updated": int, "by_source": {...}}
        """
        raise NotImplementedError


class SqliteFts5Retriever(MemoryRetriever):
    def __init__(self, db_path: str = RETRIEVAL_DB):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ── ingest ──────────────────────────────────────────────

    def _upsert_record(
        self, conn, *, source: str, source_id: str, ts: str,
        speaker: Optional[str], entities: list[str], text: str,
        importance: int = 5,
    ) -> bool:
        """새 record 또는 업데이트. Returns True if changed."""
        row = conn.execute(
            "SELECT text, entities, importance FROM records WHERE source=? AND source_id=?",
            (source, source_id),
        ).fetchone()
        ent_json = json.dumps(entities, ensure_ascii=False)
        if row is None:
            conn.execute(
                "INSERT INTO records(source, source_id, ts, speaker, entities, text, importance) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source, source_id, ts, speaker, ent_json, text, importance),
            )
            return True
        # 텍스트뿐 아니라 entities/importance 변경도 감지 — actor/about 재태깅이
        # FTS(records.entities 색인)에 전파돼야 검색이 정정된 주체로 매칭된다.
        if row["text"] != text or row["entities"] != ent_json or row["importance"] != importance:
            conn.execute(
                "UPDATE records SET ts=?, speaker=?, entities=?, text=?, importance=? "
                "WHERE source=? AND source_id=?",
                (ts, speaker, ent_json, text, importance, source, source_id),
            )
            return True
        return False

    def ingest_all(self) -> dict:
        stats = {"inserted_or_updated": 0, "by_source": {}}
        with self._conn() as conn:
            n_rec = self._ingest_records(conn)
            n_ep = self._ingest_episodes(conn)
            n_st = self._ingest_short_term(conn)
            stats["by_source"]["record"] = n_rec
            stats["by_source"]["episode"] = n_ep
            stats["by_source"]["short_term"] = n_st
            stats["inserted_or_updated"] = n_rec + n_ep + n_st
        return stats

    def _ingest_records(self, conn) -> int:
        """brain/records.py(records.json)에서 ingest."""
        try:
            from . import records as er
        except Exception:
            return 0
        n = 0
        for r in er.all_records(include_deleted=True):
            source_id = r.get("id", "")
            if not source_id:
                continue
            ts = r.get("updated_at") or r.get("created_at") or datetime.now().isoformat(timespec="seconds")
            text = (r.get("text") or "")[:500]
            entities = r.get("entities") or []
            importance = int(r.get("importance", 5))
            if self._upsert_record(
                conn, source="record", source_id=source_id, ts=ts,
                speaker=None, entities=entities, text=text,
                importance=importance,
            ):
                n += 1
            # tombstone(deleted_at) 동기화 — retrieval 단계에서 제외
            if r.get("deleted_at"):
                conn.execute(
                    "UPDATE records SET deleted_at=? WHERE source=? AND source_id=?",
                    (r["deleted_at"], "record", source_id),
                )
        return n

    def _ingest_episodes(self, conn) -> int:
        ep_dir = os.path.join(BRAIN_DIR, "episodes")
        if not os.path.isdir(ep_dir):
            return 0
        n = 0
        for path in sorted(glob.glob(os.path.join(ep_dir, "*.json"))):
            try:
                with open(path) as f:
                    ep = json.load(f)
            except Exception:
                continue
            d = os.path.basename(path).replace(".json", "")
            try:
                ts = datetime.fromisoformat(d + "T12:00:00").isoformat(timespec="seconds")
            except Exception:
                ts = d
            title = ep.get("title", "")
            events = ep.get("events", [])
            importance = ep.get("importance", 5)
            head_text = f"{title}\n" + "\n".join(
                f"{e.get('time','')}: {e.get('event','')}"
                for e in (events if isinstance(events, list) else [])
            )
            head_text = head_text[:1000]
            entities = []  # 추후 NER로 추출. 지금은 빈 list.
            if self._upsert_record(
                conn, source="episode", source_id=d, ts=ts,
                speaker=None, entities=entities, text=head_text,
                importance=importance,
            ):
                n += 1
        return n

    def _ingest_short_term(self, conn) -> int:
        path = os.path.join(BRAIN_DIR, "short_term.json")
        if not os.path.exists(path):
            return 0
        with open(path) as f:
            st = json.load(f)
        d = st.get("date", date.today().isoformat())
        items = st.get("items", []) if isinstance(st, dict) else []
        n = 0
        for idx, item in enumerate(items):
            t = item.get("time", "00:00")
            text = item.get("content", "")[:500]
            try:
                ts = datetime.fromisoformat(f"{d}T{t}:00").isoformat(timespec="seconds")
            except Exception:
                ts = d
            source_id = f"{d}#{idx}"
            if self._upsert_record(
                conn, source="short_term", source_id=source_id, ts=ts,
                speaker=None, entities=[], text=text,
                importance=item.get("importance", 5),
            ):
                n += 1
        return n

    # ── retrieve ──────────────────────────────────────────

    def retrieve(
        self,
        query: str,
        *,
        time_range: Optional[tuple[str, str]] = None,
        entities: Optional[Sequence[str]] = None,
        sources: Optional[Sequence[str]] = None,
        top_k: int = 5,
        auto_anchor: bool = True,
    ) -> list[dict]:
        if not query or not query.strip():
            return []
        # auto_anchor=True면 query에서 시간 표현 추출해 time_range 자동 설정
        if auto_anchor and time_range is None:
            anchor = parse_time_anchor(query)
            if anchor:
                time_range = anchor
        # FTS5 MATCH 쿼리 — 한국어/영어 단어만 추출 (특수문자·문장부호 제외).
        # FTS5는 reserved chars(?, *, :, ", ()) 에 syntax error 내므로 정규식으로 안전 추출.
        import re
        terms = [t for t in re.findall(r"[\w가-힣]+", query) if len(t) >= 1]
        if not terms:
            return []
        match_q = " OR ".join(terms)
        sql = (
            "SELECT r.id, r.source, r.source_id, r.ts, r.text, r.importance, "
            "       bm25(records_fts) AS score "
            "FROM records r JOIN records_fts ON records_fts.rowid = r.id "
            "WHERE records_fts MATCH ? AND r.deleted_at IS NULL "
        )
        params: list = [match_q]
        if time_range:
            sql += "  AND r.ts >= ? AND r.ts <= ? "
            params += [time_range[0], time_range[1]]
        if sources:
            placeholders = ",".join("?" for _ in sources)
            sql += f"  AND r.source IN ({placeholders}) "
            params += list(sources)
        sql += "ORDER BY score LIMIT ?"
        params.append(int(top_k))

        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()

        results = []
        for r in rows:
            results.append({
                "id": r["id"],
                "source": r["source"],
                "source_id": r["source_id"],
                "ts": r["ts"],
                "text": r["text"],
                "importance": r["importance"],
                "score": r["score"],
            })

        # entity 필터 (Python-side, 작은 결과셋이니 OK)
        if entities:
            ent_set = set(entities)
            with self._conn() as conn:
                full = conn.execute(
                    f"SELECT id, entities FROM records WHERE id IN ({','.join('?' for _ in results)})",
                    [r["id"] for r in results],
                ).fetchall() if results else []
            ent_map = {row["id"]: set(json.loads(row["entities"] or "[]")) for row in full}
            results = [r for r in results if ent_map.get(r["id"], set()) & ent_set]
        return results


def get_default_retriever() -> SqliteFts5Retriever:
    return SqliteFts5Retriever()


# ─── 자연어 시간 anchor 파서 ────────────────────────────

# 시간대 한정 (한국어 일상 표현 → 시간 범위)
_TIME_OF_DAY = {
    "아침": (5, 11),
    "오전": (5, 12),
    "점심": (11, 14),
    "낮": (11, 17),
    "오후": (12, 18),
    "저녁": (17, 21),
    "밤": (21, 26),  # 26 = 다음날 02:00
    "새벽": (3, 7),
}


def parse_time_anchor(query: str) -> Optional[tuple[str, str]]:
    """query에서 자연어 시간 표현 추출 → (start_iso, end_iso) 또는 None.

    예:
      "어제 점심 뭐 먹었지?" → (어제 11:00, 어제 14:00)
      "오늘 아침에 한 일"     → (오늘 05:00, 오늘 11:00)
      "방금"                 → (now-1h, now)
      "지난주"               → (지난주 월요일, 일요일+1)
      "3일 전"               → (3일 전 자정, 3일 전 자정+1d)

    한국어 시간 표현이 없으면 None — 호출자는 time_range 없이 retrieve.
    """
    if not query:
        return None
    import re

    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    q = query

    # "방금/좀 전/조금 전/지금" — 직전 1시간
    if any(kw in q for kw in ("방금", "좀 전", "조금 전", "방금 전")):
        return (
            (now - timedelta(hours=1)).isoformat(timespec="seconds"),
            now.isoformat(timespec="seconds"),
        )

    # 시간대 한정
    tod = None
    for kw, (sh, eh) in _TIME_OF_DAY.items():
        if kw in q:
            tod = (sh, eh)
            break

    # N일 전 / N시간 전 / N분 전
    m = re.search(r"(\d+)\s*시간\s*전", q)
    if m:
        h = int(m.group(1))
        return (
            (now - timedelta(hours=h + 1)).isoformat(timespec="seconds"),
            (now - timedelta(hours=max(0, h - 1))).isoformat(timespec="seconds"),
        )
    m = re.search(r"(\d+)\s*분\s*전", q)
    if m:
        mn = int(m.group(1))
        return (
            (now - timedelta(minutes=mn + 30)).isoformat(timespec="seconds"),
            (now - timedelta(minutes=max(0, mn - 30))).isoformat(timespec="seconds"),
        )
    m = re.search(r"(\d+)\s*일\s*전", q)
    if m:
        days = int(m.group(1))
        target = today_start - timedelta(days=days)
        return (target.isoformat(timespec="seconds"),
                (target + timedelta(days=1)).isoformat(timespec="seconds"))

    # 날짜 기준 (어제/그제/오늘/내일)
    days_ago = None
    if "그제" in q or "그저께" in q:
        days_ago = 2
    elif "그그제" in q:
        days_ago = 3
    elif "어제" in q:
        days_ago = 1
    elif "오늘" in q:
        days_ago = 0
    elif "지난주" in q:
        # 지난주 월요일 ~ 일요일 (now.weekday() 0=월)
        wd = now.weekday()
        last_mon = today_start - timedelta(days=wd + 7)
        last_sun_end = last_mon + timedelta(days=7)
        return (last_mon.isoformat(timespec="seconds"),
                last_sun_end.isoformat(timespec="seconds"))
    elif "이번 주" in q or "이번주" in q:
        wd = now.weekday()
        this_mon = today_start - timedelta(days=wd)
        return (this_mon.isoformat(timespec="seconds"),
                (today_start + timedelta(days=1)).isoformat(timespec="seconds"))
    elif "지난달" in q:
        # 지난달 1일 ~ 이번달 1일
        this_first = today_start.replace(day=1)
        last_last = this_first - timedelta(days=1)
        last_first = last_last.replace(day=1)
        return (last_first.isoformat(timespec="seconds"),
                this_first.isoformat(timespec="seconds"))

    if days_ago is None:
        return None

    # 단일 날짜 + 시간대(있으면)
    target = today_start - timedelta(days=days_ago)
    if tod:
        sh, eh = tod
        if eh > 24:
            start = target.replace(hour=sh)
            end = target + timedelta(days=1, hours=eh - 24)
        else:
            start = target.replace(hour=sh)
            end = target.replace(hour=eh) if eh < 24 else target + timedelta(days=1)
    else:
        start = target
        end = target + timedelta(days=1)
    return (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))


__all__ = [
    "MemoryRetriever",
    "SqliteFts5Retriever",
    "get_default_retriever",
    "parse_time_anchor",
    "RETRIEVAL_DB",
]
