#!/usr/bin/env python3
"""
Harness Step Executor — runs the steps within a task sequentially and self-corrects.

Usage (run from the user's project root):
    python3 "${CLAUDE_PLUGIN_ROOT}/scripts/execute.py" <task-dir> [--push]

ROOT (the target of the work) is the git root of cwd, independent of the script's own location.
"""

import argparse
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import types
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, NoReturn, Optional

def _find_project_root() -> Path:
    """Find the root of the project being worked on.

    When installed as a plugin, this script lives under ~/.claude/plugins/.../scripts/,
    so deriving the root from __file__ would point at the 'plugin install directory'
    rather than the 'user's project'. Hence we use the git root of where the user ran it
    (cwd), not the script's own location.
    Falls back to cwd if it is not a git repo (_checkout_branch then clearly reports the
    absence of git).
    """
    r = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if r.returncode == 0 and r.stdout.strip():
        return Path(r.stdout.strip())
    return Path.cwd()


ROOT = _find_project_root()

TZ = timezone(timedelta(hours=9))


def kst_stamp() -> str:
    """The timestamp every state transition is recorded with (KST, e.g. 2026-08-03T14:11:05+0900)."""
    return datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S%z")


@contextlib.contextmanager
def progress_indicator(label: str):
    """Terminal progress indicator. Use with a `with` statement; read elapsed time via .elapsed."""
    frames = "◐◓◑◒"
    stop = threading.Event()
    t0 = time.monotonic()

    def _animate():
        idx = 0
        while not stop.wait(0.12):
            sec = int(time.monotonic() - t0)
            sys.stderr.write(f"\r{frames[idx % len(frames)]} {label} [{sec}s]")
            sys.stderr.flush()
            idx += 1
        sys.stderr.write("\r" + " " * (len(label) + 20) + "\r")
        sys.stderr.flush()

    th = threading.Thread(target=_animate, daemon=True)
    th.start()
    info = types.SimpleNamespace(elapsed=0.0)
    try:
        yield info
    finally:
        stop.set()
        th.join()
        info.elapsed = time.monotonic() - t0


# ---------------------------------------------------------------------------
# Verdict contract — the pure core.
#
# The child session no longer edits `index.json`; it is invoked with `--json-schema` and
# reports a verdict through the final `result` event's `structured_output`. Everything
# below is pure: no subprocess, no threads, no clock, no file I/O — the Runner owns all of
# that. The stream-json event schema is officially undocumented, so every function here
# parses defensively: ambiguity resolves to `fail`, never to a raised exception.
# ---------------------------------------------------------------------------

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "summary": {"type": "string"},
        "error": {"type": "string"},
        "blocked": {"type": "boolean"},
        "blocked_reason": {"type": "string"},
    },
    "required": ["passed"],
    # Diagnosability: a failure must say why, a block must say what it needs. Each `if`
    # repeats the key in its own `required` — without that, `properties` is vacuously
    # satisfied by an object that omits the key, and the `then` clause would fire on
    # verdicts that never mentioned `passed`/`blocked` at all.
    "allOf": [
        {
            "if": {"properties": {"passed": {"const": False}}, "required": ["passed"]},
            "then": {"required": ["error"]},
        },
        {
            "if": {"properties": {"blocked": {"const": True}}, "required": ["blocked"]},
            "then": {"required": ["blocked_reason"]},
        },
    ],
}

# The closed vocabulary `classify_outcome` returns; the Runner routes on exactly these.
OUTCOME_COMPLETED = "completed"
OUTCOME_BLOCKED = "blocked"
OUTCOME_FAIL = "fail"


def is_result_event(obj: dict) -> bool:
    """True if `obj` looks like the final `result` event of a stream-json stream.

    Lenient by design: the event schema is undocumented, so we key only on a `type`/`role`
    field literally equal to "result" and assume nothing else about the shape.
    """
    if not isinstance(obj, dict):
        return False
    return any(obj.get(key) == "result" for key in ("type", "role"))


