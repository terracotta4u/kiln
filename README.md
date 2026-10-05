# Kiln

Kiln is a software factory for one git repository. You give it a goal. A foreman decides what happens next: scout, split the work, dispatch a worker, or ask a reviewer to read the diff. Disposable workers do the tasks, each in its own git worktree and branch. Scouts explore the repo and report back. Only workers write code. The foreman, scouts, and reviewer run read-only, and Kiln applies their decisions itself.

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

`kiln run` creates a branch for the goal (`kiln/goal-<id>-<slug>`). The foreman decides whether to scout, split the goal into tasks, and dispatch workers. Each approved task is merged onto that goal branch. When nothing is left to do, Kiln pushes the branch to `origin` and opens one pull request into `base_branch`. The pull request includes the brief and the evidence.

A task is ready when it is pending and every dependency is done. Done means the task branch has been merged into the goal branch.

The repository needs an `origin` remote and the GitHub CLI (`gh`) so the pull request can be opened.

```bash
kiln run --dry-run      # print the state the foreman would see; change nothing
kiln run --turns 1      # one foreman turn, then stop
kiln run --workers 1    # cap how many agents a turn runs at once
```

## Looking around

```bash
kiln status
kiln goal show 1     # brief, evidence, and tasks
kiln task list
kiln task show 1
kiln log            # recent events, oldest first
kiln log -n 50
kiln runs show 3    # model, report, and the tail of the agent log
```

## Human overrides

The foreman creates the task graph. These commands are the escape hatch:

```bash
kiln task add 1 "Write the handler" --acceptance "pytest passes" --priority 1
kiln task dep 2 1          # task 2 waits until task 1 is done
kiln task cancel 2
kiln work                  # one worker, next ready task, no foreman
kiln work --task 1
kiln scout "Where is the HTTP stack?"
kiln review 1 --approve
kiln review 1 --rework "The handler ignores the timeout"
kiln review 1 --fail --reason "Wrong approach"
```

`kiln review` accepts exactly one of `--approve`, `--rework`, or `--fail`. Approve merges the task branch into the goal branch. A conflict sends the task back to pending with the conflict in its feedback. Rework does the same with your note, and the next worker keeps the branch.

## Cleanup

Finished tasks (done, failed, cancelled) do not need a checkout. `kiln gc` removes their worktrees, and deletes a done task's branch when `delete_merged_branches` is true. Failed and cancelled branches stay, so the commits can still be inspected. Tasks that are pending, claimed, running, or in review are left alone. `kiln run` does this sweep at the start of a turn.

```bash
kiln gc
```

## What a run does

`kiln run` repeats a turn until every active goal is finished. Each turn asks the foreman once. The state it sees includes the brief, the turn number, which tasks are ready or blocked, and the latest review.

1. Remove leftover worktrees from finished tasks.
2. Create the goal branch from `base_branch` if it does not exist yet.
3. Ask the foreman for one fenced JSON action list.
4. Apply state changes in that order. Approving a task merges its branch into the goal branch. A conflict becomes rework.
5. Run the scouts, workers, and reviewers from that list, together. At most `max_parallel_workers` run at once. Extra dispatches wait for the next turn.

| Action | What Kiln does |
| --- | --- |
| `create_task` | Add a task. `ref` names it for later actions in the same list. `depends_on` takes refs or task ids. `priority` is an integer; higher values are scheduled first. |
| `scout` | Explore the repo and answer `question`. The note is in the next turn's state. |
| `dispatch` | Run a worker on a ready pending task, by `task_id` or `ref`. |
| `review` | Run the reviewer on a task in review. `focus` is optional. |
| `approve` | Merge a task in review into the goal branch. |
| `rework` | Send a task in review back to pending with `feedback`. |
| `fail` | Fail a task. `reason` is optional. |
| `cancel` | Cancel a task. |
| `note` | Save `text` for later turns. |
| `update_brief` | Replace the goal brief. |
| `goal_done` | Finish the goal. `evidence` is a non-empty list, and every task must already be finished. |

When a goal has no pending, claimed, running, or review tasks, Kiln pushes its branch and opens the pull request. The body includes the brief, the evidence, and the tasks. The run stops at `max_foreman_turns` (default 25), after two foreman failures in a row, or when a turn runs no agents and changes nothing.

Each worker claims one task, checks out `kiln/<id>-<slug>` under `.kiln/worktrees/<id>/` from the goal branch, runs `agent` with `--force --trust`, commits, and optionally runs the `verify` command. The task then waits in review. The reviewer uses `--mode ask` in that worktree and sees the diff against the goal branch. The foreman and scouts use `--mode ask`.

A task that has used `max_attempts` fails instead of starting another attempt.

## Configuration

`kiln.toml`:

| Key | Meaning |
| --- | --- |
| `base_branch` | Branch the pull request targets. `kiln init` uses the current branch. |
| `verify` | Shell command run in the worktree after a worker finishes. Empty skips it. |
| `max_parallel_workers` | How many scouts, workers, and reviewers one turn may run at once. Default 2. |
| `max_foreman_turns` | How many foreman turns one run may take. Default 25. |
| `max_attempts` | Rework attempts before a task fails. Default 3. |
| `delete_merged_branches` | Delete a task branch after it merges into the goal branch. Default true. The goal branch stays. |
| `models.foreman` | Model that decides what happens each turn. |
| `models.worker` | Model that edits code. |
| `models.scout` | Model for exploration. |
| `models.reviewer` | Model that reads the diff and returns a verdict. |

The defaults are `claude-opus-5-thinking-high` for the foreman, `claude-sonnet-5-thinking-high` for the worker and the reviewer, and `composer-2.5` for the scout. `KILN_AGENT_BIN` overrides the `agent` executable.
