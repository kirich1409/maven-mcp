"""Tool-handler integration tests for the maven-mcp Python server (T-6).

Covers all 10 ``handle_*`` entry points (server.py :1229-:1607) by patching
``urllib.request.urlopen`` with ``mock_urlopen`` and driving the local-scan
handlers through ``temp_project``. Each handler test cites the TypeScript test
it mirrors under ``src/tools/__tests__/``.

Two extra requirements live here:
  * the #263 regression for ``handle_compare_dependency_versions`` — asserts the
    EXACT observable produced by the ``if not latest`` guard (server.py :1299),
    so deleting that guard changes the output, and
  * P3 boundary tests for the dependency-health stat/date helpers
    (``median_days_to_close`` :452, ``_months_since`` :507, ``_parse_iso`` :496,
    ``_summarize_releases`` :523) exercised directly, not only via the handler.
"""

import datetime
import json
import os
import threading
import time
import unittest
import unittest.mock
import urllib.error

from _helpers import (
    http_error,
    mock_gradle_resolve,
    mock_urlopen,
    server,
    temp_project,
    write_fake_gradlew,
)
from fixtures.version_catalogs import RELOCATION_POM


# --- response builders ------------------------------------------------------

def _meta(versions, last_updated="20240101000000"):
    """maven-metadata.xml bytes; the parser only reads <version>/<lastUpdated>."""
    vers = "".join(f"<version>{v}</version>" for v in versions)
    xml = (
        "<metadata><versioning>"
        f"<lastUpdated>{last_updated}</lastUpdated>"
        f"<versions>{vers}</versions>"
        "</versioning></metadata>"
    )
    return xml.encode("utf-8")


def _json(obj):
    return json.dumps(obj).encode("utf-8")


def _osv_batch_bare(vuln_id_lists):
    """Real-shaped /v1/querybatch body: only ``{id, modified}`` per vuln."""
    return _json({
        "results": [
            {"vulns": [{"id": vid, "modified": "2024-01-01T00:00:00Z"} for vid in ids]}
            for ids in vuln_id_lists
        ],
    })


def _osv_vuln_get(vuln):
    """Full /v1/vulns/{id} response body."""
    return _json(vuln)


def _patch_urlopen(responses):
    return unittest.mock.patch("urllib.request.urlopen", side_effect=mock_urlopen(responses))


# --- handle_get_latest_version ----------------------------------------------

class TestGetLatestVersion(unittest.TestCase):
    """mirrors src/tools/__tests__/get-latest-version.test.ts"""

    def test_returns_latest_stable(self):
        with _patch_urlopen([(200, _meta(["1.0.0", "2.0.0"]))]):
            out = server.handle_get_latest_version(
                {"groupId": "com.example", "artifactId": "lib"}
            )
        self.assertEqual(out["latestVersion"], "2.0.0")
        self.assertEqual(out["stability"], "stable")
        self.assertEqual(out["allVersionsCount"], 2)

    def test_prefers_bare_release_over_compat_suffix_build(self):
        # #325 end-to-end repro: handle_get_latest_version must pick "0.8.0",
        # not the same-core "0.8.0-0.6.x-compat" qualifier build.
        with _patch_urlopen([(200, _meta(["0.6.0", "0.7.0", "0.7.1", "0.8.0-0.6.x-compat", "0.8.0"]))]):
            out = server.handle_get_latest_version(
                {"groupId": "org.jetbrains.kotlinx", "artifactId": "kotlinx-datetime"}
            )
        self.assertEqual(out["latestVersion"], "0.8.0")
        self.assertEqual(out["stability"], "stable")

    def test_no_bare_stable_omits_latest_version(self):
        with _patch_urlopen([(200, _meta(["1.0.0-alpha01"]))]):
            out = server.handle_get_latest_version({
                "groupId": "com.example",
                "artifactId": "lib",
            })
        self.assertNotIn("latestVersion", out)
        self.assertNotIn("stability", out)
        self.assertEqual(out["allVersionsCount"], 1)
        self.assertNotIn("error", out)


# --- handle_check_version_exists --------------------------------------------

class TestCheckVersionExists(unittest.TestCase):
    """mirrors src/tools/__tests__/check-version-exists.test.ts"""

    def test_version_present(self):
        with _patch_urlopen([(200, _meta(["1.0.0", "1.1.0"]))]):
            out = server.handle_check_version_exists({
                "groupId": "com.example", "artifactId": "lib", "version": "1.1.0",
            })
        self.assertTrue(out["exists"])
        self.assertEqual(out["repository"], "Maven Central")
        self.assertNotIn("existenceStatus", out)
        self.assertNotIn("error", out)

    def test_version_absent(self):
        with _patch_urlopen([(200, _meta(["1.0.0", "1.1.0"]))]):
            out = server.handle_check_version_exists({
                "groupId": "com.example", "artifactId": "lib", "version": "9.9.9",
            })
        self.assertFalse(out["exists"])
        self.assertNotIn("repository", out)
        self.assertNotIn("existenceStatus", out)
        self.assertNotIn("error", out)


def _dispatch_check_version(arguments):
    return server._dispatch_message({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "check_version_exists", "arguments": arguments},
    })


def _assert_unknown(test, response, *error_parts):
    """A failed lookup is a successful unknown result, not exists false and not isError."""
    test.assertNotIn("error", response)
    result = response["result"]
    test.assertNotIn("isError", result)
    payload = result["structuredContent"]
    test.assertEqual(json.loads(result["content"][0]["text"]), payload)
    test.assertEqual(payload["existenceStatus"], "unknown")
    test.assertNotIn("exists", payload)
    test.assertNotIn("repository", payload)
    test.assertNotIn("resolvedFrom", payload)
    for part in error_parts:
        test.assertIn(part, payload["error"])
    return payload


def _assert_definitive(test, response, exists):
    test.assertNotIn("error", response)
    result = response["result"]
    test.assertNotIn("isError", result)
    payload = result["structuredContent"]
    test.assertIs(payload["exists"], exists)
    test.assertNotIn("existenceStatus", payload)
    test.assertNotIn("error", payload)
    return payload


