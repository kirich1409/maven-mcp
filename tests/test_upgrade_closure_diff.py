"""I/O-free upgrade-closure merge, diff, and advisory ranking.

No network. These tests never mock urllib because the functions under test
must not fetch.
"""

import unittest
import unittest.mock

from _helpers import server


def _gav(group_id, artifact_id, version):
    return {"groupId": group_id, "artifactId": artifact_id, "version": version}


def _project():
    return {"project": True}


def _tree(module, configuration, nodes, edges):
    return {
        "module": module,
        "configuration": configuration,
        "nodes": nodes,
        "edges": edges,
    }


def _side(graph, ok=True, diff_reliable=True):
    return {"ok": ok, "diffReliable": diff_reliable, "graph": graph}


def _empty_graph():
    return {"nodes": [], "edges": []}


def _upgrade(group_id, artifact_id, from_version, to_version, **extra):
    row = {
        "groupId": group_id,
        "artifactId": artifact_id,
        "fromVersion": from_version,
        "toVersion": to_version,
    }
    row.update(extra)
    return row


def _vuln(vuln_id, severity=None, fixed=None, malicious=False, summary="example advisory"):
    item = {
        "id": vuln_id,
        "summary": summary,
        "url": "https://osv.dev/vulnerability/" + vuln_id,
        "malicious": malicious,
    }
    if severity:
        item["severity"] = severity
    if fixed:
        item["fixedVersion"] = fixed
    return item


def _osv(group_id, artifact_id, version, vulns=None, capability=None):
    record = {
        "groupId": group_id,
        "artifactId": artifact_id,
        "version": version,
        "vulnerabilities": list(vulns or []),
    }
    if capability:
        record["capabilityUnavailable"] = capability
    return record


def _clean_target(group_id="com.acme", artifact_id="lib", from_version="1.0.0", to_version="2.0.0"):
    return {
        "groupId": group_id,
        "artifactId": artifact_id,
        "fromVersion": from_version,
        "toVersion": to_version,
        "vulnerabilities": [],
        "vulnerabilityDelta": [],
    }


def _empty_vulns():
    return {"introduced": [], "remaining": [], "fixed": [], "uncompared": []}


