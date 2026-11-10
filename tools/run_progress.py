"""看一个 run 的 LLM 调用节奏与表格裁决进度。"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

run = sys.argv[1] if len(sys.argv) > 1 else "table-vlm4"
trace = Path("runs") / run / "trace.jsonl"
if not trace.is_file():
    raise SystemExit(f"没有 {trace}")

roles: Counter[str] = Counter()
lat: list[float] = []
tools: Counter[str] = Counter()
last_ts = ""
for line in trace.read_text(encoding="utf-8").splitlines():
    e = json.loads(line)
    if e.get("kind") == "llm_call":
        roles[e.get("prompt_id") or e.get("role") or "?"] += 1
        if e.get("latency_ms"):
            lat.append(float(e["latency_ms"]))
        last_ts = e.get("ts") or last_ts
    elif e.get("kind") == "tool_call":
        tools[e.get("tool") or "?"] += 1

print(f"run={run}  最后事件时间={last_ts}")
print("LLM 调用按提示词：")
for k, v in roles.most_common():
    print(f"   {k:<28}{v:>5}")
print("工具调用：", dict(tools))
if lat:
    lat.sort()
    print(
        f"延迟 ms：min={lat[0]:.0f} p50={lat[len(lat)//2]:.0f} "
        f"max={lat[-1]:.0f} 合计={sum(lat)/1000:.0f}s"
    )