class TestCheckVersionExistsUnverifiable(unittest.TestCase):
    """tools/call so isError is observable. Fails on the exists-false collapse."""

    def _args(self, root, group_id="com.example", artifact_id="lib", version="1.2.3"):
        return {
            "groupId": group_id,
            "artifactId": artifact_id,
            "version": version,
            "projectPath": root,
        }

    def test_http_500_is_unknown(self):
        errs = [http_error("u", 500, "boom") for _ in range(server.HTTP_MAX_ATTEMPTS)]
        with temp_project({}) as root, \
                unittest.mock.patch("urllib.request.urlopen", side_effect=mock_urlopen(errs)) as opened, \
                unittest.mock.patch.object(server, "_sleep"):
            response = _dispatch_check_version(self._args(root))
        self.assertEqual(opened.call_count, server.HTTP_MAX_ATTEMPTS)
        _assert_unknown(self, response, "HTTP 500", "Maven Central")

    def test_http_401_and_403_are_unknown(self):
        for status in (401, 403):
            with self.subTest(status=status):
                with temp_project({}) as root, unittest.mock.patch(
                    "urllib.request.urlopen",
                    side_effect=mock_urlopen([http_error("u", status, "no")]),
                ) as opened:
                    response = _dispatch_check_version(self._args(root))
                self.assertEqual(opened.call_count, 1)
                _assert_unknown(self, response, "auth required", f"HTTP {status}")

    def test_urlerror_is_unknown_and_drops_exception_text(self):
        token = "password-fragment-must-not-appear"
        errs = [urllib.error.URLError(token) for _ in range(server.HTTP_MAX_ATTEMPTS)]
        with temp_project({}) as root, \
                unittest.mock.patch("urllib.request.urlopen", side_effect=mock_urlopen(errs)) as opened, \
                unittest.mock.patch.object(server, "_sleep"):
            response = _dispatch_check_version(self._args(root))
        self.assertEqual(opened.call_count, server.HTTP_MAX_ATTEMPTS)
        payload = _assert_unknown(self, response, "URLError", "Maven Central")
        self.assertNotIn(token, payload["error"])

    def test_offline_empty_repo_list_is_unknown_without_urlopen(self):
        with temp_project({}) as root:
            env = {
                "MAVEN_MCP_OFFLINE": "1",
                "MAVEN_MCP_SETTINGS": os.path.join(root, "missing-settings.xml"),
            }
            with unittest.mock.patch.dict(os.environ, env, clear=False), \
                    unittest.mock.patch.object(server, "_load_gradle_init_mirrors", return_value=[]), \
                    unittest.mock.patch("urllib.request.urlopen") as opened:
                os.environ.pop("MAVEN_MCP_REPOSITORY_BASE", None)
                response = _dispatch_check_version(self._args(root))
        opened.assert_not_called()
        _assert_unknown(self, response, "no queryable repositories")

    def test_content_filter_empty_list_is_unknown_without_urlopen(self):
        files = {
            "settings.gradle.kts": (
                "dependencyResolutionManagement { repositories { "
                'maven { url = uri("https://jitpack.io"); '
                'content { includeGroup("com.github.foo") } } '
                "} }"
            ),
        }
        with temp_project(files) as root, unittest.mock.patch("urllib.request.urlopen") as opened:
            response = _dispatch_check_version(self._args(root, group_id="com.google.guava", artifact_id="guava"))
        opened.assert_not_called()
        _assert_unknown(self, response, "content/group filtering")

    def test_omitting_200_does_not_hide_sibling_401(self):
        # androidx.core: Google Maven, then Maven Central.
        responses = [
            http_error(server.GOOGLE_MAVEN_URL, 401, "no"),
            (200, _meta(["1.0.0"])),
        ]
        with temp_project({}) as root, unittest.mock.patch(
            "urllib.request.urlopen", side_effect=mock_urlopen(responses)
        ) as opened:
            response = _dispatch_check_version(self._args(root, group_id="androidx.core", artifact_id="core-ktx"))
        self.assertEqual(opened.call_count, 2)
        _assert_unknown(self, response, "auth required", "HTTP 401", "Google Maven")

    def test_404_does_not_overwrite_auth_required_error(self):
        responses = [
            http_error(server.GOOGLE_MAVEN_URL, 401, "no"),
            http_error(server.MAVEN_CENTRAL_URL, 404, "missing"),
        ]
        with temp_project({}) as root, unittest.mock.patch(
            "urllib.request.urlopen", side_effect=mock_urlopen(responses)
        ):
            response = _dispatch_check_version(self._args(root, group_id="androidx.core", artifact_id="core-ktx"))
        payload = _assert_unknown(self, response, "auth required")
        self.assertNotIn("HTTP 404", payload["error"])

    def test_listing_200_stays_exists_true(self):
        responses = [(200, _meta(["1.2.3"])), http_error("u", 404, "no pom")]
        with temp_project({}) as root, unittest.mock.patch(
            "urllib.request.urlopen", side_effect=mock_urlopen(responses)
        ):
            response = _dispatch_check_version(self._args(root))
        payload = _assert_definitive(self, response, True)
        self.assertEqual(payload["repository"], "Maven Central")

    def test_sole_omitting_200_stays_exists_false(self):
        with temp_project({}) as root, unittest.mock.patch(
            "urllib.request.urlopen",
            side_effect=mock_urlopen([(200, _meta(["1.0.0"]))]),
        ) as opened:
            response = _dispatch_check_version(self._args(root))
        self.assertEqual(opened.call_count, 1)
        _assert_definitive(self, response, False)

    def test_every_repo_404_stays_exists_false(self):
        with temp_project({}) as root, unittest.mock.patch(
            "urllib.request.urlopen",
            side_effect=mock_urlopen([http_error("u", 404, "missing")]),
        ) as opened:
            response = _dispatch_check_version(self._args(root))
        self.assertEqual(opened.call_count, 1)
        _assert_definitive(self, response, False)

    def test_unverifiable_then_listing_200_stays_exists_true(self):
        # 401 is not retried. 500 is, so queue HTTP_MAX_ATTEMPTS failures first.
        cases = (
            ("401", [http_error("u", 401, "no"), (200, _meta(["1.2.3"]))]),
            ("500", [http_error("u", 500, "boom") for _ in range(server.HTTP_MAX_ATTEMPTS)] + [(200, _meta(["1.2.3"]))]),
        )
        pom_misses = [http_error("u", 404, "no pom"), http_error("u", 404, "no pom")]
        for label, metadata in cases:
            with self.subTest(label=label), temp_project({}) as root, \
                    unittest.mock.patch(
                        "urllib.request.urlopen",
                        side_effect=mock_urlopen(metadata + pom_misses),
                    ), \
                    unittest.mock.patch.object(server, "_sleep"):
                response = _dispatch_check_version(
                    self._args(root, group_id="androidx.core", artifact_id="core-ktx")
                )
            payload = _assert_definitive(self, response, True)
            self.assertEqual(payload["repository"], "Maven Central")

    def test_google_omit_then_central_hit_reports_central(self):
        responses = [
            (200, _meta(["1.0.0"])),
            (200, _meta(["1.0.0", "1.2.3"])),
            http_error("u", 404, "no pom"),
            http_error("u", 404, "no pom"),
        ]
        with temp_project({}) as root, unittest.mock.patch(
            "urllib.request.urlopen", side_effect=mock_urlopen(responses)
        ):
            response = _dispatch_check_version(
                self._args(root, group_id="androidx.core", artifact_id="core-ktx")
            )
        payload = _assert_definitive(self, response, True)
        self.assertEqual(payload["repository"], "Maven Central")
        self.assertIn("repo1.maven.org", payload["resolvedFrom"]["url"])


class TestCheckVersionExistsSkill(unittest.TestCase):
    def test_skill_distinguishes_miss_from_check_did_not_run(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "plugin", "skills", "check-version-exists", "SKILL.md",
        )
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("unknown means the check did not run", text)
        self.assertIn("existenceStatus", text)
        self.assertIn("means the version was not in the metadata", text)
        self.assertIn("exists: false", text)
        self.assertIn("Do not say the version was not found", text)


# --- handle_check_multiple_dependencies -------------------------------------

class TestCheckMultipleDependencies(unittest.TestCase):
    """mirrors src/tools/__tests__/check-multiple-dependencies.test.ts"""

    def test_resolves_latest(self):
        with _patch_urlopen([(200, _meta(["1.0.0", "1.1.0"]))]):
            out = server.handle_check_multiple_dependencies({
                "dependencies": [{"groupId": "com.example", "artifactId": "lib"}],
            })
        self.assertEqual(out["results"][0]["latestVersion"], "1.1.0")

    def test_failed_fetch_records_error(self):
        # First dep resolves, second 404s -> fetch_metadata raises -> error entry.
        # #400: the per-dependency fan-out now runs on a ThreadPoolExecutor, so
        # urlopen calls are no longer guaranteed to happen in input order --
        # route by URL (which embeds the artifactId) instead of a
        # position-based queue.
        def _route(req, *args, **kwargs):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "good" in url:
                return mock_urlopen([(200, _meta(["1.0.0", "1.1.0"]))])(req, *args, **kwargs)
            return mock_urlopen([http_error(url, 404, "Not Found")])(req, *args, **kwargs)

        with unittest.mock.patch("urllib.request.urlopen", side_effect=_route):
            out = server.handle_check_multiple_dependencies({
                "dependencies": [
                    {"groupId": "com.example", "artifactId": "good"},
                    {"groupId": "com.example", "artifactId": "bad"},
                ],
            })
        by_artifact = {r["artifactId"]: r for r in out["results"]}
        self.assertEqual(by_artifact["good"]["latestVersion"], "1.1.0")
        self.assertNotIn("latestVersion", by_artifact["bad"])
        self.assertIn("error", by_artifact["bad"])
        # RESULT ORDERING is preserved regardless of which thread completed
        # first (#400: _map_parallel is index-mapped, not completion-order).
        self.assertEqual(out["results"][0]["artifactId"], "good")
        self.assertEqual(out["results"][1]["artifactId"], "bad")


# --- handle_compare_dependency_versions (+ #263 regression) -----------------

class TestCompareDependencyVersions(unittest.TestCase):
    """mirrors src/tools/__tests__/compare-dependency-versions.test.ts"""

    def test_263_snapshot_is_a_candidate_beside_a_sibling_minor(self):
        # A published SNAPSHOT newer than the pin is a prerelease candidate.
        # The sibling's newer bare stable is its own row. Neither is an error.
        def _route(req, *args, **kwargs):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if url.endswith(".pom"):
                return mock_urlopen([(404, b"")])(req, *args, **kwargs)
            if "nomatch" in url:
                return mock_urlopen([(200, _meta(["2.0.0-SNAPSHOT"]))])(req, *args, **kwargs)
            return mock_urlopen([(200, _meta(["1.0.0", "1.1.0"]))])(req, *args, **kwargs)

        with unittest.mock.patch("urllib.request.urlopen", side_effect=_route):
            out = server.handle_compare_dependency_versions({
                "dependencies": [
                    {"groupId": "com.example", "artifactId": "nomatch",
                     "currentVersion": "1.0.0"},
                    {"groupId": "com.example", "artifactId": "sibling",
                     "currentVersion": "1.0.0"},
                ],
            })
        by_artifact = {r["artifactId"]: r for r in out["results"]}
        no_match, sibling = by_artifact["nomatch"], by_artifact["sibling"]

        self.assertNotIn("error", no_match)
        self.assertEqual(no_match["candidates"][0]["version"], "2.0.0-SNAPSHOT")
        self.assertEqual(no_match["candidates"][0]["distance"], "prerelease")
        self.assertNotIn("latestVersion", no_match)

        self.assertEqual(sibling["candidates"][0]["version"], "1.1.0")
        self.assertEqual(sibling["candidates"][0]["distance"], "minor")
        self.assertNotIn("error", sibling)
        self.assertEqual(out["summary"], {"total": 2})
        self.assertEqual(out["results"][0]["artifactId"], "nomatch")
        self.assertEqual(out["results"][1]["artifactId"], "sibling")

    def test_up_to_date_reports_empty_candidates_without_error(self):
        with _patch_urlopen([(200, _meta(["1.0.0", "2.0.0"]))]):
            out = server.handle_compare_dependency_versions({
                "dependencies": [
                    {"groupId": "com.example", "artifactId": "uptodate",
                     "currentVersion": "2.0.0"},
                ],
            })
        result = out["results"][0]
        self.assertEqual(result["candidates"], [])
        self.assertNotIn("error", result)
        self.assertNotIn("latestVersion", result)

    def test_relocation_stays_on_the_old_coordinate(self):
        pom = RELOCATION_POM.encode()
        with _patch_urlopen([
            (200, _meta(["1.0.0", "1.2.3"])),
            (200, pom),
        ]):
            out = server.handle_compare_dependency_versions({
                "dependencies": [{
                    "groupId": "com.example",
                    "artifactId": "lib",
                    "currentVersion": "1.0.0",
                }],
            })
        row = out["results"][0]
        self.assertEqual(row["candidates"][0]["version"], "1.2.3")
        self.assertEqual(row["artifactId"], "lib")
        self.assertEqual(row["relocatedTo"], {
            "groupId": "com.example",
            "artifactId": "lib-new",
            "version": "1.2.3",
        })


