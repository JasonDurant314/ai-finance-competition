"""内联 SVG 图表 —— 零依赖、自动匹配主题。

## 为什么不用图表库

本项目的硬约束是「受控数据环境」：页面不得依赖外部 CDN。因此这里**不引入任何
JS 图表库**（那既要联网，又要在 ``THIRD_PARTY.md`` 里多声明一份第三方依赖）。

## 为什么是内联 SVG

内联 SVG 会被浏览器当作**文档的一部分**渲染，因此可以直接引用 ``app.css`` 里的
CSS 变量（``var(--s1)`` 等）—— 这意味着：

    · 图表配色与页面**同一套值**，不必维护第二份配色表；
    · 主题一变，图表跟着变，无需重新生成；
    · 矢量，投影仪上放大不糊；
    · 不落盘、不增加 HTTP 请求。

⚠️ 颜色一律走 ``style="fill:var(--s1)"`` 而**不是** ``fill="var(--s1)"``：
SVG 的**表现属性**（presentation attribute）对 ``var()`` 的支持在各浏览器上
并不一致，而 ``style`` 属性是稳的。踩过这个坑，所以全部用 style。

## 与 r4 产物图的关系

``pipelines/r4_valuation/report.py`` 也用 matplotlib 出 4 张 PNG —— 那些是**交付
产物**（报告 Markdown 里按路径引用，白底适合进正式报告），本模块**不动它们**。
网页另渲染一份主题匹配的 SVG 版本，两者并存、各司其职。

## 安全

标签文本来自运行产物（指标名、主体名等，源头是文档内容），因此**一律转义**。
返回值是 ``Markup``，供 Jinja2 直接输出而不被二次转义。
"""

from __future__ import annotations

import html
from typing import Any, Iterable, Sequence

from markupsafe import Markup

__all__ = [
    "bar_h",
    "stack",
    "donut",
    "hist",
    "range_chart",
    "tornado",
    "fmt_num",
]

# 墨阶 —— 单一蓝黑墨的五级**明度**阶，不含任何色相差异（像双色印刷的网点）。
# 只用于**有序**数据（层号 L1/L2/L3、严重度、置信度分箱），序越重墨越深。
# 类别型数据一律单墨 `_INK`，不按类别上色 —— 见 `bar_h`。
_SERIES = ("var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)")
_INK = "var(--s1)"          # 单墨：所有类别型 / 单类别数据的填充
_MUTED = "var(--dim2)"
_AXIS = "var(--axis)"
_GRID = "var(--grid)"
_RULE = "var(--rule)"       # 图元一律带 1px 描边 —— s4/s5 低于 3:1，靠它保证形状可辨

# 段落文字色按**明度**择：深段用纸色，浅段用墨色。
# 这一条同时修掉浅底方案的死穴 —— 原先硬编码 fill:#0d1117（暗底假设），
# 换到纸面上就是「深字压深段」，当场看不清。见 stack()。
_PCT_TEXT_ON = {0: "var(--sheet)", 1: "var(--sheet)", 2: "var(--ink)",
                3: "var(--ink)", 4: "var(--ink)"}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _esc(value: Any) -> str:
    """转义进 SVG 文本的任意值。标签源头是文档内容，必须转义。"""
    return html.escape(str(value), quote=True)


def _color(i: int) -> str:
    return _SERIES[i % len(_SERIES)]


def fmt_num(v: float, *, unit: str = "") -> str:
    """把数字压成短、可读的形式（中文数量级）。

    图表里的数值标注位置很窄，``1234567890`` 会直接把画布撑爆，
    因此按「亿 / 万」降位，并保留 3 位有效数字。
    """
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "—"
    sign = "-" if x < 0 else ""
    a = abs(x)
    if a >= 1e8:
        return f"{sign}{a / 1e8:.2f}亿{unit}"
    if a >= 1e4:
        return f"{sign}{a / 1e4:.1f}万{unit}"
    if a >= 100:
        return f"{sign}{a:,.0f}{unit}"
    if a == int(a):
        return f"{sign}{int(a)}{unit}"
    return f"{sign}{a:.2f}{unit}"


