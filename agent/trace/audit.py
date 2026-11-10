"""审计报告生成 —— 把 trace.jsonl 变成人类可读的过程记录。

赛题要求「执行过程可追溯」。trace.jsonl 是机器格式，
本模块产出 Markdown，供评审、答辩、报告书直接引用。
"""

from __future__ import annotations

import json
from pathlib import Path

from agent.evidence.models import TraceEvent, TraceKind
from agent.trace.replay import load_manifest, verify_run_integrity
from agent.trace.tracer import assertion_status, read_trace, summarize_trace

_KIND_LABEL = {
    TraceKind.LLM_CALL: "模型调用",
    TraceKind.TOOL_CALL: "工具调用",
    TraceKind.FILE_ACCESS: "文件访问",
    TraceKind.ASSERTION: "勾稽断言",
    TraceKind.ROUTING: "页面路由",
    TraceKind.STAGE: "阶段",
    TraceKind.ERROR: "错误",
}

#: 断言三态的展示口径 —— **未校验不是失败**，必须一眼分得开。
_ASSERT_MARK = {"pass": "✅ 通过", "fail": "❌ 失败", "skipped": "➖ 未校验（缺数据）"}

_PIPE = "\\|"
"""Markdown 表格里转义竖线。写成常量而不是内联 —— 反斜杠放进 f-string
表达式里在 3.12 之前是语法错误，而这个项目要兼容 3.11。"""


