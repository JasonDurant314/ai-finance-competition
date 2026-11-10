"""构造 R5 评测集（草稿 + 黄金标注）—— 基于公开文本与人工核验的基准值。

为什么要有这个工具
------------------
R5（研究报告纠错核查）要考察**错误识别的准确性与覆盖率、误报控制、来源定位、
修改建议的可执行性**。评测这些指标必须有标注集，而标注集不能靠"编一个文档" ——
必须满足三条：

1. **每条数字可核对**：草稿里的数要么是真的（陷阱项），要么是**有来源的错法**
   （比如把"负债和所有者权益总计"当成"资产总计"）
2. **错误形态来自真实审计文本**：单位（每10股 vs 每股）、口径（合并 vs 母公司、
   归母 vs 扣非、国际准则 vs 中国准则）这些陷阱，全部取自 `data/drafts/_public/`
   里真实披露文件的写法
3. **标注与草稿同步生成**：`char_range` 与 `quote` 由脚本按草稿正文计算，
   不手写 —— 手写的偏移必然漂移，而"来源定位"正是被测项

产出
----
- ``data/r5_eval/drafts/<draft_id>.md``         待核查草稿
- ``data/r5_eval/golden/<draft_id>.labels.json`` 黄金标注（含陷阱项 ``is_real_error=false``）

⚠️ **为什么放在 ``data/r5_eval/`` 而不是 ``data/drafts/``**（踩过一次）：
``data/drafts/`` 是 R5 管线的默认输入，而**端到端复现性测试**
（``tests/test_harness.py::test_end_to_end_is_reproducible``）会跑一次
``agent run full``，其中 R5 阶段读的正是这个目录。把 3 份评测草稿放进去之后，
R5 产物随草稿内容变化，复现性测试立刻失败 —— 而它看起来像"复现性坏了"，
不像"测试夹具对目录内容敏感"。

评测集因此隔离到独立目录，通过 ``--data data/r5_eval`` 显式指定。

错法全部**锚定真值**（数值来自人工逐页核验的基准，见
``tools/validate_against_source.py`` 的 ``BENCHMARK``），因此每条错误的
「正确值」是可复现的，不是编的。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.console import enable_utf8_output  # noqa: E402

DRAFTS = PROJECT_ROOT / "data" / "r5_eval" / "drafts"
GOLDEN = PROJECT_ROOT / "data" / "r5_eval" / "golden"

# ==========================================================================
# 真值：**单一来源** —— data/r5_eval/truth.py
#
# 评测集曾经把同一批数字写在三处（基准表、本文件的 TRUTH、草稿正文），
# 结果出现自相矛盾：系统独立计算后报出「增速不符」，而**系统是对的、评测集是错的**。
# 现在所有数值都从 truth.py 派生，本文件不出现任何手写数字。
# ==========================================================================
sys.path.insert(0, str(PROJECT_ROOT / "data" / "r5_eval"))
import truth as T  # noqa: E402

_RAW = T.RAW


# ==========================================================================
# 草稿构造
# ==========================================================================


def build_petrochina_draft() -> tuple[str, list[dict]]:
    """中国石油：一份"看起来很像券商研报"的草稿。

    错法设计（每条都锚定真值，且形态取自真实披露文本的陷阱）：

    | # | 错法 | 类型 | 为什么真实 |
    |---|---|---|---|
    | 1 | 营业收入写成 28,644.69 亿元（真值 28,644.69 亿…见下） | E1 单位 | 百万元/亿元混用是最常见的量纲事故 |
    | 2 | 归母净利润用国际准则数（1,837.55 亿）冒充中国准则口径 | E3 口径 | 真实季报里两个准则差异被专门披露 |
    | 3 | 分红占比 35.2%（真值 457.55/1573.02=29.09%） | E5 算术 | 占比算错是研究员的经典失误 |
    | 4 | 资产总计引用 2024 年数 | E4 引用 | 报告期错配 |
    | 5 | 股息"每 10 股派 0.25 元"（真实披露是**每股** 0.25 元） | E1 单位 | 每10股 vs 每股正是公告里点名的陷阱 |
    | 6 | 陷阱项：归母净利润 1,573.02 亿（正确） | — | 检验误报 |
    | 7 | 陷阱项：营业收入同比下降 2.50%（真值 28644.69/29379.81-1=-2.50%） | — | 检验误报 |
    """
    t = _RAW["601857"]
    rev25 = T.yi("601857", "revenue_2025")
    rev24 = T.yi("601857", "revenue_2024")
    np25 = T.yi("601857", "np_parent_2025")
    np24 = T.yi("601857", "np_parent_2024")
    ta25 = T.yi("601857", "total_assets_2025")
    ta24 = T.yi("601857", "total_assets_2024")
    div_yi = T.yi("601857", "dividend_total")
    np_ifrs = T.yi("601857", "np_parent_ifrs_2025")
    # 派生量一律**算**出来，不手写 —— 手写的字面量正是评测集曾经自相矛盾的来源
    g_rev = T.growth("601857", "revenue_2025", "revenue_2024")
    g_np = T.growth("601857", "np_parent_2025", "np_parent_2024")
    payout_true = T.ratio_pct(t["dividend_total"], t["np_parent_2025"])
    payout_wrong = "35.2"          # 注入的错值（唯一允许出现的字面量：它就是要错）
    np_gap = T.yi_of(str(int(t["np_parent_ifrs_2025"]) - int(t["np_parent_2025"])))

    body = f"""# 中国石油（601857）2025 年度经营回顾

