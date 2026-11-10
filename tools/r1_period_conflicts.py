"""R1 期间标注诊断：找出"同一主体同一指标在同年内出现多个互不相同的值"的情况。

这是"期间标注错"最强的结构性信号 —— 一个科目在同一期间不该有两个不同的数。
用法::

    python tools/r1_period_conflicts.py --run annual23 --metric total_assets
    python tools/r1_period_conflicts.py --run annual23 --min-ratio 1.2
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from agent.console import enable_utf8_output  # noqa: E402

# Windows 控制台默认 GBK：本脚本会打印 ✓ / ✅ 这类符号，
# 不先切 UTF-8 就会在**最需要它的时候**抛 UnicodeEncodeError 中断。
enable_utf8_output()


def main() -> int:
    ap = argparse.ArgumentParser(description="R1 期间标注冲突诊断")
    ap.add_argument("--run", required=True)
    ap.add_argument("--runs-root", default="runs")
    ap.add_argument("--metric", default=None, help="只看某个指标")
    ap.add_argument("--min-ratio", type=float, default=1.5,
                    help="同期间内两个值的最大/最小之比超过它才算冲突")
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()

    run_dir = Path(args.run)
    if not (run_dir / "facts" / "facts.json").is_file():
        run_dir = Path(args.runs_root) / args.run
    facts = json.loads((run_dir / "facts" / "facts.json").read_text(encoding="utf-8"))["facts"]
    docs = json.loads((run_dir / "facts" / "documents.json").read_text(encoding="utf-8"))
    dmap = {d["doc_id"]: str(d.get("filename") or "")[:26] for d in docs}

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for f in facts:
        if f.get("needs_review"):
            continue
        if args.metric and f["metric"] != args.metric:
            continue
        groups[(f["doc_id"], str(f["entity"]), f["metric"], str(f["period"]))].append(f)

    rows = []
    for key, items in groups.items():
        vals = []
        for f in items:
            try:
                vals.append(abs(Decimal(str(f["value"]))))
            except Exception:  # noqa: BLE001
                continue
        vals = [v for v in vals if v > 0]
        if len(vals) < 2:
            continue
        ratio = max(vals) / min(vals)
        if ratio >= Decimal(str(args.min_ratio)):
            rows.append((ratio, key, items))

    rows.sort(key=lambda x: -x[0])
    print(f"{run_dir.name}: {len(rows)} 组「同主体同期间同指标却取值不一」"
          f"（比值 ≥ {args.min_ratio}），取前 {args.top} 组\n")
    for ratio, key, items in rows[: args.top]:
        doc_id, entity, metric, period = key
        print(f"■ {metric}  {period}  比值 {float(ratio):.1f}×  "
              f"实体={entity[:22]!r}  {dmap.get(doc_id, doc_id)}")
        for f in sorted(items, key=lambda x: -abs(float(x["value"]))):
            pages = [ch.get("page") for ch in (f.get("channel_readings") or [])]
            raws = [ch.get("raw_text") for ch in (f.get("channel_readings") or [])]
            print(f"    {str(f['value']):>20} {f['unit']:<4} p={pages} "
                  f"raw={raws} scope={str(f['statement_scope'])[:12]:<13} {f['fact_id'][:22]}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
