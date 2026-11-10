"""Web 演示台测试。

覆盖两类问题：
    1. **路由可用性** —— 每个页面都能正常渲染（现场演示最怕页面 500）
    2. **数据一致性** —— 页面展示的数字必须与产物文件里的完全一致

第 2 条尤其重要：演示台若能自己"重算"或"美化"数字，
现场看到的就与评审复现的不是同一份数据了。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="需要 pip install fastapi")
pytest.importorskip("jinja2", reason="需要 pip install jinja2")

from fastapi.testclient import TestClient  # noqa: E402

from agent.runtime.loop import Plan  # noqa: E402
from web.app import (  # noqa: E402
    _locate_quote,
    _r1_consistency,
    _split_for_highlight,
    criteria_r1,
    criteria_r4,
    criteria_r5,
    render_prose,
    create_app,
    render_markdown,
    vlm_budget,
)
from web.artifacts import (  # noqa: E402
    ArtifactError,
    RunArtifacts,
    available_runs,
    load_run,
    resolve_run,
)


# ==========================================================================
# 夹具
# ==========================================================================


@pytest.fixture(scope="module")
def demo_run(project_root: Path, tmp_path_factory) -> Path:
    """跑一次最小完整流水线，作为 Web 测试的数据源。

    用 mock 模型 + 合成样本，因此无需 API Key、无需网络。
    """
    import subprocess
    import sys

    raw = project_root / "data" / "raw"
    if not any(raw.glob("*.pdf")):
        subprocess.run(
            [sys.executable, str(project_root / "tools" / "make_sample_data.py")],
            check=True, cwd=project_root,
        )

    runs_root = tmp_path_factory.mktemp("web-runs")
    r = subprocess.run(
        [
            sys.executable, "-m", "agent.cli", "run", "full",
            "--mock", "--run-id", "web-demo",
            "--runs-root", str(runs_root), "--quiet",
        ],
        cwd=project_root, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, f"准备数据失败：\n{r.stdout}\n{r.stderr}"
    return runs_root


@pytest.fixture(scope="module")
def client(demo_run: Path, tmp_path_factory, request) -> TestClient:
    """演示台客户端。**控制台的全局状态被指到空目录**，见下。

    `web/analyze.py` 的 `ANALYZE_DIR`（state.json + logs/）在仓库里是全局的，
    而 `/analyze` 会把**最新一次真实运行**的日志尾巴渲染进页面。于是
    「页面渲染出什么」成了「本机上一次跑过什么」的函数 —— 实测踩过：
    本机 `_analyze/logs/` 里留着一次真实运行的日志，里面的流水线描述带
    `**同一份文件**`，`test_no_bare_markdown_asterisks_in_rendered_pages`
    当场变红，而模板一个字都没错。

    指到空目录后，页面内容只由模板与 `runs_root` 决定 —— 这也正是全新克隆
    上的样子（`runs/` 与 `_analyze/` 都不入版本库）。不这么做，测试通过与否
    取决于谁在这台机器上跑过控制台。
    """
    from web import analyze as az
    mp = pytest.MonkeyPatch()
    scratch = tmp_path_factory.mktemp("analyze-state")
    mp.setattr(az, "ANALYZE_DIR", scratch)
    mp.setattr(az, "LOG_DIR", scratch / "logs")
    mp.setattr(az, "STATE_PATH", scratch / "state.json")
    request.addfinalizer(mp.undo)
    app = create_app(runs_root=demo_run)
    return TestClient(app)


def _jinja_env():
    """测试用的 Jinja 环境 —— **就是应用在用的那一个**。

    ★ 别在测试里自己 ``Environment(loader=...)`` 再手补 `md`/`prose`/`marks`：
    那样「测试里能编译」与「页面上能渲染」是两件事，加过滤器时得记住改 N 处。
    实测踩过 —— 见 ``web/app.py::register_filters`` 的 docstring。
    """
    return create_app().state.jinja_env


# ==========================================================================
# 产物读取层
# ==========================================================================


def test_available_runs_lists_demo(demo_run: Path) -> None:
    runs = available_runs(demo_run)
    assert any(r.run_id == "web-demo" for r in runs)


def test_resolve_run_picks_latest(demo_run: Path) -> None:
    assert resolve_run(None, demo_run) == "web-demo"


def test_resolve_run_rejects_unknown(demo_run: Path) -> None:
    with pytest.raises(ArtifactError):
        resolve_run("no-such-run", demo_run)


def test_resolve_run_explains_incomplete_run(demo_run: Path) -> None:
    """★ 「还没跑完」不能报成「找不到」。

    现场演示时后台可能正在跑一个 run，此时目录存在但没有 ``manifest.json``
    （它要等运行结束才写出）。若统一报"找不到 run"，
    会让人以为是自己路径写错了。
    """
    from web.artifacts import ArtifactError as _AE

    (demo_run / "half-done").mkdir(exist_ok=True)
    with pytest.raises(_AE) as ei:
        resolve_run("half-done", demo_run)
    msg = str(ei.value)
    assert "还没有可展示的产物" in msg, msg
    assert "找不到 run" not in msg, msg
    # 真的不存在的 run 仍报"找不到"
    with pytest.raises(_AE) as ei2:
        resolve_run("never-existed", demo_run)
    assert "找不到 run" in str(ei2.value)


def test_load_run_exposes_facts(demo_run: Path) -> None:
    run = load_run("web-demo", demo_run)
    facts = run.build_facts()
    assert facts, "事实库不应为空"
    # 契约硬闸门在 Web 层同样成立
    assert all(f.evidence for f in facts), "每条事实都必须能展示证据锚点"
    assert all(0.0 <= f.confidence <= 1.0 for f in facts)


def test_facts_match_artifact_file(demo_run: Path) -> None:
    """★ 页面展示的数字必须与产物文件**逐字一致**。

    演示台若自行重算或美化数字，现场看到的就与评审复现的不是同一份数据。
    """
    run = load_run("web-demo", demo_run)
    raw = json.loads((run.dir / "facts/facts.json").read_text(encoding="utf-8"))
    by_id = {f["fact_id"]: f for f in raw["facts"]}

    for f in run.build_facts():
        src = by_id[f.fact_id]
        assert f.value == str(src["value"])
        assert f.unit == src["unit"]
        assert f.metric == src["metric"]
        assert f.raw_text == src["raw_text"]


def test_channel_stats_are_consistent(demo_run: Path) -> None:
    run = load_run("web-demo", demo_run)
    stats = run.channel_stats()
    assert stats["scan_fields_usable"] + stats["scan_fields_rejected"] == stats["scan_fields_total"]


def test_value_display_only_inserts_separators(demo_run: Path) -> None:
    """★ 千分位是**纯显示层**：把逗号去掉必须还原成产物里的那个字符串。

    上一条测试锁的是「`value` 与产物逐字一致」。这一条锁的是它的显示形态：
    页面为了让一列 12 位数字读得出来，会插千分位 —— 但那只许**插入分隔符**，
    不许四舍五入、不许改小数位、不许换算单位。判据就是「去掉逗号能否复原」，
    对不上就是这一层偷偷动了数字。
    """
    run = load_run("web-demo", demo_run)
    facts = run.build_facts()
    assert facts, "事实库不应为空"

    for f in facts:
        shown = f.value_display
        assert shown.replace(",", "") == f.value, (
            f"{f.fact_id}: 显示值 {shown!r} 去掉千分位后与产物值 {f.value!r} 不一致")

    # 必须真的**有**一条被分过组，否则这条测试在「格式化函数恒等返回」时也会绿。
    assert any("," in f.value_display for f in facts if len(f.value) > 6), (
        "长度超过 6 位的数值一个都没被分组 —— 千分位没生效")


def test_facts_page_never_renders_an_empty_value_cell(client: TestClient) -> None:
    """★ 数值列不许出现空格子。

    这条是**回归测试**，来自一个真实事故：`facts.html` 从 `{{ f.value }}` 改成
    `{{ f.value_display }}` 后忘了重启进程，旧模块里没有这个属性 ——
    而 Jinja2 对**未定义属性默认渲染成空串**，既不报错也不告警，
    页面上就是整整一列空白数字。跑完全部页面测试也没红。
    所以这里正面断言：有事实就必须有数字，且分组生效。
    """
    r = client.get("/facts")
    assert r.status_code == 200, "首页必须能渲染"
    cells = re.findall(r'<td class="num">([^<]*)</td>', r.text)
    assert cells, "数值列一个单元格都没有 —— 选择器或模板结构变了"
    assert all(c.strip() for c in cells), f"数值列出现空格子：{cells[:5]}"
    assert any("," in c for c in cells), "数值列一条千分位都没有 —— 格式化没生效"


# ==========================================================================
# 路由：每一页都必须能渲染
# ==========================================================================


@pytest.mark.parametrize(
    "path",
    [
        "/analyze",
        "/library",
        "/extract",
        "/facts",
        "/valuation",
        "/audit",
        "/runs",
        "/process",
        "/healthz",
    ],
)
def test_page_renders(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}\n{r.text[:600]}"


def test_root_is_overview(client: TestClient) -> None:
    """``/`` 是总览首页（**不再是**跳转到 /library 的重定向）。

    2026-09 改版：导航按赛题分组后需要一个「一屏说清做了哪几题」的落地页，
    原来的 302 会让评审多跳一次。这里锁定新契约，防止无意改回重定向。
    """
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "参赛总览" in r.text
    # 三个选题都必须在这一屏上能被看到
    for topic in ("选题 1", "选题 4", "选题 5"):
        assert topic in r.text


def test_healthz_reports_runs(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["runs"] >= 1


# ==========================================================================
# 页面内容：关键信息必须真的出现
# ==========================================================================


def test_library_shows_material_names(client: TestClient) -> None:
    r = client.get("/library")
    assert "文档库" in r.text
    assert ".pdf" in r.text, "应展示材料文件名"
    assert "SHA256" in r.text, "应展示指纹以便核验来源"


def test_extract_shows_routing_reason(client: TestClient) -> None:
    """★ 展示重点 1：三层解析引擎的判定依据必须可见。

    评判标准是「为什么这一页走 OCR」有据可查，因此页面上必须出现
    路由判定理由，而不只是显示一个标签。
    """
    r = client.get("/extract")
    assert "路由决策" in r.text
    assert "有效字符" in r.text
    assert "文本层" in r.text and "OCR" in r.text


def test_extract_doc_and_page_params_work(client: TestClient, demo_run: Path) -> None:
    run = load_run("web-demo", demo_run)
    doc = run.documents[0]
    r = client.get(f"/extract?doc={doc['doc_id']}&page=1")
    assert r.status_code == 200
    assert doc["filename"] in r.text


def test_facts_shows_evidence_trace(client: TestClient) -> None:
    """★ 展示重点 2：溯源核验必须完整。

    需要同时出现：结构化字段、原文定位、置信度构成、证据锚点。
    """
    r = client.get("/facts")
    assert "溯源核验" in r.text
    assert "置信度" in r.text
    assert "证据锚点" in r.text
    assert "页面原文" in r.text


def test_facts_filter_by_layer(client: TestClient) -> None:
    r = client.get("/facts?layer=L1")
    assert r.status_code == 200
    assert "事实" in r.text


def test_facts_select_specific_fact(client: TestClient, demo_run: Path) -> None:
    """``?fact_id=`` 必须真的把溯源面板切到指定的那一条。

    ★ 这条测试此前是**假绿**的，两处叠加：
      1. 它请求的是 ``?fact=``，而路由 ``/facts`` 读的是 ``fact_id``
         （``web/app.py::facts``）—— 参数名不匹配，请求被忽略；
      2. 它断言 ``target.fact_id in r.text``，可**每一行**的「溯源」链接里
         都带着自己的 ``fact_id``（``facts.html``），所以哪怕面板选的不是它，
         这串 id 照样在页面上。
      3. 它取的还是 ``facts[0]`` —— 正是路由在选不中时回退的那一条
         （``selected = filtered[0]``），于是「选中了」与「回退了」两种情况
         渲染出**逐字节相同**的页面（实测确认）。

    现在改成：挑一条与 ``facts[0]`` 可区分的事实（指标与原文串都不同），
    断言的锚点也只取**只有 selected 才会渲染**的那两处 —— 面板标题里的指标名
    与「页面原文串」一栏。这样参数名一旦退回 ``fact=``，本测试立刻变红。
    """
    run = load_run("web-demo", demo_run)
    facts = run.build_facts()
    first = facts[0]
    target = next(
        (f for f in facts
         if f.fact_id != first.fact_id and f.raw_text and f.raw_text not in first.raw_text
         and f.metric != first.metric),
        None,
    )
    assert target is not None, "样例事实里挑不出与第一条可区分的一条，本测试将失去辨别力"

    r = client.get(f"/facts?fact_id={target.fact_id}")
    assert r.status_code == 200
    # 面板标题印的是**被选中那条**的指标名
    assert f"溯源核验 —— <code>{target.metric}</code>" in r.text
    # 「页面原文串」只在 selected 的详情栏里出现（表格列渲染的是 primary_quote）
    assert f"<dt>页面原文串</dt><dd>{target.raw_text}</dd>" in r.text
    assert target.fact_id in r.text


# ==========================================================================
# 信息架构：结论在外，明细在内
#
# 这三个页面曾经把**赛题要的交付物**排在最底部（/facts 的事实清单在第 398 行、
# 首屏全是质量汇总与图表），正好与「马上看到想要的最终结果」相反。
# 现在定了规矩：交付物与评审要点在折叠之外，比对明细一律收进 `details.fold`。
# 规矩会腐化，所以在这里钉死。
# ==========================================================================

_FOLD_OPEN = 'details class="fold"'


def _first_fold_at(html: str) -> int:
    i = html.find(_FOLD_OPEN)
    return i if i >= 0 else len(html)


@pytest.mark.parametrize(
    "path,deliverable",
    [
        ("/facts", "事实清单"),
        ("/valuation", "每股价值区间"),
        ("/audit", "问题清单"),
    ],
)
def test_deliverable_and_criteria_come_before_any_fold(
    client: TestClient, path: str, deliverable: str
) -> None:
    """★ 首屏必须先是**交付物与评审要点**，再是折叠明细。

    用「在 HTML 里的字节位置」判断，而不是「页面上有没有」——
    后者对「排在最后」这种正是要防的情况完全无感。
    """
    t = client.get(path).text
    cut = _first_fold_at(t)

    assert _FOLD_OPEN in t, f"{path} 一个折叠块都没有 —— 明细又摊回明面上了"
    # 标签用通知的原词「重点考察」，不写「评审要点」——
    # 通知全文没有「评审要点」这个词（2026-09-27 按 PDF 逐字校正）。
    assert "重点考察" in t[:cut], f"{path}: 重点考察表被折起来了"
    assert deliverable in t[:cut], (
        f"{path}: 交付物「{deliverable}」不在第一个折叠块之前 —— "
        f"评审要翻过折叠区才看得到最终结果")


def test_r1_consistency_measured_column_states_the_conflict_count() -> None:
    """★ 评审要点④ 的「实测」栏不许出现与该行数字打架的写死值，也不许
    把恒 0 值当成测量结果端上来。

    实测踩过：note 里写死「冲突 0 条」，而同一行的「实测」栏与 note 后半句
    都按 ``internal_conflicts.json`` 现算 —— 真实 run 上是 53 条。
    于是同一行同时出现「冲突 0 条」和「已标记同主体冲突 53 条」，读者只会
    得出「这页在自说自话」。

    ★ 2026-10 重构：note 那一列（「这个数是怎么来的」）整列删掉了 —— 它写的
    其实是论证而不是读数。于是那两个**必要条件**搬进了「实测」栏：
    `未被复现 N 个` 与 `（互证项恒为 0）`。规矩没变，只是换了承重的地方：
    谁渲染就守谁。note 仍随字典返回，所以这边对两个字段都验。

    写死的数字**不会随数据变化**，所以它错了也没人发现；用 demo 夹具（0 条冲突）
    更测不出来 —— 0 与 0 恰好相等。所以这里直接构造一个**有冲突**的对象来测。
    """
    class _Stub:
        build_stats = {"scale_implausible": 2, "period_contradiction": 1}

        def extraction_sources(self):
            return {"deterministic": 80, "model": 20, "total": 100}

        def cross_validation(self):
            return {"deterministic_values": 80, "model_values": 20,
                    "confirmed": 3, "confirmed_ratio": 0.15, "model_only": 17}

    conflicts = [{"metric": "total_assets"}] * 53
    row = _r1_consistency(_Stub(), facts=[{}] * 100, conflicts=conflicts)
    note, measured = row["note"], row["verdict"]      # verdict 就是「实测」栏

    assert "53" in measured, f"实测栏没报真实冲突数：{measured}"
    assert "冲突 0 条" not in measured, "写死的「冲突 0 条」又回来了 —— 与实测栏 53 条打架"
    # 页面上出现的每一个「冲突 N 条」都必须等于产物里的条数
    claimed = {int(x) for x in re.findall(r"冲突 (\d+) 条", note + measured)}
    assert claimed == {53}, f"note/实测栏报的冲突条数与产物不符：{claimed}"
    # 恒为 0 的「互证项冲突数」不得被当成测量结果端上来 —— 实测栏必须自带这句，
    # 它右边已经没有 note 那一列可以代劳了
    assert "恒为 0" in measured, (
        "「实测」栏必须点明「互证项里有几条冲突」是恒 0 值，不是测出来的")
    assert "恒为 0" in note and "不是测量结果" in note, (
        "note 里对那个恒 0 值的说明也要留着 —— 它是定义，不是论证")
    # 该报的是真正测到的量：未被复现的条数
    assert "未被复现 17 个" in measured, f"实测栏没报 model_only：{measured}"


# 通知里三个选题的「重点考察」原文，逐字从通知 PDF 抄下来。
# 通知是扫描件（无文本层、有复制保护），这几行是渲染成图后逐字读出来的；
# `_shots/pdf/p02.png`（选题 1）与 `p03.png`（选题 4、5）是那两张图。
#   · 「重点考察」是通知的用词，站点原先写「评审要点」—— 通知全文没有这个词。
#   · 三个标签在 2026-09-27 之前被「顺手写短」过：
#     ①「完整性」（漏了限定语「字段提取的」）、
#     /audit ①「准确性与覆盖率」（漏了「错误识别的」）、
#     /audit ④「修改建议可执行性」（少一个「的」）。
#     每次都是同一个动作：把长名字写短。所以这条规则要靠测试钉住，不靠记忆。
_NOTICE_LABELS = {
    "r1": ["字段提取的完整性", "格式规范性", "表格解析准确性", "结果一致性"],
    "r4": ["模型结构", "假设依据", "财务勾稽关系", "估值结果合理性", "敏感性分析完整性"],
    "r5": ["错误识别的准确性和覆盖率", "误报控制", "来源定位", "修改建议的可执行性"],
}


def test_criteria_labels_are_verbatim_from_the_notice() -> None:
    """★ 三页的考察点名称必须**照抄通知用词**，一个字都不许省。

    `_crit()` 的字段契约自己写着「照抄通知用词，便于评委逐字对照」，
    但 2026-09-27 审计发现三处没照抄（见 `_NOTICE_LABELS` 上方注释）。
    这类漂移**看不出坏**：页面上少一个「的」没有报错、没有测试会红，
    只有评委拿通知逐字比对时才会发现 ——「完整性」与「字段提取的完整性」
    甚至不是同一件事（前者会被读成结构完整性）。

    这里用一个空 run 桩调三个 criteria 函数，只取 label，不碰产物。
    """
    class _Empty:
        """什么产物都没有的 run —— 走各函数的「无产物」早退分支。

        早退分支与正常分支**各写一遍 label**，所以两边都要照抄；
        只测正常分支会漏掉一半（`r5` 就有一对重复的 ① 标签）。
        """

        # `build_stats` 是**属性**不是方法（`web/app.py` 里各处以
        # `r.build_stats.get(...)` 取值），给方法会得到 `'function' object
        # has no attribute 'get'`。
        build_stats: dict = {}

        def extraction_sources(self):
            return {}

        def cross_validation(self):
            return {}

        def table_stats(self):
            # `_r1_tables` 先读这几个键、再判 `tables` 是否为空，
            # 所以空 run 桩也得给全（给 `{}` 会 KeyError: 'cells_checked'）。
            return {"tables": [], "cells_checked": 0, "cells_agree": 0,
                    "cells_rejected": 0, "cells_errors": 0, "cells_corrected": 0,
                    "arith_checked": 0, "arith_failed_tables": 0}

        def valuation(self):
            return None

        def audit(self):
            return None

        @property
        def facts_file(self):
            # `criteria_r1` 直接对它 `.get("facts")`，给 None 会 AttributeError；
            # 给空 dict 才会走到「无事实条目」那条早退分支。
            return {}

        @property
        def conflicts(self):
            return []

        @property
        def table_adjudications(self):
            return []

        @property
        def internal_conflicts(self):
            return []

        @property
        def tables(self):
            return []

    stub = _Empty()
    got = {
        "r1": [c["label"] for c in criteria_r1(stub)],
        # 这两个还要产物参数：给 None/{} 就是为了走「无产物」的早退分支 ——
        # 那条分支把 label **又写了一遍**，所以它同样受这条规则约束。
        "r4": [c["label"] for c in criteria_r4(stub, None)],
        "r5": [c["label"] for c in criteria_r5(stub, {}, None, None, None)],
    }
    # 早退分支只会给出**前几行**（r4 无产物时只报 ①），所以不能断言全等：
    # 断言「是通知原文的前缀」—— 既管住措辞，也不强求无产物的 run 报满四条。
    for topic, labels in got.items():
        want = _NOTICE_LABELS[topic]
        assert labels == want[:len(labels)], (
            f"{topic} 的考察点名称与通知原文不一致 —— 通知 p2/p3 的原词是：\n"
            f"  期望 {want}\n  实际 {labels}\n"
            "要改这里，先回通知原文搜一遍，不要凭记忆写。")


# 上面那个桩只走「无产物」的早退分支，r4 因此只覆盖到 ①。
# 真正给评委看的是**有产物的那条分支**，所以再拿渲染出来的页面钉一遍 ——
# 页面上的标签是评委逐字比对的那一份，比函数返回值更接近现场。
@pytest.mark.parametrize("path,topic", [("/facts", "r1"), ("/valuation", "r4"),
                                        ("/audit", "r5")])
def test_rendered_pages_show_the_notice_wording_verbatim(
        client: TestClient, path: str, topic: str) -> None:
    """★ 渲染出来的考察点名称必须与通知原文逐字一致、顺序一致、条数一致。"""
    body = client.get(path).text
    shown = re.findall(r'<span class="idx">[①②③④⑤]</span>\s*([^<\n]+)', body)
    shown = [x.strip() for x in shown]
    want = _NOTICE_LABELS[topic]
    # 页面别处也会用 `.idx`（折叠块的 F1/F2 用的是另一套），所以只比前缀：
    # 考察点表在首屏，出现得比任何折叠块都早。
    assert shown[:len(want)] == want, (
        f"{path} 渲染出的考察点名称与通知不一致：\n  期望 {want}\n"
        f"  实际 {shown[:len(want)]}")


def test_overview_cards_all_keep_the_2x2_stat_grid(client: TestClient) -> None:
    """★ 首页三张选题卡的 stats **必须各 4 条**，三张卡行结构才一致。

    模板把 stats 摆成 2×2 网格（`app.css` 的 `.ovcard .stat`），列宽由容器决定，
    三张卡因此行行对齐。多塞一条就会挤成 2+2+1：最后一条独占一行、左边空一片，
    而相邻卡片的同一行仍停在第二行 —— 数字对不齐，读者会以为它们是两类东西。

    这个坑**页面照常 200、任何测试都不会红**，所以单留一条看门人。
    实测踩过：原先用 flex-wrap，「可比公司 6」被挤到了第三行。
    """
    t = client.get("/").text
    ns = re.findall(r'<div class="stat" data-n="(\d+)">', t)
    assert len(ns) == 3, f"首页应有三张选题卡，实际 {len(ns)} 张 —— 卡片结构变了"
    assert set(ns) == {"4"}, (
        f"选题卡的统计条数不再各为 4 条（实际 {ns}）—— 2×2 网格会被挤破。"
        f"要改条数，请先改 .ovcard .stat 的网格定义与 app.py 里那段注释。")


@pytest.mark.parametrize("path", ["/facts", "/valuation", "/audit", "/extract"])
def test_every_fold_summary_states_a_conclusion(client: TestClient, path: str) -> None:
    """★ 折叠的是**明细**，不是结论。

    `.fold` 的约定是每个摘要行自带一句话结论（数字或判断），
    这样折起来丢的是细节、不是信息。一个只有标题、没有结论的摘要行
    等于把内容藏了 —— 那就退回到「内容都在但看不见」的老问题。
    """
    t = client.get(path).text
    # `[^>]*` 不能省：有些折叠块默认展开（`<details class="fold" open>`），
    # 写死 `>` 会把它们悄悄漏掉 —— 而漏掉的恰恰是最该检查的那几个。
    blocks = re.findall(r'<details class="fold"[^>]*>\s*<summary>(.*?)</summary>',
                        t, flags=re.S)
    assert blocks, f"{path} 没有可检查的折叠块"
    for raw in blocks:
        idx = re.search(r'class="idx">([^<]*)<', raw)
        text = re.sub(r"<[^>]+>", " ", raw)
        text = " ".join(text.split())
        assert idx, f"{path}: 折叠块缺少编号"
        # 去掉编号与标题后，必须还剩下一段**带内容**的结论，
        # 而不是只剩标题本身（那说明 fsum 是空的或被删了）。
        assert 'class="fsum"' in raw, f"{path} {idx.group(1)}: 摘要行没有结论（fsum）"
        body = re.sub(r"<[^>]+>", " ", raw.split('class="fsum"', 1)[1])
        body = " ".join(body.split())
        assert len(body) >= 8, f"{path} {idx.group(1)}: 结论过短或为空：{body!r}"


def test_valuation_shows_assertions_and_charts(client: TestClient) -> None:
    r = client.get("/valuation")
    assert r.status_code == 200
    assert "勾稽断言" in r.text
    assert "敏感性" in r.text
    assert "事实" in r.text and "观点" in r.text, "必须区分事实与观点"


def test_audit_page_shows_findings_or_empty_state(client: TestClient) -> None:
    r = client.get("/audit")
    assert r.status_code == 200
    assert "核查" in r.text


def test_runs_page_shows_reproduce_commands(client: TestClient) -> None:
    """★ 可复现性的引导必须在页面上可见 —— 评审据此自己验证。"""
    r = client.get("/runs")
    assert "replay" in r.text
    assert "--strict" in r.text
    assert "verify" in r.text
    assert "manifest.json" in r.text


def test_runs_page_reports_file_access_from_the_runs_own_trace(
    client: TestClient, demo_run: Path
) -> None:
    """★ 「文件访问」一格只许报**本 run 的实测读数**，正反两个方向都锁。

    这一页先后写过两个极端，都被实测证伪：

    1. 原先写「我们由框架层统一记录，业务代码不需要关心 —— 这样不会因为某个
       模块忘了写日志而出现追溯断点」，而**同一页上方那张卡片写着 0**：
       ``FileGuard`` 只有 ``tests/test_harness.py`` 在调用，``pipelines/``
       零处 import，真实运行的 ``file_accesses`` 恒为 0。
    2. 反过来改成「未接入编排层、计数恒为 0」—— 2026-10-17 接入编排层之后，
       这句话对**新跑出来的 run** 同样是假的（实测一次 mock 全流程 71 条）。

    所以判据只能是本 run 自己的 ``trace.jsonl``：有几条报几条，0 就说 0。
    这条测试同时盯着「别把没记录的 run 说成记录了」和「别把记录了的 run
    说成没记录」。
    """
    body = client.get("/runs").text

    assert "不会因为某个模块忘了写日志而出现追溯断点" not in body, "旧的虚假声明又回来了"
    assert "未接入编排层" not in body, "已经接进编排层了，不能再写「未接入」"
    # 四类事件要逐类交代，读者才能分辨「没查」与「没做」
    for kind in ("工具调用", "计算过程", "结果生成", "文件访问"):
        assert kind in body, f"四类事件的交代里缺了「{kind}」"

    # 页面上那个数必须等于 trace 里的条数 —— 不是常量，不是别处的数
    trace = demo_run / "web-demo" / "trace.jsonl"
    n = sum(
        1 for line in trace.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line)["kind"] == "file_access"
    )
    assert n > 0, (
        "接入之后，一次真实运行的 file_access 仍是 0 —— 说明流水线又绕开了 "
        "ctx.guard()（见 pipelines/r1_extract/common.py）"
    )
    card = re.search(
        r'<div class="card[^"]*">\s*<div class="k">文件访问</div><div class="v">(\d+)</div>',
        body,
    )
    assert card, "页面上没有「文件访问」那张卡片"
    assert int(card.group(1)) == n, (
        f"页面报 {card.group(1)} 条，trace.jsonl 里是 {n} 条 —— 页面必须报本 run 的读数"
    )
    assert "早于接入编排层" not in body, "这个 run 是有记录的，不该套用「早于接入」那套说辞"


def test_runs_page_shows_exactly_one_output_count(client: TestClient) -> None:
    """★ 同页只许有**一个**产物口径。

    页面上曾同时出现 670（磁盘扫描）与 667（manifest 登记），于是底下要写一整段
    「差额是定义差异，不是遗漏」替页面自己辩护 —— 那段话的主语是网页，不是数据。
    改法是让差额**不存在**：只留读者眼前这一个口径（这个目录下有多少文件），
    不再并列第二个数，也就不需要任何解释。这条锁住「解释不许回来」。
    """
    body = client.get("/runs").text
    assert "定义差异" not in body, "替页面辩护的那段话又回来了"
    assert "磁盘扫描" not in body, "又并列了第二个产物口径（磁盘扫描 vs manifest 登记）"


def test_runs_page_renders_every_output_row(demo_run: Path) -> None:
    """★ 标题说几个，表里就渲染几行 —— 不静默截断。

    ``outputs[:80]`` 配上一个几百的标题，读者会以为表里就是全部；补一句
    「只列前 80 行」虽然诚实，但那笔说明本来就不必欠：清单装在一个 420px 的
    滚动盒里，全渲染不占版面。差额与截断都不存在，于是两者都不需要解释。
    """
    c = TestClient(create_app(runs_root=demo_run))
    rid = resolve_run(None, demo_run)
    outputs = load_run(rid, demo_run).outputs()
    body = c.get(f"/runs?run={rid}").text

    assert f"本次运行落盘的文件（{len(outputs)} 个）" in body, (
        "标题里的产物数不是清单自己那一口径"
    )
    m = re.search(r"本次运行落盘的文件（\d+ 个）</h2>(.*?)</table>", body, re.S)
    assert m, "找不到产物清单表"
    # `<thead><tr>` 是表头，其余每个 <tr> 对应一个产物。
    rows = len(re.findall(r"<tr", m.group(1))) - 1
    assert rows == len(outputs), (
        f"标题写 {len(outputs)} 个，表里渲染了 {rows} 行 —— 有静默截断"
    )
    assert "只列前 80 行" not in body, "截断说明还在，但已经没有截断了"


def test_process_page_has_upload_form(client: TestClient) -> None:
    r = client.get("/process")
    assert r.status_code == 200
    assert "multipart/form-data" in r.text
    assert 'type="file"' in r.text


# ==========================================================================
# 页面图片
# ==========================================================================


def test_page_image_endpoint_404_for_missing(demo_run: Path) -> None:
    app = create_app(runs_root=demo_run)
    c = TestClient(app)
    r = c.get("/page-image/doc_nonexistent/1")
    assert r.status_code == 404


# ==========================================================================
# Markdown 渲染
# ==========================================================================


def test_markdown_headings_and_bold() -> None:
    html = render_markdown("# 标题\n\n正文 **加粗** 与 `代码`。")
    assert "<h1>标题</h1>" in html
    assert "<strong>加粗</strong>" in html
    assert "<code>代码</code>" in html


def test_markdown_table() -> None:
    md = "| 项目 | 数值 |\n|---|---|\n| 收入 | 100 |"
    html = render_markdown(md)
    assert "<table>" in html
    assert "<th>项目</th>" in html
    assert "<td>100</td>" in html


def test_markdown_escapes_html() -> None:
    """★ 报告内容含用户材料，必须转义 —— 否则是 XSS 漏洞。"""
    html = render_markdown('正常 <script>alert("x")</script> 文本')
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_markdown_escapes_inside_table() -> None:
    md = "| a | b |\n|---|---|\n| <img src=x onerror=1> | 2 |"
    html = render_markdown(md)
    assert "<img" not in html
    assert "&lt;img" in html


def test_markdown_handles_empty() -> None:
    assert render_markdown("") == ""


def test_markdown_truncates_huge_input() -> None:
    html = render_markdown("x" * 100_000, max_chars=1000)
    assert len(html) < 5000


# ==========================================================================
# 原文高亮定位
# ==========================================================================


PAGE = """证券代码：430017
证券简称：星昊医药
    本次质押股份数量：12,004.50 万股
    占公司总股本比例：12.40%"""


def test_locate_quote_exact() -> None:
    loc = _locate_quote(PAGE, "本次质押股份数量：12,004.50 万股")
    assert loc is not None
    assert loc["match"] == "exact"
    assert PAGE[loc["start"]:loc["end"]] == "本次质押股份数量：12,004.50 万股"


def test_locate_quote_whitespace_insensitive() -> None:
    """原文有换行、quote 没有 —— 仍应能定位。"""
    loc = _locate_quote(PAGE, "星昊医药本次质押股份数量")
    assert loc is not None
    assert loc["match"] in ("exact", "whitespace_insensitive", "fuzzy")


def test_locate_quote_returns_none_for_missing() -> None:
    assert _locate_quote(PAGE, "完全不存在的文本片段甲乙丙丁") is None


def test_locate_quote_empty_inputs() -> None:
    assert _locate_quote("", "x") is None
    assert _locate_quote(PAGE, "") is None


def test_split_for_highlight_produces_three_parts() -> None:
    """★ 高亮必须落在**原文**上。

    早期实现直接用归一化文本的匹配下标去切原文，会截出错位内容
    （甚至切到半个汉字）。因此这里断言三段拼接后等于原文，
    且高亮段与 quote 完全一致。
    """
    quote = "本次质押股份数量：12,004.50 万股"
    loc = _locate_quote(PAGE, quote)
    parts = _split_for_highlight(PAGE, loc)
    assert parts["before"] + parts["mark"] + parts["after"] == PAGE
    assert parts["mark"] == quote


def test_split_for_highlight_without_location() -> None:
    parts = _split_for_highlight(PAGE, None)
    assert parts["before"] == PAGE
    assert parts["mark"] == ""


def test_split_for_highlight_clamps_out_of_range() -> None:
    parts = _split_for_highlight("abc", {"start": -5, "end": 999})
    assert parts["before"] + parts["mark"] + parts["after"] == "abc"


# ==========================================================================
# ★ 表格视图模型（结构 + 单元格级裁决）
# ==========================================================================


def _fake_run_with_tables(root: Path, tables: list[dict], adjs: list[dict]) -> object:
    """造一个只含表格产物的最小 run 目录。"""
    run_dir = root / "tbl-run"
    (run_dir / "channels").mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(
        json.dumps({"pipeline": "r1_extract", "status": "success"}), encoding="utf-8"
    )
    (run_dir / "channels" / "tables.json").write_text(
        json.dumps(tables, ensure_ascii=False), encoding="utf-8"
    )
    (run_dir / "channels" / "table_adjudications.json").write_text(
        json.dumps(adjs, ensure_ascii=False), encoding="utf-8"
    )
    return load_run("tbl-run", root)


def _table_rec(index: int = 0, grid: list[list[str]] | None = None,
               arithmetic: dict | None = None) -> dict:
    grid = grid or [["项目", "金额"], ["A", "100"], ["合计", "100"]]
    return {
        "doc_id": "doc_x", "page": 1, "table_index": index,
        "method": "rule_line_grid", "has_rules": True,
        "n_rows": len(grid), "n_cols": len(grid[0]), "grid": grid,
        "cells": [], "header": grid[0],
        "arithmetic": arithmetic or {"ok": True, "checked": 1,
                                     "checked_rows": 1, "checked_cols": 0,
                                     "failure_count": 0, "failures": []},
    }


def _adj_rec(index: int = 0, *, verdict: str = "normalized",
             original: str = "583,96\n5.66", new: str = "583,965.66",
             grid_after: list[list[str]] | None = None) -> dict:
    return {
        "doc_id": "doc_x", "page": 1, "table_index": index, "trigger": "no_total_line",
        "report": {
            "cells_checked": 1, "agree": 0, "normalized": 1, "corrected": 0,
            "unreadable": 0, "rejected": 0, "errors": 0, "improved": False,
            "adjudications": [{
                "row": 1, "col": 1, "original": original, "vlm_text": new,
                "verdict": verdict, "confidence": 0.92, "quote": new, "note": "",
            }],
        },
        "grid_after": grid_after or [["项目", "金额"], ["A", new], ["合计", "100"]],
    }


def test_table_view_shows_post_adjudication_grid(tmp_path: Path) -> None:
    """★ 页面必须显示**裁决之后**的网格 —— 那才是下游真正消费的内容。

    显示裁决前的版本会让人以为修正没有生效；而只显示裁决后又不给出
    "改了什么"，则无从核对。因此网格用改后版本，明细里保留改前值。
    """
    run = _fake_run_with_tables(tmp_path, [_table_rec()], [_adj_rec()])
    views = run.table_views("doc_x", 1)
    assert len(views) == 1
    v = views[0]
    assert v.rows[1][1].text == "583,965.66", "应显示裁决后的值"
    assert v.rows[1][1].verdict == "normalized"
    assert v.rows[1][1].original == "583,96\n5.66"
    assert v.rows[1][1].changed
    assert v.rows[1][1].css == "cell-norm"


def test_table_view_keeps_each_table_of_a_page(tmp_path: Path) -> None:
    """★ 一页多表时，每张表的裁决都必须各自可见。

    早期 ``adjudication_of`` 只返回该页第一条记录 —— 第二张表的裁决结果
    会在页面上**凭空消失**，而且不报任何错。
    """
    run = _fake_run_with_tables(
        tmp_path,
        [_table_rec(0), _table_rec(1, grid=[["项目", "金额"], ["B", "200"], ["合计", "200"]])],
        [
            _adj_rec(0, new="100.00", grid_after=[["项目", "金额"], ["A", "100.00"], ["合计", "100"]]),
            _adj_rec(1, new="200.00", grid_after=[["项目", "金额"], ["B", "200.00"], ["合计", "200"]]),
        ],
    )
    views = run.table_views("doc_x", 1)
    assert [v.index for v in views] == [0, 1]
    assert views[1].rows[1][1].text == "200.00", "第二张表的裁决没有生效"
    assert views[1].trigger == "no_total_line"


def test_table_view_reports_arithmetic_three_states(tmp_path: Path) -> None:
    """★ 算术校验的三种状态必须**如实**区分。

    ``arith.ok`` 对"没有合计线所以没查"的表也是真 ——
    页面若直接显示"算术自洽"，就等于把"没查过"说成"查过且没问题"。
    """
    run = _fake_run_with_tables(
        tmp_path,
        [
            _table_rec(0, arithmetic={"ok": True, "checked": 0, "checked_rows": 0,
                                      "checked_cols": 0, "failure_count": 0, "failures": []}),
            _table_rec(1, arithmetic={"ok": True, "checked": 2, "checked_rows": 0,
                                      "checked_cols": 2, "failure_count": 0, "failures": []}),
            _table_rec(2, arithmetic={"ok": False, "checked": 1, "checked_rows": 1,
                                      "checked_cols": 0, "failure_count": 1,
                                      "failures": [{"kind": "col_sum", "row": 2, "col": 1,
                                                    "label": "合计", "expected": "999",
                                                    "computed": "300", "diff": "-699"}]}),
        ],
        [],
    )
    a, b, c = run.table_views("doc_x", 1)
    assert "无从校验" in a.arith_label and a.arith_css == "dim"
    assert "全部相符" in b.arith_label and b.arith_css == "ok"
    assert "1 处算术不平" in c.arith_label and c.arith_css == "bad"
    assert c.arith_failures[0]["expected"] == "999"


def test_table_view_tolerates_legacy_arithmetic_record(tmp_path: Path) -> None:
    """旧 run 的 ``arithmetic`` 里没有 ``checked`` 字段，不能显示成「无从校验」。

    演示台必须能打开历史产物 —— 否则一旦改过产物结构，
    之前跑好的证据就全部打不开了。
    """
    run = _fake_run_with_tables(
        tmp_path,
        [_table_rec(0, arithmetic={"ok": False, "checked_rows": 0, "checked_cols": 2,
                                   "failure_count": 2, "failures": [{"kind": "col_sum"}]})],
        [],
    )
    v = run.table_views("doc_x", 1)[0]
    assert v.arith_checked == 2, "应从 checked_rows/checked_cols 反推"
    assert "无从校验" not in v.arith_label


def test_table_stats_aggregate_across_run(tmp_path: Path) -> None:
    run = _fake_run_with_tables(tmp_path, [_table_rec(0)], [_adj_rec()])
    s = run.table_stats()
    assert s["tables"] == 1
    assert s["tables_adjudicated"] == 1
    assert s["cells_checked"] == 1
    assert s["cells_normalized"] == 1
    assert s["cells_corrected"] == 0


def test_cell_crop_prefers_doc_scoped_path(tmp_path: Path) -> None:
    run = _fake_run_with_tables(tmp_path, [_table_rec(0)], [_adj_rec()])
    crops = run.dir / "pages" / "cells"
    (crops / "doc_x").mkdir(parents=True)
    (crops / "doc_x" / "cell_p1_r1c1.png").write_bytes(b"x")
    assert run.cell_crop("doc_x", 1, 1, 1) is not None


def test_cell_crop_refuses_ambiguous_flat_fallback(tmp_path: Path) -> None:
    """★ 平铺裁图只有在**无歧义**时才可回退使用。

    早期产物把裁图平铺在 ``cells/`` 下（文件名不含文档），不同文档的同页同格
    会互相覆盖。若该页有多个文档的裁决记录，平铺文件就可能属于别的文档 ——
    拿它充当证据比不给图更糟。
    """
    adjs = [_adj_rec(0), {**_adj_rec(0), "doc_id": "doc_y"}]
    run = _fake_run_with_tables(tmp_path, [_table_rec(0)], adjs)
    flat = run.dir / "pages" / "cells"
    flat.mkdir(parents=True)
    (flat / "cell_p1_r1c1.png").write_bytes(b"x")

    assert run.cell_crop("doc_x", 1, 1, 1) is None, "有歧义时必须拒绝"

    # 只有单个文档时，平铺回退是可用的（历史产物仍能打开）
    run2 = _fake_run_with_tables(tmp_path / "b", [_table_rec(0)], [_adj_rec(0)])
    flat2 = run2.dir / "pages" / "cells"
    flat2.mkdir(parents=True)
    (flat2 / "cell_p1_r1c1.png").write_bytes(b"x")
    assert run2.cell_crop("doc_x", 1, 1, 1) is not None


def test_extract_page_renders_table_section(client: TestClient) -> None:
    """抽取工作台必须始终包含表格区块（即使本页没有表格）。"""
    r = client.get("/extract")
    assert r.status_code == 200
    assert "表格结构重建与单元格级视觉裁决" in r.text
    assert "算术校验的如实说明" in r.text


def test_extraction_sources_are_counted(demo_run: Path) -> None:
    """★ 抽取路径构成必须可核实：确定性 vs 模型。

    这个比例是"确定性优先"是否真的落地了的直接证据 ——
    真实材料上曾经 73% 的事实来自 VLM 整页扫描，而它读的正是
    已经重建好的表格。页面上的数字不能与产物对不上。
    """
    run = load_run("web-demo", demo_run)
    facts = run.build_facts()
    s = run.extraction_sources()

    assert s["total"] == len(facts), "总数必须与事实库一致"
    assert s["deterministic"] + s["model"] == s["total"]
    assert sum(s["by_extractor"].values()) == s["total"]
    assert 0.0 <= s["deterministic_ratio"] <= 1.0


def test_cross_validation_counts_by_value_not_by_pair(demo_run: Path) -> None:
    """★ 双路互证必须**逐取值**比对，不能按 (主体,指标) 打包。

    同一指标天然有多个取值（多行/多期）。若把每个 (主体,指标) 的所有取值
    打包成一个集合再比，只要有一个对上就算"整组一致"，
    会得出"要么全对要么全错"的假结论 —— 实测踩过这个坑。
    """
    run = load_run("web-demo", demo_run)
    facts = run.build_facts()
    c = run.cross_validation()

    det = {(f.entity, f.metric, str(f.value)) for f in facts
           if f.extractor_name in ("table_grid", "rule_engine")}
    mod = {(f.entity, f.metric, str(f.value)) for f in facts
           if f.extractor_name.startswith("vlm")}

    assert c["deterministic_values"] == len(det)
    assert c["model_values"] == len(mod)
    assert c["confirmed"] == len(det & mod)
    assert c["model_only"] == len(mod - det)
    assert 0.0 <= c["confirmed_ratio"] <= 1.0


def test_fact_extractor_labels_are_honest(demo_run: Path) -> None:
    """确定性抽取器与模型抽取器必须能被区分出来。"""
    run = load_run("web-demo", demo_run)
    for f in run.build_facts():
        if f.extractor_name in ("table_grid", "rule_engine"):
            assert f.extractor_deterministic
            assert "确定性" in f.extractor_label
        else:
            assert not f.extractor_deterministic


def test_reattribution_artifact_is_exposed(tmp_path: Path) -> None:
    """主体归属纠正明细必须能被页面读到（改了就要留痕）。"""
    run_dir = tmp_path / "reattr-run"
    (run_dir / "facts").mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        json.dumps({"pipeline": "r1_extract", "status": "success"}), encoding="utf-8"
    )
    (run_dir / "facts" / "facts.json").write_text(
        json.dumps({"facts": []}), encoding="utf-8"
    )
    (run_dir / "facts" / "entity_reattribution.json").write_text(
        json.dumps([{"page": 7, "row": 3, "col": 8, "metric": "revenue",
                     "raw_text": "419,815.97万元", "from": "118034",
                     "to": "上饶晶科能源智造有限公司"}], ensure_ascii=False),
        encoding="utf-8",
    )
    run = load_run("reattr-run", tmp_path)
    detail = run.entity_reattribution()
    assert len(detail) == 1
    assert detail[0]["to"] == "上饶晶科能源智造有限公司"
    assert run.extraction_sources()["reattributed"] == 1


def test_facts_page_shows_extraction_sources(client: TestClient) -> None:
    """F3 面板要有「抽取路径构成」这张表与它的确定性占比读数。

    ★ 探针从「让模型去做只有它才能做的事」换成标签原文：前者是一句方法论口号，
    与页面上的数字无关，去 AI 味时被删。探针要钉的是**这一屏展示了什么**，
    所以换成同一屏上真正承重的标签 `确定性优先`，并把「确定性」收紧成
    「确定性占」，免得这三个字在页面任何一处偶然出现都算通过。

    ★ 这里**不**钉 `双路互证`：那一块要 ``crosscheck.model_values > 0`` 才渲染，
    而本夹具是 ``--mock`` 跑出来的 —— 实测模型路径事实为 0，该块在本夹具下
    永不出现，钉它等于钉了个寂寞（先前那条断言就是这么红的）。该标签由
    ``tools/web_pages_check.py`` 在真实 run 上守。
    """
    r = client.get("/facts")
    assert r.status_code == 200
    assert "抽取路径构成" in r.text
    assert "确定性优先" in r.text
    assert "确定性占" in r.text


def test_facts_page_flags_zero_model_facts(client: TestClient) -> None:
    """★ 模型路径为 0 时必须给出**原因**，不能被读成"模型不需要"。

    实测踩到：离线复跑时 VLM 整页扫描全部未命中缓存而失败，
    页面上于是显示"确定性占比 100% / 降到 0%" ——
    数字没错，但叙述把"这次运行没有模型读数"说成了"模型贡献降到 0"，
    是典型的**看起来正常、意思却错了**。
    """
    r = client.get("/facts?run=web-demo")
    assert r.status_code == 200
    if "模型路径事实为 0" in r.text:
        assert "离线复跑" in r.text, "必须说明原因，而不是只给一个 0"
        assert "不能读成" in r.text
    # ★ 这里原先还断言 `"曾实测到" in r.text` —— 那条钉的是一个**跨 run 借来的
    #   历史数字**（「真实材料上曾实测到 73% 的事实来自 VLM 整页扫描」），
    #   它贴在**本 run** 的占比下面，读者只会当成本 run 的结果。去 AI 味时整段
    #   删除，于是这条断言改成**反向**锁住它不许回来：本页只报本次运行的读数。
    assert "曾实测到" not in r.text, "跨 run 借来的历史数字又回来了"


def test_facts_template_survives_missing_context() -> None:
    """★ 模板不能因为少了一个上下文变量就 500。

    现场演示最怕的不是数据少，而是页面崩。因此新加的区块一律用
    ``{% if %}`` 兜住 —— 这次实测就踩到了：服务进程还没重启，
    新模板引用了新的上下文变量，直接 500。
    """
    env = _jinja_env()

    # 只给最小上下文：run_id 与空的 facts，其余一概不给
    html = env.get_template("facts.html").render(
        run_id="x", facts=[], total=0, layer_counts={}, conflicts=[],
        metric_counts={}, review_count=0, f_layer=None, f_metric=None,
        f_review=0, selected=None, evidence_context={},
    )
    assert "事实库" in html


# ==========================================================================
# ★ 模板字段名一致性（元测试）
# ==========================================================================


def test_templates_do_not_reference_unknown_artifact_fields() -> None:
    """模板引用的产物字段必须真实存在。

    为什么需要这条测试：`page_meta.garbled` 这种字段名写错时，
    Jinja2 默认**不会报错**，只会渲染成空字符串 —— 页面看起来正常，
    数字却全部是空白。实测踩过：模板写了 `garbled` 而产物里叫
    `garbled_ratio`，三个页面直接 500（严格模式）或静默显示空白。

    做法：把每个模板里 `xxx.field` 形式的引用抽出来，
    与对应产物文件的真实字段集合比对。
    """
    import re

    tmpl_dir = Path(__file__).resolve().parents[1] / "web" / "templates"
    src = (tmpl_dir / "extract.html").read_text(encoding="utf-8")

    # extract.html 里 page_meta 来自 pages/index.json
    pages_index = json.loads(
        (Path(__file__).resolve().parents[1] / "runs" / "fix-vlm4" / "pages" / "index.json")
        .read_text(encoding="utf-8")
    ) if (Path(__file__).resolve().parents[1] / "runs" / "fix-vlm4").is_dir() else None

    if pages_index is None:
        pytest.skip("缺少用于比对的 run 产物")

    real_fields = set(pages_index[0].keys())
    used = set(re.findall(r"page_meta\.([a-z_][a-z0-9_]*)", src))
    unknown = used - real_fields
    assert not unknown, (
        f"extract.html 引用了 pages/index.json 中不存在的字段：{sorted(unknown)}。"
        f"真实字段为：{sorted(real_fields)}"
    )


def test_all_templates_compile() -> None:
    """所有模板都必须能被 Jinja2 编译（语法正确、继承链完整）。

    ★ 用 ``create_app()`` 真正在用的那个 Jinja 环境编译，**不自己另搭一个**。
    原先这里 new 了一个 Environment 再手动补 `md` / `prose` 两个过滤器 —— 于是
    「测试能编译」和「页面上能渲染」变成两件事，加过滤器时必须记得改两处。
    实测踩过：新加的 `marks` 只注册在 ``create_app`` 里，这里没有，
    ``analyze.html`` 编译失败。现在过滤器的注册只有 ``web.app.register_filters``
    一处，本测试直接复用它（见 ``app.state.jinja_env``）。
    """
    tmpl_dir = Path(__file__).resolve().parents[1] / "web" / "templates"
    env = _jinja_env()
    for name in ("md", "prose", "marks"):
        assert name in env.filters, f"应用环境里没有 {name} 过滤器 —— register_filters 漏了"

    files = sorted(p.name for p in tmpl_dir.glob("*.html"))
    assert files, "没有找到任何模板"
    for name in files:
        try:
            env.get_template(name)
        except Exception as exc:
            pytest.fail(f"模板 {name} 编译失败：{type(exc).__name__}: {exc}")


# Jinja2 中会与 dict 内置方法冲突的键名。
# `c.values` 解析成 `.values()` 方法而不是同名键，报
# "TypeError: 'builtin_function_or_method' object is not iterable"。
# 这类错误**编译期发现不了**，只在渲染到那一行时才炸 —— 实测踩过。
_DICT_METHOD_NAMES = ("values", "items", "keys", "get", "copy", "update", "pop")


def test_templates_avoid_dict_method_name_collisions() -> None:
    """模板中不得用 ``x.values`` 这类写法访问字典键。

    Jinja2 的属性查找优先于键查找，因此 ``{{ c.values }}`` 拿到的是
    dict 的 ``values`` **方法**，而不是名为 "values" 的键。
    凡是键名与 dict 方法同名，**必须用下标访问** ``{{ c['values'] }}``。

    这条测试是静态检查：它不依赖具体数据，因此能在提交前就发现问题 ——
    而渲染期错误只在恰好有数据走到那一行时才暴露。
    """
    import re

    tmpl_dir = Path(__file__).resolve().parents[1] / "web" / "templates"
    pattern = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\.(" + "|".join(_DICT_METHOD_NAMES) + r")\b")

    offenders: list[str] = []
    for path in sorted(tmpl_dir.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            # 跳过注释行（模板注释里可能提到这个陷阱本身）
            if line.strip().startswith("{#"):
                continue
            for m in pattern.finditer(line):
                # `x.items()` 这类显式方法调用是有意的，不算冲突；
                # 但 `x.values|join(...)` 是真错。
                after = line[m.end():m.end() + 2]
                if after.startswith("("):
                    continue
                offenders.append(f"{path.name}:{lineno}  {m.group(0)}  ->  {line.strip()[:80]}")

    assert not offenders, (
        "以下模板用属性语法访问了与 dict 方法同名的键，会在渲染时报 "
        "'builtin_function_or_method' object is not iterable。\n"
        "请改用下标访问，如 c['values']：\n  " + "\n  ".join(offenders)
    )


def test_pages_render_without_empty_numbers(client: TestClient) -> None:
    """页面关键位置不应出现空白数字。

    `/extract` 的预扫描指标区若字段名写错，会渲染成空串 ——
    技术上「200 OK」，但对演示毫无价值（页面上全是空白）。
    这条测试断言那些位置真的有数字。
    """
    import re

    r = client.get("/extract")
    assert r.status_code == 200
    # 预扫描指标行必须带有实际数字
    for label in ("有效字符", "乱码率", "文本覆盖率", "图像覆盖率", "表框线段"):
        m = re.search(rf"{label}\s*<b>([^<]*)</b>", r.text)
        assert m, f"页面缺少「{label}」指标"
        assert m.group(1).strip(), f"「{label}」的值为空白 —— 模板字段名可能写错"


# ==========================================================================
# 图表层（web/charts.py + web/chartdata.py）
# ==========================================================================


def test_charts_are_markup_so_jinja_does_not_double_escape() -> None:
    """图表返回 ``Markup`` —— 否则 Jinja2 会把 SVG 当文本转义，页面上只剩源码。

    这是最容易在重构中被"顺手改坏"的一点：把返回类型改成 ``str``
    不会报任何错，只是页面上再也看不到图。
    """
    from markupsafe import Markup

    from web import charts as C

    out = C.bar_h([("a", 1), ("b", 2)], label="t")
    assert isinstance(out, Markup), "bar_h 必须返回 Markup"


def test_chart_escapes_untrusted_labels() -> None:
    """图表的标签来自产物文件，**必须转义** —— 否则报告里的一段

    文本就能往演示页注入脚本。这里的载荷是标准的 ``<script>`` 测试串。
    """
    from web import charts as C

    evil = '<script>alert(1)</script>'
    for svg in (
        C.bar_h([(evil, 1)], label=evil),
        C.stack([(evil, 1)], label=evil),
        C.donut([(evil, 1)], label=evil),
        C.tornado([{"assumption": evil, "low": 1, "high": 2}], label=evil),
    ):
        assert "<script>" not in str(svg), "图表未转义标签，存在注入风险"
        assert "&lt;script&gt;" in str(svg)


def test_charts_handle_empty_input_without_raising() -> None:
    """空数据要返回占位内容而不是抛异常 —— 图是附加信息，不该拖垮整个页面。

    占位是**一句说明文字**（``<div>``）而非空 ``<svg>``：
    给一张空的坐标轴反而会让人以为「数据是 0」。
    """
    import re

    from web import charts as C

    cases = {
        "bar_h": C.bar_h([]),
        "stack": C.stack([]),
        "donut": C.donut([]),
        "hist": C.hist([]),
        "tornado": C.tornado([]),
    }
    for name, svg in cases.items():
        s = str(svg)
        assert s.strip(), f"{name} 空输入返回空串，页面上会留下空白块"
        assert not s.startswith("<svg"), \
            f"{name} 空输入不该画出空坐标轴（会被读成「数据是 0」）"
        # 必须有一句能读的中文说明，而不是光秃秃的空 div
        assert re.search(r"[一-鿿]{2,}", s), \
            f"{name} 空输入缺少可读的说明文字：{s[:120]}"


def test_chartdata_returns_empty_dict_for_missing_artifacts() -> None:
    """没有产物的 run（如只跑了审计的评测 run）不能报错，只能返回空图集。

    页面对空图集的处理是「不渲染该图表区」，这是**如实反映数据条件**，
    而不是页面坏了。
    """
    from web import chartdata as CD

    assert CD.facts_charts([]) == {}
    assert CD.audit_charts(None) == {}
    assert CD.audit_charts({}) == {}
    assert CD.valuation_charts(None) == {}
    assert CD.valuation_charts({}) == {}


def test_chartdata_does_not_invent_metrics_when_absent() -> None:
    """``metrics`` 为 null 时**不得**凭空画出 P/R/F1。

    演示 run 以 ``--data inbox`` 启动、没有 golden 标注，导致
    ``audit.json`` 的 ``metrics`` 为 null。此时若画出一张指标图，
    就等于在页面上编造了一个评测结果 —— 这是本项目最不能犯的错。
    """
    from web import chartdata as CD

    out = CD.audit_charts({"findings": [{"severity": "confirmed",
                                         "error_type": "E1_unit"}]})
    assert "sev_stack" in out and "type_bar" in out
    assert "prf_bar" not in out, "metrics 缺失时不应出现 P/R/F1 图"


def _tax(out: dict) -> dict[str, tuple[int, bool]]:
    """把 ``type_taxonomy`` 折成 ``{显示名: (计数, 是否已实现)}``。

    元素是 **3 元组**（名, 计数, 已实现）—— 第 3 位是 2026-09-27 加的，
    为的是把「本次未检出」与「本轮未实现」分开（见
    ``chartdata.CANONICAL_TYPES`` 上方注释）。
    """
    return {name: (n, impl) for name, n, impl in out["type_taxonomy"]}


def test_taxonomy_lists_all_six_types_even_when_absent() -> None:
    """E1–E6 覆盖清单**必须列全六类**，未检出的显式写 0。

    若只列「出现过的类型」，页面上看不到 E4 就会被读成「不支持 E4」——
    而赛题考的正是能否覆盖 E1–E6。「本次没查到」与「不支持」是两件事，
    清单必须把它们分开。
    """
    from web import chartdata as CD

    out = CD.audit_charts({"findings": [{"severity": "confirmed",
                                         "error_type": "E1_unit"}]})
    tax = _tax(out)

    assert len(tax) == 6, f"应列出规范六类，实际 {len(tax)} 类：{tax}"
    assert tax["E1 单位与量纲"][0] == 1
    for name in ("E2 估值倍数", "E3 统计口径", "E4 引用与来源",
                 "E5 算术一致性", "E6 定性矛盾"):
        assert tax[name][0] == 0, f"{name} 未检出也必须出现在清单里（值 0）"


def test_taxonomy_marks_unimplemented_types_separately_from_zero_hits() -> None:
    """「本轮未实现」必须与「本次未检出」**在数据层就分开**。

    E6 在本仓**没有任何检测规则产出它**（见 ``chartdata.CANONICAL_TYPES``）。
    若它只以一个灰色的 0 出现，页面就在暗示「E6 查过了、没问题」——
    这是能力超报：把「还没做」说成「做到了」。原来正是这样显示的。
    """
    from web import chartdata as CD

    tax = _tax(CD.audit_charts({"findings": []}))

    assert tax["E6 定性矛盾"] == (0, False), "E6 无检测规则，必须标记为未实现"
    for name in ("E1 单位与量纲", "E2 估值倍数", "E3 统计口径",
                 "E4 引用与来源", "E5 算术一致性"):
        n, impl = tax[name]
        assert impl is True, f"{name} 有规则，不该被标成未实现"
        assert n == 0, f"{name} 未检出应为 0"

    # 规则确实产出 E6 时（未来实现了检测器），标记应随之翻成 True ——
    # 否则这行代码会永远把 E6 钉在「未实现」上。
    tax2 = _tax(CD.audit_charts({"findings": [
        {"severity": "confirmed", "error_type": "E6_qualitative"}]}))
    assert tax2["E6 定性矛盾"][0] == 1, "E6 的计数不应被吞掉"


def test_taxonomy_merges_alias_keys_into_one_row() -> None:
    """E2/E3/E5 在产物里各有两个别名键，清单里必须**合并成一行**。

    否则同一个类型会重复出现两次，条形图与清单对不上。
    """
    from web import chartdata as CD

    out = CD.audit_charts({"findings": [
        {"severity": "confirmed", "error_type": "E2_valuation_multiple"},
        {"severity": "confirmed", "error_type": "E2_multiple"},
        {"severity": "confirmed", "error_type": "E5_arithmetic"},
        {"severity": "confirmed", "error_type": "E5_arithmetic_consistency"},
    ]})
    tax = _tax(out)

    assert tax["E2 估值倍数"][0] == 2, "两个 E2 别名键应合并计数"
    assert tax["E5 算术一致性"][0] == 2, "两个 E5 别名键应合并计数"
    assert list(tax).count("E2 估值倍数") == 1, "不得重复出现同一类型"


def test_taxonomy_surfaces_unknown_types_instead_of_swallowing() -> None:
    """上游新增而 web 层未及同步的类型**必须原样露出**，并标注「表外」。

    静默吞掉会让清单总数小于实际检出数 —— 那种「数字对不上」本身就是
    需要被看见的信号，不该被表格掩盖。
    """
    from web import chartdata as CD

    out = CD.audit_charts({"findings": [
        {"severity": "confirmed", "error_type": "E1_unit"},
        {"severity": "confirmed", "error_type": "E9_brand_new"},
    ]})
    tax = _tax(out)

    assert len(tax) == 7, f"六类 + 1 个表外类型 = 7，实际 {len(tax)}：{tax}"
    outside = [k for k in tax if "表外" in k]
    assert len(outside) == 1, f"表外类型应被标出，实际：{tax}"
    assert tax[outside[0]][0] == 1
    # 表外类型能出现在产物里，就证明有规则产出它 —— 按「已实现」处理。
    assert tax[outside[0]][1] is True, "表外类型不应被标成未实现"


def test_taxonomy_survives_zero_findings() -> None:
    """0 检出时清单**仍须存在**（六类全 0）。

    「六类都查过、都没问题」与「压根没查」在页面上必须能分辨。若清单随
    findings 为空一起消失，一份干净草稿的页面就看不出核查到底做没做。
    """
    from web import chartdata as CD

    for metrics in (None, {"precision": 1.0, "recall": 1.0, "f1": 1.0}):
        out = CD.audit_charts({"findings": [], "metrics": metrics})
        tax = _tax(out)
        assert len(tax) == 6, f"0 检出时仍应列出六类，实际 {len(tax)}"
        assert all(n == 0 for n, _impl in tax.values()), f"0 检出应全为 0：{tax}"


def test_audit_page_renders_zero_count_types(client: TestClient) -> None:
    """路由级：页面上必须真的出现未检出类型的名字，而非只在数据层存在。"""
    body = client.get("/audit").text
    for name in ("E1 单位与量纲", "E2 估值倍数", "E3 统计口径",
                 "E4 引用与来源", "E5 算术一致性", "E6 定性矛盾"):
        assert name in body, f"页面未渲染 {name}"
    assert "E1–E6 覆盖清单" in body, "缺少清单标题，读者无从判断这是能力清单"
    assert "chip zero" in body, "未检出类型应带 zero 样式（弱化但不隐藏）"


def test_audit_page_zero_findings_is_not_reported_as_missing(tmp_path) -> None:
    """★ 「查完了、零检出」**不得**被报成「没有核查产物」。

    这是 2026-09-27 修掉的一个真 bug：模板原先的守卫是
    ``{% if not findings and not metrics %}``，而 ``metrics`` 为 null 是
    **常态**（没有 golden 标注集就算不出来）。于是「核查跑完、一条错都没检出」
    这个最干净、最常见的结果，被整页降级成一句「本次运行没有核查结果」；
    下面那句「未发现问题」永远不可达，E1–E6 覆盖清单也一并消失 ——
    而清单恰恰是 0 检出时最该出现的东西。

    判据只能是**产物在不在**（``audit_missing``），不是**查出了几条**。
    本测试直接构造「产物在、findings 为空、metrics 为 null」这个组合。
    """
    run = tmp_path / "zero"
    (run / "audit").mkdir(parents=True)
    (run / "manifest.json").write_text(
        json.dumps({"pipeline": "full", "status": "done"}), encoding="utf-8")
    (run / "audit" / "audit.json").write_text(
        json.dumps({"findings": [], "metrics": None}, ensure_ascii=False),
        encoding="utf-8")

    body = TestClient(create_app(runs_root=tmp_path)).get("/audit?run=zero").text

    assert "本次运行没有核查产物" not in body, "产物存在，不该报「没有核查产物」"
    assert "未发现问题" in body, "0 检出必须给出明确的干净结论"
    assert "E1–E6 覆盖清单" in body, "覆盖清单不随 findings 为空而消失"
    assert "E6 定性矛盾" in body, "六类必须列全"


def test_audit_page_reports_missing_artifact_when_absent(tmp_path) -> None:
    """反向：**产物真的不在**时，必须给出那句告警（否则读者以为核查跑过了）。"""
    run = tmp_path / "noaudit"
    run.mkdir()
    (run / "manifest.json").write_text(
        json.dumps({"pipeline": "extract", "status": "done"}), encoding="utf-8")

    body = TestClient(create_app(runs_root=tmp_path)).get("/audit?run=noaudit").text

    assert "本次运行没有核查产物" in body, "缺产物必须显式告警"
    assert "未发现问题" not in body, "没查过就不能说「未发现问题」"


def _assert_library_consistent(runs_root: Path, run_id: str | None = None) -> int:
    """核对 ``/library`` 上「表格页」的三个来源：卡片 / 明细列 / 产物层。

    返回该 run 的表格页总数；无材料可列时返回 ``-1``（调用方据此跳过）。
    """
    body = TestClient(create_app(runs_root=runs_root)).get(
        "/library" + (f"?run={run_id}" if run_id else "")).text
    assert "表格页" in body
    card = re.search(r'data-table-pages-total="(\d+)"', body)
    rows = [int(x) for x in re.findall(r'data-table-pages="(\d+)"', body)]
    assert card, "卡片上的「表格页」总数必须带 data-table-pages-total"

    art = load_run(run_id, runs_root)
    # ★ 按**文件**（rel_path）复算，不能按 doc_id（2026-09-27 修）。
    #   原先写 `art.pages_of(d["doc_id"])`，而 pages/index.json 是按**内容哈希**
    #   doc_id 索引的：语料里有 15 对文件内容完全相同（同一份公告抓了两次），
    #   共用同一个 doc_id，于是 pages_of 把两份的页一起返回 —— 本测试复算出 270，
    #   而卡片是 158，两个都"有依据"却互相矛盾。真正的原因是**复算口径与卡片不同源**：
    #   卡片数的是每份文件自己的 page_routes。
    #   现在改为按 rel_path 分组复算 pages/index.json —— 与 documents.json 相互独立
    #   的两个产物，真交叉核对（实测两者都给出 585/158/106/74）。
    per_file: dict[str, int] = {}
    for p in art.pages:
        if p.get("route") != "D_table":
            continue
        key = p.get("rel_path") or p.get("file") or ""
        per_file[key] = per_file.get(key, 0) + 1
    per_doc = [per_file.get(d.get("rel_path") or d.get("filename") or "", 0)
               for d in art.documents]
    if not per_doc:
        return -1                      # 该 run 没有材料，页面本就不渲染明细行
    assert rows, "有材料时明细列必须带 data-table-pages，便于本测试与探针核对"

    assert rows == per_doc, (
        f"明细列 {rows} 与产物复算 {per_doc} 不一致 —— 同页两个「表格页」数字互相矛盾")
    assert int(card.group(1)) == sum(per_doc), (
        f"卡片 {card.group(1)} != 明细列之和 {sum(per_doc)}")
    return sum(per_doc)


def test_library_table_page_column_sums_to_card(demo_run: Path) -> None:
    """★ 同一页上「表格页」的两个数字必须一致（夹具 run）。"""
    assert _assert_library_consistent(demo_run) >= 0, "夹具 run 应至少有一份材料"


def test_library_table_page_column_holds_on_real_run(project_root: Path) -> None:
    """★ 在**真有表格页**的真实 run 上复验同一个等式。

    原 bug 只在 ``D_table`` 非零时才显形（卡片 101 / 明细列恒 0）；夹具 run 是
    合成样本、D_table 恰为 0，等式退化成 0 == 0，抓不到那个 bug。
    因此这里必须挑一个 ``D_table > 0`` 的真实运行，逐个 run 复验。
    """
    root = project_root / "runs"
    if not root.is_dir():
        pytest.skip("本仓没有 runs/ 产物")

    checked, best = 0, 0
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not (d / "manifest.json").is_file():
            continue
        try:
            best = max(best, _assert_library_consistent(root, d.name))
        except ArtifactError:
            continue  # 未完成/损坏的运行，页面本身也不展示
        checked += 1

    if checked == 0:
        pytest.skip("本仓没有可展示的真实 run 产物")
    if best == 0:
        pytest.skip("本仓真实 run 均无表格页，等式在非零场景下未被验证")


def test_optnum_refuses_to_fabricate() -> None:
    """``_optnum`` 对非数值返回 ``None``，**绝不返回 0**。

    ``_fnum`` 的 0 兜底对「计数」成立（0 页就是 0 页），对**量值**却是编造：
    把 ``dcf.value_per_share = "N/A"`` 读成 ``0``，图上就会立一根
    「DCF：0 元」的标记，并让 0 参与 vmin/vmax，把真实的 12.16–14.04
    区间拖成 -1.68–15.72。读者看到的是一个**不存在的估值**。
    """
    from web import chartdata as CD

    for dirty in ("N/A", "", "NaN", "nan", "—", "不适用", None, [], {},
                  float("nan"), float("inf"), float("-inf")):
        assert CD._optnum(dirty) is None, f"{dirty!r} 应判为缺值而非 0"

    # 0 是**合法量值**（利润可以为 0），必须原样保留 —— 否则会走向另一个极端：
    # 把真数据当脏数据丢掉。
    assert CD._optnum(0) == 0.0
    assert CD._optnum("0") == 0.0
    assert CD._optnum(12.5) == 12.5
    assert CD._optnum("-3.2") == -3.2


def test_valuation_charts_drop_dirty_values_instead_of_zeroing() -> None:
    """脏量值不许进图：要么不画这个标记，要么整张图不画。"""
    from web import chartdata as CD

    base = {
        "value_range_low": 12.16, "value_range_mid": 13.10, "value_range_high": 14.04,
        "dcf": {"value_per_share": 12.16, "terminal_value_share": 0.68,
                "explicit_years": [{"year": 2026, "revenue": 3.0e11, "fcff": 2.0e10}]},
        "comps": {"peers": [{"name": "甲", "ticker": "000001", "pe": 8.1, "pb": 0.9}],
                  "median_pe": 8.1, "median_pb": 0.9},
    }
    clean = str(CD.valuation_charts(base)["range"])
    assert "DCF" in clean
    assert "nan" not in clean.lower(), "干净数据不该出现 nan 坐标"

    for dirty in ("N/A", "NaN", None):
        d = json.loads(json.dumps(base))
        d["dcf"]["value_per_share"] = dirty
        svg = str(CD.valuation_charts(d)["range"])
        assert "DCF：0" not in svg, f"{dirty!r} 被当成了 0 元的假估值"
        assert "nan" not in svg.lower(), f"{dirty!r} 污染了坐标轴"

    # 区间端点缺一 -> 主图整张不画（端点缺失时画区间等于编造区间）
    for key in ("value_range_low", "value_range_mid", "value_range_high"):
        d = json.loads(json.dumps(base))
        d[key] = "N/A"
        assert "range" not in CD.valuation_charts(d), f"{key} 缺失时不该仍画出区间图"

    # 中位数缺失 -> 标题不再声称中位数，但不连累整张图
    d = json.loads(json.dumps(base))
    d["comps"]["median_pb"] = "N/A"
    pb = CD.valuation_charts(d)["peer_pb"]
    assert "中位数" not in str(pb), "中位数算不出来时不得在标题里声称一个"
    assert "peer_pb" in CD.valuation_charts(d), "中位数缺失不该连累整张图"


def test_audit_prf_ratios_are_not_zero_filled() -> None:
    """P/R/F1 是**比率**：缺项不补 0，否则图上出现「精确率 0%」的假断言。"""
    from web import chartdata as CD

    assert "prf_bar" not in CD.audit_charts({"findings": [], "metrics": None})
    assert "prf_bar" not in CD.audit_charts({"findings": [], "metrics": {}})
    assert "prf_bar" not in CD.audit_charts(
        {"findings": [], "metrics": {"precision": "N/A"}}), "脏比率不该被读成 0"

    partial = str(CD.audit_charts(
        {"findings": [], "metrics": {"precision": 1.0}})["prf_bar"])
    assert "精确率" in partial
    assert "召回率" not in partial, "算不出来的比率不该画出一根 0 条"

    full = str(CD.audit_charts(
        {"findings": [], "metrics": {"precision": 1.0, "recall": 0.5,
                                     "f1": 0.6667}})["prf_bar"])
    assert all(k in full for k in ("精确率", "召回率", "F1"))


def test_valuation_page_keeps_no_placeholder_disclosure(client: TestClient) -> None:
    """「刻意不提供占位样本」这条声明**必须留在页面上**。

    相对估值的可信度取决于「倍数从哪来」。删掉这条声明，页面就只是
    展示了一组倍数，读者无从知道它不是编的 —— 见
    ``pipelines/r4_valuation/model.py:38-51``。
    """
    body = client.get("/valuation").text
    assert "占位样本" in body, "缺失「不提供内置占位样本」声明"
    assert "显式失败" in body, "缺失「快照缺失时显式失败」说明"


def test_valuation_page_surfaces_comps_provenance(client: TestClient) -> None:
    """可比公司倍数的**来源、时点、算式**必须能从页面读到。

    相对估值最该被追问的就是「倍数从哪来」。产物里的 ``comps_provenance``
    记着来源接口、行情抓取时间、自算式与真值校验；这些若不呈现，
    页面就只是给出了一组无从核对的倍数。
    """
    body = client.get("/valuation").text
    assert "倍数从哪来：数据来源" in body, "缺少可比公司来源折叠块"
    for probe in ("快照文件", "财务数据时点", "行情抓取时间",
                  "价格来源", "PE 算式", "PB 算式", "真值校验"):
        assert probe in body, f"来源表中缺少「{probe}」"


def test_valuation_provenance_matches_artifact_verbatim(client: TestClient,
                                                        demo_run: Path) -> None:
    """页面上显示的来源信息必须与 ``valuation.json`` **逐字一致**。

    若页面自己"转述"来源，现场看到的就与评审复现的不是同一份数据 ——
    这正是本项目最不能破的承诺。

    注意 ``demo_run`` 是 **runs_root**（run 目录是它的子目录），
    故此处按 ``*/valuation/valuation.json`` 定位，不写死 run 名。
    """
    matches = sorted(demo_run.glob("*/valuation/valuation.json"))
    if not matches:
        pytest.skip("该 run 未产出 valuation.json")
    val = json.loads(matches[0].read_text(encoding="utf-8"))
    prov = val.get("comps_provenance")
    if not prov:
        pytest.skip("该 run 的估值产物没有 comps_provenance")

    body = client.get("/valuation").text
    for key in ("snapshot_path", "as_of", "fetched_at"):
        assert prov[key] in body, f"页面未显示 {key}={prov[key]}"
    for src in prov["sources"].values():
        assert src in body, f"页面未显示来源：{src}"
    for formula in prov["multiple_formula"].values():
        assert formula in body, f"页面未显示算式：{formula}"


def test_chartdata_uses_pipeline_values_verbatim() -> None:
    """估值图必须直接用产物里算好的数，不能在此层重算。

    网页显示的估值区间若与 ``valuation.json`` 有一丝差别，
    「现场看到的 === 评审复现的」这条承诺就破了。
    """
    from web import chartdata as CD

    val = {
        "value_range_low": "12.1554",
        "value_range_mid": "13.35025",
        "value_range_high": "14.0394",
        "dcf": {"value_per_share": "14.0394", "terminal_value_share": "0.7897"},
        "comps": {"implied_value_per_share_low": "12.1554",
                  "implied_value_per_share_mid": "12.6611",
                  "implied_value_per_share_high": "13.1668"},
    }
    svg = str(CD.valuation_charts(val)["range"])
    for v in ("12.1554", "14.0394"):
        assert v in svg, f"估值区间 {v} 未出现在图中"


def test_chartdata_metric_bar_aggregates_rather_than_one_rect_per_row() -> None:
    """构成类图必须先聚合。

    直接把这些条目逐条交给 ``stack()`` 会一段画一个 ``rect``：
    359 条事实 → 48 KB 的 SVG，而语义上只有 2 段。
    """
    from web import chartdata as CD

    class _F:
        def __init__(self, layer: str) -> None:
            self.layer_tag = layer
            self.extractor_deterministic = layer == "L1"
            self.extractor_name = "table_grid" if layer == "L1" else "vlm_scan"
            self.confidence = 0.95
            self.metric = "revenue"
            self.needs_review = False
            self.review_reason = None

    facts = [_F("L1")] * 300 + [_F("L3")] * 59
    svg = str(CD.facts_charts(facts)["layer_mix"])
    assert svg.count("<rect") < 20, f"stack 未聚合，生成了 {svg.count('<rect')} 个 rect"


# ==========================================================================
# 分页（/facts 与 /extract 的页码选择器）
# ==========================================================================


def test_facts_pagination_clamps_out_of_range(client: TestClient) -> None:
    """``page`` 越界要**夹紧**到有效范围，不能 500、也不能返回空列表。"""
    import re

    base = client.get("/facts").text
    total = int(re.search(r"共 <b>(\d+)</b> 条", base).group(1))
    per_page = 50
    n_pages = max(1, -(-total // per_page))

    for bad in ("0", "-1", "99999"):
        r = client.get(f"/facts?page={bad}")
        assert r.status_code == 200, f"page={bad} 未正常渲染"
        m = re.search(r"第 <b>(\d+)–(\d+)</b> 条", r.text)
        assert m, f"page={bad} 缺少条数区间"
        assert int(m.group(2)) <= total
    # 末页应当是有效的最后一页
    r = client.get(f"/facts?page={n_pages + 5}")
    assert r.status_code == 200
    assert f"共 <b>{total}</b> 条" in r.text


@pytest.fixture(scope="module")
def big_client(tmp_path_factory) -> TestClient:
    """一个事实数远超单页的合成 run —— 用来真正跑通分页。

    为什么不复用 ``client``：mock 流水线只产出 41 条事实，不足一页（50），
    分页代码虽然被执行，但「第 2 页」永远被夹紧回第 1 页 ——
    切片写错也测不出来。这里直接造 120 条，覆盖 3 页、含末页不满的情况。
    """
    root = tmp_path_factory.mktemp("big-runs") / "big"
    (root / "facts").mkdir(parents=True)
    n = 120
    facts = [
        {
            "fact_id": f"fct_synth_{i:04d}",
            "entity": "601857",
            "metric": "revenue" if i % 2 == 0 else "net_profit_total",
            "value": str(1000 + i),
            "unit": "元",
            "source_layer": "L1_text_layer" if i % 3 else "L3_vlm_scan",
            "confidence": 0.95,
            "extractor": "table_grid",
            "evidence_span_ids": [f"evd_{i:04d}"],
            "needs_review": i % 10 == 0,
        }
        for i in range(n)
    ]
    evidence = [
        {
            "span_id": f"evd_{i:04d}", "doc_id": "doc_synth", "page": 1 + i % 5,
            "quote": f"营业收入 {1000 + i} 元", "source_layer": "L1_text_layer",
        }
        for i in range(n)
    ]
    (root / "facts" / "facts.json").write_text(
        json.dumps({"facts": facts, "evidence": evidence}, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "manifest.json").write_text(
        json.dumps({"pipeline": "synth", "status": "success",
                    "started_at": "2026-01-01T00:00:00Z"}, ensure_ascii=False),
        encoding="utf-8",
    )
    return TestClient(create_app(runs_root=root.parent))


def _page_fact_ids(html: str) -> list[str]:
    """取出该页「溯源」链接里的事实 ID（按出现顺序）。"""
    import re

    return re.findall(r"fact_id=(fct_[a-z0-9_]+)", html)


def test_facts_pagination_splits_into_pages(big_client: TestClient) -> None:
    """120 条 / 每页 50 → 3 页，且各页条数为 50/50/20。"""
    import re

    expected = {1: (1, 50), 2: (51, 100), 3: (101, 120)}
    for page, (lo, hi) in expected.items():
        r = big_client.get(f"/facts?page={page}")
        assert r.status_code == 200
        m = re.search(r"第 <b>(\d+)–(\d+)</b> 条", r.text)
        assert m, f"第 {page} 页缺少条数区间"
        assert (int(m.group(1)), int(m.group(2))) == (lo, hi), \
            f"第 {page} 页区间应为 {lo}–{hi}，实际 {m.groups()}"
        assert len(_page_fact_ids(r.text)) == hi - lo + 1


def test_facts_pagination_pages_do_not_overlap(big_client: TestClient) -> None:
    """相邻页不得重复任何一条事实 —— 切片写错时最典型的症状。"""
    p1 = set(_page_fact_ids(big_client.get("/facts?page=1").text))
    p2 = set(_page_fact_ids(big_client.get("/facts?page=2").text))
    p3 = set(_page_fact_ids(big_client.get("/facts?page=3").text))
    assert p1 and p2 and p3
    assert not (p1 & p2), "第 1、2 页出现重复事实"
    assert not (p2 & p3), "第 2、3 页出现重复事实"
    assert len(p1 | p2 | p3) == 120, "三页并集应恰好覆盖全部 120 条，无遗漏"


def test_facts_pagination_links_preserve_filters(big_client: TestClient) -> None:
    """翻页链接必须带上当前筛选条件，否则一翻页筛选就丢了。"""
    import re

    # metric=revenue → 60 条（偶数号），跨 2 页
    r = big_client.get("/facts?metric=revenue")
    assert r.status_code == 200
    links = re.findall(r'href="(/facts\?[^"]*page=\d+)"', r.text)
    assert links, "带筛选且跨页时未渲染翻页链接"
    assert all("metric=revenue" in href for href in links), \
        f"翻页链接丢失了 metric 筛选条件：{links[:3]}"

    # 翻到第 2 页后，筛选条件仍须在链接里
    r2 = big_client.get("/facts?metric=revenue&page=2")
    assert r2.status_code == 200
    for href in re.findall(r'href="(/facts\?[^"]*page=\d+)"', r2.text):
        assert "metric=revenue" in href


def test_facts_trace_link_does_not_duplicate_fact_id(client: TestClient) -> None:
    """每行的「溯源」链接只能出现**一个** ``fact_id``。

    分页 URL 里若已带 ``fact_id``，再追加就会出现重复参数；
    FastAPI 对标量查询参数取**第一个**，于是点哪行都停在旧的那条上。
    """
    import re

    r = client.get("/facts?page=1")
    assert r.status_code == 200
    for href in re.findall(r'href="(/facts\?[^"]*fact_id=[^"]*)"', r.text):
        assert href.count("fact_id=") == 1, f"溯源链接出现重复 fact_id：{href}"


def test_extract_page_selector_does_not_render_every_page(client: TestClient,
                                                          demo_run: Path) -> None:
    """页码选择器只渲染当前页附近的一段，不是整份文档逐页铺开。

    曾经把 273 页全渲染成 chip，单这一个区块就 5.7 万字节。
    """
    import re

    run = load_run("web-demo", demo_run)
    docs = run.documents
    if not docs:
        pytest.skip("该 run 没有文档")
    doc_id = docs[0]["doc_id"]
    n_pages = len(run.pages_of(doc_id))
    if n_pages < 30:
        pytest.skip(f"该 run 只有 {n_pages} 页，窗口式分页无意义")

    r = client.get(f"/extract?doc={doc_id}&page=7")
    assert r.status_code == 200
    n_links = len(re.findall(r'href="/extract\?[^"]*page=\d+"', r.text))
    assert n_links < n_pages / 2, \
        f"页码选择器渲染了 {n_links} 个链接（共 {n_pages} 页），未做窗口裁剪"


# ==========================================================================
# 视觉规范的可执行验收（2026-09-27 · 视觉方向「底稿 · 复核记号」）
#
# 这几条不是「样式好不好看」的主观检查，而是把设计文档里**可证伪的断言**
# 钉成断言。评审现场最容易翻车的是「实施到一半偷偷把颜色加回来」，
# 所以每一条都写成机器可查的。
# ==========================================================================


def test_prose_filter_escapes_before_bolding() -> None:
    """``prose`` 必须**先转义再加工** —— 顺序反了就是 XSS。

    这些字符串的源头是用户材料与模型输出，不是可信常量。
    """
    out = str(render_prose("**<script>alert(1)</script>**"))
    assert "<script>" not in out, "尖括号未转义，过滤器是 XSS 入口"
    assert "&lt;script&gt;" in out
    assert out.startswith("<strong>") and out.endswith("</strong>")


def test_prose_filter_does_not_double_escape() -> None:
    """返回值是 ``Markup``，Jinja2 不得再转义一次（否则 <strong> 变字面量）。"""
    from markupsafe import Markup

    out = render_prose("**加粗**")
    assert isinstance(out, Markup)
    assert "<strong>加粗</strong>" == str(out)


def test_prose_filter_handles_none_and_plain_text() -> None:
    assert str(render_prose(None)) == ""
    assert str(render_prose("没有星号")) == "没有星号"


def test_status_icon_is_a_ticks_mark_not_an_emoji() -> None:
    """状态图标走记号体系，不是彩色 emoji。

    emoji 的大小与基线由字体决定，同行混排必然不齐；且它把「通过/失败」
    交给了色相 —— 而本方案全站只有一种墨。
    """
    from web.artifacts import RunSummary

    got = {s: RunSummary(run_id="x", pipeline="p", status=s).status_icon
           for s in ("success", "partial", "failed", "weird")}
    assert got == {"success": "√", "partial": "◐", "failed": "×", "weird": "○"}, got
    for mark in got.values():
        assert all(ord(ch) < 0x1F000 for ch in mark), f"仍是 emoji：{mark!r}"


# 渲染后不得出现的彩色字形。
#   · **不含箭头 →**（U+2192）—— 那是真正的转换算子（报告值 → 正确值），
#     不是装饰，设计文档明确保留。
#   · **不含几何图形块 U+25xx** —— 记号 ◐ ○ ▶ 在这一块里，它们是本方案
#     的记号体系本身，不是 emoji。
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⌀-⏿"
                    "⬀-⯿️]")

@pytest.mark.parametrize("path", ["/", "/facts", "/audit", "/valuation",
                                  "/extract", "/runs", "/library", "/process",
                                  "/analyze"])
def test_no_emoji_in_rendered_pages(client: TestClient, path: str) -> None:
    """全站默认渲染零个 emoji。

    emoji 的大小与基线由字体决定，Windows 与投影仪上尤其不齐；且它把
    「通过/失败」交给了色相 —— 而本方案全站只有一种墨，只用记号。
    """
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", client.get(path).text,
                  flags=re.S)
    found = sorted(set(_EMOJI.findall(body)))
    assert not found, f"{path} 仍渲染出彩色字形：{found}"


def test_prose_filter_normalises_marks_without_touching_words() -> None:
    """记号规范化**只换记号，一个字都不改**。

    产物里的说明文字自带 emoji 前饰（⚠️ / ✅ / 🔵 …），要收敛到本站记号体系。
    但绝不能顺手改到词句 —— 那是系统写的判定依据，答辩时会被逐字追问。
    """
    src = "⚠️ 该指标**全部标成 consolidated**，因此取最大值。"
    out = str(render_prose(src))
    assert out == "◐ 该指标<strong>全部标成 consolidated</strong>，因此取最大值。", out
    # 逐字保真：剥掉标签、记号与 ** 标记后，必须与原句逐字一致
    stripped = re.sub(r"<[^>]+>", "", out).removeprefix("◐ ")
    assert stripped == src.removeprefix("⚠️ ").replace("**", ""), stripped


@pytest.mark.parametrize("path", ["/", "/facts", "/audit", "/valuation",
                                  "/extract", "/runs", "/library", "/process",
                                  "/analyze"])
def test_no_bare_markdown_asterisks_in_rendered_pages(client: TestClient,
                                                      path: str) -> None:
    """裸 ``**`` 不得出现在渲染结果里。

    产物里有大量非 Markdown 的单行字符串自带 ``**``，模板若直接 ``{{ x }}``
    就会把排版事故印在页面上。实测曾有 29 处。

    ``client`` 把控制台的全局状态（`_analyze/`）指到了空目录，所以这里扫到的
    `/analyze` 只由模板与 ``runs_root`` 决定 —— 否则这一条会去扫**本机上一次
    真实运行**的日志尾巴，通过与否取决于谁在这台机器上跑过控制台。
    日志那一路改由 ``test_printed_plan_descriptions_carry_no_markdown_marks``
    守在源头。
    """
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", client.get(path).text,
                  flags=re.S)
    bare = re.findall(r"\*\*[^*<>]{1,80}\*\*", body)
    assert not bare, f"{path} 渲染出裸 **：{bare[:3]}"


def test_printed_plan_descriptions_carry_no_markdown_marks() -> None:
    """流水线与其阶段的 ``description`` 会被**原样打印**，不许带 Markdown 记号。

    这份文本有两条去路，两条都没有渲染器：``agent/cli.py`` 把它打进控制台
    （``流水线：full — ……``），``analyze.html`` 又把它放进 ``<pre>`` 的
    运行日志里。``**`` 到那两处只会原样显示两个星号。

    实测踩过：``pipelines/full/pipeline.yaml`` 的描述里写了 ``**同一份文件**``，
    控制台的运行日志里就真的出现了这两个星号。上面那条「整页不许有裸 ``**``」
    现在看不到它了（日志目录已被指到空目录），所以这里守在源头 ——
    否则那条断言只是被搬走，不是被替代。
    """
    root = Path(__file__).resolve().parents[1] / "pipelines"
    bad = []
    for yml in sorted(root.glob("*/pipeline.yaml")):
        plan = Plan.for_pipeline(yml.parent.name, root=root)
        pairs = [(plan.name, plan.description)]
        pairs += [(s.name, s.description) for s in plan.stages]
        bad += [f"{yml.parent.name}/{where}"
                for where, text in pairs if "**" in (text or "")]
    assert not bad, f"这些 description 会被原样打印，不该带 **：{bad}"


def _all_chart_svgs(client: TestClient) -> str:
    out = []
    for path in ("/", "/facts", "/audit", "/valuation", "/extract"):
        body = client.get(path).text
        out.extend(re.findall(r"<svg\b.*?</svg>", body, re.S))
    return "\n".join(out)


def test_no_chart_element_is_filled_with_the_alarm_colour(client: TestClient) -> None:
    """**没有任何图元可以用告警色填充** —— 这是硬约束。

    赛题要求风险审慎、不大面积红黄。原先 ``/audit`` 的严重度环图在单类别时
    整圈填红，``/extract`` 的算术校验条半根填红，都是这条约束的违例。
    告警只允许落在「记号 + 数值文字 + 3px 左条」上。
    """
    svg = _all_chart_svgs(client)
    assert svg, "没有采到任何内联 SVG，测试本身失效了"
    for m in re.finditer(r'style="[^"]*fill:\s*var\(--(s-alarm|stamp)\)[^"]*"', svg):
        ctx = svg[max(0, m.start() - 120):m.start() + 60]
        assert False, f"图元被填充为告警色：…{ctx}…"


def test_chart_marks_are_stroked_and_square(client: TestClient) -> None:
    """每个图元带 1px 描边、无圆角。

    墨阶的浅端（s4 2.15:1 / s5 1.55:1）低于非文本图形 3:1 的下限，
    没有描边时色块与纸面糊在一起 —— 描边是这条补偿铁律的唯一实现。
    圆角则是「卡片套件」的形态，与底稿的硬格线相冲。
    """
    svg = _all_chart_svgs(client)
    rounded = re.findall(r'<rect\b[^>]*\brx="[1-9]', svg)
    assert not rounded, f"仍有圆角图元 {len(rounded)} 个"
    bars = re.findall(r'<rect\b[^>]*style="[^"]*fill:var\(--s[1-5]\)[^"]*"[^>]*>', svg)
    assert bars, "没有采到墨阶图元"
    missing = [b for b in bars if "stroke:" not in b]
    assert not missing, f"{len(missing)} 个墨阶图元缺描边：{missing[:2]}"


def test_stack_percentage_text_colour_tracks_segment_luminance() -> None:
    """堆叠条上的百分比文字必须按**段自身明度**择色。

    原先硬编码 ``fill:#0d1117``（暗底假设）。换到纸面上就是深字压深段 ——
    这是浅底方案独有的暴露点，只改 CSS 会当场看不清。
    """
    from web import charts as C

    svg = str(C.stack([("A", 6), ("B", 4)]))
    assert "#0d1117" not in svg, "仍残留暗底硬编码字色"
    assert "var(--sheet)" in svg, "深段未使用纸色文字"


def test_audit_severity_is_a_stack_not_a_donut(client: TestClient) -> None:
    """严重度用构成条，不用环图。

    单类别时环图那一弧就是**整圈** —— 旧版据此把 100% 的环填成告警色，
    是全场最大的一块红色面积。构成条把这块面积压成一条 6px 高的带。
    """
    from web import chartdata as CD

    out = CD.audit_charts({"findings": [{"severity": "confirmed",
                                         "error_type": "E1_unit"}]})
    assert "sev_donut" not in out
    assert "sev_stack" in out


def test_runs_page_stamps_the_sheet_when_hashes_do_not_match() -> None:
    """产物哈希对不上时，页面必须**盖章**，而不只是加一条提示。

    这是全站唯一该出现印章（``.stamp``）的地方 —— 印章是复核人写
    「此页已核，不得使用」的动作。若这个分支渲染不出来，
    ``app.css`` 里那套 ``.stamp`` 就是死样式。
    """
    env = _jinja_env()

    # `runs.html` 继承 base.html，需要一整套上下文；只取用得到的键。
    html = env.get_template("runs.html").render(
        request=None,
        summary=None,
        run_id="r-test",
        current_path="/runs",
        nav=[],
        runs=[],
        rows=[],
        manifest_output_count=0,
        outputs=[],
        stages=[],
        integrity={
            "reproducible": False,
            "hash_matches": {},
            "mismatches": [f"f{i}.json" for i in range(12)],
            "checked": 0,
        },
    )
    assert 'class="stamp"' in html, "哈希不符却没有盖章"
    # 12 条不匹配只列前 8 条，但**必须说明还有 4 条**，
    # 否则读者会以为清单就是全部。
    assert "仅列出前 8 条" in html


def test_charts_cap_their_rendered_width() -> None:
    """图必须自带 ``max-width``，否则独占一行时会被放大到糊。

    ``viewBox`` 宽 560、``width:100%``，在 1440px 屏上独占一行就是 2.3 倍 ——
    按 12px 画的轴标签渲染成 28px，图糊成标题。上限由 ``charts._svg``
    统一给（1.3 倍），本断言防止它被后来的人当成多余样式删掉。
    """
    from web import charts as C

    for svg in (str(C.bar_h([("A", 3), ("B", 5)])),
                str(C.tornado([("wacc", 10.0, 14.0)])),
                str(C.stack([("A", 1), ("B", 1)]))):
        assert "max-width:" in svg, "图元没有宽度上限，独占一行会放大到糊"


# ==========================================================================
# 三处「页面把事实说反了」的回归（第二十轮）
# ==========================================================================


def test_markdown_bullets_lose_their_marker() -> None:
    """``- 条目`` 必须渲染成 ``<li>条目</li>``，破折号不能留在正文里。

    ★ 钉的是一个**永远不匹配**的正则：原生字符串里写 ``r'^[-*]\\\\s+'``
      其实是「反斜杠 + 字母 s」，不是空白字符类。判据和剥离用了**同一个**
      坏模式，所以列表结构照建、``- `` 却留在文字里 ——
      只断言 ``'<ul>' in html`` 的测试发现不了。
    """
    from web.app import render_markdown

    html = render_markdown("- 第一条\n- 第二条")
    assert "<ul>" in html and html.count("<li>") == 2
    assert "<li>第一条</li>" in html, f"列表项的破折号没被剥掉：{html!r}"
    assert "<li>第二条</li>" in html, f"列表项的破折号没被剥掉：{html!r}"


def test_table_arith_label_keeps_three_states_apart() -> None:
    """「没有合计线」与「有合计线但不平」必须说成两句不同的话。

    ★ 产物里的 ``checked`` **只统计相符的**合计线，所以一张不平的表会同时
      呈现 ``checked == 0`` 与 ``failures`` 非空。原先把 0 原样喂给
      ``describe_arithmetic``，它的第一分支 ``checked == 0`` 抢先命中 →
      标签写「无从校验（表内没有合计线）」，而**正下方**的明细区却在列
      不平的合计线 —— 同一张表自相矛盾。实测影响 18 张表。
    """
    from web.artifacts import TableView

    def tv(**kw: object) -> TableView:
        return TableView(index=0, method="m", n_rows=1, n_cols=1,
                         has_rules=True, **kw)  # type: ignore[arg-type]

    unbalanced = tv(arith_checked=0, arith_ok=False,
                    arith_failures=[{"row": 1, "lhs": 3, "rhs": 4}])
    assert "无从校验" not in unbalanced.arith_label, (
        "有合计线但不平的表被说成「表内没有合计线」"
    )
    assert "不平" in unbalanced.arith_label
    assert unbalanced.arith_css == "bad", "不平的表不该是中性色"

    no_rules = tv(arith_checked=0, arith_ok=True, arith_failures=[])
    assert "无从校验" in no_rules.arith_label

    verified = tv(arith_checked=6, arith_ok=True, arith_failures=[])
    assert "全部相符" in verified.arith_label
    assert verified.arith_css == "ok"


@pytest.fixture(scope="module")
def conflicts_client(tmp_path_factory) -> TestClient:
    """一个只带冲突产物的合成 run —— 用来把两类冲突**分别**渲染出来。

    为什么不复用 ``client``：mock 流水线跑的是干净样本，量纲闸门一条都不触发，
    两类冲突的模板分支里只有一支会被执行到。
    """
    root = tmp_path_factory.mktemp("conf-runs") / "conf"
    (root / "facts").mkdir(parents=True)
    (root / "facts" / "facts.json").write_text(
        json.dumps({
            "facts": [{
                "fact_id": "fct_synth_0001", "entity": "601857",
                "metric": "cash_and_equivalents", "value": "20616200000000",
                "unit": "元", "source_layer": "L1_text_layer", "confidence": 0.95,
                "extractor": "table_grid", "evidence_span_ids": ["evd_0001"],
                "needs_review": True,
            }],
            "evidence": [{
                "span_id": "evd_0001", "doc_id": "doc_synth", "page": 1,
                "quote": "货币资金 20,616,200", "source_layer": "L1_text_layer",
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "facts" / "internal_conflicts.json").write_text(
        json.dumps([
            {   # 同一张表里同一科目两个值 —— 原样保留「相差 N×」的说法
                "doc_id": "doc_synth", "page": 8, "entity": "601857",
                "metric": "total_assets", "period": "2025A", "ratio": 3.5,
                "values": ["3500000000", "1000000000"],
                "fact_ids": ["fct_synth_0001", "fct_synth_0002"],
            },
            {   # 量纲闸门 —— values[1] 是**锚点**，不是同一个科目的另一个读数
                "kind": "scale_implausible", "doc_id": "doc_synth", "page": 3,
                "entity": "601857", "metric": "cash_and_equivalents",
                "period": "2025A", "ratio": 7.29,
                "values": ["20616200000000", "2828017000000"],
                "fact_ids": ["fct_synth_0001", "fct_synth_0003"],
            },
        ], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (root / "manifest.json").write_text(
        json.dumps({"pipeline": "synth", "status": "success",
                    "started_at": "2026-01-01T00:00:00Z"}, ensure_ascii=False),
        encoding="utf-8",
    )
    return TestClient(create_app(runs_root=root.parent))


def test_facts_page_separates_scale_gate_from_same_table_conflicts(
    conflicts_client: TestClient,
) -> None:
    """量纲闸门的命中不能说成「同一科目有两个值」——那是**另一回事**。

    ★ 闸门记录里 ``values[1]`` 是**同期锚点指标**（总资产 / 营业收入），
      不是同一科目的第二个读数。按同表冲突的话术渲染，页面就会宣称
      「货币资金有两个值、相差 7.29×」—— 而事实是只读到**一个**货币资金，
      它比总资产还大，所以判为量纲错误。读者照着前一种说法去核对原文，
      永远对不上。
    """
    html = conflicts_client.get("/facts").text

    # 量纲闸门这一支：必须点明「锚点」与「量纲错误」
    assert "是同期锚点指标" in html, "量纲闸门命中没有按闸门的话术渲染"
    assert "量纲错误" in html
    # 并且必须显式把两类**区分开**，不能混在一句里
    assert "同一科目有两个值" in html, "页面没有说明量纲问题与同表冲突的区别"

    # 同表冲突那一支：仍然保留「相差 N×」
    assert "相差" in html, "同表冲突的原有话术被弄丢了"
    assert "3.5" in html, "同表冲突的倍数没渲染出来"

    # 闸门记录**不得**出现在同表冲突的清单里 —— 两句断言各查各的块，
    # 混渲染会同时满足上面几条，所以再钉一次归属。
    assert html.count("是同期锚点指标") == 1, (
        "闸门记录被渲染了多次（两类分支同时命中）"
    )


# ==========================================================================
# 选题 1 与选题 4/5 的材料不同 —— 各读各的 run
# ==========================================================================

@pytest.fixture(scope="module")
def two_runs(demo_run: Path, project_root: Path) -> tuple[Path, str, str]:
    """在同一个 runs_root 下再补一个**只有 R1 产物**的 run。

    为什么要两个：选题 1 与选题 4/5 的材料本来就不同（通知 p2 点名选题 1 用
    公告，p3 要求选题 4/5 依据财务报告）。只用 ``web-demo`` 一个 run 测，
    「默认值到底走了哪个 run」这条断言恒真 —— 两个 run id 相同时，
    路由对不对根本看不出来。
    """
    import subprocess
    import sys

    r = subprocess.run(
        [
            sys.executable, "-m", "agent.cli", "run", "r1_extract",
            "--mock", "--run-id", "r1-only",
            "--runs-root", str(demo_run), "--quiet",
        ],
        cwd=project_root, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, f"补 run 失败：\n{r.stdout}\n{r.stderr}"
    return demo_run, "web-demo", "r1-only"


def _run_in(body: str) -> str:
    """从页脚/状态条里取这一页实际读的 run 目录名。"""
    m = re.search(r"runs/([\w.\-]+)/", body)
    assert m, "页面里找不到 runs/<run_id>/ —— 页脚契约变了？"
    return m.group(1)


def test_r1_pages_default_to_the_r1_run(two_runs: tuple[Path, str, str]) -> None:
    """``r1_run`` 只该改**默认值**，而且只改选题 1 那两页。"""
    root, fin, r1 = two_runs
    c = TestClient(create_app(runs_root=root, default_run=fin, r1_run=r1))

    assert _run_in(c.get("/facts").text) == r1, "/facts 没有默认到 r1_run"
    assert _run_in(c.get("/extract").text) == r1, "/extract 没有默认到 r1_run"
    # 选题 4/5 必须**不受影响**：它们吃的是定期报告的财务数据，
    # 喂公告根本做不出估值（通知 p3「根据财务数据、经营假设和可比公司信息」）。
    assert _run_in(c.get("/valuation").text) == fin, "/valuation 被 r1_run 带跑了"
    assert _run_in(c.get("/audit").text) == fin, "/audit 被 r1_run 带跑了"


def test_explicit_run_still_wins_over_the_r1_default(
        two_runs: tuple[Path, str, str]) -> None:
    """★ 显式给了 ``?run=`` 就必须听它的 —— 否则顶部运行选择器在选题 1 的页面上失灵。

    这条比「默认值对不对」更要紧：选择器点了没反应，读者会以为那个 run 不存在。
    """
    root, fin, r1 = two_runs
    c = TestClient(create_app(runs_root=root, default_run=fin, r1_run=r1))
    assert _run_in(c.get(f"/facts?run={fin}").text) == fin, (
        "?run= 被 r1_run 覆盖了 —— 运行选择器在 /facts 上会失灵"
    )


def test_nav_sends_topic1_links_to_the_r1_run(two_runs: tuple[Path, str, str]) -> None:
    """在选题 4 的页面上，导航里的「事实库溯源」不能带着估值的 run 过去。"""
    root, fin, r1 = two_runs
    c = TestClient(create_app(runs_root=root, default_run=fin, r1_run=r1))
    body = c.get("/valuation").text
    assert f'href="/facts?run={r1}"' in body, (
        "在 /valuation 上，导航的选题 1 链接没有指向 r1_run"
    )
    assert f'href="/valuation?run={fin}"' in body, (
        "导航把选题 4 自己的 run 弄丢了"
    )


def test_overview_names_the_source_run_of_each_card(
        two_runs: tuple[Path, str, str]) -> None:
    """总览三张卡可能读两个 run，来源必须写在卡上，不能让读者默认同源。"""
    root, fin, r1 = two_runs
    c = TestClient(create_app(runs_root=root, default_run=fin, r1_run=r1))
    body = c.get("/").text
    assert re.findall(r"来源 run <code>([\w.\-]+)</code>", body) == [r1, fin, fin]


def test_material_scope_numbers_come_from_the_artifacts(
        client: TestClient, demo_run: Path) -> None:
    """★ 材料范围表必须**现算** —— 这条测试防的是「页面照常 200，但在说反话」。

    那段 HTML 原先是一张写死的对照表，结论直接印在模板里（「非标准化公告：
    本轮未跑」之类）。写死的结论有一个很安静的失败模式：换一个 run，页面继续
    印旧结论，状态码 200、没有任何断言会红。所以这里**不比对页面自己的函数**
    （那是循环论证），而是回到 ``pages/index.json`` 自己数一遍。
    """
    import json as _json

    body = client.get("/facts").text
    pages = _json.loads(
        (demo_run / "web-demo" / "pages" / "index.json").read_text(encoding="utf-8")
    )
    route_count: dict[str, int] = {}
    for p in pages:
        route_count[p.get("route")] = route_count.get(p.get("route"), 0) + 1

    # 份数 / 页数。模板把它们包在同一个 <b> 里（「N 份 / M 页」），
    # 所以这里只锚后半截 —— 锚整串会在改份数措辞时误报。
    assert f"{len(pages)} 页</b>" in body, "材料范围表里的页数与产物对不上"
    # 三类材料里可数的两类 —— 扫描件与表格页
    # ★ 表格页只算 D_table，混合页单列（2026-09-27 修）。
    #   这里原先算 `D_table + E_mixed`，页面也印那个和 —— 看似自洽，但
    #   /library 的卡片印的是纯 D_table，同一个中文标签「表格页」在两个页面上
    #   是两个数（232 / 158）。本测试当时只盯 /facts 一个页面，抓不到跨页矛盾。
    #   现在两页同口径，混合页有独立标签，并额外断言四项之和 == 总页数。
    scanned = route_count.get("B_scanned", 0) + route_count.get("C_garbled", 0)
    tables = route_count.get("D_table", 0)
    mixed = route_count.get("E_mixed", 0)
    assert f"<b>{scanned} 页无可靠文本层</b>" in body, (
        "材料范围表里的扫描页数与 pages/index.json 的 route 对不上"
    )
    assert f"<b>{tables} 页表格页</b>" in body, (
        "材料范围表里的表格页数与 pages/index.json 的 route 对不上"
    )
    assert f"<b>{mixed} 页混合页</b>" in body, (
        "材料范围表里的混合页数与 pages/index.json 的 route 对不上 —— "
        "混合页必须单列，不能并进「表格页」"
    )
    # 四项互斥且穷尽：文本层 + 扫描件 + 表格页 + 混合页 == 总页数
    text_only = route_count.get("A_text_only", 0)
    assert text_only + scanned + tables + mixed == len(pages), (
        "四类路由之和 != 总页数 —— 有页面既没被列进材料范围表，也没被算进去"
    )

    # 通知 p2 点名的三类字段必须各占一行，一行都不能少
    for label in ("质押", "中标", "股权变动"):
        assert f"<b>{label}</b>字段</td>" in body, f"材料范围表缺了「{label}」这一行"


# ==========================================================================
# 视觉通道成本闸门 —— 用尽时必须自己说出来
# ==========================================================================


def _vlm_cap() -> int:
    """从配置读上限，不写死 —— 配置改了测试要跟着走，而不是变成假绿。"""
    import yaml

    from web.app import PROJECT_ROOT

    cfg = yaml.safe_load(
        (PROJECT_ROOT / "configs" / "routing.yaml").read_text(encoding="utf-8")
    )
    return int(cfg["vlm_trigger"]["max_calls_per_run"])


def _write_vlm_channels(run_dir: Path, *, page_scans: int, cells: list[int]) -> None:
    """往一个 run 里写最小的 ``vlm_scans`` / ``table_adjudications`` 产物。"""
    ch = run_dir / "channels"
    ch.mkdir(parents=True, exist_ok=True)
    (ch / "vlm_scans.json").write_text(
        json.dumps([{"doc_id": "d", "page": i} for i in range(page_scans)]),
        encoding="utf-8",
    )
    (ch / "table_adjudications.json").write_text(
        json.dumps([
            {"doc_id": "d", "page": i, "cells_budget": 4,
             "report": {"cells_checked": c, "errors": 0}}
            for i, c in enumerate(cells)
        ]),
        encoding="utf-8",
    )


def test_vlm_budget_counts_match_the_artifacts(tmp_path: Path) -> None:
    """已用调用数 = 整页扫描条数 + 各表 (cells_checked - errors)，与 judge.py 口径一致。"""
    run = tmp_path / "r"
    _write_vlm_channels(run, page_scans=3, cells=[4, 4, 1])
    vb = vlm_budget(RunArtifacts(run))
    assert vb["page_scans"] == 3
    assert vb["cells"] == 9
    assert vb["used"] == 12
    assert vb["exhausted"] is False, "上限没到就不该报「用尽」"


def test_vlm_budget_flags_exhaustion_at_the_cap(tmp_path: Path) -> None:
    """★ 到上限即判「用尽」—— 闸门用尽后 judge.py 会静默 break 掉后面的表，
    不留任何记录，所以这是页面上唯一能说出「覆盖是下界」的地方。"""
    cap = _vlm_cap()
    run = tmp_path / "r"
    _write_vlm_channels(run, page_scans=cap - 4, cells=[4])
    vb = vlm_budget(RunArtifacts(run))
    assert vb["used"] == cap
    assert vb["exhausted"] is True


def test_vlm_budget_never_claims_exhaustion_when_the_cap_is_unknown(
        tmp_path: Path) -> None:
    """读不到产物时 ``cap=0`` —— 必须按「无法判定」处理，不能默认「没超」。"""
    run = tmp_path / "r"
    run.mkdir()
    vb = vlm_budget(RunArtifacts(run))
    assert vb["used"] == 0
    assert vb["exhausted"] is False


@pytest.mark.parametrize("exhaust", [True, False])
def test_exhausted_vision_budget_is_disclosed_on_the_page(
        demo_run: Path, tmp_path_factory, exhaust: bool) -> None:
    """★ 这条防的是「页面照常 200，但把下界说成了全部」。

    预算用尽与没用尽各渲染一次：用尽时必须出现「预算已用尽」和真实计数，
    没用尽时**一个字都不能出现** —— 否则这句提示会变成常驻噪音，
    读者很快就不再当真。
    """
    import shutil

    root = tmp_path_factory.mktemp("vlm-cap")
    shutil.copytree(demo_run / "web-demo", root / "cap-demo")
    cap = _vlm_cap()
    if exhaust:
        _write_vlm_channels(root / "cap-demo", page_scans=cap - 3, cells=[3])
    else:
        _write_vlm_channels(root / "cap-demo", page_scans=2, cells=[3])

    body = TestClient(create_app(runs_root=root, default_run="cap-demo")).get(
        "/facts"
    ).text

    if exhaust:
        assert "视觉通道预算已用尽" in body, "闸门用尽却没在页面上说"
        assert f"{cap} / {cap}" in body or f"{cap}/{cap}" in body, (
            "预算提示里的计数与产物对不上"
        )
    else:
        assert "视觉通道预算已用尽" not in body, (
            "预算没用尽却报了「用尽」—— 常驻的警告等于没有警告"
        )


# ==========================================================================
# ★ 运行轨迹实时流（右侧调试抽屉的数据源）
#
# 这一节每条都对应一类「页面照常 200、但抽屉是坏的」缺陷：
# 运行中的 run 解析不出来、半行被当成事件、筛选后取不满、把未校验算成错误。
# ==========================================================================


def _ev(seq: int, kind: str, **kw) -> dict:
    return {"seq": seq, "ts": "2026-10-12T00:00:00Z", "kind": kind, **kw}


def _write_trace(root: Path, run_id: str, events: list[dict], *,
                 manifest: bool = False, tail: str = "") -> Path:
    """造一个只有 ``trace.jsonl`` 的 run 目录。

    ``tail`` 用来模拟"最后一行还在写"—— 一段没有换行结尾的碎片。
    """
    run = root / run_id
    run.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
    (run / "trace.jsonl").write_text(body + tail, encoding="utf-8")
    if manifest:
        (run / "manifest.json").write_text('{"pipeline": "full"}', encoding="utf-8")
    return run


def _stream(root: Path, **params) -> dict:
    return TestClient(create_app(runs_root=root)).get("/trace/stream", params=params).json()


def test_trace_stream_tails_the_last_rows(tmp_path: Path) -> None:
    """``offset=-1`` 取末尾 N 条，并按**从旧到新**给出（抽屉是往下追加的）。"""
    _write_trace(tmp_path, "tail-r1",
                 [_ev(i, "stage", extra={"status": "ok"}) for i in range(50)])
    d = _stream(tmp_path, run="tail-r1", offset=-1, limit=7)
    assert [r["seq"] for r in d["rows"]] == list(range(43, 50))
    assert d["error"] is None and d["eof"] is True
    assert d["offset"] < d["next_offset"], "尾部模式必须报出首行偏移，否则接不上后续轮询"


def test_trace_stream_forward_paging_never_repeats_or_stalls(tmp_path: Path) -> None:
    """前向分页必须严格前进且不重不漏 —— 抽屉靠它续读。

    这里盯的是「一段里一条合格行都没有时游标也必须前进」：否则客户端会拿
    同一个 offset 反复要同一段，页面看起来就是"卡住不动"。
    """
    _write_trace(tmp_path, "page-r1",
                 [_ev(i, "tool_call", tool="t.x", ok=True, args_hash="ab") for i in range(6)]
                 + [_ev(i, "stage", extra={"status": "ok"}) for i in range(6, 30)])
    off, seen = 0, []
    for _ in range(50):
        d = _stream(tmp_path, run="page-r1", offset=off, limit=4, kinds="tool_call")
        assert d["next_offset"] > off or d["eof"], "游标没前进也没到头 —— 抽屉会卡住"
        seen += [r["seq"] for r in d["rows"]]
        off = d["next_offset"]
        if d["eof"]:
            break
    assert seen == list(range(6)), f"前向分页漏行或重复：{seen}"


def test_trace_stream_filter_scans_back_for_enough_rows(tmp_path: Path) -> None:
    """带 ``kinds`` 过滤时，尾部模式要**往回翻窗**取满 limit 条。

    否则「末尾恰好全是 stage」就会让用户以为这个 run 没有工具调用 ——
    而统计里明明写着 89 条。
    """
    _write_trace(tmp_path, "filter-r1",
                 [_ev(i, "tool_call", tool="t.x") for i in range(5)]
                 + [_ev(i, "stage") for i in range(5, 400)])
    d = _stream(tmp_path, run="filter-r1", offset=-1, limit=4, kinds="tool_call")
    assert len(d["rows"]) == 4, "过滤后没有回翻窗口取满"
    assert {r["kind"] for r in d["rows"]} == {"tool_call"}
    assert d["stats"]["by_kind"]["tool_call"] == 5


def test_trace_stream_unknown_kinds_do_not_filter_everything(tmp_path: Path) -> None:
    """全是未知 kind 名 ⇒ 不过滤，而不是把结果滤空。"""
    _write_trace(tmp_path, "bogus-r1", [_ev(i, "stage") for i in range(3)])
    d = _stream(tmp_path, run="bogus-r1", offset=-1, limit=5, kinds="no_such_kind")
    assert len(d["rows"]) == 3


def test_trace_stream_ignores_a_half_written_last_line(tmp_path: Path) -> None:
    """★ 正在被写的半行不能被当成事件 —— 这是敢轮询活动文件的全部理由。

    同一个 offset 请求两次：文件末尾追加一段**没有换行**的碎片后，
    条数必须不变；把那行补完，它才出现。
    """
    run = _write_trace(tmp_path, "half-r1", [_ev(0, "stage"), _ev(1, "stage")])
    trace = run / "trace.jsonl"
    before = _stream(tmp_path, run="half-r1", offset=-1, limit=10)
    assert len(before["rows"]) == 2

    with trace.open("a", encoding="utf-8") as fh:
        fh.write('{"seq": 2, "ts": "2026-10-12T00:00:00Z", "kind": "stage"')  # 半行
    mid = _stream(tmp_path, run="half-r1", offset=-1, limit=10)
    assert len(mid["rows"]) == 2, "把半行当成事件解析了"
    assert mid["next_offset"] == before["next_offset"]

    with trace.open("a", encoding="utf-8") as fh:
        fh.write(', "extra": {}}\n')
    after = _stream(tmp_path, run="half-r1", offset=-1, limit=10)
    assert len(after["rows"]) == 3, "行写完之后应当看到它"


def test_trace_stream_counts_errors_by_kind_not_by_ok_flag(tmp_path: Path) -> None:
    """★ 「错误」只数 ``kind == "error"``，与 ``summarize_trace`` 同口径。

    实测踩过：按 ``ok is False`` 计数，把 15 条 **skipped 断言**（缺辅助数据、
    本次未校验）和 11 条 stage ``start`` 一起报成了「错误 26」——
    一次完全正常的运行在抽屉里显示成红的。两种 False 的含义与「失败」相反。
    """
    _write_trace(tmp_path, "errs-r1", [
        _ev(0, "assertion", ok=True, extra={"status": "pass"}),
        _ev(1, "assertion", ok=False, extra={"status": "skipped"}),
        _ev(2, "assertion", ok=False, extra={"status": "fail"}),
        _ev(3, "stage", ok=False, extra={"status": "start"}),   # start 不是错误
        _ev(4, "stage", ok=True, extra={"status": "ok"}),
        _ev(5, "error", ok=False, error="boom"),                # 只有这条是错误
    ])
    s = _stream(tmp_path, run="errs-r1", offset=-1, limit=50)["stats"]
    assert s["errors"] == 1, f"错误口径不对：{s}"
    assert (s["assertions_passed"], s["assertions_failed"], s["assertions_skipped"]) == (1, 1, 1), (
        "断言三态必须分开报 —— 把 skipped 并进失败，含义正好相反")
    assert s["by_kind"]["file_access"] == 0


def test_trace_stream_resolves_a_run_that_has_no_manifest_yet(tmp_path: Path) -> None:
    """★ 运行中的 run 没有 ``manifest.json``，而标准助手会把它排除掉。

    ``available_runs`` 把这类 run 标成「未完成」、``resolve_run`` 直接拒绝 ——
    抽屉要是走 ``_run_or_error``，就会**只在运行时**返回错误页，
    也就是最需要它的时候。这条测试把那个耦合钉死在案。
    """
    _write_trace(tmp_path, "live-r1", [_ev(i, "tool_call", tool="t") for i in range(3)])

    # 前提：标准助手确实拒绝它（否则这条测试就没在测东西）
    with pytest.raises(ArtifactError):
        resolve_run("live-r1", tmp_path)

    d = _stream(tmp_path, run="live-r1", offset=-1, limit=5)
    assert d["error"] is None
    assert len(d["rows"]) == 3
    assert d["live"] is True, "没有 manifest 就是『还在写』，抽屉据此决定要不要继续轮询"


def test_trace_stream_degrades_to_200_with_an_error_string(
        client: TestClient, tmp_path: Path) -> None:
    """★ 抽屉挂在每一页上 —— 它一炸就是全站白屏，所以这一层绝不抛。"""
    for params in (
        {"run": "no-such-run"},
        {"run": "../../etc"},
        {"run": ""},
        {"run": "ok-but-absent", "offset": -999},
    ):
        r = client.get("/trace/stream", params=params)
        assert r.status_code == 200, f"{params} 不该 500"
        body = r.json()
        assert body["rows"] == []
        assert body["error"], f"{params} 必须给出可读的错误说明"