def _request_url(req) -> str:
    return req.full_url if hasattr(req, "full_url") else str(req)


def _artifact_id_from_metadata_url(url: str) -> str:
    marker = "/maven-metadata.xml"
    if marker not in url:
        return ""
    return url.split(marker, 1)[0].rsplit("/", 1)[-1]


def _issue36_metadata(req, *args, **kwargs):
    """Thread-safe urlopen route. Failures repeat on every retry attempt."""
    aid = _artifact_id_from_metadata_url(_request_url(req))
    if aid == "fail-transport":
        raise urllib.error.URLError("timed out")
    if aid == "fail-auth":
        raise http_error("https://repo1.maven.org/maven2", 401, "Unauthorized")
    if aid == "fail-503":
        raise http_error("https://repo1.maven.org/maven2", 503, "unavailable")
    bodies = {
        "snap-only": ["2.0.0-SNAPSHOT"],
        "empty-list": [],
        "equal-current": ["1.0.0"],
        "minor-bump": ["1.0.0", "1.1.0"],
    }
    if aid not in bodies:
        raise AssertionError("unexpected urlopen: " + _request_url(req))
    return mock_urlopen((200, _meta(bodies[aid])))(req, *args, **kwargs)


def _patched_lookup(side_effect):
    return unittest.mock.patch("urllib.request.urlopen", side_effect=side_effect), \
        unittest.mock.patch.object(server, "_sleep", lambda _delay: None)


def _dep(artifact_id: str, version: str = "1.0.0") -> dict:
    return {
        "groupId": "com.example",
        "artifactId": artifact_id,
        "currentVersion": version,
    }


def _assert_compare_not_checked(test, row, error_part: str) -> None:
    test.assertIn(error_part, row["error"])
    test.assertNotIn("candidates", row)
    test.assertNotIn("latestVersion", row)


class TestCompareFailedLookupIsNotCurrent(unittest.TestCase):
    """#36: a lookup that did not produce a version is not upgradeType none."""

    def test_transport_auth_and_5xx_omit_upgrade_fields(self):
        urlopen_patch, sleep_patch = _patched_lookup(_issue36_metadata)
        with urlopen_patch, sleep_patch:
            out = server.handle_compare_dependency_versions({
                "dependencies": [
                    _dep("fail-transport"),
                    _dep("fail-auth"),
                    _dep("fail-503"),
                ],
            })
        by_artifact = {r["artifactId"]: r for r in out["results"]}
        _assert_compare_not_checked(self, by_artifact["fail-transport"], "URLError")
        _assert_compare_not_checked(self, by_artifact["fail-auth"], "auth required")
        _assert_compare_not_checked(self, by_artifact["fail-503"], "HTTP 503")
        self.assertEqual(out["summary"], {"total": 3})
        self.assertNotIn("resolvedFrom", by_artifact["fail-transport"])

    def test_snapshot_and_empty_list_are_not_metadata_failures(self):
        urlopen_patch, sleep_patch = _patched_lookup(_issue36_metadata)
        with urlopen_patch, sleep_patch:
            out = server.handle_compare_dependency_versions({
                "dependencies": [
                    _dep("snap-only"),
                    _dep("empty-list"),
                ],
            })
        by_artifact = {r["artifactId"]: r for r in out["results"]}
        snap = by_artifact["snap-only"]
        empty = by_artifact["empty-list"]
        self.assertNotIn("error", snap)
        self.assertEqual(snap["candidates"][0]["version"], "2.0.0-SNAPSHOT")
        self.assertEqual(empty["candidates"], [])
        self.assertNotIn("error", empty)
        self.assertNotIn("resolvedFrom", snap)
        self.assertEqual(out["summary"], {"total": 2})

    def test_published_stable_equal_to_current_is_still_no_upgrade(self):
        urlopen_patch, sleep_patch = _patched_lookup(_issue36_metadata)
        with urlopen_patch, sleep_patch:
            out = server.handle_compare_dependency_versions({
                "dependencies": [_dep("equal-current")],
            })
        row = out["results"][0]
        self.assertEqual(row["candidates"], [])
        self.assertNotIn("error", row)
        self.assertNotIn("latestVersion", row)

    def test_sibling_upgrade_counted_error_rows_are_not(self):
        urlopen_patch, sleep_patch = _patched_lookup(_issue36_metadata)
        with urlopen_patch, sleep_patch:
            out = server.handle_compare_dependency_versions({
                "dependencies": [
                    _dep("fail-transport"),
                    _dep("minor-bump"),
                    _dep("snap-only"),
                ],
            })
        by_artifact = {r["artifactId"]: r for r in out["results"]}
        minor = by_artifact["minor-bump"]["candidates"][0]
        self.assertEqual(minor["version"], "1.1.0")
        self.assertEqual(minor["distance"], "minor")
        self.assertIn("error", by_artifact["fail-transport"])
        self.assertNotIn("candidates", by_artifact["fail-transport"])
        self.assertEqual(by_artifact["snap-only"]["candidates"][0]["version"], "2.0.0-SNAPSHOT")
        self.assertEqual(out["summary"], {"total": 3})

    def test_output_schema_does_not_require_upgrade_fields_on_failure(self):
        tool = next(t for t in server.TOOLS if t["name"] == "compare_dependency_versions")
        items = tool["outputSchema"]["properties"]["results"]["items"]
        self.assertNotIn("upgradeType", items["properties"])
        self.assertNotIn("latestVersion", items["properties"])
        self.assertIn("candidates", items["properties"])
        self.assertEqual(tool["outputSchema"]["properties"]["summary"]["required"], ["total"])


def _maven_pom(deps) -> str:
    blocks = "".join(
        "<dependency><groupId>%s</groupId><artifactId>%s</artifactId>"
        "<version>%s</version></dependency>" % (group, artifact, version)
        for group, artifact, version in deps
    )
    return "<project><dependencies>%s</dependencies></project>" % blocks


class TestAuditFailedLookupIsNotCurrent(unittest.TestCase):
    """#36: audit no-match is an error row, not upgradeType none."""

    def test_transport_auth_and_5xx_omit_upgrade_type(self):
        pom = _maven_pom([
            ("com.example", "fail-transport", "1.0.0"),
            ("com.example", "fail-auth", "1.0.0"),
            ("com.example", "fail-503", "1.0.0"),
            ("com.example", "minor-bump", "1.0.0"),
        ])
        urlopen_patch, sleep_patch = _patched_lookup(_issue36_metadata)
        with temp_project({"pom.xml": pom}) as root:
            with urlopen_patch, sleep_patch:
                out = server.handle_audit_project_dependencies({
                    "projectPath": root,
                    "includeVulnerabilities": False,
                })
        by_artifact = {d["artifactId"]: d for d in out["dependencies"]}
        for aid, part in (
            ("fail-transport", "URLError"),
            ("fail-auth", "auth required"),
            ("fail-503", "HTTP 503"),
        ):
            row = by_artifact[aid]
            self.assertIn(part, row["error"])
            self.assertNotIn("candidates", row)
        minor = by_artifact["minor-bump"]["candidates"][0]
        self.assertEqual(minor["version"], "1.1.0")
        self.assertEqual(minor["distance"], "minor")
        self.assertEqual(out["summary"]["upgradeable"], 1)

    def test_snapshot_and_empty_list_are_not_errors(self):
        for artifact, versions in (
            ("snap-only", ["2.0.0-SNAPSHOT"]),
            ("empty-list", []),
        ):
            pom = _maven_pom([("com.example", artifact, "1.0.0")])
            urlopen_patch, sleep_patch = _patched_lookup(_issue36_metadata)
            with temp_project({"pom.xml": pom}) as root:
                with urlopen_patch, sleep_patch:
                    out = server.handle_audit_project_dependencies({
                        "projectPath": root,
                        "includeVulnerabilities": False,
                    })
            self.assertEqual(len(out["dependencies"]), 1)
            row = out["dependencies"][0]
            self.assertNotIn("error", row)
            self.assertNotIn("latestVersion", row)
            self.assertNotIn("resolvedFrom", row)
            if artifact == "snap-only":
                self.assertEqual(row["candidates"][0]["version"], versions[0])
                self.assertEqual(out["summary"]["upgradeable"], 1)
            else:
                self.assertEqual(row["candidates"], [])
                self.assertEqual(out["summary"]["upgradeable"], 0)

    def test_published_stable_equal_to_current_has_no_candidates(self):
        pom = _maven_pom([("com.example", "equal-current", "1.0.0")])
        urlopen_patch, sleep_patch = _patched_lookup(_issue36_metadata)
        with temp_project({"pom.xml": pom}) as root:
            with urlopen_patch, sleep_patch:
                out = server.handle_audit_project_dependencies({
                    "projectPath": root,
                    "includeVulnerabilities": False,
                })
        row = out["dependencies"][0]
        self.assertEqual(row["candidates"], [])
        self.assertNotIn("error", row)
        self.assertNotIn("latestVersion", row)
        self.assertEqual(out["summary"]["upgradeable"], 0)


