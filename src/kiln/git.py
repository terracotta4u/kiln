import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from kiln.errors import KilnError

_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class MergeResult:
    merged: bool
    message: str


def branch_name(job_id: int, title: str) -> str:
    return f"kiln/{job_id}-{_slug(title, 'job')}"


def goal_branch_name(goal_id: int, title: str) -> str:
    return f"kiln/goal-{goal_id}-{_slug(title, 'goal')}"


def _slug(title: str, fallback: str) -> str:
    slug = _SLUG.sub("-", title.lower()).strip("-")[:40].strip("-")
    return slug or fallback


def ensure_worktree(repo: Path, worktree: Path, branch: str, base: str) -> None:
    """Create branch at base if needed, and check it out at worktree."""
    worktree = worktree.resolve()
    if (worktree / ".git").exists():
        return
    if worktree.exists() and any(worktree.iterdir()):
        raise KilnError(f"worktree path {worktree} already exists and is not a worktree")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if _branch_exists(repo, branch):
        _git(repo, "worktree", "add", str(worktree), branch)
    else:
        _git(repo, "worktree", "add", "-b", branch, str(worktree), base)


def remove_worktree(repo: Path, worktree: Path) -> None:
    _git(repo, "worktree", "remove", "--force", str(worktree))


def prune_worktrees(repo: Path) -> str:
    result = _git(repo, "worktree", "prune", "-v")
    return result.stdout.strip()


def branch_exists(repo: Path, branch: str) -> bool:
    return _branch_exists(repo, branch)


def ensure_branch(repo: Path, branch: str, base: str) -> None:
    """Create branch at base without checking it out."""
    if _branch_exists(repo, branch):
        return
    _git(repo, "branch", branch, base)


def commits_ahead(repo: Path, base: str, branch: str) -> int:
    result = _git(repo, "rev-list", "--count", f"{base}..{branch}")
    return int(result.stdout.strip() or "0")


def remote_url(repo: Path, remote: str = "origin") -> str | None:
    result = _git(repo, "remote", "get-url", remote, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def push_branch(repo: Path, branch: str, remote: str = "origin") -> None:
    _git(repo, "push", "-u", remote, f"{branch}:{branch}")


def delete_branch(repo: Path, branch: str) -> None:
    _git(repo, "branch", "-D", branch)


def diffstat(repo: Path, base: str, branch: str) -> str:
    result = _git(repo, "diff", "--stat", f"{base}...{branch}")
    return result.stdout.strip()


def branch_diff(repo: Path, base: str, branch: str) -> str:
    """Full diff of commits on branch that are not in base."""
    result = _git(repo, "diff", f"{base}...{branch}")
    return result.stdout


def revision(repo: Path) -> str:
    """Current commit of a checkout."""
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def commit_if_dirty(worktree: Path, message: str) -> bool:
    """Commit tracked and untracked changes. Returns whether a commit was made."""
    status = _git(worktree, "status", "--porcelain")
    if not status.stdout.strip():
        return False
    _git(worktree, "add", "-A")
    _commit(worktree, message)
    return True


def merge_branch(repo: Path, branch: str, base: str) -> MergeResult:
    """Merge branch into base. On conflict, abort and restore the previous checkout."""
    head = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if head != base:
        _git(repo, "checkout", base)
    try:
        result = _git(repo, "merge", "--no-edit", branch, check=False)
        if result.returncode != 0:
            _git(repo, "merge", "--abort", check=False)
            message = (result.stderr or result.stdout).strip()
            return MergeResult(merged=False, message=message or "merge failed")
        return MergeResult(merged=True, message=(result.stdout or "").strip())
    finally:
        if head != base and head != "HEAD":
            _git(repo, "checkout", head, check=False)


def _branch_exists(repo: Path, branch: str) -> bool:
    result = _git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    return result.returncode == 0


def _commit(worktree: Path, message: str) -> None:
    result = _git(worktree, "commit", "-m", message, check=False)
    if result.returncode == 0:
        return
    if _missing_identity(result.stderr):
        result = _git(
            worktree,
            "-c",
            "user.name=Kiln",
            "-c",
            "user.email=kiln@localhost",
            "commit",
            "-m",
            message,
            check=False,
        )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise KilnError(detail or "git commit failed")


def _missing_identity(stderr: str) -> bool:
    return "Author identity unknown" in stderr or "unable to auto-detect email address" in stderr


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    for attempt in range(5):
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
        )
        if not check or result.returncode == 0:
            return result
        detail = (result.stderr or result.stdout).strip()
        if attempt < 4 and _git_lock(detail):
            time.sleep(0.05 * (attempt + 1))
            continue
        raise KilnError(detail or f"git {' '.join(args)} failed")
    raise KilnError(f"git {' '.join(args)} failed")


def _git_lock(detail: str) -> bool:
    return "index.lock" in detail or "Another git process" in detail or "could not lock" in detail
