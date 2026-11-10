"""``grid_align``（按坐标重建列结构）的测试。

★ 坐标用的是 **601857 2025 年报 p7「主要财务数据」表的实测值**（单位 pt），
不是编的。这一条很关键：本模块的全部判据都是从这组真实数字标定出来的
（同列重叠 100%、相邻列重叠 0%），用假坐标测不出判据是否成立。

实测原始单元格：

    行  格                                         x 区间
    0   ``截至或截止各年度``                        [352.63, 424.85]
    0   ``12`` / ``月`` / ``31`` / ``日``           碎片
    1   ``项`` [70.22, 79.22]  ``目`` [97.34, 106.34]
    1   ``2025`` [311.45, 329.53]  ``年`` [329.53, 340.75]
    1   ``2024`` [358.15, 375.97]  ``年`` [375.97, 387.07]
    2   ``营业收入`` [70.22, 106.33]
    2   ``2,864,469`` [304.73, 343.00]
    2   ``2,937,981`` [350.83, 389.08]

修复前：表头 9 格 / 数据 5 格，按列号配对 → 表头拼出 ``'截至或截止各年度项'``、
``'12目'``、``'月2025'`` 这类垃圾，期间与单位解析全部失效、整表 0 候选。
"""

from __future__ import annotations

from typing import Any

import pytest

from pipelines.r1_extract.grid_align import (
    _cluster_columns,
    align_grid_by_bbox,
)


def _cell(row: int, col: int, x0: float, x1: float, text: str) -> dict[str, Any]:
    return {
        "row": row, "col": col, "text": text,
        "bbox": {"x0": x0, "y0": 0.0, "x1": x1, "y1": 10.0},
    }


# ---------------------------------------------------------------------------
# 列聚类
# ---------------------------------------------------------------------------


def test_cluster_merges_overlapping_intervals() -> None:
    """同一列内不同位数的数字（宽度不同、中心漂移）必须聚成一列。

    实测：``2,864,469`` [304.73,343.00] 与 ``219,241`` [311.45,340.75]
    中心差约 6.5pt，但重叠 100%。
    """
    cols = _cluster_columns([(304.73, 343.00), (311.45, 340.75)])
    assert len(cols) == 1, cols


def test_cluster_keeps_adjacent_columns_separate() -> None:
    """相邻两列重叠为 0，绝不能并起来 —— 并起来就等于把两个期间合成一个。"""
    cols = _cluster_columns([
        (304.73, 343.00),   # 2025 列
        (350.83, 389.08),   # 2024 列
    ])
    assert len(cols) == 2, cols


def test_cluster_does_not_absorb_bracket_fragment() -> None:
    """被拆开的括号碎片（重叠约 33%）不得改变列边界。

    这是把阈值定在 0.8 而不是 0.5 的**唯一理由**：碎片应当归属到某一列、
    由文本拼接还原，而不是被当成列宽的一部分。
    """
    cols = _cluster_columns([(354.55, 359.05), (357.55, 389.08)])
    assert len(cols) == 2, f"阈值过低，括号碎片被并进了数据列：{cols}"


# ---------------------------------------------------------------------------
# 真实表的端到端对齐
# ---------------------------------------------------------------------------


@pytest.fixture
def p7_cells() -> list[dict[str, Any]]:
    """p7 表 0 的原始单元格（实测坐标，已省略无关行）。"""
    cells: list[dict[str, Any]] = []
    cells += [
        _cell(0, 0, 352.63, 424.85, "截至或截止各年度"),
        _cell(0, 1, 427.27, 436.21, "12"),
        _cell(0, 2, 436.21, 447.43, "月"),
        _cell(0, 3, 449.86, 458.80, "31"),
        _cell(0, 4, 458.80, 470.02, "日"),
        _cell(1, 0, 70.22, 79.22, "项"),
        _cell(1, 1, 97.34, 106.34, "目"),
        _cell(1, 2, 311.45, 329.53, "2025"),
        _cell(1, 3, 329.53, 340.75, "年"),
        _cell(1, 4, 358.15, 375.97, "2024"),
        _cell(1, 5, 375.97, 387.07, "年"),
    ]
    # 两行数据（值列坐标取自实测）
    for r, (label, v25, v24) in enumerate(
        [("营业收入", "2,864,469", "2,937,981"), ("净利润", "172,021", "183,755")],
        start=2,
    ):
        cells += [
            _cell(r, 0, 70.22, 106.33, label),
            _cell(r, 1, 304.73, 343.00, v25),
            _cell(r, 2, 350.83, 389.08, v24),
        ]
    return cells