def _skill_text(name: str) -> str:
    path = os.path.normpath(os.path.join(
        os.path.dirname(server.__file__), "..", "skills", name, "SKILL.md",
    ))
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestCheckDepsDoesNotCallFailedLookupCurrent(unittest.TestCase):
    """#36 skill contract. String checks, not an agent run."""

    def test_check_deps_step2_lists_failed_lookups_as_not_checked(self):
        text = _skill_text("check-deps")
        step2 = text.split("## Step 2", 1)[1].split("## Step 3", 1)[0]
        not_equal = "\u2260"
        old_rule = (
            "(`upgradeType` %s `none`, or `latestVersion` %s `currentVersion`)"
            % (not_equal, not_equal)
        )
        self.assertNotIn(old_rule, text)
        self.assertIn("A row with `error` was not checked", step2)
        self.assertIn("empty `candidates`", step2)
        self.assertIn("not checked", step2)
        self.assertIn("and the error", step2)
        self.assertIn(
            'print "All dependencies up to date." only when the not-checked set is empty',
            step2,
        )
        self.assertIn("no vulnerabilities", step2)
        self.assertIn("still up to date", step2)
        self.assertIn("no `error`", step2)

    def test_compare_skill_does_not_call_a_failed_lookup_no_upgrade(self):
        text = _skill_text("compare-dependency-versions")
        self.assertNotIn(
            "**No upgrade available for any entry:** say so plainly; do not propose edits.",
            text,
        )
        self.assertIn('Do not call it "no upgrade"', text)
        self.assertIn("Show the coordinate and the error", text)
        self.assertIn("empty `candidates`", text)
        self.assertIn("no `error`", text)


# --- handle_get_dependency_changes ------------------------------------------

class TestGetDependencyChanges(unittest.TestCase):
    """mirrors src/tools/__tests__/get-dependency-changes.test.ts"""

    def test_happy_path_github_releases(self):
        # fetch_metadata(1) + fetch_pom(1, github SCM) + gh_fetch_releases(1) = 3.
        pom = (
            "<project><scm><url>https://github.com/acme/widget</url></scm>"
            "</project>"
        )
        releases = [{
            "tag_name": "v1.2.0",
            "html_url": "https://github.com/acme/widget/releases/v1.2.0",
            "body": "release notes",
        }]
        responses = [
            (200, _meta(["1.0.0", "1.1.0", "1.2.0"])),
            (200, pom.encode("utf-8")),
            (200, _json(releases)),
        ]
        with _patch_urlopen(responses):
            out = server.handle_get_dependency_changes({
                "groupId": "com.example", "artifactId": "widget",
                "fromVersion": "1.0.0", "toVersion": "1.2.0",
            })
        self.assertEqual(out["repositoryUrl"], "https://github.com/acme/widget")
        by_version = {c["version"]: c for c in out["changes"]}
        self.assertEqual(set(by_version), {"1.1.0", "1.2.0"})
        # v1.2.0 tag normalized to 1.2.0 and matched -> carries releaseUrl/body.
        self.assertEqual(
            by_version["1.2.0"]["releaseUrl"],
            "https://github.com/acme/widget/releases/v1.2.0",
        )

    def test_repository_not_found(self):
        # POM 404 -> no SCM repo; com.example is not io.github/com.github so the
        # guess path yields nothing -> repositoryNotFound (server.py :603-:604).
        responses = [
            (200, _meta(["1.0.0", "1.1.0", "1.2.0"])),
            http_error("https://repo.example/pom", 404, "Not Found"),
        ]
        with _patch_urlopen(responses):
            out = server.handle_get_dependency_changes({
                "groupId": "com.example", "artifactId": "widget",
                "fromVersion": "1.0.0", "toVersion": "1.2.0",
            })
        self.assertIs(out["repositoryNotFound"], True)


# --- handle_scan_project_dependencies ---------------------------------------

class TestScanProjectDependencies(unittest.TestCase):
    """mirrors src/tools/__tests__/scan-project-dependencies.test.ts"""

    def test_maven_pom_scan(self):
        pom = (
            "<project><dependencies><dependency>"
            "<groupId>com.example</groupId>"
            "<artifactId>lib</artifactId>"
            "<version>1.2.3</version>"
            "</dependency></dependencies></project>"
        )
        with temp_project({"pom.xml": pom}) as root:
            out = server.handle_scan_project_dependencies({"projectPath": root})
        self.assertEqual(out["buildSystem"], "maven")
        deps = {(d["groupId"], d["artifactId"]): d for d in out["dependencies"]}
        self.assertIn(("com.example", "lib"), deps)
        self.assertEqual(deps[("com.example", "lib")]["version"], "1.2.3")

    def test_unknown_build_system(self):
        with temp_project({"README.md": "no build files here"}) as root:
            out = server.handle_scan_project_dependencies({"projectPath": root})
        self.assertEqual(out["buildSystem"], "unknown")
        self.assertEqual(out["dependencies"], [])


# --- handle_get_dependency_vulnerabilities ----------------------------------