class AdvisoryOutcomeTest(unittest.TestCase):
    def test_tc1_empty_delta_and_clean_target_is_not_a_guarantee(self):
        upgrade = _upgrade("com.acme", "lib", "1.0.0", "2.0.0")
        before = _side(server._normalise_closure_graph([
            _tree("app", "runtimeClasspath", [
                _project(),
                _gav("com.acme", "lib", "1.0.0"),
                _gav("com.acme", "trans", "5.0.0"),
            ], [{"from": 0, "to": 1}, {"from": 1, "to": 2}]),
        ]))
        after = _side(server._normalise_closure_graph([
            _tree("app", "runtimeClasspath", [
                _project(),
                _gav("com.acme", "lib", "2.0.0"),
                _gav("com.acme", "trans", "5.0.0"),
            ], [{"from": 0, "to": 1}, {"from": 1, "to": 2}]),
        ]))
        diff = server._diff_closure(before, after, [upgrade])
        self.assertTrue(diff["compared"])
        self.assertTrue(diff["diffReliable"])
        self.assertEqual(diff["summary"], {"added": 0, "changed": 0, "removed": 0, "unchanged": 1})
        classified = server._classify_vuln_delta([upgrade], diff, [
            _osv("com.acme", "lib", "1.0.0"),
            _osv("com.acme", "lib", "2.0.0"),
        ])
        target = classified["targets"][0]
        self.assertEqual(target["vulnerabilities"], [])
        self.assertEqual(target["vulnerabilityDelta"], [])
        self.assertNotIn("safeUpgrade", target)
        ranked = server._advisory_for_upgrade(
            targets=classified["targets"],
            diff_reliable=diff["diffReliable"],
            diff=diff,
            vulnerabilities=classified["vulnerabilities"],
            fixes_incomplete=classified["fixesIncomplete"],
        )
        self.assertEqual(ranked["advisory"], "none")
        self.assertTrue(ranked["notes"])
        self.assertIn("not a safety guarantee", " ".join(ranked["notes"]))
        self.assertIn("vulnerabilities", ranked["targets"][0])

    def test_tc2_all_identity_skips_diff_and_osv(self):
        poisoned = _upgrade("com.acme", "lib", "1.0.0", "1.0.0")
        poisoned["vulnerabilities"] = [_vuln("MAL-2025-1", malicious=True)]
        poisoned["landed"] = False
        poisoned["vulnerabilityDelta"] = [_vuln("CVE-1", severity="CRITICAL")]
        ranked = server._advisory_for_upgrade(
            all_identity=True,
            diff_reliable=False,
            targets=[poisoned],
            vulnerabilities={
                "introduced": [_vuln("MAL-2025-2", malicious=True)],
                "remaining": [],
                "fixed": [],
                "uncompared": [],
            },
            diff={
                "added": [_gav("com.acme", "extra", "1")],
                "changed": [],
                "removed": [],
                "summary": {"added": 1, "changed": 0, "removed": 0, "unchanged": 0},
            },
            capability_unavailable="unreachable",
        )
        self.assertEqual(ranked["advisory"], "none")
        self.assertFalse(ranked["diffReliable"])
        self.assertNotIn("vulnerabilities", ranked["targets"][0])
        self.assertNotIn("vulnerabilityDelta", ranked["targets"][0])
        self.assertNotIn("landed", ranked["targets"][0])
        self.assertEqual(ranked["targets"][0]["toVersion"], "1.0.0")
        self.assertEqual(ranked["dependencies"]["added"], [])
        self.assertIn("not re-queried", " ".join(ranked["notes"]))
        self.assertNotIn("capabilityUnavailable", ranked)

    def test_tc3_failed_side_does_not_diff(self):
        upgrade = _upgrade("com.acme", "lib", "1.0.0", "2.0.0")
        # nodes is not iterable — touching the graph would raise.
        before = {"ok": False, "graph": {"nodes": None}}
        after = _side({
            "nodes": [_gav("com.acme", "only-after", "1.0.0")],
            "edges": [],
        })
        diff = server._diff_closure(before, after, [upgrade])
        self.assertFalse(diff["compared"])
        self.assertEqual(diff["added"], [])
        self.assertEqual(diff["changed"], [])
        self.assertEqual(diff["removed"], [])
        self.assertEqual(diff["summary"], {"added": 0, "changed": 0, "removed": 0, "unchanged": 0})
        self.assertFalse(diff["diffReliable"])

        ranked = server._advisory_for_upgrade(
            before_ok=False,
            after_ok=True,
            diff_reliable=True,
            diff={
                "added": [{"groupId": "com.acme", "artifactId": "only-after", "versions": ["1.0.0"]}],
                "changed": [],
                "removed": [],
                "summary": {"added": 1, "changed": 0, "removed": 0, "unchanged": 0},
            },
            vulnerabilities=_empty_vulns(),
            targets=[_clean_target()],
        )
        self.assertEqual(ranked["advisory"], "unknown")
        self.assertEqual(ranked["dependencies"]["added"], [])
        self.assertEqual(ranked["summary"]["added"], 0)
        self.assertTrue(ranked["notes"])

    def test_tc26_unreliable_without_osv_is_not_none(self):
        ranked = server._advisory_for_upgrade(all_identity=False, diff_reliable=False)
        self.assertNotEqual(ranked["advisory"], "none")
        self.assertEqual(ranked["advisory"], "review")
        self.assertTrue(ranked["notes"])

        # diffReliable true still cannot be none when nothing was queried.
        ranked = server._advisory_for_upgrade(all_identity=False, diff_reliable=True)
        self.assertNotEqual(ranked["advisory"], "none")

    def test_soft_failure_keeps_buckets_and_blocks_none(self):
        upgrade = _upgrade("com.acme", "lib", "1.0.0", "2.0.0")
        before = _side(_empty_graph(), diff_reliable=False)
        after = _side({
            "nodes": [
                _gav("com.acme", "lib", "2.0.0"),
                _gav("com.acme", "extra", "1.0.0"),
            ],
            "edges": [{"from": 0, "to": 1}],
        })
        diff = server._diff_closure(before, after, [upgrade])
        self.assertTrue(diff["compared"])
        self.assertFalse(diff["diffReliable"])
        self.assertEqual([row["artifactId"] for row in diff["added"]], ["extra"])
        classified = server._classify_vuln_delta([upgrade], diff, [
            _osv("com.acme", "lib", "1.0.0"),
            _osv("com.acme", "lib", "2.0.0"),
        ])
        ranked = server._advisory_for_upgrade(
            diff_reliable=False,
            diff=diff,
            targets=classified["targets"],
            vulnerabilities=classified["vulnerabilities"],
        )
        self.assertEqual(ranked["advisory"], "review")
        self.assertEqual(ranked["dependencies"]["added"], diff["added"])

    def test_capability_unavailable_blocks_none_without_clearing_buckets(self):
        fixed = _vuln("CVE-FIXED", severity="LOW")
        fixed["relation"] = "fixed"
        fixed["vulnerableNode"] = _gav("com.acme", "old", "1.0.0")
        vulns = _empty_vulns()
        vulns["fixed"] = [fixed]
        diff = {
            "added": [],
            "changed": [],
            "removed": [{
                "groupId": "com.acme",
                "artifactId": "old",
                "versions": ["1.0.0"],
            }],
            "summary": {"added": 0, "changed": 0, "removed": 1, "unchanged": 0},
        }
        ranked = server._advisory_for_upgrade(
            diff_reliable=True,
            diff=diff,
            targets=[_clean_target()],
            vulnerabilities=vulns,
            capability_unavailable="unreachable",
        )
        self.assertEqual(ranked["advisory"], "review")
        self.assertEqual(ranked["vulnerabilities"]["fixed"], [fixed])
        self.assertEqual(ranked["dependencies"]["removed"], diff["removed"])
        self.assertEqual(ranked["capabilityUnavailable"], "unreachable")


