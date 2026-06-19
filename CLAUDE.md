# sg-harness — development guide (project memory)

> This file is the baseline Claude auto-loads every session **when developing sg-harness itself**.
> It is NOT the same as the CLAUDE.md the harness reads as guardrails inside a *target* project — that one lives in the user's project repo. Don't confuse the two.

## Purpose (one line)

A Claude Code workflow harness that splits a large task into isolated steps and runs them sequentially with self-correction (design → decompose → execute → knowledge-sync).

## Layout (flat — `skills/` and `hooks/` live at the repo root)

- `skills/sg-plan/` — **design** phase: runs grill-me, writes `plan/{yyyymmdd}_{task}/plan.md`. Does not implement.
- `skills/sg-phase/` — **decompose + execute** phase: splits `plan.md` into steps and runs one isolated claude session per step via `scripts/execute.py` (the orchestrator). Tests live in `scripts/test_execute.py`.
- `skills/sg-source-of-truth/` — **knowledge-sync** phase: harvests decisions from `plan.md` + the git diff into the permanent docs (e.g. this file).
- `hooks/hooks.json` — PreToolUse Bash guard that blocks `rm -rf`, `git push --force`, `git reset --hard`, `DROP TABLE`.

## Non-Goals (things we deliberately do NOT do — check new ideas against this list FIRST)

- **No parallel step execution / no per-step git worktree.** Steps run sequentially, so there is no concurrency conflict to isolate. Do not introduce worktrees unless parallel execution is formally adopted.
- **No deploy.** The most the harness does is `git push` (opt-in `--push`, to a `feat-*` branch).
- **No multi-user / concurrency handling.**
- **No forcing the target project's language.** The harness's own source is English-only.

## Invariants (contracts that must never break — every change must pass these)

1. **Target = the git root of cwd**, never the script's own install location.
2. **Only the orchestrator (`execute.py`) touches git.** Child Claude sessions must NOT be instructed to commit or push — the preamble explicitly forbids it.
3. **Step files are self-contained** — no references to external/earlier conversation.
4. **Naming:** top-index `dir` = `{yyyymmdd}_{task}` (date included) ≠ per-task `phase` = `{task}` (date excluded).
5. **Refuse to run if not a git repo.**

## Working discipline (how we work in this repo)

- **Minimal change.** Always propose the smallest change that achieves the intent first (YAGNI/KISS). Resist speculative generality.
- **A review finding is a candidate, not a task.** To become work it must pass two gates:
  1. **Is it true?** Verify against `file:line` / actual control flow. No guessing, no narrative.
  2. **Is it important?** Material to the Purpose/Invariants above. **True ≠ important.**

## Dev commands

```bash
.venv/bin/python -m pytest skills/sg-phase/scripts/test_execute.py -q
```
