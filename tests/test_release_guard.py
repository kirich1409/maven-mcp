"""Release classification, promote refusals, and wheel checks. No network."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import release_guard as rg  # noqa: E402

SHA = "a" * 40
OTHER = "b" * 40
FROZEN_V110 = "1ae5ca62417826ac1af1e40e26fdbf11348a3ed4"
FROZEN_V100 = "02fa8f5a936b85151041de0f0a524db484c6d2a0"
PYPROJECT = """\
[build-system]
requires = ["hatchling>=1.26.3"]
build-backend = "hatchling.build"

[project]
name = "maven-mcp"
version = "1.2.0"
dependencies = []
"""


def _facts(**overrides: object) -> rg.ClassifyFacts:
    values = {
        "sha": SHA,
        "version": "1.2.0",
        "version_on_main": "1.1.0",
        "main_is_ancestor_of_sha": True,
        "sha_is_ancestor_of_main": False,
        "pypi_status": 404,
        "pypi_body": None,
        "tag_object_sha": None,
        "release_exists": False,
    }
    values.update(overrides)
    return rg.ClassifyFacts(**values)  # type: ignore[arg-type]


def _pypi(
    version: str = "1.2.0",
    *,
    license_expression: str = "MIT",
    license_value: object = None,
    requires_python: str = ">=3.9",
    yanked: bool = False,
    files: tuple = ("bdist_wheel", "sdist"),
    omit_license: bool = False,
) -> dict:
    urls = []
    for kind in files:
        filename = "pkg.whl" if kind == "bdist_wheel" else "pkg.tar.gz"
        urls.append(
            {
                "packagetype": kind,
                "filename": filename,
                "yanked": False,
            }
        )
    info = {
        "version": version,
        "license_expression": license_expression,
        "requires_python": requires_python,
        "yanked": yanked,
    }
    if not omit_license:
        info["license"] = license_value
    return {"info": info, "urls": urls}


def _class(facts: rg.ClassifyFacts) -> str:
    try:
        return rg.classify(facts)
    except rg.GuardError as exc:
        if exc.reason != "conflict":
            raise
        return "conflict"


def _green_checks() -> list:
    runs = []
    for index, name in enumerate(rg.REQUIRED_CHECK_NAMES):
        runs.append(
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
                "started_at": "2024-06-01T00:00:00Z",
                "id": 10 + index,
                "html_url": f"https://example.invalid/checks/{index}",
            }
        )
    return runs


def _spec(facts: rg.ClassifyFacts, **overrides: object) -> rg.GuardSpec:
    values = {
        "github_ref": "refs/heads/main",
        "github_repository": rg.REPO,
        "sha": facts.sha,
        "version": facts.version,
        "object_type": "commit",
        "main_ref_exists": True,
        "develop_ref_exists": True,
        "sha_is_ancestor_of_develop": True,
        "check_versions_sha_ok": True,
        "check_versions_main_ok": True,
        "pyproject_text": PYPROJECT,
        "check_runs": _green_checks(),
        "facts": facts,
    }
    values.update(overrides)
    return rg.GuardSpec(**values)  # type: ignore[arg-type]


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "release-guard@example.com")
    _git(repo, "config", "user.name", "release-guard")
    _git(repo, "config", "commit.gpgsign", "false")


def _commit(repo: Path, message: str, filename: str = "README.md", body: str = "x\n") -> str:
    path = repo / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _make_wheel(
    path: Path,
    *,
    version: str = "1.2.3",
    extra: str = "",
    server: bool = True,
    matrix: bytes = b"matrix-bytes",
    paragraph: str = "Hello release wheel.",
) -> None:
    metadata = (
        "Metadata-Version: 2.1\n"
        "Name: maven-mcp\n"
        f"Version: {version}\n"
        "License-Expression: MIT\n"
        "Requires-Python: >=3.9\n"
        f"{extra}"
        "\n"
        f"{paragraph}\n"
    )
    with zipfile.ZipFile(path, "w") as archive:
        if server:
            archive.writestr("server.py", "print('ok')\n")
        archive.writestr("compat-matrices.json", matrix)
        archive.writestr(f"maven_mcp-{version}.dist-info/METADATA", metadata)


class ClassifyTest(unittest.TestCase):
    def test_noop_bootstrap_does_not_require_version_greater_than_main(self) -> None:
        body = _pypi("1.1.0")
        facts = _facts(
            sha=FROZEN_V110,
            version="1.1.0",
            version_on_main="1.2.0",
            main_is_ancestor_of_sha=False,
            sha_is_ancestor_of_main=True,
            pypi_status=200,
            pypi_body=body,
            tag_object_sha=FROZEN_V110,
            release_exists=True,
        )
        self.assertFalse(rg.version_greater("1.1.0", "1.2.0"))
        self.assertEqual(_class(facts), "noop")

    def test_noop_wins_over_frozen_conflict(self) -> None:
        facts = _facts(
            sha=FROZEN_V110,
            version="1.1.0",
            version_on_main="1.1.0",
            main_is_ancestor_of_sha=False,
            sha_is_ancestor_of_main=True,
            pypi_status=200,
            pypi_body=_pypi("1.1.0"),
            tag_object_sha=FROZEN_V110,
            release_exists=True,
        )
        self.assertEqual(_class(facts), "noop")
        outcome = rg.evaluate(_spec(facts))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.klass, "noop")

    def test_frozen_shas_conflict_when_noop_does_not_match(self) -> None:
        for frozen in (FROZEN_V110, FROZEN_V100):
            facts = _facts(
                sha=frozen,
                version="1.2.0",
                pypi_status=404,
                main_is_ancestor_of_sha=True,
                tag_object_sha=None,
                release_exists=False,
            )
            self.assertEqual(_class(facts), "conflict")

    def test_publish_when_404_and_version_is_greater(self) -> None:
        self.assertEqual(_class(_facts()), "publish")

    def test_404_conflicts_when_version_is_not_greater_tag_exists_or_rule4_fails(self) -> None:
        self.assertEqual(
            _class(_facts(version="1.1.0", version_on_main="1.1.0")),
            "conflict",
        )
        self.assertEqual(_class(_facts(tag_object_sha=SHA)), "conflict")
        self.assertEqual(_class(_facts(main_is_ancestor_of_sha=False)), "conflict")
        self.assertEqual(_class(_facts(release_exists=True)), "conflict")

    def test_missing_file_uploads_only_the_absent_name_and_rule4_failure_conflicts(self) -> None:
        wheel_only = _pypi(files=("bdist_wheel",))
        sdist_only = _pypi(files=("sdist",))
        self.assertEqual(
            _class(_facts(pypi_status=200, pypi_body=wheel_only)),
            "missing-file",
        )
        self.assertEqual(
            rg.filenames_to_upload("missing-file", ["pkg.whl", "pkg.tar.gz"], 200, wheel_only),
            ["pkg.tar.gz"],
        )
        self.assertEqual(
            rg.filenames_to_upload("missing-file", ["pkg.whl", "pkg.tar.gz"], 200, sdist_only),
            ["pkg.whl"],
        )
        self.assertEqual(
            _class(_facts(pypi_status=200, pypi_body=wheel_only, main_is_ancestor_of_sha=False)),
            "conflict",
        )

    def test_repair_github_tag_absent_or_equal_and_conflicts(self) -> None:
        body = _pypi()
        self.assertEqual(
            _class(_facts(pypi_status=200, pypi_body=body, tag_object_sha=None)),
            "repair-github",
        )
        self.assertEqual(
            _class(_facts(pypi_status=200, pypi_body=body, tag_object_sha=SHA)),
            "repair-github",
        )
        self.assertEqual(
            rg.filenames_to_upload("repair-github", ["pkg.whl", "pkg.tar.gz"], 200, body),
            [],
        )
        self.assertEqual(
            _class(_facts(pypi_status=200, pypi_body=body, tag_object_sha=OTHER)),
            "conflict",
        )
        self.assertEqual(
            _class(
                _facts(
                    pypi_status=200,
                    pypi_body=body,
                    tag_object_sha=None,
                    main_is_ancestor_of_sha=False,
                )
            ),
            "conflict",
        )

    def test_metadata_mismatch_on_200_is_conflict(self) -> None:
        mismatched = _pypi(license_expression="Apache-2.0")
        self.assertEqual(
            _class(
                _facts(
                    pypi_status=200,
                    pypi_body=mismatched,
                    tag_object_sha=SHA,
                    release_exists=True,
                    sha_is_ancestor_of_main=True,
                    main_is_ancestor_of_sha=True,
                )
            ),
            "conflict",
        )
        self.assertFalse(rg.metadata_matches(_pypi(license_value="MIT"), "1.2.0"))
        self.assertFalse(rg.metadata_matches(_pypi(yanked=True), "1.2.0"))
        self.assertFalse(rg.metadata_matches(_pypi(omit_license=True), "1.2.0"))

    def test_rule4_is_not_a_precondition_for_noop(self) -> None:
        facts = _facts(
            sha=FROZEN_V110,
            version="1.1.0",
            version_on_main="1.1.0",
            main_is_ancestor_of_sha=False,
            sha_is_ancestor_of_main=True,
            pypi_status=200,
            pypi_body=_pypi("1.1.0"),
            tag_object_sha=FROZEN_V110,
            release_exists=True,
        )
        outcome = rg.evaluate(_spec(facts))
        self.assertEqual(outcome.klass, "noop")
        self.assertNotEqual(outcome.reason, "not-ancestor")

    def test_failed_check_versions_stops_before_publish(self) -> None:
        outcome = rg.evaluate(_spec(_facts(), check_versions_sha_ok=False))
        self.assertFalse(outcome.ok)
        self.assertIsNone(outcome.klass)
        self.assertEqual(outcome.reason, "check-versions-sha")

    def test_build_policy_rejects_dependencies_and_other_backends(self) -> None:
        with_dep = PYPROJECT.replace("dependencies = []", 'dependencies = ["requests"]')
        outcome = rg.evaluate(_spec(_facts(), pyproject_text=with_dep))
        self.assertEqual(outcome.reason, "dependencies")
        other = PYPROJECT.replace(
            'requires = ["hatchling>=1.26.3"]',
            'requires = ["hatchling>=1.26.3", "setuptools"]',
        )
        outcome = rg.evaluate(_spec(_facts(), pyproject_text=other))
        self.assertEqual(outcome.reason, "build-system")


class CheckRunTest(unittest.TestCase):
    def test_empty_set_fails(self) -> None:
        with self.assertRaises(rg.GuardError) as caught:
            rg.require_green_checks([])
        self.assertIn("check-run-missing", caught.exception.reason)

    def test_newer_in_progress_hides_older_success(self) -> None:
        runs = _green_checks()
        for run in runs:
            if run["name"] == "ruff":
                run["started_at"] = "2020-01-01T00:00:00Z"
                run["id"] = 1
        runs.append(
            {
                "name": "ruff",
                "status": "in_progress",
                "conclusion": None,
                "started_at": "2024-01-01T00:00:00Z",
                "id": 2,
            }
        )
        with self.assertRaises(rg.GuardError) as caught:
            rg.require_green_checks(runs)
        self.assertIn("ruff", caught.exception.reason)

    def test_sorts_by_started_at_then_numeric_id(self) -> None:
        # Drop the fixture mypy run so the same-timestamp pair below is the whole set.
        runs = [run for run in _green_checks() if run["name"] != "mypy"]
        runs.append(
            {
                "name": "mypy",
                "status": "completed",
                "conclusion": "success",
                "started_at": "2024-06-01T00:00:00Z",
                "id": 9,
            }
        )
        runs.append(
            {
                "name": "mypy",
                "status": "completed",
                "conclusion": "failure",
                "started_at": "2024-06-01T00:00:00Z",
                "id": 10,
            }
        )
        with self.assertRaises(rg.GuardError):
            rg.require_green_checks(runs)
        later = _green_checks()
        later.append(
            {
                "name": "coverage",
                "status": "completed",
                "conclusion": "failure",
                "started_at": "2024-07-01T00:00:00Z",
                "id": 99,
            }
        )
        chosen = rg.require_green_checks(later)
        self.assertEqual([run["name"] for run in chosen], list(rg.REQUIRED_CHECK_NAMES))


class VersionTest(unittest.TestCase):
    def test_integer_triple_and_prerelease_rejection(self) -> None:
        self.assertTrue(rg.version_greater("1.10.0", "1.9.0"))
        self.assertTrue(rg.version_greater("2.0.0", "1.9.9"))
        self.assertFalse(rg.version_greater("1.2.3", "1.2.3"))
        self.assertFalse(rg.version_greater("1.2.0", "1.2.1"))
        for text in ("1.2.3-rc1", "1.2.3rc1", "v1.2.3", "1.2"):
            self.assertIsNone(rg.parse_version(text))
            self.assertFalse(rg.version_greater(text, "1.2.0"))
        outcome = rg.evaluate(_spec(_facts(version="1.2.3-rc1")))
        self.assertEqual(outcome.reason, "version")
        self.assertIsNone(outcome.klass)


class VprevTest(unittest.TestCase):
    def test_ignores_non_ancestor_and_non_version_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            base = _commit(repo, "base")
            _git(repo, "tag", "v1.0.0", base)
            _git(repo, "tag", "v1.2.0", base)
            _git(repo, "tag", "maven-mcp--v9.0.0", base)
            _git(repo, "tag", "v1.2", base)
            _git(repo, "checkout", "-q", "-b", "side", base)
            side = _commit(repo, "side", body="side\n")
            _git(repo, "tag", "v9.0.0", side)
            _git(repo, "checkout", "-q", "main")
            sha = _commit(repo, "release", body="release\n")
            _git(repo, "tag", "v1.4.0", sha)

            def is_anc(commit: str) -> bool:
                return rg.is_ancestor(repo, commit, sha)

            tags = [
                rg.TagRef("v1.0.0", base),
                rg.TagRef("v1.2.0", base),
                rg.TagRef("v1.4.0", sha),
                rg.TagRef("v9.0.0", side),
                rg.TagRef("maven-mcp--v9.0.0", base),
                rg.TagRef("v1.2", base),
                rg.TagRef("not-a-release", base),
            ]
            self.assertEqual(rg.select_vprev(tags, sha, is_anc), "v1.4.0")
            self.assertFalse(is_anc(side))
            self.assertTrue(is_anc(base))


class NotesTest(unittest.TestCase):
    def test_file_is_published_body(self) -> None:
        decision = rg.notes_decision("1.3.0", "Ship notes.\n", "v1.2.0", "abc subject")
        self.assertFalse(decision.draft)
        self.assertEqual(decision.body, "Ship notes.\n")

    def test_absent_with_vprev_is_draft_skeleton(self) -> None:
        decision = rg.notes_decision("1.3.0", None, "v1.2.0", "abc subject")
        self.assertTrue(decision.draft)
        self.assertIn(
            "https://github.com/kirich1409/maven-mcp/compare/v1.2.0...v1.3.0",
            decision.body,
        )
        self.assertIn("uvx maven-mcp", decision.body)
        self.assertIn("abc subject", decision.body)

    def test_absent_without_vprev_is_empty_draft(self) -> None:
        decision = rg.notes_decision("1.3.0", None, None, "")
        self.assertTrue(decision.draft)
        self.assertEqual(decision.body, "")
        empty = rg.notes_decision("1.3.0", " \n", None, "ignored")
        self.assertTrue(empty.draft)
        self.assertEqual(empty.body, "")


class WheelTest(unittest.TestCase):
    def test_accepts_good_wheel_and_rejects_bad_metadata(self) -> None:
        paragraph = "Hello release wheel."
        readme = f"# Title\n\n{paragraph}\n"
        matrix = b"matrix-bytes"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = root / "good.whl"
            _make_wheel(good, matrix=matrix, paragraph=paragraph)
            rg.inspect_wheel(good, "1.2.3", readme, matrix)

            requires = root / "requires.whl"
            _make_wheel(requires, extra="Requires-Dist: hatchling\n", paragraph=paragraph)
            with self.assertRaises(rg.GuardError) as caught:
                rg.inspect_wheel(requires, "1.2.3", readme, matrix)
            self.assertEqual(caught.exception.reason, "wheel-requires-dist")

            wrong = root / "wrong.whl"
            _make_wheel(wrong, version="9.9.9", paragraph=paragraph)
            with self.assertRaises(rg.GuardError) as caught:
                rg.inspect_wheel(wrong, "1.2.3", readme, matrix)
            self.assertEqual(caught.exception.reason, "wheel-version")

            missing = root / "missing.whl"
            _make_wheel(missing, server=False, paragraph=paragraph)
            with self.assertRaises(rg.GuardError) as caught:
                rg.inspect_wheel(missing, "1.2.3", readme, matrix)
            self.assertEqual(caught.exception.reason, "wheel-server")


class PromoteTest(unittest.TestCase):
    def _plan(self, **overrides: object) -> rg.PromotePlan:
        values = {
            "klass": "publish",
            "sha": SHA,
            "version": "1.2.0",
            "main_sha": OTHER,
            "main_is_ancestor_of_sha": True,
            "tag_object_sha": None,
            "release_exists": False,
            "notes_text": None,
            "vprev": None,
            "log_oneline": "",
        }
        values.update(overrides)
        return rg.promote_plan(**values)  # type: ignore[arg-type]

    def test_refuses_frozen_shas_and_published_tag_names(self) -> None:
        for frozen in (FROZEN_V110, FROZEN_V100):
            with self.assertRaises(rg.GuardError) as caught:
                self._plan(sha=frozen)
            self.assertEqual(caught.exception.reason, "frozen-sha")
        for version in ("1.0.0", "1.1.0"):
            with self.assertRaises(rg.GuardError) as caught:
                self._plan(version=version)
            self.assertEqual(caught.exception.reason, "frozen-tag")
        with self.assertRaises(rg.GuardError) as caught:
            self._plan(klass="noop")
        self.assertEqual(caught.exception.reason, "noop")

    def test_publish_plan_does_not_force_or_delete(self) -> None:
        plan = self._plan(notes_text="Notes only.\n", vprev="v1.1.0")
        self.assertEqual(plan.main, "update")
        self.assertEqual(plan.tag, "create")
        self.assertEqual(plan.release, "published")
        self.assertEqual(plan.body, "Notes only.\n")
        self.assertFalse(plan.force)
        skipped = self._plan(
            main_sha=SHA,
            tag_object_sha=SHA,
            release_exists=True,
        )
        self.assertEqual(skipped.main, "skip")
        self.assertEqual(skipped.tag, "skip")
        self.assertEqual(skipped.release, "skip")
        self.assertEqual(skipped.body, "")


class PollTest(unittest.TestCase):
    def test_sleep_schedule_and_early_stop(self) -> None:
        self.assertEqual(rg.POLL_SLEEPS, [0, 5, 10, 15, 20, 20, 20, 20])
        body = _pypi("1.2.3")
        seen: list = []
        calls = {"n": 0}

        def fetch() -> tuple:
            calls["n"] += 1
            if calls["n"] >= 2:
                return 200, body
            return 404, None

        self.assertTrue(rg.poll_until_match(fetch, "1.2.3", seen.append))
        self.assertEqual(seen, [0, 5])
        self.assertEqual(calls["n"], 2)

        exhausted: list = []

        def always_miss() -> tuple:
            return 404, None

        self.assertFalse(rg.poll_until_match(always_miss, "1.2.3", exhausted.append))
        self.assertEqual(exhausted, rg.POLL_SLEEPS)
        self.assertTrue(rg.publish_output_is_already_exists("HTTP 400: File already exists"))
        self.assertFalse(rg.publish_output_is_already_exists("HTTP 403"))


class GitAncestorTest(unittest.TestCase):
    def test_main_descendant_and_main_ancestor_from_a_temp_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            parent = _commit(repo, "parent", "pyproject.toml", 'version = "1.1.0"\n')
            _git(repo, "checkout", "-q", "-b", "develop")
            child = _commit(repo, "child", "pyproject.toml", 'version = "1.2.0"\n')
            ahead = rg.read_git_state(repo, child)
            self.assertEqual(ahead.object_type, "commit")
            self.assertTrue(ahead.main_is_ancestor_of_sha)
            self.assertFalse(ahead.sha_is_ancestor_of_main)
            self.assertTrue(ahead.sha_is_ancestor_of_develop)
            self.assertEqual(ahead.main_sha, parent)
            publish = _facts(
                sha=child,
                main_is_ancestor_of_sha=ahead.main_is_ancestor_of_sha,
                sha_is_ancestor_of_main=ahead.sha_is_ancestor_of_main,
            )
            self.assertEqual(_class(publish), "publish")

            _git(repo, "checkout", "-q", "-B", "main", child)
            _git(repo, "checkout", "-q", "-B", "develop", parent)
            behind = rg.read_git_state(repo, parent)
            self.assertTrue(behind.sha_is_ancestor_of_main)
            self.assertFalse(behind.main_is_ancestor_of_sha)
            self.assertTrue(behind.sha_is_ancestor_of_develop)
            noop = _facts(
                sha=FROZEN_V110,
                version="1.1.0",
                version_on_main="1.2.0",
                main_is_ancestor_of_sha=behind.main_is_ancestor_of_sha,
                sha_is_ancestor_of_main=behind.sha_is_ancestor_of_main,
                pypi_status=200,
                pypi_body=_pypi("1.1.0"),
                tag_object_sha=FROZEN_V110,
                release_exists=True,
            )
            self.assertEqual(_class(noop), "noop")
            conflict = _facts(
                sha=child,
                main_is_ancestor_of_sha=behind.main_is_ancestor_of_sha,
                sha_is_ancestor_of_main=behind.sha_is_ancestor_of_main,
            )
            self.assertEqual(_class(conflict), "conflict")
            shown = rg.git_show(repo, "refs/heads/main:pyproject.toml")
            self.assertIn('version = "1.2.0"', shown)


class WorkflowContractTest(unittest.TestCase):
    def test_release_workflow_shape(self) -> None:
        text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", text)
        self.assertNotIn("pull_request", text)
        self.assertNotIn("\n  push:", text)
        self.assertIn("group: release", text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("contents: read", text)
        self.assertEqual(text.count("id-token: write"), 1)
        self.assertIn(
            "needs.guard.outputs.class == 'publish' || "
            "needs.guard.outputs.class == 'missing-file' || "
            "needs.guard.outputs.class == 'repair-github'",
            text,
        )
        self.assertIn("needs: [guard, build]", text)
        self.assertIn("needs: [guard, publish]", text)
        self.assertIn("name: pypi", text)
        self.assertIn("url: https://pypi.org/p/maven-mcp", text)
        self.assertIn('UV_PUBLISH_TOKEN: ""', text)
        self.assertIn("uv publish --trusted-publishing always", text)
        self.assertIn("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", text)
        self.assertIn("astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9", text)
        self.assertIn("enable-cache: false", text)
        self.assertIn("uv python install 3.13", text)
        self.assertIn("actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a", text)
        self.assertIn("actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c", text)
        self.assertIn("astral-sh/attest-action@f589a42a7efb6fe400b4f400de60b4bc90390027", text)
        self.assertIn(
            "actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1",
            text,
        )
        self.assertIn("retention-days: 5", text)
        self.assertIn(FROZEN_V110, text)
        self.assertIn(FROZEN_V100, text)
        self.assertNotIn("force=true", text)
        self.assertNotIn("git push", text)
        self.assertIn("persist-credentials: false", text)
        self.assertIn("fetch-depth: 0", text)
        self.assertIn("refs/heads/main:scripts/release_guard.py", text)
        self.assertIn("env -u GH_TOKEN", text)
        self.assertNotIn("cp scripts/release_guard.py", text)


if __name__ == "__main__":
    unittest.main()
