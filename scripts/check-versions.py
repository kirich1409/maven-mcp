#!/usr/bin/env python3
"""Fail when the shipped version locations disagree, or an old repo URL remains."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_URL = "https://github.com/kirich1409/maven-mcp"
OLD_SLUG = "krozov-" + "ai-tools"

# Manifests of the other plugin formats shipped from plugin/ (Agent Plugins,
# Codex, Cursor); each must carry the same version and repository URL.
EXTRA_MANIFESTS = (
    "plugin/plugin.json",
    "plugin/.codex-plugin/plugin.json",
    "plugin/.cursor-plugin/plugin.json",
)

TRACKED = (
    "README.md",
    "CLAUDE.md",
    "AGENTS.md",
    "plugin/.claude-plugin/plugin.json",
    "plugin/plugin.json",
    "plugin/.codex-plugin/plugin.json",
    "plugin/.cursor-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    "docs/plans/maven-mcp-1-0/plan.md",
)


def main() -> int:
    plugin = json.loads((ROOT / "plugin/.claude-plugin/plugin.json").read_text())
    market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    entry = next(item for item in market["plugins"] if item["name"] == "maven-mcp")
    server = (ROOT / "plugin/server/server.py").read_text()
    server_version = re.search(r'^SERVER_VERSION = "([^"]+)"', server, re.M)
    user_agent = re.search(r'^USER_AGENT = "maven-mcp/([^"]+)"', server, re.M)
    project_text = (ROOT / "pyproject.toml").read_text()
    project_block = re.search(r"(?m)^\[project\]\s*$([\s\S]*?)(?=^\[|\Z)", project_text)
    project_version = None
    if project_block:
        found = re.search(r'(?m)^version\s*=\s*"([^"]+)"', project_block.group(1))
        if found:
            project_version = found.group(1)
    errors: list[str] = []

    version = plugin["version"]
    if project_version != version:
        errors.append(f"pyproject.toml version {project_version} != plugin.json {version}")
    if entry["version"] != version:
        errors.append(
            f"marketplace.json {entry['version']} != plugin.json {version}"
        )
    if server_version is None or server_version.group(1) != version:
        errors.append(f"SERVER_VERSION does not equal plugin.json {version}")
    if user_agent is None or user_agent.group(1) != version:
        errors.append(f"USER_AGENT does not equal maven-mcp/{version}")
    if plugin.get("homepage") != REPO_URL or plugin.get("repository") != REPO_URL:
        errors.append(f"plugin.json homepage/repository must be {REPO_URL}")
    for rel in EXTRA_MANIFESTS:
        manifest = json.loads((ROOT / rel).read_text())
        if manifest.get("version") != version:
            errors.append(f"{rel} version {manifest.get('version')} != plugin.json {version}")
        if manifest.get("homepage") != REPO_URL or manifest.get("repository") != REPO_URL:
            errors.append(f"{rel} homepage/repository must be {REPO_URL}")
        if manifest.get("name") != plugin["name"]:
            errors.append(f"{rel} name must be {plugin['name']}")
    if entry.get("homepage") != REPO_URL:
        errors.append(f"marketplace homepage must be {REPO_URL}")
    if market.get("name") != "maven-mcp":
        errors.append("marketplace name must be maven-mcp")
    if entry.get("source") != "./plugin":
        errors.append("marketplace source must be ./plugin")

    for rel in TRACKED:
        text = (ROOT / rel).read_text()
        if OLD_SLUG in text:
            errors.append(f"{rel} still names the previous repository")

    if len(sys.argv) == 2 and sys.argv[1] != version:
        errors.append(f"requested version {sys.argv[1]} != {version}")
    elif len(sys.argv) > 2:
        errors.append("usage: scripts/check-versions.py [X.Y.Z]")

    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"versions match {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
