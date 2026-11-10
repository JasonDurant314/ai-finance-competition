"""看内部冲突与主体 118034 的事实全貌，判断 R4 该用哪个值。"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

run = sys.argv[1] if len(sys.argv) > 1 else "final-full2"
entity = sys.argv[2] if len(sys.argv) > 2 else "118034"
base = Path("runs") / run

conf = json.loads((base / "facts" / "internal_conflicts.json").read_text(encoding="utf-8"))
print(f"内部冲突 {len(conf)} 条：")
for x in conf[:8]:
    print(f"   {x.get('entity')} / {x.get('metric')} / {x.get('period')}"
          f"  比值={x.get('ratio')}  值={x.get('values')}")

d = json.loads((base / "facts" / "facts.json").read_text(encoding="utf-8"))
facts = d["facts"] if isinstance(d, dict) else d
spans = {s["span_id"]: s for s in (d.get("evidence") or [])}

sub = [f for f in facts if f["entity"] == entity]
print(f"\n主体 {entity} 的事实 {len(sub)} 条：")
by: dict[str, list] = defaultdict(list)
for f in sub:
    by[f["metric"]].append(f)
for metric, fs in by.items():
    print(f"\n  {metric}（{len(fs)} 条）")
    for f in fs:
        sid = (f.get("evidence_span_ids") or [None])[0]
        sp = spans.get(sid) or {}
        flag = "需复核" if f.get("needs_review") else "      "
        print(f"    {flag} {str(f['value']):>18} {f['unit']:<4} conf={f['confidence']:.2f}"
              f" p{sp.get('page')} {str(f.get('extractor'))[:16]:<18}"
              f" 原文={str(f.get('raw_text'))[:18]!r}")
