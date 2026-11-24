---
name: project-rules
description: The rules every pipeline agent of this repository must follow (shared checkout, read-only integrations, personal data, migrations, tests, product rules). Preloaded by the dev-pipeline agents through `skills:`; not meant to be invoked by hand.
user-invocable: false
---

<!--
Copy this file to <repo>/.claude/skills/project-rules/SKILL.md and replace the examples with your
own rules. Every dev-pipeline agent preloads it, so it is the ONE place for rules that apply to
every role: do not repeat them in the agents, in the reviewer prompt or in task briefs.

Keep it short and concrete. A rule here is something whose violation is a defect, not a style
preference. Role-specific instructions stay in the agent files.
-->

# Rules for every pipeline agent in this repository

A violation of any item below is a defect, not a matter of style.

## Where work happens

- The main checkout (`/path/to/repo`) is shared with other sessions. In it: never `checkout`,
  `switch`, `reset`, `stash` or `cherry-pick`. Work happens in a worktree, through `git -C <tree>`.
- Before committing and before interpreting any result taken from a tree:
  `git branch --show-current` and `git status --short`.
- A subagent never delegates (no Agent inside an agent). Push, PR, merge and ticket updates belong
  to the coordinator.

## What the code must respect

- The existing pattern wins: follow the neighboring code and the decision records in `docs/adr/`.
- Migrations: the number is ABOVE the highest on `main` and on every other long-lived branch, and
  is re-checked before the push. An applied migration is never edited or renamed.
- Example: a new message queue is declared in the consumer AND in the shared topology class.
- Example: no new CI job.

## Data, network and third parties

- Example: the payments provider API is read-only for us: GET only, never in tests either.
- Personal data (name, tax id, e-mail, address, phone, card) never in the terminal, in logs, in
  error messages, in versioned files or in tests with real values. Measure by status code, count
  and field name.
- Secrets are never printed.

## Tests and machine

- Test only what you touched (e.g. `mvn -pl <module> -am test -Dtest=...`,
  `jest --findRelatedTests --maxWorkers=2`, `vitest related`). The full suite is the coordinator's.

## Product rules

- Example: screens follow the design reference in `docs/design/`; without a reference, say so
  instead of inventing a layout.
- Example: a product without a photo is never shown.

## Reporting

- Say what you ran, with the real result, and what you could NOT verify.
