# Step 0: state-store

## Files to read

First read these to understand the architecture, invariants, and the exact code you will refactor:

- `/CLAUDE.md` — project memory: Vocabulary, **Invariants** (esp. #2 "only the orchestrator touches git" — this task extends it to `index.json`), Non-Goals, Working discipline (minimal change).
- `/docs/sg/plan/20260731_executor-resilience/plan.md` — the design (deeper context; the decisions you need are embedded below).
- `/skills/sg-execute-task/scripts/execute.py` — the file you will modify. Study `StepExecutor` end to end.
- `/skills/sg-execute-task/scripts/test_execute.py` — the existing safety-net tests (52 currently pass).

## Task

Extract a new class **`StateStore`** inside `execute.py` (same file — do NOT create a new module) that becomes the **sole owner of all `index.json` (per-task) and top-index (`docs/sg/tasks/index.json`) reads, writes, status transitions, and timestamps.** `StepExecutor` keeps its public method names but delegates state work to a `StateStore` instance it holds.

This is a **behavior-preserving refactor**. No functional/observable change yet — it is the structural foundation for later steps (D6 sole-writer hard-enforcement, D11 SRP split). Rationale (plan §2, D11): today `StepExecutor` changes for three unrelated reasons — how steps run, how state is stored, how git is used. Concentrating all state ownership in one class makes the single-writer invariant *structural* and independently testable.

### What moves into `StateStore`

Move the logic (not necessarily verbatim) of these current `StepExecutor` members so that **every write to a per-task `index.json` or the top index goes through `StateStore`**:

- JSON I/O used for state: the `_read_json` / `_write_json` behavior (keep UTF-8, `indent=2`, `ensure_ascii=False`).
- `_stamp` (the KST `TZ` timestamp). **Inject the clock:** `StateStore.__init__(self, index_file, top_index_file, task_dir_name, *, now=<callable returning the stamp string>)`, defaulting to the real KST stamp. Reason: plan §6 requires the transition logic be testable without real time.
- `_ensure_created_at`, `_build_step_context`, `_check_blockers`, `_update_top_index`.
- The in-place status mutations currently **inlined** in `_execute_single_step` and `_execute_all_steps`. Expose them as named, single-responsibility methods, e.g.:
  - `mark_started(step_num)` — set `started_at` if absent.
  - `mark_completed(step_num, summary)` — set `status="completed"`, `completed_at`, and `summary`.
  - `mark_blocked(step_num, reason)` — set `blocked_at` (and `blocked_reason`).
  - `mark_retry(step_num)` — set `status="pending"`, drop `error_message`.
  - `mark_error(step_num, message)` — set `status="error"`, `error_message`, `failed_at`.
  - read helpers as needed: `load()`, `status_of(step_num)`, `summary_of`, `error_of`, `blocked_reason_of`, `next_pending()`, `count_completed()`.

### Core rules (must not drift)

1. **Single writer.** After this step, `StateStore` is the ONLY code path that calls the JSON *write* on any `index.json` / top index. The `Runner` (`StepExecutor`) must not write those files directly. Reason: this property is what step 2 (D6) hardens into "T1-B impossible by construction"; if writes leak back into the Runner, the invariant is lost.
2. **Behavior-preserving.** Do not change any observable behavior, printed strings, exit codes, timestamp format, or file contents. Reason: step 0 is a safety refactor; behavior changes belong to later steps. The existing tests define the contract — only update a test where a symbol genuinely *moved* (call site / import path changed), never to loosen an assertion.
3. **Keep the seam thin.** Prefer keeping `StepExecutor`'s existing public method names as thin delegators to `StateStore` where that avoids churn in the existing tests (e.g. `StepExecutor._read_json` / `_write_json` / `_stamp` may remain as passthroughs). Reason: minimal change (CLAUDE.md working discipline).

### Not in scope

- Do NOT extract git into a separate class. Reason: the user decided to **defer** D11's Git split (YAGNI for this task); git methods stay on `StepExecutor` unchanged.
- Do NOT touch `_invoke_claude`, the timeout, the preamble, or the verdict/schema. Reason: those are later steps; keep this diff to state ownership only.

## Acceptance Criteria

```bash
python3 -m pytest skills/sg-execute-task/scripts/test_execute.py -q
```

All tests must pass. Add new focused `StateStore` unit tests (a `TestStateStore` class) covering: each transition sets the correct `status` + timestamp key (with an **injected `now`**, asserting the exact stamp — no real time); `next_pending`; `count_completed`; `build_step_context` output unchanged; `check_blockers` exits 1 on an `error` step and 2 on a `blocked` step.

## Verification procedure

1. Run the AC command above.
2. Architecture checklist:
   - Is `StateStore` the only writer of `index.json` / top index (grep for the JSON write call site)?
   - Did any observable behavior, string, or exit code change? (It must not.)
   - Does it respect CLAUDE.md Invariant 2 and the minimal-change discipline?
3. Update this step in `docs/sg/tasks/20260731_executor-resilience/index.json`:
   - success → `"status": "completed"`, `"summary": "one-line summary of the output"`
   - still failing after 3 fix attempts → `"status": "error"`, `"error_message": "concrete detail"`
   - user intervention needed → `"status": "blocked"`, `"blocked_reason": "concrete reason"`, then stop immediately

## Prohibited

- Do not change observable behavior/output/exit codes. Reason: this is a behavior-preserving refactor; drift here corrupts the safety net for later steps.
- Do not create a new `.py` module for `StateStore`. Reason: the user chose a same-file class to keep the change minimal and avoid plugin/import wiring.
- Do not extract git or modify `_invoke_claude`. Reason: out of scope for this step (see above).
- Do not break existing tests.