class ClosureGraphTest(unittest.TestCase):
    def test_tc6_shared_gav_and_project_hop(self):
        upgrade = _upgrade("com.acme", "direct", "1.0.0", "2.0.0")
        app = _tree("app", "runtimeClasspath", [
            _project(),
            _gav("com.acme", "direct", "2.0.0"),
            _gav("com.acme", "shared", "1.0.0"),
            _project(),
            _gav("com.acme", "child", "1.0.0"),
        ], [
            {"from": 0, "to": 1},
            {"from": 1, "to": 2},
            {"from": 0, "to": 3},
            {"from": 3, "to": 4},
        ])
        lib = _tree("lib", "runtimeClasspath", [
            _project(),
            _gav("com.acme", "shared", "1.0.0"),
        ], [{"from": 0, "to": 1}])
        other = _tree("other", "runtimeClasspath", [
            _project(),
            _gav("com.acme", "other", "1.0.0"),
        ], [{"from": 0, "to": 1}])
        graph = server._normalise_closure_graph([app, lib, other])
        shared = [node for node in graph["nodes"] if node.get("artifactId") == "shared"]
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0]["usages"], [
            {"module": "app", "configuration": "runtimeClasspath", "version": "1.0.0"},
            {"module": "lib", "configuration": "runtimeClasspath", "version": "1.0.0"},
        ])
        child_index = next(
            i for i, node in enumerate(graph["nodes"]) if node.get("artifactId") == "child"
        )
        app_synthetic = next(
            i for i, node in enumerate(graph["nodes"])
            if node.get("synthetic") and node.get("module") == "app"
        )
        self.assertIn({"from": app_synthetic, "to": child_index}, graph["edges"])
        predecessor = server._bfs_predecessors(graph["edges"], app_synthetic)
        self.assertIsNotNone(server._reconstruct_path(predecessor, app_synthetic, child_index))

        diff = server._diff_closure(_side(_empty_graph()), _side(graph), [upgrade])
        by_artifact = {row["artifactId"]: row for row in diff["added"]}
        self.assertNotIn("direct", by_artifact)
        self.assertFalse(by_artifact["child"]["sideEffect"])
        self.assertFalse(by_artifact["shared"]["sideEffect"])
        self.assertTrue(by_artifact["other"]["sideEffect"])
        self.assertEqual(by_artifact["child"]["path"][-1]["artifactId"], "child")
        self.assertEqual(by_artifact["shared"]["path"], [
            _gav("com.acme", "direct", "2.0.0"),
            _gav("com.acme", "shared", "1.0.0"),
        ])
        self.assertEqual(
            [row["artifactId"] for row in diff["added"]],
            ["child", "shared", "other"],
        )

    def test_sort_uses_full_path_length(self):
        upgrade = _upgrade("com.acme", "direct", "1.0.0", "2.0.0")
        graph = server._normalise_closure_graph([_tree("app", "runtimeClasspath", [
            _project(),
            _gav("com.acme", "direct", "2.0.0"),
            _gav("com.acme", "zeta", "1.0.0"),
            _gav("com.acme", "mid", "1.0.0"),
            _gav("com.acme", "alpha", "1.0.0"),
        ], [
            {"from": 0, "to": 1},
            {"from": 1, "to": 2},
            {"from": 0, "to": 3},
            {"from": 3, "to": 4},
        ])])
        diff = server._diff_closure(_side(_empty_graph()), _side(graph), [upgrade])
        ids = [row["artifactId"] for row in diff["added"]]
        # alpha's full path is longer (synthetic hop + two GAVs) even though the
        # displayed GAV list is the same length as zeta's. Display length would
        # put alpha first.
        self.assertLess(ids.index("zeta"), ids.index("alpha"))

    def test_tc7_diamond_keeps_both_edges_and_shortens_long_paths(self):
        upgrade = _upgrade("com.acme", "root", "1.0.0", "2.0.0")
        graph = server._normalise_closure_graph([_tree("app", "runtimeClasspath", [
            _gav("com.acme", "root", "2.0.0"),
            _gav("com.acme", "mid", "1.0.0"),
            _gav("com.acme", "leaf", "1.0.0"),
        ], [
            {"from": 0, "to": 1},
            {"from": 1, "to": 2},
            {"from": 0, "to": 2},
            {"from": 0, "to": 2},
        ])])
        leaf_index = next(i for i, node in enumerate(graph["nodes"]) if node.get("artifactId") == "leaf")
        incoming = [edge for edge in graph["edges"] if edge["to"] == leaf_index]
        self.assertEqual(len(incoming), 2)
        diff = server._diff_closure(_side(_empty_graph()), _side(graph), [upgrade])
        leaf = next(row for row in diff["added"] if row["artifactId"] == "leaf")
        self.assertEqual(leaf["path"], [
            _gav("com.acme", "root", "2.0.0"),
            _gav("com.acme", "leaf", "1.0.0"),
        ])
        self.assertNotIn("pathOmitted", leaf)

        def chain(length):
            nodes = [_gav("com.acme", "n%d" % i, "1") for i in range(length)]
            edges = [{"from": i, "to": i + 1} for i in range(length - 1)]
            return {"nodes": nodes, "edges": edges}

        short = server._diff_closure(
            _side(_empty_graph()), _side(chain(8)),
            [_upgrade("com.acme", "n0", "0.9", "1")],
        )
        boundary = next(row for row in short["added"] if row["artifactId"] == "n7")
        self.assertEqual(len(boundary["path"]), 8)
        self.assertNotIn("pathOmitted", boundary)
        self.assertEqual(boundary["path"][-1]["artifactId"], "n7")

        long = server._diff_closure(
            _side(_empty_graph()), _side(chain(9)),
            [_upgrade("com.acme", "n0", "0.9", "1")],
        )
        leaf = next(row for row in long["added"] if row["artifactId"] == "n8")
        self.assertEqual(
            [node["artifactId"] for node in leaf["path"]],
            ["n0", "n1", "n2", "n8"],
        )
        self.assertEqual(leaf["path"][-1], _gav("com.acme", "n8", "1"))
        self.assertEqual(leaf["pathOmitted"], 5)
        self.assertNotIn("n4", [node["artifactId"] for node in leaf["path"]])

    def test_tc8_target_is_excluded_but_stays_a_bfs_root(self):
        upgrade = _upgrade("com.acme", "lib", "1.0.0", "2.0.0")
        before = server._normalise_closure_graph([_tree("app", "runtimeClasspath", [
            _project(),
            _gav("com.acme", "lib", "1.0.0"),
            _gav("com.acme", "kept", "1.0.0"),
        ], [{"from": 0, "to": 1}, {"from": 1, "to": 2}])])
        after = server._normalise_closure_graph([_tree("app", "runtimeClasspath", [
            _project(),
            _gav("com.acme", "lib", "2.0.0"),
            _gav("com.acme", "kept", "1.0.0"),
            _gav("com.acme", "newlib", "3.0.0"),
        ], [{"from": 0, "to": 1}, {"from": 1, "to": 2}, {"from": 1, "to": 3}])])
        self.assertIn(
            ("com.acme", "lib", "2.0.0"),
            [(node.get("groupId"), node.get("artifactId"), node.get("version")) for node in after["nodes"]],
        )
        diff = server._diff_closure(_side(before), _side(after), [upgrade])
        artifacts = []
        for bucket in ("added", "changed", "removed"):
            artifacts.extend(row["artifactId"] for row in diff[bucket])
        self.assertNotIn("lib", artifacts)
        self.assertEqual([row["artifactId"] for row in diff["added"]], ["newlib"])
        self.assertEqual(diff["added"][0]["path"][0], _gav("com.acme", "lib", "2.0.0"))
        self.assertEqual(diff["added"][0]["path"][-1], _gav("com.acme", "newlib", "3.0.0"))
        self.assertFalse(diff["added"][0]["sideEffect"])

    def test_tc9_added_and_removed_use_version_sets(self):
        upgrade = _upgrade("com.acme", "lib", "1.0.0", "2.0.0")
        before = server._normalise_closure_graph([
            _tree("app", "runtimeClasspath", [_project(), _gav("com.acme", "demo", "1.0.0")], [{"from": 0, "to": 1}]),
            _tree("app", "releaseRuntimeClasspath", [_project(), _gav("com.acme", "demo", "2.0.0")], [{"from": 0, "to": 1}]),
            _tree("app", "runtimeClasspath", [_project(), _gav("com.acme", "moved", "1.0.0")], [{"from": 0, "to": 1}]),
        ])
        after = server._normalise_closure_graph([
            _tree("app", "runtimeClasspath", [_project(), _gav("com.acme", "other", "3.0.0")], [{"from": 0, "to": 1}]),
            _tree("lib", "runtimeClasspath", [_project(), _gav("com.acme", "other", "4.0.0")], [{"from": 0, "to": 1}]),
            _tree("app", "runtimeClasspath", [
                _project(),
                _gav("com.acme", "moved", "1.0.0"),
            ], [{"from": 0, "to": 1}]),
            _tree("lib", "runtimeClasspath", [_project(), _gav("com.acme", "moved", "2.0.0")], [{"from": 0, "to": 1}]),
        ])
        diff = server._diff_closure(_side(before), _side(after), [upgrade])
        added = next(row for row in diff["added"] if row["artifactId"] == "other")
        removed = next(row for row in diff["removed"] if row["artifactId"] == "demo")
        changed = next(row for row in diff["changed"] if row["artifactId"] == "moved")
        self.assertEqual(added["versions"], ["3.0.0", "4.0.0"])
        self.assertIsInstance(added["versions"], list)
        self.assertNotIn("version", added)
        self.assertEqual(len(added["usages"]), 2)
        self.assertEqual(removed["versions"], ["1.0.0", "2.0.0"])
        self.assertIsInstance(removed["versions"], list)
        self.assertNotIn("version", removed)
        self.assertEqual(changed["fromVersions"], ["1.0.0"])
        self.assertEqual(changed["toVersions"], ["1.0.0", "2.0.0"])

    def test_depsdev_path_omits_side_effect(self):
        upgrade = _upgrade("com.acme", "lib", "1.0.0", "2.0.0")
        after = {
            "nodes": [
                _gav("com.acme", "lib", "2.0.0"),
                _gav("com.acme", "extra", "1.0.0"),
            ],
            "edges": [{"from": 0, "to": 1}],
        }
        diff = server._diff_closure(_side(_empty_graph()), _side(after), [upgrade], project_graph=False)
        self.assertNotIn("sideEffect", diff["added"][0])
        self.assertEqual(diff["added"][0]["path"][0]["artifactId"], "lib")

    def test_row_cap_keeps_full_counts(self):
        upgrade = _upgrade("com.acme", "lib", "1.0.0", "2.0.0")
        nodes = [_gav("com.acme", "lib", "2.0.0")]
        nodes.extend(_gav("com.acme", "a%03d" % i, "1") for i in range(101))
        diff = server._diff_closure(_side(_empty_graph()), _side({"nodes": nodes, "edges": []}), [upgrade])
        self.assertEqual(diff["summary"]["added"], 101)
        self.assertEqual(len(diff["added"]), 100)
        self.assertTrue(diff["dependenciesTruncated"])
        self.assertNotIn("a100", [row["artifactId"] for row in diff["added"]])
        self.assertEqual(server.MAX_UPGRADE_DIFF_ROWS, 100)
        self.assertEqual(server.MAX_UPGRADE_SUBSTITUTIONS, 20)
        ranked = server._advisory_for_upgrade(
            diff_reliable=True,
            diff=diff,
            targets=[_clean_target()],
            vulnerabilities=_empty_vulns(),
        )
        self.assertEqual(ranked["advisory"], "review")
        self.assertEqual(len(ranked["dependencies"]["added"]), 100)


