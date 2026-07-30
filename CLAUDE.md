# sg-harness — development guide (project memory)

> This file is the baseline Claude auto-loads every session **when developing sg-harness itself**.
> It is NOT the same as the CLAUDE.md the harness reads as guardrails inside a *target* project — that one lives in the user's project repo. Don't confuse the two.

## Purpose (one line)

A Claude Code workflow harness that splits a large task into isolated steps and runs them sequentially with self-correction (design → decompose → execute → knowledge-sync).

## Vocabulary (canonical — use these exact words everywhere, in code and docs)

The workflow nests along **two axes**. Learn them as two pairs:

| Term | Meaning | Example |
|------|---------|---------|
| **workflow** | the whole sg-* chain | sg-plan → sg-decompose-task → sg-execute-task → sg-source-of-truth |
| **stage** | one skill in the chain | `sg-plan` · `sg-decompose-task` · `sg-execute-task` · `sg-source-of-truth` |
| **phase** | an ordered step *inside* one skill | sg-decompose-task's A. read plan → B. step design → C. create files |
| **task** | one goal = one `plan.md` = one `docs/sg/tasks/{yyyymmdd}_{name}/` dir | "auction map viewer MVP" |
| **step** | one decomposed, isolated unit of work under a task (tracked in `index.json`) | step0 setup · step1 map … |

- **Procedure axis:** `stage → phase` (which skill / which part of that skill).
- **Work axis:** `task → step` (which goal / which slice of that goal).
- **Disambiguator:** if the harness tracks it with `status` + timestamps, it's a **step**; if it's fixed prose in a `SKILL.md`, it's a **phase**.

> Historical note: `phase` used to mean "task" in the code (the `phases/` dir, `phase_dir`, the `feat-{phase}` branch). That usage is **retired** — the on-disk dir is now `docs/sg/tasks/`, the index field is `task`, and `phase` now means only "an ordered step inside a skill".

## Layout (flat — `skills/` and `hooks/` live at the repo root)

- `skills/sg-plan/` — **design** stage: runs grill-me, writes `docs/sg/plan/{yyyymmdd}_{task}/plan.md`. Does not implement.
- `skills/sg-decompose-task/` — **decompose** stage: splits `plan.md` into steps and writes the `docs/sg/tasks/{yyyymmdd}_{task}/` files (index.json + step files). Does not run anything.
- `skills/sg-execute-task/` — **execute** stage: runs one isolated claude session per step via `scripts/execute.py` (the orchestrator). Tests live in `scripts/test_execute.py`.
- `skills/sg-source-of-truth/` — **knowledge-sync** stage: harvests decisions from `plan.md` + the git diff into the permanent docs (e.g. this file).
- `hooks/hooks.json` — PreToolUse Bash guard that blocks `rm -rf`, `git push --force`, `git reset --hard`, `DROP TABLE`.
- `.claude-plugin/plugin.json` + `marketplace.json` — plugin manifest and single-plugin marketplace (`source: "."`); `skills/` and `hooks/` are auto-discovered from the plugin root. Install: `/plugin marketplace add han0001/sg-harness`.

> **Working state lives under `docs/sg/`** in the *target* project — `docs/sg/plan/` (sg-plan) and `docs/sg/tasks/` (sg-decompose-task + execute.py). Nested under `docs/` to keep the project root clean, yet kept **separate from top-level `docs/*.md`**, which the harness injects as guardrails. This is safe because the guardrail injection globs `docs/*.md` **non-recursively** (`execute.py._load_guardrails`), so nested working files are never pulled into step prompts.

## Non-Goals (things we deliberately do NOT do — check new ideas against this list FIRST)

- **No parallel step execution / no per-step git worktree.** Steps run sequentially, so there is no concurrency conflict to isolate. Do not introduce worktrees unless parallel execution is formally adopted.
- **No deploy.** The most the harness does is `git push` (opt-in `--push`, to a `feat-*` branch).
- **No multi-user / concurrency handling.**
- **No forcing the target project's language.** The harness's own source is English-only.

## Invariants (contracts that must never break — every change must pass these)

1. **Target = the git root of cwd**, never the script's own install location.
2. **Only the orchestrator (`execute.py`) touches git.** Child Claude sessions must NOT be instructed to commit or push — the preamble explicitly forbids it.
3. **Step files are self-contained** — no references to external/earlier conversation.
4. **Naming:** top-index `dir` = `{yyyymmdd}_{task}` (date included) ≠ per-task index `task` field = `{task}` (date excluded, basis for the `feat-{task}` branch).
5. **Refuse to run if not a git repo.**

## Working discipline (how we work in this repo)

- **Minimal change.** Always propose the smallest change that achieves the intent first (YAGNI/KISS). Resist speculative generality.
- **A review finding is a candidate, not a task.** To become work it must pass two gates:
  1. **Is it true?** Verify against `file:line` / actual control flow. No guessing, no narrative.
  2. **Is it important?** Material to the Purpose/Invariants above. **True ≠ important.**

## Dev commands

```bash
.venv/bin/python -m pytest skills/sg-execute-task/scripts/test_execute.py -q
```
