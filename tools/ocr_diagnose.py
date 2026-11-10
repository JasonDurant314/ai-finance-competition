"""OCR 诊断 —— 逐行比对真值与识别结果，定位错误来源。

在做全量基准之前必须先搞清楚：**这些错误是 OCR 的问题，还是我的评测方法有问题？**

常见的评测假象：
    * 文本层顺序与视觉顺序不一致（columns/tables），导致"错位"被算成错误
    * 中文标点/全角字符归一化不当
    * 页眉页脚在图像里但不在文本层（或相反）
    * 竖排/表格线被 OCR 读成了字符
"""

from __future__ import annotations

import sys
from difflib import SequenceMatcher
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

PDF = SAMPLE_DIR / "_scratch" / "_tmp_430017_2023-11-07.pdf"
PAGE = 1


def main() -> int:
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    with fitz.open(PDF) as doc:
        pg = doc.load_page(PAGE - 1)
        gt = pg.get_text("text")
        print("=" * 80)
        print(f"真值（文本层，{len(gt)} 字符）")
        print("=" * 80)
        print(gt)

        for dpi in (150, 200, 300):
            img = WORK_DIR / f"diag_{dpi}.png"
            pg.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False).save(str(img))

    for dpi in (150, 200, 300):
        img = WORK_DIR / f"diag_{dpi}.png"
        res = recognize(img, min_confidence=0.0)
        hyp = res.text
        print("\n" + "=" * 80)
        print(f"OCR 结果 @ {dpi}dpi（{len(hyp)} 字符，{len(res.lines)} 行，置信 {res.mean_confidence:.3f}）")
        print("=" * 80)
        print(hyp)

        # 数字级差异
        gtn, hyn = numeric_tokens(gt), numeric_tokens(hyp)
        missing = [n for n in gtn if n not in hyn]
        extra = [n for n in hyn if n not in gtn]
        print(f"\n数字：真值 {len(gtn)}，识别 {len(hyn)}")
        print(f"  漏识 {len(missing)}: {missing[:25]}")
        print(f"  多识 {len(extra)}: {extra[:25]}")

        # 行级对齐，看错位还是真错
        gt_lines = [ln.strip() for ln in gt.splitlines() if normalize_for_compare(ln)]
        hy_lines = [ln.strip() for ln in hyp.splitlines() if normalize_for_compare(ln)]
        print(f"\n行数：真值 {len(gt_lines)}，识别 {len(hy_lines)}")

        sm = SequenceMatcher(
            None,
            [normalize_for_compare(x) for x in gt_lines],
            [normalize_for_compare(x) for x in hy_lines],
        )
        print("\n行级差异（真值 <-> 识别）：")
        shown = 0
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            shown += 1
            if shown > 14:
                print("    ...（更多差异省略）")
                break
            if tag == "replace":
                for k in range(max(i2 - i1, j2 - j1)):
                    a = gt_lines[i1 + k] if i1 + k < i2 else ""
                    b = hy_lines[j1 + k] if j1 + k < j2 else ""
                    print(f"  ~ GT: {a[:70]!r}")
                    print(f"    HY: {b[:70]!r}")
            elif tag == "delete":
                for k in range(i1, min(i2, i1 + 3)):
                    print(f"  - 漏: {gt_lines[k][:70]!r}")
            elif tag == "insert":
                for k in range(j1, min(j2, j1 + 3)):
                    print(f"  + 多: {hy_lines[k][:70]!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