def build_audit_markdown(run_dir: str | Path, *, max_rows: int = 400) -> str:
    """生成审计报告 Markdown。"""
    run_dir = Path(run_dir)
    manifest = load_manifest(run_dir)
    trace_path = run_dir / "trace.jsonl"
    events: list[TraceEvent] = read_trace(trace_path) if trace_path.exists() else []
    summary = summarize_trace(events) if events else {}
    integrity = verify_run_integrity(run_dir)

    out: list[str] = [
        f"# 运行审计报告 — `{manifest.run_id}`",
        "",
        "## 一、运行概况",
        "",
        f"- 流水线：`{manifest.pipeline}`",
        f"- 状态：**{manifest.status}**",
        f"- 开始：{manifest.started_at.isoformat()}",
        f"- 结束：{manifest.finished_at.isoformat() if manifest.finished_at else '（未完成）'}",
        f"- 契约版本：`{manifest.contract_version}`",
        f"- 离线模式：{manifest.offline}",
        f"- 随机种子：{manifest.seed}",
        "",
        "## 二、环境指纹（复现依据）",
        "",
        "### 模型配置",
        "",
        "| 角色 | 模型 |",
        "|---|---|",
        *[f"| {role} | `{model}` |" for role, model in sorted(manifest.models.items())],
        "",
        "### 输入文件",
        "",
        "| 文件 | SHA256（前 16 位） |",
        "|---|---|",
        *[
            f"| `{path}` | `{sha[:16]}…` |"
            for path, sha in sorted(manifest.input_files.items())
        ],
        "",
    ]

    if manifest.prompts:
        out += [
            "### 提示词版本",
            "",
            "| 提示词 | SHA256（前 16 位） |",
            "|---|---|",
            *[
                f"| `{ref}` | `{sha[:16]}…` |"
                for ref, sha in sorted(manifest.prompts.items())
            ],
            "",
        ]

    out += [
        "## 三、过程统计",
        "",
        f"- 事件总数：**{summary.get('total_events', 0)}**",
        f"- 模型调用：**{summary.get('llm_calls', 0)}** 次"
        f"（缓存命中 {summary.get('llm_cache_hits', 0)} 次，"
        f"命中率 {_pct(summary.get('cache_hit_rate'))}）",
        f"- 工具调用：**{summary.get('tool_calls', 0)}** 次",
        f"- 文件访问：**{summary.get('file_accesses', 0)}** 次"
        f"（涉及 {summary.get('unique_files', 0)} 个不同文件）",
        f"- 勾稽断言：**{summary.get('assertions', 0)}** 条 —— "
        f"通过 {summary.get('assertions_passed', 0)} / "
        f"失败 **{summary.get('assertions_failed', 0)}** / "
        f"未校验（缺数据）{summary.get('assertions_skipped', 0)}",
        f"- Token 消耗：输入 {summary.get('prompt_tokens', 0):,} / "
        f"输出 {summary.get('completion_tokens', 0):,}",
        f"- 估算成本：**¥{summary.get('cost_cny', 0.0):.4f}**",
        "",
    ]

    if summary.get("errors"):
        out += ["### ⚠️ 运行期错误", ""]
        out += [f"- {e}" for e in summary["errors"][:20]]
        out.append("")

    # —— 可复现性 ——
    out += [
        "## 四、可复现性核验",
        "",
        f"**{'✅ 产物哈希与 manifest 一致' if integrity.reproducible else '❌ 存在不一致'}**",
        "",
    ]
    if integrity.hash_matches:
        out += ["| 产物 | 一致 |", "|---|---|"]
        out += [
            f"| `{p}` | {'✅' if ok else '❌'} |"
            for p, ok in sorted(integrity.hash_matches.items())
        ]
        out.append("")
    if integrity.mismatches:
        out += ["不一致明细：", "", *[f"- {m}" for m in integrity.mismatches], ""]

    # —— 逐页路由决策（可追溯亮点） ——
    routings = [e for e in events if e.kind is TraceKind.ROUTING]
    if routings:
        out += [
            "## 五、逐页解析路由决策",
            "",
            "> 每一页为什么走文本层 / OCR / VLM 裁决，均有据可查。",
            "",
            "| 页 | 路由 | 判定理由 |",
            "|---|---|---|",
        ]
        for e in routings[:max_rows]:
            page = e.extra.get("page", "")
            route = e.extra.get("route", "")
            reason = str(e.extra.get("reason", "")).replace("|", _PIPE)
            out.append(f"| {page} | `{route}` | {reason} |")
        out.append("")

    # —— 断言行 ——
    asserts = [e for e in events if e.kind is TraceKind.ASSERTION]
    if asserts:
        out += [
            "## 六、勾稽断言明细",
            "",
            "> 三态口径：**通过** = 独立重算一致；**失败** = 校验过且不一致；",
            "> **未校验** = 缺辅助明细，本次无法独立重算（不是失败，但也不是通过）。",
            "",
            "| 断言 | 结果 | 明细 |",
            "|---|---|---|",
        ]
        for e in asserts[:max_rows]:
            mark = _ASSERT_MARK.get(assertion_status(e), assertion_status(e))
            detail = str(e.extra.get("detail", "")).replace("|", _PIPE)
            out.append(f"| `{e.tool}` | {mark} | {detail} |")
        out.append("")

    # —— 完整事件时间线 ——
    out += [
        "## 七、完整事件时间线",
        "",
        f"（共 {len(events)} 条，此处展示前 {min(len(events), max_rows)} 条；"
        "完整记录见 `trace.jsonl`）",
        "",
        "| # | 类型 | 对象 | 耗时(ms) | 说明 |",
        "|---|---|---|---|---|",
    ]
    for e in events[:max_rows]:
        obj = e.tool or e.model or e.path or ""
        note_bits: list[str] = []
        if e.kind is TraceKind.LLM_CALL:
            note_bits.append(f"{e.prompt_tokens or 0}→{e.completion_tokens or 0} tok")
            if e.cache_hit:
                note_bits.append("缓存命中")
        if e.kind is TraceKind.FILE_ACCESS:
            note_bits.append(e.mode or "")
        if e.error:
            note_bits.append(f"错误: {e.error[:60]}")
        note = "；".join(note_bits).replace("|", _PIPE)
        out.append(
            f"| {e.seq} | {_KIND_LABEL.get(e.kind, e.kind.value)} | `{obj}` | "
            f"{e.latency_ms if e.latency_ms is not None else ''} | {note} |"
        )

    out += _table_section(run_dir, max_rows=max_rows)

    out += [
        "",
        "---",
        "",
        "本报告由 `agent.trace.audit` 自动生成，"
        "数据来源为 `trace.jsonl` 与 `manifest.json`，未经人工修饰。",
        "",
    ]
    return "\n".join(out)


