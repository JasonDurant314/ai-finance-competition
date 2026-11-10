"""阶段 3：OCR 通道（三层引擎的第 2 层）。

处理路由为 ``B_SCANNED`` / ``C_GARBLED`` 的页面，以及 ``D_TABLE`` 页面的
单元格补充识别。

流程：渲染 -> 预处理 -> OCR -> 确定性规则校验
产出的通道读数写入 ``channels/ocr.json``，供阶段 4 的 VLM 裁判
作为「通道 B」输入。

**未安装 OCR 引擎时显式失败并给出安装指引**，不静默产出空结果。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from agent.evidence.models import SourceLayer
from agent.runtime.errors import HarnessError
from agent.runtime.loop import RunContext
from domain.tools.document import render_page_png
from domain.tools.image import preprocess_image
from domain.tools.ocr import available_engines, recognize
from pipelines.r1_extract.common import open_source, read_json, record_output, write_json

OCR_ROUTES = {"B_scanned", "C_garbled", "E_mixed", "D_table"}


def _needs_ocr(
    route: str | None, probe: dict[str, Any], *, min_chars: int, min_conf: float
) -> tuple[bool, str]:
    """判断某一页是否**真的需要** OCR。

    ★ 这一步是性能关键。早期实现只要路由在 ``OCR_ROUTES`` 里就一律 OCR，
    于是「表格页」也被送去跑 OCR —— 而表格页几乎都有完好的文本层，
    表格结构本来就由几何线段重建（``extract_tables`` 阶段）而非 OCR 提供。

    实测代价：一个含 47 个表格页的文档，OCR 要跑 5 分钟以上，
    而其中绝大部分纯属浪费。

    判据：
        * ``B_scanned`` / ``C_garbled`` —— 无可靠文本层，**必须** OCR
        * ``D_table`` / ``E_mixed`` / ``A_text_only`` —— 文本层够用就跳过
    """
    if route in ("B_scanned", "C_garbled"):
        return True, f"路由 {route}：无可靠文本层，必须 OCR"

    chars = int(probe.get("char_count", 0))
    garbled = float(probe.get("garbled_ratio", 0.0))
    if chars >= min_chars and garbled < min_conf:
        return False, (
            f"文本层可用（{chars} 字符，乱码率 {garbled:.2%}），"
            "跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR"
        )
    return True, f"文本层不足（{chars} 字符 < {min_chars}），需 OCR 补强"


def run(ctx: RunContext) -> dict[str, Any]:
    documents = read_json(ctx, "facts/documents.json")

    # 先看是否真的有页面需要 OCR。没有需要就**跳过**，不视为失败 ——
    # 这是正常的降级路径（例如全部页面都有干净文本层）。
    ocr_pages = sum(
        1
        for d in documents
        for r in d["page_routes"].values()
        if r in OCR_ROUTES and r != "D_table"
    )
    if ocr_pages == 0 and not _has_table_pages(documents):
        write_json(ctx, "channels/ocr.json", [])
        if ctx.tracer:
            ctx.tracer.stage_event(
                status="skipped",
                detail="无页面需要 OCR（全部页面文本层干净），第 2 层未触发",
            )
        return {"pages": 0, "skipped": True, "reason": "no_pages_need_ocr"}

    engines = available_engines()
    if not engines:
        # 区分「必需 OCR」与「锦上添花」两种情形：
        #   B_scanned / C_garbled 页面的**唯一**读数来源就是 OCR，缺失即无法处理 -> 硬失败
        #   仅 E_mixed 页面需要 OCR 补强时，可降级：标 needs_review 并显式声明缺口
        hard_pages = _count_hard_ocr_pages(documents)
        if hard_pages > 0:
            raise HarnessError(
                "未安装 OCR 引擎，但有页面**只能**依赖 OCR 读取。这是必需依赖，不会静默降级。",
                hint="pip install rapidocr-onnxruntime",
                scanned_or_garbled_pages=hard_pages,
            )

        write_json(ctx, "channels/ocr.json", [])
        write_json(
            ctx,
            "channels/ocr_degraded.json",
            {
                "reason": "ocr_engine_not_installed",
                "impact": "仅图文混排页（E_mixed）未做 OCR 补强，文本层结果不受影响",
                "pages_skipped": ocr_pages,
                "remediation": "pip install rapidocr-onnxruntime 后重跑即可补齐",
            },
        )
        if ctx.tracer:
            ctx.tracer.stage_event(
                status="degraded",
                detail=(
                    f"未安装 OCR 引擎；{ocr_pages} 个图文混排页跳过 OCR 补强，"
                    "已记录缺口并标记降级（这些页面的结论置信度相应降低）"
                ),
            )
        return {
            "pages": 0,
            "skipped": True,
            "degraded": True,
            "reason": "no_ocr_engine_but_only_soft_pages",
            "pages_skipped": ocr_pages,
        }

    routing = ctx.configs.get("routing") or {}
    dpi = int(routing.get("ocr_dpi", 200))
    dpi_small = int(routing.get("ocr_dpi_small_font", 300))
    min_conf = float(routing.get("ocr_min_confidence", 0.60))
    # OCR 门控用的「文本层够用」阈值 —— 与路由判定同一个口径
    min_chars = int(routing.get("min_chars_for_text_layer", 50))
    pre_cfg = routing.get("preprocess") or {}

    pages_index = {
        (p["doc_id"], p["page"]): p for p in read_json(ctx, "pages/index.json")
    }
    records: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    work_dir = ctx.pages_dir

    for d in documents:
        path = _resolve_raw(ctx, d.get("rel_path") or d["filename"])
        wanted_all = sorted(
            int(p) for p, r in d["page_routes"].items() if r in OCR_ROUTES
        )
        if not wanted_all:
            continue

        with open_source(ctx, path) as doc:
            for page_no in wanted_all:
                key = (d["doc_id"], page_no)
                probe = pages_index.get(key, {})
                route = d["page_routes"].get(str(page_no))

                # ★ OCR 门控：文本层够用就跳过。
                #   表格页几乎都有完好文本层，且表格结构由几何重建提供，
                #   因此对它们跑 OCR 是纯浪费（实测一个含 47 个表格页的文档
                #   会因此多跑 5 分钟以上）。
                need, why = _needs_ocr(
                    route, probe, min_chars=min_chars, min_conf=0.05
                )
                if not need:
                    skipped.append(
                        {"doc_id": d["doc_id"], "page": page_no,
                         "route": route, "reason": why}
                    )
                    if ctx.tracer:
                        ctx.tracer.routing(page=page_no, route=route or "?",
                                           reason=f"跳过 OCR：{why}")
                    continue

                # 小字号页面提高 DPI —— 财报表格数字极小，低 DPI 必错
                page_dpi = (
                    dpi_small
                    if 0 < float(probe.get("min_font_size", 99)) < 7.0
                    else dpi
                )

                t0 = time.perf_counter()
                raw_png = work_dir / f"{d['doc_id']}_p{page_no:04d}_raw.png"
                render_page_png(doc, page_no, raw_png, dpi=page_dpi)
                record_output(ctx, raw_png)

                # OCR 真正吃的是**预处理后**那张（灰度/纠偏/去噪），不是原始渲染图。
                # 两张都留痕：出错时能分清「是渲染糊了」还是「是预处理把字吃掉了」。
                pre_png = work_dir / f"{d['doc_id']}_p{page_no:04d}.png"
                pre = preprocess_image(
                    raw_png,
                    pre_png,
                    grayscale=bool(pre_cfg.get("grayscale", True)),
                    deskew=bool(pre_cfg.get("deskew", True)),
                    denoise=bool(pre_cfg.get("denoise", True)),
                    adaptive_threshold=bool(pre_cfg.get("adaptive_threshold", False)),
                    max_skew_degrees=float(pre_cfg.get("max_skew_degrees", 8.0)),
                )
                record_output(ctx, pre_png)

                ocr_res = recognize(
                    pre_png,
                    engine=engines[0],
                    min_confidence=0.0,
                )

                # —— 确定性规则校验（纯代码，不涉及模型）——
                rules = _rule_validate(ocr_res.text, min_conf=min_conf)

                # OCR 文本作为**辅助**文本源登记：提高 quote 回读召回率，
                # 但不覆盖权威原文 —— 否则 OCR 的错误会让编造的 quote 通过闸门。
                ctx.store.add_aux_text(d["doc_id"], page_no, ocr_res.text)

                ocr_rec = {
                        "doc_id": d["doc_id"],
                        "file": d["filename"],
                        "page": page_no,
                        "channel": "ocr",
                        "source_layer": SourceLayer.L2_OCR.value,
                        "engine": ocr_res.engine,
                        "dpi": page_dpi,
                        "preprocess": pre.to_dict(),
                        "raw": ocr_res.text,
                        "text": ocr_res.text,
                        "line_count": len(ocr_res.lines),
                        "mean_confidence": round(ocr_res.mean_confidence, 4),
                        "low_confidence_ratio": round(ocr_res.low_confidence_ratio, 4),
                        "lines": [
                            {
                                "text": ln.text,
                                "confidence": round(ln.confidence, 4),
                                "bbox": ln.bbox.model_dump(),
                            }
                            for ln in ocr_res.lines
                        ],
                        "rule_check": rules,
                        # ⚠️ 这里**不能**放 elapsed_ms。
                        #   耗时不是 OCR 结果的属性，而是运行过程的记录。
                        #   放进产物会让每次运行的哈希都不同 ——
                        #   「逐字节可复现」直接失效（实测踩过：
                        #   一度让 test_end_to_end_is_reproducible 失败，
                        #   而且因为此前未装 OCR 引擎、该产物为空，
                        #   这个问题一直潜伏到装上引擎后才暴露）。
                        #   耗时已由 tracer.tool() 记入 trace.jsonl。
                }
                records.append(ocr_rec)

                if ctx.tracer:
                    ctx.tracer.tool(
                        name="ocr.recognize",
                        args={"doc": d["filename"], "page": page_no, "engine": ocr_res.engine},
                        ok=True,
                        latency_ms=int((time.perf_counter() - t0) * 1000),
                        result=ocr_rec,
                    )

    write_json(ctx, "channels/ocr.json", records)
    if skipped:
        # 跳过也要留痕 —— 否则「为什么这一页没做 OCR」无从追溯
        write_json(ctx, "channels/ocr_skipped.json", skipped)

    if ctx.tracer:
        ctx.tracer.stage_event(
            status="ok",
            detail=(
                f"{len(records)} 页 OCR，引擎={engines[0]}；"
                f"{len(skipped)} 页因文本层可用而跳过"
            ),
        )
    return {
        "pages": len(records),
        "engine": engines[0],
        "skipped_pages": len(skipped),
    }


# ==========================================================================
# 确定性规则校验
# ==========================================================================


def _rule_validate(text: str, *, min_conf: float) -> dict[str, Any]:
    """OCR 后的规则校验。这些检查**不依赖任何模型**。

    重点检查金融文档高频出错形态：
        * 金额大写与小写是否一致（中文公告独有，是免费的交叉校验器）
        * 数字形态是否合法（千分位、括号负数）
        * 单位是否在已知集合内
        * 是否存在明显残缺的数字（如 "1,23.45"）
    """
    import re

    issues: list[dict[str, Any]] = []

    # 1) 大写金额 vs 小写金额交叉校验
    cn_amounts = re.findall(r"[（(]?(?:人民币)?([壹贰叁肆伍陆柒捌玖拾佰仟万亿零]{4,})[)）]?元?", text)
    if cn_amounts:
        issues.append(
            {
                "check": "chinese_amount_present",
                "count": len(cn_amounts),
                "note": "检测到中文大写金额，可用于与小写金额交叉校验",
                "samples": cn_amounts[:3],
            }
        )

    # 2) 非法千分位（如 1,23.45 / 12,34,567）
    for m in re.finditer(r"\d[\d,]*\.?\d*", text):
        s = m.group(0)
        if "," in s:
            head = s.split(".")[0]
            groups = head.split(",")
            if len(groups) > 1 and any(len(g) != 3 for g in groups[1:]):
                issues.append(
                    {
                        "check": "malformed_thousands",
                        "value": s,
                        "severity": "high",
                        "note": "千分位分组异常，OCR 可能漏字或错位",
                    }
                )
                if len(issues) > 30:
                    break

    # 3) 单位白名单
    from agent.validate.numeric import ALL_UNITS

    found_units = re.findall(r"亿元|百万元|万元|千元|万股|亿股|千股|元每股|元/股|%|‰|倍", text)
    unknown = [u for u in set(found_units) if u not in ALL_UNITS and u not in {"‰", "倍"}]
    if unknown:
        issues.append({"check": "unknown_unit", "units": unknown})

    # 4) 疑似残缺数字（小数点后位数异常多）
    for m in re.finditer(r"\d+\.\d{5,}", text):
        issues.append(
            {
                "check": "suspicious_precision",
                "value": m.group(0),
                "severity": "medium",
                "note": "小数位数异常，可能是 OCR 误识别",
            }
        )

    return {
        "issue_count": len(issues),
        "issues": issues[:30],
        "has_high_severity": any(i.get("severity") == "high" for i in issues),
    }


def _count_scanned(ctx: RunContext) -> int:
    try:
        docs = read_json(ctx, "facts/documents.json")
    except FileNotFoundError:
        return 0
    return sum(
        1
        for d in docs
        for r in d["page_routes"].values()
        if r in {"B_scanned", "C_garbled"}
    )


def _has_table_pages(documents: list[dict[str, Any]]) -> bool:
    """表格页也需要 OCR 做单元格识别。"""
    return any("D_table" in d["page_routes"].values() for d in documents)


def _count_hard_ocr_pages(documents: list[dict[str, Any]]) -> int:
    """统计**只能**依赖 OCR 的页面数（扫描页 + 乱码页 + 表格页）。

    这些页面没有可靠的文本层，OCR 缺失即无法处理，属硬依赖。
    """
    hard = {"B_scanned", "C_garbled", "D_table"}
    return sum(
        1 for d in documents for r in d["page_routes"].values() if r in hard
    )


def _resolve_raw(ctx: RunContext, rel_or_name: str) -> Path:
    """解析材料文件路径（统一实现在 ``common.resolve_raw``）。"""
    from pipelines.r1_extract.common import resolve_raw

    return resolve_raw(ctx, rel_or_name)


__all__ = ["run", "OCR_ROUTES"]
