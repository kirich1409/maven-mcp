---
name: dependency-changes
description: Show what changed between two versions of a Maven/Gradle dependency — fetches release notes, changelog entries, and GitHub releases for the version range. Use when the user says "what changed", "show changelog", "release notes", "what's new in version X", "what broke in the upgrade", "before I update show me", or provides two versions of a dependency and wants to know what's different. Also trigger when the user has just seen dependency update suggestions (from check-deps) and wants to review changes before committing to an upgrade.
---

# Dependency Changes

Show what changed between two versions of a Maven artifact by fetching release notes and
changelog data.

## Preferred — MCP

1. Parse `groupId`, `artifactId`, `fromVersion`, `toVersion` from the user or conversation
   (e.g. a prior check-deps table). If `fromVersion` is missing, ask or use the current
   version from context. If `fromVersion` equals `toVersion`, say there is nothing to show.

2. Call **`get_dependency_changes`** with:
   - `groupId`, `artifactId`
   - `fromVersion` (exclusive)
   - `toVersion` (inclusive)
   - optional `projectPath`

   Provider order: AndroidX developer docs (for `androidx.*`), AGP release notes
   (for `com.android.tools.build`), then GitHub releases. `GITHUB_TOKEN` is used
   server-side for the GitHub path when present — never pass the token in headers
   yourself.

3. Present the tool result: versions in range, release notes / bodies, links. If notes are
   sparse, still show the version list plus docs / GitHub / Maven Central URLs from the
   response. Collapse the middle when many versions are returned.

## Context: used after check-deps

When the user asks "show me what changed for X" after a candidate table, pull coordinates
and the pin from that table. The caller names both ends. The tool lists every published
version in the span.

## Finding a fix

When the user asks whether a newer version already fixes a bug, call
`compare_dependency_versions` for the pin, then one `get_dependency_changes` from the pin
to the newest candidate. Read the bodies. Name the version whose text mentions the bug,
including an alpha. No mention means the notes do not show the fix. Do not invent a
version and do not replace that answer with the naming rule in `/check-deps`.

When compare returns `relocatedTo`, call `get_latest_version` for the new coordinate
before reading notes for that line. `use` is the new coordinate.

## Error handling

- **No docs page / no GitHub repo / no notes** — say so and provide the Maven Central
  (or Google Maven) artifact URL.
- **Rate limited** — note the limit; retry via `get_dependency_changes` (server-side token)
  rather than attaching a token to a client request.
- **Artifact not found** — suggest checking spelling; do not invent changelog text.

## Fallback (MCP unavailable only)

If the tool cannot be called: fetch the POM for SCM, public `maven-metadata.xml` for the
version list, then unauthenticated GitHub `/releases` (and optionally raw CHANGELOG.md).
Do **not** add `Authorization` / `GITHUB_TOKEN` on the client. State that project-private
repos are skipped. If rate-limited, say so and stop.
