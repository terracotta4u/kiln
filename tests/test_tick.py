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
from kiln.jobs import add_goal, add_job, dependencies, get_job, list_jobs, require_goal
from kiln.models import GoalStatus, Integration, JobRole, JobStatus
from kiln.notes import list_notes
from kiln.review import approve_job
from kiln.roles.foreman import apply_actions
from kiln.roles.worker import run_worker
from kiln.tick import run_tick, run_until_done

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


def test_turns_dispatch_review_then_finish(factory: Path):
    script = _agent(factory, _smart_agent())
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        add_goal(conn, "Ship it", "Add a marker file")
        first = run_tick(conn, config, agent_bin=str(script))
        job = list_jobs(conn)[0]
        on_main_after_work = (factory / "marker.txt").exists()
        second = run_tick(conn, config, agent_bin=str(script))
        reviewed = get_job(conn, job.id)
        third = run_tick(conn, config, agent_bin=str(script))
        stored = get_job(conn, job.id)
        goal = require_goal(conn, 1)
    finally:
        conn.close()

    assert any("created job #1" in line for line in first.lines)
    assert any("job #1  completed" in line for line in first.lines)
    assert job.status == JobStatus.completed
    assert job.integration == Integration.pending
    assert on_main_after_work is False
    assert reviewed is not None and reviewed.status == JobStatus.completed
    assert reviewed.integration == Integration.pending
    assert any("reviewed #1: approve" in line for line in second.lines)
    assert stored is not None and stored.status == JobStatus.completed
    assert stored.integration == Integration.merged
    assert not (factory / "marker.txt").exists()
    marker = subprocess.run(
        ["git", "show", "kiln/goal-1-ship-it:marker.txt"],
        cwd=factory,
        check=True,
        capture_output=True,
        text=True,
    )
    assert marker.stdout == "ok\n"
    assert any("merged into kiln/goal-1-ship-it" in line for line in third.lines)
    assert any("goal #1 done" in line for line in third.lines)
    assert not (factory / ".kiln" / "worktrees" / "1").exists()
    missing = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", "refs/heads/kiln/1-add-marker"],
        cwd=factory,
    )
    assert missing.returncode != 0
    assert goal.status == GoalStatus.done
    assert goal.brief == "Success: marker.txt exists."
    assert goal.evidence == ("marker.txt is ok on the goal branch",)


def test_conflict_sends_the_job_back_for_rework(factory: Path):
    script = _agent(
        factory,
        "from pathlib import Path\n"
        "Path('README.md').write_text('worker\\n')\n"
        + _emit({"summary": "edited readme", "files": ["README.md"]}),
    )
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        job = add_job(conn, goal.id, "Edit readme")
        run_worker(conn, config, job_id=job.id, agent_bin=str(script))
        integration = require_goal(conn, goal.id).branch
        subprocess.run(["git", "checkout", integration], cwd=factory, check=True, capture_output=True)
        (factory / "README.md").write_text("goal\n")
        subprocess.run(["git", "commit", "-am", "goal edit"], cwd=factory, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "main"], cwd=factory, check=True, capture_output=True)
        message = approve_job(conn, config, job.id)
        stored = get_job(conn, job.id)
    finally:
        conn.close()

    assert "rework" in message
    assert stored is not None
    assert stored.status == JobStatus.pending
    assert stored.feedback is not None and "conflicted" in stored.feedback
    assert (factory / "README.md").read_text() == "hello\n"
    goal_readme = subprocess.run(
        ["git", "show", f"{integration}:README.md"],
        cwd=factory,
        check=True,
        capture_output=True,
        text=True,
    )
    assert goal_readme.stdout == "goal\n"
    assert subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{stored.branch}"],
        cwd=factory,
    ).returncode == 0


def test_scout_action_runs_during_the_tick(factory: Path):
    script = _agent(factory, _queue_agent())
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        add_job(conn, goal.id, "Existing")
        first = run_tick(conn, config, agent_bin=str(script))
        scout_jobs = [job for job in list_jobs(conn, goal_id=goal.id) if job.role.value == "scout"]
        second = run_tick(conn, config, agent_bin=str(script))
        notes = list_notes(conn, goal.id)
    finally:
        conn.close()

    assert any("scout job" in line for line in first.lines)
    assert len(scout_jobs) == 1
    assert scout_jobs[0].question == "scout-me please"
    assert scout_jobs[0].status.value == "completed"
    assert json.loads(scout_jobs[0].result or "")["summary"] == "answered"
    assert notes == []
    assert any("no actions" in line for line in second.lines)


