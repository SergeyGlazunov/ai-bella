from __future__ import annotations

import shutil
import sqlite3
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from contour import alert as alert_mod
from contour import dockerutil, gitutil
from contour.adapters import MissingAdapters, build_probes, require_idle_adapter
from contour.config import (
    DatabaseConfig,
    GitSourceConfig,
    PackageSourceConfig,
    enabled_databases,
)
from contour.db import get_component, get_meta, init_db, set_meta
from contour.index_bsl import index_bsl
from contour.index_graph import (
    copy_job_logs,
    index_graph,
    set_graph_full_needed,
)
from contour.lock import acquire
from contour.reads import (
    apply_close,
    open_reads,
    request_close,
    verify_closed,
    wait_server_idle,
)
from contour.schedule import duration_warn_seconds, may_write, remaining_write_seconds
from contour.runtime_policy import DEFAULT_RUNTIME_POLICY, RuntimePolicy, WriterStillAlive
from contour.state import StateStore
from contour.verify import (
    Samples,
    SubprocessGitDiffRunner,
    sample_current_tree,
    sample_paths,
    verify_indexes,
)
from contour.watch_bsl import pause_watch, restore_watch, watch_is_paused

DEFAULT_DB = "/srv/ai-contour/state/state.db"
DEFAULT_LOCK = "/srv/ai-contour/locks/sync.lock"

DatabaseRunner = Callable[..., str]


@dataclass(frozen=True)
class RuntimePaths:
    db_path: str
    lock_path: str


def resolve_runtime_paths(
    cfg,
    *,
    paths: RuntimePaths | None = None,
    db_path: str | None = None,
    lock_path: str | None = None,
) -> RuntimePaths:
    """Resolve SQLite/lock paths without letting empty CLI defaults win.

    Priority: explicit ``paths`` → explicit ``db_path``/``lock_path`` kwargs →
    ``cfg.runtime`` → legacy defaults.
    """
    if paths is not None:
        return paths
    runtime = getattr(cfg, "runtime", None)
    resolved_db = db_path
    resolved_lock = lock_path
    if resolved_db is None:
        resolved_db = runtime.db_path if runtime is not None else DEFAULT_DB
    if resolved_lock is None:
        resolved_lock = runtime.lock_path if runtime is not None else DEFAULT_LOCK
    return RuntimePaths(db_path=resolved_db, lock_path=resolved_lock)


def aggregate_database_results(results: Sequence[tuple[str, str]]) -> str:
    """Combine per-database outcomes: any failed → failed; else deferred; else success."""
    statuses = [status for _name, status in results]
    if any(status == "failed" for status in statuses):
        return "failed"
    if any(status == "deferred" for status in statuses):
        return "deferred"
    return "success"


