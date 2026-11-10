"""R5 研究报告纠错核查管线。

流程：
    data/drafts/*.md|docx|txt  研究报告草稿
    facts/facts.json           事实库（数字真源）
            ↓
    E1 单位 / E2 倍数 / E3 口径 / E4 引用 / E5 算术
            ↓
    audit.json（结构化） + audit_report.md（人读） + revision_table.xlsx（修订对照）
            ↓
    若存在黄金测试集标注 → 自动计算精确率 / 召回率 / 覆盖率

对外部读者的价值：这条管线不需要任何模型调用即可完成核查 ——
「确定性比对」既是准确率的保证，也是可复现性的保证。
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from agent.evidence.models import (
    AuditMetrics,
    AuditResult,
    ErrorType,
    FactSet,
    Finding,
    Severity,
    utcnow,
)
from agent.evidence.store import EvidenceStore, FactLedger
from agent.runtime.loop import RunContext
from domain.tools.audit_rules import (
    MultipleCheckInput,
    extract_claims,
    load_metric_aliases,
    run_all_checks,
    scope_ledger_to_draft,
)
from pipelines.r1_extract.common import read_json, record_output, write_json

SUPPORTED_DRAFT_SUFFIXES = {".md", ".markdown", ".txt", ".text"}


# ==========================================================================
# 草稿加载
# ==========================================================================


def drafts_dir(ctx: RunContext) -> Path:
    data_dir = Path(ctx.configs.get("data_dir", "data"))
    return data_dir / "drafts"


def discover_drafts(ctx: RunContext) -> list[Path]:
    root = drafts_dir(ctx)
    if not root.exists():
        return []
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_DRAFT_SUFFIXES
    )


def load_draft(path: Path, *, guard: Any = None) -> str:
    """读取研究报告草稿。

    当前支持纯文本/Markdown。若草稿是 PDF/Word，需先转换为文本
    （PDF 走 ``domain.tools.document``，Word 走 python-docx）。
    不支持时**显式报错**，不返回空串 —— 空串会让核查静默通过。

    ``guard`` 传了就经它读：**草稿是被核查的原文**，它读进来的是哪一版
    （sha256）必须可查，否则「改了草稿重跑、结论不同」会被当成模型不稳定。
    """
    if path.suffix.lower() in SUPPORTED_DRAFT_SUFFIXES:
        if guard is not None:
            return guard.read_text(path)
        return path.read_text(encoding="utf-8")
    raise ValueError(
        f"暂不支持的草稿格式 {path.suffix}。请转换为 .md 或 .txt 后重试。"
    )


# ==========================================================================
# 核查主流程
# ==========================================================================


def run(ctx: RunContext) -> dict[str, Any]:
    # ---------- 载入事实库 ----------
    import json

    from agent.trace.tracer import file_sha256

    facts_path = ctx.require_facts()
    if ctx.manifest is not None:
        ctx.manifest.input_files["facts/facts.json"] = file_sha256(facts_path)
    fs = FactSet.model_validate(ctx.guard().read_json(facts_path))

    store = EvidenceStore()
    for span in fs.evidence:
        store.add_span(span)
    ledger = FactLedger(store, run_id=fs.run_id)
    for f in fs.facts:
        ledger.add(f)

    # ---------- 构建倍数核查参考数据 ----------
    # 注意：参考值**与草稿无关**（全部从事实库独立重算），因此只建一次。
    ref = _build_multiple_ref(ledger)

    # 文档名映射：股票代码常常只在文件名里（`000722_湖南发展_…pdf`），
    # 而事实的 entity 用的是公司全名 —— 作用域判断两者都要看。
    #
    # ⚠️ 必须从**上游事实库所在目录**读，不能从本 run 目录读。
    #   实测缺陷（2026-09，直接造成 3 条确认级误报）：
    #   `agent run r5_audit --facts-run annual23` 时，本 run 目录下
    #   只有软链/复制的 `facts/facts.json`，**没有 `documents.json`**；
    #   于是这里静默拿到空映射 → `scope_ledger_to_draft` 对
    #   「000722」「002716」两份草稿都收窄失败（它们的股票代码只出现在
    #   文档名里，事实的 entity 是全称）→ 退化成**整个事实库跨公司比对** →
    #   E5 拿中国石油的两期净利润去重算湖南发展草稿的同比，
    #   报出「应改为 272.00%」这种确认级误报。
    #   601857 那份草稿反而正常 —— 因为它的 entity 恰好就是代码本身，
    #   不依赖文档名映射，所以这个缺陷此前一直被掩盖着。
    doc_names: dict[str, str] = {}
    try:
        upstream = ctx.facts_run_dir() / "facts" / "documents.json"
        if not upstream.is_file():
            upstream = ctx.facts_run_dir() / "documents.json"
        if upstream.is_file():
            for d in ctx.guard().read_json(upstream) or []:
                if d.get("doc_id"):
                    doc_names[d["doc_id"]] = str(d.get("filename") or "")
    except (OSError, ValueError):
        pass
    if not doc_names:
        try:
            for d in read_json(ctx, "facts/documents.json") or []:
                if d.get("doc_id"):
                    doc_names[d["doc_id"]] = str(d.get("filename") or "")
        except FileNotFoundError:
            pass
    if not doc_names and ctx.tracer:
        ctx.tracer.stage_event(
            status="warn",
            detail=(
                "读不到上游 documents.json，草稿作用域无法按文档名收窄 —— "
                "只有 entity 恰好等于股票代码的草稿能收窄，其余会退化为跨公司比对，"
                "可能产生大量误报。请检查 --facts-run 指向的 run 是否含 facts/documents.json"
            ),
        )

    # ---------- 载入草稿 ----------
    # ★ 遍历**全部**草稿，而不是只取第一份。
    #
    #   旧实现 `drafts[0]` 让"覆盖率、准确率"这些指标完全建立在一份草稿上 ——
    #   数据结构里放着 3 份、只核 1 份，指标看起来正常但毫无意义。
    #   这是实测发现的：评测集有 3 份草稿，审计只出了 1 份的结果。
    drafts = discover_drafts(ctx)
    if not drafts:
        # 没有草稿时，用事实库自身合成一份「含已知错误」的演示草稿，
        # 以便现场演示与自测。**产物中明确标注这是合成草稿。**
        demo_text = _synthesize_demo_draft(ledger)
        demo_id = "synthetic_demo_draft"
        if ctx.tracer:
            ctx.tracer.stage_event(
                status="warn",
                detail="data/drafts/ 为空，使用自动合成的演示草稿（含注入的已知错误）",
            )
        drafts = [None]
    else:
        demo_text = demo_id = None

    per_draft: list[dict[str, Any]] = []
    findings: list[Any] = []
    notes: list[str] = []
    checked_total = 0
    evaluated: list[tuple[str, list[Any], list[dict[str, Any]], str]] = []
    first_text = ""

    from agent.trace.tracer import file_sha256

    for draft_path in drafts:
        if draft_path is None:
            one_id, one_text = str(demo_id), str(demo_text)
        else:
            one_text = load_draft(draft_path, guard=ctx.guard())
            one_id = draft_path.stem
            if ctx.manifest is not None:
                ctx.manifest.input_files[f"drafts/{draft_path.name}"] = file_sha256(draft_path)
        if not first_text:
            first_text = one_text

        # ★ 每份草稿各自收窄作用域：草稿谈哪家公司，就只用哪家公司的事实在比对。
        #   不收窄的话，草稿里中国石油的「营业收入 28,644.69 亿元」会去跟
        #   湖南发展子公司表的 349,165,200 元比，报出"相差 10⁴ 量级"的
        #   确认级误报 —— 实测这一条贡献了 6 条假阳性中的绝大多数。
        scoped, scope_note = scope_ledger_to_draft(one_text, ledger, doc_names=doc_names)
        notes.append(f"[{one_id}] {scope_note}")
        one = run_all_checks(one_text, scoped, multiple_ref=ref)
        checked_total += one.checked
        notes.extend(one.notes)
        for f in one.findings:
            # 每条发现都要能指回它出自哪份草稿 —— 多草稿时这是必需的定位信息
            f.draft_id = one_id
        findings.extend(one.findings)

        labels = _load_golden_labels(ctx, one_id)
        if labels is not None:
            evaluated.append((one_id, one.findings, labels, one_text))

        per_draft.append(
            {
                "draft_id": one_id,
                "claims_checked": one.checked,
                "findings": len(one.findings),
                "by_severity": _count_by(one.findings, lambda x: x.severity.value),
                "by_type": _count_by(one.findings, lambda x: x.error_type.value),
            }
        )

    draft_id = per_draft[0]["draft_id"] if len(per_draft) == 1 else f"{len(per_draft)} 份草稿"
    draft_text = first_text

    # ---------- 组装结果 ----------
    audit = AuditResult(
        run_id=ctx.run_id,
        draft_doc_id=draft_id,
        findings=findings,
        checked_at=ctx.build_stamp(),
    )

    # ---------- 黄金测试集评测（逐草稿评、再汇总） ----------
    if evaluated:
        audit.metrics = _evaluate_all(evaluated)
        # 多草稿时额外给出逐份明细，便于定位是哪一份拉低了指标
        if len(evaluated) > 1:
            audit.per_draft_metrics = []
            for d_id, f_list, lab, d_text in evaluated:
                one = evaluate_findings(f_list, lab, d_text).model_dump(mode="json")
                one["draft_id"] = d_id
                audit.per_draft_metrics.append(one)

    # 多草稿时，报告与批注 HTML 只针对第一份（人工阅读用），
    # 但 **claims.json 覆盖全部草稿** —— 它是"读了哪些主张"的证据。
    all_claims: list[dict[str, Any]] = []
    for draft_path in drafts:
        one_text = (str(demo_text) if draft_path is None
                    else load_draft(draft_path, guard=ctx.guard()))
        one_id = str(demo_id) if draft_path is None else draft_path.stem
        for c in extract_claims(one_text, alias_map=load_metric_aliases()):
            d = c.to_dict()
            d["draft_id"] = one_id
            all_claims.append(d)

    write_json(ctx, "audit/audit.json", audit.model_dump(mode="json"))
    write_json(ctx, "audit/claims.json", all_claims)
    ctx.guard().write_text(ctx.audit_dir / "draft_used.md", draft_text)
    if len(drafts) > 1:
        write_json(ctx, "audit/per_draft.json", per_draft)

    md = build_audit_report(audit, notes, draft_text)
    ctx.guard().write_text(ctx.audit_dir / "audit_report.md", md)

    # 这两份由 openpyxl / 手写 HTML 落盘，落完各登记一次（见 common.record_output）
    excel_ok = _export_revision_table(audit, draft_text, str(ctx.audit_dir / "revision_table.xlsx"))
    if excel_ok:
        record_output(ctx, ctx.audit_dir / "revision_table.xlsx")
    html_ok = _export_annotated_html(audit, draft_text, str(ctx.audit_dir / "annotated_draft.html"))
    if html_ok:
        record_output(ctx, ctx.audit_dir / "annotated_draft.html")

    summary = {
        "draft": draft_id,
        "drafts_checked": len(per_draft),
        "per_draft": per_draft,
        "claims_checked": checked_total,
        "findings": len(findings),
        "by_severity": _count_by(audit.findings, lambda f: f.severity.value),
        "by_type": _count_by(audit.findings, lambda f: f.error_type.value),
        "metrics": audit.metrics.model_dump(mode="json") if audit.metrics else None,
        "excel": excel_ok,
        "html": html_ok,
    }
    write_json(ctx, "audit/audit_summary.json", summary)

    if ctx.tracer:
        ctx.tracer.stage_event(status="ok", detail=str(summary["by_type"]))

    return summary


# ==========================================================================
# 参考数据构建
# ==========================================================================


def _build_multiple_ref(ledger: FactLedger) -> MultipleCheckInput:
    """从事实库构建估值倍数核查所需的参考值。

    这里刻意**从事实库重算**而非信任报告中的数字 ——
    E2 类核查的全部价值就在于「独立重算后比对」。

    同时记录每个参考值的来源 fact_id：CONFIRMED 级发现必须携带来源证据，
    否则契约会（正确地）拒绝该发现。
    """
    ids: dict[str, str] = {}

    def v(metric: str) -> Decimal | None:
        fact = ledger.best(metric=metric)
        if fact is None:
            return None
        ids[metric] = fact.fact_id
        return fact.value

    net_profit = v("net_profit_parent")
    shares = v("shares_outstanding")
    equity = v("equity_parent") or v("equity_total")

    eps_ttm = (net_profit / shares) if (net_profit and shares) else None
    bvps = (equity / shares) if (equity and shares) else None
    price = v("trade_avg_price")
    market_cap = v("market_cap_total")

    return MultipleCheckInput(
        price=price,
        eps_ttm=eps_ttm,
        bvps=bvps,
        market_cap_total=market_cap,
        net_profit_parent=net_profit,
        net_profit_parent_ex_nonrec=v("net_profit_parent_ex_nonrec"),
        source_fact_ids=ids,
    )


# ==========================================================================
# 黄金测试集评测
# ==========================================================================


def _load_golden_labels(ctx: RunContext, draft_id: str) -> list[dict[str, Any]] | None:
    """载入人工标注的黄金测试集。

    期望文件：``data/golden/<draft_id>.labels.json``::

        [
          {"error_id": "G-001", "type": "E1_unit",
           "char_range": [1024, 1067], "quote": "营业收入约 1.2 亿元",
           "is_real_error": true, "note": "单位错误：应为 1.20 亿元"}
        ]

    ``is_real_error=false`` 的条目是**陷阱项**（看起来像错但实际正确），
    用于检验误报率 —— 这是「误报控制」评分项的直接证据。
    """
    golden_dir = Path(ctx.configs.get("paths", {}).get("golden") or "data/golden")
    path = golden_dir / f"{draft_id}.labels.json"
    if not path.exists():
        alt = golden_dir / "labels.json"
        if not alt.exists():
            return None
        path = alt
    try:
        return ctx.guard().read_json(path)
    except json.JSONDecodeError:
        return None


def _evaluate_all(
    evaluated: list[tuple[str, list[Finding], list[dict[str, Any]], str]],
) -> AuditMetrics:
    """把逐草稿的评测**汇总**成整体指标。

    汇总方式是"先合并分子分母再算比率"，而不是"对各草稿的比率求平均" ——
    后者会让一份只有 1 条错的草稿与一份有 10 条错的草稿权重相同，
    指标会被小样本草稿主导。
    """
    real = trap = tp = fp = fn = fp_trap = 0
    cov_num = cov_den = 0
    for _d_id, findings, labels, draft_text in evaluated:
        m = evaluate_findings(findings, labels, draft_text)
        real += m.true_positive + m.false_negative
        tp += m.true_positive
        fp += m.false_positive
        fp_trap += m.false_positive_on_trap
        fn += m.false_negative
        trap += sum(1 for l in labels if not l.get("is_real_error", True))
        cov_den += 1
        if m.coverage is not None:
            cov_num += m.coverage

    precision = (tp / (tp + fp)) if (tp + fp) else 0.0
    recall = (tp / (tp + fn)) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else None
    return AuditMetrics(
        true_positive=tp,
        false_positive=fp,
        false_positive_on_trap=fp_trap,
        false_negative=fn,
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1=round(f1, 4) if f1 is not None else None,
        coverage=round(cov_num / cov_den, 4) if cov_den else None,
        drafts_evaluated=len(evaluated),
        traps_evaluated=trap,
        evaluated_at=utcnow(),
    )


def evaluate_findings(
    findings: list[Finding],
    labels: list[dict[str, Any]],
    draft_text: str,
) -> AuditMetrics:
    """对黄金测试集计算精确率 / 召回率 / 覆盖率。

    匹配规则：**字符区间重叠**即视为命中同一条错误。
    这样即使规则的措辞与人工标注不同，只要定位到同一处就算命中 ——
    是评测「错误识别覆盖率」而不是「文字相似度」。
    """
    real = [l for l in labels if l.get("is_real_error", True)]
    traps = [l for l in labels if not l.get("is_real_error", True)]

    matched_labels: set[int] = set()
    tp = 0
    fp = 0
    fp_on_trap = 0

    for f in findings:
        cr = f.location.char_range
        hit = None
        for i, l in enumerate(real):
            lr = l.get("char_range")
            if cr and lr and _overlap(cr, tuple(lr)):
                hit = i
                break
            if not cr and l.get("quote") and l["quote"] in f.location.quote:
                hit = i
                break
        if hit is not None:
            matched_labels.add(hit)
            tp += 1
            continue

        # ★ 一条发现**最多算一个假正例**。
        #
        #   实测缺陷（2026-09）：这里原先写成「`fp += 1`，另外再统计命中陷阱项的
        #   `trap_fp`，最后 `fp_total = fp + trap_fp`」——
        #   而一条**报在陷阱项上**的发现必然也没匹配上真错误，
        #   于是它被计了两次。实测 3 份草稿共 8 条发现，
        #   指标却报出 FP=9：**假正例比发现总数还多**，指标自相矛盾。
        #   现在只在此处加一，并单独记录"其中落在陷阱项上"的数量，
        #   让"误报总量"与"误报里有多少是被刻意设计的陷阱钓到的"两个问题都能回答。
        fp += 1
        for t in traps:
            lr = t.get("char_range")
            if cr and lr and _overlap(cr, tuple(lr)):
                fp_on_trap += 1
                break

    fn = len(real) - len(matched_labels)
    fp_total = fp

    # 自洽性检查：假正例不可能多于发现总数。
    # 这类"指标自身矛盾"的错误比数字偏差更难发现，因此直接断言。
    assert fp_total <= len(findings), (
        f"指标不自洽：假正例 {fp_total} 多于发现总数 {len(findings)}"
        f"（草案 {len(real)} 条、陷阱 {len(traps)} 条）"
    )

    precision = tp / (tp + fp_total) if (tp + fp_total) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision and recall and (precision + recall) > 0
        else None
    )

    # 错误类型覆盖率：标注中出现过的类型，被检出多少种
    labeled_types = {l["type"] for l in real if l.get("type")}
    detected_types = {f.error_type.value for f in findings}
    coverage = (
        len(labeled_types & detected_types) / len(labeled_types) if labeled_types else None
    )

    return AuditMetrics(
        true_positive=tp,
        false_positive=fp_total,
        false_negative=fn,
        false_positive_on_trap=fp_on_trap,
        precision=round(precision, 4) if precision is not None else None,
        recall=round(recall, 4) if recall is not None else None,
        f1=round(f1, 4) if f1 is not None else None,
        coverage=round(coverage, 4) if coverage is not None else None,
    )


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return not (a[1] <= b[0] or b[1] <= a[0])


# ==========================================================================
# 演示草稿合成
# ==========================================================================

_DEMO_TEMPLATE = """# {entity} 投资价值分析报告（演示草稿）

