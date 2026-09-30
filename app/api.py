"""REST API: auth, monitors, push ingestion, notification channels, API keys, Prometheus export."""

import json
import math
import time
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from . import __version__
from .auth import (
    MAX_PASSWORD_BYTES,
    SESSION_COOKIE,
    authenticate,
    create_api_key,
    create_session,
    delete_session,
    hash_password,
    new_push_token,
    require,
    verify_password,
)
from .checks import (
    HTTP_METHODS,
    PULL_TYPES,
    THRESHOLD_OPS,
    evaluate_push,
    is_valid_host,
    is_valid_metric_name,
    parse_host_port,
    parse_push_status,
    parse_status_spec,
)
from .db import Database
from .notifier import CHANNEL_REQUIRED
from .userstore import PERMISSIONS, UserConflict, UserStore, has_permission, is_valid_permission
from .service import bucket_results, recent_beats, record_result, uptime_by_monitor

router = APIRouter(prefix="/api")
protected = APIRouter(prefix="/api", dependencies=[Depends(authenticate)])
viewer = APIRouter(prefix="/api", dependencies=[Depends(require("monitoring.view"))])
editor = APIRouter(prefix="/api", dependencies=[Depends(require("monitoring.edit"))])
user_admin = APIRouter(prefix="/api", dependencies=[Depends(require("users.manage"))])

RANGES = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
MAX_PUSH_METRICS = 50


def get_db(request: Request) -> Database:
    return request.app.state.db


# =========================================================================== models


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class SetupIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=256)


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class MonitorIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    type: Literal["http", "tcp", "ping", "dns", "push"]
    target: str = Field("", max_length=2000)
    group_name: str = Field("", max_length=100)
    interval: int = Field(60, ge=10, le=86400)
    timeout: int = Field(10, ge=1, le=120)
    retries: int = Field(0, ge=0, le=20)
    config: dict[str, Any] = Field(default_factory=dict)
    channel_ids: list[int] = Field(default_factory=list)
    enabled: bool = True


class ChannelIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    type: Literal["webpush", "webhook", "discord", "slack", "telegram", "ntfy", "email"]
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


# =========================================================================== validation


def _bad(message: str) -> HTTPException:
    return HTTPException(422, message)


def _is_http_url(value: str) -> bool:
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def validate_target(kind: str, target: str) -> str:
    target = target.strip()
    if kind == "push":
        return ""
    if not target:
        raise _bad("Ziel darf nicht leer sein")
    if kind == "http":
        if not _is_http_url(target):
            raise _bad("URL muss mit http:// oder https:// beginnen")
    elif kind == "tcp":
        try:
            parse_host_port(target)
        except ValueError as exc:
            raise _bad(str(exc)) from None
    elif kind in ("ping", "dns") and not is_valid_host(target):
        raise _bad(f"Ungültiger Hostname: {target!r}")
    return target


def _as_bool(value: Any, default: bool) -> bool:
    return default if value is None else bool(value)


