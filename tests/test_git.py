import os
import subprocess
from pathlib import Path

import pytest

from kiln.errors import KilnError
from kiln.git import (
    commit_if_dirty,
    delete_branch,
    diffstat,
    ensure_worktree,
    merge_branch,
    remove_worktree,
)


def test_worktree_commit_and_diffstat(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text(".kiln/\n")
    worktree = repo / ".kiln" / "worktrees" / "1"
    ensure_worktree(repo, worktree, "kiln/1-demo", "main")
    assert (worktree / "README.md").is_file()

    (worktree / "extra.txt").write_text("x\n")
    assert commit_if_dirty(worktree, "add extra")
    assert "extra.txt" in diffstat(repo, "main", "kiln/1-demo")
    assert not (repo / "extra.txt").exists()

    remove_worktree(repo, worktree)
    assert not worktree.exists()
    delete_branch(repo, "kiln/1-demo")
    with pytest.raises(KilnError):
        delete_branch(repo, "kiln/1-demo")


def test_merge_success_and_conflict(tmp_path: Path):
    repo = _repo(tmp_path)
    _commit_on_branch(repo, "feature", "extra.txt", "x\n")
    merged = merge_branch(repo, "feature", "main")
    assert merged.merged
    assert (repo / "extra.txt").read_text() == "x\n"

    subprocess.run(["git", "checkout", "-b", "other"], cwd=repo, check=True)
    (repo / "README.md").write_text("other\n")
    subprocess.run(["git", "commit", "-am", "other"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True)
    (repo / "README.md").write_text("main\n")
    subprocess.run(["git", "commit", "-am", "main"], cwd=repo, check=True, capture_output=True)

    conflict = merge_branch(repo, "other", "main")
    assert conflict.merged is False
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout.strip() == ""
    assert (repo / "README.md").read_text() == "main\n"


def test_commit_supplies_an_identity_when_git_has_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _repo(tmp_path)
    subprocess.run(["git", "config", "--unset", "user.name"], cwd=repo, check=True)
    subprocess.run(["git", "config", "--unset", "user.email"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.useConfigOnly", "true"], cwd=repo, check=True)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(key, raising=False)

    worktree = repo / ".kiln" / "worktrees" / "1"
    ensure_worktree(repo, worktree, "kiln/1-demo", "main")
    (worktree / "extra.txt").write_text("x\n")
    assert commit_if_dirty(worktree, "kiln commit")
    log = subprocess.run(
        ["git", "log", "-1", "--format=%an", "kiln/1-demo"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert log.stdout.strip() == "Kiln"


def _repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "kiln@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Kiln"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    return path


def _commit_on_branch(repo: Path, branch: str, name: str, content: str) -> None:
    subprocess.run(["git", "checkout", "-b", branch], cwd=repo, check=True, capture_output=True)
    (repo / name).write_text(content)
    subprocess.run(["git", "add", name], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", name], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
