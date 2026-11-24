#!/usr/bin/env python3
"""抖音极速版自动赚钱脚本（Python 移植版）。

依赖：macOS（Vision OCR 走 osascript/JXA）、adb。
"""

from __future__ import annotations

import atexit
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional, Sequence, Tuple

from droidfleet import earnings, progress
from droidfleet.human import HumanProfile, Observation, confirm_stable, observe_tap, read_after_interruption
from droidfleet.ollama import (
    OllamaFailure, RECOVERY_SCHEMA, RecoveryDecision, decode_recovery, diagnose_screen, image_chat,
    model_settings, recover_screen, risky_dismissal,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 fallback
    ZoneInfo = None  # type: ignore[assignment]

APP_PACKAGE = "com.ss.android.ugc.aweme.lite"
DISMISS_LABELS = ["关闭", "取消", "稍后再说", "以后再说", "暂不", "暂不开启", "我知道了"]
AD_COUNTDOWN_RE = re.compile(r"([0-9]+)秒后可领奖励")

USAGE = """用法
  python3 douyin_rewards.py --help          显示帮助
  python3 douyin_rewards.py --inspect       只检查当前手机页面
  python3 douyin_rewards.py --image FILE    只识别本地截图
  python3 douyin_rewards.py --check-ollama  用纯色测试图检查 Ollama，手机无需连接

执行任务
  python3 douyin_rewards.py --run           依次运行看广告赚金币、连续看视频（限时）
  python3 douyin_rewards.py --run ad        看广告赚金币
  python3 douyin_rewards.py --run shop      逛街赚钱，来回滚动并核对一次任务进度
  python3 douyin_rewards.py --run video     连续看视频赚金币，完成当前档位后停止
  python3 douyin_rewards.py --run target-video  连续刷指定视频赚金币，每批 3 条，按页面提示结算
  python3 douyin_rewards.py --run daily     连续签到、天天领金币、7天签到、天天预约领金币、每天锤一下

实跑时请保持手机解锁，使用已授权的 USB 调试或无线调试连接。看视频最多尝试解锁并领取下一档。
单独执行视频任务时，遇到应用休息提醒会点击「忽略提醒」后继续。
单独执行视频任务时，视频页识别失败会返回任务页同步进度，尝试重进一次。
单独执行视频任务时，低频查看评论或点赞，每批最多尝试视频点赞 1 次、查看评论 2 次、评论点赞 1 次。
每条随机播放 5～15 秒，随机慢滑，每批 10～30 条后回任务页核对进度。
单独执行视频任务时，每批随机回看一次上一条视频，再向前继续。
指定视频按计奖状态换下一条，遇到继续领金币提示时尝试续看一次。
如连接多部设备，用 ANDROID_SERIAL 选择目标设备。
AI 使用脚本旁 .env 中的 DROIDFLEET_MODEL 和 DROIDFLEET_OLLAMA_URL，支持本机或私有局域网地址。
环境变量可临时覆盖 .env。
运行日志使用北京时间，金币字段显示最近一次从任务页读到的余额。
每日任务按应用内状态跳过已完成、无入口或要求认证、提交资料、开通服务的项目。
每日任务结束时汇总逐项结果和本轮金币净变化。
退出码 0 表示成功或只读检查完成，1 表示流程中止，2 表示有任务未确认完成。"""


class RunError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class CommandFailure(Exception):
    def __init__(self, program: str, status: int) -> None:
        super().__init__(f"{program} 执行失败（{status}）")
        self.program = program
        self.status = status


class ForegroundError(Exception):
    def __init__(self, package: Optional[str]) -> None:
        super().__init__(f"抖音极速版没有取得手机前台焦点（{package or '切换中'}）")
        self.package = package


class ScreenRecognitionError(Exception):
    def __init__(self, stage: str) -> None:
        super().__init__(f"{stage}：未识别到预期界面")
        self.stage = stage


class UnresolvedScreen(ScreenRecognitionError):
    """异常恢复结束后，当前页面仍需人工确认。"""

class DailyTaskSkipped(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TaskKind(str, Enum):
    ad = "ad"
    shop = "shop"
    video = "video"
    target_video = "target-video"

    @property
    def label(self) -> str:
        return {
            TaskKind.ad: "看广告赚金币",
            TaskKind.shop: "逛街赚钱",
            TaskKind.video: "连续看视频",
            TaskKind.target_video: "连续刷指定视频赚金币",
        }[self]

    @property
    def buttons(self) -> List[str]:
        return {
            TaskKind.ad: ["去领取"],
            TaskKind.shop: ["去逛街"],
            TaskKind.video: ["去看视频", "立即领取"],
            TaskKind.target_video: ["去领取"],
        }[self]


class DailyTask(str, Enum):
    sign_in = "连续签到"
    wallet = "天天领金币"
    week = "7天签到"
    reservation = "天天预约领金币"
    hammer = "每天锤一下"

    @property
    def entry_buttons(self) -> List[str]:
        return {
            DailyTask.sign_in: ["去签到"],
            DailyTask.wallet: ["去领取", "去完成", "领取", "去看看"],
            DailyTask.week: ["去领取"],
            DailyTask.reservation: ["去领取", "去预约", "去完成"],
            DailyTask.hammer: ["锤一下", "去锤", "去完成"],
        }[self]


def matches(text: str, pattern: str) -> bool:
    return re.search(pattern, text) is not None


@dataclass(frozen=True)
class ScreenText:
    """OCR 识别到的一行文字，坐标归一化且以截图左上角为原点。"""

    text: str
    bounds: Tuple[float, float, float, float]  # x, y, 宽, 高

    @property
    def x(self) -> float:
        return self.bounds[0] + self.bounds[2] / 2

    @property
    def y(self) -> float:
        return self.bounds[1] + self.bounds[3] / 2

    @property
    def min_x(self) -> float:
        return self.bounds[0]

    @property
    def min_y(self) -> float:
        return self.bounds[1]

    @property
    def max_x(self) -> float:
        return self.bounds[0] + self.bounds[2]

    @property
    def max_y(self) -> float:
        return self.bounds[1] + self.bounds[3]

    def pixel_bounds(self, width: int, height: int) -> Tuple[float, float, float, float]:
        return (self.min_x * width, self.min_y * height, self.max_x * width, self.max_y * height)


_BEIJING = ZoneInfo("Asia/Shanghai") if ZoneInfo is not None else None


def _timestamp() -> str:
    if _BEIJING is not None:
        moment = datetime.now(_BEIJING)
    else:  # pragma: no cover - Python 3.8 fallback
        moment = datetime.now(timezone(timedelta(hours=8)))
    return moment.strftime("%Y-%m-%d %H:%M:%S")


class TaskLog:
    def __init__(self) -> None:
        self.task: Optional[TaskKind] = None
        self.label: Optional[str] = None
        self.balance: Optional[int] = None
        self.progress: Optional[str] = None

    def begin(self, task: Optional[TaskKind], label: Optional[str] = None) -> None:
        self.task = task
        self.label = label if label is not None else (task.label if task is not None else None)
        self.progress = None

    def observe(self, screen: "Screen", announce: bool = False) -> None:
        coins = screen.balance()
        if coins is not None:
            self.balance = coins
        value: Optional[str]
        if self.task == TaskKind.video:
            video = screen.video_progress()
            value = f"{video[0]}/{video[1]}" if video is not None else None
        elif self.task == TaskKind.shop:
            seconds = screen.shop_seconds()
            value = f"剩余 {seconds} 秒" if seconds is not None else screen.shopping_progress_text()
        elif self.task == TaskKind.ad:
            if screen.ad_finished():
                value = "广告页显示领取成功"
            else:
                badge = screen.ad_badge()
                value = badge.text if badge is not None else None
        else:
            value = None
        self.set_progress(value, announce=announce)

    def set_progress(self, value: Optional[str], announce: bool = True) -> None:
        if value is None or value == self.progress:
            return
        self.progress = value
        if announce:
            self.write()

    def credit(self, delta: int) -> None:
        self.write(f"确认到账 +{delta} 金币")
        task = DOUYIN_TASKS.get(self.task.value) if self.task is not None else None
        progress.coin_event(task[0] if task else self.label, delta)

    def write(
        self,
        message: Optional[str] = None,
        warning: bool = False,
        to: Optional[Any] = None,
    ) -> None:
        detail = " ".join(message.splitlines()) if message is not None else "进度"
        extra = f"进度[{self.progress}]" if self.progress else None
        if warning:
            extra = (extra + " " if extra else "") + "告警"
        line = progress.format_line(
            progress.APP_DOUYIN,
            self.label or "赚钱任务",
            detail,
            coins=self.balance,
            extra=extra,
        )
        try:
            from droidfleet import log as run_log

            run_log.message("task", line, level="warn" if warning else "info",
                            to=to if to is not None else sys.stdout, line=line, warning=warning,
                            coins=self.balance, progress=self.progress, task=self.label)
        except Exception:
            print(line, file=to if to is not None else sys.stdout)


task_log = TaskLog()


from droidfleet.ocr import helper_args, helper_path


_TEMP_DIR: Optional[str] = None
_GRAY_CACHE: dict = {}


def _temp_dir() -> str:
    global _TEMP_DIR
    if _TEMP_DIR is None:
        _TEMP_DIR = tempfile.mkdtemp(prefix="droidfleet-douyin-")
        atexit.register(shutil.rmtree, _TEMP_DIR, True)
    return _TEMP_DIR


def _temp_png(png: bytes) -> str:
    handle, path = tempfile.mkstemp(suffix=".png", dir=_temp_dir())
    with os.fdopen(handle, "wb") as stream:
        stream.write(png)
    return path


def _ocr_helper(args: Sequence[str]) -> dict:
    started = time.monotonic()
    result = subprocess.run([sys.executable, str(helper_path()), *helper_args(args)], capture_output=True, timeout=15)
    try:
        from droidfleet import log as run_log

        run_log.event("ocr", mode=args[0] if args else "?", args=list(args[1:2]),
                      returncode=result.returncode, ms=int((time.monotonic() - started) * 1000))
    except Exception:
        pass
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise RunError(detail or "OCR 助手执行失败")
    try:
        return json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunError(f"OCR 助手返回了无效数据：{error}") from error


def _format_number(value: float) -> str:
    return repr(float(value))


def _integral_rect(x: float, y: float, width: float, height: float) -> Tuple[float, float, float, float]:
    min_x = math.floor(x)
    min_y = math.floor(y)
    max_x = math.ceil(x + width)
    max_y = math.ceil(y + height)
    return (float(min_x), float(min_y), float(max_x - min_x), float(max_y - min_y))


def _payload_lines(payload: dict) -> List[ScreenText]:
    return [
        ScreenText(
            str(line["text"]),
            (float(line["x"]), float(line["y"]), float(line["w"]), float(line["h"])),
        )
        for line in payload["lines"]
    ]


def _contains(outer: Tuple[float, float, float, float], inner: Tuple[float, float, float, float]) -> bool:
    return (outer[0] <= inner[0] and outer[1] <= inner[1]
            and outer[0] + outer[2] >= inner[0] + inner[2]
            and outer[1] + outer[3] >= inner[1] + inner[3])


def _expanded_contains(
    bounds: Tuple[float, float, float, float],
    point: Tuple[float, float],
    dx: float,
    dy: float,
) -> bool:
    return (bounds[0] - dx <= point[0] <= bounds[0] + bounds[2] + dx
            and bounds[1] - dy <= point[1] <= bounds[1] + bounds[3] + dy)


class Screen:
    def __init__(self, png: bytes) -> None:
        self.png = png
        self.path = _temp_png(png)
        payload = _ocr_helper(["ocr", self.path])
        self.width = int(payload["width"])
        self.height = int(payload["height"])
        self.lines = _payload_lines(payload)

    # ---- 像素采样 -------------------------------------------------------

    def gray(self, bounds: Tuple[float, float, float, float], size: int = 24) -> Optional[List[int]]:
        key = (self.path, size, bounds)
        cached = _GRAY_CACHE.get(key, _GRAY_CACHE)
        if cached is not _GRAY_CACHE:
            return cached  # type: ignore[return-value]
        spec = ",".join(_format_number(value) for value in bounds)
        payload = _ocr_helper(["probe", self.path, str(size), spec])
        patch = payload["patches"][0]
        value = [int(item) for item in patch] if patch is not None else None
        if len(_GRAY_CACHE) > 2048:
            _GRAY_CACHE.clear()
        _GRAY_CACHE[key] = value
        return value

    def shopping_badge_text(self) -> Optional[ScreenText]:
        if not self.shopping_products():
            return None
        rect = _integral_rect(0.81 * self.width, 0.748 * self.height, 0.11 * self.width, 0.048 * self.height)
        try:
            payload = _ocr_helper(["crop", self.path, *(_format_number(value) for value in rect)])
        except RunError:
            return None
        lines = _payload_lines(payload)
        return next((line for line in lines if matches(line.text, "^[0-9]{1,3}秒$")), None)

    def read_shopping_badge(self) -> Optional[int]:
        badge = self.shopping_badge_text()
        if badge is None:
            return None
        try:
            return int(badge.text[:-1])
        except ValueError:
            return None

    # ---- 任务页 ---------------------------------------------------------

    @property
    def return_to_top(self) -> Optional[ScreenText]:
        return next((line for line in self.lines if line.text == "返回顶部" and line.y < 0.12 and line.x > 0.7), None)

    def task_page(self) -> bool:
        if any(line.text == "赚钱任务" and line.y < 0.12 for line in self.lines):
            return True
        return self.return_to_top is not None and self._task_coin_header() is not None

    def _task_coin_header(self) -> Optional[ScreenText]:
        # Windows OCR 会把左上角返回箭头粘进「金币收益」，允许前缀符号
        return next(
            (line for line in self.lines
             if line.x < 0.5 and line.y < 0.15
             and matches(line.text.lstrip("〈<‹＜ "), "^金[币市]收益")),
            None,
        )

    def balance(self) -> Optional[int]:
        if getattr(self, "_balance_checked", False):
            return getattr(self, "_balance_value", None)
        self._balance_checked = True
        self._balance_value = self._read_balance()
        return self._balance_value

    def _read_balance(self) -> Optional[int]:
        if not self.task_page():
            return None
        header = self._task_coin_header()
        if header is None:
            return None
        text = header.text.lstrip("〈<‹＜ ")
        direct = re.search(r"金[币市]收益[:：]?\s*([0-9]+)", text)
        if direct is not None:
            return int(direct.group(1))
        line = next(
            (item for item in self.lines
             if item.x < 0.4 and 0.13 <= item.y <= 0.20 and matches(item.text, "^[0-9]+$")),
            None,
        )
        if line is not None:
            try:
                return int(line.text)
            except ValueError:
                return None
        # 中文 OCR 常常整段漏读彩色大号数字（例如余额只有两位数时），
        # 裁出金币数字区域，用英文引擎专门再读一次
        try:
            rect = _integral_rect(
                0.01 * self.width,
                (header.y - 0.015) * self.height,
                0.45 * self.width,
                0.10 * self.height,
            )
            payload = _ocr_helper(["num", self.path, *(_format_number(value) for value in rect)])
        except RunError:
            return None
        for item in _payload_lines(payload):
            candidate = item.text.strip()
            if item.x < 0.40 and re.fullmatch(r"[0-9]{1,9}", candidate):
                return int(candidate)
        return None

    def shopping_progress_text(self) -> Optional[str]:
        if not self.task_page():
            return None
        row = next((line for line in self.lines if line.text.startswith("逛街赚钱")), None)
        if row is None:
            return None
        found = next(
            (line for line in self.lines
             if line.x < 0.65 and line.y >= row.y and line.y - row.y < 0.09
             and matches(line.text, "完成[0-9]+/[0-9]+次")),
            None,
        )
        return found.text if found is not None else None

    def shopping_progress(self) -> Optional[int]:
        text = self.shopping_progress_text()
        if text is None:
            return None
        head = text[2:].split("/")[0]
        try:
            return int(head)
        except ValueError:
            return None

    def video_progress(self) -> Optional[Tuple[int, int]]:
        if not self.task_page():
            return None
        row = self.task_row(TaskKind.video)
        if row is None:
            return None
        progress = next(
            (line for line in self.lines
             if line.y > row.y and line.y - row.y < 0.15 and matches(line.text, "^[0-9]+/[0-9]+$")),
            None,
        )
        if progress is None:
            return None
        parts = progress.text.split("/")
        if len(parts) < 2:
            return None
        try:
            current = int(parts[0])
            goal = int(parts[1])
        except ValueError:
            return None
        if goal > 0 and current <= goal and len(parts[1]) <= 4:
            return (current, goal)
        # Windows OCR 会把右侧卡片的金币数粘在进度后面（例如 39/328 + 8994 → 39/3288994），
        # 裁出第一张卡的范围，用英文引擎只读这一格的数字。
        return self._crop_video_progress(progress)

    def _crop_video_progress(self, progress: ScreenText) -> Optional[Tuple[int, int]]:
        top = max(0.0, progress.min_y - 0.010)
        height = progress.bounds[3] + 0.020
        for width in (0.175, 0.145, 0.20, 0.13):
            left = max(0.0, progress.min_x - 0.015)
            try:
                rect = _integral_rect(
                    left * self.width,
                    top * self.height,
                    width * self.width,
                    height * self.height,
                )
                payload = _ocr_helper(["num", self.path, *(_format_number(value) for value in rect)])
            except RunError:
                return None
            for item in _payload_lines(payload):
                parts = item.text.split("/")
                if len(parts) != 2 or not all(part.isdigit() for part in parts):
                    continue
                current, goal = int(parts[0]), int(parts[1])
                if 0 < goal and current <= goal:
                    return (current, goal)
        return None

    def earning_tab(self) -> Optional[ScreenText]:
        left = any(
            line.text in ("首页", "首页朋友") and line.min_x < 0.15 and line.x < 0.30 and 0.88 <= line.y <= 0.95
            for line in self.lines
        )
        right = any(
            line.text.startswith("消息") and line.x > 0.60 and 0.88 <= line.y <= 0.95
            for line in self.lines
        )
        if not (left and right):
            return None
        return next(
            (line for line in self.lines
             if line.text == "赚钱" and 0.38 <= line.x <= 0.62 and 0.86 <= line.y <= 0.95),
            None,
        )

    def on_video_feed(self) -> bool:
        """推荐视频页：底部有「首页/朋友/消息」，中间的「赚钱」是红底图标 OCR 读不到。"""
        if self.daily_text(r"^全屏观看$", y=(0.45, 0.75)) is not None:
            return True
        # Windows OCR 在深色视频上很不稳定：有时只认出「首页」，有时只认出「朋友」。
        # 底部这一排 tab 只要认出任意一个，就按推荐视频页处理。
        tab_labels = {"首页", "朋友", "同城", "赚钱", "消息", "我"}
        for line in self.lines:
            if not (0.88 <= line.y <= 0.95 and 0.02 <= line.x <= 0.98):
                continue
            label = line.text[:2] if line.text.startswith("消息") else line.text
            if label in tab_labels:
                return True
        return False

    def feed_progress(self) -> Optional[int]:
        if self.earning_tab() is None and not self.on_video_feed():
            return None
        line = next(
            (item for item in self.lines
             if matches(item.text, "^[0-9]{1,4}$")
             and ((0.06 <= item.x <= 0.15 and 0.16 <= item.y <= 0.20)     # 旧版左上角计数
                  or (item.x > 0.87 and 0.13 <= item.y <= 0.22))),        # 新版右上角红包计数
            None,
        )
        if line is None:
            return None
        try:
            return int(line.text)
        except ValueError:
            return None

    # ---- 指定视频 -------------------------------------------------------

    def target_video_back(self) -> Optional[ScreenText]:
        return next(
            (line for line in self.lines
             if line.text == "返回" and line.x < 0.25 and 0.035 <= line.y <= 0.11),
            None,
        )

    def target_video_badge(self) -> Optional[ScreenText]:
        if self.task_page():
            return None
        badge = next(
            (line for line in self.lines
             if line.x > 0.75 and 0.12 <= line.y <= 0.24
             and ("赚钱中" in line.text or "已暂停" in line.text)),
            None,
        )
        if badge is None:
            return None
        # OCR 常把「2186 金币 已暂停」粘成一行，统一成状态文案给上层比较
        state = "赚钱中" if "赚钱中" in badge.text else "已暂停"
        badge = ScreenText(state, badge.bounds)
        has_reward_label = any(
            line.x > 0.79 and 0.13 <= line.y <= 0.29
            and (line.text == "金币" or matches(line.text, "^观看[0-9]{1,3}秒$"))
            for line in self.lines
        )
        return badge if self.target_video_back() is not None or has_reward_label else None

    def target_video_watch_seconds(self) -> Optional[int]:
        if self.target_video_badge() is None:
            return None
        line = next(
            (item for item in self.lines
             if item.x > 0.79 and 0.20 <= item.y <= 0.29 and matches(item.text, "^观看[0-9]{1,3}秒$")),
            None,
        )
        if line is None:
            return None
        try:
            return int(line.text[2:-1])
        except ValueError:
            return None

    def target_video_coins(self) -> Optional[int]:
        badge = self.target_video_badge()
        if badge is None:
            return None
        unit = next(
            (line for line in self.lines
             if line.text == "金币" and abs(line.x - badge.x) < 0.07
             and 0.004 <= badge.y - line.y <= 0.045),
            None,
        )
        if unit is None:
            return None
        value = next(
            (line for line in self.lines
             if abs(line.x - unit.x) < 0.07 and 0.003 <= unit.y - line.y <= 0.035
             and matches(line.text, "^[0-9]+$")),
            None,
        )
        if value is None:
            return None
        try:
            return int(value.text)
        except ValueError:
            return None

    def target_video_offer(self) -> Optional[Tuple[ScreenText, int]]:
        title = next(
            (line for line in self.lines
             if matches(line.text, "^再看[0-9]{1,2}秒最高可得") and 0.30 <= line.y <= 0.50),
            None,
        )
        if title is None:
            return None
        digits = re.match(r"[0-9]+", title.text[2:])
        if digits is None:
            return None
        try:
            seconds = int(digits.group(0))
        except ValueError:
            return None
        if not (0 < seconds <= 60):
            return None
        button = next(
            (line for line in self.lines
             if line.text == "继续领金币" and 0.32 <= line.x <= 0.68 and 0.45 <= line.y <= 0.66),
            None,
        )
        if button is None:
            return None
        keep_watching = any(
            line.text == "坚持退出" and 0.32 <= line.x <= 0.68 and 0.53 <= line.y <= 0.74
            for line in self.lines
        )
        if not keep_watching:
            return None
        return (button, seconds)

    # ---- 通用状态 -------------------------------------------------------

    def rest_reminder(self) -> bool:
        return any("已到达你设置的休息时间" in line.text for line in self.lines)

    def video_reward(self) -> bool:
        return any("恭喜获得看视频奖励" in line.text for line in self.lines)

    def shopping_task_panel(self) -> bool:
        return (any("任务面板" in line.text for line in self.lines)
                and any("逛街得奖励" in line.text for line in self.lines))

    def shopping_done_panel(self) -> bool:
        return self.shopping_task_panel() and any(line.text == "已完成" for line in self.lines)

    def ad_badge(self) -> Optional[ScreenText]:
        return next(
            (line for line in self.lines
             if line.x > 0.58 and line.y < 0.12
             and (line.text.startswith("领取成功") or AD_COUNTDOWN_RE.search(line.text))),
            None,
        )

    def ad_seconds(self) -> Optional[int]:
        badge = self.ad_badge()
        if badge is None or badge.text.startswith("领取成功"):
            return None
        countdown = AD_COUNTDOWN_RE.search(badge.text)
        return int(countdown.group(1)) if countdown is not None else None

    def ad_finished(self) -> bool:
        badge = self.ad_badge()
        return badge is not None and badge.text.startswith("领取成功")

    def ad_countdown(self) -> bool:
        return self.ad_badge() is not None and not self.ad_finished()

    def ad_exit_button(self) -> Optional[ScreenText]:
        if not self.ad_finished():
            return None
        if not any("再看一个视频额外获得" in line.text for line in self.lines):
            return None
        return next(
            (line for line in self.lines
             if line.text == "坚持退出" and 0.35 <= line.x <= 0.65 and 0.50 <= line.y <= 0.73),
            None,
        )

    def reward_rejected(self) -> bool:
        return any(matches(line.text, "金币[溜滑流]走") for line in self.lines)

    def shop_seconds(self) -> Optional[int]:
        if self.task_page():
            return None
        if not (self.shopping_header() or self.shopping_products()):
            return None
        timer = next(
            (line for line in self.lines
             if line.x > 0.78 and 0.68 <= line.y <= 0.85 and matches(line.text, "^[0-9]{1,3}秒$")),
            None,
        )
        if timer is None:
            return None
        try:
            return int(timer.text[:-1])
        except ValueError:
            return None

    def shopping_header(self) -> bool:
        return any(
            line.text == "逛街得金币" and 0.30 <= line.x <= 0.70 and 0.07 <= line.y <= 0.24
            for line in self.lines
        )

    def shopping_products(self) -> bool:
        return not self.task_page() and any(line.text == "立即购买" for line in self.lines)

    def shopping_page(self) -> bool:
        return not self.task_page() and (self.shopping_header() or self.shop_seconds() is not None)

    def shopping_ended(self) -> bool:
        return (self.shopping_done_panel() or self.network_error() or self.reward_rejected()
                or self.shop_seconds() == 0)

    def network_error(self) -> bool:
        return any("网络错误" in line.text for line in self.lines)

    def popup_suspected(self) -> bool:
        for line in self.lines:
            if not (0.14 <= line.y <= 0.91):
                continue
            if (line.text in DISMISS_LABELS or "签到领" in line.text
                    or "连续签到赚大钱" in line.text or line.text == "任务面板"):
                return True
        if self.task_page():
            for x in (0.23, 0.74):
                patch = self.gray((x, 0.050, 0.04, 0.012), 8)
                if patch is None or max(patch) >= 195:
                    return False
            return True
        badge = self.ad_badge()
        labels = [badge] if badge is not None else [line for line in self.lines if line.text == "立即购买"]
        if not labels:
            return False
        for label in labels:
            patch = self.gray(label.bounds)
            if patch is None or max(patch) >= 195:
                return False
        return True

    def task_row(self, task: TaskKind) -> Optional[ScreenText]:
        if not self.task_page():
            return None
        return next(
            (line for line in self.lines if line.x < 0.65 and line.text.startswith(task.label)),
            None,
        )

    def button_for(self, task: TaskKind) -> Optional[ScreenText]:
        if self.balance() is None or self.popup_suspected():
            return None
        row = self.task_row(task)
        if row is None:
            return None
        if task == TaskKind.video:
            # 任务卡右上角的「立即领取」一次领掉已完成的档位，优先点它
            top_claim = [
                line for line in self.lines
                if line.text in ("立即领取",) and line.x > 0.74 and abs(line.y - row.y) < 0.05
            ]
            if len(top_claim) == 1:
                return top_claim[0]
            claims = [
                line for line in self.lines
                if line.text == "领取" and 0.05 <= line.x <= 0.76 and 0.07 <= line.y - row.y <= 0.16
            ]
            if len(claims) == 1:
                return claims[0]
        found = [
            line for line in self.lines
            if line.x > 0.74 and abs(line.y - row.y) < 0.05 and line.text in task.buttons
        ]
        return found[0] if len(found) == 1 else None

    # ---- 每日任务页面 ---------------------------------------------------

    def daily_text(
        self,
        pattern: str,
        x: Tuple[float, float] = (0.0, 1.0),
        y: Tuple[float, float] = (0.0, 1.0),
    ) -> Optional[ScreenText]:
        return next(
            (line for line in self.lines
             if x[0] <= line.x <= x[1] and y[0] <= line.y <= y[1] and matches(line.text, pattern)),
            None,
        )

    def daily_sign_in_panel(self) -> bool:
        # 标题是艺术字，OCR 时好时坏；用面板里的固定文案兜底
        return (self.daily_text("连续签到赚大钱", y=(0.20, 0.45)) is not None
                or self.daily_text(r"^已经连续签到", y=(0.20, 0.60)) is not None
                or self.daily_text(r"^签到提醒$", y=(0.55, 0.75)) is not None)

    def daily_wallet_page(self) -> bool:
        return (self.daily_text("的钱包", y=(0.03, 0.09)) is not None
                and self.daily_text("^天天领金币$|^可得[0-9]+金[币市]领金币$", x=(0.05, 0.35), y=(0.09, 0.72)) is not None)

    def daily_wallet_card(self) -> Optional[ScreenText]:
        if not self.daily_wallet_page():
            return None
        return self.daily_text("^累计金[币市]收益", x=(0.05, 0.45), y=(0.13, 0.80))

    def daily_wallet_offer(self) -> Optional[Tuple[float, float]]:
        if not self.daily_wallet_page():
            return None
        offer = self.daily_text("查看取现额度领奖励", y=(0.30, 0.45))
        if offer is not None:
            # 钱包页的任务行右侧有「去查看」，这是正常列表；只有浮层才需要关闭
            row_button = any(
                line.text in ("去查看", "去领取", "去完成")
                and line.x > 0.74 and abs(line.y - offer.y) < 0.06
                for line in self.lines
            )
            if not row_button:
                return (0.50, 0.69)
        if self.daily_text("乘车码开通福利", y=(0.28, 0.63)) is not None:
            return (0.069, 0.431)
        return None

    def daily_week_page(self) -> bool:
        return (self.daily_text("每日攒[金全]币", y=(0.06, 0.22)) is not None
                and self.daily_text("领今日金币", y=(0.44, 0.68)) is not None)

    def daily_week_receipt(self) -> bool:
        return (self.daily_text("今日金币奖励", y=(0.17, 0.74)) is not None
                and self.daily_text("已到[账帐]", y=(0.17, 0.74)) is not None
                and (self.daily_week_page() or self.daily_text("^查看奖励$", y=(0.33, 0.85)) is not None))

    def daily_reservation_page(self) -> bool:
        return self.daily_text("^天天预约领金币$", y=(0.07, 0.38)) is not None

    def daily_reservation_gift(self) -> bool:
        # 任务页上会随任务弹出「恭喜获得预约礼包 / 今日预约 明日领奖」浮层。
        # 标题是艺术字，OCR 只能读出「侦约礼包」这类残字，按钮也常读不到，
        # 所以用「预约/礼包残字 + 页面变暗 + 固定位置确实有 ×」三重确认。
        if not self.popup_suspected():
            return False
        hint = (self.daily_text(r"预约|礼包", x=(0.15, 0.85), y=(0.22, 0.45)) is not None
                or self.daily_text(r"^[0-9]+金币$", x=(0.38, 0.62), y=(0.46, 0.64)) is not None)
        return hint and close_icon((0.502, 0.769), self) is not None

    def daily_reservation_booked(self) -> bool:
        return (self.daily_reservation_page()
                and self.daily_text(
                    "^恭喜预约成功$|^已预约[，,:：]?(明日|[0-9]{1,2}[:：][0-9]{2}[:：][0-9]{2}后可领)",
                    y=(0.33, 0.83)) is not None)

    def daily_reservation_button(self) -> Optional[ScreenText]:
        if not self.daily_reservation_page():
            return None
        if self.daily_text("^今日预约[，,]?明日可领", x=(0.25, 0.75), y=(0.40, 0.86)) is None:
            return None
        return self.daily_text("^立即预约领取$", x=(0.25, 0.75), y=(0.36, 0.86))

    def daily_reservation_claim(self) -> Optional[ScreenText]:
        if not self.daily_reservation_page():
            return None
        if self.daily_reservation_booked() or self.daily_reservation_button() is not None:
            return None
        if self.daily_text("^已预约[，,]?(今日可领|24点前可领)", x=(0.25, 0.75), y=(0.47, 0.88)) is None:
            return None
        return self.daily_text("^直接领", x=(0.30, 0.70), y=(0.40, 0.85))

    def daily_reservation_receipt(self) -> bool:
        return (self.daily_reservation_page()
                and self.daily_text("^(金币领取成功|领取金币成功|领取成功|恭喜.*获得.*金币)",
                                    x=(0.25, 0.75), y=(0.19, 0.65)) is not None
                and self.daily_text("^(继续预约明日金币|开心收下)$", x=(0.25, 0.75), y=(0.35, 0.88)) is not None)

    def daily_hammer_page(self) -> bool:
        return self.daily_text("^每天锤一下$", y=(0.05, 0.36)) is not None

    def daily_hammer_reminder(self) -> bool:
        return self.daily_hammer_page() and self.daily_text("每天都来锤哦", y=(0.22, 0.40)) is not None

    def daily_blocked_reason(self) -> Optional[str]:
        if self.task_page() and not self.popup_suspected():
            return None
        sensitive = (
            "实名认证|身份认证|实名验证|人脸(识别|核验|认证)|身份信息核验|身份验证|"
            "上传(本人|您的)?(身份|证件|资料|材料|个人)|提交(资料|材料)|填写(个人|身份|实名|资料)|拍摄身份证"
        )
        gate = (
            "^(?:(?:请先|请|需先|需要|需|先|完成|进行|去|开始|立即|前往|继续))*(?:" + sensitive + ")|"
            "^(领取前|为保障).*(?:" + sensitive + ")"
        )
        in_wallet = self.daily_wallet_page()
        asks_for_identity = False
        for line in self.lines:
            if not (0.035 <= line.y <= 0.92 and matches(line.text, gate)):
                continue
            if (in_wallet and line.y < 0.095 and 0.35 <= line.x <= 0.60
                    and matches(line.text, "^请上传证件照片[〉＞>❯]$")):
                continue
            asks_for_identity = True
            break
        if asks_for_identity or self.daily_text(
            "^请输入(真实姓名|姓名|身份证号|证件号码|银行卡号|短信验证码)|^身份证(人像面|国徽面|正面|反面)"
        ) is not None:
            return "需实名认证、核验身份或提交资料"
        risky = (self.daily_text(
            "^((同意并|确认)(开通|授权|申请|借款|支付)|征信授权|绑定银行卡|立即开通(抖音月付|乘车码))"
        ) is not None) or (self.daily_text(
            "^(申请贷款|申请信用卡|开通抖音月付|借款申请|提现|银行卡管理)$", x=(0.25, 0.75), y=(0.03, 0.17)
        ) is not None)
        if not self.task_page() and self.daily_wallet_offer() is None and risky:
            return "涉及开通、授权、金融或提现操作"
        binding = re.compile(
            r"(?:去|立即|马上|先)?(?:绑定|关联)(?:银行卡|信用卡|储蓄卡|借记卡|手机号|账号|账户|支付|抖音号)?"
            r"|(?:上传|拍摄|提交|填写|认证|验证|核验).{0,6}(?:驾照|驾驶证|行驶证|身份证|证件|银行卡|信用卡|车牌|车辆信息)"
            r"|(?:驾照|驾驶证|行驶证|车牌|车辆信息|银行卡|信用卡|储蓄卡|借记卡|卡号).{0,4}(?:认证|绑定|验证|上传|核验)"
            r"|驾照|驾驶证|行驶证"
            r"|^(?:绑定银行卡|添加银行卡|上传证件|上传驾照|去认证|去绑定|立即绑定)$"
        )
        binding_hit = any(
            0.035 <= line.y <= 0.92 and binding.search(line.text) is not None
            for line in self.lines
        )
        if not self.task_page() and self.daily_wallet_offer() is None and binding_hit:
            return "涉及绑定、银行卡或驾照等任务"
        return None

    def daily_entry(self, task: DailyTask) -> Optional[Tuple[Optional[ScreenText], Optional[str]]]:
        if not self.task_page() or self.popup_suspected():
            return None
        if task == DailyTask.sign_in:
            title = "^(签到|连续签到)(?:[¥￥]?[0-9]+)?$|^已连续签到[0-9]+天$"
            completed = "^(今日已签到|已签到)"
        elif task == DailyTask.wallet:
            title = "^天天领金币"
            completed = "今日已领|明日再领|明日签到立即领"
        elif task == DailyTask.week:
            title = "^7天签到"
            completed = "今日已领|明日再来"
        elif task == DailyTask.reservation:
            title = "^(天天预约领金币|预约领金币|明日0点领金币|领金币$)"
            completed = "已预约[，,]?(明日|[0-9]{1,2}[:：][0-9]{2}[:：][0-9]{2}后可领)"
        else:
            title = "^(每天锤一下|今日[未已]锤)"
            completed = "今日已锤"
        row = self.daily_text(title, x=(0.0, 0.66), y=(0.10, 0.76))
        if row is None:
            return None
        nearby = [line for line in self.lines if line.x <= 0.68 and 0.0 <= line.y - row.y <= 0.07]
        if (task == DailyTask.reservation and row.text == "领金币"
                and not any("已预约" in line.text and "24点前" in line.text for line in nearby)):
            return None
        if any(matches(line.text, "(需|请先|完成|需先).*(实名认证|身份认证|上传|人脸)|上传(证件|身份证|资料)")
               for line in nearby):
            return (None, "需认证或上传资料")
        status = next((line for line in nearby if matches(line.text, completed)), None)
        if status is not None:
            return (None, status.text)
        if task == DailyTask.reservation and any(
            line.x > 0.74 and abs(line.y - row.y) < 0.047 and line.text == "已预约"
            for line in self.lines
        ):
            return (None, "已预约")
        buttons = [
            line for line in self.lines
            if line.x > 0.74 and abs(line.y - row.y) < 0.047 and line.text in task.entry_buttons
        ]
        if len(buttons) != 1:
            return None
        return (buttons[0], None)


# ---- adb ----------------------------------------------------------------


def local_argv(executable: str, arguments: Sequence[str]) -> list[str]:
    """macOS/Linux 用 /usr/bin/env 在 PATH 里查命令；Windows 直接执行第一个参数。"""
    if sys.platform == "win32" and executable == "/usr/bin/env":
        return list(arguments)
    return [executable, *arguments]


def command(executable: str, arguments: Sequence[str], input_data: Optional[bytes] = None,
            *, timeout: float | None = None) -> bytes:
    argv = local_argv(executable, arguments)
    try:
        completed = subprocess.run(
            argv,
            input=input_data,
            stdout=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise RunError(f"找不到可执行文件：{executable}") from error
    if completed.returncode != 0:
        program = arguments[0] if executable == "/usr/bin/env" and arguments else os.path.basename(executable)
        raise CommandFailure(program, completed.returncode)
    return completed.stdout


def device_serial() -> Optional[str]:
    return os.environ.get("ADB_SERIAL") or os.environ.get("ANDROID_SERIAL")


def adb(*arguments: str) -> bytes:
    from droidfleet import log as run_log

    serial = device_serial()
    device_args = ["-s", serial] if serial else []
    started = time.monotonic()
    timeout = 90
    try:
        with progress.countdown(progress.APP_DOUYIN, task_log.label, "等待 ADB 响应", timeout):
            output = command("/usr/bin/env", ["adb", *device_args, *arguments], timeout=timeout)
    except subprocess.TimeoutExpired as error:
        run_log.adb_result(args=list(arguments), returncode=124, reason="ADB 响应超时",
                           ms=int((time.monotonic() - started) * 1000))
        raise CommandFailure("adb 超时", 124) from error
    except CommandFailure as error:
        run_log.adb_result(args=list(arguments), returncode=error.status,
                           ms=int((time.monotonic() - started) * 1000), reason=str(error))
        raise
    except Exception as error:
        run_log.event("adb", level="error", args=list(arguments),
                      ms=int((time.monotonic() - started) * 1000), error=repr(error))
        raise
    run_log.adb_result(args=list(arguments), ms=int((time.monotonic() - started) * 1000),
                       bytes=len(output))
    return output


def phone_focus() -> Optional[str]:
    windows = adb("shell", "dumpsys", "window", "displays").decode("utf-8", "replace")
    lines = windows.split("\n")
    start = next((index for index, line in enumerate(lines) if "Display: mDisplayId=0 " in line), None)
    if start is None:
        display = lines
    else:
        display = []
        for line in lines[start + 1:]:
            if "Display: mDisplayId=" in line:
                break
            display.append(line)
    focus = next((line for line in display if "mCurrentFocus=Window{" in line), None)
    if focus is None:
        return None
    last = focus.split(" ")[-1]
    return last.split("/")[0].strip("}")


def check_foreground() -> None:
    package = phone_focus()
    for _ in range(5):
        if package == APP_PACKAGE:
            break
        pause(0.4)
        package = phone_focus()
    if package != APP_PACKAGE:
        raise ForegroundError(package)


def capture() -> Screen:
    check_foreground()
    png = adb("exec-out", "screencap", "-p")
    try:
        return Screen(png)
    except (RunError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        decision = diagnose_with_countdown("OCR 无法读取当前页面", png)
        if decision is None or decision.action != "wait":
            raise UnresolvedScreen("OCR 无法读取当前页面") from error
        time.sleep(human_profile().poll_delay())
        check_foreground()
        return Screen(adb("exec-out", "screencap", "-p"))


def input_cmd(*arguments: str, system_dialog: bool = False) -> None:
    if arguments[0] == "keyevent":
        time.sleep(human_profile().pre_tap(quick=True))
    if not system_dialog:
        check_foreground()
    adb("shell", "input", *arguments)


@lru_cache(maxsize=1)
def human_profile() -> HumanProfile:
    return HumanProfile.new(app="douyin")


def tap_bounds(bounds: Tuple[float, float, float, float], on: Screen,
               text: str, *, quick: bool = False, system_dialog: bool = False) -> None:
    profile = human_profile()
    try:
        x, y = profile.tap_point(bounds, on.width, on.height)
    except ValueError as error:
        raise RunError(str(error)) from error
    input_cmd("tap", str(x), str(y), system_dialog=system_dialog)
    wait = profile.post_tap(quick=quick)
    run_log_event_tap(text, x, y, wait=wait)
    time.sleep(wait)


def tap(target: ScreenText, on: Screen, *, quick: bool = False,
        deadline: float | None = None,
        locate: Callable[[Screen], Optional[ScreenText]] | None = None,
        system_dialog: bool = False) -> Screen:
    from droidfleet import safety
    from droidfleet.rules import load_rules

    reason = safety.check(target.text, load_rules("douyin"))
    if reason is not None:
        from droidfleet import log as run_log

        run_log.event("safety.block", level="warn", text=target.text, reason=reason)
        raise RunError(f"安全规则拦截：{reason}")

    def identify(screen: Screen) -> Optional[Tuple[ScreenText, bool]]:
        if (screen.width, screen.height) != (on.width, on.height):
            return None
        if locate is None:
            matches = [line for line in screen.lines if line.text == target.text
                       and abs(line.x - target.x) <= max(0.015, target.bounds[2] / 2)
                       and abs(line.y - target.y) <= max(0.015, target.bounds[3] / 2)]
        else:
            candidate = locate(screen)
            matches = [candidate] if candidate is not None and candidate.text == target.text else []
        if len(matches) == 1:
            return (matches[0], screen.popup_suspected())
        return None

    def stable(first: Tuple[ScreenText, bool], second: Tuple[ScreenText, bool]) -> bool:
        a, popup_a = first
        b, popup_b = second
        return popup_a == popup_b and human_profile().same_bounds(
            a.pixel_bounds(on.width, on.height), b.pixel_bounds(on.width, on.height),
        )

    time.sleep(human_profile().pre_tap(quick=quick))
    read = _raw_screen if system_dialog else capture
    fresh = confirm_stable(human_profile(), on, read, identify, deadline=deadline, matches=stable)
    if fresh is None or fresh.popup_suspected() != on.popup_suspected():
        raise unknown(on, f"确认「{target.text}」的位置")
    verified = identify(fresh)
    if verified is None:
        raise unknown(fresh, f"重新定位「{target.text}」")
    target = verified[0]
    on = fresh
    tap_bounds(target.pixel_bounds(on.width, on.height), on, target.text,
               quick=quick, system_dialog=system_dialog)
    return on


def tap_text(line: "ScreenText", on: "Screen") -> None:
    """直接点 OCR 到的这一行，不做两次定位比对。

    领奖按钮（领取 / 立即领取）常因为弹窗、列表刷新导致两次 OCR 位置抖动，
    严格比对会误判成「未识别到预期界面」。
    """
    from droidfleet import safety
    from droidfleet.rules import load_rules

    reason = safety.check(line.text, load_rules("douyin"))
    if reason is not None:
        raise RunError(f"安全规则拦截：{reason}")
    tap_bounds(line.pixel_bounds(on.width, on.height), on, line.text)


def tap_flexible(screen: Screen, keyword: str) -> bool:
    """按关键词找一行直接点，不要求两次 OCR 文案完全一致（弹窗文案经常抖）。"""
    from droidfleet import safety
    from droidfleet.rules import load_rules

    line = next((item for item in screen.lines if keyword in item.text), None)
    if line is None:
        return False
    reason = safety.check(line.text, load_rules("douyin"))
    if reason is not None:
        raise RunError(f"安全规则拦截：{reason}")
    tap_bounds(line.pixel_bounds(screen.width, screen.height), screen, line.text)
    return True


def tap_close_icon(point: Tuple[float, float], screen: Screen,
                   ignoring_behind: Optional[str] = None, *, diagnosed: bool = False) -> None:
    # 重新确认 × 图标，只在检测到的图标中心附近的像素范围内取点。
    patch = control_patch(point, screen)
    original = screen.gray(patch)
    time.sleep(human_profile().pre_tap(quick=True))
    time.sleep(human_profile().stable_delay())
    fresh = capture()
    if (original is None or (fresh.width, fresh.height) != (screen.width, screen.height)
            or fresh.gray(patch) != original):
        raise unknown(fresh, "确认关闭图标的位置", diagnosed=diagnosed)
    verified = close_icon(point, fresh, ignoring_behind)
    if verified is None:
        raise unknown(fresh, "确认关闭图标的位置", diagnosed=diagnosed)
    radius = patch[2] * fresh.width * 2 / 48
    x, y = verified[0] * fresh.width, verified[1] * fresh.height
    tap_bounds((x - radius, y - radius, x + radius, y + radius), fresh, "×", quick=True)


def swipe(screen: Screen, *, down: bool, area: Tuple[float, float, float, float],
          distance: Tuple[float, float] | None = None) -> None:
    profile = human_profile()
    gesture = profile.swipe(screen.width, screen.height, down=down, area=area,
                            distance=distance)
    input_cmd("swipe", *(str(value) for value in gesture))
    time.sleep(profile.swipe_pause())


def pause(seconds: float) -> None:
    progress.wait(progress.APP_DOUYIN, task_log.label, "等待页面响应", human_profile().delay(seconds))


def pause_after_task(stage: str) -> None:
    """任务完成后先随机停留几秒，再按返回/退出（模拟注意力分散）。"""
    from droidfleet import log as run_log

    seconds = human_profile().after_task_delay()
    if seconds <= 0:
        return
    task_log.write(f"{stage}已完成，停留 {seconds:.1f} 秒后再退出")
    run_log.event("human.after_task", stage=stage, seconds=round(seconds, 2))
    progress.wait(progress.APP_DOUYIN, task_log.label, f"{stage}完成后停留", seconds)


def exit_after_task(screen: Screen, stage: str) -> None:
    """离开当前页：先点页面上的返回/退出控件，找不到再让本地 AI 指认，最后按返回键。"""
    from droidfleet import exits

    line = exits.find_exit(screen.lines)
    if line is not None:
        tap(line, screen)
        task_log.write(f"已点击「{line.text}」离开{stage}")
        return
    rules = human_profile().rules
    target = None
    if exits.ai_enabled(rules):
        target = exits.locate_with_ai("douyin", stage, screen.png, screen.lines, allow_blind=False)
    if target is not None and target.kind == "line":
        tap(target.line, screen)
        task_log.write(f"本地 AI 指认「{target.label}」，已完成点击")
        return
    task_log.write(f"没找到退出控件，按返回键离开{stage}", warning=True)
    input_cmd("keyevent", "KEYCODE_BACK")


def enter_task(page: Screen, kind: TaskKind, until: Callable[[Screen], bool], *,
               deadline: float | None = None) -> Screen:
    """可逆的任务导航。余额或任务卡变化时交给调用方继续识别。"""
    from droidfleet import log as run_log

    target = page.button_for(kind)
    if target is None:
        raise unknown(page, f"定位{kind.label}入口")
    if deadline is not None and time.monotonic() >= deadline:
        return page
    start_coins = page.balance()
    progress = page.video_progress() if kind == TaskKind.video else page.shopping_progress()

    def classify(screen: Screen) -> Observation:
        if until(screen):
            return "success"
        if not screen.task_page():
            return "unknown"
        fresh = screen.button_for(kind)
        current_progress = screen.video_progress() if kind == TaskKind.video else screen.shopping_progress()
        if (fresh is not None and fresh.text == target.text and not screen.reward_rejected()
                and (screen.width, screen.height) == (page.width, page.height)
                and human_profile().same_bounds(
                    fresh.pixel_bounds(screen.width, screen.height),
                    target.pixel_bounds(page.width, page.height),
                )
                and screen.balance() == start_coins and current_progress == progress):
            return "unchanged"
        return "unknown"

    def locate(screen: Screen) -> Optional[ScreenText]:
        return screen.button_for(kind) if classify(screen) == "unchanged" else None

    try:
        tap(target, page, quick=True, deadline=deadline, locate=locate)
    except ScreenRecognitionError:
        # 领完奖后任务卡还在刷新，两次定位比对偶尔对不上；按新截图直接点一次
        fresh = capture()
        again = fresh.button_for(kind)
        if again is None:
            raise
        tap_text(again, fresh)
    current, outcome = observe_tap(human_profile(), capture, classify, deadline=deadline)
    if outcome == "unchanged" and (deadline is None or time.monotonic() < deadline):
        fresh = current.button_for(kind)
        if fresh is not None:
            run_log.event("tap.retry", text=fresh.text, reason="task_page_unchanged")
            tap(fresh, current, quick=True, deadline=deadline, locate=locate)
            current, outcome = observe_tap(human_profile(), capture, classify, deadline=deadline)
    run_log.event("tap.result", text=target.text, outcome=outcome)
    return current


# ---- Ollama -------------------------------------------------------------


def decode_ollama_decision(data: bytes) -> dict:
    return vars(decode_recovery(data))


def request_ollama_decision(settings: Tuple[str, str], image: bytes, prompt: str) -> dict:
    name, _ = settings

    def attempt(token_limit: int) -> dict:
        timeout = 90
        with progress.countdown(progress.APP_DOUYIN, task_log.label, "等待本地 AI 自检结果", timeout):
            return decode_ollama_decision(image_chat(settings, image, prompt, RECOVERY_SCHEMA,
                                                    token_limit=token_limit, timeout=timeout))

    try:
        return attempt(512)
    except OllamaFailure as error:
        if not error.retryable:
            raise
        task_log.write(f"Ollama（{name}）请求失败：{error.reason}，2 秒后重试一次", warning=True)
        pause(2)
        return attempt(1024)


def check_ollama() -> None:
    settings = model_settings()
    if settings is None:
        raise RunError("Ollama 已停用，DROIDFLEET_MODEL 为空")
    name, endpoint = settings
    print(f"检查 {name} · POST {endpoint}")
    sample = (
        "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAeklEQVR4nO3PUQkAIBTAwFfDVkY0qiH8"
        "OITBAtxm7fN1wwUNaEEDWtCAFjSgBQ1oQQNa0IAWNKAFDWhBA1rQgBY0oAUNaEEDWtCAFjSgBQ1oQQNa0IAW"
        "NKAFDWhBA1rQgBY0oAUNaEEDWtCAFjSgBQ1oQQNa0IAWPHYBKq+Q8Rg2IZEAAAAASUVORK5CYII="
    )
    import base64

    try:
        image = base64.b64decode(sample)
    except ValueError as error:
        raise RunError("无法读取 Ollama 自检图片") from error
    decision = request_ollama_decision(
        settings,
        image,
        "这是一张纯色测试图，没有弹窗。返回 action 为 manual，label 为空，x 和 y 为 0，\n"
        "reason 用一句简短中文描述图片。只输出 JSON。\n",
    )
    if decision["action"] != "manual":
        raise RunError("Ollama 返回了有效 JSON，但纯色测试图的判定异常")
    print("Ollama 自检通过 · HTTP 200 · 图片推理和操作 JSON 解析成功")


# ---- 弹窗处理 -----------------------------------------------------------


def control_patch(point: Tuple[float, float], on: Screen) -> Tuple[float, float, float, float]:
    width = 0.09
    height = width * on.width / on.height
    return (point[0] - width / 2, point[1] - height / 2, width, height)


def close_icon(
    near: Tuple[float, float],
    on: Screen,
    ignoring_behind: Optional[str] = None,
) -> Optional[Tuple[float, float]]:
    patch = control_patch(near, on)
    size = 48
    pixels = on.gray(patch, size)
    if pixels is None:
        return None

    def value(x: int, y: int) -> int:
        return pixels[y * size + x]

    best_contrast = 35
    center: Optional[Tuple[float, float]] = None
    for y in range(15, 33):
        for x in range(15, 33):
            for radius in (5, 7, 9):
                strokes = [value(x, y)]
                for offset in (radius, radius // 2):
                    strokes += [
                        value(x - offset, y - offset),
                        value(x + offset, y + offset),
                        value(x - offset, y + offset),
                        value(x + offset, y - offset),
                    ]
                gaps = [
                    value(x - radius, y),
                    value(x + radius, y),
                    value(x, y - radius),
                    value(x, y + radius),
                ]
                contrast = max(min(strokes) - max(gaps), min(gaps) - max(strokes))
                if contrast > best_contrast:
                    best_contrast = contrast
                    center = (
                        patch[0] + (x + 0.5) * patch[2] / size,
                        patch[1] + (y + 0.5) * patch[3] / size,
                    )
    if center is None:
        return None
    for line in on.lines:
        if line.text in ("×", "X", "x", "✕"):
            continue
        if ignoring_behind is not None and line.text.startswith(ignoring_behind):
            continue
        if _expanded_contains(line.bounds, center, 0.008, 0.004):
            return None
    return center


def dismissal_target(decision: RecoveryDecision, on: Screen) -> Optional[Tuple[float, float]]:
    if not (55 <= decision.x <= 945 and 140 <= decision.y <= 870):
        return None
    if risky_dismissal([line.text for line in on.lines]):
        return None
    point = (decision.x / 1000, decision.y / 1000)
    if decision.label == "×":
        return close_icon(point, on)
    if decision.label not in (*DISMISS_LABELS, "关闭弹窗", "知道了"):
        return None
    labels = [
        line for line in on.lines
        if line.text == decision.label and abs(line.x - point[0]) < 0.055 and abs(line.y - point[1]) < 0.035
    ]
    if len(labels) != 1:
        return None
    return (labels[0].x, labels[0].y)


def diagnose_with_countdown(stage: str, png: bytes) -> RecoveryDecision | None:
    timeout = 60
    with progress.countdown(progress.APP_DOUYIN, task_log.label, f"{stage} · 等待本地 AI", timeout):
        return diagnose_screen(progress.APP_DOUYIN, stage, png, timeout=timeout)


def unknown(screen: Screen, stage: str, *, diagnosed: bool = False) -> ScreenRecognitionError:
    if not diagnosed:
        decision = diagnose_with_countdown(stage, screen.png)
        if decision is not None:
            task_log.write(f"本地 AI：{decision.reason}", warning=True)
    path = Path(tempfile.gettempdir()) / f"droidfleet-{uuid.uuid4()}.png"
    try:
        path.write_bytes(screen.png)
    except OSError:
        pass
    else:
        task_log.write(f"待检查的截图：{path}", warning=True)
    return UnresolvedScreen(stage)


def recover_page(screen: Screen, stage: str, *,
                 read: Callable[[], Screen] | None = None,
                 validate: Callable[[Screen], None] | None = None) -> Screen | None:
    def locate(decision: RecoveryDecision, page: Screen) -> tuple[float, float, str] | None:
        if validate is not None:
            validate(page)
        if ((page.width, page.height) != (screen.width, screen.height)
                or not page.popup_suspected()):
            return None
        point = dismissal_target(decision, page)
        if point is None:
            return None
        patch = control_patch(point, page)
        initial_pixels = screen.gray(patch)
        if initial_pixels is None or page.gray(patch) != initial_pixels:
            return None
        return (*point, decision.label)

    def dismiss(target: tuple[float, float, str], page: Screen) -> None:
        x, y, label = target
        if label == "×":
            tap_close_icon((x, y), page, diagnosed=True)
        else:
            if validate is not None:
                validate(page)
            line = next(item for item in page.lines if item.text == label and (item.x, item.y) == (x, y))
            from droidfleet import safety

            safety.guard(line.text)
            tap_bounds(line.pixel_bounds(page.width, page.height), page, line.text, quick=True)

    timeout = 60
    with progress.countdown(progress.APP_DOUYIN, task_log.label, f"{stage} · 等待本地 AI 恢复", timeout):
        return recover_screen(
            progress.APP_DOUYIN, stage, screen, screenshot=lambda page: page.png, read=read or capture,
            locate=locate, dismiss=dismiss, wait_seconds=human_profile().poll_delay(), timeout=timeout,
        )


def wait_for(
    stage: str,
    seconds: float,
    until: Callable[[Screen], bool],
    known_popup: Optional[Callable[[Screen], bool]] = None,
    validate: Optional[Callable[[Screen], None]] = None,
) -> Screen:
    known = known_popup or (lambda screen: False)
    check = validate or (lambda screen: None)
    deadline = time.monotonic() + seconds

    def read() -> Screen:
        return read_after_interruption(human_profile(), capture, (ForegroundError, CommandFailure),
                                       deadline=deadline)

    with progress.countdown(progress.APP_DOUYIN, task_log.label, stage, seconds):
        screen = read()
        recoveries = 0
        while True:
            check(screen)
            if known(screen) and until(screen):
                return screen
            if not screen.popup_suspected():
                if until(screen):
                    return screen
                if time.monotonic() < deadline:
                    time.sleep(min(human_profile().poll_delay(), max(0.0, deadline - time.monotonic())))
                    screen = read()
                    continue
            if recoveries >= 2:
                raise unknown(screen, stage, diagnosed=True)
            recoveries += 1
            fresh = recover_page(screen, stage, read=read, validate=check)
            if fresh is None:
                raise unknown(screen, stage, diagnosed=True)
            screen = fresh
            deadline = max(deadline, time.monotonic() + 5)


def wait_for_daily(
    stage: str,
    seconds: float,
    until: Callable[[Screen], bool],
    known_popup: Optional[Callable[[Screen], bool]] = None,
) -> Screen:
    def validate(screen: Screen) -> None:
        reason = screen.daily_blocked_reason()
        if reason is not None:
            raise DailyTaskSkipped(reason)

    return wait_for(stage, seconds, until, known_popup=known_popup, validate=validate)


def return_from_finished_ad(initial: Screen) -> Screen:
    def settled(screen: Screen) -> bool:
        return screen.task_page() and screen.balance() is not None

    screen = initial
    pause_after_task("广告任务")
    if screen.ad_exit_button() is None:
        exit_after_task(screen, "广告退出")
        pause(0.7)
        screen = wait_for(
            "退出已播完的广告",
            10,
            known_popup=lambda item: item.ad_exit_button() is not None,
            until=lambda item: settled(item) or item.ad_exit_button() is not None,
        )
    exit_button = screen.ad_exit_button()
    if exit_button is not None:
        time.sleep(human_profile().ad_close_delay())
        screen = capture()
        if settled(screen):
            return screen
        exit_button = screen.ad_exit_button()
        if exit_button is None:
            raise unknown(screen, "重新定位广告退出按钮")
        tap(exit_button, screen)
        task_log.write("已点击「坚持退出」，返回任务页核对奖励")
        pause(0.7)
        return wait_for("核对广告奖励", 10, until=settled)
    return screen


def close_daily_panel(
    screen: Screen,
    near: Tuple[float, float],
    stage: str,
    ignoring_behind: Optional[str] = None,
) -> None:
    close = close_icon(near, screen, ignoring_behind)
    if close is None:
        raise unknown(screen, stage)
    tap_close_icon(close, screen, ignoring_behind)
    pause(0.7)


def finish_ad() -> Screen:
    def exit_offer(screen: Screen) -> bool:
        return screen.ad_exit_button() is not None

    screen = wait_for(
        "进入广告",
        18,
        known_popup=exit_offer,
        until=lambda item: item.ad_badge() is not None or (item.task_page() and item.reward_rejected()),
    )
    timeout = 100
    deadline = time.monotonic() + timeout
    with progress.ad_countdown(progress.APP_DOUYIN, task_log.label, timeout) as observe_time:
        seconds = screen.ad_seconds()
        if seconds is not None:
            observe_time(seconds)
        task_log.observe(screen, announce=seconds is None)
        while not screen.ad_finished() and not screen.task_page() and time.monotonic() < deadline:
            pause(2)
            screen = wait_for(
                "读取广告倒计时",
                6,
                known_popup=exit_offer,
                until=lambda item: item.ad_badge() is not None or item.task_page(),
            )
            seconds = screen.ad_seconds()
            if seconds is not None:
                observe_time(seconds)
            task_log.observe(screen, announce=seconds is None)
    if screen.task_page():
        return screen
    if not screen.ad_finished():
        raise unknown(screen, "广告倒计时超时")
    time.sleep(human_profile().ad_tail())
    screen = capture()
    if screen.task_page():
        return screen
    if not screen.ad_finished():
        raise unknown(screen, "重新确认广告已播完")
    return return_from_finished_ad(screen)


# ---- 逛街 ---------------------------------------------------------------


def open_shopping(initial: Screen) -> Tuple[Screen, float, Optional["ShoppingScroller"]]:
    page = initial
    entry = page.button_for(TaskKind.shop)
    if entry is None:
        raise unknown(page, "定位「去逛街」")
    previous_side: bool | None = None
    for _ in range(2):
        if 0.20 <= entry.y <= 0.70:
            break
        down = entry.y > 0.70
        purpose = "correction" if previous_side is not None and down != previous_side else "near"
        swipe(page, down=down, area=(0.42, 0.38, 0.52, 0.79),
              distance=human_profile().list_distance(purpose))
        previous_side = down
        page = capture()
        entry = page.button_for(TaskKind.shop)
        if entry is None:
            raise unknown(page, "滚动后重新定位「去逛街」")
    if not (0.20 <= entry.y <= 0.70):
        raise unknown(page, "将「去逛街」移到列表中间")

    def landed(screen: Screen) -> bool:
        return screen.shopping_ended() or (
            not screen.popup_suspected()
            and (screen.shopping_page() or screen.read_shopping_badge() is not None)
        )

    entered_at = time.monotonic()
    page = enter_task(page, TaskKind.shop, landed)
    if page.popup_suspected() and not landed(page):
        page = wait_for("处理逛街入口弹窗", 4, until=landed,
                        known_popup=lambda item: item.shopping_done_panel())
    if not landed(page):
        raise unknown(page, "进入逛街页")
    scroller = None if page.shopping_ended() else ShoppingScroller(page)
    return (page, entered_at, scroller)


class ShoppingScroller:
    """把手势留在手机上执行，无线截图变慢时也不会拉大操作空档。"""

    def __init__(self, screen: Screen) -> None:
        self.stop_file = f"/data/local/tmp/droidfleet-shop-{uuid.uuid4()}.stop"
        self.stopped = False
        script = self._script(screen)
        try:
            self.process = subprocess.Popen(
                local_argv("/usr/bin/env", ["adb", "shell", "timeout", "180", "sh"]),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
            )
        except OSError as error:
            raise RunError(f"无法启动手机端滚动：{error}") from error
        try:
            assert self.process.stdin is not None
            self.process.stdin.write(script.encode("utf-8"))
            self.process.stdin.close()
        except (BrokenPipeError, OSError) as error:
            try:
                adb("shell", "touch", self.stop_file)
            except Exception:  # noqa: BLE001 - 清理失败不影响原始错误
                pass
            self.process.terminate()
            self.process.wait()
            raise RunError(f"无法向手机端滚动脚本写入指令：{error}") from error

    @property
    def is_running(self) -> bool:
        return self.process.poll() is None

    def _script(self, screen: Screen) -> str:
        script = (
            f"stop='{self.stop_file}'\n"
            "deadline=$(( $(date +%s) + 180 ))\n"
            "count=0\n"
            "maxgap=0\n"
            "last=$(date +%s%3N)\n"
            "trap 'rm -f \"$stop\"; echo SHOP_RESULT \"$count\" \"$maxgap\"' EXIT\n"
            "step() {\n"
            "  [ ! -e \"$stop\" ] && [ \"$(date +%s)\" -lt \"$deadline\" ] || exit 0\n"
            "  dumpsys window displays | grep 'mCurrentFocus=Window{' | grep -Fq '" + APP_PACKAGE + "/' || exit 0\n"
            "  now=$(date +%s%3N)\n"
            "  gap=$((now - last))\n"
            "  [ \"$gap\" -le \"$maxgap\" ] || maxgap=$gap\n"
            "  [ \"$gap\" -lt 4500 ] || exit 1\n"
            "  input swipe \"$1\" \"$2\" \"$3\" \"$4\" \"$5\" || exit 1\n"
            "  last=$(date +%s%3N)\n"
            "  count=$((count + 1))\n"
            "  sleep \"$6\"\n"
            "}\n"
            "\n"
        )
        profile = human_profile()
        toward_bottom = True
        remaining = profile.rng.randint(2, 4)
        for _ in range(110):
            gesture = profile.swipe(screen.width, screen.height, down=toward_bottom,
                                    area=(0.41, 0.25, 0.54, 0.72), distance=(0.19, 0.37))
            delay = profile.delay(1.4)
            script += "step " + " ".join(map(str, gesture)) + f" {delay:.2f}\n"
            remaining -= 1
            if remaining == 0:
                toward_bottom = not toward_bottom
                remaining = profile.rng.randint(2, 4)
        return script

    def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        if self.is_running:
            try:
                adb("shell", "touch", self.stop_file)
            except Exception:  # noqa: BLE001 - 停止文件写入失败不阻断收尾
                pass
        timeout = 45
        try:
            with progress.countdown(progress.APP_DOUYIN, task_log.label, "等待手机端滚动停止", timeout):
                self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            task_log.write("停止滚动等待超时，结束本机 ADB 进程", warning=True)
            self.process.kill()
            self.process.wait(timeout=5)
        summary = ""
        if self.process.stdout is not None:
            summary = self.process.stdout.read().decode("utf-8", "replace")
        lines = [line for line in summary.splitlines() if line]
        fields = [field for field in (lines[-1].split(" ") if lines else []) if field]
        if len(fields) == 3 and fields[0] == "SHOP_RESULT":
            try:
                count = int(fields[1])
                gap = float(fields[2])
            except ValueError:
                pass
            else:
                task_log.write(f"本轮滑动 {count:d} 次，手机端最长操作空档约 {gap / 1000:.2f} 秒")
        if self.process.returncode != 0:
            task_log.write(f"手机端滚动已停止（状态 {self.process.returncode}）", warning=True)


def finish_shopping(initial: Screen) -> Screen:
    page, entered_at, scroller = open_shopping(initial)
    screen = page
    try:
        deadline = entered_at + 180
        missed_timer = False
        zero_since: Optional[float] = None
        while (not screen.shopping_task_panel() and not screen.network_error()
               and not screen.reward_rejected() and not screen.task_page()):
            now = time.monotonic()
            remaining = screen.shop_seconds()
            if remaining is None:
                remaining = screen.read_shopping_badge()
            if remaining == 0 and zero_since is None:
                zero_since = now
                task_log.set_progress("已归零，等待活动结算")
            if zero_since is not None and now - zero_since >= 12:
                break
            confirmed = screen.shopping_header() or remaining is not None
            if not (not screen.popup_suspected()
                    and (confirmed or (screen.shopping_products() and (not missed_timer or zero_since is not None)))):
                raise unknown(screen, "确认逛街商品页")
            missed_timer = not confirmed
            if zero_since is None and (scroller is None or not scroller.is_running):
                raise unknown(screen, "手机端滚动提前停止")
            if not now < deadline:
                raise unknown(screen, "逛街倒计时超时")
            if remaining is not None and zero_since is None:
                task_log.set_progress(f"剩余 {remaining} 秒")
            pause(0.25)
            screen = capture()
        if scroller is not None:
            scroller.stop()
        if (zero_since is not None and not screen.shopping_task_panel() and not screen.network_error()
                and not screen.reward_rejected() and not screen.task_page()
                and not screen.popup_suspected()):
            badge = screen.shopping_badge_text()
            if badge is not None and badge.text == "0秒":
                tap(badge, screen, locate=lambda item: item.shopping_badge_text())
                task_log.write("已打开归零的活动计时牌，核对任务面板")
                pause(0.6)
                screen = capture()
        task_log.observe(screen)
        if screen.network_error():
            task_log.write("商品页提示网络错误，返回任务页核对奖励", warning=True)
        elif screen.shopping_done_panel():
            task_log.write("活动面板显示「已完成」，返回任务页核对奖励")
        elif zero_since is not None:
            task_log.write("活动计时已归零，返回任务页核对奖励")
        if screen.task_page():
            return reach_task_page()
        if screen.shopping_done_panel():
            pause_after_task("逛街任务")
            return reach_task_page()
        pause_after_task("逛街任务")
        exit_after_task(screen, "逛街退出")
        return reach_task_page()
    finally:
        if scroller is not None:
            scroller.stop()


# ---- 回到任务页 ---------------------------------------------------------


def tap_earning_tab(screen: Screen) -> bool:
    x, y = (0.38, 0.62), (0.86, 0.95)

    def locate(page: Screen) -> Optional[ScreenText]:
        return page.daily_text(r"^赚钱$", x=x, y=y)

    target = locate(screen)
    if target is not None:
        tap(target, screen, locate=locate)
        return True
    return tap_named_control(screen, "赚钱", x=x, y=y, label="底部赚钱")


def reach_task_page(
    ignore_rest_reminder: bool = False,
    keep_sign_in_panel: bool = False,
    daily_mode: bool = False,
) -> Screen:
    relaunched = False
    latest: Optional[Screen] = None
    attempted_recovery = False
    backs = 0
    for _ in range(8):
        try:
            screen = capture()
        except ForegroundError as error:
            if relaunched:
                raise
            task_log.write("抖音极速版失去前台焦点，正在切回应用", warning=True)
            resolved = adb(
                "shell", "cmd", "package", "resolve-activity", "--brief",
                "-a", "android.intent.action.MAIN",
                "-c", "android.intent.category.LAUNCHER", APP_PACKAGE,
            ).decode("utf-8", "replace")
            launcher_lines = [line for line in resolved.splitlines() if line]
            launcher = launcher_lines[-1] if launcher_lines else None
            if launcher is None or not launcher.startswith(APP_PACKAGE + "/"):
                raise RunError("找不到抖音极速版的启动入口")
            adb(
                "shell", "am", "start", "-W",
                "-a", "android.intent.action.MAIN",
                "-c", "android.intent.category.LAUNCHER", "-n", launcher,
            )
            relaunched = True
            pause(1)
            continue
        if ignore_rest_reminder and screen.rest_reminder():
            screen = resume_video_after_reminder(screen)
        latest = screen
        if screen.ad_finished():
            return return_from_finished_ad(screen)
        if screen.target_video_offer() is not None:
            raise RunError("指定视频页有「继续领金币」提示，请先处理该提示")
        if daily_mode and screen.daily_blocked_reason() is not None:
            input_cmd("keyevent", "KEYCODE_BACK")
            pause(1)
            continue
        if keep_sign_in_panel and screen.daily_sign_in_panel():
            return screen
        offer = screen.daily_wallet_offer()
        if offer is not None:
            close_daily_panel(screen, offer, "关闭钱包推广弹窗")
            continue
        if screen.daily_text(r"已成功添加", y=(0.45, 0.95)) is not None:
            close = screen.daily_text(r"^[×xX]$", x=(0.75, 1.0), y=(0.40, 0.75))
            if close is not None:
                tap(close, screen)
            else:
                input_cmd("keyevent", "KEYCODE_BACK")
            pause(1.0)
            continue
        if any("看广告再赚" in line.text or "获得开宝箱奖励" in line.text
               for line in screen.lines):
            close = screen.daily_text(r"^[×xX]$", x=(0.68, 0.95), y=(0.20, 0.42))
            if close is not None:
                tap(close, screen)
            else:
                input_cmd("keyevent", "KEYCODE_BACK")
            pause(1.0)
            continue
        keep = screen.daily_text(r"^坚持退出$", x=(0.1, 0.9), y=(0.30, 0.92))
        if keep is not None:
            tap(keep, screen)
            pause(1.0)
            continue
        if screen.ad_countdown():
            input_cmd("keyevent", "KEYCODE_BACK")
            pause(1.2)
            continue
        if screen.daily_sign_in_panel() and not keep_sign_in_panel:
            claim = screen.daily_text(r"^签到领", x=(0.18, 0.82), y=(0.47, 0.73))
            if claim is not None:
                # 顺手把连续签到的金币领掉，弹窗一般会自己关掉
                tap(claim, screen)
                pause(1.5)
                try:
                    after = capture()
                except Exception:  # noqa: BLE001
                    after = None
                if after is not None and after.daily_sign_in_panel():
                    close = after.daily_text(r"^[×xX]$", x=(0.58, 0.99), y=(0.17, 0.44))
                    if close is not None:
                        tap(close, after)
                    else:
                        close_daily_panel(after, (0.801, 0.304), "关闭连续签到弹窗")
                continue
            close = screen.daily_text(r"^[×xX]$", x=(0.58, 0.99), y=(0.17, 0.44))
            if close is not None:
                tap(close, screen)
            else:
                close_daily_panel(screen, (0.801, 0.304), "关闭连续签到弹窗")
            pause(0.8)
            continue
        if screen.daily_reservation_page():
            dismiss = screen.daily_text(r"^不用提醒我也能来$", x=(0.15, 0.85), y=(0.4, 0.9))
            if dismiss is not None:
                tap(dismiss, screen)
                pause(0.8)
            input_cmd("keyevent", "KEYCODE_BACK")
            pause(1.0)
            continue
        if screen.daily_hammer_reminder():
            dismiss = screen.daily_text("^我知道了$", x=(0.28, 0.72), y=(0.49, 0.70))
            if dismiss is not None:
                tap(dismiss, screen)
                pause(0.7)
            else:
                close_daily_panel(screen, (0.50, 0.68), "关闭锤子任务提醒", "累计已锤得")
            continue
        if screen.daily_reservation_gift():
            # 浮层的 × 在粉色按钮正下方。OCR 读不出 ×，但像素核验能定位，
            # 核验通过后点击；这一层只关闭浮层，预约任务另有入口。
            close = close_icon((0.502, 0.769), screen)
            if close is None:
                raise unknown(screen, "关闭预约礼包浮层")
            tap_close_icon(close, screen)
            task_log.write("关闭预约礼包浮层")
            pause(0.8)
            continue
        if screen.video_reward():
            # 上次运行留下的看视频奖励弹窗：先收下，别让任务页判定卡住
            accept = next(
                (line for line in screen.lines
                 if "收下" in line.text and 0.30 <= line.x <= 0.70 and 0.45 <= line.y <= 0.80),
                None,
            )
            if accept is not None:
                tap_text(accept, screen)
                pause(1.0)
                continue
            close = screen.daily_text(r"^[×xX✕]$", x=(0.68, 0.92), y=(0.20, 0.40))
            if close is not None:
                tap(close, screen)
                pause(1.0)
                continue
        if screen.task_page():
            if screen.popup_suspected():
                cleared = wait_for(
                    "处理任务页弹窗",
                    4,
                    known_popup=lambda item: keep_sign_in_panel and item.daily_sign_in_panel(),
                    validate=lambda item: _daily_validate(item, daily_mode),
                    until=lambda item: (
                        (keep_sign_in_panel and item.daily_sign_in_panel())
                        or (item.task_page() and (item.balance() is not None or item.return_to_top is not None))
                    ),
                )
                if keep_sign_in_panel and cleared.daily_sign_in_panel():
                    return cleared
                if cleared.return_to_top is None and cleared.balance() is not None:
                    return cleared
                continue
            elif screen.return_to_top is not None:
                # 点完会立刻滚回顶部，「返回顶部」本身消失，严格两次定位会误判失败
                tap_text(screen.return_to_top, screen)
                pause(1)
                continue
            elif screen.balance() is not None:
                return screen
            else:
                if any("马上登录" in line.text or "登录赚更多钱" in line.text
                       for line in screen.lines):
                    raise RunError("抖音极速版未登录，请先在手机上手动登录后再运行")
                return wait_for(
                    "读取任务页余额",
                    6,
                    until=lambda item: item.task_page() and item.balance() is not None,
                )
        if (not screen.popup_suspected() and screen.target_video_badge() is not None
                and screen.target_video_back() is not None):
            tap(screen.target_video_back(), screen)  # type: ignore[arg-type]
        elif not screen.popup_suspected() and screen.on_video_feed() and tap_earning_tab(screen):
            pause(1)
            continue
        elif (screen.ad_badge() is not None or screen.shopping_page() or screen.shopping_ended()
              or screen.target_video_badge() is not None):
            input_cmd("keyevent", "KEYCODE_BACK")
        else:
            if backs < 2:
                backs += 1
                input_cmd("keyevent", "KEYCODE_BACK")
                pause(1.0)
                continue
            if attempted_recovery:
                raise unknown(screen, "返回赚钱任务页", diagnosed=True)
            attempted_recovery = True
            recovered = recover_page(screen, "返回任务页时遇到未识别页面",
                                     validate=lambda item: _daily_validate(item, daily_mode))
            if recovered is None:
                raise unknown(screen, "返回赚钱任务页", diagnosed=True)
            if (recovered.task_page() and not recovered.popup_suspected()
                    and recovered.return_to_top is None and recovered.balance() is not None):
                return recovered
            continue
        pause(1)
    if latest is not None:
        raise unknown(latest, "返回赚钱任务页", diagnosed=attempted_recovery)
    raise RunError("无法返回赚钱任务页")


def _daily_validate(screen: Screen, daily_mode: bool) -> None:
    if not daily_mode:
        return
    reason = screen.daily_blocked_reason()
    if reason is not None:
        raise DailyTaskSkipped(reason)


def wait_for_video_feed(stage: str, seconds: float) -> Screen:
    return wait_for(
        stage,
        seconds,
        known_popup=lambda screen: screen.rest_reminder(),
        until=lambda screen: (screen.earning_tab() is not None or screen.on_video_feed()
                              or screen.rest_reminder()),
    )


def resume_video_after_reminder(reminder: Screen) -> Screen:
    buttons = [
        line for line in reminder.lines
        if line.text == "忽略提醒" and 0.60 <= line.y <= 0.94
    ]
    if not reminder.rest_reminder() or len(buttons) != 1:
        raise unknown(reminder, "定位「忽略提醒」")
    tap(buttons[0], reminder)
    pause(1.5)
    resumed = wait_for_video_feed("忽略提醒后返回视频", 6)
    if resumed.rest_reminder():
        raise unknown(resumed, "忽略提醒后弹窗仍在")
    task_log.write("已点击「忽略提醒」，继续看视频", warning=True)
    return resumed


# ---- 互动控件（uiautomator） ---------------------------------------------


@dataclass(frozen=True)
class AndroidControl:
    label: str
    text: str
    resource_id: str
    bounds: Tuple[float, float, float, float]
    clickable: bool
    tap_bounds: Optional[Tuple[float, float, float, float]]

    @property
    def mid_x(self) -> float:
        return self.bounds[0] + self.bounds[2] / 2

    @property
    def mid_y(self) -> float:
        return self.bounds[1] + self.bounds[3] / 2

    @property
    def max_x(self) -> float:
        return self.bounds[0] + self.bounds[2]

    @property
    def max_y(self) -> float:
        return self.bounds[1] + self.bounds[3]


def _bounds_of(element: ET.Element) -> Optional[Tuple[float, float, float, float]]:
    value = element.get("bounds")
    if value is None or re.fullmatch(r"\[[0-9]+,[0-9]+\]\[[0-9]+,[0-9]+\]", value) is None:
        return None
    parts = [int(part) for part in re.findall(r"[0-9]+", value)]
    if len(parts) != 4 or not (parts[0] < parts[2] and parts[1] < parts[3]):
        return None
    return (float(parts[0]), float(parts[1]), float(parts[2] - parts[0]), float(parts[3] - parts[1]))


class VideoControls:
    def __init__(self, xml: bytes, screen: Screen) -> None:
        self.width = float(screen.width)
        self.height = float(screen.height)
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as error:
            raise RunError(f"无法解析互动控件：{error}") from error
        parents = {child: parent for parent in root.iter() for child in parent}
        self.nodes: List[AndroidControl] = []
        for element in root.iter("node"):
            if element.get("package") != APP_PACKAGE:
                continue
            bounds = _bounds_of(element)
            if bounds is None:
                continue
            clickable = element.get("clickable") == "true"
            tap_bounds: Optional[Tuple[float, float, float, float]] = None
            if clickable:
                tap_bounds = bounds
            else:
                parent = parents.get(element)
                if parent is not None and parent.get("clickable") == "true":
                    parent_bounds = _bounds_of(parent)
                    if (parent_bounds is not None and _contains(parent_bounds, bounds)
                            and parent_bounds[2] < screen.width * 0.25
                            and parent_bounds[3] < screen.height * 0.08):
                        tap_bounds = parent_bounds
            self.nodes.append(
                AndroidControl(
                    label=element.get("content-desc") or "",
                    text=element.get("text") or "",
                    resource_id=element.get("resource-id") or "",
                    bounds=bounds,
                    clickable=clickable,
                    tap_bounds=tap_bounds,
                )
            )

    @property
    def comments_close(self) -> Optional[AndroidControl]:
        if not any(matches(node.text, "^[0-9.万千]+条评论$") for node in self.nodes):
            return None
        found = [
            node for node in self.nodes
            if node.label == "关闭" and node.resource_id.endswith("/back_btn") and node.clickable
            and node.mid_x > self.width * 0.80
            and node.mid_y > self.height * 0.15 and node.mid_y < self.height * 0.75
        ]
        return found[0] if len(found) == 1 else None

    @property
    def video_like(self) -> Optional[AndroidControl]:
        if self.comments_close is not None:
            return None
        found = [
            node for node in self.nodes
            if matches(node.label, "^未点赞[，,]喜欢.+[，,]按钮$") and node.clickable
            and node.mid_x > self.width * 0.75
            and self.height * 0.25 <= node.mid_y <= self.height * 0.85
        ]
        return found[0] if len(found) == 1 else None

    @property
    def comments_button(self) -> Optional[AndroidControl]:
        if self.comments_close is not None:
            return None
        found = [
            node for node in self.nodes
            if matches(node.label, "^评论[0-9.万千]*[，,]按钮$") and node.clickable
            and node.mid_x > self.width * 0.75
            and self.height * 0.25 <= node.mid_y <= self.height * 0.85
        ]
        return found[0] if len(found) == 1 else None

    @property
    def comment_likes(self) -> List[AndroidControl]:
        close = self.comments_close
        if close is None:
            return []
        return [
            node for node in self.nodes
            if matches(node.label, "^赞[0-9.万千]+[,，]未选中$") and node.tap_bounds is not None
            and node.mid_x > self.width * 0.70
            and node.mid_y > close.max_y
            and node.max_y < self.height * 0.86
        ]

    def is_selected(self, control: AndroidControl, pattern: str) -> bool:
        return any(
            node.resource_id == control.resource_id
            and abs(node.mid_y - control.mid_y) < self.height * 0.012
            and abs(node.max_x - control.max_x) < self.width * 0.04
            and matches(node.label, pattern)
            for node in self.nodes
        )


def read_video_controls(screen: Screen) -> VideoControls:
    check_foreground()
    path = f"/data/local/tmp/droidfleet-ui-{uuid.uuid4()}.xml"
    try:
        adb("shell", "uiautomator", "dump", path)
        xml = adb("exec-out", "cat", path)
    finally:
        try:
            adb("shell", "rm", "-f", path)
        except Exception:  # noqa: BLE001 - 清理失败不阻断
            pass
    return VideoControls(xml, screen)


def tap_control(control: AndroidControl, screen: Screen, *, label: str = "视频互动控件") -> None:
    def locate() -> Optional[AndroidControl]:
        controls = read_video_controls(screen)
        matches = [item for item in controls.nodes
                   if (item.label, item.text, item.resource_id) == (control.label, control.text, control.resource_id)
                   and human_profile().same_bounds(item.bounds, control.bounds)
                   and item.tap_bounds is not None]
        return matches[0] if len(matches) == 1 else None

    time.sleep(human_profile().pre_tap(quick=True))
    fresh = confirm_stable(human_profile(), control, locate,
                           lambda item: item.tap_bounds if item is not None else None,
                           matches=human_profile().same_bounds)
    bounds = fresh.tap_bounds if fresh is not None else None
    if (bounds is None or bounds[0] < 0 or bounds[1] < 0
            or bounds[0] + bounds[2] > screen.width or bounds[1] + bounds[3] > screen.height):
        raise RunError("互动控件坐标未通过核验")
    tap_bounds((bounds[0], bounds[1], bounds[0] + bounds[2], bounds[1] + bounds[3]),
               screen, label, quick=True)


def tap_named_control(screen: Screen, pattern: str, *, x: Tuple[float, float],
                      y: Tuple[float, float], label: str) -> bool:
    """OCR 漏读时，只使用指定区域内可点击的同名 Android 控件。"""
    try:
        controls = read_video_controls(screen)
    except (CommandFailure, RunError):
        return False
    candidates = {
        node.tap_bounds: node
        for node in controls.nodes
        if node.tap_bounds is not None
        and x[0] * screen.width <= node.mid_x <= x[1] * screen.width
        and y[0] * screen.height <= node.mid_y <= y[1] * screen.height
        and (re.fullmatch(pattern, node.text)
             or re.fullmatch(rf"(?:{pattern})(?:[，,]\s*(?:按钮|标签页))?", node.label))
    }
    if len(candidates) != 1:
        return False
    tap_control(next(iter(candidates.values())), screen, label=label)
    return True


class VideoInteractions:
    def __init__(self) -> None:
        self.video_like_attempts = 0
        self.comment_open_attempts = 0
        self.comment_like_attempts = 0

    def perform(self, screen: Screen) -> Screen:
        if screen.earning_tab() is None or screen.popup_suspected():
            return screen
        profile = human_profile()
        like_video = self.video_like_attempts < 1 and profile.should_like()
        comment_plan = profile.comment_plan() if self.comment_open_attempts < 2 else None
        open_comments = comment_plan is not None
        if not (like_video or open_comments):
            return screen

        def read_feed() -> Screen:
            current = wait_for_video_feed("互动后返回视频", 6)
            return resume_video_after_reminder(current) if current.rest_reminder() else current

        try:
            controls = read_video_controls(screen)
        except Exception as error:  # noqa: BLE001 - 与 Swift 一致，读不到控件就继续观看
            task_log.write(f"读取互动控件失败，本条继续观看：{error}", warning=True)
            return screen
        tapped_video_like = False
        if like_video:
            target = controls.video_like
            if target is not None:
                self.video_like_attempts += 1
                try:
                    tap_control(target, screen)
                    tapped_video_like = True
                    pause(0.5)
                    controls = read_video_controls(screen)
                    selected = controls.is_selected(target, "^已点赞[，,]")
                    task_log.write("已点赞当前视频" if selected else "视频点赞状态未确认", warning=not selected)
                except Exception as error:  # noqa: BLE001
                    task_log.write(f"视频点赞未确认：{error}", warning=True)
                    return read_feed()
        if comment_plan is None:
            return read_feed() if tapped_video_like else screen
        button = controls.comments_button
        if button is None:
            return read_feed() if tapped_video_like else screen
        self.comment_open_attempts += 1
        tap_control(button, screen)
        pause(0.8)
        try:
            controls = read_video_controls(screen)
            if controls.comments_close is None:
                raise RunError("评论区未确认打开")
            task_log.write("已打开评论区")
            progress.wait(progress.APP_DOUYIN, task_log.label, "停留评论区", comment_plan["dwell"])
            controls = read_video_controls(screen)
            likes = controls.comment_likes
            if self.comment_like_attempts < 1 and comment_plan["like_comment"] and likes:
                target = profile.rng.choice(likes)
                self.comment_like_attempts += 1
                tap_control(target, screen)
                pause(0.5)
                controls = read_video_controls(screen)
                selected = controls.is_selected(target, "^赞[0-9.万千]+[,，]已选中$")
                task_log.write("已点赞一条可见评论" if selected else "评论点赞状态未确认", warning=not selected)
            time.sleep(comment_plan["close_delay"])
            controls = read_video_controls(screen)
            if controls.comments_close is None:
                raise RunError("评论区关闭控件已变化")
            tap_control(controls.comments_close, screen)
        except Exception as error:  # noqa: BLE001
            task_log.write(f"评论区互动未完成：{error}，尝试收起评论区", warning=True)
            current = capture()
            if current.rest_reminder():
                return resume_video_after_reminder(current)
            if current.earning_tab() is not None and not current.popup_suspected():
                return current
            if read_video_controls(current).comments_close is not None:
                input_cmd("keyevent", "KEYCODE_BACK")
            else:
                recovered = recover_page(current, "评论区出现未识别界面")
                if recovered is None:
                    raise unknown(current, "恢复视频界面", diagnosed=True) from error
                if recovered.earning_tab() is not None and not recovered.popup_suspected():
                    return recovered
        resumed = read_feed()
        task_log.write("已收起评论区，继续观看视频")
        return resumed


def finish_video(initial: Screen, until_complete: bool) -> Tuple[Screen, bool]:
    page = initial
    deadline = float("inf") if until_complete else time.monotonic() + 240
    # 整段连续看视频的安全上限：180 分钟 / 3000 条，避免异常页面把任务挂死
    # （抖音的进度按观看时长计点，实测约 4 条视频才涨 1 点，289 点要一个多小时）
    run_deadline = time.monotonic() + 180 * 60 if until_complete else deadline
    max_total = 3000
    total_watched = 0
    tiers_claimed = 0
    stop_requested = False
    batches_without_progress = 0

    def read_feed(stage: str, seconds: float) -> Screen:
        screen = wait_for_video_feed(stage, seconds)
        if not screen.rest_reminder():
            return screen
        if not until_complete:
            raise RunError("已到达应用设置的休息时间")
        return resume_video_after_reminder(screen)

    def swipe_video(screen: Screen, forward: bool) -> None:
        swipe(screen, down=forward, area=(0.43, 0.28, 0.55, 0.76), distance=(0.32, 0.42))

    def feed_visible(screen: Screen) -> bool:
        return screen.rest_reminder() or (
            not screen.popup_suspected()
            and (screen.earning_tab() is not None or screen.on_video_feed()))

    def open_feed(attempts: int = 3) -> Optional[Screen]:
        """从任务页点「去看视频」进入推荐视频，失败就重试，不要一次误判就整轮退出。"""
        nonlocal page
        last_error: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                target_page = page if attempt == 0 else reach_task_page(ignore_rest_reminder=until_complete)
                page = target_page
                button = target_page.button_for(TaskKind.video)
                if button is None:
                    break
                if button.text != "去看视频":
                    # 已经可以领奖：交给外层循环处理，避免在这里误领或空等
                    return None
                entered = enter_task(target_page, TaskKind.video, feed_visible, deadline=deadline)
                if feed_visible(entered):
                    if entered.rest_reminder():
                        if not until_complete:
                            raise RunError("已到达应用设置的休息时间")
                        return resume_video_after_reminder(entered)
                    return entered
                return read_feed("进入推荐视频", 10)
            except (UnresolvedScreen, RunError, ForegroundError) as error:
                last_error = error
                task_log.write(f"进入视频页失败：{error}，再试一次", warning=True)
                pause(2)
                try:
                    page = reach_task_page(ignore_rest_reminder=until_complete)
                except Exception:  # noqa: BLE001
                    break
        if last_error is not None:
            task_log.write("多次尝试后仍没进入视频页", warning=True)
        return None

    def video_task_ready(screen: Screen) -> bool:
        button = screen.button_for(TaskKind.video)
        if button is None:
            return False
        return button.text != "去看视频" or screen.video_progress() is not None

    if not video_task_ready(page):
        page = wait_for("读取连续看视频任务卡", 8, until=video_task_ready)

    while True:
        target = page.button_for(TaskKind.video)
        if target is None:
            break
        task_log.observe(page, announce=True)
        if target.text != "去看视频":
            coins_before_claim = page.balance()
            completed = page.video_progress()
            completed_target = completed[1] if completed is not None else None
            tap_text(target, page)
            reward = wait_for(
                "领取看视频奖励",
                10,
                known_popup=lambda screen: screen.video_reward(),
                until=lambda screen: screen.video_reward()
                or (screen.task_page() and screen.reward_rejected()),
            )
            if reward.reward_rejected():
                return (reward, False)
            boosted = _boost_video_reward(reward)
            if boosted is not None:
                reward = boosted
            else:
                # 翻倍广告超时：别拿旧弹窗坐标去点，重新看一眼当前页面
                try:
                    reward = capture()
                except Exception:  # noqa: BLE001
                    pass
            if reward.video_reward():
                # OCR 常把「开心收下」和下面的「24点前可领取」粘成「4刖可领心收下」，
                # 这里只要求同一行里出现「收下」，位置仍限定在弹窗按钮区。
                accept = next(
                    (line for line in reward.lines
                     if "收下" in line.text and 0.30 <= line.x <= 0.70 and 0.45 <= line.y <= 0.80),
                    None,
                )
                if accept is None:
                    raise unknown(reward, "核对看视频奖励确认按钮")
                tap(accept, reward)
            tiers_claimed += 1
            page = reach_task_page(ignore_rest_reminder=until_complete)
            coins_after_claim = page.balance()
            if (coins_before_claim is not None and coins_after_claim is not None
                    and coins_after_claim > coins_before_claim):
                task_log.observe(page)
                task_log.credit(coins_after_claim - coins_before_claim)
            if not until_complete:
                return (page, True)
            if tiers_claimed >= 12:
                task_log.write(f"本次已连领 {tiers_claimed} 档奖励，先停在这里")
                return (page, True)
            # 领完一档奖励后，若任务卡又出现「去看视频」，说明下一档已解锁，继续看完
            if time.monotonic() >= run_deadline:
                task_log.write(f"已领 {tiers_claimed} 档奖励，已达本轮时长上限，先停在这里")
                return (page, True)
            next_page = None
            for attempt in range(6):
                try:
                    fresh = page if attempt == 0 else reach_task_page(ignore_rest_reminder=True)
                except Exception:  # noqa: BLE001
                    break
                button = fresh.button_for(TaskKind.video)
                progress_now = fresh.video_progress()
                if (button is not None and button.text == "去看视频"
                        and progress_now is not None):
                    next_page = fresh
                    break
                if attempt >= 2:
                    # 没找到任务卡时往上滚一点，避免领奖后列表停在别的位置
                    scroll_task_list(fresh, target_y=0.1)
                pause(0.8)
            if next_page is None:
                return (page, True)
            following = next_page.video_progress()
            task_log.write(f"第 {tiers_claimed} 档奖励已领，继续下一档 "
                           f"{following[0]}/{following[1]}（已看 {total_watched} 条）")
            if (completed_target is not None
                    and following[1] >= completed_target * 10
                    and following[0] <= following[1] * 0.05):
                # 例：328 档领完后卡片变成「今天 0/8994 解锁中」，
                # 这是后续天数/全天累计额度，不是今天还能刷的一档。
                # 注意正常档位也可能翻几倍（今天 108 → 388），阈值放到 10 倍。
                task_log.write(
                    f"已领完今天 {completed_target} 金币档，下一档 "
                    f"{following[0]}/{following[1]} 属于后续天数额度，今天不再空刷")
                return (next_page, True)
            page = next_page
            continue

        progress = page.video_progress()
        if not (time.monotonic() < deadline and progress is not None):
            break
        feed = open_feed()
        if feed is None:
            button_now = page.button_for(TaskKind.video)
            if button_now is not None and button_now.text != "去看视频":
                task_log.write("任务卡已可领奖，回任务页领取")
            else:
                task_log.write("没能进入连续看视频页，先停在这里", warning=True)
            break
        profile = human_profile()
        stall_after = 0
        if until_complete:
            # 例如页面显示 62/88 解锁中：这一轮就看完剩下的进度。
            # 注意进度按观看时长计点，实测约 3 条视频才涨 1 点，
            # 所以不能只刷「剩余点数」条，要按 4 倍留余量，刷够为止。
            remaining = max(0, progress[1] - progress[0])
            if remaining > 0:
                batch_size = min(max(remaining * 4, remaining + 12), 600)
                stall_after = max(30, remaining * 2)
                task_log.write(
                    f"当前进度 {progress[0]}/{progress[1]}，还差 {remaining} 点"
                    f"（约 {remaining * 3} 条视频），本轮一次看完")
            else:
                batch_size = min(progress[1], 300)
                task_log.write(f"当前进度 {progress[0]}/{progress[1]}，本轮继续刷到本档完成")
            revisit_after = (profile.rng.randrange(max(5, batch_size // 2), batch_size + 1)
                             if batch_size > 10 and profile.rng.random() < 0.5 else None)
        else:
            batch_size = 5
            revisit_after = None
        interactions = VideoInteractions()
        last_feed_progress = progress[0]

        def report_feed_progress(screen: Screen) -> None:
            nonlocal last_feed_progress
            count = screen.feed_progress()
            if count is None or not (count > last_feed_progress and count <= progress[1]):
                return
            last_feed_progress = count
            task_log.set_progress(f"{count}/{progress[1]}")

        def recover_feed() -> Optional[Screen]:
            """偶尔会掉回普通推荐页（没有赚钱浮层），回任务页重新进一次。"""
            task_log.write("连续看视频页丢失，回任务页重新进入", warning=True)
            return open_feed(attempts=2)

        report_feed_progress(feed)
        watched = 0
        for watched in range(1, batch_size + 1):
            if last_feed_progress >= progress[1] or time.monotonic() >= deadline:
                break
            if total_watched >= max_total or time.monotonic() >= run_deadline:
                task_log.write(f"本轮已看 {total_watched} 条，达到安全上限，先停在这里")
                stop_requested = True
                break
            if stall_after and watched >= stall_after and last_feed_progress <= progress[0]:
                task_log.write(f"已刷 {watched - 1} 条进度仍未增长，回任务页核对")
                break
            if until_complete and watched % 10 == 1:
                task_log.write(f"已刷 {watched - 1}/{batch_size} 条，继续看剩余 {batch_size - watched + 1} 条")
            try:
                swipe_video(feed, True)
                total_watched += 1
                # 每 3~5 秒滑动一次，保持计奖一直处于「赚钱中」
                dwell = profile.rng.uniform(3.0, 5.0)
                time.sleep(min(dwell, max(0, deadline - time.monotonic())))
                feed = read_feed("读取推荐视频", 6)
                report_feed_progress(feed)
                if until_complete and last_feed_progress < progress[1]:
                    feed = interactions.perform(feed)
                    report_feed_progress(feed)
                if not (watched == revisit_after and last_feed_progress < progress[1]):
                    continue
                task_log.write("下滑回看上一条视频")
                swipe_video(feed, False)
                pause(7)
                feed = read_feed("读取回看的视频", 6)
                report_feed_progress(feed)
                if last_feed_progress >= progress[1]:
                    break
                swipe_video(feed, True)
                pause(2)
                feed = read_feed("回看后继续刷视频", 6)
                report_feed_progress(feed)
            except (UnresolvedScreen, ForegroundError) as error:
                # 被别的应用抢了前台、或掉回普通推荐页：回任务页重新进入后接着刷
                task_log.write(f"{error}，尝试回到任务页继续", warning=True)
                recovered = recover_feed()
                if recovered is None:
                    break
                feed = recovered
                continue

        watched_done = watched
        reach_task_page(ignore_rest_reminder=until_complete)
        pause(2)

        def synced(screen: Screen) -> bool:
            if not video_task_ready(screen):
                return False
            button = screen.button_for(TaskKind.video)
            if button is None:
                return False
            if button.text != "去看视频":
                return True
            updated = screen.video_progress()
            if updated is None:
                return False
            return updated[1] != progress[1] or updated[0] >= progress[0]

        page = wait_for("同步连续看视频任务卡", 8, until=synced)
        task_log.observe(page, announce=True)
        button = page.button_for(TaskKind.video)
        if button is not None and button.text != "去看视频":
            progress_now = page.video_progress()
            if progress_now is not None:
                task_log.write(f"本批看完 {watched_done} 条，任务卡进度 {progress_now[0]}/{progress_now[1]}，可以领奖")
            continue
        after = page.video_progress()
        if after is not None:
            task_log.write(f"本批看完 {watched_done} 条，任务卡进度 {after[0]}/{after[1]}")
        if after is None:
            break
        if stop_requested:
            task_log.write("达到本轮安全上限，结束连续看视频")
            break
        if until_complete and after[1] != progress[1]:
            task_log.write(f"进度已进入下一档 {after[0]}/{after[1]}，继续刷")
            continue
        if after[1] == progress[1] and after[0] == progress[0]:
            batches_without_progress += 1
            if not until_complete or batches_without_progress >= 3:
                task_log.write("连续看视频进度没有增长，已返回任务页", warning=True)
                break
        else:
            batches_without_progress = 0
    return (page, tiers_claimed > 0)


def settle_reward(
    task: TaskKind,
    before: Screen,
    returned: Screen,
    video_claimed: bool,
) -> Tuple[Screen, bool, bool]:
    coins = before.balance()
    if coins is None:
        raise unknown(before, "读取奖励基准")
    deadline = time.monotonic() + 12
    rejected = returned.reward_rejected()
    page = returned
    while True:
        pause(1.5)
        page = reach_task_page()
        rejected = rejected or page.reward_rejected()
        progress_increased = (page.shopping_progress() or 0) > (before.shopping_progress() or 0)
        if task == TaskKind.shop:
            verified = progress_increased
        elif task == TaskKind.video:
            verified = video_claimed
        else:
            verified = True
        current = page.balance()
        if (current if current is not None else coins) > coins and verified and not rejected:
            return (page, True, False)
        if rejected or (task == TaskKind.video and not video_claimed):
            break
        if not time.monotonic() < deadline:
            break
    return (page, False, rejected)


def recover_task(task: TaskKind, before: Screen) -> Tuple[Screen, bool]:
    from droidfleet import log as run_log

    current = read_after_interruption(human_profile(), capture, (ForegroundError, CommandFailure))
    if task == TaskKind.ad and current.ad_countdown():
        task_log.write("恢复后广告仍在计时，继续等待")
        current = finish_ad()
    elif task == TaskKind.ad and current.ad_finished():
        current = return_from_finished_ad(current)
    if task in (TaskKind.ad, TaskKind.shop):
        if not current.task_page():
            current = reach_task_page()
        page, credited, _ = settle_reward(task, before, current, False)
    else:
        page = reach_task_page()
        credited = False
    task_log.observe(page, announce=True)
    run_log.event("recovery.task", task=task.value, coins=page.balance(),
                  progress=page.video_progress() if task == TaskKind.video else page.shopping_progress(),
                  credited=credited)
    return page, credited


def scroll_task_list(screen: Screen, *, target_y: float | None = None,
                     correction: bool = False) -> None:
    purpose = "correction" if correction else "near" if target_y is not None else "search"
    swipe(screen, down=target_y is None or target_y > 0.52,
          area=(0.42, 0.34, 0.53, 0.81), distance=human_profile().list_distance(purpose))


# ---- 指定视频 -----------------------------------------------------------


def read_target_video(stage: str, seconds: float) -> Screen:
    screen = wait_for_daily(
        stage,
        seconds,
        known_popup=lambda item: item.rest_reminder() or item.target_video_offer() is not None,
        until=lambda item: (item.target_video_badge() is not None or item.rest_reminder()
                            or item.network_error() or item.target_video_offer() is not None),
    )
    if screen.rest_reminder():
        raise RunError("应用提示休息，已结束本批指定视频")
    if screen.network_error():
        raise RunError("指定视频页提示网络错误")
    if screen.target_video_offer() is not None:
        raise RunError("指定视频页有「继续领金币」提示")
    return screen


def next_target_video(initial: Screen) -> Screen:
    screen = initial
    previous_coins = initial.target_video_coins()
    for _ in range(2):
        swipe(screen, down=True, area=(0.41, 0.27, 0.53, 0.77), distance=(0.33, 0.43))
        screen = read_target_video("读取下一条指定视频", 6)
        badge = screen.target_video_badge()
        if badge is not None and badge.text == "赚钱中":
            return screen
        coins = screen.target_video_coins()
        if previous_coins is not None and coins is not None and coins > previous_coins:
            return screen
    raise unknown(screen, "上划后未恢复指定视频计奖")


def keep_feed_active(screen: Screen, *, low: float = 3.0, high: float = 5.0) -> Screen:
    """连续视频/指定视频：隔 3~5 秒做一次小滑动，保持屏幕活跃。

    滑动距离刻意压小，避免把正在计时的视频划走；返回刷新后的页面。
    """
    profile = human_profile()
    pause(profile.rng.uniform(low, high))
    try:
        swipe(screen, down=bool(profile.rng.getrandbits(1)),
              area=(0.40, 0.30, 0.60, 0.72), distance=(0.06, 0.12))
    except Exception:  # noqa: BLE001 - 单次手势失败不影响计时
        pass
    try:
        return capture()
    except Exception:  # noqa: BLE001
        return screen


def resume_target_playback(feed: Screen) -> Screen:
    """「已暂停」时上划一条视频，把计奖重新拉起来。"""
    for _ in range(3):
        swipe(feed, down=True, area=(0.40, 0.28, 0.60, 0.75), distance=(0.20, 0.28))
        pause(1.5)
        feed = read_target_video("恢复指定视频计奖", 6)
        badge = feed.target_video_badge()
        if badge is None or badge.text == "赚钱中":
            return feed
    return feed


def finish_target_video_clip(initial: Screen) -> Screen:
    feed = initial
    watch = feed.target_video_watch_seconds()
    max_watch = min(120, max(60, (watch if watch is not None else 30) + 30))
    deadline = time.monotonic() + max_watch
    while True:
        badge = feed.target_video_badge()
        if badge is None:
            break
        if badge.text != "赚钱中":
            feed = resume_target_playback(feed)
            badge = feed.target_video_badge()
            if badge is None or badge.text != "赚钱中":
                break
        if not time.monotonic() < deadline:
            raise unknown(feed, "指定视频计奖超时")
        feed = keep_feed_active(feed)
        if feed.target_video_offer() is not None or feed.rest_reminder():
            break
    return feed


def open_target_video_settlement(feed: Screen) -> Screen:
    pause_after_task("指定视频任务")
    back = feed.target_video_back()
    if back is not None:
        tap(back, feed)
    else:
        exit_after_task(feed, "指定视频退出")
    return wait_for_daily(
        "读取指定视频结算",
        12,
        known_popup=lambda screen: screen.target_video_offer() is not None,
        until=lambda screen: screen.task_page() or screen.target_video_offer() is not None,
    )


def run_target_video(initial: Screen) -> Tuple[Screen, bool]:
    before: Optional[Screen] = None
    offer = initial.target_video_offer()
    if offer is not None:
        tap(offer[0], initial)
        feed = read_target_video("恢复指定视频", 12)
    elif initial.target_video_badge() is not None:
        feed = initial
    else:
        page = initial
        previous_text: list[str] = []
        previous_side: bool | None = None
        stale = 0
        for _ in range(7):
            if page.button_for(TaskKind.target_video) is not None:
                break
            texts = [line.text for line in page.lines if line.y > 0.10]
            stale = stale + 1 if texts and texts == previous_text else 0
            if stale >= 2:
                break
            previous_text = texts
            row = page.task_row(TaskKind.target_video)
            y = row.y if row and not 0.23 <= row.y <= 0.70 else None
            side = y > 0.70 if y is not None else None
            scroll_task_list(page, target_y=y,
                             correction=side is not None and previous_side is not None and side != previous_side)
            previous_side = side
            page = wait_for_daily("查找指定视频任务", 6, until=lambda item: item.task_page())
        entry = page.button_for(TaskKind.target_video)
        if entry is None or page.balance() is None:
            task_log.write("当前没有可执行的指定视频入口", warning=True)
            return (reach_task_page(), False)
        before = page
        task_log.observe(page)
        tap(entry, page, quick=True)
        feed = read_target_video("进入指定视频页", 18)

    clips = 3
    task_log.set_progress(f"0/{clips}", announce=False)
    task_log.write("开始一批指定视频")
    badge = feed.target_video_badge()
    if (badge is not None and badge.text == "已暂停"
            and (before is None or before.balance() is None
                 or (feed.target_video_coins() or 0) <= (before.balance() or 0))):
        feed = next_target_video(feed)
    for watched in range(1, clips + 1):
        feed = finish_target_video_clip(feed)
        task_log.set_progress(f"{watched}/{clips}", announce=False)
        coins = feed.target_video_coins()
        task_log.write(f"本条计奖已暂停，视频页显示 {coins} 金币" if coins is not None else "本条计奖已暂停")
        if watched < clips:
            feed = next_target_video(feed)

    returned = open_target_video_settlement(feed)
    offer = returned.target_video_offer()
    if offer is not None:
        tap(offer[0], returned)
        task_log.write(f"按页面提示继续观看 {offer[1]} 秒")
        feed = read_target_video("继续领指定视频金币", 12)
        deadline = time.monotonic() + offer[1]
        for _ in range(3):
            badge = feed.target_video_badge()
            if badge is not None and badge.text == "已暂停":
                feed = next_target_video(feed)
            feed = finish_target_video_clip(feed)
            if time.monotonic() >= deadline:
                break
        returned = open_target_video_settlement(feed)
    if returned.target_video_offer() is not None:
        task_log.write("续看后仍有「继续领金币」提示，已停在提示页等待结算", warning=True)
        return (returned, False)

    if before is None or before.balance() is None:
        task_log.observe(returned)
        task_log.write("已返回任务页，本次从视频页续跑，起始金币未读到", warning=True)
        return (returned, False)
    coins = before.balance()
    result = settle_reward(TaskKind.target_video, before, returned, False)
    task_log.observe(result[0])
    if result[1]:
        ending = result[0].balance()
        if ending is not None:
            task_log.credit(ending - coins)
    else:
        task_log.write("应用提示「金币溜走了」，未确认到账" if result[2] else "任务页余额未确认增加", warning=True)
    return (result[0], result[1])


# ---- 每日任务 -----------------------------------------------------------


def find_daily_entry(
    task: DailyTask,
    initial: Screen,
) -> Tuple[Screen, Optional[ScreenText], Optional[str]]:
    screen = initial
    stale = 0
    previous_y: float | None = None
    for _ in range(7):
        entry = screen.daily_entry(task)
        if entry is not None:
            return (screen, entry[0], entry[1])
        row = next((line for line in screen.lines if line.text.startswith(task.value)
                    and line.x <= 0.66 and line.y > 0.10), None)
        y = row.y if row is not None and not 0.23 <= row.y <= 0.70 else None
        correction = previous_y is not None and y is not None and (previous_y > 0.70) != (y > 0.70)
        scroll_task_list(screen, target_y=y, correction=correction)
        previous_y = y
        scrolled = wait_for_daily(
            "查看日常任务列表",
            6,
            known_popup=lambda item: task == DailyTask.sign_in and item.daily_sign_in_panel(),
            until=lambda item: item.task_page() or (task == DailyTask.sign_in and item.daily_sign_in_panel()),
        )
        if task == DailyTask.sign_in and scrolled.daily_sign_in_panel():
            return (scrolled, None, None)
        texts = [line.text for line in scrolled.lines if line.y > 0.10]
        stale = stale + 1 if texts and texts == [line.text for line in screen.lines if line.y > 0.10] else 0
        screen = scrolled
        if stale >= 2:
            break
    entry = screen.daily_entry(task)
    if entry is None:
        return (screen, None, None)
    return (screen, entry[0], entry[1])


def finish_daily_sign_in(panel: Screen) -> Optional[str]:
    action = panel.daily_text("^签到领", x=(0.25, 0.75), y=(0.49, 0.71))
    if action is not None:
        streak_line = panel.daily_text("连续签到[0-9]+天", y=(0.34, 0.52))
        streak = streak_line.text if streak_line is not None else None
        tap(action, panel)

        def matched(screen: Screen) -> bool:
            if not screen.daily_sign_in_panel():
                return False
            if screen.daily_text("打开签到提醒|签到奖励|今日已签到", y=(0.32, 0.75)) is not None:
                return True
            updated_line = screen.daily_text("连续签到[0-9]+天", y=(0.34, 0.52))
            updated = updated_line.text if updated_line is not None else None
            return (streak is not None and updated is not None and updated != streak
                    and screen.daily_text("^签到领", x=(0.25, 0.75), y=(0.49, 0.71)) is None)

        final = wait_for_daily(
            "核对连续签到",
            10,
            known_popup=lambda item: item.daily_sign_in_panel(),
            until=matched,
        )
        result: Optional[str] = "确认已签到"
    else:
        if panel.daily_text("打开签到提醒|今日已签到", y=(0.32, 0.75)) is None:
            raise unknown(panel, "读取连续签到状态")
        final = panel
        result = None
    close_daily_panel(final, (0.804, 0.305), "关闭连续签到弹窗")
    return result


def finish_daily_wallet() -> Optional[str]:
    page = wait_for_daily(
        "打开天天领金币",
        15,
        known_popup=lambda screen: screen.daily_wallet_offer() is not None,
        until=lambda screen: screen.daily_wallet_card() is not None or screen.daily_wallet_offer() is not None,
    )
    offer = page.daily_wallet_offer()
    if offer is not None:
        close_daily_panel(page, offer, "关闭钱包推广弹窗")
        page = wait_for_daily(
            "读取钱包每日奖励",
            8,
            until=lambda screen: screen.daily_wallet_card() is not None and screen.daily_wallet_offer() is None,
        )

    def settled(screen: Screen) -> bool:
        card = screen.daily_wallet_card()
        if card is None:
            return False
        return screen.daily_text(
            "^今日已领|^明日再领",
            x=(0.05, 0.91),
            y=(card.y + 0.06, card.y + 0.16),
        ) is not None

    if settled(page):
        return None
    card = page.daily_wallet_card()
    row = None
    if card is not None:
        row = page.daily_text("^今日可领", x=(0.05, 0.48), y=(card.y + 0.06, card.y + 0.16))
    action = None
    if row is not None:
        action = page.daily_text("^领取$", x=(0.65, 0.91), y=(row.y - 0.025, row.y + 0.025))
    if action is None:
        raise unknown(page, "读取钱包每日奖励")
    tap(action, page)
    wait_for_daily("核对钱包每日奖励", 10, until=settled)
    return "确认已领钱包每日金币"


def finish_daily_week() -> Optional[str]:
    def settled(screen: Screen) -> bool:
        return screen.daily_week_receipt() or (
            screen.daily_week_page()
            and screen.daily_text("今日已领|明日再来", y=(0.66, 0.86)) is not None
        )

    page = wait_for_daily(
        "打开7天签到",
        15,
        known_popup=settled,
        until=lambda screen: screen.daily_week_page() or settled(screen),
    )
    if settled(page):
        return None
    action = page.daily_text("^立即领取$", x=(0.30, 0.70), y=(0.70, 0.84))
    if action is None:
        raise unknown(page, "读取7天签到领取状态")
    tap(action, page)
    wait_for_daily("核对7天签到奖励", 10, known_popup=lambda screen: screen.daily_week_receipt(), until=settled)
    return "确认已领7天签到的今日金币"


def finish_daily_hammer() -> Optional[str]:
    def settled(screen: Screen) -> bool:
        return screen.daily_hammer_page() and screen.daily_text("今日已锤|明日再来", y=(0.54, 0.89)) is not None

    def receipt(screen: Screen) -> bool:
        return screen.daily_hammer_page() and screen.daily_text("锤开啦|今日锤得", y=(0.20, 0.60)) is not None

    page = wait_for_daily(
        "打开每天锤一下",
        15,
        until=lambda screen: screen.daily_hammer_page() and not screen.daily_hammer_reminder(),
    )
    if settled(page):
        return None
    action = page.daily_text("^锤一下$", x=(0.28, 0.72), y=(0.74, 0.91))
    if action is None:
        raise unknown(page, "读取锤子任务状态")
    tap(action, page)
    result = wait_for_daily(
        "核对锤子任务",
        12,
        known_popup=receipt,
        until=lambda screen: settled(screen) or receipt(screen),
    )
    if receipt(result):
        accept = result.daily_text("^开心收下$", x=(0.25, 0.75), y=(0.48, 0.82))
        if accept is not None:
            tap(accept, result)
            wait_for_daily("核对今日锤子状态", 8, until=settled)
    return "确认今日已锤"


def finish_daily_reservation() -> Optional[str]:
    # 新版页面：先领今日（直接领 240 / 看视频 10 秒领 720），再预约明日。
    page = wait_for_daily(
        "打开天天预约领金币",
        15,
        until=lambda screen: (screen.daily_reservation_page()
                              or screen.daily_reservation_receipt()
                              or screen.daily_reservation_booked()),
    )
    collected = False
    direct = page.daily_text(r"^直接领", x=(0.05, 0.95), y=(0.45, 0.82))
    if direct is not None:
        tap(direct, page)
        pause(1.5)
        page = capture()
        accept = page.daily_text(r"^(开心收下|领取|立即领取)$", x=(0.2, 0.8), y=(0.3, 0.9))
        if accept is not None:
            tap(accept, page)
            collected = True
            pause(1.0)
            page = capture()
    reserve = page.daily_text(r"^(立即预约领取|预约明日领取|立即预约)$", x=(0.15, 0.85), y=(0.3, 0.92))
    if reserve is not None:
        tap(reserve, page)
        pause(1.5)
        page = wait_for_daily(
            "核对明日预约",
            10,
            until=lambda screen: (screen.daily_reservation_booked()
                                  or screen.daily_text(r"^恭喜预约成功$", y=(0.2, 0.9)) is not None),
        )
    dismiss = (page.daily_text(r"^不用提醒我也能来$", x=(0.15, 0.85), y=(0.4, 0.9))
               or page.daily_text(r"^(开心收下|知道了|关闭)$", x=(0.2, 0.8), y=(0.3, 0.9)))
    if dismiss is not None:
        tap(dismiss, page)
        pause(0.8)
    if collected:
        return "今日金币已领，明日已预约"
    return "已预约明日金币"


def finish_daily_reservation_legacy() -> Optional[str]:
    def after_claim(screen: Screen) -> bool:
        return (screen.daily_reservation_receipt() or screen.daily_reservation_button() is not None
                or screen.daily_reservation_booked())

    def known_panel(screen: Screen) -> bool:
        return screen.daily_reservation_receipt() or screen.daily_reservation_booked()

    page = wait_for_daily(
        "打开天天预约领金币",
        15,
        known_popup=known_panel,
        until=lambda screen: after_claim(screen) or screen.daily_reservation_claim() is not None,
    )
    collected_today = False
    if not after_claim(page):
        direct = page.daily_reservation_claim()
        if direct is not None:
            tap(direct, page)
            page = wait_for_daily("核对预约金币领取", 12, known_popup=known_panel, until=after_claim)
            collected_today = True
            task_log.set_progress("今日金币已领")
    if page.daily_reservation_receipt():
        accept = (page.daily_text("^开心收下$", x=(0.25, 0.75), y=(0.35, 0.88))
                  or page.daily_text("^继续预约明日金币$", x=(0.25, 0.75), y=(0.35, 0.88)))
        if accept is None:
            raise unknown(page, "读取预约金币到账弹窗")
        tap(accept, page)
        page = wait_for_daily(
            "读取次日预约状态",
            10,
            known_popup=lambda screen: screen.daily_reservation_booked(),
            until=lambda screen: (not screen.daily_reservation_receipt()
                                  and (screen.daily_reservation_button() is not None
                                       or screen.daily_reservation_booked())),
        )
    booked_tomorrow = False
    if not page.daily_reservation_booked():
        reserve = page.daily_reservation_button()
        if reserve is None:
            raise unknown(page, "读取次日预约入口")
        tap(reserve, page)
        page = wait_for_daily(
            "核对次日预约",
            12,
            known_popup=lambda screen: screen.daily_reservation_booked(),
            until=lambda screen: screen.daily_reservation_booked(),
        )
        booked_tomorrow = True
    dismiss = page.daily_text("^不用提醒我也能来$", x=(0.24, 0.76), y=(0.48, 0.88))
    if dismiss is not None:
        tap(dismiss, page)
        pause(0.7)
    if collected_today:
        return "确认今日金币已领，次日已预约"
    return "确认次日已预约" if booked_tomorrow else None


def start_task_earning(key: str, coins: int | None, *, fallback_label: str | None = None) -> earnings.TaskEarnings:
    task = DOUYIN_TASKS.get(key)
    label = task[0] if task is not None else fallback_label or key
    return earnings.start("douyin", label, coins, task_key=key, serial=device_serial())


def run_daily(tasks: Optional[Sequence["DailyTask"]] = None, *, record_earnings: bool = True) -> bool:
    confirmed = "已确认"
    skipped = "已跳过"
    failed = "未确认"

    results: List[Tuple[DailyTask, str, str]] = []
    starting_balance: Optional[int] = None
    started_task = False

    def observe_before_task(screen: Screen) -> None:
        nonlocal starting_balance
        task_log.observe(screen)
        if not started_task and starting_balance is None:
            starting_balance = screen.balance()

    def record(task: DailyTask, status: str, detail: str) -> None:
        results.append((task, status, detail))
        task_log.set_progress(status, announce=False)
        task_log.write(detail, warning=status == failed)

    for task in (tasks if tasks else DailyTask):
        task_log.begin(None, label=task.value)
        key = next(key for key, daily in DAILY_MAP.items() if daily == task)
        attempt = None
        coins_before = None
        coins_after = None
        earning_status = "interrupted"
        try:
            page = reach_task_page(keep_sign_in_panel=(task == DailyTask.sign_in), daily_mode=True)
            observe_before_task(page)
            coins_before = page.balance()
            entry = None
            reason = None
            if task != DailyTask.sign_in or not page.daily_sign_in_panel():
                page, entry, reason = find_daily_entry(task, page)
                observe_before_task(page)
                confirmed_coins = page.balance()
                if confirmed_coins is not None:
                    coins_before = confirmed_coins
            if record_earnings:
                attempt = start_task_earning(key, coins_before)
            if entry is not None:
                started_task = True
                tap(entry, page)
            elif task != DailyTask.sign_in or not page.daily_sign_in_panel():
                earning_status = "skipped"
                coins_after = page.balance()
                record(task, skipped, reason or "任务列表未显示该项入口")
                continue
            started_task = True
            task_log.write("开始任务")
            result: Optional[str]
            if task == DailyTask.sign_in:
                source = page
                if not source.daily_sign_in_panel():
                    source = wait_for_daily(
                        "打开连续签到",
                        12,
                        known_popup=lambda screen: screen.daily_sign_in_panel(),
                        until=lambda screen: screen.daily_sign_in_panel(),
                    )
                result = finish_daily_sign_in(source)
            elif task == DailyTask.wallet:
                result = finish_daily_wallet()
            elif task == DailyTask.week:
                result = finish_daily_week()
            elif task == DailyTask.reservation:
                result = finish_daily_reservation()
            else:
                result = finish_daily_hammer()
            try:
                returned = reach_task_page(daily_mode=True)
                task_log.observe(returned)
                coins_after = returned.balance()
            except Exception:  # noqa: BLE001 - 与 Swift 的 try? 一致
                pass
            earning_status = "skipped" if result is None else "complete"
            record(task, skipped if result is None else confirmed, result or "今日已完成或已预约")
        except DailyTaskSkipped as blocked:
            earning_status = "skipped"
            record(task, skipped, blocked.reason)
        except UnresolvedScreen as error:
            earning_status = "unconfirmed"
            record(task, failed, str(error))
            return False
        except Exception as error:  # noqa: BLE001 - 单任务失败不中断整轮
            record(task, failed, str(error))
        finally:
            if record_earnings:
                if attempt is None:
                    attempt = start_task_earning(key, coins_before)
                attempt.finish(coins_after, earning_status)

    task_log.begin(None, label="每日任务")
    final_page: Optional[Screen] = None
    final_error: Optional[Exception] = None
    try:
        final_page = reach_task_page(daily_mode=True)
    except Exception as error:  # noqa: BLE001 - 汇总结束后再抛出
        final_error = error
    returned = final_page
    if returned is not None:
        task_log.observe(returned)
    ending_balance = returned.balance() if returned is not None else None

    task_log.begin(None, label="每日任务汇总")
    for task, status, detail in results:
        task_log.set_progress(status, announce=False)
        task_log.write(f"{task.value} · {detail}", warning=status == failed)
    done_count = sum(1 for item in results if item[1] == confirmed)
    skipped_count = sum(1 for item in results if item[1] == skipped)
    failed_count = len(results) - done_count - skipped_count
    start_text = str(starting_balance) if starting_balance is not None else "未读到"
    end_text = str(ending_balance) if ending_balance is not None else "未读到"
    coins = f"本轮金币 {start_text} → {end_text}"
    if starting_balance is not None and ending_balance is not None:
        change = ending_balance - starting_balance
        coins += f"，净变化 {'+' if change > 0 else ''}{change}"
    task_log.begin(None, label="每日任务")
    task_log.set_progress(f"确认{done_count}项，跳过{skipped_count}项，未确认{failed_count}项", announce=False)
    task_log.write(coins)
    if final_error is not None:
        raise final_error
    return failed_count == 0


# ---- 入口 ---------------------------------------------------------------


def run(tasks: Sequence[TaskKind], *, record_earnings: bool = True, initial: Screen | None = None) -> bool:
    all_credited = True
    dedicated_video = list(tasks) == [TaskKind.video]
    task_log.begin(tasks[0] if len(tasks) == 1 else None)
    page = initial
    if page is None and list(tasks) == [TaskKind.target_video]:
        try:
            current = capture()
        except Exception:  # noqa: BLE001 - 与 Swift 的 try? 一致
            current = None
        if current is not None and (current.target_video_badge() is not None
                                    or current.target_video_offer() is not None):
            page = current
    if page is None:
        try:
            page = reach_task_page(ignore_rest_reminder=dedicated_video)
        except BaseException:
            if record_earnings and tasks:
                task = tasks[0]
                start_task_earning(task.value, None, fallback_label=task.label).finish(None, "interrupted")
            raise

    for task in tasks:
        task_log.begin(task)
        before = None
        coins = None
        try:
            coins = page.balance()
            task_log.observe(page)
        except BaseException:
            if record_earnings:
                start_task_earning(task.value, coins, fallback_label=task.label).finish(None, "interrupted")
            raise
        coins_after = None
        earning_status = "interrupted"
        attempt = None
        if record_earnings:
            attempt = start_task_earning(task.value, coins, fallback_label=task.label)
        try:
            if task == TaskKind.target_video:
                result = run_target_video(page)
                page = result[0]
                coins_after = page.balance()
                earning_status = "complete" if result[1] else "unconfirmed"
                all_credited = all_credited and result[1]
                continue
            target = page.button_for(task)
            if coins is None or target is None or (task == TaskKind.shop and page.shopping_progress() is None):
                coins_after = coins
                earning_status = "skipped"
                task_log.write("当前没有可执行的入口，跳过", warning=True)
                all_credited = False
                continue
            before = page
            task_log.write("开始任务")
            if task == TaskKind.video:
                returned, video_claimed = finish_video(page, until_complete=dedicated_video)
            elif task == TaskKind.ad:
                tap(target, page)
                returned = finish_ad()
                video_claimed = False
            else:
                returned = finish_shopping(page)
                video_claimed = False
            result = settle_reward(task, before, returned, video_claimed)
            page = result[0]
            coins_after = page.balance()
            if coins_after is None:
                raise unknown(page, "核对金币余额")
            task_log.observe(page)
            if task == TaskKind.ad:
                task_log.set_progress("结算已核对", announce=False)
            if result[1]:
                if task == TaskKind.video:
                    task_log.write(f"本次连续看视频合计确认到账 +{coins_after - coins} 金币")
                else:
                    task_log.credit(coins_after - coins)
            else:
                task_log.write("抖音提示「金币溜走了」，未确认到账" if result[2] else "未确认到账", warning=True)
            earning_status = "complete" if result[1] else "unconfirmed"
            all_credited = all_credited and result[1]
        except (ForegroundError, CommandFailure) as error:
            if task == TaskKind.target_video or before is None:
                raise
            task_log.write(f"{error}，等待恢复并核对当前任务", warning=True)
            try:
                page, credited = recover_task(task, before)
            except UnresolvedScreen:
                earning_status = "unconfirmed"
                return False
            except (ForegroundError, CommandFailure, RunError, ScreenRecognitionError):
                page = reach_task_page()
                task_log.observe(page, announce=True)
                credited = False
            coins_after = page.balance()
            earning_status = "complete" if credited else "unconfirmed"
            all_credited = all_credited and credited
        except UnresolvedScreen as error:
            earning_status = "unconfirmed"
            task_log.write(str(error), warning=True)
            return False
        except Exception as error:  # noqa: BLE001 - 单任务失败继续处理后续任务
            all_credited = False
            if task == TaskKind.target_video:
                task_log.write(str(error), warning=True)
                break
            task_log.write(f"{error}，尝试返回任务页", warning=True)
            page = reach_task_page()
            coins_after = page.balance()
        finally:
            if attempt is not None:
                attempt.finish(coins_after, earning_status)
    return all_credited


# ---- 单任务入口（控制台按钮） ------------------------------------------

DOUYIN_TASKS: dict[str, tuple[str, str]] = {
    "chest": ("开宝箱得金币", "repeat"),
    "video": ("连续看视频30天赚30元", "repeat"),
    "reserve": ("预约领金币", "daily"),
    "ad": ("看广告赚金币", "repeat"),
    "wallet": ("天天领金币", "repeat"),
    "target-video": ("连续刷指定视频赚金币", "repeat"),
    "week": ("7日签到必拿", "daily"),
    "hammer": ("每天锤一下领现金", "daily"),
    "shortcut": ("添加抖音商城到桌面", "once"),
    "sign-in": ("明日签到", "daily"),
}

TASK_ORDER = ("chest", "video", "reserve", "ad", "wallet", "target-video",
              "week", "hammer", "shortcut", "sign-in")

DAILY_MAP = {
    "sign-in": DailyTask.sign_in,
    "reserve": DailyTask.reservation,
    "wallet": DailyTask.wallet,
    "week": DailyTask.week,
    "hammer": DailyTask.hammer,
}


def _raw_screen() -> Screen:
    """不校验前台，用来看系统弹窗。"""
    return Screen(adb("exec-out", "screencap", "-p"))


def run_log_event_tap(label: str, x: int, y: int, *, wait: float | None = None) -> None:
    progress.emit(progress.APP_DOUYIN, task_log.label, "点击", kind="tap",
                  tap=(label, x, y), wait=wait)


def _row_button(screen: Screen, keyword: str,
                buttons: Iterable[str]) -> Optional[Tuple[ScreenText, ScreenText]]:
    rows = [line for line in screen.lines if keyword in line.text]
    best: Optional[Tuple[float, ScreenText, ScreenText]] = None
    for row in rows:
        for line in screen.lines:
            if line.x <= 0.66 or line.y < row.y - 0.06 or line.y > row.y + 0.06:
                continue
            if not any(button in line.text for button in buttons):
                continue
            distance = abs(line.y - row.y)
            if best is None or distance < best[0]:
                best = (distance, line, row)
    return (best[1], best[2]) if best is not None else None


def _scroll_to_row(keyword: str, buttons: Iterable[str]) -> Tuple[Screen, Optional[ScreenText]]:
    screen = reach_task_page()
    for _ in range(8):
        found = _row_button(screen, keyword, buttons)
        if found is not None:
            return screen, found[0]
        scroll_task_list(screen)
        pause(0.8)
        screen = capture()
    return screen, None


def _collect_reward_popups(limit: int = 4, allow_boost: bool = True) -> None:
    boosts = 0
    for _ in range(limit):
        screen = capture()
        coins_now = screen.balance()
        if allow_boost and boosts < 2 and (coins_now is None or coins_now >= 100):
            boost = next((line for line in screen.lines
                          if "翻倍" in line.text or "看广告再赚" in line.text
                          or "看视频再" in line.text), None)
            if boost is not None:
                tap(boost, screen)
                boosts += 1
                _handle_ad_screen(limit=90)
                continue
        button = next((line for line in screen.lines
                       if line.text.strip() in ("开心收下", "收下", "立即领取", "领取")), None)
        if button is None:
            return
        tap(button, screen)
        pause(1.0)


BOOST_WORDS = ("看广告再赚", "看视频再赚", "看广告翻倍", "看视频翻倍", "再看一个视频", "翻倍")


def _find_boost(screen: Screen) -> Optional[ScreenText]:
    return next((line for line in screen.lines
                 if any(word in line.text for word in BOOST_WORDS)), None)


def _boost_video_reward(screen: Screen, limit: float = 120) -> Optional[Screen]:
    """看视频奖励弹窗：金币≥100 时点一次「看广告再赚」翻倍。

    广告结束后通常会回到同一个奖励弹窗，也可能直接回任务页。
    读不到翻倍入口或超时返回 None，调用方继续走「开心收下」。
    """
    coins = screen.balance()
    if coins is not None and coins < 100:
        task_log.write(f"金币 {coins} 低于 100，不再看广告翻倍")
        return screen
    boost = _find_boost(screen)
    if boost is None:
        return screen
    task_log.write("奖励弹窗点击「看广告再赚」翻倍")
    # 弹窗在点下去后会立刻切广告，两次 OCR 比对会误判，这里直接点已识别到的按钮
    tap_text(boost, screen)
    pause(1.2)
    # 先确认广告真的打开了；有时候点了没反应，弹窗还在，不能直接当成已看完
    started = False
    start_deadline = time.monotonic() + 15
    page = screen
    while time.monotonic() < start_deadline:
        try:
            page = capture()
        except Exception:  # noqa: BLE001
            pause(1.0)
            continue
        if not page.video_reward():
            started = True
            break
        pause(1.2)
    if not started:
        task_log.write("点击后广告没有打开，直接收下奖励", warning=True)
        return page
    deadline = time.monotonic() + limit
    with progress.ad_countdown(progress.APP_DOUYIN, task_log.label, limit) as observe_time:
        seconds = page.ad_seconds()
        if seconds is not None:
            observe_time(seconds)
        while time.monotonic() < deadline:
            try:
                page = capture()
            except Exception:  # noqa: BLE001 - 切页瞬间读不到就重试
                pause(1.0)
                continue
            seconds = page.ad_seconds()
            if seconds is not None:
                observe_time(seconds)
            if page.video_reward() or page.task_page():
                return page
            if page.ad_finished():
                if tap_flexible(page, "坚持退出"):
                    pause(1.2)
                    continue
                try:
                    input_cmd("keyevent", "KEYCODE_BACK")
                except Exception:  # noqa: BLE001
                    pass
                pause(1.2)
                continue
            if page.ad_countdown():
                pause(4)
                continue
            if tap_flexible(page, "领取奖励"):
                pause(1.2)
                continue
            if tap_flexible(page, "继续观看"):
                pause(1.5)
                continue
            pause(2)
    return None


def run_chest() -> str:
    screen = reach_task_page()
    coins = screen.balance()
    x, y = (0.67, 0.98), (0.68, 0.95)
    label = r"开宝箱(?:得金币)?"

    def locate(page: Screen) -> Optional[ScreenText]:
        return page.daily_text(f"^{label}$", x=x, y=y) if page.task_page() else None

    chest = locate(screen)
    if chest is not None:
        tap(chest, screen, locate=locate)
    elif not tap_named_control(screen, label, x=x, y=y, label="开宝箱"):
        raise RunError("没有找到可点击的宝箱按钮")
    pause(2.0)
    _handle_ad_screen(limit=90)

    # 宝箱奖励页常有「看广告再赚」：一直看，直到没有这个入口或金币低于 100
    rounds = 0
    while rounds < 10:
        try:
            page = capture()
        except Exception:  # noqa: BLE001
            break
        boost = _find_boost(page)
        if boost is None:
            _collect_reward_popups(limit=2, allow_boost=False)
            page = capture()
            boost = _find_boost(page)
            if boost is None:
                break
        if coins is not None and coins < 100:
            progress.emit(progress.APP_DOUYIN, task_log.label,
                          f"金币 {coins} 低于 100，停止看广告翻倍")
            break
        progress.emit(progress.APP_DOUYIN, task_log.label, "找到翻倍入口", coins=coins)
        if not tap_flexible(page, "看广告再赚") and not tap_flexible(page, "看视频再赚"):
            tap_flexible(page, "翻倍")
        rounds += 1
        pause(1.5)
        _handle_ad_screen(limit=120)
        # 广告结束后原宝箱弹窗会回来，把基础+翻倍的金币一起收下
        for _ in range(5):
            try:
                latest = capture()
            except Exception:  # noqa: BLE001
                break
            if tap_flexible(latest, "开心收下") or tap_flexible(latest, "收下"):
                pause(1.0)
                break
            if latest.task_page():
                pause(1.0)
                continue
            break
        try:
            latest = capture()
            value = latest.balance()
            if value is not None:
                coins = value
        except Exception:  # noqa: BLE001
            pass

    reach_task_page()
    if rounds:
        return f"宝箱已开启，连续看广告 {rounds} 次"
    return "宝箱已开启"


def run_shortcut() -> str:
    screen, button = _scroll_to_row("添加抖音商城到桌面", ("去添加", "立即添加", "添加"))
    if button is None:
        _screen, already = _find_entry_generic(("打开抖音商城小组件", "添加抖音商城"),
                                               ("去打开", "打开", "去添加"))
        if already is not None:
            return "之前已经添加过（入口已变成「打开抖音商城小组件」）"
        raise RunError("任务列表里没有「添加抖音商城到桌面」入口")
    if "已添加" in button.text:
        return "之前已经添加过"
    tap(button, screen)
    pause(2.0)
    added = False
    for _ in range(3):
        try:
            system = _raw_screen()
        except Exception:  # noqa: BLE001 - 截图失败时交给下一步
            break
        labels = ("添加", "添加到主屏幕")
        if any(matches(line.text, r"添加.*(?:主屏幕|桌面)") for line in system.lines):
            labels += ("确定", "允许")
        confirm = max((line for line in system.lines if line.text.strip() in labels),
                      key=lambda line: line.y, default=None)
        if confirm is None:
            break
        tap(confirm, system, quick=True, system_dialog=True)
        added = True
        pause(1.5)
    if not added:
        task_log.write("没有看到系统确认弹窗，可能已直接添加")
    check_foreground()
    reach_task_page()
    return "已添加到桌面"


GENERIC_DAILY: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "week": (("7天签到", "7日签到"), ("去领取", "立即领取", "领取")),
    "hammer": (("每天锤一下", "锤一下"), ("锤一下", "去锤", "去完成")),
    "wallet": (("天天领金币",), ("去领取", "去完成", "领取")),
    "sign-in": (("明日签到", "连续签到"), ("去查看", "去签到", "去领取")),
    "reserve": (("明日0点", "预约领金币", "天天预约"), ("去预约", "立即预约领取", "去领取")),
}
CLAIM_BUTTONS = ("立即领取", "开心收下", "领取", "拆开礼包", "看广告领取", "继续预约明日金币")


def _find_entry_generic(keywords: Sequence[str], buttons: Sequence[str],
                        tries: int = 8) -> Tuple[Screen, Optional[ScreenText]]:
    screen = reach_task_page()
    for _ in range(tries):
        for keyword in keywords:
            found = _row_button(screen, keyword, buttons)
            if found is not None:
                return screen, found[0]
        scroll_task_list(screen)
        pause(0.8)
        screen = capture()
    return screen, None


def _handle_ad_screen(limit: float = 150) -> Screen:
    """通用广告页处理：倒计时 / 继续观看 / 领取 / 坚持退出。"""
    deadline = time.monotonic() + limit
    with progress.ad_countdown(progress.APP_DOUYIN, task_log.label, limit) as observe_time:
        while time.monotonic() < deadline:
            try:
                screen = capture()
            except Exception:  # noqa: BLE001 - 切页瞬间读不到就重试
                pause(1.0)
                continue
            seconds = screen.ad_seconds()
            if seconds is not None:
                observe_time(seconds)
            if screen.ad_finished():
                # 不要走 finish_ad 回任务页，否则宝箱的翻倍弹窗会被丢掉
                if tap_flexible(screen, "坚持退出"):
                    pause(1.5)
                    continue
                try:
                    input_cmd("keyevent", "KEYCODE_BACK")
                except Exception:  # noqa: BLE001
                    pass
                pause(1.5)
                continue
            # 金币不低于 100 时，优先点「看广告/看视频翻倍」，把奖励做大
            coins_now = screen.balance()
            if coins_now is None or coins_now >= 100:
                for word in ("看广告再赚", "看视频再赚", "翻倍"):
                    if tap_flexible(screen, word):
                        pause(1.5)
                        break
                else:
                    word = None
                if word is not None:
                    continue
            if any(tap_flexible(screen, word) for word in ("领取奖励", "开心收下", "立即领取")):
                pause(1.0)
                continue
            if screen.ad_countdown():
                pause(5)
                continue
            if tap_flexible(screen, "继续观看"):
                pause(2.0)
                continue
            if tap_flexible(screen, "坚持退出"):
                pause(1.0)
                continue
            if screen.task_page():
                return screen
            pause(2.0)
    return capture()


def _run_ad() -> Screen:
    screen, button = _find_entry_generic(("看广告",), ("去领取", "去完成"))
    if button is None:
        raise RunError("任务列表里没有「看广告赚金币」入口")
    tap(button, screen)
    pause(1.5)
    _handle_ad_screen()
    return reach_task_page()


def _generic_daily(key: str) -> Screen | None:
    keywords, buttons = GENERIC_DAILY[key]
    screen = reach_task_page()
    button = None
    for _ in range(8):
        for keyword in keywords:
            found = _row_button(screen, keyword, buttons)
            if found is not None:
                button = found[0]
                break
        if button is not None:
            break
        scroll_task_list(screen)
        pause(0.8)
        screen = capture()
    if button is None:
        raise RunError(f"任务列表里没有找到「{keywords[0]}」入口")
    if any(word in button.text for word in ("已领取", "已完成", "已签到", "已预约")):
        return None
    tap(button, screen)
    pause(1.5)
    for _ in range(6):
        screen = capture()
        if screen.ad_countdown() or screen.ad_finished():
            screen = finish_ad()
            continue
        claim = next((line for line in screen.lines
                      if line.text.strip() in CLAIM_BUTTONS), None)
        if claim is None:
            break
        tap(claim, screen)
        pause(1.2)
    return reach_task_page()


def _run_target_video_keepalive() -> Screen:
    """指定视频：每 3~5 秒滑动一次，保持「赚钱中」。"""
    screen, button = _find_entry_generic(("连续刷指定视频",), ("去领取", "去完成"))
    if button is None:
        raise RunError("任务列表里没有「连续刷指定视频赚金币」入口")
    tap(button, screen)
    pause(2.0)
    feed = wait_for_daily(
        "进入指定视频页",
        15,
        until=lambda item: (item.target_video_back() is not None
                            or item.target_video_badge() is not None
                            or item.target_video_offer() is not None),
    )
    if feed.target_video_offer() is not None:
        tap(feed.target_video_offer()[0], feed)
        pause(1.5)
        feed = capture()
    start_coins = feed.target_video_coins()
    profile = human_profile()
    deadline = time.monotonic() + 150
    active_clips = 0
    while time.monotonic() < deadline:
        pause(profile.rng.uniform(3.0, 5.0))
        swipe(feed, down=True, area=(0.40, 0.28, 0.60, 0.75), distance=(0.22, 0.30))
        pause(1.0)
        try:
            feed = capture()
        except Exception:  # noqa: BLE001
            break
        badge = feed.target_video_badge()
        if badge is None:
            offer = feed.target_video_offer()
            if offer is not None:
                # 「再看 N 秒最高可得」：接住它，边滑边等
                tap(offer[0], feed)
                pause(1.5)
                offer_deadline = time.monotonic() + offer[1]
                feed = capture()
                while time.monotonic() < offer_deadline:
                    pause(profile.rng.uniform(3.0, 5.0))
                    swipe(feed, down=True, area=(0.40, 0.28, 0.60, 0.75),
                          distance=(0.22, 0.30))
                    pause(1.0)
                    try:
                        feed = capture()
                    except Exception:  # noqa: BLE001
                        break
                break
            if feed.task_page():
                break
            continue
        if badge.text == "赚钱中":
            active_clips += 1
        else:
            # 暂停时立刻再滑一条，把计时拉起来
            swipe(feed, down=True, area=(0.40, 0.28, 0.60, 0.75), distance=(0.24, 0.32))
            pause(1.5)
            feed = capture()
        coins = feed.target_video_coins()
        if start_coins is not None and coins is not None and coins > start_coins:
            break
    back = feed.target_video_back()
    if back is not None:
        tap(back, feed)
    else:
        input_cmd("keyevent", "KEYCODE_BACK")
    pause(2.0)
    offer = None
    try:
        offer = capture().target_video_offer()
    except Exception:  # noqa: BLE001
        pass
    if offer is not None:
        tap(offer[0], capture())
        pause(1.5)
    page = reach_task_page()
    task_log.write(f"指定视频保持活跃滑动 {active_clips} 次")
    return page


def run_task(key: str) -> bool:
    if key not in DOUYIN_TASKS:
        raise RunError(f"未知任务：{key}")
    label, _kind = DOUYIN_TASKS[key]
    task_log.begin(None, label=label)
    attempt = None
    ending_page = None
    ok = True
    status = "interrupted"
    try:
        screen = reach_task_page()
        attempt = start_task_earning(key, screen.balance())
        if key == "ad":
            ending_page = _run_ad()
        elif key == "video":
            ok = run([TaskKind.video], record_earnings=False, initial=screen)
        elif key == "target-video":
            ending_page = _run_target_video_keepalive()
        elif key in GENERIC_DAILY:
            ending_page = _generic_daily(key)
        elif key in DAILY_MAP:
            ok = run_daily([DAILY_MAP[key]], record_earnings=False)
        elif key == "chest":
            run_chest()
        elif key == "shortcut":
            run_shortcut()
        status = "complete" if ok else "unconfirmed"
    finally:
        if attempt is None:
            attempt = start_task_earning(key, None)
            attempt.finish(None, status)
        else:
            after = None
            try:
                after = (ending_page if ending_page is not None else reach_task_page()).balance()
            except Exception:  # noqa: BLE001 - 结束时读不到余额就留空
                pass
            except BaseException:
                status = "interrupted"
                raise
            finally:
                attempt.finish(after, status)
    return ok


def run_task_all() -> bool:
    results: list[tuple[str, bool]] = []
    for key in TASK_ORDER:
        label = DOUYIN_TASKS[key][0]
        try:
            ok = run_task(key)
        except Exception as error:  # noqa: BLE001 - 单项失败不中断整轮
            progress.emit(progress.APP_DOUYIN, label, f"失败：{error}", status="failed", level="error")
            ok = False
        results.append((label, ok))
    failed = [label for label, ok in results if not ok]
    progress.emit(progress.APP_DOUYIN, "本轮任务",
                  f"流程结束 {len(results) - len(failed)}/{len(results)}", kind="round.summary")
    for label, ok in results:
        progress.emit(progress.APP_DOUYIN, label, "流程结束" if ok else "未确认", kind="round.task")
    return not failed


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def inspect_screen(screen: Screen) -> None:
    if screen.popup_suspected():
        print("检测到疑似弹窗遮挡")
    balance = screen.balance()
    print(f"任务页：{_bool_text(screen.task_page())}，余额：{balance if balance is not None else '未读到'}")
    shopping = screen.shopping_progress()
    print(f"逛街完成次数：{shopping if shopping is not None else '未读到'}")
    video = screen.video_progress()
    if video is not None:
        print(f"连续看视频进度：{video[0]}/{video[1]}")
    if screen.earning_tab() is not None:
        print("推荐视频页：已识别赚钱入口")
    badge = screen.target_video_badge()
    if badge is not None:
        print(f"指定视频页：{badge.text}")
    for task in TaskKind:
        button = screen.button_for(task)
        if button is not None:
            position = progress.pixel_position(button.x * screen.width, button.y * screen.height)
            print(f"{task.label}：{button.text}（{position}）")
    if screen.ad_countdown():
        print("广告倒计时")
    if screen.ad_finished():
        print("广告页显示领取成功")
    if screen.reward_rejected():
        print("抖音提示金币溜走了")
    seconds = screen.shop_seconds()
    if seconds is not None:
        print(f"逛街剩余 {seconds} 秒")
    if screen.network_error():
        print("逛街页提示网络错误")


def _run_cli(argv: Sequence[str]) -> int:
    try:
        if not argv or argv[0] == "--help":
            print(USAGE)
            return 0
        if argv[0] == "--inspect" and len(argv) == 1:
            inspect_screen(capture())
            return 0
        if argv[0] == "--image" and len(argv) == 2:
            try:
                png = Path(argv[1]).read_bytes()
            except OSError as error:
                raise RunError(f"无法读取截图：{error}") from error
            inspect_screen(Screen(png))
            return 0
        if argv[0] == "--check-ollama" and len(argv) == 1:
            check_ollama()
            return 0
        if argv[0] == "--run" and len(argv) == 1:
            return 0 if run([TaskKind.ad, TaskKind.video]) else 2
        if argv == ["--run", "daily"]:
            return 0 if run_daily() else 2
        if argv[0] == "--run" and len(argv) == 2:
            try:
                task = TaskKind(argv[1])
            except ValueError as error:
                raise RunError(USAGE) from error
            return 0 if run([task]) else 2
        if argv[0] == "--task" and len(argv) == 2:
            if argv[1] == "all":
                return 0 if run_task_all() else 2
            return 0 if run_task(argv[1]) else 2
        raise RunError(USAGE)
    except KeyboardInterrupt:
        return 130
    except Exception as error:  # noqa: BLE001 - 入口统一输出错误信息
        message = str(error) or error.__class__.__name__
        if argv and argv[0] == "--run" and message != USAGE:
            task_log.write(message, warning=True, to=sys.stderr)
        else:
            sys.stderr.write(message + "\n")
        return 1


def main(argv: Sequence[str]) -> int:
    from droidfleet import log as run_log

    logger = run_log.init_run("douyin", list(argv))
    try:
        code = _run_cli(argv)
    except SystemExit as exc:
        logger.close(status="exit", exit_code=getattr(exc, "code", None))
        raise
    except BaseException as error:
        logger.error("run.error", error)
        logger.close(status="error")
        raise
    logger.close(status="ok" if code == 0 else "incomplete", exit_code=code)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