def _merged_grid(n_rows: int) -> tuple[list[list[str]], list[list[int]]]:
    """模拟 ``merge_fragments`` 的输出：表头行碎片多、数据行整齐。"""
    grid = [
        ["截至或截止各年度", "12", "月", "31", "日", ""],
        ["项", "目", "2025", "年", "2024", "年"],
        ["营业收入", "2,864,469", "2,937,981", "", "", ""],
        ["净利润", "172,021", "183,755", "", "", ""],
    ]
    maps = [[[j] for j in range(len(r))] for r in grid]
    return grid[:n_rows], maps[:n_rows]


def test_alignment_puts_year_header_on_the_right_column(p7_cells) -> None:
    """★ 核心断言：``2025 年`` 必须与 ``2,864,469`` 落在**同一列**。

    修复前这两者永远对不上（表头 9 格 / 数据 5 格），期间只能靠猜。
    """
    grid, maps = _merged_grid(4)
    res = align_grid_by_bbox(grid, maps, p7_cells, data_start=2)
    assert res.applied, res.reason

    header, body = res.grid[1], res.grid[2]
    i_year = next(k for k, h in enumerate(header) if "2025" in h)
    assert body[i_year].replace(" ", "") == "2,864,469", (
        f"2025 年表头落在第 {i_year} 列，该列的值是 {body[i_year]!r}"
    )
    assert "2025" in header[i_year] and "年" in header[i_year], (
        f"表头碎片未拼回一个标签：{header[i_year]!r}"
    )


def test_alignment_keeps_row_label_in_first_column(p7_cells) -> None:
    """行标签列必须在 col0 —— ``extract_statement_facts`` 假定 ``row[0]`` 是科目名。"""
    grid, maps = _merged_grid(4)
    res = align_grid_by_bbox(grid, maps, p7_cells, data_start=2)
    assert res.applied
    assert res.grid[2][0] == "营业收入", res.grid[2]
    assert res.grid[3][0] == "净利润", res.grid[3]


def test_alignment_produces_uniform_column_count(p7_cells) -> None:
    """对齐后每行列数必须相同 —— 这正是"列结构不兼容"要消除的东西。"""
    grid, maps = _merged_grid(4)
    res = align_grid_by_bbox(grid, maps, p7_cells, data_start=2)
    assert res.applied
    widths = {len(r) for r in res.grid}
    assert len(widths) == 1, f"对齐后列数仍不一致：{widths}"


def test_alignment_moves_title_out_of_the_header_columns(p7_cells) -> None:
    """跨列标题 ``截至或截止各年度`` 不得与 ``项``/``目`` 粘成 ``'截至…项'``。

    实测修复前会拼出 ``'截至或截止各年度项'`` 这种垃圾标签，
    后果是列头单位解析不到 → 整表 0 条候选。
    """
    grid, maps = _merged_grid(4)
    res = align_grid_by_bbox(grid, maps, p7_cells, data_start=2)
    assert res.applied
    for h in res.grid[1]:
        assert "截至" not in h, f"标题污染了真表头：{res.grid[1]}"


# ---------------------------------------------------------------------------
# 安全性：判据不成立时必须放弃，而不是产出垃圾
# ---------------------------------------------------------------------------


def test_noop_when_cells_have_no_bbox() -> None:
    """纯文本后端（无 bbox）→ 必须原样返回，不能"猜"坐标。"""
    grid, maps = _merged_grid(4)
    cells = [{"row": r, "col": c, "text": t, "bbox": None}
             for r, row in enumerate(grid) for c, t in enumerate(row)]
    res = align_grid_by_bbox(grid, maps, cells, data_start=2)
    assert not res.applied
    assert res.grid == grid and res.col_maps == maps


def test_noop_when_too_few_value_columns() -> None:
    """只有一列数值 → 不值得冒险，放弃对齐。"""
    cells = [
        _cell(0, 0, 70.0, 120.0, "项目"),
        _cell(1, 0, 70.0, 120.0, "营业收入"),
        _cell(1, 1, 400.0, 450.0, "100"),
    ]
    grid = [["项目", ""], ["营业收入", "100"]]
    maps = [[[0], []], [[0], [1]]]
    res = align_grid_by_bbox(grid, maps, cells, data_start=1)
    assert not res.applied
    assert "不足以构成报表" in res.reason, res.reason


