"""R1 提取管线的共享工具：源文档发现、页码路由决策、结果落盘。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.evidence.models import BBox, Chunk, PageRoute, SourceLayer
from agent.runtime.loop import RunContext
from domain.tools.document import PageProbe, open_pdf, probe_page

SUPPORTED_SUFFIXES = {".pdf"}


# ==========================================================================
# 源文档发现
# ==========================================================================


def raw_dir(ctx: RunContext) -> Path:
    """受控原始材料目录。"""
    paths = ctx.configs.get("paths") or {}
    if paths.get("raw"):
        return Path(paths["raw"])
    return Path(ctx.configs.get("data_dir", "data")) / "raw"


def discover_documents(ctx: RunContext, *, limit: int | None = None) -> list[Path]:
    """列出待处理的原始文档。

    **只读 ``data/raw``**（或 ``--data`` 指定的材料目录），不接受任意路径 ——
    这是「受控数据环境」的落点。

    上限来源优先级：显式参数 > ``ctx.configs['doc_limit']``（CLI ``--limit``）。
    真实材料动辄数百份文档，必须能在小样本上快速迭代。
    """
    root = raw_dir(ctx)
    if not root.exists():
        return []
    files = sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
    )
    cap = limit if limit is not None else ctx.configs.get("doc_limit")
    if cap:
        files = files[: int(cap)]
    return files


def resolve_raw(ctx: RunContext, rel_or_name: str) -> Path:
    """把登记时记录的路径解析回真实文件。

    三级兜底（真实材料常按子目录组织，一级解析会失败）：
        1. 直接拼接材料根目录（相对路径命中）
        2. 按文件名在材料根目录下递归查找
        3. 原样返回，交由调用方抛出带上下文的信息

    这是**唯一**的解析实现 —— 三个阶段各自实现一遍曾导致
    「一个阶段修好了、另一个还在用旧的扁平假设」这类不一致。
    """
    root = raw_dir(ctx)
    direct = root / rel_or_name
    if direct.is_file():
        return direct

    name = Path(rel_or_name).name
    hits = sorted(root.rglob(name)) if root.exists() else []
    if hits:
        return hits[0]

    raise FileNotFoundError(
        f"材料文件不存在：{rel_or_name}",
        searched_root=str(root),
        hint="检查 data/raw（或 --data 指定目录）下的文件是否被移动或删除",
    )


# ==========================================================================
# 路由决策
# ==========================================================================

DEFAULT_ROUTING: dict[str, Any] = {
    "garbled_ratio_threshold": 0.15,
    "min_chars_for_text_layer": 50,
    "min_chars_for_text_only": 200,
    "image_coverage_threshold": 0.60,
    "text_coverage_threshold": 0.12,
    "clean_garbled_threshold": 0.05,
    "rule_line_density_threshold": 40,
}


def decide_route(probe: PageProbe, routing: dict[str, Any] | None = None) -> tuple[PageRoute, str]:
    """逐页路由决策。**纯确定性**，返回 ``(路由, 判定理由)``。

    理由字符串会写进 trace.jsonl，让「为什么这一页走 OCR」永远可查。

    决策顺序（从强到弱）：
        1. 乱码严重        -> C_GARBLED（文本层不可信）
        2. 无文本 + 有图   -> B_SCANNED（典型扫描件）
        3. 有框线          -> D_TABLE（表格需专门处理）
        4. 文本充足且干净  -> A_TEXT_ONLY（最高置信）
        5. 其余            -> E_MIXED

    重要设计说明：判定「页面有没有文字」用的是
    ``char_count``（有效字符数）而**不是** ``text_coverage``。
    原因是 text_coverage = 文本块 bbox 面积 / 页面面积，会因行距与页边距
    天然偏低（正常 A4 正文页仅 0.2–0.4），用它判空会造成大面积误判。
    """
    r = {**DEFAULT_ROUTING, **(routing or {})}

    if probe.garbled_ratio > r["garbled_ratio_threshold"]:
        return (
            PageRoute.C_GARBLED,
            f"乱码字符占比 {probe.garbled_ratio:.1%} > 阈值 {r['garbled_ratio_threshold']:.0%}，"
            "文本层不可信，转 OCR + VLM 双路对读",
        )

    if probe.char_count < r["min_chars_for_text_layer"]:
        if probe.image_coverage > r["image_coverage_threshold"]:
            return (
                PageRoute.B_SCANNED,
                f"有效字符仅 {probe.char_count} 个（< {r['min_chars_for_text_layer']}）"
                f"且图像覆盖率 {probe.image_coverage:.1%} > {r['image_coverage_threshold']:.0%}，"
                "判定为扫描页，转 OCR + VLM 双路对读",
            )
        return (
            PageRoute.E_MIXED,
            f"有效字符仅 {probe.char_count} 个但图像覆盖率也低（{probe.image_coverage:.1%}），"
            "疑似空白页或纯图形页，走混合路径谨慎处理",
        )

    if probe.rule_line_density >= r["rule_line_density_threshold"]:
        return (
            PageRoute.D_TABLE,
            f"检测到 {probe.rule_line_density} 条表框线段"
            f"（>= {r['rule_line_density_threshold']}），判定为表格页，走三引擎表决",
        )

    if (
        probe.char_count >= r["min_chars_for_text_only"]
        and probe.garbled_ratio < r["clean_garbled_threshold"]
    ):
        return (
            PageRoute.A_TEXT_ONLY,
            f"有效字符 {probe.char_count} 个（>= {r['min_chars_for_text_only']}）"
            f"且乱码率 {probe.garbled_ratio:.2%} < {r['clean_garbled_threshold']:.0%}，"
            f"文本层干净可用（文本覆盖 {probe.text_coverage:.1%}），直取最高置信",
        )

    return (
        PageRoute.E_MIXED,
        f"文本量中等（{probe.char_count} 字符，覆盖 {probe.text_coverage:.1%}）"
        f"或含图像（覆盖 {probe.image_coverage:.1%}），"
        "文本层为主 + 局部图像补强",
    )


def base_confidence_for_route(route: PageRoute) -> float:
    """路由 -> 该页抽取结果的基础置信度上限。"""
    return {
        PageRoute.A_TEXT_ONLY: 0.95,
        PageRoute.D_TABLE: 0.90,
        PageRoute.E_MIXED: 0.85,
        PageRoute.B_SCANNED: 0.80,
        PageRoute.C_GARBLED: 0.75,
    }.get(route, 0.80)


# ==========================================================================
# 落盘
# ==========================================================================


def write_json(ctx: RunContext, rel_path: str, obj: Any) -> Path:
    """写 JSON 产物到 run 目录。**走 guard**，每次落盘都在 trace 里留一条。"""
    return ctx.guard().write_text(
        ctx.run_dir / rel_path,
        json.dumps(obj, ensure_ascii=False, indent=2, default=_default) + "\n",
    )


def read_json(ctx: RunContext, rel_path: str) -> Any:
    return ctx.guard().read_json(ctx.run_dir / rel_path)


def open_source(ctx: RunContext, path: str | Path) -> Any:
    """打开一份**原始材料** PDF，并把这次读取记进 trace。

    为什么需要这一层：`fitz.open()` 在 C 扩展里开文件，Python 侧包不住它 ——
    `open_pdf` 换成 `guard.read_bytes` 再交给 PyMuPDF 会把整份年报读进内存
    （动辄几十 MB × 5 个阶段），因此这里改成「照旧由 PyMuPDF 打开，guard
    在旁边记一笔」（`FileGuard.record`）。记录里带 sha256，所以「这一页是从
    哪一份、哪一版材料里读出来的」逐字节可核验。
    """
    return open_pdf(ctx.guard().record(path, "read"))


def record_output(ctx: RunContext, path: str | Path, mode: str = "write") -> Path:
    """登记一次由**第三方库**完成的落盘（matplotlib / openpyxl / PyMuPDF）。

    同 :func:`open_source`：这些库的写盘进不来，而改写成 `guard.write_bytes`
    有可能差一个字节 —— 产物哈希必须稳定，所以宁可口径上退一步、由调用方
    在写完之后补一次登记。
    """
    return ctx.guard().record(path, mode)  # type: ignore[arg-type]


# ==========================================================================
# 内存证据库的**重水化**
# ==========================================================================


def hydrate_store_from_channels(ctx: RunContext) -> dict[str, int]:
    """把已落盘的通道产物灌回内存证据库；返回灌入条数。

    ==========================================================================
    ★★ 为什么必须有这个函数（2026-09 第十八轮实测，代价是一次完整预热）
    ==========================================================================
    `extract_text` 是**唯一**把「权威页面原文」注册进 ``ctx.store`` 的地方
    （``set_page_text`` / ``set_page_blocks`` / ``set_statement_titles``），
    `extract_ocr` 注册 OCR 辅助文本。而 ``ctx.store`` 是**纯内存对象**。

    于是 ``--resume`` 一旦跳过 ``extract_text``，下游拿到的就是一个**空库**：

        build_facts 读 ctx.store.page_text(doc_id, page)      → ""
        → find_unit_declaration("") 找不到「单位：万元」      → unit = ""
        → 1590 行按「无单位」拒绝                             → **0 条事实**

    实测（annual41-seg 复制一份、回退检查点重跑 judge 起）：

        第一代（完整跑）        事实 1050 条，table_grid_candidates 1186，no_unit    0
        第二代（--resume 重跑） 事实    0 条，table_grid_candidates   23，no_unit 1590

    而**回放**（每个阶段都跑）又是好的 —— 所以回放说「不可复现」，
    真正的问题是续跑**把内存状态丢了**，而且**不报任何错**。

    本函数的判据是：产物里有的，就能灌回来。灌不回来的（``page_blocks`` /
    ``statement_titles`` 目前未落盘）由调用方按缺口处理 —— 见
    :func:`assert_store_has_page_text`。
    """
    counts = {"page_text": 0, "chunks": 0, "chunks_failed": 0, "aux_text": 0}
    layout = _read_json_optional(ctx, "channels/page_layout.json") or {}
    blocks_by_page = {
        (b["doc_id"], int(b["page"])): b.get("blocks") or []
        for b in (layout.get("blocks") or [])
    }
    titles = layout.get("statement_titles") or {}

    for rec in _read_json_optional(ctx, "channels/text_layer.json") or []:
        doc_id, page = rec.get("doc_id"), int(rec.get("page") or 0)
        if not doc_id or not page:
            continue
        raw = rec.get("raw") or rec.get("text") or ""
        if raw:
            ctx.store.set_page_text(doc_id, page, raw)
            counts["page_text"] += 1
        # ⚠️ 落盘的 chunk 只有 {chunk_id, bbox, reading_order, char_len, text}，
        #    缺 doc_id / page / source_layer —— 而 Chunk 是 extra="forbid" 的
        #    严格模型。直接 model_validate 会**每一条都失败**，而如果那一步
        #    用 `except: continue` 吞掉，就会静默地一条块都灌不回来，
        #    于是 _locate_chunk 退化成占位块、span_id 全变、产物哈希对不上。
        #    （这不是假设：本函数第一版就是这么写的，实测被哈希比对抓出来。）
        for c in rec.get("chunks") or []:
            try:
                ctx.store.add_chunk(
                    Chunk(
                        chunk_id=c["chunk_id"],
                        doc_id=doc_id,
                        page=page,
                        bbox=BBox.model_validate(c["bbox"]),
                        text=c.get("text") or "",
                        source_layer=SourceLayer.L1_TEXT_LAYER,
                        reading_order=int(c.get("reading_order") or 0),
                    )
                )
                counts["chunks"] += 1
            except Exception:  # noqa: BLE001 - 老产物可能字段不全
                counts["chunks_failed"] += 1
        if (doc_id, page) in blocks_by_page:
            ctx.store.set_page_blocks(doc_id, page, blocks_by_page[(doc_id, page)])

    for doc_id, ts in titles.items():
        if ts:
            ctx.store.set_statement_titles(doc_id, ts)

    for rec in _read_json_optional(ctx, "channels/ocr.json") or []:
        doc_id, page = rec.get("doc_id"), int(rec.get("page") or 0)
        text = rec.get("text") or rec.get("raw") or ""
        if doc_id and page and text:
            ctx.store.add_aux_text(doc_id, page, text)
            counts["aux_text"] += 1

    return counts


def _read_json_optional(ctx: RunContext, rel_path: str) -> Any:
    p = ctx.run_dir / rel_path
    if not p.is_file():
        return None
    try:
        return json.loads(ctx.guard().read_text(p))
    except json.JSONDecodeError:
        return None


def assert_store_has_page_text(ctx: RunContext, doc_ids: list[str]) -> None:
    """证据库里一页权威原文都没有 ⇒ **硬失败**，绝不静默产出 0 条事实。

    这条闸门是本轮最贵的教训换来的：丢内存状态时，流水线**不报错**，
    只是安静地把每一条候选都按「无单位」拒掉，最后交出一个 0 条的事实库。
    一个「0 条」和一个「真的没有可抽的数字」在产物里长得一模一样。

    判据用「文档 ⇄ 该文档在册页数」而不是盲扫页码：``ctx.store`` 里有
    ``page_count`` 登记（由 ingest 落盘、可从 ``facts/documents.json`` 取回）。
    """
    from agent.runtime.errors import HarnessError

    for d in doc_ids:
        n = _page_count_of(ctx, d)
        for p in range(1, n + 1):
            if ctx.store.page_text(d, p):
                return
    raise HarnessError(
        "证据库里没有任何「权威页面原文」—— 下游会把每一条候选都按『无单位』拒掉，"
        "最终交出一个 0 条的事实库，而且不报错。\n"
        "最常见原因：**`--resume` 跳过了 extract_text**（它是唯一注册页面原文的阶段），"
        "而 ctx.store 是纯内存对象，跳过就等于丢失。\n"
        "处理：先调用 pipelines.r1_extract.common.hydrate_store_from_channels(ctx) "
        "把已落盘的 channels/*.json 灌回内存库；或不要用 --resume、整轮重跑。",
        doc_ids=doc_ids,
    )


def _page_count_of(ctx: RunContext, doc_id: str) -> int:
    for d in _read_json_optional(ctx, "facts/documents.json") or []:
        if d.get("doc_id") == doc_id:
            return int(d.get("page_count") or 0)
    return 0


def _default(o: Any) -> Any:
    from decimal import Decimal

    if isinstance(o, Decimal):
        return str(o)
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    if hasattr(o, "isoformat"):
        return o.isoformat()
    raise TypeError(f"无法序列化 {type(o)!r}")


def load_routing(ctx: RunContext) -> dict[str, Any]:
    return {**DEFAULT_ROUTING, **(ctx.configs.get("routing") or {})}


__all__ = [
    "SUPPORTED_SUFFIXES",
    "raw_dir",
    "discover_documents",
    "resolve_raw",
    "decide_route",
    "base_confidence_for_route",
    "write_json",
    "read_json",
    "open_source",
    "record_output",
    "load_routing",
    "hydrate_store_from_channels",
    "assert_store_has_page_text",
    "DEFAULT_ROUTING",
]
