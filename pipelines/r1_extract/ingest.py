"""阶段 1：文档登记与逐页预扫描（纯确定性，无 LLM）。"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from agent.runtime.loop import RunContext
from domain.tools.document import compute_sha256, probe_page
from pipelines.r1_extract.common import (
    decide_route,
    discover_documents,
    load_routing,
    open_source,
    raw_dir,
    write_json,
)

# 期间识别：年度 / 半年 / 季度报告
_YEAR_PATTERN = re.compile(
    r"(20\d{2})\s*年?\s*(半年度|半年|前三季度|第三季度|第一季度|一季度|年度|全年|年报)?"
)
# 明确排除的上下文（这些年份不是报告期）
_NOT_PERIOD_CONTEXT = ("成立", "上市", "上市时间", "注册", "设立", "未来", "预计", "目标")


def detect_document_period(page_texts: list[str]) -> str:
    """从文档全文推断**报告期**。

    为什么需要这个：很多页（尤其是续表的后续页）并不重复印刷年份，
    只有表头的「单位：万元」。若每页独立推断，这些页的期间会大面积缺失，
    而期间缺失会让 R5 的口径比对完全失效（不同报告期的数字无法比较）。

    做法：全文档统计年份出现频次，取最高频者；再结合该年份附近
    出现的期间形态词（半年/前三季度/年度）确定后缀。
    """
    blob = "\n".join(page_texts)
    counter: Counter[str] = Counter()
    suffixes: dict[str, Counter[str]] = {}

    for m in _YEAR_PATTERN.finditer(blob):
        year, span = m.group(1), (m.group(2) or "")
        ctx = blob[max(0, m.start() - 12) : m.start()]
        if any(k in ctx for k in _NOT_PERIOD_CONTEXT):
            continue
        counter[year] += 1
        suffix = {
            "半年度": "H1", "半年": "H1", "第一季度": "Q1", "一季度": "Q1",
            "第三季度": "Q3", "前三季度": "Q3", "年度": "A", "全年": "A",
            "年报": "A", "": "A",
        }.get(span, "A")
        suffixes.setdefault(year, Counter())[suffix] += 1

    if not counter:
        return "unknown"

    year = counter.most_common(1)[0][0]
    suffix = suffixes[year].most_common(1)[0][0] if suffixes.get(year) else "A"
    return f"{year}{suffix}"


def run(ctx: RunContext) -> dict[str, Any]:
    """登记 data/raw 下的全部文档，逐页预扫描并做路由决策。

    产物：
        runs/<id>/facts/documents.json  —— 文档清单 + 逐页路由
        runs/<id>/pages/index.json      —— 逐页预扫描指标（供 Web 面板）
    """
    routing = load_routing(ctx)
    max_pages = int(routing.get("max_pages_per_doc", 500))
    max_mb = float(routing.get("max_file_size_mb", 200))

    files = discover_documents(ctx)
    if not files:
        raise FileNotFoundError(
            f"data/raw/ 下没有找到任何 PDF。\n"
            f"请把受控原始材料放入：{ctx.configs.get('paths', {}).get('raw')}"
        )

    documents: list[dict[str, Any]] = []
    pages_index: list[dict[str, Any]] = []
    route_counts: dict[str, int] = {}

    for path in files:
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > max_mb:
            if ctx.tracer:
                ctx.tracer.error(
                    where="r1.ingest",
                    message=f"{path.name} 大小 {size_mb:.1f}MB 超过上限 {max_mb}MB，跳过",
                )
            ctx.ledger.reject("文件过大", file=path.name, size_mb=round(size_mb, 1))
            continue

        sha = compute_sha256(path)
        doc_id = f"doc_{sha[:12]}"
        # ★ 保存**相对于材料根目录的路径**，而不是文件名。
        #   真实材料常按子目录组织（如 agent/sample/_scratch/xxx.pdf），
        #   只存文件名会导致后续阶段找不到文件（实测踩过）。
        try:
            rel_path = str(path.relative_to(raw_dir(ctx))).replace("\\", "/")
        except ValueError:
            rel_path = path.name

        with open_source(ctx, path) as doc:
            page_count = doc.page_count
            n = min(page_count, max_pages)
            if page_count > max_pages:
                ctx.ledger.reject(
                    "页数超限，仅处理前 N 页", file=path.name, page_count=page_count, limit=max_pages
                )

            page_routes: dict[int, str] = {}
            route_reasons: dict[int, str] = {}
            has_text = False
            page_texts: list[str] = []

            for i in range(n):
                page_no = i + 1
                page_obj = doc.load_page(i)
                probe = probe_page(page_obj, page_no)
                route, reason = decide_route(probe, routing)

                page_routes[page_no] = route.value
                route_reasons[page_no] = reason
                route_counts[route.value] = route_counts.get(route.value, 0) + 1
                if probe.char_count >= int(routing.get("min_chars_for_text_layer", 50)):
                    has_text = True

                # 采集页面原文，供文档级期间推断使用
                page_texts.append(page_obj.get_text("text"))

                pages_index.append(
                    {
                        "doc_id": doc_id,
                        "file": path.name,
                        "rel_path": rel_path,
                        **probe.to_dict(),
                        "route": route.value,
                        "route_reason": reason,
                    }
                )
                if ctx.tracer:
                    ctx.tracer.routing(page=page_no, route=route.value, reason=reason)

            # 文档级报告期 —— 供页级推断失败时回退
            doc_period = detect_document_period(page_texts)
            if ctx.tracer:
                ctx.tracer.stage_event(
                    status="ok",
                    detail=f"{path.name} 识别报告期：{doc_period}",
                )

        documents.append(
            {
                "doc_id": doc_id,
                "filename": path.name,
                "rel_path": rel_path,
                "sha256": sha,
                "page_count": page_count,
                "has_text_layer": has_text,
                "document_period": doc_period,
                "page_routes": page_routes,
                "route_reasons": route_reasons,
                "parser_backend": "pymupdf",
                "size_mb": round(size_mb, 2),
                # ★ 取自 run 的统一时间戳，而不是 utcnow()。
                #   否则回放时该字段会变，facts.json 哈希不一致，
                #   「可复现」在机器校验层面不成立。
                "registered_at": ctx.build_stamp().isoformat(),
            }
        )

        # 记录输入文件哈希 -> 复现依据（用相对路径，避免绝对路径入库）
        if ctx.manifest is not None:
            ctx.manifest.input_files[rel_path] = sha

    write_json(ctx, "facts/documents.json", documents)
    write_json(ctx, "pages/index.json", pages_index)

    for route, cnt in sorted(route_counts.items()):
        print(f"    {route:<14} {cnt:>4} 页")

    return {
        "documents": len(documents),
        "pages": len(pages_index),
        "routes": route_counts,
    }


__all__ = ["run"]
