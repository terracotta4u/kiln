# Kiln

Kiln is a software factory for one git repository. You give it a goal. A foreman plans and reviews. Disposable workers do the tasks, each in its own git worktree and branch. Scouts explore the repo and report back. Only workers write code. The foreman and scouts run read-only, and Kiln applies their decisions itself.

Kiln drives the Cursor CLI (`agent`). One tick of `kiln run` does a cycle of work and then exits.

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

The first tick scouts a fresh goal, asks the foreman to break it into tasks, and dispatches workers for anything that is ready. Run `kiln run` again to review finished work and merge it. Keep going until the goal is done.

A task is ready when it is pending and every dependency is done. Done means the branch has been merged into `base_branch`.

```bash
kiln run --dry-run      # print the tick; change nothing
kiln run --no-dispatch  # scout, plan, and merge; skip workers
kiln run --workers 1    # cap how many workers this tick starts
```

## Looking around

```bash
kiln status
kiln goal show 1
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

`kiln review` accepts exactly one of `--approve`, `--rework`, or `--fail`. Approve merges the task branch. A conflict sends the task back to pending with the conflict in its feedback. Rework does the same with your note, and the next worker keeps the branch.

## Cleanup

Finished tasks (done, failed, cancelled) do not need a checkout. `kiln gc` removes their worktrees, and deletes a done task's branch when `delete_merged_branches` is true. Failed and cancelled branches stay, so the commits can still be inspected. Tasks that are pending, claimed, running, or in review are left alone. `kiln run` does this sweep at the start of a real tick.

```bash
kiln gc
```

## What a tick does

1. Remove leftover worktrees from finished tasks.
2. Scout a fresh goal once, and run any scout the previous tick queued.
3. Ask the foreman for a fenced JSON action list, then apply it: create tasks, queue a scout, approve, rework, fail, cancel, take a note, or mark the goal done.
4. Merge approved branches. A conflict becomes rework.
5. Dispatch ready tasks, up to `max_parallel_workers`.

Each worker claims one task, checks out `kiln/<id>-<slug>` under `.kiln/worktrees/<id>/`, runs `agent` with `--force --trust`, commits, and optionally runs the `verify` command. The task then waits in review. The foreman and scouts use `--mode ask` and do not edit the tree.

A task that has used `max_attempts` fails instead of starting another attempt.

## Configuration

`kiln.toml`:

| Key | Meaning |
| --- | --- |
| `base_branch` | Branch workers merge into. `kiln init` uses the current branch. |
| `verify` | Shell command run in the worktree after a worker finishes. Empty skips it. |
| `max_parallel_workers` | How many workers one tick may start. Default 2. |
| `max_attempts` | Rework attempts before a task fails. Default 3. |
| `delete_merged_branches` | Delete a task branch after a clean merge. Default true. |
| `models.foreman` | Frontier model for planning and review. |
| `models.worker` | Model that edits code. |
| `models.scout` | Cheaper model for exploration. |

The defaults are `claude-opus-5-thinking-high`, `claude-sonnet-5-thinking-high`, and `composer-2.5`. `KILN_AGENT_BIN` overrides the `agent` executable.
