---
name: check-version-exists
description: >-
  Use when the user asks "does version X exist", "is version 3.2.0 released for okhttp",
  "check if this version is published", "was this version ever released", "check version
  exists", or provides a full groupId:artifactId:version and wants to confirm that exact
  version is available in a Maven repository — not what the latest is (use /latest-version
  for that).
disable-model-invocation: true
---

# Check Version Exists

Confirm whether one specific, already-known version of a Maven artifact exists in any
resolvable repository.

## Steps

1. Parse `groupId`, `artifactId`, `version` from the user's input (e.g.
   `com.squareup.okhttp3:okhttp:4.12.0`, or a groupId:artifactId plus a version mentioned in
   conversation).

2. Call **`check_version_exists`** with:
   - `groupId`, `artifactId`, `version`
   - optional `projectPath` when the user is inside a project (uses declared repos)

3. Present the result:
   - `exists: true` — show `stability` and `repository` (which repo answered), and
     `relocatedTo` when present (the artifact was relocated via a Maven
     `<distributionManagement>` POM — tell the user the new coordinates).
   - `exists: false` means the version was not in the metadata. Do not guess a
     cause. Offer `/latest-version` to check what *is* available, or
     `/search-artifacts` if the artifact name itself may be wrong. Keep those
     offers on this branch only.
   - `existenceStatus` unknown means the check did not run (`exists` is omitted).
     Report the `error` field. Do not say the version was not found, and do not
     offer another tool as if this check had found it missing.

## Error handling

A tools/call `isError` is a tool failure and is reported as-is. Do not invent a
version or existence status from memory. `existenceStatus` unknown is a successful
result, not that failure, and not "not found".

## Constraints and non-goals

- Not for "what's the latest version of X" — use `/latest-version`.
- Not for "is my current version behind latest" — use `/compare-dependency-versions`.
- Not for checking many artifacts at once — use `/check-multiple-versions`.

## Fallback (MCP unavailable only)

Fetch `maven-metadata.xml` from public repos (Maven Central → Google Maven → Gradle Plugin
Portal) and check whether `version` is present in the `<versions>` list. A failed
metadata fetch means the check did not run, not that the version was not found.
Note that this skips project-declared private repos and relocation detection.
