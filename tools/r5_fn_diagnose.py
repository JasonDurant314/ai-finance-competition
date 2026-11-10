"""R5 漏报诊断：把每条 FN 摊开 —— 草稿声明、事实库候选、当前规则为何没报。

用法::

    python tools/r5_fn_diagnose.py --run runs/r5-eval-graded2 --facts-run annual23
    python tools/r5_fn_diagnose.py --run runs/r5-eval-graded2 --facts-run annual23 --only 000722

设计意图：漏报（FN）比误报更难定位 —— 误报有 finding 可读，漏报什么都没有。
本工具把"标注声称有错的那一段原文"与"事实库里所有相关候选"并排显示，
让"规则为什么看不见它"变成可读的事实，而不是靠猜。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from agent.console import enable_utf8_output  # noqa: E402

# Windows 控制台默认 GBK：本脚本会打印 ✓ / ✅ 这类符号，
# 不先切 UTF-8 就会在**最需要它的时候**抛 UnicodeEncodeError 中断。
enable_utf8_output()

from agent.evidence.models import FactSet  # noqa: E402
from agent.evidence.store import EvidenceStore, FactLedger  # noqa: E402
from domain.tools.audit_rules import (  # noqa: E402
    extract_claims, load_metric_aliases, scope_ledger_to_draft,
)


def _load_ledger(facts_run: Path) -> FactLedger:
    fs = FactSet.model_validate(json.loads(
        (facts_run / "facts" / "facts.json").read_text(encoding="utf-8")))
    store = EvidenceStore()
    for span in fs.evidence:
        store.add_span(span)
    ledger = FactLedger(store, run_id=fs.run_id)
    for f in fs.facts:
        ledger.add(f)
    return ledger


def _doc_names(facts_run: Path) -> dict[str, str]:
    p = facts_run / "facts" / "documents.json"
    if not p.is_file():
        return {}
    return {d["doc_id"]: str(d.get("filename") or "")
            for d in json.loads(p.read_text(encoding="utf-8")) if d.get("doc_id")}


def _resolve_run(value: str, runs_root: str = "runs") -> Path:
    """接受 run_id 或路径，统一解析成 run 目录。"""
    p = Path(value)
    if (p / "facts" / "facts.json").is_file():
        return p
    alt = Path(runs_root) / value
    if (alt / "facts" / "facts.json").is_file():
        return alt
    raise SystemExit(f"找不到事实库：{value}（已试 {p} 与 {alt}）")


def main() -> int:
    ap = argparse.ArgumentParser(description="R5 漏报逐条诊断")
    ap.add_argument("--run", required=True, help="R5 run（含 audit/audit.json）")
    ap.add_argument("--facts-run", required=True, help="R1 run（含 facts/）")
    ap.add_argument("--eval-root", default="data/r5_eval", help="评测集根目录")
    ap.add_argument("--only", default=None, help="只看某个草稿 id 前缀")
    ap.add_argument("--runs-root", default="runs")
    args = ap.parse_args()

    run_dir = Path(args.run)
    if not (run_dir / "audit" / "audit.json").is_file():
        run_dir = Path(args.runs_root) / args.run
    facts_run = _resolve_run(args.facts_run, args.runs_root)
    gold_dir = Path(args.eval_root) / "golden"
    draft_dir = Path(args.eval_root) / "drafts"

    ledger = _load_ledger(facts_run)
    doc_names = _doc_names(facts_run)

    audit = json.loads((run_dir / "audit" / "audit.json").read_text(encoding="utf-8"))
    by_draft: dict[str, list[dict]] = {}
    for f in audit.get("findings", []):
        by_draft.setdefault(str(f.get("draft_id")), []).append(f)

    aliases = load_metric_aliases()

    for gpath in sorted(gold_dir.glob("*.labels.json")):
        did = gpath.stem.replace(".labels", "")
        if args.only and not did.startswith(args.only):
            continue
        labels = json.loads(gpath.read_text(encoding="utf-8"))
        dpath = draft_dir / f"{did}.md"
        if not dpath.is_file():
            continue
        text = dpath.read_text(encoding="utf-8")
        scoped, note = scope_ledger_to_draft(text, ledger, doc_names=doc_names)
        mine = by_draft.get(did, [])

        def hit(cr) -> bool:
            if not cr:
                return False
            for f in mine:
                m = (f.get("location") or {}).get("char_range")
                if m and not (m[1] <= cr[0] or cr[1] <= m[0]):
                    return True
            return False

        print("=" * 100)
        print(f"{did}   草稿 {len(text)} 字，系统报出 {len(mine)} 条")
        print(f"  作用域：{note}")

        for lab in labels:
            if not lab.get("is_real_error", True):
                continue
            cr = tuple(lab["char_range"])
            if hit(cr):
                continue
            snippet = text[cr[0]:cr[1]]
            print(f"\n  ── 漏报 {lab['error_id']} [{lab['type']}] {snippet!r}")
            if lab.get("correct"):
                print(f"     应为：{lab['correct']!r}")
            if lab.get("note"):
                print(f"     标注说明：{lab['note'][:150]}")

            # 该段附近的数值主张
            near = [c for c in extract_claims(text, alias_map=aliases)
                    if c.start <= cr[1] + 40 and c.end >= cr[0] - 40]
            if not near:
                print("     ⚠️ 这段附近**没有识别出任何数值主张** —— "
                      "规则连输入都没有，属于提取层的漏报")
            for c in near:
                print(f"     主张 {c.text!r} value={c.value} unit={c.unit!r} "
                      f"hint={c.metric_hint!r} @[{c.start},{c.end}]")
                cands = ([f for f in scoped.by_metric(c.metric_hint)
                          if not f.needs_review] if c.metric_hint else [])
                if not cands:
                    print("        → 该指标在作用域内**没有任何候选事实**")
                for f in cands[:6]:
                    print(f"        cand {f.fact_id[:18]} {str(f.value):>18} {f.unit:<5} "
                          f"{str(f.period):<7} scope={str(f.statement_scope)[:12]:<12} "
                          f"ent={str(f.entity)[:18]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
