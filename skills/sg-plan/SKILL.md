---
name: sg-plan
description: Use when designing a feature, clarifying intent before implementation, or turning an initial request into an approved plan.md.
---

# Plan a task

This is the **design stage** of the sg-* workflow. It resolves the decision tree with the user and writes one `docs/sg/plan/{yyyymmdd}_{task-name}/plan.md` file.

**Do not implement.** Do not create task state or step files in this stage.

## Workflow

### A. Explore first

Read the repository and the permanent documentation relevant to the requested design before asking questions. Read the active project instruction files when present (`AGENTS.md`, `CLAUDE.md`, or both), following the current host's precedence rules.

Answer codebase questions by inspecting the repository. Read-only exploration subagents are optional when the host supports them and parallel exploration is useful; otherwise explore directly. Keep the design interview in the main conversation.

### B. Interview

Resolve the design with the user using this contract:

1. Ask only decisions that the repository and docs cannot answer.
2. Resolve dependencies first so later choices build on earlier ones.
3. Ask one question at a time and include a recommended answer with its reason.
4. Record each choice, reason, and trade-off for the decision log.
5. Present the resulting design, then ask for direct confirmation such as “Approve this design and create `plan.md`?” Do not treat a prior answer or silence as approval.

The interview may use a host-provided questioning skill when available, but this contract is self-contained and remains authoritative.

### C. Generate the plan

After approval, create `docs/sg/plan/{yyyymmdd}_{task-name}/plan.md`.

- `{yyyymmdd}`: today's date, for example `20260616`.
- `{task-name}`: a one- or two-word kebab-case slug, for example `csv-import`.

The folder name is the mapping key for the next stage. The `sg-decompose-task` skill must reuse `{yyyymmdd}_{task-name}` verbatim under `docs/sg/tasks/`.

```markdown
---
task: {task-name}
date: {yyyymmdd}
status: design
task_dir: {yyyymmdd}_{task-name}
---

# Design Document: {title}

## 1. Goal and Motivation
{What and why. The current problem and the direction of the fix.}

## 2. Conceptual Model
{Core structure and data flow. Diagrams if needed.}

## 3. Decisions
{Decisions approved during the interview.}

## 4. Open Questions
{Items deferred to implementation, with a default or recommendation for each.}

## 5. Decision Log
{Each choice, its reason, and its trade-off.}
```

### Next stage

Once the plan exists, use `sg-decompose-task` to turn it into executable steps.
