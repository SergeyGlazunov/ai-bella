# Design: Linux-first migration for AI-Contour

**Date:** 2026-10-12  
**Status:** proposed, awaiting written review  
**Scope:** migrate orchestration and indexing from Windows-managed execution to WSL/Linux while keeping Windows as a temporary 1C exporter.

## 1. Purpose

AI-Contour currently depends on Windows Task Scheduler, PowerShell, WSL and Docker. The target is to move all orchestration, state, indexing and runtime services into WSL/Linux. Windows remains only where the 1C platform is required for reliable configuration export.

An independent Linux server and a Linux installation of the 1C platform are optional future steps. They are not required for the first successful migration.

## 2. Success criteria

The required migration is complete when:

1. WSL hosts the orchestrator, schedule, state, locks, sources, logs and Docker workloads.
2. Windows does not manage Docker or indexing and does not decide whether the nightly cycle succeeded.
3. Windows exports KB and `klon_opl`, creates complete transfer packages and sends them to the Linux inbox.
4. UBD is updated by Git directly from WSL/Linux.
5. SQLite is the single operational source of truth for source revisions, runs, jobs and published index state.
6. UBD, KB and `klon_opl` complete a verified nightly cycle under one orchestrator.
7. A failed write leaves reads closed until indexes are reconciled and verified.
8. The system recovers deterministically after an orchestrator or container failure.

The migration does not require a separate Linux machine or the removal of the Windows 1C exporter.

## 3. Chosen approach

Use a Linux-first architecture with a temporary Windows exporter.

```text
Current:
Windows Task Scheduler → PowerShell → WSL/Docker

Required target:
Windows 1C exporter → WSL inbox → contour → Docker indexes and MCP

Optional target:
Linux 1C exporter → Linux contour → Docker indexes and MCP
```

This approach removes Windows from the control plane without making the first migration depend on the availability of a separate server or on Linux compatibility of the 1C export process.

## 4. Responsibility boundaries

### 4.1. Windows exporter

Windows is responsible only for operations requiring the installed 1C platform:

- update KB from the configuration repository;
- run `UpdateDBCfg`;
- run `DumpConfigToFiles` for KB;
- export `klon_opl`;
- build and transfer a complete package to the Linux inbox.

Windows must not:

- start or inspect Docker containers;
- run Graph or BSL indexers;
- know container names or Docker networks;
- update Linux SQLite state directly;
- determine overall nightly success.

The Windows task is successful when a valid package has been delivered. Linux decides whether the resulting indexes are publishable.

### 4.2. WSL/Linux host

The host control plane owns:

- `systemd.timer` scheduling;
- the `contour` orchestrator;
- the SQLite state database;
- the single-writer filesystem lock;
- Git operations for UBD;
- the package inbox for KB and `klon_opl`;
- atomic source application;
- Docker job creation and reconciliation;
- verification, publication and alerts;
- log retention and state backup.

The host implementation must not depend on `/mnt/c`, `wsl.exe`, Task Scheduler or Windows Docker commands. This makes later migration from WSL to a Linux server a data and service transfer rather than a redesign.

### 4.3. Containers

Containers provide application workloads:

- Neo4j;
- Graph MCP;
- metadata indexer;
- query-builder;
- BSL indexer and search MCP;
- Open WebUI and OmniRoute;
- content probes and one-shot index jobs where appropriate.

The orchestrator remains a host service. It must be able to inspect and recover container jobs when the managed container layer is degraded. Containerizing the orchestrator is outside the required migration.

## 5. Host layout

```text
/srv/ai-contour/
  config/
    contour.yaml
  state/
    state.db
  locks/
    sync.lock
  inbox/
    kb/
    klon_opl/
  sources/
    ubd/
    kb/
    klon_opl/
  logs/
    runs/
  backups/
    state/
  compose/
    graph/
    code-index/
    chat/
```

All mutable data resides on the WSL ext4 filesystem. Windows paths may be used only by the exporter before transfer.

## 6. Source model

### 6.1. UBD

UBD is a Git source managed by WSL/Linux:

1. fetch the configured remote;
2. verify that the update is fast-forward;
3. record the target commit;
4. update the working tree only after preflight succeeds;
5. index and verify that commit.

The source revision is the Git commit SHA.

### 6.2. KB and `klon_opl`

The Windows exporter delivers an immutable package:

```text
package/
  manifest.json
  src/
  READY
```

`READY` is created last. Linux ignores incomplete packages.

The manifest contains:

- schema version;
- database identifier;
- export identifier;
- export start and finish timestamps;
- 1C platform version;
- results of required 1C operations;
- file count;
- source checksum.

The package is first copied to `.incoming-<id>`, validated and then atomically renamed to `ready-<id>`. The source revision is the verified package checksum plus export identifier.

Windows never opens or modifies the Linux SQLite database.

## 7. SQLite state

The existing SQLite implementation is retained and generalized. The database remains on the host at `/srv/ai-contour/state/state.db`.

The state model must represent:

- databases: `ubd`, `kb`, `klon_opl`;
- source revisions: Git SHA or package checksum/export ID;
- runs and their final result;
- jobs and their Docker container/exec identities;
- per-layer state for source, Graph and BSL;
- storage identity and indexer version;
- read-close intent and observed read state;
- recovery flags such as required full rebuild;
- paths to logs rather than log bodies.

Lifecycle states are:

```text
received → validated → staged → reads_closed → indexing → verified → published
```

Any failed transition records the stage, error summary and log path. Large logs and source packages are not stored in SQLite.

Before applying a new revision, the database is backed up through the SQLite backup API. Only the Linux orchestrator writes operational state.

