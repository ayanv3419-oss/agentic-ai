"""Agentic AI — single-command launcher.

Usage:
    python main.py

Starts the FastAPI backend (uvicorn) and the React/Vite frontend (npm) as
parallel subprocesses, streams their output with [backend]/[frontend] prefixes,
and shuts both down cleanly on Ctrl+C. Stdlib only.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
BACKEND_DIR = ROOT / "backend"
FRONTEND_DIR = ROOT / "frontend"

IS_WINDOWS = os.name == "nt"

BACKEND_CMD = ["uvicorn", "backend.main:app", "--port", "8000", "--reload"]
FRONTEND_CMD = ["npm", "run", "dev"]

SHUTDOWN_TIMEOUT = 6.0
POLL_INTERVAL = 0.5


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def log(msg: str, *, error: bool = False) -> None:
    tag = _c("[launcher]", "31" if error else "36")
    print(f"{tag} {msg}", flush=True)


# ---------------------------------------------------------------------------
# Subprocess control
# ---------------------------------------------------------------------------

def _popen(cmd: list[str], *, cwd: Path, use_shell: bool = False) -> subprocess.Popen:
    """Launch a child with a new process group so we can signal it cleanly."""
    # On Windows, CREATE_NEW_PROCESS_GROUP lets us send CTRL_BREAK_EVENT.
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0  # type: ignore[attr-defined]
    # On Unix, start_new_session=True puts the child in its own group so a
    # Ctrl+C in our terminal does not also hit the children directly.
    return subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=use_shell,
        creationflags=creationflags,
        start_new_session=not IS_WINDOWS,
    )


def start_backend() -> subprocess.Popen:
    log("Starting backend...")
    if not (BACKEND_DIR / "main.py").exists():
        raise FileNotFoundError(f"backend/main.py not found at {BACKEND_DIR}")
    return _popen(BACKEND_CMD, cwd=ROOT)


def start_frontend() -> subprocess.Popen:
    log("Starting frontend...")
    if not (FRONTEND_DIR / "package.json").exists():
        raise FileNotFoundError(f"frontend/package.json not found at {FRONTEND_DIR}")
    if not (FRONTEND_DIR / "node_modules").exists():
        log("frontend/node_modules missing - run `npm install` in /frontend first.", error=True)
    # npm on Windows is npm.cmd; shell=True is the simplest portable launch.
    return _popen(FRONTEND_CMD, cwd=FRONTEND_DIR, use_shell=IS_WINDOWS)


def stream_output(proc: subprocess.Popen, tag: str, color: str) -> None:
    """Forward a child's stdout line-by-line with a prefix."""
    if proc.stdout is None:
        return
    prefix = _c(tag, color)
    try:
        for raw in proc.stdout:
            line = raw.rstrip()
            if line:
                print(f"{prefix} {line}", flush=True)
    except Exception:
        pass  # pipe closed during shutdown


def shutdown(proc: Optional[subprocess.Popen], name: str) -> None:
    """Stop a child process (and its tree) cleanly. Never raises."""
    if proc is None or proc.poll() is not None:
        return
    log(f"Stopping {name}...")

    try:
        if IS_WINDOWS:
            proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        else:
            proc.terminate()
    except Exception:
        pass

    try:
        proc.wait(timeout=SHUTDOWN_TIMEOUT)
        return
    except subprocess.TimeoutExpired:
        pass

    # Escalate: kill the whole process tree.
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            proc.kill()
    except Exception:
        pass

    try:
        proc.wait(timeout=2.0)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main() -> int:
    backend: Optional[subprocess.Popen] = None
    frontend: Optional[subprocess.Popen] = None
    exit_code = 0

    try:
        try:
            backend = start_backend()
        except FileNotFoundError as e:
            log(f"ERROR: backend not started: {e}", error=True)
            return 1
        except OSError as e:
            log(f"ERROR: could not launch uvicorn ({e}). "
                f"Try: pip install -r backend/requirements.txt", error=True)
            return 1

        try:
            frontend = start_frontend()
        except FileNotFoundError as e:
            log(f"ERROR: frontend not started: {e}", error=True)
            shutdown(backend, "backend")
            return 1
        except OSError as e:
            log(f"ERROR: could not launch npm ({e}). "
                f"Install Node.js and run `npm install` in /frontend.", error=True)
            shutdown(backend, "backend")
            return 1

        threading.Thread(target=stream_output, args=(backend, "[backend]", "34"), daemon=True).start()
        threading.Thread(target=stream_output, args=(frontend, "[frontend]", "32"), daemon=True).start()

        log("System running.  backend -> http://localhost:8000   "
            "frontend -> http://localhost:5173   (Ctrl+C to stop)")

        # Watchdog: bail out if either child dies; KeyboardInterrupt breaks here too.
        while True:
            be = backend.poll()
            fe = frontend.poll()
            if be is not None:
                log(f"Backend exited with code {be}; shutting down frontend.", error=True)
                exit_code = be or 1
                break
            if fe is not None:
                log(f"Frontend exited with code {fe}; shutting down backend.", error=True)
                exit_code = fe or 1
                break
            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        log("Ctrl+C received - shutting down...")
        exit_code = 0
    finally:
        shutdown(frontend, "frontend")
        shutdown(backend, "backend")

    log(f"Bye (exit {exit_code}).")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
