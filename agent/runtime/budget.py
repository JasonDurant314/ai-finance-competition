"""预算控制 —— 保证 harness 在任何情况下都有界终止。

四道闸门：步数 / token / 墙钟时间 / 成本(元)。
任一超限抛 ``BudgetExceededError``，由主循环捕获并转成**优雅降级**
（status=partial + 显式缺口声明），而不是崩溃。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from agent.runtime.errors import BudgetExceededError


@dataclass
class Budget:
    """一次运行的资源上限。全部为 None 表示不限制（不推荐）。"""

    max_steps: int | None = 200
    max_tokens: int | None = 2_000_000
    max_wall_seconds: float | None = 1800.0
    max_cost_cny: float | None = 50.0

    # 运行态计数
    steps: int = 0
    tokens: int = 0
    cost_cny: float = 0.0
    started_at: float = field(default_factory=time.monotonic)
    _stage_started: dict[str, float] = field(default_factory=dict)

    # ---------------------------------------------------------------- 记账

    def tick(self, n: int = 1) -> None:
        """记录步数。"""
        self.steps += n
        self.check()

    def charge(self, tokens: int, cost_cny: float = 0.0) -> None:
        """记录 token 与成本消耗。"""
        self.tokens += tokens
        self.cost_cny += cost_cny
        self.check()

    def enter_stage(self, name: str) -> None:
        self._stage_started[name] = time.monotonic()

    def stage_elapsed(self, name: str) -> float:
        t0 = self._stage_started.get(name)
        return 0.0 if t0 is None else time.monotonic() - t0

    # ---------------------------------------------------------------- 校验

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def check(self) -> None:
        """检查全部闸门，超限即抛。"""
        if self.max_steps is not None and self.steps > self.max_steps:
            raise BudgetExceededError(
                "步数超限", steps=self.steps, limit=self.max_steps
            )
        if self.max_tokens is not None and self.tokens > self.max_tokens:
            raise BudgetExceededError(
                "token 超限", tokens=self.tokens, limit=self.max_tokens
            )
        if self.max_wall_seconds is not None and self.elapsed > self.max_wall_seconds:
            raise BudgetExceededError(
                "时长超限", elapsed_s=round(self.elapsed, 1), limit=self.max_wall_seconds
            )
        if self.max_cost_cny is not None and self.cost_cny > self.max_cost_cny:
            raise BudgetExceededError(
                "成本超限", cost_cny=round(self.cost_cny, 4), limit=self.max_cost_cny
            )

    def remaining_wall_seconds(self) -> float | None:
        if self.max_wall_seconds is None:
            return None
        return max(0.0, self.max_wall_seconds - self.elapsed)

    def snapshot(self) -> dict[str, float | int | None]:
        return {
            "steps": self.steps,
            "max_steps": self.max_steps,
            "tokens": self.tokens,
            "max_tokens": self.max_tokens,
            "cost_cny": round(self.cost_cny, 4),
            "max_cost_cny": self.max_cost_cny,
            "elapsed_s": round(self.elapsed, 2),
            "max_wall_seconds": self.max_wall_seconds,
        }


__all__ = ["Budget"]
