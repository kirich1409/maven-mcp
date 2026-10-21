"""Process regression for audit against a dead first declared repository.

Real sockets, real ``handle_audit_project_dependencies`` — urlopen is not
patched. A silent first repository must not spend the whole tool budget:

* two or more versioned dependencies still reach the next declared
  repository inside ``TOOL_DEADLINE``, with ``1.2.0`` in ``candidates``;
* a row left unfinished names the repository it was waiting on, and says
  later repositories were not queried only when a later one was not
  contacted (not merely because two repositories are in scope);
* OSV querybatch and hydration do not start once the deadline has fired;
* a timeout or an HTTP 5xx is remembered in memory (not in ``FileCache``,
  which stays 200/404) so the next audit does not wait again. HTTP 429 is
  not remembered.

``_map_parallel`` still ignores the deadline for a single dependency. The
per-repository slice inside ``fetch_metadata`` is what lets that one call
reach the next repository before two full ``HTTP_TIMEOUT`` attempts elapse.
"""

import json
import os
import threading
import time
import unittest
import unittest.mock
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from _helpers import server, temp_cache_dir, temp_project


# Production is 15 < 30 < 15+backoff+15. Same shape, short enough for the suite.
_HANG_HTTP_TIMEOUT = 0.6
_HANG_BACKOFF_BASE = 0.02
_HANG_TOOL_DEADLINE = 1.0


class _Log:
    def __init__(self):
        self._lock = threading.Lock()
        self._events = []

    def add(self, **item):
        item["t"] = time.monotonic()
        with self._lock:
            self._events.append(item)

    def snapshot(self):
        with self._lock:
            return list(self._events)


def _metadata_xml(versions=("1.0.0", "1.2.0")):
    vers = "".join(f"<version>{version}</version>" for version in versions)
    xml = (
        "<metadata><versioning>"
        "<lastUpdated>20240101000000</lastUpdated>"
        f"<versions>{vers}</versions>"
        "</versioning></metadata>"
    )
    return xml.encode("utf-8")


def _pom(repos, artifacts):
    repo_xml = "".join(
        f"<repository><id>{repo_id}</id><url>{url}</url></repository>"
        for repo_id, url in repos
    )
    dep_xml = "".join(
        "<dependency>"
        f"<groupId>com.example</groupId><artifactId>{artifact}</artifactId>"
        "<version>1.0.0</version>"
        "</dependency>"
        for artifact in artifacts
    )
    return (
        "<project>"
        f"<repositories>{repo_xml}</repositories>"
        f"<dependencies>{dep_xml}</dependencies>"
        "</project>"
    )


def _base(httpd):
    host, port = httpd.server_address
    return f"http://{host}:{port}"


def _send(handler, status, body):
    handler.send_response(status)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    if body:
        handler.wfile.write(body)


def _hang_handler(log, release):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler naming)
            log.add(method="GET", path=self.path)
            release.wait()

        def log_message(self, *_args, **_kwargs):
            return

    return Handler


def _metadata_handler(log, body, status=200):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler naming)
            log.add(method="GET", path=self.path, status=status)
            # No Retry-After: a 5xx/429 must take the short backoff, not a stall.
            _send(self, status, body)

        def log_message(self, *_args, **_kwargs):
            return

    return Handler


