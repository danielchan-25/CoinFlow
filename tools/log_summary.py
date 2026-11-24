#!/usr/bin/env python3
"""汇总运行日志（JSONL），用于事后分析。

用法：
    python3 tools/log_summary.py                     # 汇总 logs/ 下全部
    python3 tools/log_summary.py --app soda          # 只看某个应用
    python3 tools/log_summary.py --last 5            # 只看最近 5 份
    python3 tools/log_summary.py path/to/a.jsonl ... # 指定文件
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def summarize(path: Path) -> None:
    rows = load(path)
    if not rows:
        print(f"{path}: 空文件")
        return
    events = Counter(row.get("event") for row in rows)
    levels = Counter(row.get("level") for row in rows)
    start = rows[0]
    end = next((row for row in reversed(rows) if row.get("event") == "run.end"), None)
    taps = [row for row in rows if row.get("event") == "tap"]
    adb = [row for row in rows if row.get("event") == "adb"]
    slow = sorted((row for row in rows if row.get("event") == "adb"),
                  key=lambda row: row.get("data", {}).get("ms", 0), reverse=True)[:3]
    print(f"\n== {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    print(f"  应用: {start.get('app')}  开始: {start.get('ts')}")
    if end:
        print(f"  结束: {end.get('ts')}  状态: {end.get('data', {}).get('status')}  "
              f"耗时: {end.get('data', {}).get('duration_s')}s")
    summary = next((row.get("data", {}) for row in reversed(rows) if row.get("event") == "audit.summary"), None)
    if summary is not None:
        def amount(value: int | None) -> str:
            return "未确认" if value is None else f"{value:+d}"

        start_balance = summary.get("coins_start")
        end_balance = summary.get("coins_end")
        print(f"  金币 {start_balance if start_balance is not None else '未确认'} → "
              f"{end_balance if end_balance is not None else '未确认'}，"
              f"净变化 {amount(summary.get('coins_delta'))}，未归因 {amount(summary.get('residual_delta'))}")
        for task in summary.get("tasks", []):
            expected = task.get("expected_coins")
            detail = f"，预期 {expected}，差额 {task.get('coins_shortfall')}" if expected is not None else ""
            print(f"    - {task.get('task')} [{task.get('status')}] {amount(task.get('coins_delta'))} "
                  f"({task.get('duration_s')}s){detail} {task.get('reason')}")
    print(f"  事件: {dict(events.most_common())}")
    print(f"  级别: {dict(levels)}")
    if taps:
        texts = Counter(row.get("data", {}).get("text", "?") for row in taps)
        print(f"  点击 {len(taps)} 次，Top: {texts.most_common(5)}")
    if adb:
        print(f"  adb {len(adb)} 次；最慢: " + ", ".join(
            f"{row.get('data', {}).get('args', [])[-1] if row.get('data', {}).get('args') else '?'}"
            f" {row.get('data', {}).get('ms')}ms" for row in slow))
    errors = [row for row in rows if row.get("level") in ("error", "warn")]
    if errors:
        print(f"  告警/错误 {len(errors)} 条，最近 3 条:")
        for row in errors[-3:]:
            data = row.get("data", {})
            detail = data.get("reason") or data.get("error") or data.get("line") or data.get("message")
            if detail is None and data.get("slow"):
                detail = f"耗时 {data.get('ms')} 毫秒"
            print(f"    - {row.get('ts')} {row.get('event')}: "
                  f"{detail or '详见事件字段'}")


def collect(args: argparse.Namespace) -> list[Path]:
    if args.paths:
        return [Path(item).expanduser() for item in args.paths]
    pattern = str(ROOT / "logs" / (args.app or "*") / "*.jsonl")
    files = [Path(item) for item in glob.glob(pattern) if not item.endswith("latest.jsonl")]
    files.sort(key=lambda item: os.path.getmtime(item))
    return files[-args.last:] if args.last else files


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总 JSONL 运行日志")
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--app")
    parser.add_argument("--last", type=int, default=0)
    args = parser.parse_args()
    files = collect(args)
    if not files:
        print("没有日志文件")
        return 1
    for path in files:
        summarize(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
