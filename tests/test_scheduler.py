import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.db import Database
from app.notifier import Notifier
from app.scheduler import Scheduler
from app.service import purge_old_results


def _insert_monitor(db, **fields):
    now = fields.pop("now", time.time())
    values = {
        "name": "m", "type": "push", "target": "", "interval": 60, "timeout": 5, "retries": 0,
        "config": '{"grace": 30, "thresholds": []}', "push_token": "tok", "enabled": 1, "status": "pending",
        "active_since": now, "created_at": now,
    }
    values.update(fields)
    columns = ", ".join(values)
    placeholders = ", ".join("?" * len(values))
    return db.execute(f"INSERT INTO monitors ({columns}) VALUES ({placeholders})", tuple(values.values())).lastrowid


class _Collector(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        _Collector.received.append(self.rfile.read(length))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def webhook():
    _Collector.received = []
    server = HTTPServer(("127.0.0.1", 0), _Collector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/hook", _Collector.received
    server.shutdown()


def test_missing_push_goes_down_and_notifies(tmp_path, webhook):
    url, received = webhook
    db = Database(tmp_path / "t.db")
    start = time.time() - 1000
    monitor_id = _insert_monitor(db, now=start)
    channel_id = db.execute(
        "INSERT INTO channels (name, type, config, created_at) VALUES ('h', 'webhook', ?, ?)",
        (f'{{"url": "{url}"}}', start),
    ).lastrowid
    db.execute("INSERT INTO monitor_channels VALUES (?, ?)", (monitor_id, channel_id))

    async def scenario():
        notifier = Notifier(db, "https://monitor.test")
        scheduler = Scheduler(db, notifier)
        # within interval + grace: nothing happens
        scheduler.tick(now=start + 80)
        assert db.scalar("SELECT status FROM monitors") == "pending"
        # overdue: goes down, one result
        scheduler.tick(now=start + 100)
        assert db.scalar("SELECT status FROM monitors") == "down"
        assert db.scalar("SELECT COUNT(*) FROM results") == 1
        # next tick shortly after does not add another missing-result
        scheduler.tick(now=start + 110)
        assert db.scalar("SELECT COUNT(*) FROM results") == 1
        scheduler.tick(now=start + 170)
        assert db.scalar("SELECT COUNT(*) FROM results") == 2
        await notifier.wait_idle()

    asyncio.run(scenario())
    assert len(received) == 1
    assert b'"status": "down"' in received[0] or b'"status":"down"' in received[0]
    message = db.scalar("SELECT message FROM results ORDER BY ts LIMIT 1")
    assert message.startswith("Kein Signal seit")


def test_pull_checks_run_when_due(tmp_path):
    db = Database(tmp_path / "t.db")
    monitor_id = _insert_monitor(db, type="tcp", target="127.0.0.1:1", push_token=None, config="{}", interval=30)

    async def scenario():
        scheduler = Scheduler(db, Notifier(db))
        scheduler.tick()
        assert scheduler.is_running(monitor_id)
        scheduler.tick()  # already running -> not started twice
        await asyncio.gather(*scheduler._tasks)
        assert db.scalar("SELECT COUNT(*) FROM results") == 1
        scheduler.tick()  # not due yet
        assert not scheduler._tasks

    asyncio.run(scenario())
    assert db.scalar("SELECT status FROM monitors") == "down"


def test_paused_monitors_are_skipped(tmp_path):
    db = Database(tmp_path / "t.db")
    _insert_monitor(db, enabled=0, status="paused", active_since=0)

    async def scenario():
        Scheduler(db, Notifier(db)).tick()

    asyncio.run(scenario())
    assert db.scalar("SELECT COUNT(*) FROM results") == 0


def test_purge(tmp_path):
    db = Database(tmp_path / "t.db")
    monitor_id = _insert_monitor(db)
    now = time.time()
    for age_days in (1, 40):
        db.execute("INSERT INTO results (monitor_id, ts, ok) VALUES (?, ?, 1)", (monitor_id, now - age_days * 86400))
    assert purge_old_results(db, 30, now) == 1
    assert db.scalar("SELECT COUNT(*) FROM results") == 1
