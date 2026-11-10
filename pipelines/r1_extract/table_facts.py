"""表格 → 事实：**确定性优先**的第二条抽取路径。

为什么需要它（实测动机）：

在真实材料上跑完 R1 之后，222 条事实里有 **161 条（73%）来自 VLM 整页扫描**，
而它们读的正是**已经用几何方法重建好的表格**。也就是说：

* 同一批数字，明明有确定性来源（文本层 + 表格网格），却在花模型调用去"看"
* VLM 读数的基础置信度更低（L3 0.70），还必须过 quote 回读闸门
* 页面上密密麻麻的数字靠模型肉眼归属，天生比按行列取值更容易错配

本模块把表格网格变成**一等抽取来源**：

    行主体（公司名） × 列指标（表头里的指标名） × 单元格数值

三条硬约束（与 5.1 规则抽取一致，宁缺勿错）：

1. **单位必须明确** —— 单元格里带单位、表头里带单位、或表内/表前有
   「单位：X」声明。三者都找不到就**不入库**（金融数据不允许猜单位，
   万元与元差 10⁴，猜错比漏抽严重得多）。
2. **指标必须来自表头**，且量纲族与指标声明一致。
3. **主体必须是数据行的行标签**（第一列的文字），不能拿数值当主体名。

产出的事实 `extractor=table_grid@1.0.0`，证据锚点是**单元格 bbox** ——
比整页 quote 更精确：能直接指到第几行第几列。
"""

from __future__ import annotations

import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from agent.validate.numeric import (
    normalize_unit,
    parse_decimal,
    repair_line_broken_numbers,
    to_yuan,
)

# 与 build_facts 保持一致的量纲族判定
_MONEY_UNITS = {"元", "千元", "万元", "百万元", "亿元", "万", "亿"}
_SHARE_UNITS = {"股", "千股", "万股", "亿股"}
_RATIO_UNITS = {"%", "%", "‰", "个百分点"}
_PER_SHARE_UNITS = {"元/股", "元每股"}

_UNIT_DECL = re.compile(
    r"(?<!编制)(?<!填报)\s*(?:金额\s*)?单位\s*[:：]\s*([^\s，,。；;）)]{1,8})"
)
"""「单位：X」声明。

⚠️ **必须排除「编制单位」「填报单位」** —— 它们后面跟的是公司名而不是单位。
实测踩过：``编制单位：湖南能源集团发展股份有限公司`` 让整张表的单位变成
"湖南能源集团"，于是全部数值因"量纲族不匹配"被拒收，而且不报错。

⚠️ 也必须容忍**没有冒号**的写法：``金额单位为人民币百万元``
（中国石油年报的固定写法）。旧版要求 ``单位：`` 连写，
于是中国石油 273 页里一个数字都没抽出来 —— 症状是"这家公司没数据"。
"""

_UNIT_DECL_LOOSE = re.compile(
    r"单位\s*(?:为|是)?\s*[:：]?\s*(?:人民币)?\s*"
    r"(亿元|百万元|十万元|万元|千元|元/股|元每股|亿股|万股|千股|元|股|%|％|倍)"
)
"""宽松版：只用于**兜底**，因为它容易误伤（``编制单位：X`` 也会被它命中）。

因此它的调用方必须先剥掉「编制/填报单位」这类干扰片段，见
:func:`find_unit_declaration`。
"""

_UNIT_IN_CELL = re.compile(r"(亿元|百万元|万元|千元|元/股|元每股|亿股|万股|千股|元|股|%|％|倍)\s*$")
# 表头里的单位：`预计担保额度（万元）` / `注册资本(元)` / `持股比例 %`
_UNIT_IN_LABEL = re.compile(
    r"[（(]\s*(亿元|百万元|万元|千元|元/股|元每股|亿股|万股|千股|元|股|%|％|倍)\s*[）)]"
    r"|(亿元|百万元|万元|千元|元/股|元每股|亿股|万股|千股|%|％|倍)\s*$"
)
_NUMERIC_CELL = re.compile(r"^-?[\d,]+(?:\.\d+)?$")
_NUMBERISH = re.compile(r"\d")

# 「编制单位：X」「填报单位：X」——这些不是单位声明
_ORG_DECL = re.compile(r"(?:编制|填报|被审计|委托|主管)单位\s*[:：]\s*[^\s，,。；;）)]{1,40}")


def unit_in_label(label: str) -> str:
    """从表头单元格里取单位（``预计担保额度（万元）`` → ``万元``）。"""
    m = _UNIT_IN_LABEL.search(_clean_label(label))
    if not m:
        return ""
    return normalize_unit(m.group(1) or m.group(2) or "")


def unit_family(unit: str) -> str:
    u = normalize_unit(unit)
    if u in _MONEY_UNITS:
        return "money"
    if u in _SHARE_UNITS:
        return "share"
    if u in _RATIO_UNITS:
        return "ratio"
    if u in _PER_SHARE_UNITS:
        return "per_share"
    return "other"


def expected_family(metric_unit: str) -> str:
    return unit_family(metric_unit or "")


def find_unit_declaration(page_text: str) -> str:
    """从页面文本里找「单位：X」声明。找不到返回空串。

    三步，按可靠性递减：

    1. **剥掉「编制单位：」「填报单位：」等干扰片段**，再找 ``单位：X``
    2. 宽松匹配 ``金额单位为人民币百万元`` 这类无冒号写法
    3. 都找不到返回空串（**调用方必须把空串当作"未知"处理，不得猜**）

    ⚠️ 实测教训：早期只有第 1 步的严格写法，于是

    * ``编制单位：湖南能源集团发展股份有限公司`` 被当成单位（得到公司名）
    * ``(除特别注明外，金额单位为人民币百万元)`` 完全匹配不上

    两者的共同症状都是**不报错**：前者让整表因量纲族不符被拒收，
    后者让中国石油 273 页一个数字都抽不出来，看起来像"这家公司没披露"。
    """
    text = _ORG_DECL.sub(" ", page_text or "")

    # 取**最后一个**「单位：X」—— 中文报表里「编制单位」在标题，
    # 真正的单位声明在表格上方，位置更靠后。
    found = ""
    for m in _UNIT_DECL.finditer(text):
        candidate = normalize_unit(m.group(1))
        if unit_family(candidate) != "other":
            found = candidate
    if found:
        return found

    m = _UNIT_DECL_LOOSE.search(text)
    if m:
        candidate = normalize_unit(m.group(1))
        if unit_family(candidate) != "other":
            return candidate
    return ""


def table_unit_hint(grid: list[list[str]], page_text: str) -> str:
    """推断整张表的单位：先看表头，再看页面上的「单位：」声明。"""
    for row in grid[:2]:
        for cell in row:
            m = re.search(r"[（(]\s*单位\s*[:：]\s*([^\s，,。；;）)]{1,6})", cell or "")
            if m:
                candidate = normalize_unit(m.group(1))
                if unit_family(candidate) != "other":
                    return candidate
    return find_unit_declaration(page_text)


_CJK_CHAR = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\U00020000-\U0002ffff]")


def _is_cjk(ch: str) -> bool:
    return bool(ch) and bool(_CJK_CHAR.match(ch))


def _clean_label(text: str) -> str:
    """单元格文本归一：折行按中英分别处理，**拉丁词之间保留一个空格**。

    ⚠️ 早期写成 ``replace(" ", "")``（把空格全删），实测踩过一个隐蔽的坑：

        表格路径产出主体 ``JinkoSolar(U.S.)Inc.``
        VLM 读出的是        ``JinkoSolar (U.S.) Inc.``

    同一家公司在事实库里变成**两个主体** —— 既虚增主体数、又让"双路互证"
    少算了 70 个本该对上的匹配（模型读数被误判成"确定性路径没覆盖"），
    还会让 R5 的同比重算以为两个"不同主体"各有一期数据。

    折行处理必须分中英：
      * 中文折行是排版断行 → 直接相连（「唐人神集团股」+「份有限公司」）
      * 拉丁词之间断行 → 补一个空格（「JinkoSolar (U.S.)」+「Inc.」）
    """
    t = text or ""
    if not t:
        return ""
    parts = [p for p in re.split(r"[ \t\u3000]*\n[ \t\u3000]*", t)]
    out = ""
    for p in parts:
        if not p:
            continue
        if out and not (_is_cjk(out[-1]) or _is_cjk(p[0])):
            out += " "
        out += p
    # 其余空白（含全角空格）折叠为单个半角空格
    return re.sub(r"[ \t\u3000]+", " ", out).strip()


def _label_like(text: str) -> bool:
    """像不像一个**表头标签**（而不是一格数据）。

    表头标签短、不含冒号/分号/句号；数据格往往是一个完整的句子或
    「指标：数值；指标：数值」这样的一整块。
    """
    t = text or ""
    if not (2 <= len(t) <= 24):
        return False
    return not any(ch in t for ch in "：:；;。%")


def _count_numbers(row: list[str]) -> int:
    """一行里能解析成数字的**数据格**数（年份不算）。

    ⚠️ 必须**先去掉千分位逗号**再解析：``parse_decimal("349,165,168.51")``
    返回 None（它只接受纯数字串），于是带千分位的表格会被判成"没有数字"，
    进而让 :func:`looks_like_statement_table` 认为这不是财务报表 ——
    实测这个细节让整条报表路径对 000722 完全失效。

    ★ **4 位年份不算数据**（2026-09 实测补充）。

    中文年报的"主要财务数据"表把多期年份印在**表头行**里：

        r0  截至或截止各年度 | 12 | 月 | 31 | 日        ← 标题（含"12""31"）
        r1  项 | 目 | 2025 | 年 | 2024 | 年 | 2023 | 年   ← 真表头（含年份）
        r2  营业收入 | 2,864,469 | 2,937,981 | ...

    旧实现把 ``2025`` 当成数字格，于是 r1 被判定为"数据行"、
    ``data_start_row`` 返回 1 → **真表头被当成数据吞掉**，而 r0 那个
    标题行成了表头。后果：单位（写在行标签里）解析不到，
    整表 0 条候选（实测 ``skipped_no_unit=20``）。

    为什么可以安全排除 4 位年份：**数据行里不会只有年份**——
    它总还带着真正的数值（金额/比率），去掉年份后仍有数字；
    而表头行去掉年份后就没有数字了。这条判据因此不会误伤数据行。
    """
    n = 0
    for cell in row:
        t = _clean_label(cell).replace(",", "").replace("，", "")
        if not t:
            continue
        if re.fullmatch(r"20\d{2}", t):
            continue          # 年份是**表头**信息，不是数据
        if parse_decimal(t) is not None:
            n += 1
    return n


def build_header_rows(
    grid: list[list[str]], alias_map: dict[str, str], *, max_header_rows: int = 6,
) -> tuple[list[str], list[list[str]], int]:
    """同 :func:`build_header`，但**额外返回表头各行的原始内容**。

    为什么需要（2026-09 实测）：期间信息常常分散在**多行**表头里 ——

        r0 [0]项目 [1]2025 [2]年末 [3]2024 [4]年末 [5]年末增减 [6](%) [7]2023 [8]年末

    拼接成单行后，片段列（``年末`` 自己占一列）会与邻居粘成
    ``2025年末`` / ``2024年末``（这两列能判出期间），但**落在片段位上的
    数值列**（上例的 col2、col4 —— 真正的值就在这里）在单行里已经无法
    与前一列区分。拿到原始行，才能逐行判 + 做跨列传播
    （见 :func:`periods_of_columns` 判据②b）。

    返回值：``(拼接后的单行表头, 表头各原始行, 数据起始行)``。
    """
    header, data_start = build_header(grid, alias_map, max_header_rows=max_header_rows)
    rows = [list(grid[i]) for i in range(min(max(1, data_start), len(grid)))]
    return header, rows, data_start


def build_header(
    grid: list[list[str]], alias_map: dict[str, str], *, max_header_rows: int = 6,
) -> tuple[list[str], int]:
    """把表头行拼成每列的标签，返回 ``(每列的表头文本, 数据起始行)``。

    **判据：数据从"第一个含数字的行"开始，它上面的行全部属于表头。**

    这是本函数第三次重写，前两版都栽在同一个地方 —— 用"指标别名命中数"
    来选表头行：

    * 第一版只取前两行。中文财报的表头常常是 3–4 行（标题 / 分组 / 单位声明 /
      指标行），于是指标名根本没进表头，48 张表只抽出 11 条事实。
    * 第二版改成"在前若干行里找命中别名最多的一行"。这在**公告表**上有效
      （表头行写满指标名，数据行是公司名+数字），但在**财务报表**上完全失效：

          项目            | 2025 年度        | 2024 年度     ← 真表头，命中 0
          一、营业总收入   | 349,165,168.51  | 336,788,863.52 ← 含"营业收入"，命中 1
          其中：营业成本   | 208,717,602.54  | 185,653,959.78 ← 含"营业成本"，命中 1

      数据行逐行都含指标名（行标签就是指标名），命中数轻松超过真表头，
      于是 ``data_start`` 被推到第 3 行，**前两行数据被当成表头吞掉**；
      更糟的是表头文本变成了「2025 年度349,165,168.51」这种"期间+数值"混合串，
      列映射与单位解析随之全错。实测 000722 的现金流量表因此 0 候选。

    新判据不依赖别名，只看**结构**：表头行不含数字，数据行含数字。
    对公告表同样成立（表头「被担保公司名称 | 注册资本（万元）」无数字）。

    真正需要"哪一行是指标行"的地方由 :func:`map_columns_to_metrics`
    逐列处理 —— 它与表头行数无关。
    """
    if not grid:
        return [], 0
    n_cols = max((len(r) for r in grid), default=0)
    data_start = data_start_row(grid, max_scan=max_header_rows)

    # ★ 只拼接**列数与数据行一致**的表头行。
    #
    #   实测（2026-09，601857 2025 年报 p7「主要财务数据」）：该表表头 9 格、
    #   数据行 5 格 —— **两套互不兼容的列结构**。原实现逐列把所有表头行拼起来，
    #   于是标题行的 `12`/`月`/`31`/`日` 与真表头的 `项`/`目`/`2025`/`年`
    #   按列号强行配对，拼出 `'截至或截止各年度项'`、`'12目'`、`'月2025'`
    #   这种垃圾标签。后果：列头单位解析不到 → 整表 0 条候选。
    #
    #   判据：数据行列数与众不同的表头行，**不参与拼接**（多半是跨列标题）。
    #   数据行取 `data_start` 行 —— 它一定列数一致（就是"标准"行）。
    #   保留与数据行同列数的表头行，因此正常的单行/多行表头不受影响
    #   （实测 p8、资产负债表等表的表头行与数据行列数一致）。
    #
    #   ⚠️ 这是**有界的**缓解，不是根本修复。根治要按单元格 bbox 重建网格
    #   （见 README 已知限制第 13 条）。它至少让"标题行污染表头"不再发生。
    data_n_cols = len(grid[data_start]) if data_start < len(grid) else n_cols
    head_rows = [
        i for i in range(data_start)
        if _row_populated_cols(grid[i]) == data_n_cols
    ]
    if not head_rows:                      # 全都不匹配 → 退回原行为（不制造新错）
        head_rows = list(range(data_start))

    cols: list[str] = []
    for j in range(n_cols):
        parts = []
        for i in head_rows:
            if j < len(grid[i]):
                parts.append(_clean_label(grid[i][j]))
        cols.append("".join(parts))
    return cols, data_start


def _row_populated_cols(row: list[str]) -> int:
    """该行**有内容**到第几列（最右非空格的列号 + 1）。

    用于判断两行的列结构是否一致。用"最右非空列"而不是"总长度"：
    网格常常把右侧补成等长的空串，长度相同但实际占用的列数不同
    （实测 p7 的表头行 9 格全有内容、数据行只填到第 5 格）。
    """
    last = 0
    for k, c in enumerate(row):
        if (c or "").strip():
            last = k + 1
    return last


