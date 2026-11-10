"""控制面板 —— 一页四个标签：运行 / R1 提取 / R4 估值 / R5 核查。

这是**操作台**，不是展示站。`web/app.py` 那套九个页面（一个写入口 + 八个只读页）
回答「每个数字怎么来的」，这个页面回答三个动作：**怎么跑、跑到哪了、跑出了什么**。
两者共用同一套底座：

    · 产物读取   ``web/artifacts.py``            （`runs/<id>/` 下的产物）
    · 执行与进度 ``web/analyze.py``              （起子进程 / 阶段进度 / 日志）
    · 轨迹读取   ``web/app.py::_tail_trace``     （trace.jsonl 的字节偏移游标）
    · 原文读取   ``web/app.py::_tail_jsonl``     （agent_log.jsonl 的字节偏移游标）

页面原则（2026-10-12 明确要求，改这个模板前先读一遍）：

    1. **一页四个标签，一次只看一块。** 不做子页面、不做九宫格导航。
       早先把「运行 / 过程 / 结果」三块竖着叠在一屏里，三块都在于是三块都看不清：
       想确认「跑到哪了」要先滚过一屏表格。三个选题各占一个标签页，**形状相同**
       （这一步跑了什么 → 跑出了什么 → 产物在哪），读者切过去不用重新学一遍怎么看。
    2. **只有标签、数字、表格、链接。** 不写解释性段落 —— 解释是文档（README /
       docs/）该干的事，放在控制台上只会把读数淹掉。
    3. **不重复印。** 同一个数只出现一次（状态栏里有的，正文不再说一遍）。
    4. 无外部 CDN：样式与脚本本地自带，断网可完整运行。
    5. **每个标签页上的数都要是真读出来的。** 标签徽标、每步用时/调用/花费，
       一律从 `runs/<id>/` 的产物与 `trace.jsonl` 现算；读不到就印「—」或
       「这一步没跑」，不印一个看起来很像的默认值。
    6. **右栏常驻，四个标签页上都在。** 左列是「这一块跑出了什么」，右列是
       「**这个过程**」—— 模型此刻收到什么、说了什么，工具收到什么、回了什么。
       它不属于任何一个标签页：切到 R5 就看不见 R1 那次模型调用，只会让人
       以为自己看漏了。数据源是 `agent_log.jsonl`（见下），不是 `trace.jsonl`。
    7. **赛题标签页的第一块是「重点考察」实测表**（`_criteria.html`，数据来自
       `web/app.py` 的 `criteria_r1/r4/r5`，见 `_criteria_views`）。赛题对每题都
       写明了重点考察什么 —— 那四条、五条就是评分点。评分点埋进折叠块、或者排在
       产物后面，等于让评委自己去找。表里只有三栏（考察点 / 实测 / 记号），与本页
       「只有标签、数字、表格、链接」是同一条规矩。

启动：``python -m web.panel --run <run-id>``（默认 127.0.0.1:8000）

★ 默认端口与 ``web.app`` 一样是 8000：这两个应用是**一个用、一个看**，正常不会同时跑；
  真要同时跑，给后起的那个加 ``--port 8010``。把控制台改到别的端口是个已犯过的错 ——
  演示时打开的还是 8000，那上面什么都没有。
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from web import analyze as AZ
from web.artifacts import (
    DEFAULT_RUNS_ROOT,
    ArtifactError,
    RunArtifacts,
    available_runs,
    group_thousands,
)
from web.app import (
    _ALL_TRACE_KINDS,
    _split_complete,
    _tail_trace,
    _tail_window,
    criteria_r1,
    criteria_r4,
    criteria_r5,
    find_eval_run,
    plain_marks,
    register_filters,
)

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

# ==========================================================================
# 右栏：模型与工具的**原文**流
# ==========================================================================
#
# 数据源是 `runs/<id>/agent_log.jsonl`（`agent/trace/tracer.py::Tracer.journal`）：
# 每次模型调用记完整 `messages` + 回答正文，每次工具调用记完整 `args` + 返回值，
# 每个阶段开始记一行边界。`trace.jsonl` 里前两者只有哈希（`args_hash` /
# `payload_hash`），所以右栏**不能**拿它当数据源 —— 那正是「看得到调用了几次、
# 看不到说了什么」的由来。
#
# 老 run 没有这一路（记录器是后加的）。那时退到 `cache/llm.jsonl`：它是回放用的
# 响应缓存，按调用顺序追加，所以能取出**模型答了什么**，但没有提示词、没有阶段、
# 没有工具调用 —— 如实标注，不假装完整。

#: 右栏认的三种记录（与 `Tracer.journal` 的 `kind` 对应）。
#:
#: `stage` 只有「阶段开始」这一种（`Tracer.stage_event(status="start")` 落的边界）。
#: 它让右栏看得出这一段是谁在干、以及 R4/R5 那两段**一个模型都没调**。
ACTIVITY_KINDS = ("llm", "tool", "stage")

#: 逐文件的增量统计：文件**绝对路径** → {offset, llm, tool, stage, failed, tokens, cost}
#:
#: 键必须是路径而不是 run 名：同名的 run 目录可以出现在两个 root 下
#: （测试里每个用例一个 tmp_path，run 都叫 `demo-panel`），按名字记账会把
#: 上一个文件的偏移量用到下一个文件上，统计数是那个文件的。
_ACTIVITY_SCAN: dict[str, dict[str, Any]] = {}


def empty_activity() -> dict[str, Any]:
    """右栏的空帧 —— 与正常帧**同形**。

    同形很重要：模板与 JS 都对着一份固定的字段表渲染，缺字段的话
    Jinja 渲染空串、JS 读成 `undefined`，页面照常 200 而右栏半截空白。
    """
    return {
        "offset": 0, "next_offset": 0, "eof": True, "rows": [],
        "source": "none", "note": "", "error": None,
        # 哪个 provider 跑的。右栏与真实 run 长得一样，`mock` 只能靠这个字段说出口。
        "provider": "",
        "stats": {"llm_calls": 0, "tool_calls": 0, "stages": 0, "failed": 0,
                  "cache_hits": 0, "tokens": 0, "cost_cny": 0.0},
    }


def _tail_jsonl(path: Path, *, offset: int, limit: int,
                keep: Any) -> tuple[int, int, list[dict[str, Any]]]:
    """按**字节游标**读一个 JSONL，返回 ``(起始偏移, 下一偏移, 行)``。

    照抄 ``web/app.py::_tail_trace`` 的两条既有结论：

      1. 游标必须是**字节偏移**，不能是 ``seq`` —— ``--resume`` 会把
         ``Tracer._seq`` 从 0 重开，按 seq 取会静默漏行或重复行。
      2. 只消费到**最后一个完整换行**：进程正在写的半行留到下一轮，
         否则 2 秒一次的轮询会周期性读到半截 JSON。

    ``offset < 0`` 是尾部哨兵：取最后 ``limit`` 条合格行。
    """
    size = path.stat().st_size
    start = end = max(0, offset)
    rows: list[dict[str, Any]] = []
    if offset < 0:
        with path.open("rb") as fh:
            first, end, body = _tail_window(fh, size, limit, keep)
        items = [it for it in _split_complete(body, first) if keep(it[1])][-limit:]
        start = items[0][0] if items else end
    else:
        if offset > size:                       # 文件比游标短 ⇒ 被重建过，从头流
            offset = start = 0
        with path.open("rb") as fh:
            fh.seek(offset)
            blob = fh.read()
        cut = blob.rfind(b"\n")
        if cut < 0:
            items = []                          # 只有半行 —— 下一轮再来
        else:
            items = [it for it in _split_complete(blob[: cut + 1], offset) if keep(it[1])]
            items = items[:limit]
            # ★ 一段里一条合格行都没有时游标也必须前进，否则客户端拿着同一个
            #   offset 反复要同一段，永远卡死（同 `_tail_trace`）。
            end = (items[-1][0] + len(items[-1][1]) + 1) if items else offset + cut + 1
    for _, raw in items:
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:            # pragma: no cover
            continue
        if isinstance(rec, dict):
            rows.append(rec)
    return start, end, rows


def _activity_stats(path: Path, size: int,
                    default_kind: str | None = None) -> dict[str, Any]:
    """增量统计右栏头部那几个数（模型调用 / 工具调用 / token / 花费）。

    **不每轮全量扫**：这份文件是每次调用一条、带整段提示词的，几百条就是几 MB，
    而右栏每 2 秒问一次。

    ``default_kind`` 给「没有 ``kind`` 字段的文件」兜底用 —— ``cache/llm.jsonl``
    的每行都是 ``{key, response}``（回放用的响应缓存，不认 kind），行行都是模型调用。
    不给它兜底的话，老 run 的右栏头部会写「0 次模型调用」而下面列着几百条回答。
    """
    st = _ACTIVITY_SCAN.setdefault(
        str(path.resolve()), {"offset": 0, "llm": 0, "tool": 0, "stage": 0, "failed": 0,
                              "hits": 0, "tokens": 0, "cost": 0.0})
    if size < st["offset"]:                     # 截断/重建 → 从头再来
        st.update(offset=0, llm=0, tool=0, stage=0, failed=0, hits=0, tokens=0, cost=0.0)
    blob = b""
    if size > st["offset"]:
        try:
            with path.open("rb") as fh:
                fh.seek(st["offset"])
                blob = fh.read()
        except OSError:                         # pragma: no cover
            blob = b""
    cut = blob.rfind(b"\n")
    if cut >= 0:
        st["offset"] += cut + 1
        for raw in blob[: cut + 1].splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            kind = rec.get("kind") or default_kind
            if kind == "llm":
                st["llm"] += 1
                if rec.get("cache_hit"):
                    st["hits"] += 1
            elif kind == "tool":
                st["tool"] += 1
            elif kind == "stage":
                st["stage"] += 1
                continue                        # 边界行没有成败、没有 token、没有花费
            else:
                continue
            if not rec.get("ok", True):
                st["failed"] += 1
            tokens = rec.get("prompt_tokens")
            cost = rec.get("cost_cny")
            if kind == "llm" and tokens is None:
                resp = rec.get("response") or {}      # cache 格式：用量在 response 里
                tokens = (int(resp.get("prompt_tokens") or 0)
                          + int(resp.get("completion_tokens") or 0))
                cost = None                           # 缓存里没有单价，不编一个出来
            st["tokens"] += int(tokens or 0)
            st["cost"] += float(cost or 0.0)
    return {
        "llm_calls": int(st["llm"]),
        "tool_calls": int(st["tool"]),
        "stages": int(st["stage"]),
        "failed": int(st["failed"]),
        "cache_hits": int(st["hits"]),
        "tokens": int(st["tokens"]),
        "cost_cny": round(float(st["cost"]), 4),
    }


def _read_activity(run_dir: Path, *, offset: int = -1, limit: int = 60,
                   kinds: set[str] | None = None) -> dict[str, Any]:
    """右栏的一帧数据。**绝不抛** —— 右栏挂在每一页上，它一炸就是整页空白。"""
    limit = max(1, min(int(limit), 500))
    want = set(kinds or ACTIVITY_KINDS)
    empty = empty_activity()

    journal = run_dir / "agent_log.jsonl"
    cache = run_dir / "cache" / "llm.jsonl"

    if journal.is_file():
        def keep(raw: bytes) -> bool:
            try:
                return str(json.loads(raw).get("kind")) in want
            except (json.JSONDecodeError, AttributeError):
                return False
        try:
            size = journal.stat().st_size
            start, end, rows = _tail_jsonl(journal, offset=offset, limit=limit, keep=keep)
            stats = _activity_stats(journal, size)
        except OSError as exc:                  # pragma: no cover
            return {**empty, "error": f"{type(exc).__name__}: {exc}"}
        for r in rows:
            r["src"] = "journal"
        # 空的 journal 要说出来，不能只留一片空白。
        # `Tracer.__init__` 一建就 append 打开 `agent_log.jsonl`（tracer.py:202），
        # 所以**每个** run 都有这个文件，一次模型调用都没发生的 run 也有 ——
        # 实测 21 个 run 里有 4 个是这样（`demo`/`cfg-test`/`prov-offline`/
        # `prov-online`：`trace.jsonl` 里只有 `stage` 事件）。那些 run 右栏是空的，
        # 而用户无从分辨「这个 run 没调过模型」和「右栏坏了」—— 项目里遇到这种
        # 情形一律给一句话（`未接入编排层`、`恒为 0`），这里同理。
        # 只认**整个文件为空**：文件非空而当前筛选下一行不剩，是筛选的结果，
        # 「没有记录到」那句话在那种时候是假的。
        # 不必回头再找 `cache/llm.jsonl`：那份缓存由**同一个** gateway 落盘，
        # 且**缓存命中那条路也走 `_log`**（`gateway.py:239-251`），所以一次模型
        # 调用都没有的 run，两份都是空的 —— 全仓 21 个 run 实测无一例外。
        return {**empty, "offset": start, "next_offset": end, "rows": rows,
                "source": "journal", "stats": stats,
                "note": ("" if size else
                         "该 run 没有记录到模型或工具调用 —— agent_log.jsonl 是空的")}

    if cache.is_file():
        # 老 run：只有模型**答了什么**。提示词、阶段、工具调用都没记。
        def keep_llm(raw: bytes) -> bool:
            return "llm" in want
        try:
            start, end, recs = _tail_jsonl(cache, offset=offset, limit=limit,
                                           keep=keep_llm)
        except OSError as exc:                  # pragma: no cover
            return {**empty, "error": f"{type(exc).__name__}: {exc}"}
        rows = []
        for i, rec in enumerate(recs):
            resp = rec.get("response") or {}
            rows.append({
                "kind": "llm", "src": "cache", "seq": i,
                "text": resp.get("text") or "",
                "prompt_tokens": resp.get("prompt_tokens") or 0,
                "completion_tokens": resp.get("completion_tokens") or 0,
                "finish_reason": resp.get("finish_reason"),
                "args": None, "result": None, "messages": None,
            })
        stats = _activity_stats(cache, cache.stat().st_size, default_kind="llm")
        return {**empty, "offset": start, "next_offset": end, "rows": rows,
                "source": "cache",
                "note": "该 run 早于对话记录器：只有模型回答，没有提示词与工具调用",
                "stats": stats}

    return {**empty, "error": "该 run 没有对话记录（agent_log.jsonl 与 cache/llm.jsonl 都不存在）"}


def _run_provider(run_dir: Path | None) -> str:
    """这个 run 用的是哪个 provider —— 只为了一件事：把「离线自检」标出来。

    ``--mock`` 的 run 里，模型回答是 mock provider 的**罐头**，不是真模型的输出。
    两种 run 的右栏长得一模一样（结构、字段、流式追加全都相同），差别只在
    provider 这一个字段上。不标出来的话，「模型在干什么」这一栏会把罐头当成
    模型的真话 —— 而它恰恰是给人看模型说什么的地方。

    两个来源，**顺序不能反**：

      · ``manifest.json`` —— 跑完才落盘，权威但要等。
      · ``checkpoint.json`` —— 每完成一个阶段就落一次，里面也记着 provider。
        正在跑的 run 只有这一份，而**运行中恰恰是右栏最在被看的时刻**：
        只看 manifest 的话，mock 披露会在最该出现的那段时间里恰好是空的。
    """
    if run_dir is None:
        return ""
    for name in ("manifest.json", "checkpoint.json"):
        try:
            info = json.loads((run_dir / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        got = str(info.get("provider") or "")
        if got:
            return got
    return ""


def _g(v: Any) -> str:
    """把产物里的数值串按**与事实库同一规则**分组（见 ``group_thousands``）。

    ``None`` 在产物里表示「这一项没有读数」（实测：中石油那份 comps 的
    ``ev_ebitda`` 与 ``ps`` 都是 null），**不能**渲染成 ``0`` ——
    那会把「没读到」说成「读到了，是零」。
    """
    return group_thousands(str(v)) if v is not None and v != "" else ""


# ==========================================================================
# 结果读数：把产物里的结论抽出来，模板只负责摆表格
# ==========================================================================


def _run_dir(root: Path, rid: str | None) -> tuple[str | None, Path | None, str | None]:
    """把 run 名解析成目录。**刻意不要求 manifest.json**。

    ``web/artifacts.py::resolve_run`` 会把没有 manifest 的 run 判为「未完成」并
    排除掉 —— 但**正在运行的 run 恰恰没有 manifest**（它跑完才落盘）。面板的过程区
    在运行时必须能读到自己的 run，所以这里只做同形校验 + 路径包含检查。
    """
    root = root.resolve()
    rid = (rid or "").strip()
    if not rid:
        listed = available_runs(root)
        rid = listed[0].run_id if listed else ""
    if not rid:
        return None, None, "runs/ 下还没有任何运行"
    if not AZ._RUN_ID_RE.match(rid):
        return None, None, f"run 名不合法：{rid}"
    d = (root / rid).resolve()
    if root not in d.parents or not d.is_dir():
        return None, None, f"没有这个 run：{rid}"
    return rid, d, None


# ==========================================================================
# 按赛题切开流水线：R1 / R4 / R5 各自跑了哪几步、用了多久、调了几次模型
# ==========================================================================

#: 流水线的阶段名自带赛题前缀（`r1_ingest`、`r4_model`、`r5_check`），
#: 照前缀切开即可。**不另立一张「阶段→赛题」的对照表** —— 流水线加阶段时，
#: 对照表不会报错，只会让新阶段在页面上静默消失。
TASKS = ("r1", "r4", "r5")

#: 三个标签页的名字。`run` 是「怎么跑」，其余三个是「跑出了什么」。
TAB_LABELS = {
    "run": "运行",
    "r1": "R1 提取",
    "r4": "R4 估值",
    "r5": "R5 核查",
}
TAB_ORDER = ("run", "r1", "r4", "r5")


def _seconds_between(t0: Any, t1: Any) -> float | None:
    """两个 ISO 时间戳之间差几秒；解析不了就返回 ``None``（**不是 0**）。

    0 秒与「不知道多久」在页面上必须分得开：前者是「这一步没花时间」，
    后者是「这次运行没留下这段记录」。
    """
    if not t0 or not t1:
        return None
    try:
        a = datetime.fromisoformat(str(t0).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(t1).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (b - a).total_seconds())


#: 从（可能被截断的）JSON 里抠 `"键": 标量` 用的。**不用 `json.loads` 兜底** ——
#: 见 `_stage_note` 里那段解释。
_SCALAR_PAIR_RE = re.compile(
    r'"([A-Za-z_][A-Za-z0-9_]*)":\s*'
    r'("(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?|true|false|null)')

#: 一行里最多印几个计数。四个够看出「这一步在干什么」，再多就把表格挤歪了。
_MAX_NOTE_PAIRS = 4


def _stage_note(detail: Any) -> str:
    """阶段收尾时留下的那点读数，压成一行。

    实测两种形状：一种是 JSON 计数字典（`{"vlm_calls": 400, "page_scans": 41,
    …}`，r1_judge 一口气给出十几个真实计数），一种是短句（「部分失败：单元格
    裁决调用失败 5」）。前者取前几个标量当 `k=v` 排开 —— 这些计数是**这一步
    到底干了什么**的直接证据，比任何描述都清楚；后者原样留着。

    ★ 追迹层会把过长的 `detail` **截断**，于是页面上拿到的是「看起来像 JSON、
      其实解析不了」的一段文本。早先这里 `json.loads` 失败就退回原文，结果
      R4-1 那一行印出的是 `{"entity": "601857", "assumptions": 8, &#34;gaps&#34;:
      []…` —— 一屏转义后的花括号，页面上最难看的一处。所以解析失败时改用
      正则逐个抠前几个 `"键": 标量`，抠不到才认输。
    """
    text = str(detail or "").strip()
    if not text:
        return ""
    if not text.startswith("{"):
        return text[:120]

    try:
        obj = json.loads(text)
    except ValueError:
        obj = None
    if isinstance(obj, dict):
        items = [(k, v) for k, v in obj.items()
                 if isinstance(v, (int, float, str)) and not isinstance(v, bool)]
    else:
        items = [(m.group(1), json.loads(m.group(2)))
                 for m in _SCALAR_PAIR_RE.finditer(text)]

    pairs = []
    for k, v in items[:_MAX_NOTE_PAIRS]:
        if isinstance(v, str) and len(v) > 24:
            v = v[:24] + "…"
        pairs.append(f"{k}={v}")
    return " · ".join(pairs)


def _task_stages(run_dir: Path | None) -> dict[str, list[dict[str, Any]]]:
    """把流水线的阶段按赛题切开：各自的用时、模型调用、花费、收尾读数。

    为什么要从 `trace.jsonl` **现算**，而不是读 `checkpoint.json`：断点只记
    「哪些阶段完成了」，没有时长，也没有「这一步花了几次模型调用、多少钱」。
    而那三样恰恰是三个标签页各自要回答的东西 —— 流水线是一条，读的人要按选题
    分开看，所以这里必须切得开。

    ★ 只认 `r1_*` / `r4_*` / `r5_*` 这组**流水线声明的**阶段名。追迹里同时还有
      一份不带前缀的同名事件（`ingest`、`assumptions`、`audit`…），是流水线内部
      另行发的；两套都收会让每个阶段印成两行、时长对半。
    """
    out: dict[str, list[dict[str, Any]]] = {t: [] for t in TASKS}
    trace = (run_dir / "trace.jsonl") if run_dir else None
    if trace is None or not trace.is_file():
        return out

    live: dict[str, dict[str, Any]] = {}
    cur: dict[str, Any] | None = None      # 最近开着的阶段，模型调用记到它头上
    try:
        with trace.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:             # 半行 / 坏行：跳过，不影响别的事件
                    continue
                kind = ev.get("kind")

                if kind == "llm_call":
                    if cur is not None:
                        cur["calls"] += 1
                        cur["tokens"] += int(ev.get("prompt_tokens") or 0) + \
                                         int(ev.get("completion_tokens") or 0)
                        cur["cost"] += float(ev.get("cost_cny") or 0.0)
                    continue

                if kind != "stage":
                    continue
                name = str(ev.get("stage") or "")
                task, _, _ = name.partition("_")
                if task not in out:
                    continue
                status = str((ev.get("extra") or {}).get("status") or "")

                rec = live.get(name)
                if rec is None:
                    rec = {"name": name, "t0": ev.get("ts"), "t1": ev.get("ts"),
                           "status": status, "calls": 0, "tokens": 0, "cost": 0.0,
                           "note": "", "seconds": None}
                    live[name] = rec
                    out[task].append(rec)
                    cur = rec
                if status and status != "start":
                    rec["t1"] = ev.get("ts") or rec["t1"]
                    rec["status"] = status
                    note = _stage_note((ev.get("extra") or {}).get("detail"))
                    if note:
                        rec["note"] = note
    except OSError:                                # pragma: no cover - 追迹读不了
        return out

    for task in TASKS:
        for rec in out[task]:
            rec["seconds"] = _seconds_between(rec["t0"], rec["t1"])
    return out


def _task_rollup(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """一个赛题那几步的合计。给标签上那个徽标用。"""
    known = [r["seconds"] for r in rows if r["seconds"] is not None]
    return {
        "n_stages": len(rows),
        "seconds": round(sum(known), 1) if known else None,
        "calls": sum(r["calls"] for r in rows),
        "cost": round(sum(r["cost"] for r in rows), 4),
        "done": sum(1 for r in rows if r["status"] in ("ok", "warn", "partial")),
        "warn": sum(1 for r in rows if r["status"] in ("warn", "partial")),
        "fail": sum(1 for r in rows if r["status"] in ("fail", "error")),
        "running": sum(1 for r in rows if r["status"] == "start"),
    }


def _render_stages(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给模板摆的形态：秒数/费用先格式化好，模板不做算术。"""
    out = []
    for r in rows:
        out.append({
            **r,
            "secs": f"{r['seconds']:.1f}s" if r["seconds"] is not None else "—",
            "cost_s": f"¥{r['cost']:.4f}" if r["cost"] else "",
            "tokens_s": f"{r['tokens']:,}" if r["tokens"] else "",
            "cls": {"ok": "done", "warn": "warn", "partial": "warn",
                    "fail": "fail", "error": "fail", "start": "run"}.get(r["status"], ""),
        })
    return out


