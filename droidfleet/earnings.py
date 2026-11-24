"""Persist before-and-after coin balances for each task run."""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from droidfleet import progress
from droidfleet.rules import load_rules

ROOT = Path(__file__).resolve().parents[1]
APP_LABELS = {
    "soda": progress.APP_SODA,
    "douyin": progress.APP_DOUYIN,
    "kuaishou": progress.APP_KUAISHOU,
}

_CREATE = """
CREATE TABLE IF NOT EXISTS task_earnings (
    id TEXT PRIMARY KEY,
    app TEXT NOT NULL,
    task_key TEXT NOT NULL,
    task TEXT NOT NULL,
    device_serial TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    coins_before INTEGER,
    coins_after INTEGER,
    coins_delta INTEGER,
    duration_seconds REAL,
    status TEXT NOT NULL
)
"""
_UPSERT = """
INSERT INTO task_earnings (
    id, app, task_key, task, device_serial, started_at, finished_at,
    coins_before, coins_after, coins_delta, duration_seconds, status
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    finished_at = excluded.finished_at,
    coins_after = excluded.coins_after,
    coins_delta = excluded.coins_delta,
    duration_seconds = excluded.duration_seconds,
    status = excluded.status
"""


def database_path() -> Path:
    path = os.environ.get("DROIDFLEET_EARNINGS_DB")
    if path:
        selected = Path(path).expanduser()
    else:
        directory = load_rules().get("logging.dir", "logs")
        selected = Path(directory) / "earnings.sqlite3"
    return selected if selected.is_absolute() else ROOT / selected


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class TaskEarnings:
    id: str
    app: str
    task_key: str
    task: str
    serial: str | None
    coins_before: int | None
    path: Path
    started_at: str
    started_monotonic: float
    finished: bool = False
    delta: int | None = None

    def _save(self, status: str, *, finished_at: str | None = None,
              coins_after: int | None = None, duration: float | None = None) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=8)) as db, db:
                db.execute(_CREATE)
                db.execute("CREATE INDEX IF NOT EXISTS earnings_by_task ON task_earnings (app, task_key)")
                db.execute(_UPSERT, (self.id, self.app, self.task_key, self.task, self.serial,
                                     self.started_at, finished_at, self.coins_before, coins_after,
                                     self.delta, duration, status))
        except (OSError, sqlite3.Error) as error:
            progress.emit(APP_LABELS.get(self.app, self.app), self.task, "收益数据库写入失败",
                          kind="earnings.error", level="warn", extra=str(error))

    def finish(self, coins_after: int | None, status: str) -> int | None:
        if self.finished:
            return self.delta

        self.finished = True
        duration = round(time.monotonic() - self.started_monotonic, 2)
        if self.coins_before is not None and coins_after is not None:
            self.delta = coins_after - self.coins_before
        self._save(status, finished_at=_iso_now(), coins_after=coins_after, duration=duration)
        if coins_after is None:
            detail = "当前金币未读到，金币变化未确认"
        elif self.delta is None:
            detail = "任务前金币未读到，金币变化未确认"
        else:
            detail = None
        progress.emit(APP_LABELS.get(self.app, self.app), self.task, "结束时金币", kind="earnings.finish",
                      status=status, coins=coins_after, delta=self.delta, elapsed=duration, extra=detail)
        progress.coin_event(self.task, self.delta, scope="task")
        return self.delta


def start(app: str, task: str, coins_before: int | None, *, task_key: str | None = None,
          serial: str | None = None) -> TaskEarnings:
    run = TaskEarnings(uuid4().hex, app, task_key or task, task, serial, coins_before,
                       database_path(), _iso_now(), time.monotonic())
    run._save("running")
    progress.emit(APP_LABELS.get(app, app), task, "开始前金币", kind="earnings.start", coins=coins_before,
                  extra="当前金币未读到" if coins_before is None else None)
    progress.coin_event(task, 0, scope="start")
    return run
