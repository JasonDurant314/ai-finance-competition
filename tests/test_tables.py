"""表格解析与单元格级 VLM 裁决测试。

覆盖一条真实踩坑链：
    ① 全页线段的**凸包**被当成唯一表格 → 多个表格被合并成 9×25 的空壳网格
    ② 改用连通分量，但判据要求"端点严格相接" → 分段绘制的框线判不出来，
       整体退化成较弱的坐标聚类，**比修复前更差**
    ③ 单元格文本用 ``" ".join()`` 拼接 → 换行被插空格，别名再也匹配不上
    ④ 裁决策略写在 configs/tools.yaml，但没摊到 ctx.configs 顶层
       → 裁决**静默不触发**，零调用，零报错
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.evidence.models import BBox
from domain.tools.tables import (
    Table,
    TableCell,
    _cluster_positions,
    _find_table_regions,
    _text_in_box,
    check_table_arithmetic,
)

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "agent" / "sample"


# ==========================================================================
# 表格区域识别
# ==========================================================================


def _grid_lines(xs: list[float], ys: list[float]) -> tuple[list, list]:
    """构造一个规则网格的水平/垂直线段（用于单测，不依赖真实 PDF）。"""
    hs = [(y, min(xs), max(xs)) for y in ys]
    vs = [(x, min(ys), max(ys)) for x in xs]
    return hs, vs


def test_find_regions_single_table() -> None:
    hs, vs = _grid_lines([0, 100, 200], [0, 50, 100])
    regions = _find_table_regions(hs, vs, min_rows=2, min_cols=2)
    assert len(regions) == 1
    r = regions[0]
    assert (r.x0, r.y0, r.x1, r.y1) == (0, 0, 200, 100)


def test_find_regions_two_separate_tables() -> None:
    """★ 两个互不相连的表格必须识别为**两个**区域。

    这是整个修复的核心：早期实现取全页线段凸包，会把它们合并成一个网格，
    实测产出 9 行 × 25 列、其中 15 列全空的空壳。
    """
    hs1, vs1 = _grid_lines([0, 100, 200], [0, 50, 100])
    hs2, vs2 = _grid_lines([0, 100, 200], [300, 350, 400])
    regions = _find_table_regions(hs1 + hs2, vs1 + vs2, min_rows=2, min_cols=2)
    assert len(regions) == 2, f"应识别出 2 个表格，实际 {len(regions)}"
    tops = sorted(r.y0 for r in regions)
    assert tops == [0, 300]


def test_find_regions_handles_segmented_borders() -> None:
    """★ 分段绘制的框线也要能连起来。

    实测教训：很多 PDF 的表格框线是**逐段绘制**的（每条边一段，
    端点并不精确相接）。第一版连通判据要求"端点严格相接"，
    于是这类表格全判不出来，整体退化成坐标聚类，比修复前更差。
    """
    # 同一条水平线被切成两段，中间留 3pt 缝隙
    hs = [
        (0.0, 0.0, 98.0), (0.0, 101.0, 200.0),      # 上边（两段）
        (50.0, 0.0, 200.0),                          # 中间
        (100.0, 0.0, 98.0), (100.0, 101.0, 200.0),  # 下边（两段）
    ]
    vs = [(0.0, 0.0, 100.0), (100.0, 0.0, 100.0), (200.0, 0.0, 100.0)]
    regions = _find_table_regions(hs, vs, min_rows=2, min_cols=2)
    assert len(regions) == 1, "分段框线应被识别为同一个表格"


def test_find_regions_rejects_line_only_region() -> None:
    """只有横线没有竖线的区域不是表格。"""
    hs = [(y, 0.0, 200.0) for y in (0, 50, 100)]
    regions = _find_table_regions(hs, [], min_rows=2, min_cols=2)
    assert regions == []


def test_cluster_positions_merges_close_lines() -> None:
    assert _cluster_positions([10.0, 11.0, 40.0], tol=2.0) == [10.5, 40.0]


# ==========================================================================
# 单元格文本拼接
# ==========================================================================


def test_text_in_box_joins_cjk_line_wrap() -> None:
    """★ 中文换行是排版折行，必须**直接相连**。

    早期用 ``" ".join()`` 会在中间插空格，
    「唐人神集团股份有限公司」变成「唐人神集团股 份有限公司」，
    别名匹配立刻失效。
    """
    spans = [
        ("唐人神集团股", BBox(x0=0, y0=0, x1=90, y1=12)),
        ("份有限公司", BBox(x0=0, y0=13, x1=80, y1=25)),
    ]
    box = BBox(x0=-1, y0=-1, x1=200, y1=30)
    assert _text_in_box(spans, box) == "唐人神集团股份有限公司"


def test_text_in_box_keeps_newline_before_numbers() -> None:
    """涉及数字时保留换行 —— 否则相邻数字会被粘成一个。"""
    spans = [
        ("截至", BBox(x0=0, y0=0, x1=30, y1=12)),
        ("2024", BBox(x0=0, y0=13, x1=50, y1=25)),
    ]
    box = BBox(x0=-1, y0=-1, x1=200, y1=30)
    out = _text_in_box(spans, box)
    assert "\n" in out, f"数字前应保留换行，实际 {out!r}"


def test_text_in_box_joins_same_line_spans() -> None:
    spans = [
        ("1,200", BBox(x0=0, y0=0, x1=40, y1=12)),
        (".50", BBox(x0=40, y0=0, x1=60, y1=12)),
    ]
    box = BBox(x0=-1, y0=-1, x1=200, y1=30)
    assert _text_in_box(spans, box) == "1,200.50"


def test_text_in_box_empty_when_no_spans_inside() -> None:
    spans = [("外部", BBox(x0=500, y0=500, x1=600, y1=520))]
    assert _text_in_box(spans, BBox(x0=0, y0=0, x1=100, y1=100)) == ""


# ==========================================================================
# 可疑单元格挑选
# ==========================================================================


def _table(rows: list[list[str]]) -> Table:
    cells = [
        TableCell(row=i, col=j, bbox=BBox(x0=j * 10, y0=i * 10, x1=j * 10 + 9, y1=i * 10 + 9), text=t)
        for i, r in enumerate(rows)
        for j, t in enumerate(r)
    ]
    return Table(page=1, bbox=BBox(x0=0, y0=0, x1=100, y1=100),
                 n_rows=len(rows), n_cols=max(len(r) for r in rows),
                 cells=cells, method="test")


def test_suspicious_cells_prefers_arith_failures() -> None:
    """算术不平涉及的单元格必须优先被挑出来。

    这是触发策略的核心：算术校验能把出错位置**精确到行列**，
    因此只需复核那几个格子，而不是整表逐格调用模型。
    """
    t = _table([
        ["项目", "金额"],
        ["A", "100"],
        ["B", "200"],
        ["合计", "999"],      # 错：应为 300
    ])
    chk = check_table_arithmetic(t)
    assert not chk.ok, "该表应被判定为算术不平"

    suspects = t.suspicious_cells(limit=4)
    assert suspects, "应挑出可疑单元格"
    picked = {(c.row, c.col) for c in suspects}
    assert (3, 1) in picked, f"合计格的错误值应被挑出，实际 {picked}"


def test_arith_vertical_total_row() -> None:
    """★ 中文财报最常见的形态：末行「合计」是**各列的纵向合计**。

    旧的实现要求整列每个格都能解析成数字，而表头「2024年」会被
    ``parse_decimal`` 解析成 2024、表头「项目」解析失败 —— 两头堵死，
    于是 ``checked_cols`` 恒为 0。这里同时锁住"表头行不计入分项"。
    """
    t = _table([
        ["项目", "2024年", "2023年"],
        ["营业收入", "100", "90"],
        ["营业成本", "200", "180"],
        ["合计", "300", "270"],
    ])
    chk = check_table_arithmetic(t)
    assert chk.ok, f"该表算术自洽，不应报错：{chk.failures}"
    assert chk.checked_cols == 2, f"两列都应被校验，实际 {chk.checked_cols}"


def test_arith_vertical_total_only_flags_broken_column() -> None:
    """纵向合计行里只有一列不平 → **只报那一列**，不得牵连正确的列。"""
    t = _table([
        ["项目", "2024年", "2023年"],
        ["营业收入", "100", "90"],
        ["营业成本", "200", "180"],
        ["合计", "300", "999"],          # 第二列错：应为 270
    ])
    chk = check_table_arithmetic(t)
    assert not chk.ok
    assert chk.checked_cols == 1, "对的那一列应算作已校验"
    assert len(chk.failures) == 1
    f = chk.failures[0]
    assert f["kind"] == "col_sum" and f["col"] == 2
    assert f["expected"] == "999" and f["computed"] == "270"


def test_arith_horizontal_total_row() -> None:
    """合计在**最右一格**的横向表。"""
    t = _table([
        ["项目", "一季度", "二季度", "三季度", "合计"],
        ["营业收入", "100", "200", "300", "600"],
        ["营业成本", "60", "120", "180", "999"],   # 错：应为 360
    ])
    chk = check_table_arithmetic(t)
    assert not chk.ok
    assert chk.checked_rows == 1
    got = {(f["kind"], f["row"]) for f in chk.failures}
    assert ("row_sum", 2) in got, f"应报第 2 行横向不平，实际 {got}"
    assert ("row_sum", 1) not in got, "第 1 行是平的，不应被报"


def test_arith_serial_number_column_does_not_poison_sum() -> None:
    """★ 「序号」列不能混进横向求和。

    ``1 | 收入 | 100 | 200 | 300`` 若把序号也加上就是 301，
    而正确做法是只取**紧邻合计格的连续数值段** ``100+200``。
    """
    t = _table([
        ["序号", "项目", "一季度", "二季度", "合计"],
        ["1", "营业收入", "100", "200", "300"],
        ["2", "营业成本", "60", "120", "180"],
    ])
    chk = check_table_arithmetic(t)
    assert chk.ok, f"序号列被误当分项：{chk.failures}"
    assert chk.checked_rows == 2


def test_arith_total_column_by_header() -> None:
    """合计列由**表头**识别，逐行校验。"""
    t = _table([
        ["项目", "一季度", "二季度", "合计"],
        ["营业收入", "100", "200", "300"],
        ["营业成本", "60", "120", "180"],
        ["净利润", "40", "80", "999"],             # 错：应为 120
    ])
    chk = check_table_arithmetic(t)
    assert not chk.ok
    assert chk.checked_rows == 2
    assert [f["row"] for f in chk.failures] == [3]


def test_arith_nested_subtotal_is_not_a_failure() -> None:
    """★ 分层结构（「其中：」）不判失败。

    实测结构::

        一、营业收入   1000
          其中：A       600
                B       400
        合计           1000

    ``合计`` 等于"其中"那一项，全部分项之和却是 2000。
    上一版会把它判成不平 —— 分层表在中文财报里极其常见，
    这种误报会让"算术不平"这个信号彻底失去可信度。
    """
    t = _table([
        ["项目", "金额"],
        ["一、营业收入", "1000"],
        ["其中：A", "600"],
        ["B", "400"],
        ["合计", "1000"],
    ])
    chk = check_table_arithmetic(t)
    assert chk.ok, f"分层结构被误判为不平：{chk.failures}"
    assert chk.nested == 1, "应记录一条 soft（放过但留痕）"
    assert chk.soft[0]["covered_from"] == 1


def test_arith_no_total_label_means_no_claims() -> None:
    """没有合计标签的表 → 不校验、也不报错（宁可不查，不可误报）。"""
    t = _table([
        ["项目", "2024年", "2023年", "增减"],
        ["营业收入", "1000", "900", "11.11"],
        ["净利润", "100", "90", "11.11"],
    ])
    chk = check_table_arithmetic(t)
    assert chk.ok
    assert chk.checked == 0, "没有合计线时不应声称校验过"


def test_arith_single_term_column_is_not_checked() -> None:
    """只有一个分项的「合计」说明不了什么 —— 不查，避免噪声。"""
    t = _table([["项目", "金额"], ["A", "100"], ["合计", "999"]])
    chk = check_table_arithmetic(t)
    assert chk.ok
    assert chk.checked == 0


def test_suspicious_cells_finds_line_broken_numbers() -> None:
    """数字被换行切断的单元格应被优先挑出。"""
    t = _table([["项目", "金额"], ["A", "583,96\n5.66"]])
    suspects = t.suspicious_cells(limit=3)
    assert any("583,96" in c.text for c in suspects)


def test_suspicious_cells_respects_limit() -> None:
    t = _table([[str(i), str(i * 10)] for i in range(20)])
    assert len(t.suspicious_cells(limit=5)) == 5


def test_suspicious_cells_no_duplicates() -> None:
    t = _table([["项目", "金额"], ["A", "100"], ["合计", "999"]])
    picked = [(c.row, c.col) for c in t.suspicious_cells(limit=10)]
    assert len(picked) == len(set(picked)), "同一单元格不应被重复挑出"


# ==========================================================================
# 覆盖闸门（安全关键）
# ==========================================================================


def test_cell_defaults_are_unadjudicated() -> None:
    c = TableCell(row=0, col=0, bbox=BBox(x0=0, y0=0, x1=1, y1=1), text="1")
    assert c.adjudicated is False
    assert c.verdict == ""
    assert c.original_text == ""
    assert c.vlm_text is None
    d = c.to_dict()
    for k in ("adjudicated", "verdict", "original_text", "vlm_text", "vlm_confidence"):
        assert k in d, f"to_dict 缺少裁决字段 {k}"


def test_unadjudicated_cell_keeps_original_text() -> None:
    """未被裁决的单元格，文本不得被改动。"""
    c = TableCell(row=0, col=0, bbox=BBox(x0=0, y0=0, x1=1, y1=1), text="原始值")
    assert c.text == "原始值"


# ==========================================================================
# 单元格裁决判定表（安全关键）
# ==========================================================================


def test_verdict_never_overwrites_without_gates() -> None:
    """★ 未过闸门的读数**一律不得改值**。

    「不一致就采用模型」是危险的 —— 那等于用另一个可能出错的源覆盖当前值。
    """
    from domain.tools.vlm import decide_cell_verdict

    assert decide_cell_verdict("100", "200", gates_ok=False) == "rejected"
    # 闸门全过 + 几何佐证 → 才允许改
    assert decide_cell_verdict(
        "100", "200", gates_ok=True, geometry="200"
    ) == "corrected"
    # quote 回读被拒时，即使闸门其他条件都满足也不能改
    assert decide_cell_verdict(
        "100", "200", gates_ok=True, geometry="200", reading_rejected=True
    ) == "rejected"


def test_verdict_requires_geometry_corroboration() -> None:
    """★ 模型自述通过 ≠ 读对了。必须有**几何佐证**。

    这是一次真实事故的回归测试：某次运行 184 个复核格有 80 个被"改值"，
    独立复核（用单元格 bbox 内的页面真实文字）后 **75 个是错的**。
    三道自述闸门（quote 回读 / 位置确认 / 置信度 0.95）**全部放行**，
    因为模型看错了图时，它的自述依然自洽：

    * quote 回读只校验"这段文字在整页某处存在"
    * 位置确认用的是同一张错图渲染的红框
    * 置信度自评恒为 0.95，没有区分度

    真实样例（对外担保公告，p2 r1c0）：程序读到 ``1``，模型报 ``2``；
    格子里实际印着的就是 ``1``。
    """
    from domain.tools.vlm import decide_cell_verdict

    # 几何文字里只有程序读数 → 模型读数无佐证 → 拒绝覆盖
    assert decide_cell_verdict("1", "2", gates_ok=True, geometry="1") == "rejected"
    assert decide_cell_verdict(
        "501,000.00", "179,000.00", gates_ok=True, geometry="501,000.00"
    ) == "rejected"
    # 没有文本层（扫描件）→ 无从复核 → 失败关闭
    assert decide_cell_verdict("100", "200", gates_ok=True, geometry="") == "rejected"
    # 几何文字支持模型读数、且不支持程序读数 → 采纳
    assert decide_cell_verdict(
        "179,000.00", "501,000.00", gates_ok=True, geometry="501,000.00"
    ) == "corrected"


def test_corroborate_by_geometry_states() -> None:
    from domain.tools.tables import corroborate_by_geometry

    # 模型读数在格内、程序读数不在 → 佐证成立
    assert corroborate_by_geometry("501,000.00", "179,000.00", "501,000.00") == "corroborated"
    # 程序读数在格内、模型读数不在 → 矛盾（事故情形）
    assert corroborate_by_geometry("501,000.00", "501,000.00", "179,000.00") == "contradicted"
    # 两者都在格内 / 都不在 → 无法判定
    assert corroborate_by_geometry("1 2", "1", "2") == "unknown"
    assert corroborate_by_geometry("300", "100", "200") == "unknown"
    # 没有文本层 → unknown（调用方据此失败关闭）
    assert corroborate_by_geometry("", "100", "200") == "unknown"
    # 模型没给出任何数字 → unknown
    assert corroborate_by_geometry("100", "100", "见附注") == "unknown"


def test_cell_geometry_text_selects_words_in_box() -> None:
    from domain.tools.tables import cell_geometry_text

    words = [
        (0.0, 0.0, 10.0, 5.0, "501,000.00"),
        (100.0, 0.0, 110.0, 5.0, "179,000.00"),
    ]
    box = BBox(x0=-1, y0=-1, x1=20, y1=10)
    assert cell_geometry_text(words, box) == "501,000.00"
    assert cell_geometry_text(None, box) == ""
    assert cell_geometry_text(words, BBox(x0=200, y0=200, x1=300, y1=300)) == ""


def test_verdict_agrees_on_identical_digits() -> None:
    from domain.tools.vlm import decide_cell_verdict

    assert decide_cell_verdict("1,200.50", "1200.50", gates_ok=False) == "agree"
    assert decide_cell_verdict("100万元", "100", gates_ok=False) == "agree"


def test_verdict_normalizes_line_broken_number() -> None:
    """★ 数字一位不差、但原读法**解析不出数值** → 必须归一化。

    实测踩坑：``'583,96\\n5.66'`` 与 ``'583,965.66'`` 的数字串完全相同，
    于是早期"只比数字"的判定给出 ``agree``，那一格就原样留了下来 ——
    下游 ``_to_decimal`` 解析失败，**值直接丢失**。
    T2 基线实测 55 张真实表格里有 422 个这样的碎格。
    """
    from domain.tools.vlm import decide_cell_verdict
    from domain.tools.tables import cell_number

    broken = "583,96\n5.66"
    fixed = "583,965.66"
    assert cell_number(broken) is None, "换行切断的数字本应解析不出"
    assert cell_number(fixed) is not None

    assert decide_cell_verdict(broken, fixed, gates_ok=True) == "normalized"
    # 未过闸门则不改，退回 agree（数字确实一致，不算冲突）
    assert decide_cell_verdict(broken, fixed, gates_ok=False) == "agree"
    # 两边都解析不出时没有收益，也不必改
    assert decide_cell_verdict("abc\ndef", "abcdef", gates_ok=True) == "agree"


def test_verdict_unreadable_only_when_both_empty() -> None:
    from domain.tools.vlm import decide_cell_verdict

    assert decide_cell_verdict("", "") == "unreadable"
    # 只有一边为空，不是 unreadable；空 → 有值 也要求几何佐证
    assert decide_cell_verdict("", "100", gates_ok=True, geometry="100") == "corrected"
    assert decide_cell_verdict("", "100", gates_ok=True, geometry="") == "rejected"
    assert decide_cell_verdict("100", "", gates_ok=True) == "rejected"


def test_adjudication_report_counts_normalized_separately() -> None:
    """格式归一与改值必须分开计数 —— 否则审计时分不清"改了什么"。"""
    from domain.tools.vlm import TableAdjudicationReport

    rep = TableAdjudicationReport()
    rep.normalized = 2
    assert rep.changed, "归一化也是变更，应被计入 changed"
    d = rep.to_dict()
    assert "normalized" in d


def test_cell_adjudication_carries_evidence_anchor() -> None:
    """★ 每条裁决都必须带证据锚点：位置 + 原文 + 像素。

    没有锚点，"模型改了这一格"就只是一句无法复核的自述。
    锚点让任何人能回到原始页面：bbox 定位、quote 对照、裁图直接看。
    """
    from domain.tools.vlm import CellAdjudication

    a = CellAdjudication(
        row=1, col=2, original="583,96\n5.66", vlm_text="583,965.66",
        verdict="normalized", confidence=0.92, quote="583,965.66",
        bbox=[463.6, 199.7, 510.7, 213.4], crop="cell_p2_r1c2.png",
    )
    d = a.to_dict()
    for key in ("bbox", "crop", "quote", "original", "vlm_text"):
        assert d[key], f"证据锚点缺少 {key}"
    assert d["bbox"] == [463.6, 199.7, 510.7, 213.4]


# ==========================================================================
# 审计报告里的表格一节
# ==========================================================================


def test_audit_report_splits_verified_from_unverifiable(tmp_path: Path) -> None:
    """★ 审计报告必须把「已验算」与「无从校验」分开列。

    报告里只写一个"算术自洽率"是最容易骗过自己的写法 ——
    没有合计线的表也会被算成"自洽"。实测 55 张真实表格里只有 2 张有合计线，
    这样的"自洽率 100%"完全是幻觉。
    """
    import json as _json

    from agent.trace.audit import build_audit_markdown

    run = tmp_path / "run-x"
    (run / "channels").mkdir(parents=True)
    (run / "manifest.json").write_text(
        _json.dumps({
            "run_id": "run-x", "pipeline": "r1_extract", "status": "success",
            "started_at": "2026-01-01T00:00:00", "contract_version": "1.0.0",
            "offline": False, "seed": 0, "models": {}, "outputs": {},
        }),
        encoding="utf-8",
    )
    (run / "channels" / "tables.json").write_text(
        _json.dumps([
            {"has_rules": True, "arithmetic": {"ok": True, "checked": 0}},
            {"has_rules": True, "arithmetic": {"ok": True, "checked": 2}},
            {"has_rules": False, "arithmetic": {"ok": False, "checked": 1,
                                                "failure_count": 1}},
        ]),
        encoding="utf-8",
    )
    (run / "channels" / "table_adjudications.json").write_text(
        _json.dumps([{
            "page": 3, "trigger": "no_total_line",
            "report": {"agree": 1, "normalized": 1, "corrected": 0, "rejected": 2,
                       "unreadable": 0, "errors": 0,
                       "adjudications": [{"row": 1, "col": 1, "original": "583,96\n5.66",
                                          "vlm_text": "583,965.66",
                                          "verdict": "normalized", "confidence": 0.9}]},
        }]),
        encoding="utf-8",
    )

    md = build_audit_markdown(run)
    assert "## 八、表格解析与单元格级裁决" in md
    assert "已验算合计线：**3** 条" in md, md[md.find("## 八"):][:400]
    assert "无从校验**：**1** 张" in md
    assert "发现不平 **1** 处" in md
    assert "格式归一" in md and "未采纳" in md
    assert "583,965.66" in md, "改动明细必须逐条可核对"
    # ★ 单元格里的换行必须被替换：否则 Markdown 表格会被截断成坏报告
    assert "583,96⏎5.66" in md, "改动明细里的换行没有转义，表格会被截断"
    assert "583,96\n5.66`" not in md, "原始换行泄漏进了 Markdown 表格"


# ==========================================================================
# ★ 裁图坐标（本项目最贵的一个坑）
# ==========================================================================


def test_crop_cell_matches_pdf_clip_render(tmp_path: Path) -> None:
    """★ 裁出来的图必须是**那个格子**的像素。

    这是全项目代价最大的一个 bug 的回归测试：

    PyMuPDF 的页面坐标**原点在左上、y 向下增大**（``page.rect`` 与
    ``get_text()`` 同源），而 PDF 用户空间的数学约定是原点在左下。
    早期实现按后者写了 ``y = (page_height - bbox.y) * scale``，
    于是**每一次裁图都沿页面中线上下镜像** ——

    * 第 1 行的格子裁出来是第 22 行的内容
    * 模型如实读出它看到的东西 → 与程序读数"不一致"
    * 高置信度 + 图文自洽的位置确认 → 一路放行，**覆盖掉正确的值**
    * 一次真实运行 184 个复核格被"改值"80 个，独立复核后 75 个是错的

    而且它全程**不报错**：镜像后的坐标依然合法，图依然裁得出来。

    判据只能是"裁出来的像素对不对"，因此这里用 PyMuPDF 自己的
    ``get_pixmap(clip=...)`` 作为真值 —— 它与 ``search_for`` 同源，
    必然指向正确位置。
    """
    fitz = pytest.importorskip("fitz")
    pytest.importorskip("PIL")

    from domain.tools.vlm import _crop_cell

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    # 只在页面**顶部附近**写一个数：镜像坐标会落到空白区，差异因此不可能混淆
    page.insert_text((100, 100), "501,000.00", fontsize=14)
    rect = page.search_for("501,000.00")[0]

    dpi = 300
    png = tmp_path / "page.png"
    page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False).save(str(png))

    cell = TableCell(
        row=0, col=0,
        bbox=BBox(x0=rect.x0, y0=rect.y0, x1=rect.x1, y1=rect.y1),
        text="501,000.00",
    )
    crop_path = _crop_cell(png, cell, tmp_path / "crop.png", dpi)

    from PIL import Image, ImageChops

    got = Image.open(crop_path).convert("L")
    # 真值：直接从 PDF 按同一矩形 clip 渲染
    truth = page.get_pixmap(clip=rect, dpi=dpi)
    truth_img = Image.frombytes("RGB", (truth.width, truth.height), truth.samples).convert("L")
    got_small = got.resize(truth_img.size, Image.Resampling.LANCZOS)

    # 先确认真值本身有内容（否则"空白 vs 空白"会假通过）
    ink = sum(1 for p in truth_img.getdata() if p < 128)
    assert ink > 50, "测试自身有问题：clip 渲染里没有墨迹"

    diff = ImageChops.difference(got_small, truth_img)
    mean_diff = sum(diff.getdata()) / (diff.width * diff.height)
    assert mean_diff < 20, (
        f"裁图与 clip 渲染差异过大（{mean_diff:.1f}）—— 像素框算错了。"
        f"最常见的原因是给 bbox 的 y 做了翻转（PyMuPDF 的 y 本来就向下）。"
    )
    got_ink = sum(1 for p in got_small.getdata() if p < 128)
    assert got_ink > 0, "裁图是空白的 —— 坐标指向了页面上的空白区域"


def test_cell_pixel_box_puts_top_of_page_near_zero() -> None:
    """bbox 越靠近页面顶部，像素 y 越小（y 向下）—— 不做翻转。"""
    from domain.tools.vlm import cell_pixel_box

    top = BBox(x0=10, y0=50, x1=60, y1=70)
    bottom = BBox(x0=10, y0=700, x1=60, y1=720)
    _, ty0, _, _ = cell_pixel_box(top, 300, (2480, 3508))
    _, by0, _, _ = cell_pixel_box(bottom, 300, (2480, 3508))
    assert ty0 < by0, "页面上方的格子必须落在图像上方（y 更小）"
    assert ty0 < 3508 / 2 < by0, "两格应分处页面上下两半"


# ==========================================================================
# 配置可达性（静默失效的防线）
# ==========================================================================


def test_tables_config_reachable_from_ctx() -> None:
    """★ 裁决策略必须能从 ``ctx.configs`` 直接读到。

    实测踩过：``configs/tools.yaml`` 里写了 ``tables.adjudicate: true``，
    但 ``build_context`` 只把整份配置放在 ``ctx.configs["tools"]`` 下，
    阶段函数读 ``ctx.configs.get("tables")`` 拿到的是 ``None`` ——
    于是裁决**静默不触发**：零调用、零产出、零报错。
    这类"配置写了但没生效"的失败极难发现，必须用测试锁住。
    """
    from agent.runtime.loop import build_context

    ctx = build_context(run_id="cfg-test", pipeline="r1_extract", use_mock=True)
    try:
        tables_cfg = ctx.configs.get("tables")
        assert isinstance(tables_cfg, dict), (
            "ctx.configs['tables'] 不可达 —— 表格裁决策略不会生效（静默失效）"
        )
        assert "adjudicate" in tables_cfg
        assert "max_cells_per_failed_table" in tables_cfg
        assert "probe_cells_per_unverified_table" in tables_cfg, (
            "缺少「无合计线」档的抽查预算 —— 这类表占多数，"
            "没有它就会把『无从校验』静默当成『校验通过』"
        )
        # 整份配置仍应保留
        assert "tools" in ctx.configs
    finally:
        if ctx.tracer:
            ctx.tracer.close()


def test_table_override_gates_are_declared() -> None:
    """覆盖闸门必须在配置里显式声明。

    「不一致就采用模型」是危险的 —— 那等于用另一个可能出错的源
    覆盖当前值。因此闸门条件必须是**可配置且可审**的。
    """
    from agent.config import load_config

    cfg = load_config("tools").get("tables") or {}
    req = cfg.get("override_requires") or {}
    assert req.get("quote_readback") is True, "必须要求 quote 回读成功"
    assert req.get("position_confirmed") is True, "必须要求位置确认"
    assert float(req.get("min_confidence", 0)) >= 0.5, "置信度下限过低"


def test_ocr_gating_skips_table_pages_with_text_layer() -> None:
    """★ 有文本层的表格页不应触发 OCR。

    实测代价：一个含 47 个表格页的文档，OCR 要跑 5 分钟以上。
    而表格页几乎都有完好文本层，表格结构由几何重建提供，不依赖 OCR。
    加门控后同一文档的 OCR 阶段降到 0.9 秒。
    """
    from pipelines.r1_extract.extract_ocr import _needs_ocr

    probe = {"char_count": 553, "garbled_ratio": 0.0}
    need, why = _needs_ocr("D_table", probe, min_chars=50, min_conf=0.05)
    assert not need, f"文本层可用的表格页不应 OCR：{why}"

    # 扫描页必须 OCR（无文本层就无路可走）
    need2, _ = _needs_ocr("B_scanned", {"char_count": 0}, min_chars=50, min_conf=0.05)
    assert need2, "扫描页必须 OCR"

    # 文本层不足的表格页仍需 OCR
    need3, _ = _needs_ocr("D_table", {"char_count": 10}, min_chars=50, min_conf=0.05)
    assert need3, "文本层不足时应回退到 OCR"
