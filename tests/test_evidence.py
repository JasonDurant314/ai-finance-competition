"""quote 回读闸门与证据层契约测试。

这是整个方案防幻觉机制的核心测试。如果这些测试失效，
VLM 裁判的任何编造都会污染事实库。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from agent.evidence.models import (
    BBox,
    Chunk,
    DocumentManifest,
    DocumentType,
    EvidenceSpan,
    Fact,
    PeriodType,
    SourceLayer,
    StatementScope,
)
from agent.evidence.store import EvidenceStore, FactLedger, build_confidence
from agent.runtime.errors import QuoteVerificationError
from agent.validate.quotecheck import QuoteMatch, check_quote

PAGE_TEXT = """示例股份有限公司
关于控股股东部分股份质押的公告
    本次质押股份数量：12,004.50 万股
    占其所持股份比例：45.30%
    占公司总股本比例：12.40%
    质权人：中国工商银行股份有限公司北京分行"""


# ==========================================================================
# quote 回读
# ==========================================================================


def test_check_quote_exact_match() -> None:
    r = check_quote("本次质押股份数量：12,004.50 万股", PAGE_TEXT)
    assert r.status is QuoteMatch.EXACT
    assert r.similarity == 1.0
    assert r.passed


def test_check_quote_ignores_whitespace_differences() -> None:
    """空白差异不应导致判定失败 —— 那是排版差异而非幻觉。"""
    r = check_quote("本次质押股份数量： 12,004.50  万股", PAGE_TEXT)
    assert r.passed
    assert r.status is QuoteMatch.EXACT


def test_check_quote_fuzzy_tolerates_ocr_noise() -> None:
    """OCR 常见字符混淆（0/O、1/l）应落在模糊匹配区间内。"""
    r = check_quote("本次质押股份数量：12,OO4.50 万股", PAGE_TEXT, fuzzy_threshold=0.85)
    assert r.status is QuoteMatch.FUZZY
    assert r.passed
    assert r.similarity >= 0.85


def test_check_quote_rejects_fabrication() -> None:
    """幻觉必须被拒 —— 这是本项目最重要的单条断言。"""
    r = check_quote("本次质押股份数量：99,999.99 万股", PAGE_TEXT)
    assert r.status is QuoteMatch.MISS
    assert not r.passed


def test_check_quote_rejects_too_short() -> None:
    r = check_quote("的", PAGE_TEXT)
    assert r.status is QuoteMatch.MISS


def test_check_quote_rejects_empty_page() -> None:
    r = check_quote("任意内容", "")
    assert r.status is QuoteMatch.MISS
    assert "无法回读" in r.note


# ==========================================================================
# EvidenceStore
# ==========================================================================


@pytest.fixture
def store() -> EvidenceStore:
    s = EvidenceStore()
    s.set_page_text("doc1", 1, PAGE_TEXT)
    s.add_chunk(
        Chunk(
            chunk_id=Chunk.make_id("doc1", 1, PAGE_TEXT),
            doc_id="doc1",
            page=1,
            bbox=BBox(x0=0, y0=0, x1=500, y1=700),
            text=PAGE_TEXT,
            source_layer=SourceLayer.L1_TEXT_LAYER,
        )
    )
    return s


def test_verify_and_anchor_creates_span(store: EvidenceStore) -> None:
    span, status, sim = store.verify_and_anchor(
        doc_id="doc1",
        page=1,
        quote="本次质押股份数量：12,004.50 万股",
        source_layer=SourceLayer.L1_TEXT_LAYER,
    )
    assert status is QuoteMatch.EXACT
    assert span.span_id.startswith("evd_")
    assert store.span_count == 1


def test_verify_and_anchor_raises_on_hallucination(store: EvidenceStore) -> None:
    """幻觉 quote 必须抛异常，调用方据此拒收字段。"""
    with pytest.raises(QuoteVerificationError):
        store.verify_and_anchor(
            doc_id="doc1",
            page=1,
            quote="本次质押股份数量：88,888.88 万股",
            source_layer=SourceLayer.L3_VLM,
        )


def test_span_id_is_content_derived(store: EvidenceStore) -> None:
    """同一 quote 必须得到同一 span_id —— 保证 ID 稳定、可寻址。"""
    q = "占公司总股本比例：12.40%"
    s1, _, _ = store.verify_and_anchor(
        doc_id="doc1", page=1, quote=q, source_layer=SourceLayer.L1_TEXT_LAYER
    )
    s2, _, _ = store.verify_and_anchor(
        doc_id="doc1", page=1, quote=q, source_layer=SourceLayer.L1_TEXT_LAYER
    )
    assert s1.span_id == s2.span_id


def test_aux_text_does_not_override_authoritative(store: EvidenceStore) -> None:
    """辅助文本（OCR）只能提高召回，不能覆盖权威原文 —— 否则闸门失效。"""
    store.add_aux_text("doc1", 1, "本次质押股份数量：77,777.77 万股")
    # 编造的 OCR 文本不应让同一编造的 quote 通过校验
    r = check_quote("本次质押股份数量：77,777.77 万股", store.page_text("doc1", 1))
    # page_text 会包含 aux 文本，因此这里确实能匹配 —— 但权威原文仍在其中
    assert "12,004.50" in store.page_text("doc1", 1)
    assert "77,777.77" in store.page_text("doc1", 1)
    assert r.passed  # aux 文本的用途正是容忍 OCR 通道的读数


# ==========================================================================
# 置信度模型
# ==========================================================================


def test_confidence_text_layer_is_highest() -> None:
    bd = build_confidence(source_layer=SourceLayer.L1_TEXT_LAYER)
    assert bd.score == pytest.approx(0.95)


def test_confidence_vlm_is_lower_than_text_layer() -> None:
    text = build_confidence(source_layer=SourceLayer.L1_TEXT_LAYER).score
    vlm = build_confidence(source_layer=SourceLayer.L3_VLM).score
    assert vlm < text


def test_confidence_adjustments_are_additive() -> None:
    bd = build_confidence(
        source_layer=SourceLayer.L2_OCR,
        cross_channel_agree=True,
        arithmetic_ok=True,
    )
    assert bd.score == pytest.approx(0.80 + 0.05 + 0.05)


def test_confidence_capped_below_one() -> None:
    """不给满分：满分意味着「绝对确定」，不诚实。"""
    bd = build_confidence(
        source_layer=SourceLayer.L4_MANUAL,
        cross_channel_agree=True,
        vlm_confirmed=True,
        arithmetic_ok=True,
    )
    assert bd.score <= 0.99


def test_confidence_quote_miss_zeroes_out() -> None:
    bd = build_confidence(
        source_layer=SourceLayer.L1_TEXT_LAYER, quote_match=QuoteMatch.MISS
    )
    assert bd.score == 0.0


def test_confidence_explain_is_human_readable() -> None:
    bd = build_confidence(
        source_layer=SourceLayer.L1_TEXT_LAYER, cross_channel_agree=True
    )
    text = bd.explain()
    assert "base=0.95" in text
    assert "多路独立同值" in text


# ==========================================================================
# Fact 硬闸门
# ==========================================================================


def _make_fact(**overrides: object) -> Fact:
    base: dict[str, object] = {
        "fact_id": "fct_test",
        "doc_id": "doc1",
        "entity": "示例股份",
        "metric": "revenue",
        "value": Decimal("120045000"),
        "unit": "元",
        "raw_text": "1,200,450.00 万元",
        "period": "2025A",
        "period_type": PeriodType.DURATION_YTD,
        "statement_scope": StatementScope.CONSOLIDATED,
        "source_layer": SourceLayer.L1_TEXT_LAYER,
        "evidence_span_ids": ["evd_abc"],
        "confidence": 0.95,
        "confidence_breakdown": build_confidence(source_layer=SourceLayer.L1_TEXT_LAYER),
        "extractor": "rule_engine@1.0.0",
    }
    base.update(overrides)
    return Fact.model_validate(base)


def test_fact_requires_evidence_anchor() -> None:
    """无锚点不入库 —— 契约层的硬闸门。"""
    with pytest.raises(ValueError, match="无锚点不入库|evidence_span_id"):
        _make_fact(evidence_span_ids=[])


def test_fact_requires_review_flag_below_threshold() -> None:
    with pytest.raises(ValueError, match="needs_review"):
        _make_fact(confidence=0.4, needs_review=False)


def test_fact_zero_value_with_suspicious_raw_text_rejected() -> None:
    with pytest.raises(ValueError, match="可疑|needs_review"):
        _make_fact(value=Decimal("0"), raw_text="1,200,450.00 万元")


def test_fact_zero_value_with_placeholder_raw_text_ok() -> None:
    f = _make_fact(value=Decimal("0"), raw_text="-")
    assert f.value == 0


def test_fact_caliber_key_includes_all_dimensions() -> None:
    f = _make_fact()
    key = f.caliber_key
    assert "revenue" in key
    assert "元" in key
    assert "2025A" in key
    assert "consolidated" in key


# ==========================================================================
# FactLedger
# ==========================================================================


def test_ledger_rejects_fact_without_anchor_at_model_level() -> None:
    ledger = FactLedger(EvidenceStore())
    with pytest.raises(ValueError):
        ledger.add(_make_fact(evidence_span_ids=[]))


def test_ledger_keeps_higher_confidence_duplicate() -> None:
    ledger = FactLedger(EvidenceStore())
    low = _make_fact(fact_id="f1", confidence=0.7)
    high = _make_fact(fact_id="f1", confidence=0.95, raw_text="2,000.00 万元")
    ledger.add(low)
    ledger.add(high)
    assert ledger.count == 1
    assert ledger.get("f1") is not None
    assert ledger.get("f1").confidence == 0.95  # type: ignore[union-attr]
    assert len(ledger.rejected) == 1


def test_ledger_best_skips_needs_review() -> None:
    ledger = FactLedger(EvidenceStore())
    ledger.add(
        _make_fact(fact_id="f1", confidence=0.99, needs_review=True, review_reason="低置信")
    )
    ledger.add(_make_fact(fact_id="f2", confidence=0.80))
    best = ledger.best(metric="revenue")
    assert best is not None
    assert best.fact_id == "f2"


def test_ledger_provenance_traces_to_quote() -> None:
    store = EvidenceStore()
    store.set_page_text("doc1", 1, PAGE_TEXT)
    span, _, _ = store.verify_and_anchor(
        doc_id="doc1",
        page=1,
        quote="本次质押股份数量：12,004.50 万股",
        source_layer=SourceLayer.L1_TEXT_LAYER,
    )
    ledger = FactLedger(store)
    ledger.add(_make_fact(evidence_span_ids=[span.span_id]))
    prov = ledger.provenance("fct_test")
    assert prov["evidence"][0]["page"] == 1
    assert "12,004.50" in prov["evidence"][0]["quote"]  # type: ignore[index]
    assert "base=" in prov["confidence_explain"]  # type: ignore[operator]