def normalize_config(kind: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Whitelist and validate type-specific settings."""
    if kind == "http":
        method = str(cfg.get("method") or "GET").upper()
        if method not in HTTP_METHODS:
            raise _bad(f"Ungültige HTTP-Methode: {method}")
        expected = str(cfg.get("expected_status") or "200-399").replace(" ", "")
        try:
            parse_status_spec(expected)
        except ValueError as exc:
            raise _bad(str(exc)) from None
        headers = cfg.get("headers") or {}
        if not isinstance(headers, dict) or len(headers) > 30:
            raise _bad("Header müssen ein JSON-Objekt sein (max. 30 Einträge)")
        headers = {str(k).strip(): str(v) for k, v in headers.items() if str(k).strip()}
        try:
            cert_days = int(cfg.get("cert_expiry_days", 14) or 0)
        except (TypeError, ValueError):
            raise _bad("Zertifikats-Warnung muss eine Zahl sein") from None
        return {
            "method": method,
            "expected_status": expected,
            "keyword": str(cfg.get("keyword") or "")[:500],
            "keyword_invert": _as_bool(cfg.get("keyword_invert"), False),
            "verify_tls": _as_bool(cfg.get("verify_tls"), True),
            "follow_redirects": _as_bool(cfg.get("follow_redirects"), True),
            "cert_expiry_days": max(0, min(cert_days, 365)),
            "headers": headers,
            "body": str(cfg.get("body") or "")[:10000],
        }
    if kind == "push":
        try:
            grace = int(cfg.get("grace", 60))
        except (TypeError, ValueError):
            raise _bad("Karenzzeit muss eine Zahl sein") from None
        thresholds = []
        for rule in cfg.get("thresholds") or []:
            if not isinstance(rule, dict):
                raise _bad("Ungültiger Grenzwert")
            metric = str(rule.get("metric") or "").strip()
            op = str(rule.get("op") or "")
            if not is_valid_metric_name(metric):
                raise _bad(f"Ungültiger Metrikname: {metric!r}")
            if op not in THRESHOLD_OPS:
                raise _bad(f"Ungültiger Operator: {op!r}")
            try:
                value = float(rule.get("value"))
            except (TypeError, ValueError):
                raise _bad(f"Grenzwert für {metric} muss eine Zahl sein") from None
            thresholds.append({"metric": metric, "op": op, "value": value})
        if len(thresholds) > 30:
            raise _bad("Maximal 30 Grenzwerte")
        return {"grace": max(0, min(grace, 86400)), "thresholds": thresholds}
    if kind == "dns":
        return {"expected": str(cfg.get("expected") or "").strip()[:100]}
    return {}


def normalize_channel_config(kind: str, cfg: dict[str, Any]) -> dict[str, Any]:
    clean = {k: v for k, v in cfg.items() if v not in (None, "")}
    missing = [key for key in CHANNEL_REQUIRED[kind] if not clean.get(key)]
    if missing:
        raise _bad(f"Fehlende Angaben: {', '.join(missing)}")
    for key in ("url", "server"):
        if key in clean and not _is_http_url(str(clean[key])):
            raise _bad(f"{key} muss eine http(s)-URL sein")
    if kind == "webhook" and "headers" in clean and not isinstance(clean["headers"], dict):
        raise _bad("Header müssen ein JSON-Objekt sein")
    if kind == "email":
        recipients = clean["recipients"]
        if isinstance(recipients, str):
            recipients = [r.strip() for r in recipients.split(",") if r.strip()]
        if not recipients:
            raise _bad("Mindestens ein Empfänger erforderlich")
        clean["recipients"] = recipients
        if clean.get("security", "starttls") not in ("starttls", "ssl", "none"):
            raise _bad("Verschlüsselung muss starttls, ssl oder none sein")
    return clean


def _check_channel_ids(db: Database, ids: list[int]) -> list[int]:
    ids = sorted(set(ids))
    if ids:
        placeholders = ",".join("?" * len(ids))
        found = {row["id"] for row in db.query(f"SELECT id FROM channels WHERE id IN ({placeholders})", tuple(ids))}
        unknown = set(ids) - found
        if unknown:
            raise _bad(f"Unbekannte Kanäle: {sorted(unknown)}")
    return ids


# =========================================================================== serialization


def serialize_monitor(db: Database, monitor: dict[str, Any], with_channels: bool = True) -> dict[str, Any]:
    data = dict(monitor)
    data["enabled"] = bool(data["enabled"])
    data["push_path"] = f"/api/push/{data['push_token']}" if data["type"] == "push" else None
    if with_channels:
        data["channel_ids"] = [
            row["channel_id"]
            for row in db.query("SELECT channel_id FROM monitor_channels WHERE monitor_id = ?", (monitor["id"],))
        ]
    return data


def _can_edit(principal: dict) -> bool:
    return has_permission(principal["permissions"], "monitoring.edit")


def _redact_monitor(data: dict[str, Any], principal: dict) -> dict[str, Any]:
    """Push tokens and request headers (may contain credentials) are only visible to editors."""
    if not _can_edit(principal):
        data["push_token"] = None
        data["push_path"] = None
        if data["type"] == "http":
            data["config"] = {**data["config"], "headers": {}, "body": ""}
    return data


def load_monitor(db: Database, monitor_id: int) -> dict[str, Any]:
    monitor = db.one("SELECT * FROM monitors WHERE id = ?", (monitor_id,))
    if monitor is None:
        raise HTTPException(404, "Monitor nicht gefunden")
    return monitor


def load_channel(db: Database, channel_id: int) -> dict[str, Any]:
    channel = db.one("SELECT * FROM channels WHERE id = ?", (channel_id,))
    if channel is None:
        raise HTTPException(404, "Kanal nicht gefunden")
    return channel


def serialize_channel(channel: dict[str, Any]) -> dict[str, Any]:
    data = dict(channel)
    data["enabled"] = bool(data["enabled"])
    return data


# =========================================================================== auth


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _set_session_cookie(request: Request, response: Response, token: str) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_days * 86400,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
        path="/",
    )


@router.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "version": __version__}


def get_users(request: Request) -> UserStore:
    return request.app.state.users


def _check_password_length(password: str) -> None:
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        raise _bad(f"Passwort darf höchstens {MAX_PASSWORD_BYTES} Bytes lang sein")


@router.get("/auth/status")
def auth_status(request: Request, users: UserStore = Depends(get_users)) -> dict[str, Any]:
    setup_required = users.count_users() == 0
    user = None
    try:
        principal = authenticate(request)
        user = {"name": principal["name"], "kind": principal["kind"], "permissions": sorted(principal["permissions"])}
    except HTTPException:
        pass
    settings = request.app.state.settings
    return {
        "setup_required": setup_required,
        "user": user,
        "public_url": settings.public_url,
        "retention_days": settings.retention_days,
        "user_db": settings.user_db_label,
        "version": __version__,
    }


@router.post("/auth/setup")
def setup(body: SetupIn, request: Request, response: Response, users: UserStore = Depends(get_users)) -> dict[str, Any]:
    _check_password_length(body.password)
    user_id = users.create_first_user(body.username.strip(), hash_password(body.password))
    if user_id is None:
        raise HTTPException(409, "Einrichtung wurde bereits abgeschlossen")
    _set_session_cookie(request, response, create_session(users, user_id, request.app.state.settings.session_days))
    return {"ok": True}


@router.post("/auth/login")
def login(body: Credentials, request: Request, response: Response, users: UserStore = Depends(get_users)) -> dict[str, Any]:
    throttle = request.app.state.throttle
    key = _client_key(request)
    throttle.check(key)
    user = users.get_user_by_name(body.username.strip())
    if user is None or not verify_password(body.password, user["password_hash"]):
        throttle.fail(key)
        raise HTTPException(401, "Benutzername oder Passwort falsch")
    throttle.reset(key)
    _set_session_cookie(request, response, create_session(users, user["id"], request.app.state.settings.session_days))
    return {"ok": True}


@router.post("/auth/logout")
def logout(request: Request, response: Response, users: UserStore = Depends(get_users)) -> dict[str, Any]:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        delete_session(users, token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@protected.post("/auth/password")
def change_password(
    body: PasswordChange, principal: dict = Depends(authenticate), users: UserStore = Depends(get_users)
) -> dict[str, Any]:
    if principal["kind"] != "user":
        raise HTTPException(403, "Nur für angemeldete Benutzer")
    _check_password_length(body.new_password)
    user = users.get_user(principal["id"])
    if not user or not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(400, "Aktuelles Passwort ist falsch")
    users.set_password(user["id"], hash_password(body.new_password))
    return {"ok": True}


# =========================================================================== monitors


@viewer.get("/monitors")
def list_monitors(db: Database = Depends(get_db), principal: dict = Depends(authenticate)) -> list[dict[str, Any]]:
    now = time.time()
    uptime = uptime_by_monitor(db, now - 86400)
    links: dict[int, list[int]] = {}
    for row in db.query("SELECT monitor_id, channel_id FROM monitor_channels"):
        links.setdefault(row["monitor_id"], []).append(row["channel_id"])
    monitors = []
    for monitor in db.query("SELECT * FROM monitors ORDER BY group_name COLLATE NOCASE, name COLLATE NOCASE"):
        data = serialize_monitor(db, monitor, with_channels=False)
        data["channel_ids"] = links.get(monitor["id"], [])
        data["uptime_24h"] = uptime.get(monitor["id"])
        data["beats"] = recent_beats(db, monitor["id"])
        monitors.append(_redact_monitor(data, principal))
    return monitors


@editor.post("/monitors", status_code=201)
def create_monitor(body: MonitorIn, db: Database = Depends(get_db)) -> dict[str, Any]:
    target = validate_target(body.type, body.target)
    config = normalize_config(body.type, body.config)
    now = time.time()
    with db.transaction():
        channel_ids = _check_channel_ids(db, body.channel_ids)
        monitor_id = db.execute(
            """
            INSERT INTO monitors (name, type, target, group_name, interval, timeout, retries, config,
                                  push_token, enabled, status, active_since, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                body.name.strip(), body.type, target, body.group_name.strip(), body.interval, body.timeout,
                body.retries, json.dumps(config), new_push_token() if body.type == "push" else None,
                int(body.enabled), "pending" if body.enabled else "paused", now, now,
            ),
        ).lastrowid
        for channel_id in channel_ids:
            db.execute("INSERT INTO monitor_channels (monitor_id, channel_id) VALUES (?, ?)", (monitor_id, channel_id))
    return serialize_monitor(db, load_monitor(db, monitor_id))