def _facts_counts(r: RunArtifacts) -> tuple[int, int]:
    """事实库**总共多少条** + 其中多少条打了「需复核」。

    ★ 这里原先还返回一份全量事实行，供页面印一张几百行的表。那张表 2026-10-16
      拿掉了 —— R1 标签页现在的形状是「评分点表 → 阶段明细 → 读数 → 产物链接」，
      逐条事实（指标/值/单位/期间/页码/置信度）属于 ``facts/facts.json`` 与
      ``evidence.html``，点链接看全量。
      ★ 「不许抽样、不许指到文件」那条老规矩没有被放松，它管的是**印出来的东西
      必须完整**；现在一条都不印，就不存在「印一半」这件事。留在这里的两个数
      仍是现读产物算的：标签徽标要「N 条事实」，那颗 warn 药丸要「N 条需复核」。
    """
    facts = r.build_facts()
    return len(facts), sum(1 for f in facts if f.needs_review)


def _valuation_view(r: RunArtifacts) -> dict[str, Any] | None:
    """估值结论：区间 / 交付图表 / 假设 / 可比公司 / 勾稽断言。

    ★ 假设表**不渲染 ``basis``** —— 那是一段几百字的推理文本（「由事实库算出：
    2025A 折旧摊销 … ÷ 营业收入 … = 7.53%」）。它是**交付物里的**证据，正文该去
    `valuation_report.md` 看；放进控制面板会把 8 行表格撑成八屏。

    ★ 敏感性的两张数表（7×7 网格、摆幅）2026-10-16 从页面上拿掉，换成那四张
      **交付图**。理由是同一条规矩「同一个数只出现一次」：网格的 49 格值已经
      逐格印在 ``sensitivity_heatmap.png`` 里，摆幅的低/高/基准线已经在
      ``tornado.png`` 里 —— 再摆一张表，读者要做的第一件事就是核对两处是否
      一致。顺带，网格的行列序也不再由本页决定（出图那一侧
      ``pipelines/r4_valuation/report.py`` 自己做数值排序），这一页少一处
      可能排错的地方。
    """
    val = r.valuation()
    if not val:
        return None
    comps = val.get("comps") or {}

    # 勾稽断言：按**编号**归并（实测 A1–A5 各出一期一条，26 条里 A1 占 5 条），
    # 逐条摆会把同一句名称重复五遍。归并后一行一个断言，读者看的是
    # 「哪一类没通过」，而不是「第 3 期的 A1 又通过了一次」。
    # `skipped` 必须与 `pass` 分开印：缺明细、算不了 ≠ 通过。
    grouped: dict[str, dict[str, Any]] = {}
    for a in (val.get("assertions") or []):
        key = str(a.get("code") or "?")
        g = grouped.setdefault(key, {"code": key, "name": a.get("name"),
                                     "n": 0, "pass": 0, "skipped": 0, "fail": 0})
        g["n"] += 1
        st = str(a.get("status") or "")
        if st == "pass":
            g["pass"] += 1
        elif st == "skipped":
            g["skipped"] += 1
        else:
            g["fail"] += 1
    assertions = []
    for g in grouped.values():
        g["mark"] = "×" if g["fail"] else ("√" if g["pass"] == g["n"] else "◐")
        assertions.append(g)

    return {
        "entity": val.get("entity"),
        "low": val.get("value_range_low"),
        "mid": val.get("value_range_mid"),
        "high": val.get("value_range_high"),
        "as_of": val.get("as_of"),
        "assertions": assertions,
        "assumptions": [
            {
                "name": a.get("name"),
                "value": a.get("value"),
                "unit": a.get("unit"),
                "n_facts": len(a.get("source_fact_ids") or []),
                "judgment": bool(a.get("is_judgment")),
            }
            for a in (val.get("assumptions") or [])
        ],
        # 四张交付图，**只列磁盘上确实有的**（`valuation_charts` 按存在性过滤）。
        # `path` 是相对 run 目录的路径，模板拼成 `/artifact?run=…&path=…` 取图。
        "charts": [
            {"name": name, "title": title, "path": f"valuation/charts/{name}"}
            for name, title in r.valuation_charts().items()
        ],
        "peers": [
            {"name": p.get("name"), "ticker": p.get("ticker"),
             "pe": _g(p.get("pe")), "pb": _g(p.get("pb")),
             "ev_ebitda": _g(p.get("ev_ebitda")),
             "cap": _g(p.get("market_cap"))}
            for p in (comps.get("peers") or [])
        ],
        "comps_median_pe": comps.get("median_pe"),
        "comps_median_pb": comps.get("median_pb"),
        "comps_low": comps.get("implied_value_per_share_low"),
        "comps_mid": comps.get("implied_value_per_share_mid"),
        "comps_high": comps.get("implied_value_per_share_high"),
    }


