"""断点续跑（``--resume``）不得改变运行身份 —— 否则回放报的是**假警报**。

================================================================================
为什么单独一个文件
================================================================================
现场演示档的流程是「赛前 ``--warm`` 预热 → 现场 ``--live --resume`` 秒级返回」。
实测这条流程**原本必然报「❌ 不可复现」**，原因有三个，都在这里钉住：

1. **续跑重新生成了运行身份。**
   ``build_context`` 每次调用都新建 manifest：``started_at`` 取当前时刻、
   ``doc_limit`` 按**本次命令行**重记。于是现场那一轮把 manifest 改写成
   现场时刻 + ``doc_limit=None``，而盘上产物仍是赛前那一刻的
   （``facts.json.built_at`` = 赛前）。
   ``replay`` 用现场时刻重建上下文 → 748 个产物**每一个**哈希都不同。

2. **续跑把不属于任何阶段的散件收进了产物清单。**
   ``_finalize_manifest`` 会 rglob 整个 run 目录补哈希。``audit.md`` 是 CLI 在
   loop 结束**之后**才写的，续跑时它已在盘上 → 被收进 ``outputs``；
   而回放只跑阶段，永远产不出它 → 「回放未产出：audit.md」。

3. ★★ **续跑丢掉了只活在内存里的状态 —— 而且不报错。**
   `extract_text` 是唯一把「权威页面原文 / 块级坐标 / 全文报表标题」注册进
   ``ctx.store`` 的阶段，``build_facts`` 则把事实放进 ``ctx.ledger``。
   两者都是**纯内存对象**。跳过 `extract_text` 之后：

       build_facts 读 ctx.store.page_text(...) → ""
       → find_unit_declaration("") 找不到「单位：万元」
       → 每一行都按「无单位」拒绝 → **0 条事实**，退出码 0

   实测（annual41-seg 复制一份、回退检查点重跑 judge 起）：
   事实 **1050 → 0**，而 ``channels/*.json`` **逐字节相同** ——
   差异全在内存里，产物上看不出来。这条比前两条严重得多：
   前两条只是「报告说错了」，这条是「结果悄悄变成空的」。

两个 bug 的症状一模一样（都是「不可复现」），但根因完全不同 ——
所以断言必须分开写，一个测身份继承、一个测散件不入选。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def _cli(project_root: Path, runs_root: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "agent.cli", *argv, "--runs-root", str(runs_root)],
        cwd=project_root, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


@pytest.fixture
def materials(project_root: Path, sample_raw_dir: Path) -> Path:
    """一份真实材料目录（合成 A4 年报页），供 --limit 1 试跑。"""
    if not any(sample_raw_dir.glob("*.pdf")):
        pytest.skip("缺少 data/raw 下的测试材料")
    return sample_raw_dir


@pytest.mark.slow
def test_resume_inherits_run_identity(
    tmp_path: Path, project_root: Path, materials: Path
) -> None:
    """续跑必须继承 ``started_at`` 与 ``doc_limit``，回放才可能 🟢。"""
    runs_root = tmp_path / "runs"

    # ① 赛前预热：带头 --limit 1，只处理一份材料
    r = _cli(project_root, runs_root,
             "run", "r1_extract", "--mock", "--run-id", "live",
             "--limit", "1", "--quiet")
    assert r.returncode == 0, r.stdout + r.stderr
    mf_path = runs_root / "live" / "manifest.json"
    warm = json.loads(mf_path.read_text(encoding="utf-8"))
    assert warm["doc_limit"] == 1
    warm_started = warm["started_at"]
    built_at = json.loads(
        (runs_root / "live" / "facts" / "facts.json").read_text(encoding="utf-8")
    )["built_at"]
    assert built_at.startswith(warm_started[:19]), (
        f"产物的 built_at({built_at}) 与 manifest.started_at({warm_started}) 不一致 —— "
        "本测试的前提（产物时间戳取自 manifest.started_at）被打破了"
    )

    # ② 现场：--resume，且**不带** --limit（现场演示就是这么敲的）
    r = _cli(project_root, runs_root, "run", "r1_extract", "--mock",
             "--run-id", "live", "--resume", "--quiet")
    assert r.returncode == 0, r.stdout + r.stderr
    live = json.loads(mf_path.read_text(encoding="utf-8"))

    assert live["started_at"] == warm_started, (
        "续跑重新生成了 started_at —— 盘上产物是赛前那一刻写的，"
        "回放用现场时刻重建上下文，每个产物哈希都会不同，报假『不可复现』"
    )
    assert live["doc_limit"] == 1, (
        "续跑把 doc_limit 抹成了 None —— 回放会去处理全部材料，"
        "产物不同，又是一次假『不可复现』"
    )

    # ③ 身份继承之后，回放必须通过
    r = _cli(project_root, runs_root, "replay", "live", "--strict")
    assert "✅ 可复现" in r.stdout, (
        "预热 + 续跑之后回放不一致（这正是现场演示档要展示的那一步）：\n"
        + r.stdout[-2500:]
    )


@pytest.mark.slow
def test_resume_does_not_absorb_stray_outputs(
    tmp_path: Path, project_root: Path, materials: Path
) -> None:
    """续跑不得把「不是本次产物」的文件收进 manifest.outputs。

    ``audit.md`` 是 CLI 在 loop 之后写的，不属于任何阶段，因此**回放永远产不出它**。
    一旦它进了 outputs，回放必然报「回放未产出：audit.md」。
    """
    runs_root = tmp_path / "runs"

    r = _cli(project_root, runs_root,
             "run", "r1_extract", "--mock", "--run-id", "stray",
             "--limit", "1")
    assert r.returncode == 0, r.stdout + r.stderr

    run_dir = runs_root / "stray"
    mf_path = run_dir / "manifest.json"
    # 预热刚结束时 audit.md 还不在 outputs 里（_finalize_manifest 先于它执行）
    assert "audit.md" not in json.loads(mf_path.read_text(encoding="utf-8"))["outputs"]

    # CLI 跑完之后 audit.md 才落盘 —— 模拟「盘上已有散件」的续跑现场
    assert (run_dir / "audit.md").is_file(), "CLI 未产出 audit.md，本测试前提不成立"

    r = _cli(project_root, runs_root, "run", "r1_extract", "--mock",
             "--run-id", "stray", "--resume", "--quiet")
    assert r.returncode == 0, r.stdout + r.stderr
    outputs = json.loads(mf_path.read_text(encoding="utf-8"))["outputs"]

    assert "audit.md" not in outputs, (
        "续跑把 audit.md 收进了 outputs —— 它不是任何阶段的产物，"
        "回放必然报『回放未产出』，把一次本可复现的运行判成不可复现"
    )
    assert "replay_report.md" not in outputs

    r = _cli(project_root, runs_root, "replay", "stray", "--strict")
    assert "✅ 可复现" in r.stdout, r.stdout[-2500:]


def test_cache_first_write_wins(tmp_path: Path) -> None:
    """缓存**首次写入者胜**：不覆盖，只计数。

    为什么不是「后写覆盖」：一旦有第二个写者（实测踩过 —— 两个 warm 进程
    同时写同一个 run 名），覆盖会让回放取到**产物当时没用过**的答案，
    于是「缓存被写脏」被伪装成「运行不可复现」。首次写入者胜至少让回放
    取到第一次记下的答案，而 ``overwritten_slots > 0`` 把这件事变成可显示的信号。
    """
    from agent.model.cache import ResponseCache, cache_key

    k = cache_key(provider="p", model="m", messages=[{"role": "user", "content": "x"}],
                  params={}, json_schema=None, prompt_sha=None)
    p = tmp_path / "c.jsonl"
    c = ResponseCache(p)
    assert c.get(k) is None
    c.put(k, {"text": "第一次"})
    assert c.get(k) == {"text": "第一次"}
    c.put(k, {"text": "第二次"})          # 想覆盖 —— 应当被拒绝
    assert c.get(k) == {"text": "第一次"}, "后写覆盖了先写，回放会取到错的答案"
    assert c.stats["overwritten_slots"] == 1, c.stats

    # 落盘重载后语义不变（回放走的就是重载路径）。
    # ★ 注意冲突计数回到 0 —— 因为被拒绝的那次覆盖**根本没写进文件**，
    #   文件始终是干净的「一键一条」。冲突计数在载入路径上只统计
    #   **别人写进文件的**重复（见下一个测试）。
    c2 = ResponseCache(p)
    assert c2.get(k) == {"text": "第一次"}
    assert c2.stats["entries"] == 1
    assert c2.stats["overwritten_slots"] == 0
    assert p.read_text(encoding="utf-8").count("\n") == 1, (
        "被拒绝的覆盖仍然落盘了 —— 文件里的重复记录正是回放取错答案的根源"
    )


def test_cache_load_reports_multi_writer(tmp_path: Path) -> None:
    """文件里同一个键出现两次且内容不同 ⇒ 一定是多个写者，必须报出来。"""
    from agent.model.cache import ResponseCache, cache_key

    k = cache_key(provider="p", model="m", messages=[{"role": "user", "content": "x"}],
                  params={}, json_schema=None, prompt_sha=None)
    p = tmp_path / "c.jsonl"
    p.write_text(
        json.dumps({"key": k, "response": {"text": "A"}}) + "\n"
        + json.dumps({"key": k, "response": {"text": "B"}}) + "\n",
        encoding="utf-8",
    )
    c = ResponseCache(p)
    assert c.stats["entries"] == 1, "同键被当成两条记录，槽位语义不对"
    assert c.stats["overwritten_slots"] == 1, c.stats
    assert c.get(k) == {"text": "A"}


# ==========================================================================
# 内存状态的重水化（第十八轮的核心修复）
# ==========================================================================


def _fake_run(tmp_path: Path, project_root: Path) -> Path:
    """造一个只有 channels 产物的 run 目录（不跑流水线、不碰模型）。"""
    from agent.runtime.loop import build_context

    ctx = build_context(run_id="hyd", runs_root=tmp_path / "runs",
                        pipeline="r1_extract", use_mock=True, offline=True)
    d = ctx.run_dir
    (d / "channels").mkdir(parents=True, exist_ok=True)
    (d / "facts").mkdir(parents=True, exist_ok=True)
    (d / "channels" / "text_layer.json").write_text(
        json.dumps(
            [
                {
                    "doc_id": "doc_a", "page": 1,
                    "raw": "单位：万元\n营业收入 1,234.00\n",
                    "text": "营业收入 1,234.00",
                    "chunks": [
                        {"chunk_id": "chk_1", "bbox": {"x0": 0, "y0": 0, "x1": 10, "y1": 10},
                         "reading_order": 0, "char_len": 5, "text": "营业收入"},
                    ],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (d / "facts" / "documents.json").write_text(
        json.dumps([{"doc_id": "doc_a", "filename": "a.pdf", "page_count": 1}],
                   ensure_ascii=False),
        encoding="utf-8",
    )
    return d


@pytest.mark.slow
def test_resume_does_not_launder_run_status(
    tmp_path: Path, project_root: Path, sample_raw_dir: Path
) -> None:
    """★ 空转的续跑不得把 ``partial`` 洗成 ``success``。

    实测教训：``annual41-seg`` 原本是 ``partial``（一次模型调用失败）。
    为了给演示流程计时，在它上面跑了一次 ``--resume``（阶段全被跳过）——
    那一轮 ``failed=[]``，收尾就把 status 重算成 ``success`` 覆盖写回。
    **一次空转把「运行不完整」这件事洗掉了。**

    后果不抽象：``replay`` 靠 manifest.status 决定要不要打印
    「本次不一致是预期结果（原始运行 partial）」那段说明。洗掉之后
    读者只看到一个孤零零的「❌ 不可复现」—— 正是本文件开头警告的
    「假警报会训练人忽略复现报告」。
    """
    if not any(sample_raw_dir.glob("*.pdf")):
        pytest.skip("缺少 data/raw 下的测试材料")

    runs_root = tmp_path / "runs"
    r = _cli(project_root, runs_root, "run", "r1_extract", "--mock",
             "--run-id", "st", "--quiet")
    assert r.returncode == 0, r.stdout + r.stderr

    mf = runs_root / "st" / "manifest.json"
    d = json.loads(mf.read_text(encoding="utf-8"))
    d["status"] = "partial"          # 伪造「上次有一次调用失败」
    mf.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n",
                  encoding="utf-8", newline="\n")

    r = _cli(project_root, runs_root, "run", "r1_extract", "--mock",
             "--run-id", "st", "--resume", "--quiet")
    assert r.returncode == 0, r.stdout + r.stderr

    after = json.loads(mf.read_text(encoding="utf-8"))["status"]
    assert after == "partial", (
        f"空转的续跑把状态洗成了 {after} —— 复现报告会因此丢掉"
        "「原始运行不完整，不一致是预期」这段解释"
    )


def test_hydrate_store_rebuilds_page_text_and_chunks(tmp_path: Path,
                                                     project_root: Path) -> None:
    """水化函数必须把**页面原文与文本块**都灌回来。

    ⚠️ 这里特意断言 chunk 条数：落盘的 chunk 记录缺 ``doc_id/page/source_layer``，
    而 ``Chunk`` 是 ``extra="forbid"`` 的严格模型 —— 直接 ``model_validate``
    会**每一条都失败**。第一版就是这么写的，还用 ``except: continue`` 吞掉了它，
    结果块一条都没灌回来，``_locate_chunk`` 退化成占位块、``span_id`` 全变、
    产物哈希对不上（被哈希比对抓出来，不是被"看着对"抓出来的）。
    """
    import json as _json

    from agent.runtime.loop import build_context
    from pipelines.r1_extract.common import (assert_store_has_page_text,
                                             hydrate_store_from_channels)

    _fake_run(tmp_path, project_root)
    ctx = build_context(run_id="hyd", runs_root=tmp_path / "runs",
                        pipeline="r1_extract", use_mock=True, offline=True)

    # 空库 ⇒ 闸门必须拦住（这正是「静默 0 条事实」的现场）
    with pytest.raises(Exception) as ei:
        assert_store_has_page_text(ctx, ["doc_a"])
    assert "权威页面原文" in str(ei.value)

    counts = hydrate_store_from_channels(ctx)
    assert counts["page_text"] == 1, counts
    assert counts["chunks"] == 1, f"文本块没灌回来：{counts}"
    assert counts["chunks_failed"] == 0, counts
    assert ctx.store.page_text("doc_a", 1).startswith("单位：万元")
    assert len(ctx.store.chunks_of_doc("doc_a")) == 1

    # 灌完之后闸门放行
    assert_store_has_page_text(ctx, ["doc_a"])

    # page_layout.json 里有块级坐标时也要灌
    (ctx.run_dir / "channels" / "page_layout.json").write_text(
        _json.dumps({"blocks": [{"doc_id": "doc_a", "page": 1,
                                 "blocks": [{"text": "合并资产负债表",
                                             "bbox": [0, 0, 10, 10], "y0": 0}]}],
                     "statement_titles": {"doc_a": [{"page": 1, "y0": 0, "scope": "consolidated",
                                                     "text": "合并资产负债表"}]}},
                    ensure_ascii=False),
        encoding="utf-8",
    )
    hydrate_store_from_channels(ctx)
    assert ctx.store.page_blocks("doc_a", 1), "块级坐标没灌回来 —— 口径判定会失真"
    assert ctx.store.statement_titles("doc_a"), "报表标题没灌回来"


@pytest.mark.slow
def test_resume_skipping_extract_text_still_builds_the_same_facts(
    tmp_path: Path, project_root: Path, sample_raw_dir: Path
) -> None:
    """★ 跳过 ``extract_text`` 续跑，事实库必须与完整跑**逐字节相同**。

    这是第十八轮那条「事实 1050 → 0」的回归闸门。修法有两半，缺一不可：
      · 落盘（``channels/page_layout.json``） + 水化（``hydrate_store_from_channels``）
      · 硬断言（``assert_store_has_page_text``）—— 不许再静默交出 0 条
    """
    if not any(sample_raw_dir.glob("*.pdf")):
        pytest.skip("缺少 data/raw 下的测试材料")

    import hashlib

    runs_root = tmp_path / "runs"

    def sha(rel: str) -> str:
        f = runs_root / "res" / rel
        return hashlib.sha256(f.read_bytes()).hexdigest()

    r = _cli(project_root, runs_root, "run", "r1_extract", "--mock",
             "--run-id", "res", "--quiet")
    assert r.returncode == 0, r.stdout + r.stderr
    good = sha("facts/facts.json")
    n_facts = len(json.loads(
        (runs_root / "res" / "facts" / "facts.json").read_text(encoding="utf-8")
    )["facts"])
    assert n_facts > 0, "完整跑就没抽出事实，本测试失去意义"

    # 伪造「extract_text 被记为完成、但其内存副作用已丢失」的续跑现场：
    # 只保留 ingest 与 extract_text 两步，其余全部重跑。
    cp = runs_root / "res" / "checkpoint.json"
    d = json.loads(cp.read_text(encoding="utf-8"))
    d["completed"] = [s for s in d["completed"] if s in ("ingest", "extract_text")]
    d["failed"] = []
    cp.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n",
                  encoding="utf-8", newline="\n")

    r = _cli(project_root, runs_root, "run", "r1_extract", "--mock",
             "--run-id", "res", "--resume", "--quiet")
    assert r.returncode == 0, r.stdout + r.stderr

    n_after = len(json.loads(
        (runs_root / "res" / "facts" / "facts.json").read_text(encoding="utf-8")
    )["facts"])
    assert n_after == n_facts, (
        f"跳过 extract_text 续跑后事实数从 {n_facts} 变成 {n_after} —— "
        "内存证据库没被灌回来（这正是第十八轮那个「1050 → 0」）"
    )
    assert sha("facts/facts.json") == good, (
        "跳过 extract_text 续跑后 facts.json 与完整跑不一致"
    )


# ==========================================================================
# 回放必须**只读**原始 run（第二十轮：一个假绿灯）
# ==========================================================================


def test_run_dir_override_moves_tracer_and_cache(tmp_path: Path) -> None:
    """``build_context(run_dir=...)`` 必须把 Tracer 与 gateway 的缓存一起搬走。

    ★ 这条钉的是一个**两个 bug 互相抵消**的假绿灯 ——
      ``replay`` 原先只在 ``build_context`` 返回后写 ``ctx.run_dir = out_dir``，
      而 ``Tracer`` 和 ``ResponseCache`` 都是**在函数内部**构造的：

      ① ``Tracer`` 是按**原始 run_id** 建的，gateway 又抓了一份它的引用
         （``build_gateway`` 里 ``tracer=ctx.tracer``）。返回后只改 ``ctx``
         上的字段，gateway 手里那份纹丝不动 —— 于是每次回放都把自己的事件
         **追加**进原始 run 的 ``trace.jsonl``（实测 942 → 2826 → 3768 行，
         每次都 +942）。而 ``trace.jsonl`` 不在 manifest 的产物清单里，
         ``verify`` 查不出来；受害的是 /runs 页面 —— 上面的「事件总数 /
         模型调用 / 成本」全部读自那个文件，评审按页面上的说明跑一次
         ``replay``，页面读数就被回放自己的记录污染了。

      ② ``ResponseCache.__init__`` 会**立即** ``_load()``，所以在返回后
         才 copytree 进去的缓存它根本看不见。实测：回放 **0 次 llm_call**、
         **101 次「离线缓存未命中」**，所有依赖模型的产物（facts /
         valuation / audit）全部对不上，而报告只会说「不可复现」，
         对真正的原因只字不提。

      两条同时在，症状恰好互相掩盖：tracer 泄漏让 gateway 的缓存指向
      **原始 run 的 ``cache/llm.jsonl``**（400 条齐全）→ 命中 → 报 ✅。
      所以那个绿灯不是设计对了，是**没改成对的**。
    """
    from agent.runtime.loop import build_context

    runs_root = tmp_path / "runs"
    replay_dir = runs_root / "orig" / "_replay"
    ctx = build_context(
        run_id="orig", runs_root=runs_root, run_dir=replay_dir,
        pipeline="r1_extract", use_mock=True, offline=True,
    )

    assert ctx.tracer.path == replay_dir / "trace.jsonl", (
        "Tracer 没跟着 run_dir 走 —— 回放会写脏原始 run 的 trace.jsonl"
    )
    assert ctx.gateway.tracer.path == replay_dir / "trace.jsonl", (
        "gateway 手里的 tracer 没跟着走 —— 模型调用事件仍会落进原始 run"
    )
    assert ctx.gateway.cache.path == replay_dir / "cache" / "llm.jsonl", (
        "gateway 的缓存没跟着走 —— 回放会直接读写原始 run 的缓存，不是密封的"
    )

    # 兜底：不给 run_dir 时行为必须与从前完全一致（默认 runs_root/run_id）。
    plain = build_context(
        run_id="orig", runs_root=runs_root,
        pipeline="r1_extract", use_mock=True, offline=True,
    )
    assert plain.run_dir == runs_root / "orig"
    assert plain.tracer.path == runs_root / "orig" / "trace.jsonl"
    assert plain.gateway.cache.path == runs_root / "orig" / "cache" / "llm.jsonl"


@pytest.mark.slow
def test_replay_does_not_touch_the_original_run(
    tmp_path: Path, project_root: Path, materials: Path
) -> None:
    """``replay`` 必须对原始 run **只读**：trace.jsonl 与产物一字不改。

    这是上一条的端到端版本 —— 单测保证 wiring 对，这条保证真的跑一遍
    之后原始目录**逐字节没动**（``trace.jsonl`` 要单独盯，因为它不在
    manifest 的产物清单里，``verify`` 覆盖不到它）。
    """
    runs_root = tmp_path / "runs"
    r = _cli(project_root, runs_root, "run", "r1_extract", "--mock",
             "--run-id", "rev", "--limit", "1", "--quiet")
    assert r.returncode == 0, r.stdout + r.stderr

    def snapshot() -> dict[str, str]:
        from agent.trace.tracer import file_sha256

        return {
            str(p.relative_to(runs_root / "rev")): file_sha256(p)
            for p in sorted((runs_root / "rev").rglob("*"))
            if p.is_file() and "_replay" not in p.parts
        }

    before = snapshot()
    assert "trace.jsonl" in before, "这个 run 没产出 trace.jsonl，本测试失去意义"

    r = _cli(project_root, runs_root, "replay", "rev", "--strict")
    assert r.returncode == 0, r.stdout + r.stderr

    after = snapshot()

    # ★ ``replay_report.md`` 是**故意**写在 run 目录下的，不算违反只读：
    #   它是「关于这次回放的报告」，不是运行的产物 —— loop 的
    #   ``_finalize_manifest`` 也专门把它排除在产物清单之外。所以这里
    #   放行它，但**其余任何**文件一个字节都不许动。
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    assert not changed, (
        f"回放改动了原始 run 的 {len(changed)} 个文件：{changed[:5]} —— "
        "回放必须是只读的，否则「可追溯」的读数会被回放自己污染"
    )
    appeared = sorted(set(after) - set(before) - {"replay_report.md"})
    assert not appeared, (
        f"回放在原始 run 目录里新生成了 {len(appeared)} 个文件：{appeared[:5]} —— "
        "回放自己的产物应当全部落在 _replay/ 下"
    )
    # 回放自己的 trace 应落在 _replay/ 下（而不是没产出、或写到别人家里）
    assert (runs_root / "rev" / "_replay" / "trace.jsonl").exists(), (
        "回放没有产出自己的 trace —— 它写到别处去了"
    )
