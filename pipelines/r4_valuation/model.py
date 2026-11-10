"""阶段 2：执行估值建模（DCF + 相对估值 + 敏感性 + 勾稽断言）。

本阶段**纯确定性计算，不调用任何 LLM**。因此：
    * 结果完全可复现（同样输入必然同样输出）
    * 可以通过断言独立校验
    * 现场演示可以交互式拖动滑杆实时重算

**硬闸门**：任一条 REQUIRED 勾稽断言失败，``ValuationResult.is_usable``
为 False，禁止对外输出估值结论。宁可说「模型不通过校验」，
也不能把没通过勾稽的数字当结论交出去。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from agent.evidence.models import (
    Assumption,
    CompsPeer,
    ValuationAssertion,
    ValuationResult,
)
from agent.runtime.loop import RunContext
from agent.validate.assertions import run_assertions
from domain.tools.finance import (
    CompsInput,
    DCFInput,
    run_comps,
    run_dcf,
    run_sensitivity,
)
from pipelines.r1_extract.common import read_json, write_json
from pipelines.r4_valuation.comps_data import PROJECT_ROOT

# ---------------------------------------------------------------------------
# 可比公司来源
#
# **不再使用手写占位样本**。原先是代码里五条 `{"name": "可比公司A", "pe": "12.5"}` ——
# 连名字都是编的，只能演示"筛选 → 中位数 → 隐含价值区间"这条链路。
#
# 现在读 `data/comps/<target>_<period>.json` 快照，内容由
# `tools/fetch_comps.py` 从公开接口取得（真实行情 + 年报法定披露财务数据），
# 并与人工逐页核对真值比对通过后才落盘。快照里记录了数据来源、行情时间戳
# 与倍数算式，报告会一并呈现 —— 评审核对时不必相信我们的转述。
#
# 快照缺失时 **显式报错**，不退回占位样本：否则报告会看起来正常、
# 而里面是编的数 —— 那正是本次要消除的问题。
# ---------------------------------------------------------------------------
COMPS_SNAPSHOT_ENV = "DSH_COMPS_SNAPSHOT"


def _load_comps_rows(
    ctx: RunContext, target: str, period: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """读取可比公司快照，返回 ``(rows, meta)``。

    ``meta`` 带来源 / 时点 / 筛选规则，供报告标注 —— 这些信息必须一路传到
    结论上，否则读者无法判断"这个倍数区间是从哪来的"。
    """
    from pipelines.r4_valuation.comps_data import (
        CompsDataError, default_snapshot_path, load_snapshot,
    )

    override = (ctx.configs.get("comps") or {}).get("snapshot")
    data_dir = ctx.configs.get("data_dir")
    path = (Path(override) if override
            else default_snapshot_path(target, period, data_dir))
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    try:
        data = load_snapshot(path, guard=ctx.guard())
    except CompsDataError as exc:
        raise CompsDataError(
            f"{exc}\n\n"
            "⚠️ 本项目**刻意不提供内置占位样本**：宁可让流水线失败，"
            "也不让报告里出现看起来正常、实际是编造的倍数。"
        ) from exc

    meta = {
        "snapshot_path": str(path.relative_to(PROJECT_ROOT))
        if path.is_relative_to(PROJECT_ROOT) else str(path),
        "as_of": data.get("as_of"),
        "fetched_at": data.get("fetched_at"),
        "sources": data.get("sources") or {},
        "peer_selection_rule": data.get("peer_selection_rule") or "",
        "multiple_formula": data.get("multiple_formula") or {},
        # 这两条原本漏传了：报告里那两行因此永远不显示。
        # 它们是**数据源可信度的依据**（自算而非采信接口字段的理由、
        # 以及抓取时的真值比对结果），必须一路传到报告上。
        "why_self_computed": data.get("why_self_computed") or "",
        "verification": data.get("verification") or "",
    }
    return list(data.get("rows") or []), meta


def run(ctx: RunContext) -> dict[str, Any]:
    payload = read_json(ctx, "valuation/assumptions.json")
    dcf_input = DCFInput.model_validate(payload["dcf_input"])
    assumptions = [Assumption.model_validate(a) for a in payload["assumptions"]]
    targets = payload["targets"]

    # ---------------- DCF ----------------
    dcf = run_dcf(dcf_input)

    # ---------------- 相对估值 ----------------
    rows, comps_meta = _load_comps_rows(ctx, dcf_input.entity, dcf_input.base_period)
    peers = _select_peers(rows, exclude=dcf_input.entity)
    comps_input = CompsInput(
        entity=dcf_input.entity,
        target_net_profit=Decimal(targets["net_profit_parent"]),
        target_equity=Decimal(targets["equity_parent"]),
        target_revenue=Decimal(targets["revenue"]),
        target_shares_outstanding=Decimal(targets["shares_outstanding"]),
        target_ebitda=Decimal(targets["ebitda"]),
        peers=peers,
        peer_selection_rule=str(comps_meta.get("peer_selection_rule") or ""),
        illiquidity_discount=Decimal("0.10"),
        multiple_basis="ttm",
    )
    comps = run_comps(comps_input)

    # ---------------- 敏感性 ----------------
    sensitivity = run_sensitivity(dcf_input, x_steps=7, y_steps=7)

    # ---------------- 勾稽断言 ----------------
    report = run_assertions(dcf=dcf, tax_rate=dcf_input.tax_rate)
    assertions: list[ValuationAssertion] = [a.to_model() for a in report.assertions]
    for a in report.assertions:
        if ctx.tracer:
            # 传**三态**而不是 bool：skipped（缺数据、本次没校验）与 fail
            # （校验过且不通过）在审计报告里含义相反，压成 bool 会让
            # 30 条「未校验」显示成「失败」。
            ctx.tracer.assertion(
                code=a.code, name=a.name, status=a.status.value, detail=a.detail
            )

    # ---------------- 估值区间合成 ----------------
    #
    # ⚠️ 两个方法给的**东西不一样**，必须把这个不对称讲清楚，否则区间会被误读：
    #   * DCF  —— **点估计**（一组假设推出一个每股价值）
    #   * comps —— **区间**（可比倍数本身有低/中/高）
    # 因此区间取"两个方法结果的并集极值"：下限 = min(DCF, comps 下限)，
    # 上限 = max(DCF, comps 上限)。这是**保守的口径** ——
    # 它刻意不把两者平均掉，因为平均会掩盖模型分歧。
    # 代价是：区间比任一单独方法都宽（实测 DCF 13.39 / comps 10.38–12.64
    # → 并集 10.38–13.39，宽于 comps 区间本身）。这一点写在 weighting_note 里。
    #
    # 中值则取两个方法**中心值**的等权平均，不取并集的中点 ——
    # 并集中点会被最极端的那一端拉偏，与"等权"的说法自相矛盾。
    lo = min(dcf.value_per_share, comps.implied_value_per_share_low)
    hi = max(dcf.value_per_share, comps.implied_value_per_share_high)
    mid = (dcf.value_per_share + (comps.implied_value_per_share_mid or dcf.value_per_share)) / Decimal("2")
    # 中值必须落在区间内，否则读报告的人会看到自相矛盾的三个数。
    # 这是**可判定的**不变量（不是"看起来合理"），因此直接断言。
    assert lo <= mid <= hi, (
        f"估值区间自相矛盾：low={lo} mid={mid} high={hi}"
        f"（DCF {dcf.value_per_share}，comps "
        f"{comps.implied_value_per_share_low}~{comps.implied_value_per_share_high}）"
    )

    result = ValuationResult(
        run_id=ctx.run_id,
        entity=dcf_input.entity,
        as_of=ctx.build_stamp().date(),
        built_at=ctx.build_stamp(),
        assumptions=assumptions,
        dcf=dcf,
        comps=comps,
        sensitivity=sensitivity,
        comps_provenance={
            **comps_meta,
            "peer_count_after_filter": len(peers),
            "peer_names": [p.name for p in peers],
        },
        value_range_low=lo,
        value_range_high=hi,
        value_range_mid=mid,
        weighting_note=(
            "两个方法给出的结果性质不同：DCF 是点估计（一组假设推出一个每股价值），"
            "相对估值是区间（可比倍数本身有低/中/高）。"
            f"因此区间取两者的并集极值：下限 = min(DCF {dcf.value_per_share}, "
            f"相对估值下限 {comps.implied_value_per_share_low})，"
            f"上限 = max(DCF {dcf.value_per_share}, "
            f"相对估值上限 {comps.implied_value_per_share_high})。"
            "这是偏保守的口径 —— 不把两者平均掉，因为平均会掩盖模型分歧；"
            "代价是区间宽于任一单独方法，但宽不等于分歧被放大。"
            "中值取两个方法中心值的等权平均，未赋主观权重。"
        ),
        risk_factors=[
            "WACC 假设敏感：±1pp 变动导致每股价值变动见敏感性龙卷风图",
            f"终值占比 {dcf.terminal_value_share:.1%}，"
            + ("超过 85%，估值高度依赖永续假设，需谨慎解读" if dcf.terminal_value_share > Decimal("0.85") else "处于常规区间"),
            "相对估值的可比公司为**人工按行业规则挑选**（规则见报告第五节），"
            "且仅覆盖 A 股同业 —— 未纳入港股/美股可比公司；"
            "样本量为个位数，中位数对单个样本的变动较敏感",
            "若事实库存在 needs_review 项，相关科目可能影响估值基础",
        ]
        + (
            [
                "⚠️ **本次结论为方法论演示**：核心输入 "
                + "、".join(payload.get("core_gaps") or [])
                + " 未从材料中提取到，估值使用了显式标注的模板默认值 —— "
                "该数字不代表对这家公司的估值结论。"
            ]
            if payload.get("illustrative_only")
            else []
        ),
        applicability=(
            "本估值基于受控材料离线计算，适用于投研分析演示与方法论验证，"
            "不构成任何投资建议。参数假设已在假设表中逐条标注事实/推论/观点属性。"
        ),
        assertions=assertions,
    )

    write_json(ctx, "valuation/valuation.json", result.model_dump(mode="json"))

    summary = {
        "entity": result.entity,
        "value_per_share": str(dcf.value_per_share),
        "range": [str(lo), str(hi)],
        "terminal_value_share": str(dcf.terminal_value_share),
        "assertions_total": len(assertions),
        "assertions_failed": [a.code for a in assertions if a.status.value == "fail"],
        "is_usable": result.is_usable,
    }
    write_json(ctx, "valuation/model_summary.json", summary)

    if not result.is_usable:
        # 断言失败是**严重问题**，必须让调度器看见（但不必中断整条流水线）
        if ctx.tracer:
            ctx.tracer.error(
                where="r4.model",
                message=f"勾稽断言失败：{summary['assertions_failed']}，估值结论不可用",
            )

    return summary


def _select_peers(rows: list[dict[str, Any]], *, exclude: str = "") -> list[CompsPeer]:
    """从快照行筛选可比公司。

    执行的规则（与 ``run_comps`` 输出的 ``peer_selection_rule`` 保持一致）：
        1. **剔除目标公司自身** —— 它出现在快照里只是为了验证数据源
           （见 ``tools/fetch_comps.py`` 的验证闸门），**不是**可比公司
        2. 剔除 PE <= 0 或 PE > 100 的异常值
           （实测有效：荣盛石化 FY2025 PE = 151.3，正是这条规则把它挡在外面）
        3. 剔除缺 PE 或 PB 的样本（单一倍数不足以交叉验证）
        4. 使用中位数而非均值，抗异常值

    ⚠️ 早先这里的注释声称"行业相同 + 市值同量级"，但代码只做 PE 区间与
    倍数完备性检查，**没有**做行业或市值匹配 —— 描述比实现漂亮，
    等于写了一句不成立的免责声明。现已改为如实描述。

    行业匹配改由**数据层**保证：快照的 ``peer_selection_rule`` 记录了
    可比公司是**按什么规则人工挑出来的**（见 ``tools/fetch_comps.py``），
    这段规则会原样出现在报告里，评审核对时不必相信代码注释。

    ⚠️ 曾经用 `market_cap = pe * 目标公司净利润` 推算可比公司市值 ——
    那算出的是"目标公司按这个 PE 值多少钱"，**不是可比公司的市值**。
    现在市值直接取快照里的真实值（总股本 × 现价），不再反推。
    """
    peers: list[CompsPeer] = []
    seen: set[str] = set()
    for r in rows:
        code = str(r.get("code") or "")
        if exclude and code == exclude:
            continue                      # 目标公司自身不入可比集
        if code and code in seen:
            continue
        raw_pe = r.get("pe")
        if raw_pe in (None, ""):
            continue
        try:
            pe = Decimal(str(raw_pe))
        except (InvalidOperation, ValueError):
            continue
        if pe <= 0 or pe > 100:
            continue

        pb = None
        if r.get("pb") not in (None, ""):
            try:
                pb = Decimal(str(r["pb"]))
            except (InvalidOperation, ValueError):
                pb = None
        if pb is None:      # 缺多数倍数 → 样本不可用
            continue

        cap = None
        if r.get("market_cap") not in (None, ""):
            try:
                cap = Decimal(str(r["market_cap"])).quantize(Decimal("1"))
            except (InvalidOperation, ValueError):
                cap = None

        seen.add(code)
        peers.append(
            CompsPeer(
                name=str(r.get("name") or code),
                ticker=f"{code}.{r.get('market', '')}".strip("."),
                market_cap=cap,
                pe=pe,
                pb=pb,
                # 快照不提供 EV/EBITDA —— 它需要「市值 + 有息负债 − 现金」，
                # 而现金与有息负债**没有可靠的结构化公开来源**（实测东方财富
                # 财报接口不返回这两项）。宁可留空，也不用编的数充数：
                # `run_comps` 会对缺失的倍数降级处理。
                ev_ebitda=None,
                multiple_basis=str(r.get("period") or ""),
            )
        )
    return peers


__all__ = ["run", "PROJECT_ROOT"]