def format_nightly_alert(
    conn: sqlite3.Connection, outcomes: Sequence[tuple[str, str]]
) -> str:
    """One actionable alert body: database, stage, revision, reason, log_path."""
    lines: list[str] = ["contour nightly aggregate:"]
    for name, status in outcomes:
        if status == "success":
            continue
        row = conn.execute(
            """
            SELECT lifecycle_status, published_revision, detail
            FROM database_state WHERE database = ?
            """,
            (name,),
        ).fetchone()
        run = conn.execute(
            """
            SELECT id, target_revision, result, message FROM runs
            WHERE database = ? ORDER BY id DESC LIMIT 1
            """,
            (name,),
        ).fetchone()
        job = None
        if run is not None:
            job = conn.execute(
                """
                SELECT stage, log_path FROM jobs
                WHERE database = ? AND run_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (name, int(run["id"])),
            ).fetchone()
        reason = (run["message"] if run is not None else None) or (
            row["detail"] if row is not None else None
        ) or status
        if job is not None:
            stage = str(job["stage"] or "?")
            log_path = str(job["log_path"] or "(log unavailable)")
        else:
            # Prefer explicit stage= from the run message over a stale job.
            stage = "?"
            if isinstance(reason, str) and "stage=" in reason:
                part = reason.split("stage=", 1)[1]
                stage = part.split()[0].strip(",") or "?"
            elif row is not None and row["lifecycle_status"]:
                stage = str(row["lifecycle_status"])
            log_path = "(log unavailable)"
        revision = (run["target_revision"] if run is not None else None) or (
            row["published_revision"] if row is not None else None
        ) or "?"
        lines.append(
            f"- {name}: stage={stage} revision={revision} "
            f"result={status} reason={reason} log={log_path}"
        )
    if len(lines) == 1:
        lines.append("- (no failed/deferred databases)")
    return "\n".join(lines)


def _safe_send(send, cfg, message: str) -> None:
    try:
        send(cfg, message)
    except Exception:
        pass


def select_databases(
    cfg, database: str | None = None
) -> list[DatabaseConfig]:
    """Enabled databases in YAML order, optionally filtered to one name."""
    databases = cfg.databases or {}
    if database is not None:
        selected = databases.get(database)
        if selected is None:
            raise ValueError(f"unknown database: {database}")
        if not selected.enabled:
            raise ValueError(f"database disabled: {database}")
        return [selected]
    return list(enabled_databases(cfg).values())


def _normalize_db_result(status: str) -> str:
    if status == "published":
        return "success"
    return status


def _source_kind(database: DatabaseConfig) -> str:
    return "git" if isinstance(database.source, GitSourceConfig) else "package"


def _accepted_root_for(cfg, database: DatabaseConfig) -> Path:
    return Path(cfg.runtime.root) / "accepted" / database.name


def _package_finished_at(package) -> datetime:
    from contour.packages import _parse_timestamp

    return _parse_timestamp(package.manifest.finished_at, "finished_at")


def _published_package_finished_at(
    accepted_root: Path, database: str, published_revision: str | None
) -> datetime | None:
    """Finished_at of the accepted package that matches published_revision."""
    if not published_revision:
        return None
    from contour.packages import PackageError, iter_ready_package_dirs, validate_package

    for path in iter_ready_package_dirs(accepted_root):
        try:
            package = validate_package(path, frozenset({database}))
        except PackageError:
            continue
        if package.revision == published_revision:
            return _package_finished_at(package)
    return None


def _package_is_stale(
    package,
    *,
    published_revision: str | None,
    published_finished_at: datetime | None,
) -> bool:
    """True when package must not become the next update (already applied or older)."""
    if published_revision and package.revision == published_revision:
        return True
    if published_finished_at is not None:
        return _package_finished_at(package) <= published_finished_at
    return False


def _live_layer_identities(
    database: DatabaseConfig, layer: str, docker
) -> tuple[str, str]:
    """Return (storage_id, indexer_version) for a required layer from live Docker.

    Raises on inspect failure — callers must not treat unknown identity as matching.
    """
    if layer == "code_index":
        storage = dockerutil.volume_identity(
            database.storage.code_index_volume, runner=docker
        )
        version = dockerutil.image_id(
            database.services.bsl_container, runner=docker
        )
        return storage, version
    if layer == "graph":
        storage = dockerutil.volume_identity(
            database.storage.graph_volume, runner=docker
        )
        version = dockerutil.image_id(
            database.services.graph_mcp, runner=docker
        )
        return storage, version
    raise ValueError(f"unsupported layer for live identity: {layer}")


def _layers_sqlite_ready(
    database: DatabaseConfig,
    revision: str,
    conn: sqlite3.Connection,
) -> bool:
    for layer in database.required_layers:
        row = conn.execute(
            """
            SELECT status, applied_revision FROM layer_state
            WHERE database = ? AND layer = ?
            """,
            (database.name, layer),
        ).fetchone()
        if (
            row is None
            or str(row["status"]) != "ready"
            or str(row["applied_revision"] or "") != revision
        ):
            return False
    return True


def _live_storage_status(
    database: DatabaseConfig,
    revision: str,
    conn: sqlite3.Connection,
    docker,
) -> str:
    """Return match|changed|unknown|not_ready for required layers at revision."""
    if not _layers_sqlite_ready(database, revision, conn):
        return "not_ready"
    for layer in database.required_layers:
        try:
            live_storage, live_version = _live_layer_identities(
                database, layer, docker
            )
        except Exception:
            return "unknown"
        if storage_identity_changed(
            conn,
            database.name,
            layer,
            current_storage_id=live_storage,
            current_indexer_version=live_version,
        ):
            return "changed"
    return "match"


def _layers_ready_with_live_storage(
    database: DatabaseConfig,
    revision: str,
    conn: sqlite3.Connection,
    docker,
) -> bool:
    """True only when SQLite ready and live identity is confirmed to match."""
    return _live_storage_status(database, revision, conn, docker) == "match"


def _published_needs_recovery(
    database: DatabaseConfig,
    conn: sqlite3.Connection,
    state: StateStore,
    docker,
) -> bool:
    from contour.index_graph import is_graph_full_needed
    from contour.sources import published_path_for, read_revision

    if state.required_recovery(database.name):
        return True
    if is_graph_full_needed(conn, database.name):
        return True
    published = published_path_for(database)
    if not published.is_dir():
        return False
    revision = read_revision(published)
    if not revision:
        return False
    status = _live_storage_status(database, revision, conn, docker)
    # Unknown inspect → defer, do not force rebuild from published tree.
    return status in {"not_ready", "changed"}


def _docker_for_database(database: DatabaseConfig, base_docker):
    """Wrap ``base_docker`` with this database's idle_probe when configured."""
    adapters = getattr(database, "adapters", None)
    idle = getattr(adapters, "idle_probe", None) if adapters is not None else None
    if not idle:
        return base_docker
    from contour.adapters import CommandEngine, profile_adapter_env

    underlying = getattr(base_docker, "_runner", base_docker)
    return CommandEngine(
        list(idle),
        runner=underlying,
        env=profile_adapter_env(database),
    )


def _watch_reload_for_database(database: DatabaseConfig, fallback=None):
    """Profile-scoped BSL reload. Never hide failure behind another database."""
    del fallback

    def _reload() -> None:
        from contour.watch_bsl import reload_bsl_daemon

        reload_bsl_daemon(database)

    return _reload


def _sole_enabled_database_name(cfg) -> str | None:
    enabled = enabled_databases(cfg)
    if len(enabled) != 1:
        return None
    return next(iter(enabled))


def _runtime_observe_cap(cfg) -> float | None:
    """runtime.writer_max_observe_seconds when set (None → policy default)."""
    runtime = getattr(cfg, "runtime", None)
    if runtime is None:
        return None
    return getattr(runtime, "writer_max_observe_seconds", None)


def _preflight_free_space(cfg) -> None:
    """Refuse heavy update work when free space under runtime.root is low."""
    from contour import maintenance

    runtime = getattr(cfg, "runtime", None)
    root = (
        Path(runtime.root)
        if runtime is not None and runtime.root
        else Path("/srv/ai-contour")
    )
    min_free_gb = getattr(runtime, "min_free_gb", 5.0) if runtime is not None else 5.0
    maintenance.check_free_space(root, min_free_gb=float(min_free_gb))


def _prune_after_publish(cfg, database, conn: sqlite3.Connection, state, send) -> None:
    """Post-publish retention (review 1.2). Never fails the published result."""
    from contour import maintenance
    from contour.config import PackageSourceConfig
    from contour.sources import published_path_for

    runtime = getattr(cfg, "runtime", None)

    def _int_knob(name: str, default: int) -> int:
        value = getattr(runtime, name, default) if runtime is not None else default
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return default

    try:
        if isinstance(database.source, PackageSourceConfig):
            published_revision = None
            try:
                published_revision = state.get_database(database.name)[
                    "published_revision"
                ]
            except Exception:
                published_revision = None
            maintenance.prune_accepted_packages(
                _accepted_root_for(cfg, database),
                published_revision=published_revision,
                keep=_int_knob("retain_packages", 3),
            )
        maintenance.prune_previous_sources(published_path_for(database), keep=1)
        maintenance.prune_run_history(conn, days=_int_knob("retain_runs_days", 90))
    except Exception as exc:
        _safe_send(send, cfg, f"{database.name}: retention prune failed: {exc}")


def _default_v2_database_runner(
    database: DatabaseConfig,
    *,
    cfg,
    conn: sqlite3.Connection,
    state: StateStore,
    docker=None,
    send=None,
    may_write=None,
    watch_reload=None,
    **kwargs,
) -> str:
    """Production path: package/git source → ReadController → pipeline."""
    del kwargs
    from contour.adapters import build_probes
    from contour.index_graph import is_graph_full_needed
    from contour.packages import (
        PackageError,
        choose_newest_valid_package,
        claim_package,
        validate_package,
    )
    from contour.pipeline import ProfilesLayerRunner, run_database_pipeline
    from contour.reads import DatabaseReadController
    from contour.sources import (
        build_source_provider,
        published_path_for,
        read_revision,
    )

    provider = None
    if isinstance(database.source, PackageSourceConfig):
        inbox = Path(database.source.inbox)
        accepted_root = _accepted_root_for(cfg, database)
        inbox_pkg, warnings = choose_newest_valid_package(inbox, database.name)
        for item in warnings:
            if send is not None:
                _safe_send(send, cfg, f"{database.name}: package candidate: {item}")
        accepted_pkg, accepted_warnings = choose_newest_valid_package(
            accepted_root, database.name
        )
        for item in accepted_warnings:
            if send is not None:
                _safe_send(send, cfg, f"{database.name}: accepted candidate: {item}")

        db_row = state.get_database(database.name)
        published_revision = db_row["published_revision"]
        published_finished_at = _published_package_finished_at(
            accepted_root, database.name, published_revision
        )

        candidates: list[tuple[str, object]] = []
        if inbox_pkg is not None and not _package_is_stale(
            inbox_pkg,
            published_revision=published_revision,
            published_finished_at=published_finished_at,
        ):
            candidates.append(("inbox", inbox_pkg))
        if accepted_pkg is not None and not _package_is_stale(
            accepted_pkg,
            published_revision=published_revision,
            published_finished_at=published_finished_at,
        ):
            candidates.append(("accepted", accepted_pkg))
        candidates.sort(
            key=lambda item: (
                _package_finished_at(item[1]),
                item[1].manifest.export_id,
            ),
            reverse=True,
        )

        package = None
        if candidates:
            origin, chosen = candidates[0]
            if origin == "inbox":
                try:
                    claimed = claim_package(chosen, accepted_root)
                    package = validate_package(claimed, frozenset({database.name}))
                except (PackageError, OSError) as exc:
                    raise RuntimeError(f"package claim failed: {exc}") from exc
            else:
                package = chosen

        if package is not None:
            published = published_path_for(database)
            current = read_revision(published) if published.is_dir() else None
            db_state = state.get_database(database.name)
            fully_published = (
                str(db_state["published_revision"] or "") == package.revision
                and not state.required_recovery(database.name)
            )
            if (
                fully_published
                and current == package.revision
                and not is_graph_full_needed(conn, database.name)
            ):
                storage = _live_storage_status(
                    database, package.revision, conn, docker
                )
                if storage == "match":
                    run_id = state.start_run(database.name, package.revision)
                    state.finish_run(run_id, "deferred", "already published")
                    return "deferred"
                if storage == "unknown":
                    run_id = state.start_run(database.name, package.revision)
                    state.finish_run(
                        run_id, "deferred", "storage identity unknown"
                    )
                    return "deferred"
                # changed / not_ready → rebuild from this package
            provider = build_source_provider(database, cfg, package=package)
        elif _published_needs_recovery(database, conn, state, docker):
            published = published_path_for(database)
            if published.is_dir() and read_revision(published):
                provider = build_source_provider(database, cfg, recovery=True)
            else:
                run_id = state.start_run(database.name, "(pending)")
                state.finish_run(run_id, "deferred", "no new package")
                state.set_lifecycle(
                    database.name, "deferred", detail="no new package"
                )
                return "deferred"
        else:
            published = published_path_for(database)
            current = read_revision(published) if published.is_dir() else None
            if current and _live_storage_status(
                database, current, conn, docker
            ) == "unknown":
                run_id = state.start_run(database.name, current)
                state.finish_run(run_id, "deferred", "storage identity unknown")
                state.set_lifecycle(
                    database.name, "deferred", detail="storage identity unknown"
                )
                return "deferred"
            run_id = state.start_run(database.name, "(pending)")
            state.finish_run(run_id, "deferred", "no new package")
            state.set_lifecycle(database.name, "deferred", detail="no new package")
            return "deferred"
    else:
        if _published_needs_recovery(database, conn, state, docker):
            published = published_path_for(database)
            if published.is_dir() and read_revision(published):
                provider = build_source_provider(database, cfg, recovery=True)
            else:
                provider = build_source_provider(database, cfg)
        else:
            provider = build_source_provider(database, cfg)

    reload_fn = (
        watch_reload
        if watch_reload is not None
        else _watch_reload_for_database(database, None)
    )
    # Review 1.2: refuse heavy update work when free space is low — before
    # any clone/copy closes reads or touches sources.
    _preflight_free_space(cfg)
    reads = DatabaseReadController(
        database, conn, docker, watch_reload=reload_fn
    )
    probes = build_probes(
        database, docker, required_layers=frozenset(database.required_layers)
    )
    runtime = getattr(cfg, "runtime", None)
    logs_path = runtime.logs_path if runtime is not None else None
    layers = ProfilesLayerRunner(
        conn=conn,
        docker_runner=docker,
        may_write=may_write,
        logs_path=logs_path,
        observe_cap=_runtime_observe_cap(cfg),
    )
    result = run_database_pipeline(
        database,
        provider,
        state,
        reads,
        layers,
        probes,
        conn=conn,
        may_write=may_write,
    )
    status = _normalize_db_result(result.result)
    if status == "success":
        # Review 1.2: retention runs only after a verified publish and never
        # fails the published result.
        _prune_after_publish(cfg, database, conn, state, send)
    return status


class _GitutilOps:
    def fetch(self, repo) -> None:
        gitutil.fetch(repo)

    def is_dirty(self, repo) -> bool:
        return gitutil.is_dirty(repo)

    def ff_only(self, repo, branch: str) -> str:
        return gitutil.ff_only(repo, branch)

    def rev_parse(self, repo, ref: str) -> str:
        result = subprocess.run(
            ["git", "rev-parse", ref],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def diff_name_only(self, repo, old_sha: str, new_sha: str) -> list[str]:
        return SubprocessGitDiffRunner().diff_name_only(repo, old_sha, new_sha)

    def diff_deletions(self, repo, old_sha: str, new_sha: str) -> list[str]:
        return SubprocessGitDiffRunner().diff_deletions(repo, old_sha, new_sha)


class _Engine:
    """Default docker adapter. idle_probe is not configured (B08: no boolean stubs)."""

    def __init__(self, runner: dockerutil.DockerRunner | None = None) -> None:
        self._runner = runner if runner is not None else dockerutil.SubprocessRunner()

    def run(
        self, cmd: list[str], *, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self._runner.run(cmd, timeout=timeout)

    def idle_configured(self) -> bool:
        return False

    def idle_probe(self) -> bool:
        raise MissingAdapters("idle_probe adapter is not configured")


def _enabled(cfg) -> list[str]:
    feats = cfg.features
    names: list[str] = []
    if feats is None or feats.code_index:
        names.append("code_index")
    if feats is None or feats.graph:
        names.append("graph")
    return names


def _start_run(conn: sqlite3.Connection, target: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO runs (target_commit, started_at) VALUES (?, ?)",
        (target, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def _finish_run(
    conn: sqlite3.Connection,
    run_id: int | None,
    target: str,
    result: str,
    message: str,
) -> None:
    if run_id is None:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        UPDATE runs
        SET target_commit = ?, finished_at = ?, result = ?, message = ?
        WHERE id = ?
        """,
        (target, now, result, message, run_id),
    )
    conn.commit()


CLOSED_READS_ALERT = (
    "reads_closed=1; recover with `contour open` or "
    "checks/stand_close_reads.md §6"
)


def _parse_wait_exit(stdout: str) -> int | None:
    text = (stdout or "").strip().splitlines()
    if not text:
        return None
    try:
        return int(text[-1].strip())
    except ValueError:
        return None


class WriterStateUnknown(RuntimeError):
    """Previous writer exists but its result cannot be determined."""


def _container_status(docker, container_id: str) -> str:
    try:
        result = docker.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", container_id],
            timeout=DEFAULT_RUNTIME_POLICY.docker_command_timeout_seconds,
        )
    except Exception as exc:
        raise WriterStateUnknown(
            f"cannot inspect writer {container_id}: {exc}"
        ) from exc
    status = (getattr(result, "stdout", "") or "").strip()
    if not status:
        raise WriterStateUnknown(f"empty inspect for writer {container_id}")
    return status


# Job stage → index layer (single mapping for reap / recovery).
JOB_STAGE_LAYER: dict[str, str] = {
    "bsl_index": "code_index",
    "bsl_clean": "code_index",
    "graph": "graph",
    "graph_full": "graph",
}


def job_stage_layer(stage: str) -> str | None:
    return JOB_STAGE_LAYER.get(stage)


def storage_volumes_for(database: DatabaseConfig) -> frozenset[str]:
    """Physical index volumes owned by a database profile."""
    volumes: set[str] = set()
    layers = database.required_layers
    if "code_index" in layers and database.storage.code_index_volume:
        volumes.add(database.storage.code_index_volume)
    if "graph" in layers and database.storage.graph_volume:
        volumes.add(database.storage.graph_volume)
    return frozenset(volumes)


def writer_resource_keys(database: DatabaseConfig) -> frozenset[str]:
    """Volumes and writer containers that must not host a second contour writer."""
    keys: set[str] = set(storage_volumes_for(database))
    layers = database.required_layers
    if "code_index" in layers and database.services.bsl_container:
        keys.add(f"bsl:{database.services.bsl_container}")
    if "graph" in layers and database.services.graph_mcp:
        keys.add(f"graph:{database.services.graph_mcp}")
    return frozenset(keys)


def databases_share_storage(left: DatabaseConfig, right: DatabaseConfig) -> bool:
    return bool(writer_resource_keys(left) & writer_resource_keys(right))


def storage_identity_changed(
    conn: sqlite3.Connection,
    database: str,
    layer: str,
    *,
    current_storage_id: str | None,
    current_indexer_version: str | None,
) -> bool:
    """True when recorded layer identity no longer matches live storage/image."""
    row = conn.execute(
        """
        SELECT storage_id, indexer_version, status, applied_revision
        FROM layer_state
        WHERE database = ? AND layer = ?
        """,
        (database, layer),
    ).fetchone()
    if row is None:
        return True
    if str(row["status"] or "") != "ready":
        return True
    if current_storage_id is not None and str(row["storage_id"] or "") != current_storage_id:
        return True
    if (
        current_indexer_version is not None
        and str(row["indexer_version"] or "") != current_indexer_version
    ):
        return True
    return False


def writer_blocks_database(
    conn: sqlite3.Connection,
    database: DatabaseConfig,
    peers: Sequence[DatabaseConfig],
    docker,
    *,
    cfg=None,
) -> str | None:
    """Reason to skip a new writer, or None when the index is free to write."""
    own = reap_database_jobs(conn, database.name, docker, cfg=cfg)
    if own == "busy":
        return f"{database.name}: previous indexer still running"
    if own == "unknown":
        return f"{database.name}: previous writer state unknown"
    for peer in peers:
        if peer.name == database.name:
            continue
        if not databases_share_storage(database, peer):
            continue
        peer_state = reap_database_jobs(conn, peer.name, docker, cfg=cfg)
        if peer_state == "busy":
            return (
                f"{database.name}: shared storage busy "
                f"(writer in {peer.name})"
            )
        if peer_state == "unknown":
            return (
                f"{database.name}: shared storage writer unknown "
                f"({peer.name})"
            )
    return None


def _reap_unfinished_rows(
    conn: sqlite3.Connection,
    docker,
    *,
    database: str | None,
    raise_unknown: bool,
    cfg=None,
) -> str:
    """Reap unfinished jobs. Return idle|busy. Unknown → raise or return via caller."""
    if database is None:
        rows = conn.execute(
            """
            SELECT id, database, stage, container_id, exec_id, status FROM jobs
            WHERE status NOT IN ('ok', 'failed', 'removed')
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, database, stage, container_id, exec_id, status FROM jobs
            WHERE database = ?
              AND status NOT IN ('ok', 'failed', 'removed')
            """,
            (database,),
        ).fetchall()
    busy = False
    for row in rows:
        job_db = str(row["database"] or "ubd")
        cid = row["container_id"] or ""
        exec_id = row["exec_id"] or ""
        if exec_id:
            if not cid:
                exc = WriterStateUnknown(
                    f"job {row['id']} has exec_id but no container"
                )
                if raise_unknown:
                    raise exc
                raise exc
            try:
                running = dockerutil.list_exec_ids(cid, runner=docker)
            except Exception as exc:
                err = WriterStateUnknown(
                    f"cannot list exec ids for {cid}: {exc}"
                )
                if raise_unknown:
                    raise err from exc
                raise err from exc
            if exec_id in running:
                busy = True
                continue
            # Never started (crash between INSERT and start_exec) — cancel.
            if (row["status"] or "") == "created":
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, finished_at = ?, exit_code = ?
                    WHERE id = ?
                    """,
                    ("failed", now, -1, row["id"]),
                )
                conn.commit()
                continue
            try:
                exit_code = int(dockerutil.wait_exec(cid, exec_id, runner=docker))
            except WriterStillAlive:
                busy = True
                continue
            except dockerutil.ExecRecordGone:
                # Daemon collected the finished exec record; the exit code is
                # unrecoverable. Resolve as failed (never success) and let the
                # next run resume/rebuild — do not stay "unknown" forever.
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'failed', finished_at = ?, exit_code = NULL
                    WHERE id = ?
                    """,
                    (now, row["id"]),
                )
                set_meta(
                    conn,
                    "last_exec_record_gone",
                    f"{now} job={row['id']} database={job_db} exec={exec_id}",
                )
                conn.commit()
                continue
            except Exception as exc:
                err = WriterStateUnknown(
                    f"wait_exec failed for {exec_id}: {exc}"
                )
                if raise_unknown:
                    raise err from exc
                raise err from exc
            now = datetime.now(timezone.utc).isoformat()
            job_status = "ok" if exit_code == 0 else "failed"
            conn.execute(
                """
                UPDATE jobs
                SET exit_code = ?, status = ?, finished_at = ?
                WHERE id = ?
                """,
                (exit_code, job_status, now, row["id"]),
            )
            if exit_code == 0 and str(row["stage"] or "") == "graph_full":
                set_graph_full_needed(conn, job_db, False)
            elif exit_code == 3 and job_stage_layer(str(row["stage"] or "")) == "graph":
                set_graph_full_needed(conn, job_db, True)
            conn.commit()
            continue
        if not cid:
            err = WriterStateUnknown(f"job {row['id']} has no container_id")
            if raise_unknown:
                raise err
            raise err
        try:
            status = _container_status(docker, cid)
        except WriterStateUnknown:
            if raise_unknown:
                raise
            raise
        # created = never started → cancel; do not treat as busy forever.
        if status == "created":
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, finished_at = ?, exit_code = ?
                WHERE id = ?
                """,
                ("failed", now, -1, row["id"]),
            )
            conn.commit()
            continue
        if status in {"running", "paused"}:
            busy = True
            continue
        try:
            result = docker.run(
                ["docker", "wait", cid],
                timeout=DEFAULT_RUNTIME_POLICY.docker_command_timeout_seconds,
            )
            exit_code = _parse_wait_exit(getattr(result, "stdout", "") or "")
        except Exception as exc:
            err = WriterStateUnknown(f"wait failed for writer {cid}: {exc}")
            if raise_unknown:
                raise err from exc
            raise err from exc
        if exit_code is None:
            err = WriterStateUnknown(f"empty wait output for writer {cid}")
            if raise_unknown:
                raise err
            raise err
        log_path = None
        try:
            log_path = copy_job_logs(docker, cid, cfg=cfg)
        except Exception:
            log_path = None
        now = datetime.now(timezone.utc).isoformat()
        job_status = "ok" if exit_code == 0 else "failed"
        conn.execute(
            """
            UPDATE jobs
            SET exit_code = ?, status = ?, finished_at = ?, log_path = ?
            WHERE id = ?
            """,
            (exit_code, job_status, now, log_path, row["id"]),
        )
        if exit_code == 0 and str(row["stage"] or "") == "graph_full":
            set_graph_full_needed(conn, job_db, False)
        elif exit_code == 3 and job_stage_layer(str(row["stage"] or "")) == "graph":
            set_graph_full_needed(conn, job_db, True)
        conn.commit()
    return "busy" if busy else "idle"


def reap_database_jobs(
    conn: sqlite3.Connection, database: str, docker, *, cfg=None
) -> str:
    """Reap unfinished jobs for one database. Return idle|busy|unknown."""
    try:
        return _reap_unfinished_rows(
            conn, docker, database=database, raise_unknown=False, cfg=cfg
        )
    except WriterStateUnknown:
        return "unknown"


def reap_jobs(conn: sqlite3.Connection, docker, *, cfg=None) -> str:
    """Legacy UBD-only reap. Return idle|busy; raise WriterStateUnknown if unknown.

    Filters to ``database='ubd'`` so KB/OPL jobs in a shared SQLite do not block
    or mutate the legacy UBD path after a v2 pass.
    """
    return _reap_unfinished_rows(
        conn, docker, database="ubd", raise_unknown=True, cfg=cfg
    )


_PERMANENT_NAMES = frozenset(
    {"bsl-indexer", "mcp-metadata-graph", "open-webui", "omniroute"}
)


def cleanup_jobs(conn: sqlite3.Connection, docker, *, dry_run: bool) -> list[str]:
    rows = conn.execute(
        """
        SELECT id, container_id, status FROM jobs
        WHERE container_id IS NOT NULL AND container_id != ''
          AND status IN ('ok', 'failed')
        """
    ).fetchall()
    candidates: list[tuple[int, str]] = []
    for row in rows:
        cid = str(row["container_id"])
        name = cid
        try:
            named = docker.run(
                ["docker", "inspect", "-f", "{{.Name}} {{.State.Status}}", cid]
            )
            text = (named.stdout or "").strip()
        except Exception:
            continue
        parts = text.split()
        status = parts[-1] if parts else ""
        name = parts[0].lstrip("/") if parts else cid
        if name in _PERMANENT_NAMES:
            continue
        if status == "running":
            continue
        if status not in {"exited", "dead", "created"}:
            continue
        candidates.append((int(row["id"]), cid))
    ids = [cid for _, cid in candidates]
    if dry_run:
        return ids
    removed: list[str] = []
    for job_id, cid in candidates:
        status = _container_status(docker, cid)
        if status == "running":
            continue
        docker.run(["docker", "rm", cid])
        conn.execute(
            "UPDATE jobs SET status = 'removed' WHERE id = ?",
            (job_id,),
        )
        removed.append(cid)
    conn.commit()
    return removed


def _inspect_status(cfg, docker) -> str:
    result = docker.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{.State.Status}}",
            cfg.services.graph_mcp,
        ]
    )
    return result.stdout.strip()