def _svg(width: int, height: int, body: str, *, label: str = "") -> Markup:
    """套上 svg 外壳。

    ``role="img"`` + ``aria-label``：屏幕阅读器至少要能说出这张图是什么。
    ``width:100%;height:auto`` + viewBox：随容器缩放，不用写响应式分支。

    ★ ``max-width`` 是必须的：viewBox 宽 560，独占一行的图在 1440px 屏上
    会被放大到 2.3 倍 —— 按 12px 画的轴标签渲染成 28px，整张图糊成标题。
    上限取 1.3 倍（标签 ≈15px，与正文 14.5px 同一档），
    两栏并排时列宽 630px 够不着上限，仍是满格铺开。
    """
    aria = f' role="img" aria-label="{_esc(label)}"' if label else ' aria-hidden="true"'
    return Markup(
        f'<svg viewBox="0 0 {width} {height}" '
        f'style="width:100%;height:auto;display:block;overflow:visible;'
        f'max-width:{int(width * 1.3)}px"'
        f"{aria}>{body}</svg>"
    )


def _empty(message: str = "无数据") -> Markup:
    return Markup(
        f'<div style="color:var(--dim2);font-size:12.5px;padding:18px 0;'
        f'text-align:center">{_esc(message)}</div>'
    )


def _legend(items: Sequence[Any], *, x: int, y: int,
            row_h: int = 19, max_w: int = 999) -> str:
    """图例。``items`` 是 ``(标签, 色块颜色)`` 或 ``(标签, 色块颜色, 文字颜色)``。

    色块 + 文字，文字默认用 ``--fg`` 保证对比度 —— 图例是读数的地方，不能是装饰。
    第 3 位留给需要强调的类目文字色（现仅用于印章红的记号文字）。

    ⚠️ 色块带 1px 描边：墨阶的浅端（s4 2.15:1 / s5 1.55:1）对纸面不足 3:1，
    没有描边时色块与纸面糊在一起。
    """
    out: list[str] = []
    for i, it in enumerate(items):
        label, color = it[0], it[1]
        text_fill = it[2] if len(it) > 2 and it[2] else "var(--fg)"
        cy = y + i * row_h
        if cy > max_w:
            break
        out.append(
            f'<rect x="{x}" y="{cy - 7}" width="9" height="9" '
            f'style="fill:{color};stroke:{_RULE};stroke-width:1"/>'
            f'<text x="{x + 15}" y="{cy + 1}" font-size="12" '
            f'style="fill:{text_fill}">{_esc(label)}</text>'
        )
    return "".join(out)


# ---------------------------------------------------------------------------
# 横向条形图
# ---------------------------------------------------------------------------


