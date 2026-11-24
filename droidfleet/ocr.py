"""截屏 + Vision OCR 封装（tools/douyin_ocr.py，经 macOS Vision 识别）。"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple

from droidfleet.device import Adb, AdbError

HELPER = Path(__file__).resolve().parent.parent / "tools" / "douyin_ocr.py"


class OcrError(AdbError):
    """截图已取得，文字识别失败。"""

    def __init__(self, message: str, png: bytes):
        super().__init__(message)
        self.png = png


@dataclass
class Line:
    text: str
    x: float
    y: float
    w: float
    h: float

    @property
    def min_x(self) -> float:
        return self.x - self.w / 2

    @property
    def max_x(self) -> float:
        return self.x + self.w / 2

    @property
    def min_y(self) -> float:
        return self.y - self.h / 2

    @property
    def max_y(self) -> float:
        return self.y + self.h / 2


@dataclass
class Screen:
    png: bytes
    width: int
    height: int
    lines: list[Line]


def ensure_helper() -> Path:
    if not HELPER.exists():
        raise AdbError(f"缺少 OCR 助手：{HELPER}")
    if sys.platform == "darwin" and shutil_which("osascript") is None:
        raise AdbError("缺少 osascript，OCR 只能在 macOS 上运行")
    return HELPER


def shutil_which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def helper_path() -> Path:
    return ensure_helper()


def helper_args(args: Sequence[str]) -> list[str]:
    return list(args)


def capture(adb: Adb) -> Screen:
    started = time.monotonic()
    helper = ensure_helper()
    png = adb.screencap()
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        handle.write(png)
        path = handle.name
    try:
        output = subprocess.run([sys.executable, str(helper), "ocr", path], capture_output=True, timeout=15)
    except subprocess.TimeoutExpired as error:
        raise OcrError("OCR 识别超时", png) from error
    finally:
        Path(path).unlink(missing_ok=True)
    if output.returncode != 0:
        raise OcrError("OCR 失败：" + output.stderr.decode("utf-8", "replace").strip(), png)
    try:
        data = json.loads(output.stdout)
    except json.JSONDecodeError as error:
        raise OcrError(f"OCR 返回无效数据：{error}", png) from error
    lines = [
        Line(item["text"], item["x"] + item["w"] / 2, item["y"] + item["h"] / 2, item["w"], item["h"])
        for item in data["lines"]
    ]
    screen = Screen(png, data["width"], data["height"], lines)
    try:
        from droidfleet import log

        config = log._config()
        preview = int(config.get("ocr_preview", 5) or 0)
        shot = log.save_screen(png, "ocr")
        extra = {"screen": shot} if shot else {}
        if config.get("ocr_lines", True):
            extra["line_details"] = [{"text": line.text, "x": round(line.x, 4), "y": round(line.y, 4),
                                      "w": round(line.w, 4), "h": round(line.h, 4)} for line in lines]
        log.event(
            "ocr",
            width=screen.width,
            height=screen.height,
            lines=len(lines),
            bytes=len(png),
            ms=int((time.monotonic() - started) * 1000),
            preview=[line.text for line in lines[:preview]],
            **extra,
        )
    except Exception:
        pass
    return screen


def find_all(screen: Screen, pattern: str, x_range: Optional[Tuple[float, float]] = None,
             y_range: Optional[Tuple[float, float]] = None) -> list[Line]:
    hits = []
    for line in screen.lines:
        if not re.search(pattern, line.text):
            continue
        if x_range and not (x_range[0] <= line.x <= x_range[1]):
            continue
        if y_range and not (y_range[0] <= line.y <= y_range[1]):
            continue
        hits.append(line)
    return hits


def find(screen: Screen, pattern: str, x_range: Optional[Tuple[float, float]] = None,
         y_range: Optional[Tuple[float, float]] = None) -> Optional[Line]:
    hits = find_all(screen, pattern, x_range, y_range)
    return hits[0] if hits else None
