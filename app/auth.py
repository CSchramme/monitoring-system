"""Password hashing, browser sessions and API keys."""

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from .db import Database

SESSION_COOKIE = "monitor_session"
API_KEY_PREFIX = "mon_"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(digest.hex(), digest_hex)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Database, user_id: int, days: int) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    db.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token_hash(token), user_id, now, now + days * 86400),
    )
    return token


def delete_session(db: Database, token: str) -> None:
    db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))


def create_api_key(db: Database, name: str) -> tuple[int, str]:
    key = API_KEY_PREFIX + secrets.token_urlsafe(32)
    cursor = db.execute(
        "INSERT INTO api_keys (name, key_hash, prefix, created_at) VALUES (?, ?, ?, ?)",
        (name, token_hash(key), key[:10], time.time()),
    )
    return cursor.lastrowid, key


def new_push_token() -> str:
    return secrets.token_urlsafe(18)


def authenticate(request: Request) -> dict:
    """FastAPI dependency: accepts a session cookie or 'Authorization: Bearer <api key>'."""
    db: Database = request.app.state.db
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        key = header[7:].strip()
        row = db.one("SELECT id, name FROM api_keys WHERE key_hash = ?", (token_hash(key),))
        if row:
            db.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (time.time(), row["id"]))
            return {"kind": "api_key", "id": row["id"], "name": row["name"]}
        raise HTTPException(401, "Ungültiger API-Key")

    token = request.cookies.get(SESSION_COOKIE)
    if token:
        row = db.one(
            """
            SELECT u.id, u.username FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.expires_at > ?
            """,
            (token_hash(token), time.time()),
        )
        if row:
            return {"kind": "user", "id": row["id"], "name": row["username"]}
    raise HTTPException(401, "Nicht angemeldet")


class LoginThrottle:
    """Simple in-memory limit on failed logins per client address."""

    def __init__(self, max_failures: int = 10, window: float = 900):
        self.max_failures = max_failures
        self.window = window
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        entries = self._failures[key]
        while entries and now - entries[0] > self.window:
            entries.popleft()
        return entries

    def check(self, key: str) -> None:
        if len(self._prune(key, time.time())) >= self.max_failures:
            raise HTTPException(429, "Zu viele Fehlversuche. Bitte später erneut versuchen.")

    def fail(self, key: str) -> None:
        self._prune(key, time.time()).append(time.time())

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
