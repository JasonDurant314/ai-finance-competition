"""VLM 裁判 —— 三层解析引擎的第 3 层（多模态大模型读图与裁决）。

模型：DeepSeek-V4.1-Flash（``deepseek-flash``），原生多模态视觉理解。
官方视觉能力基准中 Chartography 与表格/图表理解是强项，适合做表格裁决。

**四条防幻觉铁律**（提示词里写，代码里用闸门兜住）：
    1. 只允许输出页面上**看得见的字符串**，禁止改写/换算/四舍五入
    2. 强制 ``quote`` 回读校验 —— 对不上直接判 cannot_determine
    3. **禁止 VLM 做算术**：它说 "12,004.50"，换算成"1.20亿元"是代码的事
    4. ``cannot_determine`` 是合法且被鼓励的输出 —— 宁缺勿错

本模块产出的任何数值都必须经过 :func:`agent.evidence.store.EvidenceStore.verify_and_anchor`
才能进入事实库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agent.evidence.models import Verdict
from agent.model.gateway import ModelGateway, image_message, text_message
from agent.runtime.errors import HarnessError
from agent.tools.registry import tool
from agent.validate.quotecheck import QuoteMatch, check_quote
# 注意：``tables`` 里不含对 ``vlm`` 的导入，因此这里不是循环依赖。
# 早先写成函数内延迟导入，结果 ``decide_cell_verdict`` 看不见 ``cell_number``，
# 直接抛 NameError —— 被 tests/test_static_health.py 的静态体检验了出来。
from domain.tools.tables import cell_number, corroborate_by_geometry

# ==========================================================================
# 输出契约（模型必须严格按此结构返回）
# ==========================================================================

# 单条视觉读数的结构 —— 会被复用于「整页多字段」与「单字段裁决」两种任务
_FIELD_ESTIMATE_PROPS: dict[str, Any] = {
    "metric": {
        "type": "string",
        "description": (
            "该数值对应的**规范指标名**，必须从任务中给出的指标清单里选择。"
            "清单中没有合适的就填 \"unknown\" —— **不要自创名字**。"
        ),
    },
    "value_raw": {
        "type": "string",
        "description": "页面上原样出现的数值字符串，禁止换算与四舍五入",
    },
    "unit_as_written": {
        "type": "string",
        "description": "页面上原样出现的单位，如 万元 / % / 万股",
    },
    "quote": {
        "type": "string",
        "description": (
            "页面上**连续**的原文片段（含该数值及其上下文），必须逐字照抄。"
            "这是回读校验的依据，编造会导致整条结果被丢弃。"
        ),
    },
    "entity_hint": {
        "type": ["string", "null"],
        "description": "该数值对应的主体（公司名或股票代码），无法判断时为 null",
    },
}

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "field_id": {"type": "string"},
        "verdict": {
            "type": "string",
            "enum": ["A", "B", "both_agree", "both_wrong", "cannot_determine"],
            "description": (
                "A=采信通道A(文本层) B=采信通道B(OCR) both_agree=两路一致 "
                "both_wrong=两路皆错(以你读到的为准) cannot_determine=无法确定(鼓励使用)"
            ),
        },
        "value_raw": {
            "type": ["string", "null"],
            "description": "页面上原样出现的数值字符串，禁止换算与四舍五入；无法确定时为 null",
        },
        "unit_as_written": {
            "type": ["string", "null"],
            "description": "页面上原样出现的单位，如 万元 / % / 万股",
        },
        "quote": {
            "type": ["string", "null"],
            "description": (
                "页面上**连续**的原文片段（含该数值及其上下文），"
                "必须逐字照抄。这是回读校验的依据，编造会导致整条结果被丢弃。"
            ),
        },
        "page": {"type": ["integer", "null"]},
        "disagreement_reason": {
            "type": ["string", "null"],
            "description": "当两路不一致时，说明你的判断依据（版面位置、字形等）",
        },
        "self_confidence": {
            "type": "number",
            "description": "你对本次判断的置信度 0.0-1.0。不确定请给低分，不要虚高。",
        },
    },
    "required": ["field_id", "verdict", "self_confidence"],
    "additionalProperties": False,
}

# ★ 整页扫描的结构：一次调用读出页面上**多个**带指标归属的字段
#
# 为什么要这样：早期实现用笼统的 `page_key_fields` 作为 field_id，
# 结果同一页读出的三个不同股东的持股数被当成「同一指标的多个值」，
# 触发了 3 条**假冲突**告警。指标归属必须由模型给出，而不是由程序猜。
PAGE_SCAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "page": {"type": ["integer", "null"]},
        "fields": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "properties": _FIELD_ESTIMATE_PROPS,
                "required": ["metric", "value_raw", "unit_as_written", "quote"],
                "additionalProperties": False,
            },
            "description": (
                "本页上你能**确信**读出的字段，最多 12 条。"
                "读不准的宁可不写 —— 编造会被回读校验丢弃并记录在案。"
            ),
        },
        "page_type": {
            "type": "string",
            "enum": ["announcement", "financial_statement", "table", "other"],
        },
        "self_confidence": {"type": "number"},
    },
    "required": ["fields", "self_confidence"],
    "additionalProperties": False,
}

TABLE_CELL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": (
                "单元格内的文字，逐字照抄，不做任何加工。空单元格返回空字符串。"
                "**数字保留页面上原样的千分位与小数位。**"
            ),
        },
        "text_normalized": {
            "type": ["string", "null"],
            "description": (
                "若单元格内的数字被**换行切断**，在此给出合并后的完整数字"
                "（去掉换行与多余空格）；未切断时填 null。"
                '例：页面上是「583,96」换行「5.66」→ 这里填 "583,965.66"'
            ),
        },
        "quote": {
            "type": ["string", "null"],
            "description": "该单元格在**整页**上的连续原文，用于回读校验。",
        },
        "position_confirmed": {
            "type": "boolean",
            "description": (
                "红框标出的位置与你所读的单元格是否**同一格**？"
                "若你读到的内容明显不在红框内，填 false。"
            ),
        },
        "is_numeric": {"type": "boolean"},
        "self_confidence": {"type": "number"},
        "note": {
            "type": ["string", "null"],
            "description": "模糊/被遮挡/跨格/被切断时说明情况",
        },
    },
    "required": ["text", "self_confidence"],
    "additionalProperties": False,
}


# ==========================================================================
# 输出模型
# ==========================================================================


class JudgeOutput(BaseModel):
    """VLM 裁判的结构化输出。"""

    field_id: str
    verdict: Verdict
    value_raw: str | None = None
    unit_as_written: str | None = None
    quote: str | None = None
    page: int | None = None
    disagreement_reason: str | None = None
    self_confidence: float = Field(ge=0.0, le=1.0)

    # 代码侧补写（非模型输出）
    quote_status: str = "unchecked"
    quote_similarity: float = 0.0
    rejected: bool = False
    rejection_reason: str | None = None

    @property
    def usable(self) -> bool:
        """是否可用于入库。三个条件缺一不可。"""
        return (
            not self.rejected
            and self.verdict is not Verdict.CANNOT_DETERMINE
            and self.value_raw is not None
            and self.quote_status in (QuoteMatch.EXACT.value, QuoteMatch.FUZZY.value)
        )


class TableCellReading(BaseModel):
    """VLM 读单个单元格的结果。"""

    text: str
    text_normalized: str | None = None
    """数字被换行切断时，模型给出的合并结果。"""
    quote: str | None = None
    position_confirmed: bool | None = None
    """模型是否确认红框位置与自己读的是同一格。"""
    is_numeric: bool = False
    self_confidence: float = Field(ge=0.0, le=1.0)
    note: str | None = None

    # 代码侧补写
    quote_status: str = "unchecked"
    quote_similarity: float = 0.0
    rejected: bool = False
    rejection_reason: str | None = None

    @property
    def best_text(self) -> str:
        """优先采用合并后的数字（若有），否则用原样文本。"""
        if self.text_normalized and self.text_normalized.strip():
            return self.text_normalized.strip()
        return self.text.strip()


class FieldEstimate(BaseModel):
    """整页扫描中的**一条**视觉读数（含指标归属）。"""

    metric: str = Field(description="规范指标名，取自任务给出的清单；无法归属时为 unknown")
    value_raw: str
    unit_as_written: str
    quote: str
    entity_hint: str | None = None

    # 代码侧补写（非模型输出）
    quote_status: str = "unchecked"
    quote_similarity: float = 0.0
    rejected: bool = False
    rejection_reason: str | None = None

    @property
    def usable(self) -> bool:
        return (
            not self.rejected
            and self.metric != "unknown"
            and self.quote_status in (QuoteMatch.EXACT.value, QuoteMatch.FUZZY.value)
        )


class PageScanResult(BaseModel):
    """整页扫描结果 —— 一次调用读出多个带指标归属的字段。

    与 :class:`JudgeOutput` 的区别：后者是「对某个已知字段做裁决」，
    本结构是「不知道页面上有什么，请把能读的都读出来并归到指标上」。

    实测动机：早期用笼统的 ``page_key_fields`` 做 field_id，
    同一页三个不同股东的持股数被当成同一指标的多个值，
    触发 3 条假冲突。指标归属必须由模型给出。
    """

    page: int | None = None
    fields: list[FieldEstimate] = Field(default_factory=list)
    page_type: str = "other"
    self_confidence: float = Field(ge=0.0, le=1.0)

    # 代码侧统计
    rejected_count: int = 0
    unknown_metric_count: int = 0

    @property
    def usable_fields(self) -> list[FieldEstimate]:
        return [f for f in self.fields if f.usable]


# ==========================================================================
# 提示词（内联，便于单测；正式流程应从 domain/prompts/ 加载带版本的版本）
# ==========================================================================

_JUDGE_SYSTEM = """你是金融文档的**版面读数员**，不是分析师。你的唯一职责是把页面上印着的字符准确读出来，并判断它属于哪个财务指标。

