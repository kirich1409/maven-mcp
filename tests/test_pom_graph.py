"""POM transitive graph: Gradle cache, local Maven repo, then fetch_pom.

TC-1 … TC-20. Every case uses a temporary home. The real ~/.gradle and ~/.m2
are never read or written.
"""

import json
import os
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from _helpers import server, write_fake_gradlew


def _pom(artifact, version, body="", group="com.example"):
    return (
        "<?xml version=\"1.0\"?>"
        "<project>"
        "<modelVersion>4.0.0</modelVersion>"
        f"<groupId>{group}</groupId>"
        f"<artifactId>{artifact}</artifactId>"
        f"<version>{version}</version>"
        f"{body}"
        "</project>"
    )


def _dep(group, artifact, version=None, scope=None, optional=False, exclusions=None):
    parts = [
        f"<groupId>{group}</groupId>",
        f"<artifactId>{artifact}</artifactId>",
    ]
    if version is not None:
        parts.append(f"<version>{version}</version>")
    if scope:
        parts.append(f"<scope>{scope}</scope>")
    if optional:
        parts.append("<optional>true</optional>")
    if exclusions:
        blocks = "".join(
            "<exclusion>"
            f"<groupId>{g}</groupId><artifactId>{a}</artifactId>"
            "</exclusion>"
            for g, a in exclusions
        )
        parts.append(f"<exclusions>{blocks}</exclusions>")
    return "<dependency>" + "".join(parts) + "</dependency>"


def _deps(*items):
    return "<dependencies>" + "".join(items) + "</dependencies>"


def _dm(*items):
    return "<dependencyManagement>" + _deps(*items) + "</dependencyManagement>"


def _license(name):
    return f"<licenses><license><name>{name}</name></license></licenses>"


def _ga(nodes):
    return [(n["groupId"], n["artifactId"], n["version"], n["relation"]) for n in nodes]


class PomGraphTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        (self.home / ".gradle").mkdir()
        (self.home / ".m2" / "repository").mkdir(parents=True)
        self.project = Path(self._tmp.name) / "project"
        self.project.mkdir()
        self._env = unittest.mock.patch.dict(
            os.environ,
            {"GRADLE_USER_HOME": str(self.home / ".gradle")},
            clear=False,
        )
        self._home = unittest.mock.patch.object(server.Path, "home", return_value=self.home)
        self._env.start()
        self._home.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._home.stop)

    def ctx(self):
        return server.build_resolution_context({"projectPath": str(self.project)})

    def _gradle_pom(self, group, artifact, version, text, hashdir="abc"):
        path = (
            self.home / ".gradle" / "caches" / "modules-2" / "files-2.1"
            / group / artifact / version / hashdir / f"{artifact}-{version}.pom"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _m2_pom(self, group, artifact, version, text):
        path = (
            self.home / ".m2" / "repository" / server.group_path(group)
            / artifact / version / f"{artifact}-{version}.pom"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _fetch(self, mapping):
        calls = []

        def fetch(group_id, artifact_id, version, ctx):
            calls.append((group_id, artifact_id, version))
            return mapping.get((group_id, artifact_id, version))

        return calls, fetch

    def _resolve(self, group, artifact, version, mapping):
        calls, fetch = self._fetch(mapping)
        with unittest.mock.patch.object(server, "fetch_pom", fetch):
            out = server.resolve_pom_graph(group, artifact, version, self.ctx())
        return out, calls

    def test_tc1_direct_deps_ignore_comments(self):
        body = (
            "<!-- <dependency><groupId>com.example</groupId>"
            "<artifactId>commented</artifactId><version>9</version></dependency> -->"
            + _deps(
                _dep("com.example", "left", "1.0"),
                _dep("com.example", "right", "2.0", scope="runtime"),
            )
        )
        out, calls = self._resolve(
            "com.example", "root", "1.0",
            {
                ("com.example", "root", "1.0"): _pom("root", "1.0", body),
                ("com.example", "left", "1.0"): _pom("left", "1.0"),
                ("com.example", "right", "2.0"): _pom("right", "2.0"),
            },
        )
        self.assertTrue(out["ok"])
        self.assertFalse(out["partial"])
        self.assertEqual(_ga(out["nodes"]), [
            ("com.example", "root", "1.0", "SELF"),
            ("com.example", "left", "1.0", "DIRECT"),
            ("com.example", "right", "2.0", "DIRECT"),
        ])
        self.assertEqual(out["edges"], [
            {"from": 0, "to": 1, "requirement": "1.0"},
            {"from": 0, "to": 2, "requirement": "2.0"},
        ])
        self.assertEqual(calls, [
            ("com.example", "root", "1.0"),
            ("com.example", "left", "1.0"),
            ("com.example", "right", "2.0"),
        ])

    def test_tc2_gradle_user_home_and_sorted_pom(self):
        text_a = _pom("lib", "1.0", "FROM_A")
        text_z = _pom("lib", "1.0", "FROM_Z")
        self._gradle_pom("com.example", "lib", "1.0", text_z, hashdir="z")
        self._gradle_pom("com.example", "lib", "1.0", text_a, hashdir="a")

        def boom(*_args, **_kwargs):
            raise AssertionError("fetch_pom")

        with unittest.mock.patch.object(server, "fetch_pom", boom):
            found = server.locate_pom("com.example", "lib", "1.0", self.ctx())
        self.assertIn("FROM_A", found)
        self.assertNotIn("FROM_Z", found)

        with unittest.mock.patch.dict(os.environ, {"GRADLE_USER_HOME": ""}, clear=False):
            with unittest.mock.patch.object(server, "fetch_pom", boom):
                found = server.locate_pom("com.example", "lib", "1.0", self.ctx())
        self.assertIn("FROM_A", found)

        empty = self._gradle_pom("com.example", "empty", "1.0", "", hashdir="h")
        self.assertEqual(empty.read_text(encoding="utf-8"), "")
        with unittest.mock.patch.object(server, "fetch_pom", boom):
            out = server.resolve_pom_graph("com.example", "empty", "1.0", self.ctx())
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["nodes"]), 1)
        self.assertEqual(out["nodes"][0]["relation"], "SELF")

    def test_tc3_fetch_pom_once_when_cache_empty(self):
        out, calls = self._resolve(
            "com.example", "root", "1.0",
            {("com.example", "root", "1.0"): _pom("root", "1.0")},
        )
        self.assertTrue(out["ok"])
        self.assertEqual(calls, [("com.example", "root", "1.0")])
        self.assertEqual(len(out["nodes"]), 1)

    def test_m2_hit_skips_fetch(self):
        self._m2_pom("com.example", "lib", "1.0", _pom("lib", "1.0", "FROM_M2"))

        def boom(*_args, **_kwargs):
            raise AssertionError("fetch_pom")

        with unittest.mock.patch.object(server, "fetch_pom", boom):
            found = server.locate_pom("com.example", "lib", "1.0", self.ctx())
        self.assertIn("FROM_M2", found)

    def test_tc4_transitive_tool_does_not_call_depsdev(self):
        mapping = {
            ("com.example", "root", "1.0"): _pom(
                "root", "1.0", _deps(_dep("com.example", "leaf", "1.2")),
            ),
            ("com.example", "leaf", "1.2"): _pom("leaf", "1.2"),
        }
        calls, fetch = self._fetch(mapping)
        with unittest.mock.patch.object(server, "fetch_pom", fetch), \
                unittest.mock.patch.object(
                    server, "fetch_depsdev_dependencies",
                    side_effect=AssertionError("deps.dev"),
                ):
            out = server.get_transitive_graph(
                "com.example", "root", "1.0", self.ctx(),
            )
        self.assertEqual(
            [n["artifactId"] for n in out["nodes"]], ["root", "leaf"],
        )
        self.assertEqual(out["edges"], [{"from": 0, "to": 1}])
        self.assertFalse(out["partial"])
        self.assertFalse(out["truncated"])
        self.assertNotIn("requirement", out["edges"][0])
        self.assertEqual(calls, [
            ("com.example", "root", "1.0"),
            ("com.example", "leaf", "1.2"),
        ])

    def test_tc5_missing_root_is_not_ok(self):
        out, _calls = self._resolve("com.example", "missing", "1.0", {})
        self.assertFalse(out["ok"])
        self.assertEqual(out["nodes"], [])
        self.assertEqual(out["edges"], [])
        self.assertTrue(out["partial"])
        self.assertIn("POM not found", out["error"])

    def test_tc6_import_bom_parent_and_properties(self):
        bom = _pom(
            "bom", "1.0",
            _dm(_dep("com.example", "managed", "9.9")),
            group="org.example",
        )
        parent = _pom(
            "parent", "1.0",
            _dm(
                _dep("com.example", "from-parent", "3.0"),
                _dep("com.example", "override", "1.0"),
            ),
        )
        child = _pom(
            "root", "1.0",
            "<parent><groupId>com.example</groupId>"
            "<artifactId>parent</artifactId><version>1.0</version></parent>"
            "<properties><lib.version>5.0</lib.version></properties>"
            + _dm(
                _dep("org.example", "bom", "1.0", scope="import")
                .replace("</dependency>", "<type>pom</type></dependency>"),
                _dep("com.example", "override", "2.0"),
                _dep("com.example", "from-prop", "${lib.version}"),
            )
            + _deps(
                _dep("com.example", "managed"),
                _dep("com.example", "from-parent"),
                _dep("com.example", "override"),
                _dep("com.example", "propped", "${lib.version}"),
                _dep("com.example", "from-placeholder", "${missing}"),
            ),
        )
        # The import coordinate's type tag was inserted on the managed pin,
        # which is not a graph edge. from-placeholder is filled by the DM entry
        # above only when that entry exists; this one is the property case.
        out, calls = self._resolve("com.example", "root", "1.0", {
            ("com.example", "root", "1.0"): child,
            ("com.example", "parent", "1.0"): parent,
            ("org.example", "bom", "1.0"): bom,
        })
        versions = {
            (n["groupId"], n["artifactId"]): n["version"] for n in out["nodes"]
        }
        self.assertEqual(versions[("com.example", "managed")], "9.9")
        self.assertEqual(versions[("com.example", "from-parent")], "3.0")
        self.assertEqual(versions[("com.example", "override")], "2.0")
        self.assertEqual(versions[("com.example", "propped")], "5.0")
        self.assertNotIn(("org.example", "bom"), versions)
        self.assertIn(("org.example", "bom", "1.0"), calls)
        self.assertNotIn(("com.example", "from-placeholder"), {
            (n["groupId"], n["artifactId"]) for n in out["nodes"]
        })

    def test_tc7_nearest_wins_and_does_not_fetch_loser(self):
        root = _pom("root", "1.0", _deps(
            _dep("com.example", "near", "1.0"),
            _dep("com.example", "far", "1.0"),
        ))
        near = _pom("near", "1.0", _deps(_dep("com.example", "conflict", "1.0")))
        far = _pom("far", "1.0", _deps(_dep("com.example", "mid", "1.0")))
        mid = _pom("mid", "1.0", _deps(_dep("com.example", "conflict", "2.0")))
        conflict_1 = _pom("conflict", "1.0")
        conflict_2 = _pom("conflict", "2.0", _deps(_dep("com.example", "hidden", "1.0")))
        conflict_2_leaf = _pom("conflict", "2.0")
        out, calls = self._resolve("com.example", "root", "1.0", {
            ("com.example", "root", "1.0"): root,
            ("com.example", "near", "1.0"): near,
            ("com.example", "far", "1.0"): far,
            ("com.example", "mid", "1.0"): mid,
            ("com.example", "conflict", "1.0"): conflict_1,
            ("com.example", "conflict", "2.0"): conflict_2,
        })
        conflict = next(n for n in out["nodes"] if n["artifactId"] == "conflict")
        self.assertEqual(conflict["version"], "1.0")
        self.assertNotIn(("com.example", "conflict", "2.0"), calls)
        self.assertNotIn("hidden", [n["artifactId"] for n in out["nodes"]])
        self.assertFalse(out["partial"])

        equal = _pom("root", "1.0", _deps(
            _dep("com.example", "a", "1.0"),
            _dep("com.example", "b", "1.0"),
        ))
        a = _pom("a", "1.0", _deps(_dep("com.example", "conflict", "1.0")))
        b = _pom("b", "1.0", _deps(_dep("com.example", "conflict", "2.0")))
        out, calls = self._resolve("com.example", "root", "1.0", {
            ("com.example", "root", "1.0"): equal,
            ("com.example", "a", "1.0"): a,
            ("com.example", "b", "1.0"): b,
            ("com.example", "conflict", "1.0"): conflict_1,
            ("com.example", "conflict", "2.0"): conflict_2_leaf,
        })
        conflict = next(n for n in out["nodes"] if n["artifactId"] == "conflict")
        self.assertEqual(conflict["version"], "2.0")
        self.assertNotIn(("com.example", "conflict", "1.0"), calls)
        self.assertFalse(out["partial"])

    def test_tc8_exclusion_is_per_branch(self):
        root = _pom("root", "1.0", _deps(
            _dep("com.example", "a", "1.0", exclusions=[("com.example", "c")]),
            _dep("com.example", "b", "1.0"),
        ))
        a = _pom("a", "1.0", _deps(_dep("com.example", "c", "1.0")))
        b = _pom("b", "1.0", _deps(_dep("com.example", "c", "1.0")))
        c = _pom("c", "1.0")
        out, _calls = self._resolve("com.example", "root", "1.0", {
            ("com.example", "root", "1.0"): root,
            ("com.example", "a", "1.0"): a,
            ("com.example", "b", "1.0"): b,
            ("com.example", "c", "1.0"): c,
        })
        c_nodes = [n for n in out["nodes"] if n["artifactId"] == "c"]
        self.assertEqual(len(c_nodes), 1)
        c_index = out["nodes"].index(c_nodes[0])
        b_index = next(i for i, n in enumerate(out["nodes"]) if n["artifactId"] == "b")
        a_index = next(i for i, n in enumerate(out["nodes"]) if n["artifactId"] == "a")
        sources = [e["from"] for e in out["edges"] if e["to"] == c_index]
        self.assertIn(b_index, sources)
        self.assertNotIn(a_index, sources)

        root = _pom("root", "1.0", _deps(
            _dep("com.example", "mid1", "1.0"),
            _dep("com.example", "mid2", "1.0"),
        ))
        mid1 = _pom("mid1", "1.0", _deps(
            _dep("com.example", "shared", "1.0", exclusions=[("com.example", "evil")]),
        ))
        mid2 = _pom("mid2", "1.0", _deps(_dep("com.example", "shared", "1.0")))
        shared = _pom("shared", "1.0", _deps(_dep("com.example", "evil", "1.0")))
        evil = _pom("evil", "1.0")
        out, _calls = self._resolve("com.example", "root", "1.0", {
            ("com.example", "root", "1.0"): root,
            ("com.example", "mid1", "1.0"): mid1,
            ("com.example", "mid2", "1.0"): mid2,
            ("com.example", "shared", "1.0"): shared,
            ("com.example", "evil", "1.0"): evil,
        })
        self.assertIn("evil", [n["artifactId"] for n in out["nodes"]])

    def test_tc9_scope_and_optional(self):
        opt = _pom("opt", "1.0", _deps(_dep("com.example", "child", "1.0")))
        child = _pom("child", "1.0")
        root = _pom("root", "1.0", _deps(
            _dep("com.example", "kept", "1.0"),
            _dep("com.example", "rt", "1.0", scope="runtime"),
            _dep("com.example", "tests", "1.0", scope="test"),
            _dep("com.example", "prov", "1.0", scope="provided"),
            _dep("com.example", "ranged", "[1,2)", scope="test"),
            _dep("com.example", "opt", "1.0", optional=True),
        ))
        out, calls = self._resolve("com.example", "root", "1.0", {
            ("com.example", "root", "1.0"): root,
            ("com.example", "kept", "1.0"): _pom("kept", "1.0"),
            ("com.example", "rt", "1.0"): _pom("rt", "1.0"),
            ("com.example", "opt", "1.0"): opt,
            ("com.example", "child", "1.0"): child,
        })
        names = [n["artifactId"] for n in out["nodes"]]
        self.assertEqual(names, ["root", "kept", "rt", "opt"])
        opt_node = next(n for n in out["nodes"] if n["artifactId"] == "opt")
        self.assertEqual(opt_node["relation"], "DIRECT")
        self.assertNotIn("child", names)
        self.assertNotIn(("com.example", "child", "1.0"), calls)
        self.assertIn(("com.example", "opt", "1.0"), calls)
        self.assertFalse(out["partial"])
        self.assertEqual(out["nodeErrors"], [])

    def test_tc10_compliance_reads_pom_licenses(self):
        root = _pom(
            "root", "1.0",
            _license("MIT") + _deps(_dep("com.example", "gpl", "1.0")),
        )
        gpl = _pom("gpl", "1.0", _license("GPL-3.0-only"))
        mapping = {
            ("com.example", "root", "1.0"): root,
            ("com.example", "gpl", "1.0"): gpl,
        }
        calls, fetch = self._fetch(mapping)
        with unittest.mock.patch.object(server, "fetch_pom", fetch), \
                unittest.mock.patch.object(
                    server, "fetch_depsdev_licenses",
                    side_effect=AssertionError("licenses"),
                ), \
                unittest.mock.patch.object(
                    server, "fetch_depsdev_dependencies",
                    side_effect=AssertionError("graph"),
                ):
            out = server.check_license_compliance(
                [{"groupId": "com.example", "artifactId": "root", "version": "1.0"}],
                project_license="MIT",
                ctx=self.ctx(),
            )
        by_name = {row["artifactId"]: row for row in out["results"]}
        self.assertEqual(by_name["root"]["source"], "pom")
        self.assertEqual(by_name["root"]["verdict"], "ok")
        self.assertEqual(by_name["gpl"]["verdict"], "violation")
        self.assertEqual(by_name["gpl"]["category"], "strong-copyleft")
        self.assertTrue(any("not legal advice" in note for note in out["notes"]))
        self.assertGreaterEqual(len(calls), 1)

    def test_tc11_upgrade_closure_source(self):
        calls = []

        def fetch(group_id, artifact_id, version, ctx):
            calls.append(version)
            return {
                "ok": True,
                "nodes": [{
                    "groupId": group_id,
                    "artifactId": artifact_id,
                    "version": version,
                    "relation": "SELF",
                    "errors": [],
                    "pomLicenses": ["MIT"],
                }],
                "edges": [],
                "partial": False,
                "truncated": False,
                "error": None,
                "nodeErrors": [],
            }

        with unittest.mock.patch.object(server, "resolve_pom_graph", fetch), \
                unittest.mock.patch.object(
                    server, "fetch_depsdev_dependencies",
                    side_effect=AssertionError("deps.dev"),
                ), \
                unittest.mock.patch.object(server, "query_osv_batch", return_value=[]):
            out = server.compare_upgrade_closure({
                "projectPath": str(self.project),
                "includeLicenses": False,
                "upgrades": [{
                    "groupId": "com.example",
                    "artifactId": "lib",
                    "fromVersion": "1.0",
                    "toVersion": "2.0",
                }],
            })
        self.assertEqual(out["graphSource"], "pom")
        self.assertEqual(sorted(calls), ["1.0", "2.0"])

        gradle_root = Path(self._tmp.name) / "gradle"
        gradle_root.mkdir()
        (gradle_root / "build.gradle").write_text("plugins { id 'java' }\n", encoding="utf-8")
        write_fake_gradlew(str(gradle_root))
        tree = (
            "===MAVEN_MCP_TREE===\t:\truntimeClasspath\n"
            "PROJECT\t0\n"
            "NODE\t1\tcom.example\tlib\t1.0\n"
            "EDGE\t0\t1\n"
            "===MAVEN_MCP_TREE_END===\n"
        )

        def gradle_run(cmd, **_kwargs):
            return subprocess.CompletedProcess(cmd, 0, tree, "")

        with unittest.mock.patch.object(server, "_gradle_run", gradle_run), \
                unittest.mock.patch.object(
                    server, "resolve_pom_graph",
                    side_effect=AssertionError("pom"),
                ), \
                unittest.mock.patch.object(server, "query_osv_batch", return_value=[]):
            out = server.compare_upgrade_closure({
                "projectPath": str(gradle_root),
                "includeLicenses": False,
                "upgrades": [{
                    "groupId": "com.example",
                    "artifactId": "lib",
                    "fromVersion": "1.0",
                    "toVersion": "2.0",
                }],
            })
        self.assertEqual(out["graphSource"], "gradle")

    def test_tc12_vulnerability_path_from_self(self):
        root = _pom("root", "1.0", _deps(_dep("com.example", "mid", "1.0")))
        mid = _pom("mid", "1.0", _deps(_dep("com.example", "leaf", "1.0")))
        leaf = _pom("leaf", "1.0")
        mapping = {
            ("com.example", "root", "1.0"): root,
            ("com.example", "mid", "1.0"): mid,
            ("com.example", "leaf", "1.0"): leaf,
        }
        _calls, fetch = self._fetch(mapping)

        def osv(deps):
            rows = []
            for dep in deps:
                vulns = []
                if dep["artifactId"] == "leaf":
                    vulns = [{
                        "id": "GHSA-test",
                        "summary": "example",
                        "url": "https://osv.dev/vulnerability/GHSA-test",
                        "malicious": False,
                        "severity": "HIGH",
                    }]
                rows.append({**dep, "vulnerabilities": vulns})
            return rows

        with unittest.mock.patch.object(server, "fetch_pom", fetch), \
                unittest.mock.patch.object(server, "query_osv_batch", osv), \
                unittest.mock.patch.object(
                    server, "fetch_depsdev_dependencies",
                    side_effect=AssertionError("deps.dev"),
                ):
            out = server.get_vulnerability_paths(
                "com.example", "root", "1.0", self.ctx(),
            )
        self.assertEqual(len(out["vulnerabilityPaths"]), 1)
        path = out["vulnerabilityPaths"][0]["path"]
        self.assertEqual(
            [step["artifactId"] for step in path],
            ["root", "mid", "leaf"],
        )

    def test_tc13_cycle_stops(self):
        a = _pom("a", "1.0", _deps(_dep("com.example", "b", "1.0")))
        b = _pom("b", "1.0", _deps(_dep("com.example", "a", "1.0")))
        out, calls = self._resolve("com.example", "a", "1.0", {
            ("com.example", "a", "1.0"): a,
            ("com.example", "b", "1.0"): b,
        })
        self.assertFalse(out["partial"])
        self.assertEqual(
            sorted(n["artifactId"] for n in out["nodes"]),
            ["a", "b"],
        )
        self.assertEqual(calls.count(("com.example", "a", "1.0")), 1)
        self.assertEqual(calls.count(("com.example", "b", "1.0")), 1)

    def test_tc14_node_cap(self):
        root = _pom("root", "1.0", _deps(
            _dep("com.example", "a", "1.0"),
            _dep("com.example", "b", "1.0"),
            _dep("com.example", "c", "1.0"),
        ))
        mapping = {("com.example", "root", "1.0"): root}
        for name in ("a", "b", "c"):
            mapping[("com.example", name, "1.0")] = _pom(name, "1.0")
        with unittest.mock.patch.object(server, "MAX_TRANSITIVE_GRAPH_NODES", 2):
            out, calls = self._resolve("com.example", "root", "1.0", mapping)
        self.assertTrue(out["truncated"])
        self.assertTrue(out["partial"])
        self.assertEqual(len(out["nodes"]), 2)
        self.assertNotIn(("com.example", "c", "1.0"), calls)

    def test_tc15_version_range_is_skipped(self):
        root = _pom("root", "1.0", _dm(_dep("com.example", "lib", "9.0")) + _deps(
            _dep("com.example", "lib", "[1.0,2.0)"),
        ))
        out, calls = self._resolve(
            "com.example", "root", "1.0",
            {("com.example", "root", "1.0"): root},
        )
        self.assertTrue(out["partial"])
        self.assertEqual(len(out["nodes"]), 1)
        self.assertEqual(out["edges"], [])
        self.assertTrue(out["nodeErrors"])
        self.assertIn("version range", out["nodeErrors"][0]["errors"][0])
        self.assertNotIn(("com.example", "lib", "9.0"), calls)

    def test_tc16_unresolved_property(self):
        root = _pom("root", "1.0", _deps(
            _dep("com.example", "lib", "${missing}"),
        ))
        out, _calls = self._resolve(
            "com.example", "root", "1.0",
            {("com.example", "root", "1.0"): root},
        )
        self.assertTrue(out["partial"])
        self.assertEqual(out["edges"], [])
        self.assertIn("unresolved property", out["nodeErrors"][0]["errors"][0])

    def test_tc17_userinfo_stays_out_of_error(self):
        def boom(*_args, **_kwargs):
            raise RuntimeError("user:pass@repo.example/secret")

        with unittest.mock.patch.object(server, "locate_pom", boom):
            out = server.resolve_pom_graph("com.example", "lib", "1.0", self.ctx())
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "RuntimeError")
        blob = json.dumps(out)
        self.assertNotIn("user:pass", blob)
        self.assertNotIn("secret", blob)

    def test_tc18_dependency_management_is_not_an_edge(self):
        root = _pom("root", "1.0", (
            _dm(_dep("com.example", "pinned", "1.0"))
            + "<profiles><profile><dependencies>"
            + _dep("com.example", "profiled", "1.0")
            + "</dependencies></profile></profiles>"
            + _deps(_dep("com.example", "real", "1.0"))
        ))
        out, _calls = self._resolve("com.example", "root", "1.0", {
            ("com.example", "root", "1.0"): root,
            ("com.example", "real", "1.0"): _pom("real", "1.0"),
        })
        names = [n["artifactId"] for n in out["nodes"]]
        self.assertEqual(names, ["root", "real"])
        self.assertFalse(out["partial"])

    def test_tc19_offline_without_cache_is_explicit(self):
        with unittest.mock.patch.dict(
            os.environ, {"MAVEN_MCP_OFFLINE": "1"}, clear=False,
        ), unittest.mock.patch("urllib.request.urlopen") as urlopen:
            os.environ.pop("MAVEN_MCP_REPOSITORY_BASE", None)
            out = server.resolve_pom_graph("com.example", "lib", "1.0", self.ctx())
        urlopen.assert_not_called()
        self.assertFalse(out["ok"])
        self.assertTrue(out["partial"])
        self.assertEqual(out["nodes"], [])
        self.assertEqual(
            out["error"],
            "no queryable repositories (offline/closed mode with no mirror "
            "or MAVEN_MCP_REPOSITORY_BASE)",
        )
        self.assertEqual(out["capabilityUnavailable"], "offline")

    def test_tc20_gradle_home_is_not_the_cache(self):
        distro = Path(self._tmp.name) / "distro"
        self._write_under(
            distro / "caches" / "modules-2" / "files-2.1"
            / "com.example" / "lib" / "1.0" / "hash",
            "lib-1.0.pom",
            _pom("lib", "1.0", "FROM_DISTRO"),
        )
        self._gradle_pom("com.example", "lib", "1.0", _pom("lib", "1.0", "FROM_HOME"))

        def boom(*_args, **_kwargs):
            raise AssertionError("fetch_pom")

        with unittest.mock.patch.dict(
            os.environ,
            {"GRADLE_HOME": str(distro), "GRADLE_USER_HOME": ""},
            clear=False,
        ), unittest.mock.patch.object(server, "fetch_pom", boom):
            found = server.locate_pom("com.example", "lib", "1.0", self.ctx())
        self.assertIn("FROM_HOME", found)
        self.assertNotIn("FROM_DISTRO", found)

        for path in (self.home / ".gradle").rglob("*.pom"):
            path.unlink()
        with unittest.mock.patch.dict(
            os.environ,
            {"GRADLE_HOME": str(distro), "GRADLE_USER_HOME": ""},
            clear=False,
        ), unittest.mock.patch.object(
            server, "fetch_pom", return_value="FROM_FETCH",
        ) as fetch:
            found = server.locate_pom("com.example", "lib", "1.0", self.ctx())
        self.assertEqual(found, "FROM_FETCH")
        fetch.assert_called_once()

    def test_locate_pom_rejects_path_escape(self):
        outside = Path(self._tmp.name) / "outside"
        self._write_under(outside / "hash", "stolen.pom", "STOLEN")

        def boom(*_args, **_kwargs):
            raise AssertionError("fetch_pom")

        unsafe = (
            ("com.example", "lib", str(outside)),
            ("..", "lib", "1.0"),
            (".git", "lib", "1.0"),
            ("com.example", "..", "1.0"),
            ("com.example", "lib", ".."),
            ("com.example", "lib", "1.0/../../outside"),
            ("com..example", "lib", "1.0"),
            ("", "lib", "1.0"),
        )
        with unittest.mock.patch.object(server, "fetch_pom", boom):
            for group_id, artifact_id, version in unsafe:
                found = server.locate_pom(group_id, artifact_id, version, self.ctx())
                self.assertIsNone(found, (group_id, artifact_id, version))

    def test_deeper_required_walks_optional_winner_children(self):
        lib = _pom("lib", "1.0", _deps(_dep("com.example", "child", "1.0")))
        lib_far = _pom("lib", "2.0", _deps(_dep("com.example", "other", "1.0")))
        mid = _pom("mid", "1.0", _deps(_dep("com.example", "lib", "2.0")))
        root = _pom("root", "1.0", _deps(
            _dep("com.example", "lib", "1.0", optional=True),
            _dep("com.example", "mid", "1.0"),
        ))
        out, calls = self._resolve("com.example", "root", "1.0", {
            ("com.example", "root", "1.0"): root,
            ("com.example", "lib", "1.0"): lib,
            ("com.example", "lib", "2.0"): lib_far,
            ("com.example", "mid", "1.0"): mid,
            ("com.example", "child", "1.0"): _pom("child", "1.0"),
            ("com.example", "other", "1.0"): _pom("other", "1.0"),
        })
        names = [n["artifactId"] for n in out["nodes"]]
        self.assertIn("child", names)
        self.assertNotIn("other", names)
        lib_node = next(n for n in out["nodes"] if n["artifactId"] == "lib")
        self.assertEqual(lib_node["version"], "1.0")
        self.assertNotIn(("com.example", "lib", "2.0"), calls)
        self.assertNotIn(("com.example", "other", "1.0"), calls)
        self.assertFalse(out["partial"])

    def test_star_exclusion_matches_group_or_artifact(self):
        def names_for(root_body, extras):
            mapping = {("com.example", "root", "1.0"): _pom("root", "1.0", root_body)}
            mapping.update(extras)
            out, _calls = self._resolve("com.example", "root", "1.0", mapping)
            return out, [n["artifactId"] for n in out["nodes"]]

        leaves = {
            ("com.example", "a", "1.0"): _pom("a", "1.0", _deps(
                _dep("com.example", "c", "1.0"),
                _dep("org.other", "d", "1.0"),
            )),
            ("com.example", "c", "1.0"): _pom("c", "1.0"),
            ("org.other", "d", "1.0"): _pom("d", "1.0", group="org.other"),
        }
        out, names = names_for(
            _deps(_dep("com.example", "a", "1.0", exclusions=[("com.example", "*")])),
            leaves,
        )
        self.assertNotIn("c", names)
        self.assertIn("d", names)
        self.assertFalse(out["partial"])

        _out, names = names_for(
            _deps(_dep("com.example", "a", "1.0", exclusions=[("*", "c")])),
            leaves,
        )
        self.assertNotIn("c", names)
        self.assertIn("d", names)

        _out, names = names_for(
            _deps(_dep("com.example", "a", "1.0", exclusions=[("*", "*")])),
            leaves,
        )
        self.assertNotIn("c", names)
        self.assertNotIn("d", names)

        shared_leaves = {
            ("com.example", "left", "1.0"): _pom("left", "1.0", _deps(
                _dep("com.example", "shared", "1.0", exclusions=[("com.example", "*")]),
            )),
            ("com.example", "right", "1.0"): _pom("right", "1.0", _deps(
                _dep("com.example", "shared", "1.0", exclusions=[("com.example", "*")]),
            )),
            ("com.example", "shared", "1.0"): _pom("shared", "1.0", _deps(
                _dep("com.example", "c", "1.0"),
                _dep("org.other", "d", "1.0"),
            )),
            ("com.example", "c", "1.0"): _pom("c", "1.0"),
            ("org.other", "d", "1.0"): _pom("d", "1.0", group="org.other"),
        }
        _out, names = names_for(
            _deps(
                _dep("com.example", "left", "1.0"),
                _dep("com.example", "right", "1.0"),
            ),
            shared_leaves,
        )
        self.assertNotIn("c", names)
        self.assertIn("d", names)

        one_sided = dict(shared_leaves)
        one_sided[("com.example", "right", "1.0")] = _pom("right", "1.0", _deps(
            _dep("com.example", "shared", "1.0"),
        ))
        _out, names = names_for(
            _deps(
                _dep("com.example", "left", "1.0"),
                _dep("com.example", "right", "1.0"),
            ),
            one_sided,
        )
        self.assertIn("c", names)
        self.assertIn("d", names)

    def test_promoted_optional_children_stay_at_their_depth(self):
        # optlib is optional at depth 1 and required again through mid.
        # shared:1.0 is a child of optlib (depth 2). shared:2.0 is a child of
        # deep (depth 3). Nearest wins: 1.0.
        optlib = _pom("optlib", "1.0", _deps(_dep("com.example", "shared", "1.0")))
        deep = _pom("deep", "1.0", _deps(_dep("com.example", "shared", "2.0")))
        mid = _pom("mid", "1.0", _deps(
            _dep("com.example", "optlib", "1.0"),
            _dep("com.example", "deep", "1.0"),
        ))
        root = _pom("root", "1.0", _deps(
            _dep("com.example", "optlib", "1.0", optional=True),
            _dep("com.example", "mid", "1.0"),
        ))
        out, calls = self._resolve("com.example", "root", "1.0", {
            ("com.example", "root", "1.0"): root,
            ("com.example", "optlib", "1.0"): optlib,
            ("com.example", "mid", "1.0"): mid,
            ("com.example", "deep", "1.0"): deep,
            ("com.example", "shared", "1.0"): _pom("shared", "1.0"),
            ("com.example", "shared", "2.0"): _pom("shared", "2.0"),
        })
        shared = next(n for n in out["nodes"] if n["artifactId"] == "shared")
        self.assertEqual(shared["version"], "1.0")
        self.assertNotIn(("com.example", "shared", "2.0"), calls)
        self.assertFalse(out["partial"])

    def test_deeper_frontier_does_not_claim_shared_before_optional(self):
        # The required edge to optlib is strictly deeper than the optional
        # node (root → mid → deep, depth 3). shared on optlib is depth 2;
        # the copy on deep is depth 3. Nearest wins, not whichever frontier
        # created the GA first. onlyfar exists only on the farther POM.
        def case(near_version: str, far_version: str) -> None:
            optlib = _pom("optlib", "1.0", _deps(
                _dep("com.example", "shared", near_version),
                _dep("com.example", "onlynear", "1.0"),
            ))
            shared_far = _pom("shared", far_version, _deps(
                _dep("com.example", "onlyfar", "1.0"),
            ))
            deep = _pom("deep", "1.0", _deps(
                _dep("com.example", "optlib", "1.0"),
                _dep("com.example", "shared", far_version),
            ))
            mid = _pom("mid", "1.0", _deps(_dep("com.example", "deep", "1.0")))
            root = _pom("root", "1.0", _deps(
                _dep("com.example", "optlib", "1.0", optional=True),
                _dep("com.example", "mid", "1.0"),
            ))
            out, calls = self._resolve("com.example", "root", "1.0", {
                ("com.example", "root", "1.0"): root,
                ("com.example", "optlib", "1.0"): optlib,
                ("com.example", "mid", "1.0"): mid,
                ("com.example", "deep", "1.0"): deep,
                ("com.example", "shared", near_version): _pom("shared", near_version),
                ("com.example", "shared", far_version): shared_far,
                ("com.example", "onlynear", "1.0"): _pom("onlynear", "1.0"),
                ("com.example", "onlyfar", "1.0"): _pom("onlyfar", "1.0"),
            })
            names = [n["artifactId"] for n in out["nodes"]]
            self.assertIn("onlynear", names)
            self.assertNotIn("onlyfar", names)
            shared = next(n for n in out["nodes"] if n["artifactId"] == "shared")
            self.assertEqual(shared["version"], near_version)
            self.assertNotIn(("com.example", "shared", far_version), calls)
            self.assertNotIn(("com.example", "onlyfar", "1.0"), calls)
            self.assertFalse(out["partial"])

        case("1.0", "2.0")
        case("2.0", "1.0")

    def test_same_depth_pending_and_deferred_share_version(self):
        # bridge (child of the returned optional) and the deferred frontier
        # both introduce shared at depth 3. Higher compare_versions wins.
        # A direct child of optlib is still nearer and is covered above.
        def case(bridge_version: str, deep_version: str) -> None:
            bridge = _pom("bridge", "1.0", _deps(
                _dep("com.example", "shared", bridge_version),
            ))
            optlib = _pom("optlib", "1.0", _deps(
                _dep("com.example", "bridge", "1.0"),
            ))
            deep = _pom("deep", "1.0", _deps(
                _dep("com.example", "optlib", "1.0"),
                _dep("com.example", "shared", deep_version),
            ))
            mid = _pom("mid", "1.0", _deps(_dep("com.example", "deep", "1.0")))
            root = _pom("root", "1.0", _deps(
                _dep("com.example", "optlib", "1.0", optional=True),
                _dep("com.example", "mid", "1.0"),
            ))
            out, calls = self._resolve("com.example", "root", "1.0", {
                ("com.example", "root", "1.0"): root,
                ("com.example", "optlib", "1.0"): optlib,
                ("com.example", "bridge", "1.0"): bridge,
                ("com.example", "mid", "1.0"): mid,
                ("com.example", "deep", "1.0"): deep,
                ("com.example", "shared", bridge_version): _pom(
                    "shared", bridge_version, _deps(
                        _dep("com.example", "frombridge", "1.0"),
                    ),
                ),
                ("com.example", "shared", deep_version): _pom(
                    "shared", deep_version, _deps(
                        _dep("com.example", "fromdeep", "1.0"),
                    ),
                ),
                ("com.example", "frombridge", "1.0"): _pom("frombridge", "1.0"),
                ("com.example", "fromdeep", "1.0"): _pom("fromdeep", "1.0"),
            })
            names = [n["artifactId"] for n in out["nodes"]]
            shared = next(n for n in out["nodes"] if n["artifactId"] == "shared")
            self.assertEqual(shared["version"], "2.0")
            if bridge_version == "2.0":
                self.assertIn("frombridge", names)
                self.assertNotIn("fromdeep", names)
                self.assertNotIn(("com.example", "shared", deep_version), calls)
            else:
                self.assertIn("fromdeep", names)
                self.assertNotIn("frombridge", names)
                self.assertNotIn(("com.example", "shared", bridge_version), calls)
            self.assertFalse(out["partial"])

        case("2.0", "1.0")
        case("1.0", "2.0")

    def test_optional_intro_does_not_clear_required_exclusion(self):
        lib = _pom("lib", "1.0", _deps(_dep("com.example", "evil", "1.0")))
        evil = _pom("evil", "1.0")
        mida = _pom("mida", "1.0", _deps(
            _dep("com.example", "lib", "1.0", optional=True),
        ))
        midb = _pom("midb", "1.0", _deps(
            _dep("com.example", "lib", "1.0", exclusions=[("com.example", "evil")]),
        ))
        root = _pom("root", "1.0", _deps(
            _dep("com.example", "mida", "1.0"),
            _dep("com.example", "midb", "1.0"),
        ))
        out, calls = self._resolve("com.example", "root", "1.0", {
            ("com.example", "root", "1.0"): root,
            ("com.example", "mida", "1.0"): mida,
            ("com.example", "midb", "1.0"): midb,
            ("com.example", "lib", "1.0"): lib,
            ("com.example", "evil", "1.0"): evil,
        })
        self.assertNotIn("evil", [n["artifactId"] for n in out["nodes"]])
        self.assertNotIn(("com.example", "evil", "1.0"), calls)
        self.assertFalse(out["partial"])

        # Two required introductions: a branch without the exclusion keeps evil.
        mida_required = _pom("mida", "1.0", _deps(_dep("com.example", "lib", "1.0")))
        out, calls = self._resolve("com.example", "root", "1.0", {
            ("com.example", "root", "1.0"): root,
            ("com.example", "mida", "1.0"): mida_required,
            ("com.example", "midb", "1.0"): midb,
            ("com.example", "lib", "1.0"): lib,
            ("com.example", "evil", "1.0"): evil,
        })
        self.assertIn("evil", [n["artifactId"] for n in out["nodes"]])
        self.assertIn(("com.example", "evil", "1.0"), calls)

    def _write_under(self, directory, name, text):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
