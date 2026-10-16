"""Gradle-resolved dependency scanning (#392 / #393 / #394 / #401).

#401 collapsed the previous "1 probe + P*(1+C) `gradlew` invocations" model
into a single ``--init-script`` invocation whose Groovy script dumps resolved
first-level GAVs for the production runtime configurations the selector keeps,
plus the buildscript classpath, in one configuration phase. These tests exercise: the init-script
generator, the single-invocation output parser, and the end-to-end
``_gradle_resolve_dependencies``/``scan_project`` behavior against mocked
``_run_gradle_command`` output shaped like a real init-script dump.
"""

import os
import subprocess
import sys
import threading
import unittest
import unittest.mock
from contextlib import ExitStack

from _helpers import (
    mock_gradle_resolve,
    server,
    temp_project,
    write_fake_gradlew,
    write_smart_gradlew,
)


# A two-module (root ":" + ":app") single-invocation dump: ":app" resolves
# io.ktor via releaseRuntimeClasspath (with conflict-resolution substitution
# already applied, matching what `resolutionResult` reports — no "->" text to
# parse), plus a root buildscript classpath entry.
SINGLE_INVOCATION_FIXTURE = (
    "===MAVEN_MCP_MODULE=== :\n"
    "===MAVEN_MCP_MODULE_END===\n"
    "===MAVEN_MCP_MODULE=== :app\n"
    "===MAVEN_MCP_CONFIG=== releaseRuntimeClasspath\n"
    "io.ktor:ktor-client-core:3.1.2\n"
    "com.squareup.okhttp3:okhttp:4.12.0\n"
    "===MAVEN_MCP_CONFIG_END===\n"
    "===MAVEN_MCP_CONFIG=== compileClasspath\n"
    "io.ktor:ktor-client-core:3.1.2\n"
    "===MAVEN_MCP_CONFIG_END===\n"
    "===MAVEN_MCP_MODULE_END===\n"
    "===MAVEN_MCP_BUILDENV===\n"
    "com.android.tools.build:gradle:8.0.0\n"
    "org.jetbrains.kotlin:kotlin-gradle-plugin:2.0.0\n"
    "===MAVEN_MCP_BUILDENV_END===\n"
)

CONFIG_ERROR_FIXTURE = (
    "===MAVEN_MCP_MODULE=== :app\n"
    "===MAVEN_MCP_CONFIG=== releaseRuntimeClasspath\n"
    "io.ktor:ktor-client-core:3.1.2\n"
    "===MAVEN_MCP_CONFIG_END===\n"
    "===MAVEN_MCP_CONFIG=== debugRuntimeClasspath\n"
    "===MAVEN_MCP_CONFIG_ERROR=== org.gradle.api.internal.artifacts.ivyservice.ResolveException: Could not resolve all dependencies\n"
    "===MAVEN_MCP_CONFIG_END===\n"
    "===MAVEN_MCP_MODULE_END===\n"
    "===MAVEN_MCP_BUILDENV===\n"
    "===MAVEN_MCP_BUILDENV_END===\n"
)

ONLY_BUILDENV_FIXTURE = (
    "===MAVEN_MCP_MODULE=== :\n"
    "===MAVEN_MCP_MODULE_END===\n"
    "===MAVEN_MCP_BUILDENV===\n"
    "com.android.tools.build:gradle:8.0.0\n"
    "===MAVEN_MCP_BUILDENV_END===\n"
)


class TestGenerateGradleResolveInitScript(unittest.TestCase):
    def test_script_contains_expected_markers_and_apis(self):
        script = server._generate_gradle_resolve_init_script()
        # Marker protocol the parser depends on.
        for marker in (
            "===MAVEN_MCP_MODULE===",
            "===MAVEN_MCP_MODULE_END===",
            "===MAVEN_MCP_CONFIG===",
            "===MAVEN_MCP_CONFIG_ERROR===",
            "===MAVEN_MCP_CONFIG_END===",
            "===MAVEN_MCP_BUILDENV===",
            "===MAVEN_MCP_BUILDENV_ERROR===",
            "===MAVEN_MCP_BUILDENV_END===",
        ):
            self.assertIn(marker, script)
        # Modern resolutionResult API (never throws ResolveException) and the
        # project-vs-module distinction that excludes project(":x") deps.
        self.assertIn("resolutionResult", script)
        self.assertIn("ResolvedDependencyResult", script)
        self.assertIn("ModuleComponentIdentifier", script)
        self.assertIn("canBeResolved", script)
        self.assertIn("projectsEvaluated", script)

    def test_resolution_runs_inside_project_mutable_state(self):
        # Gradle 9 rejects resolving a configuration without the owning
        # project's exclusive lock ("attempted without an exclusive lock").
        script = server._generate_gradle_resolve_init_script()
        self.assertIn("applyToMutableState", script)

    def test_no_untrusted_interpolation(self):
        # Static script string, not an f-string / .format() with project data.
        script = server._generate_gradle_resolve_init_script()
        self.assertNotIn("{project_root}", script)

    def test_project_walk_reads_resolution_result_only_for_selected_configurations(self):
        """Fail if a dropped project configuration can reach resolutionResult.

        A parsed dump that lacks compileClasspath does not show this bug: that
        absence is the Python drop after Gradle has already resolved the graph.
        """
        script = server._generate_gradle_resolve_init_script()
        marker = "===MAVEN_MCP_BUILDENV==="
        self.assertIn(marker, script)
        prefix, suffix = script.split(marker, 1)
        # BUILDENV is the root buildscript classpath, not a project configuration.
        # Deleting it is not a pass; its resolutionResult read is not a failure.
        self.assertIn("buildscript.configurations", suffix)
        self.assertIn("resolutionResult", suffix)

        self.assertIn("gradle.projectsEvaluated", prefix)
        self.assertEqual(
            prefix.count("resolutionResult"),
            1,
            "the project walk must still read resolutionResult, and only once",
        )
        read_at = prefix.index("resolutionResult")
        self.assertLess(prefix.index("gradle.projectsEvaluated"), read_at)
        help_at = prefix.find("help.doFirst")
        if help_at >= 0:
            _, brace, close, _ = _closure_after(prefix, "help.doFirst")
            self.assertFalse(
                brace < read_at < close,
                "project-configuration resolutionResult read must not be inside help.doFirst",
            )

        self.assertEqual(prefix.count("p.configurations.each"), 1)
        each_idx, _, each_close, each_body = _closure_after(prefix, "p.configurations.each")
        self.assertNotIn(
            "resolutionResult",
            each_body,
            "p.configurations.each must only build the selected set; "
            "reading resolutionResult here resolves configurations the selector drops",
        )
        self.assertIn(
            "canBeResolved && isProductionRuntime(",
            _normalize_ws(each_body),
        )
        self.assertTrue(
            any(each_close > site > each_idx for site in _call_sites(prefix, "isProductionRuntime")),
            "the configuration walk must call isProductionRuntime before a name enters the set",
        )

        self.assertEqual(prefix.count("allprojects.each"), 1)
        _, ap_brace, ap_close, _ = _closure_after(prefix, "allprojects.each")
        self.assertTrue(
            ap_brace < read_at < ap_close,
            "project-configuration read must stay inside the per-project loop",
        )

        release_at = prefix.find('startsWith("release")', each_close)
        ends_at = prefix.find('endsWith("RuntimeClasspath")', each_close)
        exclude_at = prefix.find('!= "runtimeClasspath"', each_close)
        self.assertGreater(release_at, each_close, "missing per-project release drop")
        self.assertGreater(ends_at, each_close, "missing per-project release drop")
        self.assertGreater(exclude_at, each_close, "missing exclusion of bare runtimeClasspath")
        # A sibling project's release*RuntimeClasspath must not drop this
        # project's runtimeClasspath. The drop is inside this project's loop,
        # not on one flat list of every project.
        self.assertLess(
            max(release_at, ends_at, exclude_at),
            ap_close,
            "release drop must be inside the per-project allprojects.each",
        )
        self.assertEqual(prefix.count('startsWith("release")'), 1)

        self.assertTrue(
            _call_sites(prefix, "isProductionRuntime"),
            "resolutionResult read is not after an isProductionRuntime call",
        )
        self.assertLess(min(_call_sites(prefix, "isProductionRuntime")), read_at)

        containing = [
            span for span in _each_spans(prefix)
            if span[1] < read_at < span[2]
        ]
        self.assertTrue(containing, "resolutionResult is not inside an each iteration")
        innermost = min(containing, key=lambda span: span[2] - span[1])
        self.assertFalse(
            prefix[:innermost[0]].rstrip().endswith("configurations"),
            "resolutionResult read is still the unfiltered configurations walk",
        )
        self.assertGreater(
            innermost[1],
            max(release_at, ends_at, exclude_at),
            "resolutionResult read must iterate the already-filtered collection",
        )

        upgrade = server._generate_gradle_upgrade_init_script()
        for name in ("isTestConfiguration", "isProductionRuntime"):
            self.assertEqual(
                _normalize_ws(_groovy_def_body(script, name)),
                _normalize_ws(_groovy_def_body(upgrade, name)),
                name + " body must match the upgrade script",
            )

        # With that shape, names that can reach the project-walk read are the
        # names this selector keeps. The table is the oracle for that claim.
        kept_alone = (
            "runtimeClasspath",
            "releaseRuntimeClasspath",
            "debugRuntimeClasspath",
        )
        for name in kept_alone:
            self.assertEqual(server._select_configurations_to_resolve([name]), [name], name)
        dropped_alone = (
            "testRuntimeClasspath",
            "testCompileClasspath",
            "androidTestRuntimeClasspath",
            "compileClasspath",
            "releaseCompileClasspath",
            "debugCompileClasspath",
            "classpath",
            "compileOnly",
            "annotationProcessor",
        )
        for name in dropped_alone:
            self.assertEqual(server._select_configurations_to_resolve([name]), [], name)
        selected = server._select_configurations_to_resolve([
            "runtimeClasspath",
            "releaseRuntimeClasspath",
            "debugRuntimeClasspath",
        ])
        self.assertNotIn("runtimeClasspath", selected)
        self.assertIn("releaseRuntimeClasspath", selected)
        self.assertIn("debugRuntimeClasspath", selected)


