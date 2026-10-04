"""Pin the PR-base workflow and the wheel version reader.

The required check is a check run the PR-base App posts. This file must not
grant that name to the Actions job token, and it must not post a passing
conclusion. PyYAML is intentionally not used.
"""

import os
import re
import subprocess
import unittest
from pathlib import Path

from test_wheel import _project_version

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "base-is-develop.yml"


def _workflow_text():
    return WORKFLOW.read_text(encoding="utf-8")


def _run_script(text):
    marker = "run: |\n"
    start = text.index(marker) + len(marker)
    lines = text[start:].splitlines()
    if not lines or not lines[0].startswith(" "):
        raise AssertionError("workflow run block is missing")
    indent = len(lines[0]) - len(lines[0].lstrip(" "))
    stripped = []
    for line in lines:
        if line == "":
            stripped.append("")
            continue
        if not line.startswith(" " * indent):
            raise AssertionError("run block lost its indent: %r" % (line,))
        stripped.append(line[indent:])
    return "\n".join(stripped) + "\n"


class ProjectVersionTest(unittest.TestCase):
    def test_reads_project_table(self):
        version = _project_version()
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        project = text.split("[project]", 1)[1].split("\n[", 1)[0]
        self.assertIn('version = "%s"' % version, project)
        self.assertRegex(version, r"^[0-9]+\.[0-9]+\.[0-9]+$")

    def test_wheel_metadata_assertion_is_not_a_hardcoded_version(self):
        source = (ROOT / "tests" / "test_wheel.py").read_text(encoding="utf-8")
        self.assertIn("Version: {_project_version()}", source)
        self.assertNotRegex(source, r"Version: \d+\.\d+\.\d+")


class BaseIsDevelopWorkflowTest(unittest.TestCase):
    def test_trigger_job_and_token_permissions(self):
        text = _workflow_text()
        self.assertIn("pull_request_target:", text)
        self.assertNotRegex(text, r"pull_request:")
        self.assertNotRegex(text, r"(?m)^\s*push:")
        self.assertIn("branches: [main]", text)
        self.assertRegex(text, r"(?m)^  post-base-check:\n")
        self.assertNotRegex(text, r"(?m)^  base-is-develop:\n")
        self.assertRegex(text, r"(?m)^permissions:\n  contents: read\n")
        self.assertEqual(len(re.findall(r"(?m)^permissions:", text)), 1)
        self.assertIn("environment: pr-base", text)
        self.assertNotIn("actions/checkout", text)
        self.assertNotIn("actions/create-github-app-token", text)
        self.assertNotIn("id-token", text)
        # Job token stays read-only. The quoted form is the installation token.
        self.assertNotIn("checks: write", text)
        self.assertNotIn("statuses: write", text)
        self.assertIn('{"permissions":{"checks":"write"}}', text)
        self.assertNotIn("uses:", text)

    def test_posted_conclusion_is_failure_only(self):
        text = _workflow_text()
        self.assertRegex(text, r'"conclusion": "failure"')
        self.assertIn("conclusion=failure", text)
        self.assertNotRegex(text, r'conclusion\s*[=:]\s*[\'"]?success')
        self.assertNotRegex(text, r'"conclusion"\s*:\s*"success"')
        self.assertNotIn("success", text)
        self.assertIn("gh pr create --base develop", text)
        self.assertIn("https://api.github.com/repos/kirich1409/maven-mcp/check-runs", text)
        self.assertIn("https://api.github.com/repos/kirich1409/maven-mcp/installation", text)
        self.assertIn("/access_tokens", text)
        self.assertNotIn("/statuses", text)
        self.assertNotIn("GITHUB_SHA", text)
        self.assertIn("^[0-9a-f]{40}$", text)
        self.assertIn('"openssl", "dgst", "-sha256", "-sign"', text)
        self.assertIn("0600", text)
        self.assertIn("set -euo pipefail", text)
        self.assertNotRegex(text, r"\bset\s+-[A-Za-z]*x")
        self.assertIn("201", text)

    def test_embedded_python_compiles(self):
        script = _run_script(_workflow_text())
        chunks = re.findall(r"<<'PY'\n(.*?\n)PY\n", script, re.S)
        self.assertGreaterEqual(len(chunks), 4)
        for index, chunk in enumerate(chunks):
            compile(chunk, "<base-is-develop:%d>" % index, "exec")

    def test_script_parses_and_refuses_to_post_without_credentials(self):
        script = _run_script(_workflow_text())
        parsed = subprocess.run(
            ["bash", "-n"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(parsed.returncode, 0, parsed.stderr)

        env = os.environ.copy()
        env.update(
            {
                "APP_ID": "",
                "APP_PRIVATE_KEY": "",
                "HEAD_SHA": "a" * 40,
                "BASE_REF": "main",
                "MERGE_SHA": "",
            }
        )
        empty = subprocess.run(
            ["bash"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        self.assertEqual(empty.returncode, 1, empty.stderr)
        self.assertNotIn("success", empty.stdout)
        self.assertNotIn("success", empty.stderr)

        env["APP_ID"] = "1"
        env["APP_PRIVATE_KEY"] = "unused"
        env["HEAD_SHA"] = "not-a-sha"
        bad_sha = subprocess.run(
            ["bash"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        self.assertEqual(bad_sha.returncode, 1, bad_sha.stderr)
        self.assertNotIn("unused", bad_sha.stdout)
        self.assertNotIn("unused", bad_sha.stderr)
