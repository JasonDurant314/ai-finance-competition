"""模型价目表与成本估算。

价格来源：DeepSeek 官方 Models & Pricing（2026-09 调价后）。
**必须定期核对**，价格变动会影响预算控制的有效性。
单位：人民币元 / 百万 token。

注意：多模态模型的**图片折算 token 规则未公开**，
P0 阶段脚本 ``tools/p0_probe.py`` 会实测出每页折算值并回填此处。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    """人民币元 / 百万 token。"""

    input_cache_hit: float
    input_cache_miss: float
    output: float


# 高峰/空闲时段价格不同，此处取**高峰价**（保守估算）
PRICES: dict[str, Price] = {
    # DeepSeek-V4.1-Flash（原生多模态，官方推荐模型名 deepseek-flash）
    "deepseek-flash": Price(input_cache_hit=0.10, input_cache_miss=3.0, output=9.0),
    "deepseek-v4.1-flash": Price(input_cache_hit=0.10, input_cache_miss=3.0, output=9.0),
    # 兼容旧名（暂时路由到 V4.1 Flash）
    "deepseek-v4-flash": Price(input_cache_hit=0.10, input_cache_miss=3.0, output=9.0),
    "deepseek-v4-flash-vision-exp": Price(input_cache_hit=0.10, input_cache_miss=3.0, output=9.0),
    # DeepSeek-V4-Pro
    "deepseek-v4-pro": Price(input_cache_hit=0.20, input_cache_miss=6.0, output=18.0),
    # 本地部署 / mock：零成本
    "mock": Price(input_cache_hit=0.0, input_cache_miss=0.0, output=0.0),
    "local": Price(input_cache_hit=0.0, input_cache_miss=0.0, output=0.0),
}

# 图片 token 折算的兜底估值（**待 P0 实测回填**）。
# 暂按「一张 170dpi A4 页面 ≈ 1100 token」保守估算。
IMAGE_TOKENS_PER_PAGE_FALLBACK = 1100


def estimate_cost(
    model: str,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cached_prompt_tokens: int = 0,
) -> float:
    """估算单次调用成本（元）。未知模型按 V4.1 Flash 计价，宁可高估。"""
    price = PRICES.get(model, PRICES["deepseek-flash"])
    miss = max(0, prompt_tokens - cached_prompt_tokens)
    return (
        cached_prompt_tokens / 1_000_000 * price.input_cache_hit
        + miss / 1_000_000 * price.input_cache_miss
        + completion_tokens / 1_000_000 * price.output
    )


def estimate_image_tokens(n_pages: int, tokens_per_page: int | None = None) -> int:
    """估算 N 页图像的 token 折算量。P0 实测后请传入真实 tokens_per_page。"""
    return n_pages * (tokens_per_page or IMAGE_TOKENS_PER_PAGE_FALLBACK)


__all__ = [
    "Price",
    "PRICES",
    "estimate_cost",
    "estimate_image_tokens",
    "IMAGE_TOKENS_PER_PAGE_FALLBACK",
]
