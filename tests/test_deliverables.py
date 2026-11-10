"""交付物测试 —— 计划书、源码包、一键复现。

**为什么交付物也要测**：本项目的一条教训是「文档也是产物，也必须被测试」
（见 `tests/test_static_health.py` 的成因说明：曾经有三处文档声明了不存在的模块，
运行期不报错、测试全绿，但评审按图索骥点开目录一眼就发现）。

交付物是同一类东西，而且**后果更重**：
计划书 PDF 少了一节、源码包漏了 `domain/prompts/`，都要等到评审那边才暴露。
所以这里对三件事各钉一条：
    1. 计划书 PDF 能从 MD 重新生成，且**每一节都真的在 PDF 里**（防静默丢内容）
    2. 源码包的打包策略**包含该包含的、排除该排除的**（防漏 + 防泄漏）
    3. 一键复现脚本**自己先全绿**（评审入口不能是坏的）
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# ==========================================================================
# 一、计划书 PDF
# ==========================================================================


@pytest.mark.slow
def test_plan_pdf_regenerates_with_all_sections(tmp_path: Path, project_root: Path) -> None:
    """★ 计划书 PDF 必须能重新生成，且**每一节都在**。

    防的是两类事故：
        * 渲染器静默丢掉放不下的内容（``insert_textbox`` 放不下时返回负值并丢弃）；
        * MD 改了、PDF 忘了重新生成，交上去的是旧版本。
    """
    import fitz

    md = project_root / "deliverables" / "项目计划书.md"
    if not md.exists():
        pytest.skip("缺少 deliverables/项目计划书.md")

    out = tmp_path / "plan.pdf"
    r = subprocess.run(
        [sys.executable, "tools/make_plan_pdf.py", "--md", str(md), "--pdf", str(out)],
        cwd=project_root, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert out.exists() and out.stat().st_size > 20_000, "PDF 太小，疑似渲染失败"

    doc = fitz.open(out)
    try:
        raw = "".join(p.get_text() for p in doc)
        pages = doc.page_count
    finally:
        doc.close()

    # PDF 抽回的文本里，空格常被写成不换行空格（\xa0），行内还会换行 —— 
    # 比对前先做「去空白归一」，否则断的是排版噪声而不是内容。
    import re

    text = re.sub(r"\s+", "", raw)

    assert pages >= 8, f"只渲染出 {pages} 页，疑似大量内容被丢弃"
    # 首、中、尾各取一节，覆盖"丢中间"与"丢结尾"两种失败形态
    for section in ("项目概述", "系统方案", "验证与实测数据",
                    "工程实现与可复现性", "已知限制"):
        assert section in text, f"计划书 PDF 里找不到「{section}」—— 疑似内容被丢弃"
    # 关键内容必须落在 PDF 里（否则交付物与仓库事实脱节）。
    # 测试数**从 MD 里现取**，不写死 —— 写死会随仓库更新腐烂
    # （第一版写死 522，改了 MD 之后这条测试立刻变红，正是它该有的行为）。
    m = re.search(r"(\d+)\s*passed", md.read_text(encoding="utf-8"))
    assert m, "计划书里没写测试数"
    for token in (m.group(1), "reproduce_all.py", "选题1"):
        assert token in text, f"计划书 PDF 缺少关键内容：{token}"


def test_plan_markdown_has_no_leftover_placeholders() -> None:
    """计划书里只允许**明确标注**的待填项（人名/学校），不允许遗留模板占位符。"""
    import re

    md = Path(__file__).resolve().parents[1] / "deliverables" / "项目计划书.md"
    if not md.exists():
        pytest.skip("缺少 deliverables/项目计划书.md")
    text = md.read_text(encoding="utf-8")

    assert "PLACEHOLDER" not in text
    assert "TODO" not in text
    assert "XXX" not in text
    # 待填写只允许出现在团队分工一节，且必须带说明
    for m in re.finditer(r"待填写", text):
        window = text[max(0, m.start() - 200): m.start() + 200]
        assert "队员姓名" in window or "指导老师" in window, (
            "出现未标注的「待填写」：交付物不能留模板占位符"
        )


# ==========================================================================
# 二、源码包打包策略
# ==========================================================================


def test_submission_package_includes_core_modules_and_excludes_secrets() -> None:
    """★ 打包策略：赛题点名的核心模块**必须在**，密钥与运行产物**必须不在**。

    这条测试同时防两种事故：
        * **漏**——漏了 `domain/prompts/`（提示词不在代码里）或
          `configs/schemas/`，评审跑起来才发现；
        * **多**——把 `runs/`（体积大、含受控材料快照）甚至 `.env`（API Key）
          一起打进去。
    """
    from tools.make_submission_package import collect, scan_secrets

    files = {f.as_posix() for f in collect(with_demo=False)}
    assert files, "打包清单为空，收集逻辑可能失效"

    # 赛题点名的核心模块（编排框架 / Tool / Prompt / Skill / MCP / 数据处理 / 日志）
    must_have = [
        "agent/runtime/loop.py",            # 编排框架
        "agent/tools/registry.py",          # Tool
        "agent/prompts/registry.py",        # Prompt
        "agent/skills/registry.py",         # Skill
        "agent/mcp/client.py",              # MCP
        "agent/trace/tracer.py",            # 日志 / 追踪
        "domain/prompts/r1_extract_financial_facts.md",
        "domain/skills/r1_extraction/SKILL.md",
        "configs/schemas/facts.schema.json",
        "README.md",
        "THIRD_PARTY.md",
        "requirements.txt",
        "docs/REVIEWER.md",
        "tools/reproduce_all.py",
        # ★ 两份**合成**测试材料（2026-10-16 加）：9 条测试的固定输入，
        #   漏了它们，解压后跑测试会看到 9 个「缺少 data/raw 下的测试材料」。
        #   真实材料仍然不进包，见下面的例外规则。
        "data/raw/sample_annual_report.pdf",
        "data/raw/sample_pledge_announcement.pdf",
    ]
    missing = [p for p in must_have if p not in files]
    assert not missing, "提交包漏了关键文件：\n  " + "\n  ".join(missing)

    # 绝不能进包的东西。
    # ★ 例外（2026-09-27）：`data/real/定期报告/_SOURCES.json` 是**溯源记录**
    #   （材料的 URL + sha256 + 抓取时间），4 KB，不含正文 —— 而 `data/real/`
    #   下的 PDF 正文（25 MB）仍然一律不进包（正文已由 `demo_materials/raw/`
    #   随包提交）。例外清单与打包脚本共用同一个常量，避免两边各写一份而漂移
    #   —— 实测就是因为没有共用，加白名单时这条守卫当场变红。
    # ★ 例外二（2026-10-16）：`data/raw/sample_*.pdf` 两份**合成样本**
    #   （`tools/make_sample_data.py` 生成，35 KB，9 条测试的输入）。
    #   同样是「点名放行、不放行整个目录」。
    from tools.make_submission_package import DATA_FILE_ALLOW, REPO

    forbidden_prefix = ("runs/", "_tmp/", "data/raw/", "data/drafts/",
                        "data/real/", "data/golden/", "agent/sample/_scratch/")
    leaked = [
        f for f in files
        if f.startswith(forbidden_prefix) and Path(f).name not in DATA_FILE_ALLOW
    ]
    assert not leaked, "提交包混入了受控材料或运行产物：\n  " + "\n  ".join(leaked[:10])

    # 例外必须是**极小的、且逐个点过名的**：否则"开个白名单"就成了绕过守卫的后门。
    # ★ 2026-10-16 起 `data/raw/sample_*.pdf` 也在例外里（两份合成样本，
    #   9 条测试的输入），所以这里不能再一刀切地禁 `.pdf` ——
    #   换个更贴题的判据：**名字必须在 DATA_FILE_ALLOW 里** + 单个 < 64 KB +
    #   合起来 < 256 KB。谁想夹带一份真实材料，得同时改这个名单，且一眼看得见。
    exceptions = [f for f in files if f.startswith(forbidden_prefix)]
    for f in exceptions:
        assert Path(f).name in DATA_FILE_ALLOW, (
            f"{f} 落在受控目录里，却不在 DATA_FILE_ALLOW 的例外名单上"
        )
        assert (REPO / f).stat().st_size < 64 * 1024, (
            f"例外文件过大，疑似受控材料正文：{f}"
        )
    total_exc = sum((REPO / f).stat().st_size for f in exceptions)
    assert total_exc < 256 * 1024, (
        f"例外合计 {total_exc} 字节 —— 白名单正在变成后门，逐条复核"
    )
    assert ".env" not in files, "提交包混入了 .env（API Key）"
    assert not any("__pycache__" in f for f in files), "提交包混入了 __pycache__"

    # 密钥扫描必须真的会报警（注入验证：这条检查不能恒真）
    assert scan_secrets(collect(with_demo=False)) == [], "清单里本来不该有密钥"

    from tools.make_submission_package import FORBIDDEN_PATTERNS
    # ⚠️ 探针字符串**拼接**而成，不写成字面量：否则这个测试文件自己会被
    # scan_secrets 命中，打包脚本一跑就中止（实测踩过，见 SUBMISSION_CHECKLIST）
    probe = "DEEPSEEK_API_KEY=" + "sk-" + "a" * 24
    assert any(p.search(probe) for p, _ in FORBIDDEN_PATTERNS), (
        "密钥扫描正则匹配不到典型的 API Key —— 这条兜底等于没有"
    )


# ==========================================================================
# 三、一键复现（评审入口）
# ==========================================================================


@pytest.mark.slow
def test_one_click_reproduce_passes_quick(tmp_path: Path, project_root: Path) -> None:
    """★ 评审入口脚本必须自己先全绿 —— 它红了，评审看到的就只有红字。

    跑 ``--quick``（跳过测试套件本身，避免测试里再跑测试的递归放大）。
    实测约 35 秒。
    """
    run_id = "repro-selftest"
    report = tmp_path / "repro.json"
    try:
        r = subprocess.run(
            [sys.executable, "tools/reproduce_all.py", "--quick",
             "--run-id", run_id, "--json", str(report)],
            cwd=project_root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=900,
        )
        assert r.returncode == 0, (
            "一键复现未全绿：\n" + (r.stdout or "")[-3000:] + (r.stderr or "")[-1000:]
        )
        data = json.loads(report.read_text(encoding="utf-8"))
        assert data["ok"] is True
        failed = [s["key"] for s in data["steps"] if not s["ok"]]
        assert not failed, f"仍有未通过步骤：{failed}"
        assert len(data["steps"]) >= 11, "复现步骤变少了，检查是否有人删了步骤"
    finally:
        # 不留垃圾：脚本会在仓库 runs/ 下创建这个 run
        shutil.rmtree(project_root / "runs" / run_id, ignore_errors=True)


def _fake_run(root: Path, name: str, *, started: str, status: str,
              provider: str, tables: bool, pipeline: str = "full") -> None:
    """在 ``root`` 下捏一个 run 目录 —— 只放 ``pick_runs`` 会看的那几样。"""
    d = root / name
    (d / "channels").mkdir(parents=True)
    (d / "valuation").mkdir()
    (d / "manifest.json").write_text(json.dumps(
        {"started_at": started, "status": status, "provider": provider,
         "pipeline": pipeline}), encoding="utf-8")
    if tables:
        (d / "channels" / "tables.json").write_text(
            json.dumps([{"doc_id": "doc_x", "page": 1}]), encoding="utf-8")


def test_the_web_step_hands_its_own_run_to_the_smoke() -> None:
    """★ 一键复现的 web 步必须**指名**刚造出来的那个 run。

    不指名，核验脚本就会去 `runs/` 里挑夹具，而挑选条件（非 mock、非崩）
    恰好把刚造出来的这个筛掉，退回到一个可能不存在的 "demo"：于是所有依赖
    run 的探针一起报红，报的是「页面渲染了什么」，真相是「这里根本没有 run
    可渲染」。实测（2026-10-16，解包后的交付包）：`❌ 10/11`，连带本文件里
    `test_one_click_reproduce_passes_quick` 一起红。
    """
    from tools.reproduce_all import build_steps

    web = [s for s in build_steps("repro-x", quick=True) if s.key == "web"]
    assert web, "一键复现里没有 web 步了"
    argv = web[0].argv
    assert "--run" in argv and argv[argv.index("--run") + 1] == "repro-x", (
        f"web 步没有把 run 指名交给核验：{argv}")


def test_the_smoke_fixture_skips_crashed_and_mock_runs(tmp_path: Path) -> None:
    """回归夹具**不许**挑到崩掉的 run 或 mock run。

    这条是补上一个真实的坑：`pick_runs()` 原先只按 `started_at` 取最新，
    只要有人往 `runs/` 里跑一个新的 run，它就自动顶掉夹具 —— 而新 run 若是
    `--mock` 自检或跑到一半崩掉的，探针会**报它自己的毛病**，跑的人却去翻代码。
    （实测：三个临时 mock run 一被选中，「双路互证」「勾稽断言」双双 ✗，
    同一份代码在 `web-20261012-233510` 上两项都 ✓。）

    夹具的顺序刻意排成「最新的最坏」：不修的话这条必红。
    """
    from tools.web_smoke import pick_runs

    _fake_run(tmp_path, "newest-crashed", started="2026-10-12T16:00:00Z",
              status="failed", provider="deepseek", tables=True)
    _fake_run(tmp_path, "newer-mock", started="2026-10-12T15:00:00Z",
              status="success", provider="mock", tables=True)
    _fake_run(tmp_path, "good", started="2026-10-12T14:00:00Z",
              status="partial", provider="deepseek", tables=True)

    tbl, full = pick_runs(tmp_path)
    assert (tbl, full) == ("good", "good"), (
        f"夹具挑到了 {tbl!r}/{full!r} —— 崩掉的 run 与 mock run 都不能当夹具"
    )


def test_the_smoke_fixture_falls_back_when_everything_is_filtered(
    tmp_path: Path,
) -> None:
    """筛空了要**退回默认值**，不是把空串交给调用方。

    「筛掉了」和「没有 run」是两回事，但都不能让 `create_app(default_run="")`
    这种半坏状态发生 —— 那会在页面上变出一个不存在的 run 选择项。
    """
    from tools.web_smoke import pick_runs

    _fake_run(tmp_path, "only-mock", started="2026-10-12T15:00:00Z",
              status="success", provider="mock", tables=True)
    assert pick_runs(tmp_path) == ("demo", "demo")
    assert pick_runs(tmp_path / "does-not-exist") == ("demo", "demo")


def test_data_dependent_probes_drop_out_on_a_mock_only_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 数据相关的探针在没有那类事实的 run 上必须**撤下**，不能报红。

    守的是「解压源码包 → `python tools/reproduce_all.py` → 全绿」这条路：包内
    `runs/` 是空的，脚本只造得出一个 mock run（事实全部来自 `rule_engine`）。
    `双路互证` 那一块要 `crosscheck.model_values > 0` 才渲染，在该 run 上
    **必不出现** —— 它若留在检查清单里，交付包永远 `❌ 10/12`。

    实测（2026-10-16）：解包后 web 步报 `✗ 双路互证`，连带包内自己的
    `test_one_click_reproduce_passes_quick` 一起红。
    """
    from tools.web_smoke import facts_probe_plan

    run = tmp_path / "runs" / "r"
    (run / "facts").mkdir(parents=True)
    (run / "facts" / "facts.json").write_text(json.dumps(
        {"facts": [{"extractor": "rule_engine@1.0.0"}]}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    probes, inapplicable = facts_probe_plan("r")
    assert probes == ["抽取路径构成", "确定性优先"], (
        f"该 run 没有那类事实，探针却仍在清单里：{probes} —— 这会给假失败")
    assert [label for label, _ in inapplicable] == ["table_grid", "双路互证"]
    assert all(why for _, why in inapplicable), "撤下的探针必须说清理由"

    # 撤下是**有条件的**，不是永久删掉：真实 run 上两项都要回到清单里。
    (run / "facts" / "facts.json").write_text(json.dumps({"facts": [
        {"extractor": "rule_engine@1.0.0"},
        {"extractor": "table_grid@1.0.0"},
        {"extractor": "vlm_page@1.0.0"},
    ]}), encoding="utf-8")
    probes, inapplicable = facts_probe_plan("r")
    assert probes == ["抽取路径构成", "确定性优先", "table_grid", "双路互证"]
    assert inapplicable == []


def test_the_smoke_reads_a_pinned_run_and_notices_a_missing_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--run <id>` 与「这个 run 到底在不在」各钉一条。

    `ensure_fixture()` 靠 `has_manifest()` 决定「要不要现造一个夹具」，判错
    的后果是两种沉默：该造而不造 → 十几项假失败；不该造而造 → 往别人的
    仓库里塞一个 run。**要用真的存在与真的不存在的两个 run 各验一次** ——
    只验「不存在」的话，一个永远返回 `False` 的实现也能过。
    """
    from tools.web_smoke import has_manifest, pinned_run

    assert pinned_run(["--in-process", "--run", "repro-x"]) == "repro-x"
    assert pinned_run(["--run=repro-x"]) == "repro-x"
    assert pinned_run(["--in-process"]) == "", "没给 --run 时必须返回空串，不是猜一个"

    (tmp_path / "runs" / "yes").mkdir(parents=True)
    (tmp_path / "runs" / "yes" / "manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "runs" / "half").mkdir()
    monkeypatch.chdir(tmp_path)
    assert has_manifest("yes") is True
    assert has_manifest("half") is False, "没有 manifest.json 的是没跑完的 run，不算可用"
    assert has_manifest("no-such-run-anywhere") is False

