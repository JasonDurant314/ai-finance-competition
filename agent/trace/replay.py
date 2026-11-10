"""回放与复现校验。

赛题要求「确保……运行结果可复现」。本模块提供两件事：

1. ``verify_run_integrity`` —— 校验 run 目录自身的完整性：
   输出文件哈希是否与 manifest 记录一致（检测事后篡改/损坏）。
2. ``replay_run`` —— 在缓存命中的前提下重跑流水线，
   逐字节比对产物哈希。**这是「同输入同输出」的机器证明。**

设计约束：replay 必须在 ``offline=True`` 下运行，
即**不允许发出任何网络请求**。缓存缺一条就直接判定为不可复现。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from agent.evidence.models import RunManifest, TraceKind
from agent.trace.tracer import file_sha256, read_trace, summarize_trace


@dataclass
class ReplayReport:
    run_id: str
    reproducible: bool
    hash_matches: dict[str, bool] = field(default_factory=dict)
    missing_cache: list[str] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    trace_summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_markdown(self) -> str:
        lines = [
            f"# 复现报告 — run `{self.run_id}`",
            "",
            f"**结论：{'✅ 可复现' if self.reproducible else '❌ 不可复现'}**",
            "",
        ]
        if self.hash_matches:
            lines += ["## 产物哈希比对", "", "| 产物 | 一致 |", "|---|---|"]
            for path, ok in sorted(self.hash_matches.items()):
                lines.append(f"| `{path}` | {'✅' if ok else '❌'} |")
            lines.append("")
        if self.missing_cache:
            lines += [
                "## 缓存缺失（离线 replay 无法完成）",
                "",
                *[f"- `{k}`" for k in self.missing_cache],
                "",
            ]
        if self.mismatches:
            lines += ["## 不一致明细", "", *[f"- {m}" for m in self.mismatches], ""]
        if self.notes:
            lines += ["## 说明", "", *[f"- {n}" for n in self.notes], ""]
        if self.trace_summary:
            lines += [
                "## 追踪汇总",
                "",
                "```json",
                json.dumps(self.trace_summary, ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        return "\n".join(lines)


# ==========================================================================
# 完整性校验
# ==========================================================================


def load_manifest(run_dir: str | Path) -> RunManifest:
    p = Path(run_dir) / "manifest.json"
    if not p.exists():
        raise FileNotFoundError(f"找不到 manifest.json：{p}")
    return RunManifest.model_validate(json.loads(p.read_text(encoding="utf-8")))


def verify_run_integrity(run_dir: str | Path) -> ReplayReport:
    """校验 run 目录中的产物与 manifest 记录的哈希是否一致。

    用途：评审拿到一个 run 目录后，一条命令即可确认
    「产物未被篡改、确实由该次运行产生」。
    """
    run_dir = Path(run_dir)
    manifest = load_manifest(run_dir)
    report = ReplayReport(run_id=manifest.run_id, reproducible=True)

    for rel_path, expected in manifest.outputs.items():
        target = run_dir / rel_path
        if not target.exists():
            report.hash_matches[rel_path] = False
            report.mismatches.append(f"产物缺失：{rel_path}")
            report.reproducible = False
            continue
        actual = file_sha256(target)
        ok = actual == expected
        report.hash_matches[rel_path] = ok
        if not ok:
            report.mismatches.append(
                f"{rel_path}：期望 {expected[:12]}… 实际 {actual[:12]}…"
            )
            report.reproducible = False

    trace_path = run_dir / "trace.jsonl"
    if trace_path.exists():
        report.trace_summary = summarize_trace(read_trace(trace_path))
    else:
        report.notes.append("缺少 trace.jsonl，无法核对过程记录")
        report.reproducible = False

    return report


# ==========================================================================
# 回放
# ==========================================================================


def replay_run(
    run_dir: str | Path,
    *,
    runner: Callable[[Path, bool], dict[str, str]] | None = None,
    strict: bool = True,
) -> ReplayReport:
    """回放一次运行。

    Args:
        run_dir: 原始 run 目录。
        runner: ``(output_dir, offline) -> {相对路径: sha256}``。
            由调用方注入具体流水线（避免 trace 层依赖 pipelines 层）。
            为 None 时只做完整性校验。
        strict: True 时任何哈希不一致即判定不可复现。

    Returns:
        ``ReplayReport``
    """
    run_dir = Path(run_dir)
    manifest = load_manifest(run_dir)
    report = verify_run_integrity(run_dir)

    # ★ 原始运行自己就标了 partial（有模型调用失败）时，直接说明：
    #   这次运行**本来就不该被期望可复现** —— 失败的调用没有响应缓存，
    #   离线回放只能产出不同的结果。不解释的话，读者会以为发现了严重问题，
    #   而实际上这是已知且已声明的状态（"不可复现"与"运行不完整"是两件事）。
    if str(getattr(manifest, "status", "")) == "partial":
        report.notes.append(
            "原始运行状态为 partial（存在失败的模型调用），因此**本次回放不一致是预期结果**："
            "失败的调用没有响应缓存，离线回放无法重现它。"
            "请查看 run 目录下的 `audit.md` 与 trace.jsonl 的 error 事件。"
        )

    if runner is None:
        report.notes.append("未注入 runner，仅执行完整性校验（未做实际重跑）")
        return report

    # 缓存自检：**只有确实调用过模型时才需要缓存**。
    # 若本次运行没有 LLM 调用（mock 模式，或纯确定性流水线），
    # 缺失缓存是正常的，不应据此判定为不可复现。
    trace_path = run_dir / "trace.jsonl"
    llm_calls = 0
    if trace_path.exists():
        llm_calls = sum(1 for e in read_trace(trace_path) if e.kind is TraceKind.LLM_CALL)

    cache_path = run_dir / "cache" / "llm.jsonl"
    if llm_calls and not cache_path.exists():
        report.missing_cache.append(
            f"cache/llm.jsonl 不存在，但 trace 显示有 {llm_calls} 次模型调用"
        )
        report.reproducible = False
        if strict:
            return report
    elif not llm_calls:
        report.notes.append(
            "本次运行没有模型调用（mock 或纯确定性流水线），无需模型响应缓存"
        )

    replay_dir = run_dir / "_replay"
    # 回放必须从**干净**的目录开跑：留着上一次回放的产物，
    # 会让「本次回放到底产出了什么」变得不可判定。
    if replay_dir.exists():
        import shutil

        shutil.rmtree(replay_dir)
    replay_dir.mkdir(parents=True, exist_ok=True)

    try:
        replayed = runner(replay_dir, True)  # offline=True
    except Exception as exc:  # 离线缓存缺失会走到这里
        report.error = f"{type(exc).__name__}: {exc}"
        report.reproducible = False
        report.notes.append(
            "离线回放失败。最常见原因：缓存未覆盖全部 LLM 调用。"
            "请先联网完整跑一次以填充缓存。"
        )
        return report

    for rel_path, expected in manifest.outputs.items():
        actual = replayed.get(rel_path)
        if actual is None:
            report.hash_matches[rel_path] = False
            report.mismatches.append(f"回放未产出：{rel_path}")
            report.reproducible = False
            continue
        ok = actual == expected
        report.hash_matches[rel_path] = ok
        if not ok:
            report.mismatches.append(
                f"{rel_path}：原始 {expected[:12]}… 回放 {actual[:12]}…"
            )
            report.reproducible = False

    return report


__all__ = [
    "ReplayReport",
    "load_manifest",
    "verify_run_integrity",
    "replay_run",
]
