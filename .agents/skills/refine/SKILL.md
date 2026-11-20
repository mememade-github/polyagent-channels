---
name: refine
description: Autonomous exploratory improvement loop — thin orchestrator with fresh-context agents
argument-hint: "<task-description> [--max-iter N] [--threshold 0.85] [--project PATH] [--agent TYPE]"
user-invocable: true
allowed-tools: Bash, Read, Write, Edit, Grep, Glob, Agent
---

# /refine — Autonomous Exploratory Improvement Loop

Run a modify → evaluate → keep/discard loop. Audit, Modify, and Evaluate use fresh contexts so evidence and scoring remain isolated. Each iteration addresses the highest-priority remaining gap.

## Arguments

- `<task-description>` (required) — what to improve (may be broad).
- `--max-iter N` (default 10) · `--threshold T` (default 0.85) ·
  `--project PATH` (default: resolved root) · `--agent TYPE` (default
  general-purpose, used for the Modify step).

## Step 0a: Vendor and path resolution

Marker/attempts/output paths resolve to match whichever host's Stop-hook gate is
watching, so the mirrored Codex copy works without `$CLAUDE_PROJECT_DIR`:

```bash
if [ "${ANTIGRAVITY_AGENT:-}" = "1" ]; then
  AGENT_VENDOR=antigravity
  PROJECT="${PROJECT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
elif [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then
  AGENT_VENDOR=claude
elif [ -n "${CODEX_PROJECT_DIR:-}" ] || [ "${CODEX_CI:-}" = "1" ] || [ -n "${CODEX_THREAD_ID:-}" ]; then
  AGENT_VENDOR=codex
elif [ "${AGENT_VENDOR:-}" != "claude" ] && [ "${AGENT_VENDOR:-}" != "codex" ] && [ "${AGENT_VENDOR:-}" != "antigravity" ]; then
  echo "ERROR: cannot identify Claude, Codex, or Antigravity host; set AGENT_VENDOR=claude|codex|antigravity" >&2
  exit 2
fi
PROJECT="${PROJECT:-${CLAUDE_PROJECT_DIR:-${CODEX_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}}}"

if [ "$AGENT_VENDOR" = "codex" ]; then
  STATE_DIR="$PROJECT/.codex/state"; MARKER="$STATE_DIR/refinement-active"
  ATTEMPTS_DIR="$STATE_DIR/refinement/attempts"
else
  STATE_DIR="$PROJECT/.claude";      MARKER="$STATE_DIR/.refinement-active"
  ATTEMPTS_DIR="$STATE_DIR/agent-memory/refinement/attempts"
fi
OUTPUT="$STATE_DIR/.refine-output"; EVAL_JSON="$STATE_DIR/.refine-eval.json"
```

`CODEX_PROJECT_DIR` is optional in real Codex sessions, so `CODEX_CI` and
`CODEX_THREAD_ID` are also authoritative host signals. Antigravity's
`run_command` sets `ANTIGRAVITY_AGENT=1` and can also inherit a parent's Codex or
Claude variables, so that signal is checked first and the project is the git
root of the current directory. Antigravity shares the Claude state paths, which
its Stop hook reads through `.agents/hooks/claude-hooks-adapter.sh`. These paths
match the Claude and Codex `refinement-gate.sh` markers exactly.

## Step 0b: Fresh-role isolation

- **Claude**: use a fresh `Agent` subagent for Audit, Modify, and Evaluate.
- **Codex**: invoke `scripts/meta/run-isolated-role.sh` separately for `audit`,
  `modify`, and `evaluate`. The helper starts a new `codex exec --ephemeral`
  process every time with `--ignore-user-config --disable hooks`; authentication
  still comes from `CODEX_HOME`, while user config and project hooks cannot
  pollute or recursively trigger child automation. Evaluate starts outside the
  repository to prevent recursive AGENTS.md loading. Audit and Evaluate receive
  only read-only evidence; Modify receives the Gap Report and task scope. Never
  use the parent Codex context as its own evaluator. Codex subagents are
  supported, but the subprocess remains intentional here because the evaluator
  must receive an exact, minimal evidence channel in interactive and
  non-interactive runs.
