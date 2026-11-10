"""MCP 协议客户端 —— 自研实现（JSON-RPC 2.0 over stdio，零新增依赖）。

赛题要求提交的核心模块清单里包含 MCP。本包提供：

- :class:`MCPClient` —— 单服务器连接：initialize / tools/list / tools/call
- :class:`MCPManager` —— 按 ``configs/mcp.yaml`` 管理多个服务器
- :func:`register_into` —— 把 MCP 工具接入本地工具注册表（与自有工具同构）

设计取舍见 ``agent/mcp/client.py`` 顶部注释：安全边界、只做工具面、白名单环境变量。
"""

from agent.mcp.client import (
    PROTOCOL_VERSION,
    MCPClient,
    MCPManager,
    McpCallResult,
    McpServerConfig,
    MCPServerStatus,
    McpTool,
    register_into,
)

__all__ = [
    "PROTOCOL_VERSION",
    "MCPClient",
    "MCPManager",
    "McpCallResult",
    "McpServerConfig",
    "MCPServerStatus",
    "McpTool",
    "register_into",
]