def parse_verdict(result_event: dict) -> Optional[dict]:
    """Extract the child's verdict from an already-parsed `result` event.

    Returns the `structured_output` dict, or None when it is absent or not a dict — which
    covers a `success` subtype carrying no `structured_output` (D7). Never raises: an
    unexpected stream shape must degrade to `fail`, not to a traceback (that crash class is
    exactly what this task removes).
    """
    if not isinstance(result_event, dict):
        return None
    verdict = result_event.get("structured_output")
    return verdict if isinstance(verdict, dict) else None


def classify_outcome(verdict: Optional[dict], kill_reason: Optional[str]) -> str:
    """Map (verdict, kill_reason) onto exactly one outcome: completed / blocked / fail.

    D7 routes every failure mode — a timeout kill (`kill_reason`, e.g. "timeout-idle"), a
    missing or malformed verdict, and an explicit `passed=false` — into the single `fail`
    channel the retry loop already handles. `blocked` outranks `passed` so a child needing
    human intervention stops the run even if it also claimed to pass.
    """
    if kill_reason:
        return OUTCOME_FAIL
    if not isinstance(verdict, dict):
        return OUTCOME_FAIL
    if verdict.get("blocked") is True:
        return OUTCOME_BLOCKED
    if verdict.get("passed") is True:
        return OUTCOME_COMPLETED
    # `passed` false, missing, or a non-bool truthy value (e.g. "yes") — all ambiguous, all fail.
    return OUTCOME_FAIL


# ---------------------------------------------------------------------------
# Preflight — the mandatory capability gate (D9).
#
# The contract above only holds if the local `claude` actually implements it. An older CLI
# ignores an unknown `--json-schema` without complaint and may truncate the stream tail, so
# the failure mode is the worst kind: a run that looks healthy while every step fails for a
# reason no log explains. Hence Fail-Fast — probe once, before ANY git or state mutation,
# and refuse to start otherwise. Deliberately not a `--flag`: an opt-out would just make the
# silent-misbehaviour mode reachable again.
# ---------------------------------------------------------------------------

# The floor is empirically verified, not guessed: `stream-json` + `--json-schema` was smoke-
# tested end-to-end on this version (plan §7). Compared as a tuple so 2.1.9 < 2.1.216 — the
# string comparison a version check invites gets that backwards.
MIN_CLAUDE_VERSION = (2, 1, 216)

# Substrings `claude --help` must advertise for the invocation in `_invoke_claude` to work.
REQUIRED_CLI_CAPABILITIES = ("--json-schema", "--output-format", "stream-json")

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _parse_cli_version(text: str) -> Optional[tuple]:
    """Extract (major, minor, patch) from `claude --version` output, or None if absent.

    Today the output is `2.1.220 (Claude Code)`, but its exact shape is not a documented
    contract — so we search for the first `x.y.z` instead of parsing positionally, and treat
    "no version found" as a preflight failure rather than optimistically assuming it's new
    enough.
    """
    match = _VERSION_RE.search(text or "")
    return tuple(int(g) for g in match.groups()) if match else None


def _preflight_abort(problem: str) -> NoReturn:
    """Report what is wrong, what is required, and how to fix it — then stop the run."""
    floor = ".".join(str(n) for n in MIN_CLAUDE_VERSION)
    print(f"\n  ✗ Preflight failed: {problem}")
    print(f"  This harness requires the `claude` CLI >= {floor}, supporting "
          f"`--json-schema` and `--output-format stream-json`.")
    print(f"  Fix: run `claude update` (or `npm install -g @anthropic-ai/claude-code@latest`), "
          f"then re-run this command.")
    sys.exit(1)


def _probe_cli(run: Callable[..., subprocess.CompletedProcess], args: list) -> str:
    """Run `claude <args>` and return its stdout; abort on anything but a clean exit.

    A missing binary surfaces as OSError (FileNotFoundError) rather than a return code, so
    both have to be handled — either way the answer is the same abort.
    """
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
    """Abort the run unless the local `claude` can honour the verdict contract (D9).

    Returns None and prints nothing when the CLI is usable — a gate that passes should be
    invisible. The subprocess runner is injected so this is testable without a real CLI:
    plan §6's default gate is L1 (no network, no auth, no cost).
    """
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


