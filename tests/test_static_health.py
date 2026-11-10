"""静态体检：抓「未定义的名字」这类**静默失败**。

为什么需要这个：本项目已经踩过一次同类坑 —— `judge.py` 的表格裁决循环里
写了一个未定义变量 ``max_cells``，10 张表全部抛 ``NameError``，
但异常被 ``except Exception`` 吞掉，于是：

* 阶段状态 = success
* 产物 ``table_adjudications.json`` = ``[]``
* 报告里没有任何异常迹象

只有翻 ``trace.jsonl`` 的 error 事件才能发现。**运行期测试抓不到它**
（除非恰好跑到那条分支且断言了产物非空），所以要用静态手段兜住。

实现方式是 ``symtable``（标准库）：它替我们做完作用域分析，
我们只需要判断「函数里引用了一个既不是局部、也不是自由变量、
也不是模块级、也不是内置」的名字。
"""

from __future__ import annotations

import builtins
import re
import symtable
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 只体检自己写的代码；第三方与产物目录一律不看
PACKAGES = ("agent", "domain", "pipelines", "web", "tools", "tests")

_BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__"}


def _iter_sources():
    for pkg in PACKAGES:
        base = ROOT / pkg
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if any(part.startswith("_vendor") or part == "__pycache__"
                   for part in path.parts):
                continue
            yield path


def _undefined_in_scope(table: symtable.SymbolTable, module_names: set[str]) -> list[str]:
    """递归收集本作用域及其子作用域里「引用了但没定义」的全局名。"""
    bad: list[str] = []
    for sym in table.get_symbols():
        name = sym.get_name()
        if not sym.is_referenced():
            continue
        if sym.is_local() or sym.is_parameter() or sym.is_free():
            continue
        # 到这里说明它被当成全局名使用
        if sym.is_assigned():
            # ``global x; x = 1`` 这类：模块级一定存在同名符号，交给模块表判断
            if name in module_names or name in _BUILTINS:
                continue
        if name in module_names or name in _BUILTINS:
            continue
        bad.append(f"{table.get_name()}:{name}")
    for child in table.get_children():
        bad.extend(_undefined_in_scope(child, module_names))
    return bad


def _has_star_import(src: str) -> bool:
    import ast

    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            return True
    return False


def test_no_undefined_names_anywhere() -> None:
    problems: list[str] = []
    checked = 0
    for path in _iter_sources():
        src = path.read_text(encoding="utf-8")
        if _has_star_import(src):
            # 星号导入会把名字凭空带进来，symtable 看不到 —— 跳过并明示
            problems.append(f"{path.relative_to(ROOT)}: 使用了 import *，无法静态体检")
            continue
        try:
            table = symtable.symtable(src, str(path), "exec")
        except SyntaxError as exc:                      # pragma: no cover
            problems.append(f"{path.relative_to(ROOT)}: 语法错误 {exc}")
            continue
        checked += 1
        module_names = {s.get_name() for s in table.get_symbols()}
        for child in table.get_children():
            for name in _undefined_in_scope(child, module_names):
                problems.append(f"{path.relative_to(ROOT)}: 未定义的名字 {name}")

    assert checked > 30, f"只体检到 {checked} 个文件，路径枚举可能失效"
    assert not problems, "存在未定义的名字（运行期会抛 NameError）：\n  " + "\n  ".join(problems)


def test_judge_stage_counts_table_adjudication_errors() -> None:
    """★ 表格裁决的异常必须**计数**，不得只写 trace 就了事。

    实测踩坑：裁决全失败时，产物是空数组、阶段仍是 success，
    从报告上完全看不出问题。因此 ``table_adjudication_errors``
    这个统计量是"看得见失败"的唯一保证。
    """
    src = (ROOT / "pipelines" / "r1_extract" / "judge.py").read_text(encoding="utf-8")
    assert "table_adjudication_errors" in src, "裁决失败没有统计量"
    assert "table_attempts" in src, "未区分『进入裁决』与『裁决成功』"
    # except Exception 之后必须紧跟计数，否则又是静默吞掉
    idx = src.index("except Exception as exc:")
    tail = src[idx: idx + 900]
    assert "table_errors += 1" in tail, "except Exception 里没有计数，失败会被静默吞掉"