## 8. Update pipelines

### 8.1. UBD and KB

1. Resolve and validate the new source revision.
2. Check Docker, storage, free space, configuration and required services.
3. Acquire the single-writer lock and reconcile unfinished jobs.
4. Wait for active reads to become idle.
5. Close Graph and BSL reads.
6. Disable or pause automatic BSL watchers.
7. Apply the source revision atomically.
8. Run BSL indexing.
9. Run Graph delta or full indexing according to the indexer contract.
10. Verify source revision, storage identity and indexer version.
11. Run content probes.
12. Reopen reads.
13. Mark the revision as published.

### 8.2. `klon_opl`

The same lifecycle is used without Graph stages. BSL indexing is mandatory; a skipped mandatory stage is a failed run.

### 8.3. Writer ownership

Only `contour` starts index writers. Long-running MCP containers must not independently modify the same indexes. Watchers are disabled or placed under explicit orchestrator control.

## 9. Job execution and recovery

Before starting a Docker job, `contour` creates its SQLite job record. It then creates the container or exec, records its identity and starts it.

After restart, the orchestrator:

1. acquires the lock;
2. finds unfinished jobs in SQLite;
3. compares them with Docker state;
4. records a confirmed exit code when available;
5. finishes verification or marks the job unknown;
6. never starts a second writer while a live or unknown writer may exist.

If the failure occurred before source mutation, reads remain open. If it occurred after mutation began, reads remain closed until a known revision is fully indexed and verified.

## 10. Rollback and retention

The design keeps one active set of indexes. It does not introduce blue-green index generations.

Rollback therefore means:

1. keep reads closed;
2. restore the previous confirmed source revision;
3. rebuild or reconcile Graph and BSL against that revision;
4. run verification;
5. reopen reads only after success.

Retention defaults:

- run and job metadata: 90 days;
- detailed logs: 30 days;
- accepted KB and `klon_opl` packages: the latest 3 revisions;
- SQLite backups: daily copies with a 14-day retention period.

Values are configurable, but cleanup never removes the last published source package or the state needed for current recovery.

## 11. Required migration stages

### Stage 0. Clean and baseline the project

- perform the pre-Git cleanup;
- establish source control and vendor provenance;
- create a reproducible test environment;
- record a read-only inventory of the current stand.

### Stage 1. Prepare WSL as the Linux host

- create `/srv/ai-contour`;
- move mutable data away from `/mnt/c`;
- install `contour` under systemd without enabling the timer;
- configure SQLite backup, log rotation and storage monitoring.

### Stage 2. Introduce the Linux inbox

- version the package manifest;
- change Windows KB and `klon_opl` scripts to export and transfer only;
- validate packages manually in WSL;
- prove that incomplete transfers cannot be consumed.

### Stage 3. Extend `contour` to all databases

- generalize SQLite from one Git commit to per-database revisions;
- add UBD, KB and `klon_opl` pipelines;
- make Graph and BSL mandatory according to each database profile;
- add package, Docker and content verification.

### Stage 4. Run controlled comparison

- keep old schedules available but prevent concurrent writers;
- run the new pipeline manually;
- compare source revisions, index state and MCP responses;
- execute one controlled catch-up for all databases.

### Stage 5. Switch scheduling

- complete backup and rollback drills;
- disable Windows indexing tasks;
- retain Windows export tasks only;
- enable the WSL `systemd.timer`;
- verify one manual cycle and the next scheduled nightly cycle;
- retain disabled legacy tasks for an agreed observation period.

Completion of Stage 5 satisfies the required Linux-first migration.

## 12. Optional future stages

### Optional Stage 6. Move from WSL to a Linux server or VM

This stage depends on infrastructure availability. It transfers `/srv/ai-contour`, Docker volumes and systemd units to a dedicated Linux host. The Windows exporter is redirected to the new inbox. No redesign should be required.

### Optional Stage 7. Move 1C export to Linux

This is a separate feasibility and migration project. It requires validation of:

- the approved 1C platform version on Linux;
- licensing;
- configuration repository access;
- headless `UpdateCfg`, `UpdateDBCfg` and `DumpConfigToFiles`;
- byte-level or semantic equivalence with current Windows exports;
- operational stability across several scheduled runs.

If those checks fail, Windows remains an isolated exporter without weakening the required target architecture.

## 13. Verification strategy

The migration requires:

- unit tests for state transitions, package validation and recovery;
- integration tests with temporary SQLite and fake Docker jobs;
- failure injection before and after source mutation;
- package corruption and incomplete-transfer tests;
- Graph and BSL content probes for every database;
- a controlled stand catch-up;
- one successful scheduled night after cutover;
- a documented rollback drill.

Unit tests alone are not stand proof.

## 14. Non-goals

The required migration does not include:

- a dedicated Linux server;
- removal of Windows;
- installation of the 1C platform on Linux;
- blue-green or generational indexes;
- migration or replacement of Open WebUI and OmniRoute;
- high availability;
- direct remote access to SQLite;
- a second orchestration system alongside `contour`.

## 15. Key decisions

- Required target: WSL/Linux control plane with a Windows 1C exporter.
- Optional targets: dedicated Linux host and Linux 1C exporter.
- SQLite remains the operational source of truth and lives on the host.
- `contour` remains a host service.
- Workloads and index jobs run in containers.
- Windows packages are immutable and accepted only after manifest and checksum validation.
- One writer owns every index.
- Failure after mutation is fail-closed.
- Existing indexes remain single-generation; rollback rebuilds them against a confirmed source revision.
