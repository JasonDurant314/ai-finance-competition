"""裁图坐标诊断 —— 当"模型读到的不是这一格"时用它。

它把三方证据摆在一起，**判断问题出在哪一层**：

1. 文本层说这个数在哪（``page.search_for``，权威坐标）
2. 单元格 bbox 在哪（来自表格重建）
3. 整页图像素上到底印着什么（两种取图方式并排）

于是可以区分三种完全不同的故障：

* **bbox 错**：``search_for`` 的位置与单元格 bbox 对不上 → 表格重建的问题
* **像素换算错**：bbox 对，但从整页图裁出来的内容与 `clip` 渲染不一致
  → 坐标换算的问题（本项目踩过一次：y 轴方向搞反，裁图整体上下镜像）
* **页面图错**：存档的整页图与现渲染不一致 → 渲染/缓存的问题

用法::

    python tools/crop_coord_diag.py --run table-vlm4 --page 2 --needle 501,000.00
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.console import enable_utf8_output  # noqa: E402

# Windows 控制台默认 GBK：本脚本会打印 ✓ / ✅ 这类符号，
# 不先切 UTF-8 就会在**最需要它的时候**抛 UnicodeEncodeError 中断。
enable_utf8_output()

import fitz  # noqa: E402
from PIL import Image, ImageChops  # noqa: E402


def _data_dir(root: Path) -> Path:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    d = Path(manifest.get("data_dir") or "data")
    return d if d.is_absolute() else Path(__file__).resolve().parents[1] / d


def _resolve_pdf(data_dir: Path, rel: str) -> Path | None:
    p = data_dir / rel
    if p.is_file():
        return p
    hit = next(iter(data_dir.rglob(Path(rel).name)), None)
    return Path(hit) if hit else None


def main() -> int:
    ap = argparse.ArgumentParser(description="裁图坐标诊断")
    ap.add_argument("--run", required=True)
    ap.add_argument("--page", type=int, required=True)
    ap.add_argument("--needle", default="", help="要定位的数字（默认取该页第一个被裁决的值）")
    ap.add_argument("--out", default="_diag_crop")
    args = ap.parse_args()

    root = Path("runs") / args.run
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    data_dir = _data_dir(root)

    docs = {
        d["doc_id"]: d
        for d in json.loads((root / "facts" / "documents.json").read_text(encoding="utf-8"))
    }
    tables = json.loads((root / "channels" / "tables.json").read_text(encoding="utf-8"))
    adjs = json.loads(
        (root / "channels" / "table_adjudications.json").read_text(encoding="utf-8")
    )

    recs = [r for r in adjs if int(r["page"]) == args.page]
    if not recs:
        print(f"第 {args.page} 页没有裁决记录")
        return 1

    for rec in recs:
        doc_id = rec["doc_id"]
        rel = docs[doc_id].get("rel_path") or docs[doc_id]["filename"]
        pdf = _resolve_pdf(data_dir, rel)
        if pdf is None:
            print(f"找不到材料：{rel}")
            continue
        tbl = next(
            (t for t in tables
             if t["doc_id"] == doc_id and int(t["page"]) == args.page
             and int(t.get("table_index", 0)) == int(rec.get("table_index", 0))),
            None,
        )
        print(f"\n=== {pdf.name}  第 {args.page} 页  表{rec.get('table_index', 0)} ===")

        with fitz.open(pdf) as doc:
            pg = doc.load_page(args.page - 1)
            print(f"  page.rect={pg.rect}  rotation={pg.rotation}")
            print(f"  cropbox={pg.cropbox}  （与 mediabox 不同时会影响坐标）")
            print(f"  ⚠ PyMuPDF 坐标原点在**左上**、y 向下 —— 换算像素时不要翻转")

            stored = root / "pages" / f"{doc_id}_p{args.page:04d}_300dpi_cells.png"
            if stored.is_file():
                fresh = out / f"fresh_p{args.page}.png"
                pg.get_pixmap(matrix=fitz.Matrix(300 / 72, 300 / 72),
                              alpha=False).save(str(fresh))
                a = Image.open(fresh).convert("RGB")
                b = Image.open(stored).convert("RGB")
                same = a.size == b.size and ImageChops.difference(a, b).getbbox() is None
                print(f"  整页图：存档 {b.size} vs 现渲染 {a.size} → "
                      f"{'完全一致' if same else '**不一致（页面图本身有问题）**'}")

            # 每个被裁决的格子：文本层权威位置 vs 单元格 bbox
            for a in (rec.get("report") or {}).get("adjudications", []):
                cell = next(
                    (c for c in (tbl or {}).get("cells", [])
                     if c["row"] == a["row"] and c["col"] == a["col"]),
                    None,
                )
                print(f"\n  -- r{a['row']}c{a['col']}  {a['verdict']}")
                print(f"     程序读数 {a['original']!r}   模型读数 {a['vlm_text']!r}")
                if not cell:
                    continue
                b = cell["bbox"]
                print(f"     单元格 bbox  y {b['y0']:.1f}~{b['y1']:.1f}  "
                      f"x {b['x0']:.1f}~{b['x1']:.1f}")
                for label, needle in (("程序读数", a["original"]),
                                      ("模型读数", a.get("vlm_text") or "")):
                    digits = "".join(ch for ch in needle if ch.isdigit())
                    if len(digits) < 4:
                        continue
                    hits = pg.search_for(needle.strip().splitlines()[0][:40])
                    if hits:
                        q = hits[0]
                        inside = (abs(q.y0 - b["y0"]) < 3 and abs(q.x0 - b["x0"]) < 3)
                        print(f"     {label}在页面上的位置 y {q.y0:.1f}~{q.y1:.1f}"
                              f"  {'← 与 bbox 吻合' if inside else '← 与 bbox 不吻合'}")

                if args.needle and args.needle not in (a["original"] + (a.get("vlm_text") or "")):
                    continue
                # 两种取图方式并排，供人眼判定
                clip = fitz.Rect(b["x0"], b["y0"], b["x1"], b["y1"])
                pm = pg.get_pixmap(clip=clip, dpi=300)
                pm.save(str(out / f"A_clip_r{a['row']}c{a['col']}.png"))
                if stored.is_file():
                    img = Image.open(stored).convert("RGB")
                    scale = 300 / 72
                    box = (max(0, int(b["x0"] * scale) - 3), max(0, int(b["y0"] * scale) - 3),
                           min(img.width, int(b["x1"] * scale) + 3),
                           min(img.height, int(b["y1"] * scale) + 3))
                    img.crop(box).save(out / f"B_pagecrop_r{a['row']}c{a['col']}.png")
                print(f"     → A_clip / B_pagecrop 已写入 {out}（两者应显示同一个数）")

    print(f"\n全部输出在 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
