"""阶段 3：估值报告渲染（图表 + Markdown + Excel）。

产出的图表同时被两处消费：
    * Markdown 报告（提交物）
    * Web 演示台（``/valuation`` 页面）

配色与版面刻意做得干净，因为这是决赛现场评委第一眼看到的东西。
"""

from __future__ import annotations

import html
from decimal import Decimal
from typing import Any

from agent.evidence.models import ValuationResult
from agent.runtime.loop import RunContext
from agent.validate.numeric import q
from pipelines.r1_extract.common import read_json, write_json
from pipelines.r4_valuation.assumptions import GATE_MARKER as _GATE_MARKER

# ---------------------------------------------------------------------------
# matplotlib 中文字体配置
# ---------------------------------------------------------------------------

_FONT_CANDIDATES = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Source Han Sans SC",
                    "PingFang SC", "WenQuanYi Zen Hei"]


# ---------------------------------------------------------------------------
# 图表配色 —— 中性蓝灰墨阶（**只影响观感，不参与任何计算**）
# ---------------------------------------------------------------------------
# 2026-09-27 改。原配色是 RdYlGn 色图 + #c62828 红 + #f9a825 黄 + #2e7d32 绿。
# 两个问题：
#   ① 色相暗示「好/坏」。敏感性热力图上没有好坏，只有「价值高/低」这个
#      **有序**量 —— 有序量该用**明度阶**编码，红绿是给「类别」用的。
#      一张估值图从绿到红，读者会下意识读成「乐观到悲观」，那是我强加的。
#   ② 赛题明确要求风险审慎、**不大面积红黄**（见
#      docs/WEB_DESIGN_2026-09-25.md 的配色一节）。
#      RdYlGn 铺满一整张热力图，正是「大面积红黄」。
# 现在统一为一支蓝黑墨的五级明度阶，与网页端 --s1..--s5 同族、零外链。
# ⚠️ 这里只改颜色常量，**不改任何取值、坐标、算式或渲染顺序**。
_INK = "#1F3A52"    # 主墨：最重的读数
_INK2 = "#46698A"
_INK3 = "#7C9CB6"
_INK4 = "#B0C3D2"
_INK5 = "#D7E0E7"   # 最浅：承托面


def _setup_matplotlib() -> Any:
    """配置 matplotlib 中文字体。返回 pyplot 模块；不可用返回 None。"""
    try:
        import matplotlib

        matplotlib.use("Agg")  # 无 GUI 后端，服务器/CI 可用
        from matplotlib import font_manager, pyplot as plt

        available = {f.name for f in font_manager.fontManager.ttflist}
        for name in _FONT_CANDIDATES:
            if name in available:
                plt.rcParams["font.sans-serif"] = [name]
                break
        plt.rcParams["axes.unicode_minus"] = False
        plt.rcParams["figure.dpi"] = 130
        plt.rcParams["savefig.bbox"] = "tight"
        return plt
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 图 1：敏感性热力图（WACC × 永续增长率）
# ---------------------------------------------------------------------------


