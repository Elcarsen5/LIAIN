"""Liain CLI.

    liain info          # 현재 config·프로필·채널 요약
    liain run           # 페르소나 봇 기동 (수신 → 기억 컨텍스트 → 응답 → 기억 축적)
    liain diary         # 오늘 기억으로 일기 생성 (diary/YYYY-MM-DD.md)
    liain diary --send  # 일기 생성 + 채널 발송
    liain consolidate   # 단기→중기→장기 승격 + 패턴 인식 (하루 1회 권장)
    liain memory        # 쌓인 기억 요약 보기
"""
from __future__ import annotations
import sys

from liain import config, persona, llm, secrets
from liain import channels


def _brain():
    """Brain 인스턴스 (기억 계층). 실패 시 None — 기억 없이도 대화는 동작."""
    try:
        from liain.brain import Brain
        from liain.brain.paths import ensure_dirs
        ensure_dirs()
        return Brain()
    except Exception as e:
        print(f"[liain] 기억 계층 비활성: {e}", flush=True)
        return None


def _on_message(msg):
    """수신 → 기억 컨텍스트 주입 → 응답 → 기억 축적."""
    b = _brain()
    mem_ctx = ""
    if b:
        try:
            mem_ctx = b.get_unified_context() or ""
        except Exception as e:
            print(f"[liain] 기억 컨텍스트 실패: {e}", flush=True)

    extra = "\n\n## 이번 응답 형식\n자연스럽게 1~2문장으로 답해."
    if mem_ctx:
        extra = f"\n\n## 기억\n{mem_ctx}" + extra
    sysp = persona.system_prompt(extra)

    reply = llm.complete("chat", sysp, msg.text)
    if not reply:
        return
    role = msg.role if msg.role != "unknown" else "dad"
    channels.route(role, reply)

    # 대화 → 기억 추출·축적
    if b:
        try:
            b.process_conversation(
                [{"role": "user", "content": msg.text},
                 {"role": "assistant", "content": reply}])
        except Exception as e:
            print(f"[liain] 기억 축적 실패: {e}", flush=True)


def cmd_info():
    p = config.persona()
    print(f"페르소나: {p.get('name','(미설정)')} (제작자: {p.get('creator','?')})")
    print(f"사람: {list(config.people().keys())}")
    print(f"LLM: {llm.info()}")
    print(f"채널: {[c.name for c in channels.available_channels()]}")
    b = _brain()
    if b:
        try:
            from liain.brain.paths import BRAIN_DIR
            n = len(b.get_all_memories() or [])
            print(f"기억: {n}건 ({BRAIN_DIR})")
        except Exception:
            print("기억: (조회 실패)")


def cmd_run():
    print("[liain] 기동")
    cmd_info()
    chs = channels.available_channels()
    if not chs:
        print("[liain] 사용 가능한 채널 없음 — .env에 TELEGRAM_BOT_TOKEN 설정 필요")
        return
    for ch in chs:
        if ch.name == "telegram":
            ch.poll(_on_message)
            return
    print("[liain] 폴링 지원 채널 없음")


def cmd_diary(argv):
    from liain.brain import diary
    path = diary.write(send=("--send" in argv))
    if path:
        print(diary.read() or "")


def cmd_consolidate():
    b = _brain()
    if not b:
        print("[liain] 기억 계층 없음"); return
    try:
        r = b.consolidate_daily()
        print(f"[liain] consolidation 완료: {r}")
    except Exception as e:
        print(f"[liain] consolidation 실패: {e}")


def cmd_memory():
    b = _brain()
    if not b:
        print("[liain] 기억 계층 없음"); return
    try:
        print(b.get_unified_context() or "(아직 기억 없음)")
    except Exception as e:
        print(f"[liain] 조회 실패: {e}")


def main():
    argv = sys.argv[1:]
    cmd = argv[0] if argv else "info"
    secrets.load()
    if cmd == "run":
        cmd_run()
    elif cmd == "diary":
        cmd_diary(argv)
    elif cmd == "consolidate":
        cmd_consolidate()
    elif cmd == "memory":
        cmd_memory()
    else:
        cmd_info()


if __name__ == "__main__":
    main()