def _network_names(cfg, docker) -> str:
    result = docker.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{range $k, $v := .NetworkSettings.Networks}}{{$k}}\n{{end}}",
            cfg.services.bsl_container,
        ]
    )
    return result.stdout


def _firewall_present(cfg, docker) -> bool:
    """Best-effort: True only if iptables reports DROP. Missing iptables → False."""
    port = str(cfg.services.bsl_publish_port)
    try:
        result = docker.run(
            ["iptables", "-C", "INPUT", "-p", "tcp", "--dport", port, "-j", "DROP"]
        )
        return result.returncode == 0
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False


def _verify_open(cfg, docker) -> bool:
    """Open iff graph running and BSL on WebUI network. Firewall not part of contract."""
    if _inspect_status(cfg, docker) != "running":
        return False
    if cfg.services.webui_network not in _network_names(cfg, docker):
        return False
    return True


def _recover_reads(cfg, conn: sqlite3.Connection, docker) -> None:
    requested = get_meta(conn, "reads_close_requested") == "1"
    if requested and not verify_closed(cfg, docker):
        apply_close(cfg, docker)
        if not verify_closed(cfg, docker):
            raise RuntimeError("could not finish close from persisted intent")
        wait_server_idle(cfg, docker)
        set_meta(conn, "reads_closed", "1")


