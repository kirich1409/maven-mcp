---
name: upgrade-closure
description: >-
  Use when the user asks what a direct dependency upgrade changes in the
  resolved closure, which transitives appear or disappear, or whether the
  candidate brings new vulnerabilities or license categories. Gradle when a
  wrapper exists; otherwise one deps.dev upgrade. Not a safety verdict.
disable-model-invocation: true
---

# Upgrade closure

Call **`compare_upgrade_closure`**. Do not diff graphs yourself.

`graphSource` defaults to `auto`:

- `auto` — Gradle when the project is a Gradle build and `gradlew` exists,
  otherwise deps.dev.
- `gradle` — two sequential wrapper resolves. No deps.dev fallback if that fails.
- `depsdev` — one isolated public graph pair. Exactly one upgrade.

Gradle accepts up to 20 substitutions (`substitution` `exact` or `module`,
default `exact`). `exact` is not retried as `module`. deps.dev rejects more
than one upgrade. `includeLicenses` defaults to true.

## Steps

1. Pass the library upgrades the user named (`groupId`, `artifactId`,
   `fromVersion`, `toVersion`) and `projectPath` when the project is not the
   working directory. Leave `graphSource` unset unless the user asked for
   `gradle` or `depsdev`. Do not call the tool once per coordinate.

2. If the selection would be deps.dev and the user named more than one upgrade,
   say a batch needs a Gradle wrapper and do not call. The confirm-step split
   (markers, classpath-only rows) is a separate workflow.

3. Lead with `advisory`, `graphSource`, and `diffReliable`. Then render:

   - **`stop`** — a queried after-coordinate is `malicious`. Do not apply the edit.
     Do not call an `uncompared` id "introduced".
   - **`review`** — list introduced ids (including those with no severity), target
     `CRITICAL`/`HIGH`, and `remaining` `CRITICAL`/`HIGH` still labeled remaining.
     Then license `violation`s and every reason in `notes` (`diffReliable`,
     `fixesIncomplete`, `capabilityUnavailable`). Ask again before editing.
   - **`info`** — `added` / `changed` / `removed` counts and the first rows, then
     `remaining` `MEDIUM`/`LOW`, `fixed`, and introduced `MEDIUM`/`LOW`. Do not
     call the bump safe.
   - **`unknown`** — the closure was not compared. Quote `error` when present.
     Do not describe empty buckets as "no change" and do not fill the gap.
   - **`none` after a compare** (`diffReliable: true` and the target has
     `vulnerabilities`) — the compared graphs showed no coordinate change outside
     the target, then the target OSV result. Quote the note that an empty closure
     is not a safety guarantee.
   - **`none` when nothing was requested to change** — the current coordinate was
     not re-queried. Do not invent an empty OSV result. `targets[].vulnerabilities`
     is absent on that path; an empty list would mean it was queried.

   `none` and `unknown` are not "safe" and are not "no vulnerabilities".

4. A plugin-marker upgrade comes back as `advisory: unknown` with
   `error: "plugin marker; closure not compared"`. Do not treat that as `none`.

## Constraints and non-goals

- Do not loop this tool. One Gradle batch, or one deps.dev upgrade.
- Do not treat a failed Gradle result as a deps.dev graph.
- Do not diff two `get_transitive_graph` results. That wrapper drops `relation`,
  and the server already owns the matrix.
- Do not run `mvn dependency:tree` or `./gradlew dependencies` and present the
  text as if OSV had been queried.
- When `graphSource` is `depsdev`, say the graph is isolated: it does not see
  `dependencyManagement`, `ResolutionStrategy`, strict versions,
  `enforcedPlatform`, exclusions, or private artifacts. Do not say that about
  a Gradle result.
- `landed: false` or `rewroteVersionless: true` is at least `review`. Do not
  retry `exact` as `module` unless the user asks.
- An empty OSV list is not verified-clean. Unchanged transitives were not queried.
- License rows are heuristic signals, not legal advice. Only changed coordinates
  are licensed. On the Gradle path, license metadata is not taken from the
  resolve; a missing deps.dev record is `review`, not a known license.

## Fallback (MCP unavailable only)

Say the closure was not compared. Do not reconstruct the diff by hand and do
not treat a dependency tree printout as an OSV result.
