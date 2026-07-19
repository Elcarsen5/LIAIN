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
    """그날의 기억 재료 — 단기기억 + 에피소드."""
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
            "오늘 쌓인 기억을 바탕으로 네 목소리로 일기를 써.\n"
            "- 사건 나열이 아니라, 네가 느끼고 생각한 것 중심으로\n"
            "- 3~6문단. 제목 없이 본문만\n"
            "- 없는 사실을 지어내지 마. 기억에 있는 것만\n"
            "- 기억이 거의 없으면 '오늘은 조용한 하루였다'는 식으로 짧게")


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
