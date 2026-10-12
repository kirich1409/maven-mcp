# Product behavior

Base spec for how maven-mcp answers questions about Maven and Gradle dependencies.

This document is the target. `AGENTS.md` describes the current implementation. Where they differ, this spec is the target.

The words MUST, MUST NOT, SHOULD, and MAY are normative. A missing field means the value was not computed. It does not mean the value equals some other field.

The policy id for the advised rules in this document is `release-line`. An advised answer MUST include `policy: "release-line"`.

The server does not edit the project. An agent applies a version edit, a source edit, or a rollback, and only under the rules below.

## Authorities

A version answer promises exactly one authority:

| Authority | Promise |
| --- | --- |
| `published` | Maven `maven-metadata` tags from one queried repository. Tags are not merged across repositories. |
| `resolved` | What this project's Gradle or Maven build selected. |
| `advised` | This server's recommendation under `release-line`, over the union of version lists. A recommendation, not a claim about one repository or about the build. |

Every version answer MUST include `authority` with one of those three values.

Coordinate existence is not a version authority. It is a status: `exists`, `absent`, or `unknown`.

Other authorities MAY appear beside the primary one. The caller MUST be able to tell which value is primary.

## Default authority

When the caller does not pass `authority`, the primary value follows the question.

| Question | Primary | Beside it |
| --- | --- | --- |
| Catalog search | No version authority. Hits carry `indexLatest` until metadata is read. | `published` and `advised` after metadata for the chosen coordinate has been read. |
| Which version should I take (latest, compare, audit upgrade) | `advised` | `published` once repository metadata has been read. `resolved` only after a project resolve. |
| What the project already uses, including a scan | `resolved` | |
| Does this coordinate exist | status `exists` \| `absent` \| `unknown` | No version authority. |

## Existence

Every tool that answers whether a coordinate exists uses the same status.

| Status | Rule |
| --- | --- |
| `exists` | At least one queried repository returned the version. A declared proxy's 404 does not cancel a version the trusted public repository has already returned. |
| `absent` | Every repository that was queried, including the trusted public repository, returned a definitive not-found. |
| `unknown` | The check could not be completed. |

`unknown` covers an empty repository list after group filters, authentication failure, throttling, HTTP 5xx, and transport failure. `unknown` MUST NOT be reported as `absent`.

Version tools MUST NOT throw for `absent` or `unknown`. They return the status and no `version`.

## Repository source

Two different facts are in play. The version the tool reports, and the repository the project will download the artifact from.

**Inquiry.** The tool reads versions from the Maven repositories the project declares and from one trusted public repository:

- Maven Central, for an ordinary coordinate.
- Google Maven, for an Android or Google group.
- The Gradle Plugin Portal, for a plugin-marker coordinate.

A version that exists only in a declared repository stays in the version set.

**Connection.** A dependency is added to the build only through a repository the project already declares. The tool MUST NOT add a repository URL, and it MUST NOT insert `mavenCentral()`, `google()`, or `gradlePluginPortal()` when the project did not declare it.

A 404 from a declared proxy is not `absent` when the trusted public repository has already returned the version. The proxy fills when the build requests the artifact through that declared repository. The tool does not download the jar to fill the proxy.

When no declared repository can serve the coordinate, the version is still shown, `connectable` is `false`, and the agent MUST ask the user before any build edit.

A public search index is not a version source. `indexLatest` MUST NOT be copied into `latest` or `release`. deps.dev MUST NOT supply a version, a dependency set, a license, a scorecard, or an upgrade decision.

## Published versions

`published` is one entry per repository that returned Maven `maven-metadata`.

- `latest` is the last version added to that repository, snapshots included.
- `release` is the last non-snapshot added to that repository.
- A local sort MUST NOT replace those tags.
- A missing tag means that field is absent on that entry.
- A tag from one repository MUST NOT replace the tag from another. A proxy may say `2.16.0` while Central says `2.16.1`. Both entries stay.

`advised` is computed with `release-line` over the union of the version lists. It is not computed by picking one repository's `latest` tag.

