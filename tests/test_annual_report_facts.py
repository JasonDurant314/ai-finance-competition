"""年报（财务报表）抽取回归测试。

背景：真实年报的表格形态与公告**根本不同**，而系统原来只会处理公告那一种：

| | 公告表 | 财务报表 |
|---|---|---|
| 指标在哪 | **列头** | **行标签** |
| 主体在哪 | 行标签（被担保方） | **表外**（报表标题） |
| 期间在哪 | 单元格内 | **列头**（本期 / 上期并排） |
| 单位在哪 | 单元格内 | **表级声明**（``单位：元`` / ``金额单位为人民币百万元``） |

后果（实测）：943 页年报只抽出 241 条事实，召回 14.3%、抽中准确率 25.0%。
根因不是"读错了"，而是三处**静默降级**：数字读对了但主体/科目/期间贴错，
或者整表因"无单位"被拒。本文件把这些修复钉住。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pipelines.r1_extract.table_facts import (
    build_header,
    data_start_row,
    extract_table_facts,
    find_unit_declaration,
    looks_like_statement_table,
    magnitude_outliers,
    merge_fragments,
    metric_of_row_label,
    numeric_defects,
    periods_of_columns,
    previous_period,
    truncation_suspect,
    unit_scale_outliers,
)

# ==========================================================================
# 单位声明
# ==========================================================================


def test_unit_declaration_ignores_compiling_org() -> None:
    """★ 「编制单位：X 公司」不是单位声明。

    实测事故：``编制单位：湖南能源集团发展股份有限公司`` 让整表单位变成
    "湖南能源集团"，于是全部数值因量纲族不符被拒 —— 不报错，
    只表现为"这张表没数据"。
    """
    text = "编制单位：湖南能源集团发展股份有限公司\n单位：元\n货币资金 1,234.00"
    assert find_unit_declaration(text) == "元"


def test_unit_declaration_without_colon() -> None:
    """★ ``金额单位为人民币百万元`` —— 中国石油年报的固定写法，没有冒号。

    旧实现要求 ``单位：`` 连写，于是中国石油 273 页**一个数字都没抽出来**。
    """
    text = "(除特别注明外，金额单位为人民币百万元)"
    assert find_unit_declaration(text) == "百万元"

    text2 = "（金额单位：人民币万元）"
    assert find_unit_declaration(text2) == "万元"


def test_unit_declaration_absent_returns_empty() -> None:
    """找不到就必须返回空串 —— 由调用方决定拒收，绝不允许猜。"""
    assert find_unit_declaration("编制单位：某某股份有限公司") == ""
    assert find_unit_declaration("") == ""


# ==========================================================================
# 表头与数据起始行
# ==========================================================================


def _grid(*rows: list[str]) -> list[list[str]]:
    return [list(r) for r in rows]


def test_data_start_is_after_label_row() -> None:
    """★ ``项目 | 2025 年度 | 2024 年度`` 属于表头，尽管它含数字（年份）。

    旧实现用"第一个含数字的行"定位数据起点，于是期间行被当成数据，
    期间信息丢失、首行数据被吞。
    """
    grid = _grid(
        ["项目", "2025 年度", "2024 年度"],
        ["一、营业总收入", "349,165,168.51", "336,788,863.52"],
    )
    assert data_start_row(grid) == 1


def test_data_rows_are_not_absorbed_into_header() -> None:
    """★ 数据行里含指标名，不能被当成表头行。

    旧实现按"命中别名最多的一行"选表头，而财务报表的**每一行标签都是指标名**，
    于是命中数超过真表头，``data_start`` 被推到第 3 行，前两行数据整行丢失。
    实测 000722 的现金流量表因此 0 候选。

    夹具按真实结构给出：第 1 行是**只有标签、没有数字**的分节行
    （``一、经营活动产生的现金流量：``），它属于数据区，不是表头 ——
    数据从"第一个含纯数字格的行"开始。
    """
    grid = _grid(
        ["项目", "2025 年度", "2024 年度"],
        ["一、经营活动产生的现金流量：", "", ""],
        ["销售商品、提供劳务收到的现金", "357,024,958.98", "359,196,875.70"],
        ["经营活动产生的现金流量净额", "246,931,681.41", "185,187,778.45"],
    )
    alias_map = {"经营活动产生的现金流量净额": "ocf_net", "销售商品、提供劳务收到的现金": "sales_cash"}
    header, start = build_header(grid, alias_map)
    assert start == 2, f"数据起点错误（{start}）：表头只应是前两行"
    assert header[1] == "2025 年度"
    assert header[2] == "2024 年度"
    # 三条数据行（分节行 + 2 条真数据）都要留在数据区
    assert len(grid) - start == 2


# ==========================================================================
# 碎片合并
# ==========================================================================


def test_merge_fragments_handles_missing_probe_row() -> None:
    """已知限制的显式记录：单字标签只在某一行出现时无法合并。

    ``项`` 与 ``目`` 分处两列、而其中一行对应位置是空格时，
    "短汉字 + 短汉字"的判据不成立，两列不会并起来。

    **为什么不修**：这条判据一旦放宽（比如"只要有一行是单字就并"），
    公告表里相邻的短公司名会被误并，代价远大于收益。
    真实年报里 ``项``/``目`` 在表头各行都出现，因此不受影响
    （中国石油 p7 实测合并正常）。这里把限制写成测试，而不是假装没有。
    """
    grid = _grid(["项", "目", "2024", "年"], ["营业收入", "2,937,981", "", ""])
    merged = merge_fragments(grid)
    # 不合并：项 / 目 各自成列
    assert merged.grid[0][:2] == ["项", "目"]
    # 但 2024+年 仍会被并起来（数字+单位尾字，判据更强）
    assert "2024年" in merged.grid[0]


def test_merge_fragments_keeps_negative_numbers() -> None:
    """负数是 ``(2,275,223)`` 这种带数字的括号串，**不能**被当脚注丢掉。"""
    grid = _grid(["减：营业成本", "(2,275,223)"])
    merged = merge_fragments(grid)
    assert merged.grid[0] == ["减：营业成本", "(2,275,223)"]
    assert merged.dropped_cols == []


# ==========================================================================
# 报表识别与行标签映射
# ==========================================================================


def test_statement_table_detected_by_structure() -> None:
    grid = _grid(
        ["项目", "2025 年度", "2024 年度"],
        ["一、营业总收入", "349,165,168.51", "336,788,863.52"],
        ["二、营业总成本", "277,142,878.43", "245,341,431.87"],
    )
    header, start = build_header(grid, {})
    assert looks_like_statement_table(grid, header, start)


def _hunan_income_statement() -> list[list[str]]:
    """**真实网格片段** —— 000722 湖南发展 2025 年年度报告 p77（合并利润表）。

    直接取自 ``runs/annual3/channels/tables.json``，不做美化。
    用真数据做夹具的理由：自己臆造的表会把实现里的隐含假设带进测试，
    于是测试全绿而真实数据上全错 —— 这正是本轮返工的起因。
    """
    return _grid(
        ["项目", "2025 年度", "2024 年度"],
        ["一、营业总收入", "349,165,168.51", "336,788,863.52"],
        ["其中：营业收入", "349,165,168.51", "336,788,863.52"],
        ["二、营业总成本", "277,142,878.43", "245,341,431.87"],
        ["其中：营业成本", "208,717,602.54", "185,653,959.78"],
    )


def _hunan_balance_sheet() -> list[list[str]]:
    """**真实网格片段** —— 000722 湖南发展 2025 年报合并资产负债表（PDF 第 73–75 页）。

    逐行照抄，**不混用母公司报表的行**：同一张表里
    流动资产合计 1,541,959,205.38 + 非流动资产合计 2,005,861,104.34
    = 资产总计 3,547,820,309.72，勾稽成立。母公司资产负债表的对应数是
    另一套（资产总计 3,797,417,361.30），混进来就成了一张不勾稽的表。

    ``其他流动资产`` 与 ``流动资产合计`` 不在 ``alias_map`` 里，留在这里是当"诱饵"：
    测「上位合计不被明细行抢走」。
    """
    return _grid(
        ["项目", "期末余额", "期初余额"],
        ["货币资金", "870,802,898.17", "606,746,350.24"],
        ["其他流动资产", "21,966,978.15", "15,355,560.32"],
        ["流动资产合计", "1,541,959,205.38", "1,441,889,472.52"],
        ["资产总计", "3,547,820,309.72", "3,441,060,747.63"],
        ["负债合计", "357,822,681.50", "269,554,260.75"],
    )


def test_page_unit_declaration_branch_applies_to_statement() -> None:
    """★ 单位只写在**页面文本**里时也必须取到（真实的「单位：元」在正文层）。

    这条是真实事故的回归：单位声明与表格网格**不重叠**（网格由线段重建），
    于是整表被判"无单位"而颗粒无收 —— 而表里的数就摆在那儿（营业总收入 349,165,168.51 元）。
    """
    alias = {"营业收入": "revenue", "营业总收入": "revenue", "营业成本": "operating_cost"}
    cands, st = extract_table_facts(
        _hunan_income_statement(),
        alias_map=alias,
        metric_units={"revenue": "元", "operating_cost": "元"},
        unit_hint="",
        page_no=77,
        page_text="单位：元\n编制单位：湖南能源集团发展股份有限公司",
        doc_entity="湖南能源集团发展股份有限公司",
        doc_period="2025A",
    )
    assert st["statement_table"] == 1
    assert cands, "单位声明在页面文本里时整表被丢弃"
    assert {c["unit"] for c in cands} == {"元"}
    assert {c["unit_source"] for c in cands} == {"table"}


def test_statement_extraction_rejects_when_unit_unknown() -> None:
    """单位彻底找不到时**必须拒收**，不得猜测（本项目铁律）。"""
    cands, st = extract_table_facts(
        _hunan_income_statement(),
        alias_map={"营业收入": "revenue"},
        metric_units={"revenue": "元"},
        unit_hint="",
        page_no=1,
        page_text="没有任何单位声明",
        doc_entity="湖南能源集团发展股份有限公司",
        doc_period="2025A",
    )
    assert cands == []
    assert st["skipped_no_unit"] >= 1


def test_announcement_table_is_not_a_statement() -> None:
    """公告表不能被误判成报表 —— 否则主体会从行标签退化成文档主体。"""
    grid = _grid(
        ["被担保人", "担保金额（万元）"],
        ["海南唐人神生物科技有限公司", "5,000.00"],
        ["湖南某某科技有限公司", "3,000.00"],
    )
    header, start = build_header(grid, {"担保金额": "guarantee_amount"})
    assert not looks_like_statement_table(grid, header, start)


def test_row_label_prefix_stripped() -> None:
    am = {"营业总收入": "revenue", "营业收入": "revenue", "所得税费用": "income_tax_expense"}
    assert metric_of_row_label("一、营业总收入", am) == "revenue"
    assert metric_of_row_label("其中：营业收入", am) == "revenue"
    assert metric_of_row_label("减：所得税费用", am) == "income_tax_expense"
    assert metric_of_row_label("1.归属于母公司股东的净利润", {"归属于母公司股东的净利润": "net_profit_parent"}) == "net_profit_parent"


def test_subtotal_does_not_steal_total() -> None:
    """★ ``流动资产合计`` 不得映射到 ``total_assets``。

    实测：整个 000722 的 total_assets 变成了"流动资产合计 15.4 亿"，
    而真值是"资产总计 41.6 亿" —— 因为 ``流动资产合计`` 里含子串 ``资产合计``。
    """
    am = {"资产合计": "total_assets", "资产总计": "total_assets"}
    assert metric_of_row_label("流动资产合计", am) == ""
    assert metric_of_row_label("非流动资产合计", am) == ""
    assert metric_of_row_label("资产总计", am) == "total_assets"


def test_per_share_label_parenthetical_dropped() -> None:
    """``基本每股收益（人民币元）`` 里的括号说明要去掉。"""
    am = {"基本每股收益": "eps_basic"}
    assert metric_of_row_label("基本每股收益（人民币元）", am) == "eps_basic"


def test_ebitda_label_does_not_map_to_depreciation() -> None:
    """★ ``息税折旧摊销前利润（EBITDA）`` **不得**映射成"折旧摊销费用"。

    实测（2026-09，601857 2025 年报 p95「主要财务指标」）：中国石油的 EBITDA 行
    恰好含子串「折旧摊销」，被 ``depreciation_amortization`` 的别名命中 ——
    而 EBITDA 是**扣除折旧摊销之前**的利润，**语义正好相反**。

    后果不是"多一条候选"：事实库里该指标出现了 ``493,564``（百万元，= EBITDA）
    而真值是 ``215,818``（百万元）—— 量级差 2 倍，且被标成 2023A/2024A。
    下游按量级取最大就会拿到 EBITDA 当折旧摊销。

    这条守卫对**所有**指标生效，不限于 `_UPPER_LEVEL_METRICS`。

    ⚠️ 守卫的写法本身就是个坑（两次实测失败）：

    1. 写 ``息税折旧摊销前`` → **``折旧与摊销`` 也被拦掉**。两个串共享连续子串
       ``折旧摊``，而 ``息税折旧摊销前`` 整体就是 ``息税`` + ``折旧摊`` +
       ``销前`` 的组合。
    2. 改写字面 ``息税前`` → **``息税折旧摊销前利润`` 反而不被拦**
       （它不含连续的 ``息税前``）。

    最终用正则 ``息税.{0,6}前``：必然命中所有"息税…前"写法，
    且不命中 ``折旧及摊销``。
    """
    # 别名表**从真实配置构建**（而不是手写几个）——
    # 手写会让测试与 `configs/metrics.yaml` 脱节：实测就漏掉了
    # `折旧及摊销` 这个真实年报里出现 3 次的写法。
    from domain.tools.audit_rules import load_metric_aliases

    am = dict(load_metric_aliases())
    assert am.get("折旧及摊销") == "depreciation_amortization", (
        "配置里缺 `折旧及摊销` —— 601857 年报里出现 3 次，漏掉会导致该行不入库"
    )

    # 否定式标签：**不得**被子串「折旧摊销」抢走。
    # `configs/metrics.yaml` 里没有 ebitda 指标，因此正确结果是"整条不映射"
    # （这一行被跳过），而不是"映射成折旧摊销"。
    assert metric_of_row_label("息税折旧摊销前利润（EBITDA）（人民币百万元）", am) == ""
    assert metric_of_row_label("息税折旧摊销前利润", am) == ""
    assert metric_of_row_label("息税前利润", am) == ""
    # 真正的折旧摊销行不受影响 —— 用**真实年报里统计到的三种写法**
    # （`折旧、折耗和摊销` 5 次 / `折旧及摊销` 3 次 / `折旧摊销` 3 次）
    assert metric_of_row_label("固定资产折旧、油气资产折耗", am) == (
        "depreciation_amortization"
    )
    assert metric_of_row_label("折旧及摊销", am) == "depreciation_amortization"
    assert metric_of_row_label("折旧、折耗和摊销", am) == "depreciation_amortization"
    # ⚠️ 已知取舍：守卫挡的是**整条标签的所有别名**，而不是"只挡折旧摊销那个"。
    #    因此即使别名表里登记了 ebitda，`息税折旧摊销前利润` 也不会映射到它。
    #    当前 `configs/metrics.yaml` 没有 ebitda 指标，这个取舍没有实际代价；
    #    若将来要接入 ebitda，守卫需改成"只屏蔽语义相反的 canonical"。
    am2 = {**am, "息税折旧摊销前利润": "ebitda"}
    assert metric_of_row_label("息税折旧摊销前利润", am2) == "", (
        "守卫当前屏蔽整条标签 —— 接入 ebitda 时需按 canonical 细化"
    )


# ==========================================================================
# 期间
# ==========================================================================


def test_periods_from_column_headers() -> None:
    """★ 期间写在列头时，按列头分配 —— 这是"两期数据都要有"的根本。"""
    header = ["项目", "2025 年度", "2024 年度"]
    assert periods_of_columns(header, doc_period="2025A") == {1: "2025A", 2: "2024A"}


def test_periods_from_begin_end_balance_words() -> None:
    header = ["项目", "期末余额", "期初余额"]
    assert periods_of_columns(header, doc_period="2025A") == {1: "2025A", 2: "2024A"}


def test_periods_fallback_ignores_change_rate_column() -> None:
    """★ 列序回退不得把「本年比上年增减」当成某一期。

    实测标签（000722 / 002716 的「主要会计数据」都有这一列）：
        项目 | 2025 年 | 2024 年 | 本年比上年增减 | 2023 年
    这一列放的是变动率（`3.67%`），不是余额。给它安一个年份，
    等于在事实库里凭空造出一期数据 —— 既不会报错，quote 也能回读。
    """
    header = ["", "2025 年", "2024 年", "本年比上年增减", "2023 年"]
    assert periods_of_columns(header, doc_period="2025A") == {
        1: "2025A", 2: "2024A", 4: "2023A"
    }


def test_periods_header_fragment_inherits_when_it_holds_data() -> None:
    """★ 片段列**承载数据**时继承左侧期间；**不承载数据**时不继承。

    实测事故（三阶段演进，前两版都被真实数据否决）::

       项目 | 2025 | 年 | 2024 | 年

    * 第一版：按**表头列表下标**指派年份 → ``2024`` 那列（下标 3）
      拿到 2023A，现金 ``206,162`` 从 2025A 漂到 2024A。
    * 第二版：片段列**一律不指派**期间 → 片段位上的**真正数值列**
      掉进列序兜底拿到错误年份（见下一个测试的真实 p8 案例）。
    * 第三版（现在）：片段列**继承左侧期间，但只在它确实承载数据时**。

    最后那道 `data_cols` 闸门是实测加的：601857 p8 那张表里
    「2025 年末」的尾词列（原列 2）**同时**是「2024 年末的数值」所在列，
    继承会把 2024 的数标成 2025A。限定在"有数的列"至少保证
    我们只给自己看得懂的列贴标签。
    """
    header = ["项目", "2025", "年", "2024", "年"]
    # 片段列（2、4）承载数据 → 继承左侧
    assert periods_of_columns(header, doc_period="2025A", data_cols={1, 2, 3, 4}) == {
        1: "2025A", 2: "2025A", 3: "2024A", 4: "2024A"
    }
    # 片段列不承载数据 → 不继承（否则会造出"有期间但没数"的列）
    assert periods_of_columns(header, doc_period="2025A", data_cols={1, 3}) == {
        1: "2025A", 3: "2024A"
    }


def test_periods_do_not_claim_certainty_on_incompatible_column_layouts() -> None:
    """★ 表头行与数据行**列数不同**时，不得对片段位上的数值宣称期间。

    真实案例（2026-09，601857 2025 年报 p8「主要会计数据」）。这是本表
    **实际抽出的网格**（`maps` 是这一格对应的原始列号）::

        r0（9 格） 项目 | 2025 | 年末 | 2024 | 年末 | 年末增减 | (%) | 2023 | 年末
        r1（5 格） 总资产 | 2,828,017 | 2,753,007 | 2.7 | 2,759,237

    逐格看原列号就会发现**两套列结构互不兼容**：

        r0[2] '年末'      ← 原列 2      （属于「2025 年末」这个标签）
        r1[2] '2,753,007' ← 原列 2      （这是 **2024 年末**的数）
        r0[4] '年末'      ← 原列 4      （属于「2024 年末」这个标签）
        r1[4] '2,759,237' ← 原列 4      （这是 **2023 年末**的数）

    原列 2 同时承担"2025 年末的尾词"与"2024 年末的数值"。
    **任何按表头算出的期间映射，在数据行上都不成立。**

    因此本测试断言的是**不变量**（而不是某个具体期间）：

    1. 相邻两期不得被标成同一个期间（2,828,017 与 2,753,007 是相邻两年，
       都在 2025A 就是自相矛盾 —— R5 的表格列期核查正是被这个坑死的）
    2. 判据不得对"落在片段位上、且表头说它是片段"的列凭空断言期间

    实测验证过两条错路，都记在这里以免回退：

    * 按相邻显式期间的**差值插值**：锚点 col1=2025 / col3=2024 / col7=2023，
      列距 2、期间距 1 → 步长 0.5，判据自己就拒绝。
    * 把片段列**并进左列**（给 `_UNIT_TAIL` 加 `年末`）：会把
      ``'2,828,017 2,753,007'`` 并成一格，**两个不同列的数糊在一起**。
    """
    header = ["项目", "2025", "年末", "2024", "年末", "年末增减", "(%)", "2023", "年末"]
    # 表头说 col2 / col4 是片段（`年末`），而它们恰好承载数据 —— 病态列结构
    out = periods_of_columns(header, doc_period="2025A", data_cols={1, 2, 3, 4, 7, 8})
    assert out[1] == "2025A"
    assert out[3] == "2024A"
    assert out[7] == "2023A"
    # 不变量 1：显式判出的相邻两期必须不同
    assert out[1] != out[3] != out[7]
    # 不变量 2：落在片段位上、表头明确说是片段（非数字开头）的列，
    # 传播结果只能是"继承左侧"或"不指派"，**不能凭空造出更早的年份**
    for j in (2, 4, 8):
        got = out.get(j)
        assert got in (None, out.get(j - 1)), (
            f"col{j} 是片段位（表头 {header[j]!r}），不该得到 {got!r}"
        )


def test_periods_fallback_never_labels_quarterly_columns_as_annual() -> None:
    """季度列不得被标成 2025A/2024A —— 那是"四个季度"被写成"四个年份"。"""
    header = ["", "第一季度", "第二季度", "第三季度", "第四季度"]
    assert periods_of_columns(header, doc_period="2025A", data_cols={1, 2, 3, 4}) == {}


def test_periods_fallback_needs_period_words_in_header() -> None:
    """列头不含期间词时不做列序推断 —— 没有依据就不猜。"""
    header = ["项目", "金额", "占比", "人数"]
    assert periods_of_columns(header, doc_period="2025A", data_cols={1, 2, 3}) == {}


def test_periods_fallback_start_index_ignores_label_column() -> None:
    """★ 回退序号从**首个未指派列**起算，不能从 0 起算。

    旧实现用 ``enumerate(unfilled)``，而 ``unfilled`` 的序号不是列号，
    于是首列（空的标签列）被当成最新期间，整行年份整体错位。
    """
    header = ["", "2025 年", "2024 年", "2023 年"]
    assert periods_of_columns(header, doc_period="2025A") == {
        1: "2025A", 2: "2024A", 3: "2023A"
    }


def test_previous_period_only_for_annual() -> None:
    """半年报/季报的"上期"语义不同，不猜。"""
    assert previous_period("2025A") == "2024A"
    assert previous_period("2025H1") == "unknown"
    assert previous_period("") == "unknown"


# ==========================================================================
# 端到端：一张迷你资产负债表
# ==========================================================================


def test_statement_extraction_end_to_end() -> None:
    """报表 → 候选事实：主体取文档主体、期间按列头、单位按表级声明、值换算到元。

    同时验证「上位合计不被明细行抢走」：``流动资产合计`` 不得映射到
    ``total_assets``，否则 ``资产总计`` 那一行取不到值（实测事故）。
    """
    grid = _hunan_balance_sheet()
    alias_map = {
        "货币资金": "cash_and_equivalents",
        "资产总计": "total_assets",
        "负债合计": "total_liabilities",
    }
    metric_units = {v: "元" for v in alias_map.values()}

    cands, stats = extract_table_facts(
        grid,
        alias_map=alias_map,
        metric_units=metric_units,
        unit_hint="",
        page_no=74,
        page_text="单位：元",
        doc_entity="湖南能源集团发展股份有限公司",
        doc_period="2025A",
    )
    assert stats["statement_table"] == 1
    # 3 个指标 × 2 期 = 6 条（「其他流动资产」「流动资产合计」不在配置里，自然跳过）
    assert len(cands) == 6, f"应抽出 6 条，实际 {len(cands)}：{[(c['metric'], c['period']) for c in cands]}"

    # 主体必须是文档主体，不是行标签
    assert {c["entity"] for c in cands} == {"湖南能源集团发展股份有限公司"}
    # 期间必须两期都有（这是"两期数据都要有"的底线）
    assert {c["period"] for c in cands} == {"2025A", "2024A"}

    # ★ 资产总计必须来自「资产总计」那一行，而不是「流动资产合计」
    ta = [c for c in cands if c["metric"] == "total_assets" and c["period"] == "2025A"]
    assert len(ta) == 1
    assert ta[0]["value"] == Decimal("3547820309.72"), (
        f"资产总计取到了错误的行：{ta[0]['row_label']!r} = {ta[0]['value']}"
    )
    assert ta[0]["unit"] == "元"


# ==========================================================================
# 行标签 → 指标：合计行不得被上位明细行抢位（回归）
# ==========================================================================


def test_parent_equity_row_must_not_claim_equity_total() -> None:
    """★ 回归：「归属于母公司所有者权益合计」不得映射到 ``equity_total``。

    曾经的事故：``_STATEMENT_EXCLUDES`` 里没有"归属于母公司"，于是
    **先出现的母公司股东应占行抢走了 equity_total 这个坑位**，
    真正的「所有者权益合计」因 ``taken`` 去重被丢弃 —— R5 于是核不出
    「用归母冒充合计」这一类口径错。修法是把"归属于母公司"加入排除词。

    真值（000722 湖南发展 2025 年报合并资产负债表，PDF 第 74–75 页）::

        归属于母公司所有者权益合计   3,154,311,963.85
        少数股东权益                    35,685,664.37
        所有者权益合计               3,189,997,628.22   ← 归母 + 少数股东，这才是 equity_total

    同一份年报的母公司资产负债表（第 76–77 页）另有自己的
    「所有者权益合计」3,432,627,982.53 —— 两组数口径不同，不能互相当真值。
    """
    am = {"所有者权益合计": "equity_total", "归属于母公司所有者权益合计": "equity_parent"}
    assert metric_of_row_label("归属于母公司所有者权益合计", am) != "equity_total", (
        "「归属于母公司所有者权益合计」被映射成了 equity_total —— "
        "它会抢走坑位，导致真正的「所有者权益合计」被丢弃"
    )


def test_statement_extraction_uses_page_unit_declaration() -> None:
    """单位只在**页面文本**里（表格网格里没有）时也必须能取到。

    实测：`单位：元` 常落在正文层，而网格是从线段重建的，两者不重叠。
    """
    cands, st = extract_table_facts(
        _hunan_income_statement(),
        alias_map={"营业收入": "revenue"},
        metric_units={"revenue": "元"},
        unit_hint="",
        page_no=77,
        page_text="单位：元\n编制单位：湖南能源集团发展股份有限公司",
        doc_entity="湖南能源集团发展股份有限公司",
        doc_period="2025A",
    )
    assert cands and cands[0]["unit"] == "元"
    assert st["skipped_no_unit"] == 0


def test_announcement_table_still_uses_row_subject() -> None:
    """报表路径不得影响公告表：主体仍取行标签。"""
    grid = _grid(
        ["被担保人", "担保金额"],
        ["海南唐人神生物科技有限公司", "5,000.00万元"],
        ["湖南某某科技有限公司", "3,000.00万元"],
    )
    cands, st = extract_table_facts(
        grid,
        alias_map={"担保金额": "guarantee_amount"},
        metric_units={"guarantee_amount": "元"},
        unit_hint="",
        page_no=3,
        page_text="",
        doc_entity="披露主体股份有限公司",
        doc_period="2025A",
    )
    assert st["statement_table"] == 0
    assert {c["entity"] for c in cands} == {
        "海南唐人神生物科技有限公司", "湖南某某科技有限公司",
    }


# ==========================================================================
# F4 / F5：静默的数值损坏守卫
# ==========================================================================


def test_truncation_suspect_detects_broken_thousands() -> None:
    """★ 实测事故：折行修复把 218,073,073.97 修成 218,073,07（少 4 位）。

    位数对不上、千分位分组也不对，**没有任何报错**，置信度仍是 0.95。
    """
    reason = truncation_suspect("218,073,07", Decimal("21807307"), "元")
    assert reason, "位数明显不足却没有被标记"


def test_truncation_suspect_allows_normal_values() -> None:
    assert truncation_suspect("349,165,168.51", Decimal("349165168.51"), "元") == ""
    # 万元→元 会改变位数，属正常换算，不得误报
    assert truncation_suspect("34,916.52万元", Decimal("349165200"), "元") == ""


def test_numeric_defects_flags_leading_zero_fragment() -> None:
    """★ 实测事故：碎片段 ``08.72`` 被当成营业收入 8.72 元入库。

    它随后成了 R5 的"正确值"，让草稿里正常的「营业收入 3.49 亿元」
    被判成量级错误（差 10⁸），产生 2 条假阳性 + 荒谬建议「改为 8.72 元」。

    抓住它的正是**前导零**判据 —— 这条是真实证据，不是补丁式的特例。
    """
    assert numeric_defects("08.72", metric="revenue", unit="元")
    assert not numeric_defects("0.72", metric="revenue", unit="元"), "0.72 是合法写法"
    assert not numeric_defects("870,802,898.17", metric="revenue", unit="元")


def test_numeric_defects_flags_tiny_money_value() -> None:
    """货币指标不足 0.01 元的极端值必然有错。

    ⚠️ 这条判据**刻意做得弱**：曾经写成"小于 1 元"，于是把 0.72 元
    这类合法小额科目也判成错误。真正管用的是前导零与文档级量级离群。
    """
    assert numeric_defects("0.001", metric="revenue", unit="元")
    assert not numeric_defects("0.01", metric="revenue", unit="元")


def test_numeric_defects_allows_zero() -> None:
    """0 是合法的财务数据（某科目本期为零），不能误伤。"""
    assert not numeric_defects("0.00", metric="short_term_borrowing", unit="元")


def test_magnitude_outliers_flags_unconverted_millions() -> None:
    """★ 实测事故：中国石油年报以「人民币百万元」为单位，漏换算得到 2.7。

    真值是 2,700,000 百万元。单条看像个正常小数，横向比较才暴露。
    """
    items = [
        ("a", Decimal("2864469")),
        ("b", Decimal("2937981")),
        ("c", Decimal("2828017")),
        ("bad", Decimal("2.7")),
    ]
    assert magnitude_outliers(items) == ["bad"]


def test_magnitude_outliers_does_not_flag_large_values() -> None:
    """只标偏小的一侧：偏大往往是真实离群（资产总计 vs 明细科目）。"""
    items = [
        ("small", Decimal("100")),
        ("mid", Decimal("120")),
        ("big", Decimal("2828017000000")),
    ]
    assert magnitude_outliers(items) == []


def test_magnitude_outliers_needs_enough_observations() -> None:
    """样本不足时不判定 —— 两个点之间没有"中位数"可言。"""
    assert magnitude_outliers([("a", Decimal("1")), ("b", Decimal("1000000"))]) == []


def test_unit_scale_outlier_detection() -> None:
    """同表内同指标数量级差 10³ 以上 → 疑似量纲错误。"""
    items = [("a", Decimal("1244")), ("b", Decimal("1200")), ("c", Decimal("1300")),
             ("d", Decimal("1"))]
    assert unit_scale_outliers(items) == ["d"]


def test_unit_scale_outlier_ignores_small_tables() -> None:
    """少于 3 项时不判定 —— 样本太小，判了就是猜。"""
    assert unit_scale_outliers([("a", Decimal("1")), ("b", Decimal("1000000"))]) == []


# ==========================================================================
# 列号必须始终是单个整数
# ==========================================================================


def test_candidate_columns_are_plain_ints() -> None:
    """★ ``cand["col"]`` 必须是 ``int``，不能是列表。

    事故：合并碎片后 ``col_maps`` 存的是"原列号列表"（一个合并格可能横跨
    多列），而下游把它当 dict 键用（单元格 bbox 索引），于是抛
    ``TypeError: unhashable type: 'list'`` —— 而且是在 **judge 跑完
    24 分钟之后**的 build_facts 阶段才炸，白烧一次模型调用。

    夹具用碎片表（``2024 | 年`` 分列），因为那正是产生"多列合并"的形态。
    """
    grid = _grid(
        ["项目", "2024", "年", "2023", "年"],
        ["营业收入", "2,937,981", "", "3,012,812", ""],
        ["资产总计", "2,753,007", "", "2,759,237", ""],
    )
    cands, _ = extract_table_facts(
        grid,
        alias_map={"营业收入": "revenue", "资产总计": "total_assets"},
        metric_units={"revenue": "元", "total_assets": "元"},
        unit_hint="百万元",
        page_no=8,
        page_text="(金额单位为人民币百万元)",
        doc_entity="601857",
        doc_period="2024A",
    )
    assert len(cands) == 4
    for c in cands:
        assert isinstance(c["col"], int), f"col 类型错误：{c['col']!r}"
        assert isinstance(c["row"], int)
    # 期间来自碎片合并后的列头
    assert {c["period"] for c in cands} == {"2024A", "2023A"}


def test_merged_cell_columns_are_hashable_in_downstream_keys() -> None:
    """``col_maps`` 的每一项是**原列号列表**（合并格横跨多列）。

    这条测试直接钉住那个接口约定 —— 事故正是"把列表当整数用"，
    在下游 build_facts 阶段抛 ``TypeError: unhashable type: 'list'``。
    """
    grid = _grid(
        ["项目", "2025", "年", "2024", "年"],
        ["营业收入", "349,165,168.51", "", "336,788,863.52", ""],
    )
    mg = merge_fragments(grid)
    # 表头行的合并格映射到两列：['2025','年'] → [1, 2]
    assert mg.col_maps[0][1] == [1, 2], mg.col_maps[0]
    # 数据行只占其中第一列 → 单元素列表
    assert mg.col_maps[1][1] == [1], mg.col_maps[1]
    # 候选必须把列表收敛成整数（否则下游 dict/set 直接崩）
    cands, _ = extract_table_facts(
        grid,
        alias_map={"营业收入": "revenue"},
        metric_units={"revenue": "元"},
        unit_hint="元",
        page_no=77,
        page_text="单位：元",
        doc_entity="湖南能源集团发展股份有限公司",
        doc_period="2025A",
    )
    for c in cands:
        assert isinstance(c["col"], int)
    keys = {(c["entity"], c["metric"], c["row"], c["col"]) for c in cands}
    assert len(keys) == len(cands)
