---
name: upgrade-closure
description: >-
  Use when the user asks what a direct dependency upgrade changes in the
  resolved closure, which transitives appear or disappear, or whether the
  candidate brings new vulnerabilities or license categories. Gradle when a
  wrapper exists; otherwise one POM-graph upgrade. Not a safety verdict.
disable-model-invocation: true
---

# Upgrade closure

Call **`compare_upgrade_closure`**. Do not diff graphs yourself.

`graphSource` defaults to `auto`:

- `auto` — Gradle when the project is a Gradle build and `gradlew` exists,
  otherwise the POM graph.
- `gradle` — two sequential wrapper resolves. A failed Gradle run is not a POM graph.
- `pom` — one isolated published-POM graph. Exactly one upgrade.

Gradle accepts up to 20 substitutions (`substitution` `exact` or `module`,
default `exact`). The server does not retry `exact` as `module`. The POM graph
rejects more than one upgrade. `includeLicenses` defaults to true.

## Steps

1. Pass the library upgrades the user named (`groupId`, `artifactId`,
   `fromVersion`, `toVersion`) and `projectPath` when the project is not the
   working directory. Leave `graphSource` unset unless the user asked for
   `gradle` or `pom`. Do not call the tool once per coordinate.

2. If the selection would be the POM graph and the user named more than one upgrade,
   say "a batch closure needs a Gradle wrapper" and do not call. Do not loop one
   call per coordinate. The confirm-step split lives in `/check-deps`: markers; the
   `runtimeClasspath` predicate only for Gradle configuration names; without a
   wrapper, Maven `implementation` and `runtimeOnly` stay (drop test scopes and
   `compileOnly` / `provided`); the 20-upgrade cap; no call when nothing remains.
   Do not invent a second split.

3. Render with the same decision row as `/check-deps`. Lead with `advisory`,
   `graphSource`, `diffReliable`, and any `not closure-checked` line you already
   printed. Do not re-rank. Do not paraphrase a CVE `summary`. Skip a field
   only when the payload omitted it.

   The upgraded coordinate is not in `vulnerabilities.introduced` / `remaining` /
   `fixed`. Its introduced, remaining, and fixed rows are
   `targets[].vulnerabilityDelta`; keep that relation. `targets[].vulnerabilities`
   is `relation: target` only. Do not relabel a delta row `target`, and do not use
   the target list to fill introduced, remaining, or fixed. Other coordinates come
   from the closure buckets.

   Render every CVE decision row in those lists, whatever `advisory` is. A `stop`
   result still shows a non-MAL CVE. A `review` result still shows `fixed` and
   `MEDIUM`/`LOW` rows. `advisory` only chooses the action.

   - **`stop`** — do not apply the edit. Also call out each `MAL-` id in `targets[].vulnerabilities`,
     `targets[].vulnerabilityDelta`, and `vulnerabilities.introduced` / `remaining` /
     `uncompared`, including `path` when present. If the same id is in both the
     target list and the delta, one row, and the relation is the delta's. Do not
     apply the edit. Do not call an `uncompared` `MAL-` "introduced".
   - **`review`** — one decision row per introduced id from `vulnerabilityDelta`
     and from `vulnerabilities.introduced` (include an id with no severity), per
     `relation: target` at `CRITICAL`/`HIGH` only when that id is not already in
     `vulnerabilityDelta`, and per remaining `CRITICAL`/`HIGH` from
     `vulnerabilityDelta` and from `vulnerabilities.remaining`, still labeled
     remaining. Then license `violation`s and every matrix reason that is set:
     `diffReliable` false, `fixesIncomplete`, a target not `landed`,
     `rewroteVersionless`, `capabilityUnavailable`. When
     `compare_versions(safeUpgrade.version, toVersion) > 0`, say the confirmed
     direct version does not clear every known CVE and name that candidate. That
     compare is against the line-matched `safeUpgrade.version` for the queried
     package and the line, not clearance by a cross-line numeric win. That
     compare is `compare_versions`: numeric segments, then stability class, qualifier
     presence, prerelease ordinals, then a lexical tie-break. Not text order (`1.10`
     is above `1.9`). An RC and a final release that share a numeric core are not equal. Ask
     again before editing. Writing that higher version is a new confirm and a
     new preview.
   - **`info`** — `added` / `changed` / `removed` counts and the first rows, then
     a decision row for each remaining `MEDIUM`/`LOW`, each `fixed` id, and each
     introduced `MEDIUM`/`LOW`, from `vulnerabilityDelta` and from the closure
     buckets. License rows whose `verdict` is `review` or `ok`. Do not call the
     bump safe. Those rows are in addition to the full CVE list above, not instead of it.
   - **`unknown`** — the closure was not compared. Quote `error`. Do not describe
     empty buckets as "no change" and do not fill the gap.
   - **`none` after a compare** (`diffReliable: true`, and the target has a
     `vulnerabilities` list) — no coordinate change outside the target, then that
     target OSV result (`relation: target`). Quote the non-guarantee note from
     `notes`. Do not say safe. If the coordinate is a platform or carries
     `managedBy`, and `graphSource` is `pom`, do not use that closure
     sentence: an empty POM-subset diff is not "no coordinate change". Ranges,
     profiles, and the consumer's `dependencyManagement` are not applied. The
     server has no `isPlatform` input.
   - **`none` when nothing was requested to change** — nothing was requested to
     change and the current coordinate was not re-queried. Do not say the graphs
     were compared. Do not invent an empty OSV result. Do not say safe.
     `targets[].vulnerabilities` is absent on that path; an empty list would mean
     it was queried.

   Compared `none`, all-identity `none`, and `unknown` are not "safe" and are not
   "no vulnerabilities".

   `targets[].safeUpgrade` is one extra line on the direct coordinate, labeled
   advisory. It is not a row per CVE. `safeUpgrade.version` is advisory, is not
   the version to write, and is only the fix on the queried package and the line
   that contains the installed version. Name `safeUpgrade.version` only when
   `fixesAllKnown` is `true`. When `fixesAllKnown` is false, a version that
   fixes every known issue on that package and line is not known; surface
   `reason` and do not name a version.

