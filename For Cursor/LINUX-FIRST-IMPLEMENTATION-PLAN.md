# Linux-First Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move AI-Contour orchestration, state, source intake and indexing into WSL/Linux while Windows remains only a 1C export-and-transfer worker.

**Architecture:** `contour` runs as a systemd host service, stores operational state in SQLite and owns all Docker index writers. UBD comes from Git; KB and `klon_opl` arrive as immutable packages through a Linux inbox. A separate Linux machine and Linux-based 1C export remain optional.

**Tech Stack:** Python 3.11+, SQLite, pytest, PyYAML, systemd, Docker Engine/Compose, PowerShell 5.1+, 1C Designer.

**Spec:** `docs/superpowers/specs/2026-10-12-linux-first-migration-design.md`

## Global Constraints

- Read `AGENT.md`, `STATUS.md` when it exists, the design spec above and this plan before editing.
- Work in a real Git clone. Do not execute this plan against the current history-less copy.
- Preserve the current fail-closed rule: after source mutation, reads stay closed until all required layers are verified.
- Only `contour` may start index writers; no concurrent PowerShell or watcher writer is allowed.
- Windows must not call Docker, edit SQLite or decide overall nightly success.
- Mutable Linux data must live under `/srv/ai-contour`, not `/mnt/c`.
- Keep Open WebUI and OmniRoute unchanged unless a verified endpoint must be updated.
- Do not create blue-green index generations or a second active copy of indexes.
- Never commit `.env`, connection profiles, source dumps, logs, SQLite files or live credentials.
- Use TDD for Python and PowerShell helper logic. Run the smallest relevant test first, then the complete suite.
- Commit after every task. Do not combine repository cleanup, control-plane changes and stand cutover in one commit.
- Any step marked **STAND ONLY** requires the target machine, operator approval and a recorded rollback point.

## Cursor execution contract

The implementation will run on another machine. Before Task 1, Cursor must record:

- absolute repository path;
- branch name and HEAD SHA;
- Python, PowerShell, Docker and WSL versions;
- whether it has access to the real stand;
- whether `pytest` can run without installing global packages.

If the machine has no stand access, Cursor executes Tasks 1–10 and prepares Task 11 artifacts, but must not claim deployment or cutover success.

When actual container names, ports or paths differ from the repository documentation, stop the affected stand step, record the read-only inventory and update configuration or documentation through a reviewed commit. Do not guess names.

## Review Focus

1. An incomplete or corrupted package must remain ignored and must not replace the published source; Task 4 tests this.
2. A crash after source mutation must keep reads closed and prevent a second writer; Tasks 7 and 8 test this.
3. Legacy UBD configuration and state must migrate without silently losing the last published commit; Tasks 2 and 3 test this.
4. A skipped mandatory Graph or BSL stage must make the run fail; Task 7 tests this for all three database profiles.
5. Windows transfer retries must be idempotent and must never expose a partial `READY` package; Task 9 tests this.

---

## Work package A: repository and reproducibility

### Task 1: Clean repository and establish the baseline

**Files:**
- Read: `PRE-GIT-CLEANUP-AUDIT.md`
- Modify: `.gitignore`
- Modify: `release-manifest.json`
- Create: `STATUS.md`
- Create: `ROADMAP.md`
- Create: `VENDOR.md`
- Delete: paths approved by `PRE-GIT-CLEANUP-AUDIT.md`

**Interfaces:**
- Consumes: the cleanup audit and a verified clean Git clone.
- Produces: a repository without runtime data or credentials, plus documented vendor provenance.

- [ ] **Step 1: Create a safety branch and capture the baseline**

Run:

```bash
git status --short --branch
git rev-parse HEAD
git switch -c chore/pre-linux-migration-cleanup
```

Expected: a named branch and no unexplained modifications. If the directory is not a Git repository, obtain the canonical clone before continuing.

