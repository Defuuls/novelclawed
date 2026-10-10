"""
In-process replacement for the subprocess-based generation workers.

Android (Chaquopy) cannot spawn ``sys.executable -m ...`` child processes, so
when APP_INPROCESS_WORKERS=1 (the default on Android) the job runners call the
worker function directly in a background thread instead.

* The engine reads its configuration from ``os.environ``. To let several jobs
  run (or sit waiting for between-chapter input) concurrently, ``os.environ`` is
  replaced by a thread-aware overlay: each job thread (and any thread it spawns)
  sees its own per-job variables on top of the process environment.
* stdout/stderr of the worker thread are routed line-by-line into a queue so the
  job runner can tee them into worker.log exactly like the subprocess version.
* Cancellation is cooperative: a threading.Event is checked on every print from
  the worker thread and before every LLM HTTP request; when set, a
  CancelledByUser exception is raised inside the worker thread.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
from typing import Callable, Dict, Optional

from collections.abc import MutableMapping

_local = threading.local()


class _OverlayEnviron(MutableMapping):
    """os.environ replacement: per-thread overlay on top of the real environ."""

    def __init__(self, base):
        self._base = base

    def _overlay(self):
        return getattr(_local, "env", None)

    def __getitem__(self, key):
        ov = self._overlay()
        if ov is not None and key in ov:
            value = ov[key]
            if value is None:
                raise KeyError(key)
            return value
        return self._base[key]

    def __setitem__(self, key, value):
        ov = self._overlay()
        if ov is not None:
            ov[key] = str(value)
        else:
            self._base[key] = value

    def __delitem__(self, key):
        ov = self._overlay()
        if ov is not None:
            if key not in self:
                raise KeyError(key)
            ov[key] = None
        else:
            del self._base[key]

    def _keys(self):
        ov = self._overlay() or {}
        keys = [k for k in self._base.keys() if ov.get(k, "") is not None]
        keys += [k for k, v in ov.items() if v is not None and k not in self._base]
        return keys

    def __iter__(self):
        return iter(self._keys())

    def __len__(self):
        return len(self._keys())

    def copy(self):
        return dict(self.items())

    def __getattr__(self, name):  # encodekey, putenv helpers, ...
        return getattr(self._base, name)

    def __repr__(self):
        return f"OverlayEnviron({dict(self.items())!r})"


_env_installed = False


def _install_env_overlay() -> None:
    global _env_installed
    if _env_installed:
        return
    os.environ = _OverlayEnviron(os.environ)  # type: ignore[assignment]
    _orig_start = threading.Thread.start

    def _start(self, *a, **k):
        # Child threads inherit the job context (env overlay, log sink, cancel flag).
        parent = (getattr(_local, "env", None), getattr(_local, "sink", None), getattr(_local, "cancel_event", None))
        if any(x is not None for x in parent):
            orig_run = self.run

            def _run():
                _local.env, _local.sink, _local.cancel_event = parent
                _local.raising = False
                orig_run()

            self.run = _run
        return _orig_start(self, *a, **k)

    threading.Thread.start = _start  # type: ignore[assignment]
    _env_installed = True


class CancelledByUser(Exception):
    pass


def inprocess_enabled() -> bool:
    raw = os.getenv("APP_INPROCESS_WORKERS", "")
    if raw.strip():
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return "ANDROID_ROOT" in os.environ or hasattr(sys, "getandroidapilevel")


class _ThreadRouter:
    """sys.stdout/sys.stderr replacement that routes per-thread."""

    def __init__(self, fallback):
        self._fallback = fallback

    def _target(self):
        return getattr(_local, "sink", None)

    def write(self, data):
        sink = self._target()
        if sink is None:
            try:
                return self._fallback.write(data)
            except Exception:
                return 0
        cancel = getattr(_local, "cancel_event", None)
        if cancel is not None and cancel.is_set() and not getattr(_local, "raising", False):
            _local.raising = True
            raise CancelledByUser("Job cancelled")
        sink(data)
        return len(data)

    def flush(self):
        if self._target() is None:
            try:
                self._fallback.flush()
            except Exception:
                pass

    def reconfigure(self, *args, **kwargs):
        return None

    def isatty(self):
        return False

    def __getattr__(self, name):
        return getattr(self._fallback, name)


_installed = False
_install_lock = threading.Lock()


def _install_router() -> None:
    global _installed
    with _install_lock:
        if _installed:
            return
        sys.stdout = _ThreadRouter(sys.stdout)
        sys.stderr = _ThreadRouter(sys.stderr)
        _installed = True


def _set_llm_cancel_hooks(fn: Optional[Callable[[], bool]]) -> None:
    for mod_name in ("novelclaw.utils.openai_compat", "multiagent.utils.openai_compat"):
        mod = sys.modules.get(mod_name)
        if mod is None:
            try:
                __import__(mod_name)
                mod = sys.modules[mod_name]
            except Exception:
                continue
        mod.set_cancel_check(fn)


class InProcessJob:
    """Mimics enough of subprocess.Popen for the job runners' polling loop."""

    def __init__(self, target: Callable[[], int], env: Dict[str, str]):
        _install_router()
        _install_env_overlay()
        self.lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self.cancel_event = threading.Event()
        self.returncode: Optional[int] = None
        self._target = target
        self._env = env
        self._buf = ""
        self.pid = 0
        self._thread = threading.Thread(target=self._run, name="novelclaw-job", daemon=True)

    def start(self) -> "InProcessJob":
        self._thread.start()
        self.pid = self._thread.ident or 0
        return self

    def _sink(self, data: str) -> None:
        self._buf += data
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.lines.put(line + "\n")

    def _run(self) -> None:
        _local.env = {k: str(v) for k, v in self._env.items()}
        _local.sink = self._sink
        _local.cancel_event = self.cancel_event
        _local.raising = False
        _set_llm_cancel_hooks(self.cancel_event.is_set)
        try:
            self.returncode = int(self._target() or 0)
        except BaseException as exc:  # noqa: BLE001 - report everything to the log
            self._sink(f"[system] worker thread error: {exc!r}\n")
            self.returncode = 1
        finally:
            _set_llm_cancel_hooks(None)
            _local.sink = None
            _local.cancel_event = None
            _local.env = None
            if self._buf:
                self.lines.put(self._buf)
                self._buf = ""
            self.lines.put(None)

    # Popen-like API -----------------------------------------------------
    def poll(self) -> Optional[int]:
        return None if self._thread.is_alive() else (self.returncode if self.returncode is not None else 1)

    def terminate(self) -> None:
        self.cancel_event.set()

    kill = terminate

    def wait(self, timeout: Optional[float] = None) -> int:
        self._thread.join(timeout)
        if self._thread.is_alive():
            import subprocess
            raise subprocess.TimeoutExpired("in-process-worker", timeout or 0)
        return self.returncode if self.returncode is not None else 1
