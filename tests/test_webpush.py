import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import http_ece
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.webpush import WebPush


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class _PushService(BaseHTTPRequestHandler):
    """Fake browser push service: records requests, answers with the configured status."""

    received: list = []
    status = 201

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _PushService.received.append(({k.lower(): v for k, v in self.headers.items()}, body))
        self.send_response(_PushService.status)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def push_service():
    _PushService.received = []
    _PushService.status = 201
    server = HTTPServer(("127.0.0.1", 0), _PushService)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/push/abc"
    server.shutdown()


def _browser_keys():
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = os.urandom(16)
    return key, _b64(public), _b64(auth), auth


def test_vapid_key_is_persistent(app):
    db = app.state.db
    first = WebPush(db, "mailto:a@b.c").public_key
    assert WebPush(db, "mailto:a@b.c").public_key == first
    assert len(base64.urlsafe_b64decode(first + "==")) == 65  # uncompressed P-256 point


def test_encrypted_delivery_and_expired_subscription(app, push_service):
    webpush = app.state.webpush
    key, p256dh, auth_b64, auth = _browser_keys()
    sub_id = app.state.db.execute(
        "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, created_at) VALUES (NULL, ?, ?, ?, 0)",
        (push_service, p256dh, auth_b64),
    ).lastrowid
    sub = app.state.db.one("SELECT * FROM push_subscriptions WHERE id = ?", (sub_id,))

    assert webpush._send_one(sub, json.dumps({"title": "Test", "body": "Hallo"})) is None
    headers, body = _PushService.received[0]
    assert headers["content-encoding"] == "aes128gcm"
    assert headers["authorization"].startswith("vapid t=") and webpush.public_key in headers["authorization"]
    # the browser side can decrypt it
    plain = http_ece.decrypt(body, private_key=key, auth_secret=auth, version="aes128gcm")
    assert json.loads(plain) == {"title": "Test", "body": "Hallo"}

    _PushService.status = 410  # subscription revoked in the browser -> removed
    assert webpush._send_one(sub, "{}") is None
    assert webpush.subscriptions() == []


def test_subscribe_api_and_channel(auth_client, app, monkeypatch):
    info = auth_client.get("/api/webpush").json()
    assert info["public_key"] == app.state.webpush.public_key and info["endpoints"] == []

    bad = {"endpoint": "http://insecure.example/x", "keys": {"p256dh": "a", "auth": "b"}}
    assert auth_client.post("/api/webpush/subscriptions", json=bad).status_code == 422
    _, p256dh, auth_b64, _ = _browser_keys()
    good = {"endpoint": "https://fcm.googleapis.com/fcm/send/xyz", "keys": {"p256dh": p256dh, "auth": auth_b64}}
    assert auth_client.post("/api/webpush/subscriptions", json=good).status_code == 201
    assert auth_client.post("/api/webpush/subscriptions", json=good).status_code == 201  # idempotent
    assert auth_client.get("/api/webpush").json()["endpoints"] == [good["endpoint"]]

    sent = []
    monkeypatch.setattr("app.webpush.webpush", lambda **kwargs: sent.append(kwargs))
    assert auth_client.post("/api/webpush/test").json() == {"ok": True, "delivered": 1}
    assert json.loads(sent[0]["data"])["title"].endswith("Testnachricht vom Monitoring")

    channel = auth_client.post("/api/channels", json={"name": "App", "type": "webpush"}).json()
    assert auth_client.post(f"/api/channels/{channel['id']}/test").status_code == 200
    assert len(sent) == 2

    # users without monitoring rights get no alarms, even with a subscription
    roles = {r["name"]: r["id"] for r in auth_client.get("/api/roles").json()}
    auth_client.post("/api/users", json={"username": "x", "password": "password1", "role_ids": []})
    user = [u for u in auth_client.get("/api/users").json() if u["username"] == "x"][0]
    app.state.webpush.subscribe(user["id"], "https://push.example/other", p256dh, auth_b64)
    assert auth_client.post(f"/api/channels/{channel['id']}/test").status_code == 200
    assert len(sent) == 3
    auth_client.put(f"/api/users/{user['id']}", json={"username": "x", "role_ids": [roles["Monitoring-Betrachter"]]})
    auth_client.post(f"/api/channels/{channel['id']}/test")
    assert len(sent) == 5

    assert auth_client.post("/api/webpush/unsubscribe", json={"endpoint": good["endpoint"]}).status_code == 200
    assert auth_client.post("/api/webpush/test").status_code == 409
