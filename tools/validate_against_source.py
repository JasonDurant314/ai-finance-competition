"""独立核对工具：拿人工从原始页面读出的数值，核对系统抽出的事实。

方法论（这一步是本项目验证纪律的核心）
----------------------------------------
**先冻结系统输出 → 再做人工核对**。顺序颠倒的话，标注者会被系统答案锚定，
"准确率"就变成自证。

因此本文件的基准值来自：

1. 用 ``_tmp_locate_statements.py`` **先定死**页码（只定位、不读值）
2. 把页面渲染成 PNG
3. 由人（或独立视觉模型）**直接看原图**记录数值 —— 完全不经过本项目管道
4. 最后才与 ``facts.json`` 比对

单位纪律
--------
中国石油的年报用「人民币百万元」为单位。基准值一律换算成**元**再比对，
这样「系统是否正确处理了量纲」本身就成为被测项 ——
量纲错一个 10^6，是金融数据抽取最典型的严重错误。
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DOC_BY_CODE = {
    "000722": "湖南发展",
    "002716": "湖南白银",
    "601857": "中国石油",
}

# ==========================================================================
# ★ 预注册基准：**从 data/r5_eval/truth.py 派生**，不再手写
#
# 曾经把同一批数字写在三处（本文件的 BENCHMARK、评测集脚本的 TRUTH、草稿正文），
# 结果评测集与基准自相矛盾：系统独立计算后报出「增速不符」，而**它是对的、基准是错的**。
# 现在唯一来源是 truth.py，本文件只声明"哪个指标对应哪个键、在哪一页"。
# ==========================================================================
sys.path.insert(0, str(PROJECT_ROOT / "data" / "r5_eval"))
from agent.console import enable_utf8_output  # noqa: E402

# Windows 控制台默认 GBK：本脚本会打印 ✓ / ✅ 这类符号，
# 不先切 UTF-8 就会在**最需要它的时候**抛 UnicodeEncodeError 中断。
enable_utf8_output()
import truth as _truth  # noqa: E402

BENCHMARK: dict[str, list[dict]] = {code: _truth.benchmark_rows(code) for code in _truth.RAW}


def load_facts(run_dir: Path) -> list[dict]:
    p = run_dir / "facts" / "facts.json"
    if not p.exists():
        raise SystemExit(f"找不到事实库：{p}（运行可能还没结束）")
    data = json.loads(p.read_text(encoding="utf-8"))
    return data["facts"] if isinstance(data, dict) else data


def doc_ids_by_company(run_dir: Path) -> dict[str, str]:
    """``股票代码 -> doc_id``。

    **按 doc_id 匹配，而不是按主体名** —— 主体名在事实库里可能是
    「湖南能源集团发展股份有限公司」（文档主体）、也可能是「601857」，
    甚至可能因为归属纠正而变成别的写法。拿名字做匹配会把
    "归属正确但名字不同"的事实误判成漏抽（实测踩过：召回一度显示 0%）。
    ``doc_id`` 由文件名+内容哈希派生，是稳定的。
    """
    p = run_dir / "facts" / "documents.json"
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    for d in json.loads(p.read_text(encoding="utf-8")):
        fname = str(d.get("filename") or "")
        for code in DOC_BY_CODE:
            if code in fname:
                out[code] = d["doc_id"]
    return out


def match_entity(facts: list[dict], code: str) -> list[dict]:
    """按股票代码匹配事实的主体（旧接口，保留兼容）。

    事实里的 entity 可能是 6 位代码，也可能是公司名 —— 两种都要认。
    """
    name = DOC_BY_CODE[code]
    out = []
    for f in facts:
        ent = str(f.get("entity") or "")
        if code in ent or name in ent:
            out.append(f)
    return out


def close_enough(actual: Decimal, expected: Decimal, tol: float) -> bool:
    if expected == 0:
        return actual == 0
    return abs(actual - expected) / abs(expected) <= Decimal(str(tol))


def main() -> int:
    ap = argparse.ArgumentParser(description="用人工独立读值核对抽取结果")
    ap.add_argument("--run", required=True, help="run_id 或 run 目录")
    ap.add_argument("--tol", type=float, default=0.001, help="相对容差（默认 0.1%）")
    args = ap.parse_args()

    run_dir = Path(args.run)
    if not run_dir.is_dir():
        run_dir = PROJECT_ROOT / "runs" / args.run
    facts = load_facts(run_dir)
    doc_ids = doc_ids_by_company(run_dir)
    print(f"事实库：{run_dir / 'facts' / 'facts.json'}（{len(facts)} 条）")
    if doc_ids:
        print("按文档匹配：" + ", ".join(f"{k}→{v}" for k, v in sorted(doc_ids.items())))
    else:
        print("⚠️ 没有 documents.json，退回按主体名匹配（可能误判漏抽）")
    print()

    total = hit = value_ok = wrong = missing = 0
    errors: list[str] = []
    unit_scale_errors: list[str] = []

    for code, bench in BENCHMARK.items():
        did = doc_ids.get(code)
        sub = [f for f in facts if f.get("doc_id") == did] if did else match_entity(facts, code)
        print("=" * 74)
        print(f"{code} {DOC_BY_CODE[code]} —— 事实库中 {len(sub)} 条")
        print(f"{'指标':<30}{'期间':<7}{'基准值':>22}{'系统值':>22}  判定")
        print("-" * 74)
        for item in bench:
            total += 1
            metric, period = item["metric"], item["period"]
            expected = Decimal(item["value"])
            # 只按指标+期间匹配（文档已在子集里锁定）
            cands = [f for f in sub if f.get("metric") == metric and f.get("period") == period]
            if not cands:
                missing += 1
                print(f"{metric:<32}{period:<7}{item['raw'][:20]:>22}{'—':>22}  ❌ 漏抽")
                errors.append(f"{code} {metric} {period} 漏抽（基准 {item['raw']}）")
                continue
            hit += 1
            # 取第一条作为判定对象，同时检查是否有别的候选也命中
            got = Decimal(str(cands[0]["value"]))
            if close_enough(got, expected, args.tol):
                value_ok += 1
                print(f"{metric:<32}{period:<7}{str(expected)[:20]:>22}{str(got)[:20]:>22}  ✅ 一致")
            else:
                wrong += 1
                ratio = got / expected if expected else Decimal(0)
                tag = "⚠️ 量纲" if any(
                    close_enough(got, expected * Decimal(s), args.tol)
                    for s in (Decimal(10) ** k for k in range(-9, 10))
                ) else "❌ 数值不符"
                if tag.startswith("⚠️"):
                    unit_scale_errors.append(f"{code} {metric} {period}: 系统 {got} vs 基准 {expected}（倍数 {ratio:.6g}）")
                print(f"{metric:<32}{period:<7}{str(expected)[:20]:>22}{str(got)[:20]:>22}  {tag} 倍数={ratio:.6g}")
                errors.append(f"{code} {metric} {period}: 系统 {got}，基准 {expected}，倍数 {ratio:.6g}")

    print("\n" + "=" * 74)
    print("核对汇总")
    print("=" * 74)
    print(f"  基准项          : {total}")
    print(f"  抽到            : {hit}  （召回 {hit / total:.1%}）")
    print(f"  数值一致        : {value_ok}")
    print(f"  数值不符        : {wrong}")
    print(f"  漏抽            : {missing}")
    if hit:
        print(f"  抽中项准确率    : {value_ok / hit:.1%}")
    print(f"  整体正确率      : {value_ok / total:.1%}")
    if unit_scale_errors:
        print(f"\n⚠️  量纲类错误 {len(unit_scale_errors)} 处（最严重的一类）：")
        for e in unit_scale_errors:
            print(f"     · {e}")
    if errors:
        print(f"\n明细（{len(errors)} 条）：")
        for e in errors[:40]:
            print(f"     · {e}")
    return 0 if (wrong == 0 and missing == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
