# Step 2: liveness-runner

## Files to read

- `/CLAUDE.md` — invariants (esp. #2, now extended to `index.json`), non-goals (no worktrees/parallel), working discipline.
- `/docs/sg/plan/20260731_executor-resilience/plan.md` — the design (§2 data flow, D1–D10). Decisions embedded below.
- `/skills/sg-execute-task/scripts/execute.py` — the file you will modify. Depends on **step 0** (`StateStore`, the sole writer) and **step 1** (`VERDICT_SCHEMA`, `parse_verdict`, `classify_outcome`, `is_result_event`).
- `/skills/sg-execute-task/scripts/test_execute.py` — existing tests. `TestInvokeClaude` will need updating (see AC).

## Task

Replace the blunt `subprocess.run(..., timeout=1800)` in `_invoke_claude` with a **live-streaming spawn + layered timeout + correct process-group kill**, return a structured `AttemptResult`, and rewire the retry loop to route via `StateStore` and the verdict (never via the child's on-disk `index.json`). This closes **T1-A** (timeout crash / false-kill of long steps) and completes **T1-B** (D6 hard-enforcement).

### 1. Constants (module-level, env-overridable)

Expose as constants read from `os.environ` with these defaults (plan §4 "N values"):

- `BASH_MAX_TIMEOUT_MS = 8 * 60 * 1000` — caps the child's max legitimate silence (plan D4).
- `T_IDLE_SEC = 12 * 60` — idle watchdog (sized above the Bash cap + margin).
- `T_MAX_SEC = 90 * 60` — wall-clock backstop.
- `MAX_TURNS = 50` — `--max-turns` semantic cap.
- `MAX_RETRIES` stays **3** (unchanged on `StepExecutor`). Reason (plan §5): nothing in T1-A/T1-B motivates changing retry policy.

Keep them module-level so tests can monkeypatch tiny values (e.g. `T_IDLE_SEC ≈ 0.3`).

### 2. Spawn (plan §2, D3/D4/D8)

Replace the `subprocess.run` call with `subprocess.Popen`:

```
claude -p --dangerously-skip-permissions \
       --output-format stream-json --verbose \
       --json-schema <VERDICT_SCHEMA> --max-turns <MAX_TURNS> <prompt>
```

- `start_new_session=True` (own process group — so the child's bash/test grandchildren can be reaped).
- `stdout=PIPE, stderr=PIPE, text=True`.
- `env={**os.environ, "BASH_MAX_TIMEOUT_MS": str(BASH_MAX_TIMEOUT_MS), "BASH_DEFAULT_TIMEOUT_MS": str(BASH_MAX_TIMEOUT_MS)}`.
- The CLI's accepted form for `--json-schema` (inline JSON vs a file path) is not certain — verify against `claude --help` and pass it in whatever form the CLI accepts (write a temp schema file if required).

### 3. Reader threads — drain stdout AND stderr concurrently (plan D8)

- A **stdout** reader thread: for each line → stamp `last_activity = now()`; accumulate the raw line; try `json.loads` and if `is_result_event(obj)` keep it as the final result event. A non-JSON line updates `last_activity` but is otherwise skipped (liveness keys on "a line arrived," never on event type — plan D3).
- A **stderr** reader thread draining concurrently (keep a bounded tail for diagnostics). Reason: a full `stderr` pipe can deadlock the child (plan D8).

### 4. Layered timeout — the decision must be a PURE function

Add a pure, injectable function so it is unit-testable without real time (plan §6 L1):

```
timeout_decision(now, last_activity, start, t_idle, t_max) -> "keep" | "kill-idle" | "kill-wall"
```

A watchdog polls it on a short interval; on `kill-idle`/`kill-wall` it kills. `kill-wall` if total elapsed > `t_max`; `kill-idle` if since-last-activity > `t_idle`.

### 5. Kill correctly (plan D8)

`SIGTERM` the process **group** (`os.killpg(os.getpgid(pid), SIGTERM)`) → grace period → `SIGKILL` the group → reap; **tolerate the child exiting mid-kill** (`suppress(ProcessLookupError)`); then join reader threads with a bounded timeout. `subprocess`'s own `timeout=` must NOT be used — it only raises, it does not kill the group.

### 6. `AttemptResult` + observability (plan D10)

Build an `AttemptResult` (a small dataclass or dict) with: `outcome` (from `classify_outcome`), `verdict`, `kill_reason`, `signal`, `elapsed`, `last_activity_age`, `return_code`, `stderr_tail`, `saw_result_with_structured_output` (bool). Write `step{N}-output.json` with **all** of these fields (a timeout must be *diagnosable*, not just retryable).

### 7. Rewire the retry loop — D6 hard-enforcement (plan D6/D7)

In `_execute_single_step`, drive status **solely** from `AttemptResult` + `StateStore` — **do not re-read the child's on-disk `index.json` for control flow** (delete today's `index = self._read_json(...)` / `status = next(...)` at `execute.py:339-340`). Route (plan D7):

- `completed` → `StateStore.mark_completed(step_num, summary=verdict["summary"])` → commit → return True.
- `blocked` → `StateStore.mark_blocked(step_num, reason=verdict["blocked_reason"])` → update top index → `sys.exit(2)`.
- `fail` → if `attempt < MAX_RETRIES`: `StateStore.mark_retry(step_num)`, set `prev_error` (from `verdict["error"]` or the `kill_reason`); else `StateStore.mark_error(...)` → commit → update top index → `sys.exit(1)`.

Because status now comes only from the verdict, a stray child edit to `index.json` (possible under `--dangerously-skip-permissions`) is **discarded, not trusted** — T1-B becomes impossible by construction.

### Core rules (must not drift)

1. **Never read the child's on-disk `index.json` to decide status.** Reason: that IS T1-B; D6 requires status to come only from `AttemptResult`/verdict.
2. **Never use `subprocess.run(..., timeout=)`.** Reason: it only raises; the child's grandchildren leak and the hang still crashes the run (T1-A/D8).
3. **Never let a malformed stream line raise** in the reader. Reason: reintroduces the crash class this task removes.
4. **Drain stdout and stderr concurrently.** Reason: a full stderr pipe deadlocks (D8).
5. **A kill always yields a deterministic `fail`** `AttemptResult`. Reason: plan §2 — kill feeds the same fail→retry→error path.

## Acceptance Criteria

```bash
python3 -m pytest skills/sg-execute-task/scripts/test_execute.py -q
```

Update/replace `TestInvokeClaude` (the old `subprocess.run` + `timeout=1800` + `--output-format json` assumptions are gone; delete `test_timeout_is_1800`). Add:
- **L1 (pure/fast):** `timeout_decision` → keep / kill-idle / kill-wall via injected values; NDJSON handling (feed a list of fake lines → `last_activity` advances per line, verdict parsed from the final `result` event, non-JSON lines skipped); `Popen` env carries `BASH_MAX_TIMEOUT_MS`; **retry routing** (outcome + attempt → pending-retry / error-after-MAX / blocked, with exit codes 1/2) — this closes the currently-untested `_execute_single_step`.
- **L2 (fake-child integration, sub-second):** a tiny stand-in script for `claude`: prints N lines then stalls → idle-timeout kills it (`T_IDLE_SEC`≈0.3); steady slow output with gaps < `T_idle` → NOT killed (no false positive); a child that ignores `SIGTERM` → `SIGKILL` escalation works and a **grandchild** is reaped (no zombie); stderr backpressure / partial NDJSON handled without deadlock.

## Verification procedure

1. Run the AC command above.
2. Architecture checklist:
   - Is the child's on-disk `index.json` never read for control flow (grep confirms)?
   - Is `StateStore` still the sole writer?
   - Is the process group killed (not just the leader), tolerant of `ProcessLookupError`?
   - Are both pipes drained concurrently?
3. Update this step in `docs/sg/tasks/20260731_executor-resilience/index.json` (success → `completed` + `summary`; failure → `error` + `error_message`; needs intervention → `blocked` + `blocked_reason`, then stop).

## Prohibited

- Do not read the child's `index.json` to decide status. Reason: T1-B / D6.
- Do not use `subprocess.run(..., timeout=)`. Reason: it does not kill the process group; hang still crashes (T1-A/D8).
- Do not change the preamble or the `sg-decompose-task` template here. Reason: that is step 4 — keep this diff to the runner. (Leaving the old "edit index.json" preamble text in place is harmless now, precisely because D6 discards any child edit.)
- Do not add a new `running` status, worktrees, or parallel execution. Reason: explicit plan non-goals (§5, YAGNI).
- Do not break existing tests.
