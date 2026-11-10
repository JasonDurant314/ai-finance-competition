"""OpenAI 兼容协议提供方 —— 一份代码驱动 DeepSeek / Qwen / GLM / Kimi / vLLM。

使用 ``httpx`` 同步客户端。所有网络调用都受 :class:`FileGuard` 的
离线开关约束（由上层传入 ``offline`` 时禁止构造本对象）。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from agent.runtime.errors import (
    MissingCredentialError,
    ModelError,
    ModelRateLimitError,
    ModelTimeoutError,
)


class OpenAICompatProvider:
    """任何兼容 ``POST /chat/completions`` 的服务都能用。"""

    name = "openai_compat"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        extra_headers: dict[str, str] | None = None,
        default_params: dict[str, Any] | None = None,
        provider_name: str | None = None,
    ) -> None:
        if not base_url:
            raise MissingCredentialError("缺少 base_url")
        # 本地 vLLM/Ollama 通常无需 key
        if not api_key and "localhost" not in base_url and "127.0.0.1" not in base_url:
            raise MissingCredentialError(
                "缺少 API Key。请设置环境变量（见 .env.example），切勿写入代码或产物。",
                base_url=base_url,
            )
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.extra_headers = extra_headers or {}
        self.default_params = default_params or {}
        if provider_name:
            self.name = provider_name

    # ---------------------------------------------------------------- 调用

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        params: dict[str, Any],
        json_schema: dict[str, Any] | None,
        timeout: float,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            **self.default_params,
            **{k: v for k, v in params.items() if v is not None},
        }
        # `thinking` 是 DeepSeek 的扩展字段（OpenAI 兼容格式下的 extra_body）。
        # 不支持的网关会忽略它，因此无需做能力探测。
        if json_schema is not None:
            body["response_format"] = {"type": "json_object"}
            # 部分服务支持严格 schema 约束；用 additionalProperties=false 提高稳定性
            schema = {**json_schema}
            schema.setdefault("additionalProperties", False)

        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(
                    f"{self.base_url}/chat/completions", headers=headers, json=body
                )
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError("模型调用超时", model=model, timeout=timeout) from exc
        except httpx.HTTPError as exc:
            raise ModelError(f"模型调用网络错误：{exc}", model=model) from exc

        if resp.status_code == 429:
            raise ModelRateLimitError("模型限流 429", model=model)
        if resp.status_code >= 400:
            raise ModelError(
                f"模型返回 HTTP {resp.status_code}",
                model=model,
                body=resp.text[:500],
            )

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise ModelError("模型返回非 JSON", model=model, body=resp.text[:500]) from exc

        text = self._extract_text(data)
        usage = data.get("usage") or {}
        # ⚠️ 思考模式下 reasoning_tokens **计入** completion_tokens。
        #    必须把它单独记录下来，否则：
        #      · 成本被低估（模型为"想"付了钱，我们却只按输出计价）
        #      · 无法解释"为什么 max_tokens 明明够用，content 却是空的"
        details = usage.get("completion_tokens_details") or {}
        cached = (
            usage.get("prompt_cache_hit_tokens")
            or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
            or 0
        )
        return {
            "text": text,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "reasoning_tokens": details.get("reasoning_tokens", 0),
            "cached_prompt_tokens": cached,
            "finish_reason": (data.get("choices") or [{}])[0].get("finish_reason"),
            "raw": data,
        }

    # ---------------------------------------------------------------- 解析

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        """从响应中提取正式回答。

        ⚠️ 关键陷阱：思考模式下模型会先产出 ``reasoning_content``（思维链），
        再产出 ``content``（正式回答）。**两者都在 completion_tokens 里计费**。
        若 ``max_tokens`` 被思维链耗尽，``content`` 会是空字符串而
        ``finish_reason`` 仍是 ``stop`` —— 表现为"模型没回答但不报错"。

        因此这里的行为是：
          * ``content`` 非空        → 直接返回
          * ``content`` 为空但有思维链 → **抛出显式错误**，而不是静默返回空串
            （空串会被下游当作"模型说没有"，是最危险的失败模式）
        """
        choices = data.get("choices") or []
        if not choices:
            raise ModelError("模型返回 choices 为空", keys=list(data.keys()))
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        finish = choices[0].get("finish_reason")
        usage = data.get("usage") or {}
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)

        # 多模态/分段返回：拼接所有 text 段
        if isinstance(content, list):
            parts = [
                seg.get("text", "")
                for seg in content
                if isinstance(seg, dict) and seg.get("type") in (None, "text")
            ]
            content = "".join(parts)

        if isinstance(content, str) and content.strip():
            return content

        # content 为空：区分"确实没内容"与"token 被思考耗尽"
        if reasoning:
            raise ModelError(
                f"模型输出为空，但消耗了 {reasoning} 个推理 token —— "
                f"max_tokens 很可能被思考过程耗尽（finish_reason={finish}）。"
                "请提高 max_tokens，或为该角色设置 reasoning_effort='low' 关闭深度思考。",
                finish_reason=finish,
                reasoning_tokens=reasoning,
                completion_tokens=usage.get("completion_tokens", 0),
            )
        if finish == "length":
            raise ModelError(
                "模型输出被 max_tokens 截断（finish_reason=length）", finish_reason=finish
            )
        return content if isinstance(content, str) else ""


__all__ = ["OpenAICompatProvider"]
