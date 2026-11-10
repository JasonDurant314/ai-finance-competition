"""对比一次运行的抽取来源构成，重点看表格网格抽取的效果。"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

run = sys.argv[1] if len(sys.argv) > 1 else "table-grid1"
base = Path("runs") / run

st_p = base / "facts" / "build_stats.json"
if st_p.is_file():
    st = json.loads(st_p.read_text(encoding="utf-8"))
    print("build_stats 关键项：")
    for k, v in st.items():
        if "table_grid" in k or k in ("accepted", "vlm_accepted", "candidates",
                                      "rejected_no_unit", "rejected_quote",
                                      "needs_review"):
            print(f"   {k:<38}{v}")

facts_doc = json.loads((base / "facts" / "facts.json").read_text(encoding="utf-8"))
facts = facts_doc["facts"] if isinstance(facts_doc, dict) else facts_doc
print(f"\n事实总数 {len(facts)}")
print("提取器：", dict(Counter(f.get("extractor") for f in facts)))
print("来源层：", dict(Counter(f.get("source_layer") for f in facts)))

tb = [f for f in facts if str(f.get("extractor", "")).startswith("table_grid")]
print(f"\n表格网格事实 {len(tb)} 条，样例：")
for f in tb[:10]:
    print(f"   {f['entity'][:26]:<28}{f['metric']:<20}{f['raw_text']:<12}"
          f"{f['value']} {f['unit']}  conf={f['confidence']:.2f}")

# 与 VLM 事实交叉：同主体同指标同值 → 互为佐证
vlm = {(f["doc_id"], f["entity"], f["metric"], str(f["value"]))
       for f in facts if str(f.get("extractor", "")).startswith("vlm")}
agree = {(f["doc_id"], f["entity"], f["metric"], str(f["value"])) for f in tb} & vlm
print(f"\n与 VLM 读数**完全一致**的表格事实：{len(agree)} 条"
      f"（表格 {len(tb)} 条中占 {len(agree) / max(len(tb), 1):.0%}）")