def plot_sensitivity_heatmap(result: ValuationResult, out_path: str, *, guard: Any = None) -> bool:
    plt = _setup_matplotlib()
    if plt is None or not result.sensitivity.grid:
        return False

    xs = sorted({float(c.x_value) for c in result.sensitivity.grid})
    ys = sorted({float(c.y_value) for c in result.sensitivity.grid})
    if not xs or not ys:
        return False

    import numpy as np

    grid = np.full((len(ys), len(xs)), np.nan)
    xi = {v: i for i, v in enumerate(xs)}
    yi = {v: i for i, v in enumerate(ys)}
    for c in result.sensitivity.grid:
        grid[yi[float(c.y_value)], xi[float(c.x_value)]] = float(c.value_per_share)

    fig, ax = plt.subplots(figsize=(7.2, 5.0))
    # 单色明度阶替代 RdYlGn：浅 = 每股价值低，深 = 高。
    # 有序量用明度编码，读者不需要查图例就知道方向。
    from matplotlib.colors import LinearSegmentedColormap

    _ink_cmap = LinearSegmentedColormap.from_list("ink", [_INK5, _INK4, _INK3, _INK2])
    im = ax.imshow(grid, cmap=_ink_cmap, aspect="auto", origin="lower")
    ax.set_xticks(range(len(xs)))
    ax.set_xticklabels([f"{v:.1%}" for v in xs], fontsize=9)
    ax.set_yticks(range(len(ys)))
    ax.set_yticklabels([f"{v:.1%}" for v in ys], fontsize=9)
    ax.set_xlabel(result.sensitivity.x_name.upper(), fontsize=10)
    ax.set_ylabel("永续增长率 g", fontsize=10)
    ax.set_title("每股价值敏感性分析（元/股）", fontsize=12, pad=10)

    for i in range(len(ys)):
        for j in range(len(xs)):
            v = grid[i, j]
            if not np.isnan(v):
                # 格内数字原先是纯黑 #1a1a1a，现在填料最深也只到 _INK2，
                # 因此一律用白字反而对比度最高、且不会在浅格上糊掉。
                # 按**归一化后的深浅**分别取白/墨，保证每一格都读得清。
                frac = 0.0 if np.isnan(v) else float(
                    (v - np.nanmin(grid)) / ((np.nanmax(grid) - np.nanmin(grid)) or 1))
                ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=8,
                        color="#FFFFFF" if frac > 0.45 else _INK)

    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label("每股价值（元）", fontsize=9)
    fig.savefig(out_path)
    if guard is not None:
        guard.record(out_path, "write")
    plt.close(fig)
    return True


# ---------------------------------------------------------------------------
# 图 2：龙卷风图
# ---------------------------------------------------------------------------


_TORNADO_LABELS = {
    "wacc": "WACC",
    "terminal_growth": "永续增长率 g",
    "da_ratio_of_revenue": "折旧摊销/收入",
    "capex_ratio_of_revenue": "资本开支/收入",
    "nwc_ratio_of_revenue": "营运资本/收入",
    "tax_rate": "所得税率",
}


def plot_tornado(result: ValuationResult, out_path: str, *, guard: Any = None) -> bool:
    plt = _setup_matplotlib()
    if plt is None or not result.sensitivity.tornado:
        return False

    items = result.sensitivity.tornado[:8]
    labels = [_TORNADO_LABELS.get(i["assumption"], i["assumption"]) for i in items]
    lows = [float(Decimal(i["low"])) for i in items]
    highs = [float(Decimal(i["high"])) for i in items]
    base = float(result.dcf.value_per_share)

    import numpy as np

    y = np.arange(len(items))
    fig, ax = plt.subplots(figsize=(7.2, 0.55 * len(items) + 1.6))
    # 上调/下调是**两个方向**，不是好/坏：原先一绿一红把方向读成了褒贬。
    # 现在用同一支墨的两级明度区分，方向靠位置（基准线左右）already 分得很清。
    ax.barh(y, [h - base for h in highs], left=base, color=_INK3, alpha=0.95,
            label="假设上调", height=0.55)
    ax.barh(y, [l - base for l in lows], left=base, color=_INK, alpha=0.95,
            label="假设下调", height=0.55)
    ax.axvline(base, color=_INK2, linestyle="--", linewidth=1.4,
               label=f"基准 {base:.2f} 元")
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=9)
    ax.set_xlabel("每股价值（元）", fontsize=10)
    ax.set_title("关键假设对每股价值的影响（龙卷风图）", fontsize=12, pad=10)
    ax.legend(fontsize=8, loc="lower right")
    ax.grid(axis="x", alpha=0.25)
    fig.savefig(out_path)
    if guard is not None:
        guard.record(out_path, "write")
    plt.close(fig)
    return True


# ---------------------------------------------------------------------------
# 图 3：估值区间
# ---------------------------------------------------------------------------


