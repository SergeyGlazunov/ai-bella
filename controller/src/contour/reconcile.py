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
from contour.config import DatabaseConfig, GitSourceConfig, enabled_databases
from contour.db import get_component, get_meta, init_db, set_meta
from contour.index_bsl import index_bsl
from contour.index_graph import copy_job_logs, index_graph
from contour.lock import acquire
from contour.reads import (
    apply_close,
    open_reads,
    request_close,
    verify_closed,
    wait_server_idle,
)
from contour.schedule import duration_warn_seconds, may_write, remaining_write_seconds
from contour.state import StateStore
from contour.verify import (
    Samples,
    SubprocessGitDiffRunner,
    sample_current_tree,
    sample_paths,
    verify_indexes,
)
from contour.watch_bsl import pause_watch, restore_watch, watch_is_paused

DEFAULT_DB = "/srv/ai/state.db"
DEFAULT_LOCK = "/srv/ai/sync.lock"

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


def _default_v2_database_runner(
    database: DatabaseConfig,
    *,
    cfg,
    conn: sqlite3.Connection,
    state: StateStore,
    **kwargs,
) -> str:
    del cfg, conn, state, kwargs
    raise RuntimeError(
        f"v2 database runner is not wired for {database.name}; "
        "Task 8 later blocks provide the production runner"
    )


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
            ["docker", "inspect", "-f", "{{.State.Status}}", container_id]
        )
    except Exception as exc:
        raise WriterStateUnknown(
            f"cannot inspect writer {container_id}: {exc}"
        ) from exc
    status = (getattr(result, "stdout", "") or "").strip()
    if not status:
        raise WriterStateUnknown(f"empty inspect for writer {container_id}")
    return status


def reap_jobs(conn: sqlite3.Connection, docker) -> str:
    """Return 'idle' or 'busy'. Raise WriterStateUnknown if a job cannot be classified."""
    rows = conn.execute(
        """
        SELECT id, container_id, exec_id, status FROM jobs
        WHERE status NOT IN ('ok', 'failed', 'removed')
        """
    ).fetchall()
    busy = False
    for row in rows:
        cid = row["container_id"] or ""
        exec_id = row["exec_id"] or ""
        if exec_id:
            if not cid:
                raise WriterStateUnknown(f"job {row['id']} has exec_id but no container")
            try:
                running = dockerutil.list_exec_ids(cid, runner=docker)
            except Exception as exc:
                raise WriterStateUnknown(
                    f"cannot list exec ids for {cid}: {exc}"
                ) from exc
            if exec_id in running:
                busy = True
                continue
            # Never started (crash between INSERT and start_exec) — cancel, not busy forever.
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
            except Exception as exc:
                raise WriterStateUnknown(
                    f"wait_exec failed for {exec_id}: {exc}"
                ) from exc
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
            conn.commit()
            continue
        if not cid:
            raise WriterStateUnknown(f"job {row['id']} has no container_id")
        status = _container_status(docker, cid)
        # created = never started → cancel; do not treat as busy forever (R04 edge).
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
            result = docker.run(["docker", "wait", cid])
            exit_code = _parse_wait_exit(getattr(result, "stdout", "") or "")
        except Exception as exc:
            raise WriterStateUnknown(
                f"wait failed for writer {cid}: {exc}"
            ) from exc
        if exit_code is None:
            raise WriterStateUnknown(f"empty wait output for writer {cid}")
        log_path = None
        try:
            log_path = copy_job_logs(docker, cid)
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
        if exit_code == 3:
            set_meta(conn, "graph_full_needed", "1")
        conn.commit()
    return "busy" if busy else "idle"


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
) -> str:
    """Operator reopen: open_reads + verify-open; clear flags only if verify succeeds."""
    runtime = paths or RuntimePaths(
        db_path=db_path or DEFAULT_DB,
        lock_path=lock_path or DEFAULT_LOCK,
    )
    docker = docker if docker is not None else _Engine()
    reload_fn = watch_reload if watch_reload is not None else (lambda: None)
    send = alert_send if alert_send is not None else alert_mod.send

    held = acquire(runtime.lock_path, backend=lock_backend)
    if held is None:
        return "deferred"

    conn = init_db(runtime.db_path)
    try:
        with held:
            toml_path = cfg.services.bsl_daemon_toml
            backup = toml_path + ".bak"
            try:
                writer = reap_jobs(conn, docker)
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
    del timer, measure, clock, git, reload_fn, probes  # wired in later Task 8 blocks
    state = StateStore(conn)
    try:
        selected = select_databases(cfg, database)
    except ValueError as exc:
        send(cfg, str(exc))
        return "failed"

    try:
        dockerutil.ping(runner=docker)
    except Exception as exc:
        send(cfg, f"docker ping hung or failed: {exc}")
        return "failed"

    runner = database_runner or _default_v2_database_runner
    outcomes: list[tuple[str, str]] = []
    for db in selected:
        state.register_database(db.name, _source_kind(db))
        try:
            status = runner(
                db,
                cfg=cfg,
                conn=conn,
                state=state,
                docker=docker,
                send=send,
            )
            status = _normalize_db_result(str(status))
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            send(cfg, f"{db.name}: {message}")
            # External loop owns the attempt record when the runner did not.
            try:
                run_id = state.start_run(db.name, "(pending)")
                state.finish_run(run_id, "failed", message)
            except Exception:
                pass
            status = "failed"
        outcomes.append((db.name, status))

    return aggregate_database_results(outcomes)


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
        writer = reap_jobs(conn, docker)
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
            index_bsl(cfg, conn, run_id, runner=docker)
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
            )
            _maybe_duration_warn(cfg, conn, started, send)

        _verify_components(cfg, conn, git, probes, enabled, head)
        _checkpoint(conn, cfg, docker, enabled, head)
        _open_and_verify(cfg, conn, docker, toml_path, backup, reload_fn)
        return _finish(cfg, conn, run_id, target, "success", "", send)
    except Exception as exc:
        send(cfg, f"reconcile failed: {exc}")
        return _finish(cfg, conn, run_id, target, "failed", str(exc), send)
