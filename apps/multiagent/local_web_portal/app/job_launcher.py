from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
from pathlib import Path

from inprocess_worker import inprocess_enabled

from .job_runner import run_generation_job
from .settings import BASE_DIR, RUNS_DIR


def _launcher_log_path(job_id: int) -> Path:
    path = RUNS_DIR / "_launchers" / f"job_{job_id}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def start_generation_job_process(job_id: int) -> int:
    if inprocess_enabled():
        # Android/Chaquopy: no detached child processes; run the job in a thread.
        worker = threading.Thread(target=run_generation_job, args=(job_id,), daemon=True, name=f"multiagent-job-{job_id}")
        worker.start()
        return 0
    log_path = _launcher_log_path(job_id)
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "multiagent.local_web_portal.app.job_launcher",
        "--job-id",
        str(job_id),
    ]
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONUTF8", "1")

    with log_path.open("ab") as log_fp:
        kwargs = {
            "cwd": str(BASE_DIR.parent.parent),
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": log_fp,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        else:
            kwargs["start_new_session"] = True

        process = subprocess.Popen(cmd, **kwargs)
        log_fp.write(f"[launcher] spawned pid={process.pid} job_id={job_id}\n".encode("utf-8"))
        log_fp.flush()
        return process.pid


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a generation job in a detached launcher process.")
    parser.add_argument("--job-id", required=True, type=int, help="Generation job id.")
    args = parser.parse_args()
    print(f"[launcher] starting job_id={args.job_id}", flush=True)
    run_generation_job(args.job_id)
    print(f"[launcher] finished job_id={args.job_id}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
