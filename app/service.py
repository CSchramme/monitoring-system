"""State handling: store check results, derive monitor status, emit status changes."""

import json
import time
from dataclasses import dataclass
from typing import Any

from .checks import CheckResult
from .db import Database


@dataclass
class Transition:
    monitor: dict[str, Any]
    previous: str
    current: str
    message: str
    ts: float

    @property
    def notify(self) -> bool:
        # pending -> up is just the first successful check; everything else is worth a message.
        return not (self.previous == "pending" and self.current == "up")


def record_result(
    db: Database,
    monitor_id: int,
    result: CheckResult,
    now: float | None = None,
    pushed: bool = False,
) -> Transition | None:
    """Persist a result and update the monitor's state. Returns a Transition if the status changed."""
    now = time.time() if now is None else now
    with db.transaction():
        monitor = db.one("SELECT * FROM monitors WHERE id = ?", (monitor_id,))
        if monitor is None:
            return None

        metrics_json = json.dumps(result.metrics) if result.metrics else None
        db.execute(
            "INSERT INTO results (monitor_id, ts, ok, latency, message, metrics) VALUES (?, ?, ?, ?, ?, ?)",
            (monitor_id, now, int(result.ok), result.latency, result.message[:1000], metrics_json),
        )

        previous = monitor["status"]
        if not monitor["enabled"]:
            # Paused monitors keep collecting pushed data but never change state.
            current, fail_count = previous, monitor["fail_count"]
        elif result.ok:
            current, fail_count = "up", 0
        else:
            fail_count = monitor["fail_count"] + 1
            current = "down" if fail_count > monitor["retries"] else previous

        changed = current != previous
        db.execute(
            """
            UPDATE monitors SET
                status = ?, fail_count = ?, last_check_at = ?, last_latency = ?, last_message = ?,
                last_metrics = COALESCE(?, last_metrics),
                last_push_at = CASE WHEN ? THEN ? ELSE last_push_at END,
                last_change_at = CASE WHEN ? THEN ? ELSE last_change_at END
            WHERE id = ?
            """,
            (
                current, fail_count, now, result.latency, result.message[:1000],
                metrics_json,
                int(pushed), now,
                int(changed), now,
                monitor_id,
            ),
        )
        if not changed:
            return None
        db.execute(
            "INSERT INTO events (monitor_id, ts, status, previous_status, message) VALUES (?, ?, ?, ?, ?)",
            (monitor_id, now, current, previous, result.message[:1000]),
        )
        monitor.update(status=current, last_message=result.message, last_change_at=now)
        return Transition(monitor, previous, current, result.message, now)


def push_is_stale(monitor: dict[str, Any], now: float) -> bool:
    """True if a push monitor missed its expected signal and is due for a 'missing' result."""
    grace = int(monitor["config"].get("grace", 60))
    reference = max(monitor["last_push_at"] or 0, monitor["active_since"])
    if now <= reference + monitor["interval"] + grace:
        return False
    last = monitor["last_check_at"] or 0
    return now - last >= monitor["interval"]


def uptime_by_monitor(db: Database, since: float) -> dict[int, float]:
    rows = db.query(
        "SELECT monitor_id, AVG(ok) AS uptime FROM results WHERE ts >= ? GROUP BY monitor_id", (since,)
    )
    return {row["monitor_id"]: round(row["uptime"] * 100, 2) for row in rows}


def recent_beats(db: Database, monitor_id: int, limit: int = 40) -> list[dict[str, Any]]:
    """Latest results of one monitor, oldest first (uses the (monitor_id, ts) index)."""
    rows = db.query(
        "SELECT ts, ok, latency, message FROM results WHERE monitor_id = ? ORDER BY ts DESC LIMIT ?",
        (monitor_id, limit),
    )
    rows.reverse()
    return rows


def bucket_results(rows: list[dict[str, Any]], start: float, end: float, max_points: int = 300) -> list[dict[str, Any]]:
    """Downsample raw results for charting. A bucket counts as failed if any result in it failed."""
    if len(rows) <= max_points:
        return [
            {
                "ts": row["ts"],
                "ok": bool(row["ok"]),
                "latency": row["latency"],
                "metrics": row["metrics"] or {},
                "count": 1,
                "fails": 0 if row["ok"] else 1,
                "message": row["message"],
            }
            for row in rows
        ]

    size = (end - start) / max_points
    buckets: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(int((row["ts"] - start) // size), []).append(row)

    points = []
    for index in sorted(buckets):
        group = buckets[index]
        latencies = [r["latency"] for r in group if r["latency"] is not None]
        sums: dict[str, list[float]] = {}
        for r in group:
            for key, value in (r["metrics"] or {}).items():
                sums.setdefault(key, []).append(value)
        fails = sum(1 for r in group if not r["ok"])
        failed = next((r for r in reversed(group) if not r["ok"]), None)
        points.append(
            {
                "ts": start + (index + 0.5) * size,
                "ok": fails == 0,
                "latency": sum(latencies) / len(latencies) if latencies else None,
                "metrics": {key: sum(values) / len(values) for key, values in sums.items()},
                "count": len(group),
                "fails": fails,
                "message": (failed or group[-1])["message"],
            }
        )
    return points


def purge_old_results(db: Database, retention_days: int, now: float | None = None) -> int:
    now = time.time() if now is None else now
    cutoff = now - retention_days * 86400
    deleted = db.execute("DELETE FROM results WHERE ts < ?", (cutoff,)).rowcount
    db.execute("DELETE FROM events WHERE ts < ?", (now - max(retention_days, 90) * 86400,))
    return deleted
