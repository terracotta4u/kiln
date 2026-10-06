"""Client for the local Kiln server."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from kiln.errors import KilnError
from kiln.server.paths import log_path, server_home, socket_path
from kiln.server.protocol import (
    EVENTS,
    FACTORY_STATUS,
    PING,
    SERVER_STATUS,
    START_FACTORY,
    STOP_SERVER,
    recv,
    send,
)


class Client:
    def __init__(self, path: Path | None = None):
        self.path = path or socket_path()

    def request(self, message: dict, *, timeout: float | None = None) -> dict:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if timeout is not None:
            sock.settimeout(timeout)
        try:
            try:
                sock.connect(str(self.path))
                send(sock, message)
                reply = recv(sock)
            except KilnError:
                raise
            except OSError as exc:
                raise KilnError("kiln server is not running") from exc
        finally:
            sock.close()
        if not reply.get("ok"):
            raise KilnError(str(reply.get("error") or "kiln server request failed"))
        return reply

    def ping(self) -> dict:
        return self.request({"op": PING}, timeout=1)

    def start_factory(
        self,
        repo: Path,
        *,
        workers: int | None = None,
        turns: int | None = None,
        agent_bin: str | None = None,
    ) -> dict:
        return self.request(
            {
                "op": START_FACTORY,
                "repo": str(repo),
                "workers": workers,
                "turns": turns,
                "agent_bin": agent_bin,
            }
        )

    def factory_status(self, repo: Path) -> dict:
        return self.request({"op": FACTORY_STATUS, "repo": str(repo)})

    def events(self, repo: Path, *, after: int, limit: int = 100) -> dict:
        return self.request({"op": EVENTS, "repo": str(repo), "after": after, "limit": limit})

    def server_status(self) -> dict:
        return self.request({"op": SERVER_STATUS})

    def stop_server(self, *, force: bool = False) -> dict:
        return self.request({"op": STOP_SERVER, "force": force})


def is_running(path: Path | None = None) -> bool:
    """True only when a server answers ping. A leftover socket or pid file does not count."""
    try:
        Client(path).ping()
    except KilnError:
        return False
    return True


def start_detached() -> dict:
    """Launch a server in its own session and return its ping once it answers."""
    if is_running():
        return Client().ping()
    server_home().mkdir(parents=True, exist_ok=True)
    log = log_path().open("ab")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "kiln", "server", "serve"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=os.environ.copy(),
        )
    finally:
        log.close()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if is_running():
            return Client().ping()
        if proc.poll() is not None:
            raise KilnError(f"kiln server exited\n{_log_tail()}".rstrip())
        time.sleep(0.05)
    raise KilnError(f"kiln server did not start (see {log_path()})")


def ensure_running() -> Client:
    if not is_running():
        start_detached()
    return Client()


def _log_tail(lines: int = 20) -> str:
    path = log_path()
    if not path.is_file():
        return f"(no log at {path})"
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(text[-lines:])