- [ ] **Step 2: Remove unconditional local artifacts**

Remove only the approved paths from the cleanup audit: `workdir/`, `logs/`, `secrets/`, filled `.env`, `.cursor/`, Python caches, `.pytest_cache`, `contour.egg-info` and `mcp-query-builder.7z`.

Expected: example files remain; filled secret files do not.

- [ ] **Step 3: Remove deprecated overlays after reference verification**

Verify no active script outside repository history references `deploy-ai2/mcp_for_1c/` or `deploy-ai2/bsl-indexer/`, then remove those two deprecated directories.

Expected: canonical `*-ubd`, `*-kb` and `bsl-klon-opl` overlays remain.

- [ ] **Step 4: Expand `.gitignore`**

Add rules for `__pycache__`, `*.py[cod]`, `*.egg-info`, virtual environments, editor state, logs, PID files, temporary files and OS metadata. Preserve the existing `.env.example` exception.

- [ ] **Step 5: Add canonical project documents**

Create:

- `STATUS.md`: current runtime, known stand facts, blockers and next action only;
- `ROADMAP.md`: approved Linux-first stages 0–5, with stages 6–7 marked optional;
- `VENDOR.md`: upstream URL, commit/tag, local patches and update method for `compose/bsl-indexer` and `compose/mcp_for_1c`.

Do not invent unknown upstream commits. Mark missing provenance as a blocking fact that must be resolved before release packaging.

- [ ] **Step 6: Rebuild `release-manifest.json`**

Update its date, paths and `missing` list from the cleaned tree. It must not list removed deprecated paths or local audit artifacts as release content.

- [ ] **Step 7: Run repository hygiene checks**

Run:

```bash
git status --short --ignored
git ls-files | grep -E '(^|/)(\.env|secrets/|logs/|workdir/|__pycache__/)|\.pyc$|\.egg-info/'
```

Expected: the second command prints nothing. Run an approved secret scanner and require zero verified live secrets.

- [ ] **Step 8: Establish the Python test environment**

Create a project-local virtual environment using Python 3.11+, install the project with its dev dependencies and run:

```bash
python -m pytest -q
```

Expected: all tests pass from a clean checkout. Record the exact test count in `STATUS.md`; do not copy the stale count from `DEVELOPMENT.md`.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "chore: clean repository for linux-first migration"
```

## Work package B: configuration and state

### Task 2: Add versioned multi-database configuration

**Files:**
- Modify: `controller/src/contour/config.py`
- Modify: `contour.example.yaml`
- Modify: `controller/tests/test_config.py`
- Create: `controller/tests/fixtures/config-v2.yaml`

**Interfaces:**
- Consumes: current `Config`, `ServicesConfig`, `StorageConfig` and adapter parsing.
- Produces: `DatabaseConfig`, `GitSourceConfig`, `PackageSourceConfig`, `RuntimeConfig` and normalized `Config.databases`.

- [ ] **Step 1: Write failing configuration tests**

Add tests asserting:

- `schema_version: 2` loads exactly `ubd`, `kb` and `klon_opl` profiles;
- UBD accepts `source.type: git` and requires `remote`, `branch`, `path`, `workspace_subdir`;
- KB and `klon_opl` accept `source.type: package` and require `inbox`, `source_path`;
- required layers are `graph + code_index` for UBD/KB and `code_index` for `klon_opl`;
- duplicate database names and unknown source types raise `ValueError`;
- the legacy configuration normalizes to one `ubd` database without losing existing fields.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python -m pytest tests/test_config.py -v` from `controller/`.

Expected: new tests fail because v2 types and parsing do not exist.

- [ ] **Step 3: Add exact configuration types**

In `config.py`, introduce:

