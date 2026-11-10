"""领域技能注册表 —— 把「怎么做这件事」从 harness 中隔离出来。

设计动机
--------
赛题要求提交的核心模块清单里明确包含 **Skill**。提示词管的是「对模型说什么」，
技能管的是「这件事该怎么做」：绑定哪些工具、依赖哪些提示词、产出什么契约、
以及实测踩过的坑。把技能做成**可加载、可哈希、可校验**的注册表，有三个好处：

1. **可版本化**：``skill_id@version`` + sha256 写入 ``manifest.json``，
   与提示词一样构成复现指纹的一部分。
2. **可校验**：技能里声明的工具与提示词**必须真实存在**。
   文档说绑定了某个工具、代码里却没有 —— 这类「文档撒谎」不报错，
   但评审一查就穿帮。``validate_bindings()`` 把这种漂移变成一条测试。
3. **可隔离**：领域知识写在 Markdown 里，harness 代码不含任何金融术语。

文件格式
--------
``domain/skills/<name>/SKILL.md``：一级标题 + 一张「元信息」表格，
其余为自由正文（正文原样保留，用于生成技能目录与答辩材料）。

.. code-block:: markdown

    # Skill: R1 数据结构化提取

    ## 元信息

    | 项 | 值 |
    |---|---|
    | skill_id | `r1.structured_extraction` |
    | 版本 | `1.0.0` |
    | 对应赛题 | 选题 1——数据结构化提取 |
    | 绑定工具 | `doc.*`、`table.*`、`vlm.judge_field` |
    | 绑定提示词 | `r1.extract_financial_facts` |
    | 输出契约 | `FactSet` |

工具/提示词写成逗号或顿号分隔的列表；``doc.*`` 这类通配符
表示「以 ``doc.`` 开头的任意工具」，校验时按前缀展开。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from agent.runtime.errors import ConfigError

_H1 = re.compile(r"^#\s+(.*\S)\s*$", re.MULTILINE)
_TABLE_ROW = re.compile(r"^\s*\|(.+?)\|\s*$", re.MULTILINE)
_BACKTICK = re.compile(r"`([^`]+)`")

# 「项 -> 归一化后的键」映射。同一件事在中文文档里可以有多种写法，
# 这里做一次收敛，避免注册表依赖某个特定措辞。
_KEY_ALIASES = {
    "skill_id": "id",
    "id": "id",
    "版本": "version",
    "version": "version",
    "对应赛题": "requirement",
    "赛题": "requirement",
    "requirement": "requirement",
    "绑定工具": "tools",
    "工具": "tools",
    "tools": "tools",
    "绑定提示词": "prompts",
    "提示词": "prompts",
    "prompts": "prompts",
    "输出契约": "contract",
    "契约": "contract",
    "contract": "contract",
}


@dataclass
class Skill:
    """一个已加载的领域技能。"""

    id: str
    version: str
    title: str
    requirement: str = ""
    tools: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    contract: str = ""
    doc: str = ""
    source_path: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        """唯一引用串：``skill_id@version``。"""
        return f"{self.id}@{self.version}"

    @property
    def sha256(self) -> str:
        """覆盖元信息 + 正文 —— **改一个字指纹就变**，与提示词同一套纪律。"""
        material = "|".join(
            [
                self.id,
                self.version,
                self.title,
                self.requirement,
                ",".join(self.tools),
                ",".join(self.prompts),
                self.contract,
                self.doc,
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "title": self.title,
            "requirement": self.requirement,
            "tools": list(self.tools),
            "prompts": list(self.prompts),
            "contract": self.contract,
            "sha256": self.sha256[:16],
            "source_path": self.source_path,
        }


class SkillRegistry:
    """从目录批量加载技能，并校验其绑定是否真实存在。"""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    # ---------------------------------------------------------------- 加载

    def load_dir(self, root: str | Path) -> int:
        """加载 ``<root>/*/SKILL.md``（也接受 ``<root>/*.md``）。"""
        root = Path(root)
        if not root.exists():
            return 0
        paths = sorted(root.rglob("SKILL.md")) or sorted(root.rglob("*.md"))
        n = 0
        for path in paths:
            self.load_file(path)
            n += 1
        return n

    def load_file(self, path: str | Path) -> Skill:
        p = Path(path)
        raw = p.read_text(encoding="utf-8")
        meta = _parse_meta_table(raw)
        title_m = _H1.search(raw)

        skill_id = meta.get("id")
        if not skill_id:
            raise ConfigError(
                "技能缺少 skill_id",
                path=str(p),
                hint="在「元信息」表中加一行 | skill_id | `xxx` |",
            )
        if skill_id in self._skills:
            prev = self._skills[skill_id]
            raise ConfigError(
                f"技能 id 重复：{skill_id}",
                first=prev.source_path,
                second=str(p),
            )

        skill = Skill(
            id=skill_id,
            version=str(meta.get("version", "1.0.0")),
            title=(title_m.group(1) if title_m else skill_id),
            requirement=str(meta.get("requirement", "")),
            tools=_split_refs(meta.get("tools", "")),
            prompts=_split_refs(meta.get("prompts", "")),
            contract=str(meta.get("contract", "")),
            doc=_strip_meta_table(raw).strip(),
            source_path=str(p),
            metadata=dict(meta),
        )
        self._skills[skill.id] = skill
        return skill

    def register(self, skill: Skill) -> None:
        self._skills[skill.id] = skill

    # ---------------------------------------------------------------- 查询

    def get(self, skill_id: str) -> Skill:
        if skill_id not in self._skills:
            raise ConfigError(
                "技能未注册", skill_id=skill_id, available=sorted(self._skills)
            )
        return self._skills[skill_id]

    def __contains__(self, skill_id: object) -> bool:
        return skill_id in self._skills

    def __len__(self) -> int:
        return len(self._skills)

    @property
    def ids(self) -> list[str]:
        return sorted(self._skills)

    def fingerprint(self) -> dict[str, str]:
        """``skill_id@version -> sha256``，写入 manifest.json 用于复现比对。"""
        return {s.ref: s.sha256 for s in self._skills.values()}

    def catalog(self) -> list[dict[str, Any]]:
        return [self._skills[k].to_dict() for k in self.ids]

    # ------------------------------------------------------------ 绑定校验

    def validate_bindings(
        self,
        *,
        tools: Iterable[str] = (),
        prompts: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """检查技能声明的工具/提示词是否真实存在。

        **为什么必须有这一步**：技能文档里写「绑定工具 ``table.check_arithmetic``」
        而代码里这个工具被改名或删掉时，没有任何运行期报错 ——
        文档继续描述一个不存在的系统。这类问题一旦出现在提交物里，
        属于「项目说明与实际不符」，是评审最忌讳的一类问题。

        返回每个未解析引用的明细；空列表表示全部对上。
        """
        tool_set = set(tools)
        prompt_set = set(prompts)
        problems: list[dict[str, Any]] = []
        for skill in self._skills.values():
            for ref in skill.tools:
                missing = _unmatched(ref, tool_set)
                if missing:
                    problems.append(
                        {
                            "skill": skill.id,
                            "kind": "tool",
                            "ref": ref,
                            "missing": sorted(missing),
                        }
                    )
            for ref in skill.prompts:
                missing = _unmatched(ref, prompt_set)
                if missing:
                    problems.append(
                        {
                            "skill": skill.id,
                            "kind": "prompt",
                            "ref": ref,
                            "missing": sorted(missing),
                        }
                    )
        return problems


# ==========================================================================
# 解析辅助
# ==========================================================================


def _parse_meta_table(text: str) -> dict[str, str]:
    """从「元信息」表格里取值：``| 项 | 值 |`` 两列。

    只认两列表格，且键必须落在 ``_KEY_ALIASES`` 中 —— 这样正文里的
    其他表格（如「考察点与对策」）不会被误当成元信息。
    """
    meta: dict[str, str] = {}
    for row in _TABLE_ROW.finditer(text):
        cells = [c.strip() for c in row.group(1).split("|")]
        if len(cells) != 2:
            continue
        key, value = cells
        if set(key) <= set("-: "):  # 分隔行 |---|---|
            continue
        norm = _KEY_ALIASES.get(key.strip().strip("*` "))
        if not norm:
            continue
        cleaned = " ".join(_BACKTICK.findall(value)) if "`" in value else value
        meta[norm] = cleaned.strip() or value.strip()
    return meta


def _strip_meta_table(text: str) -> str:
    """去掉元信息表格行，保留其余正文（正文用于生成技能目录）。"""
    out: list[str] = []
    for line in text.splitlines():
        row = _TABLE_ROW.match(line)
        if row:
            cells = [c.strip() for c in row.group(1).split("|")]
            if len(cells) == 2 and _KEY_ALIASES.get(cells[0].strip().strip("*` ")):
                continue
        out.append(line)
    return "\n".join(out)


def _split_refs(value: str) -> list[str]:
    """把 ``doc.*（登记/预扫描）、ocr.recognize`` 拆成 ``['doc.*', 'ocr.recognize']``。

    两种情况都要处理：
    - 每个引用各自带反引号：`` `doc.*`、`ocr.recognize` ``
    - 一整串只包一层反引号，内部用空格或标点分隔：`` `doc.* ocr.* table.*` ``

    括号里的中文说明（如 ``（注册/预扫描/取文/渲染）``）不属于引用，必须丢掉 ——
    否则校验时会把一句说明当成一个工具名，报出假缺失。
    """
    if not value:
        return []
    if "`" in value:
        pieces = _BACKTICK.findall(value)
    else:
        pieces = [value]
    out: list[str] = []
    for piece in pieces:
        # 去掉括号内的中文说明
        piece = re.sub(r"[（(][^）)]*[）)]", " ", piece)
        for token in re.split(r"[\s、,，;；/]+", piece):
            token = token.strip()
            if not token:
                continue
            # 只保留像引用串的 token：带点号的标识符（doc.register / table.* / vlm.*）
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_*]+)+", token):
                out.append(token)
    return out


def _unmatched(ref: str, available: set[str]) -> set[str]:
    """``ref`` 是否能在可用集合中找到对应项。

    ``doc.*`` 视为「以 ``doc.`` 开头的任意工具」：
    只要存在至少一个匹配就不算缺失，返回空集。
    """
    if ref.endswith(".*"):
        prefix = ref[:-1]  # 保留结尾的点
        return set() if any(a.startswith(prefix) for a in available) else {ref}
    return set() if ref in available else {ref}


__all__ = ["Skill", "SkillRegistry"]