@viewer.get("/monitors/{monitor_id}")
def get_monitor(
    monitor_id: int, db: Database = Depends(get_db), principal: dict = Depends(authenticate)
) -> dict[str, Any]:
    data = _redact_monitor(serialize_monitor(db, load_monitor(db, monitor_id)), principal)
    now = time.time()
    stats = db.one(
        """
        SELECT
            AVG(CASE WHEN ts >= :d1 THEN ok END) AS up_24h,
            AVG(CASE WHEN ts >= :d7 THEN ok END) AS up_7d,
            AVG(ok) AS up_30d,
            AVG(CASE WHEN ts >= :d1 THEN latency END) AS avg_latency_24h
        FROM results WHERE monitor_id = :id AND ts >= :d30
        """,
        {"id": monitor_id, "d1": now - 86400, "d7": now - 7 * 86400, "d30": now - 30 * 86400},
    ) or {}

    def pct(value: float | None) -> float | None:
        return None if value is None else round(value * 100, 2)

    data["stats"] = {
        "uptime_24h": pct(stats.get("up_24h")),
        "uptime_7d": pct(stats.get("up_7d")),
        "uptime_30d": pct(stats.get("up_30d")),
        "avg_latency_24h": stats.get("avg_latency_24h"),
    }
    return data


@editor.put("/monitors/{monitor_id}")
def update_monitor(monitor_id: int, body: MonitorIn, db: Database = Depends(get_db)) -> dict[str, Any]:
    target = validate_target(body.type, body.target)
    config = normalize_config(body.type, body.config)
    now = time.time()
    with db.transaction():
        current = load_monitor(db, monitor_id)
        channel_ids = _check_channel_ids(db, body.channel_ids)
        push_token = current["push_token"] or (new_push_token() if body.type == "push" else None)
        type_changed = body.type != current["type"]
        was_enabled = bool(current["enabled"])

        if not body.enabled:
            status = "paused"
        elif not was_enabled or type_changed:
            status = "pending"
        else:
            status = current["status"]
        reset = status != current["status"] or type_changed

        db.execute(
            """
            UPDATE monitors SET name = ?, type = ?, target = ?, group_name = ?, interval = ?, timeout = ?,
                retries = ?, config = ?, push_token = ?, enabled = ?, status = ?,
                fail_count = CASE WHEN ? THEN 0 ELSE fail_count END,
                active_since = CASE WHEN ? THEN ? ELSE active_since END,
                last_change_at = CASE WHEN ? THEN ? ELSE last_change_at END,
                last_check_at = CASE WHEN ? THEN NULL ELSE last_check_at END
            WHERE id = ?
            """,
            (
                body.name.strip(), body.type, target, body.group_name.strip(), body.interval, body.timeout,
                body.retries, json.dumps(config), push_token, int(body.enabled), status,
                int(reset),
                int(reset), now,
                int(status != current["status"]), now,
                # Re-run pull checks right away after an edit so the change is visible immediately.
                int(body.enabled and body.type in PULL_TYPES),
                monitor_id,
            ),
        )
        if status != current["status"]:
            db.execute(
                "INSERT INTO events (monitor_id, ts, status, previous_status, message) VALUES (?, ?, ?, ?, ?)",
                (monitor_id, now, status, current["status"], "Monitor bearbeitet"),
            )
        db.execute("DELETE FROM monitor_channels WHERE monitor_id = ?", (monitor_id,))
        for channel_id in channel_ids:
            db.execute("INSERT INTO monitor_channels (monitor_id, channel_id) VALUES (?, ?)", (monitor_id, channel_id))
    return serialize_monitor(db, load_monitor(db, monitor_id))


