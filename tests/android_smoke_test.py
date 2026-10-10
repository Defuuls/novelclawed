"""
Smoke test for the single-process (Android) server on desktop Python.

    python -m venv .venv && .venv/bin/pip install -r requirements-android.txt
    .venv/bin/python tests/android_smoke_test.py

Starts a mock OpenAI-compatible server, starts apps/android_server.py on a free
port, configures the model via /settings, walks key routes of all three merged
apps, runs a NovelClaw job and a MultiAgent job end-to-end against the mock
(in-process worker threads), and checks cooperative cancellation.
"""
import http.cookiejar
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPS = ROOT / "apps"
CALLS = {"chat": 0, "embeddings": 0, "auth": set(), "models": set()}
SLOW = threading.Event()


class MockLLM(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        CALLS["auth"].add(self.headers.get("Authorization", ""))
        CALLS["models"].add(body.get("model"))
        if self.path.endswith("/embeddings"):
            CALLS["embeddings"] += 1
            data = [{"index": i, "embedding": [float(len(t) % 7), 1.0, 0.5]} for i, t in enumerate(body["input"])]
            return self._send({"data": data})
        CALLS["chat"] += 1
        if SLOW.is_set():
            time.sleep(1.5)
        tools = body.get("tools") or []
        if tools:
            names = [t["function"]["name"] for t in tools]
            pick = next((n for n in names if "final" in n), names[0])
            if CALLS["chat"] % 3 and "write_chapter" in names and pick != "write_chapter" and CALLS["chat"] < 6:
                pick = "write_chapter"
            msg = {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"call_{CALLS['chat']}", "type": "function", "function": {"name": pick, "arguments": "{}"}}]}
        else:
            msg = {"role": "assistant", "content": (
                '{"title": "Mock Story", "summary": "A short mock chapter.", "score": 8, "pass": true, "consistent": true, "confidence": 0.9, "issues": [], "suggestions": []}\n\n'
                "Chapter 1\n\nThe lighthouse keeper found a letter in the tide. " * 3)}
        self._send({"id": "mock", "object": "chat.completion", "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}]})

    def _send(self, obj):
        raw = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def main():
    data_dir = tempfile.mkdtemp(prefix="nc-android-")
    os.environ["NOVELCLAW_DATA_DIR"] = data_dir
    os.environ["APP_INPROCESS_WORKERS"] = "1"
    os.environ["WEB_CLAW_MAX_STEPS"] = "4"
    os.environ["WEB_MAX_ITERATIONS"] = "1"
    os.environ["WEB_MAX_TOTAL_ITERATIONS"] = "2"
    mock_port = free_port()
    mock = ThreadingHTTPServer(("127.0.0.1", mock_port), MockLLM)
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    sys.path.insert(0, str(APPS))
    import android_server

    port = free_port()
    android_server.start(port)
    base = f"http://127.0.0.1:{port}"
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    raw_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect)

    def get(path, follow=True):
        o = opener if follow else raw_opener
        try:
            r = o.open(base + path, timeout=30)
            return r.status, r.read().decode("utf-8", "replace"), r.geturl()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), e.headers.get("Location", "")

    def post(path, form, follow=True):
        o = opener if follow else raw_opener
        data = urllib.parse.urlencode(form).encode()
        try:
            r = o.open(urllib.request.Request(base + path, data=data), timeout=30)
            return r.status, r.read().decode("utf-8", "replace"), r.geturl()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), e.headers.get("Location", "")

    for _ in range(100):
        try:
            if get("/healthz")[0] == 200:
                break
        except Exception:
            time.sleep(0.1)
    failures = []

    def check(name, cond, detail=""):
        print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail[:300]}"))
        if not cond:
            failures.append(name)

    st, body, url = get("/")
    check("root redirects to /settings before configuration", url.endswith("/settings"), url)
    st, body, url = post("/settings", {"base_url": f"http://127.0.0.1:{mock_port}/v1", "api_key": "test-key-123",
                                       "model": "my-uncensored-model:latest", "label": "Mock", "embedding_model": "mock-embed"})
    check("settings saved", st == 200 and "Saved" in body, body)
    st, body, url = post("/settings/test", {})
    check("settings connection test hits mock", "Connection OK" in body, body)
    check("api key + model sent to provider", "Bearer test-key-123" in CALLS["auth"] and "my-uncensored-model:latest" in CALLS["models"], str(CALLS))

    st, body, url = get("/select-mode")
    check("auth-portal auto-login /select-mode", st == 200 and "preview" in body.lower(), body)
    check("mode page links to model settings", 'href="/settings"' in body, body[-500:])
    for path in ["/claw/dashboard", "/claw/console/chat", "/claw/console/models", "/claw/console/workspace",
                 "/claw/console/memory", "/claw/console/status", "/multiagent/dashboard", "/multiagent/jobs",
                 "/multiagent/providers", "/claw/static/style.css", "/multiagent/static/style.css", "/claw/healthz", "/multiagent/healthz"]:
        st, body, url = get(path)
        check(f"GET {path}", st == 200 and "/login" not in urllib.parse.urlsplit(url).path, f"{st} {url} {body[:200]}")
    st, body, url = get("/mode-b", follow=False)
    check("/mode-b links relatively to /claw", url.startswith("/claw"), url)
    st, body, url = get("/claw/dashboard")
    check("custom provider visible in NovelClaw", "Mock" in body or "custom" in body, body[:300])

    # --- NovelClaw job end to end (in-process thread) -----------------------
    calls_before = CALLS["chat"]
    st, body, url = post("/claw/jobs", {"idea": "A lighthouse keeper receives letters from the future.", "provider": "custom"})
    check("NovelClaw job created", st == 200, f"{st} {url}")
    from novelclaw.local_web_portal.app.db import SessionLocal
    from novelclaw.local_web_portal.app.models import GenerationJob

    def wait_job(job_id, timeout=180):
        end = time.time() + timeout
        while time.time() < end:
            with SessionLocal() as db:
                j = db.get(GenerationJob, job_id)
                if j and j.status not in {"queued", "running"}:
                    return j.status, (j.error_message or "")
            time.sleep(0.5)
        return "timeout", ""

    with SessionLocal() as db:
        job_id = max(j.id for j in db.query(GenerationJob).all())

    def run_log(job_id):
        with SessionLocal() as db:
            run_id = db.get(GenerationJob, job_id).run_id
        p = Path(data_dir, "novelclaw", "runs", run_id or "-", "worker.log")
        return p.read_text(errors="replace") if p.exists() else ""

    # The NovelClaw engine pauses after each chapter for user input; reaching
    # that pause (or a terminal state) means the full pipeline ran in-process.
    end = time.time() + 180
    reached = ""
    while time.time() < end:
        with SessionLocal() as db:
            j = db.get(GenerationJob, job_id)
            if j.status not in {"queued", "running"}:
                reached = "terminal:" + j.status + " " + (j.error_message or "")[:200]
                break
        if "Waiting for between-chapter input" in run_log(job_id):
            reached = "between-chapter pause"
            break
        time.sleep(0.5)
    log_text = run_log(job_id)
    print(f"  novelclaw job reached: {reached}  llm_calls={CALLS['chat'] - calls_before}")
    check("NovelClaw job ran pipeline to chapter pause/terminal", bool(reached) and CALLS["chat"] > calls_before, log_text[-800:])
    check("worker.log captured in-process output", "[LLM]" in log_text or "[Claw]" in log_text, log_text[-500:])
    check("chapter text produced", any(Path(data_dir, "novelclaw", "runs").rglob("*.md")) or any(Path(data_dir, "novelclaw", "runs").rglob("*chapter*")), "no chapter files")
    st, body, url = get(f"/claw/jobs/{job_id}")
    check("job detail page renders", st == 200, f"{st} {url}")

    # --- MultiAgent job runs concurrently while NovelClaw job is paused ---------
    calls_before = CALLS["chat"]
    st, body, url = post("/multiagent/jobs", {"idea": "Two rival clockmakers in Prague.", "provider": "custom", "next": "/jobs"})
    check("MultiAgent job created", st == 200 and "/multiagent/jobs/" in url, f"{st} {url}")
    from multiagent.local_web_portal.app.db import SessionLocal as MSession
    from multiagent.local_web_portal.app.models import GenerationJob as MJob
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    mstatus, merr = "timeout", ""
    end = time.time() + (240 if tail.isdigit() else 0)
    while time.time() < end:
        with MSession() as db:
            j = db.get(MJob, int(tail))
            if j and j.status not in {"queued", "running"}:
                mstatus, merr = j.status, j.error_message or ""
                break
        time.sleep(0.5)
    print(f"  multiagent job status={mstatus} llm_calls={CALLS['chat'] - calls_before} err={merr[:300]}")
    check("MultiAgent job ran concurrently against mock", CALLS["chat"] > calls_before and mstatus != "timeout", merr)

    # --- cancellation: paused job, then a job mid-LLM-call ---------------------
    post(f"/claw/jobs/{job_id}/cancel", {})
    with SessionLocal() as db:
        s1 = db.get(GenerationJob, job_id).status
    check("cancel paused NovelClaw job (status=%s)" % s1, s1 in {"canceled", "cancelled"})
    SLOW.set()
    post("/claw/jobs", {"idea": "Cancel me: a long saga of a desert city.", "provider": "custom"})
    time.sleep(2.5)
    with SessionLocal() as db:
        job2 = max(j.id for j in db.query(GenerationJob).all())
    post(f"/claw/jobs/{job2}/cancel", {})
    with SessionLocal() as db:
        status2 = db.get(GenerationJob, job2).status
    check("cancel running NovelClaw job (status=%s)" % status2, status2 in {"canceled", "cancelled"})
    SLOW.clear()
    end = time.time() + 30
    while time.time() < end and any(t.name == "novelclaw-job" for t in threading.enumerate()):
        time.sleep(0.5)
    alive = [t.name for t in threading.enumerate() if t.name == "novelclaw-job"]
    check("cancelled worker threads exited cooperatively", not alive, str(alive))

    # --- vector store / splitter / embeddings --------------------------------
    from novelclaw.config import Config
    from novelclaw.rag.vector_store import VectorStore
    from novelclaw.rag.document_processor import DocumentProcessor
    os.environ.pop("EMBEDDING_API_MODEL", None)
    cfg = Config(require_api_key=False)
    vs = VectorStore(cfg, "smoke", db_path=os.path.join(data_dir, "vs"))
    dp = DocumentProcessor(cfg)
    docs = dp.process_document("The dragon guards the mountain gold. " * 30 + "\n\nA quiet harbor town sells fish at dawn. " * 30, doc_id="d")
    for d in docs:
        d["metadata"] = {"kind": "dragon" if "dragon" in d["text"] else "harbor"}
    vs.add_documents(docs)
    q = dp.get_embeddings(["dragon gold mountain"])[0].tolist()
    res = vs.search(q, top_k=2)
    check("sqlite vector store keyword search", res and "dragon" in res[0]["text"], str(res)[:300])
    res = vs.search(q, top_k=5, filter_metadata={"kind": "harbor"})
    check("metadata filter", res and all(r["metadata"]["kind"] == "harbor" for r in res), str(res)[:200])
    check("collection size", vs.get_collection_size() == len(docs))

    print(f"\nLLM calls to mock: {CALLS['chat']}  data dir: {data_dir}")
    if failures:
        print("FAILED:", failures)
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
