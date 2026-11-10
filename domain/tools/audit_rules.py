"""R5 研究报告纠错核查 —— 五类错误的确定性规则引擎。

核心设计思想：**不用「让模型找茬」，而是用事实库做确定性比对。**

    R1 建立的事实库 = 数字真源
    R5 的每个数字主张 -> 反查事实库 -> 不一致即报错

这条路径的准确率和误报控制远超「LLM 通读找错」，因为：
    * 比对是确定性的，不依赖模型当时的状态
    * 每条结论都能给出「正确值 + 来源 + 算式」
    * 无法比对的情况会显式降级为 SUSPECTED，而不是猜一个

五类错误（对齐赛题考察点）：
    E1 单位       万元↔亿元、%↔小数、同比与绝对额混淆
    E2 估值倍数   分母口径错、时点错配、市值口径、EV 未扣净债
    E3 统计口径   合并vs母公司、归母vs扣非、单季vs累计
    E4 引用       数字无来源、引用编号错配、报告期错配
    E5 算术       分项和≠合计、占比错、增速错、前后矛盾
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Sequence

from pydantic import BaseModel, Field

from agent.evidence.models import (
    ErrorType,
    Fact,
    Finding,
    FindingLocation,
    PeriodType,
    ProfitBasis,
    Severity,
    StatementScope,
)
from agent.evidence.store import FactLedger
from agent.runtime.errors import HarnessError
from agent.tools.registry import tool
from agent.validate.numeric import (
    approx_equal,
    from_yuan,
    magnitude_gap,
    normalize_unit,
    parse_amount,
    parse_decimal,
    q,
    rel_diff,
    to_yuan,
)

# ==========================================================================
# 报告数字主张的抽取
# ==========================================================================

# 结构化主张：数字 + 单位 + 可选指标关键词
_CLAIM_PATTERN = (
    r"(?P<num>[（(]?-?\d[\d,]*(?:\.\d+)?[)）]?)"
    r"\s*"
    r"(?P<unit>亿元|百万元|万元|千元|元/股|元每股|万股|亿股|千股|股|%|％|‰|倍)?"
)

# 指标关键词 -> 规范指标名（从 configs/metrics.yaml 的 aliases 构建）
_MIN_YEAR = 1990
_MAX_YEAR = 2100

_CN_NUM = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


@dataclass
class Claim:
    """报告中一条数值主张。"""

    text: str
    value: Decimal
    unit: str
    start: int
    end: int
    section: str | None = None
    metric_hint: str | None = None
    """上下文匹配到的规范指标名（若能判断）。"""
    context: str = ""

    @property
    def in_yuan(self) -> Decimal | None:
        try:
            from agent.validate.numeric import to_yuan

            return to_yuan(self.value, self.unit) if self.unit else None
        except Exception:
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "value": str(self.value),
            "unit": self.unit,
            "start": self.start,
            "end": self.end,
            "section": self.section,
            "metric_hint": self.metric_hint,
        }


# ---------------------------------------------------------------------------
# 单位量纲族
# ---------------------------------------------------------------------------
# 为什么需要「族」而不是直接比字符串：草稿写「万元」、事实库存「元」，
# 二者说的是同一个金额，直接比单位会让**正确**的数字被判成单位错误或
# 「无来源」。归属到同一族后按数值比对才正确。
#
# 反过来，跨族比对必须禁止：「7.92%」与「2,456,000,000 元」量纲根本不同，
# 拿去比较会产生荒谬结论（实测踩过这个坑）。
_MONEY_UNITS = {"元", "千元", "万元", "百万元", "亿元"}
_SHARE_UNITS = {"股", "千股", "万股", "亿股"}
_RATIO_UNITS = {"%", "‰", "个百分点"}
_PER_SHARE_UNITS = {"元/股", "元每股", "元股"}

# 跨指标「按数值反查」时的最小量级要求：
# 至少差 10 倍才可能是「同一数值被写成了不同单位」。
_VALUE_LOOKUP_MIN_RATIO = Decimal("10")

# 量级类判据（E1 情形 2）可用的**最低事实置信度**。
#
# 0.95 = L1 文本层直取 / 表格网格直取的基础分（见 SOURCE_LAYER_BASE_CONFIDENCE）。
# 换言之这道闸门等价于「只用结构化来源做量级判定」，把 L2 OCR(0.80) 与
# L3 VLM(0.70) 的读数排除在量级判定之外 —— 它们可以用来**发现**线索，
# 但不该用来**推翻**草稿里明确声明的数字。
#
# 实测依据见 check_units 中的说明（分部明细行把 3.49 亿元判成单位错误）。
_MAGNITUDE_JUDGE_MIN_CONFIDENCE = 0.95


def _unit_family(unit: str | None) -> str:
    """把单位归类到量纲族：money / share / ratio / per_share / other。"""
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


def load_metric_aliases(configs: dict[str, Any] | None = None) -> dict[str, str]:
    """从 configs/metrics.yaml 构建 ``别名 -> 规范名`` 映射。

    三个环节共用同一份指标定义，因此不可能出现
    「抽取时叫营收、核查时叫营业收入」这种不一致。
    """
    if configs is None:
        try:
            from agent.config import load_config

            configs = load_config("metrics")
        except Exception:
            configs = {}
    alias_map: dict[str, str] = {}
    for m in configs.get("metrics", []):
        canonical = m["canonical"]
        alias_map[m.get("label", canonical)] = canonical
        for a in m.get("aliases", []):
            alias_map[a] = canonical
    return alias_map


def _detect_section(text: str, pos: int) -> str | None:
    """向前找最近的标题行，作为所属章节。"""
    head = text[:pos]
    lines = head.splitlines()
    for line in reversed(lines[-60:]):
        s = line.strip()
        if not s:
            continue
        # 形如 "3.2 估值分析" 或 "## 估值分析" 或 "三、盈利预测"
        if (
            s.startswith("#")
            or (len(s) < 60 and (s[:1].isdigit() or s[:1] in "一二三四五六七八九十"))
            and ("、" in s[:4] or "." in s[:4])
        ):
            return s.lstrip("# ").strip()
    return None


def extract_claims(
    text: str,
    *,
    alias_map: dict[str, str] | None = None,
    window: int = 40,
) -> list[Claim]:
    """从报告草稿中抽取数值主张。

    会过滤掉明显不是财务数值的内容：年份、章节号、页码、纯序号。
    """
    import re

    aliases = alias_map if alias_map is not None else load_metric_aliases()
    # 别名按长度降序，保证「归属于母公司股东的净利润」优先于「净利润」
    aliases_sorted = sorted(aliases.items(), key=lambda kv: -len(kv[0]))

    claims: list[Claim] = []
    for m in re.finditer(_CLAIM_PATTERN, text):
        num_raw = m.group("num")
        unit_raw = normalize_unit(m.group("unit"))
        val = parse_decimal(num_raw)
        if val is None:
            continue

        # —— 噪声过滤 ——
        if not unit_raw:
            # 无单位的裸数字：只有整数且像年份/序号的，丢弃
            if val == val.to_integral_value() and _MIN_YEAR <= val <= _MAX_YEAR:
                continue
            if abs(val) < 1000 and val == val.to_integral_value():
                # 可能是「3 家」「5 个」这类计数，跳过
                ctx = text[max(0, m.start() - 8) : m.start()]
                if not any(k in ctx for k in ("共", "约", "为", "达", "至", "有")):
                    continue

        start, end = m.start(), m.end()
        context = text[max(0, start - window) : min(len(text), end + window)]

        # 指标识别：优先用紧邻数字左侧的词，退化为整段上下文
        left = text[max(0, start - 24) : start]
        hint = None
        for alias, canonical in aliases_sorted:
            if alias in left:
                hint = canonical
                break
        if hint is None:
            for alias, canonical in aliases_sorted:
                if alias in context:
                    hint = canonical
                    break

        claims.append(
            Claim(
                text=num_raw,
                value=val,
                unit=unit_raw,
                start=start,
                end=end,
                section=_detect_section(text, start),
                metric_hint=hint,
                context=context,
            )
        )
    return claims


# ==========================================================================
# Markdown 表格：列期核查
# ==========================================================================

_MD_ROW = re.compile(r"^\s*\|(.+)\|\s*$", re.M)
_MD_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def _md_cells(line: str) -> list[str]:
    """拆一行 Markdown 表格为单元格文本。"""
    inner = line.strip().strip("|")
    return [c.strip() for c in inner.split("|")]


def parse_markdown_tables(text: str) -> list[dict[str, Any]]:
    """抽出草稿里的 Markdown 表格，给出**每个单元格的字符区间**与所在列。

    返回每张表::

        {
          "start": 表格首字符偏移, "end": 末字符偏移,
          "header": [表头单元格...],
          "col_periods": {列号: 期间标签},     # 从表头推出的
          "rows": [ {"label": str, "cells": [{"text","value","start","end"}]} ]
        }

    为什么要做到"单元格级"：601857 草稿的表格写成

        | 项目 | 2025 年 | 2024 年 |
        | 资产总计（亿元） | 27530.07 | 27530.07 |

    而 27,530.07 亿元是 **2024 年末**的资产总计（2025 年末为 28,280.17 亿元）。
    要发现这类"同一列里两期数据错配"，必须知道**每个数字属于哪一列**，
    而滑动窗口给不出这个信息 —— 实测窗口会把表头截断或错位：
    同一张表里 `28644.69`（2025 年列）的窗口抓到了 `2024 年`，
    而 `27530.07` 的窗口抓到了 `2025 年末`。**期间只能从表格结构读，不能从窗口猜。**
    """
    tables: list[dict[str, Any]] = []
    lines = [(m.start(), m.group(0)) for m in _MD_ROW.finditer(text)]
    i = 0
    while i < len(lines):
        start, line = lines[i]
        if _MD_SEP.match(line):
            i += 1
            continue
        # 表头行 + 分隔行 才算一张表
        if i + 1 >= len(lines) or not _MD_SEP.match(lines[i + 1][1]):
            i += 1
            continue

        header = _md_cells(line)
        col_periods: dict[int, str] = {}
        for j, h in enumerate(header):
            found = detect_periods(h)
            if len(found) == 1:
                col_periods[j] = next(iter(found))

        rows: list[dict[str, Any]] = []
        j = i + 2
        end = lines[i + 1][0] + len(lines[i + 1][1])
        while j < len(lines) and not _MD_SEP.match(lines[j][1]):
            rstart, rline = lines[j]
            cells_text = _md_cells(rline)
            cells: list[dict[str, Any]] = []
            cursor = rstart + 1
            for cell in cells_text:
                idx = text.find(cell, cursor)
                if idx < 0:
                    idx = cursor
                cells.append({
                    "text": cell, "start": idx, "end": idx + len(cell),
                    "value": parse_decimal(cell),
                })
                cursor = idx + len(cell)
            rows.append({"label": cells_text[0] if cells_text else "", "cells": cells})
            end = rstart + len(rline)
            j += 1

        tables.append({
            "start": start, "end": end, "header": header,
            "col_periods": col_periods, "rows": rows,
        })
        i = j
    return tables


def check_table_periods(
    text: str, ledger: FactLedger, *, tolerance: Decimal = Decimal("0.02"),
) -> CheckResult:
    """E4：**表格列期核查** —— 单元格的值是否真的属于它所在列的那一期。

    判据（三重条件，缺一不可）：

    1. 表头里有**明确的期间**（`2025 年` / `2025 年末`）→ 知道这一列该放哪一期
    2. 该行的行标签能映射到规范指标 → 知道这个数是什么
    3. 单元格的值与**另一期**的事实相符，且与**本列期间**的事实**不符**
       → 两期数据错配

    条件 3 刻意要求"本列期间的事实**存在且不符**"，而不是"没找到本期的数" ——
    前者说明本期有正确的数、草稿填错了；后者可能只是本期没抽到，
    报出来就是误报（宁缺勿错）。

    ⚠️ **实测状态：判据正确，但当前数据下不触发**（2026-09）。
    601857 草稿的表格确实写错了：

        | 项目 | 2025 年 | 2024 年 |
        | 资产总计（亿元） | 27530.07 | 27530.07 |   ← 真值 28280.17 / 27530.07

    但事实库里**同时存在** `total_assets 2025A = 2,753,007,000,000`
    （这本身就是错的 —— 2024 年的数被标成了 2025A），
    于是"本期的数"与单元格一致，条件 3 不成立。

    **这不是判据的问题，是 R1 期间标注的问题**：R5 只能比对，
    比对基准本身错了它无能为力。修法在 R1 侧。
    """
    res = CheckResult()
    seq = 0
    aliases = load_metric_aliases()

    def canonical(label: str) -> str | None:
        best, best_len = None, 0
        for alias, canon in aliases.items():
            if len(alias) > best_len and alias in label:
                best, best_len = canon, len(alias)
        return best

    for table in parse_markdown_tables(text):
        if len(table["col_periods"]) < 1:
            continue
        for row in table["rows"]:
            metric = canonical(row["label"])
            if not metric:
                continue
            facts = _usable_facts(ledger, metric)
            if not facts:
                continue
            # 只用**跨期互证**过的事实做判据 —— 单点出现的取值可能是分部/明细行
            # （见 `_corroborated` 的说明）。这一条把一条确认级误报挡在门外。
            trusted = _corroborated(facts)
            if not trusted:
                continue
            for j, cell in enumerate(row["cells"]):
                if j == 0 or cell["value"] is None:
                    continue
                want_period = table["col_periods"].get(j)
                if not want_period:
                    continue
                cell_yuan = _claim_value_in_yuan(cell["value"], row["label"], cell["text"])
                if cell_yuan is None:
                    continue

                same_period = [
                    f for f in trusted if str(f.period) == want_period
                    and _unit_family(f.unit) == "money"
                ]
                if not same_period:
                    continue          # 本期无数：不报（见文档字符串条件 3）
                if any(
                    _fact_in_yuan(f) is not None
                    and approx_equal(cell_yuan, _fact_in_yuan(f), rel_tol=tolerance)
                    for f in same_period
                ):
                    continue          # 与本期一致 —— 没问题

                # 本期有数却不符 → 看它是不是对上了**另一期**
                for other in trusted:
                    if str(other.period) == want_period:
                        continue
                    ov = _fact_in_yuan(other)
                    if ov is None or not approx_equal(cell_yuan, ov, rel_tol=tolerance):
                        continue
                    seq += 1
                    idx = cell["start"]
                    claim = Claim(
                        text=cell["text"], value=cell["value"], unit="亿元",
                        start=idx, end=idx + len(cell["text"]),
                        section=_detect_section(text, idx), metric_hint=metric,
                        context=f"{row['label']} | {cell['text']}",
                    )
                    res.add(
                        _finding(
                            prefix="E4", seq=seq, error_type=ErrorType.E4_CITATION,
                            severity=Severity.CONFIRMED, claim=claim,
                            reason=(
                                f"表格「{row['label']}」的 "f"{want_period} 列填了 "
                                f"{cell['text']}，但该值是原始材料中 **{other.period}** 的数"
                                f"（{q(other.value)} {other.unit}）；"
                                f"{want_period} 的值应为 "
                                f"{q(same_period[0].value)} {same_period[0].unit}"
                                " —— 报告期错配"
                            ),
                            correct_value=f"{q(same_period[0].value)} {same_period[0].unit}",
                            suggestion=(
                                f"把 {want_period} 列改为 "
                                f"{q(same_period[0].value)} {same_period[0].unit}"
                            ),
                            confidence=0.85,
                            detector="rule.E4.table_period",
                            source_evidence=same_period[0].evidence_span_ids,
                            caliber=same_period[0].caliber_key,
                        )
                    )
                    res.checked += 1
                    break
    return res


def _corroborated(facts: list[Fact], tol: Decimal = Decimal("0.02")) -> list[Fact]:
    """筛出**被跨期互证**过的事实：其取值同时出现在另一个期间。

    为什么需要它（实测误报，2026-09）：601857 的「营业收入」在事实库里有
    `2024A = 52,221,000,000`（其实是**分部收入明细行**被抽成了营业收入）。
    表格列期核查一开始拿它当"2024A 的正确值"，于是把
    `29379.81 亿元`（**正确的** 2024 年收入）报成"填了 2025 年的数" ——
    凭空造出一条确认级误报。

    判据：集团口径的科目在年报里会**跨期出现**（2025 年报同时印 2025 与 2024
    两列），而分部/明细行只在一处出现。因此"某个取值同时出现在另一个期间"
    是"这是该科目真实、可比的历史序列"的证据。

    这正是本项目反复用到的那条原则：**优先相信"多处独立一致"，
    而不是"单点存在"**。
    """
    out: list[Fact] = []
    for f in facts:
        fv = _fact_in_yuan(f)
        if fv is None:
            continue
        for other in facts:
            if str(other.period) == str(f.period):
                continue
            ov = _fact_in_yuan(other)
            if ov is not None and approx_equal(fv, ov, rel_tol=tol):
                out.append(f)
                break
    return out


def _claim_value_in_yuan(value: Decimal, label: str, cell_text: str) -> Decimal | None:
    """表格单元格的数值换算到「元」。

    表格里的单位常写在**行标签**里（`资产总计（亿元）`），单元格本身只有数字。
    取行标签里的单位；取不到时按"元"处理（事实库也以元为准）。
    """
    unit = ""
    m = re.search(r"[（(]([^）)]*?(?:元|万元|亿元|千元))[）)]", label or "")
    if m:
        unit = m.group(1)
    if not unit:
        for u in ("亿元", "万元", "千元", "元"):
            if u in (label or ""):
                unit = u
                break
    try:
        return to_yuan(value, unit) if unit else value
    except Exception:  # noqa: BLE001
        return None


# ==========================================================================
# 结果容器
# ==========================================================================


@dataclass
class CheckResult:
    findings: list[Finding] = field(default_factory=list)
    checked: int = 0
    notes: list[str] = field(default_factory=list)

    def add(self, f: Finding) -> None:
        self.findings.append(f)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "finding_count": len(self.findings),
            "notes": self.notes,
            "findings": [f.model_dump(mode="json") for f in self.findings],
        }


def _finding(
    *,
    prefix: str,
    seq: int,
    error_type: ErrorType,
    severity: Severity,
    claim: Claim | None,
    reason: str,
    suggestion: str,
    confidence: float,
    detector: str,
    correct_value: str | None = None,
    source_evidence: list[str] | None = None,
    caliber: str | None = None,
    quote: str | None = None,
) -> Finding:
    loc = FindingLocation(
        section=claim.section if claim else None,
        char_range=(claim.start, claim.end) if claim else None,
        quote=(quote or (claim.context if claim else "") or "（无上下文）"),
    )
    return Finding(
        finding_id=f"{prefix}-{seq:03d}",
        error_type=error_type,
        severity=severity,
        location=loc,
        reported_value=claim.text + (claim.unit or "") if claim else None,
        correct_value=correct_value,
        source_evidence=source_evidence or [],
        caliber=caliber,
        reason=reason,
        suggestion=suggestion,
        confidence=confidence,
        detector=detector,
    )


# ==========================================================================
# E1 单位核查
# ==========================================================================


def check_units(
    claims: Sequence[Claim],
    ledger: FactLedger,
    *,
    tolerance: Decimal = Decimal("0.02"),
) -> CheckResult:
    """E1：单位与量级核查。

    判据（任一命中即报）：
      1. 指标已识别：数值与事实库**系数相同**但单位不同（万元写成亿元）
      2. 指标已识别：换算后量级差恰为 10^4 / 10^8（万元/亿元混用）
      3. 指标未识别：数值能在事实库中**按系数**找到对应项，但单位不同
      4. 比率类指标的小数/百分数量纲错误（0.1234 写成 12.34）

    判据 3 是实测补上的：草稿里「营业收入 120.04 亿元」的指标归属
    可能失败（上下文被截断），但数值 120.04 恰好是事实库中
    120,045,000,000 元的万分之一 —— 这个「系数相同、单位不同」的
    巧合本身就是单位错误的强证据，不应因归属失败而漏报。
    """
    res = CheckResult()
    seq = 0

    # 预建索引：事实值 -> [(fact, 该值对应「元」的量)]，用于判据 3
    fact_by_value: list[tuple[Fact, Decimal]] = []
    for f in ledger.all():
        if f.needs_review:
            continue
        fact_by_value.append((f, f.value))

    for c in claims:
        facts: list[Fact] = []
        if c.metric_hint:
            facts = _usable_facts(ledger, c.metric_hint)

        # ★ 先判「这个数**对不对**」，再判「单位**错没错**」。
        #
        #   实测误报（2026-09，占了 E1 误报的全部）：000722 草稿写
        #   「营业收入 3.49 亿元」（= 349,165,168.51 元，**正确**），
        #   事实库里同一份文档内还有另一条 `revenue = 96,562.33 元`
        #   （某张明细表的序号行被抽成了营业收入）。两者比值恰为 10^4，
        #   于是判据 2「典型万元/亿元混用」命中，
        #   系统建议把正确的 3.49 亿元改成「96,562.33 元」。
        #
        #   旧流程是"遍历候选、命中任一条判据就报"，于是**只要存在一条
        #   荒谬的候选，正确的草稿也会被报错**。这与核查的目的正好相反：
        #   核查要回答的是"草稿错了吗"，而不是"能不能在事实库里
        #   找到一条与草稿差 10^4 的数"。
        #
        #   正确顺序：**换算成元后能对上任何一条候选 → 这个数没问题，不报。**
        #   只有对不上任何候选时，才轮到量级类判据去判断"是不是单位写错了"。
        claim_yuan_pre = c.in_yuan
        if claim_yuan_pre is not None:
            matched = False
            for f in facts:
                fy = _fact_in_yuan(f)
                if fy is None or _unit_family(f.unit) != _unit_family(c.unit):
                    continue
                if approx_equal(claim_yuan_pre, fy, rel_tol=tolerance):
                    matched = True
                    break
            if matched:
                continue        # 对上了 —— 这一条主张没有问题，整条跳过

        # 判据 3：指标未识别时，按数值系数反查
        matched_via_value = False
        if not facts and normalize_unit(c.unit) in _MONEY_UNITS:
            for f, fval in fact_by_value:
                if normalize_unit(f.unit) not in _MONEY_UNITS:
                    continue
                if fval == 0 or c.value == 0:
                    continue
                # 量级闸门：金额之间至少差 10 倍才可能是「同一数值不同单位」。
                # 少了这道闸门，比率数字会被拿去和金额比对。
                ratio = max(abs(c.value), abs(fval)) / max(
                    Decimal("1e-12"), min(abs(c.value), abs(fval))
                )
                if ratio < _VALUE_LOOKUP_MIN_RATIO:
                    continue
                if approx_equal(c.value, fval, rel_tol=Decimal("0.001")):
                    facts = [f]
                    matched_via_value = True
                    break

        if not facts:
            continue
        res.checked += 1

        for f in facts:
            # 只在单位同为金额或同为数量时比对
            fact_yuan = _fact_in_yuan(f)
            claim_yuan = c.in_yuan
            same_family = _unit_family(c.unit) == _unit_family(f.unit)

            # ★ 量级类判据（情形 2）**不得**由低置信度的模型读数触发。
            #
            #   实测误报（2026-09，占比很高）：000722 草稿写「营业收入 3.49 亿元」，
            #   事实库里有一条 `revenue = 92,538.62 元`（VLM 整页扫描读出，
            #   置信度 0.80，主体是「服务业及其他」—— 其实是**分部收入明细行**）。
            #   两者比值恰为 10^4，于是判据 2「典型万元/亿元混用」命中，
            #   系统建议把 3.49 亿元改成「92,538.62 元」。
            #
            #   判据 2 的逻辑是「差 10^4/10^8 说明单位用错」，它默认
            #   **候选事实是对的**。当候选事实来自模型的整页扫描、而非
            #   结构化表格直取时，这个默认不成立 —— 让一条 0.80 置信度的
            #   明细行去推翻草稿里明确声明的金额，是把误报当成了发现。
            #   0.95 是 L1 文本层/表格网格直取的基础分（见 SOURCE_LAYER_BASE_CONFIDENCE），
            #   因此这道闸门等价于「只用结构化来源做量级判定」。
            if same_family and f.confidence < _MAGNITUDE_JUDGE_MIN_CONFIDENCE:
                continue

            if fact_yuan is not None and claim_yuan is not None and same_family:
                # 情形 1：数值本身相同但单位不同 -> 典型的单位错误
                if approx_equal(c.value, f.value, rel_tol=Decimal("0.01")):
                    if normalize_unit(c.unit) != normalize_unit(f.unit):
                        seq += 1
                        res.add(
                            _finding(
                                prefix="E1", seq=seq, error_type=ErrorType.E1_UNIT,
                                severity=Severity.CONFIRMED,
                                claim=c,
                                reason=(
                                    f"数值 {c.value} 与原始材料一致，但单位标注为 "
                                    f"『{c.unit}』，原始材料为『{f.unit}』。"
                                    f"换算后相差 {_fmt_gap(claim_yuan, fact_yuan)}"
                                ),
                                correct_value=f"{q(f.value)} {f.unit}",
                                suggestion=f"改为『{q(f.value)} {f.unit}』",
                                confidence=0.95,
                                detector="rule.E1.unit_mismatch",
                                source_evidence=f.evidence_span_ids,
                                caliber=f.caliber_key,
                            )
                        )
                        break

                # 情形 2：量级差恰好是 10^4 / 10^8 -> 单位错用的强信号
                gap = magnitude_gap(claim_yuan, fact_yuan)
                if gap is not None and abs(gap) in (4, 8) and not approx_equal(
                    claim_yuan, fact_yuan, rel_tol=tolerance
                ):
                    seq += 1
                    res.add(
                        _finding(
                            prefix="E1", seq=seq, error_type=ErrorType.E1_UNIT,
                            severity=Severity.CONFIRMED,
                            claim=c,
                            reason=(
                                f"报告值 {c.text}{c.unit}（≈{q(claim_yuan)} 元）与原始材料 "
                                f"{q(f.value)} {f.unit}（≈{q(fact_yuan)} 元）相差 10^{abs(gap)} 量级，"
                                "属于典型的万元/亿元单位混用"
                            ),
                            correct_value=f"{q(f.value)} {f.unit}",
                            suggestion=(
                                f"改为『{q(f.value)} {f.unit}』，"
                                f"或换算为『{q(from_yuan(fact_yuan, '亿元'), 4)} 亿元』"
                            ),
                            confidence=0.93,
                            detector="rule.E1.magnitude_gap",
                            source_evidence=f.evidence_span_ids,
                        )
                    )
                    break

                # 情形 2b：量级差恰为 1 个数量级（小数点错一位）
                #
                # 实测漏报（2026-09）：000722 草稿写「负债合计 36.5 亿元」，
                # 合并资产负债表披露 364,789,378.77 元 = 3.65 亿元 ——
                # 正好差 10 倍。而判据 2 只认 10^4/10^8（万元/亿元混用），
                # 于是这类"小数点错一位"完全检查不到。
                #
                # ⚠️ 10 倍与 10^4 有本质区别，必须额外加闸门：
                #   几亿元的科目在年报里遍地都是，**随手一条候选都可能恰差 10 倍**。
                #   实测反例：601857 草稿写「国际准则归母净利润 1837.55 亿元」，
                #   与某条 46,807,000,000 的候选（固定资产？）恰好差 10 倍，
                #   于是报出"小数点错一位"—— 而真实问题是准则口径（IFRS vs CAS）。
                #   另一例：分红 457.55 亿元与 IFRS 净利润 1837.55 亿元恰好差 4 倍，
                #   在四舍五入的量级差下也落在 10 倍附近。
                #
                #   闸门：草稿值与 **候选整体的量级（中位数）** 必须相差 ≥ 5 倍。
                #   真的错一位时，草稿值会远离所有候选；巧合命中时，
                #   草稿值仍落在候选的正常范围内。
                if gap is not None and abs(gap) == 1 and not approx_equal(
                    claim_yuan, fact_yuan, rel_tol=tolerance
                ):
                    med = _median_abs_yuan(facts)
                    far = (
                        med is None
                        or abs(claim_yuan) >= abs(med) * Decimal("5")
                        or abs(claim_yuan) <= abs(med) / Decimal("5")
                    )
                    if far:
                        seq += 1
                        res.add(
                            _finding(
                                prefix="E1", seq=seq, error_type=ErrorType.E1_UNIT,
                                severity=Severity.SUSPECTED,
                                claim=c,
                                reason=(
                                    f"报告值 {c.text}{c.unit}（≈{q(claim_yuan)} 元）与原始材料 "
                                    f"{q(f.value)} {f.unit}（≈{q(fact_yuan)} 元）相差约 10 倍，"
                                    f"且偏离该指标候选的整体量级（中位数 ≈{q(med)} 元）"
                                    f"达 5 倍以上，疑似小数点或数量级错一位。"
                                    "合并数与母公司数也可能有此差距，需人工确认口径。"
                                ),
                                correct_value=f"{q(f.value)} {f.unit}",
                                suggestion=f"核对是否应为『{q(f.value)} {f.unit}』",
                                confidence=0.6,
                                detector="rule.E1.decimal_shift",
                                source_evidence=f.evidence_span_ids,
                            )
                        )
                        break

        # 情形 3：比率类指标的量纲错误（0.1234 写成 12.34）
        for f in facts:
            if (
                _unit_family(f.unit) == "ratio"
                and _unit_family(c.unit) == "ratio"
                and normalize_unit(f.unit) in ("%", "‰")
                and normalize_unit(c.unit) == "%"
            ):
                if approx_equal(c.value, f.value * 100, rel_tol=Decimal("0.01")) and not approx_equal(
                    c.value, f.value, rel_tol=Decimal("0.05")
                ):
                    seq += 1
                    res.add(
                        _finding(
                            prefix="E1", seq=seq, error_type=ErrorType.E1_UNIT,
                            severity=Severity.CONFIRMED,
                            claim=c,
                            reason=(
                                f"报告写作 {c.text}%，而原始材料记录为 {f.value}（小数形式），"
                                f"换算后应为 {q(f.value * 100)}%"
                            ),
                            correct_value=f"{q(f.value * 100)}%",
                            suggestion=f"改为『{q(f.value * 100)}%』",
                            confidence=0.90,
                            detector="rule.E1.ratio_scale",
                            source_evidence=f.evidence_span_ids,
                        )
                    )
                    break

    return res


def _fact_in_yuan(f: Fact) -> Decimal | None:
    from agent.validate.numeric import to_yuan

    try:
        return to_yuan(f.value, f.unit)
    except Exception:
        return None


def _fmt_gap(a: Decimal, b: Decimal) -> str:
    if b == 0:
        return "不可比"
    return f"{q(a / b, 4)} 倍"


# ==========================================================================
# E2 估值倍数核查
# ==========================================================================


@dataclass
class MultipleCheckInput:
    """估值倍数核查所需的参考数据（来自事实库）。

    ``source_fact_ids`` 记录这些数字各自来自哪条事实 ——
    这是必需的，因为 ``Finding`` 的契约规定：**CONFIRMED 级发现必须携带
    来源证据**，否则应降级为 SUSPECTED。没有来源的「确认错误」
    本质上是猜测，不能标为确认。
    """

    price: Decimal | None = None
    eps_ttm: Decimal | None = None
    eps_forward: Decimal | None = None
    bvps: Decimal | None = None
    market_cap_total: Decimal | None = None
    market_cap_free_float: Decimal | None = None
    ev: Decimal | None = None
    ebitda: Decimal | None = None
    net_debt: Decimal | None = None
    net_profit_parent: Decimal | None = None
    net_profit_parent_ex_nonrec: Decimal | None = None

    source_fact_ids: dict[str, str] = field(default_factory=dict)
    """字段名 -> fact_id。例如 ``{"net_profit_parent": "fct_ab12"}``。"""

    def evidence_for(self, *fields: str) -> list[str]:
        """取若干字段对应的事实 ID（去重、去空）。"""
        out: list[str] = []
        for f in fields:
            fid = self.source_fact_ids.get(f)
            if fid and fid not in out:
                out.append(fid)
        return out


# 报告中倍数写法的上下文关键词 -> 口径
_MULTIPLE_CONTEXT = {
    "静态": "static",
    "ttm": "ttm",
    "TTM": "ttm",
    "滚动": "ttm",
    "动态": "forward",
    "预测": "forward",
    "forward": "forward",
}


def check_multiples(
    text: str,
    ref: MultipleCheckInput,
    *,
    claims: Sequence[Claim] | None = None,
    tolerance: Decimal = Decimal("0.03"),
) -> CheckResult:
    """E2：估值倍数核查。

    三类典型错误（官方明确考察）：
      * 分母口径错：用归母算「PE」却拿扣非净利润做分母
      * 时点错配：用历史 EPS 计算却标注为「动态 PE」
      * 市值/EV 口径错：PE 用总市值、EV/EBITDA 未扣净债

    做法：从事实库**独立重算**倍数，与报告值比对。
    """
    res = CheckResult()
    seq = 0
    claims = claims if claims is not None else extract_claims(text)

    # —— 预先算出各口径的正确倍数 ——
    correct: dict[str, tuple[Decimal, str]] = {}
    if ref.market_cap_total and ref.net_profit_parent and ref.net_profit_parent > 0:
        correct["pe_static"] = (
            ref.market_cap_total / ref.net_profit_parent,
            f"总市值 {q(ref.market_cap_total)} ÷ 归母净利润 {q(ref.net_profit_parent)}",
        )
    if ref.market_cap_total and ref.net_profit_parent_ex_nonrec and ref.net_profit_parent_ex_nonrec > 0:
        correct["pe_ex_nonrec"] = (
            ref.market_cap_total / ref.net_profit_parent_ex_nonrec,
            f"总市值 {q(ref.market_cap_total)} ÷ 扣非归母净利润 {q(ref.net_profit_parent_ex_nonrec)}",
        )
    if ref.price and ref.eps_ttm and ref.eps_ttm > 0:
        correct["pe_ttm"] = (ref.price / ref.eps_ttm, f"股价 {q(ref.price)} ÷ TTM每股收益 {q(ref.eps_ttm)}")
    if ref.price and ref.eps_forward and ref.eps_forward > 0:
        correct["pe_forward"] = (
            ref.price / ref.eps_forward,
            f"股价 {q(ref.price)} ÷ 预测每股收益 {q(ref.eps_forward)}",
        )
    if ref.price and ref.bvps and ref.bvps > 0:
        correct["pb"] = (ref.price / ref.bvps, f"股价 {q(ref.price)} ÷ 每股净资产 {q(ref.bvps)}")
    if ref.ev and ref.ebitda and ref.ebitda > 0:
        correct["ev_ebitda"] = (ref.ev / ref.ebitda, f"EV {q(ref.ev)} ÷ EBITDA {q(ref.ebitda)}")

    if ref.market_cap_free_float and ref.net_profit_parent and ref.net_profit_parent > 0:
        correct["pe_free_float"] = (
            ref.market_cap_free_float / ref.net_profit_parent,
            "流通市值 ÷ 归母净利润（口径不一致，仅供参考对照）",
        )

    for c in claims:
        if normalize_unit(c.unit) != "倍":
            continue
        res.checked += 1
        ctx = c.context

        # 判定报告自述的口径
        stated: str | None = None
        for kw, basis in _MULTIPLE_CONTEXT.items():
            if kw in ctx:
                stated = basis
                break
        kind = "pe"
        if "市净率" in ctx or "PB" in ctx or "P/B" in ctx:
            kind = "pb"
        elif "EV/EBITDA" in ctx or "企业价值倍数" in ctx:
            kind = "ev_ebitda"

        # —— 时点/口径错配（结构性错误，无需数值比对即可判定）——
        if kind == "pe" and stated == "forward" and ref.eps_ttm and not ref.eps_forward:
            if correct.get("pe_ttm") and approx_equal(c.value, correct["pe_ttm"][0], rel_tol=tolerance):
                seq += 1
                res.add(
                    _finding(
                        prefix="E2", seq=seq, error_type=ErrorType.E2_MULTIPLE,
                        severity=Severity.CONFIRMED, claim=c,
                        reason=(
                            "报告标注为『动态 PE』（基于预测盈利），"
                            f"但数值 {c.value} 实际等于按历史 TTM 每股收益计算的静态 PE"
                            f"（{correct['pe_ttm'][1]} = {q(correct['pe_ttm'][0], 2)}）"
                        ),
                        correct_value=f"{q(correct['pe_ttm'][0], 2)} 倍（TTM口径）",
                        suggestion=(
                            "确认口径：若指动态 PE，需改用预测每股收益重算；"
                            "若数据为 TTM，请将表述改为『TTM PE』"
                        ),
                        confidence=0.88,
                        detector="rule.E2.basis_mismatch",
                        caliber="multiple_basis: forward vs ttm",
                    )
                )
                continue

        # —— 分母口径错：报告值更接近扣非口径但表述为归母（或反之）——
        if kind == "pe":
            pe_parent = correct.get("pe_static")
            pe_exnon = correct.get("pe_ex_nonrec")
            # 注意：near_parent 必须在最外层计算。
            # 它同时在「分母口径」与「市值口径」两处使用，
            # 若定义在 if 分支内，另一分支会触发 UnboundLocalError。
            near_parent = bool(
                pe_parent and approx_equal(c.value, pe_parent[0], rel_tol=tolerance)
            )

            if pe_parent and pe_exnon:
                if not near_parent and approx_equal(c.value, pe_exnon[0], rel_tol=tolerance):
                    seq += 1
                    res.add(
                        _finding(
                            prefix="E2", seq=seq, error_type=ErrorType.E2_MULTIPLE,
                            severity=Severity.CONFIRMED, claim=c,
                            reason=(
                                f"报告 PE={c.value} 倍，实为按扣非归母净利润计算"
                                f"（{pe_exnon[1]} = {q(pe_exnon[0], 2)}），"
                                f"但正文未标注『扣非』口径。按归母净利润计算应为 "
                                f"{q(pe_parent[0], 2)} 倍"
                            ),
                            correct_value=f"{q(pe_parent[0], 2)} 倍（归母口径）",
                            suggestion=(
                                "明确口径：若采用扣非净利润，须在文中标注『扣非后 PE』；"
                                "若采用归母净利润，请改用正确数值"
                            ),
                            confidence=0.85,
                            detector="rule.E2.denominator_caliber",
                            caliber="profit_basis: net_parent vs net_parent_ex_nonrec",
                            source_evidence=ref.evidence_for(
                                "market_cap_total",
                                "net_profit_parent",
                                "net_profit_parent_ex_nonrec",
                            ),
                        )
                    )
                    continue

            # —— 市值口径错 ——
            if pe_parent and ref.market_cap_free_float:
                pe_float = correct.get("pe_free_float")
                if pe_float and not near_parent and approx_equal(c.value, pe_float[0], rel_tol=tolerance):
                    seq += 1
                    res.add(
                        _finding(
                            prefix="E2", seq=seq, error_type=ErrorType.E2_MULTIPLE,
                            severity=Severity.CONFIRMED, claim=c,
                            reason=(
                                f"报告 PE={c.value} 倍系按**流通市值**计算"
                                f"（{q(ref.market_cap_free_float)} ÷ {q(ref.net_profit_parent or Decimal(0))}），"
                                f"而市场惯例 PE 使用总市值，应为 {q(pe_parent[0], 2)} 倍"
                            ),
                            correct_value=f"{q(pe_parent[0], 2)} 倍（总市值口径）",
                            suggestion="改用总市值计算，或在文中明确标注为『流通市值口径 PE』",
                            confidence=0.82,
                            detector="rule.E2.marketcap_caliber",
                            caliber="market_cap: total vs free_float",
                            source_evidence=ref.evidence_for(
                                "market_cap_total",
                                "market_cap_free_float",
                                "net_profit_parent",
                            ),
                        )
                    )
                    continue

        # —— 数值偏差（无法归因到具体口径）——
        expected = correct.get("pe" if kind == "pe" else kind)
        if expected and not approx_equal(c.value, expected[0], rel_tol=tolerance):
            if rel_diff(c.value, expected[0]) < Decimal("0.5"):  # 差异 < 50% 才报，避免噪声
                seq += 1
                res.add(
                    _finding(
                        prefix="E2", seq=seq, error_type=ErrorType.E2_MULTIPLE,
                        severity=Severity.SUSPECTED, claim=c,
                        reason=(
                            f"报告 {kind.upper()}={c.value} 倍，"
                            f"按原始材料重算为 {q(expected[0], 2)} 倍"
                            f"（{expected[1]}），偏差 {q(rel_diff(c.value, expected[0]) * 100, 1)}%"
                        ),
                        correct_value=f"{q(expected[0], 2)} 倍",
                        suggestion="核对分子分母的口径与时点后修正",
                        confidence=0.70,
                        detector="rule.E2.recompute_deviation",
                        source_evidence=ref.evidence_for(
                            "market_cap_total", "net_profit_parent", "price", "eps_ttm", "bvps",
                            "ebitda", "ev",
                        ),
                    )
                )

    return res


# ==========================================================================
# E3 统计口径核查
# ==========================================================================


_CALIBER_KEYWORDS: dict[str, list[str]] = {
    "consolidated": ["合并报表", "合并口径", "合并财务报表"],
    "parent_only": ["母公司报表", "母公司口径", "母公司财务报表"],
    "net_parent": ["归属于母公司", "归母", "归属于上市公司股东"],
    "net_parent_ex_nonrec": ["扣除非经常性损益", "扣非"],
    "duration_ytd": ["年初至报告期末", "累计", "前三季度", "上半年"],
    "duration_single_q": ["单季度", "本季度", "第一季度", "第二季度", "第三季度", "第四季度"],
}

# 「母公司」单独出现时的排除上下文。
# 这是实测踩出来的坑：「归属于母公司股东的净利润」里的「母公司」
# 指的是**合并报表中归属于母公司的那部分**，而不是「母公司报表口径」。
# 若不排除，每一条归母指标都会被误报为口径错误 —— 误报率直接爆表。
_PARENT_ONLY_EXCLUSIONS = (
    "归属于母公司",
    "归属于上市公司",
    "母公司股东",
    "母公司所有者",
)


def detect_statement_scope(text: str) -> str | None:
    """从表述中判定报告口径。返回 ``consolidated`` / ``parent_only`` / None。

    判定优先级：
        1. 明确出现「合并报表 / 合并口径」-> consolidated
        2. 明确出现「母公司报表 / 母公司口径」-> parent_only
        3. 其余含「母公司」的表述一律**不判定**（避免把归母当母公司的误报）
    """
    if any(k in text for k in _CALIBER_KEYWORDS["consolidated"]):
        return "consolidated"
    if any(k in text for k in _CALIBER_KEYWORDS["parent_only"]):
        return "parent_only"

    # 仅出现「母公司」时，检查是否属于「归属于母公司…」这类排除上下文
    if "母公司" in text:
        for m_start in range(len(text)):
            idx = text.find("母公司", m_start)
            if idx < 0:
                break
            m_start = idx
            # 向前看 5 个字，判断是否落在排除短语内
            head = text[max(0, idx - 5) : idx + 2]
            if any(exc in text[max(0, idx - 6) : idx + 6] for exc in _PARENT_ONLY_EXCLUSIONS):
                continue
            if not head.endswith("归属"):
                return "parent_only"
    return None


_PARENT_SCOPED_METRICS = frozenset({
    "equity_parent", "equity_parent_ex_minority",
})
"""**指标名里就写明"归属于母公司"**的权益类科目。

