"""The server keeps a factory running after the client that started it is gone."""

import json
import os
import select
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kiln.cli import _follow, app
from kiln.config import init_factory, load_config
from kiln.db import connect, events_after, record_event
from kiln.jobs import add_goal
from kiln.models import GoalStatus
from kiln.notes import list_notes
from kiln.server.client import Client, ensure_running, is_running
from kiln.server.paths import log_path, pid_path, socket_path
from kiln.server.runtime import FactoryRuntime
from kiln.tick import TickResult

runner = CliRunner()


@pytest.fixture
def factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_server_lifecycle():
    assert "not running" in runner.invoke(app, ["server", "status"]).output

    started = runner.invoke(app, ["server", "start"])
    assert started.exit_code == 0, started.output
    pid = _pid(started.output)
    status = runner.invoke(app, ["server", "status"])
    assert status.exit_code == 0, status.output
    assert f"running (pid {pid}, 0 factories)" in status.output

    again = runner.invoke(app, ["server", "start"])
    assert again.exit_code == 0, again.output
    assert f"already running (pid {pid})" in again.output

    stopped = runner.invoke(app, ["server", "stop"])
    assert stopped.exit_code == 0, stopped.output
    assert "stopped" in stopped.output
    assert "not running" in runner.invoke(app, ["server", "status"]).output
    assert not socket_path().exists()
    assert not pid_path().exists()


def test_a_stale_socket_is_not_a_running_server():
    socket_path().parent.mkdir(parents=True, exist_ok=True)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path()))
    stale.listen(1)
    pid_path().write_text(f"{_dead_pid()}\n")
    try:
        status = runner.invoke(app, ["server", "status"])
        assert status.exit_code == 0, status.output
        assert "not running" in status.output

        started = runner.invoke(app, ["server", "start"])
        assert started.exit_code == 0, started.output
        assert "started" in started.output
        assert is_running()
    finally:
        stale.close()


def test_disconnecting_the_client_does_not_stop_the_factory(factory: Path):
    script = _agent(factory, _slow_note(0.4))
    _add_goal(factory)
    env = os.environ.copy()
    env["KILN_AGENT_BIN"] = str(script)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-m", "kiln", "run", "--turns", "3"],
        cwd=factory,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    seen = _read_until(proc.stdout, lambda text: "runtime" in text or "note #" in text, timeout=20)
    proc.send_signal(signal.SIGINT)
    rest = proc.stdout.read()
    proc.wait(timeout=10)
    output = seen + rest

    assert proc.returncode == 0, output
    assert "detached" in output
    assert is_running()

    state = _wait_factory(factory, lambda factory_state: factory_state["state"] != "running")
    assert state["state"] == "stopped"
    assert state["stop_reason"] == "turn_limit"
    assert len(_notes(factory)) == 3
    assert any(
        event.kind == "factory.stopped" and "turn_limit" in event.message
        for event in _events(factory)
    )


