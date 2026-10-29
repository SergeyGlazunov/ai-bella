# STATUS

> С 2026-10-29 действует [порядок работы со стендом и Git](docs/production-workflow.md). Сводка ниже датирована 2026-10-18 и требует повторной проверки. Изменение правил работы не обновляет факты о сервисах; текущие SHA, образы и результаты ночей здесь пока не подтверждены.

**Updated:** 2026-10-18  
**Horizon:** linux-first / `contour` on stand (Task 12 cutover)  
**Repo:** https://github.com/SergeyGlazunov/ai-bella  
**Priority:** first green night on `ai-sync.timer` (UBD + KB + klon)

## Active runtime (stand ADM-SAPHX-TECH)

| Role | Fact |
|------|------|
| Canon data | `/srv/ai2/{ubd,kb,klon_opl}/src` (contour sources: `/srv/ai-contour/sources/{ubd,kb,klon_opl}`) |
| Chat | Open WebUI `:3000` (healthy); OmniRoute `:20128` |
| MDD MCP | `:8080` (do not confuse with WebUI) |
| УБД graph MCP | `:8101` (`ai2-mcp-metadata-graph` on `ai2-stack`; WebUI bearer = shared MCP secret) |
| УБД code MCP | `:8011` (`ai2-bsl-indexer`) |
| КБ graph / code | `:8201` / `:8211` — **published** 2026-10-18 (graph FULL + open) |
| OPL code | `:8311` (`ai2-bsl-klon-opl`) |
| Orchestrator | **`ai-sync.timer` enabled** — next `01:30 Asia/Vladivostok` |

## Nightly (current)

| Task | When | Entry |
|------|------|--------|
| **contour** (`ai-sync.timer`) | 01:30 Asia/Vladivostok | `/opt/ai-contour/venv/bin/contour update` |
| AI-contour KB nightly export | 01:00 | `tools/kb/Update-And-Export-Kb.ps1` (export/package only) |
| AI-contour klon_opl nightly export | 02:00 | `tools/klon_opl/Export-KlonOpl.ps1` (export/package only) |

Legacy `UBD Config Sync` indexing must stay **Disabled**. Contour owns UBD index/publish.

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
| Stand cutover | **PASS (core)** 2026-10-18 — UBD+klon published; `ai-sync.timer` enabled |
| Chat backup drill | **PASS** 2026-10-16 — throwaway `:3100`; evidence `/srv/ai-contour/backups/chat-drill-20261016T120306Z` |

Integration acceptance covers: full publish cycle ×3 profiles; package intake; corrupted/incomplete package refusal; crash after activation (reads stay closed); reaped completed job; unknown writer refusal; Graph exit 3→FULL; failed mandatory BSL; probes before reopen.

## Blockers / open

1. Vendor provenance for `compose/mcp_for_1c` incomplete — see `VENDOR.md`.
2. Live secrets stay on disk (gitignored); not in Git.
3. First green night — verify after `2026-10-19 01:30` (see `/srv/ai-contour/backups/MORNING-CHECK.txt`).

## KB repair (2026-10-18)

- Root cause: `docker compose create` has no `--no-deps`; job services extending `metadata-indexer` tried to recreate `ai2-neo4j-kb` → name conflict / empty mount path.
- Fix: job overlay + KB/UBD overrides set `depends_on: !reset`; workspace mount smoke `OK True True`.
- Evidence: `/srv/ai-contour/backups/task12-kb-pub-20261018T103942Z` — `kb` lifecycle ready (graph + code_index published); probes XML catalog + CommonModules BSL + code_index PASS.

## Next action

1. After night: `journalctl -u ai-sync.service --since 01:00` + `contour status` (UBD+KB+klon published).
2. Keep Windows `UBD Config Sync` **Disabled**; exporters package-only.
