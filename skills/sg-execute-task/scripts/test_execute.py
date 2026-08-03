"""
Safety-net tests for execute.py refactoring.
Verify that behavior is identical before and after refactoring.
"""

import io
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import execute as ex


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_project(tmp_path):
    """A temporary project structure with docs/sg/tasks/, CLAUDE.md, and docs/."""
    tasks_dir = tmp_path / "docs" / "sg" / "tasks"
    tasks_dir.mkdir(parents=True)

    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# Rules\n- rule one\n- rule two")

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(exist_ok=True)
    (docs_dir / "arch.md").write_text("# Architecture\nSome content")
    (docs_dir / "guide.md").write_text("# Guide\nAnother doc")

    return tmp_path


@pytest.fixture
def task_dir(tmp_project):
    """A task directory with 3 steps."""
    d = tmp_project / "docs" / "sg" / "tasks" / "0-mvp"
    d.mkdir()

    index = {
        "project": "TestProject",
        "task": "mvp",
        "steps": [
            {"step": 0, "name": "setup", "status": "completed", "summary": "project initialized"},
            {"step": 1, "name": "core", "status": "completed", "summary": "core logic implemented"},
            {"step": 2, "name": "ui", "status": "pending"},
        ],
    }
    (d / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False))
    (d / "step2.md").write_text("# Step 2: UI\n\nImplement the UI.")

    return d


@pytest.fixture
def top_index(tmp_project):
    """docs/sg/tasks/index.json (top-level)."""
    top = {
        "tasks": [
            {"dir": "0-mvp", "status": "pending"},
            {"dir": "1-polish", "status": "pending"},
        ]
    }
    p = tmp_project / "docs" / "sg" / "tasks" / "index.json"
    p.write_text(json.dumps(top, indent=2))
    return p


@pytest.fixture
def executor(tmp_project, task_dir):
    """A StepExecutor instance for tests. git calls must be mocked separately."""
    with patch.object(ex, "ROOT", tmp_project):
        inst = ex.StepExecutor("0-mvp")
    # Reset internal paths relative to tmp_project
    inst._root = str(tmp_project)
    inst._tasks_dir = tmp_project / "docs" / "sg" / "tasks"
    inst._task_dir = task_dir
    inst._task_dir_name = "0-mvp"
    inst._index_file = task_dir / "index.json"
    inst._top_index_file = tmp_project / "docs" / "sg" / "tasks" / "index.json"
    return inst


# ---------------------------------------------------------------------------
# _stamp (formerly now_iso)
# ---------------------------------------------------------------------------

class TestStamp:
    def test_returns_kst_timestamp(self, executor):
        result = executor._stamp()
        assert "+0900" in result

    def test_format_is_iso(self, executor):
        result = executor._stamp()
        dt = datetime.strptime(result, "%Y-%m-%dT%H:%M:%S%z")
        assert dt.tzinfo is not None

    def test_is_current_time(self, executor):
        before = datetime.now(ex.TZ).replace(microsecond=0)
        result = executor._stamp()
        after = datetime.now(ex.TZ).replace(microsecond=0) + timedelta(seconds=1)
        parsed = datetime.strptime(result, "%Y-%m-%dT%H:%M:%S%z")
        assert before <= parsed <= after


# ---------------------------------------------------------------------------
# _read_json / _write_json
# ---------------------------------------------------------------------------

