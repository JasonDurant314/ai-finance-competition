"""期间自相矛盾闸门的测试。

实测动机（2026-09 第十三轮）：601857 的 **2025 年报** p7 五年摘要表把
**2024 年**的资本开支 ``275,849`` 标成了 ``2025A``，而 2025 年真值是
``269,089``。因为 ``275,849 > 269,089``，R4 的"同期间取最大值"必然取到
**2024 年的数**并当作 2025 年的 —— 数字印在原文里、quote 回读能过、
置信度 0.95、**全程不报错**。

原有的同表冲突守卫抓不到它：那条要求"同主体 + 同指标 + **同期间** + 同表"，
而这里的问题恰恰是期间不一致。
"""

from __future__ import annotations

from decimal import Decimal

from agent.evidence.models import (
    BBox,
    Chunk,
    EvidenceSpan,
    Fact,
    PeriodType,
    ProfitBasis,
    SourceLayer,
    StatementScope,
    utcnow,
)
from agent.evidence.store import EvidenceStore, FactLedger, build_confidence


def _store() -> EvidenceStore:
    return EvidenceStore()


def _fact(
    store: EvidenceStore,
    *,
    doc_id: str,
    entity: str,
    metric: str,
    value: str,
    period: str,
    page: int = 7,
) -> Fact:
    text = f"{metric} {value} {period}"
    chunk = Chunk(
        chunk_id=Chunk.make_id(doc_id, page, text),
        doc_id=doc_id,
        page=page,
        bbox=BBox(x0=0, y0=0, x1=100, y1=10),
        text=text,
        source_layer=SourceLayer.L1_TEXT_LAYER,
    )
    store.add_chunk(chunk)
    span = EvidenceSpan(
        span_id=EvidenceSpan.make_id(doc_id, chunk.chunk_id, text),
        doc_id=doc_id,
        chunk_id=chunk.chunk_id,
        page=page,
        quote=text,
        source_layer=SourceLayer.L1_TEXT_LAYER,
    )
    store.add_span(span)
    return Fact(
        fact_id=f"fct_{doc_id}_{metric}_{value}_{period}",
        doc_id=doc_id,
        entity=entity,
        metric=metric,
        value=Decimal(value),
        unit="元",
        raw_text=value,
        period=period,
        period_type=PeriodType.DURATION_YTD,
        statement_scope=StatementScope.CONSOLIDATED,
        profit_basis=ProfitBasis.UNKNOWN,
        source_layer=SourceLayer.L1_TEXT_LAYER,
        evidence_span_ids=[span.span_id],
        channel_readings=[],
        confidence=0.95,
        confidence_breakdown=build_confidence(source_layer=SourceLayer.L1_TEXT_LAYER),
        needs_review=False,
        extractor="test@1.0.0",
        extracted_at=utcnow(),
    )


def test_same_value_under_two_periods_is_flagged() -> None:
    """同一个值被标成两个期间 → 两条都必须标为需复核。"""
    store = _store()
    ledger = FactLedger(store)
    f1 = _fact(store, doc_id="doc_2025", entity="601857", metric="capex",
               value="275849000000", period="2025A")
    f2 = _fact(store, doc_id="doc_2025", entity="601857", metric="capex",
               value="275849000000", period="2024A")
    ledger.add(f1)
    ledger.add(f2)

    flagged = ledger.check_period_contradiction()
    assert len(flagged) == 1
    assert flagged[0]["kind"] == "period_contradiction"
    assert flagged[0]["periods"] == ["2024A", "2025A"]
    # **两条都标** —— 程序无法判断哪条的期间是对的，挑一条留就是猜
    assert f1.needs_review and f2.needs_review
    assert "期间" in (f1.review_reason or "")


def test_different_values_same_period_are_not_flagged() -> None:
    """同期间不同值不属于"期间矛盾" —— 那是同表冲突守卫的职责，别抢。"""
    store = _store()
    ledger = FactLedger(store)
    ledger.add(_fact(store, doc_id="d1", entity="601857", metric="capex",
                     value="269089000000", period="2025A"))
    ledger.add(_fact(store, doc_id="d1", entity="601857", metric="capex",
                     value="205094000000", period="2025A"))
    assert ledger.check_period_contradiction() == []


def test_same_value_across_documents_is_not_flagged() -> None:
    """**跨文档**的同值不同期间是正常的（2024 年报与 2025 年报都会印 2024 年数）。

    判据必须限定在同一份文档内 —— 否则每一份年报的"上期比较数"都会被误标。
    """
    store = _store()
    ledger = FactLedger(store)
    ledger.add(_fact(store, doc_id="doc_A", entity="601857", metric="capex",
                     value="275849000000", period="2024A"))
    ledger.add(_fact(store, doc_id="doc_B", entity="601857", metric="capex",
                     value="275849000000", period="2025A"))
    assert ledger.check_period_contradiction() == []
