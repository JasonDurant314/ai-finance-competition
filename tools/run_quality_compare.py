"""对比多次 run 的归属纠正效果与冲突噪声。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

runs = sys.argv[1:] or ["final-full3", "final-full4"]
print(f"{'run':<14}{'归属纠正':>9}{'冲突':>7}{'需复核':>7}"
      f"{'118034事实':>11}{'其中需复核':>11}{'事实总数':>9}")
for run in runs:
    base = Path("runs") / run / "facts"
    if not (base / "build_stats.json").is_file():
        print(f"{run:<14} 缺产物")
        continue
    st = json.loads((base / "build_stats.json").read_text(encoding="utf-8"))
    conf = json.loads((base / "internal_conflicts.json").read_text(encoding="utf-8"))
    facts = json.loads((base / "facts.json").read_text(encoding="utf-8"))["facts"]
    sub = [f for f in facts if f["entity"] == "118034"]
    print(f"{run:<14}{st.get('entity_reattributed', 0):>9}{len(conf):>7}"
          f"{sum(1 for f in facts if f.get('needs_review')):>7}"
          f"{len(sub):>11}{sum(1 for f in sub if f.get('needs_review')):>11}"
          f"{len(facts):>9}")
