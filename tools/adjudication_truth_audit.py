"""独立复核：单元格级裁决到底改对了没有？

裁决结论本身不足以自证。本脚本用**几何**做独立裁判：
把 PDF 里落在该单元格 bbox 内的文字取出来，看
「程序读数」与「模型读数」哪一个真的出现在那个格子里。

判据（按数字 token 命中率）：

* 模型命中、程序不命中  → 裁决**改对了**
* 程序命中、模型不命中  → 裁决**改错了**（模型读到别处去了）
* 两者都命中            → 无法区分（格式归一属于此类，天然安全）
* 两者都不命中          → 无法判定

这个脚本是踩坑之后写的：某次真实运行里 184 个复核格有 80 个被"改值"，
抽查裁图发现**模型读的是对的、裁图是错的** —— 裁图文件名不含文档 id，
多个文档的同页同格互相覆盖，于是模型看的是另一份公告的表格。
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.console import enable_utf8_output  # noqa: E402

# Windows 控制台默认 GBK：本脚本会打印 ✓ / ✅ 这类符号，
# 不先切 UTF-8 就会在**最需要它的时候**抛 UnicodeEncodeError 中断。
enable_utf8_output()

import fitz  # noqa: E402

from domain.tools.vlm import decide_cell_verdict  # noqa: E402

_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_INVALID = {"", "-", "—"}


def _digits(text: str) -> list[str]:
    """抽数字 token，去掉千分位与小数点 —— 只比"数字串"。"""
    out: list[str] = []
    for m in _NUM.findall(text or ""):
        d = re.sub(r"[^\d]", "", m)
        if d and d not in _INVALID:
            out.append(d)
    return out


def _hit_rate(needles: list[str], haystack_digits: str) -> float | None:
    if not needles:
        return None
    hit = sum(1 for n in needles if n in haystack_digits)
    return hit / len(needles)


def main() -> int:
    run = sys.argv[1] if len(sys.argv) > 1 else "table-vlm4"
    root = Path("runs") / run
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    data_dir = Path(manifest.get("data_dir") or "data")
    if not data_dir.is_absolute():
        data_dir = Path(__file__).resolve().parents[1] / data_dir

    docs = {
        d["doc_id"]: d
        for d in json.loads((root / "facts" / "documents.json").read_text(encoding="utf-8"))
    }
    tables = json.loads((root / "channels" / "tables.json").read_text(encoding="utf-8"))
    adjs = json.loads(
        (root / "channels" / "table_adjudications.json").read_text(encoding="utf-8")
    )

    # (doc_id, page, table_index, row, col) → bbox
    bbox_of: dict[tuple, dict] = {}
    for t in tables:
        for c in t.get("cells") or []:
            bbox_of[(t["doc_id"], int(t["page"]), int(t.get("table_index", 0)),
                     c["row"], c["col"])] = c["bbox"]

    cache: dict[tuple[str, int], tuple[str, fitz.Page]] = {}
    verdicts: Counter[str] = Counter()
    rows: list[dict] = []

    for rec in adjs:
        doc_id, page = rec["doc_id"], int(rec["page"])
        doc = docs.get(doc_id) or {}
        rel = doc.get("rel_path") or doc.get("filename") or ""
        pdf = data_dir / rel
        if not pdf.is_file():
            pdf = next(iter(data_dir.rglob(Path(rel).name)), None)
        if pdf is None or not Path(pdf).is_file():
            verdicts["找不到材料"] += 1
            continue

        key = (doc_id, page)
        if key not in cache:
            d = fitz.open(pdf)
            cache[key] = (str(pdf), d.load_page(page - 1))
        _, page_obj = cache[key]
        words = page_obj.get_text("words")

        for a in (rec.get("report") or {}).get("adjudications", []):
            if a.get("verdict") not in ("corrected", "normalized"):
                continue
            b = bbox_of.get((doc_id, page, int(rec.get("table_index", 0)),
                             a["row"], a["col"]))
            if not b:
                verdicts["缺 bbox"] += 1
                continue
            inside = " ".join(
                w[4] for w in words
                if w[0] >= b["x0"] - 1 and w[2] <= b["x1"] + 1
                and w[1] >= b["y0"] - 1 and w[3] <= b["y1"] + 1
            )
            geom = "".join(_digits(inside))
            orig_rate = _hit_rate(_digits(a.get("original", "")), geom)
            vlm_rate = _hit_rate(_digits(a.get("vlm_text", "")), geom)

            if orig_rate is None or vlm_rate is None:
                verdict = "无数字可比"
            elif vlm_rate > orig_rate:
                verdict = "改对了"
            elif orig_rate > vlm_rate:
                verdict = "改错了"
            elif orig_rate == vlm_rate == 1.0:
                verdict = "无法区分（都在格内）"
            else:
                verdict = "无法判定"
            verdicts[verdict] += 1
            if verdict in ("改对了", "改错了"):
                rows.append({
                    "page": page, "doc": pdf.name, "row": a["row"], "col": a["col"],
                    "kind": a.get("verdict"), "verdict": verdict,
                    "orig_rate": round(orig_rate, 2), "vlm_rate": round(vlm_rate, 2),
                    "original": (a.get("original") or "")[:60],
                    "vlm_text": (a.get("vlm_text") or "")[:60],
                    "geom": inside[:60],
                })

    total = sum(verdicts.values())
    print(f"run={run}  被改写的单元格 {total} 个")
    print("=" * 70)
    for k, v in verdicts.most_common():
        print(f"  {k:<22}{v:>5}")

    # ---- 用新闸门重放判决（离线，不花一次模型调用）----
    print("\n" + "=" * 70)
    print("若加上「几何佐证」闸门，这批裁决会怎么判")
    print("=" * 70)
    replayed: Counter[str] = Counter()
    for rec in adjs:
        doc_id, page = rec["doc_id"], int(rec["page"])
        doc = docs.get(doc_id) or {}
        rel = doc.get("rel_path") or doc.get("filename") or ""
        pdf = data_dir / rel
        if not pdf.is_file():
            pdf = next(iter(data_dir.rglob(Path(rel).name)), None)
        if pdf is None or not Path(pdf).is_file():
            continue
        key = (doc_id, page)
        if key not in cache:
            d = fitz.open(pdf)
            cache[key] = (str(pdf), d.load_page(page - 1))
        _, page_obj = cache[key]
        words = page_obj.get_text("words")
        for a in (rec.get("report") or {}).get("adjudications", []):
            b = bbox_of.get((doc_id, page, int(rec.get("table_index", 0)),
                             a["row"], a["col"]))
            if not b:
                continue
            inside = " ".join(
                w[4] for w in words
                if w[0] >= b["x0"] - 1 and w[2] <= b["x1"] + 1
                and w[1] >= b["y0"] - 1 and w[3] <= b["y1"] + 1
            )
            # 存储的 verdict 已经反映了"自述闸门是否通过"
            gates_ok = a.get("verdict") in ("corrected", "normalized")
            new = decide_cell_verdict(
                a.get("original", ""), a.get("vlm_text", "") or "",
                gates_ok=gates_ok, geometry=inside,
            )
            replayed[f"{a.get('verdict')} → {new}"] += 1
    for k, v in replayed.most_common():
        print(f"  {k:<34}{v:>5}")

    if rows:
        print("\n需人工介入的条目：")
        for r in rows[:20]:
            print(
                f"  p{r['page']:<4} r{r['row']}c{r['col']:<3} {r['verdict']} "
                f"({r['kind']})  程序命中={r['orig_rate']} 模型命中={r['vlm_rate']}"
            )
            print(f"      程序读数 : {r['original']!r}")
            print(f"      模型读数 : {r['vlm_text']!r}")
            print(f"      格内实际 : {r['geom']!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
