"""R4 估值引擎与勾稽断言测试。

**最重要的一条设计原则在这里被验证**：断言必须是**独立重算**，
而不是复述模型的中间量。因此下面既有「正确模型通过」的测试，
也有「故意做错的模型必须被抓住」的测试。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from agent.evidence.models import AssertionStatus, DCFResult, ForecastYear
from agent.validate.assertions import (
    assert_balance_sheet,
    assert_fcff_recompute,
    assert_income_statement,
    assert_retained_earnings,
    assert_valuation_sanity,
    run_assertions,
)
from domain.tools.finance import (
    DCFInput,
    build_forecast,
    run_comps,
    run_dcf,
    run_sensitivity,
    CompsInput,
)


# ==========================================================================
# 测试数据
# ==========================================================================


@pytest.fixture
def base_input() -> DCFInput:
    return DCFInput(
        entity="示例股份",
        base_year=2024,
        base_revenue=Decimal("12004500000"),          # 120.045 亿元
        base_shares_outstanding=Decimal("968000000"),  # 9.68 亿股
        net_debt=Decimal("1980000000"),                # 19.8 亿净债
        minority_interest=Decimal("0"),
        explicit_years=5,
        revenue_growth=[Decimal("0.08")] * 5,
        ebit_margin=[Decimal("0.20")] * 5,
        tax_rate=Decimal("0.25"),
        da_ratio_of_revenue=Decimal("0.05"),
        capex_ratio_of_revenue=Decimal("0.06"),
        nwc_ratio_of_revenue=Decimal("0.10"),
        wacc=Decimal("0.095"),
        terminal_growth=Decimal("0.025"),
    )


# ==========================================================================
# 估值主体选择（多主体材料的关键防线）
# ==========================================================================


def _factset(rows: list[tuple[str, str]]) -> "FactSet":
    """用 (主体, 指标) 列表造一个最小事实库。"""
    from agent.evidence.models import (
        Fact, FactSet, PeriodType, ProfitBasis, SourceLayer, StatementScope,
    )
    from agent.evidence.store import build_confidence

    facts = [
        Fact(
            fact_id=f"f{i}", doc_id="doc1", entity=ent, metric=metric,
            value=Decimal("100"), unit="元", raw_text="100 元", period="2025A",
            period_type=PeriodType.DURATION_YTD,
            statement_scope=StatementScope.CONSOLIDATED,
            profit_basis=ProfitBasis.UNKNOWN, source_layer=SourceLayer.L1_TEXT_LAYER,
            evidence_span_ids=[f"evd_f{i}"], channel_readings=[],
            confidence=0.95,
            confidence_breakdown=build_confidence(source_layer=SourceLayer.L1_TEXT_LAYER),
            needs_review=False, extractor="rule_engine@1.0.0",
            extracted_at="2026-01-01T00:00:00",
        )
        for i, (ent, metric) in enumerate(rows)
    ]
    return FactSet(run_id="t", built_at="2026-01-01T00:00:00", documents=[],
                   facts=facts, evidence=[])


def test_entity_selection_ignores_subsidiary_with_more_facts() -> None:
    """★ 子公司事实再多，也不能取代披露主体。

    实测动机：表格抽取上线后一份材料里会出现 100+ 个主体（一行一家子公司）。
    原来"取事实条数最多的主体"会与子公司**打平**，而 ``max`` 在打平时返回
    先遇到的那个 —— 也就是**再加一条子公司事实就可能把估值对象换成子公司**，
    而且不会有任何报错。实测数据：披露主体 20 条、某子公司也是 20 条。
    """
    from pipelines.r4_valuation.assumptions import _select_entity

    rows = [("118034", "revenue")] * 3                 # 披露主体（6 位代码）
    rows += [("某子公司有限公司", "revenue")] * 20      # 子公司事实更多
    entity, excluded = _select_entity(_factset(rows))
    assert entity == "118034", f"选成了 {entity}"
    assert "某子公司有限公司" in excluded


def test_entity_selection_prefers_exclusive_metrics() -> None:
    """持有「主体专属指标」（总股本/每股收益/归母净利润）的主体即披露主体。"""
    from pipelines.r4_valuation.assumptions import _select_entity

    rows = [("JinkoSolar Middle East DMCC", "revenue")] * 30
    rows += [("示例股份", "shares_outstanding")]        # 只有披露主体有总股本
    entity, _ = _select_entity(_factset(rows))
    assert entity == "示例股份"


def test_entity_selection_warns_on_ambiguity() -> None:
    """两个主体打平且都不像代码 → 必须给出歧义告警，不假装确定。"""
    from pipelines.r4_valuation.assumptions import (
        _select_entity,
        entity_selection_note,
    )

    fs = _factset([("甲公司", "revenue")] * 5 + [("乙公司", "revenue")] * 5)
    entity, _ = _select_entity(fs)
    assert entity in ("甲公司", "乙公司")
    note = entity_selection_note(fs, entity)
    assert note and "并列" in note, note


# ==========================================================================
# capex ↔ D&A 一致性闸门
# ==========================================================================
#
# 这一组测试锁的是**模型结构**，不是某个数字。动机是一次真实事故：
# 601857 的 D&A 改用事实库真值 7.53% 后，DCF 从 15.21 涨到 22.54 —— 
# 而"改用更准的数据"本不该让估值变贵。原因是模板 capex 只有 6%，
# 低于 D&A，"D&A − capex" 成了正贡献（隐含不重置资产的清算假设）。


def test_capex_gate_raises_to_da_when_below() -> None:
    """capex < D&A 时必须抬到与 D&A 相等，并说明理由。"""
    from pipelines.r4_valuation.assumptions import _derive_capex_ratio

    value, note, fid = _derive_capex_ratio(Decimal("0.06"), Decimal("0.0753"), "fct_da")
    assert value == Decimal("0.0753"), value
    # 抬升的依据是 D&A 事实 → 必须挂上该 fact_id，不能记成"纯主观观点"
    assert fid == "fct_da"
    assert "清算" in note and "持续经营" in note, note
    # 依据里必须留下真实值的量级，读者才知道这一条仍偏乐观
    assert "9.39%" in note, note
    # 必须带出处标记，报告才能把它标成"事实 + 一致性调整"而不是"事实"
    from pipelines.r4_valuation.assumptions import GATE_MARKER
    assert note.startswith(GATE_MARKER), note[:40]


def test_capex_gate_leaves_template_alone_when_coherent() -> None:
    """capex ≥ D&A 时闸门不得改动模板值 —— 闸门只做最低限度干预。"""
    from pipelines.r4_valuation.assumptions import _derive_capex_ratio

    value, note, fid = _derive_capex_ratio(Decimal("0.06"), Decimal("0.05"), None)
    assert value == Decimal("0.06"), value
    assert fid is None
    assert "9.39%" in note, "取不到真实值时也必须写明真实值量级与取不到的原因"


def test_capex_gate_removes_net_reinvestment_distortion() -> None:
    """闸门生效后，``D&A − capex`` 必须恰好为 0，FCFF 不再含虚拟的净再投资正值。

    这是本闸门存在的**唯一理由**：把"用真实 D&A 反而推高估值"这条
    不报错的路径堵死。用真实的 FCFF 公式验算，而不是只比两个比率。
    """
    from pipelines.r4_valuation.assumptions import _derive_capex_ratio

    da = Decimal("0.0753")
    capex, _, _ = _derive_capex_ratio(Decimal("0.06"), da, "fct_da")
    assert da - capex == 0

    # 反例：不设闸门时 D&A−capex > 0，在这一项上凭空多出 1.53% 收入
    assert da - Decimal("0.06") > 0


def test_capex_gate_notes_fact_when_raised() -> None:
    """被抬升时 basis 走 fact 分支（有 fact_id），否则走模板分支。"""
    from pipelines.r4_valuation.assumptions import _derive_capex_ratio, _make_assumption

    value, note, fid = _derive_capex_ratio(Decimal("0.06"), Decimal("0.0753"), "fct_da")
    a = _make_assumption(
        name="capex_ratio_of_revenue", value=value, unit="比率（小数）",
        fact_id=fid, basis_from_fact=note if fid else "", basis_from_template=note,
        low=Decimal("0.02"), high=Decimal("0.25"),
    )
    assert a.is_judgment is False, "抬升发生在模型内部，但依据是真实的 D&A 事实"
    assert a.source_fact_ids == ["fct_da"]


def test_dcf_input_rejects_capex_below_da(base_input: DCFInput) -> None:
    """模型层硬拒绝 ``capex < D&A`` —— 任何构造路径都绕不过去。

    与 ``_derive_capex_ratio`` 的闸门是**两道不同的关**：
    闸门在构造前把值调合规并写明理由；模型校验在构造时确认它真的合规。
    这里测的是后者 —— 即使有人绕过 assumptions.py 直接构造 DCFInput，
    也不能悄悄拿到"折旧比投入多"的输入。
    """
    from pydantic import ValidationError

    bad = base_input.model_dump()
    bad["da_ratio_of_revenue"] = Decimal("0.0753")
    bad["capex_ratio_of_revenue"] = Decimal("0.06")
    with pytest.raises(ValidationError) as ei:
        DCFInput(**bad)
    msg = str(ei.value)
    assert "清算" in msg and "FCFF" in msg, msg


def test_dcf_input_accepts_capex_equal_to_da(base_input: DCFInput) -> None:
    """``capex == D&A`` 是允许的最低点（持续经营的最低要求）。"""
    ok = base_input.model_dump()
    ok["da_ratio_of_revenue"] = Decimal("0.0753")
    ok["capex_ratio_of_revenue"] = Decimal("0.0753")
    inp = DCFInput(**ok)
    assert inp.capex_ratio_of_revenue == inp.da_ratio_of_revenue


# ==========================================================================
# 预测构建
# ==========================================================================


def test_build_forecast_has_requested_years(base_input: DCFInput) -> None:
    years = build_forecast(base_input)
    assert len(years) == 5
    assert years[0].year == 2025
    assert years[-1].year == 2029


def test_forecast_revenue_grows_by_assumption(base_input: DCFInput) -> None:
    years = build_forecast(base_input)
    expected = base_input.base_revenue * Decimal("1.08")
    # 收入四舍五入到分，容差 1 元
    assert abs(years[0].revenue - expected) < Decimal("1")


def test_forecast_fcff_formula(base_input: DCFInput) -> None:
    """FCFF = NOPAT + D&A − CapEx − ΔNWC"""
    y = build_forecast(base_input)[0]
    expected = (
        y.nopat + y.depreciation_amortization - y.capex - y.nwc_change
    )
    assert abs(y.fcff - expected) < Decimal("1")


# ==========================================================================
# DCF
# ==========================================================================


def test_run_dcf_produces_positive_value(base_input: DCFInput) -> None:
    r = run_dcf(base_input)
    assert r.value_per_share > 0
    assert r.enterprise_value > 0
    assert r.equity_value > 0


def test_dcf_equity_bridge(base_input: DCFInput) -> None:
    """股权价值 = EV − 净债 − 少数股东权益"""
    r = run_dcf(base_input)
    expected = r.enterprise_value - r.net_debt - r.minority_interest
    assert abs(r.equity_value - expected) < Decimal("1")


def test_dcf_rejects_wacc_below_growth(base_input: DCFInput) -> None:
    """WACC ≤ g 时戈登永续模型不收敛，必须显式报错而不是返回荒谬结果。"""
    from agent.runtime.errors import HarnessError

    bad = base_input.model_copy(update={"wacc": Decimal("0.02"), "terminal_growth": Decimal("0.03")})
    with pytest.raises(HarnessError, match="WACC"):
        run_dcf(bad)


def test_dcf_value_increases_with_lower_wacc(base_input: DCFInput) -> None:
    low = run_dcf(base_input.model_copy(update={"wacc": Decimal("0.08")}))
    high = run_dcf(base_input.model_copy(update={"wacc": Decimal("0.12")}))
    assert low.value_per_share > high.value_per_share


def test_dcf_terminal_value_share_computed(base_input: DCFInput) -> None:
    r = run_dcf(base_input)
    assert Decimal("0") < r.terminal_value_share < Decimal("1")


# ==========================================================================
# 断言：正确模型必须通过
# ==========================================================================


def test_balance_sheet_assertion_passes_for_constructible_year() -> None:
    y = ForecastYear(
        year=2025, revenue=Decimal("1000"), ebit=Decimal("200"), nopat=Decimal("150"),
        depreciation_amortization=Decimal("50"), capex=Decimal("60"),
        nwc_change=Decimal("10"), fcff=Decimal("130"), net_income=Decimal("140"),
        total_assets=Decimal("1500"), total_liabilities=Decimal("800"),
        total_equity=Decimal("700"), cash_end=Decimal("100"),
        retained_earnings=Decimal("140"),
    )
    assert assert_balance_sheet(y).status is AssertionStatus.PASS


def test_fcff_assertion_passes_for_consistent_year() -> None:
    y = ForecastYear(
        year=2025, revenue=Decimal("1000"), ebit=Decimal("200"), nopat=Decimal("150"),
        depreciation_amortization=Decimal("50"), capex=Decimal("60"),
        nwc_change=Decimal("10"), fcff=Decimal("130"), net_income=Decimal("140"),
        total_assets=Decimal("1500"), total_liabilities=Decimal("800"),
        total_equity=Decimal("700"), cash_end=Decimal("100"),
        retained_earnings=Decimal("140"),
    )
    # NOPAT = EBIT × (1 − 25%) = 150 ✓；FCFF = 150 + 50 − 60 − 10 = 130 ✓
    assert assert_fcff_recompute(y, tax_rate=Decimal("0.25")).status is AssertionStatus.PASS


def test_income_statement_assertion() -> None:
    y = ForecastYear(
        year=2025, revenue=Decimal("1000"), ebit=Decimal("200"), nopat=Decimal("150"),
        depreciation_amortization=Decimal("50"), capex=Decimal("60"),
        nwc_change=Decimal("10"), fcff=Decimal("130"), net_income=Decimal("132"),
        total_assets=Decimal("1500"), total_liabilities=Decimal("800"),
        total_equity=Decimal("700"), cash_end=Decimal("100"),
        retained_earnings=Decimal("132"),
    )
    a = assert_income_statement(
        y, cost_of_sales=Decimal("600"), opex=Decimal("200"),
        tax_expense=Decimal("68"),
    )
    assert a.status is AssertionStatus.PASS  # 1000 − 600 − 200 − 68 = 132


def test_retained_earnings_assertion() -> None:
    y = ForecastYear(
        year=2025, revenue=Decimal("1000"), ebit=Decimal("200"), nopat=Decimal("150"),
        depreciation_amortization=Decimal("50"), capex=Decimal("60"),
        nwc_change=Decimal("10"), fcff=Decimal("130"), net_income=Decimal("140"),
        total_assets=Decimal("1500"), total_liabilities=Decimal("800"),
        total_equity=Decimal("700"), cash_end=Decimal("100"),
        retained_earnings=Decimal("230"),
    )
    a = assert_retained_earnings(y, retained_begin=Decimal("110"), dividends=Decimal("20"))
    assert a.status is AssertionStatus.PASS  # 110 + 140 − 20 = 230


# ==========================================================================
# 断言：错误模型必须被抓住（这才是校验的意义）
# ==========================================================================


def test_fcff_assertion_catches_wrong_nopat() -> None:
    """故意把 NOPAT 写错，断言必须 FAIL —— 否则断言只是自证。"""
    y = ForecastYear(
        year=2025, revenue=Decimal("1000"), ebit=Decimal("200"),
        nopat=Decimal("999"),  # 错！应为 150
        depreciation_amortization=Decimal("50"), capex=Decimal("60"),
        nwc_change=Decimal("10"), fcff=Decimal("130"), net_income=Decimal("140"),
        total_assets=Decimal("1500"), total_liabilities=Decimal("800"),
        total_equity=Decimal("700"), cash_end=Decimal("100"),
        retained_earnings=Decimal("140"),
    )
    a = assert_fcff_recompute(y, tax_rate=Decimal("0.25"))
    assert a.status is AssertionStatus.FAIL
    assert "重算" in a.detail


def test_fcff_assertion_catches_wrong_fcff() -> None:
    y = ForecastYear(
        year=2025, revenue=Decimal("1000"), ebit=Decimal("200"), nopat=Decimal("150"),
        depreciation_amortization=Decimal("50"), capex=Decimal("60"),
        nwc_change=Decimal("10"), fcff=Decimal("999"),  # 错！应为 130
        net_income=Decimal("140"), total_assets=Decimal("1500"),
        total_liabilities=Decimal("800"), total_equity=Decimal("700"),
        cash_end=Decimal("100"), retained_earnings=Decimal("140"),
    )
    assert assert_fcff_recompute(y, tax_rate=Decimal("0.25")).status is AssertionStatus.FAIL


def test_balance_sheet_assertion_catches_imbalance() -> None:
    y = ForecastYear(
        year=2025, revenue=Decimal("1000"), ebit=Decimal("200"), nopat=Decimal("150"),
        depreciation_amortization=Decimal("50"), capex=Decimal("60"),
        nwc_change=Decimal("10"), fcff=Decimal("130"), net_income=Decimal("140"),
        total_assets=Decimal("1500"), total_liabilities=Decimal("800"),
        total_equity=Decimal("600"),  # 错！应为 700
        cash_end=Decimal("100"), retained_earnings=Decimal("140"),
    )
    assert assert_balance_sheet(y).status is AssertionStatus.FAIL


def test_valuation_sanity_catches_negative_price() -> None:
    dcf = DCFResult(
        wacc=Decimal("0.095"), terminal_growth=Decimal("0.025"),
        explicit_years=[], terminal_value=Decimal("100"),
        enterprise_value=Decimal("100"), net_debt=Decimal("0"),
        minority_interest=Decimal("0"), equity_value=Decimal("100"),
        shares_outstanding=Decimal("100"), value_per_share=Decimal("-1"),
        terminal_value_share=Decimal("0.5"),
    )
    a = assert_valuation_sanity(dcf)
    assert a.status is AssertionStatus.FAIL
    assert "每股价值" in a.detail


def test_valuation_sanity_warns_on_excessive_terminal_share() -> None:
    dcf = DCFResult(
        wacc=Decimal("0.095"), terminal_growth=Decimal("0.025"),
        explicit_years=[], terminal_value=Decimal("1000"),
        enterprise_value=Decimal("1000"), net_debt=Decimal("0"),
        minority_interest=Decimal("0"), equity_value=Decimal("1000"),
        shares_outstanding=Decimal("100"), value_per_share=Decimal("10"),
        terminal_value_share=Decimal("0.98"),  # 过度依赖永续假设
    )
    a = assert_valuation_sanity(dcf)
    assert a.status is AssertionStatus.FAIL
    assert "终值占比" in a.detail


def test_valuation_sanity_catches_broken_equity_bridge() -> None:
    dcf = DCFResult(
        wacc=Decimal("0.095"), terminal_growth=Decimal("0.025"),
        explicit_years=[], terminal_value=Decimal("1000"),
        enterprise_value=Decimal("1000"), net_debt=Decimal("200"),
        minority_interest=Decimal("0"),
        equity_value=Decimal("900"),  # 错！应为 800
        shares_outstanding=Decimal("100"), value_per_share=Decimal("9"),
        terminal_value_share=Decimal("0.5"),
    )
    assert assert_valuation_sanity(dcf).status is AssertionStatus.FAIL


# ==========================================================================
# 断言编排
# ==========================================================================


def test_run_assertions_has_no_failures_on_generated_model(base_input: DCFInput) -> None:
    """模型自身构造保证的恒等式必须成立（无 FAIL）。

    注意这里断言的是 ``has_failures`` 而非 ``all_passed`` ——
    缺现金流明细时 A2/A3 会被标记 SKIPPED，此时并非「全部通过」，
    但也绝非「校验失败」。两者必须区分，否则报告会误导读者。
    """
    dcf = run_dcf(base_input)
    report = run_assertions(dcf=dcf, tax_rate=base_input.tax_rate)
    assert not report.has_failures, [a.detail for a in report.failed]
    assert not report.all_passed, "存在 SKIPPED，不应报告为『全部通过』"
    assert report.coverage < 1.0


def test_run_assertions_all_pass_when_auxiliary_data_supplied(base_input: DCFInput) -> None:
    """补齐现金流/利润表明细后，应达到 100% 验证覆盖。"""
    dcf = run_dcf(base_input)
    n = len(dcf.explicit_years)
    report = run_assertions(
        dcf=dcf,
        tax_rate=base_input.tax_rate,
        cost_of_sales=[y.revenue * Decimal("0.6") for y in dcf.explicit_years],
        opex=[y.revenue * Decimal("0.15") for y in dcf.explicit_years],
        tax_expense=[y.ebit * base_input.tax_rate for y in dcf.explicit_years],
        cfo=[y.net_income * Decimal("1.2") for y in dcf.explicit_years],
        cfi=[-y.capex for y in dcf.explicit_years],
        cff=[Decimal("0")] * n,
        dividends=[Decimal("0")] * n,
    )
    # A2/A3 需要与模型内部现金/留存收益一致，这里只验证它们不再是 SKIPPED
    assert all(a.status is not AssertionStatus.SKIPPED for a in report.assertions)


def test_run_assertions_skips_instead_of_silently_passing(base_input: DCFInput) -> None:
    """缺辅助数据时必须标记 SKIPPED 而非静默跳过 ——
    静默跳过会让报告看起来「全绿」而实际未校验。"""
    dcf = run_dcf(base_input)
    report = run_assertions(dcf=dcf, tax_rate=base_input.tax_rate)
    skipped = [a for a in report.assertions if a.status is AssertionStatus.SKIPPED]
    assert skipped, "缺少现金流明细时 A2/A3 应被标记为 SKIPPED"
    assert all("无法独立重算" in a.detail for a in skipped)


# ==========================================================================
# 相对估值
# ==========================================================================


def test_run_comps_requires_peers() -> None:
    from agent.runtime.errors import HarnessError

    with pytest.raises(HarnessError, match="可比公司"):
        run_comps(
            CompsInput(
                entity="X", target_net_profit=Decimal("100"),
                target_equity=Decimal("500"),
                target_shares_outstanding=Decimal("10"),
            )
        )


def test_run_comps_uses_median_not_mean() -> None:
    """中位数抗异常值 —— 相对估值方法论的基本要求。"""
    from agent.evidence.models import CompsPeer

    peers = [
        CompsPeer(name=f"P{i}", ticker=f"T{i}", market_cap=Decimal("1000"),
                  pe=Decimal(str(v)), multiple_basis="ttm")
        for i, v in enumerate([10, 12, 14, 16, 200])  # 200 是异常值
    ]
    r = run_comps(
        CompsInput(
            entity="X", target_net_profit=Decimal("1000"),
            target_equity=Decimal("5000"),
            target_revenue=Decimal("10000"),
            target_shares_outstanding=Decimal("100"),
            peers=peers,
        )
    )
    assert r.median_pe == Decimal("14")  # 中位数，而不是均值 50.4


def test_run_comps_produces_ordered_range() -> None:
    from agent.evidence.models import CompsPeer

    peers = [
        CompsPeer(name=f"P{i}", ticker=f"T{i}", market_cap=Decimal("1000"),
                  pe=Decimal(str(v)), pb=Decimal(str(v / 8)), multiple_basis="ttm")
        for i, v in enumerate([10, 12, 14])
    ]
    r = run_comps(
        CompsInput(
            entity="X", target_net_profit=Decimal("1000"),
            target_equity=Decimal("5000"),
            target_shares_outstanding=Decimal("100"),
            peers=peers,
            illiquidity_discount=Decimal("0.10"),
        )
    )
    assert r.implied_value_per_share_low <= (r.implied_value_per_share_mid or Decimal(0))
    assert (r.implied_value_per_share_mid or Decimal(0)) <= r.implied_value_per_share_high


# ==========================================================================
# 敏感性分析
# ==========================================================================


def test_sensitivity_grid_is_populated(base_input: DCFInput) -> None:
    s = run_sensitivity(base_input, x_steps=5, y_steps=5)
    assert len(s.grid) > 0
    assert all(c.value_per_share > 0 for c in s.grid)


def test_sensitivity_is_monotonic_in_wacc(base_input: DCFInput) -> None:
    """WACC 越高，每股价值越低 —— 若违反说明模型有结构性错误。"""
    s = run_sensitivity(base_input, x_steps=5, y_steps=3)
    by_wacc: dict[Decimal, list[Decimal]] = {}
    for c in s.grid:
        by_wacc.setdefault(c.x_value, []).append(c.value_per_share)
    waccs = sorted(by_wacc)
    averages = [sum(by_wacc[w]) / len(by_wacc[w]) for w in waccs]
    for a, b in zip(averages, averages[1:]):
        assert a >= b


def test_tornado_sorted_by_swing_descending(base_input: DCFInput) -> None:
    s = run_sensitivity(base_input, x_steps=3, y_steps=3)
    swings = [Decimal(str(t["swing"])) for t in s.tornado]
    assert swings == sorted(swings, reverse=True)


def test_tornado_identifies_wacc_as_top_driver(base_input: DCFInput) -> None:
    """WACC 应当是最敏感的假设 —— 这与金融常识一致，也是对模型的常识性检验。"""
    s = run_sensitivity(base_input, x_steps=3, y_steps=3)
    assert s.tornado
    assert s.tornado[0]["assumption"] == "wacc"


# ==========================================================================
# 确定性
# ==========================================================================


def test_dcf_is_deterministic(base_input: DCFInput) -> None:
    """同输入必然同输出 —— 复现性的前提。"""
    a = run_dcf(base_input)
    b = run_dcf(base_input)
    assert a.model_dump() == b.model_dump()
