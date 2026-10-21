# Tool contract

What each MCP tool returns. Roles, the decision record, version parsing, the candidate set, the naming rule, and judgment are in `docs/product-behavior.md`. This file does not repeat them.

The tool fetches and parses. It does not choose a pin, call a model, classify changelog prose, or edit the project.

The words MUST, MUST NOT, SHOULD, and MAY are normative. A missing field was not computed. It does not mean the value equals some other field.

## Reading a result

A missing field was not computed. `capabilityUnavailable`, `partial: true`, and `existenceStatus: unknown` mean the check stopped short. The model does not treat that result as clean, and does not treat an empty list beside those fields as "nothing found". `repositoryErrors` and `warnings`, when present, stay in the report. `resolvedFrom.viaPublicFallback: true` means the selected listing came from a public fallback.

## Version lookup

### `get_latest_version`

Goal: the latest bare stable of one coordinate. No current version is accepted.

Returns `groupId`, `artifactId`, `latestVersion`, `stability` (`stable`), and `allVersionsCount`. `latestVersion` is the newest bare stable. A preview, EAP, dev, or nightly build is not that version. When no bare stable exists, `latestVersion` is absent and `stability` is absent. The model does not treat a prerelease as the latest.

`stabilityFilter` is not an input. The tool does not return `candidates`.

### `check_multiple_dependencies`

Goal: the same latest-bare-stable lookup for many coordinates. A version is not accepted.

Returns `results[]`. Each result has `groupId`, `artifactId`, `latestVersion`, and `stability`, with the same absence rule as `get_latest_version`. One result's failure is `error` on that result. The others still return.

### `compare_dependency_versions`

Goal: compare each pinned version with the versions available to the project.