def _audit_view(r: RunArtifacts) -> dict[str, Any] | None:
    """核查结论：问题清单 + 指标。

    ``reason`` 同样是整段推理文本，只在 ``annotated_draft.html`` 里给 —— 面板里
    一行一件事：在哪儿、错成什么、应该是什么。
    """
    audit = r.audit()
    if not audit:
        return None
    findings = audit.get("findings") or []
    metrics = audit.get("metrics") or {}
    return {
        "findings": [
            {
                "id": f.get("finding_id"),
                "type": f.get("error_type"),
                "severity": f.get("severity"),
                "section": (f.get("location") or {}).get("section"),
                "page": (f.get("location") or {}).get("page"),
                "reported": f.get("reported_value"),
                "correct": f.get("correct_value"),
            }
            for f in findings
        ],
        "n_findings": len(findings),
        # 只留**标量**指标：产物里这一栏将来若加进嵌套结构，直接往页面上倒
        # 会印出一串 Python dict 的字面量。控制台只印一眼能读的读数，
        # 嵌套的明细属于 audit_report.md。
        "metrics": {k: v for k, v in metrics.items()
                    if isinstance(v, (int, float, str)) and len(str(v)) <= 40},
        "draft": audit.get("draft_doc_id"),
    }


def _criteria_views(r: RunArtifacts,
                    runs_root: Path) -> dict[str, list[dict[str, str]]]:
    """三个赛题各自的「重点考察」实测表 —— 直接调用展示站那三套算法。

    ★ **不在这里另写一份。** 评分点表是评分点：两处口径不一致比只有一处更糟 ——
      演示时评委看的是这一页，答辩时核的是展示站那一页，同一个考察点在两边印出
      不同的记号，等于交了两份互相打架的读数。`web/app.py` 的
      ``criteria_r1/r4/r5`` 是既有实现（含 R4 ④ 那条「合理性没有客观分数线，
      所以只能记 ◐」的判断），原样调用，一个字都不改。

    ★ R5 的准确率 / 覆盖率 / 误报率**必须有黄金标注集**才算得出来，而演示 run
      用的是合成草稿、没有标注。这三条因此走 ``find_eval_run`` 取评测 run 的数，
      并在 note 里写明来源 run_id —— 拿别的 run 的成绩不标来源，读者会把它读成
      本 run 的。本 run 自己带 metrics 时优先用本 run 的（与展示站同一优先级）。

    ``runs_root`` 就是 ``create_panel(runs_root=…)`` 那个根：``find_eval_run``
    扫的是 ``runs_root/r5-eval*``，与 ``r.dir.parent`` 是同一个目录。
    """
    audit = r.audit() or {}
    eval_run_id, eval_metrics = find_eval_run(runs_root)
    return {
        "r1": criteria_r1(r),
        "r4": criteria_r4(r, r.valuation()),
        "r5": criteria_r5(r, audit, audit.get("metrics"), eval_metrics, eval_run_id),
    }


