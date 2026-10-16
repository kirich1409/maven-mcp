"""Line-matched OSV fixedVersion / safeUpgrade / clearedBySelection.

The first-range walk returns the first ``events[].fixed`` of the first
ECOSYSTEM range of the first ``affected`` entry. These fixtures put that
wrong ``fixed`` first, on a disjoint line, so that walk fails here.
No network: querybatch bare ids, then GET /v1/vulns/{id}, same seam as
tests/test_maven_search_osv.py.
"""

import json
import unittest
import unittest.mock
from pathlib import Path

from _helpers import mock_urlopen, server


def _osv_responses(advisories_per_dep):
    """querybatch body, then one GET body per unique id in first-seen order."""
    batch = {
        "results": [
            {
                "vulns": [
                    {"id": adv["id"], "modified": "2024-01-01T00:00:00Z"}
                    for adv in advs
                ],
            }
            for advs in advisories_per_dep
        ],
    }
    responses = [(200, json.dumps(batch).encode())]
    seen = []
    for advs in advisories_per_dep:
        for adv in advs:
            if adv["id"] in seen:
                continue
            seen.append(adv["id"])
            responses.append((200, json.dumps(adv).encode()))
    return responses


def _maven(group_id, artifact_id, ranges, versions=None):
    entry = {
        "package": {"ecosystem": "Maven", "name": "%s:%s" % (group_id, artifact_id)},
        "ranges": [
            {"type": "ECOSYSTEM", "events": events}
            for events in ranges
        ],
    }
    if versions is not None:
        entry["versions"] = versions
    return entry


def _opened(introduced, fixed=None, last_affected=None, limit=None):
    events = [{"introduced": introduced}]
    if fixed is not None:
        events.append({"fixed": fixed})
    if last_affected is not None:
        events.append({"last_affected": last_affected})
    if limit is not None:
        events.append({"limit": limit})
    return events


def _adv(vuln_id, affected):
    if isinstance(affected, dict):
        affected = [affected]
    return {
        "id": vuln_id,
        "summary": vuln_id,
        "affected": affected,
        "references": [{
            "type": "ADVISORY",
            "url": "https://osv.dev/vulnerability/%s" % vuln_id,
        }],
    }


def _dep(group_id, artifact_id, version):
    return {"groupId": group_id, "artifactId": artifact_id, "version": version}


SPRING_WEB = ("org.springframework", "spring-web")
PAX = ("org.ops4j.pax.logging", "pax-logging-log4j2")
BOOT = ("org.springframework.boot", "spring-boot-autoconfigure")
LOG4J = ("org.apache.logging.log4j", "log4j-core")


def _ghsa_2rmj():
    # First fixed is 5.3.38. 6.1.11 is on the 6.1 line, fixed in 6.1.12,
    # and is listed in versions. The old walk returns 5.3.38.
    return _adv("GHSA-2rmj-mq67-h97g", _maven(
        *SPRING_WEB,
        [
            _opened("5.3.0", fixed="5.3.38"),
            _opened("6.0.0", fixed="6.0.23"),
            _opened("6.1.0", fixed="6.1.12"),
        ],
        versions=["6.1.11"],
    ))


def _ghsa_jfh8():
    # First affected entry is a different artifact. The old walk returns 2.15.0.
    return _adv("GHSA-jfh8-c2jp-5v3q", [
        _maven(*LOG4J, [_opened("2.0.0", fixed="2.15.0")]),
        _maven(*PAX, [_opened("1.8.0", fixed="1.9.2")]),
    ])


def _ghsa_xxfh():
    # First range on the queried package is a later line. 1.8.5 is in the
    # later range. The old walk returns 2.0.11.
    return _adv("GHSA-xxfh-x98p-j8fr", _maven(*PAX, [
        _opened("2.0.0", fixed="2.0.11"),
        _opened("0", fixed="1.11.10"),
    ]))


def _ghsa_xxfh_one_events_array():
    return _adv("GHSA-xxfh-one-array", _maven(*PAX, [[
        {"introduced": "2.0.0"},
        {"fixed": "2.0.11"},
        {"introduced": "0"},
        {"fixed": "1.11.10"},
    ]]))


def _ghsa_xf96():
    return _adv("GHSA-xf96-w227-r7c4", _maven(*BOOT, [
        _opened("3.0.0", fixed="3.0.7"),
        _opened("2.7.0", fixed="2.7.12"),
    ]))


