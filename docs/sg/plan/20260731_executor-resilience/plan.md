---
task: executor-resilience
date: 20260731
status: design
task_dir: 20260731_executor-resilience
---

# Design Document: Executor resilience — self-correcting timeouts & orchestrator-owned status

## 1. Goal and Motivation

`execute.py` has **no exception handling anywhere** (the only `try:` is the progress
indicator's cleanup). Two *expected operational* failures therefore crash the whole
orchestrator with a raw traceback, bypassing the self-correction the harness advertises:

- **T1-A — timeout crash.** `_invoke_claude` runs `subprocess.run(..., timeout=1800)`
  (`execute.py:265-268`). On a hung step this raises an uncaught `subprocess.TimeoutExpired`
  → the process dies mid-run (status stuck `pending`, `started_at` already stamped, no
  retry, no error recorded). Worse, the ceiling is a **fixed 30-min wall-clock**, so a
  legitimately long (e.g. 40-min) step is killed wrongly.
- **T1-B — corrupted-state crash.** The child LLM is the *writer* of `index.json`
  (preamble rule 5, `execute.py:246-249`). If it emits malformed JSON, the next
  `_read_json` (`execute.py:122-123`, bare `json.loads`) raises an uncaught
  `json.JSONDecodeError`. The run crashes, and the corrupted file then **blocks every
  future run** until a human hand-fixes it.

**Direction (Fail-Fast, correctly applied):** route *known* failure modes into the
existing `error`/retry channel instead of crashing; blow up only on the *truly
unexpected*. Two changes on one shared code path (how `execute.py` spawns the child and
reads its result), so they are designed and shipped **together**:

- **Q1 — liveness/timeout.** Replace the blunt wall-clock with a layered bound
  (idle + wall-clock backstop + `--max-turns`), driven by the child's **live event
  stream**.
- **Q2 — status ownership.** Extract a dedicated **StateStore** as the **sole writer** of
  `index.json` (an SRP split of today's do-everything `StepExecutor`). The child only
  *reports* a schema-validated verdict via structured output; it never edits `index.json`.
  This eliminates T1-B at the root.

## 2. Conceptual Model

**Two shifts — one structural, one behavioural.**

**(i) Split the god-class by responsibility (SRP).** Today `StepExecutor` runs steps *and*
owns `index.json` *and* drives git — three reasons to change in one class. Split into:

- **Runner** (`execute.py`): spawns the per-step child session, drives the retry loop,
  emits progress. Owns *no* file format — it delegates all state to the StateStore.
- **StateStore** (new module): the **sole owner** of `index.json` + the top-index. Every
  read / write / status-transition / timestamp lives here and nowhere else.
- **Git** (extracted helper): branch / commit / push.

These are **modules/classes in one process**, not separate services — a separate
state-owning process would be YAGNI and violates the harness's "no multi-user/concurrency"
non-goal.

**(ii) The child returns a result instead of mutating shared state.** Its role drops from
**"side-effecting `index.json`"** to **"returning a validated verdict."** Combined with (i),
the state file now has exactly **one writer — the StateStore** — which closes T1-B and
extends Invariant 2 ("only the orchestrator touches git") to "…and `index.json`."

**One command serves both axes:**

```
claude -p --output-format stream-json --verbose \
       --json-schema <verdict-schema> --max-turns N
```

- Live NDJSON lines → **idle watchdog (Q1)**. *Any new line = liveness; content
  irrelevant* (the stream-json event schema is undocumented, so we key on "a line
  arrived," never on a specific event type).
- Final `result` event carries `structured_output` = **the verdict (Q2)**. (Verified
  present on CLI v2.1.216 in both `json` and `stream-json` modes.)

**Data flow (one step):**

```
pending
  │  Runner spawns child:
  │    Popen(stream-json, start_new_session=True,
  │          env BASH_MAX_TIMEOUT_MS set)     ← bounds max legit silence
  ▼
reader thread: on each line → last_activity = now(); accumulate line
watchdog thread: if now-last_activity > T_idle  → kill;  if total > T_max → kill
  │
  ▼  on final `result` event → Runner parses structured_output (verdict)
StateStore writes index.json (status / summary / error / timestamps) — sole writer
  │
  ├─ passed=true            → completed  (+ commit)
  ├─ passed=false / killed / verdict missing/malformed → fail → retry loop → error
  └─ blocked=true           → blocked → stop (exit 2)
```

**Three-layer bound (industry consensus — Temporal, LangGraph, CI runners), all feeding
the same fail → retry → error path:**

| Layer | Bounds | Detects a hang | Role |
|-------|--------|----------------|------|
| ① idle timeout `T_idle` | gap between events | **fast** | primary detector |
| ② wall-clock `T_max` | total attempt time | slow | backstop ceiling |
| ③ `--max-turns` | tool-loop count | early | semantic cap (catches "emitting but looping forever") |

**Kill correctly:** `SIGTERM` the process group → grace period → `SIGKILL` the group →
reap. (`subprocess`'s `timeout=` only raises; it does not kill. The child spawns
bash/test subprocesses that must be reaped too — hence `start_new_session=True`.)

## 3. Decisions

| # | Decision | Choice |
|---|----------|--------|
| D1 | Q1 & Q2 scope | **One task.** They share the same spawn/read code path and `--output-format`; designing them apart risks two incompatible designs. |
| D2 | Liveness strategy | **Layered** (idle primary + wall-clock backstop + max-turns). *Not* wall-clock-alone (poor early detector) and *not* idle-alone (no backstop for infinite-emitting loop). |
| D3 | Idle detection mechanism | **Live pipe read** (`Popen` + reader thread), in-memory `last_activity` stamp + separate watchdog. **Not** file-mtime polling. Keyed on "any new line," not on event type. |
| D4 | Bound the silent gap | `execute.py` sets the child's `BASH_MAX_TIMEOUT_MS` (+`BASH_DEFAULT_TIMEOUT_MS`) so *max legit silence is a controlled quantity, not a measured one*; `T_idle` is sized just above it. |
| D5 | Verdict channel | `--json-schema` → validated `structured_output`. The CLI validates and retries the model; on unrecoverable failure it emits an error `subtype`, which `execute.py` treats as `fail`. |
| D6 | `index.json` ownership | **`execute.py` is the sole writer — hard-enforced.** Before spawn it snapshots `index.json`; after the child exits it **overwrites** the file from its own in-memory state + the parsed verdict, and never reads the child's version for control flow. So even under `--dangerously-skip-permissions` a child edit is *discarded, not trusted* — T1-B becomes **impossible, not merely discouraged**. The preamble still tells the child not to touch `index.json`/git (belt-and-suspenders). |
| D7 | Failure routing | timeout-kill, **missing/malformed** `structured_output` (incl. a `success` subtype that carries no `structured_output`), and `passed=false` ALL → `fail` → existing retry loop → `error` after `MAX_RETRIES`. A `blocked` verdict → stop (exit 2). |
| D8 | Process hygiene | `start_new_session=True` (process group); kill = `SIGTERM`→grace→`SIGKILL` on the group, **tolerant of the child exiting mid-kill** (suppress `ProcessLookupError`), then reap with bounded reader-thread joins. **Drain both stdout AND stderr** concurrently (`stderr=PIPE` can deadlock too). A kill always produces a deterministic `fail` verdict. |
| D9 | Preflight (Fail-Fast) | Before ANY git/state mutation (`_checkout_branch`, `_ensure_created_at`, `started_at`), **mandatorily** probe that this `claude` supports `--json-schema` + `stream-json` and meets the version floor. If not, abort with a clear message — never start a run that will silently misbehave. (Mandatory, not optional.) |
| D10 | Observability | `step{N}-output.json` records: attempt number, outcome (`completed`/`fail`/`timeout-idle`/`timeout-wall`/`blocked`), kill reason + signal, elapsed + last-activity age, child return code, `stderr` tail, and whether a `result` event carrying `structured_output` was seen. A timeout failure must be diagnosable, not just retryable. |
| D11 | Component split (SRP) | Break the `StepExecutor` god-class into **Runner** (execution / retry / progress), **StateStore** (sole owner of `index.json` + top-index), and **Git** (branch / commit / push) — modules/classes in one process, **not** separate services. Each gains one reason to change; the StateStore's single-writer invariant (D6) becomes *structural*; each unit becomes independently testable (§6). |

## 4. Open Questions

Settle at implementation time; recommended default given for each.

| Question | Recommended default |
|----------|---------------------|
| Verdict schema shape | `{ "passed": bool (required), "summary": str, "error": str, "blocked": bool, "blocked_reason": str }`. Require `error` when `passed=false` and `blocked_reason` when `blocked=true` (schema conditionals) for diagnosability. Keep minimal otherwise (YAGNI). |
| N values | Start: `BASH_MAX_TIMEOUT_MS=8min`, `T_idle=12min` (> 8min + margin), `T_max=90min` (> the 60-min legit ceiling), `--max-turns=50`. **`MAX_RETRIES` stays 3** — unchanged from current `execute.py:77`; there is no bug-driven reason to change it, and dropping to 2 would be unjustified drift against the project's minimal-change discipline. Expose the new values as constants (env-overridable). Tune `T_idle` to observed p99 of legitimate quiet gaps. |
| Also update `sg-decompose-task` step template? | **Yes, in scope.** The `step{N}.md` "update index.json" instructions conflict with D6. Move the verdict contract entirely into the preamble; simplify the template so generated steps stop telling the child to edit `index.json`. |
| Version floor | Enforced as a **mandatory preflight** (D9), not optional — an older CLI can silently ignore an invalid schema or truncate the stream tail. Also document the floor in README. |
| Backward compat with in-flight tasks | Existing `step{N}.md` files may still say "update index.json." **Genuinely harmless *only because* D6 overwrites the file from executor-owned state** — without D6's hard-enforcement it would NOT be harmless (a child following the old instruction could still corrupt it). Re-decompose when convenient. |
| `structured_output` field robustness | Present in the smoke test, but the stream-json event schema is officially undocumented → parse defensively; absent/unparseable ⇒ `fail` (D7). |

## 5. Decision Log

**D2 — layered timeout over wall-clock-alone.**
*Why:* Temporal/LangGraph/CI all converge on layering — a wall-clock sized to the
longest legit run is a poor early hang detector ("a 2-hour cap means a 5-min hang waits
~2 hours"). *Trade-off:* more code (`Popen` + reader/watchdog threads) than a one-line
`try/except`. *Rejected:* (a) fixed wall-clock only — crashes today and false-kills long
steps; (b) idle-only — no ceiling for an infinite-emitting loop; (c) LLM-written explicit
heartbeat — unreliable (a genuinely stuck child can't ping).

**D3 — live stream vs file mtime.**
*Why:* the pipe is the direct channel already in `execute.py`'s hands — no dependency on
a persisted log path or disk-flush timing, and it's immediate. *Trade-off:* a blocking
`readline` cannot also watch the clock, so liveness needs a *separate* watchdog thread.

**D4 — control `BASH_MAX_TIMEOUT_MS`.**
*Why:* the one thing research could not verify is whether the CLI emits keep-alives during
a single long tool call. Setting the child's Bash timeout ourselves converts "max healthy
silence" from an unknown into a *controlled* value, so `T_idle` can be sized safely above
it. *Trade-off:* caps the child's own long-running commands — acceptable, since AC
commands should be bounded anyway.

**D5/D6 — `--json-schema` + sole writer.**
*Why:* the CLI validates the schema and retries the model itself; the child can no longer
corrupt `index.json`, so **T1-B is eliminated at the root**, and it cleanly extends
Invariant 2. *Trade-off:* a bigger diff (preamble + decompose template change too) and a
CLI-version floor. *Rejected:* (a) child edits `index.json` in place (the current design —
this *is* T1-B); (b) parse a last-line sentinel from free-text stdout (fragile against LLM
format drift, and we'd have to hand-roll validation the CLI already does).

**D7 — route all failures to one channel.**
*Why:* unifies Q1 and Q2 — timeout-kill, missing verdict, and `passed=false` are all just
"this attempt failed," handled by the retry loop that already exists. This is Fail-Fast
*without swallowing*: known operational failures become a recorded `error`; a truly
unexpected exception still crashes. *Trade-off:* a genuinely-stuck deterministic step
burns its retries before erroring — bounded by `max-retries`.

### Non-goals for THIS task (guard against scope creep)

- **No Agent SDK migration.** The CLI flags (`--json-schema`, `stream-json`, `--max-turns`)
  cover everything this task needs; migrating is a separate, much larger bet.
- **No explicit child-written heartbeat protocol.** Idle-on-stream + controlled Bash
  timeout suffice.
- **No new `running` status.** With Q1 in place, timeouts no longer crash, so crash-
  forensics for a mid-step death is unnecessary (YAGNI).
- **No parallel execution / worktrees.** Unchanged project non-goal.

### Added after the Codex review (2026-07-31)

**D6 hard-enforcement — snapshot & overwrite.**
*Why:* under `--dangerously-skip-permissions` a prompt instruction ("do not edit
`index.json`") cannot *prevent* the child from writing it. Snapshotting before spawn and
overwriting from executor-owned state after makes a stray child edit irrelevant — T1-B is
closed *by construction*, not by trust. *Trade-off:* `execute.py` must hold the step-status
model in memory across the child run and stop re-reading the child's on-disk `index.json`
for control flow (a change from today's `execute.py:339`).

**Keep `MAX_RETRIES = 3`.**
*Why:* the draft's `max-retries=2` was an unexamined value; nothing in T1-A/T1-B motivates
changing retry policy. Holding at 3 keeps the diff minimal and behavior-preserving.

**D11 — split Runner / StateStore / Git (SRP, from the user).**
*Why:* today's `StepExecutor` changes for three unrelated reasons — how steps run, how state
is stored, how git is used (the "and…" smell). Splitting concentrates the `index.json`
single-writer invariant in one place (making D6 structural, not just a convention) and makes
each piece independently unit-testable — the "pure core" §6 depends on exactly this seam.
*Trade-off:* a larger refactor than a pure bug-fix — mitigated by the fact that Q2 already
rewrites this area, so the state logic is being touched regardless. *Rejected:* a separate
state-owning **process** — YAGNI, and it violates the "no multi-user/concurrency" non-goal.

## 6. Testing strategy

Driven by "Tests as Design" (global principle 7): the risky logic must be a **pure core**
with the clock and subprocess **injected**, so confidence comes from fast deterministic
tests, not real time. If a test needs `time.sleep(60)`, that's the smell telling us the
decision logic isn't yet separated from the timing mechanics.

- **L1 — pure / fast (the bulk of confidence).** No real subprocess, no real time.
  - verdict → status mapping (Q2 / all T1-B cases): `passed` true/false, `blocked`,
    `structured_output` absent / malformed / wrong-typed, `success` subtype without
    `structured_output`.
  - timeout decision: inject `now()` + synthetic timestamps → `keep` / `kill-idle` /
    `kill-wallclock`.
  - retry routing (**closes the currently-untested `_execute_single_step`**): outcome +
    attempt count → pending-retry / error-after-MAX / blocked, with correct exit codes.
  - NDJSON handling: feed a fake stream (list of lines) → `last_activity` updates per line;
    verdict parsed from the final `result` event; non-JSON lines skipped.
  - child env carries `BASH_MAX_TIMEOUT_MS` (assert on `Popen(env=…)`).
- **L2 — fake-child integration (OS semantics, sub-second timeouts).** A tiny script
  stands in for `claude`.
  - prints N lines then stalls → idle-timeout kills it (`T_idle`≈0.3s in tests).
  - steady slow output with gaps < `T_idle` → NOT killed (no false positive).
  - ignores `SIGTERM` → `SIGKILL` escalation works; the process group + a grandchild are
    reaped (no zombie).
  - stderr backpressure and partial / re-buffered NDJSON lines handled without deadlock.
- **L3 — one opt-in real-`claude` smoke test.** Round-trips the real `--json-schema`
  structured output; `@pytest.mark.slow` / integration, **excluded from the default CI
  gate** (needs network, auth, cost).

**Open sub-decision — red-green / test-first.** The harness advertises TDD (a `plugin.json`
keyword) but has **no written or enforced red-green rule** anywhere (confirmed by search
2026-07-31). If we want it, the enforcement points are the preamble (`_build_preamble`
work rules) and the `sg-decompose-task` step template ("write the failing test first; AC
includes it"). Caveat: an autonomous LLM step can't be *mechanically proven* to have
written the test first — enforcement is "instruct test-first + require the test in AC + CI
checks it exists and passes." **Decision pending** (also a candidate standalone harness
enhancement, separate from this task).

## 7. Review provenance

- Adversarial review by `codex exec` (codex-cli 0.136.0), read-only, 2026-07-31 → verdict
  **SOUND-WITH-FIXES**.
- **Adopted:** hard-enforced sole-writer (D6), mandatory capability/version preflight (D9),
  observability fields (D10), stderr draining + kill/reap-race handling (D8), schema
  requiredness + `success`-without-`structured_output` → fail (D7), keep `MAX_RETRIES=3`,
  backward-compat contingent on D6.
- **Not adopted as stated:** "stream-json + `--json-schema` unproven" — empirically verified
  on v2.1.216 by a smoke test; folded into D9 as a mandatory preflight rather than a blocker.
