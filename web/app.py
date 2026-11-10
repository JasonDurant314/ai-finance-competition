"""Web 演示台入口 —— FastAPI 应用与全部页面。

## 页面与它回答的问题

    /           这次参赛做了哪三题？各自结果如何？             ← 答辩第一屏
    /library    这次演示用的是哪些材料？解析状态如何？
    /extract    三层解析引擎是怎么工作的？为什么这一页走 OCR？   ← 展示重点 1
    /facts      这个数字凭什么可信？原文在哪一页哪一行？         ← 展示重点 2
    /valuation  估值模型算出了什么？哪些假设最敏感？
    /audit      报告里有哪些错误？正确值是什么？依据是什么？
    /runs       这次运行能不能复现？与上次比有何变化？
    /process    现场喂入一个新文件，实时看三层引擎跑一遍

启动：
    python -m web.app [--host 127.0.0.1] [--port 8000] [--run <run_id>]
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from web import chartdata as CD
from web.artifacts import (
    DEFAULT_RUNS_ROOT,
    PROJECT_ROOT,
    ArtifactError,
    RunArtifacts,
    available_runs,
    load_run,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.console import enable_utf8_output  # noqa: E402

# Windows 控制台默认 GBK：本脚本会打印 ✓ / ✅ 这类符号，
# 不先切 UTF-8 就会在**最需要它的时候**抛 UnicodeEncodeError 中断。
enable_utf8_output()

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


# ==========================================================================
# 极简 Markdown 渲染
# ==========================================================================


def render_markdown(md: str, *, max_chars: int = 40000) -> str:
    """把 Markdown 渲染成 HTML。

    只支持本项目报告实际用到的语法：标题、表格、列表、粗体、行内代码、引用、水平线。
    **刻意不引入 markdown 库** —— 少一个依赖、少一个复现风险，
    而且我们能完全控制输出（报告内容含用户材料，必须转义）。
    """
    if not md:
        return ""
    text = md[:max_chars]
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    in_list = False
    in_quote = False
    in_table = False

    def esc(s: str) -> str:
        return html_mod.escape(s, quote=False)

    def inline(s: str) -> str:
        s = esc(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        # 记号规范化与 `render_prose` 同一套（见 `_MARK_MAP`）。
        # 报告里大量说明文字带 emoji 前饰，收敛后全站记号才是一套。
        # ⚠️ 只换**记号**，一个字不改。
        for bad, good in _MARK_MAP.items():
            s = s.replace(bad, good)
        return s

    # 自由文本另有一个 `prose` 过滤器（见 `render_prose`），同一套「先转义后加工」。

    def close_blocks() -> None:
        nonlocal in_list, in_quote, in_table
        if in_list:
            out.append("</ul>")
            in_list = False
        if in_quote:
            out.append("</blockquote>")
            in_quote = False
        if in_table:
            out.append("</tbody></table>")
            in_table = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 表格
        if stripped.startswith("|") and i + 1 < len(lines) and re.match(
            r"^\|[\s:\-|]+\|$", lines[i + 1].strip()
        ):
            close_blocks()
            header = [c.strip() for c in stripped.strip("|").split("|")]
            out.append("<table><thead><tr>")
            out.extend(f"<th>{inline(c)}</th>" for c in header)
            out.append("</tr></thead><tbody>")
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                out.append("<tr>")
                out.extend(f"<td>{inline(c)}</td>" for c in cells)
                out.append("</tr>")
                i += 1
            out.append("</tbody></table>")
            continue

        if not stripped:
            close_blocks()
            i += 1
            continue

        if stripped.startswith("---"):
            close_blocks()
            out.append("<hr>")
            i += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            close_blocks()
            level = len(m.group(1))
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            i += 1
            continue

        if stripped.startswith(">"):
            if not in_quote:
                close_blocks()
                out.append("<blockquote>")
                in_quote = True
            out.append(f"<p>{inline(stripped.lstrip('> ').strip())}</p>")
            i += 1
            continue

        # ⚠️ 上面判据与下面剥离**必须用同一个模式**。此处原先写
        #   `re.sub(r'^[-*]\\s+', ...)` —— 原生字符串里 `\\s` 是「反斜杠 + s」，
        #   于是模式变成「行首 - 或 *，后跟一个字面反斜杠和若干 s」，
        #   对真正的 `- 条目` 永不匹配。后果是列表项把符号原样带出去：
        #   实测 `<li>- 第一条</li>`，而判据那一行是对的（`r"^[-*]\s+"`），
        #   所以列表能正确识别成 <ul>，只是每一项前面多一个「- 」。
        #   这类错误在**页面上看着像排版小瑕疵**，不像 bug，因此最容易留下。
        if re.match(r"^[-*]\s+", stripped):
            if not in_list:
                close_blocks()
                out.append("<ul>")
                in_list = True
            item = re.sub(r"^[-*]\s+", "", stripped)
            out.append(f"<li>{inline(item)}</li>")
            i += 1
            continue

        close_blocks()
        out.append(f"<p>{inline(stripped)}</p>")
        i += 1

    close_blocks()
    return "\n".join(out)


# 自由文本的记号规范化：把产物里五花八门的装饰字形收敛到**本站唯一一套记号**。
#
# 为什么要做（2026-09-27）：`pipelines/` 生成的说明文字里大量使用彩色 emoji
# 作前缀（`⚠️` / `✅` / `❌` / `🟢` / `🔵`）。它们直接从 `valuation.json`、
# `audit.json` 流到页面，于是同一个「需留意」在有的页面是 `⚠️`、有的是 `◐`，
# 而且 emoji 的字号与基线由字体决定，与相邻中文对不齐。
#
# ⚠️ 只改**记号**，一个字都不改。这些字符串是系统自己的说明（判定理由、
# 假设依据、区间合成方式），不是引用的原文 —— **原文（`location.quote`）
# 绝不走这个过滤器**，那是证据，必须逐字保真。
_MARK_MAP = {
    "⚠️": "◐", "⚠": "◐",      # 需留意 → 疑似
    "✅": "√", "✔️": "√", "✔": "√",   # 通过
    "❌": "×", "✖️": "×", "✖": "×",   # 失败
    "⏭️": "○", "⏭": "○",      # 跳过
    "➖": "○",
    "⏳": "▶",                  # 进行中
    "🟢": "", "🔵": "", "⚙️": "", "📄": "", "📋": "",   # 纯装饰，直接去掉
}


def render_prose(text: Any) -> Markup:
    """把产物里的**自由文本**渲染成可直接输出的 HTML。做两件事：

    ① ``**粗体**`` → ``<strong>``。
       产物里有大量**非 Markdown** 的单行字符串直接进模板 ——
       ``weighting_note`` / ``applicability`` / ``risk_factors`` / ``basis`` /
       ``detail`` / ``reason`` …… 它们自带 Markdown 风格的 ``**``，而模板只是
       ``{{ x }}`` 输出，于是正文里裸着 ``**重点**``（实测渲染出 29 处）。

    ② 装饰字形 → 本站记号（见 ``_MARK_MAP``）。**不改一个字**，只换记号。

    ⚠️ **转义必须在加粗之前**，否则 ``**<script>alert(1)</script>**``
    这类内容会被当成 HTML 执行 —— 这些字符串源头是用户材料与模型输出。

    返回 ``Markup`` 以免 Jinja2 二次转义（那会把 ``<strong>`` 也打成文本）。
    用了本过滤器的变量**不得**再过 ``|e``。
    """
    if text is None:
        return Markup("")
    s = html_mod.escape(str(text), quote=False)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    for bad, good in _MARK_MAP.items():
        s = s.replace(bad, good)
    return Markup(s)


def plain_marks(text: Any) -> str:
    """只做**记号规范化**，不转义、不包装 —— 给客户端 ``textContent`` 写回的场景用。

    ★ 为什么它必须与 :func:`render_marks` 成对存在：
      同一个日志尾部有**两条**渲染路径 —— 服务端先渲一遍（首屏），
      之后每次轮询都由 JS 覆盖。服务端那条走 ``|marks``（转义 + 换记号），
      客户端那条走 ``textContent``（不转义、也就不会换记号）。
      于是日志里的 ``✅`` 会在**第一次轮询之后**变回 emoji ——
      页面看起来正常，只是字形在两秒内悄悄换了一套，没有人会发现。
      把规范化后的文本放进 ``/api/status`` 里，两条路径才印同一批字形。
    """
    if text is None:
        return ""
    s = str(text)
    for bad, good in _MARK_MAP.items():
        s = s.replace(bad, good)
    return s


def render_marks(text: Any) -> Markup:
    """只做「转义 + 记号规范化」，**不加粗**。

    给**控制台日志**这类逐字回放的文本用。它与 :func:`render_prose` 的区别
    只有一条：不把 ``**x**`` 变成 ``<strong>``。日志是流水账，不是文章 ——
    里面的星号（路径通配、乘法、模型输出里的 Markdown）应当原样留着。

    为什么日志也要过这一道：`pipelines/` 往 stdout 打的进度行带 ``✅`` / ``⚠``
    这类彩色 emoji，`web/analyze.py` 逐行 tail 下来、模板 ``{{ line }}`` 直出，
    于是全站唯一一处「不是记号体系」的字形就出现在这一屏。
    与 ``_MARK_MAP`` 上方注释同一条理由：**只换记号，一个字不改**。
    """
    return Markup(html_mod.escape(plain_marks(text), quote=False))


def _as_int(value: Any, default: int = 0) -> int:
    """安全转 int。

    ⚠️ 必需这个辅助函数：产物里的 ``page`` 字段可能来自模型输出，
    因此**可能是 null**（``dict.get("page", 0)`` 对显式的 null 无效，
    它只在键缺失时生效）。直接 ``int()`` 会抛 TypeError 让页面 500 ——
    现场演示最怕这种崩。
    """
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _find_record(records: list[dict[str, Any]], doc_id: str | None,
                 page: int | None) -> dict[str, Any] | None:
    """在通道记录里找 ``(doc_id, page)`` 匹配的那一条。"""
    for rec in records:
        if rec.get("doc_id") == doc_id and _as_int(rec.get("page")) == _as_int(page):
            return rec
    return None


# ==========================================================================
# 应用
# ==========================================================================


def register_filters(env: Any) -> Any:
    """把本项目自定义的**全部** Jinja2 过滤器挂到一个环境上，返回该环境。

    ★ 为什么必须收成一个函数（2026-09-27 改）：``tests/test_web.py``
    的 ``test_all_templates_compile`` 会**自己 new 一个 Environment** 来编译
    全部模板 —— 过滤器就得在那边手动补一遍。原先是在 ``create_app`` 里散着三行
    赋值，加过滤器时极易只改一处。这一天真的发生了：新加的 ``marks`` 只在
    ``create_app`` 里注册，测试环境里没有，``analyze.html`` 编译失败。

    失败模式还分两种，**坏的那种更安静**：模板在页面上跑得好好的、测试却红了
    （能发现），或者测试绿着、模板里其实用了个生产环境不存在的过滤器（发现不了）。
    现在网页与测试共用这一个函数，加过滤器只需要改一处。
    """
    env.filters["md"] = render_markdown
    # ⚠️ 用了 `prose` 的模板变量**不得**再过 `|e`：过滤器的返回值已经是
    #    转义过的 Markup，再转一次会把 <strong> 打成字面文本。
    env.filters["prose"] = render_prose
    # 控制台日志用：只换记号，不加粗（见 render_marks 的 docstring）。
    env.filters["marks"] = render_marks
    return env


# ==========================================================================
# 材料范围 —— 赛题点名的三类材料与三类字段，在本 run 上各跑到哪一步
# ==========================================================================

# 通知 p2 点名的三类关键字段 → `configs/metrics.yaml` 里的 category。
# 只在这里写「赛题用词 → 本项目配置项」这一层映射；字段清单本身从
# `configs/metrics.yaml` 现读，不抄第二遍（抄了就会与配置漂移）。
_SCOPE_FAMILIES: tuple[tuple[str, str, str], ...] = (
    ("质押", "announcement_pledge", "pledge"),
    ("中标", "announcement_contract", "contract"),
    ("股权变动", "announcement_equity", "equity"),
)


_METRICS_CFG: dict[str, Any] | None = None


def _metrics_config() -> dict[str, Any]:
    """读一次 ``configs/metrics.yaml``（进程内缓存）。

    读不到就返回空字典，**不抛** —— 演示台不因为少一份配置就整页 500。
    代价是材料范围表里的字段清单会变空（页面上表现为「定义 0 个」），
    这比白屏好排查：一眼看得出是配置没读到。
    """
    global _METRICS_CFG
    if _METRICS_CFG is None:
        try:
            import yaml

            _METRICS_CFG = yaml.safe_load(
                (PROJECT_ROOT / "configs" / "metrics.yaml").read_text(encoding="utf-8")
            ) or {}
        except Exception:  # noqa: BLE001
            _METRICS_CFG = {}
    return _METRICS_CFG


_ROUTING_CFG: dict[str, Any] | None = None


def _routing_config() -> dict[str, Any]:
    """读一次 ``configs/routing.yaml``（进程内缓存）。读不到返回空字典，不抛。"""
    global _ROUTING_CFG
    if _ROUTING_CFG is None:
        try:
            import yaml

            _ROUTING_CFG = yaml.safe_load(
                (PROJECT_ROOT / "configs" / "routing.yaml").read_text(encoding="utf-8")
            ) or {}
        except Exception:  # noqa: BLE001
            _ROUTING_CFG = {}
    return _ROUTING_CFG


def vlm_budget(r: RunArtifacts) -> dict[str, Any]:
    """视觉通道的调用预算用掉了多少，以及**有没有用尽**（2026-09-27 加）。

    ★ 为什么这一条必须现算、且必须显示：``configs/routing.yaml`` 的
      ``vlm_trigger.max_calls_per_run`` 是成本闸门。闸门用尽后，
      ``pipelines/r1_extract/judge.py`` 会 ``break`` 掉后面**所有**表格的
      单元格裁决 —— 而那个 ``break`` **不留任何记录**：trace 里没有 error、
      日志里没有一行字、``build_stats`` 里也没有计数器。实测 ann77：
      84 次整页扫描 + 316 格 = 正好 400（= 上限），最后一张表预算 4 格、
      只裁决了 1 格就被 ``min(budget, max_calls - calls)`` 钳住，再往后的
      表全部静默跳过。

      所以「字段抽全了没有」的答案取决于闸门有没有用尽。页面上不写这一句，
      「逐格复核 316 格」就会被读成「该查的都查了」。

    已用调用数从产物现算，口径与 ``judge.py`` 里 ``calls`` 的累加一致：
    整页扫描条数 + 各表 ``cells_checked - errors``（失败格不计入预算）。
    上限读 ``configs/routing.yaml``；两份都读不到就返回 ``cap=0``，
    页面按「无法判定」处理，而不是默认「没超」。
    """
    trig = (_routing_config().get("vlm_trigger") or {})
    cap = int(trig.get("max_calls_per_run", 0) or 0)

    scans = r._read_json("channels/vlm_scans.json") or []
    tables = r._read_json("channels/table_adjudications.json") or []
    cells = 0
    for t in tables:
        rep = t.get("report") or {}
        cells += max(0, int(rep.get("cells_checked", 0) or 0)
                     - int(rep.get("errors", 0) or 0))
    used = len(scans) + cells

    return {
        "cap": cap,
        "used": used,
        "page_scans": len(scans),
        "cells": cells,
        "tables_adjudicated": len(tables),
        "exhausted": bool(cap) and used >= cap,
    }


def material_scope(r: RunArtifacts) -> dict[str, Any]:
    """赛题点名的三类材料 + 三类字段，在本 run 上各跑到哪一步。

    ★ **每个数字都从产物现算**（2026-09-27 重写）。这一段原先是一张写死的
      对照表，写着「非标准化公告：本轮未跑」之类的结论。演示 run 换成公告
      语料之后，那四行**会继续那么写** —— 页面在说反话，而且没有任何测试
      会红。写死的结论活不过一次换 run，所以现在全部现算：

        * 材料/页数 —— ``manifest.json`` 的 ``input_files`` 与 ``pages/index.json``
        * 三类材料 —— ``pages/index.json`` 每页的 ``route``
          （``B_scanned`` = 无可靠文本层的扫描件；``D_table``/``E_mixed`` = 表格页）
        * 三类字段 —— ``facts.json`` 的 ``metric`` 按 ``configs/metrics.yaml``
          的 ``category`` 归类后的条数

    这样换任何 run，这张表都跟着变，不可能再说反话。
    """
    cfg = _metrics_config()
    fam_of: dict[str, str] = {}
    for md in (cfg.get("metrics") or []):
        cat = md.get("category")
        if cat:
            fam_of[md.get("canonical", "")] = cat

    pages = r.pages
    routes: dict[str, int] = {}
    for p in pages:
        rt = p.get("route") or "?"
        routes[rt] = routes.get(rt, 0) + 1

    facts = r.build_facts()
    by_family: dict[str, int] = {key: 0 for _lbl, _cat, key in _SCOPE_FAMILIES}
    for f in facts:
        fam = fam_of.get(f.metric)
        for _lbl, cat, key in _SCOPE_FAMILIES:
            if fam == cat:
                by_family[key] += 1

    docs = r.documents
    n_docs = len(docs)
    n_pages = len(pages)
    scanned = routes.get("B_scanned", 0) + routes.get("C_garbled", 0)
    with_text = routes.get("A_text_only", 0)
    # ★ 「表格页」只算 `D_table`，混合页单列（2026-09-27 修）。
    #   原先这里写 `D_table + E_mixed`，于是 /facts 印「232 页表格页」，
    #   而 /library 的卡片印「表格页 158」—— 同一个中文标签、两个数字。
    #   两者都"有依据"（混合页确实含表格），但读者只会认为有一个是错的。
    #   现在：/facts 把四项**分开列全**（585 + 106 + 158 + 74 = 923），
    #   `table_pages` 与 /library 卡片同口径，混合页用 `mixed_pages`。
    table_pages = routes.get("D_table", 0)
    mixed_pages = routes.get("E_mixed", 0)

    fam_rows = []
    for label, cat, key in _SCOPE_FAMILIES:
        fields = [m for m in (cfg.get("metrics") or []) if m.get("category") == cat]
        fam_rows.append({
            "label": label,
            "category": cat,
            "n_fields": len(fields),
            "field_names": " / ".join(m.get("label", "") for m in fields),
            "n_facts": by_family[key],
        })

    # ★ 「份数」必须同时给出**不同文档数**（2026-09-27 加）。`doc_id` 按内容哈希
    #   生成，语料里有内容完全相同的重复文件（同一份公告抓了两次），77 个文件
    #   只对应 62 个不同文档 —— 只报 77 会被读成「77 份不同的材料」。
    #   与 /library 同源同口径，两页不能各说各话。
    n_uniq_docs = len({d.get("doc_id") for d in docs})

    # ★ 有多少份材料**至少抽到一条事实**（2026-09-27 加）。这个数不写，
    #   读者会把「命中 107 条」当成「材料都很能抽」，而实测是 47/77 份
    #   （= 36/62 份不同文档）有命中 —— 覆盖率与命中条数是两件事。
    #   `FactView` 没有 doc_id，材料归属在证据锚点上（`evidence[].doc_id`）——
    #   一条事实的全部证据都指向同一份材料，取并集即可。
    docs_with_facts = {e.doc_id for f in facts for e in f.evidence}
    n_docs_hit = sum(1 for d in docs if d.get("doc_id") in docs_with_facts)

    return {
        "data_dir": (r.summary.data_dir if r.summary else "") or "",
        "n_docs": n_docs,
        "n_uniq_docs": n_uniq_docs,
        "n_dup_extra": n_docs - n_uniq_docs,
        "n_docs_hit": n_docs_hit,
        "n_uniq_docs_hit": len(docs_with_facts),
        "n_pages": n_pages,
        "with_text": with_text,
        "scanned": scanned,
        "table_pages": table_pages,
        "mixed_pages": mixed_pages,
        "routes": routes,
        "families": fam_rows,
        "n_family_facts": sum(by_family.values()),
        "ocr_pages": r.channel_stats().get("ocr_pages", 0),
        # 视觉通道的成本闸门用没用尽 —— 见 vlm_budget 的 docstring。
        # 这一项决定上面那些命中条数该被读成「抽到的」还是「抽到的下限」。
        "vlm": vlm_budget(r),
    }


# ==========================================================================
# 运行轨迹实时流 —— 右侧调试抽屉的数据源
#
# 这一节的全部复杂度都来自一件事：**要轮询一个正在被写的文件**。
# `web/analyze.py::_live_llm_stats` 已经为同一件事踩过坑，这里的两个决定
# 与它保持一致，不要各写一套：
#   1. 游标是**字节偏移**，不是 `seq` —— `Tracer._seq` 在新进程里从 0 重开，
#      而 `analyze.start()` 是把 `agent run full` 当子进程起的，一次 `--resume`
#      就会产生第二段重叠的 seq，按 seq 取会静默漏行或重复行。
#   2. 只消费到**最后一个完整换行** —— 末尾半行留到下次读。
# ==========================================================================

_ALL_TRACE_KINDS = (
    "llm_call", "tool_call", "file_access", "assertion", "routing", "stage", "error",
)

#: 断言三态的取值 —— 从追踪层导入而不是本地重写一份（取值集合是它们的契约）。
from agent.trace.tracer import ASSERT_STATUSES as _ASSERT_STATUSES  # noqa: E402

#: 逐 run 的**增量**统计缓存：run_id → {offset, by_kind, errors, cost, tokens}
#: 只解析 trace 新增的字节，所以 2 秒一轮的抽屉轮询不会把磁盘读爆。
_TRACE_SCAN: dict[str, dict[str, Any]] = {}


def _zero_stats() -> dict[str, Any]:
    return {
        "total_events": 0,
        "by_kind": {k: 0 for k in _ALL_TRACE_KINDS},
        "errors": 0,
        "assertions_passed": 0, "assertions_failed": 0, "assertions_skipped": 0,
        "cost_cny": 0.0, "tokens": 0, "llm_calls": 0, "cache_hits": 0,
    }


def _split_complete(body: bytes, base: int) -> list[tuple[int, bytes]]:
    """把**只含完整行**的字节切成 ``[(该行起始偏移, 该行), …]``（跳过空行）。"""
    out: list[tuple[int, bytes]] = []
    pos = base
    for line in body.split(b"\n"):
        if line.strip():
            out.append((pos, line))
        pos += len(line) + 1
    return out


def _tail_window(fh: Any, size: int, limit: int,
                 keep: Any = None) -> tuple[int, int, bytes]:
    """从文件尾部倒着取，直到凑够 ``limit`` 个**合格**的完整行。

    返回 ``(首个完整行的偏移, 末尾偏移, 只含完整行的字节)``。
    ``keep`` 是一个 ``bytes -> bool`` 谓词（按 kind 过滤时用）；给了它，
    窗口就按**合格行数**而不是总行数收口 —— 否则筛 ``tool_call`` 时
    末尾恰好全是 ``stage``，用户只会看到一张空表。

    窗口按 64 KB 起、不够翻倍 —— 行长不可预期（``vlm_scan_page`` 的事件
    带整段 reason），写死窗口会在大行上取不满。
    """
    window = 64 * 1024
    while True:
        start = max(0, size - window)
        fh.seek(start)
        chunk = fh.read()
        if start > 0:
            # 开头多半是被窗口切断的半行 —— 丢掉，位置往后挪。
            nl = chunk.find(b"\n")
            if nl < 0:
                first, body = size, b""
            else:
                first, body = start + nl + 1, chunk[nl + 1:]
        else:
            first, body = 0, chunk
        if not body.endswith(b"\n"):
            # 末尾没有换行 ⇒ 最后一行还在写，整行丢弃。
            nl = body.rfind(b"\n")
            body = body[: nl + 1] if nl >= 0 else b""
        items = _split_complete(body, first)
        if keep is not None:
            items = [it for it in items if keep(it[1])]
        if first == 0 or len(items) >= limit or window >= size:
            return first, first + len(body), body
        window *= 2


def _trace_stats(run_id: str, path: Path, size: int) -> dict[str, Any]:
    """整份 trace 的分类计数与用量 —— 抽屉的筛选 chip 靠它显示每类条数。

    增量扫描（只读新增字节），所以它可以每轮都调。
    """
    st = _TRACE_SCAN.setdefault(
        run_id,
        {"offset": 0, "by_kind": {}, "errors": 0, "asserts": {},
         "cost": 0.0, "tokens": 0, "hits": 0},
    )
    if size < st["offset"]:                      # 文件被截断/重建 → 从头再来
        st.update(offset=0, by_kind={}, errors=0, asserts={},
                  cost=0.0, tokens=0, hits=0)
    blob = b""
    if size > st["offset"]:
        try:
            with path.open("rb") as fh:
                fh.seek(st["offset"])
                blob = fh.read()
        except OSError:                          # pragma: no cover
            blob = b""
    cut = blob.rfind(b"\n")
    if cut >= 0:
        st["offset"] += cut + 1
        for raw in blob[: cut + 1].splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                e = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(e, dict):
                continue
            kind = str(e.get("kind") or "?")
            st["by_kind"][kind] = st["by_kind"].get(kind, 0) + 1
            # ★ 「错误」只数 `kind == "error"` —— 与 `summarize_trace` 同一口径。
            #   不能数 `ok is False`：断言的 False 是 `skipped`（缺辅助数据、
            #   本次未校验），stage 的 False 是 `start`。把这两类算成错误，
            #   会让一次正常运行在抽屉里显示成「错误 26」。
            if kind == "error":
                st["errors"] += 1
            elif kind == "assertion":
                # 三态口径同 `agent/trace/tracer.py::assertion_status`：
                # 新事件带 extra.status；早期两态 trace 只有 ok，读回一律当 fail。
                raw = (e.get("extra") or {}).get("status")
                status = raw if raw in _ASSERT_STATUSES else (
                    "pass" if e.get("ok") else "fail")
                st["asserts"][status] = st["asserts"].get(status, 0) + 1
            st["cost"] += float(e.get("cost_cny") or 0.0)
            st["tokens"] += int(e.get("prompt_tokens") or 0) + int(
                e.get("completion_tokens") or 0
            )
            if e.get("cache_hit"):
                st["hits"] += 1

    by_kind = {k: int(st["by_kind"].get(k, 0)) for k in _ALL_TRACE_KINDS}
    asserts = st["asserts"]
    return {
        "total_events": sum(int(v) for v in st["by_kind"].values()),
        "by_kind": by_kind,
        "errors": int(st["errors"]),
        "assertions_passed": int(asserts.get("pass", 0)),
        "assertions_failed": int(asserts.get("fail", 0)),
        "assertions_skipped": int(asserts.get("skipped", 0)),
        "cost_cny": round(float(st["cost"]), 4),
        "tokens": int(st["tokens"]),
        "llm_calls": by_kind["llm_call"],
        "cache_hits": int(st["hits"]),
    }


def _tail_trace(run_dir: Path, *, offset: int = 0, limit: int = 200,
                kinds: set[str] | None = None) -> dict[str, Any]:
    """读 ``runs/<id>/trace.jsonl`` 的一段，供抽屉轮询。

    ``offset < 0`` 是**尾部哨兵**：取最后 ``limit`` 条，并把首行的字节偏移
    回给客户端（打开抽屉时先看最近发生了什么，不必从 seq 0 读起）。
    任何异常都变成 ``error`` 字符串 + 空 rows —— 这一层**绝不抛**，
    因为抽屉挂在每一页上，它一炸就是全站白屏。
    """
    limit = max(1, min(int(limit), 1000))
    path = run_dir / "trace.jsonl"
    if not path.is_file():
        return {"offset": 0, "next_offset": 0, "eof": True, "rows": [],
                "stats": _zero_stats(), "error": "该 run 没有 trace.jsonl"}
    try:
        size = path.stat().st_size
    except OSError as exc:                       # pragma: no cover
        return {"offset": 0, "next_offset": 0, "eof": True, "rows": [],
                "stats": _zero_stats(), "error": f"{type(exc).__name__}: {exc}"}

    def _keep(raw: bytes) -> bool:
        if not kinds:
            return True
        try:
            return str(json.loads(raw).get("kind")) in kinds
        except (json.JSONDecodeError, AttributeError):
            return False

    stats = _trace_stats(run_dir.name, path, size)
    start = end = max(0, offset)
    rows: list[dict[str, Any]] = []
    try:
        if offset < 0:                           # 尾部哨兵
            with path.open("rb") as fh:
                first, end, body = _tail_window(fh, size, limit, _keep)
            items = [it for it in _split_complete(body, first) if _keep(it[1])]
            items = items[-limit:]
            start = items[0][0] if items else end
            lines = [ln for _, ln in items]
        else:
            if offset > size:                    # 文件比游标短 ⇒ 被重建过，从头流
                offset = start = 0
            with path.open("rb") as fh:
                fh.seek(offset)
                blob = fh.read()
            cut = blob.rfind(b"\n")
            if cut < 0:
                lines = []                       # 只有半行 —— 下一轮再来
            else:
                items = [it for it in _split_complete(blob[: cut + 1], offset)
                         if _keep(it[1])][:limit]
                # ★ 一段里一条合格行都没有时，游标也必须前进 —— 否则客户端
                #   会拿同一个 offset 反复要同一段，永远卡死。
                end = (items[-1][0] + len(items[-1][1]) + 1) if items \
                    else offset + cut + 1
                lines = [ln for _, ln in items]
    except OSError as exc:                       # pragma: no cover
        return {"offset": start, "next_offset": end, "eof": True, "rows": [],
                "stats": stats, "error": f"{type(exc).__name__}: {exc}"}

    for raw in lines:
        try:
            e = json.loads(raw)
        except json.JSONDecodeError:             # pragma: no cover
            continue
        if isinstance(e, dict):
            rows.append(e)

    return {"offset": start, "next_offset": end, "eof": end >= size,
            "rows": rows, "stats": stats, "error": None}


# ==========================================================================
# 页内子视图标签条
#
# 5 组选题视图（总览 / 运行 / R1 / R4 / R5）由**导航分组 + 页内标签**构成，
# 路径一个都不新增 —— 见 `tests/test_web.py` 那张 9 路径表。
# ==========================================================================

#: 分组 → ((路径, 标签, tab 值), …)。`tab` 为 None 表示"该路径的默认面板"。
#: R4 三个标签共用 `/valuation`，靠 `?tab=` 区分；R5 同理。
_TAB_GROUPS: tuple[tuple[str, tuple[tuple[str, str, str | None], ...]], ...] = (
    ("run", (("/analyze", "全流程", None),
             ("/process", "单文件解析", None),
             ("/runs", "复现", None))),
    ("r1", (("/library", "材料", None),
            ("/extract", "解析", None),
            ("/facts", "事实库", None))),
    ("r4", (("/valuation", "假设", "assumptions"),
            ("/valuation", "结果", None),
            ("/valuation", "报告", "report"))),
    ("r5", (("/audit", "清单", None),
            ("/audit", "批注稿", "draft"))),
)


def _tabs_for(path: str, tab: str | None, run_id: str | None) -> list[dict[str, Any]]:
    """当前页所在分组的标签条。

    ★ run 取**当前页生效的那个**，不是分组的默认 run：组内切标签时你要继续看
    同一份数据。跨组才需要换 run —— 那是导航的事，理由见 `base.html` 里的注释。
    """
    for _key, members in _TAB_GROUPS:
        if not any(m_path == path for m_path, _l, _t in members):
            continue
        # 认不出的 `?tab=`（手敲的、旧书签的）会渲染默认那支正文，于是
        # 「一个都没高亮」——正文写着结果、标签条却全是灰的，是个小谎。
        # 这里把认不出的一律当默认：与 `GET /valuation`、`GET /audit` 里
        # `view = tab if tab in (...) else None` 的口径对齐。
        known = {m_tab for m_path, _l, m_tab in members if m_tab and m_path == path}
        want = tab if (tab in known or not tab) else None
        out: list[dict[str, Any]] = []
        for m_path, label, m_tab in members:
            query: dict[str, str] = {}
            if run_id:
                query["run"] = run_id
            if m_tab:
                query["tab"] = m_tab
            out.append({
                "label": label,
                "href": m_path + (("?" + urlencode(query)) if query else ""),
                "active": m_path == path and (m_tab or None) == (want or None),
            })
        return out
    return []


def create_app(*, default_run: str | None = None,
               runs_root: str | Path | None = None,
               r1_run: str | None = None) -> FastAPI:
    """``r1_run`` —— 选题 1 各页的**独立默认 run**（2026-09-27 加）。

    起因是赛题**点名的材料类型**与主 run 的材料不重合：通知 p2 的选题 1 原话是
    「从**非标准化公告、扫描文件和复杂表格**中提取**质押、中标、股权变动**等关键
    字段」，而主 run（``web-*``，601857 年报）的输入是一份**定期报告** ——
    那恰好是通知里**选题 2**（上市公司财务报告分析）的材料。把年报当作选题 1 的
    交付物，等于拿选题 2 的材料去回答选题 1。

    而选题 4/5 反过来**必须**吃定期报告的财务数据（通知 p3：「根据财务数据、经营
    假设和可比公司信息」「依据原始财务材料」），喂公告反而做不出来。

    所以两个 run 各有各的题，不是一个 run 通吃。给选题 1 单独一个默认 run，
    比在页面上写一段「其实材料不对」的说明要诚实 —— 说明只能承认偏差，
    换 run 是消除偏差。``None`` 时回落到 ``default_run``，行为与从前完全一致。
    """
    app = FastAPI(title="金融投研智能体 — 演示台", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    register_filters(templates.env)
    # 暴露给测试：``test_all_templates_compile`` 用**应用真正在用的那个环境**
    # 编译模板，而不是自己另搭一个 —— 否则「测试里能编译」与「页面上能渲染」
    # 是两件事（见 register_filters 的 docstring）。
    app.state.jinja_env = templates.env

    root = Path(runs_root) if runs_root else DEFAULT_RUNS_ROOT
    STATE: dict[str, Any] = {"default_run": default_run, "runs_root": root,
                             "r1_run": r1_run}

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ---------------------------------------------------------------- 工具

    def _run_or_error(run_id: str | None,
                      topic: str | None = None) -> tuple[RunArtifacts | None, str | None]:
        """``topic="r1"`` 的页面（``/facts``、``/extract``）优先用 ``r1_run``。

        ★ 只在**调用方没显式给 run_id** 时生效 —— 顶部运行选择器、导航里的
        ``?run=`` 一律照传，否则选择器会在选题 1 的页面上失灵（选了没反应），
        那比默认值不对更难查。
        """
        if run_id is None and topic == "r1":
            run_id = STATE.get("r1_run")
        try:
            return load_run(run_id or STATE["default_run"], STATE["runs_root"]), None
        except ArtifactError as exc:
            return None, str(exc)

    def _ctx(request: Request, run: RunArtifacts | None, **extra: Any) -> dict[str, Any]:
        return {
            "request": request,
            "run": run,
            "runs": available_runs(STATE["runs_root"]),
            "run_id": run.run_id if run else None,
            # 导航里选题 1 那一组用它，而不是用 run_id —— 否则在选题 4 的页面上
            # 点「事实库溯源」会带着估值的 run 过去，而那多半没有 facts/。
            "r1_run_id": STATE.get("r1_run") or (run.run_id if run else None),
            # 反向的同一个坑：选题 4/5 那一组**不能**用 run_id。
            # run_id 是**本页解析出来的** run，在选题 1 的页面（/facts、/extract）
            # 上它就是 r1_run —— 于是从 /facts 点「估值建模」会带着公告 run 过去，
            # 而公告 run 没有 valuation/。这里要的是「/valuation 默认读哪个 run」，
            # 那就是 default_run，它不随当前页漂移。
            # 不给 `run.run_id` 兜底：兜底就等于把上面那个漂移又请回来。
            "fin_run_id": STATE.get("default_run"),
            "summary": run.summary if run else None,
            "current_path": request.url.path,
            # 标签条在这里统一注入 —— 于是**所有既有路由零改动**就都有了它。
            "tabs": _tabs_for(request.url.path, request.query_params.get("tab"),
                              run.run_id if run else None),
            **extra,
        }

    # ---------------------------------------------------------------- 首页

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, run: str | None = None) -> Any:
        """总览首页 —— 一屏说清「本次参赛做了哪三题、各自结果如何」。

        刻意**不在这一页堆明细**：它是答辩时的第一屏，只回答
        「做了什么、做得怎么样、点进去看哪里」。
        """
        r, err = _run_or_error(run)
        if r is None:
            return templates.TemplateResponse(
                request, "empty.html", _ctx(request, None, message=err)
            )
        # ★ 选题 1 的数字来自 r1_run（公告语料），选题 4/5 的来自主 run（年报）。
        #   两个 run 各答各的题，见 ``create_app`` 的 docstring。r1_run 未配置
        #   或读不到时回落到 r —— 首页因此永远不会因为少配一个 run 而空白。
        r1, _r1_err = _run_or_error(None, topic="r1")
        if r1 is None:
            r1 = r

        facts = r1.build_facts()
        det = sum(1 for f in facts if f.extractor_deterministic)
        with_evidence = sum(1 for f in facts if f.evidence)
        val = r.valuation() or {}
        audit = r.audit() or {}
        findings = audit.get("findings") or []
        metrics = audit.get("metrics") or {}
        confirmed = sum(1 for f in findings if f.get("severity") == "confirmed")
        docs = r1.documents

        # 卡片上的「产物」行从**本次运行的产物清单**里过滤候选文件名得到，
        # 不是写死的常量 —— 某个 run 没产出 `revision_table.xlsx`（R5 没跑或没改过
        # 草稿）时它就自然消失，而不是在页面上印一个不存在的文件名。
        out_paths = {o["path"] for o in r.outputs()}
        r1_paths = {o["path"] for o in r1.outputs()}

        def _pick(pool: set[str], *names: str) -> str:
            return " · ".join(n.split("/")[-1] for n in names if n in pool)

        # 各题的「一句话结论」都在这里成形，模板只负责摆位。
        #
        # ★ 每张卡片的 stats **固定 4 条**（2026-09-27）。这不是审美偏好，是版面约束：
        #   模板把那 4 条摆成 2×2 网格，三张卡因此有**完全相同的行结构**，四个数字
        #   横向对齐。原先用 flex-wrap，卡片里数值宽度不同 → 换行点不同：实测
        #   「可比公司 6」单独掉到第三行，左边空一大片，而相邻卡片的同一行还停在
        #   第二行。数字对不齐时，读者会以为它们是两类东西。
        #   要加第 5 条，请先想清楚新版面 —— 别默默塞进去把网格挤成 2+2+1。
        topics = [
            {
                "topic": "选题 1",
                "step": "①",
                "name": "读懂文档 · 数据结构化提取",
                "href": "/facts",
                "cta": "打开事实清单",
                # ★ 材料类型照抄通知 p2 点名的三类，不写「年报」——
                #   年报是通知里**选题 2** 的材料（p3「从年度报告、半年度报告和
                #   季度报告等材料中提取相关财务指标」）。写「年报」，评委一眼就
                #   看出这题答的是别题的材料。
                "desc": "非标准化公告 · 扫描件 · 复杂表格 → 规定格式的结构化数据",
                # 一句话答案：数字全在下面的 2×2 网格里，但那四个格子是并列的读数，
                # 读者得自己拼出结论。这一行直接把结论写出来。
                # 「全部带原文定位」只有在真的全部都有时才这么写 —— 有缺口就报分数，
                # 否则这一行会变成一句页面自己保证不了的话。
                "answer": (f"{len(r1.pages)} 页材料 → {len(facts)} 条数字，"
                           + ("全部带原文定位" if with_evidence == len(facts)
                              else f"{with_evidence} 条带原文定位")
                           if facts else "本 run 未产出事实库"),
                "stats": [
                    {"k": "事实条目", "v": f"{len(facts)}"},
                    {"k": "确定性抽取占比",
                     "v": f"{det / len(facts) * 100:.0f}%" if facts else "—"},
                    {"k": "覆盖文档", "v": f"{len(docs)}"},
                    {"k": "待人工复核",
                     "v": f"{sum(1 for f in facts if f.needs_review)}"},
                ],
                "artifacts": _pick(r1_paths, "facts/facts.json", "facts/facts.xlsx",
                                   "facts/evidence.html"),
                "ready": bool(facts),
                "run_id": r1.run_id,
            },
            {
                "topic": "选题 4",
                "step": "②",
                "name": "算估值 · 自动化估值建模",
                "href": "/valuation",
                "cta": "打开估值结果",
                "desc": "事实库 → DCF + 可比公司法 → 每股价值区间与敏感性分析",
                "answer": (f"每股价值 {val.get('value_range_low')} – "
                           f"{val.get('value_range_high')} 元"
                           f"（中值 {val.get('value_range_mid')}）"
                           if val.get("value_range_low") else "本 run 未产出估值产物"),
                "stats": [
                    {"k": "目标实体", "v": val.get("entity") or "—"},
                    {"k": "每股价值区间",
                     "v": (f"{val.get('value_range_low')} – {val.get('value_range_high')}"
                           if val.get("value_range_low") else "—")},
                    {"k": "敏感性假设",
                     "v": f"{len((val.get('sensitivity') or {}).get('tornado') or [])}"},
                    {"k": "可比公司",
                     "v": f"{len((val.get('comps') or {}).get('peers') or [])}"},
                ],
                "artifacts": _pick(out_paths, "valuation/valuation_model.xlsx",
                                   "valuation/valuation_report.md", "valuation/valuation.json"),
                "ready": bool(val),
                "run_id": r.run_id,
            },
            {
                "topic": "选题 5",
                "step": "③",
                "name": "查错 · 研究报告纠错核查",
                "href": "/audit",
                "cta": "打开问题清单",
                "desc": "研究报告草稿 → 逐条独立重算比对 → 问题清单与批注稿",
                "answer": (f"从草稿里检出 {len(findings)} 处问题，{confirmed} 处已确认"
                           if audit else "本 run 未产出核查产物"),
                "stats": [
                    {"k": "检出问题", "v": f"{len(findings)}" if audit else "—"},
                    {"k": "已确认", "v": f"{confirmed}" if audit else "—"},
                    {"k": "精确率 P",
                     "v": f"{metrics['precision']:.2f}" if metrics.get("precision") is not None else "—"},
                    {"k": "F1",
                     "v": f"{metrics['f1']:.4f}" if metrics.get("f1") is not None else "—"},
                ],
                "artifacts": _pick(out_paths, "audit/audit_report.md",
                                   "audit/annotated_draft.html", "audit/revision_table.xlsx"),
                "ready": bool(audit),
                "run_id": r.run_id,
                # 指标算不出来时页面要**主动说明原因**，而不是留个「—」让人猜。
                "note": ("指标需 golden 标注集才能算；本 run 未挂标注，"
                         "可在顶部切换到 r5-eval-annual16 查看。"
                         if audit and not metrics else ""),
            },
        ]

        return templates.TemplateResponse(
            request,
            "overview.html",
            _ctx(
                request, r,
                topics=topics,
                charts=CD.facts_charts(facts),
                # ★ 首页只放「页面路由构成」，**不放** `charts.layer_mix`：本 run 上
                #   两者是同一个划分（L1 文本层直读 = 确定性路径 = 295，L3 VLM =
                #   模型路径 = 61），并排摆就是把 83%/17% 在同一屏里说两遍。
                #   路由图讲的是另一件事 —— 273 页各被分派给哪条通道（A 文本层 /
                #   D 表格页 / E 混合页），首页原先没有这个信息。
                #   ⚠️ 这只是**本 run 的巧合**，不是恒等式：L2 OCR 与 L4 人工复核
                #   都属于「模型路径」但不在 L1/L3 里，真实年报上两张图会分叉。
                #   所以取不到路由产物时仍旧退回 layer_mix，不留空。
                route_chart=CD.extract_charts(r).get("route_donut"),
                # 页尾那个折叠摘要行要自带结论（`details.ev` 的约定：折起来丢的是
                # 图，不是信息），所以这里把确定性占比算好递给模板。
                det_pct=f"{det / len(facts) * 100:.0f}%" if facts else "—",
                n_channels=len(r.channel_stats() or {}),
                n_pages=len(r.pages),
            ),
        )

    # ---------------------------------------------------------------- 文档库

    @app.get("/library", response_class=HTMLResponse)
    def library(request: Request, run: str | None = None) -> Any:
        # `topic="r1"`：材料清单属于选题 1，与 `/extract`、`/facts` 同一批材料。
        # 原先它没有这个 topic，于是三页里只有它会默认落到主 run（年报）上，
        # 而那一份没有公告语料的解析产物 —— 标签条切过去只会看到空页。
        r, err = _run_or_error(run, topic="r1")
        if r is None:
            return templates.TemplateResponse(request, "empty.html", _ctx(request, None, message=err))

        docs = r.documents
        route_totals: dict[str, int] = {}
        for d in docs:
            for route in (d.get("page_routes") or {}).values():
                route_totals[route] = route_totals.get(route, 0) + 1

        # ★ 有多少份材料其实是**同一份文档**（2026-09-27 加）。
        #   `doc_id` 是按**内容哈希**生成的，而语料里有 15 对文件内容完全相同
        #   （同一份公告抓了两次，只是文件名里的 h 序号不同，例如
        #   `质押_h14_605368.pdf` 与 `质押_h16_605368.pdf` 的 sha256 一致）。
        #   于是 77 份文件只对应 62 个不同的文档。这件事必须显示出来：
        #   否则「77 份」会被读成「77 份不同的材料」。
        dup_count: dict[str, int] = {}
        for d in docs:
            dup_count[d["doc_id"]] = dup_count.get(d["doc_id"], 0) + 1

        per_doc = []
        for d in docs:
            # ★ 必须与上方「表格页」卡片同源（2026-09-27 修）。
            #   此处原先写 `r.pages_of(d["doc_id"])`，而 `pages/index.json` 正是按
            #   内容哈希 doc_id 索引的 —— 上面那 15 对重复文件的 doc_id 相同，
            #   `pages_of` 会把两份的页一起返回：77 份材料这一列横向求和得 **270**，
            #   而卡片按每份自己的路由数是 **158**，同一个中文标签、同一个页面，
            #   两个互相矛盾的数字（`tests/test_web.py` 的真实 run 用例抓到的就是它；
            #   合成夹具的 D_table 恰为 0，等式退化成 0 == 0，抓不到）。
            #   改为直接数这份自己的 `page_routes`，横向求和恒等于卡片。
            routes: dict[str, int] = {}
            for route in (d.get("page_routes") or {}).values():
                routes[route] = routes.get(route, 0) + 1
            per_doc.append(
                {
                    "doc_id": d["doc_id"],
                    "filename": d.get("filename", ""),
                    "rel_path": d.get("rel_path", ""),
                    "pages": d.get("page_count", 0),
                    "has_text_layer": d.get("has_text_layer"),
                    "period": d.get("document_period", "unknown"),
                    "size_mb": d.get("size_mb", 0),
                    "sha256": d.get("sha256", "")[:16],
                    "routes": routes,
                    # ★ 必须与上方「表格页」卡片同源。此处原先写
                    #   `if p.get("lines", 0) >= 40: tables += 1`，而 pages/index.json
                    #   **根本没有 lines 键**（真键是 rule_line_density），
                    #   于是每份材料的这一列恒为 0，同页卡片却显示 101 ——
                    #   同一个中文标签、同一个页面，两个互相矛盾的数字。
                    #   不从 rule_line_density 补算：那个字段只是数值上恰好也得 101，
                    #   定义（规则线段密度）与「表格页」并不等价，早晚漂移。
                    #   直接数路由结果，保证本列横向求和 == 上方卡片。
                    "table_pages": routes.get("D_table", 0),
                    "scanned_pages": routes.get("B_scanned", 0) + routes.get("C_garbled", 0),
                    # 同一个 doc_id 出现几次 —— >1 表示这份内容在语料里有多份副本
                    "copies": dup_count.get(d["doc_id"], 1),
                }
            )

        return templates.TemplateResponse(
            request,
            "library.html",
            _ctx(request, r, docs=per_doc, route_totals=route_totals, stats=r.channel_stats(),
                 # ★ 不同文档数必须在这里算，不能交给模板（2026-09-27）。
                 #   `copies` 是**逐行**的：一对重复的两行都报 copies=2，若在模板里
                 #   求和 (copies − 1) 会得到 30 而不是 15（= Σ over doc_id of
                 #   count² − N），把不同文档数算成 47 而真值是 62。实测踩到过。
                 n_uniq_docs=len(dup_count),
                 n_dup_extra=len(docs) - len(dup_count)),
        )

    # ---------------------------------------------------------------- 抽取工作台

    @app.get("/extract", response_class=HTMLResponse)
    def extract(request: Request, run: str | None = None, doc: str | None = None,
                page: int | None = None) -> Any:
        r, err = _run_or_error(run, topic="r1")
        if r is None:
            return templates.TemplateResponse(request, "empty.html", _ctx(request, None, message=err))

        docs = r.documents
        cur_doc = doc or (docs[0]["doc_id"] if docs else None)
        pages = r.pages_of(cur_doc) if cur_doc else []
        cur_page = page or (_as_int(pages[0].get("page")) if pages else None)

        text_rec = _find_record(r.text_layer, cur_doc, cur_page)
        ocr_rec = _find_record(r.ocr_channel, cur_doc, cur_page)
        scan_rec = _find_record(r.vlm_scans, cur_doc, cur_page)
        conflict_rec = _find_record(r.conflicts, cur_doc, cur_page)
        page_meta = next(
            (p for p in pages if _as_int(p.get("page")) == _as_int(cur_page)), None
        )

        # 表格：结构 + 单元格级裁决。裁决前后都要能看到，
        # 否则"改了什么"无从核对（这是本页最有说服力的证据）。
        page_tables = r.table_views(cur_doc, cur_page) if cur_doc and cur_page else []

        return templates.TemplateResponse(
            request,
            "extract.html",
            _ctx(
                request, r,
                docs=docs, cur_doc=cur_doc, pages=pages, cur_page=cur_page,
                page_meta=page_meta,
                text_rec=text_rec, ocr_rec=ocr_rec, scan_rec=scan_rec,
                conflict_rec=conflict_rec,
                page_tables=page_tables,
                table_stats=r.table_stats(),
                stats=r.channel_stats(),
                scope=material_scope(r),
                charts=CD.extract_charts(r),
            ),
        )

    # ---------------------------------------------------------------- 事实库溯源

    @app.get("/facts", response_class=HTMLResponse)
    def facts(request: Request, run: str | None = None, metric: str | None = None,
              layer: str | None = None, review: int = 0,
              fact_id: str | None = None, page: int = 1) -> Any:
        """事实库与溯源核验页。

        查询参数用 ``fact_id``：它装的是事实的**标识符**（形如 ``f_000123``），
        不是事实对象本身 —— 名字里带 ``_id`` 是为了让调用方一眼看出这点。

        ``page`` 与 ``metric``/``layer``/``review``/``fact_id`` **正交**：
        先筛选、再分页。改这几个参数时要保证筛选条件在翻页时被带下去，
        否则用户一翻页筛选就丢了（见下方 ``qs`` 的构造）。

        ⚠️ 另有一条**同类**的真实踩坑记录，改这一带的代码时请留意：
        Jinja2 的属性查找**优先于**键查找，因此凡是键名与 ``dict`` 方法同名的
        （``values`` / ``items`` / ``keys`` / ``get`` / ``copy``），模板里必须用
        下标访问 ``c['values']``，写 ``c.values`` 会解析成**方法对象**并报
        ``'builtin_function_or_method' object is not iterable``。
        """
        r, err = _run_or_error(run, topic="r1")
        if r is None:
            return templates.TemplateResponse(request, "empty.html", _ctx(request, None, message=err))

        all_facts = r.build_facts()
        filtered = all_facts
        if metric:
            filtered = [f for f in filtered if f.metric == metric]
        if layer:
            filtered = [f for f in filtered if f.layer_tag == layer]
        if review:
            filtered = [f for f in filtered if f.needs_review]

        # 选中一条展示溯源面板。**从完整筛选结果里找**，不受当前页限制 ——
        # 否则从别处跳进来指定一条不在第 1 页的事实就选不中了。
        selected = next((f for f in filtered if f.fact_id == fact_id), None)
        if selected is None and filtered:
            selected = filtered[0]

        evidence_context: dict[str, Any] = {}
        if selected and selected.evidence:
            ev = selected.evidence[0]
            page_text = r.page_text(ev.doc_id, ev.page)
            img = r.page_image(ev.doc_id, ev.page)
            loc = _locate_quote(page_text, ev.quote)
            evidence_context = {
                "page_text": page_text,
                "quote": ev.quote,
                "page": ev.page,
                "doc_id": ev.doc_id,
                "image_url": f"/page-image/{ev.doc_id}/{ev.page}" if img else None,
                "image_name": img.name if img else None,
                "loc": loc,
                # 预先切好三段，模板只做插入 —— 转义交给 Jinja2
                "parts": _split_for_highlight(page_text, loc),
            }

        # ------------------------------------------------------------ 分页
        # 359 条（别的 run 可达 999 条）一次性铺开是本页「内容太多」的主因。
        per_page = 50
        n_pages = max(1, -(-len(filtered) // per_page))   # 向上取整
        page = max(1, min(int(page or 1), n_pages))
        page_facts = filtered[(page - 1) * per_page: page * per_page]

        # 翻页链接要带上当前的全部**筛选**条件，否则一翻页筛选就丢了。
        # 刻意**不含** ``fact_id``：它是「看哪条的溯源」这个视图参数，不是筛选条件；
        # 而且每行的「溯源」链接要在本 URL 后面再追加 ``fact_id``，
        # 若这里已带一个就会出现重复参数 —— FastAPI 对标量查询参数取**第一个**，
        # 于是点哪行都停在旧的那条上。
        base_qs = {k: v for k, v in (
            ("run", r.run_id), ("metric", metric), ("layer", layer),
            ("review", review or None),
        ) if v}

        def _page_url(n: int) -> str:
            return "/facts?" + urlencode({**base_qs, "page": n})

        metric_counts: dict[str, int] = {}
        for f in all_facts:
            metric_counts[f.metric] = metric_counts.get(f.metric, 0) + 1

        sources = r.extraction_sources()
        reattribution = r.entity_reattribution()
        crosscheck = r.cross_validation()

        return templates.TemplateResponse(
            request,
            "facts.html",
            _ctx(
                request, r,
                facts=page_facts, total=len(all_facts),
                n_filtered=len(filtered),
                selected=selected, evidence_context=evidence_context,
                metric_counts=dict(sorted(metric_counts.items())),
                layer_counts=_count_by(all_facts, lambda f: f.layer_tag),
                review_count=sum(1 for f in all_facts if f.needs_review),
                conflicts=r.internal_conflicts,
                sources=sources,
                crosscheck=crosscheck,
                reattribution=reattribution,
                # 赛题的四条评分点 —— 必须在首屏，所以由路由算好直接传。
                criteria=criteria_r1(r),
                # 材料范围 —— 同样必须在首屏，且**全部现算**，见 material_scope。
                scope=material_scope(r),
                contract_version=r.facts_file.get("contract_version", "—"),
                f_metric=metric, f_layer=layer, f_review=review,
                # 图表基于**全量**事实（不是当前页）—— 它描述的是整个 run 的质量，
                # 不该随翻页跳动。图注里已说明这点。
                charts=CD.facts_charts(all_facts),
                pg={"page": page, "n_pages": n_pages, "per_page": per_page,
                    "start": (page - 1) * per_page + 1 if filtered else 0,
                    "end": min(page * per_page, len(filtered)),
                    "url": _page_url},
            ),
        )

    @app.get("/page-image/{doc_id}/{page}")
    def page_image(doc_id: str, page: int, run: str | None = None) -> Any:
        from fastapi.responses import FileResponse

        r, err = _run_or_error(run)
        if r is None:
            return JSONResponse({"error": err}, status_code=404)
        img = r.page_image(doc_id, page)
        if img is None:
            return JSONResponse({"error": "该页没有渲染图"}, status_code=404)
        return FileResponse(img)

    @app.get("/cell-crop/{doc_id}/{page}/{row}/{col}")
    def cell_crop(doc_id: str, page: int, row: int, col: int,
                  run: str | None = None, ctx: int = 0) -> Any:
        """单元格裁图 —— 模型**实际读的那张图**。

        演示时最有说服力的一屏：左边是模型看到的像素，右边是它的读数与
        闸门结论。没有这张图，"视觉裁决"就只是一句自述。
        """
        from fastapi.responses import FileResponse

        r, err = _run_or_error(run)
        if r is None:
            return JSONResponse({"error": err}, status_code=404)
        img = r.cell_crop(doc_id, page, row, col, context=bool(ctx))
        if img is None:
            return JSONResponse({"error": "没有该单元格的裁图"}, status_code=404)
        return FileResponse(img)

    # ---------------------------------------------------------------- 估值

    @app.get("/valuation", response_class=HTMLResponse)
    def valuation(request: Request, run: str | None = None,
                  tab: str | None = None) -> Any:
        # 页内标签（见 `_TAB_GROUPS`）。默认那支必须与从前一致 ——
        # 无参 `/valuation` 是 `tools/web_smoke.py:249` 与 `web_pages_check.py`
        # 的探针页面。所以这里只把**认得的** tab 翻成 view，别的一律当作默认，
        # 而不是给默认分支另起一个名字：`view=None` 就是「全部」。
        view = tab if tab in ("assumptions", "report") else None
        r, err = _run_or_error(run)
        if r is None:
            return templates.TemplateResponse(request, "empty.html", _ctx(request, None, message=err))

        val = r.valuation()
        charts = r.valuation_charts()
        report_html = render_markdown(r.valuation_report_md())

        assumptions_grounded = 0
        assumptions_total = 0
        if val:
            for a in val.get("assumptions", []):
                assumptions_total += 1
                if not a.get("is_judgment"):
                    assumptions_grounded += 1

        return templates.TemplateResponse(
            request,
            "valuation.html",
            _ctx(
                request, r, val=val, report_html=report_html,
                # 双轨：`charts` 是 matplotlib 落盘的 PNG 交付产物（报告 Markdown
                # 引用它们，必须保留）；`svg` 是网页内联矢量图，随主题变色、
                # 不额外发请求。两者数据同源，只是渲染方式不同。
                charts=charts, svg=CD.valuation_charts(val),
                assumptions_grounded=assumptions_grounded,
                assumptions_total=assumptions_total,
                view=view,
                # 赛题的五条评分点（模型结构/假设依据/财务勾稽/结果合理性/
                # 敏感性完整性）—— 通知 p3 原文用词，照抄便于评委逐字对照。
                criteria=criteria_r4(r, val),
            ),
        )

    @app.get("/valuation-chart/{name}")
    def valuation_chart(name: str, run: str | None = None) -> Any:
        from fastapi.responses import FileResponse

        r, err = _run_or_error(run)
        if r is None or name not in r.valuation_charts():
            return JSONResponse({"error": "图表不存在"}, status_code=404)
        return FileResponse(r.dir / "valuation" / "charts" / name)

    # ---------------------------------------------------------------- 核查

    @app.get("/audit", response_class=HTMLResponse)
    def audit(request: Request, run: str | None = None,
              tab: str | None = None) -> Any:
        # 同 `/valuation`：只认得的 tab 才切视图，未知值一律落到默认那支。
        view = tab if tab == "draft" else None
        r, err = _run_or_error(run)
        if r is None:
            return templates.TemplateResponse(request, "empty.html", _ctx(request, None, message=err))

        # ★ `audit()` 缺产物时返回 None，存在时返回 dict（哪怕 findings 为空）。
        #   必须把这两件事**分开**传给模板：原先模板用 `not findings and not metrics`
        #   当守卫，而 metrics 为 null 恰恰是常态（无 golden 标注集），
        #   于是「查完了、一条错都没检出」这个最常见的干净结果被误判成「没查」，
        #   整页塌成一句假告警。判据只能是**产物在不在**，不是**查出了几条**。
        raw_audit = r.audit()
        audit_missing = raw_audit is None
        a = raw_audit or {}
        findings = a.get("findings", [])
        by_sev = _count_by(findings, lambda f: f.get("severity", "?"))
        by_type = _count_by(findings, lambda f: f.get("error_type", "?"))

        # ★ 准确率/覆盖率/误报率**必须有黄金标注集**才算得出来，而演示 run 用的是
        #   合成草稿、没有标注。这三条因此从**评测 run** 取数，并在页面上写明
        #   数据来源 run_id —— 拿别的 run 的数字不标来源，等于让读者把它读成本
        #   run 的成绩。本 run 自己有 metrics 时优先用本 run 的。
        eval_run_id, eval_metrics = find_eval_run(r.dir.parent)

        return templates.TemplateResponse(
            request,
            "audit.html",
            _ctx(
                request, r, findings=findings, by_sev=by_sev, by_type=by_type,
                audit_missing=audit_missing,
                metrics=a.get("metrics"),
                eval_metrics=eval_metrics, eval_run_id=eval_run_id,
                criteria=criteria_r5(r, a, a.get("metrics"), eval_metrics, eval_run_id),
                draft=r.draft_text(),
                report_html=render_markdown(r.audit_report_md()),
                charts=CD.audit_charts(a),
                # 标签表由 chartdata 统一提供，模板不再自带一份 ——
                # 两处各存一份必然漂移（原先模板里的 E5 键名就与产物的不一致）。
                type_labels=CD.E1_LABELS, sev_labels=CD.SEV_LABELS,
                view=view,
            ),
        )

    @app.get("/annotated-draft", response_class=HTMLResponse)
    def annotated_draft(run: str | None = None) -> Any:
        from fastapi.responses import FileResponse

        r, err = _run_or_error(run)
        if r is None:
            return HTMLResponse(f"<pre>{html_mod.escape(str(err))}</pre>", status_code=404)
        p = r.dir / "audit" / "annotated_draft.html"
        if not p.is_file():
            return HTMLResponse("<p>批注版草稿不存在</p>", status_code=404)
        return FileResponse(p)

    # ---------------------------------------------------------------- 运行与复现

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request, run: str | None = None) -> Any:
        r, _err = _run_or_error(run)
        rows = []
        for s in available_runs(STATE["runs_root"]):
            rows.append({"summary": s, "is_current": bool(r and s.run_id == r.run_id)})

        integrity = None
        if r is not None:
            try:
                from agent.trace.replay import verify_run_integrity

                rep = verify_run_integrity(r.dir)
                integrity = {
                    "reproducible": rep.reproducible,
                    "hash_matches": rep.hash_matches,
                    "mismatches": rep.mismatches,
                    "checked": len(rep.hash_matches),
                }
            except Exception as exc:
                integrity = {"error": str(exc)}

        # ★ 产物清单有两个口径，模板要把两个都显示出来并解释差额：
        #   `outputs()` 是**磁盘扫描**（会看见 .lock、cache/llm.jsonl），
        #   manifest.outputs 是**流水线声明的产物**。原先模板只显示前者、
        #   标题写作「本次运行的产物」，于是同页出现 670 与 667 两个数字，
        #   读者只看到「对不上」。两个数都对，是定义不同 —— 说清楚即可。
        manifest_output_count = 0
        if r is not None:
            manifest_output_count = len(r.manifest_outputs())

        return templates.TemplateResponse(
            request,
            "runs.html",
            _ctx(request, r, rows=rows, integrity=integrity,
                 trace=r.trace_summary() if r else {},
                 outputs=r.outputs() if r else [],
                 manifest_output_count=manifest_output_count),
        )

    # ---------------------------------------------------------------- 开始分析
    #
    # 「把材料丢进 inbox/ → 点一次 → 跑完整分析」。
    # 这是 Web 端唯一的**写入口**：其余七个页面全部只读产物。
    # 真实执行交给子进程（与命令行同一条路径），本页只负责启动与看进度。

    def _analyze_ctx(request: Request, run: RunArtifacts | None,
                     error: str | None) -> dict[str, Any]:
        from web import analyze as az

        return _ctx(
            request, run,
            inv=az.inventory(),
            task=az.status(runs_root=STATE["runs_root"]),
            default_run_id=az.new_run_id(),
            error=error,
        )

    @app.get("/analyze", response_class=HTMLResponse)
    def analyze_page(request: Request, run: str | None = None) -> Any:
        r, _err = _run_or_error(run)
        return templates.TemplateResponse(request, "analyze.html", _analyze_ctx(request, r, None))

    @app.post("/analyze/start", response_class=HTMLResponse)
    def analyze_start(
        request: Request,
        run_id: str = Form(""),
        confirm: str = Form(""),
        run: str | None = Form(None),
    ) -> Any:
        from web import analyze as az

        r, _err = _run_or_error(run)
        error: str | None = None
        try:
            az.start(run_id=run_id.strip() or None, confirmed=bool(confirm),
                     runs_root=STATE["runs_root"])
        except az.AnalyzeError as exc:
            error = str(exc)
        except Exception as exc:      # 现场演示：任何异常都要变成页面上的提示
            error = f"{type(exc).__name__}: {exc}"
        return templates.TemplateResponse(
            request,
            "analyze.html", _analyze_ctx(request, r, error)
        )

    @app.post("/analyze/resume")
    def analyze_resume(
        request: Request,
        run_id: str = Form(""),
        confirm: str = Form(""),
        run: str | None = Form(None),
    ) -> Any:
        """续跑上次中断的运行（跳过已完成阶段、复用已调用过的模型结果）。"""
        from web import analyze as az

        r, _err = _run_or_error(run)
        error: str | None = None
        try:
            st = az.status(runs_root=STATE["runs_root"])
            target = run_id.strip() or st.get("run_id")
            if not target:
                raise az.AnalyzeError("没有可续跑的运行。")
            az.start(run_id=target, confirmed=bool(confirm), resume=True,
                     runs_root=STATE["runs_root"])
        except az.AnalyzeError as exc:
            error = str(exc)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        return templates.TemplateResponse(
            request,
            "analyze.html", _analyze_ctx(request, r, error)
        )

    @app.get("/analyze/status")
    def analyze_status() -> Any:
        from web import analyze as az

        return JSONResponse(az.status(runs_root=STATE["runs_root"]))

    @app.post("/analyze/probe-model")
    def analyze_probe_model() -> Any:
        """一次最小真实调用，确认模型可用（十几 token，几秒钟）。

        真实全流程要跑十几分钟，先花这一次调用换掉"等了 18 分钟才发现 Key 过期"。
        """
        from web import analyze as az

        return JSONResponse(az.probe_model())

    # ---------------------------------------------------------------- 现场处理

    @app.get("/process", response_class=HTMLResponse)
    def process_page(request: Request, run: str | None = None) -> Any:
        r, _err = _run_or_error(run)
        return templates.TemplateResponse(
            request,
            "process.html", _ctx(request, r, result=None, error=None)
        )

    @app.post("/process", response_class=HTMLResponse)
    async def process_submit(
        request: Request,
        file: UploadFile = File(...),
        run: str | None = Form(None),
    ) -> Any:
        from web.live import process_upload

        r, _err = _run_or_error(run)
        result: dict[str, Any] | None = None
        error: str | None = None
        try:
            data = await file.read()
            result = process_upload(file.filename or "upload.pdf", data)
        except Exception as exc:  # 现场演示：任何异常都要变成页面上的友好提示
            error = f"{type(exc).__name__}: {exc}"

        return templates.TemplateResponse(
            request,
            "process.html",
            _ctx(request, r, result=result, error=error),
        )

    # ---------------------------------------------------------------- 轨迹流

    @app.get("/trace/stream")
    def trace_stream(run: str | None = None, offset: int = 0,
                     limit: int = 200, kinds: str | None = None) -> Any:
        """调试抽屉的轮询端点（契约见模块顶部那一节）。

        ★ 刻意**不走 ``_run_or_error``**：``available_runs`` 把没有
        ``manifest.json`` 的 run 标成「未完成」，而 ``resolve_run`` 会把这类
        排除掉 —— 偏偏**运行中的 run 正是没有 manifest 的那个**。
        走标准助手的话，抽屉会在最需要它的时刻（运行中）返回错误页。
        这里只做 run-id 同形校验 + 路径包含检查，直接解析目录。
        """
        from web.analyze import _RUN_ID_RE

        rid = (run or "").strip()
        base = {
            "run_id": rid, "offset": 0, "next_offset": 0, "eof": True,
            "live": False, "rows": [], "stats": _zero_stats(), "error": None,
        }
        if not rid or not _RUN_ID_RE.match(rid):
            base["error"] = "run id 不合法"
            return JSONResponse(base)

        root = Path(STATE["runs_root"]).resolve()
        run_dir = (root / rid).resolve()
        if root not in run_dir.parents:          # 防 `..` 逃逸
            base["error"] = "run id 不合法"
            return JSONResponse(base)
        if not run_dir.is_dir():
            base["error"] = f"没有这个 run：{rid}"
            return JSONResponse(base)

        want: set[str] | None = None
        if kinds:
            want = {k.strip() for k in kinds.split(",") if k.strip() in _ALL_TRACE_KINDS}
            want = want or None                  # 全是未知名字 ⇒ 不过滤，而不是全滤掉

        payload = _tail_trace(run_dir, offset=offset, limit=limit, kinds=want)
        payload["run_id"] = rid
        # 「还在写」= 没有 manifest（manifest 只在运行结束时落盘）。
        payload["live"] = not (run_dir / "manifest.json").exists()
        return JSONResponse(payload)

    # ---------------------------------------------------------------- 健康检查

    @app.get("/healthz")
    def healthz() -> Any:
        return {"ok": True, "runs": len(available_runs(STATE["runs_root"]))}

    return app


def _locate_quote(page_text: str, quote: str) -> dict[str, Any] | None:
    """在**原文**中定位 quote 的字符区间（用于溯源面板左栏高亮）。

    ⚠️ 不能直接用归一化后的下标 —— `check_quote` 的匹配是在
    去掉空白/标点、全角转半角之后的文本上做的，那个下标映射不回原文，
    直接切片会截出错位的内容（甚至切到半个汉字）。

    两级定位：
        1. **精确**：在原文中直接找 quote 的字面子串（覆盖绝大多数情况，
           因为 quote 就是照着原文抄的）
        2. **模糊**：容忍 OCR 噪声时，用 `SequenceMatcher` 在原文上
           找最佳对齐块，取其区间

    Returns:
        ``{"start", "end", "match", "similarity"}``；完全找不到返回 None。
    """
    from difflib import SequenceMatcher

    from agent.validate.quotecheck import normalize_text

    if not page_text or not quote:
        return None

    # —— 一级：字面子串 ——
    idx = page_text.find(quote)
    if idx >= 0:
        return {"start": idx, "end": idx + len(quote), "match": "exact", "similarity": 1.0}

    # —— 二级：去除空白后仍是子串（原文有换行、quote 没有）——
    compact_map: list[int] = []          # 压缩文本下标 -> 原文下标
    compact_chars: list[str] = []
    for i, ch in enumerate(page_text):
        if ch not in " \t\r\n\u3000":
            compact_chars.append(ch)
            compact_map.append(i)
    compact = "".join(compact_chars)
    q_compact = "".join(ch for ch in quote if ch not in " \t\r\n\u3000")
    if q_compact:
        ci = compact.find(q_compact)
        if ci >= 0:
            s = compact_map[ci]
            e = compact_map[min(ci + len(q_compact) - 1, len(compact_map) - 1)] + 1
            return {"start": s, "end": e, "match": "whitespace_insensitive",
                    "similarity": 1.0}

    # —— 三级：归一化后模糊对齐，再映射回原文 ——
    nq = normalize_text(quote)
    np_ = normalize_text(page_text)
    if not nq or not np_:
        return None
    sm = SequenceMatcher(None, nq, np_, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size >= max(4, len(nq) // 3)]
    if not blocks:
        return None
    first, last = blocks[0], blocks[-1]
    # 归一化下标 -> 压缩下标（归一化只是删标点+转半角，长度可能变），
    # 用比例近似映射后再吸附到最近的压缩下标
    ratio_s = first.b / max(1, len(np_))
    ratio_e = (last.b + last.size) / max(1, len(np_))
    ci_s = min(int(ratio_s * len(compact_map)), max(0, len(compact_map) - 1))
    ci_e = min(int(ratio_e * len(compact_map)), len(compact_map) - 1)
    if not compact_map:
        return None
    return {
        "start": compact_map[max(0, ci_s)],
        "end": compact_map[max(0, ci_e)] + 1,
        "match": "fuzzy",
        "similarity": round(sm.ratio(), 4),
    }


def _split_for_highlight(page_text: str, loc: dict[str, Any] | None) -> dict[str, str]:
    """把原文切成「高亮前 / 高亮 / 高亮后」三段。

    在模板里做字符串切片既难读又容易出错，因此在视图层切好。
    Jinja2 的自动转义会对三段分别转义，因此不存在 XSS 风险。
    """
    if not page_text:
        return {"before": "", "mark": "", "after": ""}
    if not loc or loc.get("start") is None:
        return {"before": page_text, "mark": "", "after": ""}
    s, e = int(loc["start"]), int(loc["end"])
    s = max(0, min(s, len(page_text)))
    e = max(s, min(e, len(page_text)))
    return {"before": page_text[:s], "mark": page_text[s:e], "after": page_text[e:]}


def _count_by(items: list[Any], key: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        k = str(key(it))
        out[k] = out.get(k, 0) + 1
    return out


# ==========================================================================
# 重点考察实测表（选题 1 / 4 / 5 共用）
# ★ 表名用通知的原词「重点考察」，不写「评审要点」—— 通知全文没有后者。
#
# 为什么要有这个东西
# ------------------
# 赛题对每道题都写明了「重点考察」什么，例如选题 1 是「字段提取的**完整性、
# 格式规范性、表格解析准确性和结果一致性**」（通知 p2）。这四条就是评分点，
# 所以它们必须是页面上**第一眼看到**的东西 —— 而不是散在十几张图表里，
# 让评委自己去拼。
#
# 三条自律（写代码时请保持）：
#   1. 每个数字都从**已落盘的产物**现算，不写死、不挑好看的舍入。
#   2. 凡是我们**无法自证**的，就在 note 里明说无法自证。典型例子：
#      「文档里到底有多少个数字、我们抽到了几成」—— 没有金标准就算不出
#      召回率，所以「完整性」一栏量的是**结构完整性**（必填项、五要素、
#      证据链），并注明这不是召回率。
#   3. 达不到 100% 就照实写。这一栏是**读数**，不是宣传语。
# ==========================================================================


def _crit(idx: str, label: str, verdict: str, note: str, mark: str = "√") -> dict[str, str]:
    """一条重点考察项的实测读数。

    Args:
        idx: 底稿索引，形如 ``①``…``④``。
        label: 赛题原话里的考察点名称，**照抄通知用词**，便于评委逐字对照。
            ★ 2026-09-27 补齐三处未照抄的（审计发现）：
              选题 1 的 ① 原写「完整性」，通知原文是「**字段提取的**完整性」
              （通知 p2）—— 省掉限定语会让评委以为本行测的就是官方那个考察点，
              而本行实测的是**结构完整性**（契约必填项 / 五要素 / 证据锚点），
              不是字段级召回。同行的 note 本来就自承了这一点，标签却把它抹平了。
              选题 5 的 ①「准确性与覆盖率」→「**错误识别的**准确性和覆盖率」、
              ④「修改建议可执行性」→「修改建议**的**可执行性」（通知 p3）。
              这条规则的三个反例全都出在「顺手把长名字写短」上 ——
              所以现在改标签前先回通知原文搜一遍，不要凭记忆写。
        verdict: 实测值，短，会以 mono 显示。
        note: 这个数是怎么量出来的 + 哪里量不到。
        mark: ``√`` 达标 / ``◐`` 部分达标或须人工看 / ``○`` 提示 / ``×`` 不达标。
    """
    return {"idx": idx, "label": label, "verdict": verdict, "note": note, "mark": mark}


def _e(v: Any) -> str:
    """把**产物里取出来的字符串**转义后再拼进 note。

    note 会以 ``|safe`` 渲染（见 ``_criteria.html``）—— 因为 note 里的
    ``<b>``/``<code>`` 是我们自己写的强调标记，必须真的成为标签。
    代价是：任何从产物 json 里读出来的字符串都**必须在源头转义**，
    否则产物里的一个 ``<`` 就能把整张表的结构撕开。
    数字（int/float）不受影响，``str()`` 之后过一遍也无副作用 ——
    统一走这里，比逐个判断哪个字段「应该安全」可靠。
    """
    return html_mod.escape(str(v), quote=False)


def _r1_completeness(r: RunArtifacts, facts: list[dict[str, Any]]) -> dict[str, str]:
    """选题 1 · 完整性 —— 量的是**结构完整性**，不是召回率。

    「文档里一共写了多少个数字、我们抽到了几成」需要金标准才成立，
    我们**没有**逐页人工点数的基准，所以不报召回率 —— 报一个自己算不出来的
    比率，比不报更糟。

    能自证的三件事（全部从 facts.json 现算）：
      · 契约必填项是否条条齐全；
      · 五个业务要素（值/单位/主体/期间/原文）是否条条齐全；
      · 证据链是否条条落地、有无悬空引用（引用了不存在的 evidence span）。
    """
    schema_path = PROJECT_ROOT / "configs" / "schemas" / "fact.schema.json"
    required: list[str] = []
    try:
        required = list(json.loads(schema_path.read_text(encoding="utf-8")).get("required", []))
    except (OSError, json.JSONDecodeError):
        pass

    n = len(facts)
    missing_req = sum(1 for f in facts if any(f.get(k) in (None, "") for k in required))
    five_keys = ("value", "unit", "entity", "period", "raw_text")
    missing_five = sum(
        1 for f in facts if any(not str(f.get(k) or "").strip() for k in five_keys)
    )
    # 证据链完整性：每条事实都要有锚点，且引用的 span 必须真实存在。
    span_ids = {e.get("span_id") for e in (r.facts_file.get("evidence") or [])}
    no_evidence = sum(1 for f in facts if not f.get("evidence_span_ids"))
    dangling = sum(
        1 for f in facts for s in (f.get("evidence_span_ids") or []) if s not in span_ids
    )

    dims = (
        f"{len({f.get('metric') for f in facts})} 个指标 × "
        f"{len({f.get('entity') for f in facts})} 个主体 × "
        f"{len({f.get('period') for f in facts})} 个报告期"
    )
    bad = missing_req + missing_five + no_evidence + dangling
    mark = "√" if bad == 0 else ("◐" if bad < max(1, n // 20) else "×")
    return _crit(
        "①", "字段提取的完整性",
        f"{n} 条 · 缺项 {bad} 条",
        f"{dims}。契约 {len(required)} 个必填项缺 {missing_req} 条；"
        f"值/单位/主体/期间/原文五要素缺 {missing_five} 条；"
        f"无证据锚点 {no_evidence} 条；悬空引用 {dangling} 处。"
        f"※ 这是<b>结构完整性</b> —— 我们能证明「抽到的东西是完整的」，"
        f"<b>不能</b>证明「文档里的数字都抽到了」（那需要人工逐页点数做金标准）。",
        mark,
    )


def _r1_schema(r: RunArtifacts, facts: list[dict[str, Any]]) -> dict[str, str]:
    """选题 1 · 格式规范性 —— 拿**公布出去的契约文件**独立校验交付产物。

    要点在于校验者是**独立的**：产物写入时走的是 pydantic 构造，这里改用
    ``configs/schemas/`` 里的 JSON Schema（与流水线同一个校验器，
    ``agent/validate/schema.py``）重新验一遍。同一份数据被两套机制都接受，
    才谈得上「按规定的数据结构输出」。

    两级都验：整份 ``facts.json`` 对 ``facts.schema.json``（FactSet 契约），
    再逐条对 ``fact.schema.json``（单条契约）。两级是 ``additionalProperties:
    false`` 的严格模式 —— 多一个字段都算不通过。
    """
    from agent.validate.schema import has_jsonschema, load_schema, validate_json

    schema_dir = PROJECT_ROOT / "configs" / "schemas"
    if not has_jsonschema():
        return _crit(
            "②", "格式规范性", "无法校验",
            "本机未安装 <code>jsonschema</code>，只能给出结构骨架校验，"
            "不足以声称通过契约。安装后本页会自动给出逐条结果。",
            "○",
        )
    try:
        set_errors = validate_json(
            r.facts_file, load_schema(schema_dir / "facts.schema.json"),
            what="facts.json", strict=False,
        )
        one = load_schema(schema_dir / "fact.schema.json")
    except Exception as exc:  # 契约文件缺失/损坏 —— 照实说，不假装通过
        return _crit("②", "格式规范性", "契约不可用", f"读取契约失败：{exc}", "○")

    bad = [f.get("fact_id") for f in facts if validate_json(f, one, strict=False)]
    ok = len(facts) - len(bad)
    strict = "additionalProperties=false（多一个字段即不通过）"
    if not set_errors and not bad:
        return _crit(
            "②", "格式规范性",
            f"{ok}/{len(facts)} 条通过",
            f"整份 <code>facts.json</code> 通过 FactSet 契约（0 处错误），"
            f"逐条通过单条 Fact 契约（{ok}/{len(facts)}）。"
            f"校验器 <code>agent/validate/schema.py</code>，契约 "
            f"<code>configs/schemas/facts.schema.json</code> + "
            f"<code>fact.schema.json</code>，{strict}。"
            f"合同版本 <code>{_e(r.facts_file.get('contract_version', '—'))}</code>。",
            "√",
        )
    sample = (set_errors or [f"{bad[0]} 未通过单条契约"])[0]
    return _crit(
        "②", "格式规范性", f"{ok}/{len(facts)} 条通过",
        f"FactSet 契约 {len(set_errors)} 处错误，单条契约 {len(bad)} 条不通过。"
        f"首条：<code>{_e(str(sample)[:160])}</code>",
        "◐" if ok else "×",
    )


def _r1_tables(r: RunArtifacts) -> dict[str, str]:
    """选题 1 · 表格解析准确性 —— 重建了多少张表，以及单元格级裁决的分布。

    口径必须说清楚，否则「一致 275 / 受检 378」会被读成一个准确率：
    378 是**送进视觉裁决闸门**的格数（不是全部单元格），275 是模型读数与
    程序读数一致的格数。剩下 81 格不一致但**未过闸门 → 没有改动任何一个值**，
    22 格因裁剪或调用失败未能裁决。真正的结论是最后那句：
    **改值 0 格** —— 在这批数据上，模型一次都没有改写程序读数。
    """
    st = r.table_stats()
    cand = int(r.build_stats.get("table_grid_candidates", 0) or 0)
    accepted = int(r.build_stats.get("table_grid_accepted", 0) or 0)
    checked = st["cells_checked"]
    agree = st["cells_agree"]
    rejected = st["cells_rejected"]
    errors = st["cells_errors"]
    corrected = st["cells_corrected"]

    if not st["tables"]:
        return _crit("③", "表格解析准确性", "本次无表格", "该 run 未产出表格重建记录。", "○")

    pct = f"{agree / checked * 100:.0f}%" if checked else "—"
    arith = st["arith_checked"]
    arith_bad = st["arith_failed_tables"]
    mark = "√" if corrected == 0 and errors == 0 else "◐"
    # ★ 预算用尽时必须说一句（见 vlm_budget 的 docstring）：闸门用尽后
    #   judge.py 会静默 break 掉后面所有表格的裁决，不留任何记录。
    #   不说这一句，「逐格复核 N 格」会被读成「该查的都查了」。
    vb = vlm_budget(r)
    budget_note = ""
    if vb["exhausted"]:
        budget_note = (
            f"<b>⚠ 视觉通道预算已用尽</b>（{vb['used']}/{vb['cap']} 次，"
            f"含整页扫描 {vb['page_scans']} 次 + 单元格 {vb['cells']} 格）："
            f"闸门用尽后，后面还有表格<b>没有被裁决</b> —— 这一条是"
            f"<b>静默 break</b>，产物与日志里都没有记录，本页据此现算。"
            f"所以上表的「受检 {checked} 格」是<b>受预算约束的下界</b>，"
            f"不是「该查的都查了」。"
        )
    return _crit(
        "③", "表格解析准确性",
        f"{st['tables']} 张表 · 逐格复核 {checked} 格",
        f"重建 {st['tables']} 张表（其中有框线 {st['tables_with_rules']} 张），"
        f"对齐 {r.build_stats.get('table_grid_align_cols', 0)} 列，"
        f"候选 {cand} 条采信 {accepted} 条。"
        f"单元格级视觉裁决：受检 {checked} 格，与程序读数一致 {agree} 格（{pct}）、"
        f"不一致但<b>未过闸门∴未改动</b> {rejected} 格、未能裁决 {errors} 格，"
        f"<b>实际改值 {corrected} 格</b>。"
        f"合计线验算 {arith} 条，不平 {arith_bad} 张表 —— 不平的表已标记，"
        f"不静默采用。"
        f"※ {pct} <b>不是准确率</b>：分母是「送进闸门的格」，不是「全部单元格」。"
        + (f"<br>{budget_note}" if budget_note else ""),
        mark,
    )


def _r1_consistency(r: RunArtifacts, facts: list[dict[str, Any]],
                    conflicts: list[Any]) -> dict[str, str]:
    """选题 1 · 结果一致性 —— 同一条事实被两条独立路径读到时，是否读成同一个值。

    三个方向合起来才叫「一致性」，缺一个都会被读成「我们保证一致」：
      · **路径构成**：确定性 vs 模型。确定性占比越高，可复算的比例越高。
        真实材料上曾实测 73% 的事实来自 VLM 整页扫描，补齐表格网格直取后
        降到 29% —— 这是本项目的主张，所以这一栏报的是实测占比。
      · **双路互证**：模型读数里有多少被确定性路径独立复现。
        这个比例低是**结构使然**（确定性路径已覆盖约 88%，模型读的是剩下的），
        所以报的是**未被复现的那部分**（model_only），而不是「互证项里有几条冲突」
        —— 后者按集合交的定义恒为 0，详见函数体内的注释。
      · **冲突标记**：同主体指标冲突与量纲闸门命中，两条都**保留并标记**，
        不静默挑一个用。
    """
    src = r.extraction_sources()
    cv = r.cross_validation()
    scale = int(r.build_stats.get("scale_implausible", 0) or 0)
    det = int(src.get("deterministic", 0) or 0)
    total = int(src.get("total", 0) or 0) or len(facts)

    if not total:
        return _crit("④", "结果一致性", "无事实条目", "事实库为空，一致性无从度量。", "○")

    det_pct = f"{det / total * 100:.0f}%"
    confirmed = int(cv.get("confirmed", 0) or 0)
    model_values = int(cv.get("model_values", 0) or 0)
    cv_pct = f"{confirmed / model_values * 100:.1f}%" if model_values else "—"
    mark = "√" if not conflicts else "◐"
    # ★ 「冲突 0 条」这句话 2026-09-27 删掉了。原先写的是：
    #     f"…其中被确定性路径独立复现 {confirmed} 个（{cv_pct}），<b>冲突 0 条</b>。"
    #   两个毛病，第二个更重：
    #   ① **与同行的数字直接打架** —— 本行「实测」栏与下面一句都写着
    #      `len(conflicts)`（本 run 53 条），只有中间这半句写死 0。同一行里
    #      同时出现「冲突 0 条」与「已标记同主体冲突 53 条」，读者只会得出
    #      「这个页面在自说自话」。
    #   ② **它是个恒真的空话**。`cross_validation()` 的 confirmed 取的是
    #      `det & mod`，即**两边读数值完全相同的 (主体,指标,值) 三元组** ——
    #      按定义就不可能「不一致」。把恒为 0 的东西报成一项测量结果，
    #      比报错更坏：它长得像证据。真正该报的是**没被复现的那部分**
    #      （model_only，本 run 52 个），那才是需要人工抽检的量。
    # ★ 「实测」栏从此必须自带两个限定语 —— 2026-10 之后它右边不再有
    #   「这个数是怎么来的」那一整列（见 _criteria.html）：
    #     · `未被复现 N 个`：真正测到的量（model_only），**不是**那个恒 0 的
    #       「互证项冲突数」。这两个数长得很像，少了限定语就会被读成后者。
    #     · `（互证项恒为 0）`：点明那个按集合交定义恒为 0 的数，免得被读成
    #       「查过、两路没冲突」——那正是 2026-09-27 删掉「冲突 0 条」的原因。
    return _crit(
        "④", "结果一致性",
        f"确定性 {det_pct} · 未被复现 {model_values - confirmed} 个 · "
        f"冲突 {len(conflicts)} 条（互证项恒为 0）",
        f"抽取路径：确定性 {det} 条 / 模型 {total - det} 条（确定性占 {det_pct}）—— "
        f"能从原文结构直接定位的不交给模型读。"
        f"双路互证：模型读到 {model_values} 个取值，其中被确定性路径独立复现 "
        f"{confirmed} 个（{cv_pct}）—— <b>未被复现 {model_values - confirmed} 个</b>。"
        f"※ 互证率低是<b>结构使然</b>（两路覆盖的页面本就基本不重叠），"
        f"不能读成「模型读数已被验证」—— 恰恰相反，未被复现的那部分最需要人工抽检。"
        f"※ 这里<b>不报「互证项里有几条冲突」</b>：互证集合按定义取的就是两边读数"
        f"相同的取值，那个数恒为 0，不是测量结果。"
        f"真正的一致性风险在事实库内部，见下："
        f"已标记同主体同指标多值冲突 {len(conflicts)} 条、量纲闸门 {scale} 条，"
        f"期间矛盾 {r.build_stats.get('period_contradiction', 0)} 条；"
        f"全部<b>保留并标记</b>，不静默挑一个用。",
        mark,
    )


def criteria_r1(r: RunArtifacts) -> list[dict[str, str]]:
    """选题 1 的四条重点考察项，按赛题顺序返回。

    ★ 一律读 ``facts.json`` 里的**原始记录**，不读 ``build_facts()`` 转出来的
    视图模型。理由：「完整性」与「格式规范性」量的是**交付出去的产物**，
    而视图模型是网页为了好渲染做的二次封装 —— 拿封装后的对象去证明产物合规，
    等于自己证明自己。原始 dict 才是评委拿到的那份东西。
    """
    facts = r.facts_file.get("facts") or []
    conflicts = r.internal_conflicts
    return [
        _r1_completeness(r, facts),
        _r1_schema(r, facts),
        _r1_tables(r),
        _r1_consistency(r, facts, conflicts),
    ]


def criteria_r4(r: RunArtifacts, val: dict[str, Any] | None) -> list[dict[str, str]]:
    """选题 4 的五条重点考察项（通知 p3）。

    赛题原文：重点考察**模型结构、假设依据、财务勾稽关系、估值结果合理性
    和敏感性分析完整性**。前四条都能从产物直接量出来；「合理性」没有绝对
    标准，只能报**可核对的相对位置**（区间宽窄、终值占比），并说明判据，
    不装作有一个客观分数。
    """
    if not val:
        return [_crit("①", "模型结构", "无产物", "该 run 未产出估值模型。", "○")]

    dcf = val.get("dcf") or {}
    years = dcf.get("explicit_years") or []
    comps = val.get("comps") or {}
    peers = comps.get("peers") or []
    sens = val.get("sensitivity") or {}
    grid = sens.get("grid") or []
    asserts = val.get("assertions") or []
    passed = sum(1 for a in asserts if a.get("status") == "pass")
    skipped = sum(1 for a in asserts if a.get("status") == "skipped")
    failed = [a for a in asserts if a.get("status") not in ("pass", "skipped")]

    low, high = val.get("value_range_low"), val.get("value_range_high")
    mid = val.get("value_range_mid")
    try:
        width = (float(high) - float(low)) / float(mid) * 100 if mid else None
    except (TypeError, ValueError, ZeroDivisionError):
        width = None
    tv_share = dcf.get("terminal_value_share")

    assumptions = val.get("assumptions") or []
    # 「锚定事实库」= 该假设带 source_fact_ids，可点开核对到具体某条事实；
    # 只写了一段文字说明而没有 fact_id 的，不算锚定。
    grounded = sum(1 for a in assumptions if a.get("source_fact_ids"))
    judgments = sum(1 for a in assumptions if a.get("is_judgment"))

    return [
        _crit(
            "①", "模型结构",
            f"DCF {len(years)} 年 + 相对估值 {len(peers)} 家",
            f"两法并用：DCF 显式预测 {len(years)} 年 + 终值"
            f"（WACC {_e(dcf.get('wacc', '—'))}、永续增长 {_e(dcf.get('terminal_growth', '—'))}），"
            f"相对估值取 {len(peers)} 家可比公司。"
            f"合同版本 <code>{_e(val.get('contract_version', '—'))}</code>。",
            "√" if years and peers else "◐",
        ),
        _crit(
            "②", "假设依据",
            f"{len(assumptions)} 条假设 · 逐条标注来源",
            f"{len(assumptions)} 条关键假设每条都带来源说明；其中"
            f"<b>{grounded} 条锚定事实库</b>的具体 fact_id（可点开核对），"
            f"{judgments} 条标注为<b>判断</b>（无事实支撑，属推论/观点）。"
            f"这是赛题「明确区分事实、推论与观点」的落点。",
            "√" if assumptions else "○",
        ),
        _crit(
            "③", "财务勾稽关系",
            f"{passed}/{len(asserts)} 条通过 · 未校验 {skipped} 条",
            f"模型自带 {len(asserts)} 条勾稽断言（资产=负债+权益、利润表纵向勾稽等），"
            f"通过 {passed} 条、<b>未校验 {skipped} 条</b>（缺明细数据，无法独立重算）、"
            f"失败 {len(failed)} 条。"
            f"※「未校验」<b>不是</b>「通过」—— 两者在产物里是不同的状态值，本页也分开写；"
            f"所以这一栏只有在 {len(asserts)} 条全部通过时才记 √。",
            "×" if failed else ("√" if passed == len(asserts) else "◐"),
        ),
        _crit(
            "④", "估值结果合理性",
            f"{low} – {high} 元/股",
            f"区间取 DCF 点估计与相对估值区间的<b>并集极值</b>（不平均 —— "
            f"平均会掩盖模型分歧），中值 {mid} 元，"
            + (f"区间宽度约为中值的 {width:.0f}%。" if width is not None else "")
            + (f"终值占 DCF 价值 {float(tv_share) * 100:.1f}%"
               f"（越高越依赖远期假设）。" if tv_share else "")
            + "※「合理性」<b>没有客观分数线</b> —— 所以这一栏的记号是 <b>◐（须人工看）</b>，"
              "不是 √（达标）。本行只报可独立复核的相对位置（区间、中值、宽度、"
              "终值占比），判据见报告与假设表；把没有判据的考察点标成达标，"
              "就是把「我算了个数」说成「我这个数合理」。",
            # ★ 2026-09-27 改：原为写死的 "√"。两处毛病：
            #   ① 与本行 note 自述的「没有客观分数线」直接冲突；
            #   ② `_criteria.html` 的表脚写着「本表每个数字都从已落盘的产物现算，
            #      没有写死的值」—— 一个写死的记号就推翻了那句话。
            #   现在改成 ◐，且**仍是一个判断而非读数**，所以 note 里写明它表示
            #   「须人工看」。要在答辩时被问「凭什么标达标」，这一行是答不上来的。
            "◐",
        ),
        _crit(
            "⑤", "敏感性分析完整性",
            f"{_e(sens.get('x_name', '—'))} × {_e(sens.get('y_name', '—'))} 共 {len(grid)} 格",
            f"双因素网格：横轴 <code>{_e(sens.get('x_name', '—'))}</code> × "
            f"纵轴 <code>{_e(sens.get('y_name', '—'))}</code>，共 {len(grid)} 格，"
            f"每格给出一个每股价值。另有龙卷风图（单因素）与瀑布图（FCFF 构成）。",
            "√" if grid else "○",
        ),
    ]


def criteria_r5(r: RunArtifacts, a: dict[str, Any], metrics: dict[str, Any] | None,
                eval_metrics: dict[str, Any] | None,
                eval_run_id: str | None) -> list[dict[str, str]]:
    """选题 5 的四条重点考察项（通知 p3）。

    赛题原文：重点考察**错误识别的准确性和覆盖率、误报控制、来源定位以及
    修改建议的可执行性**。

    ★ 关键的一点：准确率/覆盖率/误报控制**必须有黄金标注集才算得出来**，
    而演示 run 用的是合成草稿、没有标注。所以这三条一律**从评测 run** 取数
    （``runs/r5-eval-annual16``，对 ``data/r5_eval/`` 的 3 份人工标注草稿评测），
    并在 note 里写明数据来源 run_id —— 拿别的 run 的数字不标注来源，
    就是在让读者误以为是本 run 的成绩。
    """
    findings = a.get("findings") or []
    m = eval_metrics or metrics or {}
    src = f"<code>runs/{_e(eval_run_id)}</code>" if eval_run_id else "本 run"
    checked = m.get("claims_evaluated") or a.get("claims_checked") or 0

    if not m:
        prf = _crit(
            "①", "错误识别的准确性和覆盖率", "无标注集",
            "本 run 未接黄金标注集，准确率/覆盖率<b>算不出来</b>，"
            "因此不报 —— 报一个没有分母的比率不如不报。",
            "○",
        )
        fp = _crit("②", "误报控制", "无标注集",
                   "误报率同样需要标注集才能计算，本 run 无法给出。", "○")
    else:
        p = m.get("precision")
        rec = m.get("recall")
        cov = m.get("coverage")
        prf = _crit(
            "①", "错误识别的准确性和覆盖率",
            f"精确率 {p:.2f} · 召回率 {rec:.2f}"
            if p is not None and rec is not None else "n/a",
            f"对 {src} 的 {m.get('drafts_evaluated', 0)} 份人工标注草稿评测："
            f"TP {m.get('true_positive', 0)} / FP {m.get('false_positive', 0)} / "
            f"FN {m.get('false_negative', 0)}，"
            f"错误类型覆盖率 {cov * 100:.0f}%（E1–E6 中被检出过的类型比例）"
            if cov is not None else "覆盖率 n/a",
            # 记 √ 只能是两面都好：精确率满分**且**召回率过半。
            # 只看精确率给 √，会在一行「召回率 0.50」旁边打一个达标记号 ——
            # 那正好把这一栏最该被看见的短板盖掉。
            "√" if (p == 1 and (rec or 0) >= 0.6) else "◐",
        )
        fp = _crit(
            "②", "误报控制",
            f"误报 {m.get('false_positive', 0)} 条 · 陷阱项命中 {m.get('false_positive_on_trap', 0)} 个",
            f"{src} 的标注集里含 <b>{m.get('traps_evaluated', 0)} 个陷阱项</b>"
            f"（<code>is_real_error=false</code>，即「看着像错、其实是对的」）。"
            f"误报 <b>{m.get('false_positive', 0)}</b> 条，"
            f"其中命中陷阱项 <b>{m.get('false_positive_on_trap', 0)}</b> 个 —— "
            f"这是「误报控制」这一考察点的直接度量。",
            "√" if not m.get("false_positive") else "×",
        )

    # 来源定位 / 修改建议：只统计本次真正检出的条目。
    located = [f for f in findings if (f.get("location") or {}).get("char_range")]
    anchored = [f for f in findings
                if (f.get("location") or {}).get("quote") or f.get("source_evidence")]
    with_fix = [f for f in findings if str(f.get("suggestion") or "").strip()]
    with_correct = [f for f in findings if str(f.get("correct_value") or "").strip()]
    n = len(findings)

    return [
        prf,
        fp,
        _crit(
            "③", "来源定位",
            f"{len(anchored)}/{n} 条带原文锚点" if n else "本次 0 检出",
            f"检出 {n} 条，其中 <b>{len(located)}</b> 条带字符区间 "
            f"（可直接跳到草稿对应位置）、<b>{len(anchored)}</b> 条带原文引文或"
            f"证据片段 ID（<code>evd_*</code>，可沿证据链回溯到事实库的具体条目）。"
            f"<b>判据是查到的证据，不是「模型觉得不对」</b>。"
            + ("" if n == len(anchored) else "※ 有未带锚点的条目，已如实计入分母。"),
            "√" if n and len(anchored) == n else ("○" if not n else "◐"),
        ),
        _crit(
            "④", "修改建议的可执行性",
            f"{len(with_fix)}/{n} 条给出改法" if n else "本次 0 检出",
            f"{len(with_fix)} 条给出可直接替换的修改建议，其中 "
            f"<b>{len(with_correct)} 条同时给出正确值</b> —— "
            f"「这处错了」与「改成什么」是两件事，只有后者才可执行。"
            f"修订对照表落盘为 <code>audit/revision_table.xlsx</code>，"
            f"批注版草稿在原文中高亮问题位置。",
            "√" if n and len(with_fix) == n else ("○" if not n else "◐"),
        ),
    ]


def find_eval_run(runs_root: Path) -> tuple[str | None, dict[str, Any] | None]:
    """找出最近一次**接了黄金标注集**的 R5 运行，返回 ``(run_id, metrics)``。

    选题 5 的准确率/误报率只有在有标注集时才算得出来。演示 run 用合成草稿，
    因此页面上这三条必须从评测 run 取数并**注明来源** —— 拿别的 run 的数字
    不标来源，等于让读者把它读成本 run 的成绩。

    按目录名排序取最后一个（run_id 前缀含日期，字典序即时间序），
    比 mtime 稳定：拷来拷去的目录 mtime 会乱，名字不会。
    """
    best: tuple[str, dict[str, Any]] | None = None
    for rid in sorted(p.name for p in runs_root.glob("r5-eval*") if p.is_dir()):
        try:
            data = json.loads(
                (runs_root / rid / "audit" / "audit_summary.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            continue
        m = data.get("metrics")
        if m:
            best = (rid, m)
    return best if best else (None, None)


app = create_app()


def _app_for_reload() -> "FastAPI":
    """给 ``uvicorn --reload`` 用的应用工厂。

    为什么要工厂：uvicorn 的自动重载要求传入**导入字符串**而不是应用对象，
    否则它无法在子进程里重新导入模块。早期这里传的是 ``app`` 对象，
    于是 ``--no-reload`` 这个开关纯属装饰 —— 无论怎么传都不会重载，
    而使用者会以为"改了代码会自动生效"。现在改成显式的 ``--reload``，
    并且真的用导入字符串 + ``factory=True``。
    """
    return create_app(
        default_run=os.environ.get("DSH_WEB_RUN") or None,
        runs_root=os.environ.get("DSH_WEB_RUNS_ROOT") or None,
        r1_run=os.environ.get("DSH_WEB_R1_RUN") or None,
    )


def main() -> int:
    import sys

    ap = argparse.ArgumentParser(description="金融投研智能体 — Web 演示台")
    ap.add_argument("--host", default="127.0.0.1", help="绑定地址（默认仅本机）")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--run", default=None, help="默认展示的 run_id（默认取最新）")
    ap.add_argument(
        "--r1-run", default=None,
        help="选题 1（/facts、/extract）的默认 run_id —— 材料与选题 4/5 不同，"
             "通常是公告语料那个 run；不给则与 --run 相同",
    )
    ap.add_argument("--runs-root", default=None, help="runs 目录")
    ap.add_argument(
        "--reload", action="store_true",
        help="代码改动后自动重启（**开发用**；现场演示请勿开启，"
             "中途重启会打断讲解）",
    )
    args = ap.parse_args()

    try:
        import uvicorn
    except ImportError:
        print(
            "❌ 缺少 Web 依赖。请安装：\n    pip install fastapi uvicorn jinja2",
            file=sys.stderr,
        )
        return 1

    global app
    app = create_app(default_run=args.run, runs_root=args.runs_root, r1_run=args.r1_run)

    print("=" * 68)
    print("金融投研智能体 — Web 演示台")
    print("=" * 68)
    print(f"  地址：http://{args.host}:{args.port}")
    print(f"  runs：{args.runs_root or DEFAULT_RUNS_ROOT}")
    print(f"  默认 run：{args.run or '（最新一个）'}")
    print(f"  选题 1 run：{args.r1_run or '（同默认 run）'}")
    print(f"  自动重载：{'开（开发模式）' if args.reload else '关'}")
    print(f"  页面：/library  /extract  /facts  /valuation  /audit  /runs  /process")
    print("=" * 68)

    if args.reload:
        os.environ["DSH_WEB_RUN"] = args.run or ""
        os.environ["DSH_WEB_R1_RUN"] = args.r1_run or ""
        os.environ["DSH_WEB_RUNS_ROOT"] = str(args.runs_root or DEFAULT_RUNS_ROOT)
        # 必须是导入字符串 + factory，uvicorn 才能在子进程里重新导入
        uvicorn.run(
            "web.app:_app_for_reload", host=args.host, port=args.port,
            reload=True, factory=True, log_level="warning",
        )
    else:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
