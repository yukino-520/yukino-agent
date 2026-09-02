from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from service_club.storage.sqlite import sqlite_connection

_SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

LEGACY_DATABASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("workflow_runs.sqlite3", ("workflow_runs",)),
    (
        "capability_policy.sqlite3",
        ("capability_policy_denials", "capability_temporary_grants"),
    ),
)


# 作用：把旧版分散账本幂等合并到主 SQLite，并保留源文件备份。
# 参数 data_dir：能力状态、工作区和本地数据所在目录。
# 参数 target：数据合并、写入或迁移的目标位置。
def consolidate_legacy_databases(
    data_dir: str | Path,
    target: str | Path,
) -> dict[str, Any]:
    """Copy legacy split ledgers into the main SQLite database once.

    Source files are deliberately retained as recoverable backups.  The new
    runtime only writes the unified target after this migration.
    """

    data_path = Path(data_dir).resolve()
    target_path = Path(target).resolve()
    report: dict[str, Any] = {
        "target": str(target_path),
        "sources": [],
        "copied_rows": 0,
        "legacy_files_preserved": True,
    }
    with sqlite_connection(target_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS storage_schema_migrations (
                id TEXT PRIMARY KEY,
                applied_at REAL NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        for filename, tables in LEGACY_DATABASES:
            source = (data_path / filename).resolve()
            if source == target_path or not source.is_file():
                continue
            try:
                os.chmod(source, 0o600)
            except OSError:
                pass
            source_report = _copy_source(connection, source, tables)
            report["sources"].append(source_report)
            report["copied_rows"] += int(source_report["copied_rows"])
    return report


# 作用：附加单个旧数据库，复制兼容列并记录每张表的迁移标记。
# 参数 connection：当前事务使用的关系数据库连接。
# 参数 source：待读取、迁移、验证或解析的源数据。
# 参数 tables：允许从旧数据库复制的数据表名称集合。
def _copy_source(connection: Any, source: Path, tables: tuple[str, ...]) -> dict[str, Any]:
    alias = "legacy_source"
    copied_rows = 0
    copied_tables: dict[str, int] = {}
    connection.execute(f"ATTACH DATABASE ? AS {alias}", (str(source),))
    try:
        for table in tables:
            if not _SAFE_IDENTIFIER.fullmatch(table):
                raise ValueError(f"不安全的迁移表名：{table}")
            migration_id = f"consolidate-v1:{source.name}:{table}"
            already_applied = connection.execute(
                "SELECT 1 FROM storage_schema_migrations WHERE id = ?",
                (migration_id,),
            ).fetchone()
            if already_applied:
                copied_tables[table] = 0
                continue
            source_exists = connection.execute(
                f"""
                SELECT 1 FROM {alias}.sqlite_master
                WHERE type = 'table' AND name = ?
                """,
                (table,),
            ).fetchone()
            target_exists = connection.execute(
                """
                SELECT 1 FROM main.sqlite_master
                WHERE type = 'table' AND name = ?
                """,
                (table,),
            ).fetchone()
            if not source_exists or not target_exists:
                continue
            source_columns = _table_columns(connection, alias, table)
            target_columns = _table_columns(connection, "main", table)
            columns = [name for name in target_columns if name in source_columns]
            if not columns:
                continue
            quoted = ", ".join(f'"{name}"' for name in columns)
            before = int(connection.total_changes)
            connection.execute(
                f"""
                INSERT OR IGNORE INTO main."{table}" ({quoted})
                SELECT {quoted} FROM {alias}."{table}"
                """
            )
            copied = int(connection.total_changes) - before
            details = {
                "source": str(source),
                "table": table,
                "copied_rows": copied,
                "source_preserved": True,
            }
            connection.execute(
                """
                INSERT INTO storage_schema_migrations(id, applied_at, details_json)
                VALUES (?, ?, ?)
                """,
                (migration_id, time.time(), json.dumps(details, ensure_ascii=False)),
            )
            copied_tables[table] = copied
            copied_rows += copied
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.execute(f"DETACH DATABASE {alias}")
    return {
        "source": str(source),
        "tables": copied_tables,
        "copied_rows": copied_rows,
        "preserved": True,
    }


# 作用：读取指定附加数据库中一张表的列名。
# 参数 connection：当前事务使用的关系数据库连接。
# 参数 database：SQLite 主库或附加数据库的模式名称。
# 参数 table：要查询、迁移或维护的数据表名称。
def _table_columns(connection: Any, database: str, table: str) -> list[str]:
    return [
        str(row[1])
        for row in connection.execute(
            f'PRAGMA {database}.table_info("{table}")'
        ).fetchall()
    ]
