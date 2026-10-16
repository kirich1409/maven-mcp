"""TOOL_DEADLINE on the three POM-graph tools.

The walk used to fetch every POM up to the node cap. These tests patch
``locate_pom`` and ``_now`` (or a short real deadline) so a missing bound
fails without waiting on a live repository timeout.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from _helpers import empty_ctx, server


def _pom(artifact, dep=None):
    body = ""
    if dep:
        body = (
            "<dependencies><dependency>"
            f"<groupId>com.example</groupId><artifactId>{dep}</artifactId>"
            "<version>1.0.0</version></dependency></dependencies>"
        )
    return (
        "<?xml version='1.0'?><project>"
        f"<groupId>com.example</groupId><artifactId>{artifact}</artifactId>"
        f"<version>1.0.0</version>{body}</project>"
    )


def _chain_pom(artifact):
    if artifact.startswith("lib") and artifact[3:].isdigit():
        index = int(artifact[3:])
        if index < 5:
            return _pom(artifact, f"lib{index + 1}")
    return _pom(artifact)


def _notes_text(result):
    return " ".join(result.get("notes") or [])


class PomGraphDeadlineTest(unittest.TestCase):
    def test_clock_jump_stops_transitive_graph_before_the_chain(self):
        clock = {"t": 1000000.0}
        calls = []

        def now():
            return clock["t"]

        def locate(group_id, artifact_id, version, ctx, deadline_monotonic=None):
            clock["t"] += 40
            calls.append(artifact_id)
            return _chain_pom(artifact_id)

        with unittest.mock.patch.object(server, "_now", now), \
                unittest.mock.patch.object(server, "locate_pom", locate):
            out = server.get_transitive_graph(
                "com.example", "lib0", "1.0.0", empty_ctx(),
            )
        self.assertTrue(out["partial"])
        self.assertFalse(out["truncated"])
        self.assertIn("deadline", _notes_text(out).lower())
        self.assertLess(len(out["nodes"]), 6)
        self.assertLess(len(calls), 6)
        self.assertNotIn("error", out)

    def test_clock_jump_does_not_start_osv(self):
        clock = {"t": 1000000.0}

        def now():
            return clock["t"]

        def locate(group_id, artifact_id, version, ctx, deadline_monotonic=None):
            clock["t"] += 40
            return _chain_pom(artifact_id)

        with unittest.mock.patch.object(server, "_now", now), \
                unittest.mock.patch.object(server, "locate_pom", locate), \
                unittest.mock.patch.object(server, "query_osv_batch") as osv, \
                unittest.mock.patch.object(server, "_hydrate_osv_vulns") as hydrate:
            out = server.get_vulnerability_paths(
                "com.example", "lib0", "1.0.0", empty_ctx(),
            )
        osv.assert_not_called()
        hydrate.assert_not_called()
        self.assertEqual(out["vulnerabilityPaths"], [])
        self.assertTrue(out["partial"])
        self.assertFalse(out["truncated"])
        self.assertIn("deadline", _notes_text(out).lower())

    def test_osv_receives_the_walk_deadline_when_time_remains(self):
        clock = {"t": 1000000.0}

        def now():
            return clock["t"]

        def locate(group_id, artifact_id, version, ctx, deadline_monotonic=None):
            return _chain_pom(artifact_id)

        expected = 1000000.0 + server.TOOL_DEADLINE
        with unittest.mock.patch.object(server, "_now", now), \
                unittest.mock.patch.object(server, "locate_pom", locate), \
                unittest.mock.patch.object(server, "query_osv_batch", return_value=[]) as osv:
            out = server.get_vulnerability_paths(
                "com.example", "lib0", "1.0.0", empty_ctx(),
            )
        osv.assert_called_once()
        self.assertEqual(osv.call_args.kwargs.get("deadline"), expected)
        self.assertEqual(len(out["vulnerabilityPaths"]), 0)
        self.assertFalse(out["partial"])
        self.assertNotIn("deadline", _notes_text(out).lower())

    def test_past_deadline_does_not_call_locate_or_report_absence(self):
        calls = {"n": 0}
        located = []

        def now():
            calls["n"] += 1
            if calls["n"] == 1:
                return 1000000.0
            return 1000040.0

        def locate(group_id, artifact_id, version, ctx, deadline_monotonic=None):
            located.append(artifact_id)
            return None

        with unittest.mock.patch.object(server, "_now", now), \
                unittest.mock.patch.object(server, "locate_pom", locate):
            out = server.get_transitive_graph(
                "com.example", "lib0", "1.0.0", empty_ctx(),
            )
        self.assertEqual(located, [])
        self.assertEqual(out["nodes"], [])
        self.assertEqual(out["edges"], [])
        self.assertTrue(out["partial"])
        self.assertFalse(out["truncated"])
        self.assertIn("deadline", _notes_text(out).lower())
        err = out.get("error") or ""
        self.assertNotIn("POM not found", err)
        self.assertNotIn("POM graph unavailable", err)
        self.assertNotIn("capabilityUnavailable", out)

    def test_http_attempt_is_capped_to_the_remaining_deadline(self):
        timeouts = []
        body = _pom("lib0").encode()

        def urlopen(req, timeout=None, context=None):
            timeouts.append(timeout)
            return _Body(body)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        (home / ".gradle").mkdir()
        (home / ".m2" / "repository").mkdir(parents=True)
        project = home / "project"
        project.mkdir()
        ctx = server.build_resolution_context({"projectPath": str(project)})
        with unittest.mock.patch.object(server, "TOOL_DEADLINE", 0.05), \
                unittest.mock.patch.object(server.Path, "home", return_value=home), \
                unittest.mock.patch.dict(
                    os.environ, {"GRADLE_USER_HOME": str(home / ".gradle")},
                ), \
                unittest.mock.patch("urllib.request.urlopen", urlopen):
            server.get_transitive_graph(
                "com.example", "lib0", "1.0.0", ctx,
            )
        self.assertTrue(timeouts, "urlopen was not called")
        self.assertLessEqual(timeouts[0], 0.05)
        self.assertLess(timeouts[0], server.HTTP_TIMEOUT)
        self.assertNotEqual(timeouts[0], 15)

    def test_exhausted_monotonic_deadline_skips_urlopen(self):
        with unittest.mock.patch("urllib.request.urlopen") as urlopen:
            found = server.fetch_pom(
                "com.example", "lib0", "1.0.0", empty_ctx(),
                deadline_monotonic=time.monotonic() - 1,
            )
        self.assertIsNone(found)
        urlopen.assert_not_called()

    def test_maven_conflicts_do_not_wait_past_the_deadline(self):
        started = threading.Event()
        release = threading.Event()
        calls = []
        lock = threading.Lock()
        seen = {}

        def locate(group_id, artifact_id, version, ctx, deadline_monotonic=None):
            with lock:
                calls.append(artifact_id)
            if artifact_id == "blocked":
                started.set()
                release.wait(2)
                return _pom("blocked", "next")
            return _pom(artifact_id)

        real_map = server._map_parallel

        def wrapped(items, fn, max_workers=server.MAX_PARALLEL_FETCHES, deadline=None):
            seen["deadline"] = deadline
            return real_map(items, fn, max_workers=max_workers, deadline=deadline)

        scan = {
            "resolvedBy": "maven",
            "buildSystem": "maven",
            "dependencies": [
                {"groupId": "com.example", "artifactId": "left", "version": "1.0.0"},
                {"groupId": "com.example", "artifactId": "blocked", "version": "1.0.0"},
            ],
        }
        box = {}

        def run():
            try:
                box["t0"] = time.monotonic()
                box["out"] = server.detect_dependency_conflicts(
                    "/__no_project__", ctx=empty_ctx(),
                )
                box["elapsed"] = time.monotonic() - box["t0"]
            except Exception as exc:
                box["error"] = exc

        worker = threading.Thread(target=run)
        try:
            with unittest.mock.patch.object(server, "TOOL_DEADLINE", 0.05), \
                    unittest.mock.patch.object(server, "scan_project", return_value=scan), \
                    unittest.mock.patch.object(
                        server, "apply_bom_managed_versions",
                        side_effect=lambda found, _ctx: found,
                    ), \
                    unittest.mock.patch.object(server, "locate_pom", locate), \
                    unittest.mock.patch.object(server, "_map_parallel", wrapped):
                worker.start()
                self.assertTrue(started.wait(1), "blocked locate_pom did not start")
                worker.join(0.5)
                self.assertFalse(
                    worker.is_alive(),
                    "conflict scan was still waiting after the deadline",
                )
                if box.get("error"):
                    raise box["error"]
                out = box["out"]
                self.assertLess(box["elapsed"], 0.5)
                self.assertIsNotNone(seen.get("deadline"))
                self.assertTrue(out["partial"])
                self.assertIn("deadline", _notes_text(out).lower())
                self.assertLess(out["graphsFetched"], 2)
                self.assertEqual(out["graphsFailed"], 0)
                joined = " ".join(out.get("errors") or [])
                self.assertNotIn("unavailable", joined)
                self.assertNotIn("blocked", joined)
                with lock:
                    before = list(calls)
                release.set()
                time.sleep(0.2)
                with lock:
                    self.assertEqual(calls, before)
        finally:
            release.set()
            if worker.ident is not None:
                worker.join(2)

    def test_stdio_reads_ping_by_the_deadline_bound(self):
        # A 6-POM chain whose each miss sleeps the urlopen timeout, clamped
        # to 0.25s so a buggy 15s timeout cannot stall the suite. The full
        # chain is 1.5s. The ping must arrive by about TOOL_DEADLINE (0.2)
        # plus scheduling margin, not after that chain.
        bound = 1.0
        server_dir = os.path.dirname(os.path.abspath(server.__file__))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        project = root / "project"
        project.mkdir()
        home = root / "home"
        (home / ".gradle").mkdir(parents=True)
        script = root / "stdio_child.py"
        script.write_text(_STDIO_CHILD, encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = server_dir
        env["MAVEN_MCP_CACHE_DISABLE"] = "1"
        env["MAVEN_MCP_SETTINGS"] = "/__maven_mcp_test_no_settings__"
        env["MAVEN_MCP_TRANSPORT"] = "stdio"
        env.pop("MAVEN_MCP_OFFLINE", None)
        env.pop("MAVEN_MCP_REPOSITORY_BASE", None)
        env["HOME"] = str(home)
        env["GRADLE_USER_HOME"] = str(home / ".gradle")
        for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        ):
            env.pop(key, None)
        proc = subprocess.Popen(
            [sys.executable, "-u", str(script), server_dir],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(root),
        )
        killer = threading.Timer(8, proc.kill)
        killer.daemon = True
        killer.start()
        try:
            ready = _read_line(proc.stderr, 10)
            self.assertEqual(ready, b"READY\n")
            payload = (
                json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "get_transitive_graph",
                        "arguments": {
                            "groupId": "com.example",
                            "artifactId": "lib0",
                            "version": "1.0.0",
                            "projectPath": str(project),
                        },
                    },
                })
                + "\n"
                + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"})
                + "\n"
            ).encode()
            t0 = time.monotonic()
            proc.stdin.write(payload)
            proc.stdin.flush()
            lines = []
            while len(lines) < 2 and time.monotonic() - t0 < bound:
                remaining = bound - (time.monotonic() - t0)
                line = _read_line(proc.stdout, remaining)
                if not line:
                    break
                lines.append(line)
            elapsed = time.monotonic() - t0
            self.assertEqual(
                len(lines), 2,
                "ping was not written within %.3fs of handler start "
                "(elapsed %.3fs); log=%s" % (
                    bound, elapsed,
                    (root / "stdio-child.log").read_text(encoding="utf-8")
                    if (root / "stdio-child.log").exists() else "<no log>",
                ),
            )
            self.assertLess(elapsed, bound)
            graph_msg = json.loads(lines[0])
            ping_msg = json.loads(lines[1])
            self.assertEqual(graph_msg["id"], 1)
            self.assertNotIn("error", graph_msg)
            self.assertNotIn("isError", graph_msg["result"])
            graph = json.loads(graph_msg["result"]["content"][0]["text"])
            self.assertTrue(graph["partial"])
            self.assertIn("deadline", _notes_text(graph).lower())
            self.assertLess(len(graph["nodes"]), 6)
            self.assertEqual(ping_msg["id"], 2)
            self.assertNotIn("error", ping_msg)
        finally:
            killer.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()


class _Body:
    def __init__(self, body):
        self.status = 200
        self._body = body
        self.headers = {}

    def read(self, n=-1):
        if n is None or n < 0 or n >= len(self._body):
            chunk = self._body
            self._body = b""
            return chunk
        chunk = self._body[:n]
        self._body = self._body[n:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _read_line(stream, timeout):
    box = {}

    def target():
        try:
            box["line"] = stream.readline()
        except Exception as exc:
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return None
    if "error" in box:
        raise box["error"]
    return box.get("line")


_STDIO_CHILD = r'''
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, sys.argv[1])
import server

server.TOOL_DEADLINE = 0.2


class _Body:
    def __init__(self, body):
        self.status = 200
        self._body = body
        self.headers = {}

    def read(self, n=-1):
        if n is None or n < 0 or n >= len(self._body):
            chunk = self._body
            self._body = b""
            return chunk
        chunk = self._body[:n]
        self._body = self._body[n:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _artifact(url):
    name = url.rstrip("/").split("/")[-1]
    if name.endswith(".pom"):
        name = name[:-4]
    if "-" in name:
        return name.rsplit("-", 1)[0]
    return name


def _pom(artifact):
    dep = ""
    if artifact.startswith("lib") and artifact[3:].isdigit():
        index = int(artifact[3:])
        if index < 5:
            nxt = "lib%d" % (index + 1)
            dep = (
                "<dependencies><dependency>"
                "<groupId>com.example</groupId><artifactId>%s</artifactId>"
                "<version>1.0.0</version></dependency></dependencies>"
            ) % nxt
    xml = (
        "<?xml version='1.0'?><project>"
        "<groupId>com.example</groupId><artifactId>%s</artifactId>"
        "<version>1.0.0</version>%s</project>"
    ) % (artifact, dep)
    return xml.encode()


def urlopen(req, timeout=None, context=None):
    delay = 0.25 if timeout is None else min(float(timeout), 0.25)
    url = getattr(req, "full_url", None) or str(req)
    with open("stdio-child.log", "a", encoding="utf-8") as log:
        log.write("urlopen timeout=%r delay=%r url=%s\n" % (timeout, delay, url))
    time.sleep(delay)
    return _Body(_pom(_artifact(url)))


urllib.request.urlopen = urlopen
sys.stderr.write("READY\n")
sys.stderr.flush()
server.main()
'''


if __name__ == "__main__":
    unittest.main()
