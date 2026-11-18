#!/usr/bin/env python3
"""Stages 1 and 2 of /review-pr: deterministic checks, and tests for what the change touches.

Nothing here calls a model. Whatever can be decided by a rule is decided by a rule, and the
reviewer (stage 3) receives the result ready-made instead of spending context redoing it.

Usage (from anywhere; paths absolute or relative to the cwd):

    python check.py                          # current branch of --dir (default: cwd)
    python check.py --pr 640                 # open or merged PR, by its remote head
    python check.py --dir <worktree> --run-tests --json out.json
    python check.py --config path/to/review-pr.toml

Config: --config, else <repo>/.claude/review-pr.toml, else <repo>/review-pr.toml. Every key is
optional; see review-pr.example.toml next to this skill. Without a config you still get the
branch/commit, PR base, conflict and secret checks.

Output: a text report on stdout and, with --json, the same as JSON for the reviewer.
Exit code: 0 no blocker, 2 at least one BLOCK, 1 usage error.

Data rule: the script never prints the VALUE of a secret, only file and line.
Requires Python 3.11+ (tomllib), git, and optionally the GitHub CLI (gh) for PR data.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

BLOCK, WARN, OK, INFO = "BLOCK", "WARN", "OK", "INFO"
SEVERITY = {"block": BLOCK, "warn": WARN, "info": INFO}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- config

CONVENTIONAL_COMMIT = r"^(feat|fix|chore|docs|refactor|test|perf|build|ci|style|revert)(\([^)]*\))?!?: \S"

MIGRATION_PRESETS = {
    # V1__init.sql, V66.1__x.sql, V2_1__x.sql
    "flyway": r"(?P<dir>(?:.*/)?db/migration)/V(?P<ver>\d+(?:[._]\d+)*)__(?P<name>[^/]+)\.sql$",
    # 000123_add_users.up.sql + 000123_add_users.down.sql share a version on purpose
    "golang-migrate": r"(?P<dir>(?:.*/)?migrations?)/(?P<ver>\d+)_(?P<name>[^/]+?)\.(?:up|down)\.sql$",
}

DEFAULTS = {
    "remote": "origin",
    "main_branch": None,          # autodetected from <remote>/HEAD, then main, then master
    "pr_base": None,              # defaults to main_branch
    "compare_branches": [],       # other long-lived branches whose migrations also count
    "naming": {"branch": None, "commit": CONVENTIONAL_COMMIT, "pr_title": None, "hint": ""},
    "migrations": [],
    "read_only_apis": [],
    "secrets": {"patterns": [], "ignore_paths": []},
    "path_flags": [],
    "tests": [],
    "min_free_memory_gb": 2.0,
    "test_timeout_minutes": 30,
    "reviewer": {},
}


def load_config(root: Path, explicit: str | None) -> tuple[dict, str | None]:
    candidates = [Path(explicit)] if explicit else [root / ".claude" / "review-pr.toml", root / "review-pr.toml"]
    path = next((p for p in candidates if p.is_file()), None)
    if explicit and not path:
        raise RuntimeError(f"config not found: {explicit}")
    cfg = json.loads(json.dumps(DEFAULTS))
    if not path:
        return cfg, None
    if tomllib is None:
        raise RuntimeError("reading review-pr.toml needs Python 3.11+ (tomllib)")
    with path.open("rb") as f:
        user = tomllib.load(f)
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg, str(path)


# --------------------------------------------------------------------------- infra

@dataclass
class Check:
    name: str
    status: str
    summary: str
    details: list[str] = field(default_factory=list)


def sh(args: list[str], cwd: Path, check: bool = True, timeout: int | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} -> {r.returncode}\n{r.stderr.strip()}")
    return r


def git(cwd: Path, *args: str, check: bool = True) -> str:
    return sh(["git", *args], cwd, check=check).stdout


def gh_json(cwd: Path, *args: str):
    if not shutil.which("gh"):
        return None
    r = sh(["gh", *args], cwd, check=False)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return json.loads(r.stdout)


def fetch(cwd: Path, remote: str) -> str | None:
    # --no-write-fetch-head: a review must not leave FETCH_HEAD pointing at every branch,
    # or the user's next `git pull` may try an octopus merge.
    r = sh(["git", "fetch", "--quiet", "--prune", "--no-write-fetch-head", remote], cwd, check=False)
    return None if r.returncode == 0 else r.stderr.strip()


def ref_exists(cwd: Path, ref: str) -> bool:
    return sh(["git", "rev-parse", "--verify", "--quiet", ref], cwd, check=False).returncode == 0


def detect_main(cwd: Path, remote: str) -> str:
    r = sh(["git", "symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD"], cwd, check=False)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().split("/", 1)[1]
    for b in ("main", "master"):
        if ref_exists(cwd, f"{remote}/{b}") or ref_exists(cwd, b):
            return b
    raise RuntimeError("could not detect the main branch: set main_branch in review-pr.toml")


def main_ref(cwd: Path, remote: str, branch: str) -> str:
    return f"{remote}/{branch}" if ref_exists(cwd, f"{remote}/{branch}") else branch


# --------------------------------------------------------------------------- target

PR_FIELDS = "number,title,baseRefName,headRefName,state,author,url,isCrossRepository,mergeCommit"


@dataclass
class Target:
    head: str            # ref or hash of the reviewed tip
    branch: str
    base: str            # merge-base with main
    main: str            # <remote>/<main>, or main as it was before the merge if the PR already merged
    pr: dict | None
    description: str


def resolve_target(cwd: Path, cfg: dict, pr_num: int | None, branch: str | None) -> Target:
    remote, main_branch = cfg["remote"], cfg["main_branch"]
    pr = None
    if pr_num is not None:
        pr = gh_json(cwd, "pr", "view", str(pr_num), "--json", PR_FIELDS)
        if not pr:
            raise RuntimeError(f"PR #{pr_num} not found via gh")
        branch = pr["headRefName"]
        head = f"{remote}/{branch}"
        if pr.get("isCrossRepository") or not ref_exists(cwd, head):
            # fork, or branch deleted after merge: review through the PR ref
            head = f"{remote}/pr/{pr_num}"
            r = sh(["git", "fetch", "--quiet", "--no-write-fetch-head", remote,
                    f"+refs/pull/{pr_num}/head:refs/remotes/{head}"], cwd, check=False)
            if r.returncode != 0 and not ref_exists(cwd, head):
                raise RuntimeError(f"could not fetch the head of PR #{pr_num} from '{remote}':\n{r.stderr.strip()}")
    else:
        branch = branch or git(cwd, "branch", "--show-current").strip()
        if not branch:
            raise RuntimeError("detached HEAD: pass --branch or --pr")
        head = branch if ref_exists(cwd, branch) else f"{remote}/{branch}"
        found = gh_json(cwd, "pr", "list", "--head", branch, "--state", "all", "--json", PR_FIELDS)
        if found:
            open_prs = [p for p in found if p["state"] == "OPEN"]
            pr = (open_prs or found)[0]
    # Already merged: compare with main from BEFORE the merge, or the diff comes out empty and
    # its migrations look like "a number main already has".
    main = main_ref(cwd, remote, main_branch)
    before_merge = False
    if pr and pr["state"] == "MERGED" and (pr.get("mergeCommit") or {}).get("oid"):
        main = git(cwd, "rev-parse", pr["mergeCommit"]["oid"] + "^1").strip()
        before_merge = True
    base = git(cwd, "merge-base", main, head).strip()
    desc = f"PR #{pr['number']} ({pr['state']})" if pr else "no PR"
    if before_merge:
        desc += f", compared with {main_branch} before the merge ({main[:9]})"
    return Target(head=head, branch=branch, base=base, main=main, pr=pr, description=f"{branch} - {desc}")


# --------------------------------------------------------------------------- diff

@dataclass
class File:
    status: str          # A, M, D, R
    path: str
    old: str | None = None


def changed_files(cwd: Path, t: Target) -> list[File]:
    out = git(cwd, "diff", "--name-status", "-M", t.base, t.head)
    res = []
    for line in out.splitlines():
        parts = line.split("\t")
        st = parts[0][0]
        if st == "R":
            res.append(File("R", parts[2], parts[1]))
        else:
            res.append(File(st, parts[1]))
    return res


def added_lines(cwd: Path, t: Target) -> dict[str, list[tuple[int, str]]]:
    """file -> [(line number at the tip, text)] for added lines only."""
    out = git(cwd, "diff", "-U0", "--no-color", "--no-ext-diff", t.base, t.head)
    res: dict[str, list[tuple[int, str]]] = {}
    cur, n = None, 0
    for line in out.splitlines():
        if line.startswith("+++ "):
            cur = None if line[4:] == "/dev/null" else line[6:]
            continue
        m = re.match(r"@@ -\S+ \+(\d+)(?:,\d+)? @@", line)
        if m:
            n = int(m.group(1))
            continue
        if cur and line.startswith("+") and not line.startswith("+++"):
            res.setdefault(cur, []).append((n, line[1:]))
            n += 1
    return res


RE_TEST_PATH = re.compile(r"(^|/)(src/test/|__tests__/|tests?/)|_test\.go$|(^|/)test_[^/]*\.py$|\.(test|spec)\.[cm]?[jt]sx?$")


def is_test(path: str) -> bool:
    return RE_TEST_PATH.search(path) is not None


# --------------------------------------------------------------------------- checks

def chk_naming(cwd: Path, cfg: dict, t: Target) -> list[Check]:
    n = cfg["naming"]
    hint = [n["hint"]] if n.get("hint") else []
    res = []
    if n.get("branch"):
        if re.search(n["branch"], t.branch):
            res.append(Check("branch", OK, f"branch {t.branch}"))
        else:
            res.append(Check("branch", WARN, f"branch '{t.branch}' does not match {n['branch']}", hint))
    log = git(cwd, "log", "--no-merges", "--format=%h%x09%s", f"{t.base}..{t.head}")
    commits = [line.split("\t", 1) for line in log.splitlines() if line]
    if not commits:
        res.append(Check("commits", WARN, f"no commits beyond {cfg['main_branch']}"))
    elif n.get("commit"):
        bad = [f"{h} {s}" for h, s in commits if not re.search(n["commit"], s)]
        if bad:
            res.append(Check("commits", WARN, f"{len(bad)} of {len(commits)} commit subjects do not match {n['commit']}",
                             bad + hint))
        else:
            res.append(Check("commits", OK, f"{len(commits)} commit(s) with a conforming subject"))
    if t.pr and n.get("pr_title") and not re.search(n["pr_title"], t.pr["title"]):
        res.append(Check("pr-title", WARN, f"PR title does not match {n['pr_title']}: {t.pr['title']}", hint))
    return res


def chk_base_and_conflict(cwd: Path, cfg: dict, t: Target) -> list[Check]:
    res = []
    want = cfg.get("pr_base") or cfg["main_branch"]
    if t.pr:
        base = t.pr["baseRefName"]
        if base != want:
            st = BLOCK if t.pr["state"] == "OPEN" else WARN
            res.append(Check("pr-base", st, f"PR targets '{base}', not '{want}'",
                             ["a PR stacked on a branch that already merged never reaches the main branch",
                              f"fix with `gh pr edit {t.pr['number']} --base {want}`"]))
        else:
            res.append(Check("pr-base", OK, f"PR targets {want}"))
    behind = int(git(cwd, "rev-list", "--count", f"{t.head}..{t.main}").strip())
    r = sh(["git", "merge-tree", "--write-tree", "--name-only", t.main, t.head], cwd, check=False)
    if r.returncode == 1:
        # output: <tree>\n<conflicted files>\n\n<messages>
        files = r.stdout.split("\n\n", 1)[0].splitlines()[1:]
        res.append(Check("conflict", BLOCK, f"conflicts with {cfg['main_branch']} in {len(files)} file(s)", files))
    elif r.returncode == 0:
        res.append(Check("conflict", OK if behind == 0 else INFO,
                         f"no conflict with {cfg['main_branch']}" + (f" ({behind} commits behind)" if behind else "")))
    else:
        res.append(Check("conflict", WARN, "git merge-tree failed (needs git 2.38+)", [r.stderr.strip()]))
    return res


def version(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.split(r"[._]", s))


def fmt(v: tuple[int, ...]) -> str:
    return ".".join(map(str, v))


@dataclass
class MigrationSet:
    name: str
    rx: re.Pattern
    immutable: bool


def migration_sets(cfg: dict) -> list[MigrationSet]:
    res = []
    for i, m in enumerate(cfg["migrations"]):
        pattern = m.get("pattern") or MIGRATION_PRESETS.get(m.get("kind", ""))
        if not pattern:
            raise RuntimeError(f"migrations[{i}]: give a pattern or a kind ({', '.join(MIGRATION_PRESETS)})")
        rx = re.compile(pattern)
        if not {"dir", "ver"} <= set(rx.groupindex):
            raise RuntimeError(f"migrations[{i}]: the pattern needs the named groups (?P<dir>...) and (?P<ver>...)")
        res.append(MigrationSet(m.get("name") or m.get("kind") or f"migrations[{i}]", rx, m.get("immutable", True)))
    return res


def migrations_at(cwd: Path, ref: str, folder: str, rx: re.Pattern) -> dict[tuple[int, ...], set[str]]:
    """version -> distinct migration identities (the `name` group when present, else the file name)."""
    if not ref_exists(cwd, ref):
        return {}
    out = git(cwd, "ls-tree", "-r", "--name-only", ref, "--", folder, check=False)
    res: dict[tuple[int, ...], set[str]] = {}
    for c in out.splitlines():
        m = rx.match(c)
        if m and m["dir"] == folder:
            ident = m["name"] if "name" in rx.groupindex else c.rsplit("/", 1)[1]
            res.setdefault(version(m["ver"]), set()).add(ident)
    return res


def chk_migrations(cwd: Path, cfg: dict, t: Target, files: list[File], open_prs: list[dict]) -> list[Check]:
    sets = migration_sets(cfg)
    if not sets:
        return []
    res = []
    others = [main_ref(cwd, cfg["remote"], b) for b in cfg["compare_branches"]]
    for ms in sets:
        rx = ms.rx
        new = [(f, rx.match(f.path)) for f in files if f.status in "AR" and rx.match(f.path)]
        # a migration that already exists on main and was edited, deleted or renamed
        touched = []
        for f in files:
            path = f.old if f.status == "R" else f.path
            if ms.immutable and f.status in "MDR" and rx.match(path) and \
                    sh(["git", "cat-file", "-e", f"{t.main}:{path}"], cwd, check=False).returncode == 0:
                touched.append(f"{f.status} {path}" + (f" -> {f.path}" if f.status == "R" else ""))
        if touched:
            res.append(Check("migration-edited", BLOCK,
                             f"{ms.name}: a migration already on {cfg['main_branch']} was edited, renamed or deleted",
                             touched + ["databases that already applied it fail validation (checksum or missing "
                                        "version); renumbering needs a manual repair of the history table"]))
        if not new:
            if not touched:
                res.append(Check("migrations", INFO, f"{ms.name}: no new migration"))
            continue

        for folder in sorted({m["dir"] for _, m in new}):
            mine = [(f, m) for f, m in new if m["dir"] == folder]
            on_main = migrations_at(cwd, t.main, folder, rx)
            per_branch = {b: migrations_at(cwd, b, folder, rx) for b in others}
            ceiling = max([*on_main, *(v for d in per_branch.values() for v in d)], default=(0,))
            at_tip = migrations_at(cwd, t.head, folder, rx)
            seen_dup = set()
            det = [f"highest on {cfg['main_branch']}: {fmt(max(on_main, default=(0,)))}"] + \
                  [f"highest on {b}: {fmt(max(d, default=(0,)))}" for b, d in per_branch.items()]
            for f, m in mine:
                v = version(m["ver"])
                fname = f.path.rsplit("/", 1)[1]
                ident = m["name"] if "name" in rx.groupindex else fname
                if v in on_main and ident not in on_main[v]:
                    res.append(Check("migration-number", BLOCK, f"{folder}: {fname} reuses a version {cfg['main_branch']} already has",
                                     det + [f"renumber to {ceiling[0] + 1}"]))
                elif len(at_tip.get(v, ())) > 1:
                    if v not in seen_dup:
                        seen_dup.add(v)
                        res.append(Check("migration-duplicate", BLOCK, f"{folder}: version {fmt(v)} appears twice in this change",
                                         sorted(at_tip[v])))
                elif v in on_main:
                    continue  # the other half of a pair already on main (e.g. .down.sql)
                elif v <= ceiling:
                    res.append(Check("migration-number", BLOCK, f"{folder}: {fname} is not above the highest version already out",
                                     det + ["databases ahead of this branch reject a pending lower version",
                                            f"renumber to {ceiling[0] + 1} (the rule is ABOVE the highest of all compared lines)"]))
                else:
                    res.append(Check("migration-number", OK, f"{folder}: {fname} above the ceiling {fmt(ceiling)}", det))

            # race with other open PRs: two branches pick the same number and git merges both cleanly
            race = []
            my_versions = {version(m["ver"]) for _, m in mine}
            for p in open_prs:
                if p["headRefName"] == t.branch:
                    continue
                ref = f"{cfg['remote']}/{p['headRefName']}"
                if not ref_exists(cwd, ref):
                    continue
                mb = git(cwd, "merge-base", t.main, ref, check=False).strip()
                if not mb:
                    continue
                out = git(cwd, "diff", "--name-only", "--diff-filter=AR", mb, ref, "--", folder, check=False)
                for c in out.splitlines():
                    mm = rx.match(c)
                    if mm and version(mm["ver"]) in my_versions:
                        race.append(f"#{p['number']} {p['headRefName']} also creates {mm['ver']} ({c.rsplit('/', 1)[1]})")
            if race:
                res.append(Check("migration-race", WARN, f"{folder}: another open PR uses the same number",
                                 race + ["git will not flag it: whoever merges second renumbers, and rechecks before pushing"]))
    return res


WRITE_VERBS = (
    r"""\bHttpMethod\.(POST|PUT|PATCH|DELETE)\b|\.(post|put|patch|delete)\s*\(|"""
    r"""['"](POST|PUT|PATCH|DELETE)['"]|-X\s*(POST|PUT|PATCH|DELETE)\b|"""
    r"""@(Post|Put|Patch|Delete)Mapping\b|\bmethod\s*[:=]\s*['"]?(post|put|patch|delete)\b""")


def chk_read_only_apis(cfg: dict, added: dict[str, list[tuple[int, str]]]) -> list[Check]:
    res = []
    for i, api in enumerate(cfg["read_only_apis"]):
        if not api.get("resource"):
            raise RuntimeError(f"read_only_apis[{i}]: resource (a regex) is required")
        name = api.get("name") or f"read_only_apis[{i}]"
        resource = re.compile(api["resource"], re.I)
        write = re.compile(WRITE_VERBS + (f"|{api['write']}" if api.get("write") else ""))
        reason = [api["reason"]] if api.get("reason") else []
        same_line, same_file = [], []
        for path, lines in added.items():
            if is_test(path) or path.endswith(".md"):
                continue
            hits = [n for n, text in lines if resource.search(text)]
            if not hits:
                continue
            same_line += [f"{path}:{n}" for n, text in lines if resource.search(text) and write.search(text)]
            if any(write.search(text) for _, text in lines):
                same_file.append(f"{path}:{hits[0]}")
        if same_line:
            res.append(Check("read-only-api", BLOCK, f"{name}: write call on a read-only API", same_line + reason))
        elif same_file:
            res.append(Check("read-only-api", WARN, f"{name}: file cites the API and also a write verb: check it",
                             same_file + reason))
    return res


STRONG_SECRETS = [
    ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("GitHub token", r"\bgh[pousr]_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b"),
    ("AWS access key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("Google API key", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ("Slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    ("Stripe live key", r"\b[sr]k_live_[A-Za-z0-9]{20,}\b"),
    ("Anthropic API key", r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
    ("OpenAI API key", r"\bsk-(proj-)?[A-Za-z0-9]{32,}\b"),
]
GENERIC_SECRET = re.compile(
    r"""(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|app[_-]?token|client[_-]?secret)\b["']?\s*[:=]\s*["'](?![$<{])[^"'\s]{12,}["']""")
SECRET_FILE = re.compile(r"(^|/)(\.env(\.[a-z0-9]+)?|.*\.(jks|keystore|p12|pfx|pem|key))$")
SAMPLE_FILE = re.compile(r"\.(example|sample|template|dist)$")


def chk_secrets(cfg: dict, files: list[File], added: dict[str, list[tuple[int, str]]]) -> list[Check]:
    sc = cfg["secrets"]
    patterns = [(n, re.compile(p)) for n, p in STRONG_SECRETS] + \
               [(p.get("name", "custom"), re.compile(p["pattern"])) for p in sc.get("patterns", [])]
    ignore = [re.compile(p) for p in sc.get("ignore_paths", [])]
    strong, weak = [], []
    for f in files:
        if f.status in "AR" and SECRET_FILE.search(f.path) and not SAMPLE_FILE.search(f.path) \
                and not any(i.search(f.path) for i in ignore):
            strong.append(f"{f.path} (credential file committed)")
    for path, lines in added.items():
        if any(i.search(path) for i in ignore):
            continue
        for n, text in lines:
            for name, rx in patterns:
                if rx.search(text):
                    strong.append(f"{path}:{n} ({name})")
                    break
            else:
                if GENERIC_SECRET.search(text) and not is_test(path):
                    weak.append(f"{path}:{n}")
    res = []
    if strong:
        res.append(Check("secret", BLOCK, "credential in the diff (value omitted)", strong))
    if weak:
        res.append(Check("secret", WARN, "assignment that looks like a literal password/token: check it", weak))
    return res


def chk_path_flags(cfg: dict, files: list[File]) -> list[Check]:
    res = []
    for i, flag in enumerate(cfg["path_flags"]):
        rx = re.compile(flag["pattern"])
        exclude_tests = flag.get("exclude_tests", True)
        hits = [f.path for f in files if f.status != "D" and rx.search(f.path) and not (exclude_tests and is_test(f.path))]
        if not hits:
            continue
        status = SEVERITY.get(flag.get("severity", "info"), INFO)
        name = flag.get("name") or f"path_flags[{i}]"
        msg = flag.get("message") or "files touched"
        res.append(Check(name, status, f"{len(hits)} file(s): {msg}",
                         hits[:15] + ([f"... and {len(hits) - 15} more"] if len(hits) > 15 else [])))
    return res


# --------------------------------------------------------------------------- stage 2: tests

@dataclass
class Plan:
    label: str
    cwd: str
    cmd: list[str]
    why: str


def nearest(root: Path, path: str, marker: str, stop_at_root: bool = True) -> str | None:
    """Closest ancestor dir of `path` (relative, posix) that holds `marker`."""
    d = Path(path).parent
    while True:
        if (root / d / marker).is_file():
            return d.as_posix() if str(d) != "." else "."
        if str(d) in (".", ""):
            return None
        d = d.parent


def rel_to(root: Path, base: str, paths: list[str]) -> list[str]:
    return [os.path.relpath(root / p, root / base).replace("\\", "/") for p in paths]


def is_aggregator(pom: Path) -> bool:
    try:
        return re.search(r"<packaging>\s*pom\s*</packaging>", pom.read_text(encoding="utf-8", errors="replace")) is not None
    except OSError:
        return False


def plan_maven(root: Path, spec: dict, live: list[str]) -> tuple[list[Plan], list[str]]:
    modules = spec.get("modules")
    guards = spec.get("guards", {})
    by_mod: dict[str, set[str]] = {}
    for c in live:
        if modules is not None:
            mod = next((m for m in modules if c.startswith(m.rstrip("/") + "/")), None)
        else:
            mod = nearest(root, c, "pom.xml")
            # a file outside every module climbs to the parent pom: that is not a Java change
            if mod and is_aggregator(root / mod / "pom.xml") and not c.endswith("pom.xml"):
                mod = None
        if not mod:
            continue
        names = by_mod.setdefault(mod, set())
        if c.endswith((".java", ".kt")) and "/src/test/" in c:
            n = Path(c).stem
            if re.search(r"(Test|Tests|IT)$", n):
                names.add(n)
        elif c.endswith((".java", ".kt")) and "/src/main/" in c:
            n = Path(c).stem
            for test_dir in ("java", "kotlin"):
                d = root / mod / "src" / "test" / test_dir
                if d.is_dir():
                    for t in d.rglob(f"{n}*.*"):
                        if t.suffix in (".java", ".kt") and re.fullmatch(rf"{re.escape(n)}\w*(Test|Tests|IT)", t.stem):
                            names.add(t.stem)
            names.update(guards.get(mod, []))
        for rule in spec.get("when_path", []):
            if re.search(rule["pattern"], c):
                names.update(rule.get("tests", []))
    if not by_mod:
        return [], []
    warns = [] if shutil.which("mvn") else ["mvn not on PATH: Java tests will not run"]
    mods = sorted(by_mod)
    classes = sorted(set().union(*by_mod.values()))
    cmd = ["mvn", "-B", "-ntp", "-q"] + (["-o"] if spec.get("offline") else [])
    if mods != ["."]:
        cmd += ["-pl", ",".join(m for m in mods if m != "."), "-am"]
    cmd += ["test"] + list(spec.get("extra_args", []))
    if classes:
        cmd += [f"-Dtest={','.join(classes)}", "-Dsurefire.failIfNoSpecifiedTests=false"]
    else:
        cmd += ["-DskipTests"]
    why = f"{len(classes)} test class(es) in {', '.join(mods)}" + \
          (" (compile only: no test linked to what changed)" if not classes else "")
    return [Plan(spec.get("name", "maven"), str(root), cmd, why)], warns


def plan_go(root: Path, spec: dict, live: list[str]) -> tuple[list[Plan], list[str]]:
    go = [c for c in live if c.endswith(".go")]
    by_mod: dict[str, list[str]] = {}
    for c in go:
        mod = spec.get("dir") if spec.get("dir") and c.startswith(spec["dir"].rstrip("/") + "/") else \
              (None if spec.get("dir") else nearest(root, c, "go.mod"))
        if mod:
            by_mod.setdefault(mod, []).append(c)
    plans = []
    for mod, files in sorted(by_mod.items()):
        rel = rel_to(root, mod, files)
        pkgs = sorted({"./" + (os.path.dirname(r) or ".") for r in rel})
        cwd = str(root / mod)
        label = spec.get("name", "go") + ("" if len(by_mod) == 1 else f"[{mod}]")
        plans += [Plan(f"{label}-fmt", cwd, ["gofmt", "-l", *rel], "gofmt of the touched files"),
                  Plan(f"{label}-vet", cwd, ["go", "vet", *pkgs], "go vet of the touched packages"),
                  Plan(f"{label}-test", cwd, ["go", "test", *pkgs], "go test of the touched packages")]
    return plans, []


def plan_node(root: Path, spec: dict, live: list[str]) -> tuple[list[Plan], list[str]]:
    app = spec.get("dir")
    if not app:
        raise RuntimeError("tests kind=node needs dir")
    files = [c for c in live if c.startswith(app.rstrip("/") + "/") and re.search(r"\.[cm]?[jt]sx?$", c)]
    if not files:
        return [], []
    base = root / app
    if not (base / "node_modules").exists():
        return [], [f"{app} has no node_modules in this tree: front-end checks will not run "
                    "(install them in the worktree first)"]
    rel = rel_to(root, app, files)
    label = spec.get("name") or Path(app).name
    plans = []
    if spec.get("typecheck", True) and (base / "tsconfig.json").exists():
        plans.append(Plan(f"{label}-tsc", str(base), ["npx", "tsc", "--noEmit"], "project typecheck"))
    if spec.get("lint", True):
        plans.append(Plan(f"{label}-eslint", str(base), ["npx", "eslint", *rel], "eslint of the touched files"))
    runner = spec.get("runner", "jest")
    workers = str(spec.get("max_workers", 2))
    if runner == "jest":
        plans.append(Plan(f"{label}-jest", str(base),
                          ["npx", "jest", "--ci", f"--maxWorkers={workers}", "--passWithNoTests", "--findRelatedTests", *rel],
                          "tests related to the touched files"))
    elif runner == "vitest":
        plans.append(Plan(f"{label}-vitest", str(base), ["npx", "vitest", "related", "--run", "--passWithNoTests", *rel],
                          "tests related to the touched files"))
    return plans, []


def plan_command(root: Path, spec: dict, live: list[str]) -> tuple[list[Plan], list[str]]:
    if not spec.get("when") or not spec.get("cmd"):
        raise RuntimeError("tests kind=command needs when (regex) and cmd (list)")
    files = [c for c in live if re.search(spec["when"], c)]
    if not files:
        return [], []
    cwd = spec.get("cwd", ".")
    rel = rel_to(root, cwd, files)
    cmd: list[str] = []
    for part in spec["cmd"]:
        cmd += rel if part == "{files}" else [part]
    return [Plan(spec.get("name", "command"), str(root / cwd), cmd, spec.get("why", f"{len(files)} matching file(s)"))], []


PLANNERS = {"maven": plan_maven, "go": plan_go, "node": plan_node, "command": plan_command}


def plan_tests(root: Path, cfg: dict, files: list[File]) -> tuple[list[Plan], list[str]]:
    live = [f.path for f in files if f.status != "D"]
    plans, warns = [], []
    for i, spec in enumerate(cfg["tests"]):
        kind = spec.get("kind")
        if kind not in PLANNERS:
            raise RuntimeError(f"tests[{i}]: kind must be one of {', '.join(PLANNERS)}")
        p, w = PLANNERS[kind](root, spec, live)
        plans += p
        warns += w
    return plans, warns


def free_memory_gb() -> float | None:
    try:
        if os.name == "nt":
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory"],
                               capture_output=True, text=True, timeout=30)
            return int(r.stdout.strip()) / 1024 / 1024
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024 / 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


MAVEN_NOISE = re.compile(r"^\[ERROR\]\s*$|Help 1|[Rr]e-run Maven|-rf :|For more information about|"
                         r"See dump files|cwiki\.apache|full stack trace|resume the build|surefire-reports for")


def decode(b: bytes) -> str:
    # on Windows, Maven/Java print in the console code page; everything else in UTF-8
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp1252", errors="replace")


def run_tests(cfg: dict, plans: list[Plan]) -> list[Check]:
    res = []
    free = free_memory_gb()
    floor = float(cfg["min_free_memory_gb"])
    if free is not None and free < floor:
        return [Check("tests", WARN, f"only {free:.1f} GB free (floor {floor:g}): tests postponed so the machine stays up",
                      [" ".join(p.cmd) for p in plans])]
    timeout = int(cfg["test_timeout_minutes"]) * 60
    for p in plans:
        # on Windows mvn and npx are .cmd files: subprocess only finds them through the resolved path
        exe = shutil.which(p.cmd[0])
        if not exe:
            res.append(Check(f"test:{p.label}", WARN, f"{p.cmd[0]} not found on PATH"))
            continue
        try:
            r = subprocess.run([exe, *p.cmd[1:]], cwd=p.cwd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            res.append(Check(f"test:{p.label}", BLOCK, f"exceeded {timeout // 60} min", [" ".join(p.cmd)]))
            continue
        out, err = decode(r.stdout), decode(r.stderr)
        output = (out + "\n" + err).strip()
        failed = r.returncode != 0 or (p.cmd[0] == "gofmt" and out.strip())
        if failed:
            tail = [line for line in output.splitlines() if line.strip() and not MAVEN_NOISE.search(line)][-15:]
            res.append(Check(f"test:{p.label}", BLOCK, f"failed ({p.why})", [" ".join(p.cmd), *tail]))
        else:
            res.append(Check(f"test:{p.label}", OK, p.why))
    return res


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pr", type=int, help="PR number (reviews its remote head)")
    ap.add_argument("--branch", help="branch to review (default: the current one in --dir)")
    ap.add_argument("--dir", default=".", help="tree holding the code (to find and run tests)")
    ap.add_argument("--config", help="path to review-pr.toml")
    ap.add_argument("--run-tests", action="store_true", help="run stage 2 (tests for what the change touches)")
    ap.add_argument("--no-fetch", action="store_true", help="do not update remote refs")
    ap.add_argument("--json", help="also write the result as JSON to this path")
    a = ap.parse_args()

    root = Path(git(Path(a.dir).resolve(), "rev-parse", "--show-toplevel").strip())
    cfg, cfg_path = load_config(root, a.config)
    fetch_error = None if a.no_fetch else fetch(root, cfg["remote"])
    cfg["main_branch"] = cfg["main_branch"] or detect_main(root, cfg["remote"])
    t = resolve_target(root, cfg, a.pr, a.branch)
    files = changed_files(root, t)
    added = added_lines(root, t)
    open_prs = []
    if cfg["migrations"]:  # only the migration race check needs the other open PRs
        open_prs = gh_json(root, "pr", "list", "--state", "open", "--limit", "100",
                           "--json", "number,headRefName,title") or []

    checks: list[Check] = []
    if fetch_error:
        checks.append(Check("fetch", WARN, "fetch failed: remote refs may be stale", [fetch_error[-300:]]))
    if not cfg_path:
        checks.append(Check("config", INFO, "no review-pr.toml: only the generic checks ran"))
    checks += chk_naming(root, cfg, t)
    checks += chk_base_and_conflict(root, cfg, t)
    checks += chk_migrations(root, cfg, t, files, open_prs)
    checks += chk_read_only_apis(cfg, added)
    checks += chk_secrets(cfg, files, added)
    checks += chk_path_flags(cfg, files)

    # tests run in the --dir tree: they only mean something if it sits at the reviewed tip
    plans, plan_warns = plan_tests(root, cfg, files)
    checks += [Check("test-plan", WARN, s) for s in plan_warns]
    at_tip = git(root, "rev-parse", "HEAD").strip() == git(root, "rev-parse", t.head).strip()
    if a.run_tests and plans:
        if not at_tip:
            checks.append(Check("tests", WARN, f"the tree at {root} is not at the tip {t.head}: tests did not run",
                                ["create a detached worktree at the tip and pass --dir"]))
        else:
            dirty = git(root, "status", "--porcelain", "--untracked-files=no").strip()
            if dirty:
                checks.append(Check("tests", WARN, "tree has uncommitted changes: the tests measure more than the change",
                                    dirty.splitlines()[:10]))
            checks += run_tests(cfg, plans)

    blocks = [c for c in checks if c.status == BLOCK]
    warns = [c for c in checks if c.status == WARN]
    result = {
        "target": asdict(t),
        "config": cfg_path,
        "files": len(files),
        "added_lines": sum(len(v) for v in added.values()),
        "verdict": "BLOCKED" if blocks else ("WARNINGS" if warns else "CLEAN"),
        "checks": [asdict(c) for c in checks],
        "test_plan": [asdict(p) for p in plans],
        "tests_ran": bool(a.run_tests and at_tip and plans),
        "reviewer": cfg.get("reviewer", {}),
    }

    print(f"# Deterministic checks: {t.description}")
    print(f"{len(files)} file(s), {result['added_lines']} added line(s), base {t.base[:9]} -> {t.head}"
          + (f", config {cfg_path}" if cfg_path else "") + "\n")
    order = {BLOCK: 0, WARN: 1, INFO: 2, OK: 3}
    for c in sorted(checks, key=lambda c: order[c.status]):
        print(f"[{c.status}] {c.name}: {c.summary}")
        for d in c.details:
            print(f"    - {d}")
    if plans and not result["tests_ran"]:
        print("\nTest plan (run with --run-tests, or by hand):")
        for p in plans:
            print(f"    [{p.label}] (cd {p.cwd}) {' '.join(p.cmd)}")
    print(f"\nVerdict: {result['verdict']} ({len(blocks)} blocker(s), {len(warns)} warning(s))")

    if a.json:
        Path(a.json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 2 if blocks else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