```python
@dataclass(frozen=True)
class GitSourceConfig:
    type: Literal["git"]
    path: str
    remote: str
    branch: str
    workspace_subdir: str

@dataclass(frozen=True)
class PackageSourceConfig:
    type: Literal["package"]
    inbox: str
    source_path: str

@dataclass(frozen=True)
class DatabaseConfig:
    name: str
    source: GitSourceConfig | PackageSourceConfig
    required_layers: frozenset[str]
    services: ServicesConfig
    storage: StorageConfig
    adapters: AdaptersConfig

@dataclass(frozen=True)
class RuntimeConfig:
    root: str
    db_path: str
    lock_path: str
    logs_path: str
    backups_path: str
```

Extend `Config` with `schema_version: int`, `runtime: RuntimeConfig` and `databases: dict[str, DatabaseConfig]`. Keep a legacy normalization path until Task 11 completes.

- [ ] **Step 4: Replace the example with schema v2**

Use `/srv/ai-contour` paths and three database profiles. Put no live hostnames, credentials or private URLs in the example.

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_config.py -v`.

Expected: PASS, including legacy normalization.

- [ ] **Step 6: Commit**

```bash
git add controller/src/contour/config.py controller/tests/test_config.py controller/tests/fixtures/config-v2.yaml contour.example.yaml
git commit -m "feat: add versioned multi-database configuration"
```

### Task 3: Migrate SQLite to per-database revisions and layers

**Files:**
- Modify: `controller/src/contour/db.py`
- Modify: `controller/tests/test_db.py`
- Create: `controller/src/contour/state.py`
- Create: `controller/tests/test_state.py`

**Interfaces:**
- Consumes: `sqlite3.Connection`, database names and revision strings.
- Produces: schema version 2 and state functions listed below.

- [ ] **Step 1: Write failing migration and state tests**

Cover:

- a new database initializes schema version 2;
- an existing v1 database migrates without losing UBD `applied_commit`;
- a migration is idempotent;
- `(database, layer)` is unique;
- a run records database and target revision;
- published revision changes only after `mark_published`;
- unknown database names are rejected by the state service.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python -m pytest tests/test_db.py tests/test_state.py -v`.

- [ ] **Step 3: Implement schema migration**

Use `PRAGMA user_version = 2`. Preserve the v1 tables during migration until data is copied successfully in one transaction.

The v2 schema must contain:

- `database_state(database PRIMARY KEY, source_kind, received_revision, staged_revision, published_revision, lifecycle_status, detail, updated_at)`;
- `layer_state(database, layer, status, applied_revision, storage_id, indexer_version, updated_at, PRIMARY KEY(database, layer))`;
- `artifacts(id PRIMARY KEY, database, revision, package_path, checksum, received_at, validated_at, status)`;
- `runs` with `database`, `target_revision`, timestamps, result and message;
- `jobs` with `database`, `run_id`, stage, status, Docker identities, timestamps, exit code and log path;
- existing `meta` flags, scoped by keys that include the database where necessary.

- [ ] **Step 4: Implement the state service**

Create exact interfaces in `state.py`:

```python
class StateStore:
    def __init__(self, conn: sqlite3.Connection) -> None: ...
    def register_database(self, name: str, source_kind: str) -> None: ...
    def record_received(self, database: str, revision: str, artifact_id: str | None) -> None: ...
    def record_staged(self, database: str, revision: str) -> None: ...
    def mark_layer(self, database: str, layer: str, revision: str, status: str, storage_id: str | None, indexer_version: str | None) -> None: ...
    def mark_published(self, database: str, revision: str) -> None: ...
    def get_database(self, name: str) -> sqlite3.Row: ...
    def required_recovery(self, database: str) -> bool: ...
```

Every mutating method commits or runs inside an explicit caller transaction; choose one convention and document it in the class docstring.

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_db.py tests/test_state.py -v`.

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add controller/src/contour/db.py controller/src/contour/state.py controller/tests/test_db.py controller/tests/test_state.py
git commit -m "feat: track per-database revisions in sqlite"
```

### Task 4: Implement immutable package validation and inbox intake

