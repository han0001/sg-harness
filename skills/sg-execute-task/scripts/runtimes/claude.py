import json
import os
import re
import subprocess
import sys
import time
from dataclasses import replace
from typing import Callable, NoReturn, Optional

from .base import (
    AttemptResult,
    BASH_MAX_TIMEOUT_MS,
    DECISION_KEEP,
    DECISION_KILL_IDLE,
    DECISION_KILL_WALL,
    KILL_GRACE_SEC,
    MAX_TURNS,
    OUTCOME_FAIL,
    ProcessState,
    READER_JOIN_SEC,
    TAIL_CHARS,
    TAIL_LINES,
    T_IDLE_SEC,
    T_MAX_SEC,
    VERDICT_SCHEMA,
    classify_outcome,
    run_process,
    timeout_decision,
    validate_verdict,
)


MIN_CLAUDE_VERSION = (2, 1, 216)
REQUIRED_CLI_CAPABILITIES = ("--json-schema", "--output-format", "stream-json")
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _parse_cli_version(text: str) -> Optional[tuple]:
    match = _VERSION_RE.search(text or "")
    return tuple(int(g) for g in match.groups()) if match else None


def _preflight_abort(problem: str) -> NoReturn:
    floor = ".".join(str(n) for n in MIN_CLAUDE_VERSION)
    print(f"\n  ✗ Preflight failed: {problem}")
    print(f"  This harness requires the `claude` CLI >= {floor}, supporting "
          f"`--json-schema` and `--output-format stream-json`.")
    print(f"  Fix: run `claude update` (or `npm install -g @anthropic-ai/claude-code@latest`), "
          f"then re-run this command.")
    sys.exit(1)


def _probe_cli(run: Callable[..., subprocess.CompletedProcess], args: list) -> str:
    argv = ["claude"] + args
    try:
        proc = run(argv, capture_output=True, text=True)
    except OSError as e:
        _preflight_abort(f"could not execute `{' '.join(argv)}` ({e})")
    if proc.returncode != 0:
        _preflight_abort(f"`{' '.join(argv)}` exited with code {proc.returncode}: "
                         f"{(proc.stderr or '').strip()[:200]}")
    return proc.stdout or ""


def preflight_check(run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
    version = _parse_cli_version(_probe_cli(run, ["--version"]))
    if version is None:
        _preflight_abort("`claude --version` printed no recognisable version number")
    if version < MIN_CLAUDE_VERSION:
        found = ".".join(str(n) for n in version)
        _preflight_abort(f"the installed claude is {found}, older than the required floor")

    help_text = _probe_cli(run, ["--help"])
    missing = [cap for cap in REQUIRED_CLI_CAPABILITIES if cap not in help_text]
    if missing:
        _preflight_abort(f"this claude does not advertise {', '.join(missing)}")


def is_result_event(obj: dict) -> bool:
    if not isinstance(obj, dict):
        return False
    return any(obj.get(key) == "result" for key in ("type", "role"))


def parse_verdict(result_event: dict) -> Optional[dict]:
    if not isinstance(result_event, dict):
        return None
    verdict = result_event.get("structured_output")
    return verdict if validate_verdict(verdict) else None


def _safe_json_loads(line: str) -> Optional[dict]:
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


class StreamState(ProcessState):
    def __init__(self, *, now: Callable[[], float] = time.monotonic):
        super().__init__(now=now)
        self.result_event: Optional[dict] = None

    def on_stdout_line(self, line: str):
        super().on_stdout_line(line)
        obj = _safe_json_loads(line)
        if obj is not None and is_result_event(obj):
            self.result_event = obj


def run_child(cmd: list, cwd: str, env: dict) -> AttemptResult:
    state = StreamState()
    process_result = run_process(cmd, cwd, env, runtime="claude", state=state)
    verdict = parse_verdict(state.result_event)
    outcome = classify_outcome(verdict, process_result.kill_reason)
    if process_result.return_code != 0:
        outcome = OUTCOME_FAIL
    return replace(
        process_result,
        outcome=outcome,
        verdict=verdict,
        saw_result_with_structured_output=verdict is not None,
    )


class ClaudeRuntime:
    name = "claude"
    instruction_files = ("CLAUDE.md", "AGENTS.md")

    def preflight(self) -> None:
        preflight_check()

    def run(self, prompt: str, cwd: str) -> AttemptResult:
        cmd = [
            "claude", "-p", "--dangerously-skip-permissions",
            "--output-format", "stream-json", "--verbose",
            "--json-schema", json.dumps(VERDICT_SCHEMA),
            "--max-turns", str(MAX_TURNS),
            prompt,
        ]
        env = {
            **os.environ,
            "BASH_MAX_TIMEOUT_MS": str(BASH_MAX_TIMEOUT_MS),
            "BASH_DEFAULT_TIMEOUT_MS": str(BASH_MAX_TIMEOUT_MS),
        }
        return run_child(cmd, cwd=cwd, env=env)
