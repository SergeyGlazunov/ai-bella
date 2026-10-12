# Design: ai2 nightly consistency (УБД + КБ + klon_opl)

Date: 2026-10-12  
Status: draft for user review  
Approach: **2** — ai2 = sole runtime; shared helpers in `AI-contour/tools`; catch-up once after fix.

## 1. Problem

Nightly jobs pull or dump sources, but indexes used by chat are not kept consistent:

- **УБД:** Task `UBD Config Sync` updates git at `C:\MCP\ubd_config`, while ai2 MCP reads `/srv/ai2/ubd/src` (stale). Code-index looks for missing `bsl-indexer`. Graph FULL via `docker run` fails after apply. Prod `neo4j` still runs as v1 leftover.
- **КБ:** Store+dump often succeed; graph/code after dump do not complete reliably (task exit ≠ 0; no durable graph-reindex success).
- **klon_opl:** Dump+copy exist; code-index step must be part of the same nightly success path.

Prod was the first architecture; dump moved to Linux (ai2) later; prod was left in place and now conflicts with a single source of truth.

## 2. Goals

1. All three bases update on schedule into **ai2** paths and indexes.
2. Users see **no** change (same MCP host ports / WebUI tool URLs).
3. Prod УБД containers removed from the runtime path (volumes kept as backup).
4. One **manual catch-up** after the fix, then nights stay green.

## 3. Non-goals

| Out of scope | Meaning |
|--------------|---------|
| Port rename to `:8101`/`:8111` | Live stand keeps current published ports (e.g. УБД graph `:8001`, code `:8011`). |
| Chat-layer migration | Do not reinstall/move Open WebUI or OmniRoute; do not redesign tool cards unless a URL is still pointing at dead prod. |
| Contour/orchestrator cutover | Do not switch nightly from Task Scheduler PowerShell to `contour update`; fix the current PS/ai2 path. |

## 4. Target architecture

Canonical data on Linux:

| Base | Source | Tree | Graph | Code |
|------|--------|------|-------|------|
| УБД | Git FF `C:\MCP\ubd_config` (no 1C store) | `/srv/ai2/ubd/src` | ai2 УБД Neo4j + metadata indexer/graph | `ai2-bsl-indexer` |
| КБ | 1C store → IB → `DumpConfigToFiles` | `/srv/ai2/kb/src` | ai2 КБ Neo4j + full reindex | `ai2-bsl-kb` |
| klon_opl | IB dump only (**no store** — correct) | `/srv/ai2/klon_opl/src` | none | `ai2-bsl-klon-opl` |

Indexing transport: `docker.exe` + `DOCKER_HOST=tcp://127.0.0.1:2375` only. No `wsl.exe` and no compose plugin in nightly paths.

Container/network names: use **live** ai2 names on this stand (as of design time: `ai2-bsl-indexer`, `ai2-neo4j`, `ai2-metadata-indexer`, `ai2-mcp-metadata-graph` on `:8001`, KB `ai2-*-kb` / `:8201`/`:8211`, `ai2-bsl-klon-opl` on `:8311`). Do not rename to `deploy-ai2` README suffixes (`-ubd`, `:8101`) on this host. Document the live map in USER/DEVELOPMENT when implementing.

## 5. Pipelines

### 5.1 УБД

1. Preflight: Docker Engine reachable; reference indexer + `ai2-bsl-indexer` + ai2 УБД Neo4j healthy/running.
2. `git fetch` + `pull --ff-only` in `C:\MCP\ubd_config`.
3. Copy `src` → `/srv/ai2/ubd/src` (docker volume/cp pattern; no WSL).
4. Code-index: `docker exec ai2-bsl-indexer` incremental `index` + `clean` on `/repos/SRC`. **Failure fails the job** (no success with skip).
5. Graph: `docker run` `apply_changes.py`; on indexer contract exit **3** (or equivalent force signals) → FULL with `METADATA_FORCE_REINDEX=true`.
6. Persist layer OK only after that layer succeeds; otherwise write failed phase + log path.

### 5.2 КБ

Existing store → UpdateDB → dump → copy to `/srv/ai2/kb/src`, then:

1. Graph **full** reindex via `docker run` on KB indexer reference.
2. Code-index via `docker exec ai2-bsl-kb`.
3. Task exit 0 only if all steps OK; per-step logs under `logs/kb/`.

### 5.3 klon_opl

Dump → copy to `/srv/ai2/klon_opl/src` → code-index via `docker exec ai2-bsl-klon-opl`. No store step.

### 5.4 Catch-up (once)

After code is in place, one operator/agent run:

- УБД: git → copy → code → graph (FULL if required).
- КБ: from current `/srv/ai2/kb/src` → graph full → code.
- OPL: code from current tree.

Use sync lock; avoid overlapping the 01:00–03:00 task window (disable tasks or run off-window).

## 6. Components

- Shared helpers under `AI-contour/tools/` (e.g. `tools/common/`): copy-to-linux, indexer `docker run` arg builder, bsl exec, logging, docker-run preflight.
- УБД nightly entrypoint under `tools/ubd/` (logic moved/adapted from `C:\MCP\ubd_index\scripts`); Scheduler task retargeted or thin wrapper left at old path for one transition.
- Extend `tools/kb/Update-And-Export-Kb.ps1` and `tools/klon_opl/Export-KlonOpl.ps1` for missing index steps and hard fail semantics.
- Keep or relocate `sync-state.json` with an explicit path; do not claim success when a layer was skipped.

## 7. Prod teardown (option B)

After ai2 catch-up is verified:

1. Stop and **remove** prod УБД containers (`neo4j` and any leftover prod graph/bsl/query for that stack).
2. **Keep** volumes (e.g. `mcp_for_1c_neo4j-data`) as backup.
3. Ensure no scheduled task or script still references prod names for УБД writers.
4. Emergency rollback: recreate/start from backup volumes only if explicitly needed — not part of nightly.

Do not attach ai2 Neo4j to `1c-suite-net` beside a live prod `neo4j` (DNS collision).

## 8. Error handling

- Any failed mandatory step → non-zero task exit + phase marker + log path.
- Docker stderr must not abort PowerShell jobs (`2>&1` / EAP) while the container is still the source of truth for exit code.
- Heartbeats remain for long graph runs.
- Code-index lock: clear stale locks; if live indexer holds lock, fail or wait with timeout — do not silently skip.

## 9. Verification

1. Preflight PASS on ai2 references (no compose/WSL requirement).
2. Catch-up completes for all three bases.
3. Smoke: MCP on existing ports returns data consistent with fresh dump/git.
4. Next scheduled nights: three tasks exit 0; `/srv/ai2/<base>/src` mtimes/SHA align with index state; graph fingerprints newer than pre-fix baseline.
5. `docker ps -a`: no prod УБД writer containers.

## 10. Risks

| Risk | Mitigation |
|------|------------|
| УБД FULL long / OOM | Fix job/stderr first; keep logs; capacity follow-up if OOM confirmed |
| Copy vs bsl watcher race | Stabilize copy then index; reuse known watch pause/reload if required |
| KB fails without logs | Mandatory step logs + phase on failure |
| Dual Neo4j DNS | Keep ai2 nets isolated from prod `1c-suite-net` during teardown window |

## 11. Decisions log

- Scope: УБД + КБ + klon_opl.
- УБД source: **git** (not 1C store).
- Canonical runtime: **ai2**; prod empty via stop+rm containers, keep volumes.
- Catch-up: **once**, all three bases.
- Approach: shared helpers + per-base scripts in AI-contour.
- klon_opl: **no** configuration store (by design).
