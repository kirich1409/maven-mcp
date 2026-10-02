#!/usr/bin/env bash
# post-edit-deps.sh — PostToolUse /check-deps reminder for maven-mcp.
#
# After Edit/Write/MultiEdit on a build file, emit a systemMessage nudge to run
# /check-deps — but only when the changed content looks coordinate-shaped
# (avoids nagging on comment/formatting-only edits).
#
# Fail-open is structural: trap 'exit 0' EXIT immediately after set -euo
# pipefail. Malformed stdin, jq failure, or any other error exits 0 with no
# reminder (never surfaces a hook error for a best-effort nudge).
set -euo pipefail
trap 'exit 0' EXIT

# ── Dependency check ─────────────────────────────────────────────────────────
command -v jq >/dev/null 2>&1 || exit 0

# ── Read stdin once; fail-open on any jq parse error ─────────────────────────
HOOK_INPUT=""
HOOK_INPUT=$(cat) || HOOK_INPUT=""

TOOL_NAME=""
# Claude Code: tool_name / tool_input (snake_case). Grok Build: toolName / toolInput (camelCase).
TOOL_NAME=$(printf '%s' "$HOOK_INPUT" | jq -r '.tool_name // .toolName // empty' 2>/dev/null) || TOOL_NAME=""

# ── Fast gate: Claude Edit/Write/MultiEdit, Grok search_replace/write, ──────
# ── Codex apply_patch ────────────────────────────────────────────────────────
case "$TOOL_NAME" in
  Edit|Write|MultiEdit|search_replace|write) ;;
  apply_patch) ;;
  *) exit 0 ;;
esac

# Codex apply_patch: every build file in the patch is checked, each with its
# own "+" lines (same parsing as pre-edit-deps.sh). *** Move to: is the path
# used for the basename check; "+" lines are still read from the update section.
PATCH_TEXT=""
if [ "$TOOL_NAME" = "apply_patch" ]; then
  PATCH_TEXT=$(printf '%s' "$HOOK_INPUT" | jq -r '
    (.tool_input.command // .tool_input.patch // .tool_input.input // empty)
    | if type == "array" then .[-1] else . end
    | if type == "string" then . else empty end' 2>/dev/null) || PATCH_TEXT=""
  [ -n "$PATCH_TEXT" ] || exit 0
fi

# Prints the "+" lines (prefix stripped) of Add/Update section $1.
# $1 is that section's 1-based index. Two hunks may share a source path;
# matching the path would merge their additions.
_patch_section_lines() {
  printf '%s\n' "$PATCH_TEXT" | awk -v nth="$1" '
    /^\*\*\* (Add|Update) File: / { n++; inside = (n == nth); next }
    /^\*\*\* (Delete File:|End Patch)/ { inside = 0; next }
    inside && /^\+/ {
      line = substr($0, 2)
      sub(/\r$/, "", line)
      print line
    }
  ' 2>/dev/null
}

# One row per Add/Update section: index, TAB, effective path.
# *** Move to: is the effective path (Codex rename). Trailing whitespace is
# dropped so it matches Codex trim_end; otherwise basename misses the file.
_patch_effective_rows() {
  printf '%s\n' "$PATCH_TEXT" | awk '
    function trim_end(s) {
      sub(/[ \t\r]+$/, "", s)
      return s
    }
    function flush() {
      if (src != "") print n "\t" eff
      src = ""
    }
    /^\*\*\* (Add|Update) File: / {
      flush()
      n++
      src = $0
      sub(/^\*\*\* (Add|Update) File: /, "", src)
      src = trim_end(src)
      eff = src
      next
    }
    /^\*\*\* Move to: / {
      if (src != "") {
        eff = $0
        sub(/^\*\*\* Move to: /, "", eff)
        eff = trim_end(eff)
      }
      next
    }
    /^\*\*\* (Delete File:|End Patch)/ { flush(); next }
    END { flush() }
  ' 2>/dev/null
}

FILE_PATH=""
PATCH_BUILD_PATHS=""
if [ -n "$PATCH_TEXT" ]; then
  while IFS="$(printf '\t')" read -r _patch_idx _patch_eff; do
    [ -n "${_patch_eff:-}" ] || continue
    case "$(basename "$_patch_eff" 2>/dev/null)" in
      build.gradle|build.gradle.kts|settings.gradle|settings.gradle.kts|pom.xml|libs.versions.toml)
        # Newline stays outside $(...): command substitution strips trailing newlines.
        PATCH_BUILD_PATHS="${PATCH_BUILD_PATHS}$(printf '%s\t%s' "$_patch_idx" "$_patch_eff")
"
        [ -n "$FILE_PATH" ] || FILE_PATH="$_patch_eff"
        ;;
    esac
  done <<EOF_PATCH_PATHS
$(_patch_effective_rows)
EOF_PATCH_PATHS
else
  FILE_PATH=$(printf '%s' "$HOOK_INPUT" | jq -r '.tool_input.file_path // .toolInput.file_path // empty' 2>/dev/null) || FILE_PATH=""
fi
BASENAME=""
BASENAME=$(basename "$FILE_PATH" 2>/dev/null) || BASENAME=""

case "$BASENAME" in
  build.gradle|build.gradle.kts|settings.gradle|settings.gradle.kts|pom.xml|libs.versions.toml) ;;
  *) exit 0 ;;