def _ghsa_6r3c():
    # First fixed is 6.2.8. 6.0.22 is last_affected with no fixed. A first
    # range of introduced 0 / fixed 6.2.8 would contain 6.0.22 and hide the
    # omission, so the lines stay disjoint.
    return _adv("GHSA-6r3c-xf4w-jxjm", _maven(*SPRING_WEB, [
        _opened("6.2.0", fixed="6.2.8"),
        _opened("6.1.0", fixed="6.1.21"),
        _opened("6.0.5", last_affected="6.0.23"),
    ]))


def _ghsa_ggg2():
    return _adv("GHSA-ggg2-9786-hwc8", _maven(*BOOT, [
        _opened("4.0.0", fixed="4.0.7"),
        _opened("2.7.0", last_affected="2.7.33"),
    ]))


def _boot_27_fix():
    return _adv("GHSA-boot-2-7-12", _maven(*BOOT, [
        _opened("2.7.0", fixed="2.7.12"),
    ]))


def _pax_third_with_log4j_first():
    # First entry is log4j-core 2.16.0, but a pax interval still contains
    # 1.8.5 and is fixed at 1.9.2. A log4j-only document is the check-6
    # shape (no line-matched fix), not this one.
    return _adv("GHSA-7rjr-shaped", [
        _maven(*LOG4J, [_opened("2.0.0", fixed="2.16.0")]),
        _maven(*PAX, [_opened("1.8.0", fixed="1.9.2")]),
    ])


def _log4j_only():
    return _adv("GHSA-log4j-only", [
        _maven(*LOG4J, [_opened("2.0.0", fixed="2.16.0")]),
    ])


