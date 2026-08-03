# Step 4: contract-docs

## Files to read

- `/CLAUDE.md` — invariants (esp. #2, now extended to `index.json` by D6), working discipline.
- `/docs/sg/plan/20260731_executor-resilience/plan.md` — D6, and §4 ("Also update `sg-decompose-task` step template?" = **Yes, in scope**; "Version floor" = document in README). Decisions embedded below.
- `/skills/sg-execute-task/scripts/execute.py` — `_build_preamble` (work rules) and the constants added in step 2 (`T_IDLE_SEC`, `T_MAX_SEC`, `MAX_TURNS`, `BASH_MAX_TIMEOUT_MS`, `MIN_CLAUDE_VERSION`).
- `/skills/sg-decompose-task/SKILL.md` — the step template (phase C-3 "Verification procedure").
- `/README.md` — where the version floor + timeout knobs get documented.
- `/skills/sg-execute-task/scripts/test_execute.py` — `TestBuildPreamble`, esp. `test_includes_index_path` (will change).

## Task

Align the child-facing **contract and docs** with the new protocol (steps 1–3): the child no longer edits `index.json`; it makes changes, runs the AC, and its result is reported via the CLI's `--json-schema` structured verdict. `execute.py` is the sole writer of `index.json` (D6). This is the belt-and-suspenders half of D6 — the *prompt* stops asking the child to write state, matching the runner that already discards any such write.

### 1. `_build_preamble` (in `execute.py`)

- **Remove** work-rule 5's instruction to "Update the corresponding step status in `/docs/sg/tasks/.../index.json` (completed/error/blocked)." Replace it with the **verdict contract**: the child's job ends at making the changes and running the AC; it must **not** edit `index.json` and must **not** run git — the harness records status from the child's returned verdict.
- **Keep** the existing "Do NOT commit or run any git command" rule (Invariant 2).

### 2. `sg-decompose-task/SKILL.md` step template (phase C-3)

- In the generated `step{N}.md` **"Verification procedure"**, drop the "update the corresponding step in `index.json` (completed/error/blocked)" bullets. Steps should stop telling the child to edit `index.json`. Add one line stating that **`execute.py` is the sole writer of `index.json`** and the child reports via its verdict.
- Leave the rest of the template (Files to read, Task, AC, Prohibited) intact.

### 3. `README.md`

- Document the **CLI version floor** (`MIN_CLAUDE_VERSION`, currently `2.1.216`) required by the executor, and the **three-layer timeout knobs** (`T_IDLE_SEC`, `T_MAX_SEC`, `MAX_TURNS`, `BASH_MAX_TIMEOUT_MS`) — including that they are env-overridable.

### Core rules (must not drift)

1. **The preamble no longer asks the child to write `index.json`.** Reason (D6): the executor is the sole writer; a leftover instruction is misleading even if harmless.
2. **Keep the "do not commit/git" rule.** Reason: Invariant 2 is unchanged.
3. Docs must match the constants actually in the code (names/defaults). Reason: least astonishment.

## Acceptance Criteria

```bash
python3 -m pytest skills/sg-execute-task/scripts/test_execute.py -q
```

Update `TestBuildPreamble`: `test_includes_index_path` (which asserts the index path is *in* the preamble) must be rewritten to assert the child is told **not** to edit `index.json` and that the verdict is the reporting channel; keep `test_instructs_child_not_to_commit` green. Then confirm the doc edits landed:

```bash
# the child is no longer instructed to update index.json in generated steps
! grep -riq "update the corresponding step" skills/sg-decompose-task/SKILL.md
# the version floor is documented
grep -q "2.1.216" README.md
```

## Verification procedure

1. Run the AC commands above.
2. Architecture checklist:
   - Does the preamble still forbid git/commits (Invariant 2) while no longer asking the child to write `index.json`?
   - Does the `sg-decompose-task` template stop instructing `index.json` edits?
   - Do the README knob names/defaults match the constants in `execute.py`?
3. Update this step in `docs/sg/tasks/20260731_executor-resilience/index.json` (success → `completed` + `summary`; failure → `error` + `error_message`; needs intervention → `blocked` + `blocked_reason`, then stop).

## Prohibited

- Do not remove the "Do NOT commit / no git" rule from the preamble. Reason: Invariant 2 still holds — only the orchestrator touches git.
- Do not change runner/timeout/verdict logic here. Reason: that is steps 1–3; this step is the contract + docs alignment only.
- Do not document knob names/defaults that differ from the code. Reason: drifted docs are worse than none (least astonishment).
- Do not break existing tests.
