# ROADMAP

Two horizons. Do not start stand cutover to `contour` until the short horizon is green.

## Horizon A — PS/ai2 stable (current)

Spec: `docs/superpowers/specs/2026-10-12-ai2-nightly-consistency-design.md`  
Ops list: `For Cursor/to-do.md`

| Stage | Status | Notes |
|-------|--------|--------|
| A0 Cleanup + Git | done | `ai-bella`, secrets gitignored |
| A1 Scripts → ai2 + hard-fail layers | done | UBD/KB/OPL; Neo4j FULL timeout bootstrap |
| A2 Register UBD nightly | done | Interactive/Limited like KB |
| A3 Fix KB graph prepare (docker inspect EAP) | done | `47716fc` |
| A4 Green nights (2–3) | **in progress** | Wait; no daytime FULL without consent |
| A5 Prod УБД teardown (option B) | blocked on A4 | stop+rm prod writers; keep volumes |

## Horizon B — Linux-first / Python orchestrator

Spec: `For Cursor/LINUX-FIRST-DESIGN.md`  
Plan: `For Cursor/LINUX-FIRST-IMPLEMENTATION-PLAN.md`

| Stage | Tasks | Status |
|-------|-------|--------|
| 0 Design accepted | — | accepted with operational priority §1.1 |
| 1 Repo baseline | Task 1 | done |
| 2 Config + SQLite + inbox + sources | Tasks 2–5 | **done in kit** (ops subset; exotic FS deferred) |
| 3 Contour pipeline | Tasks 6–8 | Task 6 done; **next: Task 7** |
| 4 Windows = exporter only | Task 9 | pending |
| 5 Host install (`/srv/ai-contour`, systemd) | Task 10 | pending |
| 6 Dry-run acceptance | Task 11 | pending |
| 7 Stand catch-up + schedule cutover | Task 12 | **STAND ONLY** |

### Optional (plan stages 6–7 in design numbering)

- Separate Linux server (not WSL).  
- Linux-native 1C export (drop Windows exporter).

Marked **optional** — not required for first successful migration.

## Explicit non-goals until cutover

- Port rename to `:8101`/`:8111` on the live stand.  
- Reinstall Open WebUI / OmniRoute.  
- Dual concurrent writers (PS nightly + contour).  
- Daytime UBD FULL without operator consent.
