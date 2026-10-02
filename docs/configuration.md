# Configuration

Environment variables and files the server reads. Defaults are enough for a project that uses Maven Central, Google Maven, and the Gradle Plugin Portal. Install steps are in the [README](../README.md).

Boolean toggles accept `1`, `true`, `on`, and `yes` (any case). Any other value leaves the toggle off.

## GitHub

`GITHUB_TOKEN` raises the GitHub API limit from 60 to 5000 requests per hour. Changelogs (`get_dependency_changes`) and health (`get_dependency_health`, including issue search) use it. The write-time hook removes this variable from the environment it passes to the server.

## Cache

Responses are stored under `${XDG_CACHE_HOME}/maven-central-mcp`, or `~/.cache/maven-central-mcp` when `XDG_CACHE_HOME` is unset.

| What | How long |
|------|----------|
| Maven metadata (version lists) | 1 hour |
| POM files | 7 days |
| Search results | 1 hour |
| deps.dev graphs, licenses, and OpenSSF Scorecard | 1 hour |
| AndroidX and AGP release-note pages | 7 days |
| endoflife.date product documents | 7 days |
| A definitive HTTP 404 | 5 minutes |

OSV vulnerability queries, GitHub API calls, and any response that used a private-repo credential are not stored. HTTP 429, 5xx, and transport errors are not stored either.

`check_version_exists` can lag a publish by up to an hour because it reads the metadata cache. The existence probe and the did-you-mean lookup inside `verify_coordinates` stay live on every call. Two extra typosquat heuristics (a near-identical artifact under another group, and a recently published first version) reuse the 1-hour search cache.

`MAVEN_MCP_CACHE_DISABLE=1` turns the disk cache off. One audit run still deduplicates lookups in memory.

## Gradle scan timeout

`MAVEN_MCP_GRADLE_TIMEOUT` is a positive number of seconds. The default is `300`. One `gradlew` invocation resolves every module, so a large multi-module build may need more. A missing, non-integer, or non-positive value keeps the default.

## Which repositories are queried

Version lookups use the repositories declared in the project. Maven Central, Google Maven, and the Gradle Plugin Portal are used when that scope (dependencies or plugins) declares none.

`MAVEN_MCP_PUBLIC_FALLBACK` (default off) also queries those public repositories when the project already declares its own. Turn it on when versions come from a repository the build file does not name. `resolvedFrom.viaPublicFallback` is true when only a public fallback answered.

`mavenLocal()` is recognized and is not queried over HTTP. A repository URL that still contains `${...}` is used as written.

## Closed network

`MAVEN_MCP_OFFLINE` stops contact with `repo1.maven.org`, `dl.google.com`, and `plugins.gradle.org`. Repositories declared in the build are still queried. Public shorthands (`mavenCentral()`, `google()`, `gradlePluginPortal()`) are dropped unless a rewrite below points them at an internal base.

`MAVEN_MCP_REPOSITORY_BASE` replaces those three public repository URLs with one internal base (a Nexus or Artifactory group URL). Combined with offline mode, this is the closed-contour setup.

Maven mirrors are read from `MAVEN_MCP_SETTINGS` when set, otherwise `~/.m2/settings.xml`, otherwise `$M2_HOME/conf/settings.xml` or `$MAVEN_HOME/conf/settings.xml`. A `<mirror>` whose `mirrorOf` is `*`, `external:*`, `external:http:*`, a comma-separated list of ids, or a `!id` exclusion rewrites matching repository URLs. The mirror's `<id>` is the name used for credentials.

When no `settings.xml` mirror matches, and `~/.gradle/init.gradle`, `init.gradle.kts`, or one script under `init.d/` names exactly one non-public Maven URL next to a well-known shorthand, that URL is treated as a mirror of everything. A script that names more than one URL is ignored.

Order: repositories declared in the build, then the public fallback when it applies, then `MAVEN_MCP_REPOSITORY_BASE`, then mirrors, then the offline drop of anything still pointing at a public host.

