---
name: review-pr
description: Review pipeline for a pull request or branch — deterministic checks by script (migration numbers against main, other long-lived branches and open PRs; edited applied migrations; PR base; conflicts; writes to read-only APIs; secrets in the diff; flagged paths), tests only for what the change touches, a clean-context reviewer agent, and one consolidated report; optionally comments on the PR and fixes in up to 2 rounds. Use when the user types /review-pr, asks to "review PR #N", "review this branch", "run the review pipeline", or to check a PR before merging.
---

# /review-pr — review pipeline

Arguments: `[<PR number> | <branch>] [--comment] [--fix]`. No target reviews the current branch.

The principle: **whatever can be decided by a rule runs in a script; the model only judges what
needs judgment and writes the synthesis.** The script costs seconds and no tokens; the reviewer
gets its output ready-made instead of rediscovering it.

```
1 checks (script)  ──►  3 reviewer (clean context)  ──►  4 synthesis  ──►  5 --fix (≤ 2 rounds)
      └──────────►  2 tests for what the change touches (script, in parallel with 3) ─┘
```

`check.py` lives in `scripts/` inside this skill's base directory (shown when the skill loads).
Call it by that absolute path: a worktree checked out for review may not contain this skill.
Per-repository rules come from `<repo>/.claude/review-pr.toml` (see `review-pr.example.toml`
here); without it only the generic checks run.

## Roles and models

| role | agent (`subagent_type`) | model / effort |
|---|---|---|
| coordinator (this session) | — | your session model |
| architect (plan before code) | `pipeline-architect` | Opus, high |
| light dev (front end, tests, mechanical, a pointed fix) | `pipeline-dev` | Sonnet, medium (pass `effort: high` for long work or a bug with no known cause) |
| heavy dev (money, payments, auth, migrations, cross-service) | `pipeline-dev-heavy` | Opus, medium |
| checks and tests (phases 1 and 2) | script | no model |
| reviewer (phase 3) | `pipeline-reviewer` | Opus, high |
| closed-list QA | `pipeline-qa-checklist` | Haiku, medium |
| flow QA | `pipeline-qa-flow` | Sonnet, high |

Installed as a plugin, the agent types are namespaced (`dev-pipeline:pipeline-reviewer`).

## Guards

- **Never move a checkout someone else may be using.** In the main checkout: no `checkout`,
  `switch`, `reset` or `stash`. If the target is not in a tree, create a detached worktree
  (phase 0). Before running or reading tests, confirm `git branch --show-current` and
  `git status --short` of the tree being measured: a test count that dropped may be another branch.
- **Tests only for what the change touches.** The script builds the plan that way. The full
  suite does not belong here.
- **The reviewer only reads.** It does not edit, run tests or launch subagents.
- **No reviewer finding reaches the report until you verify it in the code.** The reviewer is
  wrong in the same confident tone in which it is right.
- **Never print a secret's value or personal data.** The script reports only file:line.
- **Someone else's PR:** reviewing and commenting is fine; `--fix` is not. A finding that only
  swaps their established style for another does not go in.

## Phase 0 — Target and tree

1. The script refreshes remote refs itself (`--no-fetch` to skip).
2. Find where the code is:
   - the current branch of some tree → use that tree;
   - a PR or branch with no tree → `git worktree add --detach .claude/worktrees/rev-<n> origin/<branch>`
     from the main checkout (without `cd`-ing into it).
3. If the change touches a front end and the worktree has no `node_modules`, install them there.
   On Windows a junction to the main checkout's `node_modules` is faster, but see phase 6.
4. Contract: if the branch or title names a ticket and a tracker is connected (MCP), read the
   ticket's description and checklist. Otherwise the contract is the PR body — and a missing
   ticket is already a warning.

## Phase 1 — Deterministic checks

```bash
python -I <skill dir>/scripts/check.py --dir <tree> [--pr <n>] --json <scratchpad>/rev-<n>.json
```

Runs in seconds. Output: `[BLOCK]`, `[WARN]`, `[INFO]`, `[OK]`, plus the test plan.

