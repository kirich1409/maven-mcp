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

# Codex apply_patch: take the first build file from the patch headers and its
# "+" lines as the new content (same parsing as pre-edit-deps.sh).
PATCH_TEXT=""
if [ "$TOOL_NAME" = "apply_patch" ]; then
  PATCH_TEXT=$(printf '%s' "$HOOK_INPUT" | jq -r '
    (.tool_input.command // .tool_input.patch // .tool_input.input // empty)
    | if type == "array" then .[-1] else . end
    | if type == "string" then . else empty end' 2>/dev/null) || PATCH_TEXT=""
  [ -n "$PATCH_TEXT" ] || exit 0
fi

FILE_PATH=""
if [ -n "$PATCH_TEXT" ]; then
  while IFS= read -r _patch_path; do
    case "$(basename "$_patch_path" 2>/dev/null)" in
      build.gradle|build.gradle.kts|settings.gradle|settings.gradle.kts|pom.xml|libs.versions.toml)
        FILE_PATH="$_patch_path"
        break
        ;;
    esac
  done <<EOF_PATCH_PATHS
$(printf '%s\n' "$PATCH_TEXT" | sed -E -n 's/^\*\*\* (Add|Update) File: //p' 2>/dev/null)
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
    NEW_CONTENT=$(printf '%s\n' "$PATCH_TEXT" | awk -v target="$FILE_PATH" '
      /^\*\*\* (Add|Update|Delete) File: / {
        p = $0; sub(/^\*\*\* (Add|Update|Delete) File: /, "", p); inside = (p == target); next
      }
      /^\*\*\* End Patch/ { inside = 0; next }
      inside && /^\+/ { print substr($0, 2) }
    ' 2>/dev/null) || NEW_CONTENT=""
    ;;
esac

[ -n "$NEW_CONTENT" ] || exit 0

# ── Coordinate-shaped gate (noise reduction) ──────────────────────────────────
# Emit only when the changed content contains dependency-coordinate shapes.
# Missed coordinates → no reminder (fail-open for a nudge is correct).
_Q="'"
HAS_COORDS=0
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

[ "$HAS_COORDS" -eq 1 ] || exit 0

# ── Emit reminder ─────────────────────────────────────────────────────────────
printf '%s\n' '{"systemMessage":"Build dependency file was modified. Consider running /check-deps to verify dependency versions are up to date."}'
