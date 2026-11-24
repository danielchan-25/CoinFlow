"""adb 设备封装：命令执行、手势、媒体音量（多路回退 + 校验）。"""
from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Callable, Sequence


class AdbError(RuntimeError):
    pass


class Adb:
    def __init__(self, serial: str | None = None) -> None:
        self.serial = serial or os.environ.get("ADB_SERIAL") or os.environ.get("ANDROID_SERIAL")
        self._connected = False

    # ---- 基础 -----------------------------------------------------------
    def _selector(self) -> list[str]:
        return ["-s", self.serial] if self.serial else []

    def run(self, *args: str, check: bool = True, timeout: float = 90) -> bytes:
        command = ["adb", *self._selector(), *args]
        started = time.monotonic()
        try:
            from droidfleet import progress

            with progress.countdown(progress.APP_SODA, None, "等待 ADB 响应", timeout):
                proc = subprocess.run(command, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            from droidfleet import log

            log.error("adb.timeout", error, args=list(args), ms=int((time.monotonic() - started) * 1000))
            raise
        elapsed_ms = int((time.monotonic() - started) * 1000)
        try:
            from droidfleet import log

            log.adb_result(args=list(args), returncode=proc.returncode, ms=elapsed_ms, serial=self.serial,
                           stdout_bytes=len(proc.stdout), stderr=proc.stderr.decode("utf-8", "replace")[:1000])
        except Exception:
            pass
        if check and proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip() or proc.stdout.decode("utf-8", "replace").strip()
            raise AdbError(f"{' '.join(command)} 失败：{detail}")
        return proc.stdout

    def shell(self, *args: str, check: bool = True) -> bytes:
        return self.run("shell", *args, check=check)

    def connect(self) -> None:
        if self._connected or not self.serial or ":" not in self.serial:
            self._connected = True
            return
        try:
            self.run("connect", self.serial, check=False, timeout=20)
        except Exception:
            pass
        self._connected = True

    def devices(self) -> list[str]:
        output = self.run("devices", check=False)
        result = []
        for line in output.decode("utf-8", "replace").splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                result.append(parts[0])
        return result

    # ---- 手势 -----------------------------------------------------------
    def tap(self, x: float, y: float) -> None:
        self.shell("input", "tap", str(int(x)), str(int(y)))

    def long_press(self, x: float, y: float, seconds: float) -> None:
        self.shell("input", "swipe", str(int(x)), str(int(y)), str(int(x)), str(int(y)),
                   str(int(seconds * 1000)))

    def swipe(self, x1: float, y1: float, x2: float, y2: float, duration_ms: int) -> None:
        self.shell("input", "swipe", str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)),
                   str(int(duration_ms)))

    def key(self, name: str) -> None:
        self.shell("input", "keyevent", name)

    def screencap(self) -> bytes:
        return self.run("exec-out", "screencap", "-p", timeout=10)

    # ---- 音量 -----------------------------------------------------------
    _VOLUME_INDEX = re.compile(r"streamVolume:(\d+)")
    _VOLUME_MAX = re.compile(r"^\s*Max: (\d+)", re.MULTILINE)
    _VOLUME_DEVICE = re.compile(r": (\d+)\\(\d+)")

    def media_volume(self) -> tuple[int, int] | None:
        try:
            dump = self.shell("dumpsys", "audio", check=False).decode("utf-8", "replace")
        except Exception:
            return None
        block = dump.split("- STREAM_MUSIC:", 1)
        if len(block) != 2:
            return None
        body = "\n".join(block[1].splitlines()[:14])
        maximum = self._VOLUME_MAX.search(body)
        index = self._VOLUME_INDEX.search(body)
        max_index = int(maximum.group(1)) if maximum else 15
        if index:
            return min(int(index.group(1)), max_index), max_index
        matches = self._VOLUME_DEVICE.findall(body)
        if matches:
            return min(int(matches[-1][0]), max_index), max_index
        return None

    def _verify_volume(self, target: int) -> bool:
        current = self.media_volume()
        return current is not None and current[0] == target

    def set_media_volume(self, target: int, attempts: Sequence[int] | None = None,
                         *, delay: Callable[[float], float] = float) -> int | None:
        current = self.media_volume()
        if current is None:
            return None
        max_index = current[1]
        target = max(0, min(target, max_index))
        if current[0] == target:
            return target
        strategies = [
            ("cmd", "media_session", "volume", "--stream", "3", "--set", str(target)),
            ("media", "volume", "--stream", "3", "--set", str(target)),
        ]
        for strategy in strategies:
            try:
                self.shell(*strategy, check=False)
            except Exception:
                continue
            time.sleep(delay(0.4))
            if self._verify_volume(target):
                return target
        try:
            self.shell("settings", "put", "system", "volume_music", str(target), check=False)
        except Exception:
            pass
        time.sleep(delay(0.3))
        if self._verify_volume(target):
            return target
        step = 25 if target < current[0] else 24
        delta = current[0] - target
        for _ in range(abs(delta)):
            try:
                self.key("KEYCODE_VOLUME_DOWN" if step == 25 else "KEYCODE_VOLUME_UP")
            except Exception:
                break
            time.sleep(delay(0.15))
        time.sleep(delay(0.3))
        if self._verify_volume(target):
            return target
        latest = self.media_volume()
        return latest[0] if latest else None