class TestJsonHelpers:
    def test_roundtrip(self, tmp_path):
        data = {"key": "café", "nested": [1, 2, 3]}
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, data)
        loaded = ex.StepExecutor._read_json(p)
        assert loaded == data

    def test_save_ensures_ascii_false(self, tmp_path):
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, {"key": "café"})
        raw = p.read_text()
        assert "café" in raw
        assert "\\u" not in raw

    def test_save_indented(self, tmp_path):
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, {"a": 1})
        raw = p.read_text()
        assert "\n" in raw

    def test_load_nonexistent_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ex.StepExecutor._read_json(tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# _load_guardrails
# ---------------------------------------------------------------------------

class TestLoadGuardrails:
    def test_loads_claude_md_and_docs(self, executor, tmp_project):
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "# Rules" in result
        assert "rule one" in result
        assert "# Architecture" in result
        assert "# Guide" in result

    def test_sections_separated_by_divider(self, executor, tmp_project):
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "---" in result

    def test_docs_sorted_alphabetically(self, executor, tmp_project):
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        arch_pos = result.index("arch")
        guide_pos = result.index("guide")
        assert arch_pos < guide_pos

    def test_no_claude_md(self, executor, tmp_project):
        (tmp_project / "CLAUDE.md").unlink()
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "CLAUDE.md" not in result
        assert "Architecture" in result

    def test_no_docs_dir(self, executor, tmp_project):
        import shutil
        shutil.rmtree(tmp_project / "docs")
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "Rules" in result
        assert "Architecture" not in result

    def test_empty_project(self, tmp_path):
        with patch.object(ex, "ROOT", tmp_path):
            # Static-like behavior that needs no executor, so use a throwaway instance
            tasks_dir = tmp_path / "docs" / "sg" / "tasks" / "dummy"
            tasks_dir.mkdir(parents=True)
            idx = {"project": "T", "task": "t", "steps": []}
            (tasks_dir / "index.json").write_text(json.dumps(idx))
            inst = ex.StepExecutor.__new__(ex.StepExecutor)
            result = inst._load_guardrails()
        assert result == ""


# ---------------------------------------------------------------------------
# _build_step_context
# ---------------------------------------------------------------------------

class TestBuildStepContext:
    def test_includes_completed_with_summary(self, task_dir):
        index = json.loads((task_dir / "index.json").read_text())
        result = ex.StepExecutor._build_step_context(index)
        assert "Step 0 (setup): project initialized" in result
        assert "Step 1 (core): core logic implemented" in result

    def test_excludes_pending(self, task_dir):
        index = json.loads((task_dir / "index.json").read_text())
        result = ex.StepExecutor._build_step_context(index)
        assert "ui" not in result

    def test_excludes_completed_without_summary(self, task_dir):
        index = json.loads((task_dir / "index.json").read_text())
        del index["steps"][0]["summary"]
        result = ex.StepExecutor._build_step_context(index)
        assert "setup" not in result
        assert "core" in result

    def test_empty_when_no_completed(self):
        index = {"steps": [{"step": 0, "name": "a", "status": "pending"}]}
        result = ex.StepExecutor._build_step_context(index)
        assert result == ""

    def test_has_header(self, task_dir):
        index = json.loads((task_dir / "index.json").read_text())
        result = ex.StepExecutor._build_step_context(index)
        assert result.startswith("## Previous step outputs")


# ---------------------------------------------------------------------------
# _build_preamble
# ---------------------------------------------------------------------------

class TestBuildPreamble:
    def test_includes_project_name(self, executor):
        result = executor._build_preamble("", "")
        assert "TestProject" in result

    def test_includes_guardrails(self, executor):
        result = executor._build_preamble("GUARD_CONTENT", "")
        assert "GUARD_CONTENT" in result

    def test_includes_step_context(self, executor):
        ctx = "## Previous step outputs\n\n- Step 0: done"
        result = executor._build_preamble("", ctx)
        assert "Previous step outputs" in result

    def test_instructs_child_not_to_commit(self, executor):
        # Invariant 2: only the orchestrator touches git. The child must be told
        # not to commit, and must not be handed a commit example to copy.
        result = executor._build_preamble("", "")
        assert "Do NOT commit" in result
        assert "feat(mvp):" not in result

    def test_includes_rules(self, executor):
        result = executor._build_preamble("", "")
        assert "Work rules" in result
        assert "AC" in result

    def test_no_retry_section_by_default(self, executor):
        result = executor._build_preamble("", "")
        assert "Previous attempt failed" not in result

    def test_retry_section_with_prev_error(self, executor):
        result = executor._build_preamble("", "", prev_error="a type error occurred")
        assert "Previous attempt failed" in result
        assert "a type error occurred" in result

    def test_includes_max_retries(self, executor):
        result = executor._build_preamble("", "")
        assert str(ex.StepExecutor.MAX_RETRIES) in result

    def test_includes_index_path(self, executor):
        result = executor._build_preamble("", "")
        assert "/docs/sg/tasks/0-mvp/index.json" in result


# ---------------------------------------------------------------------------
# _update_top_index
# ---------------------------------------------------------------------------

class TestUpdateTopIndex:
    def test_completed(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("completed")
        data = json.loads(top_index.read_text())
        mvp = next(t for t in data["tasks"] if t["dir"] == "0-mvp")
        assert mvp["status"] == "completed"
        assert "completed_at" in mvp

    def test_error(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("error")
        data = json.loads(top_index.read_text())
        mvp = next(t for t in data["tasks"] if t["dir"] == "0-mvp")
        assert mvp["status"] == "error"
        assert "failed_at" in mvp

    def test_blocked(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("blocked")
        data = json.loads(top_index.read_text())
        mvp = next(t for t in data["tasks"] if t["dir"] == "0-mvp")
        assert mvp["status"] == "blocked"
        assert "blocked_at" in mvp

    def test_other_tasks_unchanged(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("completed")
        data = json.loads(top_index.read_text())
        polish = next(t for t in data["tasks"] if t["dir"] == "1-polish")
        assert polish["status"] == "pending"

    def test_nonexistent_dir_warns(self, executor, top_index, capsys):
        # task_dir_name is owned by the StateStore, so target it directly.
        store = ex.StateStore(executor._index_file, top_index, "no-such-dir")
        original = json.loads(top_index.read_text())
        store.update_top_index("completed")
        after = json.loads(top_index.read_text())
        # No invalid status is recorded, so the file is unchanged
        for t_before, t_after in zip(original["tasks"], after["tasks"]):
            assert t_before["status"] == t_after["status"]
        # Fail-Fast: warn about the desync instead of staying silent
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "no-such-dir" in out

    def test_no_top_index_file(self, executor, tmp_path):
        store = ex.StateStore(executor._index_file, tmp_path / "nonexistent.json", "0-mvp")
        store.update_top_index("completed")  # should not raise


# ---------------------------------------------------------------------------
# _checkout_branch (mocked)
# ---------------------------------------------------------------------------

class TestCheckoutBranch:
    def _mock_git(self, executor, responses):
        call_idx = {"i": 0}
        def fake_git(*args):
            idx = call_idx["i"]
            call_idx["i"] += 1
            if idx < len(responses):
                return responses[idx]
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

    def test_already_on_branch(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="feat-mvp\n", stderr=""),
        ])
        executor._checkout_branch()  # should return without checkout

    def test_branch_exists_checkout(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ])
        executor._checkout_branch()

    def test_branch_not_exists_create(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="not found"),
            MagicMock(returncode=0, stdout="", stderr=""),
        ])
        executor._checkout_branch()

    def test_checkout_fails_exits(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="dirty tree"),
        ])
        with pytest.raises(SystemExit) as exc_info:
            executor._checkout_branch()
        assert exc_info.value.code == 1

    def test_no_git_exits(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=1, stdout="", stderr="not a git repo"),
        ])
        with pytest.raises(SystemExit) as exc_info:
            executor._checkout_branch()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _commit_step (mocked)
# ---------------------------------------------------------------------------

class TestCommitStep:
    def test_two_stage_commit(self, executor):
        calls = []
        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("diff", "--cached"):
                return MagicMock(returncode=1)
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

        executor._commit_step(2, "ui")

        commit_calls = [c for c in calls if c[0] == "commit"]
        assert len(commit_calls) == 2
        assert "feat(mvp):" in commit_calls[0][2]
        assert "chore(mvp):" in commit_calls[1][2]

    def test_no_code_changes_skips_feat_commit(self, executor):
        call_count = {"diff": 0}
        calls = []
        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("diff", "--cached"):
                call_count["diff"] += 1
                if call_count["diff"] == 1:
                    return MagicMock(returncode=0)
                return MagicMock(returncode=1)
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

        executor._commit_step(2, "ui")

        commit_msgs = [c[2] for c in calls if c[0] == "commit"]
        assert len(commit_msgs) == 1
        assert "chore" in commit_msgs[0]


