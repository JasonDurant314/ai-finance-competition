"""模型提供方适配。新增提供方请在此登记。"""

from agent.model.providers.mock import MockProvider
from agent.model.providers.openai_compat import OpenAICompatProvider

__all__ = ["OpenAICompatProvider", "MockProvider"]
