"""AAR SDK floor reads. No Android SDK and no CheckAarMetadata task.

The outbound AAR request must advertise identity encoding. A mocked raw zip
that still sets floors is not enough: the request headers are asserted, and a
missing or oversized Content-Length is not read.
"""

import io
import unittest
import unittest.mock
import zipfile

from _helpers import _MockHeaders, server, temp_project


def _tiny_aar(min_compile, min_sdk, pad=0, pad_first=True):
    buf = io.BytesIO()
    meta = (
        "META-INF/com/android/build/gradle/aar-metadata.properties",
        f"aarFormatVersion=1.0\nminCompileSdk={min_compile}\n",
    )
    manifest = (
        "AndroidManifest.xml",
        (
            '<manifest xmlns:android="http://schemas.android.com/apk/res/android">'
            f'<uses-sdk android:minSdkVersion="{min_sdk}" />'
            "</manifest>"
        ),
    )
    with zipfile.ZipFile(buf, "w") as zf:
        if pad and pad_first:
            zf.writestr("pad.bin", b"x" * pad, compress_type=zipfile.ZIP_STORED)
        zf.writestr(meta[0], meta[1], compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr(manifest[0], manifest[1], compress_type=zipfile.ZIP_DEFLATED)
        if pad and not pad_first:
            zf.writestr("pad.bin", b"x" * pad, compress_type=zipfile.ZIP_STORED)
    return buf.getvalue()


def _app_build(compile_sdk, min_sdk):
    return (
        "plugins {\n"
        '    id("com.android.application")\n'
        "}\n"
        "android {\n"
        f"    compileSdk = {compile_sdk}\n"
        f"    minSdk = {min_sdk}\n"
        "}\n"
    )


class _UnreadResponse:
    """200/206 stand-in whose body must not be read."""

    def __init__(self, status, headers):
        self.status = status
        self.headers = _MockHeaders(headers)
        self.read_called = False

    def read(self, n=-1):
        self.read_called = True
        raise AssertionError("AAR body was read")

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _BodyResponse:
    def __init__(self, status, body, headers):
        self.status = status
        self._body = body
        self._pos = 0
        self.headers = _MockHeaders(headers)

    def read(self, n=-1):
        if n is None or n < 0:
            chunk = self._body[self._pos:]
            self._pos = len(self._body)
            return chunk
        chunk = self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class AarSdkRequirementsTest(unittest.TestCase):
    def _project(self, build_file):
        return {
            "settings.gradle.kts": (
                "dependencyResolutionManagement {\n"
                "  repositories { mavenCentral() }\n"
                "}\n"
            ),
            "build.gradle.kts": build_file,
        }

    def _call(self, files, urlopen):
        with temp_project(files) as root:
            with unittest.mock.patch("urllib.request.urlopen", side_effect=urlopen):
                return server.handle_get_aar_sdk_requirements({
                    "groupId": "androidx.core",
                    "artifactId": "core-ktx",
                    "version": "1.19.1",
                    "projectPath": root,
                })

    def _zip_urlopen(self, zip_bytes, requests, status_for_aar=200):
        def urlopen(req, timeout=None, context=None):
            requests.append(req)
            url = req.full_url
            if url.endswith(".pom") or url.endswith("maven-metadata.xml"):
                return _BodyResponse(404, b"", {"Content-Length": "0"})
            if not url.endswith(".aar"):
                return _BodyResponse(404, b"", {"Content-Length": "0"})
            range_header = req.get_header("Range") or ""
            if range_header.startswith("bytes=-"):
                n = int(range_header[len("bytes=-"):])
                start = max(0, len(zip_bytes) - n)
                chunk = zip_bytes[start:]
                if start == 0 and status_for_aar == 200:
                    return _BodyResponse(200, chunk, {
                        "Content-Length": str(len(chunk)),
                    })
                end = start + len(chunk) - 1
                return _BodyResponse(206, chunk, {
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{len(zip_bytes)}",
                })
            spec = range_header[len("bytes="):]
            start_s, end_s = spec.split("-", 1)
            start = int(start_s)
            end = int(end_s)
            chunk = zip_bytes[start:end + 1]
            return _BodyResponse(206, chunk, {
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{len(zip_bytes)}",
            })
        return urlopen

    def _aar_requests(self, requests):
        return [req for req in requests if req.full_url.endswith(".aar")]

    def test_requires_compile_sdk_when_above_project(self):
        requests = []
        zip_bytes = _tiny_aar(37, 24)
        out = self._call(
            self._project(_app_build(36, 24)),
            self._zip_urlopen(zip_bytes, requests),
        )
        self.assertEqual(out["requiresCompileSdk"], 37)
        self.assertNotIn("requiresMinSdk", out)
        self.assertEqual(out["projectCompileSdk"], 36)
        aar_reqs = self._aar_requests(requests)
        self.assertTrue(aar_reqs)
        accept = aar_reqs[0].get_header("Accept-encoding")
        self.assertEqual(accept, "identity")
        self.assertNotIn("gzip", accept.lower())
        self.assertEqual(
            aar_reqs[0].get_header("Range"),
            f"bytes=-{server._AAR_EOCD_TAIL_BYTES}",
        )
        self.assertEqual(server._AAR_EOCD_TAIL_BYTES, 64 * 1024 + 22)
        for req in aar_reqs:
            range_header = req.get_header("Range") or ""
            if range_header.startswith("bytes=-"):
                self.assertLessEqual(
                    int(range_header[len("bytes=-"):]),
                    server._AAR_EOCD_TAIL_BYTES,
                )

    def test_aar_request_keeps_authorization_without_repo_headers_helper(self):
        requests = []

        def creds(_entry):
            return {"type": "bearer", "token": "secret"}

        with unittest.mock.patch.object(server, "resolve_repo_credentials", side_effect=creds):
            with unittest.mock.patch.object(
                server, "_repo_request_headers", side_effect=AssertionError("unchanged helper")
            ):
                out = self._call(
                    self._project(_app_build(36, 24)),
                    self._zip_urlopen(_tiny_aar(37, 24), requests),
                )
        self.assertEqual(out["requiresCompileSdk"], 37)
        aar_req = self._aar_requests(requests)[0]
        self.assertEqual(aar_req.get_header("Authorization"), "Bearer secret")
        self.assertEqual(aar_req.get_header("Accept-encoding"), "identity")

    def test_requires_min_sdk_only_when_above_project(self):
        requests = []
        out = self._call(
            self._project(_app_build(36, 24)),
            self._zip_urlopen(_tiny_aar(36, 26), requests),
        )
        self.assertNotIn("requiresCompileSdk", out)
        self.assertEqual(out["requiresMinSdk"], 26)

    def test_values_at_or_below_project_set_neither_field(self):
        requests = []
        out = self._call(
            self._project(_app_build(36, 24)),
            self._zip_urlopen(_tiny_aar(36, 24), requests),
        )
        self.assertNotIn("requiresCompileSdk", out)
        self.assertNotIn("requiresMinSdk", out)

    def test_catalog_accessor_does_not_invent_a_number(self):
        files = {
            "settings.gradle.kts": 'include(":app", ":lib")\n',
            "app/build.gradle.kts": (
                "plugins { id(\"com.android.application\") }\n"
                "android {\n"
                "    compileSdk = libs.versions.compile.sdk.get().toInt()\n"
                "    minSdk = libs.versions.min.sdk.get().toInt()\n"
                "}\n"
            ),
            "lib/build.gradle.kts": (
                "plugins { id(\"com.android.library\") }\n"
                "android { compileSdk = 36\n minSdk = 21 }\n"
            ),
        }
        requests = []
        out = self._call(files, self._zip_urlopen(_tiny_aar(37, 28), requests))
        self.assertNotIn("requiresCompileSdk", out)
        self.assertNotIn("requiresMinSdk", out)
        self.assertNotIn("projectCompileSdk", out)
        self.assertNotIn("projectMinSdk", out)
        self.assertNotIn("37", out.get("note", ""))

    def test_missing_or_oversized_content_length_is_not_read(self):
        cases = [
            {"Content-Type": "application/java-archive"},
            {"Content-Length": str(server._AAR_EOCD_TAIL_BYTES + 1)},
            {"Content-Length": "nope"},
        ]
        for headers in cases:
            unread = _UnreadResponse(200, headers)
            requests = []

            def urlopen(req, timeout=None, context=None, _unread=unread, _requests=requests):
                _requests.append(req)
                if req.full_url.endswith(".aar"):
                    return _unread
                return _BodyResponse(404, b"", {"Content-Length": "0"})

            with unittest.mock.patch.object(
                server, "_inflate_gzip_capped", side_effect=AssertionError("inflated")
            ):
                out = self._call(self._project(_app_build(36, 24)), urlopen)
            self.assertFalse(unread.read_called)
            self.assertNotIn("requiresCompileSdk", out)
            self.assertNotIn("requiresMinSdk", out)
            aar_reqs = self._aar_requests(requests)
            self.assertEqual(len(aar_reqs), 1)
            self.assertEqual(
                aar_reqs[0].get_header("Range"),
                f"bytes=-{server._AAR_EOCD_TAIL_BYTES}",
            )

    def test_gzip_content_encoding_is_not_inflated(self):
        unread = _UnreadResponse(200, {
            "Content-Length": "32",
            "Content-Encoding": "gzip",
        })

        def urlopen(req, timeout=None, context=None):
            if req.full_url.endswith(".aar"):
                accept = req.get_header("Accept-encoding") or ""
                self.assertEqual(accept, "identity")
                self.assertNotIn("gzip", accept.lower())
                return unread
            return _BodyResponse(404, b"", {"Content-Length": "0"})

        with unittest.mock.patch.object(
            server, "_inflate_gzip_capped", side_effect=AssertionError("inflated")
        ):
            out = self._call(self._project(_app_build(36, 24)), urlopen)
        self.assertFalse(unread.read_called)
        self.assertNotIn("requiresCompileSdk", out)
        self.assertNotIn("requiresMinSdk", out)

    def test_partial_tail_still_sets_the_floor(self):
        # Larger than the EOCD tail, so a 200 of the whole archive would be
        # refused. The suffix 206 plus later absolute ranges is the live path.
        requests = []
        zip_bytes = _tiny_aar(37, 28, pad=80 * 1024, pad_first=False)
        self.assertGreater(len(zip_bytes), server._AAR_EOCD_TAIL_BYTES)
        out = self._call(
            self._project(_app_build(36, 24)),
            self._zip_urlopen(zip_bytes, requests, status_for_aar=206),
        )
        self.assertEqual(out["requiresCompileSdk"], 37)
        self.assertEqual(out["requiresMinSdk"], 28)
        aar_reqs = self._aar_requests(requests)
        self.assertGreaterEqual(len(aar_reqs), 2)
        for req in aar_reqs:
            self.assertEqual(req.get_header("Accept-encoding"), "identity")
            range_header = req.get_header("Range") or ""
            if range_header.startswith("bytes=-"):
                self.assertEqual(
                    int(range_header[len("bytes=-"):]),
                    server._AAR_EOCD_TAIL_BYTES,
                )


if __name__ == "__main__":
    unittest.main()
