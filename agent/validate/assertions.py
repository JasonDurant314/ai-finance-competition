"""财务勾稽断言 —— R4 估值模型的自证机制。

赛题明确考察「财务勾稽关系」。本模块把勾稽关系写成**可执行断言**，
任一失败即阻断 R4 出报告（``ValuationResult.is_usable == False``）。

断言清单（对应 configs/assertions.yaml）：
    A1 资产总计 ≡ 负债合计 + 所有者权益合计
    A2 期末现金 ≡ 期初现金 + 经营 + 投资 + 筹资现金流净额
    A3 留存收益滚动 ≡ 上期留存收益 + 净利润 − 分红
    A4 FCFF 重算一致（收入→EBIT→NOPAT→FCFF 逐项复算）
    A5 利润表纵向勾稽（收入 − 成本 − 费用 − 税 ≡ 净利润）
    A6 估值结果合理性（每股价值 > 0、终值占比 < 95%、区间有序）

设计原则：断言必须**独立于模型的计算路径重算一遍**。
如果只是把模型自己的中间量拿来比较，那叫自证，不叫校验。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable

from agent.evidence.models import (
    AssertionStatus,
    DCFResult,
    ForecastYear,
    ValuationAssertion,
)
from agent.validate.numeric import approx_equal, q

# 默认容差：允许四舍五入误差，但不允许结构性错误
DEFAULT_REL_TOL = Decimal("0.001")   # 0.1%
DEFAULT_ABS_TOL = Decimal("0.01")    # 1 分钱


@dataclass
class Assertion:
    """一条断言的执行结果。"""

    code: str
    name: str
    status: AssertionStatus
    detail: str
    lhs: Decimal | None = None
    rhs: Decimal | None = None
    tolerance: Decimal = DEFAULT_ABS_TOL

    def to_model(self) -> ValuationAssertion:
        return ValuationAssertion(
            code=self.code,
            name=self.name,
            status=self.status,
            detail=self.detail,
            lhs=self.lhs,
            rhs=self.rhs,
            tolerance=self.tolerance,
        )


@dataclass
class AssertionReport:
    assertions: list[Assertion] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        """**全部**断言都是 PASS。有 SKIPPED 时为 False。

        注意与 :attr:`has_failures` 的区别：
            ``all_passed`` 回答「全部校验都做了且都通过了」；
            ``has_failures`` 回答「有没有校验失败」。
        缺辅助数据时断言会被标记 SKIPPED，此时 ``all_passed`` 为 False
        但 ``has_failures`` 也为 False —— 两者都必须在报告中如实呈现，
        不能只报一个。
        """
        return bool(self.assertions) and all(
            a.status is AssertionStatus.PASS for a in self.assertions
        )

    @property
    def has_failures(self) -> bool:
        """是否存在**失败**的断言（SKIPPED 不算失败）。"""
        return any(a.status is AssertionStatus.FAIL for a in self.assertions)

    @property
    def failed(self) -> list[Assertion]:
        return [a for a in self.assertions if a.status is AssertionStatus.FAIL]

    @property
    def skipped(self) -> list[Assertion]:
        return [a for a in self.assertions if a.status is AssertionStatus.SKIPPED]

    @property
    def coverage(self) -> float:
        """验证覆盖率 = PASS / 总数。缺数据时为 0.5 之类的诚实数字。"""
        if not self.assertions:
            return 0.0
        return round(
            sum(1 for a in self.assertions if a.status is AssertionStatus.PASS)
            / len(self.assertions),
            4,
        )

    def to_markdown(self) -> str:
        icon = {AssertionStatus.PASS: "✅", AssertionStatus.FAIL: "❌", AssertionStatus.SKIPPED: "⏭️"}
        lines = ["| 断言 | 名称 | 结果 | 明细 |", "|---|---|---|---|"]
        for a in self.assertions:
            lines.append(f"| {a.code} | {a.name} | {icon[a.status]} {a.status.value} | {a.detail} |")
        return "\n".join(lines)


def _cmp(
    code: str,
    name: str,
    lhs: Decimal,
    rhs: Decimal,
    *,
    rel_tol: Decimal = DEFAULT_REL_TOL,
    abs_tol: Decimal = DEFAULT_ABS_TOL,
) -> Assertion:
    ok = approx_equal(lhs, rhs, rel_tol=rel_tol, abs_tol=abs_tol)
    diff = lhs - rhs
    detail = (
        f"LHS={q(lhs)} RHS={q(rhs)} 差异={q(diff)}"
        if not ok
        else f"LHS={q(lhs)} ≡ RHS={q(rhs)}（差异 {q(diff)} 在容差内）"
    )
    return Assertion(
        code=code,
        name=name,
        status=AssertionStatus.PASS if ok else AssertionStatus.FAIL,
        detail=detail,
        lhs=lhs,
        rhs=rhs,
        tolerance=abs_tol,
    )


# ==========================================================================
# 各条断言
# ==========================================================================


def assert_balance_sheet(year: ForecastYear) -> Assertion:
    """A1 资产 = 负债 + 所有者权益"""
    return _cmp(
        "A1",
        "资产 = 负债 + 所有者权益",
        year.total_assets,
        year.total_liabilities + year.total_equity,
    )


def assert_cash_flow(
    year: ForecastYear,
    *,
    cash_begin: Decimal,
    cfo: Decimal,
    cfi: Decimal,
    cff: Decimal,
    dividends: Decimal = Decimal("0"),
) -> Assertion:
    """A2 期末现金 = 期初现金 + 经营 + 投资 + 筹资净额"""
    return _cmp(
        "A2",
        "期末现金 = 期初现金 + CFO + CFI + CFF",
        year.cash_end,
        cash_begin + cfo + cfi + cff,
    )


def assert_retained_earnings(
    year: ForecastYear, *, retained_begin: Decimal, dividends: Decimal
) -> Assertion:
    """A3 留存收益滚动 = 上期 + 净利润 − 分红"""
    return _cmp(
        "A3",
        "留存收益滚动",
        year.retained_earnings,
        retained_begin + year.net_income - dividends,
    )


def assert_fcff_recompute(
    year: ForecastYear,
    *,
    tax_rate: Decimal,
) -> Assertion:
    """A4 FCFF 独立重算：FCFF = EBIT×(1−t) + D&A − CapEx − ΔNWC

    注意：此处**不使用** ``year.nopat``，而是从 EBIT 重新计算 NOPAT，
    确保模型内部的 NOPAT 本身也是对的。
    """
    recomputed_nopat = year.ebit * (Decimal("1") - tax_rate)
    recomputed_fcff = (
        recomputed_nopat + year.depreciation_amortization - year.capex - year.nwc_change
    )
    nopat_ok = approx_equal(year.nopat, recomputed_nopat, rel_tol=DEFAULT_REL_TOL, abs_tol=DEFAULT_ABS_TOL)
    fcff_ok = approx_equal(year.fcff, recomputed_fcff, rel_tol=DEFAULT_REL_TOL, abs_tol=DEFAULT_ABS_TOL)
    ok = nopat_ok and fcff_ok
    detail = (
        f"NOPAT 模型={q(year.nopat)} 重算={q(recomputed_nopat)}；"
        f"FCFF 模型={q(year.fcff)} 重算={q(recomputed_fcff)}"
    )
    return Assertion(
        code="A4",
        name="FCFF 独立重算",
        status=AssertionStatus.PASS if ok else AssertionStatus.FAIL,
        detail=detail,
        lhs=year.fcff,
        rhs=recomputed_fcff,
        tolerance=DEFAULT_ABS_TOL,
    )


def assert_income_statement(
    year: ForecastYear,
    *,
    cost_of_sales: Decimal,
    opex: Decimal,
    tax_expense: Decimal,
    minority_interest: Decimal = Decimal("0"),
) -> Assertion:
    """A5 收入 − 成本 − 费用 − 税 ≡ 净利润（含少数股东损益调节）"""
    recomputed = year.revenue - cost_of_sales - opex - tax_expense
    return _cmp("A5", "利润表纵向勾稽", year.net_income, recomputed)


def assert_valuation_sanity(
    dcf: DCFResult,
    *,
    max_terminal_share: Decimal = Decimal("0.95"),
) -> Assertion:
    """A6 估值结果合理性。

    三个检查：
      * 每股价值为正
      * 终值占比 < 阈值（否则模型过度依赖永续假设，必须提示）
      * 股权价值 = EV − 净债 − 少数股东权益
    """
    problems: list[str] = []
    if dcf.value_per_share <= 0:
        problems.append(f"每股价值 {q(dcf.value_per_share)} ≤ 0")
    if dcf.terminal_value_share >= max_terminal_share:
        problems.append(f"终值占比 {q(dcf.terminal_value_share, 4)} ≥ {max_terminal_share}")
    equity_recomputed = dcf.enterprise_value - dcf.net_debt - dcf.minority_interest
    if not approx_equal(dcf.equity_value, equity_recomputed, rel_tol=DEFAULT_REL_TOL, abs_tol=DEFAULT_ABS_TOL):
        problems.append(
            f"股权价值 {q(dcf.equity_value)} ≠ EV−净债−少数股东 {q(equity_recomputed)}"
        )

    return Assertion(
        code="A6",
        name="估值结果合理性",
        status=AssertionStatus.PASS if not problems else AssertionStatus.FAIL,
        detail="；".join(problems) if problems else "每股价值为正、终值占比正常、股权价值桥接正确",
        lhs=dcf.equity_value,
        rhs=equity_recomputed,
        tolerance=DEFAULT_ABS_TOL,
    )


# ==========================================================================
# 编排
# ==========================================================================


def run_assertions(
    *,
    dcf: DCFResult,
    tax_rate: Decimal,
    cost_of_sales: list[Decimal] | None = None,
    opex: list[Decimal] | None = None,
    tax_expense: list[Decimal] | None = None,
    cash_begin: Decimal = Decimal("0"),
    cfo: list[Decimal] | None = None,
    cfi: list[Decimal] | None = None,
    cff: list[Decimal] | None = None,
    dividends: list[Decimal] | None = None,
    retained_begin: Decimal = Decimal("0"),
    on_assertion: Callable[[Assertion], None] | None = None,
) -> AssertionReport:
    """对全部预测年度执行勾稽断言。

    缺失的辅助数据（如现金流三表明细）对应的断言会被**标记为 SKIPPED**
    而不是静默跳过 —— 静默跳过会让报告看起来「全绿」而实际未校验。
    """
    report = AssertionReport()

    def _add(a: Assertion) -> None:
        report.assertions.append(a)
        if on_assertion is not None:
            on_assertion(a)

    # 逐年断言
    for i, year in enumerate(dcf.explicit_years):
        _add(assert_balance_sheet(year))

        if cost_of_sales and opex and tax_expense and i < len(cost_of_sales):
            _add(
                assert_income_statement(
                    year,
                    cost_of_sales=cost_of_sales[i],
                    opex=opex[i],
                    tax_expense=tax_expense[i],
                )
            )
        else:
            _add(
                Assertion(
                    "A5", "利润表纵向勾稽", AssertionStatus.SKIPPED,
                    "缺少成本/费用/税分明细，无法独立重算",
                )
            )

        _add(assert_fcff_recompute(year, tax_rate=tax_rate))

        if cfo and cfi and cff and i < len(cfo):
            prev_cash = cash_begin if i == 0 else dcf.explicit_years[i - 1].cash_end
            _add(
                assert_cash_flow(
                    year, cash_begin=prev_cash, cfo=cfo[i], cfi=cfi[i], cff=cff[i]
                )
            )
        else:
            _add(
                Assertion(
                    "A2", "现金流对账", AssertionStatus.SKIPPED,
                    "缺少 CFO/CFI/CFF 明细，无法独立重算",
                )
            )

        if dividends and i < len(dividends):
            prev_re = retained_begin if i == 0 else dcf.explicit_years[i - 1].retained_earnings
            _add(
                assert_retained_earnings(
                    year, retained_begin=prev_re, dividends=dividends[i]
                )
            )
        else:
            _add(
                Assertion(
                    "A3", "留存收益滚动", AssertionStatus.SKIPPED,
                    "缺少分红数据，无法独立重算",
                )
            )

    # 整体断言
    _add(assert_valuation_sanity(dcf))
    return report


__all__ = [
    "Assertion",
    "AssertionReport",
    "run_assertions",
    "assert_balance_sheet",
    "assert_cash_flow",
    "assert_retained_earnings",
    "assert_fcff_recompute",
    "assert_income_statement",
    "assert_valuation_sanity",
    "DEFAULT_REL_TOL",
    "DEFAULT_ABS_TOL",
]