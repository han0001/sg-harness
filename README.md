# sg-harness

> A Claude Code workflow harness that turns a large task into isolated, self-correcting steps — **design → decompose → execute → knowledge-sync**.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Claude Code Plugin](https://img.shields.io/badge/Claude%20Code-Plugin-8A63D2.svg)](https://github.com/han0001/sg-harness)

---

## Why this exists

When you hand a large task to a single Claude session, quality decays as the context fills up: earlier decisions get blurry, unrelated files pile into the window, and one bad turn can derail the rest of the run.

sg-harness attacks that from a different angle. It **splits the work into small, self-contained steps and runs each one in its own fresh Claude session**, sequentially, with automatic retry-on-failure. Each step starts clean, sees only what it needs (the guardrail docs + a short summary of what previous steps produced), and commits its result before the next one begins.

The result is a repeatable pipeline instead of one long, drifting conversation.

---

## The workflow at a glance

sg-harness is a **plugin that ships four skills**, one of which bundles an orchestrator script. You drive it stage by stage:

```mermaid
flowchart LR
    A["/sg-plan<br/>design · grill-me"] -->|plan.md| B["/sg-decompose-task<br/>decompose"]
    B -->|"step0.md … stepN.md"| C{{"/sg-execute-task<br/>execute.py orchestrator"}}
    C -->|"isolated claude session<br/>per step (sequential)"| C
    C -->|"commits to feat-task branch"| D["/sg-source-of-truth<br/>knowledge sync"]
    D -->|"update docs/*, CLAUDE.md"| E(("done"))
```

| Stage | Skill | Input | Output |
|-------|-------|-------|--------|
| **1. Design** | `/sg-plan` | your intent + `docs/`, `CLAUDE.md` | `docs/sg/plan/{yyyymmdd}_{task}/plan.md` |
| **2. Decompose** | `/sg-decompose-task` | `plan.md` | `docs/sg/tasks/{yyyymmdd}_{task}/step*.md` |
| **3. Execute** | `/sg-execute-task` | the `docs/sg/tasks/` step files | isolated `claude` session per step via `execute.py` → commits |
| **4. Knowledge sync** | `/sg-source-of-truth` | `plan.md` + the git diff | reconciled `docs/*`, `CLAUDE.md` |

Each stage is independent — you can stop after design, review, and only then move on.

---

## How execution works

The interesting part lives in `skills/sg-execute-task/scripts/execute.py`, the orchestrator that turns a folder of `step*.md` files into commits. For every pending step it:

- **Spins up an isolated `claude -p` session** — one fresh session per step, so no cross-step context bleed.
- **Injects the guardrails** — the target project's `CLAUDE.md` and `docs/*.md` are prepended to every step prompt, so each session obeys the same rules.
- **Accumulates just enough context** — a one-line `summary` written on each step's completion is passed forward into the next step's prompt (not the whole transcript).
- **Self-corrects** — on failure it retries up to **3 times**, feeding the previous error back into the prompt.
- **Commits in two stages** — code changes as a `feat` commit, metadata/status as a separate `chore` commit, onto a dedicated `feat-{task}` branch.
- **Reports progress live** — driven one step at a time (`--once`), it streams a `✓ Step N/M — {summary}` line into the chat after each step, and only stops on `error` or `blocked`.

**Target = the git root of your current directory**, never the plugin's install location — so it always operates on the project you're standing in.

### CLI version floor

`execute.py` drives each child session with `--output-format stream-json` and `--json-schema`, and reads the step's pass/fail **verdict** out of the resulting structured output. An older CLI ignores an unknown `--json-schema` silently, so the failure mode would be a run that looks healthy while every step fails for a reason no log explains.

To make that impossible, a **mandatory preflight** runs before any git or state mutation and aborts the run unless the local `claude` is at least:

```text
claude >= 2.1.216
```

The preflight also checks that `claude --help` advertises `--json-schema`, `--output-format`, and `stream-json`. It is deliberately not opt-out — a `--skip-preflight` flag would just make the silent-misbehaviour mode reachable again.

### Timeout knobs

A hung step is bounded in three layers rather than by one blunt wall-clock — an idle watchdog detects a hang fast, the wall-clock is only a backstop, and `--max-turns` catches a session that keeps emitting but loops forever. All three route into the same retry-then-`error` path.

| Layer | Constant | Env override | Default | What it bounds |
|-------|----------|--------------|---------|----------------|
| ① idle | `T_IDLE_SEC` | `SG_T_IDLE_SEC` | `720` (12 min) | gap between two events on the child's stream — the primary hang detector |
| ② wall-clock | `T_MAX_SEC` | `SG_T_MAX_SEC` | `5400` (90 min) | total time for one attempt — the backstop ceiling |
| ③ turns | `MAX_TURNS` | `SG_MAX_TURNS` | `50` | the child's tool-loop count, passed as `--max-turns` |
| (input to ①) | `BASH_MAX_TIMEOUT_MS` | `SG_BASH_MAX_TIMEOUT_MS` | `480000` (8 min) | the child's own Bash timeout, set on its environment |

All four are integer environment overrides read at import time; a non-integer value logs a `WARN` and falls back to the default.

`BASH_MAX_TIMEOUT_MS` is what makes `T_IDLE_SEC` safe to size: by capping the child's longest single Bash call ourselves, "maximum legitimate silence" becomes a value we *control* rather than one we guess, and the idle timeout sits above it with margin. If you raise it, raise `T_IDLE_SEC` to match.

> Note the `SG_` prefix on the overrides. `BASH_MAX_TIMEOUT_MS` (no prefix) is the variable `execute.py` **sets on the child**; `SG_BASH_MAX_TIMEOUT_MS` is how **you** configure it.

---

## Install

sg-harness is distributed as a Claude Code plugin from a single-plugin marketplace.

```text
# 1. Add this repo as a plugin marketplace
/plugin marketplace add han0001/sg-harness

# 2. Install the plugin from it
/plugin install sg-harness@han0001-plugins
```

The `skills/` and `hooks/` directories are auto-discovered from the plugin root — nothing else to wire up. (You can also browse and install interactively via the `/plugin` menu.)

---

## Quick start

Run the stages in order from inside a **git repository** (the harness refuses to run otherwise):

```text
/sg-plan             # interview + write docs/sg/plan/{date}_{task}/plan.md — no code yet
/sg-decompose-task   # split plan.md into steps under docs/sg/tasks/{date}_{task}/
/sg-execute-task     # after your approval, execute those steps one by one
/sg-source-of-truth  # fold the decisions back into docs/ and CLAUDE.md
```

A typical run:

1. **`/sg-plan`** grills you one decision at a time and writes a `plan.md`. It never implements.
2. **`/sg-decompose-task`** drafts a step breakdown for your review and creates the `step*.md` files. It does not run anything.
3. **`/sg-execute-task`** — after a **single safety approval** — runs `execute.py` step by step, committing as it goes.
4. **`/sg-source-of-truth`** harvests what actually changed (plan + git diff) and proposes doc edits for you to approve.

---

## Repository layout

```text
sg-harness/
├── skills/
│   ├── sg-plan/SKILL.md               # design stage (grill-me → plan.md)
│   ├── sg-decompose-task/SKILL.md     # decompose stage (plan.md → docs/sg/tasks/step*.md)
│   ├── sg-execute-task/
│   │   ├── SKILL.md                   # execute stage
│   │   └── scripts/
│   │       ├── execute.py             # the orchestrator (isolated session per step)
│   │       └── test_execute.py        # its tests
│   └── sg-source-of-truth/SKILL.md    # knowledge-sync stage
├── hooks/hooks.json                   # PreToolUse Bash safety guard
├── .claude-plugin/
│   ├── plugin.json                    # plugin manifest
│   └── marketplace.json               # single-plugin marketplace
├── CLAUDE.md                          # project memory (for developing sg-harness itself)
├── LICENSE
└── README.md
```

---

## Safety

Running code-writing sessions automatically is powerful, so the harness layers on guardrails:

- **A hook blocks dangerous commands.** `hooks/hooks.json` is a `PreToolUse` guard that refuses any Bash call matching `rm -rf`, `git push --force`, `git reset --hard`, or `DROP TABLE`.
- **Skip-permissions is gated.** `execute.py` runs each child session with `--dangerously-skip-permissions` and auto-commits — so it always asks for **one explicit approval** before the run starts, telling you exactly what it will do.
- **Only the orchestrator touches git.** Child sessions are forbidden from committing or pushing; branching/committing is done solely by `execute.py`. Pushing is opt-in via `--push`.

---

## Invariants & non-goals

**Invariants** (contracts that must always hold):

- Target is the **git root of cwd**, not the plugin's install path.
- **Only `execute.py` touches git.**
- **Step files are self-contained** — no references to an earlier conversation.
- Naming: top index `dir` = `{yyyymmdd}_{task}` (date included) ≠ per-task index `task` field = `{task}` (date excluded).
- **Refuses to run outside a git repo.**

**Non-goals** (deliberately out of scope):

- No parallel step execution / no per-step worktrees — steps run **sequentially**.
- No deploy — the most it does is an opt-in `git push` to a `feat-*` branch.
- No multi-user / concurrency handling.

---

## Development

Run the orchestrator's test suite:

```bash
.venv/bin/python -m pytest skills/sg-execute-task/scripts/test_execute.py -q
```

### Continuous integration

Two GitHub Actions run on every pull request:

- **`pytest`** (`.github/workflows/test.yml`) — runs the `execute.py` test suite.
- **`review`** (`.github/workflows/ai-review-gate.yml`) — Claude reviews the diff and posts a single `RISK: LOW | HIGH` verdict comment. Review-only: it has no merge authority.

See [`CLAUDE.md`](./CLAUDE.md) for the full development guide (purpose, invariants, and working discipline for hacking on the harness itself).

---

## License

[MIT](./LICENSE) © 2026 han0001