只有这些科目存在"表述说合计、数字却是归母"的真实错配风险：
「所有者权益合计」含少数股东权益，而「归属于母公司股东权益」不含，
两个数在同一份年报里都能查到，因此可以正确定性。

不放净利润类（`net_profit_parent`）：写「归属于母公司股东的净利润」
是标准写法，不是错配。也不放资产/现金流类：它们没有归母/合计之分。
"""


def _same_metric_family(a: str | None, b: str | None) -> bool:
    """两个规范指标名是否属于**同一族**（去口径后缀后词干相同）。

    ``equity_total`` ↔ ``equity_parent`` → True
    ``total_assets`` ↔ ``equity_parent`` → False

    用于 E3：只有同族才谈得上"合计口径 vs 归母口径"的错配。
    不限定族的话，任何主张都会去拿归母净资产比，制造大量假阳性
    （实测：一次 10 条）。
    """
    def stem(m: str) -> str:
        for suffix in ("_parent_ex_nonrec", "_parent", "_total", "_ex_nonrec"):
            if m.endswith(suffix):
                return m[: -len(suffix)]
        return m

    return bool(a) and bool(b) and stem(str(a)) == stem(str(b))


def _caliber_rank_local(fact: Fact) -> int:
    """事实口径的**特异性**评分（越大越特异）。

    与 R4 的同名函数判据一致（合并+归母 > 合并 > 母公司 > 未知），
    但这里是"特异"而非"优先"：`equity_parent`（归母净资产）比
    `equity_total`（所有者权益合计）**更特异** —— 所以"说合计却用了它"
    才是错配。反过来若两个口径一样特异，就只是两条并列候选，不能定性。
    """
    score = 0
    if fact.statement_scope in (
        StatementScope.CONSOLIDATED,
        # 分部报告的「合计」列 = 集团口径（2026-09 第十三轮新增）。
        # 不把它算作集团口径，则 601857 这类"资本开支只在分部报告里"的
        # 指标会被判成"说合计却用了板块明细"，产出假阳性。
        StatementScope.SEGMENT_TOTAL,
    ):
        score += 2
    basis = getattr(getattr(fact, "profit_basis", None), "value", None)
    if basis in ("net_parent", "net_parent_ex_nonrec"):
        score += 1
    if fact.metric.endswith("_parent"):
        score += 1
    return score


def _is_total_wording(claim: Claim) -> bool:
    """该数值主张的上下文是否声明了「合计 / 总计」口径。

    只看**紧邻上下文**（`claim.context`），不看全文 ——
    全文里出现一次「合计」不代表这一句用了合计口径。
    实测：上下文窗口里可能混入同一句话里另一个数字的修饰语
    （「资产总计 37.97 亿元。其中所有者权益合计 …」），
    因此这里只做粗筛，精确判定交给 `_caliber_rank_local`。
    """
    ctx = claim.context or ""
    return any(w in ctx for w in ("合计", "总计", "总资产", "总负债"))


def _usable_facts(ledger: FactLedger, metric: str) -> list[Fact]:
    """取某指标**可用于比对**的事实：跳过 needs_review，并剔除"主体名是行标签"的。

    为什么要在这里过滤（实测，2026-09）：VLM 整页扫描会把**报表行标签**
    当成主体名写进事实 —— 例如

        entity='资产总计'            value=3,547,820,309.72  scope=consolidated
        entity='负债和所有者权益总计'   value=3,547,820,309.72  scope=consolidated
        entity='总资产（元）'         value=3,547,820,309.72  scope=consolidated

    这三条其实是**母公司报表**的数（合并数是 3,797,417,361.30），却被标成
    `consolidated`。后果不只是"多几条候选"：

        草稿写「资产总计 37.97 亿元」（= 3,547,820,309.72 的近似值）
        E1 的"先判对不对"步骤发现它与上述候选**对得上** → 判定该主张没问题 → 不报
        而它其实与合并口径的 3,797,417,361.30 差了 6.6%

    即：**一条口径标错的候选，足以让正确性判定整体失效**。
    主体名不像主体的那些事实，其口径标签同样不可信，因此一并排除。
    """
    aliases = sorted(load_metric_aliases().items(), key=lambda kv: -len(kv[0]))
    return [
        f for f in ledger.by_metric(metric)
        if not f.needs_review and _entity_name_is_plausible(f.entity, aliases)
    ]


def check_calibers(
    claims: Sequence[Claim],
    ledger: FactLedger,
    *,
    text: str = "",
) -> CheckResult:
    """E3：统计口径核查。

    每个 Fact 都带 ``caliber`` 标签（statement_scope / profit_basis / period_type）。
    报告使用某数字时，其上下文若声明了**不同**口径，即为口径错误。
    """
    res = CheckResult()
    seq = 0

    for c in claims:
        if not c.metric_hint:
            continue
        facts = _usable_facts(ledger, c.metric_hint)
        # ★ E3 需要**同时**看到合计口径与归母口径的候选。
        #
        #   实测缺口（2026-09）：草稿写「所有者权益合计 3154311963.85 元」，
        #   该值在事实库里是 `equity_parent`（归母净资产），而主张的
        #   `metric_hint` 是 `equity_total` —— 只按 hint 取候选，
        #   归母那条**根本进不了候选集**，"说合计却用了归母"就无从判定。
        #
        #   ⚠️ 但**必须限定同族**：一开始无差别把归母类指标加进所有主张的候选，
        #   结果 14 条「总资产/净利润」主张拿 `equity_parent` 去比，
        #   一次产生 10 条假阳性。同族 = 指标名去掉 `_parent` / `_total`
        #   后缀后词干相同（`equity_total` ↔ `equity_parent`）。
        for m in _PARENT_SCOPED_METRICS:
            if m != c.metric_hint and _same_metric_family(c.metric_hint, m):
                facts = facts + _usable_facts(ledger, m)
        if not facts:
            continue
        res.checked += 1

        stated_scope = detect_statement_scope(c.context)
        stated_basis = _match_keywords(
            c.context,
            _CALIBER_KEYWORDS["net_parent"] + _CALIBER_KEYWORDS["net_parent_ex_nonrec"],
        )

        for f in facts:
            # —— 报表范围错配 ——
            if stated_scope:
                want = stated_scope
                got = f.statement_scope.value
                if got != "unknown" and got != want:
                    seq += 1
                    scope_label = "合并报表" if want == "consolidated" else "母公司报表"
                    res.add(
                        _finding(
                            prefix="E3", seq=seq, error_type=ErrorType.E3_CALIBER,
                            severity=Severity.CONFIRMED, claim=c,
                            reason=(
                                f"报告表述为『{scope_label}』口径，但所引数字来源于"
                                f"『{'合并报表' if got == 'consolidated' else '母公司报表'}』"
                            ),
                            correct_value=f"按 {scope_label} 口径重新取数",
                            suggestion="统一口径：要么改用对应报表的数字，要么修改表述",
                            confidence=0.86,
                            detector="rule.E3.statement_scope",
                            source_evidence=f.evidence_span_ids,
                            caliber=f.caliber_key,
                        )
                    )
                    break

            # —— 同名指标的口径归属：说「合计」却用了「归母」——
            #
            #   实测漏报（2026-09）：000722 草稿写「所有者权益合计 3154311963.85 元」，
            #   而 3,154,311,963.85 在事实库里被正确标为 `equity_parent`
            #   （母公司资产负债表口径）；合并口径的
            #   `equity_total = 3,797,417,361.30` 才是「所有者权益合计」。
            #   两者相差 6.4 亿，表述与口径不符。
            #
            #   为什么这个判据可靠：`statement_scope` 由 R1 的
            #   「标题-表格矩形交叠」判定（见 EvidenceStore.scope_by_overlap），
            #   是一条有空间证据的结论，不是猜测。而"说合计却用归母数"这件事
            #   在**同一份年报里两个数都能查到**，因此可以正确定性。
            #
            #   只在「母公司数 与 合并数 差异显著（≥2%）」时才报 ——
            #   全资子公司两者的数本就相同，此时不构成实质错误。
            if _is_total_wording(c):
                for f in facts:
                    # ★ 判据落在**指标名**上，而不是 `statement_scope`。
                    #
                    #   为什么换掉 statement_scope：它由 R1 的空间交叠判定，
                    #   而年报里同一页上下并排「合并资产负债表」与「母公司资产负债表」，
                    #   实测会把两者的口径标签对调 —— 000722 的合并数
                    #   3,797,417,361.30（p76）与母公司数 3,547,820,309.72（p74）
                    #   **都被标成 parent_only**，于是"用口径区分归属"根本无从下手。
                    #
                    #   而**指标名是抽取时就定死的**：`equity_parent`
                    #   （归属于母公司股东的净资产）名字里就写明归属，
                    #   `equity_total`（所有者权益合计）写明是合计 ——
                    #   "表述说合计、数字却是 _parent 那条"是可直接判定的错配。
                    #
                    #   实测误报（放开到全部指标时一次报了 3 条假阳性）：
                    #   * `net_profit_parent` —— 「归属于母公司股东的净利润」
                    #     **本来就是**归母口径，草稿这样写没有任何问题；
                    #   * `operating_cash_flow` —— 数值主张的上下文窗口里恰好含
                    #     「总资产」二字（同一句话里另一个数字的修饰语）；
                    #   * `total_assets` —— 口径标签自相矛盾（见上）。
                    #   因此限定在**权益类**科目：只有这一类"归母 vs 合计"
                    #   的术语差别是真实且容易写错的。现金流量表没有归母概念，
                    #   净利润写"归属于母公司"是标准写法而非错配。
                    if f.metric not in _PARENT_SCOPED_METRICS:
                        continue
                    merged = [
                        g for g in facts
                        if g.metric not in _PARENT_SCOPED_METRICS
                        and abs(g.value - f.value) > abs(g.value) * Decimal("0.02")
                    ]
                    if not merged:
                        continue
                    g = max(merged, key=lambda x: abs(x.value))
                    seq += 1
                    res.add(
                        _finding(
                            prefix="E3", seq=seq, error_type=ErrorType.E3_CALIBER,
                            severity=Severity.CONFIRMED, claim=c,
                            reason=(
                                f"表述为『合计/总计』口径，但 {c.value} 在原始材料中对应的是"
                                f"**归属于母公司**口径（{f.metric}）；"
                                f"合计口径为 {q(g.value)} {g.unit}，"
                                f"相差 {q(abs(g.value - f.value))} {g.unit}"
                                f"（{abs(g.value - f.value) / abs(g.value) * 100:.1f}%）"
                            ),
                            correct_value=f"{q(g.value)} {g.unit}",
                            suggestion="改用合计口径数字，或把表述改为「归属于母公司」",
                            confidence=0.85,
                            detector="rule.E3.total_vs_parent",
                            source_evidence=g.evidence_span_ids,
                            caliber=g.caliber_key,
                        )
                    )
                    break

            # —— 利润口径错配 ——
            if stated_basis and f.metric in (
                "net_profit_parent", "net_profit_parent_ex_nonrec", "net_profit_total"
            ):
                want_ex = any(
                    k in stated_basis[0] for k in _CALIBER_KEYWORDS["net_parent_ex_nonrec"]
                )
                got_basis = f.profit_basis
                # 只报「说扣非、用了归母」与「说归母、用了扣非」两种真错配
                if want_ex and got_basis is ProfitBasis.NET_PARENT:
                    seq += 1
                    res.add(
                        _finding(
                            prefix="E3", seq=seq, error_type=ErrorType.E3_CALIBER,
                            severity=Severity.CONFIRMED, claim=c,
                            reason=(
                                "报告表述为『扣除非经常性损益后』，"
                                "但所引数字实为归母净利润（未扣非）"
                            ),
                            correct_value="改用扣非归母净利润，或删除『扣非』表述",
                            suggestion=(
                                "两种改法：① 补上扣非后净利润数值；"
                                "② 若确实引用归母净利润，删除『扣除非经常性损益后』字样"
                            ),
                            confidence=0.87,
                            detector="rule.E3.profit_basis",
                            source_evidence=f.evidence_span_ids,
                            caliber=f.caliber_key,
                        )
                    )
                    break
                if not want_ex and got_basis is ProfitBasis.NET_PARENT_EX_NONREC and "归母" in c.context:
                    seq += 1
                    res.add(
                        _finding(
                            prefix="E3", seq=seq, error_type=ErrorType.E3_CALIBER,
                            severity=Severity.CONFIRMED, claim=c,
                            reason="报告表述为『归母净利润』，但所引数字为扣非后净利润",
                            correct_value="改用归母净利润，或标注为『扣非后归母净利润』",
                            suggestion="按所引数字的实际口径修改表述",
                            confidence=0.85,
                            detector="rule.E3.profit_basis",
                            source_evidence=f.evidence_span_ids,
                        )
                    )
                    break

    return res


def _match_keywords(text: str, keywords: list[str]) -> list[str]:
    return [k for k in keywords if k in text]


# ==========================================================================
# E4 引用核查
# ==========================================================================


# 「派生型」表述上下文 —— 这些数字是从其他数据算出来的，
# 不能在事实库里直接找到同名来源，因此 E4 的「无来源」判定对它们不适用。
# 对这类数字应当由 E5（算术一致性）独立重算并比对。
_DERIVED_CONTEXT = (
    "同比", "环比", "较上年", "较上期", "增长", "下降", "上升", "减少",
    "占比", "占公司", "占其", "比重", "毛利率", "净利率", "收益率", "周转率",
)


_YEAR_IN_TEXT = re.compile(r"(20\d{2})\s*年")
"""从上下文里抓报告期年份（``2025 年`` / ``2025 年末``）。"""


def detect_periods(text: str) -> set[str]:
    """从文本里提取**报告期**标签（``{'2025A', '2024A'}``）。

    为什么需要它（实测漏报，2026-09）：601857 草稿的表格写成

        | 项目 | 2025 年 | 2024 年 |
        | 资产总计（亿元） | 27530.07 | 27530.07 |

    而 27,530.07 亿元是 **2024 年末**的资产总计（2025 年末为 28,280.17 亿元）。
    旧 `check_citations` **完全不看期间** —— 它在事实库里找到了
    2024 年的那条 2753007000000，于是判定"有来源"，放行。
    这就是为什么「同一列里两期数据错配」这类引用错误一直检测不到。

    返回的是期间标签集合，而不是单一年份：一句话里可能同时提到两期
    （「较上年度的 29379.81 亿元」），此时两个期间都算"声明过的"。
    """
    out: set[str] = set()
    for m in _YEAR_IN_TEXT.finditer(text or ""):
        out.add(f"{m.group(1)}A")
    return out


def _is_derived_claim(claim: Claim) -> bool:
    """判断该数值是否为「派生指标」（由其他数字计算而来）。

    这类数字的正确性要靠**重算**验证，而不是靠**查找来源**验证。
    若不加区分，E4 会把每一个合理的增速、占比都报成「无来源」——
    这是实测出现的最大一类误报。
    """
    ctx = claim.context or ""
    if any(k in ctx for k in _DERIVED_CONTEXT):
        return True
    # 比率/倍数类且没有直接对应指标时，通常也是派生量
    return normalize_unit(claim.unit) in ("%", "‰", "倍") and claim.metric_hint is None


_RATIO_METRIC_PAIRS = (
    ("net_profit_parent", "net_profit_total", "净利润", "net_profit"),
    ("equity_parent", "equity_total", "净资产", "equity"),
)
"""口径易混的指标对照：``(归母口径, 合计口径, 中文名, 词干)``。

