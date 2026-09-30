"""Storage for user accounts, sessions and API keys.

Either the local SQLite database (default) or an external MariaDB/MySQL database, so that the
user table can be shared with other applications.
"""

import re
import threading
import time
from typing import Any, Protocol

from .db import Database

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
REQUIRED_USER_COLUMNS = ("id", "username", "password_hash")


class UserStoreError(RuntimeError):
    pass


class UserStore(Protocol):
    def count_users(self) -> int: ...
    def get_user_by_name(self, username: str) -> dict[str, Any] | None: ...
    def get_user(self, user_id: int) -> dict[str, Any] | None: ...
    def create_first_user(self, username: str, password_hash: str) -> int | None: ...
    def set_password(self, user_id: int, password_hash: str) -> None: ...
    def add_session(self, token_hash: str, user_id: int, expires_at: float) -> None: ...
    def session_user(self, token_hash: str, now: float) -> dict[str, Any] | None: ...
    def delete_session(self, token_hash: str) -> None: ...
    def purge_sessions(self, now: float) -> None: ...
    def add_api_key(self, name: str, key_hash: str, prefix: str) -> int: ...
    def find_api_key(self, key_hash: str) -> dict[str, Any] | None: ...
    def touch_api_key(self, key_id: int, now: float) -> None: ...
    def list_api_keys(self) -> list[dict[str, Any]]: ...
    def delete_api_key(self, key_id: int) -> bool: ...


class SqliteUserStore:
    """Users, sessions and API keys in the monitoring database itself."""

    def __init__(self, db: Database):
        self.db = db

    def count_users(self) -> int:
        return self.db.scalar("SELECT COUNT(*) FROM users")

    def get_user_by_name(self, username: str) -> dict[str, Any] | None:
        return self.db.one("SELECT id, username, password_hash FROM users WHERE username = ?", (username,))

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        return self.db.one("SELECT id, username, password_hash FROM users WHERE id = ?", (user_id,))

    def create_first_user(self, username: str, password_hash: str) -> int | None:
        with self.db.transaction():
            if self.count_users() > 0:
                return None
            return self.db.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, password_hash, time.time()),
            ).lastrowid

    def set_password(self, user_id: int, password_hash: str) -> None:
        self.db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))

    def add_session(self, token_hash: str, user_id: int, expires_at: float) -> None:
        self.db.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token_hash, user_id, time.time(), expires_at),
        )

    def session_user(self, token_hash: str, now: float) -> dict[str, Any] | None:
        return self.db.one(
            """
            SELECT u.id, u.username FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.expires_at > ?
            """,
            (token_hash, now),
        )

    def delete_session(self, token_hash: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def purge_sessions(self, now: float) -> None:
        self.db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))

    def add_api_key(self, name: str, key_hash: str, prefix: str) -> int:
        return self.db.execute(
            "INSERT INTO api_keys (name, key_hash, prefix, created_at) VALUES (?, ?, ?, ?)",
            (name, key_hash, prefix, time.time()),
        ).lastrowid

    def find_api_key(self, key_hash: str) -> dict[str, Any] | None:
        return self.db.one("SELECT id, name FROM api_keys WHERE key_hash = ?", (key_hash,))

    def touch_api_key(self, key_id: int, now: float) -> None:
        self.db.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now, key_id))

    def list_api_keys(self) -> list[dict[str, Any]]:
        return self.db.query("SELECT id, name, prefix, created_at, last_used_at FROM api_keys ORDER BY created_at DESC")

    def delete_api_key(self, key_id: int) -> bool:
        return self.db.execute("DELETE FROM api_keys WHERE id = ?", (key_id,)).rowcount > 0


