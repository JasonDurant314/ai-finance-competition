"""跨平台控制台编码引导。

Windows 控制台默认使用 GBK，输出 emoji 或部分 Unicode 字符会抛
``UnicodeEncodeError`` 并中断整个流程。本模块在任何输出发生前
把 stdout/stderr 切到 UTF-8（``errors="replace"`` 保证极端情况下也不崩）。

在 CLI 入口与所有独立脚本的第一行调用 :func:`enable_utf8_output`。
"""

from __future__ import annotations

import sys


def enable_utf8_output() -> None:
    """把标准输出/错误切到 UTF-8。幂等，可重复调用。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # 已被重定向到不支持重配置的对象
                pass


__all__ = ["enable_utf8_output"]
