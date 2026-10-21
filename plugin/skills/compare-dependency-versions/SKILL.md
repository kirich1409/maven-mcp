---
name: compare-dependency-versions
description: >-
  Use when the user gives one or more dependencies together with their CURRENT version and
  asks "is my version of X behind latest", "how far behind is 3.0.0", "what upgrade type
  would this be — major, minor, or patch", "classify the version jump from A to B", or wants
  upgrade-type classification for specific coordinates outside of a full project scan.
disable-model-invocation: true
---

# Compare Dependency Versions

Return the pin and the candidate versions newer than it. The server does not choose one of them.

## Steps

1. Parse `groupId`, `artifactId`, `currentVersion` for each dependency from the user's
   message (or from context, e.g. a version noticed in a build file).

2. Call **`compare_dependency_versions`** with:
   - `dependencies`: `[{groupId, artifactId, currentVersion}, ...]`
   - optional `projectPath`

3. Present per-dependency: `currentVersion`, `currentLine`, and each candidate's
   `version`, `line`, `distance`, and `stability` when present. An omitted `stability`
   is not stable. `summary` is `total` only.

**A row with `error` was not compared.** Show the coordinate and the error. Do not call it "no upgrade". Say no upgrade only when every row has no `error` and empty `candidates`. Do not propose edits in that case.

Name one version only when `currentLine` is `release` and the candidates contain exactly one newer bare stable of that same major.minor and no other bare stable. Otherwise leave the list for the model to read. When `relocatedTo` is present, call `get_latest_version` for the new coordinate. `use` is that coordinate. Candidates on this row stay the old coordinate's versions.

## Constraints and non-goals

- Not for scanning a whole project's build files for outdated dependencies — use
  `/check-deps` (which also offers to apply the updates).
- Not for a version with no current baseline to compare against — use `/latest-version` or
  `/check-multiple-versions`.

## Fallback (MCP unavailable only)

Fetch `maven-metadata.xml` per artifact and list the same candidate rows the server would: newest bare stable of the pin's minor, of each newer minor, and of each higher major, plus the newest newer known prerelease of each numeric core. Skips project-declared private repos.
