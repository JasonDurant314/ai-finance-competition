"""量出「表内连续年份序列 → 列期间」这条规则的影响面。

为什么先有这个工具（2026-09 第十四轮）：

    第十三轮我加过一条守卫（"行内期间塌陷"），**写完之后**才在真实年报上量，
    结果 needs_review 从 159 涨到 529、可用 equity_parent 从 16 降到 0 ——
    一条守卫把"个别指标可能取错"放大成"整库不可用"，只能整条删除。

    **判据的杀伤半径必须在启用之前量出来。** 本工具就是那条纪律的落地：
    它复用缓存的表格记录调用同一个函数，逐表给出"会改哪些列、改成什么"，
    不重跑抽取（省 20 分钟 + ¥1）。

用法::

    python tools/year_sequence_report.py --run annual34
    python tools/year_sequence_report.py --run annual34 --show-grid --limit 12
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
    ap = argparse.ArgumentParser(
        description="量出「表内连续年份序列 → 列期间」的影响面"
    )
    ap.add_argument("--run", required=True, help="run_id 或 runs 下的路径")
    ap.add_argument("--runs-root", default="runs")
    ap.add_argument("--limit", type=int, default=20, help="打印多少条样例")
    ap.add_argument("--show-grid", action="store_true", help="样例里打印网格")
    ap.add_argument("--page", type=int, default=None)
    args = ap.parse_args()

    run_dir = Path(args.run)
    if not (run_dir / "channels" / "tables.json").is_file():
        run_dir = Path(args.runs_root) / args.run
    path = run_dir / "channels" / "tables.json"
    if not path.is_file():
        print(f"找不到表格缓存：{path}")
        return 2

    from pipelines.r1_extract.table_facts import (
        _footnote_row_start,
        _majority_data_columns,
        build_header,
        data_start_row,
        merge_fragments,
        periods_from_header_rows,
        periods_from_year_sequence,
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
    if args.page is not None:
        records = [r for r in records if int(r.get("page") or 0) == args.page]

    hits: list[tuple[dict, dict[int, str], int, set[int]]] = []
    reasons: Counter = Counter()
    for rec in records:
        grid = rec.get("grid") or []
        if not grid:
            continue
        fixed = [[repair_line_broken_numbers(c or "")[0] for c in row]
                 for row in grid]
        mg = merge_fragments(fixed).grid
        dsr = data_start_row(mg)
        # ⚠️ 必须与生产路径**完全一致**：先排除脚注行，再用"多数行"判据取数据列。
        #    早期版本这里用的是 `_data_columns`（"任一行"），测出来的命中数
        #    与生产不符 —— 工具与生产口径不一致时，量出来的数没有意义。
        note_at = _footnote_row_start(mg, dsr)
        body = mg[:note_at]
        n_cols = max((len(r) for r in body), default=0)
        dcols = _majority_data_columns(body, dsr, n_cols)
        # ★ 与生产同一条守卫：只在**期间碰撞**（同一个期间落到两个数据列上）时介入。
        #   工具与生产口径不一致时，量出来的数没有意义（实测踩过一次）。
        hdr, hds = build_header(mg, {})
        cp = periods_from_header_rows(
            [list(r) for r in mg[:hds]] or [hdr],
            doc_period="", data_cols=set(range(n_cols)), grid=mg, data_start=hds,
        )
        used = [cp[j] for j in sorted(dcols) if cp.get(j)]
        if len(used) == len(set(used)):
            reasons["期间无碰撞（映射已正确）"] += 1
            continue
        # 年份在**脚注**里 → 扫整表；数据列在脚注前 → 用 body。
        seq = periods_from_year_sequence(body, data_cols=dcols, year_grid=mg)
        if seq:
            hits.append((rec, seq, dsr, dcols))

    print(f"缓存表数：{len(records)}")
    print(f"★ 规则命中：{len(hits)} 张表"
          f"（{len(hits)/max(1,len(records)):.1%}）")
    if not hits:
        return 0

    per_year = Counter()
    for _rec, seq, _dsr, _dc in hits:
        for p in seq.values():
            per_year[p] += 1
    print("\n会赋出的期间分布（前 10）：")
    for k, v in sorted(per_year.items(), reverse=True)[:10]:
        print(f"   {k:<8}{v:>5} 列")

    by_doc = Counter()
    for rec, _seq, _dsr, _dc in hits:
        by_doc[doc_name.get(rec.get("doc_id") or "", "?")[:22]] += 1
    print("\n按文档：")
    for k, v in by_doc.most_common():
        print(f"   {k:<26}{v:>5} 张")

    print(f"\n=== 样例（前 {args.limit} 张）===")
    for rec, seq, dsr, dcols in hits[: args.limit]:
        name = doc_name.get(rec.get("doc_id") or "", "")[:16]
        print(f"\n  p{rec.get('page')} 表{rec.get('table_index')} [{name}] "
              f"data_start={dsr} 数据列={sorted(dcols)}")
        print(f"     → 期间 = {dict(sorted(seq.items()))}")
        if args.show_grid:
            grid = rec.get("grid") or []
            for i, row in enumerate(grid[:6]):
                mark = "*" if i == dsr else " "
                print(f"      {mark}r{i} | "
                      + " | ".join(str(c)[:12] for c in row[:10]))

    # ★ 一致性抽查：年份是否与**文档名里的年份**对得上？
    #
    #   这是判断规则是否错位最直接的一条：2025 年报里的表不应给出 2024A..
    #   文档名里没有 4 位年份时跳过（判不了就不判）。
    print("\n=== 年份 vs 文档年份 一致性抽查 ===")
    import re as _re
    bad = 0
    for rec, seq, _dsr, _dc in hits:
        name = doc_name.get(rec.get("doc_id") or "", "")
        m = _re.search(r"(20\d{2})\s*年(?:度)?年报", name)
        if not m:
            continue
        doc_year = int(m.group(1))
        newest = max(int(p[:4]) for p in seq.values())
        if newest != doc_year:
            bad += 1
            print(f"  ⚠️ p{rec.get('page')} 表{rec.get('table_index')} "
                  f"[{name[:30]}] 文档年={doc_year} 但最新期间={newest}")
    print(f"  不一致 {bad} 张（文档名含年份且可比的部分）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
