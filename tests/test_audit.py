"""R5 核查规则测试 —— 含**误报控制**与黄金测试集评测。

本文件同时承担两个作用：
    1. 单元测试：每类错误能被检出
    2. **误报回归基线**：`test_no_false_positives_on_clean_draft` 保证
       一份完全正确的草稿不会产生任何发现。这是「误报控制」评分项
       的直接防线 —— 一旦规则改动引入误报，CI 立刻失败。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from agent.evidence.models import (
    ErrorType,
    Fact,
    PeriodType,
    ProfitBasis,
    Severity,
    SourceLayer,
    StatementScope,
)
from agent.evidence.store import EvidenceStore, FactLedger, build_confidence
from domain.tools.audit_rules import (
    Claim,
    MultipleCheckInput,
    check_arithmetic,
    check_calibers,
    check_citations,
    check_multiples,
    check_units,
    detect_statement_scope,
    extract_claims,
    run_all_checks,
)
from pipelines.r5_audit.check import evaluate_findings


# ==========================================================================
# 测试用事实库
# ==========================================================================


def _fact(
    fact_id: str,
    metric: str,
    value: str,
    unit: str,
    *,
    period: str = "2025A",
    scope: StatementScope = StatementScope.CONSOLIDATED,
    basis: ProfitBasis = ProfitBasis.UNKNOWN,
    entity: str = "示例股份",
) -> Fact:
    return Fact(
        fact_id=fact_id,
        doc_id="doc1",
        entity=entity,
        metric=metric,
        value=Decimal(value),
        unit=unit,
        raw_text=f"{value} {unit}",
        period=period,
        period_type=PeriodType.DURATION_YTD,
        statement_scope=scope,
        profit_basis=basis,
        source_layer=SourceLayer.L1_TEXT_LAYER,
        evidence_span_ids=[f"evd_{fact_id}"],
        confidence=0.95,
        confidence_breakdown=build_confidence(source_layer=SourceLayer.L1_TEXT_LAYER),
        extractor="test@1.0.0",
    )


@pytest.fixture
def ledger() -> FactLedger:
    lg = FactLedger(EvidenceStore(), run_id="test")
    lg.add(_fact("f_rev", "revenue", "12004500000", "元"))
    lg.add(_fact("f_np", "net_profit_parent", "2456000000", "元",
                 basis=ProfitBasis.NET_PARENT))
    lg.add(_fact("f_npe", "net_profit_parent_ex_nonrec", "2382000000", "元",
                 basis=ProfitBasis.NET_PARENT_EX_NONREC))
    lg.add(_fact("f_tot", "net_profit_total", "2503000000", "元",
                 basis=ProfitBasis.NET_TOTAL))
    lg.add(_fact("f_eq", "equity_parent", "16805000000", "元"))
    lg.add(_fact("f_sh", "shares_outstanding", "968000000", "股"))
    lg.add(_fact("f_ocf", "operating_cash_flow", "3124500000", "元"))
    lg.add(_fact("f_debt", "short_term_borrowing", "850000000", "元"))
    return lg


# ==========================================================================
# 主张抽取
# ==========================================================================


def test_extract_claims_finds_number_with_unit() -> None:
    claims = extract_claims("公司实现营业收入 1,200,450.00 万元。")
    assert len(claims) == 1
    assert claims[0].value == Decimal("1200450.00")
    assert claims[0].unit == "万元"
    assert claims[0].metric_hint == "revenue"


def test_extract_claims_skips_years() -> None:
    """裸年份不是财务数值，不应被当成主张。"""
    claims = extract_claims("2025 年公司经营情况良好，2024 年同期为 100 万元。")
    values = [c.value for c in claims]
    assert Decimal("2025") not in values
    assert Decimal("2024") not in values


def test_extract_claims_attaches_section() -> None:
    text = "## 四、估值分析\n\n公司市盈率为 45.30 倍。"
    claims = extract_claims(text)
    assert claims
    assert claims[0].section is not None
    assert "估值分析" in claims[0].section


def test_extract_claims_recognizes_metric_alias() -> None:
    claims = extract_claims("归属于母公司股东的净利润为 245,600.00 万元。")
    assert claims
    assert claims[0].metric_hint == "net_profit_parent"


# ==========================================================================
# 口径检测（误报控制的关键）
# ==========================================================================


def test_detect_statement_scope_consolidated() -> None:
    assert detect_statement_scope("合并报表口径下营业收入为…") == "consolidated"


def test_detect_statement_scope_parent_only() -> None:
    assert detect_statement_scope("母公司报表口径下…") == "parent_only"
    assert detect_statement_scope("母公司口径…") == "parent_only"


def test_detect_statement_scope_ignores_attributable_to_parent() -> None:
    """★ 误报控制的核心用例。

    「归属于母公司股东的净利润」中的「母公司」指的是**合并报表中
    归属于母公司股东的部分**，而不是「母公司报表口径」。
    若误判为 parent_only，每一条归母指标都会被报成口径错误。
    """
    assert detect_statement_scope("归属于母公司股东的净利润为 245,600 万元") is None
    assert detect_statement_scope("归属于上市公司股东的净利润") is None
    assert detect_statement_scope("实现归属于母公司所有者的净利润") is None


# ==========================================================================
# E1 单位
# ==========================================================================


def test_check_units_detects_magnitude_gap(ledger: FactLedger) -> None:
    """量级差 10^4 —— 数值系数不同但差恰好是一个「万元/亿元」量级。

    与 unit_mismatch 用例的区别：那里数值**完全相同**只是单位标错；
    这里是数值被多写了两位、又配了错误的单位，属于更隐蔽的一类。
    """
    # 报告 1,200,450,000.00 亿元（≈1.2e17 元）vs 原文 12,004,500,000 元 → 差 10^4... 实为 10^8/10^4
    text = "公司实现营业收入 12,004,500,000.00 亿元。"
    claims = extract_claims(text)
    res = check_units(claims, ledger)
    assert res.checked >= 1
    assert res.findings, "量级差达到 10^4 必须被报为单位错误"
    assert res.findings[0].error_type is ErrorType.E1_UNIT
    assert res.findings[0].severity is Severity.CONFIRMED
    assert res.findings[0].correct_value is not None


def test_check_units_detects_unit_label_mismatch(ledger: FactLedger) -> None:
    """数值与原文一致但单位标错 —— 最典型的单位错误。"""
    text = "归属于母公司股东的净利润为 2,456,000,000.00 万元。"
    res = check_units(extract_claims(text), ledger)
    assert res.findings
    assert "万元" in res.findings[0].reason
    assert "元" in (res.findings[0].correct_value or "")


def test_check_units_no_false_positive_on_correct_value(ledger: FactLedger) -> None:
    text = "公司实现营业收入 1,200,450.00 万元。"
    res = check_units(extract_claims(text), ledger)
    assert not res.findings


# ==========================================================================
# E2 估值倍数
# ==========================================================================


def test_check_multiples_detects_free_float_marketcap() -> None:
    """PE 用流通市值算但表述为一般 PE —— 官方考察点之一。"""
    ref = MultipleCheckInput(
        market_cap_total=Decimal("38000000000"),
        market_cap_free_float=Decimal("19000000000"),
        net_profit_parent=Decimal("2456000000"),
        source_fact_ids={
            "market_cap_total": "f_mc",
            "net_profit_parent": "f_np",
        },
    )
    text = "公司市盈率为 7.74 倍。"  # 19e9 / 2.456e9 = 7.736 ≈ 7.74（流通市值口径）
    res = check_multiples(text, ref, claims=extract_claims(text))
    assert res.findings
    assert res.findings[0].error_type is ErrorType.E2_MULTIPLE
    assert "流通市值" in res.findings[0].reason
    assert res.findings[0].source_evidence, "CONFIRMED 级发现必须携带来源证据"


def test_check_multiples_no_false_positive_on_correct_pe() -> None:
    ref = MultipleCheckInput(
        market_cap_total=Decimal("38000000000"),
        net_profit_parent=Decimal("2456000000"),
    )
    text = "公司市盈率为 15.47 倍。"  # 38e9 / 2.456e9 = 15.472 ✓
    res = check_multiples(text, ref, claims=extract_claims(text))
    assert not res.findings


# ==========================================================================
# E3 口径
# ==========================================================================


def test_check_calibers_detects_scope_mismatch(ledger: FactLedger) -> None:
    text = "母公司报表口径下，公司实现营业收入 1,200,450.00 万元。"
    res = check_calibers(extract_claims(text), ledger, text=text)
    assert res.findings
    assert res.findings[0].error_type is ErrorType.E3_CALIBER


def test_check_calibers_no_false_positive_on_attributable(ledger: FactLedger) -> None:
    """★ 曾经的误报重灾区：归母表述被误判为母公司口径。"""
    text = "公司实现归属于母公司股东的净利润 245,600.00 万元。"
    res = check_calibers(extract_claims(text), ledger, text=text)
    assert not res.findings, [f.reason for f in res.findings]


# ==========================================================================
# E4 引用
# ==========================================================================


def test_check_citations_detects_unsourced_number(ledger: FactLedger) -> None:
    """金额类断言若在事实库中找不到来源，应报 E4。

    注意避开比率/倍数单位 —— 那类派生量由 E5 负责（见下一个测试）。
    """
    text = "公司研发投入 50,000.00 万元。"
    res = check_citations(extract_claims(text), ledger)
    assert res.findings
    assert res.findings[0].error_type is ErrorType.E4_CITATION
    assert res.findings[0].severity in (Severity.CONFIRMED, Severity.SUSPECTED)


def test_check_citations_tolerates_rounding_of_known_value(ledger: FactLedger) -> None:
    """已知数值的小幅四舍五入不应被报成「无来源」—— 那是误报。"""
    # 营业收入 12,004,500,000 元；草稿写 1,200,000.00 万元（相对差 0.04%）
    text = "公司实现营业收入 1,200,000.00 万元。"
    res = check_citations(extract_claims(text), ledger)
    assert not res.findings, [f.reason for f in res.findings]


def test_check_citations_skips_derived_metrics(ledger: FactLedger) -> None:
    """★ 误报控制：增速/占比这类派生量不适用「查来源」判定。"""
    text = "公司营业收入同比增长 7.92%，毛利率为 48.33%。"
    res = check_citations(extract_claims(text), ledger)
    assert not res.findings, [f.reason for f in res.findings]


def test_check_citations_matches_sourced_number(ledger: FactLedger) -> None:
    text = "公司实现营业收入 1,200,450.00 万元。"
    res = check_citations(extract_claims(text), ledger)
    assert not res.findings


# ==========================================================================
# E5 算术
# ==========================================================================


def test_check_arithmetic_detects_wrong_yoy(ledger: FactLedger) -> None:
    """同比增速算错 —— 由事实库两期数据独立重算。"""
    lg = FactLedger(EvidenceStore(), run_id="t")
    lg.add(_fact("a", "revenue", "12004500000", "元", period="2025A"))
    lg.add(_fact("b", "revenue", "11123000000", "元", period="2024A"))
    text = "公司实现营业收入 1,200,450.00 万元，同比增长 20.00%。"
    # 实际增速 = (12,004,500,000 − 11,123,000,000) / 11,123,000,000 = 7.92%
    res = check_arithmetic(text, lg, claims=extract_claims(text))
    assert res.findings
    assert res.findings[0].error_type is ErrorType.E5_ARITHMETIC
    assert "7.9" in (res.findings[0].correct_value or "")


def test_check_arithmetic_no_false_positive_on_correct_yoy() -> None:
    lg = FactLedger(EvidenceStore(), run_id="t")
    lg.add(_fact("a", "revenue", "12004500000", "元", period="2025A"))
    lg.add(_fact("b", "revenue", "11123000000", "元", period="2024A"))
    text = "公司实现营业收入 1,200,450.00 万元，同比增长 7.92%。"
    res = check_arithmetic(text, lg, claims=extract_claims(text))
    assert not res.findings, [f.reason for f in res.findings]


def test_yoy_recompute_never_mixes_entities() -> None:
    """★ 同比重算**绝不能跨主体配对**。

    实测动机：表格抽取上线后，一份材料里会出现几十家子公司（以及「合计」行）
    的同一指标数据 —— 一次运行里 revenue 跨 80+ 个主体。
    若按"取两个不同期间的事实"直接配对，就会拿 A 公司 2025 年与
    B 公司 2024 年的数据算出一个根本不存在的增速，并作为「错误」报给用户。

    误报是核查系统最致命的失败模式，因此这条必须锁死。
    """
    lg = FactLedger(EvidenceStore(), run_id="t")
    # 母公司与子公司各只有一期 —— 单看期间数够 2，但一对都不该配
    lg.add(_fact("p", "revenue", "12004500000", "元", period="2025A", entity="母公司"))
    lg.add(_fact("s", "revenue", "11123000000", "元", period="2024A", entity="某子公司"))
    text = "公司实现营业收入 1,200,450.00 万元，同比增长 20.00%。"
    res = check_arithmetic(text, lg, claims=extract_claims(text))
    assert not res.findings, (
        "跨主体配出了一对期间并报了假增速：" + str([f.reason for f in res.findings])
    )


def test_yoy_recompute_refuses_when_multiple_entities_have_two_periods() -> None:
    """多个主体各含两期数据、草稿又没点名 → **跳过**，不猜。

    实测动机：表格抽取上线后，一份材料里会出现几十家子公司（以及「合计」行）
    的同一指标数据 —— 一次运行里 revenue 跨 80+ 个主体。
    无论按"期间数最多"还是"事实最多"挑一个主体，都是在**猜**；
    猜错就是报一个不存在的错误，而误报比漏报更伤可信度。
    """
    lg = FactLedger(EvidenceStore(), run_id="t")
    lg.add(_fact("a", "revenue", "12004500000", "元", period="2025A", entity="母公司"))
    lg.add(_fact("b", "revenue", "11123000000", "元", period="2024A", entity="母公司"))
    for i, (p, v) in enumerate([("2023A", "999"), ("2022A", "888"), ("2021A", "777")]):
        lg.add(_fact(f"x{i}", "revenue", v, "元", period=p, entity="某子公司"))

    text = "公司实现营业收入 1,200,450.00 万元，同比增长 20.00%。"
    res = check_arithmetic(text, lg, claims=extract_claims(text))
    assert not res.findings, (
        "草稿没点名主体，不该报出「错误」：" + str([f.reason for f in res.findings])
    )
    assert any("跳过同比重算" in n for n in res.notes), res.notes


def test_yoy_recompute_uses_named_entity() -> None:
    """草稿点名了主体 → 就用那个主体的两期数据重算。"""
    lg = FactLedger(EvidenceStore(), run_id="t")
    lg.add(_fact("a", "revenue", "12004500000", "元", period="2025A", entity="甲公司"))
    lg.add(_fact("b", "revenue", "11123000000", "元", period="2024A", entity="甲公司"))
    lg.add(_fact("c", "revenue", "500", "元", period="2025A", entity="乙公司"))
    lg.add(_fact("d", "revenue", "400", "元", period="2024A", entity="乙公司"))

    text = "甲公司实现营业收入 1,200,450.00 万元，同比增长 20.00%。"
    res = check_arithmetic(text, lg, claims=extract_claims(text))
    assert res.findings, "点名了主体就应该能重算"
    assert "7.9" in (res.findings[0].correct_value or "")


# ==========================================================================
# 汇总与抑制
# ==========================================================================


def test_run_all_checks_suppresses_lower_priority_at_same_location(
    ledger: FactLedger,
) -> None:
    """同一位置若已有 E1 单位错误，不应再重复报 E4 无来源。"""
    text = "归属于母公司股东的净利润为 2,456,000,000.00 万元。"
    res = run_all_checks(text, ledger)
    types = [f.error_type for f in res.findings]
    assert ErrorType.E1_UNIT in types
    # E4 与 E1 指向同一处，应被抑制
    e1_ranges = {
        f.location.char_range for f in res.findings if f.error_type is ErrorType.E1_UNIT
    }
    e4_ranges = {
        f.location.char_range for f in res.findings if f.error_type is ErrorType.E4_CITATION
    }
    assert not (e1_ranges & e4_ranges), "同位置的 E4 应被 E1 抑制"


def test_run_all_checks_deduplicates() -> None:
    lg = FactLedger(EvidenceStore(), run_id="t")
    lg.add(_fact("f", "revenue", "12004500000", "元"))
    text = "营业收入 999,999.00 万元，营业收入 999,999.00 万元。"
    res = run_all_checks(text, lg)
    ids = [f.finding_id for f in res.findings]
    assert len(ids) == len(set(ids)), "finding_id 必须唯一"


# ==========================================================================
# ★★ 误报回归基线 ★★
# ==========================================================================


def test_no_false_positives_on_clean_draft(ledger: FactLedger) -> None:
    """一份**完全正确**的草稿必须产生零发现。

    这是全文件最重要的测试。任何规则改动引入误报，这里立刻失败。
    误报会直接拉低「误报控制」评分，因此这条基线必须守住。
    """
    clean = """# 示例股份投资价值分析

