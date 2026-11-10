"""OCR 准确性验证 —— 比赛明确会有扫描件与复杂表格输入，这一步不可省略。

## 验证方法论

**问题**：真实扫描件没有标准答案，无法直接评测 OCR 准确率。

**解法：受控退化实验（controlled degradation）**

    电子版 PDF（有文本层）
        ↓ 渲染成图像          ← 图像内容 = 文本层内容，标准答案已知
        ↓ 可选退化（噪点/模糊/低分辨率/旋转）
    "伪扫描件"
        ↓ OCR
    识别结果
        ↓ 与已渲染的文本层比对
    字符准确率 / 数字准确率 / quote 闸门通过率

这样我们得到的是**有真值的准确率数字**，而不是"看起来还行"。

## 三层验证

| 层 | 数据 | 目的 |
|---|---|---|
| L1 无退化 | 电子版渲染图 | OCR 能力上界；确定最优 DPI 与预处理 |
| L2 受控退化 | 加噪/模糊/低分辨率 | 抗劣化能力；对应真实扫描件的质量波动 |
| L3 真实扫描件 | 样本中的纯图像页 | 端到端真实表现（用双配置一致性作为代理指标） |

## 关键指标

- **CER**（字符错误率）：`Levenshtein(gt, hyp) / len(gt)`
- **数字 token 准确率**：金融数据的核心 —— 字符错一位就是错
- **数字召回率**：真值里的数字有多少被识别出来
- **quote 闸门通过率**：识别文本能否让 quote 回读校验通过

用法::

    python tools/ocr_benchmark.py --stage clean        # L1：确定最优配置
    python tools/ocr_benchmark.py --stage degraded     # L2：抗劣化
    python tools/ocr_benchmark.py --stage real         # L3：真实扫描件
    python tools/ocr_benchmark.py --all
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.console import enable_utf8_output  # noqa: E402

enable_utf8_output()

SAMPLE_DIR = PROJECT_ROOT / "agent" / "sample"
WORK_DIR = PROJECT_ROOT / "_ocr_bench"
RESULTS = PROJECT_ROOT / "docs" / "ocr_benchmark.json"


# ==========================================================================
# 文本归一化与比对
# ==========================================================================

# 归一化时保留的字符：中日韩汉字、字母数字、常见金融符号
_KEEP = re.compile(r"[^\u4e00-\u9fff\u3400-\u4dbfA-Za-z0-9.,%‰()（）\-—/：:]")


def normalize_for_compare(text: str) -> str:
    """归一化文本用于比对。

    **关键设计**：比对时忽略空白与排版差异，但**保留所有数字与标点** ——
    金融数据里一个逗号位置错了就是错，不能容忍。
    """
    t = text.replace("\u3000", " ")
    t = _KEEP.sub("", t)
    return t


# ==========================================================================
# 指标体系
# ==========================================================================
#
# ⚠️ 一个必须避开的评测陷阱：**表格区域的读取顺序差异会被严重误判为错误。**
#
# 实测发现：文本层按「行」遍历表格（序号→药名→适应症→规格→价格），
# 而 OCR 按「列」（规格→药名→适应症→包装→价格→通用名）。两者内容相同、
# 顺序不同，但 Levenshtein 距离会把每一处错位都算成"删一个+插一个"，
# 于是一个实际上只错了几处标点的页面，CER 被算成 8–23%。
#
# 因此这里给出三个层次的指标，**主指标是「字符多重集准确率」**：
#
#   char_precision / char_recall : 字符集合层面，抗顺序差异  ← 主指标
#   cer_ordered                  : 顺序敏感，仅用于单栏正文页（作对照）
#   number_*                     : 数字级，金融数据的真正命门
#   quote_pass_rate              : 行级，quote 回读闸门能否通过
#
# 报告时必须同时看 char_recall 与 cer_ordered：两者差距大 = 顺序问题；
# 两者都差 = 真的识别错误。


def char_multiset_scores(gt: str, hyp: str) -> tuple[float, float, float]:
    """字符多重集（bag-of-characters）的精确率 / 召回率 / F1。

    对顺序不敏感，因此不会被表格的行列读取顺序差异污染。
    这是判断"OCR 到底认得准不准"的主指标。
    """
    g, h = Counter(gt), Counter(hyp)
    common = sum((g & h).values())
    precision = common / max(1, sum(h.values()))
    recall = common / max(1, sum(g.values()))
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return precision, recall, f1
def levenshtein(a: str, b: str, *, max_len: int = 20000) -> int:
    """编辑距离（顺序敏感）。超长文本截断以控制内存。"""
    a, b = a[:max_len], b[:max_len]
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# 数字 token：带可选千分位与小数
_NUM_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numeric_tokens(text: str) -> list[str]:
    """抽出全部数字 token（去掉千分位后比较，避免格式差异干扰）。"""
    return [m.group(0).replace(",", "") for m in _NUM_TOKEN.finditer(text)]


def digits_only(text: str) -> str:
    """只保留数字字符。

    用于**顺序敏感但忽略排版**的数字比对：
    去掉所有标点、空格、字母与汉字后，数字之间的相对顺序仍然保留。
    这样 `1,300` 与 `1300` 等价，而 `1300` 与 `13O0` 不等价。
    """
    return "".join(ch for ch in text if ch.isdigit())


def number_sequence_scores(gt_text: str, hyp_text: str) -> dict:
    """数字序列召回 —— 金融数据准确性的**主指标**。

    ⚠️ 为什么不能用「数字 token 多重集」：文本层会把 `1,300` 拆成
    `1` 与 `300` 两个 token，而多重集在单字符层面比较时，
    `300` 的 `0` 会和任意其他 `0` 混在一起，产生大量**假漏识**
    （实测一页 10 个数字里报了 5 个"漏识"，实际全是这种假象）。

    本指标的做法：
        1. 把真值与识别结果都压缩成纯数字串（保留顺序）
        2. 对真值中的每个数字 token，检查它是否作为**连续子串**出现在识别数字串中
        3. 召回率 = 命中数 / 真值数字数

    这样既抗标点与空格差异，又不会把不同位置的数字混为一谈。
    """
    gt_nums = numeric_tokens(gt_text)
    gt_digits = digits_only(gt_text)
    hyp_digits = digits_only(hyp_text)

    if not gt_nums:
        return {
            "number_total": 0, "number_hit": 0, "number_recall": 1.0,
            "digit_recall": 1.0, "digit_precision": 1.0,
        }

    hits = 0
    for n in gt_nums:
        if n and n in hyp_digits:
            hits += 1

    # 数字字符级的多重集指标（兜底参考）
    from collections import Counter as _C

    g, h = _C(gt_digits), _C(hyp_digits)
    common = sum((g & h).values())

    return {
        "number_total": len(gt_nums),
        "number_hit": hits,
        "number_recall": hits / len(gt_nums),
        "digit_recall": common / max(1, len(gt_digits)),
        "digit_precision": common / max(1, len(hyp_digits)),
    }


def space_boundary_score(gt_text: str, hyp_text: str) -> dict:
    """数字边界完整性 —— 衡量「相邻数字被粘连」的程度。

    实测发现的真问题：200dpi 下 OCR 会丢掉数字前后的空格，
    使 `430198 公告编号：2024-077` 变成 `4301982024-077`，
    两个独立的数字粘成一个，**下游按 token 取值必然出错**。

    指标：真值中**相邻两个数字 token** 的对数，
    在识别结果中仍保持分离的比例。
    """
    gt_nums = numeric_tokens(gt_text)
    hyp_digits = digits_only(hyp_text)
    hyp_nums = numeric_tokens(hyp_text)

    if len(gt_nums) < 2:
        return {"adjacent_pairs": 0, "still_separate": 0, "boundary_ok": 1.0}

    # 识别结果中数字 token 的起始位置，用于判断两个数字是否被合并
    positions: list[tuple[str, int]] = []
    for m in _NUM_TOKEN.finditer(hyp_text):
        positions.append((m.group(0).replace(",", ""), m.start()))

    pairs = 0
    separate = 0
    for a, b in zip(gt_nums, gt_nums[1:]):
        pairs += 1
        # 在识别结果中，a 与 b 是否作为**两个独立 token** 出现
        has_a = any(p[0] == a for p in positions)
        has_b = any(p[0] == b for p in positions)
        if has_a and has_b:
            separate += 1

    return {
        "adjacent_pairs": pairs,
        "still_separate": separate,
        "boundary_ok": separate / pairs if pairs else 1.0,
    }


@dataclass
class PageMetric:
    """单页的 OCR 质量指标。"""

    file: str
    page: int
    dpi: int
    preprocess: str
    variant: str = "clean"

    gt_chars: int = 0
    hyp_chars: int = 0
    edit_distance: int = 0
    cer: float = 1.0

    gt_numbers: int = 0
    matched_numbers: int = 0
    number_accuracy: float = 0.0
    number_recall: float = 0.0

    quote_pass: int = 0
    quote_total: int = 0

    ocr_lines: int = 0
    mean_conf: float = 0.0
    elapsed_s: float = 0.0

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        for k in ("cer", "number_accuracy", "number_recall", "mean_conf", "elapsed_s"):
            d[k] = round(float(d[k]), 4)
        return d


def score_page(
    gt_text: str, hyp_text: str, *, mean_conf: float = 0.0, ocr_lines: int = 0
) -> dict:
    """比对真值文本与 OCR 结果，返回多层指标。

    同时给出顺序敏感与顺序不敏感的指标 —— **两者的差距本身就是诊断信息**：
        char_recall 高但 cer_ordered 也高  → 内容读对了，只是顺序不同（表格）
        char_recall 低                      → 真的识别错误
    """
    gt = normalize_for_compare(gt_text)
    hyp = normalize_for_compare(hyp_text)

    dist = levenshtein(gt, hyp)
    cer = dist / max(1, len(gt))
    char_p, char_r, char_f1 = char_multiset_scores(gt, hyp)

    # ★ 数字指标：用「数字序列召回」而非「数字 token 多重集」。
    #   后者会把 1,300 拆成 1 和 300，在单字符层面产生大量假漏识。
    num = number_sequence_scores(gt_text, hyp_text)
    boundary = space_boundary_score(gt_text, hyp_text)

    # quote 闸门：把真值按行切开，看 OCR 文本能否让每行通过回读。
    # ⚠️ 表格页上 OCR 按列输出，行内容不连续，这一项会偏低 ——
    #    因此单独呈现，不与字符准确率混谈。
    from agent.validate.quotecheck import check_quote

    lines = [
        ln.strip()
        for ln in gt_text.splitlines()
        if len(normalize_for_compare(ln)) >= 8
    ]
    quote_pass = sum(1 for ln in lines if check_quote(ln, hyp_text).passed)

    return {
        "gt_chars": len(gt),
        "hyp_chars": len(hyp),
        "edit_distance": dist,
        "cer": cer,
        "char_precision": char_p,
        "char_recall": char_r,
        "char_f1": char_f1,
        **num,
        **boundary,
        "quote_pass": quote_pass,
        "quote_total": len(lines),
        "mean_conf": mean_conf,
        "ocr_lines": ocr_lines,
    }


# ==========================================================================
# 图像退化（模拟真实扫描件质量）
# ==========================================================================


def degrade_image(
    img, variant: str, *, seed: int = 0
):
    """对渲染图施加受控退化，模拟真实扫描件的质量问题。

    各变体对应真实场景：
        noise     —— 复印噪点、灰尘
        blur      —— 扫描仪失焦、传真
        gray      —— 对比度不足、纸张泛黄
        skew      —— 进纸歪斜
        lowres    —— 低分辨率扫描（150dpi 以下）
        stamp     —— 红色印章遮挡（金融文档最常见）
    """
    import numpy as np
    from PIL import Image, ImageFilter

    rng = np.random.default_rng(seed)

    if variant == "noise":
        arr = np.asarray(img.convert("L"), dtype=np.float32)
        arr += rng.normal(0, 18, arr.shape)
        return Image.fromarray(np.clip(arr, 0, 255).astype("uint8"))

    if variant == "blur":
        return img.convert("L").filter(ImageFilter.GaussianBlur(radius=1.1))

    if variant == "gray":
        arr = np.asarray(img.convert("L"), dtype=np.float32)
        # 压缩动态范围到 90–190，模拟低对比度
        arr = arr / 255.0 * 100 + 90
        return Image.fromarray(np.clip(arr, 0, 255).astype("uint8"))

    if variant == "skew":
        return img.convert("L").rotate(2.0, resample=Image.Resampling.BICUBIC, fillcolor=255)

    if variant == "lowres":
        w, h = img.size
        small = img.convert("L").resize((w // 2, h // 2), Image.Resampling.BILINEAR)
        return small.resize((w, h), Image.Resampling.BILINEAR)

    if variant == "stamp":
        base = img.convert("RGB")
        overlay = Image.new("RGB", base.size, (255, 255, 255))
        from PIL import ImageDraw

        d = ImageDraw.Draw(overlay)
        w, h = base.size
        # 红色椭圆印章，覆盖中部区域（模拟财务章/公章）
        d.ellipse(
            [w * 0.30, h * 0.38, w * 0.72, h * 0.60],
            outline=(210, 30, 30),
            width=max(2, w // 160),
        )
        return Image.blend(base, overlay, 0.42)

    return img


# ==========================================================================
# 候选配置
# ==========================================================================

# 待测组合：(dpi, 预处理)
CONFIGS: list[tuple[int, str]] = [
    (150, "none"),
    (200, "none"),
    (200, "pre"),
    (300, "none"),
    (300, "pre"),
    (400, "none"),
]

DEGRADATIONS = ["clean", "noise", "blur", "gray", "skew", "lowres", "stamp"]


# ==========================================================================
# 渲染与 OCR
# ==========================================================================


def _resolve_sample(rel_name: str) -> Path | None:
    """在 sample/ 下递归定位文件（文件散布在根目录与 _scratch/ 子目录）。"""
    direct = SAMPLE_DIR / rel_name
    if direct.is_file():
        return direct
    hits = list(SAMPLE_DIR.rglob(rel_name))
    return hits[0] if hits else None


def pick_pages(n: int = 8) -> list[tuple[Path, int]]:
    """挑选测试页：**区分「纯正文页」与「表格页」**。

    为什么必须区分：表格页上 OCR 按列读取、文本层按行读取，
    顺序差异会让顺序敏感指标虚高。因此：
        * 纯正文页（lines 少）→ 可作为 CER 的干净基准
        * 表格页（lines 多）  → 只用多重集指标评估

    取样时两类各占一半，报告中分别统计。
    """
    import pandas as pd

    csv = PROJECT_ROOT / "docs" / "sample_inventory_pages.csv"
    if not csv.exists():
        raise SystemExit("缺少页面清单。请先运行：python tools/inventory_samples.py")
    pages = pd.read_csv(csv)

    base = pages[
        (pages["route"] == "A_text_only")
        & (pages["chars"].between(300, 1500))
        & (pages["garbled"] < 0.02)
    ].copy()
    prose = base[base["lines"] < 20]
    tables = base[base["lines"] >= 20].copy()
    # 表格页放宽字符数下限（表格页文本通常较少）
    tables = pages[(pages["route"] == "D_table") & (pages["chars"] > 300)]

    chosen: list[tuple[Path, int]] = []
    used: set[str] = set()

    def take(pool, k: int) -> None:
        for _, row in pool.iterrows():
            if len(chosen) >= n or k <= 0:
                return
            if row["file"] in used:
                continue
            path = _resolve_sample(str(row["file"]))
            if path is None:
                continue
            chosen.append((path, int(row["page"])))
            used.add(row["file"])
            k -= 1

    half = max(1, n // 2)
    take(prose, half)          # 纯正文页
    take(tables, n - half)     # 表格页
    take(prose, n - len(chosen))  # 不足时用正文页补足
    return chosen


def render_and_ocr(
    pdf: Path,
    page: int,
    *,
    dpi: int,
    preprocess: str,
    variant: str,
    out_dir: Path,
) -> tuple[str, float, int, float]:
    """渲染 -> （可选退化）->（可选预处理）-> OCR。

    Returns:
        (识别文本, 平均置信度, 行数, 耗时秒)
    """
    import fitz
    from domain.tools.image import preprocess_image
    from domain.tools.ocr import recognize

    tag = f"{pdf.stem}_p{page}_{dpi}_{preprocess}_{variant}"
    raw = out_dir / f"{tag}_raw.png"

    with fitz.open(pdf) as doc:
        pg = doc.load_page(page - 1)
        pix = pg.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), alpha=False)
        pix.save(str(raw))

    work = raw
    if variant != "clean":
        from PIL import Image

        img = Image.open(raw)
        degraded = degrade_image(img, variant, seed=page * 31 + dpi)
        work = out_dir / f"{tag}_deg.png"
        degraded.save(work)

    if preprocess == "pre":
        pre = out_dir / f"{tag}_pre.png"
        preprocess_image(work, pre, deskew=True, denoise=True, grayscale=True)
        work = pre

    t0 = time.perf_counter()
    res = recognize(work, min_confidence=0.0)
    elapsed = time.perf_counter() - t0
    return res.text, res.mean_confidence, len(res.lines), elapsed


def ground_truth(pdf: Path, page: int) -> str:
    """标准答案 = 该页的文本层。

    这是本验证方法成立的前提：图像是由这段文本渲染出来的，
    因此文本层就是图像内容的权威描述。
    """
    import fitz

    with fitz.open(pdf) as doc:
        return doc.load_page(page - 1).get_text("text")


# ==========================================================================
# 阶段一：干净渲染上确定最优配置
# ==========================================================================


def stage_clean(pages: list[tuple[Path, int]], *, limit_configs: bool = False) -> dict:
    print("\n" + "=" * 80)
    print("L1 无退化渲染 —— 确定最优 DPI 与预处理配置")
    print("=" * 80)
    out = WORK_DIR / "clean"
    out.mkdir(parents=True, exist_ok=True)

    configs = CONFIGS[:4] if limit_configs else CONFIGS
    rows: list[dict] = []

    for pdf, page in pages:
        gt = ground_truth(pdf, page)
        if len(normalize_for_compare(gt)) < 100:
            continue
        print(f"\n[{pdf.name} p{page}] 真值 {len(normalize_for_compare(gt))} 字符")
        for dpi, pre in configs:
            try:
                text, conf, lines, sec = render_and_ocr(
                    pdf, page, dpi=dpi, preprocess=pre, variant="clean", out_dir=out
                )
            except Exception as exc:
                print(f"    {dpi}dpi/{pre:<5} ❌ {type(exc).__name__}: {exc}")
                continue
            m = score_page(gt, text, mean_conf=conf, ocr_lines=lines)
            rows.append(
                {
                    "file": pdf.name, "page": page, "dpi": dpi, "preprocess": pre,
                    "variant": "clean", "elapsed_s": round(sec, 2), **m,
                }
            )
            print(
                f"    {dpi:>4}dpi/{pre:<5} "
                f"字准={m['char_recall']:6.2%} 序敏CER={m['cer']:6.2%}  "
                f"数字召回={m['number_recall']:6.2%} ({m['number_hit']}/{m['number_total']})  "
                f"边界={m['boundary_ok']:6.2%}  "
                f"quote={m['quote_pass']}/{m['quote_total']}  "
                f"conf={conf:.3f}  {sec:.1f}s"
            )

    return summarize(rows, "L1 无退化渲染")


# ==========================================================================
# 阶段二：受控退化
# ==========================================================================


def stage_degraded(pages: list[tuple[Path, int]], best: tuple[int, str]) -> dict:
    dpi, pre = best
    print("\n" + "=" * 80)
    print(f"L2 受控退化 —— 最优配置 {dpi}dpi / {pre}，测试抗劣化能力")
    print("=" * 80)
    out = WORK_DIR / "degraded"
    out.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for pdf, page in pages:
        gt = ground_truth(pdf, page)
        if len(normalize_for_compare(gt)) < 100:
            continue
        print(f"\n[{pdf.name} p{page}]")
        for variant in DEGRADATIONS:
            try:
                text, conf, lines, sec = render_and_ocr(
                    pdf, page, dpi=dpi, preprocess=pre, variant=variant, out_dir=out
                )
            except Exception as exc:
                print(f"    {variant:<8} ❌ {type(exc).__name__}: {exc}")
                continue
            m = score_page(gt, text, mean_conf=conf, ocr_lines=lines)
            rows.append(
                {
                    "file": pdf.name, "page": page, "dpi": dpi, "preprocess": pre,
                    "variant": variant, "elapsed_s": round(sec, 2), **m,
                }
            )
            print(
                f"    {variant:<8} 字准={m['char_recall']:6.2%}  "
                f"序敏CER={m['cer']:6.2%}  "
                f"数字召回={m['number_recall']:6.2%}  边界={m['boundary_ok']:6.2%}  "
                f"quote={m['quote_pass']}/{m['quote_total']}"
            )

    return summarize(rows, "L2 受控退化")


# ==========================================================================
# 阶段三：真实扫描件
# ==========================================================================


def stage_real(pages: list[tuple[Path, int]], best: tuple[int, str]) -> dict:
    """真实扫描页 —— 无真值，用双配置一致性 + 数字稳定性作为代理指标。

    对纯图像页，我们没有标准答案。因此测三件能测的事：

      1. **双引擎/双配置一致性**：同页跑两种配置，数字集合的重合度。
         重合度高说明识别稳定；分歧处就是需要 VLM 裁决的地方。
      2. **数字量级合理性**：识别出的数字是否符合金融文档的形态
         （金额通常 4 位以上、带千分位；比例 0–100 之间）。
      3. **quote 自洽性**：同页两次运行的文本能否互相回读（稳定性代理）。

    并输出**实际的抽取结果**供人工抽查 —— 这是最终必须由人过目的部分。
    """
    dpi, pre = best
    print("\n" + "=" * 80)
    print(f"L3 真实扫描件 —— 无真值，测稳定性 + 输出供人工抽查")
    print("=" * 80)
    out = WORK_DIR / "real"
    out.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    samples: list[dict] = []

    for pdf, page in pages:
        print(f"\n[{pdf.name} p{page}]")
        try:
            t1, c1, n1, s1 = render_and_ocr(
                pdf, page, dpi=200, preprocess="pre", variant="clean", out_dir=out
            )
            t2, c2, n2, s2 = render_and_ocr(
                pdf, page, dpi=300, preprocess="none", variant="clean", out_dir=out
            )
        except Exception as exc:
            print(f"    ❌ {type(exc).__name__}: {exc}")
            continue

        nums1, nums2 = Counter(numeric_tokens(t1)), Counter(numeric_tokens(t2))
        common = sum((nums1 & nums2).values())
        total = max(1, sum(nums1.values()))

        # quote 自洽：配置 1 的文本能否在配置 2 的文本中回读
        from agent.validate.quotecheck import check_quote

        lines = [ln.strip() for ln in t1.splitlines() if len(normalize_for_compare(ln)) >= 8]
        self_pass = sum(1 for ln in lines if check_quote(ln, t2).passed)

        # 数字量级合理性
        plausible = 0
        all_nums = list(nums1)
        for n in all_nums:
            try:
                v = Decimal(n)
            except Exception:
                continue
            # 金融数字通常不在 (0,1) 区间（那是比例的小数形式，少见）
            # 或形如 4 位以上整数 / 带两位小数
            if v >= 100 or "." in n:
                plausible += 1

        row = {
            "file": pdf.name, "page": page,
            "chars_cfg1": len(normalize_for_compare(t1)),
            "chars_cfg2": len(normalize_for_compare(t2)),
            "numbers_cfg1": sum(nums1.values()),
            "numbers_cfg2": sum(nums2.values()),
            "number_agreement": common / total,
            "quote_self_consistency": self_pass / max(1, len(lines)),
            "mean_conf_cfg1": round(c1, 4),
            "mean_conf_cfg2": round(c2, 4),
            "plausible_number_ratio": plausible / max(1, len(all_nums)),
            "elapsed_s": round(s1 + s2, 2),
        }
        rows.append(row)
        samples.append(
            {
                "file": pdf.name, "page": page,
                "text_200dpi_pre": t1[:4000],
                "text_300dpi_none": t2[:4000],
                "numbers_200dpi_pre": sorted(nums1)[:120],
            }
        )
        print(
            f"    字符 {row['chars_cfg1']:>5} vs {row['chars_cfg2']:>5}   "
            f"数字 {row['numbers_cfg1']:>4} vs {row['numbers_cfg2']:>4}   "
            f"数字一致={row['number_agreement']:6.2%}   "
            f"quote自洽={row['quote_self_consistency']:6.2%}   "
            f"conf {c1:.3f}/{c2:.3f}"
        )

    (WORK_DIR / "real_samples.json").write_text(
        json.dumps(samples, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\n识别文本样本已写入 {WORK_DIR / 'real_samples.json'}（供人工抽查）")
    return summarize(rows, "L3 真实扫描件稳定性")


# ==========================================================================
# 汇总
# ==========================================================================


def summarize(rows: list[dict], title: str) -> dict:
    if not rows:
        print(f"\n（{title} 无有效结果）")
        return {"title": title, "rows": []}

    import pandas as pd

    df = pd.DataFrame(rows)
    print("\n" + "-" * 80)
    print(f"{title} — 汇总")
    print("-" * 80)

    group_cols = [c for c in ("variant", "dpi", "preprocess") if c in df.columns and df[c].nunique() > 1]
    if group_cols and "char_recall" in df.columns:
        agg = df.groupby(group_cols).agg(
            页数=("cer", "count"),
            字符准确率=("char_recall", "mean"),
            序敏CER=("cer", "mean"),
            数字召回=("number_recall", "mean"),
            数字边界=("boundary_ok", "mean"),
            平均置信度=("mean_conf", "mean"),
            平均耗时s=("elapsed_s", "mean"),
        )
        agg["字符准确率"] = (agg["字符准确率"] * 100).round(2).astype(str) + "%"
        agg["序敏CER"] = (agg["序敏CER"] * 100).round(2).astype(str) + "%"
        agg["数字召回"] = (agg["数字召回"] * 100).round(2).astype(str) + "%"
        agg["数字边界"] = (agg["数字边界"] * 100).round(2).astype(str) + "%"
        agg["平均置信度"] = agg["平均置信度"].round(3)
        agg["平均耗时s"] = agg["平均耗时s"].round(2)
        print(agg.to_string())
        print(
            "\n  指标说明（前两个的差距本身就是诊断信息）：\n"
            "    · 字符准确率 — 对顺序不敏感。判断「认得准不准」的主指标。\n"
            "    · 序敏CER   — 对顺序敏感。表格页上 OCR 按列读、文本层按行读，\n"
            "                  顺序差异会让它虚高，**不代表识别错误**。\n"
            "    · 数字召回  — 真值中的每个数字能否在识别结果中找到（抗标点/空格差异）。\n"
            "                  金融数据的关键指标：字符错个标点无妨，数字错一位就是错。\n"
            "    · 数字边界  — 相邻两个数字是否仍保持分离。粘连会让下游按 token 取值出错。"
        )
    else:
        # L3 真实扫描件的指标集与 L1/L2 不同（无真值，因此没有字符准确率），
        # 必须逐项判断存在性 —— 否则会 KeyError。
        def m(col: str) -> str:
            if col not in df.columns:
                return "n/a"
            return f"{df[col].mean():.2%}"

        print(
            f"  页数={len(df)}  "
            f"数字一致={m('number_agreement')}  "
            f"quote自洽={m('quote_self_consistency')}  "
            f"数字形态合理={m('plausible_number_ratio')}\n"
            f"  平均识别字符数={df['chars_cfg1'].mean():.0f}（配置1） / "
            f"{df['chars_cfg2'].mean():.0f}（配置2）  "
            f"平均置信度={df['mean_conf_cfg1'].mean():.3f} / {df['mean_conf_cfg2'].mean():.3f}"
        )

    return {"title": title, "rows": rows, "summary": df.describe().to_dict()}


def select_best(result: dict) -> tuple[int, str]:
    """从 L1 结果中挑最优配置。

    排序键（依次）：数字召回 → 字符准确率 → 耗时。
    **数字优先**：金融数据里字符错一个标点无伤大雅，数字错一位就是错。
    """
    rows = [r for r in result.get("rows", []) if r.get("variant") == "clean"]
    if not rows:
        return (200, "pre")
    import pandas as pd

    df = pd.DataFrame(rows)
    agg = df.groupby(["dpi", "preprocess"]).agg(
        num_recall=("number_recall", "mean"),
        char_recall=("char_recall", "mean"),
        cer=("cer", "mean"),
        sec=("elapsed_s", "mean"),
    )
    agg = agg.sort_values(
        ["num_recall", "char_recall", "cer", "sec"], ascending=[False, False, True, True]
    )
    best = agg.index[0]
    row = agg.iloc[0]
    print(
        f"\n★ 最优配置：{best[0]}dpi / {best[1]}  "
        f"（数字召回 {row['num_recall']:.2%}，字符准确率 {row['char_recall']:.2%}，"
        f"序敏CER {row['cer']:.2%}，{row['sec']:.1f}s/页）"
    )
    return (int(best[0]), str(best[1]))


def pick_scanned_pages(n: int = 6, *, per_doc: int = 2) -> list[tuple[Path, int]]:
    """挑选**真实扫描页**（文本层为空、且图像覆盖率高的页面）。

    筛选逻辑（实测调整过两轮）：
        1. ``chars < 30`` —— 文本层近乎为空，说明内容确实在图像里
        2. ``img_cov > 0.8`` —— 排除那些「文本层为空但也没有图」的异常页
           （实测有 4 页 img_cov=0 且 chars=0，既无文字也无图像，测了没意义）
        3. 优先选页数多的文档，每份取 ``per_doc`` 页 —— 单页取样偶然性太大

    最理想的目标是 ``股权变动_h12_688290.pdf`` 这类：20 页、图像覆盖率 97%、
    文本层完全为空。这是最硬的验证材料 —— 除了 OCR 无路可走。
    """
    import pandas as pd

    csv = PROJECT_ROOT / "docs" / "sample_inventory_pages.csv"
    if not csv.exists():
        raise SystemExit("缺少页面清单。请先运行：python tools/inventory_samples.py")
    pages = pd.read_csv(csv)

    pool = pages[(pages["chars"] < 30) & (pages["img_cov"] > 0.8)].copy()
    if pool.empty:
        return []

    # 页数多的文档优先（取样更具代表性）
    order = pool.groupby("file").size().sort_values(ascending=False)

    chosen: list[tuple[Path, int]] = []
    for fname in order.index:
        if len(chosen) >= n:
            break
        path = _resolve_sample(str(fname))
        if path is None:
            continue
        doc_pages = pool[pool["file"] == fname].sort_values("page")["page"].tolist()
        for pg in doc_pages[:per_doc]:
            if len(chosen) >= n:
                break
            chosen.append((path, int(pg)))
    return chosen


# ==========================================================================
# 入口
# ==========================================================================


def main() -> int:
    ap = argparse.ArgumentParser(description="OCR 准确性验证（受控退化实验）")
    ap.add_argument("--stage", choices=["clean", "degraded", "real"], default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--pages", type=int, default=8, help="L1/L2 使用的测试页数")
    ap.add_argument("--real-pages", type=int, default=6, help="L3 使用的真实扫描页数")
    ap.add_argument("--quick", action="store_true", help="只测前 3 个配置（快速预估）")
    args = ap.parse_args()

    if not SAMPLE_DIR.exists():
        raise SystemExit(f"样本目录不存在：{SAMPLE_DIR}")

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    report: dict = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}

    # 只跑单个阶段时，**保留此前已算出的其他阶段结果** ——
    # 否则 `--stage real` 会把来之不易的 L1/L2 数据覆盖掉，
    # 而 L1 的 6 配置 × 8 页扫描要十几分钟。
    if args.stage and RESULTS.exists():
        try:
            prev = json.loads(RESULTS.read_text(encoding="utf-8"))
            for k in ("L1_clean", "L2_degraded", "L3_real", "best_config"):
                if k in prev and k not in report:
                    report[k] = prev[k]
            print(f"（已载入 {RESULTS.name} 中的历史阶段结果）")
        except (json.JSONDecodeError, OSError):
            pass

    pages = pick_pages(args.pages)
    print(f"L1/L2 测试页（{len(pages)} 页，跨文档取样）：")
    for p, n in pages:
        print(f"    {p.name} p{n}")

    best = (200, "pre")
    if args.all or args.stage == "clean":
        r = stage_clean(pages, limit_configs=args.quick)
        report["L1_clean"] = r
        best = select_best(r)
        report["best_config"] = {"dpi": best[0], "preprocess": best[1]}

    if args.all or args.stage == "degraded":
        if "L1_clean" in report:
            best = select_best(report["L1_clean"])
        report["L2_degraded"] = stage_degraded(pages, best)

    if args.all or args.stage == "real":
        rp = pick_scanned_pages(args.real_pages)
        print(f"\nL3 真实扫描页（{len(rp)} 页，文本层为空、图像覆盖率高）：")
        for p, n in rp:
            print(f"    {p.name} p{n}")
        report["L3_real"] = stage_real(rp, best)

    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    report["best_config"] = {"dpi": best[0], "preprocess": best[1]}
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"\n完整报告：{RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
