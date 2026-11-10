"""表格 → 事实 的测试。

覆盖三个新模块，每个都对应一个实测踩过的坑：

* ``agent.validate.numeric.repair_line_broken_numbers``
  —— 折行切断的数字/单位，不改就整页抽不出事实
* ``pipelines.r1_extract.table_facts``
  —— 表头识别、行主体选择、逐列单位、续表继承表头
* ``pipelines.r1_extract.entity_attribution``
  —— 表格行里的值不能挂在披露主体名下
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from agent.validate.numeric import repair_line_broken_numbers
from pipelines.r1_extract.entity_attribution import cell_lookup, reattribute
from pipelines.r1_extract.table_facts import (
    build_header,
    extract_table_facts,
    find_unit_declaration,
    map_columns_to_metrics,
    pick_subject_column,
    subject_of_row,
    unit_in_label,
)

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "agent" / "sample"
ROOT_SRC = Path(__file__).resolve().parents[1]

ALIAS = {
    "持股比例": "shareholding_ratio",
    "负债总额": "total_liabilities",
    "预计担保额度": "guarantee_amount",
    "营业收入": "revenue",
    "净利润": "net_profit_total",
    "净资产": "equity_total",
    "资产总额": "total_assets",
}
UNITS = {
    "shareholding_ratio": "%",
    "guarantee_amount": "元",
    "revenue": "元",
    "net_profit_total": "元",
    "equity_total": "元",
    "total_assets": "元",
}


# ==========================================================================
# 折行修复
# ==========================================================================


@pytest.mark.parametrize("src,want", [
    ("42,399,138\n.44", "42,399,138.44"),   # 折行落在小数点**之前**
    ("74,088,104\n.13", "74,088,104.13"),
    ("-860,467.9\n8", "-860,467.98"),        # 小数位被截断
    ("626,207.6\n6", "626,207.66"),
    ("5,787,118.\n61", "5,787,118.61"),      # 折行落在小数点**之后**
])
def test_repair_handles_decimal_point_breaks(src: str, want: str) -> None:
    """★ 小数点附近的三种折行都要能接回去。

    实测代价：这三种形态在其中一份材料的 20 页表格里各有几十处。
    接不上的格子被「值不合规」闸门拒掉 —— 表格网格事实从 389 掉到 251，
    而且**不报错**，只是少了一批数字（并让双路互证误判成
    "确定性路径没覆盖"）。
    """
    got, changes = repair_line_broken_numbers(src)
    assert got.replace(" ", "") == want.replace(" ", "")
    assert changes


@pytest.mark.parametrize("src", [
    "1,000\n2,000",
    "Note 5.\n2024",          # 句末的点号不能当小数点用 → 不能接成 5.2024
    "见第 5.\n6 节",
])
def test_repair_never_joins_unrelated_numbers(src: str) -> None:
    got, changes = repair_line_broken_numbers(src)
    assert got == src
    assert changes == []


def test_clean_label_keeps_latin_spacing_but_joins_cjk() -> None:
    """★ 单元格文本归一必须分中英，不能一刀切删空格。

    实测踩过：早期 ``replace(" ", "")`` 把
    ``JinkoSolar (U.S.) Inc.`` 变成 ``JinkoSolar(U.S.)Inc.``，
    而 VLM 读出的是带空格的写法 —— 同一家公司在事实库里成了**两个主体**：
    虚增主体数、拆散同比分组、还让双路互证少算了 70 个匹配。
    """
    from pipelines.r1_extract.table_facts import _clean_label

    assert _clean_label("JinkoSolar (U.S.)\nInc.") == "JinkoSolar (U.S.) Inc."
    assert _clean_label("唐人神集团股\n份有限公司") == "唐人神集团股份有限公司"
    assert _clean_label("Jinko ESS Japan\nCo., Ltd.") == "Jinko ESS Japan Co., Ltd."
    assert _clean_label("预计担保额度\n（万元）") == "预计担保额度（万元）"


@pytest.mark.parametrize("src,want", [
    ("15,234,63\n8.42", "15,234,638.42"),
    ("-5,187,92\n7.75", "-5,187,927.75"),
    ("1752,50\n0,000\n美元", "1752,500,000\n美元"),
])
def test_repair_joins_split_numbers(src: str, want: str) -> None:
    got, changes = repair_line_broken_numbers(src)
    assert got == want
    assert changes, "应记录改动（数据被改过就必须留痕）"


@pytest.mark.parametrize("src,want", [
    ("营业收入：717,247.24 万\n元；净利润：38,814.56\n万元。",
     "营业收入：717,247.24 万元；净利润：38,814.56\n万元。"),
    ("资产总额：312,460.60万\n元；", "资产总额：312,460.60万元；"),
])
def test_repair_joins_split_units(src: str, want: str) -> None:
    """★ 单位被折行切断（``万`` / ``元`` 分处两行）。

    这一条不解锁的话，实测一份材料 28 页的财务数据**一条事实都抽不出来**，
    只能靠 VLM 逐页读图。
    """
    got, changes = repair_line_broken_numbers(src)
    assert got == want
    assert changes


@pytest.mark.parametrize("src", [
    "1,000\n2,000",      # 两行各是一个完整数字 —— 不能拼
    "项目\n金额",
    "截至\n2024",
    "元\n1,000",          # 「元」在前，不是残缺单位
])
def test_repair_leaves_safe_cases_untouched(src: str) -> None:
    """**宁可漏接，不可接错**：拿不准就原样保留。"""
    got, changes = repair_line_broken_numbers(src)
    assert got == src
    assert changes == []


# ==========================================================================
# 表头与列映射
# ==========================================================================


def test_build_header_single_row() -> None:
    grid = [
        ["被担保人名称", "预计担保额度（万元）"],
        ["A公司", "7,000"],
    ]
    header, data_start = build_header(grid, ALIAS)
    assert data_start == 1
    assert header == ["被担保人名称", "预计担保额度（万元）"]


def test_build_header_handles_data_that_contains_metric_names() -> None:
    """★ 数据格里出现指标名时，**不能**把它当表头。

    实测：一格财务数据写成「资产总额：312,460.60万元；负债总额：…」，
    早期实现把这一行当成表头，真实数据行被并进表头后整行丢失
    （7 行的表被算成"4 行表头"）。
    """
    grid = [
        ["序号", "公司名称", "财务数据"],
        ["1", "A公司", "资产总额：312,460.60万元；负债总额：297,032.06万元"],
        ["2", "B公司", "资产总额：486,619.82万元；负债总额：472,620.40万元"],
    ]
    header, data_start = build_header(grid, ALIAS)
    assert data_start == 1, f"数据行被误判成表头（data_start={data_start}）"
    assert map_columns_to_metrics(header, ALIAS, UNITS) == {}


def test_build_header_finds_metric_row_below_grouping_rows() -> None:
    """指标行在第 3 行（上面还有标题行、分组行、数据声明行）。"""
    grid = [
        ["被担保公司名称", "成立日期", "注册资本（万元）"],
        ["2025 年 6 月数据（单位：元，未经审计）", "", ""],
        ["资产总额", "负债总额", "营业收入"],
        ["A公司", "2020/8/24", "15,234,638.42"],
    ]
    header, data_start = build_header(grid, ALIAS)
    assert data_start == 3
    cols = map_columns_to_metrics(header, ALIAS, UNITS)
    assert set(cols.values()) == {"total_assets", "total_liabilities", "revenue"}


# ==========================================================================
# 行主体
# ==========================================================================


def test_pick_subject_column_prefers_header_hint() -> None:
    grid = [
        ["担保人名称", "被担保人名称", "金额"],
        ["母公司", "A公司", "100"],
    ]
    assert pick_subject_column(grid, 1, metric_cols={2}) == 1


def test_pick_subject_column_falls_back_to_diversity() -> None:
    """没有表头关键词时，用**取值多样性**挑主体。

    担保人列每行都一样，被担保人列每行都不同 —— 分散度天然把主体挑出来。
    """
    grid = [
        ["col1", "col2", "金额"],
        ["母公司", "A公司", "100"],
        ["母公司", "B公司", "200"],
        ["母公司", "C公司", "300"],
    ]
    assert pick_subject_column(grid, 1, metric_cols={2}) == 1


def test_subject_of_row_uses_given_column() -> None:
    row = ["母公司", "A公司", "100"]
    assert subject_of_row(row, subject_col=1) == "A公司"
    # 指定列不是文字 → 返回空，不猜
    assert subject_of_row(row, subject_col=2) == ""


# ==========================================================================
# 单位
# ==========================================================================


def test_unit_in_label() -> None:
    assert unit_in_label("预计担保额度（万元）") == "万元"
    assert unit_in_label("注册资本(元)") == "元"
    assert unit_in_label("持股比例 %") == "%"
    assert unit_in_label("公司名称") == ""


def test_find_unit_declaration() -> None:
    assert find_unit_declaration("2025 年 6 月数据（单位：元，未经审计）") == "元"
    assert find_unit_declaration("单位：万元") == "万元"
    assert find_unit_declaration("没有单位声明") == ""


# ==========================================================================
# 抽取主流程
# ==========================================================================


GRID = [
    ["担保人名称", "被担保人名称", "公司持股比例（直接+间接）", "预计担保额度（万元）"],
    ["唐人神集团股份有限公司", "海南唐人神生物科技有限公司", "100%", "7,000"],
    ["唐人神集团股份有限公司", "安徽湘大骆驼饲料有限公司", "52%", "3,000"],
]


def test_extract_table_facts_happy_path() -> None:
    facts, stats = extract_table_facts(
        GRID, alias_map=ALIAS, metric_units=UNITS, page_no=4)
    got = {(f["entity"], f["metric"]): (f["value"], f["unit"]) for f in facts}
    assert got[("海南唐人神生物科技有限公司", "guarantee_amount")] == (
        __import__("decimal").Decimal("70000000"), "元")
    assert got[("海南唐人神生物科技有限公司", "shareholding_ratio")] == (
        __import__("decimal").Decimal("100"), "%")
    assert stats["skipped_no_unit"] == 0
    # 主体必须是**被担保人**，不是担保人
    assert all(f["entity"] != "唐人神集团股份有限公司" for f in facts)


def test_extract_table_facts_requires_unit() -> None:
    """★ 找不到单位就不入库 —— 万元与元差 10⁴，猜错比漏抽严重得多。"""
    grid = [
        ["被担保人名称", "保证金"],
        ["A公司", "7,000"],
    ]
    facts, stats = extract_table_facts(
        grid, alias_map={"保证金": "guarantee_amount"}, metric_units=UNITS)
    assert facts == []
    assert stats["skipped_no_unit"] == 1


def test_extract_table_facts_rejects_wrong_unit_family() -> None:
    """量纲族不符直接拒收（比率列被当成金额列）。"""
    grid = [
        ["被担保人名称", "净资产比例"],
        ["A公司", "0.51%"],
    ]
    facts, stats = extract_table_facts(
        grid, alias_map={"净资产": "equity_total"}, metric_units=UNITS)
    assert facts == []
    assert stats["skipped_bad_value"] == 1 or stats["metric_columns"] == 0


def test_extract_table_facts_repairs_broken_cells() -> None:
    """单元格里被折行切断的数字，抽取时先接回去。"""
    grid = [
        ["被担保人名称", "资产总额（元）"],
        ["A公司", "15,234,63\n8.42"],
    ]
    facts, stats = extract_table_facts(
        grid, alias_map=ALIAS, metric_units=UNITS)
    assert stats["repaired_cells"] == 1
    assert str(facts[0]["value"]) == "15234638.42"


def test_extract_table_facts_continuation_page_uses_carried_map() -> None:
    """★ 续表：本页没有表头，沿用上一页的列映射。

    中文公告的长表跨页时只在首页印表头。实测一份材料 28 页的表里
    只有第 1 页能抽出事实，其余 27 页因为"没有表头"颗粒无收。
    """
    cont = [
        ["海南唐人神生物科技有限公司", "100%", "7,000"],
        ["安徽湘大骆驼饲料有限公司", "52%", "3,000"],
    ]
    column_map = {1: "shareholding_ratio", 2: "guarantee_amount"}
    facts, stats = extract_table_facts(
        cont, alias_map=ALIAS, metric_units=UNITS,
        unit_hint="万元", column_map=column_map)
    assert len(facts) == 4, stats
    assert {f["entity"] for f in facts} == {"海南唐人神生物科技有限公司",
                                            "安徽湘大骆驼饲料有限公司"}


# ==========================================================================
# 主体归属纠正
# ==========================================================================


class _FakeFact:
    def __init__(self, doc_id: str, entity: str, metric: str, raw: str) -> None:
        self.fact_id = f"f_{entity}_{raw}"
        self.doc_id = doc_id
        self.entity = entity
        self.metric = metric
        self.raw_text = raw


def _records() -> list[dict]:
    return [{
        "doc_id": "doc_a", "page": 7,
        "grid": [
            ["序号", "公司名称", "财务数据"],
            ["1", "A子公司", "资产总额：312,460.60万元；营业收入：419,815.97万元。"],
            ["2", "B子公司", "资产总额：486,619.82万元；营业收入：378,664.28万元。"],
        ],
    }]


def test_reattribute_moves_fact_to_row_subject() -> None:
    """★ 表格行里的值不能挂在披露主体名下。

    实测：一份材料里 100+ 条子公司财务数据被记成母公司（披露主体）的，
    而且不报错，只会让结论静默失真。
    """
    index = cell_lookup(_records(), ALIAS, UNITS)
    assert index, "应能建出单元格索引"

    facts = [
        _FakeFact("doc_a", "118034", "revenue", "419,815.97万元"),
        _FakeFact("doc_a", "118034", "revenue", "378,664.28万元"),
    ]
    changed, detail = reattribute(facts, index, lambda f: 7)
    assert changed == 2
    assert facts[0].entity == "A子公司"
    assert facts[1].entity == "B子公司"
    assert detail[0]["row"] == 1 and detail[0]["col"] == 2


def test_reattribute_leaves_unknown_alone() -> None:
    """定位不到就**保持原样** —— 宁可不动，也不乱动。"""
    index = cell_lookup(_records(), ALIAS, UNITS)
    facts = [_FakeFact("doc_a", "118034", "revenue", "999,999.99万元")]
    changed, _ = reattribute(facts, index, lambda f: 7)
    assert changed == 0
    assert facts[0].entity == "118034"

    # 页面上没有表格索引时同样不动
    changed2, _ = reattribute(
        [_FakeFact("doc_a", "118034", "revenue", "419,815.97万元")], index,
        lambda f: 99)
    assert changed2 == 0


# ==========================================================================
# 扫描件的几何佐证（OCR 行框）
# ==========================================================================


def test_ocr_lines_to_words_converts_pixels_to_points() -> None:
    """★ OCR 框是**像素**，单元格 bbox 是**页面点** —— 必须换算。

    实测背景：扫描件没有文本层，``page.get_text("words")`` 为空，
    于是单元格裁决拿不到独立裁判，只能失败关闭。
    用 OCR 行框顶上就必须把 ``px * 72 / dpi`` 换算回去 ——
    这与 ``cell_pixel_box`` 是互逆的一步，搞反了会得到
    "看起来合理但完全对不上"的几何，而且不报错。
    """
    from domain.tools.ocr import ocr_lines_to_words

    lines = [{"text": "15,234,638.42", "confidence": 0.9,
              "bbox": {"x0": 1200.0, "y0": 2400.0, "x1": 2000.0, "y1": 2500.0}}]
    words = ocr_lines_to_words(lines, dpi=300)
    assert len(words) == 1
    x0, y0, x1, y1, text = words[0]
    assert text == "15,234,638.42"
    # 300dpi → 1pt = 300/72 px，因此 1200px = 288pt
    assert abs(x0 - 288.0) < 1e-6 and abs(y0 - 576.0) < 1e-6
    assert abs(x1 - 480.0) < 1e-6 and abs(y1 - 600.0) < 1e-6

    # 缺字段/坏数据要跳过，不能抛
    assert ocr_lines_to_words([{"text": ""}, {"bbox": {}}], dpi=300) == []
    assert ocr_lines_to_words([], dpi=300) == []
    assert ocr_lines_to_words(lines, dpi=0) == []


def test_ocr_geometry_corroborates_cell_on_scanned_page() -> None:
    """★ 扫描件：用 OCR 行框做几何佐证，挡住"模型读错格子"。

    这是"无文本层的扫描表格无法几何佐证"这条限制的收口测试 ——
    没有它，扫描件上的模型读数只能失败关闭（只标记不覆盖）。
    """
    from agent.evidence.models import BBox
    from domain.tools.ocr import ocr_lines_to_words
    from domain.tools.tables import cell_geometry_text, corroborate_by_geometry

    # 300dpi 下，某单元格在页面上的位置换算成像素后是 (1200,2400)-(2000,2500)
    lines = [{"text": "501,000.00", "confidence": 0.92,
              "bbox": {"x0": 1200.0, "y0": 2400.0, "x1": 2000.0, "y1": 2500.0}}]
    words = ocr_lines_to_words(lines, dpi=300)

    cell_bbox = BBox(x0=288.0, y0=576.0, x1=480.0, y1=600.0)
    geom = cell_geometry_text(words, cell_bbox)
    assert "501,000.00" in geom

    # 模型读到的与格内一致 → 佐证成立
    assert corroborate_by_geometry(geom, "179,000.00", "501,000.00") == "corroborated"
    # 模型读数不在格内、程序读数在 → 拒绝覆盖（就是当年那次事故的形态）
    assert corroborate_by_geometry(geom, "501,000.00", "179,000.00") == "contradicted"


# ==========================================================================
# 双路互证加权
# ==========================================================================


def test_cross_validation_boosts_confirmed_facts() -> None:
    """★ 两条独立路径给出同一个数 → 置信度提升，且明细可复算。

    置信度模型里原有的"多路独立同值"比的是**文本层与 OCR**，
    而这两路在多数页面上同源。真正独立的是**确定性路径**（表格网格/规则引擎）
    与**模型路径**（VLM 看图）—— 用完全不同的机制得到同一个数。
    """
    from agent.evidence.models import SourceLayer
    from agent.evidence.store import build_confidence

    class _L:
        def __init__(self, facts): self._f = facts
        def all(self): return self._f

    class _C:
        def __init__(self, facts):
            self.ledger = _L(facts)
            self.tracer = None      # 不写 trace，单测只关心置信度变化

    class _F:
        def __init__(self, entity, metric, value, extractor, conf):
            self.entity, self.metric, self.value = entity, metric, value
            self.extractor = extractor
            self.confidence_breakdown = build_confidence(
                source_layer=(SourceLayer.L1_TEXT_LAYER
                              if extractor.startswith(("table_grid", "rule_engine"))
                              else SourceLayer.L3_VLM)
            )
            self.confidence = self.confidence_breakdown.score
            self.needs_review = False
            self.review_reason = None

    facts = [
        _F("甲公司", "revenue", "100", "table_grid@1.0.0", 0),
        _F("甲公司", "revenue", "100", "vlm_scan@1.0.0", 0),      # 双路一致
        _F("甲公司", "revenue", "200", "vlm_scan@1.0.0", 0),      # 只有模型读到
    ]
    before = [f.confidence for f in facts]
    from pipelines.r1_extract.build_facts import _apply_cross_validation

    stats: dict = {}
    boosted = _apply_cross_validation(_C(facts), stats)  # type: ignore[arg-type]

    assert boosted == 2, "一致的两条都应加分（确定性与模型各一条）"
    assert facts[0].confidence > before[0]
    assert facts[1].confidence > before[1]
    assert facts[2].confidence == before[2], "只被模型读到的不该加分"
    assert stats["cross_validated_keys"] == 1
    # 明细里必须能看见这一项（可复算，不做黑盒加分）
    reasons = [a.reason for a in facts[1].confidence_breakdown.adjustments]
    assert any("双路互证" in r for r in reasons)


def test_cross_validation_is_idempotent() -> None:
    """重复调用不能反复加分（避免"跑两次分更高"这种荒谬结果）。"""
    from agent.evidence.models import SourceLayer
    from agent.evidence.store import build_confidence
    from pipelines.r1_extract.build_facts import _apply_cross_validation

    class _L:
        def __init__(self, facts): self._f = facts
        def all(self): return self._f

    class _C:
        def __init__(self, facts):
            self.ledger = _L(facts)
            self.tracer = None

    class _F:
        def __init__(self):
            self.entity, self.metric, self.value = "甲", "revenue", "100"
            self.extractor = "table_grid@1.0.0"
            self.confidence_breakdown = build_confidence(
                source_layer=SourceLayer.L1_TEXT_LAYER)
            self.confidence = self.confidence_breakdown.score
            self.needs_review = False
            self.review_reason = None

    f = _F()
    peer = _F()
    peer.extractor = "vlm_scan@1.0.0"
    ctx = _C([f, peer])
    _apply_cross_validation(ctx, {})  # type: ignore[arg-type]
    once = f.confidence
    _apply_cross_validation(ctx, {})  # type: ignore[arg-type]
    assert f.confidence == once


def test_reattribute_handles_cross_page_continuation_row() -> None:
    """★ 跨页续行：只有财务列、没有公司名的行，要继承上一行的主体。

    中文表格里宽列（经营范围、财务数据）会把一行撑得很高，折行后财务列落到
    下一页，于是下一页出现"只有财务列、没有公司名"的行。实测某页 r2 就是这种：
    c8/c9 有「营业收入：961,617.23万元」，c0–c7 全空 —— 它的公司名在上一页。
    不继承的话，这些值会挂到**文档主体**名下，被冲突守卫标记，
    最终让 R4 无数据可用、退回模板默认值。
    """
    records = [
        {"doc_id": "doc_a", "page": 1, "grid": [
            ["序号", "公司名称", "营业收入"],
            ["1", "A子公司", "961,617.23万元"],
        ]},
        # 第 2 页：只有数据列，标签区全空
        {"doc_id": "doc_a", "page": 2, "grid": [
            ["", "", ""],
            ["", "", "434,749.30万元"],
        ]},
    ]
    index = cell_lookup(records, {"营业收入": "revenue"}, {"revenue": "元"})
    facts = [_FakeFact("doc_a", "118034", "revenue", "434,749.30万元")]
    changed, _ = reattribute(facts, index, lambda f: 2)
    assert changed == 1
    assert facts[0].entity == "A子公司", "续行没有继承上一页的主体"


def test_reattribute_digit_fallback_requires_unique_subject() -> None:
    """★ 数字兜底匹配必须**唯一指向一个主体**，否则宁可不改。

    事实的 raw_text 是"数值+单位"，单元格里可能写法不同（空格、相邻格拆分）。
    用纯数字串去找更宽容 —— 但两家子公司营收都是 0 时，数字一样、
    主体不同，这时**改错比不改严重得多**。
    """
    records = [{
        "doc_id": "doc_a", "page": 1,
        "grid": [
            ["序号", "公司名称", "营业收入"],
            ["1", "A子公司", "1230万元"],
            ["2", "B子公司", "1230万元"],
        ],
    }]
    index = cell_lookup(records, {"营业收入": "revenue"}, {"revenue": "元"})

    # 数字命中两个不同主体 → 不改
    # （原文 "12.30万元" 的**文本**匹配不上 "1230万元"，因此确实走的是数字兜底）
    ambiguous = [_FakeFact("doc_a", "118034", "revenue", "12.30万元")]
    changed, _ = reattribute(ambiguous, index, lambda f: 1)
    assert changed == 0, "数字指向两个主体时不该改"
    assert ambiguous[0].entity == "118034"

    # 数字只命中一个主体 → 改，且标明用的是数字兜底
    records2 = [{
        "doc_id": "doc_a", "page": 1,
        "grid": [
            ["序号", "公司名称", "营业收入"],
            ["1", "A子公司", "4560万元"],
            ["2", "B子公司", "7890万元"],
        ],
    }]
    index2 = cell_lookup(records2, {"营业收入": "revenue"}, {"revenue": "元"})
    unique = [_FakeFact("doc_a", "118034", "revenue", "45.60万元")]
    changed2, detail = reattribute(unique, index2, lambda f: 1)
    assert changed2 == 1
    assert unique[0].entity == "A子公司"
    assert detail[0]["matched_by"] == "digits_unique"


def test_reattribute_rejects_header_words_as_subject() -> None:
    """表头词（如「公司名称」）绝不能成为主体名。"""
    records = [{
        "doc_id": "doc_a", "page": 1,
        "grid": [
            ["序号", "公司名称", "成立日期", "财务数据"],
            ["", "", "", "营业收入：153,361.18万元。"],
        ],
    }]
    index = cell_lookup(records, ALIAS, UNITS)
    facts = [_FakeFact("doc_a", "118034", "revenue", "153,361.18万元")]
    changed, _ = reattribute(facts, index, lambda f: 1)
    assert facts[0].entity != "公司名称"


def test_entity_reattribution_runs_after_vlm_ingest() -> None:
    """★ 归属纠正必须在**两条路径都入库之后**执行。

    实测踩坑：这一步原本写在 `_ingest_table_grid` 里（即 VLM 补录之前），
    于是 VLM 事实根本轮不到被纠正 —— 而它们的 entity 来自模型的 entity_hint，
    在密集的子公司表里同样会把子公司的数挂到母公司名下
    （实测：父公司名下 4 条营业收入全来自 VLM，其实是 4 家子公司的）。
    修好后父公司名下这类事实从 8 条降到 0 条。
    """
    src = (ROOT_SRC / "pipelines" / "r1_extract" / "build_facts.py").read_text(
        encoding="utf-8")
    i_vlm = src.index('stats["vlm_accepted"] = _ingest_vlm_scans')
    i_reattr = src.index("_reattribute_entities(ctx, stats)")
    assert i_reattr > i_vlm, (
        "主体归属纠正跑在 VLM 补录之前 —— VLM 事实不会被纠正（实测踩过）"
    )


def _fake_factset(rows: list[tuple[str, str]]) -> "object":
    """用 (主体, 指标) 列表造一个最小事实库（只给 R4 主体选择用得到的字段）。"""
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
            profit_basis=ProfitBasis.UNKNOWN,
            source_layer=SourceLayer.L1_TEXT_LAYER,
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


def test_subject_facts_note_flags_material_without_subject_data() -> None:
    """★ 主体自身没有事实时要**明说估不了**，而不是换一家来估。

    实测：归属纠正修好后，某份材料的主体名下一条事实都不剩
    （那份公告披露的是**子公司**的财务数据）。
    此时"选事实最多的主体"会退而选中事实库里另一个代码（只有 1 条），
    等于给一家毫不相干的公司估值，而且不报错。
    """
    from pipelines.r4_valuation.assumptions import (
        MIN_SUBJECT_FACTS,
        subject_facts_note,
    )

    fs = _fake_factset([("118034", "revenue")] + [("某子公司", "revenue")] * 20)
    note = subject_facts_note(fs, "118034")
    assert note and "自身的财务数据" in note

    rich = _fake_factset([("118034", "revenue")] * MIN_SUBJECT_FACTS)
    assert subject_facts_note(rich, "118034") == ""


# ==========================================================================
# 主体名归一（只差空白的同一家公司必须合并）
# ==========================================================================


def test_entity_unification_merges_whitespace_variants() -> None:
    """★ 只差空白的两个主体名必须合并成一个。

    实测：表格路径产出 ``JinkoSolar(U.S.)Inc.``（早期把空格全删），
    VLM 读出 ``JinkoSolar (U.S.) Inc.``（保留排版空格）——
    同一家公司在事实库里成了两个主体。后果有三个，都不报错：

    1. 虚增主体数（干扰 R4 选估值主体）
    2. 拆散 R5 同比分组（两个"不同主体"各有一期，配不上对）
    3. 双路互证少算匹配（模型读数被误判成"确定性路径没覆盖"）
    """
    from pipelines.r1_extract.build_facts import _unify_entities

    class _L:
        def __init__(self, facts): self._f = facts
        def all(self): return self._f
        def _check_internal_conflict(self, f): pass

    class _C:
        def __init__(self, facts): self.ledger = _L(facts)

    class _F:
        def __init__(self, entity, extractor):
            self.entity, self.extractor = entity, extractor

    facts = [
        _F("JinkoSolar(U.S.)Inc.", "rule_engine@1.0.0"),
        _F("JinkoSolar (U.S.) Inc.", "vlm_scan@1.0.0"),
        _F("Jinko ESS Japan Co.,Ltd.", "table_grid@1.0.0"),
        _F("Jinko ESS Japan Co., Ltd.", "vlm_scan@1.0.0"),
        _F("唐人神集团股份有限公司", "rule_engine@1.0.0"),      # 单一写法，不动
    ]
    stats: dict = {}
    changed = _unify_entities(_C(facts), stats)  # type: ignore[arg-type]

    assert changed == 2
    # 规范写法取 VLM 那种（来自渲染图像，最接近真实排版）
    assert facts[0].entity == "JinkoSolar (U.S.) Inc."
    assert facts[1].entity == "JinkoSolar (U.S.) Inc."
    assert facts[2].entity == "Jinko ESS Japan Co., Ltd."
    assert facts[4].entity == "唐人神集团股份有限公司"
    assert stats["entity_unified"] == 2


def test_entity_unification_does_not_merge_midword_splits() -> None:
    """⚠️ 已知残留：**折行把单词切断**的情况合并不了。

    ``INTERNATI`` + ``ONA`` → ``INTERNATI ONA``；去掉空格是
    ``INTERNATIONA``，与 ``INTERNATIONAL`` 并不相等。靠"删空格"合并不了，
    硬做需要词形判断。这里**如实锁住现状**，避免以后误以为已经处理了。
    """
    from pipelines.r1_extract.build_facts import _unify_entities

    class _L:
        def __init__(self, facts): self._f = facts
        def all(self): return self._f
        def _check_internal_conflict(self, f): pass

    class _C:
        def __init__(self, facts): self.ledger = _L(facts)

    class _F:
        def __init__(self, entity):
            self.entity, self.extractor = entity, "table_grid@1.0.0"

    facts = [_F("JINKO ENERGY STORAGE INTERNATI ONA"),
             _F("JINKO ENERGY STORAGE INTERNATIONAL")]
    assert _unify_entities(_C(facts), {}) == 0  # type: ignore[arg-type]


def test_entity_unification_keeps_genuinely_different_entities() -> None:
    """不能把**真的不同**的主体合并掉。"""
    from pipelines.r1_extract.build_facts import _unify_entities

    class _L:
        def __init__(self, facts): self._f = facts
        def all(self): return self._f
        def _check_internal_conflict(self, f): pass

    class _C:
        def __init__(self, facts): self.ledger = _L(facts)

    class _F:
        def __init__(self, entity):
            self.entity, self.extractor = entity, "rule_engine@1.0.0"

    facts = [_F("晶科能源（上饶）有限公司"), _F("晶科能源（鄱阳）有限公司")]
    assert _unify_entities(_C(facts), {}) == 0  # type: ignore[arg-type]
