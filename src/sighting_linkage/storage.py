"""多源目击关联归并服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('reporter', 'specialist', 'dispatcher', 'auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

-- 原始上报：一经写入不可修改、不可删除，归并事件只通过编号引用它。
CREATE TABLE IF NOT EXISTS sight_records (
    record_id TEXT PRIMARY KEY,
    observer_id TEXT NOT NULL,
    observer_role TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    time_uncertainty_seconds INTEGER NOT NULL CHECK (time_uncertainty_seconds >= 0),
    valley TEXT NOT NULL,
    latitude TEXT,
    longitude TEXT,
    location_radius_meters INTEGER,
    location_precision_text TEXT NOT NULL,
    species_code TEXT NOT NULL,
    species_status TEXT NOT NULL CHECK (species_status IN ('confirmed', 'probable', 'suspected')),
    species_confidence TEXT NOT NULL,
    image_summary_json TEXT,
    sensitive INTEGER NOT NULL DEFAULT 0 CHECK (sensitive IN (0, 1)),
    visibility TEXT NOT NULL CHECK (visibility IN ('submitter', 'ecology', 'dispatch')),
    raw_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    submitted_by TEXT NOT NULL REFERENCES users(user_id),
    submitted_at TEXT NOT NULL,
    UNIQUE (content_sha256)
);

-- 关联计算运行：同一批记录输入（按内容摘要）永远返回同一个运行。
CREATE TABLE IF NOT EXISTS linkage_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    input_sha256 TEXT NOT NULL CHECK (length(input_sha256) = 64),
    algorithm_version TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE (scope, input_sha256)
);

CREATE TABLE IF NOT EXISTS linkage_run_records (
    run_id INTEGER NOT NULL REFERENCES linkage_runs(run_id),
    record_id TEXT NOT NULL REFERENCES sight_records(record_id),
    position INTEGER NOT NULL,
    PRIMARY KEY (run_id, record_id)
);

-- 统一事件：调度室救护派遣所依据的归并对象。
CREATE TABLE IF NOT EXISTS incidents (
    event_id TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL DEFAULT 0 CHECK (current_version >= 0),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

-- 事件版本：只追加、不修改；同一运行在同一事件上只能产生一个版本，
-- 重复确认返回既有版本，新记录（新运行）才能产生新版本。
-- dissolved 版本是人工推翻决定，run_id 可空（无新关联运行时）。
CREATE TABLE IF NOT EXISTS incident_versions (
    event_id TEXT NOT NULL REFERENCES incidents(event_id),
    version_no INTEGER NOT NULL CHECK (version_no > 0),
    run_id INTEGER REFERENCES linkage_runs(run_id),
    state TEXT NOT NULL CHECK (state IN ('confirmed', 'dissolved')),
    members_json TEXT NOT NULL,
    note TEXT NOT NULL,
    decided_by TEXT NOT NULL REFERENCES users(user_id),
    decided_at TEXT NOT NULL,
    PRIMARY KEY (event_id, version_no),
    UNIQUE (run_id, event_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sight_records_submitted ON sight_records(submitted_by);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events(entity_type, entity_id, event_id);
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "users", "sight_records", "linkage_runs", "linkage_run_records",
    "incidents", "incident_versions", "audit_events",
})


def connect(path: str | Path, *, cross_thread: bool = False) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。

    HTTP 服务多线程共享同一连接时传 cross_thread=True；请求由 API 层串行化。
    """

    connection = sqlite3.connect(
        str(path), isolation_level=None, check_same_thread=not cross_thread
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """显式事务；异常时保证回滚。"""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    """初始化基础资料表，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }
