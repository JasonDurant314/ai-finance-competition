"""指标候选速查 —— 摊开某个指标在事实库里的全部候选与复核标记。

为什么要有这个工具：排查"某个数为什么没进来/为什么取错"时，反复手写脚本
既慢又容易写错（本项目已多次在同一类脚本上浪费时间）。这个工具把
"看候选"变成一条命令。

用法::

    python tools/fact_probe.py --run annual27 --metric capex
    python tools/fact_probe.py --run annual27 --metric revenue --entity 601857 --period 2025A
    python tools/fact_probe.py --run annual27 --metric depreciation_amortization --show-reason
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# Windows 控制台默认 GBK，复核原因里含全角/箭头字符会直接抛 UnicodeEncodeError
# 把工具本身打断（实测踩过）。显式降级而不是让排查工具崩掉。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - 非文本流
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="摊开某指标的事实库候选")
    ap.add_argument("--run", required=True, help="run_id 或 runs 下的路径")
    ap.add_argument("--runs-root", default="runs")
    ap.add_argument("--metric", required=True)
    ap.add_argument("--entity", default=None, help="只看某个主体")
    ap.add_argument("--period", default=None, help="只看某个期间")
    ap.add_argument("--page", type=int, default=None, help="只看某一页")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--show-reason", action="store_true", help="打印复核原因")
    args = ap.parse_args()

    run_dir = Path(args.run)
    if not (run_dir / "facts" / "facts.json").is_file():
        run_dir = Path(args.runs_root) / args.run
    path = run_dir / "facts" / "facts.json"
    if not path.is_file():
        print(f"找不到事实库：{path}")
        return 2

    facts = json.loads(path.read_text(encoding="utf-8"))["facts"]
    rows = [f for f in facts if f["metric"] == args.metric]
    if args.entity:
        rows = [f for f in rows if str(f.get("entity")) == args.entity]
    if args.period:
        rows = [f for f in rows if str(f.get("period")) == args.period]
    if args.page is not None:
        rows = [
            f for f in rows
            if ((f.get("channel_readings") or [{}])[0].get("page")) == args.page
        ]

    print(f"{run_dir.name} / {args.metric}"
          + (f" / {args.entity}" if args.entity else "")
          + (f" / {args.period}" if args.period else "")
          + (f" / p{args.page}" if args.page is not None else "")
          + f"：{len(rows)} 条"
          f"（needs_review {sum(1 for f in rows if f['needs_review'])}）\n")
    if not rows:
        return 0

    print("期间分布:", dict(sorted(Counter(str(f["period"]) for f in rows).items())))
    print("主体分布:", dict(Counter(str(f["entity"]) for f in rows).most_common(6)))
    print()

    for f in sorted(rows, key=lambda x: -abs(Decimal(str(x["value"]))))[: args.top]:
        ch = (f.get("channel_readings") or [{}])[0]
        scope = str(f.get("statement_scope"))
        print(f"  {str(f['value']):>20} {str(f['unit']):<5} {str(f['period']):<7} "
              f"scope={scope[:12]:<13} p{str(ch.get('page')):<5} "
              f"raw={str(ch.get('raw_text'))[:22]!r:<24} "
              f"{'REV' if f['needs_review'] else '   '} {f['fact_id'][:20]}")
        label = ch.get("row_label") or ch.get("note") or ""
        if label:
            print(f"        row={label[:70]!r} col={str(ch.get('col_header') or '')[:34]!r}")
        if args.show_reason and f.get("review_reason"):
            print(f"        ↳ {f['review_reason'][:150]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
