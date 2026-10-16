# STATUS

**Updated:** 2026-10-17  
**Horizon:** PS/ai2 stable (short) → linux-first / `contour` (kit Tasks 1–11)  
**Repo:** https://github.com/SergeyGlazunov/ai-bella  
**Priority:** Task 12 catch-up + cutover close-out today

## Active runtime (stand ADM-SAPHX-TECH)

| Role | Fact |
|------|------|
| Canon data | `/srv/ai2/{ubd,kb,klon_opl}/src` (contour sources bind: `/srv/ai-contour/sources/ubd/src`) |
| Chat | Open WebUI `:3000` (healthy); OmniRoute `:20128` |
| MDD MCP | `:8080` (do not confuse with WebUI) |
| УБД graph MCP | `:8001` (`ai2-mcp-metadata-graph`) |
| УБД code MCP | `:8011` (`ai2-bsl-indexer`) |
| КБ graph / code | `:8201` / `:8211` |
| OPL code | `:8311` (`ai2-bsl-klon-opl`) |
| Orchestrator | **cutover in progress** — `ai-sync.timer` still disabled until UBD publish PASS |

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
| WSL bootstrap dry-run on host | **PASS** 2026-10-16 — `/opt/ai-contour` + validate; `ai-sync.timer` disabled; `/srv/ai2` untouched |
| Stand cutover | **in progress** — clean graph FULL + post-FULL finish watcher |
| Chat backup drill | **PASS** 2026-10-16 — throwaway `:3100` login as admin (Сергей Глазунов); chats/models restored; live `:3000` healthy; evidence `/srv/ai-contour/backups/chat-drill-20261016T120306Z` |

Integration acceptance covers: full publish cycle ×3 profiles; package intake; corrupted/incomplete package refusal; crash after activation (reads stay closed); reaped completed job; unknown writer refusal; Graph exit 3→FULL; failed mandatory BSL; probes before reopen.

## Blockers / open

1. Vendor provenance for `compose/mcp_for_1c` incomplete — see `VENDOR.md`.
2. Live secrets stay on disk (gitignored); not in Git.
3. First green night after `ai-sync.timer` enable still pending (verify after cutover).

## Next action

1. Task 12 catch-up **running** (UBD `973685e7…`): clean FULL `contour-graph-full-41d027d4e2f2` (Day-2 resolve; prior successful FULL ~4.0 h). **Do not kill.**
2. After FULL PASS, auto-finish (`/tmp/after-full-t12.log`): seed partial → publish UBD → intake/update KB+klon → restore window 01:30–06:00 → open MCP → enable `ai-sync.timer` → tear down drill `:3100`/`:20130`.
3. Gate: timer enables only if UBD publish exit 0 and `published=<sha>`.
4. KB/klon packages ready: `inbox/kb/ready-catchup-…`, `inbox/klon_opl/ready-catchup-…`.
