"""证据层：文本块存储、事实账本、置信度计算。

这是本方案区别于通用 Agent Harness 的核心 —— **Evidence-First**。
通用 harness 只关心「工具调用成功与否」，我们额外关心
「这个数字出自哪一页哪一行，凭什么可信」。

两个组件：
    EvidenceStore  管理文档 -> 页 -> 块 -> 证据锚点，并提供 quote 回读
    FactLedger     管理事实入库，执行「无锚点不入库」硬闸门
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Iterable

from agent.evidence.models import (
    SOURCE_LAYER_BASE_CONFIDENCE,
    BBox,
    ChannelReading,
    Chunk,
    ConfidenceAdjustment,
    ConfidenceBreakdown,
    DocumentManifest,
    EvidenceSpan,
    Fact,
    FactSet,
    SourceLayer,
)
from agent.runtime.errors import EvidenceError, QuoteVerificationError
from agent.validate.quotecheck import QuoteMatch, check_quote

# 置信度调整项的固定分值 —— **写在这里而不是散落各处**，
# 保证报告、Web 面板、测试三处一致。
ADJ_CROSS_CHANNEL_AGREE = 0.05    # 多路独立同值
ADJ_VLM_CONFIRMED = 0.10          # 多模态裁判确认
ADJ_ARITHMETIC_OK = 0.05          # 算术校验通过
ADJ_UNIT_ANOMALY = -0.30          # 量级/单位异常
ADJ_LOGIC_CONFLICT = -0.20        # 与同文档其他字段冲突
ADJ_FUZZY_QUOTE = -0.05           # quote 仅模糊匹配
ADJ_NO_QUOTE = -1.00              # 回读失败 —— 直接归零

REVIEW_THRESHOLD = 0.60           # 低于此值必须标记 needs_review
MAX_CONFIDENCE = 0.99             # 不给满分：满分意味着「绝对确定」，不诚实


class EvidenceStore:
    """按文档组织文本块与证据锚点，并承担 quote 回读职责。"""

    def __init__(self) -> None:
        self._chunks: dict[str, Chunk] = {}
        self._spans: dict[str, EvidenceSpan] = {}
        self._by_doc: dict[str, list[str]] = defaultdict(list)   # doc_id -> chunk_ids
        self._page_text: dict[tuple[str, int], str] = {}          # 权威原文
        self._fallback_text: dict[tuple[str, int], list[str]] = defaultdict(list)
        self._extra_text: dict[tuple[str, int], list[str]] = defaultdict(list)
        self._page_blocks: dict[tuple[str, int], list[dict[str, object]]] = {}
        # 整份文档扫出的报表标题：doc_id → [{page, y0, scope, text}]
        self._statement_titles: dict[str, list[dict[str, object]]] = {}
        self._pages: dict[str, int] = {}                          # doc_id -> page_count

    # ---------------------------------------------------------------- 写入

    def add_chunk(self, chunk: Chunk) -> Chunk:
        if chunk.chunk_id in self._chunks:
            return self._chunks[chunk.chunk_id]
        self._chunks[chunk.chunk_id] = chunk
        self._by_doc[chunk.doc_id].append(chunk.chunk_id)
        # 未登记权威原文时，退化为用块文本拼接（并在 page_text 中标注为退化来源）
        if (chunk.doc_id, chunk.page) not in self._page_text:
            self._fallback_text[(chunk.doc_id, chunk.page)].append(chunk.text)
        return chunk

    def add_chunks(self, chunks: Iterable[Chunk]) -> int:
        return sum(1 for c in chunks if self.add_chunk(c) is not None)

    def register_document(self, manifest: DocumentManifest) -> None:
        self._pages[manifest.doc_id] = manifest.page_count

    def add_span(self, span: EvidenceSpan) -> EvidenceSpan:
        self._spans[span.span_id] = span
        return span

    def set_page_text(self, doc_id: str, page: int, text: str) -> None:
        """登记页面的**权威原文**（取自 ``page.get_text("text")``）。

        quote 回读必须以权威原文为准，而不是把文本块拼起来 ——
        拼出来的文本与原文可能有换行/顺序差异，会让逐字子串校验失败。
        """
        self._page_text[(doc_id, page)] = text

    # ---------------------------------------------------------------- 读取

    def chunk(self, chunk_id: str) -> Chunk:
        if chunk_id not in self._chunks:
            raise EvidenceError("chunk 不存在", chunk_id=chunk_id)
        return self._chunks[chunk_id]

    def span(self, span_id: str) -> EvidenceSpan:
        if span_id not in self._spans:
            raise EvidenceError("证据锚点不存在", span_id=span_id)
        return self._spans[span_id]

    def spans_of(self, fact: Fact) -> list[EvidenceSpan]:
        return [self.span(s) for s in fact.evidence_span_ids if s in self._spans]

    def page_text(self, doc_id: str, page: int) -> str:
        """该页的**全部**可用文本。

        优先返回 ``set_page_text`` 登记的权威原文；未登记时退化为
        用文本块拼接（并补上 OCR 通道的文本）。取并集而非单一来源，
        是为了提高 quote 回读的召回率 —— VLM 读到的可能是 OCR 通道的文本。
        """
        parts: list[str] = []
        authoritative = self._page_text.get((doc_id, page))
        if authoritative:
            parts.append(authoritative)
        parts.extend(self._fallback_text.get((doc_id, page), []))
        for extra in self._extra_text.get((doc_id, page), []):
            if extra and extra not in parts:
                parts.append(extra)
        # 去重保序
        seen: set[str] = set()
        unique: list[str] = []
        for p in parts:
            if p and p not in seen:
                seen.add(p)
                unique.append(p)
        return "\n".join(unique)

    def page_blocks(self, doc_id: str, page: int) -> list[dict[str, object]]:
        """页面的**文本块**（含纵坐标）。缺记录时返回空表。

        用途：报表口径（合并 / 母公司）判定的唯一依据是**报表标题**，
        而标题在正文层、不在表格网格里。要靠纵坐标把标题与网格行对齐，
        因此必须保留块级坐标。

        返回空表时调用方退化为"整表按上一页口径处理" —— 有意的兜底：
        判不出口径时**不猜**，而是沿用上下文。
        """
        return list(self._page_blocks.get((doc_id, page), []))

    def set_page_blocks(
        self, doc_id: str, page: int, blocks: list[dict[str, object]]
    ) -> None:
        self._page_blocks[(doc_id, page)] = list(blocks)

    # ⚠️ 十六轮试过、**被实测否决**的一条补充判据（留档以免重犯）
    # ---------------------------------------------------------------------------
    # 观察：:meth:`scope_by_overlap` 的比较键是**纵坐标**，而年报里大量续表是
    # **整页高**的，标题必然落在表格矩形**内部** —— 实测 000722：
    #
    #     p73 标题「1、合并资产负债表」 y=74.2
    #     p74 表格（续表）              y=[72.1, 754.6]        ← 整页高
    #     p73 标题 → gap=-12.5 → 惩罚分支 → score 830.4
    #     p75 标题 → gap=-386.1 → 惩罚分支 → score 456.8      ← 反而赢
    #
    # 于是 p74（合并表的续表，行标签 ``使用权资产``/``无形资产`` 接 p73 的
    # ``流动资产：``）被配给了 p75 的**母公司**标题。看起来是真 bug。
    #
    # 试的修法：整页高的表改用"文档顺序里最近的前置标题"。
    #
    # **实测否决**（annual38 vs annual39）：
    #
    #     needs_review      179 → 177（只少 2 条）
    #     000722 的 2024A   从"有自洽三元组" → **完全没有数据**
    #
    # 净效果为负：口径判对了，但那一期的**期间**又错了（上期列落到别处），
    # 于是原本能凑出的三元组也消失了。**这不是一个可以单独修的问题** ——
    # 口径与期间两个映射在续表上互相纠缠，改一个会把另一个带偏。
    #
    # 结论：保持 `scope_by_overlap` 原样，把 p74 归到母公司口径这件事
    # 记入已知限制（README 第 23 条），不在下游加补充判据。

    def scope_by_overlap(
        self, doc_id: str, page: int, table_bbox: tuple[float, float, float, float]
    ) -> str:
        """按**矩形交叠**把一张表配给它所属报表的标题；配不上返回空串。

        **为什么必须交叠而不是按坐标先后**（三次实测否决后确定的判据）：

        中文年报里表格的**视觉顺序**与报表的**印刷顺序**不一致。实测 000722::

            p77 y=72.1   的表  ← 视觉上在 p77 标题（y=492.3）之前，
                                 但它属于 p73 开始的**合并资产负债表**
            p77 y=533.2  的表  ← 在标题之后，属于**合并利润表**

        按"上一页继承 / 最近前置标题 / (page,y0) 先后"三种判据都会把
        第一张表配到错误的报表上。只有**空间关系**能区分：
        标题文本框与表格矩形**在纵向上相邻且横向重叠**的那个，才是它的标题。

        打分（越小越像）：
          * 表格在标题**下方**且纵向间距小 → 首选（同一页的标题+表）
          * 表格在标题**上方**、标题是它紧邻的下一个 → 次选
            （跨页继续：表格的尾段在上，报表的下一个标题在下）
          * 纵向重叠很大 → 惩罚（标题被压进表里通常是误判）
          * 横向完全不重叠 → 整条候选作废（标题属于另一栏）
        """
        titles = self._statement_titles.get(doc_id, [])
        if not titles:
            return ""
        tx0, ty0, tx1, ty1 = table_bbox
        best_scope, best_score = "", None
        for t in titles:
            t_page = int(t.get("page", 0))
            bb = t.get("bbox")
            if not bb:
                continue
            # 只考虑同页或相邻页的标题（更远的标题不可能是本表的表头）
            if abs(t_page - page) > 1:
                continue
            bx0, by0, bx1, by1 = (float(v) for v in bb)
            # 横向必须与表格有重叠：否则标题属于另一栏
            if bx1 <= tx0 or bx0 >= tx1:
                continue
            # 纵向关系：标题底边到表格顶边的距离（正=表在标题下方）
            gap = ty0 - by1
            if gap >= -1.0:
                score = abs(gap) + (0.0 if t_page == page else 50.0) + (by0 - ty0) / 1000.0
            else:
                # 表在标题上方：用"标题顶边到表格底边"的距离，并加重惩罚
                score = abs(ty1 - by0) + 100.0 + (0.0 if t_page == page else 50.0)
            if best_score is None or score < best_score:
                best_scope, best_score = str(t.get("scope", "")), score
        return best_scope

    def preceding_statement_scope(self, doc_id: str, page: int, y0: float) -> str:  # noqa: ARG002
        """**已废弃**：按坐标先后的口径判据。保留仅为兼容，一律返回空串。

        口径请改用 :meth:`scope_by_overlap`（矩形交叠）。
        三次实测否决的完整记录见 ``docs/VALIDATION_ROUND2_RESULT.md`` §7.2。
        """
        return ""

    def set_statement_titles(self, doc_id: str, titles: list[dict[str, object]]) -> None:
        """登记整份文档扫出的报表标题（含页码、纵坐标、口径）。"""
        self._statement_titles[doc_id] = list(titles)

    def statement_titles(self, doc_id: str) -> list[dict[str, object]]:
        return list(self._statement_titles.get(doc_id, []))

    def add_aux_text(self, doc_id: str, page: int, text: str) -> None:
        """补入辅助文本源（如 OCR 结果），仅用于提高 quote 回读召回率。

        注意：辅助文本**不覆盖**权威原文 —— 否则幻觉的 OCR 文本会
        让编造的 quote 通过校验，闸门就失效了。
        """
        if text:
            self._extra_text[(doc_id, page)].append(text)

    def chunks_of_doc(self, doc_id: str) -> list[Chunk]:
        return sorted(
            (self._chunks[cid] for cid in self._by_doc.get(doc_id, [])),
            key=lambda c: (c.page, c.reading_order),
        )

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    @property
    def span_count(self) -> int:
        return len(self._spans)

    def all_spans(self) -> list[EvidenceSpan]:
        return list(self._spans.values())

    # ---------------------------------------------------------------- 回读

    def verify_and_anchor(
        self,
        *,
        doc_id: str,
        page: int,
        quote: str,
        source_layer: SourceLayer,
        bbox: BBox | None = None,
        chunk_id: str | None = None,
        fuzzy_threshold: float = 0.86,
    ) -> tuple[EvidenceSpan, QuoteMatch, float]:
        """校验 quote 并生成证据锚点。

        **这是幻觉硬闸门**：回读失败抛 ``QuoteVerificationError``，
        调用方必须拒收该字段（或显式降级为 needs_review）。
        """
        page_text = self.page_text(doc_id, page)
        result = check_quote(quote, page_text, fuzzy_threshold=fuzzy_threshold)

        if not result.passed:
            raise QuoteVerificationError(
                "quote 回读失败，判定为幻觉或定位错误",
                doc_id=doc_id,
                page=page,
                quote=quote[:120],
                best_similarity=result.similarity,
            )

        # 若调用方未给 chunk_id，尝试定位到包含该 quote 的块
        if chunk_id is None:
            chunk_id = self._locate_chunk(doc_id, page, quote)

        span = EvidenceSpan(
            span_id=EvidenceSpan.make_id(doc_id, chunk_id, quote),
            doc_id=doc_id,
            chunk_id=chunk_id,
            page=page,
            bbox=bbox,
            quote=quote,
            source_layer=source_layer,
        )
        self.add_span(span)
        return span, result.status, result.similarity

    def _locate_chunk(self, doc_id: str, page: int, quote: str) -> str:
        """找出包含该 quote 的 chunk；找不到则回退到该页第一个块。"""
        from agent.validate.quotecheck import normalize_text

        nq = normalize_text(quote)
        candidates = [c for c in self.chunks_of_doc(doc_id) if c.page == page]
        for c in candidates:
            if nq and nq in normalize_text(c.text):
                return c.chunk_id
        if candidates:
            return candidates[0].chunk_id
        # 该页没有任何块（异常情况）—— 生成一个占位 chunk 以保证锚点可用
        placeholder = Chunk(
            chunk_id=Chunk.make_id(doc_id, page, quote),
            doc_id=doc_id,
            page=page,
            bbox=BBox(x0=0, y0=0, x1=0, y1=0),
            text=quote,
            source_layer=SourceLayer.L3_VLM,
            block_type="figure",
        )
        self.add_chunk(placeholder)
        return placeholder.chunk_id

    # ---------------------------------------------------------------- 导出

    def export_spans(self) -> list[EvidenceSpan]:
        return list(self._spans.values())


# ==========================================================================
# 置信度计算
# ==========================================================================


def build_confidence(
    *,
    source_layer: SourceLayer,
    quote_match: QuoteMatch | None = None,
    cross_channel_agree: bool = False,
    vlm_confirmed: bool = False,
    arithmetic_ok: bool = False,
    unit_anomaly: bool = False,
    logic_conflict: bool = False,
    extra: list[ConfidenceAdjustment] | None = None,
) -> ConfidenceBreakdown:
    """构造可复算的置信度明细。

    公式（与方案 2.6 节一致）：
        基础分（由来源层决定）
        + 多路独立同值        +0.05
        + VLM 裁判确认        +0.10
        + 算术校验通过        +0.05
        + quote 仅模糊匹配    −0.05
        + 单位/量级异常       −0.30
        + 与同文档逻辑冲突    −0.20
        + quote 回读失败      −1.00（等价于归零）
        上限 0.99
    """
    base = SOURCE_LAYER_BASE_CONFIDENCE.get(source_layer, 0.5)
    adjs: list[ConfidenceAdjustment] = []

    if cross_channel_agree:
        adjs.append(ConfidenceAdjustment(reason="多路独立同值", delta=ADJ_CROSS_CHANNEL_AGREE))
    if vlm_confirmed:
        adjs.append(ConfidenceAdjustment(reason="VLM 裁判确认", delta=ADJ_VLM_CONFIRMED))
    if arithmetic_ok:
        adjs.append(ConfidenceAdjustment(reason="算术校验通过", delta=ADJ_ARITHMETIC_OK))
    if quote_match is QuoteMatch.FUZZY:
        adjs.append(ConfidenceAdjustment(reason="quote 仅模糊匹配(OCR噪声)", delta=ADJ_FUZZY_QUOTE))
    if quote_match is QuoteMatch.MISS:
        adjs.append(ConfidenceAdjustment(reason="quote 回读失败", delta=ADJ_NO_QUOTE))
    if unit_anomaly:
        adjs.append(ConfidenceAdjustment(reason="单位/量级异常", delta=ADJ_UNIT_ANOMALY))
    if logic_conflict:
        adjs.append(ConfidenceAdjustment(reason="与同文档其他字段冲突", delta=ADJ_LOGIC_CONFLICT))
    if extra:
        adjs.extend(extra)

    bd = ConfidenceBreakdown(base=base, adjustments=adjs)
    # 强制上限
    if bd.score > MAX_CONFIDENCE:
        bd.adjustments.append(
            ConfidenceAdjustment(reason="置信度上限封顶", delta=round(MAX_CONFIDENCE - bd.score, 4))
        )
    return bd


# ==========================================================================
# 事实账本
# ==========================================================================


class FactLedger:
    """事实入库与查询 —— 「无锚点不入库」的执行者。"""

    def __init__(self, store: EvidenceStore, *, run_id: str = "unknown") -> None:
        self.store = store
        self.run_id = run_id
        self._facts: list[Fact] = []
        self._by_id: dict[str, Fact] = {}
        self._rejected: list[dict[str, object]] = []
        self._conflicts: list[dict[str, object]] = []

    # ---------------------------------------------------------------- 写入

    def add(self, fact: Fact) -> Fact:
        """入库。冲突时保留**置信度高**的那条，另一条转入 rejected 供审计。"""
        existing = self._by_id.get(fact.fact_id)
        if existing is not None:
            if fact.confidence > existing.confidence:
                self._rejected.append(
                    {"reason": "被更高置信度的同 id 事实替代", "fact_id": existing.fact_id}
                )
                self._replace(fact)
                return fact
            self._rejected.append(
                {"reason": "置信度不高于已有事实", "fact_id": fact.fact_id}
            )
            return existing

        self._facts.append(fact)
        self._by_id[fact.fact_id] = fact
        self._check_internal_conflict(fact)
        return fact

    def _fact_page(self, fact: Fact) -> int | None:
        """事实所在页码（取第一个证据锚点的页）。取不到返回 None。"""
        span = self._first_span(fact)
        if span is not None and span.page:
            return int(span.page)
        return None

    def _check_internal_conflict(self, fact: Fact) -> None:
        """检测**同一张表内、同一主体、同一指标、同一期间**出现显著不同的值。

        为什么必须有这个守卫：实测踩过的坑 ——
        一份公告的同一页上同时出现「质押股份数量为2,557 万股」与
        「质押股份数量为2.557万股」，抽取出 25,570,000 与 25,570 两个值
        （相差 1000 倍）。两条都通过了 quote 回读校验（原文确实如此），
        因此**没有任何闸门能挡住它们**，最终两条都进了事实库。

        ★ **主体维度不可省略**（第二次踩坑后补上）：
        一份公告里天然存在多个主体 —— 控股股东、一致行动人、董监高、
        以及「合计」行。它们各自的质押股数、持股数本来就不同，
        这不是冲突，而是**多个主体的正常读数**。
        早期实现只按 ``(doc_id, metric, period)`` 比对，
        结果把不同股东的持股数两两配对，一页就产出 27 条假冲突告警。

        ★ **表级作用域不可省略**（第三次踩坑后补上，2026-09 年报验证）：
        年报里同一份文件同时记载了合并数、母公司数、几十家子公司明细、
        分部数据，而它们**大都被赋予同一个主体名**（报表主体取文档主体，
        因为财务报表本身没有主体列）。于是旧判据把「子公司 A 的总资产 8.9 亿」
        与「集团合并总资产 2,828 亿」当成"同一主体同一期间的冲突"，
        实测产出 **562 条 needs_review（占全库 63%）**。

        噪声的代价不只是"看着乱"：R5 会把这些事实排除在比对之外，
        于是参考值大量流失，**假阴性随之上升**。守卫把有用信号一起埋掉了。

        现在的判据：``(doc_id, entity, metric, period)`` **+ 同一张表**。
        "同一张表"用「同页 + bbox 纵向区间重叠」判定 —— 中文财报一页常有多张表，
        只看页码会把同页的两张表混起来。缺页码/缺 bbox 时退化为不比较
        （宁可漏报，不可制造噪声）。
        """
        # 期间未知时仍然比较 —— 公告语料里"同一页两处写法不同"正是靠这条发现的
        # （实测：同一页出现「2,557 万股」与「2.557万股」，两条都过了 quote 校验）。
        # 真正的噪声源是"跨表比较"，已由 _same_table 解决，不靠屏蔽期间来降噪。
        page = self._fact_page(fact)

        same = []
        for f in self._facts:
            if (
                f.doc_id != fact.doc_id
                or f.entity != fact.entity          # ← 主体必须相同
                or f.metric != fact.metric
                or f.period != fact.period
                # ★ 口径必须相同：同一页可能上下并排「合并资产负债表」与
                #   「母公司资产负债表」，科目名完全相同而数值本就不同。
                #   不比较口径时，实测一份年报产出 562 条 needs_review（占全库 63%），
                #   而 R5 会把这些事实排除在比对之外 → 参考值流失 → 假阴性上升。
                or f.statement_scope != fact.statement_scope
                or f.fact_id == fact.fact_id
            ):
                continue
            if not self._comparable_table(fact, f, page):
                continue
            same.append(f)
        for other in same:
            if other.value == 0 or fact.value == 0:
                continue
            ratio = max(abs(other.value), abs(fact.value)) / min(
                abs(other.value), abs(fact.value)
            )
            # 相差 10 倍以上即视为需要人工判定的实质性差异。
            #
            # 注意措辞：同一张表内同一主体同一指标出现多个不同值，
            # **有两种可能**，程序无法自行判断是哪一种：
            #   (a) 抽取错误（如 2,557万股 与 2.557万股 相差 1000 倍）
            #   (b) 同一张表确实记载了多次事件（如分次股权变动各有一个比例）
            # 因此结论是「需人工判定」，而不是「数据错误」。
            # 关键是把两条都标记出来并保留证据，让人**能**判断 ——
            # 绝不能静默挑一个用。
            if ratio >= 10:
                note = (
                    f"同一张表内主体（{fact.entity}）的 {fact.metric} 出现"
                    f"差异 {ratio:.0f} 倍的值："
                    f"{other.value} {other.unit}（{other.evidence_span_ids[:1]}）vs "
                    f"{fact.value} {fact.unit}（{fact.evidence_span_ids[:1]}）。"
                    "可能是抽取错误，也可能是同一张表记载了多次事件 —— 需人工判定"
                )
                for target in (other, fact):
                    target.needs_review = True
                    target.review_reason = f"{note}。两条都通过了 quote 校验，需人工判定"
                self._conflicts.append(
                    {
                        "doc_id": fact.doc_id,
                        "page": page,
                        "entity": fact.entity,
                        "metric": fact.metric,
                        "period": fact.period,
                        "ratio": round(float(ratio), 2),
                        "values": [str(other.value), str(fact.value)],
                        "fact_ids": [other.fact_id, fact.fact_id],
                    }
                )

    # 量纲合理性闸门 ------------------------------------------------------
    #
    # 与上面的"同表冲突"守卫互补：那个守卫只在**同一张表内**比对，
    # 因为跨表量级差异在年报里太常见（合并数 vs 子公司数 vs 分部数）。
    # 但有一类跨表错误它必然漏掉，而且**危害极大** ——
    # 同一指标在不同表里被赋予不同的单位，量级整体差 100 / 1000 倍。
    #
    # 实测事故（2026-09，直接毁掉 R4 估值）：
    #   601857 中国石油 2025 年报把"期末现金及现金等价物余额"印在至少 4 处，
    #   口径都是**人民币百万元**（p95 行标签明写、p28/p29 表头明写）：
    #       实际值 = 206,162 百万元 = 206,162,000,000 元
    #   但另一处表格因表头缺单位判据，落到页级声明"单位：元"，
    #   抽出 206,162 元 × 10000 = 20,616,200,000,000 元（错 100 倍）。
    #   两条都通过 quote 回读（`206,162` 确实印在原文里），
    #   R4 取净债 = 有息负债 − 现金 → −20.4 万亿，DCF 每股价值 ¥124.41。
    #
    # 判据用**资产负债表恒等式**，而不是"量级离群"：
    # 恒等式有会计含义，离群没有 —— 前者被质疑时能解释，后者不能。
    # 一家公司的货币资金不可能显著超过它的资产总额（钱也是资产），
    # 也不可能显著超过它的年度营业收入。
    #
    # 阈值刻意放到 1.5 / 3.0 这种"宽到不会误伤"的水平：
    # 本闸门要抓的是 100 倍级的量纲错误，锚点自身再偏 2–3 倍也不影响判断。
    _ASSET_BOUNDED_METRICS = ("cash_and_equivalents", "cash_at_end")
    _ASSET_BOUND_MAX = Decimal("1.5")     # 货币资金 / 资产总额
    _REVENUE_BOUND_MAX = Decimal("3.0")   # 货币资金 / 营业收入

    def check_scale_plausibility(self) -> list[dict[str, object]]:
        """按资产负债表恒等式校验**货币资金类**指标的量纲合理性。

        返回被标记的条目清单（同时已写入 ``needs_review`` / ``review_reason``）。
        锚点取同一 ``(doc_id, entity, period)`` 下 ``total_assets`` /
        ``revenue`` 的**最大值** —— 取最大值是刻意的：只有当事实连最宽松的
        锚点都撑不住时才算越界。公司自己报表里的"资产总计"若也抽错了，
        本闸门会失效，但它只会让判断从"越界"退化成"不判断"，不会反向误伤。
        """
        anchors: dict[tuple[str, str, str], dict[str, Decimal]] = defaultdict(dict)
        for f in self._facts:
            if f.metric in ("total_assets", "revenue"):
                key = (f.doc_id, str(f.entity), str(f.period))
                cur = anchors[key].get(f.metric)
                if cur is None or abs(f.value) > abs(cur):
                    anchors[key][f.metric] = f.value

        flagged: list[dict[str, object]] = []
        for f in self._facts:
            if f.metric not in self._ASSET_BOUNDED_METRICS:
                continue
            if f.needs_review:      # 已因别的理由被标记，不重复标注
                continue
            held = anchors.get((f.doc_id, str(f.entity), str(f.period)))
            if not held:
                continue
            for anchor_metric, limit in (
                ("total_assets", self._ASSET_BOUND_MAX),
                ("revenue", self._REVENUE_BOUND_MAX),
            ):
                base = held.get(anchor_metric)
                if base is None or base == 0:
                    continue
                if abs(f.value) <= abs(base) * limit:
                    continue
                ratio = abs(f.value) / abs(base)
                anchor_id = self._anchor_fact_id(
                    f.doc_id, str(f.entity), str(f.period), anchor_metric
                )
                note = (
                    f"量纲不合理：{f.metric} = {f.value} {f.unit}（{f.evidence_span_ids[:1]}）"
                    f"达到同期 {anchor_metric} = {base} 的 {ratio:.1f} 倍，"
                    f"超过恒等式上限 {limit} 倍。货币资金本身就是资产的一部分，"
                    f"不可能显著超过资产总额或年度营业收入 —— "
                    f"最可能的原因是同一指标在不同表里被赋予了不同的单位。"
                    f"锚点事实：{anchor_id or '（未定位）'}"
                )
                f.needs_review = True
                f.review_reason = note
                self._conflicts.append(
                    {
                        "kind": "scale_implausible",
                        "doc_id": f.doc_id,
                        "entity": str(f.entity),
                        "metric": f.metric,
                        "period": str(f.period),
                        "ratio": round(float(ratio), 2),
                        "values": [str(f.value), str(base)],
                        "fact_ids": [f.fact_id, anchor_id],
                    }
                )
                flagged.append(self._conflicts[-1])
                break   # 一个指标只标一次，避免同一事实写两条理由
        return flagged

    def _fact_row_label(self, fact: Fact) -> str:
        """从通道读数里取行标签（``note`` 形如 ``行：资本性支出``）。"""
        for c in fact.channel_readings or []:
            note = (c.note or "").strip()
            if note.startswith("行："):
                return note[2:].strip()
        return ""

    def check_period_contradiction(self) -> list[dict[str, object]]:
        """同一文档内，**同一个值被标成两个不同期间** → 至少有一个期间标签是错的。

        为什么必须有这条闸门（2026-09 第十三轮实测）
        --------------------------------------------
        601857 **2025 年报** p7「主要财务数据」的五年摘要表里，
        ``资本性支出`` 的 2024 年真值 ``275,849`` 被标成了 ``2025A``：

            doc_840b15aa4dc3（2025 年报） 275,849 @ 2025A   ← 错，这是 2024 年的数
            doc_15a2de01653c（2024 年报） 275,849 @ 2024A   ← 对

        而 2025 年真值是 ``269,089``。因为 ``275,849 > 269,089``，
        R4 的"同期间取最大值"**必然取到 2024 年的数**并当作 2025 年的资本开支 ——
        数字确实印在原文里、quote 回读也能过、置信度 0.95、**全程不报错**。

        原有的同表冲突守卫抓不到它：那条守卫要求"同主体 + 同指标 + **同期间**
        + 同表"，而这里的问题恰恰是**期间不一致**。

        判据：``(doc_id, entity, metric, 值)`` 相同而 ``period`` 不同。
        一个数不可能同时是两年的读数，因此**两条都不可信**，一并标记。

        为什么是"都标"而不是"挑一条留"：程序无法判断哪一条的期间是对的 ——
        实测里 2025A 那条错、2024A 那条对，但也完全可能是反的。
        挑一条留就是猜；两条都标、把矛盾摊开给人看，才是诚实的做法。
        """
        by_value: dict[tuple[str, str, str, str], list[Fact]] = defaultdict(list)
        for f in self._facts:
            key = (f.doc_id, str(f.entity), f.metric, str(f.value))
            by_value[key].append(f)

        flagged: list[dict[str, object]] = []
        for (doc_id, entity, metric, value), group in by_value.items():
            periods = {str(g.period) for g in group}
            if len(periods) < 2:
                continue
            note = (
                f"期间自相矛盾：同一份文档里 {entity} 的 {metric} = {value} "
                f"同时被标成 {sorted(periods)} —— 一个数不可能同时是两期的读数，"
                f"至少有一个期间标签是错的。**两条都不可信**，需人工核对原文。"
                f"常见成因：多年摘要表的列头年份与相邻表复用，期间归属整体错位。"
            )
            for g in group:
                g.needs_review = True
                g.review_reason = note
            self._conflicts.append(
                {
                    "kind": "period_contradiction",
                    "doc_id": doc_id,
                    "entity": entity,
                    "metric": metric,
                    "periods": sorted(periods),
                    "values": [value],
                    "fact_ids": [g.fact_id for g in group],
                }
            )
            flagged.append(self._conflicts[-1])
        return flagged

    def _anchor_fact_id(
        self, doc_id: str, entity: str, period: str, metric: str
    ) -> str | None:
        """找出充当量纲锚点的那条事实（用于让人能顺着 review_reason 去核对）。"""
        best: Fact | None = None
        for f in self._facts:
            if (
                f.doc_id == doc_id
                and str(f.entity) == entity
                and str(f.period) == period
                and f.metric == metric
            ):
                if best is None or abs(f.value) > abs(best.value):
                    best = f
        return best.fact_id if best else None

    def _comparable_table(self, a: Fact, b: Fact, page_a: int | None) -> bool:
        """两条事实是否**可以**做冲突比较（表级作用域）。

        三态，刻意不对称：

        * **两者都能定位到表** → 必须同页且同表，否则不可比
          （年报里同一主体的合并数/子公司数/分部数分布在不同表，量级天然不同）
        * **两者都定位不到表** → 视为可比，沿用旧行为
          （单测与部分工具路径构造的事实没有证据锚点；若这里返回 False，
           会把"同页两种写法"这类真实冲突一并屏蔽掉）
        * **只有一个能定位** → 不可比（无法判断是否同表，宁可漏报不制造噪声）
        """
        sa = self._first_span(a)
        sb = self._first_span(b)
        if sa is None or sb is None or sa.bbox is None or sb.bbox is None:
            return sa is None and sb is None
        if page_a is not None and self._fact_page(b) not in (None, page_a):
            return False
        return self._same_table(a, b)

    def _same_table(self, a: Fact, b: Fact) -> bool:
        """两条事实是否来自同一张表：同页 且 bbox 纵向区间有重叠或相近。

        中文财报一页常有多张表（如「资产负债表尾部」+「利润表」），
        只看页码会把它们混起来。缺 bbox 时不比较（宁可漏报）。
        """
        sa = self._first_span(a)
        sb = self._first_span(b)
        if sa is None or sb is None or sa.bbox is None or sb.bbox is None:
            return False
        if sa.page != sb.page:
            return False
        # 同一张表内的行，其 bbox 纵向区间会落在同一个表的范围内。
        # 用「区间重叠 + 纵向距离小于约 3 行高度」判定，避免把
        # 上下相邻的两张小表也算成一张。
        top = max(sa.bbox.y0, sb.bbox.y0)
        bottom = min(sa.bbox.y1, sb.bbox.y1)
        if bottom >= top:
            return True
        gap = top - bottom
        height = max(sa.bbox.y1 - sa.bbox.y0, sb.bbox.y1 - sb.bbox.y0, 1.0)
        return gap <= 3.0 * height

    def _first_span(self, fact: Fact) -> EvidenceSpan | None:
        """事实的第一个可用证据锚点。用公开的 ``store.span()`` 取，
        不去摸 store 的私有字典 —— 跨对象访问私有成员会让"谁能改谁"变得不可控。"""
        for sid in fact.evidence_span_ids:
            try:
                return self.store.span(sid)
            except Exception:  # noqa: BLE001 - 锚点缺失时继续找下一个
                continue
        return None

    @property
    def conflicts(self) -> list[dict[str, object]]:
        """检测到的同文档指标冲突清单（供审计与人工复核）。"""
        return list(self._conflicts)

    def _replace(self, fact: Fact) -> None:
        for i, f in enumerate(self._facts):
            if f.fact_id == fact.fact_id:
                self._facts[i] = fact
                break
        self._by_id[fact.fact_id] = fact

    def reject(self, reason: str, **detail: object) -> None:
        """记录一条被拒收的候选 —— 拒收也要留痕，否则无法解释覆盖率缺口。"""
        self._rejected.append({"reason": reason, **detail})

    # ---------------------------------------------------------------- 查询

    def all(self) -> list[Fact]:
        return list(self._facts)

    def get(self, fact_id: str) -> Fact | None:
        return self._by_id.get(fact_id)

    def by_metric(self, metric: str) -> list[Fact]:
        return [f for f in self._facts if f.metric == metric]

    def find(
        self,
        *,
        metric: str | None = None,
        entity: str | None = None,
        period: str | None = None,
        unit: str | None = None,
    ) -> list[Fact]:
        out = self._facts
        if metric is not None:
            out = [f for f in out if f.metric == metric]
        if entity is not None:
            out = [f for f in out if f.entity == entity]
        if period is not None:
            out = [f for f in out if f.period == period]
        if unit is not None:
            out = [f for f in out if f.unit == unit]
        return out

    def best(
        self,
        *,
        metric: str,
        entity: str | None = None,
        period: str | None = None,
    ) -> Fact | None:
        """取置信度最高的一条。R5 比对时用它作为基准。"""
        candidates = self.find(metric=metric, entity=entity, period=period)
        candidates = [c for c in candidates if not c.needs_review]
        if not candidates:
            return None
        return max(candidates, key=lambda f: f.confidence)

    def value_of(
        self,
        *,
        metric: str,
        entity: str | None = None,
        period: str | None = None,
    ) -> Decimal | None:
        f = self.best(metric=metric, entity=entity, period=period)
        return None if f is None else f.value

    # ---------------------------------------------------------------- 统计

    @property
    def count(self) -> int:
        return len(self._facts)

    @property
    def needs_review(self) -> list[Fact]:
        return [f for f in self._facts if f.needs_review]

    @property
    def rejected(self) -> list[dict[str, object]]:
        return list(self._rejected)

    def provenance(self, fact_id: str) -> dict[str, object]:
        """一路回溯到原文 —— Web 溯源面板的数据来源。"""
        fact = self.get(fact_id)
        if fact is None:
            raise EvidenceError("事实不存在", fact_id=fact_id)
        spans = self.store.spans_of(fact)
        return {
            "fact_id": fact.fact_id,
            "entity": fact.entity,
            "metric": fact.metric,
            "value": str(fact.value),
            "unit": fact.unit,
            "raw_text": fact.raw_text,
            "period": fact.period,
            "caliber": {
                "statement_scope": fact.statement_scope.value,
                "profit_basis": fact.profit_basis.value,
                "period_type": fact.period_type.value,
            },
            "confidence": fact.confidence,
            "confidence_explain": fact.confidence_breakdown.explain(),
            "source_layer": fact.source_layer.value,
            "channel_readings": [c.model_dump(mode="json") for c in fact.channel_readings],
            "evidence": [
                {
                    "span_id": s.span_id,
                    "doc_id": s.doc_id,
                    "page": s.page,
                    "quote": s.quote,
                    "bbox": s.bbox.model_dump() if s.bbox else None,
                    "source_layer": s.source_layer.value,
                }
                for s in spans
            ],
            "extractor": fact.extractor,
            "needs_review": fact.needs_review,
            "review_reason": fact.review_reason,
        }

    # ---------------------------------------------------------------- 导出

    def to_fact_set(
        self,
        *,
        documents: list[DocumentManifest] | None = None,
        built_at: "datetime | None" = None,
    ) -> FactSet:
        """导出为 FactSet。

        ``built_at`` 应由调用方传入 run 的 started_at，以保证同输入两次运行
        的产物**逐字节一致** —— 这是「可复现」能被机器验证的前提。
        """
        kwargs: dict[str, object] = {
            "run_id": self.run_id,
            "documents": documents or [],
            "facts": self.all(),
            "evidence": self.store.export_spans(),
            "needs_review_count": len(self.needs_review),
        }
        if built_at is not None:
            kwargs["built_at"] = built_at
        return FactSet(**kwargs)  # type: ignore[arg-type]

    def to_records(self) -> list[dict[str, object]]:
        """扁平化为表格记录，供 Excel / Web 表格展示。"""
        rows: list[dict[str, object]] = []
        for f in self._facts:
            spans = self.store.spans_of(f)
            rows.append(
                {
                    "fact_id": f.fact_id,
                    "entity": f.entity,
                    "metric": f.metric,
                    "value": str(f.value),
                    "unit": f.unit,
                    "raw_text": f.raw_text,
                    "period": f.period,
                    "period_type": f.period_type.value,
                    "statement_scope": f.statement_scope.value,
                    "profit_basis": f.profit_basis.value,
                    "source_layer": f.source_layer.value,
                    "confidence": f.confidence,
                    "needs_review": f.needs_review,
                    "doc_id": spans[0].doc_id if spans else "",
                    "page": spans[0].page if spans else "",
                    "quote": spans[0].quote if spans else "",
                }
            )
        return rows


__all__ = [
    "EvidenceStore",
    "FactLedger",
    "build_confidence",
    "REVIEW_THRESHOLD",
    "MAX_CONFIDENCE",
    "ADJ_CROSS_CHANNEL_AGREE",
    "ADJ_VLM_CONFIRMED",
    "ADJ_ARITHMETIC_OK",
    "ADJ_UNIT_ANOMALY",
    "ADJ_LOGIC_CONFLICT",
    "ADJ_FUZZY_QUOTE",
    "ADJ_NO_QUOTE",
]
