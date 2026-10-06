"""Where the per-machine server keeps its socket, pid, and log.

Factory state stays in each repository's .kiln/. This directory is only the
runtime: nothing here is the source of truth for a factory.
"""

import os
from pathlib import Path

SERVER_DIRNAME = ".kiln"


def server_home() -> Path:
    """$KILN_HOME, or ~/.kiln. Tests set KILN_HOME to a short directory."""
    override = os.environ.get("KILN_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / SERVER_DIRNAME).resolve()


def socket_path() -> Path:
    return server_home() / "server.sock"


def pid_path() -> Path:
    return server_home() / "server.pid"


def log_path() -> Path:
    return server_home() / "server.log"


def lock_path() -> Path:
    return server_home() / "server.lock"