**Files:**
- Create: `controller/src/contour/packages.py`
- Create: `controller/tests/test_packages.py`

**Interfaces:**
- Consumes: a package directory containing `manifest.json`, `src/` and `READY`.
- Produces: `PackageManifest`, `ValidatedPackage`, `validate_package()` and `claim_package()`.

- [ ] **Step 1: Write failing package tests**

Tests must cover:

- a valid package returns its database, export ID and checksum;
- missing `READY`, `manifest.json` or `src/Configuration.xml` is rejected;
- a checksum mismatch is rejected;
- unsupported `schema_version` is rejected;
- unexpected database names and path traversal are rejected;
- two identical deliveries produce the same artifact identity;
- validation never modifies the package;
- claiming a package uses atomic rename on the same filesystem.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python -m pytest tests/test_packages.py -v`.

- [ ] **Step 3: Implement manifest and validation types**

```python
@dataclass(frozen=True)
class PackageManifest:
    schema_version: int
    database: str
    export_id: str
    started_at: str
    finished_at: str
    platform_version: str
    operations: dict[str, str]
    file_count: int
    checksum: str

@dataclass(frozen=True)
class ValidatedPackage:
    path: Path
    manifest: PackageManifest
    revision: str

def validate_package(path: Path, allowed_databases: frozenset[str]) -> ValidatedPackage: ...
def claim_package(package: ValidatedPackage, accepted_root: Path) -> Path: ...
```

Define the checksum algorithm as SHA-256 over a deterministic manifest of relative path, file size and file SHA-256, sorted by POSIX relative path. Exclude `READY` and `manifest.json` from the source checksum.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_packages.py -v`.

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add controller/src/contour/packages.py controller/tests/test_packages.py
git commit -m "feat: validate immutable export packages"
```

### Task 5: Add source staging and atomic activation

**Files:**
- Create: `controller/src/contour/sources.py`
- Create: `controller/tests/test_sources.py`
- Modify: `controller/src/contour/gitutil.py`
- Modify: `controller/tests/test_gitutil.py`

**Interfaces:**
- Consumes: `GitSourceConfig`, `ValidatedPackage` and source root paths.
- Produces: a common `PreparedSource` and reversible activation operations.

- [ ] **Step 1: Write failing source tests**

Cover:

- Git preparation rejects dirty and non-fast-forward trees;
- package preparation never writes into the published source;
- activation uses same-filesystem rename;
- a failed activation preserves the previous published source;
- retrying the same revision is idempotent;
- rollback restores the previous revision directory;
- source paths cannot escape `/srv/ai-contour/sources`.

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_sources.py tests/test_gitutil.py -v`.

- [ ] **Step 3: Implement the source interface**

```python
@dataclass(frozen=True)
class PreparedSource:
    database: str
    revision: str
    staged_path: Path
    previous_path: Path | None

class SourceProvider(Protocol):
    def prepare(self) -> PreparedSource: ...

def activate_source(prepared: PreparedSource, published_path: Path) -> Path | None: ...
def rollback_source(published_path: Path, previous_path: Path) -> None: ...
```

Implement `GitSourceProvider` and `PackageSourceProvider`. Keep package retention separate from activation.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_sources.py tests/test_gitutil.py -v`.

- [ ] **Step 5: Commit**

```bash
git add controller/src/contour/sources.py controller/src/contour/gitutil.py controller/tests/test_sources.py controller/tests/test_gitutil.py
git commit -m "feat: stage and activate source revisions"
```

## Work package C: generalized orchestration

### Task 6: Introduce database profiles and layer adapters

**Files:**
- Create: `controller/src/contour/profiles.py`
- Create: `controller/tests/test_profiles.py`
- Modify: `controller/src/contour/index_bsl.py`
- Modify: `controller/src/contour/index_graph.py`
- Modify: `controller/tests/test_index_graph.py`

**Interfaces:**
- Consumes: `DatabaseConfig`, Docker runner, SQLite connection, run ID and source revision.
- Produces: `DatabaseProfile` and uniform index-layer calls.

- [ ] **Step 1: Write failing profile tests**

Assert:

- UBD and KB require `code_index` then `graph`;
- `klon_opl` requires only `code_index`;
- a profile cannot contain an unknown layer;
- skipped required layers raise an error;
- every job is recorded with database and target revision before start.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python -m pytest tests/test_profiles.py tests/test_index_graph.py -v`.

