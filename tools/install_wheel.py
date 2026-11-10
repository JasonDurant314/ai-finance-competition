"""在受限环境中安装纯 Python wheel。

## 为什么需要这个脚本

受限沙箱下 ``pip install`` 会在解压 wheel 元数据时报
``PermissionError [Errno 13]``（pip 需要在临时目录里枚举/删除文件，
而沙箱禁止这些操作），而且 site-packages 本身也不可写。
但**纯 Python wheel 本质上只是一个 zip**，因此可以直接解包到
工作区内的 ``_vendor/`` 目录，再由 ``web`` 包把它加进 ``sys.path``。

只对 ``py3-none-any`` 类型的 wheel 有效 —— 带 C 扩展的包不能用这种方式。

用法::

    python tools/install_wheel.py fastapi
    python tools/install_wheel.py fastapi==0.141.1

**注意**：``_vendor/`` 是应急方案。正常的复现路径是
``pip install -r requirements.txt`` —— 请优先使用后者。
"""

from __future__ import annotations

import argparse
import json
import site
import sys
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.console import enable_utf8_output  # noqa: E402

# Windows 控制台默认 GBK：本脚本会打印 ✓ / ✅ 这类符号，
# 不先切 UTF-8 就会在**最需要它的时候**抛 UnicodeEncodeError 中断。
enable_utf8_output()

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VENDOR = PROJECT_ROOT / "_vendor"


def site_packages() -> Path:
    """定位用户 site-packages（仅在可写时使用）。"""
    for p in [site.getusersitepackages(), *site.getsitepackages()]:
        pp = Path(p)
        if pp.exists() and pp.name == "site-packages":
            probe = pp / f".w{id(pp)}"
            try:
                probe.mkdir()
                probe.rmdir()
                return pp
            except OSError:
                continue
    return DEFAULT_VENDOR


def fetch_metadata(pkg: str) -> dict:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json", timeout=60) as r:
        return json.load(r)


def pick_wheel(meta: dict, version: str | None) -> dict:
    """挑一个纯 Python wheel。"""
    urls = meta["urls"]
    if version:
        releases = meta.get("releases", {}).get(version)
        if not releases:
            raise SystemExit(f"找不到版本 {version}")
        urls = releases
    pure = [
        u for u in urls
        if u.get("packagetype") == "bdist_wheel"
        and u["filename"].endswith("py3-none-any.whl")
    ]
    if not pure:
        names = [u["filename"] for u in urls if u.get("packagetype") == "bdist_wheel"]
        raise SystemExit(
            f"没有纯 Python wheel（候选：{names[:5]}）。\n"
            "本脚本只适用于 py3-none-any 类型的包。"
        )
    return pure[0]


def install(pkg: str, *, version: str | None = None, dest: Path | None = None) -> Path:
    meta = fetch_metadata(pkg)
    whl = pick_wheel(meta, version)
    target = dest or site_packages()
    target.mkdir(parents=True, exist_ok=True)

    cache = PROJECT_ROOT / "_whl"
    cache.mkdir(exist_ok=True)
    local = cache / whl["filename"]

    if not local.is_file():
        print(f"下载 {whl['filename']} …")
        urllib.request.urlretrieve(whl["url"], local)  # noqa: S310 - 固定 pypi.org

    print(f"解包到 {target} …")
    with zipfile.ZipFile(local) as z:
        z.extractall(target)

    print(f"✅ 已安装 {pkg} {meta['info']['version']} -> {target}")
    if target == DEFAULT_VENDOR:
        print("   注意：这是工作区内的 vendored 安装。")
        print("   `web` 包会自动把 _vendor/ 加入 sys.path，因此可直接使用。")
        print("   正式复现请优先使用：pip install -r requirements.txt")
    return target


def main() -> int:
    ap = argparse.ArgumentParser(description="受限环境下安装纯 Python wheel")
    ap.add_argument("package", help="包名（可带 ==version）")
    ap.add_argument("--dest", default=None,
                    help="目标目录（默认：site-packages 可写则用它，否则用 _vendor/）")
    args = ap.parse_args()

    name, _, ver = args.package.partition("==")
    target = install(name, version=ver or None,
                     dest=Path(args.dest) if args.dest else None)

    # 立即验证可导入（vendored 安装需要临时加路径）
    import importlib

    if str(target) not in sys.path:
        sys.path.insert(0, str(target))
    mod = name.replace("-", "_")
    try:
        importlib.invalidate_caches()
        m = importlib.import_module(mod)
        print(f"   验证导入：{mod} {getattr(m, '__version__', '?')}")
    except ImportError as exc:
        print(f"   ⚠️ 导入失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