def _osv_handler(log):
    """Local OSV. querybatch carries ids only; severity and fixed live on GET."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 (BaseHTTPRequestHandler naming)
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
            results = []
            for query in payload.get("queries") or []:
                name = ((query.get("package") or {}).get("name") or "")
                artifact = name.split(":")[-1]
                vuln_id = {"one": "CVE-REPRO-1", "two": "CVE-REPRO-2"}.get(artifact)
                vulns = []
                if vuln_id:
                    vulns.append({"id": vuln_id, "modified": "2024-01-01T00:00:00Z"})
                results.append({"vulns": vulns})
            body = json.dumps({"results": results}).encode("utf-8")
            log.add(method="POST", path=self.path, body=body)
            _send(self, 200, body)

        def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler naming)
            vuln_id = self.path.rsplit("/", 1)[-1]
            body = json.dumps({
                "id": vuln_id,
                "database_specific": {"severity": "HIGH"},
                "affected": [{
                    "ranges": [{
                        "type": "ECOSYSTEM",
                        "events": [{"introduced": "0"}, {"fixed": "9.9.9"}],
                    }],
                }],
            }).encode("utf-8")
            log.add(method="GET", path=self.path, body=body)
            _send(self, 200, body)

        def log_message(self, *_args, **_kwargs):
            return

    return Handler


def _cache_entries(xdg):
    root = os.path.join(xdg, "maven-central-mcp")
    if not os.path.isdir(root):
        return []
    found = []
    for name in sorted(os.listdir(root)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(root, name), encoding="utf-8") as fh:
            found.append(json.load(fh))
    return found


def _path_counts(log):
    return Counter(event["path"] for event in log.snapshot())


class AuditDeclaredRepoDeadlineTest(unittest.TestCase):
    def setUp(self):
        with server._repo_failure_lock:
            server._repo_failure_cache.clear()
        with server._repo_probe_lock:
            server._repo_probe_labels.clear()

    def tearDown(self):
        self.setUp()

    def _start(self, handler, release=None):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        httpd.daemon_threads = True
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        # LIFO: release (if any), then shutdown, then close, then join.
        self.addCleanup(thread.join, 2)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        if release is not None:
            self.addCleanup(release.set)
        return httpd

    def _audit(self, root):
        # includeVulnerabilities omitted on purpose: the default is true.
        return server.handle_audit_project_dependencies({"projectPath": root})

    def _env(self, osv):
        return {
            "MAVEN_MCP_OSV_BASE": _base(osv),
            "MAVEN_MCP_PUBLIC_FALLBACK": "",
            "MAVEN_MCP_OFFLINE": "",
            "MAVEN_MCP_REPOSITORY_BASE": "",
        }

    def _hang_patches(self):
        return (
            unittest.mock.patch.object(server, "HTTP_TIMEOUT", _HANG_HTTP_TIMEOUT),
            unittest.mock.patch.object(server, "HTTP_BACKOFF_BASE", _HANG_BACKOFF_BASE),
            unittest.mock.patch.object(server, "HTTP_BACKOFF_JITTER", 0.0),
            unittest.mock.patch.object(server, "TOOL_DEADLINE", _HANG_TOOL_DEADLINE),
        )

    def test_default_deadline_is_30s(self):
        self.assertEqual(server.TOOL_DEADLINE, 30.0)
        self.assertGreater(
            server.HTTP_TIMEOUT * server.HTTP_MAX_ATTEMPTS + server.HTTP_BACKOFF_BASE,
            server.TOOL_DEADLINE,
        )

    def test_two_deps_reach_second_repo_inside_deadline_and_remember_timeout(self):
        hang_log = _Log()
        fast_log = _Log()
        osv_log = _Log()
        release = threading.Event()
        hang = self._start(_hang_handler(hang_log, release), release)
        fast = self._start(_metadata_handler(fast_log, _metadata_xml()))
        osv = self._start(_osv_handler(osv_log))
        hang_url = _base(hang)
        fast_url = _base(fast)
        pom = _pom([("hang", hang_url), ("fast", fast_url)], ["one", "two"])

        with temp_project({"pom.xml": pom}) as root, temp_cache_dir() as cache_root:
            with unittest.mock.patch.dict(os.environ, self._env(osv)), \
                    self._hang_patches()[0], self._hang_patches()[1], \
                    self._hang_patches()[2], self._hang_patches()[3]:
                self.assertLess(server.HTTP_TIMEOUT, server.TOOL_DEADLINE)
                self.assertGreater(
                    2 * server.HTTP_TIMEOUT + server.HTTP_BACKOFF_BASE,
                    server.TOOL_DEADLINE,
                )
                started = time.monotonic()
                out = self._audit(root)
                elapsed = time.monotonic() - started

                self.assertLess(elapsed, server.TOOL_DEADLINE)
                self.assertGreater(elapsed, server.HTTP_BACKOFF_BASE)
                self.assertNotIn("partial", out)
                self.assertEqual(
                    [dep["artifactId"] for dep in out["dependencies"]],
                    ["one", "two"],
                )
                for dep in out["dependencies"]:
                    self.assertEqual(dep["currentVersion"], "1.0.0")
                    versions = [row["version"] for row in dep["candidates"]]
                    self.assertIn("1.2.0", versions)
                    self.assertNotIn("error", dep)
                    self.assertNotIn("latestVersion", dep)
                self.assertGreaterEqual(len(fast_log.snapshot()), 2)
                hang_counts = _path_counts(hang_log)
                self.assertGreaterEqual(hang_counts["/com/example/one/maven-metadata.xml"], 1)
                self.assertGreaterEqual(hang_counts["/com/example/two/maven-metadata.xml"], 1)
                # The retried full timeout would still be on the first host.
                self.assertLess(
                    hang_counts["/com/example/one/maven-metadata.xml"],
                    server.HTTP_MAX_ATTEMPTS * 2,
                )
                posts = [event for event in osv_log.snapshot() if event["method"] == "POST"]
                gets = [event for event in osv_log.snapshot() if event["method"] == "GET"]
                self.assertEqual([event["path"] for event in posts], ["/v1/querybatch"])
                self.assertEqual(
                    sorted(event["path"] for event in gets),
                    ["/v1/vulns/CVE-REPRO-1", "/v1/vulns/CVE-REPRO-2"],
                )
                by_artifact = {dep["artifactId"]: dep for dep in out["dependencies"]}
                self.assertEqual(by_artifact["one"]["vulnerabilities"][0]["severity"], "HIGH")
                self.assertEqual(by_artifact["one"]["vulnerabilities"][0]["fixedVersion"], "9.9.9")

                for entry in _cache_entries(cache_root):
                    self.assertIn(entry["status"], (200, 404))
                    self.assertNotIn(hang_url, entry["url"])

                hang_after_first = len(hang_log.snapshot())
                fast_after_first = len(fast_log.snapshot())
                started = time.monotonic()
                again = self._audit(root)
                elapsed_again = time.monotonic() - started
                self.assertLess(elapsed_again, server.HTTP_TIMEOUT)
                self.assertEqual(len(hang_log.snapshot()), hang_after_first)
                self.assertEqual(len(fast_log.snapshot()), fast_after_first)
                for dep in again["dependencies"]:
                    versions = [row["version"] for row in dep["candidates"]]
                    self.assertIn("1.2.0", versions)
                    self.assertNotIn("error", dep)

                # Expiry sends the dead host back to the network. The 200
                # metadata TTL is longer, so the live repository stays cached.
                real_now = time.time
                with unittest.mock.patch.object(
                    server, "_now", lambda: real_now() + server.TTL_REPO_FAILURE + 1,
                ):
                    self._audit(root)
                self.assertGreater(len(hang_log.snapshot()), hang_after_first)

    def test_single_dep_reaches_second_repo_without_waiting_out_the_deadline(self):
        hang_log = _Log()
        fast_log = _Log()
        osv_log = _Log()
        release = threading.Event()
        hang = self._start(_hang_handler(hang_log, release), release)
        fast = self._start(_metadata_handler(fast_log, _metadata_xml()))
        osv = self._start(_osv_handler(osv_log))
        fast_url = _base(fast)
        pom = _pom([("hang", _base(hang)), ("fast", fast_url)], ["one"])

        with temp_project({"pom.xml": pom}) as root, temp_cache_dir():
            with unittest.mock.patch.dict(os.environ, self._env(osv)), \
                    self._hang_patches()[0], self._hang_patches()[1], \
                    self._hang_patches()[2], self._hang_patches()[3]:
                deadline = server.TOOL_DEADLINE
                started = time.monotonic()
                out = self._audit(root)
                elapsed = time.monotonic() - started

        self.assertLess(elapsed, deadline)
        self.assertNotIn("partial", out)
        dep = out["dependencies"][0]
        versions = [row["version"] for row in dep["candidates"]]
        self.assertIn("1.2.0", versions)
        self.assertNotIn("error", dep)
        self.assertNotIn("resolvedFrom", dep)
        hang_times = [event["t"] for event in hang_log.snapshot()]
        fast_times = [event["t"] for event in fast_log.snapshot()]
        self.assertGreaterEqual(len(hang_times), 1)
        self.assertGreaterEqual(len(fast_times), 1)
        self.assertGreater(min(fast_times), hang_times[0])
        self.assertTrue(any(event["method"] == "POST" for event in osv_log.snapshot()))

    def test_fast_5xx_is_remembered_but_not_written_to_file_cache(self):
        bad_log = _Log()
        fast_log = _Log()
        osv_log = _Log()
        bad = self._start(_metadata_handler(bad_log, b"", status=500))
        fast = self._start(_metadata_handler(fast_log, _metadata_xml()))
        osv = self._start(_osv_handler(osv_log))
        bad_url = _base(bad)
        fast_url = _base(fast)
        pom = _pom([("bad", bad_url), ("fast", fast_url)], ["one", "two"])

        with temp_project({"pom.xml": pom}) as root, temp_cache_dir() as cache_root:
            with unittest.mock.patch.dict(os.environ, self._env(osv)):
                started = time.monotonic()
                out = self._audit(root)
                elapsed = time.monotonic() - started
                self._assert_versions(out, fast_url, elapsed)
                self.assertGreaterEqual(len(bad_log.snapshot()), server.HTTP_MAX_ATTEMPTS)
                bad_after_first = len(bad_log.snapshot())
                times = sorted(event["t"] for event in bad_log.snapshot())
                self.assertGreaterEqual(len(times), server.HTTP_MAX_ATTEMPTS)
                if len(times) >= server.HTTP_MAX_ATTEMPTS + 1:
                    self.assertGreaterEqual(
                        times[server.HTTP_MAX_ATTEMPTS] - times[0],
                        server.HTTP_BACKOFF_BASE,
                    )

                started = time.monotonic()
                again = self._audit(root)
                elapsed_again = time.monotonic() - started
                self._assert_versions(again, fast_url, elapsed_again)
                self.assertEqual(len(bad_log.snapshot()), bad_after_first)
                self.assertLess(elapsed_again, elapsed)

                entries = _cache_entries(cache_root)
                self.assertGreaterEqual(len(entries), 1)
                for entry in entries:
                    self.assertIn(entry["status"], (200, 404))
                    self.assertNotIn(bad_url, entry["url"])
                    self.assertIn(fast_url, entry["url"])

    def test_fast_429_is_not_remembered(self):
        bad_log = _Log()
        fast_log = _Log()
        osv_log = _Log()
        bad = self._start(_metadata_handler(bad_log, b"", status=429))
        fast = self._start(_metadata_handler(fast_log, _metadata_xml()))
        osv = self._start(_osv_handler(osv_log))
        bad_url = _base(bad)
        pom = _pom([("bad", bad_url), ("fast", _base(fast))], ["one", "two"])

        with temp_project({"pom.xml": pom}) as root, temp_cache_dir() as cache_root:
            with unittest.mock.patch.dict(os.environ, self._env(osv)):
                self._audit(root)
                after_first = len(bad_log.snapshot())
                self.assertGreaterEqual(after_first, server.HTTP_MAX_ATTEMPTS)
                self._audit(root)
                self.assertGreater(len(bad_log.snapshot()), after_first)
                for entry in _cache_entries(cache_root):
                    self.assertNotIn(bad_url, entry["url"])
                    self.assertIn(entry["status"], (200, 404))

    def test_past_deadline_names_repository_and_skips_osv(self):
        hang_log = _Log()
        fast_log = _Log()
        osv_log = _Log()
        release = threading.Event()
        hang = self._start(_hang_handler(hang_log, release), release)
        fast = self._start(_metadata_handler(fast_log, _metadata_xml()))
        osv = self._start(_osv_handler(osv_log))
        pom = _pom([("hang", _base(hang)), ("fast", _base(fast))], ["one", "two"])

        with temp_project({"pom.xml": pom}) as root, temp_cache_dir():
            with unittest.mock.patch.dict(os.environ, self._env(osv)), \
                    unittest.mock.patch.object(server, "TOOL_DEADLINE", -1.0):
                out = self._audit(root)
                # Let a worker that ignored the deadline show up in the logs.
                time.sleep(0.05)

        self.assertTrue(out["partial"])
        self.assertTrue(any("deadline" in note.lower() for note in out["notes"]))
        self.assertEqual(len(out["dependencies"]), 2)
        for dep in out["dependencies"]:
            self.assertIn("deadline", dep["error"])
            self.assertIn("hang", dep["error"])
            self.assertIn("later repositories were not queried", dep["error"])
            self.assertNotIn("latestVersion", dep)
            self.assertEqual(dep["currentVersion"], "1.0.0")
        self.assertEqual(hang_log.snapshot(), [])
        self.assertEqual(fast_log.snapshot(), [])
        self.assertEqual(osv_log.snapshot(), [])

    def test_in_flight_last_repository_does_not_claim_later_were_skipped(self):
        # Nine deps and two silent repos can abandon a slot whose GET to the
        # last repository was already accepted. len(repos) >= 2 must not add
        # the "later repositories" phrase in that case.
        dep = {"groupId": "com.example", "artifactId": "a8"}
        repos = [
            {
                "name": "hang-a",
                "url": "http://hang-a",
                "scope": "dependency",
                "is_public_fallback": False,
            },
            {
                "name": "hang-b",
                "url": "http://hang-b",
                "scope": "dependency",
                "is_public_fallback": False,
            },
        ]
        dep_id = id(dep)
        try:
            with unittest.mock.patch.object(server, "_repos_for", return_value=repos):
                server._repo_probe_labels[dep_id] = ("hang-b", False)
                on_last = server._audit_deadline_skip_error(dep, unittest.mock.Mock())
                server._repo_probe_labels[dep_id] = ("hang-a", True)
                on_first = server._audit_deadline_skip_error(dep, unittest.mock.Mock())
        finally:
            server._repo_probe_labels.pop(dep_id, None)

        self.assertEqual(
            on_last,
            f"skipped: exceeded the {server.TOOL_DEADLINE}s tool deadline "
            "while waiting for hang-b",
        )
        self.assertNotIn("later repositories were not queried", on_last)
        self.assertIn("while waiting for hang-a", on_first)
        self.assertIn("later repositories were not queried", on_first)

    def test_nine_deps_in_flight_on_last_repo_do_not_claim_later_unqueried(self):
        # Nine deps, pool of 8, two silent repos. A slot abandoned while the
        # last repo's GET is already accepted must not say later repositories
        # were not queried. Two repos in scope is not that claim.
        artifacts = [f"a{index}" for index in range(9)]
        hang_a_log = _Log()
        hang_b_log = _Log()
        osv_log = _Log()
        release = threading.Event()
        hang_a = self._start(_hang_handler(hang_a_log, release), release)
        hang_b = self._start(_hang_handler(hang_b_log, release), release)
        osv = self._start(_osv_handler(osv_log))
        pom = _pom(
            [("hang-a", _base(hang_a)), ("hang-b", _base(hang_b))],
            artifacts,
        )

        with temp_project({"pom.xml": pom}) as root, temp_cache_dir():
            with unittest.mock.patch.dict(os.environ, self._env(osv)), \
                    self._hang_patches()[0], self._hang_patches()[1], \
                    self._hang_patches()[2], self._hang_patches()[3]:
                deadline = server.TOOL_DEADLINE
                out = self._audit(root)
        returned = time.monotonic()
        waiting_for_last = (
            f"skipped: exceeded the {deadline}s tool deadline "
            "while waiting for hang-b"
        )

        self.assertTrue(out["partial"])
        self.assertEqual(len(out["dependencies"]), 9)
        self.assertEqual(osv_log.snapshot(), [])
        contacted = set()
        for event in hang_b_log.snapshot():
            self.assertLessEqual(event["t"], returned)
            parts = event["path"].split("/")
            # /com/example/<artifact>/maven-metadata.xml
            self.assertEqual(parts[3:], [parts[3], "maven-metadata.xml"])
            contacted.add(parts[3])
        waiting = []
        by_artifact = {}
        for dep in out["dependencies"]:
            artifact = dep["artifactId"]
            by_artifact[artifact] = dep
            error = dep.get("error") or ""
            if artifact in contacted:
                self.assertNotIn("later repositories were not queried", error)
                self.assertNotIn("latestVersion", dep)
            if error == waiting_for_last:
                waiting.append(artifact)
                self.assertIn(artifact, contacted)
        self.assertGreaterEqual(len(waiting), 1, by_artifact)
        self.assertIn("a8", by_artifact)

    def test_cached_200_beats_remembered_failure_and_a_passed_deadline(self):
        # A timeout/5xx remembered in memory must not hide a metadata document
        # already on disk, including when the tool deadline has already passed.
        # A cache hit does not contact the repository.
        log = _Log()
        bad = self._start(_metadata_handler(log, b"", status=500))
        bad_url = _base(bad)
        pom = _pom([("bad", bad_url)], ["one"])
        meta_url = server._metadata_url(bad_url, "com.example", "one")
        body = _metadata_xml(("2.0.0",))

        with temp_project({"pom.xml": pom}) as root, temp_cache_dir():
            with unittest.mock.patch.dict(os.environ, self._env(self._start(_osv_handler(_Log())))):
                server._repo_failure_remember(meta_url, "HTTP 500 from bad")
                server._file_cache.set(meta_url, 200, body)
                out = self._audit(root)
                self.assertFalse(any(
                    event["path"].endswith("maven-metadata.xml")
                    for event in log.snapshot()
                ))
                dep = out["dependencies"][0]
                versions = [row["version"] for row in dep["candidates"]]
                self.assertIn("2.0.0", versions)
                self.assertNotIn("error", dep)
                self.assertIsNone(server._repo_failure_lookup(meta_url))

                server._repo_failure_remember(meta_url, "HTTP 500 from bad")
                before_late = len(log.snapshot())
                with unittest.mock.patch.object(server, "TOOL_DEADLINE", -1.0):
                    late = self._audit(root)
                self.assertEqual(len(log.snapshot()), before_late)
                late_versions = [
                    row["version"] for row in late["dependencies"][0]["candidates"]
                ]
                self.assertIn("2.0.0", late_versions)
                self.assertNotIn("error", late["dependencies"][0])
                self.assertIsNone(server._repo_failure_lookup(meta_url))

    def test_discarded_finished_worker_keeps_the_last_probe_label(self):
        # _map_parallel can drop a finished worker's return value (the slot
        # stays None). The deadline phrase must name the repository that
        # worker actually contacted, not the first declared one, and must not
        # invent "later repositories were not queried" from len(repos) >= 2.
        bad_log = _Log()
        fast_log = _Log()
        bad = self._start(_metadata_handler(bad_log, b"", status=500))
        fast = self._start(_metadata_handler(fast_log, _metadata_xml()))
        osv = self._start(_osv_handler(_Log()))
        pom = _pom([("bad", _base(bad)), ("fast", _base(fast))], ["one", "two"])

        def discard(items, fn, max_workers=8, deadline=None):
            for item in items:
                fn(item)
            return [None] * len(items), True

        with temp_project({"pom.xml": pom}) as root, temp_cache_dir():
            with unittest.mock.patch.dict(os.environ, self._env(osv)), \
                    unittest.mock.patch.object(server, "_map_parallel", discard):
                out = self._audit(root)

        self.assertGreaterEqual(len(fast_log.snapshot()), 2)
        for dep in out["dependencies"]:
            self.assertEqual(
                dep["error"],
                f"skipped: exceeded the {server.TOOL_DEADLINE}s tool deadline "
                "while waiting for fast",
            )

    def _assert_versions(self, out, _fast_url, elapsed):
        self.assertNotIn("partial", out)
        self.assertLess(elapsed, 3.0)
        for dep in out["dependencies"]:
            self.assertEqual(dep["currentVersion"], "1.0.0")
            versions = [row["version"] for row in dep["candidates"]]
            self.assertIn("1.2.0", versions)
            row = next(item for item in dep["candidates"] if item["version"] == "1.2.0")
            self.assertEqual(row["distance"], "minor")
            self.assertNotIn("error", dep)
            self.assertNotIn("latestVersion", dep)


if __name__ == "__main__":
    unittest.main()
