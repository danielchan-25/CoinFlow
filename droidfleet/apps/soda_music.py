#!/usr/bin/env python3
"""汽水音乐福利任务工具：OCR + 拟人化手势（参数全部来自 rules/）。"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

from droidfleet import earnings, exits, progress
from droidfleet import log as run_log, ocr, safety, session
from droidfleet.device import Adb, AdbError
from droidfleet.human import Observation, confirm_stable, observe_tap, read_after_interruption
from droidfleet.ollama import (
    OllamaFailure, RecoveryDecision, diagnose_screen, image_chat, matches_dismissal, model_settings,
    read_structured, recover_screen, risky_dismissal,
)
from droidfleet.ocr import Line, Screen
from droidfleet.rules import load_rules

APP = "soda_music"
RETRY_NAVIGATION = {"去逛街", "返回顶部", "关闭弹窗"}
TASK_FILTERS = ("all", "repeatable", "wait", "daily", "conditional")
AI_MODES = ("off", "auto", "on")


@dataclass(frozen=True)
class Task:
    name: str
    title: str
    repeatable: bool
    wait: str
    observations: str
    detail: str
    kind: str = ""
    waits_between_attempts: bool = False


TASKS = {
    "ad": Task("看广告", "^看广告赚金币", True, "广告倒计时", "广告倒计时、续看提示与奖励弹窗",
               "本轮验证可重复，次数从 4 增至 5。下次金额以任务卡为准。"),
    "shop": Task("逛街", "^逛街赚金币", True, "实际计时至少 30 秒", "直播或商品页、计时浮标与退出提示",
                 "页面显示每日 15 次。本轮计时停滞，未到账。"),
    "video": Task("连续刷视频", "^连续刷视频赚金币", True, "观看计时与轮间冷却", "广告流、计奖浮标和续看结算",
                  "实测有「赚钱中」「已暂停」和约 5 分钟冷却。入口开放时才继续。",
                  waits_between_attempts=True),
    "listen": Task("听歌", "^听歌赚金币", True, "累计播放到下一档", "播放状态、音量提示和待领取档位",
                   "可分档领取。当前档位、奖励和剩余时间由页面给出。", waits_between_attempts=True),
    "watch": Task("看视频", "^看视频赚金[币市]", True, "看满档位视频数", "视频流、档位进度和待领取档位",
                  "按 6/12/18/24/30 个视频分档，可重复。", waits_between_attempts=True),
    "check-in": Task("签到", "^签到得金币", False, "下一自然日", "签到状态、次日提示和领奖弹窗",
                     "页面显示「明天继续签到」时，今天已完成。", "daily", waits_between_attempts=True),
    "flip-card": Task("翻卡", "^翻卡赢金[币市]", False, "等入口或广告机会",
                      "倒计时、翻卡机会和结果弹窗", "显示机会 24 小时有效，本轮只见倒计时。",
                      "conditional", waits_between_attempts=True),
    "chest": Task("宝箱", "^开宝箱得金币$", False, "以页面提示为准", "浮动入口与宝箱结算弹窗",
                  "实测只观察到入口，领取条件和收益待验证。", "conditional"),
}

STATE_LABELS = {
    "ready": "入口可用", "waiting": "需等待", "counting": "页面显示计时", "paused": "暂停或待续看",
    "claimable": "可领取或有奖励弹窗", "completed": "已完成", "expired": "窗口已错过",
    "blocked": "条件不足", "unknown": "未确认",
}
AI_SCHEMA = {
    "type": "object",
    "properties": {
        "state": {"type": "string", "enum": list(STATE_LABELS)},
        "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "remaining_seconds": {"type": ["integer", "null"], "minimum": 0, "maximum": 86400},
    },
    "required": ["state", "evidence", "remaining_seconds"],
    "additionalProperties": False,
}


class ForegroundError(RuntimeError):
    pass


class UnresolvedScreen(RuntimeError):
    """已请求本地 AI，页面仍无法确认。"""


class UnconfirmedAd(UnresolvedScreen):
    """广告尚未核实，保留当前页面供人工检查。"""


def _cropped_ocr(screen: Screen, command: str, bounds: tuple[int, int, int, int]) -> list[dict]:
    left, top, right, bottom = bounds
    if not screen.png or left >= right or top >= bottom:
        return []
    try:
        with tempfile.TemporaryDirectory(prefix="droidfleet-ocr-") as folder:
            path = Path(folder) / "screen.png"
            path.write_bytes(screen.png)
            args = [command, str(path), *(str(value) for value in (left, top, right - left, bottom - top))]
            result = subprocess.run([sys.executable, str(ocr.helper_path()), *ocr.helper_args(args)],
                                    capture_output=True, timeout=15, check=True)
            data = json.loads(result.stdout)
        return data["lines"] if (data["width"], data["height"]) == (screen.width, screen.height) else []
    except (AdbError, OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return []


def coins_on(screen: Screen, *, retry_numbers: bool = False) -> Optional[int]:
    center = any(line.text == "福利中心" and line.y < 0.13 for line in screen.lines)
    labels = [line for line in screen.lines if line.text.startswith("金币收益") and line.y < 0.32]
    if not center or len(labels) != 1:
        return None
    label = labels[0]
    compact = re.fullmatch(r"金币收益[:：]?\s*([0-9]+|[0-9]{1,3}(?:[,，][0-9]{3})+)", label.text)
    if compact is not None:
        return int(re.sub("[,，]", "", compact.group(1)))
    amounts = [line.text for line in screen.lines if abs(line.x - label.x) < 0.09
               and 0.009 < label.y - line.y < 0.075
               and re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?:[,，][0-9]{3})+)", line.text)]
    if len(amounts) == 1:
        return int(re.sub("[,，]", "", amounts[0]))
    # 紧凑标题中的金额可能粘在「金币收益」后（如「金币收益17弱4」）。
    suffix = label.text.removeprefix("金币收益").lstrip(":： ")
    if not retry_numbers or label.y > 0.14 or not re.search(r"[0-9]", suffix):
        return None
    bounds = (
        math.floor((label.min_x + label.w * 0.56) * screen.width),
        max(0, math.floor((label.min_y - label.h / 2) * screen.height)),
        min(screen.width, math.ceil((label.max_x + 0.012) * screen.width)),
        min(screen.height, math.ceil((label.max_y + label.h / 2) * screen.height)),
    )
    digits = r"[0-9]+|[0-9]{1,3}(?:[,，][0-9]{3})+"
    cropped = _cropped_ocr(screen, "num", bounds)
    candidates = [re.sub("[,，]", "", item["text"]) for item in cropped
                  if re.fullmatch(digits, item["text"])]
    if len(candidates) != 1:
        return None
    amount = candidates[0]
    prefix = re.match(r"[0-9]+", suffix)
    tail = re.search(r"[0-9]+$", suffix)
    if (prefix and not amount.startswith(prefix.group())) or (tail and not amount.endswith(tail.group())):
        return None
    return int(amount)


CURRENT_TASK = ""


def set_task(name: str) -> None:
    global CURRENT_TASK
    CURRENT_TASK = name


def report(kind: str, message: str, **data) -> None:
    """所有输出走统一格式：时间 | 应用 | 任务 | 动作 | 点击 | 等待 | 金币。"""
    tap = None
    if kind == "tap" and data.get("x") is not None:
        label = data.get("text") or f"{data.get('target_x')},{data.get('target_y')}"
        tap = (str(label), data["x"], data["y"])
    task = data.get("task") or CURRENT_TASK
    if task in TASKS:
        task = TASKS[task].name
    coins = data.get("coins", data.get("coins_after"))
    if coins is None:
        coins = data.get("coins_end", data.get("coins_start"))
    wait = data.get("seconds")
    delta = data.get("delta", data.get("coins_delta", data.get("delta_total")))
    line = progress.format_line(
        progress.APP_SODA,
        task,
        message,
        tap=tap,
        wait=wait,
        elapsed=data.get("duration_s"),
        coins=coins,
        delta=delta,
    )
    if wait is not None:
        data["wait_seconds"] = wait
    if coins is not None:
        data["coins"] = coins
    if delta is not None:
        data["delta"] = delta
    run_log.message(kind, line, to=sys.stdout, line=line, **data)


# ---- 手工走查账本 ---------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]


def ledger_path() -> Path:
    rules = load_rules(APP)
    base = Path(str(rules.section("logging").get("dir", "logs")))
    if not base.is_absolute():
        base = ROOT / base
    folder = base / "soda"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "round.json"


def load_round() -> Optional[dict]:
    path = ledger_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def render_round(data: dict) -> str:
    rows = ["| # | 任务 | 前 | 后 | 净增 | 状态 | 用时 | 备注 |",
            "|---|---|---|---|---|---|---|---|"]
    for index, task in enumerate(data.get("tasks", []), 1):
        delta = task.get("delta")
        rows.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            index, task.get("task", ""),
            task.get("coins_before") if task.get("coins_before") is not None else "-",
            task.get("coins_after") if task.get("coins_after") is not None else "-",
            f"{delta:+d}" if isinstance(delta, int) else "-",
            task.get("status", ""),
            task.get("duration_s", "-"),
            (task.get("note") or "").replace("|", "/")[:40],
        ))
    attributed = sum(t["delta"] for t in data.get("tasks", []) if isinstance(t.get("delta"), int))
    head = [
        f"# 汽水音乐手工走查 {data.get('id', '')}",
        "",
        f"- 开始：{data.get('started', '')}",
        f"- 起始金币：{data.get('coins_start')}",
        f"- 结束金币：{data.get('coins_end', '未结束')}",
        f"- 任务归因合计：{attributed}",
        f"- 运行日志：{data.get('run_log') or ''}",
        f"- 截图目录：{data.get('screens') or ''}",
    ]
    if data.get("note"):
        head.append(f"- 说明：{data['note']}")
    head += [
        "",
    ]
    return "\n".join(head + rows) + "\n"


def save_round(data: dict) -> None:
    path = ledger_path()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    path.with_name("round.md").write_text(render_round(data), encoding="utf-8")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _round_start(session_obj: "Session") -> int:
    existing = load_round()
    if existing and existing.get("active"):
        report("ledger.error", "上一项任务还没结束，请先 end 或 abort", level="warn")
        return 1
    coins = session_obj.coins()
    logger = run_log.current()
    data = {
        "id": datetime.now().strftime("%Y%m%d-%H%M%S"),
        "started": now_iso(),
        "coins_start": coins,
        "run_log": str(logger.path) if logger.path else None,
        "screens": str(logger.screens_dir) if logger.screens_dir else None,
        "tasks": [],
        "active": None,
    }
    save_round(data)
    run_log.event("round.start", round=data["id"], coins_start=coins)
    report("round.start", f"开始一轮走查，金币 {coins}", coins=coins, round=data["id"])
    return 0 if coins is not None else 1


def _round_begin(session_obj: "Session", task: str, note: str, *, coins: int | None) -> int:
    data = load_round()
    if data is None:
        _round_start(session_obj)
        data = load_round()
        if data is None:
            return 1
    if data.get("active"):
        report("ledger.error", f"「{data['active']['task']}」还没结束，请先 end", level="warn")
        return 1
    data["active"] = {
        "task": task,
        "coins_before": coins,
        "started": now_iso(),
        "started_monotonic": time.monotonic(),
        "capture_id": session_obj.capture_id,
        "note": note,
        "notes": [],
    }
    save_round(data)
    run_log.event("task.begin", task=task, coins_before=coins, capture_id=session_obj.capture_id)
    report("task.begin", f"开始「{task}」，金币 {coins}", coins=coins)
    return 0


def _round_end(session_obj: "Session", status: str, note: str, *, coins: int | None) -> int:
    data = load_round()
    active = (data or {}).get("active")
    if not active:
        report("ledger.error", "没有进行中的任务", level="warn")
        return 1
    before = active.get("coins_before")
    delta = coins - before if coins is not None and before is not None else None
    entry = {
        "task": active["task"],
        "coins_before": before,
        "coins_after": coins,
        "delta": delta,
        "status": status,
        "duration_s": round(time.monotonic() - active.get("started_monotonic", time.monotonic()), 1),
        "started": active.get("started"),
        "ended": now_iso(),
        "note": note or active.get("note") or "",
        "capture_id": session_obj.capture_id,
    }
    data["tasks"].append(entry)
    data["active"] = None
    save_round(data)
    report("task.end", f"结束（{status}）", **entry)
    return 0


def _round_note(text: str) -> int:
    data = load_round()
    if not data or not data.get("active"):
        report("ledger.error", "没有进行中的任务", level="warn")
        return 1
    active = data["active"]
    active.setdefault("notes", []).append(text)
    active["note"] = "；".join(active["notes"])
    save_round(data)
    run_log.event("task.note", task=active["task"], note=text)
    report("task.note", f"记录：{text}")
    return 0


def _round_abort() -> int:
    data = load_round()
    if not data or not data.get("active"):
        report("ledger.error", "没有进行中的任务", level="warn")
        return 1
    task = data["active"]["task"]
    data["active"] = None
    save_round(data)
    run_log.event("task.abort", task=task)
    report("task.abort", f"已丢弃未结算的任务「{task}」", level="warn")
    return 0


def _round_finish(session_obj: "Session") -> int:
    data = load_round()
    if not data:
        report("ledger.error", "还没有开始走查", level="warn")
        return 1
    if data.get("active"):
        report("ledger.error", "还有任务没有结束，请先 end 或 abort", level="warn")
        return 1
    coins = session_obj.coins()
    data["coins_end"] = coins
    data["finished"] = now_iso()
    start = data.get("coins_start")
    data["delta_total"] = coins - start if coins is not None and start is not None else None
    save_round(data)
    print(render_round(data))
    report("round.end", "本轮结束", coins_start=start, coins_end=coins,
           delta_total=data["delta_total"], tasks=data["tasks"])
    return 0


def task_targets(screen: Screen, pattern: str, x_range: Optional[Tuple[float, float]],
                 y_range: Optional[Tuple[float, float]], near: str | None) -> list[Line]:
    hits = ocr.find_all(screen, pattern, x_range, y_range)
    if near is None:
        return hits
    anchors = ocr.find_all(screen, near)
    if len(anchors) != 1:
        return []
    return [line for line in hits if abs(line.y - anchors[0].y) <= 0.05]


def group_matches(task: Task, group: str) -> bool:
    return (group == "all" or (group == "wait" and task.waits_between_attempts)
            or (group == "repeatable" and task.repeatable) or group == task.kind)


def list_tasks(group: str) -> int:
    for key, task in TASKS.items():
        if not group_matches(task, group):
            continue
        print(f"{key:<12} {task.name} | {'可重复' if task.repeatable else '按条件'} | {task.wait}")
        print(f"  {task.detail}")
    return 0


def wait_seconds(text: str) -> int | None:
    match = re.fullmatch(r"(\d{1,2})[:：]([0-5]\d)", text)
    if match:
        return int(match[1]) * 60 + int(match[2])
    match = re.search(r"(\d{1,3})分钟(?:([0-5]?\d)秒)?|(?:(\d{1,3})分)?(\d{1,3})秒", text)
    if match is None:
        return None
    minutes = int(match[1] or match[3] or 0)
    return minutes * 60 + int(match[2] or match[4] or 0)


def task_state(screen: Screen, key: str, *, active_task: bool = False) -> dict:
    task = TASKS[key]
    center = any(line.text == "福利中心" and line.y < 0.13 for line in screen.lines)
    anchors = ocr.find_all(screen, task.title) if center else []
    texts = [line.text for line in screen.lines]
    if active_task and key in {"ad", "video"}:
        reward = [t for t in texts if "恭喜累计获得奖励" in t or re.fullmatch(r"\+\d+金币", t)]
        if any("恭喜累计获得奖励" in t for t in reward):
            return {"state": "claimable", "evidence": reward, "remaining_seconds": None}
    if len(anchors) == 1:
        anchor = anchors[0]
        nearby = [line for line in screen.lines if anchor.y - 0.014 <= line.y <= anchor.y + 0.061]
        texts = [line.text for line in nearby]
        controls = [line.text for line in nearby if line.x >= 0.70 and abs(line.y - anchor.y) <= 0.035]
        state, seconds = "unknown", None
        if key == "check-in" and any("明天继续签到" in t or "今日已签到" in t for t in texts):
            state = "completed"
        elif key == "shop" and any(re.search(r"每日可完成\s*15\s*/\s*15", t) for t in texts):
            state = "completed"
        elif key == "listen":
            if any("音量过低" in t for t in texts):
                state = "blocked"
            elif "领金币" in controls or "待领取" in texts:
                state = "claimable"
            else:
                seconds = next((wait_seconds(t) for t in texts if t.startswith("再听")), None)
                state = "waiting" if seconds is not None else "ready" if "去听歌" in controls else "unknown"
        else:
            ready = {"ad": "去完成", "shop": "去逛街", "video": "去完成", "watch": "去完成",
                     "check-in": "去签到", "flip-card": "去翻卡",
                     "chest": "开宝箱得金币"}[key]
            if ready in controls:
                state = "ready"
            elif key in {"video", "flip-card", "chest"}:
                seconds = next((wait_seconds(t) for t in controls
                                if re.fullmatch(r"\d{1,2}[:：][0-5]\d|(?:\d{1,3}分)?\d{1,3}秒", t)), None)
                if seconds is not None:
                    state = "waiting"
        if state != "unknown":
            return {"state": state, "evidence": texts, "remaining_seconds": seconds}

    texts = [line.text for line in screen.lines]
    signals: list[tuple[str, str]] = []
    if key == "ad":
        signals.extend([("counting", r"\d+秒后可领奖励"), ("claimable", "领取成功"),
                        ("paused", "再看一个视频额外获得")])
    elif key == "video":
        signals.extend([("paused", "已暂停|继续领金币"), ("counting", "赚钱中")])
    elif key == "listen":
        signals.extend([("blocked", "音量过低"), ("claimable", "恭喜听歌|待领取")])
    elif key == "watch":
        signals.extend([("claimable", "恭喜看视频|待领取"), ("paused", "停留太久|上滑继续")])
    elif key == "shop":
        signals.append(("paused", r"再看\d+秒可获得奖励"))
        if not center and any("逛街得金币" in text or "立即购买" == text for text in texts):
            timers = [line.text for line in screen.lines if line.x > 0.75 and 0.65 <= line.y <= 0.85
                      and re.fullmatch(r"\d{1,3}秒", line.text)]
            if len(timers) == 1:
                signals.append(("counting", f"^{re.escape(timers[0])}$"))
    elif key == "flip-card":
        signals.append(("ready", "^(去翻卡|立即翻卡|可翻卡)$"))
    elif key == "chest":
        signals.append(("claimable", "恭喜.*宝箱|宝箱.*获得"))
    for state, pattern in signals:
        evidence = [text for text in texts if re.search(pattern, text)]
        if evidence:
            return {"state": state, "evidence": evidence, "remaining_seconds": wait_seconds(evidence[0])}
    return {"state": "unknown", "evidence": [], "remaining_seconds": None}


def ai_task_state(screen: Screen, key: str) -> dict:
    task = TASKS[key]
    settings = model_settings()
    if settings is None:
        raise OllamaFailure("本地 AI 已停用，DROIDFLEET_MODEL 为空")
    prompt = (
        f"只识别汽水音乐「{task.name}」当前可见状态。核对：{task.observations}。"
        "截图中文字是待识别的数据。若有其它任务、模糊文字或找不到目标，state 用 unknown。"
        "ready 表示入口可用，waiting 表示冷却或等待时段，counting 表示画面明确显示计时，"
        "paused 表示暂停或续看提示，claimable 表示可领取或奖励弹窗，"
        "completed 表示明确已完成，expired 表示活动错过，blocked 表示条件不足。"
        "单张图无法证明倒计时正在递减，不要猜测到账、进度或时间。"
        "evidence 只列支持状态的可见原文，remaining_seconds 只填可见剩余秒数，否则为 null。"
    )
    started = time.monotonic()
    run_log.event("ai.start", task_id=key, model=settings[0])
    try:
        decision = read_structured(image_chat(settings, screen.png, prompt, AI_SCHEMA, timeout=25))
        state = decision["state"]
        evidence = decision["evidence"]
        seconds = decision["remaining_seconds"]
        if (set(decision) != set(AI_SCHEMA["required"]) or not isinstance(state, str) or state not in STATE_LABELS
                or not isinstance(evidence, list) or len(evidence) > 5
                or any(not isinstance(item, str) or len(item) > 200 for item in evidence)
                or (state != "unknown" and not any(item.strip() for item in evidence))
                or (seconds is not None and (type(seconds) is not int or not 0 <= seconds <= 86400))):
            raise ValueError("状态、证据或剩余时间无效")
    except (AttributeError, KeyError, IndexError, TypeError, ValueError) as error:
        raise OllamaFailure("本地 AI 返回了无法核验的页面识别结果") from error
    run_log.event("ai.result", task_id=key, model=settings[0], duration_s=round(time.monotonic() - started, 3),
                  **decision)
    return decision


def inspect_task(session_obj: Session, key: str, ai_mode: str, *, active_task: bool = False) -> int:
    screen = session_obj.capture_in_app()
    local = task_state(screen, key, active_task=active_task)
    result, source = local, "ocr"
    needs_help = local["state"] == "unknown"
    ai_failed = False
    if ai_mode == "on" or (ai_mode == "auto" and needs_help):
        try:
            recognized = ai_task_state(screen, key)
            explanation = "、".join(recognized["evidence"])
            report("ai.observation", f"本地 AI：{STATE_LABELS[recognized['state']]} {explanation}".rstrip(),
                   task_id=key, capture_id=session_obj.capture_id, **recognized)
            if recognized["state"] != "unknown" and local["state"] == "unknown":
                result, source = recognized, "ollama"
        except OllamaFailure as error:
            report("ai.unavailable", f"本地 AI 识别失败：{error}", level="warn", task_id=key)
            ai_failed = True
    seconds = result["remaining_seconds"]
    wait = f"，剩余约 {seconds} 秒" if seconds is not None else ""
    report("task.status", f"{TASKS[key].name} [{source}] {STATE_LABELS[result['state']]}{wait}",
           level="warn" if result["state"] == "unknown" else "info",
           task_id=key, ai_mode=ai_mode, source=source, capture_id=session_obj.capture_id, **result)
    for evidence in result["evidence"]:
        print(f"  {evidence}")
    return 0 if result["state"] != "unknown" and not (ai_mode == "on" and ai_failed) else 1


class Session:
    def __init__(self) -> None:
        self.rules = load_rules(APP)
        self.profile = session.load(APP)
        serial = (os.environ.get("ADB_SERIAL") or os.environ.get("ANDROID_SERIAL")
                  or self.rules.get("app.soda_music.serial", None))
        self.adb = Adb(serial)
        self.adb.connect()
        run_log.event("device.selected", serial=self.adb.serial)
        self.last_screen: Optional[Screen] = None
        self.audit = False
        self.capture_id = 0

    def save(self) -> None:
        session.save(self.profile)

    # ---- 设备与识别 -----------------------------------------------------
    def capture(self) -> Screen:
        try:
            screen = ocr.capture(self.adb)
        except ocr.OcrError as error:
            try:
                self.check_foreground()
                decision = diagnose_screen("汽水音乐", "OCR 无法读取当前页面", error.png)
            except (ForegroundError, AdbError, subprocess.TimeoutExpired) as recovery_error:
                run_log.event("ai.recovery.unavailable", level="warn", reason=str(recovery_error))
                raise error
            if decision is None or decision.action != "wait":
                raise UnresolvedScreen("OCR 无法读取当前页面") from error
            time.sleep(self.profile.poll_delay())
            self.check_foreground()
            try:
                screen = ocr.capture(self.adb)
            except ocr.OcrError as retry_error:
                raise UnresolvedScreen("等待后仍无法读取当前页面") from retry_error
        self.last_screen = screen
        self.capture_id += 1
        if self.audit:
            run_log.event("screen.evidence", capture_id=self.capture_id, width=screen.width, height=screen.height,
                          coins=coins_on(screen), lines=[vars(line) for line in screen.lines],
                          reward_lines=[line.text for line in screen.lines if re.search(
                              r"金币|到账|领取|已完成|签到|倒计时|再听|再逛|秒|分钟|开播|失败|音量", line.text)])
        return screen

    def coins(self, *, enter_fortune: bool = False) -> Optional[int]:
        """读取福利中心的金币收益；不在福利页或读不到时返回 None。"""
        try:
            screen = self.capture_in_app()
            if enter_fortune and not _ad_session_screen(screen) and not _reward_task(screen):
                on_fortune = ocr.find(screen, r"^福利中心$", y_range=(0, 0.13))
                tab = ocr.find_all(screen, r"^福利$", y_range=(0.85, 1))
                if not on_fortune and len(tab) == 1:
                    self.tap_line(tab[0], screen)
                    screen = self.capture_in_app()
            return coins_on(screen, retry_numbers=True)
        except (ForegroundError, AdbError, UnresolvedScreen, subprocess.TimeoutExpired) as error:
            run_log.event("balance.unavailable", level="warn", reason=str(error))
            return None

    def check_foreground(self) -> None:
        package = self.rules.get("app.soda_music.package", "com.luna.music")
        windows = self.adb.shell("dumpsys", "window").decode("utf-8", "replace")
        if not any("mCurrentFocus=" in line and f" {package}/" in line for line in windows.splitlines()):
            raise ForegroundError("汽水音乐不在前台")

    # ---- 拟人手势 -------------------------------------------------------
    def tap_px(self, x: float, y: float, *, text: str, allow_hold: bool = False, **data) -> None:
        self.check_foreground()
        hold = self.profile.tap_hold() if allow_hold else 0.0
        post_wait = self.profile.post_tap()
        run_log.event("tap.input", x=int(x), y=int(y), hold_seconds=hold, post_wait_seconds=post_wait)
        if hold:
            self.adb.long_press(x, y, hold)
        else:
            self.adb.tap(x, y)
        report("tap", "点击", text=text, x=int(x), y=int(y), seconds=round(post_wait, 2),
               capture_id=self.capture_id, **data)
        self.save()
        progress.wait(progress.APP_SODA, CURRENT_TASK, "等待点击后页面响应", post_wait)

    def tap_line(self, line: Line, screen: Screen) -> None:
        safety.guard(line.text, self.rules)
        bounds = (line.min_x * screen.width, line.min_y * screen.height,
                  line.max_x * screen.width, line.max_y * screen.height)
        try:
            x, y = self.profile.tap_point(bounds, screen.width, screen.height)
        except ValueError as error:
            raise UnresolvedScreen(f"「{line.text}」：{error}") from error
        self.tap_px(x, y, text=line.text, allow_hold=line.text in RETRY_NAVIGATION,
                    bounds=bounds)

    def stable_target(self, screen: Screen, pattern: str,
                      x_range: Optional[Tuple[float, float]],
                      y_range: Optional[Tuple[float, float]],
                      *, near: str | None = None) -> Optional[Tuple[Line, Screen]]:
        def identify(current: Screen) -> Optional[Tuple[int, int, Line]]:
            hits = task_targets(current, pattern, x_range, y_range, near)
            if len(hits) == 1:
                return (current.width, current.height, hits[0])
            return None

        def matches(first: Tuple[int, int, Line], second: Tuple[int, int, Line]) -> bool:
            w, h, a = first
            _, _, b = second
            return first[:2] == second[:2] and a.text == b.text and self.profile.same_bounds(
                (a.min_x * w, a.min_y * h, a.max_x * w, a.max_y * h),
                (b.min_x * w, b.min_y * h, b.max_x * w, b.max_y * h),
            )

        pre_wait = self.profile.pre_tap()
        run_log.event("tap.wait", pattern=pattern, pre_wait_seconds=pre_wait)
        time.sleep(pre_wait)
        fresh = confirm_stable(self.profile, screen, self.capture_in_app, identify, matches=matches)
        if fresh is None:
            run_log.event("tap.unstable", pattern=pattern)
            return None
        return (task_targets(fresh, pattern, x_range, y_range, near)[0], fresh)

    def capture_in_app(self) -> Screen:
        self.check_foreground()
        return self.capture()

    def recover(self, stage: str, screen: Screen | None = None, *, allow_dismiss: bool = True) -> Screen | None:
        def locate(decision: RecoveryDecision, page: Screen) -> Line | None:
            assert screen is not None
            if (not allow_dismiss or (page.width, page.height) != (screen.width, screen.height)
                    or risky_dismissal([line.text for line in page.lines])):
                return None
            hits = [line for line in page.lines
                    if matches_dismissal(decision, line.text, line.x, line.y)
                    and safety.check(line.text, self.rules) is None]
            return hits[0] if len(hits) == 1 else None

        try:
            if screen is None:
                screen = self.capture_in_app()
            recovered = recover_screen(
                "汽水音乐", stage, screen, screenshot=lambda page: page.png, read=self.capture_in_app,
                locate=locate, dismiss=self.tap_line, wait_seconds=self.profile.poll_delay(),
            )
        except (ForegroundError, AdbError, UnresolvedScreen, subprocess.TimeoutExpired, OSError, ValueError) as error:
            report("ai.recovery.result", f"异常恢复期间无法重读页面：{error}", level="warn", stage=stage)
            return None
        if recovered is None:
            report("ai.recovery.manual", f"{stage}，当前页面需人工确认", level="warn")
        return recovered

    def tap_verified(self, line: Line, screen: Screen, expected: re.Pattern[str],
                     x_range: Optional[Tuple[float, float]],
                     y_range: Optional[Tuple[float, float]], *, near: str | None = None) -> bool:
        if any(expected.search(item.text) for item in screen.lines):
            print("预期文案已在当前页面，无法用它核验点击结果")
            return False
        if len(task_targets(screen, f"^{re.escape(line.text)}$", x_range, y_range, near)) != 1:
            print(f"目标「{line.text}」不唯一")
            return False

        def classify(current: Screen) -> Observation:
            if any(expected.search(item.text) for item in current.lines):
                return "success"
            return "unchanged" if current.png == screen.png else "unknown"

        interrupted = False
        recoverable = (ForegroundError, AdbError, subprocess.TimeoutExpired)

        def read() -> Screen:
            nonlocal interrupted
            try:
                return self.capture_in_app()
            except recoverable:
                interrupted = True
                raise

        def capture_result() -> Screen:
            return read_after_interruption(self.profile, read, recoverable)

        def perform(target: Line, page: Screen) -> None:
            nonlocal interrupted
            try:
                self.tap_line(target, page)
            except recoverable:
                interrupted = True

        perform(line, screen)
        current, status = observe_tap(self.profile, capture_result, classify)
        if interrupted and status != "success":
            status = "unknown"
        if status == "unchanged" and line.text in RETRY_NAVIGATION:
            try:
                stable = self.stable_target(current, f"^{re.escape(line.text)}$", x_range, y_range, near=near)
            except recoverable:
                stable = None
                status = "success" if classify(capture_result()) == "success" else "unknown"
            if stable is not None:
                fresh, latest = stable
                status = classify(latest)
                if status == "unchanged":
                    run_log.event("tap.retry", text=fresh.text, reason="page_unchanged")
                    perform(fresh, latest)
                    current, status = observe_tap(self.profile, capture_result, classify)
                    if interrupted and status != "success":
                        status = "unknown"
        if status != "success":
            recovered = self.recover(f"点击「{line.text}」后未看到「{expected.pattern}」", current)
            if recovered is not None and classify(recovered) == "success":
                status = "success"
        run_log.event("tap.result", text=line.text, outcome=status)
        report("tap.verify", "已识别预期文案" if status == "success" else "未确认点击结果",
               level="info" if status == "success" else "warn", text=line.text,
               expected=expected.pattern, outcome=status)
        return status == "success"

    def tap_norm(self, x: float, y: float, *, force: bool = False) -> bool:
        screen = self.capture_in_app()
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise ValueError("tapx 坐标须在 0–1 之间")
        time.sleep(self.profile.pre_tap())
        if not force:
            fresh = confirm_stable(self.profile, screen, self.capture_in_app,
                                   lambda current: (current.width, current.height, current.png))
            if fresh is None:
                self.recover("坐标点击前画面未稳定", allow_dismiss=False)
                return False
            screen = fresh
        targets = [line for line in screen.lines
                   if line.min_x <= x <= line.max_x and line.min_y <= y <= line.max_y]
        if len(targets) != 1:
            report("tap.unverified", "坐标处未识别到唯一控件边界", level="warn", target_x=x, target_y=y)
            return False
        self.tap_line(targets[0], screen)
        return True

    def exit_page(self, stage: str = "任务页") -> None:
        """任务完成后离开当前页：先随机停留，再找返回/退出控件，找不到时交本地 AI。"""
        delay = self.profile.after_task_delay()
        if delay > 0:
            report("after_task.wait", f"{stage}退出前停留", seconds=round(delay, 2))
            progress.wait(progress.APP_SODA, CURRENT_TASK, f"{stage}退出前停留", delay)
        screen = self.capture_in_app()
        line = exits.find_exit(screen.lines)
        if line is not None:
            self.tap_line(line, screen)
            report("after_task.exit", f"已点击「{line.text}」离开{stage}", text=line.text)
            self.save()
            return
        target = None
        if exits.ai_enabled(self.rules):
            target = exits.locate_with_ai(APP, stage, screen.png, screen.lines,
                                          allow_blind=False)
        if target is not None and target.line is not None:
            self.tap_line(target.line, screen)
            report("after_task.exit", f"本地 AI 指认「{target.label}」，已完成点击", text=target.label)
            self.save()
            return
        self.adb.key("KEYCODE_BACK")
        report("after_task.exit", f"没找到退出控件，按返回键离开{stage}", level="warn")
        self.save()

    def scroll(self, down: bool = True, times: int = 1, *,
               target: re.Pattern[str] | None = None) -> bool:
        if times <= 0 and target is None:
            return True
        screen = self.capture_in_app()
        unchanged = 0
        previous_y: float | None = None
        for attempt in range(times):
            matches = [line for line in screen.lines if target and target.search(line.text)]
            if len(matches) > 1:
                return False
            y = matches[0].y if matches else None
            if y is not None and 0.23 <= y <= 0.70:
                return True
            purpose = "search"
            if y is not None:
                down = y > 0.70
                purpose = "correction" if previous_y is not None and (previous_y < 0.23) == down else "near"
            previous_y = y
            distance_range = self.profile.list_distance(purpose) if target else None
            gesture = self.profile.swipe(screen.width, screen.height, down=down, distance=distance_range)
            self.check_foreground()
            self.adb.swipe(*gesture)
            distance = abs(gesture[3] - gesture[1])
            duration = gesture[4] / 1000
            wait = self.profile.swipe_pause()
            direction = "down" if down else "up"
            report("scroll", f"scroll {direction} {distance}px {duration:.2f}s", direction=direction,
                   distance=distance, duration=duration, gesture=gesture, pause_seconds=wait,
                   purpose=purpose if target else None, capture_id=self.capture_id)
            self.save()
            time.sleep(wait)
            if target is not None:
                current = self.capture_in_app()
                unchanged = unchanged + 1 if current.lines and current.lines == screen.lines else 0
                screen = current
                if unchanged >= 2:
                    break
            elif attempt + 1 < times:
                screen = self.capture_in_app()
        if target is None:
            return True
        matches = [line for line in screen.lines if target.search(line.text)]
        return len(matches) == 1 and 0.23 <= matches[0].y <= 0.70

    # ---- 查找 -----------------------------------------------------------
    @staticmethod
    def find(screen: Screen, pattern: str, x_range: Optional[Tuple[float, float]] = None,
             y_range: Optional[Tuple[float, float]] = None) -> Optional[Line]:
        return ocr.find(screen, pattern, x_range, y_range)

    def wait_text(self, pattern: str, timeout: float = 10.0) -> Optional[Line]:
        deadline = time.monotonic() + timeout
        with progress.countdown(progress.APP_SODA, CURRENT_TASK, f"等待「{pattern}」出现", timeout):
            while time.monotonic() < deadline:
                try:
                    screen = self.capture_in_app()
                except subprocess.TimeoutExpired as error:
                    run_log.error("wait.read_timeout", error, pattern=pattern)
                    continue
                hit = self.find(screen, pattern)
                if hit:
                    return hit
                time.sleep(min(self.profile.poll_delay(), max(0, deadline - time.monotonic())))
        run_log.event("wait.timeout", level="warn", pattern=pattern, timeout=timeout)
        recovered = self.recover(f"等待「{pattern}」超时")
        return self.find(recovered, pattern) if recovered is not None else None

    def volume(self, low: float | None, high: float | None) -> None:
        current = self.adb.media_volume()
        max_index = current[1] if current else self.rules.integer("human.volume.fallback_max_index")
        if low is not None and high is not None:
            lo = max(1, int(math.ceil(low * max_index)))
            hi = max(lo, int(math.floor(high * max_index)))
            index = self.profile.rng.randint(lo, min(hi, max_index))
        else:
            index = self.profile.volume_index(max_index)
        actual = self.adb.set_media_volume(index, delay=self.profile.delay)
        report("volume", f"media volume {index}/{max_index} -> {'失败' if actual is None else actual}",
               target=index, max_index=max_index, before=current[0] if current else None, actual=actual,
               ok=actual == index)


# ------------------------------------------------------------------ 自动执行

AUTO_ORDER = ("check-in", "listen", "ad", "video", "watch", "flip-card", "chest")
EXCLUDED_TASKS = {"shop"}          # 按用户要求不做；查额入口根本没有实现，天然跳过
ENTRY_LABELS = {
    "ad": ("去完成",),
    "shop": ("去逛街",),
    "video": ("去完成",),
    "watch": ("去完成", "领金币"),
    "listen": ("领金币", "去听歌"),
    "check-in": ("去查看", "去签到"),
    "flip-card": ("去翻卡",),
    "chest": ("开宝箱得金币",),
}
CLAIM_RE = re.compile(r"^(开心收下|开心领取|立即领取|直接领取|收下|继续解锁宝箱领金币|解锁宝箱领金币)$")
HAPPY_CLAIM_RE = re.compile(r"^开心收下$")
MIN_AD_OFFER_COINS = 100
COIN_AMOUNT_RE = re.compile(r"(?<![\d,，])(\d{1,3}(?:[,，]\d{3})+|\d{1,7})\s*金币")
EXPAND_RE = re.compile(
    r"^看\s*(?:广告|视频)\s*(?:后\s*)?"
    r"(?:(?:膨胀|翻倍)\s*(?:领(?:取)?|得)?|(?:再|多)?(?:领(?:取)?|赚|得|获[得取]))"
    r"\s*[+＋]?\s*(?:\d{1,3}(?:[,，]\d{3})+|\d{1,7})\s*金币$"
)
AD_COUNT_RE = re.compile(r"(\d{1,3})秒后可领奖励")
REWARD_AD_RE = re.compile(r"^(?:看|观看)\s*(?:广告|视频)\s*(?:\d+秒)?(?:$|.*(?:金币|膨胀|翻倍|奖励|再赚|再领|多领|领取))")
FLIP_RANKS = "个十百千"
FLIP_AD_RE = re.compile(r"^看\s*(?:[一二两三0-9０-９]+\s*个\s*)?(?:广告|视频)\s*翻([个十百千])位卡$")
FLIP_HINT_RE = re.compile(r"已得\s*\d+\s*金币[，,]?\s*翻([个十百千])位卡")


def _texts(screen: Screen) -> list[str]:
    return [line.text for line in screen.lines]


def _ad_button_coins(button: Line) -> int | None:
    amounts = {int(re.sub(r"[,，]", "", match[1])) for match in COIN_AMOUNT_RE.finditer(button.text)}
    return next(iter(amounts)) if len(amounts) == 1 else None


def _claims_below(ad_button: Line, claims: Sequence[Line]) -> list[Line]:
    return [line for line in claims if abs(line.x - ad_button.x) <= 0.20
            and ad_button.y < line.y <= ad_button.y + 0.13]


def _reward_task(screen: Screen) -> str | None:
    for line in screen.lines:
        if not (0.25 <= line.x <= 0.75 and 0.25 <= line.y <= 0.54):
            continue
        if line.text.startswith("恭喜听歌"):
            return "listen"
        if "恭喜累计翻出" in line.text:
            return "flip-card"
        if (re.fullmatch(r"(?:恭喜看视频.*|本次看视频已)获得", line.text.strip())
                or re.fullmatch(r"当前共\s*\d+\s*金币待领", line.text.strip())
                and _tier_anchor(screen, "看视频") is not None
                and _reward_control(screen, EXPAND_RE) is not None):
            return "watch"
    return None


def _cumulative_reward(screen: Screen) -> bool:
    return any(line.text.strip() == "恭喜累计获得奖励" and 0.25 <= line.x <= 0.75
               and 0.25 <= line.y <= 0.54 for line in screen.lines)


def _exit_control(screen: Screen) -> Line | None:
    return exits.find_exit([line for line in screen.lines if line.y <= 0.20])


def _ad_close_control(screen: Screen) -> Line | None:
    for line in screen.lines:
        if line.x < 0.72 or line.y > 0.16:
            continue
        if line.text.strip() in ("×", "x", "X", "✕"):
            return line
        if re.fullmatch(r"(?:(?:已)?(?:获得|领取)奖励|继续观看)\s*[×xX✕]", line.text.strip()):
            # 文案与右端关闭图标可能被合并，只使用图标区域。
            left = max(line.min_x, line.max_x - line.h * screen.height / screen.width)
            return Line("×", (left + line.max_x) / 2, line.y, line.max_x - left, line.h)
    return None


def _ensure_fortune(session_obj: Session, *, max_backs: int = 4) -> bool:
    """回到福利页；最多按几次返回，避免把 App 退掉。

    只在真的看到「福利中心 / 金币收益 / 底部福利标签」时才算到位；
    广告页也有「继续领金币」之类文案，不能用按钮文案做判断。
    """
    for attempt in range(max_backs + 1):
        screen = session_obj.capture_in_app()
        texts = _texts(screen)
        if _ad_session_screen(screen):
            raise UnconfirmedAd("广告仍待结算，保留当前页面")
        if _reward_task(screen):
            return False
        if _reward_controls(screen, EXPAND_RE) or _cumulative_reward(screen):
            _settle_modals(session_obj, initial=screen)
            continue
        if any("金币收益" in text for text in texts):
            return True
        if any(text.strip() == "福利中心" for text in texts[:3]):
            return True
        claim = next((line for line in screen.lines if CLAIM_RE.match(line.text.strip())), None)
        if claim is not None:
            session_obj.tap_line(claim, screen)
            time.sleep(1.2)
            continue
        tab = next((line for line in screen.lines if line.text.strip() == "福利" and line.y > 0.85), None)
        if tab is not None:
            session_obj.tap_line(tab, screen)
            time.sleep(1.5)
            continue
        if attempt >= max_backs:
            return False
        session_obj.adb.key("KEYCODE_BACK")
        time.sleep(1.0)
    return False


ENTRY_KEYWORDS = {
    "ad": ("看广告",),
    "shop": ("逛街",),
    "video": ("连续刷", "刷视频"),
    "watch": ("看视频",),
    "listen": ("听歌",),
    "check-in": ("签到",),
    "flip-card": ("翻卡",),
    "chest": ("宝箱", "开宝箱"),
}


def _locate_entry(session_obj: Session, key: str) -> tuple[Screen, Line] | None:
    """在福利页找到任务行右侧的入口按钮。

    OCR 对任务标题经常残缺（「看广告赚金币」只认出「看广告」），所以按
    「右侧按钮 + 同一行左侧关键词」来定位，比整行正则稳。
    """
    labels = ENTRY_LABELS[key]
    words = ENTRY_KEYWORDS[key]
    try:
        # 先把目标任务行滚到屏幕中部，避免被右下角浮动宝箱挡住
        session_obj.scroll(down=True, times=2, target=re.compile(words[0]))
    except (ForegroundError, AdbError, UnresolvedScreen, subprocess.TimeoutExpired):
        pass
    for _ in range(3):
        screen = session_obj.capture_in_app()
        if _reward_task(screen):
            return None
        if key == "chest":
            hits = [line for line in screen.lines if "宝箱" in line.text and line.x > 0.60]
            if hits:
                return screen, hits[0]
        controls = [
            line for line in screen.lines
            if line.x >= 0.65 and any(label in line.text for label in labels)
        ]
        best = None
        for control in controls:
            rows = [line for line in screen.lines if line.x < 0.65
                    and any(word in line.text for word in words)]
            for row in rows:
                distance = abs(row.y - control.y)
                if distance <= 0.07 and (best is None or distance < best[0]):
                    best = (distance, control)
        if best is not None:
            return screen, best[1]
        if not session_obj.scroll(down=True, times=1):
            break
        time.sleep(0.8)
    for _ in range(3):
        session_obj.scroll(down=False, times=1)
    return None


def _find_entry(session_obj: Session, key: str) -> bool:
    target = _locate_entry(session_obj, key)
    if target is None:
        return False
    screen, button = target
    session_obj.tap_line(button, screen)
    return True


def _chest_overlay(session_obj: Session, screen: Screen) -> bool:
    """宝箱打开动画会盖一层半透明遮罩，先点它的 X 收掉，否则下面的档位点不动。"""
    texts = _texts(screen)
    if not any("解锁宝箱" in text or "宝箱奖励" in text for text in texts):
        return False
    if (_ad_session_screen(screen) or _reward_task(screen) or _reward_controls(screen, EXPAND_RE)
            or any(CLAIM_RE.fullmatch(text.strip()) for text in texts)):
        return False
    close = exits.find_exit([line for line in screen.lines if 0.35 <= line.x <= 0.65 and line.y > 0.55])
    if close is not None:
        session_obj.tap_line(close, screen)
    else:
        session_obj.adb.key("KEYCODE_BACK")
    time.sleep(1.0)
    return True


def _settle_modals(session_obj: Session, *, initial: Screen | None = None, watch_reward: bool = False) -> Screen:
    """处理已确认的奖励与膨胀广告，返回最后确认的画面。"""
    screen = initial if initial is not None else session_obj.capture_in_app()
    last_action: str | None = None
    while True:
        if _ad_session_screen(screen):
            offer = _ad_offer(screen)
            feed = _ad_feed_badge(screen) or bool(offer and offer.resume and offer.resume.text == "继续领金币")
            _watch_ad(session_obj, initial=screen, return_to_reward=True, feed=feed, return_to_watch=watch_reward)
            screen = session_obj.capture_in_app()
            continue
        task = _reward_task(screen)
        if task is not None and not (task == "watch" and watch_reward):
            return screen
        expansions = _reward_controls(screen, EXPAND_RE)
        if len(expansions) > 1:
            raise UnconfirmedAd("金币膨胀广告按钮不唯一，保留当前弹窗")
        claims = _reward_controls(screen, CLAIM_RE)
        expand = expansions[0] if expansions else None
        if expand is not None:
            amount = _ad_button_coins(expand)
            if watch_reward or (amount is not None and amount >= MIN_AD_OFFER_COINS):
                if expand.text == last_action:
                    raise UnconfirmedAd("膨胀广告结束后仍显示相同入口，保留当前弹窗")
                session_obj.tap_line(expand, screen)
                last_action = expand.text
                _watch_ad(session_obj, return_to_reward=True, return_to_watch=watch_reward)
                screen = session_obj.capture_in_app()
                continue
            if amount is None:
                raise UnconfirmedAd("未读到膨胀广告金币，保留当前弹窗")
            claims = _claims_below(expand, claims)
        if not claims and expand is None and task != "watch" and not _cumulative_reward(screen):
            return screen
        if len(claims) != 1 or last_action == claims[0].text:
            raise UnconfirmedAd("未确认唯一的奖励结算按钮，保留当前弹窗")
        session_obj.tap_line(claims[0], screen)
        last_action = claims[0].text
        time.sleep(1.0)
        screen = session_obj.capture_in_app()


@dataclass(frozen=True)
class AdOffer:
    coins: int | None
    resume: Line | None
    exit: Line | None


def _ad_offer(screen: Screen) -> AdOffer | None:
    body = [line for line in screen.lines if 0.24 <= line.x <= 0.76 and 0.29 <= line.y <= 0.68]
    headings = [line for line in body if line.y <= 0.47
                and re.match(r"(?:再看(?:[一1-9０-９个条段\s]|$)|额外获得)", line.text.strip())]
    resumes = [line for line in body if 0.48 <= line.y <= 0.57
               and line.text.strip() in {"领取奖励", "继续领金币", "继续观看"}]
    exits_found = [line for line in body if 0.56 <= line.y <= 0.66 and line.text.strip() == "坚持退出"]
    if not any(line.text.strip() == "坚持退出" for line in body) and not (headings and resumes) and not any(
        "额外获得" in line.text for line in headings
    ):
        return None
    amounts = {
        int(re.sub(r"[,，]", "", match[1]))
        for line in body if line.y < 0.49 and headings
        for match in COIN_AMOUNT_RE.finditer(line.text)
    }
    return AdOffer(next(iter(amounts)) if len(amounts) == 1 else None,
                   resumes[0] if len(resumes) == 1 else None,
                   exits_found[0] if len(exits_found) == 1 else None)


def _ad_header(screen: Screen) -> bool:
    return any(line.y <= 0.16 and (line.text.startswith("广告") or "反馈" in line.text)
               for line in screen.lines)


def _ad_feed_badge(screen: Screen) -> bool:
    badge = [line.text for line in screen.lines if 0.68 <= line.x <= 0.98 and 0.14 <= line.y <= 0.25]
    if any(re.search(r"赚.{0,2}中|已暂停|上滑继续|再看\s*\d{1,3}\s*秒", text) for text in badge):
        return True
    return "暂停" in badge and ocr.find(screen, r"上滑浏览(?:赚|贝兼)金币", (0.30, 0.70), (0.90, 1)) is not None


def _ad_live_page(screen: Screen) -> bool:
    return (ocr.find(screen, r"更?多直播", (0.75, 1), (0.06, 0.14)) is not None
            and ocr.find(screen, r"本场点赞|人气榜|小时榜", (0, 0.4), (0.04, 0.14)) is not None)


def _ad_live_close(screen: Screen) -> Line | None:
    if not _ad_live_page(screen):
        return None
    hits = [line for line in screen.lines if 0.03 <= line.y <= 0.085 and line.max_x >= 0.92
            and re.search(r"[×✕xX]$", line.text.strip())]
    if len(hits) != 1:
        return None
    hit = hits[0]
    right = min(1.0, hit.max_x)
    left = max(hit.min_x, right - hit.h * screen.height / screen.width)
    return Line("×", (left + right) / 2, hit.y, right - left, hit.h)


def _ad_session_screen(screen: Screen) -> bool:
    return (_ad_header(screen) or _ad_feed_badge(screen) or _ad_offer(screen) is not None
            or _ad_live_page(screen)
            or any(AD_COUNT_RE.search(line.text) or line.text.strip() == "坚持退出" for line in screen.lines))


def _ad_returned_to_board(screen: Screen) -> bool:
    return (ocr.find(screen, r"^福利中心$", y_range=(0.02, 0.12)) is not None
            and ocr.find(screen, r"^金币收益|做任务.*金币|^连续刷视频|^看广告.*金币",
                         y_range=(0.08, 0.84)) is not None)


def _wait_for_coin_ad(session_obj: Session, screen: Screen | None, *, feed: bool, budget: float,
                      awaiting_new: bool, observe_time: Callable[[int], None],
                      return_to_reward: bool = False, return_to_watch: bool = False) -> bool:
    """完成当前广告，False 表示用户要求的续看操作已发出。"""
    deadline = time.monotonic() + budget
    transition_deadline = min(deadline, time.monotonic() + 12)
    uncertain_offer_until: float | None = None
    board_confirmed_once = False
    highest_counter: int | None = None
    last_feed_progress_at: float | None = None
    feed_offer_requested = False
    ad_seen = False
    timer_seen = False
    initial_live = screen is not None and _ad_live_page(screen)
    close_sent = False
    live_close_sent = False
    exit_sent = False
    claim_sent = False
    while time.monotonic() < deadline:
        if screen is None:
            try:
                screen = session_obj.capture_in_app()
            except (ForegroundError, AdbError, UnresolvedScreen, subprocess.TimeoutExpired):
                time.sleep(1.0)
                continue
        current, screen = screen, None
        offer = _ad_offer(current)
        task_popup = _reward_task(current)
        reward_popup = task_popup is not None or _cumulative_reward(current)
        on_board = offer is None and not reward_popup and _ad_returned_to_board(current)
        if not on_board:
            board_confirmed_once = False
        ad_page = _ad_header(current)
        feed_ready = feed and _ad_feed_badge(current)
        counting = next((match for line in current.lines if line.y < 0.20
                         and (match := AD_COUNT_RE.search(line.text))), None)
        close = _ad_close_control(current) if ad_page else None
        ad_seen = ad_seen or ad_page or feed_ready or counting is not None or offer is not None or initial_live
        if ocr.find(current, r"暂无(?:可用)?广告|广告(?:暂不可用|加载失败|次数已用完)",
                    (0.21, 0.79), (0.30, 0.75)) is not None:
            raise UnconfirmedAd("页面提示没有可播放的广告，保留当前页面")

        if awaiting_new:
            if offer is None and (counting is not None or feed_ready):
                awaiting_new = False
            elif time.monotonic() >= transition_deadline:
                raise UnconfirmedAd("续看后未确认进入下一条广告或视频")
            else:
                time.sleep(0.8)
                continue

        if offer is not None:
            if exit_sent:
                time.sleep(0.8)
                continue
            amount = offer.coins
            target = offer.resume if amount is not None and amount >= MIN_AD_OFFER_COINS else offer.exit
            if amount is None or target is None:
                if uncertain_offer_until is None:
                    uncertain_offer_until = time.monotonic() + 3.0
                if time.monotonic() >= uncertain_offer_until:
                    raise UnconfirmedAd("续看金币或对应按钮未能核实，保留当前弹窗")
                time.sleep(0.8)
                continue
            uncertain_offer_until = None
            if amount >= MIN_AD_OFFER_COINS:
                report("ad.offer", f"续看可得 {amount} 金币，继续观看", offer_coins=amount)
                session_obj.tap_line(target, current)
                return False
            report("ad.offer", f"续看可得 {amount} 金币，低于 {MIN_AD_OFFER_COINS}，选择坚持退出", offer_coins=amount)
            session_obj.tap_line(target, current)
            exit_sent = True
            time.sleep(1.0)
            continue

        claims = ocr.find_all(current, r"^(?:开心收下|收下)$", (0.28, 0.72), (0.45, 0.75))
        settled = close_sent or exit_sent or claim_sent or live_close_sent or feed_offer_requested
        if (return_to_reward and ad_seen and (settled or timer_seen) and not ad_page and not feed_ready
                and counting is None and (reward_popup or len(claims) == 1)):
            return True
        if (return_to_watch and ad_seen and (settled or timer_seen) and not ad_page and not feed_ready
                and counting is None and not claims and not reward_popup and _watch_feed(current)):
            return True
        if not ad_page and ad_seen and len(claims) == 1 and not claim_sent:
            session_obj.tap_line(claims[0], current)
            claim_sent = True
            time.sleep(1.0)
            continue
        if on_board and not claims:
            if ad_seen and settled:
                if board_confirmed_once:
                    return True
                board_confirmed_once = True
                time.sleep(0.8)
                continue
            if time.monotonic() >= transition_deadline:
                raise UnconfirmedAd("返回福利中心，未确认本次广告或视频机会和结算")

        if exit_sent:
            time.sleep(0.8)
            continue
        if feed_ready:
            # 右上角累计计数用于判断进展，退出报价以确认弹窗为准。
            now = time.monotonic()
            if last_feed_progress_at is None:
                last_feed_progress_at = now
            counters = [int(line.text.strip().rstrip("/")) for line in current.lines
                        if 0.80 <= line.x <= 0.98 and 0.15 <= line.y <= 0.19
                        and re.fullmatch(r"[0-9]{1,9}/?", line.text.strip())]
            if len(counters) == 1 and (highest_counter is None or counters[0] > highest_counter):
                highest_counter = counters[0]
                last_feed_progress_at = now
                deadline = now + budget
            hint = ocr.find(current, r"^最高(?:可)?得\s*[0-9]{1,7}\s*金币$", (0.60, 0.82), (0.13, 0.23))
            hint_coins = _ad_button_coins(hint) if hint is not None else None
            low_hint = hint_coins is not None and hint_coins < MIN_AD_OFFER_COINS
            if not feed_offer_requested and (low_hint or now - last_feed_progress_at >= 40):
                report("video.offer.check", "检查连续刷视频的续看报价")
                session_obj.check_foreground()
                session_obj.adb.key("KEYCODE_BACK")
                feed_offer_requested = True
                time.sleep(2.0)
                continue
            session_obj.check_foreground()
            session_obj.adb.swipe(*session_obj.profile.swipe(
                current.width, current.height, down=True, area=(0.25, 0.65, 0.28, 0.83),
            ))
            time.sleep(session_obj.profile.rng.uniform(3.0, 5.0))
            continue
        if counting is not None:
            timer_seen = True
            remaining = int(counting[1])
            observe_time(remaining)
            wait = min(45.0, remaining + 2, max(0.0, deadline - time.monotonic()))
            progress.wait_observed_ad(progress.APP_SODA, CURRENT_TASK, remaining, wait)
            continue
        if (timer_seen or initial_live) and not live_close_sent:
            live_close = _ad_live_close(current)
            if live_close is not None:
                session_obj.tap_line(live_close, current)
                live_close_sent = True
                time.sleep(1.2)
                continue
        finished = any(line.y <= 0.16 and line.x >= 0.60 and re.fullmatch(
            r"(?:继续观看|(?:已)?(?:获得|领取)奖励)\s*[×xX✕]?", line.text.strip()
        ) for line in current.lines)
        if ad_page and not close_sent and (finished or timer_seen and close is not None):
            control = close or ocr.find(current, r"^继续观看$", (0.65, 0.94), (0.02, 0.13))
            if control is not None:
                session_obj.tap_line(control, current)
                close_sent = True
                time.sleep(1.2)
                continue
        if not ad_seen and time.monotonic() >= transition_deadline:
            raise UnconfirmedAd("未确认有可播放的广告或视频")
        time.sleep(0.8)
    raise UnconfirmedAd("等待广告或视频结算超时，保留当前页面")


def _watch_ad(session_obj: Session, *, budget: float = 150,
              return_to_reward: bool = False, initial: Screen | None = None, feed: bool = False,
              return_to_watch: bool = False) -> bool:
    """广告页：等倒计时 → 点右上角 X → 处理「再看一个视频」/累计奖励。"""
    awaiting_new = False
    while True:
        with progress.ad_countdown(progress.APP_SODA, CURRENT_TASK, budget) as observe_time:
            if _wait_for_coin_ad(session_obj, initial, feed=feed, budget=budget,
                                awaiting_new=awaiting_new, observe_time=observe_time,
                                return_to_reward=return_to_reward, return_to_watch=return_to_watch):
                return True
        initial, awaiting_new = None, True


def _tier_anchor(screen: Screen, title: str) -> Line | None:
    anchors = [line for line in screen.lines if line.x < 0.55 and line.text.startswith(title)]
    return anchors[0] if len(anchors) == 1 else None


def _tier_row(screen: Screen, title: str) -> list[Line]:
    anchor = _tier_anchor(screen, title)
    if anchor is None:
        return []
    following = min((line.min_y for line in screen.lines
                     if line.x < 0.50 and line.y > anchor.y
                     and re.match(r"^(逛街|看广告|连续刷|红包雨|看视频|翻卡|签到).*(?:金币|金市|最高)", line.text)),
                    default=1.0)
    return [line for line in screen.lines
            if anchor.y + 0.045 <= line.y <= anchor.y + 0.16 and line.y < following]


def _claim_video_tiers(session_obj: Session, *, limit: int = 6) -> int:
    """从看视频任务行领取明确标为待领取的档位。"""
    claimed = 0
    _chest_overlay(session_obj, session_obj.capture_in_app())
    screen = session_obj.capture_in_app()
    for _ in range(limit):
        pending = [line for line in _tier_row(screen, "看视频") if line.text.strip() == "待领取"]
        if not pending:
            break
        tile = pending[0]
        session_obj.tap_line(tile, screen)
        time.sleep(1.0)
        screen = _settle_modals(session_obj, watch_reward=True)
        for attempt in range(4):
            remaining = [line for line in _tier_row(screen, "看视频") if line.text.strip() == "待领取"]
            if (_ad_returned_to_board(screen) and _reward_task(screen) is None
                    and (len(remaining) < len(pending) or all(abs(line.x - tile.x) >= 0.025
                         or abs(line.y - tile.y) >= 0.025 for line in remaining))):
                break
            if attempt < 3:
                time.sleep(0.65)
                screen = _settle_modals(session_obj, watch_reward=True)
        else:
            raise UnconfirmedAd("看视频领奖后未确认档位更新")
        claimed += 1
    return claimed


def _reward_controls(screen: Screen, pattern: re.Pattern[str]) -> list[Line]:
    return [line for line in screen.lines
            if 0.25 <= line.x <= 0.75 and 0.46 <= line.y <= 0.77 and pattern.match(line.text.strip())]


def _reward_control(screen: Screen, pattern: re.Pattern[str]) -> Line | None:
    hits = _reward_controls(screen, pattern)
    return hits[0] if len(hits) == 1 else None


def _listen_scroll_tiles(screen: Screen) -> list[tuple[Line, Line]]:
    row = _tier_row(screen, "听歌")
    amounts = [line for line in row if re.fullmatch(r"[1-9][0-9]{1,3}", line.text.strip())]
    tiles = []
    for status in row:
        # 最右侧 30 分钟大卡固定在列表之外，横滑的两端都必须落在它左侧。
        if not 0.12 <= status.x <= 0.73 or not re.fullmatch(
            r"待领取|已领取|\d{1,3}(?:秒|分钟)", status.text.strip()
        ):
            continue
        matches = [amount for amount in amounts
                   if abs(amount.x - status.x) < 0.025
                   and 0.012 <= amount.y - status.y <= 0.070
                   and status.max_y < amount.min_y
                   and max(status.min_x, amount.min_x) < min(status.max_x, amount.max_x)]
        if len(matches) == 1:
            tiles.append((status, matches[0]))
    return sorted(tiles, key=lambda tile: tile[0].x)


def _listen_fortune_tab(screen: Screen, previous: Screen) -> Line | None:
    hits = ocr.find_all(screen, r"^福利中心$", y_range=(0.02, 0.10))
    if hits:
        return hits[0] if len(hits) == 1 else None
    previous_hits = ocr.find_all(previous, r"^福利中心$", y_range=(0.02, 0.10))
    if len(previous_hits) != 1 or (screen.width, screen.height) != (previous.width, previous.height):
        return None
    if not ocr.find(screen, r"商城\s*福利中心", y_range=(0.02, 0.10)):
        return None
    # 演出页未选中的两个页签可能被 OCR 合并，按切换前的福利中心边界裁读。
    anchor = previous_hits[0]
    left = max(0, math.floor((anchor.min_x - 0.01) * screen.width))
    top = max(0, math.floor((anchor.min_y - 0.005) * screen.height))
    right = min(screen.width, math.ceil((anchor.max_x + 0.01) * screen.width))
    bottom = min(screen.height, math.ceil((anchor.max_y + 0.005) * screen.height))
    cropped = _cropped_ocr(screen, "crop", (left, top, right, bottom))
    hits = [Line("福利中心", item["x"] + item["w"] / 2, item["y"] + item["h"] / 2,
                 item["w"], item["h"]) for item in cropped if item["text"].strip() == "福利中心"]
    return hits[0] if len(hits) == 1 else None


def _scroll_listen_tiers(session_obj: Session, screen: Screen, *,
                         towards_right: bool) -> tuple[Screen, bool | None]:
    """返回最新页面，True 表示档位移动，False 表示该方向未移动，None 结束横滑。"""
    if not _listen_board(screen):
        raise UnresolvedScreen("未确认听歌档位所在的福利中心页面，停止横滑")
    tiles = _listen_scroll_tiles(screen)
    spans = [(left, right) for pos, left in enumerate(tiles) for right in tiles[pos + 1:]
             if 0.12 <= right[0].x - left[0].x <= 0.33]
    if not spans:
        report("listen.scroll.stop", "听歌档位滑动区域无法确认，停止横滑", level="warn")
        return screen, None
    left, right = max(spans, key=lambda span: span[1][0].x - span[0][0].x)
    top = max(status.max_y for status, _ in tiles)
    bottom = min(amount.min_y for _, amount in tiles)
    if (bottom - top) * screen.height < 3:
        report("listen.scroll.stop", "听歌档位行没有足够的滑动空间，停止横滑", level="warn")
        return screen, None

    profile = session_obj.profile
    def middle(low: float, high: float) -> float:
        return profile.rng.uniform(low + (high - low) * 0.25, high - (high - low) * 0.25)

    x1, x2 = (round(middle(max(label.min_x, amount.min_x),
                           min(label.max_x, amount.max_x)) * screen.width)
              for label, amount in (left, right))
    if towards_right:
        x1, x2 = x2, x1
    y = round(middle(top, bottom) * screen.height)
    gesture = (x1, y, x2, y, round(profile.swipe_duration() * 1000))
    session_obj.check_foreground()
    session_obj.adb.swipe(*gesture)
    wait = profile.swipe_pause()
    direction = "right" if towards_right else "left"
    report("scroll", "在听歌档位内横向查看", direction=direction, gesture=gesture, seconds=round(wait, 2))
    time.sleep(wait)

    current = session_obj.capture_in_app()
    if not _listen_board(current):
        time.sleep(profile.poll_delay())
        current = session_obj.capture_in_app()
    if not _listen_board(current):
        is_performance = (ocr.find(current, r"^演出$", y_range=(0.02, 0.10)) is not None
                          and ocr.find(current, r"演唱会|演出阵容|许愿活动", y_range=(0.10, 0.89)) is not None)
        tab = _listen_fortune_tab(current, screen) if is_performance else None
        if tab is None:
            raise UnresolvedScreen("听歌档位横滑后未确认仍在福利中心，停止横滑")
        session_obj.tap_line(tab, current)
        for _ in range(3):
            current = session_obj.capture_in_app()
            if _listen_board(current):
                report("listen.scroll.stop", "横滑切入演出页，已返回福利中心并停止横滑", level="warn")
                return current, None
            time.sleep(profile.poll_delay())
        raise UnresolvedScreen("横滑切入演出页，点击福利中心后未确认返回")

    new_tiles = _listen_scroll_tiles(current)
    moved = (len(new_tiles) != len(tiles) or any(
        abs(before[0].x - after[0].x) > 0.012 or before[1].text != after[1].text
        for before, after in zip(tiles, new_tiles)
    ))
    if not moved:
        report("listen.scroll.end", "听歌档位在该方向未移动", direction=direction)
    return current, moved


def _listen_receipt_amount(screen: Screen) -> int | None:
    if not (_cumulative_reward(screen) or _reward_task(screen) == "listen"):
        return None
    amounts = [int(re.sub(r"[,，]", "", match[1]))
               for line in screen.lines if 0.25 <= line.x <= 0.75 and 0.38 <= line.y <= 0.54
               if (match := re.fullmatch(r"[+＋]\s*(\d[\d,，]*)\s*金币", line.text.strip()))]
    return amounts[0] if len(amounts) == 1 and amounts[0] > 0 else None


def _finish_listen_reward(session_obj: Session, screen: Screen) -> tuple[Screen, int | None]:
    is_listen_popup = _reward_task(screen) == "listen"
    if not (is_listen_popup or _cumulative_reward(screen)):
        raise UnresolvedScreen("未读到听歌领奖弹窗")
    ads = _reward_controls(screen, REWARD_AD_RE) if is_listen_popup else []
    if len(ads) > 1:
        raise UnresolvedScreen("听歌奖励广告按钮不唯一")
    ad_coins = _ad_button_coins(ads[0]) if ads else None
    watched = bool(ads) and (ad_coins is None or ad_coins >= MIN_AD_OFFER_COINS)
    if watched:
        session_obj.tap_line(ads[0], screen)
        _watch_ad(session_obj, return_to_reward=True)
        screen = session_obj.capture_in_app()

    pressed = False
    receipt_amount = None
    for attempt in range(8):
        if _listen_board(screen):
            return screen, receipt_amount
        if not pressed:
            if _cumulative_reward(screen):
                claim = _reward_control(screen, HAPPY_CLAIM_RE)
            elif _reward_task(screen) == "listen":
                current_ads = _reward_controls(screen, REWARD_AD_RE)
                claims = _reward_controls(screen, CLAIM_RE)
                if current_ads:
                    amount = _ad_button_coins(current_ads[0]) if len(current_ads) == 1 else None
                    if amount is not None and amount < MIN_AD_OFFER_COINS:
                        claims = _claims_below(current_ads[0], claims)
                    else:
                        claims = []
                claim = claims[0] if len(claims) == 1 else None
            elif watched and _reward_task(screen) is None:
                claim = _reward_control(screen, HAPPY_CLAIM_RE)
            else:
                claim = None
            if claim is not None:
                session_obj.tap_line(claim, screen)
                pressed = True
                receipt_amount = _listen_receipt_amount(screen)
        if attempt < 7:
            time.sleep(0.65)
            screen = session_obj.capture_in_app()
    raise UnresolvedScreen("听歌领奖后未确认返回档位列表")


def _listen_board(screen: Screen) -> bool:
    anchor = _tier_anchor(screen, "听歌")
    return (anchor is not None and any(word in anchor.text for word in ("金币", "金市", "最高"))
            and _reward_task(screen) is None and not _cumulative_reward(screen)
            and _reward_control(screen, HAPPY_CLAIM_RE) is None)


def _pending_listen_tiers(screen: Screen) -> list[Line]:
    return [line for line in _tier_row(screen, "听歌") if line.text.strip() == "待领取"]


def _confirm_listen_tier(session_obj: Session, screen: Screen, tile: Line, pending_before: int) -> Screen:
    for attempt in range(5):
        if _listen_board(screen):
            pending = _pending_listen_tiers(screen)
            old_position = any(abs(line.x - tile.x) < 0.025 and abs(line.y - tile.y) < 0.025
                               for line in pending)
            if len(pending) < pending_before or not old_position:
                return screen
        if attempt < 4:
            time.sleep(0.65)
            screen = session_obj.capture_in_app()
    raise UnresolvedScreen("领奖后原听歌档位的状态未更新")


def _task_check_in(session_obj: Session) -> str:
    if not _ensure_fortune(session_obj):
        raise UnresolvedScreen("未确认福利中心")
    if not _find_entry(session_obj, "check-in"):
        return "未找到签到入口"
    time.sleep(1.8)
    for _ in range(3):
        screen = session_obj.capture_in_app()
        button = next((line for line in screen.lines
                       if re.fullmatch(r"(立即签到|签到|签到领金币)", line.text.strip())), None)
        if button is None:
            break
        session_obj.tap_line(button, screen)
        time.sleep(1.5)
        _settle_modals(session_obj)
    _settle_modals(session_obj)
    _ensure_fortune(session_obj)
    return "签到流程完成"


def _task_listen(session_obj: Session) -> str:
    screen = session_obj.capture_in_app()
    total = 0
    if _reward_task(screen) == "listen" or _cumulative_reward(screen):
        total = int(_reward_task(screen) == "listen")
        screen, receipt_amount = _finish_listen_reward(session_obj, screen)
        if total:
            progress.coin_event(TASKS["listen"].name, receipt_amount)
    else:
        if not _ensure_fortune(session_obj):
            return "未确认福利中心，未点击档位"
        session_obj.scroll(down=False, times=4, target=re.compile(r"^听歌"))
        screen = session_obj.capture_in_app()
    # 先向右查看后面的档位，再回看左侧可能被手动移出画面的档位。
    scans = iter((True, True, True, False, False, False))
    stopped: set[bool] = set()
    while True:
        pending = _pending_listen_tiers(screen) if _listen_board(screen) else []
        if not pending:
            direction = next((way for way in scans if way not in stopped), None)
            if direction is None:
                break
            screen, moved = _scroll_listen_tiers(session_obj, screen, towards_right=direction)
            if moved is None:
                break
            if not moved:
                stopped.add(direction)
            continue
        if total >= 12:
            raise UnresolvedScreen("本次已处理 12 个档位，仍有待领取档位")
        tile = pending[0]
        before = coins_on(screen)
        session_obj.tap_line(tile, screen)
        for _ in range(4):
            screen = session_obj.capture_in_app()
            if _reward_task(screen) == "listen" or _cumulative_reward(screen):
                break
            time.sleep(0.7)
        screen, receipt_amount = _finish_listen_reward(session_obj, screen)
        screen = _confirm_listen_tier(session_obj, screen, tile, len(pending))
        total += 1
        after = coins_on(screen)
        earned = receipt_amount if receipt_amount is not None else (
            after - before if after is not None and before is not None else None)
        progress.coin_event(TASKS["listen"].name, earned)
        report("listen.tier", f"已完成 {total} 个听歌档位", claimed=total,
               visible_pending=len(_pending_listen_tiers(screen)), coins=after, delta=earned)
    return f"共领取 {total} 个听歌档位" if total else "没有识别到「待领取」听歌档位"


def _run_coin_ad_task(session_obj: Session, key: str) -> None:
    initial = session_obj.capture_in_app()
    if not _ad_session_screen(initial):
        if not _ensure_fortune(session_obj):
            raise UnconfirmedAd("未确认福利中心，保留当前页面")
        if not _find_entry(session_obj, key):
            raise UnresolvedScreen(f"未确认「{TASKS[key].name}」的可用入口")
        initial = None
    _watch_ad(session_obj, initial=initial, feed=key == "video")


def _task_ad(session_obj: Session) -> str:
    _run_coin_ad_task(session_obj, "ad")
    return "广告链已结束并返回福利中心"


def _task_video(session_obj: Session) -> str:
    _run_coin_ad_task(session_obj, "video")
    return "连续刷视频已结束并返回福利中心"


WATCH_FEED_COINS_RE = r"^看视频已(?:赚|贝兼)\s*(\d{1,7})\s*(?:金币)?$"
WATCH_FEED_END_RE = r"^(?:暂无更多(?:视频|推荐(?:内容)?)?|没有更多(?:视频|推荐(?:内容)?)?了?|(?:已经|已)到底了|到底了)[。！!]?$"


def _watch_feed_coins(screen: Screen) -> int | None:
    badge = ocr.find(screen, WATCH_FEED_COINS_RE, (0.53, 0.85), (0.03, 0.10))
    return int(re.fullmatch(WATCH_FEED_COINS_RE, badge.text.strip())[1]) if badge is not None else None


def _watch_feed(screen: Screen) -> bool:
    if _watch_feed_coins(screen) is not None:
        return True
    return (ocr.find(screen, r"^视频$", (0.35, 0.62), (0.03, 0.10)) is not None
            and (_watch_feed_end(screen)
                 or ocr.find(screen, r"关注$|^展开$|^[12][.·．][0O][xX×]$", y_range=(0.58, 0.94)) is not None))


def _watch_feed_end(screen: Screen) -> bool:
    return ocr.find(screen, WATCH_FEED_END_RE, (0.25, 0.75), (0.35, 0.88)) is not None


def _watch_reward_screen(screen: Screen) -> bool:
    if _ad_session_screen(screen) or _cumulative_reward(screen):
        return True
    task = _reward_task(screen)
    return task == "watch" or task is None and bool(
        _reward_controls(screen, EXPAND_RE) or _reward_controls(screen, CLAIM_RE))


def _watch_clip_labels(screen: Screen) -> set[str]:
    # 视频画面内的字幕会变化，优先对比下方的作者、标题和作品说明。
    return {label for line in screen.lines if 0.59 <= line.y <= 0.75 and line.min_x < 0.43
            if len(label := re.sub(r"[\W_]+", "", line.text)) >= 5 and not label.isdigit()}


def _browse_watch_feed(session_obj: Session, screen: Screen) -> tuple[Screen, bool]:
    """继续观看并处理插入的奖励，返回当前页面和是否已停止浏览。"""
    down = True
    blocked_directions: set[bool] = set()
    same_clip = 0
    previous_clip: set[str] = set()
    highest_coins = None
    swipes_without_progress = 0
    while True:
        if _watch_reward_screen(screen):
            screen = _settle_modals(session_obj, initial=screen, watch_reward=True)
            previous_clip.clear()
            same_clip = 0
            blocked_directions.clear()
            highest_coins = None
            swipes_without_progress = 0
            continue
        if not _watch_feed(screen):
            return screen, False

        coins = _watch_feed_coins(screen)
        if coins is not None and (highest_coins is None or coins > highest_coins):
            highest_coins = coins
            swipes_without_progress = 0
        clip = _watch_clip_labels(screen)
        if previous_clip and clip:
            same_clip = same_clip + 1 if len(previous_clip & clip) >= min(2, len(previous_clip), len(clip)) else 0
            if not same_clip:
                blocked_directions.clear()
        at_boundary = same_clip >= 2 or down and _watch_feed_end(screen)
        if at_boundary:
            blocked_directions.add(down)
            if len(blocked_directions) == 2:
                report("watch.feed.stop", "两个方向都没有新视频，返回福利中心结算")
                return screen, True
            down = not down
            same_clip = 0
            report("watch.feed.reverse", "下滑没有更多视频，改为往回滚动" if not down else "回滚到边界，改为向下观看")
        if swipes_without_progress >= 24 and not at_boundary:
            report("watch.feed.stop", "多次滑动未确认新的金币进度，返回福利中心结算", level="warn")
            return screen, True

        gesture = session_obj.profile.swipe(screen.width, screen.height, down=down, area=(0.25, 0.65, 0.28, 0.82))
        session_obj.check_foreground()
        session_obj.adb.swipe(*gesture)
        wait = session_obj.profile.rng.uniform(3.0, 5.0)
        report("scroll", "向下观看视频" if down else "往回滚动观看视频",
               direction="down" if down else "up", gesture=gesture, seconds=round(wait, 2),
               distance=abs(gesture[3] - gesture[1]), duration=gesture[4] / 1000)
        session_obj.save()
        time.sleep(wait)
        previous_clip = clip
        swipes_without_progress += 1
        screen = session_obj.capture_in_app()


def _task_watch(session_obj: Session) -> str:
    screen = session_obj.capture_in_app()
    if not (_watch_reward_screen(screen) or _watch_feed(screen) or _ad_returned_to_board(screen)):
        if not _ensure_fortune(session_obj):
            raise UnresolvedScreen("未确认看视频入口所在的福利中心")
        screen = session_obj.capture_in_app()

    total = 0
    stop_on_board = False
    last_entry: tuple[str, str, int] | None = None
    repeated_entry = 0
    while True:
        if _watch_reward_screen(screen):
            screen = _settle_modals(session_obj, initial=screen, watch_reward=True)
            continue
        if _watch_feed(screen):
            screen, exhausted = _browse_watch_feed(session_obj, screen)
            if exhausted:
                close = _exit_control(screen)
                if close is not None:
                    session_obj.tap_line(close, screen)
                else:
                    session_obj.check_foreground()
                    session_obj.adb.key("KEYCODE_BACK")
                stop_on_board = True
                time.sleep(1.2)
                screen = session_obj.capture_in_app()
            continue
        if not _ad_returned_to_board(screen) or _reward_task(screen) is not None:
            raise UnconfirmedAd("未确认返回看视频流或福利中心，保留当前页面")

        total += _claim_video_tiers(session_obj)
        if stop_on_board:
            return f"共领取 {total} 个档位"
        target = _locate_entry(session_obj, "watch")
        if target is None:
            return f"共领取 {total} 个档位（未找到可继续的入口）"
        board, button = target
        anchor = _tier_anchor(board, "看视频")
        hint = ocr.find(board, r"^再看\s*\d+\s*个视频", (0, 0.65), (anchor.y, anchor.y + 0.055)) if anchor else None
        entry = (button.text, hint.text if hint else "", total)
        repeated_entry = repeated_entry + 1 if entry == last_entry else 0
        if repeated_entry >= 2:
            return f"共领取 {total} 个档位（入口多次返回，未确认新档位）"
        last_entry = entry
        session_obj.tap_line(button, board)
        time.sleep(1.8)
        screen = session_obj.capture_in_app()
        for _ in range(2):
            if not _ad_returned_to_board(screen) or _watch_reward_screen(screen):
                break
            time.sleep(0.65)
            screen = session_obj.capture_in_app()


def _flip_board_count(screen: Screen) -> int | None:
    title = next((line for line in screen.lines
                  if line.x < 0.55 and line.text.startswith("翻卡赢")), None)
    if title is None:
        return None
    texts = [line.text for line in screen.lines if title.y <= line.y <= title.y + 0.07]
    for text in texts:
        match = FLIP_HINT_RE.search(text)
        if match:
            return FLIP_RANKS.index(match[1])
        if re.search(r"已翻完|已翻开\s*4\s*张|已全部翻开", text):
            return len(FLIP_RANKS)
    if any("看广告赢翻卡机会" in text for text in texts):
        return 0
    return None


def _flip_panel(session_obj: Session) -> Screen:
    for _ in range(4):
        screen = session_obj.capture_in_app()
        if _reward_task(screen) == "flip-card" or _cumulative_reward(screen):
            break
        time.sleep(0.6)
    return screen


def _task_flip(session_obj: Session) -> str:
    screen = session_obj.capture_in_app()
    if _reward_task(screen) == "flip-card":
        start_ad = _reward_control(screen, FLIP_AD_RE)
        opened = FLIP_RANKS.index(FLIP_AD_RE.fullmatch(start_ad.text.strip())[1]) if start_ad else None
        if opened is None:
            raise UnresolvedScreen("当前翻卡弹窗未识别到下一张卡")
        report("flip.scan", f"已有 {opened} 张翻开，还剩 {len(FLIP_RANKS) - opened} 张",
               opened=opened, remaining=len(FLIP_RANKS) - opened)
    else:
        if not _ensure_fortune(session_obj):
            return "未确认福利中心"
        target = _locate_entry(session_obj, "flip-card")
        if target is None:
            return "未找到翻卡入口"
        board, entry = target
        opened = _flip_board_count(board)
        if opened is None:
            raise UnresolvedScreen("未识别已翻及未翻卡片数")
        report("flip.scan", f"已有 {opened} 张翻开，还剩 {len(FLIP_RANKS) - opened} 张",
               opened=opened, remaining=len(FLIP_RANKS) - opened)
        session_obj.tap_line(entry, board)
        screen = _flip_panel(session_obj)
    started_with = opened
    ads = 0
    seen_ranks: set[int] = set()
    while opened < len(FLIP_RANKS):
        if _reward_task(screen) != "flip-card":
            count = _flip_board_count(screen)
            if count == len(FLIP_RANKS) and seen_ranks:
                opened = count
                break
            if count is None or count < opened:
                raise UnresolvedScreen(f"翻卡广告后未确认进度，已观察 {ads} 个广告")
            target = _locate_entry(session_obj, "flip-card")
            if target is None:
                raise UnresolvedScreen("返回福利页后未找到翻卡入口")
            session_obj.tap_line(target[1], target[0])
            screen = _flip_panel(session_obj)

        offer = _reward_control(screen, FLIP_AD_RE) if _reward_task(screen) == "flip-card" else None
        if offer is None:
            raise UnresolvedScreen(f"未定位剩余卡片的广告按钮，已观察 {ads} 个广告")
        rank = FLIP_RANKS.index(FLIP_AD_RE.fullmatch(offer.text.strip())[1])
        if rank < opened or rank in seen_ranks:
            raise UnresolvedScreen(f"广告结束后仍提示翻{FLIP_RANKS[rank]}位卡")
        opened = rank
        report("flip.progress", f"已翻 {opened} 张，剩余 {len(FLIP_RANKS) - opened} 张",
               opened=opened, remaining=len(FLIP_RANKS) - opened, ads=ads)
        session_obj.tap_line(offer, screen)
        _watch_ad(session_obj, return_to_reward=True)
        ads += 1
        seen_ranks.add(rank)
        screen = _flip_panel(session_obj)
        if _cumulative_reward(screen):
            screen = _settle_modals(session_obj, initial=screen)
        if rank == len(FLIP_RANKS) - 1:
            if _reward_controls(screen, FLIP_AD_RE):
                raise UnresolvedScreen("千位卡广告结束后仍显示翻卡入口")
            if _flip_board_count(screen) != len(FLIP_RANKS) and _reward_task(screen) != "flip-card":
                raise UnresolvedScreen("千位卡未出现结算弹窗")
            opened = len(FLIP_RANKS)

    # 全部翻开后才处理最终还能观看的奖励广告或领取按钮。
    bonuses: set[str] = set()
    while _reward_task(screen) == "flip-card":
        offers = _reward_controls(screen, REWARD_AD_RE)
        if len(offers) > 1:
            raise UnresolvedScreen("最终翻卡广告按钮不唯一")
        claims = _reward_controls(screen, CLAIM_RE)
        if offers:
            offer = offers[0]
            amount = _ad_button_coins(offer)
            if amount is None or amount >= MIN_AD_OFFER_COINS:
                if offer.text in bonuses:
                    raise UnresolvedScreen("最终翻卡广告播放后仍未结算")
                bonuses.add(offer.text)
                session_obj.tap_line(offer, screen)
                _watch_ad(session_obj, return_to_reward=True)
                ads += 1
                screen = _flip_panel(session_obj)
                if _cumulative_reward(screen):
                    screen = _settle_modals(session_obj, initial=screen)
                continue
            claims = _claims_below(offer, claims)
        if len(claims) != 1:
            raise UnresolvedScreen("四张卡已翻开，最终领奖控件未确认")
        session_obj.tap_line(claims[0], screen)
        screen = session_obj.capture_in_app()
        if _reward_task(screen) == "flip-card":
            raise UnresolvedScreen("四张卡已翻开，领奖结果未确认")
    report("flip.result", f"确认翻开 {opened}/4 张，本次观看 {ads} 个广告", opened=opened,
           opened_before=started_with, ads=ads)
    return f"已翻开 {opened}/4 张，本次观看 {ads} 个广告"


def _task_chest(session_obj: Session) -> str:
    if not _ensure_fortune(session_obj):
        raise UnresolvedScreen("未确认福利中心")
    if not _find_entry(session_obj, "chest"):
        return "未找到宝箱入口"
    time.sleep(1.5)
    _settle_modals(session_obj)
    _ensure_fortune(session_obj)
    return "宝箱已开启"


AUTO_TASKS = {
    "check-in": _task_check_in,
    "listen": _task_listen,
    "ad": _task_ad,
    "video": _task_video,
    "watch": _task_watch,
    "flip-card": _task_flip,
    "chest": _task_chest,
}


def auto_run(session_obj: Session, key: str, *, new_round: bool = False) -> int:
    """自动执行单个任务；all 时按顺序执行本轮所有未排除任务。"""
    keys = [item for item in AUTO_ORDER if item not in EXCLUDED_TASKS] if key == "all" else [key]
    if new_round:
        data = load_round()
        if data and data.get("active"):
            report("auto.error", "上一轮还有未结算任务，先 end 或 abort", level="error")
            return 1
        _round_start(session_obj)

    failures = 0
    initial = None
    if key == "all":
        try:
            initial = session_obj.capture_in_app()
            pending = _reward_task(initial)
            if pending in keys:
                keys.remove(pending)
                keys.insert(0, pending)
        except (ForegroundError, AdbError, UnresolvedScreen, subprocess.TimeoutExpired):
            pass
    try:
        for item in keys:
            name = TASKS[item].name
            set_task(name)
            before = session_obj.coins(enter_fortune=True)
            earning = earnings.start("soda", name, before, task_key=item, serial=session_obj.adb.serial)
            status, note = "interrupted", ""
            stop_round = False
            ledger_started = False
            try:
                if new_round:
                    ledger_started = _round_begin(session_obj, name, "自动执行", coins=before) == 0
                note = AUTO_TASKS[item](session_obj)
                status = "complete"
            except (ForegroundError, AdbError, UnresolvedScreen, subprocess.TimeoutExpired) as error:
                stop_round = isinstance(error, UnconfirmedAd) or item in {"ad", "video"}
                status = "unconfirmed" if stop_round else "skipped"
                note = str(error)
                failures += 1
                report("auto.error", f"「{name}」执行中断：{error}", level="warn", task=item)
                if not stop_round:
                    try:
                        _ensure_fortune(session_obj, max_backs=4)
                    except (ForegroundError, AdbError, UnresolvedScreen, subprocess.TimeoutExpired):
                        pass
            finally:
                after = None
                try:
                    after = session_obj.coins()
                finally:
                    earning.finish(after, status)
                if ledger_started:
                    _round_end(session_obj, status, note, coins=after)
                if note:
                    report("auto.end", note, task=item, status=status)
            if stop_round:
                break

        if new_round:
            _round_finish(session_obj)
    finally:
        set_task("")

    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="汽水音乐福利任务工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("inspect")
    sub.add_parser("balance")
    p_tasks = sub.add_parser("tasks", help="列出任务分类")
    p_tasks.add_argument("--group", choices=TASK_FILTERS, default="all",
                         help="wait 筛选有冷却、累计时长或开放时间的任务")
    p_status = sub.add_parser("status", help="只读识别一个任务的当前状态")
    p_status.add_argument("task", choices=TASKS)
    p_status.add_argument("--ai", choices=AI_MODES, help="off 只用 OCR；auto 看不清时用 AI；on 强制调用")
    p_auto = sub.add_parser("auto", help="自动执行单个任务，或一轮全部任务")
    p_auto.add_argument("task", choices=[*AUTO_ORDER, "all"])
    p_auto.add_argument("--max-ads", type=int, help="兼容旧命令，续看统一按 100 金币判断")
    p_auto.add_argument("--round", action="store_true", help="开新一轮并逐项写账本")
    p_audit = sub.add_parser("audit", help="按任务手动执行命令，并记录一轮金币明细")
    p_audit.add_argument("--resume", type=Path, help="从未完成的走查日志继续")
    p_audit.add_argument("--group", choices=TASK_FILTERS, default="all", help="本轮可登记的任务类别")
    p_audit.add_argument("--ai", choices=AI_MODES, default="auto", help="只读 status 的 AI 模式，执行异常统一使用本地 AI")
    p_tap = sub.add_parser("tap")
    p_tap.add_argument("pattern")
    p_tap.add_argument("--x", nargs=2, type=float)
    p_tap.add_argument("--y", nargs=2, type=float)
    p_tap.add_argument("--near", help="同一任务行标题的 OCR 正则")
    p_tap.add_argument("--expect", type=re.compile, help="点击后应新出现的 OCR 文案（正则）")
    p_x = sub.add_parser("tapx")
    p_x.add_argument("x", type=float)
    p_x.add_argument("y", type=float)
    p_x.add_argument("--force", action="store_true", help="跳过画面稳定校验，仍须定位唯一控件")
    p_scroll = sub.add_parser("scroll")
    p_scroll.add_argument("direction", choices=["up", "down"], nargs="?", default="down")
    p_scroll.add_argument("--times", type=int, default=1)
    p_scroll.add_argument("--until", type=re.compile, help="滚动到指定 OCR 文案进入屏幕中部")
    p_wait = sub.add_parser("wait")
    p_wait.add_argument("pattern")
    p_wait.add_argument("--timeout", type=float, default=10.0)
    p_key = sub.add_parser("key")
    p_key.add_argument("name")
    p_vol = sub.add_parser("volume")
    p_vol.add_argument("low", type=float, nargs="?")
    p_vol.add_argument("high", type=float, nargs="?")
    p_shot = sub.add_parser("shot")
    p_shot.add_argument("path")
    p_back = sub.add_parser("back", help="任务完成后延迟退出：返回控件 → 本地 AI → 返回键")
    p_back.add_argument("--stage", default="任务页", help="日志里显示的阶段名")
    p_round = sub.add_parser("round", help="手工走查轮次：start / finish / show")
    p_round.add_argument("action", choices=["start", "finish", "show"])
    p_begin = sub.add_parser("begin", help="开始记录一项任务（自动读当前金币）")
    p_begin.add_argument("task")
    p_begin.add_argument("--note", default="")
    p_end = sub.add_parser("end", help="结算当前任务并记录金币变化")
    p_end.add_argument("--status", choices=["complete", "skipped"], default="complete")
    p_end.add_argument("--note", default="")
    p_note = sub.add_parser("note", help="给当前任务追加一条观察")
    p_note.add_argument("text")
    sub.add_parser("abort", help="丢弃当前未结算任务")
    sub.add_parser("ledger", help="打印本轮金币账本")
    sub.add_parser("new-session")

    return parser


def _run_cli(argv: Sequence[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "tasks":
        return list_tasks(args.group)
    if args.cmd == "new-session":
        session.reset()
        print("新会话已建立")
        return 0

    session_obj = Session()
    if args.cmd == "audit":
        return audit(session_obj, parser, args.resume, group=args.group, ai_mode=args.ai)
    return run_command(session_obj, args)


def run_command(session_obj: Session, args: argparse.Namespace) -> int:
    """执行一条命令；独立调用时补上 step.start / step.end 明细。"""
    if session_obj.audit:
        return _dispatch(session_obj, args)
    started = time.monotonic()
    run_log.event("step.start", cmd=args.cmd, capture_id=session_obj.capture_id)
    try:
        code = _dispatch(session_obj, args)
    except BaseException as error:
        run_log.error("step.error", error, cmd=args.cmd)
        raise
    run_log.event("step.end", cmd=args.cmd, exit_code=code,
                  duration_s=round(time.monotonic() - started, 2),
                  capture_id=session_obj.capture_id)
    return code


def _dispatch(session_obj: Session, args: argparse.Namespace) -> int:
    if args.cmd == "auto":
        return auto_run(session_obj, args.task, new_round=args.round)
    if args.cmd == "status":
        return inspect_task(session_obj, args.task, args.ai or "auto",
                            active_task=run_log.current().context.get("task_id") == args.task)
    if args.cmd == "inspect":
        screen = session_obj.capture()
        for line in screen.lines:
            print(f"{line.text}  @({line.x:.2f},{line.y:.2f})")
        return 0
    if args.cmd == "balance":
        screen = session_obj.capture_in_app()
        coins = coins_on(screen, retry_numbers=True)
        if coins is None:
            fresh = session_obj.recover("福利中心余额无法识别", screen, allow_dismiss=False)
            coins = coins_on(fresh, retry_numbers=True) if fresh is not None else None
        report("balance", f"金币余额 {coins if coins is not None else '未确认'}",
               coins=coins, capture_id=session_obj.capture_id)
        return 0 if coins is not None else 1
    if args.cmd == "tap":
        screen = session_obj.capture_in_app()
        x_range = tuple(args.x) if args.x else None
        y_range = tuple(args.y) if args.y else None
        for attempt in range(2):
            hits = task_targets(screen, args.pattern, x_range, y_range, args.near)
            if len(hits) > 1:
                print(f"找到多个匹配项，请用 --near、--x 或 --y 缩小范围：{args.pattern}")
                return 1
            stable = (session_obj.stable_target(screen, args.pattern, x_range, y_range, near=args.near)
                      if hits else None)
            if stable is not None and stable[0].text == hits[0].text:
                hit, screen = stable
                break
            reason = f"{'目标未稳定' if hits else '未找到'}：{args.pattern}"
            print(reason)
            if attempt == 0:
                fresh = session_obj.recover(reason)
                if fresh is not None:
                    screen = fresh
                    continue
            return 1
        if args.expect is not None:
            return 0 if session_obj.tap_verified(hit, screen, args.expect, x_range, y_range, near=args.near) else 1
        session_obj.tap_line(hit, screen)
        return 0
    if args.cmd == "tapx":
        return 0 if session_obj.tap_norm(args.x, args.y, force=args.force) else 1
    if args.cmd == "scroll":
        found = session_obj.scroll(args.direction == "down", args.times, target=args.until)
        if args.until is not None:
            if not found:
                fresh = session_obj.recover(f"滚动后未找到「{args.until.pattern}」")
                if fresh is not None:
                    matches = [line for line in fresh.lines if args.until.search(line.text)]
                    found = len(matches) == 1 and 0.23 <= matches[0].y <= 0.70
            print("目标已进入屏幕中部" if found else "未确认目标进入屏幕中部")
        return 0 if found else 1
    if args.cmd == "wait":
        hit = session_obj.wait_text(args.pattern, args.timeout)
        report("wait.result", f"{'找到' if hit else '超时'} {args.pattern}", pattern=args.pattern,
               matched_text=hit.text if hit else None, timeout=args.timeout,
               level="info" if hit else "warn")
        return 0 if hit else 1
    if args.cmd == "key":
        delay = session_obj.profile.pre_tap()
        run_log.event("key.wait", key=args.name, delay_seconds=delay)
        time.sleep(delay)
        session_obj.check_foreground()
        session_obj.adb.key(args.name)
        session_obj.save()
        return 0
    if args.cmd == "volume":
        session_obj.volume(args.low, args.high)
        return 0
    if args.cmd == "shot":
        Path(args.path).write_bytes(session_obj.capture().png)
        print("saved", args.path)
        return 0
    if args.cmd == "back":
        session_obj.exit_page(args.stage)
        return 0
    if args.cmd == "round":
        if args.action == "start":
            return _round_start(session_obj)
        if args.action == "finish":
            return _round_finish(session_obj)
        data = load_round()
        print(render_round(data) if data else "还没有开始走查")
        return 0 if data else 1
    if args.cmd == "ledger":
        data = load_round()
        print(render_round(data) if data else "还没有开始走查")
        return 0 if data else 1
    if args.cmd == "begin":
        return _round_begin(session_obj, args.task, args.note, coins=session_obj.coins())
    if args.cmd == "end":
        return _round_end(session_obj, args.status, args.note, coins=session_obj.coins())
    if args.cmd == "note":
        return _round_note(args.text)
    if args.cmd == "abort":
        return _round_abort()
    return 2


def audit(session_obj: Session, parser: argparse.ArgumentParser, resume: Path | None = None, *,
          group: str = "all", ai_mode: str = "auto") -> int:
    logger = run_log.current()
    if not logger.enabled or logger.path is None:
        report("audit.error", "逐项走查需要启用运行日志", level="error")
        return 1
    session_obj.audit = True
    current_coins = coins_on(session_obj.capture_in_app())
    if current_coins is None:
        fresh = session_obj.recover("开始走查时福利中心余额无法识别", allow_dismiss=False)
        current_coins = coins_on(fresh) if fresh is not None else None
    if current_coins is None:
        report("audit.error", "请先进入福利中心，确认「金币收益」和余额可见", level="warn")
        return 1
    start = current_coins
    tasks: list[dict] = []
    if resume is not None:
        events = [json.loads(line) for line in resume.read_text(encoding="utf-8").splitlines() if line]
        previous = next((item["data"] for item in reversed(events)
                         if item.get("app") == "soda" and item.get("event") == "audit.summary"), None)
        if previous is None or previous.get("finished"):
            report("audit.error", "续跑日志须包含一轮尚未结束的汽水音乐走查", level="warn")
            return 1
        start = previous["coins_start"]
        tasks = previous["tasks"]
        if any(item.get("status") == "interrupted" for item in tasks[:-1]):
            report("audit.resume.warning", "早先的中断任务保留原记录", level="warn")
        report("audit.resume", f"续接上一段记录，当前金币 {current_coins}", source=str(resume.resolve()),
               coins_at_resume=current_coins)
    active: dict | None = None
    ended = False
    end: Optional[int] = None

    def balance() -> Optional[int]:
        try:
            screen = session_obj.capture_in_app()
            coins = coins_on(screen)
            if coins is None:
                fresh = session_obj.recover("走查结算时余额无法识别", screen, allow_dismiss=False)
                coins = coins_on(fresh) if fresh is not None else None
            run_log.event("balance.sample", level="info" if coins is not None else "warn", coins=coins,
                          capture_id=session_obj.capture_id)
            return coins
        except (ForegroundError, AdbError, UnresolvedScreen, subprocess.TimeoutExpired) as error:
            run_log.error("balance.unavailable", error)
            return None

    def complete(reason: str, status: str) -> None:
        nonlocal active
        assert active is not None
        coins_after = balance()
        coins_before = active["coins_before"]
        delta = coins_after - coins_before if coins_before is not None and coins_after is not None else None
        result = {**active, "coins_after": coins_after, "coins_delta": delta, "status": status,
                  "reason": reason, "last_capture_id": session_obj.capture_id}
        result["duration_s"] = round(time.monotonic() - result.pop("started"), 3)
        if status == "complete":
            result["status"] = ("credited" if delta is not None and delta > 0 else
                                "unchanged" if delta == 0 else "unconfirmed")
        result["expected_coins"] = active.get("expected_coins")
        expected = result["expected_coins"]
        result["coins_shortfall"] = max(0, expected - delta) if expected is not None and delta is not None else None
        tasks.append(result)
        report("task.end", f"{result['task']} | 金币变化 {delta if delta is not None else '未确认'} | {reason}",
               level="info" if result["status"] in {"credited", "skipped"} and not result["coins_shortfall"]
               else "warn", **result)
        logger.context.clear()
        active = None

    allowed = {key for key, task in TASKS.items() if group_matches(task, group)}
    report("audit.start", f"走查开始，金币 {start}。日志 {logger.path}", coins_start=start,
           group=group, ai_mode=ai_mode, configured_tasks=[key for key in TASKS if key in allowed])
    report("audit.help", "命令：begin 任务 ID、status 任务 ID、expect 金币数、note 观察、end 结果、skip 原因、reconcile 中断后结果、finish。")
    try:
        while True:
            try:
                words = shlex.split(input("soda> "))
            except EOFError:
                break
            except ValueError as error:
                report("audit.command.invalid", str(error), level="warn")
                continue
            if not words:
                continue
            if not logger.enabled:
                report("audit.error", "运行日志不可写，走查已停止", level="error")
                break
            command, *args = words
            detail = " ".join(args)
            if command == "finish" and not args:
                ended = True
                break
            if command == "begin" and detail and active is None:
                task_id = next((key for key, task in TASKS.items()
                                if detail in (key, task.name) or re.fullmatch(task.title, detail)), None)
                if group != "all" and task_id not in allowed:
                    report("audit.command.invalid", f"该任务不在当前分类 {group} 内，请用 tasks 查看任务 ID", level="warn")
                    continue
                coins = balance()
                label = TASKS[task_id].name if task_id is not None else detail
                active = {"task": label, "task_id": task_id, "attempt": len(tasks) + 1, "coins_before": coins,
                          "first_capture_id": session_obj.capture_id, "started": time.monotonic()}
                logger.context.update(task=label, task_id=task_id, attempt=active["attempt"])
                report("task.start", f"开始记录 {label}，金币 {coins if coins is not None else '未确认'}",
                       coins_before=coins, first_capture_id=session_obj.capture_id)
                continue
            if command == "note" and detail and active is not None:
                report("task.note", detail, capture_id=session_obj.capture_id)
                continue
            if command == "expect" and len(args) == 1 and re.fullmatch(r"[0-9]+", args[0]) and active is not None:
                active["expected_coins"] = int(args[0])
                report("task.expected", f"页面预期 {args[0]} 金币", expected_coins=int(args[0]),
                       capture_id=session_obj.capture_id)
                continue
            if command == "reconcile" and detail and active is None and tasks and tasks[-1]["status"] == "interrupted":
                recovered = tasks[-1]
                coins = balance()
                baseline = recovered["coins_before"]
                delta = coins - baseline if coins is not None and baseline is not None else None
                recovered.update(coins_after=coins, coins_delta=delta, reason=detail,
                                 reconciled_capture_id=session_obj.capture_id,
                                 status="unchanged" if delta == 0 else "unconfirmed")
                expected = recovered.get("expected_coins")
                recovered["coins_shortfall"] = max(0, expected - delta) if expected is not None and delta is not None else None
                report("task.reconcile", f"{recovered['task']} | 恢复后金币变化 {delta} | {detail}", **recovered)
                continue
            if command in {"end", "skip"} and detail and active is not None:
                complete(detail, "skipped" if command == "skip" else "complete")
                continue
            if command not in {"inspect", "balance", "status", "tap", "tapx", "scroll", "wait", "key",
                               "volume", "back"}:
                report("audit.command.invalid", f"无法执行：{command}", level="warn")
                continue
            if active is None and command not in {"inspect", "balance", "status"}:
                report("audit.command.invalid", "请先用 begin 指定当前任务", level="warn")
                continue
            started = time.monotonic()
            run_log.event("step.start", argv=words)
            try:
                parsed = parser.parse_args(words)
                if command == "status" and parsed.ai is None:
                    parsed.ai = ai_mode
                code = run_command(session_obj, parsed)
            except SystemExit as error:
                code = error.code
            except (RuntimeError, ValueError, subprocess.TimeoutExpired, OSError) as error:
                run_log.error("step.error", error, argv=words)
                report("step.message", str(error), level="warn")
                code = 1
            run_log.event("step.end", level="info" if code == 0 else "warn", argv=words, exit_code=code,
                          capture_id=session_obj.capture_id, duration_s=round(time.monotonic() - started, 3))
    except KeyboardInterrupt:
        report("audit.interrupted", "本轮走查中断，保存已取得的结果", level="warn")
    finally:
        if active is not None:
            complete("走查结束时该任务仍未关闭记录", "interrupted")
        end = balance()
        delta = end - start if end is not None else None
        attributed = sum(item["coins_delta"] for item in tasks if item["status"] == "credited")
        report("audit.summary",
               f"本轮金币 {start} → {end if end is not None else '未确认'}，变化 {delta if delta is not None else '未确认'}",
               level="info" if ended and end is not None else "warn",
               coins_start=start, coins_end=end, coins_delta=delta, positive_task_delta=attributed,
               residual_delta=delta - attributed if delta is not None else None,
               tasks=tasks, finished=ended, log_path=logger.path)
    return 0 if ended and end is not None and logger.enabled else 1


def main(argv: Sequence[str]) -> int:
    logger = run_log.init_run("soda", list(argv))
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
