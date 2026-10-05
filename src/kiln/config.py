import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from kiln.db import connect, migrate
from kiln.errors import KilnError

DEFAULT_FOREMAN_MODEL = "claude-opus-5-thinking-high"
DEFAULT_WORKER_MODEL = "claude-sonnet-5-thinking-high"
DEFAULT_SCOUT_MODEL = "composer-2.5"
DEFAULT_REVIEWER_MODEL = "claude-sonnet-5-thinking-high"
DEFAULT_MAX_PARALLEL_WORKERS = 2
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_FOREMAN_TURNS = 25

KILN_DIRNAME = ".kiln"
CONFIG_FILENAME = "kiln.toml"
DB_FILENAME = "kiln.db"


@dataclass(frozen=True)
class Models:
    foreman: str
    worker: str
    scout: str
    reviewer: str


@dataclass(frozen=True)
class Config:
    repo_root: Path
    kiln_dir: Path
    db_path: Path
    toml_path: Path
    base_branch: str
    verify: str
    max_parallel_workers: int
    max_attempts: int
    max_foreman_turns: int
    delete_merged_branches: bool
    models: Models

    @property
    def runs_dir(self) -> Path:
        return self.kiln_dir / "runs"

    @property
    def worktrees_dir(self) -> Path:
        return self.kiln_dir / "worktrees"


def repo_root(start: Path | None = None) -> Path:
    cwd = start or Path.cwd()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise KilnError("not inside a git repository")
    return Path(result.stdout.strip()).resolve()


def current_branch(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    name = result.stdout.strip()
    if result.returncode != 0 or not name or name == "HEAD":
        return "main"
    return name


def load_config(start: Path | None = None) -> Config:
    root = repo_root(start)
    toml_path = root / CONFIG_FILENAME
    if not toml_path.is_file():
        raise KilnError("no kiln.toml found; run `kiln init` first")
    with toml_path.open("rb") as handle:
        data = tomllib.load(handle)
    return _config_from_data(root, toml_path, data)


def init_factory(start: Path | None = None) -> tuple[Config, bool]:
    """Create kiln.toml, .kiln/, and the database. Returns whether the toml was new."""
    root = repo_root(start)
    toml_path = root / CONFIG_FILENAME
    created = not toml_path.exists()
    if created:
        toml_path.write_text(_toml_template(current_branch(root)))
    _ensure_gitignore(root)
    config = load_config(root)
    conn = connect(config.db_path)
    try:
        migrate(conn)
    finally:
        conn.close()
    return config, created


def _config_from_data(root: Path, toml_path: Path, data: dict) -> Config:
    models_data = data.get("models")
    if not isinstance(models_data, dict):
        raise KilnError("kiln.toml is missing [models]")
    kiln_dir = root / KILN_DIRNAME
    return Config(
        repo_root=root,
        kiln_dir=kiln_dir,
        db_path=kiln_dir / DB_FILENAME,
        toml_path=toml_path,
        base_branch=_require_str(data, "base_branch"),
        verify=_require_str(data, "verify", allow_empty=True),
        max_parallel_workers=_require_positive_int(data, "max_parallel_workers"),
        max_attempts=_require_positive_int(data, "max_attempts"),
        max_foreman_turns=_optional_positive_int(data, "max_foreman_turns", DEFAULT_MAX_FOREMAN_TURNS),
        delete_merged_branches=_require_bool(data, "delete_merged_branches"),
        models=Models(
            foreman=_require_str(models_data, "foreman", label="models.foreman"),
            worker=_require_str(models_data, "worker", label="models.worker"),
            scout=_require_str(models_data, "scout", label="models.scout"),
            reviewer=_optional_str(models_data, "reviewer", DEFAULT_REVIEWER_MODEL, label="models.reviewer"),
        ),
    )


def _require_str(data: dict, key: str, *, allow_empty: bool = False, label: str | None = None) -> str:
    name = label or key
    value = data.get(key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise KilnError(f"kiln.toml is missing {name}")
    return value


def _require_bool(data: dict, key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise KilnError(f"kiln.toml is missing {key}")
    return value


def _optional_str(data: dict, key: str, default: str, *, label: str | None = None) -> str:
    if key not in data:
        return default
    return _require_str(data, key, label=label)


def _optional_positive_int(data: dict, key: str, default: int) -> int:
    if key not in data:
        return default
    return _require_positive_int(data, key)


def _require_positive_int(data: dict, key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise KilnError(f"kiln.toml {key} must be an integer >= 1")
    return value


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_template(base_branch: str) -> str:
    return f"""# Kiln factory settings for this repository.

# Branch the pull request targets.
base_branch = {_toml_string(base_branch)}

# Command run in the worktree after a worker finishes. Empty skips verification.
verify = ""

# How many scouts, workers, and reviewers one turn may run at once.
max_parallel_workers = {DEFAULT_MAX_PARALLEL_WORKERS}

# Rework attempts before a task is failed.
max_attempts = {DEFAULT_MAX_ATTEMPTS}

# Foreman turns one `kiln run` may take before it stops.
max_foreman_turns = {DEFAULT_MAX_FOREMAN_TURNS}

# Delete a task branch after it merges into the goal branch.
delete_merged_branches = true

[models]
foreman = {_toml_string(DEFAULT_FOREMAN_MODEL)}
worker = {_toml_string(DEFAULT_WORKER_MODEL)}
scout = {_toml_string(DEFAULT_SCOUT_MODEL)}
reviewer = {_toml_string(DEFAULT_REVIEWER_MODEL)}
"""


def _ensure_gitignore(root: Path) -> None:
    path = root / ".gitignore"
    text = path.read_text() if path.exists() else ""
    if any(line.strip() in {".kiln", ".kiln/"} for line in text.splitlines()):
        return
    with path.open("a") as handle:
        if text and not text.endswith("\n"):
            handle.write("\n")
        handle.write(".kiln/\n")
