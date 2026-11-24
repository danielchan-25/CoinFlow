#!/usr/bin/env python3
"""CoinFlow 控制台：内嵌手机画面 + 一键运行各 App 脚本。

- 启动后自动找当前在线的 adb 设备，并把画面嵌进窗口（优先 scrcpy，失败退回截图轮询）。
- 右侧按 App 选择模式后点「运行」，脚本输出实时显示在下方日志区。
- 不改变任何现有规则；只是把 soda / douyin / kuaishou 的入口收拢到一个界面。
"""

from __future__ import annotations

import ctypes
import io
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk
from droidfleet.rules import RuleError, load_rules

APP_NAME = "CoinFlow"
APP_TITLE = f"{APP_NAME} 控制台"
CREATE_NO_WINDOW = 0x08000000

GWL_STYLE = -16
WS_CHILD = 0x40000000
WS_POPUP = 0x80000000
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_VISIBLE = 0x10000000
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_ASYNCWINDOWPOS = 0x4000
EMBEDDED_RESIZE_INTERVAL_MS = 150

user32 = ctypes.windll.user32
user32.GetClientRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
user32.ClientToScreen.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.POINT))
user32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT)
user32.SetParent.argtypes = (wintypes.HWND, wintypes.HWND)
user32.SetParent.restype = wintypes.HWND
user32.GetParent.argtypes = (wintypes.HWND,)
user32.GetParent.restype = wintypes.HWND


# --------------------------------------------------------------------------- 环境

def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def first_existing(*paths: str) -> str | None:
    for item in paths:
        found = shutil.which(item)
        if found:
            return found
        if Path(item).is_file():
            return item
    return None


def find_adb() -> str | None:
    local = os.environ.get("LOCALAPPDATA", "")
    return first_existing("adb", str(Path(local) / "Android" / "platform-tools" / "adb.exe"))


def find_scrcpy() -> str | None:
    local = os.environ.get("LOCALAPPDATA", "")
    return first_existing("scrcpy", str(Path(local) / "scrcpy" / "scrcpy.exe"))


def find_python() -> str:
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.environ.get("DROIDFLEET_PYTHON", ""),
        str(Path(local) / "Programs" / "Python" / "Python311" / "python.exe"),
        "python",
    ]
    for item in candidates:
        if item and (shutil.which(item) or Path(item).is_file()):
            return item
    return "python"


ADB = find_adb()
SCRCPY = find_scrcpy()
PYTHON = find_python()
BASE = app_dir()


def child_env() -> dict:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    parts = []
    if ADB:
        parts.append(str(Path(ADB).parent))
    if SCRCPY:
        parts.append(str(Path(SCRCPY).parent))
    parts.append(env.get("PATH", ""))
    env["PATH"] = ";".join(p for p in parts if p)
    return env


def log_directory() -> Path:
    override = os.environ.get("DROIDFLEET_LOG_FILE")
    if override:
        return (BASE / Path(override)).parent
    rules_path = Path(os.environ.get("DROIDFLEET_RULES_DIR") or "rules").expanduser()
    try:
        directory = load_rules(root=BASE / rules_path).get("logging.dir", "logs")
    except (OSError, RuleError, ValueError):
        directory = "logs"
    return BASE / str(directory)


def adb_run(args, serial: str | None = None, timeout: float = 15) -> subprocess.CompletedProcess:
    cmd = [ADB] if ADB else ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += args
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, env=child_env(),
                          creationflags=CREATE_NO_WINDOW)


def device_report() -> list[tuple[str, str]] | None:
    """读取设备列表，ADB 查询失败时返回 None。"""
    try:
        result = adb_run(["devices"], timeout=6)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    items = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            items.append((parts[0], parts[1]))
    return items


# --------------------------------------------------------------------------- 窗口内嵌

def find_window(title: str, timeout: float = 8.0) -> int | None:
    end = time.time() + timeout
    while time.time() < end:
        found: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def callback(hwnd, _param):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if buf.value == title:
                    found.append(hwnd)
                    return False
            return True

        user32.EnumWindows(callback, 0)
        if found:
            return found[0]
        time.sleep(0.15)
    return None


