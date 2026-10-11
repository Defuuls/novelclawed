"""
向量数据库模块：SQLite + numpy 实现（替代 ChromaDB，可在 Android/Chaquopy 上运行）

Vector store backed by a single SQLite file per db_path with brute-force cosine
similarity in numpy. Keeps the public API previously provided on top of
ChromaDB: add_documents / search / delete_collection / get_collection_size,
plus a small ``collection`` shim (add / upsert / count / query).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional

import numpy as np

from multiagent.config import Config

_LOCK = threading.RLock()


def _match(meta: Dict[str, Any], where: Optional[Dict[str, Any]]) -> bool:
    """Evaluate a Chroma-style metadata filter ($and/$or/$eq/$ne/$in/$nin/$gt/$gte/$lt/$lte)."""
    if not where:
        return True
    for key, cond in where.items():
        if key == "$and":
            if not all(_match(meta, c) for c in cond):
                return False
            continue
        if key == "$or":
            if not any(_match(meta, c) for c in cond):
                return False
            continue
        val = meta.get(key)
        if isinstance(cond, dict):
            for op, ref in cond.items():
                try:
                    ok = {
                        "$eq": lambda: val == ref,
                        "$ne": lambda: val != ref,
                        "$in": lambda: val in ref,
                        "$nin": lambda: val not in ref,
                        "$gt": lambda: val is not None and val > ref,
                        "$gte": lambda: val is not None and val >= ref,
                        "$lt": lambda: val is not None and val < ref,
                        "$lte": lambda: val is not None and val <= ref,
                    }[op]()
                except (KeyError, TypeError):
                    ok = False
                if not ok:
                    return False
        elif val != cond:
            return False
    return True


class _Collection:
    def __init__(self, conn: sqlite3.Connection, name: str):
        self._conn = conn
        self.name = name

    def upsert(self, ids, embeddings=None, documents=None, metadatas=None):
        n = len(ids)
        embeddings = embeddings if embeddings is not None else [None] * n
        documents = documents if documents is not None else [""] * n
        metadatas = metadatas if metadatas is not None else [{}] * n
        rows = []
        for i in range(n):
            emb = embeddings[i]
            blob = None
            dim = 0
            if emb is not None:
                arr = np.asarray(emb, dtype=np.float32).reshape(-1)
                blob, dim = arr.tobytes(), int(arr.shape[0])
            rows.append((self.name, str(ids[i]), documents[i] or "", json.dumps(metadatas[i] or {}, ensure_ascii=False), blob, dim))
        with _LOCK:
            self._conn.executemany(
                "INSERT OR REPLACE INTO vectors(collection, id, document, metadata, embedding, dim) VALUES (?,?,?,?,?,?)",
                rows,
            )
            self._conn.commit()

    add = upsert

    def count(self) -> int:
        with _LOCK:
            row = self._conn.execute("SELECT COUNT(*) FROM vectors WHERE collection=?", (self.name,)).fetchone()
        return int(row[0] if row else 0)

    def query(self, query_embeddings, n_results: int = 5, where=None, **_: Any):
        q = np.asarray(query_embeddings[0], dtype=np.float32).reshape(-1)
        with _LOCK:
            rows = self._conn.execute(
                "SELECT id, document, metadata, embedding, dim FROM vectors WHERE collection=?",
                (self.name,),
            ).fetchall()
        cands = []
        for rid, doc, meta_raw, blob, dim in rows:
            try:
                meta = json.loads(meta_raw or "{}")
            except ValueError:
                meta = {}
            if not _match(meta, where):
                continue
            if blob is None or dim != q.shape[0]:
                dist = 1.0
            else:
                v = np.frombuffer(blob, dtype=np.float32)
                denom = float(np.linalg.norm(v) * np.linalg.norm(q))
                dist = 1.0 - (float(np.dot(v, q)) / denom if denom > 0 else 0.0)
            cands.append((dist, rid, doc, meta))
        cands.sort(key=lambda c: c[0])
        top = cands[: max(0, int(n_results or 0))]
        return {
            "ids": [[c[1] for c in top]],
            "documents": [[c[2] for c in top]],
            "metadatas": [[c[3] for c in top]],
            "distances": [[c[0] for c in top]],
        }

    def get(self, limit: Optional[int] = None, offset: int = 0, include=None, **_: Any):
        with _LOCK:
            rows = self._conn.execute(
                "SELECT id, document, metadata, embedding FROM vectors WHERE collection=? ORDER BY rowid LIMIT ? OFFSET ?",
                (self.name, -1 if limit is None else int(limit), int(offset or 0)),
            ).fetchall()
        return {
            "ids": [r[0] for r in rows],
            "documents": [r[1] for r in rows],
            "metadatas": [json.loads(r[2] or "{}") for r in rows],
            "embeddings": [np.frombuffer(r[3], dtype=np.float32).tolist() if r[3] else None for r in rows],
        }


class VectorStore:
    """向量数据库管理器 (SQLite + numpy)"""

    def __init__(self, config: Config, collection_name: str = "documents", db_path: Optional[str] = None):
        self.config = config
        self.collection_name = collection_name
        self.db_path = db_path or config.vector_db_path
        os.makedirs(self.db_path, exist_ok=True)
        self._conn = sqlite3.connect(os.path.join(self.db_path, "vectors.sqlite3"), check_same_thread=False, timeout=30)
        with _LOCK:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS vectors (collection TEXT NOT NULL, id TEXT NOT NULL, document TEXT, "
                "metadata TEXT, embedding BLOB, dim INTEGER, PRIMARY KEY(collection, id))"
            )
            self._conn.commit()
        self.collection = _Collection(self._conn, collection_name)

    def add_documents(self, documents: List[Dict]):
        if not documents:
            return
        self.collection.upsert(
            ids=[doc["id"] for doc in documents],
            embeddings=[doc.get("embedding") for doc in documents],
            documents=[doc["text"] for doc in documents],
            metadatas=[doc.get("metadata", {}) for doc in documents],
        )

    def search(self, query_embedding: List[float], top_k: Optional[int] = None, filter_metadata: Optional[Dict] = None) -> List[Dict]:
        top_k = top_k or self.config.top_k
        results = self.collection.query(query_embeddings=[query_embedding], n_results=top_k, where=filter_metadata or None)
        return [
            {"text": results["documents"][0][i], "metadata": results["metadatas"][0][i], "distance": results["distances"][0][i]}
            for i in range(len(results["ids"][0]))
        ]

    def delete_collection(self):
        with _LOCK:
            self._conn.execute("DELETE FROM vectors WHERE collection=?", (self.collection_name,))
            self._conn.commit()

    def get_collection_size(self) -> int:
        return self.collection.count()
