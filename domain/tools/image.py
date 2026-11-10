"""图像预处理 —— 提升扫描件 OCR 精度的确定性步骤。

金融扫描件常见问题：歪斜（拍照/复印）、装订线阴影、印章遮挡、
纸张泛黄导致对比度不足。每一步都做**条件触发**，不是无脑全套处理
（过度二值化反而会损失小字号数字的细节）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from agent.runtime.errors import HarnessError
from agent.tools.registry import tool

try:
    from PIL import Image, ImageOps

    _HAS_PIL = True
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]
    _HAS_PIL = False


@dataclass
class PreprocessReport:
    """预处理报告 —— 记录**做了什么、为什么做**，用于可追溯。"""

    applied: list[str]
    skipped: list[str]
    skew_degrees: float = 0.0
    original_size: tuple[int, int] = (0, 0)
    final_size: tuple[int, int] = (0, 0)

    def to_dict(self) -> dict:
        return {
            "applied": self.applied,
            "skipped": self.skipped,
            "skew_degrees": round(self.skew_degrees, 2),
            "original_size": list(self.original_size),
            "final_size": list(self.final_size),
        }


def estimate_skew(image: "Image.Image", *, max_degrees: float = 8.0) -> float:
    """估计页面歪斜角度。

    用「投影方差最大化」法：对候选角度做小角度旋转，计算水平投影
    的方差，方差最大时文本行最对齐。无需 OpenCV，纯 PIL + numpy。
    """
    import numpy as np

    gray = image.convert("L")
    # 降采样加速
    w, h = gray.size
    scale = min(1.0, 1000.0 / max(w, h))
    if scale < 1.0:
        gray = gray.resize((int(w * scale), int(h * scale)), Image.Resampling.BILINEAR)

    arr = np.asarray(gray, dtype=np.float32)
    # 二值化：暗像素为前景（文字）
    thr = arr.mean() - arr.std() * 0.5
    binary = (arr < thr).astype(np.float32)

    best_angle, best_score = 0.0, -1.0
    for angle in np.arange(-max_degrees, max_degrees + 0.25, 0.5):
        rotated = Image.fromarray((binary * 255).astype("uint8")).rotate(
            angle, resample=Image.Resampling.BILINEAR, fillcolor=0
        )
        proj = np.asarray(rotated, dtype=np.float32).sum(axis=1)
        var = float(proj.var())
        if var > best_score:
            best_score, best_angle = var, float(angle)
    return best_angle


def preprocess_image(
    src: str | Path,
    dst: str | Path,
    *,
    grayscale: bool = True,
    deskew: bool = True,
    denoise: bool = True,
    adaptive_threshold: bool = False,
    max_skew_degrees: float = 8.0,
    upscale_if_small: bool = True,
    min_width: int = 1400,
) -> PreprocessReport:
    """执行预处理链。

    顺序很重要：灰度 -> 纠偏 -> 去噪 -> （可选）二值化 -> 分辨率归一。
    二值化放最后且默认关闭，因为 OCR 引擎自带更好的二值化。
    """
    if not _HAS_PIL:
        raise HarnessError("未安装 Pillow", hint="pip install pillow")

    src_p, dst_p = Path(src), Path(dst)
    if not src_p.exists():
        raise HarnessError("源图片不存在", path=str(src_p))

    report = PreprocessReport(applied=[], skipped=[])
    img = Image.open(src_p)
    report.original_size = img.size

    # 1) 灰度
    if grayscale and img.mode != "L":
        img = img.convert("L")
        report.applied.append("grayscale")
    elif grayscale:
        report.skipped.append("grayscale(已是灰度)")

    # 2) 分辨率归一（小图先放大，OCR 对小字号极敏感）
    if upscale_if_small and img.width < min_width:
        ratio = min_width / img.width
        img = img.resize(
            (int(img.width * ratio), int(img.height * ratio)),
            Image.Resampling.LANCZOS,
        )
        report.applied.append(f"upscale x{ratio:.2f}")
    else:
        report.skipped.append("upscale")

    # 3) 纠偏
    if deskew:
        angle = estimate_skew(img, max_degrees=max_skew_degrees)
        report.skew_degrees = angle
        if abs(angle) >= 0.5:
            img = img.rotate(
                angle, resample=Image.Resampling.BICUBIC, fillcolor=255, expand=False
            )
            report.applied.append(f"deskew {angle:+.1f}deg")
        else:
            report.skipped.append("deskew(歪斜<0.5度)")
    else:
        report.skipped.append("deskew")

    # 4) 去噪（中值滤波，对椒盐噪声与复印噪点有效）
    if denoise:
        from PIL import ImageFilter

        img = img.filter(ImageFilter.MedianFilter(size=3))
        report.applied.append("median3x3")
    else:
        report.skipped.append("denoise")

    # 5) 自适应二值化（默认关闭：让 OCR 引擎自己处理更稳）
    if adaptive_threshold:
        img = ImageOps.autocontrast(img, cutoff=1)
        report.applied.append("autocontrast")
    else:
        report.skipped.append("adaptive_threshold")

    dst_p.parent.mkdir(parents=True, exist_ok=True)
    img.save(dst_p)
    report.final_size = img.size
    return report


# ==========================================================================
# 工具封装
# ==========================================================================


class PreprocessIn(BaseModel):
    src: str
    dst: str
    grayscale: bool = True
    deskew: bool = True
    denoise: bool = True
    adaptive_threshold: bool = False
    min_width: int = Field(default=1400, ge=200, le=6000)


@tool(
    name="img.preprocess",
    description="扫描件预处理链：灰度→分辨率归一→纠偏→去噪。返回做了什么/跳过了什么的报告。",
    params=PreprocessIn,
    side_effect="write",
    cost="cheap",
    tags=["ocr", "deterministic"],
)
def preprocess_tool(
    src: str,
    dst: str,
    grayscale: bool = True,
    deskew: bool = True,
    denoise: bool = True,
    adaptive_threshold: bool = False,
    min_width: int = 1400,
) -> dict:
    return preprocess_image(
        src,
        dst,
        grayscale=grayscale,
        deskew=deskew,
        denoise=denoise,
        adaptive_threshold=adaptive_threshold,
        min_width=min_width,
    ).to_dict()


__all__ = [
    "PreprocessReport",
    "estimate_skew",
    "preprocess_image",
    "preprocess_tool",
]
