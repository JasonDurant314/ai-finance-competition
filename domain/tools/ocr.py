"""OCR 通道 —— 三层解析引擎的第 2 层。

引擎选型（本机无 GPU，纯 CPU）：
    * **RapidOCR (ONNX Runtime)** —— 首选。轻量、CPU 友好、中文效果好。
    * PaddleOCR —— 备选，依赖较重。
    * 未安装任何引擎时：**明确报错，不降级为静默返回空结果**。
      静默返回空会让下游以为「这一页没有文字」，是最危险的失败模式。

OCR 结果必须经过 :mod:`domain.tools.tables` 的规则校验，
以及 :mod:`agent.validate.quotecheck` 的回读验证，才允许进入事实库。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agent.evidence.models import BBox, SourceLayer
from agent.runtime.errors import HarnessError
from agent.tools.registry import tool


@dataclass
class OcrLine:
    """一行 OCR 结果。box 为四点坐标。"""

    text: str
    confidence: float
    box: list[tuple[float, float]]

    @property
    def bbox(self) -> BBox:
        xs = [p[0] for p in self.box]
        ys = [p[1] for p in self.box]
        return BBox(x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys))


@dataclass
class OcrResult:
    """整页 OCR 结果。"""

    lines: list[OcrLine]
    engine: str
    image_path: str = ""
    preprocess: dict[str, Any] | None = None

    @property
    def text(self) -> str:
        # 按 y 再按 x 排序，保证阅读顺序稳定（可复现性要求）
        ordered = sorted(self.lines, key=lambda ln: (round(ln.bbox.y0, 1), ln.bbox.x0))
        return "\n".join(ln.text for ln in ordered)

    @property
    def mean_confidence(self) -> float:
        if not self.lines:
            return 0.0
        return sum(ln.confidence for ln in self.lines) / len(self.lines)

    @property
    def low_confidence_ratio(self) -> float:
        """低置信行占比 —— 路由与 VLM 触发的重要信号。"""
        if not self.lines:
            return 1.0
        low = sum(1 for ln in self.lines if ln.confidence < 0.60)
        return low / len(self.lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "line_count": len(self.lines),
            "mean_confidence": round(self.mean_confidence, 4),
            "low_confidence_ratio": round(self.low_confidence_ratio, 4),
            "text": self.text,
            "lines": [
                {
                    "text": ln.text,
                    "confidence": round(ln.confidence, 4),
                    "bbox": ln.bbox.model_dump(),
                }
                for ln in self.lines
            ],
        }


def ocr_lines_to_words(
    lines: list[dict[str, Any]], dpi: int,
) -> list[tuple[float, float, float, float, str]]:
    """把 OCR 产物的行级框转成**页面点坐标**的"单词"表。

    用途：扫描件没有文本层，``page.get_text("words")`` 返回空 ——
    于是单元格级裁决的**几何佐证**拿不到独立裁判，只能失败关闭
    （模型读数只标记、不覆盖）。有了这个函数，就能用 OCR 的行框顶上去：
    同一格里"页面上印着什么"，由 OCR 而不是模型来回答。

    ⚠️ **坐标必须换算**：OCR 的框是**像素**（渲染时的 dpi），
    而单元格 bbox 是**页面点**。``pt = px * 72 / dpi``。
    这与 ``cell_pixel_box`` 是互逆的一步 —— 那边从点换算到像素，
    这边从像素换算回点。搞反了就会得到"看起来合理但完全对不上"的几何。

    返回 ``[(x0, y0, x1, y1, text), …]``，与 PyMuPDF 的 words 同构，
    因此可直接喂给 ``cell_geometry_text``。
    """
    if not lines or not dpi:
        return []
    scale = 72.0 / float(dpi)
    out: list[tuple[float, float, float, float, str]] = []
    for ln in lines:
        text = str(ln.get("text") or "").strip()
        bbox = ln.get("bbox") or {}
        if not text or not bbox:
            continue
        try:
            out.append((
                float(bbox["x0"]) * scale,
                float(bbox["y0"]) * scale,
                float(bbox["x1"]) * scale,
                float(bbox["y1"]) * scale,
                text,
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return out


# ==========================================================================
# 引擎探测
# ==========================================================================

_ENGINE_CACHE: dict[str, Any] = {}


def available_engines() -> list[str]:
    engines: list[str] = []
    try:
        import rapidocr_onnxruntime  # noqa: F401

        engines.append("rapidocr")
    except ImportError:
        pass
    try:
        import paddleocr  # noqa: F401

        engines.append("paddleocr")
    except ImportError:
        pass
    return engines


def _get_engine(name: str) -> Any:
    if name in _ENGINE_CACHE:
        return _ENGINE_CACHE[name]
    if name == "rapidocr":
        from rapidocr_onnxruntime import RapidOCR

        engine = RapidOCR()
    elif name == "paddleocr":
        from paddleocr import PaddleOCR

        engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
    else:
        raise HarnessError(f"未知 OCR 引擎：{name}", available=available_engines())
    _ENGINE_CACHE[name] = engine
    return engine


def recognize(
    image_path: str | Path,
    *,
    engine: str | None = None,
    min_confidence: float = 0.0,
) -> OcrResult:
    """对一张图片做 OCR。

    Raises:
        HarnessError: 未安装任何 OCR 引擎。**不静默返回空结果。**
    """
    p = Path(image_path)
    if not p.exists():
        raise HarnessError("待识别图片不存在", path=str(p))

    engines = [engine] if engine else available_engines()
    if not engines:
        raise HarnessError(
            "未安装任何 OCR 引擎，无法处理扫描件。",
            hint="pip install rapidocr-onnxruntime（推荐，纯 CPU）",
            available=[],
        )

    last_err: Exception | None = None
    for name in engines:
        try:
            return _run_engine(name, p, min_confidence=min_confidence)
        except Exception as exc:  # 换下一个引擎
            last_err = exc
            continue

    raise HarnessError(
        f"全部 OCR 引擎均失败：{last_err}", tried=engines
    )


def _run_engine(name: str, path: Path, *, min_confidence: float) -> OcrResult:
    eng = _get_engine(name)

    if name == "rapidocr":
        raw, _elapse = eng(str(path))
        lines: list[OcrLine] = []
        for item in raw or []:
            box, text, score = item[0], item[1], float(item[2])
            if score < min_confidence:
                continue
            lines.append(
                OcrLine(
                    text=str(text),
                    confidence=score,
                    box=[(float(pt[0]), float(pt[1])) for pt in box],
                )
            )
        return OcrResult(lines=lines, engine=name, image_path=str(path))

    # paddleocr
    raw = eng.ocr(str(path), cls=True)
    lines = []
    for page in raw or []:
        for item in page or []:
            box, (text, score) = item[0], item[1]
            if float(score) < min_confidence:
                continue
            lines.append(
                OcrLine(
                    text=str(text),
                    confidence=float(score),
                    box=[(float(pt[0]), float(pt[1])) for pt in box],
                )
            )
    return OcrResult(lines=lines, engine=name, image_path=str(path))


# ==========================================================================
# 工具封装
# ==========================================================================


class RecognizeIn(BaseModel):
    image_path: str
    engine: str | None = None
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


@tool(
    name="ocr.recognize",
    description="对单页图像做 OCR，返回逐行文本、置信度与坐标。未安装引擎时显式报错。",
    params=RecognizeIn,
    side_effect="read",
    cost="cheap",
    determinism="deterministic",
    timeout_s=120.0,
    tags=["ocr", "layer2"],
)
def recognize_tool(
    image_path: str, engine: str | None = None, min_confidence: float = 0.0
) -> dict[str, Any]:
    result = recognize(image_path, engine=engine, min_confidence=min_confidence)
    out = result.to_dict()
    out["source_layer"] = SourceLayer.L2_OCR.value
    return out


class OcrStatusIn(BaseModel):
    pass


@tool(
    name="ocr.status",
    description="探测本机可用的 OCR 引擎。用于环境自检与降级决策。",
    params=OcrStatusIn,
    side_effect="none",
    cost="free",
)
def ocr_status() -> dict[str, Any]:
    engines = available_engines()
    return {
        "available": engines,
        "ready": bool(engines),
        "recommendation": (
            "OK" if engines else "请执行：pip install rapidocr-onnxruntime"
        ),
    }


__all__ = [
    "OcrLine",
    "OcrResult",
    "available_engines",
    "recognize",
    "recognize_tool",
    "ocr_status",
]
