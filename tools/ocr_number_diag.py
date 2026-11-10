"""数字层面的深度诊断 —— 为什么数字召回率不达标。

金融数据里字符错一个标点无伤大雅，**数字错一位就是错**。
因此必须逐个搞清楚：漏识的是哪些数字、为什么漏、能不能修。
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.stdout.reconfigure(encoding="utf-8")

import fitz  # noqa: E402

from domain.tools.ocr import recognize  # noqa: E402
from tools.ocr_benchmark import (  # noqa: E402
    SAMPLE_DIR,
    WORK_DIR,
    normalize_for_compare,
    numeric_tokens,
)

CASES = [
    ("_scratch/_tmp_430017_2025-08-08.pdf", 3, (200,)),
    ("_scratch/_tmp_430198_2024-07-09.pdf", 1, (200,)),
    ("_scratch/_tmp_430017_2023-11-07.pdf", 1, (150, 200, 300)),
]


def main() -> int:
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    for rel, page, depis in CASES:
        pdf = SAMPLE_DIR / rel
        if not pdf.is_file():
            print(f"跳过（不存在）：{pdf}")
            continue

        with fitz.open(pdf) as doc:
            gt = doc.load_page(page - 1).get_text("text")

        print("\n" + "=" * 84)
        print(f"{pdf.name} p{page}")
        print("=" * 84)
        print("真值原文：")
        for ln in gt.splitlines():
            if normalise_has_digit(ln):
                print(f"    {ln.strip()!r}")

        gt_nums = Counter(numeric_tokens(gt))
        print(f"\n真值数字 tokens（{sum(gt_nums.values())} 个）：{sorted(gt_nums)}")

        for dpi in depis:
            img = WORK_DIR / f"num_{pdf.stem}_{page}_{dpi}.png"
            with fitz.open(pdf) as doc:
                doc.load_page(page - 1).get_pixmap(
                    matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False
                ).save(str(img))
            res = recognize(img, min_confidence=0.0)
            hy = Counter(numeric_tokens(res.text))
            missing = sorted((gt_nums - hy).elements())
            extra = sorted((hy - gt_nums).elements())
            common = sum((gt_nums & hy).values())

            print(f"\n  @ {dpi}dpi  识别数字 {sum(hy.values())} 个，命中 {common}")
            print(f"    漏识 {len(missing)}: {missing}")
            print(f"    多识 {len(extra)}: {extra}")
            if missing:
                print("    漏识数字在原文中的上下文：")
                for n in missing[:6]:
                    for ln in gt.splitlines():
                        if n in ln.replace(",", ""):
                            print(f"      {n!r} <- {ln.strip()[:78]!r}")
                            break

    return 0


def normalise_has_digit(line: str) -> bool:
    return any(ch.isdigit() for ch in line)


if __name__ == "__main__":
    raise SystemExit(main())
