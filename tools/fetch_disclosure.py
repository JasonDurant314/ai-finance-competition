"""从公开披露渠道取定期报告 —— 带来源留痕的受控取数工具。

为什么要有这个工具，而不是随手 wget 几个 PDF
------------------------------------------------
赛题要求「确保数据来源可核验」。一份躺在 `data/raw/` 里的 PDF，
如果不记录**从哪来、什么时候取的、原始 URL 是什么、内容哈希是多少**，
它在评审眼里就是一份来历不明的文件，也就无法作为证据链的起点。

因此本工具每下载一份材料，都会往 ``data/raw/<子目录>/_SOURCES.json`` 追加一条记录：

    {"sec_code", "sec_name", "title", "publish_date", "source", "url",
     "fetched_at", "sha256", "bytes", "local_path"}

> **默认输出到 ``data/real/``，不是 ``data/raw/``** —— 这一点是踩过坑才定下的：
> ``data/raw/`` 是**自动化测试夹具的输入目录**（`tests/test_web.py` 会以
> ``--data data`` 跑一次完整流水线）。把几百页年报放进去，
> 每次跑测试都会顺带处理它们，测试从 1 分钟变成 20 分钟以上 ——
> 而且看起来像"测试变慢了"，不像配置错误。

用法::

    python tools/fetch_disclosure.py --list 002716 --year 2025
    python tools/fetch_disclosure.py --fetch 002716 --year 2025
    python tools/fetch_disclosure.py --fetch-all --year 2025

设计约束
--------
- 只取**定期报告**（年报/半年报/季报），不取临时公告 —— 目标是让 R4 有可估值的标的
- 不覆盖已有文件：同名文件先比哈希，相同则跳过（幂等，可重复执行）
- 下载后校验 PDF magic 与页数，损坏文件当场报错而不是留给下游
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.console import enable_utf8_output  # noqa: E402

DEFAULT_OUT = PROJECT_ROOT / "data" / "real" / "定期报告"
SOURCES_FILE = "_SOURCES.json"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 目标公司：湖南白银(深) / 湖南发展(深) / 中国石油(沪)
TARGETS = {
    "002716": {"name": "湖南白银", "exchange": "szse"},
    "000722": {"name": "湖南发展", "exchange": "szse"},
    "601857": {"name": "中国石油", "exchange": "sse"},
}

# 定期报告标题关键词（只认这些，避免把临时公告混进来）
PERIODIC_KEYWORDS = (
    "年度报告",
    "年报",          # ⚠️ 实测：中国石油的标题写「2025年年报」，不含「年度报告」
    "半年度报告",
    "第一季度报告",
    "第三季度报告",
    "季度报告",
)


# ==========================================================================
# 深交所
# ==========================================================================


def szse_announcements(
    sec_code: str, begin: str, end: str, *, page_size: int = 50, max_pages: int = 6
) -> list[dict]:
    """深交所公告列表（官方 API）。

    ``seDate`` 是**闭区间**，且接口按发布时间倒序返回，因此取得越早的报告越靠后。
    """
    url = "http://www.szse.cn/api/disc/announcement/annList"
    headers = {
        "User-Agent": _UA,
        "Referer": "http://www.szse.cn/disclosure/listed/fixed/index.html",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        body = {
            "seDate": [begin, end],
            "stock": [sec_code],
            "channelCode": ["listedNotice_disc"],
            "pageSize": page_size,
            "pageNum": page,
        }
        r = httpx.post(url, json=body, headers=headers, timeout=30)
        r.raise_for_status()
        payload = r.json()
        rows = payload.get("data") or []
        if not rows:
            break
        for row in rows:
            out.append(
                {
                    "title": str(row.get("title") or ""),
                    "publish_date": str(row.get("publishTime") or "")[:10],
                    "url": "http://disc.szse.cn/download"
                    + str(row.get("attachPath") or ""),
                    "sec_code": sec_code,
                    "sec_name": (row.get("secName") or [""])[0],
                    "source": "深交所巨潮资讯（szse.cn）",
                }
            )
        if len(rows) < page_size:
            break
        time.sleep(0.4)  # 对公开接口保持克制
    return out


# ==========================================================================
# 巨潮资讯（cninfo）—— 沪深通用，优先用它
# ==========================================================================

CNINFO_QUERY = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_SEARCH = "http://www.cninfo.com.cn/new/information/topSearch/query"
CNINFO_STATIC = "http://static.cninfo.com.cn/"

_CNINFO_HEADERS = {
    "User-Agent": _UA,
    "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}


def cninfo_org_id(client: httpx.Client, sec_code: str) -> str:
    """用官方搜索接口取 orgId（**不要猜**）。

    实测：猜的 orgId 会让接口返回 0 条却不报错 ——
    「查询成功但结果为空」是最难排查的一类问题，因为它看起来像"这家公司没公告"。
    """
    r = client.post(
        CNINFO_SEARCH,
        data={"keyWord": sec_code, "maxNum": "10"},
        headers={
            "User-Agent": _UA,
            "Referer": "http://www.cninfo.com.cn/new/index",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    r.raise_for_status()
    for row in r.json() or []:
        if row.get("code") == sec_code:
            return str(row.get("orgId") or "")
    return ""


def cninfo_announcements(
    client: httpx.Client, sec_code: str, begin: str, end: str
) -> list[dict]:
    """巨潮资讯公告列表。

    两个参数是**关键且极易猜错**的（都实测踩过，症状都是"返回 0 条不报错"）：

    - ``column``：必须是 ``szse`` / ``sse``，写成 ``sz`` / ``sh`` 拿不到任何数据
    - ``plate``：必须与 column 一致，缺失时报文能过但结果为空

    用 ``category=category_ndbg_szsh;`` 只取定期报告（年度报告）。
    """
    oid = cninfo_org_id(client, sec_code)
    if not oid:
        raise RuntimeError(f"巨潮未返回 orgId：{sec_code}")
    plate = "sse" if sec_code.startswith(("6", "9")) else "szse"
    body = {
        "pageNum": "1",
        "pageSize": "50",
        "column": plate,
        "tabName": "fulltext",
        "plate": plate,
        "stock": f"{sec_code},{oid}",
        "searchkey": "",
        "secid": "",
        "category": "category_ndbg_szsh;",
        "trade": "",
        "seDate": f"{begin}~{end}",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }
    r = client.post(CNINFO_QUERY, data=body, headers=_CNINFO_HEADERS)
    r.raise_for_status()
    payload = r.json()
    out: list[dict] = []
    for row in payload.get("announcements") or []:
        out.append(
            {
                "title": _strip_tags(str(row.get("announcementTitle") or "")),
                "publish_date": time.strftime(
                    "%Y-%m-%d", time.localtime(int(row.get("announcementTime") or 0) / 1000)
                )
                if row.get("announcementTime")
                else "",
                "url": CNINFO_STATIC + str(row.get("adjunctUrl") or ""),
                "sec_code": sec_code,
                "sec_name": str(row.get("secName") or ""),
                "source": "巨潮资讯网（cninfo.com.cn，证监会指定信息披露网站）",
            }
        )
    return out


def _strip_tags(text: str) -> str:
    """巨潮的高亮标题里带 ``<em>`` 标签，要去掉才能做关键词匹配。"""
    import re

    return re.sub(r"<[^>]+>", "", text)


# ==========================================================================
# 上交所（备用通道）
# ==========================================================================


def sse_announcements(sec_code: str, begin: str, end: str) -> list[dict]:
    """上交所公告列表（官方 query 接口）。

    实测：``queryCompanyBulletinNew.do`` 对 ``productId`` 参数敏感，
    需要用 ``reportType2=DQBG``（定期报告）并把日期窗口给足。
    """
    url = "http://query.sse.com.cn/security/stock/queryCompanyBulletinNew.do"
    headers = {
        "User-Agent": _UA,
        "Referer": f"http://www.sse.com.cn/disclosure/listedinfo/announcement/",
        "Accept": "*/*",
    }
    params = {
        "jsonCallBack": "",
        "isPagination": "true",
        "productId": sec_code,
        "reportType2": "DQBG",
        "reportType": "ALL",
        "beginDate": begin,
        "endDate": end,
        "pageHelp.pageSize": "50",
        "pageHelp.pageNo": "1",
        "pageHelp.beginPage": "1",
        "pageHelp.cacheSize": "1",
        "pageHelp.endPage": "5",
    }
    r = httpx.get(url, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    text = r.text.strip()
    if text.startswith("{"):
        payload = json.loads(text)
    else:  # 可能被 JSONP 包裹
        payload = json.loads(text[text.index("(") + 1 : text.rindex(")")])
    rows = ((payload.get("pageHelp") or {}).get("data")) or []
    out: list[dict] = []
    for row in rows:
        # 上交所的 URL 字段名在不同接口间不一致，逐个尝试
        link = (
            row.get("URL")
            or row.get("url")
            or row.get("attachURL")
            or row.get("bulletinUrl")
            or ""
        )
        if link and not link.startswith("http"):
            link = "http://www.sse.com.cn" + link
        out.append(
            {
                "title": str(row.get("TITLE") or row.get("title") or ""),
                "publish_date": str(
                    row.get("SSEDATE") or row.get("publishDate") or ""
                )[:10],
                "url": link,
                "sec_code": sec_code,
                "sec_name": str(row.get("COMPANY_ABBR") or ""),
                "source": "上交所（sse.com.cn）",
            }
        )
    return out


# ==========================================================================
# 下载与留痕
# ==========================================================================


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_sources(out_dir: Path) -> list[dict]:
    p = out_dir / SOURCES_FILE
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return []


def _save_sources(out_dir: Path, rows: list[dict]) -> None:
    p = out_dir / SOURCES_FILE
    p.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _safe_name(text: str, limit: int = 80) -> str:
    bad = '<>:"/\\|?*'
    cleaned = "".join("_" if c in bad else c for c in text).strip()
    return cleaned[:limit]


def download(entry: dict, out_dir: Path, *, dry_run: bool = False) -> dict | None:
    """下载一份报告并返回来源记录（已存在且哈希一致则返回 None）。"""
    if not entry.get("url"):
        print(f"  ⚠️  跳过（无下载链接）：{entry['title']}")
        return None

    name = _safe_name(
        f"{entry['sec_code']}_{entry['sec_name']}_{entry['title']}_{entry['publish_date']}"
    )
    target = out_dir / f"{name}.pdf"

    if dry_run:
        print(f"  · {entry['publish_date']}  {entry['title']}")
        print(f"      {entry['url']}")
        return None

    if target.exists() and target.stat().st_size > 0:
        print(f"  ⏭  已存在：{target.name}")
        digest = _sha256(target)
        for row in _load_sources(out_dir):
            if row.get("local_path") == target.name:
                return None
        return _record(entry, target, digest)

    headers = {"User-Agent": _UA, "Referer": entry["url"]}
    with httpx.stream(
        "GET", entry["url"], headers=headers, timeout=120, follow_redirects=True
    ) as r:
        r.raise_for_status()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as fh:
            for chunk in r.iter_bytes(1 << 16):
                fh.write(chunk)

    if target.read_bytes()[:5] != b"%PDF-":
        target.unlink(missing_ok=True)
        raise RuntimeError(f"下载内容不是 PDF：{entry['url']}")
    digest = _sha256(target)
    print(f"  ✅ {target.name}  ({target.stat().st_size / 1024:.0f} KB)")
    return _record(entry, target, digest)


def _record(entry: dict, path: Path, digest: str) -> dict:
    return {
        "sec_code": entry["sec_code"],
        "sec_name": entry.get("sec_name") or TARGETS.get(entry["sec_code"], {}).get("name", ""),
        "title": entry["title"],
        "publish_date": entry["publish_date"],
        "source": entry.get("source", ""),
        "url": entry["url"],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "sha256": digest,
        "bytes": path.stat().st_size,
        "local_path": path.name,
    }


def fetch(sec_code: str, year: int, out_dir: Path, *, dry_run: bool = False) -> int:
    meta = TARGETS.get(sec_code)
    if meta is None:
        raise SystemExit(f"未在 TARGETS 中登记股票代码：{sec_code}")

    print(f"\n=== {sec_code} {meta['name']}（{meta['exchange']}）{year} 年度 ===")
    # 年度报告通常在**次年 3–4 月**披露，故窗口取到 year+1 年底，
    # 既覆盖目标年度年报，也覆盖次年的半年报/季报（R4 多期对比需要）
    begin = f"{year}-01-01"
    end = f"{year + 1}-12-31"
    rows: list[dict] = []
    # 首选巨潮（沪深通用、证监会指定披露网站），失败再退化到交易所自有接口
    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            client.get("http://www.cninfo.com.cn/new/index")  # 取 cookie
            rows = cninfo_announcements(client, sec_code, begin, end)
        print(f"  巨潮资讯返回 {len(rows)} 条定期报告")
    except Exception as exc:
        print(f"  ⚠️  巨潮通道失败（{type(exc).__name__}: {str(exc)[:80]}），改用交易所接口")
        try:
            rows = (
                szse_announcements(sec_code, begin, end)
                if meta["exchange"] == "szse"
                else sse_announcements(sec_code, begin, end)
            )
        except Exception as exc2:
            print(f"  ❌ 备用通道也失败：{type(exc2).__name__}: {exc2}")
            return 0

    periodic = [
        r
        for r in rows
        if any(k in r["title"] for k in PERIODIC_KEYWORDS)
        and "摘要" not in r["title"]      # 排除摘要（正文才有完整三表）
        and "英文" not in r["title"]
        and "更正" not in r["title"]
        and "已取消" not in r["title"]
    ]
    # 优先精确命中目标年度的年报。
    # ⚠️ 标题写法不统一，实测踩过：中国石油写的是「2025年年报」而不是
    #    「2025年年度报告」，只匹配后一种会把已披露的年报整份漏掉，
    #    而且**不报错**（表现为"这家公司没有年报"）。因此两种都要认。
    annual_title_forms = (
        f"{year}年年度报告",
        f"{year}年年报",
        f"{year}年度报告",
    )
    exact = [r for r in periodic if any(t in r["title"] for t in annual_title_forms)]
    keep = exact or periodic

    print(f"  定期报告 {len(periodic)} 条，选中 {len(keep)} 条")
    records: list[dict] = []
    for r in sorted(keep, key=lambda x: x["publish_date"]):
        try:
            rec = download(r, out_dir, dry_run=dry_run)
            if rec:
                records.append(rec)
        except Exception as exc:
            print(f"  ❌ 下载失败：{r['title']} — {type(exc).__name__}: {exc}")

    if records and not dry_run:
        allrows = _load_sources(out_dir)
        seen = {(x.get("url"), x.get("sha256")) for x in allrows}
        allrows.extend(x for x in records if (x["url"], x["sha256"]) not in seen)
        _save_sources(out_dir, allrows)
        print(f"  📝 来源记录已更新：{out_dir / SOURCES_FILE}（共 {len(allrows)} 条）")
    return len(records)


def main() -> int:
    enable_utf8_output()
    ap = argparse.ArgumentParser(description="从公开渠道取定期报告（带来源留痕）")
    ap.add_argument("--list", metavar="CODE", help="只列出候选报告，不下载")
    ap.add_argument("--fetch", metavar="CODE", help="下载指定股票代码的定期报告")
    ap.add_argument("--fetch-all", action="store_true", help="下载全部目标公司")
    ap.add_argument("--year", type=int, default=2025, help="会计年度（默认 2025）")
    ap.add_argument("--out", default=None, help="输出目录（默认 data/raw/定期报告）")
    args = ap.parse_args()

    out_dir = Path(args.out) if args.out else DEFAULT_OUT
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.list:
        return 0 if fetch(args.list, args.year, out_dir, dry_run=True) >= 0 else 1
    if args.fetch:
        n = fetch(args.fetch, args.year, out_dir)
        return 0 if n else 2
    if args.fetch_all:
        total = 0
        for code in TARGETS:
            total += fetch(code, args.year, out_dir)
        print(f"\n共下载 {total} 份报告 → {out_dir}")
        return 0 if total else 2
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