# ---------------------------------------------------------------------------
# _invoke_claude — the spawn contract (Popen mocked, no real child)
# ---------------------------------------------------------------------------

VERDICT_OK = {"passed": True, "summary": "ui shipped"}
RESULT_LINE = json.dumps(
    {"type": "result", "subtype": "success", "structured_output": VERDICT_OK}
) + "\n"


class _FakeProc:
    """A Popen stand-in that has already exited, so run_child's watchdog never fires."""

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode
        self.pid = -1

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class TestInvokeClaude:
    def _invoke(self, executor, proc=None, preamble="PREAMBLE\n", attempt=1):
        with patch("subprocess.Popen", return_value=proc or _FakeProc(stdout=RESULT_LINE)) as popen:
            result = executor._invoke_claude({"step": 2, "name": "ui"}, preamble, attempt)
        return result, popen

    def test_spawns_a_streaming_child_carrying_the_verdict_schema(self, executor):
        _, popen = self._invoke(executor)
        cmd = popen.call_args[0][0]
        assert cmd[0] == "claude"
        assert "-p" in cmd and "--dangerously-skip-permissions" in cmd
        # stream-json is what makes the idle watchdog possible; --verbose is required with it.
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"
        assert "--verbose" in cmd
        assert json.loads(cmd[cmd.index("--json-schema") + 1]) == ex.VERDICT_SCHEMA
        assert cmd[cmd.index("--max-turns") + 1] == str(ex.MAX_TURNS)
        assert "PREAMBLE" in cmd[-1]
        assert "Implement the UI" in cmd[-1]

    def test_child_gets_its_own_process_group_and_both_pipes(self, executor):
        _, popen = self._invoke(executor)
        kwargs = popen.call_args[1]
        assert kwargs["start_new_session"] is True   # so bash/test grandchildren can be reaped
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.PIPE   # drained too, or a full pipe deadlocks

    def test_env_carries_the_bash_timeout_cap(self, executor):
        _, popen = self._invoke(executor)
        env = popen.call_args[1]["env"]
        assert env["BASH_MAX_TIMEOUT_MS"] == str(ex.BASH_MAX_TIMEOUT_MS)
        assert env["BASH_DEFAULT_TIMEOUT_MS"] == str(ex.BASH_MAX_TIMEOUT_MS)
        assert "PATH" in env    # the real environment is inherited, not replaced

    def test_returns_the_parsed_verdict(self, executor):
        result, _ = self._invoke(executor)
        assert result.outcome == ex.OUTCOME_COMPLETED
        assert result.verdict == VERDICT_OK
        assert result.saw_result_with_structured_output is True
        assert result.kill_reason is None

    def test_output_json_records_every_diagnostic_field(self, executor):
        # D10: a timeout must be diagnosable, not merely retryable.
        self._invoke(executor, proc=_FakeProc(stdout=RESULT_LINE, stderr="boom\n"), attempt=2)
        data = json.loads((executor._task_dir / "step2-output.json").read_text())
        assert data["step"] == 2 and data["name"] == "ui" and data["attempt"] == 2
        for field in ("outcome", "verdict", "kill_reason", "signal", "elapsed",
                      "last_activity_age", "return_code", "stderr_tail", "stdout_tail",
                      "saw_result_with_structured_output"):
            assert field in data, f"{field} missing from step-output.json"
        assert data["stderr_tail"] == "boom\n"

    def test_nonexistent_step_file_exits(self, executor):
        step = {"step": 99, "name": "nonexistent"}
        with pytest.raises(SystemExit) as exc_info:
            executor._invoke_claude(step, "preamble")
        assert exc_info.value.code == 1

    def test_never_falls_back_to_subprocess_run(self, executor):
        # The old `subprocess.run(..., timeout=1800)` only raised — it never killed the group.
        with patch("subprocess.run") as mock_run:
            self._invoke(executor)
        assert mock_run.call_count == 0


# ---------------------------------------------------------------------------
# progress_indicator (formerly Spinner)
# ---------------------------------------------------------------------------

class TestProgressIndicator:
    def test_context_manager(self):
        import time
        with ex.progress_indicator("test") as pi:
            time.sleep(0.15)
        assert pi.elapsed >= 0.1

    def test_elapsed_increases(self):
        import time
        with ex.progress_indicator("test") as pi:
            time.sleep(0.2)
        assert pi.elapsed > 0


# ---------------------------------------------------------------------------
# main() CLI parsing (mocked)
# ---------------------------------------------------------------------------

class TestMainCli:
    def test_no_args_exits(self):
        with patch("sys.argv", ["execute.py"]):
            with pytest.raises(SystemExit) as exc_info:
                ex.main()
            assert exc_info.value.code == 2  # argparse exits with 2

    def test_invalid_task_dir_exits(self):
        with patch("sys.argv", ["execute.py", "nonexistent"]):
            with patch.object(ex, "ROOT", Path("/tmp/fake_nonexistent")):
                with pytest.raises(SystemExit) as exc_info:
                    ex.main()
                assert exc_info.value.code == 1

    def test_missing_index_exits(self, tmp_project):
        (tmp_project / "docs" / "sg" / "tasks" / "empty").mkdir()
        with patch("sys.argv", ["execute.py", "empty"]):
            with patch.object(ex, "ROOT", tmp_project):
                with pytest.raises(SystemExit) as exc_info:
                    ex.main()
                assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _check_blockers (formerly the main() error/blocked check)
# ---------------------------------------------------------------------------

