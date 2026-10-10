"""
Pure-Python recursive character text splitter (inlined replacement for
langchain's RecursiveCharacterTextSplitter, which is too heavy for Android).
"""
from __future__ import annotations

from typing import Callable, List, Optional

DEFAULT_SEPARATORS = ["\n\n", "\n", "。", "！", "？", ". ", "! ", "? ", "；", "; ", "，", ", ", " ", ""]


class RecursiveCharacterTextSplitter:
    def __init__(
        self,
        chunk_size: int = 500,
        chunk_overlap: int = 100,
        length_function: Callable[[str], int] = len,
        separators: Optional[List[str]] = None,
        keep_separator: bool = True,
    ):
        if chunk_overlap >= chunk_size:
            chunk_overlap = max(0, chunk_size // 5)
        self.chunk_size = max(1, int(chunk_size))
        self.chunk_overlap = max(0, int(chunk_overlap))
        self.length_function = length_function
        self.separators = separators or list(DEFAULT_SEPARATORS)
        self.keep_separator = keep_separator

    def _split_on(self, text: str, sep: str) -> List[str]:
        if sep == "":
            return list(text)
        parts = text.split(sep)
        if not self.keep_separator:
            return [p for p in parts if p]
        out = [p + sep for p in parts[:-1]] + [parts[-1]]
        return [p for p in out if p]

    def _merge(self, pieces: List[str]) -> List[str]:
        chunks: List[str] = []
        current: List[str] = []
        total = 0
        for piece in pieces:
            plen = self.length_function(piece)
            if current and total + plen > self.chunk_size:
                chunk = "".join(current).strip()
                if chunk:
                    chunks.append(chunk)
                while current and (total > self.chunk_overlap or total + plen > self.chunk_size):
                    total -= self.length_function(current[0])
                    current.pop(0)
            current.append(piece)
            total += plen
        chunk = "".join(current).strip()
        if chunk:
            chunks.append(chunk)
        return chunks

    def _split(self, text: str, separators: List[str]) -> List[str]:
        sep = separators[-1]
        rest: List[str] = []
        for i, candidate in enumerate(separators):
            if candidate == "" or candidate in text:
                sep, rest = candidate, separators[i + 1:]
                break
        good: List[str] = []
        final: List[str] = []
        for piece in self._split_on(text, sep):
            if self.length_function(piece) <= self.chunk_size:
                good.append(piece)
                continue
            if good:
                final.extend(self._merge(good))
                good = []
            if rest:
                final.extend(self._split(piece, rest))
            else:
                final.append(piece)
        if good:
            final.extend(self._merge(good))
        return final

    def split_text(self, text: str) -> List[str]:
        if not text:
            return []
        return self._split(text, self.separators)