@editor.delete("/monitors/{monitor_id}", status_code=204)
def delete_monitor(monitor_id: int, db: Database = Depends(get_db)) -> Response:
    load_monitor(db, monitor_id)
    db.execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
    return Response(status_code=204)


def _set_enabled(db: Database, monitor_id: int, enabled: bool) -> dict[str, Any]:
    now = time.time()
    with db.transaction():
        monitor = load_monitor(db, monitor_id)
        if bool(monitor["enabled"]) == enabled:
            return serialize_monitor(db, monitor)
        status = "pending" if enabled else "paused"
        db.execute(
            """
            UPDATE monitors SET enabled = ?, status = ?, fail_count = 0, last_change_at = ?,
                active_since = CASE WHEN ? THEN ? ELSE active_since END,
                last_check_at = CASE WHEN ? THEN NULL ELSE last_check_at END
            WHERE id = ?
            """,
            (int(enabled), status, now, int(enabled), now, int(enabled), monitor_id),
        )
        db.execute(
            "INSERT INTO events (monitor_id, ts, status, previous_status, message) VALUES (?, ?, ?, ?, ?)",
            (monitor_id, now, status, monitor["status"], "Fortgesetzt" if enabled else "Pausiert"),
        )
    return serialize_monitor(db, load_monitor(db, monitor_id))


