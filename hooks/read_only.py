"""Read-only guard for the pipeline's reading agents (PreToolUse on Bash).

Registered once by the plugin (hooks/hooks.json). Every Bash call in the session reaches it; it
looks at `agent_type`, which Claude Code adds to the hook input when the call comes from a
subagent, and only acts for the agents in AGENT_PROFILES. Everything else passes untouched,
including the main conversation.

Why it exists: `tools: Read, Grep, Glob, Bash` does not make an agent read-only, because Bash can
write. This is an ALLOW-LIST: each simple command on the line must be a read, otherwise the call is
denied with the reason, and the agent reports NOT VERIFIED instead of looking for another way.

Profiles:
  git  read utilities + read-only git (log, show, diff, ls-tree, grep, ...)
  qa   the above + curl to localhost only + docker ps/logs/inspect/stats

Standalone use (agents copied into .claude/agents/ instead of installed as a plugin): register
it in .claude/settings.json under PreToolUse/Bash, or pass `--profile git|qa` to force a profile.
"""
import json
import re
import shlex
import sys

# agent name (without the plugin prefix) -> profile. Add your own read-only agents here.
AGENT_PROFILES = {
    "pipeline-architect": "git",
    "pipeline-reviewer": "git",
    "pipeline-security": "git",
    "pipeline-qa-checklist": "qa",
}

READ_TOOLS = {
    "cat", "head", "tail", "wc", "ls", "grep", "egrep", "fgrep", "rg", "sort", "uniq", "cut", "tr",
    "sed", "find", "echo", "printf", "cd", "pwd", "true", "false", "test", "[", "diff", "cmp",
    "basename", "dirname", "realpath", "readlink", "stat", "file", "date", "comm", "column", "nl",
    "jq", "which", "type", "nproc", "uname", "env", "printenv",
}
GIT_READ_TOOLS = {
    "log", "show", "diff", "ls-tree", "ls-files", "grep", "status", "rev-parse", "merge-base",
    "blame", "cat-file", "describe", "shortlog", "rev-list", "name-rev", "for-each-ref",
    "show-ref", "range-diff", "whatchanged", "help", "version", "count-objects",
}
SHELL_WORDS = {"do", "done", "then", "else", "elif", "fi", "!", "{", "}", "time", "while", "until", "if"}
SEPARATORS = {";", "&&", "||", "|", "&", "|&", "(", ")", ";;"}
LOCAL_HOST = re.compile(r"^(localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0)(:\d+)?$", re.I)


class Denied(Exception):
    pass


def prog_name(cmd: str) -> str:
    base = re.split(r"[\\/]", cmd)[-1].lower()
    return base[:-4] if base.endswith(".exe") else base


def words(cmd: str) -> list[str]:
    if "$(" in cmd or "`" in cmd or "<(" in cmd or ">(" in cmd:
        raise Denied("command substitution ($(...), backticks or <(...)) is not allowed")
    lx = shlex.shlex(cmd.replace("\r", "").replace("\n", " ; "), posix=True, punctuation_chars=";&|()<>")
    lx.whitespace_split = True
    lx.commenters = ""
    try:
        return list(lx)
    except ValueError as e:
        raise Denied(f"could not parse the command ({e})")


def simple_commands(toks: list[str]) -> list[list[str]]:
    """Split the line into simple commands, validating redirections on the way."""
    out, cur, i = [], [], 0
    while i < len(toks):
        t = toks[i]
        if t in SEPARATORS:
            if cur:
                out.append(cur)
            cur = []
        elif t in (">", ">>", ">|", "&>", "&>>", ">&"):
            target = toks[i + 1] if i + 1 < len(toks) else ""
            if t == ">&" and target.isdigit():  # 2>&1, >&2: duplicates a descriptor, writes nothing
                pass
            elif t != ">&" and target.lower() in ("/dev/null", "nul", "$null"):
                pass
            else:
                raise Denied(f"redirection to a file ('{t} {target}') writes to disk")
            i += 1
            if cur and cur[-1].isdigit():  # the 2 of 2>/dev/null
                cur.pop()
        elif t in ("<", "<<", "<<<"):
            i += 1  # input: read only
        else:
            cur.append(t)
        i += 1
    if cur:
        out.append(cur)
    return out