- [ ] **Step 3: Implement profile interfaces**

```python
@dataclass(frozen=True)
class LayerResult:
    layer: str
    revision: str
    storage_id: str
    indexer_version: str

@dataclass(frozen=True)
class DatabaseProfile:
    name: str
    required_layers: tuple[str, ...]

def profile_from_config(config: DatabaseConfig) -> DatabaseProfile: ...
def run_bsl_layer(database: DatabaseConfig, revision: str, run_id: int, conn: sqlite3.Connection, runner: DockerRunner) -> LayerResult: ...
def run_graph_layer(database: DatabaseConfig, revision: str, run_id: int, conn: sqlite3.Connection, runner: DockerRunner, may_write: Callable[[], bool]) -> LayerResult: ...
```

Refactor existing index functions behind these interfaces without changing the graph exit-code-3 FULL contract.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_profiles.py tests/test_index_graph.py -v`.

- [ ] **Step 5: Commit**

```bash
git add controller/src/contour/profiles.py controller/src/contour/index_bsl.py controller/src/contour/index_graph.py controller/tests/test_profiles.py controller/tests/test_index_graph.py
git commit -m "refactor: add database-specific index profiles"
```

### Task 7: Build the per-database lifecycle pipeline

**Files:**
- Create: `controller/src/contour/pipeline.py`
- Create: `controller/tests/test_pipeline.py`
- Modify: `controller/src/contour/verify.py`
- Modify: `controller/tests/test_verify.py`

**Interfaces:**
- Consumes: database config, source provider, state store, read controller, layer runners and probes.
- Produces: `PipelineResult` and `run_database_pipeline()`.

- [ ] **Step 1: Write failing lifecycle tests**

Test the exact transition order:

```text
received → validated → staged → reads_closed → indexing → verified → published
```

Also test:

- failure before activation leaves reads open;
- failure after activation leaves reads closed;
- UBD/KB cannot publish without both Graph and BSL at the target revision;
- `klon_opl` cannot publish without BSL;
- a skipped mandatory layer fails the run;
- probes run before reads reopen;
- publishing updates SQLite only after verification.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python -m pytest tests/test_pipeline.py tests/test_verify.py -v`.

- [ ] **Step 3: Implement the pipeline interface**

```python
@dataclass(frozen=True)
class PipelineResult:
    database: str
    revision: str
    result: Literal["published", "deferred", "failed"]
    message: str

def run_database_pipeline(
    database: DatabaseConfig,
    source: SourceProvider,
    state: StateStore,
    reads: ReadController,
    layers: LayerRunnerRegistry,
    probes: IndexProbes,
) -> PipelineResult: ...
```

Keep scheduling and global locking outside this function. It owns one database lifecycle only.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_pipeline.py tests/test_verify.py -v`.

- [ ] **Step 5: Commit**

```bash
git add controller/src/contour/pipeline.py controller/src/contour/verify.py controller/tests/test_pipeline.py controller/tests/test_verify.py
git commit -m "feat: add fail-closed database pipeline"
```

### Task 8: Reconcile multiple databases and recover unfinished jobs

**Files:**
- Modify: `controller/src/contour/reconcile.py`
- Modify: `controller/tests/test_reconcile.py`
- Modify: `controller/src/contour/reads.py`
- Modify: `controller/tests/test_reads.py`

**Interfaces:**
- Consumes: normalized `Config.databases`, `StateStore` and `run_database_pipeline()`.
- Produces: global scheduling, writer ownership and restart recovery.

- [ ] **Step 1: Write failing reconciliation tests**

Cover:

- databases run in configured deterministic order;
- only one global writer lock exists;
- a live unfinished Docker job blocks a second writer;
- a confirmed completed job is reaped and its exit code persisted;
- an unknown job keeps affected reads closed;
- a failed KB run does not falsely mark the entire nightly successful;
- a later database does not start after a fail-closed mutation failure;
- legacy UBD-only configuration still follows its existing behavior.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python -m pytest tests/test_reconcile.py tests/test_reads.py -v`.