铁律（违反任何一条，你的输出会被程序直接丢弃）：
1. 只允许输出页面上**真实印着**的字符串。禁止改写、禁止换算单位、禁止四舍五入、禁止补全缺失位数。
2. `quote` 必须是页面上**连续出现**的原文片段（包含目标数值及其上下文），逐字照抄。
   程序会把这段文字回到原页面文本中查找；找不到就判定你编造，整条结果作废。
3. **禁止做任何算术**。看到 "1,234.56 万元" 就原样输出 "1,234.56" 与 "万元"，
   不要换算成 "1234.56万" 或 "0.12亿"。换算由程序负责。
4. 看不清、被印章遮挡、跨页断裂、无从判断时，**必须**输出 `cannot_determine`
   （或对整页扫描中的该条字段干脆不输出）。
   说"不确定"不会扣分；编造一个看起来合理的数字会被直接丢弃并记录在案。
5. 不要输出任何解释性文字、不要输出 markdown 代码围栏，只输出一个 JSON 对象。

## 指标归属规则（最重要的一条）

每条读数都必须给出 `metric` —— 该数值对应的**规范指标名**。

* **只能从任务给出的指标清单里选**，不得自创名字、不得使用中文名。
* 清单里确实没有合适的，填 `"unknown"`。
  填 `"unknown"` 不会被扣分，但**自创名字会让这条读数无法进入事实库**。
* 判断依据是数值**紧邻左侧的标签文字**，而不是整段的主题。
  例：「质押股份数量：12,004.50 万股」→ metric 是 `pledge_shares`；
      同一段里出现的「占公司总股本比例：12.40%」→ metric 是 `pledge_ratio_of_total`。
* **同一页上多个不同主体的同一指标要分别输出**，并各自给出 `entity_hint`。
  例：三个股东的持股数都是 `shareholding_after`，但 `entity_hint` 不同 ——
  它们是**三条独立读数**，不是同一指标的三个候选值。

你的输出结构：
{"field_id": "...", "verdict": "A|B|both_agree|both_wrong|cannot_determine",
 "value_raw": "页面原样的数值串或 null", "unit_as_written": "页面原样的单位或 null",
 "quote": "连续原文片段或 null", "page": 页码, "disagreement_reason": "或 null",
 "self_confidence": 0.0-1.0}"""


# 整页扫描专用的系统提示（强调指标归属与一页多值）
_SCAN_SYSTEM = """你是金融文档的**版面读数员**，不是分析师。任务：把这一页上你能确信读出的数值**逐条**读出来，并判断每条属于哪个财务指标。

铁律（违反任何一条，你的输出会被程序直接丢弃）：
1. 只输出页面上**真实印着**的字符串。禁止改写、禁止换算单位、禁止四舍五入。
2. 每条读数的 `quote` 必须是页面上**连续出现**的原文片段，逐字照抄。
   程序会回到原页面文本中查找；找不到就判定你编造，**该条作废**。
3. **禁止做任何算术**。不要换算量纲、不要算占比、不要汇总。
4. **读不准的宁可不写**。少写几条不会被扣分；编造会被丢弃并记录在案。
   一页最多输出 12 条 —— 优先输出金额、比例、股数、日期这类关键字段。