def bar_h(items: Iterable[Any], *, width: int = 560, row_h: int = 26,
          label_w: int = 150, unit: str = "",
          label: str = "", max_rows: int = 40,
          alarm: Sequence[str] = ()) -> Markup:
    """横向条形图。

    ``items`` 接受 ``(标签, 数值)`` 或 ``{"label","value","color"}``。
    横向而非纵向：指标名是中文长标签，纵向排会互相压字。

    **类目一律单墨**（``_INK``），不按类别上色 —— E1–E6 错误类型、抽取器名、
    通道名、公司名全部交给轴标签。类别之间没有大小之分，用色相区分等于
    凭空造出一层「谁比谁重要」的暗示。需要有序明度阶时用 ``stack``。

    ``alarm`` 里的标签（精确匹配）标出，但**记号只落在几个字符与一根 3px 竖条上**：
    ``×`` 记号 + 标签/数值走 ``--stamp`` + 行左 3px 印章条。
    **绝不换掉整根条的颜色** —— 在「84 张表里 31 张算术校验未通过」这类数据下，
    整条填红就是半根红条，落在图上正属赛题明令避免的「大面积红黄」。
    告警该靠**记号 + 数值颜色**传达，不该靠面积。
    """
    rows: list[tuple[str, float, str | None]] = []
    for it in items:
        if isinstance(it, dict):
            rows.append((str(it.get("label", "")), float(it.get("value", 0)),
                         it.get("color")))
        else:
            lab, val = it[0], it[1]
            rows.append((str(lab), float(val), None))
    if not rows:
        return _empty()
    rows = rows[:max_rows]

    top, bottom = 6, 6
    h = top + len(rows) * row_h + bottom
    vmax = max((abs(v) for _, v, _ in rows), default=0) or 1
    bar_x = label_w + 8
    bar_max = width - bar_x - 62          # 右侧留出数值标注的位置

    body: list[str] = []
    for i, (lab, val, color) in enumerate(rows):
        y = top + i * row_h
        bh = row_h - 10
        bw = max(1.5, abs(val) / vmax * bar_max)
        flag = lab in alarm
        fill = color or _INK                # ★ 面积恒为单墨，alarm 不改它
        # 行底纹：隔行轻微加深，长列表里好对行（用实色 --sheet-2，不用 alpha）
        if i % 2:
            body.append(
                f'<rect x="0" y="{y - 2}" width="{width}" height="{row_h}" '
                f'style="fill:var(--sheet-2)"/>'
            )
        # 告警行的左竖条 —— 印章红的授权形态之一（3px 宽，面积可忽略）
        if flag:
            body.append(
                f'<rect x="0" y="{y - 2}" width="3" height="{row_h}" '
                f'style="fill:var(--stamp)"/>'
            )
        body.append(
            f'<text x="{label_w}" y="{y + bh / 2 + 1}" font-size="12.5" '
            f'text-anchor="end" style="fill:'
            f'{"var(--stamp)" if flag else "var(--dim)"}">'
            f'{_esc("× " if flag else "")}{_esc(lab)}</text>'
        )
        body.append(
            f'<rect x="{bar_x}" y="{y + 2}" width="{bw:.1f}" height="{bh}" '
            f'style="fill:{fill};stroke:{_RULE};stroke-width:1">'
            f'<title>{_esc(lab)}：{_esc(fmt_num(val, unit=unit))}</title></rect>'
        )
        body.append(
            f'<text x="{bar_x + bw + 8:.1f}" y="{y + bh / 2 + 1}" '
            f'font-size="12" style="fill:'
            f'{"var(--stamp)" if flag else "var(--fg)"};'
            f'font-variant-numeric:tabular-nums">'
            f'{_esc(fmt_num(val, unit=unit))}</text>'
        )
    return _svg(width, h, "".join(body), label=label)


# ---------------------------------------------------------------------------
# 堆叠条（占比）
# ---------------------------------------------------------------------------


def stack(segments: Iterable[Any], *, width: int = 560, height: int = 34,
          label: str = "", show_pct: bool = True,
          legend: bool = True) -> Markup:
    """单条堆叠占比图。

    适合「构成」类结论（三层路由各占多少、确定性 vs 模型各占多少）——
    结论本身就是「比例」，用饼/环反而更难比较，一条 100% 堆叠条更直接。
    """
    segs: list[tuple[str, float, str | None]] = []
    for it in segments:
        if isinstance(it, dict):
            segs.append((str(it.get("label", "")), float(it.get("value", 0)),
                         it.get("color")))
        else:
            name, val = it[0], it[1]
            segs.append((str(name), float(val), None))
    segs = [s for s in segs if s[1] > 0]
    if not segs:
        return _empty()

    total = sum(v for _, v, _ in segs) or 1
    body: list[str] = []
    x = 0.0
    for i, (name, val, color) in enumerate(segs):
        w = val / total * width
        # 构成图的段是**有序**的（层号 / 裁决档位），故走墨阶；不按类别上色。
        fill = color or _color(i)
        pct = val / total * 100
        body.append(
            f'<rect x="{x:.1f}" y="0" width="{max(0.0, w - 1.2):.1f}" '
            f'height="{height}" style="fill:{fill};stroke:{_RULE};stroke-width:1">'
            f'<title>{_esc(name)}：{val:g}（{pct:.1f}%）</title></rect>'
        )
        # 只在段够宽时写百分比，否则文字会溢出到相邻段。
        # ⚠️ 字色按**段自身的明度**择 —— 原先硬编码 #0d1117（暗底假设），
        # 换到纸面上就是深字压深段，看不清。
        if show_pct and w >= 34:
            body.append(
                f'<text x="{x + w / 2:.1f}" y="{height / 2 + 4.5}" '
                f'font-size="12.5" text-anchor="middle" '
                f'style="fill:{_PCT_TEXT_ON[i % len(_SERIES)]};font-weight:600">'
                f'{pct:.0f}%</text>'
            )
        x += w

    h = height
    if legend:
        h += 8 + len(segs) * 19
        body.append(
            _legend([(f"{n} · {v:g}", c or _color(i))
                     for i, (n, v, c) in enumerate(segs)], x=0, y=height + 20)
        )
    return _svg(width, h, "".join(body), label=label)


