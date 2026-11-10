"""图表数据装配层 —— 把 run 产物翻译成 ``web.charts`` 的入参。

## 为什么要单独一层

``web/charts.py`` 是**纯几何**：给数字画图，不知道「事实」「估值」是什么。
``web/app.py`` 是**路由**：本该只做「取数据 → 交给模板」。
若把「facts 里有几层路由各占多少」这类统计写进 app.py，路由函数会膨胀成
一堆 ``_count_by`` + 字典推导，模板也跟着难读。

于是中间夹这一层：**只做适配，不做渲染，也不做业务判断**。
每个 ``*_charts()`` 接收已读好的产物对象，返回 ``{模板变量名: Markup}``，
模板里 ``{{ c.layer_mix }}`` 直接用。

## 三条硬约束

1. **不重算任何业务数字。** 估值区间、龙卷风摆动、审计 P/R/F1 一律读产物文件里
   已算好的值 —— 网页显示的必须与命令行产物**逐位一致**（``web/__init__.py`` 的承诺）。
   本层只做「换个容器装」：分组、计数、排序、取标签。
2. **零新依赖。** 不用 numpy/pandas，不引入 JS 图表库（违背「受控数据环境」）。
3. **数据缺失就返回空图。** 每个 ``charts`` 函数对空输入都返回带说明的占位图，
   绝不抛异常 —— 页面上「这块没有数据」本身就是要如实传达的信息。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Sequence

from markupsafe import Markup

from web import charts as C

__all__ = [
    "facts_charts",
    "extract_charts",
    "audit_charts",
    "valuation_charts",
    "E1_LABELS",
    "TAG_LABELS",
]


# ---------------------------------------------------------------------------
# 标签表
# ---------------------------------------------------------------------------

#: 来源层 L1–L4 的中文名。与 ``pipelines/r1_extract`` 的层定义对应。
TAG_LABELS = {
    "L1": "L1 文本层直读",
    "L2": "L2 OCR 识别",
    "L3": "L3 VLM 视觉读数",
    "L4": "L4 人工复核",
}

#: E1–E6 错误类型。**与 ``pipelines/r5_audit/check.py`` 的 ``ErrorType`` 标签同源**；
#: 这里不 import 那个枚举，是为了让 ``web/`` 不依赖 ``pipelines/`` 的内部符号
#: （web 层只认产物文件）。若上游增删类型，此表需同步 —— 故在 ``audit_charts``
#: 里对**表外的类型原样显示**，不会静默吞掉。
E1_LABELS = {
    "E1_unit": "E1 单位错误",
    "E2_valuation_multiple": "E2 估值倍数",
    "E2_multiple": "E2 估值倍数",
    "E3_statistical_caliber": "E3 统计口径",
    "E3_caliber": "E3 统计口径",
    "E4_citation": "E4 引用与来源",
    "E5_arithmetic": "E5 算术一致性",
    "E5_arithmetic_consistency": "E5 算术一致性",
    "E6_qualitative": "E6 定性矛盾",
}

#: E1–E6 的**规范六类**：``(代号, 中文名, 产物里可能的键, 是否已实现检测规则)``。
#:
#: 为什么要这张表：产物里 E2/E3/E5 **各有两个别名键**（如 ``E2_valuation_multiple``
#: 与 ``E2_multiple`` 并存），直接按出现过的类型画图会重复；更要紧的是，
#: **只列「出现过的类型」会让读者以为我们只认这几类** —— 而赛题考的正是能否覆盖
#: E1–E6。故按规范六类列全，**未检出的显式显示 0**，把「能力清单」与「本次结果」
#: 分开呈现。顺序即赛题检查项顺序，便于评委逐项对照。
#:
#: ★ 第 4 位 ``implemented`` 是 2026-09-27 补的，起因是一次**能力超报**：
#:   原表把 E6 与其余五类并列，页面还写着「灰色表示本次未检出，**不代表不支持该类**」。
#:   但实测全仓**没有任何规则产出 E6**：``grep error_type=ErrorType.`` 的产出点只有
#:   E1×8 / E2×4 / E3×5 / E4×3 / E5×1，``rule.E?.`` 规则 ID 全量 16 条里没有 E6；
#:   E6 只出现在定义、优先级权重、可抑制集合与中文标签映射四处**消费端**。
#:   于是 E1–E5 的 0 是「本次未检出」（真的），E6 的 0 是「压根没有这条规则」（假）。
#:   **把"没实现"呈现为"没查到"，正是本项目 SKIPPED ≠ FAIL 那条原则要防的事。**
#:   所以未实现的类不再混在同一个灰里，由模板单独标注。
CANONICAL_TYPES: tuple[tuple[str, str, tuple[str, ...], bool], ...] = (
    ("E1", "E1 单位与量纲", ("E1_unit",), True),
    ("E2", "E2 估值倍数", ("E2_valuation_multiple", "E2_multiple"), True),
    ("E3", "E3 统计口径", ("E3_statistical_caliber", "E3_caliber"), True),
    ("E4", "E4 引用与来源", ("E4_citation",), True),
    ("E5", "E5 算术一致性", ("E5_arithmetic", "E5_arithmetic_consistency"), True),
    ("E6", "E6 定性矛盾", ("E6_qualitative",), False),
)

SEV_LABELS = {"confirmed": "已确认", "suspected": "疑似", "advisory": "提示"}

#: 页面路由（``pages[].route``）。与 ``agent/routing`` 的判定结果对应。
ROUTE_LABELS = {
    "A_text_only": "文本层直读",
    "B_scanned": "扫描件 → OCR/VLM",
    "C_garbled": "乱码 → OCR/VLM",
    "D_table": "表格页",
    "E_mixed": "混合页",
}


def _label(table: dict[str, str], key: str) -> str:
    """查标签；查不到就原样返回 —— 不静默吞掉未知类型。"""
    return table.get(key, key or "?")


def _count(values: Iterable[str]) -> list[tuple[str, int]]:
    """计数并按**频次降序**。次数相同则按名称排序，保证多次刷新顺序稳定。"""
    c = Counter(values)
    return sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))


def _fnum(v: Any, default: float = 0.0) -> float:
    """产物里的数字大多是**字符串**（保精度），这里统一转 float。

    转不动就返回 ``default`` —— 图表不该因为一个脏字段整块崩掉。

    ⚠️ **只用于「计数」类字段**（条数、页数、件数）：那里 0 是合理兜底。
    用于**量值**（金额、倍数、估值）会**凭空造数**，必须改用 ``_optnum``。
    """
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _optnum(v: Any) -> float | None:
    """能安全画进图里就返回 float，否则返回 ``None`` —— **绝不拿 0 顶替**。

    为什么必须有这个函数（2026-09-27 修）：``_fnum`` 对脏字段返回 0.0，
    用在量值上会让图表**自己造出一个数据点**。实测两例：

      · ``dcf.value_per_share = "N/A"`` → 图上出现标记「DCF：0 元」，
        且 0 参与 vmin/vmax 计算，把真实的 12.16–14.04 区间拖成 **-1.68–15.72**；
      · ``value_per_share = "NaN"`` → ``float('NaN')`` 能过 ``is not None``，
        渲染出 ``<circle cx="nan">``，坐标轴刻度被污染成 11.87/13.05/14.23。

    本模块的 docstring 写着「不重算任何业务数字，只做换个容器装」——
    把解析不了的值换成 0 恰恰是**新造了一个业务数字**，违反得最彻底的一条。
    正确姿势是**拿不到就不画**，与本文件 ``audit_charts`` 对缺失 metrics 的处理一致。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # NaN 与 ±inf 都不是可绘制的坐标（NaN 自比不等，是筛掉它的标准写法）
    return f if f == f and f not in (float("inf"), float("-inf")) else None


