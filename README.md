# dev-pipeline for Claude Code

[Leia em português](README.pt-BR.md)

A Claude Code plugin with two skills and seven role agents, extracted from the daily workflow of a
multi-service monorepo (Java, Go, React Native, React) shared by several people and several
Claude sessions at once.

- **`/review-pr`** — reviews a PR or branch. A script runs every check that a rule can decide,
  in seconds and with no tokens; a reviewer agent in a clean context judges the rest; you get a
  single report, optionally posted as one PR comment, with up to two automatic fix rounds.
- **`/parallel-dev`** — develops the tasks of a workstream in waves: one worktree, one branch,
  one commit and one PR per task, each implemented by a subagent and reviewed by another that
  never saw it being written.

## Why

Two lessons drive the design:

1. **What a rule can decide should not cost a model.** Migration numbering, PR base, merge
   conflicts, secrets and forbidden API calls are decided by `check.py`. The model only gets
   what needs judgment — and receives the script's output so it does not rediscover it.
2. **A reviewer is only useful if it is not anchored.** The reviewer never sees the author's
   reasoning or yours, only the diff and the contract. And its findings are verified in the code
   before they reach the report: it is wrong in the same confident tone in which it is right.

### Incidents the checks exist for

All of these merged cleanly in git and only failed at runtime:

| check | what happened |
|---|---|
| `migration-number` | a version that was free on `main` but already applied on `develop`: *"Detected resolved migration not applied to database"* |
| `migration-race` | two open PRs picked the same version the same afternoon; both merged, the boot died with *"Found more than one migration with version N"* |
| `migration-edited` | a header comment added to an applied migration changed its checksum |
| `pr-base` | a PR stacked on a branch that had already merged — it was merged too, into a dead branch, and never reached `main` |
| `read-only-api` | an integration that may only read orders/payments in production |

## Install

As a plugin (recommended):

```
/plugin marketplace add obelucca/claude-dev-pipeline
/plugin install dev-pipeline@obelucca
```

Or copy `skills/*` into `<repo>/.claude/skills/` (or `~/.claude/skills/`) and `agents/*` into
`.claude/agents/`. Copied this way, the read-only hook is not registered for you: add
`hooks/read_only.py` to `.claude/settings.json` under `PreToolUse` / `Bash` (see
[`hooks/hooks.json`](hooks/hooks.json)).

Requirements: `git` 2.38+, Python 3.11+, and the GitHub CLI (`gh`) for PR data and comments.

## Configure

Copy [`skills/review-pr/review-pr.example.toml`](skills/review-pr/review-pr.example.toml) to
`<repo>/.claude/review-pr.toml` and keep what applies. Everything is optional; without the file
you still get the naming, PR base, conflict and secret checks.

- `compare_branches` — other long-lived lines whose migrations count toward the ceiling.
- `[[migrations]]` — `kind = "flyway"`, `"golang-migrate"`, or your own regex.
- `[[read_only_apis]]` — an API resource that must never appear next to a write verb.
- `[[path_flags]]` — paths that raise a note, a warning or a blocker (UI files to compare with
  the design, CI files, ...).
- `[[tests]]` — how to test only what changed: `maven`, `go`, `node` (jest/vitest) or `command`.
- `[reviewer]` — project context, repository rules and the design reference for the reviewer prompt.

### Project rules, in one place

Copy [`templates/project-rules/SKILL.md`](templates/project-rules/SKILL.md) to
`<repo>/.claude/skills/project-rules/SKILL.md` and write your rules there. Every agent preloads
it through `skills:` (a missing skill is skipped silently), so the rules that apply to every role
live in one file instead of being repeated in each agent, the reviewer prompt and every task
brief. Agents also load your `CLAUDE.md`; keep in `project-rules` what a subagent must never miss.

### Read-only agents are enforced, not asked

`tools: Read, Grep, Glob, Bash` does not make an agent read-only: Bash can write. The plugin
registers one `PreToolUse` hook ([`hooks/read_only.py`](hooks/read_only.py)) that looks at the
`agent_type` of each Bash call and, only for the architect, the reviewers and the checklist QA,
allows read commands (read-only `git`, `grep`, `sed -n`, ...; the QA profile adds `curl` to
localhost and `docker ps/logs`). Everything else is denied with the reason, and the agent reports
NOT VERIFIED instead of looking for a workaround. The main conversation and the other agents pass
untouched. Add your own read-only agents to `AGENT_PROFILES`.

## Use

```
/review-pr                 # current branch
/review-pr 123 --comment   # PR #123, posts the report as one comment
/review-pr --fix           # your branch: fixes blockers and verified findings, up to 2 rounds
/parallel-dev              # plan waves from the open tasks of a workstream
```

The script also runs on its own:

```bash
python skills/review-pr/scripts/check.py --pr 123
python skills/review-pr/scripts/check.py --dir ../worktree --run-tests --json out.json
```

Exit code 0 means no blocker, 2 at least one blocker, 1 a usage error.

## Agents

| agent | model | role |
|---|---|---|
| `pipeline-architect` | Opus, high | plans before code; read-only |
| `pipeline-dev` | Sonnet, medium | light or well-specified work |
| `pipeline-dev-heavy` | Opus, medium | money, payments, auth, migrations |
| `pipeline-reviewer` | Opus, high | clean-context review; read-only |
| `pipeline-security` | Opus, high | clean-context security review of the diff (IDOR, enumeration, injection, secrets); read-only |
| `pipeline-qa-checklist` | Haiku, medium | closed list of objective criteria |
| `pipeline-qa-flow` | Sonnet, high | end-to-end on the local stack, hunting the breaking case |

## Development

```bash
python -m unittest discover -s tests -v
```

The tests build throwaway git repositories and assert each check end to end.

## License

[MIT](LICENSE)
