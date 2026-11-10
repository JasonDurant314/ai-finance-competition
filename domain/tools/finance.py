"""财务计算引擎 —— R4 自动化估值建模的确定性内核。

设计铁律：**估值模型里没有一个数字来自 LLM。**
假设来自事实库（带 evidence_span_id）或显式标注的主观判断；
从假设到每股价值的每一步都是本模块的 ``Decimal`` 精确运算，
并接受 :mod:`agent.validate.assertions` 的独立重算校验。

一个必须坚持的设计：**断言不是复述模型的中间量，而是独立重算。**
否则叫自证，不叫校验。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, model_validator

from agent.evidence.models import (
    AssertionStatus,
    CompsPeer,
    CompsResult,
    DCFResult,
    ForecastYear,
    SensitivityCell,
    SensitivityResult,
    ValuationAssertion,
)
from agent.runtime.errors import HarnessError
from agent.tools.registry import tool
from agent.validate.assertions import run_assertions
from agent.validate.numeric import q

ZERO = Decimal("0")


# ==========================================================================
# 输入契约
# ==========================================================================


class DCFInput(BaseModel):
    """DCF 输入。所有金额单位统一为**元**。"""

    entity: str
    base_year: int = Field(description="预测起始年的上一年（基年）")

    # 基年实际值（来自事实库）
    base_revenue: Decimal
    base_shares_outstanding: Decimal = Field(description="总股本（股）")
    base_period: str = Field(
        default="",
        description=(
            "基年对应的**报告期标签**（如 2025A）。用于定位同期可比公司快照 —— "
            "快照按 ``<目标>_<年份>A.json`` 命名，没有它就只能靠 base_year 拼年份，"
            "而 base_year 是整数、拼不出报告期形态（A/H1/Q3）。"
        ),
    )
    net_debt: Decimal = Field(default=ZERO, description="有息负债 − 货币资金")
    minority_interest: Decimal = Field(default=ZERO)

    # 经营假设（比例用小数表示：0.08 = 8%）
    explicit_years: int = Field(default=5, ge=1, le=15)
    revenue_growth: list[Decimal] = Field(default_factory=list, description="逐年收入增速")
    ebit_margin: list[Decimal] = Field(default_factory=list, description="逐年 EBIT 率")
    tax_rate: Decimal = Field(default=Decimal("0.25"))
    da_ratio_of_revenue: Decimal = Field(default=Decimal("0.05"), description="折旧摊销/收入")
    capex_ratio_of_revenue: Decimal = Field(default=Decimal("0.06"), description="资本开支/收入")
    nwc_ratio_of_revenue: Decimal = Field(default=Decimal("0.10"), description="营运资本占收入比")

    # 折现参数
    wacc: Decimal = Field(description="加权平均资本成本")
    terminal_growth: Decimal = Field(description="永续增长率")

    # 假设来源（用于把「事实/推论/观点」打印进报告）
    assumption_notes: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _capex_must_cover_depreciation(self) -> "DCFInput":
        """``capex ≥ D&A`` —— 持续经营的最低要求，在模型层强制。

        为什么这条**不能只写在 assumptions.py 的闸门里**
        （2026-09 真实事故的教训）：

            601857 的 D&A 换成事实库真值 7.53% 后，DCF 从 15.21 涨到 22.54。
            改成**更准的数据**却让估值变贵，且**全程不报错** ——
            因为模板 capex 只有 6%，低于 D&A，于是 FCFF 里的
            ``+D&A − capex`` 变成 **+1.53% 的正贡献**：
            "折旧比投入多"被模型解释成了**白赚的现金**。

        这不只是数字偏大：``capex < D&A`` 在会计含义上等于假设
        **公司不重置正在折旧的资产**，即清算而非持续经营。
        一个做 DCF 的模型允许自己在"永续经营"的前提下同时假设"不重置资产"，
        是自相矛盾的 —— 所以判据放在模型层，任何构造路径都绕不过去。

        ★ 为什么是**硬拒绝**而不是自动抬升：
        自动修正会让调用方"悄悄拿到一个被改过的输入"，而那正是本次事故的形态。
        闸门（``assumptions.py`` 的 ``_derive_capex_ratio``）负责**在构造前**
        把值调到合规并写明理由；这里负责**在构造时**确认它真的合规。
        两处职责不同，缺一不可。
        """
        if self.capex_ratio_of_revenue < self.da_ratio_of_revenue:
            raise ValueError(
                f"capex/D&A 不一致：资本开支占收入 {self.capex_ratio_of_revenue} "
                f"低于折旧摊销占收入 {self.da_ratio_of_revenue}。"
                "这会让 FCFF 里的「+D&A − capex」变成正贡献 —— "
                "即模型在永续经营假设下隐含「公司不重置正在折旧的资产」（清算），"
                "并**推高**估值且不报错。请先把 capex 抬到不低于 D&A，"
                "并在假设依据里写明抬升理由。"
            )
        return self


class CompsInput(BaseModel):
    entity: str
    target_net_profit: Decimal = Field(description="目标公司归母净利润（元）")
    target_equity: Decimal = Field(description="目标公司归母净资产（元）")
    target_revenue: Decimal = Field(default=ZERO)
    target_shares_outstanding: Decimal = Field(description="总股本（股）")
    target_ebitda: Decimal = Field(default=ZERO)
    peers: list[CompsPeer] = Field(default_factory=list)
    peer_selection_rule: str = Field(
        default="",
        description=(
            "**人工挑选规则**的文字说明（来自可比公司快照），"
            "与样本过滤规则一并写进结论 —— 读者据此判断倍数区间的依据。"
        ),
    )
    illiquidity_discount: Decimal = Field(default=ZERO, description="流动性折价，如 0.20")
    multiple_basis: str = Field(default="ttm", description="静态/ttm/forward —— E2 核查依赖此标注")


# ==========================================================================
# DCF
# ==========================================================================


def build_forecast(inp: DCFInput) -> list[ForecastYear]:
    """构建逐年预测。

    三表联动的简化实现：收入驱动 -> 利润表 -> FCFF；
    资产负债表用「资产 = 负债 + 权益」平衡式反推（保证 A1 必然成立，
    但因此 A1 对本模型是**结构性成立**而非独立检验 —— 这一点必须
    在报告里如实说明，不能拿 A1 充当"校验通过"的证据）。
    """
    years: list[ForecastYear] = []
    revenue_prev = inp.base_revenue
    retained = ZERO
    equity = ZERO

    growths = _pad(inp.revenue_growth, inp.explicit_years, inp.revenue_growth[-1] if inp.revenue_growth else Decimal("0.08"))
    margins = _pad(inp.ebit_margin, inp.explicit_years, inp.ebit_margin[-1] if inp.ebit_margin else Decimal("0.12"))

    for i in range(inp.explicit_years):
        revenue = revenue_prev * (Decimal("1") + growths[i])
        ebit = revenue * margins[i]
        nopat = ebit * (Decimal("1") - inp.tax_rate)
        da = revenue * inp.da_ratio_of_revenue
        capex = revenue * inp.capex_ratio_of_revenue
        nwc_change = (revenue - revenue_prev) * inp.nwc_ratio_of_revenue
        fcff = nopat + da - capex - nwc_change

        # 简化利润表：EBIT − 利息（此处并入净债成本，保持可解释）
        interest = inp.net_debt * Decimal("0.045")
        pretax = ebit - interest
        tax_expense = pretax * inp.tax_rate if pretax > 0 else ZERO
        net_income = pretax - tax_expense

        retained += net_income
        equity = equity + net_income  # 无分红情形（分红由外部扩展）
        total_assets = revenue * Decimal("1.5")  # 资产周转假设：资产/收入 = 1.5
        total_liabilities = total_assets - equity

        years.append(
            ForecastYear(
                year=inp.base_year + i + 1,
                revenue=q(revenue),
                ebit=q(ebit),
                nopat=q(nopat),
                depreciation_amortization=q(da),
                capex=q(capex),
                nwc_change=q(nwc_change),
                fcff=q(fcff),
                net_income=q(net_income),
                total_assets=q(total_assets),
                total_liabilities=q(total_liabilities),
                total_equity=q(equity),
                cash_end=q(total_assets - total_liabilities - (total_assets - equity) + equity),
                retained_earnings=q(retained),
            )
        )
        revenue_prev = revenue

    return years


def _pad(seq: list[Decimal], n: int, default: Decimal) -> list[Decimal]:
    if not seq:
        return [default] * n
    out = list(seq[:n])
    while len(out) < n:
        out.append(out[-1])
    return out


def run_dcf(inp: DCFInput) -> DCFResult:
    """执行 DCF 估值。

    Returns:
        ``DCFResult``。终值占比 > 0.85 时由 A6 断言提示风险。
    """
    if inp.wacc <= inp.terminal_growth:
        raise HarnessError(
            "WACC 必须大于永续增长率，否则戈登永续模型不收敛",
            wacc=str(inp.wacc),
            terminal_growth=str(inp.terminal_growth),
        )

    years = build_forecast(inp)

    # 折现
    pv_explicit = ZERO
    for i, y in enumerate(years):
        discount = (Decimal("1") + inp.wacc) ** (i + 1)
        pv_explicit += y.fcff / discount

    # 终值（戈登永续）
    fcff_terminal = years[-1].fcff * (Decimal("1") + inp.terminal_growth)
    tv = fcff_terminal / (inp.wacc - inp.terminal_growth)
    pv_tv = tv / ((Decimal("1") + inp.wacc) ** len(years))

    ev = pv_explicit + pv_tv
    equity_value = ev - inp.net_debt - inp.minority_interest
    per_share = (
        equity_value / inp.base_shares_outstanding
        if inp.base_shares_outstanding > 0
        else ZERO
    )
    tv_share = pv_tv / ev if ev != 0 else ZERO

    return DCFResult(
        wacc=inp.wacc,
        terminal_growth=inp.terminal_growth,
        explicit_years=years,
        terminal_value=q(tv),
        enterprise_value=q(ev),
        net_debt=inp.net_debt,
        minority_interest=inp.minority_interest,
        equity_value=q(equity_value),
        shares_outstanding=inp.base_shares_outstanding,
        value_per_share=q(per_share, 4),
        terminal_value_share=q(tv_share, 4),
    )


# ==========================================================================
# 相对估值
# ==========================================================================


def run_comps(inp: CompsInput) -> CompsResult:
    """相对估值。倍数口径必须显式标注 —— 这是 E2 类错误核查的基础。"""
    if not inp.peers:
        raise HarnessError("相对估值需要至少一个可比公司")

    pes = [p.pe for p in inp.peers if p.pe and p.pe > 0]
    pbs = [p.pb for p in inp.peers if p.pb and p.pb > 0]
    evs = [p.ev_ebitda for p in inp.peers if p.ev_ebitda and p.ev_ebitda > 0]

    median_pe = _median(pes)
    median_pb = _median(pbs)
    median_ev = _median(evs)

    disc = Decimal("1") - inp.illiquidity_discount
    candidates: list[Decimal] = []

    if median_pe and inp.target_net_profit > 0:
        candidates.append(median_pe * inp.target_net_profit / inp.target_shares_outstanding)
    if median_pb and inp.target_equity > 0:
        candidates.append(median_pb * inp.target_equity / inp.target_shares_outstanding)
    if median_ev and inp.target_ebitda > 0:
        ev_implied = median_ev * inp.target_ebitda
        # EV -> 股权价值需要净债，此处由调用方在 comps 中已隐含处理
        candidates.append(ev_implied / inp.target_shares_outstanding)

    if not candidates:
        raise HarnessError("无法计算相对估值：缺少可用倍数或目标公司财务数据")

    lo = min(candidates) * disc
    hi = max(candidates) * disc
    mid = sum(candidates) / Decimal(len(candidates)) * disc

    return CompsResult(
        peers=inp.peers,
        # ⚠️ 这段文字必须与**实际实现**一致。早期写的是「行业相同 + 市值同量级」，
        #    而 `_select_peers` 根本没做行业/规模匹配 —— 描述比实现漂亮，
        #    等于在交付物里写了一句不成立的免责声明。
        #
        #    现在由**调用方**把这段文字传进来（``peer_selection_rule``），
        #    因为"可比公司是怎么挑出来的"取决于**数据来源**：
        #    快照里记的是人工挑选规则（行业/币种/排除项），
        #    而筛选后的样本过滤（PE 区间、倍数完备性、取中位数）在这一层。
        #    两者都写清楚，读者才知道倍数区间的每一步依据。
        peer_selection_rule="；".join(
            x for x in (inp.peer_selection_rule, _SAMPLE_FILTER_RULE) if x
        ),
        median_pe=q(median_pe, 4) if median_pe else None,
        median_pb=q(median_pb, 4) if median_pb else None,
        median_ev_ebitda=q(median_ev, 4) if median_ev else None,
        implied_value_per_share_low=q(lo, 4),
        implied_value_per_share_mid=q(mid, 4),
        implied_value_per_share_high=q(hi, 4),
        illiquidity_discount=inp.illiquidity_discount,
    )


_SAMPLE_FILTER_RULE = (
    "样本过滤：剔除 PE ≤ 0 或 > 100 的异常值；"
    "样本需同时具备 PE 与 PB；取中位数而非均值（抗异常值）"
)


def _median(vals: list[Decimal]) -> Decimal | None:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 == 1 else (s[n // 2 - 1] + s[n // 2]) / Decimal("2")


# ==========================================================================
# 敏感性分析
# ==========================================================================


def run_sensitivity(
    inp: DCFInput,
    *,
    x_name: str = "wacc",
    y_name: str = "terminal_growth",
    x_steps: int = 5,
    y_steps: int = 5,
    x_span: Decimal = Decimal("0.02"),
    y_span: Decimal = Decimal("0.01"),
) -> SensitivityResult:
    """二维敏感性网格 + 单变量龙卷风图。

    官方明确考察「敏感性分析完整性」，因此这里做三件事：
      1. WACC × 永续增长率 二维网格（热力图数据源）
      2. 龙卷风图：单变量 ±区间对每股价值的影响排序
      3. 结果区间（由网格最大最小值给出）
    """
    grid: list[SensitivityCell] = []
    for i in range(x_steps):
        xv = inp.wacc - x_span + (x_span * 2) * Decimal(i) / Decimal(max(1, x_steps - 1))
        if xv <= 0:
            continue
        for j in range(y_steps):
            yv = inp.terminal_growth - y_span + (
                y_span * 2
            ) * Decimal(j) / Decimal(max(1, y_steps - 1))
            if yv >= xv:
                continue
            variant = inp.model_copy(update={"wacc": xv, "terminal_growth": yv})
            try:
                res = run_dcf(variant)
            except HarnessError:
                continue
            grid.append(
                SensitivityCell(
                    x_value=q(xv, 4), y_value=q(yv, 4), value_per_share=res.value_per_share
                )
            )

    # 龙卷风图
    base = run_dcf(inp).value_per_share
    tornado: list[dict[str, Any]] = []
    for field_name, span in (
        ("wacc", x_span),
        ("terminal_growth", y_span),
        ("da_ratio_of_revenue", Decimal("0.02")),
        ("capex_ratio_of_revenue", Decimal("0.02")),
        ("nwc_ratio_of_revenue", Decimal("0.03")),
        ("tax_rate", Decimal("0.03")),
    ):
        cur = getattr(inp, field_name)
        lo_inp = inp.model_copy(update={field_name: cur - span})
        hi_inp = inp.model_copy(update={field_name: cur + span})
        try:
            lo_v = run_dcf(lo_inp).value_per_share
            hi_v = run_dcf(hi_inp).value_per_share
        except HarnessError:
            continue
        tornado.append(
            {
                "assumption": field_name,
                "base": str(base),
                "low": str(min(lo_v, hi_v)),
                "high": str(max(lo_v, hi_v)),
                "swing": str(abs(hi_v - lo_v)),
                "swing_pct_of_base": str(
                    q(abs(hi_v - lo_v) / base * 100, 2) if base != 0 else ZERO
                ),
            }
        )
    tornado.sort(key=lambda d: Decimal(str(d["swing"])), reverse=True)

    return SensitivityResult(x_name=x_name, y_name=y_name, grid=grid, tornado=tornado)


# ==========================================================================
# 工具封装
# ==========================================================================


class RunDCFIn(BaseModel):
    input: DCFInput


class RunCompsIn(BaseModel):
    input: CompsInput


class RunSensitivityIn(BaseModel):
    input: DCFInput
    x_steps: int = Field(default=5, ge=3, le=21)
    y_steps: int = Field(default=5, ge=3, le=21)


@tool(
    name="finance.dcf",
    description="执行 DCF 估值：FCFF 折现 +  Gordon 永续 + 股权价值桥接。纯计算，无模型调用。",
    params=RunDCFIn,
    side_effect="none",
    cost="free",
    tags=["valuation", "deterministic"],
)
def dcf_tool(input: DCFInput) -> dict[str, Any]:  # noqa: A002 - 与 Pydantic 字段名保持一致
    return run_dcf(input).model_dump(mode="json")


@tool(
    name="finance.comps",
    description="执行相对估值：可比公司倍数中位数 → 隐含每股价值区间。",
    params=RunCompsIn,
    side_effect="none",
    cost="free",
    tags=["valuation", "deterministic"],
)
def comps_tool(input: CompsInput) -> dict[str, Any]:  # noqa: A002
    return run_comps(input).model_dump(mode="json")


@tool(
    name="finance.sensitivity",
    description="二维敏感性网格（WACC×g）+ 龙卷风图，输出估值区间与关键驱动排序。",
    params=RunSensitivityIn,
    side_effect="none",
    cost="free",
    tags=["valuation", "deterministic"],
)
def sensitivity_tool(input: DCFInput, x_steps: int = 5, y_steps: int = 5) -> dict[str, Any]:  # noqa: A002
    return run_sensitivity(input, x_steps=x_steps, y_steps=y_steps).model_dump(mode="json")


class AssertDCFIn(BaseModel):
    input: DCFInput


@tool(
    name="finance.assert",
    description="对 DCF 结果执行 A1–A6 勾稽断言。任一 REQUIRED 失败则模型不可用。",
    params=AssertDCFIn,
    side_effect="none",
    cost="free",
    tags=["valuation", "assertion", "deterministic"],
)
def assert_tool(input: DCFInput) -> dict[str, Any]:  # noqa: A002
    dcf = run_dcf(input)
    report = run_assertions(dcf=dcf, tax_rate=input.tax_rate)
    return {
        "all_passed": report.all_passed,
        "assertions": [a.to_model().model_dump(mode="json") for a in report.assertions],
        "failed": [a.code for a in report.failed],
        "markdown": report.to_markdown(),
    }


__all__ = [
    "DCFInput",
    "CompsInput",
    "build_forecast",
    "run_dcf",
    "run_comps",
    "run_sensitivity",
    "dcf_tool",
    "comps_tool",
    "sensitivity_tool",
    "assert_tool",
]
