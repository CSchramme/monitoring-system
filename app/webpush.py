"""Web Push (notifications to the installed web app / browser on phones and desktops).

Keys (VAPID) are generated on first start and kept in the local database. Each browser that enables
push stores a subscription; the "webpush" notification channel sends to all subscriptions of users
who may view the monitoring.
"""

import asyncio
import base64
import json
import logging
import time
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid02
from pywebpush import WebPushException, webpush

from .db import Database
from .userstore import UserStore, has_permission

log = logging.getLogger("monitoring.webpush")


class WebPush:
    def __init__(self, db: Database, subject: str, users: UserStore | None = None):
        self.db = db
        self.subject = subject
        self.users = users
        pem = self._load_or_create_key()
        self._vapid = Vapid02.from_pem(pem.encode())
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.public_key = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    def _load_or_create_key(self) -> str:
        with self.db.transaction():
            row = self.db.one("SELECT value FROM settings WHERE key = 'vapid_private_pem'")
            if row:
                return row["value"]
            pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            ).decode()
            self.db.execute("INSERT INTO settings (key, value) VALUES ('vapid_private_pem', ?)", (pem,))
            return pem

    # ------------------------------------------------------------------ subscriptions

    @staticmethod
    def validate(endpoint: str, p256dh: str, auth: str) -> None:
        parts = urlsplit(endpoint)
        if parts.scheme != "https" or not parts.netloc:
            raise ValueError("Ungültiger Push-Endpunkt")
        if not p256dh or not auth or len(p256dh) > 200 or len(auth) > 100:
            raise ValueError("Ungültige Schlüssel")

    def subscribe(self, user_id: int | None, endpoint: str, p256dh: str, auth: str, user_agent: str = "") -> None:
        self.validate(endpoint, p256dh, auth)
        with self.db.transaction():
            self.db.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
            self.db.execute(
                """
                INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, user_agent, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, endpoint, p256dh, auth, user_agent[:300], time.time()),
            )

    def unsubscribe(self, endpoint: str) -> None:
        self.db.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))

    def subscriptions(self, user_id: int | None = None) -> list[dict[str, Any]]:
        if user_id is None:
            return self.db.query("SELECT * FROM push_subscriptions")
        return self.db.query("SELECT * FROM push_subscriptions WHERE user_id = ?", (user_id,))

    # ------------------------------------------------------------------ sending

    def _recipients(self, user_id: int | None) -> list[dict[str, Any]]:
        subs = self.subscriptions(user_id)
        if user_id is not None or self.users is None:
            return subs
        allowed: dict[int, bool] = {}
        result = []
        for sub in subs:
            uid = sub["user_id"]
            if uid not in allowed:
                allowed[uid] = uid is not None and has_permission(self.users.permissions_for(uid), "monitoring.view")
            if allowed[uid]:
                result.append(sub)
        return result

    def _send_one(self, sub: dict[str, Any], data: str) -> str | None:
        try:
            webpush(
                subscription_info={"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
                data=data,
                vapid_private_key=self._vapid,
                vapid_claims={"sub": self.subject},
                ttl=24 * 3600,
                timeout=15,
            )
        except WebPushException as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (404, 410):
                # Subscription expired or was revoked in the browser.
                self.unsubscribe(sub["endpoint"])
                return None
            error = f"HTTP {status}: {exc}" if status else str(exc)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
        else:
            self.db.execute("UPDATE push_subscriptions SET last_error = '' WHERE id = ?", (sub["id"],))
            return None
        self.db.execute("UPDATE push_subscriptions SET last_error = ? WHERE id = ?", (error[:500], sub["id"]))
        return error

    async def send(self, message: dict[str, Any], user_id: int | None = None) -> tuple[int, list[str]]:
        """Send to all eligible subscriptions (or those of one user). Returns (delivered, errors)."""
        subs = self._recipients(user_id)
        data = json.dumps(message)
        results = await asyncio.gather(*(asyncio.to_thread(self._send_one, sub, data) for sub in subs))
        errors = [r for r in results if r]
        return len(subs) - len(errors), errors


def push_message(payload: dict[str, Any]) -> dict[str, Any]:
    """Compact notification payload understood by the service worker (static/sw.js)."""
    monitor = payload.get("monitor") or {}
    return {
        "title": payload["title"],
        "body": payload["text"][:500],
        "url": f"/#/monitor/{monitor['id']}" if monitor.get("id") else "/",
        "tag": f"monitor-{monitor['id']}" if monitor.get("id") else "test",
        "status": payload["status"],
    }
