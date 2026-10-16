# STATUS

**Updated:** 2026-10-16  
**Horizon:** PS/ai2 stable (short) → linux-first / `contour` (kit Tasks 1–11)  
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
| pytest | **289+ unit** + **13 integration** (Task 11 local proof, 2026-10-16) |

## Linux-first kit proof (local — not stand)

| Item | Result |
|------|--------|
| Tasks 1–10 | done in kit (`c19dedd` latest graph logs_path fix) |
| Task 11 integration | `controller/tests/integration/` — nightly cycle UBD/KB/klon_opl + recovery scenarios; fake Docker / temp SQLite |
| Runbooks | `checks/linux-first-{preflight,catchup,cutover}.md` — rollback A/B + wait-for-idle writers (catch-up/cutover) |
| WSL bootstrap dry-run on host | **pending gate** before catch-up (no `/opt` install, no timer yet); not claimed as proof |
| Stand cutover | **not started** |

Integration acceptance covers: full publish cycle ×3 profiles; package intake; corrupted/incomplete package refusal; crash after activation (reads stay closed); reaped completed job; unknown writer refusal; Graph exit 3→FULL; failed mandatory BSL; probes before reopen.

## Blockers / open

1. Need **1–2 green nights** on PS/ai2 before prod УБД teardown and contour cutover.
2. Vendor provenance for `compose/mcp_for_1c` incomplete — see `VENDOR.md`.
3. Live secrets stay on disk (gitignored); not in Git.
4. Live stand cutover still requires separate explicit permission (**Task 12**).

## Next action

1. Observe PS/ai2 nightly LastResult on the stand (operator).
2. Linux-first kit: Tasks 1–11 done. **No stand cutover**.
3. Before catch-up: **WSL bootstrap dry-run** on host (pending gate) — then Task 12 only with operator approval (preflight → catch-up → cutover).
4. Do not switch live Scheduler to package-only without inbox+contour intake and Task 12 fence.
