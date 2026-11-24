#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快手极速版「去赚钱」任务自动化（adb 截屏 + Vision OCR 驱动）。

为什么用 OCR 而不是 uiautomator：
    该机型（SM-G986U1 / Android 13）上快手任务中心长期有悬浮动画
    （「大额奖励即将到账」等），uiautomator dump 经常报
    "could not get idle state" 并返回过期层级；而 screencap + Vision OCR
    稳定可用。优先使用可信的控件边界，缺失时用 OCR 文本框。

任务类型与处理：
    A 一键领取      -> 领奖弹窗 -> 关闭弹窗
    B 看广告得金币  -> AwardVideoPlayActivity -> 底部 CTA -> 广告/直播间 -> 关闭 -> 领奖
    C 刷广告视频    -> PhotoDetailActivity 广告信息流，逐条停留后上滑
    D 看内容领金币  -> AdKwaiRnActivity 应用推广页，只点「直接领金币」，下载类一律放弃
    E H5 活动       -> KwaiYodaWebViewActivity 内的「开心收下 / 立即领取」
    F 看直播        -> 停留 95s 后关闭并退出直播间

硬性黑名单（永不点击）：认证 / 实名 / 银行卡 / 身份证 / 驾照 / 绑定 / 支付 / 提现 /
借钱贷款 / 下单购买 / 商城逛街 / 下载安装 / 邀请分享 / 通讯录 / 第三方 App 跳转 /
刮奖抽奖翻倍竞猜报名 / 签到日历睡觉 等，详见 DENY_PATTERNS。

用法：
    python3 kuaishou_rewards.py --dry-run              # 只扫描打印计划
    python3 kuaishou_rewards.py --minutes 15           # 跑 15 分钟
    python3 kuaishou_rewards.py --rounds 1 --minutes 5 --verbose
    python3 kuaishou_rewards.py --only "广告|直播"      # 只跑匹配的任务

输出：stdout 逐条状态 + logs/kuaishou/ 下的每次运行日志
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from droidfleet import earnings, log as run_log, progress, safety
from droidfleet.human import HumanProfile, Observation, confirm_stable, observe_tap, read_after_interruption
from droidfleet.ollama import (
    RecoveryDecision, diagnose_screen, locate_exit, matches_dismissal, recover_screen, risky_dismissal,
)
from droidfleet.rules import load_rules

PKG = "com.kuaishou.nebula"
MAIN_ACTIVITY = f"{PKG}/com.yxcorp.gifshow.HomeActivity"
DEFAULT_SERIAL = (os.environ.get("ADB_SERIAL") or os.environ.get("ANDROID_SERIAL")
                  or load_rules("kuaishou").get("app.kuaishou.default_serial", ""))

SCREEN_W, SCREEN_H = 1080, 2400
CONTENT_BOTTOM = 2127        # 底部导航上沿
TAP_MAX_Y = 2060            # 普通任务的点击上界
Bounds = tuple[float, float, float, float]

CENTER_TITLE = "任务中心"

ACTION_LABELS = (
    "领福利", "去领取", "去观看", "去完成", "去打开", "去搜索", "去参与", "去赚钱",
    "去翻卡", "去打卡", "拆盲盒", "领红包", "去提现", "去下单", "去逛街", "去游玩",
    "去邀请", "喂饲料", "去看剧", "去浏览", "去抽奖", "去分享", "去刮奖", "去赚金",
    "去报名", "去翻倍", "去挖矿", "待睡觉", "查看日历", "领现金", "领",
)

DENY_PATTERNS = [
    r"认证", r"实名", r"银行卡", r"信用卡", r"身份证", r"驾照", r"驾驶证", r"绑定",
    r"支付", r"提现", r"借钱", r"贷款", r"借款", r"额度", r"分期",
    r"下单", r"购买", r"购物", r"商城", r"逛街", r"安装", r"下载",
    r"邀请", r"通讯录", r"分享", r"游戏", r"开通", r"注册",
    r"支付宝", r"夸克", r"快影", r"喜番", r"淘宝", r"微信", r"蚂蚁",
    r"刮奖", r"抽奖", r"翻倍", r"竞猜", r"报名", r"钓鱼", r"游玩", r"挖宝",
    r"睡觉", r"签到", r"日历", r"掼蛋", r"斗地主", r"红包",
    r"对局", r"牌友", r"寻宝", r"地图", r"骰子", r"鸭蛋", r"养女儿", r"换装",
    r"白拿", r"手机", r"邀好友", r"拉新", r"内容偏好", r"官方客服", r"超容易",
]

NEVER_TAP = (
    "下载", "安装", "立即打开", "关注", "关注并退出", "分享", "邀请", "支付",
    "确认支付", "绑定", "认证", "实名", "输入密码", "提现", "下单", "购买",
)

CLAIM_TEXTS = ("开心收下", "立即领取", "领取奖励", "去领奖", "点我再领", "领取")
CLOSE_TEXTS = ("关闭弹窗", "知道了", "我知道了", "关闭", "以后再说", "暂不")
AD_CTA_RE = re.compile(r"点击.{0,4}(看|领)|看\s*\d+\s*秒|领\s*\d+\s*金币")
NUM_RE = re.compile(r"^[\d,./%＋+\-]+$")
SUB_RE = re.compile(r"最高|单日|完成|^\d+/\d+$|已|天")
TASK_STATUS_LABELS = {
    "done": "完成",
    "skipped": "跳过",
    "failed": "失败",
    "unconfirmed": "未确认",
    "deferred": "待后续",
}


def report_progress(kind: str, label: str, *, task: str | None = None,
                    tap: tuple[str, int, int] | None = None, wait: float | None = None,
                    elapsed: float | None = None, coins: int | None = None, delta: int | None = None,
                    extra: str | None = None, level: str = "info",
                    visible: bool = True, **data) -> None:
    line = progress.format_line(progress.APP_KUAISHOU, task, label, tap=tap,
                                wait=wait, elapsed=elapsed, coins=coins, delta=delta, extra=extra)
    if task is not None:
        data["task"] = task
    if tap is not None:
        text, x, y = tap
        data.update(text=text, x=x, y=y)
    if wait is not None:
        data["wait_seconds"] = round(wait, 2)
    if elapsed is not None:
        data["duration_s"] = round(elapsed, 2)
    if coins is not None:
        data["coins"] = coins
    if delta is not None:
        data.update(delta=delta, coins_delta=delta)
    run_log.message(kind, line, to=sys.stdout if visible else None, level=level, line=line, **data)


class FlowError(RuntimeError):
    """流程中断，需要回退到任务中心。"""

class DeviceInterrupted(FlowError):
    """设备或前台应用暂时不可用。"""


class UnresolvedScreen(RuntimeError):
    """本地 AI 无法确认如何恢复当前页面。"""


class ScreenNotRecognized(FlowError):
    """当前页面与执行步骤不符。"""


class OcrFailure(FlowError):
    """快手截图已取得，文字识别失败。"""


# ------------------------------------------------------------------ adb

