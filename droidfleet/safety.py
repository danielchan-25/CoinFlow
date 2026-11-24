"""规则驱动的安全拦截：危险按钮永不点击。"""
from __future__ import annotations

import re
import weakref

from droidfleet.rules import Rules, load_rules

_CACHE: "weakref.WeakKeyDictionary[Rules, tuple[list[re.Pattern], set[str]]]" = weakref.WeakKeyDictionary()


class SafetyBlock(RuntimeError):
    pass


def _compiled(rules: Rules) -> tuple[list[re.Pattern], set[str]]:
    cached = _CACHE.get(rules)
    if cached is not None:
        return cached
    patterns = [re.compile(item) for item in rules.list_of("safety.deny.regex", [])]
    never = set(rules.list_of("safety.never_tap", []))
    _CACHE[rules] = (patterns, never)
    return patterns, never


def check(text: str, rules: Rules | None = None) -> str | None:
    rules = rules or load_rules()
    patterns, never = _compiled(rules)
    stripped = text.strip()
    if stripped in never:
        return f"命中安全黑名单：{stripped}"
    for pattern in patterns:
        match = pattern.search(stripped)
        if match:
            return f"命中危险规则 /{pattern.pattern}/：{stripped}"
    return None


def guard(text: str, rules: Rules | None = None) -> None:
    reason = check(text, rules)
    if reason:
        try:
            from droidfleet import log

            log.event("safety.block", level="warn", text=text, reason=reason)
        except Exception:
            pass
        raise SafetyBlock(reason)