## Advised versions

`advised` uses policy `release-line`.

Lines:

- The release line is a version with no qualifier, or with a `ga`, `final`, or `release` alias. Those aliases of the same numeric core are one line. When a bare version is present, it is the candidate shown for that line.
- A platform qualifier such as `-jre` or `-android` is its own line.
- A datestamp version, such as `20030203.000550`, is not an advised candidate when a normal release exists. It remains available under `published`.
- Stability classes, from least to most stable, are `snapshot`, `alpha`, `beta`, `milestone`, `rc`, `stable`.
- Any other qualifier is its own line. `-jre` and `-android` are different lines. `-jre8` and `-jre11` are different lines too.

When the current version is already on a line, that line is selected.

- `version` is the newest version on that same line, under the order below.
- The stable completion of the current minor wins over a prerelease of the next minor, as in choice 1. The next minor's prerelease stays a `next-minor-prerelease` sibling.
- Versions on other lines are siblings. They do not replace `version`, and the agent does not ask.
- When that line has no newer stable version and more than one newer prerelease, there is no `version` until the user chooses, as in choice 3. The choice is among prereleases of that line.

## Order

This order picks the newest version on one line, and it breaks a same-depth tie inside `graph`.

1. The higher numeric core wins. Dotted integers are the core. A missing segment compares as `0`, so `1.0.0.1` is newer than `1.0.0`.
2. The same core: the higher stability class wins.
3. The same class: the higher prerelease ordinal wins, so `alpha05` is older than `alpha06`. An ordinal counts only when it is attached to `snapshot`, `alpha`, `beta`, `milestone`, or `rc`.
4. Release aliases of the same core compare equal. The displayed version is the bare one when it is present, otherwise `release`, then `final`, then `ga`.
5. Anything still tied is ordered by the greater original string, so the order is total.

A datestamp is a core whose first segment has eight or more digits, such as `20030203.000550`. The order above ranks it by those digits. `release-line` still drops it from advised candidates when the same line has a version whose first segment has fewer than eight digits.

Choices:

1. Current `1.5.0-alpha05`, and both stable `1.5.0` and `1.6.0-alpha` exist. `version` is `1.5.0`. `1.6.0-alpha` MUST still be returned as a sibling candidate with role `next-minor-prerelease`. This case does not wait for a choice.
2. No current version, and the catalog has more than one line (`2.16.1`, `2.16.1-android`, `20030203.000550`). There is no `version` until the user chooses. Candidates, best first: `2.16.1` with `recommended: true`, then `2.16.1-android`. The datestamp is not a candidate. The tool does not write to the project. The agent MUST show the list and ask. The project receives only the chosen candidate.
3. Current `1.5.0-alpha05`, and neither stable `1.5.0` nor stable `1.6.0` exists, while `1.5.0-alpha06` and `1.6.0-alpha` do. There is no `version` until the user chooses. Candidates: `1.5.0-alpha06` with `recommended: true`, then `1.6.0-alpha`. The agent MUST ask.

A prerelease MAY be the primary `version` when the user chooses it.

## Resolved versions and the POM graph

`resolved` has two levels. The POM walk is neither of them.

Example: the project depends on `com.squareup.okhttp3:okhttp:4.12.0` and constrains `kotlin-stdlib-jdk8` to `1.9.24`.

| Field | Meaning in the example |
| --- | --- |
| `resolved.direct` | The first level the build resolved: `okhttp 4.12.0` only. |
| `resolved.tree` | The build's own full tree. Gradle selects Kotlin `1.9.24`. Present only after that build succeeds. |
| `graph` | The published-POM walk. Kotlin stays `1.8.21`. |

- A missing `tree` means not computed. `direct` MUST NOT be presented as the tree.
- `graph` MUST say that the project constraint was not applied. It MUST list these gaps when they apply: version ranges skipped, dependencies inside profiles not read, children of an optional dependency not walked.
- A failed Gradle run MUST NOT rename `graph` to `tree`.
- Maven uses the same split. `tree` is `mvn dependency:tree`. The POM walk stays `graph`.

