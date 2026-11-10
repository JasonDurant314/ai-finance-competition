"""临时脚本：证明「文档诚信体检」真的会失败（注入 → 断言 → 还原）。

目的：一条永远通过的检查等于没有检查。这里把三处文档各注入一个
不存在的模块名，跑对应测试，确认它**报错**，然后原样还原。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.console import enable_utf8_output  # noqa: E402

# Windows 控制台默认 GBK：本脚本会打印 ✓ / ✅ 这类符号，
# 不先切 UTF-8 就会在**最需要它的时候**抛 UnicodeEncodeError 中断。
enable_utf8_output()

ROOT = Path(__file__).resolve().parents[1]

CASES = [
    (
        "agent/__init__.py",
        "trace/      追踪、回放、审计报告",
        "trace/      追踪、回放、审计报告\nghostmod/   注入的不存在模块",
        "tests/test_static_health.py::test_agent_module_list_matches_disk",
    ),
    (
        "README.md",
        "├── mcp/                      MCP 协议客户端（JSON-RPC over stdio，自研）",
        "├── mcp/                      MCP 协议客户端（JSON-RPC over stdio，自研）\n"
        "├── ghostmod/                 注入的不存在模块",
        "tests/test_static_health.py::test_readme_architecture_lists_real_modules",
    ),
    (
        "THIRD_PARTY.md",
        "| `agent/mcp/` | MCP 协议客户端（JSON-RPC 2.0 over stdio，手写实现） |",
        "| `agent/mcp/` | MCP 协议客户端（JSON-RPC 2.0 over stdio，手写实现） |\n"
        "| `agent/ghostmod/` | 注入的不存在模块 |",
        "tests/test_static_health.py::test_documents_do_not_claim_unimplemented_modules",
    ),
]


def main() -> int:
    failures = 0
    for rel, old, new, test_id in CASES:
        path = ROOT / rel
        original = path.read_text(encoding="utf-8")
        if old not in original:
            print(f"⚠️  {rel}: 锚点未找到，跳过（文档已变动，请更新本脚本）")
            failures += 1
            continue
        try:
            path.write_text(original.replace(old, new, 1), encoding="utf-8", newline="\n")
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", test_id, "-q", "--no-header"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            caught = proc.returncode != 0
            tail = [ln for ln in proc.stdout.splitlines() if "assert not" in ln or "声明了" in ln or "引用了" in ln]
            print(f"{'✅' if caught else '❌'} {rel} → {test_id.split('::')[1]}")
            print(f"     注入后退出码={proc.returncode}（非 0 才算真的检出）")
            if tail:
                print(f"     报错：{tail[0].strip()[:150]}")
            if not caught:
                failures += 1
        finally:
            path.write_text(original, encoding="utf-8", newline="\n")
            assert path.read_text(encoding="utf-8") == original, f"{rel} 未还原！"

    print()
    if failures:
        print(f"❌ {failures} 条检查未能检出注入 —— 它们形同虚设")
        return 1
    print("✅ 三条文档诚信检查都能检出注入（且文档已原样还原）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
