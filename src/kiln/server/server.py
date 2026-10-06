"""The Kiln server process. Unix socket, one JSON request per connection."""

import fcntl
import os
import signal
import socketserver
import sys
import threading
import time
from pathlib import Path

from kiln import __version__
from kiln.config import load_config
from kiln.db import connect, events_after, migrate
from kiln.errors import KilnError
from kiln.jobs import is_open, list_goals, list_jobs
from kiln.server.paths import lock_path, pid_path, socket_path
from kiln.server.protocol import (
    EVENTS,
    FACTORY_STATUS,
    PING,
    SERVER_STATUS,
    START_FACTORY,
    STOP_SERVER,
    error,
    ok,
    recv,
    send,
)
from kiln.server.runtime import FactoryRuntime, factory_payload

_EVENT_LIMIT = 500


class KilnServer(socketserver.ThreadingUnixStreamServer):
    def __init__(self, path: str, runtime: FactoryRuntime, started_at: str):
        self.runtime = runtime
        self.started_at = started_at
        self.pending_stop = False
        self.stop_requested = threading.Event()
        self.did_shutdown = False
        super().__init__(path, _Handler)
        self.daemon_threads = True

    def dispatch(self, message: dict) -> dict:
        op = message.get("op")
        if op == PING:
            return self._ping()
        if op == START_FACTORY:
            return self._start_factory(message)
        if op == FACTORY_STATUS:
            return self._factory_status(message)
        if op == EVENTS:
            return self._events(message)
        if op == SERVER_STATUS:
            return self._server_status()
        if op == STOP_SERVER:
            return self._stop(message)
        raise KilnError(f"unknown op: {op}")

    def _ping(self) -> dict:
        return ok(pid=os.getpid(), version=__version__, started_at=self.started_at)

    def _server_status(self) -> dict:
        return ok(
            pid=os.getpid(),
            version=__version__,
            started_at=self.started_at,
            factories=[factory.to_dict() for factory in self.runtime.factories()],
        )

    def _start_factory(self, message: dict) -> dict:
        repo = _repo(message)
        factory, started, cursor = self.runtime.start(
            repo,
            workers=_optional_int(message, "workers"),
            turns=_optional_int(message, "turns"),
            agent_bin=_optional_str(message, "agent_bin"),
        )
        return ok(started=started, factory=factory.to_dict(), cursor=cursor)

    def _factory_status(self, message: dict) -> dict:
        repo = _repo(message)
        factory = self.runtime.status(repo)
        snapshot = _snapshot(repo)
        return ok(factory=factory_payload(factory, repo), **snapshot)

    def _events(self, message: dict) -> dict:
        repo = _repo(message)
        after = message.get("after", 0)
        limit = message.get("limit", 100)
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise KilnError("after must be an integer >= 0")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise KilnError("limit must be an integer >= 1")
        # State is published after the terminal event is committed, and read
        # before the event query, so a client that sees a finished factory also
        # sees the event that recorded it.
        factory = factory_payload(self.runtime.status(repo), repo)
        config = load_config(repo)
        conn = connect(config.db_path)
        try:
            migrate(conn)
            rows = events_after(conn, after, limit=min(limit, _EVENT_LIMIT))
        finally:
            conn.close()
        return ok(events=[_event_dict(row) for row in rows], factory=factory)

    def _stop(self, message: dict) -> dict:
        force = bool(message.get("force"))
        running = [factory for factory in self.runtime.factories() if factory.state == "running"]
        if running and not force:
            lines = "\n".join(f"  {factory.repo_root}" for factory in running)
            return error(
                "factories are running:\n"
                f"{lines}\n"
                "Stop them with `kiln server stop --force`."
            )
        self.pending_stop = True
        return ok()


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: KilnServer = self.server  # type: ignore[assignment]
        try:
            message = recv(self.request)
            reply = server.dispatch(message)
        except KilnError as exc:
            reply = error(str(exc))
        except Exception as exc:
            reply = error(str(exc))
        try:
            send(self.request, reply)
        except OSError:
            return
        if server.pending_stop:
            server.stop_requested.set()