class Adb:
    def __init__(self, serial: str, profile: HumanProfile):
        self.serial = serial
        self.profile = profile
        self.width, self.height = SCREEN_W, SCREEN_H
        self.task_title: str | None = None

    def _selector(self) -> list[str]:
        return ["-s", self.serial] if self.serial else []

    def _run(self, args: Sequence[str], timeout: int = 60) -> subprocess.CompletedProcess:
        started = time.monotonic()
        with progress.countdown(progress.APP_KUAISHOU, self.task_title, "等待 ADB 响应", timeout):
            p = subprocess.run(["adb", *self._selector(), *args],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout)
        try:
            from droidfleet import log as run_log

            run_log.adb_result(args=list(args), returncode=p.returncode,
                               ms=int((time.monotonic() - started) * 1000), stderr=p.stderr[:1000])
        except Exception:
            pass
        return p

    def ensure(self) -> None:
        for attempt in range(3):
            p = self._run(["get-state"], timeout=15)
            if p.returncode == 0 and p.stdout.strip() == "device":
                self.key("KEYCODE_WAKEUP")
                return
            if attempt == 0:
                report_progress("device.reconnect", "重新连接设备")
                self._run(["connect", self.serial], timeout=20)
            self.pause(1.0)
        raise SystemExit(f"[adb] 设备不可用: {self.serial}")

    def sh(self, command: str, timeout: int = 60) -> str:
        p = self._run(["shell", command], timeout=timeout)
        if p.returncode != 0:
            raise DeviceInterrupted(f"adb shell 失败: {command}: {p.stderr.strip()}")
        return p.stdout

    def tap(self, bounds: Bounds, text: str, *, allow_nav: bool = False) -> None:
        left, top, right, bottom = bounds
        if not allow_nav and top >= TAP_MAX_Y:
            raise FlowError(f"拒绝点击导航区控件「{text}」")
        sx, sy = self.width / SCREEN_W, self.height / SCREEN_H
        left, top, right, bottom = (
            round(left * sx, 9), round(top * sy, 9), round(right * sx, 9), round(bottom * sy, 9),
        )
        # 裁剪导航区后，仍须避开裁剪前的按钮中心。
        bottom_limit = bottom if allow_nav else min(bottom, round(TAP_MAX_Y * sy, 9))
        try:
            if not (0 <= left < right <= self.width and 0 <= top < bottom <= self.height):
                raise ValueError("按钮边界超出当前屏幕")
            center_x, center_y = (left + right) / 2, (top + bottom) / 2
            button_centers = {(int(center_x), int(center_y)), (round(center_x), round(center_y))}
            for _ in range(8):
                x, y = self.profile.tap_point((left, top, right, bottom_limit), self.width, self.height)
                if (x, y) not in button_centers:
                    break
            else:
                raise ValueError("无法在按钮内选出非中心落点")
        except ValueError as error:
            raise ScreenNotRecognized(f"「{text}」{error}") from error
        if not self.foreground().startswith(PKG + "/"):
            raise DeviceInterrupted(f"快手已离开前台，未点击「{text}」")
        self.sh(f"input tap {x} {y}")
        wait = self.profile.post_tap()
        report_progress("tap", "点击", task=self.task_title, tap=(text, x, y), wait=wait,
                        button_bounds=(left, top, right, bottom))
        progress.wait(progress.APP_KUAISHOU, self.task_title, "点击后等待页面响应", wait)

    def swipe(self, *, down: bool, distance: Tuple[float, float]) -> None:
        gesture = self.profile.swipe(self.width, self.height, down=down, distance=distance)
        if not self.foreground().startswith(PKG + "/"):
            raise DeviceInterrupted("快手已离开前台")
        self.sh("input swipe " + " ".join(map(str, gesture)))
        progress.wait(progress.APP_KUAISHOU, self.task_title, "滑动后等待页面响应", self.profile.swipe_pause())

    def pause(self, seconds: float) -> None:
        progress.wait(progress.APP_KUAISHOU, self.task_title, "等待页面更新", self.profile.delay(seconds))

    def key(self, code: str) -> None:
        time.sleep(self.profile.pre_tap(quick=True))
        self.sh(f"input keyevent {code}")
        self.pause(0.5)

    def dump_xml(self, retries: int = 2) -> Optional[str]:
        """软失败：该机型快手常驻动画会导致 "could not get idle state"，返回 None 由 OCR 兜底。"""
        for _ in range(retries):
            try:
                self.sh("uiautomator dump --compressed /sdcard/kq_ui.xml >/dev/null 2>&1")
                p = self._run(["exec-out", "cat", "/sdcard/kq_ui.xml"], timeout=40)
                body = p.stdout.lstrip()
                if p.returncode == 0 and body.startswith("<?xml") and "<hierarchy" in body:
                    return body
            except (RuntimeError, subprocess.SubprocessError):
                pass
            self.pause(0.6)
        return None

    def screenshot(self) -> bytes:
        for _ in range(3):
            timeout = 90
            with progress.countdown(progress.APP_KUAISHOU, self.task_title, "等待设备截图", timeout):
                p = subprocess.run(["adb", *self._selector(), "exec-out", "screencap", "-p"],
                                   capture_output=True, timeout=timeout)
            if p.returncode == 0 and len(p.stdout) >= 24 and p.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
                width = int.from_bytes(p.stdout[16:20], "big")
                height = int.from_bytes(p.stdout[20:24], "big")
                if width > 0 and height > 0:
                    self.width, self.height = width, height
                    return p.stdout
            self.pause(0.8)
        raise DeviceInterrupted("screencap 失败")

    def foreground(self) -> str:
        out = self.sh("dumpsys activity activities | grep -E 'topResumedActivity' | head -1")
        m = re.search(r"u0 ([\w.]+)/([\w.$]+)", out)
        return f"{m.group(1)}/{m.group(2)}" if m else out.strip()

    def start_app(self) -> None:
        self.sh(f"am start -n {MAIN_ACTIVITY}")
        self.pause(3.0)


# ------------------------------------------------------------------ OCR



@dataclass
class Line:
    text: str
    x: float
    y: float
    w: float
    h: float

    @property
    def bounds(self) -> Bounds:
        return (self.x, self.y, self.x + self.w, self.y + self.h)

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def right(self) -> float:
        return self.x + self.w

    def __str__(self) -> str:
        return f"{self.text}（{progress.pixel_position(self.x, self.y)}）"


class Ocr:
    def __init__(self):
        self.helper = Path(__file__).resolve().parents[2] / "tools" / "douyin_ocr.py"
        self.ready = self.helper.exists()
        if not self.ready:
            report_progress("ocr.error", "缺少 tools/douyin_ocr.py", level="error")
            raise SystemExit(3)

    def read(self, png: bytes, *, task: str | None = None) -> List[Line]:
        import tempfile

        handle, tmp = tempfile.mkstemp(suffix=".png")
        try:
            with os.fdopen(handle, "wb") as fh:
                fh.write(png)
            timeout = 90
            with progress.countdown(progress.APP_KUAISHOU, task, "等待页面文字识别", timeout):
                p = subprocess.run([sys.executable, str(self.helper), "tsv", tmp],
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=timeout)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if p.returncode != 0:
            raise OcrFailure(f"OCR 退出码 {p.returncode}: {p.stderr.strip()[:120]}")
        out: List[Line] = []
        for raw in p.stdout.splitlines():
            parts = raw.split("\t")
            if len(parts) != 5:
                continue
            try:
                x, y, w, h = (int(float(v)) for v in parts[:4])
            except ValueError:
                continue
            out.append(Line(parts[4].strip(), x, y, w, h))
        try:
            from droidfleet import log as run_log

            run_log.event("ocr", mode="tsv", lines=len(out), bytes=len(png))
        except Exception:
            pass
        return out


@dataclass
class Node:
    cls: str
    text: str
    desc: str
    clickable: bool
    bounds: Bounds

    @property
    def cx(self) -> float:
        return (self.bounds[0] + self.bounds[2]) / 2

    @property
    def cy(self) -> float:
        return (self.bounds[1] + self.bounds[3]) / 2

    @property
    def width(self) -> float:
        return self.bounds[2] - self.bounds[0]

    @property
    def height(self) -> float:
        return self.bounds[3] - self.bounds[1]

    @property
    def label(self) -> str:
        return (self.text or self.desc).strip()


