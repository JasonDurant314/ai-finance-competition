"""校验层：JSON Schema / 数值单位 / quote 回读 / 断言。

四层校验，全部为**确定性代码**：
    1. schema  —— 模型输出结构是否合法
    2. numeric —— 单位换算与容差比较
    3. quote   —— 原文回读（幻觉硬闸门）
    4. assertion —— 财务勾稽恒等式
"""

from agent.validate.assertions import Assertion, AssertionReport, run_assertions
from agent.validate.numeric import (
    approx_equal,
    from_yuan,
    magnitude_gap,
    parse_amount,
    parse_chinese_amount,
    parse_decimal,
    to_yuan,
)
from agent.validate.quotecheck import QuoteMatch, check_quote, find_quote_span
from agent.validate.schema import (
    load_schema,
    validate_json,
    validate_model_instance,
    validate_products,
)

__all__ = [
    "Assertion",
    "AssertionReport",
    "run_assertions",
    "approx_equal",
    "from_yuan",
    "to_yuan",
    "magnitude_gap",
    "parse_amount",
    "parse_chinese_amount",
    "parse_decimal",
    "QuoteMatch",
    "check_quote",
    "find_quote_span",
    "load_schema",
    "validate_json",
    "validate_model_instance",
    "validate_products",
]