def _all_ready(
    conn: sqlite3.Connection,
    cfg,
    docker,
    enabled: list[str],
    target: str,
    probes,
) -> bool:
    if probes is None:
        return False
    volume_by_name = {
        "code_index": cfg.storage.code_index_volume,
        "graph": cfg.storage.graph_volume,
    }
    container_by_name = {
        "code_index": cfg.services.bsl_container,
        "graph": cfg.services.graph_mcp,
    }
    for name in enabled:
        row = get_component(conn, name)
        if row is None:
            return False
        if row["status"] != "ready" or row["applied_commit"] != target:
            return False
        stored_storage = (row["storage_id"] or "").strip()
        stored_version = (row["indexer_version"] or "").strip()
        if not stored_storage or not stored_version:
            return False
        try:
            live_storage = dockerutil.volume_identity(
                volume_by_name[name], runner=docker
            )
            live_version = dockerutil.image_id(
                container_by_name[name], runner=docker
            )
        except Exception:
            return False
        if stored_storage != live_storage or stored_version != live_version:
            return False
        probe = getattr(probes, name, None)
        if probe is not None and getattr(probe, "status", "ready") != "ready":
            return False
    return True


def _indexes_consistent(conn: sqlite3.Connection, cfg, docker, enabled: list[str]) -> bool:
    commits: set[str] = set()
    volume_by_name = {
        "code_index": cfg.storage.code_index_volume,
        "graph": cfg.storage.graph_volume,
    }
    container_by_name = {
        "code_index": cfg.services.bsl_container,
        "graph": cfg.services.graph_mcp,
    }
    for name in enabled:
        row = get_component(conn, name)
        if row is None:
            return False
        if row["status"] != "ready":
            return False
        applied = (row["applied_commit"] or "").strip()
        storage = (row["storage_id"] or "").strip()
        version = (row["indexer_version"] or "").strip()
        if not applied or not storage or not version:
            return False
        try:
            live_storage = dockerutil.volume_identity(
                volume_by_name[name], runner=docker
            )
            live_version = dockerutil.image_id(
                container_by_name[name], runner=docker
            )
        except Exception:
            return False
        if storage != live_storage or version != live_version:
            return False
        commits.add(applied)
    return len(commits) == 1