class TestCheckBlockers:
    def _make_executor_with_steps(self, tmp_project, steps):
        d = tmp_project / "docs" / "sg" / "tasks" / "test-task"
        d.mkdir(exist_ok=True)
        index = {"project": "T", "task": "test", "steps": steps}
        (d / "index.json").write_text(json.dumps(index))

        with patch.object(ex, "ROOT", tmp_project):
            inst = ex.StepExecutor.__new__(ex.StepExecutor)
        inst._root = str(tmp_project)
        inst._tasks_dir = tmp_project / "docs" / "sg" / "tasks"
        inst._task_dir = d
        inst._task_dir_name = "test-task"
        inst._index_file = d / "index.json"
        inst._top_index_file = tmp_project / "docs" / "sg" / "tasks" / "index.json"
        inst._state = ex.StateStore(inst._index_file, inst._top_index_file, "test-task")
        inst._task_name = "test"
        inst._total = len(steps)
        return inst

    def test_error_step_exits_1(self, tmp_project):
        steps = [
            {"step": 0, "name": "ok", "status": "completed"},
            {"step": 1, "name": "bad", "status": "error", "error_message": "fail"},
        ]
        inst = self._make_executor_with_steps(tmp_project, steps)
        with pytest.raises(SystemExit) as exc_info:
            inst._check_blockers()
        assert exc_info.value.code == 1

    def test_blocked_step_exits_2(self, tmp_project):
        steps = [
            {"step": 0, "name": "ok", "status": "completed"},
            {"step": 1, "name": "stuck", "status": "blocked", "blocked_reason": "API key"},
        ]
        inst = self._make_executor_with_steps(tmp_project, steps)
        with pytest.raises(SystemExit) as exc_info:
            inst._check_blockers()
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# --once mode (per-step execution)
# ---------------------------------------------------------------------------

class TestExecuteOnce:
    def test_once_runs_only_one_step(self, executor):
        # The fixture has step 2 pending; flip step 1 back so two steps are pending.
        idx = executor._read_json(executor._index_file)
        idx["steps"][1]["status"] = "pending"
        executor._write_json(executor._index_file, idx)

        # Stub the real runner so no child `claude` is spawned — just mark the step done.
        def fake_run(step, guardrails):
            data = executor._read_json(executor._index_file)
            for s in data["steps"]:
                if s["step"] == step["step"]:
                    s["status"] = "completed"
            executor._write_json(executor._index_file, data)
        executor._execute_single_step = fake_run

        all_done = executor._execute_all_steps("", once=True)

        still = [s for s in executor._read_json(executor._index_file)["steps"]
                 if s["status"] == "pending"]
        assert len(still) == 1      # ran exactly one step; one remains
        assert all_done is False    # not done → run() will not finalize


# ---------------------------------------------------------------------------
# StateStore — sole owner of index.json + the top index
# ---------------------------------------------------------------------------

STAMP = "2026-01-02T03:04:05+0900"


@pytest.fixture
def store(tmp_project, task_dir):
    """A StateStore over the 3-step task fixture, with the clock injected (no real time)."""
    return ex.StateStore(
        task_dir / "index.json",
        tmp_project / "docs" / "sg" / "tasks" / "index.json",
        "0-mvp",
        now=lambda: STAMP,
    )


def _step(store, num):
    return next(s for s in store.load()["steps"] if s["step"] == num)


class TestStateStore:
    # --- transitions: correct status + timestamp key, exact injected stamp ---

    def test_mark_started(self, store):
        store.mark_started(2)
        assert _step(store, 2)["started_at"] == STAMP

    def test_mark_started_keeps_existing(self, store):
        index = store.load()
        index["steps"][2]["started_at"] = "2020-01-01T00:00:00+0900"
        store.write_json(store._index_file, index)
        store.mark_started(2)
        assert _step(store, 2)["started_at"] == "2020-01-01T00:00:00+0900"

    def test_mark_completed(self, store):
        store.mark_completed(2, "ui shipped")
        s = _step(store, 2)
        assert s["status"] == "completed"
        assert s["completed_at"] == STAMP
        assert s["summary"] == "ui shipped"

    def test_mark_completed_keeps_existing_summary(self, store):
        store.mark_completed(1)
        s = _step(store, 1)
        assert s["completed_at"] == STAMP
        assert s["summary"] == "core logic implemented"

    def test_mark_blocked(self, store):
        store.mark_blocked(2, "needs API key")
        s = _step(store, 2)
        assert s["status"] == "blocked"
        assert s["blocked_at"] == STAMP
        assert s["blocked_reason"] == "needs API key"

    def test_mark_retry(self, store):
        store.mark_error(2, "boom")
        store.mark_retry(2)
        s = _step(store, 2)
        assert s["status"] == "pending"
        assert "error_message" not in s

    def test_mark_error(self, store):
        store.mark_error(2, "AC failed")
        s = _step(store, 2)
        assert s["status"] == "error"
        assert s["error_message"] == "AC failed"
        assert s["failed_at"] == STAMP

    def test_ensure_created_at(self, store):
        store.ensure_created_at()
        assert store.load()["created_at"] == STAMP

    def test_ensure_created_at_keeps_existing(self, store):
        index = store.load()
        index["created_at"] = "2020-01-01T00:00:00+0900"
        store.write_json(store._index_file, index)
        store.ensure_created_at()
        assert store.load()["created_at"] == "2020-01-01T00:00:00+0900"

    def test_mark_task_completed(self, store):
        store.mark_task_completed()
        assert store.load()["completed_at"] == STAMP

    def test_update_top_index_uses_injected_clock(self, store, top_index):
        store.update_top_index("completed")
        mvp = next(t for t in json.loads(top_index.read_text())["tasks"] if t["dir"] == "0-mvp")
        assert mvp["status"] == "completed"
        assert mvp["completed_at"] == STAMP

    # --- reads ---

    def test_next_pending(self, store):
        assert store.next_pending()["step"] == 2

    def test_next_pending_none_when_all_done(self, store):
        store.mark_completed(2)
        assert store.next_pending() is None

    def test_count_completed(self, store):
        assert store.count_completed() == 2
        store.mark_completed(2)
        assert store.count_completed() == 3

    def test_status_of_defaults_to_pending(self, store):
        assert store.status_of(2) == "pending"
        assert store.status_of(99) == "pending"   # unknown step

    def test_error_of_default(self, store):
        assert store.error_of(2) == "Step did not update status"

    def test_build_step_context(self, store):
        result = store.build_step_context(store.load())
        assert result.startswith("## Previous step outputs")
        assert "Step 0 (setup): project initialized" in result
        assert "Step 1 (core): core logic implemented" in result
        assert "ui" not in result

    # --- blockers ---

    def test_check_blockers_error_exits_1(self, store):
        store.mark_error(2, "boom")
        with pytest.raises(SystemExit) as exc_info:
            store.check_blockers()
        assert exc_info.value.code == 1

    def test_check_blockers_blocked_exits_2(self, store):
        store.mark_blocked(2, "needs API key")
        with pytest.raises(SystemExit) as exc_info:
            store.check_blockers()
        assert exc_info.value.code == 2

    def test_check_blockers_passes_when_clean(self, store):
        store.check_blockers()  # steps are completed/pending → no exit


