"""一键复现 —— 把评审需要亲手跑的检查串成一条命令。

**为什么需要这个脚本**：赛题技术要求第 6、7 条要求「确保评审方能够在指定环境中
复现核心任务流程和输出结果」。这条要求如果只靠 README 里散落的十几条命令来满足，
评审就得逐条照着敲 —— 而**任何一条敲错都会得出「跑不起来」的结论**。
本脚本把可复现的结论固化成一次**可执行、有判据、有汇总**的运行。

设计要点：

1. **零依赖路径优先**：默认全部在 ``--mock`` 下跑，不需要 API Key、不需要联网。
   真实模型路径单独在 ``docs/REVIEWER.md`` 第九节给出（要花钱、要联网，
   不应该混进一键复现里）。
2. **每一步都有判据，而不是「命令返回 0 就算过」**：可复现性必须看到
   「✅ 可复现」字样、审计报告必须真的落盘。返回 0 但结论是「不可复现」
   是最坏的情况 —— 那会给出虚假的安心。
3. **失败也保留完整输出**，并在最后集中汇总：评审只需要看最后 20 行。

用法::

    python tools/reproduce_all.py               # 全部（实测约 2 分钟）
    python tools/reproduce_all.py --quick       # 跳过测试套件（约 20 秒）
    python tools/reproduce_all.py --run-id my   # 换 run 名（默认 demo）
    python tools/reproduce_all.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agent.console import enable_utf8_output  # noqa: E402

enable_utf8_output()


@dataclass
class Step:
    """一个可判定的复现步骤。"""

    key: str
    title: str
    argv: list[str]
    expect: list[str]
    cwd: Path = REPO
    timeout: int = 1800
    critical: bool = True
    note: str = ""
    rc: int | None = None
    seconds: float = 0.0
    missing: list[str] = field(default_factory=list)
    head: str = ""
    tail: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.missing


def build_steps(run_id: str, quick: bool) -> list[Step]:
    py = sys.executable
    steps = [
        Step("doctor", "环境自检（依赖 / 配置 / 数据目录）",
             [py, "-m", "agent.cli", "doctor"], ["必需项全部就绪"]),
        Step("contracts", "数据契约导出（6 个 JSON Schema）",
             [py, "-m", "agent.cli", "contracts"], ["共导出 6 个 Schema"]),
        Step("skills", "技能注册表：绑定校验（声明的工具/提示词真实存在）",
             [py, "-m", "agent.cli", "skills"], ["全部技能绑定校验通过"]),
        Step("prompts", "提示词注册表（版本 + SHA256 指纹）",
             [py, "-m", "agent.cli", "prompts"], ["已加载 5 个提示词"]),
        Step("tools", "工具注册表（副作用 / 确定性声明）",
             [py, "-m", "agent.cli", "tools"], ["已注册 17 个工具"]),
        Step("mcp", "MCP 客户端自检（真实子进程 + 完整协议往返）",
             [py, "-m", "agent.cli", "mcp", "--self-test"],
             ["MCP 客户端自检全部通过"]),
        Step("run", "端到端跑通 R1 → R4 → R5（mock 模型，零成本）",
             [py, "-m", "agent.cli", "run", "full", "--mock", "--run-id", run_id],
             ["完成 11/11 阶段"]),
        Step("verify", "产物完整性（34 个产物的哈希与 manifest 一致）",
             [py, "-m", "agent.cli", "verify", run_id], ["结论：✅ 可复现"]),
        Step("replay", "离线回放并逐字节比对（不调用任何模型）",
             [py, "-m", "agent.cli", "replay", run_id, "--strict"], ["✅ 可复现"]),
        Step("audit", "生成人类可读审计报告（过程可追溯）",
             [py, "-m", "agent.cli", "audit", run_id], ["审计报告已生成"]),
        # ★ 把刚造出来的 run **指名**交给核验（`--run`）。不指名的话，本脚本会去
        #   `runs/` 里挑夹具，而它挑剩的条件（非 mock、非崩）恰好把刚造的这个筛掉 ——
        #   于是退回到一个可能不存在的 "demo"：交付包里 14 项探针全红，
        #   报的是「页面渲染了什么」，真相是「这里根本没有 run 可渲染」。
        #   实测（2026-10-16）：解包后 `❌ 10/11`，连带包内
        #   `test_one_click_reproduce_passes_quick` 一起红。
        Step("web", "Web 演示台渲染核验（进程内，不占端口）",
             [py, "tools/web_smoke.py", "--in-process", "--run", run_id],
             ["全部通过"]),
    ]
    if not quick:
        steps.append(
            Step("tests", "测试套件（全量）",
                 [py, "-m", "pytest", "-q"], ["passed"],
                 note="约 100 秒")
        )
    return steps


def run_step(step: Step) -> Step:
    t0 = time.time()
    try:
        proc = subprocess.run(
            step.argv, cwd=step.cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=step.timeout,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        step.rc = proc.returncode
    except subprocess.TimeoutExpired as exc:      # pragma: no cover - 环境相关
        out = f"（超时 {step.timeout}s）\n" + str(exc)
        step.rc = -1
    step.seconds = time.time() - t0
    step.missing = [e for e in step.expect if e not in out]
    lines = [ln for ln in out.splitlines() if ln.strip()]
    step.head = "\n".join(lines[:8])
    step.tail = "\n".join(lines[-8:])

    mark = "✅" if step.ok else "❌"
    print(f"{mark} {step.title}（{step.seconds:.1f}s）")
    if not step.ok:
        if step.rc != 0:
            print(f"   返回码 {step.rc}")
        for m in step.missing:
            print(f"   期望输出里没有：{m!r}")
        print("   —— 输出末尾 ——")
        for ln in step.tail.splitlines():
            print("   | " + ln)
    return step


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="一键复现：零依赖跑通全部可复现结论")
    ap.add_argument("--run-id", default="demo", help="复用/创建的 run 名（默认 demo）")
    ap.add_argument("--quick", action="store_true", help="跳过测试套件")
    ap.add_argument("--json", default=None, help="把结果写成 JSON")
    args = ap.parse_args(argv)

    print("=" * 62)
    print("一键复现 —— 金融投研智能体（零 API Key / 零联网）")
    print("=" * 62)
    print(f"项目根目录：{REPO}")
    print(f"Python    ：{sys.version.split()[0]}  ({sys.executable})")
    print(f"run 名    ：{args.run_id}")
    print()

    # `run full` 复用同名 run 时会做续跑；先清干净，保证是**冷启动**。
    run_dir = REPO / "runs" / args.run_id
    if run_dir.exists():
        shutil.rmtree(run_dir)
        print(f"（已清除旧目录 runs/{args.run_id}，本次为冷启动）\n")

    steps = [run_step(s) for s in build_steps(args.run_id, args.quick)]

    print()
    print("=" * 62)
    ok_all = all(s.ok for s in steps)
    print(f"汇总：{'✅ 全部通过' if ok_all else '❌ 存在未通过项'}"
          f"（{sum(1 for s in steps if s.ok)}/{len(steps)}）")
    print("=" * 62)
    for s in steps:
        mark = "✅" if s.ok else "❌"
        print(f"  {mark} {s.key:<10} {s.seconds:6.1f}s  {s.title}")

    failed = [s for s in steps if not s.ok]
    if failed:
        print("\n未通过步骤的排查建议：")
        for s in failed:
            print(f"  · {s.key}：{' '.join(s.argv[3:])}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "ok": ok_all,
                    "run_id": args.run_id,
                    "python": sys.version.split()[0],
                    "steps": [
                        {
                            "key": s.key, "title": s.title, "ok": s.ok,
                            "rc": s.rc, "seconds": round(s.seconds, 2),
                            "missing": s.missing, "argv": s.argv,
                        }
                        for s in steps
                    ],
                },
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n结果已写入 {args.json}")

    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())

