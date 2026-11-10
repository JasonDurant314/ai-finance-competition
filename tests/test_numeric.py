"""数值与单位归一化的单元测试。

这些是最基础也最关键的测试：金融数据的任何单位错误都会污染
R1 的事实库，进而在 R4 与 R5 里放大成错误结论。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from agent.validate.numeric import (
    approx_equal,
    fmt_amount,
    from_yuan,
    magnitude_gap,
    normalize_unit,
    parse_amount,
    parse_chinese_amount,
    parse_decimal,
    to_yuan,
)


# ==========================================================================
# parse_decimal
# ==========================================================================


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1234", Decimal("1234")),
        ("1,200,450.00", Decimal("1200450.00")),
        ("-145,200.00", Decimal("-145200.00")),
        ("（1,234.56）", Decimal("-1234.56")),          # 中文括号负数
        ("(2,000)", Decimal("-2000")),                  # 半角括号负数
        ("１２３４", Decimal("1234")),                    # 全角数字
        ("¥1,000", Decimal("1000")),                    # 带货币符号
        ("-", Decimal("0")),                            # 占位符
        ("—", Decimal("0")),
        ("不适用", Decimal("0")),
        ("", None),
        ("abc", None),
    ],
)
def test_parse_decimal(raw: str, expected: Decimal | None) -> None:
    assert parse_decimal(raw) == expected


def test_parse_decimal_uses_decimal_not_float() -> None:
    """必须用 Decimal：0.1+0.2 的浮点误差在金融场景不可接受。"""
    v = parse_decimal("0.1")
    assert v is not None
    assert v + parse_decimal("0.2") == Decimal("0.3")


# ==========================================================================
# parse_chinese_amount
# ==========================================================================


@pytest.mark.parametrize(
    "text, expected",
    [
        ("壹亿贰仟零肆万伍仟元", Decimal("120045000")),
        ("人民币壹仟万元整", Decimal("10000000")),
        ("贰拾叁万捌仟贰佰元", Decimal("238200")),
        ("伍拾万元", Decimal("500000")),
        ("壹拾万元", Decimal("100000")),
    ],
)
def test_parse_chinese_amount(text: str, expected: Decimal) -> None:
    assert parse_chinese_amount(text) == expected


def test_parse_chinese_amount_rejects_non_amount() -> None:
    assert parse_chinese_amount("营业收入") is None
    assert parse_chinese_amount("") is None


# ==========================================================================
# parse_amount
# ==========================================================================


@pytest.mark.parametrize(
    "raw, value, unit",
    [
        ("12,004.50万元", Decimal("12004.50"), "万元"),
        ("1.2亿元", Decimal("1.2"), "亿元"),
        ("312,450.00 万元", Decimal("312450.00"), "万元"),
        ("45.30%", Decimal("45.30"), "%"),
        ("2.5370元/股", Decimal("2.5370"), "元每股"),
        ("15,230.00万股", Decimal("15230.00"), "万股"),
        ("1000", Decimal("1000"), ""),          # 无单位
    ],
)
def test_parse_amount(raw: str, value: Decimal, unit: str) -> None:
    result = parse_amount(raw)
    assert result is not None
    got_value, got_unit = result
    assert got_value == value
    assert got_unit == unit


# ==========================================================================
# 单位换算
# ==========================================================================


@pytest.mark.parametrize(
    "value, unit, expected_yuan",
    [
        (Decimal("1"), "元", Decimal("1")),
        (Decimal("1"), "千元", Decimal("1000")),
        (Decimal("1"), "万元", Decimal("10000")),
        (Decimal("1"), "百万元", Decimal("1000000")),
        (Decimal("1"), "亿元", Decimal("100000000")),
        (Decimal("12004.50"), "万元", Decimal("120045000")),
    ],
)
def test_to_yuan(value: Decimal, unit: str, expected_yuan: Decimal) -> None:
    assert to_yuan(value, unit) == expected_yuan


def test_to_yuan_rejects_missing_unit() -> None:
    """单位缺失时必须报错，**不允许猜测**。这是最重要的一条。"""
    from agent.runtime.errors import ContractViolation

    with pytest.raises(ContractViolation):
        to_yuan(Decimal("100"), "")


def test_to_yuan_rejects_unknown_unit() -> None:
    from agent.runtime.errors import ContractViolation

    with pytest.raises(ContractViolation):
        to_yuan(Decimal("100"), "公斤")


def test_from_yuan_roundtrip() -> None:
    """万元的往返换算必须回到原值。"""
    v = Decimal("120045000")
    in_wan = from_yuan(v, "万元")
    assert in_wan == Decimal("12004.5")
    assert to_yuan(in_wan, "万元") == v


def test_normalize_unit_aliases() -> None:
    assert normalize_unit("人民币万元") == "万元"
    assert normalize_unit("元/股") == "元每股"
    assert normalize_unit("％") == "%"
    assert normalize_unit(None) == ""


# ==========================================================================
# 比较
# ==========================================================================


def test_approx_equal_tolerance() -> None:
    assert approx_equal(Decimal("100"), Decimal("100.4"), rel_tol=Decimal("0.005"))
    assert not approx_equal(Decimal("100"), Decimal("102"), rel_tol=Decimal("0.005"))


def test_approx_equal_absolute() -> None:
    assert approx_equal(
        Decimal("0.001"), Decimal("0.002"), rel_tol=Decimal("0"), abs_tol=Decimal("0.01")
    )


@pytest.mark.parametrize(
    "a, b, expected",
    [
        (Decimal("120000000"), Decimal("1.2"), 8),     # 亿元 vs 元
        (Decimal("120045"), Decimal("12.0045"), 4),    # 万元 vs 元
        (Decimal("100"), Decimal("100"), 0),
    ],
)
def test_magnitude_gap(a: Decimal, b: Decimal, expected: int) -> None:
    """量级差是判定 E1 单位错误的依据。"""
    assert magnitude_gap(a, b) == expected


def test_magnitude_gap_zero_returns_none() -> None:
    assert magnitude_gap(Decimal("0"), Decimal("100")) is None


def test_fmt_amount() -> None:
    assert fmt_amount(Decimal("120045000"), "万元") == "12,004.50 万元"
    assert fmt_amount(Decimal("120045000"), "亿元") == "1.20 亿元"
