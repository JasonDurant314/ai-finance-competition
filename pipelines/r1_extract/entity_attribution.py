"""把「落在表格行里的文本事实」重新归属到该行的主体。

背景（实测）：

    文本层规则抽取把**整页**的事实都挂在文档主体名下。对正文叙述这是对的，
    但表格里常常一行一个其他主体 —— 被担保人、子公司、股东。于是

        附件2 被担保人基本情况
        海南唐人神生物科技有限公司 | … | 营业收入 12,306,223.33

    里那个营业收入会被记到**披露主体**名下，而它其实是子公司的。
    实测一份材料里这样错挂的有 100+ 条 —— 而且它不报错，
    只会让"某某公司营业收入"这个结论静默失真。

本模块用**确定性**方式纠正：事实的原文若落在某张表的某个单元格里，
就把主体改成该单元格所在行的行主体（判据与表格抽取完全一致）。
定位不到 / 判不出主体 → **保持原样**（宁可不动，也不乱动）。
"""

from __future__ import annotations

import re
from typing import Any, Callable

from agent.validate.quotecheck import normalize_text
from pipelines.r1_extract.table_facts import (
    build_header,
    looks_like_value,
    map_columns_to_metrics,
    pick_subject_column,
    subject_of_row,
)

CellEntry = tuple[str, str, int, int]   # (归一化单元格文本, 主体, 行, 列)


def cell_lookup(
    records: list[dict[str, Any]],
    alias_map: dict[str, str],
    metric_units: dict[str, str],
) -> dict[tuple[str, int], list[CellEntry]]:
    """建索引：``(doc_id, page)`` → 该页所有「数据行单元格」的 (文本, 主体, 行, 列)。

    单元格文本做**归一化**（去空白、全角转半角）后再比对，
    这样页面里被折行切断的数字与表格网格里修好的数字能对上。

    必须排除**表头行**：实测某页表头占两行而 `build_header` 只认出第一行
    （第一行是空的），于是第二行被当成数据行，"主体"变成了字符串「公司名称」，
    索引里混进永远匹配不上的垃圾条目。

    还要处理**跨页续行**：中文表格里宽列（经营范围、财务数据）会把一行撑得很高，
    折行后财务列落到下一页，于是下一页出现"只有财务列、没有公司名"的行。
    实测某页 r2 就是这种：c8/c9 有「营业收入：961,617.23万元」，c0–c7 全空 ——
    它的公司名在上一页。这类行必须**继承上一行的主体**，
    否则它的值会挂到文档主体名下，被冲突守卫标记，最终让 R4 无数据可用。
    """
    index: dict[tuple[str, int], list[CellEntry]] = {}
    carry: dict[str, str] = {}          # doc_id → 最近一次判出的主体
    for rec in sorted(records, key=lambda r: (r.get("doc_id") or "",
                                              int(r.get("page") or 0))):
        doc_id = rec.get("doc_id") or ""
        page = int(rec.get("page") or 0)
        grid = rec.get("grid") or []
        if not doc_id or not page or not grid:
            continue

        header, data_start = build_header(grid, alias_map)
        col2metric = map_columns_to_metrics(header, alias_map, metric_units)
        subject_col = pick_subject_column(grid, data_start, metric_cols=set(col2metric))
        # 表头页判不出主体列是正常的（表头在上一页）；此时靠**继承**继续，
        # 只有既没有主体列、也没有可继承主体时才放弃这一页。
        if subject_col < 0 and not carry.get(doc_id):
            continue

        # 数据区起点：优先用指标列，否则取主体列右边一列。
        # （主体列本身已经排除数值列，因此 +1 是安全的）
        first_data_col = min(col2metric) if col2metric else max(subject_col, 0) + 1

        entries: list[CellEntry] = []
        for i in range(data_start, len(grid)):
            row = grid[i]
            if _is_header_like_row(row):
                continue
            subject = subject_of_row(row, subject_col=subject_col)
            if not subject and _is_continuation_row(row, first_data_col):
                subject = carry.get(doc_id, "")
            if not subject:
                continue
            carry[doc_id] = subject
            for j, cell in enumerate(row):
                norm = normalize_text(cell or "")
                if len(norm) >= 3:
                    entries.append((norm, subject, i, j))
        if entries:
            index.setdefault((doc_id, page), []).extend(entries)
    return index