@editor.post("/monitors/{monitor_id}/pause")
def pause_monitor(monitor_id: int, db: Database = Depends(get_db)) -> dict[str, Any]:
    return _set_enabled(db, monitor_id, False)


@editor.post("/monitors/{monitor_id}/resume")
def resume_monitor(monitor_id: int, db: Database = Depends(get_db)) -> dict[str, Any]:
    return _set_enabled(db, monitor_id, True)


@editor.post("/monitors/{monitor_id}/check")
async def check_now(monitor_id: int, request: Request) -> dict[str, Any]:
    db: Database = request.app.state.db
    monitor = load_monitor(db, monitor_id)
    if monitor["type"] not in PULL_TYPES:
        raise HTTPException(400, "Push-Monitore werden vom überwachten System ausgelöst")
    scheduler = request.app.state.scheduler
    if scheduler.is_running(monitor["id"]):
        raise HTTPException(409, "Prüfung läuft bereits")
    result = await scheduler.run_now(monitor)
    return {"ok": result.ok, "latency": result.latency, "message": result.message, "metrics": result.metrics}


@editor.post("/monitors/{monitor_id}/token")
def regenerate_token(monitor_id: int, db: Database = Depends(get_db)) -> dict[str, Any]:
    monitor = load_monitor(db, monitor_id)
    if monitor["type"] != "push":
        raise HTTPException(400, "Nur Push-Monitore haben ein Token")
    db.execute("UPDATE monitors SET push_token = ? WHERE id = ?", (new_push_token(), monitor_id))
    return serialize_monitor(db, load_monitor(db, monitor_id))


