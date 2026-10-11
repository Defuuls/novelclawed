"""Local web portal root package."""


# The engine modules are imported as "<app>.config", "<app>.rag.*", ... so that
# both workspaces can share one Python process (Android build). Keep the classic
# "cd apps/<app> && uvicorn local_web_portal.app.main:app" launch working by
# putting apps/ on sys.path.
import os as _os
import sys as _sys

_APPS_DIR = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
if _APPS_DIR not in _sys.path:
    _sys.path.insert(0, _APPS_DIR)
