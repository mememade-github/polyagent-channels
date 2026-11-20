#!/bin/bash
# =============================================================================
# karpathy-consistency-check.sh — Behavioral-foundation mirror oracle
# =============================================================================
# Closes AUD-2026-018: automates the behavioral-core.md <-> karpathy SKILL.md
# consistency comparison that the source-of-truth blockquote requires.
#
# Usage:
#   bash scripts/meta/karpathy-consistency-check.sh [ROOT]
#
# Canonical body policy: the synchronized region is "## 1." -> EOF (Rules 1-4
#   plus the closing self-test coda). Everything before "## 1." (frontmatter,
#   title, source-of-truth / skill-handle blockquote, attribution, source link)
#   may legitimately differ. A skill-only footer after the coda is NOT allowed
#   (it would make the extractor outputs diverge).
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Shared worktree-aware root detection — single source of truth (see lib/).
_LIB="$SCRIPT_DIR/lib/detect-root.sh"
[ -r "$_LIB" ] || { echo "FATAL: missing $_LIB" >&2; exit 3; }
# shellcheck source=scripts/meta/lib/detect-root.sh
. "$_LIB"

ROOT="$(detect_root "${1:-}")"
INVARIANT='Rules 1–4 and the closing self-test stay synchronized; only frontmatter, title, attribution, and source-link text may differ.'
CODA='**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.'

canonical_karpathy_body() { awk '/^## 1\. /{flag=1} flag' "$1"; }

FAIL=0
note_pass() { echo "[PASS] $1"; }
note_fail() { echo "[FAIL] $1"; FAIL=$((FAIL + 1)); }

# --- The four canonical paths, literal ---
mapfile -t BC_FILES < <(ls -1 "$ROOT"/.agents/rules/behavioral-core.md "$ROOT"/.claude/rules/behavioral-core.md 2>/dev/null)
mapfile -t SK_FILES < <(ls -1 "$ROOT"/.agents/skills/karpathy-guidelines/SKILL.md "$ROOT"/.claude/skills/karpathy-guidelines/SKILL.md 2>/dev/null)

echo "=== karpathy-consistency-check  root=$ROOT ==="
echo "behavioral-core=${#BC_FILES[@]}  SKILL=${#SK_FILES[@]}"

# --- 1. Pair present (count-agnostic) ---
[ "${#BC_FILES[@]}" -ge 1 ] && [ "${#SK_FILES[@]}" -ge 1 ] && note_pass "pair present (bc=${#BC_FILES[@]} skill=${#SK_FILES[@]})" || note_fail "behavioral-core/SKILL pair missing"

if [ "${#BC_FILES[@]}" -eq 0 ] || [ "${#SK_FILES[@]}" -eq 0 ]; then
    echo "=== RESULT: FAIL (no files enumerated) ==="; exit 1
fi

# --- 4. All canonical bodies identical to the reference (first behavioral-core) ---
# Compare extractor-output to extractor-output (awk vs awk): identical newline
# semantics. A $(...)-stored reference would strip trailing newlines and
# mis-report every file as differing — compare files, not captured strings.
REF_FILE="${BC_FILES[0]}"
BODY_OK=1
for f in "${BC_FILES[@]}" "${SK_FILES[@]}"; do
    if ! diff -q <(canonical_karpathy_body "$REF_FILE") <(canonical_karpathy_body "$f") >/dev/null 2>&1; then
        note_fail "canonical body differs: $f"; BODY_OK=0
    fi
done
[ "$BODY_OK" -eq 1 ] && note_pass "all ${#BC_FILES[@]}+${#SK_FILES[@]} canonical bodies identical"

# --- 5. New invariant sentence present in every bc + skill header ---
INV_OK=1
for f in "${BC_FILES[@]}" "${SK_FILES[@]}"; do
    grep -qF "$INVARIANT" "$f" || { note_fail "invariant sentence missing: $f"; INV_OK=0; }
done
[ "$INV_OK" -eq 1 ] && note_pass "invariant sentence present in all bc+skill"

# --- 7. Closing coda present in every bc + skill ---
CODA_OK=1
for f in "${BC_FILES[@]}" "${SK_FILES[@]}"; do
    grep -qF "$CODA" "$f" || { note_fail "coda missing: $f"; CODA_OK=0; }
done
[ "$CODA_OK" -eq 1 ] && note_pass "closing coda present in all bc+skill"

echo "=== RESULT: $([ "$FAIL" -eq 0 ] && echo PASS || echo "FAIL ($FAIL)") ==="
[ "$FAIL" -eq 0 ]