def serve() -> None:
    """Listen until stop_server, SIGTERM, or SIGINT, then shut down.

    SIGTERM and SIGINT take the same path as stop --force: stop accepting
    factories, ask loops to stop, kill live agents, wait, then remove the socket.
    """
    from kiln.db import utc_now
    from kiln.server.client import is_running

    home = socket_path().parent
    home.mkdir(parents=True, exist_ok=True)
    if is_running():
        return
    lock_fd = os.open(str(lock_path()), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock_fd)
            lock_fd = -1
            _wait_until_running()
            return
        if is_running():
            return
        sock = socket_path()
        if sock.exists():
            sock.unlink()
        runtime = FactoryRuntime()
        server = KilnServer(str(sock), runtime, utc_now())
        pid_path().write_text(f"{os.getpid()}\n")
        thread = threading.Thread(target=server.serve_forever, name="kiln-accept", daemon=True)
        thread.start()
        # A successful ping means serve_forever is inside its loop, so shutdown() cannot deadlock.
        if not _wait_until(_answers_ping):
            socket_path().unlink(missing_ok=True)
            pid_path().unlink(missing_ok=True)
            raise KilnError("kiln server failed to listen")

        def on_signal(_signum: int, _frame: object) -> None:
            server.pending_stop = True
            server.stop_requested.set()

        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        while not server.stop_requested.wait(timeout=0.2):
            pass
        _shutdown(server, runtime)
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)


def _wait_until_running() -> None:
    from kiln.server.client import is_running

    if not _wait_until(is_running):
        raise KilnError("kiln server lock is held but the server is not responding")


def _answers_ping() -> bool:
    from kiln.server.client import Client

    try:
        Client().ping()
    except KilnError:
        return False
    return True


def _wait_until(predicate, timeout: float = 5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _shutdown(server: KilnServer, runtime: FactoryRuntime) -> None:
    if server.did_shutdown:
        return
    server.did_shutdown = True
    try:
        leftovers = runtime.drain()
        if leftovers:
            names = ", ".join(str(path) for path in leftovers)
            print(f"kiln server: factories still running after shutdown: {names}", file=sys.stderr)
        server.shutdown()
        server.server_close()
    finally:
        socket_path().unlink(missing_ok=True)
        pid_path().unlink(missing_ok=True)


def _snapshot(repo: Path) -> dict:
    config = load_config(repo)
    conn = connect(config.db_path)
    try:
        migrate(conn)
        goals = [
            {
                "id": goal.id,
                "status": goal.status.value,
                "title": goal.title,
                "pr_url": goal.pr_url,
            }
            for goal in list_goals(conn)
        ]
        open_jobs = []
        for job in list_jobs(conn):
            if not is_open(job):
                continue
            open_jobs.append(
                {
                    "id": job.id,
                    "role": job.role.value,
                    "status": job.status.value,
                    "title": job.title,
                    "integration": job.integration.value if job.integration else None,
                }
            )
        return {"goals": goals, "open_jobs": open_jobs}
    finally:
        conn.close()


def _event_dict(event) -> dict:
    return {
        "id": event.id,
        "ts": event.ts,
        "kind": event.kind,
        "job_id": event.job_id,
        "run_id": event.run_id,
        "message": event.message,
    }


def _repo(message: dict) -> Path:
    raw = message.get("repo")
    if not isinstance(raw, str) or not raw.strip():
        raise KilnError("repo is required")
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise KilnError(f"not a directory: {path}")
    return path


def _optional_int(message: dict, key: str) -> int | None:
    value = message.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise KilnError(f"{key} must be an integer")
    return value


def _optional_str(message: dict, key: str) -> str | None:
    value = message.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise KilnError(f"{key} must be a string")
    return value