## 一、经营业绩

报告期内，公司实现营业收入 1,200,450.00 万元，同比增长 7.92%。
实现归属于母公司股东的净利润 245,600.00 万元。
扣除非经常性损益后归属于母公司股东的净利润为 238,200.00 万元。

## 二、财务状况

报告期末，归属于母公司所有者权益合计 1,680,500.00 万元，
总股本 968,000,000 股。

## 三、现金流

经营活动产生的现金流量净额为 312,450.00 万元，盈利质量良好。
"""
    res = run_all_checks(clean, ledger)
    assert not res.findings, "\n".join(
        f"{f.error_type.value} @ {f.location.quote[:60]!r}: {f.reason}"
        for f in res.findings
    )


# ==========================================================================
# 黄金测试集评测
# ==========================================================================


def test_evaluate_findings_computes_precision_recall() -> None:
    from agent.evidence.models import Finding, FindingLocation

    draft = "0123456789ABCDEFGHIJ"
    findings = [
        Finding(
            finding_id="E1-001", error_type=ErrorType.E1_UNIT,
            severity=Severity.CONFIRMED,
            location=FindingLocation(char_range=(0, 5), quote="01234"),
            reason="test", suggestion="fix", confidence=0.9,
            detector="t", source_evidence=["evd_1"],
        ),
        Finding(
            finding_id="E1-002", error_type=ErrorType.E1_UNIT,
            severity=Severity.CONFIRMED,
            location=FindingLocation(char_range=(15, 20), quote="FGHIJ"),
            reason="test", suggestion="fix", confidence=0.9,
            detector="t", source_evidence=["evd_2"],
        ),
    ]
    labels = [
        {"error_id": "G1", "type": "E1_unit", "char_range": [0, 5], "is_real_error": True},
        {"error_id": "G2", "type": "E1_unit", "char_range": [8, 12], "is_real_error": True},
        {"error_id": "T1", "type": "E1_unit", "char_range": [15, 20], "is_real_error": False},
    ]
    m = evaluate_findings(findings, labels, draft)
    assert m.true_positive == 1      # 命中 G1
    assert m.false_negative == 1     # 漏掉 G2
    # ★ 回归测试：**一条发现最多算一个假正例**。
    #
    #   这里只有 2 条发现：E1-001 命中 G1（TP），E1-002 谁也没匹配上（FP）。
    #   而 E1-002 恰好落在陷阱项 T1 上。旧实现把它同时计入 `fp` 与 `trap_fp`，
    #   再相加得 FP=2 —— **假正例比发现总数还多**，指标自相矛盾。
    #   实测在真实评测集上就是这个症状：3 份草稿共 8 条发现，指标却报 FP=9。
    assert m.false_positive == 1
    assert m.false_positive_on_trap == 1   # 且要能看出这 1 条是被陷阱钓到的
    # precision = TP / (TP + FP) = 1 / (1 + 1)
    assert m.precision == pytest.approx(0.5)
    assert m.recall == pytest.approx(0.5)


def test_evaluate_findings_false_positive_never_exceeds_findings() -> None:
    """不变量：假正例不可能多于发现总数（陷阱不能把一条发现数两遍）。"""
    from agent.evidence.models import Finding, FindingLocation

    draft = "0123456789"
    findings = [
        Finding(
            finding_id=f"F-{i}", error_type=ErrorType.E1_UNIT,
            severity=Severity.CONFIRMED,
            location=FindingLocation(char_range=(i * 2, i * 2 + 2), quote="xx"),
            reason="t", suggestion="s", confidence=0.9, detector="t",
            source_evidence=["e"],
        )
        for i in range(3)
    ]
    # 3 个陷阱项、0 个真错误 → 3 条发现全是误报，且每条都落在陷阱上
    labels = [
        {"type": "E1_unit", "char_range": [i * 2, i * 2 + 2], "is_real_error": False}
        for i in range(3)
    ]
    m = evaluate_findings(findings, labels, draft)
    assert m.false_positive == 3
    assert m.false_positive_on_trap == 3
    assert m.false_negative == 0


def test_evaluate_findings_perfect_match() -> None:
    from agent.evidence.models import Finding, FindingLocation

    draft = "0123456789"
    f = Finding(
        finding_id="E1-001", error_type=ErrorType.E1_UNIT,
        severity=Severity.CONFIRMED,
        location=FindingLocation(char_range=(0, 5), quote="01234"),
        reason="t", suggestion="s", confidence=0.9, detector="t",
        source_evidence=["e"],
    )
    labels = [{"type": "E1_unit", "char_range": [0, 5], "is_real_error": True}]
    m = evaluate_findings([f], labels, draft)
    assert m.precision == 1.0
    assert m.recall == 1.0
    assert m.f1 == 1.0
    assert m.coverage == 1.0