# ---------------------------------------------------------------------------
# Liveness runner — spawn the child, bound it, reap it.
#
# The old `subprocess.run(..., timeout=1800)` was wrong twice over: a fixed 30-min
# wall-clock false-kills a legitimately long step, and `timeout=` only *raises* — it never
# signals the child, let alone the bash/test grandchildren it spawned. This section replaces
# it with the layered bound of plan D2: an idle watchdog (fast detector) over a wall-clock
# backstop, plus `--max-turns` as a semantic cap, all reported as one `AttemptResult`.
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    """Read an integer tuning knob from the environment, falling back to `default`.

    The `SG_` prefix is deliberate: `BASH_MAX_TIMEOUT_MS` below is a value we *set on the
    child*, so reading our own configuration from that same name would make one variable
    mean two things.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"  WARN: {name}={raw!r} is not an integer; using the default {default}")
        return default


# Tuning knobs — module-level so a test can monkeypatch sub-second values.
BASH_MAX_TIMEOUT_MS = _env_int("SG_BASH_MAX_TIMEOUT_MS", 8 * 60 * 1000)  # child's max legit silence (D4)
T_IDLE_SEC = _env_int("SG_T_IDLE_SEC", 12 * 60)      # idle watchdog — sized above the Bash cap + margin
T_MAX_SEC = _env_int("SG_T_MAX_SEC", 90 * 60)        # wall-clock backstop
MAX_TURNS = _env_int("SG_MAX_TURNS", 50)             # --max-turns semantic cap

POLL_INTERVAL_SEC = 0.05   # how often the watchdog re-evaluates `timeout_decision`
KILL_GRACE_SEC = 5.0       # SIGTERM → grace → SIGKILL
READER_JOIN_SEC = 5.0      # bounded join, so a grandchild holding the pipe cannot hang us
TAIL_LINES = 50            # lines kept per pipe for diagnostics
TAIL_CHARS = 4000          # hard cap on each tail written to step{N}-output.json

DECISION_KEEP = "keep"
DECISION_KILL_IDLE = "kill-idle"
DECISION_KILL_WALL = "kill-wall"


def timeout_decision(now: float, last_activity: float, start: float,
                     t_idle: float, t_max: float) -> str:
    """Decide whether the child may keep running. Pure: every input is injected.

    Isolating the decision from the timing mechanics is what makes the layered bound
    testable in microseconds instead of `sleep(720)` (plan §6 L1). The wall-clock is checked
    first so that a run which blew its total budget is reported as such even if it also went
    quiet at the very end.
    """
    if now - start > t_max:
        return DECISION_KILL_WALL
    if now - last_activity > t_idle:
        return DECISION_KILL_IDLE
    return DECISION_KEEP


@dataclass
class AttemptResult:
    """Everything one child run produced — the sole basis for routing the step (D6/D10).

    `kill_reason` doubles as the diagnosis: it is `None` on a natural exit and otherwise the
    `timeout_decision` value that ended the run, so a timeout is diagnosable and not merely
    retryable.
    """
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


def attempt_error_message(result: AttemptResult) -> str:
    """The human-readable reason an attempt failed — recorded, printed, and fed to the retry.

    A kill outranks the verdict because `classify_outcome` already short-circuits on it: a
    child killed mid-thought may well have emitted a stale-but-passing verdict earlier.
    """
    if result.kill_reason:
        return (f"{result.kill_reason} (elapsed {result.elapsed:.0f}s, "
                f"silent for {result.last_activity_age:.0f}s)")
    if isinstance(result.verdict, dict) and result.verdict.get("error"):
        return str(result.verdict["error"])
    return f"the child returned no usable verdict (exit code {result.return_code})"


def _safe_json_loads(line: str) -> Optional[dict]:
    """Parse one NDJSON line, or None. Never raises — a malformed line is not an error here.

    The stream carries whatever the CLI decides to print; letting one odd line raise inside a
    reader thread would kill the liveness signal and reintroduce the crash class this task
    removes.
    """
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


class StreamState:
    """Liveness stamp + parse state shared between the reader threads and the watchdog.

    `last_activity` has exactly one writer (the stdout reader) and one reader (the watchdog),
    so a plain float assignment is enough — no lock. Liveness keys on *a line arriving*, never
    on its type (D3): the stream-json event schema is undocumented, so any other rule would
    silently start false-killing the day the CLI adds an event. stderr does not stamp
    liveness — it is drained for hygiene, but a child babbling warnings while making no
    progress is exactly what the idle watchdog is for.
    """

    def __init__(self, *, now: Callable[[], float] = time.monotonic):
        self._now = now
        self.last_activity = now()
        self.result_event: Optional[dict] = None
        self.stdout_tail = deque(maxlen=TAIL_LINES)
        self.stderr_tail = deque(maxlen=TAIL_LINES)

    def on_stdout_line(self, line: str):
        self.last_activity = self._now()
        self.stdout_tail.append(line)
        obj = _safe_json_loads(line)
        if obj is not None and is_result_event(obj):
            self.result_event = obj

    def on_stderr_line(self, line: str):
        self.stderr_tail.append(line)


def _tail_text(lines) -> str:
    text = "".join(lines)
    return text[-TAIL_CHARS:]


def _drain(stream, on_line: Callable[[str], None]):
    """Read `stream` line by line until EOF, handing each raw line to `on_line`."""
    with contextlib.suppress(OSError, ValueError):
        for line in iter(stream.readline, ""):
            on_line(line)


def _kill_process_group(proc: subprocess.Popen, grace: float) -> Optional[int]:
    """SIGTERM the child's whole process group, then SIGKILL it. Returns the signal that ended it.

    The group (not just the leader) is signalled because the child spawns bash/test
    grandchildren that would otherwise survive and keep the stdout pipe open. Every step
    tolerates the child exiting on its own mid-kill — that is a race we win either way, not
    an error.
    """
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


def run_child(cmd: list, cwd: str, env: dict) -> AttemptResult:
    """Run one child session under the layered bound and report the attempt.

    Threading model: two daemon reader threads drain stdout and stderr *concurrently* (a full
    stderr pipe blocks the child's writes and deadlocks the run — D8), while this thread is
    the watchdog. A kill always yields a deterministic `fail` outcome, so every failure mode
    funnels into the one retry path the caller already has.
    """
    state = StreamState()
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
    for th in readers:
        th.start()

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

    for th in readers:
        th.join(timeout=READER_JOIN_SEC)
    for pipe in (proc.stdout, proc.stderr):
        with contextlib.suppress(OSError, ValueError):
            pipe.close()

    verdict = parse_verdict(state.result_event)
    now = time.monotonic()
    return AttemptResult(
        outcome=classify_outcome(verdict, kill_reason),
        verdict=verdict,
        kill_reason=kill_reason,
        signal=sig,
        elapsed=round(now - start, 3),
        last_activity_age=round(now - state.last_activity, 3),
        return_code=proc.returncode,
        stderr_tail=_tail_text(state.stderr_tail),
        stdout_tail=_tail_text(state.stdout_tail),
        saw_result_with_structured_output=verdict is not None,
    )


class StateStore:
    """Sole owner of the task state files: the per-task `index.json` and the top-level index.

    Every read, write, status transition and timestamp for those two files lives here, and
    the Runner (`StepExecutor`) never writes them directly. Concentrating them in one class
    makes the single-writer property structural rather than a convention.

    The clock is injected (`now`) so transitions are assertable without real time.
    """

    def __init__(self, index_file: Path, top_index_file: Path, task_dir_name: str,
                 *, now: Callable[[], str] = kst_stamp):
        self._index_file = index_file
        self._top_index_file = top_index_file
        self._task_dir_name = task_dir_name
        self._now = now

    # --- JSON I/O ---

    @staticmethod
    def read_json(p: Path) -> dict:
        return json.loads(p.read_text(encoding="utf-8"))

    @staticmethod
    def write_json(p: Path, data: dict):
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # --- timestamps ---

    def stamp(self) -> str:
        return self._now()

    # --- reads ---

    def load(self) -> dict:
        return self.read_json(self._index_file)

    def _field_of(self, step_num: int, key: str, default):
        return next((s.get(key, default) for s in self.load()["steps"] if s["step"] == step_num), default)

    def status_of(self, step_num: int) -> str:
        return self._field_of(step_num, "status", "pending")

    def summary_of(self, step_num: int) -> str:
        return self._field_of(step_num, "summary", "")

    def error_of(self, step_num: int, default: str = "Step did not update status") -> str:
        return self._field_of(step_num, "error_message", default)

    def blocked_reason_of(self, step_num: int) -> str:
        return self._field_of(step_num, "blocked_reason", "")

    def next_pending(self) -> Optional[dict]:
        return next((s for s in self.load()["steps"] if s["status"] == "pending"), None)

    def count_completed(self) -> int:
        return sum(1 for s in self.load()["steps"] if s["status"] == "completed")

    @staticmethod
    def build_step_context(index: dict) -> str:
        lines = [
            f"- Step {s['step']} ({s['name']}): {s['summary']}"
            for s in index["steps"]
            if s["status"] == "completed" and s.get("summary")
        ]
        if not lines:
            return ""
        return "## Previous step outputs\n\n" + "\n".join(lines) + "\n\n"

    # --- status transitions (the only writers of index.json) ---

    def _mutate_step(self, step_num: int, apply: Callable[[dict], None]):
        index = self.load()
        for s in index["steps"]:
            if s["step"] == step_num:
                apply(s)
        self.write_json(self._index_file, index)

    def mark_started(self, step_num: int):
        """Stamp `started_at` if absent. Already-started steps are left untouched (no write)."""
        index = self.load()
        for s in index["steps"]:
            if s["step"] == step_num and "started_at" not in s:
                s["started_at"] = self._now()
                self.write_json(self._index_file, index)
                return

    def mark_completed(self, step_num: int, summary: Optional[str] = None):
        """Stamp `completed_at`. `summary=None` keeps whatever summary is already recorded."""
        def apply(s):
            s["status"] = "completed"
            s["completed_at"] = self._now()
            if summary is not None:
                s["summary"] = summary
        self._mutate_step(step_num, apply)

    def mark_blocked(self, step_num: int, reason: Optional[str] = None):
        """Stamp `blocked_at`. `reason=None` keeps whatever blocked_reason is already recorded."""
        def apply(s):
            s["status"] = "blocked"
            s["blocked_at"] = self._now()
            if reason is not None:
                s["blocked_reason"] = reason
        self._mutate_step(step_num, apply)

    def mark_retry(self, step_num: int):
        """Reset the step for another attempt: back to pending, previous error dropped."""
        def apply(s):
            s["status"] = "pending"
            s.pop("error_message", None)
        self._mutate_step(step_num, apply)

    def mark_error(self, step_num: int, message: str):
        def apply(s):
            s["status"] = "error"
            s["error_message"] = message
            s["failed_at"] = self._now()
        self._mutate_step(step_num, apply)

    # --- task-level transitions ---

    def ensure_created_at(self):
        index = self.load()
        if "created_at" not in index:
            index["created_at"] = self._now()
            self.write_json(self._index_file, index)

    def mark_task_completed(self):
        index = self.load()
        index["completed_at"] = self._now()
        self.write_json(self._index_file, index)

    def update_top_index(self, status: str):
        if not self._top_index_file.exists():
            return
        top = self.read_json(self._top_index_file)
        ts = self._now()
        matched = False
        for task in top.get("tasks", []):
            if task.get("dir") == self._task_dir_name:
                task["status"] = status
                ts_key = {"completed": "completed_at", "error": "failed_at", "blocked": "blocked_at"}.get(status)
                if ts_key:
                    task[ts_key] = ts
                matched = True
                break
        if not matched:
            # Fail-Fast: if this task entry is missing from the top index, the status silently desyncs.
            # Warn explicitly instead of staying silent. (dir must match the folder name exactly.)
            print(f"  WARN: top index (docs/sg/tasks/index.json) has no entry with dir='{self._task_dir_name}', "
                  f"so status ('{status}') could not be recorded. Check that dir matches the folder name.")
            return
        self.write_json(self._top_index_file, top)

    # --- checks ---

    def check_blockers(self):
        index = self.load()
        for s in reversed(index["steps"]):
            if s["status"] == "error":
                print(f"\n  ✗ Step {s['step']} ({s['name']}) failed.")
                print(f"  Error: {s.get('error_message', 'unknown')}")
                print(f"  Fix and reset status to 'pending' to retry.")
                sys.exit(1)
            if s["status"] == "blocked":
                print(f"\n  ⏸ Step {s['step']} ({s['name']}) blocked.")
                print(f"  Reason: {s.get('blocked_reason', 'unknown')}")
                print(f"  Resolve and reset status to 'pending' to retry.")
                sys.exit(2)
            if s["status"] != "pending":
                break


class StepExecutor:
    """Harness that runs the steps inside a task directory sequentially.

    Owns execution, retry and git; all task state is delegated to its `StateStore`.
    """

    MAX_RETRIES = 3
    FEAT_MSG = "feat({task}): step {num} — {name}"
    CHORE_MSG = "chore({task}): step {num} output"

    def __init__(self, task_dir_name: str, *, auto_push: bool = False):
        self._root = str(ROOT)
        self._tasks_dir = ROOT / "docs" / "sg" / "tasks"
        self._task_dir = self._tasks_dir / task_dir_name
        self._task_dir_name = task_dir_name
        self._top_index_file = self._tasks_dir / "index.json"
        self._auto_push = auto_push

        if not self._task_dir.is_dir():
            print(f"ERROR: {self._task_dir} not found")
            sys.exit(1)

        self._index_file = self._task_dir / "index.json"
        if not self._index_file.exists():
            print(f"ERROR: {self._index_file} not found")
            sys.exit(1)

        self._state = StateStore(self._index_file, self._top_index_file, task_dir_name)

        idx = self._state.load()
        self._project = idx.get("project", "project")
        self._task_name = idx.get("task", task_dir_name)
        self._total = len(idx["steps"])

    def run(self, once: bool = False):
        # First, before the header and before anything touches git or index.json: a CLI that
        # cannot honour the verdict contract must abort the run, not half-mutate it (D9).
        preflight_check()
        self._print_header()
        self._state.check_blockers()
        self._checkout_branch()
        guardrails = self._load_guardrails()
        self._state.ensure_created_at()
        all_done = self._execute_all_steps(guardrails, once=once)
        if all_done:
            self._finalize()

    # --- state delegation (StateStore is the owner; these are thin passthroughs) ---

    def _stamp(self) -> str:
        return self._state.stamp()

    @staticmethod
    def _read_json(p: Path) -> dict:
        return StateStore.read_json(p)

    @staticmethod
    def _write_json(p: Path, data: dict):
        StateStore.write_json(p, data)

    @staticmethod
    def _build_step_context(index: dict) -> str:
        return StateStore.build_step_context(index)

    def _check_blockers(self):
        self._state.check_blockers()

    def _update_top_index(self, status: str):
        self._state.update_top_index(status)

    # --- git ---

    def _run_git(self, *args) -> subprocess.CompletedProcess:
        cmd = ["git"] + list(args)
        return subprocess.run(cmd, cwd=self._root, capture_output=True, text=True)

    def _checkout_branch(self):
        branch = f"feat-{self._task_name}"

        r = self._run_git("rev-parse", "--abbrev-ref", "HEAD")
        if r.returncode != 0:
            print("  ERROR: git is unavailable or this is not a git repo.")
            print(f"  {r.stderr.strip()}")
            sys.exit(1)

        if r.stdout.strip() == branch:
            return

        r = self._run_git("rev-parse", "--verify", branch)
        r = self._run_git("checkout", branch) if r.returncode == 0 else self._run_git("checkout", "-b", branch)

        if r.returncode != 0:
            print(f"  ERROR: failed to checkout branch '{branch}'.")
            print(f"  {r.stderr.strip()}")
            print(f"  Hint: stash or commit your changes, then try again.")
            sys.exit(1)

        print(f"  Branch: {branch}")

    def _commit_step(self, step_num: int, step_name: str):
        output_rel = f"docs/sg/tasks/{self._task_dir_name}/step{step_num}-output.json"
        index_rel = f"docs/sg/tasks/{self._task_dir_name}/index.json"

        self._run_git("add", "-A")
        self._run_git("reset", "HEAD", "--", output_rel)
        self._run_git("reset", "HEAD", "--", index_rel)

        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = self.FEAT_MSG.format(task=self._task_name, num=step_num, name=step_name)
            r = self._run_git("commit", "-m", msg)
            if r.returncode == 0:
                print(f"  Commit: {msg}")
            else:
                print(f"  WARN: code commit failed: {r.stderr.strip()}")

        self._run_git("add", "-A")
        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = self.CHORE_MSG.format(task=self._task_name, num=step_num)
            r = self._run_git("commit", "-m", msg)
            if r.returncode != 0:
                print(f"  WARN: housekeeping commit failed: {r.stderr.strip()}")

    # --- guardrails & context ---

    def _load_guardrails(self) -> str:
        sections = []
        claude_md = ROOT / "CLAUDE.md"
        if claude_md.exists():
            sections.append(f"## Project rules (CLAUDE.md)\n\n{claude_md.read_text()}")
        docs_dir = ROOT / "docs"
        if docs_dir.is_dir():
            for doc in sorted(docs_dir.glob("*.md")):
                sections.append(f"## {doc.stem}\n\n{doc.read_text()}")
        return "\n\n---\n\n".join(sections) if sections else ""

    def _build_preamble(self, guardrails: str, step_context: str,
                        prev_error: Optional[str] = None) -> str:
        retry_section = ""
        if prev_error:
            retry_section = (
                f"\n## ⚠ Previous attempt failed — you MUST use the error below to fix it\n\n"
                f"{prev_error}\n\n---\n\n"
            )
        return (
            f"You are a developer on the {self._project} project. Carry out the step below.\n\n"
            f"{guardrails}\n\n---\n\n"
            f"{step_context}{retry_section}"
            f"## Work rules\n\n"
            f"1. Review the code written in earlier steps and keep it consistent.\n"
            f"2. Do only the work specified in this step. Do not add extra features or files.\n"
            f"3. Do not break existing tests.\n"
            f"4. Run the AC (Acceptance Criteria) verification yourself.\n"
            f"5. Report the result ONLY through the verdict you return — do NOT edit "
            f"/docs/sg/tasks/{self._task_dir_name}/index.json or any other harness state file. "
            f"The harness is the sole writer of index.json and records this step's status from your verdict; "
            f"anything you write there is discarded.\n"
            f"   - AC passes → passed=true + a one-line summary of this step's output in \"summary\"\n"
            f"   - still failing after {self.MAX_RETRIES} fix attempts → passed=false + a concrete \"error\"\n"
            f"   - if user intervention is needed (API key, auth, manual setup, etc.) → blocked=true + \"blocked_reason\", then stop immediately\n"
            f"6. Do NOT commit or run any git command. The harness commits this step for you "
            f"once the AC passes — your job is only to make the changes, run the AC, and return the verdict.\n\n---\n\n"
        )

    # --- Claude invocation ---

    def _invoke_claude(self, step: dict, preamble: str, attempt: int = 1) -> AttemptResult:
        """Run one child session for this step and record the full attempt for diagnosis."""
        step_num, step_name = step["step"], step["name"]
        step_file = self._task_dir / f"step{step_num}.md"

        if not step_file.exists():
            print(f"  ERROR: {step_file} not found")
            sys.exit(1)

        prompt = preamble + step_file.read_text()
        cmd = [
            "claude", "-p", "--dangerously-skip-permissions",
            "--output-format", "stream-json", "--verbose",
            "--json-schema", json.dumps(VERDICT_SCHEMA),
            "--max-turns", str(MAX_TURNS),
            prompt,
        ]
        # Bounding the child's own Bash timeout turns "max legitimate silence" from a measured
        # unknown into a controlled quantity, which is what lets T_IDLE_SEC be sized safely (D4).
        env = {
            **os.environ,
            "BASH_MAX_TIMEOUT_MS": str(BASH_MAX_TIMEOUT_MS),
            "BASH_DEFAULT_TIMEOUT_MS": str(BASH_MAX_TIMEOUT_MS),
        }

        result = run_child(cmd, cwd=self._root, env=env)

        output = {"step": step_num, "name": step_name, "attempt": attempt, **asdict(result)}
        out_path = self._task_dir / f"step{step_num}-output.json"
        with open(out_path, "w") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        return result

    # --- header & checks ---

    def _print_header(self):
        print(f"\n{'='*60}")
        print(f"  Harness Step Executor")
        print(f"  Task: {self._task_name} | Steps: {self._total}")
        if self._auto_push:
            print(f"  Auto-push: enabled")
        print(f"{'='*60}")

    # --- execution loop ---

    def _execute_single_step(self, step: dict, guardrails: str) -> bool:
        """Run a single step (including retries). True on completion, False on failure/blocked.

        Status is driven **solely** by the `AttemptResult` the child returned, and written
        **solely** through the StateStore. The child's own copy of `index.json` is never read
        for control flow, so a stray edit under `--dangerously-skip-permissions` is discarded
        rather than trusted (D6) — which is what makes T1-B impossible by construction rather
        than merely discouraged.
        """
        step_num, step_name = step["step"], step["name"]
        done = self._state.count_completed()
        prev_error = None

        for attempt in range(1, self.MAX_RETRIES + 1):
            step_context = self._state.build_step_context(self._state.load())
            preamble = self._build_preamble(guardrails, step_context, prev_error)

            tag = f"Step {step_num}/{self._total - 1} ({done} done): {step_name}"
            if attempt > 1:
                tag += f" [retry {attempt}/{self.MAX_RETRIES}]"

            with progress_indicator(tag) as pi:
                result = self._invoke_claude(step, preamble, attempt)
                elapsed = int(pi.elapsed)

            verdict = result.verdict if isinstance(result.verdict, dict) else {}

            if result.outcome == OUTCOME_COMPLETED:
                self._state.mark_completed(step_num, verdict.get("summary"))
                self._commit_step(step_num, step_name)
                summary = self._state.summary_of(step_num)
                nxt = self._state.next_pending()
                print(f"  ✓ Step {step_num}/{self._total - 1}: {step_name} [{elapsed}s] — {summary}")
                if nxt:
                    print(f"    Next ▶ Step {nxt['step']} {nxt['name']}")
                return True

            if result.outcome == OUTCOME_BLOCKED:
                self._state.mark_blocked(step_num, verdict.get("blocked_reason"))
                reason = self._state.blocked_reason_of(step_num)
                print(f"  ⏸ Step {step_num}: {step_name} blocked [{elapsed}s]")
                print(f"    Reason: {reason}")
                self._state.update_top_index("blocked")
                sys.exit(2)

            err_msg = attempt_error_message(result)

            if attempt < self.MAX_RETRIES:
                self._state.mark_retry(step_num)
                prev_error = err_msg
                print(f"  ↻ Step {step_num}: retry {attempt}/{self.MAX_RETRIES} — {err_msg}")
            else:
                self._state.mark_error(step_num, f"[failed after {self.MAX_RETRIES} attempts] {err_msg}")
                self._commit_step(step_num, step_name)
                print(f"  ✗ Step {step_num}: {step_name} failed after {self.MAX_RETRIES} attempts [{elapsed}s]")
                print(f"    Error: {err_msg}")
                self._state.update_top_index("error")
                sys.exit(1)

        return False  # unreachable

    def _execute_all_steps(self, guardrails: str, once: bool = False) -> bool:
        """Run pending steps. Returns True when no pending steps remain (task done).
        In once mode, runs a single step and returns whether that was the last one."""
        while True:
            pending = self._state.next_pending()
            if pending is None:
                print("\n  All steps completed!")
                return True

            self._state.mark_started(pending["step"])
            self._execute_single_step(pending, guardrails)

            if once:
                return self._state.next_pending() is None

    def _finalize(self):
        self._state.mark_task_completed()
        self._state.update_top_index("completed")

        self._run_git("add", "-A")
        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = f"chore({self._task_name}): mark task completed"
            r = self._run_git("commit", "-m", msg)
            if r.returncode == 0:
                print(f"  ✓ {msg}")

        if self._auto_push:
            branch = f"feat-{self._task_name}"
            r = self._run_git("push", "-u", "origin", branch)
            if r.returncode != 0:
                print(f"\n  ERROR: git push failed: {r.stderr.strip()}")
                sys.exit(1)
            print(f"  ✓ Pushed to origin/{branch}")

        print(f"\n{'='*60}")
        print(f"  Task '{self._task_name}' completed!")
        print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(description="Harness Step Executor")
    parser.add_argument("task_dir", help="Task directory name (e.g. 20260616_task-name)")
    parser.add_argument("--push", action="store_true", help="Push branch after completion")
    parser.add_argument("--once", action="store_true", help="Run only the next pending step, then exit")
    args = parser.parse_args()

    StepExecutor(args.task_dir, auto_push=args.push).run(once=args.once)


if __name__ == "__main__":
    main()
