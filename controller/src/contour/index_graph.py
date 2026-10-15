from __future__ import annotations

import json
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from contour.db import get_meta, set_meta
from contour.dockerutil import (
    DockerRunner,
    SubprocessRunner,
    compose_create,
    start,
    wait,
)


def graph_full_needed_key(database: str) -> str:
    """Meta key for pending Graph FULL.

    UBD keeps the legacy unscoped ``graph_full_needed`` key so existing
    reconcile recovery keeps working until Task 8 migrates it. Other
    databases use a scoped key and must not share UBD's flag.
    """
    if database == "ubd":
        return "graph_full_needed"
    return f"graph_full_needed:{database}"


def set_graph_full_needed(
    conn: sqlite3.Connection, database: str, needed: bool
) -> None:
    set_meta(conn, graph_full_needed_key(database), "1" if needed else "")


def is_graph_full_needed(conn: sqlite3.Connection, database: str) -> bool:
    return get_meta(conn, graph_full_needed_key(database)) == "1"

# Prefer runtime.logs_path from callers. Fallbacks match bootstrap layout, then
# the legacy single-tree path so older stands keep writing somewhere durable.
_DEFAULT_LOG_DIRS = (
    Path("/srv/ai-contour/logs/runs"),
    Path("/srv/ai-contour/logs"),
    Path("/srv/ai/logs"),
)

_COMPOSE_BASENAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)
_OVERRIDE_BASENAMES = (
    "compose.override.yaml",
    "compose.override.yml",
    "docker-compose.override.yaml",
    "docker-compose.override.yml",
)


def _runner(runner: DockerRunner | None) -> DockerRunner:
    return runner if runner is not None else SubprocessRunner()


def _existing_compose_files(compose_dir: str) -> list[str]:
    directory = Path(compose_dir)
    files: list[str] = []
    for name in _COMPOSE_BASENAMES:
        path = directory / name
        if path.is_file():
            files.append(str(path))
            break
    for name in _OVERRIDE_BASENAMES:
        path = directory / name
        if path.is_file():
            files.append(str(path))
            break
    return files