_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def parse_nodes(xml: str) -> List[Node]:
    import xml.etree.ElementTree as ET
    nodes: List[Node] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return nodes
    for el in root.iter():
        m = _BOUNDS_RE.match(el.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = (int(v) for v in m.groups())
        nodes.append(Node(el.get("class", ""), el.get("text", ""), el.get("content-desc", ""),
                          el.get("clickable") == "true", (x1, y1, x2, y2)))
    return nodes


# ------------------------------------------------------------------ 模型

@dataclass
class TaskRow:
    title: str
    action: str
    bounds: Bounds
    sub: str = ""
    y: float = 0


@dataclass
class State:
    center: bool
    sidebar: bool
    tab: Optional[Bounds] = None
    source: str = "xml"


@dataclass
class Result:
    title: str
    status: str
    reason: str
    seconds: float = 0.0


# ------------------------------------------------------------------ 主体

class KuaishouRewards:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.rules = load_rules("kuaishou")
        self.profile = HumanProfile.new(rules=self.rules)
        self.adb = Adb(args.serial, self.profile)
        self.ocr = Ocr()
        self.results: List[Result] = []
        self.started = time.time()
        self.deadline = self.started + args.minutes * 60
        self.deny = [re.compile(p) for p in DENY_PATTERNS]
        self.deny.extend(re.compile(p) for p in self.rules.list_of("safety.deny.regex", []))
        if args.deny:
            self.deny.extend(re.compile(p) for p in args.deny.split(",") if p)
        self.allow_override = re.compile(args.allow) if args.allow else None
        self.only = re.compile(args.only) if args.only else None
        self.coin_start: Optional[int] = None
        self._last_rows: List[TaskRow] = []

    # --- 基础设施
    def log(self, msg: str, *, level: str = "info") -> None:
        report_progress("task.detail", msg, task=self.adb.task_title,
                        level=level, visible=self.args.verbose)

    def lines(self) -> List[Line]:
        png = self.adb.screenshot()
        try:
            lines = self.ocr.read(png, task=self.adb.task_title)
        except (OcrFailure, subprocess.TimeoutExpired):
            if self.adb.foreground().startswith(PKG + "/"):
                decision = diagnose_screen("快手极速版", "OCR 无法读取当前页面", png)
                if decision is not None and decision.action == "wait":
                    time.sleep(self.profile.poll_delay())
                    if self.adb.foreground().startswith(PKG + "/"):
                        lines = self.ocr.read(self.adb.screenshot(), task=self.adb.task_title)
                    else:
                        raise DeviceInterrupted("快手不在前台")
                else:
                    raise UnresolvedScreen("OCR 无法读取当前页面")
            else:
                raise DeviceInterrupted("快手不在前台")
        return self._logical_lines(lines)

    def _logical_lines(self, lines: Sequence[Line]) -> list[Line]:
        sx, sy = SCREEN_W / self.adb.width, SCREEN_H / self.adb.height
        return [Line(ln.text, ln.x * sx, ln.y * sy, ln.w * sx, ln.h * sy) for ln in lines]

    def recover(self, stage: str, *, allow_dismiss: bool = True) -> Tuple[str, List[Line]] | None:
        def read() -> tuple[bytes, str, List[Line]]:
            foreground = self.adb.foreground()
            if not foreground.startswith(PKG + "/"):
                raise DeviceInterrupted("快手不在前台")
            png = self.adb.screenshot()
            return png, foreground, self._logical_lines(self.ocr.read(png, task=self.adb.task_title))

        try:
            initial = read()

            def locate(decision: RecoveryDecision,
                       page: tuple[bytes, str, List[Line]]) -> tuple[Bounds, str] | None:
                _, foreground, lines = page
                if (not allow_dismiss or foreground != initial[1] or time.time() >= self.deadline
                        or risky_dismissal([ln.text for ln in lines])):
                    return None
                hits = [ln for ln in lines
                        if matches_dismissal(decision, ln.text, ln.cx / SCREEN_W, ln.cy / SCREEN_H)
                        and safety.check(ln.text, self.rules) is None]
                if len(hits) != 1:
                    return None
                line = hits[0]
                if line.text not in ("×", "X", "x", "✕"):
                    return line.bounds, line.text
                close_bounds = {
                    n.bounds for n in self.nodes() or []
                    if n.clickable and self._sane(n) and n.desc in self.OVERLAY_CLOSE_DESCS
                    and n.bounds[0] <= line.cx < n.bounds[2] and n.bounds[1] <= line.cy < n.bounds[3]
                    and matches_dismissal(decision, "×", n.cx / SCREEN_W, n.cy / SCREEN_H)
                }
                if len(close_bounds) == 1:
                    return close_bounds.pop(), line.text
                return None

            fresh = recover_screen(
                "快手极速版", stage, initial, screenshot=lambda page: page[0], read=read,
                locate=locate, dismiss=lambda target, _: self.adb.tap(*target),
                wait_seconds=self.profile.poll_delay(),
            )
        except (FlowError, OSError, subprocess.TimeoutExpired) as error:
            run_log.event("ai.recovery.unavailable", level="warn", stage=stage, reason=str(error))
            return None
        if fresh is None:
            return None
        return fresh[1], fresh[2]

    def read_current(self) -> Tuple[str, List[Line]]:
        def read() -> Tuple[str, List[Line]]:
            foreground = self.adb.foreground()
            if not foreground.startswith(PKG + "/"):
                raise DeviceInterrupted("快手不在前台")
            return foreground, self.lines()

        return read_after_interruption(
            self.profile, read, (DeviceInterrupted, subprocess.TimeoutExpired),
            deadline=time.monotonic() + max(0.0, self.deadline - time.time()),
        )

    @staticmethod
    def find(lines: Sequence[Line], pattern: str, y_range: Optional[Tuple[int, int]] = None,
             below: Optional[int] = None) -> Optional[Line]:
        pat = re.compile(pattern)
        for ln in lines:
            if y_range and not (y_range[0] <= ln.y <= y_range[1]):
                continue
            if below is not None and ln.y <= below:
                continue
            if pat.search(ln.text):
                return ln
        return None

    def tap_line(self, ln: Line, why: str = "") -> None:
        self.tap_target(ln.bounds, ln.text, why=why)

    def tap_target(self, bounds: Bounds, text: str, *, why: str = "", allow_nav: bool = False,
                   row: TaskRow | None = None, layout_only: bool = False) -> None:
        if any(word in text for word in NEVER_TAP):
            raise FlowError(f"拒绝点击禁止文案「{text}」{why}")
        reason = safety.check(text, self.rules)
        if reason is not None:
            run_log.event("safety.block", level="warn", text=text, reason=reason, why=why)
            raise FlowError(f"安全规则拦截「{text}」：{reason}")
        origin = self.adb.foreground()
        if not origin.startswith(PKG + "/"):
            raise DeviceInterrupted("快手已离开前台")
        deadline = time.monotonic() + max(0.0, self.deadline - time.time())

        def locate() -> Bounds | None:
            foreground = self.adb.foreground()
            if not foreground.startswith(PKG + "/"):
                raise DeviceInterrupted("快手已离开前台")
            if foreground != origin:
                raise ScreenNotRecognized("快手页面已切换")
            if row is not None:
                current = self._locate(row.title, attempts=1)
                if current is None or current.action != text or self.deny_reason(current):
                    return None
                return current.bounds
            normalized = text.strip(" ›>|")
            tolerance = self.profile.rules.num("human.touch.stable_tolerance_px")
            left, top, right, bottom = bounds
            candidates = [
                ln.bounds for ln in ([] if layout_only else self.lines())
                if ln.text.strip(" ›>|") == normalized
                and left - tolerance <= ln.cx <= right + tolerance
                and top - tolerance <= ln.cy <= bottom + tolerance
            ]
            if len(candidates) == 1 and self.profile.same_bounds(bounds, candidates[0]):
                return candidates[0]
            nodes = {
                node.bounds for node in self.nodes() or [] if node.clickable and self._sane(node)
                and normalized in (node.text.strip(" ›>|"), node.desc.strip(" ›>|"))
                and left - tolerance <= node.cx <= right + tolerance
                and top - tolerance <= node.cy <= bottom + tolerance
            }
            if len(nodes) == 1:
                return nodes.pop()
            return candidates[0] if len(candidates) == 1 else None

        time.sleep(self.profile.pre_tap())
        fresh = confirm_stable(self.profile, bounds, locate, lambda item: item, deadline=deadline,
                               matches=self.profile.same_bounds)
        if fresh is None:
            run_log.event("tap.unstable", text=text)
            raise ScreenNotRecognized(f"「{text}」位置未稳定")
        self.adb.tap(fresh, text, allow_nav=allow_nav)

    # --- 页面判定
    def fg_now(self) -> str:
        try:
            return self.adb.foreground()
        except (RuntimeError, subprocess.SubprocessError):
            return ""

    def state(self) -> State:
        """OCR 为准：该机型 XML dump 经常只拿到部分窗口/过期层级。"""
        lines = self.lines()
        has_title = any(CENTER_TITLE in ln.text for ln in lines if ln.y < 330)
        sidebar = any(ln.text in ("常用功能", "工具服务", "我的钱包") and ln.x < 560
                      and ln.y > 600 for ln in lines)
        tab_line = next((ln for ln in lines if ln.text.strip() == "去赚钱" and ln.y > 2100), None)
        tab = tab_line.bounds if tab_line else None
        if not tab:
            nodes = self.nodes()
            if nodes:
                tab_node = next((n for n in nodes if self._sane(n) and n.clickable
                                 and (n.desc or n.text) == "去赚钱" and n.cy > 2100), None)
                if tab_node:
                    tab = tab_node.bounds
        self._last_rows = self.rows_from(lines)
        return State(center=bool(has_title and not sidebar), sidebar=sidebar, tab=tab,
                     source="ocr")

    def sidebar_open(self) -> bool:
        return self.state().sidebar

    @staticmethod
    def is_action(text: str) -> bool:
        t = text.strip(" ›>|")
        return t in ACTION_LABELS

    def _nav_to_rewards(self) -> bool:
        """从可见控件定位底部「去赚钱」标签。"""
        nodes = self.nodes()
        if nodes:
            tab = next((n for n in nodes if self._sane(n) and n.clickable and (n.desc or n.text) == "去赚钱"
                        and n.cy > 2100), None)
            if tab:
                self.tap_target(tab.bounds, "去赚钱", allow_nav=True)
                self.adb.pause(2.2)
                return True
        try:
            lines = self.lines()
        except FlowError:
            lines = []
        tab_line = next((ln for ln in lines if ln.text.strip() in ("去赚钱", "去赚钱›")
                         and ln.y > 2100), None)
        if tab_line:
            self.tap_target(tab_line.bounds, "去赚钱", allow_nav=True)
            self.adb.pause(2.2)
            return True
        return False

    def ensure_center(self, tries: int = 8) -> bool:
        self._last_rows = []
        recovered = False
        for _ in range(tries):
            fg = self.adb.foreground()
            if PKG not in fg:
                self.log(f"不在快手({fg})，重启")
                self.adb.start_app()
                continue
            try:
                st = self.state()
            except FlowError as exc:
                self.log(f"状态探测失败: {exc}")
                self.adb.pause(1.0)
                continue
            if st.sidebar:
                self.adb.key("KEYCODE_BACK")
                self.adb.pause(0.8)
                continue
            if st.center:
                return True
            if self.close_overlay():
                continue
            if st.tab:
                self.tap_target(st.tab, "去赚钱", allow_nav=True)
                self.adb.pause(2.2)
            elif self._nav_to_rewards():
                continue
            elif any(k in fg for k in ("AwardVideoPlayActivity", "KwaiYodaWebViewActivity",
                                       "AdKwaiRnActivity", "PhotoDetailActivity")) or "live" in fg.lower():
                self.adb.key("KEYCODE_BACK")
                self.adb.pause(1.2)
            else:
                if recovered or self.recover("进入任务中心时遇到未识别页面") is None:
                    raise UnresolvedScreen("任务中心页面无法确认")
                recovered = True
        if self.state().center:
            return True
        if not recovered:
            fresh = self.recover("导航重试后仍无法进入任务中心")
            if fresh is not None and self.state().center:
                return True
        raise UnresolvedScreen("无法确认任务中心")

    def back_to_center(self, tries: int = 4) -> bool:
        recovered = False
        for _ in range(tries):
            try:
                if self.state().center:
                    return True
            except FlowError:
                pass
            foreground = self.adb.foreground()
            if not foreground.startswith(PKG + "/"):
                raise DeviceInterrupted("快手不在前台")
            if self._nav_to_rewards():
                continue
            if not (any(k in foreground for k in ("AwardVideoPlayActivity", "KwaiYodaWebViewActivity",
                                                   "AdKwaiRnActivity", "PhotoDetailActivity"))
                    or "live" in foreground.lower()):
                if recovered or self.recover("返回任务中心时遇到未识别页面") is None:
                    raise UnresolvedScreen("返回任务中心时页面无法确认")
                recovered = True
                continue
            self.adb.key("KEYCODE_BACK")
            self.adb.pause(1.6)
        self.adb.start_app()
        return self.ensure_center()

    def nodes(self) -> Optional[List[Node]]:
        xml = self.adb.dump_xml()
        if not xml:
            return None
        nodes = parse_nodes(xml)
        for node in nodes:
            x1, y1, x2, y2 = node.bounds
            node.bounds = (x1 / self.adb.width * SCREEN_W, y1 / self.adb.height * SCREEN_H,
                           x2 / self.adb.width * SCREEN_W, y2 / self.adb.height * SCREEN_H)
        return nodes

    @staticmethod
    def _sane(n: Node) -> bool:
        x1, y1, x2, y2 = n.bounds
        return 0 <= x1 < x2 <= SCREEN_W and 0 <= y1 < y2 <= SCREEN_H

    def parse_rows_xml(self, nodes: Sequence[Node], scroll_index: int = 0) -> List[TaskRow]:
        """行容器(宽>=950/高120..250) -> 最上方文本为标题，动作按钮范围为点击点。"""
        sane = [n for n in nodes if self._sane(n)]
        rows: List[TaskRow] = []
        for c in sane:
            if not c.clickable or c.width < 950 or not (110 <= c.height <= 260):
                continue
            if c.bounds[1] < 150 or c.bounds[3] > CONTENT_BOTTOM - 8:
                continue
            inner = [n for n in sane if n is not c
                     and c.bounds[0] <= n.bounds[0] and n.bounds[2] <= c.bounds[2]
                     and c.bounds[1] <= n.bounds[1] and n.bounds[3] <= c.bounds[3]
                     and (n.text or n.desc)]
            titles = [n for n in inner
                      if n.text and len(n.text) >= 4 and n.bounds[0] >= 120
                      and n.text not in ACTION_LABELS
                      and not NUM_RE.match(n.text) and not SUB_RE.search(n.text)]
            titles.sort(key=lambda n: (n.bounds[1], -n.width))
            acts = [n for n in inner if (n.text in ACTION_LABELS or n.desc in ACTION_LABELS)
                    and n.bounds[0] >= 600]
            if not titles or not acts:
                continue
            act = acts[0]
            sub = next((n.desc or n.text for n in inner
                        if re.search(r"最高|单日|完成|\d+/\d+|已", n.desc or n.text)), "")
            rows.append(TaskRow(title=titles[0].text.strip(), action=act.text or act.desc,
                                bounds=act.bounds, sub=sub,
                                y=titles[0].bounds[1]))
        seen, unique = set(), []
        for r in sorted(rows, key=lambda r: r.y):
            if r.title in seen:
                continue
            seen.add(r.title)
            unique.append(r)
        return unique

    def rows_from(self, lines: Sequence[Line], min_y: int = 640) -> List[TaskRow]:
        rows: List[TaskRow] = []
        for act in lines:
            if not self.is_action(act.text):
                continue
            if act.y < min_y or act.y > CONTENT_BOTTOM - 20:
                continue
            band = [ln for ln in lines if act.y - 140 <= ln.y <= act.y + 70 and ln.x < act.x + 40]
            titles = [ln for ln in band
                      if len(ln.text) >= 4 and ln.x >= 120 and ln.w >= 100
                      and not self.is_action(ln.text)
                      and not NUM_RE.match(ln.text) and not SUB_RE.search(ln.text)]
            titles.sort(key=lambda ln: (ln.y, -ln.w))
            if not titles:
                continue
            title = titles[0]
            sub = next((ln.text for ln in band
                        if ln is not title and SUB_RE.search(ln.text) and "金币" in ln.text), "")
            rows.append(TaskRow(title=title.text, action=act.text,
                                bounds=act.bounds,
                                sub=sub, y=title.y))
        seen, unique = set(), []
        for r in sorted(rows, key=lambda r: r.y):
            key = r.title
            if key in seen:
                continue
            seen.add(key)
            unique.append(r)
        return unique

    def scroll_top(self) -> None:
        previous = self.lines()
        stale = 0
        for _ in range(10):
            if any(ln.text == "我的金币" and ln.y < 420 for ln in previous):
                return
            self.adb.swipe(down=False, distance=self.profile.list_distance("search"))
            current = self.lines()
            stale = stale + 1 if current and current == previous else 0
            if stale >= 2:
                return
            previous = current

    def scroll_down(self) -> None:
        self.adb.swipe(down=True, distance=self.profile.list_distance("search"))

    def scan_all(self, max_scrolls: int = 14) -> List[TaskRow]:
        self.scroll_top()
        self.adb.pause(0.8)
        out: List[TaskRow] = []
        stale = 0
        for _ in range(max_scrolls):
            nodes = self.nodes()
            trusted = bool(nodes) and any(n.text == CENTER_TITLE for n in nodes)
            rows = self.parse_rows_xml(nodes) if trusted else []
            if not rows:
                try:
                    rows = self.rows_from(self.lines())
                except FlowError:
                    rows = []
            new = [r for r in rows if r.title not in {o.title for o in out}]
            out.extend(new)
            stale = 0 if new else stale + 1
            if stale >= 2:
                break
            self.scroll_down()
            self.adb.pause(1.0)
        self.scroll_top()
        self.adb.pause(0.6)
        return out

    def deny_reason(self, row: TaskRow) -> Optional[str]:
        if self.only and not self.only.search(row.title):
            return "--only 未匹配"
        hay = f"{row.title}|{row.action}|{row.sub}"
        if self.allow_override and self.allow_override.search(row.title):
            return None
        long_live = re.search(r"(\d+)\s*分钟", row.title)
        if long_live and int(long_live.group(1)) >= 10:
            return f"耗时过长({long_live.group(1)}分钟)"
        for pat in self.deny:
            if pat.search(hay):
                return f"黑名单 /{pat.pattern}/"
        if any(w in row.title or w in row.action for w in NEVER_TAP):
            return "禁止文案"
        return None

    # --- 领奖弹窗
    REWARD_MODAL_TITLES = ("任务完成奖励", "金币已到账", "恭喜获得", "奖励已发放")
    CONTINUE_TEXTS = ("继续赚钱", "继续看视频", "去赚钱", "再赚一笔", "返回任务")

    OVERLAY_CLOSE_DESCS = ("关闭弹窗", "关闭", "close", "close_view", "cancel")
    EXIT_LABELS = (*OVERLAY_CLOSE_DESCS, "返回", "返回上一页", "返回上一级",
                   "返回上层", "退出", "退出直播间", "navigate up", "go back")

    def close_overlay(self) -> bool:
        """XML 优先：领奖弹窗的关闭按钮是无文字的 ViewGroup(content-desc=关闭弹窗)。"""
        nodes = self.nodes()
        if nodes:
            btn = next((n for n in nodes if self._sane(n) and n.clickable and n.bounds[3] <= CONTENT_BOTTOM
                        and (n.desc in self.OVERLAY_CLOSE_DESCS or n.text in CLOSE_TEXTS)), None)
            if btn:
                self.tap_target(btn.bounds, btn.label)
                self.adb.pause(1.6)
                return True
        try:
            lines = self.lines()
        except FlowError:
            return False
        ln = next((x for x in lines if x.y < CONTENT_BOTTOM - 20
                   and x.text.strip() in CLOSE_TEXTS), None)
        if ln:
            self.tap_line(ln)
            self.adb.pause(1.5)
            return True
        return False

    def dismiss_popup(self, rounds: int = 3) -> bool:
        for _ in range(rounds):
            if not self.close_overlay():
                return True
        return True

    def claim_all(self) -> Optional[str]:
        nodes = self.nodes()
        bounds = None
        if nodes and any(n.text == CENTER_TITLE for n in nodes):
            btn = next((n for n in nodes if self._sane(n) and n.desc == "一键领取" and n.clickable), None)
            if btn:
                bounds = btn.bounds
        if bounds is None:
            try:
                ln = self.find(self.lines(), "^一键领取$", y_range=(500, TAP_MAX_Y))
            except FlowError:
                ln = None
            bounds = ln.bounds if ln else None
        if bounds is None:
            return None
        before = self.read_center_coins()
        record = earnings.start("kuaishou", "一键领取", before,
                                task_key="claim-all", serial=self.adb.serial or None)
        after = None
        status = "interrupted"
        try:
            try:
                self.tap_target(bounds, "一键领取")
                self.adb.pause(2.4)
                after = self.read_center_coins(self.read_current())
            except (DeviceInterrupted, subprocess.TimeoutExpired):
                after = self.read_center_coins(self.read_current())
            self.dismiss_popup()
            if after is None or (before is not None and after <= before):
                after = self.read_center_coins()
            status = "done" if before is not None and after is not None and after > before else "unconfirmed"
        finally:
            if after is None and status == "interrupted":
                after = self.read_center_coins()
            delta = record.finish(after, status)
        if status == "done":
            result = f"+{delta}"
            report_progress("claim_all", "确认到账", task="一键领取", coins=after, delta=delta,
                            result=result, status=status)
            return result
        result = "已点击，到账未确认"
        report_progress("claim_all", result, task="一键领取", coins=after,
                        result=result, status=status, level="warn")
        return result

    def read_center_coins(self, snapshot: Tuple[str, List[Line]] | None = None) -> Optional[int]:
        try:
            foreground, lines = self.read_current() if snapshot is None else snapshot
            if (not foreground.startswith(PKG + "/") or "live" in foreground.lower()
                    or any(activity in foreground for activity in (
                        "AwardVideoPlayActivity", "AdKwaiRnActivity", "PhotoDetailActivity",
                        "KwaiYodaWebViewActivity"))):
                return None
            if not self.find(lines, rf"^{CENTER_TITLE}$", y_range=(0, 330)):
                return None
            return self.read_balance(lines, strict_coins=True)[0]
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
            return None

    def read_balance(self, lines: List[Line] | None = None, *,
                     strict_coins: bool = False) -> Tuple[Optional[int], Optional[str]]:
        if lines is None:
            try:
                lines = self.lines()
            except FlowError:
                return None, None
        coins = None
        anchor = self.find(lines, r"我的金币", y_range=(120, 420))
        if anchor:
            pattern = (r"(?:\d{1,9}|\d{1,3}(?:[,，]\d{3}){1,2})[、。.]?"
                       if strict_coins else r"[\d,]{3,9}[^\d]{0,2}")
            cand = [ln for ln in lines if anchor.y < ln.y <= anchor.y + 130
                    and (not strict_coins or ln.cx < SCREEN_W / 2)
                    and re.fullmatch(pattern, ln.text.strip())]
            if cand and (not strict_coins or len(cand) == 1):
                coins = int(re.sub(r"\D", "", cand[0].text))
        if coins is None and not strict_coins:
            cand = [ln for ln in lines if ln.y < 420
                    and re.fullmatch(r"[\d,]{3,9}[^\d]{0,2}", ln.text.strip())
                    and 3 <= len(re.sub(r"\D", "", ln.text)) <= 8]
            coins = int(re.sub(r"\D", "", cand[0].text)) if cand else None
        cash = None
        anchor = self.find(lines, r"我的现金", y_range=(120, 420))
        if anchor:
            cand = [ln for ln in lines if anchor.y < ln.y <= anchor.y + 130
                    and re.fullmatch(r"[\d.]+[、，.]?", ln.text.strip())]
            cash = re.sub(r"[^\d.]", "", cand[0].text) if cand else None
        return coins, cash

    # --- 各任务流程
    def handle_ad_landing(self) -> str:
        lines = self.lines()
        cta = next((ln for ln in lines if AD_CTA_RE.search(ln.text) and ln.y > 1600), None)
        if cta is None:
            recovered = self.recover("广告页未识别到观看按钮")
            if recovered is not None and "AwardVideoPlayActivity" in recovered[0]:
                cta = next((ln for ln in recovered[1] if AD_CTA_RE.search(ln.text) and ln.y > 1600), None)
            if cta is None:
                raise UnresolvedScreen("广告页观看按钮无法确认")
        self.tap_target(cta.bounds, cta.text, why="广告CTA", allow_nav=True)
        self.adb.pause(3.0)

        fg = self.adb.foreground()
        lines = self.lines()
        if "Live" not in fg and any(
                ("下载" in ln.text or "立即打开" in ln.text) and ln.y > 1000 for ln in lines):
            self.log("广告页需下载 App，放弃")
            self.back_to_center()
            return "deferred:广告要求下载 App"

        timeout = 50
        deadline = time.time() + timeout
        with progress.countdown(progress.APP_KUAISHOU, self.adb.task_title, "等待广告奖励", timeout):
            while time.time() < deadline:
                lines = self.lines()
                if any("退出直播间" in ln.text for ln in lines):
                    ln = self.find(lines, "退出直播间")
                    self.tap_line(ln)
                    self.adb.pause(2.0)
                    break
                if any("已成功领取" in ln.text for ln in lines):
                    break
                if "live" in self.fg_now().lower():
                    if not self.close_overlay():
                        self.adb.key("KEYCODE_BACK")
                time.sleep(min(self.profile.poll_delay(), max(0.0, deadline - time.time())))

        reward = next((m.group(1) for ln in self.lines()
                       for m in [re.search(r"已成功领取\s*([\d,]+)\s*金币", ln.text)] if m), None)
        if reward is None:
            self.recover("广告奖励未确认", allow_dismiss=False)
        else:
            amount = reward.replace(",", "")
            if amount.isdecimal():
                progress.coin_event(self.adb.task_title or "看广告得金币", int(amount))
        self.dismiss_popup()
        self.back_to_center()
        return f"done:+{reward}金币" if reward else "unconfirmed:广告奖励未确认"

    def handle_ad_feed(self, dwell: float = 16.0, max_ads: int = 5) -> str:
        watched = 0
        for _ in range(max_ads):
            if time.time() > self.deadline:
                break
            remaining = self.deadline - time.time()
            if remaining < dwell:
                break
            wait = min(dwell + self.profile.poll_delay(), remaining)
            progress.wait(progress.APP_KUAISHOU, self.adb.task_title, "等待当前广告播放", wait)
            if time.time() >= self.deadline:
                break
            badge = ""
            try:
                badge = next((ln.text for ln in self.lines() if re.fullmatch(r"\d+/\d+", ln.text)), "")
            except FlowError:
                pass
            self.adb.swipe(down=True, distance=(0.46, 0.59))
            self.adb.pause(2.0)
            watched += 1
            report_progress("task.detail", f"已浏览第 {watched} 条广告", task=self.adb.task_title,
                            wait=wait, extra=badge or None)
        self.back_to_center()
        return f"done:浏览{watched}条"

    def handle_h5(self) -> str:
        self.adb.pause(2.6)
        lines = self.lines()
        for text in CLAIM_TEXTS:
            ln = self.find(lines, f"^{text}$", y_range=(200, TAP_MAX_Y))
            if ln:
                return self.claim_on_page(ln, lines)
        recovered = self.recover("H5 页面未识别到领奖按钮")
        if recovered is not None and "KwaiYodaWebViewActivity" in recovered[0]:
            for text in CLAIM_TEXTS:
                ln = self.find(recovered[1], f"^{text}$", y_range=(200, TAP_MAX_Y))
                if ln:
                    return self.claim_on_page(ln, recovered[1])
        raise UnresolvedScreen("H5 页面领奖入口无法确认")

    def handle_rn_ad(self) -> str:
        self.adb.pause(2.2)
        lines = self.lines()
        ln = self.find(lines, "直接领金币", y_range=(200, TAP_MAX_Y))
        if ln:
            return self.claim_on_page(ln, lines)
        if not any("下载" in line.text or "立即打开" in line.text for line in lines):
            recovered = self.recover("推广页缺少直接领奖入口")
            if recovered is not None and "AdKwaiRnActivity" in recovered[0]:
                ln = self.find(recovered[1], "^直接领金币$", y_range=(200, TAP_MAX_Y))
                if ln:
                    return self.claim_on_page(ln, recovered[1])
            raise UnresolvedScreen("推广页领奖入口无法确认")
        self.back_to_center()
        return "deferred:需下载 App"

    def claim_on_page(self, line: Line, before: List[Line]) -> str:
        def receipt(lines: List[Line]) -> set[str]:
            return {ln.text for ln in lines if "已成功领取" in ln.text
                    or any(label in ln.text for label in self.REWARD_MODAL_TITLES)}

        existing = receipt(before)
        self.tap_line(line)
        _, outcome = observe_tap(
            self.profile, self.lines, lambda lines: "success" if receipt(lines) - existing else "unknown",
        )
        if outcome != "success":
            recovered = self.recover("点击领取后没有确认奖励回执", allow_dismiss=False)
            if recovered is not None and receipt(recovered[1]) - existing:
                outcome = "success"
        self.dismiss_popup()
        self.back_to_center()
        if outcome == "success":
            return f"done:{line.text}"
        return f"unconfirmed:{line.text}已点击，到账未确认"

    def pause_after_task(self, stage: str) -> None:
        """任务完成后先随机停留几秒，再按返回/退出（模拟注意力分散）。"""
        seconds = self.profile.after_task_delay()
        if seconds <= 0:
            return
        report_progress("human.after_task", f"{stage}结束后停留", task=self.adb.task_title,
                        wait=seconds, stage=stage, seconds=round(seconds, 2))
        progress.wait(progress.APP_KUAISHOU, self.adb.task_title, f"{stage}结束后停留", seconds)

    def exit_after_task(self, stage: str) -> None:
        """使用 OCR 或布局确认退出按钮，无法定位时按系统返回键。"""
        from droidfleet import exits

        try:
            lines = self.lines()
        except FlowError:
            lines = []
        line = exits.find_exit(lines)
        if line is not None:
            try:
                self.tap_line(line, stage)
                return
            except ScreenNotRecognized:
                pass

        controls = {(node.bounds, node.label) for node in self.nodes() or []
                    if self._sane(node) and node.clickable and node.bounds[1] < TAP_MAX_Y
                    and node.label.casefold() in self.EXIT_LABELS}
        if len(controls) > 1 and exits.ai_enabled(self.profile.rules):
            try:
                png = self.adb.screenshot()
            except (RuntimeError, subprocess.SubprocessError):
                png = b""
            decision = locate_exit("kuaishou", stage, png, timeout=25)
            if decision is not None and decision.action == "tap":
                x, y = decision.x / 1000 * SCREEN_W, decision.y / 1000 * SCREEN_H
                controls = {(bounds, label) for bounds, label in controls
                            if bounds[0] < x < bounds[2] and bounds[1] < y < bounds[3]}
        if len(controls) == 1:
            bounds, label = controls.pop()
            try:
                self.tap_target(bounds, label, why=stage, layout_only=True)
                return
            except ScreenNotRecognized:
                pass
        self.log(f"没找到退出控件，按返回键离开{stage}", level="warn")
        self.adb.key("KEYCODE_BACK")

    def handle_live(self, seconds: int = 95) -> str:
        required = time.time() + seconds
        end = min(required + self.profile.poll_delay(), self.deadline)
        wait = max(0.0, end - time.time())
        report_progress("task.detail", "直播停留", task=self.adb.task_title, wait=wait)
        progress.wait(progress.APP_KUAISHOU, self.adb.task_title, "直播停留", wait)
        if "live" in self.fg_now().lower():
            self.pause_after_task("直播观看")
            if not self.close_overlay():
                self.exit_after_task("直播退出")
        self.adb.pause(2.2)
        try:
            ln = self.find(self.lines(), "退出直播间")
        except FlowError:
            ln = None
        if ln:
            self.tap_line(ln)
            self.adb.pause(1.8)
        self.back_to_center()
        return f"done:停留至少{seconds}s" if end >= required else "deferred:直播停留时间不足"

    def handle_generic(self) -> str:
        self.adb.pause(2.2)
        try:
            lines = self.lines()
        except FlowError:
            lines = []
        for text in CLAIM_TEXTS:
            ln = next((x for x in lines if text == x.text and x.y < TAP_MAX_Y), None)
            if ln:
                return self.claim_on_page(ln, lines)
        if self.find(lines, r"^(搜索|取消)$", y_range=(0, 220)):
            self.adb.key("KEYCODE_BACK")
            self.back_to_center()
            return "done:已确认搜索页"
        fresh = self.recover("任务落地页没有可确认的处理流程")
        if fresh is not None:
            for text in CLAIM_TEXTS:
                ln = self.find(fresh[1], f"^{text}$", y_range=(200, TAP_MAX_Y))
                if ln:
                    return self.claim_on_page(ln, fresh[1])
            if self.find(fresh[1], f"^{CENTER_TITLE}$", y_range=(0, 330)):
                return "unconfirmed:已恢复到任务中心，奖励未确认"
        raise UnresolvedScreen("任务落地页无法确认")

    def dispatch(self, fg: str) -> str:
        self.log(f"落地页 {fg}")
        if "AwardVideoPlayActivity" in fg:
            return self.handle_ad_landing()
        if "AdKwaiRnActivity" in fg:
            return self.handle_rn_ad()
        if "KwaiYodaWebViewActivity" in fg:
            return self.handle_h5()
        if "PhotoDetailActivity" in fg:
            return self.handle_ad_feed()
        if "live" in fg.lower():
            return self.handle_live()
        return self.handle_generic()

    # --- 单行执行
    def enter_row(self, target: TaskRow) -> Optional[str]:
        origin = self.adb.foreground()
        before = self.lines()
        start_center = any(ln.text == CENTER_TITLE and ln.y < 330 for ln in before)
        if not start_center or not origin.startswith(PKG + "/"):
            return None
        if any(any(label in ln.text for label in self.REWARD_MODAL_TITLES) for ln in before):
            return None
        latest = next((row for row in self.rows_from(before) if row.title == target.title), None)
        if latest is not None:
            if latest.action != target.action or self.deny_reason(latest):
                return None
            target = latest

        def capture() -> Tuple[str, List[Line]]:
            foreground = self.adb.foreground()
            if not foreground.startswith(PKG + "/"):
                raise DeviceInterrupted("快手不在前台")
            return foreground, self.lines()

        def classify(state: Tuple[str, List[Line]]) -> Observation:
            foreground, lines = state
            if not foreground.startswith(PKG + "/"):
                return "unknown"
            center = any(ln.text == CENTER_TITLE and ln.y < 330 for ln in lines)
            if foreground == origin and lines == before:
                return "unchanged"
            landing = any(kind in foreground for kind in (
                "AwardVideoPlayActivity", "AdKwaiRnActivity", "KwaiYodaWebViewActivity",
                "PhotoDetailActivity",
            )) or "live" in foreground.lower()
            reward = any(any(label in ln.text for label in self.REWARD_MODAL_TITLES) for ln in lines)
            search = target.action == "去搜索" and not center and any(
                ln.text in {"搜索", "取消"} and ln.y < 220 for ln in lines
            )
            return "success" if reward or search or (foreground != origin and not center and landing) else "unknown"

        deadline = time.monotonic() + max(0.0, self.deadline - time.time())
        if time.monotonic() >= deadline:
            return None
        self.tap_target(target.bounds, target.action, row=target)
        state, outcome = observe_tap(self.profile, capture, classify, deadline=deadline)
        if (outcome == "unchanged" and target.action in {"去观看", "去浏览"}
                and time.monotonic() < deadline):
            rows = self.rows_from(state[1])
            fresh = next((row for row in rows if row.title == target.title), None)
            if fresh is None:
                fresh = self._locate(target.title, attempts=1)
            if (fresh and fresh.action == target.action and not self.deny_reason(fresh)
                    and time.monotonic() < deadline):
                run_log.event("tap.retry", text=fresh.action, task=fresh.title, reason="task_center_unchanged")
                self.tap_target(fresh.bounds, fresh.action, row=fresh)
                state, outcome = observe_tap(self.profile, capture, classify, deadline=deadline)
        run_log.event("tap.result", text=target.action, task=target.title, outcome=outcome)
        if outcome == "unknown":
            fresh = self.recover(f"「{target.title}」点击后未识别落地页")
            if fresh is None or classify(fresh) != "success":
                raise UnresolvedScreen("点击后页面无法确认，已暂停该任务")
            return fresh[0]
        if outcome == "unchanged":
            self.recover(f"「{target.title}」点击后页面仍未变化", allow_dismiss=False)
        return state[0] if outcome == "success" else None

    def run_row(self, row: TaskRow) -> Result:
        t0 = time.time()
        record = None
        status = "interrupted"
        try:
            self.scroll_top()
            target = self._locate(row.title)
            if not target:
                recovered = self.recover(f"滚动后未找到「{row.title}」任务行")
                if recovered is not None:
                    target = self._locate(row.title, attempts=1)
                if target is None:
                    raise UnresolvedScreen("滚动后仍无法确认任务行")
            reason = self.deny_reason(target)
            if reason:
                return Result(row.title, "skipped", reason, time.time() - t0)
            record = earnings.start("kuaishou", row.title, self.read_center_coins(),
                                    task_key=row.title, serial=self.adb.serial or None)
            foreground = self.enter_row(target)
            if foreground is None:
                status = "unconfirmed"
                self._safe_back()
                return Result(row.title, status, "点击结果未确认，本次运行暂缓该任务", time.time() - t0)
            outcome = self.dispatch(foreground)
            status, _, reason = outcome.partition(":")
            status = status or "done"
            return Result(row.title, status, reason, time.time() - t0)
        except UnresolvedScreen:
            status = "unconfirmed"
            raise
        except (DeviceInterrupted, subprocess.TimeoutExpired) as exc:
            status = "unconfirmed"
            try:
                snapshot = self.read_current()
                coins = self.read_center_coins(snapshot)
                _, lines = snapshot
                visible = next((item for item in self.rows_from(lines) if item.title == row.title), None)
                run_log.event("recovery.task", task=row.title, coins=coins,
                              progress=visible.sub if visible else None)
            except (FlowError, subprocess.TimeoutExpired):
                return Result(row.title, status, f"设备中断，进度未确认：{exc}", time.time() - t0)
            self._safe_back()
            return Result(row.title, status, "已重读进度，本次运行暂缓该任务", time.time() - t0)
        except ScreenNotRecognized as exc:
            status = "unconfirmed"
            recovered = self.recover(str(exc))
            if recovered is None:
                raise UnresolvedScreen(str(exc)) from exc
            return Result(row.title, status, f"已重读页面：{exc}", time.time() - t0)
        except FlowError as exc:
            status = "unconfirmed" if record is not None else "failed"
            self._safe_back()
            return Result(row.title, status, str(exc), time.time() - t0)
        except (RuntimeError, OSError) as exc:
            status = "unconfirmed" if record is not None else "failed"
            self._safe_back()
            return Result(row.title, status, f"异常: {exc}", time.time() - t0)
        finally:
            if record is not None:
                record.finish(self.read_center_coins(), status)

    def _locate(self, title: str, attempts: int = 6) -> Optional[TaskRow]:
        previous: list[tuple[str, float]] = []
        previous_side: bool | None = None
        stale = 0
        for attempt in range(attempts):
            nodes = self.nodes()
            trusted = bool(nodes) and any(n.text == CENTER_TITLE for n in nodes)
            rows = self.parse_rows_xml(nodes) if trusted else []
            if not rows:
                try:
                    rows = self.rows_from(self.lines())
                except FlowError:
                    rows = []
            hit = next((r for r in rows if r.title == title), None)
            if hit and (attempts == 1 or 520 <= hit.bounds[1] and hit.bounds[3] <= 1770):
                return hit
            snapshot = [(row.title, row.y) for row in rows]
            stale = stale + 1 if snapshot and snapshot == previous else 0
            if stale >= 2 or attempt + 1 == attempts:
                return hit
            previous = snapshot
            down = hit is None or hit.bounds[3] > 1770
            if hit is None:
                purpose = "search"
            else:
                purpose = "correction" if previous_side is not None and down != previous_side else "near"
            previous_side = down if hit is not None else None
            self.adb.swipe(down=down, distance=self.profile.list_distance(purpose))
        return None

    def _safe_back(self) -> None:
        try:
            self.back_to_center()
        except UnresolvedScreen:
            raise
        except (RuntimeError, subprocess.TimeoutExpired):
            pass

    # --- 主循环
    def run(self) -> int:
        self.adb.ensure()
        self.adb.screenshot()
        if not self.ensure_center():
            report_progress("task.error", "无法进入「去赚钱」任务中心", level="error")
            return 2
        self.coin_start, cash = self.read_balance()
        mode = "开始扫描" if self.args.dry_run else "开始运行"
        details = (f"设备 {self.adb.serial or '默认设备'} | 现金 {cash if cash is not None else '未读到'}"
                   f" | 预算 {self.args.minutes:g} 分钟 | 最多 {self.args.rounds} 轮")
        if self.coin_start is None:
            details += " | 金币未读到"
        report_progress("start", mode, task="任务中心", coins=self.coin_start, extra=details,
                        cash=cash, serial=self.adb.serial, minutes=self.args.minutes,
                        rounds=self.args.rounds, dry_run=self.args.dry_run)

        skipped_left: List[TaskRow] = []
        unconfirmed: set[str] = set()
        claim_unconfirmed = False
        for rnd in range(1, self.args.rounds + 1):
            if time.time() > self.deadline:
                break
            report_progress("round.start", f"开始第 {rnd} 轮", task="任务中心", round=rnd)
            if not self.args.dry_run and not claim_unconfirmed:
                got = self.claim_all()
                if got:
                    claim_unconfirmed = not got.startswith("+")
            rows = self.scan_all()
            plan = [(r, self.deny_reason(r)) for r in rows]
            eligible = sum(1 for _, why in plan if not why)
            report_progress("round.scan", f"扫描到 {len(rows)} 项，可执行 {eligible} 项",
                            task="任务中心", round=rnd, total=len(rows), eligible=eligible)
            if self.args.dry_run:
                for r, why in plan:
                    label = "跳过" if why else "计划"
                    details = " | ".join(part for part in (r.action, why, r.sub) if part)
                    report_progress("task.plan", label, task=r.title, extra=details,
                                    action=r.action, progress=r.sub, reason=why, round=rnd)
                continue
            failures = 0
            for r, why in plan:
                if r.title in unconfirmed:
                    continue
                if why:
                    self.results.append(Result(r.title, "skipped", why))
                    continue
                if time.time() > self.deadline:
                    skipped_left.append(r)
                    self.results.append(Result(r.title, "deferred", "时间预算用尽"))
                    continue
                self.adb.task_title = r.title
                try:
                    res = self.run_row(r)
                finally:
                    self.adb.task_title = None
                self.results.append(res)
                status = TASK_STATUS_LABELS.get(res.status, res.status)
                level = "warn" if res.status in ("failed", "unconfirmed") else "info"
                report_progress("task", status, task=r.title, elapsed=res.seconds,
                                extra=res.reason, level=level,
                                title=r.title, action=r.action, status=res.status,
                                reason=res.reason, seconds=round(res.seconds, 1))
                if res.status == "unconfirmed":
                    unconfirmed.add(r.title)
                if res.status in ("failed", "unconfirmed"):
                    failures += 1
                    if failures >= 3:
                        report_progress("round.abort", "连续 3 个任务失败或未确认，结束本轮",
                                        task="任务中心", round=rnd, level="warn")
                        break
                else:
                    failures = 0
        if not self.args.dry_run:
            self.back_to_center()
        coin_end, cash_end = self.read_balance()
        self.summary(coin_end, cash_end, skipped_left)
        return 0

    def summary(self, coin_end: Optional[int], cash_end: Optional[str],
                pending: Sequence[TaskRow]) -> None:
        done = [r for r in self.results if r.status == "done"]
        skipped = [r for r in self.results if r.status == "skipped"]
        failed = [r for r in self.results if r.status in ("failed", "unconfirmed")]
        deferred = [r for r in self.results if r.status == "deferred"]
        delta = (coin_end - self.coin_start) if (coin_end is not None and self.coin_start is not None) else None
        start = self.coin_start if self.coin_start is not None else "未读到"
        cash = cash_end if cash_end is not None else "未读到"
        details = f"初始金币 {start} | 现金 {cash}"
        if coin_end is None:
            details += " | 结束金币未读到"
        counts = f"完成 {len(done)} / 跳过 {len(skipped)} / 失败 {len(failed)} / 待后续 {len(deferred)}"
        report_progress("summary", counts, task="运行汇总", coins=coin_end, delta=delta,
                        extra=details, done=len(done), skipped=len(skipped), failed=len(failed),
                        deferred=len(deferred), cash=cash_end, coins_start=self.coin_start,
                        coins_end=coin_end, coins_delta=delta)
        for r in skipped[:20]:
            report_progress("summary.detail", "跳过", task=r.title, extra=r.reason,
                            status=r.status, reason=r.reason)
        if pending:
            report_progress("summary.detail", f"因预算未执行 {len(pending)} 项，可增大 --minutes 继续",
                            task="运行汇总", pending=len(pending))
        path = run_log.current().path
        if path is not None:
            report_progress("summary.detail", "日志文件", task="运行汇总", extra=str(path))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="快手极速版「去赚钱」任务自动化（OCR 驱动）")
    p.add_argument("--serial", default=DEFAULT_SERIAL, help="adb 设备序列号")
    p.add_argument("--minutes", type=float, default=15.0, help="总时间预算（分钟）")
    p.add_argument("--rounds", type=int, default=3, help="最多轮数")
    p.add_argument("--dry-run", action="store_true", help="导航并扫描任务中心，跳过任务与领奖")
    p.add_argument("--only", default="", help="只执行标题匹配该正则的任务")
    p.add_argument("--allow", default="", help="额外允许的标题正则（优先于黑名单）")
    p.add_argument("--deny", default="", help="追加黑名单正则，逗号分隔")
    p.add_argument("--verbose", action="store_true", help="输出调试日志")
    return p


def _run_cli(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    app = KuaishouRewards(args)
    try:
        return app.run()
    except KeyboardInterrupt:
        report_progress("run.interrupt", "收到停止请求，正在汇总", task="任务中心", level="warn")
        coins, cash = app.read_balance()
        app.summary(coins, cash, [])
        return 130
    except SystemExit:
        raise
    except ScreenNotRecognized as exc:
        app.recover(str(exc), allow_dismiss=False)
        report_progress("task.error", str(exc), level="error")
        return 2
    except (FlowError, RuntimeError) as exc:
        report_progress("task.error", str(exc), level="error")
        return 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    from droidfleet import log as run_log

    logger = run_log.init_run("kuaishou", list(argv) if argv is not None else sys.argv[1:])
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
    sys.exit(main())