def test_refuses_when_two_whole_numbers_land_in_one_column() -> None:
    """同一格里两个**完整**数值 → 列边界定错了 → 必须放弃（宁缺勿错）。

    这是防"安静地算错"的关键一条：若判据失效却仍然输出，
    期间与数值会被系统性错配，而下游 quote 回读照样通过
    （数字确实印在原文里）。

    构造方式：两个值列的**大多数**观测给出了正确的两列边界
    （所以聚类能分出 2 列），但另有一行的两个数字挤在同一列区间内 ——
    这一行必须让整个对齐放弃。
    """
    cells = [
        _cell(0, 0, 70.0, 120.0, "项目"),
        _cell(0, 1, 300.0, 340.0, "2025 年"),
        _cell(0, 2, 500.0, 540.0, "2024 年"),
        _cell(1, 0, 70.0, 120.0, "营业收入"),
        _cell(1, 1, 300.0, 340.0, "2,864,469"),
        _cell(1, 2, 500.0, 540.0, "2,937,981"),
        _cell(2, 0, 70.0, 120.0, "净利润"),
        # ↓ 这一行的两个完整数值都落在第 1 列的区间里
        _cell(2, 1, 300.0, 340.0, "172,021"),
        _cell(2, 2, 305.0, 338.0, "183,755"),
    ]
    grid = [
        ["项目", "2025 年", "2024 年"],
        ["营业收入", "2,864,469", "2,937,981"],
        ["净利润", "172,021", "183,755"],
    ]
    maps = [[[j] for j in range(3)] for _ in range(3)]
    res = align_grid_by_bbox(grid, maps, cells, data_start=1)
    assert not res.applied, f"两个完整数值落进同一列却仍然输出了：{res.grid}"
    assert "完整数值" in res.reason, res.reason


def test_noop_without_cells_at_all() -> None:
    grid, maps = _merged_grid(4)
    res = align_grid_by_bbox(grid, maps, None, data_start=2)
    assert not res.applied
    assert res.grid == grid


# ---------------------------------------------------------------------------
# 分部报告：「按取值落在哪一列定口径」
# ---------------------------------------------------------------------------
#
# 实测来源：601857 2025 年报 p178（(a) 2025 年度分部信息）。
# 列头是**业务分部名 + 合计**，不是期间：
#
#     |（行标签）  | 油气和新能源 | 炼油化工和新材料 | 销售 | 天然气销售 | 总部及其他 | 合计 |
#     | 资本性支出 | 205,094     | 47,772         | 8,216 | 4,987    | 3,020     | 269,089 |
#
# 其中 269,089 是**集团口径**的资本开支，205,094 只是勘探与生产板块的。
# 补齐"按列定口径"之前，两者同主体同期间 → 被冲突守卫判为多口径冲突 →
# 集团合计取不出来。

_P178_HEADER = [
    ["", "", "", "", "", "", ""],
    ["", "油气和", "炼油化工和", "", "天然气", "总部及", ""],
    ["", "新能源", "新材料", "销售", "销售", "其他", "合计"],
]
_P178_BODY = [
    ["分部收入", "824,808", "1,078,047", "2,352,746", "619,503", "7,168", "4,882,272"],
    ["资本性支出", "205,094", "47,772", "8,216", "4,987", "3,020", "269,089"],
]


def test_detects_segment_total_column() -> None:
    """分部表的「合计」列必须被定位到（本表列号为 6）。"""
    from pipelines.r1_extract.grid_align import detect_segment_total_col

    grid = _P178_HEADER + _P178_BODY
    # 返回**集合**（两层列头的分部表会有多个合计列，它们同属集团口径）
    assert detect_segment_total_col(grid, data_start=3) == {6}


def test_rejects_non_segment_table_with_total_column() -> None:
    """有「合计」列的**普通表**不得被当成分部表。

    实测动机：000722 的坏账准备表也有 ``合计`` 列，而它的列是
    ``第一阶段/第二阶段/第三阶段``（账龄阶段），不是业务分部。
    若被当成"分部列"，这张表的口径会被错标成 segment_detail。
    挡它的判据是"行标签里必须有『分部』"。
    """
    from pipelines.r1_extract.grid_align import detect_segment_total_col

    grid = [
        ["", "第一阶段", "第二阶段", "第三阶段", ""],
        ["坏账准备", "未来12个月", "整个存续期", "整个存续期", "合计"],
        ["期初余额", "1,403,137.79", "842,375.98", "2,400.00", "2,247,913.77"],
    ]
    assert detect_segment_total_col(grid, data_start=2) is None