def _normalize_ws(text):
    return " ".join(text.split())


def _brace_interior(text, open_index):
    """Return (close_index, interior) for the ``{`` at open_index.

    Skips quotes and comments so a brace in either does not change depth.
    This matches spans. It does not interpret Groovy.
    """
    if open_index < 0 or open_index >= len(text) or text[open_index] != "{":
        raise AssertionError("expected an opening brace")
    i = open_index + 1
    depth = 1
    limit = len(text)
    while i < limit:
        char = text[i]
        if char == "/" and i + 1 < limit and text[i + 1] == "/":
            newline = text.find("\n", i)
            i = limit if newline < 0 else newline + 1
            continue
        if char == "/" and i + 1 < limit and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            if end < 0:
                raise AssertionError("unclosed block comment")
            i = end + 2
            continue
        if char in ("'", '"'):
            quote = char
            i += 1
            while i < limit:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            else:
                raise AssertionError("unclosed string")
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i, text[open_index + 1:i]
        i += 1
    raise AssertionError("unclosed brace")


def _closure_after(text, needle, start=0):
    idx = text.find(needle, start)
    if idx < 0:
        raise AssertionError("missing " + needle)
    brace = text.find("{", idx + len(needle))
    if brace < 0:
        raise AssertionError("missing brace after " + needle)
    close, interior = _brace_interior(text, brace)
    return idx, brace, close, interior


def _groovy_def_body(script, name):
    needle = "def " + name + "("
    idx = script.find(needle)
    if idx < 0:
        raise AssertionError("missing " + needle)
    brace = script.find("{", idx + len(needle))
    if brace < 0:
        raise AssertionError("missing body for " + name)
    _, interior = _brace_interior(script, brace)
    return interior


def _each_spans(text):
    spans = []
    start = 0
    while True:
        idx = text.find(".each", start)
        if idx < 0:
            return spans
        brace = text.find("{", idx + len(".each"))
        if brace < 0:
            return spans
        if text[idx + len(".each"):brace].strip() != "":
            start = idx + len(".each")
            continue
        close, interior = _brace_interior(text, brace)
        spans.append((idx, brace, close, interior))
        start = idx + len(".each")


def _call_sites(text, name):
    needle = name + "("
    sites = []
    start = 0
    while True:
        idx = text.find(needle, start)
        if idx < 0:
            return sites
        if not text[:idx].rstrip().endswith("def"):
            sites.append(idx)
        start = idx + len(needle)


class TestParseSingleInvocationGradleOutput(unittest.TestCase):
    def test_multi_module_parse(self):
        modules, errors, buildenv, buildenv_error = server._parse_single_invocation_gradle_output(
            SINGLE_INVOCATION_FIXTURE
        )
        self.assertEqual(set(modules.keys()), {":", ":app"})
        self.assertEqual(modules[":"], {})
        self.assertEqual(
            set(modules[":app"].keys()), {"releaseRuntimeClasspath", "compileClasspath"}
        )
        self.assertEqual(
            modules[":app"]["releaseRuntimeClasspath"],
            [("io.ktor", "ktor-client-core", "3.1.2"), ("com.squareup.okhttp3", "okhttp", "4.12.0")],
        )
        self.assertEqual(errors, [])
        self.assertEqual(
            set(buildenv),
            {
                ("com.android.tools.build", "gradle", "8.0.0"),
                ("org.jetbrains.kotlin", "kotlin-gradle-plugin", "2.0.0"),
            },
        )
        self.assertIsNone(buildenv_error)

    def test_per_config_error_isolated_from_sibling_config(self):
        modules, errors, _buildenv, _buildenv_error = server._parse_single_invocation_gradle_output(
            CONFIG_ERROR_FIXTURE
        )
        # The failing debugRuntimeClasspath config must not blank out the
        # sibling releaseRuntimeClasspath config resolved in the same module.
        self.assertEqual(
            modules[":app"]["releaseRuntimeClasspath"],
            [("io.ktor", "ktor-client-core", "3.1.2")],
        )
        self.assertEqual(modules[":app"]["debugRuntimeClasspath"], [])
        self.assertTrue(any(":app:debugRuntimeClasspath resolution error:" in e for e in errors))

    def test_buildenv_error_marker(self):
        stdout = (
            "===MAVEN_MCP_MODULE=== :\n"
            "===MAVEN_MCP_MODULE_END===\n"
            "===MAVEN_MCP_BUILDENV===\n"
            "===MAVEN_MCP_BUILDENV_ERROR=== java.lang.Exception: boom\n"
            "===MAVEN_MCP_BUILDENV_END===\n"
        )
        _modules, _errors, buildenv, buildenv_error = server._parse_single_invocation_gradle_output(stdout)
        self.assertEqual(buildenv, [])
        self.assertIn("boom", buildenv_error)


class TestGradleConfigurationSelection(unittest.TestCase):
    def test_selection_prefers_release_runtime(self):
        available = [
            "releaseRuntimeClasspath",
            "debugRuntimeClasspath",
            "releaseCompileClasspath",
            "compileClasspath",
            "runtimeClasspath",
            "testRuntimeClasspath",
        ]
        selected = server._select_configurations_to_resolve(available)
        self.assertIn("releaseRuntimeClasspath", selected)
        self.assertIn("debugRuntimeClasspath", selected)
        self.assertNotIn("runtimeClasspath", selected)
        self.assertNotIn("compileClasspath", selected)
        self.assertNotIn("releaseCompileClasspath", selected)
        self.assertNotIn("testRuntimeClasspath", selected)


