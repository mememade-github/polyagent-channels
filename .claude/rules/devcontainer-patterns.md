---
trigger: always_on
---
# DevContainer Development Patterns

> Prevents Docker-in-Docker (DinD) anti-pattern and defines container validation.

## Core Principle

DevContainers run on the **HOST** Docker daemon. From inside a container, the host daemon is
accessible via mounted `docker.sock` -- this is NOT DinD.

**Prerequisites**: `docker.sock` mounted + user in `docker` group + `devcontainer` CLI installed

| Allowed (via docker.sock) | Not Possible |
|--------------------------|-------------|
| `docker compose build` | VS Code extension testing |
| `devcontainer up/exec` | GUI-dependent features |
| `docker images`, `docker inspect` | -- |
| Volume mounts via `--project-directory` | -- |

## Volume Mount Path Translation

When running `docker compose up` inside a DevContainer, bind mount paths are resolved by the
**Docker daemon**, not the container. Path translation is required.

```
DevContainer (9p mount):   /workspaces/<project>/...
Docker daemon (WSL2 host): /run/desktop/mnt/host/c/.../<project>/...
Cross-access impossible -- different filesystem namespaces.
```

**Resolution**: Set `HOST_WORKSPACE_PATH` to the HOST filesystem path, then use
`docker compose --project-directory <HOST_PATH>`.

Verify:
```bash
docker inspect <container_name> \
  --format '{{range .Mounts}}{{if eq .Destination "/workspaces"}}{{.Source}}{{end}}{{end}}'
```

### build vs volume mount Behavior

| Command | File Access Method | DevContainer Path Behavior |
|---------|-------------------|---------------------------|
| `docker compose build` | Docker CLI reads files and sends to daemon | DevContainer path OK |
| `docker compose up` (bind mount) | Docker daemon mounts directly from HOST | **HOST path required** |
