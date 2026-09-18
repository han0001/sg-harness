---
name: sg-source-of-truth
description: Use when wrapping up an sg-* task, reconciling its decisions with permanent documentation, or correcting docs that drifted from the code.
---

This skill is the **knowledge-sync stage** of the sg-* workflow. It harvests the decisions made during a task — and the architecture/design changes they caused — from scattered, volatile outputs and syncs them into the **permanent docs** (`docs/*` and the applicable project instruction files such as `AGENTS.md` or `CLAUDE.md`).

Without this stage, knowledge gets trapped in the volatile `docs/sg/plan/` folder and the permanent docs drift from reality and rot.

---

## When it runs (trigger)

**Manual.** When you judge the session to be reasonably wrapped up, invoke it directly with the target plan as an argument.

> **Why manual (caching):** `execute.py` injects the selected project instruction file plus top-level `docs/*.md` as guardrails into every step prompt. Editing these docs **mid-task** breaks the prompt cache for subsequent steps and wastes tokens. Perform the sync **all at once, after the task is done**.

---

## Workflow

### A. Collect inputs

1. Read the target plan `docs/sg/plan/{yyyymmdd}_{task-name}/plan.md` to understand the **decisions and design intent**.
2. Read the changes attributable to this task's branch/commits to understand **what actually changed**. Exclude unrelated working-tree changes; if ownership is ambiguous, surface it instead of routing those changes into permanent docs.

Combining these captures both "what and why (plan)" and "how it actually turned out (diff)".

If the plan is missing, stale, or contradicted by incomplete/failed work, do not invent or finalize a decision. Include the evidence gap in the proposal and ask the user to resolve it.

### B. Routing (document map)

Determine the runtime used for task execution from its reports; if unavailable, ask the user. Select the **active project instruction file** using the same precedence as the executor:

- Claude runtime: `CLAUDE.md`, then `AGENTS.md`.
- Codex runtime: `AGENTS.md`, then `CLAUDE.md`.

Only the first existing file is injected as the executor's instruction guardrail. Read its **document map**. If no map exists, infer each document's role from the existing repository structure and include that routing assumption in the proposal. For example:

- Technical decision → `docs/ADR.md` (append-only, add ADR-NNN)
- Structure / data-flow / state-management change → `docs/ARCHITECTURE.md` (reconcile)
- Requirements change → `docs/PRD.md` (reconcile)
- UI/design change → `docs/UI_GUIDE.md` (reconcile)
- Critical rule / tech-stack change → the project instruction file that currently owns that rule

If both instruction files exist, do not assume both are active. Update a non-active file only when the active file explicitly delegates that rule to it. If ownership overlaps or conflicts, surface the conflict and ask the user which file should be authoritative before drafting that rule change.

### C. Draft the changes (proposal)

Build the proposed edits according to each document's **update style** (append-only / reconcile).

- **append-only**: preserve existing entries and add the new entry at the end.
- **reconcile**: read the existing content and revise only the parts that changed to match the current state. Do not overwrite wholesale (idempotency).

**For current behavior, verified code is the source of truth.** When a doc disagrees with completed, verified code, draft the edit based on the code and surface the drift. When work is incomplete or the plan describes an unmet requirement, report the mismatch instead of rewriting the requirement to match partial code.

### D. Apply after approval

> **CRITICAL: do not edit the docs automatically.** Modifying permanent docs or project instruction files without permission is dangerous.

**Present the proposed edits to the user as a diff** and wait until they read and approve. Apply only the approved changes.

---

## Output format

For each target document:

| Document | Update style | Change summary | Basis (plan/diff) |
|----------|-------------|----------------|-------------------|
| docs/ADR.md | append-only | Add ADR-00N: {decision} | plan §decisions / {file} |

Then present the concrete diff for each change and request approval.