def _component_storage_stale(conn: sqlite3.Connection, cfg, docker, name: str) -> bool:
    """True when stored volume/image identity does not match live (F01)."""
    row = get_component(conn, name)
    if row is None:
        return True
    volume_by_name = {
        "code_index": cfg.storage.code_index_volume,
        "graph": cfg.storage.graph_volume,
    }
    container_by_name = {
        "code_index": cfg.services.bsl_container,
        "graph": cfg.services.graph_mcp,
    }
    if name not in volume_by_name:
        return False
    stored_storage = (row["storage_id"] or "").strip()
    stored_version = (row["indexer_version"] or "").strip()
    if not stored_storage or not stored_version:
        return True
    try:
        live_storage = dockerutil.volume_identity(volume_by_name[name], runner=docker)
        live_version = dockerutil.image_id(container_by_name[name], runner=docker)
    except Exception:
        return True
    return stored_storage != live_storage or stored_version != live_version


def _needs_index(
    conn: sqlite3.Connection,
    name: str,
    target: str,
    enabled: list[str],
    *,
    cfg=None,
    docker=None,
) -> bool:
    if name not in enabled:
        return False
    row = get_component(conn, name)
    if row is None:
        return True
    if not (row["status"] == "ready" and row["applied_commit"] == target):
        return True
    # F01: same SHA but new/empty volume identity still needs rebuild.
    if cfg is not None and docker is not None:
        return _component_storage_stale(conn, cfg, docker, name)
    return False


def _mark_building(conn: sqlite3.Connection, name: str) -> None:
    """Mark component building; keep prior applied_commit for verify diffs.

    Clearing applied_commit here would force sample_current_tree (integrity_unknown)
    after every rebuild and break empty-repo / empty-diff success paths.
    storage_id / indexer_version are cleared so mid-build identity is not trusted.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO components (
          name, status, applied_commit, storage_id, indexer_version,
          container_id, exec_id, detail, updated_at
        ) VALUES (?, 'building', '', '', '', NULL, NULL, NULL, ?)
        ON CONFLICT(name) DO UPDATE SET
          status = 'building',
          storage_id = '',
          indexer_version = '',
          updated_at = excluded.updated_at
        """,
        (name, now),
    )
    conn.commit()


