"""现场实时处理 —— 让评委现场喂入一个新文件，立刻看到三层引擎跑一遍。

赛题要求：「提供**现场可实时处理的样例数据**」。
本模块就是这条要求的落点。

## 设计要点

* **用真实流水线，不是演示版**：直接调用 ``pipelines.r1_extract`` 与
  ``domain.tools`` 里的同一批函数 —— 现场看到的与命令行跑出来的一致。
* **每页只做「解析 + 归属」，不做 LLM 抽取**：现场时间有限（3–5 分钟），
  逐页调用大模型会让演示变成等待。因此：
      文本层 / OCR / 表格 / 路由   → 全部现场计算（纯确定性，秒级）
      VLM 整页扫描                 → 可选，只对用户勾选的少数页触发
* **上传文件写在独立目录**：不污染 ``data/`` 与 ``runs/``，
  且遵守「受控数据环境」——只允许写入 ``_live_uploads/``。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from agent.config import PROJECT_ROOT, load_config
from domain.tools.document import open_pdf, probe_page, render_page_png
from domain.tools.ocr import available_engines, recognize
from domain.tools.tables import tables_of_page
from pipelines.r1_extract.common import decide_route
from pipelines.r1_extract.extract_text import _extract_candidates

UPLOAD_DIR = PROJECT_ROOT / "_live_uploads"
MAX_BYTES = 30 * 1024 * 1024      # 单个文件上限
MAX_PAGES = 25                    # 现场演示只处理前 N 页
OCR_MAX_PAGES = 8                 # OCR 较慢，限制页数保证演示流畅
VLM_MAX_PAGES = 2                 # VLM 调用最慢，默认只对前 2 页


def process_upload(filename: str, data: bytes, *,
                   use_vlm: bool = False,
                   max_pages: int = MAX_PAGES) -> dict[str, Any]:
    """对上传的 PDF 跑一遍三层解析，返回逐页结果。

    Args:
        filename: 原始文件名（仅用于展示与展示用命名）
        data: 文件字节
        use_vlm: 是否对前若干页调用 VLM 整页扫描（会消耗 token 且较慢）
        max_pages: 最多处理多少页

    Returns:
        ``{"file", "pages": [...], "stats": {...}, "elapsed_s": float}``
    """
    if not data:
        raise ValueError("文件为空")
    if len(data) > MAX_BYTES:
        raise ValueError(f"文件过大（{len(data) / 1e6:.1f}MB），演示上限 {MAX_BYTES / 1e6:.0f}MB")
    if not filename.lower().endswith(".pdf"):
        raise ValueError("目前只支持 PDF。扫描件请先转成 PDF（图片型 PDF 亦可）")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    staging = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{Path(filename).name}"
    staging.write_bytes(data)

    routing = load_config("routing")
    engines = available_engines()
    started = time.perf_counter()

    pages: list[dict[str, Any]] = []
    stats: dict[str, int] = {}

    with open_pdf(staging) as doc:
        n = min(doc.page_count, max_pages)
        for i in range(n):
            page_no = i + 1
            page_obj = doc.load_page(page_no - 1)

            # ---------- 第 0 层：预扫描 + 路由（纯确定性，毫秒级） ----------
            probe = probe_page(page_obj, page_no)
            route, reason = decide_route(probe, routing)
            stats[route.value] = stats.get(route.value, 0) + 1

            row: dict[str, Any] = {
                "page": page_no,
                "probe": probe.to_dict(),
                "route": route.value,
                "route_reason": reason,
                "text": None,
                "ocr": None,
                "table": None,
                "vlm": None,
                "elapsed_ms": 0,
            }
            t0 = time.perf_counter()

            # ---------- 第 1 层：文本层直取 ----------
            if route.value in ("A_text_only", "E_mixed", "D_table"):
                raw = page_obj.get_text("text")
                row["text"] = {
                    "chars": len(raw.strip()),
                    "preview": raw[:1200],
                    "candidates": _extract_candidates(raw)[:25],
                }

            # ---------- 表格检测（有表就展示结构）----------
            if route.value == "D_table" or probe.rule_line_density >= 20:
                try:
                    tbls = tables_of_page(page_obj, page_no=page_no)
                    if tbls:
                        t = tbls[0]
                        arith = None
                        arith_state, arith_label = "", ""
                        try:
                            from domain.tools.tables import (
                                check_table_arithmetic,
                                describe_arithmetic,
                            )

                            arith = check_table_arithmetic(t).to_dict()
                            arith_state, arith_label = describe_arithmetic(arith)
                        except Exception:
                            pass
                        row["table"] = {
                            "method": t.method,
                            "rows": t.n_rows,
                            "cols": t.n_cols,
                            "has_rules": t.has_rules,
                            "grid": t.grid()[:12],
                            "arithmetic": arith,
                            "arith_state": arith_state,
                            "arith_label": arith_label,
                        }
                except Exception as exc:
                    row["table"] = {"error": f"{type(exc).__name__}: {exc}"}

            # ---------- 第 2 层：OCR（较慢，限量）----------
            if engines and row["text"] is None and len(
                [p for p in pages if p.get("ocr")]
            ) < OCR_MAX_PAGES:
                try:
                    img = UPLOAD_DIR / f"{staging.stem}_p{page_no:04d}.png"
                    dpi = int(routing.get("ocr_dpi", 300))
                    render_page_png(doc, page_no, img, dpi=dpi)
                    res = recognize(img, engine=engines[0], min_confidence=0.0)
                    row["ocr"] = {
                        "engine": res.engine,
                        "dpi": dpi,
                        "lines": len(res.lines),
                        "mean_confidence": round(res.mean_confidence, 4),
                        "low_conf_ratio": round(res.low_confidence_ratio, 4),
                        "preview": res.text[:1200],
                    }
                except Exception as exc:
                    row["ocr"] = {"error": f"{type(exc).__name__}: {exc}"}

            row["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
            pages.append(row)

    # ---------- 可选：VLM 整页扫描（仅前 N 页）----------
    vlm_info: dict[str, Any] = {"enabled": use_vlm, "pages": []}
    if use_vlm:
        vlm_info = _run_vlm_probe(staging, pages[:VLM_MAX_PAGES])

    total_s = time.perf_counter() - started
    result = {
        "file": filename,
        "staged_as": staging.name,
        "page_count": len(pages),
        "pages": pages,
        "stats": stats,
        "ocr_engines": engines,
        "elapsed_s": round(total_s, 2),
        "vlm": vlm_info,
    }
    (UPLOAD_DIR / f"{staging.stem}.result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return result


def _run_vlm_probe(staging: Path, pages: list[dict[str, Any]]) -> dict[str, Any]:
    """对前若干页调用 VLM 整页扫描（现场演示的「第三层」按钮）。"""
    out: dict[str, Any] = {"enabled": True, "pages": [], "error": None}
    try:
        from agent.model.gateway import ModelGateway, RoleConfig
        from agent.model.providers.openai_compat import OpenAICompatProvider
        from agent.config import load_models_config
        from domain.tools.vlm import build_metric_catalog, scan_page_fields

        cfg = load_models_config()
        pcfg = cfg.get("provider") or {}
        roles = {}
        for role, spec in (cfg.get("roles") or {}).items():
            roles[role] = RoleConfig(
                model=str(spec.get("model", "deepseek-flash")),
                temperature=float(spec.get("temperature", 0.0)),
                max_tokens=int(spec.get("max_tokens", 16384)),
                reasoning_effort=spec.get("reasoning_effort"),
                thinking=bool(spec.get("thinking", False)),
                top_p=float(spec.get("top_p", 1.0)),
                supports_vision=bool(spec.get("supports_vision", False)),
            )
        provider = OpenAICompatProvider(
            base_url=str(pcfg.get("base_url", "")),
            api_key=pcfg.get("api_key"),
            provider_name=str(pcfg.get("name", "openai_compat")),
        )
        gw = ModelGateway(provider=provider, roles=roles, offline=False, strict_json=True)
        catalog = build_metric_catalog()
    except Exception as exc:
        out["error"] = f"模型未就绪：{type(exc).__name__}: {exc}"
        return out

    with open_pdf(staging) as doc:
        for row in pages:
            page_no = row["page"]
            img = UPLOAD_DIR / f"{staging.stem}_p{page_no:04d}_judge.png"
            try:
                render_page_png(doc, page_no, img, dpi=300)
                page_text = ""
                if row.get("text"):
                    page_text = row["text"].get("preview", "")
                elif row.get("ocr"):
                    page_text = row["ocr"].get("preview", "")
                scan = scan_page_fields(
                    gw, image_path=img, page_no=page_no,
                    page_text=page_text, metric_catalog=catalog,
                )
                out["pages"].append(
                    {
                        "page": page_no,
                        "page_type": scan.page_type,
                        "self_confidence": scan.self_confidence,
                        "rejected": scan.rejected_count,
                        "unknown_metric": scan.unknown_metric_count,
                        "fields": [f.model_dump(mode="json") for f in scan.fields],
                    }
                )
            except Exception as exc:
                out["pages"].append(
                    {"page": page_no, "error": f"{type(exc).__name__}: {exc}"}
                )
    return out


__all__ = ["process_upload", "UPLOAD_DIR", "MAX_BYTES", "MAX_PAGES"]
