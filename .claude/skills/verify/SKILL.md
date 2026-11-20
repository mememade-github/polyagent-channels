---
name: verify
description: Run pre-commit verification checks on a product
argument-hint: "[product-name|all]"
user-invocable: true
allowed-tools: Bash, Read
---

Run verification for `$ARGUMENTS` (default: `all`).

For `all`, run the fast checker:

```bash
bash "${CLAUDE_PROJECT_DIR:-${CODEX_PROJECT_DIR:-$(git rev-parse --show-toplevel)}}/scripts/meta/completion-checker.sh"
```

A successful run writes the per-branch marker used by the pre-commit gate. Run the docker-backed acceptance suite on demand or in CI, not per commit:

```bash
bash "${CLAUDE_PROJECT_DIR:-${CODEX_PROJECT_DIR:-$(git rev-parse --show-toplevel)}}/.devcontainer/verify-template.sh"
```

For a specific product, read its governance and `REFERENCE.md` verification section, then run repository-defined checks. Report PASS or FAIL for every command actually run.
