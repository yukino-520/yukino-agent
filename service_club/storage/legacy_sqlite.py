"""Offline-only adapter for importing pre-PostgreSQL Yukino databases.

Runtime repositories must never import this module.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from service_club.storage.relational import RelationalConnection
from service_club.storage.sqlite import sqlite_connection


class SQLiteRelationalBackend:
    name = "sqlite"
    float_type = "REAL"
    identity_type = "INTEGER PRIMARY KEY AUTOINCREMENT"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.location = str(self.path)

    @contextmanager
    def connect(self, *, immediate: bool = False) -> Iterator[RelationalConnection]:
        with sqlite_connection(self.path) as raw:
            raw.row_factory = sqlite3.Row
            if immediate:
                raw.execute("BEGIN IMMEDIATE")
            yield RelationalConnection(raw, placeholder="?")

    def lock_scope(self, connection: RelationalConnection, scope: str) -> None:
        del connection, scope

    def for_update(self, *, skip_locked: bool = False) -> str:
        del skip_locked
        return ""

    def table_exists(self, connection: RelationalConnection, table: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone() is not None

    def reseed_identity(self, connection: RelationalConnection, table: str, column: str) -> None:
        del connection, table, column
