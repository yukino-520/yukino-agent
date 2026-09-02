from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_CONFIGURATION_LOCK = threading.RLock()
_CONFIGURED_DATABASES: set[tuple[int, str]] = set()


# 作用：读取整数环境变量并钳制在安全范围内。
# 参数 name：要读取并限制的整数环境变量名称。
# 参数 default：配置缺失或解析失败时采用的默认值。
# 参数 minimum：整数配置允许的最小值。
# 参数 maximum：整数配置允许的最大值。
def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


# 作用：打开统一参数的 WAL 事务，成功提交、异常回滚并始终关闭。
# 参数 path：目标文件、数据库或状态存储路径。
@contextmanager
# 作用：执行“sqlite_connection”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
# 参数 path：配置文件、数据文件或工作区路径。
def sqlite_connection(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open a bounded, WAL-backed SQLite transaction and always close it.

    SQLite remains the zero-configuration local backend.  Keeping connection
    policy here prevents each repository from silently choosing different
    locking, durability, and foreign-key behavior.
    """

    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    busy_timeout_ms = _bounded_int(
        "YUKINO_SQLITE_BUSY_TIMEOUT_MS", 10_000, 1_000, 60_000
    )
    connection = sqlite3.connect(
        database_path,
        timeout=busy_timeout_ms / 1000,
    )
    try:
        connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA temp_store = MEMORY")
        connection.execute("PRAGMA cache_size = -4096")
        _ensure_wal(connection, database_path)
        try:
            os.chmod(database_path, 0o600)
        except OSError:
            pass
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


# 作用：为当前进程中的数据库启用并缓存 WAL 配置结果。
# 参数 connection：当前事务使用的关系数据库连接。
# 参数 path：目标文件、数据库或状态存储路径。
def _ensure_wal(connection: sqlite3.Connection, path: Path) -> None:
    key = (os.getpid(), str(path.resolve()))
    with _CONFIGURATION_LOCK:
        current = connection.execute("PRAGMA journal_mode").fetchone()
        current_mode = str(current[0]).lower() if current else ""
        if key in _CONFIGURED_DATABASES and current_mode == "wal":
            return
        row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
        mode = str(row[0]).lower() if row else ""
        if mode != "wal":
            raise sqlite3.OperationalError(
                f"SQLite 数据库无法启用 WAL 模式：{path}（当前 {mode or 'unknown'}）"
            )
        connection.execute("PRAGMA wal_autocheckpoint = 1000")
        _CONFIGURED_DATABASES.add(key)


# 作用：检查 SQLite 的日志模式、外键、表数量和空间占用。
# 参数 path：目标文件、数据库或状态存储路径。
def sqlite_status(path: str | Path) -> dict[str, Any]:
    database_path = Path(path)
    if not database_path.is_file():
        return {
            "ok": False,
            "backend": "sqlite",
            "path": str(database_path),
            "error": "database_missing",
        }
    with sqlite_connection(database_path) as connection:
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        busy_timeout_ms = int(
            connection.execute("PRAGMA busy_timeout").fetchone()[0]
        )
        foreign_keys = bool(
            int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
        )
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = int(
            connection.execute("PRAGMA freelist_count").fetchone()[0]
        )
        table_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            ).fetchone()[0]
        )
    return {
        "ok": journal_mode.lower() == "wal" and foreign_keys,
        "backend": "sqlite",
        "path": str(database_path),
        "journal_mode": journal_mode.lower(),
        "busy_timeout_ms": busy_timeout_ms,
        "foreign_keys": foreign_keys,
        "table_count": table_count,
        "size_bytes": page_size * page_count,
        "free_bytes": page_size * freelist_count,
        "local_mode": True,
    }
