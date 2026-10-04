"""The wheel is what `uvx maven-mcp` installs.

`check_version_compatibility` opens `compat-matrices.json` next to the running
`server` module. A checkout test cannot see a wheel that drops that file.
Building needs hatchling on the interpreter (`pip install 'hatchling>=1.26.3'`;
CI does this before `unittest discover`).
"""

import os
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_MATRIX = ROOT / "plugin" / "server" / "compat-matrices.json"


def _build_wheel(dest: str) -> str:
    try:
        # Build backend only. The mypy job does not install it; the ignore
        # is what keeps `import-not-found` from failing that check.
        from hatchling.build import build_wheel  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise AssertionError(
            "hatchling is required to build the wheel "
            "(pip install 'hatchling>=1.26.3')"
        ) from exc
    previous = os.getcwd()
    os.chdir(ROOT)
    try:
        # PEP 517 hook: reads pyproject.toml from the process cwd.
        return os.path.join(dest, build_wheel(dest))
    finally:
        os.chdir(previous)


def _readme_prose(readme: str) -> str:
    return next(
        block.strip()
        for block in readme.split("\n\n")
        if block.strip() and not block.lstrip().startswith("#")
    )


class WheelPackagingTest(unittest.TestCase):
    def test_wheel_ships_matrix_beside_server_and_metadata(self):
        paragraph = _readme_prose((ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertGreater(len(paragraph), 40)

        with tempfile.TemporaryDirectory() as dest:
            wheel_path = _build_wheel(dest)
            with zipfile.ZipFile(wheel_path) as zf:
                names = zf.namelist()
                self.assertEqual(
                    [
                        name for name in names
                        if name.endswith("server.py") and ".dist-info/" not in name
                    ],
                    ["server.py"],
                    names,
                )
                self.assertEqual(
                    [name for name in names if name.endswith("compat-matrices.json")],
                    ["compat-matrices.json"],
                    names,
                )
                self.assertEqual(zf.read("compat-matrices.json"), _MATRIX.read_bytes())
                metadata = zf.read(
                    next(name for name in names if name.endswith(".dist-info/METADATA"))
                ).decode("utf-8")

        self.assertIn("Name: maven-mcp\n", metadata)
        self.assertIn("Version: 1.1.0\n", metadata)
        self.assertIn("License-Expression: MIT\n", metadata)
        self.assertIn("Requires-Python: >=3.9\n", metadata)
        self.assertNotIn("Requires-Dist:", metadata)
        self.assertIn(paragraph, metadata)