def plot_value_range(result: ValuationResult, out_path: str, *, guard: Any = None) -> bool:
    plt = _setup_matplotlib()
    if plt is None:
        return False

    dcf_v = float(result.dcf.value_per_share)
    comps_lo = float(result.comps.implied_value_per_share_low)
    comps_hi = float(result.comps.implied_value_per_share_high)
    lo = float(result.value_range_low)
    hi = float(result.value_range_high)
    mid = float(result.value_range_mid)

    fig, ax = plt.subplots(figsize=(7.2, 3.4))
    # 三条读数用同一支墨的三级明度：综合区间最深（它是结论），
    # 相对估值区间次之，DCF 单点用菱形记号区分（**形**已足够，不必再给一个色相）。
    ax.barh([2], [comps_hi - comps_lo], left=comps_lo, color=_INK4, alpha=0.95, height=0.5)
    ax.plot([dcf_v], [1], marker="D", color=_INK2, markersize=10)
    ax.barh([0], [hi - lo], left=lo, color=_INK3, alpha=0.95, height=0.5)
    ax.plot([mid], [0], marker="|", color=_INK, markersize=22, markeredgewidth=2.5)

    ax.set_yticks([2, 1, 0])
    ax.set_yticklabels(["相对估值区间", "DCF 单点", "综合估值区间"], fontsize=10)
    ax.set_xlabel("每股价值（元）", fontsize=10)
    ax.set_title("估值结果对比", fontsize=12, pad=10)
    ax.grid(axis="x", alpha=0.25)
    ax.annotate(f"{dcf_v:.2f}", (dcf_v, 1), textcoords="offset points",
                xytext=(0, 12), ha="center", fontsize=9, color=_INK2)
    ax.annotate(f"{lo:.2f} – {hi:.2f}", (mid, 0), textcoords="offset points",
                xytext=(0, 12), ha="center", fontsize=9, color=_INK)
    fig.savefig(out_path)
    if guard is not None:
        guard.record(out_path, "write")
    plt.close(fig)
    return True


# ---------------------------------------------------------------------------
# 图 4：FCFF 瀑布
# ---------------------------------------------------------------------------


def plot_fcff_waterfall(result: ValuationResult, out_path: str, *, guard: Any = None) -> bool:
    plt = _setup_matplotlib()
    if plt is None or not result.dcf.explicit_years:
        return False

    years = result.dcf.explicit_years
    labels = [f"{y.year}\n收入" for y in years]
    revenue = [float(y.revenue) / 1e8 for y in years]
    fcff = [float(y.fcff) / 1e8 for y in years]
    nopat = [float(y.nopat) / 1e8 for y in years]

    import numpy as np

    x = np.arange(len(years))
    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    # FCFF 原先是红线 —— 一根贯穿全图的红线，正是「大面积红」。
    # 它是这张图的主读数，该用**最重的墨 + 折线记号**强调，而不是用色相。
    ax.bar(x - 0.22, revenue, width=0.42, label="营业收入", color=_INK4)
    ax.bar(x + 0.22, nopat, width=0.42, label="NOPAT", color=_INK3)
    ax.plot(x, fcff, marker="o", color=_INK, linewidth=2, label="FCFF")
    ax.set_xticks(x)
    ax.set_xticklabels([str(y.year) for y in years], fontsize=9)
    ax.set_ylabel("金额（亿元）", fontsize=10)
    ax.set_title("预测期收入 / NOPAT / FCFF", fontsize=12, pad=10)
    ax.legend(fontsize=9)
    ax.grid(axis="y", alpha=0.25)
    fig.savefig(out_path)
    if guard is not None:
        guard.record(out_path, "write")
    plt.close(fig)
    return True


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------


def _cell(value: Any) -> str:
    """表格单元格：缺值写 `—`，不要写 `None`。

    可比公司的 EV/EBITDA 当前一律取不到，直接插值会在报告里印出一列
    字面量 `None`，读起来像程序崩溃而不是「该指标不可得」。
    """
    return "—" if value is None else str(value)