def test_dry_run_changes_nothing(factory: Path):
    script = _agent(factory, "from pathlib import Path\nPath('agent-ran').write_text('x')\n")
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        add_goal(conn, "Ship it", "A factory")
        result = run_tick(conn, config, agent_bin=str(script), dry_run=True)
        jobs = list_jobs(conn)
    finally:
        conn.close()

    assert not (factory / "agent-ran").exists()
    assert jobs == []
    assert any("Ship it" in line for line in result.lines)
    assert any("Turn 1 of 25" in line for line in result.lines)
    assert any("would create branch" in line for line in result.lines)


def test_worker_limit_dispatches_one_of_two_ready_jobs(factory: Path):
    script = _agent(factory, _smart_agent())
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        add_job(conn, goal.id, "First")
        add_job(conn, goal.id, "Second")
        run_tick(conn, config, workers=1, agent_bin=str(script))
        jobs = list_jobs(conn)
    finally:
        conn.close()

    statuses = sorted(job.status for job in jobs)
    assert statuses == [JobStatus.completed, JobStatus.pending]


def test_two_workers_run_together(factory: Path):
    script = _agent(factory, _smart_agent())
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        add_job(conn, goal.id, "First")
        add_job(conn, goal.id, "Second")
        run_tick(conn, config, workers=2, agent_bin=str(script))
        jobs = list_jobs(conn)
    finally:
        conn.close()

    assert [job.status for job in jobs] == [JobStatus.completed, JobStatus.completed]


def test_run_until_done_opens_one_pull_request(factory: Path):
    bare = factory.parent / f"{factory.name}-origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=factory, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=factory, check=True, capture_output=True)
    script = _agent(factory, _smart_agent())
    gh = factory / "fake-gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"Path({str(factory / 'pr-args.txt')!r}).write_text('\\n'.join(sys.argv[1:]))\n"
        "print('https://example.com/pull/1')\n"
    )
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        add_goal(conn, "Ship it", "Add a marker file")
        result = run_until_done(conn, config, agent_bin=str(script), gh_bin=str(gh))
        goal = get_goal_row(conn)
        from kiln.jobs import list_goals

        stored = list_goals(conn)[0]
    finally:
        conn.close()

    assert goal == GoalStatus.done
    assert stored.pr_url == "https://example.com/pull/1"
    assert any("opened https://example.com/pull/1" in line for line in result.lines)
    assert not (factory / "marker.txt").exists()
    marker = subprocess.run(
        ["git", "show", "kiln/goal-1-ship-it:marker.txt"],
        cwd=factory,
        check=True,
        capture_output=True,
        text=True,
    )
    assert marker.stdout == "ok\n"
    subprocess.run(
        ["git", "ls-remote", "--exit-code", "--heads", "origin", "kiln/goal-1-ship-it"],
        cwd=factory,
        check=True,
        capture_output=True,
    )
    args = (factory / "pr-args.txt").read_text()
    assert "pr\ncreate" in args
    assert "--base\nmain" in args
    assert "--head\nkiln/goal-1-ship-it" in args
    assert stored.brief == "Success: marker.txt exists."
    assert stored.evidence == ("marker.txt is ok on the goal branch",)
    assert "Success: marker.txt exists." in args
    assert "- marker.txt is ok on the goal branch" in args
    assert any("reviewed #1: approve" in line for line in result.lines)
    assert any("goal #1 done" in line for line in result.lines)


def test_turn_cap_stops_after_notes(factory: Path):
    script = _agent(factory, _note_agent())
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        result = run_until_done(conn, config, agent_bin=str(script), turns=2)
        notes = list_notes(conn, goal.id)
        status = get_goal_row(conn)
    finally:
        conn.close()

    assert len(notes) == 2
    assert status == GoalStatus.active
    assert result.stop_reason == "turn_limit"
    assert "stopped: reached 2 foreman turns" in result.lines


def test_two_foreman_failures_stop_the_run(factory: Path):
    script = _agent(factory, "print('not a report')\n")
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        add_goal(conn, "Ship it")
        result = run_until_done(conn, config, agent_bin=str(script), turns=5)
    finally:
        conn.close()

    failures = [line for line in result.lines if line.startswith("foreman failed")]
    assert len(failures) == 2
    assert result.stop_reason == "foreman_failures"
    assert "stopped: foreman failed twice in a row" in result.lines
    assert not any("reached" in line for line in result.lines)