def map_columns_to_metrics(
    header: list[str],
    alias_map: dict[str, str],
    metric_units: dict[str, str],
) -> dict[int, str]:
    """列号 → 指标。取**最长别名**优先，避免「净利润」抢走「归母净利润」的列。"""
    alias_sorted = sorted(alias_map, key=len, reverse=True)
    out: dict[int, str] = {}
    used: set[str] = set()
    for alias in alias_sorted:
        canonical = alias_map[alias]
        if canonical in used:
            continue
        for j, label in enumerate(header):
            if j in out or not label:
                continue
            # 表头可能是多行拼接出来的（「注册资本」+「（万元）」），
            # 因此这里**不能**用长度限制；但要排除**数据块** ——
            # 「资产总额：312,460.60万元；负债总额：…」这种一格数据里也含指标名。
            # 判据用「冒号紧跟数字」：数据块必然是"指标名：数值"，
            # 而正常的表头即使带冒号也是"（单位：元，未经审计）」这种声明。
            if re.search(r"[:：]\s*-?[\d（(]", label):
                continue
            if alias in label:
                out[j] = canonical
                used.add(canonical)
                break
    return out


_SUBJECT_HEADER_HINTS = ("被担保人", "公司名称", "企业名称", "股东名称", "客户名称",
                         "标的公司", "名称")

# ==========================================================================
# 列 → 期间
# ==========================================================================

# 「期末余额 / 期初余额」这类相对写法：语义由**指标类型**决定
_CURR_END_HINTS = ("期末余额", "期末数", "年末余额", "年末数", "本报告期末", "期末")
_CURR_BEGIN_HINTS = ("期初余额", "期初数", "年初余额", "年初数", "上年度末", "期初")

_PERIOD_COL = re.compile(r"(20\d{2})\s*年?\s*(半年度|年度|全年|前三季度|一季度|三季度|半年)?")
_YEAR = re.compile(r"(20\d{2})")
_PERIOD_SUFFIX = {
    "半年": "H1", "半年度": "H1", "前三季度": "Q3", "一季度": "Q1",
    "三季度": "Q3", "年度": "A", "全年": "A", "": "A",
}


def periods_from_header_rows(
    header_rows: list[list[str]],
    *,
    doc_period: str,
    prev_period: str | None = None,
    data_cols: set[int] | None = None,
    grid: list[list[str]] | None = None,
    data_start: int = 0,
) -> dict[int, str]:
    """用**原始表头行**逐行取期间，取不到的列再看后续行。

    为什么需要它：``build_header`` 只返回**一行**表头文本，而真实年报的
    表头常常是两三层。实测 601857 2024 年报 p115：

        r1  2024 | 年度 | 2023 | 年度 | 2024 | 年度 | 2023 | 年度
        r2  项目 | 附注 | 合并 | 合并 | 公司 | 公司
        r3  营业收入 | 42 | 2,937,981 | 3,012,812 | 1,810,603 | 1,851,487

    ``build_header`` 返回的是 ``r1``，于是期间信息本来就在 ——
    但另一类表（p7）返回的是 ``['截至或截止各年度','12','月','31','日',…]``，
    年份在**下一行**，期间就全丢了。

    这里按行顺序合并：先到先得，已有期间的列不被覆盖。

    ★ **列序回退（判据③）的启用条件比单行调用严格得多**：
    只有当**所有表头行合起来仍然一个期间标签都没有**时才启用。

    理由（实测，601857 2024 年报 p8）：

        r0  2023 | 年 | 2023 | 年 | 本年比上年 | 2022 | 2022
        r1  项目 | 2024 | 年 | ( | 追溯后 | ) | 追溯前 | 增减 | (%) | ...
        r2  营业收入 | 2,937,981 | 3,012,812 | 3,011,012 | (2.5) | 3,240,951

    这张表**有**年份标签（col1=2024 年、col2/col3=2023 年、col5=2022 年），
    只是分列/合并得七零八落。若仍按列序兜底，就会给 col0 安一个 2023A，
    于是"col1 是 2024 年"与"col0 是 2023 年"两条结论互相矛盾 ——
    而 col1 的 2,937,981 是**2024 年**的收入，被标成 2025A 或 2023A 都是错的。

    判据③ 的前提是"这些列按期间排列"。表里只要**存在**期间标签，
    这个前提就不再需要靠猜 —— 猜只会与已知标签打架。
    """
    out: dict[int, str] = {}
    # 标签列永远不是期间列 —— 这条守卫对**所有**判据生效，不只是判据③。
    #
    # 实测（601857 2024 年报 p8）：表头第一行是
    #     `2023 | 年 | 2023 | 年 | 本年比上年 | 2022 | 2022`
    # 其中首个 `2023` 落在 **col0**，而 col0 是本表的科目标签列
    # （数据行是 `营业收入 | 2,937,981 | …`）。若把 col0 认成 2023A 期间列，
    # 会连带让判据③ 以为"期间已有着落"而拒绝兜底，
    # 最终整表的期间全错或全空。
    label_col = (
        _label_column(grid, data_start, max(len(r) for r in grid)) if grid else -1
    )

    for row in header_rows:
        got = periods_of_columns(
            row,
            doc_period=doc_period,
            prev_period=prev_period,
            data_cols=data_cols,
            grid=grid,
            data_start=data_start,
            allow_column_order=False,
        )
        for j, p in got.items():
            if j == label_col:
                continue
            out.setdefault(j, p)

    if out:
        return out

    # 汇总后仍无任何期间 → 才允许列序兜底
    got = periods_of_columns(
        header_rows[0] if header_rows else [],
        doc_period=doc_period,
        prev_period=prev_period,
        data_cols=data_cols,
        grid=grid,
        data_start=data_start,
    )
    return got


def _label_column(grid: list[list[str]], start: int, n_cols: int) -> int:
    """找出**科目/主体标签列**的列号（列序回退必须跳过它）。

    ⚠️ 不能假设"标签列一定是 col0"。实测 601857 2024 年报 p8 的表格：

        r0  2023 | 年 | 2023 | 年 | 本年比上年 | 2022 | 年 | 2022 | 年
        r1  项目 | 2024 | 年 | ( | 追溯后 | ) | ( | 追溯前 | ) | 增减 | (%) | ...
        r2  营业收入 | 2,937,981 | 3,012,812 | 3,011,012 | (2.5) | 3,240,951 | ...

    科目名 `营业收入` 在 **col1**，期间列从 **col0** 起 ——
    因为这是"追溯调整前后"对比表，首行年份被印刷在科目列上方。
    早期实现硬编码"跳过 j=0"，于是整行年份**整体错位一格**：
    2024 年的收入被标成 2025A（跑 2025 年报时反之）。
    这种错误不报错、quote 也能回读，只有对着年报逐格核对才会发现。

    判据取"数据区里非数字文本最多的那一列"，平票取最靠左 ——
    这是结构性事实（标签列必然全是文字），不依赖对具体表格的假设。
    """
    best_col, best_score = 0, -1
    for j in range(n_cols):
        texts = 0
        for i in range(start, len(grid)):
            row = grid[i]
            if j >= len(row):
                continue
            t = _clean_label(row[j]).replace(",", "").replace("，", "")
            if t and _NUMERIC_CELL.match(t):
                texts -= 1          # 数值格：这是数据列
            elif t and not re.fullmatch(r"[\d/\-年月日.%（）()]+", t):
                texts += 1          # 文字格：这是标签列
        if texts > best_score:
            best_col, best_score = j, texts
    return best_col


def _data_columns(grid: list[list[str]], start: int, n_cols: int) -> set[int]:
    """数据区里**真正承载数字**的列号集合。

    为什么需要它：表头经过分列后会产生**片段列** —— `2025 | 年 | 2024 | 年`
    里的 ``年`` 自己占一列。这类列头看着像期间，实际不含任何数据。
    列序回退（:func:`periods_of_columns` 判据③）若把它们也算成一期，
    就会给相邻的真实数据列派错年份（实测：p95 的 `206,162` 被标成 2024A）。

    判据只认**数据区里出现过可解析数字**的列：列头文本无法自我证明，
    但"这一列下面有没有数"是结构性事实。
    """
    cols: set[int] = set()
    for row in grid[start:]:
        for j in range(n_cols):
            if j in cols or j >= len(row):
                continue
            t = _clean_label(row[j]).replace(",", "").replace("，", "")
            if not t:
                continue
            if _NUMERIC_CELL.match(t) or re.search(r"\d", t):
                cols.add(j)
    return cols


_YEAR4_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")


def _majority_data_columns(
    grid: list[list[str]], start: int, n_cols: int, *, min_frac: float = 0.4,
) -> set[int]:
    """**多数行**都带数字的列 —— :func:`_data_columns` 的严格版本。

    为什么需要它（2026-09 第十四轮实测，正好卡住 capex）
    --------------------------------------------------
    `_data_columns` 的判据是"**任一**行出现过数字"。对一般表格没问题，
    但对"数据区后面紧跟脚注"的表会失效：

        资本性支出        | 269,089 | 275,849 | 275,393 | 274,494 | 251,323
        经营活动产生的现金流量净额 | 412,510 | 406,532 | 456,847 | 393,246 | 341,424
        注：（1）截至 2021 年 12 月 31 日、2022 年 12 月 31 日、……
        12 月 31 日，每股基本及摊薄盈利是按照……

    脚注被 PDF 拆成日期碎片（``12`` ``月`` ``31`` ``日``），**它们也是"数字"**，
    于是第 6 列因为脚注里的 ``12`` 被算成"数据列"：
    实测数据列从应有的 **5 列膨胀到 6 列**（排除脚注行后仍是 6，
    因为脚注**起始行** ``注：（ | 1 | ）截至 | …`` 自己就带 ``1``）。

    而 `periods_from_year_sequence` 的判据 1 是"年份数**恰好等于**数据列数"，
    多这一列就永远不成立 —— **这个坑正是它要解决的问题本身**。

    判据改成**计数比例**：某列在数据行里带数字的行数 ≥ ``min_frac`` 比例才算。
    真数据列（每行都有值）比例接近 1.0，脚注列只出现一次（≈0.14），
    两者区分得很干净。

    ⚠️ 刻意**不改** `_data_columns` 本身：它被 `periods_of_columns` 等
    三处使用，收紧它会牵动所有走行导向的表 —— 而那块正是第十三轮
    刚弄坏过一次的地方。这里只给新规则提供一个更严的兄弟函数，
    影响面可逐处核对。
    """
    rows = [r for r in grid[start:] if r]
    if not rows:
        return set()
    need = max(1, int(len(rows) * min_frac + 0.999))   # 向上取整，至少 1 行
    counts = [0] * n_cols
    for row in rows:
        for j in range(min(n_cols, len(row))):
            t = _clean_label(row[j]).replace(",", "").replace("，", "")
            if t and (_NUMERIC_CELL.match(t) or re.search(r"\d", t)):
                counts[j] += 1
    # 第 0 列通常是行标签列（是文字不是数值），即便它偶尔带数字也不该算数据列
    return {j for j in range(1, n_cols) if counts[j] >= need}


# 是否启用"表内连续年份序列 → 列期间"兜底（见 periods_from_year_sequence）。
#
# ★ **默认开启**（2026-09 第十四轮）。
#
# 它只在一种表上介入：**同一个期间落到了两个不同的数据列上**（期间碰撞）。
# 两个列代表两个年份，不可能共享一个期间 —— 碰撞说明"列 → 期间"的映射是坏的。
# 实测（四份年报 987 张表）只在 **4 张表（0.4%）** 上成立，误报面极小，
# 而它修掉的正是卡了六轮的那个问题：
#
#     601857 的 2025A 资本开支候选从 3 条（269,089 / 275,849 / 275,393）
#     **收敛到 1 条 —— 就是正确的那条**，于是 R4 终于能用上事实值：
#         capex_ratio_of_revenue = 9.39%（事实）→ DCF ¥19.4316 → ¥14.0394
#
# 启用前的完整量测（本轮纪律：先量再改）：
#
#     规则命中        4 / 987 张表
#     一致性抽查      最新期间 vs 文档年份：**0 处不一致**
#     真值比对        命中 9 / 舍入相符 4 / 不符 5 —— **与改动前完全一致**
#     needs_review    209 → 209（不变）
#     事实总数        1055 → 1048（期间重新分配导致的去重差异）
#     revenue 可用     78 → 77
#
# 设 `R1_YEAR_SEQ=0` 可关闭（保留这个开关是为了能一键回退做对照）。
_YEAR_SEQ_FALLBACK = os.environ.get("R1_YEAR_SEQ") != "0"


_NOTE_ROW_RE = re.compile(r"^\s*(注|附注|註)\s*[:：]?\s*[（(]?")


def _footnote_row_start(grid: list[list[str]], data_start: int) -> int:
    """数据区里第一行**看起来是脚注**的行号；没有则返回 ``len(grid)``。

    为什么需要它（2026-09 第十四轮实测）：
    601857 p7 的五年摘要表**没有表头行**（``data_start=0``），脚注紧跟在
    数据行之后：

        r0  资本性支出 | 269,089 | 275,849 | 275,393 | 274,494 | 251,323
        r1  经营活动产生的现金流量净额 | 412,510 | …
        r5  注：（1）截至 2021 年 12 月 31 日、2022 年 12 月 31 日、……
        r6  12 月 31 日，每股基本及摊薄盈利是按照……

    脚注被 PDF 拆成一格一格的日期碎片（``12`` ``月`` ``31`` ``日``），
    它们**也是"数字"**，于是 `_data_columns` 把这些列全算成"数据列"：
    实测数据列从应有的 5 列膨胀到 **18 列**，与 5 个年份对不上，
    "按年份序列定期间"因此判据不成立 —— 这个坑正是它要解决的问题本身。

    判据只认**以「注／附注」开头**的行：保守、可解释，不猜。

    ⚠️ 要扫**整行**，不能只看第 0 格：`merge_fragments` 会把
    ``注：（1）截至`` 拆成 ``r5[0]='注：（'``、``r5[1]='1'``、
    ``r5[2]='）截至'`` —— 只看第 0 格虽然能匹配 ``^\\s*注``，
    但换个排版（编号另起一格）就漏了（实测踩过）。
    """
    for i in range(max(0, data_start), len(grid)):
        for cell in grid[i] or []:
            text = _clean_label(cell)
            if not text:
                continue
            if _NOTE_ROW_RE.match(text):
                return i
            # 一行的第 0 格若是文字，就不再往后看 —— 避免把正文里的
            # 「注：」字样误当成脚注起始行
            break
    return len(grid)


