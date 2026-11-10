"""看一次 full run 的勾稽断言与 R5 核查结论。"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

run = sys.argv[1] if len(sys.argv) > 1 else "final-full"
base = Path("runs") / run

v = json.loads((base / "valuation" / "valuation.json").read_text(encoding="utf-8"))
asserts = v.get("assertions") or []
print(f"勾稽断言 {len(asserts)} 条：{dict(Counter(a.get('status') for a in asserts))}")
for a in asserts:
    if a.get("status") != "pass":
        print(f"   {a.get('code')} {a.get('name')} [{a.get('status')}]"
              f" {str(a.get('detail'))[:80]}")

print(f"\n每股价值 {v.get('dcf', {}).get('value_per_share')}")
print(f"估值对象 {v.get('entity')}")

audit = base / "audit" / "audit.json"
if audit.is_file():
    a = json.loads(audit.read_text(encoding="utf-8"))
    fs = a.get("findings") or []
    print(f"\nR5 核查发现 {len(fs)} 条：{dict(Counter(f.get('error_type') for f in fs))}")
    for f in fs[:6]:
        print(f"   [{f.get('severity')}] {f.get('error_type')} "
              f"{str(f.get('reason'))[:90]}")
