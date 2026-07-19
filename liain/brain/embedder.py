"""SBERT (Sentence-BERT) embedder — P3.1 Hybrid Retrieval.

자료 권고 ❷ Episodic Store: vector embedding으로 의미 검색 보강.
FTS5 BM25는 정확 토큰 매칭 — 패러프레이즈 ("아빠 어디 살아?" vs "거주지: 인천 영종도")
못 잡음. SBERT로 의미 cosine + RRF로 hybrid.

설계:
- Lazy load (첫 embed 호출 시 모델 다운로드/로드, 5~15초)
- Apple Silicon MPS 가속 (torch.device("mps")) — Pi5는 CPU
- 배치 처리 — backfill 시 batch_size=32로
- L2 normalize (cosine = dot product)
- 384차원 (multilingual MiniLM) — 한국어 OK + 빠름

저장:
- entity_records.add() 안에서 자동 embed → records.json의 'embedding' 필드 (list[float; 384])
- backfill: 기존 records 백그라운드 batch embed

사용:
    from memory_embedder import embed, embed_batch
    vec = embed("아빠 어디 살아?")        # → np.array shape=(384,)
    vecs = embed_batch(["...", "..."])    # → np.array shape=(N, 384)
"""
from __future__ import annotations
import os
import threading
from typing import Sequence

# 모델 — multilingual MiniLM, 한국어 OK
MODEL_NAME = os.environ.get("SBERT_MODEL", "intfloat/multilingual-e5-small")
EMBED_DIM = 384

_MODEL = None
_LOAD_LOCK = threading.Lock()


def _device():
    """Apple Silicon MPS 가속 우선, 없으면 CPU."""
    try:
        import torch
        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def _load_model():
    """Lazy load SentenceTransformer (5~15초 첫 호출). Thread-safe."""
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    with _LOAD_LOCK:
        if _MODEL is not None:
            return _MODEL
        try:
            from sentence_transformers import SentenceTransformer
            dev = _device()
            print(f"[memory_embedder] {MODEL_NAME} 로드 중 (device={dev})...", flush=True)
            _MODEL = SentenceTransformer(MODEL_NAME, device=dev)
            # warm-up — 첫 inference latency 줄임
            _MODEL.encode("warmup", convert_to_numpy=True, normalize_embeddings=True)
            print(f"[memory_embedder] 로드 완료. dim={_MODEL.get_sentence_embedding_dimension()}",
                  flush=True)
        except Exception as e:
            print(f"[memory_embedder] 로드 실패: {e}", flush=True)
            _MODEL = None
    return _MODEL


def is_ready() -> bool:
    """모델 로드 가능한지 (의존 라이브러리 + 모델 캐시)."""
    try:
        import sentence_transformers, torch  # noqa: F401
        return True
    except ImportError:
        return False


def embed(text: str):
    """단일 텍스트 → np.array(384,). 실패 시 None."""
    if not text:
        return None
    m = _load_model()
    if m is None:
        return None
    # e5 모델은 query/passage prefix 권장 — 단순화하여 passage로 통일
    text = f"passage: {text}" if "e5" in MODEL_NAME.lower() else text
    try:
        return m.encode(text, convert_to_numpy=True, normalize_embeddings=True)
    except Exception as e:
        print(f"[memory_embedder] embed 실패: {e}", flush=True)
        return None


def embed_query(text: str):
    """검색 query용 (e5 모델은 query: prefix). cosine 시 passage embed와 매칭."""
    if not text:
        return None
    m = _load_model()
    if m is None:
        return None
    text = f"query: {text}" if "e5" in MODEL_NAME.lower() else text
    try:
        return m.encode(text, convert_to_numpy=True, normalize_embeddings=True)
    except Exception as e:
        print(f"[memory_embedder] embed_query 실패: {e}", flush=True)
        return None


def embed_batch(texts: Sequence[str], batch_size: int = 32):
    """여러 텍스트 → np.array(N, 384). backfill용."""
    if not texts:
        return None
    m = _load_model()
    if m is None:
        return None
    prefixed = [f"passage: {t}" if "e5" in MODEL_NAME.lower() else t for t in texts]
    try:
        return m.encode(prefixed, batch_size=batch_size,
                        convert_to_numpy=True, normalize_embeddings=True,
                        show_progress_bar=False)
    except Exception as e:
        print(f"[memory_embedder] embed_batch 실패: {e}", flush=True)
        return None


def cosine(a, b) -> float:
    """L2 normalize된 두 벡터의 dot product (= cosine similarity)."""
    if a is None or b is None:
        return 0.0
    try:
        import numpy as np
        return float(np.dot(a, b))
    except Exception:
        return 0.0


def cosine_topk(query_vec, candidates: Sequence, k: int = 10) -> list[tuple[int, float]]:
    """query_vec과 candidates(list of np.array) 중 top-k cosine.

    Returns: [(idx, score), ...] descending.
    """
    if query_vec is None or not candidates:
        return []
    try:
        import numpy as np
        # candidates를 (N, D) matrix로
        valid_idx = [i for i, c in enumerate(candidates) if c is not None]
        if not valid_idx:
            return []
        mat = np.array([candidates[i] for i in valid_idx])
        scores = mat @ query_vec  # (N,)
        order = np.argsort(-scores)[:k]
        return [(valid_idx[i], float(scores[i])) for i in order]
    except Exception as e:
        print(f"[memory_embedder] cosine_topk 실패: {e}", flush=True)
        return []


__all__ = [
    "MODEL_NAME", "EMBED_DIM",
    "is_ready", "embed", "embed_query", "embed_batch",
    "cosine", "cosine_topk",
]


# CLI: 단발 검증
if __name__ == "__main__":
    import sys
    if "--smoke" in sys.argv:
        print(f"is_ready: {is_ready()}")
        v = embed("아빠는 인천 영종도에 산다")
        print(f"embed shape: {v.shape if v is not None else None}")
        q = embed_query("아빠 어디 살아?")
        print(f"query embed shape: {q.shape if q is not None else None}")
        print(f"cosine: {cosine(v, q):.4f}")
        v2 = embed("A가 학원 갔어")
        print(f"unrelated cosine: {cosine(v2, q):.4f}")
    else:
        print("Usage: python3 memory_embedder.py --smoke")
