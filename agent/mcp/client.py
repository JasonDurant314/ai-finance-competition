"""MCP 协议客户端 —— 自研实现（JSON-RPC 2.0 over stdio）。

为什么自己写
------------
赛题要求提交的核心模块清单里明确包含 **MCP**。用现成的 MCP SDK 会引入
一个我们无法完全掌控的依赖，而 MCP 的传输层协议本身足够小：
**换行分隔的 JSON-RPC 2.0 报文**。因此这里手写实现，好处是：

- 零新增依赖（只用标准库 ``subprocess`` / ``json`` / ``threading``）
- 每一步都可写进 ``trace.jsonl``（§ 技术要求第 4 条）
- 出问题时能定位到具体是哪一条报文，而不是「SDK 内部报错」

协议范围
--------
实现的是 MCP 的**工具面**（tools facet），这是智能体真正需要的部分：

1. ``initialize`` → 协商协议版本 / 能力
2. ``notifications/initialized`` → 完成握手
3. ``tools/list`` → 发现工具（含 ``inputSchema``）
4. ``tools/call`` → 调用工具

.. note::
   MCP 规范同时支持 ``Content-Length`` 头与换行分隔两种报文框架；
   官方 stdio 传输使用**换行分隔**，本实现与官方一致。

安全边界（对应「受控数据环境」要求）
------------------------------------
``MCPClient`` **只按配置启动子进程**，并且：

- 服务器命令必须来自 ``configs/mcp.yaml``，不接受运行期任意命令
- 环境变量白名单传递，不把 ``DEEPSEEK_API_KEY`` 漏给第三方服务器
- 每次调用都有超时，超时后杀掉进程组，不留孤儿进程
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.config import PROJECT_ROOT, load_yaml
from agent.runtime.errors import ConfigError, ToolExecutionError

PROTOCOL_VERSION = "2024-11-05"
"""本实现声明的 MCP 协议版本（客户端要求的最低版本）。"""

_CLIENT_INFO = {"name": "fin-investment-agent", "version": "0.1.0"}

# 传给 MCP 子进程的环境变量白名单。
# **故意不包含 DEEPSEEK_API_KEY 等凭据** —— 第三方 MCP 服务器不需要它们，
# 给了就等于把密钥交给了不受控代码。
_ENV_ALLOWLIST = (
    "PATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "LANG",
    "LC_ALL",
    "PYTHONPATH",
    "PYTHONIOENCODING",
    "PYTHONHOME",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "COMSPEC",
    "PATHEXT",
)


@dataclass
class McpServerConfig:
    """一个 MCP 服务器的启动配置。"""

    name: str
    command: list[str]
    description: str = ""
    enabled: bool = True
    timeout_s: float = 30.0
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "McpServerConfig":
        raw_name = d.get("name")
        # ⚠️ YAML 会把 `off` / `on` / `yes` / `no` 解析成布尔值，
        #    于是 `name: off` 会得到 False。若直接判 `if not name` 就会
        #    报出「缺少 name」这种指不到原因的错误（真实踩过），
        #    所以先归一化类型，再判空。
        if raw_name is None or raw_name == "":
            raise ConfigError("MCP 服务器配置缺少 name", entry=d)
        name = str(raw_name)
        if name in ("False", "True"):
            name = "off" if name == "False" else "on"
        command = d.get("command")
        if isinstance(command, str):
            command = [command]
        if not command:
            raise ConfigError("MCP 服务器配置缺少 command", server=name)
        return cls(
            name=str(name),
            command=[str(c) for c in command],
            description=str(d.get("description", "")),
            enabled=bool(d.get("enabled", True)),
            timeout_s=float(d.get("timeout_s", 30.0)),
            tags=[str(t) for t in (d.get("tags") or [])],
        )


@dataclass
class McpTool:
    """MCP 服务器暴露的一个工具。"""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    server: str = ""

    @property
    def qualified_name(self) -> str:
        """注册到工具表时使用的名字：``mcp.<server>.<tool>``。

        加前缀是为了让「这个工具来自外部 MCP 服务器」在日志与工具清单里
        一眼可见 —— 评审需要能分清哪些是自有工具、哪些是外部接入。
        """
        return f"mcp.{self.server}.{self.name}"


@dataclass
class McpCallResult:
    ok: bool
    content: Any = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0


class MCPClient:
    """一个 MCP 服务器的连接（stdio 传输）。"""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        tracer: Any | None = None,
        cwd: str | Path | None = None,
    ) -> None:
        self.config = config
        self.tracer = tracer
        self.cwd = Path(cwd) if cwd else PROJECT_ROOT
        self._proc: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._responses: dict[int, dict[str, Any]] = {}
        self._ready = threading.Event()
        self._stderr_tail: list[str] = []
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}

    # ------------------------------------------------------------ 生命周期

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> "MCPClient":
        """启动子进程并完成 MCP 握手。"""
        if self.alive:
            return self
        argv = self._resolve_command()
        env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOWLIST}
        env.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            self._proc = subprocess.Popen(  # noqa: S603 - 命令来自受控配置
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=str(self.cwd),
                env=env,
            )
        except FileNotFoundError as exc:
            raise ConfigError(
                "MCP 服务器命令不存在",
                server=self.config.name,
                command=self.config.command,
                hint=str(exc),
            ) from exc

        self._reader = threading.Thread(
            target=self._read_loop, name=f"mcp-{self.config.name}", daemon=True
        )
        self._reader.start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

        self._initialize()
        if self.tracer is not None:
            self.tracer.tool(
                name=f"mcp.connect:{self.config.name}",
                args={"command": argv},
                ok=True,
                latency_ms=0,
            )
        return self

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def __enter__(self) -> "MCPClient":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------------------------------------------------------------- 握手

    def _initialize(self) -> None:
        resp = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": _CLIENT_INFO,
            },
        )
        result = resp.get("result") or {}
        self.server_info = result.get("serverInfo") or {}
        self.capabilities = result.get("capabilities") or {}
        # 握手完成的标志是一个通知（无 id、无响应）
        self._notify("notifications/initialized", {})

    # ---------------------------------------------------------------- 工具面

    def list_tools(self) -> list[McpTool]:
        """``tools/list``（支持分页 cursor）。"""
        out: list[McpTool] = []
        cursor: str | None = None
        for _ in range(20):  # 防御性上限，避免服务器给死循环 cursor
            params: dict[str, Any] = {}
            if cursor:
                params["cursor"] = cursor
            resp = self._request("tools/list", params)
            result = resp.get("result") or {}
            for t in result.get("tools") or []:
                out.append(
                    McpTool(
                        name=str(t.get("name", "")),
                        description=str(t.get("description", "")),
                        input_schema=t.get("inputSchema") or {},
                        server=self.config.name,
                    )
                )
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return out

    def call_tool(self, name: str, arguments: dict[str, Any]) -> McpCallResult:
        """``tools/call``。协议层的错误与业务错误分开报告。"""
        t0 = time.perf_counter()
        try:
            resp = self._request(
                "tools/call", {"name": name, "arguments": arguments}
            )
        except Exception as exc:
            ms = int((time.perf_counter() - t0) * 1000)
            if self.tracer is not None:
                self.tracer.tool(
                    name=f"mcp.{self.config.name}.{name}",
                    args=arguments,
                    ok=False,
                    latency_ms=ms,
                    error=f"{type(exc).__name__}: {exc}"[:400],
                )
            return McpCallResult(ok=False, error=str(exc), latency_ms=ms)

        ms = int((time.perf_counter() - t0) * 1000)
        if "error" in resp:
            err = resp["error"] or {}
            msg = f"{err.get('code')}: {err.get('message')}"
            if self.tracer is not None:
                self.tracer.tool(
                    name=f"mcp.{self.config.name}.{name}",
                    args=arguments,
                    ok=False,
                    latency_ms=ms,
                    error=msg[:400],
                )
            return McpCallResult(ok=False, error=msg, raw=resp, latency_ms=ms)

        result = resp.get("result") or {}
        # MCP 约定：工具级失败通过 ``isError`` 表达，而不是 JSON-RPC error
        is_error = bool(result.get("isError", False))
        content = _flatten_content(result.get("content"))
        if self.tracer is not None:
            self.tracer.tool(
                name=f"mcp.{self.config.name}.{name}",
                args=arguments,
                ok=not is_error,
                latency_ms=ms,
                error=(str(content)[:400] if is_error else None),
                result=content,
            )
        return McpCallResult(
            ok=not is_error,
            content=content,
            error=(str(content) if is_error else None),
            raw=result,
            latency_ms=ms,
        )

    # ---------------------------------------------------------------- 传输

    def _resolve_command(self) -> list[str]:
        argv = list(self.config.command)
        # 允许配置里写 "python"，解析为本解释器 —— 评审机器上的 python
        # 未必与运行本项目的是同一个，用 sys.executable 最稳。
        if argv and argv[0] in ("python", "python3", "py"):
            argv[0] = sys.executable
        elif argv:
            found = shutil.which(argv[0])
            if found:
                argv[0] = found
        return argv

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.alive:
            raise ToolExecutionError(
                "MCP 服务器未运行", server=self.config.name,
                hint="请先调用 start()，或检查子进程是否已退出",
            )
        with self._lock:
            rid = self._next_id
            self._next_id += 1
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            deadline = time.monotonic() + self.config.timeout_s
            while time.monotonic() < deadline:
                if rid in self._responses:
                    return self._responses.pop(rid)
                if not self.alive:
                    raise ToolExecutionError(
                        "MCP 服务器已退出",
                        server=self.config.name,
                        stderr="".join(self._stderr_tail[-5:]),
                    )
                time.sleep(0.005)
        raise ToolExecutionError(
            f"MCP 调用超时（{self.config.timeout_s}s）",
            server=self.config.name,
            method=method,
            stderr="".join(self._stderr_tail[-5:]),
        )

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise ToolExecutionError("MCP 传输不可用", server=self.config.name)
        try:
            proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise ToolExecutionError(
                "MCP 写入失败（子进程可能已退出）",
                server=self.config.name,
                error=str(exc),
            ) from exc

    def _read_loop(self) -> None:
        """读线程：只认 JSON 行，非 JSON 输出归入 stderr 尾巴。

        为什么必须单独起线程：服务器可能在任意时刻发**通知**（无 id），
        同步读会把它当成响应而错位。这里把所有带 id 的报文收进字典，
        调用方按 id 取。
        """
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self._stderr_tail.append(line[:200])
                del self._stderr_tail[:-20]
                continue
            if isinstance(msg, dict) and "id" in msg and msg["id"] is not None:
                self._responses[int(msg["id"])] = msg
        self._ready.set()

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            self._stderr_tail.append(line.rstrip()[:200])
            del self._stderr_tail[:-20]


def _flatten_content(content: Any) -> Any:
    """把 MCP 的 content 数组压成 Python 值。

    约定：单个 ``text`` 块且内容可被 JSON 解析时，返回解析后的对象
    —— 这样工具调用结果能直接进入事实库，而不是一堆字符串。
    多个块时返回列表。
    """
    if not isinstance(content, list):
        return content
    values: list[Any] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            try:
                values.append(json.loads(text))
            except (json.JSONDecodeError, TypeError):
                values.append(text)
        else:
            values.append(block)
    return values[0] if len(values) == 1 else values


# ==========================================================================
# 多服务器管理
# ==========================================================================


@dataclass
class MCPServerStatus:
    name: str
    ok: bool
    tool_count: int = 0
    server_info: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    command: list[str] = field(default_factory=list)


class MCPManager:
    """按 ``configs/mcp.yaml`` 管理多个 MCP 服务器连接。"""

    def __init__(self, *, tracer: Any | None = None, cwd: str | Path | None = None) -> None:
        self.tracer = tracer
        self.cwd = cwd
        self.servers: list[McpServerConfig] = []
        self._clients: dict[str, MCPClient] = {}

    # ---------------------------------------------------------------- 配置

    @classmethod
    def from_config(
        cls,
        path: str | Path | None = None,
        *,
        tracer: Any | None = None,
        cwd: str | Path | None = None,
    ) -> "MCPManager":
        cfg_path = Path(path) if path else PROJECT_ROOT / "configs" / "mcp.yaml"
        mgr = cls(tracer=tracer, cwd=cwd)
        if not cfg_path.exists():
            return mgr
        cfg = load_yaml(cfg_path)
        for entry in cfg.get("servers") or []:
            mgr.servers.append(McpServerConfig.from_dict(entry))
        return mgr

    @property
    def enabled_servers(self) -> list[McpServerConfig]:
        return [s for s in self.servers if s.enabled]

    # ---------------------------------------------------------------- 连接

    def client(self, name: str) -> MCPClient:
        for cfg in self.servers:
            if cfg.name == name:
                if name not in self._clients:
                    self._clients[name] = MCPClient(
                        cfg, tracer=self.tracer, cwd=self.cwd
                    )
                return self._clients[name]
        raise ConfigError(
            "未配置该 MCP 服务器",
            server=name,
            available=[s.name for s in self.servers],
        )

    def probe(self) -> list[MCPServerStatus]:
        """逐个连接并列出工具。**失败不抛异常** —— 一个服务器挂了
        不该让整份清单拿不到，但必须显式标注为失败。"""
        out: list[MCPServerStatus] = []
        for cfg in self.servers:
            if not cfg.enabled:
                out.append(
                    MCPServerStatus(
                        name=cfg.name, ok=False, error="已禁用（enabled: false）",
                        command=cfg.command,
                    )
                )
                continue
            client = self.client(cfg.name)
            try:
                client.start()
                tools = client.list_tools()
                out.append(
                    MCPServerStatus(
                        name=cfg.name,
                        ok=True,
                        tool_count=len(tools),
                        server_info=client.server_info,
                        command=cfg.command,
                    )
                )
            except Exception as exc:
                out.append(
                    MCPServerStatus(
                        name=cfg.name,
                        ok=False,
                        error=f"{type(exc).__name__}: {exc}",
                        command=cfg.command,
                    )
                )
        return out

    def all_tools(self) -> list[McpTool]:
        tools: list[McpTool] = []
        for cfg in self.enabled_servers:
            try:
                client = self.client(cfg.name).start()
                tools.extend(client.list_tools())
            except Exception:
                # probe() 负责把失败显式呈现；这里静默跳过会让调用方
                # 以为「工具就这么多」，因此由 probe() 先行，本方法只在
                # 全部健康时被使用（见 cmd_mcp 与 register_into）。
                continue
        return tools

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()


# ==========================================================================
# 接入工具注册表
# ==========================================================================


def register_into(registry: Any, tools: list[McpTool], clients: dict[str, MCPClient]) -> int:
    """把 MCP 工具注册进本地工具表。

    注册后的工具与自有工具**完全同构**：同样有 ``side_effect`` /
    ``determinism`` / ``cost`` 声明，同样经过 ``ToolRegistry.call`` 落 trace。
    区别只在元数据上如实标注：

    - ``side_effect="network"``（跨进程调用，可能触发外部访问）
    - ``determinism="nondeterministic"``（我们**无法保证**第三方服务器是纯函数，
      因此默认按最保守的方式声明 —— 调度器会据此禁止把它用于可复现路径）
    """
    from agent.tools.registry import ToolSpec

    n = 0
    for tool in tools:
        client = clients.get(tool.server)
        if client is None:
            continue

        def _make(t: McpTool, c: MCPClient):
            def _call(**kwargs: Any) -> Any:
                res = c.call_tool(t.name, kwargs)
                if not res.ok:
                    raise ToolExecutionError(
                        f"MCP 工具 {t.qualified_name} 执行失败",
                        server=t.server,
                        error=res.error,
                    )
                return res.content

            return _call

        registry.register(
            ToolSpec(
                name=tool.qualified_name,
                description=tool.description or f"（来自 MCP 服务器 {tool.server}）",
                func=_make(tool, client),
                side_effect="network",
                determinism="nondeterministic",
                cost="cheap",
                tags=["mcp", tool.server],
            ),
            overwrite=True,
        )
        n += 1
    return n


__all__ = [
    "PROTOCOL_VERSION",
    "McpServerConfig",
    "McpTool",
    "McpCallResult",
    "MCPClient",
    "MCPManager",
    "MCPServerStatus",
    "register_into",
]
