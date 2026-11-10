"""把「可提交的源码包」打成一个 zip，并在打包前做安全检查。

**为什么需要它**：赛题技术要求第 6/7 条要求提交**与现场展示版本一致的完整源代码**
（编排框架、Tool、Prompt、Skill、MCP、数据处理、日志记录）**并附依赖文件与运行说明**，
且要保证评审能在指定环境中复现。手工拖文件夹最容易出两类事故：

1. **漏**：忘了 `domain/prompts/`（提示词不在代码里）或 `configs/schemas/`，
   评审跑起来才发现缺文件；
2. **多**：把 `runs/`（体积大、含受控材料快照）、`_tmp/`、
   `__pycache__/`，甚至 **`.env`（API Key）** 一起打进去。

所以打包策略写在代码里、可复核、可重复：**白名单目录 + 显式排除 + 密钥扫描**。

用法::

    python tools/make_submission_package.py                  # 打包 + 打印清单
    python tools/make_submission_package.py --no-demo        # 不含 demo_materials
    python tools/make_submission_package.py --out x.zip
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agent.console import enable_utf8_output  # noqa: E402

enable_utf8_output()

# ---------------------------------------------------------------- 打包策略

#: 必须随包提交的顶层目录（赛题点名的核心模块都在这几个里面）
INCLUDE_DIRS = [
    "agent",        # 编排框架 / Tool / Prompt / Skill / MCP / 追踪与复现
    "domain",       # 领域提示词、技能、文档与财务工具
    "pipelines",    # 三条流水线与 full
    "web",          # 现场演示台（决赛要求的系统原型）
    "tools",        # 复现与诊断脚本（含一键复现）
    "tests",        # 测试套件（全量；数量随开发增长，不在此写死）
    "configs",      # 配置与 6 个 JSON Schema 契约
    "docs",         # 作品说明与验证记录
    "deliverables", # 计划书与视频脚本
    "data",         # 只挑下面白名单里的子目录
]

#: `data/` 下允许进包的子目录 —— 其余（raw/drafts/real/golden）是受控材料，不进包
DATA_ALLOW = ["comps", "market", "samples", "r5_eval"]

#: ★ 例外：只放行这些**文件名**，不论在哪个 data/ 子目录下（2026-09-27 加）。
#:   为什么需要它：`docs/VALIDATION_ROUND2*.md` 里用反引号引用了
#:   `data/real/定期报告/_SOURCES.json` 与 `data/drafts/_public/_SOURCES.json`
#:   （材料的来源与抓取时间 —— 正是"可追溯"要的东西），而 `real`/`drafts`
#:   不在 DATA_ALLOW 里 → 文件不入包 → 包内 `test_documents_do_not_reference_
#:   missing_files` 判 FAIL，解包后 `reproduce_all.py` 变 `❌ 10/12`。
#:   两份 `_SOURCES.json` 实测合计 12 KB。**放行溯源文件、不放行那 25 MB 的
#:   PDF 正文**（正文已由 `demo_materials/raw/` 随包提交）。
#:
#: ★ 例外二（2026-10-16 加）：`data/raw/sample_*.pdf` 两份**合成样本**
#:   （`tools/make_sample_data.py` 生成，22 KB + 13 KB，不含任何真实材料）。
#:   9 条测试拿它们当输入（`tests/test_harness.py` 6 条 + `tests/test_replay_resume.py`
#:   3 条，`glob("data/raw/*.pdf")` 找不到就一律 SKIPPED）—— 解压后跑测试会看到
#:   9 个「缺少 data/raw 下的测试材料」，看着像套件本身不完整。
#:   按**文件名**放行这两份，而不是把整个 `data/raw/` 开白：
#:   谁往那个目录里丢一份真实年报，它照样进不了包。
DATA_FILE_ALLOW = {"_SOURCES.json", "sample_annual_report.pdf",
                   "sample_pledge_announcement.pdf"}

#: ★ 例外：这些 `docs/*.csv` 要进包（2026-09-27 加）。
#:   `README.md` 用反引号引用了 `docs/sample_inventory_pages.csv`（116 KB）——
#:   它是「923 页里 106 页无文本层」这条结论的**逐页证据**，评审会照着点开。
#:   原先被「docs 下的 csv 一律不进包」这条规则挡掉，于是包内文档体检判 FAIL。
#:   116 KB 换一条可核对的证据，值。
DOCS_FILE_ALLOW = {"sample_inventory_pages.csv"}

INCLUDE_FILES = [
    "README.md", "THIRD_PARTY.md", "SUBMISSION_CHECKLIST.md",
    "requirements.txt", "pytest.ini", ".env.example",
]

#: 目录名命中即整棵子树排除
EXCLUDE_DIR_NAMES = {
    "__pycache__", ".pytest_cache", ".mplcache", ".mypy_cache", ".ruff_cache",
    "runs", "_tmp", "_piptmp", "_pytest_tmp", ".pytest_tmp", "_realtest",
    "_tabletest", "_table_bench", "_mcp_selftest", "_report_png", "_notice_png",
    "_vendor", "_whl", ".git", ".venv", "venv", "node_modules", "_live_uploads",
    ".idea", ".vscode",
}

#: 文件名/后缀命中即排除
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log", ".tmp", ".bak", ".zip", ".mp4"}
EXCLUDE_NAMES = {".env", ".DS_Store", "Thumbs.db", "desktop.ini"}

#: **绝不进包**的文件（安全兜底：密钥与凭证）
FORBIDDEN_NAMES = {".env", "credentials.json", "secrets.json"}
FORBIDDEN_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9]{16,}"), "疑似 API Key（sk-…）"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥内容"),
]

#: 可选：现场演示用的自包含材料（真实公开材料，约 20 MB）
DEMO_DIR = "demo_materials"


def _excluded(rel: Path) -> bool:
    if any(part in EXCLUDE_DIR_NAMES for part in rel.parts):
        return True
    if rel.name in EXCLUDE_NAMES or rel.suffix.lower() in EXCLUDE_SUFFIX:
        return True
    # 名字以 _ 开头的**目录**一律排除（顶层临时目录，以及 agent/sample/_scratch
    # 这类中途下载的散件）。只看目录部分 —— `__init__.py` 是文件，必须留下。
    return any(part.startswith("_") for part in rel.parts[:-1])


def collect(*, with_demo: bool, with_samples: bool = True) -> list[Path]:
    """按白名单收集要打包的文件（相对 REPO 的路径）。"""
    files: list[Path] = []
    for top in INCLUDE_DIRS:
        base = REPO / top
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(REPO)
            if _excluded(rel):
                continue
            if top == "agent" and not with_samples and rel.parts[1:2] == ("sample",):
                # ★ 写「296 份」是错的（2026-09-27 修）：那是 agent/sample/ 在
                #   **仓库里**的规模。`_excluded()` 会把 `_` 前缀的目录整棵丢掉，
                #   而 `_scratch/`（219 份抓取临时件）正是 `_` 开头 —— 所以
                #   **实际入包的是 77 份**（约 20 MB）。实测：解包后
                #   `agent/sample/*.pdf` 计数 = 77、`_scratch` 命中 = 0。
                continue      # 77 份真实公告：可选的"大规模测试"语料
            if top == "data":
                # data/ 下只放行白名单子目录，且只放行小文件
                sub = rel.parts[1] if len(rel.parts) > 1 else ""
                if sub not in DATA_ALLOW and p.name not in DATA_FILE_ALLOW:
                    continue
            if top == "docs" and p.suffix.lower() in {".csv"} \
                    and p.name not in DOCS_FILE_ALLOW:
                continue          # 中间统计表，体积大且可再生（白名单里的除外）
            files.append(rel)

    for name in INCLUDE_FILES:
        p = REPO / name
        if p.is_file():
            files.append(Path(name))

    if with_demo and (REPO / DEMO_DIR).is_dir():
        for p in sorted((REPO / DEMO_DIR).rglob("*")):
            if p.is_file():
                rel = p.relative_to(REPO)
                if not _excluded(rel):
                    files.append(rel)
    return sorted(set(files))


def scan_secrets(files: list[Path]) -> list[str]:
    """密钥扫描：宁可误报，也不把 Key 打进提交包。"""
    problems: list[str] = []
    for rel in files:
        if rel.name in FORBIDDEN_NAMES:
            problems.append(f"{rel}：文件名本身就在禁止清单里")
            continue
        path = REPO / rel
        if path.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".xlsx", ".zip"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:                     # pragma: no cover
            continue
        for pat, label in FORBIDDEN_PATTERNS:
            if pat.search(text):
                problems.append(f"{rel}：{label}")
    return problems


def human(n: float) -> str:
    """人类可读体积。

    ⚠️ 第一版把「已除以 1024 的数」又除了一次，于是 600 KB 显示成 0.6 KB ——
    体积这类**用来做决定**的数字显示错了，比不显示更糟。
    """
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="打一个可提交的源码包（含安全检查）")
    ap.add_argument("--out", default="deliverables/金融投研智能体_源码包.zip")
    ap.add_argument("--no-demo", action="store_true",
                    help="不打包 demo_materials/（现场演示材料）")
    ap.add_argument("--no-samples", action="store_true",
                    help="不打包 agent/sample/（77 份真实公告；仓库里是 296 份，"
                         "但 _scratch/ 因 _ 前缀规则不入包）")
    ap.add_argument("--list-only", action="store_true", help="只列清单，不打包")
    args = ap.parse_args(argv)

    files = collect(with_demo=not args.no_demo, with_samples=not args.no_samples)
    problems = scan_secrets(files)
    if problems:
        print("❌ 安全检查未通过，已中止打包：")
        for p in problems:
            print("   " + p)
        return 1

    total = sum((REPO / f).stat().st_size for f in files)
    by_top: dict[str, list[int]] = {}
    for f in files:
        top = f.parts[0] if len(f.parts) > 1 else "(根文件)"
        stat = by_top.setdefault(top, [0, 0])
        stat[0] += 1
        stat[1] += (REPO / f).stat().st_size

    print("=" * 62)
    print("提交包清单")
    print("=" * 62)
    for top, (n, size) in sorted(by_top.items(), key=lambda kv: -kv[1][1]):
        print(f"  {top:<20} {n:>4} 个文件   {human(size):>10}")
    print(f"  {'合计':<20} {len(files):>4} 个文件   {human(total):>10}")

    if args.list_only:
        for f in files:
            print(f"    {f}")
        return 0

    out = Path(args.out)
    if not out.is_absolute():
        out = REPO / out
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for f in files:
            zf.write(REPO / f, arcname=str(Path("金融投研智能体") / f))

    print()
    print(f"✅ 已生成 {out}")
    print(f"   压缩包 {human(out.stat().st_size)}（原始 {human(total)}）")
    print("   解压后目录名：金融投研智能体/")
    print()
    print("解压后请先跑：python tools/reproduce_all.py（约 2 分钟，零依赖）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