`graph` mediates with nearest-wins. These rules apply only to `graph`.

- An edge is compile or runtime. An empty scope is compile. An optional dependency is a node, and its children are not walked. An exclusion applies only below the node that declared it.
- The version on a node is the version on the dependency, otherwise the enclosing POM's `dependencyManagement` including import BOMs, otherwise the parent POM.
- There is one node per `groupId:artifactId`.
- The nearer node wins. Nearness is the breadth-first depth from the root. The smaller depth wins.
- The same depth: the newer version wins, by the order in Order. Declaration order is not a tie-break.
- A version range, a dependency that appears only inside a profile, and the consumer's `dependencyManagement` are not applied. When one of those is skipped, `graph.gaps` says so.
- A missing transitive POM keeps the declared node and marks the graph partial.

## Search

Search discovers candidate coordinates. It does not choose the version to pin. The tool keeps the search index order and does not pick a winner. A hit's version is `indexLatest` until `maven-metadata` is read from a declared repository or from the trusted public repository.

Example: `q=okhttp` returns `org.wso2.orbit.com.squareup.okhttp:okhttp` (`indexLatest` `4.12.0.wso2v5`) before `com.squareup.okhttp3:okhttp`. That order stands. The first hit is not the library for that name.

Each hit carries `groupId`, `artifactId`, `indexLatest`, and `versionCount`.

If the user did not name an exact `groupId` and `artifactId`, the agent MUST ask which hit is meant before it computes an advised version.

The same query parse applies to Maven Central, Nexus, and Artifactory.

- `groupId:artifactId` and `groupId:artifactId:version` are a coordinate lookup. The version is ignored for the search.
- A miss is `results: []` with no `capabilityUnavailable`.
- `unreachable`, `rate_limited`, and `blocked` are reserved for a real HTTP or transport failure.
- A single-letter search field stays a field query. `g:io.ktor` means group `io.ktor`, not group `g` and artifact `io.ktor`.

The caller's `limit` caps how many hits are returned. The server follows the backend's next page, including a Nexus `continuationToken`, until there is no further page or the limit is filled. `truncated` is true when a further page existed and was not read. `versionCount` is the count the backend reports for that coordinate. It is not the number of rows on the page that was read. When the backend reports no count, `versionCount` is omitted.

## Provenance

The source of a version is a repository that was queried and that contains that version. An index row or a deps.dev row is not a source.

The query order is the project's declared repositories first, then the trusted public repository. When several of them contain the version, the answer names the first in that order and lists the others.

Example: the company repository returns HTTP 200 without `2.16.1`. Maven Central answers next and contains `2.16.1`. The source of `2.16.1` is Central. The project still connects through its declared repository.

A field with no supplying repository has no source.

## Relocation

When a POM contains `relocation`:

- Every answer that saw it includes `relocatedTo`.
- The old coordinate is `exists`, because its POM is published. `published` describes that stub.
- `advised` for which coordinate to take is the new coordinate.
- The agent MUST NOT add the old coordinate to the project.

## Gradle plugin markers

For a marker such as `org.jetbrains.kotlin.jvm:org.jetbrains.kotlin.jvm.gradle.plugin`:

- The answer identity stays the marker. That is what the build file contains. The latest plugin version is computed on the marker.
- `resolvedImplementation` is present when the marker POM was read.
- Vulnerabilities, health, and upgrade closure are computed on the implementation coordinate.
- If the marker POM cannot be read, those facts are `unknown`. An empty CVE list MUST NOT be treated as clean.
- Upgrade closure MUST NOT drop the plugin with `plugin marker; closure not compared`.

## License

License text comes from the POM `<licenses>` element. When that element is missing or empty, it comes from the LICENSE of the source repository named by the POM `<scm>` element.

deps.dev is not a license source. The tool does not download a jar to discover a license.

When neither the POM nor `<scm>` yields a license, the license is unknown. An unknown license MUST NOT block an upgrade.

## Cache

An answer served from cache includes `observedAt`. The status stays `exists` or `absent`.