def periods_from_year_sequence(
    grid: list[list[str]],
    *,
    data_cols: set[int],
    year_grid: list[list[str]] | None = None,
) -> dict[int, str]:
    """用**表内出现的连续年份序列**确定列 → 期间；判据不成立返回 ``{}``。

    为什么需要它（2026-09 第十四轮）
    -------------------------------
    601857 2025 年报 p7 的**五年摘要表**没有自己的表头，于是
    2025 / 2024 / 2023 三个年份的资本开支被**全部标成同一期间**：

        资本性支出 | 269,089 | 275,849 | 275,393 | 274,494 | 251,323

    三条候选的置信度、口径、单位完全相同，**没有任何信号能把它们分开**；
    R4 只能"分歧即不取"、回退模板值（`capex` 因此一直不是事实值）。

    年份其实印在**同一张表的脚注**里：

        注：（1）截至 2021 年 12 月 31 日、2022 年 12 月 31 日、……
                2025 年 12 月 31 日，每股基本及摊薄盈利是……

    交叉验证（两处独立证据）：

    * p7 表 0 —— 表头**明写** ``2025 年 | 2024 年 | 2023 年 …``，
      数据列**从新到旧**；
    * p7 表 3 —— 脚注是 ``2021 → 2022 → … → 2025``（**升序**），
      而数据列是 ``269,089 → 251,323``（**从新到旧**）。

    所以判据是：**N 个数据列 + 表内出现 N 个连续年份 → 按从新到旧分配。**

    ★ 三条限制（缺一不可，否则返回空、调用方保持原行为）：

    1. **年份个数必须恰好等于数据列数。** 不等说明年份不是给这些列的
       （例如正文提到的报告年度），强行分配必然错位。
    2. **必须构成连续年份**，且每年只出现一次 —— 否则不是"多年摘要"的序列。
    3. **只在列头完全没有期间时使用**（由调用方保证）。列头已写明期间的表
       一律不动：那条路径经过实测验证，不该被本规则打扰。

    ⚠️ **实测边界（第十四轮，如实记录）**：本规则在四份年报的 987 张表上
    命中 **50 张（5.1%）**，且抽查"最新期间 vs 文档年份"**零处不一致**；
    启用前后事实库 1055 → 1057 条、`needs_review` 不变、capex/D&A/revenue
    可用条数不变 —— **即它当前是安全的，但还没有解决 p7 的 capex**。

    原因是那条表 `data_start=0`（首行即数据、没有表头行），`_data_columns`
    把**脚注里的日期碎片列**（``12`` ``月`` ``31`` ``日``）也算成数据列，
    于是"数据列数"是 6 而不是 5，判据 1 不成立。
    已加 `_footnote_row_start` 把脚注行排除（实测数据列 **18 → 6**），
    但脚注**起始行本身**（``注：（ | 1 | ）截至 | …``）仍贡献了一个只出现
    一次的日期列，数据列仍是 6。**要让本规则救下 capex，还差这一步**：
    把"数据列"的定义从"任一数据行出现过数字"收紧为"多数数据行都有数字"。

    返回 ``{列号: "YYYYA"}``；任何一条不满足即返回 ``{}``。

    ★ ``year_grid`` 与 ``data_cols`` **必须来自不同的行区间**（实测踩过）
    ------------------------------------------------------------------
    数据列要排除脚注行（否则脚注里的 ``12`` ``月`` ``31`` 会把列数撑大），
    但**年份恰恰印在脚注里** —— 用"排除脚注后的网格"去扫年份，
    扫到的是空列表，规则永远不成立。

    实测（p7 五年摘要表）：``data_cols`` 用脚注前的行算出 5 列（正确），
    而 ``year_grid`` 传脚注前的行 → 年份 0 个 → 规则返回 ``{}``。
    所以调用方传 ``grid=脚注前的行、year_grid=整表``。
    """
    if not data_cols or not grid:
        return {}
    n_data = len(data_cols)
    if n_data < 2:
        return {}

    years: list[int] = []
    for row in (year_grid if year_grid is not None else grid):
        # ⚠️ 只从**不含数字的行**取年份。
        #
        #   实测踩过（p7 表 0）：那张表的数据区是从**别的表带来的表头**
        #   ``2025 年 | 2024 年 | 2023 年 | 2022 年 | 2021 年`` 加几行数据。
        #   若连表头行一起扫，会得到 2021..2025 五个年份 → 规则按"从新到旧"
        #   派成 2024A..2020A，**整体偏移一年**；而它的真实期间是 2025A..2021A。
        #
        #   年份真正有意义的地方是**标题行与脚注行** —— 那些行不含数字。
        if any(_is_data_number(_clean_label(c)) for c in (row or []) if (c or "").strip()):
            continue
        for cell in row or []:
            for m in _YEAR4_RE.finditer(str(cell)):
                years.append(int(m.group(1)))

    # ★ 用**去重后**的年份个数判等，不是原始出现次数。
    #
    #   实测踩过（p7 表 3，这是本规则要修的正是那张表）：
    #   年份在整表里出现**两次** —— 一次在数据区上方的期间表头行
    #   （``2025 | 2024 | 2023 | 2022 | 2021``），一次在脚注里
    #   （``截至 2021 年 12 月 31 日、…… 2025 年 12 月 31 日``）。
    #   共 10 个，而判据 1 要求"恰好等于数据列数 5" → **永远不成立**，
    #   规则在最需要它的那张表上失效。
    #
    #   两处年份序列**方向相反**（表头从新到旧、脚注从旧到新）但集合相同，
    #   去重后 {2021..2025} 恰好 5 个、连续、无重复 —— 正是我们要的判据。
    uniq = sorted(set(years))
    if len(uniq) != n_data:
        return {}
    if uniq[-1] - uniq[0] != n_data - 1:
        return {}                       # 不连续

    # 从新到旧：最左的数据列 = 最新年份。这是中文财报的固定排版顺序
    # （p7 表 0 的期间表头 2025|2024|2023 与表 3 的取值方向都印证了这一点）。
    ordered = sorted(data_cols)
    return {j: f"{y}A" for j, y in zip(ordered, sorted(uniq, reverse=True))}


def periods_of_columns(
    header: list[str],
    *,
    doc_period: str = "unknown",
    prev_period: str | None = None,
    data_cols: set[int] | None = None,
    grid: list[list[str]] | None = None,
    data_start: int = 0,
    allow_column_order: bool = True,
) -> dict[int, str]:
    """表头标签 → 每列的期间。

    **为什么需要它**：财务报表的利润表 / 现金流量表是**本期与上期并排**：

        项目                | 2025 年度        | 2024 年度
        经营活动产生的现金流量净额 | 246,931,681.41 | 185,187,778.45

    而早期实现把所有列都按*文档级期间*赋值，于是 2024 年那一列被标成 2025A。
    后果不是"少了点数据"，而是**系统性错误**：事实库里每个指标都只有一个期间，
    多期估值（R4 的 DCF）无法启动，同比核查（R5 的 E5）也无从校验。
    这个错误同样不报错 —— 数字看着都对，只是期间标签全错。

    三类写法，按优先级：

    1. **显式年份**：``2025 年度`` → ``2025A``、``2024 年度`` → ``2024A``
    2. **相对写法**：``期末余额`` → 文档期间；``期初余额`` → 上一期
    3. **推断不出** → 不起作用（调用方回退到文档级期间，不制造新错误）

    ``prev_period`` 缺省时由 ``doc_period`` 内部推算 ——
    早期版本要求调用方传，而**有的调用方忘了传**，
    于是 ``期初余额`` 一列静默拿不到期间（单元格被标成无期间）。
    这类"参数可选但忘了就错"的设计本身就是缺陷，所以在内部补上。
    """
    if prev_period is None:
        prev_period = previous_period(doc_period)

    out: dict[int, str] = {}
    for j, label in enumerate(header):
        text = _clean_label(label)
        if not text:
            continue
        m = _YEAR.search(text)
        if m:
            # 表头里可能出现多个年份（如「2025 年 12 月 31 日 / 2024 年 12 月 31 日」
            # 被拼在同一格），取**第一个** —— 当前期总是排在前面。
            #
            # ⚠️ 这里**不能**再写 `if period != prev_period` 之类的守卫：
            #    `2024 年度` 恰好就是上一期，守卫会把它拦下来交给下面的
            #    相对写法分支，而那一列既没有「期末」也没有「期初」字样，
            #    于是**整整一列期间丢失** —— 表现为"比较期数据全没了"，
            #    而数字本身完全正确。实测这个守卫让 2025 年报的 2024 列全部落空。
            out[j] = f"{m.group(1)}{_span_of(text)}"
            continue
        if any(h in text for h in _CURR_END_HINTS):
            if doc_period != "unknown":
                out[j] = doc_period
            continue
        if any(h in text for h in _CURR_BEGIN_HINTS):
            if prev_period != "unknown":
                out[j] = prev_period
            continue

    # ②b **表头跨列传播**（且**只作用于真正承载数据的列**）
    #
    #   实测（2026-09）：601857 2025 年报 p8「主要会计数据」的列头被拆成
    #
    #       r0 [1]2025 [2]年末 [3]2024 [4]年末 [5]年末增减 [6](%) [7]2023 [8]年末
    #
    #   即「2025 年末」横跨 col1–col2。旧实现只在 col1 认出 2025A，
    #   col2 的 `年末` 认不出期间 → 落进列序兜底拿到 2024A，
    #   而 col3 的 `2024` 本就是 2024A → **同一期间出现两个值**。
    #
    #   因此需要把期间传播到片段列。但**必须限定在 `data_cols`（真正有数的列）**：
    #
    #   ⚠️ 这张表**本身是病态的**，必须说明白：
    #
    #       表头行 9 格：项目 | 2025 | 年末 | 2024 | 年末 | 年末增减 | (%) | 2023 | 年末
    #       数据行 5 格：总资产 | 2,828,017 | 2,753,007 | 2.7 | 2,759,237
    #
    #   原列 2 同时承担「2025 年末」的尾词**与**「2024 年末的数值」；
    #   原列 4 同时承担「2024 年末」的尾词**与**「2023 年末的数值」。
    #   **两套列结构互不兼容** —— 任何按表头算出的期间映射在数据行上都不成立。
    #
    #   实测验证过两条错路，都记在这里以免回退：
    #   * 「按相邻显式期间的差值插值」：锚点是 col1=2025/col3=2024/col7=2023，
    #     列距 2、期间距 1 → 步长 0.5，非整数，判据自己就拒绝了。
    #   * 「把片段列并进左列」（扩 `_UNIT_TAIL` 加 `年末`）：会把
    #     `'2,828,017 2,753,007'` 并成一格，把两个不同列的数**糊在一起**。
    #
    #   因此这里的取舍是：**只给"确实有数据、且表头判据能独立支持"的列定期间**，
    #   病态表里那些落在片段位上的数值**没有期间就不入库**（`build_facts` 的
    #   `period or doc_period` 会兜到文档级期间，但那只是兜底，不是错标）。
    #   宁可漏，不可错 —— 一条期间错标会污染 R4 估值与 R5 的比对基准，
    #   而漏一条只是少一个观测。
    for j in range(1, len(header)):
        if j in out:
            continue
        text = _clean_label(header[j])
        if not text or re.match(r"^\d", text) or is_change_column(text):
            continue
        if data_cols is not None and j not in data_cols:
            continue      # 该列不承载数据 → 不传播（避免造出无数据的期间列）
        if j - 1 in out:
            out[j] = out[j - 1]

    # ③ **并排多期的列序推断**（兜底）
    #
    #    中文财报的表格恒为「本期在左、上期在右」。实测中国石油的
    #    「主要会计数据」表把 6–7 期并排，列头只有 `本年比上年增减` 之类
    #    的字样、**没有 20xx 年份**，于是前两条判据全不命中，
    #    两列都落到文档级期间 → 同一物理年份被标成两种期间：
    #
    #        p8  的 234,579(2025) / 255,286(2024) / 253,522(2023) 全标 2025A
    #        p115 的 255,286(2024) / 253,522(2023) / 183,928     全标 2024A
    #
    #    后果（实测）：R4 给 601857 估值时，收入取 p8 的 2025 年数、
    #    营业利润却取到 p8 里标着 2025A 的 **2024 年**数 —— 分子分母跨期，
    #    而两年利润率恰好都接近 8.19%，**错误完全不可见**。
    #
    #    因此：表头判不出期间、且表内恰有两列及以上无期间时，
    #    按列序依次指派 doc_period, prev_period, 再往前一年。
    #
    #    ⚠️ 判据③ 自身踩过三个坑，都是**实测**暴露的，且都不报错：
    #
    #    坑 1：**序号从 0 起算**。`enumerate` 的下标是 `unfilled` 列表里的序号，
    #      不是表格列号。而首列表头通常是空的（`项目`/`类别` 那格），
    #      于是"第一个无期间列"被当成最新期间：
    #          p8 000722 表：col0 空 | 2025 | 2024 | 本年比上年增减 | 2023
    #          → col2（增减列）拿到 base-0 = 2025A，col3（真·2023 年）拿到 2024A。
    #      改成用 `j - unfilled[0]` 起算，并跳过 j=0 的标签列。
    #
    #    坑 2：**把非期间列也算进去**。`本年比上年增减`、`同比`、`变动` 这类列
    #      列的是"两期之差"，不是某一期；给它安一个年份，等于凭空造出一期。
    #      因此先剔除变动率列，再判"是否还有 ≥2 列需要推断"。
    #
    #    坑 3：**把表头片段列当成一期**，以及**把季度列当年报期间**。
    #      `2025 | 年 | 2024 | 年` 里的 `年` 是分列产生的独立列，
    #      若按列序参与推断，会把真正的数据列挤到错误的年份上
    #      （实测：p95 的现金 `206,162` 从 2025A 漂到 2024A）。
    #      季度列（`第一季度`…）更不该被标成 2025A/2024A/2023A —— 那是
    #      "四个季度"被写成"四个年份"，期间形态完全错了。
    #      因此回退只作用于：**确实含数字的列** ∩ **列头是期间词的列**。
    #
    #    坑 4：**假设标签列就是 col0**。见 :func:`_label_column` ——
    #      601857 2024 年报 p8 的科目列在 col1、期间列从 col0 起，
    #      跳过 j=0 会让整行年份错位一格。
    if allow_column_order and doc_period != "unknown" and doc_period.endswith("A"):
        # 没有网格时（单测直接调用）不排除任何列 —— 不假装知道标签列是谁
        label_col = (
            _label_column(grid, data_start, max(len(r) for r in grid)) if grid else -1
        )
        unfilled = [
            j for j in range(len(header))
            if j != label_col
            and j not in out
            and header[j]
            and not is_change_column(header[j])
            and _is_period_label(header[j])
            and (data_cols is None or j in data_cols)
        ]
        if len(unfilled) >= 2:
            year0 = _YEAR.search(doc_period)
            if year0:
                base = int(year0.group(1))
                suffix = doc_period[len(year0.group(1)):]
                for k, j in enumerate(unfilled):
                    out[j] = f"{base - (j - unfilled[0])}{suffix}"
    return out


_CHANGE_COLUMN_WORDS = ("增减", "同比", "变动", "增长率", "增幅", "降幅", "变化")
"""变动率列的列头特征词。

这类列放的是"两期之差"或"两期之比"，**不是某一期的余额或发生额**。
给它按列序安一个年份，等于在事实库里凭空造出一期数据 —— 而它
既不会报错、quote 也能回读（`3.67%` 确实印在表里）。
"""


def is_change_column(label: str) -> bool:
    """列头是否表示"变动率/变动额"而非某一期间。"""
    t = _clean_label(label or "")
    return any(w in t for w in _CHANGE_COLUMN_WORDS)


_PERIOD_COLUMN_WORDS = (
    "本期", "上期", "本年", "上年", "去年", "同期", "期末", "期初",
    "年末", "年初", "余额", "年度", "全年", "当期", "前期", "本报告期",
)
"""可以**按列序推断年份**的列头词。

判据③ 是"所有列头都判不出期间"时的兜底，它的前提假设是
"这些列就是按期间排列的"。列头里出现这些词，才说明该列确实是一个期间；
否则（`EBITDA`、`油气和新能源`、`第一季度`）按列序派年份就是无依据的臆造。
"""


def _is_period_label(label: str) -> bool:
    """列头是否为一个**期间列**（可参与列序推断）。"""
    t = _clean_label(label or "")
    return any(w in t for w in _PERIOD_COLUMN_WORDS)


def _span_of(text: str) -> str:
    """从列头文本里取期间形态后缀（``2025 年度`` → ``A``）。

    ⚠️ 这里踩过一个**静默失效**的坑：原先写成一个正则
    ``(20\\d{2})\\s*年?\\s*(年度|半年|…)?``，而 ``"2025 年度"`` 里
    年份与「年度」之间**有一个空格**，``年?`` 匹配不上，于是 ``年度``
    被可选组直接跳过 —— 期间形态永远是 ``A``，而 ``2025 年度`` 之外的
    写法（半年报的 ``2025 年半年度``、季报的 ``前三季度``）全部退化成 ``A``。
    拆成"先取年份、再找形态词"两步才稳。
    """
    for key, suffix in (
        ("前三季度", "Q3"), ("半年度", "H1"), ("半年", "H1"),
        ("一季度", "Q1"), ("三季度", "Q3"),
        ("年度", "A"), ("全年", "A"),
    ):
        if key in text:
            return suffix
    return "A"


def previous_period(period: str) -> str:
    """由 ``2025A`` 推出 ``2024A``。推不出返回 ``unknown``。

    只处理年报（``A``）：季报/半年报的"上期"语义不同（上年同期 vs 上季度），
    在没有明确标注时不猜。
    """
    m = re.fullmatch(r"(20\d{2})A", period or "")
    if not m:
        return "unknown"
    return f"{int(m.group(1)) - 1}A"

