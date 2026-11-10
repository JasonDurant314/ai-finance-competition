"""现场演示：把材料丢进一个文件夹，当场跑出三个需求的产物。

**这个脚本解决的具体问题**
------------------------
现场演示的约束是**时间**，而完整 R1 在真实年报上要 5–8 分钟/份。
三档的实测数据（000722 年报 185 页，真实模型）：

| 档 | 命令 | 实测 | 说明 |
|---|---|---|---|
| 完整（含单元格裁决） | `--warm` | **469 秒**、¥0.55 | 488 个单元格裁决调用 |
| **现场（缓存预热 + `--resume`）** | `--live` | **1.5 秒、0 token** | 命中缓存，跳过已完成阶段 |

★ **关键机制：缓存按 run 存**（`runs/<run_id>/cache/llm.jsonl`）。
所以「换个 run 名重跑」**永远是冷启动**。必须**同一个 run 名 + `--resume`** 才能命中。

⚠️⚠️ **两条硬约束**（都是实测踩出来的，写进代码注释以免重犯）

1. **预热与现场之间绝不可改动 `configs/`。**
   实测：为提速把 `tables.adjudicate` 临时改成 false 跑了一轮，之后还原成 true ——
   那一轮 `replay --strict` 直接报「❌ 不可复现」，因为 manifest 里的
   `agent_config_hash` 记的是当时的配置，而 replay 用**当前**配置重算，指纹自然不同。
   本脚本因此**完全不碰配置**。

2. **一个 run 名同时只能有一个进程在写。**
   实测：两个 warm 进程同时写 `runs/live`，缓存文件被穿插追加
   （569 行里 168 个键被写了两次，行距 26–58 行），于是回放取到的不是产物当时用的
   那个答案 —— 一次**本可复现**的运行被判「❌ 不可复现」。
   现在 `Loop` 会写 `runs/<run_id>/.lock`（记 pid），发现别人在写就当场警告。

用法::

    # ① 赛前预热（真实模型，按份数计时间与费用）
    python tools/demo_run.py --data <材料目录> --warm

    # ② 现场运行（秒级，不花钱）
    python tools/demo_run.py --data <材料目录> --live

    # ③ 起台
    python -m web.app --run live

    # 只看材料能不能被发现（不跑任何东西）
    python tools/demo_run.py --data <材料目录> --dry-run

材料目录的组织方式（**一个目录同时喂给三个需求**）::

    <材料目录>/
      raw/      年报 / 公告 PDF        → R1 结构化提取（递归查找 .pdf）
      drafts/   研究报告草稿 .md/.txt  → R5 纠错核查

  · 没有 `raw/` 时，PDF 直接放在 `<材料目录>/` 下也可以。
  · 没有 `drafts/` 时 R5 会退化为「自动合成一份含已知错误的演示草稿」，
    仍然能跑通，但覆盖率与准确率失去意义（脚本会提示）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
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


def _list_pdfs(data: Path) -> list[Path]:
    """递归找 .pdf —— 与 R1 的 ``discover_documents`` 用同一判据。"""
    if not data.exists():
        return []
    return sorted(
        p for p in data.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"
    )


def _list_drafts(data: Path) -> list[Path]:
    """``<材料目录>/drafts/`` 下的草稿 —— 与 R5 的 ``discover_drafts`` 用同一判据。"""
    root = data / "drafts"
    if not root.is_dir():
        return []
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in {".md", ".txt"}
    )


def _cache_lines(run_dir: Path) -> int:
    p = run_dir / "cache" / "llm.jsonl"
    if not p.is_file():
        return 0
    try:
        return sum(1 for _ in p.open(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return 0


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _report_r1(run_dir: Path) -> None:
    facts = _read_json(run_dir / "facts" / "facts.json").get("facts") or []
    if not facts:
        return
    nr = sum(1 for f in facts if f.get("needs_review"))
    c = Counter((f.get("extractor") or "?").split("@")[0] for f in facts)
    det = sum(v for k, v in c.items() if k in ("table_grid", "rule_engine"))
    print(f"   R1 事实 {len(facts)} 条，needs_review {nr}")
    print(f"      确定性路径 {det}/{len(facts)} = {det / len(facts):.1%}；"
          f"逐项 {dict(c.most_common())}")


def _report_r4(run_dir: Path) -> None:
    v = _read_json(run_dir / "valuation" / "valuation.json")
    if not v:
        print("   R4 未产出 valuation.json")
        return
    dcf = (v.get("dcf") or {})
    comps = (v.get("comps") or {})
    per_share = dcf.get("value_per_share")
    lo, hi = comps.get("value_per_share_low"), comps.get("value_per_share_high")
    print(f"   R4 估值对象 {v.get('entity') or v.get('target') or '?'}"
          f"（基期 {v.get('base_period') or '?'}）")
    if per_share is not None:
        print(f"      DCF 每股 ¥{float(per_share):.4f}")
    if lo is not None and hi is not None:
        print(f"      相对估值区间 ¥{float(lo):.4f} ~ ¥{float(hi):.4f}")
    gaps = v.get("gaps") or []
    if gaps:
        print(f"      缺口 {len(gaps)} 项：{'、'.join(str(g) for g in gaps[:4])}")


def _report_r5(run_dir: Path) -> None:
    a = _read_json(run_dir / "audit" / "audit.json")
    if not a:
        print("   R5 未产出 audit.json")
        return
    m = a.get("metrics") or a.get("scores") or {}
    print(f"   R5 草稿 {a.get('drafts_checked', '?')} 份；指标 {m}")


def main() -> int:
    ap = argparse.ArgumentParser(description="现场演示：材料放进文件夹，当场跑出产物")
    ap.add_argument("--data", required=True, help="材料目录（递归查找 .pdf；drafts/ 放草稿）")
    ap.add_argument("--run-id", default="live", help="run 名（默认 live）")
    ap.add_argument("--pipeline", default="full",
                    choices=["full", "r1_extract", "r4_valuation", "r5_audit"],
                    help="跑哪条流水线（默认 full：R1→R4→R5 一条命令）")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 份材料（省时省钱）")
    ap.add_argument("--warm", action="store_true", help="赛前预热：跑真实模型并落盘")
    ap.add_argument("--live", action="store_true", help="现场运行：--resume 复用缓存（秒级）")
    ap.add_argument("--dry-run", action="store_true", help="只检查材料，不真跑")
    args = ap.parse_args()

    if not (args.warm or args.live or args.dry_run):
        print("请指定 --warm（赛前预热）/ --live（现场秒级）/ --dry-run（只看材料）")
        return 2

    data = Path(args.data)
    if not data.is_absolute():
        data = (REPO / data).resolve()

    pdfs = _list_pdfs(data)
    drafts = _list_drafts(data)
    print(f"材料目录：{data}")
    if not pdfs and args.pipeline != "r5_audit":
        print("  ❌ 没有找到 .pdf —— 检查路径；只支持 PDF（SUPPORTED_SUFFIXES = {'.pdf'}）")
        return 2
    if pdfs:
        total_mb = sum(p.stat().st_size for p in pdfs) / 1024 / 1024
        print(f"  找到 {len(pdfs)} 份 PDF，共 {total_mb:.1f} MB")
        for p in pdfs[:5]:
            print(f"    · {p.name}")
        if len(pdfs) > 5:
            print(f"    … 另 {len(pdfs) - 5} 份")
    print(f"  找到 {len(drafts)} 份研究报告草稿"
          f"（{data / 'drafts'}）" if drafts else
          f"  ⚠️ {data / 'drafts'} 下没有 .md/.txt 草稿 —— "
          "R5 会退化为自动合成的演示草稿，覆盖率/准确率将失去意义")

    # R4 的两个必需输入。实测踩过：只放 PDF 时 R4 会以 CompsDataError 显式失败
    # （刻意不内置占位样本 —— 宁可失败，也不让报告里出现编造的倍数）。
    n_comps = len(list((data / "comps").glob("*.json"))) if (data / "comps").is_dir() else 0
    n_rates = len(list((data / "market").glob("*.json"))) if (data / "market").is_dir() else 0
    print(f"  可比公司快照 {n_comps} 份（{data / 'comps'}）")
    print(f"  市场利率缓存 {n_rates} 份（{data / 'market'}）")
    if args.pipeline in ("full", "r4_valuation") and (not n_comps or not n_rates):
        print("  ⚠️ R4 需要 <材料目录>/comps/<主体>_<年份>A.json 与 "
              "<材料目录>/market/rates_*.json。缺了它 R4 会**显式失败**"
              "（刻意如此：宁可失败，也不给出编造的倍数）。")
        print("     生成快照：python tools/fetch_comps.py --target <代码> --as-of <报告期>")
    if args.limit:
        print(f"  ⚠️ --limit {args.limit} 只跑前 {args.limit} 份材料 —— "
              "R4 会对**事实最多的那个主体**估值，被截掉的话可能不是你要的目标公司。")

    if args.dry_run:
        print("\n--dry-run：材料可被发现，未执行。")
        return 0

    run_dir = REPO / "runs" / args.run_id
    cached = _cache_lines(run_dir)
    prev = _read_json(run_dir / "manifest.json")

    if args.live:
        if not run_dir.is_dir() or cached == 0:
            print(f"\n❌ runs/{args.run_id} 还没有缓存（{cached} 条）。")
            print("   现场档依赖赛前预热，请先跑一次：")
            print(f"     python tools/demo_run.py --data {args.data} --warm "
                  f"--run-id {args.run_id}")
            return 3
        print(f"\n现场档：缓存 {cached} 条，预期**秒级**返回")
        if prev and prev.get("pipeline") not in (None, args.pipeline):
            print(f"  ⚠️ runs/{args.run_id} 上一次是 `{prev.get('pipeline')}`。"
                  "换流水线会让阶段名对不上、断点被忽略（缓存仍复用）。")
    else:
        n = args.limit or len(pdfs)
        print(f"\n预热档：真实模型，实测约 5–8 分钟/份 × {n} 份"
              f"（约 {5 * n:.0f}–{8 * n:.0f} 分钟；会产生 token 费用）")
        if cached:
            print(f"  ⚠️ runs/{args.run_id} 已有 {cached} 条缓存，预热会复用它们")
        print("  ⚠️ 预热之后直到现场，**不要改动 configs/** —— 改了会让该轮无法复现")
        print(f"  ⚠️ 预热期间**不要再起第二个进程**写 runs/{args.run_id} —— "
              "两个写者会把缓存写脏，回放会报假的「不可复现」")
        lock = run_dir / ".lock"
        if lock.is_file():
            print(f"  ⚠️ 已存在锁文件 {lock}：{lock.read_text(encoding='utf-8').strip()}")

    cmd = [sys.executable, "-m", "agent.cli", "run", args.pipeline,
           "--data", str(data), "--run-id", args.run_id]
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    if args.live:
        cmd.append("--resume")
    elif prev or cached:
        # ★ 预热档撞上**已经跑过**的 run：直接跑会把新事件追加到旧 trace 后面
        #   （seq 从 0 重来；实测同 run 名跑第二遍后 trace 146 行变 292 行、
        #   146 个 seq 全重复，`verify` 随即判「不可复现」）。`agent.cli run`
        #   现在会直接拒绝这种用法，所以这里改走 --resume —— 已完成的阶段跳过、
        #   缓存照样命中，正是"复用缓存"想要的效果。
        #   代价：--resume 会**继承上次的运行身份**（started_at / doc_limit），
        #   想换 --limit 或换材料必须删掉 runs/<id> 重来。
        cmd.append("--resume")
        print("  ↪ 该 run 已存在：本次按 --resume 续跑（不追加、不重写 trace）")
        print("     想从零重来请先删掉 runs/" + args.run_id + "\n")
    print("\n执行：", " ".join(cmd), "\n")

    t0 = time.time()
    proc = subprocess.run(cmd, cwd=REPO)
    elapsed = time.time() - t0

    if proc.returncode != 0:
        print(f"\n❌ 运行失败（exit {proc.returncode}），耗时 {elapsed:.1f} 秒")
        return proc.returncode

    print(f"\n✅ 完成，耗时 **{elapsed:.1f} 秒**")
    _report_r1(run_dir)
    _report_r4(run_dir)
    _report_r5(run_dir)

    print("\n=== 现场这样演示 ===")
    print("  1) 起台（默认展示这一轮）：")
    print(f"       python -m web.app --run {args.run_id}")
    print("  2) 走这几页：")
    print("       /library   → 材料与 SHA256（“这是真实材料”）")
    print("       /extract   → 表格单元格级裁决 + 裁图物证")
    print("       /facts     → 抽取路径构成（确定性 vs 模型）+ 溯源")
    print("       /valuation → DCF 与相对估值、敏感性、假设的事实/推论标注")
    print("       /audit     → 研究报告纠错核查（命中/误报/漏报）")
    print("       /process   → 现场再拖一份新材料（<1 秒，只看不落库）")
    print("  3) 当场验证可复现：")
    print(f"       python -m agent.cli replay {args.run_id} --strict")
    print("     ⚠️ 这一步只在**同一次预热**的 run 上成立；"
          "预热期间若有第二个进程写过，它必然报「不可复现」")
    if not args.live:
        print(f"\n  ★ 现场请改用：python tools/demo_run.py --data {args.data} "
              f"--live --run-id {args.run_id} --pipeline {args.pipeline}")
    else:
        print("\n  ⚠️ 提醒：现在起到演示结束，别动 configs/ —— 否则这一步会报「不可复现」")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