class ScrcpyMirror:
    """把 scrcpy 的窗口挂到 Tk 的 Frame 里。"""

    def __init__(self, app: "App", host_hwnd: int, serial: str, on_fail):
        self.app = app
        self.host_hwnd = host_hwnd      # 主线程里取的 HWND，工作线程不再碰 Tk
        self.serial = serial
        self.on_fail = on_fail
        self.proc: subprocess.Popen | None = None
        self.hwnd: int | None = None
        self.resize_job: str | None = None
        self.title = f"{APP_NAME}Embed-{os.getpid()}-{int(time.time() * 1000) % 100000}"
        self.dead = False

    def start(self):
        if not SCRCPY:
            self.on_fail("未找到 scrcpy")
            return
        cmd = [
            SCRCPY, "-s", self.serial,
            "--no-audio", "--stay-awake", "--window-borderless",
            # 默认的 direct3d11 渲染器在跨进程 SetParent 后会黑屏/退出，opengl 正常
            "--render-driver=opengl",
            "--no-window-aspect-ratio-lock",
            "--window-title", self.title,
            "--window-x", "-3000", "--window-y", "0",
        ]
        log_dir = BASE / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / "gui-scrcpy.log"
        try:
            self.log_file = open(self.log_path, "ab")
            self.proc = subprocess.Popen(cmd, stdout=self.log_file,
                                         stderr=subprocess.STDOUT,
                                         creationflags=CREATE_NO_WINDOW, env=child_env())
        except Exception as error:  # pragma: no cover - 取决于本机环境
            self.on_fail(f"scrcpy 启动失败: {error}")
            return
        threading.Thread(target=self._attach, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self):
        proc = self.proc
        if not proc:
            return
        code = proc.wait()
        if not self.dead:
            self.on_fail(f"scrcpy 已退出（code {code}）")

    def _attach(self):
        hwnd = find_window(self.title, timeout=20)
        if self.dead:
            self._kill_proc()
            return
        if not hwnd:
            self.on_fail("等不到 scrcpy 窗口")
            return
        style = user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
        style = (style & ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME)) | WS_CHILD | WS_VISIBLE
        if style & 0x80000000:  # SetWindowLongW 需要 32 位有符号值
            style -= 0x100000000
        user32.SetWindowLongW(hwnd, GWL_STYLE, ctypes.c_long(style))
        user32.SetParent(hwnd, self.host_hwnd)
        if user32.GetParent(hwnd) != self.host_hwnd:
            self.on_fail("无法将 scrcpy 窗口嵌入控制台")
            return
        self.app.root.after(0, lambda: self._after_attach(hwnd))

    def _after_attach(self, hwnd: int):
        """沿主线程走：重新贴合尺寸并更新状态。"""
        if self.dead or self.app.mirror is not self:
            user32.SetParent(hwnd, 0)
            self._kill_proc()
            return
        self.hwnd = hwnd
        self.app._embed_fails = 0
        self.resize(frame_changed=True)
        # 手机旋转会改变 scrcpy 子窗口，Tk 宿主不一定收到尺寸事件。
        self.resize_job = self.app.root.after(EMBEDDED_RESIZE_INTERVAL_MS, self._sync_bounds)
        self.app.set_status(f"内嵌投屏（scrcpy {self.serial}）")

    def _sync_bounds(self):
        self.resize_job = None
        if self.dead or self.app.mirror is not self or not self.hwnd:
            return
        self.resize()
        self.resize_job = self.app.root.after(EMBEDDED_RESIZE_INTERVAL_MS, self._sync_bounds)

    def resize(self, *, frame_changed: bool = False):
        hwnd = self.hwnd
        if not hwnd or self.dead or self.app.mirror is not self:
            return
        host, current = wintypes.RECT(), wintypes.RECT()
        origin = wintypes.POINT(0, 0)
        if (user32.GetParent(hwnd) != self.host_hwnd
                or not user32.GetClientRect(self.host_hwnd, ctypes.byref(host))
                or not user32.ClientToScreen(self.host_hwnd, ctypes.byref(origin))
                or not user32.GetWindowRect(hwnd, ctypes.byref(current))):
            return
        width, height = host.right - host.left, host.bottom - host.top
        if width <= 0 or height <= 0:
            return
        if not frame_changed and (current.left, current.top, current.right, current.bottom) == (
                origin.x, origin.y, origin.x + width, origin.y + height):
            return
        flags = SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS
        if frame_changed:
            flags |= SWP_FRAMECHANGED
        user32.SetWindowPos(hwnd, None, 0, 0, width, height, flags)

    def _kill_proc(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()
        self.proc = None
        log_file = getattr(self, "log_file", None)
        if log_file:
            try:
                log_file.close()
            except Exception:
                pass
            self.log_file = None

    def stop(self):
        self.dead = True
        if self.resize_job is not None:
            self.app.root.after_cancel(self.resize_job)
            self.resize_job = None
        if self.hwnd:
            # 先把窗口从 Tk 里摘出来，避免切换设备时残留黑块
            try:
                user32.SetParent(self.hwnd, 0)
                user32.ShowWindow(self.hwnd, 0)
            except Exception:
                pass
        self.hwnd = None
        self._kill_proc()


class PollMirror:
    """兜底：定时截图显示，点画面即点手机。"""

    def __init__(self, app: "App", canvas: tk.Canvas, serial: str, on_fail):
        self.app = app
        self.canvas = canvas
        self.serial = serial
        self.on_fail = on_fail
        self.stop_event = threading.Event()
        self.queue: queue.Queue = queue.Queue(maxsize=1)
        self.photo = None
        self.frame = None
        self.latest_layout: tuple[tuple[int, int], int] | None = None
        self.displayed = None
        self.last_frame = time.monotonic()
        self.item = None
        self.paint_job = None
        self.tap_binding = self.canvas.bind("<Button-1>", self._tap)
        self.resize_binding = self.canvas.bind("<Configure>", self._redraw)

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        self.paint_job = self.canvas.after(60, self._paint)

    def _loop(self):
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                proc = subprocess.run(
                    ([ADB] if ADB else ["adb"]) + ["-s", self.serial, "exec-out", "screencap", "-p"],
                    capture_output=True, timeout=20, env=child_env(),
                    creationflags=CREATE_NO_WINDOW,
                )
                data = proc.stdout
            except Exception:
                time.sleep(0.5)
                continue
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                try:
                    image = Image.open(io.BytesIO(data))
                    image.load()
                    layout = self.latest_layout
                    if layout is None or layout[0] != image.size:
                        # 仅在截图尺寸改变时更新，按到旧方向的点击会被忽略。
                        layout = (image.size, 0 if layout is None else layout[1] + 1)
                        self.latest_layout = layout
                    try:
                        self.queue.get_nowait()
                    except queue.Empty:
                        pass
                    self.queue.put((image, layout))
                    self.last_frame = time.monotonic()
                except Exception:
                    pass
            self.stop_event.wait(max(0.05, 0.3 - (time.monotonic() - started)))

    def _paint(self):
        self.paint_job = None
        if self.stop_event.is_set():
            return
        try:
            self.frame = self.queue.get_nowait()
        except queue.Empty:
            pass
        else:
            self._redraw()
        self.paint_job = self.canvas.after(60, self._paint)

    def _redraw(self, _event=None):
        if self.stop_event.is_set() or self.frame is None:
            return
        image, layout = self.frame
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if cw < 2 or ch < 2:
            return
        try:
            ratio = min(cw / image.width, ch / image.height)
            width = max(1, min(cw, round(image.width * ratio)))
            height = max(1, min(ch, round(image.height * ratio)))
            left, top = (cw - width) // 2, (ch - height) // 2
            photo = ImageTk.PhotoImage(image.resize((width, height), Image.BILINEAR))
            if self.item is None:
                self.item = self.canvas.create_image(left, top, image=photo, anchor="nw")
            else:
                self.canvas.itemconfigure(self.item, image=photo)
                self.canvas.coords(self.item, left, top)
            self.photo = photo
            self.displayed = (layout, (left, top, width, height))
        except Exception:
            pass

    def _tap(self, event):
        if self.stop_event.is_set() or self.displayed is None:
            return
        layout, (left, top, width, height) = self.displayed
        if layout != self.latest_layout:
            return
        if not (left <= event.x < left + width and top <= event.y < top + height):
            return
        (device_width, device_height), _ = layout
        x = (event.x - left) * device_width // width
        y = (event.y - top) * device_height // height
        threading.Thread(target=self._do_tap, args=(x, y, layout), daemon=True).start()

    def _do_tap(self, x: int, y: int, layout: tuple[tuple[int, int], int]):
        if self.stop_event.is_set() or layout != self.latest_layout:
            return
        try:
            adb_run(["shell", "input", "tap", str(x), str(y)], self.serial, timeout=8)
        except Exception:
            pass

    def stop(self):
        self.stop_event.set()
        if self.paint_job is not None:
            self.canvas.after_cancel(self.paint_job)
            self.paint_job = None
        if self.tap_binding is not None:
            self.canvas.unbind("<Button-1>", self.tap_binding)
            self.tap_binding = None
        if self.resize_binding is not None:
            self.canvas.unbind("<Configure>", self.resize_binding)
            self.resize_binding = None
        if self.item is not None:
            self.canvas.delete(self.item)
            self.item = None
        self.photo = None
        self.frame = None
        self.displayed = None


# --------------------------------------------------------------------------- 脚本清单

# 汽水音乐福利页的单项任务（按钮顺序即执行/展示顺序）
SODA_TASKS = [
    ("check-in", "签到得金币"),
    ("listen", "听歌赚金币"),
    ("ad", "看广告赚金币"),
    ("video", "连续刷视频赚金币"),
    ("watch", "看视频赚金币"),
    ("flip-card", "翻卡赢金币"),
    ("chest", "开宝箱得金币"),
]
SODA_SKIPPED = ("查额成功领大奖", "逛街赚金币", "红包雨")

DOUYIN_TASKS = [
    ("chest", "开宝箱得金币", "可重复"),
    ("video", "连续看视频30天赚30元", "可重复"),
    ("reserve", "预约领金币", "每日一次"),
    ("ad", "看广告赚金币", "可重复"),
    ("wallet", "天天领金币", "可重复"),
    ("target-video", "连续刷指定视频赚金币", "可重复"),
    ("week", "7日签到必拿", "每日一次"),
    ("hammer", "每天锤一下领现金", "每日一次"),
    ("shortcut", "添加抖音商城到桌面", "仅一次"),
    ("sign-in", "明日签到", "每日一次"),
]

APPS = [
    ("抖音极速版", "douyin", []),
    ("快手极速版", "kuaishou", [
        ("默认 10 分钟", ["kuaishou_rewards.py", "--minutes", "10"]),
        ("干跑测试 1 分钟", ["kuaishou_rewards.py", "--dry-run", "--minutes", "1", "--rounds", "1"]),
        ("跑 30 分钟", ["kuaishou_rewards.py", "--minutes", "30"]),
    ]),
]

COIN_EVENT_PREFIX = "DROIDFLEET_COIN\t"
SCRIPT_APPS = {"soda.py": "soda", "douyin_rewards.py": "douyin", "kuaishou_rewards.py": "kuaishou"}


@dataclass(frozen=True)
class LogHeading:
    title: str


def read_earning(text: str, credits: dict[str, int]) -> tuple[str, int] | None:
    try:
        event = json.loads(text)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    task, delta, scope = event.get("task"), event.get("delta"), event.get("scope")
    if not isinstance(task, str) or not task.strip() or type(delta) is not int:
        return None
    task = task.strip()
    if scope == "start":
        credits.pop(task, None)
        return None
    if scope == "task":
        delta -= credits.pop(task, 0)
    elif scope == "credit" and delta > 0:
        credits[task] = credits.get(task, 0) + delta
    else:
        return None
    return (task, delta) if delta > 0 else None


class CoinPanel(ttk.LabelFrame):
    """展示当前 App 最近确认的金币增加。"""

    def __init__(self, panel):
        super().__init__(panel, text="金币到账", padding=(8, 4))
        summary = ttk.Frame(self)
        summary.pack(fill="x")
        self.amount = ttk.Label(summary, text="—", font=("Segoe UI", 18, "bold"), foreground="#0e694f")
        self.amount.pack(side="left")
        self.detail = ttk.Label(summary, text="暂无已确认到账", anchor="e")
        self.detail.pack(side="right", fill="x", expand=True, padx=(8, 0))
        self.history = ttk.Treeview(self, columns=("time", "task", "amount"), show="", height=2,
                                    selectmode="none", takefocus=False)
        self.history.column("time", width=62, stretch=False)
        self.history.column("task", width=145)
        self.history.column("amount", width=74, stretch=False, anchor="e")
        self.history.pack(fill="x", pady=(4, 0))
        self.latest: tuple[str, str, int] | None = None

    def record(self, timestamp: str, task: str, amount: int):
        if self.latest is not None:
            previous_time, previous_task, previous_amount = self.latest
            self.history.insert("", 0, values=(previous_time, previous_task, f"{previous_amount:+,}"))
            for row in self.history.get_children()[2:]:
                self.history.delete(row)
        self.latest = (timestamp, task, amount)
        self.amount.configure(text=f"{amount:+,}")
        self.detail.configure(text=f"{task} · {timestamp}")


# --------------------------------------------------------------------------- 主界面

class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("1320x860")
        self.root.minsize(1040, 700)

        self.mirror = None
        self.mirror_kind = tk.StringVar(value="未连接")
        self.device_var = tk.StringVar()
        self.serial: str | None = None
        self.script_proc: subprocess.Popen | None = None
        self.log_queue: queue.Queue[str | LogHeading | tuple[str, str, str, int]] = queue.Queue()
        self.coin_panels: dict[str, CoinPanel] = {}
        self._last_reconnect = 0.0
        self._embed_fails = 0
        self._mirror_gen = 0
        self._busy_until = 0.0
        self._task_serial: str | None = None
        self._poll_only: set[str] = set()
        self.multi_view = False
        self.online_devices: list[str] = []
        self.extras: dict[str, tuple[tk.Frame, PollMirror]] = {}
        self._layout_key: tuple[str | None, ...] | None = None

        try:
            self.root.tk.call("tk", "scaling", user32.GetDpiForSystem() / 72.0)
        except Exception:
            pass

        self._build()
        self._tick_log()
        self.append_log(f"目录   : {BASE}")
        self.append_log(f"adb    : {ADB or '未找到'}")
        self.append_log(f"scrcpy : {SCRCPY or '未找到'}")
        self.append_log(f"python : {PYTHON}")
        self.append_log("提示   : 左侧画面可直接用鼠标点；右侧选 App 与模式后点「运行」")
        self.root.after(250, self.refresh_devices)
        self.root.after(5000, self._guard_mirror)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- 布局
    def _build(self):
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass

        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill="x")
        toolbar = ttk.Frame(top)
        toolbar.pack(fill="x")
        navigation = ttk.Frame(toolbar)
        navigation.pack(side="right", padx=(6, 0))
        ttk.Button(navigation, text="主页", command=lambda: self.key(3)).pack(side="left", padx=(0, 6))
        ttk.Button(navigation, text="返回", command=lambda: self.key(4)).pack(side="left")
        ttk.Label(toolbar, text="设备").pack(side="left")
        self.device_box = ttk.Combobox(toolbar, textvariable=self.device_var, width=28, state="readonly")
        self.device_box.pack(side="left", padx=(6, 6))
        self.device_box.bind("<<ComboboxSelected>>", self._on_device_selected)
        ttk.Button(toolbar, text="刷新设备", command=self.refresh_devices).pack(side="left")
        ttk.Button(toolbar, text="重连投屏", command=self.reconnect).pack(side="left", padx=6)
        ttk.Button(toolbar, text="独立窗口投屏", command=self.open_standalone).pack(side="left")
        self.multi_btn = ttk.Button(toolbar, text="多设备同屏", command=self.toggle_multi_view)
        self.multi_btn.pack(side="left", padx=6)
        ttk.Label(top, textvariable=self.mirror_kind, foreground="#666", width=1,
                  anchor="w").pack(fill="x", pady=(3, 0))

        body = ttk.Panedwindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        left = ttk.Frame(body)
        right = ttk.Frame(body, width=330)
        body.add(left, weight=4)
        body.add(right, weight=1)

        self.mirror_area = tk.Frame(left, bg="black", highlightthickness=0)
        self.mirror_area.pack(fill="both", expand=True)
        self.video_grid = tk.Frame(self.mirror_area, bg="black")
        self.video_grid.pack(fill="both", expand=True)
        self.primary_tile = tk.Frame(self.video_grid, bg="black")
        self.primary_label = ttk.Label(self.primary_tile, anchor="center")
        self.stage = tk.Frame(self.primary_tile, bg="black")
        self.stage.pack(fill="both", expand=True)
        self.stage.bind("<Configure>", lambda _e: self._resize_mirror())
        self.canvas = tk.Canvas(self.stage, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._sync_video_layout()

        # 右侧：按 App 分页
        notebook = ttk.Notebook(right)
        notebook.pack(fill="x")
        soda_tab = ttk.Frame(notebook, padding=8)
        notebook.add(soda_tab, text="汽水音乐")
        self.mode_vars = {}
        self.coin_panels["soda"] = CoinPanel(soda_tab)
        self.coin_panels["soda"].pack(fill="x", pady=(0, 8))

        ttk.Button(soda_tab, text="▶ 开始新一轮任务（不含已排除）",
                   command=lambda: self.run_cmd(["soda.py", "auto", "all", "--round"],
                                                "汽水音乐·整轮")).pack(fill="x", pady=(0, 8))
        grid = ttk.Frame(soda_tab)
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        for index, (task_key, label) in enumerate(SODA_TASKS):
            ttk.Button(grid, text=label,
                       command=lambda k=task_key, name=label: self.run_cmd(
                           ["soda.py", "auto", k], f"汽水音乐·{name}")
                       ).grid(row=index // 2, column=index % 2, sticky="ew", padx=2, pady=2)
        ttk.Label(soda_tab, text="已排除：" + "、".join(SODA_SKIPPED),
                  foreground="#888").pack(anchor="w", pady=(8, 4))
        utils = ttk.Frame(soda_tab)
        utils.pack(fill="x")
        ttk.Button(utils, text="工具命令行",
                   command=lambda: self._spawn_console(["soda.cmd"], title="汽水音乐工具")
                   ).pack(side="left")
        ttk.Button(utils, text="本轮账本",
                   command=lambda: self.run_cmd(["soda.py", "round", "show"], "账本")
                   ).pack(side="left", padx=4)
        ttk.Button(utils, text="页面识别",
                   command=lambda: self.run_cmd(["soda.py", "inspect"], "识别")
                   ).pack(side="left")

        for name, key, modes in APPS:
            tab = ttk.Frame(notebook, padding=8)
            notebook.add(tab, text=name)
            self.coin_panels[key] = CoinPanel(tab)
            self.coin_panels[key].pack(fill="x", pady=(0, 8))
            if key == "douyin":
                ttk.Button(tab, text="▶ 开始本轮任务（10 项）",
                           command=lambda: self.run_cmd(["douyin_rewards.py", "--task", "all"],
                                                        "抖音·整轮")).pack(fill="x", pady=(0, 8))
                grid = ttk.Frame(tab)
                grid.pack(fill="x")
                grid.columnconfigure(0, weight=1)
                grid.columnconfigure(1, weight=1)
                for index, (task_key, label, limit) in enumerate(DOUYIN_TASKS):
                    ttk.Button(grid, text=label,
                               command=lambda k=task_key, n=label: self.run_cmd(
                                   ["douyin_rewards.py", "--task", k], f"抖音·{n}")
                               ).grid(row=index // 2, column=index % 2, sticky="ew", padx=2, pady=2)
                ttk.Label(tab,
                          text="每日一次：明日签到、7日签到必拿、每天锤一下领现金、预约领金币\n"
                               "仅一次：添加抖音商城到桌面",
                          foreground="#888", justify="left").pack(anchor="w", pady=(8, 0))
                ttk.Button(tab, text="页面识别",
                           command=lambda: self.run_cmd(["douyin_rewards.py", "--inspect"], "抖音·识别")
                           ).pack(fill="x", pady=(8, 0))
                continue
            if not modes:
                continue
            var = tk.StringVar(value=modes[0][0])
            self.mode_vars[key] = (var, modes)
            ttk.Combobox(tab, textvariable=var, state="readonly",
                         values=[m[0] for m in modes]).pack(fill="x")
            ttk.Button(tab, text="运行", command=lambda k=key: self.run_app(k)
                       ).pack(fill="x", pady=(6, 0))

        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=(2, 6))
        ttk.Button(actions, text="停止脚本", command=self.stop_script).pack(side="left")
        ttk.Button(actions, text="打开日志目录", command=self.open_logs).pack(side="left", padx=6)

        log_header = ttk.Frame(right)
        log_header.pack(fill="x", pady=(6, 2))
        ttk.Label(log_header, text="运行日志").pack(side="left")
        ttk.Button(log_header, text="清空日志", command=self.clear_log).pack(side="right")
        log_frame = ttk.Frame(right)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, height=10, wrap="word", state="disabled",
                           font=("Consolas", 9))
        self.log.tag_configure("task", font=("Segoe UI", 10, "bold"), foreground="#173654",
                               background="#e6eef6", spacing1=6, spacing3=4)
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # ---- 设备 / 投屏
    def refresh_devices(self):
        if getattr(self, "_refreshing", False):
            return
        self._refreshing = True

        def work():
            items = device_report()
            self.root.after(0, lambda: self._apply_devices(items))

        threading.Thread(target=work, daemon=True).start()

    def _apply_devices(self, items):
        self._refreshing = False
        if items is None:
            if self.mirror is None:
                self.set_status("暂时无法读取 ADB 设备列表，稍后重试")
            return
        devices = [serial for serial, state in items if state == "device"]
        pending = [f"{serial}({state})" for serial, state in items if state != "device"]
        self.online_devices = devices
        self.device_box["values"] = devices
        if devices and self.device_var.get() not in devices:
            # 多设备时保留当前选择；没有选择时用上次用过的设备
            if self.serial in devices:
                self.device_var.set(self.serial)
            else:
                saved = self._load_saved_serial()
                chosen = saved if saved in devices else devices[0]
                self.device_var.set(chosen)
                self.append_log(f"自动选择设备：{chosen}" + ("（上次使用）" if chosen == saved else ""))
        if not devices:
            if self.mirror is not None:
                self._mirror_gen += 1
                self.stop_mirror()
            self._sync_video_layout()
            hint = "、".join(pending) if pending else "无设备"
            if self.serial:
                self.set_status(f"等待设备上线（{hint}）")
            else:
                self.set_status(f"没有可用设备：{hint}")
            return
        if self.serial != self.device_var.get() or self.mirror is None:
            self.reconnect()
        else:
            self._sync_video_layout()
            self._guard_once()

    def _guard_mirror(self):
        self.refresh_devices()
        self.root.after(5000, self._guard_mirror)

    def _guard_once(self):
        """看门狗：设备在线但主投屏退出或长时间没有画面时重连。"""
        try:
            if self.serial not in self.online_devices or time.monotonic() < self._busy_until:
                return
            dead = self.mirror is None
            if isinstance(self.mirror, ScrcpyMirror):
                proc = self.mirror.proc
                dead = proc is None or proc.poll() is not None
            elif isinstance(self.mirror, PollMirror):
                # 截图轮询超过 12 秒没画面，再试着恢复内嵌投屏
                dead = time.monotonic() - self.mirror.last_frame > 12
                if dead:
                    self._embed_fails = 0
            if dead and time.monotonic() - self._last_reconnect > 8:
                self._last_reconnect = time.monotonic()
                self.reconnect()
        except Exception:
            pass

    def _state_path(self) -> Path:
        return BASE / "logs" / "gui-state.json"

    def _load_saved_serial(self) -> str:
        try:
            data = json.loads(self._state_path().read_text(encoding="utf-8-sig"))
            return str(data.get("serial") or "")
        except Exception:
            return ""

    def _save_serial(self, serial: str) -> None:
        try:
            path = self._state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"serial": serial}, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _on_device_selected(self, _event=None):
        self._save_serial(self.device_var.get().strip())
        self.reconnect()

    def reconnect(self):
        serial = self.device_var.get().strip()
        if serial not in self.online_devices:
            return
        self._mirror_gen += 1
        gen = self._mirror_gen
        self._busy_until = time.monotonic() + 6
        self.stop_mirror()
        changed = serial != self.serial
        if changed:
            self._embed_fails = 0
        self.serial = serial
        self._sync_video_layout()
        self.canvas.pack_forget()
        if changed and self.script_proc and self.script_proc.poll() is None:
            self.append_log(f"[投屏] 切换设备不影响正在运行的任务（任务固定用 {self._task_serial}）")
        if serial in self._poll_only:
            self.set_status("该设备 scrcpy 不稳定，改用截图轮询")
            self._start_poll(serial, gen)
            return
        self.set_status("正在启动投屏…")

        def fallback(reason: str):
            # 回调来自后台线程，切回主线程再动 UI
            self.root.after(0, lambda: self._on_embed_fail(serial, gen, reason))

        self.root.update_idletasks()
        host_hwnd = self.stage.winfo_id()
        self.mirror = ScrcpyMirror(self, host_hwnd, serial, fallback)
        self.mirror.start()

    def _on_embed_fail(self, serial: str, gen: int, reason: str):
        if self.serial != serial or gen != self._mirror_gen:
            return
        self._embed_fails += 1
        crashed = "code" in reason and "code 0" not in reason
        if crashed:
            # 某些机型（例如 Android 8 的 S8）内嵌会直接崩溃，改用轮询，避免反复黑屏
            self._poll_only.add(serial)
            self.set_status("该设备 scrcpy 连不上，已切换为截图轮询")
            self._start_poll(serial, gen)
            return
        if self._embed_fails <= 2:
            # 多半是另一台设备接入导致 adb 重启，隔几秒重试内嵌
            self.set_status(f"投屏中断（{reason}），5 秒后重试…")
            self.root.after(5000,
                            lambda: self.serial == serial and gen == self._mirror_gen and self.reconnect())
            return
        self.set_status(f"scrcpy 内嵌失败（{reason}），改用截图轮询")
        self._start_poll(serial, gen)

    def _start_poll(self, serial: str, gen: int):
        if gen != self._mirror_gen:
            return
        self.stop_mirror()
        self.canvas.pack(fill="both", expand=True)
        self.mirror = PollMirror(self, self.canvas, serial, lambda r: None)
        self.mirror.start()
        self.set_status("截图轮询投屏（约 3 帧/秒，可点击操作）")

    def _sync_video_layout(self):
        """等分画面区域，并保留设备未变的投屏。"""
        show_devices = self.multi_view and self.serial in self.online_devices
        primary = self.serial if show_devices else None
        extras = []
        if show_devices:
            extras = [serial for serial in self.online_devices if serial != primary][:3]
        layout_key = (primary, *extras)
        if layout_key == self._layout_key:
            return

        for serial in list(self.extras):
            if serial not in extras:
                tile, mirror = self.extras.pop(serial)
                mirror.stop()
                tile.destroy()
        if primary:
            self.primary_label.configure(text=primary)
            self.primary_label.pack(fill="x", before=self.stage)
        else:
            self.primary_label.pack_forget()

        tiles = [self.primary_tile]
        for serial in extras:
            if serial not in self.extras:
                tile = tk.Frame(self.video_grid, bg="black")
                ttk.Label(tile, text=serial, anchor="center").pack(fill="x")
                canvas = tk.Canvas(tile, bg="black", highlightthickness=0)
                canvas.pack(fill="both", expand=True)
                mirror = PollMirror(self, canvas, serial, lambda _r: None)
                mirror.start()
                self.extras[serial] = (tile, mirror)
            tiles.append(self.extras[serial][0])

        cols = 2 if len(tiles) > 1 else 1
        rows = (len(tiles) + cols - 1) // cols
        for index in range(2):
            self.video_grid.rowconfigure(index, weight=int(index < rows),
                                         uniform="tile" if index < rows else "")
            self.video_grid.columnconfigure(index, weight=int(index < cols),
                                            uniform="tile" if index < cols else "")
        for index, tile in enumerate(tiles):
            row, col = divmod(index, cols)
            tile.grid(row=row, column=col, sticky="nsew", padx=1, pady=1)
        self._layout_key = layout_key

    def toggle_multi_view(self):
        self.multi_view = not self.multi_view
        self.multi_btn.configure(text="关闭多设备" if self.multi_view else "多设备同屏")
        self._sync_video_layout()

    def _resize_mirror(self):
        if isinstance(self.mirror, ScrcpyMirror):
            self.mirror.resize()

    def stop_mirror(self):
        if self.mirror:
            try:
                self.mirror.stop()
            except Exception:
                pass
            self.mirror = None

    def open_standalone(self):
        serial = self.serial or self.device_var.get().strip()
        if not serial:
            messagebox.showwarning(APP_TITLE, "还没有选择设备")
            return
        exe = SCRCPY or "scrcpy"
        self._spawn_console([exe, "-s", serial, "--stay-awake"], title="scrcpy")

    def key(self, code: int):
        serial = self.serial or self.device_var.get().strip()
        if not serial:
            return
        threading.Thread(target=lambda: adb_run(["shell", "input", "keyevent", str(code)],
                                                serial, timeout=8), daemon=True).start()

    def set_status(self, text: str):
        self.mirror_kind.set(text)

    # ---- 脚本
    def run_cmd(self, argv, label: str = ""):
        """运行一条脚本命令，输出实时进日志区。"""
        if self.script_proc and self.script_proc.poll() is None:
            if not messagebox.askyesno(APP_TITLE, "已有脚本在运行，先停止再启动新的吗？"):
                return
            self.stop_script()

        cmd = [PYTHON, str(BASE / argv[0])] + [str(a) for a in argv[1:]]
        env = child_env()
        app_key = SCRIPT_APPS.get(Path(argv[0]).name)
        if app_key is not None:
            env["DROIDFLEET_GUI_COIN_EVENTS"] = "1"
        serial = self.serial or self.device_var.get().strip()
        self._task_serial = serial
        if serial:
            env["ADB_SERIAL"] = serial
            env["ANDROID_SERIAL"] = serial
        self.log_queue.put(LogHeading(label or "运行任务"))
        try:
            self.script_proc = subprocess.Popen(
                cmd, cwd=str(BASE), env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                bufsize=1, creationflags=CREATE_NO_WINDOW,
            )
        except Exception as error:
            self.log_queue.put(f"启动失败：{error}")
            return
        batch = app_key == "kuaishou" or "all" in argv[1:]
        threading.Thread(target=self._pump, args=(self.script_proc, app_key, batch), daemon=True).start()

    def run_app(self, key: str):
        var, modes = self.mode_vars[key]
        label = var.get()
        target = next((m[1] for m in modes if m[0] == label), None)
        if target is None:
            return
        if target == "console":
            self._spawn_console(["soda.cmd"], title="汽水音乐工具")
            self.append_log("[console] 已打开 soda.cmd，可在新窗口里手动执行命令")
            return
        app_name = next((name for name, app_key, _ in APPS if app_key == key), "")
        self.run_cmd(target, f"{app_name}·{label}" if app_name else label)

    def _pump(self, proc: subprocess.Popen, app_key: str | None, batch: bool = False):
        assert proc.stdout is not None
        credits: dict[str, int] = {}
        task_seen = False
        for line in proc.stdout:
            text = line.rstrip("\r\n")
            if text.startswith(COIN_EVENT_PREFIX):
                earning = read_earning(text.removeprefix(COIN_EVENT_PREFIX), credits)
                if app_key is not None and earning is not None:
                    task, amount = earning
                    self.log_queue.put((app_key, time.strftime("%H:%M:%S"), task, amount))
                continue
            fields = text.split(" | ", 4)
            if len(fields) >= 4 and fields[3] == "开始前金币":
                if batch or task_seen:
                    self.log_queue.put(LogHeading(f"{fields[1]}·{fields[2]}"))
                task_seen = True
            self.log_queue.put(text)
        code = proc.wait()
        self.log_queue.put(f"[退出码 {code}]")

    def stop_script(self):
        proc = self.script_proc
        if not proc or proc.poll() is not None:
            return
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, creationflags=CREATE_NO_WINDOW)
        except Exception:
            proc.kill()
        self.append_log("[停止] 已终止当前脚本")

    def _spawn_console(self, argv, title=APP_NAME):
        workdir = str(BASE)
        inner = subprocess.list2cmdline([str(a) for a in argv])
        cmd = ["cmd", "/c", "start", title, "cmd", "/k", inner]
        try:
            subprocess.Popen(cmd, cwd=workdir, env=child_env(), creationflags=CREATE_NO_WINDOW)
        except Exception as error:
            messagebox.showerror(APP_TITLE, f"打开命令行失败: {error}")

    def open_logs(self):
        try:
            path = log_directory().resolve()
            path.mkdir(parents=True, exist_ok=True)
            explorer = Path(os.environ.get("WINDIR", r"C:\Windows")) / "explorer.exe"
            subprocess.Popen([str(explorer), str(path)])
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror(APP_TITLE, f"打开日志目录失败：{error}", parent=self.root)

    # ---- 日志
    def append_log(self, text: str, *, tag: str = ""):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self):
        self._drain_log(self.log_queue.qsize(), show_text=False)
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _drain_log(self, count: int, *, show_text: bool = True):
        for _ in range(count):
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(line, tuple):
                app_key, timestamp, task, amount = line
                self.coin_panels[app_key].record(timestamp, task, amount)
            elif show_text:
                if isinstance(line, LogHeading):
                    self.append_log(f"━━  {line.title}  ━━", tag="task")
                else:
                    self.append_log(line)

    def _tick_log(self):
        self._drain_log(200)
        self.root.after(120, self._tick_log)

    # ---- 退出
    def _on_close(self):
        self.stop_script()
        self.stop_mirror()
        for _tile, mirror in self.extras.values():
            try:
                mirror.stop()
            except Exception:
                pass
        self.extras.clear()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main() -> int:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_NAME}.Console")
    except Exception:
        pass
    app = App()
    if "--selftest" in sys.argv:
        # 无人值守自检：跑一次「查看任务清单」，把日志写到 logs/gui-selftest.txt
        def start():
            app.mode_vars["soda"][0].set("查看任务清单")
            app.run_app("soda")

        def finish():
            text = app.log.get("1.0", "end")
            out = BASE / "logs" / "gui-selftest.txt"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            app._on_close()

        app.root.after(1200, start)
        app.root.after(15000, finish)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
