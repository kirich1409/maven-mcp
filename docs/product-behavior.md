# Product behavior

How a dependency decision is made, and which part does which work.

`docs/tool-contract.md` is what each tool returns. `AGENTS.md` describes the current implementation. Where they differ, this document and the tool contract are the target.

The words MUST, MUST NOT, SHOULD, and MAY are normative. A missing field means the value was not computed. It does not mean the value equals some other field.

The server does not edit the project. A file edit waits for the user.

## Roles

Three parts. Each does only its own work.

**Tool.** Deterministic fetch and parse. It reads the project, repository metadata, and POM files. It returns those facts. It does not choose a pin. It does not call a model. It does not classify changelog prose. It does not edit the project.

**Skill.** The procedure. It names which tool to call, in which order, and which result a rule already decides. The model follows a skill rule as written.

**Model.** It follows the skill for the [Decision](#decision). It judges only in the places listed under [Judgment](#judgment). When the user asks a question that procedure does not answer, the model calls the tools it already has and concludes from their facts. It does not add a tool and it does not invent a fact the tools did not return.

`check-deps` is the procedure for a project. `latest-version` is the procedure for one coordinate when the project has no pin. A question about only licenses, only vulnerabilities, only health, or only a changelog uses that skill. No extra skill owns the decision or a fix search.

## Decision

The skill produces one record for each dependency in the project, or for the one coordinate the user named. The tool does not return this record.

| Answer | Meaning |
| --- | --- |
| `use` | The coordinate to use, after relocation. |
| `upgradeTo` | The version to move to, or a question to the user. |
| `worth` | Whether that move is worth making, from the POM and the notes. |
| `vulnerabilities` | Advisories on the pin and on `upgradeTo`, and the path when the graph has one. |

`worth` and `upgradeTo` are the skill's words to the user. They are not tool fields.

| Source | Fact the tool returns |
| --- | --- |
| Project | The pin, where it is written, the version the build selected, declared repositories, and toolchain pins for AGP, Kotlin, Gradle, Spring Boot, and JDK. |
| Metadata | `candidates` when there is a pin. One newest bare stable when there is no pin. Each repository's `latest` and `release` tags, unchanged by a local sort. |
| POM of the pin | `relocatedTo`, licenses, `<scm>`, managed versions, and the dependency graph. For a plugin marker, the implementation coordinate from its dependency. |
| The version being named | The same POM facts, advisories for that version, and the changelog text between the pin and that version. |

When the POM contains `relocation`, `use` is the new coordinate. The tool returns `relocatedTo` on the answer that read that POM. It does not switch that answer's `candidates` to the new coordinate. The skill calls the version tool for the new coordinate. The old coordinate stays `exists`, because its POM is published. The skill does not propose adding the old coordinate.

## Authorities

An observation promises exactly one authority. The [Decision](#decision) is not an authority and is not a tool field.

| Authority | Promise |
| --- | --- |
| `published` | Maven `maven-metadata` tags from one queried repository. Tags are not merged across repositories. |
| `resolved` | What this project's Gradle or Maven build selected. |

Every observation that is one of those two MUST include `authority`. A candidate list does not.

Coordinate existence is not a version authority. It is a status: `exists`, `absent`, or `unknown`.

Other authorities MAY appear beside the primary one. The caller MUST be able to tell which value is primary.

## Default authority

When the caller does not pass `authority`, the primary value follows the question.

| Question | Primary | Beside it |
| --- | --- | --- |
| Catalog search | No version authority. Hits carry `indexLatest` until metadata is read. | `published` after metadata for the chosen coordinate has been read. |
| Which version should I take | No version authority. The skill builds the [Decision](#decision) from `candidates` and the POM facts. | `published` once repository metadata has been read. `resolved` only after a project resolve. |
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

`candidates` are computed over the union of the version lists. They are not computed by picking one repository's `latest` tag.

## Parsing a version

The tool parses. The model does not parse the version string again. `compare_dependency_versions` and each audit dependency include the pin already parsed as `currentLine` and `currentStability`. `currentStability` is omitted when the pin's qualifier is unknown.

Stability classes, from least to most stable, are `snapshot`, `alpha`, `beta`, `milestone`, `rc`, `stable`.

A known token matches only as a whole token, at a qualifier boundary. `-` or `.` precedes it. A digit, `-`, `.`, `+`, or the end of the version follows it. Case does not matter.

| Token | Class |
| --- | --- |
| `SNAPSHOT`, `dev`, `nightly` | `snapshot` |
| `alpha`, `a`, `preview`, `eap`, `ea`, `pre` | `alpha` |
| `beta`, `b` | `beta` |
| `milestone`, `M` | `milestone` |
| `rc`, `cr` | `rc` |

A longer word is not the token. `devtools` is not `dev`. `prerelease` is not `pre`. When two known tokens appear in one version, the less stable class wins.

`line` is `release` for a bare stable or a known prerelease. `1.5.0-alpha05`, `1.5.0-rc01`, `1.0.0-preview.1`, and `1.0.0` share the `release` line when the numeric core is the same. Different numeric cores are different rows. `ga`, `final`, and `release` aliases of the same numeric core are the `release` line. When a bare version is present, it is the row shown for that core.

A number after `+` on a known prerelease is its ordinal. `17-ea+9` is older than `17-ea+10`. A `+` number on a stable version is not an ordinal.

Any other qualifier is its own line, and `stability` is omitted. A number on `jre` or `android` is part of the line name. `jre8` and `jre11` are different lines. `jre8`, `jre-8`, and `jre.8` are one line. For any other unknown qualifier, one trailing number is an ordinal inside that line. `foo`, `foo.1`, and `foo9` are one line. `foo-bar` is another line.

A datestamp version, such as `20030203.000550`, is not a candidate when a normal release exists. It remains available under `published`.

## Candidates

`compare_dependency_versions` and the update section of `audit_project_dependencies` return `candidates`. No candidate is marked recommended. `get_latest_version` returns one newest bare stable and does not return `candidates`. When no bare stable exists, that version is absent. A preview, EAP, dev, or nightly build is not that version.

Given a current version, `candidates` contains:

- the newest bare stable of the same major.minor, when it is newer than current
- the newest bare stable of each newer minor in the same major
- the newest bare stable of each higher major
- the newest known prerelease newer than current on each numeric core
- the newest version of each unknown-qualifier line

Patch history inside one minor is not included. A bare stable sorts above a prerelease and above `jre` or `android`. Those other rows are still returned. The tool does not pick one of `1.3.6`, `1.5.6`, and `2.0.0`.

`distance` is present when a current version was given. It is `patch` when the major and minor match and the patch is newer, `minor` when the major matches and the minor is newer, `major` when the major is newer, `prerelease` for a known prerelease, and `other` for any other qualifier. It is not a recommendation.

## Naming rule

The skill names a version, and the model does not override it, only when all three hold: `currentLine` is `release`, the set contains one newer bare stable, and that stable has the same major.minor as the pin. A preview pin is on the `release` line, so `1.3.0-preview.2` with only `1.3.6` newer is named `1.3.6`.

When the caller has no pin, `get_latest_version` is the tool. Its one bare stable is the version the skill names, and the skill does not ask. A project pin uses `compare_dependency_versions`.

## Judgment

The model judges only in these cases.

- `stability` is omitted on the pin or on a candidate. The model says whether the qualifier is a prerelease, a platform build, or a stable alias. When it cannot tell, it asks. It keeps the row.
- Several newer minors are present, or a higher major is present. The model does not name a version from `distance` alone. Before it names a higher minor or a higher major, the skill has it call `get_dependency_changes` for the pin and that version, and `get_dependency_vulnerabilities` for that version. For an AGP, Kotlin Gradle plugin, Gradle, Spring Boot, or JDK pin it also calls `check_version_compatibility`. Missing notes, a break the notes do not settle, or an advisory on that version means the model asks.
- `currentLine` is not `release`. The naming rule does not take the bare stable. The newer version on the same line stays visible beside it, and the model asks which line to keep.
- Changelog text is present for a version the skill will write. A package move is shown and not written. A concrete caller edit in the same packages is applied together with the version, and the model asks when that edit cannot be applied safely. Any other text means the model asks before writing. The tool returns the text. It does not return `notesStatus` or `upgradeEffect`.

A preview or a `jre` / `android` line is not preferred over a bare stable on the `release` line. Writing the build file still waits for the user. `compare_upgrade_closure` runs after that confirmation, for the versions the user accepted.

`capabilityUnavailable`, `partial: true`, and `existenceStatus: unknown` mean the check stopped short. The model does not treat that result as clean, and does not treat an empty list beside those fields as nothing found.

## Finding a fix

When the user asks whether a newer release, alpha, or other prerelease already contains a fix for a bug, the model uses the tools it has.

It calls `compare_dependency_versions` for the pin. The result includes newer bare stables and the newest known prerelease of each numeric core. It then calls `get_dependency_changes` from the pin to the newest of those candidates. That call lists every published version in the span, not only the two ends. Each entry has `body` when the notes contain a section for that version. An entry without `body` is still a published version.

The model reads those bodies and names the version whose text mentions the fix. That version may be an alpha, including when the pin is already an alpha and a newer alpha is in the span. No mention means the notes do not show the fix. The model does not invent a version and does not search past the text the tool returned.

This answer is not the [Naming rule](#naming-rule). The naming rule still names the single newer same-minor stable when the user asked what to upgrade to. A fix search reports the version whose notes mention the bug.

## Order

This order picks the newest version on one line, and it breaks a same-depth tie inside `graph`.

1. The higher numeric core wins. Dotted integers are the core. A missing segment compares as `0`, so `1.0.0.1` is newer than `1.0.0`.
2. The same core: the higher stability class wins.
3. The same class: the higher prerelease ordinal wins, so `alpha05` is older than `alpha06`, `preview.9` is older than `preview.10`, and `eap9` is older than `eap13`. An ordinal counts when it is attached to a recognised prerelease token by `-`, `.`, or no separator. A number after `+` on a recognised prerelease is that ordinal, so `17-ea+9` is older than `17-ea+10`. A `+` number on a stable version is not an ordinal.
4. Release aliases of the same core compare equal. The displayed version is the bare one when it is present, otherwise `release`, then `final`, then `ga`.
5. Anything still tied is ordered by the greater original string, so the order is total.

A datestamp is a core whose first segment has eight or more digits, such as `20030203.000550`. The order above ranks it by those digits. [Parsing a version](#parsing-a-version) still drops it from `candidates` when a normal release exists.

This order sorts rows and breaks a same-depth tie inside `graph`. It does not pick `upgradeTo`. The [Naming rule](#naming-rule) and [Judgment](#judgment) do that.

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

If the user did not name an exact `groupId` and `artifactId`, the model MUST ask which hit is meant before the skill builds a [Decision](#decision) for it. `indexLatest` is not `upgradeTo`.

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
- `use` in the [Decision](#decision) is the new coordinate. The tool does not switch `candidates` onto that coordinate inside the same answer. The skill calls the version tool for it.
- The skill MUST NOT propose adding the old coordinate to the project.

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

Notes are context. They do not choose the version number. The tool returns the text when it is available from GitHub releases, `CHANGELOG.md` on the default branch, or the AndroidX and AGP docs. It does not classify that text. A wider web search happens only after the user agrees.

How the model reads the text is [Judgment](#judgment). A failed compile is not the classifier.

After a version edit, a compile failure is the user's choice. The model offers to fix the build error, or to roll back the upgrade together with the edits it made for that upgrade. The model does not choose. If the user chooses to fix and the build fails again, the model offers the same choice again. It does not roll back on its own.

## Pair labels

A pair is not given `upgradeType`, `numeric`, or a promoted/same/regressed `stability`. Each candidate carries its own `stability` class and, when a current version was given, `distance`. `2.0.0-beta-1` beside `2.0.0` is a prerelease row and a bare stable row. The [Naming rule](#naming-rule) names `2.0.0` when it is the one newer bare stable of that same major.minor.

## Machine fields

Wire JSON uses these names. A field omitted means not computed.

```yaml
authority: published | resolved   # observation only; the Decision is not an authority
status: exists | absent | unknown
published:                    # one entry per repository that returned metadata
  - repository: string
    latest: string            # omitted when that repository has no <latest>
    release: string           # omitted when that repository has no <release>
indexLatest: string           # search-index version; not upgradeTo
connectable: boolean          # false when no declared repository can serve the coordinate
connectsThrough: string       # declared repository the build will use; omitted when connectable is false
currentLine: release | string # parsed pin; compare and audit
currentStability: snapshot | alpha | beta | milestone | rc | stable   # omitted when the qualifier is unknown
candidates:
  - version: string
    line: release | string
    stability: snapshot | alpha | beta | milestone | rc | stable   # omitted when the qualifier is unknown
    distance: patch | minor | major | prerelease | other   # omitted when no current version was given
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
changelogNotes: [string]      # text only; the tool does not classify it
```

The [Decision](#decision) record (`use`, `upgradeTo`, `worth`, `vulnerabilities`) is what the skill tells the user. It is not a tool field.

Search hit: `{groupId, artifactId, indexLatest, versionCount}`. `versionCount` is omitted when the backend reports none. A search response includes `truncated: boolean`.

Search failure on a real HTTP or transport error: `capabilityUnavailable: unreachable | rate_limited | blocked`. A coordinate miss does not use this field.

## What this replaces in open issues

The issue stays open until the code and the skill match this document. The old expected result is what this document replaces.

| Issue | Replaced result |
| --- | --- |
| #70 | A known prerelease token has its class. It is not the bare stable the naming rule uses. |
| #45 | `upgradeType` and a promoted/same/regressed pair label are gone. A candidate carries `distance` and its own class. `ga`, `final`, and `release` of one core compare equal. |
| #46 | There is no `authority: advised`, no `policy: release-line`, and no `recommended`. Version choice is the [Decision](#decision). Per-repository `latest` and `release` tags stay [Published versions](#published-versions). |
| #50 | `relocatedTo` is on every answer that saw the POM block. `use` is the new coordinate through a second tool call. |
| #53 | The tool returns changelog text. [Judgment](#judgment) reads it. The tool does not grow `notesStatus` or `upgradeEffect`. |

#47, #48, #49, #51, #52, #54, #25, and #23 stay tool facts. They do not become a second way to choose `upgradeTo`.
