"""结构化运行日志：每次脚本运行写一份 JSONL，便于事后分析。

默认输出到仓库根目录 logs/<app>/<app>-YYYYmmdd-HHMMSS-微秒-<pid>.jsonl，
同目录下 latest.jsonl 指向最新一份。字段：

    ts      带时区的 ISO 时间戳（毫秒）
    app     应用名（soda / douyin / kuaishou / rules ...）
    event   事件类型（run.start / adb / tap / ocr / safety.block / error ...）
    level   info / warn / error
    data    事件字段
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
_CURRENT: "RunLog | None" = None
_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(record.payload, ensure_ascii=False, default=str)


class JsonHandler(logging.StreamHandler):
    def __init__(self, stream: Any) -> None:
        super().__init__(stream)
        self.failed = False

    def handleError(self, record: logging.LogRecord) -> None:
        # 日志写入失败时避免标准库将完整事件内容输出到终端。
        if not self.failed:
            print("运行日志写入失败", file=sys.stderr)
        self.failed = True


def _config() -> dict[str, Any]:
    try:
        from droidfleet.rules import load_rules

        rules = load_rules()
    except Exception:
        return {}
    config: dict[str, Any] = {}
    for key, value in rules.section("logging").items():
        config[key] = value
    return config


class RunLog:
    def __init__(self, app: str, argv: list[str] | None = None) -> None:
        self.app = app
        self.started = time.time()
        self._started_monotonic = time.monotonic()
        self.enabled = True
        self.path: Path | None = None
        self._handle = None
        self._handler: JsonHandler | None = None
        self._logger = logging.Logger(f"droidfleet.{app}", logging.DEBUG)
        self._logger.propagate = False
        self.context: dict[str, Any] = {}
        config = _config()
        self.adb_slow_ms = int(config.get("adb_slow_ms", 1500))
        self.save_screens = bool(config.get("save_screens", True))
        self.ocr_lines = bool(config.get("ocr_lines", True))
        self.screens_dir: Path | None = None
        self._screen_seq = 0
        self.enabled = bool(config.get("enabled", True))
        if not self.enabled:
            return
        override = os.environ.get("DROIDFLEET_LOG_FILE")
        if override:
            path = Path(override)
            if not path.is_absolute():
                path = ROOT / path
            folder = path.parent
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.enabled = False
                return
            self.path = path
        else:
            self.path = None
            folder = None
        if self.path is None:
            directory = Path(str(config.get("dir", "logs")))
            base = directory if directory.is_absolute() else ROOT / directory
            folder = base / app
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.enabled = False
                return
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            self.path = folder / f"{app}-{stamp}-{os.getpid()}.jsonl"
        try:
            self._handle = self.path.open("a", encoding="utf-8")
        except OSError:
            self.enabled = False
            return
        self._handler = JsonHandler(self._handle)
        self._handler.setFormatter(JsonFormatter())
        self._logger.addHandler(self._handler)
        if not override:
            self._link_latest(folder)
        self.event(
            "run.start",
            argv=list(argv if argv is not None else sys.argv),
            cwd=os.getcwd(),
            pid=os.getpid(),
            python=sys.version.split()[0],
            serial=os.environ.get("ADB_SERIAL") or os.environ.get("ANDROID_SERIAL"),
        )

    def save_screen(self, png: bytes, tag: str = "screen") -> str | None:
        """把这一步的截图存到 <run>-screens/ 下，返回路径。"""
        if not (self.enabled and self.save_screens and self.path is not None and png):
            return None
        if self.screens_dir is None:
            self.screens_dir = self.path.parent / f"{self.path.stem}-screens"
            try:
                self.screens_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.screens_dir = None
                return None
        self._screen_seq += 1
        target = self.screens_dir / f"{self._screen_seq:04d}-{tag}.png"
        try:
            target.write_bytes(png)
        except OSError:
            return None
        return str(target)

    def _link_latest(self, folder: Path) -> None:
        assert self.path is not None
        link = folder / "latest.jsonl"
        candidate = folder / f".latest-{self.path.stem}"
        try:
            candidate.symlink_to(self.path.name)
            candidate.replace(link)
        except OSError:
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass

    def event(self, kind: str, level: str = "info", **data: Any) -> None:
        if not self.enabled or self._handle is None:
            return
        record: dict[str, Any] = {
            "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "app": self.app,
            "event": kind,
            "level": level,
        }
        if self.context or data:
            record["data"] = {**self.context, **data}
        self._logger.log(_LEVELS.get(level, logging.INFO), kind, extra={"payload": record})
        if self._handler is not None and self._handler.failed:
            self.enabled = False

    def error(self, kind: str, error: BaseException, **data: Any) -> None:
        self.event(kind, level="error", error=repr(error),
                   traceback="".join(traceback.format_exception(error))[-4000:], **data)

    def close(self, status: str = "ok", **data: Any) -> None:
        if self._handle is not None:
            self.event("run.end", status=status, duration_s=round(time.monotonic() - self._started_monotonic, 3), **data)
        if self._handler is not None:
            self._logger.removeHandler(self._handler)
            self._handler.close()
            self._handler = None
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None


def _process_name() -> str:
    name = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else "droidfleet"
    return name or "droidfleet"


def init_run(app: str, argv: list[str] | None = None) -> RunLog:
    global _CURRENT
    if _CURRENT is not None:
        _CURRENT.close(status="replaced")
    _CURRENT = RunLog(app, argv)
    return _CURRENT


def current() -> RunLog:
    global _CURRENT
    if _CURRENT is None:
        _CURRENT = RunLog(_process_name())
    return _CURRENT


def event(kind: str, level: str = "info", **data: Any) -> None:
    current().event(kind, level=level, **data)


def message(kind: str, text: str, /, *, level: str = "info", to: Any = None, **data: Any) -> None:
    event(kind, level=level, message=text, **data)
    if to is not None:
        print(text, file=to, flush=True)


def save_screen(png: bytes, tag: str = "screen") -> str | None:
    """把当前截图归档到本次运行的 screens 目录。"""
    try:
        return current().save_screen(png, tag)
    except Exception:
        return None


def adb_result(*, ms: int, returncode: int = 0, **data: Any) -> None:
    logger = current()
    slow = ms >= logger.adb_slow_ms
    level = "error" if returncode else "warn" if slow else "info"
    if returncode and "reason" not in data:
        data["reason"] = data.get("stderr") or f"ADB 退出码 {returncode}"
    elif slow and "reason" not in data:
        data["reason"] = f"耗时 {ms} 毫秒"
    logger.event("adb", level=level, ms=ms, returncode=returncode, slow=slow, **data)


def error(kind: str, exc: BaseException, **data: Any) -> None:
    current().error(kind, exc, **data)


def close(status: str = "ok", **data: Any) -> None:
    if _CURRENT is not None:
        _CURRENT.close(status=status, **data)
