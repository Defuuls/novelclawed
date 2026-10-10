"""
Single-process NovelClaw server (used by the Android app, also runnable on desktop).

Merges the three services that docker-compose runs on ports 8010/8011/8012 into
one FastAPI app on one localhost port:

    /                -> auth-portal (single-user auto-login + mode selection)
    /multiagent/...  -> MultiAgent workspace   (was :8011)
    /claw/...        -> NovelClaw workspace    (was :8012)
    /settings        -> on-device LLM settings (base URL / API key / model)

All state lives under NOVELCLAW_DATA_DIR (set by the Android service to
context.filesDir). Run on desktop with:

    cd apps && NOVELCLAW_DATA_DIR=/tmp/nc python android_server.py --port 8765
"""

import argparse
import html
import json
import os
import secrets
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

APPS_DIR = Path(__file__).resolve().parent
if str(APPS_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_DIR))
DEFAULT_PORT = 8765
CUSTOM_SLUG = "custom"

_server = None
_server_thread: Optional[threading.Thread] = None


def _data_root() -> Path:
    raw = os.getenv("NOVELCLAW_DATA_DIR", "").strip()
    root = Path(raw).expanduser() if raw else (Path.home() / ".novelclaw")
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _settings_path() -> Path:
    return _data_root() / "llm_settings.json"


def load_llm_settings() -> Dict[str, str]:
    path = _settings_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {k: str(v or "") for k, v in data.items()}
        except Exception:
            pass
    return {}


def apply_llm_settings(cfg: Dict[str, str]) -> None:
    """Expose the on-device settings to both engines through env vars.

    No provider or model is hard-coded: the user's base URL + model become the
    'custom' provider and the default provider for new jobs.
    """
    base_url = (cfg.get("base_url") or "").strip().rstrip("/")
    model = (cfg.get("model") or "").strip()
    api_key = (cfg.get("api_key") or "").strip()
    if base_url and model:
        os.environ["WEB_EXTRA_PROVIDERS_JSON"] = json.dumps(
            [{
                "slug": CUSTOM_SLUG,
                "label": (cfg.get("label") or "").strip() or "My model",
                "base_url": base_url,
                "model": model,
                "wire_api": (cfg.get("wire_api") or "chat").strip() or "chat",
            }]
        )
        os.environ["WEB_DEFAULT_PROVIDER"] = CUSTOM_SLUG
        os.environ["LLM_PROVIDER"] = CUSTOM_SLUG
        os.environ["LLM_BASE_URL"] = base_url
        os.environ["MODEL_NAME"] = model
        # Local servers (Ollama, llama.cpp) accept any key; send a placeholder so
        # the engine's "missing API key" guards don't trip.
        effective_key = api_key or "no-key"
        os.environ["CUSTOM_API_KEY"] = effective_key
        os.environ["LLM_API_KEY"] = effective_key
    for env_name, key in (("EMBEDDING_API_MODEL", "embedding_model"), ("EMBEDDING_BASE_URL", "embedding_base_url")):
        val = (cfg.get(key) or "").strip()
        if val:
            os.environ[env_name] = val
        else:
            os.environ.pop(env_name, None)
    if (cfg.get("timeout") or "").strip().isdigit():
        os.environ["LLM_TIMEOUT_SECONDS"] = cfg["timeout"].strip()


def save_llm_settings(cfg: Dict[str, str]) -> None:
    path = _settings_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    tmp.replace(path)
    apply_llm_settings(cfg)


