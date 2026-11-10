"""P1 契约冻结 —— 全局数据契约（Single Source of Truth）。

设计原则
--------
1. **无锚点不入库**：任何 ``Fact`` 必须携带至少一个 ``EvidenceSpan`` 引用。
2. **无口径不比对**：任何数字必须带 ``unit`` + ``period`` + ``caliber``，
   否则 R5 的确定性比对无法进行（口径缺失 = 比对无意义）。
3. **ID 可寻址**：所有 ID 由内容哈希派生，不依赖页码/顺序，
   保证 PDF 重渲染或换 OCR 引擎后仍然稳定。
4. **LLM 不产数字**：模型只能产出 ``raw_text``（原文片段），
   数值换算、四舍五入、求和一律由确定性代码完成。

本模块是全项目唯一的数据结构定义处。任何模块不得自定义
跨模块传输的数据结构；新增字段请在此处修改并同步 bump CONTRACT_VERSION。
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --------------------------------------------------------------------------
# 契约版本：任何破坏性变更必须 bump，并写入 runs/<run_id>/manifest.json
# --------------------------------------------------------------------------
CONTRACT_VERSION = "1.0.0"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sha256_text(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x1f")  # unit separator，避免拼接歧义
    return h.hexdigest()


def derive_id(prefix: str, *parts: str, length: int = 16) -> str:
    """由内容派生稳定 ID。同一内容永远得到同一 ID。"""
    return f"{prefix}_{sha256_text(*parts)[:length]}"


# 单位后缀（剥离后判断 raw_text 是否表示零）
_UNIT_SUFFIX = re.compile(
    r"(亿元|百万元|万元|千元|元/股|元每股|亿股|万股|千股|股|%|％|‰|倍|元)\s*$"
)
# 表示「无数据」的占位符
_ZERO_PLACEHOLDERS = {"-", "—", "－", "/", "不适用", "无", "n/a", "N/A", ""}


def _raw_text_is_zero(raw: str) -> bool:
    """判断原文串是否**真的表示零**（而非解析失败）。

    先剥单位与空白，再看剩余部分：
    ``"0万元"`` / ``"0.00%"`` / ``"-"`` 都算零；
    ``"1,200,450.00万元"`` 显然不算（那是解析失败）。
    """
    s = (raw or "").strip()
    if s in _ZERO_PLACEHOLDERS:
        return True
    s = _UNIT_SUFFIX.sub("", s).strip()
    if s in _ZERO_PLACEHOLDERS:
        return True
    try:
        return Decimal(s.replace(",", "")) == 0
    except (InvalidOperation, ValueError):
        return False


# ==========================================================================
# 枚举
# ==========================================================================


class DocumentType(str, Enum):
    ANNOUNCEMENT = "announcement"      # 公告（质押/中标/股权变动）
    ANNUAL_REPORT = "annual_report"    # 年报
    SEMI_REPORT = "semi_report"        # 半年报
    QUARTER_REPORT = "quarter_report"  # 季报
    PROSPECTUS = "prospectus"          # 招股书
    RESEARCH_DRAFT = "research_draft"  # 研究报告草稿（R5 输入）
    COMPS_TABLE = "comps_table"        # 可比公司数据
    UNKNOWN = "unknown"


class SourceLayer(str, Enum):
    """事实的来源层 —— 直接决定基础置信度。"""

    L1_TEXT_LAYER = "L1_text_layer"   # PDF 原生文本层（最高置信）
    L2_OCR = "L2_ocr"                 # OCR + 规则校验
    L3_VLM = "L3_vlm"                 # 多模态大模型读图/裁决
    L4_MANUAL = "L4_manual"           # 人工录入（可覆盖一切）
    COMPUTED = "computed"             # 由其他事实计算派生（R4 中间量）


# 基础置信度：与 2.6 节置信度模型一一对应
SOURCE_LAYER_BASE_CONFIDENCE: dict[SourceLayer, float] = {
    SourceLayer.L1_TEXT_LAYER: 0.95,
    SourceLayer.L2_OCR: 0.80,
    SourceLayer.L3_VLM: 0.70,
    SourceLayer.L4_MANUAL: 0.99,
    SourceLayer.COMPUTED: 0.90,
}


class PageRoute(str, Enum):
    """逐页路由决策（configs/routing.yaml 驱动）。"""

    A_TEXT_ONLY = "A_text_only"        # 文本层充足且干净，直取
    B_SCANNED = "B_scanned"            # 无文本层/低文本，走 OCR + VLM 双路
    C_GARBLED = "C_garbled"            # 文本层乱码，走 OCR + VLM 双路
    D_TABLE = "D_table"                # 表格页，走三引擎表决
    E_MIXED = "E_mixed"                # 图文混排，文本层 + 局部图像


class Verdict(str, Enum):
    """VLM 裁判结论。cannot_determine 是合法且被鼓励的输出。"""

    CHANNEL_A = "A"                     # 采信通道 A
    CHANNEL_B = "B"                     # 采信通道 B
    BOTH_AGREE = "both_agree"           # 双路一致
    BOTH_WRONG = "both_wrong"           # 双路皆错，值取自 VLM 读图
    CANNOT_DETERMINE = "cannot_determine"


class StatementScope(str, Enum):
    """报表口径 —— R5 的 E3「统计口径」错误依赖此字段。

    ★ 为什么需要 ``SEGMENT_TOTAL`` / ``SEGMENT_DETAIL``（2026-09 第十三轮新增）
    ---------------------------------------------------------------------
    分部报告的列头是**业务分部名 + 合计**，不是期间：

        |（行标签）  | 油气和新能源 | 炼油化工和新材料 | … | **合计** |
        | 分部收入   | 824,808     | 1,078,043      | … | 4,882,272 |
        | 资本性支出 | 205,094     | 47,772         | … | **269,089** |

    其中 ``合计`` 列与分部列**语义完全不同**：合计列的 269,089 就是集团口径的
    资本开支，而 205,094 只是勘探与生产板块的。但在补齐这两个值之前，
    提取器只能把它们全部标成 ``consolidated`` —— 同表同期同主体，于是被
    ``store._check_internal_conflict`` 判为"多口径冲突、需人工判定"，
    集团合计因此**取不出来**（第十一轮实测：12 条 capex 候选全部 need_review）。

    第十二轮按单元格坐标重建列结构之后，``合计`` 列的位置可以被稳定定位，
    于是"按取值落在哪一列定口径"成为可做的事 —— 这就是这两个值的来源。

    ⚠️ ``SEGMENT_TOTAL`` 与 ``CONSOLIDATED`` 在**经济含义上等价**
    （都是集团整体），但**来源不同**：前者来自分部报告的合计列，
    后者来自合并报表。刻意分成两个值而不是合并成一个，理由有二：

    1. **可追溯**：报告里能说清这个数是"分部报告合计列"还是"合并报表"，
       审计时可以分别核对 —— 合并成一个值就永久丢失了这个信息。
    2. **不静默合并**：两者数值可能不一致（口径调整、重述），
       合并成一个值会让这种不一致消失。保持分离，让它在冲突守卫里显形。

    下游凡是判断"是不是集团口径"的地方，**两个值都要算**（见
    ``r4_valuation.assumptions._caliber_rank`` 与
    ``domain.tools.audit_rules._caliber_rank_local``）。
    """

    CONSOLIDATED = "consolidated"        # 合并报表
    PARENT_ONLY = "parent_only"          # 母公司报表
    SEGMENT_TOTAL = "segment_total"      # 分部报告的**合计列**（集团口径）
    SEGMENT_DETAIL = "segment_detail"    # 分部报告的**分部分列**（板块口径）
    UNKNOWN = "unknown"


class ProfitBasis(str, Enum):
    """利润口径 —— R5 的 E2/E3 依赖此字段。"""

    NET_PARENT = "net_parent"            # 归母净利润
    NET_PARENT_EX_NONREC = "net_parent_ex_nonrec"  # 扣非归母净利润
    NET_TOTAL = "net_total"              # 净利润（含少数股东）
    UNKNOWN = "unknown"


class PeriodType(str, Enum):
    POINT_IN_TIME = "point_in_time"      # 时点（资产负债表）
    DURATION_YTD = "duration_ytd"        # 年初至报告期末（累计）
    DURATION_SINGLE_Q = "duration_single_q"  # 单季度
    DURATION_FY = "duration_fy"          # 完整会计年度
    UNKNOWN = "unknown"


class Severity(str, Enum):
    CONFIRMED = "confirmed"   # 🔴 确认错误：与事实库硬冲突 / 算术错误
    SUSPECTED = "suspected"   # 🟡 疑似问题：口径不明 / 需人工判断
    ADVISORY = "advisory"     # 🔵 提示：规范性建议


class ErrorType(str, Enum):
    """R5 五类错误 —— 与赛题「单位、收入利润、估值倍数、统计口径、引用」对齐。"""

    E1_UNIT = "E1_unit"
    E2_MULTIPLE = "E2_valuation_multiple"
    E3_CALIBER = "E3_statistical_caliber"
    E4_CITATION = "E4_citation"
    E5_ARITHMETIC = "E5_arithmetic_consistency"
    E6_QUALITATIVE = "E6_qualitative"  # 定性表述与数字矛盾（扩展）


class AssertionStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"


class TraceKind(str, Enum):
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    FILE_ACCESS = "file_access"
    ASSERTION = "assertion"
    ROUTING = "routing"
    STAGE = "stage"
    ERROR = "error"


# ==========================================================================
# L1 证据层：文档 / 页 / 块 / 证据锚点
# ==========================================================================


class DocumentManifest(BaseModel):
    """一份原始材料的登记信息。SHA256 是后续一切可复现性的根。"""

    model_config = ConfigDict(extra="forbid")

    doc_id: str
    filename: str
    rel_path: str = Field(description="相对于 data/raw/ 的路径，禁止绝对路径入库")
    sha256: str
    page_count: int = Field(ge=0)
    doc_type: DocumentType = DocumentType.UNKNOWN
    issued_date: date | None = None
    registered_at: datetime = Field(default_factory=utcnow)

    # 预扫描汇总（由 ingest 阶段填充）
    has_text_layer: bool | None = None
    page_routes: dict[int, PageRoute] = Field(
        default_factory=dict, description="页码(1-based) -> 路由决策"
    )
    route_reasons: dict[int, str] = Field(
        default_factory=dict, description="页码 -> 路由判定理由（可追溯要求）"
    )
    parser_backend: str = Field(default="pymupdf", description="pymupdf | pypdfium2")
    ocr_engine: str | None = None

    @field_validator("rel_path")
    @classmethod
    def _no_absolute(cls, v: str) -> str:
        if v.startswith(("/", "\\")) or (len(v) > 1 and v[1] == ":"):
            raise ValueError("rel_path 必须是相对路径，禁止将绝对路径写入产物")
        return v.replace("\\", "/")


class BBox(BaseModel):
    """页面坐标（PDF user space，原点左下）。用于 Web 端原文高亮。"""

    model_config = ConfigDict(extra="forbid")

    x0: float
    y0: float
    x1: float
    y1: float

    @model_validator(mode="after")
    def _ordered(self) -> "BBox":
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError("BBox 必须满足 x1>=x0 且 y1>=y0")
        return self

    def area(self) -> float:
        return max(0.0, self.x1 - self.x0) * max(0.0, self.y1 - self.y0)

    def union(self, other: "BBox") -> "BBox":
        return BBox(
            x0=min(self.x0, other.x0),
            y0=min(self.y0, other.y0),
            x1=max(self.x1, other.x1),
            y1=max(self.y1, other.y1),
        )

    def iou(self, other: "BBox") -> float:
        inter = self.intersection(other)
        if inter is None:
            return 0.0
        union_area = self.area() + other.area() - inter.area()
        return inter.area() / union_area if union_area > 0 else 0.0

    def intersection(self, other: "BBox") -> "BBox | None":
        x0, y0 = max(self.x0, other.x0), max(self.y0, other.y0)
        x1, y1 = min(self.x1, other.x1), min(self.y1, other.y1)
        if x1 <= x0 or y1 <= y0:
            return None
        return BBox(x0=x0, y0=y0, x1=x1, y1=y1)


class Chunk(BaseModel):
    """页内文本块。chunk_id 由内容派生，因此跨重渲染稳定。

    注意：页码只用于展示与定位图像，**绝不作为主键**。
    """

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    doc_id: str
    page: int = Field(ge=1, description="1-based 页码，仅用于展示/定位")
    bbox: BBox
    text: str
    source_layer: SourceLayer
    reading_order: int = Field(default=0, ge=0)
    block_type: Literal["text", "title", "table", "figure", "header", "footer"] = "text"
    section_path: list[str] = Field(default_factory=list, description="标题层级路径")

    @staticmethod
    def make_id(doc_id: str, page: int, text: str) -> str:
        return derive_id("chk", doc_id, str(page), text.strip())


class EvidenceSpan(BaseModel):
    """证据锚点 —— 事实与原文之间的唯一桥梁。

    ``quote`` 是硬闸门：入库前必须能回原文匹配成功（见 verify.quote_check）。
    """

    model_config = ConfigDict(extra="forbid")

    span_id: str
    doc_id: str
    chunk_id: str
    page: int = Field(ge=1)
    bbox: BBox | None = None
    quote: str = Field(min_length=1, description="原文连续片段，必须逐字可回读")
    char_range: tuple[int, int] | None = None
    source_layer: SourceLayer

    @staticmethod
    def make_id(doc_id: str, chunk_id: str, quote: str) -> str:
        return derive_id("evd", doc_id, chunk_id, quote.strip())


# ==========================================================================
# R1 事实层
# ==========================================================================


class ChannelReading(BaseModel):
    """单个解析通道对某字段的读数 —— 用于多路表决与溯源展示。"""

    model_config = ConfigDict(extra="forbid")

    channel: Literal["text_layer", "ocr", "vlm", "table_engine", "manual"]
    raw_text: str | None = None
    page: int | None = None
    bbox: BBox | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    note: str | None = None


class ConfidenceBreakdown(BaseModel):
    """置信度的可复算明细 —— 必须原样输出到报告与 Web 面板。

    加分项：评审可以逐项质疑并自行重算。禁止只给一个黑盒分数。
    """

    model_config = ConfigDict(extra="forbid")

    base: float = Field(ge=0.0, le=1.0)
    adjustments: list["ConfidenceAdjustment"] = Field(default_factory=list)

    @property
    def score(self) -> float:
        s = self.base + sum(a.delta for a in self.adjustments)
        return round(max(0.0, min(0.99, s)), 4)

    def explain(self) -> str:
        parts = [f"base={self.base:.2f}"]
        parts += [f"{a.reason} {a.delta:+.2f}" for a in self.adjustments]
        return " | ".join(parts) + f" => {self.score:.2f}"


class ConfidenceAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str
    delta: float = Field(ge=-1.0, le=1.0)


class Fact(BaseModel):
    """事实库的一条记录 —— 全项目唯一的数字真源。

    R4 的假设依据、R5 的比对基准都从这里取。任何下游模块
    **不得自行解析原始文档**，只能消费本结构。
    """

    model_config = ConfigDict(extra="forbid")

    fact_id: str
    doc_id: str
    entity: str = Field(description="主体，如公司名或股票代码")
    metric: str = Field(description="指标规范名，见 configs/metrics.yaml")

    # —— 值的三元组：数值 + 单位 + 口径，缺一不可比对 ——
    value: Decimal
    unit: str = Field(description="归一化后的单位：元/万元/亿元/%/股/万股/倍/元每股…")
    raw_text: str = Field(description="页面上原样出现的字符串，禁止改写")

    period: str = Field(description="报告期，如 2025A / 2025H1 / 2025Q3 / 2025-06-30")
    period_type: PeriodType = PeriodType.UNKNOWN
    statement_scope: StatementScope = StatementScope.UNKNOWN
    profit_basis: ProfitBasis = ProfitBasis.UNKNOWN

    source_layer: SourceLayer
    evidence_span_ids: list[str] = Field(min_length=1)  # ★ 无锚点不入库
    channel_readings: list[ChannelReading] = Field(default_factory=list)

    confidence: float = Field(ge=0.0, le=1.0)
    confidence_breakdown: ConfidenceBreakdown

    needs_review: bool = False
    review_reason: str | None = None

    extractor: str = Field(description="prompt_id@version 或 rule_engine@version")
    extracted_at: datetime = Field(
        default_factory=utcnow,
        description=(
            "抽取时间。**复现流水线会统一传入 run 的 started_at**，"
            "否则同一输入两次运行的时间戳不同，产物哈希无法一致。"
        ),
    )

    @model_validator(mode="after")
    def _gate(self) -> "Fact":
        # 硬闸门 1：无证据锚点直接拒收
        if not self.evidence_span_ids:
            raise ValueError("Fact 必须携带至少一个 evidence_span_id（无锚点不入库）")
        # 硬闸门 2：低置信事实必须标记复查
        if self.confidence < 0.60 and not self.needs_review:
            raise ValueError("confidence<0.60 的事实必须置 needs_review=True")
        # 硬闸门 3：声称有值却解析成 0，通常是解析失败
        #
        # ⚠️ 判据必须做**单位剥离**再比较。早期实现拿 raw_text 直接比对
        # 一个固定的零值字面量集合，于是 `raw_text='0万元'`、`'0.00%'`
        # 这类**合法的零值**被误判为可疑并抛错，整条流水线中断（实测踩过）。
        if self.value == 0 and not _raw_text_is_zero(self.raw_text):
            raise ValueError(
                f"value=0 但 raw_text={self.raw_text!r} 可疑，请标记 needs_review"
            )
        return self

    @property
    def caliber_key(self) -> str:
        """比对键：R5 用它判断两个数字是否「同一口径、可比较」。"""
        return f"{self.metric}|{self.unit}|{self.period}|{self.statement_scope.value}|{self.profit_basis.value}"


class FactSet(BaseModel):
    """一次 R1 运行的完整产物 —— 写入 runs/<id>/facts/facts.json。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: str = CONTRACT_VERSION
    run_id: str
    built_at: datetime = Field(
        default_factory=utcnow,
        description=(
            "构建时间。**复现时必须传入 run 的 started_at**，"
            "否则两次运行的产物哈希无法一致，『可复现』无从证明。"
        ),
    )
    documents: list[DocumentManifest] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    evidence: list[EvidenceSpan] = Field(default_factory=list)
    needs_review_count: int = 0

    def by_metric(self, metric: str) -> list[Fact]:
        return [f for f in self.facts if f.metric == metric]

    def caliber_index(self) -> dict[str, list[Fact]]:
        """口径键 -> 事实列表。R5 比对的核心索引。"""
        idx: dict[str, list[Fact]] = {}
        for f in self.facts:
            idx.setdefault(f.caliber_key, []).append(f)
        return idx