class VulnDeltaTest(unittest.TestCase):
    def _bomb(self):
        return unittest.mock.patch("urllib.request.urlopen", side_effect=AssertionError("network"))

    def test_tc10_severity_ranks_without_moving_buckets(self):
        upgrade = _upgrade("com.acme", "lib", "1.0.0", "2.0.0")
        records = [
            _osv("com.acme", "lib", "1.0.0"),
            _osv("com.acme", "lib", "2.0.0"),
        ]

        def rank(diff, extra_records):
            classified = server._classify_vuln_delta([upgrade], diff, records + extra_records)
            ranked = server._advisory_for_upgrade(
                diff_reliable=True,
                diff=diff,
                targets=classified["targets"],
                vulnerabilities=classified["vulnerabilities"],
                fixes_incomplete=classified["fixesIncomplete"],
            )
            return classified, ranked

        with self._bomb():
            classified, ranked = rank(
                {"added": [{
                    "groupId": "com.acme", "artifactId": "new", "versions": ["1.0.0"],
                }], "changed": [], "removed": [], "summary": {"added": 1, "changed": 0, "removed": 0, "unchanged": 0}},
                [_osv("com.acme", "new", "1.0.0", [_vuln("CVE-BARE")])],
            )
        self.assertEqual(ranked["advisory"], "review")
        self.assertEqual(
            [item["id"] for item in classified["vulnerabilities"]["introduced"]],
            ["CVE-BARE"],
        )
        self.assertNotIn("severity", classified["vulnerabilities"]["introduced"][0])

        changed = {
            "added": [],
            "changed": [{
                "groupId": "com.acme",
                "artifactId": "widget",
                "fromVersions": ["1.0.0"],
                "toVersions": ["2.0.0"],
            }],
            "removed": [],
            "summary": {"added": 0, "changed": 1, "removed": 0, "unchanged": 0},
        }
        for severity, expect in (("CRITICAL", "review"), ("HIGH", "review"), ("MEDIUM", "info"), ("LOW", "info")):
            with self._bomb():
                classified, ranked = rank(changed, [
                    _osv("com.acme", "widget", "1.0.0", [_vuln("CVE-R", severity=severity)]),
                    _osv("com.acme", "widget", "2.0.0", [_vuln("CVE-R", severity=severity)]),
                ])
            self.assertEqual(ranked["advisory"], expect, severity)
            self.assertEqual(
                [item["id"] for item in classified["vulnerabilities"]["remaining"]],
                ["CVE-R"],
            )
            self.assertEqual(classified["vulnerabilities"]["introduced"], [])
            self.assertEqual(classified["vulnerabilities"]["remaining"][0]["relation"], "remaining")

        removed = {
            "added": [],
            "changed": [],
            "removed": [{
                "groupId": "com.acme", "artifactId": "old", "versions": ["1.0.0"],
            }],
            "summary": {"added": 0, "changed": 0, "removed": 1, "unchanged": 0},
        }
        with self._bomb():
            classified, ranked = rank(
                removed,
                [_osv("com.acme", "old", "1.0.0", [_vuln("CVE-F", severity="LOW")])],
            )
        self.assertEqual(ranked["advisory"], "info")
        self.assertEqual(
            [item["id"] for item in classified["vulnerabilities"]["fixed"]],
            ["CVE-F"],
        )

    def test_tc11_decision_fields_are_local(self):
        upgrade = _upgrade("com.acme", "lib", "1.0.0", "1.2.0")
        below = _vuln("CVE-BELOW", severity="HIGH", fixed="1.5.0", summary="parser overflow")
        above = _vuln("CVE-ABOVE", severity="MEDIUM", fixed="1.5.0", summary="cleared upstream")
        gone = _vuln("CVE-GONE", severity="LOW", fixed="9.0.0", summary="no longer selected")
        diff = {
            "added": [
                {
                    "groupId": "com.acme",
                    "artifactId": "below",
                    "versions": ["1.2.0"],
                    "path": [
                        _gav("com.acme", "lib", "1.2.0"),
                        _gav("com.acme", "below", "1.2.0"),
                    ],
                    "pathOmitted": 2,
                },
                {
                    "groupId": "com.acme",
                    "artifactId": "above",
                    "versions": ["2.0.0"],
                },
            ],
            "changed": [],
            "removed": [{
                "groupId": "com.acme",
                "artifactId": "gone",
                "versions": ["1.0.0"],
            }],
            "summary": {"added": 2, "changed": 0, "removed": 1, "unchanged": 0},
        }
        with self._bomb():
            classified = server._classify_vuln_delta([upgrade], diff, [
                _osv("com.acme", "lib", "1.0.0"),
                _osv("com.acme", "lib", "1.2.0", [_vuln("CVE-TARGET", severity="HIGH", fixed="9.0.0", summary="on the candidate")]),
                _osv("com.acme", "below", "1.2.0", [below]),
                _osv("com.acme", "above", "2.0.0", [above]),
                _osv("com.acme", "gone", "1.0.0", [gone]),
            ])
        by_id = {}
        for bucket in classified["vulnerabilities"].values():
            for item in bucket:
                by_id[item["id"]] = item
        low = by_id["CVE-BELOW"]
        self.assertEqual(low["relation"], "introduced")
        self.assertEqual(low["vulnerableNode"], _gav("com.acme", "below", "1.2.0"))
        self.assertEqual(low["summary"], "parser overflow")
        self.assertEqual(low["url"], "https://osv.dev/vulnerability/CVE-BELOW")
        self.assertEqual(low["fixedVersion"], "1.5.0")
        self.assertFalse(low["clearedBySelection"])
        self.assertEqual(
            low["clearedBySelection"],
            server.compare_versions("1.2.0", "1.5.0") >= 0,
        )
        self.assertEqual(low["path"][0]["artifactId"], "lib")
        self.assertEqual(low["pathOmitted"], 2)
        high = by_id["CVE-ABOVE"]
        self.assertTrue(high["clearedBySelection"])
        self.assertEqual(
            high["clearedBySelection"],
            server.compare_versions("2.0.0", "1.5.0") >= 0,
        )
        fixed = by_id["CVE-GONE"]
        self.assertEqual(fixed["relation"], "fixed")
        self.assertEqual(fixed["vulnerableNode"]["version"], "1.0.0")
        self.assertNotIn("clearedBySelection", fixed)
        target_item = classified["targets"][0]["vulnerabilities"][0]
        self.assertEqual(target_item["relation"], "target")
        self.assertEqual(target_item["vulnerableNode"]["version"], "1.2.0")
        self.assertIn("clearedBySelection", target_item)
        self.assertNotIn("path", target_item)
        self.assertNotIn("CVE-TARGET", by_id)

    def test_tc12_safe_upgrade_only_on_target_to_version(self):
        upgrades = [
            _upgrade("com.acme", "lib", "1.0.0", "2.0.0"),
            _upgrade("com.acme", "empty", "1.0.0", "2.0.0"),
        ]
        target_vuln = _vuln("CVE-T", severity="HIGH", fixed="3.0.0")
        transitive = _vuln("CVE-X", severity="HIGH", fixed="9.9.9")
        diff = {
            "added": [{
                "groupId": "com.acme", "artifactId": "extra", "versions": ["1.0.0"],
            }],
            "changed": [],
            "removed": [],
            "summary": {"added": 1, "changed": 0, "removed": 0, "unchanged": 0},
        }
        with self._bomb(), unittest.mock.patch.object(
            server, "_compute_safe_upgrade", wraps=server._compute_safe_upgrade,
        ) as spy:
            classified = server._classify_vuln_delta(upgrades, diff, [
                _osv("com.acme", "lib", "1.0.0"),
                _osv("com.acme", "lib", "2.0.0", [target_vuln]),
                _osv("com.acme", "empty", "1.0.0"),
                _osv("com.acme", "empty", "2.0.0"),
                _osv("com.acme", "extra", "1.0.0", [transitive]),
            ])
        self.assertEqual(spy.call_count, 1)
        self.assertEqual([item["id"] for item in spy.call_args.args[0]], ["CVE-T"])
        self.assertEqual(classified["targets"][0]["safeUpgrade"], {
            "version": "3.0.0",
            "fixesAllKnown": True,
        })
        self.assertNotIn("safeUpgrade", classified["targets"][1])
        self.assertEqual(classified["targets"][1]["vulnerabilities"], [])

    def test_tc13_uncompared_before_version_and_malicious_stop(self):
        upgrade = _upgrade("com.acme", "lib", "1.0.0", "2.0.0")
        diff = {
            "added": [],
            "changed": [{
                "groupId": "com.acme",
                "artifactId": "widget",
                "fromVersions": ["1.0.0"],
                "toVersions": ["2.0.0"],
            }],
            "removed": [],
            "summary": {"added": 0, "changed": 1, "removed": 0, "unchanged": 0},
        }
        mal = _vuln("MAL-2025-9", malicious=True, summary="malicious package")
        plain = _vuln("CVE-NEW", summary="seen only after")
        with self._bomb():
            classified = server._classify_vuln_delta([upgrade], diff, [
                _osv("com.acme", "lib", "1.0.0"),
                _osv("com.acme", "lib", "2.0.0"),
                _osv("com.acme", "widget", "2.0.0", [plain, mal]),
            ])
        buckets = classified["vulnerabilities"]
        self.assertEqual(buckets["introduced"], [])
        self.assertEqual(buckets["fixed"], [])
        self.assertEqual(
            sorted(item["id"] for item in buckets["uncompared"]),
            ["CVE-NEW", "MAL-2025-9"],
        )
        for item in buckets["uncompared"]:
            self.assertEqual(item["relation"], "uncompared")
            self.assertEqual(item["reason"], "before-version not queried")
            self.assertNotEqual(item["relation"], "introduced")
        self.assertTrue(classified["fixesIncomplete"])
        mal_item = next(item for item in buckets["uncompared"] if item["id"] == "MAL-2025-9")
        self.assertTrue(mal_item["malicious"])
        ranked = server._advisory_for_upgrade(
            diff_reliable=True,
            diff=diff,
            targets=classified["targets"],
            vulnerabilities=buckets,
            fixes_incomplete=True,
        )
        self.assertEqual(ranked["advisory"], "stop")
        self.assertEqual(
            [item["id"] for item in ranked["vulnerabilities"]["uncompared"]],
            ["CVE-NEW", "MAL-2025-9"],
        )

        with self._bomb():
            classified = server._classify_vuln_delta([upgrade], diff, [
                _osv("com.acme", "lib", "1.0.0"),
                _osv("com.acme", "lib", "2.0.0"),
                _osv("com.acme", "widget", "2.0.0", [plain]),
            ])
        self.assertEqual(classified["vulnerabilities"]["introduced"], [])
        self.assertEqual(classified["vulnerabilities"]["fixed"], [])
        self.assertEqual(classified["vulnerabilities"]["uncompared"][0]["id"], "CVE-NEW")
        ranked = server._advisory_for_upgrade(
            diff_reliable=True,
            diff=diff,
            targets=classified["targets"],
            vulnerabilities=classified["vulnerabilities"],
            fixes_incomplete=classified["fixesIncomplete"],
        )
        self.assertEqual(ranked["advisory"], "review")
        self.assertNotEqual(ranked["advisory"], "stop")


