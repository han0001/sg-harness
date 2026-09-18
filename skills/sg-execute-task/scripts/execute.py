#!/usr/bin/env python3
"""
Harness Step Executor — runs the steps within a task sequentially and self-corrects.

Usage (run from the user's project root):
    python3 "<installed-skill-dir>/scripts/execute.py" <task-dir> [--runtime claude|codex] [--push]

ROOT (the target of the work) is the git root of cwd, independent of the script's own location.
"""

import argparse
import contextlib
import json
import subprocess
import sys
import threading
import time
import types
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Optional

from runtimes import AgentRuntime, get_runtime
from runtimes.base import (
    AttemptResult,
    OUTCOME_BLOCKED,
    OUTCOME_COMPLETED,
    OUTCOME_FAIL,
    VERDICT_SCHEMA,
    attempt_error_message,
    classify_outcome,
)
from runtimes.claude import (
    BASH_MAX_TIMEOUT_MS,
    DECISION_KEEP,
    DECISION_KILL_IDLE,
    DECISION_KILL_WALL,
    KILL_GRACE_SEC,
    MAX_TURNS,
    MIN_CLAUDE_VERSION,
    READER_JOIN_SEC,
    REQUIRED_CLI_CAPABILITIES,
    StreamState,
    TAIL_CHARS,
    TAIL_LINES,
    T_IDLE_SEC,
    T_MAX_SEC,
    _parse_cli_version,
    _preflight_abort,
    _probe_cli,
    _safe_json_loads,
    is_result_event,
    parse_verdict,
    preflight_check,
    run_child,
    timeout_decision,
)

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


# Runtime contracts and provider-specific helpers are imported above for compatibility.
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

    @contextlib.contextmanager
    def preserve_indexes(self):
        """Restore harness-owned indexes after an untrusted child process returns."""
        snapshots = {
            path: path.read_bytes() if path.exists() else None
            for path in (self._index_file, self._top_index_file)
        }
        try:
            yield
        finally:
            for path, content in snapshots.items():
                if content is None:
                    with contextlib.suppress(FileNotFoundError):
                        path.unlink()
                    continue
                if path.is_symlink():
                    path.unlink()
                path.write_bytes(content)

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

    def __init__(self, task_dir_name: str, *, auto_push: bool = False,
                 runtime: Optional[AgentRuntime] = None):
        self._root = str(ROOT)
        self._tasks_dir = ROOT / "docs" / "sg" / "tasks"
        self._task_dir = self._tasks_dir / task_dir_name
        self._task_dir_name = task_dir_name
        self._top_index_file = self._tasks_dir / "index.json"
        self._auto_push = auto_push
        self._runtime = runtime or get_runtime()

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
        self._runtime.preflight()
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
        instruction_files = getattr(self, "_runtime", None)
        instruction_files = getattr(instruction_files, "instruction_files", ("CLAUDE.md",))
        for filename in instruction_files:
            instruction_file = ROOT / filename
            if instruction_file.exists():
                sections.append(
                    f"## Project rules ({filename})\n\n{instruction_file.read_text()}"
                )
                break
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

    # --- runtime invocation ---

    def _invoke_runtime(self, step: dict, preamble: str, attempt: int = 1) -> AttemptResult:
        """Run one child session through the selected runtime and record the attempt."""
        step_num, step_name = step["step"], step["name"]
        step_file = self._task_dir / f"step{step_num}.md"

        if not step_file.exists():
            print(f"  ERROR: {step_file} not found")
            sys.exit(1)

        prompt = preamble + step_file.read_text()
        with self._state.preserve_indexes():
            result = self._runtime.run(prompt, cwd=self._root)

        output = {"step": step_num, "name": step_name, "attempt": attempt, **asdict(result)}
        out_path = self._task_dir / f"step{step_num}-output.json"
        with open(out_path, "w") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        return result

    def _invoke_claude(self, step: dict, preamble: str, attempt: int = 1) -> AttemptResult:
        """Compatibility alias for callers that used the former Claude-specific helper."""
        return self._invoke_runtime(step, preamble, attempt)

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
        for control flow, so a stray child edit to that file is discarded
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
                result = self._invoke_runtime(step, preamble, attempt)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Harness Step Executor")
    parser.add_argument("task_dir", help="Task directory name (e.g. 20260616_task-name)")
    parser.add_argument("--push", action="store_true", help="Push branch after completion")
    parser.add_argument("--once", action="store_true", help="Run only the next pending step, then exit")
    parser.add_argument(
        "--runtime",
        choices=("claude", "codex"),
        default="claude",
        help="Child agent runtime (default: claude)",
    )
    return parser


def main():
    args = build_parser().parse_args()

    StepExecutor(
        args.task_dir,
        auto_push=args.push,
        runtime=get_runtime(args.runtime),
    ).run(once=args.once)


if __name__ == "__main__":
    main()