#: 待复核原因的归类规则：``(命中关键词, 归类名)``，**顺序敏感**（先匹配先归属）。
#: 为什么要归类：``review_reason`` 是整句中文（动辄上百字，含具体数值与证据 ID），
#: 直接当条形图标签会挤成一团。归类后只剩「哪类问题最多」，明细仍在表格里。
_REVIEW_RULES: tuple[tuple[str, str], ...] = (
    ("出现差异", "同表内数值冲突"),
    ("量纲不合理", "量纲不合理"),
    ("数量级", "同表数量级异常"),
    ("恒等式", "恒等式不平"),
    ("quote", "原文校验未过"),
)


def _review_bucket(reason: str) -> str:
    """把一整句复核原因压成一个短类别。"""
    for kw, name in _REVIEW_RULES:
        if kw in reason:
            return name
    return "其他"


# ---------------------------------------------------------------------------
# 选题 1 · 数据结构化提取
# ---------------------------------------------------------------------------


def facts_charts(facts: Sequence[Any]) -> dict[str, Markup]:
    """``/facts`` 与 ``/extract`` 共用的提取质量图。

    ``facts`` 是 ``FactView`` 列表（已由 ``RunArtifacts.build_facts()`` 构造）。
    """
    if not facts:
        return {}

    # 1) 来源层构成 —— 本项目「确定性优先」主张的直接证据。
    #    ⚠️ 必须先聚合再交给 stack()：它按**段**画 rect，直接传 359 条事实
    #    会生成 359 个 rect（实测 48 KB SVG），而语义上只有 2 段。
    layer_mix = C.stack(
        [(f"{_label(TAG_LABELS, k)}", v)
         for k, v in _count(f.layer_tag for f in facts)],
        label="来源层构成",
    )

    # 2) 确定性 vs 模型 —— 全项目的头号卖点，单独一张环图
    det = sum(1 for f in facts if f.extractor_deterministic)
    mdl = len(facts) - det
    det_ratio = C.donut(
        [("确定性路径", det), ("模型路径", mdl)],
        label="抽取路径",
        center=f"{det / len(facts) * 100:.0f}%",
        center_sub="确定性",
    )

    # 3) 置信度分布 —— 用来看「有没有一堆低置信的糊弄过去」
    conf_hist = C.hist(
        [f.confidence for f in facts],
        bins=10, x_label="置信度", label="置信度分布",
    )

    # 4) 各指标条数 top —— 哪些科目抽得多
    by_metric = _count(f.metric or "未命名" for f in facts)
    metric_bar = C.bar_h(
        by_metric, label="指标条目数", unit="条", max_rows=12,
    )

    # 5) 待复核原因 —— 需要人看的那部分长什么样。
    #    归类后再画（整句中文当标签会挤成一团），明细仍在下方表格里。
    n_review = sum(1 for f in facts if f.needs_review)
    reasons = _count(_review_bucket(f.review_reason or "") for f in facts if f.needs_review)
    review_bar = C.bar_h(
        reasons, label=f"待复核原因分类（共 {n_review} 条）",
        unit="条", max_rows=10,
    ) if reasons else Markup("")

    return {
        "layer_mix": layer_mix,
        "det_ratio": det_ratio,
        "conf_hist": conf_hist,
        "metric_bar": metric_bar,
        "review_bar": review_bar,
    }


