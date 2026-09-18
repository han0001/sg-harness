---
name: sg-execute-task
description: Use when an sg-* task has been decomposed into step files and is ready for sequential execution.
---

# Execute a decomposed task

This is the **execution stage** of the sg-* workflow. It runs the step files produced by `sg-decompose-task` sequentially, one isolated child-agent session per step, with retry and progress reporting.

The **host** is the agent running this skill. The **runtime** is the child-agent CLI selected with `--runtime`; they may be different providers.

## Input

The current project must contain:

- `docs/sg/tasks/index.json`, including this task's `dir` entry.
- `docs/sg/tasks/{yyyymmdd}_{task-name}/index.json`.
- `docs/sg/tasks/{yyyymmdd}_{task-name}/step{N}.md` files.

If they are missing, use `sg-decompose-task` first.

## Before execution

1. Confirm that the host is operating from the target project's git repository.
2. Select the requested runtime: `claude` or `codex`. If the user did not specify one, ask them to choose before requesting safety approval. Do not infer it from the host and never switch runtimes after a failure.
3. Use the source locator supplied by the host when it loaded this skill. For a filesystem-backed skill, take the directory containing this `SKILL.md` and append `scripts/execute.py`; verify that file exists before running it. If the host does not expose a usable bundled-resource locator, stop and report that packaging error instead of guessing from cwd or a provider-specific environment variable.
4. Tell the user which runtime will run and obtain one explicit safety approval. The executor creates or checks out a feature branch and commits automatically. Claude children disable permission checks; Codex children run non-interactively with `workspace-write`. Pushing remains opt-in.

## Execute

Run one step per call so the host can relay progress after every step:

```bash
python3 "<directory containing the loaded SKILL.md>/scripts/execute.py" {yyyymmdd}_{task-name} --runtime {claude|codex} --once
python3 "<directory containing the loaded SKILL.md>/scripts/execute.py" {yyyymmdd}_{task-name} --runtime {claude|codex} --once --push
```

Use `--push` only on the final call and only when the user requested a push.

1. Run the next pending step with `--once`.
2. Relay the printed `✓ Step N/M … — {summary}` and `Next ▶ …` progress lines. Do not read `step{N}-output.json`; the printed summary is sufficient.
3. If another step remains and the result is neither `error` nor `blocked`, repeat immediately without requesting another approval.
4. Stop when the executor prints `All steps completed!`, or on `error`/`blocked`.

The target is always the **git root of cwd**, not the plugin installation directory.

## Executor contract

`execute.py`:

- creates or checks out `feat-{task-name}`;
- loads project instructions with runtime-specific precedence (`CLAUDE.md` first for Claude, `AGENTS.md` first for Codex), plus top-level `docs/*.md`;
- passes completed-step summaries into the next prompt;
- retries failed attempts up to three times with the previous error;
- records timestamps and normalized verdicts;
- commits code and metadata separately;
- pushes only with `--push`;
- warns when task indexes are desynchronized.

Only the orchestrator may commit or push. Child sessions report their result through the verdict schema and must not edit `index.json`.

## Error recovery

- **On error:** after fixing the cause, set the step `status` back to `"pending"`, remove `error_message`, and rerun.
- **On blocked:** resolve `blocked_reason`, set `status` back to `"pending"`, remove `blocked_reason`, and rerun.

## Next stage

When execution is complete, use `sg-source-of-truth` to propose syncing the task's decisions into permanent docs and active project instruction files.
