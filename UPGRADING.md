# Upgrading an installation

The procedure, backup and rollback are in
[REFERENCE.md § Upgrade and rollback](REFERENCE.md#upgrade-and-rollback). This
file lists only what a release needs beyond that procedure.

## 0.1.2

### Upgrading from 0.1.1 or earlier — container rebuild required

Compose now runs in the upstream Polyagent DevContainer form,
`docker compose --env-file .devcontainer/.env -f .devcontainer/docker-compose.yml`,
from the repository root. The root `compose.yaml` is gone, so plain
`docker compose` there finds no Compose file.

Before the upgrade procedure:

1. Find the project name the running service uses: `docker compose ls -a`
   prints it in the NAME column of the row whose CONFIG FILES lie in this
   repository. Earlier releases took it from the repository folder name.
2. In `.devcontainer/.env`, created from `.env.example` if it is missing, set
   `COMPOSE_PROJECT_NAME` to that name. Another name starts with an empty
   volume, and the previous messages stay in the old one.
3. If `.devcontainer/.env` sets `INSTANCE_NAME`, rename it to `CONTAINER_NAME`.
4. If another instance runs on the same Docker host, give each one its own
   `IMAGE_NAME`. The service image is now named by `IMAGE_NAME` rather than by
   the project, so instances that keep the default share one image name.

The VS Code DevContainer now names `.devcontainer/docker-compose.yml` directly
and reads the same `.devcontainer/.env`.

The service container now mounts named volumes over the agent CLIs' folders
(`~/.claude`, `~/.codex`, `~/.gemini`, `~/.config/antigravity`) and
`/commandhistory`. They start empty, so this upgrade still discards the logins
and sessions of the old container, as every recreation did before. To keep
them, close the agent sessions, archive those folders and `~/.claude.json`
into the ignored `.env/` folder before the upgrade and unpack the archive in
the new container. From this release on the five volumes keep those folders
across a recreated container; `~/.claude.json` stays outside them, as
[REFERENCE.md § Upgrade and rollback](REFERENCE.md#upgrade-and-rollback) says.

The service container now mounts the host's Docker socket at
`/var/run/docker.sock`, as the upstream DevContainer does. Every process in
the container then controls the host's Docker daemon. Read
[REFERENCE.md § Privilege boundary](REFERENCE.md#privilege-boundary) before
you upgrade a service that other computers reach.