class TestGradleResolveTimeoutOverride(unittest.TestCase):
    """MAVEN_MCP_GRADLE_TIMEOUT override for the single-invocation timeout scope
    change (#401 code review follow-up): the timeout now bounds ONE invocation
    resolving every project/configuration, not one subprocess call per config,
    so it must be overridable for very large multi-module projects."""

    def test_default_when_unset(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAVEN_MCP_GRADLE_TIMEOUT", None)
            self.assertEqual(server._gradle_resolve_timeout_seconds(), server.GRADLE_RESOLVE_TIMEOUT)

    def test_valid_override_used(self):
        with unittest.mock.patch.dict(os.environ, {"MAVEN_MCP_GRADLE_TIMEOUT": "600"}):
            self.assertEqual(server._gradle_resolve_timeout_seconds(), 600)

    def test_non_integer_falls_back_to_default(self):
        with unittest.mock.patch.dict(os.environ, {"MAVEN_MCP_GRADLE_TIMEOUT": "not-a-number"}):
            self.assertEqual(server._gradle_resolve_timeout_seconds(), server.GRADLE_RESOLVE_TIMEOUT)

    def test_non_positive_falls_back_to_default(self):
        with unittest.mock.patch.dict(os.environ, {"MAVEN_MCP_GRADLE_TIMEOUT": "0"}):
            self.assertEqual(server._gradle_resolve_timeout_seconds(), server.GRADLE_RESOLVE_TIMEOUT)
        with unittest.mock.patch.dict(os.environ, {"MAVEN_MCP_GRADLE_TIMEOUT": "-5"}):
            self.assertEqual(server._gradle_resolve_timeout_seconds(), server.GRADLE_RESOLVE_TIMEOUT)

    def test_gradle_resolve_dependencies_passes_override_as_timeout(self):
        captured = {}

        def _fake_run(project_root, gradlew, args, timeout=server.GRADLE_RESOLVE_TIMEOUT):
            captured["timeout"] = timeout
            return 0, SINGLE_INVOCATION_FIXTURE, ""

        with temp_project({}) as root:
            write_fake_gradlew(root)
            with unittest.mock.patch.dict(os.environ, {"MAVEN_MCP_GRADLE_TIMEOUT": "900"}):
                with unittest.mock.patch.object(server, "_run_gradle_command", _fake_run):
                    server._gradle_resolve_dependencies(root)
        self.assertEqual(captured["timeout"], 900)

    def test_gradle_resolve_dependencies_disables_configuration_cache(self):
        # A configuration-cache hit skips the configuration phase, so the init
        # script's projectsEvaluated hook never runs and the dump is empty.
        captured = {}

        def _fake_run(project_root, gradlew, args, timeout=server.GRADLE_RESOLVE_TIMEOUT):
            captured["args"] = args
            return 0, SINGLE_INVOCATION_FIXTURE, ""

        with temp_project({}) as root:
            write_fake_gradlew(root)
            with unittest.mock.patch.object(server, "_run_gradle_command", _fake_run):
                server._gradle_resolve_dependencies(root)
        self.assertIn("--no-configuration-cache", captured["args"])


class TestFindGradleWrapper(unittest.TestCase):
    def test_windows_selects_bat_when_both_exist(self):
        with temp_project({}) as root:
            for name in ("gradlew", "gradlew.bat"):
                with open(os.path.join(root, name), "w", encoding="utf-8") as fh:
                    fh.write(name + "\n")
            with unittest.mock.patch.object(server.os, "name", "nt"):
                chosen = server._find_gradle_wrapper(root)
            self.assertEqual(os.path.basename(chosen), "gradlew.bat")

    def test_posix_selects_gradlew_when_both_exist(self):
        with temp_project({}) as root:
            for name in ("gradlew", "gradlew.bat"):
                with open(os.path.join(root, name), "w", encoding="utf-8") as fh:
                    fh.write(name + "\n")
            with unittest.mock.patch.object(server.os, "name", "posix"):
                chosen = server._find_gradle_wrapper(root)
            self.assertEqual(os.path.basename(chosen), "gradlew")


# Grandchild lifetime. Longer than the join deadline below, so a read that
# waits for this process cannot pass as "returned within a few seconds".
_GRANDCHILD_HOLD_SECONDS = 30
_WINDOWS_JOB_JOIN_SECONDS = 8
_CREATE_SUSPENDED = 0x00000004


class _KernelCall:
    """Callable with the restype/argtypes attributes ``WinDLL`` functions have."""

    def __init__(self, fn):
        self._fn = fn
        self.restype = None
        self.argtypes = None

    def __call__(self, *args, **kwargs):
        return self._fn(*args, **kwargs)


class _JobModel:
    def __init__(self):
        self.members = []
        self.closed = False


class _PipeModel:
    def __init__(self):
        self.parent_write_open = True
        self.held_by_child = False
        self.dups = []

    def writer_open(self):
        return self.parent_write_open or self.held_by_child or bool(self.dups)


class _HandleRec:
    def __init__(self, kind, fd=None, pipe=None):
        self.kind = kind
        self.fd = fd
        self.pipe = pipe
        self.closed = False
        self.inheritable = True


class _ProcModel:
    def __init__(self):
        self.job = None
        self.resumed = False
        self.started_outside = False
        self.in_job_before_resume = False
        self.stdout_dup = None
        self.stderr_dup = None
        self.stdout_pipe = None
        self.stderr_pipe = None
        self.direct = None
        self.grandchild = None
        self.exited = False
        self.exit_code = None
        self.exit_event = threading.Event()


class WindowsGradleJobStandIn:
    """kernel32 stand-in for the grandchild-holds-the-pipe hang.

    ``TerminateJobObject`` kills the grandchild only when the wrapper was
    assigned to the job before ``ResumeThread``. ``TerminateProcess`` kills
    only the direct child and leaves the grandchild's pipe ends open.
    ``bad_gradle_run`` is the current Windows ``subprocess.run`` cleanup:
    kill the direct child, then read with no timeout.
    """

    def __init__(self, quick_exit=None, fail_bat=False):
        self.quick_exit = quick_exit
        self.fail_bat = fail_bat
        self.last_error = 0
        self.grandchild = None
        self.direct = None
        self.env = None
        self.command_lines = []
        self.application_names = []
        self.creation_flags = []
        self.wait_ms = None
        self.terminate_job_called = False
        self.kill_on_job_close = False
        self.assigned_before_resume = False
        self.direct_only_killed = False
        self.job_killed_grandchild = False
        self.unbounded_after_direct_kill = False
        self.gradle_run_called = False
        self.child_holds_stdout = False
        self.child_holds_stderr = False
        self._next = 1
        self._create_count = 0
        self._fds = []
        self._children = []
        self.handles = {}
        self.procs = {}
        self.threads = {}
        self.jobs = {}
        for name in (
            "CreateJobObjectW",
            "CreatePipe",
            "SetHandleInformation",
            "GetStdHandle",
            "CreateProcessW",
            "AssignProcessToJobObject",
            "ResumeThread",
            "WaitForSingleObject",
            "TerminateJobObject",
            "TerminateProcess",
            "GetExitCodeProcess",
            "ReadFile",
            "PeekNamedPipe",
            "CloseHandle",
            "SetInformationJobObject",
        ):
            setattr(self, name, _KernelCall(getattr(self, "_" + name)))

    def _as_int(self, handle):
        if handle is None:
            return 0
        value = getattr(handle, "value", handle)
        if value is None:
            return 0
        return int(value)

    def _alloc(self, kind, fd=None, pipe=None):
        hid = self._next
        self._next += 1
        self.handles[hid] = _HandleRec(kind, fd=fd, pipe=pipe)
        return hid

    def _track_fd(self, fd):
        self._fds.append(fd)
        return fd

    def _cmdline(self, buf):
        if isinstance(buf, str):
            return buf
        return buf.value

    def _parse_env(self, buf):
        if buf is None:
            return {}
        if isinstance(buf, dict):
            return dict(buf)
        chars = [buf[i] for i in range(len(buf))]
        env = {}
        for part in "".join(chars).split("\0"):
            if not part or "=" not in part:
                continue
            key, value = part.split("=", 1)
            env[key] = value
        return env

    def _close_dup(self, proc, which):
        dup = proc.stdout_dup if which == "stdout" else proc.stderr_dup
        pipe = proc.stdout_pipe if which == "stdout" else proc.stderr_pipe
        if dup is None:
            return
        try:
            os.close(dup)
        except OSError:
            pass
        if pipe is not None and dup in pipe.dups:
            pipe.dups.remove(dup)
        if which == "stdout":
            proc.stdout_dup = None
        else:
            proc.stderr_dup = None

    def _inherit_write(self, handle):
        rec = self.handles.get(self._as_int(handle))
        if rec is None or rec.fd is None or rec.pipe is None or rec.kind != "write":
            return None, None
        if not rec.inheritable:
            return None, None
        dup = self._track_fd(os.dup(rec.fd))
        rec.pipe.dups.append(dup)
        return rec.pipe, dup

    def _spawn_sleeper(self, stdout, stderr):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(%d)" % _GRANDCHILD_HOLD_SECONDS],
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            close_fds=True,
        )
        self._children.append(child)
        return child

    def _start(self, proc):
        def _write_all(fd, data):
            # One os.write returns once the pipe buffer is full. A loop is
            # what blocks when nobody is reading, which is the hang under test.
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                if n <= 0:
                    break
                view = view[n:]

        if self.quick_exit is not None:
            out, err = self.quick_exit
            if proc.stdout_dup is not None:
                if out:
                    _write_all(proc.stdout_dup, out)
                self._close_dup(proc, "stdout")
            if proc.stderr_dup is not None:
                if err:
                    _write_all(proc.stderr_dup, err)
                self._close_dup(proc, "stderr")
            proc.exit_code = 0
            proc.exited = True
            proc.exit_event.set()
            return
        grandchild = self._spawn_sleeper(
            proc.stdout_dup if proc.stdout_dup is not None else subprocess.DEVNULL,
            proc.stderr_dup if proc.stderr_dup is not None else subprocess.DEVNULL,
        )
        direct = self._spawn_sleeper(subprocess.DEVNULL, subprocess.DEVNULL)
        proc.grandchild = grandchild
        proc.direct = direct
        self.grandchild = grandchild
        self.direct = direct
        if proc.stdout_pipe is not None and proc.stdout_dup is not None:
            proc.stdout_pipe.held_by_child = True
            self.child_holds_stdout = True
        if proc.stderr_pipe is not None and proc.stderr_dup is not None:
            proc.stderr_pipe.held_by_child = True
            self.child_holds_stderr = True
        # Our copies must not keep the write ends open after the grandchild
        # has inherited them. TerminateProcess on the direct child does not
        # close the grandchild's copies.
        self._close_dup(proc, "stdout")
        self._close_dup(proc, "stderr")

    def _kill_tree(self, proc):
        for child in (proc.direct, proc.grandchild):
            if child is not None and child.poll() is None:
                child.kill()
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        if proc.stdout_pipe is not None:
            proc.stdout_pipe.held_by_child = False
        if proc.stderr_pipe is not None:
            proc.stderr_pipe.held_by_child = False
        self._close_dup(proc, "stdout")
        self._close_dup(proc, "stderr")
        proc.exited = True
        proc.exit_code = 1
        proc.exit_event.set()

    def _grandchild_alive(self):
        child = self.grandchild
        return child is not None and child.poll() is None

    def _note_unbounded(self):
        if self.direct_only_killed and not self.job_killed_grandchild and self._grandchild_alive():
            self.unbounded_after_direct_kill = True

    def bad_gradle_run(self, args, cwd=None, capture_output=None, text=None, timeout=None, env=None, **_kwargs):
        """Current Windows cleanup: kill only the direct child, then read until EOF."""
        self.gradle_run_called = True
        read_out, write_out = os.pipe()
        read_err, write_err = os.pipe()
        self._track_fd(read_out)
        self._track_fd(write_out)
        self._track_fd(read_err)
        self._track_fd(write_err)
        grandchild = self._spawn_sleeper(write_out, write_err)
        direct = self._spawn_sleeper(subprocess.DEVNULL, subprocess.DEVNULL)
        os.close(write_out)
        os.close(write_err)
        self.grandchild = grandchild
        self.direct = direct
        self.child_holds_stdout = True
        self.child_holds_stderr = True
        if direct.poll() is None:
            direct.kill()
            direct.wait(timeout=2)
        self.direct_only_killed = True
        self.unbounded_after_direct_kill = True
        os.read(read_out, 1)
        return subprocess.CompletedProcess(args, 124, "", "")

    def stop(self):
        for child in list(self._children):
            if child.poll() is None:
                child.kill()
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        for proc in self.procs.values():
            if proc.stdout_pipe is not None:
                proc.stdout_pipe.held_by_child = False
            if proc.stderr_pipe is not None:
                proc.stderr_pipe.held_by_child = False

    def close_fds(self):
        for fd in list(self._fds):
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()

    def _CreateJobObjectW(self, _attrs, name):
        if name not in (None, ""):
            self.named_job = True
        job = self._alloc("job")
        self.jobs[job] = _JobModel()
        return job

    def _CreatePipe(self, h_read, h_write, _sa, _n_size):
        read_fd, write_fd = os.pipe()
        self._track_fd(read_fd)
        self._track_fd(write_fd)
        pipe = _PipeModel()
        h_read._obj.value = self._alloc("read", fd=read_fd, pipe=pipe)
        h_write._obj.value = self._alloc("write", fd=write_fd, pipe=pipe)
        return 1

    def _SetHandleInformation(self, handle, mask, flags):
        rec = self.handles.get(self._as_int(handle))
        if rec is not None and int(mask) & 1:
            rec.inheritable = bool(int(flags) & 1)
        return 1

    def _GetStdHandle(self, _which):
        return self._alloc("stdin")

    def _CreateProcessW(self, app, cmdline, _pa, _ta, _inherit, flags, env, cwd, startup, process_info):
        self._create_count += 1
        self.application_names.append(app)
        self.command_lines.append(self._cmdline(cmdline))
        self.creation_flags.append(int(flags))
        self.env = self._parse_env(env)
        self.cwd = cwd
        if self.fail_bat and self._create_count == 1:
            self.last_error = 193
            return 0
        si = startup._obj
        proc = _ProcModel()
        suspended = bool(int(flags) & _CREATE_SUSPENDED)
        proc.stdout_pipe, proc.stdout_dup = self._inherit_write(si.hStdOutput)
        proc.stderr_pipe, proc.stderr_dup = self._inherit_write(si.hStdError)
        process = self._alloc("process")
        thread = self._alloc("thread")
        self.procs[process] = proc
        self.threads[thread] = proc
        info = process_info._obj
        info.hProcess = process
        info.hThread = thread
        info.dwProcessId = process
        info.dwThreadId = thread
        if not suspended:
            # Primary thread already ran. A later assign cannot catch the JVM.
            proc.started_outside = True
            proc.resumed = True
            self._start(proc)
        return 1

    def _AssignProcessToJobObject(self, job, process):
        proc = self.procs.get(self._as_int(process))
        job_rec = self.jobs.get(self._as_int(job))
        if proc is None or job_rec is None:
            self.last_error = 6
            return 0
        proc.job = job_rec
        job_rec.members.append(proc)
        return 1

    def _ResumeThread(self, thread):
        proc = self.threads.get(self._as_int(thread))
        if proc is None:
            self.last_error = 6
            return 0xFFFFFFFF
        if not proc.resumed:
            proc.resumed = True
            proc.in_job_before_resume = proc.job is not None and not proc.started_outside
            if proc.in_job_before_resume:
                self.assigned_before_resume = True
            self._start(proc)
        return 1

    def _WaitForSingleObject(self, handle, ms):
        self.wait_ms = int(ms)
        proc = self.procs.get(self._as_int(handle))
        if proc is None:
            self.last_error = 6
            return 0xFFFFFFFF
        if proc.exited:
            return 0
        if int(ms) == 0xFFFFFFFF or int(ms) > 0xFFFFFFFE:
            self._note_unbounded()
            proc.exit_event.wait(_GRANDCHILD_HOLD_SECONDS)
            return 0 if proc.exited else 0x102
        if ms:
            proc.exit_event.wait(int(ms) / 1000.0)
        return 0 if proc.exited else 0x102

    def _TerminateJobObject(self, job, _code):
        self.terminate_job_called = True
        job_rec = self.jobs.get(self._as_int(job))
        if job_rec is None:
            self.last_error = 6
            return 0
        for proc in list(job_rec.members):
            if not proc.in_job_before_resume:
                continue
            self._kill_tree(proc)
            self.job_killed_grandchild = True
        return 1

    def _TerminateProcess(self, process, code):
        proc = self.procs.get(self._as_int(process))
        if proc is None:
            self.last_error = 6
            return 0
        self.direct_only_killed = True
        if proc.direct is not None and proc.direct.poll() is None:
            proc.direct.kill()
            try:
                proc.direct.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        if not proc.resumed:
            self._close_dup(proc, "stdout")
            self._close_dup(proc, "stderr")
        proc.exited = True
        proc.exit_code = int(code)
        proc.exit_event.set()
        return 1

    def _GetExitCodeProcess(self, process, code_ptr):
        proc = self.procs.get(self._as_int(process))
        if proc is None:
            self.last_error = 6
            return 0
        code_ptr._obj.value = 259 if not proc.exited else int(proc.exit_code or 0)
        return 1

    def _ReadFile(self, handle, buf, size, nread, _overlapped):
        rec = self.handles.get(self._as_int(handle))
        if rec is None or rec.fd is None:
            self.last_error = 6
            return 0
        self._note_unbounded()
        try:
            data = os.read(rec.fd, int(size) or 1)
        except OSError:
            data = b""
        if data and buf is not None:
            buf[:len(data)] = data
        if nread is not None:
            nread._obj.value = len(data)
        return 1

    def _PeekNamedPipe(self, handle, _buf, _size, _nread, avail, _left):
        rec = self.handles.get(self._as_int(handle))
        if rec is None or rec.pipe is None:
            self.last_error = 6
            return 0
        if not rec.pipe.writer_open():
            self.last_error = 109
            return 0
        if avail is not None:
            avail._obj.value = 0
        return 1

    def _CloseHandle(self, handle):
        key = self._as_int(handle)
        if key in self.jobs:
            self.jobs[key].closed = True
            return 1
        rec = self.handles.get(key)
        if rec is None or rec.closed:
            return 0
        rec.closed = True
        if rec.kind == "write" and rec.pipe is not None:
            rec.pipe.parent_write_open = False
        if rec.fd is not None:
            try:
                os.close(rec.fd)
            except OSError:
                pass
            rec.fd = None
        return 1

    def _SetInformationJobObject(self, _job, _info_class, info, _length):
        flags = getattr(info, "LimitFlags", None)
        if flags is None and hasattr(info, "_obj"):
            flags = getattr(info._obj, "LimitFlags", None)
        if flags is not None and int(flags) & 0x2000:
            self.kill_on_job_close = True
        return 1