用于 :func:`check_ratio_calibers`：草稿写一个比率（如「净利率约 2.73%」）
却没有说明口径时，检验它是用**归母数**还是**合计数**算出来的。
"""


def check_ratio_calibers(
    text: str, claims: Sequence[Claim], ledger: FactLedger,
) -> CheckResult:
    """E3：**比率类主张的口径未说明**核查。

    实测漏报（2026-09）：002716 草稿写「按 2025 年度数据计算，公司净利率约为 2.73%」。
    核对：3.4019 亿（**归母**净利润）÷ 124.4462 亿（营业收入）= 2.7336% —— 算式没错，
    但用的是**归属于母公司**口径的净利润，而文中**没有说明**这一点。
    规范的「净利率」分母口径是含少数股东损益的净利润，两者横向对比时不可比。

    判据（三重，全部可判定）：给定一个未说明口径的比率主张，
    遍历 (归母, 合计) 两种口径的候选，用**事实库的收入**反推分母应为多少，
    看哪一种口径能重现报告的比率：

    * 只有**归母**口径能重现 → 报「用了归母口径却未说明」
    * 只有合计口径能重现 → 没问题（默认口径就是合计）
    * 两者都能重现 → 差异不显著，**不报**（宁缺勿错）

    ⚠️ **实测状态：在 `runs/annual23` 上不触发**（2026-09）。原因不是判据错，
    而是上游数据质量挡住了输入：002716 的 `net_profit_total` 候选里
    **也有一条 340,185,327.64**（同一个数字被同时标成归母口径与合计口径），
    于是"两种口径都能重现报告的 2.73%"，判据按设计选择不报。

    这条判据保留的理由：它本身是可判定的、且已通过单元测试；
    一旦上游把口径标签修对，它就能直接生效。**在上游修好之前，
    它在真实数据上等于不工作** —— 这一点必须写在这里，
    否则会被误认为"已经覆盖了比率口径问题"。
    """
    res = CheckResult()
    seq = 0
    aliases = load_metric_aliases()

    usable = {}
    for kind, _total, _cn, _stem in _RATIO_METRIC_PAIRS:
        usable[kind] = _usable_facts(ledger, kind)
        usable[_total] = _usable_facts(ledger, _total)

    for c in claims:
        if normalize_unit(c.unit) not in ("%", "‰") or c.value is None:
            continue
        ctx = c.context or ""
        # 已说明归母口径 → 不是问题
        if any(k in ctx for k in ("归母", "归属于母公司", "归属于上市公司股东")):
            continue
        base = _usable_facts(ledger, "revenue")
        if not base:
            continue
        ratio = c.value / Decimal("100")

        for parent_m, total_m, cn, _stem in _RATIO_METRIC_PAIRS:
            parent_facts = [f for f in usable[parent_m] if _fact_in_yuan(f)]
            total_facts = [f for f in usable[total_m] if _fact_in_yuan(f)]
            if not parent_facts or not total_facts:
                continue

            def reproduces(targets: list[Fact]) -> Fact | None:
                """哪种口径的数 ÷ 收入能重现报告的比率。"""
                for f in targets:
                    fv = _fact_in_yuan(f)
                    for b in base:
                        bv = _fact_in_yuan(b)
                        if not fv or not bv or str(b.period) != str(f.period):
                            continue
                        if str(f.period) not in ctx and str(b.period) not in ctx:
                            continue
                        got = fv / bv
                        if abs(got - ratio) / ratio <= Decimal("0.03"):
                            return f
                return None

            hit_parent = reproduces(parent_facts)
            hit_total = reproduces(total_facts)
            if hit_parent is None or hit_total is not None:
                continue          # 不能重现 或 两口径都对得上 → 不报

            parent_v = _fact_in_yuan(hit_parent)
            if parent_v is None:
                continue

            # 该主张在草稿里的字符区间（按文本反查）
            idx = text.find(c.text, max(0, c.start - 4))
            if idx < 0:
                idx = c.start
            seq += 1
            res.add(
                _finding(
                    prefix="E3", seq=seq, error_type=ErrorType.E3_CALIBER,
                    severity=Severity.SUSPECTED,
                    claim=Claim(
                        text=c.text, value=c.value, unit=c.unit,
                        start=idx, end=idx + len(c.text),
                        section=c.section, metric_hint=parent_m, context=ctx,
                    ),
                    reason=(
                        f"比率 {c.text}{c.unit} 由**归属于母公司**口径的{cn}"
                        f"（{q(hit_parent.value)} {hit_parent.unit}，{hit_parent.period}）"
                        f"算出，而文中未说明口径；"
                        f"规范的{cn}率应使用含少数股东损益的合计口径"
                        " —— 横向对比时不可比"
                    ),
                    suggestion=f"改为「归母{cn}率」，或改用合计口径的{cn}重算",
                    confidence=0.7,
                    detector="rule.E3.ratio_caliber",
                    source_evidence=hit_parent.evidence_span_ids,
                    caliber=hit_parent.caliber_key,
                )
            )
            break
    return res


def check_citations(
    claims: Sequence[Claim],
    ledger: FactLedger,
    *,
    tolerance: Decimal = Decimal("0.02"),
    require_source: bool = True,
) -> CheckResult:
    """E4：引用与来源核查。

    两类问题：
      * 报告中的数字在事实库中**找不到任何来源**（可能是编造或来自未提交材料）
      * 同指标事实存在但报告值与所有来源都显著不符

    重要限制：**派生指标（增速、占比、比率）不适用「查来源」判定**，
    它们的正确性由 E5 独立重算负责。此处直接跳过，避免大面积误报。
    """
    res = CheckResult()
    seq = 0

    fact_values: list[tuple[Fact, Decimal]] = [(f, f.value) for f in ledger.all()]

    for c in claims:
        if not c.unit:
            continue
        if _is_derived_claim(c):
            res.notes.append(
                f"跳过派生指标主张（位置 {c.start}，单位 {c.unit}）："
                "增速/占比/比率类数字的正确性由 E5 重算负责，不适用来源查找"
            )
            continue
        res.checked += 1

        # 把主张换算到与事实库可比的量纲 —— 事实库统一存「元」「股」，
        # 而草稿可能写「万元」。不归一会让**正确**的数字被判为「无来源」，
        # 这是实测出现过的最大一类误报。
        target = c.in_yuan if _unit_family(c.unit) == "money" else c.value
        claim_family = _unit_family(c.unit)

        matched: Fact | None = None
        for f, fv in fact_values:
            if claim_family != _unit_family(f.unit):
                continue  # 量纲不同族，不可比
            cmp_v = _fact_in_yuan(f) if claim_family == "money" else fv
            if cmp_v is None or target is None:
                continue
            if approx_equal(target, cmp_v, rel_tol=tolerance, abs_tol=Decimal("0.01")):
                matched = f
                break

        if matched is not None:
            continue

        # 未匹配：若有同指标同量纲的事实但数值不符 -> 报「数值与来源不符」
        near_fact = None
        if c.metric_hint:
            same = [
                f for f in ledger.by_metric(c.metric_hint)
                if not f.needs_review and _unit_family(f.unit) == claim_family
            ]
            if same:
                near_fact = min(
                    same,
                    key=lambda f: abs(
                        (_fact_in_yuan(f) if claim_family == "money" else f.value)
                        - (target or Decimal(0))
                    ),
                )

        if near_fact is not None:
            near_val = (
                _fact_in_yuan(near_fact) if claim_family == "money" else near_fact.value
            )
            if target is not None and near_val and rel_diff(target, near_val) < Decimal("0.30"):
                continue  # 接近但被容差挡掉，属四舍五入，不报
            seq += 1
            res.add(
                _finding(
                    prefix="E4", seq=seq, error_type=ErrorType.E4_CITATION,
                    severity=Severity.CONFIRMED, claim=c,
                    reason=(
                        f"报告值 {c.text}{c.unit} 在原始材料中找不到对应来源；"
                        f"同指标唯一可查的数值为 {q(near_fact.value)} {near_fact.unit}"
                        f"（{near_fact.caliber_key}）"
                    ),
                    correct_value=f"{q(near_fact.value)} {near_fact.unit}",
                    suggestion="核对数据来源；若引用其他口径或期间，请显式标注来源与报告期",
                    confidence=0.80,
                    detector="rule.E4.no_matching_source",
                    source_evidence=near_fact.evidence_span_ids,
                    caliber=near_fact.caliber_key,
                )
            )
        elif require_source:
            seq += 1
            res.add(
                _finding(
                    prefix="E4", seq=seq, error_type=ErrorType.E4_CITATION,
                    severity=Severity.SUSPECTED, claim=c,
                    reason=(
                        f"报告值 {c.text}{c.unit} 在已提交的原始材料中无任何来源记录"
                        + (f"（指标：{c.metric_hint}）" if c.metric_hint else "")
                    ),
                    suggestion="补充出处（公告名称/报告期/页码），或删除该数字",
                    confidence=0.55,
                    detector="rule.E4.unsourced_number",
                )
            )

    return res


# ==========================================================================
# E5 算术一致性核查
# ==========================================================================

# 「A 为 B 的 C%」「A 占 B 的 C%」「较上年同期增长 C%」等表述。
# 每个模式**必须单独编译**：合并成一条 alternation 会导致
# 同名捕获组 `pct` 重复定义（re.error: redefinition of group name）。
_RATIO_PATTERNS: list[tuple[str, str]] = [
    (r"占[^。；\n]{0,20}?(?P<pct>\d+(?:\.\d+)?)%", "share"),
    (r"为[^。；\n]{0,20}?的\s*(?P<pct>\d+(?:\.\d+)?)%", "share"),
    (r"同比增长\s*(?P<pct>-?\d+(?:\.\d+)?)%", "yoy"),
    (r"较上年同期(?:增长|上升|下降|减少)\s*(?P<pct>-?\d+(?:\.\d+)?)%", "yoy"),
    (r"同比(?:增长|上升|下降|减少)\s*(?P<pct>-?\d+(?:\.\d+)?)%", "yoy"),
]


def _entity_name_is_plausible(
    entity: str | None, aliases_sorted: Sequence[tuple[str, str]]
) -> bool:
    """主体名是否**像**一个主体，而不是一条报表行标签。

    为什么需要它（实测事故，2026-09）：事实库里有 69 条事实的 `entity`
    其实是**报表行标签** ——

        '营业收入' / '归属于上市公司股东的净利润（元）' /
        '二、营业利润（亏损以“－”号填列）' / '减：所得税费用' …

    成因是整页文本抽取把行标签当成了主体名（共有 39 个这样的假主体）。
    它们破坏的是 `check_arithmetic` 的**按主体分组**逻辑：

        草稿写「归母净利润 0.72 亿元，同比增长 6.39%」（正确）
        事实库里有两条 net_profit_parent 两期序列：
          主体「湖南能源集团发展股份有限公司」→ 0.68 → 0.72  = +6.39%  ✓
          主体「归属于上市公司股东的净利润」→ 0.83 → 3.10  = +272%   ✗
        假主体名**字面出现在草稿片段里**，于是通过了"草稿点名了哪个主体"的
        检查，系统报出「应改为 272.00%」—— 用行标签算出来的假增速
        去否定正确的数字。

    判据：**主体名里不该出现指标词**。真实公司名（含简称、全称、人名）
    不会包含「营业收入」「净利润」「合计」「其中」这类报表用语。
    代价是可能滤掉极少数含这类词的怪名字 —— 相比"报出假错误"，
    这个代价可以接受（漏报 < 误报，与项目既定的取舍一致）。
    """
    name = str(entity or "").strip()
    if not name:
        return True          # 空名交由 UNKNOWN 分组处理，不在这里判
    for alias, _canonical in aliases_sorted:
        if alias and alias in name:
            return False
    return not any(w in name for w in _NON_ENTITY_WORDS)


_NON_ENTITY_WORDS = (
    "合计", "其中", "归属于", "减：", "加：", "期末", "期初",
    "年金额", "本期", "上期", "填列", "总计",
)
"""出现即说明"主体名"其实是报表行标签的词。

