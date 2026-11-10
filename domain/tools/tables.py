"""表格解析 —— 赛题「表格解析准确性」考察点的落点。

**三引擎表决策略**（方案 2.5 节）：

    通路 1  文本层坐标聚类   电子版 PDF 的表格（最高置信）
    通路 2  几何线段重建     有框线表格（精确、可靠、快）
    通路 3  VLM 读图         无框线/扫描件表格（需裁决）

三种结果两两比对：>=2 路一致 -> 高置信；分歧 -> 交 VLM 裁决；
仍不确定 -> 标 ``needs_review`` **并从事实库排除**（宁缺勿错）。

最关键的校验是**算术校验**：合计行/列必须等于分项和。
算不平就说明解析错了，而且能精确定位到具体单元格 ——
这是纯代码的确定性判断，不依赖任何模型。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agent.evidence.models import BBox
from agent.runtime.errors import HarnessError
from agent.tools.registry import tool
from agent.validate.numeric import approx_equal, parse_decimal

try:
    import fitz

    _HAS_FITZ = True
except ImportError:  # pragma: no cover
    fitz = None  # type: ignore[assignment]
    _HAS_FITZ = False


# ==========================================================================
# 数据结构
# ==========================================================================


@dataclass
class TableCell:
    row: int
    col: int
    bbox: BBox
    text: str = ""
    confidence: float = 0.0
    source_channel: str = ""

    # —— 单元格级 VLM 裁决的痕迹（由 adjudicate_table 回填）——
    original_text: str = ""
    """裁决前的读数。用于在报告里展示「改了什么」。"""
    adjudicated: bool = False
    vlm_text: str | None = None
    vlm_confidence: float | None = None
    vlm_quote: str | None = None
    verdict: str = ""
    """``agree`` / ``corrected`` / ``unreadable`` / ``not_checked``。"""
    note: str = ""

    @property
    def is_numeric(self) -> bool:
        return bool(re.search(r"\d", self.text))

    def to_dict(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "col": self.col,
            "bbox": self.bbox.model_dump(),
            "text": self.text,
            "confidence": round(self.confidence, 4),
            "source_channel": self.source_channel,
            "original_text": self.original_text,
            "adjudicated": self.adjudicated,
            "vlm_text": self.vlm_text,
            "vlm_confidence": self.vlm_confidence,
            "vlm_quote": self.vlm_quote,
            "verdict": self.verdict,
            "note": self.note,
        }


@dataclass
class Table:
    """一个已识别的表格。"""

    page: int
    bbox: BBox
    n_rows: int
    n_cols: int
    cells: list[TableCell] = field(default_factory=list)
    method: str = ""
    has_rules: bool = False

    def grid(self) -> list[list[str]]:
        g = [["" for _ in range(self.n_cols)] for _ in range(self.n_rows)]
        for c in self.cells:
            if 0 <= c.row < self.n_rows and 0 <= c.col < self.n_cols:
                g[c.row][c.col] = c.text
        return g

    def cell(self, row: int, col: int) -> TableCell | None:
        for c in self.cells:
            if c.row == row and c.col == col:
                return c
        return None

    # ------------------------------------------------------------ 单元格裁决

    def header_row(self) -> list[str]:
        """第一行（表头）。多数公告表格的第一行都是表头。"""
        return self.grid()[0] if self.n_rows else []

    def numeric_cells(self) -> list[TableCell]:
        return [c for c in self.cells if c.is_numeric and c.text.strip()]

    def suspicious_cells(self, *, limit: int = 8) -> list[TableCell]:
        """挑出**最值得复核**的单元格。

        优先级（实测确定）：
            1. 参与算术不平的行/列 —— 算术不平本身就说明那里有错
            2. 数字被换行切断的单元格（如 ``'583,96\\n5.66'``）
            3. 单格字符数异常（相邻格被合并）
            4. 空单元格（可能漏读）

        只取前 ``limit`` 个，控制 VLM 调用成本。
        """
        scored: list[tuple[int, TableCell]] = []

        # 1) 算术不平涉及的行列
        try:
            chk = check_table_arithmetic(self)
            hot: set[tuple[int, int]] = set()
            for f in chk.failures:
                if f.get("kind") == "row_sum":
                    r = int(f["row"])
                    for c in range(self.n_cols):
                        hot.add((r, c))
                elif f.get("kind") == "col_sum":
                    c = int(f["col"])
                    for r in range(self.n_rows):
                        hot.add((r, c))
            for (r, c) in hot:
                cell = self.cell(r, c)
                if cell is not None:
                    scored.append((0, cell))
        except Exception:
            pass

        for c in self.cells:
            if not c.text.strip():
                scored.append((3, c))                      # 空单元格
            elif re.search(r"\d\s*\n\s*\d", c.text) or re.search(r",\n\d", c.text):
                scored.append((1, c))                      # 数字被换行切断
            elif len(c.text) > 60:
                scored.append((2, c))                      # 异常长（可能合并）
            elif c.is_numeric:
                scored.append((4, c))                      # 普通数值格

        scored.sort(key=lambda t: (t[0], t[1].row, t[1].col))
        out: list[TableCell] = []
        seen: set[tuple[int, int]] = set()
        for _, cell in scored:
            key = (cell.row, cell.col)
            if key in seen:
                continue
            seen.add(key)
            out.append(cell)
            if len(out) >= limit:
                break
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "bbox": self.bbox.model_dump(),
            "n_rows": self.n_rows,
            "n_cols": self.n_cols,
            "method": self.method,
            "has_rules": self.has_rules,
            "grid": self.grid(),
            "cells": [c.to_dict() for c in self.cells],
        }


# ==========================================================================
# 通路 1：文本层坐标聚类
# ==========================================================================


def cluster_text_to_rows(
    items: list[tuple[str, BBox]], *, y_tol: float = 3.0
) -> list[list[tuple[str, BBox]]]:
    """把带坐标的文本片段按 y 坐标聚成行，行内按 x 排序。

    这是无框线表格的低成本重建方式：财报附注表格即使没有框线，
    文本层的坐标信息也足以恢复行列结构。
    """
    if not items:
        return []
    ordered = sorted(items, key=lambda it: (it[1].y0, it[1].x0))
    rows: list[list[tuple[str, BBox]]] = []
    current: list[tuple[str, BBox]] = [ordered[0]]
    current_y = ordered[0][1].y0

    for text, bbox in ordered[1:]:
        if abs(bbox.y0 - current_y) <= y_tol:
            current.append((text, bbox))
        else:
            rows.append(sorted(current, key=lambda it: it[1].x0))
            current = [(text, bbox)]
            current_y = bbox.y0
    rows.append(sorted(current, key=lambda it: it[1].x0))
    return rows


def extract_table_via_text_layer(
    page: "fitz.Page", *, page_no: int, min_cols: int = 3, y_tol: float = 3.0
) -> list[Table]:
    """用文本层坐标聚类识别表格候选。

    判据：连续多行的列数一致且 >= ``min_cols``，且行内 x 位置有明显间隔。
    """
    words: list[tuple[str, BBox]] = []
    for blk in page.get_text("dict").get("blocks", []):
        if blk.get("type") != 0:
            continue
        for line in blk.get("lines", []):
            for span in line.get("spans", []):
                txt = span.get("text", "").strip()
                if not txt:
                    continue
                b = span.get("bbox")
                words.append((txt, BBox(x0=b[0], y0=b[1], x1=b[2], y1=b[3])))

    rows = cluster_text_to_rows(words, y_tol=y_tol)
    if len(rows) < 3:
        return []

    # 找连续的行块，其列数稳定且 >= min_cols
    tables: list[Table] = []
    run: list[list[tuple[str, BBox]]] = []

    def flush() -> None:
        if len(run) >= 3:
            n_cols = max(len(r) for r in run)
            if n_cols >= min_cols:
                tables.append(_rows_to_table(run, page_no=page_no, n_cols=n_cols, method="text_layer_cluster"))
        run.clear()

    for r in rows:
        if len(r) >= min_cols:
            run.append(r)
        else:
            flush()
    flush()
    return tables


def _rows_to_table(
    rows: list[list[tuple[str, BBox]]],
    *,
    page_no: int,
    n_cols: int,
    method: str,
) -> Table:
    cells: list[TableCell] = []
    all_boxes: list[BBox] = []
    for i, r in enumerate(rows):
        for j, (text, bbox) in enumerate(r):
            all_boxes.append(bbox)
            cells.append(
                TableCell(
                    row=i,
                    col=j,
                    bbox=bbox,
                    text=text,
                    confidence=0.95,  # 文本层直取
                    source_channel="text_layer",
                )
            )
    outer = all_boxes[0]
    for b in all_boxes[1:]:
        outer = outer.union(b)
    return Table(
        page=page_no,
        bbox=outer,
        n_rows=len(rows),
        n_cols=n_cols,
        cells=cells,
        method=method,
        has_rules=False,
    )


# ==========================================================================
# 通路 2：几何线段重建（有框线表格）
# ==========================================================================

# 中文财报/公告表格常见的表头关键词，用于识别表头行
_HEADER_HINTS = ("项目", "科目", "名称", "年度", "本期", "上期", "本期发生额", "上期发生额")


def _cluster_positions(values: list[float], tol: float = 2.0) -> list[float]:
    if not values:
        return []
    vals = sorted(values)
    out = [vals[0]]
    for v in vals[1:]:
        if abs(v - out[-1]) <= tol:
            out[-1] = (out[-1] + v) / 2.0
        else:
            out.append(v)
    return out


def _extract_segments(
    page: "fitz.Page", *, min_len: float = 30.0
) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    """抽取页面上的水平/垂直线段。

    Returns:
        ``(h_segments, v_segments)``，每个元素为 ``(位置, 起点, 终点)``：
        水平线段是 ``(y, x0, x1)``，垂直线段是 ``(x, y0, y1)``。
    """
    hs: list[tuple[float, float, float]] = []
    vs: list[tuple[float, float, float]] = []

    for d in page.get_drawings():
        for item in d.get("items", []):
            if item[0] == "l":
                p1, p2 = item[1], item[2]
                dx, dy = abs(p2.x - p1.x), abs(p2.y - p1.y)
                if dx >= min_len and dy <= 2.0:
                    hs.append(((p1.y + p2.y) / 2, min(p1.x, p2.x), max(p1.x, p2.x)))
                elif dy >= min_len and dx <= 2.0:
                    vs.append(((p1.x + p2.x) / 2, min(p1.y, p2.y), max(p1.y, p2.y)))
            elif item[0] == "re":
                r = item[1]
                if r.width >= min_len:
                    hs.append((r.y0, r.x0, r.x1))
                    hs.append((r.y1, r.x0, r.x1))
                if r.height >= min_len:
                    vs.append((r.x0, r.y0, r.y1))
                    vs.append((r.x1, r.y0, r.y1))
    return hs, vs


def _find_table_regions(
    hs: list[tuple[float, float, float]],
    vs: list[tuple[float, float, float]],
    *,
    tol: float = 4.0,
    min_rows: int = 2,
    min_cols: int = 2,
) -> list[BBox]:
    """用线段的**连通分量**找出页面上每一个独立的表格区域。

    ★ 这是修掉「多个表格被合并成一个网格」的关键。

    为什么必须这样：早期实现把**全页所有线段**收集起来，
    取其凸包当作唯一表格的边界，然后在其中铺一个网格。
    结果页面上若有多个表格（公告里很常见），
    它们的线段会被混在一起 → 网格列数暴涨（实测见过 9 行 × 25 列，
    其中 15 列全是空的）→ 文本落进错误的列 → 结构完全不可用。

    ## 连通判据（试了两版）

    第一版用「**端点严格相接**」—— 太严。实测发现很多表格的框线是
    **分段绘制**的（每条边一段，端点并不精确相接），于是大量表格
    判不出区域，整体退化成较弱的 ``text_layer_cluster`` 路径，
    反而比修复前更差。

    现版用「**近似共线 + 投影重叠**」：
      横线 a(y_a, x0_a, x1_a) 与 横线 b 连通 ⟺ |y_a − y_b| ≤ tol 且 x 区间重叠
      竖线同理；横线与竖线则在**交叉点**处连通（端点靠近对方线段即可）。
    这能容忍分段绘制，同时仍然按空间位置把不同表格分开。
    """
    n = len(hs) + len(vs)
    if n == 0:
        return []
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    def span_overlap(a0: float, a1: float, b0: float, b1: float, pad: float = 2.0) -> bool:
        return not (a1 < b0 - pad or b1 < a0 - pad)

    # —— 同向线段：近似共线 + 投影重叠 ——
    for i in range(len(hs)):
        yi, xi0, xi1 = hs[i]
        for j in range(i + 1, len(hs)):
            yj, xj0, xj1 = hs[j]
            if abs(yi - yj) <= tol and span_overlap(xi0, xi1, xj0, xj1):
                union(i, j)
    for i in range(len(vs)):
        xi, yi0, yi1 = vs[i]
        for j in range(i + 1, len(vs)):
            xj, yj0, yj1 = vs[j]
            if abs(xi - xj) <= tol and span_overlap(yi0, yi1, yj0, yj1):
                union(len(hs) + i, len(hs) + j)

    # —— 横竖之间：交叉点处连通 ——
    # 用网格索引避免 O(n²)：只比较可能相交的线段
    CELL = 48.0
    grid: dict[tuple[int, int], list[int]] = {}
    for j, (x, y0, y1) in enumerate(vs):
        for ty in range(int(y0 // CELL), int(y1 // CELL) + 1):
            grid.setdefault((int(x // CELL), ty), []).append(j)

    for i, (y, x0, x1) in enumerate(hs):
        seen: set[int] = set()
        for tx in range(int(x0 // CELL), int(x1 // CELL) + 2):
            # 垂直线可能在相邻列
            for dx in (0, -1, 1):
                for j in grid.get((tx + dx, int(y // CELL)), ()):  # type: ignore[arg-type]
                    if j in seen:
                        continue
                    seen.add(j)
                    x, vy0, vy1 = vs[j]
                    if (x0 - tol) <= x <= (x1 + tol) and (vy0 - tol) <= y <= (vy1 + tol):
                        union(i, len(hs) + j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    regions: list[BBox] = []
    for members in groups.values():
        h_idx = [i for i in members if i < len(hs)]
        v_idx = [i - len(hs) for i in members if i >= len(hs)]
        # 一个真正的表格区域必须同时有足够的横线与竖线
        if len({round(hs[i][0], 1) for i in h_idx}) < min_rows + 1:
            continue
        if len({round(vs[j][0], 1) for j in v_idx}) < min_cols + 1:
            continue
        xs_all: list[float] = []
        ys_all: list[float] = []
        for i in h_idx:
            y, x0, x1 = hs[i]
            ys_all.append(y)
            xs_all += [x0, x1]
        for j in v_idx:
            x, y0, y1 = vs[j]
            xs_all.append(x)
            ys_all += [y0, y1]
        regions.append(
            BBox(x0=min(xs_all), y0=min(ys_all), x1=max(xs_all), y1=max(ys_all))
        )

    # 顶部到底部排序，保证输出顺序稳定（可复现性要求）
    regions.sort(key=lambda b: (round(b.y0, 1), round(b.x0, 1)))
    return regions


def _fallback_region(
    hs: list[tuple[float, float, float]],
    vs: list[tuple[float, float, float]],
    *,
    min_rows: int,
    min_cols: int,
) -> list[BBox]:
    """连通分量判不出区域时的兜底：用全页线段凸包当作一个表格。

    保留这条路径很重要 —— 宁可退回旧行为，也不要因为新判据太严
    而把整页表格全部丢掉（实测发生过：修复后 ``rule_line_grid``
    的触发率反而下降，因为分段绘制的框线端点并不相接）。
    """
    h_unique = _cluster_positions([y for (y, _, _) in hs])
    v_unique = _cluster_positions([x for (x, _, _) in vs])
    if len(h_unique) < min_rows + 1 or len(v_unique) < min_cols + 1:
        return []
    xs = [x for (_, x0, x1) in hs for x in (x0, x1)] + [x for (x, _, _) in vs]
    ys = [y for (y, _, _) in hs] + [y for (_, y0, y1) in vs for y in (y0, y1)]
    return [BBox(x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys))]


def extract_table_via_rules(
    page: "fitz.Page", *, page_no: int, min_rows: int = 2, min_cols: int = 2, min_len: float = 30.0
) -> list[Table]:
    """用水平/垂直线段重建单元格网格。仅适用于**有框线**表格。

    优势：单元格边界是精确的，不需要任何模型推断，
    因此结果置信度高于坐标聚类与 VLM 读图。

    ★ 支持**一页多个表格**：先用线段连通分量找出每个表格区域，
    再对每个区域单独铺网格。早期实现只取全页线段的凸包，
    会把多个表格合并成一个（实测见过 9×25 的空壳网格）。
    """
    hs, vs = _extract_segments(page, min_len=min_len)
    if not hs or not vs:
        return []

    regions = _find_table_regions(
        hs, vs, min_rows=min_rows, min_cols=min_cols
    )
    if not regions:
        # 连通分量为空 → 退回全页凸包（保住旧行为，避免整页表格被丢弃）
        regions = _fallback_region(hs, vs, min_rows=min_rows, min_cols=min_cols)
    if not regions:
        return []

    spans = _collect_spans(page)
    tables: list[Table] = []

    for region in regions:
        # 只取**落在此区域内**的线段 —— 这是与"全页凸包"做法的根本区别
        h = _cluster_positions(
            [y for (y, x0, x1) in hs
             if region.y0 - 2 <= y <= region.y1 + 2
             and x0 >= region.x0 - 2 and x1 <= region.x1 + 2]
        )
        v = _cluster_positions(
            [x for (x, y0, y1) in vs
             if region.x0 - 2 <= x <= region.x1 + 2
             and y0 >= region.y0 - 2 and y1 <= region.y1 + 2]
        )
        if len(h) < min_rows + 1 or len(v) < min_cols + 1:
            continue

        h = _prune_sparse_lines(h, spans, axis="h", region=region)
        v = _prune_sparse_lines(v, spans, axis="v", region=region)
        if len(h) < min_rows + 1 or len(v) < min_cols + 1:
            continue

        n_rows, n_cols = len(h) - 1, len(v) - 1
        cells: list[TableCell] = []
        for i in range(n_rows):
            for j in range(n_cols):
                bbox = BBox(x0=v[j], y0=h[i], x1=v[j + 1], y1=h[i + 1])
                text = _text_in_box(spans, bbox)
                cells.append(
                    TableCell(
                        row=i, col=j, bbox=bbox, text=text,
                        confidence=0.95 if text else 0.0,
                        source_channel="rule_lines",
                    )
                )

        tables.append(
            Table(
                page=page_no,
                bbox=region,
                n_rows=n_rows,
                n_cols=n_cols,
                cells=cells,
                method="rule_line_grid",
                has_rules=True,
            )
        )

    return tables


def _prune_sparse_lines(
    positions: list[float],
    spans: list[tuple[str, BBox]],
    *,
    axis: str,
    region: BBox,
    min_fill: float = 0.02,
) -> list[float]:
    """剔除内部几乎不含文本的分隔线。

    为什么需要：几何重建偶尔会捡到装饰线、页边框或相邻表格的边线，
    它们会在网格里制造出整列/整行全空的格子。
    实测见过 9 行 × 25 列而右侧 15 列全空的网格 —— 那些空列并非真实存在。

    判据：相邻两条线之间若在所有行/列上都没有文本，则这条线是多余的。
    """
    if len(positions) < 4:
        return positions

    keep = [positions[0]]
    for k in range(1, len(positions) - 1):
        lo, hi = positions[k - 1], positions[k + 1]
        filled = 0
        total = 0
        for (text, b) in spans:
            if not text.strip():
                continue
            if axis == "v":
                cx, cy = (b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2
                if region.y0 <= cy <= region.y1 and lo <= cx <= hi:
                    filled += len(text)
                    total = max(total, 1)
            else:
                cx, cy = (b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2
                if region.x0 <= cx <= region.x1 and lo <= cy <= hi:
                    filled += len(text)
                    total = max(total, 1)
        span_size = max(1.0, hi - lo)
        # 文本密度过低 → 这条线是多余的
        other = (region.y1 - region.y0) if axis == "v" else (region.x1 - region.x0)
        density = filled / max(1.0, span_size * max(1.0, other) / 1000.0)
        if filled > 0 and density >= min_fill:
            keep.append(positions[k])
    keep.append(positions[-1])

    # 去重后返回
    out: list[float] = []
    for p in keep:
        if not out or abs(p - out[-1]) > 1.0:
            out.append(p)
    return out


def _collect_spans(page: "fitz.Page") -> list[tuple[str, BBox]]:
    out: list[tuple[str, BBox]] = []
    for blk in page.get_text("dict").get("blocks", []):
        if blk.get("type") != 0:
            continue
        for line in blk.get("lines", []):
            for span in line.get("spans", []):
                txt = span.get("text", "").strip()
                if txt:
                    b = span.get("bbox")
                    out.append((txt, BBox(x0=b[0], y0=b[1], x1=b[2], y1=b[3])))
    return out


def _text_in_box(spans: list[tuple[str, BBox]], box: BBox) -> str:
    """取落在单元格内的文本，**按视觉行智能拼接**。

    ## 为什么需要「智能拼接」

    朴素做法是把格内所有 span 用空格连起来。但单元格里的文字常常
    **换行**（列宽不够时表格会自动折行），例如::

        唐人神集团股
        份有限公司            → 朴素拼接得 "唐人神集团股 份有限公司"（中间多个空格）
        100%                 → 得 "100%"（正常）

    空格一插进去，「唐人神集团股份有限公司」就再也匹配不上任何别名，
    而且「3,000」被折成「3,0 00」时数字直接损坏。
    实测：这是结构完整率只有 53.6% 的主要原因之一。

    ## 拼接规则

    * 同一视觉行内的 span：**直接相连**（表格里相邻 span 通常是同一串文字被切碎）
    * 不同视觉行之间：**换行符**分隔，且**只有当两侧都是中日韩字符时才不加空格**；
      若任一侧是数字/字母，加不加空格都可能出错，因此保留换行符交给下游
      （下游按 aliases 匹配时不关心换行，但数字 token 化会因此正确断词）
    """
    hits: list[tuple[str, BBox]] = []
    for txt, sb in spans:
        if not txt.strip():
            continue
        center_x = (sb.x0 + sb.x1) / 2
        center_y = (sb.y0 + sb.y1) / 2
        if box.x0 - 1 <= center_x <= box.x1 + 1 and box.y0 - 1 <= center_y <= box.y1 + 1:
            hits.append((txt, sb))
    if not hits:
        return ""

    hits.sort(key=lambda it: (round(it[1].y0, 1), it[1].x0))

    # 按视觉行分组（y 相近的归为一行）
    rows: list[list[tuple[str, BBox]]] = []
    for txt, sb in hits:
        if rows and abs(sb.y0 - rows[-1][0][1].y0) <= 3.0:
            rows[-1].append((txt, sb))
        else:
            rows.append([(txt, sb)])

    pieces: list[str] = []
    for row in rows:
        row.sort(key=lambda it: it[1].x0)
        line = "".join(t for t, _ in row).strip()
        if line:
            pieces.append(line)

    # 行间：中韩日之间不加空格，其余保留换行（避免破坏数字断词）
    out = ""
    for i, piece in enumerate(pieces):
        if i == 0:
            out = piece
            continue
        prev = out[-1] if out else ""
        cur = piece[0] if piece else ""
        if _is_cjk(prev) and _is_cjk(cur):
            out += piece                       # 中文换行只是排版折行，直接相连
        else:
            out += "\n" + piece                # 涉及数字/字母：保留换行，交给下游分词
    return out.strip()


def _is_cjk(ch: str) -> bool:
    return bool(ch) and (
        "\u4e00" <= ch <= "\u9fff"
        or "\u3400" <= ch <= "\u4dbf"
        or ch in "，。、；：（）《》【】“”‘’"
    )


def tables_of_page(page: "fitz.Page", *, page_no: int, prefer_rules: bool = True) -> list[Table]:
    """按优先级选择表格识别通路。"""
    if prefer_rules:
        by_rules = extract_table_via_rules(page, page_no=page_no)
        if by_rules:
            return by_rules
    return extract_table_via_text_layer(page, page_no=page_no)


# ==========================================================================
# 算术校验（确定性，不依赖模型）
# ==========================================================================


@dataclass
class ArithmeticCheck:
    """表格算术校验结果。定位到具体单元格，便于人工复核。"""

    ok: bool
    checked_rows: int = 0
    checked_cols: int = 0
    soft: list[dict[str, Any]] = field(default_factory=list)
    """**不判失败**的合计线：合计只覆盖一部分分项（分层小计 / 多行表头）。

    保留成单独一路是有意的 —— 放过它们是为了不误报，
    但"为什么放过"必须留痕，否则等于把信息丢掉。
    """
    failures: list[dict[str, Any]] = field(default_factory=list)

    @property
    def checked(self) -> int:
        return self.checked_rows + self.checked_cols

    @property
    def nested(self) -> int:
        return len(self.soft)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_rows": self.checked_rows,
            "checked_cols": self.checked_cols,
            "checked": self.checked,
            "nested": self.nested,
            "failure_count": len(self.failures),
            "failures": self.failures[:20],
            "soft_count": len(self.soft),
            "soft": self.soft[:10],
        }


def _to_decimal(text: str) -> Decimal | None:
    """把单元格文本转成数值。识别不了（表头、文字）返回 None。"""
    if not text:
        return None
    s = text.strip().replace(",", "").replace("，", "")
    s = s.replace("(", "-").replace("（", "-").replace(")", "").replace("）", "")
    return parse_decimal(s)


def cell_number(text: str) -> Decimal | None:
    """公开别名：单元格文本 → 数值。

    裁决逻辑要判断"这一格到底取不取得到数"，必须用**下游真正会走的那条路径**。
    ``'583,96\\n5.66'`` 数字一位不差，却因为换行被切断而解析成 ``None`` ——
    如果裁决只用"数字是否一致"来判定，这种格子会被判成 ``agree``，
    值就永远丢失了（T2 实测：55 张表有 422 个这样的碎格）。
    """
    return _to_decimal(text)


_EN_TOTAL = {"total", "totals", "sum", "subtotal", "sub-total", "grandtotal"}
_CN_TOTAL_STRONG = ("合计", "总计", "共计", "总和", "加总")
_CN_TOTAL_WEAK = ("小计",)


def total_label_kind(text: str) -> str:
    """判断一段文本是不是「合计」标签，返回 ``""`` / ``"strong"`` / ``"weak"``。

    只认**短标签**：长句里出现「合计」多半是说明文字
    （如「其中：关联交易合计金额占同类交易的比例」），不能当合计行处理。

    标签是比位置可靠得多的信号 —— 中文财报的合计行**几乎总带标签**，
    而"最后一行就是合计"这个假设经常不成立（最后一行可能是附注或下一张表的表头）。
    """
    t = re.sub(r"[\s\u3000:：,，.。、()（）\[\]【】]", "", text or "").lower()
    if not t or len(t) > 14:
        return ""
    if t in _EN_TOTAL:
        return "strong"
    if any(k in t for k in _CN_TOTAL_STRONG):
        return "strong"
    if any(k in t for k in _CN_TOTAL_WEAK):
        return "weak"
    return ""


def _nested_explains(terms: list[Decimal], total: Decimal, rel_tol: Decimal) -> int | None:
    """合计只覆盖了**一部分**分项时，返回被覆盖部分的起始下标；否则返回 ``None``。

    中文财报的分层结构::

        一、营业收入        1,000
          其中：A             600
                B             400
        合计                1,000

    ``合计`` 等于"其中"那一项，而不是全部分项之和。这类结构下不平是**正常的**，
    必须放过，否则每一张分层表都会误报。

    取**连续后缀**而不是任意子集：这样既能覆盖「其中：」的缩进层级，
    也能覆盖"多行表头里混进了 ``2024年`` 这种可被解析成数字的表头格"
    （见 :func:`total_label_kind` 上方关于表头的说明）。
    注意后缀从下标 1 起算 —— 全体分项之和（下标 0）属于"正常合计"，
    若它成立就不该走到这里。
    """
    for k in range(1, len(terms)):
        if approx_equal(sum(terms[k:], Decimal("0")), total, rel_tol=rel_tol,
                        abs_tol=Decimal("0.02")):
            return k
    return None


def cell_geometry_text(
    page_words: list[tuple[float, float, float, float, str]] | None,
    bbox: Any,
) -> str:
    """取落在某个 bbox 内的**页面上真实文字**（单词级坐标）。

    ``page_words`` 是 ``page.get_text("words")`` 的结果，
    每项形如 ``(x0, y0, x1, y1, word, ...)``。

    用途：给单元格裁决找一个**不依赖模型自述**的裁判。
    见 :func:`corroborate_by_geometry`。
    """
    if not page_words or bbox is None:
        return ""

    def coord(name: str) -> float:
        # 默认值必须**惰性**求值：BBox 是 pydantic 模型，没有 .get()
        val = getattr(bbox, name, None)
        if val is None and isinstance(bbox, dict):
            val = bbox.get(name, 0)
        return float(val or 0)

    x0, y0, x1, y1 = coord("x0"), coord("y0"), coord("x1"), coord("y1")
    out: list[str] = []
    for w in page_words:
        if len(w) < 5:
            continue
        if (w[0] >= x0 - 1 and w[2] <= x1 + 1 and w[1] >= y0 - 1 and w[3] <= y1 + 1):
            out.append(str(w[4]))
    return " ".join(out)


def corroborate_by_geometry(geometry_text: str, original: str, candidate: str) -> str:
    """用**单元格自己的几何文字**独立复核模型读数。

    返回 ``corroborated`` / ``contradicted`` / ``unknown``。

    为什么必须加这道闸门：实测一次真实运行里 184 个复核格有 80 个被"改值"，
    抽查后 **75 个是错的** —— 模型读数根本不在那个格子里，而程序读数在。
    （根因是裁图文件名不含文档 id，多文档同页同格互相覆盖，模型看的是
    另一份公告的表格。）原有的三道闸门全部失效：

    * quote 回读只校验"这段文字**在整页某处**存在"，读错格子照样通过
    * 位置确认用的是同一张错图渲染的红框，图文自洽，必然通过
    * 置信度自评恒为 0.95，没有区分度

    也就是说：**只靠模型自述的闸门等于没有闸门**。
    这里改为用一个模型无法影响的事实来判 —— 页面上那个矩形里到底印着什么字。
    """
    cand_digits = [d for d in _digit_tokens(candidate)]
    if not cand_digits:
        return "unknown"
    geom = "".join(_digit_tokens(geometry_text))
    if not geom:
        # 没有文本层（扫描件）→ 无从独立复核。**失败关闭**：不覆盖。
        return "unknown"
    orig_digits = [d for d in _digit_tokens(original)]
    cand_hit = sum(1 for d in cand_digits if d in geom) / len(cand_digits)
    orig_hit = (
        sum(1 for d in orig_digits if d in geom) / len(orig_digits) if orig_digits else 0.0
    )
    if cand_hit >= 0.99 and orig_hit < 0.99:
        return "corroborated"
    if orig_hit >= 0.99 and cand_hit < 0.99:
        return "contradicted"
    return "unknown"


def _digit_tokens(text: str) -> list[str]:
    """抽数字 token 的纯数字串（忽略千分位/小数点/单位）。"""
    return [
        re.sub(r"\D", "", m)
        for m in re.findall(r"\d[\d.,]*", text or "")
        if re.sub(r"\D", "", m)
    ]


def describe_arithmetic(arith: dict[str, Any] | None) -> tuple[str, str]:
    """把算术校验结果转成 ``(状态, 人话说明)``，状态 ∈ ``dim`` / ``ok`` / ``bad``。

    三态必须分开，因为 ``ok`` 对"**没有合计线所以没查**"的表也是 ``True``。
    界面若把这种表显示成"✓ 算术通过（0 行 / 0 列）"，
    就等于把"没查过"说成"查过且没问题" —— 这是最不该出现在
    一个以可验证为卖点的系统里的措辞。

    界面与报告都走这一个函数，避免同一个语义在两处各写一遍而后走样。
    """
    a = arith or {}
    checked = a.get("checked")
    if checked is None:
        checked = int(a.get("checked_rows", 0) or 0) + int(a.get("checked_cols", 0) or 0)
        # 老产物没有 checked 字段：有失败记录就说明确实查过
        if not checked and a.get("failure_count"):
            checked = int(a.get("failure_count") or 0)
    checked = int(checked or 0)

    if checked == 0:
        return "dim", "无从校验（表内没有合计线）"
    if a.get("ok", True):
        return "ok", f"已验算 {checked} 条合计线，全部相符"
    return "bad", f"发现 {int(a.get('failure_count', 0) or 0)} 处算术不平"


def check_table_arithmetic(
    table: Table,
    *,
    rel_tol: Decimal = Decimal("0.005"),
    min_terms: int = 2,
) -> ArithmeticCheck:
    """校验表格的合计关系，返回**定位到行列**的不平记录。

    这是纯确定性判断，是发现解析错误最有效的信号：
    数字识别错一位，合计几乎必然对不上。

    为什么重写：旧实现把"最后一行 / 最后一列"**无条件**当作合计行，
    它有两副面孔，而且都很难发现：

    * **假报**：在 48 张真实公告表格上它报出 40 处"算术不平"，
      逐条核对**全部是误报** —— 股权变动表的最后一行是另一家子公司，
      不是合计行（见 ``tools/table_arith_audit.py``）。
      把误报写进审计报告，比不报更伤可信度。
    * **漏报**：若网格第一行是「项目 / 合计」这类文本表头，旧实现要求
      整列每个格都能解析成数字，于是 ``checked_rows``/``checked_cols``
      恒为 0，函数**永远返回 OK** —— 一张 ``合计=999`` 而分项和为 ``300``
      的表被判定为"算术自洽"。

    现在改为**认标签**：合计行看行标签，合计列看表头。

    三条判定规则：

    1. **合计行**（行标签含「合计/小计/总计/Total」）：先按"该行是各列的纵向
       合计"检验，再按"该行是最右数值格的横向合计"检验。只要有任何一列纵向
       成立，就认定方向为纵向，**只报不平的那几列**（避免把纵向表误判成横向）。
    2. **合计列**（表头含「合计」）：逐行检验"该行 = 其左侧连续数值段之和"。
    3. 分项只取**紧邻的连续数值段**：这样「序号」列与文字列不会混进求和
       （``1 | 项目 | 100 | 200 | 300`` 只取 ``100+200``）。

    **只有带合计标签的表才会被校验**。这不是能力缺失而是刻意的取舍：
    没有合计线就没有独立的算术证据，"最后一行大概是合计"的猜测只会制造噪声。
    代价是覆盖率低 —— 实测 48 张真实公告表格里只有 1 张带「合计」标签，
    因此 :func:`Table.suspicious_cells` 还要靠换行切断、异常长、空格子等
    几何特征来找可疑单元格。调用方必须区分「校验通过」与「无从校验」：
    ``ok`` 对后者同样是 ``True``，要看 ``checked`` 是否大于 0。
    """
    grid = table.grid()
    check = ArithmeticCheck(ok=True)
    if not grid or not grid[0]:
        return check

    n_rows = len(grid)
    n_cols = max(len(r) for r in grid)
    grid = [list(r) + [""] * (n_cols - len(r)) for r in grid]
    nums: list[list[Decimal | None]] = [[_to_decimal(c) for c in row] for row in grid]

    # 第一行是表头（extract_table_via_rules 的约定），不计入分项：
    # 「2024年」这类表头会被 parse_decimal 解析成数字，计进分项必然不平。
    first_data = 1 if n_rows >= 3 else 0

    total_rows: dict[int, str] = {}
    for i in range(n_rows):
        if all(v is None for v in nums[i]):
            continue
        for j in range(n_cols):
            if nums[i][j] is not None:
                continue
            kind = total_label_kind(grid[i][j])
            if kind:
                total_rows[i] = kind
                break

    total_cols: dict[int, str] = {}
    for j in range(n_cols):
        for i in range(min(2, n_rows)):
            if nums[i][j] is not None:
                continue
            kind = total_label_kind(grid[i][j])
            if kind:
                total_cols[j] = kind
                break

    def run_left(i: int, j: int) -> list[Decimal]:
        """``(i, j)`` 左侧**紧邻**的连续数值段（横向合计的分项）。"""
        out: list[Decimal] = []
        k = j - 1
        while k >= 0 and nums[i][k] is not None and k not in total_cols:
            val = nums[i][k]
            assert val is not None
            out.append(val)
            k -= 1
        out.reverse()
        return out

    def run_up(i: int, j: int) -> list[Decimal]:
        """``(i, j)`` 上方**紧邻**的连续数值段（纵向合计的分项）。"""
        out: list[Decimal] = []
        k = i - 1
        while k >= first_data and nums[k][j] is not None and k not in total_rows:
            val = nums[k][j]
            assert val is not None
            out.append(val)
            k -= 1
        out.reverse()
        return out

    def label_of(i: int) -> str:
        for j in range(n_cols):
            if grid[i][j].strip() and nums[i][j] is None:
                return grid[i][j].strip()
        return grid[i][0].strip() if n_cols else ""

    def record(kind: str, total: Decimal, terms: list[Decimal], *,
               row: int, col: int, total_cell: tuple[int, int]) -> None:
        s = sum(terms, Decimal("0"))
        check.ok = False
        check.failures.append(
            {
                # row_sum = 横向不平（一行之内）；col_sum = 纵向不平（一列之内）
                "kind": kind,
                "row": row,
                "col": col,
                "label": label_of(row),
                "total_cell": [total_cell[0], total_cell[1]],
                "expected": str(total),
                "computed": str(s),
                "diff": str(s - total),
                "terms": [str(t) for t in terms],
            }
        )

    def soft_note(kind: str, total: Decimal, terms: list[Decimal], *, row: int,
                  col: int, from_idx: int) -> None:
        check.soft.append(
            {
                "kind": kind,
                "row": row,
                "col": col,
                "label": label_of(row),
                "expected": str(total),
                "full_sum": str(sum(terms, Decimal("0"))),
                "covered_from": from_idx,
                "note": "合计只覆盖部分分项（分层小计或多行表头），不判失败",
            }
        )

    # —— 规则 1：合计行 ——
    for i in sorted(total_rows):
        numeric_cols = [j for j in range(n_cols) if nums[i][j] is not None]

        vertical: list[tuple[int, Decimal, list[Decimal], bool]] = []
        for j in numeric_cols:
            total = nums[i][j]
            assert total is not None
            terms = run_up(i, j)
            if len(terms) < min_terms or (not any(terms) and total == 0):
                continue
            ok = approx_equal(sum(terms, Decimal("0")), total,
                              rel_tol=rel_tol, abs_tol=Decimal("0.02"))
            vertical.append((j, total, terms, ok))

        horizontal: tuple[int, Decimal, list[Decimal], bool] | None = None
        if len(numeric_cols) >= min_terms + 1:
            j_last = numeric_cols[-1]
            total = nums[i][j_last]
            assert total is not None
            terms = run_left(i, j_last)
            if len(terms) >= min_terms and (any(terms) or total != 0):
                horizontal = (j_last, total, terms,
                              approx_equal(sum(terms, Decimal("0")), total,
                                           rel_tol=rel_tol, abs_tol=Decimal("0.02")))

        # 有列纵向成立 → 这就是一张纵向合计表，横向不成立不算错
        if any(v[3] for v in vertical):
            for j, total, terms, ok in vertical:
                if ok:
                    check.checked_cols += 1
                    continue
                idx = _nested_explains(terms, total, rel_tol)
                if idx is not None:
                    soft_note("col_sum", total, terms, row=i, col=j, from_idx=idx)
                else:
                    record("col_sum", total, terms, row=i, col=j, total_cell=(i, j))
            continue

        # 横向成立 → 记一次行校验
        if horizontal is not None and horizontal[3]:
            check.checked_rows += 1
            continue

        # 两个方向都不成立：优先按横向判定（合计格在最右）
        if horizontal is not None:
            j_last, total, terms, _ = horizontal
            idx = _nested_explains(terms, total, rel_tol)
            if idx is not None:
                soft_note("row_sum", total, terms, row=i, col=j_last, from_idx=idx)
            else:
                record("row_sum", total, terms, row=i, col=j_last, total_cell=(i, j_last))
            continue

        for j, total, terms, _ in vertical:
            idx = _nested_explains(terms, total, rel_tol)
            if idx is not None:
                soft_note("col_sum", total, terms, row=i, col=j, from_idx=idx)
            else:
                record("col_sum", total, terms, row=i, col=j, total_cell=(i, j))

    # —— 规则 2：合计列 ——
    for j in sorted(total_cols):
        for i in range(first_data, n_rows):
            if i in total_rows:
                continue
            total = nums[i][j]
            if total is None:
                continue
            terms = run_left(i, j)
            if len(terms) < min_terms or (not any(terms) and total == 0):
                continue
            if approx_equal(sum(terms, Decimal("0")), total,
                            rel_tol=rel_tol, abs_tol=Decimal("0.02")):
                check.checked_rows += 1
                continue
            idx = _nested_explains(terms, total, rel_tol)
            if idx is not None:
                soft_note("row_sum", total, terms, row=i, col=j, from_idx=idx)
            else:
                record("row_sum", total, terms, row=i, col=j, total_cell=(i, j))

    return check


def load_comparison_table(path: str | Path) -> list[dict[str, str]]:
    """读取 CSV 对比表（供模板比对与黄金测试集使用）。"""
    import csv

    p = Path(path)
    if not p.exists():
        raise HarnessError("CSV 不存在", path=str(p))
    with p.open(encoding="utf-8-sig", newline="") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


# ==========================================================================
# 工具封装
# ==========================================================================


class DetectTablesIn(BaseModel):
    path: str
    page: int = Field(ge=1)


class CheckArithmeticIn(BaseModel):
    grid: list[list[str]] = Field(description="二维字符串网格")
    rel_tol: float = Field(default=0.005, ge=0.0, le=0.1)


@tool(
    name="table.detect",
    description="识别某页的表格：优先几何线段重建（有框线），否则文本层坐标聚类。",
    params=DetectTablesIn,
    side_effect="read",
    cost="cheap",
    tags=["table", "deterministic"],
)
def detect_tables_tool(path: str, page: int) -> list[dict[str, Any]]:
    if not _HAS_FITZ:
        raise HarnessError("未安装 PyMuPDF")
    with fitz.open(path) as doc:
        if page < 1 or page > doc.page_count:
            raise HarnessError("页码越界", page=page)
        return [t.to_dict() for t in tables_of_page(doc.load_page(page - 1), page_no=page)]


@tool(
    name="table.detect_lines",
    description="快速判断某页是否存在表格框线（线段密度），用于路由决策。",
    params=DetectTablesIn,
    side_effect="read",
    cost="free",
)
def detect_lines_tool(path: str, page: int) -> dict[str, Any]:
    from domain.tools.document import _count_rule_lines, open_pdf

    with open_pdf(path) as doc:
        p = doc.load_page(page - 1)
        density = _count_rule_lines(p)
        return {"page": page, "rule_line_density": density, "likely_table": density >= 20}


@tool(
    name="table.check_arithmetic",
    description="校验表格「合计行/列 = 分项和」。纯确定性，是发现解析错误最有效的信号。",
    params=CheckArithmeticIn,
    side_effect="none",
    cost="free",
    tags=["table", "deterministic", "crosscheck"],
)
def check_arithmetic_tool(grid: list[list[str]], rel_tol: float = 0.005) -> dict[str, Any]:
    n_rows = len(grid)
    n_cols = max((len(r) for r in grid), default=0)
    cells = [
        TableCell(row=i, col=j, bbox=BBox(x0=0, y0=0, x1=0, y1=0), text=grid[i][j])
        for i in range(n_rows)
        for j in range(len(grid[i]))
    ]
    table = Table(
        page=0, bbox=BBox(x0=0, y0=0, x1=0, y1=0),
        n_rows=n_rows, n_cols=n_cols, cells=cells, method="inline",
    )
    return check_table_arithmetic(table, rel_tol=Decimal(str(rel_tol))).to_dict()


__all__ = [
    "TableCell",
    "Table",
    "ArithmeticCheck",
    "cell_number",
    "total_label_kind",
    "cluster_text_to_rows",
    "extract_table_via_text_layer",
    "extract_table_via_rules",
    "tables_of_page",
    "check_table_arithmetic",
    "detect_tables_tool",
    "detect_lines_tool",
    "check_arithmetic_tool",
]