def test_no_progress_stops_the_run(factory: Path):
    script = _agent(
        factory,
        "import sys\n"
        "prompt = sys.argv[-1]\n"
        "if 'You are the Kiln foreman' in prompt:\n"
        f"    {_emit_call('{\"actions\": []}')}\n",
    )
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        add_goal(conn, "Ship it")
        result = run_until_done(conn, config, agent_bin=str(script), turns=5)
        status = get_goal_row(conn)
    finally:
        conn.close()

    assert status == GoalStatus.active
    assert result.stop_reason == "no_progress"
    assert any("no actions" in line for line in result.lines)
    assert "stopped: a turn made no progress" in result.lines


def test_should_stop_ends_the_run_before_the_next_turn(factory: Path):
    script = _agent(factory, _note_agent())
    config = load_config(factory)
    conn = connect(config.db_path)
    seen = {"n": 0}

    def stop() -> bool:
        seen["n"] += 1
        return seen["n"] > 1

    try:
        goal = add_goal(conn, "Ship it")
        result = run_until_done(conn, config, agent_bin=str(script), turns=5, should_stop=stop)
        notes = list_notes(conn, goal.id)
        status = get_goal_row(conn)
    finally:
        conn.close()

    assert seen["n"] == 2
    assert len(notes) == 1
    assert status == GoalStatus.active
    assert result.stop_reason == "server_shutdown"
    assert "stopped: server shutting down" in result.lines


def test_cli_run_turns_dry_run_and_drops_no_dispatch(factory: Path, monkeypatch: pytest.MonkeyPatch):
    script = _agent(factory, _note_agent())
    monkeypatch.setenv("KILN_AGENT_BIN", str(script))
    assert runner.invoke(app, ["goal", "add", "Ship it"]).exit_code == 0

    dry = runner.invoke(app, ["run", "--dry-run", "--turns", "3"])
    assert dry.exit_code == 0, dry.output
    assert "Turn 1 of 3" in dry.output
    assert "would create branch" in dry.output

    capped = runner.invoke(app, ["run", "--turns", "2"])
    assert capped.exit_code == 0, capped.output
    assert "stopped: reached 2 foreman turns" in capped.output
    assert "note #1" in capped.output
    assert "note #2" in capped.output

    rejected = runner.invoke(app, ["run", "--turns", "0"])
    assert rejected.exit_code != 0
    assert "turns must be >= 1" in rejected.output

    removed = runner.invoke(app, ["run", "--no-dispatch"])
    assert removed.exit_code != 0
    assert "No such option" in removed.output


def test_cli_review_approves(factory: Path, monkeypatch: pytest.MonkeyPatch):
    script = _agent(
        factory,
        "from pathlib import Path\n"
        "Path('marker.txt').write_text('ok\\n')\n"
        + _emit({"summary": "added marker", "files": ["marker.txt"]}),
    )
    monkeypatch.setenv("KILN_AGENT_BIN", str(script))
    assert runner.invoke(app, ["goal", "add", "Ship it"]).exit_code == 0
    assert runner.invoke(app, ["job", "add", "1", "Add marker"]).exit_code == 0
    worked = runner.invoke(app, ["work"])
    assert worked.exit_code == 0, worked.output
    approved = runner.invoke(app, ["review", "1", "--approve"])
    assert approved.exit_code == 0, approved.output
    assert "merged into kiln/goal-1-ship-it" in approved.output
    assert not (factory / "marker.txt").exists()
    marker = subprocess.run(
        ["git", "show", "kiln/goal-1-ship-it:marker.txt"],
        cwd=factory,
        check=True,
        capture_output=True,
        text=True,
    )
    assert marker.stdout == "ok\n"


