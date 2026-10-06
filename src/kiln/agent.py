"""Run the Cursor `agent` CLI and pull a JSON report out of its reply.

Probed with `agent -p` on 2026-10-04:

- `--output-format json` prints one object:
  `{"type":"result","subtype":"success","is_error":false,"result":"<assistant text>",...}`
- `--mode ask` is read-only and works together with `-p`.
- `--trust` skips the workspace-trust prompt. Pass it on every non-interactive run
  so a fresh worktree cannot hang waiting for a person.
"""

import json
import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from kiln.errors import KilnError

DEFAULT_TIMEOUT_SECONDS = 600
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_LIVE: set[subprocess.Popen[str]] = set()
_LIVE_LOCK = threading.Lock()
_SHUTTING_DOWN = False


@dataclass(frozen=True)
class ParsedOutput:
    text: str
    report: dict | None
    envelope: dict | None

    @property
    def is_error(self) -> bool:
        return bool(self.envelope and self.envelope.get("is_error"))


@dataclass(frozen=True)
class AgentResult:
    exit_code: int
    stdout: str
    parsed: ParsedOutput
    timed_out: bool
    log_path: Path

    @property
    def report(self) -> dict | None:
        return self.parsed.report


def default_agent_bin() -> str:
    return os.environ.get("KILN_AGENT_BIN", "agent")


def kill_live_agents() -> None:
    """Kill every agent process this interpreter still has running."""
    with _LIVE_LOCK:
        processes = list(_LIVE)
    for process in processes:
        _kill(process)


def shutdown_agents() -> None:
    """Refuse new agents, then kill the ones already running.

    The shutdown flag and the snapshot of live processes are taken under the
    same lock that run_agent holds across Popen and registration, so a spawn
    cannot land in the gap and outlive the server.
    """
    global _SHUTTING_DOWN
    with _LIVE_LOCK:
        _SHUTTING_DOWN = True
        processes = list(_LIVE)
    for process in processes:
        _kill(process)


def build_command(
    *,
    prompt: str,
    model: str,
    workspace: Path,
    mode: str | None = None,
    force: bool = False,
    trust: bool = True,
    agent_bin: str | None = None,
) -> list[str]:
    command = [
        agent_bin or default_agent_bin(),
        "-p",
        "--output-format",
        "json",
        "--workspace",
        str(workspace),
        "--model",
        model,
    ]
    if trust:
        command.append("--trust")
    if mode:
        command.extend(["--mode", mode])
    if force:
        command.append("--force")
    command.append(prompt)
    return command


def parse_agent_output(stdout: str) -> ParsedOutput:
    """Unwrap the JSON envelope, then take the last fenced JSON object."""
    raw = stdout.strip()
    envelope: dict | None = None
    text = raw
    parsed = _json_object(raw)
    if parsed is not None and parsed.get("type") == "result" and isinstance(parsed.get("result"), str):
        envelope = parsed
        text = parsed["result"]
    return ParsedOutput(text=text, report=_extract_report(text), envelope=envelope)


def run_agent(
    *,
    prompt: str,
    model: str,
    workspace: Path,
    log_path: Path,
    mode: str | None = None,
    force: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    agent_bin: str | None = None,
) -> AgentResult:
    command = build_command(
        prompt=prompt,
        model=model,
        workspace=workspace,
        mode=mode,
        force=force,
        agent_bin=agent_bin,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with _LIVE_LOCK:
        if _SHUTTING_DOWN:
            raise KilnError("server is shutting down")
        try:
            process = subprocess.Popen(
                command,
                cwd=workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise KilnError(f"agent executable not found: {command[0]}") from exc
        _LIVE.add(process)
    try:
        stdout = _capture(process, command, prompt, log_path, timeout)
    finally:
        with _LIVE_LOCK:
            _LIVE.discard(process)
    timed_out = stdout is None
    if timed_out:
        captured = _read_log_output(log_path)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n--- timed out after {timeout:g}s ---\n")
    else:
        captured = stdout
    code = process.returncode if process.returncode is not None else 1
    return AgentResult(
        exit_code=code,
        stdout=captured,
        parsed=parse_agent_output(captured),
        timed_out=timed_out,
        log_path=log_path,
    )


def _capture(
    process: subprocess.Popen[str],
    command: list[str],
    prompt: str,
    log_path: Path,
    timeout: float,
) -> str | None:
    """Stream stdout into the log. Returns the captured text, or None on timeout."""
    assert process.stdout is not None
    chunks: list[str] = []

    def reader() -> None:
        with log_path.open("w", encoding="utf-8") as log:
            shown = command[:-1] + ["<prompt>"]
            log.write("$ " + " ".join(shown) + "\n--- prompt ---\n")
            log.write(prompt)
            log.write("\n--- output ---\n")
            while True:
                chunk = process.stdout.read(4096) if process.stdout is not None else ""
                if not chunk:
                    break
                chunks.append(chunk)
                log.write(chunk)
                log.flush()

    thread = threading.Thread(target=reader)
    thread.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill(process)
    thread.join()
    if timed_out:
        return None
    return "".join(chunks)


def _kill(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _read_log_output(log_path: Path) -> str:
    if not log_path.is_file():
        return ""
    text = log_path.read_text(encoding="utf-8")
    marker = "\n--- output ---\n"
    index = text.find(marker)
    if index == -1:
        return ""
    return text[index + len(marker) :]


def _extract_report(text: str) -> dict | None:
    found: dict | None = None
    for match in _FENCE.finditer(text):
        obj = _json_object(match.group(1))
        if obj is not None:
            found = obj
    if found is not None:
        return found
    return _json_object(text)


def _json_object(text: str) -> dict | None:
    try:
        value = json.loads(text.strip())
    except json.JSONDecodeError:
        return None
    if isinstance(value, dict):
        return value
    return None
