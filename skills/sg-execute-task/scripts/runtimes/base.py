from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol


VERDICT_SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "verdict.schema.json"
VERDICT_SCHEMA = json.loads(VERDICT_SCHEMA_PATH.read_text(encoding="utf-8"))

OUTCOME_COMPLETED = "completed"
OUTCOME_BLOCKED = "blocked"
OUTCOME_FAIL = "fail"


def validate_verdict(verdict: object) -> bool:
    if not isinstance(verdict, dict) or type(verdict.get("passed")) is not bool:
        return False

    typed_fields = {
        "summary": str,
        "error": str,
        "blocked": bool,
        "blocked_reason": str,
    }
    if any(key in verdict and type(verdict[key]) is not expected
           for key, expected in typed_fields.items()):
        return False
    if verdict["passed"] is False and "error" not in verdict:
        return False
    if verdict.get("blocked") is True and "blocked_reason" not in verdict:
        return False
    return True


def classify_outcome(verdict: Optional[dict], kill_reason: Optional[str]) -> str:
    if kill_reason:
        return OUTCOME_FAIL
    if not isinstance(verdict, dict):
        return OUTCOME_FAIL
    if verdict.get("blocked") is True:
        return OUTCOME_BLOCKED
    if verdict.get("passed") is True:
        return OUTCOME_COMPLETED
    return OUTCOME_FAIL


@dataclass
class AttemptResult:
    outcome: str
    verdict: Optional[dict]
    kill_reason: Optional[str]
    signal: Optional[int]
    elapsed: float
    last_activity_age: float
    return_code: Optional[int]
    stderr_tail: str
    stdout_tail: str
    saw_result_with_structured_output: bool
    runtime: str = "claude"


def attempt_error_message(result: AttemptResult) -> str:
    if result.kill_reason:
        return (f"{result.kill_reason} (elapsed {result.elapsed:.0f}s, "
                f"silent for {result.last_activity_age:.0f}s)")
    if isinstance(result.verdict, dict) and result.verdict.get("error"):
        return str(result.verdict["error"])
    return f"the child returned no usable verdict (exit code {result.return_code})"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"  WARN: {name}={raw!r} is not an integer; using the default {default}")
        return default


BASH_MAX_TIMEOUT_MS = _env_int("SG_BASH_MAX_TIMEOUT_MS", 8 * 60 * 1000)
T_IDLE_SEC = _env_int("SG_T_IDLE_SEC", 12 * 60)
T_MAX_SEC = _env_int("SG_T_MAX_SEC", 90 * 60)
MAX_TURNS = _env_int("SG_MAX_TURNS", 50)

POLL_INTERVAL_SEC = 0.05
KILL_GRACE_SEC = 5.0
READER_JOIN_SEC = 5.0
TAIL_LINES = 50
TAIL_CHARS = 4000

DECISION_KEEP = "keep"
DECISION_KILL_IDLE = "kill-idle"
DECISION_KILL_WALL = "kill-wall"


def timeout_decision(now: float, last_activity: float, start: float,
                     t_idle: float, t_max: float) -> str:
    if now - start > t_max:
        return DECISION_KILL_WALL
    if now - last_activity > t_idle:
        return DECISION_KILL_IDLE
    return DECISION_KEEP


class ProcessState:
    def __init__(self, *, now: Callable[[], float] = time.monotonic):
        self._now = now
        self.last_activity = now()
        self.stdout_tail = deque(maxlen=TAIL_LINES)
        self.stderr_tail = deque(maxlen=TAIL_LINES)

    def on_stdout_line(self, line: str):
        self.last_activity = self._now()
        self.stdout_tail.append(line)

    def on_stderr_line(self, line: str):
        self.stderr_tail.append(line)


def _tail_text(lines) -> str:
    text = "".join(lines)
    return text[-TAIL_CHARS:]


def _drain(stream, on_line: Callable[[str], None]):
    with contextlib.suppress(OSError, ValueError):
        for line in iter(stream.readline, ""):
            on_line(line)


def _kill_process_group(proc: subprocess.Popen, grace: float) -> Optional[int]:
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return None

    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
        return int(signal.SIGTERM)
    except subprocess.TimeoutExpired:
        pass

    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=grace)
    return int(signal.SIGKILL)


def run_process(cmd: list, cwd: str, env: dict, *, runtime: str,
                state: Optional[ProcessState] = None) -> AttemptResult:
    state = state or ProcessState()
    start = time.monotonic()
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )

    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, state.on_stdout_line), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, state.on_stderr_line), daemon=True),
    ]
    for thread in readers:
        thread.start()

    kill_reason = None
    sig = None
    while proc.poll() is None:
        decision = timeout_decision(time.monotonic(), state.last_activity, start,
                                    T_IDLE_SEC, T_MAX_SEC)
        if decision != DECISION_KEEP:
            kill_reason = decision
            sig = _kill_process_group(proc, KILL_GRACE_SEC)
            break
        time.sleep(POLL_INTERVAL_SEC)

    for thread in readers:
        thread.join(timeout=READER_JOIN_SEC)
    for pipe in (proc.stdout, proc.stderr):
        with contextlib.suppress(OSError, ValueError):
            pipe.close()

    now = time.monotonic()
    return AttemptResult(
        outcome=OUTCOME_FAIL,
        verdict=None,
        kill_reason=kill_reason,
        signal=sig,
        elapsed=round(now - start, 3),
        last_activity_age=round(now - state.last_activity, 3),
        return_code=proc.returncode,
        stderr_tail=_tail_text(state.stderr_tail),
        stdout_tail=_tail_text(state.stdout_tail),
        saw_result_with_structured_output=False,
        runtime=runtime,
    )


class AgentRuntime(Protocol):
    name: str
    instruction_files: tuple[str, ...]

    def preflight(self) -> None: ...

    def run(self, prompt: str, cwd: str): ...
