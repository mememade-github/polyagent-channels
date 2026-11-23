# DevContainer base maintenance

[Polyagent DevContainer](https://github.com/mememade-github/polyagent-devcontainer) supplies the base. [devcontainer-base.lock.json](devcontainer-base.lock.json) records the pinned commit and the Git blob of every base-owned file. Update those files from upstream as a set. Keep local adaptations in the product files below. The offline test compares blobs after Git's end-of-line normalization.
With the [clone-local Codex filter](REFERENCE.md#what-installation-adds), a
channel installation leaves `.codex/config.toml` unchanged for Git and the
lock test. The importer applies the clone's checkout filters to the new
base files, preserving the registration when the new configuration accepts it.

## Shared paths

| Path | What to carry forward |
| --- | --- |
| `.devcontainer/devcontainer.json` | Keep the product workspace and service settings and select `compose.channels.yml`. Keep `initializeCommand` so Create/Reopen builds the base before the service. Review upstream lifecycle changes for any setup the product needs. |
| `.devcontainer/.env.example` | Keep the product override form. Review any upstream variables the product needs and document their effective defaults in the product Compose file and reference. |
| `.gitignore` | Replace its upstream prefix with the new upstream blob, then retain the product block. Use the base rules for Codex runtime markers. |
| `README.md` | Keep product prerequisites, discovery and attribution. Review changed upstream prerequisites and notices. |
| `LICENSE` | Keep the product license at the root. Review upstream license changes and retain applicable notices under the locations in [Attribution](README.md#license--attribution). |
| `PROJECT.md` | Keep product context and the pointer to the product rule for clients that load project context. Review any changed base session-start requirements. |
| `REFERENCE.md` | Keep product commands and a link to the upstream lifecycle section at the new pin. Edit the [rendering template](scripts/REFERENCE.md.in) and regenerate the reference. |
| `UPGRADING.md` | Keep product release-specific steps. Add actions operators need when an upstream update affects them. |

The product [Compose file](.devcontainer/compose.channels.yml) extends the base service. It resets the base DNS, interactive terminal and stdin settings and replaces the environment, healthcheck and mounts. This preserves the Channels runtime configuration, including the long-form workspace bind that fails on a missing host path and the Channels healthcheck timing. Base changes to mounts, environment or healthcheck do not reach the product and must be ported. Review any new inherited settings to keep the product runtime stable.

The DNS reset keeps Docker's default resolver. The base's fixed public
resolvers cannot answer on a closed network.

The build-only service uses the upstream Dockerfile. Its local image is tagged `<IMAGE_NAME>-base/<COMPOSE_PROJECT_NAME>:<IMAGE_TAG>`. Appending `-base/<COMPOSE_PROJECT_NAME>` gives the base a different repository name from the product image and scopes it to the Compose project. Keep the build argument used by `FROM` and the corresponding additional context on the same image reference. [Dockerfile.channels](.devcontainer/Dockerfile.channels) replaces the base's bundled Compose with 2.33.0 to support the service build context. Drop that override once the base bundles a compatible Compose and builder.

The [project rule](.claude/rules/project/polyagent-channels.md) belongs to Channels. Regenerate its mirror using the upstream sync script.

## Update the pin

Updating the pin requires access to the upstream base repository. Start from a clean product branch at the repository root. Set `upstream` to a separate clone of the base repository and `revision` to the desired tag or commit. Fetch the target in that clone first if necessary. Run this block in Bash with Python 3.12 and Git available.

```bash
set -e
# Set upstream and revision before running the block.
: "${upstream:?set upstream to the base clone}"
: "${revision:?set revision to the new base pin or tag}"
pin=$(git -C "$upstream" rev-parse --verify "${revision}^{commit}")

python scripts/update_devcontainer_base.py "$upstream" "$pin"

ignore=$(mktemp)
git -C "$upstream" show "$pin:.gitignore" > "$ignore"
printf '\n' >> "$ignore"
sed -n '/^# Polyagent Channels$/,$p' .gitignore >> "$ignore"
cat "$ignore" > .gitignore
rm "$ignore"
```

The importer discovers files in the new Git tree, skips the shared paths in the
table, and removes files listed in the old lock that are absent at the new pin.
It updates individual files so product additions in the same directories
survive. If a new base path overlaps a product file or directory, resolve its
ownership before retrying the import. Review the shared paths and product-layer
settings above, update the lifecycle link in the reference template to `$pin`,
and run the mirror script.

```bash
bash scripts/sync-agents-mirror.sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python scripts/render_docs.py
PYTHONPATH=src python -m pytest tests/test_devcontainer_base.py tests/test_documents.py
bash scripts/meta/completion-checker.sh
```

Run the upstream `.devcontainer/verify-template.sh` with Docker unavailable and
follow the [product verification procedure](REFERENCE.md#verification). Check
a cold product Compose build and a cold DevContainer Create/Reopen (or `devcontainer up`) in an isolated
checkout and confirm the product's effective runtime settings before starting
an isolated stack.