# ---------------------------------------------------------------------------
# Verdict contract — the pure core (no subprocess, no threads, no clock, no file I/O)
# ---------------------------------------------------------------------------

_ABSENT = object()


def _result_event(structured_output=_ABSENT, subtype="success"):
    """A stream-json `result` event; omit structured_output to model the absent case."""
    event = {"type": "result", "subtype": subtype}
    if structured_output is not _ABSENT:
        event["structured_output"] = structured_output
    return event


def _conditional_for(key, const_value):
    """The schema's `if/then` rule guarding `key == const_value`, or None."""
    for rule in ex.VERDICT_SCHEMA.get("allOf", []):
        cond = rule.get("if", {})
        if cond.get("properties", {}).get(key, {}).get("const") is const_value:
            return rule
    return None


class TestVerdictContract:
    # --- verdict → outcome (T1-B cases) ---

    def test_passed_true_is_completed(self):
        verdict = ex.parse_verdict(_result_event({"passed": True, "summary": "done"}))
        assert ex.classify_outcome(verdict, None) == ex.OUTCOME_COMPLETED

    def test_passed_false_is_fail(self):
        verdict = ex.parse_verdict(_result_event({"passed": False, "error": "AC failed"}))
        assert ex.classify_outcome(verdict, None) == ex.OUTCOME_FAIL

    def test_blocked_true_is_blocked(self):
        verdict = ex.parse_verdict(
            _result_event({"passed": False, "error": "needs key", "blocked": True,
                           "blocked_reason": "API key missing"})
        )
        assert ex.classify_outcome(verdict, None) == ex.OUTCOME_BLOCKED

    def test_blocked_outranks_passed(self):
        # A child that needs human help stops the run even if it also claimed to pass.
        verdict = {"passed": True, "blocked": True, "blocked_reason": "needs auth"}
        assert ex.classify_outcome(verdict, None) == ex.OUTCOME_BLOCKED

    def test_passed_missing_is_fail(self):
        assert ex.classify_outcome({"summary": "did stuff"}, None) == ex.OUTCOME_FAIL

    @pytest.mark.parametrize("value", [1, "true", "yes", [True], {"v": True}, None])
    def test_passed_non_bool_is_fail(self, value):
        assert ex.classify_outcome({"passed": value}, None) == ex.OUTCOME_FAIL

    # --- missing / malformed structured_output → None → fail ---

    def test_structured_output_absent(self):
        assert ex.parse_verdict(_result_event()) is None
        assert ex.classify_outcome(ex.parse_verdict(_result_event()), None) == ex.OUTCOME_FAIL

    def test_success_subtype_without_structured_output(self):
        # D7: a `success` subtype that carries no verdict is still a failure, not a pass.
        event = _result_event(subtype="success")
        assert ex.parse_verdict(event) is None
        assert ex.classify_outcome(ex.parse_verdict(event), None) == ex.OUTCOME_FAIL

    def test_error_subtype_without_structured_output(self):
        event = _result_event(subtype="error_max_turns")
        assert ex.parse_verdict(event) is None
        assert ex.classify_outcome(ex.parse_verdict(event), None) == ex.OUTCOME_FAIL

    @pytest.mark.parametrize(
        "malformed",
        ['{"passed": true}', ["passed"], None, 42, True, ""],
        ids=["json-string", "list", "null", "int", "bool", "empty-string"],
    )
    def test_structured_output_malformed(self, malformed):
        event = _result_event(malformed)
        assert ex.parse_verdict(event) is None
        assert ex.classify_outcome(ex.parse_verdict(event), None) == ex.OUTCOME_FAIL

    @pytest.mark.parametrize("event", [None, "result", [], 42, {"type": "assistant"}])
    def test_parse_verdict_never_raises(self, event):
        # Defensive parsing: an unexpected stream shape degrades to fail, never a traceback.
        assert ex.parse_verdict(event) is None

    def test_verdict_none_is_fail(self):
        assert ex.classify_outcome(None, None) == ex.OUTCOME_FAIL

    # --- kill_reason short-circuits everything ---

    @pytest.mark.parametrize("kill_reason", ["timeout-idle", "timeout-wall"])
    def test_kill_reason_with_passing_verdict_is_fail(self, kill_reason):
        assert ex.classify_outcome({"passed": True}, kill_reason) == ex.OUTCOME_FAIL

    def test_kill_reason_with_blocked_verdict_is_fail(self):
        verdict = {"passed": False, "blocked": True, "blocked_reason": "needs auth"}
        assert ex.classify_outcome(verdict, "timeout-idle") == ex.OUTCOME_FAIL

    def test_kill_reason_without_verdict_is_fail(self):
        assert ex.classify_outcome(None, "timeout-wall") == ex.OUTCOME_FAIL

    # --- is_result_event ---

    def test_detects_result_event(self):
        assert ex.is_result_event({"type": "result", "subtype": "success"}) is True

    @pytest.mark.parametrize(
        "obj",
        [{"type": "assistant"}, {"type": "system", "subtype": "init"}, {}, None, "result", []],
    )
    def test_rejects_non_result_events(self, obj):
        assert ex.is_result_event(obj) is False

    def test_lenient_about_the_key_name(self):
        # The event schema is undocumented, so a `role`-keyed variant still counts.
        assert ex.is_result_event({"role": "result"}) is True

    # --- VERDICT_SCHEMA shape ---

    def test_schema_fields_are_exactly_the_five(self):
        assert set(ex.VERDICT_SCHEMA["properties"]) == {
            "passed", "summary", "error", "blocked", "blocked_reason"
        }

    def test_schema_requires_passed(self):
        assert ex.VERDICT_SCHEMA["required"] == ["passed"]

    def test_schema_requires_error_when_passed_false(self):
        rule = _conditional_for("passed", False)
        assert rule is not None
        assert "error" in rule["then"]["required"]
        # The `if` must also require the key, or it matches verdicts that omit `passed`.
        assert rule["if"]["required"] == ["passed"]

    def test_schema_requires_blocked_reason_when_blocked_true(self):
        rule = _conditional_for("blocked", True)
        assert rule is not None
        assert "blocked_reason" in rule["then"]["required"]
        assert rule["if"]["required"] == ["blocked"]

    def test_schema_is_json_serializable(self):
        # It is handed to the CLI as `--json-schema`, so it must survive a round trip.
        assert json.loads(json.dumps(ex.VERDICT_SCHEMA)) == ex.VERDICT_SCHEMA