class LicenseAndSelectionTest(unittest.TestCase):
    def test_tc14_order_cap_and_rank(self):
        self.assertEqual(server.MAX_UPGRADE_LICENSE_NODES, 40)
        upgrade = _upgrade("com.acme", "lib", "1.0.0", "2.0.0")
        added = [
            {"groupId": "com.acme", "artifactId": "a%02d" % i, "versions": ["1"]}
            for i in range(2)
        ]
        diff = {
            "added": added,
            "changed": [{
                "groupId": "com.acme",
                "artifactId": "changed",
                "fromVersions": ["1"],
                "toVersions": ["2"],
            }],
            "removed": [{
                "groupId": "com.acme", "artifactId": "removed", "versions": ["1"],
            }],
            "summary": {"added": 2, "changed": 1, "removed": 1, "unchanged": 0},
        }
        selected = server._select_delta_gavs(
            [upgrade], diff, include_targets=False, cap=server.MAX_UPGRADE_LICENSE_NODES,
        )
        self.assertEqual(
            [(row["artifactId"], row["version"]) for row in selected["gavs"]],
            [("a00", "1"), ("a01", "1"), ("changed", "2"), ("changed", "1"), ("removed", "1")],
        )
        self.assertFalse(selected["truncated"])
        capped = server._select_delta_gavs([upgrade], diff, include_targets=False, cap=3)
        self.assertEqual(
            [(row["artifactId"], row["version"]) for row in capped["gavs"]],
            [("a00", "1"), ("a01", "1"), ("changed", "2")],
        )
        self.assertTrue(capped["truncated"])

        many = {
            "added": [
                {"groupId": "com.acme", "artifactId": "a%02d" % i, "versions": ["1"]}
                for i in range(38)
            ],
            "changed": diff["changed"],
            "removed": diff["removed"],
        }
        wide = server._select_delta_gavs(
            [], many, include_targets=False, cap=server.MAX_UPGRADE_LICENSE_NODES,
        )
        self.assertEqual(len(wide["gavs"]), 40)
        self.assertTrue(wide["truncated"])
        # 38 added + changed-to + changed-from fill the cap; removed is the tail.
        self.assertEqual(wide["gavs"][0]["artifactId"], "a00")
        self.assertEqual((wide["gavs"][-1]["artifactId"], wide["gavs"][-1]["version"]), ("changed", "1"))
        self.assertNotIn(("removed", "1"), [(row["artifactId"], row["version"]) for row in wide["gavs"]])

        reserved = server._select_delta_gavs(
            [
                _upgrade("com.acme", "one", "1", "2"),
                _upgrade("com.acme", "two", "3", "4"),
            ],
            diff,
            cap=3,
        )
        self.assertEqual(
            [(row["artifactId"], row["version"]) for row in reserved["gavs"]],
            [("one", "2"), ("two", "4"), ("one", "1"), ("two", "3")],
        )
        self.assertTrue(reserved["truncated"])

        def fetched(artifact_id, version, licenses):
            return {
                "groupId": "com.acme",
                "artifactId": artifact_id,
                "version": version,
                "ok": True,
                "licenses": licenses,
            }

        complete = [
            fetched("a00", "1", ["MIT"]),
            fetched("a01", "1", ["Apache-2.0"]),
            fetched("changed", "2", ["GPL-3.0-only"]),
            fetched("changed", "1", ["MIT"]),
            fetched("removed", "1", ["MIT"]),
        ]
        with unittest.mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            licensed = server._license_delta(diff, complete)
        self.assertEqual(
            [(row["artifactId"], row["version"]) for row in licensed["appeared"]],
            [("a00", "1"), ("a01", "1"), ("changed", "2")],
        )
        self.assertEqual(
            [(row["artifactId"], row["version"]) for row in licensed["disappeared"]],
            [("changed", "1"), ("removed", "1")],
        )
        self.assertEqual(licensed["categoriesIntroduced"], ["strong-copyleft"])
        gpl = next(row for row in licensed["appeared"] if row["artifactId"] == "changed")
        self.assertEqual(gpl["verdict"], "violation")
        self.assertEqual(gpl["category"], "strong-copyleft")
        self.assertIn("spdxId", gpl)
        self.assertNotIn("verdict", licensed["disappeared"][0])

        for drop in (("changed", "1"), ("removed", "1")):
            partial = [row for row in complete if (row["artifactId"], row["version"]) != drop]
            licensed = server._license_delta(diff, partial)
            self.assertNotIn("categoriesIntroduced", licensed)
            self.assertTrue(licensed["appeared"])

        unknown = server._license_delta(diff, [
            fetched("a00", "1", []),
            fetched("a01", "1", ["MIT"]),
            fetched("changed", "2", ["MIT"]),
            fetched("changed", "1", ["MIT"]),
            fetched("removed", "1", ["MIT"]),
        ])
        self.assertEqual(unknown["appeared"][0]["verdict"], "review")

        review_only = server._advisory_for_upgrade(
            diff_reliable=True,
            diff={"added": [], "changed": [], "removed": [], "summary": {"added": 0, "changed": 0, "removed": 0, "unchanged": 0}},
            targets=[_clean_target()],
            vulnerabilities=_empty_vulns(),
            license_delta={"appeared": [unknown["appeared"][0]], "disappeared": []},
        )
        self.assertEqual(review_only["advisory"], "info")
        violation = server._advisory_for_upgrade(
            diff_reliable=True,
            diff={"added": [], "changed": [], "removed": [], "summary": {"added": 0, "changed": 0, "removed": 0, "unchanged": 0}},
            targets=[_clean_target()],
            vulnerabilities=_empty_vulns(),
            license_delta={"appeared": [gpl], "disappeared": []},
        )
        self.assertEqual(violation["advisory"], "review")

    def test_tool_registry_unchanged(self):
        self.assertEqual(len(server.TOOLS), 20)


if __name__ == "__main__":
    unittest.main()
