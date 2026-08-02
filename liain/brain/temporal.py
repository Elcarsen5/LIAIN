"""시간 인지 — 상대 표현을 절대 날짜로 못 박고, 경과는 코드가 계산한다.

근본 문제 (LLM에 날짜를 맡기면 벌어지는 일):
    저장할 때 "내일 병원"을 그대로 박고, 회상할 때 모델에게 "3일 전 + 내일 =
    언제?"를 계산시킨다. LLM은 날짜 산수를 못 해서 지난 일을 오늘로, 2일째를
    1일째로, 3일째를 2주차로 착각한다.

해법:
    - 캡처 시점(anchor)에 상대 표현을 **절대 날짜로 못 박는다** (resolve).
    - 회상 시점에 경과/잔여를 **코드가 계산해서 문장으로 준다** (recall_phrase).
    모델은 산수를 하지 않는다.

이 모듈은 LLM을 쓰지 않는다 — 전부 규칙 기반이라 100% 재현 가능. 정밀도
우선: 애매한 표현("그냥 화요일", "다음 달")은 손대지 않고 확실한 것만
해석한다. 틀린 절대날짜를 박느니 안 박는 게 낫다.

지금은 한국어 표현을 다룬다. 다른 언어는 _DAY_OFFSETS/_REL_WEEK/_WD 테이블을
바꿔 확장할 수 있다.
"""
from __future__ import annotations

import re
from datetime import datetime, date, timedelta
from typing import Optional, Tuple

_WD = ["월", "화", "수", "목", "금", "토", "일"]      # 0=월 … 6=일


# 기억 항목의 시점 해석 규칙 — 시스템 프롬프트에 붙여 LLM이 [시점]·(절대날짜)를
# 지금으로 오해하지 않게 한다.
ANCHOR_NOTE = (
    "\n\n## 기억 시점 해석 (반드시)\n"
    "기억 항목 앞 [시점]과 문장 속 (YYYY-MM-DD 요일) 주석이 그 일이 실제로 언제인지야. "
    "문장에 '오늘/내일/어제' 같은 말이 있어도 그건 기록 당시 기준이지 지금이 아니야 — "
    "괄호 안 절대날짜와 [시점] 표기를 따라. "
    "'지난 일'로 표시된 건 이미 지나간 일이라 지금 일어나는 것처럼 말하지 마. "
    "'N일째/N주차'는 이미 계산된 값이니 다시 세지 말고 그대로 써."
)


def _wd_kr(d: date) -> str:
    return _WD[d.weekday()]


def _fmt(d: date) -> str:
    """절대날짜 주석 형식: 2026-07-22(화)."""
    return f"{d.isoformat()}({_wd_kr(d)})"


# ── 상대 → 절대 (캡처 시) ────────────────────────────────

_DAY_OFFSETS = {
    "그저께": -2, "그제": -2, "엊그제": -2,
    "어제": -1,
    "오늘": 0,
    "내일모레": 2, "낼모레": 2, "모레": 2, "내일": 1, "낼": 1,
    "글피": 3,
}

_REL_WEEK = {
    "지난주": -7, "저번주": -7, "전주": -7,
    "이번주": 0, "금주": 0,
    "다음주": 7, "담주": 7, "다음 주": 7, "차주": 7,
}


def _week_weekday(anchor: date, offset_days: int, target_wd: int) -> date:
    """anchor가 속한 주(월요일 시작) 기준 offset_days 만큼 이동한 주의 target 요일."""
    monday = anchor - timedelta(days=anchor.weekday())
    return monday + timedelta(days=offset_days + target_wd)