The agent MUST NOT say the check just happened when `observedAt` is older than the response.

Ceilings stay: metadata up to one hour, a cached not-found up to five minutes. `verify_coordinates` and responses that used authorization stay uncached.

A cached not-found from a declared proxy MUST NOT override a version the trusted public repository has already returned.

## Applying an upgrade

Notes are context. They do not choose the version number. The tool reads them when they are available from GitHub releases, `CHANGELOG.md` on the default branch, or the AndroidX and AGP docs. A wider web search happens only after the user agrees.

A failed compile is not the classifier.

| Notes | `notesStatus` | `upgradeEffect` | What the agent does |
| --- | --- | --- | --- |
| None found | `missing` | omitted | Continue as a plain version bump. Do not ask whether to bump or stop. |
| They describe a package move or a migration guide | `classified` | `migration` | Show `version`. Do not write it into the project. Warn the user. `2.0.0` → `3.0.0` moving `com.example.lib` to `com.example.lib2` is this case. |
| They name a concrete caller edit in the same packages | `classified` | `source-adaptation` | Apply that edit together with the version. `1.4.0` → `1.5.0` replacing `oldName()` with `newName()` is this case. If the edit cannot be applied safely, ask the user. Do not revert only because the edit exists. |
| Found, and they match neither case | `inconclusive` | omitted | Ask the user before editing the project. |

After a version edit, a compile failure is the user's choice. The agent offers to fix the build error, or to roll back the upgrade together with the edits it made for that upgrade. The agent does not choose. If the user chooses to fix and the build fails again, the agent offers the same choice again. It does not roll back on its own.

## Upgrade labels

A pair of versions has two labels.

| Label | Values | Compared on |
| --- | --- | --- |
| `numeric` | `major`, `minor`, `patch`, `same` | The numeric core only. The qualifier is ignored. |
| `stability` | `promoted`, `same`, `regressed` | `snapshot`, `alpha`, `beta`, `milestone`, `rc`, `stable`. |

`2.0.0-beta-1` → `2.0.0` is `numeric: same` and `stability: promoted`. `2.0.0-beta-1` → `2.0.1` is `numeric: patch` and `stability: promoted`.

## Machine fields

Wire JSON uses these names. A field omitted means not computed.

```yaml
authority: published | resolved | advised
policy: release-line          # required when authority is advised
status: exists | absent | unknown
version: string               # omitted when the user must choose, and on absent/unknown
published:                    # one entry per repository that returned metadata
  - repository: string
    latest: string            # omitted when that repository has no <latest>
    release: string           # omitted when that repository has no <release>
indexLatest: string           # search-index latestVersion; not release
connectable: boolean          # false when no declared repository can serve the coordinate
connectsThrough: string       # declared repository the build will use; omitted when connectable is false
candidates:
  - version: string
    recommended: boolean
    role: next-minor-prerelease   # optional
resolved:
  direct: [coordinate]
  tree: [coordinate]          # omitted until the build tree succeeds
graph:
  nodes: [coordinate]
  gaps: [string]
relocatedTo: {groupId, artifactId, version}
resolvedImplementation: {groupId, artifactId, version}
license:
  origin: pom | scm | unknown
observedAt: string            # when the answer was served from cache
source:
  repository: string          # first queried repository that contains this version
  alsoFoundIn: [string]
numeric: major | minor | patch | same
stability: promoted | same | regressed
notesStatus: missing | inconclusive | classified
upgradeEffect: bump | source-adaptation | migration   # required when notesStatus is classified; omitted otherwise
changelogNotes: [string]
```

Search hit: `{groupId, artifactId, indexLatest, versionCount}`. `versionCount` is omitted when the backend reports none. A search response includes `truncated: boolean`.

Search failure on a real HTTP or transport error: `capabilityUnavailable: unreachable | rate_limited | blocked`. A coordinate miss does not use this field.

## Closed

Nothing in this spec is left undecided. The order, the current platform line, search pagination, and `graph` mediation are the sections Order, Advised versions, Search, and Resolved versions and the POM graph.
