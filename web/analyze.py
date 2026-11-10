"""「把材料丢进文件夹 → 点一下 → 跑完整分析」—— Web 端的**正式入口**。

这个模块只做三件事，但每一件都有明确的理由：

1. **一个固定收件目录** ``inbox/``
   用户不需要记 ``--data`` / ``--run-id`` / ``comps`` / ``market`` 这些参数，
   只管把 PDF 丢进 ``inbox/raw/``（丢在 ``inbox/`` 根下也行，递归查找）。

2. **一次点击 = 一条真实模型的 ``agent run full``**
   走**子进程**而不是进程内调用，理由有三条：
       * 与命令行**完全同一条路径**（同一份 argv、同一份 manifest、同一份产物）——
         Web 端若另起一套执行路径，"页面看到的"与"命令行跑出的"就可能不一致；
       * 真实模型跑一份年报实测约 18 分钟，不能占住 Web 的事件循环；
       * Web 进程崩了不会带走分析，日志天然落在文件里，可以直接在页面上 tail。

3. **跑完直接接回已有的可视化页面**
   产物落在 ``runs/<run_id>/``，与 ``agent run`` 完全同构，
   因此各选题视图（``/library`` ``/extract`` ``/facts`` ``/valuation`` ``/audit``
   ``/runs`` 等）原样可用（它们只读产物，不做二次加工）。

安全与并发（都是本项目踩过的坑）：

* **同一个 run 名只允许一个写者。** 实测：两个进程写同一个 run，
  缓存被穿插追加，回放取到的不是产物当时用的那个答案 ——
  一次**本可复现**的运行被判「❌ 不可复现」。启动前检查 ``.lock`` 里的 pid。
* **同一时刻只允许一个分析任务。** 真实模型跑十几分钟，
  排队比并发更可预期，也让「现在到底在跑什么」没有歧义。
* **必须显式确认费用。** 表单里那个勾选框不是装饰：真实模型按 token 计费，
  一次点击可能就是真金白银。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from web.artifacts import DEFAULT_RUNS_ROOT, PROJECT_ROOT

# ==========================================================================
# 目录约定
# ==========================================================================

INBOX = PROJECT_ROOT / "inbox"
INBOX_RAW = INBOX / "raw"
INBOX_DRAFTS = INBOX / "drafts"

ANALYZE_DIR = PROJECT_ROOT / "_analyze"
LOG_DIR = ANALYZE_DIR / "logs"
STATE_PATH = ANALYZE_DIR / "state.json"

#: R4 的两个必需输入。收件目录里没有时，从仓库自带的公开快照**自动补齐** ——
#: 这不是"编数据"：它们是随包提交、带来源与抓取时间的公开行情/利率快照
#: （见 `.gitignore` 里对 `data/comps`、`data/market` 的白名单说明）。
R4_FALLBACK = {
    "comps": PROJECT_ROOT / "data" / "comps",
    "market": PROJECT_ROOT / "data" / "market",
}

#: 单份 185 页年报的实测值（README / docs/VALIDATION_INDEX.md），用来给用户一个量级感
_PAGES_PER_BENCHMARK = 185.0
_MINUTES_PER_BENCHMARK = 18.0
_CNY_PER_BENCHMARK = 0.96

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,59}$")

INBOX_README = """# 收件目录（inbox）

把要分析的 PDF 丢进来，然后在 Web 端 `/analyze` 点一次「开始分析」即可。

    inbox/
      raw/        年报 / 公告 PDF —— **放这里**（递归查找，只认 .pdf）
      drafts/     可选：研究报告草稿 .md / .txt，用于 R5 纠错核查
      comps/      R4 可比公司快照（缺失时自动从 data/comps 补齐）
      market/     R4 市场利率缓存（缺失时自动从 data/market 补齐）

说明：

* PDF 直接放在 `inbox/` 根目录下也可以，效果一样。
* 分析产出落在 `runs/<run_id>/`，与命令行 `agent run` 完全同构，
  Web 端各选题视图（/library /extract /facts /valuation /audit /runs）原样可看。
