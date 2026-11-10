"""可复现的 Excel 导出。

**为什么需要专门处理**：``openpyxl`` 写入 ``.xlsx`` 时会在 ``docProps/core.xml``
里嵌入当前时间（``dcterms:created`` / ``dcterms:modified``），并且 zip 成员
带本地时间戳。结果是：同一份数据两次导出，文件字节不同、哈希不同 ——
这对于声称「结果可复现」的作品是硬伤。

做法：导出后统一时间戳。对所有 ``.xlsx`` 产物生效，
因此 R1 的事实表、R4 的估值模型、R5 的修订对照表都能通过哈希比对。
"""

from __future__ import annotations

import re
import shutil
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import Any

# OOXML 的固定时间戳：取自 openpyxl 默认产物中稳定可复现的字面量
_FIXED_W3CDTF = "2020-01-01T00:00:00Z"
_FIXED_ZIP_DATE = (2020, 1, 1, 0, 0, 0)

_CREATED_RE = re.compile(rb"(<dcterms:created[^>]*>)([^<]*)(</dcterms:created>)")
_MODIFIED_RE = re.compile(rb"(<dcterms:modified[^>]*>)([^<]*)(</dcterms:modified>)")


def normalize_xlsx_metadata(path: str | Path) -> bool:
    """把 ``.xlsx`` 中的时间戳统一为固定值，使其字节级可复现。

    Returns:
        True 表示已处理；False 表示文件不存在或处理失败（不抛异常，
        因为 Excel 元数据不影响业务正确性，不应阻断流水线）。
    """
    p = Path(path)
    if not p.is_file():
        return False

    tmp = p.with_suffix(p.suffix + ".norm")
    try:
        with zipfile.ZipFile(p, "r") as src, zipfile.ZipFile(
            tmp, "w", zipfile.ZIP_DEFLATED
        ) as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename == "docProps/core.xml":
                    data = _CREATED_RE.sub(rb"\g<1>" + _FIXED_W3CDTF.encode() + rb"\g<3>", data)
                    data = _MODIFIED_RE.sub(rb"\g<1>" + _FIXED_W3CDTF.encode() + rb"\g<3>", data)
                # 重建 ZipInfo 以抹掉本地时间戳与额外字段
                info = zipfile.ZipInfo(item.filename, date_time=_FIXED_ZIP_DATE)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = item.external_attr
                dst.writestr(info, data)
        shutil.move(str(tmp), str(p))
        return True
    except Exception:
        tmp.unlink(missing_ok=True)
        return False


def write_xlsx(sheets: dict[str, list[dict[str, Any]]], path: str | Path) -> bool:
    """写出多工作表 Excel，并统一元数据以保证可复现。

    Args:
        sheets: ``{工作表名: [行字典, ...]}``

    Returns:
        True 表示成功；False 表示缺少 pandas/openpyxl（调用方据此降级）。
    """
    try:
        import pandas as pd
    except ImportError:
        return False

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with pd.ExcelWriter(p, engine="openpyxl") as writer:
            for name, rows in sheets.items():
                pd.DataFrame(rows).to_excel(writer, sheet_name=name[:31], index=False)
        normalize_xlsx_metadata(p)
        return True
    except Exception:
        return False


def json_default(obj: Any) -> Any:
    """JSON 序列化兜底：Decimal / datetime / Pydantic 模型。"""
    if isinstance(obj, Decimal):
        return str(obj)
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    raise TypeError(f"无法序列化 {type(obj)!r}")


__all__ = ["normalize_xlsx_metadata", "write_xlsx", "json_default"]
