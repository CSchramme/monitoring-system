"""Runtime settings, read from environment variables."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("MONITOR_DATA_DIR", "./data")))
    # Base URL under which the web UI is reachable from outside, e.g. https://monitor.example.com.
    # Used for links in notifications. The UI falls back to the browser origin if empty.
    public_url: str = field(default_factory=lambda: os.environ.get("MONITOR_PUBLIC_URL", "").rstrip("/"))
    retention_days: int = field(default_factory=lambda: int(os.environ.get("MONITOR_RETENTION_DAYS", "30")))
    max_concurrent_checks: int = field(
        default_factory=lambda: int(os.environ.get("MONITOR_MAX_CONCURRENT_CHECKS", "50"))
    )
    # Set to true when the UI is served over HTTPS so the session cookie gets the Secure flag.
    secure_cookies: bool = field(default_factory=lambda: _bool("MONITOR_SECURE_COOKIES", False))
    session_days: int = field(default_factory=lambda: int(os.environ.get("MONITOR_SESSION_DAYS", "30")))

    # Optional external MariaDB/MySQL database for user accounts (shared with other applications).
    # If MONITOR_USER_DB_HOST is empty, users are stored in the local SQLite database.
    user_db_host: str = field(default_factory=lambda: os.environ.get("MONITOR_USER_DB_HOST", ""))
    user_db_port: int = field(default_factory=lambda: int(os.environ.get("MONITOR_USER_DB_PORT", "3306")))
    user_db_name: str = field(default_factory=lambda: os.environ.get("MONITOR_USER_DB_NAME", ""))
    user_db_user: str = field(default_factory=lambda: os.environ.get("MONITOR_USER_DB_USER", ""))
    user_db_password: str = field(default_factory=lambda: os.environ.get("MONITOR_USER_DB_PASSWORD", ""), repr=False)
    user_db_table: str = field(default_factory=lambda: os.environ.get("MONITOR_USER_DB_TABLE", "users"))

    # Contact for Web Push services (VAPID "sub" claim): mailto: address or https URL.
    vapid_subject: str = field(default_factory=lambda: os.environ.get("MONITOR_VAPID_SUBJECT", ""))

    @property
    def push_subject(self) -> str:
        if self.vapid_subject:
            return self.vapid_subject
        if self.public_url.startswith("https://"):
            return self.public_url
        return "mailto:monitoring@example.com"

    @property
    def user_db_label(self) -> str:
        if not self.user_db_host:
            return "SQLite (lokal)"
        return f"MariaDB {self.user_db_host}:{self.user_db_port}/{self.user_db_name} (Tabelle {self.user_db_table})"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "monitor.db"
