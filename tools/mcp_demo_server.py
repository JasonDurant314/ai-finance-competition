"""演示用 MCP 服务器 —— 纯标准库、纯确定性、完全离线。

用途有两个：

1. **证明 MCP 客户端真的能工作**（``python -m agent.cli mcp --self-test``）。
   客户端代码再漂亮，没有对端跑不起来就没有说服力。
2. **给评审一个可读的 MCP 服务器样例** —— 60 行实现换行分隔的 JSON-RPC 2.0，
   说明这个协议小到可以自己写，不需要引入 SDK。

暴露的工具（全部确定性、无副作用、不联网）：

- ``number.normalize``  —— 复用本项目的中文金融数字归一化（含万/亿换算）
- ``table.grid_shape``  —— 由二维数组算出行列数与非空单元格数
- ``text.char_stats``   —— 字符/中英文/数字计数

协议：每行一条 JSON 报文，请求带 ``id`` 则必须回响应，通知不回。
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal, InvalidOperation

PROTOCOL_VERSION = "2024-11-05"

# ==========================================================================
# 工具实现
# ==========================================================================

_MULTIPLIERS = {"万": Decimal(10_000), "亿": Decimal(100_000_000)}


def _normalize_number(raw: str) -> dict:
    """把中文金融写法归一为「数值 + 单位」。

    这是本项目 ``agent/validate/numeric.py`` 能力的**最小复刻**：
    演示 MCP 服务器如何把本地的确定性能力暴露给别的智能体。
    真正的归一化仍在主进程完成 —— 这个工具只是通道示例。
    """
    text = str(raw).strip().replace(",", "").replace("，", "")
    unit = ""
    for suffix in ("元", "%", "股", "元每股", "倍"):
        if text.endswith(suffix):
            unit = suffix
            text = text[: -len(suffix)]
            break
    scale = Decimal(1)
    for marker, mult in _MULTIPLIERS.items():
        if marker in text:
            scale = mult
            text = text.replace(marker, "")
            unit = unit or "元"
            break
    try:
        value = Decimal(text) * scale
    except InvalidOperation:
        return {"ok": False, "error": f"无法解析为数字：{raw!r}"}
    normalized = format(value.normalize(), "f")
    return {"ok": True, "value": normalized, "unit": unit, "scale": str(scale)}


def _grid_shape(rows: list) -> dict:
    if not isinstance(rows, list):
        return {"ok": False, "error": "rows 必须是二维数组"}
    heights = [len(r) if isinstance(r, list) else 0 for r in rows]
    filled = sum(
        1
        for r in rows
        if isinstance(r, list)
        for c in r
        if str(c).strip() != ""
    )
    return {
        "ok": True,
        "row_count": len(rows),
        "col_count": max(heights) if heights else 0,
        "ragged": len(set(heights)) > 1,
        "filled_cells": filled,
        "empty_cells": sum(heights) - filled,
    }


def _char_stats(text: str) -> dict:
    s = str(text)
    return {
        "ok": True,
        "total": len(s),
        "cjk": sum(1 for c in s if "\u4e00" <= c <= "\u9fff"),
        "digits": sum(1 for c in s if c.isdigit()),
        "ascii_letters": sum(1 for c in s if c.isascii() and c.isalpha()),
        "whitespace": sum(1 for c in s if c.isspace()),
    }


TOOLS = {
    "number.normalize": {
        "handler": _normalize_number,
        "description": "把中文金融数字写法归一为数值与单位（支持 万/亿 换算）",
        "schema": {
            "type": "object",
            "properties": {
                "raw": {"type": "string", "description": "原始写法，如 1.2亿元"},
            },
            "required": ["raw"],
        },
    },
    "table.grid_shape": {
        "handler": _grid_shape,
        "description": "计算表格网格的行列数、是否参差、非空单元格数",
        "schema": {
            "type": "object",
            "properties": {
                "rows": {
                    "type": "array",
                    "items": {"type": "array"},
                    "description": "二维数组形式的表格",
                },
            },
            "required": ["rows"],
        },
    },
    "text.char_stats": {
        "handler": _char_stats,
        "description": "统计文本的字符构成（中文/数字/字母/空白）",
        "schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
}


# ==========================================================================
# JSON-RPC 2.0 over stdio（换行分隔）
# ==========================================================================


def _reply(rid, result=None, error=None) -> None:
    msg = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle(msg: dict) -> None:
    rid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}

    # 通知（无 id）不需要响应
    if method == "notifications/initialized":
        return

    if method == "initialize":
        _reply(
            rid,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "demo-tools", "version": "1.0.0"},
            },
        )
        return

    if method == "tools/list":
        _reply(
            rid,
            {
                "tools": [
                    {
                        "name": name,
                        "description": spec["description"],
                        "inputSchema": spec["schema"],
                    }
                    for name, spec in TOOLS.items()
                ]
            },
        )
        return

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        spec = TOOLS.get(name)
        if spec is None:
            _reply(
                rid,
                error={"code": -32602, "message": f"未知工具：{name}"},
            )
            return
        try:
            result = spec["handler"](**args)
        except TypeError as exc:
            # 工具级错误用 isError 表达，而不是 JSON-RPC error
            _reply(
                rid,
                {
                    "content": [{"type": "text", "text": f"参数错误：{exc}"}],
                    "isError": True,
                },
            )
            return
        _reply(
            rid,
            {
                "content": [
                    {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                ],
                "isError": not result.get("ok", True),
            },
        )
        return

    if rid is not None:
        _reply(rid, error={"code": -32601, "message": f"不支持的方法：{method}"})


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict):
            _handle(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
