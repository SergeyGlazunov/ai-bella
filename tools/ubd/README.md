# tools/ubd

Nightly УБД path for ai2 (graph + code).

| File | Role |
|------|------|
| `Register-UbdGitSync.ps1` | Task Scheduler: **git-only** pull into `C:\MCP\ubd_config` (01:00; before contour) |
| `Register-UbdNightly.ps1` | Legacy full sync+index registration — keep **Disabled** after contour cutover |
| `scripts/sync-git-only.ps1` | GitLab → `C:\MCP\ubd_config` FF pull (no indexing) |
| `neo4j_timeout_bootstrap.py` | FULL indexer bootstrap: `NEO4J_TIMEOUT_SEC` + smaller delete batches |
| `scripts/` | Canonical copies of `C:\MCP\ubd_index\scripts\` patches (ai2 targets, copy to `/srv/ai2/ubd/src`, timeout wire-up) |

After linux-first cutover: contour (`ai-sync.timer`) owns УБД git fetch + index/publish. Live stand uses direct GitLab remote in `/etc/ai-contour/contour.yaml`; token lives in `/srv/ai-contour/secrets/git-credentials` (ai-contour credential helper), not in yaml. Leave **UBD Config Sync** and **UBD Config Git Sync** Disabled — Windows mirror is not on the runtime path.

`Register-UbdGitSync.ps1` / `sync-git-only.ps1` remain as fallback if someone reverts to a path remote.

Live stand scripts under `C:\MCP\ubd_index\scripts\` are legacy for full sync+index only.

Bootstrap must also exist on Linux at `/srv/ai2/tools/ubd/neo4j_timeout_bootstrap.py` (mounted by `New-UbdIndexerDockerRunArgs` in `scripts/lib.ps1`).