# ---------------------------------------------------------------------------
# 环形图
# ---------------------------------------------------------------------------


def donut(items: Iterable[Any], *, size: int = 170, thickness: int = 24,
          label: str = "", center: str = "", center_sub: str = "") -> Markup:
    """环形图。

    用 ``stroke-dasharray`` 画弧而**不是**手算 path 的 ``A`` 指令 ——
    弧长就是 ``2πr``，按比例切分即可，既短又不会在 100% 处出现接缝。

    环心留白放**总数**：环形图最大的毛病是「看不出总量」，中心写死一个数就补上了。
    """
    rows: list[tuple[str, float, str | None]] = []
    for it in items:
        if isinstance(it, dict):
            rows.append((str(it.get("label", "")), float(it.get("value", 0)),
                         it.get("color")))
        else:
            nm, val = it[0], it[1]
            rows.append((str(nm), float(val), None))
    rows = [r for r in rows if r[1] > 0]
    if not rows:
        return _empty()

    total = sum(v for _, v, _ in rows) or 1
    r = size / 2 - thickness / 2 - 2
    cx = cy = size / 2
    circ = 2 * 3.141592653589793 * r

    body: list[str] = [
        f'<circle cx="{cx}" cy="{cy}" r="{r:.1f}" fill="none" '
        f'stroke-width="{thickness}" style="stroke:var(--sheet-2)"/>'
    ]
    offset = 0.0
    for i, (name, val, color) in enumerate(rows):
        frac = val / total
        seg = frac * circ
        # ★ 弧的面积一律走墨阶，**不接受任何告警色**。
        #   环图是本站最危险的一块面积：只有一类时那一弧就是**整圈** ——
        #   旧版在「已确认」单类下把 100% 的环填红，是全场最大的红色区块。
        #   严重度这类信息改由「构成条 + 记号 + 排序」承担（见 audit_charts）。
        fill = color or _color(i)
        # 段与段之间留 2px 缝，视觉上不糊成一片；最后一小段不留，避免缺口
        dash = max(0.0, seg - 2)
        body.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r:.1f}" fill="none" '
            f'stroke-width="{thickness}" '
            f'stroke-dasharray="{dash:.2f} {circ - dash:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" '
            f'transform="rotate(-90 {cx} {cy})" '
            f'style="stroke:{fill}"><title>{_esc(name)}：{val:g}'
            f'（{frac * 100:.1f}%）</title></circle>'
        )
        offset += seg

    if center:
        body.append(
            f'<text x="{cx}" y="{cy + (2 if not center_sub else -3)}" '
            f'font-size="21" font-weight="600" text-anchor="middle" '
            f'style="fill:var(--fg);font-variant-numeric:tabular-nums">'
            f'{_esc(center)}</text>'
        )
    if center_sub:
        body.append(
            f'<text x="{cx}" y="{cy + 15}" font-size="11.5" '
            f'text-anchor="middle" style="fill:var(--dim2)">'
            f'{_esc(center_sub)}</text>'
        )

    # 图例放右侧，与环并排 —— 放下面会让卡片变很高。
    legend_items = [
        (f"{n} · {v:g}", c or _color(i)) for i, (n, v, c) in enumerate(rows)
    ]
    lx = size + 14
    body.append(_legend(legend_items, x=lx, y=cy - (len(rows) - 1) * 9.5))

    return _svg(int(size + 14 + 190), size, "".join(body), label=label)