# ---------------------------------------------------------------------------
# timeout_decision — the layered bound as a pure function (no clock, no process)
# ---------------------------------------------------------------------------

class TestTimeoutDecision:
    def test_keep_while_both_bounds_hold(self):
        assert ex.timeout_decision(now=100, last_activity=99, start=90,
                                   t_idle=10, t_max=60) == ex.DECISION_KEEP

    def test_kill_idle_when_silent_too_long(self):
        assert ex.timeout_decision(now=100, last_activity=80, start=70,
                                   t_idle=10, t_max=600) == ex.DECISION_KILL_IDLE

    def test_kill_wall_even_while_still_chatty(self):
        # One second since the last event, but the total budget is spent.
        assert ex.timeout_decision(now=100, last_activity=99, start=0,
                                   t_idle=10, t_max=60) == ex.DECISION_KILL_WALL

    def test_wall_outranks_idle_when_both_trip(self):
        assert ex.timeout_decision(now=100, last_activity=0, start=0,
                                   t_idle=10, t_max=60) == ex.DECISION_KILL_WALL

    def test_bounds_are_exclusive(self):
        # Exactly at a bound is still alive; only strictly past it is a kill.
        assert ex.timeout_decision(now=60, last_activity=55, start=0,
                                   t_idle=10, t_max=60) == ex.DECISION_KEEP
        assert ex.timeout_decision(now=60, last_activity=50, start=10,
                                   t_idle=10, t_max=60) == ex.DECISION_KEEP

    def test_a_long_but_chatty_step_is_never_idle_killed(self):
        # T1-A's false-kill: 40 minutes of work is fine while the stream keeps moving,
        # which the old fixed 30-minute wall-clock could not express.
        assert ex.timeout_decision(now=40 * 60, last_activity=40 * 60 - 1, start=0,
                                   t_idle=ex.T_IDLE_SEC, t_max=ex.T_MAX_SEC) == ex.DECISION_KEEP


# ---------------------------------------------------------------------------
# StreamState — NDJSON handling and the liveness stamp (clock injected)
# ---------------------------------------------------------------------------

class TestStreamHandling:
    def _feed(self, lines):
        """Feed fake stdout lines through a StreamState whose clock ticks 1, 2, 3, …"""
        ticks = iter(range(1, len(lines) + 2))
        state = ex.StreamState(now=lambda: next(ticks))   # tick 1 is the initial stamp
        for line in lines:
            state.on_stdout_line(line)
        return state

    def test_every_line_advances_liveness(self):
        state = self._feed(['{"type":"assistant"}\n', 'not json at all\n', '\n'])
        assert state.last_activity == 4     # init(1) + one tick per line

    def test_non_json_lines_are_skipped_never_raised(self):
        state = self._feed(['hello\n', '{"broken\n', '[1,2,3]\n', 'null\n'])
        assert state.result_event is None   # nothing mistaken for a verdict...
        assert state.last_activity == 5     # ...but all four still counted as liveness

    def test_verdict_comes_from_the_final_result_event(self):
        stale = json.dumps({"type": "result", "structured_output": {"passed": False, "error": "x"}})
        final = json.dumps({"type": "result", "structured_output": VERDICT_OK})
        state = self._feed([stale + "\n", '{"type":"assistant"}\n', final + "\n"])
        assert ex.parse_verdict(state.result_event) == VERDICT_OK

    def test_stderr_is_drained_but_does_not_stamp_liveness(self):
        ticks = iter(range(1, 10))
        state = ex.StreamState(now=lambda: next(ticks))
        state.on_stderr_line("warning\n")
        state.on_stderr_line("another\n")
        assert state.last_activity == 1     # still the initial stamp
        assert "warning" in "".join(state.stderr_tail)

    def test_tails_are_bounded(self):
        state = ex.StreamState(now=time.monotonic)
        for i in range(ex.TAIL_LINES * 3):
            state.on_stdout_line(f"line {i}\n")
        assert len(state.stdout_tail) == ex.TAIL_LINES
        assert f"line {ex.TAIL_LINES * 3 - 1}" in "".join(state.stdout_tail)

    @pytest.mark.parametrize("line", ["", "\n", "not json", '{"broken', "[1,2]", "null", "42"])
    def test_safe_json_loads_never_raises(self, line):
        assert ex._safe_json_loads(line) is None