- **Antigravity**: invoke a fresh built-in `self` subagent (`invoke_subagent`,
  `TypeName: self`) separately for Audit, Modify, and Evaluate. Role
  instructions: Audit and Modify use the Step 3 and Step 4 text of this skill
  verbatim; Evaluate uses the body of `.agents/skills/evaluator/SKILL.md`. Give
  each child only that instruction and the permitted evidence as its prompt, and
  never reuse an evaluator conversation. The child receives the repository rules
  and hooks unchanged, as a Claude subagent does.
- In a DevContainer where bubblewrap is unavailable, the child may use
  `--dangerously-bypass-approvals-and-sandbox`. This is a compatibility fallback,
  not a security boundary. What the helper does enforce, fail-closed: Evaluate
  requires an output file (in-repo outputs must be gitignored); the report must
  be valid JSON with `checks_total >= 1`; and the evaluator's final message must
  be valid JSON with a `score` in [0,1]. A missing, stale, or malformed report
  fails the run.

## Step 0c: Pre-flight

- **Git must be completely clean**, including untracked files, because DISCARD
  removes files created by the current iteration. `git status --porcelain` must
  be empty or the run aborts.
- **Stale marker** — if `$MARKER` exists, abort (previous crash); remove it
  manually after confirming no other refine session is running.

```bash
TASK_ID="refine-$(date +%Y%m%d-%H%M%S)"
THRESHOLD="${THRESHOLD:-0.85}"; MAX_ITER="${MAX_ITER:-10}"
ATTEMPTS="$ATTEMPTS_DIR/$TASK_ID.jsonl"; mkdir -p "$ATTEMPTS_DIR"
```

## Step 1: Discover → Verification Contract

Rediscover ground truth every run (no cached config). Read the project
(Glob/Read/Grep), then build a Contract:

| Mode | When | Evaluator | Scoring |
|---|---|---|---|
| `objective` | tests/build/lint exist | none | verify_cmd → parse → number |
| `tool-augmented` | checks definable / no infra | evaluator subagent | checks[] + diff explore |
| `calibrated` | no objective metric (last resort) | evaluator subagent | `rubrics/default.yml` anchors |

Prefer `objective`. If a project-local `.refine/score.sh` exists (JSON out:
`{"score":0-1,"feedback":"...","metrics":{"<id>":"pass|fail"}}`) it is
authoritative. Validate that the Contract covers the requested outcome before
freezing it. If the baseline already satisfies the required checks and acceptance
threshold, accept it without modification. Do not add checks or raise the threshold
merely to force an improvement. Freeze the Contract into `$MARKER` (immutable thereafter):

```bash
cat > "$MARKER" <<EOF
{"task_id":"$TASK_ID","threshold":$THRESHOLD,"max_iterations":$MAX_ITER,"contract":{ <mode,verify_cmd,parse,metric,direction,checks,discovery_log> }}
EOF
```

## Step 2: Baseline

Run the baseline once. For tool-augmented/calibrated contracts with no
`verify_cmd`, use a fresh evaluator run of `Contract.checks[]` against HEAD
(no diff). Failed execution or invalid output is an error, not a passing
baseline. For objective mode:

```bash
bash -c "<Contract.verify_cmd>" > "$OUTPUT" 2>&1
SCORE=<parse .score>; GAPS=<failing check IDs, or []>
echo "{\"score\":$SCORE,\"gaps\":$GAPS,\"result\":\"Baseline\",\"feedback\":\"initial\"}" >> "$ATTEMPTS"
```

If the baseline `SCORE >= THRESHOLD`, report **ACCEPT** with zero modification
iterations, remove `$MARKER`, and exit without Audit, Modify, staging, or a
commit. Otherwise continue to Step 3.

## Step 3: Audit (fresh Explore subagent — read-only)

Spawn a fresh Explore role (Claude Agent or Codex ephemeral subprocess) given:
project, task context including the user's verbatim request or its existing source path,
`$OUTPUT`, `$ATTEMPTS`, current GAPS. It must: identify which checks fail; read
`$ATTEMPTS` to skip already-resolved gaps; gather evidence across code/config/
infra to find the TRUE root cause (code adapting to an infra limit is a
workaround, not a fix); flag any **regression** (a previously-passing check now
failing) as highest priority; select ONE gap cluster (1–3 related gaps). Returns
a Gap Report: `PRIORITY_GAP / EVIDENCE / ROOT_CAUSE / REGRESSION / REMAINING`.

