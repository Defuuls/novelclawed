# NovelClaw on Android (single APK, everything on the phone)

The Android app runs the whole NovelClaw stack on the device:

* **Python 3.11 via [Chaquopy](https://chaquo.com/chaquopy/)** embedded in a small Kotlin app.
* A **foreground service** (`ServerService`) starts `apps/android_server.py`, which
  serves all three former services from **one FastAPI app on `127.0.0.1:8765`**
  (plain uvicorn + h11, no uvloop/httptools):

  | Path            | Was                         |
  |-----------------|-----------------------------|
  | `/`             | auth-portal (:8010) - single-user auto-login, mode selection |
  | `/multiagent/…` | MultiAgent workspace (:8011) |
  | `/claw/…`       | NovelClaw workspace (:8012)  |
  | `/settings`     | new: on-device model settings |

* A full-screen **WebView** (`MainActivity`) shows the UI. Exports/downloads are
  saved to the phone's Downloads folder.
* All state (one shared SQLite DB `novelclaw.db`, runs, vector store, secrets,
  `llm_settings.json`) lives in the app's private storage
  (`context.filesDir/novelclaw`, passed to Python as `NOVELCLAW_DATA_DIR`).
* Only the LLM is remote: the phone needs network access to your
  OpenAI-compatible endpoint.

## Configuring your model

On first launch the app opens **Model settings** (`/settings`). You can come back
to it any time from the **Model settings** link on the mode-selection page.

* **Base URL** – any OpenAI-compatible API root, e.g.
  `https://openrouter.ai/api/v1`, `http://192.168.1.20:11434/v1` (Ollama on your
  LAN), `http://192.168.1.20:8080/v1` (llama.cpp / vLLM / LM Studio), or a hosted
  provider.
* **API key** – stored only on the device; leave empty for servers without auth.
* **Model name** – the exact model id your endpoint expects. Nothing is
  hard-coded and there is no model allow-list.
* **API style** – `/chat/completions` (default) or `/responses`.
* **Embedding model** (optional) – if set, retrieval uses `/embeddings` on the
  same endpoint; otherwise a keyword (hashed bag-of-words) fallback is used.
* **Test connection** sends a one-line prompt to verify the settings.

The saved model appears as the provider **custom** ("My model" / your display
name) in both workspaces and is the default for new jobs. You can still add
more providers and per-provider keys in each workspace's *Models/Providers*
page as before.

## Building the debug APK (Linux, no device needed)

Requirements:

* JDK 17
* Android SDK command-line tools with `platforms;android-34` and
  `build-tools;34.0.0` (`sdkmanager` accepts licenses with `yes |`)
* A host **Python 3.11** with `pip` (Chaquopy uses it at build time)
* Network access (Gradle, Maven, PyPI and `chaquo.com/pypi-13.1` wheels)

```bash
# one-time SDK setup (example paths)
export JAVA_HOME=/path/to/jdk-17
export ANDROID_HOME=$HOME/android-sdk
mkdir -p $ANDROID_HOME/cmdline-tools
# unzip commandlinetools-linux-*_latest.zip so that
# $ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager exists, then:
yes | $ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager \
    "platform-tools" "platforms;android-34" "build-tools;34.0.0"

cd android
echo "sdk.dir=$ANDROID_HOME" > local.properties
./gradlew assembleDebug -PchaquopyBuildPython=$(command -v python3.11)
# -> android/app/build/outputs/apk/debug/app-debug.apk (~70 MB, arm64-v8a + x86_64)
```

Install with `adb install -r app/build/outputs/apk/debug/app-debug.apk` or copy
the APK to the phone and open it (allow "install unknown apps").

The Gradle build stages the Python sources from `apps/` into
`android/app/build/python-src` (auth-portal's `local_web_portal` becomes the
top-level `local_web_portal` package) and pip-installs
`requirements-android.txt` for both ABIs. The Chaquopy free license shows no
nag in debug builds; see Chaquopy's license terms for release builds.

## What changed for Android (and why)

| Original                          | Android replacement |
|-----------------------------------|---------------------|
| `openai` SDK (needs pydantic v2 / pydantic-core: no Android wheel) | `utils/openai_compat.py`, stdlib `urllib` client for `/chat/completions`, `/responses`, `/embeddings` (tool calls supported) |
| pydantic v2                       | pydantic 1.10 pure-Python wheel; FastAPI 0.116 still supports v1, so no API changes were needed |
| `uvicorn[standard]`               | plain `uvicorn` with `h11` + asyncio |
| ChromaDB                          | `rag/vector_store.py`: SQLite + numpy cosine search, Chroma-style metadata filters |
| sentence-transformers / torch     | remote embeddings via the configured endpoint, keyword fallback (`rag/embeddings.py`) |
| langchain text splitter           | `rag/text_splitter.py` (inlined recursive splitter) |
| tiktoken, datasets                | dropped (tiktoken was unused; datasets only for optional offline imports) |
| 3 uvicorn services + Docker       | one process, mounted sub-apps, relative links, one SQLite DB |
| `subprocess.Popen` workers        | `apps/inprocess_worker.py`: jobs run in threads, per-job env overlay, log capture, cooperative cancel (checked on output, before every LLM request and in wait loops) |

Desktop/Docker deployments keep working unchanged: the engines are now imported
as `novelclaw.*` / `multiagent.*` packages, and each `local_web_portal/__init__.py`
puts `apps/` on `sys.path`, so `cd apps/novelclaw && uvicorn local_web_portal.app.main:app`
still works. You can also run the single-process server on desktop:

```bash
pip install -r requirements-android.txt
cd apps && NOVELCLAW_DATA_DIR=/tmp/novelclaw python android_server.py --port 8765
# open http://127.0.0.1:8765/
python tests/android_smoke_test.py   # mocked-LLM end-to-end smoke test
```

## Known limitations

* A cancelled job stops at its next checkpoint (next log line, next LLM request
  or wait-loop tick); an HTTP request already in flight finishes first (bounded
  by the timeout on the settings page, default 600 s).
* Android may still stop the app under aggressive battery optimisation; the
  foreground service + partial wake lock keep long runs alive in normal cases.
  Consider exempting NovelClaw from battery optimisation for very long books.
* Both workspaces share one SQLite database (same `users`/`generation_jobs`
  tables), so a job started in one workspace is also listed in the other.
* Static RAG / knowledge-base features that need a local embedding model use
  the remote `/embeddings` endpoint or keyword retrieval instead.
* Windows helper files (`*.bat`, `*.ps1`, `ISTask.dll`) are not used or
  packaged by the Android build.
