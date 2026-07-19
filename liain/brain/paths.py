"""기억 데이터 저장 경로.

Liain은 **작업 디렉토리 기준**으로 기억을 쌓는다 — 페르소나 폴더 하나가
그 인격의 전부(config + 기억)가 되도록.

    my-persona/
      persona.yaml  contacts.yaml  llm.yaml  .env
      brain/            ← 기억 (자동 생성)
      diary/            ← 일기 (자동 생성)

환경변수 `LIAIN_DATA_DIR`로 위치를 바꿀 수 있다 (여러 인격을 한 곳에서 굴릴 때).
"""
import os

_ROOT = os.environ.get("LIAIN_DATA_DIR") or os.getcwd()

BRAIN_DIR = os.path.join(_ROOT, "brain")
DIARY_DIR = os.path.join(_ROOT, "diary")


def ensure_dirs():
    for d in (BRAIN_DIR, DIARY_DIR):
        os.makedirs(d, exist_ok=True)


def data_root() -> str:
    return _ROOT
