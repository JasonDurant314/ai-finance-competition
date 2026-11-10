"""工具注册表 —— 受控工具环境的唯一入口。

每个工具用 ``@tool`` 声明元数据，注册表自动导出 JSON Schema 供模型调用。
``determinism`` 与 ``side_effect`` 字段是**可复现性声明**：
调度器据此决定某个工具调用是否允许重试、是否需要落盘。

设计约束：工具必须是**纯函数式**的 —— 相同入参必然得到相同出参。
任何带随机性、时间依赖或网络依赖的工具必须在 ``determinism`` 中标注，
并接受调度器的种子注入。
"""

from __future__ import annotations

import inspect
import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Literal, get_type_hints

from pydantic import BaseModel

from agent.runtime.errors import ContractViolation, ToolExecutionError, ToolNotFoundError
from agent.trace.tracer import Tracer, payload_hash

SideEffect = Literal["none", "read", "write", "network"]
Determinism = Literal["deterministic", "seeded", "nondeterministic"]
Cost = Literal["free", "cheap", "expensive"]


@dataclass
class ToolSpec:
    """工具的完整元数据。模型看到的是 ``to_openai_schema()`` 的结果。"""

    name: str
    description: str
    func: Callable[..., Any]
    params_model: type[BaseModel] | None = None
    returns_model: type[BaseModel] | None = None
    side_effect: SideEffect = "none"
    determinism: Determinism = "deterministic"
    cost: Cost = "free"
    timeout_s: float = 60.0
    requires_evidence: bool = False
    """True 表示该工具的产出必须带证据锚点，否则契约违规。"""
    tags: list[str] = field(default_factory=list)

    def to_openai_schema(self) -> dict[str, Any]:
        """导出 OpenAI function-calling 格式的 schema。

        注意：**保留 ``$defs``** —— 嵌套 Pydantic 模型会生成 ``$ref``
        指向 ``#/$defs/...``，删掉 ``$defs`` 会导致 schema 失效。
        """
        if self.params_model is not None:
            schema = self.params_model.model_json_schema()
            schema.pop("title", None)
        else:
            schema = {"type": "object", "properties": {}, "additionalProperties": False}
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }

    def invoke(self, args: dict[str, Any]) -> Any:
        """校验入参后执行。入参非法直接拒绝，不交给函数体。"""
        if self.params_model is not None:
            try:
                validated = self.params_model.model_validate(args)
            except Exception as exc:
                raise ContractViolation(
                    f"工具 {self.name} 入参不符合 Schema", error=str(exc)[:400]
                ) from exc
            return self.func(**validated.model_dump())
        return self.func(**args)


class ToolRegistry:
    """工具注册与调度。"""

    def __init__(self, *, tracer: Tracer | None = None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self.tracer = tracer

    # ---------------------------------------------------------------- 注册

    def register(self, spec: ToolSpec, *, overwrite: bool = False) -> None:
        if spec.name in self._tools and not overwrite:
            raise ContractViolation(f"工具名重复：{spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise ToolNotFoundError(
                "工具未注册", tool=name, available=sorted(self._tools)
            )
        return self._tools[name]

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    def subset(self, names: list[str]) -> "ToolRegistry":
        """按白名单裁剪 —— 每个阶段只暴露它需要的工具（最小权限）。"""
        sub = ToolRegistry(tracer=self.tracer)
        for n in names:
            sub.register(self.get(n))
        return sub

    def to_openai_schemas(self) -> list[dict[str, Any]]:
        return [t.to_openai_schema() for t in self._tools.values()]

    # ---------------------------------------------------------------- 调用

    def call(self, name: str, args: dict[str, Any], *, stage: str | None = None) -> Any:
        spec = self.get(name)
        t0 = time.perf_counter()
        try:
            result = spec.invoke(args)
        except Exception as exc:
            if self.tracer is not None:
                self.tracer.tool(
                    name=name,
                    args=args,
                    ok=False,
                    latency_ms=int((time.perf_counter() - t0) * 1000),
                    stage=stage,
                    error=f"{type(exc).__name__}: {exc}"[:400],
                )
            if isinstance(exc, (ContractViolation, ToolNotFoundError)):
                raise
            raise ToolExecutionError(
                f"工具 {name} 执行失败：{exc}", tool=name
            ) from exc

        if self.tracer is not None:
            self.tracer.tool(
                name=name,
                args=args,
                ok=True,
                latency_ms=int((time.perf_counter() - t0) * 1000),
                out_hash=payload_hash(_jsonable(result)),
                stage=stage,
                # 返回值也进逐步记录（agent_log.jsonl）—— trace 那路只有
                # payload_hash，读不出「这个工具回了什么」。工具注册表是
                # 唯一同时握着入参与返回值的地方。
                result=_jsonable(result),
            )
        return result

    def describe(self) -> list[dict[str, Any]]:
        """生成工具清单文档（写入 README 或 Web 页面）。"""
        return [
            {
                "name": t.name,
                "description": t.description,
                "side_effect": t.side_effect,
                "determinism": t.determinism,
                "cost": t.cost,
                "requires_evidence": t.requires_evidence,
                "tags": t.tags,
            }
            for t in self._tools.values()
        ]


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


# ==========================================================================
# 装饰器
# ==========================================================================


def tool(
    *,
    name: str,
    description: str,
    params: type[BaseModel] | None = None,
    returns: type[BaseModel] | None = None,
    side_effect: SideEffect = "none",
    determinism: Determinism = "deterministic",
    cost: Cost = "free",
    timeout_s: float = 60.0,
    requires_evidence: bool = False,
    tags: list[str] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """把一个函数登记为工具。

    用法::

        class SearchIn(BaseModel):
            pattern: str
            doc_id: str

        @tool(name="doc.search", description="按正则检索页面文本",
              params=SearchIn, side_effect="read", cost="cheap")
        def search(pattern: str, doc_id: str) -> list[dict]:
            ...

    被装饰的函数会获得 ``.spec`` 属性，供 ``ToolRegistry`` 收集。
    """

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        spec = ToolSpec(
            name=name,
            description=description,
            func=fn,
            params_model=params,
            returns_model=returns,
            side_effect=side_effect,
            determinism=determinism,
            cost=cost,
            timeout_s=timeout_s,
            requires_evidence=requires_evidence,
            tags=tags or [],
        )
        fn.spec = spec  # type: ignore[attr-defined]
        return fn

    return deco


def collect_tools(module: Any) -> list[ToolSpec]:
    """扫描模块，收集所有被 ``@tool`` 装饰的函数。"""
    specs: list[ToolSpec] = []
    for _, obj in inspect.getmembers(module):
        spec = getattr(obj, "spec", None)
        if isinstance(spec, ToolSpec):
            specs.append(spec)
    return specs


def build_registry(*modules: Any, tracer: Tracer | None = None) -> ToolRegistry:
    """从若干模块批量构建注册表。"""
    reg = ToolRegistry(tracer=tracer)
    for mod in modules:
        for spec in collect_tools(mod):
            reg.register(spec)
    return reg


__all__ = [
    "ToolSpec",
    "ToolRegistry",
    "tool",
    "collect_tools",
    "build_registry",
    "SideEffect",
    "Determinism",
    "Cost",
]
