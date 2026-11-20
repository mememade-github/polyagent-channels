#!/bin/bash
# PreToolUse hook: git push safety gate for the Codex harness — a policy
# tripwire, not a security sandbox (mirror of the Claude gate). Positive-match
# blocks only (exit 2); anything else — parse ambiguity, non-push commands,
# missing tools, internal errors — exits 0 (fail-open by design; the container
# is a workspace boundary, not a trust boundary).
#
# Blocks:
#  1. Clear plain force push (--force / -f / +refspec) without a scoped
#     single-use approval marker in the target repo root
#     (.codex/state/allow-force-push.<remote>.<branch>), consumed on allow.
#     Decision: --force-with-lease passes WITHOUT a marker — it is the
#     narrower alternative and fails
#     instead of clobbering remote work it has not seen.
#  2. Credential-looking secret in the raw command text of ANY command.
#     Accepted cost: token-shaped fixture/doc text inline in a command also
#     blocks — keep such strings in files.
#  3. Stored-config credential visible from the resolved target repo:
#     remote/url/credential config values carrying a literal token or
#     user:pass@. A helper value that only references a shell variable
#     (e.g. "password=${GITHUB_PAT}") is the sanctioned pattern and passes.
set -uf

INPUT=$(cat) || exit 0
command -v jq >/dev/null 2>&1 || exit 0
command -v git >/dev/null 2>&1 || exit 0
COMMAND=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null) || exit 0
[ -n "$COMMAND" ] || exit 0

# Block 2: credential-looking secret in the raw command text.
CRED_RE='ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,}|oauth2:[^@[:space:]]+@|x-access-token:[^@[:space:]]+@|[A-Za-z][A-Za-z0-9+.-]*://[^/@:[:space:]]+:[^/@[:space:]]+@'
if printf '%s' "$COMMAND" | grep -Eq "$CRED_RE"; then
  echo "Blocked: credential-looking secret in the command text." >&2
  echo "Keep tokens in gitignored files; supply them via a credential helper at call time." >&2
  exit 2
fi

# Everything below applies only to a clear `git ... push` in command position
# (start or after ; | & ( or whitespace) in a quote-stripped view: quoted spans
# are removed first, so force-looking text inside string arguments never matches.
QUOTELESS=$(printf '%s' "$COMMAND" | sed -E "s/'[^']*'//g; s/\"[^\"]*\"//g")
printf '%s' "$QUOTELESS" | grep -Eq '(^|[;&|([:space:]])git[[:space:]]([^;|&]*[[:space:]])?push([[:space:];|&)]|$)' || exit 0

# Resolve the target repo: honor a clear `git -C <path>`, else the session dir.
TARGET_DIR=$(printf '%s' "$COMMAND" | sed -nE 's/.*git[[:space:]]+-C[[:space:]]+([^[:space:];|&]+).*/\1/p' | head -1)
TARGET_DIR=${TARGET_DIR//[\"\']/}; case "$TARGET_DIR" in '$PWD'|'${PWD}'|'$(pwd)') TARGET_DIR=$PWD ;; '$CODEX_PROJECT_DIR'|'${CODEX_PROJECT_DIR}') TARGET_DIR="${CODEX_PROJECT_DIR:-.}" ;; esac
[ -n "$TARGET_DIR" ] || TARGET_DIR="${CODEX_PROJECT_DIR:-.}"
REPO_ROOT=$(git -C "$TARGET_DIR" rev-parse --show-toplevel 2>/dev/null) || exit 0

# Block 3: stored-config credential (remote URLs, credential-helper values).
STORED=$(git -C "$REPO_ROOT" config --get-regexp '^(remote\.|credential\.|url\.)' 2>/dev/null | grep -E "$CRED_RE" || true)
if [ -n "$STORED" ]; then
  echo "Blocked: git config visible from $REPO_ROOT stores a literal credential in:" >&2
  printf '%s\n' "$STORED" | awk '{print "  " $1}' | sort -u >&2
  echo "Fix: keep remote URLs clean; let a credential helper read the token from a gitignored file (variable-reference helper values pass)." >&2
  exit 2
fi

# Block 1: clear plain force push requires a scoped single-use marker.
TAIL=$(printf '%s' "$QUOTELESS" | tr '\n' ';' | sed -E 's/.*\bpush\b//; s/[;|&()].*//')
printf '%s' "$TAIL" | grep -Eq '(^|[[:space:]])(--force|-[A-Za-z]*f[A-Za-z]*|\+[A-Za-z0-9][^[:space:]]*)([[:space:]]|$)' || exit 0

REMOTE=""
for w in $TAIL; do case "$w" in -*|+*) ;; *) REMOTE="$w"; break ;; esac; done
[ -n "$REMOTE" ] || REMOTE=$(git -C "$REPO_ROOT" rev-parse --abbrev-ref '@{upstream}' 2>/dev/null | cut -d/ -f1)
[ -n "$REMOTE" ] || REMOTE=origin
BRANCH=$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
[ -n "$BRANCH" ] || BRANCH=unknown
R=$(printf '%s' "$REMOTE" | sed -E 's/[^A-Za-z0-9._-]+/_/g')
B=$(printf '%s' "$BRANCH" | sed -E 's/[^A-Za-z0-9._-]+/_/g')
MARKER="$REPO_ROOT/.codex/state/allow-force-push.${R:-unnamed}.${B:-unnamed}"
if [ -f "$MARKER" ]; then
  rm -f "$MARKER"
  exit 0
fi
echo "Blocked: force push requires explicit single-use approval (remote '$REMOTE', branch '$BRANCH')." >&2
echo "Narrower alternative: --force-with-lease passes without a marker." >&2
echo "To approve exactly once, create the marker file and retry:" >&2
echo "  mkdir -p '$REPO_ROOT/.codex/state' && touch '$MARKER'" >&2
exit 2
