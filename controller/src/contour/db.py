from __future__ import annotations

import sqlite3

META_KEYS = ("reads_close_requested", "reads_closed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS components (
  name TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  applied_commit TEXT,
  storage_id TEXT,
  indexer_version TEXT,
  container_id TEXT,
  exec_id TEXT,
  detail TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  target_commit TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  result TEXT,
  message TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL,
  stage TEXT NOT NULL,
  status TEXT NOT NULL,
  container_id TEXT,
  exec_id TEXT,
  started_at TEXT,
  finished_at TEXT,
  exit_code INTEGER,
  log_path TEXT
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for key in META_KEYS:
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
            (key, ""),
        )
    conn.commit()
    return conn


def get_component(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM components WHERE name = ?",
        (name,),
    ).fetchone()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return ""
    return str(row["value"])