- [ ] **Step 3: Refactor reconciliation**

Keep `reconcile(...)` as the public entry point. It must:

1. initialize/migrate SQLite;
2. acquire the global lock;
3. reconcile unfinished jobs;
4. evaluate the write window;
5. run configured database pipelines in order;
6. report one aggregate result without masking a failed database;
7. send one actionable alert containing database, stage and log path.

Scope read-state meta keys by database. Preserve the rule that a database with unknown or partial index state cannot reopen.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_reconcile.py tests/test_reads.py -v`.

- [ ] **Step 5: Run the complete controller suite**

Run: `python -m pytest -q`.

Expected: PASS with no deselected legacy tests.

- [ ] **Step 6: Commit**

```bash
git add controller/src/contour/reconcile.py controller/src/contour/reads.py controller/tests/test_reconcile.py controller/tests/test_reads.py
git commit -m "feat: reconcile all database pipelines"
```

## Work package D: Windows export boundary

### Task 9: Convert Windows scripts into package exporters

**Files:**
- Create: `tools/common/Export-Package.psm1`
- Create: `tools/common/Export-Package.Tests.ps1`
- Modify: `tools/kb/Update-And-Export-Kb.ps1`
- Modify: `tools/klon_opl/Export-KlonOpl.ps1`
- Modify: `tools/kb/README.md`
- Modify: `tools/klon_opl/README.md`
- Modify: example connection profiles; never modify or commit live profiles

**Interfaces:**
- Consumes: a completed 1C dump directory and a configured transfer destination.
- Produces: `.incoming-<export-id>` followed by atomic publication as `ready-<export-id>` with `READY` created last.

- [ ] **Step 1: Write failing Pester tests**

Test:

- deterministic SHA-256 manifest generation matches Python package validation fixtures;
- `READY` is created only after all files and `manifest.json` are present;
- a failed copy leaves only `.incoming-*` and returns non-zero;
- retrying the same export ID is idempotent;
- destination paths and credentials never appear in process arguments or normal logs;
- exporter code contains no `docker`, `wsl.exe` or SQLite invocation;
- KB manifest records `UpdateCfg`, `UpdateDBCfg` and dump results;
- `klon_opl` manifest records dump only.

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
Invoke-Pester tools/common/Export-Package.Tests.ps1
```

- [ ] **Step 3: Implement shared package functions**

Export these PowerShell functions:

```powershell
New-SourceChecksum -SourcePath <string>
New-ExportManifest -Database <string> -ExportId <string> -SourcePath <string> -Operations <hashtable> -PlatformVersion <string>
Publish-ExportPackage -SourcePath <string> -DestinationRoot <string> -Manifest <hashtable>
```

The checksum contract must exactly match Task 4.

- [ ] **Step 4: Remove Docker/index responsibilities from exporters**

KB ends after validated package publication. Remove `Invoke-KbGraphFullReindex` and related container settings. `klon_opl` likewise ends after package publication. Preserve 1C exit-code checking and per-operation export logs.

- [ ] **Step 5: Run tests and syntax checks**

Run Pester and parse all PowerShell files with `System.Management.Automation.Language.Parser`.

Expected: PASS and zero parser errors.

- [ ] **Step 6: Commit**

