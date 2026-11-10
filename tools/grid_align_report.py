"""列对齐覆盖率 —— 一条命令回答「按坐标重建列结构到底生效了多少」。

为什么要有这个工具（2026-09 第十二轮的教训）：

    首次把坐标对齐接进生产路径、跑完真实年报后，我在产物里**查不到任何
    "它是否生效"的统计** —— 加在 `tstats` 里的计数键被 `_ingest_table_grid`
    返回的精选字典吞掉了。后果很具体：**一个从未触发的分支与一个一直在
    工作的分支，在产物里长得一模一样。**

    本项目原则「每个被吞掉的异常都要有可见计数器」，同一条也适用于
    "某个改进到底有没有真的生效"。该计数器现已修好（`build_stats.json`
    里的 `table_grid_aligned`），本工具则用来做**不依赖重跑**的独立复核：
    直接复用缓存下来的表格记录调用同一个函数，逐表给出结论。

用法::

    python tools/grid_align_report.py --run annual29
    python tools/grid_align_report.py --run annual29 --page 178 --show-grid
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from agent.console import enable_utf8_output  # noqa: E402

# Windows 控制台默认 GBK：本脚本会打印 ✓ / ✅ 这类符号，
# 不先切 UTF-8 就会在**最需要它的时候**抛 UnicodeEncodeError 中断。
enable_utf8_output()

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="统计按坐标重建列结构的覆盖率")
    ap.add_argument("--run", required=True, help="run_id 或 runs 下的路径")
    ap.add_argument("--runs-root", default="runs")
    ap.add_argument("--page", type=int, default=None, help="只看某页")
    ap.add_argument("--doc", default=None, help="只看文件名含该串的文档")
    ap.add_argument("--show-grid", action="store_true", help="打印对齐前后的网格")
    args = ap.parse_args()

    run_dir = Path(args.run)
    if not (run_dir / "channels" / "tables.json").is_file():
        run_dir = Path(args.runs_root) / args.run
    path = run_dir / "channels" / "tables.json"
    if not path.is_file():
        print(f"找不到表格缓存：{path}")
        return 2

    from pipelines.r1_extract.grid_align import align_grid_by_bbox
    from pipelines.r1_extract.table_facts import (
        data_start_row,
        merge_fragments,
        repair_line_broken_numbers,
    )

    records = json.loads(path.read_text(encoding="utf-8"))
    doc_name: dict[str, str] = {}
    dpath = run_dir / "facts" / "documents.json"
    if dpath.is_file():
        doc_name = {
            d["doc_id"]: d["filename"]
            for d in json.loads(dpath.read_text(encoding="utf-8"))
        }

    if args.doc:
        records = [
            r for r in records
            if args.doc in doc_name.get(r.get("doc_id") or "", "")
        ]
    if args.page is not None:
        records = [r for r in records if int(r.get("page") or 0) == args.page]

    if not records:
        print("没有匹配的表格记录。")
        return 0

    applied = 0
    reasons: Counter = Counter()
    col_counts: Counter = Counter()
    examples: list[str] = []

    for rec in records:
        grid = rec.get("grid") or []
        cells = rec.get("cells") or []
        if not grid or not cells:
            reasons["无网格或无单元格坐标"] += 1
            continue
        fixed = [[repair_line_broken_numbers(c or "")[0] for c in row] for row in grid]
        mg = merge_fragments(fixed)
        res = align_grid_by_bbox(
            mg.grid, mg.col_maps, cells, data_start=data_start_row(mg.grid)
        )
        tag = (f"p{rec.get('page')} 表{rec.get('table_index')} "
               f"[{doc_name.get(rec.get('doc_id') or '', '')[:24]}]")
        if res.applied:
            applied += 1
            col_counts[res.n_cols] += 1
            if len(examples) < 8:
                before = max((len(r) for r in mg.grid), default=0)
                examples.append(f"    {tag} 原 {before} 列 → 对齐 {res.n_cols} 列")
            if args.show_grid:
                print(f"\n### {tag}  → {res.n_cols} 列")
                for i, r in enumerate(res.grid[:10]):
                    mark = "*" if i == data_start_row(res.grid) else " "
                    print(f"   {mark} | " + " | ".join(str(c)[:12] for c in r))
        else:
            reasons[res.reason[:16]] += 1
            if args.show_grid:
                print(f"\n### {tag}  → 未对齐：{res.reason}")

    print(f"\n缓存表数：{len(records)}")
    print(f"★ 对齐生效：{applied} / {len(records)}"
          f"（{applied / len(records):.1%}）")
    if applied:
        print(f"  重建列数合计：{sum(k * v for k, v in col_counts.items())}")
        print("  列数分布：" + "，".join(
            f"{k} 列 {v} 张" for k, v in col_counts.most_common(6)))
    if examples:
        print("\n样例：")
        print("\n".join(examples))
    if reasons:
        print("\n未对齐的原因分布：")
        for k, v in reasons.most_common(8):
            print(f"  {v:>5}  {k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
