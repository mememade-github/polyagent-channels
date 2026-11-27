#!/bin/bash
# Pre-push gate (PreToolUse, matcher: Bash): a policy tripwire, not a security
# sandbox. It judges the command TEXT, not what the shell will run; REFERENCE.md
# §Hooks states the matching rules and their limits. Exit codes:
#   2  blocked (stderr is the reason): a credential pattern in the raw text of
#      ANY command (inline token-shaped fixture text blocks too; keep it in
#      files), or, for push-shaped text, a literal credential in the target
#      repo's remote/url/credential config. Force pushes are not judged.
#   0  not blocked. Not an approval and not evidence that the command is safe.
#   1  not checked: one line "pre-push gate: not checked: <cause>" on stderr.
#      Claude Code treats it as a non-blocking error, so the command still runs.
set -uf

not_checked() { echo "pre-push gate: not checked: $1" >&2; exit 1; }
# match <regex> <text>: grep -Eq, where an exit above 1 is a grep error, not "no match".
match() { printf '%s' "$2" | grep -Eq "$1"; local rc=$?; [ "$rc" -le 1 ] || not_checked "grep exited $rc"; return "$rc"; }

INPUT=$(cat) || not_checked "could not read the hook input"
command -v jq >/dev/null 2>&1 || not_checked "jq not found"
command -v git >/dev/null 2>&1 || not_checked "git not found"
COMMAND=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null) || not_checked "jq could not read the hook input as JSON"
[ -n "$COMMAND" ] || not_checked "no tool_input.command in the hook input"

# Block: credential-looking secret in the raw command text.
CRED_RE='ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,}|oauth2:[^@[:space:]]+@|x-access-token:[^@[:space:]]+@|[A-Za-z][A-Za-z0-9+.-]*://[^/@:[:space:]]+:[^/@[:space:]]+@'
if match "$CRED_RE" "$COMMAND"; then
  echo "Blocked: the command text matches a credential-looking secret." >&2
  echo "Keep tokens in gitignored files; supply them via a credential helper at call time." >&2
  exit 2
fi

# Everything below applies only to `git ... push` text at the start or after
# ; | & ( or whitespace, in a view with quoted spans removed: text inside
# quotes is ignored, while unquoted push text matches even as an argument.
QUOTELESS=$(printf '%s' "$COMMAND" | sed -E "s/'[^']*'//g; s/\"[^\"]*\"//g") || not_checked "sed failed"
match '(^|[;&|([:space:]])git[[:space:]]([^;|&]*[[:space:]])?push([[:space:];|&)]|$)' "$QUOTELESS" || exit 0

# check_target <text>: the target repo is the text's `git -C <path>` up to the
# first space ; | or &, quotes removed (a path with a space is cut there; the
# repo containing the cut path is used), else the session dir. Exits 2 when
# that repo's config stores a literal credential; when no repo is found, the
# path is kept for the not-checked line.
UNRESOLVED=""
check_target() {
  local TARGET_DIR REPO_ROOT CONFIG STORED KEYS rc
  TARGET_DIR=$(printf '%s' "$1" | sed -nE 's/.*git[[:space:]]+-C[[:space:]]+([^[:space:];|&]+).*/\1/p') || not_checked "sed failed"
  TARGET_DIR=${TARGET_DIR%%$'\n'*}
  TARGET_DIR=${TARGET_DIR//[\"\']/}; case "$TARGET_DIR" in '$PWD'|'${PWD}'|'$(pwd)') TARGET_DIR=$PWD ;; '$CLAUDE_PROJECT_DIR'|'${CLAUDE_PROJECT_DIR}') TARGET_DIR="${CLAUDE_PROJECT_DIR:-.}" ;; esac
  [ -n "$TARGET_DIR" ] || TARGET_DIR="${CLAUDE_PROJECT_DIR:-.}"
  REPO_ROOT=$(git -C "$TARGET_DIR" rev-parse --show-toplevel 2>/dev/null) || { [ -n "$UNRESOLVED" ] || UNRESOLVED=$TARGET_DIR; return 0; }

  # Block: stored-config credential (remote URLs, credential-helper values).
  # git config exits 1 when no key matches; any other non-zero status is an error.
  CONFIG=$(git -C "$REPO_ROOT" config --get-regexp '^(remote\.|credential\.|url\.)' 2>/dev/null); rc=$?
  [ "$rc" -le 1 ] || not_checked "git config exited $rc"
  STORED=$(printf '%s\n' "$CONFIG" | grep -E "$CRED_RE"); rc=$?
  [ "$rc" -le 1 ] || not_checked "grep exited $rc"
  if [ -n "$STORED" ]; then
    echo "Blocked: git config visible from $REPO_ROOT stores a literal credential in:" >&2
    KEYS=$(printf '%s\n' "$STORED" | awk '!seen[$1]++ {print "  " $1}') || KEYS="  (could not list the keys: awk failed)"
    printf '%s\n' "$KEYS" >&2
    echo "Fix: keep remote URLs clean; let a credential helper read the token from a gitignored file (variable-reference helper values pass)." >&2
    exit 2
  fi
}

# Check each piece of the text between ; | & ( ) and newlines that is push-shaped
# with its quoted spans removed, so each push is checked against its own
# `git -C`. When no single piece is push-shaped (e.g. `git $(...) push`), the
# whole text is checked as one piece.
PIECES=$(printf '%s' "$COMMAND" | tr ';|&()' '\n\n\n\n\n') || not_checked "tr failed"
FOUND=""
while IFS= read -r P; do
  Q=$(printf '%s' "$P" | sed -E "s/'[^']*'//g; s/\"[^\"]*\"//g") || not_checked "sed failed"
  match '(^|[[:space:]])git[[:space:]](.*[[:space:]])?push([[:space:]]|$)' "$Q" || continue
  FOUND=1
  check_target "$P"
done <<< "$PIECES"
[ -n "$FOUND" ] || check_target "$COMMAND"
[ -z "$UNRESOLVED" ] || not_checked "no git repository found at $UNRESOLVED"
exit 0