* 本目录不入版本库（见 `.gitignore`）：里面放的是你自己的材料。
"""


class AnalyzeError(RuntimeError):
    """可以展示给用户看的错误（不是内部异常）。"""


# ==========================================================================
# 收件目录
# ==========================================================================


def ensure_inbox() -> Path:
    """确保收件目录存在，并放一份说明。幂等。"""
    for d in (INBOX_RAW, INBOX_DRAFTS):
        d.mkdir(parents=True, exist_ok=True)
    readme = INBOX / "README.md"
    if not readme.exists():
        readme.write_text(INBOX_README, encoding="utf-8", newline="\n")
    return INBOX


def _pdf_pages(path: Path) -> int | None:
    """快速取页数（只读目录，不解析内容）。取不到就返回 None。"""
    try:
        import fitz

        with fitz.open(path) as doc:
            return doc.page_count
    except Exception:                              # pragma: no cover - 损坏文件
        return None


def inventory(*, count_pages: bool = True, max_files: int = 200) -> dict[str, Any]:
    """收件目录里有什么。

    Args:
        count_pages: 是否打开 PDF 数页数（用于估算耗时/费用）。
            只对前 ``max_files`` 份打开，避免收件目录里堆了几百份时页面卡住。
    """
    ensure_inbox()
    root = INBOX_RAW if any(INBOX_RAW.rglob("*.pdf")) else INBOX
    pdfs: list[dict[str, Any]] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() != ".pdf":
            continue
        item: dict[str, Any] = {
            "name": p.name,
            "rel": p.relative_to(INBOX).as_posix(),
            "size_mb": round(p.stat().st_size / 1024 / 1024, 2),
            "pages": _pdf_pages(p) if (count_pages and len(pdfs) < max_files) else None,
        }
        pdfs.append(item)

    def _count(sub: str, pattern: str) -> int:
        d = INBOX / sub
        return len(list(d.glob(pattern))) if d.is_dir() else 0

    pages_known = [p["pages"] for p in pdfs if p.get("pages")]
    est_pages = sum(pages_known) if pages_known else len(pdfs) * 40   # 没数出来按 40 页/份粗估
    ratio = est_pages / _PAGES_PER_BENCHMARK

    # R4 的可比公司快照按**主体代码**命名（`<代码>_<年份>A.json`）。
    # 材料文件名里的 6 位股票代码能提前告诉我们"这次要估的是谁"，
    # 于是可以在**开跑之前**就警告"这个主体没有快照，R4 会显式失败" ——
    # 否则用户要等十几分钟才发现。
    codes = sorted({
        c for p in pdfs
        for c in re.findall(r"(?<!\d)(\d{6})(?!\d)", p["name"])
    })
    snap_targets = sorted({
        f.stem.split("_")[0]
        for src in (INBOX / "comps", R4_FALLBACK["comps"])
        if src.is_dir()
        for f in src.glob("*.json")
    })
    return {
        "inbox": str(INBOX),
        "raw_dir": str(root),
        "pdfs": pdfs,
        "pdf_count": len(pdfs),
        "pages_known": len(pages_known),
        "est_pages": est_pages,
        "est_minutes": round(ratio * _MINUTES_PER_BENCHMARK, 1),
        "est_cny": round(ratio * _CNY_PER_BENCHMARK, 2),
        "drafts": _count("drafts", "*.md") + _count("drafts", "*.txt"),
        "comps": _count("comps", "*.json"),
        "market": _count("market", "*.json"),
        "r4_ready": _count("comps", "*.json") > 0 and _count("market", "*.json") > 0,
        "r4_fallback": {k: str(v) for k, v in R4_FALLBACK.items()},
        "codes_in_names": codes,
        "comps_targets": snap_targets,
        #: 材料文件名里能认出主体代码、但没有任何一份快照对应它 → R4 会失败
        "codes_without_comps": [c for c in codes if c not in snap_targets],
    }


def provision_r4_inputs() -> dict[str, int]:
    """R4 的输入缺失时，从仓库自带的公开快照补齐到收件目录。

    为什么要补：R4 少了 ``comps/`` 或 ``market/`` 会**显式失败**（刻意设计：
    宁可失败，也不让报告里出现看起来正常、其实是编造的倍数）。
    用户只丢了几份 PDF 就想看到完整结论，因此这里替他补齐 ——
    但**在页面上写明补了什么、从哪补的**，不做无声的动作。
    """
    ensure_inbox()
    copied: dict[str, int] = {}
    for sub, src in R4_FALLBACK.items():
        dst = INBOX / sub
        if dst.is_dir() and any(dst.glob("*.json")):
            copied[sub] = 0
            continue
        if not src.is_dir():
            copied[sub] = 0
            continue
        dst.mkdir(parents=True, exist_ok=True)
        n = 0
        for f in sorted(src.glob("*.json")):
            target = dst / f.name
            if not target.exists():
                shutil.copy2(f, target)
                n += 1
        copied[sub] = n
    return copied


# ==========================================================================
# 阶段计划（与命令行同源）
# ==========================================================================


def plan_stages() -> list[dict[str, Any]]:
    """``full`` 流水线的阶段清单 —— 直接读 pipeline.yaml，不写死。"""
    try:
        from agent.runtime.loop import Plan

        plan = Plan.for_pipeline("full")
        return [
            {"name": s.name, "description": s.description, "optional": s.optional}
            for s in plan.stages
        ]
    except Exception:                              # pragma: no cover - 配置损坏
        return []


# ==========================================================================
# 任务状态
# ==========================================================================


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_state() -> dict[str, Any] | None:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):        # pragma: no cover
        return None


def _write_state(state: dict[str, Any]) -> None:
    ANALYZE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )


def _pid_alive(pid: int) -> bool:
    """复用 ``agent.runtime.loop._pid_alive`` —— 判不了就当活着（宁可多警告）。"""
    from agent.runtime.loop import _pid_alive as impl

    return impl(pid)


#: trace.jsonl 的最后一条事件超过这么久没更新 → 进程多半已经不在了
#: （运行中它是持续追加的）。取值偏保守：**假「运行中」比假「失败」便宜得多** ——
#: 前者最多让用户多等几分钟，后者会让用户以为一次跑了半小时的任务白费了。
STALE_TRACE_S = 300.0
#: 刚启动不到这么久 → 产物还没落盘是正常的，不能判失败
START_GRACE_S = 90.0


def _pid_alive_strict(pid: int) -> bool | None:
    """pid 活着吗？返回 ``True`` / ``False`` / **``None``（判不了）**。

    与 ``agent.runtime.loop._pid_alive`` 的取舍**相反**，这是刻意的：
    那边用它决定"要不要打印一条并发写警告"，所以判不了就当活着（宁可多警告）；
    这边用它决定"要不要拦住用户的下一次点击"，误判的代价是
    **用户再也点不动「开始分析」** —— 所以判不了时不下结论，交给其它证据。
    """
    if pid <= 0:
        return None
    try:
        if os.name == "nt":
            import subprocess

            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode != 0:
                # 实测：沙箱里 tasklist 返回 `ERROR: Access denied`（rc=1，stdout 空）。
                # 这正是"判不了"，不是"已经死了"。
                return None
            return str(pid) in (out.stdout or "")
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    except Exception:                              # pragma: no cover - 环境相关
        return None


def _age_s(ts: str | None) -> float | None:
    """某个 ISO 时间戳距现在多少秒。"""
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - t).total_seconds())
    except ValueError:                             # pragma: no cover
        return None


def _trace_last_ts(run_dir: Path) -> str | None:
    """读 ``trace.jsonl`` **最后一条事件**的时间戳。

    ⚠️ 为什么不用文件 mtime（实测踩过的坑）：在 G:（网络/虚拟盘）上
    mtime 会滞后一百多秒 —— 2026-09-25 那次真实运行里，
    trace 的最后一条事件时间就是"此刻"，而 ``stat().st_mtime`` 却说 169 秒前。
    于是页面在运行**完全正常**时报出「❌ 失败」，而用户正盯着它看。
    事件时间戳写在**文件内容里**，不受文件系统元数据缓存影响，是可信的那一个。

    只读文件末尾 64KB，因此 trace 涨到几 MB 也不会拖慢页面。
    """
    path = run_dir / "trace.jsonl"
    if not path.exists():
        return None
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size == 0:
                return None
            fh.seek(max(0, size - 65536))
            blob = fh.read().decode("utf-8", errors="replace")
    except OSError:                                # pragma: no cover
        return None
    for line in reversed(blob.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            ts = json.loads(line).get("ts")
        except json.JSONDecodeError:
            continue        # 尾巴上可能是被切断的半行，继续往前找
        if ts:
            return str(ts)
    return None


def _derive_phase(*, manifest: dict[str, Any] | None, state: dict[str, Any],
                  run_dir: Path) -> str:
    """任务处于哪个阶段。

    **证据优先级：产物 > 进程 > trace 事件时间 > 启动宽限。** 三条都不能只看一个：

    * 只看 pid：受限环境里 pid 探测可能完全不可用（``tasklist`` 被拒）；
    * 只看文件 mtime：网络盘上 mtime 会滞后一百多秒（实测，见 :func:`_trace_last_ts`）；
    * 只看启动时间：跑十几分钟的任务早就过了宽限期。

    判错的代价不对称：**假「运行中」只是让用户多等，假「失败」会让用户以为任务白跑了**。
    """
    if manifest is not None:
        # loop 只在**结束**时才写 manifest.json，因此它一出现就说明跑完了
        return "failed" if manifest.get("status") == "failed" else "finished"
    if _pid_alive_strict(int(state.get("pid") or 0)) is True:
        return "running"
    trace_age = _age_s(_trace_last_ts(run_dir))
    if trace_age is not None and trace_age < STALE_TRACE_S:
        return "running"                           # 还在写事件
    started_age = _age_s(state.get("started_at"))
    if started_age is not None and started_age < START_GRACE_S:
        return "running"                           # 刚启动，产物还没落盘
    # 没跑完、也没在跑 —— 但**已经跑出过东西**的话，这是「中断，可续跑」，
    # 不是「失败」。两者的处置完全不同：前者一个 --resume 就能接着跑
    # （已完成的阶段跳过、模型调用命中缓存），后者要查原因。
    # 实测：用户按错窗口 Ctrl+C 打断了 13 分钟的运行，页面报「失败」，
    # 看上去像是白跑了 —— 其实 checkpoint 与缓存都在，续跑只补没做完的那一步。
    if (run_dir / "checkpoint.json").exists():
        return "interrupted"
    return "failed"


def running_task(*, runs_root: str | Path | None = None) -> dict[str, Any] | None:
    """当前是否有分析在跑。返回 state（含 pid）或 None。"""
    st = _read_state()
    if not st:
        return None
    root = Path(runs_root) if runs_root else DEFAULT_RUNS_ROOT
    run_dir = root / str(st.get("run_id"))
    phase = _derive_phase(manifest=_read_json(run_dir / "manifest.json"),
                          state=st, run_dir=run_dir)
    return st if phase == "running" else None


def tail_log(n: int | None = 40, *, run_id: str | None = None) -> list[str]:
    """日志末尾 n 行（页面上的实时输出）；``n=None`` 表示**全部**。

    ``None`` 与 ``0`` 是两件事：控制台要的是「一行都不许藏」，写 ``0``
    没法表达这个意思（``lines[-0:]`` 恰好也等于全部，但那是巧合，
    读代码的人只会以为是 bug）。
    """
    st = _read_state() or {}
    rid = run_id or st.get("run_id")
    if not rid:
        return []
    path = LOG_DIR / f"{rid}.log"
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:                                # pragma: no cover
        return []
    return lines if n is None else lines[-n:]


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):        # pragma: no cover
        return None


#: 实时用量缓存：run_id → {offset, tokens, cost, calls, hits}
#: 只解析 trace **新增**的字节，因此页面每 3 秒轮询一次也不会把磁盘读爆。
_LIVE_CACHE: dict[str, dict[str, Any]] = {}


def _live_llm_stats(run_id: str, run_dir: Path) -> dict[str, Any]:
    """从 ``trace.jsonl`` **增量**汇总模型调用次数、token 与费用。

    ⚠️ 实测踩过的坑：这个函数是为了修一个「页面骗人」的缺陷 ——
    最初 token/费用读的是 ``manifest.json`` 的 ``total_tokens``，
    而 manifest **只在运行结束时才写**。于是真实运行已经花了 ¥0.58，
    页面上却一直显示「Token 0 / 费用 ¥0.0000」，
    用户完全无法判断这次跑得贵不贵、要不要中途叫停。

    ``checkpoint.json`` 里的 budget 也不够用：它**每个阶段结束才更新一次**，
    而最贵的 ``r1_judge`` 阶段一跑就是二十多分钟，中间全是 0。

    只有 trace 是**每次模型调用都追加**的，所以它是唯一实时的来源。
    """
    path = run_dir / "trace.jsonl"
    st = _LIVE_CACHE.setdefault(
        run_id, {"offset": 0, "tokens": 0, "cost": 0.0, "calls": 0, "hits": 0}
    )
    if not path.exists():
        return {"tokens": 0, "cost_cny": 0.0, "llm_calls": 0, "cache_hits": 0}

    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size < st["offset"]:                # 文件被截断/重建 → 从头再来
                st.update(offset=0, tokens=0, cost=0.0, calls=0, hits=0)
            if size > st["offset"]:
                fh.seek(st["offset"])
                blob = fh.read()
            else:
                blob = b""
    except OSError:                                # pragma: no cover
        blob = b""

    if blob:
        # 只处理**完整行**：并发写入时末尾可能是半行，把它留到下次读。
        cut = blob.rfind(b"\n")
        if cut >= 0:
            complete, st["offset"] = blob[: cut + 1], st["offset"] + cut + 1
            for raw in complete.splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    e = json.loads(raw)
                except json.JSONDecodeError:       # pragma: no cover
                    continue
                if e.get("kind") != "llm_call":
                    continue
                st["calls"] += 1
                st["tokens"] += int(e.get("prompt_tokens") or 0) + int(
                    e.get("completion_tokens") or 0
                )
                st["cost"] += float(e.get("cost_cny") or 0.0)
                if e.get("cache_hit"):
                    st["hits"] += 1

    return {
        "tokens": int(st["tokens"]),
        "cost_cny": round(float(st["cost"]), 4),
        "llm_calls": int(st["calls"]),
        "cache_hits": int(st["hits"]),
    }


def _run_summary(manifest: dict[str, Any] | None, run_id: str,
                 run_dir: Path) -> dict[str, Any]:
    """页面上那行「Token / 费用」。跑完之后以 manifest 为准，跑的过程中看 trace。"""
    live = _live_llm_stats(run_id, run_dir)
    if manifest is not None:
        return {
            "total_tokens": int(manifest.get("total_tokens", 0) or 0),
            "total_cost_cny": float(manifest.get("total_cost_cny", 0) or 0),
            "output_count": len(manifest.get("outputs", {}) or {}),
            "llm_calls": live["llm_calls"],
            "cache_hits": live["cache_hits"],
            "live": False,
        }
    return {
        "total_tokens": live["tokens"],
        "total_cost_cny": live["cost_cny"],
        "output_count": 0,
        "llm_calls": live["llm_calls"],
        "cache_hits": live["cache_hits"],
        "live": True,
    }


def status(*, runs_root: str | Path | None = None,
           log_tail: int | None = 40) -> dict[str, Any]:
    """当前任务的完整状态：阶段进度、耗时、花费、日志、产物入口。

    ``log_tail=None`` 表示日志**全给**（控制台用）；默认 40 行是展示站的口径。
    """
    root = Path(runs_root) if runs_root else DEFAULT_RUNS_ROOT
    st = _read_state()
    stages = plan_stages()

    if not st:
        # ★ 这个分支的键必须与下面那个**逐一对齐**。
        #   少一个键不会在这里报错，而是在模板里炸成 `UndefinedError` ——
        #   上一次就漏了 `run_summary`：任何**从未跑过**的机器（`_analyze/state.json`
        #   还不存在）打开页面直接 500。而「新机器第一次打开」恰恰是最常见的路径。
        return {
            "phase": "idle", "run_id": None, "stages": stages,
            "completed": [], "failed": [], "skipped": [],
            "current": None, "elapsed_s": 0.0, "log": [],
            "budget": {}, "run_status": None, "links": {}, "progress": 0.0,
            "resumable": False,
            "started_at": "", "mock": False, "resume": False, "inbox": None,
            "command": None, "pid": 0,
            "run_summary": {"total_tokens": 0, "total_cost_cny": 0.0,
                            "output_count": 0, "llm_calls": 0,
                            "cache_hits": 0, "live": False},
        }

    run_id = str(st.get("run_id"))
    run_dir = root / run_id
    pid = int(st.get("pid") or 0)

    checkpoint = _read_json(run_dir / "checkpoint.json") or {}
    completed = [s for s in (checkpoint.get("completed") or [])]
    failed = [s for s in (checkpoint.get("failed") or [])]
    skipped = [s for s in (checkpoint.get("skipped") or [])]
    manifest = _read_json(run_dir / "manifest.json")
    phase = _derive_phase(manifest=manifest, state=st, run_dir=run_dir)

    names = [s["name"] for s in stages]
    done_set = set(completed) | set(skipped)
    current = None
    if phase == "running":
        for n in names:
            if n not in done_set and n not in failed:
                current = n
                break

    started = st.get("started_at") or ""
    elapsed = 0.0
    if started:
        try:
            t0 = datetime.fromisoformat(started)
            end = datetime.now(timezone.utc)
            if manifest and manifest.get("finished_at"):
                end = datetime.fromisoformat(str(manifest["finished_at"]).replace("Z", "+00:00"))
            elapsed = max(0.0, (end - t0).total_seconds())
        except ValueError:                         # pragma: no cover
            elapsed = 0.0

    total = len(names) or 1
    links = {
        "library": f"/library?run={run_id}",
        "extract": f"/extract?run={run_id}",
        "facts": f"/facts?run={run_id}",
        "valuation": f"/valuation?run={run_id}",
        "audit": f"/audit?run={run_id}",
        "runs": f"/runs?run={run_id}",
    }
    return {
        "phase": phase,
        "run_id": run_id,
        "started_at": started,
        "mock": bool(st.get("mock")),
        "resume": bool(st.get("resume")),
        #: 有断点、又没跑完 → 可以一个 --resume 接着跑
        "resumable": (manifest is None and (run_dir / "checkpoint.json").exists()),
        "inbox": st.get("inbox"),
        "command": st.get("command"),
        "pid": pid,
        "stages": stages,
        "completed": completed,
        "failed": failed,
        "skipped": skipped,
        "current": current,
        "elapsed_s": round(elapsed, 1),
        "log": tail_log(log_tail, run_id=run_id),
        "budget": (checkpoint.get("budget") or {}),
        "run_status": (manifest or {}).get("status"),
        "run_summary": _run_summary(manifest, run_id, run_dir),
        "links": links,
        "progress": round(len(done_set & set(names)) / total, 4),
    }


# ==========================================================================
# 启动
# ==========================================================================


def new_run_id(prefix: str = "web") -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def probe_model(timeout_s: float = 30.0) -> dict[str, Any]:
    """一次**最小真实调用**，用来在开跑前确认模型真的可用。

    为什么值得做这一次调用：真实全流程跑一份年报实测约 18 分钟。
    如果不先验证凭据与网络，用户会在**等满 18 分钟之后**才发现
    API Key 过期、余额不足或网络不通 —— 而这次探测只发十来个 token。

    它不会被自动触发（每次页面加载都打一次模型既慢又花钱），
    只能由用户点「测试模型连接」显式发起。
    """
    from agent.config import load_models_config
    from agent.model.gateway import RoleConfig
    from agent.model.providers.openai_compat import OpenAICompatProvider

    out: dict[str, Any] = {
        "ok": False, "model": "", "provider": "", "elapsed_s": 0.0,
        "text": "", "tokens": 0, "error": None,
    }
    t0 = time.perf_counter()
    try:
        cfg = load_models_config()
        pcfg = cfg.get("provider") or {}
        roles = cfg.get("roles") or {}
        model = str((roles.get("default") or {}).get("model") or "deepseek-flash")
        out["model"] = model
        out["provider"] = str(pcfg.get("name") or "")

        provider = OpenAICompatProvider(
            base_url=str(pcfg.get("base_url", "")),
            api_key=pcfg.get("api_key"),
            provider_name=str(pcfg.get("name", "openai_compat")),
        )
        # ⚠️ 参数必须走 `RoleConfig.to_params()`，**不要自己手拼**。
        # 实测踩过：手写 `{"thinking": False}` 会拿到 HTTP 422 ——
        # 这个网关要的是 `{"type": "disabled"}` 结构体，不是裸 bool。
        # 探测的意义就在于"和真实调用走同一条参数构造路径"，
        # 自己拼一套参数，探出来的就不是真实链路。
        role = RoleConfig(model=model, temperature=0.0, max_tokens=16,
                          thinking=False, top_p=1.0)
        r = provider.complete(
            model=model,
            messages=[{"role": "user", "content": "只回复两个字：正常"}],
            params=role.to_params(),
            json_schema=None,
            timeout=timeout_s,
        )
        out["ok"] = True
        out["text"] = str(r.get("text") or "").strip()[:40]
        out["tokens"] = int(r.get("prompt_tokens", 0) or 0) + int(
            r.get("completion_tokens", 0) or 0
        )
    except Exception as exc:                       # noqa: BLE001 - 要把原因原样给用户
        out["error"] = f"{type(exc).__name__}: {exc}"
    out["elapsed_s"] = round(time.perf_counter() - t0, 2)
    return out


def build_argv(run_id: str, *, data_dir: Path | str | None = None,
               runs_root: Path | str | None = None, mock: bool = False,
               resume: bool = False, limit: int | None = None,
               extra_args: list[str] | None = None) -> list[str]:
    """拼出子进程命令行 —— 抽成纯函数，好让"到底跑的是什么"可以被单独断言。

    默认（``mock=False``）就是**真实模型**：页面上没有 mock 开关，
    这条代码路径也不接受来自表单的开关（``mock`` 只给自动化测试用）。
    """
    argv = [sys.executable, "-m", "agent.cli", "run", "full",
            "--data", str(data_dir if data_dir is not None else INBOX),
            "--run-id", run_id]
    if runs_root:
        # ⚠️ 必须把 runs 根目录也传给子进程：状态是去 `runs_root/<id>/` 读的，
        # 子进程若写到别处（默认 runs/），就会出现"跑完了却报失败"这种最迷惑的现象 ——
        # 产物明明在，只是不在我们看的地方（实测踩过：测试里跑到了仓库的 runs/ 下）。
        argv += ["--runs-root", str(runs_root)]
    if resume:
        # 续跑：已完成的阶段跳过（checkpoint），模型调用命中缓存（cache/llm.jsonl）。
        argv.append("--resume")
    if mock:
        argv.append("--mock")
    if limit:
        argv += ["--limit", str(limit)]
    if extra_args:
        argv += list(extra_args)
    return argv


def start(
    *,
    run_id: str | None = None,
    confirmed: bool = False,
    mock: bool = False,
    resume: bool = False,
    limit: int | None = None,
    runs_root: str | Path | None = None,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """启动（或续跑）一次完整分析，默认**真实模型**。

    Args:
        confirmed: 用户是否已确认「会调用真实模型并产生费用」。未确认为**拒绝启动** ——
            这个勾选框是本次点击与真金白银之间唯一的闸门。
        resume: 续跑上次中断的运行。已完成的阶段跳过、模型调用命中缓存，
            因此只补没做完的那部分（实测：13 分钟中断的运行续跑只补 r1_judge）。
        mock: 仅供自检/离线演示（页面上不提供这个开关）。真实使用一律 False。

    Raises:
        AnalyzeError: 材料为空、已有任务在跑、run 名重复、断点状态不匹配等**可展示**的原因。
    """
    inv = inventory()
    if inv["pdf_count"] == 0:
        raise AnalyzeError(
            f"收件目录里没有 PDF：{INBOX_RAW}（或 {INBOX}）下放几份 .pdf 再点开始。"
        )
    if not confirmed:
        raise AnalyzeError("请先勾选「我知道这会调用真实模型并产生费用」。")

    root = Path(runs_root) if runs_root else DEFAULT_RUNS_ROOT

    busy = running_task(runs_root=root)
    if busy:
        raise AnalyzeError(
            f"已经有一个分析在跑（run_id={busy.get('run_id')}，pid={busy.get('pid')}）。"
            "真实模型跑一份年报约 18 分钟，请等它结束 —— "
            "同一个 run 目录同时只能有一个写者，否则缓存会被写脏、复现报告会误报。"
        )

    rid = (run_id or "").strip() or new_run_id()
    if not _RUN_ID_RE.match(rid):
        raise AnalyzeError("run 名只允许字母/数字/._-，且以字母或数字开头（最长 60 字符）。")

    run_dir = root / rid
    if (run_dir / "manifest.json").exists():
        raise AnalyzeError(f"runs/{rid} 已经是一次完成的运行，换个 run 名（或先归档它）。")

    has_checkpoint = (run_dir / "checkpoint.json").exists()
    if resume and not has_checkpoint:
        raise AnalyzeError(
            f"runs/{rid} 没有可续跑的断点（checkpoint.json 不存在）—— 直接新建一次运行即可。"
        )
    if not resume and has_checkpoint:
        # 原地重跑会把**已经做完的工作再做一遍**（虽然缓存能省钱，但仍然浪费时间）。
        # 与其让用户困惑"为什么又要跑一遍"，不如明确告诉他该点哪个按钮。
        raise AnalyzeError(
            f"runs/{rid} 上次没跑完（存在断点）。不要原地重跑 —— 请点「继续未完成的运行」，"
            "它会跳过已完成的阶段并复用已调用过的模型结果。"
        )

    copied = provision_r4_inputs()

    argv = build_argv(rid, runs_root=root, mock=mock, resume=resume,
                      limit=limit, extra_args=extra_args)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{rid}.log"
    state = {
        "run_id": rid,
        "pid": 0,
        "started_at": _now_iso(),
        "mock": bool(mock),
        "resume": bool(resume),
        "inbox": str(INBOX),
        "command": " ".join(argv),
        "r4_provisioned": copied,
        "log": str(log_path),
    }
    _write_state(state)

    # 日志用 utf-8 打开：脚本自己会切 UTF-8 输出，这里保持一致，
    # 否则 Windows 上中文会变成乱码写进文件（页面上的实时输出就废了）。
    # 续跑时用追加：上一次的运行日志与这一次接在一起，才看得出"从哪继续的"。
    mode = "a" if resume and log_path.exists() else "w"
    with log_path.open(mode, encoding="utf-8", newline="\n") as fh:
        if mode == "a":
            fh.write(f"\n\n===== 续跑（--resume）{' ' + _now_iso()} =====\n")
        fh.write(f"$ {' '.join(argv)}\n\n")
        fh.flush()
        proc = subprocess.Popen(argv, cwd=str(PROJECT_ROOT), stdout=fh,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)

    state["pid"] = proc.pid
    _write_state(state)
    return status(runs_root=root)


__all__ = [
    "AnalyzeError", "INBOX", "INBOX_RAW", "ANALYZE_DIR",
    "build_argv", "ensure_inbox", "inventory", "plan_stages", "probe_model",
    "provision_r4_inputs", "new_run_id", "running_task", "start", "status",
    "tail_log",
]

