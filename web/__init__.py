"""Web 演示台 —— 面向决赛现场演示的可视化层。

## 定位

本包**不重新实现任何业务逻辑**，只做三件事：
    1. 读取 ``runs/<run_id>/`` 下的产物并呈现
    2. 把已记录的证据链可视化（原文高亮、置信度构成、三层引擎读数）
    3. 支持现场喂入新文件并实时展示三层解析过程

所有数字都来自 ``facts.json`` 等产物文件 —— **与命令行跑出来的是同一份数据**。
这保证了「现场演示看到的」与「评审复现得到的」完全一致。

## 依赖

    pip install fastapi uvicorn jinja2

## 启动

    python -m web.app                 # 默认 http://127.0.0.1:8000
    python -m web.app --run <run_id> --port 8080
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _bootstrap_vendor() -> None:
    """把工作区内的 ``_vendor/`` 加入 ``sys.path``（**仅在包确实缺失时**）。

    为什么需要：受限沙箱下 site-packages 不可写，``pip install`` 会失败。
    此时可以用 ``python tools/install_wheel.py fastapi`` 把纯 Python 包
    解包到工作区内的 ``_vendor/``。

    ⚠️ 这只是**应急通道**：正常安装路径是 ``pip install -r requirements.txt``。
    因此这里的逻辑是「先看能不能正常导入，不能才回退」——
    **绝不覆盖**已经正确安装的包，也不修改任何全局状态。
    """
    try:
        import fastapi  # noqa: F401

        return  # 正常安装可用，什么都不做
    except ImportError:
        pass

    vendor = _PROJECT_ROOT / "_vendor"
    if vendor.is_dir() and str(vendor) not in sys.path:
        sys.path.insert(0, str(vendor))


_bootstrap_vendor()

# 依赖就绪后再暴露读取层（避免在缺依赖时因导入失败而无法给出友好提示）
from web.artifacts import (  # noqa: E402
    ArtifactError,
    EvidenceView,
    FactView,
    RunArtifacts,
    RunSummary,
    available_runs,
    load_run,
    resolve_run,
)

__all__ = [
    "ArtifactError",
    "EvidenceView",
    "FactView",
    "RunArtifacts",
    "RunSummary",
    "available_runs",
    "load_run",
    "resolve_run",
]
