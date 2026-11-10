"""对 sample/ 目录做全量体检：文档类型 × 页面路由 × 表格密度。

用途：
    1. 量化样本构成（多少页有文本层、多少是扫描件、多少含表格）
    2. 为 OCR 验证挑选候选样本
    3. 为 P0 的图片 token 实测挑选代表性页面

纯确定性，不调用任何模型。
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from agent.config import load_config  # noqa: E402
from domain.tools.document import open_pdf, probe_page  # noqa: E402
from pipelines.r1_extract.common import decide_route  # noqa: E402

SAMPLE = Path(__file__).resolve().parents[1] / "agent" / "sample"


def main() -> int:
    routing = load_config("routing")
    pdfs = sorted(p for p in SAMPLE.rglob("*.pdf"))
    print(f"共 {len(pdfs)} 个 PDF\n")

    doc_rows: list[dict] = []
    page_rows: list[dict] = []

    for i, pdf in enumerate(pdfs, 1):
        try:
            with open_pdf(pdf) as doc:
                routes = Counter()
                table_pages = 0
                for pno in range(doc.page_count):
                    pr = probe_page(doc.load_page(pno), pno + 1)
                    route, _ = decide_route(pr, routing)
                    routes[route.value] += 1
                    if pr.rule_line_density >= routing.get("rule_line_density_threshold", 40):
                        table_pages += 1
                    page_rows.append(
                        {
                            "file": pdf.name,
                            "category": pdf.name.split("_")[0],
                            "page": pno + 1,
                            "chars": pr.char_count,
                            "garbled": round(pr.garbled_ratio, 4),
                            "text_cov": round(pr.text_coverage, 4),
                            "img_cov": round(pr.image_coverage, 4),
                            "lines": pr.rule_line_density,
                            "min_font": round(pr.min_font_size, 1),
                            "route": route.value,
                        }
                    )
                doc_rows.append(
                    {
                        "file": pdf.name,
                        "category": pdf.name.split("_")[0],
                        "pages": doc.page_count,
                        "size_kb": round(pdf.stat().st_size / 1024, 1),
                        "text_pages": routes["A_text_only"],
                        "scanned_pages": routes["B_scanned"] + routes["C_garbled"],
                        "table_pages": table_pages,
                        "mixed_pages": routes["E_mixed"],
                        "table_route": routes["D_table"],
                    }
                )
        except Exception as exc:
            doc_rows.append({"file": pdf.name, "category": pdf.name.split("_")[0],
                             "pages": -1, "error": f"{type(exc).__name__}: {exc}"})
        if i % 40 == 0:
            print(f"  ...已扫描 {i}/{len(pdfs)}")

    import pandas as pd

    docs = pd.DataFrame(doc_rows)
    pages = pd.DataFrame(page_rows)
    out = Path(__file__).resolve().parents[1] / "docs"
    out.mkdir(exist_ok=True)
    docs.to_csv(out / "sample_inventory_docs.csv", index=False, encoding="utf-8-sig")
    pages.to_csv(out / "sample_inventory_pages.csv", index=False, encoding="utf-8-sig")

    print("\n" + "=" * 78)
    print("一、按类别汇总（页面级路由）")
    print("=" * 78)
    if not pages.empty:
        agg = pages.groupby("category").agg(
            文档数=("file", "nunique"),
            总页数=("page", "count"),
            文本页=("route", lambda s: (s == "A_text_only").sum()),
            扫描页=("route", lambda s: s.isin(["B_scanned", "C_garbled"]).sum()),
            表格页=("route", lambda s: (s == "D_table").sum()),
            混排页=("route", lambda s: (s == "E_mixed").sum()),
        )
        agg["扫描页占比"] = (agg["扫描页"] / agg["总页数"] * 100).round(1).astype(str) + "%"
        print(agg.to_string())

    print("\n" + "=" * 78)
    print("二、全部页面路由分布")
    print("=" * 78)
    print(pages["route"].value_counts().to_string())
    print(f"\n总页数：{len(pages)}")

    print("\n" + "=" * 78)
    print("三、扫描/乱码页清单（OCR 验证的目标）")
    print("=" * 78)
    scanned = pages[pages["route"].isin(["B_scanned", "C_garbled"])]
    print(f"共 {len(scanned)} 页，来自 {scanned['file'].nunique()} 个文档")
    if not scanned.empty:
        print(scanned.groupby("file").size().sort_values(ascending=False).head(25).to_string())

    print("\n" + "=" * 78)
    print("四、表格密度最高的页面（复杂表格验证的目标）")
    print("=" * 78)
    dense = pages.nlargest(25, "lines")[["file", "page", "chars", "lines", "img_cov", "route"]]
    print(dense.to_string(index=False))

    print("\n" + "=" * 78)
    print("五、字符数为 0 或极低的页面（纯图像页）")
    print("=" * 78)
    blank = pages[pages["chars"] < 20]
    print(f"共 {len(blank)} 页")
    if not blank.empty:
        print(blank.groupby("file").size().sort_values(ascending=False).head(20).to_string())

    print(f"\n明细已写入：{out / 'sample_inventory_docs.csv'}")
    print(f"              {out / 'sample_inventory_pages.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