# ==========================================================================
# 类成员完整性（"类体被切开"这类结构性事故）
# ==========================================================================


def test_no_nested_property_in_module_functions() -> None:
    """★ 模块级函数里不得出现 ``@property def x(self)`` —— 那是类体被切开的信号。

    实测事故：往 ``OcrResult`` 后面插一个新函数时，插入点落在了**类的中间**，
    于是 ``text`` / ``mean_confidence`` / ``low_confidence_ratio`` / ``to_dict``
    被挤进了新函数的**函数体**（``@property`` 在函数体内是合法的嵌套定义，
    只是永远不会被调用）。结果是 ``OcrResult`` 静默失去这些属性，
    直到跑真实扫描件才炸：

        AttributeError: 'OcrResult' object has no attribute 'text'

    更值得记住的是：**当时 378 个测试全绿** —— 因为没有测试直接用 ``OcrResult``。
    静态检查比"等某天跑真实数据"快得多地锁住这一类结构性错误。
    """
    import ast

    bad: list[str] = []
    for path in _iter_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:                      # 只看模块顶层
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in node.body:
                if not isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                args = [a.arg for a in sub.args.args]
                is_property = any(
                    (isinstance(d, ast.Name) and d.id == "property")
                    or (isinstance(d, ast.Attribute) and d.attr in ("setter", "getter"))
                    for d in sub.decorator_list
                )
                if is_property and args[:1] == ["self"]:
                    bad.append(f"{path.relative_to(ROOT)}:{sub.lineno} "
                               f"{node.name}() 里嵌了 @property {sub.name}(self)")
    assert not bad, (
        "疑似类体被切开：模块级函数里出现了 @property 方法（它们永远不会被调用）：\n  "
        + "\n  ".join(bad)
    )


def test_ocr_result_public_api_is_intact() -> None:
    """``OcrResult`` 的成员必须都还在（上一条事故的行为级底线）。"""
    import inspect

    from domain.tools.ocr import OcrLine, OcrResult

    for name in ("text", "mean_confidence", "low_confidence_ratio"):
        assert isinstance(inspect.getattr_static(OcrResult, name, None), property), (
            f"OcrResult.{name} 不是属性 —— 类体可能被切开了"
        )
    assert callable(getattr(OcrResult, "to_dict", None))

    r = OcrResult(
        lines=[OcrLine(text="a", confidence=0.9, box=[(0, 0), (1, 0), (1, 1), (0, 1)])],
        engine="t",
    )
    assert r.text == "a"
    assert r.to_dict()["line_count"] == 1


# ==========================================================================
# 文档诚信体检：文档里写了、磁盘上却没有的模块
# ==========================================================================


def test_agent_module_list_matches_disk() -> None:
    """★ ``agent/__init__.py`` 的模块清单必须与磁盘上的目录一致。

    为什么值得一条测试：赛题要求提交「智能体编排框架、Tool、Prompt、Skill、MCP、
    数据处理和日志记录等核心模块」。我们曾在 ``agent/__init__.py``、``README.md``
    与 ``THIRD_PARTY.md`` 三处同时声明了 ``mcp/``（MCP 客户端）与 ``skills/``，
    而这两个目录**当时并不存在**。

    这类「文档描述了不存在的系统」有几个共同特征，使它特别危险：

    - 运行期没有任何报错（文档不是代码，不会被导入）
    - 所有测试都通过（测试不会去读文档）
    - 本地开发时看不出问题（写文档的人记得自己的意图）
    - **评审按图索骥点开目录，一眼就发现**

    修法有两条：把模块补上，或者把话删掉。我们两条都做了 ——
    ``agent/mcp/`` 与 ``agent/skills/`` 现在是真实实现（各有独立测试），
    这条测试负责防止清单再次漂移。
    """
    import re

    src = (ROOT / "agent" / "__init__.py").read_text(encoding="utf-8")
    declared = re.findall(r"^(\w+)/\s{2,}\S", src, re.MULTILINE)
    assert declared, "未能从 agent/__init__.py 的文档字符串中解析出模块清单"

    missing = [d for d in declared if not (ROOT / "agent" / d).is_dir()]
    assert not missing, (
        "agent/__init__.py 声明了并不存在的模块："
        + ", ".join(f"agent/{m}/" for m in missing)
        + " —— 文档描述了一个不存在的系统"
    )

    # 反向：磁盘上有、清单里漏了（漏写不是撒谎，但会让清单失去参考价值）
    on_disk = {
        p.name
        for p in (ROOT / "agent").iterdir()
        if p.is_dir() and not p.name.startswith("_") and p.name != "sample"
    }
    undeclared = sorted(on_disk - set(declared))
    assert not undeclared, (
        "agent/__init__.py 的模块清单漏了："
        + ", ".join(undeclared)
        + " —— 请补进清单（评审靠它理解目录结构）"
    )


