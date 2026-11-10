"""列出 runs/ 下的产物概况（表格数、是否有估值/核查产物）。"""

from __future__ import annotations

import json
from pathlib import Path


def main() -> int:
    root = Path("runs")
    print(f"{'run':<20}{'pipeline':<14}{'status':<10}{'tables':>7}{'adjs':>6}  val  audit")
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        m = d / "manifest.json"
        info = json.loads(m.read_text(encoding="utf-8")) if m.is_file() else {}
        ch = d / "channels"
        t = ch / "tables.json"
        a = ch / "table_adjudications.json"
        n = len(json.loads(t.read_text(encoding="utf-8"))) if t.is_file() else 0
        k = len(json.loads(a.read_text(encoding="utf-8"))) if a.is_file() else 0
        print(
            f"{d.name:<20}{info.get('pipeline', ''):<14}{info.get('status', ''):<10}"
            f"{n:>7}{k:>6}  {'Y' if (d / 'valuation').is_dir() else '-':<4} "
            f"{'Y' if (d / 'audit').is_dir() else '-'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