esac

# ── Extract new content from the tool payload ─────────────────────────────────
# Edit/search_replace → new_string; Write/write → content;
# MultiEdit → concatenate edits[].new_string
NEW_CONTENT=""
case "$TOOL_NAME" in
  Edit|search_replace)
    NEW_CONTENT=$(printf '%s' "$HOOK_INPUT" | jq -r '.tool_input.new_string // .toolInput.new_string // empty' 2>/dev/null) || NEW_CONTENT=""
    ;;
  Write|write)
    NEW_CONTENT=$(printf '%s' "$HOOK_INPUT" | jq -r '.tool_input.content // .toolInput.content // empty' 2>/dev/null) || NEW_CONTENT=""
    ;;
  MultiEdit)
    NEW_CONTENT=$(printf '%s' "$HOOK_INPUT" | jq -r '[((.tool_input.edits // .toolInput.edits // [])[]?.new_string // empty)] | join("\n")' 2>/dev/null) || NEW_CONTENT=""
    ;;
  apply_patch)
    # Checked per section below; non-empty here only to pass the gate.
    NEW_CONTENT="$PATCH_BUILD_PATHS"
    ;;
esac

[ -n "$NEW_CONTENT" ] || exit 0

# ── Coordinate-shaped gate (noise reduction) ──────────────────────────────────
# Emit only when the changed content contains dependency-coordinate shapes.
# Missed coordinates → no reminder (fail-open for a nudge is correct).
_Q="'"
HAS_COORDS=0
# Sets HAS_COORDS=1 when $NEW_CONTENT has coordinate shapes for $BASENAME.
_check_coords() {
  case "$BASENAME" in
    build.gradle|build.gradle.kts|settings.gradle|settings.gradle.kts)
      # Quoted Gradle notation: "g:a" / "g:a:v" or 'g:a' / 'g:a:v'
      if printf '%s\n' "$NEW_CONTENT" | grep -qE '"[A-Za-z0-9._-]+:[A-Za-z0-9._-]+(:[^"]+)?"' 2>/dev/null; then
        HAS_COORDS=1
      elif printf '%s\n' "$NEW_CONTENT" | grep -qE "${_Q}[A-Za-z0-9._-]+:[A-Za-z0-9._-]+(:[^${_Q}]+)?${_Q}" 2>/dev/null; then
        HAS_COORDS=1
      fi
      ;;
    pom.xml)
      # Any dependency GAV tag in the changed span
      if printf '%s\n' "$NEW_CONTENT" | grep -qE '<(groupId|artifactId|dependency)>' 2>/dev/null; then
        HAS_COORDS=1
      fi
      ;;
    libs.versions.toml)
      if printf '%s\n' "$NEW_CONTENT" | grep -qE 'module[[:space:]]*=[[:space:]]*"[A-Za-z0-9._-]+:[A-Za-z0-9._-]+"' 2>/dev/null; then
        HAS_COORDS=1
      elif printf '%s\n' "$NEW_CONTENT" | grep -qE '"[A-Za-z0-9._-]+:[A-Za-z0-9._-]+:[^"]+"' 2>/dev/null; then
        HAS_COORDS=1
      fi
      ;;
  esac
}

if [ -n "$PATCH_BUILD_PATHS" ]; then
  while IFS="$(printf '\t')" read -r _patch_idx _patch_path; do
    [ -n "${_patch_path:-}" ] || continue
    BASENAME=$(basename "$_patch_path" 2>/dev/null) || continue
    NEW_CONTENT=$(_patch_section_lines "$_patch_idx") || NEW_CONTENT=""
    if [ -n "$NEW_CONTENT" ]; then
      _check_coords
    fi
  done <<EOF_PATCH_CHECK
$PATCH_BUILD_PATHS
EOF_PATCH_CHECK
else
  _check_coords
fi

[ "$HAS_COORDS" -eq 1 ] || exit 0

# ── Emit reminder ─────────────────────────────────────────────────────────────
printf '%s\n' '{"systemMessage":"Build dependency file was modified. Consider running /check-deps to verify dependency versions are up to date."}'