def _database_for_run(cfg, conn: sqlite3.Connection, run_id: int) -> str:
    name = getattr(cfg, "name", None)
    if name:
        return str(name)
    row = conn.execute(
        "SELECT database FROM runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    if row is not None:
        return str(row["database"] if hasattr(row, "keys") else row[0])
    return "ubd"


def _insert_job(
    conn: sqlite3.Connection,
    run_id: int,
    stage: str,
    container_id: str,
    *,
    database: str,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO jobs (database, run_id, stage, status, container_id, started_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (database, run_id, stage, "created", container_id, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def _finish_job(
    conn: sqlite3.Connection,
    job_id: int,
    exit_code: int,
    log_path: str | None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    status = "ok" if exit_code == 0 else "failed"
    conn.execute(
        """
        UPDATE jobs
        SET exit_code = ?, status = ?, finished_at = ?, log_path = ?
        WHERE id = ?
        """,
        (exit_code, status, now, log_path, job_id),
    )
    conn.commit()


def _write_override(text: str) -> str:
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".yml",
        prefix="contour-graph-",
        delete=False,
        encoding="utf-8",
    )
    with handle:
        handle.write(text)
    return handle.name


def _logs_dir_from_cfg(cfg) -> Path | None:
    runtime = getattr(cfg, "runtime", None) if cfg is not None else None
    path = getattr(runtime, "logs_path", None) if runtime is not None else None
    if path:
        return Path(str(path))
    return None


def _log_directory(logs_dir: str | Path | None = None) -> Path:
    """Resolve a writable log directory; never raises for bad configured paths."""
    if logs_dir is not None:
        path = Path(logs_dir)
        try:
            path.mkdir(parents=True, exist_ok=True)
            if path.is_dir():
                return path
        except OSError:
            pass
    for candidate in _DEFAULT_LOG_DIRS:
        if candidate.is_dir():
            return candidate
    return Path(tempfile.gettempdir())


def copy_job_logs(
    docker: DockerRunner,
    container_id: str,
    *,
    logs_dir: str | Path | None = None,
    cfg=None,
) -> str | None:
    """Write container logs under runtime.logs_path (or host fallback), else tempfile.

    Bounded by the docker command timeout. Failure returns None without
    affecting the caller's job exit_code — including mkdir/path errors.
    """
    from contour.runtime_policy import DEFAULT_RUNTIME_POLICY

    try:
        result = docker.run(
            ["docker", "logs", container_id],
            timeout=DEFAULT_RUNTIME_POLICY.docker_command_timeout_seconds,
        )
    except Exception:
        return None
    resolved = logs_dir if logs_dir is not None else _logs_dir_from_cfg(cfg)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in container_id)
    body = (result.stdout or "") + (result.stderr or "")
    try:
        path = _log_directory(resolved) / f"contour-graph-{safe}.log"
        path.write_text(body, encoding="utf-8")
        return str(path)
    except OSError:
        try:
            handle = tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".log",
                prefix="contour-graph-log-",
                delete=False,
                encoding="utf-8",
            )
            with handle:
                handle.write(body)
            return handle.name
        except OSError:
            return None


def _copy_logs(
    docker: DockerRunner,
    container_id: str,
    *,
    cfg=None,
    logs_dir: str | Path | None = None,
) -> str | None:
    return copy_job_logs(docker, container_id, cfg=cfg, logs_dir=logs_dir)


def allow_graph_full(
    *,
    may_write_decision: str,
    remaining_budget: float | None,
    measure: bool,
    deadline_seconds: int | None,
) -> bool:
    """Start FULL only inside the window. Duration warn does not block. Running jobs are not killed."""
    del measure, deadline_seconds
    if may_write_decision != "ok":
        return False
    if remaining_budget is not None and remaining_budget <= 0:
        return False
    return True


def _job_identity(stage: str) -> tuple[str, str]:
    token = uuid.uuid4().hex[:12]
    name = f"contour-{stage.replace('_', '-')}-{token}"
    return name, name


def _build_override(
    stage: str,
    base_service: str,
    job_service: str,
    container_name: str,
    base_file: str,
) -> str:
    extra = (
        "    environment:\n      - METADATA_FORCE_REINDEX=true\n"
        if stage == "graph_full"
        else '    entrypoint: ["python"]\n    command: ["/app/apply_changes.py"]\n'
    )
    return (
        "services:\n"
        f"  {job_service}:\n"
        "    extends:\n"
        f"      file: {json.dumps(base_file)}\n"
        f"      service: {base_service}\n"
        f"    container_name: {container_name}\n"
        f"{extra}"
    )


def _resolve_logs_path(cfg, logs_path: str | Path | None) -> Path | None:
    if logs_path is not None:
        return Path(logs_path)
    return _logs_dir_from_cfg(cfg)


def _run_graph_job(
    cfg,
    conn: sqlite3.Connection,
    run_id: int,
    stage: str,
    docker: DockerRunner,
    *,
    logs_path: str | Path | None = None,
) -> int:
    job_service, container_name = _job_identity(stage)
    existing = _existing_compose_files(cfg.services.graph_compose_dir)
    base_file = str(Path(existing[0]).resolve().as_posix()) if existing else ""
    override_text = _build_override(
        stage,
        cfg.services.graph_service,
        job_service,
        container_name,
        base_file,
    )
    override_path = _write_override(override_text)
    files = [*existing, override_path]
    container_id = compose_create(
        cfg.services.graph_compose_dir,
        cfg.services.graph_project,
        [job_service],
        runner=docker,
        files=files,
        container_name=container_name,
    )
    database = _database_for_run(cfg, conn, run_id)
    job_id = _insert_job(conn, run_id, stage, container_id, database=database)
    start(container_id, runner=docker)
    exit_code = wait(container_id, runner=docker)
    # Logging must never block recording the real indexer exit code.
    log_path = None
    try:
        log_path = _copy_logs(
            docker, container_id, cfg=cfg, logs_dir=logs_path
        )
    except Exception:
        log_path = None
    _finish_job(conn, job_id, exit_code, log_path)
    return exit_code


def index_graph(
    cfg,
    conn: sqlite3.Connection,
    run_id: int,
    *,
    runner: DockerRunner | None = None,
    may_write_decision: str = "ok",
    may_write_fn=None,
    remaining_budget: float | None = None,
    measure: bool = False,
    full_needed: bool = False,
    logs_path: str | Path | None = None,
) -> str:
    """Run graph delta/FULL. Returns the writer container id used last.

    ``logs_path`` is the host directory for diagnostic files (typically
    ``cfg.runtime.logs_path``). Pass it explicitly when ``cfg`` is a
    ``DatabaseConfig`` without ``runtime``.
    """
    docker = _runner(runner)
    database = _database_for_run(cfg, conn, run_id)
    resolved_logs = _resolve_logs_path(cfg, logs_path)
    code = (
        3
        if full_needed
        else _run_graph_job(
            cfg, conn, run_id, "graph", docker, logs_path=resolved_logs
        )
    )
    if code == 0:
        set_graph_full_needed(conn, database, False)
        return _latest_graph_writer(conn, run_id)
    if code == 3:
        set_graph_full_needed(conn, database, True)
        decision = may_write_fn() if callable(may_write_fn) else may_write_decision
        deadline = getattr(getattr(cfg, "schedule", None), "build_deadline_seconds", None)
        if not allow_graph_full(
            may_write_decision=decision,
            remaining_budget=remaining_budget,
            measure=measure,
            deadline_seconds=deadline,
        ):
            raise RuntimeError(
                "graph FULL skipped: may_write deferred or window remaining is 0"
            )
        full_code = _run_graph_job(
            cfg, conn, run_id, "graph_full", docker, logs_path=resolved_logs
        )
        if full_code != 0:
            raise RuntimeError(f"graph FULL failed with exit {full_code}")
        set_graph_full_needed(conn, database, False)
        return _latest_graph_writer(conn, run_id)
    raise RuntimeError(f"graph index failed with exit {code}")


def _latest_graph_writer(conn: sqlite3.Connection, run_id: int) -> str:
    row = conn.execute(
        """
        SELECT container_id FROM jobs
        WHERE run_id = ? AND stage IN ('graph', 'graph_full')
          AND container_id IS NOT NULL AND container_id != ''
        ORDER BY id DESC LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"graph writer container missing for run {run_id}")
    return str(row["container_id"] if hasattr(row, "keys") else row[0])