class LineFixedVersionTest(unittest.TestCase):
    def _query(self, group_id, artifact_id, version, advisories):
        with unittest.mock.patch(
            "urllib.request.urlopen",
            side_effect=mock_urlopen(_osv_responses([advisories])),
        ):
            return server.query_osv_batch([_dep(group_id, artifact_id, version)])

    def _vuln(self, group_id, artifact_id, version, advisories):
        results = self._query(group_id, artifact_id, version, advisories)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(results[0]["vulnerabilities"]), len(advisories))
        return results[0]["vulnerabilities"]

    def test_ghsa_2rmj_matches_the_line_that_contains_the_version(self):
        for version, expected in (("6.1.11", "6.1.12"), ("5.3.37", "5.3.38")):
            with self.subTest(version=version):
                vuln = self._vuln(*SPRING_WEB, version, [_ghsa_2rmj()])[0]
                self.assertEqual(vuln["fixedVersion"], expected)
                self.assertNotEqual(
                    vuln["fixedVersion"],
                    "5.3.38" if version == "6.1.11" else "6.1.12",
                )
                self.assertIn("_lineMatch", vuln)
                self.assertIn("6.1.11", vuln["_lineMatch"]["versions"])

    def test_ghsa_jfh8_ignores_the_other_package(self):
        vuln = self._vuln(*PAX, "1.8.5", [_ghsa_jfh8()])[0]
        self.assertEqual(vuln["fixedVersion"], "1.9.2")
        self.assertNotEqual(vuln["fixedVersion"], "2.15.0")
        stored = [
            bound
            for rng in vuln["_lineMatch"]["ranges"]
            for event in rng["events"]
            for bound in event.values()
        ]
        self.assertIn("1.9.2", stored)
        self.assertNotIn("2.15.0", stored)

    def test_non_matching_package_omits_fixed_version_and_line_match(self):
        # Packages are present, none is pax. No first-fixed fallback.
        vuln = self._vuln(*PAX, "1.8.5", [_log4j_only()])[0]
        self.assertNotIn("fixedVersion", vuln)
        self.assertNotIn("_lineMatch", vuln)

    def test_ghsa_xxfh_uses_the_containing_range_not_the_first_same_package_fixed(self):
        vuln = self._vuln(*PAX, "1.8.5", [_ghsa_xxfh()])[0]
        self.assertEqual(vuln["fixedVersion"], "1.11.10")
        self.assertNotEqual(vuln["fixedVersion"], "2.0.11")

    def test_later_pair_in_the_same_events_array_is_the_fix(self):
        vuln = self._vuln(*PAX, "1.8.5", [_ghsa_xxfh_one_events_array()])[0]
        self.assertEqual(vuln["fixedVersion"], "1.11.10")
        self.assertNotEqual(vuln["fixedVersion"], "2.0.11")

    def test_leading_fixed_does_not_invent_introduced_zero(self):
        adv = _adv("GHSA-leading-fixed", _maven(*PAX, [[
            {"fixed": "2.0.11"},
            {"introduced": "1.8.0"},
            {"fixed": "1.9.2"},
        ]]))
        vuln = self._vuln(*PAX, "1.8.5", [adv])[0]
        self.assertEqual(vuln["fixedVersion"], "1.9.2")
        self.assertNotEqual(vuln["fixedVersion"], "2.0.11")

    def test_ghsa_xf96_uses_the_2_7_line(self):
        vuln = self._vuln(*BOOT, "2.7.11", [_ghsa_xf96()])[0]
        self.assertEqual(vuln["fixedVersion"], "2.7.12")
        self.assertNotEqual(vuln["fixedVersion"], "3.0.7")

    def test_ghsa_6r3c_line_match_and_last_affected_omission(self):
        fixed = self._vuln(*SPRING_WEB, "6.1.11", [_ghsa_6r3c()])[0]
        self.assertEqual(fixed["fixedVersion"], "6.1.21")
        self.assertNotEqual(fixed["fixedVersion"], "6.2.8")

        omitted = self._vuln(*SPRING_WEB, "6.0.22", [_ghsa_6r3c()])[0]
        self.assertNotIn("fixedVersion", omitted)
        self.assertIn("_lineMatch", omitted)

    def test_ghsa_ggg2_omits_fixed_version(self):
        vuln = self._vuln(*BOOT, "2.7.11", [_ghsa_ggg2()])[0]
        self.assertNotIn("fixedVersion", vuln)
        self.assertIn("_lineMatch", vuln)

    def test_versions_list_does_not_borrow_a_non_containing_fixed(self):
        adv = _adv("GHSA-versions-only", _maven(
            *SPRING_WEB,
            [_opened("6.2.0", fixed="6.2.8")],
            versions=["6.1.11"],
        ))
        vuln = self._vuln(*SPRING_WEB, "6.1.11", [adv])[0]
        self.assertNotIn("fixedVersion", vuln)
        self.assertEqual(vuln["_lineMatch"]["versions"], ["6.1.11"])

    def test_limit_interval_does_not_borrow_a_later_line_fixed(self):
        adv = _adv("GHSA-limit", _maven(*SPRING_WEB, [
            _opened("6.0.0", limit="6.1.0"),
            _opened("6.1.0", fixed="6.1.12"),
        ]))
        vuln = self._vuln(*SPRING_WEB, "6.0.22", [adv])[0]
        self.assertNotIn("fixedVersion", vuln)

    def test_any_unfixed_containing_interval_omits_fixed_version(self):
        # 1.2.0 sits in a last_affected interval and in a later fixed interval.
        # Any unfixed containing interval wins: do not borrow the other fix.
        adv = _adv("GHSA-mixed-contain", _maven(*SPRING_WEB, [
            _opened("1.0.0", last_affected="2.0.0"),
            _opened("1.0.0", fixed="1.9.0"),
        ]))
        vuln = self._vuln(*SPRING_WEB, "1.2.0", [adv])[0]
        self.assertNotIn("fixedVersion", vuln)
        self.assertIn("_lineMatch", vuln)

    def test_several_containing_intervals_use_the_highest_fixed(self):
        adv = _adv("GHSA-overlap", _maven(*SPRING_WEB, [
            _opened("1.0.0", fixed="1.5.0"),
            _opened("1.0.0", fixed="1.9.0"),
        ]))
        vuln = self._vuln(*SPRING_WEB, "1.2.0", [adv])[0]
        self.assertEqual(vuln["fixedVersion"], "1.9.0")

    def test_package_less_document_still_uses_containment(self):
        # Existing single-range fixtures, plus a later containing range so a
        # no-package branch that returns the first fixed fails here.
        single = _adv("GHSA-packageless", [{
            "ranges": [{"type": "ECOSYSTEM", "events": [
                {"introduced": "0"}, {"fixed": "1.2.3"},
            ]}],
        }])
        vuln = self._vuln("com.example", "lib", "1.0.0", [single])[0]
        self.assertEqual(vuln["fixedVersion"], "1.2.3")
        self.assertIn("_lineMatch", vuln)

        other = _adv("GHSA-packageless-201", [{
            "ranges": [{"type": "ECOSYSTEM", "events": [
                {"introduced": "0"}, {"fixed": "2.0.1"},
            ]}],
        }])
        vuln = self._vuln("com.example", "lib", "1.5.0", [other])[0]
        self.assertEqual(vuln["fixedVersion"], "2.0.1")

        later = _adv("GHSA-packageless-later", [{
            "ranges": [
                {"type": "ECOSYSTEM", "events": [
                    {"introduced": "9.0.0"}, {"fixed": "9.9.9"},
                ]},
                {"type": "ECOSYSTEM", "events": [
                    {"introduced": "0"}, {"fixed": "1.2.3"},
                ]},
            ],
        }])
        vuln = self._vuln("com.example", "lib", "1.0.0", [later])[0]
        self.assertEqual(vuln["fixedVersion"], "1.2.3")
        self.assertNotEqual(vuln["fixedVersion"], "9.9.9")


