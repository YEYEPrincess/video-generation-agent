"""SQLite 连接、建表、事务、时间工具。

设计要点（ARCHITECTURE.md §5 / PROJECT-PLAN.md §3.3）：
- WAL + busy_timeout，允许多进程并发读写；
- 时间戳双轨：`lease_until` 用 epoch 秒（数值比较，跨进程安全），
  展示字段用 ISO8601 UTC 字符串（人可读）；
- 事务一律显式 `BEGIN IMMEDIATE`，避免"读后写"升级锁导致的 busy。
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id                 TEXT PRIMARY KEY,
    topic              TEXT NOT NULL,
    partition          TEXT,
    status             TEXT NOT NULL,
    current_stage      TEXT,
    attempt            INTEGER NOT NULL DEFAULT 0,
    qc_rounds          INTEGER NOT NULL DEFAULT 0,
    worker_id          TEXT,
    lease_until        REAL,
    budget_spent_cents INTEGER NOT NULL DEFAULT 0,
    idem_submit        TEXT UNIQUE,
    error              TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at);

CREATE TABLE IF NOT EXISTS stages (
    task_id     TEXT NOT NULL,
    stage       TEXT NOT NULL,
    status      TEXT NOT NULL,
    attempt     INTEGER NOT NULL DEFAULT 0,
    started_at  TEXT,
    finished_at TEXT,
    idem_key    TEXT,
    artifact_id TEXT,
    error       TEXT,
    PRIMARY KEY (task_id, stage)
);

CREATE TABLE IF NOT EXISTS shots (
    task_id      TEXT NOT NULL,
    idx          INTEGER NOT NULL,
    narration    TEXT,
    visual_prompt TEXT,
    kind         TEXT,
    asset_path   TEXT,
    sim_score    REAL,
    status       TEXT NOT NULL DEFAULT 'PENDING',
    retry_count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, idx)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id         TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL,
    stage      TEXT NOT NULL,
    version    INTEGER NOT NULL,
    path       TEXT NOT NULL,
    hash       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'json',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id, stage);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    ts          TEXT NOT NULL,
    level       TEXT NOT NULL DEFAULT 'info',
    stage       TEXT,
    type        TEXT NOT NULL,
    duration_ms INTEGER,
    tokens      INTEGER,
    cost_cents  INTEGER,
    payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """打开连接。注意：单连接可跨线程使用，但不要跨线程并发写同一连接。"""
    conn = sqlite3.connect(db_path, timeout=10.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """轻量迁移：老库补列。生产环境该上 alembic，demo 用 pragma 足够。"""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    if "qc_rounds" not in columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN qc_rounds INTEGER NOT NULL DEFAULT 0")


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式写事务。SQLite 没有真正的嵌套，注意不要嵌套调用。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def now() -> float:
    """当前 epoch 秒。"""
    return time.time()


def iso(ts: float | None = None) -> str:
    """epoch 秒 -> ISO8601 UTC（毫秒精度，以 Z 结尾）。"""
    dt = datetime.fromtimestamp(now() if ts is None else ts, tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
