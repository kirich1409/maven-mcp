"""compare_upgrade_closure on one POM graph pair.

Network stays on the existing seams: resolve_pom_graph and
query_osv_batch. Licenses come from pomLicenses on those nodes.
The HTTP 500 querybatch flag is pinned in test_maven_search_osv.py.
"""

import os
import unittest
import unittest.mock

from _helpers import server, mock_urlopen, http_error

# Not a Gradle build, so omitted graphSource stays on the POM graph
# even when the process cwd has a wrapper.
_PROJECT = os.path.dirname(os.path.abspath(__file__))


def _node(group_id, artifact_id, version, relation="", errors=None, licenses=None):
    node = {
        "groupId": group_id,
        "artifactId": artifact_id,
        "version": version,
        "relation": relation,
        "errors": list(errors or []),
    }
    if licenses is not None:
        node["pomLicenses"] = list(licenses)
    return node


def _edge(src, dst):
    return {"from": src, "to": dst}


def _graph(nodes, edges=None, **extra):
    out = {
        "ok": True,
        "status": 200,
        "error": None,
        "nodes": nodes,
        "edges": edges or [],
        "partial": False,
        "truncated": False,
    }
    out.update(extra)
    return out


def _upgrade(group_id="com.squareup.okhttp3", artifact_id="okhttp",
             from_version="4.9.3", to_version="4.12.0"):
    return {
        "groupId": group_id,
        "artifactId": artifact_id,
        "fromVersion": from_version,
        "toVersion": to_version,
    }


def _args(upgrade, **extra):
    payload = {"upgrades": [upgrade], "projectPath": _PROJECT}
    payload.update(extra)
    return payload


def _clean_osv(deps):
    return [
        {
            "groupId": dep["groupId"],
            "artifactId": dep["artifactId"],
            "version": dep["version"],
            "vulnerabilities": [],
        }
        for dep in deps
    ]


def _root_only(group_id, artifact_id, version):
    return _graph([_node(group_id, artifact_id, version, "SELF")])


def _spy_map(calls):
    real = server._map_parallel

    def wrapped(items, fn, max_workers=server.MAX_PARALLEL_FETCHES, deadline=None):
        calls.append({"items": list(items), "deadline": deadline})
        return real(items, fn, max_workers=max_workers, deadline=deadline)

    return wrapped