def test_readme_architecture_lists_real_modules() -> None:
    """``README.md`` 架构图里出现的每个 ``<目录>/<模块>/`` 都必须真实存在。

    实现要点（踩过一次）：第一版把解析出的名字与一份**手写白名单**求交集，
    于是新增的、不存在的模块名被交集过滤掉了 —— 检查永远通过。
    一条只会通过的检查等于没有检查。现在改为跟随架构图的分组行
    （``agent/``、``domain/``、``pipelines/`` ……）确定父目录，逐个核对磁盘，
    并且有 ``tools/doc_truth_check.py`` 用注入的方式证明它会失败。
    """
    import ast
    import re

    lines = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    # README 里有多个代码块（顶部还有一张质量链示意图），
    # 这里只取**含 "★ 自研 Harness" 标注**的那一块 —— 即架构图。
    marker = next(
        (i for i, ln in enumerate(lines) if "★ 自研 Harness" in ln), None
    )
    assert marker is not None, "README 里找不到架构图（含「★ 自研 Harness」标注的代码块）"
    start = max(i for i in range(marker) if lines[i].strip().startswith("```"))
    end = next(i for i in range(marker, len(lines)) if lines[i].strip().startswith("```"))

    # 分组行形如「agent/                    ★ 自研 Harness」，不带树形前缀
    group = re.compile(r"^([A-Za-z_][\w]*)/\s")
    # 条目行形如「├── runtime/    loop.py」
    item = re.compile(r"[├└]──\s+([A-Za-z_][\w.]*)/")

    groups: list[tuple[str, list[str]]] = []
    current: str | None = None
    for line in lines[start + 1 : end]:
        g = group.match(line)
        if g:
            current = g.group(1)
            groups.append((current, []))
            continue
        it = item.search(line)
        if it and current:
            groups[-1][1].append(it.group(1))

    assert groups, "未能从架构图解析出任何分组"
    names = {g for g, _ in groups}
    assert {"agent", "domain", "pipelines"} <= names, (
        f"架构图分组异常：{sorted(names)}"
    )

    missing = [
        f"{g}/{m}/"
        for g, mods in groups
        if (ROOT / g).is_dir()
        for m in mods
        if not (ROOT / g / m).exists()
    ]
    assert not missing, (
        "README.md 架构图声明了并不存在的模块：" + ", ".join(missing)
    )
    agent_mods = dict(groups).get("agent", [])
    assert "mcp" in agent_mods, "README 架构图漏了自研 MCP 客户端（赛题点名的核心模块）"
    assert "skills" in agent_mods, "README 架构图漏了技能注册表"

    # 顺带核对架构图与磁盘的**完整**一致性（防漏写：漏写会让评审看不懂目录）
    disk_agent = {
        p.name
        for p in (ROOT / "agent").iterdir()
        if p.is_dir() and not p.name.startswith("_") and p.name != "sample"
    }
    undocumented = sorted(disk_agent - set(agent_mods))
    assert not undocumented, (
        "agent/ 下有目录未出现在架构图中：" + ", ".join(undocumented)
    )


