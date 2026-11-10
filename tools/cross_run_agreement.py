"""双路互证：确定性抽取 vs 模型读数。

    python tools/cross_run_agreement.py table-dual              # 同一个 run 内比较（推荐）
    python tools/cross_run_agreement.py table-grid7 table-vlm5  # 跨 run 比较

为什么要看这个：

「确定性优先」不是"不用模型"，而是**让模型去做只有它才能做的事**。
那么模型读到的那些值，有多少能被确定性路径独立复现？
复现比例高 → 两条路径互为佐证，结论可信；
只被模型读到的那部分 → 要么是确定性路径的漏抽（该补），
要么是模型看错了（该拦），必须逐类分清。

按**取值**而不是按 (主体,指标) 比对：同一指标天然有多个取值（多行/多期），
打包成集合比会得到"要么全对要么全错"的假结论 —— 实测踩过。
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.console import enable_utf8_output  # noqa: E402

# Windows 控制台默认 GBK：本脚本会打印 ✓ / ✅ 这类符号，
# 不先切 UTF-8 就会在**最需要它的时候**抛 UnicodeEncodeError 中断。
enable_utf8_output()

DETERMINISTIC = ("table_grid", "rule_engine")
MODEL = ("vlm_scan", "vlm_field")
"""抽取器短名。

⚠️ 这里写死的是 ``vlm_scan`` 而不是 ``vlm`` —— 实测踩过：写成 ``vlm``
匹配不到任何一条，工具于是报告"模型路径没有任何事实"，
看起来像"这批材料没触发 VLM"，实际是**过滤条件写错了**。
验证工具自己给假结论，比没有工具更危险。"""


def load(run: str) -> list[dict]:
    d = json.loads((Path("runs") / run / "facts" / "facts.json").read_text(encoding="utf-8"))
    return d["facts"] if isinstance(d, dict) else d


def dec(v) -> Decimal | None:
    try:
        return Decimal(str(v))
    except Exception:
        return None


def values(facts: list[dict]) -> set[tuple[str, str, Decimal]]:
    out: set[tuple[str, str, Decimal]] = set()
    for f in facts:
        d = dec(f.get("value"))
        if d is not None:
            out.add((str(f.get("entity")), str(f.get("metric")), d))
    return out


def by_name(facts: list[dict], prefixes: tuple[str, ...]) -> list[dict]:
    return [f for f in facts
            if str(f.get("extractor", "")).split("@")[0] in prefixes]


def report(run_det: str, run_model: str) -> int:
    fa = load(run_det)
    fb = fa if run_model == run_det else load(run_model)
    det = by_name(fa, DETERMINISTIC)
    mod = by_name(fb, MODEL)

    va, vb = values(det), values(mod)
    both = va & vb

    print(f"确定性路径（{run_det}）：{len(det)} 条，{len(va)} 个不同取值")
    print(f"模型路径（{run_model}）：{len(mod)} 条，{len(vb)} 个不同取值")
    if run_det != run_model:
        print("（两次 run 用的是同一批材料，因此可以逐值比对）")
    print()

    if not vb:
        print("模型路径没有任何事实 —— 无法互证。")
        print("常见原因：该 run 是离线复跑（--offline）且缓存未命中，"
              "或这批材料没有触发 VLM 扫描。")
        return 0

    print(f"模型读到、且被确定性路径**独立复现**：{len(both)} / {len(vb)}"
          f"（{len(both) / len(vb):.0%}）")
    print(f"确定性路径独有：{len(va - vb)} 个取值")

    missing = sorted(vb - va)
    if missing:
        print("\n仅模型读到、确定性路径未覆盖（前 15 个）：")
        for ent, metric, v in missing[:15]:
            print(f"   {ent[:28]:<30}{metric:<20}{v}")

    ka: dict[tuple[str, str], set[Decimal]] = defaultdict(set)
    kb: dict[tuple[str, str], set[Decimal]] = defaultdict(set)
    for ent, metric, v in va:
        ka[(ent, metric)].add(v)
    for ent, metric, v in vb:
        kb[(ent, metric)].add(v)
    diffs = [(k, sorted(ka[k])[:3], sorted(kb[k])[:3])
             for k in set(ka) & set(kb) if not (ka[k] & kb[k])]
    if diffs:
        print(f"\n⚠ 同一 (主体, 指标) 下两边**没有一个对上**：{len(diffs)} 组（前 8 组）")
        for k, av, bv in diffs[:8]:
            print(f"   {k[0][:26]:<28}{k[1]:<20}确定性={av}  模型={bv}")
    else:
        print("\n没有出现「同一主体同一指标两边完全对不上」的情况。")
    return 0


def main() -> int:
    a = sys.argv[1] if len(sys.argv) > 1 else "table-dual"
    b = sys.argv[2] if len(sys.argv) > 2 else a
    return report(a, b)


if __name__ == "__main__":
    raise SystemExit(main())