def check_git(args: list[str]) -> None:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in ("-C", "-c"):
            i += 2
        else:
            i += 1  # --no-pager, -P, --git-dir=..., --work-tree=...
    if i >= len(args):
        return
    sub, rest = args[i], args[i + 1:]
    if any(a.startswith("--output") for a in rest):
        raise Denied("git with --output writes a file")
    if sub in GIT_READ_TOOLS:
        return
    if sub == "branch" and all(a in ("--show-current", "-a", "-r", "--all", "--remotes", "--list", "-l",
                                     "-v", "-vv", "--contains", "--merged", "--no-merged") or not a.startswith("-")
                               for a in rest) and (not rest or rest[0].startswith("-")):
        return
    if sub == "worktree" and rest[:1] == ["list"]:
        return
    if sub == "stash" and rest[:1] in (["list"], ["show"]):
        return
    if sub == "remote" and (not rest or rest[0] in ("-v", "get-url", "show")):
        return
    if sub == "config" and rest and rest[0] in ("--get", "--get-all", "--get-regexp", "--list", "-l"):
        return
    raise Denied(f"'git {sub}' is not a read (allowed: {', '.join(sorted(GIT_READ_TOOLS))}, "
                 "branch --show-current, worktree list, stash list/show, remote -v, config --get)")


def check_curl(args: list[str]) -> None:
    for j, a in enumerate(args):
        if a in ("-o", "--output") and (j + 1 >= len(args) or args[j + 1] != "/dev/null"):
            raise Denied("curl writing a file (-o) is not allowed; use -o /dev/null")
        if a in ("-O", "--remote-name", "-T", "--upload-file") or a.startswith("--output="):
            raise Denied(f"curl {a} writes or uploads a file")
        target = None
        if "://" in a:
            target = a.split("://", 1)[1]
        elif re.match(r"^[\w.\-\[\]:]+:\d+(/|$)", a):
            target = a
        if target is not None:
            host = re.split(r"[/?#]", target, maxsplit=1)[0].rsplit("@", 1)[-1]
            if not LOCAL_HOST.match(host):
                raise Denied(f"curl is localhost-only in the qa profile (asked for: {host})")


def check(simple: list[str], profile: str) -> None:
    ws = list(simple)
    while ws and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", ws[0]) or ws[0] in SHELL_WORDS):
        ws.pop(0)
    if not ws:
        return
    if ws[0] == "for":  # for X in a b c: the list is not a command
        return
    prog, args = prog_name(ws[0]), ws[1:]
    if prog == "git":
        return check_git(args)
    if profile == "qa" and prog == "curl":
        return check_curl(args)
    if profile == "qa" and prog == "docker":
        sub = [a for a in args if not a.startswith("-")][:2]
        if sub[:1] in (["ps"], ["logs"], ["inspect"], ["stats"]) or sub in (["compose", "ps"], ["compose", "logs"]):
            return
        raise Denied("docker only for ps, logs, inspect and stats in the qa profile")
    if prog not in READ_TOOLS:
        raise Denied(f"'{prog}' is not on the read list of profile {profile}")
    if prog == "sed" and any(a.startswith("-i") or a.startswith("--in-place") for a in args):
        raise Denied("sed -i edits the file")
    if prog == "find" and any(a in ("-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint", "-fprintf", "-fls")
                              for a in args):
        raise Denied("find with -exec/-delete/-fprint is not a read")
    if prog == "env" and [a for a in args if not a.startswith("-") and "=" not in a]:
        raise Denied("env running another command is not allowed")


def decide(cmd: str, profile: str) -> str | None:
    """None = allow; text = why it is denied."""
    try:
        for c in simple_commands(words(cmd)):
            check(c, profile)
    except Denied as e:
        return str(e)
    return None


def profile_for(data: dict) -> str | None:
    if "--profile" in sys.argv:
        return sys.argv[sys.argv.index("--profile") + 1]
    agent = (data.get("agent_type") or "").rsplit(":", 1)[-1]  # dev-pipeline:pipeline-reviewer
    return AGENT_PROFILES.get(agent)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    profile = profile_for(data)
    if profile is None:
        return 0
    cmd = (data.get("tool_input") or {}).get("command") or ""
    reason = decide(cmd, profile)
    if reason is None:
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"Read-only agent (profile {profile}): {reason}. Do not look for another way to do the "
            "same thing: report the item as NOT VERIFIED and name the command you would have needed."
        ),
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
