"""Storage for user accounts, roles/permissions, sessions and API keys.

Either the local SQLite database (default) or an external MariaDB/MySQL database. In MariaDB mode the
tables for users, roles and permissions are meant to be shared with other applications (global user
and rights management); sessions and API keys stay in monitor_* tables.

Rights model:
    users ──< user_roles >── roles ──< role_permissions (permission strings)

Permissions are free-form strings such as "monitoring.view". "*" grants everything and "app.*"
grants every permission of that application, so other applications can use the same tables with
their own permission names.
"""

import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .db import Database

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_PERMISSION_RE = re.compile(r"^(\*|[A-Za-z0-9_\-]+(\.[A-Za-z0-9_\-]+)*(\.\*)?)$")
REQUIRED_USER_COLUMNS = ("id", "username", "password_hash")

# Permissions this application checks. Other applications may define their own.
PERMISSIONS = {
    "*": "Alles (Superadmin, alle Anwendungen)",
    "users.manage": "Benutzer, Rollen und Rechte verwalten (global)",
    "monitoring.view": "Monitoring: ansehen",
    "monitoring.edit": "Monitoring: Monitore und Benachrichtigungen bearbeiten",
}

DEFAULT_ROLES = (
    ("Administrator", "Vollzugriff auf alle Anwendungen", ("*",)),
    ("Monitoring-Bearbeiter", "Monitore und Benachrichtigungen verwalten", ("monitoring.view", "monitoring.edit")),
    ("Monitoring-Betrachter", "Nur lesender Zugriff auf das Monitoring", ("monitoring.view",)),
)
ADMIN_ROLE = "Administrator"


class UserStoreError(RuntimeError):
    """The user database is unreachable or misconfigured."""


class UserConflict(ValueError):
    """A write was rejected, e.g. duplicate name or a constraint of the shared table."""


def is_valid_permission(value: str) -> bool:
    return bool(_PERMISSION_RE.match(value)) and len(value) <= 100


def has_permission(granted: set[str] | frozenset[str], needed: str) -> bool:
    if "*" in granted or needed in granted:
        return True
    return any(p.endswith(".*") and needed.startswith(p[:-1]) for p in granted)


