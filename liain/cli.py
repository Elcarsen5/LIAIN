"""Liain CLI.

    liain info          # 현재 config·프로필·채널 요약
    liain run           # 페르소나 봇 기동 (수신 → 기억 컨텍스트 → 응답 → 기억 축적)
    liain diary         # 오늘 기억으로 일기 생성 (diary/YYYY-MM-DD.md)
    liain diary --send  # 일기 생성 + 채널 발송
    liain consolidate   # 단기→중기→장기 승격 + 패턴 인식 + 자기기분 성찰 (하루 1회 권장)
    liain memory        # 쌓인 기억 요약 보기

환경변수:
    LIAIN_COALESCE=0    # 메시지 코얼레싱 끄기(기본 ON). 켜져 있으면 버스트를 합쳐 1회 응답
                        #  — 완결 메시지는 ~2.5s, 진행중이면 더 대기(30s 상한).
"""
from __future__ import annotations
import sys

from liain import config, persona, llm, secrets
from liain import channels

_COALESCER = None      # cmd_run이 설정 — _on_message supersession 참조용


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


def _room_label(msg):
    """생각 self발송 헤더용 방 라벨 — 어느 대화방의 생각인지 제작자가 알아보게."""
    if getattr(msg, "is_group", False):
        return "그룹"
    name = msg.entity or msg.role or "상대"
    return f"{name} 1:1"


def _on_message(msg):
    """수신 → 기억 컨텍스트 주입 → 2단 생성(THINK→SPEAK) → 응답 → 기억 축적."""
    b = _brain()
    mem_ctx = ""
    if b:
        try:
            mem_ctx = b.get_unified_context() or ""
        except Exception as e:
            print(f"[liain] 기억 컨텍스트 실패: {e}", flush=True)

    # 기억 컨텍스트만 시스템에 — 2단 파이프라인은 <생각> CoT 형식(OUTPUT_FORMAT_NOTE)이 불필요.
    mem_extra = ""
    if mem_ctx:
        # 기억 시점 해석 규칙 — [시점]·(절대날짜)를 지금으로 오해하지 않게
        try:
            from liain.brain.temporal import ANCHOR_NOTE
        except Exception:
            ANCHOR_NOTE = ""
        mem_extra = f"\n\n## 기억\n{mem_ctx}{ANCHOR_NOTE}"
    sysp = persona.system_prompt(mem_extra)

    # 2단 생성 — THINK(전략·private)→생각 self발송(보존)→SPEAK(보낼 말만). 누출·잘림 원천 차단.
    from liain import generate, guard
    who = msg.entity or msg.sender or "상대"
    reply, thought = generate.think_then_speak(
        sysp, msg.text, "", who, room_label=_room_label(msg))
    if not reply:
        return
    # supersession(부작용 전 체크): 생성 중 같은 대화에 새 메시지가 왔으면 이 응답을 취소하고
    # 원문을 되돌려 다음 flush가 누적분을 합쳐 재검토(문맥 보존). 여기서 먼저 걸러야 취소될
    # 응답의 생각을 기억에 이중기록하지 않는다. 코얼레싱 미사용 시 _COALESCER=None → 통과.
    if _COALESCER is not None:
        _cid = msg.chat_id or msg.sender or msg.entity or "default"
        if _COALESCER.is_superseded(_cid):
            print("[coalesce] 발송 취소 — 새 메시지 수신, 재검토 예정", flush=True)
            _COALESCER.resubmit(msg)
            return
    # 생각을 자기기억으로도 남김(self발송은 generate가 이미 함 — 이건 기억·일기용).
    # 잔여 <생각> 최종 방어는 transport sanitize_outgoing(각 채널 send)이 담당.
    if thought:
        guard.record_self_monologue([thought], brain=b, who=(msg.entity or ""))
    role = msg.role if msg.role != "unknown" else "dad"
    channels.route(role, reply)

    # 대화 → 기억 축적. 두 경로 모두 태워야 함:
    #  ① process_conversation: emotional + episode
    #  ② memory.process_message: entity records(사실 추출) — get_unified_context가
    #     읽는 곳. 이게 빠지면 대화해도 '아는 사실'이 안 쌓임(핵심 기능).
    if b:
        try:
            b.process_conversation(
                [{"role": "user", "content": msg.text},
                 {"role": "assistant", "content": reply}])
        except Exception as e:
            print(f"[liain] 감정·에피소드 축적 실패: {e}", flush=True)
        try:
            from liain.brain import memory
            from datetime import datetime
            memory.process_message(
                msg.text, channel=(msg.channel or "chat"),
                ts=datetime.now().isoformat(timespec="seconds"),
                speaker=(msg.entity or None),
                entities_hint=([msg.entity] if msg.entity else None))
        except Exception as e:
            print(f"[liain] 사실 추출 실패: {e}", flush=True)


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
            from liain.brain import records
            n = len(records.all_records())   # 실제 기억(record) 수 — 5계층 dict 키가 아님
            print(f"기억: {n}건 ({BRAIN_DIR})")
        except Exception:
            print("기억: (조회 실패)")


def cmd_run():
    print("[liain] 기동")
    cmd_info()
    chs = channels.available_channels()
    if not chs:
        print("[liain] 사용 가능한 채널 없음 — Telegram 토큰(.env) 또는 iMessage(macOS) 필요")
        return
    # 코얼레싱 — 버스트를 합쳐 1회 응답(각자 비슷한 답 도배 방지). LIAIN_COALESCE=0으로 끔.
    import os as _os
    global _COALESCER
    handler = _on_message
    if _os.environ.get("LIAIN_COALESCE", "1").lower() not in ("0", "false", "no"):
        from liain.coalesce import Coalescer
        _COALESCER = Coalescer(_on_message)
        handler = _COALESCER.submit
        print("[liain] 메시지 코얼레싱 ON (끄려면 LIAIN_COALESCE=0)", flush=True)
    # poll을 지원하는 첫 채널로 수신 루프 (iMessage/Telegram 모두 지원).
    for ch in chs:
        try:
            print(f"[liain] {ch.name} 수신 폴링 시작", flush=True)
            ch.poll(handler)
            return
        except NotImplementedError:
            continue
        except Exception as e:
            print(f"[liain] {ch.name} 폴링 실패: {e}", flush=True)
            continue
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
    # 자기성찰 — 오늘 기분 갱신(어제→오늘 이월). 조회 시 감쇠로 회복. 실패해도 무해.
    try:
        m = b.reflect_self_mood()
        if m:
            print(f"[liain] self-mood: {m.get('label')} (valence {m.get('valence')})")
    except Exception as e:
        print(f"[liain] self-mood 성찰 실패: {e}")


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
