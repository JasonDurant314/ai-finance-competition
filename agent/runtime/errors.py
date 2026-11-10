"""Harness 统一异常体系。

原则：**绝不静默失败**。任何降级、跳过、缺数据都必须显式抛出并落日志，
最终体现在产物的 needs_review / status=partial 上。
"""

from __future__ import annotations

from typing import Any


class HarnessError(Exception):
    """所有 harness 异常的基类。"""

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def __str__(self) -> str:  # pragma: no cover - 展示用
        if not self.context:
            return self.message
        kv = ", ".join(f"{k}={v!r}" for k, v in self.context.items())
        return f"{self.message} ({kv})"


# ---------------------------------------------------------------- 配置/环境


class ConfigError(HarnessError):
    """配置缺失或非法。"""


class MissingCredentialError(ConfigError):
    """缺少 API Key 等凭据。"""


# ---------------------------------------------------------------- 模型层


class ModelError(HarnessError):
    """模型调用失败的基类。"""


class ModelTimeoutError(ModelError):
    """模型调用超时。"""


class ModelRateLimitError(ModelError):
    """模型限流。可重试。"""


class StructuredOutputError(ModelError):
    """模型未按 JSON Schema 输出。

    携带 ``raw_response`` 以便把校验错误回灌给模型重试（自我修复）。
    """

    def __init__(self, message: str, raw_response: str = "", validation_error: str = "") -> None:
        super().__init__(message, raw_response_len=len(raw_response))
        self.raw_response = raw_response
        self.validation_error = validation_error


class BudgetExceededError(HarnessError):
    """预算（步数/token/时长/成本）超限。"""


# ---------------------------------------------------------------- 工具/沙箱


class ToolNotFoundError(HarnessError):
    """工具未注册。"""


class ToolExecutionError(HarnessError):
    """工具执行失败。"""


class SandboxViolation(HarnessError):
    """文件访问或网络访问越权 —— 对应题目『受控数据环境』要求。"""


# ---------------------------------------------------------------- 证据/校验


class EvidenceError(HarnessError):
    """证据锚点缺失或无法回读。"""


class QuoteVerificationError(EvidenceError):
    """quote 回读校验失败 —— 幻觉硬闸门。

    当 LLM 声称的原文片段在页面文本中找不到时抛出。
    """


class ContractViolation(HarnessError):
    """产物违反已冻结的数据契约。"""


class AssertionFailed(HarnessError):
    """财务勾稽断言失败 —— 阻断 R4 出报告。"""


# ---------------------------------------------------------------- 控制流


class StageSkipped(HarnessError):
    """阶段被主动跳过（预算不足或前置条件未满足）。属正常降级路径。"""


class NeedsReview(HarnessError):
    """结果不可信，需人工复核。不是崩溃，而是显式降级。"""


__all__ = [
    "HarnessError",
    "ConfigError",
    "MissingCredentialError",
    "ModelError",
    "ModelTimeoutError",
    "ModelRateLimitError",
    "StructuredOutputError",
    "BudgetExceededError",
    "ToolNotFoundError",
    "ToolExecutionError",
    "SandboxViolation",
    "EvidenceError",
    "QuoteVerificationError",
    "ContractViolation",
    "AssertionFailed",
    "StageSkipped",
    "NeedsReview",
]