Tools that call OSV, GitHub, deps.dev, developer.android.com, or endoflife.date return `capabilityUnavailable: "offline"` immediately in offline mode, so an empty CVE, health, changelog, graph, or end-of-life result is an unavailable check. A transport failure against those hosts returns `capabilityUnavailable: "unreachable"` after a short timeout. Point these variables at an internal mirror to keep the feature:

| Variable | Points at |
|----------|-----------|
| `MAVEN_MCP_OSV_BASE` | OSV API |
| `MAVEN_MCP_GITHUB_BASE` | GitHub Enterprise root, usually ending in `/api/v3` |
| `MAVEN_MCP_DEPSDEV_BASE` | deps.dev |
| `MAVEN_MCP_ANDROID_DOCS_BASE` | Android developer docs |
| `MAVEN_MCP_ENDOFLIFE_BASE` | endoflife.date API (default `https://endoflife.date/api/v1`) |

An override whose host differs from the public default still runs when offline mode is on.

## Search

`search_artifacts` uses Maven Central outside closed mode. When offline mode, `MAVEN_MCP_REPOSITORY_BASE`, or a mirror is active, search goes to that base: Nexus 3 (`GET /service/rest/v1/search`) or Artifactory (GAVC for `group:artifact` coordinates, AQL for keywords). The manager is detected from the URL and the response. Set the tool argument `repositoryType`, or `MAVEN_MCP_REPOSITORY_TYPE`, to `auto` (default), `nexus`, `artifactory`, or `central`. An unknown manager returns an empty list and `searchBackendUnavailable`.

A Maven Central search that stays rate-limited reports `capabilityUnavailable: "rate_limited"` (HTTP 429). HTTP 403 reports `"blocked"`. Other failures report `"unreachable"`.

## Private repository credentials

Credentials are never read from the build file. For each repository the server tries, in order:

1. Environment: `MAVEN_REPO_<ID>_USER` and `MAVEN_REPO_<ID>_PASSWORD` (HTTP Basic), or `MAVEN_REPO_<ID>_TOKEN` alone (Bearer), or `USER` together with `TOKEN` (Basic, token as the password — GitHub Packages and Artifactory PATs).
2. A `<server>` in `~/.m2/settings.xml` whose `<id>` matches.
3. `~/.gradle/gradle.properties` keys `{id}Username` / `{id}Password` or `{id}Token`.

`<ID>` is the Maven `<id>` or the Gradle `name`, otherwise the repository hostname. Non-alphanumeric characters become `_` and the name is uppercased (`nexus.example.com` → `NEXUS_EXAMPLE_COM`).

A hostname-keyed secret applies directly: the host comes from the URL the request is sent to. A name-keyed secret applies only when `MAVEN_REPO_<ID>_HOST` equals that hostname. Without the pin the secret is skipped. This stops a build file from naming an `<id>` that belongs to another host and receiving that host's password (GHSA-m2hv-xh72-cccw). A repository rewritten by a `settings.xml` mirror is the exception: the mirror `<id>` comes from the same settings file as the URL, so it does not need `MAVEN_REPO_<ID>_HOST`.

A repository that answers 401 or 403 without a credential surfaces as `auth required for <repo>`. Secrets are not printed in tool output.

## TLS and HTTP proxy

`MAVEN_MCP_CA_CERT`, or `SSL_CERT_FILE`, or `NODE_EXTRA_CA_CERTS`, adds a CA bundle. Certificate verification stays on.

`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` (and the lowercase names) and `NO_PROXY` / `no_proxy` apply to every outbound call.

`MAVEN_MCP_INSECURE_TLS=1` turns verification off and logs a one-time warning. Prefer the CA bundle.

## HTTP transport

`MAVEN_MCP_TRANSPORT=http` serves `POST /mcp` (JSON responses, no sessions, no SSE). `MAVEN_MCP_HTTP_HOST` defaults to `127.0.0.1`. `MAVEN_MCP_HTTP_PORT` defaults to `8765`. There is no authentication. Bind to localhost or a trusted network. A non-loopback bind logs a warning.