# ---------------------------------------------------------------------------
# Retry routing — status driven solely by AttemptResult + StateStore (D6/D7)
# ---------------------------------------------------------------------------

def _attempt(outcome, *, verdict=None, kill_reason=None, return_code=0):
    return ex.AttemptResult(
        outcome=outcome, verdict=verdict, kill_reason=kill_reason, signal=None,
        elapsed=1.0, last_activity_age=0.1, return_code=return_code,
        stderr_tail="", stdout_tail="", saw_result_with_structured_output=verdict is not None,
    )


class TestRetryRouting:
    """Drives _execute_single_step off AttemptResults alone — no child, no real time."""

    STEP = {"step": 2, "name": "ui"}

    def _drive(self, executor, attempts):
        """Queue one AttemptResult per invocation; return the preambles the child was given."""
        queued = list(attempts)
        preambles = []
        executor._commits = []

        def fake_invoke(step, preamble, attempt=1):
            preambles.append(preamble)
            return queued.pop(0)

        executor._invoke_claude = fake_invoke
        executor._commit_step = lambda num, name: executor._commits.append((num, name))
        return preambles

    def _step2(self, executor):
        return next(s for s in executor._state.load()["steps"] if s["step"] == 2)

    def test_completed_verdict_marks_completed_and_commits(self, executor):
        self._drive(executor, [_attempt(ex.OUTCOME_COMPLETED, verdict=VERDICT_OK)])
        assert executor._execute_single_step(self.STEP, "") is True
        s = self._step2(executor)
        assert s["status"] == "completed"
        assert s["summary"] == "ui shipped"     # the summary comes from the verdict
        assert executor._commits == [(2, "ui")]

    def test_blocked_verdict_exits_2_and_records_the_reason(self, executor, top_index):
        self._drive(executor, [_attempt(
            ex.OUTCOME_BLOCKED,
            verdict={"passed": False, "error": "no key", "blocked": True,
                     "blocked_reason": "ANTHROPIC_API_KEY missing"},
        )])
        with pytest.raises(SystemExit) as exc_info:
            executor._execute_single_step(self.STEP, "")
        assert exc_info.value.code == 2
        s = self._step2(executor)
        assert s["status"] == "blocked"
        assert s["blocked_reason"] == "ANTHROPIC_API_KEY missing"
        top = next(t for t in json.loads(top_index.read_text())["tasks"] if t["dir"] == "0-mvp")
        assert top["status"] == "blocked"

    def test_fail_then_success_retries_and_feeds_the_error_forward(self, executor):
        preambles = self._drive(executor, [
            _attempt(ex.OUTCOME_FAIL, verdict={"passed": False, "error": "3 tests red"}),
            _attempt(ex.OUTCOME_COMPLETED, verdict={"passed": True, "summary": "green"}),
        ])
        assert executor._execute_single_step(self.STEP, "") is True
        assert len(preambles) == 2
        assert "Previous attempt failed" in preambles[1]
        assert "3 tests red" in preambles[1]    # the retry is told what went wrong
        assert self._step2(executor)["status"] == "completed"

    def test_error_after_max_retries_exits_1(self, executor, top_index):
        fail = _attempt(ex.OUTCOME_FAIL, verdict={"passed": False, "error": "still red"})
        preambles = self._drive(executor, [fail] * ex.StepExecutor.MAX_RETRIES)
        with pytest.raises(SystemExit) as exc_info:
            executor._execute_single_step(self.STEP, "")
        assert exc_info.value.code == 1
        assert len(preambles) == ex.StepExecutor.MAX_RETRIES
        s = self._step2(executor)
        assert s["status"] == "error"
        assert f"failed after {ex.StepExecutor.MAX_RETRIES} attempts" in s["error_message"]
        assert "still red" in s["error_message"]
        top = next(t for t in json.loads(top_index.read_text())["tasks"] if t["dir"] == "0-mvp")
        assert top["status"] == "error"

    def test_a_timeout_kill_is_diagnosable_in_the_recorded_error(self, executor):
        killed = _attempt(ex.OUTCOME_FAIL, kill_reason=ex.DECISION_KILL_IDLE, return_code=-9)
        self._drive(executor, [killed] * ex.StepExecutor.MAX_RETRIES)
        with pytest.raises(SystemExit) as exc_info:
            executor._execute_single_step(self.STEP, "")
        assert exc_info.value.code == 1
        assert ex.DECISION_KILL_IDLE in self._step2(executor)["error_message"]

    def test_a_verdictless_child_fails_rather_than_passing(self, executor):
        # D7: a `success` subtype carrying no structured_output is still a failure.
        self._drive(executor, [_attempt(ex.OUTCOME_FAIL, return_code=0)] * ex.StepExecutor.MAX_RETRIES)
        with pytest.raises(SystemExit) as exc_info:
            executor._execute_single_step(self.STEP, "")
        assert exc_info.value.code == 1
        assert "no usable verdict" in self._step2(executor)["error_message"]

    def test_a_stray_child_edit_to_index_json_is_discarded(self, executor):
        # T1-B: under --dangerously-skip-permissions the child *can* write index.json.
        # Status must come from the verdict, so its self-declared "completed" is ignored.
        attempts = []

        def fake_invoke(step, preamble, attempt=1):
            attempts.append(attempt)
            data = json.loads(executor._index_file.read_text())
            for s in data["steps"]:
                if s["step"] == 2:
                    s["status"] = "completed"
                    s["summary"] = "I promise it worked"
            executor._index_file.write_text(json.dumps(data))
            return _attempt(ex.OUTCOME_FAIL, verdict={"passed": False, "error": "AC failed"})

        executor._invoke_claude = fake_invoke
        executor._commit_step = lambda num, name: None

        with pytest.raises(SystemExit) as exc_info:
            executor._execute_single_step(self.STEP, "")
        assert exc_info.value.code == 1
        assert attempts == [1, 2, 3]    # it retried; the on-disk "completed" never short-circuited
        assert self._step2(executor)["status"] == "error"