class SafeUpgradeLineMatchTest(unittest.TestCase):
    def _handle(self, group_id, artifact_id, version, advisories):
        with unittest.mock.patch(
            "urllib.request.urlopen",
            side_effect=mock_urlopen(_osv_responses([advisories])),
        ):
            out = server.handle_get_dependency_vulnerabilities({
                "dependencies": [_dep(group_id, artifact_id, version)],
            })
        return out["results"][0]

    def test_unfixed_line_forces_fixes_all_known_false(self):
        result = self._handle(*BOOT, "2.7.11", [_ghsa_ggg2(), _boot_27_fix()])
        self.assertEqual(result["vulnerabilities"][0]["id"], "GHSA-ggg2-9786-hwc8")
        self.assertNotIn("fixedVersion", result["vulnerabilities"][0])
        self.assertEqual(result["vulnerabilities"][1]["fixedVersion"], "2.7.12")
        self.assertEqual(
            result["safeUpgrade"],
            {
                "fixesAllKnown": False,
                "reason": "no fixed version known for: GHSA-ggg2-9786-hwc8",
            },
        )
        self.assertNotIn("version", result["safeUpgrade"])

    def test_spring_web_safe_upgrade_is_the_highest_line_matched_fix(self):
        result = self._handle(*SPRING_WEB, "6.1.11", [_ghsa_2rmj(), _ghsa_6r3c()])
        self.assertEqual(result["vulnerabilities"][0]["fixedVersion"], "6.1.12")
        self.assertEqual(result["vulnerabilities"][1]["fixedVersion"], "6.1.21")
        self.assertEqual(
            result["safeUpgrade"],
            {"version": "6.1.21", "fixesAllKnown": True},
        )
        self.assertNotEqual(result["safeUpgrade"]["version"], "5.3.38")
        self.assertNotEqual(result["safeUpgrade"]["version"], "6.2.8")
        for vuln in result["vulnerabilities"]:
            self.assertNotIn("_lineMatch", vuln)

    def test_pax_safe_upgrade_ignores_a_leading_other_package_fixed(self):
        result = self._handle(*PAX, "1.8.5", [
            _ghsa_jfh8(),
            _ghsa_xxfh(),
            _pax_third_with_log4j_first(),
        ])
        fixes = [vuln["fixedVersion"] for vuln in result["vulnerabilities"]]
        self.assertEqual(fixes, ["1.9.2", "1.11.10", "1.9.2"])
        self.assertEqual(
            result["safeUpgrade"],
            {"version": "1.11.10", "fixesAllKnown": True},
        )
        self.assertNotEqual(result["safeUpgrade"]["version"], "2.16.0")
        self.assertNotEqual(result["safeUpgrade"]["version"], "2.15.0")

    def test_log4j_only_advisory_is_not_a_line_matched_fix(self):
        # A truly other-package document has no fix on the queried line, so
        # the aggregate cannot claim fixesAllKnown. The old walk would take
        # 2.16.0 and set fixesAllKnown true.
        result = self._handle(*PAX, "1.8.5", [
            _ghsa_jfh8(),
            _ghsa_xxfh(),
            _log4j_only(),
        ])
        self.assertNotIn("fixedVersion", result["vulnerabilities"][2])
        self.assertFalse(result["safeUpgrade"]["fixesAllKnown"])
        self.assertNotIn("version", result["safeUpgrade"])
        self.assertIn("GHSA-log4j-only", result["safeUpgrade"]["reason"])

    def test_zero_vulnerabilities_omit_safe_upgrade(self):
        with unittest.mock.patch(
            "urllib.request.urlopen",
            side_effect=mock_urlopen(_osv_responses([[]])),
        ):
            out = server.handle_get_dependency_vulnerabilities({
                "dependencies": [_dep("com.example", "lib", "1.0.0")],
            })
        result = out["results"][0]
        self.assertEqual(result["vulnerabilities"], [])
        self.assertNotIn("safeUpgrade", result)


