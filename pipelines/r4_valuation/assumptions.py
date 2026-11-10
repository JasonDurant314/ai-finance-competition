"""阶段 1：从事实库推导估值假设（R4 的唯一输入来源）。

**架构约束（重要）**：R4 **不读取任何原始文档**，只消费 R1 产出的
``facts/facts.json``。这保证了 R1/R4/R5 三处使用的数字必然一致 ——
也就是赛题考察的「结果一致性」。

每个假设必须满足二选一（由 ``Assumption`` 模型强制）：
    * 有 ``source_fact_ids``  —— 来自事实库，属「事实」
    * ``is_judgment=True``    —— 主观判断，属「观点」，必须在报告中标注

官方明确要求「明确区分事实、推论与观点」，这里就是执行点。

缺少必需事实时（例如样本材料不含财务数据），使用**显式标注的模板默认值**
并在报告中列出缺口 —— 绝不静默编造。
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from agent.evidence.models import Assumption, FactSet
from agent.runtime.loop import RunContext
from domain.tools.finance import DCFInput
from pipelines.r1_extract.common import write_json
from pipelines.r4_valuation.comps_data import PROJECT_ROOT

# 模板默认值 —— 仅在事实库缺该项时使用，且**一律标注为观点**
DEFAULT_TEMPLATE: dict[str, Decimal] = {
    "revenue_growth": Decimal("0.08"),
    "ebit_margin": Decimal("0.12"),
    "tax_rate": Decimal("0.25"),
    "da_ratio": Decimal("0.05"),
    "capex_ratio": Decimal("0.06"),
    "nwc_ratio": Decimal("0.10"),
    "wacc": Decimal("0.095"),
    "terminal_growth": Decimal("0.025"),
    "asset_turnover": Decimal("1.5"),
    "interest_rate": Decimal("0.045"),
}

# 一致性闸门的**出处标记**：被闸门调整过取值的假设，其依据文本以此开头。
#
# 为什么需要一个显式标记（而不是让报告去猜）：报告要把每条假设标成
# 「事实 / 推论 / 观点」三选一，而被闸门调整过的那条**三者都不完全成立** ——
# 依据是真事实，取值却是模型内部抬升的结果。没有标记时它会被渲染成
# "🟢 事实"，即把一个被改过的数字当作事实库直读值呈现，正是本项目
# 明确要避免的过度声称。有了标记，报告能把它单列为
# "🟢 事实 + ⚙️ 一致性调整"，闸门就不会变成藏调整的黑箱。
GATE_MARKER = "一致性闸门："


def _wacc_market_reference(rates: dict[str, Any], target_pe: Decimal | None) -> str:
    """把**市场自身隐含的要求回报率**算出来，与模型 WACC 并列呈现。

    为什么需要它（2026-09 实测发现）：模型用 CAPM 算出 Ke = 7.19%，
    而市场给这家公司的盈利收益率（1 ÷ PE）是 **8.14%** —— 市场要求 8.14%，
    模型却用 7.19% 折现，于是 DCF 天然高于市价。

    这不是"算错了"：低利率环境下 1/PE 高企本身可能意味着市场在定价
    风险溢价上行或盈利不可持续。但**读者必须看到这个差额**，
    否则会把"模型比市场贵 60%"当成模型的结论，而不是当成一个待解释的信号。

    这里只做呈现，**不用它反向校准 WACC** —— 用市价校准折现率会让 DCF
    退化成"把市价重新算一遍"，失去独立估计的意义。
    """
    if target_pe is None or target_pe <= 0:
        return ""
    implied = (Decimal("1") / target_pe).quantize(Decimal("0.0001"))
    rf_node = rates.get("risk_free_rate") or {}
    return (
        f"市场参照：目标公司 PE {target_pe}，隐含盈利收益率 1/PE = {implied:.2%} —— "
        "即市场当前对该公司的要求回报率量级。"
        "把它与模型成本率并列看：若模型明显更低，DCF 会系统性高于市价，"
        "这不是 DCF 的结论，而是待解释的差异。"
        "（本项目不用市价反向校准折现率 —— 那会让 DCF 退化成把市价重算一遍，"
        "失去独立估计的意义。）"
        + (f"市场数据截至 {rates.get('as_of')}。" if rf_node else "")
    )


_CAPEX_PLAUSIBLE = (Decimal("0.01"), Decimal("0.35"))

# 目标公司资本开支强度的**已知真实值**，用于在取不到时如实告知量级。
#
# 为什么要把一个具体数字写进代码（看起来像硬编码）：它的用途不是参与计算，
# 而是**让"取不到"这件事带上量级**。读者看到"用了模板值 6%"不会知道这意味着
# 什么；看到"真实约 9.39%，本值低估 → DCF 偏乐观"才知道该往哪个方向打折。
# 它只出现在**依据说明文本**里，永远不进模型输入。
_KNOWN_CAPEX_TRUTH = "601857 真实值约 9.39%（269,089 ÷ 2,864,469，2025 年报「主要财务数据」五年摘要表）"


def _derive_capex_ratio(
    template: Decimal,
    da_ratio_value: Decimal,
    da_ratio_fid: str | None,
    fs: FactSet | None = None,
    *,
    revenue: Decimal | None = None,
    revenue_fid: str | None = None,
    base_period: str = "",
) -> tuple[Decimal, str, str | None]:
    """资本开支占收入比 —— 优先取事实库，取不到才回退模板值 + 一致性闸门。

    为什么可以先取事实（2026-09 第十三轮的进展）
    --------------------------------------------
    第十二轮按单元格坐标重建列结构、第十三轮按**取值落在哪一列**定口径之后，
    601857 的真实资本开支终于可用了：

    * 2025 年报 p7「主要财务数据」五年摘要表里的 ``资本性支出`` 行 ——
      ``269,089``（2025）/ ``275,849``（2024）/ ``275,393`` / ``274,494`` / ``251,323``
      这张表**在 840 条事实的基线里一条都没被抽出来**（`data_start_row` 把
      第一行数据当成表头了）。
    * 分部报告 p178/p179 的合计列 —— 按列定口径后不再与五个分部明细
      （205,094 / 47,772 / 8,216 / 4,987 / 3,020）相撞。

    ★ 但它必须先过一道**可信度闸门**，否则会安静地取到错的值
    ------------------------------------------------------------------
    实测（2026-09，annual30）：同一份**2025 年报**里 ``275,849`` 同时带着
    ``2024A`` 与 ``2025A`` 两个期间入库。275,849 是 **2024 年的**资本开支
    （2024 年报里它以 ``2024A`` 出现，两者对得上），却在 2025 年报里被标成
    2025A —— 那张五年摘要表的列头年份与相邻表复用，期间归属整体错位。

    后果非常具体：``275,849 > 269,089``，于是"同期间取最大值"这个选择键
    **必然取到 2024 年的数**，把它当成 2025 年的资本开支，
    而且**不报错**（数字确实印在原文里、quote 回读也能通过）。

    因此闸门是：**同一期间内若出现多个彼此差异显著的候选，就不取**。
    这与 `_derive_da_ratio` 的"取最大值"是同一类选择的两种处境 ——
    那里候选差异来自"合并 vs 母公司"（单调，最大值必是合并），
    这里差异来自**期间错标**（无单调性，最大值可能是错的那条）。
    分不清就退回模板值，并把原因写进依据。

    返回值：``(比率, 依据说明, 被引用的事实 id)``。
    """
    derived, derived_note, derived_fid = _capex_from_facts(
        fs, revenue=revenue, revenue_fid=revenue_fid, base_period=base_period
    )
    if derived is not None and derived_fid is not None:
        # 一致性闸门仍然要过：事实值低于 D&A 时抬到 D&A
        if da_ratio_value > 0 and derived < da_ratio_value:
            return da_ratio_value, (
                f"{GATE_MARKER}事实值 {derived:.2%} 低于折旧摊销占收入 "
                f"{da_ratio_value:.2%}，抬升至相等。{derived_note}"
                "判据：``D&A − capex`` 是净再投资项，为正是模型在假设"
                "「公司不重置正在折旧的资产」（清算），与永续经营矛盾。"
            ), derived_fid
        return derived, derived_note, derived_fid

    # 取不到事实 → 回退模板值，但**仍过一致性闸门**
    if da_ratio_value <= 0 or template >= da_ratio_value:
        return template, (
            f"资本开支占收入 {template:.2%}，行业中性假设，属观点。"
            + derived_note
        ), None

    return da_ratio_value, (
        f"{GATE_MARKER}抬升至 {da_ratio_value:.2%}（原模板假设 {template:.2%}）。"
        f"判据：自由现金流中 `D&A − capex` 是净再投资项，"
        f"当 capex < D&A 时模型隐含「公司不重置正在折旧的资产」，即清算而非持续经营。"
        f"更隐蔽的是，此时用真实 D&A（{da_ratio_value:.2%}）反而推高 DCF —— "
        f"税盾 +{Decimal('0.25') * da_ratio_value:.2%} 与税后加回 "
        f"+{Decimal('0.75') * da_ratio_value:.2%} 之和超过 capex 扣减。"
        f"闸门把 capex 抬到与 D&A 相等，消除这一项人为的净再投资负值。"
        f"抬到「相等」而非更高，是因为超出部分没有事实依据，只能是观点；"
        f"而「相等」是持续经营的最低要求。"
        + derived_note
    ), da_ratio_fid


def _capex_from_facts(
    fs: FactSet | None,
    *,
    revenue: Decimal | None,
    revenue_fid: str | None,
    base_period: str,
) -> tuple[Decimal | None, str, str | None]:
    """从事实库推导 capex/收入；不值得采信时返回 ``(None, 原因, None)``。

    三条闸门（任一不过即不采信）：

    1. 必须有**同期**收入作分母，且报告期是年度
    2. 比值得落在 ``(1%, 35%)`` —— 实体企业的资本开支强度区间
    3. ★ **同一期间内不得出现多个差异显著的候选**（> 5%）。
       这是防"期间错标"的：实测 275,849（2024 年的数）被标成 2025A，
       与正确的 269,089 同期共存；``275,849 > 269,089``，
       于是"取最大值"必然取到错的那条。**分不清就不取。**
    """
    if fs is None or revenue is None or not revenue or not str(base_period).endswith("A"):
        return None, (
            f"事实库未提供可比收入（或报告期不是年度），资本开支占比只能用模板值。"
            f"{_KNOWN_CAPEX_TRUTH} —— 该值偏低会系统性抬高 DCF，"
            "读者必须把这一条计入不确定性。"
        ), None

    cands = [
        f for f in fs.facts
        if f.metric == "capex"
        and str(f.period) == str(base_period)
        and not f.needs_review
        and f.value > 0
    ]
    if not cands:
        return None, (
            f"事实库没有可采信的 {base_period} 资本开支，只能用模板值。"
            f"{_KNOWN_CAPEX_TRUTH} —— 若此处取不到，多半是被同表冲突守卫标记了。"
            "该值偏低会系统性抬高 DCF。"
        ), None

    # ★ 按**收入锚点**筛候选（2026-09 第十三轮）。
    #
    #   实测（annual32）：``2025A`` 的 capex 候选里混进了**别的公司**的数 ——
    #   湖南白银 196,985,372 元、湖南发展 96,149,596 元。它们与 601857 的
    #   2,864,469,000,000 元收入同期间入库，而"取最大值"会选中 275,849,000,000
    #   （同一份年报里被错标成 2025A 的 **2024 年**资本开支）。
    #
    #   判据用**资本开支占收入的合理区间**：一家实体企业的 capex/收入 不可能
    #   低到 0.007%（196,985,372 ÷ 2,864,469,000,000），也不可能高到几十倍。
    #   上限 35% 是实体企业的宽松上界，下限 1%/3（约 0.33%）专门用来滤掉
    #   "量级完全不属于本主体"的候选 —— 真正的集团资本开支不会只有收入的千分之几。
    #
    #   ⚠️ 这是**筛候选**而不是"改数值"：被滤掉的候选原样留在事实库里，
    #   只是不参与本次推导。滤完为空就退回模板值，不猜。
    floor = abs(revenue) * _CAPEX_PLAUSIBLE[0] / Decimal("3")
    ceiling = abs(revenue) * _CAPEX_PLAUSIBLE[1]
    cands = [f for f in cands if floor <= abs(f.value) <= ceiling]
    if not cands:
        return None, (
            f"{base_period} 的 capex 候选都落在与本主体收入不相称的量级上"
            f"（收入 {revenue:,} 元，合理区间 {floor:,.0f} ~ {ceiling:,.0f} 元）—— "
            "多半是别的主体或别期的读数混进了同一期间。本着「分不清就不取」，"
            f"回退模板值。{_KNOWN_CAPEX_TRUTH}。"
        ), None

    lo = min(abs(c.value) for c in cands)
    hi = max(abs(c.value) for c in cands)
    # 相对分歧：0 = 完全一致，0.025 = 相差 2.5%。
    # ⚠️ 必须用 (hi−lo)/lo 而不是 hi/lo —— 后者是"倍数"（1.025），
    #    直接当百分比格式化会报出 **102.51%** 这种荒唐数字（实测踩过）。
    spread = ((hi - lo) / lo) if lo > 0 else Decimal("0")
    best = max(cands, key=lambda f: (f.confidence, abs(f.value)))
    if spread > Decimal("0.005"):
        # ★ 分歧一律不取（2026-09 第十三轮的最终结论）。
        #
        #   实测（601857 2025 年报 p7 五年摘要表）：同一张表的「资本性支出」行
        #   产出了三个候选，**全部**标成 2025A、全部 ``consolidated``、
        #   置信度全部 0.95 —— 列到期间的映射在那张表上塌掉了，
        #   多个年份的值挤进同一期间：
        #
        #       269,089,000,000  ← 真值（2025）
        #       275,849,000,000  ← 其实是 2024
        #       275,393,000,000  ← 其实是 2023
        #
        #   没有任何信号能把它们分开。此时三条路：
        #
        #   * 取**最大值** → 275,849（2024 年的数），DCF 从 18.1 掉到 **4.98**
        #   * 取**中位数** → 275,393（2023 年的数），DCF 5.04
        #   * **不取**     → 回退模板值 + 一致性闸门，DCF 18.11（保守但可辩护）
        #
        #   前两种都是"安静地算错"，且都会把 DCF 打到相对估值区间之下很远，
        #   读者无法从报告里看出这是个数据问题。第三种会明确记进 `gaps`，
        #   并写清"真实值约 9.39%，本值偏低 → DCF 偏保守"。
        #   **本项目的取向：宁可保守并说清，也不要安静地精确。**
        #
        #   0.5% 这个阈值：候选若真的互相印证，差异只来自口径微调与四舍五入
        #   （实测 000722 的差异在 1% 以内）。超过它就不再是"印证"而是"分歧"。
        return None, (
            f"{base_period} 的资本开支候选彼此分歧（{lo:,} ~ {hi:,}，"
            f"相差 {spread:.2%}），无法判断哪一条才是本期真实值。"
            "成因是列到期间的映射塌陷：同一张五年摘要表把 2025 / 2024 / "
            "2023 三个年份的值都标成了 2025A（该表没有自己的表头，列头年份在"
            "另一张表里且被相邻表复用）。三条候选的置信度、口径、单位全部相同，"
            "没有任何信号能把它们分开。"
            "这里不取任何一个：取最大会选到 2024 年的数（实测把 DCF "
            "从 18.1 打到 4.98），取中位数会选到 2023 年的数 —— 两者都是"
            "安静地算错。回退模板值会明确记进缺口，读者能看到方向。"
            f"{_KNOWN_CAPEX_TRUTH}，本值偏低会使 DCF 偏保守。"
        ), None

    # ⚠️ 这里**不能**再算一次 `best = max(...)`。
    #    实测踩过：分歧分支里已经选好了 `best`，下面又用
    #    `(confidence, |value|)` 重算一遍，把上面的选择**覆盖掉**，
    #    于是依据文字与实际取值不一致，而且不报错。选择逻辑只能有一处。
    ratio = (best.value / revenue).quantize(Decimal("0.0001"))
    if not (_CAPEX_PLAUSIBLE[0] < ratio < _CAPEX_PLAUSIBLE[1]):
        return None, (
            f"推导出的资本开支占比 {ratio:.2%} 超出合理区间 "
            f"(1%, 35%)，判定为量纲错误或科目取错（{base_period} 取到 {best.value}，"
            f"收入 {revenue}）—— 回退模板值。{_KNOWN_CAPEX_TRUTH}。"
        ), None

    return ratio, (
        f"由事实库算出：{base_period} 资本开支 {best.value} 元 ÷ 营业收入 {revenue} 元 "
        f"= {ratio:.2%}。资本开支取自 {best.fact_id}，收入取自 {revenue_fid}。"
        f"该期间共 {len(cands)} 条候选，彼此差异在 0.5% 以内（互相印证）。"
    ), best.fact_id


def _derive_da_ratio(
    fs: FactSet, revenue: Decimal | None, revenue_fid: str | None, *, base_period: str,
) -> tuple[Decimal, str, str | None]:
    """用事实库的**折旧摊销与收入**推导 D&A 占收入比，返回 ``(比率, 说明, fact_id)``。

    ⚠️ 这个指标在事实库里的候选**全部被标成 `consolidated`**，包括母公司口径的那几条：

        601857 合并现金流量表 p176「固定资产折旧、油气资产折耗」
            215,818 | 210,990 | 136,699 | 133,940
            ↑合并2025  ↑合并2024  ↑母公司2025 ↑母公司2024

    实测事实库里这四条**都带 `scope=consolidated`** —— 口径标签在这里不可靠
    （原因见 README 已知限制第 13 条：报表口径跨页错标）。

    因此选择只能靠**结构**：同一期间内**取最大值**。依据是现金流量表的
    列序恒为「合并 → 母公司」，而合并数必然 ≥ 母公司数
    （母公司是合并的一部分）。

    ⚠️ 这是**权宜判据**，不是原则性判据。它在该表结构下成立，
    但若某家公司把母公司列印在左边，就会取错。因此下限有闸门兜底。

    闸门（任一不过即回退模板值并记缺口）：

    1. 比率必须落在 ``(0, 0.30)`` —— D&A 占收入超过 30% 对实体企业不合理
    2. 分母必须是**同一报告期**的收入（否则分子分母跨期）
    """
    t = DEFAULT_TEMPLATE
    if revenue is None or not revenue or not str(base_period).endswith("A"):
        return t["da_ratio"], (
            "事实库未提供可比收入（或报告期不是年度），"
            "折旧摊销占收入采用行业中性假设 5%。属观点。"
        ), None

    cands = [
        f for f in fs.facts
        if f.metric == "depreciation_amortization"
        and str(f.period) == str(base_period)
        and not f.needs_review
        and f.value > 0
    ]
    if not cands:
        return t["da_ratio"], (
            f"事实库没有 {base_period} 的折旧摊销，采用行业中性假设 5%。属观点。"
        ), None

    best = max(cands, key=lambda f: (f.confidence, abs(f.value)))
    ratio = (best.value / revenue).quantize(Decimal("0.0001"))
    if not (Decimal("0") < ratio < Decimal("0.30")):
        return t["da_ratio"], (
            f"推导出的折旧摊销占比 {ratio:.2%} 超出合理区间 (0, 30%)，"
            f"判定为量纲错误或口径取错（{base_period} 取到 {best.value}，"
            f"收入 {revenue}）—— **回退模板值 5% 并列为缺口**。"
        ), None

    return ratio, (
        f"由事实库算出：{base_period} 折旧摊销 {best.value} 元 ÷ 营业收入 {revenue} 元 "
        f"= {ratio:.2%}。折旧摊销取自 {best.fact_id}，收入取自 {revenue_fid}。"
        f"该指标在事实库里的候选全部标成 consolidated（含母公司口径那几条），"
        f"因此同一期间内取最大值 —— 依据是现金流量表列序恒为「合并→母公司」"
        f"且合并数必然不小于母公司数。这是权宜判据（若某公司把母公司列印在左边会取错），"
        f"已用 (0, 30%) 闸门兜底。"
    ), best.fact_id


def _derive_revenue_growth(
    fs: FactSet, revenue: Decimal | None, revenue_fid: str | None, *, doc_period: str,
) -> tuple[Decimal, str, str | None]:
    """用事实库的**两期真实收入**推导增速，过不了闸门就回退模板值。

    返回 ``(增速, 说明, 依据的 fact_id 或 None)``。

    ⚠️ 实测（2026-09）：直接取两期收入会得到 **+5385%** ——
    `2024A revenue` 的 14 条候选里，选择键选中的是 52,221,000,000
    （某张分部/大客户表的收入明细行），而真值是 2,937,981,000,000。
    这类"取错候选"在下游**无法靠选择键避免**（同一期间有 14 条合法候选、
    期间与置信度全相同），只能**靠结果合理性反向识别**。

    因此闸门放在这里：算出的增速必须落在 ``(-50%, +100%)``。
    超出即判定取错候选，回退模板值并把缺口写进 ``gaps`` ——
    用一个荒谬的增速跑 DCF 会得到荒谬的每股价值，且**不报错**。
    """
    t = DEFAULT_TEMPLATE
    if revenue is None or not doc_period or not str(doc_period).endswith("A"):
        return t["revenue_growth"], (
            "事实库未提供可比的两期收入（或报告期不是年度），"
            "采用行业中性假设 8%。"
        ), None

    try:
        year = int(str(doc_period)[:4])
    except ValueError:
        return t["revenue_growth"], "报告期无法解析为年份，采用模板值 8%。", None
    prev_period = f"{year - 1}A"

    prev_facts = [
        f for f in fs.facts
        if f.metric == "revenue" and str(f.period) == prev_period and not f.needs_review
    ]
    if not prev_facts:
        return t["revenue_growth"], (
            f"事实库没有 {prev_period} 的营业收入，无法计算同比增速，"
            "采用行业中性假设 8%。"
        ), None

    prev = max(
        prev_facts,
        key=lambda f: (_period_rank(f.period), f.confidence, _caliber_rank(f),
                       0 if str(f.value).startswith("-") else 1),
    )
    if not prev.value:
        return t["revenue_growth"], f"{prev_period} 收入为 0，无法计算增速，采用模板值 8%。", None

    g = (revenue - prev.value) / abs(prev.value)
    if not (Decimal("-0.5") < g < Decimal("1.0")):
        return t["revenue_growth"], (
            f"用两期收入算出的增速 {g:.2%} 超出合理区间 (-50%, +100%)，"
            f"判定为**取错候选**（{prev_period} 取到 {prev.value}，"
            f"本报告期取到 {revenue}）—— 事实库里同一期间有"
            f"{len(prev_facts)} 条收入候选，选择键无法区分集团口径与分部明细行。"
            "**回退模板值 8% 并列为缺口**：用一个荒谬的增速跑 DCF 会得到荒谬的"
            "每股价值，而且不报错。"
        ), None

    return g.quantize(Decimal("0.0001")), (
        f"由事实库两期收入算出：{prev_period} {prev.value} 元 → "
        f"{doc_period} {revenue} 元，同比 {g:.2%}。"
        f"两期均取自事实库（{prev.fact_id} / {revenue_fid}）。"
        "显式预测期沿用该增速（未做逐年递减），这是简化，已在报告中标注。"
    ), revenue_fid


def _target_pe_from_comps(
    ctx: Any, entity: str, period: str,
) -> Decimal | None:
    """从可比公司快照里取**目标公司自身**的 PE（快照把它作为验证行保留）。

    取不到就返回 ``None`` —— 这只是"参照"，缺了不影响估值主链路，
    因此不做任何兜底猜测。
    """
    try:
        path = (ctx.configs.get("comps") or {}).get("snapshot")
        if path:
            p = Path(path)
            if not p.is_absolute():
                p = PROJECT_ROOT / p
        else:
            from pipelines.r4_valuation.comps_data import default_snapshot_path
            p = default_snapshot_path(entity, period, ctx.configs.get("data_dir"))
        if not p.is_file():
            return None
        data = ctx.guard().read_json(p)
        for row in data.get("rows") or []:
            if str(row.get("code")) == str(entity) and row.get("pe"):
                return Decimal(str(row["pe"]))
    except Exception:  # noqa: BLE001 - 参照值为可选，取不到不影响主链路
        return None
    return None


def _load_market_rates(ctx: Any) -> tuple[dict[str, Any], str]:
    """读取**有出处的**市场利率缓存，返回 ``(数据, 来源说明)``。

    为什么要有这一层（P3）：原先 ``wacc = 9.5%`` 与 ``terminal_growth = 2.5%``
    都是**主观取值**，说明里写着"无风险利率 2.0% + 股权风险溢价 6.0% × β1.1" ——
    但那三个数没有任何出处，读者无法核对，也无法判断是否过时。

    现在改为读 ``data/market/rates_<日期>.json``：里面每个参数都带
    ``value`` / ``source`` / ``basis`` / ``range``。取不到文件时**显式退回**
    模板值并把缺口写进 gaps —— 不让"没取到数据"静默变成"用了旧的主观值"。
    """
    path = (ctx.configs.get("comps") or {}).get("rates_snapshot")
    if path:
        p = Path(path)
    else:
        data_dir = Path(ctx.configs.get("data_dir") or "data")
        if not data_dir.is_absolute():
            data_dir = PROJECT_ROOT / data_dir
        p = data_dir / "market" / "rates_2026-09-17.json"
    if not p.is_file():
        return {}, f"未找到市场利率缓存：{p}"
    try:
        return ctx.guard().read_json(p), ""
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"市场利率缓存不可读：{p}（{exc}）"


def _compute_wacc(rates: dict[str, Any]) -> tuple[Decimal, str, Decimal, Decimal]:
    """按 CAPM 加权平均算 WACC，返回 ``(值, 说明, low, high)``。

    每一步都写出来，**包括哪些是市场数据、哪些是假设** ——
    这是 P3 的全部意义：把"9.5%"这个数字拆成可核对的分项。
    """
    def val(key: str, default: str) -> Decimal:
        node = rates.get(key) or {}
        try:
            return Decimal(str(node.get("value", default)))
        except Exception:  # noqa: BLE001
            return Decimal(default)

    rf = val("risk_free_rate", "0.020")
    erp = val("equity_risk_premium", "0.055")
    beta = val("beta", "1.00")
    kd = val("cost_of_debt", "0.031")
    dc = val("target_debt_to_capital", "0.20")
    tax = DEFAULT_TEMPLATE["tax_rate"]

    ke = rf + beta * erp
    wacc = ke * (Decimal("1") - dc) + kd * (Decimal("1") - tax) * dc

    def src(key: str) -> str:
        node = rates.get(key) or {}
        return str(node.get("source") or "（无来源标注）")

    note = (
        f"按 CAPM 计算：Ke = Rf {rf:.4f} + β {beta} × ERP {erp:.4f} = {ke:.4f}；"
        f"WACC = Ke × (1 − 债务占比 {dc}) + Kd {kd:.4f} × (1 − 税率 {tax}) × 债务占比 = "
        f"{wacc.quantize(Decimal('0.0001'))}。"
        f"分项来源 —— Rf：{src('risk_free_rate')}；"
        f"ERP：{src('equity_risk_premium')}；"
        f"β：{src('beta')}；"
        f"Kd：{src('cost_of_debt')}；"
        f"资本结构：{src('target_debt_to_capital')}。"
        "其中 Rf 为市场数据，其余为假设/推论 —— 报告第五节列出区间与依据。"
    )
    lo = (rf * Decimal("0.8") + beta * Decimal("0.80") * erp * Decimal("0.7"))
    hi = (rf * Decimal("1.2") + beta * Decimal("1.20") * erp * Decimal("1.3"))
    return wacc.quantize(Decimal("0.0001")), note, lo.quantize(Decimal("0.0001")), hi.quantize(Decimal("0.0001"))


def _compute_terminal_growth(rates: dict[str, Any]) -> tuple[Decimal, str]:
    """永续增长率 = 长期通胀目标，且**不得高于**长期国债收益率。

    为什么加上限：永续增长率若高于无风险利率，等价于假设企业
    **永远以高于国债的速度增长** —— 那隐含"这家公司最终会吞掉整个经济体"，
    是永续增长假设最常见的滥用。上限用同期的 10 年期国债收益率作代理。
    """
    def val(key: str, default: str) -> Decimal:
        node = rates.get(key) or {}
        try:
            return Decimal(str(node.get("value", default)))
        except Exception:  # noqa: BLE001
            return Decimal(default)

    g = val("inflation_target", "0.02")
    rf = val("risk_free_rate", "0.020")
    note = (
        f"取长期通胀目标 {g:.2%} 作为永续增长率（名义永续增长不应长期高于通胀中枢）。"
        f"上限校验：{g:.2%} ≤ 10 年期国债收益率 {rf:.2%} —— 通过。"
    )
    if g > rf:
        g = rf
        note = (
            f"长期通胀目标高于 10 年期国债收益率，永续增长率已下调至 {rf:.2%}。"
            "永续增长率高于无风险利率意味着假设企业永远以高于国债的速度增长，"
            "那隐含「这家公司最终会吞掉整个经济体」，是永续增长假设最常见的滥用。"
        )
    return g.quantize(Decimal("0.0001")), note


def scope_to_entity(fs: FactSet, entity: str) -> FactSet:
    """把事实库收窄到**估值对象**那一家公司。

    **为什么必需**（实测事故）：``_fact_value`` 原先写成::

        candidates = [f for f in fs.facts if f.metric == metric and not f.needs_review]
        best = max(candidates, key=lambda f: f.confidence)

    它按**全库最高置信度**取数，**不区分主体**。于是在一次含三家公司材料
    的运行里，给中国石油估值时取到的 `operating_profit` 可能是湖南发展的
    （两家公司的同名指标置信度都是 0.95，谁先被 max 选中取决于顺序）。
    后果是 EBIT 率算成一个荒谬的值（实测 104%），而**没有任何报错** ——
    数字来自事实库、单位正确、quote 也能回读。

    R4 的输入契约是"只消费 R1 的事实库"，但事实库是**跨公司**的；
    "取数"这一步必须自己守住主体边界，不能指望上游替它过滤。
    """
    facts = [f for f in fs.facts if str(f.entity) == entity]
    if not facts:  # 主体名不匹配时退回全量（宁可算错也不空转，但会被缺口暴露）
        return fs
    evidence = fs.evidence
    return fs.model_copy(update={"facts": facts, "evidence": evidence})


def _best_fact(fs: FactSet, metric: str, *, expected: Decimal | None = None) -> Any | None:
    """按**统一的**选择键取该指标的最佳事实。

    选择键（越靠前越优先）：
    **期间最新 → 置信度最高 → 报表口径（合并+归母）→ 非负**。

    ⚠️ 取值（:func:`_fact_value`）与写说明（:func:`_fact_summary`）**必须共用本函数**。
    实测事故：两处各写一套（一处按期间、一处按置信度），结果假设的值来自
    2025A、说明却引用 2023A —— 报告里"值"与"依据"指向不同事实，读者照依据核对必然对不上。

    ⚠️ 并列打破键**不能用 `len(str(value))`**。曾经的写法就是它，
    后果是**系统性偏好量级更大的值**：中国石油的货币资金有两条候选
    （206,162,000,000 与放大 100 倍的 20,616,200,000,000），
    其余键全部打平，于是"字面更长"的错误值胜出，DCF 每股价值被推到 ¥124.41。
    现在的末位键是"非负"，量级不再参与排序。

    ⚠️ **本函数无法可靠地"从多条候选中挑出集团口径的那一条"**，
    这是已知且未解决的局限（试用过两种判据，都被实测否掉）：

    1. **锚点比例带**（占资产总额 10%–80%）：中国石油的货币资金只占资产
       **7.29%**，低于下限，于是整条带内一条候选都没有，判据空转。
    2. **同文档跨页重复出现**（集团数会印在多页、子公司明细只印一页）：
       事实库对同 id 已去重，且多文档事实混在一个 FactSet 里，
       "跨页计数"无法还原真实的重复印刷 —— 实测让「522.21 亿」
       这类被重复抽到的明细行反超了真实营业收入，**比不改更糟**。

    因此本函数的输出必须被下游核对（见 :func:`_fact_summary` 提供的依据链，
    以及 ``tools/verify_truth.py`` 对真值的逐项比对）。
    真正的修法在 R1 侧：让表格抽取能识别"这是合并报表的营业收入行，
    那是分部/大客户表的同名行"，而不是在下游靠启发式猜。

    ``expected``：保留参数位以便将来接入更可靠的判据；当前实现不使用它，
    这样调用方不必改写，也不会因为一个空转的判据得到虚假的安全感。
    """
    candidates = [f for f in fs.facts if f.metric == metric and not f.needs_review]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda f: (
            _period_rank(f.period),
            f.confidence,
            _caliber_rank(f),
            0 if str(f.value).startswith("-") else 1,
        ),
    )


# ⚠️ 十四轮试过、**被实测否决**的一条并列打破键（留档以免重犯）
# ---------------------------------------------------------------------------
# 想法：资产负债表**合计类**指标（总资产/总负债/所有者权益）满足
# "合并 ≥ 母公司"（母公司是集团的一部分），所以其余键打平时**取最大值**
# 就能选到合并口径。
#
# 依据听起来是会计结构、不是量级偏好，但**实测否决**：
#
#     000722 2025A total_assets 有 **17 条候选**，其中有一条
#     ``7,648,491,700.00``（比真值 3,797,417,361.30 还大）。
#     加上这条键之后，选中的正是它 —— **比不改更糟**。
#
# 根因和第十一轮那条守卫是同一个：**候选集合本身不干净**，
# 任何"从候选里挑一条"的启发式都会被垃圾候选俘获。
# 真正的修法在 R1 侧（让口径标注可靠），不在下游加排序键。
#
# 这条与当年被删掉的 `len(str(value))` 是同一类错误：
# 都假定"数值更大 = 更可信"，而实测里错误值经常更大。
#
# 为什么需要单独一组（2026-09 第十四轮，000722 实测）：
#   ```
#   000722 2025A total_assets 的三条候选在期间/置信度/口径上**完全打平**：
#       3,797,417,361.30   p76  ← 真值（合并）
#       3,547,820,309.72   p74  ← 母公司（却被标成 consolidated）
#       3,441,060,747.63   p74
#   ```
#   并列打破键刻意避开了量级（当年 `len(str(value))` 让 100 倍错误值胜出），
#   于是 `max` 取了**先遇到**的那条 —— 母公司数。真值一直在库里，只是没被选中。
#
# 对**合计类**指标，"取最大"不是"偏好量级"，而是**会计恒等式**：
# 母公司是集团的一部分，合并口径的总资产/总负债/所有者权益
# **必然 ≥** 母公司口径的同名数。所以最大值就是集团合并数。
#
# ⚠️ **绝不能推广到其它指标**：货币资金不是"合计"，
#   而实测事故恰恰是它的错误值**更大**（放大 100 倍）。
#   这正是当年那条 `len(str(value))` 键被删除的原因 —— 本条只对
#   结构上单调的合计类指标成立。
def _statement_rank(fact: Any) -> int:
    """报表口径优先：来自合并/母公司报表的事实 > 口径未知的事实。

    为什么需要它：年报里同一个指标名会出现在很多张表里 ——
    合并利润表的「营业收入」、分部报告的「营业收入」、大客户集中的「营业收入」、
    甚至员工人数表的同名行。它们的**期间与置信度完全相同**，
    前几个键分不出来，`max` 就只能按顺序任取一个。

    与 `_caliber_rank` 是同类键（都回答"这条事实是不是报表主体的口径"），
    只是判据不同：`_caliber_rank` 看**哪个**口径（合并还是母公司），
    本函数看**有没有**口径（报表锚定还是明细表）。
    `statement_scope` 由 R1 的「标题-表格矩形交叠」判定，
    是一条有证据的空间关系，不是猜测。
    """
    scope = getattr(getattr(fact, "statement_scope", None), "value", None)
    return 1 if scope in ("consolidated", "parent_only") else 0


def _fact_period(fact_id: str | None, fs: FactSet) -> str | None:
    """从事实 id 反查它的**报告期标签**（``2025A``）。

    与 :func:`_fact_year` 同源，但保留形态后缀（A/H1/Q3）——
    可比公司快照按 ``<目标>_<年份>A.json`` 命名，需要完整标签来定位。
    """
    if not fact_id:
        return None
    for f in fs.facts:
        if f.fact_id == fact_id:
            p = str(f.period or "")
            return p or None
    return None


def _fact_year(fact_id: str | None, fs: FactSet) -> int | None:
    """从事实 id 反查它所属的**报告期年份**（``2025A`` → 2025）。

    为什么需要它：假设表里的"基年"必须与"基年数值"来自同一条事实。
    实测事故：``base_year`` 曾硬编码为 2024，而收入取的是最新期间事实
    （2025 年报 → 2025A），于是报告里"基年 2024 / 基年收入 2,864,469 百万元"
    互相矛盾 —— 2,864,469 百万元是 2025 年的数（2024 年是 2,937,981 百万元）。
    这种错误不会触发任何数值闸门，但会让整份报告的年份锚点偏移一年。

    取不到期间时返回 ``None``，由调用方决定兜底值。
    """
    if not fact_id:
        return None
    for f in fs.facts:
        if f.fact_id == fact_id:
            m = re.match(r"(20\d{2})", str(f.period or ""))
            return int(m.group(1)) if m else None
    return None


def _fact_value(
    fs: FactSet, metric: str, *, expected: Decimal | None = None
) -> tuple[Decimal | None, str | None]:
    """取指标的最佳事实值。跳过 needs_review 的条目。

    ⚠️ 两处坑，都是实测踩出来的：

    1. **必须限定主体** —— 调用方应先 :func:`scope_to_entity`。R4 的输入契约是
       "只消费 R1 的事实库"，而事实库是**跨公司**的；不限定主体时，
       给中国石油估值可能取到湖南发展的同名指标（两家置信度都是 0.95，
       谁被 `max` 选中取决于顺序），且**不报错**。
    2. **必须限定期间** —— 一份年报里同一指标含多年比较数
       （实测 601857 的 `operating_profit` 同时有 2025A / 2024A / 2023A，
       置信度全为 0.95）。只按置信度取会让 DCF 的分子分母来自**不同年度**。

    ``expected``：可选的量级锚点，转交 :func:`_best_fact`。
    """
    best = _best_fact(fs, metric, expected=expected)
    if best is None:
        return None, None
    return best.value, best.fact_id


def _select_consistent_triple(
    fs: FactSet, *, period: str, tol: Decimal = Decimal("0.001"),
) -> tuple[Decimal, Decimal, Decimal, str] | None:
    """在候选里找一个**满足资产负债表恒等式**的（总资产, 总负债, 所有者权益）。

    为什么是"联合选择"而不是"逐指标各自排序"
    ----------------------------------------
    实测（2026-09 第十四轮，601857 2025A）：三个指标各有 27 / 21 / 26 条候选，
    而 `_best_fact` 逐指标独立排序，把所有者权益选成了 **7,300,000 元**
    —— 一条垃圾候选。三者拼起来恒等式残差 1.8 万亿，而**没有任何闸门报警**
    （每个指标单看都"合法"）。

    恒等式 ``总资产 = 总负债 + 所有者权益`` 是**跨指标**的约束，
    所以它天然要求**联合选择**：单独看任何一个指标都发现不了问题。

    为什么"资产最大"这一步是有依据的
    --------------------------------
    实测 601857 的候选里有 **18 组**满足恒等式 —— 它们**都是真实报表**
    （不同口径、不同期），只是大小不同。在**已自洽**的集合里取资产最大，
    依据是：合并（集团）口径的资产**必然 ≥** 其母公司/子公司口径 ——
    母公司是集团的一部分。

    ⚠️ 这与第十四轮被否决的"量级打破并列"**不是一回事**：
    那个是在**未过滤**的候选里偏好大数，于是被垃圾大数（7,648,491,700）
    俘获；这个是在**已经过恒等式硬约束过滤**的候选里偏好大数，
    而每个候选都是真实报表。**先过滤、再排序**是关键区别。

    ``tol``：相对容差（默认 0.1%）—— 报表数字有四舍五入，
    不允许绝对相等。

    找不到返回 ``None``（调用方保持原选择并记缺口）。
    """
    def _vals(metric: str) -> list[Decimal]:
        return [
            abs(f.value) for f in fs.facts
            if f.metric == metric
            and str(f.period) == str(period)
            and not f.needs_review
            and f.value != 0
        ]

    assets, liabs, equities = (
        _vals("total_assets"), _vals("total_liabilities"), _vals("equity_total"),
    )
    if not assets or not liabs or not equities:
        return None

    best: tuple[Decimal, Decimal, Decimal] | None = None
    for a in assets:
        for li in liabs:
            for e in equities:
                if abs(a - (li + e)) <= abs(a) * tol:
                    if best is None or a > best[0]:
                        best = (a, li, e)
    if best is None:
        return None
    a, li, e = best
    return a, li, e, (
        f"总资产 {a:,} = 总负债 {li:,} + 所有者权益 {e:,}"
        f"（在恒等式自洽的组合中取资产最大者 —— 合并口径资产必然 ≥ 母公司口径）"
    )


def _caliber_rank(fact: Any) -> int:
    """口径优先级（越大越优先）：合并 + 归母 > 合并 > 母公司 > 未知。

    R4 估值用的净资产、净利润都应是**合并归母**口径；
    实测事实库里同一指标同时存在 `consolidated/net_parent` 与
    `parent_only` 等多条，不排序就会取到母公司口径。

    ★ `segment_total` 必须与 `consolidated` **同分**（2026-09 第十三轮）：
    分部报告的「合计」列在经济含义上就是集团口径，只是来源不同。
    不给它同分，则后续任何"取集团口径"的选择都会跳过它 ——
    601857 的资本开支就只在分部报告里有，同分才取得出来。
    `segment_detail`（板块口径）刻意**不加分**：它是子公司/板块级读数，
    与集团口径不可比，绝不能顶替集团数。
    """
    score = 0
    scope = getattr(getattr(fact, "statement_scope", None), "value", None)
    basis = getattr(getattr(fact, "profit_basis", None), "value", None)
    if scope in ("consolidated", "segment_total"):
        score += 2
    if basis in ("net_parent", "net_parent_ex_nonrec"):
        score += 1
    return score


def _period_rank(period: str) -> tuple[int, int]:
    """期间的可用性排序键：年份越大越好；同年内年度 > 半年 > 季度。

    取不到年份时给 (0, 0)，排在任何具体年份之后 —— 期间不明的数据
    不该优先于期间明确的数据。
    """
    text = str(period or "")
    m = re.match(r"(20\d{2})", text)
    if not m:
        return (0, 0)
    year = int(m.group(1))
    suffix = text[len(m.group(1)):]
    order = {"A": 3, "FY": 3, "H1": 2, "Q3": 1, "Q1": 1}.get(suffix.strip(), 0)
    return (year, order)


def _fact_summary(fs: FactSet, metric: str) -> str:
    """生成可读的事实来源说明，用作假设的 ``basis``。

    ⚠️ 不能在有事实来源时把 basis 置空 —— ``Assumption.basis`` 的契约要求
    至少 1 个字符（「依据说明」是这份报告可审性的核心）。
    正确做法是**引用事实库里的实际内容**：值、单位、报告期、来源锚点。
    """
    candidates = [f for f in fs.facts if f.metric == metric and not f.needs_review]
    if not candidates:
        return ""
    # ⚠️ **必须与 `_fact_value` 用同一个选择键**。
    #    实测事故：这里原先写 `max(candidates, key=confidence)`，而 `_fact_value`
    #    用「期间 → 置信度 → 口径」排序。于是假设的**值**来自 2025A 的营业利润
    #    （234,579），而自动生成的**文字说明**却引用了 2023A 的那条（255,286）——
    #    报告里"值"与"依据"指向两个不同的事实。数值本身没错，但可审性被破坏了：
    #    读者照 basis 去核对，会对不上。
    best = _best_fact(fs, metric)
    if best is None:
        return ""
    anchor = best.evidence_span_ids[0] if best.evidence_span_ids else "无锚点"
    return (
        f"来自事实库（{anchor}）：{best.metric} = {best.value} {best.unit}"
        f"（报告期 {best.period}，口径 {best.statement_scope.value}"
        f"/{best.profit_basis.value}，置信度 {best.confidence:.2f}）"
    )


def _make_assumption(
    *,
    name: str,
    value: Decimal,
    unit: str,
    fact_id: str | None,
    basis_from_fact: str,
    basis_from_template: str,
    low: Decimal | None = None,
    high: Decimal | None = None,
) -> Assumption:
    """构造一条假设。

    ``basis`` 是必填且非空 —— 它承载「这条假设凭什么」。
    有事实来源时用事实描述，主观判断时用模板说明，**不允许两者都为空**。
    """
    grounded = fact_id is not None
    basis = basis_from_fact if grounded else basis_from_template
    if not basis.strip():
        # 兜底：即使调用方漏传，也不能让契约失败
        basis = (
            f"来自事实库（{fact_id}）" if grounded else "主观假设（未标注具体依据）"
        )
    return Assumption(
        assumption_id=f"asm_{name}",
        name=name,
        value=value,
        unit=unit,
        source_fact_ids=[fact_id] if fact_id else [],
        is_judgment=not grounded,
        basis=basis,
        low=low,
        high=high,
    )


def _entities_in(fs: FactSet) -> dict[str, int]:
    """统计事实库中的主体及其事实条数。"""
    counts: dict[str, int] = {}
    for f in fs.facts:
        counts[f.entity] = counts.get(f.entity, 0) + 1
    return counts


_EXCLUSIVE_METRICS = {
    "shares_outstanding",   # 总股本 —— 只有披露主体自己有
    "eps_basic",            # 每股收益
    "net_profit_parent",    # 归母净利润
    "equity_parent",        # 归母净资产
}
"""**主体专属指标**：子公司/被担保人表里不会出现的指标。