def test_multiple_total_columns_are_all_group_caliber() -> None:
    """两个「合计」列时，**两列都算集团口径**（不再放弃定位）。

    实测动机：002716 的分部合同表列头是两层的 ——

        r0  合同分类 | 分部1 | 分部1 | 分部2 | 分部2 | 本期数 | 本期数 | 合计 | 合计
        r1  合同分类 | 营业收入 | 营业成本 | 营业收入 | 营业成本 | 营业收入 | 营业成本 | 营业收入 | 营业成本

    拼接后两列都含「合计」（``合计营业收入`` / ``合计营业成本``）。
    旧实现见多于一列就返回 None（"无法判断哪列代表集团整体"）——
    但这两列**都是**集团口径，只是分别装营业收入与营业成本。
    返回 None 的代价是整张表的口径**全部**退回 consolidated，
    与分部明细撞成"同表多口径冲突"。
    """
    from pipelines.r1_extract.grid_align import detect_segment_total_col

    # 实测结构（002716 p170）：两层表头 + 两行数据
    grid = [
        ["合同分类", "分部1", "分部1", "分部2", "分部2", "合计", "合计"],
        ["合同分类", "营业收入", "营业成本", "营业收入", "营业成本",
         "营业收入", "营业成本"],
        ["业务类型", "", "", "", "", "", ""],
        ["其中：", "", "", "", "", "", ""],
        ["有色金属及其制品", "", "", "", "", "12,371,785,621", "11,548,771,649"],
        ["其他", "", "", "", "", "62,870,408", "54,931,299"],
    ]
    # 列 5、6 拼接后是「合计营业收入」「合计营业成本」—— 两列同属集团口径。
    # 判据 3（行标签含「分部」）由表头里的「分部1/分部2」满足：
    # 拼接后的 col1..4 是「分部1营业收入」等，落在 others 之外（含数字），
    # 而 col0 是标签列 —— 因此这里用另一种形态验证：直接给出含「分部」的行标签。
    grid[4][0] = "分部收入合计行"      # 让行标签区出现「分部」二字
    assert detect_segment_total_col(grid, data_start=4) == {5, 6}


def test_year_in_title_row_does_not_block_detection() -> None:
    """标题行里的年份碎片**不得**否决整张分部表。

    实测踩过两次：601857 的标题是
    ``(a) 2025 年度及 | 2025 年 | 12 月 | 31 | 日分部信息``，
    日期碎片与分部名落在**同一列**，拼接后得到 ``'2025 年油气和新能源'``。
    第一版判据"表头含年份即否决"把这张真正的分部表杀掉了；
    第二版"只看最上面一行"仍然误杀。
    正确做法是"分部名列不得含数字" —— 只丢那一列，不否决整表。
    """
    from pipelines.r1_extract.grid_align import detect_segment_total_col

    grid = [
        ["(a) 2025 年度及", "2025 年", "12 月 31", "日分部信息", "", "", ""],
        ["", "油气和", "炼油化工和", "", "天然气", "总部及", ""],
        ["", "新能源", "新材料", "销售", "销售", "其他", "合计"],
        ["分部收入", "824,808", "1,078,047", "2,352,746", "619,503", "7,168", "4,882,272"],
    ]
    assert detect_segment_total_col(grid, data_start=3) == {6}


def test_single_member_columns_are_dropped() -> None:
    """只出现一次的列簇要丢掉 —— 表体内的**期间碎片行**会造成伪列。

    实测（601857 p178 表 1，没有自己表头的那张）：表里有一行
    ``2025 | 年 | 12 | 月 | 31 | 日``。``2025``/``12``/``31`` 都是"数值"
    且各只出现一次，于是把一张 6 列的表撑成 9 列，列号整体错位。
    """
    from pipelines.r1_extract.grid_align import _cluster_columns

    ivs = [
        (86.42, 106.52),    # '2025'（期间碎片，只出现一次）
        (121.58, 131.48),   # '12'（同上）
        (146.66, 156.56),   # '31'（同上）
        (198.77, 241.22), (206.00, 239.00),   # 真列 1（两个成员）
        (262.97, 297.98), (268.00, 293.00),   # 真列 2
    ]
    assert len(_cluster_columns(ivs)) == 5, (
        "不过滤时：3 个期间碎片各成 1 簇 + 2 个真列各成 1 簇 = 5 簇"
    )
    kept = _cluster_columns(ivs, min_members=2)
    assert len(kept) == 2, f"过滤后应只剩 2 个真列，实际 {kept}"


