from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from contour import dockerutil
from contour.db import get_meta, set_meta
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


def _attempt_meta_key(database: str) -> str:
    return f"bsl_attempt:{database}"


def _max_job_id(conn: sqlite3.Connection, database: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS m FROM jobs WHERE database = ?",
        (database,),
    ).fetchone()
    if row is None:
        return 0
    value = row["m"] if hasattr(row, "keys") else row[0]
    return int(value or 0)


def _record_bsl_attempt(
    conn: sqlite3.Connection,
    database: str,
    revision: str,
    storage_id: str,
    indexer_version: str,
) -> None:
    """Persist the in-flight BSL attempt (not applied_revision) as one meta blob.

    A new revision/storage/image starts a new attempt and sets job_floor to the
    current max job id so earlier successes cannot satisfy stage checks.
    Retrying the same attempt keeps the same floor so a completed background
    index from this attempt remains usable.
    """
    prev = _read_bsl_attempt(conn, database)
    if (
        prev is not None
        and prev[0] == revision
        and prev[1] == storage_id
        and prev[2] == indexer_version
    ):
        job_floor = prev[3]
    else:
        job_floor = _max_job_id(conn, database)
    payload = {
        "revision": revision,
        "storage": storage_id,
        "image": indexer_version,
        "job_floor": job_floor,
    }
    set_meta(conn, _attempt_meta_key(database), json.dumps(payload, sort_keys=True))


def _read_bsl_attempt(
    conn: sqlite3.Connection, database: str
) -> tuple[str, str, str, int] | None:
    raw = get_meta(conn, _attempt_meta_key(database))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    revision = str(data.get("revision") or "")
    storage = str(data.get("storage") or "")
    image = str(data.get("image") or "")
    try:
        job_floor = int(data.get("job_floor", -1))
    except (TypeError, ValueError):
        return None
    if not revision or not storage or not image or job_floor < 0:
        return None
    return revision, storage, image, job_floor


def _capture_live_identity(
    cfg, docker: DockerRunner
) -> tuple[str, str]:
    storage = dockerutil.volume_identity(
        cfg.storage.code_index_volume, runner=docker
    )
    image = dockerutil.image_id(cfg.services.bsl_container, runner=docker)
    if not storage or not image:
        raise RuntimeError("cannot record BSL attempt identity from live storage")
    return storage, image


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
    *,
    observe_cap: float | None = None,
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
    exit_code = wait_exec(container, exec_id, runner=docker, observe_cap=observe_cap)
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


def _stage_succeeded_for_attempt(
    conn: sqlite3.Connection,
    database: str,
    stage: str,
    revision: str,
    job_floor: int,
) -> bool:
    """True when the latest job for this stage within the attempt finished ok.

    Jobs at or below ``job_floor`` belong to earlier attempts (other volumes /
    revisions) and must not satisfy resume.
    """
    row = conn.execute(
        """
        SELECT j.status, j.exit_code
        FROM jobs j
        JOIN runs r ON r.id = j.run_id
        WHERE j.database = ?
          AND j.stage = ?
          AND r.target_revision = ?
          AND j.id > ?
        ORDER BY j.id DESC
        LIMIT 1
        """,
        (database, stage, revision, job_floor),
    ).fetchone()
    if row is None:
        return False
    status = str(row["status"] if hasattr(row, "keys") else row[0] or "")
    exit_code = row["exit_code"] if hasattr(row, "keys") else row[1]
    try:
        code = int(exit_code) if exit_code is not None else 1
    except (TypeError, ValueError):
        return False
    return status == "ok" and code == 0


def should_resume_bsl_stages(
    cfg,
    conn: sqlite3.Connection,
    *,
    revision: str,
    runner: DockerRunner | None = None,
) -> bool:
    """Resume unfinished stages using the recorded current attempt, not applied_revision.

    An attempt records target revision + live storage/image *before* the writer
    starts. Resume requires that record to match this revision and live identity.
    Empty identity never counts as a match. applied_revision is unused here.
    """
    database = str(getattr(cfg, "name", None) or "ubd")
    row = conn.execute(
        """
        SELECT status FROM layer_state
        WHERE database = ? AND layer = 'code_index'
        """,
        (database,),
    ).fetchone()
    if row is None:
        return False
    status = str(row["status"] or "")
    if status not in {"building", "failed"}:
        return False
    attempt = _read_bsl_attempt(conn, database)
    if attempt is None:
        return False
    attempt_rev, attempt_storage, attempt_image, _job_floor = attempt
    if attempt_rev != revision:
        return False
    docker = _runner(runner)
    try:
        live_storage, live_image = _capture_live_identity(cfg, docker)
    except Exception:
        return False
    if live_storage != attempt_storage or live_image != attempt_image:
        return False
    return True


def index_bsl(
    cfg,
    conn: sqlite3.Connection,
    run_id: int,
    *,
    runner: DockerRunner | None = None,
    may_write=None,
    resume: bool | None = None,
    observe_cap: float | None = None,
) -> None:
    docker = _runner(runner)
    database = _database_for_run(cfg, conn, run_id)
    revision = _run_revision(conn, run_id)
    if resume is None:
        resume = bool(
            revision
            and should_resume_bsl_stages(
                cfg, conn, revision=revision, runner=docker
            )
        )
    attempt = _read_bsl_attempt(conn, database) if resume else None
    job_floor = attempt[3] if attempt is not None else -1

    def stage_ok(stage: str) -> bool:
        return bool(
            resume
            and revision is not None
            and attempt is not None
            and _stage_succeeded_for_attempt(
                conn, database, stage, revision, job_floor
            )
        )

    run_index = not stage_ok("bsl_index")
    if run_index:
        # Record attempt identity before the writer when revision is known
        # (v2). Legacy callers without target_revision still run index once.
        if revision is not None:
            live_storage, live_image = _capture_live_identity(cfg, docker)
            _record_bsl_attempt(
                conn, database, revision, live_storage, live_image
            )
        _run_exec(
            cfg, conn, run_id, "bsl_index", ["index", "--path", SRC_PATH], docker,
            observe_cap=observe_cap,
        )
    if may_write is not None:
        decision = may_write()
        if decision != "ok":
            from contour.runtime_policy import WriteWindowClosed

            raise WriteWindowClosed(str(decision))
    # Clean: attempt-scoped latest status (not historical jobs from other volumes).
    if revision is None:
        _run_exec(
            cfg, conn, run_id, "bsl_clean", ["clean", "--path", SRC_PATH], docker,
            observe_cap=observe_cap,
        )
    else:
        recorded = _read_bsl_attempt(conn, database)
        floor = recorded[3] if recorded is not None else -1
        if recorded is None or not _stage_succeeded_for_attempt(
            conn, database, "bsl_clean", revision, floor
        ):
            _run_exec(
                cfg,
                conn,
                run_id,
                "bsl_clean",
                ["clean", "--path", SRC_PATH],
                docker,
                observe_cap=observe_cap,
            )