# ---------------------------------------------------------------------------
# 选题 1 · 提取通道（``/extract`` 专有）
# ---------------------------------------------------------------------------


def extract_charts(run: Any) -> dict[str, Markup]:
    """``/extract`` 的通道与表格裁决图。``run`` 是 ``RunArtifacts``。

    ⚠️ ``channel_stats()`` / ``table_stats()`` 返回的是**扁平标量字典**
    （``{"text_pages": 273, "cells_agree": 277, ...}``），不是 ``{通道: {页数}}``。
    这里按**语义**挑字段重组 —— 全部字段的原始值仍在上方表格里可见，不丢信息。
    """
    out: dict[str, Markup] = {}

    # 1) 各通道覆盖页数 —— 「为什么需要多通道」的直观答案
    try:
        cs = run.channel_stats() or {}
    except Exception:
        cs = {}
    channels = [
        ("文本层直读", _fnum(cs.get("text_pages"))),
        ("OCR 识别", _fnum(cs.get("ocr_pages"))),
        ("VLM 视觉扫描", _fnum(cs.get("vlm_scan_pages"))),
    ]
    channels = [(n, v) for n, v in channels if v > 0]
    if channels:
        out["channel_bar"] = C.bar_h(channels, label="各通道覆盖页数", unit="页")

    # 2) VLM 扫描字段的可用性 —— 383 个读数里驳掉了多少，这是「宁缺毋滥」的直接证据
    usable = _fnum(cs.get("scan_fields_usable"))
    rejected = _fnum(cs.get("scan_fields_rejected"))
    if usable + rejected > 0:
        out["scan_stack"] = C.stack(
            [("通过校验", usable), ("驳回", rejected)],
            label=f"VLM 扫描字段可用性（共 {usable + rejected:g} 个读数）",
        )

    # 3) 表格单元格裁决构成 —— 表格解析的核心证据
    try:
        ts = run.table_stats() or {}
    except Exception:
        ts = {}
    cell_buckets = [
        ("与网格一致", _fnum(ts.get("cells_agree"))),
        ("已归一化", _fnum(ts.get("cells_normalized"))),
        ("已纠正", _fnum(ts.get("cells_corrected"))),
        ("驳回", _fnum(ts.get("cells_rejected"))),
        ("不可读", _fnum(ts.get("cells_unreadable"))),
        ("解析出错", _fnum(ts.get("cells_errors"))),
    ]
    cell_buckets = [(n, v) for n, v in cell_buckets if v > 0]
    if cell_buckets:
        out["cell_stack"] = C.stack(
            cell_buckets, label=f"表格单元格裁决构成（共 {_fnum(ts.get('cells_checked')):g} 格送检）",
        )

    # 4) 算术自洽校验 —— 失败的表数要显眼，这是**告警**不是构成
    checked = _fnum(ts.get("arith_checked"))
    failed = _fnum(ts.get("arith_failed_tables"))
    if checked > 0:
        out["arith_bar"] = C.bar_h(
            [("算术校验通过", checked - failed), ("算术校验未通过", failed)],
            label=f"表格算术自洽校验（共 {checked:g} 张表）",
            unit="张", alarm=("算术校验未通过",),
        )

    # 5) 页面路由构成 —— 路由把每页分给哪条通道
    try:
        pages = run.pages or []
    except Exception:
        pages = []
    routes = _count(
        ROUTE_LABELS.get(str(p.get("route", "")), str(p.get("route") or "未分类"))
        for p in pages if isinstance(p, dict)
    )
    if routes:
        out["route_donut"] = C.donut(
            routes, label="页面路由构成",
            center=str(len(pages)), center_sub="页",
        )

    return out


