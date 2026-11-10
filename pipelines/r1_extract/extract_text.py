"""阶段 2：文本层直取（三层引擎的第 1 层，最高置信）。

只处理路由为 ``A_TEXT_ONLY`` / ``E_MIXED`` / ``D_TABLE`` 的页面。

产出的通道读数写入 ``channels/text_layer.jsonl``，供阶段 4 的 VLM 裁判
作为「通道 A」输入 —— **不直接生成 Fact**，必须先经过交叉验证。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.evidence.models import BBox, Chunk, SourceLayer
from agent.runtime.loop import RunContext
from domain.tools.document import extract_chunks
from pipelines.r1_extract.common import open_source, read_json, write_json
from pipelines.r1_extract.table_facts import statement_scope_of_title

# 数值 + 单位的通用形态，用于从整页文本里定位候选字段
_NUM_UNIT = (
    r"[（(]?-?\d[\d,]*(?:\.\d+)?[)）]?\s*"
    r"(?:亿元|百万元|万元|千元|元/股|元每股|亿股|万股|千股|股|%|％|‰|倍)?"
)


def run(ctx: RunContext) -> dict[str, Any]:
    documents = read_json(ctx, "facts/documents.json")
    text_routes = {"A_text_only", "E_mixed", "D_table"}

    channel_records: list[dict[str, Any]] = []
    total_chunks = 0
    # 块级坐标与全文报表标题：原本只注册进内存证据库，现在同步落盘，
    # 好让 `--resume` 跳过本阶段后仍能把它们灌回来（见 common 里的水化函数）。
    layout_records: list[dict[str, Any]] = []
    statement_titles_all: dict[str, list[dict[str, Any]]] = {}

    for d in documents:
        path = _resolve_raw(ctx, d.get("rel_path") or d["filename"])
        wanted = sorted(
            int(p) for p, r in d["page_routes"].items() if r in text_routes
        )
        if not wanted:
            continue

        with open_source(ctx, path) as doc:
            # ★ **全文档级**报表标题扫描。
            #
            #   为什么必须扫全文而不是"看本页/继承上一页"：年报里合并表与母公司表
            #   的页序可能交错（000722：合并 p73-74 → 母公司 p75 → 合并 p76-77），
            #   按上下文继承必然张冠李戴 —— 实测把合并表标成了母公司表。
            #   一张表属于它**前面最近的那个报表标题**，因此要先把全文标题扫出来。
            titles_all: list[dict[str, Any]] = []
            for pno in range(1, min(doc.page_count, 600) + 1):
                for blk in doc.load_page(pno - 1).get_text("blocks"):
                    text = str(blk[4]).strip().replace("\n", "")
                    if not text or len(text) > 30:
                        continue
                    scope = statement_scope_of_title(text)
                    if scope:
                        titles_all.append(
                            {
                                "page": pno,
                                "y0": float(blk[1]),
                                # 完整矩形：口径配对靠矩形交叠（见 store.scope_by_overlap）
                                "bbox": [
                                    float(blk[0]), float(blk[1]),
                                    float(blk[2]), float(blk[3]),
                                ],
                                "scope": scope,
                                "text": text,
                            }
                        )
            ctx.store.set_statement_titles(d["doc_id"], titles_all)
            statement_titles_all[d["doc_id"]] = titles_all

            for page_no in wanted:
                page_obj = doc.load_page(page_no - 1)
                chunks = extract_chunks(page_obj, d["doc_id"], page_no)
                for c in chunks:
                    ctx.store.add_chunk(c)
                total_chunks += len(chunks)

                # ★ 权威页面文本用 get_text("text")，
                #   **不是**把 chunk 拼起来。原因：get_text("dict") 返回的块顺序
                #   是 PDF 内容流的写入顺序，与阅读顺序不一致；而 quote 回读要求
                #   证据片段是页面文本的**逐字子串**。用拼出来的文本会导致
                #   quote 定位漂移、期间推断丢失（实测踩过这两个坑）。
                raw_text = page_obj.get_text("text")
                ordered_text = "\n".join(c.text for c in chunks)

                # 注册到证据库：quote 回读以 raw_text 为准
                ctx.store.set_page_text(d["doc_id"], page_no, raw_text)
                # 保留**块级坐标**：报表口径（合并/母公司）只能靠报表标题判断，
                # 而标题在正文层、不在表格网格里，必须靠纵坐标把两者对齐。
                ctx.store.set_page_blocks(
                    d["doc_id"],
                    page_no,
                    [
                        {
                            "text": str(b[4]),
                            # 完整矩形（x0,y0,x1,y1）：口径配对靠**矩形交叠**，
                            # 只留 y0 不够 —— 实测按坐标先后排序必然判错
                            "bbox": [float(b[0]), float(b[1]), float(b[2]), float(b[3])],
                            "y0": float(b[1]),
                        }
                        for b in page_obj.get_text("blocks")
                    ],
                )

                channel_records.append(
                    {
                        "doc_id": d["doc_id"],
                        "file": d["filename"],
                        "page": page_no,
                        "channel": "text_layer",
                        "source_layer": SourceLayer.L1_TEXT_LAYER.value,
                        # raw: 权威原文（用于抽取与 quote 回读）
                        "raw": raw_text,
                        # text: 按阅读顺序重排的文本（仅用于展示）
                        "text": ordered_text,
                        "chunk_count": len(chunks),
                        "chunks": [
                            {
                                "chunk_id": c.chunk_id,
                                "bbox": c.bbox.model_dump(),
                                "reading_order": c.reading_order,
                                "char_len": len(c.text),
                                "text": c.text,
                            }
                            for c in chunks
                        ],
                        "candidates": _extract_candidates(raw_text),
                    }
                )
                # ★ 块级坐标必须**落盘**。它是报表口径（合并/母公司）判定的
                #   唯一依据，原本只活在内存证据库里；一旦 `--resume` 跳过本阶段，
                #   下游就再也拿不到它。第十八轮实测：内存库空掉会让 build_facts
                #   交出 0 条事实且不报错（见 common.hydrate_store_from_channels）。
                layout_records.append(
                    {
                        "doc_id": d["doc_id"],
                        "page": page_no,
                        "blocks": [
                            {
                                "text": str(b[4]),
                                "bbox": [float(b[0]), float(b[1]), float(b[2]), float(b[3])],
                                "y0": float(b[1]),
                            }
                            for b in page_obj.get_text("blocks")
                        ],
                    }
                )

    write_json(ctx, "channels/text_layer.json", channel_records)
    write_json(
        ctx,
        "channels/page_layout.json",
        {"blocks": layout_records, "statement_titles": statement_titles_all},
    )

    if ctx.tracer:
        ctx.tracer.stage_event(
            status="ok",
            detail=f"{len(channel_records)} 页，{total_chunks} 个文本块",
        )
    return {"pages": len(channel_records), "chunks": total_chunks}


def _extract_candidates(page_text: str) -> list[dict[str, Any]]:
    """从页面文本中定位「数值+单位」候选串。

    这一步**不做语义归属**（不知道这个数字属于哪个指标），
    只做形态识别。语义归属由阶段 3/4 的 LLM 与规则共同完成。
    """
    import re

    out: list[dict[str, Any]] = []
    for m in re.finditer(_NUM_UNIT, page_text):
        raw = m.group(0).strip()
        if not raw or raw in {"-", "—"}:
            continue
        out.append(
            {
                "raw": raw,
                "start": m.start(),
                "end": m.end(),
                "context": page_text[max(0, m.start() - 30) : m.end() + 30],
            }
        )
    return out[:400]  # 单页候选上限，防跑飞


def _resolve_raw(ctx: RunContext, rel_or_name: str) -> Path:
    """解析材料文件路径（统一实现在 ``common.resolve_raw``）。

    这里曾各自实现一遍，结果是修好一个、另两个仍用旧的扁平假设 ——
    真实材料按子目录组织时就会 FileNotFoundError。现在只有一处实现。
    """
    from pipelines.r1_extract.common import resolve_raw

    return resolve_raw(ctx, rel_or_name)


__all__ = ["run"]
