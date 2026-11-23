# Upgrading an installation

The procedure, backup and rollback are in
[REFERENCE.md § Upgrade and rollback](REFERENCE.md#upgrade-and-rollback). This
file lists only what a release needs beyond that procedure.

## 0.2.1

### Upgrading from 0.2.0

[Update each Codex client](REFERENCE.md#updating-a-client) to forward
`CHANNEL_KEY_FILE` and `CHANNEL_STATE_DIR` to its tools and receiver.
Upgrading the service alone leaves the registration's old variable list in place.

Before downgrading a Codex client to 0.2.0,
[uninstall it with its installed harness](REFERENCE.md#uninstall), then
[install the 0.2.0 client](REFERENCE.md#client-installation). The 0.2.0 installer
refuses this release's registration, and its uninstaller leaves it behind.

## 0.2.0

### Upgrading from 0.1.x

The product Compose file moves to `.devcontainer/compose.channels.yml`.
In this release, `.devcontainer/docker-compose.yml` defines the generic
DevContainer service. Select the product file for each tree using the
[upgrade and rollback procedure](REFERENCE.md#upgrade-and-rollback). Check the
[Compose and Buildx requirements](README.md#requirements) before rebuilding.

For a closed-network upgrade, bring the two images and matching release tree
described in [Operating without internet access](REFERENCE.md#operating-without-internet-access).
Keep the existing instance settings and set the update-check overrides before
recreating the container if its network cannot reach the update servers.

[Update the client](REFERENCE.md#updating-a-client) to refresh an existing
Codex registration. The new registration passes the container's
`SKIP_CODEX_UPDATE` setting through to Codex session wakes. The update also
sets up the clone-local Git filter for a tracked configuration.
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
