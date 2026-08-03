"""
Safety-net tests for execute.py refactoring.
Verify that behavior is identical before and after refactoring.
"""

import json
import os
import subprocess
import sys
import textwrap
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
# _invoke_claude (mocked)
# ---------------------------------------------------------------------------

class TestInvokeClaude:
    def test_invokes_claude_with_correct_args(self, executor):
        mock_result = MagicMock(returncode=0, stdout='{"result": "ok"}', stderr="")
        step = {"step": 2, "name": "ui"}
        preamble = "PREAMBLE\n"

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            output = executor._invoke_claude(step, preamble)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--output-format" in cmd
        assert "PREAMBLE" in cmd[-1]
        assert "Implement the UI" in cmd[-1]

    def test_saves_output_json(self, executor):
        mock_result = MagicMock(returncode=0, stdout='{"ok": true}', stderr="")
        step = {"step": 2, "name": "ui"}

        with patch("subprocess.run", return_value=mock_result):
            executor._invoke_claude(step, "preamble")

        output_file = executor._task_dir / "step2-output.json"
        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert data["step"] == 2
        assert data["name"] == "ui"
        assert data["exitCode"] == 0

    def test_nonexistent_step_file_exits(self, executor):
        step = {"step": 99, "name": "nonexistent"}
        with pytest.raises(SystemExit) as exc_info:
            executor._invoke_claude(step, "preamble")
        assert exc_info.value.code == 1

    def test_timeout_is_1800(self, executor):
        mock_result = MagicMock(returncode=0, stdout="{}", stderr="")
        step = {"step": 2, "name": "ui"}

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            executor._invoke_claude(step, "preamble")

        assert mock_run.call_args[1]["timeout"] == 1800


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
