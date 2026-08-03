# Step 3: preflight

## Files to read

- `/CLAUDE.md` — invariants and Fail-Fast (blow up on invalid state before mutating anything).
- `/docs/sg/plan/20260731_executor-resilience/plan.md` — D9 and §7 (version provenance). Decisions embedded below.
- `/skills/sg-execute-task/scripts/execute.py` — the file you will modify. Note `run()` and its ordering (`_check_blockers` → `_checkout_branch` → `_ensure_created_at` → execute steps). Steps 0–2 already landed.
- `/skills/sg-execute-task/scripts/test_execute.py` — existing tests.

## Task

Add a **mandatory preflight** that runs **before ANY git or state mutation** and aborts the run if the local `claude` CLI cannot support the new contract. This is Fail-Fast (plan D9): an older CLI can silently ignore an invalid `--json-schema` or truncate the stream tail, so a run that will misbehave must never start.

### 1. `preflight_check(run=subprocess.run) -> None`

- Take the subprocess runner as a parameter (default `subprocess.run`) so tests can inject a fake. Do not use an output parameter.
- Probe the local `claude`:
  - Runs `claude --version`; parse the version and compare against a module-level floor `MIN_CLAUDE_VERSION` (use the empirically verified floor from plan §7: **`2.1.216`**).
  - Confirm the CLI advertises `--json-schema` and `--output-format stream-json` (e.g. check `claude --help` output). Keep this minimal — a version at/above the floor plus the flags present is sufficient.
- On ANY failure (`claude` missing, version below floor, flags absent) → print a **clear, actionable** message (what is wrong, the required floor, how to upgrade) and `sys.exit(1)`. On success → return `None` (do not print noise).

### 2. Wire it into `run()`

Call `preflight_check()` as the **first** action in `StepExecutor.run()`, before `_check_blockers`, `_checkout_branch`, and `_ensure_created_at`. Reason (D9): abort before mutating git/state.

### Core rule (must not drift)

**Preflight is mandatory and runs before any git/state mutation.** Reason (plan D9): it is not a `--flag`; a silently-misbehaving run (ignored schema, truncated stream) is worse than a clear early abort.

## Acceptance Criteria

```bash
python3 -m pytest skills/sg-execute-task/scripts/test_execute.py -q
```

Add a `TestPreflight` class (inject a fake `run`): version **at/above** floor + flags present → returns without raising; version **below** floor → `SystemExit(1)` with a message naming the floor; `claude` **missing** (FileNotFoundError / non-zero) → `SystemExit(1)`; required flag **absent** → `SystemExit(1)`. Do not require a real `claude` binary in these tests.

## Verification procedure

1. Run the AC command above.
2. Architecture checklist:
   - Does `preflight_check` run before `_checkout_branch` / any state write in `run()`?
   - Is the failure message actionable (states the floor and remedy)?
   - Is the subprocess runner injected (testable without a real CLI)?
3. Update this step in `docs/sg/tasks/20260731_executor-resilience/index.json` (success → `completed` + `summary`; failure → `error` + `error_message`; needs intervention → `blocked` + `blocked_reason`, then stop).

## Prohibited

- Do not run preflight after checkout or any state stamping. Reason: D9 — a run that will misbehave must abort before mutating git/state.
- Do not make preflight optional / flag-gated. Reason: plan D9 says mandatory.
- Do not hard-depend on a real `claude` binary in the tests. Reason: the CI gate has no network/auth (plan §6 L1 is the default gate; real-CLI checks are the excluded L3).
- Do not break existing tests.
