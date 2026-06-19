---
name: sg-source-of-truth
description: Knowledge-sync phase of the sg-* workflow. Harvests the decisions and architecture changes made during a task from plan.md and the git diff, and syncs them into the permanent docs (docs/*, CLAUDE.md). Use when wrapping up a task/session, or when the docs have drifted from the code.
---

This skill is the **knowledge-sync phase** of the sg-* workflow. It harvests the decisions made during a task — and the architecture/design changes they caused — from scattered, volatile outputs and syncs them into the **permanent docs** (`docs/*`, `CLAUDE.md`).

Without this phase, knowledge gets trapped in the old `plan/` folder and the permanent docs drift from reality and rot.

---

## When it runs (trigger)

**Manual.** When you judge the session to be reasonably wrapped up, invoke it directly with the target plan as an argument.

> **Why manual (caching):** `execute.py` injects the full `CLAUDE.md + docs/*` as guardrails into every step prompt. Editing these docs **mid-task** breaks the prompt cache for every subsequent step and wastes significant tokens. So the sync is performed **all at once, after the task is done**.

---

## Workflow

### A. Collect inputs

1. Read the target plan `plan/{yyyymmdd}_{task-name}/plan.md` to understand the **decisions and design intent**.
2. Read the `git diff` (or the changes on the task branch) to understand **what actually changed**.

Combining these captures both "what and why (plan)" and "how it actually turned out (diff)".

### B. Routing (document map)

Read the **"document map"** table in `CLAUDE.md` to understand each document's nature and update style. Classify this task's changes to the right place accordingly. For example:

- Technical decision → `docs/ADR.md` (append-only, add ADR-NNN)
- Structure / data-flow / state-management change → `docs/ARCHITECTURE.md` (reconcile)
- Requirements change → `docs/PRD.md` (reconcile)
- UI/design change → `docs/UI_GUIDE.md` (reconcile)
- CRITICAL rule / tech-stack change → `CLAUDE.md` itself

### C. Draft the changes (proposal)

Build the proposed edits according to each document's **update style** (append-only / reconcile).

- **append-only**: preserve existing entries and add the new entry at the end.
- **reconcile**: read the existing content and revise only the parts that changed to match the current state. Do not overwrite wholesale (idempotency).

**On conflict: code is the source of truth.** When a doc disagrees with the code, draft the edit based on the code. But surface the fact that they had drifted.

### D. Apply after approval

> **CRITICAL: do not edit the docs automatically.** Modifying the permanent docs — especially `CLAUDE.md` — without permission is dangerous.

**Present the proposed edits to the user as a diff** and wait until they read and approve. Apply only the approved changes.

---

## Output format

For each target document:

| Document | Update style | Change summary | Basis (plan/diff) |
|----------|-------------|----------------|-------------------|
| docs/ADR.md | append-only | Add ADR-00N: {decision} | plan §decisions / {file} |

Then present the concrete diff for each change and request approval.