| check | why |
|---|---|
| `migration-number`, `migration-duplicate` | a new version must be ABOVE the highest of main and every compared branch; both failures only show up at boot |
| `migration-race` | another open PR creates the same version: git merges both cleanly, the database does not |
| `migration-edited` | a migration already on main was edited or renamed: checksum/history mismatch on applied databases |
| `pr-base`, `conflict` | a PR stacked on an already-merged branch never reaches main; conflict with the tip of main |
| `read-only-api` | a write verb next to an API the repo may only read (configured) |
| `secret` | credential or credential file in the diff |
| `branch`, `commits`, `pr-title` | naming patterns (configured) |
| path flags | configured notes, e.g. UI files the reviewer compares with the design reference |

A PR that already merged is compared with main as it was **before** the merge, so it can be
reviewed after the fact.

## Phase 2 — Tests for what the change touches (in parallel with phase 3)

Same command with `--run-tests`, **in the background** (a Maven run can outlast a foreground
shell timeout). It only runs if the tree sits at the reviewed tip, and it postpones itself below
`min_free_memory_gb`. The plan per `[[tests]]` kind:

- **maven**: touched test classes + `<Class>*Test/IT` of each touched production class + the
  module's guard tests, in one `mvn -pl <modules> -am test -Dtest=...`.
- **go**: `gofmt -l`, `go vet` and `go test` of the touched packages.
- **node**: `tsc --noEmit`, `eslint` of the touched files, `jest --findRelatedTests` or `vitest related`.
- **command**: anything else, with the matching files substituted for `{files}`.

## Phase 3 — Clean-context reviewer

Launch **one** `Agent` (`subagent_type: "pipeline-reviewer"`) with the template in
[references/reviewer-prompt.md](references/reviewer-prompt.md), filled with: tree, `base..head`
range, the phase 0 contract, the phase 1 output, and the `reviewer` block of the JSON (project
context, repo rules, design reference) plus any rule in the repo's `CLAUDE.md` that applies.
**Do not pass your opinion or the author's about the PR**: its value is not being anchored.

Large PR (more than ~1,500 added lines, or back end and front end together): one reviewer per
area, in the same message, each with only its files.

## Phase 4 — Synthesis

1. Verify each reviewer finding by reading the cited code. Drop what does not hold (and say how
   many you dropped). Merge with phases 1 and 2.
2. Report, in this order, with no cosmetic findings:

```markdown
## Review — <branch> (PR #<n>)
**Verdict: BLOCKED | APPROVED WITH WARNINGS | APPROVED**

### Blockers
- [check or file:line] the defect, and what happens if it merges like this

### Findings (verified)
1. file:line — concrete defect — failure scenario — suggested fix

### Warnings
- ...

### Tests
- maven: N classes, green | go: ... | web: ... (or "did not run: reason")
```

A deterministic BLOCK or a red test → BLOCKED. A verified finding that changes behavior → at
most APPROVED WITH WARNINGS, and only if it is about coverage.

3. With `--comment`: `gh pr comment <n> --body-file <scratchpad>/rev-<n>.md`, **one** comment,
   ending with "Review generated by /review-pr (Claude Code)". Without the flag the report stays
   in the terminal.

## Phase 5 — `--fix` (your own branch only)

1. Up to **2 rounds**. In each: fix blockers and verified findings in the branch's tree, rerun
   phases 1 and 2. Stop when there is no BLOCK and no red test.
2. Commit: if the branch was never pushed, `--amend` (one task, one commit); if it was, a new
   `fix: ... review adjustments` commit, never a force-push.
3. Phase 3 only runs again if the fix changed design (more than the cited lines). Hand the fix to
   `pipeline-dev` (or `pipeline-dev-heavy` for a heavy area) when it spans more than a few files;
   a small one the coordinator fixes directly.
4. Still failing after round 2: stop and show the user what is left and why. Do not insist.

## Phase 6 — Cleanup

For a worktree created in phase 0: **remove any `node_modules` junction first** (PowerShell
`(Get-Item <junction>).Delete()`, then confirm no reparse point is left), then
`git worktree remove` and `git worktree prune`, each in its own command.
`git worktree remove` with a live junction deletes the target's contents — the main checkout's
`node_modules`.

## With /parallel-dev

Before each push in its phase 5, phase 1 of this skill (`check.py --dir <task worktree>`) catches
in seconds what its reviewer cannot see: migration numbers against the other open PRs, the PR
base, conflicts.