```bash
git add tools/common tools/kb tools/klon_opl
git commit -m "refactor: make windows tasks export packages only"
```

## Work package E: host installation and operator surface

### Task 10: Update CLI, systemd and bootstrap for `/srv/ai-contour`

**Files:**
- Modify: `controller/src/contour/cli.py`
- Modify: `controller/tests/test_cli.py`
- Modify: `bootstrap.sh`
- Modify: `systemd/ai-sync.service`
- Modify: `systemd/ai-sync.timer`
- Create: `systemd/ai-contour-tmpfiles.conf`
- Create: `systemd/ai-contour-logrotate`
- Modify: `USER.md`

**Interfaces:**
- Consumes: schema-v2 config and multi-database reconcile.
- Produces: operator commands and a WSL/Linux installation that does not enable cutover automatically.

- [ ] **Step 1: Write failing CLI tests**

Add tests for:

- `contour validate` reports all three database profiles;
- `contour status --database kb` shows source, Graph and BSL revisions;
- `contour intake --database kb` validates but does not index outside the orchestrated update;
- `contour update --database <name>` permits a controlled single-database run;
- unknown database names return non-zero;
- status output never prints secrets or remote URL userinfo.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python -m pytest tests/test_cli.py -v`.

- [ ] **Step 3: Implement CLI additions**

Keep existing commands. Add optional `--database` to `status` and `update`, plus `intake --database NAME`. `update` without a database processes all configured profiles in order.

- [ ] **Step 4: Update host installation**

Use:

- package root: `/opt/ai-contour`;
- mutable root: `/srv/ai-contour`;
- config: `/etc/ai-contour/contour.yaml`;
- service user: `ai-contour`;
- SQLite: `/srv/ai-contour/state/state.db`;
- lock: `/srv/ai-contour/locks/sync.lock`.

`bootstrap.sh` creates directories with least privilege, installs units and validates configuration, but does not enable the timer.

- [ ] **Step 5: Add state backup and retention commands**

Use SQLite backup API through a CLI command such as `contour backup-state`, not a raw copy of a live database. Configure log rotation and retention without deleting the current published package.

- [ ] **Step 6: Run tests and static validation**

Run:

```bash
python -m pytest -q
systemd-analyze verify systemd/ai-sync.service systemd/ai-sync.timer
shellcheck bootstrap.sh integrations/*.sh
```

Expected: all applicable checks pass. If a tool is unavailable on the development machine, run it in a documented Linux/WSL verification environment before review.

- [ ] **Step 7: Commit**

```bash
git add controller/src/contour/cli.py controller/tests/test_cli.py bootstrap.sh systemd USER.md
git commit -m "feat: install linux-first contour control plane"
```

## Work package F: integration proof and cutover

### Task 11: Add integration fixtures and dry-run acceptance

**Files:**
- Create: `controller/tests/integration/test_nightly_cycle.py`
- Create: `controller/tests/integration/test_recovery.py`
- Create: `checks/linux-first-preflight.md`
- Create: `checks/linux-first-catchup.md`
- Create: `checks/linux-first-cutover.md`
- Modify: `checks/README.md`
- Modify: `STATUS.md`

**Interfaces:**
- Consumes: completed Tasks 1–10.
- Produces: executable proof for package intake, all database profiles and failure recovery.

- [ ] **Step 1: Write integration tests using temporary SQLite and fake Docker**

Test one complete cycle for UBD, KB and `klon_opl`, plus:

- corrupted package refusal;
- crash after source activation;
- resumed completed container job;
- unknown writer refusal;
- Graph exit code 3 followed by FULL;
- failed mandatory BSL stage;
- no reads reopen before probes pass.

- [ ] **Step 2: Run integration and full test suites**

Run:

```bash
python -m pytest tests/integration -v
python -m pytest -q
```

Expected: PASS.

- [ ] **Step 3: Write stand runbooks with explicit stop conditions**

Each runbook must state:

- exact read-only inventory commands;
- prerequisites and expected names sourced from config;
- commands that mutate the stand;
- evidence to save;
- rollback point;
- conditions requiring immediate stop;
- prohibition on concurrent legacy and new writers.

- [ ] **Step 4: Perform a local WSL dry run**

Use synthetic or non-production source packages. Prove package transfer, intake, SQLite transitions, container job records, restart reconciliation and log paths.

Expected: evidence recorded in `STATUS.md` as local proof, not stand proof.

- [ ] **Step 5: Request code review**

Review the entire branch against the design spec, with special focus on the five Review Focus items and secret handling.

- [ ] **Step 6: Commit**

```bash
git add controller/tests/integration checks STATUS.md
git commit -m "test: add linux-first nightly acceptance proof"
```

### Task 12: Execute stand catch-up and scheduling cutover

**Files:**
- Modify: `STATUS.md`
- Modify: `DEVELOPMENT.md` or its archived successor only if project policy still requires it
- Modify: actual stand configuration outside Git, with secrets kept outside the repository

**Interfaces:**
- Consumes: reviewed implementation, passing Task 11, operator approval and stand access.
- Produces: WSL `systemd.timer` as the only nightly orchestrator; Windows remains export-only.

**STAND ONLY — do not perform from a development copy.**

- [ ] **Step 1: Capture read-only stand inventory**

Record tasks, containers, networks, volumes, ports, source revisions, index fingerprints, WebUI tool endpoints, disk space and current health. Compare actual values with schema-v2 config.

- [ ] **Step 2: Complete backup and rollback drills**

Prove restoration of SQLite state and the chat-layer data required by existing policy. Record recovery time and exact rollback commands.

- [ ] **Step 3: Install without enabling the timer**

Install the reviewed commit into WSL, create `/srv/ai-contour`, configure secrets externally and run `contour validate`.

- [ ] **Step 4: Transfer test packages**

Run Windows exporters for KB and `klon_opl`. Verify that packages arrive atomically and that incomplete test packages are ignored.

- [ ] **Step 5: Execute one controlled catch-up**

Disable or otherwise fence legacy writers for the catch-up window. Run UBD, KB and `klon_opl`; require matching revisions, layer state and successful probes.

- [ ] **Step 6: Verify user-facing behavior**

Confirm existing Open WebUI users, models and MCP tools behave as before. Do not update chat images during this cutover.

- [ ] **Step 7: Switch writer ownership**

Disable Windows indexing tasks. Leave only export-and-transfer tasks. Enable the WSL timer and record its next trigger.

- [ ] **Step 8: Verify the next scheduled night**

Require all three database runs to publish successfully. Check SQLite, source revisions, Graph/BSL state, probes, alerts and logs.

- [ ] **Step 9: Close the migration stage**

Update `STATUS.md` with evidence, actual paths and remaining optional stages. Keep legacy tasks disabled for the agreed observation period; do not delete rollback assets yet.

- [ ] **Step 10: Commit documentation only**

```bash
git add STATUS.md DEVELOPMENT.md
git commit -m "docs: record linux-first cutover evidence"
```

If `DEVELOPMENT.md` has already been archived or removed under Task 1, add only the active status/history file defined by the cleaned documentation structure.

## Final acceptance

Cursor may report the required migration complete only when:

- the repository is clean and secret-scanned;
- the full Python and PowerShell test suites pass from documented environments;
- WSL owns schedule, lock, SQLite and all index writers;
- Windows scripts only export and transfer packages;
- UBD, KB and `klon_opl` have published verified revisions;
- failure after mutation is proven fail-closed;
- one manual catch-up and the next scheduled night succeed;
- rollback evidence exists;
- stages 6 and 7 remain explicitly optional.

Without target-machine access, the correct completion statement is: **implementation and local verification complete; stand deployment and cutover pending**.
