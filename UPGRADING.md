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
