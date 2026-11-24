"""统一的进度日志行。

所有 App 的自动化都通过这里输出同一格式，控制台的运行日志直接展示：

    12:03:45 | 汽水音乐 | 听歌 | 领取档位 | 点击「待领取」（横向 277 像素，纵向 1067 像素） | 等待 1.0s | 金币 1833 (+360)
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from typing import Callable, Iterator

from droidfleet import log

APP_SODA = "汽水音乐"
APP_DOUYIN = "抖音极速版"
APP_KUAISHOU = "快手极速版"

_COUNTDOWN_THRESHOLD = 30.0
_COUNTDOWN_INTERVAL = 5.0
_WAIT_STATE = threading.local()


def timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def pixel_position(x: int | float, y: int | float) -> str:
    return f"横向 {int(x)} 像素，纵向 {int(y)} 像素"


def format_line(
    app: str,
    task: str | None,
    action: str,
    *,
    tap: tuple[str, int, int] | None = None,
    wait: float | None = None,
    remaining: int | None = None,
    elapsed: float | None = None,
    coins: int | None = None,
    delta: int | None = None,
    extra: str | None = None,
) -> str:
    parts = [timestamp(), app, task or "任务页", action or ""]
    if tap is not None:
        label, x, y = tap
        parts.append(f"点击「{label}」（{pixel_position(x, y)}）")
    if wait is not None and wait > 0:
        parts.append(f"等待 {wait:.1f}s")
    if remaining is not None:
        parts.append(f"剩余 {remaining}s")
    if elapsed is not None:
        parts.append(f"耗时 {elapsed:.1f}s")
    if coins is not None:
        text = f"金币 {coins}"
        if delta is not None:
            text += f" ({delta:+d})"
        parts.append(text)
    if extra:
        parts.append(extra)
    return " | ".join(part for part in parts if part != "")


def emit(app: str, task: str | None, action: str, *, kind: str = "progress",
         level: str = "info", status: str | None = None, **kwargs) -> str:
    line = format_line(app, task, action, **kwargs)
    data = {
        "task": task,
        "action": action,
        "status": status,
        "coins": kwargs.get("coins"),
        "delta": kwargs.get("delta"),
        "extra": kwargs.get("extra"),
        "remaining_seconds": kwargs.get("remaining"),
    }
    wait = kwargs.get("wait")
    if wait is not None:
        data["wait_seconds"] = round(wait, 2)
    elapsed = kwargs.get("elapsed")
    if elapsed is not None:
        data["duration_s"] = round(elapsed, 2)
    tap = kwargs.get("tap")
    if tap is not None:
        label, x, y = tap
        data.update(text=label, x=int(x), y=int(y))
    log.message(kind, line, to=sys.stdout, level=level, line=line,
                **{key: value for key, value in data.items() if value is not None})
    return line


def coin_event(task: str | None, delta: int | None, *, scope: str = "credit") -> None:
    """向控制台发送已确认金额和任务边界，普通命令行仍只显示进度日志。"""
    if os.environ.get("DROIDFLEET_GUI_COIN_EVENTS") != "1" or not task:
        return
    if scope == "credit":
        if type(delta) is not int or delta <= 0:
            return
    elif scope in {"start", "task"}:
        delta = max(0, delta) if type(delta) is int else 0
    else:
        raise ValueError(f"未知金币事件类型：{scope}")
    data = {"task": task, "delta": delta, "scope": scope}
    print("DROIDFLEET_COIN\t" + json.dumps(data, ensure_ascii=False), flush=True)


@contextmanager
def _countdown(app: str, task: str | None, action: str, seconds: float, *,
               exact: bool = False, show_short: bool = False,
               report_if: Callable[[], bool] | None = None) -> Iterator[None]:
    outer = getattr(_WAIT_STATE, "active", None)
    started = time.monotonic()
    if (not math.isfinite(seconds) or seconds < 0 or (not show_short and seconds <= _COUNTDOWN_THRESHOLD)
            or (outer is not None and not exact and started < outer[1])):
        yield
        return

    stopped = threading.Event()
    muted = threading.Event()
    deadline = started + seconds
    label = action if exact else f"{action}（距超时）"

    def report_until_done() -> None:
        next_tick = started + _COUNTDOWN_INTERVAL
        while next_tick < deadline and not stopped.wait(max(0.0, next_tick - time.monotonic())):
            now = time.monotonic()
            if now >= deadline:
                return
            if not muted.is_set() and (report_if is None or report_if()):
                emit(app, task, label, kind="wait.remaining",
                     remaining=math.ceil(deadline - max(now, next_tick)))
            next_tick = max(next_tick + _COUNTDOWN_INTERVAL,
                            started + (math.floor((now - started) / _COUNTDOWN_INTERVAL) + 1) * _COUNTDOWN_INTERVAL)

    worker = threading.Thread(target=report_until_done, name="wait-countdown", daemon=True)
    if outer is not None:
        outer[0].set()
    _WAIT_STATE.active = (muted, deadline)
    try:
        worker.start()
        try:
            yield
        finally:
            stopped.set()
            worker.join()
    finally:
        _WAIT_STATE.active = outer
        if outer is not None:
            outer[0].clear()


def countdown(app: str, task: str | None, action: str, timeout: float) -> AbstractContextManager[None]:
    """等待操作结束，在有较长超时上限时定时显示剩余上限。"""
    return _countdown(app, task, action, timeout)


def wait(app: str, task: str | None, action: str, seconds: float) -> None:
    """等待指定时长，超过 30 秒时每 5 秒报告剩余时间。"""
    with _countdown(app, task, action, seconds, exact=True):
        time.sleep(seconds)


@contextmanager
def ad_countdown(app: str, task: str | None, timeout: float) -> Iterator[Callable[[int], None]]:
    """优先报告手机显示的广告秒数，没有读数时才报告结算超时上限。"""
    visible_until = last_report = -math.inf

    def observe(seconds: int) -> None:
        nonlocal visible_until, last_report
        now = time.monotonic()
        visible_until = now + max(2 * _COUNTDOWN_INTERVAL, seconds)
        if now - last_report >= _COUNTDOWN_INTERVAL:
            last_report = now
            emit(app, task, "广告倒计时", kind="wait.remaining", remaining=seconds)

    with _countdown(app, task, "等待广告结算", timeout,
                    report_if=lambda: time.monotonic() >= visible_until):
        yield observe


def wait_observed_ad(app: str, task: str | None, remaining: int, duration: float) -> None:
    """等待已读到的广告计时器，额外页面缓冲不计入显示的剩余秒数。"""
    with _countdown(app, task, "广告倒计时", remaining, exact=True, show_short=True):
        time.sleep(duration)
