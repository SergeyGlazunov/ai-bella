from __future__ import annotations

import argparse
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from contour.adapters import CommandEngine, MissingAdapters, build_probes
from contour.alert import command_is_runnable
from contour.config import enabled_databases, load_config
from contour.db import get_component, get_meta, init_db
from contour.dockerutil import SubprocessRunner
from contour.gitutil import safe_remote
from contour.lock import acquire
from contour.reconcile import (
    DEFAULT_DB,
    DEFAULT_LOCK,
    RuntimePaths,
    cleanup_jobs,
    reconcile,
    reopen_reads,
    resolve_runtime_paths,
    select_databases,
)
from contour.watch_bsl import reload_bsl_daemon


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


def _print_status(cfg, paths: RuntimePaths) -> None:
    print(f"remote: {safe_remote(cfg.repo.remote)}")
    print(f"branch: {cfg.repo.branch}")
    print(f"repo: {cfg.repo.path}")
    conn = init_db(paths.db_path)
    try:
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
            for db_name in enabled_databases(cfg):
                row = conn.execute(
                    """
                    SELECT lifecycle_status, published_revision, detail, updated_at
                    FROM database_state WHERE database = ?
                    """,
                    (db_name,),
                ).fetchone()
                if row is None:
                    print(f"  {db_name}: (no state)")
                    continue
                print(
                    f"  {db_name}: status={row['lifecycle_status']} "
                    f"published={row['published_revision'] or '-'} "
                    f"detail={row['detail'] or '-'}"
                )
                layers = conn.execute(
                    """
                    SELECT layer, status, applied_revision FROM layer_state
                    WHERE database = ? ORDER BY layer
                    """,
                    (db_name,),
                ).fetchall()
                for layer in layers:
                    print(
                        f"    {layer['layer']}: {layer['status']} "
                        f"rev={layer['applied_revision'] or '-'}"
                    )
    finally:
        conn.close()


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
    try:
        build_probes(cfg, _docker())
        if cfg.adapters is None or not cfg.adapters.idle_probe:
            raise MissingAdapters("adapters.idle_probe is required")
    except MissingAdapters:
        return 1
    return 0


def _bsl_watch_reload(cfg) -> None:
    """Reload daemon after daemon.toml pause/restore (R03/F03). Require reloaded=true."""
    reload_bsl_daemon(cfg)


def main(argv: list[str] | None = None) -> int:
    raw = list(argv) if argv is not None else None
    # R01: pull --config/--db/--lock from anywhere so both
    # `contour --config X validate` and `contour validate --config X` work.
    global_parser = argparse.ArgumentParser(add_help=False)
    global_parser.add_argument("--config", default="/opt/ai-sync/config.yaml")
    # None defaults so v2 cfg.runtime is not silently overridden by legacy paths.
    global_parser.add_argument("--db", default=None)
    global_parser.add_argument("--lock", default=None)
    global_args, rest = global_parser.parse_known_args(raw)

    parser = argparse.ArgumentParser(prog="contour")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate")
    sub.add_parser("status")
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

    args = parser.parse_args(rest)
    args.config = global_args.config
    args.db = global_args.db
    args.lock = global_args.lock

    if args.command == "validate":
        try:
            cfg = load_config(args.config)
        except Exception:
            return 1
        return _validate(cfg)

    try:
        cfg = load_config(args.config)
    except Exception:
        return 1

    paths = resolve_runtime_paths(
        cfg,
        db_path=args.db,
        lock_path=args.lock,
    )

    if args.command == "status":
        _print_status(cfg, paths)
        return 0

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
                database = enabled[0].name
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
                ids = cleanup_jobs(conn, _docker(), dry_run=bool(args.dry_run))
        finally:
            conn.close()
        for cid in ids:
            print(cid)
        return 0

    return 2
