"""간단한 .env 로더 — launchd/systemd 호환. 이미 set된 키는 덮어쓰지 않음."""
import os


def load(path: str | None = None, verbose: bool = False) -> int:
    p = path or os.environ.get("LIAIN_ENV_FILE",
                               os.path.join(os.getcwd(), ".env"))
    if not os.path.exists(p):
        return 0
    n = 0
    try:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and not os.environ.get(k):
                    os.environ[k] = v
                    n += 1
    except Exception as e:
        if verbose:
            print(f"[liain.secrets] 로드 실패: {e}", flush=True)
    return n