def test_remap_column_between_different_grids() -> None:
    """原始列号必须**按目标网格换算**，不能直接套用。

    实测踩过：合计列在"有表头的那张表"里判出（对齐后 7 列，合计列 = 6），
    而承载目标行的表**没有自己的表头**、对齐后是另一个列数。
    直接套用会把普通数据列标成"集团口径" —— 数字仍对、标签全错、不报错。
    """
    from pipelines.r1_extract.grid_align import remap_col_via_maps

    # 目标网格 7 列，每列对应一个原始列 → 原样
    maps7 = [[[j] for j in range(7)]]
    assert remap_col_via_maps(6, maps7) == 6

    # 目标网格 10 列（多出 3 个空列），原始列 6 落在第 7 个对齐列
    maps10 = [[[0], [1], [2], [], [3], [4], [5], [6], [7], [8]]]
    assert remap_col_via_maps(6, maps10) == 7

    # 原始列不在映射里 → 放弃，不许猜
    assert remap_col_via_maps(99, maps7) is None


def test_segment_scope_by_column() -> None:
    """口径由**取值落在哪一列**决定：合计列 = 集团，其余 = 板块。"""
    from pipelines.r1_extract.table_facts import _segment_scope

    assert _segment_scope(6, 6) == "segment_total"
    assert _segment_scope(1, 6) == "segment_detail"
    assert _segment_scope(5, 6) == "segment_detail"
    # 不是分部表 → 空串，调用方回退到文档级口径（保持原行为）
    assert _segment_scope(3, None) == ""


def test_table_period_hint_finds_body_year() -> None:
    """分部报告的年份印在**表体内的碎片行**上，要从那里取出来。

    实测：601857 分部报告用 ``(a) 2025`` / ``(b) 2024`` 两组印出，
    而列头只有分部名 → ``periods_of_columns`` 一列期间都取不到 →
    整表回落到文档级期间 → **2024 年的 275,849 被标成 2025A**。
    """
    from pipelines.r1_extract.table_facts import table_period_hint

    grid_2025 = [
        ["折旧、折耗和摊销费用", "186,238", "27,746", "17,727", "4,551", "1,576", "237,838"],
        ["资本性支出", "205,094", "47,772", "8,216", "4,987", "3,020", "269,089"],
        # ↓ 期间碎片行，落在表的**底部**
        ["", "2025", "年", "12", "月", "31", "日"],
    ]
    assert table_period_hint(grid_2025) == "2025A"

    grid_2024 = [
        ["资本性支出", "227,633", "33,489", "7,188", "4,300", "3,239", "275,849"],
        ["", "2024", "年", "12", "月", "31", "日"],
    ]
    assert table_period_hint(grid_2024) == "2024A"


def test_table_period_hint_refuses_when_ambiguous() -> None:
    """表内出现**多个不同年份**时不猜 —— 多期并排的表无法从表体判定期间。"""
    from pipelines.r1_extract.table_facts import table_period_hint

    grid = [
        ["项目", "2025", "年", "2024", "年"],
        ["营业收入", "2,864,469", "", "2,937,981", ""],
    ]
    assert table_period_hint(grid) == ""


def test_table_period_hint_ignores_amounts() -> None:
    """金额不是年份 —— 判据要求整格恰好是 4 位年份。"""
    from pipelines.r1_extract.table_facts import table_period_hint

    grid = [["营业收入", "2,864,469"], ["净利润", "157,302"]]
    assert table_period_hint(grid) == ""


