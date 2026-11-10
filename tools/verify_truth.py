"""关键指标真值对照 —— 把 R1 事实库的取值与**人工逐页核对**的真值逐项比对。

为什么需要这个工具：R1 的全部自动闸门（单位归一化、量纲族校验、quote 回读、
算术重算、恒等式闸门）都只能保证"内部自洽"，**无法保证与年报一致**。
实测事故里最危险的那一类（行标签里的单位被漏看 → 现金被放大 100 倍）
恰恰是内部完全自洽的：两条事实各自都过了 quote 回读，
只有对着年报原文逐格核对才会发现有一个是错的。

用法::

    python tools/verify_truth.py runs/annual22
    python tools/verify_truth.py runs/annual23 --code 601857
    python tools/verify_truth.py runs/annual23 --show-candidates

真值与 R5 评测集**共用同一份**（``data/r5_eval/truth.py``），
避免"两个地方各写一套真值"。本工具只做只读核对，不修改任何产物。
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

from data.r5_eval.truth import RAW, benchmark_rows  # noqa: E402

CODES = ("601857", "000722", "002716")


def _entity_aliases(code: str) -> set[str]:
    """某个代码在事实库里可能使用的全部主体写法。

    事实库的主体名有**两种形态**，取决于抽取路径：
    ``table_grid`` 用文档主体的规范写法（``601857`` 这种代码，
    或 ``湖南能源集团发展股份有限公司`` 这种全称），
    而公告表的行主体是公司名。核对工具必须两种都认，
    否则会把"主体写法不同"误报成"事实缺失"。
    """
    names = {code}
    info = RAW.get(code) or {}
    short = info.get("name")
    if short:
        names.add(str(short))
        names.add(f"{short}股份有限公司")
    return names


def _same_entity(fact_entity: str, aliases: set[str]) -> bool:
    """主体名是否属于该证券代码。

    ⚠️ 不能用"互为前缀"来匹配 —— 实测 ``000722`` 的真值简称是 ``湖南发展``，
    而事实库里写的是全称 ``湖南能源集团发展股份有限公司``（``湖南能源集团`` +
    原名 ``湖南发展``）。简称**不是**全称的前缀，前缀匹配会漏掉它。

    改用两条判据：
    1. 完全相等，或较短者 ≥4 字且互为前缀（覆盖 ``XXX`` vs ``XXX股份有限公司``）
    2. 较短者 ≥4 字且**被较长者包含**（覆盖 ``湖南发展`` ⊂ ``湖南能源集团发展股份有限公司``）
       —— 之所以要求 ≥4 字，是为了避免"湖南"这种两字前缀把不同公司混起来
    """
    e = str(fact_entity).strip()
    for a in aliases:
        if e == a:
            return True
        short, long_ = (a, e) if len(a) <= len(e) else (e, a)
        if len(short) >= 4 and (long_.startswith(short) or short in long_):
            return True
    return False


def _entity_by_doc(run_dir: Path) -> dict[str, set[str]]:
    """``doc_id`` → 该文档可能的主体写法集合。

    为什么需要它：事实库的主体名是**正文里出现的名字**，
    而证券简称与正文全称可以毫无字面关系 ——
    实测 ``000722`` 的真值简称是 ``湖南发展``，正文全称却是
    ``湖南能源集团发展股份有限公司``（``湖南发展`` 甚至不是它的子串）。

    可靠的做法是走**文档文件名**：素材文件名形如
    ``000722_湖南发展_2025年年度报告_2026-04-04.pdf``，
    代码与简称都在里面。这样主体归属由"这条事实出自哪份文件"决定，
    而不是靠字符串猜。
    """
    out: dict[str, set[str]] = {}
    docs_path = run_dir / "facts" / "documents.json"
    if not docs_path.exists():
        return out
    docs = json.loads(docs_path.read_text(encoding="utf-8"))
    for d in (docs if isinstance(docs, list) else docs.get("documents", [])):
        doc_id = d.get("doc_id")
        name = str(d.get("filename") or d.get("file") or "")
        stem = Path(name).stem
        parts = stem.split("_")
        aliases = {p for p in parts if p} | {stem}
        if doc_id:
            out[str(doc_id)] = aliases
    return out


def _facts(run_dir: Path) -> list[dict]:
    path = run_dir / "facts" / "facts.json"
    if not path.exists():
        raise FileNotFoundError(f"找不到事实库：{path}")
    return json.loads(path.read_text(encoding="utf-8"))["facts"]


def _pick(
    facts: list[dict], code: str, metric: str, period: str,
    by_doc: dict[str, set[str]] | None = None,
) -> tuple[dict | None, int, int]:
    """取该 (主体, 指标, 期间) 下最可能被下游采用的那条事实。

    主体判定**优先走文档归属**（``by_doc``：这条事实出自哪份素材文件），
    回退到字符串比对。文档归属可靠得多，见 :func:`_entity_by_doc`。

    与 R4 ``_best_fact`` 的做法一致：跳过 needs_review，再按置信度取。
    本工具刻意**不**复用它 —— 如果复用，R4 选错时这里也会跟着选错，
    核对就失去意义了。
    """
    aliases = _entity_aliases(code)
    by_doc = by_doc or {}

    def belongs(f: dict) -> bool:
        doc_aliases = by_doc.get(str(f.get("doc_id")))
        if doc_aliases and code in doc_aliases:
            return True
        return _same_entity(str(f["entity"]), aliases)

    rows = [
        f for f in facts
        if belongs(f) and f["metric"] == metric and str(f["period"]) == period
    ]
    usable = [f for f in rows if not f["needs_review"]]
    if not usable:
        return None, len(rows), 0
    return (
        max(usable, key=lambda f: (f["confidence"], len(str(f["value"])))),
        len(rows),
        len(usable),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="R1 事实库 vs 人工核对真值")
    ap.add_argument("run", help="run_id（如 annual22）或 runs 下的路径")
    ap.add_argument("--runs-root", default="runs")
    ap.add_argument("--code", default=None, help="只核对某个证券代码（如 601857）")
    ap.add_argument("--show-candidates", action="store_true",
                    help="同时打印候选条数与可用条数")
    args = ap.parse_args()

    run_dir = Path(args.run)
    if not run_dir.exists():
        run_dir = Path(args.runs_root) / args.run
    facts = _facts(run_dir)
    by_doc = _entity_by_doc(run_dir)
    codes = [args.code] if args.code else list(CODES)

    print(f"事实库: {run_dir / 'facts' / 'facts.json'}（{len(facts)} 条事实）")
    print("真值  : data/r5_eval/truth.py::benchmark_rows\n")
    print(f"{'代码':<8}{'指标':<32}{'期间':<8}{'事实库取值':>20}{'真值':>20}  {'判定'}")

    hit = miss = absent = rounding = 0
    for code in codes:
        for row in benchmark_rows(code):
            metric, period = row["metric"], row["period"]
            want = float(row["value"])
            pick, n_all, n_ok = _pick(facts, code, metric, period, by_doc)
            if pick is None:
                mark, got = "—", "（无可用事实）"
                absent += 1
            else:
                got_v = float(pick["value"])
                diff = abs(got_v - want)
                got = pick["value"]
                if diff <= max(1.0, abs(want) * 1e-9):
                    mark = "✓"
                    hit += 1
                elif diff <= abs(want) * 1e-4:
                    # 相对差 ≤0.01% —— 是**四舍五入**（源表只印到万元或整数），
                    # 不是取错值。与"取错值"必须分开统计：
                    # 前者无需处理，后者要人工查清。
                    mark = "≈"
                    rounding += 1
                else:
                    mark = "✗"
                    miss += 1
            extra = f"   (候选 {n_all} / 可用 {n_ok})" if args.show_candidates else ""
            print(f"{code:<8}{metric:<32}{period:<8}{str(got):>20}{want:>20.0f}  {mark}{extra}")

    comparable = hit + rounding + miss
    print(f"\n命中 {hit} / 舍入相符 {rounding} / 不符 {miss}（可比 {comparable}），缺失 {absent}")
    if miss:
        print("⚠️ 有指标与真值**实质不符**（相对差 >0.01%）。这属于必须人工判定的差异 —— "
              "不要只看总体命中率，逐条查清是哪一侧错了。")
    return 0 if miss == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
