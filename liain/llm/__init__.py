"""LLM 역할 라우터 — config/llm.yaml의 profile로 역할별 백엔드 결정.

    from liain import llm
    llm.complete("chat", system, prompt)
    llm.backend_for("vision")
"""
from __future__ import annotations
import os
import functools

from liain.llm import profiles as _profiles
from liain.llm import backends as _backends


@functools.lru_cache(maxsize=1)
def _resolved() -> dict:
    from liain import config as _cfg
    cfg = {}
    path = os.path.join(_cfg.CONFIG_DIR, "llm.yaml")
    if os.path.exists(path):
        try:
            import yaml
            with open(path) as f:
                cfg = yaml.safe_load(f) or {}
        except Exception as e:
            print(f"[liain.llm] llm.yaml 로드 실패: {e}", flush=True)
    profile = (os.environ.get("LIAIN_LLM_PROFILE")
               or cfg.get("profile") or _profiles.DEFAULT_PROFILE)
    overrides = cfg.get("backends") or {}
    return {"_profile": profile, **_profiles.resolve(profile, overrides)}


def reload():
    _resolved.cache_clear()


def profile_name() -> str:
    return _resolved().get("_profile", _profiles.DEFAULT_PROFILE)


def backend_for(role: str) -> str:
    return _resolved().get(role, "none")


def complete(role: str, system: str, prompt: str, **opts) -> str:
    return _backends.complete(backend_for(role), system, prompt, **opts)


def info() -> dict:
    r = dict(_resolved())
    return {"profile": r.pop("_profile", ""), "roles": r}
