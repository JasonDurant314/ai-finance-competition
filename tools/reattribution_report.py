"""查看主体归属纠正的明细。"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

run = sys.argv[1] if len(sys.argv) > 1 else "table-grid5"
base = Path("runs") / run

st = json.loads((base / "facts" / "build_stats.json").read_text(encoding="utf-8"))
print("entity_reattributed =", st.get("entity_reattributed"))

p = base / "facts" / "entity_reattribution.json"
if not p.is_file():
    print("没有改动明细文件")
    raise SystemExit(0)

d = json.loads(p.read_text(encoding="utf-8"))
print(f"改动明细 {len(d)} 条，样例：")
for r in d[:8]:
    print(f"   p{r['page']} r{r['row']}c{r['col']}  {r['from']} → {r['to']}"
          f"  ({r['metric']} {str(r['raw_text'])[:22]})")
print("\n改后主体分布（前 10）：")
for k, v in Counter(r["to"] for r in d).most_common(10):
    print(f"   {v:>4}  {k}")
print("\n按指标：")
for k, v in Counter(r["metric"] for r in d).most_common(10):
    print(f"   {v:>4}  {k}")
