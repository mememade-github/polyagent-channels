---
name: verify
description: Run the checks relevant to a change and report what ran, failed, or was skipped
argument-hint: "[product-name|all]"
user-invocable: true
allowed-tools: Bash, Read
---

Run the checks relevant to the request and the change for `$ARGUMENTS` (default: `all`).

For `all`, run the fast checker:

```bash
bash "${CLAUDE_PROJECT_DIR:-${CODEX_PROJECT_DIR:-$(git rev-parse --show-toplevel)}}/scripts/meta/completion-checker.sh"
```

It checks shell syntax, the hook and rule JSON files, the Codex TOML, the tracked-content leak scan, and mirror drift, and names the checks that passed, failed, or were not checked. The TOML parse is not checked when python3 has no tomllib, as with this template image's system python3 3.10; a pass establishes only the checks it names. Run the on-demand template checks, not per commit; they list the tools on the runner, run the fast checker once, and feed synthetic input to the hook scripts, and they start no container:

```bash
bash "${CLAUDE_PROJECT_DIR:-${CODEX_PROJECT_DIR:-$(git rev-parse --show-toplevel)}}/.devcontainer/verify-template.sh"
```

For a specific product, read its governance and `REFERENCE.md` verification section, then run repository-defined checks. Report which commands ran and passed, which failed, and which checks were skipped.