def test_indented_row_labels_stay_in_label_column() -> None:
    """★ 行标签**缩进不一致**时，标签不得被算进数值列。

    实测（601857 p176 现金流量表）：行标签的 x 位置不统一 ——
    ``净利润`` 在 x=86，而 ``固定资产折旧、油气资产折耗`` 在 x=107（缩进 20pt）。

    旧判据是"与最左边界等距（±2pt）"，于是缩进的那几个标签不算标签，
    被按 x 归进**第一个数值列**，与数值挤进同一格：

        '固定资产折旧、油气资产折耗 215,818'

    后果：现金流量表的 D&A **整条读不出来**（下游按"第 0 列是科目名、
    其余列是数值"取值），"折旧摊销占收入比"从事实值退回模板值 ——
    **而且不报错**，看起来像"这家公司没披露"。

    新判据是"不与任何数值列重叠"，对缩进深度不敏感。
    """
    from pipelines.r1_extract.grid_align import align_grid_by_bbox

    # 实测坐标：标签 x=86 / x=107，数值列 x0 = 282 / 346 / 410 / 485
    def cell(row, col, x0, x1, text):
        return {"row": row, "col": col, "text": text,
                "bbox": {"x0": x0, "y0": row * 15.0, "x1": x1, "y1": row * 15.0 + 10}}

    cells = [
        cell(0, 0, 272, 292, "2025"), cell(0, 1, 336, 356, "2024"),
        cell(1, 0, 86, 141, "净利润"),
        cell(1, 1, 282, 324, "172,005"), cell(1, 2, 346, 388, "183,747"),
        cell(2, 0, 107, 201, "固定资产折旧、油气资产折耗"),   # ← 缩进 20pt
        cell(2, 1, 282, 324, "215,818"), cell(2, 2, 346, 388, "210,990"),
    ]
    grid = [
        ["2025", "2024"],
        ["净利润", "172,005", "183,747"],
        ["固定资产折旧、油气资产折耗", "215,818", "210,990"],
    ]
    maps = [[[j] for j in range(len(r))] for r in grid]

    res = align_grid_by_bbox(grid, maps, cells, data_start=1)
    assert res.applied, res.reason
    # 标签必须落在第 0 列，数值必须在它右边
    assert res.grid[2][0] == "固定资产折旧、油气资产折耗", res.grid[2]
    assert "215,818" in " ".join(res.grid[2][1:]), (
        f"215,818 没有成为独立的数值格：{res.grid[2]}"
    )
    assert "215,818" not in res.grid[2][0], "数值被粘进了标签格"


def test_noop_when_alignment_would_bury_a_value() -> None:
    """数值若会被并进文字标签，必须整体放弃对齐（宁可列号不准，不可少数据）。"""
    from pipelines.r1_extract.grid_align import _drops_values

    located = [
        ({"row": 0, "text": "净利润", "bbox": {"x0": 86, "y0": 0, "x1": 141, "y1": 10}},
         (86.0, 141.0)),
        ({"row": 0, "text": "172,005", "bbox": {"x0": 282, "y0": 0, "x1": 324, "y1": 10}},
         (282.0, 324.0)),
    ]
    # 数值与标签被放进同一格 → 视为丢失
    buried = {(0, 0): [("净利润", 0), ("172,005", 1)]}
    assert _drops_values(buried, located) is True
    # 数值独占一格 → 正常
    ok = {(0, 0): [("净利润", 0)], (0, 1): [("172,005", 1)]}
    assert _drops_values(ok, located) is False
    """★ 候选口径 → 契约枚举的映射必须**接住每个值**。

    实测踩过（2026-09 第十三轮）：这一步原本是内联在候选入库循环里的一段
    if/elif 白名单。新增 ``segment_total`` / ``segment_detail`` 之后，
    两个新值落进 else 分支被覆盖成 ``consolidated`` ——
    **"按取值落在哪一列定口径"在产物里完全看不出来**：表里已经算对了，
    事实库里却仍是 consolidated，冲突守卫照样把 24 条候选全标 needs_review。
    数字全部合法、**不报错**，只是这一轮改动等于白做。

    这条测试盯的就是"新增口径值时忘记接线"。
    """
    from agent.evidence.models import StatementScope
    from pipelines.r1_extract.build_facts import (
        _SCOPE_OF_CANDIDATE,
        scope_of_candidate,
    )

    assert scope_of_candidate("segment_total", "") is StatementScope.SEGMENT_TOTAL
    assert scope_of_candidate("segment_detail", "") is StatementScope.SEGMENT_DETAIL

    # StatementScope 的每个值都得在这张表里有归宿（unknown 除外，它走兜底）
    for member in StatementScope:
        if member is StatementScope.UNKNOWN:
            continue
        assert member in set(_SCOPE_OF_CANDIDATE.values()), (
            f"{member.value} 没有对应的候选口径字符串，"
            "新增枚举时必须同步这里"
        )


def test_statement_scope_values_are_distinct() -> None:
    """两个分部口径必须与 consolidated 区分开 —— 否则 caliber_key 相同，冲突守卫依旧误报。"""
    from agent.evidence.models import StatementScope

    vals = {
        StatementScope.CONSOLIDATED.value,
        StatementScope.SEGMENT_TOTAL.value,
        StatementScope.SEGMENT_DETAIL.value,
        StatementScope.PARENT_ONLY.value,
    }
    assert len(vals) == 4
