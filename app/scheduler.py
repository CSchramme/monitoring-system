"""Background loop that runs due pull checks and detects missing push signals."""

import asyncio
import logging
import time
from typing import Any

from .checks import CheckResult, PULL_TYPES, run_check
from .db import Database
from .notifier import Notifier
from .userstore import UserStore
from .service import purge_old_results, push_is_stale, record_result

log = logging.getLogger("monitoring.scheduler")


def _format_age(seconds: float) -> str:
    if seconds < 120:
        return f"{seconds:.0f} s"
    if seconds < 7200:
        return f"{seconds / 60:.0f} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} Tagen"


class Scheduler:
    def __init__(
        self,
        db: Database,
        notifier: Notifier,
        max_concurrent: int = 50,
        retention_days: int = 30,
        users: UserStore | None = None,
    ):
        self.db = db
        self.users = users
        self.notifier = notifier
        self.retention_days = retention_days
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._running: set[int] = set()
        self._tasks: set[asyncio.Task] = set()
        self._stop = asyncio.Event()
        self._main: asyncio.Task | None = None
        self._last_purge = 0.0

    def start(self) -> None:
        self._main = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._main:
            await self._main
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Scheduler tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1)
            except (asyncio.TimeoutError, TimeoutError):
                pass

    def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for monitor in self.db.query("SELECT * FROM monitors WHERE enabled = 1"):
            if monitor["type"] == "push":
                if push_is_stale(monitor, now):
                    age = now - max(monitor["last_push_at"] or 0, monitor["active_since"])
                    result = CheckResult(False, None, f"Kein Signal seit {_format_age(age)}")
                    self.notifier.dispatch(record_result(self.db, monitor["id"], result, now))
            elif monitor["type"] in PULL_TYPES and self._is_due(monitor, now):
                self.run_now(monitor)

        if now - self._last_purge > 3600:
            self._last_purge = now
            deleted = purge_old_results(self.db, self.retention_days, now)
            if deleted:
                log.info("Purged %d old results", deleted)
            if self.users is not None:
                try:
                    self.users.purge_sessions(now)
                except Exception:
                    log.exception("Purging expired sessions failed")

    def is_running(self, monitor_id: int) -> bool:
        return monitor_id in self._running

    def _is_due(self, monitor: dict[str, Any], now: float) -> bool:
        if self.is_running(monitor["id"]):
            return False
        last = monitor["last_check_at"]
        return last is None or now - last >= monitor["interval"]

    def run_now(self, monitor: dict[str, Any]) -> asyncio.Task:
        self._running.add(monitor["id"])
        task = asyncio.get_running_loop().create_task(self._run(monitor))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _run(self, monitor: dict[str, Any]) -> CheckResult:
        try:
            async with self._semaphore:
                result = await run_check(monitor)
            self.notifier.dispatch(record_result(self.db, monitor["id"], result))
            return result
        finally:
            self._running.discard(monitor["id"])
