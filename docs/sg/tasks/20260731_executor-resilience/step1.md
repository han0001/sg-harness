# Step 1: verdict-contract

## Files to read

- `/CLAUDE.md` — invariants and working discipline.
- `/docs/sg/plan/20260731_executor-resilience/plan.md` — the design (decisions embedded below).
- `/skills/sg-execute-task/scripts/execute.py` — the file you will modify (note the `StateStore` added in step 0).
- `/skills/sg-execute-task/scripts/test_execute.py` — existing tests.

## Task

Add the **pure verdict contract** to `execute.py`: module-level constants + functions with **no subprocess, no threads, no real time, no file I/O.** These are the "pure core" that step 2 (the streaming runner) will consume. Prefer plain functions over classes (no state to hold).

Background (plan Q2, D5): the child Claude session no longer edits `index.json`. Instead the CLI is invoked with `--json-schema`, and the child's final `result` event carries `structured_output` = a **verdict**. `execute.py` parses that verdict and maps it to a step status. Because the stream-json event schema is officially **undocumented**, all parsing here must be **defensive**: any ambiguity resolves to `fail`, never a raised exception.

### 1. `VERDICT_SCHEMA` (a JSON-schema dict)

Shape (plan §4 "Verdict schema shape", keep minimal — YAGNI):

- `passed`: bool — **required**
- `summary`: str
- `error`: str
- `blocked`: bool
- `blocked_reason`: str

Add JSON-schema conditionals: **require `error` when `passed` is `false`**, and **require `blocked_reason` when `blocked` is `true`** (for diagnosability). Do not add fields beyond these.

### 2. `parse_verdict(result_event: dict) -> dict | None`

Given the already-JSON-parsed final `result` event, return the verdict dict found at its `structured_output`, or `None` if it is absent, not a dict, or otherwise malformed. Never raise.

### 3. `classify_outcome(verdict: dict | None, kill_reason: str | None) -> str`

Return one of `"completed"`, `"blocked"`, `"fail"`. Rules (plan D7 — route all failures to one channel):

- `kill_reason` is set (e.g. `"timeout-idle"` / `"timeout-wall"`) → `"fail"`.
- `verdict is None` → `"fail"`.
- `verdict.get("blocked") is True` → `"blocked"`.
- `verdict.get("passed") is True` → `"completed"`.
- otherwise (`passed` false / missing / non-bool) → `"fail"`.

### 4. Result-event helpers

- `is_result_event(obj: dict) -> bool` — detect the final `result` event of a stream-json stream (key on the event's `type`/role field as emitted by the CLI; be lenient).
- Treat a **`success` subtype that carries no `structured_output`** as `fail` (plan D7): i.e. `parse_verdict` returns `None` for it, so `classify_outcome` yields `"fail"`.

### Core rule (must not drift)

**Parse defensively; resolve every ambiguity to `fail`, never crash.** Reason (plan D5/§4): a raw exception on an unexpected/undocumented stream shape reintroduces the T1-B crash class this whole task removes.

## Acceptance Criteria

```bash
python3 -m pytest skills/sg-execute-task/scripts/test_execute.py -q
```

Add a `TestVerdictContract` class covering every T1-B case (plan §6 L1):
- `passed=true` → `completed`; `passed=false` → `fail`; `blocked=true` → `blocked`.
- `structured_output` **absent**, **malformed** (not a dict / wrong-typed), and a `success` subtype **without** `structured_output` → `parse_verdict` returns `None` → `classify_outcome` returns `fail`.
- `kill_reason` set with any/no verdict → `fail`.
- `VERDICT_SCHEMA` requires `passed`; requires `error` when `passed=false`; requires `blocked_reason` when `blocked=true`.

## Verification procedure

1. Run the AC command above.
2. Architecture checklist:
   - Are these functions genuinely pure (no subprocess/threads/time/file I/O)?
   - Does every malformed/absent input path return `fail`/`None` rather than raise?
3. Update this step in `docs/sg/tasks/20260731_executor-resilience/index.json` (success → `completed` + `summary`; failure → `error` + `error_message`; needs intervention → `blocked` + `blocked_reason`, then stop).

## Prohibited

- Do not spawn subprocesses, start threads, read the clock, or touch the filesystem here. Reason: this is the pure core; step 2 owns all of that. Keeping it pure is what makes plan §6 L1 fast and deterministic.
- Do not let any parsing path raise on unexpected input. Reason: that is exactly the crash class (T1-B) being eliminated.
- Do not break existing tests.