4. A plugin-marker upgrade comes back as `advisory: unknown` with
   `error: "plugin marker; closure not compared"`. Do not treat that as `none`.

## Decision row

One CVE, in this order. Same row as `/check-deps`. Do not paraphrase `summary`.

1. `relation` — introduced, remaining, fixed, uncompared, or target.
2. `vulnerableNode` as `groupId:artifactId:version`.
3. `id`, linked with `url`. When `url` is empty, use `https://osv.dev/vulnerability/{id}`.
4. `severity`, or `severity unknown` when hydration left it off.
5. `summary`, or the id alone when `summary` is empty.
6. `fixedVersion`, the fix on the queried package and the line that contains the vulnerable node's version, or `fix unknown` when omitted, including when the containing range has no `fixed`. That string is the `fixed` event of the ECOSYSTEM interval that contains the queried version on the affected entry whose `package.ecosystem` is `Maven` and whose `package.name` is `groupId:artifactId`. Do not take the first ECOSYSTEM `fixed`. `fixed` is exclusive. `last_affected` is inclusive and is not a fix. `limit` is not a fix. Do not borrow a fix from another line of the same package or from a different package.
7. `clearedBySelection`: false means the selected version is still inside an affected range of that package or is listed in that entry's `versions`, even if it sorts above `fixedVersion`. True means the selected version is at or above the fix for the queried package and the line that contains it, and is not still inside an affected range or listed in its versions. A numeric win over `fixedVersion` is not clearance when that string is not the line that contains the selected version. Omit this clause when there is no `fixedVersion`. For `relation: fixed`, say the bump no longer selects the vulnerable version.
8. `path` as an arrow chain (`g:a:v → g:a:v`) when present. When `pathOmitted` is present, say the middle was shortened and the leaf is the vulnerable node.
9. When the node is not the target and `clearedBySelection` is false: this coordinate is not the line being edited; clearing it needs a constraint or a higher root, and this preview does not propose that pin.

## Constraints and non-goals

- Do not loop this tool. One Gradle batch, or one POM-graph upgrade.
- Do not treat a failed Gradle result as a POM graph.
- Do not diff two `get_transitive_graph` results. That wrapper drops `relation`,
  and the server already owns the matrix.
- Do not run `mvn dependency:tree` or `./gradlew dependencies` and present the
  text as if OSV had been queried.
- When `graphSource` is `pom`, say the graph is isolated: it does not see
  version ranges, profiles, the consumer's `dependencyManagement`,
  `ResolutionStrategy`, strict versions, `enforcedPlatform`, or exclusions.
  Do not say that about a Gradle result.
- `landed: false` or `rewroteVersionless: true` is at least `review`. If
  `landed` is false, you may offer one retry with `substitution: "module"`.
  The server does not launch that second pair. Do not send it unless the user
  accepts. An accepted retry is a new preview, not an edit.
- An empty OSV list is not verified-clean. Unchanged transitives were not queried.
- License rows are heuristic signals, not legal advice. Only changed coordinates
  are licensed. On the Gradle path, license metadata is not taken from the
  resolve; a missing license record is `review`, not a known license. The
  Gradle path still reads that license metadata from deps.dev GetVersion. The
  POM path reads `<licenses>` from the artifact POM.

## Fallback (MCP unavailable only)

Say the closure was not compared. Do not reconstruct the diff by hand and do
not treat a dependency tree printout as an OSV result.
