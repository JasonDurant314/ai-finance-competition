"""可比公司快照抓取 —— 从公开接口取**真实**行情与年报财务数据，落盘为可复现快照。

为什么需要它（P1）
------------------
R4 的相对估值原先用的是 `pipelines/r4_valuation/model.py::COMPS_POOL` ——
五条 `{"name": "可比公司A", "pe": "12.5", ...}` 的**手写占位样本**，
连名字都是编的。它只演示了"筛选 → 中位数 → 隐含价值区间"这条链路，
不构成任何真实估值结论。

本工具把它换成**可核对的三段式数据**：

    价格   ← 新浪行情接口（带时间戳）
    股本/市值 ← 东方财富 push2 接口
    归母净利 / 归母权益 ← 东方财富财报接口（按报告期，法定披露口径）

并用 `pe = 总市值 ÷ 归母净利`、`pb = 总市值 ÷ 归母权益` **自己算倍数**，
而不是采信接口给的 PE/PB 字段 —— 因为那些字段的口径**未在任何随附文档中标注**
（是否含少数股东权益、按合并报表还是母公司报表口径，均无从确认）。
**自己算 + 能对上真值** 比"采信一个说不清口径的字段"更可核对。

> 修订记录（2026-09-27）：此处曾写「601857 的 PB 字段给 1.22，而『总市值 ÷ 归母权益』
> 算出 0.86，两者矛盾」。该表述**无法由快照验证且与快照矛盾** —— 快照里
> `market_cap ÷ equity_parent = 1932701525758.08 ÷ 1617287000000 = 1.1950`，
> 与文档 §4.1 结果表的 PB=1.1950 一致，并不存在 0.86。
> 且本工具**从未把接口的原始 PE/PB 字段写进快照**，故"1.22"也无从复核。
> 已改为只陈述可验证的口径政策，不再引用无法回溯的具体数字。

数据源可信度验证
----------------
本工具内置一条**验证闸门**：抓完 601857 的 FY2025 归母净利后，
必须与 `data/r5_eval/truth.py` 的值逐位一致（该真值来自人工逐页核对年报）。
不一致直接报错退出，不生成快照 —— 否则一个错的基准会静默污染整条估值链。

实测（2026-09-18）：601857 / 000722 / 002716 三家的归母净利
与 truth.py **逐位一致**，因此采信该接口。

用法::

    python tools/fetch_comps.py --target 601857 --as-of 2025-12-31
    python tools/fetch_comps.py --target 601857 --as-of 2025-12-31 --out data/comps/601857_2025A.json
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from agent.console import enable_utf8_output  # noqa: E402

# Windows 控制台默认 GBK：本脚本会打印 ✓ / ✅ 这类符号，
# 不先切 UTF-8 就会在**最需要它的时候**抛 UnicodeEncodeError 中断。
enable_utf8_output()

from data.r5_eval.truth import RAW  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0"}
# 新浪行情接口要求带 Referer，否则返回 403（东方财富不需要）。
UA_SINA = {**UA, "Referer": "https://finance.sina.com.cn"}
SINA_HQ = "https://hq.sinajs.cn/list={codes}"
EM_QUOTE = ("https://push2.eastmoney.com/api/qt/stock/get"
            "?secid={secid}&fields=f43,f57,f58,f84,f116")
EM_DATA = ("https://datacenter.eastmoney.com/securities/api/data/v1/get"
           "?reportName={report}&columns={cols}"
           "&filter=(SECUCODE%3D%22{sec}%22)"
           "&pageSize=60&sortColumns=REPORT_DATE&sortTypes=-1"
           "&source=HSF10&client=PC")

# ---------------------------------------------------------------------------
# 可比公司池：**按行业与可比性人工挑选**，不是随机取样。
#
# 目标公司 601857 中国石油（石油天然气一体化）。
# 挑选规则（写在这里，而不是藏在代码逻辑里，便于评审核对与质疑）：
#   1. 同属**石油天然气**产业链（上游勘探 / 一体化 / 油服工程）
#   2. A 股上市，交易币种一致 → 市值与倍数**无需汇率换算**
#   3. 剔除与目标公司商业模式差异过大的（如纯贸易、纯新能源）
#
# 刻意**不纳入**港股/美股同业：那需要汇率换算与时点对齐，
# 会给"倍数从哪来"引入第二个可争议环节。宁可样本少而干净。
# ---------------------------------------------------------------------------
PEER_POOL: list[dict[str, str]] = [
    {"code": "600028", "market": "SH", "name": "中国石化", "segment": "一体化"},
    {"code": "600938", "market": "SH", "name": "中国海油", "segment": "上游勘探"},
    {"code": "601088", "market": "SH", "name": "中国神华", "segment": "能源综合"},
    {"code": "600346", "market": "SH", "name": "恒力石化", "segment": "炼化"},
    {"code": "002493", "market": "SZ", "name": "荣盛石化", "segment": "炼化"},
    {"code": "601808", "market": "SH", "name": "中海油服", "segment": "油服"},
    {"code": "600583", "market": "SH", "name": "海油工程", "segment": "油服工程"},
]


def _get(url: str, enc: str = "utf-8", headers: dict | None = None) -> str:
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode(enc, "ignore")


def fetch_quotes(codes: list[tuple[str, str]]) -> dict[str, dict]:
    """取实时行情：现价 + 行情时间戳。``codes`` 为 ``[(代码, 市场)]``。"""
    sina_codes = ",".join(f"{mk.lower()}{code}" for code, mk in codes)
    raw = _get(SINA_HQ.format(codes=sina_codes), enc="gbk", headers=UA_SINA)
    out: dict[str, dict] = {}
    for line in raw.strip().split("\n"):
        if '"' not in line:
            continue
        key = line.split("=")[0].strip().split("_")[-1]      # sh601857
        fields = line.split('"')[1].split(",")
        if len(fields) < 32:
            continue
        out[key[2:]] = {
            "price": fields[3] or fields[2],                 # 现价，无成交时退回昨收
            "quote_date": fields[30],
            "quote_time": fields[31],
        }
    return out


def fetch_market(code: str, market: str) -> dict:
    """取股本与总市值（东方财富 push2）。"""
    secid = f"{1 if market == 'SH' else 0}.{code}"
    d = json.loads(_get(EM_QUOTE.format(secid=secid))).get("data") or {}
    return {
        "name": d.get("f58") or "",
        "shares_total": d.get("f84"),
        "market_cap": d.get("f116"),
    }


def fetch_financials(code: str, market: str, as_of: str) -> dict:
    """取指定报告期的**法定披露口径**财务数据（东方财富财报接口）。

    ``as_of`` 形如 ``2025-12-31``。取不到该期返回空 dict ——
    由调用方决定是"剔除该样本"还是"报错"，不在这里猜。
    """
    sec = f"{code}.{market}"
    inc = json.loads(_get(EM_DATA.format(
        report="RPT_F10_FINANCE_GINCOME",
        cols="SECUCODE,REPORT_DATE,PARENT_NETPROFIT", sec=sec)))
    bal = json.loads(_get(EM_DATA.format(
        report="RPT_F10_FINANCE_GBALANCE",
        cols="SECUCODE,REPORT_DATE,TOTAL_PARENT_EQUITY", sec=sec)))

    def pick(payload: dict, field: str) -> str | None:
        for row in ((payload.get("result") or {}) or {}).get("data") or []:
            if str(row.get("REPORT_DATE", ""))[:10] == as_of:
                v = row.get(field)
                return None if v is None else str(v)
        return None

    return {
        "period": as_of,
        "net_profit_parent": pick(inc, "PARENT_NETPROFIT"),
        "equity_parent": pick(bal, "TOTAL_PARENT_EQUITY"),
    }


def _truth_check(code: str, net_profit: str | None, as_of: str) -> tuple[bool, str]:
    """验证闸门：抓到的净利必须与人工核对真值一致（仅对真值表里有的公司）。"""
    info = RAW.get(code)
    if not info or not as_of.endswith("12-31"):
        return True, "（真值表无此公司，跳过验证）"
    want = info.get(f"np_parent_{as_of[:4]}")
    if want is None:
        return True, "（真值表无该期，跳过验证）"
    if net_profit is None:
        return False, f"接口未返回 {as_of} 归母净利，无法与真值 {want} 比对"
    if Decimal(net_profit) != Decimal(str(want)):
        return False, f"接口 {net_profit} ≠ 真值 {want} —— **数据源不可信，拒绝生成快照**"
    return True, f"与真值逐位一致（{want}）"


def main() -> int:
    ap = argparse.ArgumentParser(description="抓取可比公司快照（真实行情 + 年报财务）")
    ap.add_argument("--target", default="601857", help="目标公司代码")
    ap.add_argument("--target-market", default="SH")
    ap.add_argument("--as-of", default="2025-12-31", help="财务数据报告期")
    ap.add_argument("--out", default=None, help="输出路径")
    ap.add_argument("--skip-network", action="store_true", help="只校验已有快照")
    args = ap.parse_args()

    out_path = Path(args.out) if args.out else (
        REPO / "data" / "comps" / f"{args.target}_{args.as_of[:4]}A.json")
    if args.skip_network:
        print(f"跳过网络；现有快照：{out_path}（存在={out_path.is_file()}）")
        return 0

    all_codes = [(args.target, args.target_market)] + [
        (p["code"], p["market"]) for p in PEER_POOL
    ]
    print(f"抓取 {len(all_codes)} 家公司的行情…")
    quotes = fetch_quotes(all_codes)

    rows: list[dict] = []
    problems: list[str] = []
    for code, market, name, segment in [
        (args.target, args.target_market, "（目标公司自身，用于验证数据源）", "target")
    ] + [(p["code"], p["market"], p["name"], p["segment"]) for p in PEER_POOL]:
        q = quotes.get(code) or {}
        mkt = fetch_market(code, market)
        fin = fetch_financials(code, market, args.as_of)
        ok, why = _truth_check(code, fin["net_profit_parent"], args.as_of)
        if not ok:
            problems.append(f"{code} {mkt.get('name') or name}：{why}")

        cap = Decimal(str(mkt["market_cap"])) if mkt.get("market_cap") else None
        np_ = Decimal(fin["net_profit_parent"]) if fin["net_profit_parent"] else None
        eq_ = Decimal(fin["equity_parent"]) if fin["equity_parent"] else None
        pe = (cap / np_).quantize(Decimal("0.0001")) if cap and np_ and np_ > 0 else None
        pb = (cap / eq_).quantize(Decimal("0.0001")) if cap and eq_ and eq_ > 0 else None
        rows.append({
            "code": code,
            "market": market,
            "name": mkt.get("name") or name,
            "segment": segment,
            "price": q.get("price"),
            "shares_total": str(mkt["shares_total"]) if mkt.get("shares_total") else None,
            "market_cap": str(cap) if cap else None,
            "net_profit_parent": fin["net_profit_parent"],
            "equity_parent": fin["equity_parent"],
            "period": args.as_of,
            "pe": str(pe) if pe else None,
            "pb": str(pb) if pb else None,
            "verification": why,
        })
        print(f"  {code} {rows[-1]['name']:<8} 价={rows[-1]['price']} "
              f"市值={rows[-1]['market_cap']} PE={rows[-1]['pe']} PB={rows[-1]['pb']}")

    if problems:
        print("\n⛔ 数据源验证未通过，拒绝生成快照：")
        for p in problems:
            print(f"   · {p}")
        return 2

    snapshot = {
        "contract_version": "1.0.0",
        "target": args.target,
        "as_of": args.as_of,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "sources": {
            "price": "新浪财经行情接口 hq.sinajs.cn（含行情时间戳）",
            "shares_and_market_cap": "东方财富 push2 行情接口",
            "financials": ("东方财富财报接口，法定披露口径"
                           "（PARENT_NETPROFIT / TOTAL_PARENT_EQUITY）"),
        },
        "multiple_formula": {
            "pe": "总市值 ÷ 归母净利润（本工具自算，不采信接口的 PE 字段）",
            "pb": "总市值 ÷ 归母所有者权益（同上）",
        },
        # 只陈述**可被本快照验证**的口径政策。此处曾引用「接口 PB 1.22 vs 自算 0.86」
        # 作为"口径不透明"的例证，但快照里 market_cap ÷ equity_parent = 1.1950，
        # 既非 0.86，也与文档 §4.1 结果表自相矛盾；且接口原始 PE/PB 字段从未落盘，
        # "1.22"同样无从复核。断言一个读者无法在产物里核对的数字，正是本项目
        # 最不能犯的错（见 SKIPPED ≠ FAIL 的同一取舍）。
        "why_self_computed": (
            "接口直接给出 PE/PB 字段，但其口径未在任何随附文档中标注 —— "
            "是否含少数股东权益、按合并报表还是母公司报表口径，均无从确认。"
            "本项目改为自算：pe = 总市值 ÷ 归母净利润，pb = 总市值 ÷ 归母权益。"
            "算式随报告一并给出，读者可用同一份快照里的 market_cap 与 equity_parent "
            "自行复核；基线另由 data/r5_eval/truth.py 的人工真值闸门验证。"
            "可解释性优先于省事。"
        ),
        "peer_selection_rule": (
            "同属石油天然气产业链（上游勘探 / 一体化 / 油服工程）；"
            "A 股上市、交易币种一致（倍数无需汇率换算）；"
            "刻意不纳入港股/美股同业 —— 那需要汇率与时点对齐，"
            "会给「倍数从哪来」引入第二个可争议环节。"
        ),
        "verification": (
            "抓取后自动比对 data/r5_eval/truth.py（人工逐页核对年报得到的真值）；"
            "任何一家不一致即拒绝生成快照。"
        ),
        "rows": rows,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\n✅ 快照 → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
