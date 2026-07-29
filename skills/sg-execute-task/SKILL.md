---
name: sg-execute-task
description: Execution stage of the sg-* workflow. Drives an isolated claude session per step sequentially via the bundled execute.py, over the task/step files produced by sg-decompose-task. Use when the steps are decomposed and ready to run, or when you need to "run the steps" / "execute the plan".
---

This skill is the **execution stage** of the sg-* workflow. It takes the task/step files produced by `/sg-decompose-task` (under `tasks/{yyyymmdd}_{task-name}/`) and uses the bundled `execute.py` to drive an isolated claude session per **step**, sequentially and with self-correction.

> **Vocabulary** (see `CLAUDE.md` › Vocabulary for the canonical definitions): a **stage** is one skill; a **task** is one goal = one `plan.md`; a **step** is one decomposed, isolated unit of work. This stage runs the steps of a single task.

(Decomposition — splitting the plan into steps and writing the files — is handled by `/sg-decompose-task`. This skill only runs them.)

---

## Workflow

### Input: the decomposed task

`/sg-decompose-task` must have already created, in the user's project (cwd):

- `tasks/index.json` — the top-level status index (with this task's `dir` entry).
- `tasks/{yyyymmdd}_{task-name}/index.json` — the task detail (`task` name + `steps[]`).
- `tasks/{yyyymmdd}_{task-name}/step{N}.md` — one self-contained file per step.

If these are missing, run `/sg-decompose-task` first.

### Execute

Run the executor. **There is one reporting mode and you do not ask the user to choose it.** This session drives the run **automatically from the first pending step to the last** — it never pauses mid-run to ask "run the next step?". After each step finishes it emits a **one-line progress report** into this chat (`✓ Step N/M … — {summary}` + `Next ▶ …`), so the user watches progress live without lifting a finger. The run only stops on **error** or **blocked** (see Error recovery below).

> **This skill (the current Claude session) runs it directly.** The bundled-script path variable `${CLAUDE_SKILL_DIR}` is only expanded in Claude's execution context (it is empty if the user types it into their own terminal). So tell the user what will run, get the **one** safety approval (it disables permission checks and auto-commits — see Safety below), and then invoke it via Bash.

**This session drives the loop via `--once` — one step per call, not one call for the whole task.** (A single whole-task call cannot stream: Bash returns stdout only when the command exits, so every `✓ Step` line would arrive bunched up at the end instead of one-per-step.)

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/execute.py" {yyyymmdd}_{task-name} --once          # run the next pending step only
python3 "${CLAUDE_SKILL_DIR}/scripts/execute.py" {yyyymmdd}_{task-name} --once --push    # ... on the FINAL step only, to push after it
```

1. Run `execute.py {task-dir} --once`. It runs exactly one pending step and exits.
2. Relay the `✓ Step N/M … — {summary}` and `Next ▶ …` lines it printed as a one-line progress report. **Do NOT read `step{N}-output.json`** (the child's full stdout — large and unnecessary); the printed summary is enough.
3. If a `Next ▶` step remains and no error/blocked occurred, **repeat from 1 immediately without asking the user**. Keep going until it prints `All steps completed!`, then stop. (Add `--push` only on the final step's call.)

> **⚠ Safety.** For each step, execute.py spins up a child claude session with permission checks disabled (`--dangerously-skip-permissions`) and automatically branches/commits (and pushes if requested) to the current project's git repo. Always get user approval before running.

**Target = the current project (cwd).** execute.py treats the **git root of cwd** — not its own install location — as the project root, reading `tasks/`, `CLAUDE.md`, and `docs/` and committing to that repo. So this session must be running at the user's project root, and the step files created by `/sg-decompose-task` must live there too.

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

### Next stage

When the work is done and you are wrapping up the session, use `/sg-source-of-truth` to sync this task's decisions and changes back into the permanent docs (docs/*, CLAUDE.md).
