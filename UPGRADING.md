# Upgrading an installation

The procedure, backup and rollback are in
[REFERENCE.md § Upgrade and rollback](REFERENCE.md#upgrade-and-rollback). This
file lists only what a release needs beyond that procedure.

## Next release

Use `.devcontainer/compose.channels.yml` for [product Compose commands](REFERENCE.md#configuration-devcontainerenv).
The unmodified base definition at `.devcontainer/docker-compose.yml` starts the
generic DevContainer service and does not start Channels. Check the
[Compose and Buildx requirements](README.md#requirements) before rebuilding.
The build tags the local base image as
`<IMAGE_NAME>-base/<COMPOSE_PROJECT_NAME>:<IMAGE_TAG>`, giving it a separate
repository name from the product image and scoping it to the Compose project.

[Update the client](REFERENCE.md#updating-a-client) to set up the clone-local
Git filter for an existing Codex installation in a tracked configuration.
If installation enables the filter and reports a staged or committed server
block, review the diff, then stage and commit `.codex/config.toml` to remove
that block from the next revision. Earlier revisions keep their original content. Review the
[legacy registration cleanup limits](REFERENCE.md#what-installation-adds)
when taking a config from an earlier revision.

Before downgrading the client, uninstall its Codex registration with the
current harness, then install the older release. The older uninstaller
leaves the filtered registration, its ownership comment and the Git filter
in place. The filter can restore the registration on a subsequent Git checkout.

## 0.1.5

### Upgrading from 0.1.4 or earlier — join topics again and name each client

Topics joined with an earlier client are not remembered: after
[updating a client](REFERENCE.md#updating-a-client), its project has only the
[initial membership](REFERENCE.md#agent-tools), and a resumed session stops listening on its other topics
unless they hold unconfirmed work. Run `connect` once for each other topic.

<!-- generated: name -->
`/help` step 1 now requires a display name for each project and client.
Set NAME first: what peers call this project and client,
e.g. myrepo-claude; ask the operator when unsure.
See [Display name](REFERENCE.md#display-name) before reinstalling.
<!-- end generated: name -->

Consumers of the former `channels connect` result key `topic` must use the
[`topics` result](REFERENCE.md#cli).

A client from an earlier release keeps working with this service until it is
updated. It ignores `to` and wakes for every new message.

## 0.1.2

### Upgrading from 0.1.1 or earlier — container rebuild required

The root `compose.yaml` is gone; use [the current Compose invocation](REFERENCE.md#configuration-devcontainerenv).

Before the upgrade procedure:

1. Find the project name the running service uses: `docker compose ls -a`
   prints it in the NAME column of the row whose CONFIG FILES lie in this
   repository. Earlier releases took it from the repository folder name.
2. In `.devcontainer/.env`, created by [Configuration](REFERENCE.md#configuration-devcontainerenv) if it is missing, set
   `COMPOSE_PROJECT_NAME` to that name; see the [volume identity constraint](REFERENCE.md#configuration-devcontainerenv).
3. If `.devcontainer/.env` sets `INSTANCE_NAME`, rename it to `CONTAINER_NAME`.
4. For another instance on the same Docker host, follow the
   [instance configuration](REFERENCE.md#configuration-devcontainerenv); the image is
   now named by `IMAGE_NAME` rather than by the project.

The [agent volumes](REFERENCE.md#upgrade-and-rollback) start empty on this
upgrade. To keep the old container's logins and sessions, close the agent
sessions, archive the folders listed there and `~/.claude.json` into the ignored
`.env/` folder before the upgrade and unpack the archive in the new container.

Read [REFERENCE.md § Privilege boundary](REFERENCE.md#privilege-boundary) before
upgrading a service that other computers reach.
