# sg-harness — provider-neutral development guide

> Claude loads this file directly; Codex enters through the short `AGENTS.md` file at the repository root.
> This is project memory for developing sg-harness itself, not an instruction file from a target project being operated on by the harness.

## Purpose (one line)

A Claude Code and Codex workflow harness that splits a large task into isolated steps and runs them sequentially with a selected child-agent runtime (design → decompose → execute → knowledge-sync).

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

- `skills/sg-plan/` — **design** stage: runs a self-contained interview, writes `docs/sg/plan/{yyyymmdd}_{task}/plan.md`. Does not implement.
- `skills/sg-decompose-task/` — **decompose** stage: splits `plan.md` into steps and writes the `docs/sg/tasks/{yyyymmdd}_{task}/` files (index.json + step files). Does not run anything.
- `skills/sg-execute-task/` — **execute** stage: runs one isolated Claude or Codex child per step via `scripts/execute.py`. Runtime adapters live under `scripts/runtimes/`; the shared verdict schema lives under `scripts/schemas/`.
- `skills/sg-source-of-truth/` — **knowledge-sync** stage: harvests decisions from `plan.md` + the git diff into the permanent docs (e.g. this file).
- `hooks/hooks.json` — PreToolUse Bash guard that blocks `rm -rf`, `git push --force`, `git reset --hard`, `DROP TABLE`; host payload tests live in `hooks/test_hooks.py`.
- `.claude-plugin/plugin.json` and `.codex-plugin/plugin.json` — provider manifests with one shared version and the same `skills/` tree. `.claude-plugin/marketplace.json` remains the Claude marketplace descriptor.

> **Working state lives under `docs/sg/`** in the *target* project — `docs/sg/plan/` (sg-plan) and `docs/sg/tasks/` (sg-decompose-task + execute.py). Nested under `docs/` to keep the project root clean, yet kept **separate from top-level `docs/*.md`**, which the harness injects as guardrails. This is safe because the guardrail injection globs `docs/*.md` **non-recursively** (`execute.py._load_guardrails`), so nested working files are never pulled into step prompts.

## Non-Goals (things we deliberately do NOT do — check new ideas against this list FIRST)

- **No parallel step execution / no per-step git worktree.** Steps run sequentially, so there is no concurrency conflict to isolate. Do not introduce worktrees unless parallel execution is formally adopted.
- **No deploy.** The most the harness does is `git push` (opt-in `--push`, to a `feat-*` branch).
- **No multi-user / concurrency handling.**
- **No forcing the target project's language.** The harness's own source is English-only.

## Invariants (contracts that must never break — every change must pass these)

1. **Target = the git root of cwd**, never the script's own install location.
2. **Only the orchestrator (`execute.py`) touches git.** Child runtimes must NOT be instructed to commit or push — the preamble explicitly forbids it.
3. **Step files are self-contained** — no references to external/earlier conversation.
4. **Naming:** top-index `dir` = `{yyyymmdd}_{task}` (date included) ≠ per-task index `task` field = `{task}` (date excluded, basis for the `feat-{task}` branch).
5. **Refuse to run if not a git repo.**
6. **Runtime selection is explicit.** The CLI accepts only `claude` or `codex`; omitted `--runtime` remains backward-compatible and selects `claude`. Never auto-fallback to another runtime.
7. **Preflight before mutation.** The selected runtime validates its executable and capabilities before blocker checks, git operations, or state writes.
8. **Instruction precedence is runtime-specific.** Claude uses `CLAUDE.md`, then `AGENTS.md`; Codex uses `AGENTS.md`, then `CLAUDE.md`. Inject only the first existing file.
9. **One verdict contract.** Both runtimes must return the schema in `skills/sg-execute-task/scripts/schemas/verdict.schema.json` and normalize it into `AttemptResult`.

## Working discipline (how we work in this repo)

- **Minimal change.** Always propose the smallest change that achieves the intent first (YAGNI/KISS). Resist speculative generality.
- **A review finding is a candidate, not a task.** To become work it must pass two gates:
  1. **Is it true?** Verify against `file:line` / actual control flow. No guessing, no narrative.
  2. **Is it important?** Material to the Purpose/Invariants above. **True ≠ important.**

## Dev commands

```bash
python3 -m pytest skills/sg-execute-task/scripts/test_execute.py hooks/test_hooks.py -q
python3 skills/sg-execute-task/scripts/execute.py --help
```
