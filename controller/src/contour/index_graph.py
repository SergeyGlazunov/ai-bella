from __future__ import annotations

import json
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from contour.db import set_meta
from contour.dockerutil import (
    DockerRunner,
    SubprocessRunner,
    compose_create,
    start,
    wait,
)

SRV_LOGS = Path("/srv/ai/logs")

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


def _insert_job(conn: sqlite3.Connection, run_id: int, stage: str, container_id: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO jobs (run_id, stage, status, container_id, started_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (run_id, stage, "created", container_id, now),
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


def _log_directory() -> Path:
    if SRV_LOGS.is_dir():
        return SRV_LOGS
    return Path(tempfile.gettempdir())


def copy_job_logs(docker: DockerRunner, container_id: str) -> str | None:
    """Write container logs under /srv/ai/logs if it exists, else tempfile."""
    try:
        result = docker.run(["docker", "logs", container_id])
    except Exception:
        return None
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in container_id)
    path = _log_directory() / f"contour-graph-{safe}.log"
    try:
        path.write_text(
            (result.stdout or "") + (result.stderr or ""),
            encoding="utf-8",
        )
        return str(path)
    except OSError:
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".log",
            prefix="contour-graph-log-",
            delete=False,
            encoding="utf-8",
        )
        with handle:
            handle.write(result.stdout or "")
            if result.stderr:
                handle.write(result.stderr)
        return handle.name


def _copy_logs(docker: DockerRunner, container_id: str) -> str | None:
    return copy_job_logs(docker, container_id)


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


def _run_graph_job(
    cfg,
    conn: sqlite3.Connection,
    run_id: int,
    stage: str,
    docker: DockerRunner,
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
    job_id = _insert_job(conn, run_id, stage, container_id)
    start(container_id, runner=docker)
    exit_code = wait(container_id, runner=docker)
    log_path = _copy_logs(docker, container_id)
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
) -> None:
    docker = _runner(runner)
    code = 3 if full_needed else _run_graph_job(cfg, conn, run_id, "graph", docker)
    if code == 0:
        set_meta(conn, "graph_full_needed", "")
        return
    if code == 3:
        set_meta(conn, "graph_full_needed", "1")
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
        full_code = _run_graph_job(cfg, conn, run_id, "graph_full", docker)
        if full_code != 0:
            raise RuntimeError(f"graph FULL failed with exit {full_code}")
        set_meta(conn, "graph_full_needed", "")
        return
    raise RuntimeError(f"graph index failed with exit {code}")
