"""P0 技术验证 —— 三个必须在写业务代码前拿到答案的问题。

赛题方案里 P0 阶段要测三件事，它们会校准后续所有成本与架构决策：

    ① **图片 token 折算率**：一张 A4 扫描件渲染成图后，实际折算多少 token？
       ——决定「是否敢对全页做 VLM 校验」以及全部成本预算。
    ② **视觉 JSON 输出稳定性**：同一张图连问 N 次，结构化输出是否始终合法、取值是否一致？
       ——决定 VLM 裁判能否作为可靠的第三层。
    ③ **三层路由可行性**：真实材料上，逐页预扫描能否正确定位扫描页与表格页？
       ——纯确定性，无需模型。

用法::

    # 全部三项（需要 API Key 与 data/raw/ 下的真实材料）
    python tools/p0_probe.py --all

    # 只测路由（无需 API Key）
    python tools/p0_probe.py --routing

    # 指定测试图片
    python tools/p0_probe.py --tokens --image runs/xxx/pages/a_p0001_200dpi.png

输出会给出可直接回填到 ``configs/models.yaml`` 的配置片段。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.console import enable_utf8_output  # noqa: E402

enable_utf8_output()


# ==========================================================================
# ① 图片 token 折算实测
# ==========================================================================


def probe_image_tokens(image_path: Path, *, repeats: int = 3) -> dict:
    """实测单张图片的 token 折算量。

    做法：发一个极短的 prompt + 一张图，读取 ``usage.prompt_tokens``，
    再发一次纯文本的同 prompt，两者之差即为图片的折算 token 数。
    这是**唯一可靠**的测法 —— 官方未公开图像 token 计算规则。
    """
    from agent.model.providers.openai_compat import OpenAICompatProvider

    provider = _make_provider()
    model = os.environ.get("P0_MODEL", "deepseek-flash")
    prompt = "用一句话描述这张图的类型（公告/报表/表格/其他）。"

    # ⚠️ 必须显式关闭思考模式（thinking.disabled）：
    #    DeepSeek 的思考模式默认开启，且**不支持 temperature** ——
    #    开着思考时 temperature=0 不生效，输出不可复现。
    #    实测：开思考时视觉任务把 3072 个 token 全烧在思维链上，content 为空。
    base_params = {
        "temperature": 0.0,
        "max_tokens": 2048,
        "thinking": {"type": "disabled"},
    }

    # 基准：纯文本
    text_only = provider.complete(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        params=base_params,
        json_schema=None,
        timeout=120.0,
    )
    base_tokens = text_only["prompt_tokens"]

    samples: list[int] = []
    for i in range(repeats):
        from agent.model.gateway import image_message

        msg = image_message("user", text=prompt, image_paths=[image_path], detail="high")
        resp = provider.complete(
            model=model,
            messages=[msg],
            params=base_params,
            json_schema=None,
            timeout=180.0,
        )
        delta = resp["prompt_tokens"] - base_tokens
        samples.append(delta)
        print(f"    第 {i+1} 次：prompt_tokens={resp['prompt_tokens']} "
              f"(基准 {base_tokens}) → 图片折算 {delta} token"
              f"  [推理 {resp.get('reasoning_tokens', 0)} tok]")
        time.sleep(0.5)

    from agent.model.prices import estimate_cost

    median = int(statistics.median(samples))
    cost_per_page = estimate_cost(model, prompt_tokens=median, completion_tokens=200)

    return {
        "image": image_path.name,
        "size_kb": round(image_path.stat().st_size / 1024, 1),
        "base_text_tokens": base_tokens,
        "image_token_samples": samples,
        "image_tokens_median": median,
        "estimated_cost_per_image_call_cny": round(cost_per_page, 6),
        "estimated_cost_100_pages_cny": round(cost_per_page * 100, 3),
    }


# ==========================================================================
# ② 视觉 JSON 输出稳定性
# ==========================================================================

_PROBE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "doc_type": {
            "type": "string",
            "enum": ["announcement", "financial_statement", "table", "other"],
        },
        "has_table": {"type": "boolean"},
        "numbers_visible": {"type": "integer"},
        "quote": {
            "type": ["string", "null"],
            "description": "页面上连续出现的任意一句话，逐字照抄",
        },
        "self_confidence": {"type": "number"},
    },
    "required": ["doc_type", "has_table", "numbers_visible", "self_confidence"],
    "additionalProperties": False,
}


def probe_vision_stability(image_path: Path, *, repeats: int = 5) -> dict:
    """连问 N 次，检查结构化输出合法率与取值一致性。

    这是判断「VLM 能否作为可靠第三层」的关键实验：
      * **JSON 合法率** = 100% 才可接受
      * **取值一致率** 决定是否需要多次投票取众数

    ⚠️ 实测教训：**必须把字段名写进提示词。**
    只给 JSON Schema 而不在提示词里说明字段名时，模型会自作主张用中文键
    （``{"文档类型": ..., "是否含表格": ...}``）—— 内容全对，但结构对不上。
    这不是模型的问题，是提示词的问题。
    生产提示词（``domain/prompts/r1_vlm_judge_field.md``）里已内联完整结构。
    """
    from agent.model.gateway import image_message, parse_json_output, text_message
    from agent.runtime.errors import StructuredOutputError

    provider = _make_provider()
    model = os.environ.get("P0_MODEL", "deepseek-flash")

    system = (
        "你是金融文档的版面读数员。\n"
        "**只输出一个 JSON 对象**，不要任何解释文字、不要 markdown 代码围栏。\n"
        "字段名必须**严格**使用下列英文字段，不得使用中文键名：\n"
        "  doc_type        —— 枚举：announcement | financial_statement | table | other\n"
        "  has_table       —— 布尔值 true/false\n"
        "  numbers_visible —— 整数，页面上可见的数字个数\n"
        "  quote           —— 页面上**连续出现**的一句原文，逐字照抄\n"
        "  self_confidence —— 0.0 到 1.0 的小数\n"
        "看不清时必须给出低 self_confidence，不要编造。"
    )

    results: list[dict] = []
    parse_failures = 0
    raw_outputs: list[str] = []
    latencies: list[int] = []

    for i in range(repeats):
        msg = image_message(
            "user",
            text=(
                "判断这页的文档类型、是否含表格、可见数字个数，并摘录一句原文。\n"
                '输出示例：{"doc_type":"announcement","has_table":true,'
                '"numbers_visible":38,"quote":"……","self_confidence":0.9}'
            ),
            image_paths=[image_path],
            detail="high",
        )
        t0 = time.perf_counter()
        resp = provider.complete(
            model=model,
            messages=[text_message("system", system), msg],
            params={
                "temperature": 0.0,
                "max_tokens": 2048,
                "thinking": {"type": "disabled"},
            },
            json_schema=_PROBE_SCHEMA,
            timeout=180.0,
        )
        latency = int((time.perf_counter() - t0) * 1000)
        latencies.append(latency)
        raw_outputs.append(resp["text"][:500])

        try:
            parsed = parse_json_output(resp["text"], _PROBE_SCHEMA)
            results.append(parsed)
            print(f"    第 {i+1} 次：✅ {json.dumps(parsed, ensure_ascii=False)[:110]} ({latency}ms)")
        except StructuredOutputError as exc:
            parse_failures += 1
            print(f"    第 {i+1} 次：❌ JSON 校验失败 — {exc.validation_error[:110]}")
            print(f"        原始输出：{resp['text'][:160]!r}")
        time.sleep(0.5)

    # 一致性统计（对可比较的字段）
    consistency: dict[str, float] = {}
    if results:
        for key in ("doc_type", "has_table"):
            values = [r.get(key) for r in results if key in r]
            if values:
                most = max(set(map(str, values)), key=lambda v: sum(str(x) == v for x in values))
                consistency[key] = round(
                    sum(str(x) == most for x in values) / len(values), 4
                )

    return {
        "image": image_path.name,
        "repeats": repeats,
        "json_valid": repeats - parse_failures,
        "json_valid_rate": round((repeats - parse_failures) / repeats, 4),
        "field_consistency": consistency,
        "latency_ms_median": int(statistics.median(latencies)) if latencies else None,
        "raw_outputs": raw_outputs,
        "raw_results": results,
    }


# ==========================================================================
# ③ 三层路由可行性（纯确定性，无需 API Key）
# ==========================================================================


def _raw_dirs(extra: str | None = None) -> list[Path]:
    """待扫描的 PDF 目录。

    默认同时扫描 ``data/raw/``（合成样本）与 ``agent/sample/``（真实材料），
    因为两者代表的场景不同：前者是受控合成数据，后者是真实公告。
    """
    dirs: list[Path] = []
    if extra:
        dirs.append(Path(extra))
    dirs += [PROJECT_ROOT / "data" / "raw", PROJECT_ROOT / "agent" / "sample"]
    seen: set[Path] = set()
    out: list[Path] = []
    for d in dirs:
        rp = d.resolve()
        if rp.is_dir() and rp not in seen:
            seen.add(rp)
            out.append(d)
    return out


def probe_routing(*, pdf_dir: str | None = None, limit: int = 0) -> dict:
    """在真实材料上验证逐页路由是否合理。

    Args:
        pdf_dir: 指定目录（默认扫描 data/raw 与 agent/sample）。
        limit: 每个目录最多扫描多少个 PDF（0 = 不限）。
    """
    from collections import Counter

    from domain.tools.document import open_pdf, probe_page
    from pipelines.r1_extract.common import decide_route

    try:
        from agent.config import load_config

        routing = load_config("routing")
    except Exception:
        routing = {}

    all_docs: list[dict] = []
    global_routes: Counter[str] = Counter()

    for root in _raw_dirs(pdf_dir):
        pdfs = sorted(root.rglob("*.pdf"))
        if limit:
            pdfs = pdfs[:limit]
        print(f"  扫描 {root.relative_to(PROJECT_ROOT) if root.is_relative_to(PROJECT_ROOT) else root}"
              f"：{len(pdfs)} 个 PDF")
        for pdf in pdfs:
            try:
                with open_pdf(pdf) as doc:
                    routes: Counter[str] = Counter()
                    page_rows = []
                    for i in range(doc.page_count):
                        probe = probe_page(doc.load_page(i), i + 1)
                        route, reason = decide_route(probe, routing)
                        routes[route.value] += 1
                        global_routes[route.value] += 1
                        page_rows.append(
                            {
                                "page": i + 1,
                                "chars": probe.char_count,
                                "garbled": round(probe.garbled_ratio, 4),
                                "text_cov": round(probe.text_coverage, 4),
                                "img_cov": round(probe.image_coverage, 4),
                                "lines": probe.rule_line_density,
                                "min_font": round(probe.min_font_size, 1),
                                "route": route.value,
                                "reason": reason,
                            }
                        )
            except Exception as exc:
                all_docs.append({"file": pdf.name, "error": f"{type(exc).__name__}: {exc}"})
                continue

            all_docs.append(
                {
                    "file": pdf.name,
                    "source": root.name,
                    "pages": len(page_rows),
                    "route_distribution": dict(routes),
                    "pages": page_rows,
                }
            )

    ok_docs = [d for d in all_docs if "pages" in d and "error" not in d]
    total_pages = sum(int(d.get("pages") or 0) for d in ok_docs)
    print(f"\n  合计：{len(ok_docs)} 个文档 / {total_pages} 页")
    for route, cnt in sorted(global_routes.items(), key=lambda kv: -kv[1]):
        pct = cnt / max(1, total_pages) * 100
        print(f"    {route:<14} {cnt:>5} 页  ({pct:5.1f}%)")

    return {
        "files": len(ok_docs),
        "pages": total_pages,
        "route_distribution": dict(global_routes),
        "error_files": [d for d in all_docs if "error" in d],
        "documents": all_docs,
    }


# ==========================================================================
# 辅助
# ==========================================================================


def _make_provider():
    """装配模型提供方。

    **凭据来源与主流程一致**（configs/models.yaml + .env），
    而不是自己读 os.environ —— 否则会出现「主流程能跑、探针说缺 Key」
    这种让人困惑的不一致。
    """
    from agent.config import load_config
    from agent.model.providers.openai_compat import OpenAICompatProvider

    cfg = load_config("models")
    pcfg = cfg.get("provider") or {}
    base_url = pcfg.get("base_url") or os.environ.get(
        "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
    )
    key = pcfg.get("api_key") or os.environ.get("DEEPSEEK_API_KEY")

    if not key:
        raise SystemExit(
            "❌ 未找到 API Key。\n"
            "   请任选一种方式配置：\n"
            "     1) 在项目根目录的 .env 中写入 DEEPSEEK_API_KEY=sk-...\n"
            "     2) PowerShell:  $env:DEEPSEEK_API_KEY=\"sk-...\"\n"
            "     3) bash:        export DEEPSEEK_API_KEY=\"sk-...\"\n"
            "   或只测路由（无需 Key）：python tools/p0_probe.py --routing"
        )
    return OpenAICompatProvider(
        base_url=base_url, api_key=key, provider_name="deepseek"
    )


def _find_test_image() -> Path | None:
    """自动寻找一张可用的测试图：优先 runs/ 下已渲染的页面。"""
    for pattern in ("runs/*/pages/*_200dpi.png", "runs/*/pages/*.png", "data/samples/*.png"):
        found = sorted(PROJECT_ROOT.glob(pattern))
        if found:
            return found[0]
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="P0 技术验证：图片 token 折算 / 视觉 JSON 稳定性 / 三层路由可行性"
    )
    ap.add_argument("--all", action="store_true", help="执行全部三项验证")
    ap.add_argument("--tokens", action="store_true", help="① 图片 token 折算实测")
    ap.add_argument("--vision", action="store_true", help="② 视觉 JSON 输出稳定性")
    ap.add_argument("--routing", action="store_true", help="③ 三层路由可行性（无需 API Key）")
    ap.add_argument("--image", default=None, help="指定测试图片路径")
    ap.add_argument("--dir", default=None, help="③ 指定扫描目录（默认 data/raw + agent/sample）")
    ap.add_argument("--repeats", type=int, default=None, help="重复次数")
    ap.add_argument("--out", default="runs/_p0_probe.json", help="结果输出路径")
    args = ap.parse_args()

    if not any([args.all, args.tokens, args.vision, args.routing]):
        ap.print_help()
        return 1

    image: Path | None = Path(args.image) if args.image else _find_test_image()
    report: dict = {"probed_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": os.environ.get("P0_MODEL", "deepseek-flash")}

    print("=" * 74)
    print("P0 技术验证")
    print("=" * 74)

    # ---------------- ③ 路由（先做，无需 Key） ----------------
    if args.all or args.routing:
        print("\n【③】三层路由可行性（纯确定性，无模型调用）")
        try:
            report["routing"] = probe_routing(pdf_dir=args.dir)
        except Exception as exc:
            print(f"  ❌ {type(exc).__name__}: {exc}")
            report["routing"] = {"error": str(exc)}

    needs_api = args.all or args.tokens or args.vision
    if needs_api and not image:
        print("\n⚠️  未找到测试图片。请先运行：")
        print("      python tools/make_sample_data.py")
        print("      python -m agent.cli run r1_extract --mock --run-id probe")
        print("   或用 --image 指定一张页面图片。")
        needs_api = args.routing  # 只保留路由结果

    # ---------------- ① 图片 token 折算 ----------------
    if (args.all or args.tokens) and image:
        print(f"\n【①】图片 token 折算实测 — {image.name}")
        try:
            report["image_tokens"] = probe_image_tokens(
                image, repeats=args.repeats or 3
            )
            r = report["image_tokens"]
            print(f"\n  ▶ 单张页面折算：**{r['image_tokens_median']} token**")
            print(f"  ▶ 单页 VLM 调用成本：约 ¥{r['estimated_cost_per_image_call_cny']:.6f}")
            print(f"  ▶ 100 页全页裁决成本：约 ¥{r['estimated_cost_100_pages_cny']:.3f}")
        except Exception as exc:
            print(f"  ❌ {type(exc).__name__}: {exc}")
            report["image_tokens"] = {"error": str(exc)}

    # ---------------- ② 视觉 JSON 稳定性 ----------------
    if (args.all or args.vision) and image:
        print(f"\n【②】视觉 JSON 输出稳定性 — {image.name}")
        try:
            report["vision_stability"] = probe_vision_stability(
                image, repeats=args.repeats or 5
            )
            r = report["vision_stability"]
            print(f"\n  ▶ JSON 合法率：**{r['json_valid_rate']:.0%}**"
                  f"（{r['json_valid']}/{r['repeats']}）")
            print(f"  ▶ 字段一致率：{r['field_consistency']}")
            print(f"  ▶ 中位延迟：{r['latency_ms_median']} ms")
            if r["json_valid_rate"] < 1.0:
                print("  ⚠️ 存在解析失败 —— 需要启用网关的自我修复重试（已默认开启）")
        except Exception as exc:
            print(f"  ❌ {type(exc).__name__}: {exc}")
            report["vision_stability"] = {"error": str(exc)}

    # ---------------- 输出配置建议 ----------------
    print("\n" + "=" * 74)
    print("配置回填建议（configs/models.yaml 的 image_tokens 段）")
    print("=" * 74)
    it = report.get("image_tokens") or {}
    if "image_tokens_median" in it:
        print(f"""
image_tokens:
  per_page_a4_170dpi: {it['image_tokens_median']}
  measured_at: "{report['probed_at']}"
  note: "由 tools/p0_probe.py 在 {it['image']} 上实测（{it['size_kb']}KB）"
""")
    else:
        print("  （未实测到图片 token 折算值，先完成 ① 项）")

    out = PROJECT_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"完整报告已写入：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
