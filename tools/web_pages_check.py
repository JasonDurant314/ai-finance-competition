"""核验全流程 run 的页面渲染（估值 / 核查 / 双路互证）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from tools.web_smoke import pick_runs  # noqa: E402
from web.app import create_app  # noqa: E402

# ⚠️ run 名**不能写死** —— 实测踩过：脚本里写死了某个 run，
# 后来它被清理掉，脚本随即报出"缺失 3 项"的假失败。
# 按磁盘现状挑最新的 R1 run 与全流程 run。
_r1, _full = pick_runs()
client = TestClient(create_app(runs_root="runs", default_run=_full))
print(f"全流程 run = {_full}    R1 run = {_r1}\n")

CASES = [
    (f"/valuation?run={_full}", ["勾稽断言", "敏感性", "可比公司", "占位样本"]),
    (f"/audit?run={_full}", ["核查", "E4", "修订"]),
    (f"/facts?run={_r1}", ["抽取路径构成", "双路互证", "独立复现"]),
    (f"/facts?run={_full}", ["抽取路径构成", "追溯", "证据锚点"]),
    (f"/extract?run={_full}", ["表格结构重建", "算术校验的如实说明"]),
]

# ⚠️ 反面探针：页面**不得**出现这些串。
#
# 「命中清单」只能证明页面没漏掉该有的东西，**不能证明页面没多出不该有的东西**。
# 2026-09-27 的教训：`why_self_computed` 里那句「接口 PB 字段 1.22 vs 自算 0.86」
# 从头到尾都是编的（快照里根本没有这两个数），
# 而当时的探针只检查「有没有渲染出这一栏」，于是它一路绿灯。
# 补上反面探针：把已知的错误断言钉死，防止它以任何形式回流。
FORBIDDEN = [
    (f"/valuation?run={_full}", ["1.22", "0.86"],
     "已证伪的 PE/PB 口径断言（快照里 market_cap÷equity_parent=1.1950，不存在 0.86）"),
    # 「文件访问」这一格前后写过两版假话，两版都钉住：
    #   ① 「框架层统一记录…不会出现追溯断点」—— 写它的时候 file_accesses 恒为 0；
    #   ② 「未接入编排层」—— 2026-10-17 接入编排层之后，这句对新 run 也是假的。
    # 判据只能是本 run 的 trace（页面那一格就是这么渲的，见 web/templates/runs.html）。
    ("/runs", ["不会因为某个模块忘了写日志而出现追溯断点", "未接入编排层"],
     "这两句都已被证伪：file_access 自 2026-10-17 起由 ctx.guard() 逐点记录"),
]

bad = 0
for path, probes in CASES:
    r = client.get(path)
    body = r.text
    hit = [p for p in probes if p in body]
    miss = [p for p in probes if p not in body]
    print(f"{r.status_code}  {path}")
    print(f"      命中 {len(hit)}/{len(probes)}：{hit}")
    if miss:
        print(f"      ✗ 缺失：{miss}")
        bad += 1
    if r.status_code != 200:
        bad += 1

for path, needles, why in FORBIDDEN:
    body = client.get(path).text
    found = [n for n in needles if n in body]
    if found:
        print(f"❌ {path} 出现**已证伪**的断言：{found}")
        print(f"      原因：{why}")
        bad += 1

print(f"\n{'✅ 全部渲染正常，且无已证伪断言' if bad == 0 else f'❌ {bad} 项异常'}")
sys.exit(1 if bad else 0)