## 一、公司概况

{entity}（证券代码：{code}）主营业务稳健，报告期内经营情况良好。

## 二、经营业绩

报告期内，公司实现营业收入 {revenue_wrong} 亿元，同比增长 {yoy_wrong}%；
实现归属于母公司股东的净利润 {profit_wrong} 万元。

> 上述数据引自公司 {period} 年度报告。

## 三、盈利质量

公司扣除非经常性损益后归属于母公司股东的净利润为 {profit} 万元，
非经常性损益占利润总额比重较高，需关注其可持续性。

## 四、估值分析

截至报告日，公司市盈率（动态 PE）为 {pe_wrong} 倍，
市净率为 {pb} 倍，处于行业中等水平。
可比公司平均市盈率为 15.8 倍。

## 五、风险提示

1. 原材料价格波动风险；
2. 下游需求不及预期风险；
3. 控股股东质押比例较高，需关注质押风险。{pledge_line}
"""


def _synthesize_demo_draft(ledger: FactLedger) -> str:
    """合成一份**故意含有已知错误**的演示草稿。

    用途：现场演示与自动化测试。注入的错误覆盖 E1/E2/E3/E5 四类，
    全部为确定性可验证的错误（不是"可能有问题"）。
    """
    entity = "示例股份有限公司"
    for f in ledger.all():
        if f.entity and f.entity != "UNKNOWN":
            entity = f.entity
            break

    revenue = ledger.value_of(metric="revenue")
    profit = ledger.value_of(metric="net_profit_parent")
    shares = ledger.value_of(metric="shares_outstanding")

    # 注入错误 1（E1 单位）：把真实收入错写为"亿元"，数值保留万元量级特征
    if revenue:
        revenue_yi = revenue / Decimal("1e8")
        revenue_wrong = f"{revenue_yi:.2f}"
    else:
        revenue_wrong = "12.00"

    # 注入错误 2（E1 单位）：净利润用"万元"但数值按"元"填
    profit_wrong = f"{profit:.2f}" if profit else "9600.00"

    # 注入错误 3（E5 算术）：同比增速故意算错
    yoy_wrong = "3.20"

    # 注入错误 4（E2 倍数口径）：PE 用错误数值
    pe_wrong = "45.30"
    pb = "1.62"

    pledge_line = ""
    for f in ledger.all():
        if f.metric.startswith("pledge"):
            pledge_line = f"\n4. 控股股东质押情况：{f.raw_text}（来源：{f.evidence_span_ids[0]}）。"
            break

    return _DEMO_TEMPLATE.format(
        entity=entity,
        code="000000",
        revenue_wrong=revenue_wrong,
        profit_wrong=profit_wrong,
        profit=f"{profit / Decimal('10000'):.2f}" if profit else "9600.00",
        yoy_wrong=yoy_wrong,
        pe_wrong=pe_wrong,
        pb=pb,
        period="2025",
        pledge_line=pledge_line,
    )


# ==========================================================================
# 报告渲染
# ==========================================================================


def group_notes(notes: list[str]) -> list[str]:
    """把「同一类、只有参数不同」的过程记录并成一行。

    实测一份草稿会产出 7 行「跳过派生指标主张（位置 N，单位 U）：<完全相同的
    一句话>」，逐行罗列会把过程说明变成刷屏，读者反而看不出到底跳过了什么。

    只合并「前缀相同 + 括号参数不同 + 冒号后逐字相同」的记录，其余原样保留 ——
    宁可少合并，也不要把结论不同的记录混成一条。
    """
    head_re = re.compile(
        r"^(?P<head>[^（:：]*?)\s*[（(](?P<param>[^）)]*)[）)]\s*[:：]\s*(?P<tail>.+)$"
    )

    # key -> (首次出现的下标, 全部参数)
    grouped: dict[tuple[str, str], list[Any]] = {}
    passthrough: list[tuple[int, str]] = []
    for idx, n in enumerate(notes):
        m = head_re.match(n)
        if not m:
            passthrough.append((idx, n))
            continue
        key = (m.group("head"), m.group("tail"))
        slot = grouped.setdefault(key, [idx, []])
        slot[1].append(m.group("param"))

    out: list[tuple[int, str]] = list(passthrough)
    for (head, tail), (idx, params) in grouped.items():
        if len(params) == 1:
            out.append((idx, f"{head}（{params[0]}）：{tail}"))
        else:
            joined = "；".join(params)
            out.append((idx, f"{head} 共 {len(params)} 条：{tail}（{joined}）"))
    return [n for _, n in sorted(out, key=lambda kv: kv[0])]


def build_audit_report(audit: AuditResult, notes: list[str], draft_text: str) -> str:
    # 严重度用文字标注。原先用彩色圆点（红/黄/蓝），在终端、PDF 和
    # 不同字体下都可能掉字形，读起来也更像监控面板而不是核查结论。
    sev_label = {
        Severity.CONFIRMED: "确认", Severity.SUSPECTED: "疑似", Severity.ADVISORY: "提示",
    }
    type_label = {
        ErrorType.E1_UNIT: "单位错误",
        ErrorType.E2_MULTIPLE: "估值倍数错误",
        ErrorType.E3_CALIBER: "统计口径错误",
        ErrorType.E4_CITATION: "引用/来源问题",
        ErrorType.E5_ARITHMETIC: "算术一致性错误",
        ErrorType.E6_QUALITATIVE: "定性表述矛盾",
    }

    def quote_line(text: str) -> str:
        """把引文压成单行，并剥掉行首的 Markdown 标记。

        引文是原样嵌进列表项的，而草稿本身是 Markdown：里面的换行会把列表
        断成好几段，行首的 `#` 会被解析成真标题 —— 报告正文里于是凭空多出
        一个不属于报告结构的二级标题，`>` 也会多出一个引用块。压成单行后，
        `#`/`>` 不在行首，就只是普通字符了。
        """
        flat = re.sub(r"\s+", " ", text).strip()
        return re.sub(r"^[#>\s]+", "", flat)

    # 章节号按实际输出递增。原先写死「一/二/三…」，而「评测指标」一节
    # 只在携带人工标注时输出，于是没有标注的运行会从「一、」直接跳到
    # 「三、」。编号是给读者定位用的，跳号比不编号更糟。
    sec = iter(["一", "二", "三", "四", "五", "六"])

    def head(title: str) -> str:
        return f"## {next(sec)}、{title}"

    lines = [
        f"# 研究报告纠错核查报告",
        "",
        f"> 核查对象：`{audit.draft_doc_id}` ｜ 运行 ID：`{audit.run_id}`",
        f"> 核查时间：{audit.checked_at.isoformat()[:19]}",
        "> 核查基准：R1 三层解析引擎构建的事实库（每条数字可回溯至原文）",
        "",
        "---",
        "",
        head("核查概况"),
        "",
        f"- 发现问题总数：{len(audit.findings)}",
    ]
    for sev in (Severity.CONFIRMED, Severity.SUSPECTED, Severity.ADVISORY):
        cnt = len(audit.by_severity(sev))
        lines.append(f"  - {sev_label[sev]}（{sev.value}）：{cnt} 条")
    lines.append("")
    lines.append("| 错误类型 | 数量 |")
    lines.append("|---|---|")
    for et in ErrorType:
        cnt = len(audit.by_type(et))
        if cnt:
            lines.append(f"| {type_label.get(et, et.value)} | {cnt} |")
    lines.append("")

    if audit.metrics:
        m = audit.metrics
        lines += [
            head("评测指标（对人工标注黄金测试集）"),
            "",
            "| 指标 | 数值 |",
            "|---|---|",
            f"| 真正例 TP | {m.true_positive} |",
            f"| 假正例 FP（误报） | {m.false_positive} |",
            f"| ↳ 其中落在陷阱项上 | {m.false_positive_on_trap} |",
            f"| 假负例 FN（漏报） | {m.false_negative} |",
            f"| 精确率 Precision | {m.precision if m.precision is not None else 'n/a'} |",
            f"| 召回率 Recall | {m.recall if m.recall is not None else 'n/a'} |",
            f"| F1 | {m.f1 if m.f1 is not None else 'n/a'} |",
            f"| 错误类型覆盖率 | {m.coverage if m.coverage is not None else 'n/a'} |",
            "",
        ]

    lines += [
        head("问题清单（按严重度与置信度排序）"),
        "",
    ]

    if not audit.findings:
        lines.append("（未发现问题）")
    for f in audit.findings:
        lines += [
            f"### `{f.finding_id}` "
            f"{type_label.get(f.error_type, f.error_type.value)}"
            f"（{sev_label[f.severity]}）",
            "",
            f"- 位置：{f.location.section or '（未定位章节）'}"
            + (f" 字符 {f.location.char_range}" if f.location.char_range else ""),
            f"- 原文：「{quote_line(f.location.quote)}」",
            f"- 报告值：`{f.reported_value}`",
        ]
        if f.correct_value:
            lines.append(f"- 正确值：`{f.correct_value}`")
        if f.caliber:
            lines.append(f"- 口径：`{f.caliber}`")
        lines.append(f"- 判定理由：{f.reason}")
        lines.append(f"- 修改建议：{f.suggestion}")
        lines.append(f"- 置信度：{f.confidence:.2f} ｜ 检出规则：`{f.detector}`")
        if f.source_evidence:
            lines.append(f"- 来源：{', '.join(f'`{s}`' for s in f.source_evidence)}")
        lines.append("")

    if notes:
        lines += [head("核查过程说明"), ""]
        lines += [f"- {n}" for n in group_notes(notes)]
        lines.append("")

    lines += [
        head("核查方法"),
        "",
        "核查走确定性比对，不依赖模型当时的状态：",
        "",
        "1. 事实库由 R1 三层解析引擎从原始材料构建，每条数字带原文锚点；",
        "2. 从草稿中抽取全部数值主张，连同上下文与指标归属；",
        "3. 按 E1–E5 五类规则独立重算后比对；",
        "4. 无法确定性判定的降级为「疑似」，不强行下定论。",
        "",
        "每条结论都给出正确值、判定理由与证据来源，便于逐条复核。",
        "",
        "---",
        "",
        "本报告由 `pipelines.r5_audit.check` 生成。",
        "",
    ]
    return "\n".join(lines)


def _export_revision_table(audit: AuditResult, draft_text: str, path: str) -> bool:
    """导出「原文 / 问题 / 建议 / 依据」四列对照表 —— 可直接用于改稿。

    时间戳会被统一（见 :mod:`domain.tools.xlsx`），保证可复现。
    """
    from domain.tools.xlsx import write_xlsx

    rows = []
    for f in audit.findings:
        cr = f.location.char_range
        snippet = draft_text[max(0, cr[0] - 40) : cr[1] + 40] if cr else f.location.quote
        rows.append(
            {
                "编号": f.finding_id,
                "严重度": f.severity.value,
                "类型": f.error_type.value,
                "所在章节": f.location.section or "",
                "原文上下文": snippet.replace("\n", " "),
                "报告值": f.reported_value or "",
                "正确值": f.correct_value or "",
                "问题说明": f.reason,
                "修改建议": f.suggestion,
                "口径": f.caliber or "",
                "依据来源": ", ".join(f.source_evidence),
                "置信度": f.confidence,
                "检出规则": f.detector,
            }
        )
    return write_xlsx({"核查发现": rows}, path)


def _export_annotated_html(audit: AuditResult, draft_text: str, path: str) -> bool:
    """导出批注版草稿：在原文中高亮问题位置，悬停显示修改建议。"""
    try:
        marks: list[tuple[int, int, Finding]] = []
        for f in audit.findings:
            cr = f.location.char_range
            if cr:
                marks.append((cr[0], cr[1], f))
        marks.sort(key=lambda m: m[0], reverse=True)

        body = draft_text
        for start, end, f in marks:
            color = {
                Severity.CONFIRMED: "#ffd7d5",
                Severity.SUSPECTED: "#fff3cd",
                Severity.ADVISORY: "#d7e9ff",
            }[f.severity]
            tip = (
                f"{f.finding_id} · {f.error_type.value}\n"
                f"报告值：{f.reported_value}\n正确值：{f.correct_value or '—'}\n"
                f"建议：{f.suggestion}"
            )
            body = (
                body[:start]
                + f'<mark style="background:{color}" title="{html_escape(tip)}">'
                + body[start:end]
                + "</mark>"
                + body[end:]
            )

        doc = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>批注版草稿 — {audit.draft_doc_id}</title>
<style>body{{max-width:860px;margin:32px auto;padding:0 20px;line-height:1.9;
font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:#1a1a1a}}
pre{{white-space:pre-wrap;font-family:inherit}}mark{{cursor:help;padding:1px 2px;border-radius:2px}}
.legend{{background:#f6f8fa;padding:12px 16px;border-radius:6px;font-size:13px;margin-bottom:20px}}
</style></head><body>
<div class="legend"><b>批注版草稿</b> — 共 {len(marks)} 处标注。
<span style="background:#ffd7d5">确认错误</span>
<span style="background:#fff3cd">疑似问题</span>
<span style="background:#d7e9ff">规范性提示</span>
（鼠标悬停查看详情）</div>
<pre>{body}</pre></body></html>"""
        Path(path).write_text(doc, encoding="utf-8", newline="\n")
        return True
    except Exception:
        return False


def html_escape(s: str) -> str:
    import html

    return html.escape(s).replace("\n", "&#10;")


def _count_by(items: list[Finding], key) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        k = key(it)
        out[k] = out.get(k, 0) + 1
    return out


__all__ = [
    "run",
    "load_draft",
    "discover_drafts",
    "evaluate_findings",
    "build_audit_report",
]
