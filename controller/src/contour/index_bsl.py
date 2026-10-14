from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from contour.dockerutil import (
    DockerRunner,
    SubprocessRunner,
    create_exec,
    start_exec,
    wait_exec,
)

CODE_INDEX_HOME = "/data/code-index-home"
SRC_PATH = "/repos/SRC"


def _runner(runner: DockerRunner | None) -> DockerRunner:
    return runner if runner is not None else SubprocessRunner()


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


def _insert_exec_job(
    conn: sqlite3.Connection,
    run_id: int,
    stage: str,
    container_id: str,
    exec_id: str,
    *,
    database: str,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO jobs (
          database, run_id, stage, status, container_id, exec_id, started_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (database, run_id, stage, "created", container_id, exec_id, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def _mark_exec_running(conn: sqlite3.Connection, job_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        UPDATE jobs SET status = ?, started_at = ? WHERE id = ?
        """,
        ("running", now, job_id),
    )
    conn.commit()


def _finish_exec_job(
    conn: sqlite3.Connection, job_id: int, exit_code: int
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    status = "ok" if exit_code == 0 else "failed"
    conn.execute(
        """
        UPDATE jobs
        SET exit_code = ?, status = ?, finished_at = ?
        WHERE id = ?
        """,
        (exit_code, status, now, job_id),
    )
    conn.commit()


def _run_exec(
    cfg,
    conn: sqlite3.Connection,
    run_id: int,
    stage: str,
    args: list[str],
    docker: DockerRunner,
) -> None:
    """Create exec → INSERT job → start → wait (R04: no race on ExecIDs)."""
    container = cfg.services.bsl_container
    env = [f"CODE_INDEX_HOME={CODE_INDEX_HOME}"]
    cmd = ["bsl-indexer", *args]
    exec_id = create_exec(container, cmd, env=env, runner=docker)
    database = _database_for_run(cfg, conn, run_id)
    job_id = _insert_exec_job(
        conn, run_id, stage, container, exec_id, database=database
    )
    start_exec(exec_id, detach=True, runner=docker)
    _mark_exec_running(conn, job_id)
    exit_code = wait_exec(container, exec_id, runner=docker)
    _finish_exec_job(conn, job_id, exit_code)
    if exit_code != 0:
        raise RuntimeError(f"{stage} failed with exit {exit_code}")


def _run_revision(conn: sqlite3.Connection, run_id: int) -> str | None:
    row = conn.execute(
        "SELECT target_revision FROM runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    value = row["target_revision"] if hasattr(row, "keys") else row[0]
    if value in (None, ""):
        return None
    return str(value)


def _stage_succeeded_for_revision(
    conn: sqlite3.Connection,
    database: str,
    stage: str,
    revision: str,
) -> bool:
    """True when a prior job for the same database/revision finished ok."""
    row = conn.execute(
        """
        SELECT j.id
        FROM jobs j
        JOIN runs r ON r.id = j.run_id
        WHERE j.database = ?
          AND j.stage = ?
          AND j.status = 'ok'
          AND COALESCE(j.exit_code, 1) = 0
          AND r.target_revision = ?
        ORDER BY j.id DESC
        LIMIT 1
        """,
        (database, stage, revision),
    ).fetchone()
    return row is not None


def index_bsl(
    cfg,
    conn: sqlite3.Connection,
    run_id: int,
    *,
    runner: DockerRunner | None = None,
    may_write=None,
) -> None:
    docker = _runner(runner)
    database = _database_for_run(cfg, conn, run_id)
    revision = _run_revision(conn, run_id)
    if revision is None or not _stage_succeeded_for_revision(
        conn, database, "bsl_index", revision
    ):
        _run_exec(
            cfg, conn, run_id, "bsl_index", ["index", "--path", SRC_PATH], docker
        )
    if may_write is not None:
        decision = may_write()
        if decision != "ok":
            from contour.runtime_policy import WriteWindowClosed

            raise WriteWindowClosed(str(decision))
    if revision is None or not _stage_succeeded_for_revision(
        conn, database, "bsl_clean", revision
    ):
        _run_exec(
            cfg, conn, run_id, "bsl_clean", ["clean", "--path", SRC_PATH], docker
        )
