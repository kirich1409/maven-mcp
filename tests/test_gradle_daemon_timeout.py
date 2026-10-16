"""A resolve timeout must not leave its daemon holding the project lock.

Calls the real ``_run_gradle_command``. A mocked ``TimeoutExpired`` does not
show this bug. Skips only when ``java`` or ``gradle`` is already absent from
PATH — does not install a toolchain or download a wrapper distribution.
"""

import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
import unittest.mock

from _helpers import server


_HOLD_MS = 120_000
_TIMEOUT_S = 15
_FOLLOW_TIMEOUT_S = 20


def _alive(pid):
    if not pid or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _cmdline(pid):
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-ww", "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout or ""


def _daemon_logs(root):
    found = set()
    if not root or not os.path.isdir(root):
        return found
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.startswith("daemon-") and name.endswith(".out.log"):
                found.add(os.path.join(dirpath, name))
    return found


def _pid_of_log(path):
    name = os.path.basename(path)
    text = name[len("daemon-"):-len(".out.log")]
    if not text.isdigit():
        return None
    return int(text)


def _gradle_daemon_pids():
    try:
        result = subprocess.run(
            ["ps", "-ax", "-o", "pid=", "-o", "command="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    pids = set()
    for line in (result.stdout or "").splitlines():
        stripped = line.strip()
        if "GradleDaemon" not in stripped:
            continue
        pid_text = stripped.split(None, 1)[0]
        if pid_text.isdigit():
            pids.add(int(pid_text))
    return pids


def _sigkill(pid, protected):
    if not pid or pid <= 1 or pid in protected or pid == os.getpid():
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return


def _wait_dead(pid, seconds=2.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


_LOCK_PROBE = """\
import java.io.File;
import java.io.RandomAccessFile;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;

public class LockProbe {
    public static void main(String[] args) {
        int blocked = 0;
        for (String path : args) {
            File file = new File(path);
            if (!file.isFile()) {
                System.out.println("MISSING " + path);
                blocked++;
                continue;
            }
            RandomAccessFile raf = null;
            try {
                raf = new RandomAccessFile(file, "rw");
                FileChannel channel = raf.getChannel();
                FileLock lock = channel.tryLock();
                if (lock == null) {
                    System.out.println("HELD " + path);
                    blocked++;
                } else {
                    System.out.println("FREE " + path);
                    lock.release();
                }
            } catch (Exception ex) {
                System.out.println("ERR " + path + " " + ex.getClass().getSimpleName());
                blocked++;
            } finally {
                if (raf != null) {
                    try {
                        raf.close();
                    } catch (Exception ignored) {
                    }
                }
            }
        }
        System.exit(blocked == 0 ? 0 : 2);
    }
}
"""


def _project_lock_paths(project):
    """The exclusive locks a timed-out configuration holds. Version dir is not hardcoded."""
    gradle = os.path.join(project, ".gradle")
    paths = [
        os.path.join(gradle, "configuration-cache", "configuration-cache.lock"),
        os.path.join(gradle, "buildOutputCleanup", "buildOutputCleanup.lock"),
    ]
    if os.path.isdir(gradle):
        for name in os.listdir(gradle):
            base = os.path.join(gradle, name)
            if not os.path.isdir(base):
                continue
            paths.append(os.path.join(base, "fileHashes", "fileHashes.lock"))
            paths.append(os.path.join(base, "checksums", "checksums.lock"))
    return paths


def _locks_free(project, work):
    """True only when every required lock file exists and FileChannel.tryLock takes it."""
    javac = shutil.which("javac")
    java = shutil.which("java")
    if not javac or not java:
        return False, "javac/java missing; cannot probe locks"
    src = os.path.join(work, "LockProbe.java")
    _write(src, _LOCK_PROBE)
    compiled = subprocess.run(
        [javac, src],
        cwd=work,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if compiled.returncode != 0:
        return False, compiled.stderr
    paths = _project_lock_paths(project)
    required = (
        "configuration-cache.lock",
        "fileHashes.lock",
        "checksums.lock",
        "buildOutputCleanup.lock",
    )
    present = {os.path.basename(path) for path in paths if os.path.isfile(path)}
    missing = [name for name in required if name not in present]
    if missing:
        return False, "missing lock files: %s" % ", ".join(missing)
    existing = [path for path in paths if os.path.isfile(path)]
    probed = subprocess.run(
        [java, "-cp", work, "LockProbe", *existing],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    detail = (probed.stdout or "") + (probed.stderr or "")
    return probed.returncode == 0, detail


class GradleDaemonIsolationContractTest(unittest.TestCase):
    """Argv contract. Does not show the lock bug; the live test does."""

    def test_offline_prefix_and_constant_pid_script(self):
        seen = []

        def capture(cmd, **kwargs):
            path = cmd[cmd.index("--init-script") + 1]
            with open(path, "rb") as handle:
                script = handle.read()
            seen.append((list(cmd), dict(kwargs.get("env") or {}), script))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.dict(
                os.environ,
                {"MAVEN_MCP_OFFLINE": "1", "XDG_CACHE_HOME": tmp},
            ):
                os.environ.pop("GRADLE_USER_HOME", None)
                with unittest.mock.patch.object(server, "_gradle_run", side_effect=capture):
                    server._run_gradle_command(
                        "/tmp", "gradlew", ["--init-script", "caller.gradle", "-q", "help"],
                    )
                    server._run_gradle_command(
                        "/tmp", "gradlew", ["--init-script", "caller.gradle", "-q", "help"],
                    )
                registry = os.path.abspath(
                    os.path.join(tmp, "maven-central-mcp", "gradle-daemon-registry")
                )
                self.assertEqual(os.stat(registry).st_mode & 0o777, 0o700)
        self.assertEqual(len(seen), 2)
        for cmd, env, script in seen:
            self.assertEqual(cmd[0], "gradlew")
            self.assertEqual(cmd[1], "--offline")
            self.assertEqual(cmd[2], "--init-script")
            self.assertNotIn("--no-daemon", cmd)
            self.assertNotIn("--stop", cmd)
            self.assertIn("caller.gradle", cmd)
            self.assertIn("-Dorg.gradle.daemon.registry.base=" + registry, cmd)
            self.assertTrue(any(arg.startswith("-Dmaven.mcp.daemon.pidfile=") for arg in cmd))
            self.assertNotIn("GRADLE_USER_HOME", env)
            self.assertEqual(script, server._GRADLE_DAEMON_PID_INIT_SCRIPT.encode("utf-8"))
            self.assertNotIn(b"com.example", script)
        self.assertEqual(seen[0][2], seen[1][2])

    def test_registry_failure_keeps_today_command_and_does_not_kill(self):
        seen = []

        def capture(cmd, **_kwargs):
            seen.append(list(cmd))
            raise subprocess.TimeoutExpired(cmd, 1)

        with unittest.mock.patch.object(server, "_gradle_daemon_registry_dir", return_value=None), \
                unittest.mock.patch.object(server, "_kill_isolated_gradle_daemon") as kill, \
                unittest.mock.patch.object(server, "_gradle_run", side_effect=capture):
            code, stdout, stderr = server._run_gradle_command("/tmp", "gradlew", ["-q", "help"])
        self.assertEqual((code, stdout, stderr), (124, "", "Gradle command timed out after 300s"))
        self.assertEqual(seen, [["gradlew", "-q", "help"]])
        kill.assert_not_called()


@unittest.skipUnless(
    shutil.which("java") and shutil.which("gradle"),
    "java or gradle is not already on PATH",
)
class GradleDaemonTimeoutTest(unittest.TestCase):
    def test_timeout_releases_project_lock_and_leaves_the_ide_daemon(self):
        protected = _gradle_daemon_pids()
        root = tempfile.mkdtemp(prefix="maven-mcp-daemon-timeout-")
        guh = os.path.join(root, "gradle-user-home")
        xdg = os.path.join(root, "xdg")
        short = os.path.join(root, "short")
        hold = os.path.join(root, "hold")
        os.makedirs(guh)
        os.makedirs(xdg)
        self._write_project(short, hold=False)
        self._write_project(hold, hold=True)
        registry = os.path.abspath(
            os.path.join(xdg, "maven-central-mcp", "gradle-daemon-registry")
        )
        ide_pid = None
        isolated_pid = None
        old_env = {
            "XDG_CACHE_HOME": os.environ.get("XDG_CACHE_HOME"),
            "GRADLE_USER_HOME": os.environ.get("GRADLE_USER_HOME"),
        }
        os.environ["XDG_CACHE_HOME"] = xdg
        os.environ["GRADLE_USER_HOME"] = guh
        try:
            ide_pid = self._start_ide_daemon(short, guh)
            self.assertTrue(_alive(ide_pid))
            self.assertIn("GradleDaemon", _cmdline(ide_pid))
            self.assertFalse(any(ide_pid == _pid_of_log(path) for path in _daemon_logs(registry)))

            first = server._run_gradle_command(short, self._gradlew(short), ["-q", "help"], timeout=90)
            self.assertEqual(first[0], 0, first[2])
            self.assertEqual(os.environ.get("GRADLE_USER_HOME"), guh)
            self.assertTrue(
                os.path.isdir(os.path.join(guh, "caches")),
                "server must not redirect GRADLE_USER_HOME away from the test home",
            )
            isolated_logs = _daemon_logs(registry)
            self.assertEqual(len(isolated_logs), 1, isolated_logs)
            isolated_pid = _pid_of_log(next(iter(isolated_logs)))
            self.assertIsNotNone(isolated_pid)
            self.assertNotEqual(isolated_pid, ide_pid)
            self.assertTrue(_alive(isolated_pid))
            self.assertIn("GradleDaemon", _cmdline(isolated_pid))
            with open(next(iter(isolated_logs)), encoding="utf-8", errors="replace") as handle:
                log_text = handle.read()
            self.assertIn("daemonRegistryDir=" + registry, log_text)
            self._assert_child_home(isolated_pid, guh)

            before_second = set(isolated_logs)
            second = server._run_gradle_command(short, self._gradlew(short), ["-q", "help"], timeout=60)
            self.assertEqual(second[0], 0, second[2])
            self.assertEqual(_daemon_logs(registry), before_second)
            self.assertTrue(_alive(isolated_pid))
            self.assertTrue(_alive(ide_pid))

            started = time.monotonic()
            code, stdout, stderr = server._run_gradle_command(
                hold,
                self._gradlew(hold),
                ["-q", "help", "-Dmav.hold=true"],
                timeout=_TIMEOUT_S,
            )
            elapsed = time.monotonic() - started
            self.assertEqual(code, 124)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "Gradle command timed out after %ss" % _TIMEOUT_S)
            self.assertLess(elapsed, 30, "timeout path waited on the configuration loop")

            new_logs = _daemon_logs(registry) - before_second
            if len(new_logs) == 1:
                used = _pid_of_log(next(iter(new_logs)))
            elif not new_logs:
                used = isolated_pid
            else:
                used = None
            dead = used is not None and _wait_dead(used)
            locks_free, lock_detail = _locks_free(hold, root)
            self.assertTrue(
                dead or locks_free,
                "daemon %s still alive and locks not free\n%s" % (used, lock_detail),
            )
            self._assert_following_help_does_not_block(hold, guh)
            self.assertTrue(_alive(ide_pid), "IDE daemon was killed")
            self.assertIn("GradleDaemon", _cmdline(ide_pid))
            self.assertNotEqual(ide_pid, used)
        finally:
            self._cleanup(protected, registry, guh, ide_pid, isolated_pid)
            shutil.rmtree(root, ignore_errors=True)
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def _write_project(self, project, hold):
        _write(os.path.join(project, "settings.gradle"), "rootProject.name = 'mav13'\n")
        _write(
            os.path.join(project, "gradle.properties"),
            "org.gradle.daemon=true\n"
            "org.gradle.vfs.watch=false\n"
            "org.gradle.welcome=never\n"
            "org.gradle.configuration-cache=true\n",
        )
        # The loop is in the project build file, after init scripts, so the pid
        # file is written before the lock is held. Sleep ignores interrupt so a
        # client cancel does not drop the lock by itself.
        if hold:
            build = (
                "if (System.getProperty('mav.hold') == 'true') {\n"
                "    def end = System.currentTimeMillis() + %dL\n"
                "    while (System.currentTimeMillis() < end) {\n"
                "        try {\n"
                "            Thread.sleep(200L)\n"
                "        } catch (InterruptedException ignored) {\n"
                "        }\n"
                "    }\n"
                "}\n" % _HOLD_MS
            )
        else:
            build = "tasks.register('noop')\n"
        _write(os.path.join(project, "build.gradle"), build)
        gradlew = self._gradlew(project)
        _write(gradlew, "#!/bin/sh\nexec gradle \"$@\"\n")
        os.chmod(gradlew, 0o755)

    def _gradlew(self, project):
        return os.path.join(project, "gradlew")

    def _start_ide_daemon(self, project, guh):
        env = os.environ.copy()
        env["GRADLE_USER_HOME"] = guh
        before = _daemon_logs(os.path.join(guh, "daemon"))
        result = subprocess.run(
            [self._gradlew(project), "-q", "help"],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        new = _daemon_logs(os.path.join(guh, "daemon")) - before
        self.assertEqual(len(new), 1, new)
        pid = _pid_of_log(next(iter(new)))
        self.assertTrue(_alive(pid))
        return pid

    def _assert_child_home(self, pid, guh):
        try:
            result = subprocess.run(
                ["ps", "eww", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None
        text = "" if result is None else (result.stdout or "")
        if "GRADLE_USER_HOME=" in text:
            self.assertIn("GRADLE_USER_HOME=" + guh, text)
            self.assertNotIn("GRADLE_USER_HOME=" + os.path.expanduser("~/.gradle"), text)

    def _assert_following_help_does_not_block(self, project, guh):
        env = os.environ.copy()
        env["GRADLE_USER_HOME"] = guh
        started = time.monotonic()
        try:
            result = subprocess.run(
                [self._gradlew(project), "-q", "help"],
                cwd=project,
                env=env,
                capture_output=True,
                text=True,
                timeout=_FOLLOW_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self.fail("following help blocked on the timed-out daemon: %s" % exc)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, _FOLLOW_TIMEOUT_S)
        self.assertEqual(
            result.returncode,
            0,
            "following help failed\n%s" % ((result.stderr or "")[-500:]),
        )

    def _cleanup(self, protected, registry, guh, ide_pid, isolated_pid):
        doomed = set()
        if ide_pid:
            doomed.add(ide_pid)
        if isolated_pid:
            doomed.add(isolated_pid)
        for path in _daemon_logs(registry) | _daemon_logs(os.path.join(guh, "daemon")):
            pid = _pid_of_log(path)
            if pid:
                doomed.add(pid)
        for pid in doomed:
            _sigkill(pid, protected)
