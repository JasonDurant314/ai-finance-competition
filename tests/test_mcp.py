"""MCP 客户端测试 —— 真的起子进程，真的走协议。

为什么不用假对象（mock）：这个模块的存在意义就是「协议实现是真的」。
如果测试只对着一个假的对端打桩，那它验证的是打桩代码，不是协议。
因此这里的测试一律启动 ``tools/mcp_demo_server.py``（纯标准库、离线、耗时 5ms 级），
与 ``python -m agent.cli mcp --self-test`` 走的是同一条路径。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.config import PROJECT_ROOT
from agent.mcp.client import (
    MCPClient,
    MCPManager,
    McpServerConfig,
    McpTool,
    register_into,
)
from agent.runtime.errors import ConfigError, ToolExecutionError
from agent.trace.tracer import Tracer, read_trace

DEMO = PROJECT_ROOT / "tools" / "mcp_demo_server.py"


def _config(**kw) -> McpServerConfig:
    base = {"name": "demo", "command": ["python", str(DEMO)], "timeout_s": 20}
    base.update(kw)
    return McpServerConfig.from_dict(base)


@pytest.fixture()
def client():
    c = MCPClient(_config())
    c.start()
    yield c
    c.close()


# ==========================================================================
# 启动与握手
# ==========================================================================


def test_start_performs_handshake(client: MCPClient) -> None:
    assert client.alive
    assert client.server_info.get("name") == "demo-tools"
    assert "tools" in client.capabilities


def test_python_command_resolves_to_current_interpreter() -> None:
    """配置里写 "python" 必须解析为**当前解释器**。

    评审机器上 `python` 未必指向跑本项目的那个解释器；写死会造成
    「在我机器上能跑」。这里用 ``sys.executable`` 兜住。
    """
    import sys

    c = MCPClient(_config())
    argv = c._resolve_command()
    assert argv[0] == sys.executable
    assert argv[1:] == [str(DEMO)]


def test_missing_command_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="命令不存在"):
        MCPClient(_config(command=["definitely-not-a-real-binary-xyz"])).start()


def test_config_requires_name_and_command() -> None:
    with pytest.raises(ConfigError, match="缺少 name"):
        McpServerConfig.from_dict({"command": ["python"]})
    with pytest.raises(ConfigError, match="缺少 command"):
        McpServerConfig.from_dict({"name": "x"})
    # 允许把 command 写成单个字符串
    cfg = McpServerConfig.from_dict({"name": "x", "command": "python"})
    assert cfg.command == ["python"]


def test_yaml_boolean_names_are_normalized() -> None:
    """YAML 会把 ``off`` / ``on`` 解析成布尔值，名字不能因此变成 False。

    真实踩过：``name: off`` 报出「缺少 name」—— 因为 yaml 给出的是布尔 False，
    而报错信息完全指不到真正的原因。
    """
    assert McpServerConfig.from_dict({"name": False, "command": ["x"]}).name == "off"
    assert McpServerConfig.from_dict({"name": True, "command": ["x"]}).name == "on"


# ==========================================================================
# 工具面
# ==========================================================================


def test_list_tools_returns_schemas(client: MCPClient) -> None:
    tools = client.list_tools()
    names = {t.name for t in tools}
    assert {"number.normalize", "table.grid_shape", "text.char_stats"} <= names
    for t in tools:
        assert t.input_schema.get("type") == "object"
        assert t.description
        assert t.qualified_name == f"mcp.demo.{t.name}"


def test_call_tool_parses_json_payload(client: MCPClient) -> None:
    """content 里的 JSON 文本块应被解析为对象，而不是留下一串字符串。"""
    res = client.call_tool("number.normalize", {"raw": "1.2亿元"})
    assert res.ok
    assert isinstance(res.content, dict)
    assert res.content["value"] == "120000000"
    assert res.content["unit"] == "元"


def test_call_tool_reports_tool_level_error(client: MCPClient) -> None:
    """工具级失败（isError）必须体现为 ok=False，不能被当成成功。"""
    res = client.call_tool("number.normalize", {"raw": "不是数字"})
    assert res.ok is False
    assert res.error


def test_call_unknown_tool_returns_protocol_error(client: MCPClient) -> None:
    res = client.call_tool("no.such.tool", {})
    assert res.ok is False
    assert "-32602" in (res.error or "")


def test_call_tool_after_close_is_reported_not_silent() -> None:
    """连接关闭后调用必须报错，而不是静默返回空结果。"""
    c = MCPClient(_config())
    c.start()
    c.close()
    res = c.call_tool("text.char_stats", {"text": "x"})
    assert res.ok is False
    assert res.error


def test_call_timeout_is_reported() -> None:
    """超时必须抛错并带上服务器名 —— 挂死的对端不能拖住整个流水线。"""
    slow = PROJECT_ROOT / "_mcp_selftest" / "_slow_server.py"
    slow.parent.mkdir(parents=True, exist_ok=True)
    slow.write_text(
        "import sys, time\n"
        "for line in sys.stdin:\n"
        "    time.sleep(30)\n",
        encoding="utf-8",
    )
    try:
        c = MCPClient(
            McpServerConfig(name="slow", command=["python", str(slow)], timeout_s=1.0)
        )
        c.start()  # initialize 就会超时
        pytest.fail("应当因超时抛错")
    except ToolExecutionError as exc:
        assert "超时" in str(exc)
    finally:
        slow.unlink(missing_ok=True)


# ==========================================================================
# 追踪（赛题要求「记录工具调用」）
# ==========================================================================


def test_calls_are_traced(tmp_path: Path) -> None:
    tracer = Tracer(tmp_path / "trace.jsonl")
    c = MCPClient(_config(), tracer=tracer)
    c.start()
    c.call_tool("text.char_stats", {"text": "质押 2,557 万股"})
    c.call_tool("number.normalize", {"raw": "不是数字"})  # 失败也要留痕
    c.close()
    tracer.close()

    events = read_trace(tmp_path / "trace.jsonl")
    names = [e.tool for e in events]
    assert "mcp.connect:demo" in names
    assert "mcp.demo.text.char_stats" in names
    failed = [e for e in events if e.tool == "mcp.demo.number.normalize"]
    assert failed and failed[0].ok is False, "失败调用必须留下 ok=False 的记录"


# ==========================================================================
# 环境变量白名单（安全边界）
# ==========================================================================


def test_credentials_are_not_passed_to_server() -> None:
    """★ API Key 绝不能传给第三方 MCP 服务器。

    这不是理论风险：MCP 服务器是**外部进程**，把凭据给它等于把密钥
    交给不受控代码。客户端只传白名单环境变量。
    """
    from agent.mcp.client import _ENV_ALLOWLIST

    assert "DEEPSEEK_API_KEY" not in _ENV_ALLOWLIST
    assert "OPENAI_API_KEY" not in _ENV_ALLOWLIST
    assert "PATH" in _ENV_ALLOWLIST  # 否则子进程找不到解释器


def test_server_cannot_see_api_key(tmp_path: Path) -> None:
    """端到端验证：让子进程把收到的环境变量回吐出来。"""
    spy = tmp_path / "spy_server.py"
    spy.write_text(
        "import json, os, sys\n"
        "for line in sys.stdin:\n"
        "    msg = json.loads(line)\n"
        "    if msg.get('method') == 'tools/call':\n"
        "        out = {'jsonrpc': '2.0', 'id': msg['id'], 'result': {\n"
        "            'content': [{'type': 'text', 'text': json.dumps({\n"
        "                'has_key': any('API_KEY' in k for k in os.environ)})}],\n"
        "            'isError': False}}\n"
        "        print(json.dumps(out), flush=True)\n"
        "    elif 'id' in msg:\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'],\n"
        "                          'result': {'protocolVersion': '2024-11-05',\n"
        "                                     'capabilities': {}, 'serverInfo': {}}}),\n"
        "              flush=True)\n",
        encoding="utf-8",
    )
    c = MCPClient(McpServerConfig(name="spy", command=["python", str(spy)]))
    c.start()
    res = c.call_tool("probe", {})
    c.close()
    assert res.ok and isinstance(res.content, dict)
    assert res.content["has_key"] is False, "子进程竟然能看到 API Key"


# ==========================================================================
# 接入工具注册表
# ==========================================================================


def test_register_into_tool_registry(client: MCPClient) -> None:
    from agent.tools.registry import ToolRegistry

    tools = client.list_tools()
    reg = ToolRegistry()
    n = register_into(reg, tools, {"demo": client})
    assert n == len(tools)
    assert "mcp.demo.text.char_stats" in reg

    spec = reg.get("mcp.demo.text.char_stats")
    # 第三方服务器无法保证是纯函数 → 按最保守方式声明，调度器据此
    # 不会把它用在可复现路径上
    assert spec.determinism == "nondeterministic"
    assert spec.side_effect == "network"

    got = reg.call("mcp.demo.text.char_stats", {"text": "营业收入 1.2 亿元"})
    assert got["cjk"] == 6


def test_registered_mcp_tool_failure_becomes_tool_execution_error(
    client: MCPClient,
) -> None:
    from agent.tools.registry import ToolRegistry

    reg = ToolRegistry()
    register_into(reg, client.list_tools(), {"demo": client})
    with pytest.raises(ToolExecutionError):
        reg.call("mcp.demo.number.normalize", {"raw": "不是数字"})


def test_mcp_tool_names_are_prefixed() -> None:
    t = McpTool(name="x", description="", server="fs")
    assert t.qualified_name == "mcp.fs.x"


# ==========================================================================
# 管理多个服务器 / 配置
# ==========================================================================


def test_manager_without_config_has_no_servers() -> None:
    mgr = MCPManager.from_config(PROJECT_ROOT / "configs" / "mcp.yaml")
    assert mgr.servers == [], (
        "默认配置必须为空：MCP 是可选扩展面，默认不启动任何外部进程"
        "（对应赛题「封闭或半封闭数据环境」）"
    )
    assert mgr.probe() == []


def test_manager_probe_reports_failure_without_raising(tmp_path: Path) -> None:
    """一个服务器挂掉不该让整份清单拿不到 —— 但必须显式标注失败。"""
    cfg = tmp_path / "mcp.yaml"
    cfg.write_text(
        "servers:\n"
        "  - name: broken\n"
        "    command: ['definitely-not-a-real-binary-xyz']\n"
        "  - name: demo\n"
        f"    command: ['python', '{DEMO.as_posix()}']\n",
        encoding="utf-8",
    )
    mgr = MCPManager.from_config(cfg)
    statuses = {s.name: s for s in mgr.probe()}
    assert statuses["broken"].ok is False
    assert statuses["broken"].error
    assert statuses["demo"].ok is True
    assert statuses["demo"].tool_count == 3
    assert len(mgr.all_tools()) == 3
    mgr.close()


def test_disabled_server_is_not_started(tmp_path: Path) -> None:
    cfg = tmp_path / "mcp.yaml"
    cfg.write_text(
        "servers:\n"
        "  - name: off\n"
        f"    command: ['python', '{DEMO.as_posix()}']\n"
        "    enabled: false\n",
        encoding="utf-8",
    )
    mgr = MCPManager.from_config(cfg)
    statuses = mgr.probe()
    assert len(statuses) == 1
    assert statuses[0].ok is False
    assert "禁用" in (statuses[0].error or "")
    assert mgr.enabled_servers == []


def test_unknown_server_raises_config_error() -> None:
    mgr = MCPManager.from_config(PROJECT_ROOT / "configs" / "mcp.yaml")
    with pytest.raises(ConfigError, match="未配置"):
        mgr.client("nope")


# ==========================================================================
# 仓库自带演示服务器（它是自检与测试的对端，必须自身正确）
# ==========================================================================


def test_demo_server_normalizes_chinese_units() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("mcp_demo_server", DEMO)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod._normalize_number("1.2亿元")["value"] == "120000000"
    assert mod._normalize_number("3,500万股")["value"] == "35000000"
    assert mod._normalize_number("12.5%") == {
        "ok": True, "value": "12.5", "unit": "%", "scale": "1",
    }
    assert mod._normalize_number("不是数字")["ok"] is False
    assert mod._grid_shape([["a", "b"], ["c"]])["ragged"] is True
    assert mod._char_stats("质押 2,557 万股")["digits"] == 4
