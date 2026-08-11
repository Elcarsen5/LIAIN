"""2단 생성 파이프라인 (THINK → SPEAK) — 누출·잘림 근본 차단.

배경(reference persona Lian, 2026-08-11): 한 프롬프트로 사고+메시지를 함께 내는 단일 CoT
(`<생각>` 인라인)는 한 출력에 두 목적(전략+말)이 섞여 **누출·잘림이 구조적으로** 남는다.
부정 지시("전략 서술 마")로도, 발송 필터로도 근본 차단이 안 됐다. 생성을 둘로 쪼개
각 단계를 단일목적화하면 — THINK는 전략만, SPEAK는 이미 나온 판단을 '참고'만 하고 메시지만 —
누출~0·잘림 급감.

THINK 결과(생각)는 에이전트가 **자기 자신에게 발송**해 보존한다(제작자 점검용). 자기발송이라
is_from_me=True → 폴러가 응답 안 함(루프 없음). 목적지 self_handle 미설정이면 graceful no-op.
"""
from __future__ import annotations

from liain import llm


def _send_thought_to_self(thought: str, room_label: str, trigger_text: str) -> None:
    """생각(THINK 결과)을 에이전트 자기 계정으로 발송 — 제작자가 나중에 점검하도록 보존.

    코드가 헤더 부착: 어느 방(room_label)의, 상대의 어떤 말(trigger_text)에 대한 생각인지.
    실패해도 대화 흐름엔 영향 없음(순수 로그).
    """
    if not (thought or "").strip():
        return
    trig = " ".join((trigger_text or "").split())   # 개행·중복공백 정리
    if len(trig) > 60:
        trig = trig[:60] + "…"
    body = f'💭 [{room_label}] "{trig}"에 대한 생각\n\n{thought.strip()}'
    try:
        from liain import channels
        if channels.send_to_self(body):
            print(f"[liain.generate] 생각→self [{room_label}] {thought.strip()[:50]}", flush=True)
    except Exception as e:
        print(f"[liain.generate] 생각 self발송 오류: {e}", flush=True)


def think_then_speak(system_base: str, user_text: str, history_text: str, who: str,
                     room_label: str = "", speak_extra: str = "", role: str = "chat"):
    """2단 생성 — THINK(전략, private) → self발송(점검 보존) → SPEAK(보낼 말만).

    system_base : 페르소나 시스템 프롬프트(기억 포함). THINK·SPEAK 공통.
    speak_extra : SPEAK **시스템**에만 붙는 응답 지침(user 프롬프트 echo 방지).
    room_label  : 있으면 생각을 자기발송(보존). 빈 문자열이면 자기발송 생략(재생성 등).
    Returns (speak, thought). thought는 호출부가 자기기억(record_self_monologue)으로도 남긴다.
    """
    hist = f"## 최근 대화\n{history_text}\n\n" if history_text else ""
    # ── Stage 1: THINK (속마음/전략, 짧게, private) ──
    tp = (f"{hist}{who}가 방금: {user_text}\n\n"
          "이 상황을 어떻게 읽어? 지금 무슨 마음이고, 어떤 톤·방향으로 답하면 좋을지 "
          "속으로만 2~3문장으로 짧게 정리해. 실제 보낼 말은 쓰지 마 — 판단만. 없는 사실은 지어내지 마.")
    thought = (llm.complete(role, system_base, tp) or "").strip()
    if thought and room_label:
        _send_thought_to_self(thought, room_label, user_text)
    # ── Stage 2: SPEAK (보낼 메시지만) — 지침은 시스템에(user echo 방지) ──
    speak_system = system_base + (("\n\n" + speak_extra) if speak_extra else "")
    ref = f"(네가 방금 속으로 판단한 것 — 참고만: {thought})\n\n" if thought else ""
    sp = (f"{hist}{who}가 방금: {user_text}\n\n{ref}"
          f"이 판단을 반영해서 {who}에게 **실제로 보낼 말만** 자연스럽게 써. 판단 문장을 그대로 "
          "옮기지 말고 대화로. 문장은 중간에 끊지 말고 끝까지 완결해서.")
    speak = (llm.complete(role, speak_system, sp) or "").strip()
    return speak, thought


__all__ = ["think_then_speak"]