class TestGetDependencyVulnerabilities(unittest.TestCase):
    """mirrors src/tools/__tests__/get-dependency-vulnerabilities.test.ts"""

    def test_reports_vulnerability(self):
        # querybatch is bare; severity/fixed come from GET /v1/vulns/{id} (#338).
        batch = _osv_batch_bare([["GHSA-xxxx"]])
        full = {
            "id": "GHSA-xxxx",
            "summary": "bad bug",
            "severity": [{"type": "CVSS_V3", "score": "9.8"}],
            "affected": [{"ranges": [{"type": "ECOSYSTEM", "events": [
                {"introduced": "0"}, {"fixed": "1.2.3"},
            ]}]}],
            "references": [{"type": "ADVISORY", "url": "https://advisory/x"}],
        }
        with _patch_urlopen([(200, batch), (200, _osv_vuln_get(full))]):
            out = server.handle_get_dependency_vulnerabilities({
                "dependencies": [
                    {"groupId": "com.example", "artifactId": "lib", "version": "1.0.0"},
                ],
            })
        result = out["results"][0]
        self.assertEqual(result["vulnerabilityCount"], 1)
        self.assertEqual(result["vulnerabilities"][0]["severity"], "CRITICAL")
        self.assertEqual(result["vulnerabilities"][0]["fixedVersion"], "1.2.3")

    def test_osv_non_200_yields_no_vulns(self):
        with _patch_urlopen([(500, b"")]):
            out = server.handle_get_dependency_vulnerabilities({
                "dependencies": [
                    {"groupId": "com.example", "artifactId": "lib", "version": "1.0.0"},
                ],
            })
        self.assertEqual(out["results"][0]["vulnerabilityCount"], 0)

    def test_resolves_plugin_marker_to_implementation_before_osv_query(self):
        # #290: the marker POM (1) is fetched and resolved to the implementation
        # GAV before the OSV batch query (2). Result keeps the ORIGINAL marker
        # identity and surfaces the resolved implementation separately.
        marker_pom = (
            "<project><dependencies><dependency>"
            "<groupId>com.example</groupId>"
            "<artifactId>foo-impl</artifactId>"
            "<version>1.2.3</version>"
            "</dependency></dependencies></project>"
        ).encode()
        batch = _osv_batch_bare([["GHSA-yyyy"]])
        full = {
            "id": "GHSA-yyyy",
            "summary": "bad bug in impl",
            "severity": [{"type": "CVSS_V3", "score": "9.8"}],
            "affected": [{"ranges": [{"type": "ECOSYSTEM", "events": [
                {"introduced": "0"}, {"fixed": "1.2.4"},
            ]}]}],
            "references": [{"type": "ADVISORY", "url": "https://advisory/y"}],
        }
        with temp_project({"README.md": "no build files"}) as root:
            with _patch_urlopen([
                (200, marker_pom),
                (200, batch),
                (200, _osv_vuln_get(full)),
            ]):
                out = server.handle_get_dependency_vulnerabilities({
                    "projectPath": root,
                    "dependencies": [
                        {
                            "groupId": "com.example.foo",
                            "artifactId": "com.example.foo.gradle.plugin",
                            "version": "1.0.0",
                        },
                    ],
                })
        result = out["results"][0]
        self.assertEqual(result["groupId"], "com.example.foo")
        self.assertEqual(result["artifactId"], "com.example.foo.gradle.plugin")
        self.assertEqual(result["version"], "1.0.0")
        self.assertEqual(
            result["resolvedImplementation"],
            {"groupId": "com.example", "artifactId": "foo-impl", "version": "1.2.3"},
        )
        self.assertEqual(result["vulnerabilityCount"], 1)
        self.assertEqual(result["vulnerabilities"][0]["fixedVersion"], "1.2.4")

    def test_non_marker_dependency_shape_unchanged(self):
        # Regression: a normal dependency makes OSV POST + one hydration GET
        # (no POM fetch) and the result carries no resolvedImplementation key.
        batch = _osv_batch_bare([["GHSA-xxxx"]])
        full = {
            "id": "GHSA-xxxx",
            "summary": "bad bug",
            "severity": [{"type": "CVSS_V3", "score": "9.8"}],
            "affected": [{"ranges": [{"type": "ECOSYSTEM", "events": [
                {"introduced": "0"}, {"fixed": "1.2.3"},
            ]}]}],
            "references": [{"type": "ADVISORY", "url": "https://advisory/x"}],
        }
        with _patch_urlopen([(200, batch), (200, _osv_vuln_get(full))]) as m:
            out = server.handle_get_dependency_vulnerabilities({
                "dependencies": [
                    {"groupId": "com.example", "artifactId": "lib", "version": "1.0.0"},
                ],
            })
        result = out["results"][0]
        self.assertEqual(m.call_count, 2)  # POST querybatch + GET /v1/vulns
        self.assertNotIn("resolvedImplementation", result)
        self.assertEqual(result["vulnerabilityCount"], 1)
        self.assertEqual(result["vulnerabilities"][0]["fixedVersion"], "1.2.3")

    def test_non_marker_request_skips_resolution_context(self):
        # #290 purity fix: discover_repositories does filesystem I/O reading the
        # project's build files. A request with no plugin-marker-shaped
        # dependency must never trigger it — patch it to raise if called.
        osv = {"results": [{"vulns": []}]}
        with unittest.mock.patch(
            "server.discover_repositories",
            side_effect=AssertionError(
                "discover_repositories must not be called for non-marker requests"
            ),
        ):
            with _patch_urlopen([(200, _json(osv))]):
                out = server.handle_get_dependency_vulnerabilities({
                    "dependencies": [
                        {"groupId": "com.example", "artifactId": "lib", "version": "1.0.0"},
                    ],
                })
        self.assertEqual(out["results"][0]["vulnerabilityCount"], 0)

    def test_resolution_context_failure_degrades_gracefully(self):
        # A marker-shaped request whose resolution-context construction fails
        # (e.g. an unreadable/malformed build file) must not raise — the marker
        # is treated as unresolved and queried against OSV as-is.
        osv = {"results": [{"vulns": []}]}
        with unittest.mock.patch(
            "server.discover_repositories", side_effect=OSError("malformed build file"),
        ):
            with _patch_urlopen([(200, _json(osv))]) as m:
                out = server.handle_get_dependency_vulnerabilities({
                    "dependencies": [
                        {
                            "groupId": "com.example.foo",
                            "artifactId": "com.example.foo.gradle.plugin",
                            "version": "1.0.0",
                        },
                    ],
                })
        result = out["results"][0]
        self.assertNotIn("resolvedImplementation", result)
        self.assertEqual(m.call_count, 1)  # only the OSV POST, no POM fetch attempted
        self.assertEqual(result["vulnerabilityCount"], 0)

    def test_safe_upgrade_synthesized_from_single_fixed_version(self):
        # #412: exactly one known CVE with a fixed version -> that version IS
        # the minimum clearing every known vulnerability.
        batch = _osv_batch_bare([["GHSA-xxxx"]])
        full = {
            "id": "GHSA-xxxx",
            "summary": "bad bug",
            "severity": [{"type": "CVSS_V3", "score": "9.8"}],
            "affected": [{"ranges": [{"type": "ECOSYSTEM", "events": [
                {"introduced": "0"}, {"fixed": "1.2.3"},
            ]}]}],
            "references": [{"type": "ADVISORY", "url": "https://advisory/x"}],
        }
        with _patch_urlopen([(200, batch), (200, _osv_vuln_get(full))]):
            out = server.handle_get_dependency_vulnerabilities({
                "dependencies": [
                    {"groupId": "com.example", "artifactId": "lib", "version": "1.0.0"},
                ],
            })
        result = out["results"][0]
        self.assertEqual(
            result["safeUpgrade"], {"version": "1.2.3", "fixesAllKnown": True}
        )

    def test_safe_upgrade_takes_highest_fixed_across_multiple_cves(self):
        # #412: two CVEs on the same coordinate, fixed at different versions ->
        # the safe target is the HIGHEST of the two (clears both at once).
        batch = _osv_batch_bare([["GHSA-aaaa", "GHSA-bbbb"]])
        low_fix = {
            "id": "GHSA-aaaa", "summary": "bug A",
            "severity": [{"type": "CVSS_V3", "score": "7.5"}],
            "affected": [{"ranges": [{"type": "ECOSYSTEM", "events": [
                {"introduced": "0"}, {"fixed": "1.2.3"},
            ]}]}],
            "references": [],
        }
        high_fix = {
            "id": "GHSA-bbbb", "summary": "bug B",
            "severity": [{"type": "CVSS_V3", "score": "9.8"}],
            "affected": [{"ranges": [{"type": "ECOSYSTEM", "events": [
                {"introduced": "0"}, {"fixed": "1.5.0"},
            ]}]}],
            "references": [],
        }
        with _patch_urlopen([
            (200, batch),
            (200, _osv_vuln_get(low_fix)),
            (200, _osv_vuln_get(high_fix)),
        ]):
            out = server.handle_get_dependency_vulnerabilities({
                "dependencies": [
                    {"groupId": "com.example", "artifactId": "lib", "version": "1.0.0"},
                ],
            })
        result = out["results"][0]
        self.assertEqual(
            result["safeUpgrade"], {"version": "1.5.0", "fixesAllKnown": True}
        )

    def test_safe_upgrade_cannot_synthesize_when_a_cve_has_no_fixed_version(self):
        # #412: one CVE has a known fix, the other has none -> no single
        # version can be certified to clear BOTH, so no version is surfaced.
        batch = _osv_batch_bare([["GHSA-aaaa", "GHSA-cccc"]])
        has_fix = {
            "id": "GHSA-aaaa", "summary": "bug A",
            "affected": [{"ranges": [{"type": "ECOSYSTEM", "events": [
                {"introduced": "0"}, {"fixed": "1.2.3"},
            ]}]}],
            "references": [],
        }
        no_fix = {
            "id": "GHSA-cccc", "summary": "bug C, unfixed",
            "affected": [{"ranges": [{"type": "ECOSYSTEM", "events": [
                {"introduced": "0"},
            ]}]}],
            "references": [],
        }
        with _patch_urlopen([
            (200, batch),
            (200, _osv_vuln_get(has_fix)),
            (200, _osv_vuln_get(no_fix)),
        ]):
            out = server.handle_get_dependency_vulnerabilities({
                "dependencies": [
                    {"groupId": "com.example", "artifactId": "lib", "version": "1.0.0"},
                ],
            })
        result = out["results"][0]
        self.assertFalse(result["safeUpgrade"]["fixesAllKnown"])
        self.assertNotIn("version", result["safeUpgrade"])
        self.assertIn("GHSA-cccc", result["safeUpgrade"]["reason"])

    def test_no_safe_upgrade_key_when_zero_vulnerabilities(self):
        # #412: nothing to synthesize when the coordinate has no known CVEs.
        with _patch_urlopen([(500, b"")]):
            out = server.handle_get_dependency_vulnerabilities({
                "dependencies": [
                    {"groupId": "com.example", "artifactId": "lib", "version": "1.0.0"},
                ],
            })
        self.assertNotIn("safeUpgrade", out["results"][0])

    def test_caps_truncate_dependencies_over_100(self):
        # 101 deps -> the HANDLER truncates to 100 before any I/O (same pattern
        # as verify_coordinates). Mock query_osv_batch to assert it only sees 100.
        deps = [
            {"groupId": "com.x", "artifactId": f"a{i}", "version": "1.0.0"}
            for i in range(101)
        ]
        with unittest.mock.patch.object(
            server, "query_osv_batch",
            side_effect=lambda ds: [{**d, "vulnerabilities": []} for d in ds],
        ) as m:
            out = server.handle_get_dependency_vulnerabilities({"dependencies": deps})
        self.assertEqual(len(out["results"]), 100)
        m.assert_called_once()
        self.assertEqual(len(m.call_args.args[0]), 100)


