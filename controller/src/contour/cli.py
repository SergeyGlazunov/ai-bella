from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from contour.adapters import CommandEngine, MissingAdapters, build_probes
from contour.alert import command_is_runnable
from contour.config import (
    GitSourceConfig,
    PackageSourceConfig,
    enabled_databases,
    load_config,
)
from contour.db import get_component, get_meta, init_db
from contour.dockerutil import SubprocessRunner
from contour.gitutil import safe_remote
from contour.lock import acquire
from contour.runtime_policy import DEFAULT_RUNTIME_POLICY
from contour.reconcile import (
    DEFAULT_DB,
    DEFAULT_LOCK,
    RuntimePaths,
    cleanup_jobs,
    protected_container_names,
    reconcile,
    reopen_reads,
    resolve_runtime_paths,
    select_databases,
)
from contour.watch_bsl import reload_bsl_daemon

DEFAULT_CONFIG = "/etc/ai-contour/contour.yaml"
STATE_BACKUP_RETAIN_DAYS = 14


def _docker():
    return SubprocessRunner()


def _graph_actual_status(cfg) -> str:
    """Cheap inspect of graph MCP vs reads_closed flag. Never raises."""
    try:
        result = _docker().run(
            [
                "docker",
                "inspect",
                "-f",
                "{{.State.Status}}",
                cfg.services.graph_mcp,
            ]
        )
        return (result.stdout or "").strip() or "unknown"
    except Exception:
        return "unavailable"


def _job_duration(started_at: str | None) -> str:
    if not started_at:
        return ""
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return ""
    now = datetime.now(timezone.utc)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    seconds = max(0, int((now - started).total_seconds()))
    return f" duration={seconds}s"


def _source_kind(database) -> str:
    if isinstance(database.source, PackageSourceConfig):
        return "package"
    if isinstance(database.source, GitSourceConfig):
        return "git"
    return "unknown"


def _print_database_status(conn: sqlite3.Connection, cfg, db_name: str) -> None:
    db = (cfg.databases or {}).get(db_name)
    if db is None:
        print(f"database: {db_name} (not in config)")
        return
    kind = _source_kind(db)
    print(f"database: {db_name}")
    print(f"  enabled: {str(bool(db.enabled)).lower()}")
    print(f"  source: {kind}")
    if isinstance(db.source, GitSourceConfig):
        print(f"  git_path: {db.source.path}")
        print(f"  git_remote: {safe_remote(db.source.remote)}")
        print(f"  git_branch: {db.source.branch}")
    elif isinstance(db.source, PackageSourceConfig):
        print(f"  inbox: {db.source.inbox}")
        print(f"  source_path: {db.source.source_path}")
    print(f"  required_layers: {','.join(sorted(db.required_layers))}")
    row = conn.execute(
        """
        SELECT lifecycle_status, published_revision, staged_revision, detail
        FROM database_state WHERE database = ?
        """,
        (db_name,),
    ).fetchone()
    if row is None:
        print("  state: (none)")
    else:
        print(
            f"  lifecycle: {row['lifecycle_status']} "
            f"published={row['published_revision'] or '-'} "
            f"staged={row['staged_revision'] or '-'} "
            f"detail={row['detail'] or '-'}"
        )
    layers = conn.execute(
        """
        SELECT layer, status, applied_revision, storage_id, indexer_version
        FROM layer_state WHERE database = ? ORDER BY layer
        """,
        (db_name,),
    ).fetchall()
    if not layers:
        print("  layers: (none)")
        return
    for layer in layers:
        print(
            f"  {layer['layer']}: status={layer['status']} "
            f"rev={layer['applied_revision'] or '-'} "
            f"storage={layer['storage_id'] or '-'} "
            f"indexer={layer['indexer_version'] or '-'}"
        )