class MariaDBUserStore:
    """Users in a (possibly shared) MariaDB/MySQL table; sessions and API keys in monitor_* tables.

    The user table only needs the columns id, username and password_hash. If it already exists
    (e.g. used by other applications) it is used as is; otherwise it is created.
    """

    def __init__(self, host: str, port: int, user: str, password: str, database: str, user_table: str = "users"):
        import pymysql  # imported lazily: only needed in MariaDB mode

        if not _IDENTIFIER_RE.match(user_table):
            raise UserStoreError(f"Ungültiger Tabellenname: {user_table!r}")
        self._pymysql = pymysql
        self._params = dict(
            host=host, port=port, user=user, password=password, database=database,
            charset="utf8mb4", autocommit=True, connect_timeout=10, read_timeout=30, write_timeout=30,
            cursorclass=pymysql.cursors.DictCursor,
        )
        self.table = user_table
        self._conn = None
        self._lock = threading.RLock()
        self._init_schema()

    # ------------------------------------------------------------------ connection

    def _connection(self):
        if self._conn is not None:
            try:
                self._conn.ping()
            except self._pymysql.MySQLError:
                self._conn = None
        if self._conn is None:
            try:
                self._conn = self._pymysql.connect(**self._params)
            except self._pymysql.MySQLError as exc:
                raise UserStoreError(
                    f"Keine Verbindung zur Benutzer-Datenbank {self._params['host']}:{self._params['port']}: {exc}"
                ) from exc
        return self._conn

    def _run(self, sql: str, params: tuple = ()) -> Any:
        with self._lock:
            try:
                cursor = self._connection().cursor()
                cursor.execute(sql, params)
                return cursor
            except self._pymysql.OperationalError:
                # Connection dropped between ping and query: reconnect once.
                self._conn = None
                cursor = self._connection().cursor()
                cursor.execute(sql, params)
                return cursor

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            return self._run(sql, params).fetchone()

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._run(sql, params).fetchall())

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------ schema

    def _init_schema(self) -> None:
        columns = {
            row["COLUMN_NAME"].lower()
            for row in self._all(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                (self.table,),
            )
        }
        if columns:
            missing = [c for c in REQUIRED_USER_COLUMNS if c not in columns]
            if missing:
                raise UserStoreError(
                    f"Die vorhandene Tabelle {self.table!r} hat nicht die nötigen Spalten "
                    f"({', '.join(missing)} fehlen). Benötigt werden: {', '.join(REQUIRED_USER_COLUMNS)}. "
                    "Mit MONITOR_USER_DB_TABLE kann eine andere Tabelle gewählt werden."
                )
        else:
            self._run(
                f"""
                CREATE TABLE IF NOT EXISTS `{self.table}` (
                    id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    username VARCHAR(64) NOT NULL UNIQUE,
                    password_hash VARCHAR(255) NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
        self._run(
            """
            CREATE TABLE IF NOT EXISTS monitor_sessions (
                token_hash CHAR(64) NOT NULL PRIMARY KEY,
                user_id INT UNSIGNED NOT NULL,
                created_at DOUBLE NOT NULL,
                expires_at DOUBLE NOT NULL,
                KEY idx_monitor_sessions_expires (expires_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        self._run(
            """
            CREATE TABLE IF NOT EXISTS monitor_api_keys (
                id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                key_hash CHAR(64) NOT NULL UNIQUE,
                prefix VARCHAR(16) NOT NULL,
                created_at DOUBLE NOT NULL,
                last_used_at DOUBLE NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )

    # ------------------------------------------------------------------ users

    def count_users(self) -> int:
        return self._one(f"SELECT COUNT(*) AS n FROM `{self.table}`")["n"]

    def get_user_by_name(self, username: str) -> dict[str, Any] | None:
        return self._one(f"SELECT id, username, password_hash FROM `{self.table}` WHERE username = %s", (username,))

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        return self._one(f"SELECT id, username, password_hash FROM `{self.table}` WHERE id = %s", (user_id,))

    def create_first_user(self, username: str, password_hash: str) -> int | None:
        with self._lock:
            # Insert only while the table is empty; atomic without needing table locks.
            cursor = self._run(
                f"""
                INSERT INTO `{self.table}` (username, password_hash)
                SELECT %s, %s FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM `{self.table}`)
                """,
                (username, password_hash),
            )
            return cursor.lastrowid if cursor.rowcount == 1 else None

    def set_password(self, user_id: int, password_hash: str) -> None:
        self._run(f"UPDATE `{self.table}` SET password_hash = %s WHERE id = %s", (password_hash, user_id))

    # ------------------------------------------------------------------ sessions

    def add_session(self, token_hash: str, user_id: int, expires_at: float) -> None:
        self._run(
            "INSERT INTO monitor_sessions (token_hash, user_id, created_at, expires_at) VALUES (%s, %s, %s, %s)",
            (token_hash, user_id, time.time(), expires_at),
        )

    def session_user(self, token_hash: str, now: float) -> dict[str, Any] | None:
        return self._one(
            f"""
            SELECT u.id, u.username FROM monitor_sessions s JOIN `{self.table}` u ON u.id = s.user_id
            WHERE s.token_hash = %s AND s.expires_at > %s
            """,
            (token_hash, now),
        )

    def delete_session(self, token_hash: str) -> None:
        self._run("DELETE FROM monitor_sessions WHERE token_hash = %s", (token_hash,))

    def purge_sessions(self, now: float) -> None:
        self._run("DELETE FROM monitor_sessions WHERE expires_at < %s", (now,))

    # ------------------------------------------------------------------ API keys

    def add_api_key(self, name: str, key_hash: str, prefix: str) -> int:
        return self._run(
            "INSERT INTO monitor_api_keys (name, key_hash, prefix, created_at) VALUES (%s, %s, %s, %s)",
            (name, key_hash, prefix, time.time()),
        ).lastrowid

    def find_api_key(self, key_hash: str) -> dict[str, Any] | None:
        return self._one("SELECT id, name FROM monitor_api_keys WHERE key_hash = %s", (key_hash,))

    def touch_api_key(self, key_id: int, now: float) -> None:
        self._run("UPDATE monitor_api_keys SET last_used_at = %s WHERE id = %s", (now, key_id))

    def list_api_keys(self) -> list[dict[str, Any]]:
        return self._all(
            "SELECT id, name, prefix, created_at, last_used_at FROM monitor_api_keys ORDER BY created_at DESC"
        )

    def delete_api_key(self, key_id: int) -> bool:
        return self._run("DELETE FROM monitor_api_keys WHERE id = %s", (key_id,)).rowcount > 0
