"""阶段 4：交叉验证与 VLM 裁判（三层引擎的第 3 层）。

三件事，顺序不能颠倒：

    4.1 通道比对（纯确定性）：找出文本层与 OCR 读数不一致的页面/数值
    4.2 触发判定（纯确定性）：按 routing.yaml 决定哪些需要 VLM 裁决
    4.3 VLM 裁决：对照原图判决，**强制 quote 回读**防幻觉

成本闸门（重要）：VLM 调用是整条流水线最贵的环节，
因此只在「冲突 / 扫描件 / 表格 / 关键字段」时才触发，
并受 ``max_pages_per_doc`` 与 ``max_calls_per_run`` 双重限制。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agent.evidence.models import SourceLayer, Verdict
from agent.runtime.errors import HarnessError, ModelError
from agent.runtime.loop import RunContext
from agent.validate.quotecheck import QuoteMatch
from domain.tools.document import render_page_png
from domain.tools.ocr import ocr_lines_to_words
from domain.tools.vlm import (
    adjudicate_table,
    build_metric_catalog,
    judge_field,
    scan_page_fields,
)
from pipelines.r1_extract.common import open_source, read_json, record_output, write_json

_NUM_TOKEN = re.compile(r"[（(]?-?\d[\d,]*(?:\.\d+)?[)）]?")


def run(ctx: RunContext) -> dict[str, Any]:
    if ctx.gateway is None:
        raise HarnessError("未装配模型网关，无法执行 VLM 裁决阶段")

    routing = ctx.configs.get("routing") or {}
    trig = routing.get("vlm_trigger") or {}
    max_pages = int(trig.get("max_pages_per_doc", 120))
    max_calls = int(trig.get("max_calls_per_run", 400))
    key_metrics = set(routing.get("key_metrics") or [])
    fuzzy = float(routing.get("quote_fuzzy_threshold", 0.86))
    dpi = int(routing.get("ocr_dpi", 200))

    text_ch = {(r["doc_id"], r["page"]): r for r in read_json(ctx, "channels/text_layer.json")}
    ocr_ch = {(r["doc_id"], r["page"]): r for r in read_json(ctx, "channels/ocr.json")}
    documents = read_json(ctx, "facts/documents.json")

    conflicts: list[dict[str, Any]] = []
    verdicts: list[dict[str, Any]] = []
    scans: list[dict[str, Any]] = []
    calls = 0
    rejected = 0
    cannot = 0
    unknown = 0
    accepted = 0

    # 指标清单只构建一次 —— 把它喂给 VLM 是消除「自创指标名」的前提
    metric_catalog = build_metric_catalog()
    if ctx.tracer:
        ctx.tracer.stage_event(
            status="ok",
            detail=f"已构建指标清单（{len(metric_catalog.splitlines())} 条），将随每页扫描一并下发",
        )

    # 表格结构（由 extract_tables 阶段产出）—— 按 (doc_id, page) 索引
    tables_by_page: dict[tuple[str, int], list[dict[str, Any]]] = {}
    try:
        for rec in read_json(ctx, "channels/tables.json"):
            tables_by_page.setdefault((rec["doc_id"], int(rec["page"])), []).append(rec)
    except FileNotFoundError:
        pass

    table_cfg = ctx.configs.get("tables") or {}
    tr_enabled = bool(table_cfg.get("adjudicate", True))
    tr_max_cells = int(table_cfg.get("max_cells_per_failed_table", 6))
    tr_probe_cells = int(table_cfg.get("probe_cells_per_clean_table", 0))
    tr_probe_unverified = int(table_cfg.get("probe_cells_per_unverified_table", 2))
    table_reports: list[dict[str, Any]] = []
    table_calls = 0            # 成功产出裁决报告的表格数
    table_attempts = 0         # 进入裁决的表格数（含失败）
    table_errors = 0           # 裁决抛异常的表格数
    table_cells_corrected = 0
    table_cells_normalized = 0  # 数字一致、仅修正格式（换行切断）的格数
    table_cells_failed = 0      # 单元格级调用失败的格数（必须可见）

    for d in documents:
        path = _resolve_raw(ctx, d.get("rel_path") or d["filename"])
        pages_done = 0

        with open_source(ctx, path) as doc:
            for page_key in sorted(
                {k for k in list(text_ch) + list(ocr_ch) if k[0] == d["doc_id"]},
                key=lambda k: k[1],
            ):
                page_no = page_key[1]
                route = d["page_routes"].get(str(page_no)) or d["page_routes"].get(page_no)

                t = text_ch.get(page_key)
                o = ocr_ch.get(page_key)

                # ---------- 4.1 通道比对（确定性） ----------
                diffs = _diff_channels(t, o)
                need, why = _need_vlm(
                    route=route, diffs=diffs, ocr=o, trig=trig, pages_done=pages_done,
                    max_pages=max_pages, calls=calls, max_calls=max_calls,
                )

                if diffs:
                    conflicts.append(
                        {
                            "doc_id": d["doc_id"],
                            "page": page_no,
                            "route": route,
                            "diff_count": len(diffs),
                            "diffs": diffs[:20],
                            "vlm_triggered": need,
                            "trigger_reason": why,
                        }
                    )

                if not need:
                    continue

                # ---------- 4.3 VLM 在线（读图 + 指标归属） ----------
                page_text = "\n".join(
                    filter(None, [
                        _auth_text(t),
                        _auth_text(o),
                    ])
                )
                img = ctx.pages_dir / f"{d['doc_id']}_p{page_no:04d}_{dpi}dpi_judge.png"
                render_page_png(doc, page_no, img, dpi=dpi)
                record_output(ctx, img)

                # (a) 整页扫描 —— 读出多个带指标归属的字段。
                #     这是主路径：取代了早期「每页只抽一个 page_key_fields」的做法。
                #     早期做法让同页不同主体的多个值挤在同一个 field_id 下，
                #     被下游当成「同一指标的多个候选值」而触发假冲突。
                if calls < max_calls:
                    calls += 1
                    try:
                        scan = scan_page_fields(
                            ctx.gateway,
                            image_path=img,
                            page_no=page_no,
                            page_text=page_text,
                            metric_catalog=metric_catalog,
                            verify_quote=bool(trig.get("verify_quote", True)),
                            fuzzy_threshold=fuzzy,
                            max_fields=int(trig.get("max_fields_per_page", 12)),
                        )
                    except ModelError as exc:
                        if ctx.tracer:
                            ctx.tracer.error(
                                where="vlm.scan_page_fields", message=str(exc),
                            )
                        ctx.ledger.reject("VLM 整页扫描失败", page=page_no, detail=str(exc))
                        scan = None

                    if scan is not None:
                        rejected += scan.rejected_count
                        unknown += scan.unknown_metric_count
                        accepted += len(scan.usable_fields)
                        scans.append(
                            {
                                "doc_id": d["doc_id"],
                                "page": page_no,
                                "route": route,
                                "task": "page_scan",
                                **scan.model_dump(mode="json"),
                            }
                        )
                        for est in scan.fields:
                            if est.rejected:
                                ctx.ledger.reject(
                                    "VLM 视觉读数未通过 quote 回读闸门（判定为幻觉）",
                                    page=page_no,
                                    metric=est.metric,
                                    detail=est.rejection_reason,
                                )
                else:
                    if ctx.tracer:
                        ctx.tracer.error(
                            where="r1.judge",
                            message=f"VLM 调用数达上限 {max_calls}，停止裁决",
                        )

                # (b) 具体冲突值的定向裁决。
                #     两路读数给出了**互不相同**的具体数值时，
                #     整页扫描未必覆盖到这些值，需要单独点名裁决。
                targets = _judge_targets(diffs, t, o, key_metrics)
                for tgt in targets:
                    if tgt["field_id"] == "page_key_fields":
                        continue  # 该任务已由整页扫描取代
                    if calls >= max_calls:
                        break
                    calls += 1
                    try:
                        out = judge_field(
                            ctx.gateway,
                            image_path=img,
                            page_no=page_no,
                            field_id=tgt["field_id"],
                            field_label=tgt["field_label"],
                            channel_a=tgt.get("channel_a"),
                            channel_b=tgt.get("channel_b"),
                            page_text=page_text,
                            verify_quote=bool(trig.get("verify_quote", True)),
                            fuzzy_threshold=fuzzy,
                        )
                    except ModelError as exc:
                        if ctx.tracer:
                            ctx.tracer.error(
                                where="vlm.judge_field", message=str(exc),
                            )
                        ctx.ledger.reject("VLM 调用失败", page=page_no, field=tgt["field_id"])
                        continue

                    if out.rejected:
                        rejected += 1
                        ctx.ledger.reject(
                            "VLM 输出未通过 quote 回读闸门（判定为幻觉）",
                            page=page_no,
                            field=out.field_id,
                            detail=out.rejection_reason,
                        )
                    if out.verdict is Verdict.CANNOT_DETERMINE:
                        cannot += 1

                    verdicts.append(
                        {
                            "doc_id": d["doc_id"],
                            "page": page_no,
                            "route": route,
                            "task": "field_judge",
                            **out.model_dump(mode="json"),
                        }
                    )

                # ---------- 4.4 表格单元格级裁决 ----------
                # 触发条件是**算术不平**与可疑单元格的存在，而不是整页路由。
                # 算术校验能把出错位置精确到行列，因此只需复核那几个格子。
                page_tables = tables_by_page.get((d["doc_id"], page_no), [])
                if page_tables and tr_enabled and calls < max_calls:
                    page_img = ctx.pages_dir / f"{d['doc_id']}_p{page_no:04d}_{dpi}dpi_cells.png"
                    if not page_img.is_file():
                        render_page_png(doc, page_no, page_img, dpi=dpi)
                        record_output(ctx, page_img)
                    # 单元格 bbox 内的**页面真实文字**（单词级坐标）。
                    # 这是唯一不依赖模型自述的裁判 —— 见 corroborate_by_geometry。
                    page_words = doc.load_page(page_no - 1).get_text("words")
                    geometry_source = "text_layer"
                    if not page_words:
                        # 扫描件没有文本层 → ``get_text("words")`` 为空。
                        # 此时用 **OCR 的行框**顶上：它同样不是模型自述，
                        # 只是把"页面上印着什么"交给 OCR 回答。
                        # 没有它就只能失败关闭（模型读数只标记不覆盖）。
                        o = ocr_ch.get((d["doc_id"], page_no)) or {}
                        page_words = ocr_lines_to_words(
                            o.get("lines") or [], int(o.get("dpi") or dpi)
                        )
                        geometry_source = "ocr" if page_words else "none"
                    if ctx.tracer and geometry_source != "text_layer":
                        ctx.tracer.stage_event(
status="ok",
                            detail=(f"{d['filename']} p{page_no}：几何佐证改用 "
                                    f"{'OCR 行框' if geometry_source == 'ocr' else '（无）'}"
                                    f"（该页无文本层）"),
                        )
                    # 裁图落在**按文档分目录**的路径下：文件名只带页码与行列，
                    # 若所有文档共用一个目录，不同文档的同页同格会互相覆盖 ——
                    # 证据图显示的就不是这张表了，而且不报任何错。
                    cells_dir = ctx.pages_dir / "cells" / d["doc_id"]

                    for rec in page_tables:
                        arith = rec.get("arithmetic") or {}
                        checked = int(arith.get("checked", 0) or 0)
                        arith_ok = bool(arith.get("ok", True))
                        # 三档触发。注意 **arith_ok 默认 True**：一张没有合计线的表
                        # 会"算术自洽"得毫无依据（checked == 0），不能当已校验看待，
                        # 否则"没查过"被当成"查过且没问题"。
                        if not arith_ok:
                            budget, trigger = tr_max_cells, "unbalanced"
                        elif checked > 0:
                            budget, trigger = tr_probe_cells, "balanced"
                        else:
                            budget, trigger = tr_probe_unverified, "no_total_line"
                        if budget <= 0 or calls >= max_calls:
                            break

                        table_attempts += 1
                        try:
                            table = _table_from_record(rec)
                            rep = adjudicate_table(
                                ctx.gateway,
                                table=table,
                                page_image_path=page_img,
                                page_no=page_no,
                                page_text=page_text,
                                page_words=page_words,
                                work_dir=cells_dir,
                                guard=ctx.guard(),
                                max_cells=min(budget, max_calls - calls),
                                fuzzy_threshold=fuzzy,
                            )
                        except ModelError as exc:
                            table_errors += 1
                            if ctx.tracer:
                                ctx.tracer.error(
                                    where="vlm.adjudicate_table",
                                    message=str(exc),
                                )
                            continue
                        except Exception as exc:
                            # 这里必须计数：`except Exception` 会把**代码 bug**
                            # 和"模型偶发失败"一起吞掉。实测踩过 —— 裁决循环里
                            # 写了未定义变量，10 张表全部抛 NameError，
                            # 而阶段仍报 success、产物是空数组，只能靠 trace 才发现。
                            table_errors += 1
                            if ctx.tracer:
                                ctx.tracer.error(
                                    where="vlm.adjudicate_table",
                                    message=f"{type(exc).__name__}: {exc}",
                                )
                            continue

                        table_calls += 1
                        # 只把**成功**的单元格计入调用预算：模型调用失败时
                        # rep.errors 已经计过，再算进 calls 会虚增成本统计
                        # （实测：离线且缓存为空时 184 个格子全失败，
                        #   却显示复核了 184 格、花了 184 次调用）
                        calls += max(0, rep.cells_checked - rep.errors)
                        table_cells_corrected += rep.corrected
                        table_cells_normalized += rep.normalized
                        table_cells_failed += rep.errors
                        adj_rec = {
                                "doc_id": d["doc_id"],
                                "page": page_no,
                                "table_index": rec.get("table_index", 0),
                                "method": rec.get("method", ""),
                                "n_rows": rec.get("n_rows"),
                                "n_cols": rec.get("n_cols"),
                                "header": rec.get("header"),
                                "trigger": trigger,
                                "cells_budget": budget,
                                "arithmetic_before": arith,
                                "report": rep.to_dict(),
                                # 裁决必须留下**改后的完整网格**，否则下游拿不到修正值
                                "grid_after": table.grid(),
                                "cells_after": [c.to_dict() for c in table.cells],
                        }
                        table_reports.append(adj_rec)
                        if ctx.tracer:
                            ctx.tracer.tool(
                                name="table.adjudicate",
                                args={"doc": d["filename"], "page": page_no,
                                      "table": rec.get("table_index", 0)},
                                ok=True, latency_ms=0,
                                # 裁决的「返回」就是这份报告（含改后的网格）。
                                # 逐格裁决的模型调用在别处另记（角色 vlm）。
                                result=adj_rec,
                            )

                pages_done += 1

    write_json(ctx, "channels/conflicts.json", conflicts)
    write_json(ctx, "channels/vlm_verdicts.json", verdicts)
    write_json(ctx, "channels/vlm_scans.json", scans)
    # ⚠️ 这里存的是**裁决后**的完整网格（grid_after），它才是下游该用的版本。
    #    ``channels/tables.json`` 里保留的是裁决**前**的网格与当时的算术校验结论，
    #    两者都在产物里，因此"改了什么"可以逐格对照。
    write_json(ctx, "channels/table_adjudications.json", table_reports)

    stats = {
        "conflict_pages": len(conflicts),
        "vlm_calls": calls,
        "page_scans": len(scans),
        "scan_fields_accepted": accepted,
        "scan_fields_rejected": rejected,
        "scan_fields_unknown_metric": unknown,
        "field_judge_verdicts": len(verdicts),
        "rejected_by_quote_gate": rejected + sum(
            1 for v in verdicts if v.get("rejected")
        ),
        "cannot_determine": cannot,
        "tables_attempted": table_attempts,
        "tables_adjudicated": table_calls,
        "table_adjudication_errors": table_errors,
        "table_cells_checked": sum(
            int((r.get("report") or {}).get("cells_checked", 0) or 0) for r in table_reports
        ),
        "table_cells_corrected": table_cells_corrected,
        "table_cells_normalized": table_cells_normalized,
        "table_cells_failed": table_cells_failed,
        "table_arith_improved": sum(
            1 for r in table_reports if r["report"].get("improved")
        ),
    }
    if table_errors:
        # 裁决失败必须显性化：产物为空数组 + 阶段 success 的组合极具误导性
        print(f"   ⚠ 表格裁决有 {table_errors} 张失败（详见 trace.jsonl 的 error 事件）")
    if table_attempts:
        print(
            f"   表格裁决：进入 {table_attempts} 张 / 成功 {table_calls} 张，"
            f"复核单元格 {stats['table_cells_checked']} 个，"
            f"改值 {table_cells_corrected} 个，格式归一 {table_cells_normalized} 个，"
            f"算术改善 {stats['table_arith_improved']} 张"
        )
        if table_cells_failed:
            # 单元格级失败同样必须可见 —— 它会让"改值 0"看起来像"没问题"
            print(f"   ⚠ 其中 {table_cells_failed} 个单元格调用失败，未能裁决")

    # ★ 把"部分失败"上报给调度器，让整次运行标为 partial。
    #   不这样做的话：阶段报 success、产物已落盘、运行状态 success，
    #   而 `agent replay --strict` 会说"不可复现"却给不出原因 ——
    #   因为那次失败的模型调用没有响应缓存。实测踩过。
    failures: dict[str, int] = {}
    if table_errors:
        failures["表格裁决失败"] = table_errors
    if table_cells_failed:
        failures["单元格裁决调用失败"] = table_cells_failed
    if failures:
        stats["partial_failures"] = failures
        print(
            "   ⚠ 本次运行标记为 partial：有模型调用失败，结果不完整，"
            "且离线回放可能不可复现"
        )
    if ctx.tracer:
        ctx.tracer.stage_event(
            status="warn" if table_errors else "ok",
            detail=str(stats),
        )
    return stats


# ==========================================================================
# 4.1 通道比对
# ==========================================================================


def _auth_text(rec: dict[str, Any] | None) -> str:
    """取通道记录的权威文本（优先 ``raw``，回退 ``text``）。"""
    if not rec:
        return ""
    return rec.get("raw") or rec.get("text") or ""


def _table_from_record(rec: dict[str, Any]) -> Any:
    """从 ``channels/tables.json`` 的记录重建 :class:`Table` 对象。

    为什么需要：产物是 JSON（可追溯、可复现），而裁决函数操作对象。
    重建时**必须把 bbox 一起带回来** —— 单元格级裁剪与证据锚点全靠它。
    """
    from agent.evidence.models import BBox
    from domain.tools.tables import Table, TableCell

    cells = [
        TableCell(
            row=int(c["row"]),
            col=int(c["col"]),
            bbox=BBox(**c["bbox"]),
            text=c.get("text", ""),
            confidence=float(c.get("confidence", 0.0)),
            source_channel=c.get("source_channel", ""),
        )
        for c in rec.get("cells", [])
    ]
    return Table(
        page=int(rec["page"]),
        bbox=BBox(**rec["bbox"]),
        n_rows=int(rec["n_rows"]),
        n_cols=int(rec["n_cols"]),
        cells=cells,
        method=rec.get("method", ""),
        has_rules=bool(rec.get("has_rules", False)),
    )


def _numbers(text: str) -> list[str]:
    """抽出页面里的数值 token（归一化掉千分位差异）。"""
    out = []
    for m in _NUM_TOKEN.finditer(text or ""):
        s = m.group(0).strip("()（）").replace(",", "")
        if s and s not in {"-", "—"}:
            out.append(s)
    return out


def _diff_channels(
    text_rec: dict[str, Any] | None, ocr_rec: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """比对两个通道的读数。

    比对的是**数值集合**而非逐字文本：OCR 与文本层的换行、空格差异
    不代表解析错误，只有数字对不上才是真冲突。
    """
    if text_rec is None or ocr_rec is None:
        return []

    tn = set(_numbers(_auth_text(text_rec)))
    on = set(_numbers(_auth_text(ocr_rec)))
    if not tn or not on:
        return []

    only_text = sorted(tn - on)[:15]
    only_ocr = sorted(on - tn)[:15]
    if not only_text and not only_ocr:
        return []

    return [
        {
            "kind": "numbers_only_in_text_layer",
            "values": only_text,
            "note": "文本层有、OCR 无 —— 可能是 OCR 漏识或文本层为水印/页眉",
        },
        {
            "kind": "numbers_only_in_ocr",
            "values": only_ocr,
            "note": "OCR 有、文本层无 —— 可能是扫描图像中的内容，OCR 更可信",
        },
    ]


# ==========================================================================
# 4.2 触发判定
# ==========================================================================


def _need_vlm(
    *,
    route: str | None,
    diffs: list[dict[str, Any]],
    ocr: dict[str, Any] | None,
    trig: dict[str, Any],
    pages_done: int,
    max_pages: int,
    calls: int,
    max_calls: int,
) -> tuple[bool, str]:
    """决定是否需要 VLM 裁决，并给出理由（写入产物，可追溯）。"""
    if pages_done >= max_pages:
        return False, f"已达单文档 VLM 页数上限 {max_pages}"
    if calls >= max_calls:
        return False, f"已达单次运行 VLM 调用上限 {max_calls}"

    if diffs and trig.get("on_channel_conflict", True):
        total = sum(len(d["values"]) for d in diffs)
        return True, f"通道冲突：文本层与 OCR 有 {total} 个数值不一致"

    if route in ("B_scanned", "C_garbled") and trig.get("on_scanned_page", True):
        return True, f"路由为 {route}（无可靠文本层），无可交叉的独立来源"

    if route == "D_table" and trig.get("on_table_page", True):
        return True, "表格页：行列结构易崩，需视觉裁决"

    if ocr and ocr.get("low_confidence_ratio", 0) > 0.3:
        return True, f"OCR 低置信行占比 {ocr['low_confidence_ratio']:.0%} 偏高"

    return False, "无触发条件命中（文本层干净且与 OCR 一致）"


def _judge_targets(
    diffs: list[dict[str, Any]],
    text_rec: dict[str, Any] | None,
    ocr_rec: dict[str, Any] | None,
    key_metrics: set[str],
) -> list[dict[str, Any]]:
    """生成裁决目标清单。

    有具体冲突值 -> 逐值裁决（精确、便宜）。
    无冲突但需裁决（扫描件/表格）-> 做一次整页关键字段裁决。
    """
    targets: list[dict[str, Any]] = []

    conflict_values: list[str] = []
    for d in diffs:
        conflict_values.extend(d.get("values", [])[:5])

    for i, v in enumerate(conflict_values[:8]):
        targets.append(
            {
                "field_id": f"page_conflict_{i}",
                "field_label": f"页面上的数值 {v}（两个通道读数不一致，需确认其真实值与所属指标）",
                "channel_a": v if any(
                    v in d.get("values", []) for d in diffs
                    if d["kind"] == "numbers_only_in_text_layer"
                ) else None,
                "channel_b": v if any(
                    v in d.get("values", []) for d in diffs
                    if d["kind"] == "numbers_only_in_ocr"
                ) else None,
            }
        )

    if not targets:
        # 无具体冲突：做一次整页关键字段提取（受 key_metrics 约束）
        metrics_hint = "、".join(sorted(key_metrics)[:12]) if key_metrics else "主要财务科目"
        targets.append(
            {
                "field_id": "page_key_fields",
                "field_label": (
                    f"本页的关键字段（重点关注：{metrics_hint}）。"
                    "请找出页面上最显著的一个数值及其所属指标，按约定 JSON 输出"
                ),
                "channel_a": (_auth_text(text_rec))[:600] or None,
                "channel_b": (_auth_text(ocr_rec))[:600] or None,
            }
        )

    return targets


def _resolve_raw(ctx: RunContext, rel_or_name: str) -> Path:
    """解析材料文件路径（统一实现在 ``common.resolve_raw``）。"""
    from pipelines.r1_extract.common import resolve_raw

    return resolve_raw(ctx, rel_or_name)


__all__ = ["run"]
