"""
文档处理模块：文档分块、嵌入等
"""
import os
from typing import List, Dict, Optional
import uuid
from novelclaw.rag.text_splitter import RecursiveCharacterTextSplitter
# 避免在某些环境中 transformers 自动导入 TensorFlow 引发冲突
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")
os.environ.setdefault("TRANSFORMERS_NO_FLAX", "1")
os.environ.setdefault("USE_TF", "0")


def _embedding_downloads_disabled() -> bool:
    raw = str(os.getenv("DISABLE_EMBEDDING_DOWNLOADS", "1") or "1").strip().lower()
    return raw in {"1", "true", "yes", "on"}


if _embedding_downloads_disabled():
    # Make accidental HuggingFace embedding initialization stay offline by default.
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import numpy as np

from novelclaw.rag.embeddings import RemoteEmbedder, keyword_embeddings


class DocumentProcessor:
    """文档处理器"""
    
    def __init__(self, config):
        self.config = config
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=config.chunk_size,
            chunk_overlap=config.chunk_overlap,
            length_function=len,
        )
        # sentence-transformers/torch are not available on Android. Embeddings come from
        # the configured OpenAI-compatible /embeddings endpoint when EMBEDDING_API_MODEL
        # is set; otherwise a keyword (hashed bag-of-words) fallback is used.
        self.embedding_model = RemoteEmbedder.from_config(config)

    def split_documents(self, text: str) -> List[str]:
        """
        将文档分割成块
        
        Args:
            text: 原始文本
        
        Returns:
            文本块列表
        """
        chunks = self.text_splitter.split_text(text)
        return chunks
    
    def get_embeddings(self, texts: List[str]) -> np.ndarray:
        """
        获取文本的嵌入向量
        
        Args:
            texts: 文本列表
        
        Returns:
            嵌入向量数组
        """
        if self.embedding_model is None:
            # keyword fallback: hashed bag-of-words vectors (no model required)
            return keyword_embeddings(texts)
        try:
            return self.embedding_model.encode(texts)
        except Exception as exc:
            print(f"[Embeddings] remote embedding failed, using keyword fallback: {exc}")
            return keyword_embeddings(texts)
    
    def process_document(self, text: str, doc_id: Optional[str] = None) -> List[Dict]:
        """
        处理文档：分块并生成嵌入
        
        Args:
            text: 原始文档
            doc_id: 文档唯一ID（可选，用于去重）
        
        Returns:
            包含文本块和嵌入的字典列表
        """
        # doc_id 用于稳定去重；建议传入稳定值（如 file_path hash / jsonl 行号）
        prefix = doc_id or uuid.uuid4().hex
        chunks = self.split_documents(text)
        embeddings = self.get_embeddings(chunks)
        
        result = []
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            result.append({
                "id": f"{prefix}_chunk_{i}",
                "text": chunk,
                "embedding": embedding.tolist(),
                "metadata": {
                    "chunk_index": i,
                    "total_chunks": len(chunks)
                }
            })
        
        return result

