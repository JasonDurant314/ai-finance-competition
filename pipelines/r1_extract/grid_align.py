"""按**单元格 bbox** 重建表格网格 —— 抽取层「列结构不兼容」的根治尝试。

问题
----
本项目的 PDF 后端给出的是**按坐标逐格**的文本，同一视觉单元格可能被拆成
多格（``2025 | 年``），而**表头行与数据行的拆分粒度不同**。实测
601857 2025 年报 p7「主要财务数据」（单位 pt）：

===========  ==============================================
行            单元格 x 区间
===========  ==============================================
表头 r1       ``项`` ``目`` | ``2025`` ``年`` | ``2024`` ``年`` …
数据 r2       ``营业收入`` | ``2,864,469`` | ``2,937,981`` …
===========  ==============================================

表头把每个年份拆成 2 格，数据行是 1 格，于是**两套列结构互不兼容**：
任何"按列号配对表头与数据"的做法都会错位。

现有缓解（``table_facts.build_header``）只能**跳过**列数不一致的表头行 ——
它让标题行不再污染表头，但真表头（``2025``/``年``）仍然贴着错误的列号，
期间与单位解析因此失效（README 已知限制第 13 条）。

本模块的做法
------------
不看列号，**只看 x 坐标**。从实测数据标定出的判据：

1. **列边界从数据行反推**，绝不从表头猜 —— 表头正是被拆碎的那一方
2. 同一列内的格**大幅重叠**（实测 100%），相邻两列**几乎不重叠**（实测 0%）
   → 用「重叠率 ≥ 0.8」判同列，比"中心点距离"稳健（数字宽度随位数变化，
   中心点会漂移十几个点，而区间重叠不受影响）
3. 数据行里**最左的非数值列是行标签列**，其余是数值列 —— 这与
   :func:`table_facts.extract_statement_facts` 的假设（``row[0]`` 是科目名）
   天然一致

安全性设计（宁可不用，不可用错）
--------------------------------
只做**有界**的事，任一条件不满足就返回 ``applied=False``，调用方保持原行为：

* 无 bbox（纯文本后端）→ 不对齐
* 数值列少于 2 个 → 不对齐（列太少没有歧义，不值得冒险）
* 列边界互不重叠且有序，否则 → 不对齐
* 同一格里出现两个**独立完整数值** → 说明列边界定错了 → 不对齐

**这是本模块存在的前提：它只能让结果变好，不能因为自己出错而让结果变差。**
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

# 两个区间重叠超过**较窄者**的这个比例时，视为同一列。
#
# 实测标定（601857 2025 年报 p7，单位 pt）：
#
#   同一列内：``2,864,469`` [304.73,343.00] vs ``219,241`` [311.45,340.75]
#             → 重叠 29.30 / 较窄宽 29.30 = **100%**
#   相邻两列：同两格 vs ``2,937,981`` [350.83,389.08]
#             → 重叠 **0**
#
# 取 0.8 而不是 0.5 是刻意的：0.5 会把"被拆开的括号碎片"
# （``(`` 与 ``(54,144)`` 重叠约 33%）也并进来，而那种碎片应当各自
# 归属到**同一个**列、由文本拼接还原，而不是被当成列边界的一部分。
_OVERLAP_RATIO = 0.8

# 至少这么多**数值列**（不含行标签列）才做对齐
_MIN_VALUE_COLS = 2


@dataclass
class AlignedGrid:
    """坐标对齐的结果。

    ``applied=False`` 时 ``grid`` / ``col_maps`` 与输入**完全一致**，
    调用方不需要分支处理。
    """

    grid: list[list[str]]
    col_maps: list[list[int]]
    applied: bool = False
    reason: str = ""
    n_cols: int = 0
    col_bounds: list[tuple[float, float]] = field(default_factory=list)
    # 分部报告表的 **合计列集合**（列号）；不是分部表时为空集。
    # 有它才能把"集团合计"与"某板块明细"分开标口径（见 detect_segment_total_col）。
    # ⚠️ 用**集合**而不是单列：两层列头的分部表会有多个合计列
    #    （实测 002716 的「合计营业收入 / 合计营业成本」），它们同属集团口径。
    segment_total_cols: frozenset[int] = frozenset()

    @property
    def note(self) -> str:
        if self.applied:
            return f"已按单元格坐标重建 {self.n_cols} 列"
        return f"未做坐标对齐（{self.reason}）"


# ---------------------------------------------------------------------------
# 分部报告：定位「合计」列
# ---------------------------------------------------------------------------

# 「合计」列的列头写法。实测（四份真实年报，共 10 张分部报告表）有这些形态：
#
#   601857（列已对齐）  ``'合计'`` / ``'除特别注明外，金额单位为人民币百万元 )合计'``
#   002716（列未对齐）  ``'合计营业收入'`` / ``'合计营业成本'``
#
# 因此判据是**包含**而非相等。但"包含"会误伤 ``'本期数营业收入'``？
# 不会 —— 它不含「合计」。真正的误伤风险来自 ``'小计'``，
# 所以这里只收「合计 / 总计」，并用"该列右邻不得是另一条财务科目"兜底（见下）。
_TOTAL_HEADER_RE = re.compile(r"合计|总计")

# 分部报告的**标志性行标签** —— 出现它才认定这是一张分部报告表。
#
# 为什么必须有这一条（实测踩过）：仅凭"表头里有『合计』列"会把一大堆
# 普通表格误判成分部表 —— 比如 000722 的坏账准备表就有 ``合计`` 列，
# 而它把「第一阶段/第二阶段/第三阶段」当成分部，口径会被标错。
# 「分部」二字是分部报告独有的，用它做闸门最稳。
_SEGMENT_ROW_RE = re.compile(r"分部")

# 分部列至少要有这么多列（不含标签列与合计列），否则不像分部报告
_MIN_SEGMENT_COLS = 2


def _drops_values(
    picked: dict[tuple[int, int], list[tuple[str, int]]],
    located: list[tuple[dict[str, Any], tuple[float, float]]],
) -> bool:
    """重建后是否有数值格**与文字标签挤进了同一格**（= 读不出来了）？

    ⚠️ 判据不能是"文本还在不在" —— 实测踩过：``215,818`` 并没有消失，
    而是与行标签**粘进了同一个格**（``'固定资产折旧、油气资产折耗 215,818'``）。
    文字检查会认为"还在"，而下游 `extract_statement_facts` 按
    "第 0 列是科目名、其余列是数值"取值，这个数**再也读不出来** ——
    与丢失等价。

    正确判据：每个数值格必须与**它所在行最左的文字标签**落在**不同的格**里。

    ⚠️ 只看同一行里"有没有这个数字"是不够的（第一版就是这么写的，等于没查）：
    拼进标签格的数字仍然是"存在"的，检查会通过，而缺陷照样发生。
    必须比较**归属到哪一格**。
    """
    # 行 → 格号 → 碎片列表
    by_row: dict[int, dict[int, list[tuple[str, int]]]] = {}
    for (r, k), items in picked.items():
        by_row.setdefault(r, {})[k] = items

    for c, _iv in located:
        text = str(c.get("text") or "").strip()
        if not _is_numeric_text(text):
            continue
        core = _digits(text)
        if not core:
            continue
        r = int(c.get("row", -1))
        cells = by_row.get(r)
        if not cells:
            return True                      # 整行都没了 → 丢
        # 这个数字落在哪些格
        holder = [
            k for k, items in cells.items()
            if any(_digits(t) == core for t, _ in items)
        ]
        if not holder:
            return True                      # 数字不见了 → 丢
        # 该行最左的**文字**格（标签格）
        labels = {
            k for k, items in cells.items()
            if any(_has_cjk(t) for t, _ in items)
        }
        if labels and holder and min(labels) == min(holder):
            # 数值与标签同格 → 下游读不出来
            return True
    return False


def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def _digits(text: str) -> str:
    """取出文本里的数字骨架（去千分位、括号、百分号、汉字与空格）。

    用于判断"这个数值是否还在某一格里独占" —— ``'215,818'`` 与
    ``'215818'`` 视为同一个数，而 ``'固定资产折旧 215,818'`` 的骨架是
    ``'215818'``，与 ``'215,818'`` 相同 —— 因此**不能只看骨架相等**，
    还要看那一格是不是"以数值为主"。
    """
    return re.sub(r"[^\d.]", "", text or "")


def _with_repaired_text(cell: dict[str, Any]) -> dict[str, Any]:
    """返回该单元格的**浅拷贝**，text 换成折行修复后的形态。

    为什么需要（2026-09 实测 002716 的分部合同表）：网格里的数字是
    ``repair_line_broken_numbers`` 修好的（``'12,371,785,621.02'``），
    而 ``cells`` 里保留原始折行（``'12,371,78\\n5,621.02'``）——
    两条路径的文本口径不一致，于是"这个格是不是数字"的判断在
    cells 上一律为假，数值列判成 0 列、整表放弃对齐。

    ⚠️ 只改 text，其余字段（尤其 bbox）原样保留 —— 几何信息是入参的一部分。
    """
    from pipelines.r1_extract.table_facts import repair_line_broken_numbers

    out = dict(cell)
    text = str(cell.get("text") or "")
    if text:
        out["text"] = repair_line_broken_numbers(text)[0]
    return out


def remap_col_via_maps(col: int, col_maps: list[list[int]]) -> int | None:
    """把一个**原始列号**换算成 ``col_maps`` 所属网格里的列号。

    为什么需要它（实测踩过的坑）：分部报告的合计列是在**有表头的那张表**里
    判出来的（``p178 表0``，对齐后 7 列，合计列 = 6），而承载目标行
    （资本性支出）的表在下面、**没有自己的表头**（``p178 表1``）。
    两张表的列数可以不同 —— 实测表 0 对齐后 7 列、表 1 对齐后 10 列。

    **原始列号只在它所属的那套网格里有意义。** 直接把 6 套到 10 列的网格上
    会把"第 6 列"标成合计列，而那一列其实是普通数据列 —— 口径全错，
    而且不会报错（数字本身仍然是对的，只是标签错）。

    换算方式：对每个对齐后的列，看它的 ``col_maps`` 是否覆盖该原始列号。
    ``col_maps[k]`` 是第 k 列由哪些原始列拼成，因此"原始列 c 落在第 k 列"
    等价于 ``c in col_maps[k]``。

    返回 ``None`` 表示该原始列不在这个网格的映射里 —— 调用方应当**放弃**
    这次换算，而不是猜一个接近的列号。

    ⚠️ ``col_maps`` 与 grid **同形（二维）**：``col_maps[row][col]`` 才是
    该格的原始列号列表。合计列在所有数据行里位置相同，故取第一行判定。
    """
    first_row = col_maps[0] if col_maps else []
    for k, idxs in enumerate(first_row):
        if col in (idxs or []):
            return k
    # 退化情形：某些网格的 col_maps 不完整（如列被脚注剔除）。
    # 此时按"原始列号的顺序位置"折算 —— 仅在列数一致时可信，故加此限制。
    #
    # ⚠️ `col_maps` 与 grid 同形，是**二维**的：`col_maps[row][col]` 才是
    #    该格的原始列号列表。合计列在所有数据行里的位置相同，取第一行即可。
    first_row = col_maps[0] if col_maps else []
    flat = [j for idxs in first_row for j in (idxs or [])]
    n_raw = (max(flat) + 1) if flat else 0
    if n_raw == len(first_row) and 0 <= col < n_raw:
        return col
    return None


# 财务科目名 —— 列头里出现这些词说明那一列是**指标**而不是分部名。
#
# 用于 :func:`detect_segment_total_col` 判据 2：分部表的列头是
# 「分部名 [+ 指标名]」，而分部名本身可能含数字（``分部1``/``分部2``）。
# 因此不能靠"含不含数字"判断，要靠"是不是科目名"。
_FINANCIAL_ITEM_RE = re.compile(
    r"营业收入|营业成本|营业利润|净利润|利润总额|资产|负债|权益|现金流|"
    r"折旧|摊销|费用|支出|收入|成本|合计|总计"
)

# 分部报告的自称 —— 列头里出现它即为分部名列（判据 b）。
_SEGMENT_WORD_RE = re.compile(r"分部|板块|业务分部|经营分部")


def detect_segment_total_col(grid: list[list[str]], *, data_start: int) -> int | None:
    """找出**分部报告表**的「合计」列号；不是分部表就返回 None。

    这是第十二轮解锁的能力第一次被用上：按单元格坐标重建列结构之后，
    ``合计`` 列的位置可以被稳定定位，于是"按取值落在哪一列定口径"可做。

    判据（三条**全部**满足，缺一不可 —— 只做有把握的判定）：

    1. **表头里有一列写着「合计」或「总计」**
    2. **至少 2 个其它非数值列标签** —— 那些就是分部名
       （实测：``油气和新能源``、``炼油化工和新材料``、``销售``、``天然气销售``、
       ``总部及其他``；以及 002716 的 ``分部1营业收入``、``分部2营业成本``）
    3. **行标签区出现「分部」字样** —— 分部报告独有的标志

    第 3 条是必需的：只凭前两条会把普通表误判成分部表。
    实测 000722 的坏账准备表也有 ``合计`` 列，若被当成"分部列"，
    它的口径会被错标成 ``segment_detail``，而它其实是**账龄阶段**。
    """
    if not grid or data_start <= 0 or data_start > len(grid):
        return None

    n_cols = max((len(r) for r in grid), default=0)
    if n_cols < _MIN_SEGMENT_COLS + 2:
        return None

    # ---- 判据 3：行标签区出现「分部」 ----
    labels = " ".join(
        str(grid[i][0]) for i in range(data_start, len(grid)) if grid[i]
    )
    if not _SEGMENT_ROW_RE.search(labels):
        return None

    # ---- 逐列拼表头 ----
    head = [
        "".join(
            str(grid[i][j]).strip()
            for i in range(data_start)
            if j < len(grid[i])
        )
        for j in range(n_cols)
    ]

    total_cols = [j for j, h in enumerate(head) if _TOTAL_HEADER_RE.search(h)]
    if not total_cols:
        return None                     # 没有「合计」列 → 不是分部表
    # ★ **允许多个合计列**（2026-09 实测 002716 的分部合同表）。
    #
    #   该表列头是两层的：
    #
    #       r0  合同分类 | 分部1 | 分部1 | 分部2 | 分部2 | 本期数 | 本期数 | 合计 | 合计
    #       r1  合同分类 | 营业收入 | 营业成本 | 营业收入 | 营业成本 | 营业收入 | 营业成本 | 营业收入 | 营业成本
    #
    #   拼接后两列都含「合计」（``合计营业收入`` / ``合计营业成本``）。
    #   旧实现见多于一列就返回 None（"无法判断哪列代表集团整体"）——
    #   但这里**两列都是**集团口径，只是分别装营业收入与营业成本。
    #   返回 None 的代价是整张表的口径**全部**退回 consolidated，
    #   与分部明细撞成"同表多口径冲突"。
    #
    #   改成返回**集合**：多个合计列都标 `segment_total`，
    #   其余列标 `segment_detail`。这与"哪列是集团整体"无关 ——
    #   它回答的是"这个取值是不是集团口径"，而多列同属集团口径是常态。
    total = set(total_cols)

    # ---- 判据 2：其它非数值列标签（分部名）够多 ----
    #
    # ★ 分部名里**不含数字**，期间标签里**一定含数字**（``2025 年``）。
    #   这一条同时替代了两版失败的写法：
    #
    #   * 第一版"表头出现年份即否决" —— 误杀。601857 的标题行
    #     ``(a) 2025 年度及 | 2025 年 | 12 月 | 31 | 日分部信息`` 里的日期碎片
    #     与分部名落在**同一列**，拼接后得到 ``'2025 年油气和新能源'``。
    #     年份是标题带来的噪声，不是那一列的语义，但"含年份"判据照样否决。
    #   * 第二版"只看最上面一行有没有年份" —— 仍然误杀，因为 p178 的标题行
    #     本身就带 ``2025 年``。
    #
    #   改成"分部名列里不得含数字"之后：``'2025 年油气和新能源'`` 被排除
    #   （那一列的分部归属丢失），而 ``'炼油化工和新材料'``/``'天然气销售'``
    #   保留 —— 判据从"否决整张表"变成"只丢一列"，**更不容易误杀**。
    #   实测 601857 p178 仍剩 4 列，远高于下限 2。
    #   ★ 但"不得含数字"这一条**对 002716 的分部合同表失效**（2026-09 实测）：
    #   那张表的分部名列头是 ``分部1营业收入``、``分部2营业成本`` ——
    #   **含数字**（分部编号），旧判据把它们全排除，``others`` 只剩标签列，
    #   于是 `len(others) < 2` → 返回 None → 整表口径退回 consolidated。
    #
    #   ★ 改用两条**任一成立**的判据（2026-09 第十六轮，第二次收紧）：
    #
    #   (a) 至少 2 列是"纯分部名" —— 不含报告年份、也不是财务科目名。
    #       601857：``油气和新能源`` / ``炼油化工和新材料`` ✓
    #   (b) 拼接头里出现 **「分部」二字** —— 分部报告的直接标志。
    #       002716：``分部1营业收入`` / ``分部2营业成本`` ✓
    #
    #   为什么需要 (b)：002716 的分部名列头含**分部编号**（``分部1``），
    #   而"不得含数字"的旧判据把它们全排除；改用"不是财务科目名"之后
    #   又因为标签里带 ``营业收入`` 被排除 —— 两条路都堵死。
    #   而「分部」二字是分部报告的自称，直接、稳定、不需要推断。
    #
    #   ⚠️ 实测教训保留：(a) 里仍要排除**报告年份**，因为 601857 的标题行
    #   ``(a) 2025 年度及 …`` 的日期碎片会与分部名落在同一列，拼成
    #   ``'2025 年油气和新能源'`` —— 那是标题噪声，不是这一列的语义。
    pure_segment = [
        j for j in range(1, n_cols)
        if j not in total
        and head[j]
        and not _is_numeric_text(head[j])
        and not re.search(r"20\d{2}", head[j])
        and not _FINANCIAL_ITEM_RE.search(head[j])
    ]
    named_segment = [
        j for j in range(1, n_cols)
        if j not in total and _SEGMENT_WORD_RE.search(head[j] or "")
    ]
    if len(pure_segment) < _MIN_SEGMENT_COLS and len(named_segment) < _MIN_SEGMENT_COLS:
        return None

    return total


def _seg_debug(grid: list[list[str]], data_start: int) -> dict[str, Any]:
    """临时诊断（不参与生产）：把 detect 的每条判据摊开。"""
    n_cols = max((len(r) for r in grid), default=0)
    labels = " ".join(str(grid[i][0]) for i in range(data_start, len(grid)) if grid[i])
    head = [
        "".join(str(grid[i][j]).strip() for i in range(data_start) if j < len(grid[i]))
        for j in range(n_cols)
    ]
    total_cols = [j for j, h in enumerate(head) if _TOTAL_HEADER_RE.search(h)]
    others = [
        j for j in range(1, n_cols)
        if j not in total_cols and head[j] and not _is_numeric_text(head[j])
    ]
    return {
        "n_cols": n_cols, "head": head, "total_cols": total_cols,
        "others": others, "labels_has_fenbu": bool(_SEGMENT_ROW_RE.search(labels)),
        "others_years": [head[j] for j in others if re.search(r"20\d{2}", head[j])],
        "numeric_head": [head[j] for j in range(1, n_cols) if _is_numeric_text(head[j])],
    }


# ---------------------------------------------------------------------------
# 基础几何
# ---------------------------------------------------------------------------


def _x_interval(bbox: Any) -> tuple[float, float] | None:
    """从 bbox（dict 或对象）取 ``(x0, x1)``；取不到返回 None。"""
    if bbox is None:
        return None
    if isinstance(bbox, dict):
        x0, x1 = bbox.get("x0"), bbox.get("x1")
    else:
        x0, x1 = getattr(bbox, "x0", None), getattr(bbox, "x1", None)
    try:
        x0f, x1f = float(x0), float(x1)
    except (TypeError, ValueError):
        return None
    if x1f < x0f:
        x0f, x1f = x1f, x0f
    return (x0f, x1f)


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    """两个区间的重叠长度（可为 0）。"""
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def _cluster_columns(
    intervals: list[tuple[float, float]],
    *,
    ratio: float = _OVERLAP_RATIO,
    min_members: int = 1,
) -> list[tuple[float, float]]:
    """把若干 x 区间聚成列：两两重叠超过 ``ratio`` 就并成一列。

    为什么用**重叠率**而不是"中心点距离"：同一列里数字的宽度随位数变化
    （``2,864,469`` 比 ``219,241`` 宽），中心点会漂移十几个点；
    但它们的 x 区间**必然大幅重叠**。而相邻两列在中文年报里几乎总有
    明显间隙 —— 重叠率为 0。重叠率因此比中心距离稳健得多。

    ``min_members > 1`` 时丢掉成员数不足的簇 —— 用于剔除"表体内期间
    碎片行"造成的伪列（见 :func:`align_grid_by_bbox` 的说明）。

    返回每列的 ``(x_left, x_right)``（该列所有成员区间的并集）。
    """
    if not intervals:
        return []
    items = sorted(intervals, key=lambda iv: (iv[0], iv[1]))
    groups: list[list[tuple[float, float]]] = [[items[0]]]
    for iv in items[1:]:
        lo = min(m[0] for m in groups[-1])
        hi = max(m[1] for m in groups[-1])
        narrower = min(hi - lo, iv[1] - iv[0])
        if narrower > 0 and _overlap((lo, hi), iv) / narrower >= ratio:
            groups[-1].append(iv)
        else:
            groups.append([iv])
    return [
        (min(m[0] for m in g), max(m[1] for m in g))
        for g in groups
        if len(g) >= min_members
    ]


def _best_col(
    cell_x: tuple[float, float], bounds: list[tuple[float, float]],
) -> int | None:
    """给一个单元格找它所属的列：**重叠最多**的那一列。

    全部无重叠时取中心最近的一列 —— 表头可能整体比数据区更靠左
    （或更靠右），此时按距离归属比丢弃它更符合事实。
    平局取**更靠左**的列：表头标签 ``2025`` 的左边界与值列的左边界
    常常恰好相等，此时它属于左边那一列。
    """
    if not bounds:
        return None
    best, best_score = 0, _overlap(cell_x, bounds[0])
    for k in range(1, len(bounds)):
        score = _overlap(cell_x, bounds[k])
        if score > best_score:
            best, best_score = k, score
    if best_score > 0:
        return best
    cx = (cell_x[0] + cell_x[1]) / 2
    return min(
        range(len(bounds)),
        key=lambda k: abs(cx - (bounds[k][0] + bounds[k][1]) / 2),
    )


# ---------------------------------------------------------------------------
# 文本处理
# ---------------------------------------------------------------------------


def _is_numeric_text(text: str) -> bool:
    """这个格是不是"一个数"（含被拆开的括号/百分号形态）。"""
    return bool(
        re.fullmatch(r"[\s\-−–—()（）]*[\d,]+(?:\.\d+)?[\s%％)）]*", text or "")
    )


def _whole_number(text: str) -> bool:
    """是不是一个**完整的**数值（不是 ``(`` 或 ``)`` 这种碎片）。

    判据：去掉括号/符号/空白后至少 4 个字符 —— 中文年报里最小的
    "完整数值"（``0.86``、``(2.5)``）都满足，而纯符号碎片不满足。
    """
    core = re.sub(r"[\s\-−–—()（）%％]", "", text or "")
    return len(core) >= 4 and core.replace(",", "").replace(".", "").isdigit()


def _join_fragments(parts: list[str]) -> str:
    """把同一列内的多个碎片拼成一个标签。

    两侧都是 CJK 时**紧贴**（``项`` + ``目`` → ``项目``），否则加空格
    （``2025`` + ``年`` → ``2025 年``）。

    ⚠️ 汉字与数字之间**要留空格**：期间解析靠正则 ``20\\d{2}\\s*年?``，
    加不加空格都能过；但 ``12 月 31 日`` 比 ``12月31日`` 更容易被
    ``table_facts._is_data_number`` 之类的判据识别为"日期碎片"。
    """
    out = ""
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        if not out:
            out = p
            continue
        a, b = out[-1], p[0]
        a_cjk = "\u4e00" <= a <= "\u9fff"
        b_cjk = "\u4e00" <= b <= "\u9fff"
        out += ("" if (a_cjk and b_cjk) else " ") + p
    return out


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def align_grid_by_bbox(
    grid: list[list[str]],
    col_maps: list[list[int]],
    cells: list[dict[str, Any]] | None,
    *,
    data_start: int = 0,
    min_value_cols: int = _MIN_VALUE_COLS,
) -> AlignedGrid:
    """按单元格 x 坐标重建网格的列结构。

    参数
    ----
    grid
        ``merge_fragments`` 之后的网格（仅用于取行列规模与兜底）。
    col_maps
        与 ``grid`` 同形，每个单元记录它来自哪些**原始列号**。
    cells
        原始单元格记录（须含 ``row`` / ``col`` / ``bbox`` / ``text``）。
    data_start
        数据起始行。列边界**只从数据行**推断。

    返回 :class:`AlignedGrid`；未对齐时其 ``grid`` 与入参逐格相同。
    """
    noop = AlignedGrid(grid=grid, col_maps=col_maps, reason="未启用")
    if not grid or not cells:
        noop.reason = "没有单元格坐标"
        return noop

    n_rows = len(grid)

    # ★ 先把单元格文本做**折行修复**，与网格保持同一口径。
    #
    #   实测踩过（002716 的分部合同表 p170/p203）：PDF 把数字按行折断了，
    #   网格里的值是修复过的 ``'12,371,785,621.02'``，而 ``cells`` 里
    #   仍是原始形态 ``'12,371,78\n5,621.02'``。`_is_numeric_text` 用的是
    #   ``fullmatch`` 且不含 ``\n``，于是**一个数字格都认不出来**：
    #
    #       全表数字格 = 0 个    →    数值列 0 列    →    整表放弃对齐
    #
    #   后果：那两张分部表的合计口径取不到（`seg_total_col=None`），
    #   整表在 R4 侧完全用不上。这是"调用方与工具口径不一致"的又一个实例 ——
    #   与 `tools/year_sequence_report.py` 那次同类。
    #
    #   做法：生成**浅拷贝**并替换 text，不改动调用方的字典
    #   （调用方后续还要用原始 cells 取 bbox 与文本）。
    cells = [_with_repaired_text(c) for c in cells]

    # ---- ① 取数据行的单元格坐标 ----
    data_cells = [
        c for c in cells
        if data_start <= int(c.get("row", -1)) < n_rows
        and str(c.get("text") or "").strip()
    ]
    if not data_cells:
        noop.reason = "数据区没有带坐标的单元格"
        return noop

    located: list[tuple[dict[str, Any], tuple[float, float]]] = []
    for c in data_cells:
        iv = _x_interval(c.get("bbox"))
        if iv:
            located.append((c, iv))
    if len(located) < len(data_cells) * 0.9:
        noop.reason = "单元格普遍缺少 bbox"
        return noop

    # 全表带坐标的格 —— 列结构从**全表**的数值格推断，不依赖 data_start
    # （见下方 ② 的说明）。
    located_all: list[tuple[dict[str, Any], tuple[float, float]]] = []
    for c in cells:
        if int(c.get("row", -1)) >= n_rows or not str(c.get("text") or "").strip():
            continue
        iv = _x_interval(c.get("bbox"))
        if iv:
            located_all.append((c, iv))
    if not located_all:
        noop.reason = "全表没有带坐标的单元格"
        return noop

    # ---- ② 数值列：**全表**的数值格聚成列 ----
    #
    # ★ 用全表而不是只用 `data_start` 之后的行（2026-09 修正）。
    #
    #   原实现只看数据行，于是**依赖 `data_start` 判对**。而 `data_start`
    #   是从网格猜的，会猜错 —— 实测 601857 p178 表 1（资本性支出所在表，
    #   没有自己的表头）的 `data_start_row` 返回 **1**，把
    #   ``折旧、折耗和摊销费用 | 186,238 | …`` 这一整行当成了表头，
    #   于是标签列判不出来、数值列被过度切分（7 列被对齐成 10 列）。
    #
    #   数值格的 x 布局本身就是列结构，**不需要知道哪一行是数据行**。
    #   改用全表之后，表头行里的年份碎片顶多多出几个窄簇，
    #   由 `_OVERLAP_RATIO = 0.8` 的同列重叠判据吸收。
    num_ivs = [
        iv for c, iv in located_all
        if _is_numeric_text(str(c.get("text") or ""))
    ]
    # 只保留**至少两格**的列簇。
    #
    # 为什么必须过滤（实测 601857 p178 表 1）：中文报表会把期间印在
    # **表体内的碎片行**上 —— ``2025 | 年 | 12 | 月 | 31 | 日``。
    # 这些碎片都是"数值"（``2025``/``12``/``31``），各自只出现一次，
    # 于是聚成 3 个**单成员**列簇，把一张 6 列的表撑成 9 列：
    #
    #   列0 '2025'(r4) / 列1 '12'(r4) / 列2 '31'(r4) / 列3..8 真数据列
    #
    # 而真正的数据列在表内至少出现两次（本表每列 5 个值）。
    # 零值列（如 p178 表 1 的 ``-`` 列、002716 分部表的空分部列）
    # 不受影响：被丢的是"只出现一次"的簇，不是"值为零"的列。
    value_bounds = _cluster_columns(num_ivs, min_members=2)
    if len(value_bounds) < min_value_cols:
        # 退回不过滤 —— 表很短时（只有一行数据）真列也可能只出现一次
        value_bounds = _cluster_columns(num_ivs)
    if len(value_bounds) < min_value_cols:
        noop.reason = f"数值列仅 {len(value_bounds)} 列，不足以构成报表"
        return noop

    # ---- ③ 行标签列：**不与任何数值列重叠**的非数值格 ----
    #
    #   判据是"不与数值列重叠"，**不是"与最左边界等距"**。
    #
    #   实测踩过两次，都是这条判据写窄了：
    #
    #   * 601857 p176（现金流量表）：行标签缩进不一致 —— ``净利润`` 在
    #     x=86，而 ``固定资产折旧、油气资产折耗`` 在 x=107（缩进 20pt）。
    #     按"等于最左边界"判，后者不算标签 → 被按 x 归到**第一个数值列**，
    #     于是它与 ``215,818`` 挤进同一格
    #     （``'固定资产折旧、油气资产折耗 215,818'``）。
    #     后果：现金流量表的 D&A **整条读不出来**，"折旧摊销占收入比"
    #     从事实值退回模板值 —— 而且不报错。
    #   * 601857 p7（主要财务数据）：标签恰好都在 x=70.22，所以按等距判也能过。
    #
    #   正确判据：**非数值、且 x 区间不与任何数值列重叠** → 属于标签列。
    #   中文报表的行标签一律左对齐且都在数值区左侧，这条判据对缩进深度不敏感。
    label_cells = [
        (c, iv) for c, iv in located_all
        if not _is_numeric_text(str(c.get("text") or ""))
        and not any(_overlap(iv, b) > 0 for b in value_bounds)
    ]
    if not label_cells:
        label_bounds: list[tuple[float, float]] = []
    else:
        label_bounds = _cluster_columns([iv for _, iv in label_cells])

    # ---- ④ 列边界必须有序且互不重叠 ----
    #
    #   ⚠️ 标签列的**右边界必须截到第一个数值列之前**。
    #   实测（601857 p7）：标签列由 ``营业收入``/``所得税费用`` 等格聚成，
    #   右边界是它们 x1 的最大值 115.33；而第一个数值 ``2,864,469``
    #   的 x0 是 304.73 —— 本不冲突。但**行标签的缩进深度不一**
    #   （``其中：…`` 比 ``…合计`` 靠右），聚出来的标签区间会与第一个数值列
    #   真实重叠，于是被这条检查拒绝。实测 987 张表里有 **122 张**
    #   （占全部拒绝原因的三分之一）栽在这里，而它们本来是可以对齐的。
    #   截断不引入新风险：标签列的**唯一用途**就是承载 ``row[0]``，
    #   它的右边界在哪对归属判定没有影响（标签格走强制归属，见 ⑤）。
    bounds = list(value_bounds)
    if label_bounds:
        lbl = list(label_bounds[0])
        lbl[1] = min(lbl[1], bounds[0][0] - 1.0)
        bounds = [tuple(lbl)] + bounds  # type: ignore[list-item]
    for k in range(1, len(bounds)):
        if bounds[k][0] < bounds[k - 1][1]:
            noop.reason = f"第 {k} 列与前一代列边界重叠，放弃对齐"
            return noop

    label_idx = 0 if label_bounds else None
    value_offset = 1 if label_bounds else 0

    # ---- ⑤ 每个原始格归属到列 ----
    #   label_cells 里的格**强制**进标签列：它们的左边界差异很大
    #   （``所得税费用`` 比 ``净利润`` 宽），按最近中心判会漂到数值列去。
    label_ids = {id(c) for c, _ in label_cells}
    picked: dict[tuple[int, int], list[tuple[str, int]]] = {}
    for c, iv in located:
        r, j = int(c.get("row", -1)), int(c.get("col", -1))
        if id(c) in label_ids and label_idx is not None:
            k = label_idx
        else:
            k = _best_col(iv, value_bounds)
            if k is None:
                continue
            k += value_offset
        picked.setdefault((r, k), []).append((str(c.get("text") or ""), j))

    # 表头行也必须归属（否则真表头仍然贴着错误的列号）
    for c in cells:
        r = int(c.get("row", -1))
        if r < 0 or r >= data_start or r >= n_rows:
            continue
        text = str(c.get("text") or "").strip()
        if not text:
            continue
        iv = _x_interval(c.get("bbox"))
        if iv is None:
            continue
        j = int(c.get("col", -1))
        if label_idx is not None and not _is_numeric_text(text):
            # 表头里的 ``项``/``目``/``项目`` 属于标签列
            if _overlap(iv, bounds[label_idx]) > 0 and not any(
                _overlap(iv, b) > _overlap(iv, bounds[label_idx])
                for b in value_bounds
            ):
                picked.setdefault((r, label_idx), []).append((text, j))
                continue
        k = _best_col(iv, value_bounds)
        if k is None:
            continue
        picked.setdefault((r, k + value_offset), []).append((text, j))

    # ---- ⑥ 安全检查：同一格里不得出现两个**完整**数值 ----
    for (r, k), items in picked.items():
        wholes = [t for t, _ in items if _whole_number(t)]
        if len(wholes) >= 2:
            noop.reason = (
                f"第 {r} 行第 {k} 列内出现 {len(wholes)} 个完整数值 "
                f"{wholes[:3]}，列边界判定有误，放弃对齐"
            )
            return noop

    # ---- ⑦ ★ 不丢值检查（2026-09 第十三轮的教训，必须有）----
    #
    #   重建列结构**只许重新排列单元格，不许丢内容**。这是本模块
    #   "只能让结果变好"那句承诺的**可验证形式** —— 在此之前它只是注释。
    #
    #   实测踩过（601857 p176 现金流量表）：
    #
    #       原始 r4:  固定资产折旧、油气资产折耗 | 215,818 | 210,990 | 136,699 | 133,940
    #       对齐后 r4:  （空）                  | 固定资产折旧、油气资产折耗
    #                   | 210,990 | 136,699 | 133,940
    #
    #   ``215,818`` **被挤掉了**（标签占了第 1 列，四个值只剩三个位置）。
    #   后果：现金流量表的 D&A 整条抽不到，"折旧摊销占收入比"从事实值
    #   退回模板值；而**不会报错**（少一条事实看起来像"这家公司没披露"）。
    #
    #   判据：每个数值格都必须在新网格里出现。丢一个就整体放弃对齐 ——
    #   退回原行为，宁可列号不准也不能少数据。
    if _drops_values(picked, located):
        noop.reason = "对齐会丢失数值格，放弃对齐（宁可列号不准，不可少数据）"
        return noop

    # ---- ⑦ 重建网格 ----
    new_grid: list[list[str]] = []
    new_maps: list[list[int]] = []
    for r in range(n_rows):
        row_texts: list[str] = []
        row_map: list[list[int]] = []
        for k in range(len(bounds)):
            items = sorted(picked.get((r, k), []), key=lambda t: t[1])
            row_texts.append(_join_fragments([t for t, _ in items]))
            row_map.append([j for _, j in items])
        new_grid.append(row_texts)
        new_maps.append(row_map)

    total_cols = detect_segment_total_col(new_grid, data_start=data_start)
    return AlignedGrid(
        grid=new_grid, col_maps=new_maps, applied=True,
        reason=f"按单元格坐标重建 {len(bounds)} 列（含行标签列）",
        n_cols=len(bounds), col_bounds=bounds,
        segment_total_cols=(
            frozenset({total_cols}) if isinstance(total_cols, int)
            else frozenset(total_cols or ())
        ),
    )


__all__ = [
    "AlignedGrid",
    "align_grid_by_bbox",
    "detect_segment_total_col",
    "remap_col_via_maps",
]