## Step 4: Modify (fresh subagent — focused)

Spawn a fresh Modify role with: task including the user's verbatim request or its existing source path, Contract summary, the
Gap Report, `$ATTEMPTS` path. Rules: address **only** the `PRIORITY_GAP`; use the
evidence (don't assume); `git add` changed files; return ONE line (what changed +
which gap). The main agent never edits directly.

## Step 5: Evaluate

**objective mode** (no evaluator):
```bash
timeout 300 bash -c "<Contract.verify_cmd>" > "$OUTPUT" 2>&1
SCORE=<parse .score>; GAPS=<failing IDs>; SUGGESTION=<parse .feedback>
# parse failure or timeout → SCORE=0
```

**tool-augmented / calibrated** — spawn a fresh evaluator role with **ONLY**:
Contract JSON, `git diff --cached`, calibration anchors from
`rubrics/default.yml` (calibrated only), frozen at Contract time, and
`previous scores: $(jq -s '[.[].score]' "$ATTEMPTS")`, plus the `$EVAL_JSON`
output path. It
writes its full report to `$EVAL_JSON` and returns ONLY
`{"score":N,"suggestion":"one line"}`. The helper reserves `$EVAL_JSON` for that
authored report, captures Codex's final message in a separate temporary file,
prints the final message to stdout, and fails if the report is absent or empty.

**Context isolation (load-bearing):** the evaluator MUST NOT receive the task
description, the modifier's reasoning, or *why* changes were made. On Codex,
  run the evaluator with
  `bash "$PROJECT/scripts/meta/run-isolated-role.sh" evaluate "$PROJECT" <prompt-file> "$EVAL_JSON"`
  — never in-session. On Antigravity, invoke the evaluator as specified in Step 0b.

## Step 6: Keep or Discard

```bash
PREV_BEST=$(jq -s 'sort_by(.score)|last|.score//0' "$ATTEMPTS" 2>/dev/null || echo 0)
```
- `SCORE > PREV_BEST` → **KEEP**: `git commit -m "refine: $TASK_ID iter $N — score $SCORE"`
- else → **DISCARD**: list iteration-created untracked paths with
  `git clean -nd`, restore tracked files with
  `git restore --staged --worktree -- .`, then remove only those listed paths
  with path-scoped `git clean -fd -- <paths>`.
- `SCORE >= THRESHOLD` → also **ACCEPT** (exit after recording).

## Step 7: Record + Terminate

```bash
echo "{\"score\":$SCORE,\"gaps\":$GAPS,\"result\":\"<KEEP|DISCARD>: $SUMMARY\",\"feedback\":\"$SUGGESTION\"}" >> "$ATTEMPTS"
ITERATION=$(wc -l < "$ATTEMPTS")
```
- `SCORE >= THRESHOLD` → **ACCEPT** · `ITERATION > MAX_ITER` → **STOP** · else → return to **Step 3**.
- **Always `rm -f "$MARKER"` on every exit path** (ACCEPT / STOP / error), then
  report best: `jq -s 'sort_by(.score)|last' "$ATTEMPTS"`. Do not ask permission
  between iterations.

## Design principles

1. **Explore within scope** — discover the next gap in the requested outcome and frozen Contract.
2. **Thin orchestrator** — heavy work in fresh subagents; keep returned summaries concise.
3. **Audit→Modify separation** — evidence-before-modification is structurally enforced.
4. **Context reset per iteration**; output to file, not context.
5. **Generator ≠ Evaluator** — context-isolated scoring (Anthropic GAN principle); the agent that modified code never writes or modifies its own scorer in the same iteration.
6. **Metric over judgment** — objective if available; calibrated is last resort.
7. **A qualifying baseline is a successful no-op** — change the Contract only to correct an actual mismatch with the requested outcome before it is frozen.
8. **Scorer notes** — prefer graduated checks over binary pass/fail (binary → wasteful 0→1.0 jumps); evolve `score.sh` only *between* runs.
9. **No dead data** — store only `{score, gaps, result, feedback}` per attempt.
