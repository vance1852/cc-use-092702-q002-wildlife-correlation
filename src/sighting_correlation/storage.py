"""目击关联服务的 SQLite 模式与事务辅助。"""

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
    role TEXT NOT NULL CHECK (role IN ('observer', 'specialist', 'auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

-- 原始上报：只追加，永不更新。归并、拆回都只引用其内容摘要。
CREATE TABLE IF NOT EXISTS sightings (
    sighting_id TEXT PRIMARY KEY,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    payload_json TEXT NOT NULL,
    reported_by TEXT NOT NULL REFERENCES users(user_id),
    observer_kind TEXT NOT NULL,
    organization TEXT NOT NULL,
    sensitive_location INTEGER NOT NULL CHECK (sensitive_location IN (0, 1)),
    observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 提交者声明的可见范围：被授权人才能看到敏感位置的精确几何。
CREATE TABLE IF NOT EXISTS sighting_visibility (
    sighting_id TEXT NOT NULL REFERENCES sightings(sighting_id),
    user_id TEXT NOT NULL REFERENCES users(user_id),
    granted_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (sighting_id, user_id)
);

-- 关联版本内容寻址：同一算法版本 + 同一组记录摘要必然命中同一行。
CREATE TABLE IF NOT EXISTS link_versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    algorithm_version TEXT NOT NULL,
    input_sha256 TEXT NOT NULL CHECK (length(input_sha256) = 64),
    record_ids_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE (algorithm_version, input_sha256)
);

CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    initial_version_id INTEGER NOT NULL REFERENCES link_versions(version_id),
    latest_version_id INTEGER NOT NULL REFERENCES link_versions(version_id),
    status TEXT NOT NULL CHECK (status IN ('active', 'split')),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    split_at TEXT,
    split_by TEXT REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS incident_members (
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    sighting_id TEXT NOT NULL REFERENCES sightings(sighting_id),
    link_version_id INTEGER NOT NULL REFERENCES link_versions(version_id),
    confirmed_by TEXT NOT NULL REFERENCES users(user_id),
    confirmed_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    PRIMARY KEY (incident_id, sighting_id)
);

-- 历次决定独立保留：确认与每次拆回都追加一行，永不修改。
CREATE TABLE IF NOT EXISTS incident_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    kind TEXT NOT NULL CHECK (kind IN ('confirm', 'split')),
    link_version_id INTEGER NOT NULL REFERENCES link_versions(version_id),
    members_before_json TEXT NOT NULL,
    members_after_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    decided_by TEXT NOT NULL REFERENCES users(user_id),
    decided_at TEXT NOT NULL
);

-- 确认幂等：同一版本、同一组成员集合的重复确认稳定返回既有事件。
CREATE TABLE IF NOT EXISTS incident_confirm_keys (
    link_version_id INTEGER NOT NULL REFERENCES link_versions(version_id),
    members_digest TEXT NOT NULL CHECK (length(members_digest) = 64),
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    PRIMARY KEY (link_version_id, members_digest)
);

-- 一条记录在同一时刻至多属于一个存续中的统一事件；
-- 拆回后 active=0，行保留作历史，记录可重新归并。
CREATE UNIQUE INDEX IF NOT EXISTS one_active_incident_per_sighting
ON incident_members(sighting_id)
WHERE active = 1;

CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
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
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "users", "sightings", "sighting_visibility", "link_versions",
    "incidents", "incident_members", "incident_decisions", "incident_confirm_keys",
    "idempotency_keys", "audit_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
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