def test_documents_do_not_claim_unimplemented_modules() -> None:
    """在整份文档里搜「自主实现」清单中提到的模块，逐一对上磁盘。

    这条覆盖 ``THIRD_PARTY.md`` —— 它是给评审看的合规声明，
    对不上就是「项目说明与实际不符」。
    """
    import re

    text = (ROOT / "THIRD_PARTY.md").read_text(encoding="utf-8")
    refs = set(re.findall(r"`(agent|domain|pipelines)/([a-z_]+)/", text))
    missing = [
        f"{pkg}/{mod}/"
        for pkg, mod in sorted(refs)
        if not (ROOT / pkg / mod).is_dir()
    ]
    assert not missing, (
        "THIRD_PARTY.md 引用了并不存在的目录：" + ", ".join(missing)
    )
    # 至少引用了一次 mcp 与 skills（否则说明声明没跟上实现）
    prefixes = {f"{p}/{m}" for p, m in refs}
    assert "agent/mcp" in prefixes, "THIRD_PARTY.md 未声明自研 MCP 客户端"
    assert "agent/skills" in prefixes, "THIRD_PARTY.md 未声明技能注册表"


# ==========================================================================
# 控制台编码体检：会打印 ✓ / ✅ 的独立脚本必须先切 UTF-8
# ==========================================================================

#: 会触发 GBK 编码错误的符号（GBK 里没有这些码位）
_MARK_CHARS = "✓✗✅❌⚠→▲■▶"
_MARK_RE = re.compile(f"[{_MARK_CHARS}]")


def _missing_utf8_bootstrap(paths) -> list[str]:
    """返回「会打印 Unicode 符号、却没切 UTF-8 输出」的独立脚本。

    判据刻意保守：只要求**有 ``__main__`` 入口**（库模块的输出会经过 CLI，
    而 CLI 入口已经切过了）且脚本里出现会触发 GBK 报错的符号。
    """
    bad: list[str] = []
    for path in paths:
        try:
            src = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):      # pragma: no cover
            continue
        if "__main__" not in src or not _MARK_RE.search(src):
            continue
        if "enable_utf8_output" not in src:
            bad.append(str(path))
    return bad


def test_standalone_scripts_bootstrap_utf8_output(tmp_path: Path) -> None:
    """★ README 让评审跑的脚本，不能在 GBK 控制台上**自己先崩掉**。

    实测踩过的坑：``python tools/web_smoke.py --in-process``（README 明确
    推荐评审用的一条命令）在 Windows GBK 控制台上打印 ``✓`` 时抛
    ``UnicodeEncodeError`` 并中断 —— 排查工具在**最需要它的时候**不可用。
    ``agent/console.py`` 早就写明了约定：「CLI 入口与**所有独立脚本**的
    第一行调用 ``enable_utf8_output()``」，但当时 16 个脚本没照做。

    为什么值得一条测试：这个缺陷**只在特定控制台上出现**，
    在 UTF-8 终端 / CI 里永远绿；而它毁掉的恰好是评审的第一印象。

    下面同时做**反向注入**：证明这条检查真的会失败，而不是恒真。
    """
    targets = sorted((ROOT / "tools").glob("*.py")) + [ROOT / "web" / "app.py"]
    missing = _missing_utf8_bootstrap(targets)
    assert not missing, (
        "这些独立脚本会打印 ✓/✅ 却没切 UTF-8，在 GBK 控制台上会抛 "
        "UnicodeEncodeError 中断：\n  " + "\n  ".join(missing)
    )

    # —— 注入验证：构造两个假脚本，一个该被抓、一个不该被抓 ——
    # 用 tmp_path 夹具而不是 tempfile：受限环境里系统 Temp 常常不可写
    # （见 tests/conftest.py 的说明）。
    good = tmp_path / "good.py"
    good.write_text(
        "import sys\n"
        "from agent.console import enable_utf8_output\n"
        "enable_utf8_output()\n"
        'if __name__ == "__main__":\n    print("✓ 通过")\n',
        encoding="utf-8",
    )
    bad = tmp_path / "bad.py"
    bad.write_text(
        'if __name__ == "__main__":\n    print("✓ 通过")\n',
        encoding="utf-8",
    )
    assert _missing_utf8_bootstrap([bad]) == [str(bad)], (
        "注入的『未切 UTF-8 的脚本』没被抓到 —— 这条检查等于没有检查"
    )
    assert _missing_utf8_bootstrap([good]) == [], (
        "已正确引导的脚本被误报，检查过严"
    )


# ==========================================================================
# 文档引用体检：文档里写出来的**仓库内路径**必须真实存在
# ==========================================================================

