"""资产负债表恒等式「联合选择」的测试（第十四轮）。

实测动机：601857 2025A 的三个指标各有 27 / 21 / 26 条候选，而 R4 逐指标
独立排序，把 ``equity_total`` 选成了 **7,300,000 元**（一条垃圾候选）。
三者拼起来恒等式残差 1.8 万亿 —— 而**没有任何闸门报警**，因为
每个指标单看都"合法"。

恒等式 ``总资产 = 总负债 + 所有者权益`` 是**跨指标**约束，
单独看任何一个指标都发现不了问题，因此必须**联合选择**。
"""

from __future__ import annotations

from decimal import Decimal

from agent.evidence.models import (
    Fact,
    FactSet,
    PeriodType,
    ProfitBasis,
    SourceLayer,
    StatementScope,
    utcnow,
)
from agent.evidence.store import build_confidence
from pipelines.r4_valuation.assumptions import _select_consistent_triple


def _fs(rows: list[tuple[str, str, str]]) -> FactSet:
    """rows = [(metric, value, scope)]"""
    facts = [
        Fact(
            fact_id=f"f{i}", doc_id="d1", entity="X", metric=m,
            value=Decimal(v), unit="元", raw_text=v, period="2025A",
            period_type=PeriodType.POINT_IN_TIME,
            statement_scope=StatementScope(sc) if sc else StatementScope.CONSOLIDATED,
            profit_basis=ProfitBasis.UNKNOWN, source_layer=SourceLayer.L1_TEXT_LAYER,
            evidence_span_ids=[f"e{i}"], channel_readings=[],
            confidence=0.95,
            confidence_breakdown=build_confidence(source_layer=SourceLayer.L1_TEXT_LAYER),
            needs_review=False, extractor="t@1", extracted_at=utcnow(),
        )
        for i, (m, v, sc) in enumerate(rows)
    ]
    return FactSet(run_id="t", built_at=utcnow(), documents=[], facts=facts, evidence=[])


def test_picks_the_self_consistent_triple() -> None:
    """在混乱的候选里，选出满足恒等式的那一组。"""
    fs = _fs([
        # 垃圾候选：权益只有 730 万，与资产量级完全不符
        ("total_assets", "2827777000000", "consolidated"),
        ("total_liabilities", "1028453000000", "consolidated"),
        ("equity_total", "7300000", "consolidated"),
        # 真值：三项自洽
        ("equity_total", "1799548000000", "consolidated"),
        ("total_liabilities", "1028469000000", "consolidated"),
    ])
    got = _select_consistent_triple(fs, period="2025A")
    assert got is not None, "应当找到自洽的三元组"
    a, li, e, note = got
    assert a == Decimal("2827777000000")
    # 自洽 = 在容差内相等（允许 0.1%），不是精确相等：
    # 报表以百万元为单位披露，三项各自四舍五入后不会逐分对上。
    assert abs(a - (li + e)) <= a * Decimal("0.001"), (
        f"选出的组合必须自洽：{li} + {e} = {li + e}，资产 {a}"
    )
    assert "恒等式" in note or "自洽" in note


def test_prefers_largest_assets_among_consistent_triples() -> None:
    """自洽的三元组可能有多组（不同口径/不同期），取**资产最大**的那组。

    依据：合并（集团）口径的资产必然 ≥ 其母公司/子公司口径 ——
    母公司是集团的一部分。**先过滤再排序**是关键：
    未过滤时"取最大"会被垃圾大数俘获（本轮实测过）。
    """
    fs = _fs([
        # 小口径（如母公司）—— 自洽
        ("total_assets", "1000", "consolidated"),
        ("total_liabilities", "400", "consolidated"),
        ("equity_total", "600", "consolidated"),
        # 大口径（合并）—— 也自洽
        ("total_assets", "9000", "consolidated"),
        ("total_liabilities", "3500", "consolidated"),
        ("equity_total", "5500", "consolidated"),
        # 一个更大的**不自洽**的值 —— 必须被过滤掉，不能靠它取胜
        ("total_assets", "99999", "consolidated"),
    ])
    got = _select_consistent_triple(fs, period="2025A")
    assert got is not None
    assert got[0] == Decimal("9000"), f"应取自洽集合里最大的资产，实际 {got[0]}"


def test_returns_none_when_no_consistent_triple() -> None:
    """找不到自洽组合时返回 None —— 调用方保持原选择并记缺口，不猜。"""
    fs = _fs([
        ("total_assets", "1000", "consolidated"),
        ("total_liabilities", "400", "consolidated"),
        ("equity_total", "999999", "consolidated"),
    ])
    assert _select_consistent_triple(fs, period="2025A") is None


def test_returns_none_when_a_metric_is_missing() -> None:
    fs = _fs([
        ("total_assets", "1000", "consolidated"),
        ("total_liabilities", "400", "consolidated"),
    ])
    assert _select_consistent_triple(fs, period="2025A") is None


def test_ignores_other_periods() -> None:
    """只在本期间内组合 —— 跨期拼出来的恒等式没有意义。"""
    fs = _fs([
        ("total_assets", "1000", "consolidated"),
        ("total_liabilities", "400", "consolidated"),
    ])
    # 权益属于 2024A，不该被用
    fs.facts.append(
        _fs([("equity_total", "600", "consolidated")]).facts[0].model_copy(
            update={"period": "2024A"}
        )
    )
    assert _select_consistent_triple(fs, period="2025A") is None
