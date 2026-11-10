"""领域工具集。所有工具均为纯函数式，通过 ``@tool`` 登记元数据。"""

from domain.tools import (
    audit_rules,
    document,
    finance,
    image,
    ocr,
    tables,
    vlm,
)

__all__ = [
    "document",
    "image",
    "ocr",
    "tables",
    "vlm",
    "finance",
    "audit_rules",
]