@viewer.get("/monitors/{monitor_id}/results")
def monitor_results(
    monitor_id: int,
    range_: Literal["1h", "24h", "7d", "30d"] = Query("24h", alias="range"),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    load_monitor(db, monitor_id)
    end = time.time()
    start = end - RANGES[range_]
    rows = db.query(
        "SELECT ts, ok, latency, message, metrics FROM results WHERE monitor_id = ? AND ts >= ? ORDER BY ts",
        (monitor_id, start),
    )
    points = bucket_results(rows, start, end)
    metric_keys = sorted({key for point in points for key in point["metrics"]})
    return {"start": start, "end": end, "points": points, "metric_keys": metric_keys}


@viewer.get("/events")
def list_events(
    monitor_id: int | None = None,
    limit: int = Query(50, ge=1, le=500),
    db: Database = Depends(get_db),
) -> list[dict[str, Any]]:
    sql = """
        SELECT e.*, m.name AS monitor_name, m.type AS monitor_type
        FROM events e JOIN monitors m ON m.id = e.monitor_id
    """
    params: tuple = ()
    if monitor_id is not None:
        sql += " WHERE e.monitor_id = ?"
        params = (monitor_id,)
    sql += " ORDER BY e.ts DESC LIMIT ?"
    return db.query(sql, params + (limit,))


# =========================================================================== push ingestion


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@router.api_route("/push/{token}", methods=["GET", "POST"])
async def push(token: str, request: Request) -> dict[str, Any]:
    """Receive a heartbeat / metrics from a monitored system.

    GET  /api/push/<token>?status=up&msg=OK&ping=12&cpu=40
    POST /api/push/<token>  {"status": "up", "message": "...", "latency": 12, "metrics": {"cpu": 40}}
    """
    db: Database = request.app.state.db
    monitor = db.one("SELECT * FROM monitors WHERE push_token = ? AND type = 'push'", (token,))
    if monitor is None:
        raise HTTPException(404, "Unbekanntes Push-Token")

    data: dict[str, Any] = dict(request.query_params)
    metrics: dict[str, Any] = {}
    if request.method == "POST":
        raw = await request.body()
        if len(raw) > 64 * 1024:
            raise HTTPException(413, "Payload zu groß")
        if raw.strip():
            try:
                body = json.loads(raw)
            except ValueError:
                raise HTTPException(400, "Body muss JSON sein") from None
            if not isinstance(body, dict):
                raise HTTPException(400, "Body muss ein JSON-Objekt sein")
            if isinstance(body.get("metrics"), dict):
                metrics.update(body.pop("metrics"))
            data.update(body)

    reserved = {"status", "msg", "message", "ping", "latency"}
    for key, value in data.items():
        if key not in reserved:
            metrics.setdefault(key, value)

    clean_metrics: dict[str, float] = {}
    for key, value in metrics.items():
        number = _to_float(value)
        if number is not None and is_valid_metric_name(str(key)):
            clean_metrics[str(key)] = number
        if len(clean_metrics) >= MAX_PUSH_METRICS:
            break

    status_ok = parse_push_status(data.get("status"))
    if status_ok is None:
        raise HTTPException(400, "Unbekannter Status (erlaubt: up/ok oder down/fail/error)")
    message = str(data.get("message") or data.get("msg") or "")[:1000]
    latency = _to_float(data.get("latency", data.get("ping")))

    result = evaluate_push(status_ok, message, clean_metrics, monitor["config"].get("thresholds", []), latency)
    transition = record_result(db, monitor["id"], result, pushed=True)
    request.app.state.notifier.dispatch(transition)
    current = db.scalar("SELECT status FROM monitors WHERE id = ?", (monitor["id"],))
    return {"ok": True, "status": current, "accepted_metrics": sorted(clean_metrics)}


# =========================================================================== channels


@viewer.get("/channels")
def list_channels(db: Database = Depends(get_db), principal: dict = Depends(authenticate)) -> list[dict[str, Any]]:
    channels = [serialize_channel(c) for c in db.query("SELECT * FROM channels ORDER BY name COLLATE NOCASE")]
    if not _can_edit(principal):  # channel settings contain tokens and passwords
        for channel in channels:
            channel["config"] = {}
    return channels


@editor.post("/channels", status_code=201)
def create_channel(body: ChannelIn, db: Database = Depends(get_db)) -> dict[str, Any]:
    config = normalize_channel_config(body.type, body.config)
    channel_id = db.execute(
        "INSERT INTO channels (name, type, config, enabled, created_at) VALUES (?, ?, ?, ?, ?)",
        (body.name.strip(), body.type, json.dumps(config), int(body.enabled), time.time()),
    ).lastrowid
    return serialize_channel(load_channel(db, channel_id))


@editor.put("/channels/{channel_id}")
def update_channel(channel_id: int, body: ChannelIn, db: Database = Depends(get_db)) -> dict[str, Any]:
    load_channel(db, channel_id)
    config = normalize_channel_config(body.type, body.config)
    db.execute(
        "UPDATE channels SET name = ?, type = ?, config = ?, enabled = ? WHERE id = ?",
        (body.name.strip(), body.type, json.dumps(config), int(body.enabled), channel_id),
    )
    return serialize_channel(load_channel(db, channel_id))


@editor.delete("/channels/{channel_id}", status_code=204)
def delete_channel(channel_id: int, db: Database = Depends(get_db)) -> Response:
    load_channel(db, channel_id)
    db.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    return Response(status_code=204)


@editor.post("/channels/{channel_id}/test")
async def test_channel(channel_id: int, request: Request) -> dict[str, Any]:
    db: Database = request.app.state.db
    channel = load_channel(db, channel_id)
    notifier = request.app.state.notifier
    error = await notifier.send_and_record(channel, notifier.test_payload())
    if error:
        raise HTTPException(502, f"Versand fehlgeschlagen: {error}")
    return {"ok": True}


# =========================================================================== API keys


@protected.get("/keys")
def list_keys(principal: dict = Depends(authenticate), users: UserStore = Depends(get_users)) -> list[dict[str, Any]]:
    if has_permission(principal["permissions"], "users.manage"):
        return users.list_api_keys()
    return users.list_api_keys(principal["id"]) if principal["id"] else []


@protected.post("/keys", status_code=201)
def add_key(
    body: ApiKeyIn, principal: dict = Depends(authenticate), users: UserStore = Depends(get_users)
) -> dict[str, Any]:
    if principal["kind"] != "user":
        raise HTTPException(403, "API-Keys können nur angemeldete Benutzer erzeugen")
    key_id, key = create_api_key(users, body.name.strip(), principal["id"])
    return {"id": key_id, "name": body.name.strip(), "key": key}


@protected.delete("/keys/{key_id}", status_code=204)
def delete_key(
    key_id: int, principal: dict = Depends(authenticate), users: UserStore = Depends(get_users)
) -> Response:
    key = users.get_api_key(key_id)
    if key is None:
        raise HTTPException(404, "API-Key nicht gefunden")
    if key["user_id"] != principal["id"] and not has_permission(principal["permissions"], "users.manage"):
        raise HTTPException(403, "Nur eigene API-Keys können widerrufen werden")
    users.delete_api_key(key_id)
    return Response(status_code=204)


# =========================================================================== web push (app notifications)


class PushSubscriptionIn(BaseModel):
    endpoint: str = Field(max_length=2000)
    keys: dict[str, str]


class PushEndpointIn(BaseModel):
    endpoint: str = Field(max_length=2000)


@viewer.get("/webpush")
def webpush_info(request: Request, principal: dict = Depends(authenticate)) -> dict[str, Any]:
    webpush = request.app.state.webpush
    return {
        "public_key": webpush.public_key,
        "endpoints": [s["endpoint"] for s in webpush.subscriptions(principal["id"])],
    }


@viewer.post("/webpush/subscriptions", status_code=201)
def webpush_subscribe(body: PushSubscriptionIn, request: Request, principal: dict = Depends(authenticate)) -> dict[str, Any]:
    if principal["kind"] != "user":
        raise HTTPException(403, "Nur für angemeldete Benutzer")
    try:
        request.app.state.webpush.subscribe(
            principal["id"], body.endpoint, body.keys.get("p256dh", ""), body.keys.get("auth", ""),
            request.headers.get("user-agent", ""),
        )
    except ValueError as exc:
        raise _bad(str(exc)) from None
    return {"ok": True}


@viewer.post("/webpush/unsubscribe")
def webpush_unsubscribe(body: PushEndpointIn, request: Request) -> dict[str, Any]:
    request.app.state.webpush.unsubscribe(body.endpoint)
    return {"ok": True}


@viewer.post("/webpush/test")
async def webpush_test(request: Request, principal: dict = Depends(authenticate)) -> dict[str, Any]:
    from .notifier import Notifier
    from .webpush import push_message

    delivered, errors = await request.app.state.webpush.send(push_message(Notifier.test_payload()), principal["id"])
    if errors:
        raise HTTPException(502, f"Versand fehlgeschlagen: {errors[0]}")
    if delivered == 0:
        raise HTTPException(409, "Auf keinem deiner Geräte sind Push-Benachrichtigungen aktiviert")
    return {"ok": True, "delivered": delivered}


# =========================================================================== users & rights


class UserIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str | None = Field(None, max_length=256)
    role_ids: list[int] = Field(default_factory=list)


class RoleIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field("", max_length=255)
    permissions: list[str] = Field(default_factory=list, max_length=100)


def _check_roles(users: UserStore, role_ids: list[int]) -> list[int]:
    known = {role["id"] for role in users.list_roles()}
    unknown = sorted(set(role_ids) - known)
    if unknown:
        raise _bad(f"Unbekannte Rollen: {unknown}")
    return sorted(set(role_ids))


def _ensure_admin_left(users: UserStore) -> None:
    """Called inside a transaction after a change: roll back if nobody could manage users anymore."""
    if users.count_admins() == 0:
        raise UserConflict("Mindestens ein Benutzer muss Benutzer und Rechte verwalten dürfen")


@user_admin.get("/permissions")
def list_permissions(users: UserStore = Depends(get_users)) -> list[dict[str, str]]:
    known = dict(PERMISSIONS)
    for role in users.list_roles():
        for permission in role["permissions"]:
            known.setdefault(permission, "")
    return [{"permission": key, "description": value} for key, value in known.items()]


@user_admin.get("/users")
def list_users(users: UserStore = Depends(get_users)) -> list[dict[str, Any]]:
    return users.list_users()


@user_admin.post("/users", status_code=201)
def create_user(body: UserIn, users: UserStore = Depends(get_users)) -> dict[str, Any]:
    if not body.password or len(body.password) < 8:
        raise _bad("Passwort muss mindestens 8 Zeichen haben")
    _check_password_length(body.password)
    role_ids = _check_roles(users, body.role_ids)
    with users.transaction():
        user_id = users.create_user(body.username.strip(), hash_password(body.password))
        users.set_user_roles(user_id, role_ids)
    return next(u for u in users.list_users() if u["id"] == user_id)


@user_admin.put("/users/{user_id}")
def update_user(user_id: int, body: UserIn, users: UserStore = Depends(get_users)) -> dict[str, Any]:
    if users.get_user(user_id) is None:
        raise HTTPException(404, "Benutzer nicht gefunden")
    if body.password:
        if len(body.password) < 8:
            raise _bad("Passwort muss mindestens 8 Zeichen haben")
        _check_password_length(body.password)
    role_ids = _check_roles(users, body.role_ids)
    with users.transaction():
        users.rename_user(user_id, body.username.strip())
        if body.password:
            users.set_password(user_id, hash_password(body.password))
        users.set_user_roles(user_id, role_ids)
        _ensure_admin_left(users)
    return next(u for u in users.list_users() if u["id"] == user_id)


@user_admin.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: int, principal: dict = Depends(authenticate), users: UserStore = Depends(get_users)) -> Response:
    if user_id == principal["id"]:
        raise HTTPException(409, "Du kannst dich nicht selbst löschen")
    with users.transaction():
        if not users.delete_user(user_id):
            raise HTTPException(404, "Benutzer nicht gefunden")
        _ensure_admin_left(users)
    return Response(status_code=204)


