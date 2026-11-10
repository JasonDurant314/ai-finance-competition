"""阶段 6：导出事实库产物（JSON / Excel / 溯源 HTML）。

三个产物对应三类消费者：
    facts.json    机器消费（R4 估值、R5 核查的输入）
    facts.xlsx    评审直接看的规范表格
    evidence.html 溯源核验 —— 每条事实点开可看原文片段与置信度明细
"""

from __future__ import annotations

import html
import json
from decimal import Decimal
from typing import Any

from agent.evidence.models import FactSet
from agent.runtime.loop import RunContext
from domain.tools.xlsx import normalize_xlsx_metadata
from pipelines.r1_extract.common import read_json, record_output, write_json

# ---------------------------------------------------------------------------
# 溯源 HTML —— 这是决赛演示最关键的一屏，因此自包含、无外部依赖
# ---------------------------------------------------------------------------

_HTML_HEAD = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>事实库溯源核验 — 金融投研智能体</title>
<style>
:root{--bg:#0f1419;--panel:#1a2028;--line:#2a333f;--fg:#e6edf3;--dim:#8b98a5;
      --ok:#3fb950;--warn:#d29922;--bad:#f85149;--accent:#58a6ff;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{padding:18px 24px;border-bottom:1px solid var(--line);background:var(--panel)}
h1{margin:0;font-size:18px;font-weight:600}
.sub{color:var(--dim);font-size:12px;margin-top:4px}
.stats{display:flex;gap:24px;padding:14px 24px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.stat{font-size:12px;color:var(--dim)}
.stat b{display:block;font-size:20px;color:var(--fg);font-weight:600}
main{padding:16px 24px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--dim);font-weight:500;position:sticky;top:0;background:var(--bg);cursor:pointer}
tr:hover{background:#161c23}
.tag{display:inline-block;padding:1px 6px;border-radius:3px;font-size:11px;border:1px solid}
.tag.L1{color:var(--ok);border-color:var(--ok)}
.tag.L2{color:var(--warn);border-color:var(--warn)}
.tag.L3{color:var(--bad);border-color:var(--bad)}
.tag.L4{color:var(--accent);border-color:var(--accent)}
.conf{font-variant-numeric:tabular-nums}
.quote{color:var(--dim);font-size:12px;max-width:420px}
.quote mark{background:#3d2f00;color:#ffd866;padding:1px 2px;border-radius:2px}
details{margin-top:4px}
summary{cursor:pointer;color:var(--accent);font-size:12px}
pre{background:#0b0f14;padding:8px;border-radius:4px;overflow:auto;font-size:11px;color:var(--dim)}
.review{color:var(--warn)}
footer{padding:16px 24px;color:var(--dim);font-size:12px;border-top:1px solid var(--line);margin-top:24px}
</style>
</head>
<body>
"""


def build_evidence_html(fact_set: FactSet, ctx: RunContext) -> str:
    """生成自包含的溯源核验页面。"""
    facts = fact_set.facts
    easy = sum(1 for f in facts if not f.needs_review)
    l1 = sum(1 for f in facts if f.source_layer.value == "L1_text_layer")
    l2 = sum(1 for f in facts if f.source_layer.value == "L2_ocr")
    l3 = sum(1 for f in facts if f.source_layer.value == "L3_vlm")
    avg_conf = (
        sum(f.confidence for f in facts) / len(facts) if facts else 0.0
    )

    rows: list[str] = []
    for f in facts:
        spans = ctx.store.spans_of(f)
        span = spans[0] if spans else None
        quote_html = ""
        if span:
            q = html.escape(span.quote)
            # 高亮原文中的目标数值
            needle = html.escape(f.raw_text)
            if needle and needle in q:
                q = q.replace(needle, f"<mark>{needle}</mark>", 1)
            quote_html = (
                f'<div class="quote">「{q}」'
                f'<br><span style="font-size:11px">第 {span.page} 页 · '
                f'{html.escape(span.doc_id)}</span></div>'
            )

        readings = "".join(
            f"<div>· {html.escape(c.channel)}: {html.escape(str(c.raw_text))} "
            f"(conf {c.confidence:.2f})</div>"
            for c in f.channel_readings
        )
        breakdown = html.escape(f.confidence_breakdown.explain())
        layer = f.source_layer.value.split("_")[0]
        review_mark = (
            f'<span class="review" title="{html.escape(f.review_reason or "")}">⚠️ 需复核</span>'
            if f.needs_review else ""
        )

        rows.append(
            f"""<tr>
  <td><code style="font-size:11px">{html.escape(f.fact_id)}</code></td>
  <td>{html.escape(f.entity)}</td>
  <td><b>{html.escape(f.metric)}</b></td>
  <td style="text-align:right;font-variant-numeric:tabular-nums">{html.escape(str(f.value))}</td>
  <td>{html.escape(f.unit)}</td>
  <td>{html.escape(f.period)}<br>
      <span style="font-size:11px;color:var(--dim)">{f.period_type.value} /
      {f.statement_scope.value} / {f.profit_basis.value}</span></td>
  <td><span class="tag {layer}">{html.escape(f.source_layer.value)}</span></td>
  <td class="conf">{f.confidence:.2f}<br>{review_mark}</td>
  <td>{quote_html}
      <details><summary>置信度构成 / 通道读数</summary>
        <pre>{breakdown}
{readings or '（无多通道读数 — 单源提取）'}</pre>
      </details></td>
</tr>"""
        )

    body = f"""<header>
  <h1>事实库溯源核验</h1>
  <div class="sub">run {html.escape(fact_set.run_id)} · 契约版本 {fact_set.contract_version}
   · 构建时间 {fact_set.built_at.isoformat()[:19]}</div>
</header>
<div class="stats">
  <div class="stat"><b>{len(facts)}</b>事实总数</div>
  <div class="stat"><b>{l1}</b>文本层直取</div>
  <div class="stat"><b>{l2}</b>OCR 识别</div>
  <div class="stat"><b>{l3}</b>VLM 读图</div>
  <div class="stat"><b>{avg_conf:.2f}</b>平均置信度</div>
  <div class="stat"><b>{len(facts) - easy}</b>需人工复核</div>
  <div class="stat"><b>{len(fact_set.documents)}</b>源文档</div>
</div>
<main>
<table>
<thead><tr>
  <th>fact_id</th><th>主体</th><th>指标</th><th style="text-align:right">数值</th><th>单位</th>
  <th>报告期 / 口径</th><th>来源层</th><th>置信度</th><th>证据锚点（原文片段）</th>
</tr></thead>
<tbody>
{''.join(rows) if rows else '<tr><td colspan="9">（无事实记录）</td></tr>'}
</tbody>
</table>
</main>
<footer>
  置信度 = 来源层基础分 + 多路一致(+0.05) + VLM确认(+0.10) + 算术校验(+0.05)
  − 模糊匹配(0.05) − 单位异常(0.30) − 逻辑冲突(0.20)，上限 0.99。<br>
  每条事实均可回溯到原文片段；<code>needs_review</code> 的事实不参与下游估值与核查。
</footer>
</body></html>"""
    return _HTML_HEAD + body


# ---------------------------------------------------------------------------
# Excel 导出
# ---------------------------------------------------------------------------


def export_excel(rows: list[dict[str, Any]], path: str) -> bool:
    """导出事实表到 Excel。缺 openpyxl 时返回 False（不阻断流程）。"""
    try:
        import pandas as pd
    except ImportError:
        return False
    try:
        df = pd.DataFrame(rows)
        column_order = [
            "fact_id", "entity", "metric", "value", "unit", "raw_text",
            "period", "period_type", "statement_scope", "profit_basis",
            "source_layer", "confidence", "needs_review", "doc_id", "page", "quote",
        ]
        cols = [c for c in column_order if c in df.columns]
        df = df[cols] if cols else df
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="事实库", index=False)
            ws = writer.sheets["事实库"]
            for i, col in enumerate(df.columns, start=1):
                width = max(10, min(60, int(df[col].astype(str).str.len().max() or 10) + 2))
                ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = width
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 阶段入口
# ---------------------------------------------------------------------------


def run(ctx: RunContext) -> dict[str, Any]:
    from agent.evidence.models import DocumentManifest, Fact

    documents = [
        DocumentManifest.model_validate(
            {k: v for k, v in d.items() if k in DocumentManifest.model_fields}
        )
        for d in read_json(ctx, "facts/documents.json")
    ]

    # ★ 台账（ctx.ledger）也是**内存对象**：`build_facts` 往里放事实，本阶段读出来落盘。
    #   于是 `--resume` 若跳过 build_facts 而只重跑本阶段，台账是空的 ——
    #   会把一份**空事实库**写进 facts.json，而且不报错。
    #   事实库的内容本来就以 facts.json 的形式落过盘，所以直接灌回来即可。
    if ctx.ledger.count == 0 and (ctx.facts_dir / "facts.json").is_file():
        try:
            prev = json.loads(ctx.guard().read_text(ctx.facts_dir / "facts.json"))
            restored = 0
            for raw in prev.get("facts") or []:
                try:
                    ctx.ledger.add(Fact.model_validate(raw))
                    restored += 1
                except Exception:  # noqa: BLE001 - 单条读不回来不该拖垮整体
                    continue
            if restored and ctx.tracer:
                ctx.tracer.stage_event(
                    status="warn",
                    detail=f"台账为空（build_facts 被跳过），已从 facts/facts.json 灌回 {restored} 条",
                )
        except json.JSONDecodeError:
            pass

    fact_set = ctx.ledger.to_fact_set(
        documents=documents,
        built_at=ctx.build_stamp(),
    )
    write_json(ctx, "facts/facts.json", fact_set.model_dump(mode="json"))

    rows = ctx.ledger.to_records()
    write_json(ctx, "facts/facts_table.json", rows)

    excel_ok = export_excel(rows, str(ctx.facts_dir / "facts.xlsx"))
    if excel_ok:
        # 登记要**在元数据归一化之后**：归一化会重写整个 zip，先记的话
        # sha256 对应的是归一化前那份，和交付物对不上。
        normalize_xlsx_metadata(ctx.facts_dir / "facts.xlsx")
        record_output(ctx, ctx.facts_dir / "facts.xlsx")

    ctx.guard().write_text(
        ctx.facts_dir / "evidence.html", build_evidence_html(fact_set, ctx)
    )

    summary = {
        "facts": len(fact_set.facts),
        "evidence_spans": len(fact_set.evidence),
        "needs_review": fact_set.needs_review_count,
        "rejected": len(ctx.ledger.rejected),
        "internal_conflicts": len(ctx.ledger.conflicts),
        "excel_exported": excel_ok,
        "html": "facts/evidence.html",
    }
    # 同文档指标冲突必须单独落盘 —— 它不会让流程失败，只会让数据静默失真，
    # 因此必须显式可见，供人工复核。
    if ctx.ledger.conflicts:
        write_json(ctx, "facts/internal_conflicts.json", ctx.ledger.conflicts)
    write_json(ctx, "facts/export_summary.json", summary)

    if ctx.tracer:
        ctx.tracer.stage_event(status="ok", detail=str(summary))
    return summary


__all__ = ["run", "build_evidence_html", "export_excel"]