5. 只输出一个 JSON 对象，不要解释文字、不要 markdown 代码围栏。

## 指标归属（本任务的核心）

每条读数都必须给出 `metric` —— 该数值对应的**规范指标名**。

* **只能从下面给出的指标清单里选**，不得自创名字、不得使用中文名。
* 清单里没有合适的，填 `"unknown"`。填 unknown 不扣分，自创名字会让该条无法入库。
* 判断依据是数值**紧邻左侧的标签文字**。例：
  「质押股份数量：12,004.50 万股」→ `pledge_shares`
  「占公司总股本比例：12.40%」    → `pledge_ratio_of_total`
  注意「占公司总股本比例」不是「总股本」—— 别被包含关系骗了。
* **同一页上多个不同主体的同一指标要分别输出**，各带 `entity_hint`。
  例：三个股东的持股数都归 `shareholding_after`，但 entity_hint 不同 ——
  这是**三条独立读数**，不是同一指标的三个候选值。

## 输出结构

{"page": 页码, "page_type": "announcement|financial_statement|table|other",
 "fields": [{"metric": "规范指标名", "value_raw": "页面原样数值串",
             "unit_as_written": "页面原样单位", "quote": "连续原文片段",
             "entity_hint": "主体名或 null"}],
 "self_confidence": 0.0-1.0}"""


def _build_judge_prompt(
    *,
    field_id: str,
    field_label: str,
    page_no: int,
    channel_a: str | None,
    channel_b: str | None,
    extra_instruction: str = "",
) -> str:
    parts = [
        f"目标字段：`{field_id}`（{field_label}）",
        f"页码：第 {page_no} 页（图像即此页）",
        "",
    ]
    if channel_a is not None:
        parts.append(f"通道 A（PDF 文本层）读到：{channel_a!r}")
    else:
        parts.append("通道 A（PDF 文本层）：不可用（该页无文本层或该字段未提取到）")
    if channel_b is not None:
        parts.append(f"通道 B（OCR）读到：{channel_b!r}")
    else:
        parts.append("通道 B（OCR）：不可用或未识别到该字段")
    parts += [
        "",
        "请对照图像判断该字段的真实取值，按约定 JSON 输出。",
    ]
    if extra_instruction:
        parts += ["", extra_instruction]
    return "\n".join(parts)


# ==========================================================================
# 裁判调用
# ==========================================================================


@dataclass
class JudgeStats:
    calls: int = 0
    rejected_quote: int = 0
    cannot_determine: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "rejected_by_quote_gate": self.rejected_quote,
            "cannot_determine": self.cannot_determine,
            "rejection_rate": round(
                self.rejected_quote / self.calls, 4
            ) if self.calls else None,
        }


def judge_field(
    gateway: ModelGateway,
    *,
    image_path: str | Path,
    page_no: int,
    field_id: str,
    field_label: str,
    channel_a: str | None = None,
    channel_b: str | None = None,
    page_text: str = "",
    extra_instruction: str = "",
    verify_quote: bool = True,
    fuzzy_threshold: float = 0.86,
) -> JudgeOutput:
    """让 VLM 对某字段做裁决，并执行 quote 回读闸门。

    Args:
        page_text: 该页的**全部可用文本**（文本层 + OCR 并集），
            用于 quote 回读。为空时跳过回读（此时不接受其数值）。
    """
    prompt = _build_judge_prompt(
        field_id=field_id,
        field_label=field_label,
        page_no=page_no,
        channel_a=channel_a,
        channel_b=channel_b,
        extra_instruction=extra_instruction,
    )
    messages = [
        text_message("system", _JUDGE_SYSTEM),
        image_message("user", text=prompt, image_paths=[image_path], detail="high"),
    ]

    result = gateway.call(
        role="vision",
        messages=messages,
        json_schema=JUDGE_SCHEMA,
        prompt_id="r1.vlm_judge_field",
        prompt_version="1.0.0",
    )

    if result.parsed is None:
        raise HarnessError("VLM 未返回结构化结果", field_id=field_id)

    out = JudgeOutput.model_validate(result.parsed)

    # —— quote 回读硬闸门 ——
    if verify_quote:
        _apply_quote_gate(out, page_text, field_id=field_id, fuzzy_threshold=fuzzy_threshold)

    return out


def _apply_quote_gate(
    out: JudgeOutput,
    page_text: str,
    *,
    field_id: str,
    fuzzy_threshold: float,
) -> None:
    """执行回读校验。不通过则标记 rejected（**不抛异常**，让调用方决定降级策略）。"""
    if not page_text:
        out.quote_status = QuoteMatch.MISS.value
        out.rejected = True
        out.rejection_reason = "该页无可用文本，无法执行 quote 回读，拒绝采信视觉读数"
        return

    if not out.quote:
        # 没给 quote：不直接拒绝，但要求数值能在页面文本中找到
        if out.value_raw and out.value_raw.replace(",", "") in page_text.replace(",", ""):
            out.quote_status = QuoteMatch.EXACT.value
            return
        out.quote_status = QuoteMatch.MISS.value
        out.rejected = True
        out.rejection_reason = "未提供 quote 且数值无法在页面文本中定位"
        return

    res = check_quote(out.quote, page_text, fuzzy_threshold=fuzzy_threshold)
    out.quote_status = res.status.value
    out.quote_similarity = res.similarity

    if res.status is QuoteMatch.MISS:
        out.rejected = True
        out.rejection_reason = (
            f"quote 回读失败（相似度 {res.similarity:.3f}）："
            f"模型声称的原文片段在页面文本中找不到 —— 判定为幻觉"
        )
        return

    # 二次确认：数值本身必须出现在 quote 或页面文本中
    if out.value_raw:
        needle = out.value_raw.replace(",", "").replace(" ", "")
        haystack = (out.quote + "\n" + page_text).replace(",", "").replace(" ", "")
        if needle not in haystack:
            out.rejected = True
            out.rejection_reason = (
                f"数值 {out.value_raw!r} 未出现在 quote 或页面文本中 —— 判定为幻觉"
            )


def build_metric_catalog(
    metrics_config: dict[str, Any] | None = None,
    *,
    max_items: int = 80,
) -> str:
    """构建给 VLM 看的**规范指标清单**。

    格式：``canonical — 中文标签 ［单位：X］（常见写法：…）``，一行一个。

    **单位必须写进清单**：模型需要据此判断量纲是否匹配。
    实测踩过的坑：模型把「持股 34,469,568 股」归到了
    期望单位为 ``%`` 的 ``shareholding_after`` ——
    数值读对了，但指标名与量纲不符，下游无法使用。

    为什么必须把清单喂给模型：模型只会照着给它的名字写。
    不提供清单时它会自创名字（实测：返回 ``{"文档类型": ...}`` 这样的中文键，
    或把「占公司总股本比例」归到「总股本」），
    这些名字无法与事实库的规范名对齐，读数就进不了库。
    """
    if metrics_config is None:
        try:
            from agent.config import load_config

            metrics_config = load_config("metrics")
        except Exception:
            metrics_config = {}

    lines: list[str] = []
    for m in (metrics_config.get("metrics") or [])[:max_items]:
        canonical = m.get("canonical")
        label = m.get("label", canonical)
        unit = m.get("unit", "")
        aliases = "、".join(m.get("aliases", [])[:4])
        line = f"  {canonical} — {label} ［单位：{unit}］"
        if aliases:
            line += f"（常见写法：{aliases}）"
        lines.append(line)
    return "\n".join(lines) if lines else "  （无指标清单）"


def expected_unit_family(metric: str, metrics_config: dict[str, Any] | None = None) -> str | None:
    """取某规范指标**声明的单位族**（money / share / ratio / per_share）。

    用于校验 VLM 的指标归属是否与量纲匹配。返回 None 表示指标未知。
    """
    global _UNIT_FAMILY_INDEX
    if _UNIT_FAMILY_INDEX is None:
        if metrics_config is None:
            try:
                from agent.config import load_config

                metrics_config = load_config("metrics")
            except Exception:
                metrics_config = {}
        idx: dict[str, str] = {}
        for m in metrics_config.get("metrics") or []:
            unit = str(m.get("unit", ""))
            if unit == "元":
                idx[m["canonical"]] = "money"
            elif unit == "股":
                idx[m["canonical"]] = "share"
            elif unit in ("%", "‰"):
                idx[m["canonical"]] = "ratio"
            elif unit in ("元每股", "元/股"):
                idx[m["canonical"]] = "per_share"
        _UNIT_FAMILY_INDEX = idx
    return _UNIT_FAMILY_INDEX.get(metric)


def unit_family(unit: str) -> str:
    """把页面上的单位写法归到量纲族。

    与 ``pipelines.r1_extract.build_facts._unit_family`` 保持一致 ——
    两处必须用同一套归类，否则闸门判定会自相矛盾。
    """
    u = (unit or "").strip()
    if u in ("元", "千元", "万元", "百万元", "亿元", "万", "亿"):
        return "money"
    if u in ("股", "千股", "万股", "亿股"):
        return "share"
    if u in ("%", "％", "‰", "个百分点"):
        return "ratio"
    if u in ("元/股", "元每股"):
        return "per_share"
    return "other"


_UNIT_FAMILY_INDEX: dict[str, str] | None = None


def scan_page_fields(
    gateway: ModelGateway,
    *,
    image_path: str | Path,
    page_no: int,
    page_text: str = "",
    metric_catalog: str = "",
    extra_instruction: str = "",
    verify_quote: bool = True,
    fuzzy_threshold: float = 0.86,
    max_fields: int = 12,
) -> PageScanResult:
    """整页扫描：一次调用读出页面上**多个**带指标归属的字段。

    与 :func:`judge_field` 的分工：
        judge_field      已知要读哪个字段 → 两路读数对照裁决
        scan_page_fields 不知道页面上有什么 → 把能读的都读出来并归到指标上

    实测动机：早期对扫描页用笼统的 ``page_key_fields`` 做 field_id，
    同一页三个不同股东的持股数被当成「同一指标的多个值」，
    触发 3 条假冲突告警。指标归属必须由模型给出，而不是由程序猜。

    Args:
        metric_catalog: :func:`build_metric_catalog` 的输出。**必传** ——
            不传清单模型会自创指标名。
    """
    if not metric_catalog:
        metric_catalog = build_metric_catalog()

    prompt_parts = [
        f"页码：第 {page_no} 页（图像即此页）",
        "",
        "## 可选指标清单（metric 只能从这里选）",
        "",
        metric_catalog,
        "",
        "## 说明",
        "",
        f"请把这一页上你能确信读出的数值逐条输出，最多 {max_fields} 条。",
        "每条都要给出：规范指标名、页面原样数值串、页面原样单位、连续原文片段。",
        "读不准的**不要写**。清单里没有对应指标时 metric 填 \"unknown\"。",
    ]
    if page_text:
        prompt_parts += [
            "",
            "## 参考：该页已由程序提取到的文本（可能不完整/有错，仅供定位参考）",
            "",
            page_text[:1500],
        ]
    if extra_instruction:
        prompt_parts += ["", extra_instruction]

    messages = [
        text_message("system", _SCAN_SYSTEM),
        image_message("user", text="\n".join(prompt_parts), image_paths=[image_path], detail="high"),
    ]
    result = gateway.call(
        role="vision",
        messages=messages,
        json_schema=PAGE_SCAN_SCHEMA,
        prompt_id="r1.vlm_scan_page",
        prompt_version="1.0.0",
    )
    if result.parsed is None:
        raise HarnessError("VLM 未返回结构化结果", page=page_no)

    scan = PageScanResult.model_validate(result.parsed)

    # —— 逐条过 quote 回读闸门 ——
    for est in scan.fields:
        if est.metric == "unknown":
            scan.unknown_metric_count += 1
        if not verify_quote:
            continue
        _apply_field_quote_gate(est, page_text, fuzzy_threshold=fuzzy_threshold)
        if est.rejected:
            scan.rejected_count += 1

    return scan


def _apply_field_quote_gate(
    est: FieldEstimate,
    page_text: str,
    *,
    fuzzy_threshold: float,
) -> None:
    """对单条视觉读数执行三层校验（就地修改 est）。

    三层依次是：
        1. **量纲匹配** —— 指标声明的单位族必须与读到的单位族一致
        2. **quote 回读** —— 原文片段必须能在页面文本中找到
        3. **数值回读** —— 数值本身必须出现在 quote 或页面文本中
    """
    # —— 1. 量纲闸门 ——
    #    实测踩过的坑：模型把「持股 34,469,568 股」归到了期望单位为 %
    #    的 shareholding_after。数值读对了，但指标名与量纲不符，
    #    入库后同一指标下会同时存在「26.05 %」与「34469568 股」，
    #    下游无法使用，而且会触发大量假冲突。
    if est.metric and est.metric != "unknown":
        want = expected_unit_family(est.metric)
        got = unit_family(est.unit_as_written)
        # per_share 与 money 在写法上容易混（元/股 vs 元），只在明确冲突时拒绝
        if want and got != "other" and got != want:
            est.rejected = True
            est.rejection_reason = (
                f"指标 {est.metric} 的单位族应为 {want}，"
                f"但读到的单位 {est.unit_as_written!r} 属于 {got} —— 指标归属与量纲不符"
            )
            return

    if not page_text:
        est.quote_status = QuoteMatch.MISS.value
        est.rejected = True
        est.rejection_reason = "该页无可用文本，无法执行 quote 回读，拒绝采信视觉读数"
        return

    if not est.quote:
        est.quote_status = QuoteMatch.MISS.value
        est.rejected = True
        est.rejection_reason = "未提供 quote，无法回读校验"
        return

    res = check_quote(est.quote, page_text, fuzzy_threshold=fuzzy_threshold)
    est.quote_status = res.status.value
    est.quote_similarity = res.similarity

    if res.status is QuoteMatch.MISS:
        est.rejected = True
        est.rejection_reason = (
            f"quote 回读失败（相似度 {res.similarity:.3f}）："
            "模型声称的原文片段在页面文本中找不到 —— 判定为幻觉"
        )
        return

    # —— 3. 数值回读 ——
    if est.value_raw:
        needle = est.value_raw.replace(",", "").replace(" ", "")
        haystack = (est.quote + "\n" + page_text).replace(",", "").replace(" ", "")
        if needle not in haystack:
            est.rejected = True
            est.rejection_reason = (
                f"数值 {est.value_raw!r} 未出现在 quote 或页面文本中 —— 判定为幻觉"
            )
            return

    # —— 无量纲字段（日期、代码等）不入库 ——
    #    这类值由 extract_financial_facts 提示词处理，不属于「财务指标」
    if unit_family(est.unit_as_written) == "other":
        est.rejected = True
        est.rejection_reason = (
            f"单位 {est.unit_as_written!r} 不属于任何已知量纲族"
            "（金额/股数/比率/每股）—— 金融指标必须有可归一化的单位"
        )


def read_table_cell(
    gateway: ModelGateway,
    *,
    cell_image_path: str | Path,
    page_no: int,
    row: int,
    col: int,
    page_text: str = "",
    verify_quote: bool = False,
    expected: str = "",
    header: str = "",
    context_image_path: str | Path | None = None,
) -> TableCellReading:
    """让 VLM 读**单个单元格**图像（裁剪放大后）。

    比让 VLM 一次读整张表更可靠：单格图像分辨率高、无相邻干扰。
    代价是调用次数多，因此只对**可疑单元格**使用（见
    :meth:`domain.tools.tables.Table.suspicious_cells`）。

    Args:
        expected: 程序已读到的文本。**必须传给模型做对照** ——
            这样它是在"核对"而不是"从零识别"，两者准确性差别很大。
        header: 该列的列名。给模型上下文，帮助它判断这一格应该是什么。
        context_image_path: **整页**图像。若提供，会在上面用红框标出该格，
            请模型确认「红框位置与我读的是否同一格」——
            这是对**坐标错位**的防护：裁剪框若偏了，
            模型读到的会是邻格内容，而那看起来同样"合理"。
    """
    prompt_parts = [
        f"这是一张财务表格中第 {row+1} 行、第 {col+1} 列的单元格裁剪图"
        f"（原始页码：第 {page_no} 页）。",
    ]
    if header:
        prompt_parts.append(f"该列的列名是：{header!r}。")
    if expected:
        prompt_parts.append(
            f"程序用文本层读到的内容是：{expected!r}。\n"
            "请**核对**它是否正确：正确就原样输出，有错就输出你看到的正确内容。"
        )
    else:
        prompt_parts.append("请逐字读出单元格内的文字。")

    prompt_parts += [
        "",
        "要求：",
        "1. 若是数字，保留页面上原样的千分位与小数位，**不要换算、不要四舍五入**。",
        "2. 若数字被**换行切断**（如「583,96」换行「5.66」），",
        "   在 `text_normalized` 里给出合并后的完整数字（如 \"583,965.66\"）。",
        "3. 空单元格：`text` 返回空字符串，并在 `note` 说明。",
        "4. 若提供了整页图，请核对红框位置与你所读的格子是否同一格，",
        "   填 `position_confirmed`。",
        "5. `quote` 填该单元格在整页上的连续原文（用于回读校验）。",
        "只输出 JSON。",
    ]

    images = [cell_image_path]
    if context_image_path:
        images.append(context_image_path)

    messages = [
        text_message("system", _JUDGE_SYSTEM),
        image_message("user", text="\n".join(prompt_parts), image_paths=images, detail="high"),
    ]
    result = gateway.call(
        role="vision",
        messages=messages,
        json_schema=TABLE_CELL_SCHEMA,
        prompt_id="r1.vlm_read_table_cell",
        prompt_version="1.1.0",
    )
    if result.parsed is None:
        raise HarnessError("VLM 未返回结构化结果", row=row, col=col)

    reading = TableCellReading.model_validate(result.parsed)

    # quote 回读：确证模型引用的原文确实在页面上
    if page_text and reading.quote:
        res = check_quote(reading.quote, page_text)
        reading.quote_status = res.status.value
        reading.quote_similarity = res.similarity
        if verify_quote and res.status is QuoteMatch.MISS:
            reading.rejected = True
            reading.rejection_reason = (
                f"quote 回读失败（相似度 {res.similarity:.3f}）—— 判定为幻觉"
            )
    elif page_text and not reading.quote:
        reading.quote_status = QuoteMatch.MISS.value
        if verify_quote:
            reading.rejected = True
            reading.rejection_reason = "未提供 quote，无法回读校验"

    # 位置校验：模型自己说不一致 —— 直接拒收，避免张冠李戴
    if reading.position_confirmed is False:
        reading.rejected = True
        reading.rejection_reason = "模型确认红框位置与实际所读格不一致 —— 坐标可能错位"

    return reading


# ==========================================================================
# 单元格级表格裁决
# ==========================================================================


@dataclass
class CellAdjudication:
    """一个单元格的裁决记录 —— 同时是它的**证据锚点**。

    锚点三要素：位置（``bbox``）、原文（``quote``）、像素（``crop``）。
    有了这三样，任何人都能回到原始页面复核这一次改写：
    Web 演示台的 ``/cell-crop/<doc>/<page>/<row>/<col>`` 直接返回那张裁图。
    """

    row: int
    col: int
    original: str
    vlm_text: str
    verdict: str
    confidence: float = 0.0
    quote: str | None = None
    note: str = ""
    bbox: list[float] | None = None
    """该单元格在页面上的位置 ``[x0, y0, x1, y1]``（pt）。"""
    crop: str = ""
    """模型实际读的那张裁图的文件名（位于 run 的 ``pages/cells/<doc_id>/``）。"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "row": self.row, "col": self.col, "original": self.original,
            "vlm_text": self.vlm_text, "verdict": self.verdict,
            "confidence": round(self.confidence, 4), "quote": self.quote,
            "note": self.note, "bbox": self.bbox, "crop": self.crop,
        }