def _results(r: RunArtifacts) -> dict[str, Any]:
    """三块结果 + 可下载的产物清单（只列**确实存在**的文件）。

    ★ 两份长报告（``valuation_report.md`` / ``audit_report.md``）**不嵌进页面**，
      只给链接。理由不是省事，是这条：报告正文是**产物**，控制台是**读数**。
      实测把两份报告渲进页面会让单页 HTML 从 15 KB 涨到 45 KB，
      而多出来的 30 KB 里绝大部分是模型的推理长文（假设的 ``basis``、
      核查的 ``reason``）—— 那正是这一版控制台要求清掉的东西。
      结论级的读数已经在上面的表里（区间、假设取值、可比倍数、勾稽断言、
      问题清单），要看论证过程，点产物链接。

    ★ 事实库同理，2026-10-16 起也只给链接（``facts/facts.json``）：356 行表格
      属于产物，页面上留两个数（总条数 / 需复核条数）与一个入口。
    """
    present = {o["path"] for o in r.outputs()}

    def pick(*names: str) -> list[dict[str, str]]:
        return [{"path": n, "name": n.split("/")[-1]} for n in names if n in present]

    n_facts, n_review = _facts_counts(r)
    return {
        "run_id": r.run_id,
        "summary": r.summary,
        "n_outputs": len(present),
        "r1": {
            "n_facts": n_facts,
            "n_review": n_review,
            "n_docs": len(r.documents),
        },
        # 产物链接一律走 `<题>_files` 这个顶层键（`_task_view` 按
        # `files_key = task + "_files"` 一处取）。★ R1 的这一份**不能**顺手塞进
        # `results["r1"]["files"]` 里：那样 `files_key` 就得是 `"r1"`，取回来的是
        # 整个 r1 视图字典，模板 `{% for f in tv.files %}` 会去遍历字典的**键名**，
        # 印出四个 `path=` 为空的链接 —— 页面照常 200、字数照常，只有点下去才发现
        # 打不开。三个键名对齐之后，这个位置本身就不存在了。
        #
        # ★ R1 只给 `facts.json` 一个入口（`facts.xlsx` / `evidence.html` /
        #   `documents.json` 不列）：这一页的读数是「多少条事实、多少条需复核」，
        #   出处就是那一份 JSON。四个链接并排会把「结论的出处」摊成一份文件列表，
        #   而演示时任何一个点错都是当场卡壳。其余三份仍在 `runs/<id>/facts/` 下，
        #   答辩要翻底稿直接开目录。
        "r1_files": pick("facts/facts.json"),
        "r4": _valuation_view(r),
        # ★ 同 R1：只给 `valuation_report.md` 一个入口（模型表 / 原始 JSON 不列）。
        #   这一页的读数是区间中值、假设取值、可比倍数、勾稽断言；要说这些数是怎么
        #   推出来的，看的就是那份报告。三个链接并排会把「一份报告 + 两个中间件」
        #   摊平，演示时最容易被点开的反而不是报告。
        "r4_files": pick("valuation/valuation_report.md"),
        "r5": _audit_view(r),
        "r5_files": pick("audit/audit_report.md", "audit/annotated_draft.html",
                         "audit/revision_table.xlsx"),
    }