Input `currentVersion` is required. Returns `results[]` and does not return a rolled-up count of one chosen upgrade. Each result has `groupId`, `artifactId`, `currentVersion`, `currentLine`, `currentStability` (omitted when the pin's qualifier is unknown), and `candidates` as defined in `docs/product-behavior.md`. There is no single `latestVersion` and no single `upgradeType`. The skill applies the naming rule there. The model applies judgment only where that document says so. A failed lookup returns `error` on that result and omits `candidates`. Successful metadata with nothing newer than the pin returns `candidates: []` and no `error`.

### `check_version_exists`

Goal: whether one exact version is in a repository the project resolves.

Returns `groupId`, `artifactId`, `version`, and `existenceStatus`: `exists`, `absent`, or `unknown`. `exists: true` only when `existenceStatus` is `exists`. `unknown` means the lookup could not prove absence. The model does not treat `unknown` as a miss or as clean. `stability` describes that exact version when it was found. `relocatedTo` is present only when the POM of that version carries a relocation.

## Project facts

### `scan_project_dependencies`

Goal: the dependencies the project declares.

Returns `buildSystem`, `dependencies[]` (`groupId`, `artifactId`, `version`, where it is used), and `deadRepositoryHints`. A version here is the pin in the build, not a version the server suggests.

### `audit_project_dependencies`

Goal: one report on the dependencies the project already has. Available updates are part of that report. The vulnerability, license, and health facts are about the pinned version, not about a version the server picked.

Each dependency carries the pin (`currentVersion`, `currentLine`, `currentStability`) and the `candidates` set from `compare_dependency_versions` for that pin. It does not carry one `latestVersion` or one `upgradeType`. Vulnerability, license, and health fields describe `currentVersion`. They do not describe a candidate. The model queries a candidate's advisories only when it is about to name that candidate, through `get_dependency_vulnerabilities`. `onlyIssues` keeps a row when the pin has a vulnerability, a license signal, an error, or a non-empty `candidates` list. The summary still counts the full scan.

### `detect_dependency_conflicts`

Goal: the same coordinate already resolved at two or more versions inside this project.

Returns `conflicts[]` with the versions seen, `resolvedTo` under the build's own rule (Gradle highest-wins, Maven nearest-wins), and `risk`. `resolvedTo` is what the build would select. It is not an upgrade offer.

### `catalog_entry`

Goal: a Gradle version-catalog entry, or a list of catalog mistakes. No network.

`mode: generate` returns `alias`, `accessor`, `entry`, and `suggestedDiff`. `mode: validate` returns `violations[]`. The model applies `suggestedDiff` only through the catalog rules already in that result.

## A version the caller names

These tools do not choose the version. The model names it, then calls the tool. Vulnerability calls take the versions the model names. A changelog call takes the two ends the model names.

### `get_dependency_changes`

Goal: release notes between two versions the caller names.

Returns `fromVersion`, `toVersion`, and `changes[]`. `changes` lists every published version from `fromVersion` exclusive to `toVersion` inclusive. Each item has `version`, `body` when the notes have a section for it, and `releaseUrl` when that section has one. A version with no section is still listed, without `body`. The body is text. The server does not search that text for a bug and does not classify it as a migration or a mechanical bump. The model reads the text.

### `get_dependency_vulnerabilities`

Goal: known advisories for the pinned versions the caller names. A coordinate without a version is not queried.

Returns `results[]` with `version`, `vulnerabilities[]`, and `vulnerabilityCount`. An empty list means OSV returned no advisory. It does not mean the coordinate is safe. When at least one advisory exists, `safeUpgrade` is the highest fixed version already present in those advisories, with `fixesAllKnown`. The model verifies it. The server does not pick it from the catalog. `capabilityUnavailable` means the check did not run.

### `get_vulnerability_paths`

Goal: the path from one root GAV to each vulnerable transitive the POM graph can see.

Returns `vulnerabilityPaths[]` (`vulnerableNode`, `path`, `vulnerabilities`). An empty list means none were found on the walked graph. `partial: true` means the walk or the advisory check stopped short.

### `get_transitive_graph`

Goal: the POM graph of one GAV.

Returns `nodes[]`, `edges[]`, `partial`, and `truncated`. This is not the project's Gradle resolve.

### `expand_bom`

Goal: managed versions inside one BOM.

Returns `managed[]` of `groupId`, `artifactId`, `version`. Import BOMs are expanded first-wins. A missing POM entry is skipped.

### `compare_upgrade_closure`

Goal: what changes in the resolved graph if the caller applies the named `fromVersion` → `toVersion` pairs.

Returns the closure diff for those pairs. The model chooses `toVersion` before this call. The result is not a safety verdict.

### `get_aar_sdk_requirements`

Goal: SDK floors of one AAR version the caller names, compared with the application's literal `compileSdk` and `minSdk` when those are present.

Returns the floors it could read. A missing AAR or a missing floor omits that field and does not raise. The model decides whether a floor blocks the pin.

### `check_version_compatibility`

Goal: whether the toolchain and dependency versions the caller passed contradict a shipped matrix or a Spring Boot BOM.

Returns `compatible`, `conflicts[]` (`kind`, `requested`, `expected`, `suggestion`, `reference`), and `notes[]`. `suggestion` is the matrix or BOM value. The model decides whether to apply it. An unknown AGP or Kotlin line is a note, not a conflict.

### `get_eol_status`

Goal: published end-of-life facts for the Kotlin, Gradle, Spring Boot, or JDK version the caller names. JDK requires a vendor.

Returns one item per requested product: `cycle`, `isEol`, `eolDate`, `isMaintained`, `isLts`, `latestInCycle`, or `error`, or `capabilityUnavailable`. The model decides what an end-of-life date means for the project.

### `check_license_compliance`

Goal: SPDX licenses on the POM graph of the roots the caller names, judged against the passed posture.

Returns `results[]` with `verdict` `ok`, `review`, or `violation`, plus `reason`. A missing license is `review`. The model does not treat the verdict as legal advice.

## Facts about a coordinate

### `get_dependency_health`

Goal: maintenance signals for the named coordinates. A passed `version` is the version inspected. When `version` is omitted, the newest bare stable is inspected so there is a POM to read. That choice is not an upgrade offer.

Returns `versionCount`, `latestVersion`, `stability`, `signals[]`, GitHub activity when a repository is known, and `scorecard` when deps.dev has one. `latestVersion` here is the inspected version's newest bare stable, the same value as `get_latest_version`. The model does not use it as the pin to write when the user asked about a different version.

### `get_dependency_license`

Goal: the license of the named coordinates. A passed `version` is the POM read. When `version` is omitted, the newest bare stable is read. That choice is not an upgrade offer.

Returns `spdxId`, `name`, `category`, `notes`, and `source` (`pom`, `github`, or `spdx-normalized`). `category` `unknown` means the license was not identified.

### `search_artifacts`

Goal: coordinates matching a keyword or a `group:artifact` query.

Returns `results[]` of `groupId`, `artifactId`, `latestVersion`, and `versionCount`. `latestVersion` is the newest bare stable of that hit, for display. The model opens `get_latest_version` or `compare_dependency_versions` before treating it as a pin. `capabilityUnavailable` means the search did not run. An empty `results` with that field is not "nothing matches".

### `verify_coordinates`

Goal: whether a coordinate exists, and whether an absent name is close to a real one.

Returns `existenceStatus` `exists`, `absent`, or `unknown`. `likelyHallucination: true` only for `absent` with a close candidate. `suggestions[]` are candidates to verify, not replacements. `typosquatRisk` on an existing coordinate is a heuristic, not a verdict. `unknown` is not clean. `stability`, when present, is the class of the newest bare stable and is not a version to pin.