class SqlUserStore:
    """Backend-independent logic. SQL uses %s placeholders and backtick-quoted identifiers."""

    table = "users"

    # -- backend primitives
    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    def _write(self, sql: str, params: tuple = ()) -> tuple[int, int]:
        """Execute a write; returns (lastrowid, rowcount)."""
        raise NotImplementedError

    @contextmanager
    def transaction(self) -> Iterator[None]:
        raise NotImplementedError
        yield

    def _insert_user_sql(self) -> str:
        return f"INSERT INTO `{self.table}` (username, password_hash) VALUES (%s, %s)"

    # -- setup
    def _seed_default_roles(self) -> None:
        with self.transaction():
            if self._one("SELECT COUNT(*) AS n FROM roles")["n"] > 0:
                return
            for name, description, permissions in DEFAULT_ROLES:
                role_id, _ = self._write("INSERT INTO roles (name, description) VALUES (%s, %s)", (name, description))
                for permission in permissions:
                    self._write(
                        "INSERT INTO role_permissions (role_id, permission) VALUES (%s, %s)", (role_id, permission)
                    )

    # -- users
    def count_users(self) -> int:
        return self._one(f"SELECT COUNT(*) AS n FROM `{self.table}`")["n"]

    def get_user_by_name(self, username: str) -> dict[str, Any] | None:
        return self._one(f"SELECT id, username, password_hash FROM `{self.table}` WHERE username = %s", (username,))

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        return self._one(f"SELECT id, username, password_hash FROM `{self.table}` WHERE id = %s", (user_id,))

    def list_users(self) -> list[dict[str, Any]]:
        users = self._all(f"SELECT id, username FROM `{self.table}` ORDER BY username")
        links: dict[int, list[int]] = {}
        for row in self._all("SELECT user_id, role_id FROM user_roles"):
            links.setdefault(row["user_id"], []).append(row["role_id"])
        for user in users:
            user["role_ids"] = sorted(links.get(user["id"], []))
        return users

    def create_first_user(self, username: str, password_hash: str) -> int | None:
        """Create the initial administrator, only while no user exists."""
        with self.transaction():
            if self.count_users() > 0:
                return None
            user_id = self.create_user(username, password_hash)
            admin = self._one("SELECT id FROM roles WHERE name = %s", (ADMIN_ROLE,))
            if admin:
                self._write("INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s)", (user_id, admin["id"]))
            return user_id

    def create_user(self, username: str, password_hash: str) -> int:
        if self.get_user_by_name(username):
            raise UserConflict(f"Benutzer {username!r} existiert bereits")
        return self._write(self._insert_user_sql(), (username, password_hash))[0]

    def rename_user(self, user_id: int, username: str) -> None:
        existing = self.get_user_by_name(username)
        if existing and existing["id"] != user_id:
            raise UserConflict(f"Benutzer {username!r} existiert bereits")
        self._write(f"UPDATE `{self.table}` SET username = %s WHERE id = %s", (username, user_id))

    def set_password(self, user_id: int, password_hash: str) -> None:
        self._write(f"UPDATE `{self.table}` SET password_hash = %s WHERE id = %s", (password_hash, user_id))

    def delete_user(self, user_id: int) -> bool:
        with self.transaction():
            self._write("DELETE FROM user_roles WHERE user_id = %s", (user_id,))
            self._write("DELETE FROM monitor_api_keys WHERE user_id = %s", (user_id,))
            self._delete_user_sessions(user_id)
            return self._write(f"DELETE FROM `{self.table}` WHERE id = %s", (user_id,))[1] > 0

    def _delete_user_sessions(self, user_id: int) -> None:
        raise NotImplementedError

    def set_user_roles(self, user_id: int, role_ids: list[int]) -> None:
        with self.transaction():
            self._write("DELETE FROM user_roles WHERE user_id = %s", (user_id,))
            for role_id in sorted(set(role_ids)):
                self._write("INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s)", (user_id, role_id))

    def permissions_for(self, user_id: int) -> frozenset[str]:
        rows = self._all(
            """
            SELECT DISTINCT rp.permission FROM user_roles ur
            JOIN role_permissions rp ON rp.role_id = ur.role_id
            WHERE ur.user_id = %s
            """,
            (user_id,),
        )
        return frozenset(row["permission"] for row in rows)

    def count_admins(self) -> int:
        """Users who can manage users and rights (guards against locking everyone out)."""
        row = self._one(
            """
            SELECT COUNT(DISTINCT ur.user_id) AS n FROM user_roles ur
            JOIN role_permissions rp ON rp.role_id = ur.role_id
            WHERE rp.permission IN ('*', 'users.*', 'users.manage')
            """
        )
        return row["n"]

    # -- roles
    def list_roles(self) -> list[dict[str, Any]]:
        roles = self._all("SELECT id, name, description FROM roles ORDER BY name")
        permissions: dict[int, list[str]] = {}
        for row in self._all("SELECT role_id, permission FROM role_permissions ORDER BY permission"):
            permissions.setdefault(row["role_id"], []).append(row["permission"])
        counts = {
            row["role_id"]: row["n"]
            for row in self._all("SELECT role_id, COUNT(*) AS n FROM user_roles GROUP BY role_id")
        }
        for role in roles:
            role["permissions"] = permissions.get(role["id"], [])
            role["user_count"] = counts.get(role["id"], 0)
        return roles

    def get_role(self, role_id: int) -> dict[str, Any] | None:
        return next((r for r in self.list_roles() if r["id"] == role_id), None)

    def save_role(self, role_id: int | None, name: str, description: str, permissions: list[str]) -> int:
        with self.transaction():
            clash = self._one("SELECT id FROM roles WHERE name = %s", (name,))
            if clash and clash["id"] != role_id:
                raise UserConflict(f"Rolle {name!r} existiert bereits")
            if role_id is None:
                role_id = self._write("INSERT INTO roles (name, description) VALUES (%s, %s)", (name, description))[0]
            else:
                self._write("UPDATE roles SET name = %s, description = %s WHERE id = %s", (name, description, role_id))
                self._write("DELETE FROM role_permissions WHERE role_id = %s", (role_id,))
            for permission in sorted(set(permissions)):
                self._write("INSERT INTO role_permissions (role_id, permission) VALUES (%s, %s)", (role_id, permission))
            return role_id

    def delete_role(self, role_id: int) -> bool:
        with self.transaction():
            self._write("DELETE FROM user_roles WHERE role_id = %s", (role_id,))
            self._write("DELETE FROM role_permissions WHERE role_id = %s", (role_id,))
            return self._write("DELETE FROM roles WHERE id = %s", (role_id,))[1] > 0

    # -- API keys (owned by a user, inherit that user's permissions)
    def add_api_key(self, name: str, key_hash: str, prefix: str, user_id: int | None) -> int:
        return self._write(
            "INSERT INTO monitor_api_keys (name, key_hash, prefix, created_at, user_id) VALUES (%s, %s, %s, %s, %s)",
            (name, key_hash, prefix, time.time(), user_id),
        )[0]

    def find_api_key(self, key_hash: str) -> dict[str, Any] | None:
        return self._one("SELECT id, name, user_id FROM monitor_api_keys WHERE key_hash = %s", (key_hash,))

    def touch_api_key(self, key_id: int, now: float) -> None:
        self._write("UPDATE monitor_api_keys SET last_used_at = %s WHERE id = %s", (now, key_id))

    def list_api_keys(self, user_id: int | None = None) -> list[dict[str, Any]]:
        sql = f"""
            SELECT k.id, k.name, k.prefix, k.created_at, k.last_used_at, k.user_id, u.username
            FROM monitor_api_keys k LEFT JOIN `{self.table}` u ON u.id = k.user_id
        """
        if user_id is None:
            return self._all(sql + " ORDER BY k.created_at DESC")
        return self._all(sql + " WHERE k.user_id = %s ORDER BY k.created_at DESC", (user_id,))

    def get_api_key(self, key_id: int) -> dict[str, Any] | None:
        return self._one("SELECT id, user_id FROM monitor_api_keys WHERE id = %s", (key_id,))

    def delete_api_key(self, key_id: int) -> bool:
        return self._write("DELETE FROM monitor_api_keys WHERE id = %s", (key_id,))[1] > 0