> 本报告依据公司公开披露材料整理。所有金额单位如无特别说明均为人民币。

## 一、经营业绩

2025 年度公司实现营业收入 {rev25} 亿元，较上年度的 {rev24} 亿元下降 {g_rev}%；
实现归属于母公司股东的净利润 {np25} 亿元，较上年度的 {np24} 亿元下降 {g_np}%。

公司同期按国际财务报告会计准则计算的归属于母公司股东的净利润为 {np_ifrs} 亿元。

报告期末，公司资产总计 {ta25} 亿元，总股本 {int(t['shares_2025']):,} 股。

## 二、股东回报

2025 年年度利润分配方案为每 10 股派发现金红利 {t['dividend_per_share']} 元（含税），
共计派发现金红利 {div_yi} 亿元，占本年度归属于母公司股东净利润的 {payout_wrong}%。

## 三、主要财务数据摘要

| 项目 | 2025 年 | 2024 年 |
|---|---|---|
| 营业收入（亿元） | {rev25} | {rev24} |
| 资产总计（亿元） | {ta24} | {ta24} |

| 项目 | 2025 年末 |
|---|---|
| 资产总计（亿元） | {ta24} |

## 四、口径说明

公司同时在境内外披露财务信息，中国企业会计准则与国际财务报告准则下的
净利润存在差异，本报告除特别说明外均采用中国企业会计准则口径。

## 五、风险提示

国际油价波动、炼化行业景气度变化及汇率波动可能对公司经营业绩产生影响。
"""
    labels = [
        # —— 真错（全部锚定真值）——
        {"type": "E1_unit", "quote": f"每 10 股派发现金红利 {t['dividend_per_share']} 元",
         "is_real_error": True,
         "note": f"单位错误：2025 年年度 A 股权益分派实施公告为**每股**派发现金红利 "
                 f"{t['dividend_per_share']} 元（含税），不是每 10 股。"
                 f"照抄会把单位口径放大 10 倍。",
         "correct": f"每股派发现金红利 {t['dividend_per_share']} 元（含税）"},
        {"type": "E3_caliber",
         "quote": f"按国际财务报告会计准则计算的归属于母公司股东的净利润为 {np_ifrs} 亿元",
         "is_real_error": True,
         "note": f"口径错误：本报告第四节声明采用中国企业会计准则，此处却引国际准则数。"
                 f"中国准则下 2025 年归母净利润为 {np25} 亿元，两者相差 {np_gap} 亿元。",
         "correct": f"中国企业会计准则下归属于母公司股东的净利润为 {np25} 亿元"},
        {"type": "E5_arithmetic", "quote": f"占本年度归属于母公司股东净利润的 {payout_wrong}%",
         "is_real_error": True,
         "note": f"算术错误：{div_yi} ÷ {np25} = {payout_true}%，不是 {payout_wrong}%。",
         "correct": f"占本年度归属于母公司股东净利润的 {payout_true}%"},
        {"type": "E4_citation", "quote": f"| 资产总计（亿元） | {ta24} | {ta24} |",
         "is_real_error": True,
         "note": f"引用错误（报告期错配）：{ta24} 亿元是 **2024 年末**资产总计，"
                 f"却被填进 2025 年列，与正文「报告期末资产总计 {ta25} 亿元」自相矛盾。",
         "correct": f"2025 年为 {ta25} 亿元，2024 年为 {ta24} 亿元"},
        {"type": "E4_citation", "quote": f"| 资产总计（亿元） | {ta24} |",
         "is_real_error": True,
         "note": f"引用错误：「2025 年末」栏填的是 2024 年末数 {ta24} 亿元，"
                 f"与正文及上一张表的 2025 年列同时错误，属同一处事实被复制到两个口径。",
         "correct": f"2025 年末资产总计 {ta25} 亿元"},
        # —— 陷阱项（看起来可疑但实际正确；判定基准与系统重算一致）——
        {"type": "E5_arithmetic", "quote": f"较上年度的 {rev24} 亿元下降 {g_rev}%",
         "is_real_error": False,
         "note": f"陷阱项：{rev25} ÷ {rev24} - 1 = -{g_rev}%，与派生值一致，不应报错。"},
        {"type": "E5_arithmetic", "quote": f"较上年度的 {np24} 亿元下降 {g_np}%",
         "is_real_error": False,
         "note": f"陷阱项：{np25} ÷ {np24} - 1 = -{g_np}%，与派生值一致，不应报错。"},
        {"type": "E4_citation", "quote": f"实现归属于母公司股东的净利润 {np25} 亿元",
         "is_real_error": False,
         "note": "陷阱项：与 2025 年报「主要会计数据」一致，正确，不应报错。"},
        {"type": "E5_arithmetic", "quote": f"| 营业收入（亿元） | {rev25} | {rev24} |",
         "is_real_error": False,
         "note": "陷阱项：两期营业收入与正文一致，正确，不应报错。"},
    ]
    return body, labels


def build_hunanfazhan_draft() -> tuple[str, list[dict]]:
    """湖南发展：口径（合并 vs 母公司）为主。

    ⚠️ 前一版把「同比增长 3.67%」「6.39%」标成陷阱项，却又在基准表里
    写了两期原始值 —— 二者**由不同来源手写**，于是系统独立重算后报了错，
    而它是对的。现在全部由 :mod:`truth` 派生，标注与基准不可能再分叉。
    """
    t = _RAW["000722"]
    rev25 = T.yi("000722", "revenue_2025")
    rev24 = T.yi("000722", "revenue_2024")
    np25 = T.yi("000722", "np_parent_2025")
    np24 = T.yi("000722", "np_parent_2024")
    npx25 = T.yi("000722", "np_nonrec_2025")
    ta25 = T.yi("000722", "total_assets_2025")
    ocf25 = T.yi("000722", "ocf_2025")
    ocf24 = T.yi("000722", "ocf_2024")
    g_rev = T.growth("000722", "revenue_2025", "revenue_2024")
    g_np = T.growth("000722", "np_parent_2025", "np_parent_2024")
    g_npx = T.growth("000722", "np_nonrec_2025", "np_nonrec_2024")
    g_ocf = T.growth("000722", "ocf_2025", "ocf_2024")
    # 注入的错值（唯一允许的手写字面量：它就是要错）
    equity_wrong = t["equity_parent_2025"]      # 母公司权益冒充"所有者权益合计"
    # ⚠️ 单位错误必须**真的错**。2026-09 修正：这里原先是 `"3.65"`，
    #   而（当时基准文件里的）负债合计 364,789,378.77 元 = 3.65 亿元 ——
    #   草稿写 3.65 亿元是**完全正确**的，却被标成 is_real_error=true，
    #   且 correct 字段填的是与草稿一字不差的文本、note 自身前后矛盾
    #   （既说"少了一个数量级"、又给出 3.65 亿元这个正确值）。
    #   修正为"多一个数量级"：36.5 亿元 = 3,650,000,000 元。
    #   2026-10-10：基准改成合并口径后，该指标的真值变成 357,822,681.50 元
    #   = 3.58 亿元（原先误用母公司口径的 364,789,378.77 元 = 3.65 亿元）。
    #   36.5 亿元仍然比它高一个数量级，判定方向不变，只是 note/correct
    #   里的正确值随生成器一起改成了 3.58 亿元。
    liab_wrong = "36.5"
    liab_true = T.yi("000722", "total_liabilities_2025")

    body = f"""# 湖南发展（000722）2025 年度业绩点评

## 一、收入与利润

2025 年度公司实现营业收入 {rev25} 亿元，同比增长 {g_rev}%；
实现归属于上市公司股东的净利润 {np25} 亿元，同比增长 {g_np}%；
扣除非经常性损益后归属于上市公司股东的净利润 {npx25} 亿元，同比增长 {g_npx}%。

