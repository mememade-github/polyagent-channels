# DevContainer base maintenance

[Polyagent DevContainer](https://github.com/mememade-github/polyagent-devcontainer) supplies the base. [devcontainer-base.lock.json](devcontainer-base.lock.json) records the pinned commit and the Git blob of every base-owned file. Update those files from upstream as a set. Keep local adaptations in the product files below. The offline test compares blobs after Git's end-of-line normalization.
With the [clone-local Codex filter](REFERENCE.md#what-installation-adds), a
channel installation leaves `.codex/config.toml` unchanged for Git and the
lock test. The importer applies the clone's checkout filters to the new
base files, preserving the registration when the new configuration accepts it.

## Shared paths

<!-- generated: shared paths -->
| Path | What to carry forward |
| --- | --- |
| `.devcontainer/devcontainer.json` | Review upstream settings against [DevContainer settings](#devcontainer-settings). |
| `.devcontainer/.env.example` | Keep the product override form. Review upstream variables against [Base environment inputs](#base-environment-inputs) and derive effective defaults in the reference from product Compose. |
| `.gitignore` | Replace its upstream prefix with the new upstream blob, then retain the product block. Use the base rules for Codex runtime markers. |
| `README.md` | Keep product prerequisites, discovery and attribution. Review changed upstream prerequisites and notices. |
| `LICENSE` | Keep the product license at the root. Review upstream license changes and retain applicable notices under the locations in [Attribution](README.md#license--attribution). |
| `PROJECT.md` | Keep product context and the pointer to the product rule for clients that load project context. Review any changed base session-start requirements. |
| `REFERENCE.md` | Keep product commands and a link to the upstream lifecycle section at the new pin. Edit the [rendering template](scripts/REFERENCE.md.in) and regenerate the reference. |
| `UPGRADING.md` | Keep product release-specific steps. Add actions operators need when an upstream update affects them. |
<!-- end generated: shared paths -->

## DevContainer settings

[Channels' DevContainer JSON](.devcontainer/devcontainer.json) owns the editor entry path.

| Upstream key | Channels disposition and reason |
| --- | --- |
| `name` | Shows Polyagent Channels in the editor. |
| `dockerComposeFile` | Selects `compose.channels.yml` so both entry paths use the product stack. |
| `service` | Selects `polyagent-channels`, which runs Uvicorn and depends on healthy Redis. |
| `workspaceFolder`, `remoteUser` | Retain `/workspaces` and `vscode` to match the Compose bind and service user. |
| `shutdownAction` | Retains `stopCompose` for editor shutdown. |
| `containerEnv` | Omitted. The base Dockerfile supplies `DEVCONTAINER=true`, `LANG=en_US.UTF-8` and `LC_ALL=en_US.UTF-8` to both entry paths, and startup setup exports the locale for its own commands. Channels runs Python and leaves Node tools on their normal heap defaults, so the upstream `NODE_OPTIONS` override is omitted. |
| `remoteEnv` | Omitted. Claude follows its own environment and settings without the upstream IDE-only `CLAUDE_CODE_DISABLE_AUTO_MEMORY=0` override. |
| `features` | Omitted because the upstream object is empty and tooling comes from the images. |
| `customizations` | Omitted. Format-on-save, formatter, indentation, encoding, line endings, whitespace and final newline settings, the Bash profile, watcher exclusions, and the ESLint, Prettier, GitLens and Docker extensions are editor choices independent of the service. |
| `postCreateCommand`, `postStartCommand`, `waitFor` | Omitted. On every start the inherited entrypoint calls `setup-env.sh` before executing the image command. Setup applies `core.filemode=false` to discovered workspace Git repositories. The entrypoint orders setup and Compose tracks HTTP health, so the post-create wait is unnecessary. |
| `updateRemoteUserUID` | Omitted as an explicit setting. Automatic UID adjustment follows the DevContainer client's defaults. Plain Compose uses the image's `vscode` identity. |
| `init` | Declared as `true` in the product Compose service so both entry paths get init. |
| `mounts` | Omitted so the editor path does not expose host SSH keys. Compose supplies the workspace, host Docker socket and persistent agent and history volumes to both entry paths. |

The product adds `initializeCommand` to build the local base image before
Create/Reopen or `devcontainer up` starts the service. `overrideCommand: false`
preserves the image's command and its startup path.

## Compose layering

The product [Compose file](.devcontainer/compose.channels.yml) extends the base service. It resets the base DNS, interactive terminal and stdin settings and replaces the environment, healthcheck and mounts. This preserves the Channels runtime configuration, including the long-form workspace bind that fails on a missing host path and the Channels healthcheck timing. Base changes to mounts, environment or healthcheck do not reach the product and must be ported. Review any new inherited settings to keep the product runtime stable.

The DNS reset keeps Docker's default resolver. The base's fixed public
resolvers cannot answer on a closed network.

Channels keeps the base entrypoint and supplies the image and build settings,
user, working directory and init in Compose. The Python daemon needs neither
the base's interactive stdin nor its TTY. The environment override carries the
Channels and Redis connection settings, timezone and updater skips. The HTTP
healthcheck replaces the base's CLI and workspace check, including its start
period, so health reflects whether the service can answer requests. The mount
override keeps the host socket and the five project-scoped volumes for agent
state and shell history. Channels owns port publication, the healthy Redis
dependency and Redis persistence, logging and restart policy.

The build-only service uses the upstream Dockerfile. Its local image is tagged `<IMAGE_NAME>-base/<COMPOSE_PROJECT_NAME>:<IMAGE_TAG>`. Appending `-base/<COMPOSE_PROJECT_NAME>` gives the base a different repository name from the product image and scopes it to the Compose project. Keep BASE_IMAGE set to the tagged local base image so the DevContainer CLI can inspect it. Define both the repository-only key `<IMAGE_NAME>-base/<COMPOSE_PROJECT_NAME>` and the tagged key `<IMAGE_NAME>-base/<COMPOSE_PROJECT_NAME>:<IMAGE_TAG>` as additional contexts pointing to `service:devcontainer-base`. BuildKit looks up the repository-only key for `:latest` and the tagged key for other tags. [Dockerfile.channels](.devcontainer/Dockerfile.channels) replaces the base's bundled Compose with 2.33.0 to support the service build context. Drop that override once the base bundles a compatible Compose and builder.

## Base environment inputs

See the [configuration table](REFERENCE.md#configuration-devcontainerenv) for
effective defaults and overrides.

| Base input | Channels disposition and reason |
| --- | --- |
| `COMPOSE_PROJECT_NAME`, `CONTAINER_NAME`, `IMAGE_NAME`, `IMAGE_TAG` | Retained with product-specific project, container and image names. These settings identify the instance and its images, and the project name scopes the volumes and the local base image. |
| `HOST_WORKSPACE_PATH` | Retained to resolve the workspace bind on the Docker daemon host. Use the relative default when the daemon can see the resolved path and a daemon-visible path otherwise. |
| `TZ` | Carried into the service environment to select the process timezone. The image's system timezone files remain UTC. |
| `PROJECT_NODE_VERSION` | Passed only to the build-only `devcontainer-base` service, whose Dockerfile uses a nonempty value to install project Node with nvm. Interactive Bash tries `.nvmrc` when present and otherwise selects the project alias. An empty value leaves the base's system Node in place. Channels overrides its own build arguments with `BASE_IMAGE`, the sole argument its Dockerfile reads. |
| `DNS_PRIMARY` | Omitted with the base DNS list so the service uses [Docker's resolver](#compose-layering). |
| `TYPESAFE_API_KEY` | An optional TypeSafe skill input listed only in the upstream example. The base Compose service does not inject it, and Channels omits it from its override form and service environment. |
| `SKIP_CLAUDE_UPDATE`, `SKIP_CODEX_UPDATE`, `SKIP_AGY_UPDATE` | Forwarded to the inherited startup setup so operators can opt out of update checks, including for [closed-network operation](REFERENCE.md#operating-without-internet-access). |

The [project rule](.claude/rules/project/polyagent-channels.md) belongs to Channels. Regenerate its mirror using the upstream sync script.

## Update the pin

Updating the pin requires access to the upstream base repository. Start from a clean product branch at the repository root. Set `upstream` to a separate clone of the base repository and `revision` to the desired tag or commit. Fetch the target and the commit recorded in the product lock in that clone if necessary. Run the review and import blocks in the same Bash shell with Python 3.12 and Git available.

```bash
# Set upstream and revision before running the block.
pin=$(git -C "${upstream:?set upstream to the base clone}" rev-parse --verify "${revision:?set revision to the new base pin or tag}^{commit}") &&
  python scripts/update_devcontainer_base.py --review "$upstream" "$pin"
```

After review succeeds, inspect the patch for the shared paths and the full
base Compose file. Use the [Shared paths](#shared-paths) guidance, including
the environment, healthcheck and mounts the product replaces. Then import the
reviewed pin.

```bash
(
  set -e
  python scripts/update_devcontainer_base.py "$upstream" "$pin"

  ignore=$(mktemp)
  trap 'rm -f "$ignore"' EXIT
  git -C "$upstream" show "$pin:.gitignore" > "$ignore"
  printf '\n' >> "$ignore"
  sed -n '/^# Polyagent Channels$/,$p' .gitignore >> "$ignore"
  cat "$ignore" > .gitignore
)
```

The importer discovers files in the new Git tree, skips the shared paths in the
table, and removes files listed in the old lock that are absent at the new pin.
It updates individual files so product additions in the same directories
survive. If a new base path overlaps a product file or directory, resolve its
ownership before retrying the import. Port any changes identified in review,
update the lifecycle link in the reference template to `$pin`, and run the
mirror script in the product checkout.

```bash
bash scripts/sync-agents-mirror.sh
```

From the product checkout in the Python 3.12 test environment provisioned in
[Verification](REFERENCE.md#verification), regenerate the documents and run
the lock and document tests.

```bash
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
  /tmp/channels-test-venv/bin/python scripts/render_docs.py
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
  /tmp/channels-test-venv/bin/python -m pytest -p no:cacheprovider tests/test_devcontainer_base.py tests/test_documents.py
```

Run the upstream `.devcontainer/verify-template.sh` with Docker unavailable and
follow the [product verification procedure](REFERENCE.md#verification). Check
a cold product Compose build and a cold DevContainer Create/Reopen (or `devcontainer up`) in an isolated
checkout and confirm the product's effective runtime settings before starting
an isolated stack.

Before committing, run `bash scripts/meta/completion-checker.sh` in the product
checkout. On success it writes ignored per-branch completion markers.