def test_scout_then_worker_then_reviewer_before_approve(factory: Path):
    script = _agent(factory, _graph_agent())
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it", "Add a marker")
        apply_actions(
            conn,
            config,
            goal,
            [
                {
                    "type": "create_job",
                    "ref": "look",
                    "role": "scout",
                    "title": "Where is the readme",
                    "question": "Where is the readme?",
                },
                {"type": "dispatch", "ref": "look"},
            ],
            agent_bin=str(script),
        )
        scout = next(job for job in list_jobs(conn) if job.role == JobRole.scout)
        apply_actions(
            conn,
            config,
            goal,
            [
                {
                    "type": "create_job",
                    "ref": "marker",
                    "role": "worker",
                    "title": "Add marker",
                    "acceptance": "file exists",
                    "depends_on": [scout.id],
                },
                {"type": "dispatch", "ref": "marker"},
            ],
            agent_bin=str(script),
        )
        worker = next(job for job in list_jobs(conn) if job.role == JobRole.worker)
        reviewed = apply_actions(
            conn,
            config,
            goal,
            [
                {
                    "type": "create_job",
                    "ref": "check",
                    "role": "reviewer",
                    "title": "Review marker",
                    "target_job_id": worker.id,
                    "depends_on": [scout.id, worker.id],
                    "focus": "the marker",
                },
                {"type": "dispatch", "ref": "check"},
            ],
            agent_bin=str(script),
        )
        reviewer = next(job for job in list_jobs(conn) if job.role == JobRole.reviewer)
        dep_ids = {dep.id for dep in dependencies(conn, reviewer.id)}
        worker_after = get_job(conn, worker.id)
        notes = list_notes(conn, goal.id)
    finally:
        conn.close()

    assert scout.status == JobStatus.completed
    assert json.loads(scout.result or "")["summary"] == "scouted"
    assert notes == []
    assert worker.status == JobStatus.completed
    assert worker.integration == Integration.pending
    assert dep_ids == {scout.id, worker.id}
    assert reviewer.target_job_id == worker.id
    assert reviewer.status == JobStatus.completed
    assert worker_after is not None and worker_after.integration == Integration.pending
    assert any(f"reviewed #{worker.id}: approve" in line for line in reviewed)


def test_a_dispatch_does_not_see_another_dispatch_finish(factory: Path):
    script = _agent(factory, _graph_agent())
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it", "Add a marker")
        messages = apply_actions(
            conn,
            config,
            goal,
            [
                {
                    "type": "create_job",
                    "ref": "first",
                    "role": "worker",
                    "title": "Add marker",
                    "acceptance": "file exists",
                },
                {
                    "type": "create_job",
                    "ref": "second",
                    "role": "worker",
                    "title": "Next",
                    "depends_on": ["first"],
                },
                {
                    "type": "create_job",
                    "ref": "check",
                    "role": "reviewer",
                    "title": "Review marker",
                    "target_job_id": "first",
                    "depends_on": ["first"],
                },
                {"type": "dispatch", "ref": "first"},
                {"type": "dispatch", "ref": "second"},
                {"type": "dispatch", "ref": "check"},
            ],
            agent_bin=str(script),
        )
        jobs = {job.role: job for job in list_jobs(conn) if job.role != JobRole.worker}
        workers = [job for job in list_jobs(conn) if job.role == JobRole.worker]
    finally:
        conn.close()

    first, second = workers
    assert "completed" in messages[3]
    assert f"job #{second.id} is blocked" in messages[4]
    assert f"job #{jobs[JobRole.reviewer].id} is blocked" in messages[5]
    assert first.status == JobStatus.completed
    assert first.integration == Integration.pending
    assert first.attempts == 1
    assert second.status == JobStatus.pending
    assert second.attempts == 0
    assert jobs[JobRole.reviewer].status == JobStatus.pending
    assert jobs[JobRole.reviewer].attempts == 0


def test_a_single_worker_can_finish_the_goal(factory: Path):
    script = _agent(factory, _graph_agent())
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        apply_actions(
            conn,
            config,
            goal,
            [
                {
                    "type": "create_job",
                    "ref": "marker",
                    "role": "worker",
                    "title": "Add marker",
                    "acceptance": "file exists",
                },
                {"type": "dispatch", "ref": "marker"},
            ],
            agent_bin=str(script),
        )
        apply_actions(conn, config, goal, [{"type": "approve", "job_id": 1}])
        apply_actions(
            conn,
            config,
            goal,
            [{"type": "goal_done", "evidence": ["marker.txt exists on the goal branch"]}],
        )
        jobs = list_jobs(conn)
        stored = require_goal(conn, goal.id)
    finally:
        conn.close()

    assert len(jobs) == 1
    assert jobs[0].role == JobRole.worker
    assert jobs[0].status == JobStatus.completed
    assert jobs[0].integration == Integration.merged
    assert stored.status == GoalStatus.done


