---
name: sg-plan
description: Design phase of the sg-* workflow. Uses grill-me to resolve the decision tree one branch at a time and writes the result out to a plan.md plan document. Use when designing a new feature, when the user says "let's design first" / "let's make a plan", or when you want to clarify intent before implementing.
---

This skill is the **design phase** of the sg-* workflow. It uses grill-me to resolve the decision tree and writes the result out to a plan document.

**It does not implement.** The only output is a single `plan/{yyyymmdd}_{task-name}/plan.md` file. (No state file is created — pipeline stages are inferred from file existence.)

---

## Workflow

### A. Explore

Read the documents under `/docs/` (PRD, ARCHITECTURE, ADR, etc.) and `CLAUDE.md` to understand the product, architecture, and design intent. For questions the codebase can answer, explore directly using Explore agents in parallel.

### B. Interview (grill-me)

Use the `/grill-me` skill to interrogate the user through the decision tree **one branch at a time**.

- Present a **recommended answer** alongside each question.
- Resolve decisions in dependency order, dependencies first.
- For questions you can answer by reading the codebase, **explore directly** instead of asking the user.

The goal is to clarify design intent enough that the next phase (`/sg-phase`) can decompose the work — without implementing anything.

### C. Generate the plan

Once you reach agreement, get user approval and create `plan/{yyyymmdd}_{task-name}/plan.md`.

- `{yyyymmdd}`: today's date (e.g. `20260616`).
- `{task-name}`: a kebab-case slug (e.g. `csv-import`). Capture the core task in one or two words.

**Naming contract (important):** the `{yyyymmdd}_{task-name}` folder name chosen here is reused verbatim by `/sg-phase` when it creates `phases/{yyyymmdd}_{task-name}/` under the **same name**, making it the **mapping key**. Choose the task name carefully.

#### plan.md structure

```markdown
---
task: {task-name}
date: {yyyymmdd}
status: design
phase_dir: {yyyymmdd}_{task-name}
---

# Design Document: {title}

## 1. Goal and Motivation
{What and why. The current problem and the direction of the fix.}

## 2. Conceptual Model
{Core structure and data flow. Diagrams if needed.}

## 3. Decisions
{Decisions agreed via grill-me. Table or list.}

## 4. Open Questions
{Items to settle at implementation time + a default/recommendation for each.}

## 5. Decision Log
{The "why" behind each decision — choice / reason / trade-off.}
```

### Next step

Once the plan exists, move on to `/sg-phase` to decompose this plan.md into steps.