# --- handle_get_dependency_health -------------------------------------------

class TestGetDependencyHealth(unittest.TestCase):
    """mirrors src/tools/__tests__/get-dependency-health.test.ts"""

    def test_full_github_signals(self):
        # Call sequence (all must be 200 + valid JSON or the chain short-circuits):
        # metadata(1) + pom(1) + depsdev-scorecard(1, #411) + gh_fetch_repo(1)
        # + gh_fetch_releases(1) + gh_fetch_issue_stats(3) + gh_fetch_user(1) = 9.
        pom = (
            "<project><scm><url>https://github.com/acme/widget</url></scm>"
            "<licenses><license><name>Apache-2.0</name></license></licenses>"
            "</project>"
        )
        repo = {
            "stargazers_count": 100, "forks_count": 10, "open_issues_count": 5,
            "archived": False,
            "owner": {"login": "acme", "type": "Organization"},
            "pushed_at": "2024-06-01T00:00:00Z",
            "license": {"spdx_id": "Apache-2.0"},
            "created_at": "2020-01-01T00:00:00Z",
        }
        scorecard_project = {
            "projectKey": {"id": "github.com/acme/widget"},
            "scorecard": {
                "date": "2026-07-06T00:00:00Z",
                "repository": {"name": "github.com/acme/widget", "commit": "abc123"},
                "scorecard": {"version": "v5.5.1", "commit": "def456"},
                "overallScore": 8.5,
                "checks": [
                    {
                        "name": "Maintained",
                        "documentation": {"shortDescription": "x", "url": "https://x"},
                        "score": 10,
                        "reason": "19 commit(s) in the last 90 days",
                        "details": [],
                    },
                ],
                "metadata": [],
            },
        }
        responses = [
            (200, _meta(["1.0.0"])),
            (200, pom.encode("utf-8")),
            (200, _json(scorecard_project)),         # deps.dev GetProject (#411)
            (200, _json(repo)),
            (200, _json([])),                       # releases
            (200, _json({"total_count": 5})),       # issues open
            (200, _json({"total_count": 20})),      # issues closed
            (200, _json({"items": []})),            # median items
            (200, _json({"public_repos": 50, "created_at": "2015-01-01T00:00:00Z"})),
        ]
        with _patch_urlopen(responses):
            out = server.handle_get_dependency_health({
                "dependencies": [{"groupId": "com.example", "artifactId": "widget"}],
            })
        result = out["results"][0]
        self.assertEqual(result["repository"]["owner"], "acme")
        self.assertIsNotNone(result["github"])
        self.assertEqual(result["github"]["stars"], 100)
        self.assertEqual(result["github"]["license"], "Apache-2.0")
        # #411: OpenSSF Scorecard surfaced, trimmed to name/score/reason per check.
        self.assertEqual(result["scorecard"]["overallScore"], 8.5)
        self.assertEqual(result["scorecard"]["generatedBy"], "OpenSSF")
        self.assertEqual(
            result["scorecard"]["checks"][0],
            {"name": "Maintained", "score": 10, "reason": "19 commit(s) in the last 90 days"},
        )

    def test_scorecard_absent_degrades_without_breaking_github_signals(self):
        # #411: deps.dev 404 "project not found" (empirically the common case
        # for a repo it has not indexed) must not affect the surrounding
        # GitHub metrics — the scorecard field is simply omitted.
        pom = (
            "<project><scm><url>https://github.com/acme/widget</url></scm>"
            "<licenses><license><name>Apache-2.0</name></license></licenses>"
            "</project>"
        )
        repo = {
            "stargazers_count": 100, "forks_count": 10, "open_issues_count": 5,
            "archived": False,
            "owner": {"login": "acme", "type": "Organization"},
            "pushed_at": "2024-06-01T00:00:00Z",
            "license": {"spdx_id": "Apache-2.0"},
            "created_at": "2020-01-01T00:00:00Z",
        }
        responses = [
            (200, _meta(["1.0.0"])),
            (200, pom.encode("utf-8")),
            (404, b"project not found"),             # deps.dev GetProject (#411)
            (200, _json(repo)),
            (200, _json([])),                       # releases
            (200, _json({"total_count": 5})),       # issues open
            (200, _json({"total_count": 20})),      # issues closed
            (200, _json({"items": []})),            # median items
            (200, _json({"public_repos": 50, "created_at": "2015-01-01T00:00:00Z"})),
        ]
        with _patch_urlopen(responses):
            out = server.handle_get_dependency_health({
                "dependencies": [{"groupId": "com.example", "artifactId": "widget"}],
            })
        result = out["results"][0]
        self.assertNotIn("scorecard", result)
        self.assertEqual(result["github"]["stars"], 100)

    def test_no_github_repo_health_error(self):
        # metadata(1) + pom 404 -> None(1); guess fails for com.example -> no repo.
        responses = [
            (200, _meta(["1.0.0"])),
            http_error("https://repo.example/pom", 404, "Not Found"),
        ]
        with _patch_urlopen(responses):
            out = server.handle_get_dependency_health({
                "dependencies": [{"groupId": "com.example", "artifactId": "widget"}],
            })
        result = out["results"][0]
        self.assertIsNone(result["github"])
        self.assertIn("healthError", result)

    def test_fan_out_bounded_to_max_github_workers(self):
        # R2c (perf review of #400): this fan-out makes SEVERAL api.github.com
        # calls per dependency (gh_repo_exists/fetch_repo/fetch_releases/
        # issue-stats/discover, plus the rate-limited Search API), and
        # GitHub's secondary rate limiter specifically penalizes CONCURRENT
        # requests -- it must run at MAX_GITHUB_PARALLEL_FETCHES, a smaller
        # dedicated bound, not the default MAX_PARALLEL_FETCHES (8) every
        # other fan-out uses. fetch_metadata is the ONLY mock needed: it
        # tracks peak concurrency, sleeps to widen the race window, then
        # raises so _check_one short-circuits before any GitHub call (the
        # bound on the FAN-OUT itself is what's under test, not what each
        # dep's body happens to do afterward).
        lock = threading.Lock()
        state = {"current": 0, "peak": 0}

        def _tracking_fetch_metadata(group_id, artifact_id, ctx):
            with lock:
                state["current"] += 1
                state["peak"] = max(state["peak"], state["current"])
            time.sleep(0.03)
            with lock:
                state["current"] -= 1
            raise ValueError("boom")

        deps = [{"groupId": "com.example", "artifactId": f"lib{i}"} for i in range(8)]
        with unittest.mock.patch.object(
            server, "fetch_metadata", side_effect=_tracking_fetch_metadata,
        ):
            out = server.handle_get_dependency_health({"dependencies": deps})

        self.assertEqual(len(out["results"]), 8)
        for r in out["results"]:
            self.assertIn("healthError", r)
        self.assertLessEqual(state["peak"], server.MAX_GITHUB_PARALLEL_FETCHES)
        # Sanity: real overlap was actually attempted, so the assertion above
        # meaningfully exercises the bound rather than being vacuously true.
        self.assertGreater(state["peak"], 1)


# --- handle_search_artifacts ------------------------------------------------

class TestSearchArtifacts(unittest.TestCase):
    """mirrors src/tools/__tests__/search-artifacts.test.ts"""

    def test_returns_results(self):
        body = {"response": {"docs": [
            {"g": "com.example", "a": "lib", "latestVersion": "1.0.0", "versionCount": 3},
        ]}}
        with _patch_urlopen([(200, _json(body))]):
            out = server.handle_search_artifacts({"query": "lib"})
        self.assertEqual(out["results"][0]["groupId"], "com.example")
        self.assertEqual(out["results"][0]["latestVersion"], "1.0.0")

    def test_non_200_yields_empty(self):
        # 503 is retryable (_is_retryable_status), so _request_with_retry
        # exhausts HTTP_MAX_ATTEMPTS before returning the definitive (503, b"")
        # this test exercises -- queue one response per attempt and patch
        # _sleep so the retry backoff does not slow the test.
        with unittest.mock.patch.object(server, "_sleep"), \
                _patch_urlopen([(503, b"")] * server.HTTP_MAX_ATTEMPTS):
            out = server.handle_search_artifacts({"query": "lib"})
        self.assertEqual(out["results"], [])

    def test_clamps_limit_over_max(self):
        # limit=500 -> handler clamps to SEARCH_LIMIT_MAX before the Solr URL.
        body = {"response": {"docs": []}}
        with _patch_urlopen([(200, _json(body))]) as m:
            server.handle_search_artifacts({"query": "lib", "limit": 500})
        url = m.call_args_list[0].args[0].full_url
        self.assertIn(f"rows={server.SEARCH_LIMIT_MAX}", url)

    def test_clamps_limit_below_one_and_rejects_non_int(self):
        # 0 / negative -> 1; non-int -> SEARCH_LIMIT_DEFAULT. Three separate
        # handle_search_artifacts calls -> three separate network calls ->
        # one queued 200 response each.
        body = {"response": {"docs": []}}
        with _patch_urlopen([(200, _json(body))] * 3) as m:
            server.handle_search_artifacts({"query": "lib", "limit": 0})
            server.handle_search_artifacts({"query": "lib", "limit": -5})
            server.handle_search_artifacts({"query": "lib", "limit": "nope"})
        self.assertIn("rows=1", m.call_args_list[0].args[0].full_url)
        self.assertIn("rows=1", m.call_args_list[1].args[0].full_url)
        self.assertIn(
            f"rows={server.SEARCH_LIMIT_DEFAULT}",
            m.call_args_list[2].args[0].full_url,
        )


