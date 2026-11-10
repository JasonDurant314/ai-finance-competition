"""量纲合理性闸门的契约测试（资产负债表恒等式）。

背景（实测事故，2026-09）：601857 中国石油 2025 年报把"期末现金及现金等价物余额"
印在至少 4 处，口径都是**人民币百万元**：

    p95  「期末现金及现金等价物余额（人民币百万元）」 206,162
    p28/p29/p110  表头明写人民币百万元

但另一处表因表头缺单位判据，落到页级声明"单位：元"，抽出
`206,162 元 × 10000 = 20,616,200,000,000 元`（错 100 倍）。
两条都通过 quote 回读（`206,162` 确实印在原文里），
于是 R4 取净债 = 有息负债 − 现金 → −20.4 万亿，DCF 每股价值被推到 ¥124.41。

本模块用**注入式**用例证明闸门真的会响，而不是只在注释里声称会响。
"""

from __future__ import annotations

from decimal import Decimal

from agent.evidence.models import (
    Fact,
    PeriodType,
    SourceLayer,
    StatementScope,
)
from agent.evidence.store import EvidenceStore, FactLedger, build_confidence

TOTAL_ASSETS_TEXT = "资产总计 2,828,017,000,000"
CASH_OK_TEXT = "期末现金及现金等价物余额 206,162,000,000"
CASH_INFLATED_TEXT = "现金及现金等价物的年末余额 20,616,200,000,000"


def _anchor(store: EvidenceStore, doc_id: str, page: int, text: str, quote: str) -> str:
    store.set_page_text(doc_id, page, text)
    span, _status, _sim = store.verify_and_anchor(
        doc_id=doc_id,
        page=page,
        quote=quote,
        source_layer=SourceLayer.L1_TEXT_LAYER,
    )
    return span.span_id


def _fact(span_id: str, *, doc_id: str, metric: str, value: str,
          fact_id: str, entity: str = "601857", period: str = "2025A") -> Fact:
    return Fact(
        fact_id=fact_id,
        doc_id=doc_id,
        entity=entity,
        metric=metric,
        value=Decimal(value),
        unit="元",
        raw_text=value,
        period=period,
        period_type=PeriodType.POINT_IN_TIME,
        statement_scope=StatementScope.CONSOLIDATED,
        source_layer=SourceLayer.L1_TEXT_LAYER,
        evidence_span_ids=[span_id],
        confidence=0.95,
        confidence_breakdown=build_confidence(source_layer=SourceLayer.L1_TEXT_LAYER),
        extractor="table_grid@1.0.0",
    )


def _ledger_with_three_facts(doc_id: str = "doc_probe") -> FactLedger:
    """资产总额 / 正常现金 / 放大 100 倍的现金，**放在不同页**。

    刻意分页：同页且 bbox 相近时 ``_check_internal_conflict`` 也会标记它们，
    那样就分不清到底是哪个闸门起作用了。本测试要单独验证量纲闸门。
    """
    store = EvidenceStore()
    a = _anchor(store, doc_id, 1, TOTAL_ASSETS_TEXT, "资产总计 2,828,017,000,000")
    b = _anchor(store, doc_id, 2, CASH_OK_TEXT, "期末现金及现金等价物余额 206,162,000,000")
    c = _anchor(store, doc_id, 3, CASH_INFLATED_TEXT,
                "现金及现金等价物的年末余额 20,616,200,000,000")

    ledger = FactLedger(store, run_id="t")
    ledger.add(_fact(a, doc_id=doc_id, metric="total_assets",
                     value="2828017000000", fact_id="fct_assets"))
    ledger.add(_fact(b, doc_id=doc_id, metric="cash_and_equivalents",
                     value="206162000000", fact_id="fct_cash_ok"))
    ledger.add(_fact(c, doc_id=doc_id, metric="cash_and_equivalents",
                     value="20616200000000", fact_id="fct_cash_inflated"))
    return ledger


def test_plausibility_gate_flags_100x_unit_error() -> None:
    """真值 206,162,000,000 放行，放大 100 倍的 20,616,200,000,000 被标记。"""
    ledger = _ledger_with_three_facts()

    flagged = ledger.check_scale_plausibility()

    assert [w["metric"] for w in flagged] == ["cash_and_equivalents"]
    assert flagged[0]["values"] == ["20616200000000", "2828017000000"]

    ok = ledger.get("fct_cash_ok")
    bad = ledger.get("fct_cash_inflated")
    assert ok is not None and bad is not None
    assert not ok.needs_review, "合理值不应被误伤"
    assert bad.needs_review, "越界值必须被标记"
    assert "量纲不合理" in (bad.review_reason or "")
    assert "fct_assets" in (bad.review_reason or ""), "标记里要能顺藤摸瓜找到锚点"


def test_plausibility_gate_is_silent_without_anchor() -> None:
    """没有资产总额锚点时不判断 —— 宁可漏报，不可凭空判定。"""
    store = EvidenceStore()
    c = _anchor(store, "doc_noanchor", 1, CASH_INFLATED_TEXT,
                "现金及现金等价物的年末余额 20,616,200,000,000")
    ledger = FactLedger(store, run_id="t")
    ledger.add(_fact(c, doc_id="doc_noanchor", metric="cash_and_equivalents",
                     value="20616200000000", fact_id="fct_cash_inflated"))

    assert ledger.check_scale_plausibility() == []
    assert not ledger.get("fct_cash_inflated").needs_review  # type: ignore[union-attr]


def test_plausibility_gate_allows_legitimately_cash_rich_company() -> None:
    """阈值刻意放宽：货币资金 = 资产的 1.2 倍不报（重现金行业不该被误伤）。"""
    store = EvidenceStore()
    a = _anchor(store, "doc_rich", 1, "资产总计 1,000,000,000,000",
                "资产总计 1,000,000,000,000")
    b = _anchor(store, "doc_rich", 2, "现金及现金等价物 1,200,000,000,000",
                "现金及现金等价物 1,200,000,000,000")
    ledger = FactLedger(store, run_id="t")
    ledger.add(_fact(a, doc_id="doc_rich", metric="total_assets",
                     value="1000000000000", fact_id="fct_a"))
    ledger.add(_fact(b, doc_id="doc_rich", metric="cash_and_equivalents",
                     value="1200000000000", fact_id="fct_c"))

    assert ledger.check_scale_plausibility() == []


def test_plausibility_gate_uses_revenue_as_second_anchor() -> None:
    """资产锚点缺失时，营业收入仍能兜住 —— 两个锚点独立生效。"""
    store = EvidenceStore()
    r = _anchor(store, "doc_rev", 1, "营业收入 300,000,000,000",
                "营业收入 300,000,000,000")
    c = _anchor(store, "doc_rev", 2, "现金及现金等价物 5,000,000,000,000",
                "现金及现金等价物 5,000,000,000,000")
    ledger = FactLedger(store, run_id="t")
    ledger.add(_fact(r, doc_id="doc_rev", metric="revenue",
                     value="300000000000", fact_id="fct_r"))
    ledger.add(_fact(c, doc_id="doc_rev", metric="cash_and_equivalents",
                     value="5000000000000", fact_id="fct_c"))

    flagged = ledger.check_scale_plausibility()
    assert len(flagged) == 1
    assert flagged[0]["values"] == ["5000000000000", "300000000000"]
    assert ledger.get("fct_c").needs_review  # type: ignore[union-attr]
