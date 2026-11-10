"""表格算术校验的「修复前 vs 修复后」审计器。

用法::

    python tools/table_arith_audit.py --run table-vlm2
    python tools/table_arith_audit.py --run table-vlm2 --dump 3    # 展开前 3 张表的网格
    python tools/table_arith_audit.py --run table-vlm2 --json docs/table_arith_audit.json

为什么需要它：``channels/tables.json`` 里既存了每张表的网格，也存了**当时的**
算术校验结论。因此可以对着同一批真实表格，把旧实现的结论与新实现现场重算的
结论摆在一起比对 —— 这是判定"这次修改到底是修好了还是改坏了"的唯一硬证据。

实测结论（48 张真实公告表格）：旧实现报出 40 处"算术不平"，逐条核对后
**全部是误报** —— 它把每张表的最后一行无条件当成合计行，而股权变动表的
最后一行是另一家子公司。新实现改为**认合计标签**，同一批数据零误报。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.console import enable_utf8_output  # noqa: E402
from domain.tools.tables import (  # noqa: E402
    cell_number,
    check_table_arithmetic,
    total_label_kind,
)
from pipelines.r1_extract.judge import _table_from_record  # noqa: E402


def _resolve(run: str) -> Path:
    p = Path(run)
    if not p.is_absolute():
        cand = ROOT / "runs" / run
        p = cand if cand.is_dir() else ROOT / p
    tables = p / "channels" / "tables.json"
    if not tables.is_file():
        raise SystemExit(f"找不到表格产物：{tables}\n（该运行可能没有表格页）")
    return tables


def audit(tables_path: Path, *, dump: int = 0, top: int = 25) -> dict[str, Any]:
    recs = json.loads(tables_path.read_text(encoding="utf-8"))

    before_fail = before_checked = 0
    after_fail = after_rows = after_cols = after_soft = 0
    after_fail_tables = 0
    labeled = 0
    old_tables_with_claims = 0
    samples: list[dict[str, Any]] = []
    dumps: list[str] = []

    for r in recs:
        old = r.get("arithmetic") or {}
        old_n = int(old.get("failure_count", 0) or 0)
        before_fail += old_n
        before_checked += int(old.get("checked_rows", 0) or 0) + int(
            old.get("checked_cols", 0) or 0
        )
        if old_n:
            old_tables_with_claims += 1

        t = _table_from_record(r)
        chk = check_table_arithmetic(t)
        after_fail += len(chk.failures)
        after_rows += chk.checked_rows
        after_cols += chk.checked_cols
        after_soft += len(chk.soft)
        if chk.failures:
            after_fail_tables += 1
            for f in chk.failures[:3]:
                samples.append(
                    {
                        "page": r.get("page"),
                        "label": f.get("label"),
                        "kind": f["kind"],
                        "expected": f["expected"],
                        "computed": f["computed"],
                        "diff": f["diff"],
                        "n_terms": len(f["terms"]),
                    }
                )

        grid = t.grid()
        if any(total_label_kind(c) for row in grid for c in row):
            labeled += 1

        if len(dumps) < dump and old_n:
            lines = [
                f"\n=== page={r.get('page')} {r.get('n_rows')}x{r.get('n_cols')} "
                f"旧实现报 {old_n} 处不平 ==="
            ]
            for row in grid[:8]:
                lines.append("   | " + " | ".join((c or "")[:12].replace("\n", "⏎")
                                                   for c in row[:9]))
            for f in (old.get("failures") or [])[:6]:
                lines.append(
                    f"   -> 旧: kind={f.get('kind')} row={f.get('row')} col={f.get('col')} "
                    f"期望={f.get('expected')} 算得={f.get('computed')}"
                )
            dumps.append("\n".join(lines))

    return {
        "tables": len(recs),
        "tables_with_total_label": labeled,
        "before": {
            "failures": before_fail,
            "checked": before_checked,
            "tables_with_claims": old_tables_with_claims,
        },
        "after": {
            "failures": after_fail,
            "checked_rows": after_rows,
            "checked_cols": after_cols,
            "checked": after_rows + after_cols,
            "soft_nested": after_soft,
            "tables_with_claims": after_fail_tables,
        },
        "failures_top": samples[:top],
        "dumps": dumps,
    }


def adjudication_summary(run_dir: Path, *, examples: int = 12) -> dict[str, Any] | None:
    """汇总单元格级裁决结果。没有该产物时返回 None。"""
    p = run_dir / "channels" / "table_adjudications.json"
    if not p.is_file():
        return None
    try:
        adjs = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not adjs:
        return {
            "tables": 0, "verdicts": {}, "examples": [],
            "note": "本次运行没有产生裁决记录",
        }

    verdicts: dict[str, int] = {}
    by_table: list[dict[str, Any]] = []
    examples_out: list[dict[str, Any]] = []
    triggers: dict[str, int] = {}
    recovered = 0
    for rec in adjs:
        rep = rec.get("report") or {}
        triggers[rec.get("trigger", "?")] = triggers.get(rec.get("trigger", "?"), 0) + 1
        for k in ("agree", "normalized", "corrected", "rejected", "unreadable", "errors"):
            verdicts[k] = verdicts.get(k, 0) + int(rep.get(k, 0) or 0)
        by_table.append({
            "page": rec.get("page"),
            "table_index": rec.get("table_index"),
            "trigger": rec.get("trigger"),
            "checked": rep.get("cells_checked", 0),
            "agree": rep.get("agree", 0),
            "normalized": rep.get("normalized", 0),
            "corrected": rep.get("corrected", 0),
            "rejected": rep.get("rejected", 0),
            "improved": rep.get("improved"),
        })
        for a in rep.get("adjudications", []):
            if a.get("verdict") == "normalized":
                # 归一化的**实际收益**：原本解析不出数值的格子恢复了可用数值
                if cell_number(a.get("original", "")) is None:
                    recovered += 1
            if a.get("verdict") in ("normalized", "corrected") and len(examples_out) < examples:
                examples_out.append({
                    "page": rec.get("page"),
                    "row": a.get("row"), "col": a.get("col"),
                    "original": a.get("original"), "vlm_text": a.get("vlm_text"),
                    "verdict": a.get("verdict"),
                    "confidence": a.get("confidence"), "quote": a.get("quote"),
                    "was_unparseable": cell_number(a.get("original", "")) is None,
                })

    return {
        "tables": len(adjs),
        "triggers": triggers,
        "verdicts": verdicts,
        "cells_checked": sum(t["checked"] for t in by_table),
        "cells_recovered": recovered,
        "by_table": by_table,
        "examples": examples_out,
    }


def main() -> int:
    enable_utf8_output()
    ap = argparse.ArgumentParser(description="表格质量审计：算术校验前后对比 + 单元格裁决汇总")
    ap.add_argument("--run", default="table-vlm2", help="运行 id 或路径")
    ap.add_argument("--dump", type=int, default=0, help="展开前 N 张旧实现报错的表格网格")
    ap.add_argument("--json", default="", help="把结果写成 JSON")
    args = ap.parse_args()

    tables_path = _resolve(args.run)
    run_dir = tables_path.parent.parent
    res = audit(tables_path, dump=args.dump)
    b, a = res["before"], res["after"]

    print(f"运行目录表格数              {res['tables']}")
    print(f"含「合计」标签的表格        {res['tables_with_total_label']}")
    print()
    print(f"{'':26}{'修复前':>10}{'修复后':>10}")
    print(f"{'算术不平处数':26}{b['failures']:>10}{a['failures']:>10}")
    print(f"{'已校验合计线':26}{b['checked']:>10}{a['checked']:>10}")
    print(f"{'  其中纵向（列合计）':26}{'':>10}{a['checked_cols']:>10}")
    print(f"{'  其中横向（行合计）':26}{'':>10}{a['checked_rows']:>10}")
    print(f"{'  放过（分层小计/多行表头）':22}{'':>10}{a['soft_nested']:>10}")
    print(f"{'报出问题的表格数':26}{b['tables_with_claims']:>10}"
          f"{a['tables_with_claims']:>10}")

    if res["failures_top"]:
        print("\n不平明细（修复后）：")
        for s in res["failures_top"]:
            print(
                f"  p{s['page']:<4} {s['kind']:<8} {str(s['label'])[:16]:<18}"
                f" 表内合计={s['expected']:>16} 分项和={s['computed']:>16}"
                f" 差={s['diff']:>14} 分项数={s['n_terms']}"
            )
    else:
        print("\n修复后：没有报出任何算术不平。")

    for d in res["dumps"]:
        print(d)

    adj = adjudication_summary(run_dir)
    if adj is not None:
        print("\n" + "=" * 66)
        print("单元格级视觉裁决")
        print("=" * 66)
        if adj["tables"] == 0:
            print("本次运行没有产生裁决记录。")
        else:
            print(f"被裁决的表格            {adj['tables']} 张")
            print(f"复核单元格              {adj['cells_checked']} 个")
            print(f"触发档分布              {adj['triggers']}")
            labels = {
                "agree": "一致（模型与程序读数相同）",
                "normalized": "格式归一（数字未变，修复换行切断）",
                "corrected": "改值（通过了全部闸门）",
                "rejected": "未采纳（未过闸门，只标记不改）",
                "unreadable": "读不出",
                "errors": "出错",
            }
            for k, label in labels.items():
                print(f"  {label:<34}{adj['verdicts'].get(k, 0):>6}")
            rec = adj.get("cells_recovered", 0)
            if rec:
                print(f"\n  其中 {rec} 个格子在归一前**解析不出数值**"
                      f"（换行把数字切断），归一后恢复可用 —— 这是实打实的提取收益。")
            if adj["examples"]:
                print("\n改动样例（程序读数 → 模型读数）：")
                for e in adj["examples"]:
                    print(
                        f"  p{e['page']:<4} r{e['row']}c{e['col']:<3} "
                        f"{e['verdict']:<10} 置信度={e['confidence']}  "
                        f"{e['original']!r} → {e['vlm_text']!r}"
                    )
            else:
                print("\n没有任何格子被改写。")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: v for k, v in res.items() if k != "dumps"}
        payload["adjudication"] = adj
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                       encoding="utf-8")
        print(f"\n已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
