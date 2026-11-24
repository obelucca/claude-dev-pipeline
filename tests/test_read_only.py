"""Tests for hooks/read_only.py.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "read_only.py"
sys.path.insert(0, str(HOOK.parent))
from read_only import decide  # noqa: E402

ALLOW = {
    "git": [
        "git -C /repo log --oneline -5",
        "git -C /repo diff abc..def -- services/api",
        "git show HEAD:services/api/pom.xml | head -40",
        "git ls-tree -r --name-only origin/main -- db/migration | sed 's|.*/||' | sort -V | tail -3",
        'for b in origin/main origin/develop; do echo "== $b =="; git ls-tree -r --name-only $b -- x | tail -3; done',
        "git grep -n 'Topology' -- '*.java' 2>/dev/null",
        "git log --format='%h > %s' -3",
        "git branch --show-current && git status --short",
        "cd /repo && git worktree list",
        "git log -1 2>&1 | grep -c commit",
        "grep -rn 'Authorization' services/api/src | wc -l",
        "find services -name '*.sql' -newer pom.xml",
        "sed -n 1,80p services/api/src/main/resources/application.yml",
        "git -c core.quotepath=off --no-pager diff --stat HEAD~1",
        "cat a.txt < /dev/null",
    ],
    "qa": [
        "curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/actuator/health",
        "curl -s http://127.0.0.1:8080/v1/home | jq '.items | length'",
        "docker ps --format '{{.Names}}'",
        "docker logs --tail 50 api 2>&1 | grep ERROR",
        "git diff --name-only origin/main...HEAD",
    ],
}

DENY = {
    "git": [
        "touch x",
        "echo hi > file.txt",
        "echo hi >> file.txt",
        "git checkout main",
        "git -C /repo commit -m x",
        "git stash",
        "git branch -D PROJ-1",
        "git fetch origin",
        "git diff --output=x.patch",
        "sed -i 's/a/b/' pom.xml",
        "find . -name '*.tmp' -delete",
        "find . -exec rm {} \\;",
        "rm -rf target",
        "python -c 'print(1)'",
        "cat $(git ls-files | head -1)",
        "cat `ls`",
        "git log | tee out.txt",
        "curl http://localhost:8080/x",
        "xargs rm < list",
        "mvn -q test",
        "npm test",
        "git log -1\nrm -rf x",
        "env rm x",
        "docker ps",
    ],
    "qa": [
        "curl https://api.example.com/orders",
        "curl -s localhost:8080/x -o response.json",
        "curl -X POST http://10.0.0.5:8080/x",
        "curl -O http://localhost:8080/file",
        "docker rm -f api",
        "docker compose down",
        "docker image prune -a",
    ],
}


def run_hook(payload: dict, *args: str) -> str | None:
    r = subprocess.run([sys.executable, "-I", str(HOOK), *args], input=json.dumps(payload),
                       capture_output=True, text=True, check=True)
    if not r.stdout.strip():
        return None
    return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"]


class DecideTest(unittest.TestCase):
    def test_allowed(self):
        for profile, cases in ALLOW.items():
            for cmd in cases:
                with self.subTest(profile=profile, cmd=cmd):
                    self.assertIsNone(decide(cmd, profile))

    def test_denied(self):
        for profile, cases in DENY.items():
            for cmd in cases:
                with self.subTest(profile=profile, cmd=cmd):
                    self.assertIsNotNone(decide(cmd, profile))


class AgentRoutingTest(unittest.TestCase):
    """The plugin hook sees every Bash call: it must only act for the read-only agents."""

    def payload(self, cmd: str, agent: str | None) -> dict:
        p = {"tool_name": "Bash", "tool_input": {"command": cmd}}
        if agent is not None:
            p["agent_type"] = agent
        return p

    def test_main_conversation_untouched(self):
        self.assertIsNone(run_hook(self.payload("rm x", None)))

    def test_other_agents_untouched(self):
        self.assertIsNone(run_hook(self.payload("rm x", "pipeline-dev")))
        self.assertIsNone(run_hook(self.payload("rm x", "Explore")))

    def test_reviewer_denied_with_and_without_plugin_prefix(self):
        for agent in ("pipeline-reviewer", "dev-pipeline:pipeline-reviewer"):
            with self.subTest(agent=agent):
                self.assertEqual(run_hook(self.payload("rm x", agent)), "deny")
                self.assertIsNone(run_hook(self.payload("git log -1", agent)))

    def test_qa_profile_for_checklist_agent(self):
        p = self.payload("curl -s http://localhost:8080/health", "dev-pipeline:pipeline-qa-checklist")
        self.assertIsNone(run_hook(p))
        p = self.payload("curl -s https://example.com", "dev-pipeline:pipeline-qa-checklist")
        self.assertEqual(run_hook(p), "deny")

    def test_forced_profile(self):
        self.assertEqual(run_hook(self.payload("rm x", None), "--profile", "git"), "deny")


if __name__ == "__main__":
    unittest.main()