def _table_section(run_dir: Path, *, max_rows: int = 400) -> list[str]:
    """表格解析与单元格级裁决一节。

    刻意**不用**"算术自洽率"这种数字：没有合计线的表 ``ok`` 也是真，
    单看自洽率会把"没查过"混进"查过且没问题"。所以这里把
    「已验算 / 无从校验 / 发现不平」三态分开列。
    """
    tpath = run_dir / "channels" / "tables.json"
    apath = run_dir / "channels" / "table_adjudications.json"
    if not tpath.is_file():
        return []

    try:
        tables = json.loads(tpath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not tables:
        return []

    checked = unverifiable = failed_tables = failures = soft = 0
    for t in tables:
        a = t.get("arithmetic") or {}
        n = int(a.get("checked", 0) or 0) or (
            int(a.get("checked_rows", 0) or 0) + int(a.get("checked_cols", 0) or 0)
        )
        checked += n
        soft += int(a.get("soft_count", 0) or 0)
        if n == 0:
            unverifiable += 1
        elif not a.get("ok", True):
            failed_tables += 1
            failures += int(a.get("failure_count", 0) or 0)

    out = [
        "## 八、表格解析与单元格级裁决",
        "",
        f"- 识别到表格：**{len(tables)}** 张"
        f"（其中有框线 {sum(1 for t in tables if t.get('has_rules'))} 张）",
        f"- 已验算合计线：**{checked}** 条"
        + (f"，发现不平 **{failures}** 处（{failed_tables} 张表）" if failures else "，全部相符"),
        f"- 表内无合计线、算术上**无从校验**：**{unverifiable}** 张",
    ]
    if soft:
        out.append(f"- 分层小计（合计只覆盖部分分项，不判失败）：{soft} 条")

    try:
        adjs = json.loads(apath.read_text(encoding="utf-8")) if apath.is_file() else []
    except (OSError, json.JSONDecodeError):
        adjs = []

    if adjs:
        agg: dict[str, int] = {}
        for rec in adjs:
            for k, v in (rec.get("report") or {}).items():
                if isinstance(v, int):
                    agg[k] = agg.get(k, 0) + v
        out += [
            "",
            f"### 单元格级视觉裁决：{len(adjs)} 张表被复核",
            "",
            "| 结论 | 格数 | 说明 |",
            "|---|---|---|",
            f"| 一致 | {agg.get('agree', 0)} | 模型读数与程序读数一致 |",
            f"| 格式归一 | {agg.get('normalized', 0)} | 数字未变，仅修复换行切断（原本取不到数） |",
            f"| 改值 | {agg.get('corrected', 0)} | 不一致且通过全部闸门 |",
            f"| 未采纳 | {agg.get('rejected', 0)} | 不一致但未过闸门 → **只标记不改** |",
            f"| 读不出 | {agg.get('unreadable', 0)} | 模型也没读出内容 |",
            f"| 出错 | {agg.get('errors', 0)} | 裁剪或调用失败 |",
            "",
        ]
        changed = [
            (rec, a)
            for rec in adjs
            for a in (rec.get("report") or {}).get("adjudications", [])
            if a.get("verdict") in ("corrected", "normalized")
        ]
        if changed:
            out += [
                "#### 改动明细（可逐条核对）",
                "",
                "| 页 | 位置 | 原读数 | 改后 | 结论 | 置信度 |",
                "|---|---|---|---|---|---|",
            ]
            for rec, a in changed[:max_rows]:
                orig = _cell_text(a.get("original"))
                new = _cell_text(a.get("vlm_text"))
                out.append(
                    f"| {rec.get('page')} | r{a.get('row')}c{a.get('col')} | "
                    f"`{orig}` | `{new}` | {a.get('verdict')} | {a.get('confidence')} |"
                )
            out.append("")
    else:
        out += ["", "本次运行未产生单元格级裁决记录。", ""]

    return out


def _cell_text(value: object) -> str:
    """把单元格文本压成 Markdown 表格里安全的一行。

    单元格里带换行是**常态**（"碎格"本身就是换行切断造成的），
    直接写进表格会把 Markdown 结构截断，报告看起来像坏了。
    """
    s = str(value or "").replace("|", _PIPE)
    s = s.replace("\r\n", "⏎").replace("\n", "⏎").replace("\r", "⏎")
    s = s.replace("\t", " ")
    return s[:60]


def write_audit_report(run_dir: str | Path, *, filename: str = "audit.md") -> Path:
    run_dir = Path(run_dir)
    md = build_audit_markdown(run_dir)
    target = run_dir / filename
    target.write_text(md, encoding="utf-8", newline="\n")
    return target


def build_trace_dump(run_dir: str | Path) -> list[dict[str, object]]:
    """导出 JSON 格式的 trace，供 Web 面板消费。"""
    trace_path = Path(run_dir) / "trace.jsonl"
    if not trace_path.exists():
        return []
    return [e.model_dump(mode="json") for e in read_trace(trace_path)]


def dump_trace_json(run_dir: str | Path, *, filename: str = "trace_view.json") -> Path:
    run_dir = Path(run_dir)
    target = run_dir / filename
    target.write_text(
        json.dumps(build_trace_dump(run_dir), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return target


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


__all__ = [
    "build_audit_markdown",
    "write_audit_report",
    "build_trace_dump",
    "dump_trace_json",
]