# --- handle_audit_project_dependencies --------------------------------------

class TestAuditProjectDependencies(unittest.TestCase):
    """mirrors src/tools/__tests__/audit-project-dependencies.test.ts"""

    def test_maven_audit_with_upgrade(self):
        pom = (
            "<project><dependencies><dependency>"
            "<groupId>com.example</groupId>"
            "<artifactId>lib</artifactId>"
            "<version>1.0.0</version>"
            "</dependency></dependencies></project>"
        )
        # scan (local) -> fetch_metadata -> relocation POM -> query_osv_batch POST.
        responses = [
            (200, _meta(["1.0.0", "1.1.0"])),
            (200, b"<project></project>"),
            (200, _json({"results": [{"vulns": []}]})),
        ]
        with temp_project({"pom.xml": pom}) as root:
            with _patch_urlopen(responses):
                out = server.handle_audit_project_dependencies({"projectPath": root})
        self.assertEqual(out["buildSystem"], "maven")
        dep = out["dependencies"][0]
        self.assertEqual(dep["candidates"][0]["version"], "1.1.0")
        self.assertEqual(dep["candidates"][0]["distance"], "minor")
        self.assertGreaterEqual(out["summary"]["upgradeable"], 1)

    def test_empty_project_no_network(self):
        # Unknown build system -> no deps, no network calls (mock would assert if
        # any urlopen happened). Exercises the empty-scan edge of the orchestrator.
        with temp_project({"README.md": "nothing"}) as root:
            with _patch_urlopen([]):
                out = server.handle_audit_project_dependencies({"projectPath": root})
        self.assertEqual(out["buildSystem"], "unknown")
        self.assertEqual(out["dependencies"], [])
        self.assertEqual(out["summary"]["total"], 0)

    def test_gradle_plugin_marker_resolved_in_vuln_enrichment(self):
        # #290: scan_project emits a plugins-DSL plugin as groupId == pluginId,
        # artifactId == "{pluginId}.gradle.plugin" (server.py process_plugins_block).
        # The vuln-enrichment block must resolve that marker to its implementation
        # GAV before querying OSV, and attach resolvedImplementation to the entry.
        build_file = 'plugins {\n    id("com.example.foo") version "1.0.0"\n}\n'
        marker_pom = (
            "<project><dependencies><dependency>"
            "<groupId>com.example</groupId>"
            "<artifactId>foo-impl</artifactId>"
            "<version>1.2.3</version>"
            "</dependency></dependencies></project>"
        ).encode()
        batch = _osv_batch_bare([["GHSA-zzzz"]])
        full = {
            "id": "GHSA-zzzz",
            "summary": "bad plugin impl bug",
            "severity": [{"type": "CVSS_V3", "score": "9.8"}],
            "affected": [{"ranges": [{"type": "ECOSYSTEM", "events": [
                {"introduced": "0"}, {"fixed": "1.2.4"},
            ]}]}],
            "references": [{"type": "ADVISORY", "url": "https://advisory/z"}],
        }
        # scan (local) -> fetch_metadata queries EVERY repo in the plugin-scope
        # public fallback and merges (Gradle Plugin Portal, then Maven Central:
        # 2 calls) -> resolve marker pom (first-hit, Gradle Plugin Portal: 1
        # call) -> OSV POST (1) -> GET /v1/vulns/{id} (1).
        responses = [
            (200, _meta(["1.0.0"])),
            http_error("https://repo1.maven.org/...", 404, "Not Found"),
            (200, marker_pom),
            (200, marker_pom),
            (200, batch),
            (200, _osv_vuln_get(full)),
        ]
        with temp_project({"build.gradle.kts": build_file}) as root:
            write_fake_gradlew(root)
            resolved = [{
                "groupId": "com.example.foo",
                "artifactId": "com.example.foo.gradle.plugin",
                "version": "1.0.0",
                "resolvedBy": "gradle",
                "usages": [{"module": None, "configuration": "plugin-dsl"}],
            }]
            with mock_gradle_resolve(resolved, production_count=1):
                with _patch_urlopen(responses):
                    out = server.handle_audit_project_dependencies({"projectPath": root})
        plugin_entries = [
            d for d in out["dependencies"] if d["artifactId"] == "com.example.foo.gradle.plugin"
        ]
        self.assertEqual(len(plugin_entries), 1)
        entry = plugin_entries[0]
        self.assertEqual(
            entry["resolvedImplementation"],
            {"groupId": "com.example", "artifactId": "foo-impl", "version": "1.2.3"},
        )
        self.assertTrue(entry["vulnerabilities"])
        self.assertEqual(entry["vulnerabilities"][0]["fixedVersion"], "1.2.4")

    def test_metadata_cache_dedups_across_duplicate_ga_under_concurrency(self):
        # #400 thread-safety audit: two scanned rows share the SAME
        # groupId:artifactId at DIFFERENT versions (e.g. two Gradle modules
        # pinned to different versions of the same library -- verified
        # empirically that scan_project keeps these as two separate rows, not
        # merged). Deliberately widen the race window (artificial delay) so
        # BOTH worker threads race past the "not yet cached" check before
        # either finishes its own fetch. metadata_cache_lock's guarantee is
        # NOT "never double-fetch" -- a check-then-fetch race can still cause
        # a benign redundant fetch (perf, not correctness, per the audit) --
        # it is that the dict is never corrupted and every entry sharing the
        # GA ends up referencing the SAME (first-installed-via-setdefault)
        # metadata object, so both audit rows compute IDENTICAL, valid
        # results regardless of which thread's fetch "won".
        resolved = [
            {
                "groupId": "com.example", "artifactId": "shared-lib", "version": "1.0.0",
                "resolvedBy": "gradle",
                "usages": [{"module": "app", "configuration": "releaseRuntimeClasspath"}],
            },
            {
                "groupId": "com.example", "artifactId": "shared-lib", "version": "2.0.0",
                "resolvedBy": "gradle",
                "usages": [{"module": "lib2", "configuration": "releaseRuntimeClasspath"}],
            },
        ]
        call_count = {"n": 0}
        lock = threading.Lock()

        def _counting_fetch_metadata(group_id, artifact_id, ctx):
            with lock:
                call_count["n"] += 1
            time.sleep(0.02)  # widen the race window so both threads overlap
            return {"versions": ["1.0.0", "1.5.0", "2.0.0"], "resolvedFrom": None}

        with temp_project({
            "settings.gradle.kts": 'rootProject.name = "app"\n',
            "build.gradle.kts": "dependencies { }\n",
        }) as root:
            write_fake_gradlew(root)
            with mock_gradle_resolve(resolved, production_count=2):
                with unittest.mock.patch.object(
                    server, "fetch_metadata", side_effect=_counting_fetch_metadata,
                ), unittest.mock.patch.object(
                    server, "check_relocation", return_value=None,
                ):
                    out = server.handle_audit_project_dependencies({
                        "projectPath": root, "includeVulnerabilities": False,
                    })
        # Bounded (never unbounded/runaway -- at most one call per entry
        # sharing this GA) and at least one call actually happened.
        self.assertGreaterEqual(call_count["n"], 1)
        self.assertLessEqual(call_count["n"], 2)
        by_version = {d["currentVersion"]: d for d in out["dependencies"]}
        self.assertEqual(set(by_version), {"1.0.0", "2.0.0"})
        # Both rows see a CONSISTENT metadata view (same version list) no
        # matter which thread's fetch ended up cached -- proof the dict
        # itself was never corrupted or left half-written under the race.
        # The pin 1.0.0 keeps the newer minor and the higher major. The
        # pin 2.0.0 has nothing newer.
        self.assertEqual(
            {row["version"] for row in by_version["1.0.0"]["candidates"]},
            {"1.5.0", "2.0.0"},
        )
        self.assertEqual(by_version["2.0.0"]["candidates"], [])

    def test_deadline_marks_partial_and_flags_skipped_entries(self):
        # #402: force the deadline path deterministically by making
        # fetch_metadata artificially slow relative to a tiny TOOL_DEADLINE
        # -- avoids any race on exact submit/check timing (the 10x margin
        # between the sleep and the deadline absorbs scheduling jitter).
        # Both deps must still appear in `dependencies` (never silently
        # dropped), each flagged with a deadline-skip error, and the
        # top-level partial/notes contract must fire.
        pom = (
            "<project><dependencies>"
            "<dependency><groupId>com.example</groupId><artifactId>one</artifactId>"
            "<version>1.0.0</version></dependency>"
            "<dependency><groupId>com.example</groupId><artifactId>two</artifactId>"
            "<version>1.0.0</version></dependency>"
            "</dependencies></project>"
        )

        def _slow_fetch_metadata(group_id, artifact_id, ctx):
            time.sleep(0.2)
            return {"versions": ["1.0.0"], "resolvedFrom": None}

        with temp_project({"pom.xml": pom}) as root:
            with unittest.mock.patch.object(server, "TOOL_DEADLINE", 0.02), \
                    unittest.mock.patch.object(
                        server, "fetch_metadata", side_effect=_slow_fetch_metadata,
                    ), unittest.mock.patch.object(
                        server, "check_relocation", return_value=None,
                    ):
                out = server.handle_audit_project_dependencies({
                    "projectPath": root, "includeVulnerabilities": False,
                })
        self.assertTrue(out.get("partial"))
        self.assertTrue(any("deadline" in n.lower() for n in out["notes"]))
        self.assertEqual(len(out["dependencies"]), 2)
        for dep in out["dependencies"]:
            self.assertIn("error", dep)
            self.assertIn("deadline", dep["error"])

    def test_only_issues_filters_to_dependencies_with_signals(self):
        # #405: onlyIssues narrows `dependencies` to entries carrying a signal
        # (here: an available upgrade) while `summary` still covers the FULL
        # scanned set.
        pom = (
            "<project><dependencies>"
            "<dependency><groupId>com.example</groupId><artifactId>outdated</artifactId>"
            "<version>1.0.0</version></dependency>"
            "<dependency><groupId>com.example</groupId><artifactId>current</artifactId>"
            "<version>2.0.0</version></dependency>"
            "</dependencies></project>"
        )

        def _route(req, *args, **kwargs):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "outdated" in url:
                return mock_urlopen([(200, _meta(["1.0.0", "1.1.0"]))])(req, *args, **kwargs)
            return mock_urlopen([(200, _meta(["2.0.0"]))])(req, *args, **kwargs)

        with temp_project({"pom.xml": pom}) as root:
            with unittest.mock.patch("urllib.request.urlopen", side_effect=_route):
                out = server.handle_audit_project_dependencies({
                    "projectPath": root,
                    "includeVulnerabilities": False,
                    "onlyIssues": True,
                })
        self.assertTrue(out["onlyIssues"])
        self.assertEqual(out["summary"]["total"], 2)
        self.assertEqual(out["summary"]["withIssues"], 1)
        self.assertEqual(out["summary"]["clean"], 1)
        self.assertEqual([d["artifactId"] for d in out["dependencies"]], ["outdated"])

    def test_only_issues_default_false_leaves_output_unchanged(self):
        # Omitted (default false) -> every scanned dep present, no
        # onlyIssues/withIssues/clean keys added (byte-for-byte unchanged
        # shape from before #405).
        pom = (
            "<project><dependencies><dependency>"
            "<groupId>com.example</groupId><artifactId>lib</artifactId><version>2.0.0</version>"
            "</dependency></dependencies></project>"
        )
        with temp_project({"pom.xml": pom}) as root:
            with _patch_urlopen([(200, _meta(["2.0.0"]))]):
                out = server.handle_audit_project_dependencies({
                    "projectPath": root, "includeVulnerabilities": False,
                })
        self.assertNotIn("onlyIssues", out)
        self.assertNotIn("withIssues", out["summary"])
        self.assertEqual(len(out["dependencies"]), 1)

    def test_resolved_from_dedup_hoists_shared_default(self):
        # Audit does not select one version, so a shared repo is not hoisted
        # and no row carries resolvedFrom. These pins equal the only
        # published version, so candidates stay empty.
        pom = (
            "<project><dependencies>"
            "<dependency><groupId>com.example</groupId><artifactId>a</artifactId><version>1.0.0</version></dependency>"
            "<dependency><groupId>com.example</groupId><artifactId>b</artifactId><version>1.0.0</version></dependency>"
            "<dependency><groupId>com.example</groupId><artifactId>c</artifactId><version>1.0.0</version></dependency>"
            "</dependencies></project>"
        )
        with temp_project({"pom.xml": pom}) as root:
            with _patch_urlopen([(200, _meta(["1.0.0"]))] * 3):
                out = server.handle_audit_project_dependencies({
                    "projectPath": root, "includeVulnerabilities": False,
                })
        self.assertNotIn("defaultResolvedFrom", out)
        for dep in out["dependencies"]:
            self.assertEqual(dep["candidates"], [])
            self.assertNotIn("error", dep)
            self.assertNotIn("resolvedFrom", dep)