@dataclass
class TableAdjudicationReport:
    """一张表的裁决结果。"""

    cells_checked: int = 0
    agree: int = 0
    normalized: int = 0
    """数字完全一致、但原读法**解析不出数值**（换行切断/粘连）而采用模型读法的格数。

    这类改写不改任何一位数字，风险极低，收益却是"这一格从取不到数变成取得到"。
    """
    corrected: int = 0
    unreadable: int = 0
    rejected: int = 0
    errors: int = 0
    arith_failures_before: int = 0
    arith_failures_after: int = 0
    adjudications: list[CellAdjudication] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.corrected > 0 or self.normalized > 0

    @property
    def improved(self) -> bool:
        """算术不平的处数是否减少 —— 裁决是否**真的**有帮助。"""
        return self.arith_failures_after < self.arith_failures_before

    def to_dict(self) -> dict[str, Any]:
        return {
            "cells_checked": self.cells_checked,
            "agree": self.agree,
            "normalized": self.normalized,
            "corrected": self.corrected,
            "unreadable": self.unreadable,
            "rejected": self.rejected,
            "errors": self.errors,
            "arith_failures_before": self.arith_failures_before,
            "arith_failures_after": self.arith_failures_after,
            "improved": self.improved,
            "adjudications": [a.to_dict() for a in self.adjudications],
        }