# ==========================================================================
# R4 估值层
# ==========================================================================


class Assumption(BaseModel):
    """经营假设 —— 每个假设必须能指向事实或标注为主观判断。"""

    model_config = ConfigDict(extra="forbid")

    assumption_id: str
    name: str
    value: Decimal
    unit: str
    # 证据链：来自事实库则填 fact_id；主观假设则留空并置 is_judgment=True
    source_fact_ids: list[str] = Field(default_factory=list)
    is_judgment: bool = Field(
        default=False, description="True 表示主观判断，必须在报告中标注为『观点』"
    )
    basis: str = Field(min_length=1, description="依据说明，写入报告假设表")
    low: Decimal | None = None
    high: Decimal | None = None

    @model_validator(mode="after")
    def _must_be_grounded(self) -> "Assumption":
        # 官方要求：明确区分事实、推论与观点
        if not self.source_fact_ids and not self.is_judgment:
            raise ValueError(
                f"假设 {self.name} 既无事实来源也未标注为主观判断，"
                "违反『事实/推论/观点必须可区分』要求"
            )
        return self


class ForecastYear(BaseModel):
    """单一年度预测三表联动结果。单位统一为『元』。"""

    model_config = ConfigDict(extra="forbid")

    year: int
    revenue: Decimal
    ebit: Decimal
    nopat: Decimal
    depreciation_amortization: Decimal
    capex: Decimal
    nwc_change: Decimal
    fcff: Decimal
    # 勾稽所需的辅助量
    net_income: Decimal
    total_assets: Decimal
    total_liabilities: Decimal
    total_equity: Decimal
    cash_end: Decimal
    retained_earnings: Decimal


class DCFResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wacc: Decimal
    terminal_growth: Decimal
    explicit_years: list[ForecastYear]
    terminal_value: Decimal
    enterprise_value: Decimal
    net_debt: Decimal
    minority_interest: Decimal
    equity_value: Decimal
    shares_outstanding: Decimal
    value_per_share: Decimal
    terminal_value_share: Decimal = Field(
        description="终值占比。>0.85 通常意味着模型过度依赖永续假设，需在报告中提示"
    )


class CompsPeer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    ticker: str
    market_cap: Decimal
    pe: Decimal | None = None
    pb: Decimal | None = None
    ev_ebitda: Decimal | None = None
    ps: Decimal | None = None
    multiple_basis: str = Field(description="静态/TTM/动态 —— E2 核查依赖此标注")
    source_fact_ids: list[str] = Field(default_factory=list)


class CompsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    peers: list[CompsPeer]
    peer_selection_rule: str = Field(description="筛选规则（写死在代码中，可审）")
    median_pe: Decimal | None = None
    median_pb: Decimal | None = None
    median_ev_ebitda: Decimal | None = None
    implied_value_per_share_low: Decimal
    implied_value_per_share_mid: Decimal | None = None
    implied_value_per_share_high: Decimal
    illiquidity_discount: Decimal = Decimal("0")


