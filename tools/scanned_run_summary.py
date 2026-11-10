"""扫描件（无文本层）一次性跑通的情况：OCR、表格、事实、几何佐证来源。"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

run = sys.argv[1] if len(sys.argv) > 1 else "scan-probe"
base = Path("runs") / run

docs = json.loads((base / "facts" / "documents.json").read_text(encoding="utf-8"))
for d in docs:
    print(f"文档 {d['filename']}  页数={d.get('page_count')}  "
          f"文本层={d.get('has_text_layer')}")
    print(f"  路由：{dict(Counter(d.get('page_routes', {}).values()))}")

ocr = json.loads((base / "channels" / "ocr.json").read_text(encoding="utf-8"))
print(f"\nOCR：{len(ocr)} 页")
if ocr:
    r0 = ocr[0]
    print(f"   引擎={r0.get('engine')} dpi={r0.get('dpi')} 行数={r0.get('line_count')} "
          f"平均置信度={r0.get('mean_confidence')}")
    print(f"   样例行：{str((r0.get('lines') or [{}])[0].get('text'))[:40]!r}")

tables = json.loads((base / "channels" / "tables.json").read_text(encoding="utf-8"))
print(f"\n表格：{len(tables)} 张  ← 扫描件靠矢量线段重建，预期为 0")

st = json.loads((base / "facts" / "build_stats.json").read_text(encoding="utf-8"))
print(f"\n事实统计：规则={st.get('accepted')} 表格={st.get('table_grid_accepted')} "
      f"VLM={st.get('vlm_accepted')}")

fd = json.loads((base / "facts" / "facts.json").read_text(encoding="utf-8"))
facts = fd["facts"] if isinstance(fd, dict) else fd
print(f"事实总数 {len(facts)}；来源层 {dict(Counter(f['source_layer'] for f in facts))}")

# 几何佐证来源
ev = [json.loads(l) for l in (base / "trace.jsonl").read_text(encoding="utf-8").splitlines()
      if l.strip()]
geo = [(e.get("extra") or {}).get("detail", "") for e in ev
       if e.get("kind") == "stage" and "几何佐证" in str((e.get("extra") or {}).get("detail"))]
print(f"\n几何佐证来源切换事件：{len(geo)} 条")
for g in geo[:3]:
    print(f"   {g}")
