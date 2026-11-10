"""从公开渠道取「待核查文本」——为 R5（研究报告纠错核查）准备素材。

这个工具解决一个具体问题：**R5 需要"给定草稿"，而公开的券商研报 PDF 有反爬**。
与其在反爬上耗时间，不如换一类同样公开、同样含数字主张、而且**可逐条核对**
的文本：

| 素材类型 | 为什么适合 R5 |
|---|---|
| 定期报告摘要（业绩公告） | 官方披露，含"营业收入 X 亿元""归母净利润 Y 亿元"，可与年报正文逐条比对 |
| 权益分派 / 利润分配公告 | 含每股股利、分红总额、占比 —— 天然的**单位与口径**陷阱（每10股 vs 每股） |
| 投资者关系活动记录表 | 公司对经营数据的口头表述，常与财报口径不完全一致 |
| 业绩说明会公告 | 同上 |

**为什么这才是对的材料**：R5 的考察点是「收入、利润、估值倍数、单位、统计口径、
引用」的错误识别与**来源定位**。上述文本每一条数字都能在 `data/real/` 的受控
材料里找到出处，因此"来源定位"是真检查，而不是走过场。

来源留痕与 `tools/fetch_disclosure.py` 一致：URL + sha256 + 抓取时间 → `_SOURCES.json`。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.console import enable_utf8_output  # noqa: E402

DEFAULT_OUT = PROJECT_ROOT / "data" / "drafts" / "_public"
SOURCES_FILE = "_SOURCES.json"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
    "X-Requested-With": "XMLHttpRequest",
}

TARGETS = {
    "002716": {"name": "湖南白银", "column": "szse"},
    "000722": {"name": "湖南发展", "column": "szse"},
    "601857": {"name": "中国石油", "column": "sse"},
}

# 只要这些类型的文本（标题关键词）——它们含可核对的数字主张
DRAFT_KEYWORDS = (
    "业绩公告", "年度报告摘要", "半年度报告摘要", "季度报告",
    "权益分派", "利润分配", "业绩快报", "业绩预告",
    "投资者关系活动", "业绩说明会",
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def _save(path: Path, rows: list[dict]) -> None:
    path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _org_id(client: httpx.Client, code: str) -> str:
    r = client.post(
        "http://www.cninfo.com.cn/new/information/topSearch/query",
        data={"keyWord": code, "maxNum": "10"},
    )
    for row in r.json() or []:
        if row.get("code") == code:
            return str(row.get("orgId") or "")
    return ""


def list_drafts(code: str, begin: str, end: str) -> list[dict]:
    """列出候选文本型公告。"""
    meta = TARGETS[code]
    with httpx.Client(headers=_HEADERS, timeout=30, follow_redirects=True) as c:
        c.get("http://www.cninfo.com.cn/new/index")
        oid = _org_id(c, code)
        if not oid:
            raise RuntimeError(f"未取到 orgId：{code}")
        body = {
            "pageNum": "1", "pageSize": "50", "column": meta["column"],
            "tabName": "fulltext", "plate": meta["column"],
            "stock": f"{code},{oid}", "searchkey": "", "secid": "",
            "category": "", "trade": "", "seDate": f"{begin}~{end}",
            "sortName": "", "sortType": "", "isHLtitle": "true",
        }
        r = c.post("http://www.cninfo.com.cn/new/hisAnnouncement/query", data=body)
        rows = r.json().get("announcements") or []

    out: list[dict] = []
    for a in rows:
        title = re.sub(r"<[^>]+>", "", str(a.get("announcementTitle") or ""))
        if not any(k in title for k in DRAFT_KEYWORDS):
            continue
        if "英文" in title or "H股" in title:
            continue
        out.append(
            {
                "title": title,
                "publish_date": time.strftime(
                    "%Y-%m-%d", time.localtime(int(a["announcementTime"]) / 1000)
                ),
                "url": "http://static.cninfo.com.cn/" + str(a.get("adjunctUrl") or ""),
                "sec_code": code,
                "sec_name": (a.get("secName") or meta["name"]),
                "source": "巨潮资讯网（cninfo.com.cn，证监会指定信息披露网站）",
            }
        )
    return out


def download(entry: dict, out_dir: Path, *, dry_run: bool = False) -> dict | None:
    name = re.sub(r'[<>:"/\\|?*]', "_", f"{entry['sec_code']}_{entry['title']}")[:90]
    target = out_dir / f"{name}.pdf"
    if dry_run:
        print(f"  · {entry['publish_date']}  {entry['title'][:56]}")
        return None
    if target.exists() and target.stat().st_size > 0:
        print(f"  ⏭  已存在：{target.name}")
        return None
    r = httpx.get(
        entry["url"],
        headers={"User-Agent": _UA, "Referer": "http://www.cninfo.com.cn/"},
        timeout=120,
        follow_redirects=True,
    )
    r.raise_for_status()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(r.content)
    if target.read_bytes()[:5] != b"%PDF-":
        target.unlink(missing_ok=True)
        raise RuntimeError(f"不是 PDF：{entry['url']}")
    print(f"  ✅ {target.name}  ({target.stat().st_size // 1024} KB)")
    return {
        **entry,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "sha256": _sha256(target),
        "bytes": target.stat().st_size,
        "local_path": target.name,
    }


def fetch(code: str, begin: str, end: str, out_dir: Path, *, dry_run: bool = False) -> int:
    print(f"\n=== {code} {TARGETS[code]['name']}  {begin} ~ {end} ===")
    rows = list_drafts(code, begin, end)
    print(f"  候选文本 {len(rows)} 份")
    records = []
    for r in sorted(rows, key=lambda x: x["publish_date"]):
        try:
            rec = download(r, out_dir, dry_run=dry_run)
            if rec:
                records.append(rec)
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ {r['title'][:40]} — {type(exc).__name__}: {exc}")
    if records and not dry_run:
        p = out_dir / SOURCES_FILE
        allrows = _load(p)
        seen = {(x.get("url"), x.get("sha256")) for x in allrows}
        allrows.extend(x for x in records if (x["url"], x["sha256"]) not in seen)
        _save(p, allrows)
        print(f"  📝 来源记录：{p}（共 {len(allrows)} 条）")
    return len(records)


def main() -> int:
    enable_utf8_output()
    ap = argparse.ArgumentParser(description="取公开的待核查文本（R5 素材）")
    ap.add_argument("--list", metavar="CODE", help="只列出候选")
    ap.add_argument("--fetch", metavar="CODE")
    ap.add_argument("--fetch-all", action="store_true")
    ap.add_argument("--begin", default="2026-01-01")
    ap.add_argument("--end", default="2026-12-31")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    out_dir = Path(args.out) if args.out else DEFAULT_OUT
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.list:
        fetch(args.list, args.begin, args.end, out_dir, dry_run=True)
        return 0
    if args.fetch:
        return 0 if fetch(args.fetch, args.begin, args.end, out_dir) else 2
    if args.fetch_all:
        n = sum(fetch(c, args.begin, args.end, out_dir) for c in TARGETS)
        print(f"\n共取到 {n} 份文本 → {out_dir}")
        return 0 if n else 2
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
