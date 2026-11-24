---
name: parallel-dev
description: Orchestrates the tasks of a workstream in parallel waves — launches developer subagents in their own worktrees (one per task, branched from the remote main), reviews each in a clean context, fixes with amend, and delivers one commit and one PR per task. Use when the user asks to "develop these tasks/stories in parallel", "run a wave", "orchestrate development", "speed up development" or "launch the dev subagents".
---

# /parallel-dev — task orchestrator in waves

You are the coordinator. Subagents develop and review; you plan the waves, create the
worktrees, integrate, merge (when authorized) and keep the tracker up to date. The prompt
templates are in `references/prompts.md` — read them before launching the first wave.

## Guards (always, no exceptions)

- **The main checkout may be shared** with other sessions and agents: in it, never `checkout`,
  `switch`, `reset`, `stash` or `cherry-pick`. All work happens in worktrees, driven by
  `git -C <worktree>` and absolute paths. Never `cd` into a worktree (the session cwd goes with
  it and the removal fails later).
- **One task = one worktree = one branch = one commit = one PR against main.** The branch is
  born from `origin/<main>` right after `git fetch`. Branch and commit names follow the repo's
  convention (`.claude/review-pr.toml` `[naming]`, or the repo's CONTRIBUTING/CLAUDE.md).
- **Push, PR and merge belong to the coordinator** — and merging only if the user authorized it
  for this workstream. Without that, stop at an open, reviewed PR.
- **Commit:** conventional subject with the ticket id, a body with defect/cause/change for a
  fix, and a `Co-Authored-By` trailer **of the model that wrote the commit**. Do not rewrite a
  subagent's trailer or amend because of it.
- **Tracker** (if one is connected via MCP): only the coordinator writes. Create the missing
  ticket (one per defect), move it to In Progress at launch, comment progress, and **close it
  after the merge**. Never close before the commit is on main. No tracker → one line saying so,
  and carry on.
- **Living document of the workstream** (an RFC or doc the user names): only the coordinator
  edits it. A subagent proposes text in its report.
- **Tests:** a subagent runs only the test classes of the affected area. The full suite of each
  touched module runs **once**, by the coordinator, on the integrated tree, before the merge,
  alone on the machine, with enough free memory.
- **A subagent never delegates.** Agent-inside-Agent, and skills that launch subagents, hang:
  the children report to the top-level session.
- **Never overwrite anyone's uncommitted changes.** A dirty file in the way → stop and ask.
- Extra product scope discovered midway: do not expand silently — record it as a new ticket and
  tell the user. Do not propose chore/infra tasks along the way.

## Models

The coordinator does no grunt work. Use the role agents of this plugin, or `Agent` with a model:

- **Opus** (`pipeline-dev-heavy`) — money, payments, integrations with external systems of
  record, auth, migrations, heavy design, end-to-end proof, and every reviewer (including
  `pipeline-security`).
- **Sonnet** (`pipeline-dev`) — front end, tests, scaffolding, extraction, mechanical edits, chores.
- On repeated 429/529, ask before switching models.

## Phase 1 — Prepare

1. Backlog: the workstream's open tickets (tracker) and the living document.
2. `git fetch origin`; note the hash of `origin/<main>` (the wave's base) and the highest
   migration number on main and on every other long-lived branch.
3. `git status` in the main checkout only to know what is dirty; nothing to do there.
4. Candidates: tasks whose dependencies are already on main.

## Phase 2 — Plan the waves

1. Group by real independence: no functional dependency **and** little file overlap (services,
   apps and workers are natural areas).
2. Settle up front what two devs could fight over: the number of each new migration, the owner
   of each section of the living document, shared constant/route names.
3. Present the plan (waves, tasks, area, model, conflict risks). The first wave of a workstream
   waits for the user's OK; later waves of the same workstream proceed unless told otherwise.

## Phase 3 — Launch the wave

For each task, in the same message (parallel):

1. `git worktree add .claude/worktrees/<TASK> -b <TASK> origin/<main>` from the main checkout.
   Do not use the harness's `isolation: "worktree"`: it branches from the local main, which is
   often behind.
2. Write the brief in the scratchpad (`<workstream>/<TASK>-brief.md`, plus a shared
   `common-rules.md` for the workstream) from the dev template in `references/prompts.md`.
   Environment specifics (JDK path, offline Maven, how to install front-end dependencies)
   belong in `common-rules.md`.
3. `Agent` with the model for its weight, pointing to the brief, the worktree's absolute path
   and the base hash.
4. Ticket → In Progress.

The dev report must bring: worktree, hash of the single commit, summary, tests (command and
counts) and the proposed text for the living document. If something is missing, ask via
`SendMessage`.

## Phase 4 — Clean-context review

1. Launch a **new** reviewer `Agent` (`pipeline-reviewer`, no worktree of its own) with the
   review template, pointing to the worktree and the commit. The reviewer only reads: it does not
   run tests (the dev reported the counts), edit or delegate. When the architect marked the task
   `security: yes` (or the diff touches auth, personal data, payments, endpoints, webhooks or
   secrets), launch `pipeline-security` in the same message, with the same inputs.
2. Findings → `SendMessage` to the **same** dev to fix with `--amend` and report the new hash.
   At most 2 rounds; if it persists, escalate to the user with the reviewer's report.
3. A coverage-only finding does not go back to the dev: the coordinator closes it at integration.
4. Clean review → phase 5. If `/review-pr` is installed, its phase 1
   (`check.py --dir <worktree>`) runs here too, in seconds.

## Phase 5 — Integrate and merge

With the worktree still alive:

1. `git fetch`; if main moved, rebase the branch onto `origin/<main>`. A wave with conflicts
   between tasks becomes a linear stack (each PR against main, with the note "merge after #N");
   after an `--amend` in the middle, the ones above go with `git rebase --onto <new> <old-hash>`.
2. Conflict in the living document's log: keep both lines. A migration whose number is already
   taken: renumber above the highest of every compared line and delete any stale copy in build
   output. A fixture broken by another task of the wave that changed a type: fold the fix into
   that task's commit (amend), not a new commit.
3. Update the living document on the branch (decisions, criteria met, open items).
4. Full suite of the touched modules, once, on the integrated tip (detached in the background
   when it outlasts the shell timeout, watching the log).
5. Push, open the PR (`gh pr create --base <main>`), and — if authorized — merge. Confirm the PR
   reports merged before any cleanup: a merge refused for conflicts leaves it open.
6. Cleanup, each step in its own command and from the main checkout: remove `node_modules`
   junctions first (and confirm no reparse point is left), then `git worktree remove`,
   `git worktree prune`, `git branch -D` and `git push --delete` of the branch.

## Phase 6 — Close

Ticket closed with a comment (PR and commit on main). Summary to the user per task: what
changed, tests, findings fixed, PR. Then the next wave.

## When something goes wrong

- A rate limit (429/529) takes down every agent: the worktrees stay intact. Do not relaunch;
  `SendMessage` to the same agent with "resume where you stopped".
- A dev stuck or with a poor result: read the report and choose between instructing via
  `SendMessage` or returning the task to the backlog and telling the user.
- Full suite failing on the integrated tip: fix it in the responsible task's commit (amend) and
  run again; never merge red.
- Tracker MCP unavailable → one warning line and go on without tracking, no retry loop.
