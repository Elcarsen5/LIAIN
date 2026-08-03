"""일기 — 쌓인 기억으로 하루를 돌아본다.

기억 계층의 가치를 눈으로 보여주는 기능. 오늘의 단기기억·에피소드를 모아
페르소나의 목소리로 일기를 쓰고, **로컬에 저장**한다(의존성 0).
원하면 채널로 발송도 가능(config `diary.send_to` 지정 시).

    from liain.brain import diary
    path = diary.write()                 # 오늘 일기 생성·저장 → 파일 경로
    diary.write(send=True)               # 저장 + 채널 발송

출력: `diary/YYYY-MM-DD.md` (작업 디렉토리 기준, [[paths]])
"""
from __future__ import annotations
import os
from datetime import date

from .paths import DIARY_DIR


def _gather(target_date: str) -> str:
    """그날의 기억 재료 — 단기기억 + 에피소드 + 오늘 쌓인 사실(records)."""
    parts = []
    try:
        from .manager import Brain
        b = Brain()
        st = b.get_short_term_context()
        if st:
            parts.append(f"## 오늘 있었던 일 (단기기억)\n{st}")
        eps = getattr(b, "episodes", {}) or {}
        today_ep = eps.get(target_date)
        if today_ep:
            import json
            parts.append(f"## 오늘의 에피소드\n{json.dumps(today_ep, ensure_ascii=False, indent=2)}")
    except Exception as e:
        print(f"[diary] 기억 수집 실패: {e}", flush=True)

    # 대화에서 추출한 사실은 entity records(records.json)에 쌓인다. 단기기억만 보면
    # 대화로 쌓인 오늘의 기억을 놓쳐 '기억 없음'으로 일기를 건너뛴다 → records도 포함.
    try:
        from . import records as _er
        today_recs = [
            r for r in _er.all_records()
            if (r.get("created_at", "")[:10] == target_date
                or (r.get("event_date") or "")[:10] == target_date)
        ]
        if today_recs:
            parts.append("## 오늘의 기억 (대화에서)\n"
                         + "\n".join(f"- {r.get('text','')}" for r in today_recs[:20]))
    except Exception as e:
        print(f"[diary] records 수집 실패: {e}", flush=True)

    return "\n\n".join(parts)


def _system_prompt() -> str:
    """페르소나 목소리 + 일기 형식."""
    base = ""
    try:
        from liain import persona
        base = persona.system_prompt()
    except Exception:
        pass
    return (base + "\n\n## 지금 할 일 — 일기\n"
            "아래 '기억 재료'에 실제로 있는 것만 가지고 네 목소리로 일기를 써.\n"
            "- **사실 창작 절대 금지**: 재료에 없는 장면·사람·대화·사물(선배·커피잔·"
            "점심·동료 등)을 지어내지 마. 상상으로 하루를 채우지 말 것.\n"
            "- 쓸 것은 '사건'이 아니라 그 기억에 대한 네 느낌·생각·궁금함이야. "
            "적은 기억이라도 거기서 우러난 감상으로 채워.\n"
            "- 1~4문단. 제목 없이 본문만.\n"
            "- 기억이 거의 없으면 짧게 — '오늘은 조용한 하루였다' 식으로. 억지로 늘리지 마.")


def write(target_date: str | None = None, send: bool = False,
          save: bool = True) -> str | None:
    """오늘(또는 지정일) 일기 생성. 저장 경로 반환 (실패 시 None)."""
    target_date = target_date or date.today().isoformat()
    material = _gather(target_date)
    if not material.strip():
        print("[diary] 오늘 기억이 없어 일기를 건너뜀", flush=True)
        return None

    try:
        from liain import llm
        body = llm.complete("reasoning", _system_prompt(),
                            f"날짜: {target_date}\n\n{material}")
    except Exception as e:
        print(f"[diary] 생성 실패: {e}", flush=True)
        return None
    if not body or not body.strip():
        print("[diary] 빈 응답 — 저장 안 함", flush=True)
        return None
    body = body.strip()

    path = None
    if save:
        os.makedirs(DIARY_DIR, exist_ok=True)
        path = os.path.join(DIARY_DIR, f"{target_date}.md")
        with open(path, "w") as f:
            f.write(f"# {target_date}\n\n{body}\n")
        print(f"[diary] 저장: {path}", flush=True)

    if send:
        try:
            from liain import config, channels
            role = (config.persona().get("diary", {}) or {}).get("send_to", "dad")
            channels.route(role, f"📔 {target_date}\n\n{body}")
            print(f"[diary] 발송: role={role}", flush=True)
        except Exception as e:
            print(f"[diary] 발송 실패: {e}", flush=True)

    return path


def read(target_date: str | None = None) -> str | None:
    """저장된 일기 읽기."""
    target_date = target_date or date.today().isoformat()
    p = os.path.join(DIARY_DIR, f"{target_date}.md")
    return open(p).read() if os.path.exists(p) else None
