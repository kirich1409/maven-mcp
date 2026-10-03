"""Gradle before/after upgrade closure.

Mocked ``_gradle_run`` covers ordering, timeout, and soft failure. Two real
wrapper projects lock versionless ``exact`` and a same-GA carrier that must
not be rewritten. ``auto`` is the default only because those projects pass.
"""

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
import zipfile

from _helpers import server


def _upgrade(group_id="com.example", artifact_id="lib",
             from_version="4.9.3", to_version="4.12.0"):
    return {
        "groupId": group_id,
        "artifactId": artifact_id,
        "fromVersion": from_version,
        "toVersion": to_version,
    }


def _args(upgrades, **extra):
    payload = {"upgrades": upgrades}
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


def _completed(cmd, code, stdout, stderr=""):
    return subprocess.CompletedProcess(cmd, code, stdout, stderr)


def _tree(module, configuration, lines):
    body = "\n".join(lines)
    return (
        f"===MAVEN_MCP_TREE===\t{module}\t{configuration}\n"
        f"{body}\n"
        "===MAVEN_MCP_TREE_END===\n"
    )


def _lib_tree(version, extra=None):
    lines = [
        "PROJECT\t0",
        f"NODE\t1\tcom.example\tlib\t{version}",
        "EDGE\t0\t1",
    ]
    if extra:
        lines.extend([
            f"NODE\t2\tcom.example\textra\t{extra}",
            "EDGE\t1\t2",
        ])
    return _tree(":", "runtimeClasspath", lines)


def _matched(rewritten=1, versionless=0, from_version="4.9.3", to_version="4.12.0"):
    return (
        "===MAVEN_MCP_MATCHED===\tcom.example\tlib\t"
        f"{from_version}\t{to_version}\t{rewritten}\t{versionless}\n"
    )


def _request(version, rewritten):
    return f"===MAVEN_MCP_REQUEST===\tcom.example\tlib\t{version}\t{rewritten}\n"


class _GradleRunSpy:
    """Records wrapper launches. Calls do not overlap."""

    def __init__(self, responder):
        self.responder = responder
        self.calls = []
        self.scripts = []
        self.payloads = []
        self.envs = []
        self._active = 0
        self.overlapped = False

    def __call__(self, cmd, **kwargs):
        if self._active:
            self.overlapped = True
        self._active += 1
        try:
            self.calls.append(list(cmd))
            self.envs.append(dict(kwargs.get("env") or {}))
            if "--init-script" in cmd:
                path = cmd[cmd.index("--init-script") + 1]
                with open(path, "rb") as handle:
                    self.scripts.append(handle.read())
            for arg in cmd:
                if arg.startswith("-Dmaven.mcp.upgrade.file="):
                    path = arg.split("=", 1)[1]
                    with open(path, encoding="utf-8") as handle:
                        self.payloads.append(json.load(handle))
            stdout, stderr, code = self.responder(self.payloads[-1] if self.payloads else {})
            return _completed(cmd, code, stdout, stderr)
        finally:
            self._active -= 1


def _versions(side, artifact="lib"):
    found = []
    for node in (side.get("graph") or {}).get("nodes") or []:
        if node.get("artifactId") == artifact and node.get("version"):
            found.append(node["version"])
    return found


