"""Mock 提供方 —— 让整条流水线在**零 API Key、零网络**的情况下跑通。

用途：
1. P2 Harness 内核的端到端测试（A 的交付判据）
2. CI 中的稳定性回归（同输入跑 3 次比对产物哈希）
3. 决赛现场的降级兜底

行为：按注册的「规则」匹配消息内容并返回预设响应；
未命中则返回一个结构合法的占位响应（保证 Schema 校验通过）。
"""

from __future__ import annotations

import json
from typing import Any, Callable


class MockProvider:
    """可编程的假模型。"""

    name = "mock"

    def __init__(
        self,
        *,
        rules: list[tuple[str, dict[str, Any]]] | None = None,
        fallback: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
    ) -> None:
        # rules: [(消息中出现的子串, 要返回的 dict)]
        self.rules = rules or []
        self.fallback = fallback
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        params: dict[str, Any],
        json_schema: dict[str, Any] | None,
        timeout: float,
    ) -> dict[str, Any]:
        self.calls.append({"model": model, "messages": messages, "params": params})

        blob = json.dumps(messages, ensure_ascii=False, default=str)
        for needle, payload in self.rules:
            if needle in blob:
                return self._wrap(payload)

        if self.fallback is not None:
            return self._wrap(self.fallback(messages))

        # 默认：按 schema 生成一个最小合法对象
        return self._wrap(_synthesize(json_schema) if json_schema else {"text": "mock response"})

    @staticmethod
    def _wrap(payload: dict[str, Any]) -> dict[str, Any]:
        text = payload.get("text")
        if text is None:
            text = json.dumps(payload, ensure_ascii=False)
        return {
            "text": text,
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "reasoning_tokens": 0,
            "cached_prompt_tokens": 0,
            "finish_reason": "stop",
            "raw": {"mock": True},
        }


def _synthesize(schema: dict[str, Any]) -> dict[str, Any]:
    """按 JSON Schema 合成最小合法对象。"""
    stype = schema.get("type")
    if stype == "object" or "properties" in schema:
        out: dict[str, Any] = {}
        props = schema.get("properties") or {}
        for key in schema.get("required", []):
            sub = props.get(key, {})
            out[key] = _synthesize(sub)
        return out
    if stype == "array":
        return []
    if stype == "string":
        return schema.get("enum", ["mock"])[0] if "enum" in schema else "mock"
    if stype in ("number", "integer"):
        return 0
    if stype == "boolean":
        return False
    if stype == "null":
        return None
    return None


__all__ = ["MockProvider"]
