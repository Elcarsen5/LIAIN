"""Liain config 레이어 — 페르소나·연락처·LLM 프로필을 코드에서 분리.

config 디렉토리(기본 ./config, 환경변수 LIAIN_CONFIG_DIR로 변경)에서 로드:
  persona.yaml   — 페르소나 정체성 (이름·제작자·톤·가족 로스터)
  contacts.yaml  — 연락처 로스터 {entity: {name, role, channel handles, aliases}}
  llm.yaml       — LLM 프로필 (lite/full-subscription/full-local)

파일이 없으면 빈 dict 반환 → 호출부가 안전하게 처리.
"""
from __future__ import annotations
import os
import functools

def _find_config_dir() -> str:
    """config 위치 탐색 — 사용자가 어디에 두든 찾는다.
    우선순위: $LIAIN_CONFIG_DIR > ./config/ > ./ (작업 디렉토리 루트)
    """
    env = os.environ.get("LIAIN_CONFIG_DIR")
    if env:
        return env
    cwd = os.getcwd()
    sub = os.path.join(cwd, "config")
    if os.path.exists(os.path.join(sub, "persona.yaml")):
        return sub
    if os.path.exists(os.path.join(cwd, "persona.yaml")):
        return cwd
    return sub   # 기본(없어도 여기로 안내)


CONFIG_DIR = _find_config_dir()


def _load_yaml(name: str) -> dict:
    path = os.path.join(CONFIG_DIR, name)
    if not os.path.exists(path):
        return {}
    try:
        import yaml
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[liain.config] {name} 로드 실패: {e}", flush=True)
        return {}


@functools.lru_cache(maxsize=1)
def _contacts() -> dict:
    return _load_yaml("contacts.yaml")


@functools.lru_cache(maxsize=1)
def _persona_cfg() -> dict:
    return _load_yaml("persona.yaml")


def reload():
    _contacts.cache_clear()
    _persona_cfg.cache_clear()


# ─── 로스터 ───────────────────────────────────────────
def people() -> dict:
    return _contacts().get("people") or {}


def groups() -> dict:
    return _contacts().get("groups") or {}


def location(key: str, default: str = "") -> str:
    return (_contacts().get("locations") or {}).get(key, default)


def person_by_role(role: str) -> dict:
    for entity, p in people().items():
        if p.get("role") == role:
            return {**p, "entity": entity}
    return {}


def primary_entity() -> str:
    """주 사용자(owner=dad 역할) 엔티티. 없으면 ''."""
    p = person_by_role("dad")
    return p.get("entity", "") if p else ""


def _primary_imessage(p: dict) -> str:
    im = p.get("imessage")
    if isinstance(im, (list, tuple)):
        return im[0] if im else ""
    return im or ""


def entity_aliases() -> dict:
    out = {}
    for entity, p in people().items():
        al = p.get("aliases") or [x for x in (p.get("name"), p.get("display")) if x]
        out[entity] = [str(a) for a in al]
    return out


def alias_to_entity() -> dict:
    out = {}
    for entity, aliases in entity_aliases().items():
        for a in aliases:
            out[a] = entity
    return out


# ─── 페르소나 ─────────────────────────────────────────
def persona() -> dict:
    return _persona_cfg().get("persona") or {}


def family_in_prompt() -> list:
    return _persona_cfg().get("family_in_prompt") or []
