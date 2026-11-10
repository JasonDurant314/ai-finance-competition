"""响应缓存 —— 可复现性的命门。

缓存键 = sha256(provider | model | params | messages | json_schema | prompt_sha)
同一键得到同一响应，因此 ``replay`` 可以在**完全不调用模型**的情况下
逐字节重放整条流水线，这就是『运行结果可复现』的技术实现。

================================================================================
一、一个键只能有一个答案 —— **首次写入者胜**
================================================================================
原实现是「后写覆盖先写」。这个选择在**单写者**前提下无所谓（同一键在
一次运行里只会被写一次：第二次问它就已经命中了，不会再写）。
但一旦有**第二个写者**，覆盖就会让回放取到**别人**的答案，而产物里记的是
先写者的答案 —— 回放于是报「❌ 不可复现」，而真正的问题在缓存被写脏了。

因此改为**首次写入者胜**：已有的槽不再被覆盖，只把「有人想覆盖」记进
``overwritten_slots``。这样回放至少总是取到**第一次被记下的那个答案**，
而覆盖计数把「这份缓存被多个写者动过」变成一个**可显示的信号**，
不再是静默的错误结论。

================================================================================
二、★ 真正的坑：同一个 run 目录只能有一个写者
================================================================================
实测（本轮踩到，代价是一次完整的 8 分钟预热）：`runs/live` 的缓存里有

    总行数 569 | 唯一键 401 | 被写两次以上的键 168

且这 168 组重复键的行距集中在 26–58 行（中位 53）。行距**不为 0 也不定长**，
说明不是「同一进程内的重试」，而是**两个进程各自按同样的顺序调用**、
互相穿插着往同一个文件里追加 —— 即**两个 warm 进程同时在跑同一个 run 名**。

后果分三层，每层都值得记住：
  1. 缓存被写脏 → 回放取到的不是产物当时用的那个答案 → 「不可复现」是**假警报**，
     但报告上看起来和真的不可复现一模一样。
  2. `_finalize_manifest` 会把目录里**任何** json/md/xlsx/png 收进产物清单，
     于是另一个进程产出的裁图（如 `cell_p140_r2c4.png`）也被算作本轮产物，
     而回放这一轮根本不会渲染它 → 又一处「回放未产出」。
  3. 计数与费用翻倍，而两次运行各自的 manifest 又互相覆盖。

所以：**预热与现场演示，一个 run 名同时只能有一个进程在写。**
`Loop` 会写一个 `runs/<run_id>/.lock`（记 pid），发现锁被别人持有时**当场吼一声**。

存储：``runs/<run_id>/cache/llm.jsonl``，一行一条 JSON。
选 JSONL 而非 SQLite：可读、可 diff、可随 run 目录一起打包提交。
"""

from __future__ import annotations

import hashlib
import json
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

from agent.trace.tracer import payload_hash


def _default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if hasattr(o, "isoformat"):
        return o.isoformat()
    raise TypeError(f"无法序列化 {type(o)!r}")


def cache_key(
    *,
    provider: str,
    model: str,
    messages: list[dict[str, Any]],
    params: dict[str, Any],
    json_schema: dict[str, Any] | None,
    prompt_sha: str | None,
) -> str:
    """构造缓存键。prompt_sha 参与其中，因此换 prompt 会自动失效旧缓存。"""
    material = {
        "provider": provider,
        "model": model,
        "messages": messages,
        "params": {k: v for k, v in sorted(params.items())},
        "json_schema": json_schema,
        "prompt_sha": prompt_sha,
    }
    return payload_hash(material)


class ResponseCache:
    """磁盘缓存：**一个键一个答案，首次写入者胜**。

    命中返回响应体，未命中返回 None。槽被重复写入时**不覆盖**，只计数 ——
    见模块文档第一节与第二节。
    """

    def __init__(self, path: str | Path | None, *, enabled: bool = True) -> None:
        self.path = Path(path) if path else None
        self.enabled = enabled and self.path is not None
        self._mem: dict[str, dict[str, Any]] = {}
        self._conflicts = 0
        self._lock = threading.Lock()
        if self.enabled:
            assert self.path is not None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._load()

    # ---------------------------------------------------------------- 载入

    def _load(self) -> None:
        assert self.path is not None
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 容忍中断写入的半行
                key = rec["key"]
                prev = self._mem.get(key)
                if prev is None:
                    self._mem[key] = rec
                    continue
                # 同一个键在文件里出现第二次 —— 说明有多个写者。首次写入者胜，
                # 但把这件事记下来：它是「这份缓存不可全信」的唯一证据。
                if prev.get("response") != rec.get("response"):
                    self._conflicts += 1

    # ---------------------------------------------------------------- 读写

    def get(self, key: str) -> dict[str, Any] | None:
        """取缓存的**响应体**。

        返回的是 ``response`` 字段本身，而不是包含 key 的整条记录 ——
        调用方要的是模型响应，不该关心缓存的内部封装格式。
        """
        if not self.enabled:
            return None
        rec = self._mem.get(key)
        if rec is None:
            return None
        return rec.get("response")

    def put(self, key: str, response: dict[str, Any]) -> None:
        """写入响应；**已有槽不覆盖**（首次写入者胜）。

        为什么不再覆盖：见模块文档第一节。覆盖会让回放取到产物当时没用过的
        那个答案，从而把「缓存被写脏」伪装成「运行不可复现」。
        """
        if not self.enabled:
            return
        assert self.path is not None
        with self._lock:
            prev = self._mem.get(key)
            if prev is not None:
                if prev.get("response") != response:
                    self._conflicts += 1
                return
            rec = {"key": key, "response": response}
            self._mem[key] = rec
            with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False, default=_default) + "\n")

    def __len__(self) -> int:
        return len(self._mem)

    def __bool__(self) -> bool:
        """**显式定义为恒真**，除非被禁用。

        ⚠️ 这是一个真实踩过的坑：本类定义了 ``__len__``，于是 Python 的
        真值测试会退化为 ``len(cache) != 0`` —— 空缓存为 falsy！
        调用方写 ``if cache:`` 时，第一次运行（缓存为空）会**静默跳过写入**，
        缓存永远是空的，`replay` 与成本优化全部失效，而且不报任何错。

        显式定义 ``__bool__`` 后，``if cache:`` 表达的是「有没有配缓存」，
        这正是调用方的本意。
        """
        return self.enabled

    @property
    def stats(self) -> dict[str, int]:
        """``overwritten_slots`` > 0 ⇒ 这份缓存被**多个写者**动过，回放结论要打折。"""
        return {"entries": len(self._mem), "overwritten_slots": self._conflicts}


def hash_schema(schema: dict[str, Any] | None) -> str | None:
    if schema is None:
        return None
    blob = json.dumps(schema, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


__all__ = ["ResponseCache", "cache_key", "hash_schema"]