# --- dependency-health stat/date helpers (P3 boundaries, direct) ------------

class TestDependencyHealthHelpers(unittest.TestCase):
    """P3 edges for the health stat/date helpers, exercised directly.

    ``median_days_to_close`` (server.py :452) is a closure inside
    ``gh_fetch_issue_stats``; "directly" therefore means driving that function
    one level below ``handle_get_dependency_health`` with mocked urlopen, not
    through the handler.
    """

    def test_median_days_to_close_empty_durations(self):
        # gh_fetch_issue_stats issues 3 _gh_get calls: open, closed, median items.
        # Empty items -> empty durations -> median returns None (:471-:472).
        responses = [
            (200, _json({"total_count": 3})),   # open
            (200, _json({"total_count": 2})),   # closed
            (200, _json({"items": []})),        # median items (empty)
        ]
        with _patch_urlopen(responses):
            stats = server.gh_fetch_issue_stats("acme", "widget")
        self.assertIsNone(stats["medianDaysToClose"])

    def test_median_days_to_close_single_element(self):
        # One closed issue 4 days wide -> single-element durations -> odd branch
        # returns that value, rounded to days (:475-:479).
        responses = [
            (200, _json({"total_count": 1})),
            (200, _json({"total_count": 1})),
            (200, _json({"items": [
                {"created_at": "2024-01-01T00:00:00Z",
                 "closed_at": "2024-01-05T00:00:00Z"},
            ]})),
        ]
        with _patch_urlopen(responses):
            stats = server.gh_fetch_issue_stats("acme", "widget")
        self.assertEqual(stats["medianDaysToClose"], 4)

    def test_months_since(self):
        self.assertIsNone(server._months_since(None))  # :508-:509
        # Naive UTC "now" (matches _months_since's internal utcnow baseline)
        # without the deprecated utcnow() call.
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        recent = (now - datetime.timedelta(days=15)).isoformat()
        self.assertEqual(server._months_since(recent), 0)  # int(15/30)
        old = (now - datetime.timedelta(days=400)).isoformat()
        self.assertEqual(server._months_since(old), 13)    # int(400/30)
        # Aware ISO (offset present): :515-:516 strips tzinfo before subtracting.
        aware = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(days=400)).isoformat()
        self.assertTrue(aware.endswith("+00:00"))
        self.assertEqual(server._months_since(aware), 13)

    def test_parse_iso_naive_and_aware(self):
        naive = server._parse_iso("2020-01-02T03:04:05")  # :496
        self.assertEqual((naive.year, naive.month, naive.day), (2020, 1, 2))
        self.assertIsNone(naive.utcoffset())  # naive timestamp -> no offset
        aware = server._parse_iso("2020-01-02T03:04:05Z")  # Z -> +00:00 (:499)
        self.assertEqual(aware.utcoffset(), datetime.timedelta(0))

    def test_summarize_releases(self):
        # Empty -> all-None (:539-:540).
        empty = server._summarize_releases([])
        self.assertEqual(empty, {"last": None, "cadenceDays": None, "count": 0})

        # Single -> last set, cadence None (:542-:543).
        single = server._summarize_releases([{"published_at": "2024-03-21T00:00:00Z"}])
        self.assertEqual(single["count"], 1)
        self.assertEqual(single["last"], "2024-03-21T00:00:00Z")
        self.assertIsNone(single["cadenceDays"])

        # Cadence math: gaps of 10 and 30 days -> even-length median = 20 days.
        # draft/prerelease entries are filtered out before the math (:527).
        releases = [
            {"published_at": "2024-03-21T00:00:00Z"},
            {"published_at": "2024-03-11T00:00:00Z"},   # 10 days before
            {"published_at": "2024-02-10T00:00:00Z"},   # 30 days before 03-11
            {"published_at": "2024-04-01T00:00:00Z", "prerelease": True},
            {"published_at": "2024-04-02T00:00:00Z", "draft": True},
        ]
        summary = server._summarize_releases(releases)
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["last"], "2024-03-21T00:00:00Z")
        self.assertEqual(summary["cadenceDays"], 20)


if __name__ == "__main__":
    unittest.main()
