# tools/ubd

Nightly УБД path for ai2 (graph + code).

| File | Role |
|------|------|
| `Register-UbdNightly.ps1` | Task Scheduler registration (needs elevation) |
| `neo4j_timeout_bootstrap.py` | FULL indexer bootstrap: `NEO4J_TIMEOUT_SEC` + smaller delete batches |
| `scripts/` | Canonical copies of `C:\MCP\ubd_index\scripts\` patches (ai2 targets, copy to `/srv/ai2/ubd/src`, timeout wire-up) |

Live stand currently executes from `C:\MCP\ubd_index\scripts\`. After pull, copy `scripts/*` there (or retarget the scheduled task to this tree).

Bootstrap must also exist on Linux at `/srv/ai2/tools/ubd/neo4j_timeout_bootstrap.py` (mounted by `New-UbdIndexerDockerRunArgs` in `scripts/lib.ps1`).
