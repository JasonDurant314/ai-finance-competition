"""「开始分析」页 —— Web 端唯一的**写入口**的测试。

这个功能与其它八个页面有本质区别：那些页面**只读产物**，出错了顶多显示空状态；
这一页会**真的启动一条流水线**，跑真模型、花真钱、写 `runs/`。
因此它的每条护栏都要有测试：

    材料为空        → 拒绝启动，并告诉用户把 PDF 放哪
    没勾费用确认    → 拒绝启动（这是点击与真金白银之间唯一的闸门）
    已有任务在跑    → 拒绝启动（同一个 run 目录同时只能有一个写者）
    run 名已存在    → 拒绝启动（不覆盖别人跑完的结果）
    正常启动        → 真的跑完整 11 个阶段，跑完给出八个页面的链接

最后一条用 `--mock` 跑（零成本、零联网），验的是**同一条编排路径**：
子进程 argv、阶段清单、产物目录结构都与真实模型一致。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web import analyze as az
from web.app import create_app


# ==========================================================================
# 夹具：把收件目录与任务状态都指到 tmp（不碰仓库里的 inbox/ 与 _analyze/）
# ==========================================================================


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """把 analyze 模块的目录常量整体搬到 tmp。

    ⚠️ 必须连 `STATE_PATH` / `LOG_DIR` 一起搬：它们是**模块级常量**，
    只搬 `INBOX` 会让任务状态写回仓库里的 `_analyze/state.json`，
    从而污染开发机上"当前有没有任务在跑"的判断（实测过一次，很迷惑）。
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
    return {"inbox": inbox, "runs": runs, "analyze": analyze_dir, "tmp": tmp_path}


def _drop_pdf(inbox: Path, project_root: Path, name: str = "sample.pdf") -> Path:
    """往收件目录放一份真实存在的 PDF（用仓库自带的合成样本）。"""
    inbox.mkdir(parents=True, exist_ok=True)
    src = project_root / "data" / "samples" / "sample_pledge_announcement.pdf"
    if not src.exists():
        pytest.skip("缺少 data/samples/ 下的合成样本")
    dst = inbox / name
    shutil.copy2(src, dst)
    return dst


# ==========================================================================
# 一、收件目录清单
# ==========================================================================


def test_inventory_on_empty_inbox(sandbox: dict[str, Path]) -> None:
    inv = az.inventory()
    assert inv["pdf_count"] == 0
    assert inv["est_minutes"] == 0.0 and inv["est_cny"] == 0.0
    assert (sandbox["inbox"] / "README.md").exists(), "首次访问应自动生成收件目录与说明"


def test_inventory_counts_pdfs_and_pages(sandbox: dict[str, Path],
                                         project_root: Path) -> None:
    _drop_pdf(sandbox["inbox"] / "raw", project_root)
    inv = az.inventory()
    assert inv["pdf_count"] == 1
    assert inv["pdfs"][0]["name"] == "sample.pdf"
    assert inv["pdfs"][0]["pages"], "应能数出页数（用于估算耗时与费用）"
    assert inv["est_minutes"] > 0 and inv["est_cny"] > 0


def test_pdfs_in_inbox_root_are_found_too(sandbox: dict[str, Path],
                                          project_root: Path) -> None:
    """PDF 直接丢在 inbox/ 根下也要能找到（README 里承诺过）。"""
    _drop_pdf(sandbox["inbox"], project_root, name="root-level.pdf")
    inv = az.inventory()
    assert inv["pdf_count"] == 1
    assert inv["pdfs"][0]["rel"] == "root-level.pdf"


def test_inventory_warns_when_target_has_no_comps_snapshot(
    sandbox: dict[str, Path], project_root: Path
) -> None:
    """★ 开跑**之前**就要警告"这个主体没有可比公司快照"。

    R4 的快照按主体命名（`data/comps/<代码>_<年份>A.json`），缺了它会
    `CompsDataError` **显式失败**（刻意如此：宁可失败，也不让报告里出现编造的倍数）。
    用户丢进来的第一份材料如果不在快照覆盖范围里，**十几分钟后**才会看到失败 ——
    文件名里的 6 位代码足够提前发现这件事。
    """
    _drop_pdf(sandbox["inbox"] / "raw", project_root,
              name="000722_湖南发展_2025年年度报告.pdf")
    inv = az.inventory()
    assert inv["codes_in_names"] == ["000722"]
    assert inv["codes_without_comps"] == ["000722"]

    client = TestClient(create_app(runs_root=sandbox["runs"]))
    html = client.get("/analyze").text
    assert "R4 可能取不到可比公司快照" in html
    assert "fetch_comps.py --target 000722" in html, (
        "光说有问题不够，必须给出补齐快照的确切命令"
    )