# ---------------------------------------------------------------------------
# 直方图
# ---------------------------------------------------------------------------


def hist(values: Sequence[float], *, bins: int = 10, width: int = 560,
         height: int = 150, label: str = "", x_label: str = "",
         unit: str = "") -> Markup:
    """直方图 —— 看**分布**，不是看总量。

    置信度这类数据，平均值没有意义（一堆 0.95 加几个 0.6，均值照样好看），
    必须看它长什么样。这是「结果稳定性」最诚实的呈现方式。
    """
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 2:
        return _empty("样本不足，无法绘制分布")

    lo, hi = min(vals), max(vals)
    if hi <= lo:
        hi = lo + 1e-9
    counts = [0] * bins
    for v in vals:
        idx = min(bins - 1, int((v - lo) / (hi - lo) * bins))
        counts[idx] += 1
    cmax = max(counts) or 1

    pad_l, pad_r, pad_t, pad_b = 34, 10, 12, 30
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    bw = plot_w / bins

    body: list[str] = []
    # 水平网格线：只画 3 条，够读高度即可，多了是噪声
    for g in (0, 0.5, 1.0):
        gy = pad_t + plot_h * (1 - g)
        body.append(
            f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{width - pad_r}" '
            f'y2="{gy:.1f}" style="stroke:{_GRID}" stroke-width="1"/>'
            f'<text x="{pad_l - 6}" y="{gy + 4:.1f}" font-size="10.5" '
            f'text-anchor="end" style="fill:var(--dim2)">'
            f'{int(round(cmax * g))}</text>'
        )
    for i, c in enumerate(counts):
        if c <= 0:
            continue
        bh = c / cmax * plot_h
        bx = pad_l + i * bw
        body.append(
            f'<rect x="{bx + 1:.1f}" y="{pad_t + plot_h - bh:.1f}" '
            f'width="{max(1.0, bw - 2):.1f}" height="{bh:.1f}" '
            f'style="fill:var(--s1);stroke:{_RULE};stroke-width:1">'
            f'<title>{lo + (hi - lo) * i / bins:.3g} – '
            f'{lo + (hi - lo) * (i + 1) / bins:.3g}：{c} 条</title></rect>'
        )
    # 只标首尾两个刻度，中间留白 —— 中文标签横排容易挤
    for frac, anchor, tx in ((0.0, "start", pad_l), (1.0, "end", width - pad_r)):
        body.append(
            f'<text x="{tx}" y="{height - 10}" font-size="10.5" '
            f'text-anchor="{anchor}" style="fill:var(--dim2)">'
            f'{_esc(fmt_num(lo + (hi - lo) * frac, unit=unit))}</text>'
        )
    if x_label:
        body.append(
            f'<text x="{width / 2}" y="{height - 10}" font-size="11" '
            f'text-anchor="middle" style="fill:var(--dim2)">{_esc(x_label)}</text>'
        )
    return _svg(width, height, "".join(body), label=label)


# ---------------------------------------------------------------------------
# 估值区间图
# ---------------------------------------------------------------------------