def _annotate_cell_on_page(
    page_image: str | Path, bbox_pt: tuple[float, float, float, float],
    out_path: str | Path, *, dpi: int = 300,
) -> Path | None:
    """在整页图上用红框标出目标单元格。

    坐标系注意：**这里不做 y 翻转**。PyMuPDF 的 ``page.rect`` 与
    ``get_text()`` 用的是同一套坐标（原点左上、y 向下），因此
    ``pixel_y = bbox_y * dpi / 72`` 就是对的。

    早期实现误以为 bbox 是"PDF 用户空间（原点左下）"，加了
    ``(page_height - y)`` 翻转，于是红框被画在**垂直镜像**的位置 ——
    位置校验不但没起到作用，反而"确认"了错误的裁图。
    详见 :func:`cell_pixel_box`。
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    src = Path(page_image)
    if not src.is_file():
        return None

    img = Image.open(src).convert("RGB")
    w, h = img.size
    scale = dpi / 72.0

    x0, y0, x1, y1 = bbox_pt
    px0 = max(0.0, x0 * scale)
    px1 = min(float(w), x1 * scale)
    py0 = max(0.0, y0 * scale)
    py1 = min(float(h), y1 * scale)
    if px1 <= px0 or py1 <= py0:
        return None

    draw = ImageDraw.Draw(img)
    width = max(2, int(scale / 12))
    draw.rectangle([px0, py0, px1, py1], outline=(220, 30, 30), width=width)

    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    img.save(target)
    return target


def adjudicate_table(
    gateway: ModelGateway,
    *,
    table: "Any",
    page_image_path: str | Path,
    page_no: int,
    page_text: str = "",
    page_words: list[tuple[float, float, float, float, str]] | None = None,
    work_dir: str | Path,
    max_cells: int = 6,
    dpi: int = 300,
    fuzzy_threshold: float = 0.86,
    on_progress: "Any" = None,
    guard: "Any" = None,
) -> TableAdjudicationReport:
    """对一张表格的可疑单元格做**逐格 VLM 裁决**，并就地回写结果。

    为什么是「可疑单元格」而不是整表：
        逐格调用模型成本与延迟都随格数线性增长。而算术校验已经能把
        出错位置**精确定位到行列**，因此只需复核那些格子。

    裁决流程（每格）：
        1. 400dpi 裁剪该单元格 → 交模型精读
        2. 同时提供**整页图 + 红框标注**，让模型确认坐标没错位
        3. 对照程序已读到的文本（模型是在"核对"而非"从零识别"）
        4. quote 回读校验
        5. **几何复核**：该 bbox 内页面上真实印着的字，是否支持模型读数
        6. 回写：把 ``verdict``、``vlm_text`` 等写回 ``TableCell``

    回写策略（重要）：
        * ``agree``      —— 读到的与程序一致，不改
        * ``normalized`` —— 数字一致，仅修复换行切断导致的"取不到数"
        * ``corrected``  —— 不一致，且**模型自述闸门 + 几何佐证全部通过**
        * 其余情况       —— **不改**，只标记，交人工复核

    「不一致就采用模型」是危险的 —— 那等于用另一个可能出错的源
    去覆盖当前值。而"模型自述通过"并不足以证明它读对了：
    实测 80 次"改值"里 75 次是错的，且三道自述闸门全部放行。
    """
    from domain.tools.document import crop_region_png, open_pdf, render_page_png
    from domain.tools.tables import cell_geometry_text, check_table_arithmetic

    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)

    report = TableAdjudicationReport()
    try:
        report.arith_failures_before = len(check_table_arithmetic(table).failures)
    except Exception:
        pass

    suspects = table.suspicious_cells(limit=max_cells)
    if not suspects:
        return report

    # 整页图（带红框）只需渲染一次
    if page_image_path is None or not Path(page_image_path).is_file():
        page_image_path = work / f"page_{page_no:04d}_for_cell.png"
        render_page_png_path = page_image_path
        # 由调用方保证 pdf 可用；这里若失败就退化为无整页图
    header = table.header_row()

    for cell in suspects:
        report.cells_checked += 1
        cell.original_text = cell.text

        cell_img = work / f"cell_p{page_no}_r{cell.row}c{cell.col}.png"
        ctx_img = work / f"cell_p{page_no}_r{cell.row}c{cell.col}_ctx.png"

        try:
            _crop_cell(page_image_path, cell, cell_img, dpi)
            # 裁剪图是**模型实际看到的那张**。裁决结果对不上时，第一件要查的
            # 就是"模型当时看到的到底是不是这个格子"，所以它必须留痕。
            if guard is not None:
                guard.record(cell_img, "write")
        except Exception as exc:
            report.errors += 1
            report.adjudications.append(
                CellAdjudication(cell.row, cell.col, cell.text, "",
                                 "error", note=f"裁剪失败：{exc}")
            )
            continue

        ctx = None
        try:
            ctx = _annotate_cell_on_page(
                page_image_path,
                (cell.bbox.x0, cell.bbox.y0, cell.bbox.x1, cell.bbox.y1),
                ctx_img, dpi=dpi,
            )
            if ctx is not None and guard is not None:
                guard.record(ctx_img, "write")
        except Exception:
            ctx = None

        col_header = header[cell.col] if cell.col < len(header) else ""
        try:
            reading = read_table_cell(
                gateway,
                cell_image_path=cell_img,
                page_no=page_no,
                row=cell.row,
                col=cell.col,
                page_text=page_text,
                verify_quote=True,
                expected=cell.text,
                header=col_header,
                context_image_path=ctx,
            )
        except Exception as exc:
            report.errors += 1
            report.adjudications.append(
                CellAdjudication(cell.row, cell.col, cell.text, "",
                                 "error", note=f"{type(exc).__name__}: {exc}")
            )
            if on_progress:
                on_progress(cell, "error")
            continue

        vlm_text = reading.best_text

        # 何时允许采用模型读数（覆盖原值）
        gates_ok = (
            reading.quote_status in (QuoteMatch.EXACT.value, QuoteMatch.FUZZY.value)
            and reading.position_confirmed is not False
            and reading.self_confidence >= 0.7
            and bool(vlm_text.strip())
        )

        # —— 判定（判定表见 decide_cell_verdict，那里是唯一的事实来源）——
        verdict = decide_cell_verdict(
            cell.text, vlm_text,
            reading_rejected=reading.rejected, gates_ok=gates_ok,
            geometry=cell_geometry_text(page_words, cell.bbox),
        )
        if verdict == VERDICT_CORRECTED:
            cell.text = vlm_text
            cell.confidence = max(cell.confidence, reading.self_confidence)
            cell.source_channel = (cell.source_channel or "text_layer") + "+vlm"
            report.corrected += 1
        elif verdict == VERDICT_NORMALIZED:
            # 数字一位不差，只是原读法被换行切断而解析不出数值。
            # 结果从"这一格取不到数"变成"取得到数"，不存在改错数字的风险。
            cell.text = vlm_text
            cell.source_channel = (cell.source_channel or "text_layer") + "+vlm_norm"
            report.normalized += 1
        elif verdict == VERDICT_AGREE:
            report.agree += 1
        elif verdict == VERDICT_UNREADABLE:
            report.unreadable += 1
        else:
            report.rejected += 1
            if verdict != VERDICT_REJECTED or not reading.rejected:
                reading.rejection_reason = (
                    reading.rejection_reason
                    or "读数不一致但未通过全部闸门，不予覆盖"
                )

        cell.adjudicated = True
        cell.vlm_text = vlm_text
        cell.vlm_confidence = reading.self_confidence
        cell.vlm_quote = reading.quote
        cell.verdict = verdict
        cell.note = reading.note or reading.rejection_reason or ""

        report.adjudications.append(
            CellAdjudication(
                row=cell.row, col=cell.col, original=cell.original_text,
                vlm_text=vlm_text, verdict=verdict,
                confidence=reading.self_confidence, quote=reading.quote,
                note=cell.note,
                # 证据锚点：位置 + 像素。任何人都能据此回到原始页面复核
                bbox=[cell.bbox.x0, cell.bbox.y0, cell.bbox.x1, cell.bbox.y1],
                crop=cell_img.name,
            )
        )
        if on_progress:
            on_progress(cell, verdict)

    try:
        report.arith_failures_after = len(check_table_arithmetic(table).failures)
    except Exception:
        pass

    return report


def _digits(text: str) -> str:
    return "".join(ch for ch in (text or "") if ch.isdigit())


# 单元格裁决的六种结论。`error` 由异常路径产生，不在判定表里。
VERDICT_AGREE = "agree"
VERDICT_NORMALIZED = "normalized"
VERDICT_CORRECTED = "corrected"
VERDICT_UNREADABLE = "unreadable"
VERDICT_REJECTED = "rejected"
VERDICT_ERROR = "error"


def decide_cell_verdict(
    original: str,
    vlm_text: str,
    *,
    reading_rejected: bool = False,
    gates_ok: bool = False,
    geometry: str = "",
) -> str:
    """纯函数：决定一个单元格该怎么处理（不调用模型，可穷举测试）。

    这是整条链路上**最安全攸关**的判断 —— 它决定要不要用模型的读数
    覆盖文本层的读数。因此从"调用模型"的那段代码里拆出来，
    否则只能靠真花钱的端到端运行来验证。

    判定表：

    ======================  ============================  ===============
    条件                    结论                          是否改值
    ======================  ============================  ===============
    quote 回读被拒          ``rejected``                  否
    模型没读出内容          ``unreadable``/``rejected``   否
    数字一致 + 原读法        ``agree``                     否
    可解析
    数字一致 + 原读法        ``normalized``                是（**仅格式**）
    解析不出 + 过闸门
    数字不一致 + 闸门全过    ``corrected``                 是
    + **几何佐证**
    其余                    ``rejected``                  否
    ======================  ============================  ===============

    ``geometry`` 是**该单元格 bbox 内页面上真实印着的字**，
    用于独立复核模型读数（:func:`domain.tools.tables.corroborate_by_geometry`）。
    这道闸门是被一次真实事故逼出来的：某次运行 184 个复核格有 80 个被
    "改值"，独立复核后 **75 个是错的** —— 模型读数不在那个格子里，
    程序读数才在。原有的三道闸门全部失效，因为它们校验的都是**模型自述**：

    * quote 回读只校验"这段文字在整页某处存在"，读错格子照样通过
    * 位置确认用的是同一张错图渲染的红框，图文自洽，必然通过
    * 置信度自评恒为 0.95，毫无区分度

    也就是说 —— **只靠模型自述的闸门等于没有闸门**。

    ``normalized`` 这一档不需要几何佐证：数字一位不差，采用模型读法
    不存在改错数字的风险，收益却是"这一格从取不到数变成取得到"。
    """
    if reading_rejected:
        return VERDICT_REJECTED
    if not vlm_text.strip():
        # 模型什么都没读出来 → 无论闸门如何都不得覆盖。
        # 调用方的 ``gates_ok`` 里已经含 ``bool(vlm_text.strip())``，
        # 这里再挡一次是**纵深防御**：这是安全攸关的函数，
        # 不能依赖调用方把条件拼对。
        return VERDICT_UNREADABLE if not original.strip() else VERDICT_REJECTED

    if _digits(original) == _digits(vlm_text):
        if (
            gates_ok
            and cell_number(original) is None
            and cell_number(vlm_text) is not None
        ):
            return VERDICT_NORMALIZED
        return VERDICT_AGREE

    # 数值不一致 —— 这是唯一会改**数字**的分支，闸门最严：
    # 模型自述 + 几何佐证，缺一不可。
    if gates_ok and corroborate_by_geometry(geometry, original, vlm_text) == "corroborated":
        return VERDICT_CORRECTED
    return VERDICT_REJECTED


def cell_pixel_box(
    bbox: "Any", dpi: int, image_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    """把单元格 bbox 换算成整页图像上的像素框 ``(x0, y0, x1, y1)``。

    ⚠️ **不做 y 翻转**。这是本项目踩过的最贵的一个坑：

    PyMuPDF 的页面坐标**原点在左上、y 向下增大**（``page.rect`` 与
    ``get_text()`` 用的是同一套），而 PDF 用户空间的数学约定是原点在左下。
    早期实现按后者写了 ``y = (page_height - bbox.y) * scale``，
    于是**每一次裁图都沿页面中线上下镜像**：

    * 第 1 行的格子裁出来是第 22 行的内容
    * 模型如实读出它看到的东西 → "不一致" → 高置信度 → 覆盖程序读数
    * 一次真实运行 184 个复核格被"改值" 80 个，独立复核后 **75 个是错的**

    而且它**不报任何错**：镜像后的坐标依然是合法坐标，图依然裁得出来，
    只是内容不对。判据只能来自"裁出来的像素是不是那个格子" ——
    见 ``tests/test_tables.py::test_crop_cell_matches_pdf_clip_render``。
    """
    scale = dpi / 72.0
    w, h = image_size
    x0 = max(0, int(bbox.x0 * scale) - 3)
    x1 = min(w, int(bbox.x1 * scale) + 3)
    y0 = max(0, int(bbox.y0 * scale) - 3)
    y1 = min(h, int(bbox.y1 * scale) + 3)
    return x0, y0, x1, y1


def _crop_cell(
    page_image: str | Path, cell: "Any", out: Path, dpi: int,
) -> Path:
    """从**整页图像**上裁出单元格（而不是重新渲染 PDF）。

    为什么这样：整页图已经在内存里/磁盘上，裁剪是纯像素操作，
    比"重新打开 PDF 再 clip 渲染"快一个数量级，且画质完全一致。
    代价是必须把 bbox 正确换算成像素 —— 见 :func:`cell_pixel_box`。
    """
    from PIL import Image

    img = Image.open(page_image).convert("RGB")
    x0, y0, x1, y1 = cell_pixel_box(cell.bbox, dpi, img.size)
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"单元格 bbox 无效：{cell.bbox}")

    crop = img.crop((x0, y0, x1, y1))
    # 放大 1.6 倍：模型对小字号数字的识别率明显提升
    crop = crop.resize(
        (max(1, int(crop.width * 1.6)), max(1, int(crop.height * 1.6))),
        Image.Resampling.LANCZOS,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    crop.save(out)
    return out


# ==========================================================================
# 工具封装（供 ToolRegistry 使用；需在上下文中注入 gateway）
# ==========================================================================

_GATEWAY_REF: dict[str, ModelGateway] = {}


def bind_gateway(gateway: ModelGateway) -> None:
    """把网关注入模块级引用，供工具接口调用。

    注意：这是**工具注册表与模型网关之间的唯一胶水**，
    由 harness 在启动时绑定一次。工具函数本身不持有模型配置。
    """
    _GATEWAY_REF["gw"] = gateway


def get_gateway() -> ModelGateway:
    gw = _GATEWAY_REF.get("gw")
    if gw is None:
        raise HarnessError(
            "VLM 工具尚未绑定模型网关",
            hint="harness 启动时会调用 domain.tools.vlm.bind_gateway()",
        )
    return gw


class JudgeFieldIn(BaseModel):
    image_path: str
    page_no: int = Field(ge=1)
    field_id: str
    field_label: str
    channel_a: str | None = None
    channel_b: str | None = None
    page_text: str = ""


@tool(
    name="vlm.judge_field",
    description="多模态裁判：对照页面图像判断字段真实取值，强制 quote 回读防幻觉。",
    params=JudgeFieldIn,
    side_effect="network",
    determinism="deterministic",
    cost="expensive",
    requires_evidence=True,
    timeout_s=180.0,
    tags=["vlm", "layer3", "anti_hallucination"],
)
def judge_field_tool(
    image_path: str,
    page_no: int,
    field_id: str,
    field_label: str,
    channel_a: str | None = None,
    channel_b: str | None = None,
    page_text: str = "",
) -> dict[str, Any]:
    out = judge_field(
        get_gateway(),
        image_path=image_path,
        page_no=page_no,
        field_id=field_id,
        field_label=field_label,
        channel_a=channel_a,
        channel_b=channel_b,
        page_text=page_text,
    )
    return out.model_dump(mode="json")


class ReadTableCellIn(BaseModel):
    cell_image_path: str
    page_no: int = Field(ge=1)
    row: int = Field(ge=0)
    col: int = Field(ge=0)
    page_text: str = ""


@tool(
    name="vlm.read_table_cell",
    description="读取单个表格单元格图像（高分辨率裁剪），逐字照抄不做任何加工。",
    params=ReadTableCellIn,
    side_effect="network",
    determinism="deterministic",
    cost="expensive",
    timeout_s=180.0,
    tags=["vlm", "layer3", "table"],
)
def read_table_cell_tool(
    cell_image_path: str, page_no: int, row: int, col: int, page_text: str = ""
) -> dict[str, Any]:
    r = read_table_cell(
        get_gateway(),
        cell_image_path=cell_image_path,
        page_no=page_no,
        row=row,
        col=col,
        page_text=page_text,
    )
    return r.model_dump(mode="json")


__all__ = [
    "JUDGE_SCHEMA",
    "PAGE_SCAN_SCHEMA",
    "TABLE_CELL_SCHEMA",
    "JudgeOutput",
    "FieldEstimate",
    "PageScanResult",
    "TableCellReading",
    "CellAdjudication",
    "TableAdjudicationReport",
    "JudgeStats",
    "judge_field",
    "scan_page_fields",
    "build_metric_catalog",
    "read_table_cell",
    "adjudicate_table",
    "decide_cell_verdict",
    "VERDICT_AGREE",
    "VERDICT_NORMALIZED",
    "VERDICT_CORRECTED",
    "VERDICT_UNREADABLE",
    "VERDICT_REJECTED",
    "VERDICT_ERROR",
    "bind_gateway",
    "get_gateway",
    "judge_field_tool",
    "read_table_cell_tool",
]
