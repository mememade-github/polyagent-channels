# AGENTS.md — Codex CLI and Antigravity CLI governance

## Context

- Workspace: `/workspaces/`; environment: Ubuntu 22.04 DevContainer, user `vscode`.
- In this DevContainer, bubblewrap cannot create user namespaces; Codex shell execution uses `--dangerously-bypass-approvals-and-sandbox` as a compatibility fallback, not a security boundary.
- `.claude/` is ground truth; `.agents/rules/` and `.agents/skills/` are its byte-faithful generated mirror. `.agents/hooks.json`, `.agents/hooks/`, and `.agents/rules.json` are hand-maintained Antigravity configuration, and `.codex/` holds Codex-specific configuration.
- Claude is native/auto-updated at `~/.local/bin/claude`; Codex is npm-global at `~/.npm-global/bin/codex`; Antigravity (`agy`) is a self-updating native binary at `~/.local/bin/agy`; `PROJECT_NODE_VERSION` adds project Node; 9p `postStartCommand` sets `core.filemode=false`; project rules: `.claude/rules/project/`; Git helpers: `scripts/git/`.
- Codex does not follow `@import`. On Codex, at session start, read [`.agents/rules/behavioral-core.md`](.agents/rules/behavioral-core.md), [`.agents/rules/devcontainer-patterns.md`](.agents/rules/devcontainer-patterns.md), and [PROJECT.md](PROJECT.md). Read [REFERENCE.md](REFERENCE.md) when the task needs commands, configuration, or troubleshooting.

## Trust model: advisory gates

The container and its host-mounted `docker.sock` are not a security sandbox (see [REFERENCE.md](REFERENCE.md) §Privilege boundary). Hooks are policy tripwires: they block only positive matches and fail open on ambiguity. Positive matches are commit bypass (`--no-verify`/`-n`), credential patterns in command text or push configuration, force push, or a missing/stale marker. `completion-checker.sh` writes a per-branch marker; the pre-commit gate accepts it by existence and age (under 24 hours), not by content fingerprint. Do not commit credentials or run untrusted code expecting host isolation.

Codex command hooks run only after project trust and `/hooks` review; changed hooks require review. Until then, run the gates manually; use `--dangerously-bypass-hook-trust` only for automation that independently vets them. Codex hook matchers inspect `Bash`; accepted editor tool names are `Bash`, `apply_patch`, `Edit`, and `Write`.

## Automated workflow

- At session start, check `MEMORY.md`; if the hook reports WIP and the current request permits resuming saved work, read the relevant WIP `README.md` and continue its first actionable item. Otherwise follow the current request, and wait only if no task has been supplied.
- The request or approved plan sets scope. Keep changes surgical; report unrelated issues. Commit tests only when requested or customary here; keep scratch checks outside the repository.
- Every claim must be verified by execution; this does not expand the request's check scope or repository-defined tier timing.
- Ground progress and completion claims in tool results from this session. State failed or skipped checks plainly.
- Get explicit approval before `rm -rf`, overwriting `mv`/`cp`, `git push --force`, `git reset --hard`, or database `DROP`/`DELETE`.
- Fix root causes — diagnose across infra/config/deploy/code; no workarounds.
- Explicit failure — every operation must succeed or fail visibly.
- Use the `refine` skill for meaningful changes and direct editing for trivial ones. Evaluation never runs in the modifier's context. On Codex, run it through `scripts/meta/run-isolated-role.sh` in a fresh `codex exec --ephemeral` context with the exact Contract/diff-only evidence channel. On Antigravity, run it in a fresh built-in `self` subagent given only the evaluator role body and the permitted evidence.
- Invoke `status` for repository/worktree/WIP/upstream/marker state, `verify` for pre-commit checks, and `karpathy-guidelines` when writing, reviewing, or refactoring code.
- For work likely to span sessions, load `.agents/skills/wip-manager/SKILL.md` and use it to manage the work at `wip/task-YYYYMMDD-description/README.md`; delete the WIP directory when complete.
- Before an agent-issued commit, `bash scripts/meta/completion-checker.sh` must pass; it is environment-independent across fresh clones, CI, and temp checkouts. Never bypass verification. Run the docker-backed `.devcontainer/verify-template.sh` on demand and in CI, not per commit.

## Antigravity

Keep `trigger: always_on` frontmatter on source rules; `.agents/rules.json` registers `.agents/rules/project`, and any deeper rule folder must be registered there. `/refine` runs Audit, Modify, and Evaluate in separate fresh built-in `self` subagents that retain repository rules and hooks without the parent conversation.

## Mirror and communication

Edit `.claude/`, then run `bash scripts/sync-agents-mirror.sh`; use `--dry` to check drift. Do not edit `.agents/rules/` or `.agents/skills/` by hand; the sync leaves the other `.agents/` files untouched. Codex uses skill names and descriptions for discovery; Claude-specific role metadata such as tools, model, and color does not configure the Codex role. Respond in the user's language unless this file sets a team language.

## Behavioral foundation

The rules derive from [forrestchang/andrej-karpathy-skills](https://github.com/forrestchang/andrej-karpathy-skills) (MIT).
