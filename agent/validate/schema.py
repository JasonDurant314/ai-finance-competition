"""JSON Schema 校验 —— 契约执行的强制手段。

产物必须通过 Schema 校验才能写入 runs 目录。这是「无契约不通过」的落点。

依赖 ``jsonschema`` 时用严格校验；否则回退到 gateway 内的轻量校验器
（只校验结构骨架）。**生产环境应安装 jsonschema**。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agent.runtime.errors import ContractViolation

try:  # pragma: no cover - 取决于环境
    import jsonschema as _js

    _HAS_JSONSCHEMA = True
except ImportError:  # pragma: no cover
    _js = None  # type: ignore[assignment]
    _HAS_JSONSCHEMA = False


def load_schema(path: str | Path) -> dict[str, Any]:
    """从文件加载 JSON Schema。"""
    p = Path(path)
    if not p.exists():
        raise ContractViolation("Schema 文件不存在", path=str(p))
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractViolation("Schema 文件不是合法 JSON", path=str(p), error=str(exc)) from exc


def validate_json(
    data: Any,
    schema: dict[str, Any],
    *,
    what: str = "产物",
    strict: bool = True,
) -> list[str]:
    """校验 JSON 数据。返回错误列表；空列表表示通过。

    Args:
        strict: True 时错误直接抛 ``ContractViolation``；False 时返回错误列表。
    """
    errors: list[str] = []

    if _HAS_JSONSCHEMA:
        validator_cls = _js.Draft202012Validator
        validator = validator_cls(schema)
        for err in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
            loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
            errors.append(f"{loc}: {err.message}")
    else:  # pragma: no cover - 回退路径
        from agent.model.gateway import _validate_required

        errors = _validate_required(data, schema)

    if errors and strict:
        raise ContractViolation(
            f"{what} 未通过 Schema 校验（{len(errors)} 处）",
            errors=errors[:10],
        )
    return errors


def validate_model_instance(obj: BaseModel, *, what: str = "对象") -> None:
    """对 Pydantic 实例做一次往返校验。

    Pydantic 已在构造时校验，此处的价值在于**捕获
    ``model_construct`` 等绕过校验的路径**以及字段被后置修改的情况。
    """
    try:
        type(obj).model_validate(obj.model_dump())
    except Exception as exc:
        raise ContractViolation(
            f"{what} 校验失败：{exc}", type=type(obj).__name__
        ) from exc


def validate_products(
    run_dir: str | Path,
    schema_dir: str | Path,
    *,
    mapping: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """批量校验一个 run 目录下的产物。

    Args:
        run_dir: ``runs/<run_id>/``
        schema_dir: ``configs/schemas/``
        mapping: ``{相对产物路径: schema 文件名}``。为 None 时按
            ``<产物名>.schema.json`` 自动匹配。

    Returns:
        ``{产物路径: 错误列表}``。全部通过时值均为空列表。
    """
    run_dir = Path(run_dir)
    schema_dir = Path(schema_dir)
    results: dict[str, list[str]] = {}

    if mapping is None:
        mapping = {}
        for schema_file in schema_dir.glob("*.schema.json"):
            stem = schema_file.name.replace(".schema.json", "")
            for candidate in run_dir.rglob(f"{stem}.json"):
                mapping[str(candidate.relative_to(run_dir)).replace("\\", "/")] = schema_file.name

    for rel_path, schema_name in mapping.items():
        target = run_dir / rel_path
        if not target.exists():
            results[rel_path] = [f"产物不存在：{rel_path}"]
            continue
        schema = load_schema(schema_dir / schema_name)
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            results[rel_path] = [f"不是合法 JSON：{exc}"]
            continue
        results[rel_path] = validate_json(data, schema, what=rel_path, strict=False)

    return results


def has_jsonschema() -> bool:
    return _HAS_JSONSCHEMA


__all__ = [
    "load_schema",
    "validate_json",
    "validate_model_instance",
    "validate_products",
    "has_jsonschema",
]