def _is_continuation_row(row: list[str], first_data_col: int) -> bool:
    """标签区没有**文字标签**、但数据区有内容 → 这是上一页某一行的续行。

    注意"标签"要排除数值格：「434,749.30万元」含汉字单位，容易被误当成标签。
    """
    labels = [c for c in row[:max(first_data_col, 0)]
              if (c or "").strip() and not looks_like_value(c)]
    data = [c for c in row[max(first_data_col, 0):] if (c or "").strip()]
    return not labels and bool(data)


_LABEL_WORDS = (
    "公司名称", "成立日期", "注册资本", "注册地点", "法定代表人", "经营范围",
    "股东结构", "序号", "项目", "被担保人", "担保人", "持股比例", "主营业务",
    "注册资本", "注册地",
)


def _is_header_like_row(row: list[str]) -> bool:
    """整行都是短标签 → 是表头行（中文表格的表头常常不止一行）。

    ⚠️ **不能给单元格数量设上限**：实测表头行有 10 个格子
    （序号/公司名称/成立日期/…/财务数据），设了上限就漏判 ——
    于是表头行被当成数据行，"主体"变成字符串「公司名称」，
    索引里混进垃圾条目，真数据行反而被挤掉。
    """
    texts = [normalize_text(c or "") for c in row if (c or "").strip()]
    if not texts:
        return False
    labelish = sum(1 for t in texts
                   if len(t) <= 12 and any(w in t for w in _LABEL_WORDS))
    return labelish >= 3 and labelish >= len(texts) // 2


def reattribute(
    facts: list[Any],
    index: dict[tuple[str, int], list[CellEntry]],
    page_of: Callable[[Any], int | None],
) -> tuple[int, list[dict[str, Any]]]:
    """就地改主体。返回 ``(改动条数, 改动明细)``。

    ``page_of``：从证据锚点取出事实所在页码（事实本身不带页码）。

    两级匹配：

    1. **归一化文本包含**（首选，最具体）
    2. **纯数字包含 + 唯一性**（兜底）：文本匹配不上时，用纯数字串去找。
       这么做的原因：事实的 ``raw_text`` 是"数值+单位"（如 ``961,617.23万元``），
       而单元格里可能写成 ``营业收入：961,617.23 万元；…`` 之外，
       还可能被切成两个相邻单元格。数字匹配更宽容。

       ⚠️ 但宽容必须配**唯一性闸门**：若多个**不同主体**的单元格都含这个数字串
       （比如两家子公司营收都是 0），那说明它区分不出主体，**宁可不改** ——
       改错主体比不改严重得多。
    """
    changed = 0
    detail: list[dict[str, Any]] = []
    for fact in facts:
        page = page_of(fact)
        if not page:
            continue
        entries = index.get((fact.doc_id, int(page)))
        if not entries:
            continue
        needle = normalize_text(getattr(fact, "raw_text", "") or "")
        if len(needle) < 2:
            continue

        # —— ① 文本匹配：取最长匹配（一个单元格里可能含多个数字，长匹配更具体）
        best: CellEntry | None = None
        for entry in entries:
            if needle in entry[0] and (best is None or len(entry[0]) > len(best[0])):
                best = entry
        matched_by = "text"

        # —— ② 数字兜底：必须**唯一指向一个主体**
        if best is None:
            digits = re.sub(r"\D", "", needle)
            if len(digits) >= 3:
                cands = [e for e in entries if digits in re.sub(r"\D", "", e[0])]
                subjects = {e[1] for e in cands}
                if len(subjects) == 1 and cands:
                    best = max(cands, key=lambda e: len(e[0]))
                    matched_by = "digits_unique"
        if best is None:
            continue

        subject = best[1]
        if len(subject) < 3 or re.fullmatch(r"[\d,.\-/%]+", subject):
            continue
        if subject == getattr(fact, "entity", ""):
            continue

        detail.append({
            "fact_id": fact.fact_id,
            "metric": fact.metric,
            "raw_text": fact.raw_text,
            "from": fact.entity,
            "to": subject,
            "page": int(page),
            "row": best[2],
            "col": best[3],
            "matched_by": matched_by,
        })
        fact.entity = subject
        changed += 1
    return changed, detail


__all__ = ["cell_lookup", "reattribute", "CellEntry"]