def _resolve_one(text: str, anchor: date) -> list[Tuple[int, int, date, str]]:
    """text에서 해석 가능한 상대표현 위치들. [(start, end, 절대date, 원표현), ...]."""
    hits: list[Tuple[int, int, date, str]] = []
    taken = [False] * (len(text) + 1)

    def _claim(s: int, e: int) -> bool:
        if any(taken[s:e]):
            return False
        for i in range(s, e):
            taken[i] = True
        return True

    # 1) 이번주/다음주/지난주 + 요일 (가장 구체적 → 먼저)
    for wk, wk_off in sorted(_REL_WEEK.items(), key=lambda x: -len(x[0])):
        for wd_i, wd in enumerate(_WD):
            pat = re.compile(re.escape(wk) + r"\s*" + wd + r"(?:요일)?")
            for m in pat.finditer(text):
                if _claim(m.start(), m.end()):
                    hits.append((m.start(), m.end(),
                                 _week_weekday(anchor, wk_off, wd_i), m.group()))

    # 2) N일 후/뒤/있다가 · N일 전
    for m in re.finditer(r"(\d{1,3})\s*일\s*(후|뒤|있다가|만에|지나서)", text):
        if _claim(m.start(), m.end()):
            hits.append((m.start(), m.end(),
                         anchor + timedelta(days=int(m.group(1))), m.group()))
    for m in re.finditer(r"(\d{1,3})\s*일\s*전", text):
        if _claim(m.start(), m.end()):
            hits.append((m.start(), m.end(),
                         anchor - timedelta(days=int(m.group(1))), m.group()))

    # 3) 이번 주말 → 그 주 토요일
    for m in re.finditer(r"이번\s*주말|이번주말", text):
        if _claim(m.start(), m.end()):
            hits.append((m.start(), m.end(),
                         _week_weekday(anchor, 0, 5), m.group()))

    # 4) 단순 오프셋 단어 (오늘/내일/어제…). 긴 단어 먼저.
    for word in sorted(_DAY_OFFSETS, key=len, reverse=True):
        for m in re.finditer(re.escape(word), text):
            if _claim(m.start(), m.end()):
                hits.append((m.start(), m.end(),
                             anchor + timedelta(days=_DAY_OFFSETS[word]), m.group()))

    hits.sort(key=lambda h: h[0])
    return hits


def resolve(text: str, anchor: Optional[datetime] = None
            ) -> Tuple[str, Optional[str]]:
    """상대 표현을 절대날짜 주석으로 못 박는다.

        resolve("내일 병원 가야 돼", anchor=<월>)
        → ("내일(2026-07-22(화)) 병원 가야 돼", "2026-07-22")

    - 원 단어는 남기고 뒤에 (YYYY-MM-DD 요일) 주석을 붙인다.
    - event_date: 해석된 날짜 중 가장 미래 것 하나. 과거만 있으면 가장 최근.
    - 이미 (YYYY-MM-DD…) 주석이 있는 텍스트는 재처리 안 함(멱등).
    """
    if not text:
        return text, None
    anchor = anchor or datetime.now()
    anchor_d = anchor.date() if isinstance(anchor, datetime) else anchor
    if re.search(r"\(\d{4}-\d{2}-\d{2}", text):
        return text, None

    hits = _resolve_one(text, anchor_d)
    if not hits:
        return text, None

    out = text
    for s, e, d, _orig in sorted(hits, key=lambda h: -h[0]):
        out = out[:e] + f"({_fmt(d)})" + out[e:]

    dates = [d for _, _, d, _ in hits]
    future = sorted([d for d in dates if d >= anchor_d])
    event_date = (future[-1] if future else max(dates)).isoformat()
    return out, event_date


# ── 진행형 이벤트 감지 ───────────────────────────────────

# "시작점부터 지속되는 것" — 회상 시 'N일째/N주차'로 세야 하는 부류.
_ONGOING_HINTS = (
    "출장", "여행", "휴가", "입원", "연수", "교육", "다이어트", "부터",
    "시작", "째", "동안", "연차", "출근 첫", "첫 출근", "첫날", "합숙",
    "프로젝트", "챌린지", "금연", "금주", "복용", "재활", "입대", "훈련",
)


def is_ongoing(text: str) -> bool:
    """이 이벤트가 '진행 중(N일째로 세는)' 성격인가 — 키워드 휴리스틱."""
    t = text or ""
    return any(h in t for h in _ONGOING_HINTS)


# ── 절대날짜 → 회상 문장 (회상 시) ──────────────────────

def recall_phrase(event_date_iso: str, now: Optional[datetime] = None,
                  ongoing: bool = False) -> str:
    """event_date를 '지금 기준' 문장으로. 모델이 계산 안 하게 코드가 만든다.

        미래:  "내일(D-1)", "모레(D-2)", "5일 뒤(2026-07-28(월))"
        오늘:  "오늘(D-day)"
        과거(일회성): "지난 일 — 2일 전(2026-07-21(화))"
        과거(진행형): "3일째 (시작 2026-07-21(화), 1주차)"   ← 포함 계산
    """
    now = now or datetime.now()
    try:
        ev = date.fromisoformat(event_date_iso[:10])
    except Exception:
        return event_date_iso
    d = (now.date() - ev).days   # >0 과거, <0 미래

    if d < 0:
        n = -d
        if n == 1:
            return "내일(D-1)"
        if n == 2:
            return "모레(D-2)"
        if n == 3:
            return f"글피(D-3, {_fmt(ev)})"
        return f"{n}일 뒤(D-{n}, {_fmt(ev)})"
    if d == 0:
        return "오늘(D-day)"
    if ongoing:
        return f"{d + 1}일째 (시작 {_fmt(ev)}, {d // 7 + 1}주차)"
    return f"지난 일 — {d}일 전({_fmt(ev)})"


