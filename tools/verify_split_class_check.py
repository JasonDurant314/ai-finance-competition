"""验证新静态检查确实能抓到"类体被切开"，而不是一条永远为真的断言。

做法：临时把 ocr.py 改成事故形态（把 @property 挪进模块级函数体），
跑检查 → 应当失败；恢复 → 应当通过。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
target = ROOT / "domain" / "tools" / "ocr.py"
backup = target.read_text(encoding="utf-8")

ACCIDENT = '''

def _tmp_accident():
    """人为制造：属性被挤进函数体（类体被切开）。"""
    pass

    @property
    def text(self):
        return ""

'''

try:
    broken = backup.replace("\ndef ocr_lines_to_words(", ACCIDENT + "\ndef ocr_lines_to_words(", 1)
    assert broken != backup, "没能注入事故形态（锚点没找到）"
    target.write_text(broken, encoding="utf-8", newline="\n")

    r = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_static_health.py::test_no_nested_property_in_module_functions",
         "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    print("注入事故后：", "已抓到 ✓" if r.returncode != 0 else "没抓到 ✗")
    if r.returncode == 0:
        print(r.stdout[-600:])
finally:
    target.write_text(backup, encoding="utf-8", newline="\n")
    print("已恢复原文件")

r2 = subprocess.run(
    [sys.executable, "-m", "pytest", "tests/test_static_health.py", "-q",
     "-p", "no:cacheprovider"],
    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
)
print("恢复后：", r2.stdout.strip().splitlines()[-1])
