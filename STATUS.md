# STATUS

**Updated:** 2026-10-18  
**Repo:** https://github.com/SergeyGlazunov/ai-bella  
**Stand:** ADM-SAPHX-TECH — contour owns index/publish (`ai-contour.timer` enabled)

## Active runtime

| Role | Fact |
|------|------|
| Canon data | `/srv/ai2/{ubd,kb,klon_opl}/src` (contour sources: `/srv/ai-contour/sources/{ubd,kb,klon_opl}`) |
| Chat | Open WebUI `:3000` (healthy); OmniRoute `:20128` |
| MDD MCP | `:8080` (do not confuse with WebUI) |
| УБД graph MCP | `:8101` (`ai2-mcp-metadata-graph` on `ai2-stack`; WebUI bearer = shared MCP secret) |
| УБД code MCP | `:8011` (`ai2-bsl-indexer`) |
| КБ graph / code | `:8201` / `:8211` — published |
| OPL code | `:8311` (`ai2-bsl-klon-opl`) |
| Orchestrator | **`ai-contour.timer` enabled** — `01:30 Asia/Vladivostok` |

## Nightly

| Task | When | Entry |
|------|------|--------|
| **contour** (`ai-contour.timer`) | 01:30 Asia/Vladivostok | `/opt/ai-contour/venv/bin/contour update` |
| AI-contour KB nightly export | 01:00 | `tools/kb/Update-And-Export-Kb.ps1` (export/package only) |
| AI-contour klon_opl nightly export | 02:00 | `tools/klon_opl/Export-KlonOpl.ps1` (export/package only) |

Legacy `UBD Config Sync` indexing must stay **Disabled**. Contour owns UBD index/publish. Commands: [NIGHTLY.md](NIGHTLY.md).

## Agent / toolchain (this machine)

| Tool | Version / note |
|------|----------------|
| Git | 2.56 (Windows); remote `origin` → ai-bella |
| Python | 3.12.10 (`controller` venv; needs `tzdata` on Windows for tests) |
| Docker Engine | via `DOCKER_HOST=tcp://127.0.0.1:2375` |
| WSL | Ubuntu Running (v2) |

## Blockers / open

1. Vendor provenance for `compose/mcp_for_1c` / `compose/bsl-indexer` incomplete — see [README.md](README.md) § Vendor.
2. Live secrets stay on disk (gitignored); not in Git.

## Next action

1. Keep Windows `UBD Config Sync` **Disabled**; exporters package-only.
2. After night: `journalctl -u ai-contour.service --since 01:00` + `contour status` (UBD+KB+klon published).
