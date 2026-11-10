"""阶段 5：构建事实库（无锚点不入库）。

抽取策略分两路，**确定性优先**：

    5.1 规则抽取（优先）：用 ``configs/metrics.yaml`` 的别名表在页面文本中
        定位「指标名 + 数值 + 单位」。命中即为高置信事实。
    5.2 视觉裁决补录：把阶段 4 通过 quote 回读闸门的 VLM 结论转成事实，
        来源层标 ``L3_VLM``，基础置信度较低，且强制经过锚点校验。

三条硬闸门（任一不过就不入库，只记 rejected）：
    * 单位缺失且无法从上下文补全  -> 拒收（不允许猜单位）
    * quote 回读失败              -> 拒收（幻觉）
    * 置信度 < 0.60               -> 入库但强制 needs_review

关于「宁缺勿错」：拒收会降低覆盖率，但错误的事实会在 R5 阶段制造**误报**，
直接打击「误报控制」这一评分项。两者代价不对等，因此偏保守。
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from agent.evidence.models import (
    BBox,
    ChannelReading,
    ConfidenceAdjustment,
    Fact,
    PeriodType,
    ProfitBasis,
    SourceLayer,
    StatementScope,
    derive_id,
)
from agent.evidence.store import REVIEW_THRESHOLD, build_confidence
from agent.runtime.errors import QuoteVerificationError
from agent.runtime.loop import RunContext
from agent.validate.numeric import (
    normalize_unit,
    parse_amount,
    repair_line_broken_numbers,
    to_yuan,
)
from agent.validate.quotecheck import QuoteMatch
from pipelines.r1_extract.common import (
    assert_store_has_page_text,
    hydrate_store_from_channels,
    load_routing,
    read_json,
    write_json,
)

# 指标别名与数值之间允许的最大间隔（字符）
_GAP_CHARS = r"[^。；\n]{0,16}?"
# 数值 + 单位
_NUM = r"[（(]?-?\d[\d,]*(?:\.\d+)?[)）]?"
_UNIT = r"(亿元|百万元|万元|千元|元/股|元每股|亿股|万股|千股|股|%|％|‰|倍)"

# ---------------------------------------------------------------------------
# 单位换算族 —— 用于判断「抽出来的数是不是这一类指标该有的量纲」
# ---------------------------------------------------------------------------

_MONEY_UNITS = {"元", "千元", "万元", "百万元", "亿元"}
_SHARE_UNITS = {"股", "千股", "万股", "亿股"}
_RATIO_UNITS = {"%", "‰", "个百分点"}
_PER_SHARE_UNITS = {"元/股", "元每股", "元股"}


def _unit_family(unit: str) -> str:
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


def _expected_family(metric_unit: str) -> str:
    """指标定义里声明的目标单位 -> 期望的量纲族。"""
    u = normalize_unit(metric_unit)
    if u in ("元"):
        return "money"
    if u in ("股"):
        return "share"
    if u in ("%", "‰"):
        return "ratio"
    if u in ("元每股", "元/股"):
        return "per_share"
    return "other"


# 别名与数值之间不得跨越的边界（避免跨行/跨列张冠李戴）
_BOUNDARY_CHARS = "。；\n\r\t"

# 指标名之后允许出现的连接词（「为」「：」「达」「约」等）
_CONNECTORS = ("为", "：", ":", "达", "约", "至", "是", "计", "额", "数", "为人民币")

# ==========================================================================
# 口径推断 —— E2/E3 类核查的基础
# ==========================================================================


def infer_period(context: str, default: str = "unknown") -> str:
    """从上下文推断报告期。

    **取窗口中第一个年份，而非最后一个。** 原因是中文财报的叙述结构：
    「公司实现营业收入 X 万元，同比增长 Y%」—— 前一个年份是本报告期，
    后一个年份（或「上年同期」）是基期。实测取最后一个会把 2024 当成
    2025 年报告期，进而让 R5 的口径比对全部错位。

    表格标题同理：「2025年 / 2024年」两列表头中，当前期总是排在前面。
    """
    matches = list(re.finditer(r"(20\d{2})\s*年?\s*(半年|前三季度|一季度|三季度|年度|全年)?", context))
    if not matches:
        return default

    # 优先取明确标注了期间形态的匹配；否则取第一个
    chosen = matches[0]
    for m in matches:
        if m.group(2):
            chosen = m
            break

    year, span = chosen.group(1), chosen.group(2) or ""
    suffix = {
        "半年": "H1", "前三季度": "Q3", "一季度": "Q1",
        "三季度": "Q3", "年度": "A", "全年": "A", "": "A",
    }.get(span, "A")
    return f"{year}{suffix}"


def period_window(page_text: str, pos: int, *, back: int = 900, fwd: int = 120) -> str:
    """为期间推断提供足够宽的上下文窗口。

    实现要点：报告期通常写在**表格标题**里（如「单位：万元 / 2025年」），
    与具体数值之间隔着整个表体，可能有好几行。因此：

      * 不能按单个换行截断 —— 那会把表头切掉，导致期间大面积缺失，
        而期间缺失会让 R5 的口径比对失去意义（实测踩过这个坑）。
      * 应当退到**更大的结构边界**：空行、章节标题（第X节 / 一、二、）
        或表格起始行。

    找不到期间时返回原文切片，由 :func:`infer_period` 返回默认值。
    """
    start = max(0, pos - back)
    window = page_text[start : min(len(page_text), pos + fwd)]

    # 退到最近的结构边界，避免读到上一张表或上一节的期间。
    # 注意：边界取**标题行的末尾**（含标题本身），而不是标题之前 ——
    # 因为期间信息常常就写在标题里（如「第四节 …（2025年）」）。
    boundary = -1
    for pattern in (
        r"\n\s*\n",
        r"\n\s*第[一二三四五六七八九十]+[节章][^\n]*",
        r"\n\s*[一二三四五六七八九十]+、[^\n]*",
    ):
        for m in re.finditer(pattern, window):
            boundary = max(boundary, m.end())
    if boundary > 0:
        window = window[boundary:]
    return window


_BS_METRICS = {
    "total_assets", "total_liabilities", "equity_total", "equity_parent",
    "cash_and_equivalents", "shares_outstanding",
    "short_term_borrowing", "long_term_borrowing", "bonds_payable",
}


def infer_period_type(context: str, metric: str) -> PeriodType:
    if metric in _BS_METRICS:
        return PeriodType.POINT_IN_TIME
    if any(k in context for k in ("单季度", "本季度")):
        return PeriodType.DURATION_SINGLE_Q
    if any(k in context for k in ("年初至", "累计", "上半年", "前三季度")):
        return PeriodType.DURATION_YTD
    return PeriodType.DURATION_YTD


def infer_profit_basis(alias: str, metric: str) -> ProfitBasis:
    if metric == "net_profit_parent_ex_nonrec" or "扣非" in alias or "扣除非经常性" in alias:
        return ProfitBasis.NET_PARENT_EX_NONREC
    if metric == "net_profit_parent":
        return ProfitBasis.NET_PARENT
    if metric == "net_profit_total":
        return ProfitBasis.NET_TOTAL
    return ProfitBasis.UNKNOWN


def infer_statement_scope(context: str) -> StatementScope:
    if any(k in context for k in ("母公司报表", "母公司口径", "母公司资产负债表")):
        return StatementScope.PARENT_ONLY
    # A 股定期报告正文默认披露合并报表。这是**推断**，会体现在置信度说明中。
    return StatementScope.CONSOLIDATED


# 候选里的口径字符串 → 契约枚举。
#
# ★ 为什么把这张表抽成函数（2026-09 第十三轮，实测踩过）：
#
#   原实现是内联在候选入库循环里的一段 if/elif 白名单。新增
#   `segment_total` / `segment_detail` 之后，两个新值落进 else 分支，
#   被 `infer_statement_scope` 覆盖成 `consolidated` —— 于是
#   **"按取值落在哪一列定口径"在产物里完全看不出来**：
#   表里已经算对了 `segment_total`，事实库里却仍是 `consolidated`，
#   冲突守卫照样把 24 条候选全标 needs_review，等于白改一场，
#   而且**不报错**（口径是合法枚举值）。
#
#   抽成独立函数之后，可以有一条直接的测试盯住"每个候选口径都能映射"，
#   新增值时忘记接线会立刻被测试抓住。
_SCOPE_OF_CANDIDATE = {
    "parent_only": StatementScope.PARENT_ONLY,
    "consolidated": StatementScope.CONSOLIDATED,
    "segment_total": StatementScope.SEGMENT_TOTAL,
    "segment_detail": StatementScope.SEGMENT_DETAIL,
}


def scope_of_candidate(scope: str, page_text: str) -> StatementScope:
    """把候选里的口径字符串转成 :class:`StatementScope`。

    未在表中的取值回退到 :func:`infer_statement_scope`（页面文本推断）——
    保持原有兜底行为，不因为新增枚举而改变老路径。
    """
    mapped = _SCOPE_OF_CANDIDATE.get(str(scope or "").strip())
    return mapped if mapped is not None else infer_statement_scope(page_text)


# ==========================================================================
# 主体识别
# ==========================================================================


def infer_entity(page_text: str, filename: str) -> str:
    """从页面文本/文件名中识别主体（公司名或股票代码）。"""
    m = re.search(r"(?:证券代码|股票代码|代码)\s*[:：]?\s*(\d{6})", page_text)
    if m:
        return m.group(1)
    m = re.search(r"([\u4e00-\u9fa5]{2,12}(?:股份有限公司|有限责任公司|集团股份有限公司))", page_text)
    if m:
        return m.group(1)
    # 退化为文件名主干（去掉常见后缀）
    base = re.sub(r"\.(pdf|PDF)$", "", filename)
    base = re.sub(r"[-_（(].*$", "", base)
    return base[:40] or "UNKNOWN"


# ==========================================================================
# 规则抽取
# ==========================================================================


def find_metric_occurrences(
    page_text: str,
    aliases: list[str],
    *,
    expected_family: str | None = None,
    claimed_spans: set[tuple[int, int]] | None = None,
    max_hits: int = 32,
) -> list[dict[str, Any]]:
    """为**单个指标**定位「别名 + 数值 + 单位」组合。

    **算法：以数值为中心，最近的别名匹配。**

    朴素做法（对每个别名做独立正则）会踩两个坑，实测都出现过：

      * 「占公司总股本比例 12.40%」——别名「总股本」匹配到 12.40%，
        把占比当成了股数。
      * 「归属于母公司股东的净利润」与「净利润」并列时，
        别名「净利润」匹配到了归母那一行的数值。

    解法是：先扫数值，再在数值左侧同一行内找别名，取得分最高者，
    得分为「别名长度² ÷ 距离」——**更长、更近的别名胜出**。

    单指标内部仍会跨指标冲突（如上例的「净利润」）。这由调用方通过
    ``claimed_spans`` 做**全局仲裁**：按别名长度降序处理各指标，
    先处理的长别名声明其覆盖区间，后处理的短别名不得不与之重叠。
    「归母净利润」比「净利润」长，因此先声明、先获胜。

    另有两道独立闸门：
      * **边界**：别名与数值之间不得跨越句号/换行/制表符（防跨行错配）
      * **量纲**：抽出的单位必须与指标声明的单位同族
    """
    if not page_text:
        return []

    alias_list = sorted({a for a in aliases if a}, key=len, reverse=True)
    if not alias_list:
        return []

    claimed = claimed_spans if claimed_spans is not None else set()
    out: list[dict[str, Any]] = []
    local_spans: list[tuple[int, int]] = []
    used_values: set[tuple[str, str]] = set()

    pattern = re.compile(r"(?P<num>" + _NUM + r")\s*(?P<unit>" + _UNIT + r"?)")
    for m in pattern.finditer(page_text):
        raw_num = m.group("num")
        unit = normalize_unit(m.group("unit") or "")
        value_pos = m.start()

        # —— 量纲闸门 ——
        if expected_family and unit and _unit_family(unit) != expected_family:
            continue

        # —— 仲裁闸门：该数值的来源区间已被更具体的指标占用 ——
        if any(
            not (m.end() <= s or e <= value_pos) for s, e in claimed
        ):
            continue

        # —— 找最近的、行内无边界阻隔的别名 ——
        best_alias: str | None = None
        best_score = 0.0
        best_start = -1
        for alias in alias_list:
            start = max(0, value_pos - 60)
            idx = page_text.rfind(alias, start, value_pos)
            if idx < 0:
                continue
            between = page_text[idx + len(alias) : value_pos]
            # ★ 「指标名：<换行>数值」是排版折行，不是跨行错配，必须放过。
            #   实测：单元格里「营业收入：385,643.08 万元；净利润：\n80,082.13 万元」
            #   —— 冒号后正好折行，早期一律按"跨行"拒掉，
            #   于是这些值只剩 VLM 一条来源，双路互证里表现为"确定性路径没覆盖"。
            #   判据：把**空白与标签分隔符**都去掉之后若什么都不剩，
            #   说明别名与数值之间只有排版符号，可以跨行；否则仍按边界规则拒。
            core = between.strip(" \t\r\n\u3000：:、,，")
            if core and any(ch in between for ch in _BOUNDARY_CHARS):
                continue
            residue = between
            for conn in _CONNECTORS:
                residue = residue.replace(conn, "")
            if residue.strip():
                continue
            distance = max(1, value_pos - (idx + len(alias)))
            score = (len(alias) ** 2) / distance
            if score > best_score:
                best_score = score
                best_alias = alias
                best_start = idx

        if best_alias is None:
            continue

        # —— 去重：同一 (数值, 单位) 在一页内只归属一个指标 ——
        dedup_key = (raw_num.replace(",", "").strip("()（）"), unit)
        if dedup_key in used_values:
            continue

        parsed = parse_amount(raw_num + unit)
        if parsed is None:
            continue
        value, parsed_unit = parsed
        unit = unit or parsed_unit

        used_values.add(dedup_key)
        local_spans.append((best_start, m.end()))
        out.append(
            {
                "alias": best_alias,
                "value": value,
                "unit": unit,
                "raw_text": (raw_num + (m.group("unit") or "")).strip(),
                "quote": page_text[max(0, best_start) : min(len(page_text), m.end())],
                "start": value_pos,
                "end": m.end(),
                "context": page_text[max(0, value_pos - 80) : m.end() + 40],
                "match_score": round(best_score, 3),
            }
        )
        if len(out) >= max_hits:
            break

    # 把本次声明的区间并入全局占用集合，供后续（更短别名的）指标仲裁
    claimed.update(local_spans)
    return out


# ==========================================================================
# 主阶段
# ==========================================================================


def run(ctx: RunContext) -> dict[str, Any]:
    from agent.config import load_config

    metrics_cfg = load_config("metrics")
    routing = load_routing(ctx)
    fuzzy = float(routing.get("quote_fuzzy_threshold", 0.86))

    documents = {d["doc_id"]: d for d in read_json(ctx, "facts/documents.json")}
    doc_periods = {
        d["doc_id"]: d.get("document_period", "unknown") for d in documents.values()
    }
    text_channels = read_json(ctx, "channels/text_layer.json")
    ocr_channels = read_json(ctx, "channels/ocr.json")

    # ★★ 先把内存证据库灌回来，再断言它确实有页面原文。
    #
    #   为什么（第十八轮实测，代价是一次完整预热）：`extract_text` 是唯一把
    #   「权威页面原文」注册进 ctx.store 的阶段，而 ctx.store 是纯内存对象。
    #   `--resume` 跳过它 ⇒ 库是空的 ⇒ 本函数下面的表格路径
    #   `find_unit_declaration(page_text)` 找不到任何单位 ⇒ 1590 行按「无单位」
    #   拒绝 ⇒ **0 条事实，且不报错**。实测：完整跑 1050 条 → 续跑重跑 0 条，
    #   而 channels/*.json 逐字节相同（差异只在内存里）。
    #   灌回来之后重跑，accepted / table_grid_candidates 与完整跑**完全一致**。
    _hyd = hydrate_store_from_channels(ctx)
    assert_store_has_page_text(ctx, list(documents))
    if ctx.tracer and any(_hyd.values()):
        ctx.tracer.stage_event(
            status="warn",
            detail=f"内存证据库是空的，已从 channels/ 灌回：{_hyd}",
        )

    # 按页合并通道
    page_channels: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    for rec in text_channels:
        page_channels.setdefault((rec["doc_id"], rec["page"]), {})["text_layer"] = rec
    for rec in ocr_channels:
        page_channels.setdefault((rec["doc_id"], rec["page"]), {})["ocr"] = rec

    entities: dict[str, str] = {}
    for (doc_id, _page), chans in page_channels.items():
        if doc_id not in entities:
            primary = chans.get("text_layer") or chans.get("ocr")
            entities[doc_id] = infer_entity(
                (primary or {}).get("text", ""), documents.get(doc_id, {}).get("filename", "")
            )

    stats: dict[str, int] = {
        "candidates": 0,
        "accepted": 0,
        "rejected_no_unit": 0,
        "rejected_quote": 0,
        "needs_review": 0,
        "multi_channel_agree": 0,
        # 折行断裂修复次数（数字被切断 / 单位被切断）
        "broken_number_repairs": 0,
    }

    # ---------- 5.1 规则抽取 ----------
    # 指标按「最长别名」降序处理：更具体的指标先声明其覆盖的原文区间，
    # 后续更泛化的指标（如「净利润」vs「归母净利润」）无法再抢占同一区间。
    # 这是保证跨指标归属正确的关键一步。
    metric_plan: list[tuple[int, dict[str, Any], list[str]]] = []
    for metric_def in metrics_cfg.get("metrics", []):
        canonical = metric_def["canonical"]
        aliases = [metric_def.get("label", canonical)] + list(metric_def.get("aliases", []))
        aliases = [a for a in aliases if a]
        metric_plan.append((max((len(a) for a in aliases), default=0), metric_def, aliases))
    metric_plan.sort(key=lambda t: -t[0])

    for (doc_id, page), chans in sorted(page_channels.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        text_rec = chans.get("text_layer")
        ocr_rec = chans.get("ocr")
        primary = text_rec or ocr_rec
        if primary is None:
            continue

        # ★ 抽取以**权威原文**为准（raw 字段），而非按阅读顺序重排的 text。
        #   重排会打乱「单位：万元 / 2025年」这类表头与数据行的相对位置，
        #   导致期间推断失效 —— 实测踩过这个坑。
        page_text_raw = "\n".join(
            filter(None, [
                (text_rec or {}).get("raw") or (text_rec or {}).get("text", ""),
                (ocr_rec or {}).get("raw") or (ocr_rec or {}).get("text", ""),
            ])
        )
        # 先修折行断口：数字被切断、单位被切断（「717,247.24 万 / 元」）。
        # 不修的话别名—数值—单位这个模式跨不过换行，整页数据一条都抽不出来。
        page_text, repairs = repair_line_broken_numbers(page_text_raw)
        if repairs:
            stats["broken_number_repairs"] = stats.get("broken_number_repairs", 0) + len(
                repairs
            )
        entity = entities.get(doc_id, "UNKNOWN")
        source_layer = (
            SourceLayer.L1_TEXT_LAYER if text_rec is not None else SourceLayer.L2_OCR
        )

        # 每页独立仲裁：claimed_spans 只在本页内生效
        claimed_spans: set[tuple[int, int]] = set()
        for _alias_len, metric_def, aliases in metric_plan:
            canonical = metric_def["canonical"]
            family = _expected_family(metric_def.get("unit", ""))
            occ = find_metric_occurrences(
                page_text, aliases, expected_family=family, claimed_spans=claimed_spans
            )
            if not occ:
                continue

            # 每页每个指标最多取多少条。
            # 早期写死 4 —— 实测这会把「一页里有 3 家公司 × 2 个期间」的表截断：
            # 第 5、6 个取值被静默丢掉，于是那些值只剩 VLM 一条来源
            # （双路互证里表现为"确定性路径没覆盖"，实际是被上限截了）。
            # 现在由配置控制，默认放到 16。
            max_per_metric = int(
                (ctx.configs.get("extract") or {}).get("max_facts_per_metric_page", 16)
            )
            for hit in occ[:max_per_metric]:
                stats["candidates"] += 1
                # 必须在进入各闸门之前初始化 —— 闸门里的 `continue` 会跳过
                # 后面的赋值，导致 UnboundLocalError（实测踩过）。
                period_conflict = False

                # 闸门 1：单位缺失直接拒收 —— 宁可漏，不可猜
                if not hit["unit"]:
                    stats["rejected_no_unit"] += 1
                    ctx.ledger.reject(
                        "单位缺失，拒绝猜测（金融数据不允许无单位入库）",
                        doc_id=doc_id, page=page, metric=canonical, raw=hit["raw_text"],
                    )
                    continue

                # 归一化到标准单位
                try:
                    if hit["unit"] in ("万股", "亿股", "千股", "股"):
                        target_unit = "股"
                        value_std = _shares_to_shares(hit["value"], hit["unit"])
                    elif hit["unit"] in ("%", "％"):
                        target_unit = "%"
                        value_std = hit["value"]
                    elif hit["unit"] == "倍":
                        target_unit = "倍"
                        value_std = hit["value"]
                    elif hit["unit"] in ("元/股", "元每股"):
                        target_unit = "元每股"
                        value_std = hit["value"]
                    else:
                        target_unit = "元"
                        value_std = to_yuan(hit["value"], hit["unit"])
                except Exception as exc:
                    stats["rejected_no_unit"] += 1
                    ctx.ledger.reject(
                        "单位换算失败，拒绝入库",
                        doc_id=doc_id, page=page, metric=canonical,
                        unit=hit["unit"], error=str(exc),
                    )
                    continue

                # 闸门 2：quote 回读（幻觉硬闸门）
                try:
                    span, match_status, similarity = ctx.store.verify_and_anchor(
                        doc_id=doc_id,
                        page=page,
                        quote=hit["quote"],
                        source_layer=source_layer,
                        fuzzy_threshold=fuzzy,
                    )
                except QuoteVerificationError as exc:
                    stats["rejected_quote"] += 1
                    ctx.ledger.reject(
                        "quote 回读失败", doc_id=doc_id, page=page,
                        metric=canonical, error=str(exc),
                    )
                    continue

                # 多路一致性：两个通道都能读到同一数值
                agree = _cross_channel_agree(hit["raw_text"], text_rec, ocr_rec)
                if agree:
                    stats["multi_channel_agree"] += 1

                arithmetic_ok = _looks_like_total(hit["quote"])
                extra_adj: list[ConfidenceAdjustment] = []
                if period_conflict:
                    extra_adj.append(
                        ConfidenceAdjustment(
                            reason="页级与文档级报告期不一致（已采信文档级）", delta=-0.15
                        )
                    )
                breakdown = build_confidence(
                    source_layer=source_layer,
                    quote_match=match_status,
                    cross_channel_agree=agree,
                    arithmetic_ok=arithmetic_ok,
                    extra=extra_adj,
                )

                readings = [
                    ChannelReading(
                        channel=c,
                        raw_text=hit["raw_text"],
                        page=page,
                        confidence=0.95 if c == "text_layer" else 0.80,
                    )
                    for c in ("text_layer", "ocr")
                    if (chans.get(c) is not None and hit["raw_text"].replace(",", "") in
                        (chans[c].get("text", "").replace(",", "")))
                ]

                needs_review = breakdown.score < REVIEW_THRESHOLD
                if needs_review:
                    stats["needs_review"] += 1

                window = period_window(page_text, hit["start"])
                period = infer_period(window)
                doc_period = doc_periods.get(doc_id, "unknown")

                if period == "unknown":
                    # 页级推断失败时回退到**文档级报告期**。
                    # 很多续表页不重复印年份，只有「单位：万元」；
                    # 若不回退，这些页的期间会缺失，R5 的口径比对将无从进行。
                    period = doc_period
                elif doc_period != "unknown" and period != doc_period:
                    # ★ 页级推断与文档级不一致 —— 实测踩过的坑：
                    #   页面里可能同时出现多个年份（如「2025 年与 2014 年对比」
                    #   或引用历史数据），页级推断会取错。
                    #   此时**采信文档级**（它由全文年份频次统计得出，更可靠），
                    #   同时降低置信度以标记这一不确定性。
                    period_conflict = True
                    period = doc_period

                use_context = window if period != "unknown" else hit["context"]

                fact = Fact(
                    fact_id=derive_id(
                        "fct", doc_id, canonical, hit["raw_text"], str(page)
                    ),
                    doc_id=doc_id,
                    entity=entity,
                    metric=canonical,
                    value=value_std,
                    unit=target_unit,
                    raw_text=hit["raw_text"],
                    period=period,
                    period_type=infer_period_type(use_context, canonical),
                    statement_scope=infer_statement_scope(use_context),
                    profit_basis=infer_profit_basis(hit["alias"], canonical),
                    source_layer=source_layer,
                    evidence_span_ids=[span.span_id],
                    channel_readings=readings,
                    confidence=breakdown.score,
                    confidence_breakdown=breakdown,
                    needs_review=needs_review,
                    review_reason=(
                        f"置信度 {breakdown.score:.2f} 低于阈值 {REVIEW_THRESHOLD}"
                        if needs_review else None
                    ),
                    extractor="rule_engine@1.0.0",
                    extracted_at=ctx.build_stamp(),
                )
                ctx.ledger.add(fact)
                stats["accepted"] += 1

    # ---------- 5.15 表格网格抽取（确定性，优先于 VLM）----------
    stats.update(_ingest_table_grid(
        ctx, metrics_cfg, entities, doc_periods, fuzzy, stats))

    # ---------- 5.2 视觉读数补录（整页扫描，含指标归属）----------
    stats["vlm_accepted"] = _ingest_vlm_scans(ctx, documents, entities, fuzzy, stats)

    # ---------- 5.25 主体归属纠正（必须在**所有**事实入库之后）----------
    # ⚠️ 早期这一步写在 `_ingest_table_grid` 里，即**阶段 5.2 之前** ——
    #    于是 VLM 事实根本轮不到被纠正（它们的 entity 来自模型的 entity_hint，
    #    在密集的子公司表里同样会把子公司的数挂到母公司名下）。
    #    实测：父公司 118034 名下 4 条营业收入全部来自 VLM，且互不相同
    #    （其实是 4 家子公司的收入）→ 冲突守卫标记 → R4 无数据可用。
    #    纠正必须覆盖两条路径，因此挪到两条路径都跑完之后。
    stats.update(_reattribute_entities(ctx, stats))

    # ---------- 5.3 主体名归一（只差空白的同一家公司必须合并）----------
    unified = _unify_entities(ctx, stats)
    if unified and ctx.tracer:
        ctx.tracer.stage_event(
            status="ok",
            detail=(f"主体名归一：{unified} 条事实的主体写法与同一实体的其他写法合并"
                    f"（分词/折行差异，不合并会虚增主体并拆散同比分组）"),
        )

    # ---------- 5.4 双路互证加权 ----------
    _apply_cross_validation(ctx, stats)

    # ---------- 5.5 量纲合理性闸门（资产负债表恒等式）----------
    # 必须放在**所有**入库与主体纠正之后：它按 (doc_id, entity, period) 找锚点，
    # 主体名还没归一就找不到锚点，闸门会静默失效。
    stats.update(_check_scale_plausibility(ctx, stats))
    # 期间自相矛盾：同一文档里同一个值被标成两个期间 → 两条都不可信。
    # 必须放在量纲闸门**之后**（它只处理未被标记的事实），且必须进 stats。
    stats.update(_check_period_contradiction(ctx, stats))

    write_json(ctx, "facts/build_stats.json", stats)

    if ctx.tracer:
        ctx.tracer.stage_event(status="ok", detail=str(stats))
    return stats


def _check_period_contradiction(ctx: RunContext, stats: dict[str, Any]) -> dict[str, Any]:
    """标记"同一文档里同一个值被标成两个期间"的事实，并把明细落盘。

    见 :meth:`agent.evidence.store.FactLedger.check_period_contradiction` 的说明：
    实测 601857 的 2025 年报把 2024 年的资本开支 275,849 标成了 2025A，
    而 ``275,849 > 269,089``（2025 年真值），于是 R4 的"同期间取最大值"
    必然取到错的那条，**且不报错**。原有的同表冲突守卫要求"同期间"，
    抓不到这类"期间本身错位"的问题。
    """
    out = {"period_contradiction": 0}
    try:
        flagged = ctx.ledger.check_period_contradiction()
    except Exception as exc:  # noqa: BLE001 - 闸门失败不能拖垮建库
        out["period_contradiction_error"] = 1
        if ctx.tracer:
            ctx.tracer.error(
                where="r1.build_facts", message=f"期间矛盾闸门异常：{exc}"
            )
        return out
    out["period_contradiction"] = len(flagged)
    if flagged:
        write_json(ctx, "facts/period_contradictions.json", flagged)
        if ctx.tracer:
            ctx.tracer.stage_event(
                status="warn",
                detail=(
                    f"期间自相矛盾 {len(flagged)} 组：同一文档里同一个值被标成"
                    "不同期间，相关事实已标为需复核"
                ),
            )
    return out


def _check_scale_plausibility(ctx: RunContext, stats: dict[str, Any]) -> dict[str, Any]:
    """按资产负债表恒等式标记量纲异常事实，并把明细落盘。

    见 :meth:`agent.evidence.store.FactLedger.check_scale_plausibility` 的说明：
    跨表量级差异在年报里很常见（合并数 / 子公司数 / 分部数），因此**不能**
    简单地"跨表量级不一致就报错"。但"货币资金超过资产总额"没有合法解释 ——
    钱本身就是资产的一部分。这一条恒等式足以抓住 100 倍级的单位错误，
    而实测那类错误可以直接把 R4 的每股价值推高一个数量级。
    """
    out = {"scale_implausible": 0}
    try:
        flagged = ctx.ledger.check_scale_plausibility()
    except Exception as exc:  # noqa: BLE001 - 闸门自身出问题不能连累建库
        if ctx.tracer:
            ctx.tracer.stage_event(
                status="warn",
                detail=f"量纲合理性闸门执行失败，本次未做该检查：{exc}",
            )
        return out
    out["scale_implausible"] = len(flagged)
    if flagged:
        write_json(ctx, "facts/scale_implausible.json", flagged)
    stats["scale_implausible"] = len(flagged)
    if ctx.tracer and flagged:
        preview = "；".join(
            f"{w['metric']} {w['values'][0]} vs 锚点 {w['values'][1]}（{w['ratio']}×）"
            for w in flagged[:3]
        )
        ctx.tracer.stage_event(
            status="warn",
            detail=(f"量纲合理性闸门标记 {len(flagged)} 条：{preview}"
                    f"（明细见 facts/scale_implausible.json）"),
        )
    return out


def _reattribute_entities(ctx: RunContext, stats: dict[str, Any]) -> dict[str, Any]:
    """主体归属纠正：事实原文若落在某张表的某个单元格里，就按**该行的主体**归属。

    为什么两条路径都要纠正：

    * ``rule_engine`` —— 整页文本抽取，主体默认是**文档主体**
    * ``vlm_scan``    —— 主体来自模型的 ``entity_hint``，而模型在密集的
      子公司表里同样会把子公司的数挂到母公司名下
      （实测：父公司 118034 名下 4 条营业收入全来自 VLM，其实是 4 家子公司的）

    ``table_grid`` 不需要：它的主体本来就是按行主体取的。

    定位不到就**保持原样** —— 宁可不动，也不乱动。每次改动都落盘可核对。
    """
    from pipelines.r1_extract.entity_attribution import cell_lookup, reattribute

    out = {"entity_reattributed": 0}
    try:
        records = read_json(ctx, "channels/tables.json")
    except FileNotFoundError:
        return out
    if not records:
        return out

    alias_map: dict[str, str] = {}
    metric_units: dict[str, str] = {}
    for metric_def in (ctx.configs.get("metrics") or {}).get("metrics", []):
        canonical = metric_def["canonical"]
        metric_units[canonical] = metric_def.get("unit", "")
        for alias in [metric_def.get("label", canonical)] + list(metric_def.get("aliases", [])):
            if alias:
                alias_map.setdefault(alias, canonical)

    index = cell_lookup(records, alias_map, metric_units)
    if not index:
        return out

    def _page_of(fact: Any) -> int | None:
        spans = ctx.store.spans_of(fact)
        return spans[0].page if spans else None

    targets = [
        f for f in ctx.ledger.all()
        if str(getattr(f, "extractor", "")).split("@")[0]
        in ("rule_engine", "vlm_scan", "vlm_field")
    ]
    changed, detail = reattribute(targets, index, _page_of)
    out["entity_reattributed"] = changed
    stats["entity_reattributed"] = changed
    if detail:
        write_json(ctx, "facts/entity_reattribution.json", detail)
    if ctx.tracer and changed:
        ctx.tracer.stage_event(
            status="ok",
            detail=(f"主体归属纠正：{changed} 条事实的原文落在表格行里，"
                    f"已按该行主体重新归属（明细见 facts/entity_reattribution.json）"),
        )
    return out


def _apply_cross_validation(ctx: RunContext, stats: dict[str, Any]) -> int:
    """双路互证：两条**互不相干**的路径给出同一个数，就提升置信度。

    为什么值得做：置信度模型里原本只有一个"多路独立同值 +0.05"，
    它比的是**文本层与 OCR** —— 而这两路在多数页面上是同源的（都读文本层/同一张图）。
    真正独立的两路是：

        * 确定性路径：表格网格按行列取值 / 规则引擎按别名定位
        * 模型路径：VLM 看图读数

    它们用完全不同的机制得到同一个数，这种一致比任何单路自评都硬。
    实测一次运行里 162 条模型读数有 139 条（86%）被确定性路径独立复现。

    比对口径是 **（主体, 指标, 数值）三元组**，不按 (主体,指标) 打包 ——
    同一指标天然有多个取值（多行/多期），打包比会得出"要么全对要么全错"的假结论。
    """
    from agent.evidence.models import ConfidenceAdjustment

    facts = ctx.ledger.all()
    det = {(f.entity, f.metric, str(f.value)) for f in facts
           if str(getattr(f, "extractor", "")).split("@")[0]
           in ("table_grid", "rule_engine")}
    mod = {(f.entity, f.metric, str(f.value)) for f in facts
           if str(getattr(f, "extractor", "")).startswith("vlm")}

    both = det & mod
    if not both:
        stats["cross_validated_facts"] = 0
        return 0

    boosted = 0
    for f in facts:
        if (f.entity, f.metric, str(f.value)) not in both:
            continue
        bd = f.confidence_breakdown
        if bd is None:
            continue
        if any("双路互证" in a.reason for a in bd.adjustments):
            continue
        # 直接在原有明细后追加一项 —— `ConfidenceBreakdown.score` 是**算出来的**，
        # 因此追加即生效，且报告里能逐项复算（不做黑盒加分）。
        bd.adjustments.append(
            ConfidenceAdjustment(
                reason="双路互证：确定性路径与模型读数独立得到同一数值", delta=0.05
            )
        )
        f.confidence = bd.score
        # ⚠️ **绝不因此清除 needs_review**。
        #    该标记有两个来源：置信度低于阈值，以及**同主体指标冲突**。
        #    实测踩过：这里原本写 `f.needs_review = f.confidence < REVIEW_THRESHOLD`，
        #    于是被"冲突守卫"标记过的事实，仅因为双路互证加了 0.05 就被**解除标记**，
        #    冲突值重新流向下游（R4 直接拿它当估值输入）。
        #    加分只能提高置信度；"要不要人看一眼"是另一个判断，不归这里管。
        boosted += 1

    stats["cross_validated_facts"] = boosted
    stats["cross_validated_keys"] = len(both)
    if ctx.tracer and boosted:
        ctx.tracer.stage_event(
            status="ok",
            detail=(
                f"双路互证：{len(both)} 组（主体,指标,数值）被确定性路径与模型读数"
                f"独立复现，共 {boosted} 条事实置信度 +0.05"
            ),
        )
    return boosted


def _unify_entities(ctx: RunContext, stats: dict[str, Any]) -> int:
    """把**只差空白**的同一主体归一到同一个写法。

    实测踩过的坑：

        表格/规则路径产出   ``JinkoSolar(U.S.)Inc.``（早期把空格全删了）
        VLM 读出            ``JinkoSolar (U.S.) Inc.``（保留排版空格）

    同一家公司在事实库里变成**两个主体**，后果有三个，而且都不报错：

    1. 虚增主体数（R4 选估值主体时被干扰）
    2. 拆散 R5 的同比分组（两个"不同主体"各有一期数据，重算就配不上对）
    3. 双路互证少算匹配（模型读数被误判成"确定性路径没覆盖"）

    规范写法优先取 **VLM 读出的那种** —— 它来自渲染图像，最接近真实排版；
    没有 VLM 事实时取出现次数最多的。

    ⚠️ **只处理"纯空白差异"**。折行把单词从中间切断的情况
    （``INTERNATI`` + ``ONA`` → ``INTERNATI ONA``）不在覆盖范围内：
    去掉空格后是 ``INTERNATIONA``，与 ``INTERNATIONAL`` 并不相等，
    靠"删空格"是合并不了的。硬做需要词形判断，代价与风险都不值得，
    因此如实记为已知残留。
    """
    from collections import Counter, defaultdict

    groups: dict[str, list[Any]] = defaultdict(list)
    for f in ctx.ledger.all():
        key = re.sub(r"\s+", "", getattr(f, "entity", "") or "")
        if key:
            groups[key].append(f)

    changed = 0
    for items in groups.values():
        if len({str(f.entity) for f in items}) == 1:
            continue
        vlm_forms = [str(f.entity) for f in items
                     if str(getattr(f, "extractor", "")).startswith("vlm")]
        canonical = Counter(vlm_forms or [str(f.entity) for f in items]).most_common(1)[0][0]
        for f in items:
            if str(f.entity) != canonical:
                f.entity = canonical
                changed += 1
                # 合并主体可能**新产生**同主体指标冲突，必须重查一遍
                try:
                    ctx.ledger._check_internal_conflict(f)  # noqa: SLF001
                except Exception:
                    pass
    stats["entity_unified"] = changed
    return changed


def _shares_to_shares(value: Decimal, unit: str) -> Decimal:
    table = {"股": Decimal("1"), "千股": Decimal("1000"),
             "万股": Decimal("10000"), "亿股": Decimal("100000000")}
    return value * table[unit]


def _table_bbox(cells: list[dict[str, Any]]) -> tuple[float, float, float, float] | None:
    """整张表的矩形（所有单元格 bbox 的并集）。缺 bbox 时返回 None。

    用于把"网格表"与"报表标题文本框"做**矩形交叠**配对
    （见 :meth:`EvidenceStore.scope_by_overlap`）—— 口径判定靠空间关系，
    不能靠坐标先后（实测：年报里表格的视觉顺序与报表印刷顺序不一致）。
    """
    xs0: list[float] = []
    ys0: list[float] = []
    xs1: list[float] = []
    ys1: list[float] = []
    for c in cells or []:
        bbox = c.get("bbox") or {}
        try:
            xs0.append(float(bbox["x0"]))
            ys0.append(float(bbox["y0"]))
            xs1.append(float(bbox["x1"]))
            ys1.append(float(bbox["y1"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not xs0:
        return None
    return (min(xs0), min(ys0), max(xs1), max(ys1))


def _first_y(cells: list[dict[str, Any]]) -> float:
    """表格记录里最小的纵坐标 —— 用于与报表标题对齐（标题在正文层）。

    缺 bbox 时返回 0.0，让 :meth:`EvidenceStore.preceding_statement_scope`
    退化为"取本页第一个标题之前的最近标题"，即按页归属（安全兜底）。
    """
    ys = []
    for c in cells or []:
        bbox = c.get("bbox") or {}
        y0 = bbox.get("y0")
        if y0 is not None:
            try:
                ys.append(float(y0))
            except (TypeError, ValueError):
                continue
    return min(ys) if ys else 0.0


def _ingest_table_grid(
    ctx: RunContext,
    metrics_cfg: dict[str, Any],
    entities: dict[str, str],
    doc_periods: dict[str, str],
    fuzzy: float,
    stats: dict[str, Any],
) -> dict[str, Any]:
    """把**表格网格**当作一等抽取来源（``extractor=table_grid@1.0.0``）。

    为什么需要这条路径（实测动机）：真实材料上 222 条事实里有 161 条（73%）
    来自 VLM 整页扫描，而它们读的正是**已经用几何方法重建好的表格**。
    同一批数字明明有确定性来源，却在花模型调用去"看"，
    而且肉眼在多列多行的表里做归属，天生比按行列取值更容易错配。

    与 5.1 / 5.2 共用同一套闸门：单位必须明确、quote 必须回读成功、
    锚点落在**单元格 bbox** 上（比整页级 quote 更精确）。

    去重：若同文档同主体同指标已有**同值**事实（多半来自 5.1 文本规则），
    则不重复入库，只记 ``table_grid_dup``。同一页里不同主体的同名数值
    （如两家子公司营收都是 0）不会互相覆盖 —— fact_id 里带主体与行列。
    """
    from pipelines.r1_extract.table_facts import (
        build_header,
        extract_table_facts,
        find_unit_declaration,
        map_columns_to_metrics,
        row_y_from_cells,
    )

    out = {
        "table_grid_tables": 0,
        "table_grid_candidates": 0,
        "table_grid_accepted": 0,
        "table_grid_dup": 0,
        "table_grid_rejected_quote": 0,
        "table_grid_rejected_no_unit": 0,
        "table_grid_rejected_bad_value": 0,
        "table_grid_carried": 0,
        "table_grid_statement": 0,
        "table_grid_suspect": 0,
        "table_grid_aligned": 0,
        "table_grid_align_cols": 0,
        "entity_reattributed": 0,
    }

    try:
        records = read_json(ctx, "channels/tables.json")
    except FileNotFoundError:
        return out
    if not records:
        return out

    alias_map: dict[str, str] = {}
    metric_units: dict[str, str] = {}
    for metric_def in metrics_cfg.get("metrics", []):
        canonical = metric_def["canonical"]
        metric_units[canonical] = metric_def.get("unit", "")
        for alias in [metric_def.get("label", canonical)] + list(metric_def.get("aliases", [])):
            if alias:
                alias_map.setdefault(alias, canonical)

    seen: set[tuple[str, str, str, str]] = {
        (f.doc_id, f.entity, f.metric, str(f.value)) for f in ctx.ledger.all()
    }

    # 续表表头继承：中文公告的长表跨页时**只在首页印表头**，
    # 续页直接是数据行。实测一份材料 28 页的表里只有第 1 页能抽出事实，
    # 其余 27 页全部因为"没有表头 → 列匹配不上指标"而颗粒无收。
    # 这里按文档记住上一张成功映射的表（列数相同才继承，防止串表）。
    carry: dict[str, tuple[dict[int, str], str, int]] = {}
    # 文档级货币单位（``单位：元`` 只在报表首页印一次，续页要延续）
    doc_unit: dict[str, str] = {}
    # 文档级**报表表头**：财务报表跨页时表头只印一次，
    # 而合计行（资产总计/负债合计/所有者权益合计）往往落在续页上。
    # 不继承表头，这些合计行永远抽不到，下游会拿别处同名指标顶上（实测事故）。
    #
    # ⚠️ 键是 **（文档, 口径）** 而不是文档单键 —— 因为年报里合并表与母公司表
    #    的页序会**交错**（实测 000722 p73–p77）。按文档单键时后出现的表头
    #    会覆盖先出现的，续页因此可能继承到**另一条报表**的表头。
    doc_header: dict[tuple[str, str], list[str]] = {}
    # 文档级**分部表合计列**：分部报告的表头只印在**第一张表**上，
    # 承载我们真正想要的那几行（资本性支出、折旧摊销、分部资产）的表
    # 紧跟着印在同一页的下方、**没有自己的表头**。
    #
    # 实测（2025 年报 p178）：表 0 是表头 + 分部收入/费用/利润，
    # 表 1 直接是 `折旧、折耗和摊销费用 / 资产减值损失 / 资本性支出 / 分部资产`
    # —— 合计列 269,089 就在表 1 里，而表 1 自己判不出"哪一列是合计"。
    # 不继承就会漏掉本轮要修的那个数。
    #
    # 只在列数相同时继承（与 `carry` 同一条防串表判据）。
    doc_seg_total: dict[str, tuple[int, int]] = {}
    # 文档级**报表口径**（合并 / 母公司）：财报标题只印一次、表格跨页，
    # 因此续表必须继承上一页的口径（见 table_facts.split_by_scope）。
    doc_scope: dict[str, str] = {}
    # 页内后段标题判出的口径：**只影响下一页**，不影响本页页首部分。
    # 分开两个字典是刻意的 —— 一页里可能先结束一条报表、再开始另一条，
    # 页首那部分属于**上一条报表**（实测：000722 的 p75/p77）。
    next_scope: dict[str, str] = {}
    page_of_last: dict[str, int] = {}

    for rec in sorted(records, key=lambda r: (r.get("doc_id") or "",
                                              int(r.get("page") or 0))):
        doc_id = rec.get("doc_id") or ""
        page = int(rec.get("page") or 0)
        grid = rec.get("grid") or []
        # 进入新的一页时，把上一页页内判出的口径升格为本页页首口径
        if doc_id and page_of_last.get(doc_id) != page:
            if doc_id in next_scope:
                doc_scope[doc_id] = next_scope.pop(doc_id)
            page_of_last[doc_id] = page
        if not doc_id or not page or not grid:
            continue
        out["table_grid_tables"] += 1

        page_text = ctx.store.page_text(doc_id, page)
        unit_hint = find_unit_declaration(page_text)
        n_cols = max((len(r) for r in grid), default=0)

        # ★ 单位声明延续：中文定期报告的「单位：元」只在报表首页印一次，
        #   续页直接是数据行。不延续的话，续页整表因"无单位"被拒 ——
        #   实测 000722 的资产负债表跨 p73–p76，只有 p73 有单位声明，
        #   于是「所有者权益合计」「资产总计」这些合计行全部丢失。
        if not unit_hint:
            unit_hint = doc_unit.get(doc_id, "")
        elif doc_unit.get(doc_id) != unit_hint:
            doc_unit[doc_id] = unit_hint

        prior = carry.get(doc_id)
        column_map = None
        if prior and prior[2] == n_cols and not find_unit_declaration(page_text):
            # 本页既没有表头也没有单位声明 → 大概率是续表
            column_map, carry_unit, _ = prior
            if carry_unit and not unit_hint:
                unit_hint = carry_unit

        # 分部表合计列的继承：表头只印在第一张表上，承载目标行的表紧随其后
        # 且没有自己的表头（见 doc_seg_total 的说明）。列数相同才继承。
        seg_prior = doc_seg_total.get(doc_id)
        carry_seg_total = (
            seg_prior[0] if seg_prior and seg_prior[1] == n_cols else None
        )

        # ★ 口径（合并 / 母公司）：取该表**最近的前置报表标题**声明的口径。
        #
        #   为什么不能用"上一页继承"（实测否决）：年报里合并表与母公司表的
        #   页序可能交错 —— 000722 的合并资产负债表从 p73 跨到 p77，
        #   而母公司资产负债表插在 p75（p75 自己的「所有者权益合计」是
        #   3,189,997,628.22，合并真值 3,797,417,361.30 在 p76）。
        #   按上一页继承会把合并表标成母公司表。
        #   正确判据是"这张表属于它前面最近的那个报表标题"，因此标题要
        #   在阶段 2 扫全文得到（见 extract_text.run）。
        # ★ 口径（合并 / 母公司）：按**矩形交叠**把本表配给它所属报表的标题。
        #   判据必须是空间关系，不能是坐标先后 —— 实测中文年报里表格的
        #   视觉顺序与报表的印刷顺序不一致（详见 store.scope_by_overlap 的说明）。
        scope_hint = ""
        tbox = _table_bbox(rec.get("cells") or [])
        if tbox is not None:
            scope_hint = ctx.store.scope_by_overlap(doc_id, page, tbox)
        if not scope_hint:
            # 配不上就退回页首作用域（跨页续表），再退回文档默认
            scope_hint = doc_scope.get(doc_id, "")

        # ⚠️ 传给抽取函数的是**本表**判出的口径（scope_hint），不是页首口径 ——
        #    后者只在配不上时作为兜底，用于跨页续表（见下）。
        #
        # ★ 报表表头的继承必须按 **（文档, 口径）** 取，不能按文档单键
        #   （2026-09 第十五轮实测）。
        #
        #   实测（000722 p73–p77）两条报表**交错**：
        #
        #       p73 表0  ★表头「项目|期末余额|期初余额」  行「流动资产：」  合并
        #       p74 表0   续表（无表头）                  行「使用权资产」  合并
        #       p75 表0   续表（无表头）                  行「租赁负债」    **母公司**
        #       p75 表1  ★表头「项目|期末余额|期初余额」  行「流动资产：」  母公司
        #       p76 表0   续表（无表头）                  行「其他流动资产」母公司
        #
        #   按文档单键时，p75 表1 一建表头就**覆盖**了 p73 的表头；
        #   于是 p74/p76 这两张续表继承到的表头可能来自**另一条报表**，
        #   列→期间的映射随之错位：实测 000722 资产负债表的上期列
        #   （真值 3,777,520,117.62，应为 2024A）被标成了 **2025A**，
        #   与本期列挤进同一期间 —— 于是 2024A 的真值无处安放，
        #   该期间只剩 p159 子公司汇总表的杂项。
        #
        #   按口径键之后，合并表的续表拿合并表的表头、母公司表的续表拿
        #   母公司表的表头 —— 与 `scope_hint` 用的是**同一个判据**
        #   （最近前置标题 / 矩形交叠），两者天然一致。
        carried_header = doc_header.get((doc_id, scope_hint or "")) or doc_header.get(
            (doc_id, "")
        )
        candidates, tstats = extract_table_facts(
            grid,
            alias_map=alias_map,
            metric_units=metric_units,
            unit_hint=unit_hint,
            page_no=page,
            column_map=column_map,
            page_text=page_text,
            doc_entity=entities.get(doc_id, ""),
            doc_period=doc_periods.get(doc_id, "unknown"),
            carry_header=carried_header,
            # 口径（合并 / 母公司）：由**全文档标题扫描**判出的最近前置标题
            # 决定（见上方 scope_hint 的说明）
            page_blocks=ctx.store.page_blocks(doc_id, page),
            row_y=row_y_from_cells(rec.get("cells") or []),
            prev_scope=scope_hint or "consolidated",
            # 单元格坐标：用于按 x 区间重建列结构（见 grid_align 的说明）。
            # 没有它就只能靠列号配对表头与数据，而两者列结构不兼容时必然错位。
            grid_cells=rec.get("cells") or [],
            # 分部表合计列：本表若自带表头，extract_table_facts 会自己判出来
            # 并覆盖这个继承值；判不出来时用继承来的（续表情形）。
            carry_seg_total_col=carry_seg_total,
        )
        # 记住本页最后一个口径，供**续表**继承 —— 财报跨页时标题只印一次。
        # ⚠️ 不能直接改 doc_scope：它同时被用作"页首默认口径"，
        #    而一张表内部可能在页中被切段（表头之前 / 之后分属不同报表）。
        #    页首那一部分必须用**页开始时的作用域**，否则会被本页后段的
        #    标题污染 —— 实测把合并表的尾段标成了母公司口径。
        if tstats.get("statement_table"):
            scopes = [c.get("scope") for c in candidates if c.get("scope")]
            if scopes:
                next_scope[doc_id] = str(scopes[-1])
        # 本页是报表 → 把表头记下来，供**续页**继承。
        #
        # ⚠️ 键必须是 **（文档, 口径）**，不能是文档单键（2026-09 第十五轮实测）。
        #    实测 000722 p73–p77 里合并表与母公司表**交错**，按文档单键会让
        #    p75 表1 的母公司表头覆盖 p73 的合并表头，于是合并表的续表
        #    （p74/p76）继承到错误的期间映射 —— 上期列（2024）被标成 2025A。
        #    详见 `carried_header` 取值处的长注释。
        if tstats.get("statement_table") and not tstats.get("carried_header"):
            from pipelines.r1_extract.table_facts import build_header as _bh
            from pipelines.r1_extract.table_facts import merge_fragments as _mf

            hdr, _ = _bh(_mf(grid).grid, alias_map)
            if hdr and any(hdr):
                doc_header[(doc_id, scope_hint or "")] = hdr
        # 本表判出了分部合计列 → 记下来供**紧随其后、没有表头**的那张表继承
        # （实测 p178：表 0 有表头，目标行在表 1）。
        #
        # ⚠️ 记的是**原始列号**（本表网格坐标系），不是对齐后的列号 ——
        #    下游用 `remap_col_via_maps` 按自己的 col_maps 换算。
        #    实测两张表对齐后的列数可以不同（7 vs 10），套用未换算的列号会错。
        seg_col_here = tstats.get("segment_total_col")
        if seg_col_here is not None and int(seg_col_here) >= 0:
            doc_seg_total[doc_id] = (int(seg_col_here), n_cols)
        if tstats["metric_columns"]:
            # 记下本页的列映射，供后续续页使用
            header, _ = build_header(grid, alias_map)
            mapping = map_columns_to_metrics(header, alias_map, metric_units)
            if mapping:
                carry[doc_id] = (mapping, unit_hint, n_cols)
            elif column_map:
                out["table_grid_carried"] += 1
        out["table_grid_candidates"] += len(candidates)
        out["table_grid_rejected_no_unit"] += tstats["skipped_no_unit"]
        out["table_grid_rejected_bad_value"] += tstats["skipped_bad_value"]
        # 按单元格坐标重建列结构的启用情况 —— 必须计数。
        # 本项目原则「每一个被吞掉的异常都要有可见计数器」，同一条也适用于
        # "某个改进到底有没有真的生效"：没有计数器时，一个从未触发的分支
        # 与一个一直在工作的分支在产物里长得一模一样（本轮实测踩过：
        # 首次跑完 annual28 时产物里查不到任何对齐统计，无法判断它是否生效）。
        if tstats.get("grid_aligned"):
            out["table_grid_aligned"] += 1
            out["table_grid_align_cols"] += int(tstats.get("grid_align_cols") or 0)
        out["table_grid_statement"] += int(tstats.get("statement_table", 0))

        # ⚠️ 这里**故意不做**"跨行量级离群"判断。
        #
        #    曾经加过两版：按文档分组、再按 (主体, 指标) 分组。两版都被实测否决：
        #
        #    * 按文档分组 → needs_review 从 60 涨到 563 条（占全库 63%）。
        #      原因：一份年报里同时有合并数、子公司数、分部数，
        #      子公司 1,404,764,300 元相对母公司合并数被判"小 13 倍"。
        #    * 改按 (主体, 指标) 分组 → 只降到 562 条。原因：**同名主体在不同表里
        #      本就相差几个数量级** —— 子公司汇总表把母公司全名也印在表头，
        #      于是"湖南能源集团发展股份有限公司"的 total_assets 既有合并数
        #      37.97 亿、也有子公司明细值 1,404.76 万，二者相差 27 倍但都是对的。
        #
        #    结论：**"同一 (主体, 指标) 在文档内应当量级一致"这个假设不成立**，
        #    不能用来判错。可靠的量级守卫只存在于**单张表内部**
        #    （见 table_facts.extract_statement_facts 里的 unit_scale_outliers，
        #    以及单元格级的 numeric_defects）。
        #
        #    这条记录保留下来，是为了避免以后又有人"顺手加回一条看起来很有用的判据"。

        cells = {
            (c.get("row"), c.get("col")): c
            for c in (rec.get("cells") or [])
        }
        for cand in candidates:
            key = (doc_id, cand["entity"], cand["metric"], str(cand["value"]))
            if key in seen:
                out["table_grid_dup"] += 1
                continue

            cell = cells.get((cand["row"], cand["col"])) or {}
            bbox = None
            if cell.get("bbox"):
                b = cell["bbox"]
                bbox = BBox(x0=b["x0"], y0=b["y0"], x1=b["x1"], y1=b["y1"])

            try:
                span, match_status, _ = ctx.store.verify_and_anchor(
                    doc_id=doc_id,
                    page=page,
                    quote=cand["quote"],
                    source_layer=SourceLayer.L1_TEXT_LAYER,
                    bbox=bbox,
                    fuzzy_threshold=fuzzy,
                )
            except QuoteVerificationError:
                out["table_grid_rejected_quote"] += 1
                ctx.ledger.reject(
                    "表格网格：单元格文本回读失败", doc_id=doc_id, page=page,
                    metric=cand["metric"], raw=cand["raw_text"],
                )
                continue

            from pipelines.r1_extract.build_facts import infer_period_type as _ipt

            # ★ 期间优先取**列头**里的期间。
            #   财务报表的利润表/现金流量表是"本期 + 上期"并排，期间写在列头
            #   （`2025 年度 | 2024 年度`）。早期实现一律用文档级期间赋值，
            #   于是 2024 年那一列被标成 2025A —— 数字全对、标签全错，
            #   而且不报错：事实库里每个指标只剩一个期间，多期估值无法启动。
            period = cand.get("period") or doc_periods.get(doc_id, "unknown")
            period_type = (
                _ipt(period, cand["metric"]) if cand.get("period")
                else _ipt(page_text, cand["metric"])
            )
            # ★ 口径：由**全文档标题扫描**判出的最近前置标题决定。
            #
            #   撤销过一版"按上一页继承"的实现（实测否决）：一份年报里
            #   合并表与母公司表的页序可能交错 —— 000722 的合并资产负债表
            #   从 p73 跨到 p77，而母公司资产负债表插在 p75/p76。
            #   按上下文继承必然张冠李戴，实测把合并表标成了母公司表。
            #   现在改为把全文标题先扫出来（extract_text），再按
            #   "最近的前置标题"归属（store.preceding_statement_scope）。
            scope = str(cand.get("scope") or "").strip()
            statement_scope = scope_of_candidate(scope, page_text)
            breakdown = build_confidence(
                source_layer=SourceLayer.L1_TEXT_LAYER,
                quote_match=match_status,
                cross_channel_agree=False,
                arithmetic_ok=False,
                extra=[ConfidenceAdjustment(
                    reason="表格网格直取（行列确定，非模型读数）", delta=0.0)],
            )
            needs_review = breakdown.score < REVIEW_THRESHOLD
            # ★ 数值截断 / 量纲离群守卫（F4/F5）：
            #   这两类问题**不会降低置信度** —— 数字看着完全正常。
            #   实测事故：折行修复把 `218,073,073.97` 修成 `218,073,07`，
            #   位数少 4 位、置信度仍是 0.95，直接入库流向下游。
            #   因此这里把它们**显式提升为 needs_review**，
            #   让"不可信"以复核项的形式暴露，而不是静默通过。
            suspect = str(cand.get("suspect") or "")
            review_reason = None
            if suspect:
                needs_review = True
                review_reason = suspect
                out["table_grid_suspect"] = out.get("table_grid_suspect", 0) + 1
            elif needs_review:
                review_reason = f"置信度 {breakdown.score:.2f} 低于阈值 {REVIEW_THRESHOLD}"
            fact = Fact(
                fact_id=derive_id(
                    "fct_tbl", doc_id, cand["entity"], cand["metric"],
                    cand["raw_text"], str(page), f"{cand['row']}-{cand['col']}",
                ),
                doc_id=doc_id,
                entity=cand["entity"],
                metric=cand["metric"],
                value=cand["value"],
                unit=cand["unit"],
                raw_text=cand["raw_text"],
                period=period,
                period_type=period_type,
                statement_scope=statement_scope,
                profit_basis=infer_profit_basis(cand["metric"], cand["metric"]),
                source_layer=SourceLayer.L1_TEXT_LAYER,
                evidence_span_ids=[span.span_id],
                channel_readings=[
                    ChannelReading(
                        channel="table_engine", raw_text=cand["raw_text"],
                        page=page, confidence=0.95,
                        # ★ 行标签必须留痕（2026-09 诊断后补）：
                        #   「合计行 vs 明细行」是同一张表里唯一能把集团口径与
                        #   分部明细区分开的信号，而它此前**完全没有落盘** ——
                        #   于是复核时只看到一堆数值和一句"多口径冲突"，
                        #   无法判断该采信哪条。这条 note 让 fact_probe / 审计
                        #   报告能直接摊开行标签，把人工判定从"猜"变成"看"。
                        note=f"行：{cand.get('row_label') or '?'}"
                             + (f"｜列：{cand['col_period']}" if cand.get("col_period") else ""),
                    )
                ],
                confidence=breakdown.score,
                confidence_breakdown=breakdown,
                needs_review=needs_review,
                review_reason=review_reason,
                extractor="table_grid@1.0.0",
                extracted_at=ctx.build_stamp(),
            )
            ctx.ledger.add(fact)
            seen.add(key)
            out["table_grid_accepted"] += 1

    stats["table_grid_accepted"] = out["table_grid_accepted"]
    stats["table_grid_suspect"] = out.get("table_grid_suspect", 0)
    stats["table_grid_statement"] = out.get("table_grid_statement", 0)
    # 列对齐的启用统计要进 build_stats —— 否则无法回答"它到底生效了没有"
    stats["table_grid_aligned"] = out.get("table_grid_aligned", 0)
    stats["table_grid_align_cols"] = out.get("table_grid_align_cols", 0)

    if ctx.tracer and out["table_grid_tables"]:
        ctx.tracer.stage_event(
            status="ok",
            detail=(
                f"表格网格抽取：{out['table_grid_tables']} 张表 → "
                f"{out['table_grid_candidates']} 个候选，入库 {out['table_grid_accepted']}，"
                f"与既有事实重复 {out['table_grid_dup']}，"
                f"无单位跳过 {out['table_grid_rejected_no_unit']}，"
                f"数值可疑 {out.get('table_grid_suspect', 0)}"
            ),
        )
    return out


def _cross_channel_agree(
    raw: str, text_rec: dict[str, Any] | None, ocr_rec: dict[str, Any] | None
) -> bool:
    """两个通道是否都读到了同一个数值。"""
    if text_rec is None or ocr_rec is None:
        return False
    needle = raw.replace(",", "").strip("()（）")
    in_text = needle in text_rec.get("text", "").replace(",", "")
    in_ocr = needle in ocr_rec.get("text", "").replace(",", "")
    return in_text and in_ocr


def _looks_like_total(quote: str) -> bool:
    """quote 上下文是否出现「合计/总计/小计」—— 这类字段可参与算术校验。"""
    return any(k in quote for k in ("合计", "总计", "小计", "总额"))


def _normalize_vlm_unit(
    value: Decimal, unit: str
) -> tuple[Decimal, str]:
    """把视觉读数的值与单位归一到标准形态。

    与第 1/2 层走**同一套**换算逻辑。实测踩过的坑：VLM 读出「2,557 万股」时，
    若不归一化就入库，事实库里会同时存在 ``25570000 股``（文本层）
    与 ``2557 万股``（VLM），两条记录说的是同一件事却无法比较，
    下游 R5 的口径比对直接失效。
    """
    fam = _unit_family(unit)
    if fam == "money":
        return to_yuan(value, unit), "元"
    if fam == "share":
        share_table = {
            "股": Decimal("1"), "千股": Decimal("1000"),
            "万股": Decimal("10000"), "亿股": Decimal("100000000"),
        }
        return value * share_table[normalize_unit(unit)], "股"
    return value, normalize_unit(unit)


def _ingest_vlm_scans(
    ctx: RunContext,
    documents: dict[str, dict[str, Any]],
    entities: dict[str, str],
    fuzzy: float,
    stats: dict[str, int],
) -> int:
    """把 VLM 整页扫描结果转为事实。

    与旧实现的**关键区别**：直接使用模型给出的 ``metric``（规范指标名），
    而不是把整页读数都塞进笼统的 ``vlm_field_page_key_fields``。

    旧做法的后果（实测）：同一页三个股东的持股数被当成
    「同一 `vlm_field_page_key_fields` 指标的三个候选值」，
    触发 3 条**假冲突**告警，干扰人工复核。

    四重限制，确保视觉读数不污染事实库：
      1. 必须未被 quote 闸门拒绝
      2. ``metric`` 必须是规范指标名（非 ``unknown``）
      3. 单位必须存在（无单位不入库，与 1/2 层同一规则）
      4. 置信度按 ``L3_VLM`` 基础分计算，必然低于文本层
    """
    try:
        scans = read_json(ctx, "channels/vlm_scans.json")
    except FileNotFoundError:
        return 0

    valid_metrics = _load_valid_metrics()
    accepted = 0
    unknown_skipped = 0
    invalid_metric = 0

    for scan in scans:
        doc_id = scan.get("doc_id")
        page = scan.get("page")
        if not (doc_id and page):
            continue

        for est in scan.get("fields", []):
            if est.get("rejected"):
                continue

            metric = str(est.get("metric") or "").strip()
            if not metric or metric == "unknown":
                unknown_skipped += 1
                continue
            if valid_metrics and metric not in valid_metrics:
                # 模型自创了指标名 —— 不入库，但要留痕，因为这说明
                # 提示词里的指标清单需要补充别名
                invalid_metric += 1
                ctx.ledger.reject(
                    "VLM 返回了清单外的指标名（疑似自创）",
                    page=page, metric=metric, raw=est.get("value_raw"),
                )
                continue

            raw = est.get("value_raw")
            quote = est.get("quote")
            unit = normalize_unit(est.get("unit_as_written") or "")
            if not (raw and quote and unit):
                continue

            parsed = parse_amount(str(raw) + unit)
            if parsed is None:
                continue
            value, _ = parsed

            # 视觉读数同样要过锚点校验（双重保险）
            try:
                span, match_status, _ = ctx.store.verify_and_anchor(
                    doc_id=doc_id,
                    page=int(page),
                    quote=str(quote),
                    source_layer=SourceLayer.L3_VLM,
                    fuzzy_threshold=fuzzy,
                )
            except QuoteVerificationError as exc:
                ctx.ledger.reject("VLM 事实锚点校验失败", error=str(exc))
                continue

            try:
                value_std, target_unit = _normalize_vlm_unit(value, unit)
            except Exception as exc:
                ctx.ledger.reject(
                    "VLM 事实单位换算失败", page=page, raw=str(raw), unit=unit,
                    detail=str(exc),
                )
                continue

            entity = (
                str(est.get("entity_hint")).strip()
                if est.get("entity_hint")
                else entities.get(doc_id, "UNKNOWN")
            )

            breakdown = build_confidence(
                source_layer=SourceLayer.L3_VLM,
                quote_match=match_status,
                vlm_confirmed=True,
            )
            needs_review = breakdown.score < REVIEW_THRESHOLD
            if needs_review:
                stats["needs_review"] = stats.get("needs_review", 0) + 1

            fact = Fact(
                fact_id=derive_id("fct_vlm", doc_id, metric, str(page), str(raw), unit),
                doc_id=doc_id,
                entity=entity,
                metric=metric,
                value=value_std,
                unit=target_unit,
                raw_text=str(raw),
                period=infer_period(str(quote), default=doc_periods_of(ctx, doc_id)),
                period_type=infer_period_type(str(quote), metric),
                statement_scope=infer_statement_scope(str(quote)),
                profit_basis=infer_profit_basis("", metric),
                source_layer=SourceLayer.L3_VLM,
                evidence_span_ids=[span.span_id],
                channel_readings=[
                    ChannelReading(
                        channel="vlm",
                        raw_text=str(raw),
                        page=int(page),
                        confidence=float(scan.get("self_confidence", 0.7)),
                    )
                ],
                confidence=breakdown.score,
                confidence_breakdown=breakdown,
                needs_review=needs_review,
                review_reason=(
                    f"视觉读数（置信度 {breakdown.score:.2f}）"
                    + ("，低于阈值需人工复核" if needs_review else "，指标归属由 VLM 判定")
                ),
                extractor="vlm_scan@1.0.0",
                extracted_at=ctx.build_stamp(),
            )
            ctx.ledger.add(fact)
            accepted += 1

    stats["vlm_unknown_metric_skipped"] = unknown_skipped
    stats["vlm_invalid_metric"] = invalid_metric
    return accepted


_METRIC_CACHE: set[str] | None = None


def _load_valid_metrics() -> set[str]:
    """加载规范指标名集合（用于校验 VLM 的指标归属）。"""
    global _METRIC_CACHE
    if _METRIC_CACHE is None:
        try:
            from agent.config import load_config

            cfg = load_config("metrics")
            _METRIC_CACHE = {m["canonical"] for m in cfg.get("metrics", [])}
        except Exception:
            _METRIC_CACHE = set()
    return _METRIC_CACHE


def doc_periods_of(ctx: RunContext, doc_id: str) -> str:
    """取某文档的报告期（用于 VLM 事实的期间回退）。"""
    try:
        docs = read_json(ctx, "facts/documents.json")
    except FileNotFoundError:
        return "unknown"
    for d in docs:
        if d.get("doc_id") == doc_id:
            return d.get("document_period", "unknown")
    return "unknown"


__all__ = [
    "run",
    "find_metric_occurrences",
    "infer_period",
    "infer_period_type",
    "infer_profit_basis",
    "infer_statement_scope",
    "infer_entity",
]
