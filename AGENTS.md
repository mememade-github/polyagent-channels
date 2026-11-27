# AGENTS.md — Shared governance for Claude Code, Codex CLI, and Antigravity CLI

## Context

- Workspace: `/workspaces/`; environment: Ubuntu 22.04 DevContainer, user `vscode`.
- Under Docker's default seccomp profile, which this DevContainer keeps, bubblewrap cannot create user namespaces, so Codex shell execution here uses `--dangerously-bypass-approvals-and-sandbox` as a compatibility measure. Commands then run unsandboxed with the container's access, including the host Docker socket; it is not a security boundary.
- This file is the single hand-maintained source of the shared duties and behavioral rules for all three vendors; `CLAUDE.md` imports it for Claude Code. `.claude/` is the source of the rules, skills, and agent roles that `scripts/sync-agents-mirror.sh` copies into the generated `.agents/rules/` and `.agents/skills/`. `.agents/hooks.json`, `.agents/hooks/`, and `.agents/rules.json` are hand-maintained Antigravity configuration, and `.codex/` holds hand-maintained Codex configuration.
- Claude is native/auto-updated at `~/.local/bin/claude`; Codex is npm-global at `~/.npm-global/bin/codex`; Antigravity (`agy`) is a self-updating native binary at `~/.local/bin/agy`; `PROJECT_NODE_VERSION` adds project Node; on every start `setup-env.sh` sets `core.filemode=false` in each repository under `/workspaces` (path A's `postStartCommand` repeats it in `/workspaces`), for mounts such as 9p whose file modes Git would otherwise report as changes, so Git ignores executable-bit changes there on every filesystem; project rules: `.claude/rules/project/`; Git helpers: `scripts/git/`.
- Claude Code reads this file and [PROJECT.md](PROJECT.md) through the imports in `CLAUDE.md` and loads `.claude/rules/`, including `.claude/rules/project/`, by itself. Codex loads only this file and does not follow `@` imports: on Codex, at session start, read [PROJECT.md](PROJECT.md) and every `.md` file under `.agents/rules/project/`, including subfolders. Antigravity loads `.agents/rules/project/` through `.agents/rules.json` and receives [PROJECT.md](PROJECT.md) in the session-start message from the hook adapter (REFERENCE §Hooks). Read [REFERENCE.md](REFERENCE.md) when the task needs commands, configuration, or troubleshooting.

## Trust model: advisory gates

The container and its host-mounted `docker.sock` are not a security sandbox (see [REFERENCE.md](REFERENCE.md) §Privilege boundary). Hooks are policy tripwires. The pre-push gate reads the command text, not what the shell will run, and blocks only on a match: a credential pattern in the command text or in the push target's git config. Not being blocked is not an approval or evidence of safety. When the gate cannot check a command, it lets the command run and writes `pre-push gate: not checked: <cause>` to stderr, which did not reach the agent in the observed sessions (REFERENCE §Hooks says where each vendor put it). Do not commit credentials or run untrusted code expecting host isolation.

In an untrusted project Codex runs neither the project hooks nor the project `.codex/config.toml`, and `--dangerously-bypass-hook-trust` does not change that. The Codex hooks documentation says project hooks load only from a trusted project and each must also be reviewed and trusted in `/hooks`; new or changed hooks are skipped until then. Until both are done, run the pre-push gate manually.

## Automated workflow

- At session start, if the hook reports WIP and the current request permits resuming saved work, read the relevant WIP `README.md` and continue its first actionable item. Otherwise follow the current request, and wait only if no task has been supplied.
- The request or approved plan sets scope; report unrelated issues. Commit tests only when requested or customary here; keep scratch checks outside the repository.
- Base judgments, actions, and reports on actual facts relevant to authorized requirements; substantiate behavioral claims by executing the relevant behavior and observing results at the claimed boundary. Substantiate structural claims (path, symbol, reference) by reading the source. This does not expand the request's check scope or repository-defined tier timing.
- String searches, pattern/byte/count comparisons, syntax passes, and completed checklists establish only the properties actually checked; do not substitute these formal results for evidence of meaning, compliance, or behavior they did not examine.
- Create, retain, or enforce checks only to obtain relevant evidence for authorized requirements, and make them gates only when those requirements are prerequisites for the work they block.
- On failure, distinguish an actual requirement violation, a faulty or inapplicable check, and missing evidence; act within scope on what is established, and report what remains unobserved.
- Ground progress and completion claims in tool results from this session. State failed or skipped checks plainly.
- After a change, the documents, rules, and implementation it touches must direct the same action for the same request and conditions; where they disagree, decide by their purpose which side to change. Write a statement only if an agent's or operator's action would differ without it, state it in one place, and leave one-time history to the commit message.
- Get explicit approval before `rm -rf`, overwriting `mv`/`cp`, `git push --force`, `git reset --hard`, or database `DROP`/`DELETE`.
- Fix root causes — diagnose across infra/config/deploy/code; do not hide a defect behind a workaround. A compatibility measure that an environment constraint requires is allowed when the condition under which it applies and its remaining limits are stated where it is applied.
- Explicit failure — every operation must succeed or fail visibly.
- Before a meaningful change, state the authorized requirement and acceptance conditions; make trivial edits directly. A change is meaningful when it alters behavior, a duty, an interface, an operating procedure, or a generation relationship, including in documentation alone; a typo fix that changes no meaning is a trivial edit. For that change, have an evaluator in a fresh context, never the modifier's, judge the actual diff against those conditions using observed evidence and the permitted inputs in the evaluator role (`.claude/agents/evaluator.md`, generated as `.agents/skills/evaluator/SKILL.md`), using a fresh **evaluator** subagent on Claude Code, `scripts/meta/run-isolated-role.sh evaluate` on Codex, or a fresh built-in `self` subagent on Antigravity. The evaluation is incomplete when no fresh evaluator context can be created, when the evaluator leaves no report, or when its report does not judge the actual diff; report it as incomplete and do not treat the change as evaluated. Repeat only for an evidenced gap in the authorized outcome; no count, score, or pass rate decides acceptance.
- Invoke `status` for repository/worktree/WIP/upstream state and `verify` for the fast checks.
- For work likely to span sessions, manage the work at `wip/task-YYYYMMDD-description/README.md` with the wip-manager role (the **wip-manager** agent on Claude Code; load `.agents/skills/wip-manager/SKILL.md` on Codex and Antigravity); delete the WIP directory when complete.
- When a change touches what `bash scripts/meta/completion-checker.sh` checks (shell syntax, the hook and rule JSON, the Codex TOML, the tracked-content leak scan, mirror drift), run it; it needs no Docker or network and checks the checkout that contains the script, including a fresh clone, temp checkout, or worktree. Report which checks ran, which failed, and which were not checked; a pass establishes only the checks it names. Do not skip a repository's own Git hooks with `--no-verify`. Run `.devcontainer/verify-template.sh` on demand, not per commit; it runs the fast checker once and feeds synthetic input to the hook scripts and the Antigravity adapter, and it observes no container start and no agent session.

## Antigravity

Keep `trigger: always_on` frontmatter on source rules; `.agents/rules.json` registers `.agents/rules/project`, and any deeper rule folder must be registered there.

## Mirror and communication

Edit `.claude/`, then run `bash scripts/sync-agents-mirror.sh`; use `--dry` to check drift. Do not edit `.agents/rules/` or `.agents/skills/` by hand; the sync leaves the other `.agents/` files untouched. Codex uses skill names and descriptions for discovery; Claude-specific role metadata such as tools, model, and color does not configure the Codex role. Respond in the user's language unless this file sets a team language.

## Behavioral rules

The four rules below are adapted from [Andrej Karpathy's observations](https://x.com/karpathy/status/2015883857489522876) as packaged in [forrestchang/andrej-karpathy-skills](https://github.com/forrestchang/andrej-karpathy-skills) (MIT). The other duties in this file are this template's own.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

### 1. Think Before Coding

State assumptions that affect the approach. If different readings would materially change the result, present them or ask. Say when a simpler approach is sufficient.

### 2. Simplicity First

Implement only requested behavior. Do not add speculative features, abstractions, configuration, or handling for impossible cases.

### 3. Surgical Changes

Read before editing, match existing style, and change only lines traceable to the request. Leave unrelated cleanup alone; remove only orphans your change creates.

### 4. Goal-Driven Execution

Turn the request into verifiable outcomes and run the relevant checks before claiming completion.
