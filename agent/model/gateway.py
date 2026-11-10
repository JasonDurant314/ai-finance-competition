"""模型网关 —— 多角色、可缓存、可记账、可复现。

设计要点
--------
1. **OpenAI 兼容协议为统一接口**：同一份代码可驱动 DeepSeek / Qwen / GLM /
   Kimi / 本地 vLLM，换模型只改配置文件。
2. **多角色**：extractor（快、低 effort）/ planner（中）/ critic（高 effort）/
   vision（多模态裁决）。角色 -> 模型的映射在 configs/models.yaml。
3. **响应缓存**：命中则完全不发请求，是 replay 与成本控制的基础。
4. **结构化输出**：JSON Schema 强约束；校验失败把错误回灌重试（自我修复）。
5. **成本与 token 全记账**，落入 trace.jsonl。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agent.model.cache import ResponseCache, cache_key, hash_schema
from agent.model.prices import estimate_cost
from agent.runtime.budget import Budget
from agent.runtime.errors import (
    ConfigError,
    ModelError,
    ModelRateLimitError,
    ModelTimeoutError,
    StructuredOutputError,
)
from agent.trace.tracer import Tracer, payload_hash


# ==========================================================================
# 消息构造
# ==========================================================================


def text_message(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "content": text}


def image_message(
    role: str,
    *,
    text: str,
    image_paths: list[str | Path],
    detail: str = "high",
) -> dict[str, Any]:
    """构造多模态消息。

    图片以 base64 data URL 内联 —— 避免对外暴露本地路径，也避免
    依赖外网可访问的 URL（契合『封闭数据环境』要求）。
    """
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for p in image_paths:
        path = Path(p)
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        parts.append(
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}", "detail": detail}}
        )
    return {"role": role, "content": parts}


# ==========================================================================
# Provider 协议
# ==========================================================================


class LLMProvider(Protocol):
    """模型提供方协议。新增提供方只需实现本协议。"""

    name: str

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        params: dict[str, Any],
        json_schema: dict[str, Any] | None,
        timeout: float,
    ) -> dict[str, Any]:
        """返回 {text, prompt_tokens, completion_tokens, raw}。"""
        ...


# ==========================================================================
# 调用结果
# ==========================================================================


@dataclass
class LLMResult:
    text: str
    role: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    """思考模式消耗的推理 token。**已包含在 completion_tokens 内**，
    单独记录用于诊断「max_tokens 被思考耗尽」这类问题。"""
    cached_prompt_tokens: int = 0
    """命中提示缓存的输入 token —— 按缓存价计费，成本核算需要它。"""
    cache_hit: bool = False
    cost_cny: float = 0.0
    latency_ms: int = 0
    prompt_id: str = ""
    prompt_version: str = ""
    prompt_sha256: str = ""
    parsed: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def answer_tokens(self) -> int:
        """正式回答占用的 token（不含思考）。"""
        return max(0, self.completion_tokens - self.reasoning_tokens)


# ==========================================================================
# 网关
# ==========================================================================


@dataclass
class RoleConfig:
    """一个角色的模型配置。

    ⚠️ **思考模式与可复现性冲突**（DeepSeek 官方文档明确说明）：

        「Thinking mode does not support the ``temperature``, ``presence_penalty``,
          or ``frequency_penalty`` parameters... setting these parameters will not
          trigger an error but will also have no effect.」

    也就是说，思考模式下 ``temperature=0`` **不生效**，而思维链本身具有随机性。
    结果：同一输入两次运行可能给出不同答案 —— 直接违背赛题的「结果可复现」要求。

    因此本项目的策略是**默认关闭思考**（``thinking=False``）：
        * 抽取、裁判这类任务输出高度确定（"读出页面上的字符"），不需要思维链
        * 关闭后 ``temperature=0`` 生效，输出可复现
        * 顺带解决「max_tokens 被思考耗尽导致空响应」的问题

    仅对确实需要推理的角色（critic 复核、planner 规划）开启思考，
    这些角色的**结论不直接进入产物**，或已通过确定性校验兜底。
    """

    model: str
    temperature: float = 0.0
    max_tokens: int = 16384
    reasoning_effort: str | None = None  # minimal | low | medium | high | xhigh | max
    thinking: bool = False
    """是否开启思考模式。**默认 False**，原因见类文档。"""
    top_p: float = 1.0
    supports_vision: bool = False

    def to_params(self) -> dict[str, Any]:
        p: dict[str, Any] = {
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": self.top_p,
            # OpenAI 兼容格式下，思考开关走 thinking 字段（官方文档）
            "thinking": {"type": "enabled" if self.thinking else "disabled"},
        }
        if self.thinking and self.reasoning_effort:
            p["reasoning_effort"] = self.reasoning_effort
        return p


class ModelGateway:
    """模型网关。所有 LLM 调用必须经过此处 —— 这是可复现性的唯一入口。"""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        roles: dict[str, RoleConfig],
        tracer: Tracer | None = None,
        cache: ResponseCache | None = None,
        budget: Budget | None = None,
        offline: bool = False,
        max_retries: int = 2,
        timeout: float = 120.0,
        strict_json: bool = True,
    ) -> None:
        if "default" not in roles:
            raise ConfigError("roles 配置必须包含 'default' 角色")
        self.provider = provider
        self.roles = roles
        self.tracer = tracer
        self.cache = cache
        self.budget = budget
        self.offline = offline
        self.max_retries = max_retries
        self.timeout = timeout
        self.strict_json = strict_json
        self._stats = {"calls": 0, "cache_hits": 0, "repairs": 0}

    # ---------------------------------------------------------------- 核心

    def call(
        self,
        *,
        role: str = "default",
        messages: list[dict[str, Any]],
        json_schema: dict[str, Any] | None = None,
        prompt_id: str = "inline",
        prompt_version: str = "0",
        prompt_sha256: str = "",
        overrides: dict[str, Any] | None = None,
        stage: str | None = None,
    ) -> LLMResult:
        cfg = self.roles.get(role) or self.roles["default"]
        params = cfg.to_params()
        if overrides:
            params.update(overrides)

        key = cache_key(
            provider=self.provider.name,
            model=cfg.model,
            messages=messages,
            params=params,
            json_schema=json_schema if self.strict_json else None,
            prompt_sha=prompt_sha256 or None,
        )

        t0 = time.perf_counter()
        # ⚠️ 必须用 `is not None` 而不是 `if self.cache:`。
        # ResponseCache 定义了 __len__，若其 __bool__ 语义变化，
        # 真值测试会变成「缓存非空吗」——空缓存时静默跳过读，
        # 整个缓存机制失效且不报错。显式判空更安全。
        cached = self.cache.get(key) if self.cache is not None else None

        if cached is not None:
            payload = cached  # ResponseCache.get 已返回响应体本身
            self._stats["cache_hits"] += 1
            result = self._to_result(
                payload, role=role, cfg=cfg, cache_hit=True,
                latency_ms=int((time.perf_counter() - t0) * 1000),
                prompt_id=prompt_id, prompt_version=prompt_version,
                prompt_sha256=prompt_sha256, json_schema=json_schema,
            )
            self._log(result, stage=stage, ok=True, error=None, messages=messages)
            return result

        if self.offline:
            raise ModelError(
                "离线模式下缓存未命中，无法调用模型。"
                "请先联网跑一次 run 以填充缓存，或改用 --no-offline。",
                role=role, cache_key=key[:16],
                cache_entries=len(self.cache) if self.cache is not None else 0,
            )

        # —— 真实调用（含重试与自我修复） ——
        payload, repair_note = self._invoke_with_retry(
            cfg=cfg, params=params, messages=messages, json_schema=json_schema
        )
        self._stats["repairs"] += repair_note

        if self.cache is not None:
            self.cache.put(key, payload)

        result = self._to_result(
            payload, role=role, cfg=cfg, cache_hit=False,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            prompt_id=prompt_id, prompt_version=prompt_version,
            prompt_sha256=prompt_sha256, json_schema=json_schema,
        )
        # 记账
        if self.budget:
            self.budget.charge(result.total_tokens, result.cost_cny)
        self._log(result, stage=stage, ok=True, error=None, messages=messages)
        return result

    # ---------------------------------------------------------------- 内部

    def _invoke_with_retry(
        self,
        *,
        cfg: RoleConfig,
        params: dict[str, Any],
        messages: list[dict[str, Any]],
        json_schema: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], int]:
        """带重试与自我修复的调用。

        返回 (payload, 修复轮数)。
        """
        msgs = list(messages)
        repairs = 0
        last_err: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                payload = self.provider.complete(
                    model=cfg.model,
                    messages=msgs,
                    params=params,
                    json_schema=json_schema if self.strict_json else None,
                    timeout=self.timeout,
                )
            except ModelRateLimitError as exc:
                last_err = exc
                time.sleep(min(2.0 * (2**attempt), 20.0))
                continue
            except ModelTimeoutError as exc:
                last_err = exc
                continue

            if json_schema is None or not self.strict_json:
                return payload, repairs

            # 校验结构化输出；失败则把错误回灌给模型重试（自我修复）
            try:
                _parse_json(payload.get("text", ""), json_schema)
                return payload, repairs
            except StructuredOutputError as exc:
                last_err = exc
                repairs += 1
                if attempt >= self.max_retries:
                    break
                msgs = [
                    *messages,
                    {"role": "assistant", "content": payload.get("text", "")[:2000]},
                    {
                        "role": "user",
                        "content": (
                            "上一次输出未通过 JSON Schema 校验，错误如下：\n"
                            f"{exc.validation_error}\n\n"
                            "请**只输出修正后的 JSON 对象**，不要任何解释文字、不要 markdown 代码围栏。"
                        ),
                    },
                ]

        raise ModelError(
            f"模型调用在 {self.max_retries + 1} 次尝试后仍失败：{last_err}",
            model=cfg.model,
        )

    def _to_result(
        self,
        payload: dict[str, Any],
        *,
        role: str,
        cfg: RoleConfig,
        cache_hit: bool,
        latency_ms: int,
        prompt_id: str,
        prompt_version: str,
        prompt_sha256: str,
        json_schema: dict[str, Any] | None,
    ) -> LLMResult:
        pt = int(payload.get("prompt_tokens", 0) or 0)
        ct = int(payload.get("completion_tokens", 0) or 0)
        rt = int(payload.get("reasoning_tokens", 0) or 0)
        cached = int(payload.get("cached_prompt_tokens", 0) or 0)
        text = payload.get("text", "") or ""
        parsed = None
        if json_schema is not None and self.strict_json:
            parsed = _parse_json(text, json_schema)
        return LLMResult(
            text=text,
            role=role,
            model=cfg.model,
            prompt_tokens=pt,
            completion_tokens=ct,
            reasoning_tokens=rt,
            cached_prompt_tokens=cached,
            cache_hit=cache_hit,
            # 提示缓存命中的部分按缓存价计费，否则成本会被高估
            cost_cny=estimate_cost(
                cfg.model,
                prompt_tokens=pt,
                completion_tokens=ct,
                cached_prompt_tokens=cached,
            ),
            latency_ms=latency_ms,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            prompt_sha256=prompt_sha256 or hash_schema(json_schema) or "",
            parsed=parsed,
            raw=payload.get("raw", {}),
        )

    def _log(self, result: LLMResult, *, stage: str | None, ok: bool, error: str | None,
             messages: list[dict[str, Any]] | None = None) -> None:
        self._stats["calls"] += 1
        if self.tracer is not None:
            self.tracer.llm(
                role=result.role,
                model=result.model,
                prompt_id=result.prompt_id,
                prompt_version=result.prompt_version,
                prompt_sha256=result.prompt_sha256,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                latency_ms=result.latency_ms,
                cache_hit=result.cache_hit,
                cost_cny=result.cost_cny,
                out_hash=payload_hash(result.parsed if result.parsed is not None else result.text),
                stage=stage,
                ok=ok,
                error=error,
                extra={
                    "reasoning_tokens": result.reasoning_tokens,
                    "answer_tokens": result.answer_tokens,
                    "cached_prompt_tokens": result.cached_prompt_tokens,
                },
                # 原文只进 agent_log.jsonl（trace 那路仍只有 out_hash）。
                # 这里是全仓**唯一**同时握着 messages 与响应正文的地方，
                # 所以右栏「模型在干什么」的料只能在这里接。
                messages=messages,
                text=result.text,
            )

    @property
    def stats(self) -> dict[str, Any]:
        calls = max(1, self._stats["calls"])
        return {
            **self._stats,
            "cache_hit_rate": round(self._stats["cache_hits"] / calls, 4),
        }


# ==========================================================================
# JSON 解析与 Schema 轻校验
# ==========================================================================


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def _parse_json(text: str, schema: dict[str, Any] | None) -> dict[str, Any]:
    """解析模型输出为 JSON 并按 schema 轻校验。失败抛 StructuredOutputError。"""
    candidate = _strip_fences(text)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(
            "模型输出不是合法 JSON", raw_response=text, validation_error=str(exc)
        ) from exc

    if not isinstance(data, dict):
        raise StructuredOutputError(
            "模型输出的顶层必须是 JSON 对象", raw_response=text,
            validation_error=f"got {type(data).__name__}",
        )

    if schema:
        errs = _validate_required(data, schema)
        if errs:
            raise StructuredOutputError(
                "模型输出不满足 JSON Schema", raw_response=text,
                validation_error="; ".join(errs),
            )
    return data


def parse_json_output(text: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
    """公开的 JSON 输出解析与校验入口。

    P0 实测脚本等外部工具需要复用同一套校验逻辑 —— 保证「测试用的校验」
    与「生产用的校验」完全一致，避免出现「测试通过但线上失败」。
    """
    return _parse_json(text, schema)


def _validate_required(data: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """轻量必需字段校验（不引入 jsonschema 依赖以保持可控）。

    只校验结构骨架：required / type / items / enum。
    数值域与业务规则由 validate/ 模块负责。
    """
    errs: list[str] = []
    stype = schema.get("type")

    if stype == "object" or "properties" in schema:
        if not isinstance(data, dict):
            return [f"{path}: 期望 object，实际 {type(data).__name__}"]
        for req in schema.get("required", []):
            if req not in data:
                errs.append(f"{path}.{req}: 缺少必需字段")
        for key, sub in (schema.get("properties") or {}).items():
            if key in data:
                errs.extend(_validate_required(data[key], sub, f"{path}.{key}"))
    elif stype == "array":
        if not isinstance(data, list):
            return [f"{path}: 期望 array，实际 {type(data).__name__}"]
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(data):
                errs.extend(_validate_required(item, item_schema, f"{path}[{i}]"))
    elif stype == "string":
        if not isinstance(data, str):
            errs.append(f"{path}: 期望 string，实际 {type(data).__name__}")
    elif stype in ("number", "integer"):
        if isinstance(data, bool) or not isinstance(data, (int, float)):
            errs.append(f"{path}: 期望 number，实际 {type(data).__name__}")
    elif stype == "boolean":
        if not isinstance(data, bool):
            errs.append(f"{path}: 期望 boolean，实际 {type(data).__name__}")
    elif stype == "null":
        if data is not None:
            errs.append(f"{path}: 期望 null")

    if "enum" in schema and data not in schema["enum"]:
        errs.append(f"{path}: 取值 {data!r} 不在允许集合 {schema['enum']} 内")
    return errs


__all__ = [
    "LLMProvider",
    "LLMResult",
    "RoleConfig",
    "ModelGateway",
    "text_message",
    "image_message",
    "parse_json_output",
]