class GradleClosureParserTest(unittest.TestCase):
    def test_versions_with_colon_and_space_round_trip(self):
        stdout = _tree(":", "runtimeClasspath", [
            "PROJECT\t0",
            "NODE\t1\tcom.example\tlib\t1.2:meta",
            "NODE\t2\tcom.example\tother\t1.0+build 2",
            "EDGE\t0\t1",
            "EDGE\t1\t2",
            "EDGE\t0\t2",
            "NODE 3 com.example lib 9.9.9",
        ])
        parsed = server._parse_gradle_closure_stdout(stdout)
        nodes = parsed["trees"][0]["nodes"]
        versions = [node.get("version") for node in nodes if node.get("version")]
        self.assertEqual(versions, ["1.2:meta", "1.0+build 2"])
        self.assertTrue(nodes[0].get("project"))
        self.assertEqual(len(parsed["trees"][0]["edges"]), 3)
        kept, _errors, _truncated = server._filter_gradle_closure_parse(parsed)
        graph = server._normalise_closure_graph(kept)
        gavs = {
            (node.get("groupId"), node.get("artifactId"), node.get("version"))
            for node in graph["nodes"]
            if node.get("groupId")
        }
        self.assertIn(("com.example", "lib", "1.2:meta"), gavs)
        self.assertIn(("com.example", "other", "1.0+build 2"), gavs)
        self.assertTrue(any(node.get("synthetic") for node in graph["nodes"]))
        self.assertFalse(any(node.get("project") for node in graph["nodes"]))

    def test_test_configuration_is_dropped(self):
        stdout = _tree(":", "testRuntimeClasspath", [
            "NODE\t0\tcom.example\tlib\t1.0.0",
        ])
        parsed = server._parse_gradle_closure_stdout(stdout)
        kept, _errors, _truncated = server._filter_gradle_closure_parse(parsed)
        self.assertEqual(kept, [])

    def test_script_bytes_ignore_the_coordinate(self):
        first = server._generate_gradle_upgrade_init_script()
        second = server._generate_gradle_upgrade_init_script()
        self.assertEqual(first, second)
        self.assertNotIn("@@NODE_CAP@@", first)
        self.assertIn(str(server.MAX_TRANSITIVE_GRAPH_NODES), first)
        self.assertNotIn("com.example", first)
        self.assertIn("_is_production_runtime_configuration", first)
        self.assertIn("_select_configurations_to_resolve", first)
        self.assertNotIn("useStrictVersion", first)


