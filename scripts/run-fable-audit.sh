#!/bin/bash
# Run one Fable audit of this repository in a fresh Claude process. It starts
# outside the repository with no session persistence, no settings files and no
# link to the calling session, and reads the repository through --add-dir.
#
# Credential order:
#   1. The dedicated key FABLE_AUDIT_TOKEN in the ignored .env/fable-audit.env,
#      under an empty temporary config directory.
#   2. The session login, when the key file or value is missing, or when the
#      key is rejected with HTTP 401 (invalid, revoked or expired).
# Any other failure, such as a usage limit on the key, stops with an error
# instead of spending the session quota. The script never prints the key or
# places it in an environment variable or a command line.
#
# Output: the audit's final message, to stdout or [output-file]. The credential
# used goes to stderr. The auditor has Read and Bash on the repository, .env/
# included; this is not a security boundary.
set -euo pipefail

REPO_ROOT=$(realpath -- "$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")/..")
PROMPT_FILE="${1:-}"; OUTPUT_FILE="${2:-}"
[ -f "$PROMPT_FILE" ] ||
    { echo "Usage: $0 <prompt-file> [output-file]" >&2; exit 2; }
PROMPT_FILE=$(realpath -- "$PROMPT_FILE")
if [ -n "$OUTPUT_FILE" ]; then
    OUTPUT_FILE=$(realpath -m -- "$OUTPUT_FILE")
    [ "$OUTPUT_FILE" != "$PROMPT_FILE" ] ||
        { echo "ERROR: output-file must differ from prompt-file" >&2; exit 2; }
    case "$OUTPUT_FILE" in  # truncating below would destroy a key
        */.env|*/.env/*|*.env)
            echo "ERROR: output-file must not be an env or key file: $OUTPUT_FILE" >&2; exit 2 ;;
    esac
    EXISTING=$(dirname "$OUTPUT_FILE")
    while [ ! -d "$EXISTING" ]; do EXISTING=$(dirname "$EXISTING"); done
    if git -C "$EXISTING" rev-parse --is-inside-work-tree >/dev/null 2>&1 &&
        ! git -C "$EXISTING" check-ignore -q -- "$OUTPUT_FILE"; then
        echo "ERROR: in-repo output must be gitignored: $OUTPUT_FILE" >&2; exit 2
    fi
    mkdir -p "$(dirname "$OUTPUT_FILE")"
    : > "$OUTPUT_FILE"  # a stale report must not survive a failed run
fi

KEY_FILE="$REPO_ROOT/.env/fable-audit.env"
TOKEN=""
[ ! -f "$KEY_FILE" ] || TOKEN=$(awk '/^[ \t]*(export[ \t]+)?FABLE_AUDIT_TOKEN=/ {
    sub(/\r$/, ""); sub(/^[^=]*=/, ""); sub(/^["\047]/, ""); sub(/["\047]$/, ""); print; exit
    }' "$KEY_FILE")

WORK=$(mktemp -d)  # outside the repository: no project CLAUDE.md auto-load
trap 'rm -rf "$WORK"' EXIT
mkdir "$WORK/run" "$WORK/config"
# shellcheck disable=SC2054  # the commas are the CLI's own tool-list syntax
ARGS=(-p --output-format json --no-session-persistence --setting-sources ""
      --model fable --add-dir "$REPO_ROOT" --tools Read,Grep,Glob,Bash
      --allowedTools Read,Grep,Glob,Bash)
# Variables that tie a child to the calling Claude session.
SESSION_LINKS=(CLAUDECODE CLAUDE_CODE_CHILD_SESSION CLAUDE_CODE_ENTRYPOINT
      CLAUDE_CODE_EXECPATH CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN
      CLAUDE_CODE_SESSION_ATTENDED CLAUDE_CODE_SESSION_ID CLAUDE_EFFORT CLAUDE_PID)

run_fable() {  # $1 = key|session; result JSON goes to $WORK/result.json
    local rc=0
    (
        cd "$WORK/run" || exit 1; unset "${SESSION_LINKS[@]}"
        if [ "$1" = key ]; then
            # The key goes through a pipe on fd 3 (printf is a builtin), so it is
            # in no command line and in no environment the auditor's Bash sees.
            unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN
            export CLAUDE_CONFIG_DIR="$WORK/config" CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR=3
            exec claude "${ARGS[@]}" < "$PROMPT_FILE" > "$WORK/result.json" 3< <(printf '%s' "$TOKEN")
        fi
        exec claude "${ARGS[@]}" < "$PROMPT_FILE" > "$WORK/result.json"
    ) || rc=$?
    if ! { [ -s "$WORK/result.json" ] &&
        jq -e 'type == "object" and has("is_error")' "$WORK/result.json" >/dev/null 2>&1; }; then
        echo "ERROR: claude ($1 credential) exited $rc without a JSON result" >&2; exit 1
    fi
}

CREDENTIAL=session
if [ -n "$TOKEN" ]; then
    run_fable key; CREDENTIAL=key
    if [ "$(jq -r '.api_error_status // empty' "$WORK/result.json")" = 401 ]; then
        echo "fable-audit: dedicated key rejected (401); using the session login" >&2
        run_fable session; CREDENTIAL=session
    fi
else
    echo "fable-audit: no FABLE_AUDIT_TOKEN value in $KEY_FILE; using the session login" >&2
    run_fable session
fi

MODELS=$(jq -r '.modelUsage // {} | keys | join(",")' "$WORK/result.json")
echo "fable-audit: credential=$CREDENTIAL model=${MODELS:-none}" >&2
if [ "$(jq -r '.is_error' "$WORK/result.json")" != false ]; then
    echo "ERROR: $(jq -r '.result // "no result"' "$WORK/result.json")" >&2; exit 1
fi
jq -e '.result | type == "string" and length > 0' "$WORK/result.json" >/dev/null ||
    { echo "ERROR: the audit returned an empty result" >&2; exit 1; }
if [ -n "$OUTPUT_FILE" ]; then
    jq -r '.result' "$WORK/result.json" > "$OUTPUT_FILE"
else
    jq -r '.result' "$WORK/result.json"
fi
