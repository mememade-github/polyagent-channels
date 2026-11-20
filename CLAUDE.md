# CLAUDE.md — Project Workspace

## Context

- Workspace: `/workspaces/`; environment: Ubuntu 22.04 DevContainer, user `vscode`.
- `.claude/` is ground truth; `.agents/rules/` and `.agents/skills/` are its generated mirror for Codex and Antigravity, the other `.agents/` files are hand-maintained Antigravity configuration, and `.codex/` holds Codex-specific configuration.
- Claude is native/auto-updated at `~/.local/bin/claude`; Codex is npm-global at `~/.npm-global/bin/codex`; `PROJECT_NODE_VERSION` adds project Node; 9p `postStartCommand` sets `core.filemode=false`; project rules: `.claude/rules/project/`; Git helpers: `scripts/git/`.
- [PROJECT.md](PROJECT.md) supplies product context. Read [REFERENCE.md](REFERENCE.md) when the task needs commands, configuration, or troubleshooting.

## Trust model: advisory gates

The container and its host-mounted `docker.sock` are not a security sandbox (see [REFERENCE.md](REFERENCE.md) §Privilege boundary). Hooks are policy tripwires: they block only positive matches and fail open on ambiguity. Positive matches are commit bypass (`--no-verify`/`-n`), credential patterns in command text or push configuration, force push, or a missing/stale marker. `completion-checker.sh` writes a per-branch marker; the pre-commit gate accepts it by existence and age (under 24 hours), not by content fingerprint. Do not commit credentials or run untrusted code expecting host isolation.

## Automated workflow

- At session start, check `MEMORY.md`; if the hook reports WIP and the current request permits resuming saved work, read the relevant WIP `README.md` and continue its first actionable item. Otherwise follow the current request, and wait only if no task has been supplied.
- The request or approved plan sets scope. Keep changes surgical; report unrelated issues. Commit tests only when requested or customary here; keep scratch checks outside the repository.
- Every claim must be verified by execution; this does not expand the request's check scope or repository-defined tier timing.
- Ground progress and completion claims in tool results from this session. State failed or skipped checks plainly.
- Get explicit approval before `rm -rf`, overwriting `mv`/`cp`, `git push --force`, `git reset --hard`, or database `DROP`/`DELETE`.
- Fix root causes — diagnose across infra/config/deploy/code; no workarounds.
- Explicit failure — every operation must succeed or fail visibly.
- Use `/refine` for meaningful changes and direct editing for trivial ones. Evaluation runs in a fresh evaluator context, never the modifier's context.
- For work likely to span sessions, create a WIP via the **wip-manager** agent at `wip/task-YYYYMMDD-description/README.md`; delete the WIP directory when complete.
- Before an agent-issued commit, `bash scripts/meta/completion-checker.sh` must pass; it is environment-independent across fresh clones, CI, and temp checkouts. Never bypass verification. Run the docker-backed `.devcontainer/verify-template.sh` on demand and in CI, not per commit.

## Mirror and communication

Edit `.claude/`, then run `bash scripts/sync-agents-mirror.sh`; use `--dry` to check drift. Do not edit `.agents/rules/` or `.agents/skills/` by hand. Respond in the user's language unless this file sets a team language.

## Behavioral foundation

The imported rules derive from [forrestchang/andrej-karpathy-skills](https://github.com/forrestchang/andrej-karpathy-skills) (MIT).

@.claude/rules/behavioral-core.md
@.claude/rules/devcontainer-patterns.md
@PROJECT.md