class GradleClosureMockTest(unittest.TestCase):
    def _run(self, responder, args, env=None):
        spy = _GradleRunSpy(responder)
        with contextlib.ExitStack() as stack:
            stack.enter_context(unittest.mock.patch.object(server, "_gradle_run", spy))
            stack.enter_context(unittest.mock.patch.object(server, "query_osv_batch", _clean_osv))
            deps = stack.enter_context(
                unittest.mock.patch.object(server, "fetch_depsdev_dependencies"),
            )
            lic = stack.enter_context(
                unittest.mock.patch.object(server, "fetch_depsdev_licenses"),
            )
            if env:
                stack.enter_context(unittest.mock.patch.dict(os.environ, env))
            out = server.compare_upgrade_closure(args)
        return out, spy, deps, lic

    def test_calls_do_not_overlap_and_env_is_scrubbed(self):
        def respond(payload):
            version = "4.9.3" if payload.get("phase") == "before" else "4.12.0"
            versionless = 1 if payload.get("phase") == "after" else 0
            stdout = _lib_tree(version) + _matched(1, versionless)
            if payload.get("phase") == "after":
                stdout += _request("", 1)
            return stdout, "", 0

        out, spy, deps, _lic = self._run(
            respond,
            _args([_upgrade()], projectPath=self._wrapper_project(), substitution="exact"),
            env={"GITHUB_TOKEN": "gh-secret", "MAVEN_REPO_EXAMPLE_TOKEN": "repo-secret"},
        )
        self.assertFalse(spy.overlapped)
        self.assertEqual(len(spy.calls), 2)
        self.assertEqual([item["phase"] for item in spy.payloads], ["before", "after"])
        self.assertEqual(spy.scripts[0], spy.scripts[1])
        self.assertNotIn(b"com.example", spy.scripts[0])
        for cmd in spy.calls:
            self.assertIn("--no-configuration-cache", cmd)
            self.assertEqual(cmd.count("--offline"), 0)
        for env in spy.envs:
            self.assertNotIn("GITHUB_TOKEN", env)
            self.assertFalse(any(key.startswith("MAVEN_REPO_") for key in env))
        deps.assert_not_called()
        self.assertEqual(out["graphSource"], "gradle")
        self.assertTrue(out["targets"][0]["landed"])
        self.assertTrue(out["targets"][0]["rewroteVersionless"])
        self.assertGreater(out["targets"][0]["substitutionMatched"], 0)
        self.assertIn(server._VERSIONLESS_ARM_NOTE, out["notes"])
        self.assertIn(server._GRADLE_LICENSE_METADATA_NOTE, out["notes"])
        self.assertFalse(any(note.startswith("Graphs are resolved per root") for note in out["notes"]))
        self.assertEqual(out["advisory"], "review")

    def test_timeout_is_unknown(self):
        def respond(_payload):
            return "", "", 124

        out, spy, deps, lic = self._run(
            respond,
            _args([_upgrade()], projectPath=self._wrapper_project(), graphSource="gradle"),
        )
        deps.assert_not_called()
        lic.assert_not_called()
        self.assertEqual(len(spy.calls), 2)
        self.assertFalse(spy.overlapped)
        self.assertEqual(out["advisory"], "unknown")
        self.assertEqual(out["dependencies"]["added"], [])
        self.assertNotEqual(out["advisory"], "none")
        self.assertIn("timed out", out["error"].lower())

    def test_exit_zero_with_no_trees_is_unknown_and_drops_stderr(self):
        def respond(_payload):
            return "", "PROGRESS user:pass@repo", 0

        out, _spy, deps, _lic = self._run(
            respond,
            _args([_upgrade()], projectPath=self._wrapper_project(), graphSource="gradle"),
        )
        deps.assert_not_called()
        self.assertEqual(out["advisory"], "unknown")
        self.assertNotIn("user:pass", json.dumps(out))
        self.assertNotIn("PROGRESS", json.dumps(out))
        self.assertEqual(out["error"], "Gradle produced no dependency trees")

    def test_nonzero_with_trees_is_soft_failure(self):
        def respond(payload):
            if payload.get("phase") == "before":
                return _lib_tree("1.0.0") + _matched(0, 0, "1.0.0", "2.0.0"), "", 0
            stdout = _lib_tree("2.0.0", extra="9.0.0") + _matched(1, 0, "1.0.0", "2.0.0")
            return stdout, "stderr-secret", 1

        upgrade = _upgrade(from_version="1.0.0", to_version="2.0.0")
        out, _spy, deps, _lic = self._run(
            respond,
            _args([upgrade], projectPath=self._wrapper_project(), graphSource="gradle"),
        )
        deps.assert_not_called()
        self.assertEqual(out["graphSource"], "gradle")
        self.assertTrue(out["dependencies"]["added"])
        self.assertFalse(out["diffReliable"])
        self.assertEqual(out["advisory"], "review")
        self.assertNotIn("error", out)
        self.assertNotIn("stderr-secret", json.dumps(out))

    def test_stderr_on_launch_failure_is_truncated(self):
        secret = "TAIL-SECRET"
        stderr = ("x" * 500) + secret

        def respond(_payload):
            return "", stderr, 1

        out, _spy, _deps, _lic = self._run(
            respond,
            _args([_upgrade()], projectPath=self._wrapper_project(), graphSource="gradle"),
        )
        self.assertEqual(out["advisory"], "unknown")
        self.assertEqual(len(out["error"]), 500)
        self.assertNotIn(secret, out["error"])
        self.assertNotIn("Traceback", out["error"])

    def test_gradle_without_wrapper_does_not_launch_or_fall_back(self):
        root = tempfile.mkdtemp(prefix="maven-mcp-no-wrapper-")
        self.addCleanup(shutil.rmtree, root, True)
        with open(os.path.join(root, "build.gradle"), "w", encoding="utf-8") as handle:
            handle.write("plugins { id 'java' }\n")
        with unittest.mock.patch.object(server, "_gradle_run") as run, \
                unittest.mock.patch.object(server, "fetch_depsdev_dependencies") as deps, \
                unittest.mock.patch.object(server, "query_osv_batch") as osv:
            out = server.compare_upgrade_closure(_args(
                [_upgrade()], projectPath=root, graphSource="gradle",
            ))
        run.assert_not_called()
        deps.assert_not_called()
        osv.assert_not_called()
        self.assertEqual(out["graphSource"], "gradle")
        self.assertEqual(out["advisory"], "unknown")
        self.assertIn("wrapper", out["error"].lower())
        self.assertEqual(out["dependencies"]["added"], [])

    def test_auto_without_wrapper_is_depsdev_once(self):
        root = tempfile.mkdtemp(prefix="maven-mcp-auto-depsdev-")
        self.addCleanup(shutil.rmtree, root, True)
        with open(os.path.join(root, "build.gradle"), "w", encoding="utf-8") as handle:
            handle.write("plugins { id 'java' }\n")
        calls = {"detect": 0}
        real_detect = server._detect_build_system

        def detect(path):
            calls["detect"] += 1
            return real_detect(path)

        def fetch(group_id, artifact_id, version):
            return {
                "ok": True,
                "nodes": [{
                    "groupId": group_id,
                    "artifactId": artifact_id,
                    "version": version,
                    "relation": "SELF",
                }],
                "edges": [],
                "truncated": False,
                "graphError": None,
            }

        with unittest.mock.patch.object(server, "_detect_build_system", detect), \
                unittest.mock.patch.object(server, "_gradle_run") as run, \
                unittest.mock.patch.object(server, "fetch_depsdev_dependencies", fetch), \
                unittest.mock.patch.object(server, "query_osv_batch", _clean_osv):
            out = server.compare_upgrade_closure(_args([_upgrade()], projectPath=root))
        run.assert_not_called()
        self.assertEqual(calls["detect"], 1)
        self.assertEqual(out["graphSource"], "depsdev")
        self.assertEqual(out["advisory"], "none")

    def test_rejects_more_than_twenty_and_a_depsdev_batch(self):
        root = self._wrapper_project()
        many = [
            _upgrade(artifact_id=f"lib{i}", from_version="1.0.0", to_version="1.1.0")
            for i in range(21)
        ]
        two = [
            _upgrade(),
            _upgrade(artifact_id="other", from_version="1.0.0", to_version="1.1.0"),
        ]
        with unittest.mock.patch.object(server, "_gradle_run") as run, \
                unittest.mock.patch.object(server, "fetch_depsdev_dependencies") as deps:
            with self.assertRaisesRegex(ValueError, "not truncated"):
                server.compare_upgrade_closure(_args(many, projectPath=root, graphSource="gradle"))
            with self.assertRaisesRegex(ValueError, "exactly one"):
                server.compare_upgrade_closure(_args(two, projectPath=root, graphSource="depsdev"))
        run.assert_not_called()
        deps.assert_not_called()

    def test_marker_only_does_not_launch(self):
        marker = _upgrade(
            group_id="com.example.plugin",
            artifact_id="com.example.plugin.gradle.plugin",
        )
        with unittest.mock.patch.object(server, "_gradle_run") as run:
            out = server.compare_upgrade_closure(_args(
                [marker],
                projectPath=self._wrapper_project(),
                graphSource="gradle",
            ))
        run.assert_not_called()
        self.assertEqual(out["graphSource"], "gradle")
        self.assertEqual(out["advisory"], "unknown")
        self.assertEqual(out["upgrades"][0]["error"], "plugin marker; closure not compared")
        self.assertEqual(out["targets"], [])

    def test_marker_does_not_drop_the_library(self):
        def respond(payload):
            version = "1.0.0" if payload.get("phase") == "before" else "1.1.0"
            return _lib_tree(version) + _matched(1, 0, "1.0.0", "1.1.0"), "", 0

        marker = _upgrade(
            group_id="com.example.plugin",
            artifact_id="com.example.plugin.gradle.plugin",
            from_version="1.0.0",
            to_version="2.0.0",
        )
        library = _upgrade(from_version="1.0.0", to_version="1.1.0")
        out, spy, _deps, _lic = self._run(
            respond,
            _args([marker, library], projectPath=self._wrapper_project(), graphSource="gradle"),
        )
        self.assertEqual(len(spy.calls), 2)
        self.assertEqual(len(spy.payloads[0]["upgrades"]), 1)
        self.assertEqual(spy.payloads[0]["upgrades"][0]["artifactId"], "lib")
        self.assertEqual(out["upgrades"][0]["error"], "plugin marker; closure not compared")
        self.assertNotIn("error", out["upgrades"][1])
        self.assertEqual(out["targets"][0]["artifactId"], "lib")
        self.assertTrue(out["targets"][0]["landed"])

    def test_module_substitution_is_opt_in(self):
        def respond(payload):
            self.assertEqual(payload["substitution"], "module")
            return _lib_tree("4.12.0") + _matched(2, 0), "", 0

        _out, spy, _deps, _lic = self._run(
            respond,
            _args(
                [_upgrade()],
                projectPath=self._wrapper_project(),
                graphSource="gradle",
                substitution="module",
            ),
        )
        self.assertEqual(spy.payloads[0]["substitution"], "module")
        self.assertEqual(spy.payloads[1]["substitution"], "module")

    def test_all_identity_does_not_launch(self):
        upgrade = _upgrade(from_version="4.12.0", to_version="4.12.0")
        with unittest.mock.patch.object(server, "_gradle_run") as run, \
                unittest.mock.patch.object(server, "query_osv_batch") as osv:
            out = server.compare_upgrade_closure(_args(
                [upgrade], projectPath=self._wrapper_project(), graphSource="gradle",
            ))
        run.assert_not_called()
        osv.assert_not_called()
        self.assertEqual(out["advisory"], "none")
        self.assertNotIn("vulnerabilities", out["targets"][0])

    def _wrapper_project(self):
        root = tempfile.mkdtemp(prefix="maven-mcp-wrapper-flag-")
        self.addCleanup(shutil.rmtree, root, True)
        with open(os.path.join(root, "build.gradle"), "w", encoding="utf-8") as handle:
            handle.write("plugins { id 'java' }\n")
        with open(os.path.join(root, "gradlew"), "w", encoding="utf-8"):
            pass
        return root


