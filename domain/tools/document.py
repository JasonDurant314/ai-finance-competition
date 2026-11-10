"""领域工具：文档解析（三层引擎的第 0、1 层）。

与 harness 的分工：本模块只管「怎么把 PDF 变成结构化文本与图像」，
不关心任务语义。所有函数都是**纯确定性**的（无 LLM、无网络）。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agent.evidence.models import BBox, Chunk, DocumentManifest, DocumentType, SourceLayer
from agent.runtime.errors import HarnessError
from agent.tools.registry import tool

try:
    import fitz  # PyMuPDF

    _HAS_FITZ = True
except ImportError:  # pragma: no cover
    fitz = None  # type: ignore[assignment]
    _HAS_FITZ = False


# ==========================================================================
# 页面预扫描：路由判定的输入
# ==========================================================================

# 乱码特征：CID 未映射、私用区字符、替换符
_GARBLED_CHARS = re.compile(r"[\ufffd\u0000-\u0008\u000b-\u001f\ue000-\uf8ff]")
# 页码/页眉页脚噪声（路由判定时应排除）
_PAGE_NUMBER_LIKE = re.compile(r"^\s*[-—–]?\s*\d{1,4}\s*[-—–]?\s*$")


@dataclass
class PageProbe:
    """单页预扫描结果。纯几何与字符统计，不涉及语义。"""

    page: int
    char_count: int = 0
    garbled_ratio: float = 0.0
    text_coverage: float = 0.0
    image_coverage: float = 0.0
    rule_line_density: int = 0
    block_count: int = 0
    page_area: float = 0.0
    width: float = 0.0
    height: float = 0.0
    has_images: bool = False
    min_font_size: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "char_count": self.char_count,
            "garbled_ratio": round(self.garbled_ratio, 4),
            "text_coverage": round(self.text_coverage, 4),
            "image_coverage": round(self.image_coverage, 4),
            "rule_line_density": self.rule_line_density,
            "block_count": self.block_count,
            "has_images": self.has_images,
            "min_font_size": round(self.min_font_size, 2),
            "size": [round(self.width, 1), round(self.height, 1)],
        }


def probe_page(page: "fitz.Page", page_no: int) -> PageProbe:
    """对单页做纯确定性预扫描。

    这些指标直接决定路由分支，因此**必须可解释、可复现**。
    """
    rect = page.rect
    area = max(1.0, rect.width * rect.height)
    probe = PageProbe(
        page=page_no, page_area=area, width=rect.width, height=rect.height
    )

    # —— 文本统计 ——
    data = page.get_text("dict")
    blocks = data.get("blocks", [])
    text_area = 0.0
    chars: list[str] = []
    min_font = 0.0

    for blk in blocks:
        if blk.get("type") != 0:  # 0 = 文本块
            continue
        probe.block_count += 1
        bbox = blk.get("bbox")
        if bbox:
            text_area += max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])
        for line in blk.get("lines", []):
            for span in line.get("spans", []):
                txt = span.get("text", "")
                chars.append(txt)
                size = float(span.get("size", 0.0))
                if size > 0 and (min_font == 0.0 or size < min_font):
                    min_font = size

    raw_text = "".join(chars)
    probe.min_font_size = min_font

    # 排除页码类噪声后再统计有效字符
    effective = [
        ln for ln in raw_text.splitlines() if ln.strip() and not _PAGE_NUMBER_LIKE.match(ln)
    ]
    effective_text = "".join(effective).strip()
    probe.char_count = len(effective_text)

    if raw_text:
        garbled = len(_GARBLED_CHARS.findall(raw_text))
        probe.garbled_ratio = garbled / max(1, len(raw_text))

    probe.text_coverage = min(1.0, text_area / area)

    # —— 图像统计 ——
    image_area = 0.0
    try:
        for img in page.get_images(full=True):
            for r in page.get_image_rects(img[0]):
                image_area += max(0.0, r.width) * max(0.0, r.height)
                probe.has_images = True
    except Exception:  # pragma: no cover - 畸形 PDF
        pass
    probe.image_coverage = min(1.0, image_area / area)

    # —— 线段统计（判有无框线表格）——
    probe.rule_line_density = _count_rule_lines(page)

    return probe


def _count_rule_lines(page: "fitz.Page", min_len: float = 20.0) -> int:
    """统计近似水平/垂直的长线段，用于判断表格框线密度。"""
    n = 0
    try:
        for d in page.get_drawings():
            for item in d.get("items", []):
                if item[0] == "l":  # 直线
                    p1, p2 = item[1], item[2]
                    dx, dy = abs(p2.x - p1.x), abs(p2.y - p1.y)
                    if max(dx, dy) >= min_len and min(dx, dy) <= 2.0:
                        n += 1
                elif item[0] == "re":  # 矩形边框
                    r = item[1]
                    if max(r.width, r.height) >= min_len:
                        n += 1
    except Exception:  # pragma: no cover
        pass
    return n


# ==========================================================================
# 文本层抽取
# ==========================================================================


def extract_chunks(page: "fitz.Page", doc_id: str, page_no: int) -> list[Chunk]:
    """把一页的文本层转成 Chunk 列表，保留 bbox 供 Web 高亮。"""
    out: list[Chunk] = []
    data = page.get_text("dict")
    order = 0

    for blk in data.get("blocks", []):
        if blk.get("type") != 0:
            continue
        bbox = blk.get("bbox") or (0, 0, 0, 0)
        lines: list[str] = []
        for line in blk.get("lines", []):
            spans = [s.get("text", "") for s in line.get("spans", [])]
            joined = "".join(spans).strip()
            if joined:
                lines.append(joined)
        text = "\n".join(lines).strip()
        if not text:
            continue

        out.append(
            Chunk(
                chunk_id=Chunk.make_id(doc_id, page_no, text),
                doc_id=doc_id,
                page=page_no,
                bbox=BBox(x0=bbox[0], y0=bbox[1], x1=bbox[2], y1=bbox[3]),
                text=text,
                source_layer=SourceLayer.L1_TEXT_LAYER,
                reading_order=order,
                block_type="text",
            )
        )
        order += 1

    # 保持 CPU 坐标顺序（自上而下、自左而右）
    out.sort(key=lambda c: (-c.bbox.y1, c.bbox.x0))
    for i, c in enumerate(out):
        c.reading_order = i
    return out


def open_pdf(path: str | Path) -> "fitz.Document":
    if not _HAS_FITZ:
        raise HarnessError("未安装 PyMuPDF，无法解析 PDF", hint="pip install pymupdf")
    p = Path(path)
    if not p.exists():
        raise HarnessError("PDF 文件不存在", path=str(p))
    try:
        return fitz.open(p)
    except Exception as exc:
        raise HarnessError(f"无法打开 PDF：{exc}", path=str(p)) from exc


def render_page_png(
    doc: "fitz.Document",
    page_no: int,
    out_path: str | Path,
    *,
    dpi: int = 200,
) -> Path:
    """把一页渲染成 PNG。页码 1-based。"""
    page = doc.load_page(page_no - 1)
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(target))
    return target


def page_image_bytes(doc: "fitz.Document", page_no: int, *, dpi: int = 200) -> bytes:
    """渲染到内存。供 VLM 直接消费，不落盘。"""
    page = doc.load_page(page_no - 1)
    pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), alpha=False)
    return pix.tobytes("png")


def crop_region_png(
    doc: "fitz.Document",
    page_no: int,
    bbox: BBox,
    out_path: str | Path,
    *,
    dpi: int = 300,
    pad: float = 4.0,
) -> Path:
    """裁剪页面局部区域 —— 用于 VLM 精读某个单元格/字段。

    提高 DPI 是因为小字号数字在低分辨率下 OCR/VLM 都会读错。
    """
    page = doc.load_page(page_no - 1)
    clip = fitz.Rect(
        max(0, bbox.x0 - pad), max(0, bbox.y0 - pad),
        bbox.x1 + pad, bbox.y1 + pad,
    )
    pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), clip=clip, alpha=False)
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(target))
    return target


def compute_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


# ==========================================================================
# 文档级工具
# ==========================================================================


class RegisterDocIn(BaseModel):
    path: str = Field(description="相对于 data/raw 的文档路径或绝对路径")
    doc_type: str = Field(default="unknown", description="文档类型提示")


class ProbePagesIn(BaseModel):
    path: str
    max_pages: int = 500


class PageTextIn(BaseModel):
    path: str
    page: int = Field(ge=1, description="1-based 页码")


class RenderPageIn(BaseModel):
    path: str
    page: int = Field(ge=1, description="1-based 页码")
    out_dir: str = ""
    dpi: int = Field(default=200, ge=72, le=600)


@tool(
    name="doc.register",
    description="登记一份原始文档：计算 SHA256、页数、doc_id。整个可复现性的根。",
    params=RegisterDocIn,
    side_effect="read",
    cost="cheap",
    tags=["ingest", "deterministic"],
)
def register_document(path: str, doc_type: str = "unknown") -> dict[str, Any]:
    p = Path(path)
    sha = compute_sha256(p)
    doc_id = f"doc_{sha[:12]}"

    valid_types = {t.value for t in DocumentType}
    manifest = DocumentManifest(
        doc_id=doc_id,
        filename=p.name,
        rel_path=p.name,
        sha256=sha,
        page_count=0,
        doc_type=DocumentType(doc_type) if doc_type in valid_types else DocumentType.UNKNOWN,
    )
    if _HAS_FITZ and p.suffix.lower() == ".pdf":
        with fitz.open(p) as d:
            manifest.page_count = d.page_count
        manifest.has_text_layer = _has_text_layer(p)

    return {"manifest": manifest.model_dump(mode="json"), "sha256": sha, "doc_id": doc_id}


def _has_text_layer(path: Path, sample_pages: int = 5) -> bool:
    """抽样判断是否存在可用文本层。"""
    try:
        with fitz.open(path) as d:
            n = min(sample_pages, d.page_count)
            for i in range(n):
                txt = d.load_page(i).get_text("text").strip()
                if len(txt) > 100:
                    return True
        return False
    except Exception:  # pragma: no cover
        return False


@tool(
    name="doc.probe_pages",
    description="对 PDF 逐页做确定性预扫描，输出 char_count/garbled_ratio/coverage/线段密度等路由指标。",
    params=ProbePagesIn,
    side_effect="read",
    cost="cheap",
    tags=["route", "deterministic"],
)
def probe_pages(path: str, max_pages: int = 500) -> list[dict[str, Any]]:
    with open_pdf(path) as doc:
        n = min(doc.page_count, max_pages)
        return [probe_page(doc.load_page(i), i + 1).to_dict() for i in range(n)]


@tool(
    name="doc.page_text",
    description="取某一页的文本层全文（用于 quote 回读与规则抽取）。",
    params=PageTextIn,
    side_effect="read",
    cost="cheap",
)
def page_text(path: str, page: int) -> str:
    with open_pdf(path) as doc:
        if page < 1 or page > doc.page_count:
            raise HarnessError("页码越界", page=page, page_count=doc.page_count)
        return doc.load_page(page - 1).get_text("text")


@tool(
    name="doc.render_page",
    description="把某页渲染为 PNG 图片，供 OCR 与多模态裁判使用。",
    params=RenderPageIn,
    side_effect="write",
    cost="cheap",
)
def render_page(path: str, page: int, out_dir: str = "", dpi: int = 200) -> str:
    with open_pdf(path) as doc:
        name = f"{Path(path).stem}_p{page:04d}_{dpi}dpi.png"
        target = Path(out_dir) / name if out_dir else Path(path).with_name(name)
        render_page_png(doc, page, target, dpi=dpi)
        return str(target)


__all__ = [
    "PageProbe",
    "probe_page",
    "extract_chunks",
    "open_pdf",
    "render_page_png",
    "page_image_bytes",
    "crop_region_png",
    "compute_sha256",
    "register_document",
    "probe_pages",
    "page_text",
    "render_page",
]