# ---------------------------------------------------------------------------
# 选题 5 · 研究报告纠错核查
# ---------------------------------------------------------------------------


def audit_charts(audit: dict[str, Any] | None) -> dict[str, Markup]:
    """``/audit`` 的核查结果图。

    ⚠️ ``metrics`` 为空是**正常情况**（演示 run 未挂 golden 标注），
    此时不画指标图，由模板渲染说明文字 —— 详见 design 文档 §5.2。
    """
    if not audit:
        return {}
    findings = audit.get("findings") or []
    out: dict[str, Markup] = {}

    # 1) 严重度构成 —— **构成条**，不是环图。
    #
    #    2026-09-27 改。原为环图 + alarm=("已确认",)。环图是这里最坏的选择：
    #    演示 run 只有一类严重度时，那一弧就是**整圈** —— 旧版把它 100% 填红，
    #    是本站最大的一块红色面积，正撞赛题「不大面积红黄」的硬约束。
    #    换成一整条 6px 高的构成条后，红色只可能落在「已确认」那一段的**长度**上，
    #    且段的填充恒为墨阶，语义改由「段长 + 图例数值 + 记号」承担。
    sev = _count(_label(SEV_LABELS, f.get("severity", "?")) for f in findings)
    if sev:
        out["sev_stack"] = C.stack(
            list(sev), label=f"问题严重度构成（共 {len(findings)} 个问题）",
        )

    # 2) 错误类型分布 —— 横向条，中文标签放得下
    typ = _count(
        _label(E1_LABELS, f.get("error_type", "?")) for f in findings
    )
    if typ:
        out["type_bar"] = C.bar_h(typ, label="错误类型分布", unit="个", max_rows=12)

    # 2b) 完整 E1–E6 能力清单 —— **含 0 计数**，与上图互补：
    #     上图答「本次查出了什么」，本表答「这套核查覆盖哪几类」。
    #     只按出现过的类型列举，会让「E4 未出现」被误读成「不支持 E4」。
    #
    #     **不随 findings 为空而消失**：一份完全干净的草稿（0 检出）恰恰最需要
    #     这张清单 —— 「六类全查过、都没问题」和「压根没查」在页面上必须能分辨。
    raw = dict(_count(f.get("error_type") or "?" for f in findings))
    # (显示名, 计数, 是否已实现) —— 第 3 位让模板能把「未检出」与「未实现」分开呈现。
    taxonomy: list[tuple[str, int, bool]] = []
    for _code, name, aliases, implemented in CANONICAL_TYPES:
        taxonomy.append((name, sum(raw.pop(a, 0) for a in aliases), implemented))
    # 表外类型（上游新增而此处未及同步）**原样列出**，不静默吞掉 —— 计数会失真
    # 本身就是需要被看见的信号。见 E1_LABELS 上方注释。表外类型按「已实现」处理：
    # 它能出现在产物里，就证明有规则产出它。
    for leftover in sorted(raw):
        taxonomy.append((f"{_label(E1_LABELS, leftover)}（表外）", raw[leftover], True))
    out["type_taxonomy"] = taxonomy

    # 3) P/R/F1 —— 有标注才算得出，没有就不画
    #    ⚠️ 这三项是**比率**不是计数，绝不能走 `_fnum`：它兜底成 0，图上就会画出
    #    「精确率 0%」「F1 0%」—— 那是「报了全错」的强断言，而真相往往是
    #    「本 run 没有标注集、根本算不出来」。缺项就**少画一根条**，
    #    不画一根假的 0。见 `_optnum` 注释。
    m = audit.get("metrics") or {}
    prf: list[tuple[str, float]] = []
    for _nm, _key in (("精确率 P", "precision"), ("召回率 R", "recall"), ("F1", "f1")):
        _pv = _optnum(m.get(_key))
        if _pv is not None:
            prf.append((_nm, _pv * 100))
    if prf:
        out["prf_bar"] = C.bar_h(prf, label="核查指标（%）", unit="%")

    return out


