#!/bin/bash
# =============================================================================
# sync-agents-mirror.sh — one-way generated mirror of .claude/ into .agents/
# =============================================================================
# `.claude/` is the source of the rules, skills, and agent roles that become the
# generated `.agents/rules/` and `.agents/skills/`; the shared duties live in the
# hand-maintained AGENTS.md, which this script does not read or write. Do not
# edit the generated trees by hand. Within those two trees, source
# additions/edits are copied and destination-only entries are pruned
# recursively. Every other `.agents/` entry (e.g. Antigravity's native
# hooks.json, hooks/, rules.json) is hand-maintained and never touched here.
# Vendor-coupled text must be made vendor-neutral at the source; the sync copies
# verbatim.
#
# Usage:
#   bash scripts/sync-agents-mirror.sh         # update mirror
#   bash scripts/sync-agents-mirror.sh --dry   # show pending changes only
#
# Mapping:
#   .claude/rules/        → .agents/rules/        (directory copy)
#   .claude/skills/       → .agents/skills/       (directory copy)
#   .claude/agents/<X>.md → .agents/skills/<X>/SKILL.md  (file → skill directory)
#
# Excluded: .claude/hooks/ (Codex uses .codex/hooks/), .claude/settings.json.
# Symlink rejection and comparison cover only the generated trees and their
# .claude/ sources. A failed find, diff, or stat stops the script before
# it reports a result or changes anything.
# Vendor losses: frontmatter tools/model/color — Codex ignores; body preserved.
# =============================================================================
set -e

die() { echo "ERROR: $1" >&2; exit 1; }

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$PROJECT_ROOT/.claude"
DST="$PROJECT_ROOT/.agents"
DRY_RUN=0
if [ "$#" -gt 1 ]; then
    echo "Usage: bash scripts/sync-agents-mirror.sh [--dry]" >&2
    exit 2
fi
case "${1:-}" in
    "") ;;
    "--dry") DRY_RUN=1 ;;
    *)
        echo "Usage: bash scripts/sync-agents-mirror.sh [--dry]" >&2
        exit 2
        ;;
esac

[ -d "$SRC" ] || die "$SRC not found; it is the source of the generated mirror."

EXPECTED_ROOT=$(mktemp -d)
trap 'rm -r "$EXPECTED_ROOT"' EXIT
EXPECTED="$EXPECTED_ROOT/.agents"
mkdir -p "$EXPECTED/rules" "$EXPECTED/skills"

LINK_SCOPE=()
for P in "$SRC/rules" "$SRC/skills" "$SRC/agents" "$DST/rules" "$DST/skills"; do
    if [ -e "$P" ] || [ -L "$P" ]; then LINK_SCOPE+=("$P"); fi
done
LINKS=""
[ ! -L "$DST" ] || LINKS="$DST"
if [ "${#LINK_SCOPE[@]}" -gt 0 ]; then
    FOUND=$(find "${LINK_SCOPE[@]}" -type l -print) ||
        die "find failed while looking for symlinks; nothing compared or changed."
    if [ -n "$FOUND" ]; then
        if [ -n "$LINKS" ]; then LINKS="$LINKS"$'\n'"$FOUND"; else LINKS="$FOUND"; fi
    fi
fi
if [ -n "$LINKS" ]; then
    echo "ERROR: generated mirror and its sources do not permit symlinks:" >&2
    while IFS= read -r L; do echo "  $L" >&2; done <<< "$LINKS"
    exit 1
fi

# --- 1. Build the complete expected mirror in a temporary tree. ---
for SUB in rules skills; do
    if [ -d "$SRC/$SUB" ]; then
        chmod --reference="$SRC/$SUB" "$EXPECTED/$SUB"
        cp -R --preserve=mode "$SRC/$SUB"/. "$EXPECTED/$SUB"/
    fi
done

if [ -d "$SRC/agents" ]; then
    for AGENT in "$SRC/agents"/*.md; do
        [ -f "$AGENT" ] || continue
        NAME=$(basename "$AGENT" .md)
        mkdir -p "$EXPECTED/skills/$NAME"
        cp --preserve=mode "$AGENT" "$EXPECTED/skills/$NAME/SKILL.md"
    done
fi

# --- 2. Compare or reconcile the generated trees, including nested orphans. ---
GENERATED="rules skills"
if [ -d "$DST" ]; then
    DIFF_OUTPUT=""
    for SUB in $GENERATED; do
        if [ -d "$DST/$SUB" ]; then
            DIFF_RC=0
            SUB_DIFF=$(diff -rq "$EXPECTED/$SUB" "$DST/$SUB") || DIFF_RC=$?
            [ "$DIFF_RC" -le 1 ] || die "diff could not compare $DST/$SUB (exit $DIFF_RC); nothing changed."
        else
            SUB_DIFF="Destination missing: $DST/$SUB"
        fi
        if [ -n "$SUB_DIFF" ]; then
            [ -z "$DIFF_OUTPUT" ] || DIFF_OUTPUT="${DIFF_OUTPUT}"$'\n'
            DIFF_OUTPUT="${DIFF_OUTPUT}${SUB_DIFF}"
        fi
    done
    MODE_DIFF=""
    MODES=$(find "$EXPECTED" -mindepth 1 -printf '%P\t%m\n') ||
        die "find failed while listing expected modes; nothing changed."
    MODES=$(printf '%s\n' "$MODES" | LC_ALL=C sort)
    while IFS=$'\t' read -r rel expected_mode; do
        [ -n "$rel" ] || continue
        [ -e "$DST/$rel" ] || continue
        actual_mode=$(stat -c '%a' "$DST/$rel") || die "stat failed on $DST/$rel; nothing changed."
        if [ "$expected_mode" != "$actual_mode" ]; then
            MODE_DIFF="${MODE_DIFF}Mode differs: $DST/$rel ($actual_mode != $expected_mode)"$'\n'
        fi
    done <<< "$MODES"
    if [ -n "$MODE_DIFF" ]; then
        [ -z "$DIFF_OUTPUT" ] || DIFF_OUTPUT="${DIFF_OUTPUT}"$'\n'
        DIFF_OUTPUT="${DIFF_OUTPUT}${MODE_DIFF%$'\n'}"
    fi
else
    DIFF_OUTPUT="Destination missing: $DST"
fi
# Counted and printed in the shell, so no pipeline stage can fail unseen.
CHANGED=0
while IFS= read -r LINE; do
    [ -z "$LINE" ] || CHANGED=$((CHANGED + 1))
done <<< "$DIFF_OUTPUT"

if [ "$DRY_RUN" -eq 1 ]; then
    while IFS= read -r LINE; do
        [ -z "$LINE" ] || echo "[DRY] $LINE"
    done <<< "$DIFF_OUTPUT"
else
    if [ -e "$DST" ] && [ ! -d "$DST" ]; then
        rm -f "$DST"
    fi
    mkdir -p "$DST"

    for SUB in $GENERATED; do
        if [ -d "$DST/$SUB" ]; then
            rm -r "${DST:?}/$SUB"
        elif [ -e "$DST/$SUB" ]; then
            rm -f "${DST:?}/$SUB"
        fi
        cp -R --preserve=mode "$EXPECTED/$SUB" "$DST"/
    done
    echo "[SYNC] exact .agents/rules and .agents/skills"
fi

if [ "$DRY_RUN" -eq 1 ]; then
    echo ""; echo "Dry run complete. $CHANGED change(s) detected."
else
    echo ""; echo "Sync complete. $CHANGED change(s) applied."
    echo "Note: .agents/rules and .agents/skills are generated. Edit their sources in .claude/."
fi