@user_admin.get("/roles")
def list_roles(users: UserStore = Depends(get_users)) -> list[dict[str, Any]]:
    return users.list_roles()


def _clean_permissions(permissions: list[str]) -> list[str]:
    cleaned = sorted({p.strip() for p in permissions if p.strip()})
    invalid = [p for p in cleaned if not is_valid_permission(p)]
    if invalid:
        raise _bad(f"Ungültige Berechtigungen: {', '.join(invalid)} (Format: app.aktion, app.* oder *)")
    return cleaned


@user_admin.post("/roles", status_code=201)
def create_role(body: RoleIn, users: UserStore = Depends(get_users)) -> dict[str, Any]:
    role_id = users.save_role(None, body.name.strip(), body.description.strip(), _clean_permissions(body.permissions))
    return users.get_role(role_id)


@user_admin.put("/roles/{role_id}")
def update_role(role_id: int, body: RoleIn, users: UserStore = Depends(get_users)) -> dict[str, Any]:
    if users.get_role(role_id) is None:
        raise HTTPException(404, "Rolle nicht gefunden")
    permissions = _clean_permissions(body.permissions)
    with users.transaction():
        users.save_role(role_id, body.name.strip(), body.description.strip(), permissions)
        _ensure_admin_left(users)
    return users.get_role(role_id)