def _checkpoint(conn: sqlite3.Connection, cfg, docker, enabled: list[str], target: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    volume_by_name = {
        "code_index": cfg.storage.code_index_volume,
        "graph": cfg.storage.graph_volume,
    }
    container_by_name = {
        "code_index": cfg.services.bsl_container,
        "graph": cfg.services.graph_mcp,
    }
    for name in enabled:
        storage_id = dockerutil.volume_identity(volume_by_name[name], runner=docker)
        indexer_version = dockerutil.image_id(container_by_name[name], runner=docker)
        conn.execute(
            """
            INSERT INTO components (
              name, status, applied_commit, storage_id, indexer_version,
              container_id, exec_id, detail, updated_at
            ) VALUES (?, 'ready', ?, ?, ?, NULL, NULL, NULL, ?)
            ON CONFLICT(name) DO UPDATE SET
              status = 'ready',
              applied_commit = excluded.applied_commit,
              storage_id = excluded.storage_id,
              indexer_version = excluded.indexer_version,
              updated_at = excluded.updated_at
            """,
            (name, target, storage_id, indexer_version, now),
        )
    conn.commit()


def _clear_read_flags(conn: sqlite3.Connection) -> None:
    set_meta(conn, "reads_close_requested", "")
    set_meta(conn, "reads_closed", "")
    set_meta(conn, "reads_open_requested", "")


def _alert_if_reads_closed(cfg, conn: sqlite3.Connection, send) -> None:
    if get_meta(conn, "reads_closed") == "1":
        send(cfg, CLOSED_READS_ALERT)


def _finish(
    cfg,
    conn: sqlite3.Connection,
    run_id: int | None,
    target: str,
    result: str,
    message: str,
    send,
) -> str:
    _alert_if_reads_closed(cfg, conn, send)
    _finish_run(conn, run_id, target, result, message)
    return result


def _open_and_verify(cfg, conn: sqlite3.Connection, docker, toml_path: str, backup: str, reload_fn) -> None:
    set_meta(conn, "reads_open_requested", "1")
    open_reads(cfg, docker)
    if not _verify_open(cfg, docker):
        raise RuntimeError("verify open failed")
    if Path(toml_path).is_file() and watch_is_paused(
        lambda: Path(toml_path).read_text(encoding="utf-8")
    ):
        if Path(backup).is_file():
            restore_watch(toml_path, backup, reload_fn)
    _clear_read_flags(conn)


def reopen_reads(
    cfg,
    *,
    paths: RuntimePaths | None = None,
    db_path: str | None = None,
    lock_path: str | None = None,
    lock_backend=None,
    docker=None,
    watch_reload=None,
    alert_send=None,
    git=None,
    probes=None,
    database: str | None = None,
) -> str:
    """Operator reopen: open_reads + verify-open; clear flags only if verify succeeds."""
    runtime = resolve_runtime_paths(
        cfg, paths=paths, db_path=db_path, lock_path=lock_path
    )
    docker = docker if docker is not None else _Engine()
    reload_fn = watch_reload if watch_reload is not None else (lambda: None)
    send = alert_send if alert_send is not None else alert_mod.send

    if int(getattr(cfg, "schema_version", 1) or 1) >= 2:
        if database is None:
            database = _sole_enabled_database_name(cfg)
        if database is not None:
            return _reopen_reads_v2_database(
                cfg,
                database=database,
                runtime=runtime,
                lock_backend=lock_backend,
                docker=docker,
                watch_reload=reload_fn,
                send=send,
            )

    held = acquire(runtime.lock_path, backend=lock_backend)
    if held is None:
        return "deferred"

    conn = init_db(runtime.db_path)
    try:
        with held:
            toml_path = cfg.services.bsl_daemon_toml
            backup = toml_path + ".bak"
            try:
                writer = reap_jobs(conn, docker, cfg=cfg)
            except WriterStateUnknown as exc:
                send(cfg, f"reopen refused: writer state unknown: {exc}")
                _alert_if_reads_closed(cfg, conn, send)
                return "failed"
            if writer == "busy":
                send(cfg, "reopen refused: indexer still running")
                _alert_if_reads_closed(cfg, conn, send)
                return "failed"
            if get_meta(conn, "graph_full_needed") == "1":
                send(cfg, "reopen refused: graph FULL still needed")
                _alert_if_reads_closed(cfg, conn, send)
                return "failed"
            if not _indexes_consistent(conn, cfg, docker, _enabled(cfg)):
                send(cfg, "reopen refused: indexes mixed or not ready")
                _alert_if_reads_closed(cfg, conn, send)
                return "failed"
            try:
                active_git = git if git is not None else _GitutilOps()
                active_probes = probes
                if active_probes is None:
                    active_probes = build_probes(cfg, docker)
                head = active_git.rev_parse(cfg.repo.path, "HEAD")
                enabled = _enabled(cfg)
                for name in enabled:
                    row = get_component(conn, name)
                    if row is None or (row["applied_commit"] or "") != head:
                        send(cfg, "reopen refused: applied_commit != HEAD")
                        _alert_if_reads_closed(cfg, conn, send)
                        return "failed"
                _verify_components(
                    cfg, conn, active_git, active_probes, enabled, head
                )
                _open_and_verify(cfg, conn, docker, toml_path, backup, reload_fn)
            except Exception as exc:
                send(cfg, f"reopen failed: {exc}")
                _alert_if_reads_closed(cfg, conn, send)
                return "failed"
            return "success"
    finally:
        conn.close()


def _reopen_reads_v2_database(
    cfg,
    *,
    database: str,
    runtime: RuntimePaths,
    lock_backend,
    docker,
    watch_reload,
    send,
) -> str:
    """Reopen one v2 database using its own services/layers (not primary UBD)."""
    from contour.index_graph import is_graph_full_needed
    from contour.reads import DatabaseReadController

    try:
        selected = select_databases(cfg, database)
    except ValueError as exc:
        _safe_send(send, cfg, str(exc))
        return "failed"
    db = selected[0]
    held = acquire(runtime.lock_path, backend=lock_backend)
    if held is None:
        return "deferred"
    conn = init_db(runtime.db_path)
    try:
        with held:
            writer = reap_database_jobs(conn, db.name, docker, cfg=cfg)
            if writer == "busy":
                _safe_send(send, cfg, f"reopen refused: {db.name} indexer still running")
                return "failed"
            if writer == "unknown":
                _safe_send(send, cfg, f"reopen refused: {db.name} writer state unknown")
                return "failed"
            if is_graph_full_needed(conn, db.name):
                _safe_send(send, cfg, f"reopen refused: {db.name} graph FULL still needed")
                return "failed"
            all_profiles = list((cfg.databases or {}).values())
            block = writer_blocks_database(conn, db, all_profiles, docker, cfg=cfg)
            if block is not None:
                _safe_send(send, cfg, f"reopen refused: {block}")
                return "failed"
            for layer in db.required_layers:
                row = conn.execute(
                    """
                    SELECT status, applied_revision FROM layer_state
                    WHERE database = ? AND layer = ?
                    """,
                    (db.name, layer),
                ).fetchone()
                if row is None or str(row["status"] or "") != "ready":
                    _safe_send(
                        send,
                        cfg,
                        f"reopen refused: {db.name} layer {layer} not ready",
                    )
                    return "failed"
            ctrl = DatabaseReadController(
                db, conn, docker, watch_reload=watch_reload
            )
            try:
                ctrl.open()
            except Exception as exc:
                _safe_send(send, cfg, f"reopen failed for {db.name}: {exc}")
                return "failed"
            return "success"
    finally:
        conn.close()


def _git_diff_runner(git):
    if hasattr(git, "diff_name_only") and hasattr(git, "diff_deletions"):
        return git
    return None


def _watch_is_paused_file(path: str) -> bool:
    return watch_is_paused(lambda: Path(path).read_text(encoding="utf-8"))


def _pause_watch_preserving_backup(toml_path: str, backup: str, reload_fn) -> None:
    if not Path(toml_path).is_file():
        raise RuntimeError(f"daemon.toml missing; cannot pause watcher: {toml_path}")
    already_paused = _watch_is_paused_file(toml_path)
    bak_has_pre_pause = Path(backup).is_file() and not _watch_is_paused_file(backup)
    if not already_paused and not bak_has_pre_pause:
        shutil.copy2(toml_path, backup)
    if not already_paused:
        pause_watch(toml_path, reload_fn)
    else:
        # File already paused — still reload so active daemon matches disk (F03).
        reload_fn()


def _verify_components(cfg, conn, git, probes, enabled: list[str], target: str) -> None:
    if probes is None:
        raise RuntimeError("index probes required")
    git_diff = _git_diff_runner(git)
    sub = getattr(cfg.repo, "workspace_subdir", "") or ""
    for name in enabled:
        row = get_component(conn, name)
        old = (row["applied_commit"] if row is not None else "") or ""
        if not old:
            samples = sample_current_tree(cfg.repo.path, workspace_subdir=sub)
        else:
            samples = sample_paths(
                cfg.repo.path, old, target, git=git_diff, workspace_subdir=sub
            )
            # Empty commit-diff (same SHA / reopen): still smoke-check tree content.
            if samples.is_empty() and not samples.integrity_unknown:
                smoke = sample_current_tree(
                    cfg.repo.path, workspace_subdir=sub, limit=8
                )
                samples = Samples(
                    bsl_paths=smoke.bsl_paths,
                    xml_paths=smoke.xml_paths,
                    deletions=[],
                    integrity_unknown=False,
                )
        verify_indexes(samples, probes)


def reconcile(
    cfg,
    *,
    timer: bool,
    measure: bool,
    now: datetime | None = None,
    paths: RuntimePaths | None = None,
    db_path: str | None = None,
    lock_path: str | None = None,
    lock_backend=None,
    docker=None,
    git=None,
    alert_send=None,
    watch_reload=None,
    probes=None,
    database: str | None = None,
    database_runner: DatabaseRunner | None = None,
) -> str:
    if timer:
        measure = False
    runtime = resolve_runtime_paths(
        cfg, paths=paths, db_path=db_path, lock_path=lock_path
    )
    git = git if git is not None else _GitutilOps()
    docker = docker if docker is not None else _Engine()
    send = alert_send if alert_send is not None else alert_mod.send
    reload_fn = watch_reload if watch_reload is not None else (lambda: None)
    frozen = now
    clock = (lambda: frozen) if frozen is not None else (lambda: datetime.now(timezone.utc))

    held = acquire(runtime.lock_path, backend=lock_backend)
    if held is None:
        return "deferred"

    # Always enter the lock context so init_db failures still release it.
    with held:
        try:
            conn = init_db(runtime.db_path)
        except Exception:
            raise
        try:
            if int(getattr(cfg, "schema_version", 1) or 1) >= 2:
                return _reconcile_v2_locked(
                    cfg,
                    conn,
                    timer=timer,
                    measure=measure,
                    clock=clock,
                    docker=docker,
                    git=git,
                    send=send,
                    reload_fn=reload_fn,
                    probes=probes,
                    database=database,
                    database_runner=database_runner,
                )
            if database is not None:
                send(cfg, f"database filter requires schema_version 2: {database}")
                return "failed"
            return _reconcile_locked(
                cfg,
                conn,
                timer=timer,
                measure=measure,
                clock=clock,
                docker=docker,
                git=git,
                send=send,
                reload_fn=reload_fn,
                probes=probes,
            )
        finally:
            conn.close()


def _reconcile_v2_locked(
    cfg,
    conn: sqlite3.Connection,
    *,
    timer: bool,
    measure: bool,
    clock,
    docker,
    git,
    send,
    reload_fn,
    probes,
    database: str | None,
    database_runner: DatabaseRunner | None,
) -> str:
    del git, probes  # optional hooks for specialized runners
    state = StateStore(conn)
    try:
        selected = select_databases(cfg, database)
    except ValueError as exc:
        _safe_send(send, cfg, str(exc))
        return "failed"

    try:
        dockerutil.ping(
            runner=docker,
            timeout=DEFAULT_RUNTIME_POLICY.docker_command_timeout_seconds,
        )
    except Exception as exc:
        _safe_send(send, cfg, f"docker ping hung or failed: {exc}")
        return "failed"

    runner = database_runner or _default_v2_database_runner
    outcomes: list[tuple[str, str]] = []
    all_profiles = list((cfg.databases or {}).values())

    def _fresh_may_write() -> str:
        return may_write(
            now=clock(), cfg=cfg, is_timer=timer, measure=measure
        )

    # Reap unfinished jobs for every selected database before the write gate so
    # completed Docker work is recorded even outside the window.
    for db in selected:
        state.register_database(db.name, _source_kind(db))
        try:
            reap_database_jobs(conn, db.name, docker, cfg=cfg)
        except Exception as exc:
            _safe_send(send, cfg, f"{db.name}: reap failed: {exc}")

    for db in selected:
        window = _fresh_may_write()
        if window != "ok":
            message = f"{db.name}: write window closed ({window})"
            try:
                run_id = state.start_run(db.name, "(pending)")
                state.finish_run(run_id, "deferred", message)
            except Exception:
                pass
            _safe_send(send, cfg, message)
            outcomes.append((db.name, "deferred"))
            continue
        block = writer_blocks_database(conn, db, all_profiles, docker, cfg=cfg)
        if block is not None:
            try:
                run_id = state.start_run(db.name, "(pending)")
                state.finish_run(run_id, "deferred", block)
            except Exception:
                pass
            _safe_send(send, cfg, block)
            outcomes.append((db.name, "deferred"))
            continue
        try:
            db_docker = _docker_for_database(db, docker)
            # Always profile-scoped; never fall back to another database's reload.
            db_reload = _watch_reload_for_database(db, reload_fn)
            status = runner(
                db,
                cfg=cfg,
                conn=conn,
                state=state,
                docker=db_docker,
                send=send,
                may_write=_fresh_may_write,
                timer=timer,
                measure=measure,
                clock=clock,
                watch_reload=db_reload,
            )
            status = _normalize_db_result(str(status))
        except WriterStillAlive as exc:
            message = str(exc) or "writer still alive"
            try:
                run_id = state.start_run(db.name, "(pending)")
                state.finish_run(run_id, "deferred", message)
            except Exception:
                pass
            _safe_send(send, cfg, f"{db.name}: {message}")
            status = "deferred"
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            try:
                run_id = state.start_run(db.name, "(pending)")
                state.finish_run(run_id, "failed", message)
            except Exception:
                pass
            _safe_send(send, cfg, f"{db.name}: {message}")
            status = "failed"
        outcomes.append((db.name, status))

    aggregate = aggregate_database_results(outcomes)
    if aggregate != "success":
        _safe_send(send, cfg, format_nightly_alert(conn, outcomes))
    return aggregate


def _maybe_duration_warn(cfg, conn: sqlite3.Connection, started: float, send) -> None:
    warn = duration_warn_seconds(cfg)
    if warn is None:
        return
    if (time.monotonic() - started) < warn:
        return
    if get_meta(conn, "duration_warned") == "1":
        return
    send(
        cfg,
        f"duration_warn_seconds exceeded ({warn}s); indexer continues, not killed",
    )
    set_meta(conn, "duration_warned", "1")


def _reconcile_locked(
    cfg,
    conn: sqlite3.Connection,
    *,
    timer: bool,
    measure: bool,
    clock,
    docker,
    git,
    send,
    reload_fn,
    probes,
) -> str:
    run_id: int | None = None
    target = ""
    try:
        dockerutil.ping(runner=docker)
    except Exception as exc:
        send(cfg, f"docker ping hung or failed: {exc}")
        return _finish(cfg, conn, run_id, target, "failed", str(exc), send)

    try:
        writer = reap_jobs(conn, docker, cfg=cfg)
    except WriterStateUnknown as exc:
        send(cfg, f"previous writer state unknown; not starting a new writer: {exc}")
        return _finish(cfg, conn, run_id, target, "failed", "writer unknown", send)
    if writer == "busy":
        send(cfg, "previous indexer still running; not starting a new writer")
        return _finish(cfg, conn, run_id, target, "deferred", "writer busy", send)
    if get_meta(conn, "graph_full_needed") == "1":
        send(cfg, "reaped graph job exit 3; FULL needed on next in-window write")

    try:
        if probes is None:
            probes = build_probes(cfg, docker)
        require_idle_adapter(docker)
    except MissingAdapters as exc:
        send(cfg, f"adapters missing; refusing close/write: {exc}")
        return _finish(cfg, conn, run_id, target, "failed", "adapters", send)

    try:
        _recover_reads(cfg, conn, docker)
    except Exception as exc:
        send(cfg, f"reads recovery failed: {exc}")
        return _finish(cfg, conn, run_id, target, "failed", str(exc), send)

    repo = cfg.repo.path
    try:
        git.fetch(repo)
        if git.is_dirty(repo):
            send(cfg, "dirty working tree; refusing new close/write")
            return _finish(cfg, conn, run_id, target, "failed", "dirty", send)
        target = git.rev_parse(repo, f"origin/{cfg.repo.branch}")
        head = git.rev_parse(repo, "HEAD")
    except Exception as exc:
        send(cfg, f"git fetch/rev-parse failed: {exc}")
        return _finish(cfg, conn, run_id, target, "failed", str(exc), send)

    run_id = _start_run(conn, target)
    enabled = _enabled(cfg)
    toml_path = cfg.services.bsl_daemon_toml
    backup = toml_path + ".bak"
    full_needed = get_meta(conn, "graph_full_needed") == "1"

    if (
        head == target
        and _all_ready(conn, cfg, docker, enabled, target, probes)
        and not full_needed
    ):
        try:
            if verify_closed(cfg, docker) or get_meta(conn, "reads_closed") == "1":
                _open_and_verify(cfg, conn, docker, toml_path, backup, reload_fn)
        except Exception as exc:
            send(cfg, f"ready-at-target open failed: {exc}")
            return _finish(cfg, conn, run_id, target, "failed", str(exc), send)
        return _finish(cfg, conn, run_id, target, "success", "", send)

    now = clock()
    decision = may_write(now=now, cfg=cfg, is_timer=timer, measure=measure)
    if decision == "deferred_window":
        return _finish(cfg, conn, run_id, target, "deferred", "outside window", send)

    request_close(conn)
    apply_close(cfg, docker)
    if not verify_closed(cfg, docker):
        send(cfg, "reads close verify failed")
        return _finish(cfg, conn, run_id, target, "failed", "close verify", send)
    try:
        wait_server_idle(cfg, docker)
    except Exception as exc:
        send(cfg, f"wait idle failed: {exc}")
        return _finish(cfg, conn, run_id, target, "failed", str(exc), send)
    set_meta(conn, "reads_closed", "1")

    try:
        _pause_watch_preserving_backup(toml_path, backup, reload_fn)
    except Exception as exc:
        send(cfg, f"watch pause/reload failed: {exc}")
        return _finish(cfg, conn, run_id, target, "failed", str(exc), send)
    try:
        wait_server_idle(cfg, docker)
    except Exception as exc:
        send(cfg, f"wait idle after watch pause failed: {exc}")
        return _finish(cfg, conn, run_id, target, "failed", str(exc), send)

    started = time.monotonic()
    set_meta(conn, "duration_warned", "")
    observe_cap = _runtime_observe_cap(cfg)

    try:
        head = git.ff_only(repo, cfg.repo.branch)

        if _needs_index(conn, "code_index", head, enabled, cfg=cfg, docker=docker):
            now = clock()
            if may_write(now=now, cfg=cfg, is_timer=timer, measure=measure) != "ok":
                send(cfg, "window closed before BSL index; not starting stage")
                return _finish(
                    cfg, conn, run_id, target, "failed", "window closed", send
                )
            _maybe_duration_warn(cfg, conn, started, send)
            _mark_building(conn, "code_index")
            index_bsl(cfg, conn, run_id, runner=docker, observe_cap=observe_cap)
            _maybe_duration_warn(cfg, conn, started, send)

        # Keep watcher paused through graph + verify (F03). Restore in _open_and_verify.
        need_graph = (
            _needs_index(conn, "graph", head, enabled, cfg=cfg, docker=docker)
            or full_needed
        )
        if need_graph:
            now = clock()
            write_now = may_write(now=now, cfg=cfg, is_timer=timer, measure=measure)
            if write_now != "ok":
                send(cfg, "window closed before graph index; not starting stage")
                return _finish(
                    cfg, conn, run_id, target, "failed", "window closed", send
                )
            remaining = remaining_write_seconds(
                now=now,
                cfg=cfg,
                measure=measure,
                elapsed_monotonic=time.monotonic() - started,
            )
            _maybe_duration_warn(cfg, conn, started, send)
            _mark_building(conn, "graph")

            def _fresh_may_write() -> str:
                return may_write(
                    now=clock(), cfg=cfg, is_timer=timer, measure=measure
                )

            index_graph(
                cfg,
                conn,
                run_id,
                runner=docker,
                may_write_decision=write_now,
                may_write_fn=_fresh_may_write,
                remaining_budget=remaining,
                measure=measure,
                full_needed=full_needed,
                observe_cap=observe_cap,
            )
            _maybe_duration_warn(cfg, conn, started, send)

        _verify_components(cfg, conn, git, probes, enabled, head)
        _checkpoint(conn, cfg, docker, enabled, head)
        _open_and_verify(cfg, conn, docker, toml_path, backup, reload_fn)
        return _finish(cfg, conn, run_id, target, "success", "", send)
    except Exception as exc:
        send(cfg, f"reconcile failed: {exc}")
        return _finish(cfg, conn, run_id, target, "failed", str(exc), send)
