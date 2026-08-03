---
name: sg-decompose-task
description: Decomposition stage of the sg-* workflow. Splits a plan.md plan into self-contained, executable steps and writes the task/step files that sg-execute-task later runs. Use when moving a design into an execution plan, or when you need "task breakdown" / "split into steps".
---

This skill is the **decomposition stage** of the sg-* workflow. It takes the plan (`plan.md`) produced by `/sg-plan` as input, splits it into executable **steps**, and writes the task/step files under `docs/sg/tasks/`. It does **not** run anything — execution is the next stage (`/sg-execute-task`).

> **Vocabulary** (see `CLAUDE.md` › Vocabulary for the canonical definitions): a **stage** is one skill; a **phase** is an ordered step inside this skill (A, B, C below); a **task** is one goal = one `plan.md`; a **step** is one decomposed, isolated unit of work that `sg-execute-task` runs.

(Exploration, discussion, and design are handled by `/sg-plan`. Running the steps is handled by `/sg-execute-task`.)

---

## Workflow

### Phase A — Input: read the plan

Read `docs/sg/plan/{yyyymmdd}_{task-name}/plan.md` to understand the design intent and decisions.

- If plan.md **exists**: use its decisions as the basis for decomposition.
- If plan.md is **missing**: warn that "running `/sg-plan` first is recommended", then, if the user wants to proceed, explore `/docs/` directly and decompose (backward compatibility).

Also read `/docs/` (ARCHITECTURE, ADR, etc.) and `CLAUDE.md` to confirm the architecture, tech stack, and CRITICAL rules.

### Phase B — Step design

Draft a breakdown into multiple steps and request feedback.

**Climb the structure ladder before drafting** (adapted from ponytail, MIT). Decomposition fixes the structural upper bound on the resulting code: every speculative module/class the plan names, each isolated execution session then dutifully builds (work rule 2 makes it build exactly what is specified — no more, no less). So at each rung, stop if it holds — the plan should introduce only the structure that must exist:

1. **Need to exist at all?** A speculative step/module/seam → drop it, note it in one line. (YAGNI)
2. **Already in the codebase or an earlier step?** Reuse it; do not re-spec it.
3. **Stdlib / native feature / installed dependency covers it?** Specify *that*, not a new wrapper or abstraction.
4. **Could a function replace a class/hierarchy?** Prefer the function; introduce the class only when real state or polymorphism demands it.

Lazy about *structure*, never about the *contract*: still specify each step's interface and its non-negotiable rules (validation, security, idempotency, data integrity — see principle 4) in full. Under-specifying a contract makes independent sessions diverge, which costs more than the structure you saved; and a genuine second module still earns its own step (principle 1). The ladder removes *invented* seams, not real ones.

Design principles:

1. **Minimize scope** — each step touches only one layer or module. If multiple modules must change at once, split the step.
2. **Self-containment** — each step file runs in an independent Claude session. External references like "as discussed in the earlier conversation" are forbidden. **Write the relevant decisions from plan.md directly into the step file** (execute.py does not inject plan.md).
3. **Force the prep work** — list the relevant doc paths and the paths of files created/modified in earlier steps.
4. **Signature-level instructions** — specify only the interface of functions/classes and leave the internal implementation to the agent's discretion. However, always spell out the core rules that must not drift from the design intent (idempotency, security, data integrity, etc.).
5. **AC must be runnable commands** — not abstract prose like "X should work", but actual runnable verification commands such as `npm run build && npm test`.
6. **Be specific in cautions** — instead of "be careful", write in the form "Do not do X. Reason: Y".
7. **Naming** — the step name is a kebab-case slug capturing the step's core module/task in one or two words (e.g. `project-setup`, `api-layer`).

### Phase C — Create files

Once the user approves, create the following files in the user's project (cwd).

> **⚠ Naming contract (directly tied to Fail-Fast).** The two names point to different levels, so distinguish them precisely:
> - top index `dir` = **the folder name verbatim** = `{yyyymmdd}_{task-name}` (date **included**). execute.py matches the top index by this value; a mismatch desyncs the status and raises a `WARN`.
> - per-task index `task` = **task name only** = `{task-name}` (date **excluded**). execute.py creates the `feat-{task-name}` branch from this value.

#### C-1. `docs/sg/tasks/index.json` (overall status)

A top-level index that manages multiple tasks. If it already exists, append a new entry to the `tasks` array.

```json
{
  "tasks": [
    { "dir": "{yyyymmdd}_{task-name}", "status": "pending" }
  ]
}
```

- `dir`: the task directory name. **Match it exactly to the `{yyyymmdd}_{task-name}` folder name.**
- `status`: `"pending"` | `"completed"` | `"error"` | `"blocked"`. execute.py updates it automatically.
- Timestamps are recorded automatically by execute.py. Do not add them at creation time.

#### C-2. `docs/sg/tasks/{yyyymmdd}_{task-name}/index.json` (task detail)

```json
{
  "project": "<project-name>",
  "task": "{task-name}",
  "steps": [
    { "step": 0, "name": "project-setup", "status": "pending" },
    { "step": 1, "name": "core-types", "status": "pending" }
  ]
}
```

- `project`: the project name (see CLAUDE.md).
- `task`: **task name only** (date excluded). It is the basis for the branch name `feat-{task-name}`.
- `steps[].step`: a 0-based sequence number.
- `steps[].name`: a kebab-case slug.
- `steps[].status`: all initialized to `"pending"`.

Fields recorded automatically on state transitions:

| Transition | Recorded fields | Owner |
|------------|----------------|-------|
| → `completed` | `completed_at`, `summary` | execute.py (timestamp + `summary` from the step session's verdict) |
| → `error` | `failed_at`, `error_message` | execute.py (timestamp + `error_message` from the verdict) |
| → `blocked` | `blocked_at`, `blocked_reason` | execute.py (timestamp + `blocked_reason` from the verdict) |

`summary` is a one-line summary of the step's output written on completion; execute.py accumulates it as context into subsequent step prompts. `created_at` and `started_at` are recorded automatically by execute.py.

#### C-3. `docs/sg/tasks/{yyyymmdd}_{task-name}/step{N}.md` (one per step)

```markdown
# Step {N}: {name}

## Files to read

First read the files below to understand the architecture and design intent:

- `/docs/ARCHITECTURE.md`
- `/docs/ADR.md`
- {paths of files created/modified in earlier steps}

## Task

{Concrete implementation instructions. File paths, class/function signatures, logic description.
Write the relevant decisions from plan.md directly here (self-containment).
Provide code snippets at signature level only and leave the implementation to the agent.
However, clearly nail down the core rules that must not drift from the design intent.}

## Acceptance Criteria

```bash
npm run build   # no compile errors
npm test        # tests pass
```

## Verification procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Does it follow the ARCHITECTURE.md directory structure?
   - Does it stay within the ADR tech stack?
   - Does it violate any CLAUDE.md CRITICAL rule?
3. Report the result through the verdict you return, not by editing files. execute.py is the
   **sole writer** of `index.json` — it records this step's status from your verdict.

## Prohibited

- {What must not be done in this step. Use the form "Do not do X. Reason: Y"}
- Do not break existing tests
```

### Next stage

Once the step files are created and approved, move on to `/sg-execute-task` to run them.