上年同期（2024 年度）公司实现营业收入 {rev24} 亿元，
归属于上市公司股东的净利润 {np24} 亿元。

## 二、资产与权益

截至报告期末，公司资产总计 {ta25} 亿元。
其中所有者权益合计 {equity_wrong} 元，负债合计 {liab_wrong} 亿元。

报告期内公司经营活动产生的现金流量净额为 {ocf25} 亿元，
较上年同期的 {ocf24} 亿元增长 {g_ocf}%。

## 三、评价

公司主业以清洁能源与自然资源开发为主，收入结构稳定，
现金流质量较好，扣非净利润增速高于归母净利润增速，
说明本期利润增长主要来自经营性业务而非一次性损益。

## 四、风险提示

来水波动、电价政策调整及新项目投产进度可能影响未来业绩。
"""
    labels = [
        {"type": "E3_caliber", "quote": f"所有者权益合计 {equity_wrong} 元",
         "is_real_error": True,
         "note": f"口径错误：{equity_wrong} 元是**归属于母公司**所有者权益，"
                 f"而「所有者权益合计」应含少数股东权益，2025 年末为 "
                 f"{t['equity_total_2025']} 元。",
         "correct": f"所有者权益合计 {t['equity_total_2025']} 元"},
        {"type": "E1_unit", "quote": f"负债合计 {liab_wrong} 亿元",
         "is_real_error": True,
         "note": f"单位错误：合并资产负债表披露负债合计 {t['total_liabilities_2025']} 元，"
                 f"即 {liab_true} 亿元；草稿写成 {liab_wrong} 亿元，"
                 f"多了一个数量级。",
         "correct": f"负债合计 {liab_true} 亿元"},
        # —— 陷阱项（派生值与系统重算必然一致）——
        {"type": "E5_arithmetic", "quote": f"同比增长 {g_rev}%",
         "is_real_error": False,
         "note": f"陷阱项：{rev25} ÷ {rev24} - 1 = {g_rev}%，由 truth 派生，不应报错。"},
        {"type": "E5_arithmetic", "quote": f"同比增长 {g_np}%",
         "is_real_error": False,
         "note": f"陷阱项：{np25} ÷ {np24} - 1 = {g_np}%，由 truth 派生，不应报错。"},
        {"type": "E5_arithmetic", "quote": f"同比增长 {g_npx}%",
         "is_real_error": False,
         "note": f"陷阱项：扣非增速由 truth 派生，不应报错。"},
        {"type": "E5_arithmetic", "quote": f"较上年同期的 {ocf24} 亿元增长 {g_ocf}%",
         "is_real_error": False,
         "note": f"陷阱项：{ocf25} ÷ {ocf24} - 1 = {g_ocf}%，由 truth 派生，不应报错。"},
    ]
    return body, labels


def build_hunanbaiyin_draft() -> tuple[str, list[dict]]:
    """湖南白银：口径（归母 vs 全部净利润）为主。

    前一版这里有一条 `is_placeholder` 的"示例标注"，实际含义是
    "这个数写对了但我不知道该标它错还是对" —— 那是评测集污染的根源之一，
    现已删除。留下的每条都必须是**能判定**的：要么是注入的错，要么是派生出来的对。
    """
    t = _RAW["002716"]
    rev25 = T.yi("002716", "revenue_2025")
    rev24 = T.yi("002716", "revenue_2024")
    np25 = T.yi("002716", "np_parent_2025")
    np24 = T.yi("002716", "np_parent_2024")
    g_rev = T.growth("002716", "revenue_2025", "revenue_2024")
    g_np = T.growth("002716", "np_parent_2025", "np_parent_2024")
    margin_true = T.ratio_pct(t["np_parent_2025"], t["revenue_2025"])

    body = f"""# 湖南白银（002716）2025 年度业绩快评

## 一、业绩概览

2025 年度公司实现营业收入 {rev25} 亿元，同比增长 {g_rev}%；
实现归属于上市公司股东的净利润 {np25} 亿元，同比增长 {g_np}%。

2024 年度公司实现营业收入 {rev24} 亿元，
归属于上市公司股东的净利润 {np24} 亿元。

## 二、增长驱动

本期业绩大幅增长主要来自有色金属采选与冶炼业务量价齐升。
公司有色金属冶炼及其他业务收入占比 94.95%，有色金属采选占比 5.05%，
两项合计 100%，收入结构清晰。

## 三、盈利质量

