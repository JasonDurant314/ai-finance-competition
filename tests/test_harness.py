"""Harness 内核测试 —— 调度、预算、缓存、契约、端到端、复现性。

这是 P2 阶段（Harness 内核）的验收测试。核心断言：
    * 任意情况下都有界终止（预算闸门生效）
    * 同输入必然同输出（缓存 + 确定性）
    * 失败必须显式暴露，绝不静默降级
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agent.model.cache import ResponseCache, cache_key, hash_schema
from agent.model.gateway import ModelGateway, RoleConfig, text_message
from agent.model.providers.mock import MockProvider
from agent.runtime.budget import Budget
from agent.runtime.errors import (
    BudgetExceededError,
    ContractViolation,
    ModelError,
    SandboxViolation,
    StructuredOutputError,
    ToolNotFoundError,
)
from agent.runtime.loop import Loop, Plan, RunContext, build_context
from agent.tools.fsguard import FileGuard
from agent.tools.registry import ToolRegistry, build_registry, tool
from agent.trace.tracer import Tracer, payload_hash, read_trace, summarize_trace
from pydantic import BaseModel


# ==========================================================================
# 预算闸门
# ==========================================================================


def test_budget_stops_on_step_limit() -> None:
    b = Budget(max_steps=3)
    b.tick()
    b.tick()
    b.tick()
    with pytest.raises(BudgetExceededError, match="步数超限"):
        b.tick()


def test_budget_stops_on_token_limit() -> None:
    b = Budget(max_tokens=100)
    b.charge(60)
    with pytest.raises(BudgetExceededError, match="token 超限"):
        b.charge(50)


def test_budget_stops_on_cost_limit() -> None:
    b = Budget(max_cost_cny=1.0)
    with pytest.raises(BudgetExceededError, match="成本超限"):
        b.charge(0, 1.5)


def test_budget_stops_on_wall_time() -> None:
    """墙钟闸门：预算为 0 时必须立刻超限。

    起点显式回拨 1 秒。不能只写 max_wall_seconds=0.0 —— 那样断言成立与否
    取决于 time.monotonic() 在两次调用之间是否推进，而 Windows 上它常常
    不推进（实测 1000/1000 次 elapsed 恰为 0.0），测试会假失败。
    """
    b = Budget(max_wall_seconds=0.0, started_at=time.monotonic() - 1.0)
    with pytest.raises(BudgetExceededError, match="时长超限"):
        b.check()


def test_budget_unlimited_when_none() -> None:
    """全部闸门为 None 时不应抛异常（虽然不推荐这样配）。"""
    b = Budget(max_steps=None, max_tokens=None, max_wall_seconds=None, max_cost_cny=None)
    b.tick(1000)
    b.charge(10**9, 10**6)


def test_budget_snapshot_shape() -> None:
    b = Budget()
    b.tick(2)
    snap = b.snapshot()
    assert snap["steps"] == 2
    assert "elapsed_s" in snap


# ==========================================================================
# 文件守卫（受控数据环境）
# ==========================================================================


def test_fsguard_allows_read_under_allowed_root(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    f = tmp_path / "data" / "a.txt"
    f.write_text("hello", encoding="utf-8")
    guard = FileGuard(read_roots=[tmp_path / "data"], write_roots=[tmp_path / "runs"])
    assert guard.read_text(f) == "hello"


def test_fsguard_denies_read_outside_roots(tmp_path: Path) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("no", encoding="utf-8")
    guard = FileGuard(read_roots=[tmp_path / "data"], write_roots=[tmp_path / "runs"])
    with pytest.raises(SandboxViolation, match="越权"):
        guard.read_text(outside)


def test_fsguard_denies_write_to_readonly_root(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    guard = FileGuard(read_roots=[tmp_path / "data"], write_roots=[tmp_path / "runs"])
    with pytest.raises(SandboxViolation):
        guard.write_text(tmp_path / "data" / "evil.txt", "x")


def test_fsguard_write_records_hash(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    tracer = Tracer(tmp_path / "trace.jsonl")
    guard = FileGuard(read_roots=[tmp_path], write_roots=[runs], tracer=tracer)
    guard.write_text(runs / "out.json", '{"a":1}')
    tracer.close()
    events = read_trace(tmp_path / "trace.jsonl")
    assert any(e.kind.value == "file_access" and e.mode == "write" for e in events)
    assert any(e.path_sha256 for e in events)


def test_fsguard_denies_network_when_offline() -> None:
    guard = FileGuard(read_roots=["."], write_roots=["."], allow_network=False)
    with pytest.raises(SandboxViolation, match="离线模式"):
        guard.check_network("api.deepseek.com")


# ==========================================================================
# 工具注册表
# ==========================================================================


class _EchoIn(BaseModel):
    text: str
    times: int = 1


class _EchoOut(BaseModel):
    result: str


@tool(
    name="test.echo",
    description="回显输入",
    params=_EchoIn,
    returns=_EchoOut,
    side_effect="none",
    cost="free",
)
def _echo(text: str, times: int = 1) -> dict:
    return {"result": text * times}


def test_registry_registers_and_invokes() -> None:
    reg = build_registry(__import__(__name__, fromlist=["_echo"]))
    assert "test.echo" in reg
    out = reg.call("test.echo", {"text": "ab", "times": 2})
    assert out == {"result": "abab"}


def test_registry_rejects_unknown_tool() -> None:
    reg = ToolRegistry()
    with pytest.raises(ToolNotFoundError):
        reg.call("nope", {})


def test_registry_validates_tool_args() -> None:
    """入参不符合 Schema 时应拒绝，不把脏数据交给函数体。"""
    reg = build_registry(__import__(__name__, fromlist=["_echo"]))
    with pytest.raises(ContractViolation, match="入参不符合 Schema"):
        reg.call("test.echo", {"times": "not-an-int"})


def test_registry_duplicate_name_rejected() -> None:
    reg = ToolRegistry()
    mod = __import__(__name__, fromlist=["_echo"])
    reg2 = build_registry(mod)
    for spec in reg2._tools.values():  # noqa: SLF001 - 测试内部一致性
        reg.register(spec)
    with pytest.raises(ContractViolation, match="工具名重复"):
        for spec in reg2._tools.values():  # noqa: SLF001
            reg.register(spec)


def test_registry_exports_openai_schema() -> None:
    reg = build_registry(__import__(__name__, fromlist=["_echo"]))
    schemas = reg.to_openai_schemas()
    assert len(schemas) == 1
    fn = schemas[0]["function"]
    assert fn["name"] == "test.echo"
    assert "text" in fn["parameters"]["properties"]


def test_registry_subset_enforces_least_privilege() -> None:
    reg = build_registry(__import__(__name__, fromlist=["_echo"]))
    sub = reg.subset(["test.echo"])
    assert "test.echo" in sub
    assert len(sub) == 1


def test_registry_records_tool_call_in_trace(tmp_path: Path) -> None:
    tracer = Tracer(tmp_path / "t.jsonl")
    reg = build_registry(__import__(__name__, fromlist=["_echo"]), tracer=tracer)
    reg.call("test.echo", {"text": "x"})
    tracer.close()
    events = read_trace(tmp_path / "t.jsonl")
    assert any(e.tool == "test.echo" and e.ok for e in events)


# ==========================================================================
# 响应缓存
# ==========================================================================


def test_cache_key_is_stable() -> None:
    kw = dict(
        provider="p", model="m",
        messages=[{"role": "user", "content": "hi"}],
        params={"temperature": 0.0},
        json_schema=None, prompt_sha="abc",
    )
    assert cache_key(**kw) == cache_key(**kw)


def test_cache_key_changes_with_prompt_version() -> None:
    """prompt_sha 参与键计算 —— 换提示词必须让旧缓存失效。"""
    base = dict(
        provider="p", model="m", messages=[], params={}, json_schema=None
    )
    assert cache_key(**base, prompt_sha="v1") != cache_key(**base, prompt_sha="v2")


def test_cache_roundtrip(tmp_path: Path) -> None:
    c = ResponseCache(tmp_path / "c.jsonl")
    c.put("k1", {"text": "hello"})
    assert c.get("k1") == {"text": "hello"}
    # 新实例应从磁盘恢复
    c2 = ResponseCache(tmp_path / "c.jsonl")
    assert c2.get("k1") == {"text": "hello"}


def test_cache_tolerates_truncated_line(tmp_path: Path) -> None:
    """中断写入留下的半行不应导致缓存整体不可用。"""
    p = tmp_path / "c.jsonl"
    p.write_text('{"key":"a","response":{"text":"ok"}}\n{"key":"b","resp', encoding="utf-8")
    c = ResponseCache(p)
    assert c.get("a") == {"text": "ok"}


def test_cache_disabled_returns_none(tmp_path: Path) -> None:
    c = ResponseCache(tmp_path / "c.jsonl", enabled=False)
    c.put("k", {"x": 1})
    assert c.get("k") is None


# ==========================================================================
# 模型网关
# ==========================================================================


def _gateway(**kw) -> ModelGateway:
    """构造测试用网关。

    ⚠️ 必须把 ``**kw`` 完整转发给 ModelGateway —— 早期版本漏了这一步，
    导致传入的 cache/budget/offline 全部被静默忽略，测试「通过」但实际
    什么都没验证。这类 bug 比测试失败更危险。
    """
    roles = {
        "default": RoleConfig(model="mock", temperature=0.0),
        "extractor": RoleConfig(model="mock", reasoning_effort="low"),
    }
    kw.setdefault("provider", MockProvider())
    kw.setdefault("roles", roles)
    return ModelGateway(**kw)


def test_gateway_returns_text() -> None:
    gw = _gateway()
    r = gw.call(messages=[text_message("user", "hi")])
    assert r.text
    assert r.role == "default"


def test_gateway_caches_and_reports_hit(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path / "c.jsonl")
    gw = _gateway(cache=cache)
    msgs = [text_message("user", "same question")]
    a = gw.call(messages=msgs)
    b = gw.call(messages=msgs)
    assert not a.cache_hit
    assert b.cache_hit
    assert a.text == b.text


def test_gateway_offline_without_cache_raises(tmp_path: Path) -> None:
    """离线 + 缓存未命中必须显式报错，绝不静默降级。"""
    gw = _gateway(cache=ResponseCache(tmp_path / "c.jsonl"), offline=True)
    with pytest.raises(ModelError, match="离线模式"):
        gw.call(messages=[text_message("user", "never asked before")])


def test_offline_does_not_change_provider_identity(tmp_path: Path) -> None:
    """★ offline ≠ mock —— 离线不得更换 provider。

    实测踩过的坑：``build_gateway`` 早期写成
    ``if use_mock or offline: provider = MockProvider()``。
    结果是 replay（``offline=True``）会**换掉 provider**，
    而缓存键包含 provider 名 → 27 条缓存全部未命中 →
    明明完全相同的运行被判定为「不可复现」。

    正确语义：``offline`` 只禁止出网，provider 身份必须保持不变。
    """
    from agent.runtime.errors import MissingCredentialError
    from agent.runtime.loop import build_context

    # ★ 交付包里没有 `.env`（`EXCLUDE_NAMES` 刻意排除，密钥不能进提交包），
    #   于是 `build_context` 抛 MissingCredentialError —— 那是**缺凭据**，
    #   不是「offline 改坏了 provider 身份」。原先这里直接崩成 FAIL，
    #   让解包后的 `python -m pytest -q` 无端多一条红（2026-09-27 实测）。
    #   缺凭据时跳过并说明：本用例要验的是 online 与 offline 两个上下文
    #   的 provider 名一致，没有凭据就构造不出 online 那一半。
    try:
        ctx_online = build_context(run_id="prov-online", pipeline="t")
    except MissingCredentialError as exc:
        pytest.skip(f"缺少 API Key，构造不出 online 上下文：{exc}")
    ctx_offline = build_context(run_id="prov-offline", pipeline="t", offline=True)
    try:
        assert ctx_online.gateway is not None
        assert ctx_offline.gateway is not None
        assert ctx_online.gateway.provider.name == ctx_offline.gateway.provider.name, (
            "offline 改变了 provider 身份 —— 会导致缓存键变化、回放全部未命中"
        )
    finally:
        for c in (ctx_online, ctx_offline):
            if c.tracer:
                c.tracer.close()


def test_manifest_records_provider_and_data_dir(tmp_path: Path, project_root: Path) -> None:
    """manifest 必须记录 provider 与数据目录 —— 回放靠它们重建上下文。

    缺 provider → 回放换 provider → 缓存全未命中（假「不可复现」）。
    缺 data_dir → 回放去读默认 data/ → 输入不同 → 产物不同（假「不可复现」）。
    这两个字段都是实测踩坑后补上的。
    """
    import subprocess
    import sys

    if not any((project_root / "data" / "raw").glob("*.pdf")):
        pytest.skip("缺少 data/raw 下的测试材料")

    runs_root = tmp_path / "runs"
    r = subprocess.run(
        [
            sys.executable, "-m", "agent.cli", "run", "r1_extract",
            "--mock", "--run-id", "ctx-check", "--data", "data",
            "--runs-root", str(runs_root), "--quiet",
        ],
        cwd=project_root, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    manifest = json.loads(
        (runs_root / "ctx-check" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest.get("provider"), "manifest 未记录 provider，回放无法重建网关"
    assert manifest.get("data_dir"), "manifest 未记录 data_dir，回放会读错输入目录"


@pytest.mark.slow
def test_replay_reproduces_run_with_limit_and_facts_run(
    tmp_path: Path, project_root: Path
) -> None:
    """★ 影响产物的运行参数必须被记录，否则回放报的是**假警报**。

    真实踩过两次（都是"本可复现却被判不可复现"）：

    1. ``agent run full --mock --limit 1`` → replay ❌
       原因：``--limit`` 没写进 manifest，回放处理了全部文档。
    2. ``agent run r4_valuation --mock --facts-run smoke`` → replay ❌
       原因：``--facts-run`` 没写进 manifest，回放去本 run 目录找 facts.json。

    为什么这类 bug 特别值得一条测试：**假警报比不校验更糟**。
    复现报告一旦开始出错，人就会学会忽略它 ——
    那时候真正的「不可复现」也不会被发现了。
    """
    import subprocess
    import sys

    if not any((project_root / "data" / "raw").glob("*.pdf")):
        pytest.skip("缺少 data/raw 下的测试材料")

    runs_root = tmp_path / "runs"

    def cli(*argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "agent.cli", *argv,
             "--runs-root", str(runs_root)],
            cwd=project_root, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )

    # ① 小样本试跑：--limit 必须被记录，且回放一致
    r = cli("run", "r1_extract", "--mock", "--run-id", "lim", "--limit", "1", "--quiet")
    assert r.returncode == 0, r.stdout + r.stderr
    manifest = json.loads(
        (runs_root / "lim" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest.get("doc_limit") == 1, (
        "manifest 未记录 doc_limit —— 回放会处理全部文档，产物不同，报假『不可复现』"
    )

    r = cli("replay", "lim", "--strict")
    assert "✅ 可复现" in r.stdout, (
        "带 --limit 的运行回放不一致：\n" + r.stdout[-2000:]
    )

    # ② 链式运行：--facts-run 必须被记录，且回放一致
    r = cli("run", "r4_valuation", "--mock", "--run-id", "chain",
            "--facts-run", "lim", "--quiet")
    assert r.returncode == 0, r.stdout + r.stderr
    manifest = json.loads(
        (runs_root / "chain" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest.get("upstream_facts_run") == "lim", (
        "manifest 未记录 upstream_facts_run —— 回放找不到事实库，报假『不可复现』"
    )

    r = cli("replay", "chain", "--strict")
    assert "✅ 可复现" in r.stdout, (
        "链式运行（--facts-run）回放不一致：\n" + r.stdout[-2000:]
    )


@pytest.mark.slow
def test_mock_run_audit_report_calls_skipped_not_failed(
    tmp_path: Path, project_root: Path
) -> None:
    """★ 端到端钉住「未校验 ≠ 失败」在**产物里**也成立。

    上面两条测的是汇总函数；这条测的是评审真正会打开的东西：
    ``run full --mock`` 之后的 ``audit.md``。合成样本缺现金流/利润表明细，
    所以 A2/A3/A5 必然 SKIPPED —— 报告必须写成「未校验（缺数据）」，
    统计行必须写「失败 0」。
    """
    import subprocess
    import sys

    if not any((project_root / "data" / "raw").glob("*.pdf")):
        pytest.skip("缺少 data/raw 下的测试材料")

    runs_root = tmp_path / "runs"
    r = subprocess.run(
        [sys.executable, "-m", "agent.cli", "run", "full", "--mock",
         "--run-id", "three-state", "--runs-root", str(runs_root), "--quiet"],
        cwd=project_root, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stdout + r.stderr

    # 审计报告单独生成（`--quiet` 会跳过它；`agent audit` 是评审路径上的那条命令）
    a = subprocess.run(
        [sys.executable, "-m", "agent.cli", "audit", "three-state",
         "--runs-root", str(runs_root)],
        cwd=project_root, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert a.returncode == 0, a.stdout + a.stderr

    audit_md = (runs_root / "three-state" / "audit.md").read_text(encoding="utf-8")
    assert "未校验（缺数据）" in audit_md, (
        "审计报告没有把 SKIPPED 与 FAIL 分开显示：\n"
        + "\n".join(l for l in audit_md.splitlines() if "断言" in l)[:800]
    )
    assert "失败 **0**" in audit_md, (
        "缺辅助数据被统计成了失败（虚报）：\n"
        + "\n".join(l for l in audit_md.splitlines() if "断言" in l)[:800]
    )


def test_run_refuses_to_append_to_an_existing_run(tmp_path: Path,
                                                 project_root: Path) -> None:
    """★ 不带 ``--resume`` 重跑同一个 run 名，必须被**拒绝**。

    背景：``Tracer`` 以 append 方式打开 ``trace.jsonl``，重跑会把新事件叠在旧事件
    后面、``seq`` 从 0 重来。2026-10-17 实测：同 run 名跑第二遍后 trace 从 146 行
    变 292 行、**146 个 seq 全部重复**，紧接着 ``verify`` 判「不可复现」。
    这不是台账难看，是把一份能复现的 run 弄成不能复现的 —— 评审照文档把同一条
    命令敲两遍就会踩到，所以守卫必须存在，且必须给出可照做的两条出路。
    """
    import subprocess
    import sys

    if not any((project_root / "data" / "raw").glob("*.pdf")):
        pytest.skip("缺少 data/raw 下的测试材料")

    runs_root = tmp_path / "runs"
    base = [sys.executable, "-m", "agent.cli", "run", "r1_extract", "--mock",
            "--run-id", "dup", "--limit", "1", "--runs-root", str(runs_root)]

    first = subprocess.run(base + ["--quiet"], cwd=project_root,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    assert first.returncode == 0, first.stdout + first.stderr
    trace = runs_root / "dup" / "trace.jsonl"
    lines_before = len(trace.read_text(encoding="utf-8").splitlines())

    second = subprocess.run(base + ["--quiet"], cwd=project_root,
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    assert second.returncode != 0, "同一个 run 名被重跑了却没有报错"
    msg = second.stdout + second.stderr
    assert "已经跑过了" in msg, msg
    assert "--resume" in msg, "拒绝时没有告诉用户出路：\n" + msg
    # 关键：被拒的那一次**一个字节都没写**（否则守卫等于没守）
    assert len(trace.read_text(encoding="utf-8").splitlines()) == lines_before, (
        "被拒绝的重跑仍然往 trace.jsonl 写了东西"
    )

    # 续跑是合法出路，不能被同一条守卫挡住
    resumed = subprocess.run(base + ["--resume", "--quiet"], cwd=project_root,
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
    assert "已经跑过了" not in (resumed.stdout + resumed.stderr), (
        "--resume 也被这条守卫拦住了"
    )


def test_gateway_repairs_bad_json(tmp_path: Path) -> None:
    """模型输出非 JSON 时，网关应把校验错误回灌并重试。"""
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    }
    calls: list[int] = []

    def fallback(messages):
        calls.append(1)
        if len(calls) == 1:
            return {"text": "这不是 JSON"}  # 第一次故意给坏输出
        return {"text": '{"ok": true}'}

    gw = ModelGateway(
        provider=MockProvider(fallback=fallback),
        roles={"default": RoleConfig(model="mock")},
        cache=ResponseCache(tmp_path / "c.jsonl"),
    )
    r = gw.call(messages=[text_message("user", "x")], json_schema=schema)
    assert r.parsed == {"ok": True}
    assert len(calls) >= 2, "应发生自我修复重试"


def test_gateway_raises_after_repair_exhausted(tmp_path: Path) -> None:
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    gw = ModelGateway(
        provider=MockProvider(fallback=lambda m: {"text": "永远不是 JSON"}),
        roles={"default": RoleConfig(model="mock")},
        cache=ResponseCache(tmp_path / "c.jsonl"),
        max_retries=1,
    )
    with pytest.raises(ModelError, match="仍失败"):
        gw.call(messages=[text_message("user", "x")], json_schema=schema)


def test_gateway_charges_budget() -> None:
    b = Budget(max_tokens=10_000)
    gw = _gateway(budget=b)
    gw.call(messages=[text_message("user", "hi")])
    assert b.tokens > 0


def test_gateway_stats_track_cache_hits(tmp_path: Path) -> None:
    gw = _gateway(cache=ResponseCache(tmp_path / "c.jsonl"))
    msgs = [text_message("user", "q")]
    gw.call(messages=msgs)
    gw.call(messages=msgs)
    assert gw.stats["cache_hits"] == 1
    assert gw.stats["cache_hit_rate"] == pytest.approx(0.5)


def test_gateway_requires_default_role() -> None:
    with pytest.raises(ContractViolation.__mro__[1] if False else Exception):
        ModelGateway(provider=MockProvider(), roles={"extractor": RoleConfig(model="m")})


# ==========================================================================
# 追踪
# ==========================================================================


def test_tracer_writes_jsonl(tmp_path: Path) -> None:
    tr = Tracer(tmp_path / "t.jsonl")
    tr.llm(
        role="extractor", model="m", prompt_id="p", prompt_version="1",
        prompt_sha256="abc", prompt_tokens=10, completion_tokens=5,
        latency_ms=100, cache_hit=False,
    )
    tr.assertion(code="A1", name="平衡", passed=True, detail="ok")
    tr.close()
    events = read_trace(tmp_path / "t.jsonl")
    assert len(events) == 2
    assert events[0].seq == 0
    assert events[1].seq == 1


def test_tracer_summary_computes_cache_rate(tmp_path: Path) -> None:
    tr = Tracer(tmp_path / "t.jsonl")
    for hit in (True, True, False):
        tr.llm(
            role="r", model="m", prompt_id="p", prompt_version="1", prompt_sha256="s",
            prompt_tokens=1, completion_tokens=1, latency_ms=1, cache_hit=hit,
        )
    tr.close()
    s = summarize_trace(read_trace(tmp_path / "t.jsonl"))
    assert s["llm_calls"] == 3
    assert s["cache_hit_rate"] == pytest.approx(2 / 3, abs=0.01)


def test_assertion_summary_separates_skipped_from_failed(tmp_path: Path) -> None:
    """★ 「未校验」不得被统计成「失败」。

    实测踩过的坑：``Tracer.assertion()`` 早期只收 ``passed: bool``，
    ``pipelines/r4_valuation/model.py`` 传的是 ``passed=(status == "pass")``，
    于是缺辅助数据时被标为 SKIPPED 的 30 条断言全部落进 ``ok=False``，
    ``summarize_trace`` 把它统计成「失败 30 条」——
    评审跑的第一条命令 ``run full --mock`` 就会看到这个数字，
    而真相是「30 条勾稽关系本次无法独立重算」。

    SKIPPED 与 FAIL 对读者的含义完全相反（``agent/evidence/models.py``
    的 ``is_usable`` 早已按三态判断），把它压成 bool 属于
    「同一个决策写在两处，第二处覆盖第一处」。
    """
    tr = Tracer(tmp_path / "t.jsonl")
    tr.assertion(code="A1", name="平衡", status="pass", detail="独立重算一致")
    tr.assertion(code="A2", name="现金流对账", status="skipped", detail="缺少明细，无法独立重算")
    tr.assertion(code="A3", name="留存收益滚动", status="fail", detail="残差超容差")
    tr.close()

    s = summarize_trace(read_trace(tmp_path / "t.jsonl"))
    assert s["assertions"] == 3
    assert s["assertions_passed"] == 1
    assert s["assertions_skipped"] == 1
    assert s["assertions_failed"] == 1, "只有 fail 才算失败 —— skipped 混进来会虚报失败数"


def test_assertion_bool_shorthand_still_records_failure(tmp_path: Path) -> None:
    """反向注入：**真失败** 必须仍然被算成失败。

    上面那条测试的正确修法是「把三态分开」，不是「把失败也归到未校验」。
    把 fail 洗成 skipped 会更危险 —— 它会掩盖真正的勾稽不通过。
    顺带钉住：未知状态必须立刻报错，而不是被当成通过。
    """
    tr = Tracer(tmp_path / "t.jsonl")
    tr.assertion(code="A4", name="FCFF 重算", passed=False, detail="重算不一致")
    tr.assertion(code="A1", name="平衡", passed=True, detail="一致")
    tr.close()
    s = summarize_trace(read_trace(tmp_path / "t.jsonl"))
    assert s["assertions_failed"] == 1
    assert s["assertions_skipped"] == 0

    bad = Tracer(tmp_path / "t2.jsonl")
    try:
        with pytest.raises(ValueError, match="未知断言状态"):
            bad.assertion(code="X", name="x", status="ok", detail="")
    finally:
        bad.close()


def test_payload_hash_is_order_insensitive_for_dicts() -> None:
    assert payload_hash({"a": 1, "b": 2}) == payload_hash({"b": 2, "a": 1})


def test_payload_hash_distinguishes_values() -> None:
    assert payload_hash({"a": 1}) != payload_hash({"a": 2})


# ==========================================================================
# 逐步记录（agent_log.jsonl）—— 控制台右栏「模型在干什么」的数据源
# ==========================================================================
#
# trace.jsonl 记「发生了什么」（工具名、耗时、载荷哈希），逐步记录记「说了什么」
# （提示词、回答、工具参数与返回）。两份文件在同一个 run 目录里并列，各有各的
# 用途 —— 这一节的测试守的就是这条边界：**原文不许漏进 trace，记录不许混进产物**。


def _journal(dir_: Path) -> list[dict]:
    """读回逐步记录。读不到时返回空表（「没有」与「空」在断言里要分得开）。"""
    p = dir_ / "agent_log.jsonl"
    if not p.is_file():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_journal_records_the_prompt_and_the_answer(tmp_path: Path) -> None:
    """模型调用的**原文**要落在 agent_log.jsonl，且**不得**漏进 trace.jsonl。"""
    tr = Tracer(tmp_path / "trace.jsonl")
    tr.llm(
        role="extractor", model="m", prompt_id="r1.vlm_judge_field",
        prompt_version="1", prompt_sha256="abc", prompt_tokens=10,
        completion_tokens=5, latency_ms=100, cache_hit=False,
        messages=[{"role": "system", "content": "你是财务抽取器"},
                  {"role": "user", "content": "营业收入是多少"}],
        text='{"revenue": 1}',
    )
    tr.close()

    recs = _journal(tmp_path)
    assert len(recs) == 1
    assert recs[0]["kind"] == "llm"
    assert [m["content"] for m in recs[0]["messages"]] == ["你是财务抽取器", "营业收入是多少"]
    assert recs[0]["text"] == '{"revenue": 1}'
    assert recs[0]["seq"] == 0 and recs[0]["ts"]

    trace_blob = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert "你是财务抽取器" not in trace_blob, (
        "提示词漏进了 trace.jsonl —— 那是审计入口，只该有哈希"
    )


def test_journal_records_tool_args_and_return_value(tmp_path: Path) -> None:
    """工具调用的入参与返回都要记 —— 经工具注册表实跑一遍（不是直接调 Tracer）。"""
    tracer = Tracer(tmp_path / "t.jsonl")
    reg = build_registry(__import__(__name__, fromlist=["_echo"]), tracer=tracer)
    reg.call("test.echo", {"text": "ab", "times": 2}, stage="r1_ingest")
    tracer.close()

    tool_recs = [r for r in _journal(tmp_path) if r["kind"] == "tool"]
    assert len(tool_recs) == 1
    assert tool_recs[0]["args"] == {"text": "ab", "times": 2}
    assert tool_recs[0]["result"] == {"result": "abab"}
    assert tool_recs[0]["stage"] == "r1_ingest" and tool_recs[0]["ok"] is True

    ev = [e for e in read_trace(tmp_path / "t.jsonl") if e.tool == "test.echo"][0]
    assert ev.args_hash, "trace 那一路仍应有哈希"
    assert "abab" not in json.dumps(ev.model_dump(mode="json"), ensure_ascii=False)


def test_journal_strips_page_image_payloads(tmp_path: Path) -> None:
    """★ 整页图的 base64 **整条换掉**，不留头尾。

    实测（在中石油那份 273 页年报表上跑一遍）：226 条模型调用，每次的 user
    消息里都挂着一条 `data:image/png;base64,…`，一页上百万字符。按「留头留尾」
    截到 2 万字符等于每条存 2 万个「iVBORw0KGgo…」—— 读不出图，只占磁盘。
    """
    blob = "data:image/png;base64," + "iVBORw0KGgo" * 50_000
    tr = Tracer(tmp_path / "trace.jsonl")
    tr.llm(
        role="vision", model="m", prompt_id="r1.vlm_scan_page", prompt_version="1",
        prompt_sha256="s", prompt_tokens=1, completion_tokens=1, latency_ms=1,
        cache_hit=False, text="{}",
        messages=[{"role": "user",
                   "content": [{"type": "text", "text": "读这一页"},
                               {"type": "image_url", "image_url": {"url": blob}}]}],
    )
    tr.close()

    rec = _journal(tmp_path)[0]
    url = rec["messages"][0]["content"][1]["image_url"]["url"]
    assert url.startswith("<图片已省略") and "iVBORw0KGgoiVBOR" not in url
    assert rec["cut"]["messages[0].content[1].image_url.url"] == {
        "kind": "image", "total": len(blob)}
    assert "读这一页" in json.dumps(rec, ensure_ascii=False), "文字部分不该被牵连"


def test_journal_keeps_head_and_tail_of_a_long_prompt(tmp_path: Path) -> None:
    """超长文本**留头也留尾** —— 提示词里真正的问题在末尾。

    只留开头会把要看的那句裁掉：结构是「系统提示 + 长材料在前，问题在最后」。
    """
    long_text = "材料开头" + "填充" * 20_000 + "请问营业收入是多少"
    tr = Tracer(tmp_path / "trace.jsonl")
    tr.llm(
        role="extractor", model="m", prompt_id="p", prompt_version="1",
        prompt_sha256="s", prompt_tokens=1, completion_tokens=1, latency_ms=1,
        cache_hit=False, text="{}",
        messages=[{"role": "user", "content": long_text}],
    )
    tr.close()

    rec = _journal(tmp_path)[0]
    kept = rec["messages"][0]["content"]
    assert kept.startswith("材料开头")
    assert kept.endswith("请问营业收入是多少"), "只留了开头 —— 真正的问题被裁掉了"
    assert "中略" in kept
    meta = rec["cut"]["messages[0].content"]
    assert meta["total"] == len(long_text) and meta["kept"] < len(long_text)


def test_journal_omits_an_oversized_result_instead_of_writing_it(tmp_path: Path) -> None:
    """整条记录超上限时**丢载荷、留指认**，而不是把几 MB 写进文件。

    右栏是每 2 秒轮询一次的：一条超大记录会被反复搬进浏览器。
    """
    big = {"cells": [{"v": "x" * 200} for _ in range(4000)]}
    tr = Tracer(tmp_path / "trace.jsonl")
    tr.tool(name="table.adjudicate", args={"page": 1}, ok=True, latency_ms=1,
            result=big)
    tr.close()

    rec = _journal(tmp_path)[0]
    assert rec["omitted"]["result"]["omitted"] is True
    assert rec["omitted"]["result"]["chars"] > 0
    assert rec["result"] is None, "超限的载荷要被换掉，而不是照写"
    assert rec["args"] == {"page": 1}, "小的字段不该被牵连"
    assert (tmp_path / "agent_log.jsonl").stat().st_size < 200_000


def test_journal_can_be_switched_off(tmp_path: Path) -> None:
    """``journal=False`` 只关逐步记录这一路，trace 那一路照写。"""
    tr = Tracer(tmp_path / "trace.jsonl", journal=False)
    tr.llm(role="r", model="m", prompt_id="p", prompt_version="1", prompt_sha256="s",
           prompt_tokens=1, completion_tokens=1, latency_ms=1, cache_hit=False,
           messages=[{"role": "user", "content": "hi"}], text="yo")
    tr.close()
    assert _journal(tmp_path) == []
    assert not (tmp_path / "agent_log.jsonl").exists()
    assert len(read_trace(tmp_path / "trace.jsonl")) == 1


def test_journal_borrows_the_current_stage(tmp_path: Path) -> None:
    """★ 调用方没给 ``stage`` 时，落到「当前正在跑的阶段」。

    实测踩到的坑：``domain/tools/vlm.py`` 那三处 ``gateway.call(...)`` 全都没传
    ``stage``，于是 226 条 ``llm_call`` 的 ``stage`` **全是 null** ——
    右栏只能把所有模型调用摊成一堆，回答不了「这句话是哪个阶段说的」。

    修法不是去每个调用点补 ``stage=``（领域层不该知道流水线阶段名），而是让
    编排层已有的 ``stage_event(status="start")`` 顺手把当前阶段记下来。

    阶段开始那一行**自己也是一条记录**（控制台右栏的边界，见下一条），所以这里
    按 kind 取模型与工具那两行来断。
    """
    tr = Tracer(tmp_path / "trace.jsonl")
    tr.stage_event(stage="r1_judge", status="start")
    tr.llm(role="vision", model="m", prompt_id="p", prompt_version="1",
           prompt_sha256="s", prompt_tokens=1, completion_tokens=1, latency_ms=1,
           cache_hit=False, text="{}")
    tr.tool(name="t", args={}, ok=True, latency_ms=1)
    tr.stage_event(stage="r1_judge", status="ok")

    # 显式给了就以给的为准 —— 兜底不能覆盖调用方的判断
    tr.llm(role="vision", model="m", prompt_id="p", prompt_version="1",
           prompt_sha256="s", prompt_tokens=1, completion_tokens=1, latency_ms=1,
           cache_hit=False, text="{}", stage="r4_model")
    tr.close()

    recs = [r for r in _journal(tmp_path) if r["kind"] in ("llm", "tool")]
    assert [r["stage"] for r in recs] == ["r1_judge", "r1_judge", "r4_model"]
    events = read_trace(tmp_path / "trace.jsonl")
    llm_events = [e for e in events if e.kind.value == "llm_call"]
    assert [e.stage for e in llm_events] == ["r1_judge", "r4_model"], (
        "trace 那一路也要跟着补上阶段 —— 两路对同一次调用给出不同的阶段会让人无法对账"
    )


def test_journal_marks_every_stage_start_and_nothing_else(tmp_path: Path) -> None:
    """★ 每个阶段开始记**一行边界**，阶段内的其它事件一行都不记。

    边界是给控制台右栏用的：那一栏列的全是模型与工具行，没有边界时分不出
    「这一段是哪个阶段说的」，更看不出「R4/R5 那一大段一个模型都没调」。
    而阶段内的 ``warn`` / ``skipped`` 是读数（已进「这一步的计数」那一列），
    再往右栏抄一遍只会把流淹掉 —— 所以只认 ``status="start"``。
    """
    tr = Tracer(tmp_path / "trace.jsonl")
    tr.stage_event(stage="r1_ingest", status="start")
    tr.stage_event(stage="r1_ingest", status="warn", detail="跳过 3 页")
    tr.stage_event(stage="r1_judge", status="start")
    tr.stage_event(stage="r1_judge", status="ok")
    tr.stage_event(stage="r4_model", status="start")
    tr.close()

    recs = _journal(tmp_path)
    assert [(r["kind"], r.get("stage"), r.get("status")) for r in recs] == [
        ("stage", "r1_ingest", "start"),
        ("stage", "r1_judge", "start"),
        ("stage", "r4_model", "start"),
    ]
    # 边界只进逐步记录这一路：trace.jsonl 仍是**一份**契约，不因它多出记录。
    assert len(read_trace(tmp_path / "trace.jsonl")) == 5


def test_journal_is_not_declared_as_an_artifact(tmp_path: Path) -> None:
    """★ 逐步记录**不得**进产物清单 —— 进去就会让回放永远报「不可复现」。

    ``_finalize_manifest`` 把 run 目录里的 ``*.json`` / ``*.md`` / ``*.xlsx`` /
    ``*.csv`` / ``*.png`` 统统收进 ``outputs`` 并记哈希。``.jsonl`` 不在其中
    （``cache/llm.jsonl`` 同理）。这条测试钉住那个「恰好不在其中」：哪天有人把
    模式改成 ``*.json*`` 或加一条兜底扫描，回放就会开始逐字节比对一份**带时间戳**
    的记录 —— 永远不一致。
    """
    from agent.runtime.loop import Stage

    import sys
    import types

    def stage_body(ctx: RunContext) -> dict:
        ctx.tracer.tool(name="test.echo", args={"text": "z"}, ok=True, latency_ms=1)
        ctx.tracer.llm(role="r", model="m", prompt_id="p", prompt_version="1",
                       prompt_sha256="s", prompt_tokens=1, completion_tokens=1,
                       latency_ms=1, cache_hit=False, text="{}")
        return {}

    mod = types.ModuleType("_test_stages_journal")
    mod.body = stage_body  # type: ignore[attr-defined]
    sys.modules["_test_stages_journal"] = mod

    ctx = _bare_context(tmp_path, run_id="jr")
    plan = Plan(name="t", stages=[Stage(name="j", fn_ref="_test_stages_journal:body")])
    manifest = Loop(ctx, plan).run()

    assert _journal(ctx.run_dir), "跑完了却没有逐步记录"
    assert not [k for k in manifest.outputs if k.endswith(".jsonl")], (
        f"逐步记录混进了产物清单：{[k for k in manifest.outputs if k.endswith('.jsonl')]}"
    )
    assert "trace.jsonl" not in manifest.outputs

# 这些字段名一旦出现在产物里，就会让同一输入的两次运行产出不同哈希，
# 「逐字节可复现」失效。它们属于**运行过程记录**（trace.jsonl），
# 不属于**产物**（runs/<id>/ 下的业务文件）。
_VOLATILE_KEYS = {
    "elapsed_ms", "elapsed_s", "latency_ms", "duration_ms", "runtime_ms",
    "timestamp", "ts", "now", "generated_at", "produced_at",
}


def test_channel_artifacts_contain_no_volatile_fields(tmp_path: Path, project_root: Path) -> None:
    """★ 通道产物中不得出现耗时类字段。

    实测踩过的坑：``channels/ocr.json`` 里存了 ``elapsed_ms``，
    导致每次运行产物哈希都不同，``agent replay --strict`` 判定不可复现。
    更隐蔽的是 —— 该缺陷在未安装 OCR 引擎时**不会暴露**
    （那时 ocr.json 是空数组），装上引擎后才显形。

    这条测试直接扫描真实跑出来的产物，不依赖任何 mock。
    """
    import subprocess
    import sys

    if not any((project_root / "data" / "raw").glob("*.pdf")):
        pytest.skip("缺少 data/raw 下的测试材料")

    runs_root = tmp_path / "runs"
    r = subprocess.run(
        [
            sys.executable, "-m", "agent.cli", "run", "r1_extract",
            "--mock", "--run-id", "volatile-check",
            "--runs-root", str(runs_root), "--quiet",
        ],
        cwd=project_root, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stdout + r.stderr

    def scan(node: object, path: str, hits: list[str]) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in _VOLATILE_KEYS:
                    hits.append(f"{path}.{k}")
                scan(v, f"{path}.{k}", hits)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                scan(v, f"{path}[{i}]", hits)

    run_dir = runs_root / "volatile-check"
    for rel in ("channels/ocr.json", "channels/text_layer.json", "facts/documents.json"):
        target = run_dir / rel
        if not target.exists():
            continue
        hits: list[str] = []
        scan(json.loads(target.read_text(encoding="utf-8")), rel, hits)
        assert not hits, (
            f"{rel} 含易变字段 {hits[:5]} —— 会让产物哈希每次不同，"
            "破坏可复现性。耗时记录应写入 trace.jsonl"
        )


# ==========================================================================
# 调度器：Plan
# ==========================================================================


def test_plan_loads_from_yaml(project_root: Path) -> None:
    plan = Plan.for_pipeline("r1_extract", root=project_root / "pipelines")
    assert [s.name for s in plan.stages] == [
        "ingest", "extract_text", "extract_ocr", "extract_tables",
        "judge", "build_facts", "export",
    ]


def test_plan_full_pipeline_has_all_three_requirements(project_root: Path) -> None:
    plan = Plan.for_pipeline("full", root=project_root / "pipelines")
    names = [s.name for s in plan.stages]
    assert any(n.startswith("r1_") for n in names)
    assert any(n.startswith("r4_") for n in names)
    assert any(n.startswith("r5_") for n in names)


def test_plan_rejects_missing_yaml(tmp_path: Path) -> None:
    from agent.runtime.errors import ConfigError

    with pytest.raises(ConfigError):
        Plan.for_pipeline("nonexistent", root=tmp_path)


def test_stage_resolve_rejects_bad_ref() -> None:
    from agent.runtime.errors import ConfigError
    from agent.runtime.loop import Stage

    with pytest.raises(ConfigError, match="module:function"):
        Stage(name="x", fn_ref="no-colon").resolve()
    with pytest.raises(ConfigError, match="无法导入"):
        Stage(name="x", fn_ref="no.such.module:run").resolve()
    with pytest.raises(ConfigError, match="不存在函数"):
        Stage(name="x", fn_ref="agent.runtime.budget:no_such_fn").resolve()


# ==========================================================================
# 调度器：执行与降级
# ==========================================================================


def test_loop_runs_stages_in_order(tmp_path: Path) -> None:
    order: list[str] = []

    def make(tag: str):
        def _run(ctx: RunContext):
            order.append(tag)
            return {"tag": tag}
        return _run

    import sys
    import types

    mod = types.ModuleType("_test_stages")
    mod.s1 = make("one")  # type: ignore[attr-defined]
    mod.s2 = make("two")  # type: ignore[attr-defined]
    sys.modules["_test_stages"] = mod

    plan = Plan(
        name="t",
        stages=[
            __import__("agent.runtime.loop", fromlist=["Stage"]).Stage(
                name="a", fn_ref="_test_stages:s1"
            ),
            __import__("agent.runtime.loop", fromlist=["Stage"]).Stage(
                name="b", fn_ref="_test_stages:s2"
            ),
        ],
    )
    ctx = _bare_context(tmp_path)
    manifest = Loop(ctx, plan).run()
    assert order == ["one", "two"]
    assert manifest.status == "success"


def test_loop_marks_partial_on_optional_stage_failure(tmp_path: Path) -> None:
    from agent.runtime.errors import HarnessError
    from agent.runtime.loop import Stage

    def boom(ctx: RunContext):
        raise HarnessError("故意失败")

    def ok(ctx: RunContext):
        return {}

    import sys
    import types

    mod = types.ModuleType("_test_stages2")
    mod.boom = boom  # type: ignore[attr-defined]
    mod.ok = ok  # type: ignore[attr-defined]
    sys.modules["_test_stages2"] = mod

    plan = Plan(
        name="t",
        stages=[
            Stage(name="a", fn_ref="_test_stages2:boom", optional=True),
            Stage(name="b", fn_ref="_test_stages2:ok"),
        ],
    )
    ctx = _bare_context(tmp_path)
    manifest = Loop(ctx, plan).run()
    # 可选阶段失败 -> partial 且后续阶段继续执行
    assert manifest.status == "partial"


def test_loop_stops_on_required_stage_failure(tmp_path: Path) -> None:
    from agent.runtime.errors import HarnessError
    from agent.runtime.loop import Stage

    ran: list[str] = []

    def boom(ctx: RunContext):
        raise HarnessError("必须失败")

    def later(ctx: RunContext):
        ran.append("later")
        return {}

    import sys
    import types

    mod = types.ModuleType("_test_stages3")
    mod.boom = boom  # type: ignore[attr-defined]
    mod.later = later  # type: ignore[attr-defined]
    sys.modules["_test_stages3"] = mod

    plan = Plan(
        name="t",
        stages=[
            Stage(name="a", fn_ref="_test_stages3:boom", optional=False),
            Stage(name="b", fn_ref="_test_stages3:later"),
        ],
    )
    ctx = _bare_context(tmp_path)
    manifest = Loop(ctx, plan).run()
    assert manifest.status == "failed"
    assert ran == [], "必需阶段失败后不得继续执行后续阶段"


def test_loop_writes_checkpoint_for_resume(tmp_path: Path) -> None:
    from agent.runtime.loop import Stage

    calls: list[str] = []

    def stage_a(ctx: RunContext):
        calls.append("a")
        return {}

    def stage_b(ctx: RunContext):
        calls.append("b")
        return {}

    import sys
    import types

    mod = types.ModuleType("_test_stages4")
    mod.a = stage_a  # type: ignore[attr-defined]
    mod.b = stage_b  # type: ignore[attr-defined]
    sys.modules["_test_stages4"] = mod

    plan = Plan(
        name="t",
        stages=[
            Stage(name="a", fn_ref="_test_stages4:a"),
            Stage(name="b", fn_ref="_test_stages4:b"),
        ],
    )
    ctx = _bare_context(tmp_path)
    Loop(ctx, plan).run()
    assert (ctx.run_dir / "checkpoint.json").exists()

    # 断点续跑：已完成的阶段不应重跑
    calls.clear()
    ctx2 = _bare_context(tmp_path, run_id=ctx.run_id)
    Loop(ctx2, plan, resume=True).run()
    assert calls == [], "resume 时应跳过已完成的阶段"


def test_checkpoint_records_the_provider_while_the_run_is_live(
    tmp_path: Path,
) -> None:
    """跑的过程中就要能从断点里读出 provider —— 不必等 manifest。

    `manifest.json` 是**跑完**才落的，而控制台右栏展示原始对话恰恰是在**运行中**。
    只靠 manifest 读 provider 的话，「这个 run 是 mock，回答是罐头」这句话在
    最需要它的那段时间里是空的 —— 而这正是罐头最容易被当成真话的时候。
    """
    import sys
    import types

    from agent.evidence.models import RunManifest
    from agent.runtime.loop import Stage

    mod = types.ModuleType("_test_stages4b")
    mod.a = lambda ctx: {}  # type: ignore[attr-defined]
    sys.modules["_test_stages4b"] = mod

    plan = Plan(name="t", stages=[Stage(name="a", fn_ref="_test_stages4b:a")])
    ctx = _bare_context(tmp_path)
    ctx.manifest = RunManifest(run_id=ctx.run_id, pipeline="t", provider="mock")
    Loop(ctx, plan).run()

    cp = json.loads((ctx.run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    assert cp["provider"] == "mock", (
        "断点里没有 provider：运行中的 mock run 与真实 run 在控制台上无从分辨"
    )


def test_loop_writes_manifest_with_output_hashes(tmp_path: Path) -> None:
    from agent.runtime.loop import Stage

    def produce(ctx: RunContext):
        (ctx.run_dir / "out.json").write_text('{"x":1}', encoding="utf-8")
        return {}

    import sys
    import types

    mod = types.ModuleType("_test_stages5")
    mod.produce = produce  # type: ignore[attr-defined]
    sys.modules["_test_stages5"] = mod

    plan = Plan(name="t", stages=[Stage(name="p", fn_ref="_test_stages5:produce")])
    ctx = _bare_context(tmp_path)
    Loop(ctx, plan).run()

    manifest = json.loads((ctx.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert "out.json" in manifest["outputs"]
    assert len(manifest["outputs"]["out.json"]) == 64  # sha256 十六进制
    assert manifest["finished_at"] is not None


def test_partial_failures_mark_run_as_partial(tmp_path: Path) -> None:
    """★ 阶段"完成但有部分失败"必须让**整次运行**标为 partial。

    实测事故：一步表格裁决的 VLM 调用失败 1 次，阶段报 success、产物照常落盘、
    运行状态也是 success。后果是 ``agent replay --strict`` 说"不可复现"
    却给不出原因 —— 那次失败的调用没有响应缓存，离线回放只能产出不同结果。

    失败当时**已经被计数**（`table_cells_failed`），问题在于计数只进了产物，
    没有传导到运行状态，于是没人看。这条测试钉住"计数 → 状态"的传导。
    """
    import sys
    import types

    from agent.runtime.loop import Stage, _partial_marker

    def half_failed(ctx: RunContext):
        (ctx.run_dir / "out.json").write_text('{"x":1}', encoding="utf-8")
        return {"table_cells_failed": 1, "partial_failures": {"单元格裁决调用失败": 1}}

    def clean(ctx: RunContext):
        (ctx.run_dir / "out.json").write_text('{"x":1}', encoding="utf-8")
        return {"table_cells_failed": 0}

    mod = types.ModuleType("_test_stages6")
    mod.half_failed = half_failed  # type: ignore[attr-defined]
    mod.clean = clean  # type: ignore[attr-defined]
    sys.modules["_test_stages6"] = mod

    # 有失败 → partial
    ctx = _bare_context(tmp_path, run_id="half")
    plan = Plan(name="t", stages=[Stage(name="j", fn_ref="_test_stages6:half_failed")])
    manifest = Loop(ctx, plan).run()
    assert manifest.status == "partial", (
        f"部分失败却仍是 {manifest.status} —— replay 报不可复现时无人知道原因"
    )

    # 无失败 → success（不能把 partial 变成默认值）
    ctx2 = _bare_context(tmp_path, run_id="ok")
    plan2 = Plan(name="t", stages=[Stage(name="j", fn_ref="_test_stages6:clean")])
    manifest2 = Loop(ctx2, plan2).run()
    assert manifest2.status == "success"

    # 标记解析
    assert _partial_marker({"partial_failures": {"a": 2, "b": 0}}) == "a 2"
    assert _partial_marker({}) == ""
    assert _partial_marker(None) == ""


def _bare_context(tmp_path: Path, *, run_id: str = "t1") -> RunContext:
    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return RunContext(
        run_id=run_id,
        run_dir=run_dir,
        pipeline="test",
        tracer=Tracer(run_dir / "trace.jsonl"),
        budget=Budget(),
    )


# ==========================================================================
# 端到端（mock 模型，零成本）
# ==========================================================================


@pytest.mark.slow
def test_end_to_end_full_pipeline_mock(tmp_path: Path, project_root: Path) -> None:
    """完整跑通 R1 → R4 → R5，并校验全部关键产物存在。

    这是 P2 Harness 内核的**验收测试**。
    """
    import subprocess
    import sys

    raw = project_root / "data" / "raw"
    if not any(raw.glob("*.pdf")):
        pytest.skip("缺少 data/raw 下的测试材料")

    runs_root = tmp_path / "runs"
    result = subprocess.run(
        [
            sys.executable, "-m", "agent.cli", "run", "full",
            "--mock", "--run-id", "e2e",
            "--runs-root", str(runs_root),
            "--quiet",
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    run = runs_root / "e2e"
    for rel in (
        "manifest.json",
        "trace.jsonl",
        "facts/facts.json",
        "facts/evidence.html",
        "valuation/valuation.json",
        "valuation/valuation_report.md",
        "audit/audit.json",
        "audit/audit_report.md",
    ):
        assert (run / rel).exists(), f"缺少产物 {rel}"

    # 事件上的阶段名只许是**这个计划里真实存在的阶段**（`__` 开头的是编排层
    # 自己的伪阶段 `__boot__` / `__loop__`，不属于任何计划）。
    #
    # 实测踩到（2026-10-12）：流水线模块各自写死了**单跑时**的阶段名 ——
    # R1-4 写成 `judge`、R5 写成 `audit`、第 2 层写成 `extract_ocr`。
    # 跑 `r1_extract` 时这些名字是对的，跑 `full` 时它们对应的阶段叫
    # `r1_judge` / `r5_check` / `r1_extract_ocr`。于是在一份真实的 full run
    # （`runs/web-20261012-205543`）里：88 条 `tool_call` 全挂在 `judge` 上、
    # 376 条 `routing` 挂在 `ingest`/`extract_ocr` 上 —— 三个计划里根本不存在
    # 的名字，控制台按阶段汇总时这些事件无处落座。
    # 名字现在只由编排层给：它每进一个阶段发一次 `stage_event(status="start")`，
    # `Tracer._in_stage` 记下来补给不带 `stage` 的调用。
    plan_names = {
        s.name for s in Plan.for_pipeline("full", root=project_root / "pipelines").stages
    }
    stray: dict[str, int] = {}
    for line in (run / "trace.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        stage = json.loads(line).get("stage")
        if stage is not None and stage not in plan_names and not stage.startswith("__"):
            stray[stage] = stray.get(stage, 0) + 1
    assert not stray, f"trace 里出现了计划外的阶段名：{stray}（计划阶段：{sorted(plan_names)}）"

    # ---- 文件访问（赛题四类事件的第一类，2026-10-17 接入编排层）----
    #
    # 这条断言刻意**只看一次真实 run 的 trace.jsonl**，不看源码里有没有
    # `FileGuard` —— 接入之前本仓的状态就是「类写得很好、`pipelines/` 里
    # 零处 `ctx.guard()`」，实测每次 full run 的 file_access 都是 0，
    # 而源码看起来完全正常。所以判据只能是跑出来的数。
    #
    # 三层，缺一层就挡不住：
    #   ① 有 —— 事件数不为 0（防"接入被改回去"）
    #   ② 对 —— 每条都要有相对路径 / 合法 mode / sha256 / 业务阶段
    #   ③ 全 —— 盘上每个产物都有对应的写记录（"完整"二字的唯一判据）
    events = [
        json.loads(line)
        for line in (run / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    file_events = [e for e in events if e["kind"] == "file_access"]
    assert file_events, "本次 run 的 trace.jsonl 里一条 file_access 都没有 —— 又绕开 ctx.guard() 了"

    for e in file_events:
        rel = e["path"]
        # 绝对路径会把本机用户名（`C:\Users\<名字>\…`）写进 trace.jsonl，
        # 而那是要交给评审的交付物。
        assert rel and not Path(rel).is_absolute() and ":" not in rel, (
            f"文件访问记了绝对路径：{rel}"
        )
        assert e["mode"] in ("read", "write", "list"), f"{rel} 的 mode 非法：{e['mode']}"
        assert e["path_sha256"], f"{rel} 的访问记录没有 sha256"
        assert e["stage"] and not e["stage"].startswith("__"), (
            f"{rel} 的访问没有归到业务阶段（stage={e['stage']}）—— "
            "读者就无法把这次访问挂到某一步上"
        )

    written = {e["path"] for e in file_events if e["mode"] == "write"}
    # 编排层自己维护的台账不走 guard（它们是"关于这次运行"的记录，
    # 不是本次运行的产物），列出来免得每次都得重新推一遍。
    ledger = {".lock", "agent_log.jsonl", "checkpoint.json", "manifest.json", "trace.jsonl"}
    missing = sorted(
        p.relative_to(run).as_posix()
        for p in run.rglob("*")
        if p.is_file()
        and p.relative_to(run).as_posix() not in ledger
        and p.relative_to(run).as_posix() not in written
    )
    assert not missing, f"这些产物在盘上却没有 file_access 写记录：{missing}"


    facts = json.loads((run / "facts/facts.json").read_text(encoding="utf-8"))
    assert facts["facts"], "事实库不应为空"
    # 契约硬闸门：每条事实必须有证据锚点
    for f in facts["facts"]:
        assert f["evidence_span_ids"], f"事实 {f['fact_id']} 缺证据锚点"

    val = json.loads((run / "valuation/valuation.json").read_text(encoding="utf-8"))
    assert val["dcf"]["value_per_share"]
    failed = [a for a in val["assertions"] if a["status"] == "fail"]
    assert not failed, f"勾稽断言失败：{[a['code'] for a in failed]}"


@pytest.mark.slow
def test_end_to_end_is_reproducible(tmp_path: Path, project_root: Path) -> None:
    """结果稳定性 —— 两层验证。

    **层次一：逐字节可复现（``agent replay``）**
        同一次运行在离线模式下重放，全部产物哈希必须与原始一致。
        这是最强、也最有说服力的判据，且不调用任何模型。

    **层次二：跨运行语义等价**
        两次独立运行的产物**不可能逐字节相同** —— `manifest.started_at`
        与 `run_id` 是运行特有的。因此跨运行比对应在**剔除易变字段后**
        进行，验证的是「同样的输入产生同样的结论」。

    区分这两个层次很重要：把「跨运行字节相同」当成可复现性的判据，
    是对该概念的误解，也会导致永远无法通过的测试（本项目早期即如此）。
    """
    import subprocess
    import sys

    if not any((project_root / "data" / "raw").glob("*.pdf")):
        pytest.skip("缺少 data/raw 下的测试材料")

    runs_root = tmp_path / "runs"
    run_id = "repro"

    def run_once(attempt: int) -> Path:
        root = runs_root / f"attempt{attempt}"
        r = subprocess.run(
            [
                sys.executable, "-m", "agent.cli", "run", "full",
                "--mock", "--run-id", run_id,
                "--runs-root", str(root), "--quiet",
            ],
            cwd=project_root, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        assert r.returncode == 0, r.stdout + r.stderr
        return root / run_id

    dir1 = run_once(1)

    # ---------------- 层次一：逐字节回放 ----------------
    rep = subprocess.run(
        [
            sys.executable, "-m", "agent.cli", "replay", run_id,
            "--runs-root", str(runs_root / "attempt1"), "--strict",
        ],
        cwd=project_root, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert rep.returncode == 0, f"离线回放判定不可复现：\n{rep.stdout[-3000:]}"

    # ---------------- 层次二：跨运行语义等价 ----------------
    dir2 = run_once(2)

    # 易变字段：运行特有的时间戳与标识，不参与跨运行比对
    VOLATILE = {
        "run_id", "started_at", "finished_at", "registered_at",
        "extracted_at", "built_at", "checked_at", "produced_at",
        "as_of", "evaluated_at", "generated_at",
    }

    def normalize(node: object) -> object:
        if isinstance(node, dict):
            return {k: normalize(v) for k, v in sorted(node.items()) if k not in VOLATILE}
        if isinstance(node, list):
            return [normalize(v) for v in node]
        return node

    for rel in (
        "facts/facts.json",
        "valuation/valuation.json",
        "audit/audit.json",
    ):
        a = json.loads((dir1 / rel).read_text(encoding="utf-8"))
        b = json.loads((dir2 / rel).read_text(encoding="utf-8"))
        assert normalize(a) == normalize(b), f"{rel} 两次运行的语义内容不一致"
