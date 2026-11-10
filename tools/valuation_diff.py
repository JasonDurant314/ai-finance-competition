"""对比两次 full run 的估值输入，解释每股价值为何变化。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

a = sys.argv[1] if len(sys.argv) > 1 else "final-full"
b = sys.argv[2] if len(sys.argv) > 2 else "final-full2"


def load(run: str, rel: str):
    p = Path("runs") / run / rel
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


for run in (a, b):
    v = load(run, "valuation/valuation.json")
    inp = load(run, "valuation/assumptions.json")
    print(f"\n=== {run} ===")
    if v:
        print(f"  估值对象 {v.get('entity')}  每股价值 {v.get('dcf', {}).get('value_per_share')}")
        d = v.get("dcf") or {}
        for k in ("base_revenue", "enterprise_value", "equity_value",
                  "shares_outstanding", "net_debt", "wacc", "terminal_growth"):
            if k in d:
                print(f"    {k:<20}{d[k]}")
    if inp:
        t = inp.get("targets") or {}
        print(f"  targets: " + ", ".join(f"{k}={v}" for k, v in list(t.items())[:8]))
        print(f"  假设 {len(inp.get('assumptions') or [])} 条")

# 事实层面的差异
for run in (a, b):
    fs = load(run, "facts/facts.json")
    facts = (fs or {}).get("facts") or []
    sub = [f for f in facts if f["entity"] == "118034"]
    from collections import Counter
    print(f"\n{run}: 主体 118034 的事实 {len(sub)} 条")
    print("   ", dict(Counter(f["metric"] for f in sub)))
    print("   需复核:", sum(1 for f in sub if f.get("needs_review")))
