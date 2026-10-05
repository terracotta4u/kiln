"""Run a coding-agent CLI and pull a JSON report out of its reply.

Cursor, probed with `agent -p` on 2026-10-04:

- `--output-format json` prints one object:
  `{"type":"result","subtype":"success","is_error":false,"result":"<assistant text>",...}`
- `--mode ask` is read-only and works together with `-p`.
- `--trust` skips the workspace-trust prompt. Pass it on every non-interactive run
  so a fresh worktree cannot hang waiting for a person.

Codex, probed with `codex exec --json` on 2026-10-05 (codex-cli 0.159.2, `--sandbox
read-only`, model `gpt-6-luna`, a one-word prompt in a temp directory):

- `--json` prints JSONL, one event per line. A successful turn was:
  `{"type":"thread.started","thread_id":"..."}`
  `{"type":"turn.started"}`
  `{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"pong"}}`
  `{"type":"turn.completed","usage":{"input_tokens":13501,"cached_input_tokens":11008,"output_tokens":5,"reasoning_output_tokens":0}}`
- A failed turn (unknown model, exit 1) emitted an error item, then:
  `{"type":"error","message":"..."}`
  `{"type":"turn.failed","error":{"message":"..."}}`
- The assistant text is the last `agent_message` item's `text`. Events with
  `type` `error` or `turn.failed`, and items with `type` `error`, set `is_error`.
"""

import json
import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kiln.errors import KilnError

DEFAULT_TIMEOUT_SECONDS = 600
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_CODEX_EVENT_TYPES = frozenset(
    {
        "error",
        "item.completed",
        "item.started",
        "item.updated",
        "thread.started",
        "turn.completed",
        "turn.failed",
        "turn.started",
    }
)


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


class Harness(Protocol):
    name: str

    def default_bin(self) -> str: ...

    def build_command(
        self,
        *,
        prompt: str,
        model: str,
        workspace: Path,
        readonly: bool,
        bin: str | None,
    ) -> list[str]: ...

    def parse_output(self, stdout: str) -> ParsedOutput: ...


class CursorHarness:
    name = "cursor"

    def default_bin(self) -> str:
        return os.environ.get("KILN_AGENT_BIN", "agent")

    def build_command(
        self,
        *,
        prompt: str,
        model: str,
        workspace: Path,
        readonly: bool,
        bin: str | None,
    ) -> list[str]:
        return _cursor_argv(
            prompt=prompt,
            model=model,
            workspace=workspace,
            mode="ask" if readonly else None,
            force=not readonly,
            trust=True,
            executable=bin or self.default_bin(),
        )

    def parse_output(self, stdout: str) -> ParsedOutput:
        """Unwrap the JSON envelope, then take the last fenced JSON object."""
        raw = stdout.strip()
        envelope: dict | None = None
        text = raw
        parsed = _json_object(raw)
        if parsed is not None and parsed.get("type") == "result" and isinstance(parsed.get("result"), str):
            envelope = parsed
            text = parsed["result"]
        return ParsedOutput(text=text, report=_extract_report(text), envelope=envelope)


class CodexHarness:
    name = "codex"

    def default_bin(self) -> str:
        return os.environ.get("KILN_CODEX_BIN", "codex")

    def build_command(
        self,
        *,
        prompt: str,
        model: str,
        workspace: Path,
        readonly: bool,
        bin: str | None,
    ) -> list[str]:
        sandbox = "read-only" if readonly else "workspace-write"
        return [
            bin or self.default_bin(),
            "exec",
            "-m",
            model,
            "-C",
            str(workspace),
            "--skip-git-repo-check",
            "--color",
            "never",
            "--sandbox",
            sandbox,
            "--json",
            prompt,
        ]

    def parse_output(self, stdout: str) -> ParsedOutput:
        text, is_error, saw_events = _codex_jsonl(stdout)
        if not saw_events:
            raw = stdout.strip()
            return ParsedOutput(text=raw, report=_extract_report(raw), envelope=None)
        return ParsedOutput(
            text=text,
            report=_extract_report(text),
            envelope={"is_error": is_error, "result": text},
        )


def get_harness(name: str) -> Harness:
    if name == "cursor":
        return CursorHarness()
    if name == "codex":
        return CodexHarness()
    raise KilnError(f"unknown harness: {name}")


def default_agent_bin() -> str:
    return CursorHarness().default_bin()


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
    return _cursor_argv(
        prompt=prompt,
        model=model,
        workspace=workspace,
        mode=mode,
        force=force,
        trust=trust,
        executable=agent_bin or default_agent_bin(),
    )


def parse_agent_output(stdout: str) -> ParsedOutput:
    """Unwrap the JSON envelope, then take the last fenced JSON object."""
    return CursorHarness().parse_output(stdout)


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
    harness: Harness | None = None,
    readonly: bool | None = None,
) -> AgentResult:
    """Run ``harness`` (Cursor by default).

    When ``readonly`` is omitted, ``mode="ask"`` is read-only and ``force=True``
    is writable. Cursor calls that pass only those legacy flags keep today's
    command line, including a run that sets neither.
    """
    selected: Harness = harness if harness is not None else CursorHarness()
    if isinstance(selected, CursorHarness) and readonly is None:
        command = build_command(
            prompt=prompt,
            model=model,
            workspace=workspace,
            mode=mode,
            force=force,
            agent_bin=agent_bin,
        )
    else:
        command = selected.build_command(
            prompt=prompt,
            model=model,
            workspace=workspace,
            readonly=_resolve_readonly(mode=mode, force=force, readonly=readonly),
            bin=agent_bin,
        )
    log_path.parent.mkdir(parents=True, exist_ok=True)
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

    stdout = _capture(process, command, prompt, log_path, timeout)
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
        parsed=selected.parse_output(captured),
        timed_out=timed_out,
        log_path=log_path,
    )


def _resolve_readonly(*, mode: str | None, force: bool, readonly: bool | None) -> bool:
    """Explicit ``readonly`` wins. Otherwise ``force=True`` writes and ``mode="ask"`` does not.

    Codex only has read-only and workspace-write, so a call that sets neither
    flag is read-only too. ``mode`` is accepted so the legacy ask flag stays
    part of the call, and every non-force value takes that read-only path.
    """
    if readonly is not None:
        return readonly
    if force:
        return False
    if mode == "ask":
        return True
    return True


def _cursor_argv(
    *,
    prompt: str,
    model: str,
    workspace: Path,
    mode: str | None,
    force: bool,
    trust: bool,
    executable: str,
) -> list[str]:
    command = [
        executable,
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


def _codex_jsonl(stdout: str) -> tuple[str, bool, bool]:
    """Return ``(assistant text, is_error, saw_events)`` from Codex JSONL."""
    text = ""
    is_error = False
    saw_events = False
    for line in stdout.splitlines():
        event = _json_object(line)
        if event is None or event.get("type") not in _CODEX_EVENT_TYPES:
            continue
        saw_events = True
        if event.get("type") in {"error", "turn.failed"}:
            is_error = True
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            text = item["text"]
        elif item.get("type") == "error":
            is_error = True
    return text, is_error, saw_events


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
