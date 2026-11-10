"""汇总所有 run 的 API 开销（token 与人民币）。"""

from __future__ import annotations

import json
from pathlib import Path

rows = []
tok = cost = 0
for d in sorted(Path("runs").iterdir()):
    m = d / "manifest.json"
    if not m.is_file():
        continue
    j = json.loads(m.read_text(encoding="utf-8"))
    c = float(j.get("total_cost_cny") or 0)
    t = int(j.get("total_tokens") or 0)
    if c or t:
        rows.append((d.name, j.get("pipeline", ""), t, c))
        tok += t
        cost += c

print(f"{'run':<18}{'pipeline':<14}{'tokens':>10}   CNY")
for n, p, t, c in sorted(rows, key=lambda r: -r[3]):
    print(f"{n:<18}{p:<14}{t:>10}   {c:.4f}")
print("-" * 52)
print(f"{'合计':<18}{'':<14}{tok:>10}   {cost:.4f}")