def range_chart(lo: float, mid: float, hi: float, *,
                markers: Sequence[tuple[str, float]] = (),
                width: int = 560, label: str = "", unit: str = "元",
                axis_label: str = "每股价值") -> Markup:
    """区间图 —— 估值结论的主图。

    一张图回答「估了多少钱」：横轴是每股价值，条形是区间，菱形是各方法的单点。
    比堆数字卡片直观得多，答辩时可以直接指着讲。
    """
    vals = [lo, mid, hi] + [v for _, v in markers]
    if not all(isinstance(v, (int, float)) for v in vals):
        return _empty()
    vmin, vmax = min(vals), max(vals)
    if vmax <= vmin:
        vmax = vmin + 1
    span = vmax - vmin
    vmin -= span * 0.12
    vmax += span * 0.12

    pad_l, pad_r, pad_t = 18, 18, 30
    plot_w = width - pad_l - pad_r

    def X(v: float) -> float:
        return pad_l + (v - vmin) / (vmax - vmin) * plot_w

    row_h = 34
    h = pad_t + row_h * 2 + 40
    body: list[str] = []

    # 主区间
    y0 = pad_t
    body.append(
        f'<rect x="{X(lo):.1f}" y="{y0}" width="{max(1.0, X(hi) - X(lo)):.1f}" '
        f'height="22" style="fill:var(--s2);stroke:{_RULE};stroke-width:1">'
        f'<title>综合估值区间 {lo:g} – {hi:g} {_esc(unit)}</title></rect>'
    )
    body.append(
        f'<line x1="{X(mid):.1f}" y1="{y0 - 4}" x2="{X(mid):.1f}" '
        f'y2="{y0 + 26}" style="stroke:var(--fg)" stroke-width="2.5"/>'
        f'<text x="{X(mid):.1f}" y="{y0 - 9}" font-size="12" font-weight="600" '
        f'text-anchor="middle" style="fill:var(--fg)">中值 {mid:g}</text>'
    )
    body.append(
        f'<text x="{pad_l}" y="{y0 + 38}" font-size="11.5" '
        f'style="fill:var(--dim)">综合估值区间 '
        f'{lo:g} – {hi:g} {_esc(unit)}</text>'
    )

    # 各方法单点 —— **单墨**。落点之间靠「形 + 下方文字标签」区分，
    # 不靠色相：五个色相会让人以为方法之间有优劣次序，而它们只是不同口径。
    y1 = y0 + 52
    for i, (name, v) in enumerate(markers):
        x = X(v)
        body.append(
            f'<line x1="{x:.1f}" y1="{y1 - 6}" x2="{x:.1f}" y2="{y1 + 20}" '
            f'style="stroke:{_INK}" stroke-width="2"/>'
            f'<circle cx="{x:.1f}" cy="{y1 + 7}" r="4.5" '
            f'style="fill:{_INK};stroke:var(--sheet);stroke-width:1.5">'
            f'<title>{_esc(name)}：{v:g} {_esc(unit)}</title></circle>'
        )
        body.append(
            f'<text x="{pad_l + i * 150}" y="{y1 + 36}" font-size="11.5" '
            f'style="fill:var(--dim)">{_esc(name)} '
            f'<tspan style="fill:var(--fg);font-weight:600">{v:g}</tspan></text>'
        )

    # 轴线
    body.append(
        f'<line x1="{pad_l}" y1="{h - 12}" x2="{width - pad_r}" y2="{h - 12}" '
        f'style="stroke:{_AXIS}" stroke-width="1"/>'
    )
    for frac in (0.0, 0.5, 1.0):
        v = vmin + (vmax - vmin) * frac
        body.append(
            f'<text x="{X(v):.1f}" y="{h - 1}" font-size="10.5" '
            f'text-anchor="middle" style="fill:var(--dim2)">{v:.2f}</text>'
        )
    body.append(
        f'<text x="{width - pad_r}" y="{pad_t - 14}" font-size="11" '
        f'text-anchor="end" style="fill:var(--dim2)">{_esc(axis_label)}'
        f'（{_esc(unit)}）</text>'
    )
    return _svg(width, int(h), "".join(body), label=label)


# ---------------------------------------------------------------------------
# 龙卷风图（敏感性）
# ---------------------------------------------------------------------------