用来回答"这份材料在讲哪家公司"。"""
_CODE_LIKE = re.compile(r"^\d{6}$")


def _select_entity(fs: FactSet) -> tuple[str, list[str]]:
    """选出估值对象，并返回被排除的其他主体。

    ★ 为什么必须有这一步：实测踩过的坑 ——
    把三份不同公司的公告（300339 / 688290 / 430017）一起投入流水线后，
    R4 把三家公司的财务事实**混在一起**算出了一个 `每股价值 0.028 元`
    的荒谬结果，**而且没有任何报错**。

    ★ 第二次踩坑（表格抽取上线之后）：一份材料里现在会出现 **100+ 个主体**
    —— 表格里一行一家子公司。原来"取事实条数最多的主体"立刻不够用了：

       118034（披露主体）              20 条
       JinkoSolar Middle East DMCC     20 条   ← 打平
       青海晶科能源有限公司              18 条

    Python 的 ``max`` 在打平时返回**先遇到**的那个，而顺序取决于事实入库顺序 ——
    也就是说，**再加一条子公司事实就可能把估值对象换成一家子公司**，
    而且不会有任何报错。因此判据必须升级：

    1. **主体专属指标**优先：持有总股本/每股收益/归母净利润的主体就是披露主体
    2. 其次是**证券代码形态**（中文公告的披露主体通常是 6 位代码）
    3. 最后才比事实条数，并且**打平也要有确定性次序**（代码优先 → 名称短优先 → 字典序）
    """
    counts = _entities_in(fs)
    if not counts:
        return "UNKNOWN", []

    exclusive = {f.entity for f in fs.facts if f.metric in _EXCLUSIVE_METRICS}
    code_like = {e for e in counts if _CODE_LIKE.match(e)}
    pool = exclusive or code_like or set(counts)

    def rank(entity: str) -> tuple[int, int, int, str]:
        return (
            counts[entity],
            1 if _CODE_LIKE.match(entity) else 0,
            -len(entity),
            entity,
        )

    primary = max(pool, key=rank)
    excluded = sorted(e for e in counts if e != primary)
    return primary, excluded


MIN_SUBJECT_FACTS = 3
"""认定"这个主体有自身财务数据"的最低事实条数。"""


def subject_facts_note(fs: FactSet, primary: str) -> str:
    """主体自身事实过少时给一句说明；否则返回空串。

    实测踩过的坑：主体归属纠正修好之后，某份材料的主体（118034）名下**一条事实都不剩**
    —— 因为那份公告披露的是**子公司**的财务数据（对外担保公告的附件），
    主体自身的财务数据根本不在材料里。此时"选事实最多的主体"会退而选中
    事实库里另一个代码（300339，只有 1 条），等于**给一家毫不相干的公司估值**，
    而且不报错。

    正确做法是**如实说明"这份材料估不了这个主体"**，而不是换一家来估。
    """
    counts = _entities_in(fs)
    n = counts.get(primary, 0)
    if n >= MIN_SUBJECT_FACTS:
        return ""
    return (
        f"主体 {primary} 自身可用事实仅 {n} 条（少于 {MIN_SUBJECT_FACTS} 条）——"
        f"这份材料很可能只披露了子公司/被担保人的数据，不含披露主体自身的财务数据。"
        f"此时任何估值结论都只能用模板默认值，不具备参考意义。"
    )


def entity_selection_note(fs: FactSet, primary: str) -> str:
    """若主体选择**有歧义**，给一句可写进 trace 的告警；否则返回空串。

    歧义的定义：次优候选与选中者处于同一档（事实条数相同、是否代码形态也相同）。
    这时结论依赖排序细节，必须让读者知道"这里本来可能选成另一家"。
    """
    counts = _entities_in(fs)
    if not counts or primary not in counts:
        return ""
    exclusive = {f.entity for f in fs.facts if f.metric in _EXCLUSIVE_METRICS}
    pool = exclusive or {e for e in counts if _CODE_LIKE.match(e)} or set(counts)

    def key(e: str) -> tuple[int, int]:
        return (counts[e], 1 if _CODE_LIKE.match(e) else 0)

    top = key(primary)
    rivals = sorted(e for e in pool if e != primary and key(e) == top)
    if not rivals:
        return ""
    return (
        f"主体选择存在并列：{primary} 与 {rivals[:3]} 的事实条数相同"
        f"（{top[0]} 条），当前按确定性次序取前者；"
        f"如结论存疑请核对材料中的披露主体"
    )


def run(ctx: RunContext) -> dict[str, Any]:
    import json

    from agent.trace.tracer import file_sha256

    facts_path = ctx.require_facts()
    if ctx.manifest is not None:
        # 记录上游事实库哈希 —— 复现时必须能定位到同一份输入
        ctx.manifest.input_files["facts/facts.json"] = file_sha256(facts_path)

    fs_all = FactSet.model_validate(ctx.guard().read_json(facts_path))

    # 只保留估值对象的事实；跨主体混合会产生无意义的结果
    entity, excluded = _select_entity(fs_all)
    if excluded:
        kept = [f for f in fs_all.facts if f.entity == entity]
        if ctx.tracer:
            # 100+ 个主体时把名单全打印出来只会淹没重点 —— 只列前 8 个
            shown = "、".join(excluded[:8])
            more = f" 等 {len(excluded)} 个" if len(excluded) > 8 else ""
            ctx.tracer.stage_event(
                status="warn",
                detail=(
                    f"事实库含 {len(excluded) + 1} 个主体，仅对 {entity} 估值；"
                    f"已排除：{shown}{more}。跨主体混算会产生无意义结果。"
                ),
            )
            note = entity_selection_note(fs_all, entity)
            if note:
                ctx.tracer.stage_event(
status="warn", detail=note,
                )
        fs = FactSet(
            run_id=fs_all.run_id,
            built_at=fs_all.built_at,
            documents=fs_all.documents,
            facts=kept,
            evidence=fs_all.evidence,
            needs_review_count=sum(1 for f in kept if f.needs_review),
        )
    else:
        fs = fs_all

    gaps: list[str] = []
    assumptions: list[Assumption] = []

    # 主体自身事实是否足够 —— 不够就**明说"这份材料估不了这个主体"**，
    # 而不是退而求其次换一家（实测：会选中另一个代码，给毫不相干的公司估值）
    note = subject_facts_note(fs, entity)
    if note:
        gaps.append("披露主体自身的财务数据")
        if ctx.tracer:
            ctx.tracer.stage_event(status="warn", detail=note)

    # ---------------- 必需财务事实 ----------------
    revenue, revenue_fid = _fact_value(fs, "revenue")
    if revenue is None:
        gaps.append("营业收入（revenue）")
        revenue = Decimal("10000000000")  # 100 亿，仅用于让流程可跑通

    # **基年必须跟着收入事实走**，不能写死。
    #
    # 实测事故：这里原先硬编码 ``base_year=2024``，而 ``revenue`` 取的是
    # `_best_fact` 选出的**最新期间**事实（2025 年报里就是 2025A）。
    # 于是假设表写成"基年 2024、基年收入 2,864,469 百万元" ——
    # 2,864,469 百万元 是 **2025 年**的收入（2024 年是 2,937,981 百万元）。
    # 报告里"基年"与"基年收入"指向两个不同年度，读者按基年去核对必然对不上，
    # 而 DCF 的显式预测期也因此整体前移了一年。
    base_year = _fact_year(revenue_fid, fs) or 2025
    # 报告期标签（2025A）要在这里定死并全程复用：同比增速、可比公司快照、
    # DCF 输入三处都依赖它。只传 base_year（整数）会丢掉 A/H1/Q3 的形态。
    base_period = _fact_period(revenue_fid, fs) or f"{base_year}A"

    shares, shares_fid = _fact_value(fs, "shares_outstanding")
    if shares is None:
        gaps.append("总股本（shares_outstanding）")
        shares = Decimal("1000000000")  # 10 亿股

    net_assets, na_fid = _fact_value(fs, "equity_parent")
    if net_assets is None:
        net_assets, na_fid = _fact_value(fs, "equity_total")
    if net_assets is None:
        gaps.append("归母净资产（equity_parent）")
        net_assets = revenue * Decimal("0.8")

    net_profit, np_fid = _fact_value(fs, "net_profit_parent")
    if net_profit is None:
        gaps.append("归母净利润（net_profit_parent）")
        net_profit = revenue * Decimal("0.08")

    np_ex_nonrec, npe_fid = _fact_value(fs, "net_profit_parent_ex_nonrec")

    # ---------------- ★ 输入事实的资产负债表恒等式检查与**联合选择** ----------------
    #
    # 为什么必须查（2026-09 第十四轮实测）：
    # 事实库里同一个指标有大量候选（601857 的 2025A 有 27 条总资产 / 21 条总负债 /
    # 26 条所有者权益），而 `_best_fact` **无法可靠挑出同一口径的那一组**。
    # 实测它把 `equity_total` 选成了 **7,300,000 元**（七百三十万）——
    # 一条垃圾候选，于是恒等式残差高达 1.8 万亿：
    #
    #     选中  2,827,777,000,000 ≠ 1,028,453,000,000 + 7,300,000
    #     真值  2,827,777,000,000 =  1,028,469,000,000 + 1,799,548,000,000   ✓
    #
    # ★ 关键观察：**恒等式能独立判断对错**，而且它只用会计结构，
    #   不依赖任何量级启发式。实测 601857 的候选里有 **18 组**满足恒等式 ——
    #   它们都是真实的报表（不同口径/不同期），只是大小不同。
    #
    # ★ 所以正确的用法是**联合选择**，而不是逐指标独立排序：
    #   1. 在候选里枚举满足恒等式的三元组（硬约束 —— 不自洽的直接排除）；
    #   2. 在自洽的三元组里取**资产最大**的那组。
    #
    #   第 2 步用"资产最大"是**有依据的**：自洽三元组都是真实报表，
    #   而合并（集团）口径的资产**必然 ≥** 其母公司/子公司口径 ——
    #   母公司是集团的一部分。这与被否决的"量级打破并列"不同：
    #   那个是在**未经过滤**的候选里偏好大数（于是被垃圾大数俘获），
    #   这个是在**已经自洽**的候选里偏好大数（每个候选都是真实报表）。
    #
    # 找不到自洽组合时**保持原选择**，并把不一致记进 `gaps`——
    # 让报告显式说出"这三个输入彼此不自洽"，而不是安静地算一个数。
    _assets, _afid = _best_fact(fs, "total_assets"), None
    _liab = _best_fact(fs, "total_liabilities")
    _assets_v = _assets.value if _assets else None
    _liab_v = _liab.value if _liab else None
    _trip = _select_consistent_triple(fs, period=base_period)
    if _trip is not None:
        _a, _l, _e, _note = _trip
        _assets_v, _liab_v, net_assets = _a, _l, _e
        if ctx.tracer:
            ctx.tracer.stage_event(
status="ok",
                detail=f"资产负债表三项按恒等式联合选定：{_note}",
            )
    elif _assets_v and _liab_v and net_assets:
        _imbalance = abs(_assets_v - (_liab_v + net_assets))
        if _imbalance > abs(_assets_v) * Decimal("0.01"):
            gaps.append(
                f"资产负债表恒等式不成立（总资产 {_assets_v:,} ≠ 总负债 {_liab_v:,} "
                f"+ 净资产 {net_assets:,}，差 {_imbalance:,}）—— "
                "候选里找不到自洽的三元组，说明至少一条取到了错误口径的候选"
            )

    # ---------------- 经营假设 ----------------
    t = DEFAULT_TEMPLATE

    # EBIT（息税前利润）—— **必须用营业利润，不能用净利润代替**。
    #
    # 实测事故：这一项原先写成 ``net_profit / revenue``，于是在真实年报上得到
    # ``ebit_margin = 0.0549``（中国石油 2025：归母净利润 1,573.02 亿 ÷ 收入 28,644.69 亿）。
    # 数字本身不算错，但它**把两个不同的东西当了同一个**：
    # 用净利润充当 EBIT，等于隐含假设"营业成本、费用、税全部为零"，
    # 再乘 (1−税率) 得 NOPAT —— 于是 DCF 每一年的 FCFF 都被系统性高估。
    # 这是"读取了一个错误的科目"，不是单位问题，因此单位换算类闸门抓不住。
    #
    # 正确来源是 ``operating_profit``（营业利润，即会计准则下的 EBIT 近似）。
    # 拿不到时**不猜**：改用模板值并把缺口写进 gaps（宁可标注缺口，
    # 也不要用一个看起来合理的错值把报告填满）。
    # ---------------- EBIT 率 ----------------
    # ⚠️ 这一节必须在 ``t = DEFAULT_TEMPLATE`` **之后**（模板值要用到 t）。
    ebit, ebit_fid = _fact_value(fs, "operating_profit")
    if ebit is None or revenue is None or revenue == 0:
        if ebit is None:
            gaps.append("营业利润（operating_profit，DCF 的 EBIT 来源）")
        ebit_margin_value = t["ebit_margin"]
        ebit_margin_fid = None
        ebit_margin_basis = (
            "事实库未提供营业利润，无法推导 EBIT 率，采用行业中性模板值 12%。"
            "这是观点而非事实。"
            "此处不用归母净利润代替 EBIT —— 那等于隐含假设成本费用税全为零。"
        )
    else:
        ebit_margin_value = (ebit / revenue).quantize(Decimal("0.0001"))
        ebit_margin_fid = ebit_fid
        ebit_margin_basis = (
            _fact_summary(fs, "operating_profit")
            + "；EBIT 率 = 营业利润 ÷ 营业收入（会计准则口径的息税前利润近似）"
        )
    # 合理性闸门：EBIT 率必须落在 (-50%, 80%) 内。
    # 超出即说明取错了科目或量纲 —— 实测 2026-09 年报验证中，
    # 若这里没有闸门，一个 104% 的 EBIT 率会一路传到每股价值。
    if not (Decimal("-0.5") < ebit_margin_value < Decimal("0.8")):
        ebit_margin_value = t["ebit_margin"]
        ebit_margin_fid = None
        ebit_margin_basis = (
            f"推导出的 EBIT 率 {ebit_margin_value} 超出合理区间 (-50%, 80%)，"
            "判定为科目取错或量纲错误，**回退模板值 12% 并列为缺口**。"
        )
        gaps.append("EBIT 率（推导值不合理，已回退模板值）")

    # ---------------- 折旧摊销占收入比（本轮新接入）----------------
    #
    # 为什么必须用它而不是模板值：`capex − 折旧摊销` 是自由现金流里的
    # **净再投资**项。模板用 D&A/收入 = 5%，而 601857 的真实值是 **7.53%**
    # （215,818 ÷ 2,864,469，2025 年报合并现金流量表）。低估 D&A 会让
    # FCFF 被高估 —— 与"低估资本开支"是同一个方向，都在推高 DCF。
    da_ratio_value, da_ratio_note, da_ratio_fid = _derive_da_ratio(
        fs, revenue, revenue_fid, base_period=base_period
    )
    assumptions.append(
        _make_assumption(
            name="da_ratio_of_revenue", value=da_ratio_value, unit="比率（小数）",
            fact_id=da_ratio_fid,
            basis_from_fact=da_ratio_note if da_ratio_fid else "",
            basis_from_template=da_ratio_note,
            low=Decimal("0.02"), high=Decimal("0.20"),
        )
    )
    if da_ratio_fid is None:
        gaps.append("折旧摊销占收入比（未取到可比事实，已回退模板值）")

    # 现金与有息负债都要**锚定在资产总额的量级上**取 —— 这两类指标在年报里
    # 候选极多（合并/母公司、各子公司、现金流量表的增减额），口径与置信度全部打平。
    # 实测事故：现金选到了某家子公司的 2,289（百万元），而合并口径是 206,162。
    assets_anchor, _ = _fact_value(fs, "total_assets")

    cash, cash_fid = _fact_value(fs, "cash_and_equivalents", expected=assets_anchor)
    st_debt, std_fid = _fact_value(fs, "short_term_borrowing", expected=assets_anchor)
    lt_debt, ltd_fid = _fact_value(fs, "long_term_borrowing", expected=assets_anchor)
    bonds, bond_fid = _fact_value(fs, "bonds_payable", expected=assets_anchor)

    if cash is None:
        gaps.append("货币资金（cash_and_equivalents）")
        cash = Decimal("0")
    debt = (st_debt or Decimal("0")) + (lt_debt or Decimal("0")) + (bonds or Decimal("0"))
    if st_debt is None and lt_debt is None and bonds is None:
        gaps.append("有息负债（短期/长期借款、应付债券）")
    net_debt = debt - cash
    net_debt_ids = [i for i in (std_fid, ltd_fid, bond_fid, cash_fid) if i]


    # ---------------- 收入增速（P4）----------------
    #
    # 优先用**事实库里的两期真实收入**算实际增速；但必须先过两道闸门，
    # 因为实测发现：拿两期收入直接算会得到 **+5385%** 这种荒谬结果 ——
    # `2024A revenue` 在事实库里有 14 条候选，其中最大的一条
    # 52,221,000,000 是**分部收入明细行**，而真值是 2,937,981,000,000（相差 56 倍）。
    #
    # 闸门：
    #   ① 两期都必须取到（同一 `_best_fact` 选择键，只是限定期间）
    #   ② 算出的增速必须落在合理区间 (-50%, +100%) —— 超出即判定取错候选，
    #      **回退模板值并写进 gaps**（宁可标注缺口，也不用一个荒谬的增速跑 DCF）
    #
    # ⚠️ 第二道闸门不是形式：它拦下的正是"取到了分部/子公司明细行"这类错误，
    #    而那类错误在事实库里**无法靠选择键避免**（详见
    #    `docs/VALIDATION_ROUND7_COMPS.md` 与第四轮报告里的局限说明）。
    growth_value, growth_note, growth_fid = _derive_revenue_growth(
        fs, revenue, revenue_fid, doc_period=base_period
    )
    if growth_fid is None and "营业收入同比增速" not in gaps:
        gaps.append("营业收入同比增速（两期可比收入，已回退模板值）")

    assumptions.append(
        _make_assumption(
            name="revenue_growth", value=growth_value, unit="%",
            fact_id=growth_fid,
            basis_from_fact=growth_note if growth_fid else "",
            basis_from_template=growth_note,
            low=Decimal("-0.05"), high=Decimal("0.15"),
        )
    )
    assumptions.append(
        _make_assumption(
            name="ebit_margin",
            # 约定：**比率一律以小数存放**（0.0549 = 5.49%），报告层负责显示成百分比。
            # 假设表的 unit 因此写「比率（小数）」而不是「%」——
            # 写「%」会让读者以为 0.0549 是 0.0549%。
            value=ebit_margin_value,
            unit="比率（小数）",
            fact_id=ebit_margin_fid,
            basis_from_fact=ebit_margin_basis,
            # ⚠️ 说明必须**两路都给**：`_make_assumption` 在 basis_from_fact 为空时
            # 会用 basis_from_template 兜底；只给一路会让推导过程的说明被
            # "主观假设（未标注具体依据）"覆盖掉 —— 实测踩过，
            # 于是"为什么回退模板值"这个最该被看到的信息反而消失了。
            basis_from_template=ebit_margin_basis,
            low=Decimal("0.05"), high=Decimal("0.25"),
        )
    )
    assumptions.append(
        _make_assumption(
            name="tax_rate", value=t["tax_rate"], unit="%", fact_id=None,
            basis_from_fact="",
            basis_from_template="按《企业所得税法》一般税率 25%，为法定税率而非实际税负。",
        )
    )
    # ---------------- 资本开支占收入比 + 一致性闸门（本轮新接入）----------------
    #
    # 顺序刻意放在 D&A 之后：闸门需要 D&A 的**推导结果**，而不是模板值。
    # 先 D&A 后 capex，才能保证"抬升"依据的是真实折旧强度。
    capex_ratio_value, capex_ratio_note, capex_ratio_fid = _derive_capex_ratio(
        t["capex_ratio"], da_ratio_value, da_ratio_fid,
        fs, revenue=revenue, revenue_fid=revenue_fid, base_period=base_period,
    )
    assumptions.append(
        _make_assumption(
            name="capex_ratio_of_revenue", value=capex_ratio_value, unit="比率（小数）",
            fact_id=capex_ratio_fid,
            basis_from_fact=capex_ratio_note if capex_ratio_fid else "",
            basis_from_template=capex_ratio_note,
            low=Decimal("0.02"), high=Decimal("0.25"),
        )
    )
    # 分部报告的「合计列」：让本表的取值按**落在哪一列**定口径
    #   ⚠️ capex 现在可能是「分歧即不取」后的模板值 —— 那种情况**也算缺口**。
    #   判据不能只看 fact_id 是否为空：一致性闸门抬升时会挂上 D&A 的 fact_id，
    #   于是"capex 其实没取到事实"这件事会被 fact_id 掩盖。用 note 里的
    #   标记区分（闸门会写 GATE_MARKER，分歧会写"分歧"字样）。
    if capex_ratio_fid is None or "分歧" in capex_ratio_note:
        gaps.append("资本开支占收入比（事实库分歧或取不到，已回退模板值）")

    assumptions.append(
        _make_assumption(
            name="nwc_ratio_of_revenue", value=t["nwc_ratio"], unit="%", fact_id=None,
            basis_from_fact="", basis_from_template="营运资本占收入 10%，行业中性假设。",
        )
    )
    # ---------------- 市场数据（P3）----------------
    rates, rates_err = _load_market_rates(ctx)
    if rates_err:
        gaps.append("市场利率缓存（WACC 的分项来源）")
        if ctx.tracer:
            ctx.tracer.stage_event(status="warn", detail=rates_err)

    wacc_value, wacc_note, wacc_low, wacc_high = _compute_wacc(rates)
    tg_value, tg_note = _compute_terminal_growth(rates)
    if rates:
        rf_node = rates.get("risk_free_rate") or {}
        wacc_note += (
            f"（市场数据截至 {rates.get('as_of')}，来源：{rf_node.get('url') or rf_node.get('source')}）"
        )
    # 市场隐含要求回报率：与模型成本率并列，差额本身就是要呈现的信息
    target_pe = _target_pe_from_comps(ctx, entity, base_period)
    market_ref = _wacc_market_reference(rates, target_pe)
    if market_ref:
        wacc_note += market_ref

    assumptions.append(
        _make_assumption(
            name="wacc", value=wacc_value, unit="%", fact_id=None,
            basis_from_fact="",
            # ⚠️ 说明两路都给：`_make_assumption` 在 basis_from_fact 为空时
            # 会用 basis_from_template 兜底，只给一路会让这段推导被覆盖。
            basis_from_template=wacc_note,
            low=wacc_low, high=wacc_high,
        )
    )
    assumptions.append(
        _make_assumption(
            name="terminal_growth", value=tg_value, unit="%", fact_id=None,
            basis_from_fact="", basis_from_template=tg_note,
            low=Decimal("0.00"), high=Decimal("0.030"),
        )
    )

    target_ebitda = net_profit / Decimal("0.75") + (net_profit * Decimal("0.05"))

    # ---------------- 组装 DCF 输入 ----------------
    dcf_input = DCFInput(
        entity=entity,
        base_year=base_year,
        # 报告期标签要一路传下去：R4 的相对估值要按它定位同期的可比公司快照。
        # 只传 base_year（整数）会丢掉 A/H1/Q3 的形态。
        base_period=base_period,
        base_revenue=revenue,
        base_shares_outstanding=shares,
        net_debt=net_debt,
        minority_interest=Decimal("0"),
        explicit_years=5,
        revenue_growth=[growth_value] * 5,
        # 用上面推导（并过闸门）的 EBIT 率，**不要在这里重算** ——
        # 两处各算一遍正是上一版把"净利润"当"EBIT"却没被发现的原因：
        # 一处改了、另一处没改，模型与假设表就会说两套话。
        ebit_margin=[ebit_margin_value] * 5,
        tax_rate=t["tax_rate"],
        da_ratio_of_revenue=da_ratio_value,
        capex_ratio_of_revenue=capex_ratio_value,
        nwc_ratio_of_revenue=t["nwc_ratio"],
        wacc=wacc_value,
        terminal_growth=tg_value,
        assumption_notes={a.name: a.basis for a in assumptions},
    )

    # 核心输入是否用了模板默认值 —— 这个判断必须一路传到结论上。
    # 实测动机：一次真实运行里主体 118034 的 10 条营业收入事实被"冲突守卫"
    # 全部标记（它们其实是 10 家**子公司**的收入，归属没挂上），
    # R4 于是拿模板默认值 100 亿去估值，得出一份看起来正常的报告与每股价值。
    # 报告末尾确实有"数据缺口声明"，但**结论本身不带这个标记** ——
    # 读者第一眼看到的是数字。
    #
    # ★ 「恒等式不成立」也算核心缺口（2026-09 第十四轮补）。
    #   实测踩过：那条检查加进去之后确实报警了（601857 差 2,133 亿，
    #   占资产 7.5%），但**关键字表里没有它**，于是 `illustrative_only`
    #   仍是 False —— 报告里带着一条"输入彼此不自洽"却照常给出每股价值。
    #   这正是本函数开头那段注释要防的情形，只是这次漏在了关键字上。
    #   恒等式不成立意味着 DCF 的**分母（净资产/净债）本身可疑**，
    #   比"用了模板值"更该触发"仅作方法论演示"。
    _CORE_GAP_KEYS = (
        "披露主体", "revenue", "shares", "equity_parent", "net_profit",
        "恒等式",
    )
    core_gaps = [g for g in gaps if any(k in g for k in _CORE_GAP_KEYS)]
    payload = {
        "entity": entity,
        "excluded_entities": excluded,
        "dcf_input": dcf_input.model_dump(mode="json"),
        "assumptions": [a.model_dump(mode="json") for a in assumptions],
        "gaps": gaps,
        "core_gaps": core_gaps,
        # 只要核心输入有缺口，结论就只是"方法论演示"，不是对这家公司的估值
        "illustrative_only": bool(core_gaps),
        "grounding": {
            "revenue": revenue_fid, "shares": shares_fid, "net_assets": na_fid,
            "net_profit": np_fid, "net_profit_ex_nonrec": npe_fid,
            "net_debt_components": net_debt_ids,
        },
        "targets": {
            "net_profit_parent": str(net_profit),
            "net_profit_parent_ex_nonrec": str(np_ex_nonrec) if np_ex_nonrec else None,
            "equity_parent": str(net_assets),
            "revenue": str(revenue),
            "shares_outstanding": str(shares),
            "ebitda": str(target_ebitda),
            "net_debt": str(net_debt),
        },
    }
    write_json(ctx, "valuation/assumptions.json", payload)

    if gaps and ctx.tracer:
        ctx.tracer.stage_event(
status="warn",
            detail=f"事实库缺少 {len(gaps)} 项必需数据，已使用显式标注的模板值：{gaps}",
        )
    return {
        "entity": entity,
        "assumptions": len(assumptions),
        "gaps": gaps,
        "excluded_entities": excluded,
    }


def _dominant_entity(fs: FactSet) -> str:
    """（保留以兼容旧调用）取事实条数最多的主体。"""
    return _select_entity(fs)[0]


__all__ = ["run"]
