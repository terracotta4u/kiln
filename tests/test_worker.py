import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kiln.cli import app
from kiln.config import init_factory, load_config
from kiln.db import connect
from kiln.errors import KilnError
from kiln.jobs import add_dependency, add_goal, add_job, get_job
from kiln.models import Integration, JobStatus, RunStatus
from kiln.roles.worker import run_worker

runner = CliRunner()


@pytest.fixture
def factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "kiln@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Kiln"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    monkeypatch.chdir(tmp_path)
    init_factory(tmp_path)
    return tmp_path


def test_worker_commits_on_a_branch_and_leaves_the_task_in_review(factory: Path, monkeypatch):
    capture = factory / "args"
    monkeypatch.setenv("KILN_CAPTURE_ARGS", str(capture))
    script = _agent(factory, _write_and_report("added hello", "hello.txt"))
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        task = add_job(conn, goal.id, "Add hello", acceptance="file exists")
        conn.execute("UPDATE jobs SET feedback = ? WHERE id = ?", ("try again", task.id))
        outcome = run_worker(conn, config, agent_bin=str(script))
        stored = get_job(conn, task.id)
    finally:
        conn.close()

    assert outcome.failure is None
    assert outcome.summary == "added hello"
    assert stored is not None
    assert stored.status == JobStatus.completed
    assert stored.integration == Integration.pending
    result = json.loads(stored.result or "")
    assert result["summary"] == "added hello"
    assert result["artifacts"] == [{"type": "branch", "name": "kiln/1-add-hello"}]
    assert result["role_result"]["files"] == ["hello.txt"]
    assert result["role_result"]["committed"] is True
    assert stored.attempts == 1
    assert stored.branch == "kiln/1-add-hello"
    assert "hello.txt" in outcome.diffstat
    assert not (factory / "hello.txt").exists()
    worktree = factory / ".kiln" / "worktrees" / "1" / "hello.txt"
    assert worktree.read_text() == "hello\n"
    assert _subject(factory, stored.branch or "") == "kiln: task #1 Add hello"
    assert _file_on_branch(factory, "main", "hello.txt") is False
    args = capture.read_text().splitlines()
    assert "--force" in args
    assert "--trust" in args
    assert "--mode" not in args
    assert "claude-sonnet-5-thinking-high" in args
    log = Path(outcome.run.log_path or "").read_text()
    assert "Add hello" in log
    assert "try again" in log
    assert outcome.run.status == RunStatus.succeeded