def tornado(items: Iterable[Any], *, width: int = 560, row_h: int = 30,
            label_w: int = 130, base: float | None = None,
            label: str = "", unit: str = "") -> Markup:
    """龙卷风图 —— 敏感性分析的**标准图形**，一眼看出谁最敏感。

    每行是一根从「下调情景」到「上调情景」的条，全部围绕基准值对齐。
    按摆动幅度降序：最敏感的在最上面，因为那才是结论。
    """
    rows: list[tuple[str, float, float, float | None]] = []
    for it in items:
        if isinstance(it, dict):
            rows.append((
                str(it.get("assumption") or it.get("label") or ""),
                float(it.get("low", 0)),
                float(it.get("high", 0)),
                float(it["base"]) if it.get("base") is not None else None,
            ))
        else:
            nm, lo_, hi_ = it[0], float(it[1]), float(it[2])
            rows.append((str(nm), lo_, hi_, None))
    if not rows:
        return _empty()

    # 摆动幅度大的排上面 —— 那才是结论所在
    rows.sort(key=lambda r: abs(r[2] - r[1]), reverse=True)
    base_v = base if base is not None else (rows[0][3] or 0.0)

    lo_all = min([r[1] for r in rows] + [base_v])
    hi_all = max([r[2] for r in rows] + [base_v])
    if hi_all <= lo_all:
        hi_all = lo_all + 1
    pad = (hi_all - lo_all) * 0.06
    lo_all -= pad
    hi_all += pad

    pad_l, pad_r, pad_t, pad_b = label_w + 10, 58, 26, 26
    plot_w = width - pad_l - pad_r
    h = pad_t + len(rows) * row_h + pad_b

    def X(v: float) -> float:
        return pad_l + (v - lo_all) / (hi_all - lo_all) * plot_w

    body: list[str] = []
    body.append(
        f'<text x="{pad_l}" y="12" font-size="11" style="fill:var(--dim2)">'
        f'基准 {_esc(fmt_num(base_v, unit=unit))}</text>'
    )
    # 基准竖线
    body.append(
        f'<line x1="{X(base_v):.1f}" y1="{pad_t - 8}" x2="{X(base_v):.1f}" '
        f'y2="{h - pad_b + 6}" style="stroke:{_AXIS}" stroke-width="1.5" '
        f'stroke-dasharray="3 3"/>'
    )

    for i, (name, lo_v, hi_v, _b) in enumerate(rows):
        y = pad_t + i * row_h
        bh = row_h - 12
        # 下行段（低于基准）与上行段分别着色：同一色系的两个明度
        for (v1, v2, color) in ((min(lo_v, base_v), max(lo_v, base_v), "var(--s3)"),
                                (min(hi_v, base_v), max(hi_v, base_v), "var(--s1)")):
            x1, x2 = X(v1), X(v2)
            if x2 - x1 < 0.6:
                continue
            body.append(
                f'<rect x="{x1:.1f}" y="{y}" width="{x2 - x1:.1f}" '
                f'height="{bh}" style="fill:{color};stroke:{_RULE};stroke-width:1"/>'
            )
        body.append(
            f'<text x="{label_w}" y="{y + bh / 2 + 1}" font-size="12" '
            f'text-anchor="end" style="fill:var(--dim)">{_esc(name)}</text>'
        )
        body.append(
            f'<text x="{width - 4}" y="{y + bh / 2 + 1}" font-size="11" '
            f'text-anchor="end" style="fill:var(--fg);'
            f'font-variant-numeric:tabular-nums">'
            f'{_esc(fmt_num(hi_v - lo_v, unit=unit))}</text>'
        )
        # 悬停给出完整三情景，正文表格可以收起来
        body.append(
            f'<rect x="{pad_l}" y="{y}" width="{plot_w}" height="{bh}" '
            f'style="fill:transparent"><title>{_esc(name)}：'
            f'下调 {lo_v:g} / 基准 {base_v:g} / 上调 {hi_v:g}</title></rect>'
        )

    body.append(
        f'<line x1="{pad_l}" y1="{h - pad_b + 6}" x2="{width - pad_r}" '
        f'y2="{h - pad_b + 6}" style="stroke:{_AXIS}" stroke-width="1"/>'
    )
    body.append(
        f'<text x="{width - 4}" y="{pad_t - 14}" font-size="10.5" '
        f'text-anchor="end" style="fill:var(--dim2)">摆动幅度</text>'
    )
    return _svg(width, int(h), "".join(body), label=label)
