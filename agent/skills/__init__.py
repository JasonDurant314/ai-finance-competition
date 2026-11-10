"""技能层 —— 领域技能的加载、哈希与绑定校验。

与 ``agent/prompts`` 对称：提示词管「对模型说什么」，技能管「这件事该怎么做」。
"""

from agent.skills.registry import Skill, SkillRegistry

__all__ = ["Skill", "SkillRegistry"]