def test_rework_keeps_the_first_attempt_and_the_next_review_sees_the_new_one(factory: Path):
    script = _agent(factory, _lifecycle_agent())
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it", "Add a marker")
        apply_actions(
            conn,
            config,
            goal,
            [
                {
                    "type": "create_job",
                    "ref": "marker",
                    "role": "worker",
                    "title": "Add marker",
                    "acceptance": "file exists",
                },
                {"type": "dispatch", "ref": "marker"},
            ],
            agent_bin=str(script),
        )
        worker = get_job(conn, 1)
        assert worker is not None
        first_roles = _run_roles(conn, worker.id)
        apply_actions(
            conn,
            config,
            goal,
            [
                {
                    "type": "create_job",
                    "ref": "first",
                    "role": "reviewer",
                    "title": "First review",
                    "target_job_id": worker.id,
                    "depends_on": [worker.id],
                    "focus": "the first draft",
                },
                {"type": "dispatch", "ref": "first"},
            ],
            agent_bin=str(script),
        )
        after_review = get_job(conn, worker.id)
        first_review = next(job for job in list_jobs(conn) if job.role == JobRole.reviewer)
        apply_actions(
            conn,
            config,
            goal,
            [{"type": "rework", "job_id": worker.id, "feedback": "try again"}],
        )
        reworked = get_job(conn, worker.id)
        roles_after_rework = _run_roles(conn, worker.id)
        apply_actions(
            conn,
            config,
            goal,
            [{"type": "dispatch", "job_id": worker.id}],
            agent_bin=str(script),
        )
        second = get_job(conn, worker.id)
        roles_after_second = _run_roles(conn, worker.id)
        apply_actions(
            conn,
            config,
            goal,
            [
                {
                    "type": "create_job",
                    "ref": "second",
                    "role": "reviewer",
                    "title": "Second review",
                    "target_job_id": worker.id,
                    "depends_on": [worker.id],
                    "focus": "the second draft",
                },
                {"type": "dispatch", "ref": "second"},
            ],
            agent_bin=str(script),
        )
        reviews = [job for job in list_jobs(conn) if job.role == JobRole.reviewer]
        apply_actions(conn, config, goal, [{"type": "approve", "job_id": worker.id}])
        approved = get_job(conn, worker.id)
        reviews_after = [job for job in list_jobs(conn) if job.role == JobRole.reviewer]
    finally:
        conn.close()

    assert worker.status == JobStatus.completed
    assert worker.integration == Integration.pending
    assert worker.result is not None and "wrote one" in worker.result
    assert first_roles == ["worker"]
    assert after_review is not None and after_review.status == JobStatus.completed
    assert after_review.integration == Integration.pending
    assert after_review.result is not None and "wrote one" in after_review.result
    assert first_review.status == JobStatus.completed
    assert first_review.result is not None and "first attempt" in first_review.result
    assert reworked is not None
    assert reworked.status == JobStatus.pending
    assert reworked.result is None
    assert reworked.integration is None
    assert reworked.feedback == "try again"
    assert reworked.branch
    assert roles_after_rework == ["worker"]
    assert second is not None and second.status == JobStatus.completed
    assert second.integration == Integration.pending
    assert second.result is not None and "wrote two" in second.result
    assert roles_after_second == ["worker", "worker"]
    assert len(reviews) == 2
    assert reviews[0].id == first_review.id
    assert reviews[0].status == JobStatus.completed
    assert reviews[1].status == JobStatus.completed
    assert reviews[1].result is not None and "second attempt" in reviews[1].result
    assert approved is not None and approved.status == JobStatus.completed
    assert approved.integration == Integration.merged
    assert approved.result is not None and "wrote two" in approved.result
    assert reviews_after[0].result == first_review.result