@user_admin.delete("/roles/{role_id}", status_code=204)
def delete_role(role_id: int, users: UserStore = Depends(get_users)) -> Response:
    with users.transaction():
        if not users.delete_role(role_id):
            raise HTTPException(404, "Rolle nicht gefunden")
        _ensure_admin_left(users)
    return Response(status_code=204)


# =========================================================================== Prometheus


def _label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


metrics_router = APIRouter(dependencies=[Depends(require("monitoring.view"))])


@metrics_router.get("/metrics", response_class=PlainTextResponse)
def prometheus_metrics(db: Database = Depends(get_db)) -> str:
    status_value = {"up": 1, "down": 0}
    lines = [
        "# HELP monitor_up 1 = up, 0 = down, -1 = pending/paused",
        "# TYPE monitor_up gauge",
    ]
    latency_lines = [
        "# HELP monitor_latency_ms Latency of the last check in milliseconds",
        "# TYPE monitor_latency_ms gauge",
    ]
    metric_lines = [
        "# HELP monitor_metric Last value of a metric reported by a push monitor",
        "# TYPE monitor_metric gauge",
    ]
    for m in db.query("SELECT * FROM monitors ORDER BY id"):
        labels = f'id="{m["id"]}",name="{_label(m["name"])}",type="{m["type"]}",group="{_label(m["group_name"])}"'
        lines.append(f"monitor_up{{{labels}}} {status_value.get(m['status'], -1)}")
        if m["last_latency"] is not None:
            latency_lines.append(f"monitor_latency_ms{{{labels}}} {m['last_latency']:.3f}")
        for key, value in sorted((m["last_metrics"] or {}).items()):
            metric_lines.append(f'monitor_metric{{{labels},metric="{_label(key)}"}} {value}')
    return "\n".join(lines + latency_lines + metric_lines) + "\n"

