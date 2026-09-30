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

    @property
    def db_path(self) -> Path:
        return self.data_dir / "monitor.db"