def _publish(repo, artifact, version, pom_deps=""):
    base = os.path.join(repo, "com", "example", artifact, version)
    os.makedirs(base, exist_ok=True)
    pom = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<project>\n"
        "  <modelVersion>4.0.0</modelVersion>\n"
        "  <groupId>com.example</groupId>\n"
        f"  <artifactId>{artifact}</artifactId>\n"
        f"  <version>{version}</version>\n"
        f"{pom_deps}"
        "</project>\n"
    )
    pom_path = os.path.join(base, f"{artifact}-{version}.pom")
    jar_path = os.path.join(base, f"{artifact}-{version}.jar")
    with open(pom_path, "w", encoding="utf-8") as handle:
        handle.write(pom)
    with zipfile.ZipFile(jar_path, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
    for path in (pom_path, jar_path):
        with open(path, "rb") as handle:
            data = handle.read()
        for name in ("sha1", "sha256"):
            digest = hashlib.new(name, data).hexdigest()
            with open(path + "." + name, "w", encoding="utf-8") as handle:
                handle.write(digest + "\n")


def _metadata(repo, artifact, versions):
    base = os.path.join(repo, "com", "example", artifact)
    os.makedirs(base, exist_ok=True)
    listed = "\n".join(f"      <version>{version}</version>" for version in versions)
    latest = versions[-1] if versions else ""
    text = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<metadata>\n"
        "  <groupId>com.example</groupId>\n"
        f"  <artifactId>{artifact}</artifactId>\n"
        "  <versioning>\n"
        f"    <latest>{latest}</latest>\n"
        f"    <release>{latest}</release>\n"
        "    <versions>\n"
        f"{listed}\n"
        "    </versions>\n"
        "  </versioning>\n"
        "</metadata>\n"
    )
    path = os.path.join(base, "maven-metadata.xml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
    with open(path + ".sha1", "w", encoding="utf-8") as handle:
        handle.write(digest + "\n")


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class GradleWrapperFixtureTest(unittest.TestCase):
    """Real gradlew. Not strictly, not +, and 9.9.9 is not published."""

    @classmethod
    def setUpClass(cls):
        cls._root = tempfile.mkdtemp(prefix="maven-mcp-upgrade-fixtures-")
        seed = os.path.join(cls._root, "seed")
        os.makedirs(seed)
        _write(os.path.join(seed, "settings.gradle"), "rootProject.name = 'seed'\n")
        _write(os.path.join(seed, "build.gradle"), "plugins { id 'java' }\n")
        subprocess.run(
            ["gradle", "wrapper", "--gradle-version", "9.7.1", "--no-daemon"],
            cwd=seed,
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._root, ignore_errors=True)

    def _copy_wrapper(self, project):
        seed = os.path.join(self._root, "seed")
        for name in ("gradlew", "gradlew.bat"):
            src = os.path.join(seed, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(project, name))
        shutil.copytree(
            os.path.join(seed, "gradle", "wrapper"),
            os.path.join(project, "gradle", "wrapper"),
        )
        os.chmod(os.path.join(project, "gradlew"), 0o755)

    def _project(self, name, build_gradle, publish):
        project = os.path.join(self._root, name)
        os.makedirs(project)
        self.addCleanup(shutil.rmtree, project, True)
        repo = os.path.join(project, "repo")
        publish(repo)
        _write(os.path.join(project, "settings.gradle"), f"rootProject.name = '{name}'\n")
        _write(os.path.join(project, "build.gradle"), build_gradle)
        # A configuration-cache hit skips the init script and looks like zero trees.
        _write(
            os.path.join(project, "gradle.properties"),
            "org.gradle.configuration-cache=true\n",
        )
        self._copy_wrapper(project)
        return project

    def _compare(self, project, upgrade):
        captured = {}
        real_diff = server._diff_closure
        real_run = server._gradle_run
        seen = {"scripts": [], "cmds": [], "overlap": 0, "max": 0}

        def spy_diff(before, after, upgrades, project_graph=True):
            captured["before"] = before
            captured["after"] = after
            return real_diff(before, after, upgrades, project_graph=project_graph)

        def spy_run(cmd, **kwargs):
            if seen["overlap"]:
                seen["max"] = seen["overlap"] + 1
            seen["overlap"] += 1
            seen["max"] = max(seen["max"], seen["overlap"])
            try:
                seen["cmds"].append(list(cmd))
                if "--init-script" in cmd:
                    path = cmd[cmd.index("--init-script") + 1]
                    with open(path, "rb") as handle:
                        seen["scripts"].append(handle.read())
                return real_run(cmd, **kwargs)
            finally:
                seen["overlap"] -= 1

        with unittest.mock.patch.object(server, "_diff_closure", spy_diff), \
                unittest.mock.patch.object(server, "_gradle_run", spy_run), \
                unittest.mock.patch.object(server, "query_osv_batch", _clean_osv), \
                unittest.mock.patch.object(server, "fetch_depsdev_dependencies") as deps, \
                unittest.mock.patch.object(server, "fetch_depsdev_licenses", return_value={
                    "ok": True, "licenses": ["MIT"], "error": None,
                }):
            out = server.compare_upgrade_closure(_args([upgrade], projectPath=project))
        deps.assert_not_called()
        self.assertEqual(seen["max"], 1)
        self.assertEqual(len(seen["scripts"]), 2)
        self.assertEqual(seen["scripts"][0], seen["scripts"][1])
        for cmd in seen["cmds"]:
            self.assertIn("--no-configuration-cache", cmd)
        self.assertEqual(out["graphSource"], "gradle")
        return out, captured

    def test_project_a_versionless_exact(self):
        def publish(repo):
            _publish(repo, "lib", "4.9.3")
            _publish(repo, "lib", "4.12.0")
            _metadata(repo, "lib", ["4.9.3"])

        project = self._project("project-a", """\
plugins {
    id "java"
}

repositories {
    maven { url uri("${rootDir}/repo") }
}

dependencies {
    implementation("com.example:lib")
    constraints {
        implementation("com.example:lib:4.9.3")
    }
}
""", publish)
        out, captured = self._compare(project, _upgrade())
        self.assertEqual(_versions(captured["before"]), ["4.9.3"])
        self.assertEqual(_versions(captured["after"]), ["4.12.0"])
        target = out["targets"][0]
        self.assertEqual(target["selectedVersions"], ["4.12.0"])
        self.assertTrue(target["landed"])
        self.assertTrue(target["targetPresent"])
        self.assertTrue(target["rewroteVersionless"])
        self.assertGreater(target["substitutionMatched"], 0)
        empty = [
            row for row in out["requests"]
            if row["requestedVersion"] == "" and row["rewritten"] == 1
        ]
        self.assertTrue(empty)
        for row in out["requests"]:
            if row["requestedVersion"] == "4.9.3":
                self.assertEqual(row["rewritten"], 1)
        self.assertEqual(out["advisory"], "review")
        self.assertIn(server._GRADLE_LICENSE_METADATA_NOTE, out["notes"])
        self.assertFalse(any(
            note.startswith("Graphs are resolved per root") for note in out["notes"]
        ))

    def test_project_b_exact_does_not_rewrite_carrier(self):
        def publish(repo):
            for version in ("4.9.3", "4.12.0", "5.0.0"):
                _publish(repo, "lib", version)
            _metadata(repo, "lib", ["4.9.3", "4.12.0", "5.0.0"])
            _publish(
                repo,
                "carrier",
                "1.0.0",
                pom_deps=(
                    "  <dependencies>\n"
                    "    <dependency>\n"
                    "      <groupId>com.example</groupId>\n"
                    "      <artifactId>lib</artifactId>\n"
                    "      <version>5.0.0</version>\n"
                    "    </dependency>\n"
                    "  </dependencies>\n"
                ),
            )
            _metadata(repo, "carrier", ["1.0.0"])

        project = self._project("project-b", """\
plugins {
    id "java"
}

repositories {
    maven { url uri("${rootDir}/repo") }
}

dependencies {
    implementation("com.example:lib:4.9.3")
    implementation("com.example:carrier:1.0.0")
}
""", publish)
        out, captured = self._compare(project, _upgrade())
        self.assertEqual(_versions(captured["after"]), ["5.0.0"])
        target = out["targets"][0]
        self.assertEqual(target["selectedVersions"], ["5.0.0"])
        self.assertFalse(target["landed"])
        self.assertTrue(target["targetPresent"])
        self.assertFalse(target["rewroteVersionless"])
        self.assertGreater(target["substitutionMatched"], 0)
        direct = [
            row for row in out["requests"]
            if row["requestedVersion"] == "4.9.3" and row["rewritten"] == 1
        ]
        carrier = [
            row for row in out["requests"]
            if row["requestedVersion"] == "5.0.0" and row["rewritten"] == 0
        ]
        self.assertTrue(direct)
        self.assertTrue(carrier)
        self.assertEqual(out["advisory"], "review")


if __name__ == "__main__":
    unittest.main()
