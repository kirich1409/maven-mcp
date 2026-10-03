---
name: upgrade-closure
description: >-
  Use when the user asks what a single direct dependency upgrade changes in the
  published closure, which transitives appear or disappear, or whether the
  candidate coordinate brings new vulnerabilities or license categories. One
  deps.dev graph pair. Not a project resolve and not a safety verdict.
disable-model-invocation: true
---

# Upgrade closure

Compare one direct upgrade (`fromVersion` → `toVersion`) on an isolated public
deps.dev graph. Call the tool. Do not diff graphs yourself.

## Steps

1. Take exactly one library upgrade: `groupId`, `artifactId`, `fromVersion`,
   `toVersion`. If the user named more than one, say a batch needs a project
   resolve that this tool does not do, and do not call it once per coordinate.

2. Call **`compare_upgrade_closure`** with that single upgrade. Leave
   `graphSource` unset or set it to `depsdev`. `includeLicenses` defaults to
   true.

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

- One upgrade. Do not loop this tool.
- Do not diff two `get_transitive_graph` results. That wrapper drops `relation`,
  and the server already owns the matrix.
- Do not run `mvn dependency:tree` or `./gradlew dependencies` and present the
  text as if OSV had been queried.
- This is an isolated public graph, not the project's classpath. It does not
  see `dependencyManagement`, `ResolutionStrategy`, strict versions,
  `enforcedPlatform`, exclusions, or private artifacts. Say that when you
  render the result.
- An empty OSV list is not verified-clean. Unchanged transitives were not queried.
- License rows are heuristic signals, not legal advice. Only changed coordinates
  are licensed.

## Fallback (MCP unavailable only)

Say the closure was not compared. Do not reconstruct the diff by hand and do
not treat a dependency tree printout as an OSV result.