_MIN_SUBJECT_LEN = 3
"""主体名的最小长度。取 3 而不是 4：中文公司名/人名可以只有三个字
（「张三丰」「国泰君安」是四字，但「王小明」是三字）。
再短就与「序号」「合计」「其中」这类表内标签混淆了。"""


_STATEMENT_ROW_WORDS = (
    "营业收入", "营业成本", "营业总收入", "营业总成本", "净利润", "利润总额",
    "货币资金", "应收账款", "存货", "资产总计", "资产合计", "负债合计",
    "所有者权益", "股东权益", "经营活动", "投资活动", "筹资活动", "现金流量",
    "流动资产", "非流动资产", "流动负债", "非流动负债", "实收资本", "资本公积",
    "盈余公积", "未分配利润", "长期借款", "短期借款", "研发费用", "管理费用",
    "销售费用", "财务费用", "所得税", "营业利润",
)


def looks_like_statement_table(
    grid: list[list[str]], header: list[str], data_start: int
) -> bool:
    """像不像**财务报表**（而不是公告里的明细表）。

    判据是结构性的，与具体公司无关：

    1. 第 0 列表头是「项目 / 科目 / 项 目」这类**行标题**（不是"公司名称"）
    2. 数据区里**至少一列**含数字（2 行以上）
    3. 数据行的行标签是**指标名**（命中别名）

    为什么不能只靠"选不出主体列"：``pick_subject_column`` 的多样性兜底
    总能在报表里挑出一列（行标签列取值最分散），于是主体列"选得出"，
    结果是把「一、营业总收入」这类科目名当成了主体名。
    实测就是这个连锁反应：一份报表的数字被拆散到几十个伪主体名下，
    而且不报错。
    """
    if not grid or not header:
        return False
    h0 = _clean_label(header[0])
    # ★ 首格是「行标题」即可，**不要求恰好含「项目」两字**。
    #
    #   实测（2026-09，601857 2025 年报 p7「主要财务数据」）：该表表头被拆成
    #   ``项 | 目 | 2025 | 年 | 2024 | 年 | …``，首格只有 ``项`` 一个字，
    #   而 ``目`` 落在第 2 格。旧判据要求首格含「项目」→ 判定不是报表 →
    #   走列导向路径 → ``metric_columns=0`` → **整表 0 条候选**。
    #   而这张表恰恰是"科目在行、期间在列"的典型形态，本该走行导向。
    #
    #   改用 `_is_label_row` 的判据（首格是短汉字标签，允许被拆字/粘数字），
    #   它本来就是为这种碎片化表头写的。
    if not any(k in h0 for k in ("项目", "科目")) and _is_label_row(header) < 0:
        return False
    if len(h0) > 8:  # 「项目」列里被拼进了指标名 → 表头没切干净，不敢认
        return False

    n_cols = max((len(r) for r in grid), default=0)
    numeric_cols = 0
    for j in range(1, n_cols):
        hits = sum(
            1 for i in range(data_start, len(grid))
            if j < len(grid[i]) and _count_numbers([grid[i][j]]) >= 1
        )
        if hits >= 2:
            numeric_cols += 1
    if numeric_cols < 1:
        return False

    label_hits = 0
    for i in range(data_start, min(len(grid), data_start + 12)):
        if grid[i]:
            t = _clean_label(grid[i][0])
            if t and any(a in t for a in _STATEMENT_ROW_WORDS):
                label_hits += 1
    return label_hits >= 1


_STATEMENT_ROW_WORDS = (
    "营业收入", "营业成本", "营业总收入", "营业总成本", "净利润", "利润总额",
    "货币资金", "应收账款", "存货", "资产总计", "资产合计", "负债合计",
    "所有者权益", "股东权益", "经营活动", "投资活动", "筹资活动", "现金流量",
    "流动资产", "非流动资产", "流动负债", "非流动负债", "实收资本", "资本公积",
    "盈余公积", "未分配利润", "长期借款", "短期借款", "研发费用", "管理费用",
    "销售费用", "财务费用", "所得税", "营业利润",
)


_STATEMENT_EXCLUDES = ("流动", "非流动", "其他流动", "其他非流动", "其中", "递延", "应收", "应付")

_NEGATED_LABEL_RE = re.compile(r"息税.{0,6}前|EBITDA|EBIT")
"""**否定式标签**的正则 —— 命中则整条不映射。

★ 2026-09 实测（601857 2025 年报 p95「主要财务指标」）：
中国石油的 EBITDA 行写作 ``息税折旧摊销前利润（EBITDA）（人民币百万元）``，
其中恰好含子串「折旧摊销」，于是被 ``depreciation_amortization`` 的别名
``折旧摊销`` 命中 —— 而 EBITDA 是**扣除折旧摊销之前**的利润，
语义与"折旧摊销费用"**相反**。

实测后果：事实库里 ``depreciation_amortization`` 出现
``49,356,400,000,000``（= 493,564 百万元，EBITDA）这种量级大 200 倍的值，
且被标成 2023A/2024A —— 而真正的折旧摊销是 ``215,818``（百万元）。

⚠️ **为什么必须用正则，不能写字面词**（两次实测失败）：

1. 写 ``息税折旧摊销前`` → **``折旧与摊销`` 也被拦掉**。
   两个串共享连续子串 ``折旧摊``（``折旧与摊销`` 中间插了 ``与``，
   但 ``折旧摊`` 三字仍连续），而 ``息税折旧摊销前`` 整体就是
   ``息税`` + ``折旧摊`` + ``销前`` 的组合 —— 互相包含对方的片段。
2. 改写字面 ``息税前`` → **``息税折旧摊销前利润`` 反而不被拦**
   （它不含连续的 ``息税前``，中间隔着 ``折旧摊销`` 三字）。

``息税.{0,6}前`` 同时满足两条：必然命中所有"息税…前"的写法，
且不会命中 ``折旧与摊销``（它的 ``息税`` 后面没有 ``前``）。
"""

# 「合计/总计/净额/小计」在中文报表里是**上位合计**，通常才是规范指标；
# 但带「流动/非流动」限定的合计不是。见 metric_of_row_label 的说明。
_TOTAL_MARKERS = ("合计", "总计", "净额")

_PER_SHARE_TAIL = ("元", "股", "人民币元")


def metric_of_row_label(
    label: str, alias_map: dict[str, str], *, metric_units: dict[str, str] | None = None
) -> str:
    """行标签 → 指标。取**最长别名**匹配，避免「净利润」抢走「归母净利润」。

    三个必须的处理（都是实测踩出来的）：

    1. **剪掉层级前缀**：``一、营业总收入`` → ``营业总收入``；
       ``其中：营业收入`` → ``营业收入``；``减：所得税费用`` → ``所得税费用``
    2. **拒绝错配**：``流动资产合计`` 里的子串 ``资产合计`` 会命中
       ``total_assets``，于是**资产总计那一行反而取不到值** ——
       实测 000722 的 total_assets 变成了"流动资产合计 15.4 亿"，
       而真值是 41.6 亿。带有「流动/非流动/其中」等限定的行**一律不参与**
       ``total_*`` / ``equity_*`` 这类上位指标的映射。
    3. **单位写在名字里要剪掉**：``基本每股收益（人民币元）`` 保留会让
       别名 ``基本每股收益`` 仍能命中，但 ``元`` 会被当成数值单位干扰解析，
       故一并清除
    """
    t = _clean_label(label or "")
    if not t:
        return ""
    t = re.sub(r"^[（(]?[一二三四五六七八九十\d]+[）)、.．]\s*", "", t)
    t = re.sub(r"^(其中|减|加)\s*[:：]?\s*", "", t)
    t = re.sub(r"[（(][^）)]*[）)]\s*$", "", t)
    t = t.lstrip(":：").strip()
    if not t:
        return ""

    best, best_len = "", 0
    for alias, canonical in alias_map.items():
        if len(alias) <= best_len or alias not in t:
            continue
        # ★ 否定式标签守卫（对所有指标生效，不限于上位指标）。
        #
        #   实测（601857 p95）：``息税折旧摊销前利润（EBITDA）`` 含子串「折旧摊销」，
        #   会把 EBITDA 映射成"折旧摊销费用" —— **语义正好相反**。
        #   实测后果：事实库里该指标出现 493,564 百万元（EBITDA）这种
        #   量级大 200 倍的值，而真值是 215,818 百万元。
        if _NEGATED_LABEL_RE.search(t):
            continue
        if canonical in _UPPER_LEVEL_METRICS:
            j = t.find(alias)
            before = t[max(0, j - 4):j]
            if any(x in before for x in _STATEMENT_EXCLUDES):
                continue
        best, best_len = canonical, len(alias)

    # 精确优先：行标签里带「合计/总计/净额」的，若它自己就是别名则直接用
    return best


_UPPER_LEVEL_METRICS = {
    "total_assets", "total_liabilities", "equity_total", "equity_parent",
    "net_profit_total", "net_profit_parent", "revenue", "operating_cost",
    "operating_profit", "total_profit", "ocf_net", "operating_cash_flow",
}


def _segment_scope(col: int, seg_total_cols: Any) -> str:
    """按**取值落在哪一列**定分部表的口径；不是分部表返回 ``""``。

    这是"合计列"这个信息唯一的用处：同一张分部报告表里，
    合计列的取值是**集团口径**，分部列的取值是**板块口径**，
    两者在经济含义上完全不同，绝不能当成同一个读数的两个观测。

    ``seg_total_cols`` 是**合计列的集合**（可为 int / set / frozenset / None）——
    两层列头的分部表会有多个合计列（实测 002716 的
    「合计营业收入 / 合计营业成本」），它们**同属集团口径**。

    返回 ``""`` 时调用方回退到 ``doc_scope``（即原来的行为）。
    """
    if seg_total_cols is None:
        return ""
    if isinstance(seg_total_cols, int):
        if seg_total_cols < 0:          # 继承值为"无"时的哨兵
            return ""
        return "segment_total" if col == seg_total_cols else "segment_detail"
    cols = set(seg_total_cols)
    if not cols:
        return ""
    return "segment_total" if col in cols else "segment_detail"


def _is_row_oriented(
    grid: list[list[str]],
    header: list[str],
    data_start: int,
    carry_header: list[str] | None,
    alias_map: dict[str, str],
) -> bool:
    """该不该走**行导向**（财务报表）路径。

    三种情形：

    1. 结构性判据命中（第 0 列表头是「项目」+ 多列数字 + 行标签是指标名）
    2. **续页**：调用方带来了上一页的报表表头（``carry_header``），
       且本页确实有多列数字 —— 续页往往没有表头，但**合计行就在续页上**，
       不认就会丢掉「资产总计」「负债合计」「所有者权益合计」
    3. 其余 → 列导向（公告表）

    ★ 情形 4（2026-09 第十三轮新增）：``data_start == 0`` 且首列有值 ——
    说明**这张表根本没有表头行**（表头印在同一文档的上一张表里），
    于是 ``header`` 全空、情形 1 必然不成立。实测 601857 p178 表 1：
    它是分部报告的第二张表，行是 ``折旧、折耗和摊销费用 / 资产减值损失 /
    资本性支出 / 分部资产``，列是各分部 + 合计 —— **资本开支真值
    269,089 就在这张表里**，而旧判据让它 0 条候选。

    判据用"数据区有多少列是数字"（与情形 2 同一条），另加"首列非空"，
    避免把纯数据块当成报表。

    ``alias_map``（别名 → 规范名）由**调用方传进来**，本函数不自己读配置。
    原先 ``_has_metric_row_labels`` 就近读了一次 ``configs/metrics.yaml``
    并进程级缓存 —— 那次读不经过 ``ctx.guard()``，于是在
    ``trace.jsonl`` 里没有 ``file_access`` 记录，而"完整记录文件访问"
    是本项目的硬要求。调用链上 ``alias_map`` 本来就是现成的参数
    （:func:`extract_table_facts` 的入参），传下来即可，代价为零。
    """
    if looks_like_statement_table(grid, header, data_start):
        return True
    if not carry_header:
        # 没有表头行、也没有继承表头 → 仍然可能是"表头在上一张表"的续表
        if data_start == 0 and _has_metric_row_labels(grid, alias_map):
            return True
        return False
    n_cols = max((len(r) for r in grid), default=0)
    numeric_cols = sum(
        1
        for j in range(n_cols)
        if sum(
            1 for i in range(min(data_start, len(grid)), len(grid))
            if j < len(grid[i]) and _count_numbers([grid[i][j]]) >= 1
        ) >= 2
    )
    return numeric_cols >= 2


def _has_metric_row_labels(
    grid: list[list[str]], alias_map: dict[str, str], *, probe: int = 12
) -> bool:
    """首列里是否出现**科目名**形态的行标签。

    用于"这张表没有表头行"时的兜底判据（见 :func:`_is_row_oriented` 情形 4）。
    判据刻意保守：首列有多行是中文文字（非纯数字、非空、长度 ≥2），
    且这些行里**至少一个**能映射到已知指标别名。

    为什么要求"能映射到指标"：`extract_statement_facts` 只对能映射出行标签
    的行产出候选，若一行都映射不上，走这条路等于白走，不如让列导向去处理。

    ``alias_map``（别名 → 规范名）由调用方传入（见 :func:`_is_row_oriented`）：
    上游本来就有这份参数，传下来即可。原先是就地读一次
    ``configs/metrics.yaml`` 再进程级缓存 —— 那次读绕开了 ``ctx.guard()``，
    ``trace.jsonl`` 里没有对应记录，而"完整记录文件访问"是硬要求。
    """
    if not grid:
        return False
    if not alias_map:
        return False
    hits = 0
    for row in grid[:probe]:
        if not row:
            continue
        label = _clean_label(row[0])
        if len(label) < 2 or _count_numbers([label]) >= 1:
            continue
        if metric_of_row_label(label, alias_map):
            hits += 1
    return hits >= 1