class ClearedBySelectionLineMatchTest(unittest.TestCase):
    def _item(self, raw, selected, relation="target"):
        return server._upgrade_vuln_item(
            raw,
            relation=relation,
            group_id="org.springframework",
            artifact_id="spring-web",
            version=selected,
            selected_version=selected,
        )

    def test_listed_or_still_inside_is_not_cleared(self):
        listed = {
            "id": "GHSA-2rmj-mq67-h97g",
            "summary": "",
            "malicious": False,
            "fixedVersion": "5.3.38",
            "_lineMatch": {
                "ranges": [{"events": [{"introduced": "5.3.0"}, {"fixed": "5.3.38"}]}],
                "versions": ["6.1.11"],
            },
        }
        item = self._item(listed, "6.1.11")
        self.assertFalse(item["clearedBySelection"])
        self.assertNotIn("_lineMatch", item)
        self.assertGreater(server.compare_versions("6.1.11", "5.3.38"), 0)

        inside = {
            "id": "GHSA-2rmj-mq67-h97g",
            "summary": "",
            "malicious": False,
            "fixedVersion": "5.3.38",
            "_lineMatch": {
                "ranges": [{"events": [
                    {"introduced": "6.1.0"}, {"fixed": "6.1.12"},
                ]}],
                "versions": [],
            },
        }
        item = self._item(inside, "6.1.11")
        self.assertFalse(item["clearedBySelection"])
        self.assertNotIn("_lineMatch", item)

    def test_synthetic_record_without_range_data_keeps_numeric_compare(self):
        raw = {
            "id": "CVE-ABOVE",
            "summary": "",
            "malicious": False,
            "fixedVersion": "1.5.0",
        }
        item = self._item(raw, "2.0.0")
        self.assertTrue(item["clearedBySelection"])
        self.assertEqual(
            item["clearedBySelection"],
            server.compare_versions("2.0.0", "1.5.0") >= 0,
        )
        self.assertNotIn("_lineMatch", item)

    def test_outside_stored_ranges_keeps_numeric_compare(self):
        # 6.1.12 is the exclusive end of the stored interval, and it is not
        # listed. No other range contains it, so the numeric compare stands.
        raw = {
            "id": "GHSA-2rmj-mq67-h97g",
            "summary": "",
            "malicious": False,
            "fixedVersion": "5.3.38",
            "_lineMatch": {
                "ranges": [{"events": [
                    {"introduced": "6.1.0"}, {"fixed": "6.1.12"},
                ]}],
                "versions": ["6.1.11"],
            },
        }
        item = self._item(raw, "6.1.12")
        self.assertTrue(item["clearedBySelection"])
        self.assertNotIn("_lineMatch", item)

    def test_relation_fixed_omits_cleared_by_selection(self):
        raw = {
            "id": "GHSA-2rmj-mq67-h97g",
            "summary": "",
            "malicious": False,
            "fixedVersion": "5.3.38",
            "_lineMatch": {
                "ranges": [{"events": [
                    {"introduced": "6.1.0"}, {"fixed": "6.1.12"},
                ]}],
                "versions": ["6.1.11"],
            },
        }
        item = server._upgrade_vuln_item(
            raw,
            relation="fixed",
            group_id="org.springframework",
            artifact_id="spring-web",
            version="6.1.11",
            selected_version="6.1.11",
        )
        self.assertNotIn("clearedBySelection", item)
        self.assertNotIn("_lineMatch", item)


