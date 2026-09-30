"""Notification channels: webhook, Discord, Slack, Telegram, ntfy, email."""

import asyncio
import logging
import smtplib
import ssl
import time
from email.message import EmailMessage
from typing import Any

import httpx

from .db import Database
from .service import Transition

log = logging.getLogger("monitoring.notifier")

CHANNEL_TYPES = ("webpush", "webhook", "discord", "slack", "telegram", "ntfy", "email")

# Required config keys per channel type (used for validation in the API).
CHANNEL_REQUIRED: dict[str, tuple[str, ...]] = {
    "webpush": (),
    "webhook": ("url",),
    "discord": ("url",),
    "slack": ("url",),
    "telegram": ("bot_token", "chat_id"),
    "ntfy": ("topic",),
    "email": ("host", "sender", "recipients"),
}

STATUS_LABEL = {"up": "OK", "down": "STÖRUNG", "pending": "Ausstehend", "paused": "Pausiert"}


class NotificationError(Exception):
    pass


class Notifier:
    def __init__(self, db: Database, public_url: str = ""):
        self.db = db
        self.public_url = public_url
        self._tasks: set[asyncio.Task] = set()
        self.webpush = None  # set by the app (app.webpush.WebPush)

    def dispatch(self, transition: Transition | None) -> None:
        """Fire-and-forget delivery for a status change. Must be called from the event loop."""
        if transition is None or not transition.notify:
            return
        task = asyncio.get_running_loop().create_task(self.deliver(transition))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_idle(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def deliver(self, transition: Transition) -> None:
        channels = self.db.query(
            """
            SELECT c.* FROM channels c
            JOIN monitor_channels mc ON mc.channel_id = c.id
            WHERE mc.monitor_id = ? AND c.enabled = 1
            """,
            (transition.monitor["id"],),
        )
        if not channels:
            return
        payload = self.build_payload(transition)
        await asyncio.gather(*(self.send_and_record(channel, payload) for channel in channels))

    async def send_and_record(self, channel: dict[str, Any], payload: dict[str, Any]) -> str | None:
        """Send and store the outcome on the channel. Returns an error message or None."""
        error = None
        try:
            await self.send(channel, payload)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            log.warning("Notification via channel %s (%s) failed: %s", channel["id"], channel["type"], error)
        self.db.execute(
            "UPDATE channels SET last_sent_at = ?, last_error = ? WHERE id = ?",
            (time.time(), (error or "")[:500], channel["id"]),
        )
        return error

    def build_payload(self, transition: Transition) -> dict[str, Any]:
        monitor = transition.monitor
        status = transition.current
        icon = "✅" if status == "up" else "🔴"
        title = f"{icon} {monitor['name']}: {STATUS_LABEL.get(status, status)}"
        link = f"{self.public_url}/#/monitor/{monitor['id']}" if self.public_url else ""
        lines = [transition.message or "", f"Ziel: {monitor['target']}" if monitor["target"] else ""]
        if link:
            lines.append(link)
        return {
            "event": "status_change",
            "title": title,
            "text": "\n".join(line for line in lines if line),
            "status": status,
            "previous_status": transition.previous,
            "message": transition.message,
            "timestamp": transition.ts,
            "url": link,
            "monitor": {
                "id": monitor["id"],
                "name": monitor["name"],
                "type": monitor["type"],
                "target": monitor["target"],
                "group": monitor["group_name"],
            },
        }

    @staticmethod
    def test_payload() -> dict[str, Any]:
        return {
            "event": "test",
            "title": "🔔 Testnachricht vom Monitoring",
            "text": "Wenn du das liest, funktioniert der Benachrichtigungskanal.",
            "status": "test",
            "previous_status": "",
            "message": "Testnachricht",
            "timestamp": time.time(),
            "url": "",
            "monitor": None,
        }

    async def send(self, channel: dict[str, Any], payload: dict[str, Any]) -> None:
        cfg = channel["config"]
        kind = channel["type"]
        if kind == "email":
            await asyncio.to_thread(_send_email, cfg, payload)
            return
        if kind == "webpush":
            if self.webpush is None:
                raise NotificationError("Web-Push ist nicht verfügbar")
            from .webpush import push_message

            delivered, errors = await self.webpush.send(push_message(payload))
            if errors:
                raise NotificationError(f"{len(errors)} Gerät(e) nicht erreicht: {errors[0]}")
            if delivered == 0:
                raise NotificationError("Noch kein Gerät hat Push-Benachrichtigungen aktiviert")
            return

        async with httpx.AsyncClient(timeout=15) as client:
            if kind == "webhook":
                response = await client.post(cfg["url"], json=payload, headers=cfg.get("headers") or None)
            elif kind == "discord":
                color = 0x0CA30C if payload["status"] == "up" else 0xD03B3B if payload["status"] == "down" else 0x2A78D6
                embed = {"title": payload["title"], "description": payload["text"][:4000], "color": color}
                if payload["url"]:
                    embed["url"] = payload["url"]
                response = await client.post(cfg["url"], json={"embeds": [embed]})
            elif kind == "slack":
                response = await client.post(cfg["url"], json={"text": f"*{payload['title']}*\n{payload['text']}"})
            elif kind == "telegram":
                response = await client.post(
                    f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
                    json={
                        "chat_id": cfg["chat_id"],
                        "text": f"{payload['title']}\n{payload['text']}",
                        "disable_web_page_preview": True,
                    },
                )
            elif kind == "ntfy":
                server = (cfg.get("server") or "https://ntfy.sh").rstrip("/")
                headers = {
                    # HTTP headers must be latin-1; strip the emoji from the title.
                    "Title": payload["title"].encode("latin-1", "ignore").decode().strip(),
                    "Priority": "urgent" if payload["status"] == "down" else "default",
                    "Tags": "rotating_light" if payload["status"] == "down" else "white_check_mark",
                }
                if payload["url"]:
                    headers["Click"] = payload["url"]
                if cfg.get("token"):
                    headers["Authorization"] = f"Bearer {cfg['token']}"
                response = await client.post(
                    f"{server}/{cfg['topic']}", content=payload["text"].encode(), headers=headers
                )
            else:
                raise NotificationError(f"Unbekannter Kanaltyp: {kind}")

        if response.status_code >= 400:
            raise NotificationError(f"HTTP {response.status_code}: {response.text[:200]}")


def _send_email(cfg: dict[str, Any], payload: dict[str, Any]) -> None:
    recipients = cfg["recipients"]
    if isinstance(recipients, str):
        recipients = [r.strip() for r in recipients.split(",") if r.strip()]
    message = EmailMessage()
    message["Subject"] = payload["title"]
    message["From"] = cfg["sender"]
    message["To"] = ", ".join(recipients)
    message.set_content(payload["text"])

    port = int(cfg.get("port") or 587)
    security = cfg.get("security", "starttls")  # starttls | ssl | none
    context = ssl.create_default_context()
    if security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(cfg["host"], port, timeout=20, context=context)
    else:
        server = smtplib.SMTP(cfg["host"], port, timeout=20)
    with server:
        if security == "starttls":
            server.starttls(context=context)
        if cfg.get("username"):
            server.login(cfg["username"], cfg.get("password", ""))
        server.send_message(message, to_addrs=recipients)