def test_a_finished_goal_completes_and_a_capped_run_stops(factory: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KILN_AGENT_BIN", str(_agent(factory, _done_agent())))
    assert runner.invoke(app, ["goal", "add", "Ship it"]).exit_code == 0
    completed = runner.invoke(app, ["run"])
    assert completed.exit_code == 0, completed.output
    assert "factory completed" in completed.output
    state = Client().factory_status(factory)["factory"]
    assert state["state"] == "completed"
    assert state["stop_reason"] is None
    conn = connect(load_config(factory).db_path)
    try:
        status = conn.execute("SELECT status FROM goals WHERE id = 1").fetchone()["status"]
    finally:
        conn.close()
    assert status == GoalStatus.done.value

    monkeypatch.setenv("KILN_AGENT_BIN", str(_agent(factory, _note_agent())))
    assert runner.invoke(app, ["goal", "add", "Keep going"]).exit_code == 0
    capped = runner.invoke(app, ["run", "--turns", "2"])
    assert capped.exit_code == 0, capped.output
    assert "factory stopped (turn_limit)" in capped.output
    assert Client().factory_status(factory)["factory"]["stop_reason"] == "turn_limit"

    monkeypatch.setenv("KILN_AGENT_BIN", str(_agent(factory, "print('not a report')\n")))
    assert runner.invoke(app, ["goal", "add", "Unreadable"]).exit_code == 0
    failed_foreman = runner.invoke(app, ["run", "--turns", "5"])
    assert failed_foreman.exit_code == 0, failed_foreman.output
    assert "factory stopped (foreman_failures)" in failed_foreman.output


def test_kiln_run_exits_when_the_factory_itself_fails(factory: Path):
    _add_goal(factory)
    failed = runner.invoke(app, ["run", "--workers", "0"])
    assert failed.exit_code == 1, failed.output
    assert "factory failed" in failed.output
    assert "workers must be >= 1" in failed.output
    assert Client().factory_status(factory)["factory"]["state"] == "failed"


def test_a_raised_loop_is_failed(factory: Path, monkeypatch: pytest.MonkeyPatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("kiln.server.runtime.run_until_done", boom)
    runtime = FactoryRuntime()
    running, started, _cursor = runtime.start(factory, turns=1)
    assert started
    assert running.thread is not None
    running.thread.join(5)
    assert running.state == "failed"
    assert running.error == "boom"
    assert any(event.kind == "factory.failed" for event in _events(factory))


def test_a_goal_added_as_the_run_returns_is_taken_on_another_pass(
    factory: Path, monkeypatch: pytest.MonkeyPatch
):
    entered = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def fake_run(*_args, **_kwargs) -> TickResult:
        calls["n"] += 1
        if calls["n"] == 1:
            entered.set()
            assert release.wait(5)
        return TickResult()

    monkeypatch.setattr("kiln.server.runtime.run_until_done", fake_run)
    runtime = FactoryRuntime()
    running, started, _cursor = runtime.start(factory, turns=1)
    assert started
    assert entered.wait(5)
    again, started_again, _again = runtime.start(factory, turns=1)
    assert started_again is False
    assert again is running
    release.set()
    assert running.thread is not None
    running.thread.join(5)

    assert calls["n"] == 2
    assert running.state == "completed"


def test_a_stopped_run_does_not_start_another_pass_for_a_new_goal(
    factory: Path, monkeypatch: pytest.MonkeyPatch
):
    entered = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def fake_run(*_args, **_kwargs) -> TickResult:
        calls["n"] += 1
        entered.set()
        assert release.wait(5)
        result = TickResult()
        result.stop_reason = "turn_limit"
        return result

    monkeypatch.setattr("kiln.server.runtime.run_until_done", fake_run)
    runtime = FactoryRuntime()
    running, started, _cursor = runtime.start(factory, turns=1)
    assert started
    assert entered.wait(5)
    _again, started_again, _cursor_again = runtime.start(factory, turns=1)
    assert started_again is False
    release.set()
    assert running.thread is not None
    running.thread.join(5)

    assert calls["n"] == 1
    assert running.state == "stopped"
    assert running.stop_reason == "turn_limit"


def test_a_follower_reads_events_past_the_first_page(factory: Path, capsys: pytest.CaptureFixture[str]):
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        for index in range(120):
            record_event(conn, "runtime", f"padded {index}")
    finally:
        conn.close()
    client = ensure_running()
    assert client.start_factory(factory, turns=1)["started"] is True
    _wait_factory(factory, lambda factory_state: factory_state["state"] == "completed")

    _follow(client, factory, 0)
    output = capsys.readouterr().out
    assert "padded 0" in output
    assert "padded 119" in output
    assert "factory completed" in output


def test_attach_shows_the_factory_another_client_started(factory: Path):
    script = _agent(factory, _slow_note(0.3))
    _add_goal(factory)
    client = ensure_running()
    started = client.start_factory(factory, turns=3, agent_bin=str(script))
    assert started["started"] is True
    _wait_for_event(factory, "runtime")

    attached = runner.invoke(app, ["attach"])
    assert attached.exit_code == 0, attached.output
    assert "running since" in attached.output
    assert "Ship it" in attached.output
    assert "stopped: reached 3 foreman turns" in attached.output
    assert "factory stopped (turn_limit)" in attached.output


def test_a_second_run_attaches_to_the_factory_already_running(factory: Path):
    script = _agent(factory, _slow_note(0.3))
    _add_goal(factory)
    client = ensure_running()
    first = client.start_factory(factory, turns=2, agent_bin=str(script))
    second = client.start_factory(factory, turns=2, agent_bin=str(script))
    assert first["started"] is True
    assert second["started"] is False
    running = [
        factory_state
        for factory_state in client.server_status()["factories"]
        if factory_state["state"] == "running"
    ]
    assert len(running) == 1
    assert Path(running[0]["repo"]) == factory.resolve()

    attached = runner.invoke(app, ["run", "--turns", "9"])
    assert attached.exit_code == 0, attached.output
    assert "already running" in attached.output
    assert "factory stopped (turn_limit)" in attached.output
    assert len(_notes(factory)) == 2


def test_runtime_starts_one_loop_per_repository(factory: Path):
    script = _agent(factory, _slow_note(0.3))
    _add_goal(factory)
    runtime = FactoryRuntime()
    first, started, _cursor = runtime.start(factory, turns=2, agent_bin=str(script))
    second, started_again, _again = runtime.start(factory, turns=2, agent_bin=str(script))
    assert started is True
    assert started_again is False
    assert first is second
    assert first.thread is not None
    first.thread.join(20)
    assert len(_notes(factory)) == 2


def test_two_watchers_see_the_same_factory(factory: Path):
    script = _agent(factory, _slow_note(0.2))
    _add_goal(factory)
    client = ensure_running()
    assert client.start_factory(factory, turns=2, agent_bin=str(script))["started"] is True
    seen: list[list[str]] = [[], []]

    def watch(bucket: list[str]) -> None:
        cursor = 0
        while True:
            page = Client().events(factory, after=cursor)
            for event in page["events"]:
                bucket.append(event["message"])
                cursor = event["id"]
            if page["factory"]["state"] != "running":
                bucket.append(page["factory"]["state"])
                return
            time.sleep(0.05)

    threads = [threading.Thread(target=watch, args=(bucket,)) for bucket in seen]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert all(not thread.is_alive() for thread in threads), seen
    assert all(bucket[-1] == "stopped" for bucket in seen), seen
    assert all(any("reached 2 foreman turns" in message for message in bucket) for bucket in seen), seen


def test_lifecycle_commands_refuse_while_a_factory_is_running(factory: Path):
    script = _agent(factory, _slow_note(0.4))
    assert runner.invoke(app, ["goal", "add", "Ship it"]).exit_code == 0
    assert runner.invoke(app, ["job", "add", "1", "Wait"]).exit_code == 0
    client = ensure_running()
    assert client.start_factory(factory, turns=4, agent_bin=str(script))["started"] is True
    _wait_factory(factory, lambda factory_state: factory_state["state"] == "running")

    for args in (["review", "1", "--approve"], ["job", "cancel", "1"], ["work"], ["gc"]):
        refused = runner.invoke(app, args)
        assert refused.exit_code != 0, refused.output
        assert "a factory is running" in refused.output

    assert runner.invoke(app, ["goal", "add", "Another"]).exit_code == 0
    assert runner.invoke(app, ["job", "list"]).exit_code == 0
    assert runner.invoke(app, ["log"]).exit_code == 0
    dashboard = runner.invoke(app, ["status"])
    assert dashboard.exit_code == 0, dashboard.output
    assert "factory running since" in dashboard.output

    stopped = runner.invoke(app, ["server", "stop", "--force"])
    assert stopped.exit_code == 0, stopped.output
    cancelled = runner.invoke(app, ["job", "cancel", "1"])
    assert cancelled.exit_code == 0, cancelled.output
    assert "cancelled #1" in cancelled.output


def test_stop_refuses_while_a_factory_runs_unless_forced(factory: Path):
    script = _agent(factory, _slow_note(0.5))
    _add_goal(factory)
    client = ensure_running()
    assert client.start_factory(factory, turns=5, agent_bin=str(script))["started"] is True
    _wait_for_event(factory, "factory.started")

    refused = runner.invoke(app, ["server", "stop"])
    assert refused.exit_code != 0, refused.output
    assert str(factory.resolve()) in refused.output
    assert is_running()

    forced = runner.invoke(app, ["server", "stop", "--force"])
    assert forced.exit_code == 0, forced.output
    assert not is_running()
    assert not socket_path().exists()
    assert any(
        event.kind == "factory.stopped" and "server_shutdown" in event.message
        for event in _events(factory)
    )


def test_sigterm_kills_agents_and_shuts_the_server_down(factory: Path):
    script = _agent(factory, _sleeping_agent())
    _add_goal(factory)
    client = ensure_running()
    assert client.start_factory(factory, turns=2, agent_bin=str(script))["started"] is True
    agent_pid = _wait_for_pid(factory / "agent.pid")
    server_pid = int(pid_path().read_text().strip())

    os.kill(server_pid, signal.SIGTERM)
    assert _gone(server_pid), _server_log()
    assert _gone(agent_pid), _server_log()
    assert not socket_path().exists()
    assert not pid_path().exists()
    assert any(
        event.kind == "factory.stopped" and "server_shutdown" in event.message
        for event in _events(factory)
    )

    restarted = runner.invoke(app, ["server", "start"])
    assert restarted.exit_code == 0, restarted.output
    assert "started" in restarted.output


def test_one_server_runs_two_repositories(tmp_path: Path):
    first = tmp_path / "one"
    second = tmp_path / "two"
    _init_repo(first)
    _init_repo(second)
    _add_goal(first)
    _add_goal(second)
    client = ensure_running()
    assert client.start_factory(first, turns=1, agent_bin=str(_agent(first, _slow_note(0.4))))["started"]
    assert client.start_factory(second, turns=1, agent_bin=str(_agent(second, _slow_note(0.4))))["started"]

    running = {
        Path(factory_state["repo"])
        for factory_state in client.server_status()["factories"]
        if factory_state["state"] == "running"
    }
    assert running == {first.resolve(), second.resolve()}

    _wait_factory(first, lambda factory_state: factory_state["state"] == "stopped")
    _wait_factory(second, lambda factory_state: factory_state["state"] == "stopped")
    assert len(_notes(first)) == 1
    assert len(_notes(second)) == 1


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "kiln@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Kiln"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    init_factory(path)


def _add_goal(repo: Path, title: str = "Ship it") -> None:
    config = load_config(repo)
    conn = connect(config.db_path)
    try:
        add_goal(conn, title)
    finally:
        conn.close()


def _events(repo: Path):
    config = load_config(repo)
    conn = connect(config.db_path)
    try:
        return events_after(conn, 0, limit=500)
    finally:
        conn.close()


def _notes(repo: Path, goal_id: int = 1):
    config = load_config(repo)
    conn = connect(config.db_path)
    try:
        return list_notes(conn, goal_id)
    finally:
        conn.close()


def _wait_for_event(repo: Path, kind: str, timeout: float = 20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = _events(repo)
        if any(event.kind == kind for event in events):
            return events
        time.sleep(0.05)
    raise AssertionError(f"no {kind} event\n{_server_log()}")


def _wait_factory(repo: Path, predicate, timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = Client().factory_status(repo)["factory"]
        if predicate(last):
            return last
        time.sleep(0.05)
    raise AssertionError(f"{last}\n{_server_log()}")


def _wait_for_pid(path: Path, timeout: float = 20) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.read_text().strip().isdigit():
            return int(path.read_text().strip())
        time.sleep(0.05)
    raise AssertionError(f"no pid at {path}\n{_server_log()}")


def _read_until(stream, predicate, timeout: float) -> str:
    chunks: list[str] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _unused, _also = select.select([stream], [], [], 0.2)
        if not ready:
            continue
        line = stream.readline()
        if not line:
            break
        chunks.append(line)
        if predicate("".join(chunks)):
            return "".join(chunks)
    raise AssertionError("".join(chunks) or _server_log())


def _gone(pid: int, timeout: float = 20) -> bool:
    """True once pid has exited. Reaps it if this process is the parent.

    A server started by the test is a child of pytest. After it exits, kill(pid, 0)
    still succeeds until something waits on it.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            waited = 0
        if waited == pid:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _server_log() -> str:
    path = log_path()
    if not path.is_file():
        return "(no server log)"
    return path.read_text(encoding="utf-8", errors="replace")


def _pid(text: str) -> str:
    for token in text.split():
        digits = token.strip("().")
        if digits.isdigit():
            return digits
    raise AssertionError(text)


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _note_agent() -> str:
    return (
        "import sys\n"
        "prompt = sys.argv[-1]\n"
        "if 'You are the Kiln foreman' in prompt:\n"
        f"    {_emit_call(json.dumps({'actions': [{'type': 'note', 'text': 'still going'}]}))}\n"
    )


def _slow_note(seconds: float) -> str:
    return (
        "import sys, time\n"
        f"time.sleep({seconds})\n"
        + _note_agent()
    )


def _done_agent() -> str:
    payload = json.dumps({"actions": [{"type": "goal_done", "evidence": ["shipped"]}]})
    return (
        "import sys\n"
        "prompt = sys.argv[-1]\n"
        "if 'You are the Kiln foreman' in prompt:\n"
        f"    {_emit_call(payload)}\n"
    )


def _sleeping_agent() -> str:
    return (
        "import os, time\n"
        "from pathlib import Path\n"
        "Path('agent.pid').write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )


def _emit_call(payload: str) -> str:
    return "print(" + json.dumps(_envelope(payload)) + ")"


def _envelope(result_body: str) -> str:
    text = "```json\n" + result_body + "\n```"
    return json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": text}
    )


def _agent(directory: Path, body: str) -> Path:
    path = directory / "fake-agent"
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path
