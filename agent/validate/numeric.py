"""单位归一化与数值解析 —— **LLM 永不产数字，只产原文串**。

本模块是整个项目数值准确性的地基。模型只能输出 ``raw_text``（页面上
原样出现的字符串），由本模块负责：
    1. 解析成 ``Decimal``（不用 float —— 金融数据不接受二进制浮点误差）
    2. 归一化单位（万元/亿元/元/千元 → 统一到元）
    3. 保留原始单位与原始串，供溯源展示

所有换算用 ``Decimal`` 精确运算，最后一步才按规则四舍五入。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from agent.runtime.errors import ContractViolation

# ==========================================================================
# 单位表：中文财务文档里实际会出现的写法
# ==========================================================================

# 金额单位 -> 相对「元」的倍数
MONEY_UNITS: dict[str, Decimal] = {
    "元": Decimal("1"),
    "人民币元": Decimal("1"),
    "千元": Decimal("1000"),
    "万元": Decimal("10000"),
    "人民币万元": Decimal("10000"),
    "十万元": Decimal("100000"),
    "百万元": Decimal("1000000"),
    "亿元": Decimal("100000000"),
    "人民币亿元": Decimal("100000000"),
    "万": Decimal("10000"),      # 财报表头简写
    "亿": Decimal("100000000"),
}

# 数量单位 -> 相对「股」的倍数
SHARE_UNITS: dict[str, Decimal] = {
    "股": Decimal("1"),
    "千股": Decimal("1000"),
    "万股": Decimal("10000"),
    "亿股": Decimal("100000000"),
}

# 比率单位（不需换算，仅归一化写法）
RATIO_UNITS: set[str] = {"%", "‰", "倍", "元每股", "元/股", "个百分点"}

ALL_UNITS: set[str] = set(MONEY_UNITS) | set(SHARE_UNITS) | RATIO_UNITS

# 单位规范化别名
_UNIT_ALIASES: dict[str, str] = {
    "人民币元": "元",
    "人民币万元": "万元",
    "人民币亿元": "亿元",
    "元/股": "元每股",
    "％": "%",
    "百分号": "%",
    "个百分点": "个百分点",
}

# 括号负数（中文财报标准写法）：（1,234.56）表示 -1234.56
_PAREN_NEG = re.compile(r"^[（(]\s*(.*?)\s*[)）]$")

# 数字串：允许千分位、正负号、小数
_NUM_BODY = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?$|^[+-]?\d+(?:\.\d+)?$")

# 带单位的完整数字串，如 "12,004.50万元"
_NUM_WITH_UNIT = re.compile(
    r"^\s*[（(]?\s*([+-]?\d[\d,]*(?:\.\d+)?)\s*[)）]?\s*"
    r"(亿元|人民币亿元|百万元|十万元|万元|人民币万元|千元|人民币元|元|亿股|万股|千股|股|%|‰|％|倍|元每股|元/股|个百分点|万|亿)?\s*$"
)

# 中文大写数字（用于金额大写交叉校验）
_CN_DIGITS = {
    "零": 0, "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5, "陆": 6, "柒": 7, "捌": 8, "玖": 9,
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "两": 2,
}
_CN_UNITS = {"分": Decimal("0.01"), "角": Decimal("0.1"), "拾": Decimal("10"), "十": Decimal("10"),
             "佰": Decimal("100"), "百": Decimal("100"), "仟": Decimal("1000"), "千": Decimal("1000"),
             "万": Decimal("10000"), "亿": Decimal("100000000")}


# ==========================================================================
# 解析
# ==========================================================================


def normalize_unit(unit: str | None) -> str:
    """单位写法归一化。"""
    if not unit:
        return ""
    u = unit.strip()
    return _UNIT_ALIASES.get(u, u)


def parse_decimal(raw_text: str) -> Decimal | None:
    """把页面上的原始串解析成 Decimal。无法解析返回 None（不是抛异常）。

    支持：千分位、括号负数、正负号、全角符号、中文大写金额。
    """
    if raw_text is None:
        return None
    s = raw_text.strip()
    if not s:
        return None

    # 全角数字/符号转半角
    s = _to_halfwidth(s)

    # 占位符：财报里的「-」「—」「不适用」表示无数据
    if s in {"-", "—", "－", "不适用", "无", "N/A", "n/a", "/"}:
        return Decimal("0")

    negative = False
    m = _PAREN_NEG.match(s)
    if m:
        negative = True
        s = m.group(1).strip()

    # 去掉货币符号与空格
    s = re.sub(r"^[¥￥$]\s*", "", s)
    s = s.replace(" ", "").replace(",", "").replace("，", "")

    # 去掉尾部单位后再试
    s_num = _NUM_WITH_UNIT.match(s)
    if s_num:
        s = s_num.group(1).replace(",", "")

    if not _NUM_BODY.match(s):
        # 尝试中文大写
        cn = parse_chinese_amount(raw_text)
        return cn

    try:
        val = Decimal(s)
    except InvalidOperation:
        return None
    return -val if negative else val


def parse_chinese_amount(text: str) -> Decimal | None:
    """解析中文大写金额，如「壹亿贰仟零肆万伍仟元」。

    用途：中文公告的「金额 + 大写」是**两个独立通道**，
    互为校验 —— 这是金融文档独有的免费校验器。
    """
    if not text:
        return None
    s = _to_halfwidth(text.strip())
    s = re.sub(r"^(人民币|RMB|￥|¥)", "", s)
    s = re.sub(r"(元整|元|整)$", "", s)
    if not s or not any(ch in _CN_DIGITS for ch in s):
        return None

    total = Decimal("0")
    section = Decimal("0")   # 当前「万/亿」小节
    number = Decimal("0")

    for ch in s:
        if ch in _CN_DIGITS:
            number = Decimal(_CN_DIGITS[ch])
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit >= Decimal("10000"):
                section = (section + number) * unit
                total += section
                section = Decimal("0")
                number = Decimal("0")
            else:
                if number == 0:
                    number = Decimal("1")  # 「拾」单独出现表示 10
                section += number * unit
                number = Decimal("0")
        elif ch in {"零", "整"}:
            continue
        else:
            return None
    return total + section + number


def parse_amount(raw_text: str) -> tuple[Decimal, str] | None:
    """解析「数值 + 单位」，返回 ``(归一化到元的 Decimal, 原始单位)``。

    例：``"12,004.50万元"`` -> ``(Decimal('120045000.00'), '万元')``
    """
    if not raw_text:
        return None
    s = _to_halfwidth(raw_text.strip())
    negative = False
    m = _PAREN_NEG.match(s)
    if m:
        negative = True
        s = m.group(1).strip()

    mm = _NUM_WITH_UNIT.match(s)
    if not mm:
        # 没写单位：返回裸数值 + 空单位（调用方须按文档级默认单位补齐）
        val = parse_decimal(raw_text)
        return (val, "") if val is not None else None

    digits, unit = mm.group(1), normalize_unit(mm.group(2) or "")
    try:
        val = Decimal(digits.replace(",", ""))
    except InvalidOperation:
        return None
    if negative:
        val = -val
    return val, unit


def to_yuan(value: Decimal, unit: str) -> Decimal:
    """按金额单位换算到「元」。未知单位抛异常（不允许猜测）。"""
    u = normalize_unit(unit)
    if u == "":
        raise ContractViolation("金额单位缺失，拒绝换算（不允许猜测单位）", unit=unit)
    if u in MONEY_UNITS:
        return value * MONEY_UNITS[u]
    if u in RATIO_UNITS:
        return value  # 比率不做换算
    if u in SHARE_UNITS:
        raise ContractViolation(f"单位 {u} 是股数而非金额，调用方用错了函数", unit=unit)
    raise ContractViolation(f"未知金额单位 {u!r}", unit=unit)


def from_yuan(value_yuan: Decimal, unit: str) -> Decimal:
    """从「元」换算回指定单位。"""
    u = normalize_unit(unit)
    if u not in MONEY_UNITS:
        raise ContractViolation(f"未知金额单位 {u!r}", unit=unit)
    return value_yuan / MONEY_UNITS[u]


def shares_to_shares(value: Decimal, unit: str) -> Decimal:
    """按股数单位换算到「股」。"""
    u = normalize_unit(unit)
    if u in SHARE_UNITS:
        return value * SHARE_UNITS[u]
    raise ContractViolation(f"未知股数单位 {u!r}", unit=unit)


# ==========================================================================
# 比较
# ==========================================================================


def approx_equal(
    a: Decimal,
    b: Decimal,
    *,
    rel_tol: Decimal = Decimal("0.005"),
    abs_tol: Decimal = Decimal("0"),
) -> bool:
    """相对/绝对容差比较。默认 0.5% 相对容差。

    用途：R5 比对报告值与事实库值。报告常做四舍五入，
    因此必须有容差，但容差不能大到掩盖真实错误。
    """
    diff = abs(a - b)
    if diff <= abs_tol:
        return True
    denom = max(abs(a), abs(b))
    if denom == 0:
        return diff <= abs_tol
    return (diff / denom) <= rel_tol


def rel_diff(a: Decimal, b: Decimal) -> Decimal:
    """相对差异，用于生成人类可读的错误说明。"""
    denom = max(abs(a), abs(b))
    if denom == 0:
        return Decimal("0")
    return abs(a - b) / denom


def magnitude_gap(a: Decimal, b: Decimal) -> int | None:
    """量级差（以 10 为底）。用于 E1 单位错误判定。

    例：120045000（1.2亿）与 1.2 的量级差约为 8，
    可判定为「亿元/元」单位混用。
    """
    if a == 0 or b == 0:
        return None
    try:
        return int((abs(a) / abs(b)).log10().to_integral_value(rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return None


# ==========================================================================
# 取整与格式化
# ==========================================================================


def q(value: Decimal, places: int = 2) -> Decimal:
    """四舍五入到指定小数位（金融惯例 ROUND_HALF_UP，非银行家舍入）。"""
    exp = Decimal(1).scaleb(-places)
    return value.quantize(exp, rounding=ROUND_HALF_UP)


def fmt_amount(value_yuan: Decimal, unit: str = "万元", places: int = 2) -> str:
    """格式化金额用于报告展示。"""
    v = from_yuan(value_yuan, unit)
    return f"{q(v, places):,} {unit}"


def pct(value: Decimal, places: int = 2) -> str:
    """0.1234 -> '12.34%'"""
    return f"{q(value * 100, places)}%"


# ==========================================================================
# 内部
# ==========================================================================


def repair_line_broken_numbers(text: str) -> tuple[str, list["NumberRepair"]]:
    """把被**排版折行**切断的数字或单位接回去。返回 ``(修好的文本, 改动清单)``。

    为什么需要（实测数据）：

    金融 PDF 的表格列很窄，长数字与单位会被折行切开，文本层里就是断的::

        15,234,63          营业收入：717,247.24 万
        8.42               元；净利润：38,814.56 万元。

    左图是**数字被切断**，右图是**单位被切断**（万 / 元分处两行）。

    这带来两个后果，而且都不报错：

    1. **抽取漏掉**：事实抽取的正则是 ``\\d[\\d,]*``，跨不过换行；
       单位跟在断口之后，于是「指标名 + 数值 + 单位」这个模式匹配不上。
       实测：一份材料里 28 页的财务数据页**产出 0 条事实**，
       最后只能靠 VLM 逐页读图（成本高、置信度还低）。
    2. **表格碎格**：单元格文本同样带换行 —— 55 张真实表格共 422 个碎格，
       其中 44% 是数字或单位被切断。

    修法本可以用 VLM 逐格读图，但那是**拿钱买一个纯确定性的判断**，
    而且预算有限、覆盖率上不去。折行的接法本身没有歧义，用规则做即可。

    **保守规则**（宁可漏接，不可接错）：

    * **数字接续**：拼接后必须是合法数字（只含数字/千分位逗号/至多一个小数点），
      且**至少有一个片段本身不是规范数字** —— 否则视为"两行各是一个完整数字"
      （如 ``1,000`` / ``2,000``），不拼。
    * **单位接续**：仅当行尾是「数字 + 万/亿/千/百」而下一行以「元」开头时拼接。
      这一条极其克制 —— 只认这四种残缺单位，
      因为「万元」被拆成「万 / 元」是窄列表格里最常见的折行形态，
      而其他组合（如把下一行的"元"接到本行金额上）可能张冠李戴。
    """
    text, n_changes = _join_split_numbers(text)
    changes: list[NumberRepair] = n_changes
    text, unit_changes = _join_split_units(text)
    changes.extend(unit_changes)
    return text, changes


# 行尾：数字 + 半个单位（万/亿/千/百）；下一行以「元」开头
_TAIL_HALF_UNIT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(万|亿|千|百)\s*$")
_HEAD_YUAN = re.compile(r"^\s*元")


def _join_split_units(text: str) -> tuple[str, list["NumberRepair"]]:
    lines = text.split("\n")
    changes: list[NumberRepair] = []
    i = 0
    while i < len(lines) - 1:
        cur, nxt = lines[i], lines[i + 1]
        m = _TAIL_HALF_UNIT.search(cur)
        if m and _HEAD_YUAN.match(nxt):
            idx = nxt.index("元")
            lines[i] = cur + "元" + nxt[idx + 1:]
            changes.append(NumberRepair(f"{m.group(0)}\n元", f"{m.group(2)}元"))
            del lines[i + 1]
            continue
        i += 1
    return "\n".join(lines), changes


def _join_split_numbers(text: str) -> tuple[str, list["NumberRepair"]]:
    lines = text.split("\n")
    changes: list[NumberRepair] = []
    i = 0
    while i < len(lines) - 1:
        cur, nxt = lines[i], lines[i + 1]
        m_tail = _TRAILING_NUMBER.search(cur)
        stripped = nxt.lstrip()
        m_head = _LEADING_NUMBER.match(stripped)
        m_dec = None if m_head else _LEADING_DECIMAL.match(stripped)
        if m_tail and (m_head or m_dec):
            matched = m_head or m_dec
            assert matched is not None
            head, tail = m_tail.group(1), matched.group(1)
            joined = head + tail
            if _is_plausible_join(head, tail, joined):
                lines[i] = cur[: m_tail.start()] + joined + stripped[matched.end():]
                changes.append(NumberRepair(f"{head}\n{tail}", joined))
                del lines[i + 1]
                # 一个数字可能被切成三段以上，继续在同一行上试
                continue
        i += 1
    return "\n".join(lines), changes


@dataclass(frozen=True)
class NumberRepair:
    """一次数字拼接的前后对照 —— 数据被改过，就必须留痕。"""

    before: str
    after: str

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"{self.before!r} → {self.after!r}"


# 尾部数字（可能带千分位与小数点）。允许**以小数点结尾** ——
# 折行正好落在小数点之后是常见形态（``5,787,118.`` + ``61``）。
# 早期不允许，于是这类格子接不上，只能靠"把换行整个删掉"来兜底，
# 而那种兜底在保留拉丁空格之后立刻失效（实测 389 → 251 条）。
_TRAILING_NUMBER = re.compile(r"(-?\d[\d,]*(?:\.\d*)?)\s*$")
_LEADING_NUMBER = re.compile(r"^(-?\d[\d,]*(?:\.\d+)?)")
# 下一行**以小数点开头**：折行正好落在小数点之前（``42,399,138`` + ``.44``）
_LEADING_DECIMAL = re.compile(r"^(\.\d{1,6})")
# 「规范数字」：千分位分组正确，或干脆没有分组
_CLEAN_NUMBER = re.compile(r"^-?\d{1,3}(?:,\d{3})*(?:\.\d+)?$|^-?\d+(?:\.\d+)?$")
_JOINABLE = re.compile(r"^-?[\d,]+(?:\.\d+)?$")
_DIGITS_ONLY = re.compile(r"^\d+$")


def _is_plausible_join(head: str, tail: str, joined: str) -> bool:
    if not _JOINABLE.match(joined):
        return False
    if joined.count(".") > 1 or joined.endswith((",", ".")) or ",," in joined:
        return False
    if joined.lstrip("-").startswith(","):
        return False
    if head.endswith("."):
        # 行尾是小数点时**不能无条件接**：「Note 5.」+「2024」会变成「5.2024」。
        # 只在小数点前确实是个**格式化数字**时才接：带千分位，或位数够多。
        if "," not in head and len(re.sub(r"\D", "", head)) < 4:
            return False
        if len(tail.split(".")[0]) > 6:      # 小数位通常 1–2 位
            return False
    if "." in head and _DIGITS_ONLY.match(tail):
        # 小数部分被折行截断：``-860,467.9`` + ``8`` → ``-860,467.98``
        # 这时两个片段**各自都是规范数字**，但那不代表它们是两个值 ——
        # 一个带小数的数后面跟一行纯数字，是"小数位没写完"的典型形态。
        return True
    # 两个片段各自都是规范数字 → 这是两行两个值，不能拼
    return not (_CLEAN_NUMBER.match(head) and _CLEAN_NUMBER.match(tail))


def _to_halfwidth(s: str) -> str:
    """全角转半角（数字、字母、常见符号）。"""
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


__all__ = [
    "MONEY_UNITS",
    "SHARE_UNITS",
    "RATIO_UNITS",
    "ALL_UNITS",
    "normalize_unit",
    "parse_decimal",
    "parse_chinese_amount",
    "parse_amount",
    "to_yuan",
    "from_yuan",
    "shares_to_shares",
    "approx_equal",
    "rel_diff",
    "magnitude_gap",
    "q",
    "fmt_amount",
    "pct",
    "repair_line_broken_numbers",
    "NumberRepair",
]