def test_worker_keeps_a_commit_the_agent_already_made(factory: Path):
    script = _agent(
        factory,
        "import subprocess\n"
        "from pathlib import Path\n"
        "Path('hello.txt').write_text('hello\\n')\n"
        "subprocess.run(['git', 'add', 'hello.txt'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "subprocess.run(['git', 'commit', '-m', 'agent commit'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        + _print_report("added hello", ["hello.txt"]),
    )
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        task = add_job(conn, goal.id, "Add hello")
        outcome = run_worker(conn, config, task_id=task.id, agent_bin=str(script))
    finally:
        conn.close()

    assert outcome.failure is None
    assert _subject(factory, outcome.job.branch or "") == "agent commit"
    assert _commit_count(factory, outcome.job.branch or "") == 1


def test_verify_failure_still_reaches_review(factory: Path):
    _set_verify(factory, "echo nope && exit 3")
    script = _agent(factory, _write_and_report("added hello", "hello.txt"))
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        task = add_job(conn, goal.id, "Add hello")
        outcome = run_worker(conn, config, agent_bin=str(script))
    finally:
        conn.close()

    assert outcome.job.status == JobStatus.completed
    assert outcome.job.integration == Integration.pending
    result = json.loads(outcome.job.result or "")
    assert "verify exited 3" in result["evidence"][0]
    assert result["role_result"]["verify"]["exit_code"] == 3
    assert outcome.run.status == RunStatus.failed
    assert outcome.failure is not None
    assert "verify exited 3" in outcome.failure
    assert "nope" in outcome.failure
    assert "hello.txt" in outcome.diffstat


def test_missing_report_still_commits(factory: Path):
    script = _agent(
        factory,
        "from pathlib import Path\n"
        "Path('hello.txt').write_text('hello\\n')\n"
        "print('nope')\n",
    )
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        add_job(conn, goal.id, "Add hello")
        outcome = run_worker(conn, config, agent_bin=str(script))
    finally:
        conn.close()

    assert outcome.failure == "response had no JSON report"
    assert outcome.job.status == JobStatus.completed
    assert outcome.job.integration == Integration.pending
    result = json.loads(outcome.job.result or "")
    assert result["summary"] == f"committed {outcome.job.branch}"
    assert result["evidence"] == ["response had no JSON report"]
    assert _commit_count(factory, outcome.job.branch or "") == 1


def test_a_worker_that_does_not_commit_fails(factory: Path):
    script = _agent(factory, _print_report("nothing to change", []))
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        task = add_job(conn, goal.id, "Add hello")
        outcome = run_worker(conn, config, task_id=task.id, agent_bin=str(script))
    finally:
        conn.close()

    assert outcome.failure == "no commit was produced"
    assert outcome.job.status == JobStatus.failed
    assert outcome.job.integration is None
    assert outcome.job.result is None
    assert outcome.run.status == RunStatus.failed


def test_exhausted_task_is_failed_and_the_next_ready_task_is_taken(factory: Path):
    script = _agent(factory, _write_and_report("did the low one", "low.txt"))
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        high = add_job(conn, goal.id, "High", priority=10)
        low = add_job(conn, goal.id, "Low", priority=1)
        conn.execute("UPDATE jobs SET attempts = max_attempts WHERE id = ?", (high.id,))
        outcome = run_worker(conn, config, agent_bin=str(script))
        failed = get_job(conn, high.id)
    finally:
        conn.close()

    assert failed is not None
    assert failed.status == JobStatus.failed
    assert outcome.job.id == low.id
    assert outcome.job.status == JobStatus.completed


def test_only_exhausted_task_is_failed(factory: Path):
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        task = add_job(conn, goal.id, "High")
        conn.execute("UPDATE jobs SET attempts = max_attempts WHERE id = ?", (task.id,))
        with pytest.raises(KilnError, match="exhausted"):
            run_worker(conn, config, agent_bin=str(factory / "missing"))
        stored = get_job(conn, task.id)
    finally:
        conn.close()

    assert stored is not None
    assert stored.status == JobStatus.failed
    assert not (factory / ".kiln" / "worktrees").exists()


def test_blocked_task_is_not_claimed(factory: Path):
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        first = add_job(conn, goal.id, "First")
        second = add_job(conn, goal.id, "Second")
        add_dependency(conn, second.id, first.id)
        with pytest.raises(KilnError, match="not ready"):
            run_worker(conn, config, task_id=second.id, agent_bin=str(factory / "missing"))
        stored = get_job(conn, second.id)
    finally:
        conn.close()

    assert stored is not None
    assert stored.status == JobStatus.pending


def test_missing_base_branch_releases_the_claim(factory: Path):
    toml = (factory / "kiln.toml").read_text().replace(
        'base_branch = "main"',
        'base_branch = "missing"',
    )
    (factory / "kiln.toml").write_text(toml)
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        task = add_job(conn, goal.id, "Add hello")
        with pytest.raises(KilnError):
            run_worker(conn, config, agent_bin=str(factory / "missing"))
        stored = get_job(conn, task.id)
    finally:
        conn.close()

    assert stored is not None
    assert stored.status == JobStatus.pending
    assert stored.attempts == 0
    assert stored.claimed_by is None


def test_cli_work(factory: Path, monkeypatch: pytest.MonkeyPatch):
    script = _agent(factory, _write_and_report("from the cli", "hello.txt"))
    monkeypatch.setenv("KILN_AGENT_BIN", str(script))
    assert runner.invoke(app, ["goal", "add", "Ship it"]).exit_code == 0
    assert runner.invoke(app, ["job", "add", "1", "Add hello"]).exit_code == 0
    result = runner.invoke(app, ["work"])
    assert result.exit_code == 0, result.output
    assert "completed" in result.output
    assert "from the cli" in result.output
    assert "hello.txt" in result.output
    again = runner.invoke(app, ["work"])
    assert again.exit_code != 0
    assert "no ready job" in again.output


def _write_and_report(summary: str, filename: str) -> str:
    return (
        "from pathlib import Path\n"
        f"Path({filename!r}).write_text('hello\\n')\n"
        + _print_report(summary, [filename])
    )


def _print_report(summary: str, files: list[str]) -> str:
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "```json\n" + json.dumps({"summary": summary, "files": files}) + "\n```",
    }
    saving = (
        "import os, pathlib, sys\n"
        "path = os.environ.get('KILN_CAPTURE_ARGS')\n"
        "if path:\n"
        "    pathlib.Path(path).write_text('\\n'.join(sys.argv[1:]))\n"
    )
    return saving + "print(" + json.dumps(json.dumps(envelope)) + ")\n"


def _agent(directory: Path, body: str) -> Path:
    path = directory / "fake-agent"
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _set_verify(repo: Path, command: str) -> None:
    path = repo / "kiln.toml"
    text = path.read_text().replace('verify = ""', f"verify = {json.dumps(command)}", 1)
    path.write_text(text)


def _subject(repo: Path, branch: str) -> str:
    result = subprocess.run(
        ["git", "log", "-1", "--format=%s", branch],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit_count(repo: Path, branch: str) -> int:
    result = subprocess.run(
        ["git", "rev-list", "--count", f"main..{branch}"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return int(result.stdout.strip())


def _file_on_branch(repo: Path, branch: str, name: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{branch}:{name}"],
        cwd=repo,
        capture_output=True,
    )
    return result.returncode == 0
