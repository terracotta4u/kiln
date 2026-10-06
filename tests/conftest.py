"""Keep every test off the user's ~/.kiln server."""

import os
import shutil
import signal
import tempfile
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def kiln_home(monkeypatch: pytest.MonkeyPatch) -> Path:
    home = Path(tempfile.mkdtemp(prefix="kiln", dir="/tmp"))
    monkeypatch.setenv("KILN_HOME", str(home))
    yield home
    _stop_server()
    shutil.rmtree(home, ignore_errors=True)


def _stop_server() -> None:
    from kiln.errors import KilnError
    from kiln.server.client import Client, is_running
    from kiln.server.paths import pid_path, socket_path

    if is_running():
        try:
            Client().stop_server(force=True)
        except KilnError:
            pass
        deadline = time.monotonic() + 20
        while is_running() and time.monotonic() < deadline:
            time.sleep(0.05)
    pidfile = pid_path()
    if pidfile.is_file():
        text = pidfile.read_text().strip()
        if text.isdigit():
            try:
                os.kill(int(text), signal.SIGKILL)
            except OSError:
                pass
        pidfile.unlink(missing_ok=True)
    socket_path().unlink(missing_ok=True)
