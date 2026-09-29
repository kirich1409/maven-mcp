"""Pin the non-Claude plugin manifests shipped from plugin/.

- plugin/plugin.json + plugin/mcp.json — Agent Plugins 1.0 portable format
  (closed manifest schema, ${PLUGIN_ROOT} expansion only in args/env/cwd).
- plugin/.codex-plugin/plugin.json — Codex native manifest.
- plugin/.cursor-plugin/plugin.json + plugin/hooks/cursor-hooks.json — Cursor.

The Claude Code / Grok Build files (.claude-plugin/, .mcp.json, hooks.json)
stay the reference; these tests keep the other formats from drifting away
from them (server id, server script, skills, version).
"""

import json
import os
import re
import subprocess
import tempfile
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.normpath(os.path.join(_TESTS_DIR, "..", "plugin"))

_PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
_MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
_ALLOWED_PLUGIN_KEYS = {
    "$schema", "name", "version", "description", "author", "homepage",
    "repository", "license", "keywords", "extensions",
}
# Agent Plugins 1.0: 1-64 chars, lowercase alnum / hyphen / period, alnum at
# both ends, no consecutive hyphens or periods.
_PLUGIN_NAME_RE = re.compile(r"^(?!.*[-.]{2})[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?$")
# Agent Skills: lowercase alnum and single hyphens, max 64 chars.
_SKILL_NAME_RE = re.compile(r"^(?!.*--)[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


def _load(rel):
    with open(os.path.join(_PLUGIN_DIR, rel), encoding="utf-8") as fh:
        return json.load(fh)


def _skill_frontmatter(path):
    """Return {name, description} from a SKILL.md YAML frontmatter (subset parser)."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if match is None:
        return {}
    fields = {}
    key = None
    for line in match.group(1).splitlines():
        top = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if top:
            key = top.group(1)
            value = top.group(2).strip()
            fields[key] = "" if value in (">-", ">", "|", "|-") else value
        elif key is not None and line.startswith(" "):
            fields[key] = (fields[key] + " " + line.strip()).strip()
    return fields


class AgentPluginManifestTest(unittest.TestCase):
    def test_plugin_json_is_closed_schema(self):
        data = _load("plugin.json")
        self.assertEqual(data["$schema"], _PLUGIN_SCHEMA)
        self.assertLessEqual(set(data), _ALLOWED_PLUGIN_KEYS)
        self.assertRegex(data["name"], _PLUGIN_NAME_RE)

    def test_plugin_json_matches_claude_manifest(self):
        portable = _load("plugin.json")
        claude = _load(".claude-plugin/plugin.json")
        for key in ("name", "version", "homepage", "repository", "license"):
            with self.subTest(key=key):
                self.assertEqual(portable[key], claude[key])

    def test_mcp_json_shape(self):
        mcp = _load("mcp.json")
        self.assertEqual(mcp["$schema"], _MCP_SCHEMA)
        self.assertEqual(
            mcp["$schema"].split("/")[-2], _load("plugin.json")["$schema"].split("/")[-2],
            "mcp.json and plugin.json must share the schema version",
        )
        self.assertTrue(mcp["mcpServers"])
        for server_id, cfg in mcp["mcpServers"].items():
            with self.subTest(server=server_id):
                self.assertEqual(cfg["type"], "stdio")
                self.assertRegex(cfg["command"], r"^[^\s$]+$")
                self.assertNotIn("PLUGIN_ROOT", cfg.get("env", {}))
                self.assertNotIn("PLUGIN_DATA", cfg.get("env", {}))
                for arg in cfg.get("args", []):
                    self.assertNotIn("CLAUDE_PLUGIN_ROOT", arg)
                    if arg.startswith("${PLUGIN_ROOT}/"):
                        target = os.path.join(_PLUGIN_DIR, arg[len("${PLUGIN_ROOT}/"):])
                        self.assertTrue(os.path.isfile(target), target)

    def test_mcp_json_matches_claude_mcp_json(self):
        portable = _load("mcp.json")["mcpServers"]
        claude = _load(".mcp.json")
        self.assertEqual(set(portable), set(claude))
        for server_id, cfg in claude.items():
            with self.subTest(server=server_id):
                self.assertEqual(portable[server_id]["command"], cfg["command"])
                expected = [a.replace("${CLAUDE_PLUGIN_ROOT}", "${PLUGIN_ROOT}") for a in cfg["args"]]
                self.assertEqual(portable[server_id]["args"], expected)


class NativeManifestTest(unittest.TestCase):
    def _assert_relative_paths_exist(self, manifest):
        for key in ("skills", "hooks", "mcpServers"):
            value = manifest.get(key)
            if not isinstance(value, str):
                continue
            with self.subTest(key=key):
                self.assertTrue(value.startswith("./"), value)
                self.assertNotIn("..", value)
                self.assertTrue(os.path.exists(os.path.join(_PLUGIN_DIR, value)), value)

    def test_codex_manifest(self):
        codex = _load(".codex-plugin/plugin.json")
        claude = _load(".claude-plugin/plugin.json")
        self.assertEqual(codex["name"], claude["name"])
        self.assertEqual(codex["version"], claude["version"])
        self.assertEqual(codex["hooks"], "./hooks/hooks.json")
        self.assertIn("displayName", codex["interface"])
        self.assertIn("shortDescription", codex["interface"])
        self._assert_relative_paths_exist(codex)

    def test_cursor_manifest(self):
        cursor = _load(".cursor-plugin/plugin.json")
        claude = _load(".claude-plugin/plugin.json")
        self.assertEqual(cursor["name"], claude["name"])
        self.assertEqual(cursor["version"], claude["version"])
        # Cursor's manifest validation rejects unknown fields such as displayName.
        self.assertNotIn("displayName", cursor)
        self._assert_relative_paths_exist(cursor)

    def test_cursor_hooks_json(self):
        hooks = _load("hooks/cursor-hooks.json")
        self.assertEqual(hooks["version"], 1)
        entries = hooks["hooks"]["preToolUse"]
        self.assertTrue(entries)
        for entry in entries:
            with self.subTest(command=entry["command"]):
                # Cursor has no plugin-root variable: relative paths only.
                self.assertTrue(entry["command"].startswith("./"))
                self.assertNotIn("..", entry["command"])
                self.assertTrue(os.path.isfile(os.path.join(_PLUGIN_DIR, entry["command"])))


class SkillsConformanceTest(unittest.TestCase):
    def test_every_skill_follows_agent_skills_spec(self):
        skills_dir = os.path.join(_PLUGIN_DIR, "skills")
        names = sorted(
            n for n in os.listdir(skills_dir) if os.path.isdir(os.path.join(skills_dir, n))
        )
        self.assertTrue(names)
        for name in names:
            path = os.path.join(skills_dir, name, "SKILL.md")
            with self.subTest(skill=name):
                self.assertTrue(os.path.isfile(path))
                meta = _skill_frontmatter(path)
                self.assertEqual(meta.get("name"), name)
                self.assertRegex(name, _SKILL_NAME_RE)
                description = meta.get("description", "")
                self.assertTrue(description)
                self.assertLessEqual(len(description), 1024)


class McpJsonLaunchTest(unittest.TestCase):
    """Start the server exactly as an Agent Plugins client would from mcp.json."""

    def test_initialize_and_list_tools(self):
        cfg = _load("mcp.json")["mcpServers"]["maven-mcp"]
        args = [a.replace("${PLUGIN_ROOT}", _PLUGIN_DIR) for a in cfg["args"]]
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                        "clientInfo": {"name": "agent-plugins-test", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        with tempfile.TemporaryDirectory() as data_dir:
            env = dict(os.environ)
            env["PLUGIN_ROOT"] = _PLUGIN_DIR
            env["PLUGIN_DATA"] = data_dir
            proc = subprocess.run(
                [cfg["command"], *args],
                input="".join(json.dumps(r) + "\n" for r in requests).encode(),
                capture_output=True,
                cwd=_PLUGIN_DIR,
                env=env,
                timeout=30,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        responses = {}
        for line in proc.stdout.decode().splitlines():
            if line.strip():
                msg = json.loads(line)
                responses[msg.get("id")] = msg
        self.assertEqual(responses[1]["result"]["serverInfo"]["name"], "maven-mcp")
        self.assertEqual(len(responses[2]["result"]["tools"]), 20)


if __name__ == "__main__":
    unittest.main()
