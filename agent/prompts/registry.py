"""提示词注册表 —— 提示即资产，必须可版本化、可哈希、可回放。

约定：每个 prompt 是 ``domain/prompts/<id>.md``，带 YAML front-matter：

```
---
id: r1.extract_financials
version: 1.2.0
role: extractor
description: 从财报页面抽取财务科目
output_schema: r1.financial_facts
---
<正文，支持 {var} 占位符>
```

``sha256`` 覆盖 front-matter + 正文，因此**改一个字就会导致缓存失效**，
从而保证「换了 prompt 之后结果变化可归因」。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agent.runtime.errors import ConfigError

_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


@dataclass
class Prompt:
    """一个已加载的提示词模板。"""

    id: str
    version: str
    role: str
    description: str
    template: str
    output_schema: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    source_path: str = ""

    @property
    def sha256(self) -> str:
        material = f"{self.id}|{self.version}|{self.role}|{self.template}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @property
    def ref(self) -> str:
        """唯一引用串：``prompt_id@version``。"""
        return f"{self.id}@{self.version}"

    def placeholders(self) -> set[str]:
        return set(_PLACEHOLDER.findall(self.template))

    def render(self, **variables: Any) -> str:
        """渲染模板。缺失变量**直接报错**，不允许静默留空。"""
        missing = self.placeholders() - set(variables)
        if missing:
            raise ConfigError(
                f"提示词 {self.ref} 缺少变量：{sorted(missing)}",
                provided=sorted(variables),
            )
        out = self.template
        for key, value in variables.items():
            out = out.replace("{" + key + "}", str(value))
        return out


class PromptRegistry:
    """从目录批量加载并校验提示词。"""

    def __init__(self) -> None:
        self._prompts: dict[str, Prompt] = {}

    # ---------------------------------------------------------------- 加载

    def load_dir(self, root: str | Path) -> int:
        root = Path(root)
        if not root.exists():
            return 0
        n = 0
        for path in sorted(root.rglob("*.md")):
            self.load_file(path)
            n += 1
        return n

    def load_file(self, path: str | Path) -> Prompt:
        p = Path(path)
        raw = p.read_text(encoding="utf-8")
        m = _FRONT_MATTER.match(raw)
        if not m:
            raise ConfigError(
                "提示词缺少 YAML front-matter", path=str(p), hint="文件必须以 --- 开头"
            )
        meta = yaml.safe_load(m.group(1)) or {}
        body = m.group(2).strip()
        for required in ("id", "version"):
            if required not in meta:
                raise ConfigError(f"提示词 front-matter 缺少字段 {required}", path=str(p))

        prompt = Prompt(
            id=str(meta["id"]),
            version=str(meta["version"]),
            role=str(meta.get("role", "default")),
            description=str(meta.get("description", "")),
            template=body,
            output_schema=meta.get("output_schema"),
            metadata=meta,
            source_path=str(p),
        )
        if prompt.id in self._prompts:
            prev = self._prompts[prompt.id]
            raise ConfigError(
                f"提示词 id 重复：{prompt.id}",
                first=prev.source_path,
                second=str(p),
            )
        self._prompts[prompt.id] = prompt
        return prompt

    def register(self, prompt: Prompt) -> None:
        self._prompts[prompt.id] = prompt

    # ---------------------------------------------------------------- 查询

    def get(self, prompt_id: str) -> Prompt:
        if prompt_id not in self._prompts:
            raise ConfigError(
                "提示词未注册", prompt_id=prompt_id, available=sorted(self._prompts)
            )
        return self._prompts[prompt_id]

    def __contains__(self, prompt_id: object) -> bool:
        return prompt_id in self._prompts

    def __len__(self) -> int:
        return len(self._prompts)

    @property
    def ids(self) -> list[str]:
        return sorted(self._prompts)

    def fingerprint(self) -> dict[str, str]:
        """``prompt_id@version -> sha256``，写入 manifest.json 用于复现比对。"""
        return {p.ref: p.sha256 for p in self._prompts.values()}

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "id": p.id,
                "version": p.version,
                "role": p.role,
                "description": p.description,
                "output_schema": p.output_schema,
                "sha256": p.sha256[:16],
                "placeholders": sorted(p.placeholders()),
                "source_path": p.source_path,
            }
            for p in self._prompts.values()
        ]


__all__ = ["Prompt", "PromptRegistry"]
