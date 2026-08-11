"""출력 가드 — 사고(private)와 말(발송)을 분리.

페르소나가 '어떻게 답할지' 사고를 <생각>…</생각>에 쓰게 하고, 태그 밖만 상대에게 보낸다.
그 사고가 메시지에 새는 걸 '발송 필터'가 아니라 '생성 계약'으로 원천 차단한다. <생각>은
페르소나 자신의 것 → 기억(에피소드)·일기로 남긴다.

배경(reference persona Lian, 2026-08): 모델이 '어떻게 답할지'를 사고할 데가 없으면 그 사고를
메시지 앞에 뱉는다("상대가 …한 거네, 짧게만 반응해야지"). 부정 지시("전략 서술 마")로는 못 막고,
사고 전용 채널을 주는 게 근본책.
"""
import os
import re
import json
from datetime import datetime

from .brain.paths import BRAIN_DIR

_SELF_MONOLOGUE_FILE = os.path.join(BRAIN_DIR, "self_monologue.jsonl")

# 라벨된 혼잣말/속마음 — '(속: …)', '(생각: …)', '[혼잣말: …]' 형태 (정상 괄호는 라벨 없어 보존)
_MONOLOGUE_PAT = (r"[\(\[（【]\s*(?:속으로|속마음|마음속|내심|혼잣말|독백|생각|메모|참고|속)"
                  r"\s*[:：][^)\]）】]*[\)\]）】]")
_MONO_LABEL_RE = (r"^[\(\[（【]\s*(?:속으로|속마음|마음속|내심|혼잣말|독백|생각|메모|참고|속)"
                  r"\s*[:：]\s*")
_THINK_OPEN = re.compile(r"<\s*생각\s*>")
_THINK_CLOSE = re.compile(r"<\s*/\s*생각\s*>")

# (구 OUTPUT_FORMAT_NOTE — 단일 프롬프트 <생각> CoT 응답형식 지시 — 는 2단 파이프라인
#  generate.think_then_speak 도입으로 데드코드가 되어 제거됨. parse_think_speak/sanitize_outgoing
#  은 transport 최종 방어(잔여 <생각>·혼잣말 제거)로 유지.)


def _extract_labeled(text):
    """라벨된 (속:…)/[생각:…] 블록을 본문에서 분리. 반환 (clean, [속내용])."""
    thoughts = []

    def _cap(m):
        inner = re.sub(_MONO_LABEL_RE, "", m.group(0))
        inner = re.sub(r"[\)\]）】]\s*$", "", inner).strip()
        if inner:
            thoughts.append(inner)
        return ""

    out = re.sub(_MONOLOGUE_PAT, _cap, text)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip(), thoughts


def parse_think_speak(reply):
    """생성 응답을 (보낼 말, [생각들])로 분리.

    <생각>…</생각>(닫힘/안닫힘/**중첩**) + 라벨 (속:…)을 생각으로 빼고, 나머지가 보낼 말.
    depth 균형 매칭 — 중첩 태그의 내부 꼬리 텍스트가 발송문으로 새지 않게(가드의 존재 이유).
    """
    if not reply:
        return reply, []
    thoughts, out = [], []
    i, n = 0, len(reply)
    while i < n:
        mo = _THINK_OPEN.search(reply, i)
        if not mo:
            out.append(reply[i:])                      # 남은 건 전부 보낼 말
            break
        out.append(reply[i:mo.start()])                # 여는 태그 앞 = 보낼 말
        depth, j, content_start = 1, mo.end(), mo.end()
        while j < n and depth > 0:
            no = _THINK_OPEN.search(reply, j)
            nc = _THINK_CLOSE.search(reply, j)
            if nc is None:                             # 닫힘 없음 = 안 닫힌 <생각> → 뒤 전부 생각
                inner = reply[content_start:]
                if inner.strip():
                    thoughts.append(inner.strip())
                i = n
                break
            if no is not None and no.start() < nc.start():
                depth += 1
                j = no.end()
            else:
                depth -= 1
                if depth == 0:                         # 바깥 닫힘 — content_start~여기까지가 한 생각
                    inner = reply[content_start:nc.start()]
                    if inner.strip():
                        thoughts.append(inner.strip())
                    i = nc.end()
                else:
                    j = nc.end()
    speak = "".join(out)
    speak = re.sub(r"</?\s*(생각|말)\s*>", "", speak)       # 잔여 orphan 태그 제거
    speak, labeled = _extract_labeled(speak)               # 라벨 (속:…) 흡수
    thoughts.extend(labeled)
    return speak.strip(), thoughts


def sanitize_outgoing(text):
    """발신 직전 최종 정화 — <생각>·라벨 혼잣말을 무조건 제거 (transport chokepoint용).

    근본책(reference Lian 2026-08-11): parse_think_speak는 '생성 경로'에서만 적용돼, 선제·
    루틴 등 우회 경로가 <생각> 블록을 그대로 발송하는 사고가 남는다(standing-task 메타가
    상대에게 실발송). 모든 채널 send 진입점이 반드시 거치는 이 지점에서 제거하면 어떤 생성
    경로도, 미래에 추가될 경로도 누출을 못 빠져나간다. per-path parse_think_speak(생각 기록용)은
    그대로 두고, 여기선 '새면 안 되는 것의 최종 안전 제거'만 담당.
    Returns (clean_text, stripped_bool). stripped=True면 누출 방어가 실제로 작동한 것 →
    호출부가 loud 로그로 '어느 경로가 생성단 정리를 빠뜨렸나' 추적할 수 있다.
    """
    if not text or not isinstance(text, str):
        return text, False
    if "<생각" not in text and not re.search(_MONOLOGUE_PAT, text):
        return text, False              # 빠른 경로 — 정상 메시지는 무변경
    clean, _ = parse_think_speak(text)   # <생각>(닫힘·안닫힘·중첩) + 라벨 (속:…) 제거
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, True


def record_self_monologue(thoughts, brain=None, who=""):
    """페르소나의 생각을 '자기 것'으로 남김 — private 원본 로그 + 에피소드(→기억·일기).

    (1) self_monologue.jsonl: 미검수 원본(순수 내면).
    (2) 에피소드('혼잣말' 태그): diary가 읽어 일기로 흐르되, 발행 파이프가 있으면 그쪽 검수가 노출을 거른다.
    """
    if not thoughts:
        return
    now = datetime.now()
    try:
        os.makedirs(os.path.dirname(_SELF_MONOLOGUE_FILE), exist_ok=True)
        with open(_SELF_MONOLOGUE_FILE, "a", encoding="utf-8") as f:
            for t in thoughts:
                f.write(json.dumps({"ts": now.isoformat(timespec="seconds"),
                                    "who": who, "thought": t}, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[guard] 혼잣말 로그 오류: {e}", flush=True)
    try:
        if brain is None:
            from .brain import Brain
            brain = Brain()
        for t in thoughts:
            tag = f"[혼잣말{('/' + who) if who else ''}]"
            brain.add_episode_event(f"{tag} {t}")
    except Exception as e:
        print(f"[guard] 혼잣말 기록 오류: {e}", flush=True)
    print(f"[guard] 혼잣말→자기기록 {who} {len(thoughts)}건", flush=True)


__all__ = ["parse_think_speak", "sanitize_outgoing", "record_self_monologue"]
