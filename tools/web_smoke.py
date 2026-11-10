"""Web 演示台：真实产物渲染核验。

不做"页面 200 就算过"的检查 —— 页面 200 但区块空白是最常见的假通过
（模板字段名写错、数据没取到，都会渲染成"看起来正常"的空白）。
因此这里核对每一段关键内容是否**真的出现在 HTML 里**，
并且用产物里的真实页码去取表格页，而不是猜一个页码。

两种运行方式：

    # ① 对着已启动的服务
    python -m web.app --port 8099 --runs-root runs --run table-grid7
    python tools/web_smoke.py http://127.0.0.1:8099

    # ② **进程内**渲染，不占端口（受限环境 / CI 推荐）
    python tools/web_smoke.py --in-process

第 ② 种是必需的兜底：实测在带沙箱的环境里，新进程**绑定端口会被拒绝**
（``WinError 10013``），而老进程可能还占着端口、跑的却是旧代码 ——
那时"服务还在响应"会给出虚假的安心。进程内渲染绕开网络，
验的是同一套模板与同一套产物读取代码。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.console import enable_utf8_output  # noqa: E402

# Windows 控制台默认 GBK：本脚本会打印 ✓ / ✅ 这类符号，
# 不先切 UTF-8 就会在**最需要它的时候**抛 UnicodeEncodeError 中断。
enable_utf8_output()


def pick_runs(root: Path = Path("runs")) -> tuple[str, str]:
    """挑出「最新的、当得了回归夹具的 run」—— 一个给表格页，一个给全流程页。

    必须要求 ``manifest.json`` 存在 —— 正在跑的 run 目录已经出现，
    但产物还不完整（``manifest.json`` 要等结束才写），拿它做核验只会得到假失败。

    ⚠️ 按 **manifest 里的 started_at** 排序，而不是按目录名 ——
    目录名排序会挑到 "table-vlm5" 而不是更新的 "table-grid7"
    （字母序 v > g），于是验的是旧产物，还会给出"某项未通过"的假失败。

    ★ 「最新」还不够，还得「当得了夹具」。下面两条都是**实测**出来的，
    不加就是给自己埋雷 —— 新起的 run 会自动顶掉夹具，然后把**它自己的毛病**
    报成**代码的回归**，而跑的人会去翻代码：

      · ``status == "failed"`` 的 run 是跑到一半崩掉的，产物写了一半。
        实测（2026-10-12，三个临时 mock run 删掉之前）：它们一被选中，
        「双路互证」与「勾稽断言」双双报 ✗，而**同一份代码**在
        `web-20261012-233510`（partial）上两项都 ✓ —— 差别只在被验的那个 run。
      · ``provider == "mock"`` 的 run **答不出「双路互证」** —— 那条表格路是
        VLM 走的，mock 不产生 VLM 表格行，于是没有第二路可互证。
        实测：`demo` / `polish` / `verify` / `repro-check` / `logtest`
        这五个 mock run 的 facts 页，无一例外都没有「双路互证」。

    这两条合用的时候要小心：**别把「筛掉了」和「没有 run」混为一谈**，
    筛空了仍然退回 ``demo``（下面最后一行），而不是让调用方拿到空串。
    """
    entries: list[tuple[str, dict]] = []
    if not root.is_dir():
        return "demo", "demo"
    for d in root.iterdir():
        if not d.is_dir():
            continue
        m = d / "manifest.json"
        if not m.is_file():
            continue
        try:
            info = json.loads(m.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if info.get("status") == "failed" or info.get("provider") == "mock":
            continue
        entries.append((d.name, {**info, "_dir": d}))
    entries.sort(key=lambda kv: str(kv[1].get("started_at") or ""), reverse=True)

    tbl_run = full_run = ""
    for name, info in entries:
        d = info["_dir"]
        t = d / "channels" / "tables.json"
        if not tbl_run and t.is_file() and json.loads(t.read_text(encoding="utf-8")):
            tbl_run = name
        if not full_run and info.get("pipeline") == "full" and (d / "valuation").is_dir():
            full_run = name
        if tbl_run and full_run:
            break
    return tbl_run or "demo", full_run or "demo"


def extractor_names(run: str) -> set[str]:
    """读 ``runs/<run>/facts/facts.json``，返回该 run 用到的抽取器名（去掉 ``@版本``）。

    ``extractor`` 字段形如 ``table_grid@1.0.0`` / ``rule_engine@1.0.0``。
    用来判断某个**数据相关**的标签（如 ``table_grid``）在该 run 上是否**应该**
    出现 —— 缺了它不等于页面坏了，可能只是这个 run 没有那类事实。
    """
    p = Path("runs") / run / "facts" / "facts.json"
    if not p.is_file():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    return {str(f.get("extractor", "")).split("@")[0] for f in data.get("facts", [])}


def pinned_run(argv: list[str]) -> str:
    """``--run <id>``：把夹具 run **指名**交给核验，跳过 ``pick_runs`` 的挑拣。

    给 `reproduce_all.py` 用：它先造一个 mock run 再核验，把这个名字交出来，
    就不必赌 `runs/` 里恰好有别的 run —— 交付包解压后 `runs/` 是空的，
    赌输的那一刻本脚本会对着一个不存在的 run 报出十几项假失败。
    """
    for i, a in enumerate(argv):
        if a.startswith("--run="):
            return a.split("=", 1)[1]
        if a == "--run" and i + 1 < len(argv):
            return argv[i + 1]
    return ""


def has_manifest(run: str) -> bool:
    """``runs/<run>/manifest.json`` 在不在 —— 在，才谈得上「页面渲染了什么」。"""
    return bool(run) and (Path("runs") / run / "manifest.json").is_file()


def ensure_fixture(run: str = "demo") -> bool:
    """**一个可核验的 run 都没有**时，现造一个 mock run 当夹具。

    全新解包的交付包就是这样（`runs/` 不随包提交，`reproduce_all.py` 也还没跑）。
    不造的话下面十几项探针全报 ✗，而它们报的是「页面渲染了什么」——
    真相是「这里根本没有 run 可以渲染」。**空跑一场的绿灯是假通过**，
    空跑一场的红灯同样不是诊断，所以这里选择把夹具补齐：
    零成本、不联网、约 3 秒，与 `reproduce_all.py` 的 run 步是同一件事。

    只在 ``--in-process`` 下调用（对着别人的服务跑时，不该往人家磁盘里写东西）。
    """
    print(f"runs/ 下没有任何可核验的 run —— 先用 mock 造一个"
          f"（零成本、不联网、约 3 秒）：runs/{run}")
    r = subprocess.run(
        [sys.executable, "-m", "agent.cli", "run", "full", "--mock", "--run-id", run],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(f"  ✗ 造不出来（返回码 {r.returncode}）—— 下面的探针会照实报红："
              f"{(r.stderr or r.stdout or '')[-300:]}")
        return False
    print("  ✓ 已生成（下面的探针都对着它跑）\n")
    return True


def facts_probe_plan(run: str) -> tuple[list[str], list[tuple[str, str]]]:
    """事实库那两屏**该**出现哪些探针，以及哪些不适用、为什么。

    ★ 两个标签都是**数据相关**的，不是静态文案 —— 缺了它不等于页面坏了，
      可能只是这个 run 没有那类事实：
        · ``table_grid`` 进抽取路径图例，当且仅当该 run 真有「来自表格网格」的事实；
        · ``双路互证`` 那一块要 ``crosscheck.model_values > 0`` 才渲染
          （见 ``web/templates/facts.html``），而模型路径的取值只有 VLM 抽取器产得出。

    交付包里 ``reproduce_all.py`` 造的是 mock run（事实全部来自 ``rule_engine``），
    两个都不适用。**实测（2026-10-16）：** 包一解压跑 `reproduce_all.py`
    就是 `❌ 10/12`（web 步报 `✗ 双路互证`），连带包内自己的
    ``test_one_click_reproduce_passes_quick`` 一起红 —— 而包内 README 第一句
    就是让人跑它。原先只给 ``table_grid`` 开了这个口子，``双路互证`` 是后来
    换探针时补进去的（那句方法论口号换成承重标签），换的时候没跟着开。

    与「表格区块」同理：缺产物 → **明说理由并单独计数**，不判失败。
    """
    names = extractor_names(run)
    probes = ["抽取路径构成", "确定性优先"]
    inapplicable: list[tuple[str, str]] = []
    for label, present, why in (
        ("table_grid", "table_grid" in names,
         f"里没有 table_grid 来源（只有 {sorted(names) or '无'}）"),
        ("双路互证", any(n.startswith("vlm") for n in names),
         "里没有模型路径（VLM）事实（model_values = 0）"),
    ):
        if present:
            probes.append(label)
        else:
            inapplicable.append((label, why))
    return probes, inapplicable


def first_table_page(run: str) -> tuple[str, int, int, str]:
    """从 tables.json 里读出第一张表所在的 (doc_id, page, table_index, method)。"""
    p = Path("runs") / run / "channels" / "tables.json"
    if not p.is_file():
        return "", 1, 0, ""
    recs = json.loads(p.read_text(encoding="utf-8"))
    if not recs:
        return "", 1, 0, ""
    r = recs[0]
    return (r.get("doc_id", ""), int(r.get("page", 1)),
            int(r.get("table_index", 0)), r.get("method", ""))


def main() -> int:
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    in_process = "--in-process" in sys.argv
    base = argv[0] if argv else "http://127.0.0.1:8081"
    tbl_run, full_run = pick_runs()
    pinned = pinned_run(sys.argv)
    if pinned:
        tbl_run = full_run = pinned
    elif in_process and not has_manifest(tbl_run):
        if ensure_fixture():
            tbl_run = full_run = "demo"
    doc_id, page_no, tbl_idx, method = first_table_page(tbl_run)
    print(f"模式 = {'进程内渲染' if in_process else base}")
    print(f"表格 run = {tbl_run}   表格页 = {doc_id} p{page_no} 表{tbl_idx}"
          f"（识别方式 {method}）")
    print(f"全流程 run = {full_run}")

    fails = 0
    skipped = 0          # ★ 与 fails 分开计（2026-09-27）：缺产物导致的不适用项

    if in_process:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import io

        from fastapi.testclient import TestClient

        from web.app import create_app

        client = TestClient(create_app(runs_root="runs", default_run=tbl_run))

        def get(path: str) -> str:
            r = client.get(path)
            assert r.status_code == 200, f"{path} -> {r.status_code}\n{r.text[:400]}"
            return r.text

        def get_bytes(path: str) -> tuple[int, bytes]:
            r = client.get(path)
            return r.status_code, r.content
    else:
        def get(path: str) -> str:
            return urllib.request.urlopen(base + path, timeout=90).read().decode(
                "utf-8", "replace")

        def get_bytes(path: str) -> tuple[int, bytes]:
            with urllib.request.urlopen(base + path, timeout=90) as r:
                return r.status, r.read()

    def check(html: str, probes: list[str], title: str) -> int:
        bad = 0
        print(f"\n=== {title} ===")
        for probe in probes:
            ok = probe in html
            bad += 0 if ok else 1
            print(("  ✓ " if ok else "  ✗ ") + probe)
        return bad

    # ---- 抽取工作台：表格区块（用真实表格页）----
    # ★ 没有「带表格产物的 run」时**跳过**，而不是判失败（2026-09-27 修）。
    #   解包后的交付包里 `runs/` 是空的，`reproduce_all.py` 只造了一个 mock run，
    #   它没有 `channels/tables.json` —— 此时这 4 项检查**不适用**，不是"页面坏了"。
    #   原先一律计入 fails，于是交付包一解压跑 `reproduce_all.py` 就是 `❌ 10/12`，
    #   而包内 README 的第一句就是让人跑它（实测：解包目录里 5 项红）。
    #   跳过必须**说出理由并单独计数** —— 本项目原则：SKIPPED 既不是 FAIL 也不是 PASS。
    if not doc_id:
        skipped += 4
        print("\n=== 抽取工作台 · 表格区块 ===")
        print(f"  · 跳过 4 项：runs/ 下没有带表格产物的 run"
              f"（tbl_run={tbl_run!r} 的 channels/tables.json 不存在或为空）。"
              f"\n    无法核验：表格结构重建标题 / 识别方式 / 算术校验措辞 / 网格单元格。"
              f"\n    这 4 项**未通过也未失败**，不计入通过数。")
    else:
        html = get(f"/extract?run={tbl_run}&doc={doc_id}&page={page_no}")
        fails += check(html, [
            "表格结构重建与单元格级视觉裁决",
            method or "rule_line_grid",
            "算术校验的如实说明",
            'class="grid"',
        ], "抽取工作台 · 表格区块")

        grids = re.findall(r'<table class="grid">(.*?)</table>', html, re.S)
        cells = sum(len(re.findall(r"<td ", g)) for g in grids)
        print(f"  渲染出的网格单元格总数：{cells}")
        if cells == 0:
            fails += 1
            print("  ✗ 网格没有任何单元格")

        if re.search(r"无从校验|全部相符|处算术不平", html):
            print("  ✓ 算术校验用了三态措辞（不把「没查过」说成「通过」）")
        else:
            fails += 1
            print("  ✗ 未出现任何三态措辞")

    # ---- 单元格裁图：演示里最有说服力的一环，必须真的能取到 ----
    # ★ `html` 只在「有表格产物的 run」那个分支里被赋值 —— 这里必须同进同出
    #   （2026-09-27 修）。踩坑实录：交付包里 `runs/` 是空的，走到这条
    #   `re.findall(..., html)` 就抛 `UnboundLocalError`，于是「4 项跳过」
    #   升级成**整个脚本崩溃**；而 `reproduce_all.py` 只截了输出末尾，
    #   web 那一步报红却**看不出原因**（排查建议那行是空的）。
    crops = re.findall(r"/cell-crop/[^\"']+", html) if doc_id else []
    if not doc_id:
        skipped += 1
        print("\n=== 单元格裁图 ===\n  · 跳过 1 项：没有带表格产物的 run，无裁图可取"
              "（未通过也未失败）")
    elif not crops:
        print("\n=== 单元格裁图 ===\n  · 本页没有可展示的裁图（该页可能未做单元格裁决）")
    else:
        url = crops[0].replace("&amp;", "&")
        try:
            status, body = get_bytes(url)
            is_png = body[:4] == b"\x89PNG"
            ok = status == 200 and is_png and len(body) > 500
            print(f"\n=== 单元格裁图 ===\n  {'✓' if ok else '✗'} {url} → "
                  f"{status}，{len(body)} 字节，PNG={is_png}")
            fails += 0 if ok else 1
        except Exception as exc:
            print(f"\n=== 单元格裁图 ===\n  ✗ {url} 取不到：{exc}")
            fails += 1

    # ---- 事实库：抽取路径构成 + 主体归属纠正（本轮新增的两个面板）----
    facts_html = get(f"/facts?run={tbl_run}")
    facts_probes, inapplicable = facts_probe_plan(tbl_run)
    for label, why in inapplicable:
        skipped += 1
        print("\n=== 事实库溯源 · 抽取路径构成 ===")
        print(f"  · 跳过 1 项：run {tbl_run!r} 的事实{why}，"
              f"「{label}」本就不应出现（未通过也未失败）")
    fails += check(facts_html, facts_probes, "事实库溯源 · 抽取路径构成")

    if "主体归属纠正" in facts_html:
        print("  ✓ 展示了主体归属纠正明细（改了就有留痕）")
    else:
        print("  · 该 run 没有主体归属纠正记录（属正常）")

    # ---- 其余页面 ----
    for path, probes, title in [
        # 总览首页是答辩第一屏：它必须真的列出三个选题，而不是渲染出一个空壳。
        # 早先这份清单漏了它 —— 「9 个页面」里有 1 个从没被自动检查过。
        ("/", ["参赛总览", "三个选题"], "总览首页"),
        (f"/library?run={full_run}", ["文档库", "SHA256"], "文档库"),
        (f"/facts?run={full_run}", ["溯源核验", "置信度", "证据锚点", "页面原文"], "事实库溯源"),
        (f"/valuation?run={full_run}", ["勾稽断言", "敏感性"], "估值"),
        (f"/audit?run={full_run}", ["核查"], "核查"),
        (f"/runs?run={full_run}", ["replay", "--strict", "manifest.json"], "运行记录"),
        ("/process", ["multipart/form-data", 'type="file"'], "现场上传"),
        # 「开始分析」是 Web 端唯一的**写入口**：这页必须能渲染出收件目录清单、
        # 费用确认勾选框与阶段进度表 —— 少任何一样，用户就不知道该往哪放材料、
        # 或者会在不知情的情况下点出一次真实模型调用。
        ("/analyze", ["收件目录", "开始分析", "真实模型"], "开始分析（写入口）"),
    ]:
        fails += check(get(path), probes, title)

    # ---- 控制台（`web.panel`，与展示站是两个应用）----
    # 它只有三条契约：可以运行 / 查看运行过程 / 查看运行结果。三条各验一处。
    # 为什么值得单独扫：控制台是**演示现场唯一会被点的那个页面**，
    # 而它的渲染路径与展示站不共用模板（`panel.html` 不继承 `base.html`），
    # 上面那 9 页全绿不代表它绿。
    if in_process:
        from web.panel import create_panel

        pclient = TestClient(create_panel(runs_root="runs", default_run=full_run))
        presp = pclient.get("/")
        phtml = presp.text
        ok = presp.status_code == 200
        print("\n=== 控制台（web.panel）===")
        print(f"  {'✓' if ok else '✗'} GET / → {presp.status_code}，{len(phtml)} 字节")
        fails += 0 if ok else 1
        n_stage = len(re.findall(r"data-stage=", phtml))
        ok = n_stage >= 11
        print(f"  {'✓' if ok else '✗'} 阶段清单 {n_stage} 个（应 ≥ 11）")
        fails += 0 if ok else 1
        leaves = re.findall(r"https?://[^\"'\s>]+", phtml)
        ok = not leaves
        print(f"  {'✓' if ok else '✗'} 无外部资源引用（断网可渲染）：{[*leaves][:3]}")
        fails += 0 if ok else 1
        # ★ 控制台的立身之本：**页面里不出现模型写下的推理长文**
        #   （假设的 basis、核查的 reason）。它们属于产物，不属于控制台。
        #   这条一旦破，页面就从「8 行读数」变回「八屏散文」。
        ok = "basis" not in phtml
        print(f"  {'✓' if ok else '✗'} 不含假设推理段（basis）")
        fails += 0 if ok else 1

        def pget(path: str) -> tuple[int, str]:
            r = pclient.get(path)
            return r.status_code, r.text

        for path, probe, title in [
            ("/api/status", '"stages"', "控制台 · 任务状态"),
            (f"/api/trace?run={full_run}&offset=-1&limit=50", '"rows"', "控制台 · 事件流"),
            # ★ 这条是**必须 200** 的：过程区每 2 秒取一次，它一 500 就是整屏空白，
            #   而演示现场最怕的正是这个。降级成「空 rows + error」才是对的。
            ("/api/trace?run=no-such-run", '"error"', "控制台 · 未知 run 降级"),
            # `?kinds=` 是过程区那几个筛选 chip 走的参数。它曾经**必 500**
            # （常量没导入），而不带 kinds 时一切正常 —— 所以必须单列一项。
            (f"/api/trace?run={full_run}&kinds=tool_call,stage", '"rows"',
             "控制台 · 事件类型筛选"),
        ]:
            try:
                status, body = pget(path)
                ok = status == 200 and probe in body
                print(f"  {'✓' if ok else '✗'} {title}：{path} → {status}")
                if not ok:
                    print(f"      {body[:200]}")
                fails += 0 if ok else 1
            except Exception as exc:
                print(f"  ✗ {title}：{path} 直接报错：{type(exc).__name__}: {exc}")
                fails += 1

        # ★ 四个标签页（运行 / R1 提取 / R4 估值 / R5 核查）都要能开出来，
        #   且**没有一页**混进推理长文 —— 把结果挪进标签页之后，
        #   「这条规矩只对首页成立」是很自然的失守方式。
        tab_sizes: list[str] = []
        for t in ("run", "r1", "r4", "r5"):
            tstatus, thtml = pget(f"/?run={full_run}&tab={t}")
            tab_ok = (tstatus == 200 and "R1 提取" in thtml and "basis" not in thtml
                      and "https://" not in thtml)
            tab_sizes.append(f"{t}:{len(thtml)}B" if tab_ok else f"{t}:✗")
            fails += 0 if tab_ok else 1
        print(f"  {'✓' if '✗' not in ' '.join(tab_sizes) else '✗'} "
              f"四个标签页：{' '.join(tab_sizes)}")
        # 写错的 tab 必须退回运行页，而不是 500 或空白页。
        status, body = pget("/?tab=../../etc")
        ok = status == 200 and "开始运行" in body
        print(f"  {'✓' if ok else '✗'} 未知 tab 退回运行页 → {status}")
        fails += 0 if ok else 1

        # 产物链接必须**看出得去**、越权必须**看不出去**。
        art = f"/artifact?run={full_run}&path=facts/facts.json"
        status, body = pget(art)
        ok = status == 200 and body.lstrip()[:1] in "{["
        print(f"  {'✓' if ok else '✗'} 产物内联打开（json）→ {status}")
        fails += 0 if ok else 1
        status, _ = pget(f"/artifact?run={full_run}&path=../../pyproject.toml")
        ok = status == 404
        print(f"  {'✓' if ok else '✗'} 路径穿越被拒（../../pyproject.toml）→ {status}")
        fails += 0 if ok else 1

    # 阶段进度表来自 pipeline.yaml（full = 11 个阶段），不是写死在模板里的
    analyze_html = get("/analyze")
    stages = len(re.findall(r'class="stage ', analyze_html))
    ok = stages >= 11
    print(f"  {'✓' if ok else '✗'} /analyze 阶段进度表：{stages} 个阶段（应 ≥ 11）")
    fails += 0 if ok else 1

    # ---- 空态：不存在的 run 必须优雅降级，不能 500 ----
    # 两种空态都要验，但**不能写死 run 名** —— 实测踩过：
    # 脚本里写死了 "table-vlm4" 作为"未完成的 run"，后来那个 run 被清理掉，
    # 于是它变成"不存在"，脚本报出假失败。改为按磁盘现状挑：
    #   * 一定有一个"真的不存在"的 run 名
    #   * "存在但没跑完"（没有 manifest.json）的 run 名**有就验，没有就跳过**
    incomplete = ""
    for d in (Path("runs")).iterdir():
        if d.is_dir() and not (d / "manifest.json").is_file():
            incomplete = d.name
            break
    cases = [("no-such-run", "找不到 run")]
    if incomplete:
        cases.append((incomplete, "还没有可展示的产物"))

    for rid, expect in cases:
        try:
            status, raw = get_bytes(f"/extract?run={rid}")
            body = raw.decode("utf-8", "replace")
            ok = status == 200 and (
                expect in body or "表格结构重建" in body or "还没有可展示的产物" in body
            )
            print(f"\n=== 空态降级（{rid}）===\n  {'✓' if ok else '✗'} 返回 {status}，提示可读")
            fails += 0 if ok else 1
        except Exception as exc:
            print(f"\n=== 空态降级（{rid}）===\n  ✗ 直接报错：{type(exc).__name__}: {exc}")
            fails += 1

    # ★ 跳过项必须出现在结论行里（2026-09-27）：只印「全部通过」会把
    #   「有 4 项根本没法验」读成「4 项都验过了」。交付包里正是这种情况。
    tail_note = f"（{skipped} 项因缺产物跳过）" if skipped else ""
    print(f"\n{'✅ 全部通过' if fails == 0 else f'❌ 有 {fails} 项未通过'}{tail_note}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())

