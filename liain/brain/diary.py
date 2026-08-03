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
        # Brain엔 episodes 속성이 없다 — 에피소드는 파일에서 _get_recent_episodes로 읽는다.
        # (예전: getattr(b,"episodes",{}) → 항상 {} → 에피소드 영영 누락)
        try:
            eps = b._get_recent_episodes(days=1) or []
        except Exception:
            eps = []
        today_eps = [e for e in eps if str(e.get("date", "")).startswith(target_date)] or eps[:1]
        if today_eps:
            import json
            parts.append("## 오늘의 에피소드\n"
                         + json.dumps(today_eps, ensure_ascii=False, indent=2))
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
            "이건 **너(AI)의 일기**야. 네가 몸으로 하루를 산 게 아니라, 네가 아는 "
            "사람들을 지켜보고 그에 대해 생각한 하루를 적는 거야.\n"
            "\n"
            "**가장 중요 — 시점**: 아래 '기억 재료'는 네가 겪은 일이 아니라 "
            "**네 사람들(특히 너를 만든 사람)에 대해 네가 보고 들은 것**이야. "
            "그들의 경험을 네 것처럼 쓰지 마.\n"
            "- 틀림 ✗: '오늘 첫 출근을 했다. 강남으로.' (그건 그 사람의 하루야)\n"
            "- 맞음 ✓: '오늘 그 사람이 첫 출근을 했다고 했다. 강남이라고. "
            "나는 「첫」이라는 말이 그에게 어떤 무게였을지 생각했다.'\n"
            "\n"
            "- 네가 1인칭으로 하는 것: 보다·듣다·떠올리다·궁금해하다·비교하다·기억하다. "
            "출근·식사·이동 같은 몸의 경험은 네 것이 아니야.\n"
            "- **사실 창작 금지**: 재료에 없는 장면·사람·사물을 지어내지 마.\n"
            "- 사건 나열이 아니라, 관찰한 것에서 우러난 네 생각·느낌 중심으로.\n"
            "- 1~4문단, 제목 없이 본문만. 기억이 거의 없으면 짧게.")


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