def _persistent_secret(name: str, factory) -> str:
    path = _data_root() / name
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = factory()
    path.write_text(value, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return value


def _configure_environment(port: int) -> Path:
    root = _data_root()
    db_path = root / "novelclaw.db"
    os.environ.setdefault("APP_SINGLE_PROCESS", "1")
    os.environ.setdefault("APP_INPROCESS_WORKERS", "1")
    os.environ["APP_DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["APP_AUTH_DATABASE_URL"] = os.environ["APP_DATABASE_URL"]
    os.environ["APP_SHARED_PORTAL_PORT"] = "same"
    os.environ["APP_SHARED_PORTAL_URL"] = ""
    os.environ["APP_MULTIAGENT_URL"] = "/multiagent/dashboard"
    os.environ["APP_CLAW_URL"] = "/claw/dashboard"
    os.environ.setdefault("APP_SESSION_COOKIE_NAME", "novelclaw_session")
    os.environ.setdefault("APP_SESSION_SECRET", _persistent_secret("session.secret", lambda: secrets.token_urlsafe(48)))

    def _fernet_key() -> str:
        from cryptography.fernet import Fernet

        return Fernet.generate_key().decode("ascii")

    os.environ.setdefault("APP_ENCRYPTION_KEY", _persistent_secret("fernet.key", _fernet_key))
    os.environ.setdefault("WEB_MODELLESS_MODE", "0")
    os.environ.setdefault("WEB_UI_LANGUAGE", "en")
    os.environ.setdefault("LLM_TIMEOUT_SECONDS", "600")
    os.environ.setdefault("LLM_MAX_RETRIES", "2")
    os.environ.setdefault("VECTOR_DB_PATH", str(root / "vector_db"))
    os.environ.setdefault("PYTHONUTF8", "1")
    apply_llm_settings(load_llm_settings())
    # The engines use relative paths in a few places; keep them inside the data dir.
    os.chdir(root)
    return root


def _import_portal(name: str, data_root: Path, base_path: str):
    """Import one sub-app with its own data/runs dirs and base path."""
    os.environ["APP_DATA_DIR"] = str(data_root / name / "data")
    os.environ["APP_RUNS_DIR"] = str(data_root / name / "runs")
    os.environ["APP_BASE_PATH"] = base_path
    if name == "auth-portal":
        portal_dir = str(APPS_DIR / "auth-portal")
        if portal_dir not in sys.path:
            sys.path.insert(0, portal_dir)
        from local_web_portal.app.main import app as sub_app  # type: ignore
    elif name == "multiagent":
        from multiagent.local_web_portal.app.main import app as sub_app
    else:
        from novelclaw.local_web_portal.app.main import app as sub_app
    return sub_app


SETTINGS_FORM = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Model settings</title>
<style>body{{font-family:system-ui,sans-serif;max-width:640px;margin:0 auto;padding:16px;background:#111;color:#eee}}
label{{display:block;margin-top:14px;font-weight:600}}input,select{{width:100%;padding:10px;margin-top:4px;box-sizing:border-box;background:#222;color:#eee;border:1px solid #444;border-radius:6px}}
small{{color:#aaa}}button{{margin-top:18px;padding:12px 18px;border:0;border-radius:6px;background:#3a7afe;color:#fff;font-size:16px}}
.ok{{background:#1d3b24;padding:10px;border-radius:6px}}.err{{background:#4a1d1d;padding:10px;border-radius:6px}}a{{color:#8ab4ff}}</style></head>
<body><h2>Model settings</h2>{notice}
<p><small>Any OpenAI-compatible endpoint works (OpenRouter, a self-hosted server, Ollama, LM Studio, vLLM, llama.cpp, ...). Stored only on this device.</small></p>
<form method="post" action="/settings">
<label>Base URL<input name="base_url" value="{base_url}" placeholder="https://openrouter.ai/api/v1" required></label>
<label>API key<input name="api_key" type="password" value="" placeholder="{key_hint}" autocomplete="off"><small>Leave blank to keep the saved key. Local servers can use any value.</small></label>
<label>Model name<input name="model" value="{model}" placeholder="exact model id from your provider" required></label>
<label>Display name<input name="label" value="{label}" placeholder="My model"></label>
<label>API style<select name="wire_api"><option value="chat" {chat_sel}>/chat/completions</option><option value="responses" {resp_sel}>/responses</option></select></label>
<label>Embedding model (optional)<input name="embedding_model" value="{embedding_model}" placeholder="leave blank for keyword retrieval"></label>
<label>Request timeout seconds<input name="timeout" value="{timeout}" inputmode="numeric"></label>
<label><input type="checkbox" name="clear_key" value="1" style="width:auto"> Clear saved API key</label>
<button type="submit">Save</button></form>
<form method="post" action="/settings/test"><button type="submit" style="background:#444">Test connection</button></form>
<p><a href="/claw/dashboard">NovelClaw workspace</a> · <a href="/multiagent/dashboard">MultiAgent workspace</a> · <a href="/select-mode">Mode selection</a></p>
</body></html>"""


def build_app(port: int = DEFAULT_PORT):
    from fastapi import FastAPI, Form, Request
    from fastapi.responses import HTMLResponse, RedirectResponse
    from urllib.parse import quote_plus

    root = _configure_environment(port)
    auth_app = _import_portal("auth-portal", root, "")
    multiagent_app = _import_portal("multiagent", root, "/multiagent")
    claw_app = _import_portal("novelclaw", root, "/claw")
    os.environ.pop("APP_BASE_PATH", None)

    from contextlib import asynccontextmanager
    import inspect

    sub_apps = (auth_app, multiagent_app, claw_app)

    @asynccontextmanager
    async def lifespan(_app):
        # Mounted sub-apps don't get lifespan events; run their startup/shutdown
        # handlers (table creation, stale-job recovery) from the root app.
        for sub in sub_apps:
            for handler in list(getattr(sub.router, "on_startup", [])):
                res = handler()
                if inspect.isawaitable(res):
                    await res
        yield
        for sub in sub_apps:
            for handler in list(getattr(sub.router, "on_shutdown", [])):
                res = handler()
                if inspect.isawaitable(res):
                    await res

    app = FastAPI(title="NovelClaw (single process)", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "mode": "single-process"}

    def _render(notice: str = "", error: str = "") -> HTMLResponse:
        cfg = load_llm_settings()
        key = cfg.get("api_key", "")
        hint = (f"saved: {key[:3]}…{key[-3:]}" if len(key) > 8 else ("saved" if key else "sk-..."))
        msg = ""
        if notice:
            msg = f'<p class="ok">{html.escape(notice)}</p>'
        if error:
            msg += f'<p class="err">{html.escape(error)}</p>'
        e = lambda k, d="": html.escape(cfg.get(k, d) or d)
        wire = cfg.get("wire_api", "chat")
        return HTMLResponse(SETTINGS_FORM.format(
            notice=msg, base_url=e("base_url"), key_hint=html.escape(hint), model=e("model"), label=e("label"),
            chat_sel="selected" if wire != "responses" else "", resp_sel="selected" if wire == "responses" else "",
            embedding_model=e("embedding_model"), timeout=e("timeout", os.getenv("LLM_TIMEOUT_SECONDS", "600")),
        ))

    @app.get("/settings")
    def settings_page(message: str = "", error: str = ""):
        return _render(message, error)

    @app.post("/settings")
    def settings_save(
        base_url: str = Form(...),
        model: str = Form(...),
        api_key: str = Form(""),
        label: str = Form(""),
        wire_api: str = Form("chat"),
        embedding_model: str = Form(""),
        timeout: str = Form(""),
        clear_key: str = Form(""),
    ):
        base_url = base_url.strip()
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            return RedirectResponse("/settings?error=" + quote_plus("Base URL must start with http:// or https://"), status_code=303)
        cfg = load_llm_settings()
        cfg.update({
            "base_url": base_url.rstrip("/"), "model": model.strip(), "label": label.strip(),
            "wire_api": "responses" if wire_api == "responses" else "chat",
            "embedding_model": embedding_model.strip(), "timeout": timeout.strip(),
        })
        if clear_key:
            cfg["api_key"] = ""
        elif api_key.strip():
            cfg["api_key"] = api_key.strip()
        save_llm_settings(cfg)
        return RedirectResponse("/settings?message=" + quote_plus("Saved. New jobs will use this model."), status_code=303)

    @app.post("/settings/test")
    def settings_test():
        from novelclaw.utils.openai_compat import OpenAICompatClient, message_content

        cfg = load_llm_settings()
        if not cfg.get("base_url") or not cfg.get("model"):
            return RedirectResponse("/settings?error=" + quote_plus("Save a base URL and model first."), status_code=303)
        client = OpenAICompatClient(api_key=cfg.get("api_key", ""), base_url=cfg["base_url"], timeout=60, max_retries=0)
        try:
            out = message_content(client.chat_completions(
                model=cfg["model"], messages=[{"role": "user", "content": "Reply with the single word: pong"}], max_tokens=16,
            ))
            return RedirectResponse("/settings?message=" + quote_plus(f"Connection OK. Model replied: {out[:80]}"), status_code=303)
        except Exception as exc:
            return RedirectResponse("/settings?error=" + quote_plus(f"Connection failed: {exc}"[:400]), status_code=303)

    @app.get("/")
    def index(request: Request):
        target = "/select-mode" if load_llm_settings().get("base_url") else "/settings"
        return RedirectResponse(target, status_code=303)

    app.mount("/multiagent", multiagent_app)
    app.mount("/claw", claw_app)
    app.mount("/", auth_app)
    return app


def start(port: int = DEFAULT_PORT, host: str = "127.0.0.1") -> int:
    """Start uvicorn in a background thread (called from the Android Service)."""
    global _server, _server_thread
    if _server_thread is not None and _server_thread.is_alive():
        return port
    import uvicorn

    app = build_app(port)
    config = uvicorn.Config(app, host=host, port=port, log_level="info", loop="asyncio", http="h11", ws="none", lifespan="on")
    _server = uvicorn.Server(config)
    _server.install_signal_handlers = lambda: None  # not allowed off the main thread
    _server_thread = threading.Thread(target=_server.run, name="uvicorn", daemon=True)
    _server_thread.start()
    return port


def is_ready(port: int = DEFAULT_PORT) -> bool:
    return bool(_server is not None and getattr(_server, "started", False))


def stop() -> None:
    if _server is not None:
        _server.should_exit = True


def main() -> int:
    parser = argparse.ArgumentParser(description="Run NovelClaw as a single process.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(build_app(args.port), host=args.host, port=args.port, loop="asyncio", http="h11", ws="none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