class SensitivityCell(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x_value: Decimal
    y_value: Decimal
    value_per_share: Decimal


class SensitivityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x_name: str
    y_name: str
    grid: list[SensitivityCell]
    tornado: list[dict[str, Any]] = Field(
        default_factory=list, description="单变量影响排序 [{assumption, low, high, swing}]"
    )


class ValuationAssertion(BaseModel):
    """财务勾稽断言结果 —— 全绿才允许出报告。"""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(description="A1..A6，见 configs/assertions.yaml")
    name: str
    status: AssertionStatus
    detail: str
    lhs: Decimal | None = None
    rhs: Decimal | None = None
    tolerance: Decimal = Decimal("0")


class ValuationResult(BaseModel):
    """一次 R4 运行的完整产物 —— runs/<id>/valuation/valuation.json。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: str = CONTRACT_VERSION
    run_id: str
    entity: str
    as_of: date
    built_at: datetime = Field(
        default_factory=utcnow,
        description="构建时间。复现时传入 run 的 started_at 以保证产物哈希一致。",
    )

    assumptions: list[Assumption]
    dcf: DCFResult
    comps: CompsResult
    sensitivity: SensitivityResult

    comps_provenance: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "可比公司数据的**来源与时点**：快照路径、行情日期、接口来源、"
            "倍数算式。放进产物契约是刻意的 —— 读者据此判断"
            "「这个倍数区间是从哪来的」，而不必相信报告里的转述。"
        ),
    )

    value_range_low: Decimal
    value_range_high: Decimal
    value_range_mid: Decimal
    weighting_note: str = Field(description="DCF 与相对估值的加权方式及理由")

    risk_factors: list[str] = Field(default_factory=list)
    applicability: str = Field(default="", description="适用范围声明")
    assertions: list[ValuationAssertion] = Field(default_factory=list)

    @property
    def assertions_all_passed(self) -> bool:
        """没有任何 FAIL（SKIPPED 不算失败）。"""
        return not any(a.status is AssertionStatus.FAIL for a in self.assertions)

    @property
    def assertions_skipped(self) -> list[ValuationAssertion]:
        return [a for a in self.assertions if a.status is AssertionStatus.SKIPPED]

    @property
    def is_usable(self) -> bool:
        """模型是否可用于对外输出结论。

        判据是**没有 FAIL**，而不是**全部 PASS**。
        原因：缺辅助数据时断言会被标记 SKIPPED —— 如实声明「本次未校验」，
        这与 FAIL（校验过且不通过）是两件完全不同的事。
        把 SKIPPED 当失败会得出「因为没数据所以模型不可用」这种荒谬结论。

        但缺口必须**显式暴露**给读者：见 ``assertions_skipped`` 与
        ``verification_coverage``，报告会如实列出哪些勾稽关系本次未能验证。
        """
        return self.assertions_all_passed

    @property
    def verification_coverage(self) -> float:
        """勾稽验证覆盖率 = PASS / 总数。

        这是**诚实度**的度量：0.6 表示有 40% 的勾稽关系本次没验证到，
        读者应当据此打折看待结论。
        """
        if not self.assertions:
            return 0.0
        passed = sum(1 for a in self.assertions if a.status is AssertionStatus.PASS)
        return round(passed / len(self.assertions), 4)


# ==========================================================================
# R5 核查层
# ==========================================================================


class FindingLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: str | None = None
    char_range: tuple[int, int] | None = None
    page: int | None = None
    quote: str = Field(min_length=1, description="报告草稿中的原文片段")


class Finding(BaseModel):
    """一条核查发现 —— 必须带来源定位与可执行修改建议（官方考察点）。"""

    model_config = ConfigDict(extra="forbid")

    finding_id: str
    error_type: ErrorType
    severity: Severity
    location: FindingLocation
    draft_id: str | None = Field(
        default=None,
        description=(
            "该发现出自哪份草稿。多草稿核查时**必需** —— 否则产物里只有"
            "「有一条错误」而不知道是哪份文件里的，位置信息（section/char_range）"
            "也就失去了参照物。"
        ),
    )

    reported_value: str | None = None
    correct_value: str | None = None

    source_evidence: list[str] = Field(
        default_factory=list, description="evidence_span_id 或 fact_id，用于来源定位"
    )
    caliber: str | None = None
    reason: str = Field(min_length=1)
    suggestion: str = Field(min_length=1, description="可执行的修改建议")

    confidence: float = Field(ge=0.0, le=1.0)
    detector: str = Field(description="触发该发现的规则 ID 或模型标识")

    @model_validator(mode="after")
    def _severity_requires_evidence(self) -> "Finding":
        # 误报控制：确认级错误必须有来源或明确算式支撑
        if self.severity is Severity.CONFIRMED and not self.source_evidence:
            raise ValueError(
                f"CONFIRMED 级发现 {self.finding_id} 必须携带 source_evidence"
                "（否则视为误报风险，请降级为 SUSPECTED）"
            )
        return self


class AuditMetrics(BaseModel):
    """对黄金测试集的评测结果 —— 直接回答『误报控制』这个考察点。"""

    model_config = ConfigDict(extra="forbid")

    true_positive: int = 0
    false_positive: int = 0
    false_positive_on_trap: int = Field(
        default=0,
        description=(
            "假正例中**落在陷阱项上**的数量。陷阱项（is_real_error=false）是"
            "刻意设计成「看起来像错但实际正确」的条目 —— "
            "单独统计它，才能回答「误报里有多少是被设计好的诱饵钓到的」。"
            "注意它是 false_positive 的**子集**，不是另加的一项。"
        ),
    )
    false_negative: int = 0
    precision: float | None = None
    recall: float | None = None
    f1: float | None = None
    coverage: float | None = Field(default=None, description="错误类型覆盖率 E1-E5")
    drafts_evaluated: int = Field(
        default=0, description="参与评测的草稿数 —— 只有 1 份时指标不具代表性"
    )
    traps_evaluated: int = Field(
        default=0, description="参与评测的陷阱项数（is_real_error=false）"
    )
    evaluated_at: datetime = Field(default_factory=utcnow)


class AuditResult(BaseModel):
    """一次 R5 运行的完整产物 —— runs/<id>/audit/audit.json。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: str = CONTRACT_VERSION
    run_id: str
    draft_doc_id: str
    checked_at: datetime = Field(
        default_factory=utcnow,
        description="核查时间。复现时传入 run 的 started_at 以保证产物哈希一致。",
    )
    per_draft_metrics: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "多草稿时逐份的评测明细。**为什么需要**：只看汇总指标无法知道"
            "是哪一份草稿拉低了精确率 —— 而「哪一份、错在哪类」才是可行动的。"
        ),
    )

    findings: list[Finding] = Field(default_factory=list)
    metrics: AuditMetrics | None = None

    def by_severity(self, severity: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity is severity]

    def by_type(self, error_type: ErrorType) -> list[Finding]:
        return [f for f in self.findings if f.error_type is error_type]