def test_inventory_no_warning_when_comps_snapshot_exists(
    sandbox: dict[str, Path], project_root: Path
) -> None:
    """反向：有了对应快照就不该再报警（否则警告会变成没人看的噪声）。"""
    src = project_root / "data" / "comps" / "601857_2025A.json"
    if not src.exists():
        pytest.skip("缺少 data/comps/601857_2025A.json")
    dst = sandbox["inbox"] / "comps"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst / src.name)
    _drop_pdf(sandbox["inbox"] / "raw", project_root,
              name="601857_中国石油_2025年年报.pdf")
    inv = az.inventory()
    assert inv["codes_in_names"] == ["601857"]
    assert inv["codes_without_comps"] == []
    client = TestClient(create_app(runs_root=sandbox["runs"]))
    assert "R4 可能取不到可比公司快照" not in client.get("/analyze").text


# ==========================================================================
# 二、护栏
# ==========================================================================


def test_start_refuses_without_materials(sandbox: dict[str, Path]) -> None:
    with pytest.raises(az.AnalyzeError, match="没有 PDF"):
        az.start(confirmed=True, runs_root=sandbox["runs"])


def test_start_refuses_without_cost_confirmation(sandbox: dict[str, Path],
                                                 project_root: Path) -> None:
    _drop_pdf(sandbox["inbox"] / "raw", project_root)
    with pytest.raises(az.AnalyzeError, match="勾选"):
        az.start(confirmed=False, runs_root=sandbox["runs"])
    assert not az.running_task(), "被拒绝的启动不该留下任务状态"


def test_start_refuses_when_another_task_is_running(sandbox: dict[str, Path],
                                                    project_root: Path) -> None:
    """正在跑的任务必须拦住第二次点击。

    这里模拟的是**真实运行中的形态**：run 目录已建、trace.jsonl 正在被写。
    为什么不只写一个 pid：在受限环境里 pid 探测可能完全不可用
    （实测沙箱里 `tasklist` 返回 `ERROR: Access denied`），
    那时判据必须落在"产物还在动"上，否则用户会在一次真实运行还在写盘时又点一次。
    """
    _drop_pdf(sandbox["inbox"] / "raw", project_root)
    sandbox["analyze"].mkdir(parents=True, exist_ok=True)
    run_dir = sandbox["runs"] / "someone-else"
    run_dir.mkdir(parents=True)
    (run_dir / "trace.jsonl").write_text('{"seq": 0}\n', encoding="utf-8")
    az.STATE_PATH.write_text(
        json.dumps({"run_id": "someone-else", "pid": os.getpid(),
                    "started_at": az._now_iso()}),
        encoding="utf-8",
    )
    assert az.running_task(runs_root=sandbox["runs"]) is not None
    with pytest.raises(az.AnalyzeError, match="已经有一个分析在跑"):
        az.start(confirmed=True, runs_root=sandbox["runs"])


