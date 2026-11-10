"""阶段 2.5：表格结构抽取（第 2 层的表格分支）。

## 为什么表格要独立成一个阶段

表格与「整页文本抽取」是两条不同的解析路径：

    普通页  → 文本层 / OCR → 逐页文本
    表格页  → 几何线段重建（有框线）/ 坐标聚类（无框线）→ **结构化网格**

早先实现把表格识别散落在 `judge.py` 里顺手调用，结果是：
    * 表格结构**从不落盘**，无法追溯、无法复现、无法在 Web 上展示
    * 表格里的数字**根本不进事实库** —— 而表格页占样本的 17.2%

现在把它做成独立阶段，产物写 ``channels/tables.json``，
后续阶段（judge 裁决、build_facts 取数）都消费同一份文件。

## 抽到的表格包含什么

每个单元格都带：
    * ``bbox``  —— 精确坐标（几何重建得来，不是估算）
    * ``text``  —— 该格文本
    * ``channel`` —— 来源（``rule_lines`` / ``text_layer``）
    * 列名 —— 由表头行提供，供下游做指标归属

**算术校验同步执行**：合计行/列必须等于分项和。
算不平就说明至少有一个单元格读错了，而且能定位到具体行列 ——
这正是单元格级 VLM 裁决的触发依据。
"""

from __future__ import annotations

from typing import Any

from agent.runtime.loop import RunContext
from domain.tools.tables import check_table_arithmetic, tables_of_page, total_label_kind
from pipelines.r1_extract.common import open_source, read_json, write_json

# 需要做表格抽取的路由
TABLE_ROUTES = {"D_table", "A_text_only", "E_mixed"}


def run(ctx: RunContext) -> dict[str, Any]:
    documents = read_json(ctx, "facts/documents.json")
    pages_index = {
        (p["doc_id"], int(p["page"])): p for p in read_json(ctx, "pages/index.json")
    }
    routing = ctx.configs.get("routing") or {}
    min_lines = int(routing.get("rule_line_density_threshold", 40))
    max_tables = int(routing.get("max_tables_per_page", 4))

    records: list[dict[str, Any]] = []
    stats = {
        "pages_scanned": 0,
        "tables_found": 0,
        "cells_total": 0,
        "tables_with_total_label": 0,
        "arith_checked": 0,
        "arith_failed_tables": 0,
        "arith_failures": 0,
        "arith_soft": 0,
        # 「无从校验」必须单独计数：没有合计线的表 arith.ok 也是 True，
        # 若只报"算术自洽比例"，就会把"没查过"混进"查过且没问题"。
        "arith_unverifiable": 0,
    }

    for d in documents:
        path = _resolve_raw(ctx, d.get("rel_path") or d["filename"])
        wanted: list[int] = []
        for pno_str, route in (d.get("page_routes") or {}).items():
            pno = int(pno_str)
            meta = pages_index.get((d["doc_id"], pno), {})
            # 表格页一定抽；其他页面若框线密度够高也抽（路由可能判成 A_text_only）
            if route in TABLE_ROUTES and (
                route == "D_table" or int(meta.get("rule_line_density", 0)) >= min_lines
            ):
                wanted.append(pno)

        if not wanted:
            continue

        with open_source(ctx, path) as doc:
            for page_no in sorted(wanted):
                stats["pages_scanned"] += 1
                page_obj = doc.load_page(page_no - 1)
                try:
                    tables = tables_of_page(page_obj, page_no=page_no)
                except Exception as exc:
                    if ctx.tracer:
                        ctx.tracer.error(
                            where="r1.extract_tables",
                            message=f"{d['filename']} p{page_no} 表格识别失败：{exc}",
                        )
                    continue

                for ti, t in enumerate(tables[:max_tables]):
                    stats["tables_found"] += 1
                    stats["cells_total"] += len(t.cells)

                    arith = check_table_arithmetic(t).to_dict()
                    checked = int(arith.get("checked", 0) or 0)
                    stats["arith_checked"] += checked
                    stats["arith_soft"] += int(arith.get("soft_count", 0) or 0)
                    if checked == 0:
                        stats["arith_unverifiable"] += 1
                    elif not arith.get("ok", True):
                        stats["arith_failed_tables"] += 1
                        stats["arith_failures"] += arith.get("failure_count", 0)
                    if any(total_label_kind(c) for row in t.grid() for c in row):
                        stats["tables_with_total_label"] += 1

                    header = t.header_row()
                    records.append(
                        {
                            "doc_id": d["doc_id"],
                            "file": d["filename"],
                            "page": page_no,
                            "table_index": ti,
                            "method": t.method,
                            "has_rules": t.has_rules,
                            "n_rows": t.n_rows,
                            "n_cols": t.n_cols,
                            "bbox": t.bbox.model_dump(),
                            "header": header,
                            "grid": t.grid(),
                            "arithmetic": arith,
                            # 单元格带 bbox —— 这是后续 VLM 裁剪与证据锚点的依据
                            "cells": [c.to_dict() for c in t.cells],
                        }
                    )

    write_json(ctx, "channels/tables.json", records)

    summary = (
        f"{stats['pages_scanned']} 页 → {stats['tables_found']} 张表，"
        f"{stats['cells_total']} 个单元格；"
        f"已验算合计线 {stats['arith_checked']} 条"
    )
    if stats["arith_failed_tables"]:
        summary += f"，其中 {stats['arith_failed_tables']} 张不平（{stats['arith_failures']} 处）"
    if stats["arith_unverifiable"]:
        # 说清楚"有多少张根本没得查"，而不是让读者以为其余都自洽
        summary += f"；{stats['arith_unverifiable']} 张表内无合计线，算术上无从校验"
    if ctx.tracer:
        ctx.tracer.stage_event(status="ok", detail=summary)

    if stats["pages_scanned"] == 0:
        return {**stats, "note": "没有页面被判定为表格页"}

    return stats


def _resolve_raw(ctx: RunContext, rel_or_name: str):
    from pipelines.r1_extract.common import resolve_raw

    return resolve_raw(ctx, rel_or_name)


__all__ = ["run", "TABLE_ROUTES"]
