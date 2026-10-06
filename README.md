# Kiln

Kiln is a software factory for one git repository. You give it a goal. A foreman decides what happens next: create a job, dispatch it, or approve, rework, or reject a worker branch. A job has a role. Workers edit code, each in its own git worktree and branch. Scouts and reviewers are read-only. The foreman decides the process. Kiln runs the job and keeps the invariants.

Kiln drives the Cursor CLI (`agent`). `kiln run` keeps working until the goal is finished, then opens one pull request.

## Setup

Kiln needs Python 3.12 and [uv](https://docs.astral.sh/uv/). The `agent` command must be on your `PATH`.

```bash
uv sync
uv run kiln --help
```

Run Kiln from the repository you want a factory for:

```bash
cd /path/to/your-repo
uv run --project /path/to/kiln kiln init
```

`kiln init` writes `kiln.toml` (commit this) and creates `.kiln/` (gitignored). `.kiln/` holds the SQLite database, agent logs, and worker worktrees. Running init again leaves an existing `kiln.toml` alone.

## A goal

```bash
kiln goal add "Add a health check" --description "GET /health returns 200"
kiln run
```

`kiln run` creates a branch for the goal (`kiln/goal-<id>-<slug>`). The foreman creates jobs and dispatches them. Each approved worker is merged onto that goal branch. When nothing is left to do, Kiln pushes the branch to `origin` and opens one pull request into `base_branch`. The pull request includes the brief and the evidence.

A scout or reviewer is ready when it is pending and every dependency is completed. A worker is ready when it is pending, every dependency is completed, and every worker it depends on is merged. A rejected worker does not unblock jobs that depend on it.

The repository needs an `origin` remote and the GitHub CLI (`gh`) so the pull request can be opened.

```bash
kiln run --dry-run      # print the state the foreman would see; change nothing
kiln run --turns 1      # one foreman turn, then stop
kiln run --workers 1    # cap how many agents a turn runs at once
```

## Looking around

```bash
kiln status
kiln goal show 1     # brief, evidence, and jobs
kiln job list
kiln job show 1
kiln log            # recent events, oldest first
kiln log -n 50
kiln runs show 3    # model, report, and the tail of the agent log
```

## Human overrides

The foreman creates the job graph. These commands are the escape hatch:

```bash
kiln job add 1 "Write the handler" --acceptance "pytest passes" --priority 1
kiln job dep 2 1           # job 2 waits until job 1 is done
kiln job cancel 2
kiln work                  # one worker, next ready worker job, no foreman
kiln work --job 1
kiln scout "Where is the HTTP stack?"
kiln review 1 --approve
kiln review 1 --rework "The handler ignores the timeout"
kiln review 1 --reject --reason "Wrong approach"
```

`kiln job add` creates a worker. `kiln scout` creates a scout job and runs it. `kiln review` does not run the reviewer. It approves, reworks, or rejects a completed worker whose integration is still pending. Pass exactly one of `--approve`, `--rework`, or `--reject`. Approve merges the job branch into the goal branch. A conflict sends the job back to pending with the conflict in its feedback. Rework does the same with your note, clears the current result, and the next attempt keeps the branch. Reject leaves execution completed, keeps the result and the branch, and sets integration to rejected.

## Cleanup

`kiln gc` removes worktrees that finished jobs no longer need. A completed worker whose integration is still pending keeps its worktree, because a reviewer may still read it. A merged worker's branch is deleted when `delete_merged_branches` is true. Rejected, failed, and cancelled jobs drop the worktree and keep the branch, so the commits can still be inspected. Pending, claimed, and running jobs are left alone. `kiln run` does this sweep at the start of a turn.

```bash
kiln gc
```

## What a run does

`kiln run` repeats a turn until every active goal is finished. Each turn asks the foreman once. The state it sees includes the brief, the turn number, each job's role, whether it is ready or blocked, a worker's integration, a reviewer's target, and the current result summary and evidence. It does not include the diff.

1. Remove leftover worktrees from finished jobs.
2. Create the goal branch from `base_branch` if it does not exist yet.
3. Ask the foreman for one fenced JSON action list.
4. Apply state changes in that order. Approving a job merges its branch into the goal branch. A conflict becomes rework. Rejecting a job changes integration only.
5. Run the dispatches from that list, together. The role selects the executor. At most `max_parallel_workers` run at once. Extra dispatches wait for the next turn.

| Action | What Kiln does |
| --- | --- |
| `create_job` | Add a job. `role` is `scout`, `worker`, or `reviewer`. `ref` names it for later actions in the same list. `depends_on` takes refs or job ids. `priority` is an integer; higher values are scheduled first. A scout requires `question`. A reviewer requires `target_job_id`, and that worker must be listed in `depends_on`. |
| `dispatch` | Run a ready pending job, by `job_id` or `ref`. A reviewer also needs its target completed with integration still pending. |
| `approve` | Merge a completed worker whose integration is still pending into the goal branch. Execution stays completed. |
| `reject` | Set that worker's integration to rejected. Execution stays completed and the result stays. |
| `rework` | Send that worker back to pending with `feedback`. The current result is cleared. Earlier attempts stay on the run history. The branch and worktree stay. |
| `cancel` | Cancel a job. |
| `note` | Save `text` for later turns. Notes are the foreman's scratch pad. Scout output is the scout job's result. |
| `update_brief` | Replace the goal brief. |
| `goal_done` | Finish the goal. `evidence` is a non-empty list. Kiln refuses this while any job is pending, claimed, running, or a completed worker whose integration is still pending. |

Kiln refuses a list that dispatches a reviewer and also approves or rejects that reviewer's target. Both actions are refused. The rest of the list still runs.

When a goal has no open jobs, Kiln pushes its branch and opens the pull request. The body includes the brief, the evidence, and the jobs. The run stops at `max_foreman_turns` (default 25), after two foreman failures in a row, or when a turn runs no agents and changes nothing.

A worker checks out `kiln/<id>-<slug>` under `.kiln/worktrees/<id>/` from the goal branch, runs `agent` with `--force --trust`, and must commit. A commit, including one whose verify command fails, completes the job and sets integration to pending. The verify failure is inside the result. No commit fails the job and stores no result. The reviewer uses `--mode ask` in that worktree and sees the diff against the goal branch. Its verdict is stored on the reviewer job and does not merge or reject. The foreman and scouts use `--mode ask`. A scout reads the repository checkout.

A job that has used `max_attempts` cannot be reworked. It stays completed so the foreman can approve or reject it.

## Configuration

`kiln.toml`:

| Key | Meaning |
| --- | --- |
| `base_branch` | Branch the pull request targets. `kiln init` uses the current branch. |
| `verify` | Shell command run in the worktree after a worker commits. Empty skips it. |
| `max_parallel_workers` | How many dispatches one turn may run at once. Default 2. |
| `max_foreman_turns` | How many foreman turns one run may take. Default 25. |
| `max_attempts` | Attempts before rework is refused. Default 3. |
| `delete_merged_branches` | Delete a worker branch after it merges into the goal branch. Default true. The goal branch stays. |
| `models.foreman` | Model that decides what happens each turn. |
| `models.worker` | Model that edits code. |
| `models.scout` | Model for exploration. |
| `models.reviewer` | Model that reads the diff and returns a verdict. |

The defaults are `claude-opus-5-thinking-high` for the foreman, `claude-sonnet-5-thinking-high` for the worker and the reviewer, and `composer-2.5` for the scout. `KILN_AGENT_BIN` overrides the `agent` executable.