def _task_view(results: dict[str, Any] | None, task: str,
               stage_rows: list[dict[str, Any]],
               criteria: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """一个赛题标签页要的全部东西：评分点 + 这一步跑了什么 + 跑出了什么。

    这一版控制台的核心改动：把「一条流水线」按赛题切成三份。每个标签页
    各自回答同一个形状的问题 —— **赛题在这里考察什么（评分点） · 这块跑了
    哪几步（用时/模型调用/花费） · 产出了什么（读数） · 产物在哪（链接）**
    —— 而不是让三块内容在一屏里排队等着被滚动。

    ``criteria`` 由 ``_criteria_views`` 算好按题传进来，不在这里现算：
    它要的是**同一个** ``RunArtifacts``（见 ``_page``），而 ``results`` 只是
    已经转好的视图模型。
    """
    # ★ 键名必须与 `_results` 里那三个顶层键**逐字对齐**（`r1_files` / `r4_files` /
    #   `r5_files`）。早先这里是 `{"r1": "r1"}`,取到的是整个 r1 视图字典，模板
    #   遍历出四个空 `path=` 的链接（见 `_results` 里那段注释）。
    files = (results or {}).get(f"{task}_files") or []
    return {
        "task": task,
        "label": TAB_LABELS[task],
        "stages": _render_stages(stage_rows),
        "rollup": _task_rollup(stage_rows),
        "view": (results or {}).get(task),
        # 评分点表排在阶段明细之前，且**不依赖有没有产物**：R4 没跑时它会印一行
        # 「无产物 ○」，比一片空白说得清楚。
        "criteria": criteria or [],
        "files": files,
        # 这一块**有没有产物**是标签页首先要说的事：R4 没跑过与 R4 跑了但没结论
        # 是两件事，前者要看运行页，后者要看这一步自己报了什么问题。
        "empty": (results or {}).get(task) is None,
    }


# ==========================================================================
# 应用
# ==========================================================================


def create_panel(*, runs_root: str | Path | None = None,
                 default_run: str | None = None) -> FastAPI:
    app = FastAPI(title="金融投研智能体 · 控制台", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    # ★ 复用展示站的过滤器（`md` / `prose` / `marks`），不另建一套 ——
    #   报告正文的渲染口径两处必须一致，否则同一个 md 在两边长得不一样。
    register_filters(templates.env)

    root = Path(runs_root) if runs_root else DEFAULT_RUNS_ROOT
    state: dict[str, Any] = {"runs_root": root, "default_run": default_run}

    def _page(request: Request, rid: str | None = None, tab: str | None = None,
              **extra: Any) -> Any:
        """渲染面板。任何一步失败都降级成一个 error 字符串，页面照常 200。

        ``extra`` 里可能带着 ``?err=``（``/start`` 把失败原因甩回来的那条路）。
        **两条 err 的优先级必须显式写死**，不能让字典字面量的书写顺序决定 ——
        早先 ``**extra`` 排在最后，于是 `?err=`（没失败时就是 ``None``）
        把 ``_run_dir`` 算出来的「run 名不合法」整个盖掉：run 名写错时
        页面一片空白加一句解释都没有。**有具体原因时用具体原因，没原因时用
        `_run_dir` 的**，两者都不许把对方清成空。
        """
        flash = extra.pop("err", None)
        tab = tab if tab in TAB_ORDER else "run"
        run_sel = rid or state["default_run"]
        resolved, run_dir, err = _run_dir(root, run_sel)
        results = None
        criteria: dict[str, list[dict[str, str]]] = {}
        if run_dir is not None:
            try:
                # ★ 评分点表与结果读数共用**同一个** RunArtifacts：各建一个实例
                #   会把 facts.json / tables / valuation.json 那几份产物整个再读
                #   一遍（实测中石油那份 facts.json 有 356 条）。
                art = RunArtifacts(run_dir)
                results = _results(art)
                criteria = _criteria_views(art, root)
            except ArtifactError as exc:            # pragma: no cover - 产物损坏
                err = str(exc)
        # `log_tail=None` = 日志全给。控制台不许拿「末 N 行」把数据挡在外面
        # （展示站默认 40 行是另一回事：那一页的定位是概览）。
        task = AZ.status(runs_root=root, log_tail=None)
        try:
            inv = AZ.inventory()
        except Exception:                           # pragma: no cover - 收件目录不可读
            inv = {"pdf_count": 0, "pdfs": [], "drafts": 0, "est_pages": 0,
                   "est_minutes": 0, "est_cny": 0, "inbox": str(AZ.INBOX),
                   "pages_known": 0, "comps": 0, "market": 0,
                   "comps_targets": [], "codes_without_comps": [],
                   "raw_dir": str(AZ.INBOX_RAW), "r4_ready": False}

        stage_map = _task_stages(run_dir)
        # 标签条：每一项都带上**自己能回答的数**（几个阶段 / 几条事实 / 每股价值 /
        # 几处问题），这样不点进去也知道那边有什么。空的那一项标出来 ——
        # 「R4 没跑」是要立刻看见的，不是点进去才发现。
        r4v = (results or {}).get("r4") or {}
        r5v = (results or {}).get("r5") or {}
        badges = {
            "run": f"{len(task.get('completed') or [])}/{len(task.get('stages') or [])} 阶段",
            "r1": f"{(results or {}).get('r1', {}).get('n_facts', 0) or 0} 条事实",
            "r4": f"{r4v.get('mid')} 元/股" if r4v.get("mid") else "无估值",
            "r5": f"{r5v.get('n_findings', 0) or 0} 处问题",
        }
        tabs = [{"key": k, "label": TAB_LABELS[k], "badge": badges[k],
                 "n_stages": len(stage_map.get(k, [])),
                 "on": k == tab} for k in TAB_ORDER]

        return templates.TemplateResponse(
            request, "panel.html",
            {
                "request": request,
                "task": task, "inv": inv, "runs": available_runs(root),
                "run_id": resolved, "provider": _run_provider(run_dir),
                "results": results,
                "err": flash or err,
                "new_run_id": AZ.new_run_id(),
                # 每个赛题标签页自带的那一份：评分点表 + 阶段明细 + 读数 + 产物链接。
                "task_views": {t: _task_view(results, t, stage_map.get(t, []),
                                             criteria.get(t) or [])
                               for t in TASKS},
                "run_stages": _render_stages(
                    [r for r in stage_map.get("r1", []) + stage_map.get("r4", [])
                     + stage_map.get("r5", [])]),
                "tabs": tabs, "tab": tab,
                # 日志在服务端就拼成一段**带真换行的文本**再交给 `<pre>`。
                # 让模板去 join 会撞上「模板里的 `\n` 到底转不转义」这个
                # 看实现而定的问题，而这个页面在两种写法下都长一样 ——
                # 那正是最容易一路错下去的地方。
                "log_text": "\n".join(task.get("log") or []),
                **extra,
            },
        )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, run: str | None = None, tab: str | None = None,
              err: str | None = None, uploaded: int | None = None) -> Any:
        return _page(request, run, tab, err=err, uploaded=uploaded)

    # ---------------------------------------------------------------- 运行

    @app.post("/start")
    def start(run_id: str = Form(""), confirm: str = Form(""), mock: str = Form(""),
              resume: str = Form("")) -> Any:
        """起一次运行。所有可展示的失败原因都以 ``?err=`` 回到页面上，不吞。

        ★ 「离线自检」自己就满足费用闸门：``mock`` 这条路径**不调用模型**
        （``build_argv`` 给它加 ``--mock``），所以要它再勾一遍
        「我知道会产生费用」是自相矛盾的。真实运行（不勾自检）仍然
        必须有那个勾 —— 闸门一个字都没放松。
        """
        mock_on = bool(mock)
        try:
            st = AZ.start(run_id=run_id.strip() or None,
                          confirmed=bool(confirm) or mock_on,
                          mock=mock_on, resume=bool(resume), runs_root=root)
            # 起完停在「运行」页：刚点的就是这个按钮，接下来要看的当然是进度。
            return RedirectResponse(
                f"/?tab=run&run={quote(str(st.get('run_id') or ''))}", status_code=303)
        except AZ.AnalyzeError as exc:
            # ★ 失败原因里有中文（「收件目录里没有 PDF：…」），必须转义后再进查询串 ——
            #   直接拼进 Location 头会被 http 客户端判成非法 URL。
            return RedirectResponse(f"/?tab=run&err={quote(str(exc))}", status_code=303)

    @app.post("/upload")
    async def upload(files: list[UploadFile] = File(default=[])) -> Any:
        """把 PDF 收进 ``inbox/raw/`` —— 之后「开始运行」用的就是它们。"""
        AZ.ensure_inbox()
        AZ.INBOX_RAW.mkdir(parents=True, exist_ok=True)
        n = 0
        for f in files:
            name = Path(f.filename or "").name        # 只取文件名，防 `..` 越权
            if not name or Path(name).suffix.lower() != ".pdf":
                continue
            (AZ.INBOX_RAW / name).write_bytes(await f.read())
            n += 1
        return RedirectResponse(f"/?tab=run&uploaded={n}", status_code=303)

    # ---------------------------------------------------------------- 过程

    @app.get("/api/status")
    def api_status() -> Any:
        """任务状态：阶段进度、当前阶段、用时、日志**全文**。面板每 2 秒取一次。"""
        st = AZ.status(runs_root=root, log_tail=None)
        summary = st.get("run_summary") or {}
        return JSONResponse({
            "phase": st.get("phase"), "run_id": st.get("run_id"),
            "progress": st.get("progress"), "current": st.get("current"),
            "completed": st.get("completed"), "failed": st.get("failed"),
            "skipped": st.get("skipped"), "elapsed_s": st.get("elapsed_s"),
            "stages": [s["name"] for s in (st.get("stages") or [])],
            # 日志先过 `plain_marks`：这一批文本会被 JS 用 `textContent` 写回，
            # 不做这一步的话首屏（服务端 `|marks`）与轮询后（原样）字形不一样。
            # 见 `web/app.py::plain_marks`。
            "log": [plain_marks(line) for line in (st.get("log") or [])],
            "resumable": st.get("resumable"),
            "run_status": st.get("run_status"),
            "tokens": summary.get("total_tokens"),
            "cost_cny": summary.get("total_cost_cny"),
            "llm_calls": summary.get("llm_calls"),
        })

    @app.get("/api/trace")
    def api_trace(run: str | None = None, offset: int = 0, limit: int = 200,
                  kinds: str | None = None) -> Any:
        """Agent 事件流（工具调用 / 阶段 / 断言 / 模型调用…）。契约同 ``/trace/stream``。"""
        rid, run_dir, err = _run_dir(root, run or state["default_run"])
        base = {"run_id": rid, "offset": 0, "next_offset": 0, "eof": True,
                "live": False, "rows": [], "error": err,
                "stats": {"total_events": 0, "by_kind": {}, "errors": 0,
                          "cost_cny": 0.0, "tokens": 0}}
        if run_dir is None:
            return JSONResponse(base)
        want: set[str] | None = None
        if kinds:
            want = {k.strip() for k in kinds.split(",") if k.strip() in _ALL_TRACE_KINDS}
            want = want or None
        payload = _tail_trace(run_dir, offset=offset, limit=limit, kinds=want)
        payload["run_id"] = rid
        # 「还在写」= 没有 manifest（manifest 只在这一跑结束时落盘）。
        payload["live"] = not (run_dir / "manifest.json").exists()
        return JSONResponse(payload)

    @app.get("/api/agent-log")
    def api_agent_log(run: str | None = None, offset: int = -1, limit: int = 60,
                      kinds: str | None = None) -> Any:
        """右栏：模型与工具的**原文**流（提示词 / 回答 / 工具参数 / 工具返回）。

        与 ``/api/trace`` 是两路数据，不是同一路的不同视图：

          · ``/api/trace`` 读 ``trace.jsonl``，事件是定死的契约、载荷只有哈希 ——
            它回答「跑了哪些步、各花多久、调了几次模型」。
          · 这里读 ``agent_log.jsonl``，载荷是原文 —— 它回答「模型看到什么、
            说了什么；工具收到什么、回了什么」。看过程要的是后者。

        ``offset=-1`` 是尾部哨兵（打开右栏先看最近发生了什么）。
        """
        rid, run_dir, err = _run_dir(root, run or state["default_run"])
        if run_dir is None:
            return JSONResponse({**empty_activity(), "run_id": rid, "live": False,
                                 "error": err})
        want = None
        if kinds:
            want = {k.strip() for k in kinds.split(",") if k.strip() in ACTIVITY_KINDS}
            want = want or None
        payload = _read_activity(run_dir, offset=offset, limit=limit, kinds=want)
        payload["run_id"] = rid
        payload["live"] = not (run_dir / "manifest.json").exists()
        payload["provider"] = _run_provider(run_dir)
        return JSONResponse(payload)

    # ---------------------------------------------------------------- 产物下载

    @app.get("/artifact")
    def artifact(run: str | None = None, path: str = "") -> Any:
        """打开 run 目录里的一个产物（xlsx / json / md / html / png）。

        文本类（``md`` / ``html`` / ``json`` / ``txt`` / ``csv``）与图片
        （``png`` / ``jpg``）**内联显示**，其余（``xlsx`` 等）走下载。
        默认的 ``FileResponse(..., filename=...)`` 一律带
        ``Content-Disposition: attachment``，于是「查看核查报告」会变成
        「下载一个文件」—— 演示时点开报告却弹出下载框，而这个报告正是
        选题 5 的交付物。所以按类型分开处置，并**显式给字符集**：
        不给的话浏览器对 ``text/plain`` 用本地编码猜，中文会乱码。

        ★ 图片这一支是 R4 标签页要的：那四张交付图用
          ``<img src="/artifact?run=…&path=valuation/charts/…png">`` 直接嵌在
          页面上。不给 ``image/*`` 的话浏览器会把它们当附件下载，页面上只剩
          四个空框 —— 而演示现场最怕的正是这个。
          刻意**不认** ``.svg``：内联 SVG 是脚本执行的入口（XSS 面），
          这一页只需要位图。
        """
        rid, run_dir, err = _run_dir(root, run)
        if run_dir is None or err:
            return JSONResponse({"error": err or "run 不可用"}, status_code=404)
        target = (run_dir / path).resolve()
        if run_dir.resolve() not in target.parents or not target.is_file():
            return JSONResponse({"error": f"产物不存在：{path}"}, status_code=404)
        suffix = target.suffix.lower()
        inline_type = {
            ".md": "text/plain; charset=utf-8",
            ".txt": "text/plain; charset=utf-8",
            ".csv": "text/plain; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".html": "text/html; charset=utf-8",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
        }.get(suffix)
        if inline_type:
            return FileResponse(target, media_type=inline_type)
        return FileResponse(target, filename=target.name)

    return app


def main() -> int:
    ap = argparse.ArgumentParser(description="金融投研智能体 · 控制台")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--run", default=None, help="结果区默认打开哪个 run")
    ap.add_argument("--runs-root", default=None)
    args = ap.parse_args()

    app = create_panel(runs_root=args.runs_root, default_run=args.run)
    print("=" * 60)
    print(f"  控制台：http://{args.host}:{args.port}")
    print(f"  runs：{args.runs_root or DEFAULT_RUNS_ROOT}")
    print(f"  收件目录：{AZ.INBOX}")
    print("=" * 60)
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
