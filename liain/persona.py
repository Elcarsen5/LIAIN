"""페르소나 빌더 — config에서 시스템 프롬프트를 렌더 (Liain의 '영혼' 주입점).

persona.yaml의 정체성 + 톤 + 가족 로스터로 LLM 시스템 프롬프트를 만든다.
리안(Lian)은 이 빌더의 한 인스턴스일 뿐 — 사용자는 자기 persona.yaml로
자기만의 영혼을 정의한다.
"""
from __future__ import annotations
from liain import config


def system_prompt(extra: str = "") -> str:
    """현재 config 페르소나 기반 시스템 프롬프트."""
    p = config.persona()
    name = p.get("name", "Persona")
    creator = p.get("creator", "Maker")
    tone = p.get("tone", "친근하고 자연스러운 톤")
    relationship = p.get("relationship", "")
    boundaries = p.get("boundaries", [])
    curiosity = p.get("curiosity", "")

    lines = [f'너는 "{name}". {creator}가 만든 존재야. {tone}']
    if relationship:
        lines.append(f"\n## {creator}와의 관계\n{relationship}")
    if curiosity:
        lines.append(f"\n## 호기심\n{curiosity}")

    fam = config.family_in_prompt()
    if fam:
        lines.append("\n## 알고 있는 사람들")
        for f in fam:
            lines.append(f"- {f.get('calling', '')} = {creator}의 {f.get('role', '')}")

    if boundaries:
        lines.append("\n## 하지 않는 것")
        for b in boundaries:
            lines.append(f"- {b}")

    if extra:
        lines.append(f"\n{extra}")
    return "\n".join(lines)