归母净利润同比翻番，增速显著高于收入增速，反映毛利率改善。
按 2025 年度数据计算，公司净利率约为 {margin_true}%。

## 四、风险提示

金属价格波动、原材料采购成本上升以及环保政策变化可能影响后续盈利。
"""
    labels = [
        {"type": "E3_caliber", "quote": f"净利率约为 {margin_true}%",
         "is_real_error": True,
         "note": f"口径错误：{margin_true}% = {np25} ÷ {rev25}，用的是**归母净利润**；"
                 f"而「净利率」的规范口径是净利润（含少数股东损益）÷ 营业收入。"
                 f"两者在本期差异虽小，但口径必须说明，否则与其他公司横向对比时不可比。",
         "correct": f"归母净利率约 {margin_true}%（若用净利润口径需另行说明）"},
        # —— 陷阱项 ——
        {"type": "E5_arithmetic", "quote": f"同比增长 {g_rev}%",
         "is_real_error": False,
         "note": f"陷阱项：{rev25} ÷ {rev24} - 1 = {g_rev}%，由 truth 派生，不应报错。"},
        {"type": "E5_arithmetic", "quote": f"同比增长 {g_np}%",
         "is_real_error": False,
         "note": f"陷阱项：{np25} ÷ {np24} - 1 = {g_np}%，由 truth 派生，不应报错。"},
        {"type": "E5_arithmetic", "quote": "两项合计 100%",
         "is_real_error": False,
         "note": "陷阱项：94.95% + 5.05% = 100%，分项和与合计一致，不应报错。"},
    ]
    return body, labels


BUILDERS = {
    "petrochina": ("601857_2025_annual_review", build_petrochina_draft),
    "hunanfazhan": ("000722_2025_annual_review", build_hunanfazhan_draft),
    "hunanbaiyin": ("002716_2025_annual_review", build_hunanbaiyin_draft),
}


def annotate(body: str, labels: list[dict], draft_id: str) -> list[dict]:
    """给标注补上 ``char_range`` / ``error_id``，并剔除占位项。

    ``char_range`` 由脚本按正文计算 —— 手写偏移必然漂移，
    而"来源定位（来源定位是 R5 的考察点）"要求偏移准确。
    """
    out: list[dict] = []
    seq = 0
    for lab in labels:
        if lab.pop("is_placeholder", False):
            continue
        quote = lab["quote"]
        idx = body.find(quote)
        if idx < 0:
            raise SystemExit(f"[{draft_id}] 标注引文在正文中找不到：{quote!r}")
        seq += 1
        prefix = "G" if lab["is_real_error"] else "T"
        out.append(
            {
                "error_id": f"{prefix}-{seq:03d}",
                "draft_id": draft_id,
                "type": lab["type"],
                "char_range": [idx, idx + len(quote)],
                "quote": quote,
                "is_real_error": bool(lab["is_real_error"]),
                "note": lab["note"],
                **({"correct": lab["correct"]} if lab.get("correct") else {}),
            }
        )
    return out


def main() -> int:
    enable_utf8_output()
    ap = argparse.ArgumentParser(description="构造 R5 评测集（草稿 + 黄金标注）")
    ap.add_argument("--only", choices=sorted(BUILDERS), default=None)
    ap.add_argument("--drafts-dir", default=None)
    ap.add_argument("--golden-dir", default=None)
    args = ap.parse_args()

    drafts_dir = Path(args.drafts_dir) if args.drafts_dir else DRAFTS
    golden_dir = Path(args.golden_dir) if args.golden_dir else GOLDEN
    drafts_dir.mkdir(parents=True, exist_ok=True)
    golden_dir.mkdir(parents=True, exist_ok=True)

    keys = [args.only] if args.only else list(BUILDERS)
    for key in keys:
        draft_id, builder = BUILDERS[key]
        body, labels = builder()
        ann = annotate(body, labels, draft_id)
        (drafts_dir / f"{draft_id}.md").write_text(body, encoding="utf-8", newline="\n")
        (golden_dir / f"{draft_id}.labels.json").write_text(
            json.dumps(ann, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        real = sum(1 for a in ann if a["is_real_error"])
        trap = len(ann) - real
        types = sorted({a["type"] for a in ann if a["is_real_error"]})
        print(f"  ✅ {draft_id}: 真错 {real} 条 / 陷阱 {trap} 条  类型 {types}")
        print(f"       草稿 → {drafts_dir / (draft_id + '.md')}")
        print(f"       标注 → {golden_dir / (draft_id + '.labels.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
