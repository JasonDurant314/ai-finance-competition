"""两件事一起量，回答「限制① 到底该怎么修」：

  A. 数字折行修复在**真实表格**上的修复率（决定要不要接进表格链路）
  B. 语料里有多少张表是「指标在行/列、期间在另一维」的**财务报表式**表格
     —— 只有这种表才可能安全地回灌事实库（值属于本主体）

  B 很关键：被担保人清单表里也有 资产总额/净利润 列，但那些数是**子公司的**，
  当成披露主体的财务数据入库就是错的。所以先量清楚比例，再决定做不做。
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz  # noqa: E402

from agent.config import load_config  # noqa: E402
from agent.validate.numeric import repair_line_broken_numbers  # noqa: E402
from domain.tools.tables import tables_of_page  # noqa: E402
from tools.table_benchmark import pick_table_pages  # noqa: E402

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 40

metrics_cfg = load_config("metrics")
alias_map: dict[str, str] = {}
for m in metrics_cfg.get("metrics", []):
    for a in [m.get("label", m["canonical"])] + list(m.get("aliases", [])):
        if a:
            alias_map[a] = m["canonical"]
aliases = sorted(alias_map, key=len, reverse=True)


def hits_in(text: str) -> set[str]:
    t = (text or "").replace("\n", "")
    return {alias_map[a] for a in aliases if a in t}


pages = pick_table_pages(LIMIT)
print(f"抽样表格页 {len(pages)} 个\n")

frag = frag_fixed = 0
kinds: Counter[str] = Counter()
examples: list[str] = []

for pdf, pno in pages:
    try:
        with fitz.open(pdf) as doc:
            page = doc.load_page(pno - 1)
            grid_tables = tables_of_page(page, page_no=pno)
    except Exception:
        continue
    for t in grid_tables[:2]:
        g = t.grid()
        if not g or not g[0]:
            continue
        # A. 碎格修复率
        for c in t.cells:
            if "\n" in c.text:
                frag += 1
                fixed, ch = repair_line_broken_numbers(c.text)
                if ch and "\n" not in fixed:
                    frag_fixed += 1

        # B. 表格形态判定
        col0 = {k for i in range(1, len(g)) if g[i] for k in hits_in(g[i][0])}
        head = {k for c in g[0] for k in hits_in(c)}
        if head:
            head2 = {k for c in (g[1] if len(g) > 1 else []) for k in hits_in(c)}
            head |= head2
        if col0 and head:
            kind = "双向都有（口径待定）"
        elif col0:
            kind = "指标在**行**（财务报表式）"
        elif head:
            kind = "指标在**列**（清单式/被担保人式）"
        else:
            kind = "无受跟踪指标"
        kinds[kind] += 1
        if kind != "无受跟踪指标" and len(examples) < 8:
            examples.append(f"{pdf.name[:34]} p{pno} {t.n_rows}×{t.n_cols} → {kind}")

print(f"A. 表格碎格 {frag} 个，其中可被确定性修复 {frag_fixed} 个"
      f"（{frag_fixed / max(frag, 1):.1%}）")

print("\nB. 表格形态分布：")
for k, v in kinds.most_common():
    print(f"   {k:<28}{v:>4} 张")

print("\n形态样例：")
for e in examples:
    print("   " + e)