def _run_roles(conn, job_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT role FROM runs WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()
    return [row["role"] for row in rows]


def _graph_agent() -> str:
    return f"""
import sys
from pathlib import Path
prompt = sys.argv[-1]
if "You are a scout" in prompt:
    {_emit_call('{"summary": "scouted", "findings": ["README.md"]}')}
elif "You are a Kiln reviewer" in prompt:
    {_emit_call('{"verdict": "approve", "summary": "looked", "findings": ["marker"], "confidence": 0.8}')}
else:
    Path("marker.txt").write_text("ok\\n")
    {_emit_call('{"summary": "added marker", "files": ["marker.txt"]}')}
"""


def _lifecycle_agent() -> str:
    return f"""
import sys
from pathlib import Path
prompt = sys.argv[-1]
if "You are a Kiln reviewer" in prompt:
    if "+two" in prompt:
        {_emit_call('{"verdict": "approve", "summary": "second attempt", "findings": [], "confidence": 0.9}')}
    else:
        {_emit_call('{"verdict": "needs_changes", "summary": "first attempt", "findings": ["rewrite it"], "confidence": 0.4}')}
else:
    if "try again" in prompt:
        Path("marker.txt").write_text("two\\n")
        {_emit_call('{"summary": "wrote two", "files": ["marker.txt"]}')}
    else:
        Path("marker.txt").write_text("one\\n")
        {_emit_call('{"summary": "wrote one", "files": ["marker.txt"]}')}
"""


def _smart_agent() -> str:
    return f"""
import json, sys
from pathlib import Path
prompt = sys.argv[-1]
if "You are a scout" in prompt:
    {_emit_call('{"summary": "repo has a readme", "findings": ["README.md"]}')}
elif "You are a Kiln reviewer" in prompt:
    {_emit_call('{"verdict": "approve", "summary": "marker is one line", "findings": ["file exists"], "confidence": 0.9}')}
elif "You are the Kiln foreman" in prompt:
    if "[completed]" in prompt and "integration pending" in prompt and "marker is one line" in prompt:
        {_emit_call('{"actions": [{"type": "approve", "job_id": 1}, {"type": "update_brief", "text": "Success: marker.txt exists."}, {"type": "goal_done", "evidence": ["marker.txt is ok on the goal branch"]}]}')}
    elif "[completed]" in prompt and "integration pending" in prompt:
        {_emit_call('{"actions": [{"type": "create_job", "ref": "check", "role": "reviewer", "title": "Review marker", "target_job_id": 1, "depends_on": [1], "focus": "marker.txt is one line"}, {"type": "dispatch", "ref": "check"}]}')}
    elif "Jobs:\\n(none)" in prompt:
        {_emit_call('{"actions": [{"type": "create_job", "ref": "marker", "role": "worker", "title": "Add marker", "description": "Write marker.txt", "acceptance": "file exists", "depends_on": [], "priority": 1}, {"type": "dispatch", "ref": "marker"}]}')}
    elif "[pending]" in prompt:
        {_emit_call('{"actions": [{"type": "dispatch", "job_id": 1}, {"type": "dispatch", "job_id": 2}]}')}
    else:
        {_emit_call('{"actions": []}')}
else:
    Path("marker.txt").write_text("ok\\n")
    {_emit_call('{"summary": "added marker", "files": ["marker.txt"]}')}
"""


def _note_agent() -> str:
    return (
        "import sys\n"
        "prompt = sys.argv[-1]\n"
        "if 'You are the Kiln foreman' in prompt:\n"
        f"    {_emit_call('{\"actions\": [{\"type\": \"note\", \"text\": \"still going\"}]}')}\n"
    )


def _queue_agent() -> str:
    return f"""
import sys
prompt = sys.argv[-1]
if "You are a scout" in prompt:
    {_emit_call('{"summary": "answered", "findings": []}')}
elif "You are the Kiln foreman" in prompt:
    if "scout-me please" in prompt:
        {_emit_call('{"actions": []}')}
    else:
        {_emit_call('{"actions": [{"type": "create_job", "ref": "look", "role": "scout", "title": "scout-me please", "question": "scout-me please"}, {"type": "dispatch", "ref": "look"}]}')}
else:
    {_emit_call('{"summary": "worker", "files": []}')}
"""


def _emit_call(payload: str) -> str:
    return "print(" + json.dumps(_envelope(payload)) + ")"


def _emit(payload: dict) -> str:
    return "print(" + json.dumps(_envelope(json.dumps(payload))) + ")\n"


def _envelope(result_body: str) -> str:
    if not result_body.startswith("{"):
        body = result_body
    else:
        body = result_body
    report = body if body.startswith("{") else body
    # result_body is already a JSON object string.
    text = "```json\n" + report + "\n```"
    return json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": text}
    )


def _agent(directory: Path, body: str) -> Path:
    path = directory / "fake-agent"
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def get_goal_row(conn):
    from kiln.jobs import list_goals

    return list_goals(conn)[0].status
