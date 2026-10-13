"""Desktop entry point for Deerstalker.

The FastAPI app stays local to this machine and is served to a pywebview
window. Run ``python main.py`` after installing ``requirements.txt``.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import uvicorn

from app.config import Settings, get_settings
from app.log import logger


_HOST = "127.0.0.1"
_READINESS_TIMEOUT_SECONDS = 15.0
_WINDOW_TITLE = "Deerstalker - Local File Investigation"


def ensure_elevated() -> bool:
    """Re-launch the desktop app with Windows administrator approval.

    ``starter.bat`` already does this for the normal one-click path.  Keeping
    the same safeguard here also covers people who start ``python main.py``
    directly, so connected storage and protected folders receive the same
    access attempt either way.  The original, unelevated process returns
    after it successfully launches its elevated replacement.
    """
    if sys.platform != "win32":
        return True
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return True
        arguments = subprocess.list2cmdline([os.path.abspath(__file__), *sys.argv[1:]])
        result = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            sys.executable,
            arguments,
            os.getcwd(),
            1,
        )
    except Exception as exc:  # pragma: no cover - Windows integration path
        raise RuntimeError("Deerstalker needs administrator approval to start.") from exc
    if result <= 32:  # User cancelled UAC or Windows could not start Python.
        raise RuntimeError("Administrator approval was not granted; Deerstalker did not start.")
    return False


def start_backend(settings: Settings) -> tuple[uvicorn.Server, threading.Thread]:
    """Start the local FastAPI server in a daemon thread."""
    config = uvicorn.Config(
        "app.api:app",
        host=_HOST,
        port=settings.app_port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run,
        daemon=True,
        name="deerstalker-api",
    )
    thread.start()
    return server, thread


def wait_for_backend(port: int, timeout_seconds: float = _READINESS_TIMEOUT_SECONDS) -> bool:
    """Return whether the local API responds before ``timeout_seconds``."""
    deadline = time.monotonic() + timeout_seconds
    url = f"http://{_HOST}:{port}/api/roots"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if 200 <= response.status < 300:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.15)
    return False


def stop_backend(server: uvicorn.Server, thread: threading.Thread) -> None:
    """Ask Uvicorn to stop and give its thread a short graceful window."""
    server.should_exit = True
    thread.join(timeout=5.0)
    if thread.is_alive():
        server.force_exit = True
        thread.join(timeout=1.0)


def run() -> None:
    """Start the local service, show the desktop window, and clean up."""
    try:
        import webview
    except ImportError as exc:  # pragma: no cover - installation issue
        raise RuntimeError("pywebview is required. Run: python -m pip install -r requirements.txt") from exc

    settings = get_settings()
    server, thread = start_backend(settings)
    if not wait_for_backend(settings.app_port):
        stop_backend(server, thread)
        raise RuntimeError(
            f"Deerstalker could not start its local server on port {settings.app_port}. "
            "Choose a free APP_PORT in .env and try again."
        )

    def on_window_closed(*_args: object) -> None:
        server.should_exit = True

    try:
        window = webview.create_window(
            _WINDOW_TITLE,
            f"http://localhost:{settings.app_port}",
            min_size=(900, 650),
        )
        window.events.closed += on_window_closed
        webview.start()
    finally:
        stop_backend(server, thread)
        logger.info("Deerstalker desktop shell closed")


if __name__ == "__main__":
    if ensure_elevated():
        run()