# ---------------------------------------------------------------------------
# run_child — OS semantics against a stand-in child (sub-second bounds)
# ---------------------------------------------------------------------------

def _fake_child(tmp_path, name, body):
    """Write a tiny python script that stands in for `claude`, and return its argv."""
    p = tmp_path / name
    p.write_text(textwrap.dedent(body))
    return [sys.executable, str(p)]


def _run_child(cmd, tmp_path):
    return ex.run_child(cmd, cwd=str(tmp_path), env=dict(os.environ))


@pytest.fixture
def fast_bounds(monkeypatch):
    """Shrink the layered bound to test scale; the decision logic itself is unchanged."""
    monkeypatch.setattr(ex, "T_IDLE_SEC", 0.3)
    monkeypatch.setattr(ex, "T_MAX_SEC", 30.0)
    monkeypatch.setattr(ex, "KILL_GRACE_SEC", 0.4)
    monkeypatch.setattr(ex, "READER_JOIN_SEC", 3.0)


class TestRunChildIntegration:
    def test_a_stalled_child_is_idle_killed(self, tmp_path, fast_bounds):
        cmd = _fake_child(tmp_path, "stall.py", '''
            import json, time
            for i in range(2):
                print(json.dumps({"type": "assistant", "i": i}), flush=True)
            time.sleep(60)
        ''')
        result = _run_child(cmd, tmp_path)
        assert result.kill_reason == ex.DECISION_KILL_IDLE
        assert result.outcome == ex.OUTCOME_FAIL      # a kill always yields a deterministic fail
        assert result.verdict is None
        assert result.elapsed < 5                     # bounded by T_idle, not by the 60s sleep

    def test_steady_slow_output_is_not_killed(self, tmp_path, fast_bounds):
        cmd = _fake_child(tmp_path, "steady.py", '''
            import json, time
            for i in range(8):
                print(json.dumps({"type": "assistant", "i": i}), flush=True)
                time.sleep(0.1)
            print(json.dumps({"type": "result", "subtype": "success",
                              "structured_output": {"passed": True, "summary": "ok"}}), flush=True)
        ''')
        result = _run_child(cmd, tmp_path)
        assert result.kill_reason is None             # no false positive on a slow-but-alive step
        assert result.outcome == ex.OUTCOME_COMPLETED
        assert result.verdict == {"passed": True, "summary": "ok"}
        assert result.return_code == 0
        assert result.saw_result_with_structured_output is True

    def test_the_wall_clock_backstop_kills_an_endlessly_chatty_child(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ex, "T_IDLE_SEC", 30.0)   # never idle...
        monkeypatch.setattr(ex, "T_MAX_SEC", 0.4)     # ...but out of total budget
        monkeypatch.setattr(ex, "KILL_GRACE_SEC", 0.4)
        cmd = _fake_child(tmp_path, "chatty.py", '''
            import json, time
            while True:
                print(json.dumps({"type": "assistant"}), flush=True)
                time.sleep(0.02)
        ''')
        result = _run_child(cmd, tmp_path)
        assert result.kill_reason == ex.DECISION_KILL_WALL
        assert result.outcome == ex.OUTCOME_FAIL

    def test_sigterm_ignoring_child_escalates_to_sigkill_and_reaps_its_grandchild(
            self, tmp_path, fast_bounds):
        cmd = _fake_child(tmp_path, "stubborn.py", '''
            import json, signal, subprocess, sys, time
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            grandchild = subprocess.Popen([sys.executable, "-c",
                "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)"])
            print(json.dumps({"type": "assistant", "grandchild": grandchild.pid}), flush=True)
            time.sleep(300)
        ''')
        result = _run_child(cmd, tmp_path)

        assert result.kill_reason == ex.DECISION_KILL_IDLE
        assert result.signal == signal.SIGKILL    # SIGTERM was ignored, so we escalated

        pid = next(json.loads(line)["grandchild"]
                   for line in result.stdout_tail.splitlines() if "grandchild" in line)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"grandchild {pid} survived the process-group kill")

    def test_an_stderr_flood_does_not_deadlock_the_child(self, tmp_path, fast_bounds):
        # 240KB of stderr, far past the ~64KB pipe buffer. Without a concurrent stderr reader
        # the child blocks in write(), never emits its verdict, and gets idle-killed.
        cmd = _fake_child(tmp_path, "noisy.py", '''
            import json, sys
            print(json.dumps({"type": "assistant"}), flush=True)
            sys.stderr.write("noise\\n" * 40000)
            sys.stderr.flush()
            print(json.dumps({"type": "result", "subtype": "success",
                              "structured_output": {"passed": True, "summary": "survived"}}), flush=True)
        ''')
        result = _run_child(cmd, tmp_path)
        assert result.kill_reason is None
        assert result.verdict == {"passed": True, "summary": "survived"}
        assert len(result.stderr_tail) <= ex.TAIL_CHARS   # bounded tail, not the whole flood

    def test_partial_and_malformed_lines_do_not_lose_the_verdict(self, tmp_path, fast_bounds):
        cmd = _fake_child(tmp_path, "messy.py", '''
            import json, sys
            sys.stdout.write('{"type": "result", "structured_ou')   # truncated, never completed
            sys.stdout.write("\\n")
            print("plain prose the CLI decided to print", flush=True)
            print(json.dumps({"type": "result", "subtype": "success",
                              "structured_output": {"passed": True, "summary": "recovered"}}), flush=True)
        ''')
        result = _run_child(cmd, tmp_path)
        assert result.outcome == ex.OUTCOME_COMPLETED
        assert result.verdict == {"passed": True, "summary": "recovered"}
