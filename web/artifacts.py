"""产物读取层 —— 把 ``runs/<run_id>/`` 下的文件变成视图模型。

设计原则：
    * **只读**。Web 层绝不修改产物 —— 演示台与命令行必须看到同一份数据。
    * **容错**。缺哪份产物就返回空视图并给出提示，而不是 500。
      现场演示时最怕的不是数据少，而是页面崩。
    * **不重算**。所有数字直接取自产物文件，不做任何二次加工 ——
      否则演示看到的结果可能与评审复现的不一致。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from domain.tools.tables import describe_arithmetic


class ArtifactError(RuntimeError):
    """产物缺失或损坏。Web 层据此给出友好提示而非 500。"""


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_ROOT = PROJECT_ROOT / "runs"


def group_thousands(value: str) -> str:
    """给一个数值的**整数部分**加千分位分组，**不做舍入、不做单位换算**。

    非纯数值（``—``、空串、``1,720.21`` 这种原文已带分隔符的）原样返回 ——
    认不出来就不要猜，显示错的分组比不显示分组更糟。

    抽成模块级函数是因为它有两个调用点：事实库的 ``value_display``（展示站）
    与控制台的可比公司表。两处必须按同一规则分组，否则同一个市值在两张页面上
    的位数对不齐 —— 而那正是这个函数存在的理由。
    """
    m = re.fullmatch(r"(-?)(\d+)(\.\d+)?", (value or "").strip())
    if not m:
        return value
    sign, int_part, dec = m.groups()
    return f"{sign}{int(int_part):,}{dec or ''}"


# ==========================================================================
# 视图模型
# ==========================================================================


@dataclass
class EvidenceView:
    """一条证据锚点（用于原文高亮）。"""

    span_id: str
    doc_id: str
    page: int
    quote: str
    bbox: dict[str, float] | None = None
    source_layer: str = ""


@dataclass
class TableCellView:
    """表格里的一个格子 —— 含它的裁决结论。"""

    row: int
    col: int
    text: str
    verdict: str = ""
    """``""`` 未裁决 / agree / normalized / corrected / rejected / unreadable / error"""
    original: str = ""
    vlm_text: str = ""
    confidence: float | None = None
    quote: str = ""
    note: str = ""

    @property
    def changed(self) -> bool:
        return self.verdict in ("corrected", "normalized")

    @property
    def css(self) -> str:
        return {
            "corrected": "cell-fix",
            "normalized": "cell-norm",
            "rejected": "cell-rej",
            "error": "cell-rej",
            "agree": "cell-ok",
        }.get(self.verdict, "")

    @property
    def verdict_label(self) -> str:
        return {
            "agree": "一致",
            "normalized": "格式归一",
            "corrected": "改值",
            "rejected": "未采纳",
            "unreadable": "读不出",
            "error": "出错",
        }.get(self.verdict, "未裁决")

    @property
    def confidence_pct(self) -> str:
        return "—" if self.confidence is None else f"{self.confidence * 100:.0f}%"


@dataclass
class TableView:
    """一张表格 —— 页面上要展示的全部信息（含逐格裁决）。"""

    index: int
    method: str
    n_rows: int
    n_cols: int
    has_rules: bool
    rows: list[list[TableCellView]] = field(default_factory=list)
    arith_ok: bool = True
    arith_checked: int = 0
    arith_failures: list[dict[str, Any]] = field(default_factory=list)
    arith_soft: int = 0
    trigger: str = ""
    cells_checked: int = 0
    cells_normalized: int = 0
    cells_corrected: int = 0
    cells_rejected: int = 0
    hits: list[TableCellView] = field(default_factory=list)
    """有裁决结论的格子（用于明细列表）。"""

    def _arith_payload(self) -> dict[str, Any]:
        """喂给 ``describe_arithmetic`` 的三态输入。

        ★ 关键：``arith_checked`` 为 0 且**有**失败记录时，必须传 ``None``
        而不是 0。原因有两层：

        ① 产物里的 ``checked`` 只统计**相符**的合计线（``domain/tools/tables.py``
           在 ``approx_equal`` 成立时才递增），所以「有合计线但不平」的表
           会得到 ``checked == 0, failure_count > 0``。
        ② ``describe_arithmetic`` 的第一分支是 ``checked == 0`` →
           「无从校验（表内没有合计线）」。显式传 0 会让这个分支抢先命中，
           于是页面出现自相矛盾的一句：标签说「表内没有合计线」，
           紧跟着展开的明细块却逐条列出了不平的合计线（实测 18 张表如此）。

        传 ``None`` 则走它自己的兜底 —— 「有失败记录就说明确实查过」，
        得到 ``bad / 发现 N 处算术不平``，那才是事实。
        """
        checked: int | None = self.arith_checked
        if not checked and self.arith_failures:
            checked = None
        return {
            "checked": checked,
            "ok": self.arith_ok,
            "failure_count": len(self.arith_failures),
        }

    @property
    def arith_label(self) -> str:
        """算术校验的**如实**表述（三态，措辞与 ``describe_arithmetic`` 一致）。"""
        return describe_arithmetic(self._arith_payload())[1]

    @property
    def arith_css(self) -> str:
        return describe_arithmetic(self._arith_payload())[0]

    @property
    def changed_cells(self) -> list[TableCellView]:
        return [c for c in self.hits if c.changed]


@dataclass
class FactView:
    """一条事实 —— 页面上要展示的全部信息。"""

    fact_id: str
    entity: str
    metric: str
    value: str
    unit: str
    raw_text: str
    period: str
    period_type: str
    statement_scope: str
    profit_basis: str
    source_layer: str
    confidence: float
    confidence_explain: str
    needs_review: bool
    review_reason: str | None
    extractor: str
    evidence: list[EvidenceView] = field(default_factory=list)
    channel_readings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def layer_tag(self) -> str:
        """用于页面上的来源层标签（L1/L2/L3/L4）。"""
        return self.source_layer.split("_")[0] if self.source_layer else "?"

    @property
    def extractor_name(self) -> str:
        """抽取器短名（``table_grid`` / ``rule_engine`` / ``vlm_scan``…）。"""
        return (self.extractor or "").split("@")[0] or "?"

    @property
    def extractor_label(self) -> str:
        """人话标签。**确定性路径与模型路径必须一眼分得清** ——
        这正是本项目"确定性优先"的落点。"""
        return {
            "table_grid": "表格网格（行列定位，确定性）",
            "rule_engine": "规则引擎（别名定位，确定性）",
            "vlm_scan": "VLM 整页扫描（视觉读数）",
            "vlm_field": "VLM 字段裁决（视觉读数）",
        }.get(self.extractor_name, self.extractor_name)

    @property
    def extractor_deterministic(self) -> bool:
        return self.extractor_name in ("table_grid", "rule_engine")

    @property
    def confidence_pct(self) -> str:
        return f"{self.confidence * 100:.0f}%"

    @property
    def value_display(self) -> str:
        """数值的可读化显示：**只给整数部分加千分位，一个数字都不改**。

        `facts.json` 里存的是机器读数（``183020977818``），入库时逐字保真 ——
        这是对的，产物不该为了好看而动。但表格里并排出现
        ``183020977818`` / ``172021000000.00`` / ``9900000.0`` 时，
        读者得一位一位数才能比大小；而右边「原文片段」列里的原文
        （``183,020,977,818股``）本身就带千分位，两列反而对不齐。
        分组规则本身见 :func:`group_thousands`（控制台的可比公司表也用它，
        两张表的数字必须按同一规则分组）。
        """
        return group_thousands(self.value)

    @property
    def primary_quote(self) -> str:
        return self.evidence[0].quote if self.evidence else ""


@dataclass
class RunSummary:
    """运行摘要（用于 /runs 列表与顶部状态条）。"""

    run_id: str
    pipeline: str = ""
    status: str = "unknown"
    started_at: str = ""
    finished_at: str = ""
    total_tokens: int = 0
    total_cost_cny: float = 0.0
    cache_hit_rate: float | None = None
    data_dir: str = ""
    provider: str = ""
    models: dict[str, str] = field(default_factory=dict)
    output_count: int = 0
    error: str | None = None

    @property
    def status_icon(self) -> str:
        """复核记号，**不是 emoji**。

        2026-09-27 改。原返回 ✅⚠️❌❔ —— 彩色 emoji 在同一行里大小与基线
        由字体决定，Windows/投影仪上尤其不一致，而且它把「通过/失败」
        这件事交给了色相。记号体系（见 app.css 的 .mk-*）用同一族的
        等宽字形承载：

            √  通过（常态，不上色）
            ◐  部分成功 —— 有产出但不完整，需要人看一眼
            ×  失败
            ○  未完成 / 未知

        注意 `partial` 从 ⚠️ 改成 ◐ 而不是 ×：partial 是「有结果但不全」，
        与「跑挂了」是两回事，混成一个记号会让 partial 被当成失败读。
        """
        return {"success": "√", "partial": "◐", "failed": "×"}.get(self.status, "○")

    @property
    def started_at_short(self) -> str:
        return self.started_at[:19].replace("T", " ") if self.started_at else ""


# ==========================================================================
# 运行列表
# ==========================================================================


def available_runs(runs_root: str | Path | None = None) -> list[RunSummary]:
    """列出全部运行（按开始时间倒序）。"""
    root = Path(runs_root) if runs_root else DEFAULT_RUNS_ROOT
    if not root.is_dir():
        return []

    out: list[RunSummary] = []
    for d in root.iterdir():
        if not d.is_dir() or d.name.startswith("_"):
            continue
        mf = d / "manifest.json"
        if not mf.exists():
            out.append(RunSummary(run_id=d.name, status="未完成"))
            continue
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            out.append(RunSummary(run_id=d.name, status="损坏"))
            continue
        out.append(
            RunSummary(
                run_id=d.name,
                pipeline=m.get("pipeline", ""),
                status=m.get("status", "unknown"),
                started_at=m.get("started_at", ""),
                finished_at=m.get("finished_at", "") or "",
                total_tokens=int(m.get("total_tokens", 0) or 0),
                total_cost_cny=float(m.get("total_cost_cny", 0) or 0),
                cache_hit_rate=m.get("cache_hit_rate"),
                data_dir=m.get("data_dir", ""),
                provider=m.get("provider", ""),
                models=m.get("models", {}) or {},
                output_count=len(m.get("outputs", {}) or {}),
            )
        )
    out.sort(key=lambda r: r.started_at, reverse=True)
    return out


def resolve_run(run_id: str | None, runs_root: str | Path | None = None) -> str:
    """把 run_id 解析为一个存在的 run（None 时取最新一个）。

    Raises:
        ArtifactError: 没有任何可用 run，或指定的 run 不存在。
    """
    runs = available_runs(runs_root)
    usable = [r for r in runs if r.status not in ("未完成", "损坏")]
    if not usable:
        raise ArtifactError(
            "还没有任何可展示的运行。请先执行：\n"
            "    python -m agent.cli run full --data data --run-id demo"
        )
    if run_id:
        for r in usable:
            if r.run_id == run_id:
                return r.run_id
        # 区分「不存在」与「存在但还没跑完」。后者在演示现场很常见
        # （后台正在跑），说成"找不到"会让人以为路径写错了。
        known = {r.run_id: r for r in runs}
        if run_id in known:
            raise ArtifactError(
                f"运行 {run_id} 还没有可展示的产物（状态：{known[run_id].status}）。"
                f"manifest.json 要等运行结束才会写出 —— 若是正在跑，跑完刷新即可。"
            )
        raise ArtifactError(f"找不到 run：{run_id}（可用：{[r.run_id for r in usable][:8]}）")
    return usable[0].run_id


# ==========================================================================
# 单个 run 的产物读取
# ==========================================================================


class RunArtifacts:
    """一个 run 目录的只读访问器。"""

    def __init__(self, run_dir: Path) -> None:
        self.dir = run_dir
        self.run_id = run_dir.name

    # ---------------------------------------------------------------- 基础

    def _read_json(self, rel: str, *, required: bool = False) -> Any:
        p = self.dir / rel
        if not p.is_file():
            if required:
                raise ArtifactError(f"缺少产物：{rel}（未运行到该阶段？）")
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            if required:
                raise ArtifactError(f"产物损坏：{rel} —— {exc}") from exc
            return None

    def manifest_outputs(self) -> dict[str, str]:
        """``manifest.json`` 里登记的产物 ``{相对路径: SHA256}``。

        与 :meth:`outputs`（磁盘扫描）是**两个口径**，必须分开暴露：
        磁盘扫描会看见 ``.lock``、``cache/llm.jsonl`` 这类运行期文件，
        manifest 只登记流水线声明的产物。页面上两个数字不同是**定义差异**，
        不是遗漏 —— 把两个都显示出来并解释差额，读者才能自己复核。
        """
        m = self._read_json("manifest.json") or {}
        out = m.get("outputs")
        return dict(out) if isinstance(out, dict) else {}

    @property
    def summary(self) -> RunSummary:
        m = self._read_json("manifest.json") or {}
        return RunSummary(
            run_id=self.run_id,
            pipeline=m.get("pipeline", ""),
            status=m.get("status", "unknown"),
            started_at=m.get("started_at", ""),
            finished_at=m.get("finished_at", "") or "",
            total_tokens=int(m.get("total_tokens", 0) or 0),
            total_cost_cny=float(m.get("total_cost_cny", 0) or 0),
            cache_hit_rate=m.get("cache_hit_rate"),
            data_dir=m.get("data_dir", ""),
            provider=m.get("provider", ""),
            models=m.get("models", {}) or {},
            output_count=len(m.get("outputs", {}) or {}),
        )

    # ---------------------------------------------------------------- 文档

    @property
    def facts_file(self) -> dict[str, Any]:
        """整份 ``facts/facts.json``（含 ``evidence`` 与合同版本）。

        :meth:`build_facts` 把它转成了视图模型，但**证据 span 原始表**
        和 ``contract_version`` 在转换中会丢掉，而「格式规范性」一栏
        需要拿整份文件去过 FactSet 契约、并核对证据链有无悬空引用 ——
        所以这里额外暴露原始 dict。读不到时返回空 dict，调用方按空处理。
        """
        d = self._read_json("facts/facts.json")
        return d if isinstance(d, dict) else {}

    @property
    def build_stats(self) -> dict[str, Any]:
        """``facts/build_stats.json`` —— 抽取过程的计数器（候选/采信/否决等）。

        这是**流水线自己的统计**，不是网页现算的，所以页面引用它时必须
        写明口径（例如 ``table_grid_rejected_bad_value`` 是「值不合理被否决」，
        不是「读错了」）。
        """
        d = self._read_json("facts/build_stats.json")
        return d if isinstance(d, dict) else {}

    @property
    def documents(self) -> list[dict[str, Any]]:
        return self._read_json("facts/documents.json") or []

    @property
    def pages(self) -> list[dict[str, Any]]:
        return self._read_json("pages/index.json") or []

    def pages_of(self, doc_id: str) -> list[dict[str, Any]]:
        return [p for p in self.pages if p.get("doc_id") == doc_id]

    # ---------------------------------------------------------------- 事实

    def build_facts(self) -> list[FactView]:
        """把 facts.json 转成视图模型（含证据与置信度明细）。"""
        fs = self._read_json("facts/facts.json")
        if not fs:
            return []

        evidence_by_id: dict[str, EvidenceView] = {}
        for e in fs.get("evidence", []):
            evidence_by_id[e["span_id"]] = EvidenceView(
                span_id=e["span_id"],
                doc_id=e["doc_id"],
                page=e["page"],
                quote=e.get("quote", ""),
                bbox=e.get("bbox"),
                source_layer=e.get("source_layer", ""),
            )

        out: list[FactView] = []
        for f in fs.get("facts", []):
            evs = [evidence_by_id[s] for s in f.get("evidence_span_ids", []) if s in evidence_by_id]
            cb = f.get("confidence_breakdown") or {}
            out.append(
                FactView(
                    fact_id=f["fact_id"],
                    entity=f.get("entity", ""),
                    metric=f.get("metric", ""),
                    value=str(f.get("value", "")),
                    unit=f.get("unit", ""),
                    raw_text=f.get("raw_text", ""),
                    period=f.get("period", ""),
                    period_type=f.get("period_type", ""),
                    statement_scope=f.get("statement_scope", ""),
                    profit_basis=f.get("profit_basis", ""),
                    source_layer=f.get("source_layer", ""),
                    confidence=float(f.get("confidence", 0)),
                    confidence_explain=_explain_confidence(cb),
                    needs_review=bool(f.get("needs_review")),
                    review_reason=f.get("review_reason"),
                    extractor=f.get("extractor", ""),
                    evidence=evs,
                    channel_readings=f.get("channel_readings", []) or [],
                )
            )
        return out

    @property
    def internal_conflicts(self) -> list[dict[str, Any]]:
        return self._read_json("facts/internal_conflicts.json") or []

    def entity_reattribution(self) -> list[dict[str, Any]]:
        """主体归属纠正明细（表格行里的事实按该行主体重新归属）。

        这份产物本身就是"改了什么必须留痕"的体现，因此页面直接展示。
        """
        return self._read_json("facts/entity_reattribution.json") or []

    def extraction_sources(self) -> dict[str, Any]:
        """抽取路径构成 —— **确定性 vs 模型**，一眼看清。

        为什么值得单列：真实材料上曾出现「73% 的事实来自 VLM 整页扫描，
        而它读的正是已经重建好的表格」。修好之后这个比例降到 29%。
        这个数字是"确定性优先"是否真的落地了的直接证据。
        """
        facts = self.build_facts()
        by_extractor: dict[str, int] = {}
        for f in facts:
            by_extractor[f.extractor_name] = by_extractor.get(f.extractor_name, 0) + 1
        deterministic = sum(
            n for name, n in by_extractor.items()
            if name in ("table_grid", "rule_engine")
        )
        total = len(facts)
        return {
            "by_extractor": dict(sorted(by_extractor.items(), key=lambda kv: -kv[1])),
            "deterministic": deterministic,
            "model": total - deterministic,
            "total": total,
            "deterministic_ratio": (deterministic / total) if total else 0.0,
            "reattributed": len(self.entity_reattribution()),
        }

    def cross_validation(self) -> dict[str, Any]:
        """双路互证：模型读到的值里，有多少被**确定性路径独立复现**。

        口径是**逐取值**比对（主体 + 指标 + 数值），不是按 (主体,指标) 打包 ——
        同一指标天然有多个取值（多行/多期），打包比会得出
        "要么全对要么全错"的假结论。

        只看事实库，不重算任何数字：页面上的比例必须与产物一致。
        """
        det: set[tuple[str, str, str]] = set()
        mod: set[tuple[str, str, str]] = set()
        for f in self.build_facts():
            key = (f.entity, f.metric, str(f.value))
            if f.extractor_name in ("table_grid", "rule_engine"):
                det.add(key)
            elif f.extractor_name.startswith("vlm"):
                mod.add(key)
        both = det & mod
        return {
            "deterministic_values": len(det),
            "model_values": len(mod),
            "confirmed": len(both),
            "confirmed_ratio": (len(both) / len(mod)) if mod else 0.0,
            "model_only": len(mod - det),
        }

    # ---------------------------------------------------------------- 通道

    @property
    def text_layer(self) -> list[dict[str, Any]]:
        return self._read_json("channels/text_layer.json") or []

    @property
    def ocr_channel(self) -> list[dict[str, Any]]:
        return self._read_json("channels/ocr.json") or []

    @property
    def vlm_scans(self) -> list[dict[str, Any]]:
        return self._read_json("channels/vlm_scans.json") or []

    @property
    def tables(self) -> list[dict[str, Any]]:
        """表格结构（由 extract_tables 阶段产出）。"""
        return self._read_json("channels/tables.json") or []

    @property
    def table_adjudications(self) -> list[dict[str, Any]]:
        """单元格级裁决记录（由 judge 阶段产出）。"""
        return self._read_json("channels/table_adjudications.json") or []

    def tables_of(self, doc_id: str, page: int) -> list[dict[str, Any]]:
        return [
            t for t in self.tables
            if t.get("doc_id") == doc_id and int(t.get("page", 0)) == int(page)
        ]

    def adjudications_of(self, doc_id: str, page: int) -> dict[int, dict[str, Any]]:
        """按 ``table_index`` 索引该页的裁决记录。

        一页可能有多张表，而裁决逐表产生 —— 只返回第一条会让第二张表的
        裁决结果在页面上凭空消失。
        """
        out: dict[int, dict[str, Any]] = {}
        for rec in self.table_adjudications:
            if rec.get("doc_id") == doc_id and int(rec.get("page", 0)) == int(page):
                out[int(rec.get("table_index", 0) or 0)] = rec
        return out

    def table_views(self, doc_id: str, page: int) -> list[TableView]:
        """把「表格结构 + 裁决记录」拼成页面可直接渲染的视图模型。

        放在这里而不是模板里做，是因为模板里做不了 ——
        嵌套循环下取不到外层下标、字典元组键、``sum(attribute=...)``
        对 dict 不生效，硬写只会得到静默错误的结果。
        """
        adjs = self.adjudications_of(doc_id, page)
        out: list[TableView] = []

        for t in self.tables_of(doc_id, page):
            idx = int(t.get("table_index", 0) or 0)
            adj = adjs.get(idx)
            report = (adj or {}).get("report") or {}

            # 逐格裁决结论，(row, col) → 该条记录
            by_cell: dict[tuple[int, int], dict[str, Any]] = {}
            for a in report.get("adjudications") or []:
                by_cell[(int(a.get("row", -1)), int(a.get("col", -1)))] = a

            # 优先显示**裁决之后**的网格：那才是下游真正消费的内容
            grid = (adj or {}).get("grid_after") or t.get("grid") or []
            rows: list[list[TableCellView]] = []
            for i, row in enumerate(grid):
                view_row: list[TableCellView] = []
                for j, text in enumerate(row):
                    a = by_cell.get((i, j))
                    view_row.append(
                        TableCellView(
                            row=i, col=j, text="" if text is None else str(text),
                            verdict=(a or {}).get("verdict", ""),
                            original=(a or {}).get("original", ""),
                            vlm_text=(a or {}).get("vlm_text", "") or "",
                            confidence=(a or {}).get("confidence"),
                            quote=(a or {}).get("quote") or "",
                            note=(a or {}).get("note") or "",
                        )
                    )
                rows.append(view_row)

            arith = t.get("arithmetic") or {}
            hits = [c for r in rows for c in r if c.verdict]
            out.append(
                TableView(
                    index=idx,
                    method=t.get("method", "") or "",
                    n_rows=int(t.get("n_rows", len(rows)) or 0),
                    n_cols=int(t.get("n_cols", 0) or 0),
                    has_rules=bool(t.get("has_rules")),
                    rows=rows,
                    arith_ok=bool(arith.get("ok", True)),
                    # 老运行的 arithmetic 里没有 checked 字段 —— 用 failure_count
                    # 反推"确实查过"，否则旧产物会被显示成"无从校验"
                    arith_checked=int(
                        arith.get("checked")
                        if arith.get("checked") is not None
                        else (
                            int(arith.get("checked_rows", 0) or 0)
                            + int(arith.get("checked_cols", 0) or 0)
                        )
                        or (1 if arith.get("failure_count") else 0)
                    ),
                    arith_failures=list(arith.get("failures") or []),
                    arith_soft=int(arith.get("soft_count", 0) or 0),
                    trigger=(adj or {}).get("trigger", "") or "",
                    cells_checked=int(report.get("cells_checked", 0) or 0),
                    cells_normalized=int(report.get("normalized", 0) or 0),
                    cells_corrected=int(report.get("corrected", 0) or 0),
                    cells_rejected=int(report.get("rejected", 0) or 0),
                    hits=hits,
                )
            )
        return out

    def table_stats(self) -> dict[str, int]:
        """表格与单元格裁决的汇总（用于抽取工作台顶部）。"""
        tables = self.tables
        adjs = self.table_adjudications
        reports = [r.get("report") or {} for r in adjs]
        return {
            "tables": len(tables),
            "tables_with_rules": sum(1 for t in tables if t.get("has_rules")),
            "arith_checked": sum(
                int((t.get("arithmetic") or {}).get("checked", 0) or 0) for t in tables
            ),
            "arith_failed_tables": sum(
                1 for t in tables if not (t.get("arithmetic") or {}).get("ok", True)
            ),
            "tables_adjudicated": len(adjs),
            "cells_checked": sum(int(r.get("cells_checked", 0) or 0) for r in reports),
            "cells_agree": sum(int(r.get("agree", 0) or 0) for r in reports),
            "cells_normalized": sum(int(r.get("normalized", 0) or 0) for r in reports),
            "cells_corrected": sum(int(r.get("corrected", 0) or 0) for r in reports),
            "cells_rejected": sum(int(r.get("rejected", 0) or 0) for r in reports),
            "cells_unreadable": sum(int(r.get("unreadable", 0) or 0) for r in reports),
            "cells_errors": sum(int(r.get("errors", 0) or 0) for r in reports),
        }

    @property
    def vlm_verdicts(self) -> list[dict[str, Any]]:
        return self._read_json("channels/vlm_verdicts.json") or []

    def cell_crop(self, doc_id: str, page: int, row: int, col: int, *,
                  context: bool = False) -> Path | None:
        """单元格裁图路径。

        ``context=False`` 是模型真正读的那张（放大后的单格）；
        ``context=True`` 是整页图上的红框标注，用于人工核对
        "模型看的确实是这一格"。
        """
        suffix = "_ctx" if context else ""
        name = f"cell_p{int(page)}_r{int(row)}c{int(col)}{suffix}.png"
        p = self.dir / "pages" / "cells" / doc_id / name
        if p.is_file():
            return p

        # 兼容早期产物：裁图曾平铺在 ``cells/`` 下（不带文档目录），
        # 于是不同文档的同页同格**互相覆盖**。只有当该页的裁决记录
        # 只涉及一个文档时，平铺文件才无歧义；否则宁可返回 None ——
        # 拿别的文档的图充当证据，比不给图更糟。
        flat = self.dir / "pages" / "cells" / name
        if flat.is_file():
            docs_on_page = {
                rec.get("doc_id") for rec in self.table_adjudications
                if int(rec.get("page", 0) or 0) == int(page)
            }
            if len(docs_on_page) <= 1:
                return flat
        return None

    @property
    def conflicts(self) -> list[dict[str, Any]]:
        return self._read_json("channels/conflicts.json") or []

    def channel_stats(self) -> dict[str, Any]:
        """三层引擎的汇总统计（用于抽取工作台顶部）。"""
        scans = self.vlm_scans
        verdicts = self.vlm_verdicts
        scan_fields = [f for s in scans for f in s.get("fields", [])]
        rejected = [f for f in scan_fields if f.get("rejected")]
        return {
            "text_pages": len(self.text_layer),
            "ocr_pages": len(self.ocr_channel),
            "vlm_scan_pages": len(scans),
            "vlm_verdicts": len(verdicts),
            "scan_fields_total": len(scan_fields),
            "scan_fields_usable": len(scan_fields) - len(rejected),
            "scan_fields_rejected": len(rejected),
            "conflict_pages": len(self.conflicts),
        }

    # ---------------------------------------------------------------- 文本

    def page_text(self, doc_id: str, page: int) -> str:
        """取某页的权威原文（优先 raw，回退 text）。"""
        for rec in self.text_layer:
            if rec.get("doc_id") == doc_id and int(rec.get("page", 0)) == page:
                return rec.get("raw") or rec.get("text") or ""
        for rec in self.ocr_channel:
            if rec.get("doc_id") == doc_id and int(rec.get("page", 0)) == page:
                return rec.get("raw") or rec.get("text") or ""
        return ""

    def page_image(self, doc_id: str, page: int) -> Path | None:
        """找到该页已渲染的图片（用于溯源面板左栏）。"""
        for pattern in (
            f"{doc_id}_p{page:04d}_300dpi.png",
            f"{doc_id}_p{page:04d}_200dpi.png",
            f"{doc_id}_p{page:04d}.png",
            f"{doc_id}_p{page:04d}_raw.png",
        ):
            p = self.dir / "pages" / pattern
            if p.is_file():
                return p
        hits = sorted((self.dir / "pages").glob(f"{doc_id}_p{page:04d}*.png"))
        return hits[0] if hits else None

    # ---------------------------------------------------------------- R4/R5

    def valuation(self) -> dict[str, Any] | None:
        return self._read_json("valuation/valuation.json")

    def valuation_charts(self) -> dict[str, str]:
        """返回可用的图表文件名 -> 展示标题。"""
        titles = {
            "sensitivity_heatmap.png": "敏感性热力图（WACC × 永续增长率）",
            "tornado.png": "龙卷风图（关键假设影响排序）",
            "value_range.png": "估值区间对比",
            "fcff_waterfall.png": "预测期收入 / NOPAT / FCFF",
        }
        d = self.dir / "valuation" / "charts"
        if not d.is_dir():
            return {}
        return {name: titles[name] for name in titles if (d / name).is_file()}

    def valuation_report_md(self) -> str:
        p = self.dir / "valuation" / "valuation_report.md"
        return p.read_text(encoding="utf-8") if p.is_file() else ""

    def audit(self) -> dict[str, Any] | None:
        return self._read_json("audit/audit.json")

    def audit_report_md(self) -> str:
        p = self.dir / "audit" / "audit_report.md"
        return p.read_text(encoding="utf-8") if p.is_file() else ""

    def draft_text(self) -> str:
        p = self.dir / "audit" / "draft_used.md"
        return p.read_text(encoding="utf-8") if p.is_file() else ""

    def trace_summary(self) -> dict[str, Any]:
        """从 trace.jsonl 汇总运行过程统计。"""
        from agent.trace.tracer import read_trace, summarize_trace

        p = self.dir / "trace.jsonl"
        if not p.is_file():
            return {}
        try:
            return summarize_trace(read_trace(p))
        except Exception:
            return {}

    def outputs(self) -> list[dict[str, Any]]:
        """产物清单（用于「本次运行产出了什么」）。"""
        rows: list[dict[str, Any]] = []
        for p in sorted(self.dir.rglob("*")):
            if not p.is_file() or "_replay" in p.parts:
                continue
            rel = str(p.relative_to(self.dir)).replace("\\", "/")
            if rel in ("manifest.json", "checkpoint.json", "trace.jsonl"):
                continue
            rows.append({"path": rel, "size": p.stat().st_size,
                         "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%H:%M:%S")})
        return rows


def _explain_confidence(cb: dict[str, Any]) -> str:
    """把置信度明细拼成可读字符串（与命令行输出保持一致）。"""
    if not cb:
        return ""
    parts = [f"base={cb.get('base', 0):.2f}"]
    for adj in cb.get("adjustments", []) or []:
        parts.append(f"{adj.get('reason', '')} {adj.get('delta', 0):+.2f}")
    total = cb.get("base", 0) + sum(a.get("delta", 0) for a in cb.get("adjustments", []) or [])
    return " | ".join(parts) + f" => {max(0.0, min(0.99, total)):.2f}"


# ==========================================================================
# 便捷入口
# ==========================================================================


def load_run(run_id: str | None = None, runs_root: str | Path | None = None) -> RunArtifacts:
    """解析并打开一个 run。"""
    resolved = resolve_run(run_id, runs_root)
    root = Path(runs_root) if runs_root else DEFAULT_RUNS_ROOT
    return RunArtifacts(root / resolved)


__all__ = [
    "ArtifactError",
    "EvidenceView",
    "FactView",
    "RunSummary",
    "RunArtifacts",
    "available_runs",
    "resolve_run",
    "load_run",
    "PROJECT_ROOT",
    "DEFAULT_RUNS_ROOT",
]
