"""JSON 规则引擎：加载、合并、校验、增删规则。

规则目录结构（可用环境变量 DROIDFLEET_RULES_DIR 覆盖）：

    rules/
      human/     拟人化参数（节奏、触摸、内容消费、任务节奏、音量）
      safety/    安全黑名单
      apps/      各应用专属参数（含 human.* 覆盖）

每个文件格式：

    {
      "id": "human.rhythm",
      "description": "节奏参数",
      "priority": 100,
      "values": {"human.rhythm.speed.range": [0.85, 1.2]},
      "schema": {"human.rhythm.speed.range": {"type": "range", "min": 0, "max": 2}}
    }

合并顺序：priority 升序，其次文件路径字典序；后者覆盖前者。
追加规则 = 新增一个 json 文件（或往 values 里加键）；
删除规则 = 删除文件（或删掉对应键）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RULES_DIR = PACKAGE_ROOT / "rules"
_MISSING = object()


class RuleError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuleFile:
    path: Path
    id: str
    description: str
    priority: int
    values: dict[str, Any]
    schema: dict[str, Any]

    @property
    def name(self) -> str:
        return self.path.name


def rules_dir() -> Path:
    override = os.environ.get("DROIDFLEET_RULES_DIR")
    return Path(override).expanduser().resolve() if override else DEFAULT_RULES_DIR


def _parse_file(path: Path) -> RuleFile:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuleError(f"{path} 不是合法 JSON：{error}") from error
    if not isinstance(data, dict):
        raise RuleError(f"{path} 顶层必须是对象")
    values = data.get("values", {})
    schema = data.get("schema", {})
    if not isinstance(values, dict) or not isinstance(schema, dict):
        raise RuleError(f"{path} 的 values/schema 必须是对象")
    return RuleFile(
        path=path,
        id=str(data.get("id") or path.stem),
        description=str(data.get("description", "")),
        priority=int(data.get("priority", 100)),
        values=values,
        schema=schema,
    )


def discover(root: Path | None = None) -> list[RuleFile]:
    base = root or rules_dir()
    if not base.exists():
        raise RuleError(f"规则目录不存在：{base}")
    files = [_parse_file(path) for path in sorted(base.rglob("*.json"))]
    files.sort(key=lambda item: (item.priority, str(item.path)))
    return files


def _check_value(key: str, value: Any, spec: dict[str, Any], origin: Path) -> list[str]:
    errors: list[str] = []
    kind = spec.get("type")
    if kind in {"range", "point"}:
        if (not isinstance(value, (list, tuple)) or len(value) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)):
            want = "[x, y]" if kind == "point" else "[low, high]"
            return [f"{key} 需要 {want}（{origin.name}）"]
        if kind == "range" and value[0] > value[1]:
            errors.append(f"{key} 下界大于上界（{origin.name}）")
    elif kind == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return [f"{key} 需要数字（{origin.name}）"]
    elif kind == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return [f"{key} 需要整数（{origin.name}）"]
    elif kind == "string":
        if not isinstance(value, str):
            return [f"{key} 需要字符串（{origin.name}）"]
    elif kind == "boolean":
        if not isinstance(value, bool):
            return [f"{key} 需要布尔值（{origin.name}）"]
    elif kind == "list":
        if not isinstance(value, list):
            return [f"{key} 需要列表（{origin.name}）"]
    elif kind is not None:
        errors.append(f"{key} 的 type 未知：{kind}（{origin.name}）")

    if kind in {"number", "integer", "range", "point"}:
        nums = value if isinstance(value, (list, tuple)) else [value]
        low, high = spec.get("min"), spec.get("max")
        for num in nums:
            if low is not None and num < low:
                errors.append(f"{key} 小于下限 {low}（{origin.name}）")
            if high is not None and num > high:
                errors.append(f"{key} 大于上限 {high}（{origin.name}）")
    if kind == "list" and spec.get("choices"):
        allowed = set(spec["choices"])
        bad = [item for item in value if item not in allowed]
        if bad:
            errors.append(f"{key} 含未允许的值 {bad}（{origin.name}）")
    return errors


class Rules:
    def __init__(self, files: Iterable[RuleFile], app: str | None = None) -> None:
        self.files = list(files)
        self.app = app
        merged: dict[str, Any] = {}
        schema: dict[str, Any] = {}
        for item in self.files:
            if self.app and item.path.parent.name == "apps" and item.path.stem != self.app:
                continue
            merged.update(item.values)
            schema.update(item.schema)
        self.values = merged
        self.schema = schema

    def get(self, key: str, default: Any = _MISSING) -> Any:
        if key in self.values:
            return self.values[key]
        if default is _MISSING:
            raise RuleError(f"缺少规则：{key}")
        return default

    def range_of(self, key: str, default: tuple[float, float] | None = None) -> tuple[float, float]:
        value = self.get(key, default)
        if value is None:
            raise RuleError(f"缺少区间规则：{key}")
        low, high = value
        return float(low), float(high)

    def floats(self, key: str, default: tuple[float, float] | None = None) -> tuple[float, float]:
        return self.range_of(key, default)

    def list_of(self, key: str, default: Sequence[Any] | None = None) -> list[Any]:
        value = self.get(key, list(default) if default is not None else None)
        if value is None:
            raise RuleError(f"缺少列表规则：{key}")
        return list(value)

    def num(self, key: str, default: float | None = None) -> float:
        value = self.get(key, default)
        if value is None:
            raise RuleError(f"缺少数值规则：{key}")
        return float(value)

    def integer(self, key: str, default: int | None = None) -> int:
        value = self.get(key, default)
        if value is None:
            raise RuleError(f"缺少整数规则：{key}")
        return int(value)

    def section(self, prefix: str) -> dict[str, Any]:
        head = prefix.rstrip(".") + "."
        return {key[len(head):]: value for key, value in self.values.items() if key.startswith(head)}

    def validate(self) -> list[str]:
        errors: list[str] = []
        for item in self.files:
            for key, spec in item.schema.items():
                if key in item.values:
                    errors.extend(_check_value(key, item.values[key], spec, item.path))
                else:
                    errors.append(f"{key} 在 schema 中声明但 values 缺失（{item.path.name}）")
        return errors

    def dump(self) -> dict[str, Any]:
        return dict(sorted(self.values.items()))


_CACHE: dict[tuple[str, str | None], tuple[tuple[Any, ...], Rules]] = {}


def _fingerprint(base: Path) -> tuple[Any, ...]:
    return tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in sorted(base.rglob("*.json")))


def load_rules(app: str | None = None, root: Path | None = None, reload: bool = False) -> Rules:
    base = (root or rules_dir()).resolve()
    cache_key = (str(base), app)
    fingerprint = _fingerprint(base)
    if not reload and cache_key in _CACHE and _CACHE[cache_key][0] == fingerprint:
        return _CACHE[cache_key][1]
    instance = Rules(discover(base), app=app)
    _CACHE[cache_key] = (fingerprint, instance)
    return instance


def _file_for(relative: str, base: Path) -> Path:
    path = (base / relative).resolve()
    if base not in path.parents:
        raise RuleError(f"规则文件必须位于 {base} 内：{relative}")
    return path


def _write_values(path: Path, values: dict[str, Any]) -> None:
    data: dict[str, Any] = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("id", path.stem)
    data.setdefault("description", "")
    data.setdefault("priority", 100)
    data["values"] = values
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _coerce(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _cmd_list(args: argparse.Namespace) -> int:
    base = rules_dir()
    for item in discover(base):
        print(f"{item.id:<24} priority={item.priority:<4} {item.path.relative_to(base)}  ({len(item.values)} 项)")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    base = rules_dir()
    picks = [item for item in discover(base) if item.id == args.id or item.path.stem == args.id]
    if not picks:
        raise RuleError(f"没有找到规则文件：{args.id}")
    for item in picks:
        print(f"# {item.id}  {item.path.relative_to(base)}")
        print(json.dumps(item.values, ensure_ascii=False, indent=2))
    return 0


def _cmd_get(args: argparse.Namespace) -> int:
    print(json.dumps(load_rules(args.app).get(args.key), ensure_ascii=False))
    return 0


def _cmd_dump(args: argparse.Namespace) -> int:
    print(json.dumps(load_rules(args.app).dump(), ensure_ascii=False, indent=2))
    return 0


def _cmd_set(args: argparse.Namespace) -> int:
    base = rules_dir()
    path = _file_for(args.file, base)
    values: dict[str, Any] = {}
    if path.exists():
        values = dict(json.loads(path.read_text(encoding="utf-8")).get("values", {}))
    values[args.key] = _coerce(args.value)
    _write_values(path, values)
    errors = load_rules(reload=True).validate()
    if errors:
        print("规则已写入，但校验有告警：", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
    print(f"{args.key} = {json.dumps(values[args.key], ensure_ascii=False)}  -> {path.relative_to(base)}")
    return 0


def _cmd_unset(args: argparse.Namespace) -> int:
    base = rules_dir()
    path = _file_for(args.file, base)
    if not path.exists():
        raise RuleError(f"文件不存在：{args.file}")
    data = json.loads(path.read_text(encoding="utf-8"))
    values = dict(data.get("values", {}))
    if args.key not in values:
        raise RuleError(f"{args.file} 中没有 {args.key}")
    del values[args.key]
    _write_values(path, values)
    print(f"已删除 {args.key}  ({args.file})")
    return 0


def _cmd_add_file(args: argparse.Namespace) -> int:
    base = rules_dir()
    path = _file_for(args.file, base)
    if path.exists():
        raise RuleError(f"文件已存在：{args.file}")
    payload = {
        "id": args.id or path.stem,
        "description": args.description or "",
        "priority": args.priority,
        "values": {},
        "schema": {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已创建 {path.relative_to(base)}（id={payload['id']}）")
    return 0


def _cmd_remove_file(args: argparse.Namespace) -> int:
    base = rules_dir()
    path = _file_for(args.file, base)
    if not path.exists():
        raise RuleError(f"文件不存在：{args.file}")
    path.unlink()
    print(f"已删除 {path.relative_to(base)}")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    instance = load_rules(args.app, reload=True)
    errors = instance.validate()
    if not errors:
        print(f"规则校验通过：{len(instance.files)} 个文件，{len(instance.values)} 个键")
        return 0
    for error in errors:
        print(f"- {error}", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m droidfleet.rules", description="CoinFlow 规则管理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出所有规则文件")

    show = sub.add_parser("show", help="查看某个规则文件")
    show.add_argument("id")

    get = sub.add_parser("get", help="读取合并后的单个键")
    get.add_argument("key")
    get.add_argument("--app")

    dump = sub.add_parser("dump", help="输出合并后的全部规则")
    dump.add_argument("--app")

    set_cmd = sub.add_parser("set", help="写入/覆盖一个键")
    set_cmd.add_argument("key")
    set_cmd.add_argument("value")
    set_cmd.add_argument("--file", required=True, help="相对 rules/ 的文件路径")

    unset = sub.add_parser("unset", help="删除一个键")
    unset.add_argument("key")
    unset.add_argument("--file", required=True)

    add = sub.add_parser("add-file", help="新增规则文件")
    add.add_argument("file")
    add.add_argument("--id")
    add.add_argument("--description")
    add.add_argument("--priority", type=int, default=100)

    remove = sub.add_parser("remove-file", help="删除规则文件")
    remove.add_argument("file")

    validate = sub.add_parser("validate", help="校验全部规则")
    validate.add_argument("--app")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "list": _cmd_list,
        "show": _cmd_show,
        "get": _cmd_get,
        "dump": _cmd_dump,
        "set": _cmd_set,
        "unset": _cmd_unset,
        "add-file": _cmd_add_file,
        "remove-file": _cmd_remove_file,
        "validate": _cmd_validate,
    }
    from droidfleet import log as run_log

    logger = run_log.init_run("rules", list(argv) if argv is not None else sys.argv[1:])
    try:
        code = handlers[args.cmd](args)
    except SystemExit as exc:
        logger.close(status="exit", exit_code=getattr(exc, "code", None))
        raise
    except RuleError as error:
        logger.event("rules.error", level="error", command=args.cmd, error=str(error))
        logger.close(status="error", exit_code=2)
        print(f"错误：{error}", file=sys.stderr)
        return 2
    except BaseException as error:
        logger.error("run.error", error)
        logger.close(status="error")
        raise
    logger.event("rules.command", command=args.cmd, exit_code=code)
    logger.close(status="ok", exit_code=code)
    return code


if __name__ == "__main__":
    sys.exit(main())
