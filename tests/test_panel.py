"""控制台（``web/panel.py``）—— 一页四个标签：运行 / R1 提取 / R4 估值 / R5 核查。

这个应用与展示站（``web/app.py`` 的那九个只读页）共用底座，但**它的契约只有三条**：

    ① 可以运行      —— 传 PDF 进收件目录、点一次真的起一次流水线
    ② 查看运行过程  —— 阶段进度、事件、日志，运行中每 2 秒刷新
    ③ 查看运行结果  —— 三个选题各自的读数 + 产物链接

因此这里测的也是这三条，以及三条硬约束（页面里不许出现解释性段落、
不许出现模型推理长文、无外部 CDN）。

★ 「运行结果」分成三个标签页是 2026-10-12 明确要求的：`R1 提取 / R4 估值 /
  R5 核查` 各占一个标签，各自回答**同一个形状**的问题 —— 这块跑了哪几步
  （用时/模型调用/花费）、跑出了什么（读数）、产物在哪（链接）。所以下面凡是
  读结果的用例都**必须指明 tab**：`client.get("/")` 永远只给运行页，
  在它上面找 R4 的中值必然落空（这正是改版时红掉的那几个用例的病因）。

⚠️ 一条容易踩空的：**正在运行的 run 没有 ``manifest.json``**（它跑完才落盘），
而 ``web/artifacts.py::resolve_run`` 会把这种 run 判成「未完成」并排除掉。
面板**刻意绕过**那个助手（见 ``panel.py::_run_dir``）—— 否则「查看运行过程」
会在最需要它的时刻（正在跑）返回「没有这个 run」。``test_live_run_without_manifest``
钉的就是这条。

★ 2026-10-16：三个结果标签页的**第一块**是赛题的「重点考察」实测表
  （`_criteria.html`，数据来自 `web/app.py` 的 `criteria_r1/r4/r5`，见
  `panel.py::_criteria_views`）。它排在阶段明细之前，且**不依赖有没有产物** ——
  R4 没跑时它印一行「无产物 ○」，比一片空白说得清楚。同一批改动里，
  结果页不再印逐条事实（归 `facts/facts.json`）、不再印 7×7 敏感性网格与摆幅表
  （归热力图与龙卷风图两张交付图），那些数字每个都只留一处。
"""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web import analyze as az
from web.artifacts import group_thousands
from web.panel import _seconds_between, _stage_note, create_panel


# ==========================================================================
# 夹具：手写一份最小 run（比跑一遍流水线快得多，且能精确摆出边界情形）
# ==========================================================================


