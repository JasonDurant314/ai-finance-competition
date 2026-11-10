"""配置加载 —— 所有可调参数集中管理，模块内禁止硬编码。

支持 ``${ENV_VAR}`` 与 ``${ENV_VAR:-default}`` 形式的环境变量展开，
**API Key 只从环境变量读，永不写入配置文件或产物**。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from agent.runtime.errors import ConfigError

def _load_dotenv(path: Path | None = None) -> None:
    """从项目根目录的 ``.env`` 加载环境变量。

    为什么需要：赛题要求「提供依赖文件及运行说明，确保评审能在指定环境中复现」。
    让评审必须手动 export 一堆变量会显著提高复现门槛。
    这里在导入时自动读取 ``.env``，**但绝不覆盖已存在的环境变量** ——
    显式设置的变量优先级更高。

    ⚠️ 安全：``.env`` 已在 ``.gitignore`` 中，绝不进入版本库；
       本函数只把值放进 ``os.environ``，不写入任何产物。
    """
    p = path or (Path(__file__).resolve().parents[1] / ".env")
    if not p.is_file():
        return
    try:
        content = p.read_text(encoding="utf-8")
    except OSError:
        return

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:  # 不覆盖已有变量
            os.environ[key] = value


_load_dotenv()

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# 项目根目录（configs/ 的父目录）
# __file__ = <root>/agent/config.py  ->  parents[0]=agent, [1]=<root>
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "configs"


def _expand_env(value: Any) -> Any:
    """递归展开 ${VAR} / ${VAR:-default}。"""
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            got = os.environ.get(name)
            if got is None:
                if default is None:
                    raise ConfigError(
                        f"配置引用了未设置的环境变量 {name}，且未提供默认值",
                        hint="请参考 .env.example 配置环境变量",
                    )
                return default
            return got

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_yaml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise ConfigError("配置文件不存在", path=str(p))
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是映射", path=str(p))
    return _expand_env(raw)


def load_config(name: str, *, config_dir: str | Path | None = None) -> dict[str, Any]:
    """加载 ``configs/<name>.yaml``。"""
    root = Path(config_dir) if config_dir else CONFIG_DIR
    return load_yaml(root / f"{name}.yaml")


# ==========================================================================
# 模型配置
# ==========================================================================


def load_models_config(config_dir: str | Path | None = None) -> dict[str, Any]:
    """加载模型与角色配置。

    期望结构::

        provider:
          type: openai_compat
          base_url: https://api.deepseek.com/v1
          api_key: ${DEEPSEEK_API_KEY}
        roles:
          default:  {model: deepseek-flash, temperature: 0.0, max_tokens: 8192}
          extractor:{model: deepseek-flash, reasoning_effort: low}
          critic:   {model: deepseek-flash, reasoning_effort: max}
          vision:   {model: deepseek-flash, supports_vision: true}
        budget:
          max_steps: 200
          max_cost_cny: 50
    """
    cfg = load_config("models", config_dir=config_dir)
    if "roles" not in cfg:
        raise ConfigError("models.yaml 缺少 roles 段")
    if "default" not in cfg["roles"]:
        raise ConfigError("models.yaml 的 roles 必须包含 default")
    return cfg


def load_tools_config(config_dir: str | Path | None = None) -> dict[str, Any]:
    """工具白名单与限额配置。"""
    try:
        return load_config("tools", config_dir=config_dir)
    except ConfigError:
        return {}


def load_routing_config(config_dir: str | Path | None = None) -> dict[str, Any]:
    """逐页路由阈值配置。"""
    try:
        return load_config("routing", config_dir=config_dir)
    except ConfigError:
        return _DEFAULT_ROUTING


_DEFAULT_ROUTING: dict[str, Any] = {
    "garbled_ratio_threshold": 0.15,
    "min_chars_for_text_layer": 50,
    "image_coverage_threshold": 0.6,
    "text_coverage_threshold": 0.6,
    "clean_garbled_threshold": 0.05,
    "rule_line_density_threshold": 40,
    "ocr_dpi": 200,
    "ocr_dpi_small_font": 300,
    "quote_fuzzy_threshold": 0.86,
    "vlm_always_for_key_fields": True,
    "max_pages_per_doc": 500,
}


__all__ = [
    "load_yaml",
    "load_config",
    "load_models_config",
    "load_tools_config",
    "load_routing_config",
    "PROJECT_ROOT",
    "CONFIG_DIR",
]