def is_stale_oneoff(event_date_iso: str, now: Optional[datetime] = None,
                    grace_days: int = 0) -> bool:
    """지나간 일회성 예정일인가 (always-on 컨텍스트에서 뺄 후보).

    진행형 판단은 caller가 is_ongoing으로 별도 처리.
    """
    now = now or datetime.now()
    try:
        ev = date.fromisoformat(event_date_iso[:10])
    except Exception:
        return False
    return (now.date() - ev).days > grace_days


# ── ISO timestamp → 자연어 (기억 회상 프리픽스) ─────────

def _time_phase(hour: int) -> str:
    if 0 <= hour < 5:
        return "새벽"
    if 5 <= hour < 8:
        return "이른 아침"
    if 8 <= hour < 11:
        return "아침"
    if 11 <= hour < 14:
        return "점심"
    if 14 <= hour < 18:
        return "오후"
    if 18 <= hour < 22:
        return "저녁"
    return "밤"


def humanize_timestamp(ts_iso: str, now: Optional[datetime] = None) -> str:
    """ISO timestamp → '오늘 09:30' / '어제 22:13' / '3일 전 (금) 오후 15:30'.

    event_date가 없는 record의 회상 프리픽스. (event_date 있으면 recall_phrase)
    """
    now = now or datetime.now()
    try:
        ts = datetime.fromisoformat(ts_iso)
    except Exception:
        return ts_iso
    sec = (now - ts).total_seconds()
    days_ago = (now.date() - ts.date()).days
    hh_mm = ts.strftime("%H:%M")
    weekday = _WD[ts.weekday()]
    phase = _time_phase(ts.hour)

    if sec < 60:
        return "방금"
    if sec < 3600:
        return f"{int(sec // 60)}분 전"
    if days_ago == 0:
        return f"오늘 {phase} {hh_mm}"
    if days_ago == 1:
        return f"어제 {phase} {hh_mm}"
    if days_ago == 2:
        return f"그제 {phase} {hh_mm}"
    if days_ago < 7:
        return f"{days_ago}일 전 ({weekday}) {phase} {hh_mm}"
    if days_ago < 14:
        return f"지난주 {weekday}요일 {phase} {hh_mm}"
    if days_ago < 30:
        return f"{days_ago // 7}주 전 ({weekday}) {phase} {hh_mm}"
    if days_ago < 365:
        return f"{days_ago // 30}개월 전 ({ts.strftime('%m월 %d일')}) {phase}"
    return f"{days_ago // 365}년 전 ({ts.strftime('%Y년 %m월')})"


# ── 자체 테스트 ─────────────────────────────────────────

if __name__ == "__main__":
    # anchor: 2026-07-20 (월). 이름은 중립 예시.
    A = datetime(2026, 7, 20, 14, 0)
    cases = [
        ("내일 병원 가야 돼", "2026-07-21"),
        ("어제 친구랑 통화함", "2026-07-19"),
        ("오늘 점심 뭐 먹지", "2026-07-20"),
        ("모레 시험이야", "2026-07-22"),
        ("다음주 수요일 회의", "2026-07-29"),
        ("이번주 금요일에 만나", "2026-07-24"),
        ("지난주 화요일에 갔었어", "2026-07-14"),
        ("3일 후에 출장", "2026-07-23"),
        ("이번 주말에 캠핑", "2026-07-25"),
        ("그냥 밥 먹었어", None),
    ]
    print("=== resolve (anchor=2026-07-20 월) ===")
    ok = 0
    for txt, expect in cases:
        out, ed = resolve(txt, anchor=A)
        mark = "✓" if ed == expect else "✗"
        ok += (ed == expect)
        print(f"  {mark} {txt!r}\n      → {out!r}  event_date={ed}")
    print(f"  ── {ok}/{len(cases)} 통과")

    print("\n=== recall_phrase (now=2026-07-23 목) ===")
    N = datetime(2026, 7, 23, 9, 0)
    for ev, ong in [("2026-07-24", False), ("2026-07-23", False),
                    ("2026-07-22", False), ("2026-07-21", True),
                    ("2026-07-28", False)]:
        print(f"  {ev} ongoing={ong} → {recall_phrase(ev, now=N, ongoing=ong)}")
