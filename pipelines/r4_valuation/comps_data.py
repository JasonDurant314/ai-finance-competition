"""可比公司快照的读取与校验。

**为什么要有这一层**：R4 的相对估值原先读的是代码里手写的 `COMPS_POOL`，
现在改为读 `data/comps/<target>_<period>.json` 快照。读的时候必须校验，
否则一个被截断或改坏的文件会让估值**静默**用一个不完整的可比集 ——
而"可比公司少了三家"这种错误，从最终每股价值上是看不出来的。
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class CompsDataError(RuntimeError):
    """快照缺失或不可用 —— 显式抛出，不静默退回占位样本。"""


def default_snapshot_path(target: str, period: str,
                          data_dir: Path | str | None = None) -> Path:
    """默认快照路径：``<data_dir>/comps/<target>_<YYYY>A.json``。

    ``data_dir`` 缺省为 ``PROJECT_ROOT/data``。让它可以被覆盖是必要的：
    测试与演示会指向自己的数据根目录（``agent run --data <dir>``），
    写死 ``PROJECT_ROOT/data`` 会让它们与真实素材抢同一个位置。
    """
    year = str(period)[:4]
    base = Path(data_dir) if data_dir else (PROJECT_ROOT / "data")
    if not base.is_absolute():
        base = PROJECT_ROOT / base
    return base / "comps" / f"{target}_{year}A.json"


def load_snapshot(path: Path, *, guard: Any = None) -> dict[str, Any]:
    """读取并**校验**快照；任何不满足契约的地方都显式报错。

    校验项（都对应真实会出错的地方）：
      * 文件存在且是合法 JSON
      * 有 ``rows`` 且非空
      * 每一行有 ``code``，且 ``pe`` / ``pb`` 至少有一个可解析为 Decimal
      * 记录 ``sources`` / ``as_of`` —— 缺了就无法在报告里标注数据来源与时点
    """
    if not path.is_file():
        raise CompsDataError(
            f"找不到可比公司快照：{path}\n"
            "请先运行：python tools/fetch_comps.py --target <代码> --as-of <报告期>\n"
            "（该工具从公开接口取真实行情与年报财务数据，并与人工核对真值比对后才落盘）"
        )
    try:
        data = (guard.read_json(path) if guard is not None
                else json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise CompsDataError(f"快照不是合法 JSON：{path}（{exc}）") from exc

    rows = data.get("rows") or []
    if not rows:
        raise CompsDataError(f"快照里没有任何可比公司：{path}")
    for key in ("as_of", "sources"):
        if not data.get(key):
            raise CompsDataError(f"快照缺少 {key} —— 无法标注数据来源与时点：{path}")

    usable = 0
    for row in rows:
        if not row.get("code"):
            raise CompsDataError(f"快照某一行缺少 code：{row}")
        vals = []
        for k in ("pe", "pb"):
            v = row.get(k)
            if v in (None, ""):
                continue
            try:
                vals.append(Decimal(str(v)))
            except (InvalidOperation, ValueError) as exc:
                raise CompsDataError(
                    f"{row['code']} 的 {k} 无法解析为数值：{v!r}") from exc
        if vals:
            usable += 1
    if usable == 0:
        raise CompsDataError(f"快照里没有任何一行同时具备可用倍数：{path}")
    return data