# ==========================================================================
# Harness 运行时：追踪事件与运行清单
# ==========================================================================


class TraceEvent(BaseModel):
    """trace.jsonl 的一行。

    ``TraceKind`` 的四类**都会落盘**：文件访问 / 工具调用 / 模型调用 /
    计算与结果（外加阶段与错误）。``FILE_ACCESS`` 自 2026-10-17 起由
    ``RunContext.guard()`` 在每个读写点位逐条写入 ——
    见 ``agent/trace/tracer.py::Tracer.file_access`` 的 docstring。

    ★ 但**不要假定每一个 run 的四类都非空**：早于该日期的 run
    （本仓 ``runs/demo``、``runs/web-*``）里 ``file_access`` 确实是 0，
    那是历史产物的属性，不是契约。读 ``trace.jsonl`` 的一方按实际计数判断，
    别按契约假定。
    """

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=0)
    ts: datetime = Field(default_factory=utcnow)
    kind: TraceKind
    stage: str | None = None
    step: int | None = None
    role: str | None = Field(default=None, description="planner|coder|extractor|critic|vision")

    # LLM 调用
    model: str | None = None
    prompt_id: str | None = None
    prompt_version: str | None = None
    prompt_sha256: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_hit: bool | None = None

    # 工具调用
    tool: str | None = None
    args_hash: str | None = None

    # 文件访问（题目明文要求完整记录）
    path: str | None = None
    path_sha256: str | None = None
    mode: Literal["read", "write", "list"] | None = None

    # 结果生成
    payload_hash: str | None = None
    latency_ms: int | None = None
    cost_cny: float | None = None
    ok: bool = True
    error: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class RunManifest(BaseModel):
    """runs/<run_id>/manifest.json —— 环境指纹，可复现性的根。

    replay 时逐字段比对；任一不一致即判定为「不可复现」。
    """

    model_config = ConfigDict(extra="forbid")

    contract_version: str = CONTRACT_VERSION
    run_id: str
    pipeline: str
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None

    # 输入指纹
    input_files: dict[str, str] = Field(default_factory=dict, description="相对路径 -> sha256")
    data_dir: str = Field(
        default="data",
        description=(
            "本次运行使用的数据目录（相对于项目根目录）。"
            "**回放必须据此重建上下文** —— 否则 `agent replay` 会去读默认的 "
            "data/ 而不是原始输入，导致重放结果与原始产物哈希不一致，"
            "表现为『不可复现』这个假警报（实测踩过）。"
        ),
    )
    doc_limit: int | None = Field(
        default=None,
        description=(
            "`agent run --limit N` 的取值。**必须记录**：它决定了参与本次运行的"
            "文档数量。不记录时回放会处理全部文档，产物与原始不同，"
            "于是把一次**本可复现**的运行误判为『不可复现』（实测踩过："
            "`--limit 1` 试跑 → replay 报 ❌，而同样的命令不加 --limit 就 ✅）。"
            "这类假警报比不做校验更糟：它会训练人忽略复现报告。"
        ),
    )
    upstream_facts_run: str | None = Field(
        default=None,
        description=(
            "`agent run --facts-run <run_id|路径>` 的取值（R4 / R5 专用）。"
            "**必须记录**：链式运行时事实库来自另一个 run 目录，"
            "不记录的话回放会去本 run 目录找 facts.json，必然找不到"
            "（实测踩过：`run r4_valuation --facts-run smoke` 的 replay ❌）。"
        ),
    )

    # 环境指纹
    code_version: str = "0.1.0"
    python_version: str = ""
    agent_config_hash: str = ""
    prompts: dict[str, str] = Field(default_factory=dict, description="prompt_id@ver -> sha256")
    skills: dict[str, str] = Field(default_factory=dict)
    models: dict[str, str] = Field(default_factory=dict, description="role -> model_id")
    provider: str = Field(
        default="",
        description=(
            "本次运行使用的 provider 名（如 deepseek / mock）。"
            "**回放必须据此选择同一个 provider** —— 缓存键包含 provider 名，"
            "回放时换成别的 provider 会导致缓存全部未命中，"
            "被误判为『不可复现』（实测踩过）。"
        ),
    )
    model_params: dict[str, Any] = Field(default_factory=dict)
    seed: int = 0
    offline: bool = False

    # 输出指纹
    outputs: dict[str, str] = Field(default_factory=dict, description="相对路径 -> sha256")

    # 运行统计
    status: Literal["running", "success", "failed", "partial"] = "running"
    total_steps: int = 0
    total_tokens: int = 0
    total_cost_cny: float = 0.0
    cache_hit_rate: float | None = None
    error: str | None = None


# 解析前向引用
ConfidenceBreakdown.model_rebuild()


__all__ = [
    "CONTRACT_VERSION",
    "utcnow",
    "sha256_text",
    "derive_id",
    "DocumentType",
    "SourceLayer",
    "SOURCE_LAYER_BASE_CONFIDENCE",
    "PageRoute",
    "Verdict",
    "StatementScope",
    "ProfitBasis",
    "PeriodType",
    "Severity",
    "ErrorType",
    "AssertionStatus",
    "TraceKind",
    "DocumentManifest",
    "BBox",
    "Chunk",
    "EvidenceSpan",
    "ChannelReading",
    "ConfidenceBreakdown",
    "ConfidenceAdjustment",
    "Fact",
    "FactSet",
    "Assumption",
    "ForecastYear",
    "DCFResult",
    "CompsPeer",
    "CompsResult",
    "SensitivityCell",
    "SensitivityResult",
    "ValuationAssertion",
    "ValuationResult",
    "FindingLocation",
    "Finding",
    "AuditMetrics",
    "AuditResult",
    "TraceEvent",
    "RunManifest",
]
