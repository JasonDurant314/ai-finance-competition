"""用 node **真的执行**控制台页面的 JavaScript。

## 为什么需要这一条

`tools/web_smoke.py`、`tools/web_pages_check.py`、`tests/test_panel.py` 全走
in-process 渲染 —— 它们拿到的是 HTML 字符串，**不执行任何 JS**。于是「页面渲染
正确」与「页面能用」之间有一整层没人看过，而右栏恰好住在那一层。

实测撞上过：`web/static/trail.js::activeKinds` 返回的是**字符串**，而
`web/static/activity.js` 里同名的函数返回**数组**；控制台「运行」页把两者放在
同一个 `<script>` 里，`Trail.activeKinds(on).join(',')` 于是抛
`kinds.join is not a function`。**同步**异常掐掉了同一段脚本里后面的右栏 IIFE：
页眉那行永远停在 `未连接`，事件流与阶段进度也一起不动 —— 而 `/api/agent-log`
本身返回的数据完全正确。四个渲染门全绿。

## 依赖

需要 `node`。没有就 **skip**（本仓库不把它设成硬依赖：交付物的运行不需要 JS，
但**看**这一栏需要浏览器）。skip 是如实的：这一条跳过时，「页面能不能用」
确实没有被验证过。

夹具与两条设计约束见 `tests/js/panel_harness.js` 顶部。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from web.panel import create_panel  # noqa: E402

HARNESS = REPO / "tests" / "js" / "panel_harness.js"
STATIC = REPO / "web" / "static"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="本机没有 node，无法执行页面 JS —— 「页面能不能用」这一条没被验证",
)


def _write(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _journal(run_dir: Path, *records: dict[str, object]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "agent_log.jsonl").open("a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


@pytest.fixture()
def panel_app(tmp_path: Path) -> TestClient:
    """一个 run：右栏三行（一条阶段边界 + 一次模型调用 + 一次工具调用），provider 是 mock。"""
    d = tmp_path / "runs" / "js-panel"
    _write(d / "manifest.json", {
        "pipeline": "full", "status": "success", "provider": "mock",
        "started_at": "2026-10-12T08:00:00Z",
        "total_tokens": 120, "total_cost_cny": 0.01, "outputs": {},
    })
    _journal(
        d,
        {"seq": 0, "ts": "2026-10-12T08:00:00Z", "kind": "stage", "stage": "r1_judge",
         "status": "start"},
        {"seq": 1, "ts": "2026-10-12T08:00:01Z", "kind": "llm", "stage": "r1_judge",
         "role": "vision", "model": "deepseek-chat", "ok": True, "latency_ms": 900,
         "prompt_tokens": 100, "completion_tokens": 20, "cost_cny": 0.01,
         "messages": [{"role": "user", "content": "这一页的营业收入是多少"}],
         "text": "营业收入 1000000 元"},
        {"seq": 2, "ts": "2026-10-12T08:00:02Z", "kind": "tool", "stage": "r1_judge",
         "tool": "table.adjudicate", "ok": True, "latency_ms": 12,
         "args": {"table": "t1", "candidates": 3}, "result": {"picked": "合并"}},
    )
    return TestClient(create_panel(runs_root=tmp_path / "runs", default_run="js-panel"))


def _run_page(c: TestClient, html_path: Path, payload_path: Path, page: Path) -> dict:
    """把页面 + 它会请求的那几个响应交给 node 跑一遍，回收它的结论。"""
    payloads = {p: c.get(p).json() for p in ("/api/trace", "/api/status", "/api/agent-log")}
    _write(payload_path, payloads)
    r = subprocess.run(
        ["node", str(HARNESS), str(page), str(payload_path), str(STATIC)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert r.returncode == 0, f"node 退出 {r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
    assert r.stdout.strip(), f"node 没有输出\nstderr={r.stderr}"
    return json.loads(r.stdout)


def test_the_rail_actually_runs_in_a_browser(panel_app: TestClient, tmp_path: Path) -> None:
    """「运行」页：右栏必须真的活起来 —— 不是渲染出 HTML，是**跑完 JS** 后的样子。

    这一条是整段页面脚本的看门人：右栏 IIFE 与事件流 IIFE 同处一个 `<script>`，
    前面任何一个**同步**异常都会让右栏停在初始文案。所以这里断的不是「右栏有
    数据」，而是「页眉那行离开了初始值 `未连接`」—— 初始值恰好是故障的样子。
    """
    page = tmp_path / "run.html"
    page.write_text(panel_app.get("/", params={"run": "js-panel"}).text, encoding="utf-8")
    got = _run_page(panel_app, tmp_path / "run.html", tmp_path / "p.json", page)

    assert got["errors"] == [], f"页面脚本抛异常：{got['errors']}"
    assert not [m for m in got["log"] if "StubGap" in m], (
        f"夹具少实现了页面用到的 DOM 属性（不是页面的 bug）：{got['log']}")

    assert got["rail_state"] != "未连接", (
        "右栏停在初始文案 —— 页面脚本在跑到右栏之前就断了")
    assert "取活动流失败" not in (got["rail_state"] or ""), got["rail_state"]

    # 三行都真的渲染出来了：阶段边界、模型的回答、工具的返回，逐字在页面上。
    # 这正是这一栏存在的理由，所以断的是正文本身，不是「有 3 个节点」。
    assert got["activity_rows"] == 3, got
    stage_text, llm_text, tool_text = got["activity_text"]
    assert "阶段" in stage_text and "r1_judge" in stage_text, (
        f"阶段边界没渲染出来 —— 没有它，读者分不出这一段是谁在干：{stage_text!r}")
    assert "营业收入 1000000 元" in llm_text, f"模型回答没渲染出来：{llm_text!r}"
    assert "这一页的营业收入是多少" in llm_text, f"提示词没渲染出来：{llm_text!r}"
    assert "table.adjudicate" in tool_text, f"工具名没渲染出来：{tool_text!r}"
    assert "合并" in tool_text, f"工具返回值没渲染出来：{tool_text!r}"

    # 类别开关上带着各自的条数（llm 1 / tool 1 / stage 1），三个都必须默认开着
    chips = {c["kind"]: c["text"] for c in got["chips"]}
    assert set(chips) == {"llm", "tool", "stage"}, got["chips"]
    assert "1" in chips["llm"] and "1" in chips["tool"] and "1" in chips["stage"], chips

    # mock 披露（这一栏与真实 run 长得一样，不说就看不出来）
    assert got["rail_note_hidden"] is False and "mock" in (got["rail_note"] or "")

    # 事件流与阶段状态也要真的发出去 —— 它们和右栏同命（同一个 <script>）
    urls = " ".join(got["fetches"])
    assert "/api/trace?" in urls, f"事件流没发请求：{got['fetches']}"
    assert "/api/status" in urls, f"阶段状态没发请求：{got['fetches']}"
    assert "kinds=tool_call%2Cstage%2Cassertion%2Cerror" in urls, (
        f"事件的类别串不对：{got['fetches']}")
    # 事件流**从第一个字节取起**，不是从尾部哨兵取最后一页 —— 带哨兵的话
    # 一个 936 条事件的 run 只会显示最后 300 条，而页面上还写着「共 936 条」。
    assert "offset=0&limit=1000" in urls, (
        f"事件流不是从头取的，只用尾部哨兵会漏掉前面的行：{got['fetches']}")


def test_the_rail_survives_the_other_tabs(panel_app: TestClient, tmp_path: Path) -> None:
    """R1/R4/R5 页：事件流与阶段表的元素不在，脚本必须**提前返回**且不报错。

    这几页上右栏是唯一还在跑的东西（它不受 `onRunTab` 限制）。提前返回那段
    写错（比如少了 `return`）会让右栏在非运行页上读空元素而崩 —— 而 in-process
    的门看不出任何区别。
    """
    page = tmp_path / "r1.html"
    page.write_text(panel_app.get("/", params={"run": "js-panel", "tab": "r1"}).text,
                    encoding="utf-8")
    got = _run_page(panel_app, tmp_path / "r1.html", tmp_path / "p.json", page)

    assert got["errors"] == [], f"页面脚本抛异常：{got['errors']}"
    assert got["rail_state"] not in ("未连接", ""), got["rail_state"]
    assert got["activity_rows"] == 3, got
    urls = " ".join(got["fetches"])
    assert "/api/trace?" not in urls, "非运行页不该去取事件流"
