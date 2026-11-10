"""把 Markdown 计划书渲染成 PDF（零新增依赖，只用 PyMuPDF）。

**为什么自己渲染而不是手工导出**：计划书是**交付物**，交付物也应当能被复现 ——
`deliverables/项目计划书.md` 是唯一真源，PDF 由这条命令生成，
改一个字重新生成即可，不会出现「PDF 与源文件不一致、且没人知道哪份是新版」。

四条硬约束（前三条都是**实测踩过的坑**，所以写进代码）：

1. **绝不静默丢内容。** ``Page.insert_textbox`` 在内容放不下时会**画出能放下的部分
   并返回负值** —— 直接用它会产出「看起来正常、实际缺了几段」的 PDF
   （第一版就是这么写的：换页重试时，上一页残留的半段文字被**重复了一遍**）。
   正确做法是：**先在独立的测量文档里量高度**，再决定换不换页，然后只画一次。
2. **测量必须用独立的 scratch 文档。** 第一版在真实文档里开"探针页"再删掉，
   一旦探针页成为最后一页，``delete_page`` 会误删**正在使用的页**，
   后续绘制全部抛 ``AttributeError: 'NoneType' object has no attribute 'is_pdf'``。
3. **量宽必须与画宽完全一致。** 代码块第一版量的是 455pt、画的是 449pt
   （差在左右内边距上），换行结果不同 → 自检直接抛错。
   宁可抛错也不静默裁掉，这正是自检存在的意义。
4. **中文字体优先用系统字体**（微软雅黑 / 黑体……），找不到才退回 PyMuPDF 内置的
   ``china-s``。内置字体可用，但它把 ASCII 也按全角排，
   "2026" 会渲染成 "2 0 2 6" —— 对交付物来说太难看了。

用法::

    python tools/make_plan_pdf.py                          # 默认渲染计划书
    python tools/make_plan_pdf.py --md x.md --pdf y.pdf
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agent.console import enable_utf8_output  # noqa: E402

enable_utf8_output()

import fitz  # noqa: E402

# ---------------------------------------------------------------- 版面常量

PAGE_W, PAGE_H = 595.0, 842.0          # A4（pt）
MARGIN_X, MARGIN_TOP, MARGIN_BOT = 62.0, 66.0, 62.0
BODY_W = PAGE_W - 2 * MARGIN_X
BODY_H = PAGE_H - MARGIN_TOP - MARGIN_BOT

H_SIZES = {1: 19.0, 2: 14.5, 3: 12.2, 4: 11.2}
BODY_SIZE = 10.4
CODE_SIZE = 8.4
LINE_LEAD = 1.62
GRAY = (0.30, 0.33, 0.38)
BLACK = (0.0, 0.0, 0.0)
RULE_GRAY = (0.78, 0.80, 0.84)
CODE_BG = (0.955, 0.960, 0.968)
HEAD_BG = (0.925, 0.937, 0.955)
BIG = 4000.0                            # 测量用"足够高"的矩形

# ---------------------------------------------------------------- 字体

#: 正文字体候选（按优先级）。``None`` 表示"用 PyMuPDF 内置字体"。
BODY_CANDIDATES: list[tuple[str, str]] = [
    ("msyh.ttc", "微软雅黑"), ("msyh.ttf", "微软雅黑"), ("simsun.ttc", "宋体"),
    ("NotoSansCJK-Regular.ttc", "Noto Sans CJK"), ("NotoSansSC-Regular.otf", "Noto Sans SC"),
    ("PingFang.ttc", "苹方"), ("wqy-zenhei.ttc", "文泉驿正黑"),
]
HEAD_CANDIDATES: list[tuple[str, str]] = [
    ("msyhbd.ttc", "微软雅黑 Bold"), ("simhei.ttf", "黑体"),
    ("NotoSansCJK-Bold.ttc", "Noto Sans CJK Bold"), ("msyh.ttc", "微软雅黑"),
]

_FONT_DIRS = [
    Path("C:/Windows/Fonts"),
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
    Path("/System/Library/Fonts"),
    Path.home() / ".fonts",
    Path.home() / "AppData/Local/Microsoft/Windows/Fonts",
]


def _find_font(candidates: list[tuple[str, str]]) -> tuple[Path | None, str]:
    """在常见字体目录里找第一个可用的候选字体。"""
    for name, label in candidates:
        for base in _FONT_DIRS:
            if not base.is_dir():
                continue
            direct = base / name
            if direct.is_file():
                return direct, label
            try:
                hit = next(base.rglob(name), None)
            except OSError:                     # pragma: no cover - 受限目录
                hit = None
            if hit is not None:
                return hit, label
    return None, "PyMuPDF 内置 CJK 字体"


class Fonts:
    """字体解析结果 + 每次插入时要带的参数。

    ⚠️ 测量与绘制必须用**同一个** ``kw()`` 结果，否则换行会不一致。
    """

    def __init__(self) -> None:
        self.body_file, self.body_label = _find_font(BODY_CANDIDATES)
        self.head_file, self.head_label = _find_font(HEAD_CANDIDATES)
        if self.head_file is None:
            self.head_file, self.head_label = self.body_file, self.body_label

    def kw(self, which: str = "body") -> dict:
        path = self.body_file if which == "body" else self.head_file
        if path is None:
            return {"fontname": "china-s" if which == "body" else "china-ss"}
        return {"fontname": "dshbody" if which == "body" else "dshhead",
                "fontfile": str(path)}

    def describe(self) -> str:
        b = f"{self.body_label}" + (f"（{self.body_file.name}）" if self.body_file else "")
        h = f"{self.head_label}" + (f"（{self.head_file.name}）" if self.head_file else "")
        return f"正文 {b} / 标题 {h}"


# ---------------------------------------------------------------- Markdown 解析


def _strip_inline(text: str) -> str:
    """去掉行内标记，保留可读文字。"""
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = text.replace("\\|", "|")
    text = re.sub(r"^\s*>\s?", "", text)
    return text.rstrip()


def parse_markdown(md: str) -> list[dict]:
    """把 Markdown 解析成块序列（只支持本计划书用到的子集）。"""
    blocks: list[dict] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()

        if not line.strip():
            i += 1
            continue

        if line.lstrip().startswith("```"):
            i += 1
            buf: list[str] = []
            while i < len(lines) and not lines[i].lstrip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            blocks.append({"kind": "code", "lines": buf})
            continue

        if re.fullmatch(r"\s*(-{3,}|\*{3,}|_{3,})\s*", line):
            blocks.append({"kind": "rule"})
            i += 1
            continue

        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            blocks.append({"kind": "head", "level": len(m.group(1)),
                           "text": _strip_inline(m.group(2))})
            i += 1
            continue

        if line.lstrip().startswith("|") and i + 1 < len(lines) and \
                re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not re.match(r"^[\s:|-]+$", "|".join(cells)):
                    rows.append([_strip_inline(c) for c in cells])
                i += 1
            if rows:
                blocks.append({"kind": "table", "rows": rows})
            continue

        if line.lstrip().startswith(">"):
            buf = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                buf.append(_strip_inline(lines[i]))
                i += 1
            blocks.append({"kind": "quote", "text": "\n".join(buf)})
            continue

        m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", raw)
        if m:
            text = m.group(3)
            while i + 1 < len(lines) and lines[i + 1].strip() and \
                    not re.match(r"^(\s*)([-*+]|\d+[.)])\s+", lines[i + 1]) and \
                    not lines[i + 1].lstrip().startswith(("#", "|", ">", "```")):
                i += 1
                text += " " + lines[i].strip()
            blocks.append({"kind": "li", "text": _strip_inline(text),
                           "indent": len(m.group(1)), "marker": m.group(2)})
            i += 1
            continue

        buf = [line]
        while i + 1 < len(lines) and lines[i + 1].strip() and \
                not re.match(r"^\s*(#{1,4}\s|[-*+]\s|\d+[.)]\s|>|\||```|-{3,})",
                             lines[i + 1]):
            i += 1
            buf.append(lines[i])
        blocks.append({"kind": "para", "text": _strip_inline(" ".join(buf))})
        i += 1
    return blocks


# ---------------------------------------------------------------- 排版引擎


class Renderer:
    """按块排版、块内自动换页的极简渲染器。**任何一次绘制都必须成功。**"""

    def __init__(self, doc: fitz.Document, footer: str, fonts: Fonts):
        self.doc = doc
        self.fonts = fonts
        self.scratch = fitz.open()          # 只用于测量，绝不与真实文档混用
        self.footer = footer
        self.page = doc.new_page(width=PAGE_W, height=PAGE_H)
        self.y = MARGIN_TOP
        self.page_no = 1
        self.blocks_drawn = 0

    # -- 插入（测量与绘制共用同一套字体参数） -------------------------------

    def _insert_box(self, page: fitz.Page, rect: fitz.Rect, text: str, *,
                    size: float, which: str, leading: float, color,
                    align: int = 0) -> float:
        return page.insert_textbox(
            rect, text, fontsize=size, lineheight=leading, color=color,
            align=align, **self.fonts.kw(which),
        )

    def _measure(self, text: str, *, size: float, which: str, width: float,
                 leading: float) -> float:
        """在 scratch 文档里量文本高度；放不下（返回负值）则为 ``inf``。"""
        if not text.strip():
            return 0.0
        probe = self.scratch.new_page(width=PAGE_W, height=PAGE_H)
        try:
            left = self._insert_box(probe, fitz.Rect(0, 0, width, BIG), text,
                                    size=size, which=which, leading=leading,
                                    color=BLACK)
        finally:
            self.scratch.delete_page(self.scratch.page_count - 1)
        return float("inf") if left < 0 else BIG - left

    def _split_to_fit(self, text: str, *, size: float, which: str, width: float,
                      leading: float, max_height: float) -> list[str]:
        """把文本切成若干块，每块都能放进 ``max_height``（按行切，不截断）。"""
        chunks: list[str] = []
        cur: list[str] = []
        for ln in text.split("\n"):
            trial = "\n".join(cur + [ln]) if cur else ln
            h = self._measure(trial, size=size, which=which, width=width,
                              leading=leading)
            if h > max_height:
                if not cur:
                    raise RuntimeError(f"单行就放不下（宽度 {width:.0f}pt）：{ln[:60]!r}")
                chunks.append("\n".join(cur))
                cur = [ln]
                if self._measure(ln, size=size, which=which, width=width,
                                 leading=leading) > max_height:
                    raise RuntimeError(f"单行就放不下：{ln[:60]!r}")
            else:
                cur.append(ln)
        if cur:
            chunks.append("\n".join(cur))
        return chunks

    # -- 骨架 -------------------------------------------------------------

    def _room(self) -> float:
        return PAGE_H - MARGIN_BOT - self.y

    def _new_page(self) -> None:
        self._footer()
        self.page = self.doc.new_page(width=PAGE_W, height=PAGE_H)
        self.page_no += 1
        self.y = MARGIN_TOP

    def _footer(self) -> None:
        self.page.insert_text((MARGIN_X, PAGE_H - 34), self.footer,
                              fontsize=7.6, color=GRAY, **self.fonts.kw("body"))
        self.page.insert_text((PAGE_W - MARGIN_X - 46, PAGE_H - 34),
                              f"第 {self.page_no} 页", fontsize=7.6, color=GRAY,
                              **self.fonts.kw("body"))

    # -- 绘制 -------------------------------------------------------------

    def _draw(self, chunk: str, *, size: float, which: str, leading: float,
              color, indent: float, height: float) -> None:
        """把一块文本画在当前页（调用前必须已经确认放得下）。"""
        rect = fitz.Rect(MARGIN_X + indent, self.y, PAGE_W - MARGIN_X,
                         self.y + height + 1.0)
        left = self._insert_box(self.page, rect, chunk, size=size, which=which,
                                leading=leading, color=color)
        if left < 0:                        # pragma: no cover - 防御性
            raise RuntimeError(
                f"排版自检失败：确认放得下却画不下（{height:.1f}pt）：{chunk[:50]!r}"
            )
        self.y = rect.y1

    def put(self, text: str, *, size: float = BODY_SIZE, which: str = "body",
            leading: float = LINE_LEAD, color=BLACK, indent: float = 0.0,
            gap_after: float = 7.0) -> None:
        if not text.strip():
            return
        width = PAGE_W - MARGIN_X - (MARGIN_X + indent)
        for chunk in self._split_to_fit(text, size=size, which=which, width=width,
                                        leading=leading, max_height=BODY_H):
            h = self._measure(chunk, size=size, which=which, width=width,
                              leading=leading)
            if h > self._room():
                self._new_page()
            self._draw(chunk, size=size, which=which, leading=leading, color=color,
                       indent=indent, height=h)
            self.y += gap_after
            self.blocks_drawn += 1

    # -- 块类型 -----------------------------------------------------------

    def title_page(self, title: str, subtitle: str, meta: list[str]) -> None:
        self.y = 200.0
        self.put(title, size=24.0, which="head", leading=1.35, gap_after=10.0,
                 color=(0.09, 0.13, 0.24))
        self.put(subtitle, size=11.6, leading=1.6, gap_after=8.0, color=GRAY)
        self.page.draw_line(fitz.Point(MARGIN_X, self.y + 2),
                            fitz.Point(PAGE_W - MARGIN_X, self.y + 2),
                            color=RULE_GRAY, width=1.0)
        self.y += 20.0
        for m in meta:
            self.put(m, size=9.4, leading=1.5, gap_after=2.0, color=GRAY)
        self._new_page()

    def head(self, level: int, text: str) -> None:
        size = H_SIZES.get(level, BODY_SIZE)
        width = PAGE_W - 2 * MARGIN_X
        h = self._measure(text, size=size, which="head", width=width, leading=1.42)
        if level == 1:
            if self.y > MARGIN_TOP + 4:
                self._new_page()
            self.y += 8.0
            self._draw(text, size=size, which="head", leading=1.42,
                       color=(0.09, 0.13, 0.24), indent=0.0, height=h)
            self.y += 6.0
            self.page.draw_line(fitz.Point(MARGIN_X, self.y),
                                fitz.Point(PAGE_W - MARGIN_X, self.y),
                                color=RULE_GRAY, width=0.9)
            self.y += 12.0
            self.blocks_drawn += 1
            return
        # 标题不与后文分离：留出标题 + 约两行正文的空间
        if self._room() < h + 34.0:
            self._new_page()
        if level == 2:
            self.y += 7.0
        self._draw(text, size=size, which="head", leading=1.42,
                   color=(0.13, 0.18, 0.30), indent=0.0, height=h)
        self.y += 6.0
        self.blocks_drawn += 1

    def bullets(self, items: list[dict]) -> None:
        for it in items:
            indent = 14.0 + (10.0 if it["indent"] >= 2 else 0.0)
            marker = (it["marker"] + " ") if it["marker"][0].isdigit() else "· "
            self.put(marker + it["text"], indent=indent, gap_after=3.0)

    def quote(self, text: str) -> None:
        start_y = self.y
        self.put(text, size=9.7, indent=14.0, gap_after=7.0, color=GRAY)
        if self.y > start_y:
            self.page.draw_line(
                fitz.Point(MARGIN_X + 5, start_y + 2),
                fitz.Point(MARGIN_X + 5, max(start_y + 2, self.y - 9)),
                color=(0.62, 0.66, 0.74), width=2.0,
            )

    def code(self, lines: list[str]) -> None:
        text = "\n".join(lines)
        # ⚠️ 量宽必须与**画宽完全一致**：第一版量 455pt、画 449pt
        # （差在左右内边距上），换行结果不同 → 自检直接抛错。
        box_x0, box_x1 = MARGIN_X + 6, PAGE_W - MARGIN_X
        pad_x0, pad_x1 = box_x0 + 10, box_x1 - 6
        width = pad_x1 - pad_x0
        chunks = self._split_to_fit(text, size=CODE_SIZE, which="body",
                                    width=width, leading=1.45,
                                    max_height=BODY_H - 16)
        for chunk in chunks:
            h = self._measure(chunk, size=CODE_SIZE, which="body", width=width,
                              leading=1.45)
            need = h + 14.0
            if need > self._room():
                self._new_page()
            box = fitz.Rect(box_x0, self.y, box_x1, self.y + need)
            self.page.draw_rect(box, color=None, fill=CODE_BG)
            self.page.draw_line(fitz.Point(box.x0, box.y0),
                                fitz.Point(box.x0, box.y1),
                                color=(0.72, 0.76, 0.82), width=2.0)
            left = self._insert_box(
                self.page, fitz.Rect(pad_x0, box.y0 + 6, pad_x1, box.y1), chunk,
                size=CODE_SIZE, which="body", leading=1.45,
                color=(0.10, 0.12, 0.16),
            )
            if left < 0:                    # pragma: no cover - 防御性
                raise RuntimeError(f"代码块排版自检失败：{chunk[:50]!r}")
            self.y = box.y1 + 9.0
            self.blocks_drawn += 1

    def table(self, rows: list[list[str]]) -> None:
        ncols = max(len(r) for r in rows)
        rows = [r + [""] * (ncols - len(r)) for r in rows]
        if ncols == 1:
            widths = [BODY_W]
        else:
            first = min(BODY_W * 0.34, max(96.0, BODY_W * 0.28))
            rest = (BODY_W - first) / (ncols - 1)
            widths = [first] + [rest] * (ncols - 1)

        size, pad = 8.6, 4.5

        def row_height(cells: list[str]) -> float:
            h = 0.0
            for cell, w in zip(cells, widths):
                h = max(h, self._measure(cell or " ", size=size, which="body",
                                         width=w - 2 * pad, leading=1.42))
            return h + 2 * pad

        header, body = rows[0], rows[1:]
        if self._room() < row_height(header) + (row_height(body[0]) if body else 0.0):
            self._new_page()

        def draw_row(cells: list[str], *, is_head: bool) -> None:
            h = row_height(cells)
            x = MARGIN_X
            for cell, w in zip(cells, widths):
                box = fitz.Rect(x, self.y, x + w, self.y + h)
                if is_head:
                    self.page.draw_rect(box, color=None, fill=HEAD_BG)
                self.page.draw_rect(box, color=RULE_GRAY, width=0.6)
                left = self._insert_box(
                    self.page,
                    fitz.Rect(box.x0 + pad, box.y0 + pad - 1.0, box.x1 - pad, box.y1),
                    cell or " ", size=size,
                    which="head" if is_head else "body", leading=1.42,
                    color=(0.10, 0.13, 0.20) if is_head else BLACK,
                )
                if left < 0:                # pragma: no cover - 防御性
                    raise RuntimeError(f"单元格放不下（列宽 {w:.0f}pt）：{cell[:40]!r}")
                x += w
            self.y += h
            self.blocks_drawn += 1

        draw_row(header, is_head=True)
        for r in body:
            if self._room() < row_height(r):
                self._new_page()
                draw_row(header, is_head=True)      # 跨页重复表头
            draw_row(r, is_head=False)
        self.y += 9.0

    def finish(self) -> None:
        self._footer()
        self.scratch.close()


# ---------------------------------------------------------------- 主流程


def render(md_path: Path, pdf_path: Path, *, title: str, subtitle: str,
           meta: list[str]) -> dict:
    blocks = parse_markdown(md_path.read_text(encoding="utf-8"))
    fonts = Fonts()
    doc = fitz.open()
    r = Renderer(doc, footer=f"金融投研智能体 · {md_path.name}", fonts=fonts)
    r.title_page(title, subtitle, meta)

    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b["kind"] == "head":
            r.head(b["level"], b["text"])
        elif b["kind"] == "para":
            r.put(b["text"])
        elif b["kind"] == "li":
            run = []
            while i < len(blocks) and blocks[i]["kind"] == "li":
                run.append(blocks[i])
                i += 1
            r.bullets(run)
            continue
        elif b["kind"] == "table":
            r.table(b["rows"])
        elif b["kind"] == "code":
            r.code(b["lines"])
        elif b["kind"] == "quote":
            r.quote(b["text"])
        elif b["kind"] == "rule":
            r.y += 5.0
            r.page.draw_line(fitz.Point(MARGIN_X, r.y),
                             fitz.Point(PAGE_W - MARGIN_X, r.y),
                             color=RULE_GRAY, width=0.7)
            r.y += 11.0
        i += 1

    r.finish()
    drawn, total = r.blocks_drawn, len(blocks)
    if drawn < total * 0.8:                 # 块数下限自检（列表/表格会合并计数）
        raise RuntimeError(
            f"排版自检失败：解析出 {total} 个内容块，只画了 {drawn} 次 —— 疑似丢块"
        )
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    # 字体子集化：整份 msyh.ttc 有 19 MB，不子集化 PDF 会胀到 22 MB（实测）。
    # 交付物是要上传到竞赛平台的，体积要克制。
    try:
        doc.subset_fonts()
    except Exception as exc:                # pragma: no cover - 旧版本没有这个 API
        print(f"（字体子集化跳过：{exc}）")
    doc.save(str(pdf_path), deflate=True, garbage=4)
    info = {"pages": doc.page_count, "blocks": total, "drawn": drawn,
            "fonts": fonts.describe(), "pdf": str(pdf_path),
            "bytes": pdf_path.stat().st_size}
    doc.close()
    return info


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把 Markdown 计划书渲染成 PDF")
    ap.add_argument("--md", default="deliverables/项目计划书.md")
    ap.add_argument("--pdf", default="deliverables/项目计划书.pdf")
    ap.add_argument("--title", default="金融投研智能体")
    ap.add_argument("--subtitle", default="可验证 · 可追溯 · 可复现的投研 AI 系统")
    ap.add_argument("--meta", action="append", default=None)
    args = ap.parse_args(argv)

    md_path = Path(args.md)
    if not md_path.is_absolute():
        md_path = REPO / md_path
    pdf_path = Path(args.pdf)
    if not pdf_path.is_absolute():
        pdf_path = REPO / pdf_path
    if not md_path.exists():
        print(f"❌ 找不到 {md_path}")
        return 2

    meta = args.meta or [
        "2026 年北京市大学生金融人工智能竞赛 · 参赛作品",
        "选题 1 数据结构化提取 · 选题 4 自动化估值建模 · 选题 5 研究报告纠错核查",
        f"源文件 {md_path.name} —— 本 PDF 由 tools/make_plan_pdf.py 生成，可重新生成",
    ]
    info = render(md_path, pdf_path, title=args.title, subtitle=args.subtitle, meta=meta)
    print(f"✅ 已生成 {info['pdf']}")
    print(f"   {info['pages']} 页 / {info['blocks']} 个内容块（绘制 {info['drawn']} 次）/ "
          f"{info['bytes'] / 1024:.0f} KB")
    print(f"   字体：{info['fonts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