def test_stale_task_is_not_reported_as_running(sandbox: dict[str, Path]) -> None:
    """★ 反向：进程没了、trace 也不动了，就不能再拦着用户。

    这条与上一条是一对：**判据太松**会让用户永远点不动，
    **判据太紧**会让两个写者同时写一个 run 目录。两边都要有测试。
    """
    sandbox["analyze"].mkdir(parents=True, exist_ok=True)
    run_dir = sandbox["runs"] / "long-gone"
    run_dir.mkdir(parents=True)
    stale = run_dir / "trace.jsonl"
    stale.write_text(
        '{"ts": "2020-01-01T00:00:00Z", "kind": "stage"}\n', encoding="utf-8"
    )
    old = time.time() - (az.STALE_TRACE_S + 60)
    os.utime(stale, (old, old))
    az.STATE_PATH.write_text(
        json.dumps({"run_id": "long-gone", "pid": 999999,
                    "started_at": "2020-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    assert az.running_task(runs_root=sandbox["runs"]) is None
    assert az.status(runs_root=sandbox["runs"])["phase"] == "failed"


def test_running_run_with_stale_file_mtime_is_not_reported_failed(
    sandbox: dict[str, Path]
) -> None:
    """★ 现场事故回归：**文件 mtime 滞后 ≠ 任务失败**。

    2026-09-25 用户跑真实年报时踩到：trace.jsonl 的最后一条事件时间就是"此刻"，
    而 ``stat().st_mtime`` 在 G:（网络/虚拟盘）上滞后了 **169 秒**，
    于是页面在运行完全正常时显示「❌ 失败」，而用户正盯着它看。

    修法：判活读的是 **trace 文件内容里的事件时间戳**，不是文件系统元数据。
    这条测试把"mtime 很旧 + 事件时间很新"这个组合固定下来。
    """
    sandbox["analyze"].mkdir(parents=True, exist_ok=True)
    run_dir = sandbox["runs"] / "live-but-mtime-stale"
    run_dir.mkdir(parents=True)
    trace = run_dir / "trace.jsonl"
    trace.write_text(
        '{"ts": "%s", "kind": "llm_call"}\n' % az._now_iso(), encoding="utf-8"
    )
    old = time.time() - (az.STALE_TRACE_S + 600)      # mtime 假装是十分钟前
    os.utime(trace, (old, old))
    az.STATE_PATH.write_text(
        json.dumps({"run_id": "live-but-mtime-stale", "pid": 999999,
                    "started_at": "2020-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    assert az.running_task(runs_root=sandbox["runs"]) is not None, (
        "mtime 滞后被误判成失败 —— 这正是现场那次事故"
    )
    assert az.status(runs_root=sandbox["runs"])["phase"] == "running"


def test_start_refuses_existing_run_id(sandbox: dict[str, Path],
                                       project_root: Path) -> None:
    _drop_pdf(sandbox["inbox"] / "raw", project_root)
    (sandbox["runs"] / "dup").mkdir(parents=True)
    (sandbox["runs"] / "dup" / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(az.AnalyzeError, match="已经是一次完成的运行"):
        az.start(run_id="dup", confirmed=True, runs_root=sandbox["runs"])


def test_start_refuses_bad_run_id(sandbox: dict[str, Path], project_root: Path) -> None:
    _drop_pdf(sandbox["inbox"] / "raw", project_root)
    with pytest.raises(az.AnalyzeError, match="run 名"):
        az.start(run_id="../逃逸", confirmed=True, runs_root=sandbox["runs"])


def test_real_mode_argv_has_no_mock_flag(sandbox: dict[str, Path]) -> None:
    """★ 页面上的按钮**永远是真实模型** —— `--mock` 只给自动化测试用。

    这条测试存在的理由：整个项目的说服力建立在"页面看到的 = 命令行跑出的"，
    如果哪天有人为了"让演示快一点"在默认路径上加了 `--mock`，
    用户会以为自己在做真实分析，实际拿到的是一份假结果 ——
    这与"编造的倍数"是同一类错误，因此必须在代码层面钉住。
    """
    real = az.build_argv("r1", runs_root=sandbox["runs"])
    assert "--mock" not in real
    assert real[:4] == [os.sys.executable, "-m", "agent.cli", "run"]
    assert "full" in real and "--data" in real and "--run-id" in real

    self_test = az.build_argv("r2", runs_root=sandbox["runs"], mock=True)
    assert "--mock" in self_test, "自检模式必须显式传 --mock（否则测试会真的花钱）"


# ==========================================================================
# 三、R4 输入自动补齐
# ==========================================================================


def test_provision_r4_inputs_copies_public_snapshots(sandbox: dict[str, Path],
                                                     project_root: Path) -> None:
    """R4 缺 comps/market 会**显式失败**（刻意设计），所以启动前自动补齐。"""
    copied = az.provision_r4_inputs()
    assert copied["comps"] >= 1 and copied["market"] >= 1, (
        f"未从 data/ 补齐 R4 输入：{copied}"
    )
    assert list((sandbox["inbox"] / "comps").glob("*.json"))
    assert list((sandbox["inbox"] / "market").glob("*.json"))
    # 已有内容时不重复拷贝（幂等）
    assert az.provision_r4_inputs() == {"comps": 0, "market": 0}


# ==========================================================================
# 四、端到端：真的跑完 11 个阶段（--mock，零成本）
# ==========================================================================


def test_live_cost_is_reported_while_running(sandbox: dict[str, Path]) -> None:
    """★ 现场事故回归：运行**进行中**也必须看得到 token 与费用。

    2026-09-25 用户跑真实年报时踩到：页面一直显示「Token 0 / 费用 ¥0.0000」，
    而实际上那一刻已经调用模型 125 次、花了 ¥0.59。

    根因：这两个数读的是 ``manifest.json`` 的 ``total_tokens`` ——
    而 manifest **只在运行结束时才写**。``checkpoint.json`` 的 budget 也不够用：
    它每个阶段才更新一次，而最贵的 ``r1_judge`` 一跑就是二十多分钟。
    只有 ``trace.jsonl`` 是每次模型调用都追加的，所以它才是实时来源。

    这条测试钉住的是：**用户必须能判断"这次跑得贵不贵、要不要中途叫停"**。
    """
    sandbox["analyze"].mkdir(parents=True, exist_ok=True)
    run_dir = sandbox["runs"] / "live-cost"
    run_dir.mkdir(parents=True)
    (run_dir / "trace.jsonl").write_text(
        '{"kind": "stage", "ok": true}\n'
        '{"kind": "llm_call", "prompt_tokens": 1000, "completion_tokens": 200,'
        ' "cost_cny": 0.02, "cache_hit": false, "model": "deepseek-flash"}\n'
        '{"kind": "llm_call", "prompt_tokens": 500, "completion_tokens": 100,'
        ' "cost_cny": 0.01, "cache_hit": true, "model": "deepseek-flash"}\n',
        encoding="utf-8",
    )
    az.STATE_PATH.write_text(
        json.dumps({"run_id": "live-cost", "pid": 999999,
                    "started_at": az._now_iso()}),
        encoding="utf-8",
    )
    st = az.status(runs_root=sandbox["runs"])
    assert st["phase"] == "running"
    rs = st["run_summary"]
    assert rs["total_tokens"] == 1800, f"运行中的 token 没统计出来：{rs}"
    assert rs["total_cost_cny"] == pytest.approx(0.03), f"运行中的费用没统计出来：{rs}"
    assert rs["llm_calls"] == 2
    assert rs["cache_hits"] == 1
    assert rs["live"] is True


def test_live_stats_are_incremental_and_not_double_counted(
    sandbox: dict[str, Path]
) -> None:
    """增量累计：第二次轮询不能把已读过的行再算一遍。

    页面每 3 秒轮询一次，一次真实运行会追加几万条事件 ——
    既不能重复计数，也不能每次都把整个 trace 重读一遍。
    """
    sandbox["analyze"].mkdir(parents=True, exist_ok=True)
    run_dir = sandbox["runs"] / "incremental"
    run_dir.mkdir(parents=True)
    trace = run_dir / "trace.jsonl"
    trace.write_text(
        '{"kind": "llm_call", "prompt_tokens": 100, "completion_tokens": 0,'
        ' "cost_cny": 0.01, "cache_hit": false}\n',
        encoding="utf-8",
    )
    az.STATE_PATH.write_text(
        json.dumps({"run_id": "incremental", "pid": 999999,
                    "started_at": az._now_iso()}),
        encoding="utf-8",
    )
    first = az.status(runs_root=sandbox["runs"])["run_summary"]
    assert first["llm_calls"] == 1 and first["total_tokens"] == 100

    with trace.open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "llm_call", "prompt_tokens": 50, "completion_tokens": 10,'
                 ' "cost_cny": 0.005, "cache_hit": false}\n')
    second = az.status(runs_root=sandbox["runs"])["run_summary"]
    assert second["llm_calls"] == 2, "新增行没被读到"
    assert second["total_tokens"] == 160, f"重复计数或漏计：{second}"
    assert second["total_cost_cny"] == pytest.approx(0.015)


def test_finished_run_prefers_manifest_totals(sandbox: dict[str, Path]) -> None:
    """跑完之后以 manifest 为准（它是那次运行的权威账）。"""
    sandbox["analyze"].mkdir(parents=True, exist_ok=True)
    run_dir = sandbox["runs"] / "done-run"
    run_dir.mkdir(parents=True)
    (run_dir / "trace.jsonl").write_text(
        '{"kind": "llm_call", "prompt_tokens": 1, "completion_tokens": 1,'
        ' "cost_cny": 0.001, "cache_hit": false}\n',
        encoding="utf-8",
    )
    (run_dir / "manifest.json").write_text(
        json.dumps({"status": "success", "total_tokens": 999,
                    "total_cost_cny": 1.23, "outputs": {"a": "x", "b": "y"}}),
        encoding="utf-8",
    )
    az.STATE_PATH.write_text(
        json.dumps({"run_id": "done-run", "pid": 999999,
                    "started_at": az._now_iso()}),
        encoding="utf-8",
    )
    rs = az.status(runs_root=sandbox["runs"])["run_summary"]
    assert rs["total_tokens"] == 999 and rs["live"] is False
    assert rs["output_count"] == 2


def test_status_shape_when_idle(sandbox: dict[str, Path]) -> None:
    st = az.status(runs_root=sandbox["runs"])
    assert st["phase"] == "idle"
    assert len(st["stages"]) == 11, "阶段清单必须来自 pipeline.yaml（full 是 11 个阶段）"
    assert st["links"] == {}


@pytest.mark.slow
def test_mock_run_completes_and_links_to_pages(sandbox: dict[str, Path],
                                               project_root: Path) -> None:
    """★ 从「点一下」到「能在八个页面里看结果」的完整闭环。

    用 `--mock`：零成本、不会调真实模型，但走的是**同一条编排路径** ——
    同一份 argv、同一份阶段清单、同一个产物目录结构。
    """
    _drop_pdf(sandbox["inbox"] / "raw", project_root)
    st = az.start(run_id="web-selftest", confirmed=True, mock=True,
                  runs_root=sandbox["runs"])
    assert st["phase"] in ("running", "finished")
    assert "agent.cli run full" in st["command"]
    assert str(sandbox["runs"]) in st["command"], (
        "必须把 --runs-root 传给子进程，否则产物写到默认 runs/ 下，"
        "状态会去错误的地方找 manifest，跑完了也报失败"
    )

    deadline = time.time() + 600
    while time.time() < deadline:
        st = az.status(runs_root=sandbox["runs"])
        if st["phase"] in ("finished", "failed"):
            break
        time.sleep(2.0)

    assert st["phase"] == "finished", (
        f"分析未正常结束：phase={st['phase']}，日志末尾：\n"
        + "\n".join(st["log"][-15:])
    )
    assert st["run_status"] == "success"
    assert st["progress"] == 1.0
    names = {s["name"] for s in st["stages"]}
    assert names <= set(st["completed"]), "有阶段没跑完就结束了"

    # 产物必须与命令行同构（否则那八个页面读不到东西）
    run_dir = sandbox["runs"] / "web-selftest"
    for rel in ("manifest.json", "facts/facts.json", "valuation/valuation.json",
                "audit/audit.json", "trace.jsonl"):
        assert (run_dir / rel).exists(), f"缺少产物 {rel}"

    # 跑完必须给出八个页面的入口（用户说的「完成之后依然要在 Web 端展示」）
    for key in ("library", "extract", "facts", "valuation", "audit", "runs"):
        assert st["links"][key].endswith("run=web-selftest"), st["links"]


# ==========================================================================
# 四之二、中断与续跑
# ==========================================================================


def _interrupted_run(sandbox: dict[str, Path], run_id: str = "killed-mid") -> Path:
    """造一个「跑到一半被打断」的 run：有 checkpoint、有产物、没有 manifest。"""
    sandbox["analyze"].mkdir(parents=True, exist_ok=True)
    run_dir = sandbox["runs"] / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "checkpoint.json").write_text(
        json.dumps({"pipeline": "full",
                    "completed": ["r1_ingest", "r1_extract_text"],
                    "failed": [], "skipped": [], "budget": {}}),
        encoding="utf-8",
    )
    (run_dir / "trace.jsonl").write_text(
        '{"ts": "2020-01-01T00:00:00Z", "kind": "stage"}\n', encoding="utf-8"
    )
    az.STATE_PATH.write_text(
        json.dumps({"run_id": run_id, "pid": 999999,
                    "started_at": "2020-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    return run_dir


def test_interrupted_run_is_resumable_not_failed(sandbox: dict[str, Path]) -> None:
    """★ 现场事故回归：**中断 ≠ 失败**，前者一个 --resume 就能接着跑。

    2026-09-25 用户跑真实年报时，不小心在跑分析的那个窗口按了 Ctrl+C
    （我当时让他重启的是 Web 服务窗口）。日志里写得很清楚：「中断。已完成的阶段
    保存在 run 目录中，可用 --resume 续跑。」—— 13 分钟、¥0.75 的工作**都还在**，
    但页面报「❌ 失败」，看上去像是全白跑了。

    两者的处置完全不同，所以必须分开显示：
        中断 → 点「继续未完成的运行」（跳过已完成阶段 + 命中模型缓存）
        失败 → 查原因
    """
    _interrupted_run(sandbox)
    st = az.status(runs_root=sandbox["runs"])
    assert st["phase"] == "interrupted", f"中断被误报成 {st['phase']}"
    assert st["resumable"] is True
    assert st["completed"] == ["r1_ingest", "r1_extract_text"]


def test_resume_argv_has_resume_flag(sandbox: dict[str, Path]) -> None:
    """续跑走的是同一条命令 + `--resume`（跳过已完成阶段、复用缓存）。"""
    argv = az.build_argv("killed-mid", runs_root=sandbox["runs"], resume=True)
    assert "--resume" in argv
    assert "--mock" not in argv, "续跑仍然是真实模型"
    assert az.build_argv("x", runs_root=sandbox["runs"]).count("--resume") == 0


def test_start_refuses_to_redo_an_interrupted_run(
    sandbox: dict[str, Path], project_root: Path
) -> None:
    """已有断点时**不许原地新建** —— 那会把做过的事再做一遍，白花钱。"""
    _drop_pdf(sandbox["inbox"] / "raw", project_root)
    _interrupted_run(sandbox)
    with pytest.raises(az.AnalyzeError, match="继续未完成的运行"):
        az.start(run_id="killed-mid", confirmed=True, runs_root=sandbox["runs"])


def test_resume_requires_an_existing_checkpoint(sandbox: dict[str, Path],
                                                project_root: Path) -> None:
    """没有断点却说"续跑" → 明确拒绝，而不是悄悄从头跑一遍。"""
    _drop_pdf(sandbox["inbox"] / "raw", project_root)
    with pytest.raises(az.AnalyzeError, match="没有可续跑的断点"):
        az.start(run_id="never-started", confirmed=True, resume=True,
                 runs_root=sandbox["runs"])


@pytest.mark.slow
def test_resume_skips_completed_stages_end_to_end(sandbox: dict[str, Path],
                                                  project_root: Path) -> None:
    """★ 端到端：真的续跑一次，已完成的阶段不被重算。

    做法：先用 --mock 跑一遍（零成本），删掉 manifest 与后段产物把它变成"中断态"，
    再 --resume；判据是**续跑日志里出现「断点续跑」**且最终仍能跑完。
    """
    _drop_pdf(sandbox["inbox"] / "raw", project_root)
    az.start(run_id="resume-e2e", confirmed=True, mock=True, runs_root=sandbox["runs"])
    deadline = time.time() + 600
    while time.time() < deadline:
        if az.status(runs_root=sandbox["runs"])["phase"] in ("finished", "failed"):
            break
        time.sleep(2.0)
    assert az.status(runs_root=sandbox["runs"])["phase"] == "finished"

    run_dir = sandbox["runs"] / "resume-e2e"
    manifest = run_dir / "manifest.json"
    done_before = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    manifest.unlink()                                  # 伪装成"中断"
    # 刚跑完的 trace 时间戳是"此刻"，会被判成"还在跑"（这是刻意的：
    # 假"运行中"只是让人多等，假"失败"会让人以为白跑了）。
    # 这里追加一条**时间戳很旧**的事件，并把启动时间也改成很久以前 ——
    # 真实的"中断"必然已经过了 90 秒的启动宽限期。
    with (run_dir / "trace.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "2020-01-01T00:00:00Z", "kind": "stage", "stage": "__interrupted__"}\n')
    az.STATE_PATH.write_text(
        json.dumps({"run_id": "resume-e2e", "pid": 999999,
                    "started_at": "2020-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    assert az.status(runs_root=sandbox["runs"])["phase"] == "interrupted"

    st = az.start(run_id="resume-e2e", confirmed=True, mock=True, resume=True,
                  runs_root=sandbox["runs"])
    assert "--resume" in st["command"]
    deadline = time.time() + 600
    while time.time() < deadline:
        st = az.status(runs_root=sandbox["runs"])
        if st["phase"] in ("finished", "failed"):
            break
        time.sleep(2.0)
    assert st["phase"] == "finished", "\n".join(st["log"][-15:])
    assert "断点续跑" in "\n".join(st["log"]), (
        "续跑日志里没有出现「断点续跑」——很可能实际上是从头跑的"
    )
    assert set(done_before["completed"]) <= set(st["completed"])


# ==========================================================================
# 五、HTTP 路由
# ==========================================================================


def test_analyze_page_renders_inventory(sandbox: dict[str, Path],
                                        project_root: Path) -> None:
    _drop_pdf(sandbox["inbox"] / "raw", project_root)
    client = TestClient(create_app(runs_root=sandbox["runs"]))
    r = client.get("/analyze")
    assert r.status_code == 200
    assert "开始分析" in r.text
    assert "sample.pdf" in r.text, "材料清单要显示出来（否则用户不知道系统看到了什么）"
    assert "真实模型" in r.text, "必须写明这会调用真实模型并产生费用"


def test_analyze_page_warns_when_inbox_empty(sandbox: dict[str, Path]) -> None:
    client = TestClient(create_app(runs_root=sandbox["runs"]))
    r = client.get("/analyze")
    assert r.status_code == 200
    assert "还没有 PDF" in r.text


def test_analyze_start_route_refuses_without_confirmation(
    sandbox: dict[str, Path], project_root: Path
) -> None:
    _drop_pdf(sandbox["inbox"] / "raw", project_root)
    client = TestClient(create_app(runs_root=sandbox["runs"]))
    r = client.post("/analyze/start", data={"run_id": "nope", "confirm": ""})
    assert r.status_code == 200
    assert "勾选" in r.text
    assert not (sandbox["runs"] / "nope").exists(), "未确认却建了 run 目录"


def test_analyze_status_is_json(sandbox: dict[str, Path]) -> None:
    client = TestClient(create_app(runs_root=sandbox["runs"]))
    r = client.get("/analyze/status")
    assert r.status_code == 200
    body = r.json()
    assert body["phase"] == "idle"
    assert isinstance(body["stages"], list)


def test_analyze_page_has_model_probe_button(sandbox: dict[str, Path]) -> None:
    """开跑前要能花十几 token 确认模型可用 —— 否则要等 18 分钟才发现 Key 过期。"""
    client = TestClient(create_app(runs_root=sandbox["runs"]))
    html = client.get("/analyze").text
    assert "测试模型连接" in html
    assert "/analyze/probe-model" in html


def test_probe_model_route_reports_result(sandbox: dict[str, Path],
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """路由的契约（**不真的调用模型** —— 测试不该花钱）。"""
    monkeypatch.setattr(
        az, "probe_model",
        lambda timeout_s=30.0: {"ok": True, "model": "deepseek-flash",
                                "provider": "deepseek", "elapsed_s": 0.9,
                                "text": "正常", "tokens": 10, "error": None},
    )
    client = TestClient(create_app(runs_root=sandbox["runs"]))
    r = client.post("/analyze/probe-model")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["tokens"] == 10


def test_probe_model_surfaces_failure(sandbox: dict[str, Path],
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """失败必须**如实展示原因**，不能吞掉变成"连接正常"。"""
    monkeypatch.setattr(
        az, "probe_model",
        lambda timeout_s=30.0: {"ok": False, "model": "m", "provider": "p",
                                "elapsed_s": 1.2, "text": "", "tokens": 0,
                                "error": "MissingCredentialError: 缺少 API Key"},
    )
    client = TestClient(create_app(runs_root=sandbox["runs"]))
    body = client.post("/analyze/probe-model").json()
    assert body["ok"] is False
    assert "缺少 API Key" in body["error"]

