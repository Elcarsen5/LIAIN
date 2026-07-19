"""LLM 백엔드 어댑터 — 구독 CLI / 로컬 Ollama / 유료 API.

API Zero 기본: claude_cli(구독) + ollama(로컬)만. 유료 API는 사용자가 키 넣을 때만.
"""
from __future__ import annotations
import os
import subprocess

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")


def _find_claude_bin() -> str:
    if env := os.environ.get("CLAUDE_CODE_PATH"):
        if os.path.exists(env):
            return env
    import shutil
    return (shutil.which("claude")
            or ("/opt/homebrew/bin/claude" if os.path.exists("/opt/homebrew/bin/claude")
                else "/usr/local/bin/claude"))


# ─── 구독 CLI (Claude Code) ────────────────────────────
def claude_cli_complete(system: str, prompt: str, model: str = "sonnet",
                        timeout: int = 120, **_) -> str:
    """Claude Code CLI (구독, 무과금). 유료 키 사용 안 함."""
    bin_path = _find_claude_bin()
    if not os.path.exists(bin_path):
        return ""
    cmd = [bin_path, "-p", "--tools", "", "--model", model]
    if system:
        cmd += ["--system-prompt", system]
    env = os.environ.copy()
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(k, None)
    if cfg := os.environ.get("LIAIN_CLAUDE_CONFIG_DIR"):
        env["CLAUDE_CONFIG_DIR"] = cfg
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           timeout=timeout, env=env)
        reply = (r.stdout or "").strip()
        low = reply.lower()
        if reply and not any(p in low for p in
                             ("not logged in", "please run /login", "invalid api key")):
            return reply
    except Exception as e:
        print(f"[liain.llm/claude_cli] {e}", flush=True)
    return ""


# ─── 로컬 Ollama ───────────────────────────────────────
def ollama_complete(system: str, prompt: str, model: str = "qwen3:14b",
                    timeout: int = 180, max_tokens: int = 800, **_) -> str:
    try:
        import requests
        msgs = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
        r = requests.post(f"{OLLAMA_URL}/api/chat", json={
            "model": model, "messages": msgs, "stream": False,
            "options": {"temperature": 0.6, "num_predict": max_tokens},
        }, timeout=timeout)
        return ((r.json().get("message") or {}).get("content") or "").strip()
    except Exception as e:
        print(f"[liain.llm/ollama] {model}: {e}", flush=True)
        return ""


def api_complete(system: str, prompt: str, model: str = "", **_) -> str:
    print("[liain.llm/api] 유료 API 백엔드 — 키 설정 + 구현 필요(기본 API Zero).", flush=True)
    return ""


def parse_backend(spec: str) -> tuple[str, str]:
    if ":" in spec:
        kind, model = spec.split(":", 1)
        return kind, model
    return spec, ""


def complete(spec: str, system: str, prompt: str, **opts) -> str:
    kind, model = parse_backend(spec)
    if kind == "claude_cli":
        return claude_cli_complete(system, prompt, model=model or "sonnet", **opts)
    if kind == "ollama":
        return ollama_complete(system, prompt, model=model or "qwen3:14b", **opts)
    if kind == "api":
        return api_complete(system, prompt, model=model, **opts)
    if kind == "none":
        return ""
    print(f"[liain.llm] 알 수 없는 백엔드: {spec}", flush=True)
    return ""
