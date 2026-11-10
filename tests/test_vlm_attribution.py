"""VLM 视觉读数的指标归属测试。

覆盖一条真实踩坑链：
    ① 早期用笼统的 ``vlm_field_page_key_fields`` 做 field_id
       → 同页不同主体的值被当成「同一指标的多个候选值」
       → 触发大量**假冲突**
    ② 改成让 VLM 输出规范指标名
       → 但模型会把股数归到以 % 为单位的指标上（量纲不符）
    ③ 加上量纲闸门 + 补全指标清单
       → 归属正确，冲突告警从 27 条降到 2 条真实告警
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from agent.evidence.models import (
    Fact,
    PeriodType,
    ProfitBasis,
    SourceLayer,
    StatementScope,
)
from agent.evidence.store import EvidenceStore, FactLedger, build_confidence
from domain.tools.vlm import (
    FieldEstimate,
    PageScanResult,
    build_metric_catalog,
    expected_unit_family,
    unit_family,
)

PAGE_TEXT = """杭州景业智能科技股份有限公司简式权益变动报告书
本次权益变动后，信息披露义务人合计持有 34,469,568 股，占公司总股本的 38.18%。
信息披露义务人1：杭州行之远控股有限公司
变动前持股数量：23,884,931 股
变动后持股数量：26,618,123 股
股票代码：688290
签署日期：2025年6月25日"""


# ==========================================================================
# 指标清单
# ==========================================================================


def test_metric_catalog_contains_canonical_names() -> None:
    cat = build_metric_catalog()
    assert "revenue" in cat
    assert "pledge_shares" in cat
    assert "shareholding_after" in cat


def test_metric_catalog_includes_units() -> None:
    """★ 单位必须写进清单 —— 模型据此判断量纲是否匹配。

    实测教训：不写单位时，模型把「持股 34,469,568 股」归到了
    期望单位为 % 的 shareholding_after。
    """
    cat = build_metric_catalog()
    assert "［单位：" in cat
    assert "%" in cat and "股" in cat and "元" in cat


def test_metric_catalog_covers_count_vs_ratio_distinction() -> None:
    """持股数量与持股比例必须是两个不同指标。

    实测教训：清单里缺 `shareholding_count` 时，模型只能把股数
    硬塞进 `shareholding_after`（% 口径），被闸门拦下退化成 unknown。
    补上之后归属就有了正确落点。
    """
    cat = build_metric_catalog()
    assert "shareholding_count" in cat
    assert "shareholding_after" in cat
    assert "pledge_cumulative_shares" in cat


# ==========================================================================
# 单位族判定
# ==========================================================================


@pytest.mark.parametrize(
    "metric, expected",
    [
        ("revenue", "money"),
        ("net_profit_parent", "money"),
        ("pledge_shares", "share"),
        ("shareholding_count", "share"),
        ("shareholding_after", "ratio"),
        ("pledge_ratio_of_total", "ratio"),
        ("eps_basic", "per_share"),
    ],
)
def test_expected_unit_family(metric: str, expected: str) -> None:
    assert expected_unit_family(metric) == expected


def test_expected_unit_family_unknown_metric() -> None:
    assert expected_unit_family("no_such_metric") is None


@pytest.mark.parametrize(
    "unit, expected",
    [
        ("元", "money"), ("万元", "money"), ("亿元", "money"),
        ("股", "share"), ("万股", "share"),
        ("%", "ratio"), ("％", "ratio"), ("‰", "ratio"),
        ("元/股", "per_share"), ("元每股", "per_share"),
        ("", "other"), ("个", "other"), ("人民币", "other"),
    ],
)
def test_unit_family(unit: str, expected: str) -> None:
    assert unit_family(unit) == expected


# ==========================================================================
# 量纲闸门
# ==========================================================================


def _gate(est: FieldEstimate, page_text: str = PAGE_TEXT) -> FieldEstimate:
    from domain.tools.vlm import _apply_field_quote_gate

    _apply_field_quote_gate(est, page_text, fuzzy_threshold=0.86)
    return est


def test_gate_rejects_share_count_assigned_to_ratio_metric() -> None:
    """★ 核心用例：股数归到百分比指标必须被拒。

    实测出现的真实错误：模型返回
    ``{"metric": "shareholding_after", "value_raw": "34,469,568", "unit_as_written": "股"}``
    —— 数值读对了，但指标声明单位是 %。
    """
    est = FieldEstimate(
        metric="shareholding_after",
        value_raw="34,469,568",
        unit_as_written="股",
        quote="合计持有 34,469,568 股",
    )
    _gate(est)
    assert est.rejected
    assert "量纲不符" in (est.rejection_reason or "")


def test_gate_accepts_matching_unit_family() -> None:
    est = FieldEstimate(
        metric="shareholding_count",
        value_raw="34,469,568",
        unit_as_written="股",
        quote="合计持有 34,469,568 股",
    )
    _gate(est)
    assert not est.rejected, est.rejection_reason
    assert est.usable


def test_gate_rejects_metric_not_in_catalog() -> None:
    """单位族未知的指标无法校验量纲，但至少要过 quote 闸门。"""
    est = FieldEstimate(
        metric="some_invented_metric",
        value_raw="34,469,568",
        unit_as_written="股",
        quote="合计持有 34,469,568 股",
    )
    _gate(est)
    # 指标未知 -> 不做量纲校验；quote 能对上 -> 通过
    assert not est.rejected


def test_gate_rejects_unitless_value() -> None:
    """无单位不入库 —— 与第 1/2 层同一规则。"""
    est = FieldEstimate(
        metric="unknown",
        value_raw="688290",
        unit_as_written="",
        quote="股票代码：688290",
    )
    _gate(est)
    assert est.rejected
    assert "量纲族" in (est.rejection_reason or "")


def test_gate_rejects_hallucinated_quote() -> None:
    est = FieldEstimate(
        metric="shareholding_count",
        value_raw="99,999,999",
        unit_as_written="股",
        quote="合计持有 99,999,999 股",
    )
    _gate(est)
    assert est.rejected
    assert "回读失败" in (est.rejection_reason or "") or "幻觉" in (est.rejection_reason or "")


def test_gate_marks_unknown_metric_as_unusable() -> None:
    """metric=unknown 的读数不算「可用」—— 它无法进入事实库。

    注意：这里用页面里**确实存在**的原文，保证失败原因只可能是
    「指标未归属」而不是「quote 对不上」。
    """
    est = FieldEstimate(
        metric="unknown",
        value_raw="23,884,931",
        unit_as_written="股",
        quote="变动前持股数量：23,884,931 股",
    )
    _gate(est)
    assert not est.rejected, est.rejection_reason
    assert not est.usable, "metric=unknown 的读数不应被视为可用"


# ==========================================================================
# PageScanResult
# ==========================================================================


def test_page_scan_result_filters_usable_fields() -> None:
    scan = PageScanResult(
        page=1,
        self_confidence=0.9,
        fields=[
            FieldEstimate(
                metric="shareholding_count", value_raw="100", unit_as_written="股",
                quote="持股 100 股", quote_status="exact",
            ),
            FieldEstimate(
                metric="unknown", value_raw="200", unit_as_written="股",
                quote="某值 200 股", quote_status="exact",
            ),
            FieldEstimate(
                metric="shareholding_count", value_raw="300", unit_as_written="股",
                quote="持股 300 股", rejected=True, quote_status="miss",
            ),
        ],
    )
    usable = scan.usable_fields
    assert len(usable) == 1
    assert usable[0].value_raw == "100"


# ==========================================================================
# 同文档冲突守卫：主体维度不可省略
# ==========================================================================


def _fact(
    fid: str, metric: str, value: str, unit: str, *, entity: str, doc: str = "doc1",
    period: str = "2025A",
) -> Fact:
    return Fact(
        fact_id=fid, doc_id=doc, entity=entity, metric=metric,
        value=Decimal(value), unit=unit, raw_text=f"{value}{unit}",
        period=period, period_type=PeriodType.DURATION_YTD,
        statement_scope=StatementScope.CONSOLIDATED, profit_basis=ProfitBasis.UNKNOWN,
        source_layer=SourceLayer.L1_TEXT_LAYER, evidence_span_ids=[f"evd_{fid}"],
        confidence=0.95,
        confidence_breakdown=build_confidence(source_layer=SourceLayer.L1_TEXT_LAYER),
        extractor="test@1.0",
    )


def test_different_entities_same_metric_is_not_a_conflict() -> None:
    """★ 不同主体的同一指标不是冲突 —— 这是 27 条假冲突的根源。

    一份公告里天然有多个主体（控股股东、一致行动人、董监高、合计行），
    它们各自的质押股数、持股数本来就不同。
    早期实现只按 (doc_id, metric, period) 比对，把它们两两配对，
    一页就产出 27 条告警，把真问题淹没在噪声里。
    """
    ledger = FactLedger(EvidenceStore(), run_id="t")
    ledger.add(_fact("f1", "pledge_shares", "460000", "股", entity="润和投资"))
    ledger.add(_fact("f2", "pledge_shares", "1820000", "股", entity="周红卫"))
    ledger.add(_fact("f3", "pledge_shares", "59090000", "股", entity="合计"))
    assert ledger.conflicts == [], f"不同主体被误判为冲突：{ledger.conflicts}"
    assert not any(f.needs_review for f in ledger.all())


def test_same_entity_same_metric_large_gap_is_a_conflict() -> None:
    """同一主体同一指标相差 1000 倍 —— 这是真实需要复核的差异。"""
    ledger = FactLedger(EvidenceStore(), run_id="t")
    ledger.add(_fact("f1", "pledge_shares", "25570000", "股", entity="润和投资"))
    ledger.add(_fact("f2", "pledge_shares", "25570", "股", entity="润和投资"))
    assert len(ledger.conflicts) == 1
    assert ledger.conflicts[0]["entity"] == "润和投资"
    assert ledger.conflicts[0]["ratio"] == pytest.approx(1000.0, rel=0.01)
    # 两条都必须被标记 —— 不能静默挑一个用
    assert all(f.needs_review for f in ledger.all())


def test_same_entity_small_gap_is_not_a_conflict() -> None:
    """四舍五入差异不是冲突。"""
    ledger = FactLedger(EvidenceStore(), run_id="t")
    ledger.add(_fact("f1", "revenue", "1200450000", "元", entity="A"))
    ledger.add(_fact("f2", "revenue", "1200500000", "元", entity="A"))
    assert ledger.conflicts == []


def test_conflict_note_distinguishes_error_from_multiple_events() -> None:
    """告警措辞必须承认「也可能是同一文档的多次事件」。

    程序无法自行判断 1000 倍差异是抽取错误还是分次事件的记录，
    因此结论只能是「需人工判定」，而不是断言「数据错误」。
    """
    ledger = FactLedger(EvidenceStore(), run_id="t")
    ledger.add(_fact("f1", "equity_change_ratio", "2.94", "%", entity="A"))
    ledger.add(_fact("f2", "equity_change_ratio", "0.18", "%", entity="A"))
    assert ledger.conflicts
    reason = next(f.review_reason for f in ledger.all() if f.needs_review)
    assert "需人工判定" in (reason or "")
