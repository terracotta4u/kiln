import subprocess
from pathlib import Path

import pytest

from kiln.config import init_factory, load_config
from kiln.errors import KilnError

_MINIMAL = """
base_branch = "main"
verify = ""
max_parallel_workers = 2
max_attempts = 3
delete_merged_branches = true

[models]
foreman = "foreman"
worker = "worker"
scout = "scout"
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def test_missing_harness_defaults_to_cursor(repo: Path):
    (repo / "kiln.toml").write_text(_MINIMAL)
    assert load_config(repo).harness == "cursor"


def test_explicit_codex_harness(repo: Path):
    (repo / "kiln.toml").write_text('harness = "codex"\n' + _MINIMAL)
    assert load_config(repo).harness == "codex"


@pytest.mark.parametrize(
    "snippet",
    [
        'harness = "claude"',
        'harness = "Cursor"',
        'harness = "CODEX"',
        'harness = ""',
        "harness = 1",
        "harness = true",
    ],
)
def test_invalid_harness_is_rejected(repo: Path, snippet: str):
    (repo / "kiln.toml").write_text(snippet + "\n" + _MINIMAL)
    with pytest.raises(KilnError, match=r'kiln.toml harness must be "cursor" or "codex"'):
        load_config(repo)


def test_init_template_includes_cursor_harness_and_loads(repo: Path):
    config, created = init_factory(repo)
    assert created
    text = (repo / "kiln.toml").read_text()
    assert 'harness = "cursor"' in text
    assert config.harness == "cursor"
    assert load_config(repo).harness == "cursor"