# ---------------------------------------------------------------------------
# 选题 4 · 自动化估值建模
# ---------------------------------------------------------------------------


def valuation_charts(val: dict[str, Any] | None) -> dict[str, Markup]:
    """``/valuation`` 的估值图。``val`` 是 ``valuation.json`` 的内容。"""
    if not val:
        return {}
    out: dict[str, Markup] = {}

    # 三个区间端点同样是**量值**，一律 `_optnum`：取不到就是 None，
    # 下面用 `is not None` 判定是否画图。原先写 `_fnum(..., float("nan"))`
    # 再靠 `lo == lo`（NaN 自比不等）筛缺值 —— 虽然不造数，但一旦有人
    # 顺手把默认值改成 0 就会静默变味；`None` 是显式的，不会有人误读。
    lo = _optnum(val.get("value_range_low"))
    mid = _optnum(val.get("value_range_mid"))
    hi = _optnum(val.get("value_range_high"))

    # 1) 估值区间主图 —— 各方法单点作标记
    #    标量一律走 `_optnum`：取不到就**不放这个标记**，不拿 0 顶替
    #    （0 会连坐标轴区间一起拖坏，见 `_optnum` 注释）。
    markers: list[tuple[str, float]] = []
    dcf = val.get("dcf") or {}
    comps = val.get("comps") or {}
    _dcf_v = _optnum(dcf.get("value_per_share"))
    if _dcf_v is not None:
        markers.append(("DCF", _dcf_v))
    for key, nm in (("implied_value_per_share_low", "可比·低"),
                    ("implied_value_per_share_mid", "可比·中"),
                    ("implied_value_per_share_high", "可比·高")):
        _v = _optnum(comps.get(key))
        if _v is not None:
            markers.append((nm, _v))
    if lo is not None and mid is not None and hi is not None:
        out["range"] = C.range_chart(lo, mid, hi, markers=markers,
                                     label="估值区间")

    # 2) 龙卷风 —— 产物里已算好摆动幅度，直接透传（含 base/low/high/swing）
    tor = (val.get("sensitivity") or {}).get("tornado") or []
    if tor:
        out["tornado"] = C.tornado(tor, label="敏感性：各假设对每股价值的影响",
                                   unit="元")

    # 3) 可比公司倍数 —— 一眼看出标的相对同业贵还是便宜
    peers = comps.get("peers") or []
    if peers:
        def _peer_rows(key: str) -> list[tuple[str, float]]:
            rows_: list[tuple[str, float]] = []
            for p in peers:
                v = _optnum(p.get(key))
                if v is not None:                    # 脏值直接不画，不补 0
                    rows_.append((f"{p.get('name', '?')} {p.get('ticker', '')}".strip(), v))
            return rows_

        def _median_label(key: str, name: str) -> str:
            m = _optnum(comps.get(key))
            return f"{name}（中位数 {m:.2f}）" if m is not None else name

        _pe_rows, _pb_rows = _peer_rows("pe"), _peer_rows("pb")
        if _pe_rows:
            out["peer_pe"] = C.bar_h(_pe_rows, label=_median_label("median_pe", "可比公司 PE"),
                                     max_rows=12)
        if _pb_rows:
            out["peer_pb"] = C.bar_h(_pb_rows, label=_median_label("median_pb", "可比公司 PB"),
                                     max_rows=12)

    # 4) 预测期经营轨迹 —— 收入与 FCFF 两条读数
    years = dcf.get("explicit_years") or []
    if years:
        def _year_rows(key: str) -> list[tuple[str, float]]:
            rows_: list[tuple[str, float]] = []
            for y in years:
                v = _optnum(y.get(key))
                if v is not None:
                    rows_.append((str(y.get("year", "?")), v / 1e8))
            return rows_

        _rev, _fcff = _year_rows("revenue"), _year_rows("fcff")
        if _rev:
            out["proj_revenue"] = C.bar_h(_rev, label="预测期营业收入",
                                          unit="亿元", max_rows=12)
        if _fcff:
            out["proj_fcff"] = C.bar_h(_fcff, label="预测期企业自由现金流 FCFF",
                                       unit="亿元", max_rows=12)

    # 5) 终值占比 —— 估值里最该被质疑的结构性数字，单独一张堆叠条
    tv_share = _optnum(dcf.get("terminal_value_share"))
    if tv_share is not None and 0 <= tv_share <= 1:
        out["tv_share"] = C.stack(
            [("终值现值", tv_share), ("明确预测期", 1 - tv_share)],
            label=f"企业价值构成：终值占 {tv_share * 100:.1f}%",
        )

    return out