def _write(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _inbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """把 analyze 的目录常量整体搬到 tmp，返回空的收件目录。

    ★ 这几个是**模块级常量**，只搬 `INBOX` 会让 `/start` 把任务状态写回仓库里的
    `_analyze/state.json` —— 测试跑完用户的真实断点状态就被踩掉了。
    与 `tests/test_analyze.py::sandbox` 同一条理由。
    """
    inbox = tmp_path / "inbox"
    monkeypatch.setattr(az, "INBOX", inbox)
    monkeypatch.setattr(az, "INBOX_RAW", inbox / "raw")
    monkeypatch.setattr(az, "INBOX_DRAFTS", inbox / "drafts")
    monkeypatch.setattr(az, "ANALYZE_DIR", tmp_path / "_analyze")
    monkeypatch.setattr(az, "LOG_DIR", tmp_path / "_analyze" / "logs")
    monkeypatch.setattr(az, "STATE_PATH", tmp_path / "_analyze" / "state.json")
    (inbox / "raw").mkdir(parents=True, exist_ok=True)
    return inbox


def _put_pdf(inbox: Path) -> None:
    """放一份最小可读 PDF 进收件目录（`inventory` 会去数它的页数）。"""
    (inbox / "raw" / "sample.pdf").write_bytes(
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n")


@pytest.fixture
def panel_run(tmp_path: Path) -> Path:
    """一个产物齐全的 run 目录（三块结果都有）。"""
    root = tmp_path / "runs"
    d = root / "demo-panel"
    (d / "facts").mkdir(parents=True)
    (d / "valuation").mkdir(parents=True)
    (d / "audit").mkdir(parents=True)

    _write(d / "manifest.json", {
        "pipeline": "full", "status": "success", "started_at": "2026-10-12T08:00:00Z",
        "total_tokens": 1234, "total_cost_cny": 0.5, "outputs": {"a": "x"},
    })
    _write(d / "facts" / "facts.json", {
        "contract_version": "1", "evidence": [
            {"span_id": "e1", "doc_id": "doc1", "page": 7, "quote": "营业收入 100 元"},
        ],
        "facts": [
            {"fact_id": "f1", "metric": "revenue", "value": "1000000", "unit": "元",
             "period": "2025A", "evidence_span_ids": ["e1"], "confidence": 0.95},
            {"fact_id": "f2", "metric": "net_profit", "value": "250", "unit": "元",
             "period": "2025A", "evidence_span_ids": [], "confidence": 0.4,
             "needs_review": True, "review_reason": "单通道读数"},
        ],
    })
    _write(d / "facts" / "documents.json", [{"doc_id": "doc1", "pages": 7}])
    _write(d / "valuation" / "valuation.json", {
        "entity": "600000", "as_of": "2026-10-12",
        "value_range_low": "10.0", "value_range_mid": "11.0", "value_range_high": "12.0",
        "assumptions": [
            {"assumption_id": "a1", "name": "wacc", "value": "0.0622", "unit": "%",
             "source_fact_ids": [], "is_judgment": True,
             "basis": "★这段是模型写下的推理长文，控制台里不该出现★"},
        ],
        "assertions": [
            {"code": "A1", "name": "资产 = 负债 + 权益", "status": "pass"},
            {"code": "A4", "name": "FCFF 独立重算", "status": "pass"},
            {"code": "A4", "name": "FCFF 独立重算", "status": "skipped"},
        ],
        "sensitivity": {
            "x_name": "wacc", "y_name": "terminal_growth",
            "grid": [
                {"x_value": "0.1", "y_value": "0.02", "value_per_share": "9.0"},
                {"x_value": "0.095", "y_value": "0.02", "value_per_share": "11.0"},
                {"x_value": "0.1", "y_value": "0.03", "value_per_share": "13.0"},
                {"x_value": "0.095", "y_value": "0.03", "value_per_share": "15.0"},
            ],
            "tornado": [{"assumption": "wacc", "low": "9.0", "high": "15.0",
                         "swing": "6.0", "swing_pct_of_base": "54.55"}],
        },
        "comps": {
            "peers": [
                {"name": "同业甲", "ticker": "600001.SH", "market_cap": "1234567890123",
                 "pe": "15.0", "pb": "1.5", "ev_ebitda": None},
                {"name": "同业乙", "ticker": "600002.SH", "market_cap": None,
                 "pe": None, "pb": "2.0", "ev_ebitda": None},
            ],
            "median_pe": "15.0", "median_pb": "1.75",
            "implied_value_per_share_low": "10.0",
            "implied_value_per_share_mid": "11.0",
            "implied_value_per_share_high": "12.0",
        },
    })
    _write(d / "audit" / "audit.json", {
        "draft_doc_id": "draft1",
        "metrics": {"n_claims": 9, "n_skipped": 7, "nested": {"a": 1}},
        "findings": [
            {"finding_id": "E1-001", "error_type": "E1_unit", "severity": "confirmed",
             "location": {"section": "二、经营业绩", "page": None},
             "reported_value": "1万元", "correct_value": "1 元",
             "reason": "★这段也是模型写下的推理长文★"},
        ],
    })
    (d / "valuation" / "valuation_report.md").write_text(
        "# 估值报告\n\n全文在这里。\n", encoding="utf-8")
    (d / "audit" / "audit_report.md").write_text(
        "# 核查报告\n\n全文在这里。\n", encoding="utf-8")
    # ★ 一份「三个选题都跑过」的追迹。标签页上的徽标、每一步的用时/模型调用/花费
    #   全都从这一段现算，所以这里必须三个前缀都有、都要有首尾两条 stage 事件
    #   （只有 start 会算出 0 秒），并且让 llm_call 夹在某个阶段中间。
    #   最后那条 r1_judge 的 detail **刻意截断**成非法 JSON —— 追迹层真的会这么写。
    (d / "trace.jsonl").write_text(
        '{"seq": 1, "ts": "2026-10-12T08:00:00Z", "kind": "stage", "stage": "r1_ingest",'
        ' "ok": true, "extra": {"status": "start"}}\n'
        '{"seq": 2, "ts": "2026-10-12T08:00:10Z", "kind": "llm_call", "role": "extract",'
        ' "model": "deepseek-chat", "prompt_tokens": 100, "completion_tokens": 50,'
        ' "cost_cny": 0.01}\n'
        '{"seq": 3, "ts": "2026-10-12T08:00:20Z", "kind": "tool_call", "tool": "pdf.read",'
        ' "ok": true, "args_hash": "abcdef0123456789", "latency_ms": 12}\n'
        '{"seq": 4, "ts": "2026-10-12T08:00:20Z", "kind": "stage", "stage": "r1_ingest",'
        ' "ok": true, "extra": {"status": "ok", "detail": "{\\"pages\\": 7}"}}\n'
        '{"seq": 5, "ts": "2026-10-12T08:00:20Z", "kind": "stage", "stage": "r1_judge",'
        ' "ok": true, "extra": {"status": "start"}}\n'
        '{"seq": 6, "ts": "2026-10-12T08:00:50Z", "kind": "stage", "stage": "r1_judge",'
        ' "ok": true, "extra": {"status": "partial", "detail":'
        ' "{\\"vlm_calls\\": 400, \\"page_scans\\": 41, \\"scan_fie"}}\n'
        '{"seq": 7, "ts": "2026-10-12T08:01:00Z", "kind": "stage", "stage": "r4_assumptions",'
        ' "ok": true, "extra": {"status": "start"}}\n'
        '{"seq": 8, "ts": "2026-10-12T08:01:05Z", "kind": "stage", "stage": "r4_assumptions",'
        ' "ok": true, "extra": {"status": "ok", "detail": "{\\"assumptions\\": 8}"}}\n'
        '{"seq": 9, "ts": "2026-10-12T08:01:05Z", "kind": "stage", "stage": "r4_model",'
        ' "ok": true, "extra": {"status": "start"}}\n'
        '{"seq": 10, "ts": "2026-10-12T08:01:20Z", "kind": "llm_call", "role": "model",'
        ' "model": "deepseek-chat", "prompt_tokens": 200, "completion_tokens": 100,'
        ' "cost_cny": 0.02}\n'
        '{"seq": 11, "ts": "2026-10-12T08:01:35Z", "kind": "stage", "stage": "r4_model",'
        ' "ok": true, "extra": {"status": "ok"}}\n'
        '{"seq": 12, "ts": "2026-10-12T08:02:00Z", "kind": "stage", "stage": "r5_check",'
        ' "ok": true, "extra": {"status": "start"}}\n'
        '{"seq": 13, "ts": "2026-10-12T08:02:40Z", "kind": "stage", "stage": "r5_check",'
        ' "ok": true, "extra": {"status": "ok", "detail": "读完"}}\n'
        # 追迹里另有一份**不带前缀**的同名事件。2026-10-12 之前流水线模块自己
        # 写的阶段名就是这样（R5 单跑叫 `check`，模块里写的是 `audit`），所以
        # 老 run 的 trace 里真的有这种行；现在的 run 不会再有（名字只由编排层给）。
        # 收下它会让每个阶段印成两行、时长对半 —— 这是最容易写错的一处，钉住它。
        '{"seq": 14, "ts": "2026-10-12T08:02:40Z", "kind": "stage", "stage": "audit",'
        ' "ok": true, "extra": {"status": "ok"}}\n',
        encoding="utf-8")
    return root


@pytest.fixture
def client(panel_run: Path) -> TestClient:
    return TestClient(create_panel(runs_root=panel_run, default_run="demo-panel"))


# ==========================================================================
# 一、页面本身
# ==========================================================================


def test_panel_renders_four_tabs(client: TestClient) -> None:
    """四个标签都在，且运行页那四块（①②③④）齐全。"""
    r = client.get("/")
    assert r.status_code == 200
    for probe in ('tab=run', 'tab=r1', 'tab=r4', 'tab=r5',
                  "运行", "R1 提取", "R4 估值", "R5 核查"):
        assert probe in r.text, f"控制台缺了「{probe}」"
    for probe in ("①", "②", "③", "④"):
        assert probe in r.text, f"运行页缺了「{probe}」这一块"


def test_unknown_tab_falls_back_to_the_run_tab(client: TestClient) -> None:
    """`?tab=` 写错不该 500，也不该给一张空白页 —— 退回运行页。"""
    html = client.get("/", params={"tab": "../../etc"}).text
    assert "①" in html and "②" in html and "③" in html, "没退回运行页"
    assert "元/股（区间中值）" not in html, "退回的是 R4 而不是运行页"


def test_every_tab_renders(client: TestClient) -> None:
    """四个标签页各自 200 **且内容互不相同** —— 只是换个高亮不算分了标签。"""
    pages = {t: client.get("/", params={"tab": t}).text
             for t in ("run", "r1", "r4", "r5")}
    for t, html in pages.items():
        assert "R1 提取" in html, f"{t} 页没有标签条"
    assert len({pages[t] for t in pages}) == 4, "有两个标签页渲染出了同一张页面"


def test_tab_badges_say_what_is_behind_them(client: TestClient) -> None:
    """★ 不点进去也要知道那边有什么。

    徽标是这一版把「一条流水线」切成三份之后唯一的导航线索：`11/11 阶段`
    只在阶段事件齐的时候才对，`357 条事实` 只在事实库真读出来的时候才对。
    印死一个「—」最省事，那也正是这个用例要挡住的。
    """
    html = client.get("/").text
    assert "2 条事实" in html, "R1 标签没报事实条数"
    assert "11.0 元/股" in html, "R4 标签没报每股价值"
    assert "1 处问题" in html, "R5 标签没报问题处数"
    assert "/11 阶段" in html, "运行标签没报阶段进度（分母该是 pipeline.yaml 的 11）"


def test_panel_prints_the_results_not_the_reasoning(client: TestClient) -> None:
    """★ 页面里不许出现模型写下的推理长文。

    这是这一版控制台存在的理由：假设的 `basis` 与核查的 `reason` 各是几百字的
    推理段落，进了正文就会把 8 行表格撑成八屏。它们属于
    `valuation_report.md` / `annotated_draft.html`，控制台只给结论与链接。

    ★ 四个标签页都得查一遍：把结果挪进标签页之后，「这条规矩只对首页成立」
      是个很自然的失守方式。
    """
    pages = {t: client.get("/", params={"tab": t}).text
             for t in ("run", "r1", "r4", "r5")}
    for t, html in pages.items():
        assert "★这段是模型写下的推理长文" not in html, f"{t} 页混进了假设的 basis"
        assert "★这段也是模型写下的推理长文" not in html, f"{t} 页混进了核查的 reason"
        assert "basis" not in html, f"{t} 页把 basis 这个键名印出来了"
    # 但结论必须在（各自在自己的页上）
    assert "wacc" in pages["r4"] and "11.0" in pages["r4"]
    assert "E1-001" in pages["r5"]


def test_panel_has_no_external_cdn(client: TestClient) -> None:
    """断网可完整渲染 —— 受控数据赛道的硬约束。"""
    html = client.get("/").text
    assert "http://" not in html.replace("http://127.0.0.1", "")
    assert "https://" not in html
    assert 'src="/static/trail.js"' in html and 'href="/static/panel.css"' in html


def test_panel_static_assets_are_served(client: TestClient) -> None:
    for path in ("/static/panel.css", "/static/trail.js"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} 取不到，页面会没有样式/脚本"
    assert "Trail" in client.get("/static/trail.js").text


def _without_comments(text: str) -> str:
    """去掉 CSS / JS 注释再断言。

    下面那条测试断的是「某个记号不该再出现」，而**说明它为什么被删掉的注释
    里正要提到那个记号** —— 不摘注释，注释会把自己告了（这条实测踩过一次，
    第一次写这个门的时候就是这么红的）。
    """
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def test_the_rail_cannot_be_hidden(client: TestClient) -> None:
    """右栏是**常驻**的：没有收起键，也没有能把它藏起来的规则。

    实测踩过：右栏标题旁有个「收起」，它的样式是 ``body.rail-off .rail
    {display:none}`` —— 藏的是**整条右栏**，而那个按钮自己就住在 ``.rail-head``
    里。点一下，按钮跟着一起消失，页面上再没有第二个入口能把它找回来；
    ``localStorage`` 又把这个状态记了下来，刷新也没用，只能去清站点数据。
    原话：「agent活动居然还有收起键，收起完居然没法展开了」。
    第一版修法是让栏头活下来（收起变成一条 400px 的横条）；用户的裁决更干脆：
    **删掉这个键** —— 这一栏就是本页存在的理由，不是可以关掉的东西。

    所以这里断三件事，少任何一条这个坑都能回来：右栏还在、按钮不在、
    ``rail-off`` 那条路在 CSS 与页面脚本里都不在。
    """
    html = client.get("/").text
    assert '<aside class="rail" id="rail">' in html, "右栏本身必须还在"
    assert 'id="rail-toggle"' not in _without_comments(html), "收起键又回来了，它是一扇单向门"
    assert "rail-off" not in _without_comments(html), "页面脚本又在碰 rail-off 了"

    css = _without_comments(client.get("/static/panel.css").text)
    assert "rail-off" not in css, "panel.css 里又有 rail-off 规则了 —— 别再藏右栏"


# ==========================================================================
# 二、运行标签页（① 传入数据 / ② 运行）
# ==========================================================================


def test_inventory_readouts_are_on_the_page(client: TestClient) -> None:
    html = client.get("/").text
    assert "开始运行" in html and "收进收件目录" in html
    assert 'name="run_id"' in html and 'name="mock"' in html and 'name="confirm"' in html
    assert 'action="/upload"' in html, "① 传入数据那块没有上传入口"


def test_start_refuses_without_cost_confirmation(panel_run: Path,
                                                 monkeypatch: pytest.MonkeyPatch,
                                                 tmp_path: Path) -> None:
    """★ 材料齐了、只差那个勾 → 不启动。

    夹具里**必须先放一份 PDF**：``AZ.start`` 先查材料、后查费用勾选，
    空收件目录会先抛「收件目录里没有 PDF」，那条分支就永远测不到费用闸门了
    —— 测试会绿，闸门是不是还在没人知道。
    """
    inbox = _inbox(monkeypatch, tmp_path)
    _put_pdf(inbox)
    client = TestClient(create_panel(runs_root=panel_run), follow_redirects=False)
    r = client.post("/start", data={"run_id": "x-1", "confirm": ""})
    assert r.status_code == 303
    assert "err=" in r.headers["location"], "没勾费用确认就起跑了"
    assert "tab=run" in r.headers["location"], "失败后应落回运行页，而不是别的标签"


def test_start_error_with_chinese_survives_the_redirect(panel_run: Path,
                                                        monkeypatch: pytest.MonkeyPatch,
                                                        tmp_path: Path) -> None:
    """★ 失败原因里全是中文，直接拼进 Location 头会被判成非法 URL。

    收件目录为空时 `AZ.start` 抛的是「收件目录里没有 PDF：…」—— 它现在
    走 `quote()` 转义后再进查询串。跟着重定向走一遍，页面必须**原样读到那句话**。
    """
    _inbox(monkeypatch, tmp_path)
    client = TestClient(create_panel(runs_root=panel_run))
    r = client.post("/start", data={"run_id": "x-2", "confirm": "1"})
    assert r.status_code == 200, "跟随重定向后应落回面板页"
    assert "没有 PDF" in r.text, "中文失败原因在重定向中丢失了"


def test_mock_start_does_not_need_the_cost_confirmation(
    panel_run: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """离线自检不调模型，就不该要用户勾「我知道会花钱」。

    但它**必须**在面板这一层被显式放行，而不是把闸门整个拆掉 ——
    下面同时断言真实模式仍然拒绝，两条一起才说明闸门没被拆。
    """
    inbox = _inbox(monkeypatch, tmp_path)
    _put_pdf(inbox)
    client = TestClient(create_panel(runs_root=panel_run), follow_redirects=False)

    mock = client.post("/start", data={"run_id": "self-1", "mock": "1"})
    assert "err=" not in mock.headers["location"], (
        "离线自检被费用闸门挡住了：" + mock.headers["location"])
    real = client.post("/start", data={"run_id": "real-1", "confirm": ""})
    assert "err=" in real.headers["location"], "真实运行少了费用闸门"


def test_upload_only_takes_pdf_basenames(panel_run: Path,
                                         monkeypatch: pytest.MonkeyPatch,
                                         tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    monkeypatch.setattr(az, "INBOX", inbox)
    monkeypatch.setattr(az, "INBOX_RAW", inbox / "raw")
    client = TestClient(create_panel(runs_root=panel_run), follow_redirects=False)
    r = client.post("/upload", files=[
        ("files", ("a.pdf", b"%PDF-1.4 x", "application/pdf")),
        ("files", ("notes.txt", b"x", "text/plain")),
        ("files", ("../../escape.pdf", b"%PDF-1.4 y", "application/pdf")),
    ])
    assert r.status_code == 303
    assert "tab=run" in r.headers["location"] and "uploaded=2" in r.headers["location"]
    landed = sorted(p.name for p in (inbox / "raw").glob("*"))
    assert landed == ["a.pdf", "escape.pdf"], (
        "只该收 .pdf，且必须把路径剥成文件名（`../../escape.pdf` 不能写到目录外）"
    )


# ==========================================================================
# 三、运行标签页（③ 流水线状态 / ④ 事件）
# ==========================================================================


def test_stage_strip_lists_all_eleven_stages(client: TestClient) -> None:
    html = client.get("/").text
    assert html.count("data-stage=") == 11, "阶段清单必须来自 pipeline.yaml（full 是 11 个）"
    assert "r1_judge" in html and "r5_check" in html


def test_stage_table_splits_the_pipeline_into_three_tasks(client: TestClient) -> None:
    """★ 「流水线状态」按选题切得开 —— 这是标签页成立的前提。

    三个标签页各自的用时/模型调用/花费都是从 `trace.jsonl` **现算**的：断点
    （`checkpoint.json`）只记「哪些阶段完成了」，没有时长、没有调用次数、没有钱。
    切错的后果是每块都印成整条流水线 —— 页面看起来完整，读者拿到的却是同一份数。

    夹具里 r1 两个阶段 50 秒 / 1 次调用 / ¥0.01，r4 两个阶段 35 秒 / 1 次 / ¥0.02，
    r5 一个阶段 40 秒 / 0 次。三份互不相同，所以「没切开」一定会被抓到。
    """
    run_tab = client.get("/").text
    # 用 `>r1_judge</td>` 定位：`r1_judge` 在阶段条里也出现一次（那是 ② 的内容），
    # 按裸名字切会切到阶段条上，后面第一个 `</tr>` 就成了别人的行。
    row = run_tab.split(">r1_judge</td>", 1)[1].split("</tr>", 1)[0]
    assert "30.0s" in row, f"r1_judge 的用时不是 30 秒：{row[:200]}"
    assert "vlm_calls=400" in row, f"这一步的计数没印出来：{row[:200]}"

    r1 = client.get("/", params={"tab": "r1"}).text
    assert "2 个阶段" in r1 and "50.0 s" in r1
    assert "1 次模型调用" in r1 and "¥0.0100" in r1
    assert "1 个阶段有部分失败" in r1, "r1_judge 是 partial，标签页该说出来"

    r4 = client.get("/", params={"tab": "r4"}).text
    assert "2 个阶段" in r4 and "35.0 s" in r4 and "¥0.0200" in r4

    r5 = client.get("/", params={"tab": "r5"}).text
    assert "1 个阶段" in r5 and "40.0 s" in r5
    # 页面脚本里也有一句 `' 次模型调用'`，所以只能找那颗 pill 本身。
    assert '<span class="pill">0 次模型调用</span>' not in r5, \
        "r5 这一步没有调模型，不该印出调用数（那颗 pill 整个该消失）"


def test_stage_note_flattens_a_truncated_json_blob(client: TestClient) -> None:
    """★ 追迹层会**截断**过长的 `detail`，页面上拿到的是「像 JSON 但解析不了」的一段。

    R4-1 那一行因此印出过 `{"entity": "601857", "assumptions": 8, &#34;gaps&#34;:
    []…` —— 一屏转义后的花括号。规则是：解析得动就取标量，解析不动就正则抠
    前几个 `"键": 标量`，抠不到才认输。
    """
    html = client.get("/", params={"tab": "r1"}).text
    assert "vlm_calls=400 · page_scans=41" in html, "截断的 JSON 没被压成 k=v"
    assert "&#34;" not in html and "&#39;" not in html, "页面上还有转义过的花括号/引号"
    assert "&quot;" not in html

    run_tab = client.get("/").text
    assert "pages=7" in run_tab, "能解析的 JSON 也该压成 k=v，而不是原样印出来"


def test_trace_api_returns_rows_and_stats(client: TestClient) -> None:
    d = client.get("/api/trace", params={
        "run": "demo-panel", "offset": -1, "limit": 100}).json()
    assert d["error"] is None and len(d["rows"]) == 14
    assert d["stats"]["by_kind"]["tool_call"] == 1
    assert d["stats"]["by_kind"]["llm_call"] == 2
    assert d["live"] is False, "有 manifest ⇒ 已经跑完了"
    # limit 真的在限：尾巴模式只给最后几条，且仍按时间正序。
    tail = client.get("/api/trace", params={
        "run": "demo-panel", "offset": -1, "limit": 3}).json()
    assert [r["seq"] for r in tail["rows"]] == [12, 13, 14]


def test_trace_api_accepts_a_kind_filter(client: TestClient) -> None:
    """★ 带 `?kinds=` 的这条路**之前是必 500 的**（`_ALL_TRACE_KINDS` 没导入）。

    不带 kinds 时它照样工作，所以「点一下页面上那几个筛选 chip」才会炸 ——
    而那几个 chip 正是过程区的主控件。实测断言：过滤生效，且未知名字被丢掉
    而不是被当成「全都要」。
    """
    d = client.get("/api/trace", params={
        "run": "demo-panel", "kinds": "tool_call,不存在的种类"}).json()
    assert d["error"] is None
    assert [r["kind"] for r in d["rows"]] == ["tool_call"], "筛选没生效"

    only_stage = client.get("/api/trace", params={
        "run": "demo-panel", "kinds": "stage"}).json()
    assert {r["kind"] for r in only_stage["rows"]} == {"stage"}
    assert len(only_stage["rows"]) == 11, "阶段选上了，数量也该是全部阶段事件"


def test_trace_api_degrades_instead_of_500(client: TestClient) -> None:
    """未知 run、读不了的 trace 一律 200 + error + 空 rows。

    这条**绝不许变成 500**：面板的「② 过程」每 2 秒取一次，
    它一炸就是整屏空白，而演示现场最怕的正是这个。
    """
    r = client.get("/api/trace", params={"run": "no-such-run"})
    assert r.status_code == 200
    d = r.json()
    assert d["rows"] == [] and d["error"]

    r2 = client.get("/api/trace", params={"run": "demo-panel", "offset": 10 ** 9})
    assert r2.status_code == 200, "offset 超过文件大小（截断/轮转）也要活下来"


def test_live_run_without_manifest_is_readable(panel_run: Path) -> None:
    """★ 正在跑的 run **没有 manifest**，也必须能看它的过程。

    这是本面板与原九个只读页最关键的一处分歧：`resolve_run` 把没有 manifest 的
    run 判成「未完成」并排除，而运行中的 run 恰恰没有 manifest。
    照抄那个助手，「查看运行过程」就会在最需要它的时刻报「没有这个 run」。
    """
    live = panel_run / "live-run"
    live.mkdir(parents=True)
    (live / "trace.jsonl").write_text(
        '{"seq": 1, "ts": "2026-10-12T09:00:00Z", "kind": "stage",'
        ' "stage": "r1_judge", "ok": true, "extra": {"status": "start"}}\n',
        encoding="utf-8")
    (live / "checkpoint.json").write_text(json.dumps({"completed": ["r1_ingest"]}),
                                          encoding="utf-8")
    c = TestClient(create_panel(runs_root=panel_run, default_run="live-run"))

    page = c.get("/")
    assert page.status_code == 200
    assert "live-run" in page.text
    d = c.get("/api/trace", params={"run": "live-run"}).json()
    assert d["error"] is None and d["live"] is True, "没有 manifest 就是「还在写」"
    assert len(d["rows"]) == 1


def test_api_status_shape(client: TestClient) -> None:
    s = client.get("/api/status").json()
    for key in ("phase", "current", "completed", "stages", "log", "tokens", "cost_cny"):
        assert key in s, f"面板要刷的读数缺了 {key}"
    assert len(s["stages"]) == 11


def test_status_log_marks_match_the_rendered_page(
    panel_run: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """★ 同一段日志的**两条渲染路径必须印同一批字形**。

    首屏由服务端 `|marks` 渲染（把 `✅` 换成 `√`），之后每次轮询由 JS 用
    `textContent` 覆写。如果 `/api/status` 原样返回带 emoji 的日志，
    字形会在**两秒之后**悄悄换一套 —— 页面看起来正常，没人会发现。
    """
    _inbox(monkeypatch, tmp_path)
    az.LOG_DIR.mkdir(parents=True, exist_ok=True)
    (az.LOG_DIR / "demo-panel.log").write_text(
        "✅ r1_judge 完成（1041.4s）\n⚠ 5 个单元格调用失败\n", encoding="utf-8")
    monkeypatch.setattr(az, "_read_state", lambda: {
        "run_id": "demo-panel", "pid": 999999, "started_at": az._now_iso()})

    lines = TestClient(create_panel(runs_root=panel_run)).get("/api/status").json()["log"]
    joined = "\n".join(lines)
    assert "✅" not in joined and "⚠" not in joined, f"接口没做记号规范化：{joined}"
    assert "√ r1_judge 完成" in joined


# ==========================================================================
# 三·B、右栏：模型与工具的**原文**流（`/api/agent-log`）
#
# 与「③ 流水线状态」是两路数据，不是同一路的不同画法：
#   · `/api/trace`   读 `trace.jsonl` —— 事件是定死的契约，载荷只有哈希。
#   · `/api/agent-log` 读 `agent_log.jsonl` —— 提示词、回答、工具入参、返回值。
# `Tracer.tool()` 只写 `args_hash`，所以右栏的料**只能**来自后者：
# 拿 trace 画不出这一栏（这是加记录器的原因，不是加个视图的原因）。
# ==========================================================================


def _journal(run_dir: Path, *records: dict[str, object]) -> None:
    """往 run 目录追加逐步记录（`Tracer.journal` 的落盘格式）。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "agent_log.jsonl").open("a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _llm(seq: int, **extra: object) -> dict[str, object]:
    rec: dict[str, object] = {
        "seq": seq, "ts": "2026-10-12T08:00:00Z", "kind": "llm",
        "stage": "r1_judge", "role": "extract", "model": "deepseek-chat",
        "messages": [{"role": "system", "content": "你是抽取器"},
                     {"role": "user", "content": "营业收入是多少"}],
        "text": '{"revenue": "1000000"}', "prompt_tokens": 100,
        "completion_tokens": 20, "latency_ms": 800, "cost_cny": 0.01, "ok": True,
    }
    rec.update(extra)
    return rec


def _tool(seq: int, **extra: object) -> dict[str, object]:
    rec: dict[str, object] = {
        "seq": seq, "ts": "2026-10-12T08:00:01Z", "kind": "tool",
        "stage": "r1_extract_ocr", "tool": "ocr.recognize", "ok": True,
        "latency_ms": 4200, "args": {"doc": "a.pdf", "page": 7},
        "result": {"lines": 41, "engine": "rapidocr"},
    }
    rec.update(extra)
    return rec


def test_activity_returns_the_prompt_and_the_answer(panel_run: Path) -> None:
    """右栏要的三样东西一次到位：提示词、模型回答、工具返回。"""
    _journal(panel_run / "demo-panel", _llm(0), _tool(1))
    c = TestClient(create_panel(runs_root=panel_run, default_run="demo-panel"))
    d = c.get("/api/agent-log").json()

    assert d["source"] == "journal" and d["error"] is None
    kinds = [r["kind"] for r in d["rows"]]
    assert kinds == ["llm", "tool"], f"两条记录应各占一行：{kinds}"

    llm, tool = d["rows"]
    assert llm["messages"][1]["content"] == "营业收入是多少", "提示词没记下来"
    assert llm["text"] == '{"revenue": "1000000"}', "回答没记下来"
    assert llm["stage"] == "r1_judge"
    assert tool["args"] == {"doc": "a.pdf", "page": 7}, "工具入参没记下来"
    assert tool["result"] == {"lines": 41, "engine": "rapidocr"}, "工具返回值没记下来"
    assert d["stats"]["llm_calls"] == 1 and d["stats"]["tool_calls"] == 1


def test_activity_carries_the_stage_boundaries(panel_run: Path) -> None:
    """★ 阶段开始那一行要进右栏 —— 不然读者看不出「R4/R5 那段没调模型」。

    R4/R5 是纯确定性计算，只发 stage 事件；右栏读的是逐步记录，两路数据不通。
    没有边界时，中石油那份 run 的右栏里 488 行全挤在一起，看着像「整条流水线
    都在调模型」，而实际上只有 `r1_judge` 一个阶段调过。
    """
    _journal(panel_run / "demo-panel",
             {"seq": 0, "ts": "2026-10-12T08:00:00Z", "kind": "stage",
              "stage": "r1_ingest", "status": "start"},
             _llm(1), _tool(2))
    c = TestClient(create_panel(runs_root=panel_run, default_run="demo-panel"))

    d = c.get("/api/agent-log").json()
    assert [r["kind"] for r in d["rows"]] == ["stage", "llm", "tool"]
    assert d["rows"][0]["stage"] == "r1_ingest"
    assert d["stats"]["stages"] == 1, "边界行要计数，否则 chip 上那个数没来源"
    # 边界不是调用：它不该混进模型/工具的次数里。
    assert d["stats"]["llm_calls"] == 1 and d["stats"]["tool_calls"] == 1
    # 逐步记录这一路只累计 `prompt_tokens`（缓存那一路是 prompt+completion，两者
    # 现在不一致 —— 是既有行为，这条测试如实记下当前值，不替它改口径）。
    assert d["stats"]["tokens"] == 100, "边界行没有 token，别让它把用量算花"

    only_stage = c.get("/api/agent-log", params={"offset": 0, "kinds": "stage"}).json()
    assert [r["kind"] for r in only_stage["rows"]] == ["stage"]


def test_activity_tail_sentinel_takes_the_last_rows_in_order(panel_run: Path) -> None:
    """`offset=-1` 是尾部哨兵：先看最近发生了什么，且顺序是**由旧到新**。"""
    _journal(panel_run / "demo-panel", *[_llm(i) for i in range(5)])
    c = TestClient(create_panel(runs_root=panel_run, default_run="demo-panel"))
    d = c.get("/api/agent-log", params={"offset": -1, "limit": 2}).json()

    assert [r["seq"] for r in d["rows"]] == [3, 4], "尾部取的不该是最早的两条"
    assert d["next_offset"] == (panel_run / "demo-panel" / "agent_log.jsonl").stat().st_size
    assert d["eof"] is True


def test_activity_cursor_is_a_byte_offset_that_never_repeats(
    panel_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """游标必须是**字节偏移**，不是 `seq`。

    `--resume` 会把 `Tracer._seq` 从 0 重开，同一份文件里于是有两段重叠的 seq
    —— 按 seq 取会静默漏行或重复行。按字节取则新追加多少就读多少。
    """
    _journal(panel_run / "demo-panel", _llm(0))
    c = TestClient(create_panel(runs_root=panel_run, default_run="demo-panel"))
    first = c.get("/api/agent-log", params={"offset": 0}).json()
    assert [r["seq"] for r in first["rows"]] == [0]

    # 模拟 `--resume`：seq 从 0 重开，只追加，不改动已有内容
    _journal(panel_run / "demo-panel", _llm(0, text="重跑的第一句"))
    second = c.get("/api/agent-log", params={"offset": first["next_offset"]}).json()
    assert [r["text"] for r in second["rows"]] == ["重跑的第一句"], (
        "按游标续读只该拿到新增的那条，不该把第一条再读一遍"
    )
    assert [r["seq"] for r in second["rows"]] == [0], "seq 确实重开了，这不是 bug"


def test_activity_half_written_line_waits_for_the_next_round(panel_run: Path) -> None:
    """进程正在写的**半行**不能被当成一条记录。

    这是敢每 2 秒轮询一个正在被写的文件的唯一理由。
    """
    run = panel_run / "demo-panel"
    _journal(run, _llm(0))
    with (run / "agent_log.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"seq": 1, "kind": "tool", "tool": "ocr.reco')      # 写到一半

    c = TestClient(create_panel(runs_root=panel_run, default_run="demo-panel"))
    d = c.get("/api/agent-log", params={"offset": 0}).json()
    assert [r["kind"] for r in d["rows"]] == ["llm"], "半行被解析成了一条记录"
    assert d["next_offset"] < (run / "agent_log.jsonl").stat().st_size, (
        "游标不该越过那个不完整的换行"
    )


def test_activity_kind_filter_drops_the_other_half(panel_run: Path) -> None:
    """`kinds=tool` 只给工具调用 —— 右栏的两个 chip 就是这个筛选。"""
    _journal(panel_run / "demo-panel", _llm(0), _tool(1), _llm(2))
    c = TestClient(create_panel(runs_root=panel_run, default_run="demo-panel"))

    only_tool = c.get("/api/agent-log", params={"offset": 0, "kinds": "tool"}).json()
    assert [r["kind"] for r in only_tool["rows"]] == ["tool"]
    # ★ 头部那几个数是**整份文件**的，不跟着筛选变 —— 否则一筛就变成
    #   「本次运行只有 1 次模型调用」，那是在改读数。
    assert only_tool["stats"]["llm_calls"] == 2
    assert only_tool["stats"]["tool_calls"] == 1


def test_activity_falls_back_to_the_cache_and_says_so(panel_run: Path) -> None:
    """老 run 没有逐步记录 ⇒ 退到 `cache/llm.jsonl`，但**明说少了什么**。

    缓存里只有模型答了什么：`{key, response}`，key 是含 messages 的载荷哈希，
    提示词不可逆。少了一半的数据不能看起来像完整的数据。
    """
    run = panel_run / "demo-panel"
    (run / "cache").mkdir(parents=True, exist_ok=True)
    (run / "cache" / "llm.jsonl").write_text(
        json.dumps({"key": "a" * 64, "response": {
            "text": "老 run 的答案", "prompt_tokens": 30, "completion_tokens": 10,
            "finish_reason": "stop"}}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    c = TestClient(create_panel(runs_root=panel_run, default_run="demo-panel"))
    d = c.get("/api/agent-log").json()

    assert d["source"] == "cache" and d["error"] is None
    assert d["note"], "降级必须写在 note 里，页面上要显示出来"
    assert [r["src"] for r in d["rows"]] == ["cache"], "每行都要带着来源，别混在一起"
    assert d["rows"][0]["text"] == "老 run 的答案"
    assert d["rows"][0]["messages"] is None, "缓存里根本没有提示词，不能编一条出来"
    # ★ 缓存的行**没有 `kind` 字段**（它是回放缓存，不认 kind）。不给兜底的话
    #   头部会写「0 次模型调用」而下面列着几百条回答。
    assert d["stats"]["llm_calls"] == 1, "缓存文件里的行必须计成模型调用"
    assert d["stats"]["tokens"] == 40


def test_activity_degrades_instead_of_500(panel_run: Path) -> None:
    """未知 run、两种文件都没有、文件读不了 —— 一律 200 + error，绝不 500。

    右栏挂在**全部四个标签页**上：它一炸就是整页空白，而演示现场最怕这个。
    """
    c = TestClient(create_panel(runs_root=panel_run, default_run="demo-panel"))

    r = c.get("/api/agent-log", params={"run": "no-such-run"})
    assert r.status_code == 200 and r.json()["rows"] == [] and r.json()["error"]

    # demo-panel 两种文件都没有 ⇒ 说清楚为什么没有
    d = c.get("/api/agent-log").json()
    assert d["error"] and d["rows"] == [] and d["source"] == "none"
    assert "对话记录" in d["error"]

    # 游标超过文件大小（截断/轮转）也要活下来
    r2 = c.get("/api/agent-log", params={"run": "demo-panel", "offset": 10 ** 9})
    assert r2.status_code == 200


def test_a_run_that_has_not_written_yet_is_live_not_finished(panel_run: Path) -> None:
    """刚点下「开始分析」的 run：目录在、`agent_log.jsonl` 还没有。

    这时必须是 `error` + **`live: true`** —— 右栏靠 `live` 决定要不要继续轮询。
    少这一位，右栏一看到 error 就停，停在「没有对话记录」直到整页刷新，
    而它上面正好挂着用户想看的那个过程。
    """
    live = panel_run / "just-started"
    live.mkdir(parents=True)
    (live / "checkpoint.json").write_text('{"completed": []}', encoding="utf-8")
    c = TestClient(create_panel(runs_root=panel_run, default_run="just-started"))

    d = c.get("/api/agent-log").json()
    assert d["error"] and d["rows"] == []
    assert d["live"] is True, "还没落 manifest 就是「还在写」，不是「没有记录」"


def test_an_empty_journal_says_so_instead_of_leaving_a_blank_rail(panel_run: Path) -> None:
    """`agent_log.jsonl` 在但是空的：右栏要留一句话，不能只留一片空白。

    `Tracer.__init__` 一建就 append 打开这个文件（`tracer.py:202`），所以
    **每个** run 都有它 —— 包括一次模型调用都没发生的 run。实测 21 个 run 里
    `demo`、`cfg-test`、`prov-offline`、`prov-online` 四种就是这种（它们的
    `trace.jsonl` 里只有 `stage` 事件）。它们与「右栏坏了」在屏幕上长得
    一模一样，而两者的处置完全不同，所以这一句必须由后端给出。
    """
    run = panel_run / "no-calls"
    _journal(run)                       # 建出空文件，与真实 run 同形
    assert (run / "agent_log.jsonl").stat().st_size == 0
    c = TestClient(create_panel(runs_root=panel_run, default_run="no-calls"))

    d = c.get("/api/agent-log").json()
    assert d["source"] == "journal" and d["rows"] == [] and d["error"] is None
    assert "没有记录到模型或工具调用" in d["note"], (
        "空 journal 不说出来，就分不清「这个 run 没调过模型」和「右栏坏了」")

    # 反面：文件非空，只是当前筛选下一行不剩 —— 那不是「没有记录到」。
    # 两种情形都是空列表，只有文件大小能把它们分开。
    _journal(run, _llm(0))
    d2 = c.get("/api/agent-log", params={"kinds": "tool"}).json()
    assert d2["rows"] == [] and d2["note"] == "", (
        "把筛选的结果说成「没有记录到」是假话")


def test_activity_frame_always_has_the_same_shape(panel_run: Path) -> None:
    """空帧与正常帧**同形** —— 模板与 JS 都对着这份字段表渲染。

    不同形的后果很隐蔽：Jinja 把未定义属性渲染成空串、JS 读成 `undefined`，
    页面照常 200，右栏半截空白。
    """
    from web.panel import empty_activity

    keys = set(empty_activity())
    assert "provider" in keys, "空帧少了 provider：右栏的 mock 披露就靠它"
    c = TestClient(create_panel(runs_root=panel_run, default_run="demo-panel"))
    for params in ({}, {"run": "no-such-run"}, {"offset": 0}, {"kinds": "tool"}):
        d = c.get("/api/agent-log", params=params).json()
        assert keys <= set(d), f"{params} 的帧少了字段：{keys - set(d)}"
        assert keys - {"stats"} <= set(d)
        assert {"llm_calls", "tool_calls", "stages", "failed", "tokens", "cost_cny"} <= set(d["stats"])


def test_a_mock_run_says_so_in_the_rail_and_the_header(panel_run: Path) -> None:
    """`--mock` 的 run：模型回答是 mock provider 的**罐头**，必须标出来。

    右栏与真实 run 长得一模一样（结构、字段、流式追加全相同），差别只写在
    `manifest.json` 的 `provider` 上。不标的话，「模型在干什么」这一栏会把罐头
    当成模型的真话 —— 而它恰恰是给人看模型说了什么的地方。
    """
    run = panel_run / "demo-panel"
    info = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    info["provider"] = "mock"
    _write(run / "manifest.json", info)
    _journal(run, _llm(0))
    c = TestClient(create_panel(runs_root=panel_run, default_run="demo-panel"))

    d = c.get("/api/agent-log").json()
    assert d["provider"] == "mock", "接口没说这是 mock run，右栏就标不出来"
    # 探的是页眉那颗药丸，不是「离线自检」四个字 —— 运行页表单里本来就有个
    # 「离线自检（不调模型）」勾选框，按那四个字找等于什么都没测。
    assert "这次运行的 provider 是 mock" in c.get("/").text, "页眉没标出整页的数都不是真模型跑出来的"


def test_a_live_mock_run_is_labelled_before_it_finishes(panel_run: Path) -> None:
    """运行中的 `--mock` run 也要标出来 —— 右栏最在被看的正是这段时间。

    `manifest.json` 跑完才落盘，所以这一路只能读 `checkpoint.json`（每完成一个
    阶段落一次，里面也记着 provider）。只看 manifest 的话，mock 披露会在
    「正在跑、正在看」这段窗口里恰好是空的 —— 而罐头最容易被当成真话，
    恰恰是在你盯着它一行一行往外冒的时候。
    """
    live = panel_run / "live-mock"
    live.mkdir(parents=True)
    _write(live / "checkpoint.json", {"pipeline": "full", "provider": "mock",
                                      "completed": ["r1_ingest"]})
    _journal(live, _llm(0))
    c = TestClient(create_panel(runs_root=panel_run, default_run="live-mock"))

    d = c.get("/api/agent-log").json()
    assert d["live"] is True, "前提错了：这条测试要的是「还没落 manifest」的 run"
    assert d["provider"] == "mock", "运行中的 mock run 认不出来，右栏就在替罐头背书"
    assert "这次运行的 provider 是 mock" in c.get("/").text


def test_a_real_run_carries_no_mock_label(panel_run: Path) -> None:
    """反向：真实 run 不能被打上「离线自检」—— 那是另一种不实。"""
    run = panel_run / "demo-panel"
    info = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    info["provider"] = "deepseek"
    _write(run / "manifest.json", info)
    c = TestClient(create_panel(runs_root=panel_run, default_run="demo-panel"))

    assert c.get("/api/agent-log").json()["provider"] == "deepseek"
    assert "这次运行的 provider 是 mock" not in c.get("/").text


def test_rail_is_on_every_tab(client: TestClient) -> None:
    """右栏在四个标签页上都在，且脚本/样式都取得到。

    「常驻」是这一栏的定义：它是布局的第二列，不是又一个区块。切到 R5 就没了，
    那就退回成「一个看事件的地方」，而不是「一直能看见谁在干什么」。
    """
    for tab in ("run", "r1", "r4", "r5"):
        html = client.get("/", params={"tab": tab}).text
        assert 'id="activity"' in html, f"{tab} 页没有右栏容器"
        assert 'src="/static/activity.js"' in html, f"{tab} 页没挂右栏脚本"
    assert "Activity" in client.get("/static/activity.js").text
    for path in ("/static/activity.js", "/static/panel.css"):
        assert client.get(path).status_code == 200


def test_rail_renders_text_through_the_dom_not_html(client: TestClient) -> None:
    """右栏渲染的是**模型输出与工具返回值** —— 任意的、不受我们控制的文本。

    所以只能走 `textContent`。一句 `innerHTML` 就等于把模型的话当 HTML 执行。
    """
    js = client.get("/static/activity.js").text
    # 查的是**用法**（`.innerHTML` / `insertAdjacentHTML` …），不是这个词本身。
    for probe in (".innerHTML", ".outerHTML", "insertAdjacentHTML", "document.write("):
        assert probe not in js, f"右栏用了 {probe}，模型输出会被当 HTML 解析"
    assert "textContent" in js


# ==========================================================================
# 四、R1 / R4 / R5 三个结果标签页
# ==========================================================================


def test_r1_tab_reads_the_fact_counts_and_links_the_json(client: TestClient) -> None:
    """R1 标签页 = 评分点表 + 两个数 + 一个入口，逐条事实不再印。

    ★ 2026-10-16 换的口径，但**旧口径没被放松**。原先这里印全量事实行（实测
      中石油那份 356 行），规矩是「产物里有几条就印几条，不许抽样、也不许缀一句
      『全量见 facts.xlsx』」。现在一条都不印：逐条事实（指标/值/单位/期间/页码/
      置信度）归 `facts/facts.json`，页面上只留两个**现读出来**的数 —— 总条数、
      以及其中多少条自己报了「需复核」—— 外加一个入口。一条都不印，就不存在
      「印一半」这件事。
    """
    html = client.get("/", params={"tab": "r1"}).text
    assert "2 条事实" in html and "1 份材料" in html
    # ★ 需复核的条数要报**全库**的：整列有没有值，由它兜底。
    assert "1 条需复核" in html, "全库的需复核条数没上报"
    # ★ 入口**只有一个**：`facts.json`。其余三份同目录产物不列 —— 这一页给的是
    #   「结论的出处」，不是一份产物清单（并排四个链接，演示时点错哪个都卡壳）。
    assert "facts.json" in html, "没有导到全量事实库的入口"
    for extra_link in ("facts.xlsx", "evidence.html"):
        assert extra_link not in html, f"事实库的入口不止一个：{extra_link}"
    # ★ 入口的位置本身就是要求的一部分：**评分点表 → 入口 → 阶段明细**。
    #   排在阶段表和药丸后面的话，读者得先滚过一屏过程数据才够得着结论的出处。
    #   （锚在阶段块上，不锚「N 条事实」—— 那个数在标签条的徽标里也印了一遍。）
    stage_block = html.find('<table class="tight">')
    if stage_block < 0:
        stage_block = html.index("这一步没跑")
    assert (html.index('class="ledger"') < html.index('class="dl"')
            < stage_block), "facts.json 的入口没排在评分点表正下面"
    # 逐条事实表撤了 —— 连里面那些值也不该留在页面上。
    for gone in ("revenue", "net_profit", "2025A"):
        assert gone not in html, f"逐条事实表还在：{gone}"
    assert "全量见" not in html and "表内 " not in html


def test_artifact_links_always_carry_a_path(client: TestClient) -> None:
    """产物链接不许印出 `path=` 为空的空壳。

    ★ 这一条是补的一个**静默**错法：`_task_view` 早先按 `files_key = "r1"` 取 R1 的
      产物清单，取回来的是整个 r1 视图字典，模板 `{% for f in tv.files %}` 于是遍历了
      字典的四个**键名**，印出四个 `path=` 为空的 `<a></a>`。页面照常 200、字数照常、
      布局照常 —— 只有演示时点下去才发现打不开。所以这里查的是**渲染结果**：只要
      是产物链接，`path=` 后面就必须有东西。
    """
    for tab in ("r1", "r4", "r5"):
        html = client.get("/", params={"tab": tab}).text
        for href in re.findall(r'<a[^>]*href="(/artifact\?[^"]*)"', html):
            assert not href.endswith("path="), f"{tab} 页上有空产物链接：{href}"
        assert not re.search(r"<a\b[^>]*></a>", html), f"{tab} 页上有空壳链接"


def test_r4_tab_reads_the_valuation(client: TestClient) -> None:
    html = client.get("/", params={"tab": "r4"}).text
    assert "11.0" in html, "估值中值没印出来"
    assert "10.0" in html and "12.0" in html
    assert "同业甲" in html and "同业乙" in html
    assert "1,234,567,890,123" in html, "市值要按与事实库同一规则分组"
    assert "600000" in html and "2026-10-12" in html


def test_r5_tab_reads_the_findings(client: TestClient) -> None:
    html = client.get("/", params={"tab": "r5"}).text
    assert "E1-001" in html and "E1_unit" in html
    assert "1 处问题" in html
    assert "二、经营业绩" in html, "问题出在哪一节没印出来"


def test_every_finding_reaches_the_page_and_the_fact_base_is_linked(
    panel_run: Path,
) -> None:
    """核查清单**不许抽样**（有几条印几条）；事实库则是「两个数 + 一个入口」。

    ★ 早先 R1 与 R5 各自只印前 25 条、缀一句「全量见 *.xlsx」。那是错的：
      控制台是看数的地方，把数指到一个文件等于没给。抽样还有第二个坏处 ——
      前 25 条里一条需复核都没有时，那一列会整列空白，读者把「抽样里没有」
      读成「没有」（实测中石油那份 357 条里 73 条需复核，抽样命中与否全看运气）。

    ★ 2026-10-16：R1 那一半的口径换了 —— 事实库现在**一条都不印**，逐条归
      `facts/facts.json`（页面上只有总条数与需复核条数两个现读出来的数）。
      「不许指到文件」那条规矩因此只对 **R5 的核查清单**成立，而它仍然钉死：
      产物里有几条就印几条。

    条数刻意取 40：比旧上限 25 大（少了就没有说服力），又不是 25 的整数倍
    （恰好等于上限时，`>` 写成 `>=` 这种错法会蒙混过去）。
    """
    d = panel_run / "demo-panel"
    n_facts, n_findings = 40, 40
    _write(d / "facts" / "facts.json", {
        "contract_version": "1",
        "evidence": [{"span_id": "e1", "doc_id": "doc1", "page": 7, "quote": "q"}],
        "facts": [
            {"fact_id": f"f{i}", "metric": f"m{i:03d}", "value": str(i), "unit": "元",
             "period": "2025A", "evidence_span_ids": ["e1"], "confidence": 0.9}
            for i in range(n_facts)
        ],
    })
    _write(d / "audit" / "audit.json", {
        "draft_doc_id": "draft1", "metrics": {"n_claims": 9},
        "findings": [
            {"finding_id": f"E1-{i:03d}", "error_type": "E1_unit", "severity": "confirmed",
             "location": {"section": "二、经营业绩", "page": None},
             "reported_value": f"a{i}", "correct_value": f"b{i}"}
            for i in range(n_findings)
        ],
    })
    c = TestClient(create_panel(runs_root=panel_run, default_run="demo-panel"))

    r1 = c.get("/", params={"tab": "r1"}).text
    assert f"{n_facts} 条事实" in r1, "总条数没印出来"
    assert "facts.json" in r1, "没有导到全量事实库的入口"
    assert "m000" not in r1, "逐条事实表又回来了 —— 它该只活在 facts.json 里"
    assert "全量见" not in r1, "又在让人去翻表格文件"

    r5 = c.get("/", params={"tab": "r5"}).text
    missing = [i for i in range(n_findings) if f"E1-{i:03d}" not in r5]
    assert not missing, f"这些核查问题被截掉了：{missing}"
    assert f"{n_findings} 处问题" in r5
    assert "全量见" not in r5


def test_null_peer_number_is_not_rendered_as_zero(client: TestClient) -> None:
    """产物里的 `null` 是「这一项没读到」，不是 0。

    实测：中石油那份 comps 的 `ev_ebitda` / `ps` 都是 null。渲染成 0
    会把「没读到」说成「读到了，是零」—— 在一张倍数表上这是最坏的一种错。
    """
    html = client.get("/", params={"tab": "r4"}).text
    assert "同业乙" in html
    row = html.split("同业乙", 1)[1].split("</tr>", 1)[0]
    assert "0.00" not in row and "—" in row


_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8"
    "AAAAASUVORK5CYII=")


def test_r4_charts_are_shown_inline_not_offered_as_downloads(
    panel_run: Path,
) -> None:
    """R4 的图形证据要**显示在页面上**，不是四个下载链接。

    ★ 这里原先钉的是「敏感性网格的行列按数值排，不按字典序」—— 那张 7×7 表
      2026-10-16 撤了（49 格的值已经逐格印在热力图里，摆幅也在龙卷风图里），
      排序连同那段 `axis()` 一起交给出图那一侧
      （`pipelines/r4_valuation/report.py` 自己 `sorted(float)`）。
      留下来的是同一件事的另一半：这几张图得真的显示出来。

    ★ 必须查 `content-type`。`/artifact` 走的是 `FileResponse(..., filename=…)`
      —— 那条路一律带 `Content-Disposition: attachment`，浏览器会把图当附件下载，
      而 `<img>` 标签一个不少、页面照常 200：屏幕上只剩四个空框。所以
      「有 `<img>`」证明不了任何事，得把 `src` 取出来真的请求一次。
    """
    charts = panel_run / "demo-panel" / "valuation" / "charts"
    charts.mkdir(parents=True)
    (charts / "tornado.png").write_bytes(_PNG_1PX)

    c = TestClient(create_panel(runs_root=panel_run, default_run="demo-panel"))
    html = c.get("/", params={"tab": "r4"}).text
    assert "龙卷风图" in html, "交付图那一块没渲染出来"

    src = re.search(r'<img src="([^"]*tornado\.png)"', html)
    assert src, "页面上没有那张图的 <img>"
    img = c.get(src.group(1))
    assert img.status_code == 200
    assert img.headers["content-type"] == "image/png", (
        "图片类型没认出来 —— 浏览器会把它下载下来，页面上只剩空框")
    assert "attachment" not in img.headers.get("content-disposition", "")


def test_no_table_cell_is_left_blank(client: TestClient) -> None:
    """★ 模板里有一个视图模型没给的键 ⇒ 那一列**恒为空白**，而页面照常 200。

    实测就是这么来的：摆幅表的 `{{ t.swing }}` 那一列在视图模型里根本没有对应的
    键，Jinja 对未定义属性渲染空串，于是中石油那份 7 行摆幅表的「摆幅」一列一个字
    都没有 —— 不报错、不错位，只是那一列废了（那张表 2026-10-16 已撤，但同类错法
    对**任何**新表都成立，所以这条门留着）。

    这条规矩推广开：**表格里不许出现空单元格**。印「—」可以（那是「这一项没有
    读数」），什么都不印不行 —— 读者分不清那是「没有」还是「页面坏了」。
    四个标签页一起查，评分点表（`_criteria.html`）也在其中。
    """
    for t in ("run", "r1", "r4", "r5"):
        html = client.get("/", params={"tab": t}).text
        blanks = re.findall(r"<td[^>]*>\s*</td>", html)
        assert not blanks, f"{t} 页有 {len(blanks)} 个空单元格，说明有键没带过来"
    assert "1 条需复核" in client.get("/", params={"tab": "r1"}).text


def test_assertions_are_grouped_and_skipped_is_not_pass(client: TestClient) -> None:
    """断言按编号归并，且「未校验」与「通过」分开印。

    规则漏检 ≠ 规则通过。同一句名称重复印五遍（A1–A5 各出一期一条）既占地方，
    又让人看不出「哪一类没通过」。
    """
    html = client.get("/", params={"tab": "r4"}).text
    flat = html.replace("\n", " ").replace("  ", " ")
    assert "勾稽断言 2 通过" in flat and "1 未校验" in flat, "断言小计没印全"
    assert "未校验" in html
    assert html.count("FCFF 独立重算") == 1, "同一断言应按编号归并成一行"


def test_long_reports_are_linked_not_embedded(client: TestClient) -> None:
    """报告正文是**产物**，控制台只给链接。

    实测把两份报告嵌进页面会让单页 HTML 从 15 KB 涨到 45 KB，多出来的
    几乎全是模型的推理长文 —— 正是这一版要清掉的东西。
    """
    pages = {t: client.get("/", params={"tab": t}).text
             for t in ("run", "r1", "r4", "r5")}
    for t, html in pages.items():
        assert "全文在这里" not in html, f"{t} 页把报告正文嵌进来了"
    assert "valuation_report.md" in pages["r4"]
    assert "audit_report.md" in pages["r5"]


def test_task_tabs_name_the_artifacts_that_actually_exist(client: TestClient) -> None:
    """产物链接只列**磁盘上确实有**的文件，不列一张愿望清单。

    ★ 2026-10-16：R4 也收成了一个入口。所以「有文件」只是必要条件，还得多一条 ——
      链接是**挑出来的那一份**，不是把 `valuation/` 下的东西摊开列。夹具里
      `valuation.json` 是真实存在的，它照旧不给链接；`valuation_model.xlsx`
      则压根没有，那是「愿望清单」那一半，两条都要钉。
    """
    import web.panel as panel

    r4 = client.get("/", params={"tab": "r4"}).text
    assert "valuation_report.md" in r4
    assert "valuation_model.xlsx" not in r4, "夹具里没有这个文件，不该给链接"
    assert "valuation.json" not in r4, "同目录的中间件不该跟报告并排给链接"
    assert panel.TAB_LABELS["r4"] == "R4 估值"


def test_a_task_that_did_not_run_says_so(client: TestClient, panel_run: Path) -> None:
    """★ 「R4 没跑过」与「R4 跑了但没结论」是两件事。

    标签页必须把前者说出来 —— 否则点进去看到一片空白，读者会当成页面坏了，
    而真实情况是这次运行根本只跑了 R1（夹具里 `runs/r1-only`）。
    """
    only = panel_run / "r1-only"
    (only / "facts").mkdir(parents=True)
    _write(only / "manifest.json", {"pipeline": "r1_extract", "status": "success",
                                    "outputs": {"a": "x"}})
    _write(only / "facts" / "facts.json", {"contract_version": "1", "evidence": [],
                                           "facts": []})
    c = TestClient(create_panel(runs_root=panel_run, default_run="r1-only"))
    r4 = c.get("/", params={"tab": "r4"}).text
    assert "没有这一块的产物" in r4
    assert "无估值" in c.get("/").text, "徽标上也要看得出来 R4 是空的"


def test_artifact_route_serves_inline_for_text_and_download_for_binary(
    client: TestClient,
) -> None:
    """看报告该在浏览器里看，不该弹下载框；xlsx 反过来。"""
    md = client.get("/artifact", params={
        "run": "demo-panel", "path": "valuation/valuation_report.md"})
    assert md.status_code == 200
    assert md.headers["content-type"].startswith("text/plain")
    assert "attachment" not in md.headers.get("content-disposition", "")
    assert "全文在这里" in md.text

    empty = client.get("/artifact", params={"run": "demo-panel", "path": "valuation"})
    assert empty.status_code == 404


def test_artifact_route_refuses_path_traversal(client: TestClient) -> None:
    for bad in ("../../pyproject.toml", "facts/../../manifest.json", "/etc/passwd"):
        r = client.get("/artifact", params={"run": "demo-panel", "path": bad})
        assert r.status_code == 404, f"{bad} 不该被读出 run 目录"


def test_bad_run_id_is_rejected_without_touching_the_disk(panel_run: Path) -> None:
    """run 名要先过同形校验再看目录 —— 不给 `..` 之类的写法任何机会。"""
    c = TestClient(create_panel(runs_root=panel_run, default_run="../.."))
    r = c.get("/")
    assert r.status_code == 200
    assert "不合法" in r.text or "没有这个 run" in r.text


def test_page_survives_an_empty_runs_root(tmp_path: Path) -> None:
    """一个 run 都没有时页面照常 200，只是三块都空着。"""
    c = TestClient(create_panel(runs_root=tmp_path / "nothing"))
    r = c.get("/")
    assert r.status_code == 200
    assert "还没有任何运行" in r.text or "没有可读的产物" in r.text


# ==========================================================================
# 五、视图模型里的几个纯函数
# ==========================================================================


def test_group_thousands_never_rounds_or_guesses() -> None:
    assert group_thousands("183020977818") == "183,020,977,818"
    assert group_thousands("-1234567.25") == "-1,234,567.25"
    assert group_thousands("1,720.21") == "1,720.21", "原文已带分隔符就原样返回"
    assert group_thousands("—") == "—"
    assert group_thousands("") == ""


def test_seconds_between_says_unknown_rather_than_zero() -> None:
    """★ 「0 秒」与「不知道多久」在页面上必须分得开。

    前者是「这一步没花时间」，后者是「这次运行没留下这段记录」。都印成 `0.0s`
    的话，一个从没跑过的阶段会看起来像跑完了且瞬间完成。
    """
    assert _seconds_between("2026-10-12T08:00:00Z", "2026-10-12T08:00:30Z") == 30.0
    assert _seconds_between("2026-10-12T08:00:00", "2026-10-12T08:00:01") == 1.0
    for a, b in (("", "2026-10-12T08:00:00Z"), ("2026-10-12T08:00:00Z", ""),
                 (None, None), ("不是时间", "2026-10-12T08:00:00Z")):
        assert _seconds_between(a, b) is None, f"{a!r}/{b!r} 该说「不知道」而不是 0"
    # 时钟回拨（追迹是子进程写的，理论上有）不该算出负数。
    assert _seconds_between("2026-10-12T08:00:30Z", "2026-10-12T08:00:00Z") == 0.0


def test_stage_note_handles_every_shape_the_tracer_emits() -> None:
    """`detail` 实测有三种形状：空、短句、JSON 计数（可能被截断）。"""
    assert _stage_note(None) == "" and _stage_note("") == ""
    assert _stage_note("部分失败：单元格裁决调用失败 5") == "部分失败：单元格裁决调用失败 5"
    # 完整 JSON → 取标量；嵌套的数组/对象丢掉（印出来就是一片花括号）
    assert _stage_note('{"entity": "601857", "assumptions": 8, "gaps": []}') == \
        "entity=601857 · assumptions=8"
    # 截断的 JSON → 正则抠得动几个算几个
    assert _stage_note('{"vlm_calls": 400, "page_scans": 41, "scan_fie') == \
        "vlm_calls=400 · page_scans=41"
    # 整段只有一个长文本值、且连它都被截断 → 一个标量都抠不到，认输。
    # 宁可这一格空着，也不能把半截花括号印到页面上。
    assert _stage_note('{"note": "这是一段很长的自然语言') == ""
    # 超长的字符串值掐掉，免得一行把表格撑歪
    long = _stage_note('{"p": "x' + "y" * 60 + '"}')
    assert len(long) < 40 and long.endswith("…")


def test_module_has_no_stray_undefined_names() -> None:
    """`symtable` 扫一遍本模块 —— 与 `test_static_health.py` 同一条规矩，
    但这里只盯面板（新加的代码最容易漏掉某个名字）。"""
    import symtable

    src = (Path(__file__).resolve().parents[1] / "web" / "panel.py").read_text(
        encoding="utf-8")
    st = symtable.symtable(src, "panel.py", "exec")
    names = {s.get_name() for s in st.get_symbols()}
    for n in ("create_panel", "main", "_results", "_valuation_view", "_audit_view",
              "_task_stages", "_task_rollup", "_task_view", "_render_stages",
              "_stage_note", "_seconds_between", "TAB_ORDER", "_SCALAR_PAIR_RE"):
        assert n in names


# ==========================================================================
# 六、端到端：从面板传一份数据 → 开始运行 → 跑完三个任务（--mock，零成本）
# ==========================================================================


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """把 analyze 的目录常量整体搬到 tmp。

    与 `tests/test_analyze.py::sandbox` 同一条理由：这些是**模块级常量**，
    只搬 `INBOX` 会让任务状态写回仓库里的 `_analyze/state.json`。
    """
    inbox = tmp_path / "inbox"
    runs = tmp_path / "runs"
    analyze_dir = tmp_path / "_analyze"
    monkeypatch.setattr(az, "INBOX", inbox)
    monkeypatch.setattr(az, "INBOX_RAW", inbox / "raw")
    monkeypatch.setattr(az, "INBOX_DRAFTS", inbox / "drafts")
    monkeypatch.setattr(az, "ANALYZE_DIR", analyze_dir)
    monkeypatch.setattr(az, "LOG_DIR", analyze_dir / "logs")
    monkeypatch.setattr(az, "STATE_PATH", analyze_dir / "state.json")
    runs.mkdir(parents=True, exist_ok=True)
    return {"inbox": inbox, "runs": runs, "analyze": analyze_dir}


def test_the_panel_shows_the_whole_log(sandbox: dict[str, Path]) -> None:
    """日志**不截断** —— 控制台要把整份日志摊在页面上，不是「末 N 行」。

    夹具刻意写 60 行，且首行与末行各带一个独特记号：只断「行数对不对」的话，
    「取最后 25 行」与「取全部」在一个恰好 25 行的日志上分不出来。
    又：`tail_log` 的 `n=None` 与 `n=0` 必须分开 —— `lines[-0:]` 恰好也等于
    全部，但那是巧合，读代码的人只会当成 bug。
    """
    log_dir = sandbox["analyze"] / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"第 {i} 行" for i in range(1, 61)]
    (log_dir / "logdemo.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _write(sandbox["analyze"] / "state.json", {"run_id": "logdemo"})

    c = TestClient(create_panel(runs_root=sandbox["runs"]))
    html = c.get("/").text
    assert "第 1 行" in html, "日志的第一行被截掉了"
    assert "第 60 行" in html, "日志的末行没印出来"
    assert "60 行" in html, "摘要那行没报真实行数"

    assert c.get("/api/status").json()["log"] == lines, "轮询回来的日志也不是全量"


@pytest.mark.slow
def test_start_from_the_panel_runs_to_completion(sandbox: dict[str, Path],
                                                 project_root: Path) -> None:
    """① 可以运行 —— 从面板的按钮到「③ 结果」里出现读数，整条链路走通。

    用 `--mock`：零成本、不联网，但走的是同一条编排路径（同一份 argv、
    同一份阶段清单、同一个产物目录结构）。这条测试守的是**面板自己的**那一段：
    表单字段名对得上、重定向回到新 run、进度真的动、跑完结果区读得到。
    """
    import shutil

    src = project_root / "data" / "samples" / "sample_pledge_announcement.pdf"
    if not src.exists():
        pytest.skip("缺少 data/samples/ 下的合成样本")
    (sandbox["inbox"] / "raw").mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, sandbox["inbox"] / "raw" / "sample.pdf")

    client = TestClient(create_panel(runs_root=sandbox["runs"]))
    r = client.post("/start", data={"run_id": "panel-e2e", "mock": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "err=" not in r.headers["location"], r.headers["location"]
    assert "panel-e2e" in r.headers["location"], "应重定向到刚起的那个 run"

    deadline = time.time() + 600
    s = client.get("/api/status").json()
    while time.time() < deadline and s["phase"] not in ("finished", "failed"):
        time.sleep(2.0)
        s = client.get("/api/status").json()
    assert s["phase"] == "finished", (
        f"面板起的任务没结束：phase={s['phase']}，日志末尾：\n"
        + "\n".join(s["log"][-15:])
    )

    run_dir = sandbox["runs"] / "panel-e2e"
    for rel in ("manifest.json", "facts/facts.json", "valuation/valuation.json",
                "audit/audit.json", "trace.jsonl"):
        assert (run_dir / rel).exists(), f"缺少产物 {rel}"

    html = client.get("/", params={"run": "panel-e2e"}).text
    assert "panel-e2e" in html

    # ★ 三个结果标签各读各的 —— 只看首页的话，「跑完了一条流水线却有一块是空的」
    #   会完全看不出来，而那正是最该发现的失败。
    r1 = client.get("/", params={"run": "panel-e2e", "tab": "r1"}).text
    assert "条事实" in r1, "R1 页没读到事实库"
    assert "r1_ingest" in r1, "R1 页没列出这一步的阶段明细"
    r4 = client.get("/", params={"run": "panel-e2e", "tab": "r4"}).text
    assert "元/股" in r4, "跑完了但 R4 页没读出估值"
    assert "勾稽断言" in r4
    r5 = client.get("/", params={"run": "panel-e2e", "tab": "r5"}).text
    assert "处问题" in r5, "R5 页没读到核查结论"

    # 徽标上三个数都要是真读到的那三个（跑完一条 full 流水线，三块都不该是空的）。
    assert "无估值" not in html and "0 条事实" not in html