def extract_statement_facts(
    grid: list[list[str]],
    *,
    header: list[str],
    data_start: int,
    alias_map: dict[str, str],
    metric_units: dict[str, str],
    unit_hint: str,
    page_text: str,
    page_no: int,
    doc_entity: str,
    doc_period: str,
    doc_scope: str = "consolidated",
    row_offset: int = 0,
    max_rows: int = 300,
    header_rows: list[list[str]] | None = None,
    seg_total_col: Any = None,
    period_hint: str = "",
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """**行导向**抽取：财务报表（科目在行、期间在列）。

    为什么必须是独立的一条路径
    --------------------------
    :func:`extract_table_facts` 是**列导向**的：它假设「指标是列头、主体是行标签」，
    这在公告表上成立（``被担保公司名称 | 注册资本`` × 一行一家公司）。
    财务报表正好相反：

        项目                    | 2025 年度        | 2024 年度
        一、营业总收入           | 349,165,168.51  | 336,788,863.52
        其中：营业成本           | 208,717,602.54  | 185,653,959.78

    指标名在**行标签**里，期间在**列头**里，而且**没有主体列**。
    列导向路径在报表上的表现是 ``metric_columns=0`` → 直接返回空，
    这就是"943 页年报只抽出 241 条事实"的主要来源。

    三个必须的约束（与列导向路径一致，宁缺勿错）：

    1. **单位必须明确**：单元格自带 > 本列表头 > 整表/页面声明，都没有则不入库
    2. **量纲族必须与指标声明一致**（指标单位未知时不设限）
    3. **主体是文档主体**，不是行标签 —— 行标签是科目名
    """
    stats = {
        "statement_rows": 0,
        "statement_candidates": 0,
        "skipped_no_metric": 0,
        "skipped_no_unit": 0,
        "skipped_bad_value": 0,
        "period_from_header": 0,
    }
    if not grid:
        return [], stats

    col_period = periods_from_header_rows(
        # 优先用**原始表头行**（多行，含片段列）；调用方未提供时退回单行 header。
        # 多行是必要的：期间常被 PDF 拆成 `2025 | 年末`，而数值落在 `年末` 那列。
        header_rows if header_rows else [header],
        doc_period=doc_period,
        data_cols=_data_columns(grid, data_start, max((len(r) for r in grid), default=0)),
        grid=grid,
        data_start=data_start,
    )
    stats["period_from_header"] = len(col_period)
    # 本表**自己声明**的期间（见 :func:`table_period_hint`）。
    #
    # ⚠️ 不能只写进 `col_period`：分部报告的列头是分部名，`col_period` 是
    #    **空字典**，往空字典里塞键塞不进去（实测踩过 —— 写了覆盖逻辑但
    #    一列都没覆盖到，period 仍为空）。因此这里只记统计，
    #    真正的取值在候选构造处按 `period_hint` 兜底。
    if period_hint:
        stats["period_from_table_body"] = 1
    # 没有列头期间时，表内至少要有两列数字，否则退回列导向路径处理更合适
    if not col_period and max((len(r) for r in grid), default=0) < 2:
        return [], stats

    # ★ 列头**没有完整给出**期间时，用表内出现的连续年份序列兜底
    #   （见 periods_from_year_sequence 的说明）。
    #
    #   ⚠️ 判据是"**有没有覆盖到全部数据列**"，不是"是不是空字典"。
    #
    #   实测踩过（这就是 capex 一直救不回来的原因）：p7 五年摘要表从
    #   上一张表继承了表头，`periods_from_header_rows` 于是**只给了一部分列**
    #   期间 —— capex 候选的 period 实测是混合的：
    #
    #       capex 269,089,000,000 period=''
    #       capex 275,849,000,000 period='2025A'
    #       capex 275,393,000,000 period='2025A'
    #       capex 274,494,000,000 period='2024A'
    #
    #   "只给了一部分"正是这个缺陷的**症状**（部分列的期间是从别的表
    #   误搬过来的），而旧守卫 `if not col_period` 把它当成了"已经处理过"，
    #   于是规则永远不介入。
    #
    #   ⚠️ 但**不能**只看"是否覆盖全部数据列"—— 那会产生附带损害：
    #   p7 表 0/1/2 的期间是**完整且正确**的，只因为"年份数恰好等于列数"
    #   就会被规则覆盖（实测：改宽守卫后这三张表都被覆盖了）。
    #
    #   ★ 最终判据：**同一个期间落到了两个不同的数据列上**（期间碰撞）。
    #
    #   两个不同的列代表两个不同的年份，它们**不可能**共享一个期间 ——
    #   出现碰撞就说明"列 → 期间"的映射是坏的，而不是"已经处理过了"。
    #   实测目标表（p7 表 3）就是这个形态：
    #
    #       col1 269,089  period=''         ← 没拿到期间
    #       col2 275,849  period='2025A'    ┐ 同一期间出现两次
    #       col3 275,393  period='2025A'    ┘
    #       col4 274,494  period='2024A'    ┐ 同一期间出现两次
    #       col5 251,323  period='2024A'    ┘
    #
    #   而 p7 表 0/1/2 每个数据列各有一个**互不重复**的期间 → 不碰撞 → 不动。
    #   "碰撞"比"缺列"更精确：它只在本规则**要修的那种表**上成立。
    _seq_cols = _majority_data_columns(
        grid[: _footnote_row_start(grid, data_start)], data_start,
        max((len(r) for r in grid), default=0),
    )
    _used = [col_period[j] for j in sorted(_seq_cols) if col_period.get(j)]
    _collision = len(_used) != len(set(_used))
    if _YEAR_SEQ_FALLBACK and _collision:
        # 只把**脚注之前**的行当作数据区 —— 脚注里的日期碎片也是"数字"，
        # 会把数据列的列数撑大（实测 5 → 18），让判据 1 不成立。
        note_at = _footnote_row_start(grid, data_start)
        body = grid[:note_at]
        seq = periods_from_year_sequence(
            body,
            # ★ 用**严格版**的数据列判据：`_data_columns` 会把脚注里的
            #   日期碎片列也算进来（实测 5 → 6 列），而本规则的判据 1 要求
            #   "年份数恰好等于数据列数"，多一列就永远不成立 ——
            #   那正是它要解决的问题本身。
            data_cols=_majority_data_columns(
                body, data_start, max((len(r) for r in body), default=0)
            ),
            # ★ 年份**在脚注里**，所以扫年份要用整表，不能用排除脚注后的 body。
            #   两者混用会让规则永远扫不到年份（实测：数据列 5 个、年份 0 个）。
            year_grid=grid,
        )
        if seq:
            col_period = seq
            stats["period_from_year_sequence"] = len(seq)
            stats["period_replaced"] = 1

    n_cols = max((len(r) for r in grid), default=0)
    taken: set[tuple[str, int]] = set()   # (metric, col) 已取过 —— 长表里有父子行重复
    out: list[dict[str, Any]] = []

    for i in range(data_start, min(len(grid), data_start + max_rows)):
        row = grid[i]
        if not row:
            continue
        label = row[0] if row else ""
        metric = metric_of_row_label(label, alias_map)
        if not metric:
            stats["skipped_no_metric"] += 1
            continue
        for j in range(1, n_cols):
            if j >= len(row):
                continue
            raw = _clean_label(row[j])
            if not raw or not re.search(r"\d", raw):
                continue

            m_unit = _UNIT_IN_CELL.search(raw)
            unit = normalize_unit(m_unit.group(1)) if m_unit else ""
            number_text = raw[: m_unit.start()].strip() if m_unit else raw
            unit_source = "cell"
            # ★ 行标签里的单位（见 extract_table_facts 里的同款说明）：
            #   中国石油年报 p95「主要财务指标」表把单位写在行标签里，
            #   漏看会让量纲错 100 倍（实测：现金被抽成 20.6 万亿）。
            row_label_unit = unit_in_label(label) if label else ""
            if not unit and row_label_unit:
                unit = row_label_unit
                unit_source = "row_label"
            if not unit:
                unit = unit_in_label(header[j]) if j < len(header) else ""
                unit_source = "column_header"
            if not unit:
                unit = unit_hint or find_unit_declaration(page_text)
                unit_source = "table"
            if not unit:
                stats["skipped_no_unit"] += 1
                continue

            want = expected_family(metric_units.get(metric, ""))
            got = unit_family(unit)
            if want not in ("", "other") and got != "other" and want != got:
                stats["skipped_bad_value"] += 1
                continue

            if not _NUMERIC_CELL.match(number_text):
                stats["skipped_bad_value"] += 1
                continue
            value = parse_decimal(number_text)
            if value is None:
                stats["skipped_bad_value"] += 1
                continue

            try:
                if got == "share":
                    value_std, target = _to_shares(value, unit), "股"
                elif got == "ratio":
                    value_std, target = value, "%"
                elif got == "per_share":
                    value_std, target = value, "元每股"
                elif unit == "倍":
                    value_std, target = value, "倍"
                else:
                    value_std, target = to_yuan(value, unit), "元"
            except Exception:
                stats["skipped_bad_value"] += 1
                continue

            key = (metric, j)
            if key in taken:
                # 同一列同一指标已被更靠上的行取走（如「营业总收入」与
                # 「其中：营业收入」都映射到 revenue）。取靠上的那个 ——
                # 中文报表里上位科目在前，下位是明细。
                continue
            taken.add(key)

            stats["statement_candidates"] += 1
            out.append({
                "metric": metric,
                "entity": doc_entity,
                "value": value_std,
                "unit": target,
                "raw_text": raw,
                "quote": raw,
                "row": i + row_offset,
                "col": j,
                "page": page_no,
                "unit_source": unit_source,
                # 列头期间优先；取不到时用**本表自己声明的期间**
                # （分部报告：年份印在表体内，列头只有分部名 —— 见 table_period_hint）
                "period": col_period.get(j, "") or period_hint,
                "row_label": _clean_label(label),
                # ★ 口径按**取值落在哪一列**定（2026-09 第十三轮）。
                #
                #   分部报告的列头是「油气和新能源 | … | 合计」，不是期间。
                #   补齐这一条之前，合计列的 269,089 与分部列的 205,094
                #   被标成同一主体同一期间，触发"同表多口径冲突"→ 全部
                #   needs_review → 集团资本开支取不出来（第十一轮实测）。
                #
                #   现在：落在合计列 → segment_total（集团口径），
                #         落在其它分部列 → segment_detail（板块口径）。
                #   两者 caliber_key 不同，冲突守卫不再把它们配成一对。
                "scope": (
                    _segment_scope(j, seg_total_col) or doc_scope
                ),
                "suspect": truncation_suspect(raw, value_std, target),
            })
    stats["statement_rows"] = len(grid) - data_start
    # 同表内的量纲离群：同一指标的数值数量级差 10³ 以上 → 标记可疑
    by_metric: dict[str, list[tuple[int, Decimal]]] = {}
    for idx, c in enumerate(out):
        by_metric.setdefault(c["metric"], []).append((idx, c["value"]))
    for metric, items in by_metric.items():
        for idx in unit_scale_outliers(items):
            if not out[idx].get("suspect"):
                out[idx]["suspect"] = (
                    f"同表内 {metric} 的数量级与其他行相差 10³ 以上，疑似单位/量纲错误"
                )
    return out, stats

# ==========================================================================
# 碎片合并：PDF 把一格拆成多格
# ==========================================================================

_UNIT_TAIL = ("年", "月", "日", "季度", "%", "％", "股", "元", "万", "亿", "倍", "期")
_PAREN_OR_PUNCT = re.compile(r"^[（）()\[\]【】\s.,，、:：;；\-—/]+$")
_SHORT_CJK = re.compile(r"^[\u4e00-\u9fa5]{1,3}$")


def _has_digits(text: str) -> bool:
    return bool(text) and bool(_NUMBERISH.search(text))


def _is_footnote_fragment(text: str) -> bool:
    """像不像**脚注编号**而不是数值：全是括号/标点、没有数字。

    实测（中国石油年报）：期间行是
    ``['2024','年','2023','年','(2)','2022','年',…]`` ——
    其中 ``'(2)'`` 被拆成 ``'('`` + ``'2'`` + ``')'`` 三格。
    它夹在期间列之间会**把后面所有列错开一位**。
    """
    t = (text or "").strip()
    return bool(t) and not _has_digits(t) and bool(_PAREN_OR_PUNCT.match(t))


class MergedGrid:
    """碎片合并的结果。

    ``dropped_cols`` 是**被判定为脚注编号而删除的原始列号**。
    它必须被带到后续步骤：删除列会让列号整体左移，而列头与数据行
    **必须按同一套列号解释** —— 否则期间会错位。
    """

    __slots__ = ("grid", "col_maps", "dropped_cols")

    def __init__(
        self,
        grid: list[list[str]],
        col_maps: list[list[int]],
        dropped_cols: list[int],
    ) -> None:
        self.grid = grid
        self.col_maps = col_maps
        self.dropped_cols = dropped_cols


def _header_row_bounds(grid: list[list[str]], *, probe_rows: int = 3) -> tuple[list[tuple[int, int]], int]:
    """在表头区推断**列边界**：把 ``2024 | 年`` 这类拆开的格认成一组。

    返回 ``(groups, data_start_probe)``，``groups`` 是 ``(起列, 止列)`` 列表。

    **为什么必须先定列结构**：如果每行各自合并，同一列在不同行的边界就会
    不一致 —— 表头合并了 ``2024``+``年`` 占 2 列，数据行的 ``2,937,981``
    只占 1 列，于是**表头与数据的列号对不上**，期间全部错位。
    实测这个坑让中国石油的期间列整列偏移。

    判据只看表头区（前几行）：数字格后面紧跟单位尾字（``年``/``月``/``股``…）
    就并成一组；连续的单字汉字也并成一组。
    """
    if not grid:
        return [], 0
    n_cols = max((len(r) for r in grid), default=0)
    probe = grid[: min(probe_rows, len(grid))]

    # 逐列判断"是否与右边一列属于同一视觉单元格"
    merge_right: set[int] = set()
    for j in range(n_cols - 1):
        texts = [
            (r[j] or "").strip()
            for r in probe
            if j < len(r) and (r[j] or "").strip()
        ]
        rights = [
            (r[j + 1] or "").strip()
            for r in probe
            if j + 1 < len(r) and (r[j + 1] or "").strip()
        ]
        if not texts or not rights:
            continue
        # ① 数字格 + 单位尾字
        if all(re.fullmatch(r"-?[\d,]+(?:\.\d+)?", t) for t in texts) and all(
            t in _UNIT_TAIL for t in rights
        ):
            merge_right.add(j)
            continue
        # ② 短汉字 + 短汉字
        if all(_SHORT_CJK.match(t) for t in texts) and all(
            _SHORT_CJK.match(t) for t in rights
        ):
            merge_right.add(j)

    groups: list[tuple[int, int]] = []
    j = 0
    while j < n_cols:
        end = j
        while end in merge_right:
            end += 1
        groups.append((j, end))
        j = end + 1
    return groups, len(probe)


def _footnote_columns(grid: list[list[str]], *, scan_rows: int = 3) -> set[int]:
    """表头区里**疑似脚注编号**的列号。

    判据（逐列，只看表头区最前面几行）：

    1. 该列所有非空格都是**短数字**（≤3 位）、短括号片段或标点
    2. **不含**年份（``20xx``）或 4 位以上数字
    3. 该列至少在**两行**里出现过内容 —— 只出现一次的多半是数据，
       不是脚注标号

    实测（中国石油年报 p7）::

        ['项','目','2024','年','2023','年','(','2',')','2022','年', …]

    第 6–8 列就是拆碎的脚注 ``(2)``。它们混在期间列之间，
    会让后面所有列**整体错位一位** —— 而 ``(2)`` 里的 ``2`` 是个
    "看起来很正常"的数字，不专门判就一定会留下来占位。

    ⚠️ 判据必须保守：**误删一列会毁掉整张表**（数据与期间错位），
    而漏判只是少抽几条。因此三重条件缺一不可。

    还要排除**已被列结构合并的列**：``(`` ``2`` ``)`` 三列在表头里
    都只出现一次时不该整列删除，而应由列结构处理（它们不在任何组里）。
    """
    if not grid:
        return set()
    n_cols = max((len(r) for r in grid), default=0)
    header_rows = grid[: min(scan_rows, len(grid))]
    out: set[int] = set()
    for j in range(n_cols):
        texts = [
            (r[j] or "").strip()
            for r in header_rows
            if j < len(r) and (r[j] or "").strip()
        ]
        if len(texts) < 2:
            continue
        if any(re.search(r"20\d{2}", t) or re.search(r"\d{4,}", t) for t in texts):
            continue
        if all(
            len(t) <= 3
            and (
                t.isdigit()
                or t in _UNIT_TAIL
                or (not _has_digits(t) and _PAREN_OR_PUNCT.match(t))
            )
            for t in texts
        ):
            out.add(j)
    return out


def merge_fragments(grid: list[list[str]]) -> MergedGrid:
    """按**统一的列结构**合并被 PDF 拆碎的格，并剔除脚注编号列。

    **为什么必须做**：本项目的 PDF 后端给出的是**按坐标逐格**的文本，
    同一视觉单元格可能被拆成多格。实测三种高频形态：

    * 期间表头：``2024 | 年 | 2023 | 年``（"2024 年" 被拆成两格）
    * 单字标签：``项 | 目 | 附 | 注``
    * 脚注编号：``(`` ``2`` ``)`` 占了三列，把后面所有列挤偏

    不适配的后果是"表头识别不出来"：表头行既没有 ``项目`` 这样的行标题，
    也凑不出期间列，于是整表被判成"没有指标列"而丢弃 ——
    中国石油 273 页在修复前**一个数字都没抽出来**，症状看起来像"这家公司没披露"。

    做法：先在表头区推断一次列边界（:func:`_header_row_bounds`），
    再把**同一套边界**应用到每一行。逐行各自合并是错的 ——
    表头合并占 2 列、数据只占 1 列，列号就对不上了。

    原列号会被保留（``col_maps``），因为候选事实要锚定到**原始单元格**拿 bbox。
    """
    dropped = _footnote_columns(grid)
    groups, _ = _header_row_bounds(grid)
    live = [(a, b) for (a, b) in groups if a not in dropped]

    new_grid: list[list[str]] = []
    col_maps: list[list[int]] = []
    for row in grid:
        cells: list[str] = []
        maps: list[list[int]] = []
        for a, b in live:
            parts: list[str] = []
            idxs: list[int] = []
            for j in range(a, min(b + 1, len(row))):
                if j in dropped:
                    continue
                t = (row[j] or "").strip()
                if not t:
                    continue
                parts.append(t)
                idxs.append(j)
            # ★ 空列也要**占位**，绝不能 `continue` 丢掉 ——
            #   丢掉会让**每一行的列数不同**，列号语义在表头与数据之间错位
            #   （表头 9 列、数据行 5 列，同一个 `col_period[j]` 指向不同原列）。
            #   实测：601857 p8 的 2,753,007（真值 2024 年末）被标成 2025A，
            #   与 2,828,017（真值 2025 年末）撞进同一期间，
            #   而 R5 的表格列期核查正是拿这个自相矛盾的基准去比对的。
            text = ""
            for p in parts:
                if text and not (_is_cjk(text[-1]) or _is_cjk(p[0])):
                    text += " "
                text += p
            cells.append(text)
            maps.append(idxs)
        new_grid.append(cells)
        col_maps.append(maps)
    return MergedGrid(new_grid, col_maps, sorted(dropped))


def _is_label_row(row: list[str]) -> int:
    """这一行是不是**行标题行**（``项目 | 2025 年度 | 2024 年度``）。

    返回第 0 格内容的长度（找不到返回 -1）。

    判据故意放宽：第 0 格是**短的纯汉字标签**即可（允许尾部粘上数字 ——
    碎片合并会把 ``项`` ``目`` ``2025`` ``年`` 并成 ``项目2025年``），
    不要求文字恰好是「项目」。
    """
    if not row:
        return -1
    t = _clean_label(row[0])
    if not t:
        return -1
    # 允许「项目2025年」这种合并结果
    t = re.sub(r"\d{4}\s*年?", "", t).strip()
    if not t:
        return -1
    if len(t) <= 4 and _SHORT_CJK.match(t):
        return len(t)
    return -1


def data_start_row(grid: list[list[str]], *, max_scan: int = 8) -> int:
    """数据起始行 = **第一个含数字的行**（它上面全是表头）。

    ⚠️ 判据演进过三次，前两次都错在不同方向：

    1. "取前两行当表头" —— 中文财报的表头常有 3–4 行（标题 / 分组 /
       单位声明 / 指标行），指标名根本没进表头，48 张表只抽出 11 条事实。
    2. "取命中指标别名最多的那一行" —— 在公告表上有效，但在**财务报表**上
       反向失效：报表的每一行标签都是指标名，数据行的命中数超过真表头，
       于是 ``data_start`` 被推到第 3 行，**前两行数据整行丢失**，
       且表头文本变成「2025 年度349,165,168.51」这种"期间+数值"混合串。
    3. **当前**："表头行不含数字、数据行含数字"。

    第 3 条的关键细节：表头行**可能**含数字（``项目 | 2025 年度 | 2024 年度``
    里的年份）。因此"含数字"必须理解为"含**纯数字格**"（``2024`` 算，``2025 年度``
    不算）—— 碎片合并之后，期间单元格是 ``2025年度``，不会被误判。
    实测：把年份也算数字时，期间行被当成数据行，期间标签全部丢失。
    """
    if not grid:
        return 0
    if len(grid) == 1:
        return 0
    # 数据起始行 = 第一个含**数据数字**的行（标题 / 表头 / 期间行都不算）。
    #
    #   实测（2026-09，601857 2025 年报 p7「主要财务数据」）：
    #
    #       r0  截至或截止各年度 | 12 | 月 | 31 | 日        ← 标题（含"12""31"）
    #       r1  项 | 目 | 2025 | 年 | 2024 | 年 | 2023 | 年   ← 期间表头（含年份）
    #       r2  营业收入 | 2,864,469 | 2,937,981 | ...       ← 第一行数据
    #
    #   旧判据是"任何纯数字格即视为数据行"，于是 r0 的 "12" / "31" 让它
    #   返回 1 —— **真表头被当成数据吞掉**，r0 那个标题行成了表头。
    #   后果不是"少一条数据"，而是整表 0 条候选：单位写在行标签里，
    #   而行标签所在的 r2 已不在表头区（实测 ``skipped_no_unit=20``）。
    #
    #   判据因此收紧到 `_is_data_number`：**年份与日期碎片都不算数据**。
    #   另有一条测试保护相反的场景（"2025 年 6 月数据（单位：元）"这类
    #   声明行必须仍被视为表头的一部分），见
    #   ``test_build_header_finds_metric_row_below_grouping_rows``。
    limit = min(max_scan, len(grid))
    for i in range(limit):
        if any(_is_data_number(_clean_label(c)) for c in grid[i] if (c or "").strip()):
            # ★ 第 0 行到底是**表头**还是**数据**？
            #
            #   原实现一律返回 `max(1, i)` —— 强制至少一行表头。
            #   这在"表头 + 数据"的正常表上没问题，但**没有自己表头的续表**
            #   会因此丢掉第一行数据。实测（601857 p178 表 1，分部报告的
            #   第二张表，表头印在上一张表里）：
            #
            #       r0  折旧、折耗和摊销费用 | 186,238 | 27,746 | …   ← 这是数据！
            #       r1  资产减值损失 | 13,119 | 986 | …
            #
            #   返回 1 之后，r0 成了"表头"，接着 `build_header` 拼出
            #   `['折旧、折耗和摊销费用', '', '', …]`，`looks_like_statement_table`
            #   判不出报表 → 不走行导向 → 该表 0 条候选；
            #   而**资本性支出 269,089 就在这张表里**。
            #
            #   判据：第 0 行的首格是**科目名**（非空、非年份、非日期碎片）
            #   且本行确有数据数字 → 第 0 行就是数据行，表头行数为 0。
            #   首格是「项目」「科目」这类行标题，或首格本身就是年份/日期碎片时，
            #   仍按原行为保留至少一行表头。
            if i == 0 and _looks_like_data_row(grid[0]):
                return 0
            return max(1, i)

    # ★ 走到这里说明**前 max_scan 行都没有数据数字** —— 这是一张
    #   "表头多行 + 数据不在前几行"的表（或用例测试里的小网格）。
    #
    #   默认返回 1（保持原行为：至少一行表头）。但**多级表头**会让这个
    #   返回值偏小。实测（002716 的分部合同表 p170）：

    #       r0  合同分类 | 分部1 | 分部1 | 分部2 | 分部2 | 本期数 | 本期数 | 合计 | 合计
    #       r1  合同分类 | 营业收入 | 营业成本 | 营业收入 | 营业成本 | 营业收入 | 营业成本 | 营业收入 | 营业成本
    #       r2  业务类型 |  |  |  |  |  |  |  |
    #       r3  其中：   |  |  |  |  |  |  |  |
    #       r4  有色金属及其制品 | … | 12,371,785,621 | …
    #
    #   r1 是**表头第二层**（把 ``分部1``/``合计`` 细分成营业收入/营业成本），
    #   但它不含数据数字，于是 `data_start_row` 一路扫到返回 1 ——
    #   "数据区"只剩 2 行真数据，`_majority_data_columns` 判不出列结构，
    #   整个分部表的合计口径因此取不到。
    #
    #   判据：表头层之后的**连续空/标签行**也算表头 —— 它们不含数据数字、
    #   且非空格都是**短标签**（≤6 字，不含长句）。遇到第一个含数据数字的
    #   行就停。**保守取向**：只有连续 2 行以上都满足才延长，
    #   且最多延长到 max_scan 行，避免把真正的短数据行吞掉。
    #
    #   ⚠️ 延长的**行本身也不能是数据行** —— 实测被测试抓到一次：
    #      公告表的 ``["序号","公司名称","营业收入"]`` 之后是
    #      ``["1","A子公司","4560万元"]``，而 ``"1"`` **正是数据数字**。
    #      只查"首格能否解析成数字"会漏掉它（`_is_data_number("1")` 为假，
    #      因为 1..31 被当成日期碎片），但 `_looks_like_data_row`
    #      （首格是文字标签 + 本行有数据数字）能挡住。
    extended = 1
    for i in range(1, min(len(grid), max_scan)):
        row = grid[i]
        if _looks_like_data_row(row):
            break
        # ⚠️ 任何**数字形状**的格都要停 —— 包括 ``"1"`` 这种短数字。
        #    `_is_data_number("1")` 为假（1..31 被当成日期碎片，那是为
        #    "12 月 31 日"这类碎片设计的），但公告表的数据行首格恰恰是
        #    ``"1"``（序号）。实测被测试抓到一次：``["1","A子公司","4560万元"]``
        #    被当成了可延长的表头行。
        filled = [_clean_label(c) for c in (row or []) if (c or "").strip()]
        if any(re.search(r"\d", t) for t in filled):
            break
        if any(_is_data_number(t) for t in filled):
            break
        if not filled:
            extended = i + 1
            continue
        # 只认短标签行（表头分层的典型形态）——长文本行不延长
        if all(len(t) <= 6 and not _is_footnote_fragment(t) for t in filled):
            extended = i + 1
            continue
        break
    return extended if extended >= 3 else 1


def _looks_like_data_row(row: list[str]) -> bool:
    """第 0 行是**数据行**（而不是表头行）吗？—— 用于没有表头的续表。

    三条同时成立才算：

    1. 首格非空，且**不是**年份/日期碎片、不含"单位"之类声明字样
    2. 首格不是「项目」「科目」这类**行标题词**（那说明它就是表头行）
    3. 除首格外还有 `_is_data_number` 认可的**数据数字**

    保守取向：任何一条拿不准就返回 False，退回"至少一行表头"的旧行为。
    旧行为在正常表上是对的，本条只为了救"续表首行即数据"这一种情形。
    """
    if not row:
        return False
    first = _clean_label(row[0])
    if not first:
        return False
    if re.fullmatch(r"20\d{2}\s*年?", first) or "单位" in first:
        return False
    if any(k in first for k in ("项目", "科目", "项", "目")):
        return False
    return any(
        _is_data_number(_clean_label(c)) for c in row[1:] if (c or "").strip()
    )


def table_period_hint(grid: list[list[str]], *, probe: int = 8) -> str:
    """从表体内的**日期碎片行**取出本表所属报告期；取不到返回 ``""``。

    ★ 为什么需要它（2026-09 第十三轮，实测 601857 分部报告）：

    分部报告在同一份年报里用 ``(a) 2025`` / ``(b) 2024`` 两组印出，
    而**年份印在表体内的碎片行**上（跨列并排），不在列头里：

        2025 | 年 | 12 | 月 | 31 | 日

    列头是业务分部名（``油气和新能源``/``合计``），所以
    ``periods_of_columns`` 一列期间都取不到 → 整表回落到文档级期间
    ``2025A``。后果是 **2024 年的 275,849 被标成 2025A**，与 2025 年的
    269,089 撞进同一期间 —— 按期间或按最大值筛资本开支都会取到错的那条，
    而且不报错（数字确实印在原文里）。

    本函数把那个年份捞出来，让每张表**自己声明期间**：p178 表 1 → ``2025A``，
    p179 表 1 → ``2024A``。期间一分开，两条候选各归其位。

    ★ 判据经过两轮实测收紧：

    1. **扫全表，不是只扫前几行**。日期碎片行可以落在表的**底部** ——
       实测 p178 表 1 的 ``2025 年 12 月 31 日`` 在第 5 行（共 6 行），
       而 p178 表 0 的在第 1 行。只看前几行会漏掉前者。
    2. **要求全表只出现一个不同年份**。多期并排的表（``2025 年 | 2024 年``
       列头、或 ``(a) 2025`` 与 ``(b) 2024`` 两组同页）会出现多个年份，
       此时"本表属于哪一期"无法从表体判定 —— 返回 ``""``，不猜。
       这一条同时挡住了"把数据里的金额误认成年份"的极端情形。

    返回 ``"YYYYA"`` 形态（年报口径）。取不到返回 ``""``，调用方保持原行为。
    """
    if not grid:
        return ""
    years: set[str] = set()
    for row in grid[: max(probe, len(grid))]:
        for cell in row or []:
            t = _clean_label(cell)
            if re.fullmatch(r"20\d{2}", t):
                years.add(t)
    if len(years) != 1:
        return ""
    return f"{years.pop()}A"


def _is_year_header_row(row: list[str]) -> bool:
    """该行是否是**多期年份表头行**（``项目 | 2025 年 | 2024 年 | 2023 年``）。

    判据（三条同时成立）：

    1. 含 **≥2 个** 4 位年份格
    2. 含年份（≥1 个）—— 单年份行更像"某一期的数据行"，不在此列
    3. 除年份与首格外，其余非空格都是**短单位尾词**（``年`` / ``月`` / ``日`` /
       ``%`` 等，≤4 字）—— 数据行这里会是金额

    为什么需要它：年份印在表头行里是中文年报的常态，而
    :func:`data_start_row` 的判据是"含数字即数据行"。两者冲突时
    会把真表头吞掉。这条判据把"期间表头"从"数据"里摘出来。
    """
    years = 0
    others = 0
    for k, cell in enumerate(row):
        t = _clean_label(cell)
        if not t:
            continue
        # 年份格允许尾随「年」：碎片合并会把 ``2023`` + ``年`` 并成 ``2023年``
        if re.fullmatch(r"20\d{2}\s*年?", t):
            years += 1
            continue
        if k == 0:
            continue                      # 首格是行标题/科目名
        if len(t) <= 4 and (_SHORT_CJK.match(t) or t in ("%", "％", "(%)", "（%）")):
            continue                      # 单位尾词
        others += 1
    return years >= 2 and others == 0


def _is_data_number(text: str) -> bool:
    """该格是否为一个**数据**数字。

    ★ 2026-09 实测补充：**年份与日期碎片都不算数据**。

    中文年报的表头 / 标题行里含有大量数字，但它们不是数据：

        r0  截至或截止各年度 | 12 | 月 | 31 | 日        ← "12""31" 是日期碎片
        r1  项 | 目 | 2025 | 年 | 2024 | 年 | 2023 | 年   ← 年份

    旧判据（任何纯数字格即视为数据行）让 ``data_start_row`` 返回 1 ——
    **真表头被当成数据吞掉**，而 r0 那个标题行成了表头，整表 0 条候选
    （实测 ``skipped_no_unit=20``）。

    排除规则：
    * ``20xx`` —— 年份
    * 纯 2 位及以下且 ≤31 的整数 —— 月 / 日的碎片（``12``/``31``）

    为什么后一条安全：数据行里的金额总有千分位或更多位数
    （``2,864,469``），而纯 ``12`` 这种小整数出现在**数据行**里时，
    该行必然还有别的更大的数，不影响"这一行是数据行"的判定。
    另有测试保护相反场景（``2025 年 6 月数据（单位：元）`` 这类
    声明行必须仍算表头），见
    ``test_build_header_finds_metric_row_below_grouping_rows``。
    """
    if not _NUMERIC_CELL.match(text):
        return False
    t = text.replace(",", "").replace("，", "")
    if re.fullmatch(r"20\d{2}", t):
        return False
    if re.fullmatch(r"\d{1,2}", t) and 1 <= int(t) <= 31:
        return False
    return True


def numeric_defects(raw_text: str, *, metric: str = "", unit: str = "") -> str:
    """返回数值**形态缺陷**的描述；无缺陷返回空串。

    这是"数值质量守卫"的**规则部分**（不需要跨行比较的那几条）。
    全部判据都来自对真实事实库的普查（419 条里命中 6 条），不是拍脑袋：

    | 缺陷 | 实测实例 | 为什么必然是错的 |
    |---|---|---|
    | 前导零 | ``08.72`` | 规范写法不会以 0 开头（``0.72`` 可以）。这是被切碎的片段 |
    | 千分位分组不足三位 | ``218,073,07``（真值 12 位） | 分组错位 = 数字被切开后拼接错了 |
    | 以分隔符结尾 | ``1,234.`` | 小数部分缺失，折行被截断 |
    | **货币指标小于 1 元** | ``revenue = 8.72 元`` | 金额类指标的规范单位是元；没有任何一家上市公司的收入或资产会小于 1 元 |
    """
    t = (raw_text or "").strip()
    if not t:
        return ""
    body = t.split(".")[0]

    # ① 前导零
    if re.search(r"(?<![\d.])0\d[\d,]*", t.replace(",", "").replace("，", "")):
        return f"原文 {t!r} 含前导零，疑似被切碎的数字片段"

    # ② 千分位分组不足三位
    m = re.search(r"\d[\d,]*", body)
    if m:
        groups = m.group(0).split(",")
        if len(groups) > 1 and any(len(g) != 3 for g in groups[1:]):
            return f"千分位分组异常（{m.group(0)}），疑似数字被切开后拼接错误"

    # ③ 以分隔符结尾
    if re.search(r"[,.]$", t):
        return f"原文 {t!r} 以分隔符结尾，小数部分缺失"

    # ④ 货币指标小于 1 分（0.01 元）
    #
    #    ⚠️ 这条判据刻意做得**很弱**。曾经写成"小于 1 元"，结果自相矛盾：
    #    0.72 元是合法的小额科目（如每股股利的小数部分），却被判为错误。
    #    真正管用的两条是①前导零与文档级量级离群（见 magnitude_outliers）——
    #    实测的 `08.72` 与 `2.7` 分别由它们抓住。这条只兜"连一分钱都不到"的极端值。
    if metric and unit_family(unit or "元") == "money":
        cleaned = _strip_unit(t)
        try:
            if cleaned and Decimal(cleaned) != 0 and abs(Decimal(cleaned)) < Decimal("0.01"):
                return (
                    f"金额类指标 {metric} 的值为 {t!r}（不足 0.01 元），"
                    f"只可能来自片段或单位未换算"
                )
        except Exception:  # noqa: BLE001 - 解析不了就不做这条判断
            pass
    return ""


def _strip_unit(text: str) -> str:
    """去掉尾部的单位与千分位，留下纯数字串。"""
    t = (text or "").strip()
    m = _UNIT_IN_CELL.search(t)
    if m:
        t = t[: m.start()].strip()
    return t.replace(",", "").replace("，", "")


def magnitude_outliers(
    values: list[tuple[Any, Decimal]], *, threshold: int = 5
) -> list[Any]:
    """同一指标在同一文档内**偏小** 10^threshold 倍以上的观测 → 返回其标识。

    用途（真实事故）：中国石油的年报全部以「人民币百万元」为单位，
    漏了换算的行得到 ``2.7``，而真值是 2,700,000 百万元 ——
    单条看"是个正常的小数"，与同文档其他观测比才暴露差 10⁶。

    ⚠️ **只标偏小的一侧**。偏大往往是真实的离群（资产总计 vs 某个明细科目），
    标了就是误伤；而金额类指标偏小 10⁵ 倍以上，几乎只可能是漏换算或片段。
    """
    if len(values) < 3:
        return []
    adjs = [(k, abs(v).adjusted()) for k, v in values if v is not None and v != 0]
    if len(adjs) < 3:
        return []
    median = sorted(a for _, a in adjs)[len(adjs) // 2]
    return [k for k, a in adjs if median - a >= threshold]


def truncation_suspect(raw_text: str, value: Decimal, unit: str) -> str:
    """数值是否**疑似截断或形态错误**。返回原因字符串，正常则返回空串。

    **为什么需要它**（真实事故）：折行数字修复在一份年报上触发 2533 次，
    其中至少一次修出了位数不足的值：

        total_assets = 21807307   raw_text = '218,073,07'
        真值          = 218,073,073.97

    8 位 vs 12 位，**没有任何报错**，置信度 0.95，直接入库。
    这类错误的危险程度高于漏抽：漏抽会显式拉低覆盖率，
    而截断值看起来完全正常，下游（估值、核查）只会得到一个错误的结论。

    判据分两层：形态缺陷（:func:`numeric_defects`）与位数不一致。
    """
    defect = numeric_defects(raw_text, unit=unit)
    if defect:
        return defect

    digits_raw = re.sub(r"\D", "", raw_text or "").lstrip("0")
    digits_val = str(abs(value).to_integral_value()).lstrip("0") if value == value.to_integral_value() else ""
    if digits_raw and digits_val and len(digits_raw) != len(digits_val):
        # 单位换算会改变位数（万元→元 加 4 位），因此只在**同量级**时才判定
        diff = len(digits_val) - len(digits_raw)
        if diff == 0 or diff in (1, -1):  # 末位四舍五入允许 ±1 位
            return (
                f"原文位数（{len(digits_raw)}）与解析值位数（{len(digits_val)}）不一致，"
                f"疑似折行修复截断"
            )
    return ""


def unit_scale_outliers(
    values: list[tuple[Any, Decimal]], *, threshold: int = 3
) -> list[Any]:
    """同一指标在一张表里数量级差异过大 → 返回可疑的项。

    用途：兜住 10ⁿ 类错误。中国石油的年报全部以「人民币百万元」为单位，
    若某一行漏掉了单位换算，它会比同表其他行小 10⁶ ——
    这类错误在单条事实上看不出来，只有横向比较才暴露。

    ``values`` 是 ``[(标识, 数值)]``；返回数量级偏离中位数最多的那些标识。
    """
    if len(values) < 3:
        return []
    magnitudes = []
    for key, v in values:
        if v is None or v == 0:
            continue
        magnitudes.append((key, abs(v).adjusted()))
    if len(magnitudes) < 3:
        return []
    ordered = sorted(m for _, m in magnitudes)
    median = ordered[len(ordered) // 2]
    return [k for k, m in magnitudes if abs(m - median) >= threshold]


_SCOPE_TITLE = re.compile(
    r"(合并及公司|合并|母公司|公司)?\s*(资产负债表|利润表|现金流量表|股东权益变动表|所有者权益变动表)"
)


def statement_scope_of_title(text: str) -> str:
    """从报表标题判断口径：``consolidated`` / ``parent_only`` / `""`（判不出）。

    报表标题是判断口径的**唯一权威来源** —— 同一页可能上下并排
    「合并资产负债表」与「母公司资产负债表」，两者的科目名完全相同，
    只有标题能区分。

    实测（000722 年报 p75/p77）：不区分口径时，事实库里同一期间的
    所有者权益既有 3,797,417,361.30（合并）又有 3,189,997,628.22（母公司），
    下游任取一个都可能取错，而且**不报错**。
    """
    m = _SCOPE_TITLE.search(text or "")
    if not m:
        return ""
    prefix = m.group(1) or ""
    if "合并" in prefix:
        return "consolidated"
    if "母公司" in prefix:
        return "parent_only"
    return ""


def statement_blocks(page_blocks: list[dict[str, Any]]) -> list[tuple[float, str]]:
    """从页面文本块里取出报表标题及其纵坐标，按位置升序。

    ``page_blocks``：``[{"text": str, "y0": float}, ...]``（PyMuPDF 的 text blocks）。
    只保留**短文本块**且含报表名的，避免把正文里提到"利润表"的句子误当标题。
    """
    out: list[tuple[float, str]] = []
    for blk in page_blocks or []:
        text = str(blk.get("text") or "").strip().replace("\n", "")
        if not text or len(text) > 30:
            continue
        scope = statement_scope_of_title(text)
        if scope:
            out.append((float(blk.get("y0") or 0.0), scope))
    return sorted(out)


def split_by_scope(
    grid: list[list[str]],
    row_y: dict[int, float],
    titles: list[tuple[float, str]],
    *,
    default_scope: str,
) -> list[tuple[list[list[str]], str]]:
    """按标题的纵坐标把一张网格切成若干「同口径」的段。

    ``titles`` 是**本页**出现的标题（可能为空）；``default_scope`` 是
    **上一页遗留的口径** —— 中文财报的报表常常跨页而标题只印在首页，
    因此续表必须继承上一页的口径。

    实测（000722）：合并资产负债表从 p73 跨到 p76，标题只在 p73；
    p77 上半那张表其实是它的尾巴，若按"本页标题"判断就会张冠李戴。
    """
    if not grid:
        return []
    if not titles:
        return [(grid, default_scope)]

    segments: list[tuple[list[list[str]], str]] = []
    boundaries = sorted(titles)
    first_y = boundaries[0][0]
    head = [i for i in range(len(grid)) if row_y.get(i, 1e9) < first_y]
    if head:
        segments.append(([grid[i] for i in head], default_scope))
    for idx, (y0, scope) in enumerate(boundaries):
        end_y = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else 1e18
        rows = [i for i in range(len(grid)) if y0 - 1.0 <= row_y.get(i, 1e9) < end_y]
        if rows:
            segments.append(([grid[i] for i in rows], scope))
    return [s for s in segments if s[0]]


def row_y_from_cells(cells: list[dict[str, Any]]) -> dict[int, float]:
    """从表格记录的 ``cells`` 里取「原始行号 → 纵坐标」。

    用于把网格行与报表标题（页面文本块）按纵坐标对齐 —— 标题不在网格里，
    只能靠坐标把两者对上。缺 bbox 时返回空字典，:func:`split_by_scope`
    会退化为"整表按上一页口径处理"（安全兜底，不猜）。

    ⚠️ 这里用的是**原始行号**；列号可能被碎片合并改动，但行号不会
    （合并只在列方向进行）。
    """
    out: dict[int, float] = {}
    for c in cells or []:
        r = c.get("row")
        bbox = c.get("bbox") or {}
        y0 = bbox.get("y0")
        if r is None or y0 is None:
            continue
        try:
            out[int(r)] = min(out.get(int(r), 1e18), float(y0))
        except (TypeError, ValueError):
            continue
    return out


def pick_subject_column(
    grid: list[list[str]], data_start: int, *, metric_cols: set[int],
) -> int:
    """选出**行主体**所在列。返回列号，选不出返回 -1。

    为什么不能简单取第一列：担保表的第 0 列是**担保人**（每行都一样），
    第 1 列才是被担保人。取错主体，一整表的事实都会挂到错误的主体名下。

    两条判据，按优先级：

    1. **表头关键词**（被担保人/公司名称/股东名称/…）—— 最直接
    2. **取值多样性**：跨数据行取值最分散的那一列。
       担保人列重复 9 次，被担保人列 9 个不同 —— 分散度天然把主体挑出来。
    """
    if not grid:
        return -1
    n_cols = max(len(r) for r in grid)
    first_data = min(data_start, len(grid) - 1)
    header = grid[0] if grid else []

    # 判据 1：表头关键词
    for hint in _SUBJECT_HEADER_HINTS:
        for j in range(n_cols):
            label = _clean_label(header[j]) if j < len(header) else ""
            if label and hint in label and j not in metric_cols:
                return j

    # 判据 2：多样性
    best_j, best_score = -1, 0.0
    for j in range(n_cols):
        if j in metric_cols:
            continue
        values = []
        for i in range(first_data, len(grid)):
            if j >= len(grid[i]):
                continue
            t = _clean_label(grid[i][j])
            if t:
                values.append(t)
        if len(values) < 1:
            continue
        # 只有"文字型"取值才有资格当主体列 —— 必须排除**数值格**。
        # 实测：`6,000.00元`、`100%` 这类含汉字/百分号的格子能骗过
        # `_NUMERIC_CELL`，于是某个数值列靠"取值分散度高"当选主体列，
        # 行主体变成 "100%"，后续主体归属全线错位。
        texty = [
            v for v in values
            if not _NUMERIC_CELL.match(v) and not looks_like_value(v)
            and len(v) >= _MIN_SUBJECT_LEN
        ]
        if not texty:
            continue
        distinct = len(set(texty)) / len(texty)
        if distinct > best_score:
            best_j, best_score = j, distinct
    return best_j


_VALUE_LIKE = re.compile(
    r"^[（(]?-?[\d,]+(?:\.\d+)?[)）]?\s*"
    r"(?:亿元|百万元|万元|千元|元/股|元每股|亿股|万股|千股|元|股|%|％|倍)?\s*[。；;]?$"
)


def looks_like_value(text: str) -> bool:
    """像不像**一个数值**（而不是主体名）。

    用途：行主体判定时必须排除数值格。实测：「434,749.30万元」这种格子
    因为含汉字单位，`_NUMERIC_CELL` 判不出来，于是被当成"主体名"，
    归因结果就成了一个数字 —— 荒谬但不会报错。
    """
    t = _clean_label(text)
    if not t or not re.search(r"\d", t):
        return False
    return bool(_VALUE_LIKE.match(t))


def subject_of_row(row: list[str], *, subject_col: int = -1,
                   metric_cols: set[int] | None = None) -> str:
    """数据行的主体名。优先用选定的主体列，退化时取最靠左的文字格。"""
    if subject_col >= 0 and subject_col < len(row):
        t = _clean_label(row[subject_col])
        if (len(t) >= _MIN_SUBJECT_LEN and not _NUMERIC_CELL.match(t)
                and not looks_like_value(t)):
            return t
        return ""
    bad = metric_cols or set()
    for j, cell in enumerate(row):
        if j in bad:
            continue
        t = _clean_label(cell)
        if len(t) < _MIN_SUBJECT_LEN or _NUMERIC_CELL.match(t):
            continue
        if looks_like_value(t):
            continue
        if re.fullmatch(r"[\d/\-年月日.]+", t):
            continue
        return t
    return ""


def _remap_columns(
    cands: list[dict[str, Any]], col_maps: list[list[int]]
) -> list[dict[str, Any]]:
    """把合并后网格的列号换回**原始网格**的列号。

    候选事实要锚定到原始单元格取 bbox（裁图、原文高亮都依赖它），
    因此合并只是内部手段，对外仍用原列号。

    ``col_maps`` 里每一项是**原列号列表**（一个合并格可能横跨多列）。
    这里统一取**第一个**原列号：它一定落在这个合并格覆盖的范围内，
    因此 :func:`cell_pixel_box` 能正确裁到含该数值的那一块。

    ⚠️ 曾经直接把整个列表赋给 ``cand["col"]``，于是下游把它当 dict 键用，
    抛 ``TypeError: unhashable type: 'list'`` —— 而且是跑到 build_facts
    阶段才炸（judge 已跑完 24 分钟）。列号从始至终都必须是**单个整数**。
    """
    for c in cands:
        row_idx, col_idx = c.get("row"), c.get("col")
        try:
            mapped = col_maps[row_idx][col_idx]
        except (IndexError, TypeError):
            continue
        if isinstance(mapped, (list, tuple)):
            mapped = mapped[0] if mapped else None
        if isinstance(mapped, int):
            c["col"] = mapped
    return cands


def extract_table_facts(
    grid: list[list[str]],
    *,
    alias_map: dict[str, str],
    metric_units: dict[str, str],
    unit_hint: str = "",
    page_no: int = 0,
    column_map: dict[int, str] | None = None,
    max_rows: int = 200,
    page_text: str = "",
    doc_entity: str = "",
    doc_period: str = "unknown",
    carry_header: list[str] | None = None,
    page_blocks: list[dict[str, Any]] | None = None,
    row_y: dict[int, float] | None = None,
    prev_scope: str = "consolidated",
    grid_cells: list[dict[str, Any]] | None = None,
    carry_seg_total_col: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """从一张表的网格里抽事实。返回 ``(候选列表, 统计)``。

    ``column_map`` 用于**公告续表**：中文公告的长表跨页时通常只在首页印表头，
    续页直接是数据行。此时由调用方把上一页的「列 → 指标」映射传进来。

    ``carry_header`` 用于**财务报表续页**：资产负债表/利润表跨页时也是只印一次表头，
    而**合计行往往就在续页上**（实测：000722 的「资产总计」「负债合计」
    「所有者权益合计」全在 p76/p77 的续页，首页 p73 只有明细科目）。
    不继承表头的话，这些合计行永远抽不到 —— 表现为"资产总计缺失"，
    而下游会去用别处（子公司汇总表）的同名指标顶上，得到错误的数。

    ``page_text`` / ``doc_entity`` / ``doc_period`` 是**财务报表支持**的三个输入：

    * ``page_text``：整表的「单位：元」声明常常不在网格里（在表格上方的正文层），
      只在网格里找会漏掉，整表被判"无单位"而颗粒无收
    * ``doc_entity``：财务报表**没有主体列** —— 主体在报表标题上，不在表内。
      此时行标签（「营业收入」「经营活动产生的现金流量净额」）只是科目名，
      若拿它当主体，一份报表的数字会被拆散到几十个伪主体名下
    * ``doc_period``：列头写着期间时以列头为准（见 :func:`periods_of_columns`）
    * ``page_blocks`` / ``prev_scope``：**口径**（合并 / 母公司）。同一页常上下并排
      两张报表，科目名完全相同，只有标题能区分；而标题只印一次、表格跨页，
      因此续页要靠 ``prev_scope`` 继承上一页的口径

    候选字段与 5.1 规则抽取对齐，便于复用同一套闸门与置信度模型。
    """
    stats = {
        "tables": 1,
        "rows": 0,
        "metric_columns": 0,
        "candidates": 0,
        "skipped_no_unit": 0,
        "skipped_bad_value": 0,
        "skipped_no_subject": 0,
        "repaired_cells": 0,
        "statement_table": 0,
        "period_from_header": 0,
        "carried_header": 0,
        "grid_aligned": 0,
        "grid_align_cols": 0,
        "segment_table": 0,
        "segment_total_col": -1,
        "segment_carried": 0,
    }
    if not grid:
        return [], stats

    # 单元格文本先做折行修复（数字被排版切断是常态）
    fixed_grid: list[list[str]] = []
    for row in grid:
        new_row = []
        for cell in row:
            fixed, changes = repair_line_broken_numbers(cell or "")
            if changes:
                stats["repaired_cells"] += 1
            new_row.append(fixed)
        fixed_grid.append(new_row)

    # 再把 PDF 拆碎的相邻格并回去（`2024|年|2023|年` → `2024年|2023年`），
    # 并剔除脚注编号列（它们混在期间列之间会让后面整体错位）。
    # 不处理的话表头识别不出来，整表被视为"没有指标列"。
    merged = merge_fragments(fixed_grid)
    fixed_grid, col_maps = merged.grid, merged.col_maps
    stats["rows_after_merge"] = len(fixed_grid)
    stats["dropped_footnote_cols"] = len(merged.dropped_cols)

    # ★ 按单元格坐标重建列结构（根治「表头 9 格 / 数据 5 格」）。
    #
    #   为什么必须在 `merge_fragments` **之后**再对齐、而不是取代它：
    #   `merge_fragments` 的列边界是从**表头区**推的，而表头正是被拆碎的
    #   那一方 —— 它按那个边界逐行合并，于是数据行的格被并进了错误的列
    #   （实测 p7 的 `(54,144)` 与 `(` 被并成一格）。但它的**单元内**文本
    #   拼接规则是对的，且已经处理了脚注编号列。
    #
    #   因此分工为：`merge_fragments` 负责**格内**文本的合理拼接，
    #   本步骤负责**把每个格放回它真正所属的列**（用 x 区间重叠判定）。
    #   两者叠加后，表头与数据落在同一套列号上。
    #
    #   判据不成立时 `applied=False`，`fixed_grid` 与 `col_maps` 原样返回 ——
    #   这条路径**只能让结果变好，不会因为自己出错而让结果变差**。
    # 分部表「合计列」的**集合**（不是单列）：两层列头的分部表会有多个合计列，
    # 它们同属集团口径（实测 002716 的「合计营业收入 / 合计营业成本」）。
    seg_total_col: frozenset[int] = frozenset()
    # ★ 本表自己声明的期间：**必须在列对齐之前取**（见 table_period_hint）。
    #
    #   对齐会把 ``2025`` / ``年`` / ``12 月`` / ``31 日`` 这些碎片拼进
    #   同一个格，得到 ``'2025 年 12 月 31 日'`` —— 年份不再是独立的一格，
    #   按"整格就是一个年份"的判据就再也取不到了（实测踩过：
    #   对齐前 hint='2025A'，对齐后 hint=''）。
    table_period = table_period_hint(fixed_grid)
    if table_period:
        stats["period_from_table_body"] = 1
    if grid_cells:
        from pipelines.r1_extract.grid_align import align_grid_by_bbox

        aligned = align_grid_by_bbox(
            fixed_grid, col_maps, grid_cells, data_start=data_start_row(fixed_grid),
        )
        stats["grid_aligned"] = 1 if aligned.applied else 0
        if aligned.applied:
            fixed_grid, col_maps = aligned.grid, aligned.col_maps
            stats["grid_align_cols"] = aligned.n_cols
            seg_total_col = aligned.segment_total_cols
    # 本表判不出合计列时用**继承值**：分部报告的表头只印在第一张表上，
    # 承载目标行（资本性支出等）的表紧随其后且没有自己的表头。
    #
    # ⚠️ 继承来的是**原始列号**，而本表的网格列结构可能与上一张表不同
    #    （实测 p178：表 0 对齐后 7 列、表 1 对齐后 10 列）。必须经
    #    `remap_col_via_maps` 换算；换算不出来就**放弃**，绝不套用 ——
    #    套错只会把普通数据列标成"集团口径"，数字仍是对的、标签全错、不报错。
    if not seg_total_col and carry_seg_total_col is not None:
        from pipelines.r1_extract.grid_align import remap_col_via_maps

        mapped = remap_col_via_maps(int(carry_seg_total_col), col_maps)
        if mapped is not None and mapped < max((len(r) for r in fixed_grid), default=0):
            seg_total_col = frozenset({mapped})
            stats["segment_carried"] = 1
    if seg_total_col:
        stats["segment_total_col"] = min(seg_total_col)
        stats["segment_table"] = 1

    header, data_start = build_header(fixed_grid, alias_map)

    # **表头实际有几行** —— 期间提取要用它，不能用 `data_start`（见下方注释）。
    # 续页时本页没有表头，行数按 1 算（表头内容来自 carry_header，不由本页提供）。
    header_row_count = 1 if carry_header else max(1, data_start_row(fixed_grid))

    # 续页：本页没有表头，但上一页的报表表头可以继承
    if carry_header and len(carry_header) >= max((len(r) for r in fixed_grid), default=0):
        header = list(carry_header)
        stats["carried_header"] = 1

    # —— 分流：财务报表走**行导向**路径 ——
    #   判据必须在列映射之前：报表的列头是期间，列映射天然为空，
    #   先算列映射会把整张报表判成"没有指标列"而直接丢弃。
    if doc_entity and _is_row_oriented(
        fixed_grid, header, data_start, carry_header, alias_map
    ):
        # ★ 按口径切段：同一页可能上下并排「合并资产负债表」与
        #   「母公司资产负债表」，科目名完全相同；不切段就会把两者的值
        #   都当成同一口径下的观测，下游任取一个都可能取错（实测事故）。
        rows_y = row_y if row_y is not None else {}
        titles = statement_blocks(page_blocks or [])
        # 只切**数据区**，表头行由每段原样带上（见下）
        body = fixed_grid[data_start:]
        body_y = {i - data_start: y for i, y in rows_y.items() if i >= data_start}
        segments = split_by_scope(
            body, body_y, titles, default_scope=prev_scope or "consolidated"
        )
        head = [list(fixed_grid[i]) for i in range(data_start)]
        head_maps = [col_maps[i] for i in range(data_start)]
        # 本表**没有表头行**（表头在上一张表里）时，`head` 是空的 ——
        # 期间解析要退回调用方带来的 `carry_header`，否则每一列都拿不到期间，
        # 候选会因"没有期间"而被下游丢掉。
        if not head and carry_header:
            head = [list(carry_header)]
            head_maps = [[j] for j in range(len(carry_header))]
            stats["head_from_carry"] = 1

        # 本表自己声明的期间已在列对齐之前取出（见 table_period 的说明），
        # 这里只负责把它传给语句抽取。
        seg_period = table_period

        seg_cands: list[dict[str, Any]] = []
        seg_stats: dict[str, int] = {}
        # 分部表合计列要传给每一段 —— 它按**列号**判定，与口径切段无关
        seg_stats["segment_total_col"] = (
            min(seg_total_col) if seg_total_col else -1
        )
        if seg_total_col:
            seg_stats["segment_table"] = 1
        for seg_body, seg_scope in segments:
            # 把表头拼回每段之前；行号用 row_offset 换算回**整表坐标系**，
            # 否则 _remap_columns 会拿子段行号去查整表的列映射（实测：口径张冠李戴）
            c, st = extract_statement_facts(
                [*head, *seg_body],
                header=header,
                header_rows=head,
                data_start=data_start,
                row_offset=body.index(seg_body[0]) if seg_body else 0,
                alias_map=alias_map,
                metric_units=metric_units,
                unit_hint=unit_hint,
                page_text=page_text,
                page_no=page_no,
                doc_entity=doc_entity,
                doc_period=doc_period,
                doc_scope=seg_scope,
                # 分部报告的「合计列」：让本表的取值按**落在哪一列**定口径
                # （合计列 = 集团，分部列 = 板块）。不是分部表时为 None。
                seg_total_col=seg_total_col,
                # 本表自己声明的期间（分部报告的年份印在表体内）
                period_hint=seg_period,
            )
            seg_cands.extend(c)
            for k, v in st.items():
                seg_stats[k] = seg_stats.get(k, 0) + int(v)
        stats.update(seg_stats)
        stats["statement_table"] = 1
        stats["metric_columns"] = seg_stats.get("statement_candidates", 0)
        stats["scopes"] = len(segments)
        return _remap_columns(seg_cands, col_maps), stats

    col2metric = map_columns_to_metrics(header, alias_map, metric_units)
    if not col2metric and column_map:
        # 续表：本页没有表头（表头只在首页印一次），沿用上一页的列映射。
        # 此时要判断第 0 行到底是数据还是重复的表头 ——
        # 判据是"指标列里有没有能解析成数字的格"。
        col2metric = dict(column_map)
        first = fixed_grid[0] if fixed_grid else []
        numeric = sum(
            1 for j in col2metric
            if j < len(first) and parse_decimal(_clean_label(first[j])) is not None
        )
        data_start = 0 if numeric else 1
    stats["metric_columns"] = len(col2metric)
    if not col2metric:
        return [], stats

    hint = unit_hint or table_unit_hint(fixed_grid, page_text)
    # 页面级单位声明兜底：网格里没有「单位：」时，用调用方给的页面文本再找一次。
    if not hint and page_text:
        hint = find_unit_declaration(page_text)
    metric_cols = set(col2metric)
    subject_col = pick_subject_column(fixed_grid, data_start, metric_cols=metric_cols)

    # 列导向路径里也可能遇到"主体列选不出、但行标签是科目名"的表：
    # 此时用文档主体兜底，总比拿科目名当主体强。
    statement_table = False

    # ⚠️ 必须只用**表头行**（``header_row_count``），不能用 ``data_start``：
    #    续页表（``carried_header``）的 row 0 已经是数据，``data_start`` 会指进数据区，
    #    把数据行当表头读。这里取 ``header_row_count`` 是**安全边界** ——
    #    真实的期间信息只会印在表头里，永远不会印在第一行数据里。
    col_period = periods_from_header_rows(
        [list(fixed_grid[i]) for i in range(min(header_row_count, len(fixed_grid)))],
        doc_period=doc_period,
        prev_period=previous_period(doc_period),
        data_cols=_data_columns(
            fixed_grid, data_start, max((len(r) for r in fixed_grid), default=0)        ),
        grid=fixed_grid,
        data_start=data_start,
    )
    if col_period:
        stats["period_from_header"] = len(col_period)

    out: list[dict[str, Any]] = []

    for i in range(data_start, min(len(fixed_grid), data_start + max_rows)):
        row = fixed_grid[i]
        stats["rows"] += 1
        if statement_table:
            subject = doc_entity
        else:
            subject = subject_of_row(row, subject_col=subject_col, metric_cols=metric_cols)
        if not subject:
            stats["skipped_no_subject"] += 1
            continue
        for j, canonical in col2metric.items():
            if j >= len(row):
                continue
            raw = _clean_label(row[j])
            if not raw or not re.search(r"\d", raw):
                continue

            # —— 闸门 1：单位必须明确 ——
            #   优先级：单元格自带的单位 > 本列表头里的单位 > 整表单位声明。
            #   逐列取单位是必须的：同一张表里「金额（万元）」与「比例 %」并存。
            m_unit = _UNIT_IN_CELL.search(raw)
            unit = normalize_unit(m_unit.group(1)) if m_unit else ""
            number_text = raw
            if m_unit:
                number_text = raw[: m_unit.start()].strip()
            unit_source = "cell"
            # ★ 优先级 2：**行标签里的单位**。
            #
            #   实测事故（严重）：中国石油年报 p95 是一张「主要财务指标」表，
            #   单位写在**行标签**里而不是列头或页面声明：
            #
            #       期末现金及现金等价物余额（人民币百万元） | 206,162 | 172,477
            #
            #   原实现只看「单元格 → 列头 → 表级声明」，行标签被漏掉，
            #   于是单元格没单位时被页面/表级声明顶上，量纲错 100 倍：
            #   抽出的现金是 **20.6 万亿**（真值 2,061.62 亿）。
            #   后果：净债 = 833 亿 − 20.6 万亿 ≈ −20.5 万亿，
            #   DCF 每股价值被推到 ¥124.41（真实市值对应约 ¥10 量级）。
            #   这类错误不报错、quote 也能回读（`206,162` 确实在原文里）。
            #
            #   本路径里行标签就是 `subject`：`subject_of_row` 取的是本行
            #   最靠左的非数值文字格，正是「期末现金及现金等价物余额（人民币百万元）」
            #   这种带单位的科目名。
            row_label_unit = unit_in_label(subject) if subject else ""
            if not unit and row_label_unit:
                unit = row_label_unit
                unit_source = "row_label"
            if not unit:
                unit = unit_in_label(header[j]) if j < len(header) else ""
                unit_source = "column_header"
            if not unit:
                unit = hint
                unit_source = "table"
            if not unit:
                stats["skipped_no_unit"] += 1
                continue

            # —— 闸门 2：量纲族必须与指标声明一致 ——
            #   但**指标单位未知时不设限**：`倍`（估值倍数）这类指标在
            #   metrics.yaml 里的单位写的是「倍」，而报表里普通列就是数值。
            #   早期实现无条件要求族匹配，导致单位声明为「元」的报表里
            #   连百分比列一起被拒 —— 拒得没有道理，因为"未知"不等于"不符"。
            want = expected_family(metric_units.get(canonical, ""))
            got = unit_family(unit)
            if want not in ("", "other") and got != "other" and want != got:
                stats["skipped_bad_value"] += 1
                continue

            if not _NUMERIC_CELL.match(number_text):
                stats["skipped_bad_value"] += 1
                continue
            value = parse_decimal(number_text)
            if value is None:
                stats["skipped_bad_value"] += 1
                continue

            try:
                if got == "share":
                    value_std, target = _to_shares(value, unit), "股"
                elif got == "ratio":
                    value_std, target = value, "%"
                elif got == "per_share":
                    value_std, target = value, "元每股"
                elif unit == "倍":
                    value_std, target = value, "倍"
                else:
                    value_std, target = to_yuan(value, unit), "元"
            except Exception:
                stats["skipped_bad_value"] += 1
                continue

            stats["candidates"] += 1
            out.append({
                "metric": canonical,
                "entity": subject,
                "value": value_std,
                "unit": target,
                "raw_text": raw,
                "quote": raw,
                "row": i,
                "col": j,
                "page": page_no,
                "unit_source": unit_source,
                "period": col_period.get(j, ""),
                "suspect": truncation_suspect(raw, value_std, target),
            })
    return out, stats


def _to_shares(value: Decimal, unit: str) -> Decimal:
    table = {"股": Decimal("1"), "千股": Decimal("1000"),
             "万股": Decimal("10000"), "亿股": Decimal("100000000")}
    return value * table.get(normalize_unit(unit), Decimal("1"))


__all__ = [
    "extract_table_facts",
    "build_header",
    "map_columns_to_metrics",
    "subject_of_row",
    "table_unit_hint",
    "find_unit_declaration",
    "unit_family",
    "expected_family",
    "periods_of_columns",
    "previous_period",
]