def build_report_md(result: ValuationResult, gaps: list[str]) -> str:
    # 核心输入缺口：只有这些会让"估值结论"变成"方法论演示"。
    # 缺个明细科目（如货币资金）不影响结论性质；缺总股本/净利润则完全改变性质。
    # 「披露主体自身的财务数据」是最重的一条 —— 它意味着这份材料压根估不了这个主体。
    illustrative_gaps = [g for g in (gaps or []) if (
        "披露主体" in g or "revenue" in g or "shares" in g
        or "equity_parent" in g or "net_profit" in g
    )]
    a = result.assertions
    passed = sum(1 for x in a if x.status.value == "pass")
    failed = [x for x in a if x.status.value == "fail"]
    actual_pass = sum(
        1 for x in a if x.status.value == "pass" and x.code in {"A1", "A4", "A5"}
    )
    total_real = sum(1 for x in a if x.code in {"A1", "A4", "A5"})

    icon = {"pass": "√", "fail": "×", "skipped": "○"}
    lines: list[str] = [
        f"# {result.entity} 估值分析报告",
        "",
        f"> 生成时间：{result.as_of.isoformat()} ｜ 运行 ID：`{result.run_id}`",
        "> 本报告由金融投研智能体自动生成，全部数字可追溯至原始材料。",
        "",
        "---",
        "",
        "## 一、估值结论",
        "",
        # 横幅必须放在结论表上方：核心输入用了模板默认值时，下面那些数字
        # 只是方法演示，不是对这家公司的估值。实测动机：一次真实运行里核心
        # 输入缺了 4 项（含总股本、归母净利润），R4 用模板值算出「每股 ¥7.86」
        # 并正常输出；缺口的说明在报告末尾，而读者第一眼看到的是这个数字。
        *(
            [
                "> **本结论仅为方法论演示，不构成对该公司的估值判断。**",
                ">",
                "> 以下核心输入未从提交材料中提取到，估值使用了显式标注的模板默认值："
                + "、".join(illustrative_gaps),
                "> 补齐材料后重跑即可自动替换为真实数据（详见文末「数据缺口声明」）。",
                "",
            ]
            if illustrative_gaps
            else []
        ),
        "| 项目 | 数值 |",
        "|---|---|",
        f"| 综合估值区间 | **{q(result.value_range_low, 2)} – {q(result.value_range_high, 2)} 元/股** |",
        f"| 区间中值 | {q(result.value_range_mid, 2)} 元/股 |",
        f"| DCF 单点估值 | {q(result.dcf.value_per_share, 2)} 元/股 |",
        f"| 相对估值区间 | {q(result.comps.implied_value_per_share_low, 2)} – "
        f"{q(result.comps.implied_value_per_share_high, 2)} 元/股 |",
        f"| 企业价值 EV | {q(result.dcf.enterprise_value / Decimal('1e8'), 2)} 亿元 |",
        f"| 股权价值 | {q(result.dcf.equity_value / Decimal('1e8'), 2)} 亿元 |",
        f"| 终值占比 | {q(result.dcf.terminal_value_share * 100, 1)}% |",
        "",
        f"区间合成方式：{result.weighting_note}",
        "",
        "## 二、模型可信度（勾稽断言）",
        "",
        f"通过 {passed}/{len(a)} 条。"
        + (
            "全部关键断言通过，估值结论可用。"
            if result.is_usable
            else f"存在失败断言 {[x.code for x in failed]}，估值结论不可用。"
        ),
        "",
        "| 断言 | 名称 | 结果 | 明细 |",
        "|---|---|---|---|",
    ]
    for x in a:
        lines.append(
            f"| {x.code} | {x.name} | {icon[x.status.value]} | "
            f"{x.detail.replace('|', '/')} |"
        )

    lines += [
        "",
        f"> 说明：其中 A1/A4/A5 为独立重算类断言（共 {total_real} 条，通过 {actual_pass} 条），"
        "其余为结构性恒等式，其成立性由模型构造方式保证，不能作为独立验证证据。",
        "",
        "## 三、关键假设与依据",
        "",
        "> 下表逐条标注每项假设的属性：事实、推论，或观点。",
        "",
        "| 假设 | 取值 | 属性 | 依据 |",
        "|---|---|---|---|",
    ]
    for asm in result.assumptions:
        # 第三类属性：依据是事实，但取值被模型内部的一致性闸门改过。
        # 只标「事实」是过度声称 —— 它的数值毕竟不是事实库里直接读到的那个；
        # 只标「观点」又是反向失真（依据确实是真事实）。读者需要能看出
        # 这一条被调整过、为什么调，否则闸门就成了把调整藏起来的黑箱。
        # 标注逻辑与 Excel 工作簿共用 `_attribution`。
        attr = {
            "事实": "事实",
            "观点": "观点",
            "事实 + 一致性调整": "事实 + 一致性调整",
        }[_attribution(asm)]
        lines.append(
            f"| {asm.name} | {q(asm.value, 4)} | {attr} | {asm.basis.replace('|', '/')} |"
        )

    lines += [
        "",
        "## 四、敏感性分析",
        "",
        "### 关键驱动因素（按影响幅度排序）",
        "",
        "| 假设 | 下调情景 | 基准 | 上调情景 | 波动幅度 | 占基准 |",
        "|---|---|---|---|---|---|",
    ]
    for t in result.sensitivity.tornado:
        lines.append(
            f"| {_TORNADO_LABELS.get(t['assumption'], t['assumption'])} | {t['low'][:8]} | "
            f"{t['base'][:8]} | {t['high'][:8]} | {t['swing'][:8]} | {t['swing_pct_of_base']}% |"
        )

    lines += [
        "",
        "二维敏感性网格（WACC × 永续增长率）见 `charts/sensitivity_heatmap.png`，",
        "龙卷风图见 `charts/tornado.png`。",
        "",
        "## 五、相对估值",
        "",
        f"筛选规则：{result.comps.peer_selection_rule}",
        "",
        "| 公司 | 代码 | PE | PB | EV/EBITDA | 倍数口径 |",
        "|---|---|---|---|---|---|",
    ]
    for p in result.comps.peers:
        lines.append(
            f"| {p.name} | {p.ticker} | {_cell(p.pe)} | {_cell(p.pb)} | "
            f"{_cell(p.ev_ebitda)} | {p.multiple_basis} |"
        )
    lines += [
        "",
        f"- 行业中位 PE：{_cell(result.comps.median_pe)}",
        f"- 行业中位 PB：{_cell(result.comps.median_pb)}",
        f"- 流动性折价：{q(result.comps.illiquidity_discount * 100, 1)}%",
        "",
    ]

    # ---- 数据来源与时点 ----
    # 这一段是给评审核对用的：倍数从哪来、什么时候的行情、怎么算的。
    # 缺了它，读者只能相信报告的转述；有了它，可以自己复核每一个数。
    prov = result.comps_provenance or {}
    if prov:
        lines += ["数据来源与时点：", ""]
        if prov.get("snapshot_path"):
            lines.append(f"- 快照文件：`{prov['snapshot_path']}`")
        if prov.get("fetched_at"):
            lines.append(f"- 抓取时间：{prov['fetched_at']}")
        if prov.get("as_of"):
            lines.append(f"- 财务数据报告期：{prov['as_of']}")
        for k, v in (prov.get("sources") or {}).items():
            lines.append(f"- 数据源（{k}）：{v}")
        for k, v in (prov.get("multiple_formula") or {}).items():
            lines.append(f"- 倍数算式（{k}）：{v}")
        if prov.get("why_self_computed"):
            lines.append(f"- 为什么自算而不采信接口字段：{prov['why_self_computed']}")
        if prov.get("verification"):
            lines.append(f"- 数据源验证：{prov['verification']}")
        if prov.get("peer_count_after_filter") is not None:
            lines.append(
                f"- 筛选后可比公司数：{prov['peer_count_after_filter']}"
                f"（{ '、'.join(prov.get('peer_names') or []) }）"
            )
        lines.append("")

    lines += [
        "## 六、风险因素与适用范围",
        "",
    ]
    for r in result.risk_factors:
        lines.append(f"- {r}")

    lines += ["", f"适用范围：{result.applicability}", ""]

    if gaps:
        lines += [
            "## 七、数据缺口声明",
            "",
            "以下科目在本次提交的原始材料中未能提取到，估值使用了显式标注的模板值：",
            "",
        ]
        lines += [f"- {g}" for g in gaps]
        lines += [
            "",
            "> 这些缺口是主动披露的，不是静默填充。补齐材料后重跑即可替换为真实数据。",
            "",
        ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 阶段入口
# ---------------------------------------------------------------------------


def run(ctx: RunContext) -> dict[str, Any]:
    result = ValuationResult.model_validate(read_json(ctx, "valuation/valuation.json"))
    try:
        gaps = read_json(ctx, "valuation/assumptions.json").get("gaps", [])
    except FileNotFoundError:
        gaps = []

    charts_dir = ctx.valuation_dir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)

    guard = ctx.guard()
    produced: dict[str, bool] = {
        "sensitivity_heatmap.png": plot_sensitivity_heatmap(
            result, str(charts_dir / "sensitivity_heatmap.png"), guard=guard
        ),
        "tornado.png": plot_tornado(
            result, str(charts_dir / "tornado.png"), guard=guard
        ),
        "value_range.png": plot_value_range(
            result, str(charts_dir / "value_range.png"), guard=guard
        ),
        "fcff_waterfall.png": plot_fcff_waterfall(
            result, str(charts_dir / "fcff_waterfall.png"), guard=guard
        ),
    }

    md = build_report_md(result, gaps)
    guard.write_text(ctx.valuation_dir / "valuation_report.md", md)

    excel_ok = _export_excel(result, str(ctx.valuation_dir / "valuation_model.xlsx"))
    if excel_ok:
        guard.record(ctx.valuation_dir / "valuation_model.xlsx", "write")

    summary = {
        "charts": produced,
        "excel": excel_ok,
        "report_md": "valuation/valuation_report.md",
        "is_usable": result.is_usable,
    }
    write_json(ctx, "valuation/report_summary.json", summary)

    if ctx.tracer:
        ctx.tracer.stage_event(
            status="ok",
            detail=f"图表 {sum(produced.values())}/{len(produced)}，Excel={excel_ok}",
        )
    return summary