class PublicLineMatchStrippedTest(unittest.TestCase):
    def test_handler_strips_line_match_query_batch_keeps_it(self):
        advisories = [_ghsa_2rmj()]
        with unittest.mock.patch(
            "urllib.request.urlopen",
            side_effect=mock_urlopen(_osv_responses([advisories])),
        ):
            batched = server.query_osv_batch([_dep(*SPRING_WEB, "6.1.11")])
        self.assertIn("_lineMatch", batched[0]["vulnerabilities"][0])
        self.assertEqual(batched[0]["vulnerabilities"][0]["fixedVersion"], "6.1.12")

        with unittest.mock.patch(
            "urllib.request.urlopen",
            side_effect=mock_urlopen(_osv_responses([advisories])),
        ):
            out = server.handle_get_dependency_vulnerabilities({
                "dependencies": [_dep(*SPRING_WEB, "6.1.11")],
            })
        vuln = out["results"][0]["vulnerabilities"][0]
        self.assertEqual(vuln["fixedVersion"], "6.1.12")
        self.assertNotIn("_lineMatch", vuln)
        self.assertEqual(
            out["results"][0]["safeUpgrade"],
            {"version": "6.1.12", "fixesAllKnown": True},
        )

    def test_vulnerability_paths_strip_line_match(self):
        line_match = {
            "ranges": [{"events": [{"introduced": "1.0.0"}, {"fixed": "1.2.0"}]}],
            "versions": ["1.1.0"],
        }
        reachable = {
            "id": "GHSA-path",
            "summary": "path",
            "url": "https://osv.dev/vulnerability/GHSA-path",
            "malicious": False,
            "fixedVersion": "1.2.0",
            "_lineMatch": line_match,
        }
        orphan = {
            "id": "GHSA-orphan",
            "summary": "orphan",
            "url": "https://osv.dev/vulnerability/GHSA-orphan",
            "malicious": False,
            "fixedVersion": "9.1.0",
            "_lineMatch": dict(line_match),
        }
        nodes = [
            {"groupId": "com.example", "artifactId": "root", "version": "1.0",
             "relation": "SELF", "errors": []},
            {"groupId": "com.example", "artifactId": "lib", "version": "1.1.0",
             "relation": "DIRECT", "errors": []},
            {"groupId": "com.example", "artifactId": "orphan", "version": "9.0",
             "relation": "INDIRECT", "errors": []},
        ]
        graph = {
            "ok": True,
            "status": 200,
            "error": None,
            "graphError": None,
            "nodes": nodes,
            "edges": [{"from": 0, "to": 1}],
            "partial": False,
            "truncated": False,
        }
        osv_out = [
            {"groupId": "com.example", "artifactId": "root", "version": "1.0",
             "vulnerabilities": []},
            {"groupId": "com.example", "artifactId": "lib", "version": "1.1.0",
             "vulnerabilities": [reachable]},
            {"groupId": "com.example", "artifactId": "orphan", "version": "9.0",
             "vulnerabilities": [orphan]},
        ]
        with unittest.mock.patch.object(
            server, "resolve_pom_graph", return_value=graph,
        ), unittest.mock.patch.object(
            server, "query_osv_batch", return_value=osv_out,
        ):
            out = server.get_vulnerability_paths("com.example", "root", "1.0")
        self.assertNotIn("_lineMatch", out["vulnerabilityPaths"][0]["vulnerabilities"][0])
        self.assertNotIn(
            "_lineMatch",
            out["unreachableVulnerabilities"][0]["vulnerabilities"][0],
        )
        self.assertIn("_lineMatch", reachable)
        self.assertIn("_lineMatch", orphan)


class SkillLineMatchWordingTest(unittest.TestCase):
    def test_skills_recommend_the_queried_package_and_line(self):
        root = Path(__file__).resolve().parents[1]
        files = {
            "dependency-vulnerabilities": (
                root / "plugin/skills/dependency-vulnerabilities/SKILL.md"
            ),
            "check-deps-vulnerabilities": (
                root / "plugin/skills/check-deps-vulnerabilities/SKILL.md"
            ),
            "upgrade-closure": root / "plugin/skills/upgrade-closure/SKILL.md",
            "check-deps": root / "plugin/skills/check-deps/SKILL.md",
        }
        gloss = (
            "`selected version is at or above the fix`, "
            "`selected version is still below the fix`"
        )
        for name, path in files.items():
            text = " ".join(path.read_text().split())
            self.assertIn("safeUpgrade.version", text, name)
            self.assertIn("fixesAllKnown` is `true`", text, name)
            self.assertIn("queried package and the line", text, name)
            self.assertIn("ECOSYSTEM interval that contains", text, name)
            self.assertIn("Do not take the first ECOSYSTEM `fixed`", text, name)
            self.assertIn("`last_affected` is inclusive and is not a fix", text, name)
            self.assertIn("`limit` is not a fix", text, name)
        for name in ("upgrade-closure", "check-deps"):
            text = files[name].read_text()
            self.assertNotIn(gloss, text, name)
            flat = " ".join(text.split())
            self.assertIn("still inside an affected range", flat, name)
            self.assertIn("queried package and the line", flat, name)