只放**报表结构用语**，不放行业词 —— 行业词（如「银行」「证券」）
可能真在公司名里，放进去会误伤真实主体。
"""


def _median_abs_yuan(facts: Sequence[Fact]) -> Decimal | None:
    """一组候选事实取值（绝对值，换算到元）的**中位数**。

    用途见 `check_units` 情形 2b：10 倍级的量级差太小，任何一条候选都可能
    凑巧对上；用"候选整体量级"做参照，才能区分"真的错一位"与"巧合"。
    取中位数而不是均值 —— 年报里同一指标常混有子公司/分部的小数值，
    均值会被拉偏，中位数代表"这个指标的典型量级"。
    """
    vals: list[Decimal] = []
    for f in facts:
        v = _fact_in_yuan(f)
        if v is not None and v != 0:
            vals.append(abs(v))
    if not vals:
        return None
    vals.sort()
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / Decimal("2")


def check_arithmetic(
    text: str,
    ledger: FactLedger,
    *,
    claims: Sequence[Claim] | None = None,
    tolerance: Decimal = Decimal("0.03"),
) -> CheckResult:
    """E5：算术与一致性核查。

    可自动验证的三类：
      * **合计校验**：文中同一指标出现「总计」与「分项」时，分项和须等于总计
      * **占比校验**：由事实库独立重算占比
      * **同比校验**：由事实库两期数据独立重算增速
    """
    import re

    res = CheckResult()
    seq = 0
    claims = claims if claims is not None else extract_claims(text)
    aliases_sorted = sorted(
        load_metric_aliases().items(), key=lambda kv: -len(kv[0])
    )

    # —— 同比/占比表述与事实库重算 ——
    for pattern, kind in _RATIO_PATTERNS:
        for m in re.finditer(pattern, text):
            snippet = text[max(0, m.start() - 60) : m.end() + 10]
            pct_raw = m.group("pct")
            if pct_raw is None:
                continue
            stated = parse_decimal(pct_raw)
            if stated is None:
                continue

            # 找到表述中提到的指标
            for alias, canonical in aliases_sorted:
                if alias not in snippet:
                    continue
                facts = [
                    f for f in ledger.by_metric(canonical) if not f.needs_review
                ]
                if len(facts) < 2:
                    break

                # 取两个**不同期间**的事实做同比。
                # ★ 必须**按主体分组**：一份材料里常常同时有母公司与几十家
                #   子公司（或「合计」行）的数据，跨主体比同比会得出荒谬结论，
                #   并制造误报 —— 而误报是核查系统最致命的失败模式。
                #   实测：表格抽取上线后同一指标跨主体可达 80+ 个取值，
                #   早期实现把不同主体的两期数据配成一对，算出假增速。
                by_entity: dict[str, dict[str, Fact]] = {}
                for f in facts:
                    if not f.period or f.period == "unknown":
                        continue
                    if not _entity_name_is_plausible(f.entity, aliases_sorted):
                        # 主体名其实是一条**报表行标签** —— 见函数说明
                        continue
                    by_entity.setdefault(f.entity or "UNKNOWN", {}).setdefault(f.period, f)

                ready = {e: bp for e, bp in by_entity.items() if len(bp) >= 2}
                if not ready:
                    break
                if len(ready) == 1:
                    entity, by_period = next(iter(ready.items()))
                else:
                    # 多个主体各自都有两期数据 —— 草稿里没点名就**不猜**。
                    # 猜错主体等于报一个不存在的错误，而误报比漏报更伤可信度。
                    named = [e for e in ready if e and e in snippet]
                    if len(named) != 1:
                        res.notes.append(
                            f"{canonical}：有 {len(ready)} 个主体各含两期数据，"
                            f"草稿未明确指向哪一个，跳过同比重算（宁缺勿错）"
                        )
                        break
                    entity, by_period = named[0], ready[named[0]]
                periods = sorted(by_period)
                prev, cur = by_period[periods[-2]], by_period[periods[-1]]
                if prev.value == 0:
                    break

                recomputed = (cur.value - prev.value) / abs(prev.value) * 100
                res.checked += 1
                if not approx_equal(
                    stated, q(recomputed), rel_tol=tolerance, abs_tol=Decimal("0.5")
                ):
                    if kind == "yoy" or any(k in snippet for k in ("同比", "较上年", "增长")):
                        seq += 1
                        idx = text.find(pct_raw, m.start())
                        claim = Claim(
                            text=pct_raw, value=stated, unit="%",
                            start=idx, end=idx + len(pct_raw),
                            section=_detect_section(text, idx), metric_hint=canonical,
                            context=snippet,
                        )
                        res.add(
                            _finding(
                                prefix="E5", seq=seq, error_type=ErrorType.E5_ARITHMETIC,
                                severity=Severity.CONFIRMED, claim=claim,
                                reason=(
                                    f"报告称增速 {stated}%，按原始材料重算："
                                    f"（{q(cur.value)} − {q(prev.value)}）÷ |{q(prev.value)}| "
                                    f"= {q(recomputed)}%（{prev.period} → {cur.period}，"
                                    f"主体：{entity}）"
                                ),
                                correct_value=f"{q(recomputed)}%",
                                suggestion=f"改为『{q(recomputed)}%』，或核对所引报告期",
                                confidence=0.88,
                                detector="rule.E5.yoy_recompute",
                                source_evidence=cur.evidence_span_ids + prev.evidence_span_ids,
                            )
                        )
                break

    # —— 合计校验：同一指标的多条事实 vs 文中「合计」表述 ——
    res.notes.append(
        "合计校验需表格结构信息；独立表结构由 pipelines/r5_audit 的表格核查阶段补充"
    )
    return res


# ==========================================================================
# 汇总执行
# ==========================================================================


_DRAFT_CODE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_DRAFT_COMPANY = re.compile(
    r"[\u4e00-\u9fa5]{2,12}?(?:股份有限公司|有限责任公司|集团有限公司|集团|银行|证券)"
)


def draft_entity_anchors(draft_text: str) -> set[str]:
    """从**草稿本身**抽取可信的主体标识：6 位股票代码 + 规范公司名。

    ⚠️ 只从草稿抽，**不去匹配事实库里的实体名**。这是踩过一次之后的结论：

    第一版拿事实库里的 ``entity`` 去草稿里找（含"取前 3–4 字前缀"的宽松匹配），
    而事实库的实体字段里混着**被切碎的行标签**（``1.归属于母公司股东的净利润``、
    ``2.少数股东损益``、``2023年金额``）—— 它们的前缀在财务草稿里几乎必然出现，
    于是"收窄作用域"变成了"每份文档都命中"，等于没收窄。

    现在反过来：草稿里出现规范公司名或 6 位代码，才认。宁可不收窄，不可收错 ——
    因为作用域判错会导致**漏报**，而漏报比误报更难被发现。
    """
    anchors: set[str] = set()
    for m in _DRAFT_CODE.finditer(draft_text or ""):
        code = m.group(1)
        # 年份、每股收益这类数字不是股票代码：A 股代码以 0/3/6/8 开头
        if code[0] in "0368":
            anchors.add(code)
    for m in _DRAFT_COMPANY.finditer(draft_text or ""):
        name = m.group(0)
        anchors.add(name)
        # 简称：去掉后缀后的主干（「湖南发展股份有限公司」→「湖南发展」）
        for suffix in ("股份有限公司", "有限责任公司", "集团有限公司", "集团"):
            if name.endswith(suffix) and len(name) > len(suffix) + 1:
                anchors.add(name[: -len(suffix)])
                break
    return anchors


def _fact_has_numeric_defect(fact: Fact) -> bool:
    """事实是否为**数值形态有缺陷**（片段、分组错、极小金额）。

    复用 R1 侧同一套判据（:func:`pipelines.r1_extract.table_facts.numeric_defects`），
    保证"入库守卫"与"核查入口守卫"不会各判一套。
    判据只依赖单条事实自身，不做跨行比较 —— 跨行比较在年报上不可靠
    （同一主体在不同表里的数值天然相差数量级）。
    """
    from pipelines.r1_extract.table_facts import numeric_defects

    return bool(numeric_defects(str(fact.raw_text or ""), metric=fact.metric, unit=fact.unit))


def scope_ledger_to_draft(
    draft_text: str, ledger: FactLedger, *, doc_names: dict[str, str] | None = None
) -> tuple[FactLedger, str]:
    """把事实库收窄到**草稿所谈论的那些公司**。返回 ``(作用域内账本, 说明)``。

    **为什么必需**（实测事故）：R5 的核查是"草稿里的数 ↔ 事实库里的数"比对，
    而事实库是**跨公司**的（一次 R1 运行可能包含多家公司的材料）。
    不限定作用域时，草稿里中国石油的「营业收入 28,644.69 亿元」会去跟
    湖南发展子公司表的 349,165,200 元比，得出"相差 10⁴ 量级，属单位错误"——
    一条**看起来头头是道、实际毫无意义**的确认级误报。实测这一条错误
    贡献了 6 条假阳性中的绝大多数。

    匹配方式：草稿里抽出的**代码或公司名**，出现在某条事实的 ``entity``、
    文档文件名或文档主体名里 → 该文档进入作用域。抽不到任何标识时**不收窄**。

    ⚠️ **必须同时匹配文件名**：实测三家公司的 ``entity`` 写法并不统一 ——
    中国石油的事实用股票代码（``601857``）作主体，另两家用公司全名，
    而代码只出现在**文件名**（``000722_湖南发展_2025年年度报告.pdf``）里。
    只匹配 entity 会让后两家永远命中不了，作用域静默失效。
    """
    all_facts = ledger.all()
    if not all_facts:
        return ledger, "事实库为空，未收窄作用域"

    # ★ 先剔除**数值形态有缺陷**的事实（如碎片段 `08.72` 被当成营业收入 8.72 元）。
    #
    #   为什么必须"不加载"而不是"标记后排除"：R5 的比对是"草稿值 ↔ 参考值"，
    #   标记后排除只会让该指标失去参考值，于是比对逻辑转而拿**别的数**去比，
    #   照样产出假阳性（实测：草稿里正常的「营业收入 3.49 亿元」被判成
    #   与 8.72 元相差 10⁸ 的量级错误）。垃圾值必须在入口处就不进来。
    clean: list[Fact] = []
    dropped = 0
    for f in all_facts:
        if _fact_has_numeric_defect(f):
            dropped += 1
            continue
        clean.append(f)
    if not clean:
        return ledger, "剔除形态缺陷事实后事实库为空，退回全量比对"
    all_facts = clean

    anchors = draft_entity_anchors(draft_text)
    if not anchors:
        return ledger, "草稿未出现股票代码或规范公司名，未收窄作用域（宁可漏报不可误报）"

    haystack: dict[str, set[str]] = {}
    for f in all_facts:
        h = haystack.setdefault(f.doc_id, set())
        h.add(str(f.entity or ""))
    # 文档名：股票代码往往只出现在文件名里（`000722_湖南发展_2025年年度报告.pdf`），
    # 而 entity 用的是公司全名。只匹配 entity 会让这类文档永远命中不了。
    for doc_id, name in (doc_names or {}).items():
        haystack.setdefault(doc_id, set()).add(str(name))

    hit_docs: set[str] = set()
    for doc_id, texts in haystack.items():
        blob = " ".join(texts)
        if any(a and a in blob for a in anchors):
            hit_docs.add(doc_id)

    if not hit_docs:
        return ledger, (
            f"草稿标识 {sorted(anchors)[:3]} 未命中任何文档，未收窄作用域"
        )

    scoped = FactLedger(ledger.store, run_id=ledger.run_id)
    kept = 0
    for f in all_facts:
        if f.doc_id in hit_docs:
            scoped.add(f)
            kept += 1
    if not kept:
        return ledger, "作用域内没有任何事实，退回全量比对"
    return scoped, (
        f"已按草稿主体 {sorted(anchors)[:3]} 收窄比对范围："
        f"{len(hit_docs)}/{len(haystack)} 份文档、{kept}/{len(all_facts)} 条事实"
    )


def run_all_checks(
    draft_text: str,
    ledger: FactLedger,
    *,
    multiple_ref: MultipleCheckInput | None = None,
    metric_configs: dict[str, Any] | None = None,
) -> CheckResult:
    """执行全部五类核查，返回合并结果。

    ``ledger`` 必须是**已经收窄到该草稿所谈主体**的账本 —— 见
    :func:`scope_ledger_to_draft`。跨公司的账本会让比对拿 A 公司的数
    去证明 B 公司的草稿有错，产生大量"合理但无意义"的误报。
    """
    alias_map = load_metric_aliases(metric_configs)
    claims = extract_claims(draft_text, alias_map=alias_map)

    merged = CheckResult()
    merged.notes.append(f"共抽取 {len(claims)} 条数值主张")

    for part in (
        check_units(claims, ledger),
        check_calibers(claims, ledger, text=draft_text),
        check_citations(claims, ledger),
        check_arithmetic(draft_text, ledger, claims=claims),
        # 比率口径：检验「净利率」这类比率是用归母数还是合计数算的
        check_ratio_calibers(draft_text, claims, ledger),
        # 表格列期核查：**必须单独跑**，因为它需要单元格级的行列结构，
        # 而 `claims` 是按文本流抽的、拿不到"这个数属于哪一列"。
        check_table_periods(draft_text, ledger),
    ):
        merged.findings.extend(part.findings)
        merged.checked += part.checked
        merged.notes.extend(part.notes)

    if multiple_ref is not None:
        part = check_multiples(draft_text, multiple_ref, claims=claims)
        merged.findings.extend(part.findings)
        merged.checked += part.checked
        merged.notes.extend(part.notes)

    # 去重：同一位置同一类型只保留置信度最高的一条
    merged.findings = _dedupe(merged.findings)
    # 抑制：同一位置若已有确定性数值错误（E1/E2/E5），
    # 更低优先级的「口径/来源」类结论不再重复报出
    merged.findings = _suppress_lower_priority(merged.findings)
    merged.findings.sort(key=lambda f: (-f.confidence, f.finding_id))
    return merged


# 错误类型的优先级：数字本身错了，比「口径/来源」问题更硬、更该先报。
# 同一处位置同时命中两类时，只保留优先级高的那条 —— 这是**误报控制**的关键一步。
_TYPE_PRIORITY: dict[ErrorType, int] = {
    ErrorType.E5_ARITHMETIC: 100,       # 算术错误：可独立重算，最硬
    ErrorType.E1_UNIT: 90,              # 单位错误：量级差可直接证明
    ErrorType.E2_MULTIPLE: 80,          # 倍数口径：可独立重算
    ErrorType.E3_CALIBER: 50,           # 口径：需要语义判断
    ErrorType.E4_CITATION: 40,          # 来源缺失：最软
    ErrorType.E6_QUALITATIVE: 30,
}

# 只在「高优先级类型能完整解释该处问题」时才抑制低优先级。
# E2（倍数）与 E4（来源）关注点不同，因此不参与抑制。
_SUPPRESSOR_TYPES = {ErrorType.E5_ARITHMETIC, ErrorType.E1_UNIT}
_SUPPRESSIBLE_TYPES = {ErrorType.E3_CALIBER, ErrorType.E4_CITATION, ErrorType.E6_QUALITATIVE}


def _ranges_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """两个字符区间是否重叠。"""
    return not (a[1] <= b[0] or b[1] <= a[0])


def _suppress_lower_priority(findings: list[Finding]) -> list[Finding]:
    """抑制与确定性数值错误重叠的低优先级结论。

    场景：草稿把「净利润 2,456,000,000.00 元」误写为「…万元」。
    E1 会报「单位错误」，E4 也会报「无来源」—— 后者是前者的**副作用**，
    重复报出会虚增问题条数并拉低精确率。

    抑制规则：若某条 E3/E4/E6 的字符区间与某条 E1/E5 的区间重叠，
    则丢弃前者。
    """
    suppressors = [
        f for f in findings
        if f.error_type in _SUPPRESSOR_TYPES and f.location.char_range
    ]
    if not suppressors:
        return findings

    kept: list[Finding] = []
    for f in findings:
        if f.error_type in _SUPPRESSIBLE_TYPES and f.location.char_range:
            cr = f.location.char_range
            if any(
                _ranges_overlap(cr, s.location.char_range)  # type: ignore[arg-type]
                for s in suppressors
            ):
                continue
        kept.append(f)
    return kept


def _dedupe(findings: Iterable[Finding]) -> list[Finding]:
    best: dict[tuple[str, int, int], Finding] = {}
    out: list[Finding] = []
    for f in findings:
        cr = f.location.char_range
        key = (f.error_type.value, cr[0] if cr else -1, cr[1] if cr else -1)
        prev = best.get(key)
        if prev is None or f.confidence > prev.confidence:
            best[key] = f
    for f in best.values():
        out.append(f)
    # 重新编号，保证 finding_id 稳定可读
    counters: dict[str, int] = {}
    for f in sorted(out, key=lambda x: (x.error_type.value, x.location.char_range or (0, 0))):
        prefix = f.error_type.value.split("_")[0]
        counters[prefix] = counters.get(prefix, 0) + 1
        f.finding_id = f"{prefix}-{counters[prefix]:03d}"
    return out


# ==========================================================================
# 工具封装
# ==========================================================================


class CheckUnitIn(BaseModel):
    draft_text: str
    facts_json: str = Field(default="", description="事实库 JSON 内容或路径")


class RunAllChecksIn(BaseModel):
    draft_text: str
    facts_json: str


@tool(
    name="rule.check_all",
    description="对研究报告草稿执行 E1-E5 五类核查（单位/倍数/口径/引用/算术），返回分级发现清单。",
    params=RunAllChecksIn,
    side_effect="none",
    cost="free",
    tags=["audit", "deterministic", "r5"],
)
def check_all_tool(draft_text: str, facts_json: str) -> dict[str, Any]:
    import json
    from pathlib import Path

    from agent.evidence.models import FactSet

    raw = facts_json
    p = Path(facts_json)
    if p.exists():
        raw = p.read_text(encoding="utf-8")
    fs = FactSet.model_validate(json.loads(raw))

    from agent.evidence.store import EvidenceStore

    store = EvidenceStore()
    for span in fs.evidence:
        store.add_span(span)
    ledger = FactLedger(store, run_id=fs.run_id)
    for f in fs.facts:
        ledger.add(f)

    res = run_all_checks(draft_text, ledger)
    return res.to_dict()


__all__ = [
    "Claim",
    "CheckResult",
    "MultipleCheckInput",
    "load_metric_aliases",
    "extract_claims",
    "check_units",
    "check_multiples",
    "check_calibers",
    "check_citations",
    "check_arithmetic",
    "run_all_checks",
    "check_all_tool",
]