def _windows_job_context(standin):
    stack = ExitStack()
    stack.enter_context(unittest.mock.patch.object(
        server, "_gradle_windows_job", return_value=True,
    ))
    stack.enter_context(unittest.mock.patch.object(
        server, "_load_kernel32", return_value=standin,
    ))
    stack.enter_context(unittest.mock.patch.object(
        server, "_gradle_run", side_effect=standin.bad_gradle_run,
    ))
    return stack


class TestRunGradleCommand(unittest.TestCase):
    def test_oserror_names_wrapper_and_is_not_success(self):
        def _raise(*_args, **_kwargs):
            err = OSError("[WinError 193] %1 is not a valid Win32 application")
            raise err

        wrapper = os.path.join("/proj", "gradlew.bat")
        with unittest.mock.patch.object(server, "_gradle_run", side_effect=_raise):
            code, stdout, stderr = server._run_gradle_command("/proj", wrapper, ["--version"])
        self.assertNotEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertIn(wrapper, stderr)
        self.assertIn("WinError 193", stderr)

    def test_oserror_reaches_scan_as_tool_error_text(self):
        def _raise(*_args, **_kwargs):
            raise OSError("[WinError 193] %1 is not a valid Win32 application")

        with temp_project({"build.gradle": "repositories { mavenCentral() }\n"}) as root:
            for name in ("gradlew", "gradlew.bat"):
                with open(os.path.join(root, name), "w", encoding="utf-8") as fh:
                    fh.write(name + "\n")
            with unittest.mock.patch.object(server.os, "name", "nt"):
                with unittest.mock.patch.object(server, "_gradle_run", side_effect=_raise):
                    result = server.scan_project(root)
        text = "; ".join(result.get("gradleErrors") or [])
        self.assertIn(os.path.join(root, "gradlew.bat"), text)
        self.assertNotIn(
            "not a valid Win32 application", text.split("Failed to launch", 1)[0]
        )
        self.assertNotEqual(result.get("resolvedBy"), "gradle")

    def test_timeout_expired_returns_124(self):
        def _raise_timeout(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(cmd="gradlew", timeout=1)

        with unittest.mock.patch.object(server, "_gradle_run", side_effect=_raise_timeout):
            code, stdout, stderr = server._run_gradle_command("/tmp", "gradlew", ["--init-script", "x"])
        self.assertEqual(code, 124)
        self.assertEqual(stdout, "")
        self.assertIn("timed out", stderr)

    def test_secret_env_scrubbed_from_subprocess_call(self):
        # GHSA-4778-r7hp-92v7: the scanned project's OWN Gradle build scripts
        # (buildSrc, convention plugins) run arbitrary code that can read
        # System.getenv() — credential-bearing vars must never be passed to
        # that subprocess, including the now-single init-script invocation.
        captured = {}

        def _capture_env(cmd, cwd=None, capture_output=None, text=None, timeout=None, env=None):
            captured["env"] = env
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with unittest.mock.patch.dict(
            os.environ,
            {
                "MAVEN_REPO_TESTREPO_TOKEN": "supersecret",
                "MAVEN_REPO_TESTREPO_USER": "alice",
                "GITHUB_TOKEN": "ghsecret",
                "JAVA_HOME": "/usr/lib/jvm/test",
            },
            clear=False,
        ):
            with unittest.mock.patch.object(server, "_gradle_run", side_effect=_capture_env):
                server._run_gradle_command("/tmp", "gradlew", ["--init-script", "x", "-q", "help"])
        env = captured["env"]
        self.assertIsNotNone(env)
        self.assertNotIn("MAVEN_REPO_TESTREPO_TOKEN", env)
        self.assertNotIn("MAVEN_REPO_TESTREPO_USER", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        # Non-secret env (JAVA_HOME, PATH, ...) must still reach the subprocess.
        self.assertEqual(env.get("JAVA_HOME"), "/usr/lib/jvm/test")

    def test_proxy_userinfo_redacted_not_dropped(self):
        # R2a follow-up to GHSA-4778-r7hp-92v7: HTTP(S)_PROXY/ALL_PROXY
        # routinely embed user:pass@host (#298) — a scanned build's own code
        # could read them via System.getenv() and exfiltrate the proxy
        # password. The var must NOT be dropped (Gradle needs host:port to
        # route through the proxy at all) — only the userinfo is redacted,
        # via the same _strip_userinfo used for #317 tool-output redaction
        # (produces "***@host", not a bare host — see test_resolution.py's
        # REDACTED_URL contract).
        captured = {}

        def _capture_env(cmd, cwd=None, capture_output=None, text=None, timeout=None, env=None):
            captured["env"] = env
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with unittest.mock.patch.dict(
            os.environ,
            {
                "HTTPS_PROXY": "http://proxyuser:proxypass@proxy.corp.example:8080",
                "http_proxy": "http://otheruser:otherpass@proxy2.corp.example:3128",
                "MAVEN_MCP_REPOSITORY_BASE": "https://repouser:repopass@nexus.corp.example/repo",
            },
            clear=False,
        ):
            with unittest.mock.patch.object(server, "_gradle_run", side_effect=_capture_env):
                server._run_gradle_command("/tmp", "gradlew", ["--init-script", "x", "-q", "help"])
        env = captured["env"]
        self.assertEqual(env["HTTPS_PROXY"], "http://***@proxy.corp.example:8080")
        self.assertEqual(env["http_proxy"], "http://***@proxy2.corp.example:3128")
        self.assertEqual(
            env["MAVEN_MCP_REPOSITORY_BASE"], "https://***@nexus.corp.example/repo"
        )
        # Host:port must survive the redaction — Gradle still needs the address.
        self.assertIn("proxy.corp.example:8080", env["HTTPS_PROXY"])
        self.assertIn("proxy2.corp.example:3128", env["http_proxy"])
        dump = repr(env)
        self.assertNotIn("proxyuser", dump)
        self.assertNotIn("proxypass", dump)
        self.assertNotIn("otheruser", dump)
        self.assertNotIn("otherpass", dump)
        self.assertNotIn("repouser", dump)
        self.assertNotIn("repopass", dump)

    def test_proxy_userinfo_not_visible_to_real_gradlew_subprocess(self):
        # End-to-end companion to the capture-based test above: a real
        # subprocess.run call, no mocking of _gradle_run — proves the
        # redacted (not dropped) value is what the child process actually
        # sees, not merely what is captured at the call-arg boundary.
        with temp_project({}) as root:
            gradlew = os.path.join(root, "gradlew")
            with open(gradlew, "w", encoding="utf-8") as fh:
                fh.write('#!/bin/sh\necho "PROXY=[${HTTPS_PROXY}]"\n')
            os.chmod(gradlew, 0o700)
            with unittest.mock.patch.dict(
                os.environ,
                {"HTTPS_PROXY": "http://proxyuser:proxypass@proxy.corp.example:8080"},
                clear=False,
            ):
                code, stdout, _stderr = server._run_gradle_command(
                    root, gradlew, ["-q", "noop"]
                )
        self.assertEqual(code, 0)
        self.assertNotIn("proxyuser", stdout)
        self.assertNotIn("proxypass", stdout)
        self.assertIn("PROXY=[http://***@proxy.corp.example:8080]", stdout)

    def test_secret_env_not_visible_to_real_gradlew_subprocess(self):
        # End-to-end (real subprocess.run, no mocking of _gradle_run): a fake
        # gradlew script that echoes the env vars back proves they are
        # genuinely absent from the child process, not merely from a mocked
        # call-arg capture.
        with temp_project({}) as root:
            gradlew = os.path.join(root, "gradlew")
            with open(gradlew, "w", encoding="utf-8") as fh:
                fh.write(
                    "#!/bin/sh\n"
                    'echo "TOKEN=[${MAVEN_REPO_TESTREPO_TOKEN}][${GITHUB_TOKEN}]"\n'
                )
            os.chmod(gradlew, 0o700)
            with unittest.mock.patch.dict(
                os.environ,
                {
                    "MAVEN_REPO_TESTREPO_TOKEN": "supersecret",
                    "GITHUB_TOKEN": "ghsecret",
                },
                clear=False,
            ):
                code, stdout, _stderr = server._run_gradle_command(
                    root, gradlew, ["-q", "noop"]
                )
        self.assertEqual(code, 0)
        self.assertNotIn("supersecret", stdout)
        self.assertNotIn("ghsecret", stdout)
        self.assertIn("TOKEN=[][]", stdout)

    def test_windows_timeout_stops_jvm_holding_pipes(self):
        # Models the Windows hang: the JVM is a grandchild that holds both
        # pipe write ends for 30s unless the job it was assigned to before
        # ResumeThread is terminated. _gradle_run is armed as the current
        # cleanup (kill only the direct child, then read with no timeout).
        # A skipUnless(win32) test would pass on the buggy tree; CI is Ubuntu.
        standin = WindowsGradleJobStandIn(fail_bat=True)
        box = {}

        def _call():
            try:
                box["result"] = server._run_gradle_command(
                    "C:\\proj",
                    os.path.join("C:\\proj", "gradlew.bat"),
                    ["--init-script", "x"],
                    timeout=1,
                )
            except Exception as exc:
                box["error"] = exc

        thread = threading.Thread(target=_call, daemon=True)
        try:
            with _windows_job_context(standin):
                thread.start()
                thread.join(_WINDOWS_JOB_JOIN_SECONDS)
                self.assertFalse(
                    thread.is_alive(),
                    "call did not return within 8s while the grandchild held the pipes",
                )
                if "error" in box:
                    raise box["error"]
                self.assertEqual(
                    box["result"],
                    (124, "", "Gradle command timed out after 1s"),
                )
                self.assertIsNotNone(standin.grandchild)
                self.assertIsNotNone(standin.grandchild.poll())
                self.assertFalse(standin.unbounded_after_direct_kill)
                self.assertFalse(standin.gradle_run_called)
                self.assertTrue(standin.assigned_before_resume)
                self.assertTrue(standin.terminate_job_called)
                self.assertFalse(standin.kill_on_job_close)
                self.assertTrue(standin.child_holds_stdout)
                self.assertTrue(standin.child_holds_stderr)
                self.assertGreaterEqual(len(standin.command_lines), 2)
                self.assertIn("/c", standin.command_lines[-1])
                self.assertIn("gradlew.bat", standin.command_lines[-1])
        finally:
            standin.stop()
            if thread.ident is not None:
                thread.join(2)
            standin.close_fds()

    def test_windows_job_success_does_not_kill_on_close(self):
        standin = WindowsGradleJobStandIn(quick_exit=(b"ok\n", b""))
        box = {}

        def _call():
            try:
                box["result"] = server._run_gradle_command(
                    "/proj", "gradlew.bat", ["--version"], timeout=30,
                )
            except Exception as exc:
                box["error"] = exc

        thread = threading.Thread(target=_call, daemon=True)
        try:
            with unittest.mock.patch.dict(
                os.environ,
                {"JAVA_HOME": "/usr/lib/jvm/test", "GITHUB_TOKEN": "ghsecret"},
                clear=False,
            ):
                with _windows_job_context(standin):
                    thread.start()
                    thread.join(_WINDOWS_JOB_JOIN_SECONDS)
                    self.assertFalse(thread.is_alive(), "success path did not return")
                    if "error" in box:
                        raise box["error"]
                    self.assertEqual(box["result"], (0, "ok\n", ""))
                    self.assertFalse(standin.terminate_job_called)
                    self.assertFalse(standin.kill_on_job_close)
                    self.assertFalse(standin.gradle_run_called)
                    self.assertEqual(standin.env.get("JAVA_HOME"), "/usr/lib/jvm/test")
                    self.assertNotIn("GITHUB_TOKEN", standin.env)
                    self.assertIn("--version", standin.command_lines[-1])
                    self.assertIn("gradlew.bat", standin.command_lines[-1])
        finally:
            standin.stop()
            if thread.ident is not None:
                thread.join(2)
            standin.close_fds()

    def test_windows_wait_does_not_pass_infinite(self):
        standin = WindowsGradleJobStandIn(quick_exit=(b"ok\n", b""))
        box = {}

        def _call():
            try:
                box["result"] = server._run_gradle_command(
                    "/proj", "gradlew.bat", ["--version"], timeout=10 ** 7,
                )
            except Exception as exc:
                box["error"] = exc

        thread = threading.Thread(target=_call, daemon=True)
        try:
            with _windows_job_context(standin):
                thread.start()
                thread.join(_WINDOWS_JOB_JOIN_SECONDS)
                self.assertFalse(thread.is_alive(), "huge timeout was treated as infinite")
                if "error" in box:
                    raise box["error"]
                self.assertEqual(box["result"][0], 0)
                self.assertNotEqual(standin.wait_ms, 0xFFFFFFFF)
                self.assertLess(standin.wait_ms, 0xFFFFFFFF)
                self.assertEqual(standin.wait_ms, 0xFFFFFFFE)
        finally:
            standin.stop()
            if thread.ident is not None:
                thread.join(2)
            standin.close_fds()

    def test_windows_large_stdout_is_not_a_timeout(self):
        # Bigger than a pipe buffer. ResumeThread writes it all. Without a
        # reader already running, that write never finishes and the call
        # misses the 8s join.
        payload = b"x" * 200000
        standin = WindowsGradleJobStandIn(quick_exit=(payload, b""))
        box = {}

        def _call():
            try:
                box["result"] = server._run_gradle_command(
                    "/proj", "gradlew.bat", ["--version"], timeout=30,
                )
            except Exception as exc:
                box["error"] = exc

        thread = threading.Thread(target=_call, daemon=True)
        try:
            with _windows_job_context(standin):
                thread.start()
                thread.join(_WINDOWS_JOB_JOIN_SECONDS)
                self.assertFalse(thread.is_alive(), "large stdout was treated as a hang")
                if "error" in box:
                    raise box["error"]
                self.assertEqual(box["result"], (0, "x" * 200000, ""))
                self.assertFalse(standin.terminate_job_called)
        finally:
            standin.stop()
            if thread.ident is not None:
                thread.join(2)
            standin.close_fds()

    def test_windows_bat_retry_wraps_the_comspec_payload(self):
        standin = WindowsGradleJobStandIn(quick_exit=(b"ok\n", b""), fail_bat=True)
        box = {}
        gradlew = "C:\\Users\\John Smith\\gradlew.bat"

        def _call():
            try:
                box["result"] = server._run_gradle_command(
                    "C:\\Users\\John Smith",
                    gradlew,
                    ["--init-script", "C:\\a dir\\init.gradle"],
                    timeout=30,
                )
            except Exception as exc:
                box["error"] = exc

        thread = threading.Thread(target=_call, daemon=True)
        try:
            with _windows_job_context(standin):
                thread.start()
                thread.join(_WINDOWS_JOB_JOIN_SECONDS)
                self.assertFalse(thread.is_alive(), "bat retry did not return")
                if "error" in box:
                    raise box["error"]
                self.assertEqual(box["result"][0], 0)
                line = standin.command_lines[-1]
                payload = line.split(" /c ", 1)[1]
                self.assertTrue(payload.startswith('"') and payload.endswith('"'), line)
                inner = payload[1:-1]
                self.assertIn('"C:\\Users\\John Smith\\gradlew.bat"', inner)
                self.assertIn('"C:\\a dir\\init.gradle"', inner)
        finally:
            standin.stop()
            if thread.ident is not None:
                thread.join(2)
            standin.close_fds()


class TestMergeGradleWithProvenance(unittest.TestCase):
    def test_provenance_only_plugin_included(self):
        resolved = [{
            "groupId": "io.ktor",
            "artifactId": "ktor-client-core",
            "version": "3.1.2",
            "usages": [{"module": ":app", "configuration": "releaseRuntimeClasspath"}],
        }]
        provenance = [
            {
                "groupId": "io.ktor",
                "artifactId": "ktor-client-core",
                "version": "3.1.1",
                "source": {"kind": "catalog-library", "alias": "ktor-core"},
                "usages": [{"module": ":app", "configuration": "implementation"}],
            },
            {
                "groupId": "org.jetbrains.kotlin.android",
                "artifactId": "org.jetbrains.kotlin.android.gradle.plugin",
                "version": "2.0.0",
                "source": {"kind": "plugins-dsl", "file": "build.gradle.kts"},
                "usages": [{"module": None, "configuration": "plugin-dsl"}],
            },
        ]
        merged = server._merge_gradle_with_provenance(resolved, provenance)
        plugin = next(
            d for d in merged
            if d["artifactId"] == "org.jetbrains.kotlin.android.gradle.plugin"
        )
        self.assertEqual(plugin["resolvedBy"], "provenance")
        self.assertEqual(plugin["source"]["kind"], "plugins-dsl")

    def test_unused_catalog_entry_preserved_when_ga_resolved(self):
        resolved = [{
            "groupId": "com.example",
            "artifactId": "lib",
            "version": "2.0",
            "usages": [{"module": ":app", "configuration": "releaseRuntimeClasspath"}],
        }]
        provenance = [
            {
                "groupId": "com.example",
                "artifactId": "lib",
                "version": "1.0",
                "source": {
                    "kind": "catalog-library",
                    "alias": "unused-lib",
                    "tomlPath": "gradle/libs.versions.toml",
                },
                "usages": [],
            },
        ]
        merged = server._merge_gradle_with_provenance(resolved, provenance)
        self.assertEqual(len(merged), 2)
        unused = next(
            d for d in merged
            if d.get("source", {}).get("alias") == "unused-lib"
        )
        self.assertEqual(unused["resolvedBy"], "provenance")
        self.assertEqual(unused["version"], "1.0")
        gradle_entry = next(d for d in merged if d.get("resolvedBy") == "gradle")
        self.assertEqual(gradle_entry["source"]["kind"], "gradle-resolved")


class TestGradleConflictDetection(unittest.TestCase):
    def test_detect_conflicts_from_gradle_scan_usages(self):
        scan = {
            "buildSystem": "gradle",
            "resolvedBy": "gradle",
            "dependencies": [
                {
                    "groupId": "com.example",
                    "artifactId": "lib",
                    "version": "2.0",
                    "usages": [{"module": ":app", "configuration": "releaseRuntimeClasspath"}],
                },
                {
                    "groupId": "com.example",
                    "artifactId": "lib",
                    "version": "1.0",
                    "usages": [{"module": ":core", "configuration": "releaseRuntimeClasspath"}],
                },
            ],
        }
        out = server._detect_conflicts_from_gradle_scan(scan)
        self.assertEqual(len(out["conflicts"]), 1)
        conflict = out["conflicts"][0]
        self.assertEqual(conflict["resolvedTo"], "2.0")
        self.assertEqual(conflict["strategy"], "highest-wins")
        self.assertTrue(any("Gradle-resolved" in n for n in out["notes"]))


class TestGradleResolveIntegration(unittest.TestCase):
    """scan_project against a mocked single-invocation `_run_gradle_command`."""

    def _mock_run(self, stdout, code=0, stderr=""):
        def _fake_run(project_root, gradlew, args, timeout=server.GRADLE_RESOLVE_TIMEOUT):
            return code, stdout, stderr

        return unittest.mock.patch.object(server, "_run_gradle_command", _fake_run)

    def test_scan_project_merges_gradle_with_provenance(self):
        files = {
            "settings.gradle.kts": 'include(":app")',
            "gradle/libs.versions.toml": (
                "[libraries]\n"
                'ktor-core = { module = "io.ktor:ktor-client-core", version = "3.1.1" }\n'
            ),
            "app/build.gradle.kts": (
                "dependencies {\n"
                "    implementation(libs.ktor.core)\n"
                "}"
            ),
        }
        with temp_project(files) as root:
            write_fake_gradlew(root)
            with self._mock_run(SINGLE_INVOCATION_FIXTURE):
                result = server.scan_project(root)
        dep = next(
            d for d in result["dependencies"]
            if d["groupId"] == "io.ktor" and d["artifactId"] == "ktor-client-core"
            and d.get("resolvedBy") == "gradle"
        )
        self.assertEqual(dep["version"], "3.1.2")
        self.assertEqual(dep["source"]["kind"], "catalog-library")
        self.assertEqual(dep["source"]["alias"], "ktor-core")
        self.assertEqual(result["resolvedBy"], "gradle")

    def test_missing_gradlew_raises_clear_error(self):
        files = {"build.gradle.kts": 'plugins { id("java") }'}
        with temp_project(files) as root:
            with self.assertRaises(ValueError) as ctx:
                server.scan_project(root)
        self.assertIn("Gradle wrapper", str(ctx.exception))

    def test_resolution_errors_surface_without_raising(self):
        files = {"build.gradle.kts": 'plugins { id("java") }'}
        with temp_project(files) as root:
            write_fake_gradlew(root)
            with mock_gradle_resolve(errors=["Gradle daemon unavailable"], dependencies=[]):
                result = server.scan_project(root)
        self.assertNotEqual(result.get("resolvedBy"), "gradle")
        self.assertIn("Gradle daemon unavailable", result.get("gradleErrors") or [])

    def test_only_classpath_deps_keeps_provenance(self):
        files = {"build.gradle.kts": 'plugins { id("java") }'}
        with temp_project(files) as root:
            write_fake_gradlew(root)
            with self._mock_run(ONLY_BUILDENV_FIXTURE):
                result = server.scan_project(root)
        self.assertNotEqual(result.get("resolvedBy"), "gradle")
        text = " ".join(result.get("gradleErrors") or [])
        self.assertIn("No production runtime dependencies resolved", text)
        self.assertTrue(any(
            d["groupId"] == "com.android.tools.build" for d in result["dependencies"]
        ))

    def test_all_silent_task_failures_return_scan(self):
        files = {"build.gradle.kts": 'plugins { id("java") }'}
        with temp_project(files) as root:
            write_fake_gradlew(root)
            with unittest.mock.patch.object(
                server, "_run_gradle_command", return_value=(1, "", "")
            ):
                result = server.scan_project(root)
        self.assertNotEqual(result.get("resolvedBy"), "gradle")
        text = " ".join(result.get("gradleErrors") or []) + " " + " ".join(result.get("notes") or [])
        self.assertIn("All Gradle dependency tasks failed.", text)

    def test_failed_resolve_audit_looks_up_catalog_rows(self):
        files = {
            "settings.gradle.kts": 'rootProject.name = "app"\n',
            "gradle/libs.versions.toml": (
                "[versions]\n"
                'coreKtx = "1.16.0"\n'
                'agp = "9.3.2"\n'
                "\n"
                "[libraries]\n"
                'androidx-core-ktx = { module = "androidx.core:core-ktx", version.ref = "coreKtx" }\n'
                "\n"
                "[plugins]\n"
                'android-application = { id = "com.android.application", version.ref = "agp" }\n'
            ),
        }
        with temp_project(files) as root:
            path = os.path.join(root, "gradlew")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\nexit 1\n")
            os.chmod(path, 0o700)

            def _raise_metadata(group_id, artifact_id, _ctx):
                raise ValueError(
                    f"Could not fetch metadata for {group_id}:{artifact_id}: HTTP 404"
                )

            with unittest.mock.patch.object(
                server, "fetch_metadata", side_effect=_raise_metadata
            ) as fetch:
                with unittest.mock.patch.object(server, "apply_bom_managed_versions") as bom:
                    result = server.handle_audit_project_dependencies({
                        "projectPath": root,
                        "includeVulnerabilities": False,
                    })
        self.assertFalse(bom.called)
        self.assertGreaterEqual(fetch.call_count, 2)
        coords = {
            (d["groupId"], d["artifactId"]) for d in result["dependencies"]
        }
        self.assertIn(("androidx.core", "core-ktx"), coords)
        self.assertIn(
            ("com.android.application", "com.android.application.gradle.plugin"),
            coords,
        )
        for dep in result["dependencies"]:
            if dep.get("currentVersion"):
                self.assertTrue(
                    dep["error"].startswith("Could not fetch metadata for"),
                    dep,
                )
        self.assertTrue(result.get("gradleErrors"))
        notes = " ".join(result.get("notes") or [])
        self.assertIn("All Gradle dependency tasks failed.", notes)

    def test_failed_resolve_conflict_scan_does_not_fallback(self):
        files = {"settings.gradle.kts": 'rootProject.name = "app"\n'}
        with temp_project(files) as root:
            path = os.path.join(root, "gradlew")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\nexit 1\n")
            os.chmod(path, 0o700)
            with unittest.mock.patch.object(server, "apply_bom_managed_versions") as bom:
                with unittest.mock.patch.object(
                    server, "fetch_depsdev_dependencies"
                ) as depsdev:
                    with unittest.mock.patch.object(
                        server, "resolve_pom_graph"
                    ) as pom_graph:
                        result = server.detect_dependency_conflicts(root)
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(result["graphsFetched"], 0)
        self.assertFalse(bom.called)
        self.assertFalse(depsdev.called)
        self.assertFalse(pom_graph.called)
        text = " ".join(result.get("errors") or []) + " " + " ".join(result.get("notes") or [])
        self.assertTrue(
            "exited with code 1" in text or "All Gradle dependency tasks failed." in text
        )

    def test_failed_resolve_keeps_deadline_note_with_resolution_notes(self):
        files = {
            "settings.gradle.kts": 'rootProject.name = "app"\n',
            "gradle/libs.versions.toml": (
                "[libraries]\n"
                'core = { module = "androidx.core:core-ktx", version = "1.16.0" }\n'
                'nav = { module = "androidx.navigation3:navigation3-ui", version = "1.0.0" }\n'
            ),
        }
        seen = {"n": 0}

        def _now():
            seen["n"] += 1
            if seen["n"] == 1:
                return 0.0
            return float(server.TOOL_DEADLINE) + 1.0

        with temp_project(files) as root:
            path = os.path.join(root, "gradlew")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\nexit 1\n")
            os.chmod(path, 0o700)
            with unittest.mock.patch.object(server, "_now", side_effect=_now):
                result = server.handle_audit_project_dependencies({
                    "projectPath": root,
                    "includeVulnerabilities": False,
                })
        self.assertTrue(result.get("partial"))
        notes = result.get("notes") or []
        self.assertIn("All Gradle dependency tasks failed.", notes)
        self.assertTrue(any(note.startswith("Stopped early after exceeding the ") for note in notes))
        self.assertTrue(result.get("gradleErrors"))

    def test_partial_failure_surfaces_gradle_errors(self):
        # Per-module, per-configuration failure isolation (#401): one
        # configuration's resolution error must not blank out a sibling
        # configuration's successfully-resolved dependencies in the same
        # module, and must still surface in gradleErrors.
        files = {
            "settings.gradle.kts": 'include(":app")',
            "app/build.gradle.kts": (
                "dependencies {\n"
                '    implementation("io.ktor:ktor-client-core:3.1.1")\n'
                "}"
            ),
        }
        with temp_project(files) as root:
            write_fake_gradlew(root)
            with self._mock_run(CONFIG_ERROR_FIXTURE):
                result = server.scan_project(root)
        self.assertTrue(any("debugRuntimeClasspath" in e for e in result.get("gradleErrors", [])))
        dep = next(
            d for d in result["dependencies"]
            if d["groupId"] == "io.ktor" and d["artifactId"] == "ktor-client-core"
        )
        self.assertEqual(dep["version"], "3.1.2")
        flat = server.flatten_scan_result(result)
        self.assertIn("gradleErrors", flat)


class TestGradleResolveSubprocessE2E(unittest.TestCase):
    """End-to-end: real subprocess to smart gradlew stub (no JVM)."""

    def test_scan_project_via_subprocess_stub(self):
        files = {
            "settings.gradle.kts": 'include(":app")',
            "gradle/libs.versions.toml": (
                "[libraries]\n"
                'ktor-core = { module = "io.ktor:ktor-client-core", version = "3.1.1" }\n'
            ),
            "app/build.gradle.kts": (
                "dependencies {\n"
                "    implementation(libs.ktor.core)\n"
                "}"
            ),
        }
        with temp_project(files) as root:
            write_smart_gradlew(root)
            result = server.scan_project(root)
        dep = next(
            d for d in result["dependencies"]
            if d["groupId"] == "io.ktor"
            and d["artifactId"] == "ktor-client-core"
            and d["version"] == "3.1.2"
        )
        self.assertEqual(dep["version"], "3.1.2")
        self.assertEqual(dep["source"]["kind"], "catalog-library")
        self.assertEqual(result["resolvedBy"], "gradle")


class TestPickBestProvenance(unittest.TestCase):
    def test_catalog_wins_over_module_direct(self):
        resolved = [{
            "groupId": "io.ktor",
            "artifactId": "ktor-client-core",
            "version": "3.1.2",
            "usages": [{"module": ":app", "configuration": "releaseRuntimeClasspath"}],
        }]
        provenance = [
            {
                "groupId": "io.ktor",
                "artifactId": "ktor-client-core",
                "version": "3.1.1",
                "source": {"kind": "module-direct", "file": "app/build.gradle.kts"},
                "usages": [],
            },
            {
                "groupId": "io.ktor",
                "artifactId": "ktor-client-core",
                "version": "3.1.1",
                "source": {"kind": "catalog-library", "alias": "ktor-core"},
                "usages": [{"module": ":app", "configuration": "implementation"}],
            },
        ]
        merged = server._merge_gradle_with_provenance(resolved, provenance)
        gradle_dep = next(d for d in merged if d.get("resolvedBy") == "gradle")
        self.assertEqual(gradle_dep["source"]["kind"], "catalog-library")


class TestFlattenScanResultResolvedBy(unittest.TestCase):
    def test_top_level_and_entry_resolved_by(self):
        scan = {
            "buildSystem": "gradle",
            "resolvedBy": "gradle",
            "dependencies": [{
                "groupId": "com.example",
                "artifactId": "lib",
                "version": "1.0",
                "resolvedBy": "gradle",
                "source": {"kind": "gradle-resolved"},
                "usages": [{"module": None, "configuration": "releaseRuntimeClasspath"}],
            }],
            "deadRepositoryHints": [],
        }
        flat = server.flatten_scan_result(scan)
        self.assertEqual(flat["resolvedBy"], "gradle")
        self.assertEqual(flat["dependencies"][0]["resolvedBy"], "gradle")


if __name__ == "__main__":
    unittest.main()