class SqliteUserStore(SqlUserStore):
    """Everything in the local monitoring database (tables users/sessions/api_keys + role tables)."""

    def __init__(self, db: Database):
        self.db = db
        self._seed_default_roles()
        # Before roles existed every local account was an administrator: keep it that way on upgrade.
        with self.transaction():
            if self.count_admins() == 0 and self.count_users() > 0:
                admin = self._one("SELECT id FROM roles WHERE name = %s", (ADMIN_ROLE,))
                if admin:
                    for user in self._all("SELECT id FROM users"):
                        self._write("INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s)", (user["id"], admin["id"]))

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return self.db.query(self._sql(sql), params)

    def _write(self, sql: str, params: tuple = ()) -> tuple[int, int]:
        try:
            cursor = self.db.execute(self._sql(sql), params)
        except sqlite3.IntegrityError as exc:
            raise UserConflict(f"Von der Datenbank abgelehnt: {exc}") from exc
        return cursor.lastrowid, cursor.rowcount

    @staticmethod
    def _sql(sql: str) -> str:
        return sql.replace("%s", "?").replace("monitor_api_keys", "api_keys")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.db.transaction():
            yield

    def _insert_user_sql(self) -> str:
        return f"INSERT INTO `{self.table}` (username, password_hash, created_at) VALUES (%s, %s, (julianday('now') - 2440587.5) * 86400.0)"

    def _delete_user_sessions(self, user_id: int) -> None:
        self._write("DELETE FROM sessions WHERE user_id = %s", (user_id,))

    def add_session(self, token_hash: str, user_id: int, expires_at: float) -> None:
        self._write(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (%s, %s, %s, %s)",
            (token_hash, user_id, time.time(), expires_at),
        )

    def session_user(self, token_hash: str, now: float) -> dict[str, Any] | None:
        return self._one(
            """
            SELECT u.id, u.username FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = %s AND s.expires_at > %s
            """,
            (token_hash, now),
        )

    def delete_session(self, token_hash: str) -> None:
        self._write("DELETE FROM sessions WHERE token_hash = %s", (token_hash,))

    def purge_sessions(self, now: float) -> None:
        self._write("DELETE FROM sessions WHERE expires_at < %s", (now,))


