"""agent CLI —— 命令行入口。

    agent run     <pipeline> [--data ...] [--run-id ...] [--offline] [--mock] [--resume]
    agent replay  <run_id>   [--strict]
    agent audit   <run_id>
    agent verify  <run_id>
    agent list    [--runs-root ...]
    agent prompts
    agent skills
    agent tools
    agent mcp     [--list | --self-test]
    agent contracts
    agent doctor

设计原则：**一条命令即可复现全流程**，这是评审复现我们作品的入口。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent.config import PROJECT_ROOT, load_config
from agent.console import enable_utf8_output
from agent.runtime.errors import ConfigError, HarnessError
from agent.runtime.loop import Plan, build_context, new_run_id
from agent.trace.audit import write_audit_report
from agent.trace.replay import replay_run, verify_run_integrity
from agent.validate.schema import has_jsonschema

RUNS_ROOT = PROJECT_ROOT / "runs"


# ==========================================================================
# 子命令
# ==========================================================================


def cmd_run(args: argparse.Namespace) -> int:
    from agent.runtime.loop import Loop

    pipeline = args.pipeline
    plan = Plan.for_pipeline(pipeline)
    if plan.name and not args.quiet:
        print(f"流水线：{plan.name} — {plan.description}")
        print(f"阶段：{' -> '.join(s.name for s in plan.stages)}\n")

    run_id = args.run_id or new_run_id(pipeline)

    # ★★ 不带 --resume 却指向一个**已经跑过**的 run = 静默把一份好 run 弄坏。
    #    `Tracer.__init__` 以 "a" 模式打开 trace.jsonl / agent_log.jsonl，
    #    新事件会叠在旧事件后面、`seq` 从 0 重来。
    #    实测（2026-10-17）：同 run 名跑第二遍后 trace 从 146 行变 292 行、
    #    146 个 `seq` 全部重复，紧接着 `verify` 判「❌ 不可复现」。
    #    这不是"台账难看"，是**把一份能复现的 run 弄成不能复现的** ——
    #    评审照着 README / REVIEWER 把同一条命令敲两遍就会看到这条红字。
    #    所以宁可在这里挡住，也不让它静默发生。
    prior = None if args.resume else _already_run_dir(run_id, args.runs_root)
    if prior is not None:
        raise HarnessError(
            f"runs/{run_id} 已经跑过了；不带 --resume 再跑会把新事件**追加**到旧 "
            f"trace 后面（seq 从 0 重来），随后 verify 会判「不可复现」。"
            "两条出路：① 续跑/复用缓存，加 --resume；"
            "② 从零重来，先删掉该 run 目录，或换一个 --run-id。",
            run_dir=str(prior),
        )

    # ★★ `--resume` 是「继续这一次运行」，不是「新开一次运行」——
    #    因此它**不能重新生成运行身份**。实测踩过的坑（现场演示档）：
    #      赛前 --warm --limit 1 跑完 → 现场 --live --resume（没带 --limit）
    #      → manifest 被改写成 started_at=现场时刻、doc_limit=None；
    #      而盘上的产物仍是赛前那一刻的（facts.json built_at=赛前）。
    #      于是 `replay --strict` 用现场时刻重建上下文 → 每个产物哈希都不同
    #      → 一次**本可复现**的运行被报成「❌ 不可复现」，
    #      而且回放还会按 doc_limit=None 去处理全部材料，白烧 ¥1.93 token。
    #    结论：断点续跑必须继承 manifest 里的**身份字段**。
    #    （`build_context` 每次都新建 manifest，所以继承只能在这里做。）
    prev_manifest = _load_previous_manifest(run_id, args.runs_root) if args.resume else None
    if prev_manifest is not None and prev_manifest.get("pipeline") not in (None, pipeline):
        # 换了流水线还叫同一个 run 名：调度器会忽略断点（阶段名对不上），
        # 但 LLM 缓存仍会命中。说出来，免得看到「全部阶段重跑」以为是 bug。
        print(
            f"⚠️ runs/{run_id} 上一次是 `{prev_manifest.get('pipeline')}`，"
            f"本次是 `{pipeline}` —— 阶段名对不上，断点将被忽略（缓存仍复用）。"
        )
        prev_manifest = None
    if prev_manifest is not None and not args.quiet:
        print(
            f"续跑：继承上次运行身份 started_at={prev_manifest.get('started_at')}"
            f"、doc_limit={prev_manifest.get('doc_limit')}"
        )

    ctx = build_context(
        run_id=run_id,
        runs_root=args.runs_root,
        pipeline=pipeline,
        offline=args.offline,
        use_mock=args.mock,
        seed=args.seed,
        facts_run=args.facts_run,
        started_at=_parse_ts(prev_manifest.get("started_at")) if prev_manifest else None,
    )
    # 数据目录：--data 指到某个目录时，既接受「含 raw/ 子目录的根」，
    # 也接受「直接就是材料目录」两种情况（评审手上材料组织方式不确定）。
    data_arg = Path(args.data).resolve()
    raw_dir = data_arg / "raw" if (data_arg / "raw").is_dir() else data_arg
    ctx.configs["data_dir"] = str(data_arg)
    ctx.configs["paths"] = {
        "data": str(data_arg),
        "raw": str(raw_dir),
        "golden": str((data_arg / "golden").resolve()),
        "drafts": str((data_arg / "drafts").resolve()),
        "samples": str((data_arg / "samples").resolve()),
    }
    # --limit 用于小样本试跑（真实材料动辄数百页）
    if getattr(args, "limit", None):
        ctx.configs["doc_limit"] = int(args.limit)

    # ★ 把「影响产物的运行参数」记入 manifest：回放时据此重建上下文。
    #   不记录的话，回放会用**不同的参数**重跑，产物必然不同，
    #   于是一次本可复现的运行被误判为『不可复现』。实测踩过两次：
    #     · --limit 1 试跑 → replay ❌（回放处理了全部文档）
    #     · run r4_valuation --facts-run smoke → replay ❌（找不到事实库）
    #   这类假警报比不做校验更糟：它会训练人忽略复现报告。
    #
    # ★★ 续跑时**一律继承**上次的值：盘上的产物就是按上次的参数产出的，
    #    而这次命令行有没有再写一遍 `--limit` 与产物无关（阶段被跳过）。
    #    照命令行重记，等于用「这次的意图」去描述「上次的产物」。
    if ctx.manifest is not None:
        if prev_manifest is not None:
            ctx.manifest.doc_limit = prev_manifest.get("doc_limit")
            ctx.manifest.upstream_facts_run = prev_manifest.get("upstream_facts_run")
            ctx.manifest.data_dir = prev_manifest.get("data_dir")
            # ★ 产物清单也一并继承。不继承会**误收**上一轮遗留的散件：
            #   `_finalize_manifest` 会 rglob 整个 run 目录补哈希，
            #   而 `audit.md` 是 CLI 在 loop 结束后才写的，不属任何阶段。
            #   于是续跑那一轮把它扫进 outputs —— 回放只跑阶段，永远产不出它，
            #   一次本可复现的运行被判「❌ 不可复现」（实测踩过）。
            ctx.manifest.outputs = dict(prev_manifest.get("outputs") or {})
        else:
            ctx.manifest.doc_limit = (
                int(args.limit) if getattr(args, "limit", None) else None
            )
            if getattr(args, "facts_run", None):
                ctx.manifest.upstream_facts_run = str(args.facts_run)

    # ★ 把数据目录记入 manifest：回放时据此重建上下文。
    #   不记录的话，`agent replay` 会去读默认的 data/，
    #   重放结果与原始产物必然不同，被误判为「不可复现」。
    if ctx.manifest is not None and prev_manifest is None:
        try:
            ctx.manifest.data_dir = str(
                Path(args.data).resolve().relative_to(PROJECT_ROOT)
            ).replace("\\", "/")
        except ValueError:
            # 数据目录在项目之外：记录绝对路径（回放同样可用）
            ctx.manifest.data_dir = str(Path(args.data).resolve())

    # ★ 续跑时若 `--data` 与上次记录的不一致，必须吼一声：
    #   跳过的阶段不会重读材料，盘上的产物仍来自**上次**那个目录。
    #   若个别阶段这次真的重跑了，产物就会是「两个材料目录」拼出来的，
    #   而这种混合产物在哈希上与任何一次完整运行都对不上。
    if prev_manifest is not None:
        recorded = prev_manifest.get("data_dir")
        if recorded and _resolve_data_dir(recorded) != data_arg:
            print(
                f"⚠️ 本次 --data={data_arg} 与上次记录的 {recorded} 不一致。\n"
                "   跳过的阶段不会重读材料 —— 盘上产物仍来自上次那个目录。\n"
                "   要换材料请**换一个 run 名**（或去掉 --resume 重跑）。"
            )

    print(f"run_id  : {run_id}")
    print(f"run_dir : {ctx.run_dir}")
    print(f"offline : {args.offline}   mock: {args.mock}\n")

    loop = Loop(ctx, plan, resume=args.resume)
    try:
        manifest = loop.run()
    finally:
        if ctx.tracer:
            ctx.tracer.close()

    if not args.quiet:
        try:
            report_path = write_audit_report(ctx.run_dir)
            print(f"\n审计报告：{report_path}")
        except Exception as exc:  # 审计失败不该影响主流程退出码
            print(f"（审计报告生成失败：{exc}）", file=sys.stderr)

    print(f"\n运行目录：{ctx.run_dir}")
    return 0 if manifest.status in ("success", "partial") else 1


def cmd_replay(args: argparse.Namespace) -> int:
    run_dir = _resolve_run_dir(args.run_id, args.runs_root)
    print(f"回放并校验：{run_dir}\n")

    if args.integrity_only:
        report = verify_run_integrity(run_dir)
    else:
        plan = Plan.for_pipeline(_pipeline_of(run_dir, args.runs_root))

        def runner(out_dir: Path, offline: bool) -> dict[str, str]:
            from agent.runtime.loop import Loop
            import shutil

            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

            # ★ 缓存必须在 build_context **之前**就位。
            #   `ResponseCache` 是在 build_context 内部的 `build_gateway` 里构造的，
            #   那一刻它就绑定了自己的文件路径。放在后面再拷贝，它读到的就是
            #   一个空缓存 —— 实测后果：回放 0 次 llm_call、101 次「离线缓存未命中」，
            #   所有依赖模型的产物（facts / valuation / audit）全部对不上，
            #   而报告里只会说「不可复现」，对真正的原因（缓存没就位）只字不提。
            #   顺带：旧写法里 gateway 的缓存指向的是**原始 run 的 cache/llm.jsonl**，
            #   等于回放直接读原始缓存 —— 能过，但不是因为设计对，是因为没改成对的。
            src_cache = run_dir / "cache"
            if src_cache.exists():
                shutil.copytree(src_cache, out_dir / "cache", dirs_exist_ok=True)

            ctx = build_context(
                run_id=manifest["run_id"],
                runs_root=args.runs_root,
                # ★ 产物（含 trace.jsonl）必须写到 _replay/ 下，不能碰原始 run。
                #   这个参数得在**构造时**就给对：Tracer 和 ModelGateway 都是在
                #   build_context 内部建的，gateway 会自己抓一份 tracer 引用。
                #   只在返回后改 ctx.run_dir / ctx.tracer 是改不动的 ——
                #   实测那样做，每次 replay 仍会把 397 条 llm_call 事件
                #   追加进原始 run 的 trace.jsonl，而 /runs 页面上的
                #   「事件总数 / 模型调用 / 成本」全部读自那个文件。
                run_dir=out_dir,
                pipeline=manifest["pipeline"],
                offline=offline,
                # ★ 必须复用**同一个 provider**：缓存键包含 provider 名，
                #   换 provider 会让全部缓存未命中，被误判为不可复现。
                use_mock=(manifest.get("provider") == "mock"),
                # ★ 复用原始运行的时间戳：产物中的时间字段取自它，
                #   若用当前时间，回放产物哈希必然与原始不同。
                started_at=_parse_ts(manifest.get("started_at")),
            )
            # ★ 复用原始运行的**数据目录**。不重建这一步，回放会去读
            #   默认的 data/，与原始输入不同 → 产物不同 → 假警报「不可复现」。
            data_dir = _resolve_data_dir(manifest.get("data_dir") or args.data)
            raw_dir = data_dir / "raw" if (data_dir / "raw").is_dir() else data_dir
            ctx.configs["data_dir"] = str(data_dir)
            ctx.configs["paths"] = {
                "data": str(data_dir),
                "raw": str(raw_dir),
                "golden": str(data_dir / "golden"),
                "drafts": str(data_dir / "drafts"),
                "samples": str(data_dir / "samples"),
            }
            # ★ 恢复「影响产物的运行参数」。原始运行用过 --limit / --facts-run 时，
            #   回放必须用同样的参数，否则是在重跑另一件事，报『不可复现』是假警报。
            doc_limit = manifest.get("doc_limit")
            if doc_limit:
                ctx.configs["doc_limit"] = int(doc_limit)
            facts_run = manifest.get("upstream_facts_run")
            if facts_run:
                ctx.upstream_facts_run = _resolve_facts_run_dir(facts_run, args.runs_root)
            ctx.ensure_dirs()
            loop = Loop(ctx, plan)
            loop.run()
            from agent.trace.tracer import file_sha256

            out: dict[str, str] = {}
            for rel in manifest.get("outputs", {}):
                f = ctx.run_dir / rel
                if f.exists():
                    out[rel] = file_sha256(f)
            return out

        report = replay_run(run_dir, runner=runner, strict=args.strict)

    print(report.to_markdown())
    (run_dir / "replay_report.md").write_text(
        report.to_markdown() + "\n", encoding="utf-8", newline="\n"
    )
    return 0 if report.reproducible else 2


def cmd_audit(args: argparse.Namespace) -> int:
    run_dir = _resolve_run_dir(args.run_id, args.runs_root)
    path = write_audit_report(run_dir)
    print(f"审计报告已生成：{path}")
    print(path.read_text(encoding="utf-8")[:3000])
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    run_dir = _resolve_run_dir(args.run_id, args.runs_root)
    report = verify_run_integrity(run_dir)
    print(report.to_markdown())
    return 0 if report.reproducible else 2


def cmd_list(args: argparse.Namespace) -> int:
    root = Path(args.runs_root) if args.runs_root else RUNS_ROOT
    if not root.exists():
        print(f"（目录不存在：{root}）")
        return 0
    rows = []
    for d in sorted(root.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        mf = d / "manifest.json"
        if mf.exists():
            try:
                m = json.loads(mf.read_text(encoding="utf-8"))
                rows.append((d.name, m.get("pipeline", ""), m.get("status", ""), len(m.get("outputs", {}))))
            except json.JSONDecodeError:
                rows.append((d.name, "?", "损坏", 0))
        else:
            rows.append((d.name, "?", "未完成", 0))
    if not rows:
        print("（暂无运行记录）")
        return 0
    print(f"{'run_id':<32} {'pipeline':<14} {'status':<10} {'产物数':>6}")
    print("-" * 68)
    for r in rows:
        print(f"{r[0]:<32} {r[1]:<14} {r[2]:<10} {r[3]:>6}")
    return 0


def cmd_prompts(args: argparse.Namespace) -> int:
    from agent.prompts.registry import PromptRegistry

    reg = PromptRegistry()
    n = reg.load_dir(PROJECT_ROOT / "domain" / "prompts")
    print(f"已加载 {n} 个提示词（来自 domain/prompts/）\n")
    for p in reg.catalog():
        print(f"  {p['id']}@{p['version']}  [{p['role']}]  sha={p['sha256']}")
        if p["description"]:
            print(f"      {p['description']}")
    return 0


def _load_tool_modules() -> list:
    """导入「工具模块」清单。

    只有被 ``@tool`` 装饰的函数才能被发现。数值换算与 quote 校验不在此列 ——
    它们是 ``validate/`` 层的库函数，由工具内部调用，不直接暴露给模型
    （避免模型自行做算术）。
    """
    import importlib

    modules = []
    for name in (
        "domain.tools.document",
        "domain.tools.image",
        "domain.tools.ocr",
        "domain.tools.tables",
        "domain.tools.vlm",
        "domain.tools.finance",
        "domain.tools.audit_rules",
    ):
        try:
            modules.append(importlib.import_module(name))
        except ImportError as exc:
            print(f"  ⚠️  跳过 {name}：{exc}", file=sys.stderr)
    return modules


def cmd_tools(args: argparse.Namespace) -> int:
    """列出已注册工具及其副作用/确定性标注。"""
    from agent.tools.registry import build_registry

    modules = _load_tool_modules()
    reg = build_registry(*modules)
    print(f"已注册 {len(reg)} 个工具（来自 {len(modules)} 个模块）\n")
    for t in reg.describe():
        flags = []
        if t["side_effect"] != "none":
            flags.append(f"副作用={t['side_effect']}")
        if t["determinism"] != "deterministic":
            flags.append(f"确定性={t['determinism']}")
        if t["cost"] != "free":
            flags.append(f"成本={t['cost']}")
        if t["requires_evidence"]:
            flags.append("需证据锚点")
        print(f"  {t['name']:<32} [{' '.join(flags) or 'pure / deterministic'}]")
        print(f"      {t['description']}")
    return 0


def cmd_skills(args: argparse.Namespace) -> int:
    """列出领域技能，并校验其绑定的工具/提示词是否**真实存在**。

    这一步不是装饰：技能文档写「绑定工具 `table.check_arithmetic`」
    而代码里该工具被改名或删除时，运行期不会有任何报错 ——
    文档会继续描述一个不存在的系统。``validate_bindings()`` 把这种漂移
    变成一条可见的检查（``tests/test_prompts_and_skills.py`` 里是硬断言）。
    """
    from agent.prompts.registry import PromptRegistry
    from agent.skills.registry import SkillRegistry
    from agent.tools.registry import build_registry

    skills = SkillRegistry()
    n = skills.load_dir(PROJECT_ROOT / "domain" / "skills")
    print(f"已加载 {n} 个领域技能（来自 domain/skills/）\n")

    prompts = PromptRegistry()
    prompts.load_dir(PROJECT_ROOT / "domain" / "prompts")
    tools = build_registry(*_load_tool_modules())

    for s in skills.catalog():
        print(f"  {s['id']}@{s['version']}  sha={s['sha256']}")
        print(f"      {s['title']}")
        if s["requirement"]:
            print(f"      对应赛题：{s['requirement']}")
        if s["tools"]:
            print(f"      绑定工具：{', '.join(s['tools'])}")
        if s["prompts"]:
            print(f"      绑定提示词：{', '.join(s['prompts'])}")
        if s["contract"]:
            print(f"      输出契约：{s['contract']}")
        print()

    problems = skills.validate_bindings(tools=tools.names, prompts=prompts.ids)
    if problems:
        print(f"❌ {len(problems)} 处绑定未对上（文档描述了不存在的工具/提示词）：")
        for p in problems:
            print(f"     · {p['skill']} 的 {p['kind']} `{p['ref']}` 不存在")
        return 1
    print("✅ 全部技能绑定校验通过（声明的工具与提示词均真实存在）")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    """列出 MCP 服务器配置与连通状态（`--list` / 默认行为）。"""
    from agent.mcp.client import MCPManager

    mgr = MCPManager.from_config(args.config)
    if not mgr.servers:
        print(f"（{args.config or 'configs/mcp.yaml'} 未配置任何 MCP 服务器）\n")
        print("MCP 是可选扩展面，默认不启动任何外部进程 —— 这符合赛题")
        print("「封闭或半封闭数据环境」的要求。\n")
        print("验证客户端可用性（用仓库自带的演示服务器，零依赖、离线可跑）：")
        print("    python -m agent.cli mcp --self-test")
        return 0

    print(f"已配置 {len(mgr.servers)} 个 MCP 服务器"
          f"（启用 {len(mgr.enabled_servers)} 个）\n")
    statuses = mgr.probe()
    for st in statuses:
        icon = "✅" if st.ok else "❌"
        print(f"  {icon} {st.name}")
        if st.command:
            print(f"      命令：{' '.join(st.command)}")
        if st.ok:
            info = st.server_info or {}
            print(f"      对端：{info.get('name', '?')} {info.get('version', '')}")
            print(f"      工具：{st.tool_count} 个")
        else:
            print(f"      失败：{st.error}")
        print()

    if not args.list:
        tools = mgr.all_tools()
        if tools:
            print(f"共发现 {len(tools)} 个 MCP 工具（注册名以 mcp. 开头）：\n")
            for t in tools:
                print(f"  {t.qualified_name}")
                print(f"      {t.description}")
    mgr.close()
    return 0 if all(s.ok for s in statuses) else 2


def cmd_mcp_selftest(args: argparse.Namespace) -> int:
    """★ MCP 端到端自检：真的起子进程、真的走完整协议。

    用仓库自带的 ``tools/mcp_demo_server.py``（纯标准库、离线可跑）跑一遍：

    1. initialize 握手
    2. tools/list 发现工具
    3. tools/call 调用工具并校验结果
    4. 把全过程写进追踪文件（赛题要求「记录工具调用」）

    没有这一步，「我们实现了 MCP 客户端」就只是一句话；
    有了这一步，它在任何一台装了 Python 的机器上都能被复现。
    """
    import json as _json

    from agent.mcp.client import MCPClient, McpServerConfig, register_into
    from agent.trace.tracer import Tracer

    demo_argv = [
        "python",
        str(PROJECT_ROOT / "tools" / "mcp_demo_server.py"),
    ]

    print("=" * 62)
    print("MCP 客户端自检 —— 真实子进程 + 完整协议往返")
    print("=" * 62)
    print(f"服务器命令：{' '.join(demo_argv)}\n")

    # 追踪文件写到工作区内（不用系统临时目录：受限环境下可能不可写）
    tmp = PROJECT_ROOT / "_mcp_selftest"
    tmp.mkdir(parents=True, exist_ok=True)
    trace_file = tmp / "trace.jsonl"
    if trace_file.exists():
        trace_file.unlink()
    tracer = Tracer(trace_file)
    failures: list[str] = []

    try:
        client = MCPClient(
            McpServerConfig(name="demo", command=demo_argv, timeout_s=20),
            tracer=tracer,
        )
        client.start()
        info = client.server_info or {}
        print(f"① initialize  ✅  对端 {info.get('name')} {info.get('version')}")
        caps = ", ".join(client.capabilities) if client.capabilities else "无"
        print(f"   能力声明：{caps}")

        tools = client.list_tools()
        print(f"\n② tools/list  ✅  发现 {len(tools)} 个工具")
        for t in tools:
            required = (t.input_schema or {}).get("required") or []
            print(f"   · {t.qualified_name:<26} 参数 {required}")
        if not tools:
            failures.append("tools/list 返回空")

        print("\n③ tools/call  逐个调用并校验结果")
        cases = [
            ("number.normalize", {"raw": "1.2亿元"}, "120000000"),
            ("number.normalize", {"raw": "3,500万股"}, "35000000"),
            ("table.grid_shape", {"rows": [["项目", "金额"], ["营业收入", "100"]]}, None),
            ("text.char_stats", {"text": "营业收入 1.2 亿元"}, None),
        ]
        for name, arg, expect_value in cases:
            res = client.call_tool(name, arg)
            mark = "✅" if res.ok else "❌"
            shown = _json.dumps(res.content, ensure_ascii=False)
            print(f"   {mark} {name}({_json.dumps(arg, ensure_ascii=False)})")
            print(f"        → {shown}   [{res.latency_ms} ms]")
            if not res.ok:
                failures.append(f"{name} 调用失败：{res.error}")
            if expect_value is not None and isinstance(res.content, dict):
                if str(res.content.get("value")) != expect_value:
                    failures.append(
                        f"{name} 结果不符：期望 {expect_value}，实际 {res.content.get('value')}"
                    )

        # 工具级错误必须被如实报告（isError → ok=False），不能静默成功
        bad = client.call_tool("number.normalize", {"raw": "不是数字"})
        print(f"\n④ 出错路径   {'✅' if not bad.ok else '❌'} "
              f"无法解析的输入被如实报错：{bad.error}")
        if bad.ok:
            failures.append("非法输入竟然返回成功 —— 错误被静默吞掉")

        # 未注册方法必须返回 JSON-RPC error
        unknown = client.call_tool("no.such.tool", {})
        print(f"⑤ 未知工具   {'✅' if not unknown.ok else '❌'} "
              f"协议层错误被正确抛出：{unknown.error}")
        if unknown.ok:
            failures.append("未知工具竟然调用成功")

        print("\n⑥ 接入工具注册表")
        from agent.tools.registry import ToolRegistry

        reg = ToolRegistry(tracer=tracer)
        n = register_into(reg, tools, {"demo": client})
        print(f"   ✅ 注册 {n} 个 MCP 工具（副作用/确定性声明为最保守值）")
        for spec in reg.describe():
            print(f"      {spec['name']:<26} 副作用={spec['side_effect']} "
                  f"确定性={spec['determinism']}")
        # 通过注册表调用一次 —— 证明它与自有工具**同构**
        got = reg.call("mcp.demo.text.char_stats", {"text": "质押 2,557 万股"})
        print(f"   注册表调用结果：{_json.dumps(got, ensure_ascii=False)}")

        client.close()
    except Exception as exc:
        failures.append(f"{type(exc).__name__}: {exc}")
    finally:
        tracer.close()

    events = [
        _json.loads(line)
        for line in trace_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    print(f"\n⑦ 追踪记录   ✅ trace.jsonl 共 {len(events)} 条工具调用事件")
    for e in events:
        print(f"      [{e['kind']}] {e['tool']}  ok={e['ok']}  {e['latency_ms']}ms")

    print("\n" + "=" * 62)
    if failures:
        print(f"❌ 自检未通过（{len(failures)} 项）：")
        for f in failures:
            print(f"     · {f}")
        return 1
    print("✅ MCP 客户端自检全部通过（握手 / 发现 / 调用 / 错误路径 / 注册表接入 / 追踪）")
    print(f"   追踪文件：{trace_file}")
    return 0


def cmd_contracts(args: argparse.Namespace) -> int:
    """导出数据契约为 JSON Schema，供第三方或 Web 前端使用。"""
    from agent.evidence.models import (
        AuditResult,
        Fact,
        FactSet,
        RunManifest,
        TraceEvent,
        ValuationResult,
    )

    out_dir = Path(args.out) if args.out else PROJECT_ROOT / "configs" / "schemas"
    out_dir.mkdir(parents=True, exist_ok=True)
    targets = {
        "facts": FactSet,
        "fact": Fact,
        "valuation": ValuationResult,
        "audit": AuditResult,
        "manifest": RunManifest,
        "trace_event": TraceEvent,
    }
    for name, model in targets.items():
        schema = model.model_json_schema()
        (out_dir / f"{name}.schema.json").write_text(
            json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"  ✅ {name}.schema.json")
    print(f"\n共导出 {len(targets)} 个 Schema 到 {out_dir}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """环境自检 —— 评审与队员的第一条命令。

    三级状态，避免把「可选缺失」误报成失败：
        ✅ 必需项就绪
        ⚠️ 可选项缺失（功能降级，但流水线仍可运行）
        ℹ️ 提示（如尚未放入数据，属正常状态）
        ❌ 必需项缺失 —— **只有这一级才让退出码非零**
    """
    import platform

    print("=" * 62)
    print("金融投研智能体 — 环境自检")
    print("=" * 62)
    print(f"Python      : {sys.version.split()[0]} ({platform.machine()})")
    print(f"项目根目录  : {PROJECT_ROOT}")

    # (名称, 状态, 说明)  状态: ok / warn / info / fail
    checks: list[tuple[str, str, str]] = []

    def chk(name: str, status: str, note: str = "") -> None:
        checks.append((name, status, note))

    # —— 依赖：区分必需与可选 ——
    for mod, mod_note in [
        ("pydantic", "数据契约与校验"),
        ("yaml", "配置解析"),
        ("httpx", "模型 API 客户端"),
        ("pandas", "表格与 Excel 导出"),
        ("openpyxl", "Excel 写入引擎"),
        ("matplotlib", "估值图表渲染"),
        ("fitz", "PDF 解析（AGPL-3.0，见 THIRD_PARTY.md）"),
        ("PIL", "扫描件图像预处理"),
        ("jsonschema", "严格 Schema 校验"),
        ("jinja2", "报告模板"),
        ("uvicorn", "Web 演示台（决赛用）"),
        ("fastapi", "Web 演示台（决赛用）"),
        ("rapidocr_onnxruntime", "扫描件 OCR（有扫描件时必需）"),
    ]:
        import importlib

        try:
            m = importlib.import_module(mod)
            ver = getattr(m, "__version__", "?")
            chk(f"{mod} v{ver}" if ver != "?" else mod, "ok", mod_note)
        except ImportError:
            # OCR 与 Web 属可选项；其余是必需项
            if mod in ("rapidocr_onnxruntime", "fastapi", "jsonschema", "jinja2", "uvicorn"):
                chk(mod, "warn", f"未安装 — {mod_note}")
            else:
                chk(mod, "fail", f"未安装 — {mod_note}")

    # —— 配置 ——
    for cfg_name in ("models", "tools", "routing", "assertions", "metrics"):
        try:
            load_config(cfg_name)
            chk(f"configs/{cfg_name}.yaml", "ok", "可加载")
        except ConfigError as exc:
            chk(f"configs/{cfg_name}.yaml", "fail", str(exc)[:60])

    # —— 数据 ——
    import os

    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        chk("DEEPSEEK_API_KEY", "ok", "已设置，可调用真实模型")
    else:
        chk(
            "DEEPSEEK_API_KEY", "info",
            "未设置 — mock / offline 模式仍可完整跑通全流程",
        )

    for sub, sub_note in (
        ("raw", "受控原始材料"),
        ("drafts", "待核查的研究报告草稿"),
        ("golden", "黄金测试集（评测误报率用）"),
        ("samples", "演示样例"),
    ):
        p = PROJECT_ROOT / "data" / sub
        # 排除 .gitkeep 等占位文件，只统计真实数据
        n = (
            len([f for f in p.glob("*") if f.is_file() and not f.name.startswith(".")])
            if p.exists()
            else 0
        )
        if n:
            chk(f"data/{sub}/", "ok", f"{n} 个文件 — {sub_note}")
        else:
            chk(f"data/{sub}/", "info", f"空 — {sub_note}")

    # —— 输出 ——
    print()
    width = max(len(c[0]) for c in checks) + 2
    icon = {"ok": "✅", "warn": "⚠️ ", "info": "ℹ️ ", "fail": "❌"}
    for name, status, note in checks:
        print(f"  {icon[status]} {name:<{width}} {note}")

    fails = [c for c in checks if c[1] == "fail"]
    warns = [c for c in checks if c[1] == "warn"]
    print()
    if fails:
        print(f"❌ {len(fails)} 项必需检查未通过，流水线无法运行：")
        for name, _, note in fails:
            print(f"     · {name}: {note}")
        return 1
    if warns:
        print(f"⚠️  {len(warns)} 项可选能力缺失（不影响核心流程，相关功能降级）：")
        for name, _, note in warns:
            print(f"     · {name}: {note}")
        print("\n   补齐建议：pip install rapidocr-onnxruntime fastapi jinja2")
    print("✅ 必需项全部就绪。")
    return 0


# ==========================================================================
# 辅助
# ==========================================================================


def _already_run_dir(run_id: str, runs_root: str | None) -> Path | None:
    """这个 run 名在盘上是否**已经跑出了东西**（不带 ``--resume`` 时用来挡住覆盖）。

    判据是"盘上有产物"而不是"目录存在"：上一次中途崩溃可能只留下一个空骨架，
    那种情况应当允许重新开始，不该逼人先手工删目录。
    """
    root = Path(runs_root) if runs_root else RUNS_ROOT
    d = Path(run_id) if Path(run_id).is_dir() else root / run_id
    if not d.is_dir():
        return None
    if (d / "manifest.json").is_file():
        return d
    trace = d / "trace.jsonl"
    try:
        if trace.is_file() and trace.stat().st_size > 0:
            return d
    except OSError:
        return None
    return None


def _resolve_run_dir(run_id: str, runs_root: str | None) -> Path:
    root = Path(runs_root) if runs_root else RUNS_ROOT
    direct = Path(run_id)
    if direct.is_dir():
        return direct
    target = root / run_id
    if not target.is_dir():
        raise HarnessError(
            f"找不到 run：{run_id}", runs_root=str(root),
            hint="用 `agent list` 查看可用 run",
        )
    return target


def _pipeline_of(run_dir: Path, runs_root: str | None) -> str:
    mf = run_dir / "manifest.json"
    if mf.exists():
        return json.loads(mf.read_text(encoding="utf-8")).get("pipeline", "r1_extract")
    return "r1_extract"


def _load_previous_manifest(run_id: str, runs_root: str | None) -> dict | None:
    """读上一次落在同一 run 目录里的 manifest（续跑时用来继承运行身份）。

    读不到就返回 None —— 续跑一个**全新的** run 名是合法的
    （等价于首次运行），不该因此报错。
    """
    root = Path(runs_root) if runs_root else RUNS_ROOT
    mf = Path(run_id) / "manifest.json" if Path(run_id).is_dir() else root / run_id / "manifest.json"
    if not mf.is_file():
        return None
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _parse_ts(value: str | None) -> "datetime | None":
    """解析 manifest 中的 ISO 时间戳。解析失败返回 None（由上层取当前时间）。"""
    if not value:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _resolve_data_dir(value: str | None) -> Path:
    """把 manifest 中记录的数据目录解析为绝对路径。

    相对路径按项目根目录解析；绝对路径原样使用。
    这样即使 runs 目录被拷贝到另一台机器（评审复现场景），
    只要项目结构与数据目录同名，回放仍能工作。
    """
    if not value:
        return PROJECT_ROOT / "data"
    p = Path(value)
    return p if p.is_absolute() else (PROJECT_ROOT / p)


def _resolve_facts_run_dir(value: str, runs_root: str | None = None) -> Path:
    """把 manifest 中记录的 ``--facts-run`` 解析为事实库所在目录。

    三种写法都要支持（评审手上的目录组织方式不确定）：
        1. 绝对路径 —— 原样使用
        2. 相对路径 —— 按项目根目录解析（与 ``data_dir`` 同一规则）
        3. run_id  —— 在 ``runs/`` 下查找；找不到时原样返回（由
           ``require_facts()`` 抛出带操作指引的错误，而不是在这里静默兜底）
    """
    if not value:
        raise ConfigError("facts_run 为空")
    p = Path(value)
    if p.is_absolute():
        return p
    candidate = PROJECT_ROOT / p
    if candidate.exists():
        return candidate
    root = Path(runs_root) if runs_root else RUNS_ROOT
    under_runs = root / value
    if under_runs.exists():
        return under_runs
    return candidate


# ==========================================================================
# 参数解析
# ==========================================================================


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent",
        description="金融投研智能体 — 自研 Agent Harness（竞赛作品）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--version", action="version", version="agent 0.1.0")
    sub = p.add_subparsers(dest="command", required=True)

    # run
    pr = sub.add_parser(
        "run",
        help="执行一条流水线",
        description=(
            "执行流水线。可选：r1_extract / r4_valuation / r5_audit / full（端到端）。\n"
            "R4 与 R5 只消费 R1 的事实库产物；用 --facts-run 指定上游 run，\n"
            "或用 `agent run full` 一条命令跑完三阶段。"
        ),
    )
    pr.add_argument(
        "pipeline",
        help="流水线名（pipelines/ 下的目录名）：r1_extract | r4_valuation | r5_audit | full",
    )
    pr.add_argument("--data", default="data", help="数据根目录（默认 data/）。也接受直接指向材料目录")
    pr.add_argument("--run-id", default=None, help="指定 run_id（默认按时间生成）")
    pr.add_argument("--runs-root", default=None, help="runs 根目录")
    pr.add_argument(
        "--limit", type=int, default=None,
        help="只处理前 N 个文档（真实材料动辄数百页，用于小样本试跑）",
    )
    pr.add_argument(
        "--facts-run",
        default=None,
        help="上游 R1 运行目录（run_id 或路径）。R4 / R5 用它定位 facts.json",
    )
    pr.add_argument("--offline", action="store_true", help="离线模式：只用缓存，不发网络请求")
    pr.add_argument("--mock", action="store_true", help="使用 mock 模型（无需 API Key）")
    pr.add_argument("--resume", action="store_true", help="从检查点续跑")
    pr.add_argument("--seed", type=int, default=0, help="随机种子")
    pr.add_argument("--quiet", action="store_true", help="减少输出")
    pr.set_defaults(func=cmd_run)

    # replay
    prp = sub.add_parser("replay", help="回放并校验可复现性")
    prp.add_argument("run_id")
    prp.add_argument("--runs-root", default=None)
    prp.add_argument("--strict", action="store_true", help="任何不一致即判定不可复现")
    prp.add_argument("--integrity-only", action="store_true", help="只校验产物哈希，不重跑")
    prp.set_defaults(func=cmd_replay)

    # audit
    pa = sub.add_parser("audit", help="生成人类可读审计报告")
    pa.add_argument("run_id")
    pa.add_argument("--runs-root", default=None)
    pa.set_defaults(func=cmd_audit)

    # verify
    pv = sub.add_parser("verify", help="校验 run 目录完整性")
    pv.add_argument("run_id")
    pv.add_argument("--runs-root", default=None)
    pv.set_defaults(func=cmd_verify)

    # list
    pl = sub.add_parser("list", help="列出历史运行")
    pl.add_argument("--runs-root", default=None)
    pl.set_defaults(func=cmd_list)

    # 信息类
    sub.add_parser("prompts", help="列出已注册提示词").set_defaults(func=cmd_prompts)
    sub.add_parser("skills", help="列出领域技能并校验其绑定").set_defaults(func=cmd_skills)
    sub.add_parser("tools", help="列出已注册工具").set_defaults(func=cmd_tools)

    # MCP（自研客户端：JSON-RPC 2.0 over stdio）
    pm = sub.add_parser(
        "mcp",
        help="MCP 客户端：列出/探测外部 MCP 服务器，或跑端到端自检",
        description=(
            "MCP 是可选扩展面。默认 configs/mcp.yaml 不启用任何服务器，"
            "因此本命令默认只报告配置状态。\n"
            "用 --self-test 走一遍完整协议（真实子进程 + 仓库自带演示服务器）。"
        ),
    )
    pm.add_argument("--list", action="store_true", help="只列出服务器，不查询其工具清单")
    pm.add_argument("--self-test", action="store_true", dest="self_test",
                    help="用仓库自带演示服务器跑端到端协议自检")
    pm.add_argument("--config", default=None, help="MCP 配置文件（默认 configs/mcp.yaml）")
    pm.set_defaults(func=cmd_mcp)

    pc = sub.add_parser("contracts", help="导出数据契约 JSON Schema")
    pc.add_argument("--out", default=None, help="输出目录（默认 configs/schemas/）")
    pc.set_defaults(func=cmd_contracts)

    sub.add_parser("doctor", help="环境自检").set_defaults(func=cmd_doctor)

    return p


def main(argv: list[str] | None = None) -> int:
    enable_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    # `agent mcp --self-test` 走另一条实现（它是端到端演示，不是状态查询）
    if args.command == "mcp" and getattr(args, "self_test", False):
        args.func = cmd_mcp_selftest
    try:
        return int(args.func(args))
    except HarnessError as exc:
        print(f"\n❌ {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\n中断。已完成的阶段保存在 run 目录中，可用 --resume 续跑。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