#: 会被当"路径引用"检查的顶层目录（`runs/` 刻意不在其中：运行产物不入库，
#: 文档提到它是正常的，见 docs/REVIEWER.md 第五节的对照表）
_DOC_TOPS = ("agent/", "domain/", "pipelines/", "web/", "tools/", "tests/",
             "configs/", "docs/", "data/", "deliverables/")

#: 只有带这些后缀的反引号内容才算"文件引用"（目录另有检查）
_DOC_EXTS = (".py", ".md", ".json", ".yaml", ".yml", ".html", ".css",
             ".txt", ".ini", ".pdf", ".csv", ".zip")

#: 明显的占位/示例名不算"声称"（例：事故复盘里引用的历史路径 `data/raw/xxx.pdf`）
_DOC_PLACEHOLDERS = ("xxx", "yyy", "zzz", "foo", "bar", "your", "...", "<", "*", "?")

_DOC_BACKTICK = re.compile(r"`([^`\n]+)`")


def _missing_doc_paths(docs: list[tuple[str, str]], root: Path) -> list[str]:
    """返回「文档里引用了、磁盘上却不存在」的仓库内路径。

    只认**反引号包起来的、带文件后缀的、以已知顶层目录开头**的路径。
    三条限制都是刻意的：文档里还有命令、代码、run 名，
    把它们当路径检查只会制造噪声，而噪声会让这条体检被忽略。
    """
    bad: list[str] = []
    for name, text in docs:
        for m in _DOC_BACKTICK.finditer(text):
            raw = m.group(1).strip()
            if not raw.startswith(_DOC_TOPS) or raw.endswith("/"):
                continue
            if not raw.endswith(_DOC_EXTS):
                continue
            if any(hint in raw for hint in _DOC_PLACEHOLDERS):
                continue
            if not (root / raw).exists():
                bad.append(f"{name} → {raw}")
    return bad


def _read_docs() -> list[tuple[str, str]]:
    targets = [ROOT / "README.md", ROOT / "SUBMISSION_CHECKLIST.md",
               ROOT / "THIRD_PARTY.md"]
    targets += sorted((ROOT / "docs").glob("*.md"))
    targets += sorted((ROOT / "deliverables").glob("*.md"))
    out: list[tuple[str, str]] = []
    for p in targets:
        if p.is_file():
            out.append((p.relative_to(ROOT).as_posix(),
                        p.read_text(encoding="utf-8")))
    return out


def test_documents_do_not_reference_missing_files() -> None:
    """★ 文档里点名的仓库文件必须真的在。

    这是「文档也是产物」这条纪律的第三条体检（前两条查**模块目录**：
    ``test_agent_module_list_matches_disk`` / ``test_readme_architecture_lists_real_modules``）。
    前两条只管目录树，管不到 ``tools/xxx.py`` 这类**文件级**引用 ——
    而评审最常做的就是照着文档里的路径去点开文件。

    实测价值：本轮就是靠这条检查发现 README / VALIDATION_INDEX 引用了
    磁盘上并不存在的 run（`annual41-seg` / `annual37-yrseq`）以及过时的
    测试计数。**引用不存在的东西，与声称实现了不存在的模块是同一类错误。**

    同样带**注入验证**：下面的假文档必须被抓出来。
    """
    missing = _missing_doc_paths(_read_docs(), ROOT)
    assert not missing, (
        "文档引用了并不存在的文件（评审会照着点开）：\n  " + "\n  ".join(missing)
    )

    # —— 注入验证 ——
    fake = [("fake.md", "见 `agent/no_such_module_9f3a.py` 与 `tools/no_such_tool.py`")]
    got = _missing_doc_paths(fake, ROOT)
    assert len(got) == 2, f"注入的不存在路径没被抓到（拿到 {got}）—— 检查等于没有"

    ok = [("fake.md", "见 `agent/cli.py` 与 `tools/reproduce_all.py`")]
    assert _missing_doc_paths(ok, ROOT) == [], "真实存在的路径被误报，检查过严"

    # 占位名与 run 引用不该被误报（否则噪声会淹没这条体检）
    noise = [("fake.md", "历史事故：`data/raw/xxx.pdf` 找不到；运行产物在 `runs/demo/` 下")]
    assert _missing_doc_paths(noise, ROOT) == [], "占位名 / runs/ 引用被误报"