def _print_status(cfg, paths: RuntimePaths, *, database: str | None = None) -> int:
    if int(getattr(cfg, "schema_version", 1) or 1) < 2 or database is None:
        # Legacy / full status: never print remote userinfo.
        print(f"remote: {safe_remote(cfg.repo.remote)}")
        print(f"branch: {cfg.repo.branch}")
        print(f"repo: {cfg.repo.path}")
    conn = init_db(paths.db_path)
    try:
        if database is not None:
            databases = cfg.databases or {}
            if database not in databases:
                print(f"unknown database: {database}")
                return 1
            # status may inspect enabled:false profiles (repair); update still refuses.
            _print_database_status(conn, cfg, database)
            return 0

        close_req = get_meta(conn, "reads_close_requested")
        closed = get_meta(conn, "reads_closed")
        print(f"reads_close_requested: {close_req}")
        print(f"reads_closed: {closed}")
        actual = _graph_actual_status(cfg)
        print(f"graph_mcp_actual: {actual}")
        print(
            f"reads_closed_flag_vs_actual: flag={closed or '(empty)'} "
            f"graph={actual}"
        )
        last = conn.execute(
            "SELECT target_commit, result, message FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last is not None:
            print(
                f"last_run: target={last['target_commit']} "
                f"result={last['result']} message={last['message']}"
            )
        reap_note = get_meta(conn, "last_exec_record_gone")
        if reap_note:
            print(f"last_exec_record_gone: {reap_note}")
        delivery_note = get_meta(conn, "alert_delivery_last_error")
        if delivery_note:
            print(f"alert_delivery_last_error: {delivery_note}")
        jobs = conn.execute(
            """
            SELECT id, stage, status, container_id, exec_id, started_at, finished_at
            FROM jobs ORDER BY id
            """
        ).fetchall()
        for job in jobs:
            duration = ""
            if job["status"] not in {"ok", "failed", "removed"}:
                duration = _job_duration(job["started_at"])
            print(
                f"job {job['id']}: stage={job['stage']} "
                f"status={job['status']} container_id={job['container_id']} "
                f"exec_id={job['exec_id'] or ''} started_at={job['started_at'] or ''}"
                f"{duration}"
            )
        for name in ("code_index", "graph", "source"):
            row = get_component(conn, name)
            if row is None:
                continue
            print(
                f"{name}: status={row['status']} "
                f"applied_commit={row['applied_commit']} "
                f"storage_id={row['storage_id']} "
                f"indexer_version={row['indexer_version']}"
            )
        if int(getattr(cfg, "schema_version", 1) or 1) >= 2:
            print("databases:")
            for db_name in (cfg.databases or {}):
                _print_database_status(conn, cfg, db_name)
    finally:
        conn.close()
    return 0


def _validate(cfg) -> int:
    if not command_is_runnable(cfg.alert.command[0]):
        return 1
    alert_path = Path(cfg.alert.command[0])
    if alert_path.is_file():
        try:
            text = alert_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if "alert.sh.example is a stub" in text or "NOT production-ready" in text:
            return 1

    if int(getattr(cfg, "schema_version", 1) or 1) >= 2:
        databases = cfg.databases or {}
        if not databases:
            print("validate failed: no databases in schema_version 2 config")
            return 1
        for name, db in databases.items():
            kind = _source_kind(db)
            print(
                f"database: {name} enabled={str(bool(db.enabled)).lower()} "
                f"source={kind} layers={','.join(sorted(db.required_layers))}"
            )
            if not db.enabled:
                continue
            try:
                build_probes(
                    db,
                    _docker(),
                    required_layers=frozenset(db.required_layers),
                )
                if not db.adapters.idle_probe:
                    raise MissingAdapters(
                        f"database {name}: adapters.idle_probe is required"
                    )
            except MissingAdapters as exc:
                print(str(exc))
                return 1
        return 0

    try:
        build_probes(cfg, _docker())
        if cfg.adapters is None or not cfg.adapters.idle_probe:
            raise MissingAdapters("adapters.idle_probe is required")
    except MissingAdapters:
        return 1
    return 0


def _intake(cfg, database: str) -> int:
    """Validate and claim the newest inbox package; do not index."""
    from contour.packages import (
        PackageError,
        choose_newest_valid_package,
        claim_package,
        validate_package,
    )

    try:
        selected = select_databases(cfg, database)
    except ValueError as exc:
        print(str(exc))
        return 1
    db = selected[0]
    if not isinstance(db.source, PackageSourceConfig):
        print(f"intake requires a package source database, got {_source_kind(db)}")
        return 1
    if cfg.runtime is None or not cfg.runtime.root:
        print("intake requires runtime.root for accepted packages")
        return 1

    chosen, warnings = choose_newest_valid_package(
        Path(db.source.inbox), db.name
    )
    for item in warnings:
        print(f"warning: {item}")
    if chosen is None:
        print(f"no valid package in inbox for {db.name}")
        return 1

    accepted_root = Path(cfg.runtime.root) / "accepted" / db.name
    try:
        claimed = claim_package(chosen, accepted_root)
        package = validate_package(claimed, frozenset({db.name}))
    except (PackageError, OSError) as exc:
        print(f"intake failed: {exc}")
        return 1

    print(
        f"intake ok: database={db.name} revision={package.revision} "
        f"path={claimed}"
    )
    print("note: indexing runs only via contour update")
    return 0


def _backup_state(cfg, paths: RuntimePaths, *, retain_days: int = STATE_BACKUP_RETAIN_DAYS) -> int:
    """SQLite online backup into runtime.backups_path; prune older copies.

    Missing source state.db is an error: do not create an empty DB and do not prune.
    """
    runtime = getattr(cfg, "runtime", None)
    if runtime is not None and runtime.backups_path:
        backup_dir = Path(runtime.backups_path)
    else:
        backup_dir = Path("/srv/ai-contour/backups/state")

    src_path = Path(paths.db_path)
    if not src_path.is_file():
        print(f"backup-state failed: state database missing: {src_path}")
        return 1

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = backup_dir / f"state-{stamp}.db"

    # Open source read-only via URI so a missing path cannot create a new DB.
    try:
        src = sqlite3.connect(src_path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"backup-state failed: cannot open {src_path}: {exc}")
        return 1
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    except sqlite3.Error as exc:
        dest.unlink(missing_ok=True)
        print(f"backup-state failed: {exc}")
        return 1
    finally:
        src.close()

    print(f"backup-state ok: {dest}")

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, retain_days))
    for path in backup_dir.glob("state-*.db"):
        if path.resolve() == dest.resolve():
            continue
        try:
            # state-YYYYMMDDTHHMMSSZ.db
            token = path.stem.removeprefix("state-")
            stamped = datetime.strptime(token, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if stamped < cutoff:
            path.unlink(missing_ok=True)
            print(f"backup-state pruned: {path.name}")
    return 0


def _bsl_watch_reload(cfg) -> None:
    """Reload daemon after daemon.toml pause/restore (R03/F03). Require reloaded=true."""
    reload_bsl_daemon(cfg)


def _writer_gone(row, docker) -> str | None:
    """Reason the job must stay unfinished, or None when the writer is gone.

    The orchestrator lock proves no *orchestrator* is writing; it says nothing
    about a detached Docker writer left behind by a crash or a deferred
    observation. Check the recorded writer before releasing job accounting.
    """
    from contour import dockerutil
    from contour.dockerutil import ExecRecordGone

    exec_id = str(row["exec_id"] or "")
    container_id = str(row["container_id"] or "")
    if exec_id:
        try:
            payload = dockerutil.exec_state(exec_id, runner=docker)
        except ExecRecordGone:
            # Record collected long after exit; the writer is finished.
            return None
        except Exception as exc:
            return f"writer state unknown: {exc}"
        running = payload.get("Running")
        if running is True or running in (1, "true", "True"):
            return f"exec {exec_id} is still running"
        return None
    if container_id:
        try:
            result = docker.run(
                [
                    "docker",
                    "inspect",
                    "-f",
                    "{{.State.Status}}",
                    container_id,
                ],
                timeout=DEFAULT_RUNTIME_POLICY.docker_command_timeout_seconds,
            )
        except Exception as exc:
            return f"writer state unknown: {exc}"
        status = (getattr(result, "stdout", "") or "").strip()
        if status in ("running", "paused", "restarting"):
            return f"container {container_id} is {status}"
        if status in ("exited", "dead", "created", "removing"):
            return None
        return f"container {container_id} state {status!r} unrecognized; refusing"
    # No recorded writer identity — nothing that can still be writing.
    return None


def main(argv: list[str] | None = None) -> int:
    raw = list(argv) if argv is not None else None
    # R01: pull --config/--db/--lock from anywhere so both
    # `contour --config X validate` and `contour validate --config X` work.
    global_parser = argparse.ArgumentParser(add_help=False)
    global_parser.add_argument("--config", default=DEFAULT_CONFIG)
    # None defaults so v2 cfg.runtime is not silently overridden by legacy paths.
    global_parser.add_argument("--db", default=None)
    global_parser.add_argument("--lock", default=None)
    global_args, rest = global_parser.parse_known_args(raw)

    parser = argparse.ArgumentParser(prog="contour")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate")
    status = sub.add_parser("status")
    status.add_argument(
        "--database",
        default=None,
        help="show one v2 database profile (source + layer revisions)",
    )
    update = sub.add_parser("update")
    update.add_argument(
        "--measure",
        action="store_true",
        help="optional timing log; does not kill jobs and is not the sole write path",
    )
    update.add_argument(
        "--database",
        default=None,
        help="run a single database profile (schema_version 2)",
    )
    intake = sub.add_parser(
        "intake",
        help="validate and claim the newest inbox package without indexing",
    )
    intake.add_argument(
        "--database",
        required=True,
        help="package-source database to intake",
    )
    backup = sub.add_parser(
        "backup-state",
        help="SQLite online backup of state.db into runtime.backups_path",
    )
    backup.add_argument(
        "--retain-days",
        type=int,
        default=STATE_BACKUP_RETAIN_DAYS,
        help=f"delete backups older than N days (default {STATE_BACKUP_RETAIN_DAYS})",
    )
    open_parser = sub.add_parser(
        "open",
        help="reopen MCP reads only if indexes are consistent and no writer is running",
    )
    open_parser.add_argument(
        "--database",
        default=None,
        help="reopen one v2 database; required when multiple profiles are enabled",
    )
    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument("--dry-run", action="store_true")

    jobs_parser = sub.add_parser(
        "jobs",
        help="operator job maintenance",
    )
    jobs_sub = jobs_parser.add_subparsers(dest="jobs_command", required=True)
    jobs_fail = jobs_sub.add_parser(
        "fail",
        help=(
            "mark an unfinished job failed; refuses while its writer is "
            "running or its state is unknown"
        ),
    )
    jobs_fail.add_argument("job_id", type=int)
    jobs_fail.add_argument(
        "--force",
        action="store_true",
        help=(
            "override the writer check for a job you have verified yourself "
            "(e.g. the Docker record is gone); requires --reason"
        ),
    )
    jobs_fail.add_argument(
        "--reason",
        default="",
        help="why the writer check is being overridden (recorded in state)",
    )

    args = parser.parse_args(rest)
    args.config = global_args.config
    args.db = global_args.db
    args.lock = global_args.lock

    if args.command == "validate":
        try:
            cfg = load_config(args.config)
        except Exception as exc:
            print(f"config error: {args.config}: {exc}", file=sys.stderr)
            return 1
        return _validate(cfg)

    try:
        cfg = load_config(args.config)
    except Exception as exc:
        print(f"config error: {args.config}: {exc}", file=sys.stderr)
        return 1

    paths = resolve_runtime_paths(
        cfg,
        db_path=args.db,
        lock_path=args.lock,
    )

    if args.command == "status":
        return _print_status(
            cfg, paths, database=getattr(args, "database", None)
        )

    if args.command == "intake":
        return _intake(cfg, args.database)

    if args.command == "backup-state":
        return _backup_state(
            cfg, paths, retain_days=int(args.retain_days)
        )

    if args.command == "update":
        database = getattr(args, "database", None)
        if database is not None:
            try:
                select_databases(cfg, database)
            except ValueError as exc:
                print(str(exc))
                return 1
        try:
            if int(getattr(cfg, "schema_version", 1) or 1) >= 2:
                # Seed engine from the filtered (or first) profile; v2 reconcile
                # re-wraps per database so idle_probe stays profile-scoped.
                profiles = select_databases(cfg, database)
                idle = profiles[0].adapters.idle_probe if profiles else None
                if not idle:
                    raise MissingAdapters("database adapters.idle_probe is required")
                docker = CommandEngine(idle)
            else:
                if cfg.adapters is None:
                    raise MissingAdapters("adapters section is required")
                docker = CommandEngine(cfg.adapters.idle_probe)
                profiles = []
        except MissingAdapters:
            return 1

        reload_target = profiles[0] if profiles else cfg

        def _reload() -> None:
            _bsl_watch_reload(reload_target)

        result = reconcile(
            cfg,
            timer=False,
            measure=bool(args.measure),
            paths=paths,
            docker=docker,
            watch_reload=_reload,
            database=database,
        )
        return 0 if result in {"success", "deferred"} else 1

    if args.command == "open":
        database = getattr(args, "database", None)
        open_target = cfg
        if int(getattr(cfg, "schema_version", 1) or 1) >= 2:
            enabled = enabled_databases(cfg)
            if database is None and len(enabled) != 1:
                print(
                    "open requires --database when multiple databases are enabled"
                )
                return 1
            if database is None and len(enabled) == 1:
                database = next(iter(enabled))
            if database is not None:
                try:
                    open_target = select_databases(cfg, database)[0]
                except ValueError as exc:
                    print(str(exc))
                    return 1

        def _reload_open() -> None:
            _bsl_watch_reload(open_target)

        result = reopen_reads(
            cfg,
            paths=paths,
            watch_reload=_reload_open,
            database=database,
        )
        return 0 if result == "success" else 1

    if args.command == "cleanup":
        held = acquire(paths.lock_path)
        if held is None:
            return 1
        conn = init_db(paths.db_path)
        try:
            with held:
                ids = cleanup_jobs(
                    conn,
                    _docker(),
                    dry_run=bool(args.dry_run),
                    protected=protected_container_names(cfg),
                )
        finally:
            conn.close()
        for cid in ids:
            print(cid)
        return 0

    if args.command == "jobs" and getattr(args, "jobs_command", None) == "fail":
        held = acquire(paths.lock_path)
        if held is None:
            print(
                "jobs fail: another contour process holds the lock",
                file=sys.stderr,
            )
            return 1
        conn = init_db(paths.db_path)
        try:
            with held:
                row = conn.execute(
                    """
                    SELECT id, status, container_id, exec_id FROM jobs
                    WHERE id = ?
                    """,
                    (args.job_id,),
                ).fetchone()
                if row is None:
                    print(f"jobs fail: no job {args.job_id}", file=sys.stderr)
                    return 1
                if str(row["status"]) in {"ok", "failed", "removed"}:
                    print(
                        f"jobs fail: job {args.job_id} already finished "
                        f"({row['status']})",
                        file=sys.stderr,
                    )
                    return 1
                reason = str(getattr(args, "reason", "") or "").strip()
                if getattr(args, "force", False):
                    if not reason:
                        print(
                            "jobs fail: --force requires --reason "
                            "(who verified the writer and how)",
                            file=sys.stderr,
                        )
                        return 1
                else:
                    block = _writer_gone(row, _docker())
                    if block is not None:
                        print(
                            f"jobs fail: refusing, {block}; verify the writer "
                            "yourself and rerun with --force --reason, or fix "
                            "Docker first",
                            file=sys.stderr,
                        )
                        return 1
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'failed', finished_at = ?, exit_code = NULL
                    WHERE id = ?
                    """,
                    (now, args.job_id),
                )
                if getattr(args, "force", False):
                    from contour.db import set_meta

                    set_meta(
                        conn,
                        f"jobs_fail_override:{args.job_id}",
                        f"{now} reason={reason}",
                    )
                conn.commit()
                forced = " (forced)" if getattr(args, "force", False) else ""
                print(
                    f"jobs fail ok{forced}: job {args.job_id} marked failed; "
                    "next run resumes or rebuilds the layer"
                )
                return 0
        finally:
            conn.close()

    return 2
