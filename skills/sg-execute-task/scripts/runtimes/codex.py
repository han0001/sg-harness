from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Callable, NoReturn, Optional

from .base import (
    AttemptResult,
    OUTCOME_FAIL,
    VERDICT_SCHEMA_PATH,
    classify_outcome,
    run_process,
    validate_verdict,
)


REQUIRED_CLI_CAPABILITIES = (
    "--json",
    "--ephemeral",
    "--sandbox",
    "--output-schema",
    "--output-last-message",
)


def _preflight_abort(problem: str) -> NoReturn:
    print(f"\n  ✗ Preflight failed: {problem}")
    print("  This harness requires `codex exec` with --json, --ephemeral, "
          "--sandbox, --output-schema, and --output-last-message support.")
    print("  Fix: update the Codex CLI, authenticate it, then re-run this command.")
    sys.exit(1)


def _probe_cli(run: Callable[..., subprocess.CompletedProcess], args: list[str]) -> str:
    argv = ["codex"] + args
    try:
        proc = run(argv, capture_output=True, text=True)
    except OSError as error:
        _preflight_abort(f"could not execute `{' '.join(argv)}` ({error})")
    if proc.returncode != 0:
        _preflight_abort(f"`{' '.join(argv)}` exited with code {proc.returncode}: "
                         f"{(proc.stderr or '').strip()[:200]}")
    return proc.stdout or ""


def preflight_check(run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
    _probe_cli(run, ["--version"])
    help_text = _probe_cli(run, ["exec", "--help"])
    missing = [cap for cap in REQUIRED_CLI_CAPABILITIES if cap not in help_text]
    if missing:
        _preflight_abort(f"this codex does not advertise {', '.join(missing)}")
    _probe_cli(run, ["login", "status"])


def _read_verdict(path: Path) -> Optional[dict]:
    try:
        verdict = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return verdict if validate_verdict(verdict) else None


def _run_codex_process(cmd: list, cwd: str, env: dict) -> AttemptResult:
    return run_process(cmd, cwd, env, runtime="codex")


class CodexRuntime:
    name = "codex"
    instruction_files = ("AGENTS.md", "CLAUDE.md")

    def __init__(self, *, child_runner: Callable[..., AttemptResult] = _run_codex_process):
        self._child_runner = child_runner

    def preflight(self) -> None:
        preflight_check()

    def run(self, prompt: str, cwd: str) -> AttemptResult:
        with tempfile.TemporaryDirectory(prefix=".sg-codex-", dir=cwd) as temp_dir:
            verdict_path = Path(temp_dir) / "verdict.json"
            cmd = [
                "codex", "exec", "--json", "--ephemeral",
                "--sandbox", "workspace-write",
                "--output-schema", str(VERDICT_SCHEMA_PATH),
                "-o", str(verdict_path),
                prompt,
            ]
            process_result = self._child_runner(cmd, cwd=cwd, env=dict(os.environ))
            verdict = _read_verdict(verdict_path)

        outcome = classify_outcome(verdict, process_result.kill_reason)
        if process_result.return_code != 0:
            outcome = OUTCOME_FAIL
        return replace(
            process_result,
            outcome=outcome,
            verdict=verdict,
            saw_result_with_structured_output=verdict is not None,
            runtime=self.name,
        )
