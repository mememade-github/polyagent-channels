#!/bin/bash
# =============================================================================
# git-status.sh - Unified Git Status Viewer
# =============================================================================
# Purpose: Display git status for root and all subprojects in standardized format
# Usage: ./scripts/git/git-status.sh [--brief|--help]
# =============================================================================

set -e

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Color definitions
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# Output mode
OUTPUT_MODE="full"

log_header() { echo -e "\n${BOLD}${CYAN}═══════════════════════════════════════════════════════════════${NC}"; echo -e "${BOLD}${CYAN}  $1${NC}"; echo -e "${BOLD}${CYAN}═══════════════════════════════════════════════════════════════${NC}"; }
log_section() { echo -e "\n${BOLD}${BLUE}─── $1 ───${NC}"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# -----------------------------------------------------------------------------
# Single repository status
# -----------------------------------------------------------------------------
get_repo_status() {
    local repo_path="$1"
    local repo_name="$2"

    if [ ! -e "$repo_path/.git" ]; then
        echo "NOT_A_REPO"
        return 1
    fi

    local branch=$(git -C "$repo_path" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "N/A")
    local remote=$(git -C "$repo_path" remote get-url origin 2>/dev/null | sed 's/.*@//' | sed 's/github_pat_[^@]*@//' || echo "N/A")
    local last_commit_hash=$(git -C "$repo_path" rev-parse --short HEAD 2>/dev/null || echo "N/A")
    local last_commit_msg=$(git -C "$repo_path" log -1 --format='%s' 2>/dev/null || echo "N/A")
    local last_commit_date=$(git -C "$repo_path" log -1 --format='%ci' 2>/dev/null | cut -d' ' -f1 || echo "N/A")

    # Change counts
    local staged=$(git -C "$repo_path" diff --cached --numstat 2>/dev/null | wc -l)
    local unstaged=$(git -C "$repo_path" diff --numstat 2>/dev/null | wc -l)
    local untracked=$(git -C "$repo_path" ls-files --others --exclude-standard 2>/dev/null | wc -l)

    # Upstream status ($upstream empty when the branch has no tracking ref —
    # reported explicitly so "no upstream" is never conflated with "0 ahead/behind").
    local ahead=0
    local behind=0
    local upstream
    upstream=$(git -C "$repo_path" rev-parse --abbrev-ref @{upstream} 2>/dev/null || echo "")
    if [ -n "$upstream" ]; then
        ahead=$(git -C "$repo_path" rev-list --count @{upstream}..HEAD 2>/dev/null || echo 0)
        behind=$(git -C "$repo_path" rev-list --count HEAD..@{upstream} 2>/dev/null || echo 0)
    fi

    if [ "$OUTPUT_MODE" = "brief" ]; then
        local status_icon="✓"
        local status_color="$GREEN"
        if [ $staged -gt 0 ] || [ $unstaged -gt 0 ] || [ $untracked -gt 0 ]; then
            status_icon="●"
            status_color="$YELLOW"
        fi
        # Sync column: the skill contract asks brief mode to surface unpushed
        # commits and a missing upstream, so the values computed above are
        # printed here rather than discarded.
        local sync
        if [ -z "$upstream" ]; then
            sync="no upstream"
        elif [ $ahead -gt 0 ] && [ $behind -gt 0 ]; then
            sync="ahead $ahead behind $behind"
        elif [ $ahead -gt 0 ]; then
            sync="ahead $ahead"
        elif [ $behind -gt 0 ]; then
            sync="behind $behind"
        else
            sync="-"
        fi
        printf "${status_color}${status_icon}${NC} %-25s ${CYAN}%-10s${NC} %-18s %s %s\n" \
            "$repo_name" "$branch" "$sync" "$last_commit_hash" "$last_commit_msg"
    else
        log_section "$repo_name"
        echo -e "  ${BOLD}Branch:${NC}      $branch"
        echo -e "  ${BOLD}Remote:${NC}      $remote"
        echo -e "  ${BOLD}Last Commit:${NC} $last_commit_hash ($last_commit_date)"
        echo -e "  ${BOLD}Message:${NC}     $last_commit_msg"

        # Show changes
        if [ $staged -gt 0 ] || [ $unstaged -gt 0 ] || [ $untracked -gt 0 ]; then
            echo -e "  ${BOLD}Changes:${NC}"
            [ $staged -gt 0 ] && echo -e "    ${GREEN}Staged:${NC}    $staged file(s)"
            [ $unstaged -gt 0 ] && echo -e "    ${YELLOW}Unstaged:${NC}  $unstaged file(s)"
            [ $untracked -gt 0 ] && echo -e "    ${RED}Untracked:${NC} $untracked file(s)"
        else
            echo -e "  ${BOLD}Changes:${NC}     ${GREEN}Clean${NC}"
        fi

        # Sync status
        if [ -z "$upstream" ]; then
            echo -e "  ${BOLD}Sync:${NC}       ${YELLOW}no upstream (not tracking)${NC}"
        elif [ $ahead -gt 0 ] || [ $behind -gt 0 ]; then
            echo -e "  ${BOLD}Sync:${NC}"
            [ $ahead -gt 0 ] && echo -e "    ${CYAN}Ahead:${NC}  $ahead commit(s)"
            [ $behind -gt 0 ] && echo -e "    ${YELLOW}Behind:${NC} $behind commit(s)"
        fi
    fi

    # This function only prints; its status carries no meaning past the
    # NOT_A_REPO return above. Without this, `set -e` kills the caller's loop
    # whenever the last conditional echo above happens to be false.
    return 0
}

# -----------------------------------------------------------------------------
# All repositories status
# -----------------------------------------------------------------------------
get_all_status() {
    if [ "$OUTPUT_MODE" = "brief" ]; then
        echo -e "${BOLD}Project Status Summary${NC} ($(date '+%Y-%m-%d %H:%M'))"
        echo -e "──────────────────────────────────────────────────────────────────────────────"
        printf "  %-25s %-10s %-18s %s\n" "PROJECT" "BRANCH" "SYNC" "LAST COMMIT"
        echo -e "──────────────────────────────────────────────────────────────────────────────"
    else
        log_header "Git Repository Status"
        echo -e "  Timestamp: $(date '+%Y-%m-%d %H:%M:%S %Z')"
    fi

    # Root repository
    if [ -e "$ROOT_DIR/.git" ]; then
        get_repo_status "$ROOT_DIR" "root (workspace)"
    fi

    local gitdir repo name
    while IFS= read -r -d '' gitdir; do
        repo=$(dirname "$gitdir")
        [ "$repo" != "$ROOT_DIR" ] || continue
        name=${repo#"$ROOT_DIR/"}
        get_repo_status "$repo" "$name"
    done < <(find "$ROOT_DIR" \( -name node_modules -o -name .venv \) -prune -o -name .git -prune -print0 | sort -z)

    if [ "$OUTPUT_MODE" = "brief" ]; then
        echo -e "──────────────────────────────────────────────────────────────────────────────"
    else
        log_header "Status Complete"
    fi
}

# -----------------------------------------------------------------------------
# Usage
# -----------------------------------------------------------------------------
show_usage() {
    cat <<EOF
Usage: $0 [options]

Options:
  (none)      Full detailed status
  --brief     Brief summary (one line per repo)
  --help      Show this help

Examples:
  $0              # Full detailed status
  $0 --brief      # Quick summary
EOF
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
main() {
    case "${1:-}" in
        --brief)
            OUTPUT_MODE="brief"
            get_all_status
            ;;
        --help|-h)
            show_usage
            ;;
        "")
            OUTPUT_MODE="full"
            get_all_status
            ;;
        *)
            log_error "Unknown option: $1"
            show_usage
            exit 1
            ;;
    esac
}

main "$@"
