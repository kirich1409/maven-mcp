# maven-mcp

Agent plugin for Claude Code, Grok Build, Cursor, and Codex that provides Maven dependency intelligence via an MCP server — query artifact versions, scan projects for outdated dependencies, check for vulnerabilities, and fetch changelogs.

## How it works

The plugin bundles a single-file Python 3 MCP server (`plugin/server/server.py`) that speaks MCP over stdio (JSON-RPC 2.0 on stdin/stdout) or over a stateless Streamable HTTP endpoint. It uses the Python standard library only — zero pip dependencies. The plugin registers the server via `.mcp.json` (Claude Code, Grok Build) and `mcp.json` (Cursor, Codex), both `command: python3`, so it installs with no extra runtime setup. The server can also be run standalone and connected to any MCP-compatible agent — see [Use with any MCP client](#use-with-any-mcp-client).

Version lookups use the repositories the build file declares. Maven Central, Google Maven, and the Gradle Plugin Portal are used when that scope declares none. Private repositories need credentials — see [Configuration](docs/configuration.md).

**Gradle scanning** runs the project's wrapper once and reads production runtime classpaths (`*RuntimeClasspath`), then merges declared provenance from build files and version catalogs. **Maven scanning** reads `pom.xml` locally.

### Tools

| Tool | Description |
|------|-------------|
| `get_latest_version` | Find latest version of an artifact with stability-aware selection |
| `check_version_exists` | Verify if a specific version exists and classify its stability |
| `check_multiple_dependencies` | Bulk lookup of latest versions for multiple dependencies |
| `compare_dependency_versions` | Compare current versions against latest (major/minor/patch) |
| `get_dependency_changes` | Show changes between versions (AndroidX docs, then AGP docs, then GitHub releases; `CHANGELOG.md` on the default branch when no release body is usable) |
| `scan_project_dependencies` | Scan Gradle/Maven build files and Gradle version catalogs (`gradle/libs.versions.toml`) for dependencies |
| `expand_bom` | Expand a Maven BOM into managed dependency versions |
| `get_transitive_graph` | Resolved transitive dependency graph for a GAV via deps.dev |
| `get_vulnerability_paths` | Shortest dependency path from a project root GAV to each transitively vulnerable node (deps.dev graph + OSV.dev) |
| `detect_dependency_conflicts` | Flag GAs resolved at multiple versions (Gradle: from resolved scan usages; Maven: deps.dev per-root graphs with nearest-wins) |
| `check_version_compatibility` | Check Spring Boot / AGP / Kotlin / javax→jakarta compatibility |
| `get_dependency_vulnerabilities` | Check for known CVEs via OSV.dev |
| `get_dependency_health` | Assess adoption-worthiness: version/stability, GitHub activity, issue dynamics, license, owner — raw signals for a verdict |
| `get_dependency_license` | SPDX / category license intelligence for direct dependencies |
| `check_license_compliance` | Aggregate transitive licenses via deps.dev; flag copyleft/risky vs project policy |
| `search_artifacts` | Search artifacts (Maven Central Solr; Nexus/Artifactory in closed mode) |
| `audit_project_dependencies` | Full audit: scan + version compare + vulnerability check |
| `catalog_entry` | Generate/validate Gradle version-catalog entries (`libs.versions.toml`) with rule-correct aliases and minimal diffs |
| `verify_coordinates` | Tri-state existence check + did-you-mean for hallucinated coordinates |
| `get_eol_status` | End-of-life / support status for JDK (vendor-specific), Kotlin, Gradle, and Spring Boot via endoflife.date |

### Skills

Claude Code keeps a listing of every installed skill's name and description in context, on a
budget of ~1% of the model's context window; when the listing overflows, descriptions get
dropped. Twenty-one entries from one plugin consume that budget on their own, so only the
skills whose body adds a workflow beyond a single tool call stay model-routed. The rest are
manual: the slash command and the underlying MCP tool are unchanged, Claude just no longer
carries their descriptions in every session.

**Model-routed** — Claude picks these up on its own, and you can also invoke them by name:

| Skill | Description |
|-------|-------------|
| `/latest-version <groupId:artifactId>` | Find latest version of a Maven artifact |
| `/check-deps` | Scan project for outdated dependencies and update them |
| `/check-deps-vulnerabilities` | Scan project dependencies for known CVEs/GHSA via OSV (includes Gradle/Maven submodules) |
| `/audit-project-dependencies` | One combined report: updates + vulnerabilities + optional license posture |
| `/check-version-compatibility` | Validate AGP/Gradle/JDK/Kotlin and Spring Boot BOM/javax→jakarta compatibility |
| `/dependency-changes` | Show release notes/changelog between two versions of a Maven/Gradle dependency |
| `/dependency-health` | Assess whether a Maven dependency is worth adopting (maintenance, activity, license, owner) |
| `/catalog-entry` | Generate or validate a Gradle version-catalog (`libs.versions.toml`) entry |

**Manual only** (`disable-model-invocation: true`) — invoke by name; Claude reaches the same
capability through the MCP tool above:

| Skill | Description |
|-------|-------------|
| `/check-version-exists` | Confirm whether one specific, already-known version exists |
| `/check-multiple-versions` | Batch latest-version lookup for several artifacts being evaluated |
| `/compare-dependency-versions` | Compare specific current versions against latest and classify the upgrade type |
| `/scan-project-dependencies` | Raw inventory of a project's declared dependencies (no freshness/CVE check) |
| `/expand-bom` | Expand a Maven BOM/platform into its managed dependency versions |
| `/transitive-graph` | Resolved transitive dependency graph for a single GAV |
| `/vulnerability-paths` | Trace each transitively vulnerable dependency back to the project root |
| `/dependency-conflicts` | Flag GAs resolved at multiple versions across a project |
| `/dependency-vulnerabilities` | Check specific named coordinates for known CVEs/GHSA, outside a project scan |
| `/dependency-license` | SPDX/category license intelligence for specific dependencies |
| `/license-compliance` | Aggregate transitive licenses vs a project license policy; flag copyleft/violations |
| `/search-artifacts` | Search Maven Central (or Nexus/Artifactory in closed mode) by keyword |
| `/eol-status` | Check end-of-life / support status for JDK, Kotlin, Gradle, or Spring Boot |

### Supported build systems

- **Gradle** — `build.gradle`, `build.gradle.kts`, `settings.gradle`, `settings.gradle.kts`
- **Maven** — `pom.xml`
- **Version catalogs** — `gradle/libs.versions.toml`

## Requirements

- **Python 3.9+** — the server uses the standard library only; no pip dependencies.
- **jq** and **timeout** / **gtimeout** — used by the write-time hooks. On macOS, `timeout` comes from `brew install coreutils` (`gtimeout`). Without them the hooks do nothing and the edit proceeds. The MCP server itself does not need either.

## Configuration

| Variable | Default | Effect |
|----------|---------|--------|
| `GITHUB_TOKEN` | unset | GitHub API limit 60 → 5000 requests/hour for changelogs and health |
| `MAVEN_MCP_OFFLINE` | off | Skip public Maven, Google, Plugin Portal, and enrichment APIs |
| `MAVEN_MCP_CACHE_DISABLE` | off | Skip the on-disk response cache |
| `MAVEN_MCP_TRANSPORT` | `stdio` | `http` serves `POST /mcp` |

Cache location, private-repo credentials, mirrors, TLS, and the rest of the variables: [docs/configuration.md](docs/configuration.md).

## Installation

### Claude Code (marketplace)

```
/plugin marketplace add kirich1409/maven-mcp
/plugin install maven-mcp@maven-mcp
```

### Grok Build (marketplace)

```bash
grok plugin marketplace add kirich1409/maven-mcp
grok plugin install maven-mcp@maven-mcp --trust
```

`--trust` is required for the bundled MCP server and write-guard hooks to run. Reload plugins (`r` in the Plugins tab) or start a new session after install.

### Cursor and Codex (`npx plugins`)

```bash
npx plugins add kirich1409/maven-mcp
```

The [`plugins`](https://www.npmjs.com/package/plugins) CLI detects installed agents and installs into each of them. `plugin/` ships three manifests over the same `skills/`, server, and hook scripts:

| Manifest | Read by | Skills + MCP server | Write-time guard |
|---|---|---|---|
| `.claude-plugin/plugin.json` + `.mcp.json` + `hooks/hooks.json` | Claude Code, Grok Build | yes | blocks (`deny`) |
| `.codex-plugin/plugin.json` + `mcp.json` (hooks from `hooks/hooks.json`) | Codex | yes | runs, but does not block: Codex applies an `apply_patch` write even after `deny` and may not show the reason ([openai/codex#27833](https://github.com/openai/codex/issues/27833)) |
| `.cursor-plugin/plugin.json` + `mcp.json` + `hooks/cursor-hooks.json` | Cursor | yes | `preToolUse` reply with `permission` |

There is deliberately no root `plugin.json` ([Agent Plugins 1.0](https://github.com/agentplugins/agent-plugins-spec) manifest). With one present, Codex loads the package through its Agent Plugins loader, ignores `.codex-plugin/plugin.json`, and silently disables every hook ([openai/codex#39895](https://github.com/openai/codex/issues/39895)). It comes back once that is fixed; `mcp.json` already uses the Agent Plugins shape.

`python3` (3.9+) must be on `PATH`, same as for the Claude Code plugin.

### Local path (development)

```bash
# Claude Code
claude plugin marketplace add /path/to/maven-mcp
claude plugin install maven-mcp@maven-mcp

# Grok Build
grok plugin marketplace add /path/to/maven-mcp
grok plugin install maven-mcp@maven-mcp --trust
```

The plugin registers the bundled server via `.mcp.json` automatically; no separate install or build step is required.

npm, Homebrew, and an MCPB bundle are not install channels. Non-plugin clients use `uv` (`uvx maven-mcp`, or `maven-mcp` after `uv tool install maven-mcp`). `uv` downloads Python 3.9+; it is not preinstalled by local Claude Code, Codex, or Grok. Claude Code cloud VMs already have Python and `uv`. Web ChatGPT cannot spawn a local process and is HTTP-only (see below).

## Use with any MCP client

Codex, Cursor, Claude Desktop, Gemini CLI, and Kimi run the published console script. The command is `uvx maven-mcp` (distribution name `maven-mcp`).

- **Kimi Code** — `~/.kimi-code/mcp.json` (user-level) or `.kimi-code/mcp.json` (project-level):

  ```json
  {
    "mcpServers": {
      "maven-mcp": {
        "command": "uvx",
        "args": ["maven-mcp"]
      }
    }
  }
  ```

- **Cursor** — `~/.cursor/mcp.json`, same `mcpServers` shape as above.
- **Claude Desktop** — `claude_desktop_config.json`, same `mcpServers` shape as above.
- **Gemini CLI** — `~/.gemini/settings.json`:

  ```json
  {
    "mcpServers": {
      "maven-mcp": {
        "command": "uvx",
        "args": ["maven-mcp"]
      }
    }
  }
  ```

- **Codex** — `~/.codex/config.toml` (Codex Desktop may ignore a project `.codex/config.toml`; the user-level file is the one these steps use):

  ```toml
  [mcp_servers.maven-mcp]
  command = "uvx"
  args = ["maven-mcp"]
  ```

Environment variables (`GITHUB_TOKEN`, `MAVEN_MCP_OFFLINE`, …) can be passed through each client's `env` field. Plugin installs keep `python3` and `${CLAUDE_PLUGIN_ROOT}/server/server.py` in `.mcp.json`.

### HTTP mode (remote / cloud agents)

For agents that cannot spawn a local process (cloud sandboxes, remote workspaces), the server also speaks stateless Streamable HTTP. Start it once:

```bash
MAVEN_MCP_TRANSPORT=http MAVEN_MCP_HTTP_HOST=127.0.0.1 MAVEN_MCP_HTTP_PORT=8765 \
  uvx maven-mcp
```

The MCP endpoint is `http://<host>:<port>/mcp` (single `POST` endpoint, JSON responses, no SSE). Connect with a URL-based entry instead of `command`:

- **Kimi Code** (`mcp.json`): `{"mcpServers": {"maven-mcp": {"url": "http://127.0.0.1:8765/mcp"}}}`
- **Gemini CLI** (`settings.json`): `{"mcpServers": {"maven-mcp": {"httpUrl": "http://127.0.0.1:8765/mcp"}}}`
- **Codex** (`config.toml`): `[mcp_servers.maven-mcp]` with `url = "http://127.0.0.1:8765/mcp"`

`MAVEN_MCP_HTTP_HOST` defaults to `127.0.0.1` and `MAVEN_MCP_HTTP_PORT` to `8765`. The HTTP transport has **no authentication** — bind it to localhost or a trusted network only; for exposure to cloud agents over the internet, put it behind a reverse proxy that terminates TLS and enforces auth.

## Hooks

`pre-edit-deps.sh` runs before an edit to a Gradle, Maven, or version-catalog file. It can block a coordinate that looks hallucinated or is flagged malicious, and it can ask on a critical or high CVE, a typosquat-shaped package, or a toolchain mismatch. `post-edit-deps.sh` reminds you to run `/check-deps`. Both fail open: a missing `jq`, `timeout`/`gtimeout`, or a server error lets the edit through. Codex still applies `apply_patch` after a deny ([openai/codex#27833](https://github.com/openai/codex/issues/27833)).

## Development

```bash
python3 -m unittest discover -s tests
python3 scripts/check-versions.py
```

The implementation contract for coding agents is [AGENTS.md](AGENTS.md).

## License

MIT. See [LICENSE](LICENSE).
