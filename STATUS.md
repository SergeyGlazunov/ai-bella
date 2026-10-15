# STATUS

**Updated:** 2026-10-15  
**Horizon:** PS/ai2 stable (short) → linux-first / `contour` (next)  
**Repo:** https://github.com/SergeyGlazunov/ai-bella  
**Priority:** operational nights + recovery (`For Cursor/LINUX-FIRST-DESIGN.md` §1.1)

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
| UBD Config Sync | 02:00 | `C:\MCP\ubd_index/scripts/sync-and-index.ps1` (synced from `tools/ubd/scripts/`) |
| AI-contour klon_opl nightly export | 02:00 | `tools/klon_opl/Export-KlonOpl.ps1` |

UBD FULL graph uses bootstrap `/srv/ai2/tools/ubd/neo4j_timeout_bootstrap.py` (`NEO4J_TIMEOUT_SEC=1800`, `NEO4J_DELETE_BATCH=2000`). Proven 2026-10-13: FULL exit 0 (~5.3 h).

## Agent / toolchain (this machine)

| Tool | Version / note |
|------|----------------|
| Git | 2.56 (Windows); remote `origin` → ai-bella |
| Python | 3.12.10 (`controller` venv; needs `tzdata` on Windows for tests) |
| Docker Engine | via `DOCKER_HOST=tcp://127.0.0.1:2375` |
| WSL | Ubuntu Running (v2) |
| pytest | **273 passed, 6 skipped** + Task8 review 36 passed (2026-10-15) |

## Blockers / open

1. Need **1–2 green nights** on PS/ai2 before prod УБД teardown and contour cutover.
2. Vendor provenance for `compose/mcp_for_1c` incomplete — see `VENDOR.md`.
3. Live secrets stay on disk (gitignored); not in Git.
4. Live stand cutover still requires separate explicit permission.

## Next action

1. Observe PS/ai2 nightly LastResult on the stand (operator). Note: Task 9 kit exporters no longer index — until cutover, either keep legacy nightly paths or temporarily dual-run; **do not switch live Task Scheduler to package-only without inbox+contour intake**.
2. Linux-first kit: Tasks 2–9 done in kit code. Task 9: `tools/common/Export-Package.psm1` + KB/`klon_opl` publish `ready-*` packages (Pester 9 passed). Set real Neo4j container names per DB on the stand when wiring contour. **No stand cutover**.
3. Next kit work: **Task 10** (CLI / systemd / bootstrap for `/srv/ai-contour`) from `For Cursor/LINUX-FIRST-IMPLEMENTATION-PLAN.md`.
4. Before stand dry-run: Linux/WSL lock/path checks on a WSL host; Tasks 11–12 for dry-run/cutover.
