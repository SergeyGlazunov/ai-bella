# STATUS

**Updated:** 2026-10-13  
**Horizon:** PS/ai2 stable (short) → linux-first / `contour` (next)  
**Repo:** https://github.com/SergeyGlazunov/ai-bella (`main` @ `47716fc` at doc write; branch may advance)

## Active runtime (stand ADM-SAPHX-TECH)

| Role | Fact |
|------|------|
| Canon data | `/srv/ai2/{ubd,kb,klon_opl}/src` |
| Chat | Open WebUI `:3000` (healthy); OmniRoute `:20128` |
| MDD MCP | `:8080` (do not confuse with WebUI) |
| УБД graph MCP | `:8001` (`ai2-mcp-metadata-graph`) |
| УБД code MCP | `:8011` (`ai2-bsl-indexer`) |
| КБ graph / code | `:8201` / `:8211` |
| OPL code | `:8311` (`ai2-bsl-klon-opl`) |
| Orchestrator | **Not cut over** — nightly = Task Scheduler + PowerShell |

## Nightly (current)

| Task | When | Entry |
|------|------|--------|
| AI-contour KB nightly export | 01:00 | `tools/kb/Update-And-Export-Kb.ps1` |
| UBD Config Sync | 02:00 | `C:\MCP\ubd_index\scripts\sync-and-index.ps1` (synced from `tools/ubd/scripts/`) |
| AI-contour klon_opl nightly export | 02:00 | `tools/klon_opl/Export-KlonOpl.ps1` |

UBD FULL graph uses bootstrap `/srv/ai2/tools/ubd/neo4j_timeout_bootstrap.py` (`NEO4J_TIMEOUT_SEC=1800`, `NEO4J_DELETE_BATCH=2000`). Proven 2026-10-13: FULL exit 0 (~5.3 h).

## Agent / toolchain (this machine)

| Tool | Version / note |
|------|----------------|
| Git | 2.56 (Windows); remote `origin` → ai-bella |
| Python | 3.12.10 (`controller` venv; needs `tzdata` on Windows for tests) |
| Docker Engine | 29.8.1 via `DOCKER_HOST=tcp://127.0.0.1:2375` |
| WSL | Ubuntu Running (v2) |
| pytest | **104 passed** (2026-10-13, `controller` + `tzdata` on Windows) |

## Blockers / open

1. Need **1–2 green nights** on PS/ai2 (especially KB after inspect EAP fix `47716fc`) before prod УБД teardown and contour cutover.
2. Vendor provenance for `compose/mcp_for_1c` incomplete — see `VENDOR.md`.
3. Live secrets stay on disk (gitignored); not in Git.

## Next action

1. Observe tonight’s KB/UBD/OPL LastResult.
2. Linux-first Tasks 2–4 done (config v2, SQLite StateStore, package validate/claim).
3. Next: Task 5+ from `For Cursor/LINUX-FIRST-IMPLEMENTATION-PLAN.md` — no Task Scheduler cutover until Task 11–12.
