"""In-memory registry of factory runtimes.

SQLite under each repo's .kiln/ is the durable truth. This process only
remembers which factories it is currently running. If the server disappears,
that memory is gone; nothing here reconstructs it.
"""

import threading
import time
from dataclasses import dataclass
from pathlib import Path

from kiln.agent import kill_live_agents
from kiln.config import load_config
from kiln.db import connect, last_event_id, migrate, record_event, utc_now
from kiln.errors import KilnError
from kiln.tick import run_until_done

RUNNING = "running"
COMPLETED = "completed"
STOPPED = "stopped"
FAILED = "failed"
IDLE = "idle"

_DRAIN_TIMEOUT_SECONDS = 15


@dataclass
class Factory:
    repo_root: Path
    state: str
    stop_reason: str | None
    error: str | None
    started_at: str
    finished_at: str | None
    workers: int | None
    turns: int | None
    stop_event: threading.Event
    thread: threading.Thread | None = None
    rerun_requested: bool = False

    def to_dict(self) -> dict:
        return {
            "repo": str(self.repo_root),
            "state": self.state,
            "stop_reason": self.stop_reason,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "workers": self.workers,
            "turns": self.turns,
        }


class FactoryRuntime:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._factories: dict[Path, Factory] = {}
        self._draining = False

    def start(
        self,
        repo: Path,
        *,
        workers: int | None = None,
        turns: int | None = None,
        agent_bin: str | None = None,
    ) -> tuple[Factory, bool, int]:
        """Start the factory for repo. Returns the factory, whether this call started it, and an event cursor.

        A second start while the same repo is already running returns the existing factory
        and asks it to take another pass before it marks itself completed. That is how a goal
        added at the exit of a run is not left behind. The cursor is the last event id from
        before this run, so a client can stream exactly it.
        """
        repo = repo.resolve()
        cursor = _cursor(repo)
        with self._lock:
            if self._draining:
                raise KilnError("server is shutting down")
            current = self._factories.get(repo)
            if current is not None and current.state == RUNNING:
                current.rerun_requested = True
                return current, False, _cursor(repo)
            factory = Factory(
                repo_root=repo,
                state=RUNNING,
                stop_reason=None,
                error=None,
                started_at=utc_now(),
                finished_at=None,
                workers=workers,
                turns=turns,
                stop_event=threading.Event(),
            )
            thread = threading.Thread(
                target=self._run,
                args=(factory, agent_bin),
                name="kiln-factory",
                daemon=True,
            )
            factory.thread = thread
            self._factories[repo] = factory
        thread.start()
        return factory, True, cursor

    def status(self, repo: Path) -> Factory | None:
        with self._lock:
            return self._factories.get(repo.resolve())

    def factories(self) -> list[Factory]:
        with self._lock:
            return list(self._factories.values())

    def drain(self, timeout: float = _DRAIN_TIMEOUT_SECONDS) -> list[Path]:
        """Stop accepting factories, stop loops, kill agents, and wait.

        Returns repos whose threads were still alive when the timeout expired.
        """
        with self._lock:
            self._draining = True
            running = [factory for factory in self._factories.values() if factory.state == RUNNING]
        for factory in running:
            factory.stop_event.set()
        kill_live_agents()
        deadline = time.monotonic() + timeout
        for factory in running:
            thread = factory.thread
            if thread is None:
                continue
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return [
            factory.repo_root
            for factory in running
            if factory.thread is not None and factory.thread.is_alive()
        ]

    def _run(self, factory: Factory, agent_bin: str | None) -> None:
        conn = None
        try:
            config = load_config(factory.repo_root)
            conn = connect(config.db_path)
            migrate(conn)
            record_event(conn, "factory.started", f"factory started for {factory.repo_root}")
            while True:
                result = run_until_done(
                    conn,
                    config,
                    workers=factory.workers,
                    turns=factory.turns,
                    agent_bin=agent_bin,
                    reporter=lambda line: _persist_line(config.db_path, line),
                    should_stop=factory.stop_event.is_set,
                )
                # The rerun flag and the terminal state change under one lock, so a
                # start_factory that arrives as this pass returns cannot be missed.
                with self._lock:
                    if result.stop_reason is None and factory.rerun_requested:
                        factory.rerun_requested = False
                        continue
                    if result.stop_reason is None:
                        record_event(conn, "factory.completed", "factory completed")
                        self._publish(factory, COMPLETED, None, None)
                    else:
                        record_event(
                            conn,
                            "factory.stopped",
                            f"factory stopped ({result.stop_reason})",
                        )
                        self._publish(factory, STOPPED, result.stop_reason, None)
                    break
        except Exception as exc:
            if conn is not None:
                try:
                    record_event(conn, "factory.failed", f"factory failed: {exc}")
                except Exception:
                    pass
            self._finish(factory, FAILED, None, str(exc))
        finally:
            if conn is not None:
                conn.close()

    def _finish(
        self,
        factory: Factory,
        state: str,
        stop_reason: str | None,
        error: str | None,
    ) -> None:
        with self._lock:
            self._publish(factory, state, stop_reason, error)

    def _publish(
        self,
        factory: Factory,
        state: str,
        stop_reason: str | None,
        error: str | None,
    ) -> None:
        factory.state = state
        factory.stop_reason = stop_reason
        factory.error = error
        factory.finished_at = utc_now()
        factory.rerun_requested = False


def factory_payload(factory: Factory | None, repo: Path) -> dict:
    if factory is None:
        return {
            "repo": str(repo),
            "state": IDLE,
            "stop_reason": None,
            "error": None,
            "started_at": None,
            "finished_at": None,
            "workers": None,
            "turns": None,
        }
    return factory.to_dict()


def _cursor(repo: Path) -> int:
    config = load_config(repo)
    conn = connect(config.db_path)
    try:
        migrate(conn)
        return last_event_id(conn)
    finally:
        conn.close()


def _persist_line(db_path: Path, line: str) -> None:
    """One connection per call. Reporter lines arrive from the factory thread and from dispatch threads."""
    conn = connect(db_path)
    try:
        record_event(conn, "runtime", line)
    finally:
        conn.close()
