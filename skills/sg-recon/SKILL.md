---
name: sg-recon
description: Use when a goal needs facts the code alone cannot reveal — runtime behavior, test results, logs, metrics, data, tickets, docs — gathered before designing or implementing.
---

# Recon

Gather evidence for a goal through read-only probes run by `sg-harness:sg-prober` subagents. The main session only orchestrates and keeps a compact ledger; raw output stays inside the subagents.

**Claude Code only.** If the host cannot launch `sg-harness:sg-prober`, stop and say that this skill requires Claude Code.

The deliverable is the final report in this conversation, in the user's language. Write no files: the user saves the report when needed, and `sg-plan` in the same session reads it from the conversation.

## Hard lines
- Change nothing: code, data, external systems, deployments. Running and querying to observe is fine.
- Never bring raw personal data or secrets into the main session; aggregate or mask.

## Workflow
1. **Goal** — restate the goal and its done criterion: what must be known to move on.
2. **Code recon** — one `Explore` subagent summarizes what the code already answers. Never probe for that.
3. **Questions** — list only the unknowns the code cannot answer, each with the decision it unlocks, where to look, and how. Find sources in the project instructions, then in the available tools; if still unknown, ask the user.
4. **Gate** — present the list once. The user may answer, drop, or add questions; the user is the cheapest source.
5. **Round** — launch one `sg-harness:sg-prober` per question, in parallel, in one message. Brief: the question, why it matters, source hints. Never your hypothesis.
6. **Ledger** — merge the results and print `Round N: answered a · partial p · new q`. Keep a new question only if it names a decision tied to the goal. Later rounds need no approval.
7. **Verify** — when answers conflict, or an `inferred` answer decides the done criterion, send a prober to rerun the repro and look for counter-evidence. Still unresolved → report it as a conflict.
8. **Stop** when the done criterion is met, no new questions remain, or every remaining question is unanswerable.

## Report
```
Goal: <one line> | Done criterion met: yes/no

| # | Question | Answer | Grade | Repro |

Conflicts: <sources that disagree, docs vs. actual behavior>
Open unknowns: <unanswerable or replaced by an assumption, with the reason>
Implications: <2–4 lines: what this decides or blocks next>
```
