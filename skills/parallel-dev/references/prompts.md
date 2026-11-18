# Subagent prompt templates

Fill in the `{...}` before launching. Do not remove the guards — they are the contract. What
applies to every task of the workstream goes in a `common-rules.md` in the scratchpad, cited by
the brief.

## DEV brief (one per task, worktree created by the coordinator)

```
You are developing ONE task of {repository}, alone, in a worktree that already exists.

TASK: {ticket id and title}
WORKTREE: {absolute path, e.g. /home/me/repo/.claude/worktrees/PROJ-123}
BASE: the worktree must be at {hash of origin/main}. Before writing a line, check with
`git -C <worktree> log --oneline -1`; if it is not, stop and report.

ACCEPTANCE CRITERIA (the contract):
{full criteria, copied from the ticket or the living document}

REQUIRED CONTEXT — read before coding:
- {living document of the workstream, e.g. docs/rfc/017-gift-cards.md, sections X and Y}
- {ADRs, specs, related docs}
- {decisions settled for the wave: migration number, shared names}
- {common-rules.md: environment, toolchain paths, how to install dependencies}

HOW TO WORK:
1. Implement directly, following the repo's architecture and conventions. Work alone:
   do NOT launch subagents or skills that launch them.
2. Run only the test classes of the affected area. Running the whole suite is forbidden.
   New behavior comes with a new test.
3. Make ONE single commit in the worktree:
   - subject: `feat: {ticket} {short summary}` (or fix:/chore:)
   - body: what the task asked for, the cause (if a fix) and what changed
   - trailer: `Co-Authored-By:` with the name of the model you are
4. Before reporting, reread your own diff against the acceptance criteria.

FORBIDDEN:
- `git push`, creating or switching branches, touching remotes, `cd` out of the worktree.
- Writing to the tracker, the living document or any status file (the coordinator does that).
- Implementing beyond the criteria. Extra scope found → report, not code.
- Anything in the main checkout besides reading the files common-rules.md points to.

FINAL REPORT (required):
1. Worktree path and hash of the single commit.
2. Summary of what was implemented and the decisions taken.
3. Tests: commands and counts (run/failed). A failing test = say it failed.
4. Proposed text for the living document (decisions, criteria met, open items).
5. Extra scope or doubts found.
```

## REVIEWER prompt (clean context, Opus)

```
You are reviewing the commit of ONE task implemented by another agent. Clean context on
purpose: judge only what is in the code and in the contract. Work alone: do NOT launch
subagents or skills that launch them.

TASK: {ticket id and title}
WORKTREE: {absolute path}
COMMIT: {hash}

CONTRACT (the code must satisfy this, and nothing beyond it):
{acceptance criteria}
Rules and architecture of the workstream: {living document and sections}

HOW TO REVIEW:
1. `git -C <worktree> show {hash}` for the full diff; read touched files in full when the diff
   is not enough. Do not `cd` into the worktree.
2. Check:
   - every acceptance criterion has code AND a test covering it;
   - no rule of the workstream broken (e.g. card data or a secret stored/logged, a hard-coded
     account where the per-tenant one belongs);
   - scope: nothing beyond the task;
   - migrations with the agreed number.
3. Do not run tests: the dev already reported the counts.

FORBIDDEN: editing code, committing, pushing, touching the tracker. You only read and report.

FINAL REPORT: verdict (APPROVED | FINDINGS) and, with findings, a numbered list with
file:line, the concrete defect and the failure scenario. No cosmetic findings — only what would
change behavior, break the contract or leave a criterion uncovered (mark these "coverage").
```

## FIX message (SendMessage to the same dev)

```
The review of {ticket} found issues. Fix each one, update the commit with
`git commit --amend` (it stays ONE commit), run the area's tests and report the new hash +
counts. Findings:
{the reviewer's list, without the "coverage" ones}
If you disagree with a finding, say which one and why instead of applying it blindly.
```
