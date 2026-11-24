"""CoinFlow：adb + OCR 驱动的手机任务工具包。

模块划分：
    rules     规则引擎（JSON 规则可增删）
    human     拟人化决策器（全部参数来自规则）
    device    adb 封装与设备音量控制
    ocr       截屏 + Vision OCR 封装
    session   跨命令持久化的拟人会话状态
    safety    规则驱动的安全拦截
    apps      各应用脚本（soda_music / douyin / kuaishou）
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from droidfleet.rules import RuleError, Rules, load_rules

_LAZY = {"RuleError", "Rules", "load_rules"}

__all__ = sorted(_LAZY)


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from droidfleet import rules as _rules

        return getattr(_rules, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
