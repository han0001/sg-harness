---
name: sg-phase
description: Decomposition and execution phase of the sg-* workflow. Splits a plan.md plan into executable steps and drives an isolated claude session per step sequentially via the bundled execute.py. Use when moving a design into code, or when you need "task breakdown" / "split into steps and run".
---

This skill is the **decomposition and execution phase** of the sg-* workflow. It takes the plan (plan.md) produced by `/sg-plan` as input, splits it into executable steps, and uses `execute.py` to drive an isolated claude session per step sequentially.

(Exploration, discussion, and design are handled by `/sg-plan`. This skill focuses on moving the design result into code.)

---

## Workflow

### A. Input: read the plan

Read `plan/{yyyymmdd}_{task-name}/plan.md` to understand the design intent and decisions.

- If plan.md **exists**: use its decisions as the basis for decomposition.
- If plan.md is **missing**: warn that "running `/sg-plan` first is recommended", then, if the user wants to proceed, explore `/docs/` directly and decompose (backward compatibility).

Also read `/docs/` (ARCHITECTURE, ADR, etc.) and `CLAUDE.md` to confirm the architecture, tech stack, and CRITICAL rules.

### B. Step design

Draft a breakdown into multiple steps and request feedback.

Design principles:

1. **Minimize scope** — each step touches only one layer or module. If multiple modules must change at once, split the step.
2. **Self-containment** — each step file runs in an independent Claude session. External references like "as discussed in the earlier conversation" are forbidden. **Write the relevant decisions from plan.md directly into the step file** (execute.py does not inject plan.md).
3. **Force the prep work** — list the relevant doc paths and the paths of files created/modified in earlier steps.
4. **Signature-level instructions** — specify only the interface of functions/classes and leave the internal implementation to the agent's discretion. However, always spell out the core rules that must not drift from the design intent (idempotency, security, data integrity, etc.).
5. **AC must be runnable commands** — not abstract prose like "X should work", but actual runnable verification commands such as `npm run build && npm test`.
6. **Be specific in cautions** — instead of "be careful", write in the form "Do not do X. Reason: Y".
7. **Naming** — the step name is a kebab-case slug capturing the step's core module/task in one or two words (e.g. `project-setup`, `api-layer`).

### C. Create files

Once the user approves, create the following files in the user's project (cwd).

> **⚠ Naming contract (directly tied to Fail-Fast).** The two names point to different levels, so distinguish them precisely:
> - top index `dir` = **the folder name verbatim** = `{yyyymmdd}_{task-name}` (date **included**). execute.py matches the top index by this value; a mismatch desyncs the status and raises a `WARN`.
> - per-task index `phase` = **task name only** = `{task-name}` (date **excluded**). execute.py creates the `feat-{task-name}` branch from this value.

#### C-1. `phases/index.json` (overall status)

A top-level index that manages multiple tasks. If it already exists, append a new entry to the `phases` array.

```json
{
  "phases": [
    { "dir": "{yyyymmdd}_{task-name}", "status": "pending" }
  ]
}
```

- `dir`: the task directory name. **Match it exactly to the `{yyyymmdd}_{task-name}` folder name.**
- `status`: `"pending"` | `"completed"` | `"error"` | `"blocked"`. execute.py updates it automatically.
- Timestamps are recorded automatically by execute.py. Do not add them at creation time.

#### C-2. `phases/{yyyymmdd}_{task-name}/index.json` (task detail)

```json
{
  "project": "<project-name>",
  "phase": "{task-name}",
  "steps": [
    { "step": 0, "name": "project-setup", "status": "pending" },
    { "step": 1, "name": "core-types", "status": "pending" }
  ]
}
```

- `project`: the project name (see CLAUDE.md).
- `phase`: **task name only** (date excluded). It is the basis for the branch name `feat-{task-name}`.
- `steps[].step`: a 0-based sequence number.
- `steps[].name`: a kebab-case slug.
- `steps[].status`: all initialized to `"pending"`.

Fields recorded automatically on state transitions:

| Transition | Recorded fields | Owner |
|------------|----------------|-------|
| → `completed` | `completed_at`, `summary` | Claude session (summary) + execute.py (timestamp) |
| → `error` | `failed_at`, `error_message` | Claude session (message) + execute.py (timestamp) |
| → `blocked` | `blocked_at`, `blocked_reason` | Claude session (reason) + execute.py (timestamp) |

`summary` is a one-line summary of the step's output written on completion; execute.py accumulates it as context into subsequent step prompts. `created_at` and `started_at` are recorded automatically by execute.py.

#### C-3. `phases/{yyyymmdd}_{task-name}/step{N}.md` (one per step)

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
3. Based on the result, update the corresponding step in `phases/{yyyymmdd}_{task-name}/index.json`:
   - success → `"status": "completed"`, `"summary": "one-line summary of the output"`
   - still failing after 3 fix attempts → `"status": "error"`, `"error_message": "concrete error detail"`
   - user intervention needed → `"status": "blocked"`, `"blocked_reason": "concrete reason"`, then stop immediately

## Prohibited

- {What must not be done in this step. Use the form "Do not do X. Reason: Y"}
- Do not break existing tests
```

### D. Execute

Once the step files are created and approved, run the executor to execute the steps sequentially.

> **This skill (the current Claude session) runs it directly.** The bundled-script path variable `${CLAUDE_SKILL_DIR}` is only expanded in Claude's execution context (it is empty if the user types it into their own terminal). So tell the user what will run, and **after getting approval**, invoke it via Bash:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/execute.py" {yyyymmdd}_{task-name}        # run sequentially
python3 "${CLAUDE_SKILL_DIR}/scripts/execute.py" {yyyymmdd}_{task-name} --push  # run, then push
```

> **⚠ Safety.** For each step, execute.py spins up a child claude session with permission checks disabled (`--dangerously-skip-permissions`) and automatically branches/commits (and pushes if requested) to the current project's git repo. Always get user approval before running.

**Target = the current project (cwd).** execute.py treats the **git root of cwd** — not its own install location — as the project root, reading `phases/`, `CLAUDE.md`, and `docs/` and committing to that repo. So this session must be running at the user's project root, and the step files created in C must live there too.

What execute.py handles automatically:

- Creates/checks out the `feat-{task-name}` branch
- Injects guardrails — includes CLAUDE.md + docs/*.md in every step prompt
- Accumulates context — passes completed steps' summaries into the next step prompt
- Self-correction — retries up to 3 times on failure, feeding the previous error back into the prompt
- Two-stage commit — commits code changes (`feat`) and metadata (`chore`) separately
- Records timestamps automatically
- **Fail-Fast** — if the `dir` entry is missing from the top index, reports the desync via `WARN`

Error recovery:

- **On error**: set the step's `status` back to `"pending"` in index.json, delete `error_message`, then re-run.
- **On blocked**: resolve the `blocked_reason`, set `status` back to `"pending"`, delete `blocked_reason`, then re-run.

### Next step

When the work is done and you are wrapping up the session, use `/sg-source-of-truth` to sync this task's decisions and changes back into the permanent docs (docs/*, CLAUDE.md).