class DepsdevClosureTest(unittest.TestCase):
    def test_not_ok_is_unknown_and_skips_osv(self):
        def fetch(_group_id, _artifact_id, _version, _ctx=None):
            return {
                "ok": False,
                "error": "deps.dev returned HTTP 404",
                "nodes": [_node("com.example", "would-diff", "1.0.0")],
                "edges": [],
                "partial": True,
                "truncated": False,
            }

        with unittest.mock.patch.object(server, "resolve_pom_graph", fetch), \
                unittest.mock.patch.object(server, "query_osv_batch") as osv:
            out = server.compare_upgrade_closure(_args(_upgrade()))
        osv.assert_not_called()
        self.assertEqual(out["graphSource"], "pom")
        self.assertEqual(out["advisory"], "unknown")
        self.assertEqual(out["dependencies"], {"added": [], "changed": [], "removed": []})
        self.assertEqual(out["summary"], {"added": 0, "changed": 0, "removed": 0, "unchanged": 0})
        self.assertFalse(out["diffReliable"])
        self.assertTrue(out["partial"])
        self.assertTrue(out["notes"])
        self.assertEqual(out["error"], "deps.dev returned HTTP 404")
        self.assertNotIn("capabilityUnavailable", out)

    def test_capability_unavailable_is_unknown_with_empty_buckets(self):
        def fetch(_group_id, _artifact_id, _version, _ctx=None):
            return {
                "ok": False,
                "error": "deps.dev unavailable (offline/closed mode)",
                "capabilityUnavailable": "offline",
                "nodes": [],
                "edges": [],
                "partial": True,
                "truncated": False,
            }

        with unittest.mock.patch.object(server, "resolve_pom_graph", fetch), \
                unittest.mock.patch.object(server, "query_osv_batch") as osv:
            out = server.compare_upgrade_closure(_args(_upgrade()))
        osv.assert_not_called()
        self.assertEqual(out["advisory"], "unknown")
        self.assertEqual(out["capabilityUnavailable"], "offline")
        self.assertEqual(out["dependencies"]["added"], [])
        self.assertEqual(out["dependencies"]["changed"], [])
        self.assertEqual(out["dependencies"]["removed"], [])
        self.assertNotIn("user:pass", out["error"])

    def test_fetch_exception_does_not_leak_exception_text(self):
        def fetch(_group_id, _artifact_id, _version, _ctx=None):
            raise RuntimeError("user:pass@repo.example/secret")

        with unittest.mock.patch.object(server, "resolve_pom_graph", fetch), \
                unittest.mock.patch.object(server, "query_osv_batch") as osv:
            out = server.compare_upgrade_closure(_args(_upgrade()))
        osv.assert_not_called()
        self.assertEqual(out["advisory"], "unknown")
        self.assertNotIn("user:pass", out["error"])
        self.assertNotIn("secret", out["error"])
        self.assertEqual(out["error"], "POM graph fetch failed")

    def test_truncated_or_node_errors_keeps_buckets_and_is_not_none(self):
        cases = (
            {"truncated": True, "partial": True},
            {"partial": True, "nodeErrors": [{
                "groupId": "com.example",
                "artifactId": "skipped",
                "version": "1",
                "errors": ["version range is not resolved"],
            }]},
        )
        for extra in cases:
            with self.subTest(extra=extra):
                def fetch(_group_id, _artifact_id, version, _ctx=None, _extra=extra):
                    nodes = [_node(_group_id, _artifact_id, version, "SELF")]
                    edges = []
                    if version == "4.12.0":
                        nodes.append(_node(
                            "com.example", "extra", "9.0.0", licenses=["MIT"],
                        ))
                        edges.append(_edge(0, 1))
                    return _graph(nodes, edges, **_extra)

                with unittest.mock.patch.object(server, "resolve_pom_graph", fetch), \
                        unittest.mock.patch.object(server, "query_osv_batch", _clean_osv):
                    out = server.compare_upgrade_closure(_args(_upgrade()))
                self.assertTrue(out["dependencies"]["added"])
                self.assertNotIn("sideEffect", out["dependencies"]["added"][0])
                self.assertFalse(out["diffReliable"])
                self.assertIn(out["advisory"], ("review", "stop"))
                self.assertNotEqual(out["advisory"], "none")
                self.assertTrue(out["partial"])
                if extra.get("truncated"):
                    self.assertTrue(out["truncated"])

    def test_targets_are_osv_queried_when_closure_is_unchanged(self):
        captured = []

        def fetch(group_id, artifact_id, version, ctx=None):
            return _graph([
                _node(group_id, artifact_id, version, "SELF"),
                _node("com.example", "stable", "1.2.3"),
            ], [_edge(0, 1)])

        def osv(deps):
            captured.extend(deps)
            return _clean_osv(deps)

        with unittest.mock.patch.object(server, "resolve_pom_graph", fetch), \
                unittest.mock.patch.object(server, "query_osv_batch", osv), \
                unittest.mock.patch.object(server, "get_transitive_graph") as trimmed, \
                unittest.mock.patch.object(server, "_gradle_resolve_dependencies") as gradle:
            out = server.compare_upgrade_closure(_args(_upgrade()))
        trimmed.assert_not_called()
        gradle.assert_not_called()
        keys = {(d["groupId"], d["artifactId"], d["version"]) for d in captured}
        self.assertIn(("com.squareup.okhttp3", "okhttp", "4.12.0"), keys)
        self.assertIn(("com.squareup.okhttp3", "okhttp", "4.9.3"), keys)
        self.assertNotIn(("com.example", "stable", "1.2.3"), keys)
        self.assertEqual(out["graphSource"], "pom")
        self.assertEqual(out["advisory"], "none")
        self.assertEqual(out["targets"][0]["vulnerabilities"], [])
        self.assertTrue(out["diffReliable"])
        self.assertTrue(any("not a safety guarantee" in note for note in out["notes"]))
        self.assertTrue(any(note.startswith("Graphs are resolved per root") for note in out["notes"]))

    def test_one_license_gav_skips_map_parallel_and_graphs_use_deadline(self):
        calls = []

        def fetch(group_id, artifact_id, version, ctx=None):
            nodes = [_node(group_id, artifact_id, version, "SELF")]
            edges = []
            if version == "4.12.0":
                nodes.append(_node(
                    "com.example", "extra", "9.0.0", licenses=["MIT"],
                ))
                edges.append(_edge(0, 1))
            return _graph(nodes, edges)

        with unittest.mock.patch.object(server, "_now", return_value=1000.0), \
                unittest.mock.patch.object(server, "_map_parallel", _spy_map(calls)), \
                unittest.mock.patch.object(server, "resolve_pom_graph", fetch), \
                unittest.mock.patch.object(server, "query_osv_batch", _clean_osv), \
                unittest.mock.patch.object(server, "check_license_compliance") as compliance:
            out = server.compare_upgrade_closure(_args(_upgrade()))
        compliance.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["items"], ["4.9.3", "4.12.0"])
        self.assertEqual(calls[0]["deadline"], 1000.0 + server.TOOL_DEADLINE)
        appeared = [
            row for row in out["license"]["appeared"]
            if row["artifactId"] == "extra"
        ]
        self.assertEqual(appeared[0]["spdxId"], "MIT")
        self.assertNotIn("sideEffect", out["dependencies"]["added"][0])
        self.assertTrue(any(note.startswith("Graphs are resolved per root") for note in out["notes"]))
        self.assertTrue(any(note.startswith("Verdicts are heuristic") for note in out["notes"]))
        self.assertTrue(any(note.startswith("A POM license name is normalized") for note in out["notes"]))
        self.assertIn(server._UPGRADE_CHANGED_ONLY_LICENSE_NOTE, out["notes"])
        self.assertIn("license", out)

    def test_two_license_gavs_use_deadline(self):
        calls = []

        def fetch(group_id, artifact_id, version, ctx=None):
            if version == "4.9.3":
                return _graph([
                    _node(group_id, artifact_id, version, "SELF"),
                    _node("com.example", "old", "1.0.0", licenses=["Apache-2.0"]),
                ], [_edge(0, 1)])
            return _graph([
                _node(group_id, artifact_id, version, "SELF"),
                _node("com.example", "new", "2.0.0", licenses=["Apache-2.0"]),
            ], [_edge(0, 1)])

        with unittest.mock.patch.object(server, "_now", return_value=1000.0), \
                unittest.mock.patch.object(server, "_map_parallel", _spy_map(calls)), \
                unittest.mock.patch.object(server, "resolve_pom_graph", fetch), \
                unittest.mock.patch.object(server, "query_osv_batch", _clean_osv):
            out = server.compare_upgrade_closure(_args(_upgrade()))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["items"], ["4.9.3", "4.12.0"])
        self.assertEqual(calls[0]["deadline"], 1000.0 + server.TOOL_DEADLINE)
        appeared = {row["artifactId"]: row["spdxId"] for row in out["license"]["appeared"]}
        disappeared = {
            row["artifactId"]: row["spdxId"] for row in out["license"]["disappeared"]
        }
        self.assertEqual(appeared["new"], "Apache-2.0")
        self.assertEqual(disappeared["old"], "Apache-2.0")

    def test_osv_http_500_is_unreachable_not_clean(self):
        def fetch(group_id, artifact_id, version, ctx=None):
            return _root_only(group_id, artifact_id, version)

        with unittest.mock.patch.object(server, "resolve_pom_graph", fetch), \
                unittest.mock.patch.object(server, "_sleep"), \
                unittest.mock.patch(
                    "urllib.request.urlopen",
                    side_effect=mock_urlopen([
                        http_error("u", 500, "boom"),
                        http_error("u", 500, "boom"),
                    ]),
                ):
            out = server.compare_upgrade_closure(_args(_upgrade()))
        self.assertEqual(out["capabilityUnavailable"], "unreachable")
        self.assertEqual(out["vulnerabilities"]["introduced"], [])
        self.assertEqual(out["vulnerabilities"]["remaining"], [])
        self.assertEqual(out["vulnerabilities"]["fixed"], [])
        self.assertNotEqual(out["advisory"], "none")
        self.assertEqual(out["targets"][0]["vulnerabilities"], [])
        self.assertEqual(out["targets"][0]["capabilityUnavailable"], "unreachable")

    def test_all_identity_short_circuit(self):
        upgrade = _upgrade(from_version="4.12.0", to_version="4.12.0")
        with unittest.mock.patch.object(server, "resolve_pom_graph") as fetch, \
                unittest.mock.patch.object(server, "query_osv_batch") as osv, \
                unittest.mock.patch.object(server, "_map_parallel") as parallel:
            out = server.compare_upgrade_closure(_args(upgrade))
        fetch.assert_not_called()
        osv.assert_not_called()
        parallel.assert_not_called()
        self.assertEqual(out["graphSource"], "pom")
        self.assertEqual(out["advisory"], "none")
        self.assertFalse(out["diffReliable"])
        self.assertFalse(out["partial"])
        self.assertNotIn("vulnerabilities", out["targets"][0])
        self.assertNotIn("vulnerabilityDelta", out["targets"][0])
        self.assertTrue(any("not re-queried" in note for note in out["notes"]))
        self.assertEqual(out["dependencies"]["added"], [])

    def test_marker_only_is_not_none(self):
        marker = _upgrade(
            group_id="com.example.plugin",
            artifact_id="com.example.plugin.gradle.plugin",
        )
        for fr, to in (("1.0.0", "1.0.0"), ("1.0.0", "1.1.0")):
            with self.subTest(fr=fr, to=to):
                upgrade = dict(marker)
                upgrade["fromVersion"] = fr
                upgrade["toVersion"] = to
                with unittest.mock.patch.object(server, "resolve_pom_graph") as fetch, \
                        unittest.mock.patch.object(server, "query_osv_batch") as osv:
                    out = server.compare_upgrade_closure(_args(upgrade))
                fetch.assert_not_called()
                osv.assert_not_called()
                self.assertEqual(out["graphSource"], "pom")
                self.assertEqual(out["advisory"], "unknown")
                self.assertNotEqual(out["advisory"], "none")
                self.assertEqual(out["upgrades"][0]["error"], "plugin marker; closure not compared")
                self.assertEqual(out["targets"], [])
                self.assertEqual(out["dependencies"]["added"], [])
                self.assertTrue(out["notes"])

    def test_rejects_before_fetch(self):
        ok = _upgrade()
        other = _upgrade(group_id="com.example", artifact_id="lib", from_version="1.0.0", to_version="2.0.0")
        same = _upgrade(from_version="1.0.0", to_version="2.0.0")
        cases = (
            ({"upgrades": [ok, other], "projectPath": _PROJECT, "graphSource": "pom"}, "exactly one"),
            ({"upgrades": [same, dict(same, toVersion="3.0.0")], "projectPath": _PROJECT}, "duplicate"),
            ({"upgrades": [
                _upgrade(from_version="1.0.0", to_version="1.0.0"),
                other,
            ], "projectPath": _PROJECT}, "cannot mix"),
            (_args(_upgrade(group_id="com.example lib")), "must match"),
            (_args(_upgrade(from_version="1.0.0$bad")), "must match"),
        )
        for args, pattern in cases:
            with self.subTest(pattern=pattern):
                with unittest.mock.patch.object(server, "resolve_pom_graph") as fetch, \
                        unittest.mock.patch.object(server, "query_osv_batch") as osv, \
                        self.assertRaisesRegex(ValueError, pattern):
                    server.compare_upgrade_closure(args)
                fetch.assert_not_called()
                osv.assert_not_called()

    def test_node_error_license_is_review_and_partial(self):
        def fetch(group_id, artifact_id, version, ctx=None):
            nodes = [_node(group_id, artifact_id, version, "SELF", licenses=["MIT"])]
            if version == "4.12.0":
                nodes.append(_node(
                    "com.example", "extra", "9.0.0", "DIRECT",
                    errors=["POM not found for com.example:extra:9.0.0"],
                ))
            return _graph(nodes)

        with unittest.mock.patch.object(server, "resolve_pom_graph", fetch), \
                unittest.mock.patch.object(server, "query_osv_batch", _clean_osv):
            out = server.compare_upgrade_closure(_args(_upgrade()))
        self.assertEqual(out["advisory"], "review")
        self.assertTrue(out["partial"])
        self.assertFalse(out["diffReliable"])
        self.assertNotEqual(out.get("capabilityUnavailable"), "unreachable")
        extra = [
            row for row in out["license"]["appeared"]
            if row["artifactId"] == "extra"
        ]
        self.assertEqual(extra[0]["verdict"], "review")

    def test_license_404_is_a_review_verdict_not_unreachable(self):
        def fetch(group_id, artifact_id, version, ctx=None):
            nodes = [_node(group_id, artifact_id, version, "SELF", licenses=["MIT"])]
            if version == "4.12.0":
                nodes.append(_node(
                    "com.example", "extra", "9.0.0", "DIRECT",
                    errors=["POM not found for com.example:extra:9.0.0"],
                ))
            return _graph(nodes)

        with unittest.mock.patch.object(server, "resolve_pom_graph", fetch), \
                unittest.mock.patch.object(server, "query_osv_batch", _clean_osv):
            out = server.compare_upgrade_closure(_args(_upgrade()))
        extra = [
            row for row in out["license"]["appeared"]
            if row["artifactId"] == "extra"
        ]
        self.assertEqual(extra[0]["verdict"], "review")
        self.assertNotEqual(out.get("capabilityUnavailable"), "unreachable")

    def test_license_cap_is_review_and_partial(self):
        def fetch(group_id, artifact_id, version, ctx=None):
            nodes = [_node(group_id, artifact_id, version, "SELF")]
            if version == "4.12.0":
                extra_count = server.MAX_UPGRADE_LICENSE_NODES + 1
                nodes.extend(
                    _node(
                        "com.example", f"extra{i}", "1.0.0", "DIRECT",
                        licenses=["MIT"],
                    )
                    for i in range(extra_count)
                )
            return _graph(nodes)

        with unittest.mock.patch.object(server, "resolve_pom_graph", fetch), \
                unittest.mock.patch.object(server, "query_osv_batch", _clean_osv):
            out = server.compare_upgrade_closure(_args(_upgrade()))
        self.assertTrue(out["license"]["truncated"])
        self.assertNotIn("categoriesIntroduced", out["license"])
        self.assertEqual(out["advisory"], "review")
        self.assertTrue(out["partial"])
        self.assertTrue(any("capped" in note for note in out["notes"]))


if __name__ == "__main__":
    unittest.main()
