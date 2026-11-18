"""Smoke tests for skills/review-pr/scripts/check.py against throwaway git repositories.

    python -m unittest discover -s tests -v

Needs git 2.38+ (merge-tree --write-tree). gh is not required: without it the script simply
has no PR data, which is what these tests exercise.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

CHECK = Path(__file__).resolve().parents[1] / "skills" / "review-pr" / "scripts" / "check.py"
MIG = "services/api/src/main/resources/db/migration"

CONFIG = textwrap.dedent(r"""
    main_branch = "main"
    compare_branches = ["develop"]

    [naming]
    branch = '^PROJ-\d+'
    commit = '^(feat|fix): PROJ-\d+ '

    [[migrations]]
    name = "api"
    kind = "flyway"

    [[read_only_apis]]
    name = "payments"
    resource = '/api/payments/'
    reason = "GET only"

    [[path_flags]]
    name = "ci"
    pattern = '^\.github/workflows/'
    severity = "warn"
    exclude_tests = false
""")


def run(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise AssertionError(f"{args} failed: {r.stderr}")
    return r.stdout


class Repo:
    def __init__(self, root: Path):
        self.root = root
        run(root, "git", "init", "-q", "-b", "main")
        run(root, "git", "config", "core.autocrlf", "false")

    def write(self, path: str, text: str = "x\n") -> None:
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def commit(self, msg: str) -> None:
        run(self.root, "git", "add", "-A")
        run(self.root, "git", "commit", "-q", "-m", msg)

    def git(self, *args: str) -> str:
        return run(self.root, "git", *args)

    def check(self) -> dict:
        out = self.root / "result.json"
        r = subprocess.run([sys.executable, "-I", str(CHECK), "--dir", str(self.root), "--no-fetch",
                            "--json", str(out)], capture_output=True, text=True)
        self.assertion_output = r.stdout + r.stderr
        if r.returncode not in (0, 2):
            raise AssertionError(f"check.py exited {r.returncode}:\n{self.assertion_output}")
        data = json.loads(out.read_text(encoding="utf-8"))
        out.unlink()
        return data


def statuses(result: dict) -> dict[str, set[str]]:
    res: dict[str, set[str]] = {}
    for c in result["checks"]:
        res.setdefault(c["name"], set()).add(c["status"])
    return res


@unittest.skipUnless(shutil.which("git"), "git not on PATH")
class CheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(Path(self.tmp.name))
        r = self.repo
        r.write(".claude/review-pr.toml", CONFIG)
        r.write(f"{MIG}/V1__init.sql")
        r.write(f"{MIG}/V2__users.sql")
        r.write("README.md", "hello\n")
        r.commit("feat: PROJ-1 base")
        # a long-lived line that is already ahead of main
        r.git("checkout", "-q", "-b", "develop")
        r.write(f"{MIG}/V3__orders.sql")
        r.commit("feat: PROJ-2 orders")
        r.git("checkout", "-q", "main")

    def tearDown(self):
        self.repo.git("checkout", "-q", "main")
        self.tmp.cleanup()

    def branch(self, name: str):
        self.repo.git("checkout", "-q", "-b", name, "main")

    def test_clean_change_passes(self):
        self.branch("PROJ-10")
        self.repo.write(f"{MIG}/V4__invoices.sql")
        self.repo.commit("feat: PROJ-10 invoices")
        res = self.repo.check()
        s = statuses(res)
        self.assertEqual(res["verdict"], "CLEAN", self.repo.assertion_output)
        self.assertEqual(s["migration-number"], {"OK"})
        self.assertEqual(s["branch"], {"OK"})

    def test_migration_below_the_other_line_blocks(self):
        # V3 is free on main but already taken on develop
        self.branch("PROJ-11")
        self.repo.write(f"{MIG}/V3__payments.sql")
        self.repo.commit("feat: PROJ-11 payments")
        res = self.repo.check()
        self.assertEqual(res["verdict"], "BLOCKED")
        self.assertIn("BLOCK", statuses(res)["migration-number"])

    def test_duplicate_version_in_change_blocks(self):
        self.branch("PROJ-12")
        self.repo.write(f"{MIG}/V5__a.sql")
        self.repo.write(f"{MIG}/V5__b.sql")
        self.repo.commit("feat: PROJ-12 two")
        self.assertIn("BLOCK", statuses(self.repo.check())["migration-duplicate"])

    def test_editing_applied_migration_blocks(self):
        self.branch("PROJ-13")
        self.repo.write(f"{MIG}/V2__users.sql", "changed\n")
        self.repo.commit("fix: PROJ-13 edit")
        self.assertIn("BLOCK", statuses(self.repo.check())["migration-edited"])

    def test_write_on_read_only_api_blocks(self):
        self.branch("PROJ-14")
        self.repo.write("src/client.py", 'requests.post(BASE + "/api/payments/123/refund")\n')
        self.repo.commit("feat: PROJ-14 refund")
        self.assertIn("BLOCK", statuses(self.repo.check())["read-only-api"])

    def test_get_on_read_only_api_passes(self):
        self.branch("PROJ-15")
        self.repo.write("src/client.py", 'requests.get(BASE + "/api/payments/123")\n')
        self.repo.commit("feat: PROJ-15 read")
        self.assertNotIn("read-only-api", statuses(self.repo.check()))

    def test_secret_blocks_without_printing_it(self):
        self.branch("PROJ-16")
        key = "AKIA" + "ABCDEFGHIJKLMNOP"
        self.repo.write("src/settings.py", f'KEY = "{key}"\n')
        self.repo.write(".env", "TOKEN=1\n")
        self.repo.commit("feat: PROJ-16 settings")
        res = self.repo.check()
        self.assertIn("BLOCK", statuses(res)["secret"])
        self.assertNotIn(key, self.repo.assertion_output)
        self.assertNotIn(key, json.dumps(res))

    def test_conflict_with_main_blocks(self):
        self.branch("PROJ-17")
        self.repo.write("README.md", "branch\n")
        self.repo.commit("feat: PROJ-17 readme")
        self.repo.git("checkout", "-q", "main")
        self.repo.write("README.md", "main\n")
        self.repo.commit("feat: PROJ-18 readme")
        self.repo.git("checkout", "-q", "PROJ-17")
        self.assertIn("BLOCK", statuses(self.repo.check())["conflict"])

    def test_naming_and_path_flags_warn(self):
        self.branch("feature/no-ticket")
        self.repo.write(".github/workflows/ci.yml", "on: push\n")
        self.repo.commit("add ci")
        s = statuses(self.repo.check())
        self.assertEqual(s["branch"], {"WARN"})
        self.assertEqual(s["commits"], {"WARN"})
        self.assertEqual(s["ci"], {"WARN"})

    def test_up_down_pair_is_not_a_duplicate(self):
        cfg = CONFIG + textwrap.dedent("""
            [[migrations]]
            name = "worker"
            kind = "golang-migrate"
        """)
        self.repo.write(".claude/review-pr.toml", cfg)
        self.repo.write("worker/migrations/000001_init.up.sql")
        self.repo.write("worker/migrations/000001_init.down.sql")
        self.repo.commit("feat: PROJ-20 worker")
        self.branch("PROJ-21")
        self.repo.write("worker/migrations/000002_jobs.up.sql")
        self.repo.write("worker/migrations/000002_jobs.down.sql")
        self.repo.commit("feat: PROJ-21 jobs")
        s = statuses(self.repo.check())
        self.assertNotIn("migration-duplicate", s)
        self.assertEqual(s["migration-number"], {"OK"})

    def test_command_test_plan_expands_files(self):
        cfg = CONFIG + textwrap.dedent(r"""
            [[tests]]
            kind = "command"
            name = "pytest"
            when = '^backend/.*\.py$'
            cwd = "backend"
            cmd = ["python", "-m", "pytest", "{files}"]
        """)
        self.repo.write(".claude/review-pr.toml", cfg)
        self.repo.commit("feat: PROJ-22 config")
        self.branch("PROJ-23")
        self.repo.write("backend/app/models.py")
        self.repo.write("frontend/x.ts")
        self.repo.commit("feat: PROJ-23 model")
        plan = self.repo.check()["test_plan"]
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["cmd"], ["python", "-m", "pytest", "app/models.py"])

    def test_maven_plan_maps_class_to_test_and_skips_non_java(self):
        cfg = CONFIG + textwrap.dedent("""
            [[tests]]
            kind = "maven"
            guards = { "services/api" = ["ArchitectureTest"] }
        """)
        r = self.repo
        r.write(".claude/review-pr.toml", cfg)
        r.write("pom.xml", "<project><packaging>pom</packaging></project>\n")
        r.write("services/api/pom.xml", "<project><packaging>jar</packaging></project>\n")
        r.write("services/api/src/main/java/a/Foo.java", "class Foo {}\n")
        r.write("services/api/src/test/java/a/FooTest.java", "class FooTest {}\n")
        r.write("services/api/src/test/java/a/FooIntegrationIT.java", "class FooIntegrationIT {}\n")
        r.commit("feat: PROJ-24 maven")
        self.branch("PROJ-25")
        r.write("apps/web/x.ts", "export {}\n")
        r.commit("feat: PROJ-25 front only")
        self.assertEqual(r.check()["test_plan"], [])
        r.write("services/api/src/main/java/a/Foo.java", "class Foo { int x; }\n")
        r.commit("feat: PROJ-25 foo")
        plan = r.check()["test_plan"]
        self.assertEqual(len(plan), 1)
        cmd = " ".join(plan[0]["cmd"])
        self.assertIn("-pl services/api -am", cmd)
        self.assertIn("-Dtest=ArchitectureTest,FooIntegrationIT,FooTest", cmd)

    def test_without_config_only_generic_checks(self):
        (self.repo.root / ".claude" / "review-pr.toml").unlink()
        self.repo.commit("chore: PROJ-19 drop config")
        self.branch("anything")
        self.repo.write(f"{MIG}/V1__dup.sql")
        self.repo.commit("whatever")
        res = self.repo.check()
        s = statuses(res)
        self.assertIn("config", s)
        self.assertNotIn("migration-duplicate", s)
        self.assertNotIn("branch", s)


if __name__ == "__main__":
    unittest.main()
