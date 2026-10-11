"""
Minimal pure-Python client for OpenAI-compatible HTTP APIs.

Replaces the ``openai`` SDK (which depends on pydantic v2 / pydantic-core,
unavailable on Android). Works with any OpenAI-compatible endpoint
(OpenAI, DeepSeek, OpenRouter, Ollama, llama.cpp, vLLM, LM Studio, ...).
Only the standard library is used (urllib), so it runs under Chaquopy.

Supported calls: POST {base}/chat/completions, {base}/responses, {base}/embeddings.
"""
from __future__ import annotations

import json
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

# Cooperative cancellation hook. In-process job threads register a callable
# returning True when the job should stop; it is checked before every request.
_local = threading.local()


class CancelledByUser(Exception):
    """Raised inside a worker thread when its job was cancelled."""


def set_cancel_check(fn: Optional[Callable[[], bool]]) -> None:
    _local.cancel_check = fn


def check_cancelled() -> None:
    fn = getattr(_local, "cancel_check", None)
    if fn is not None and fn():
        raise CancelledByUser("Job cancelled")
    # In-process job threads (and threads they spawn) carry a cancel event.
    iw = sys.modules.get("inprocess_worker")
    event = getattr(getattr(iw, "_local", None), "cancel_event", None) if iw else None
    if event is not None and event.is_set():
        raise CancelledByUser("Job cancelled")


class APIError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


_RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


class OpenAICompatClient:
    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://api.openai.com/v1",
        timeout: Optional[float] = None,
        max_retries: int = 1,
        extra_headers: Optional[Dict[str, str]] = None,
    ):
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "").strip().rstrip("/")
        self.timeout = timeout if (timeout and timeout > 0) else None
        self.max_retries = max(0, int(max_retries or 0))
        self.extra_headers = dict(extra_headers or {})

    # -- low level -------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "NovelClaw-Android/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)
        return headers

    def post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.base_url:
            raise APIError("LLM base URL is not configured")
        url = f"{self.base_url}/{path.lstrip('/')}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        attempt = 0
        while True:
            check_cancelled()
            req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                try:
                    return json.loads(raw)
                except ValueError:
                    raise APIError(f"Invalid JSON from {url}: {raw[:300]}")
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                if e.code in _RETRY_STATUSES and attempt < self.max_retries:
                    attempt += 1
                    time.sleep(min(20.0, (2 ** attempt) + random.random()))
                    continue
                raise APIError(f"HTTP {e.code} from {url}: {body[:500]}", status=e.code, body=body)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                if attempt < self.max_retries:
                    attempt += 1
                    time.sleep(min(20.0, (2 ** attempt) + random.random()))
                    continue
                raise APIError(f"Connection error to {url}: {e}")

    # -- endpoints -------------------------------------------------------
    def chat_completions(self, **payload: Any) -> Dict[str, Any]:
        payload = {k: v for k, v in payload.items() if v is not None}
        result = self.post("chat/completions", payload)
        if isinstance(result, dict) and result.get("error") and not result.get("choices"):
            raise APIError(f"API error: {result.get('error')}")
        return result

    def responses(self, **payload: Any) -> Dict[str, Any]:
        payload = {k: v for k, v in payload.items() if v is not None}
        return self.post("responses", payload)

    def embeddings(self, model: str, inputs: List[str]) -> List[List[float]]:
        result = self.post("embeddings", {"model": model, "input": inputs})
        rows = sorted(result.get("data") or [], key=lambda r: r.get("index", 0))
        return [list(map(float, r.get("embedding") or [])) for r in rows]


def message_content(result: Dict[str, Any]) -> str:
    choices = result.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):  # some servers return content parts
        return "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return content or ""


def message_tool_calls(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    choices = result.get("choices") or []
    if not choices:
        return []
    return list((choices[0].get("message") or {}).get("tool_calls") or [])


def responses_output_text(result: Dict[str, Any]) -> str:
    if isinstance(result.get("output_text"), str):
        return result["output_text"]
    parts: List[str] = []
    for item in result.get("output") or []:
        for c in item.get("content") or []:
            if isinstance(c, dict) and c.get("type") in {"output_text", "text"}:
                parts.append(str(c.get("text", "")))
    return "".join(parts)
