"""看双路互证加权到底落在了哪些事实上。"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

run = sys.argv[1] if len(sys.argv) > 1 else "table-dual9"
base = Path("runs") / run

st = json.loads((base / "facts" / "build_stats.json").read_text(encoding="utf-8"))
print(f"cross_validated_keys  = {st.get('cross_validated_keys')}")
print(f"cross_validated_facts = {st.get('cross_validated_facts')}")

d = json.loads((base / "facts" / "facts.json").read_text(encoding="utf-8"))
facts = d["facts"] if isinstance(d, dict) else d

boosted = [f for f in facts
           if any("双路互证" in a.get("reason", "")
                  for a in (f.get("confidence_breakdown") or {}).get("adjustments", []))]
print(f"\n实际带「双路互证」加分项的事实：{len(boosted)} 条")
print("按来源层：", dict(Counter(f["source_layer"] for f in boosted)))

print("\n样例（含完整置信度明细）：")
for f in boosted[:6]:
    bd = f["confidence_breakdown"]
    detail = " + ".join(f"{a['reason'][:16]}{a['delta']:+.2f}"
                        for a in bd["adjustments"])
    print(f"   {str(f['entity'])[:18]:<20}{f['metric']:<20}{f['value']:>16}")
    print(f"      base={bd['base']}  {detail}  => {f['confidence']}")

# 对照组：未被互证的模型事实
vlm_only = [f for f in facts if str(f.get("extractor", "")).startswith("vlm")
            and f not in boosted]
print(f"\n对照组：只被模型读到的 {len(vlm_only)} 条")
for f in vlm_only[:3]:
    print(f"   {str(f['entity'])[:18]:<20}{f['metric']:<20}{f['value']:>16}"
          f"  置信度={f['confidence']}")