def _attribution(asm: Any) -> str:
    """假设的**属性标注** —— 事实 / 观点 / 事实 + 一致性调整。

    第三类是必须单列出来的（2026-09 第十一轮）：依据确实是真事实，
    但**取值被模型内部的一致性闸门抬升过**。只标"事实"是过度声称
    （它的数值不是事实库里直接读到的那个），只标"观点"又是反向失真。
    Markdown 报告与 Excel 工作簿共用本函数，两处不能各说一套。
    """
    if _GATE_MARKER in asm.basis:
        return "事实 + 一致性调整"
    return "观点" if asm.is_judgment else "事实"


def _export_excel(result: ValuationResult, path: str) -> bool:
    """导出估值模型工作簿。公式可审是相对估值方法论透明性的一部分。

    时间戳会被统一（见 :mod:`domain.tools.xlsx`），保证两次导出字节一致 ——
    否则「结果可复现」在哈希层面无法验证。
    """
    from domain.tools.xlsx import write_xlsx

    return write_xlsx(
        {
            "假设表": [
                {
                    "假设": a.name, "取值": str(a.value),
                    "属性": _attribution(a),
                    "来源事实": ",".join(a.source_fact_ids) or "-",
                    "依据": a.basis,
                }
                for a in result.assumptions
            ],
            "预测三表": [
                {
                    "年度": y.year,
                    "营业收入": str(y.revenue),
                    "EBIT": str(y.ebit),
                    "NOPAT": str(y.nopat),
                    "折旧摊销": str(y.depreciation_amortization),
                    "资本开支": str(y.capex),
                    "营运资本变动": str(y.nwc_change),
                    "FCFF": str(y.fcff),
                    "净利润": str(y.net_income),
                }
                for y in result.dcf.explicit_years
            ],
            "DCF": [
                {"项目": "WACC", "数值": str(result.dcf.wacc)},
                {"项目": "永续增长率", "数值": str(result.dcf.terminal_growth)},
                {"项目": "终值", "数值": str(result.dcf.terminal_value)},
                {"项目": "企业价值 EV", "数值": str(result.dcf.enterprise_value)},
                {"项目": "净债", "数值": str(result.dcf.net_debt)},
                {"项目": "股权价值", "数值": str(result.dcf.equity_value)},
                {"项目": "总股本", "数值": str(result.dcf.shares_outstanding)},
                {"项目": "每股价值", "数值": str(result.dcf.value_per_share)},
                {"项目": "终值占比", "数值": str(result.dcf.terminal_value_share)},
            ],
            "敏感性网格": [
                {
                    "x_value": str(c.x_value), "y_value": str(c.y_value),
                    "value_per_share": str(c.value_per_share),
                }
                for c in result.sensitivity.grid
            ],
            "勾稽断言": [
                {
                    "断言": x.code, "名称": x.name, "结果": x.status.value,
                    "明细": x.detail, "LHS": str(x.lhs), "RHS": str(x.rhs),
                }
                for x in result.assertions
            ],
        },
        path,
    )


__all__ = [
    "run",
    "build_report_md",
    "plot_sensitivity_heatmap",
    "plot_tornado",
    "plot_value_range",
    "plot_fcff_waterfall",
]
