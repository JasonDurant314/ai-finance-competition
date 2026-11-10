"""表格解析基准 —— 量化「当前表格解析到底有多准」。

## 为什么必须先做基准

「修好表格裁决」这句话在工程上不可验证 ——
除非先有一个能给出数字的基准。没有基准，改动只能靠"看起来对了"来判断。

## 真值从哪来

表格真值天然难做。这里用**三层可验证的代理指标**，
每一层都不需要人工标注，且逐层更严格：

    T1 数值保真率（bag-of-numbers）
       真值 = 页面上**渲染出来的全部数字**（文本层给出）。
       指标 = `extract_table` 抽出的网格里，数字有多少能在页面数字集合中找到。
       **可完全度量**：网格里凭空多出来的数字 = 伪造；缺失的 = 遗漏。

    T2 结构完整性
       行数、列数、以及「每行的列数是否一致」。
       真值不可得，但**内部一致性**可判定：真实表格的行列结构是规则的，
       严重不规则说明重建失败。

    T3 算术自洽
       合计行 = 分项和。这不需要真值 —— 表格自身就提供了约束。
       算不平即说明**至少有单元格被读错了**，且能定位到行/列。

    T4 单元格图像可读性（可选，需 API）
       把可疑单元格裁剪出来交 VLM 读，与文本层读数比对。
       这是接入前后的**直接对比**。

## 用法

    python tools/table_benchmark.py --scan            # 扫描样本，统计表格页分布
    python tools/table_benchmark.py --baseline        # T1–T3 基线（无需 API）
    python tools/table_benchmark.py --vlm --limit 12  # T4：VLM 读单元格并比对（需 API）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.console import enable_utf8_output  # noqa: E402

enable_utf8_output()

SAMPLE_DIR = PROJECT_ROOT / "agent" / "sample"
WORK_DIR = PROJECT_ROOT / "_table_bench"
RESULTS = PROJECT_ROOT / "docs" / "table_benchmark.json"

_NUM_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")


def digits_only(text: str) -> str:
    """只保留数字字符。

    ⚠️ 比对时**两侧必须用同一套归一化**。早期实现一侧用
    ``digits_only``（去掉小数点 → "1000"）、另一侧用数字 token
    去千分位（保留小数点 → "1000.00"），于是**所有带小数的金额
    都判不出匹配** —— 实测一页报了 38 个"伪造数字"，逐条核对后
    发现全是 ``1,000.00`` 这类正常金额。指标错得离谱。
    """
    return "".join(ch for ch in text if ch.isdigit())


def number_key(num_token: str) -> str:
    """把数字 token 归一到「纯数字串」，与 :func:`digits_only` 口径一致。"""
    return digits_only(num_token)


def numbers_in(text: str) -> list[str]:
    """页面/单元格里的数字 token（**归一化后**的纯数字串）。"""
    return [number_key(m.group(0)) for m in _NUM_TOKEN.finditer(text or "") if m.group(0)]


# ==========================================================================
# 指标
# ==========================================================================


@dataclass
class TableMetric:
    file: str
    page: int
    method: str
    has_rules: bool
    n_rows: int
    n_cols: int

    # T1 数值保真率
    grid_numbers: int = 0
    grid_numbers_found: int = 0
    fabricated_numbers: int = 0          # 网格里有、页面文本层没有 —— 危险信号

    # T2 结构质量（详见 evaluate_table 内的说明）
    nonempty_cells: int = 0
    fragmented_cells: int = 0            # 格内仍有换行 —— 文字被切开
    empty_cols: int = 0                  # 整列全空 —— 网格里混入了不存在的列
    empty_rows: int = 0                  # 整行全空
    oversized_cells: int = 0             # 单格异常长 —— 相邻格被合并

    # T3 算术自洽
    arith_checked_rows: int = 0
    arith_checked_cols: int = 0
    arith_failures: int = 0
    arith_soft: int = 0                  # 合计只覆盖部分分项（分层小计），放过
    has_total_label: bool = False        # 表内是否出现「合计/小计/总计」标签

    elapsed_s: float = 0.0
    error: str | None = None

    @property
    def fidelity(self) -> float:
        """T1：网格数字中能在页面里找到的比例。"""
        return self.grid_numbers_found / self.grid_numbers if self.grid_numbers else 1.0

    @property
    def structure_ok(self) -> bool:
        """结构是否可用：没有碎格、没有空行空列、没有异常合并格。"""
        return (
            self.fragmented_cells == 0
            and self.empty_cols == 0
            and self.empty_rows == 0
            and self.oversized_cells == 0
            and self.n_rows >= 2
            and self.n_cols >= 2
        )

    @property
    def arith_ok(self) -> bool:
        return self.arith_failures == 0

    @property
    def arith_verified(self) -> bool:
        """是否**真的**校验过（而不是"没有合计线所以无从校验"）。

        这是 ``arith_ok`` 的必要补充：没有合计线的表 ``arith_ok`` 也为真，
        若直接把两者当同一件事，就会把"没查过"当成"查过且没问题"。
        """
        return self.arith_checked_rows + self.arith_checked_cols > 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fidelity"] = round(self.fidelity, 4)
        d["structure_ok"] = self.structure_ok
        d["arith_ok"] = self.arith_ok
        d["arith_verified"] = self.arith_verified
        d["elapsed_s"] = round(self.elapsed_s, 2)
        return d


def evaluate_table(table, page_text: str, *, elapsed: float = 0.0) -> TableMetric:
    """对一张已解析的表格计算 T1–T3 指标。"""
    grid = table.grid()
    page_digits = digits_only(page_text)
    page_nums = Counter(numbers_in(page_text))

    m = TableMetric(
        file="", page=table.page, method=table.method, has_rules=table.has_rules,
        n_rows=table.n_rows, n_cols=table.n_cols, elapsed_s=elapsed,
    )

    # T1：数值保真
    # 判据：网格里的每个数字 token，其**纯数字串**必须出现在页面数字串中。
    # 这样 `1,000.00` → "100000" 与页面里的 `1000.00` → "100000" 能对上。
    for row in grid:
        for cell in row:
            if not cell.strip():
                continue
            m.nonempty_cells += 1
            for n in numbers_in(cell):
                if not n:
                    continue
                m.grid_numbers += 1
                if n in page_digits:
                    m.grid_numbers_found += 1
                else:
                    m.fabricated_numbers += 1

    # T2 结构质量
    # ⚠️ 早期用「每行非空单元格数是否一致」判结构完整性 ——
    #    这个指标几乎恒为真（网格列数固定，多行文本仍落在同一格里），
    #    因此测不出任何东西。实测 53.6% 的"不齐"全部来自列数本身不等，
    #    不是真正的结构问题。
    #
    # 换成三个真正有意义的量：
    #   1. **碎片率**：单元格内是否还残留换行 —— 换行说明格内文字被切开，
    #      会破坏别名匹配与数字分词
    #   2. **空列/空行率**：整列或整行全空，说明网格里混入了不存在的行列
    #   3. **超长单元格率**：单格字符数远超其余单元格，说明相邻格被合并
    for row in grid:
        for cell in row:
            if "\n" in cell:
                m.fragmented_cells += 1
    n_rows, n_cols = len(grid), max((len(r) for r in grid), default=0)
    for j in range(n_cols):
        if all(not (grid[i][j] if j < len(grid[i]) else "").strip() for i in range(n_rows)):
            m.empty_cols += 1
    for i in range(n_rows):
        if all(not c.strip() for c in grid[i]):
            m.empty_rows += 1
    lengths = [len(c) for r in grid for c in r if c.strip()]
    if lengths:
        avg = sum(lengths) / len(lengths)
        m.oversized_cells = sum(1 for L in lengths if avg > 0 and L > avg * 4 and L > 40)

    # T3：算术自洽
    try:
        from domain.tools.tables import check_table_arithmetic, total_label_kind

        m.has_total_label = any(total_label_kind(c) for r in grid for c in r)
        chk = check_table_arithmetic(table)
        m.arith_checked_rows = chk.checked_rows
        m.arith_checked_cols = chk.checked_cols
        m.arith_failures = len(chk.failures)
        m.arith_soft = len(chk.soft)
    except Exception as exc:
        m.error = f"算术校验异常：{type(exc).__name__}: {exc}"

    return m


# ==========================================================================
# 样本选择
# ==========================================================================


def _resolve(rel: str) -> Path | None:
    direct = SAMPLE_DIR / rel
    if direct.is_file():
        return direct
    hits = list(SAMPLE_DIR.rglob(rel))
    return hits[0] if hits else None


def pick_table_pages(n: int, *, prefer_rules: bool = True) -> list[tuple[Path, int]]:
    """挑选表格页（按框线密度排序，优先结构清晰的）。

    取样策略：**按框线密度分层**，避免全选到最规整的表。
    每层最多取 2 页，同时保证不重复取同一个文档的太多页。

    ⚠️ 早期实现有一个致命 bug：``if bucket in seen_lines and len(chosen) < n // 2``
    在 ``n`` 较大时几乎总是成立，导致循环前几轮就把所有层标记为"已见"，
    最终只选出 5 页（而请求了 24 页）。因此这里改为**每层独立计数**。
    """
    import pandas as pd

    csv = PROJECT_ROOT / "docs" / "sample_inventory_pages.csv"
    if not csv.exists():
        raise SystemExit("缺少页面清单。请先运行：python tools/inventory_samples.py")
    pages = pd.read_csv(csv)

    pool = pages[(pages["lines"] >= 40) & (pages["chars"] > 200)].copy()
    if prefer_rules:
        pool = pool.sort_values("lines", ascending=False)

    chosen: list[tuple[Path, int]] = []
    per_bucket: dict[int, int] = {}
    per_file: dict[str, int] = {}
    MAX_PER_BUCKET = max(2, n // 6)
    MAX_PER_FILE = max(1, n // 5)

    for _, row in pool.iterrows():
        if len(chosen) >= n:
            break
        path = _resolve(str(row["file"]))
        if path is None:
            continue
        bucket = int(row["lines"]) // 50
        if per_bucket.get(bucket, 0) >= MAX_PER_BUCKET:
            continue
        if per_file.get(path.name, 0) >= MAX_PER_FILE:
            continue
        chosen.append((path, int(row["page"])))
        per_bucket[bucket] = per_bucket.get(bucket, 0) + 1
        per_file[path.name] = per_file.get(path.name, 0) + 1

    return chosen


# ==========================================================================
# T1–T3 基线
# ==========================================================================


def run_baseline(pages: list[tuple[Path, int]], *, save_cells: bool = False) -> dict:
    import fitz

    from domain.tools.tables import check_table_arithmetic, tables_of_page

    print("\n" + "=" * 82)
    print("T1–T3 表格解析基线")
    print("=" * 82)

    rows: list[dict] = []
    dumped: list[dict] = []
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    for pdf, page_no in pages:
        try:
            with fitz.open(pdf) as doc:
                pg = doc.load_page(page_no - 1)
                page_text = pg.get_text("text")
                t0 = time.perf_counter()
                tables = tables_of_page(pg, page_no=page_no)
                elapsed = time.perf_counter() - t0
        except Exception as exc:
            rows.append(TableMetric(file=pdf.name, page=page_no, method="",
                                    has_rules=False, n_rows=0, n_cols=0,
                                    error=f"{type(exc).__name__}: {exc}").to_dict())
            continue

        if not tables:
            rows.append(TableMetric(file=pdf.name, page=page_no, method="none",
                                    has_rules=False, n_rows=0, n_cols=0,
                                    error="未识别到表格").to_dict())
            continue

        for t in tables[:2]:                     # 每页最多评两张表
            m = evaluate_table(t, page_text, elapsed=elapsed / len(tables))
            m.file = pdf.name
            m_dict = m.to_dict()
            rows.append(m_dict)
            print(
                f"  {pdf.name[:30]:<30} p{page_no:<3} {t.method:<20} "
                f"{t.n_rows:>3}×{t.n_cols:<3} "
                f"保真={m.fidelity:6.2%} "
                f"碎格={m.fragmented_cells:>3} 空列={m.empty_cols:>2} 合并={m.oversized_cells:>2} "
                f"算术={'OK' if m.arith_ok else f'{m.arith_failures}处不平':<9} "
                f"{elapsed:.2f}s"
            )
            if save_cells:
                dumped.append({
                    "file": pdf.name, "page": page_no, "method": t.method,
                    "grid": t.grid(),
                    "cells": [c.to_dict() for c in t.cells[:400]],
                })

    if save_cells and dumped:
        (WORK_DIR / "tables_dump.json").write_text(
            json.dumps(dumped, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\n  表格结构已导出：{WORK_DIR / 'tables_dump.json'}（供接入后对比）")

    return summarize(rows, "T1–T3 基线")


def summarize(rows: list[dict], title: str) -> dict:
    if not rows:
        print(f"\n（{title} 无有效结果）")
        return {"title": title, "rows": []}

    import pandas as pd

    df = pd.DataFrame(rows)
    ok = df[df["error"].isna()] if "error" in df.columns else df

    print("\n" + "-" * 82)
    print(f"{title} — 汇总")
    print("-" * 82)
    if ok.empty:
        print("  全部失败：")
        for _, r in df.iterrows():
            print(f"    {r.get('file','?')} p{r.get('page','?')}: {r.get('error')}")
        return {"title": title, "rows": rows}

    print(f"  评估表格数           {len(ok)}")
    print(f"  识别到表格的页数      {len(ok[ok['method'] != 'none'])}")
    print(f"  T1 平均数值保真率     {ok['fidelity'].mean():.2%}")
    print(f"  T1 伪造数字总数       {int(ok['fabricated_numbers'].sum())}  ← 应为 0（网格里凭空多出的数字）")
    print(f"  T2 碎格总数           {int(ok['fragmented_cells'].sum())}  ← 格内仍有换行，文字被切开")
    print(f"  T2 空列总数           {int(ok['empty_cols'].sum())}  ← 网格里混入了不存在的列")
    print(f"  T2 合并格总数         {int(ok['oversized_cells'].sum())}  ← 相邻格被并成一格")
    print(f"  T2 结构可用比例       {ok['structure_ok'].mean():.2%}")
    print(f"  T3 算术自洽比例       {ok['arith_ok'].mean():.2%}")
    print(f"  T3 算术失败总数       {int(ok['arith_failures'].sum())}  ← 每个都意味着至少一个单元格读错")
    print(
        f"  T3 **真的校验过**的比例 {ok['arith_verified'].mean():.2%}"
        f"  ← 其余是无合计线、无从校验（arith_ok 对它们也为真，不可混为一谈）"
    )
    print(f"  T3 含合计标签的表      {int(ok['has_total_label'].sum())} / {len(ok)}")
    print(f"  T3 放过（分层小计）    {int(ok['arith_soft'].sum())}")
    print(f"  平均耗时              {ok['elapsed_s'].mean():.2f}s/表")
    if "method" in ok.columns:
        print("\n  按识别方法分组：")
        g = ok.groupby("method").agg(
            表数=("fidelity", "count"),
            平均保真=("fidelity", "mean"),
            算术自洽=("arith_ok", "mean"),
            真的校验过=("arith_verified", "mean"),
            伪造数=("fabricated_numbers", "sum"),
        )
        g["平均保真"] = (g["平均保真"] * 100).round(2).astype(str) + "%"
        g["算术自洽"] = (g["算术自洽"] * 100).round(2).astype(str) + "%"
        g["真的校验过"] = (g["真的校验过"] * 100).round(2).astype(str) + "%"
        print(g.to_string())

    return {"title": title, "rows": rows,
            "summary": {
                "tables": len(ok),
                "mean_fidelity": round(float(ok["fidelity"].mean()), 4),
                "fabricated_total": int(ok["fabricated_numbers"].sum()),
                "structure_ok_rate": round(float(ok["structure_ok"].mean()), 4),
                "arith_ok_rate": round(float(ok["arith_ok"].mean()), 4),
                "arith_verified_rate": round(float(ok["arith_verified"].mean()), 4),
                "tables_with_total_label": int(ok["has_total_label"].sum()),
                "arith_failures_total": int(ok["arith_failures"].sum()),
                "arith_soft_total": int(ok["arith_soft"].sum()),
                "mean_elapsed_s": round(float(ok["elapsed_s"].mean()), 3),
            }}


# ==========================================================================
# T4：VLM 读单元格（需 API）
# ==========================================================================


def run_vlm_cells(pages: list[tuple[Path, int]], *, max_cells_per_page: int = 6) -> dict:
    """对可疑单元格做 VLM 读数，与文本层读数比对。

    「可疑」的判定：**算术校验失败的那些行列涉及的单元格**。
    这比随机抽查更有针对性 —— 算术不平本身就说明那里有错。
    """
    import fitz

    from agent.config import load_config
    from agent.model.gateway import ModelGateway, RoleConfig, text_message
    from agent.model.providers.openai_compat import OpenAICompatProvider
    from domain.tools.document import crop_region_png
    from domain.tools.tables import check_table_arithmetic, tables_of_page
    from domain.tools.vlm import read_table_cell

    cfg = load_config("models")
    pcfg = cfg.get("provider") or {}
    roles = {
        r: RoleConfig(
            model=str(s.get("model", "deepseek-flash")),
            temperature=float(s.get("temperature", 0.0)),
            max_tokens=int(s.get("max_tokens", 16384)),
            thinking=bool(s.get("thinking", False)),
            supports_vision=bool(s.get("supports_vision", False)),
        )
        for r, s in (cfg.get("roles") or {}).items()
    }
    gw = ModelGateway(
        provider=OpenAICompatProvider(
            base_url=str(pcfg.get("base_url", "")), api_key=pcfg.get("api_key"),
            provider_name=str(pcfg.get("name", "openai_compat")),
        ),
        roles=roles, offline=False,
    )

    print("\n" + "=" * 82)
    print("T4 单元格级 VLM 读数（与文本层读数比对）")
    print("=" * 82)

    out_dir = WORK_DIR / "cells"
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    matches = 0
    mismatches = 0
    empty = 0
    cell_total = 0

    for pdf, page_no in pages:
        with fitz.open(pdf) as doc:
            pg = doc.load_page(page_no - 1)
            page_text = pg.get_text("text")
            tables = tables_of_page(pg, page_no=page_no)
            if not tables:
                continue
            table = tables[0]
            chk = check_table_arithmetic(table)

            # 从算术失败里定位可疑单元格
            suspect: list[tuple[int, int]] = []
            for f in chk.failures[:4]:
                if f.get("kind") == "row_sum":
                    r = int(f["row"])
                    for c in range(table.n_cols):
                        suspect.append((r, c))
                elif f.get("kind") == "col_sum":
                    c = int(f["col"])
                    for r in range(table.n_rows):
                        suspect.append((r, c))

            if not suspect:
                # 无算术失败：抽样数值单元格（做无偏估计）
                for cell in table.cells:
                    if any(ch.isdigit() for ch in cell.text):
                        suspect.append((cell.row, cell.col))
                    if len(suspect) >= max_cells_per_page:
                        break

            seen: set[tuple[int, int]] = set()
            print(f"\n[{pdf.name[:36]} p{page_no}] 算术失败 {len(chk.failures)} 处，"
                  f"抽查 {min(len(suspect), max_cells_per_page)} 个单元格")

            for (r, c) in suspect:
                if (r, c) in seen:
                    continue
                seen.add((r, c))
                if len(seen) > max_cells_per_page:
                    break

                cell = table.cell(r, c)
                if cell is None or not cell.text.strip():
                    continue
                cell_total += 1

                img = out_dir / f"{pdf.stem}_p{page_no}_r{r}c{c}.png"
                try:
                    crop_region_png(doc, page_no, cell.bbox, img, dpi=400)
                    reading = read_table_cell(
                        gw, cell_image_path=img, page_no=page_no, row=r, col=c,
                        page_text=page_text, verify_quote=False,
                    )
                except Exception as exc:
                    rows.append({"file": pdf.name, "page": page_no, "row": r, "col": c,
                                 "error": f"{type(exc).__name__}: {exc}"})
                    continue

                vlm_digits = digits_only(reading.text)
                text_digits = digits_only(cell.text)
                if not vlm_digits and not text_digits:
                    empty += 1
                    verdict = "both_empty"
                elif vlm_digits == text_digits:
                    matches += 1
                    verdict = "agree"
                else:
                    mismatches += 1
                    verdict = "disagree"

                rows.append({
                    "file": pdf.name, "page": page_no, "row": r, "col": c,
                    "text_layer": cell.text, "vlm": reading.text,
                    "vlm_quote": reading.quote, "conf": reading.self_confidence,
                    "verdict": verdict, "image": img.name,
                })
                mark = {"agree": "✓", "disagree": "✗", "both_empty": "·"}[verdict]
                print(f"    {mark} r{r}c{c}  文本层={cell.text[:26]!r:<30} "
                      f"VLM={reading.text[:26]!r:<30} conf={reading.self_confidence:.2f}")
                if verdict == "disagree":
                    print(f"         VLM quote: {str(reading.quote)[:80]!r}")

    print("\n" + "-" * 82)
    judged = matches + mismatches
    print(f"  抽查单元格数        {cell_total}")
    print(f"  两者一致            {matches}")
    print(f"  两者不一致          {mismatches}")
    if judged:
        print(f"  **一致率            {matches / judged:.2%}**")
    print(f"\n  不一致的单元格图像已保存到 {out_dir}，供人工核对。")

    (WORK_DIR / "vlm_cells.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "cells": cell_total, "agree": matches, "disagree": mismatches,
        "both_empty": empty,
        "agree_rate": round(matches / judged, 4) if judged else None,
        "rows": rows,
    }


# ==========================================================================
# 入口
# ==========================================================================


def scan() -> dict:
    """统计样本里的表格页分布。"""
    import pandas as pd

    csv = PROJECT_ROOT / "docs" / "sample_inventory_pages.csv"
    if not csv.exists():
        raise SystemExit("缺少页面清单。请先运行：python tools/inventory_samples.py")
    pages = pd.read_csv(csv)

    print("=" * 82)
    print("表格页分布")
    print("=" * 82)
    for th in (20, 40, 80, 120, 160):
        n = int((pages["lines"] >= th).sum())
        print(f"  框线线段 ≥ {th:>3}：{n:>4} 页  ({n / len(pages):.1%})")

    dense = pages[pages["lines"] >= 40]
    print(f"\n表格页共 {len(dense)} 页，来自 {dense['file'].nunique()} 个文档")
    print("\n按文档：")
    g = dense.groupby("file").agg(表格页=("page", "count"), 最大线段=("lines", "max"))
    print(g.sort_values("表格页", ascending=False).head(15).to_string())
    return {"table_pages": int(len(dense)), "documents": int(dense["file"].nunique())}


def main() -> int:
    ap = argparse.ArgumentParser(description="表格解析基准")
    ap.add_argument("--scan", action="store_true", help="统计表格页分布")
    ap.add_argument("--baseline", action="store_true", help="T1–T3 基线（无需 API）")
    ap.add_argument("--vlm", action="store_true", help="T4：VLM 读单元格（需 API）")
    ap.add_argument("--limit", type=int, default=20, help="评估多少个表格页")
    ap.add_argument("--dump", action="store_true", help="导出表格结构供后续对比")
    args = ap.parse_args()

    if not any([args.scan, args.baseline, args.vlm]):
        ap.print_help()
        return 1

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    report: dict = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}

    if args.scan:
        report["scan"] = scan()

    if args.baseline or args.vlm:
        pages = pick_table_pages(args.limit)
        print(f"\n选中的测试页（{len(pages)} 页）：")
        for p, n in pages[:12]:
            print(f"    {p.name} p{n}")
        if len(pages) > 12:
            print(f"    … 共 {len(pages)} 页")
        if args.baseline:
            report["baseline"] = run_baseline(pages, save_cells=args.dump)
        if args.vlm:
            report["vlm_cells"] = run_vlm_cells(pages)

    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"\n完整报告：{RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
