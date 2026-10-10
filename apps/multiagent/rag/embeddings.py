"""
Embedding helpers for the Android build.

* RemoteEmbedder: calls the configured OpenAI-compatible ``/embeddings`` endpoint.
  Enabled when EMBEDDING_API_MODEL is set (EMBEDDING_BASE_URL / EMBEDDING_API_KEY
  default to the chat LLM's base URL / key).
* keyword_embeddings: deterministic hashed bag-of-words vectors, used when no
  remote embedding model is configured or the remote call fails.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import List, Optional

import numpy as np

KEYWORD_DIM = 512
_TOKEN_RE = re.compile(r"[A-Za-z0-9_']+|[\u4e00-\u9fff]")


def _tokens(text: str) -> List[str]:
    toks = [t.lower() for t in _TOKEN_RE.findall(text or "")]
    # add CJK bigrams so Chinese gets some phrase-level signal
    cjk = [t for t in toks if len(t) == 1 and "\u4e00" <= t <= "\u9fff"]
    toks.extend(a + b for a, b in zip(cjk, cjk[1:]))
    return toks


def keyword_embeddings(texts: List[str], dim: int = KEYWORD_DIM) -> np.ndarray:
    out = np.zeros((len(texts), dim), dtype=np.float32)
    for row, text in enumerate(texts):
        for tok in _tokens(text):
            h = int.from_bytes(hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest(), "little")
            out[row, h % dim] += 1.0 if (h >> 63) == 0 else -1.0
        norm = float(np.linalg.norm(out[row]))
        if norm > 0:
            out[row] /= norm
    return out


class RemoteEmbedder:
    def __init__(self, model: str, base_url: str, api_key: str, timeout: Optional[float] = 60.0):
        from multiagent.utils.openai_compat import OpenAICompatClient

        self.model = model
        self.client = OpenAICompatClient(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=1)

    @classmethod
    def from_config(cls, config) -> Optional["RemoteEmbedder"]:
        model = (os.getenv("EMBEDDING_API_MODEL", "") or "").strip()
        if not model or model.lower() in {"none", "off", "disabled", "keyword"}:
            return None
        base = (os.getenv("EMBEDDING_BASE_URL", "") or getattr(config, "api_base_url", "") or "").strip()
        key = (os.getenv("EMBEDDING_API_KEY", "") or getattr(config, "api_key", "") or "").strip()
        if not base:
            return None
        return cls(model=model, base_url=base, api_key=key)

    def encode(self, texts: List[str]) -> np.ndarray:
        vectors: List[List[float]] = []
        for i in range(0, len(texts), 64):
            vectors.extend(self.client.embeddings(self.model, list(texts[i:i + 64])))
        return np.asarray(vectors, dtype=np.float32)
