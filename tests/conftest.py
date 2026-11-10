"""pytest 全局配置与环境适配。

处理三件在受限环境（Windows 沙箱 / CI）下必然遇到的事：

1. **控制台编码**：Windows 默认 GBK，输出中文或 emoji 会抛
   ``UnicodeEncodeError`` 并中断测试收集。
2. **临时目录**：pytest 默认把 ``tmp_path`` 放在系统 Temp（Windows 上是
   ``%LOCALAPPDATA%\\Temp\\pytest-of-<user>``）。受限环境下该位置不可写，
   会产生大量 ``PermissionError`` 而非真实测试失败。因此改到工作区内。
3. **符号链接清理**：pytest 在 session 结束时会清理 basetemp 中的死符号链接，
   在 Windows 上对受限目录执行 ``iterdir`` 会抛 ``PermissionError``，
   把本来全绿的测试变成退出码非 0。此步骤对本项目无意义，直接禁用。
"""

from __future__ import annotations

import itertools
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

# 保证 `import agent` / `import domain` 可用（不依赖安装为包）
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.console import enable_utf8_output  # noqa: E402

enable_utf8_output()


# ---------------------------------------------------------------------------
# 临时目录：**覆盖 pytest 内置的 tmp_path**
#
# 为什么必须覆盖（这是踩了四次坑后的结论）：
#   pytest 的 TempPathFactory 在分配目录时会**枚举 basetemp 下的已有目录**
#   （``root.iterdir()``）以决定下一个编号。在受限沙箱中，对工作区外
#   （甚至工作区内的隐藏目录）执行目录枚举会被拒绝，于是抛出
#   ``PermissionError: [WinError 5]``，把 23 个正常测试变成 ERROR。
#
#   我们的实现只做「唯一路径 + mkdir」，不枚举、不清理、不建符号链接，
#   因此在任何可写目录下都能工作。
# ---------------------------------------------------------------------------

_TMP_SEQ = itertools.count()


def _make_temp_base() -> Path:
    """挑一个可写的临时根目录。逐个探测，全失败则退回工作区。"""
    candidates: list[Path | None] = [
        Path(os.environ["AGENT_PYTEST_TMP"]) if os.environ.get("AGENT_PYTEST_TMP") else None,
        Path(tempfile.gettempdir()) if os.environ.get("DSH_ALLOW_TMP") else None,
        ROOT / "_tmp",
    ]
    for cand in candidates:
        if cand is None:
            continue
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / f"w{next(_TMP_SEQ)}"
            probe.mkdir()
            probe.rmdir()
            return cand
        except OSError:
            continue
    return ROOT / "_tmp"


_TMP_BASE = _make_temp_base()


@pytest.fixture
def tmp_path() -> Path:
    """单测级临时目录。

    与 pytest 内置版本的差异：不依赖目录枚举，因此可在受限沙箱下工作。
    行为契约保持一致 —— 每个测试拿到一个**全新且唯一**的目录。
    """
    p = _TMP_BASE / f"t{uuid.uuid4().hex[:12]}"
    p.mkdir(parents=True, exist_ok=False)
    return p


@pytest.fixture(scope="session")
def tmp_path_factory() -> object:
    """会话级临时目录工厂（提供内置版本用到的少量接口）。"""

    class _Factory:
        def mktemp(self, basename: str, numbered: bool = True) -> Path:
            p = _TMP_BASE / f"{basename}-{uuid.uuid4().hex[:12]}"
            p.mkdir(parents=True, exist_ok=False)
            return p

        def getbasetemp(self) -> Path:
            return _TMP_BASE

    return _Factory()


@pytest.fixture(scope="session")
def project_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def sample_raw_dir(project_root: Path) -> Path:
    """合成样本材料目录。缺失时自动生成。"""
    raw = project_root / "data" / "raw"
    if not any(raw.glob("*.pdf")):
        import subprocess

        subprocess.run(
            [sys.executable, str(project_root / "tools" / "make_sample_data.py")],
            check=True,
            cwd=project_root,
        )
    return raw
