"""任务收尾：先找页面上的返回/退出控件，找不到再让本地 AI 指认。

设计原则与异常恢复一致——模型只提供候选：
1. 模型给的坐标必须能在当前 OCR 结果里对回同一行文字，才允许点击；
2. 对不上文字时，仅当调用方允许盲点，且点位附近没有任何领奖/支付类文案，才使用原始坐标；
3. 任何可能放弃奖励或改变账号状态的文案一律拒绝。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from droidfleet import log as run_log
from droidfleet import ollama

_STRIP = " \t\u3000›>|《》<>»"
_GLYPH = {"×", "✕", "X", "x", "╳", "‹", "〈", "«", "＜", "←", "↩", "⇦", "巛"}
_LEAD = "‹〈«＜←↩⇦ "


@dataclass(frozen=True)
class ExitTarget:
    kind: str                      # "line"：对上了 OCR 行；"point"：只有模型坐标
    line: Any = None
    point: Optional[tuple[float, float]] = None
    label: str = ""


def is_exit_text(text: str) -> bool:
    """是否是安全的离开控件文案（返回/退出/关闭/×/箭头）。"""
    cleaned = text.strip().strip(_STRIP).strip()
    if not cleaned or ollama.EXIT_RISKY.search(cleaned):
        return False
    if cleaned in _GLYPH:
        return True
    bare = cleaned.lstrip(_LEAD).strip()
    return bare in _GLYPH or bool(ollama.EXIT_PREFIX.match(bare))


def find_exit(lines: Sequence[Any]) -> Optional[Any]:
    """按从上到下、从左到右的顺序挑一个离开控件。"""
    hits = [line for line in lines if is_exit_text(getattr(line, "text", ""))]
    if not hits:
        return None
    return min(hits, key=lambda line: (float(getattr(line, "y", 0.0)), float(getattr(line, "x", 0.0))))


def match_decision(lines: Sequence[Any], decision: ollama.ExitDecision, *,
                   x_scale: float = 1.0, y_scale: float = 1.0) -> Optional[Any]:
    """把模型给的 0–1000 坐标对回当前 OCR 行，坐标或文案不吻合就放弃。"""
    target_x = decision.x / 1000.0
    target_y = decision.y / 1000.0
    best: tuple[float, Any] | None = None
    for line in lines:
        raw = getattr(line, "text", "")
        if not is_exit_text(raw):
            continue
        cleaned = raw.strip().strip(_STRIP).strip().lstrip(_LEAD).strip()
        x = float(getattr(line, "x", 0.0)) * x_scale
        y = float(getattr(line, "y", 0.0)) * y_scale
        if abs(x - target_x) > 0.10 or abs(y - target_y) > 0.09:
            continue
        if decision.label and cleaned not in _GLYPH and not (
                cleaned.startswith(decision.label) or decision.label.startswith(cleaned)):
            continue
        distance = abs(x - target_x) + abs(y - target_y)
        if best is None or distance < best[0]:
            best = (distance, line)
    return best[1] if best is not None else None


def risky_at(lines: Sequence[Any], x: float, y: float, *,
             x_scale: float = 1.0, y_scale: float = 1.0) -> bool:
    """点位附近是否存在危险的文案（领奖、支付、下载等）。"""
    for line in lines:
        text = getattr(line, "text", "")
        if not ollama.EXIT_RISKY.search(text):
            continue
        lx = float(getattr(line, "x", 0.0)) * x_scale
        ly = float(getattr(line, "y", 0.0)) * y_scale
        if abs(lx - x) <= 0.09 and abs(ly - y) <= 0.06:
            return True
    return False


def locate_with_ai(app: str, stage: str, png: bytes, lines: Sequence[Any], *,
                   timeout: float = 25, x_scale: float = 1.0, y_scale: float = 1.0,
                   allow_blind: bool = True) -> Optional[ExitTarget]:
    """让本地 AI 指认退出控件，并用当前截图复核。"""
    decision = ollama.locate_exit(app, stage, png, timeout=timeout)
    if decision is None or decision.action != "tap":
        return None
    line = match_decision(lines, decision, x_scale=x_scale, y_scale=y_scale)
    if line is not None:
        run_log.event("ai.exit.result", stage=stage, outcome="located", label=decision.label,
                      x=decision.x, y=decision.y)
        return ExitTarget("line", line=line, label=decision.label)
    if not allow_blind:
        run_log.event("ai.exit.result", level="warn", stage=stage, outcome="unverified",
                      reasoning="禁止盲点，模型坐标没有对应控件")
        return None
    point = (decision.x / 1000.0, decision.y / 1000.0)
    if not decision.label.startswith("返回"):
        run_log.event("ai.exit.result", level="warn", stage=stage, outcome="rejected",
                      reason="未复核的坐标只允许返回箭头，其余控件必须对上 OCR 文字",
                      label=decision.label)
        return None
    if not (0.02 <= point[0] <= 0.30 and 0.02 <= point[1] <= 0.25):
        run_log.event("ai.exit.result", level="warn", stage=stage, outcome="rejected",
                      reason="返回箭头不在左上角安全区域", label=decision.label,
                      x=decision.x, y=decision.y)
        return None
    if risky_at(lines, *point, x_scale=x_scale, y_scale=y_scale):
        run_log.event("ai.exit.result", level="warn", stage=stage, outcome="rejected",
                      reason="模型坐标附近有危险文案")
        return None
    run_log.event("ai.exit.result", stage=stage, outcome="blind_point", label=decision.label,
                  x=decision.x, y=decision.y)
    return ExitTarget("point", point=point, label=decision.label)


def ai_enabled(rules: Any) -> bool:
    try:
        return bool(rules.get("human.after_task.ai_fallback", True))
    except Exception:
        return True


def blind_tap_enabled(rules: Any) -> bool:
    try:
        return bool(rules.get("human.after_task.ai_blind_tap", True))
    except Exception:
        return True
