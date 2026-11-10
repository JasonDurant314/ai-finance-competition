"""R5 评测的唯一真值源 —— 基准、草稿文本、黄金标注都从这里生成。

**为什么必须只有一处真值**（真实事故）：
评测集最初把同一批数字写在三个地方 —— `validate_against_source.py` 的
`BENCHMARK`、`make_r5_eval_set.py` 的 `TRUTH`、以及草稿正文的字面量。
三处各写一遍，于是出现这样的自相矛盾：

    草稿正文写「同比增长 3.67%」
    黄金标注把这条标成 is_real_error=false（陷阱项，"看起来可疑但正确"）
    而 BENCHMARK 里 2025A 营收 349,165,168.51、2024A 336,788,863.52

系统独立重算后报出「增速不符」—— **系统是对的，评测集错了**。
评测集自身不可信时，后面算出来的精确率/召回率全都没有意义。

因此现在只有一处：**原始披露值（元，字符串，保持披露精度）**，
其余全部由它派生：

* ``yi()``：元 → 亿元（两位小数），草稿正文与展示用
* ``growth()``：由两期值算出增速（两位小数），草稿正文用
* 黄金标注里的 ``correct``：同样从原始值派生

派生量一旦可计算，就绝不手写字面量。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

# ==========================================================================
# 原始披露值（单位：元，保持年报里的披露精度）
# 来源：三家公司的年报原始页面，人工逐页核验（见 docs/VALIDATION_ROUND2.md §3）
# ==========================================================================
RAW: dict[str, dict[str, str]] = {
    "601857": {
        "name": "中国石油",
        "revenue_2025": "2864469000000",
        "revenue_2024": "2937981000000",
        "np_parent_2025": "157302000000",
        "np_parent_2024": "164676000000",
        "np_nonrec_2025": "161671000000",
        "total_assets_2025": "2828017000000",
        "total_assets_2024": "2753007000000",
        "shares_2025": "183020977818",
        # 公开披露：2025 年年度 A 股权益分派实施公告（data/drafts/_public/）
        "dividend_per_share": "0.25",
        "dividend_total": "45755444454.50",
        # 国际财务报告会计准则口径的归母净利润（口径陷阱用）
        "np_parent_ifrs_2025": "183755000000",
    },
    "000722": {
        "name": "湖南发展",
        "revenue_2025": "349165168.51",
        "revenue_2024": "336788863.52",
        "np_parent_2025": "72137817.29",
        "np_parent_2024": "67801953.36",
        "np_nonrec_2025": "58626493.36",
        "np_nonrec_2024": "47556858.13",
        # 资产负债表四项取自**合并**报表（PDF 第 74–75 页），
        # 与「主要会计数据」披露的总资产 3,547,820,309.72 一致。
        # 母公司资产负债表（第 76–77 页）同一批科目是另一套数：
        # 资产总计 3,797,417,361.30、负债合计 364,789,378.77、
        # 所有者权益合计 3,432,627,982.53。2026-10-10 之前这一组误用了母公司口径，
        # 于是 verify_truth 把管道取到的、**正确**的合并数判成"不符"。
        "total_assets_2025": "3547820309.72",
        "total_assets_2024": "3441060747.63",
        "total_liabilities_2025": "357822681.50",
        "total_liabilities_2024": "269554260.75",
        "equity_parent_2025": "3154311963.85",   # 归属于母公司所有者权益合计
        "equity_total_2025": "3189997628.22",    # = 归母 3,154,311,963.85 + 少数股东权益 35,685,664.37
        "ocf_2025": "246931681.41",
        "ocf_2024": "185187778.45",
        "shares_2025": "464158282",
    },
    "002716": {
        "name": "湖南白银",
        "revenue_2025": "12444621019.23",
        "revenue_2024": "8313104638.16",
        "np_parent_2025": "340185327.64",
        "np_parent_2024": "169767192.07",
        "total_assets_2025": "1276655490.19",
        "shares_2025": "2823088646",
    },
}

_YI = Decimal("100000000")


def yi(code: str, key: str, *, places: int = 2) -> str:
    """元 → 亿元（默认两位小数）。草稿正文里的展示值一律由它派生。"""
    value = Decimal(RAW[code][key])
    q = Decimal(1).scaleb(-places)
    return str((value / _YI).quantize(q, rounding=ROUND_HALF_UP))


def growth(code: str, key_now: str, key_prev: str, *, places: int = 2) -> str:
    """由两期原始值算增速（百分比字符串，不带 % 号）。"""
    now = Decimal(RAW[code][key_now])
    prev = Decimal(RAW[code][key_prev])
    if prev == 0:
        raise ValueError(f"{code}.{key_prev} 为 0，无法计算增速")
    q = Decimal(1).scaleb(-places)
    return str(((now - prev) / prev * 100).quantize(q, rounding=ROUND_HALF_UP))


def yi_of(value_yuan: str, *, places: int = 2) -> str:
    """任意元值 → 亿元字符串（用于不是从 RAW 直接取的情形）。"""
    q = Decimal(1).scaleb(-places)
    return str((Decimal(value_yuan) / _YI).quantize(q, rounding=ROUND_HALF_UP))


def ratio_pct(numerator_yuan: str, denominator_yuan: str, *, places: int = 2) -> str:
    """两个元值之比 → 百分比字符串（如分红占净利润的比例）。"""
    num, den = Decimal(numerator_yuan), Decimal(denominator_yuan)
    q = Decimal(1).scaleb(-places)
    return str((num / den * 100).quantize(q, rounding=ROUND_HALF_UP))


def as_benchmark() -> dict[str, list[dict]]:
    """导出给 ``validate_against_source.py`` 用的基准结构。

    只导出**能与事实库指标对应**的项（metric / period / value / page）。
    页面号来自人工定位（docs/VALIDATION_ROUND2.md §3），不是算出来的。
    ``page`` 是 **0 起始的 PDF 页序号**（与 ``pymupdf.open(pdf)[page]`` 的下标一致），
    可直接用 ``fitz`` 复现定位。
    """
    return {
        "000722": [
            {"metric": "revenue", "period": "2025A", "key": "revenue_2025", "page": 7},
            {"metric": "revenue", "period": "2024A", "key": "revenue_2024", "page": 7},
            {"metric": "net_profit_parent", "period": "2025A", "key": "np_parent_2025", "page": 7},
            {"metric": "net_profit_parent", "period": "2024A", "key": "np_parent_2024", "page": 7},
            {"metric": "net_profit_parent_ex_nonrec", "period": "2025A", "key": "np_nonrec_2025", "page": 7},
            {"metric": "net_profit_parent_ex_nonrec", "period": "2024A", "key": "np_nonrec_2024", "page": 7},
            {"metric": "ocf_net", "period": "2025A", "key": "ocf_2025", "page": 7},
            {"metric": "ocf_net", "period": "2024A", "key": "ocf_2024", "page": 7},
            {"metric": "equity_total", "period": "2025A", "key": "equity_total_2025", "page": 74},
            {"metric": "total_liabilities", "period": "2025A", "key": "total_liabilities_2025", "page": 74},
            {"metric": "total_liabilities", "period": "2024A", "key": "total_liabilities_2024", "page": 74},
            {"metric": "total_assets", "period": "2025A", "key": "total_assets_2025", "page": 73},
            {"metric": "total_assets", "period": "2024A", "key": "total_assets_2024", "page": 73},
        ],
        "002716": [
            {"metric": "revenue", "period": "2025A", "key": "revenue_2025", "page": 7},
            {"metric": "revenue", "period": "2024A", "key": "revenue_2024", "page": 7},
            {"metric": "net_profit_parent", "period": "2025A", "key": "np_parent_2025", "page": 7},
            {"metric": "net_profit_parent", "period": "2024A", "key": "np_parent_2024", "page": 7},
        ],
        "601857": [
            {"metric": "revenue", "period": "2025A", "key": "revenue_2025", "page": 7},
            {"metric": "revenue", "period": "2024A", "key": "revenue_2024", "page": 7},
            {"metric": "net_profit_parent", "period": "2025A", "key": "np_parent_2025", "page": 7},
            {"metric": "net_profit_parent", "period": "2024A", "key": "np_parent_2024", "page": 7},
            {"metric": "net_profit_parent_ex_nonrec", "period": "2025A", "key": "np_nonrec_2025", "page": 7},
            {"metric": "total_assets", "period": "2025A", "key": "total_assets_2025", "page": 7},
            {"metric": "total_assets", "period": "2024A", "key": "total_assets_2024", "page": 7},
        ],
    }


def benchmark_rows(code: str) -> list[dict]:
    """把某公司的基准展开成 ``validate_against_source`` 需要的完整行。"""
    rows = []
    for item in as_benchmark().get(code, []):
        rows.append(
            {
                "metric": item["metric"],
                "period": item["period"],
                "value": RAW[code][item["key"]],
                "page": item["page"],
                "raw": RAW[code][item["key"]],
            }
        )
    return rows


__all__ = ["RAW", "yi", "yi_of", "growth", "ratio_pct", "as_benchmark", "benchmark_rows"]