class MariaDBUserStore(SqlUserStore):
    """Users, roles and permissions in (possibly shared) MariaDB/MySQL tables.

    Existing tables are used as they are if they have the required columns, otherwise they are created.
    """

    REQUIRED_COLUMNS = {
        "roles": ("id", "name", "description"),
        "role_permissions": ("role_id", "permission"),
        "user_roles": ("user_id", "role_id"),
        "monitor_api_keys": ("id", "name", "key_hash", "prefix", "created_at", "last_used_at", "user_id"),
    }

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
        self._in_transaction = False
        self._init_schema()
        self._seed_default_roles()

    # ------------------------------------------------------------------ connection

    def _connection(self):
        if self._conn is not None and not self._in_transaction:
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

    def _cursor(self, sql: str, params: tuple):
        with self._lock:
            try:
                cursor = self._connection().cursor()
                cursor.execute(sql, params)
                return cursor
            except self._pymysql.IntegrityError as exc:
                raise UserConflict(f"Von der Datenbank abgelehnt: {exc.args[-1]}") from exc
            except self._pymysql.OperationalError as exc:
                if self._in_transaction:
                    raise UserStoreError(str(exc)) from exc
                # Connection dropped between ping and query: reconnect once.
                self._conn = None
                cursor = self._connection().cursor()
                cursor.execute(sql, params)
                return cursor

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._cursor(sql, params).fetchall())

    def _write(self, sql: str, params: tuple = ()) -> tuple[int, int]:
        with self._lock:
            cursor = self._cursor(sql, params)
            return cursor.lastrowid, cursor.rowcount

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            if self._in_transaction:  # nested: join the outer transaction
                yield
                return
            conn = self._connection()
            conn.begin()
            self._in_transaction = True
            try:
                yield
            except BaseException:
                self._in_transaction = False
                try:
                    conn.rollback()
                except self._pymysql.MySQLError:
                    self._conn = None
                raise
            else:
                self._in_transaction = False
                conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------ schema

    def _columns(self, table: str) -> set[str]:
        return {
            row["COLUMN_NAME"].lower()
            for row in self._all(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                (table,),
            )
        }

    def _check_columns(self, table: str, required: tuple[str, ...], hint: str = "") -> bool:
        """True if the table exists (with all required columns); raises if it exists without them."""
        columns = self._columns(table)
        if not columns:
            return False
        missing = [c for c in required if c not in columns]
        if missing:
            raise UserStoreError(
                f"Die vorhandene Tabelle {table!r} hat nicht die nötigen Spalten ({', '.join(missing)} fehlen). "
                f"Benötigt werden: {', '.join(required)}.{(' ' + hint) if hint else ''}"
            )
        return True

    def _init_schema(self) -> None:
        if not self._check_columns(
            self.table, REQUIRED_USER_COLUMNS, "Mit MONITOR_USER_DB_TABLE kann eine andere Tabelle gewählt werden."
        ):
            self._write(
                f"""
                CREATE TABLE `{self.table}` (
                    id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    username VARCHAR(64) NOT NULL UNIQUE,
                    password_hash VARCHAR(255) NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
        if not self._check_columns("roles", self.REQUIRED_COLUMNS["roles"]):
            self._write(
                """
                CREATE TABLE roles (
                    id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(100) NOT NULL UNIQUE,
                    description VARCHAR(255) NOT NULL DEFAULT ''
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
        if not self._check_columns("role_permissions", self.REQUIRED_COLUMNS["role_permissions"]):
            self._write(
                """
                CREATE TABLE role_permissions (
                    role_id INT UNSIGNED NOT NULL,
                    permission VARCHAR(100) NOT NULL,
                    PRIMARY KEY (role_id, permission)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
        if not self._check_columns("user_roles", self.REQUIRED_COLUMNS["user_roles"]):
            self._write(
                """
                CREATE TABLE user_roles (
                    user_id INT UNSIGNED NOT NULL,
                    role_id INT UNSIGNED NOT NULL,
                    PRIMARY KEY (user_id, role_id),
                    KEY idx_user_roles_role (role_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
        self._write(
            """
            CREATE TABLE IF NOT EXISTS monitor_sessions (
                token_hash CHAR(64) NOT NULL PRIMARY KEY,
                user_id INT UNSIGNED NOT NULL,
                created_at DOUBLE NOT NULL,
                expires_at DOUBLE NOT NULL,
                KEY idx_monitor_sessions_expires (expires_at),
                KEY idx_monitor_sessions_user (user_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        self._write(
            """
            CREATE TABLE IF NOT EXISTS monitor_api_keys (
                id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                key_hash CHAR(64) NOT NULL UNIQUE,
                prefix VARCHAR(16) NOT NULL,
                created_at DOUBLE NOT NULL,
                last_used_at DOUBLE NULL,
                user_id INT UNSIGNED NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        # Tables of the previous version had no owner column for API keys.
        if "user_id" not in self._columns("monitor_api_keys"):
            self._write("ALTER TABLE monitor_api_keys ADD COLUMN user_id INT UNSIGNED NULL")

    # ------------------------------------------------------------------ sessions

    def _delete_user_sessions(self, user_id: int) -> None:
        self._write("DELETE FROM monitor_sessions WHERE user_id = %s", (user_id,))

    def add_session(self, token_hash: str, user_id: int, expires_at: float) -> None:
        self._write(
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
        self._write("DELETE FROM monitor_sessions WHERE token_hash = %s", (token_hash,))

    def purge_sessions(self, now: float) -> None:
        self._write("DELETE FROM monitor_sessions WHERE expires_at < %s", (now,))


UserStore = SqlUserStore
