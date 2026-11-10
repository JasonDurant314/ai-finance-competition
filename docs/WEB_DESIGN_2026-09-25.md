# Web 演示台 —— 可视化增强与页面精简（设计）

日期：2026-09-25
范围：`web/` 展示层 + 仓库垃圾清理。不改动 `pipelines/`、`agent/`、`domain/` 的运行时逻辑。

## 1. 背景与目标

决赛要求「现场展示投研智能体逻辑和表现，并接受现场答辩评审组的提问」，且成果须
「可验证、可追溯、可复现」。当前 Web 演示台存在三个问题：

1. 阻断性依赖 Bug —— 全部 HTML 页面 500（详见 §2）。
2. 页面过长 —— `/facts` 单页 196,595 字节（192 KB）HTML，把 359 条事实一次性铺开；
   大量方法论说明文字淹没结论。
3. 几乎没有图表 —— 仅 `/valuation` 有 4 张 matplotlib PNG（且白底配色与暗色 UI 冲突）；
   `/extract`、`/audit`、`/facts` 零图表，尽管它们的数据非常适合可视化。

成功标准：页面变短、重点前置、图表补齐；现有功能零损失（溯源、单元格裁图、
运行复现、现场处理全部照旧）；有可重复执行的验证方式证明没坏。

## 2. 已完成的独立修复（前置）

`requirements.txt` 把 `fastapi`/`uvicorn` 注释掉、且无 lock 文件，导致评审装依赖后拿到
新版 Starlette —— 而 Starlette 1.7.0 移除了旧签名 `TemplateResponse(name, context)`，
项目 17 处调用全部失效，所有 HTML 页面 500。

另：`python-multipart` 从未声明，却是 `Form`/`File` 端点的硬依赖，缺失时应用在
注册路由阶段即抛 `RuntimeError`，演示台根本起不来。

修复：`web/app.py` 17 处改为 `TemplateResponse(request, name, context)`（该签名自
Starlette 0.29 起支持，前后兼容）；`requirements.txt` 解除注释并补 `python-multipart`。

验证：TestClient 全路由 18/18 → 200；真起 `python -m web.app` + curl 全通。

## 3. 可视化层：`web/charts.py`

纯 Python 生成内联 SVG，零新增依赖。

为什么是内联 SVG：内联 SVG 继承页面 CSS，可直接用 `var(--ok)` 等变量 → 自动匹配
主题，不必维护第二套配色；矢量清晰；不落盘、不增请求；契合项目「无外部 CDN 依赖」的
受控数据环境约束。

配色（用户明确要求）：中性色系 —— 蓝灰为主，红色只用于真实告警的小面积
状态标记，不做大面积红黄。理由：赛题强调风险审慎性，满屏红黄会把需要看到的告警淹没在噪声里。
语义色仅用于结果状态标签，不用于图表面积填充。

> 修订（2026-09-27）：上面最后一句当时没有落实。
> `charts.py` 的 `alarm=` 参数虽然写明「小面积用法」，实现上却是直接把整根条 /
> 整段弧填成 `--s-alarm`。于是 `/audit` 的严重度环图在「全部已确认」时是一整圈红
> （面积占比 100%），`/extract` 的算术校验条是半根红条（58%）。
> 现改为：面积一律走中性色板，告警只用「标签前的 ▲ 记号 + 该行数值/图例文字变红」
> 表达 —— 红只落在几个字符上。已用脚本核验：全部图元中再无 `fill:var(--s-alarm)` /
> `stroke:var(--s-alarm)`。规则没变，只是这次把它落实到了实现里。

图元（保持少量、可复用）：

| 函数 | 用途 |
|---|---|
| `bar_h(items)` | 横向条：指标 Top-N、抽取器构成、检出规则贡献 |
| `stack(segments)` | 单条堆叠：三层路由占比、确定性 vs 模型 |
| `donut(items)` | 环形：来源层分布、严重度分布 |
| `hist(values, bins)` | 直方图：OCR 置信度、事实置信度 |
| `range_chart(lo, mid, hi, markers)` | 区间图：综合区间 vs DCF 单点 vs 相对估值 |
| `tornado(items)` | 龙卷风：敏感性（网页版） |

每个 `<svg>` 带 `role="img"` + `aria-label`；数据点带 `<title>` 悬停显示数值。

不碰 pipeline 的计算逻辑：`r4_valuation` 产出的 4 张 PNG 与网页端 SVG 并存 ——
PNG 是报告 Markdown 里按路径引用的交付产物。

> 修订（2026-09-27）：PNG 的配色改了，生成逻辑没改。
> 本节原先写「原样保留」，并据此把这 4 张 PNG 排除在下面的配色纪律之外。
> 事后看，这个豁免是错的：那 4 张图用的是 `RdYlGn` 色图 + `#c62828` 红 +
> `#f9a825` 黄 + `#2e7d32` 绿，正是本文件 §配色 明令禁止的「大面积红黄」，
> 而「因为它是交付产物所以豁免」等于把唯一的违规处写进了豁免条款。
> 现已改为与网页端同族的一支蓝黑墨五级明度阶（`pipelines/r4_valuation/report.py`
> 顶部的 `_INK.._INK5`）。只改颜色常量：取值、坐标、算式、渲染顺序与
> 断言逻辑一律未动，499 项非 Web 测试与 91 项 Web 测试全绿。
> 详见 §3 末尾与 `docs/VALIDATION_ROUND7_COMPS.md` 的同一原则：
> 「可解释性优先于省事」——豁免条款也是一种省事。

## 4. 导航重组 + 总览首页

`/` 原重定向到 `/library`，现改为渲染新的 `overview.html`。

导航按选题分组。标签采用通知原文的「选题 N」编号与赛题全名（不用口头的「任务一/四/五」），
理由：评审对照的是通知文本，编号与名称一致才能一眼对应。实际实现（`base.html` 的 `NAV`）：

```
总览 │ 选题 1 · 数据结构化提取  [抽取工作台 /extract][事实库溯源 /facts]
     │ 选题 4 · 自动化估值建模  [估值建模 /valuation]
     │ 选题 5 · 研究报告纠错核查 [核查报告 /audit]
     │ 工程                    [运行与复现 /runs][文档库 /library][现场处理 /process]
     │ ▶ 开始分析 /analyze
```

顶部另有全局「运行」选择器（`<select onchange="this.form.submit()">`），任意页面一次点击可换 run ——
这是 §5.2 展示取舍得以成立的前提。

总览页（答辩第一屏）：顶部 KPI 条（run id / 页数 / token / 状态），下接三张选题卡，
每卡含赛题号、赛题名、一句话说明、4 项关键指标与入口链接；图表区与「复现与合规」说明
收进折叠块。三张卡在缺少对应产物时各自显示 `note` 说明原因（如选题 5 的指标缺失说明，
见 §5.2 —— 那是数据条件所致，非缺陷），不静默留白。

## 5. 各页精简原则

总原则（用户选定）：结论留正文，论据进 `<details>` 默认折叠，零信息损失 ——
评委屈问「为什么」时当场展开。

代码注释一律不动：注释里记录了大量踩坑结论（Jinja2 的 `values` 键名会解析成 dict
方法、`page` 字段可能是 `null`、`route` 字段名必须与 `pages/index.json` 一致），这些是
维护资产，只搬走页面上的展示文字。

| 页面 | 正文保留 | 收进 `<details>` | 图表（实际交付） |
|---|---|---|---|
| `/facts` | KPI 条、筛选、溯源面板、分页 | 冲突说明、抽取路径长篇论证、主体归属表 | 5：抽取路径（确定性 vs 模型）、来源层构成、置信度分布、指标条目数 Top 12、待复核原因分类 |
| `/extract` | 三层通道卡片、表格网格、窗口式分页器 | 原文前 1200 字转储、长 note、裁决明细 | 5：各通道覆盖页数、VLM 扫描字段可用性、表格单元格裁决构成、表格算术自洽校验、页面路由构成 |
| `/audit` | KPI 条、问题清单 | 指标表、草稿全文 | 2–3：问题严重度构成、错误类型分布，+ P/R/F1（仅当 `metrics` 存在，见 §5.2） |
| `/valuation` | KPI 条、断言结论 | 断言表、假设表、敏感性数值表、风险、适用范围、完整报告 | 11 = 7 内联 SVG（估值区间与各方法落点、企业价值构成、敏感性龙卷风、可比 PE、可比 PB、预测期收入、预测期 FCFF）+ 4 张 matplotlib PNG 交付产物 |

图表总量：4 → 23（改动前仅 `/valuation` 有 4 张 PNG；现另增 19 张内联 SVG，
其中 `/valuation` 的 4 张 PNG 因是报告 Markdown 引用的交付产物而与 SVG 并存）。

> 修订（2026-09-27）：上句原写 4 张 PNG「原样保留」。保留的是「这张图还在、
> 仍由 r4 渲染、仍被报告引用」这层意思；配色已改为与网页端同族的蓝黑墨明度阶，
> 不再是 `RdYlGn` 红黄绿。见 §3 的修订块。

### 5.1 `/facts` 分页（问题主因）

359 条（别的 run 达 999 条）事实一次性铺开是「内容太多」的主因。加分页（50/页），
并加指标聚合视图：同一指标多条时折叠为一行（显示条数与取值区间），可展开看明细。

新增查询参数：`page`。与现有 `metric`/`layer`/`review`/`fact_id` 正交，筛选后分页。

### 5.2 `/audit` 的 `metrics: null`：设计使然，不是缺陷（本节两度修订）

第一稿断言：「`metrics` 为 null 但 `per_draft_metrics` 有数据，回退读之即可」。
该断言错误 —— 来源是一次子代理报告，未加验证即采信。（记此以儆效尤。）

第二稿仍不准确：把成因写成「演示 run 数据不全 / 遗留待裁决的缺陷」，隐含着「本该有却没有」。
第三稿（本版）实测推翻了这个框架：`null` 是正确行为。

逐行复核 `pipelines/r5_audit/check.py`（798 行）后的确切机制：

```
208|  labels = _load_golden_labels(ctx, one_id)
209|  if labels is not None:                      # ← 只有带标注的草稿进入 evaluated
210|      evaluated.append((one_id, one.findings, labels, one_text))
...
234|  if evaluated:                               # ← 空则 metrics 保持 None
235|      audit.metrics = _evaluate_all(evaluated)
237|      if len(evaluated) > 1:                  # ← 单草稿时 per_draft_metrics 保持 []
238|          audit.per_draft_metrics = []
```

`metrics` 与 `per_draft_metrics` 同受一个闸门 `evaluated` 控制，而 `evaluated` 只收
「有 golden 标注」的草稿。所以二者同空是同一个原因的两个表现，不存在「一个有值另一个没有」
的回退空间 —— 第一稿设想的回退路径在数据上根本不成立。

`_load_golden_labels` 第 349 行的兜底：

```python
golden_dir = Path(ctx.configs.get("paths", {}).get("golden") or "data/golden")
```

两个 run 的实际配置（读 `manifest.json`，非推测）：

| run | `data_dir` | pipeline | tokens | 事实库 | 估值 | `metrics` |
|---|---|---|---:|---|---|---|
| `web-20260925-123442` | `inbox` | `full`（r1→r4→r5） | 976,205 | 359 条 + 4 张 PNG | 12.16–14.04 元/股 | `null` |
| `r5-eval-annual16` | `data/r5_eval` | 仅 `r5_audit` | 0 | — | — | P=1.0 R=0.5 F1=0.6667 |

（注：本表此前的「facts 10 / valuation 7」等数字是 `runs/<run>/<dir>/` 的目录条目数，
不是事实条数，属误读，已更正为实际内容。）

`metrics` 需要人工标注的黄金测试集才能计算。演示 run 核查的是一份合成草稿，材料目录下
没有标注文件 → 指标在数学上就无法计算，`null` 反映的是实际情况，并非漏跑。评测 run 指向标注齐备的
`data/r5_eval/`，指标正常，且与 `docs/COMPETITION_FIT.md:21` 记录的 P 1.0 / F1 0.6667 一致
（该 run 全程 0 token —— 纯确定性比对，正是赛题「不靠大模型通读找错」主张的直接证据）。

web 层的正确做法是说明实际情况，不做回退取值（无值可回退）：

1. `metrics` 为空时不渲染指标卡（不显示 0、不显示占位），改渲染一条 `note.info`，
   讲清为什么没有、指明这需要标注集，并指向 `r5-eval-annual16`。
2. 顶栏「运行」选择器是全局的（`<select onchange="this.form.submit()">`），切换 run 只需一次点击。
3. 不修改 `pipelines/` 的任何计算逻辑，也不给演示 run 补造标注 —— 那会让「现场看到的」
   与「评审复现的」不一致，违背 `web/__init__.py` 的核心承诺。

该展示取舍的结论（2026-09-25 定）：当前没有任何一个 run 同时具备估值产物与审计指标，
这是数据条件的反映，不是需要修的缺陷。演示脚本据此确定：选题 1 / 选题 4 用
`web-20260925-123442`；选题 5 讲指标时切一次到 `r5-eval-annual16`。不改数据配置。

## 6. 死代码清理

已逐条独立复核（grep 计数 + 调用方确认）。

### 第 1 档：实际只删了 4 项（本节曾误标「零风险垃圾」，经复核更正）

已删除（确认无任何引用）：

- `pytest-cache-files-*`（9 个空目录）
- `_piptmp/`
- `_analyze/`（已确认是别的盘 `G:\` 上的旧状态，pid 已死）
- `_table_bench/tables_dump.json`

未删除 —— 且本就不该删。初稿把下列目录一并列为「零风险垃圾（约 276 MB）」，
该判断错误，复核发现它们是活跃工作目录，删除会破坏功能：

| 目录 | 为什么不能删 | 证据 |
|---|---|---|
| `_tmp/` | 测试套件的临时根目录。`_make_temp_base()` 按序探测 `AGENT_PYTEST_TMP` → 系统 temp（需 `DSH_ALLOW_TMP`）→ `ROOT/_tmp`；两个环境变量都没设时，每个 `tmp_path` fixture 都写在这里。里面的 `big-runs-*` 是当前测试草稿，不是陈年垃圾 | `tests/conftest.py:52-73` |
| `_mcp_selftest/` | `mcp-selftest` CLI 命令与一个测试按需创建的工作目录 | `agent/cli.py:466-471`、`tests/test_mcp.py:143-150` |
| `.mplcache/` | matplotlib 字体缓存，可再生（删了只是下次变慢，无功能影响，但亦无收益） | — |
| `.pytest_tmp/` | 空目录，无害 | — |

（另：初稿的「267 MB」是过期数字，实测 `_tmp/` 为 24.5 MB / 2779 文件。）

正确处理方式是「打包排除」，不必「删除」—— 而且该机制已经存在：
`tools/make_submission_package.py:61-67` 的 `EXCLUDE_DIR_NAMES` 已完整列出
`.mplcache`、`_tmp`、`_piptmp`、`_pytest_tmp`、`.pytest_tmp`、`_mcp_selftest`、
`_table_bench`、`_notice_png`、`_report_png` 等，整棵子树不进提交包。
`SUBMISSION_CHECKLIST.md:97` 同样说明。故无需任何改动。

> 这一条正是「删之前再三确认」的价值所在：若按初稿清单「把活干完」删掉 `_tmp/`，
> 就会移除整个测试套件的临时根目录，并让 `mcp-selftest` 命令与 `test_mcp.py` 失去工作目录。

**现状注（2026-10-17，实测更正）**：上面「不能删」的说法过重了。
`_make_temp_base()` 对候选目录做的是 `mkdir(parents=True, exist_ok=True)`
（`tests/conftest.py:61`），`_mcp_selftest/` 也由 `mcp-selftest` 命令与
`test_mcp.py` 当场创建 —— 两者都是**按需重建**的工作目录，删掉不会让任何东西
失去工作目录，只是下一次跑测试/命令时重新长出来。本次整理确实删了
`_tmp/`（75 MB / 988 个子目录）、`_mcp_selftest/`、`_analyze/`，
随后完整套件仍是 `736 passed, 2 skipped`。真正该守住的结论只有一条：
它们**不该进提交包**（`EXCLUDE_DIR_NAMES` 已经这么做了），而不是「不许删」。

### 第 2 档：确认为死代码，已全部删除（已复核为空）

- CSS `.pagetext .fuzzy` 与 `label.check`（全仓 class 属性 0 次）→ 已移除
- `artifacts.py` 7 个零调用成员（`exists`/`build_stats`/`export_summary`/`audit_md`/
  `adjudication_of`/`EvidenceView.bbox_list`/`FactView.primary_page`）→ 已移除
- 4 个模块级 helper + `web/__init__.py` 的死 re-export → 已移除
- `app.py` 的 `uploaded_name=` 参数 → 已移除

### 第 3 档：不动

`_notice_png/`（无脚本可重建，源 PDF 文本层为 0 字符，是 `docs/COMPETITION_FIT.md`
的依据）、`_report_png/`、`_realtest/`、`_tabletest/`、8 个孤立 `tools/` 诊断脚本、
`FactView.extractor_label`/`extractor_deterministic`（测试守护诚实性声明）、`.tag.L4`（防御性，2 行）。

附带：`_notice_png/` 与 `_report_png/` 漏在 `.gitignore` 之外（兄弟目录都列了）→ 已补上。

说明：`_notice_png/` 被排除出提交包是有意为之：通知 PDF 原件本身会进包，
PNG 是从它渲染出的派生证据，留在仓库供 `COMPETITION_FIT.md` 溯源即可。

## 7. 验证方式与最终结果

全路由冒烟（数据源：`web-20260925-123442`）：

```python
from fastapi.testclient import TestClient
from web.app import create_app
c = TestClient(create_app(default_run='web-20260925-123442'))
for p in ['/','/library','/extract','/facts','/valuation','/audit','/runs','/process',
          '/analyze','/healthz','/analyze/status','/facts?layer=L1','/facts?review=1',
          '/facts?page=2','/extract?doc=doc_840b15aa4dc3&page=7',
          '/valuation-chart/value_range.png','/annotated-draft',
          '/page-image/doc_840b15aa4dc3/7']:
    assert c.get(p, follow_redirects=True).status_code == 200
```

### 7.1 最终结果（2026-09-25 实测）

| 项 | 结果 |
|---|---|
| `tests/test_web.py` | 80 passed / 2 skipped（新增 8 个测试前为 72） |
| 全量测试套件 | 579 passed / 2 skipped（303.3s）；本轮开工前为 559 passed / 1 skipped |
| 项目自带核验工具 | `tools/web_pages_check.py` 全绿 · `tools/web_smoke.py --in-process` 全绿 |
| TestClient 双 run 冒烟 | 24/24 路由 200（`web-20260925-123442` + `r5-eval-annual16`） |
| 真 uvicorn（端口 8743） | 13/13 路由 200，含 `/static/app.css`；按精确 PID 结束，`netstat` 确认无 LISTENING 残留 |
| 图表渲染 | 六个页面 `<svg>` 计数 5/5/5/7/2/3，被转义的 svg = 0（确为图形非源码） |
| 配色写法 | `style="fill:var(--sX)"` 43 处，`fill="var(--sX)"` 表示属性 0 处（后者浏览器支持不一致） |
| 空图表网格 | 九个页面均为 0 个空 `.charts` 容器（0 检出时不会渲染空框） |

> 注意：Windows 上 `os.kill(pid, 0)` 不是存活探测：它走 `TerminateProcess`，
> 等于再杀一次，不去查询状态，因此恒不抛异常 → 会被误读成「进程仍在」。判断服务是否
> 真的停了要用 `netstat -ano | grep <port>` 看有无 `LISTENING`（`TIME_WAIT` 是内核
> 正常回收，不代表还在跑）。本轮实测即被这个假信号误导过一次。

### 7.2 页面体积（只列同口径有效前后对）

> 口径陷阱：本表一律用 `len(response.text)`（字符数）。用
> `len(response.content)`（字节数）会因中文 UTF-8 占 3 字节而整体放大 1.1–1.25 倍。
> 实测复核时正是踩了这个坑：误用字节数后，7 个未改动的页面看起来全都"增长了 12%"，
> 险些当成回归去查。两者是同一份文档，只是单位不同 —— 比较体积前先确认单位一致。


| 页面 | 之前 | 现在 | 降幅 |
|---|---:|---:|---:|
| `/facts` | 196,595 | 61,926 | −69% |
| `/extract` | 63,851 | 15,529 | −76% |
| `/extract?doc=…&page=7` | 120,507 | 66,736 | −45% |

`/audit` 与 `/valuation` 无有效基线 —— 这两个模板在量「之前」值之前就已被改写，
取到的 `10168` / `46116` 实为改写之后的值。故不声称其降幅；二者的变化是结构重排
（可见内容折叠 + 新增 7 张内联图），字节数并非其合适指标。此处照实记录以免误读。

改动真凶定位（值得记下）：`/extract` 页面膨胀至 120 KB，主因是 273 个页码芯片占 57 KB
（近半页），并非单元格裁图或文本网格（实测仅 397 个单元格、16 张图）。
改为窗口式分页器后降至 16 KB。第一直觉（裁图）经测量被证伪。

§7.4 两处修复的体积代价（同口径字符数，实测）：

| 页面 | 修复前 | 修复后 | 代价 |
|---|---:|---:|---:|
| `/valuation` | 46,116 | 47,486 | +1,370（占位样本声明 + 可比公司来源折叠块） |
| `/audit` | 10,168 | 10,682 | +514（E1–E6 覆盖清单） |
| 其余 7 页 | — | — | ±0（逐字节未变） |
| 合计 | 199,257 | 201,141 | +1,884（+0.95%） |

「其余 7 页 ±0」是这张表最有价值的一行：它证明这两处修复没有波及其他页面，
代价也确实只花在该花的地方。

### 7.3 数据正确性（按内容而非状态码验证）

- 分页：359 条 → 50/50/…/9 共 8 页；`page=9` 收敛到 8，`page=0` 收敛到 1
- 筛选：`metric=revenue` → 44 条 1 页；`review=1` → 73 条 2 页
- 筛选条件在翻页链接中互相保留；溯源链接不重复 `fact_id`（FastAPI 标量查询参数取首次出现值）

### 7.4 项目自带的核验工具抓到两处我自己的改动引入的内容丢失

**教训：改写模板后，必须跑项目自带的「内容探针」，不能只信自己写的测试。**
`tools/web_pages_check.py` 与 `tools/web_smoke.py` 断言的是「关键文案真的在 HTML 里」，
不是「页面返回 200」—— 正好覆盖了模板改写最容易出的错。跑它们，抓到两处：

| 页面 | 丢失内容 | 性质 |
|---|---|---|
| `/valuation` | 「本项目刻意不提供内置占位样本」声明 | 诚实性声明丢失，最严重 |
| `/audit` | E4（本次未检出的错误类型） | 能力清单被截断 |

第 1 处：该声明出自 `pipelines/r4_valuation/model.py:38-51`，讲的是「快照缺失时显式失败，
不退回占位样本 —— 否则报告会看起来完全正常，里面却是编的数」。改写时被
`peer_selection_rule` 的图表说明挤掉了。这条不能丢：相对估值的可信度全靠
「倍数从哪来」，删掉声明后页面就只是一组来路不明的倍数。已恢复，并加测试守护。

第 2 处：原页面列的是 E1–E6 全类型，我的版本只画「出现过的类型」，于是本次
无 E4 检出的 run 上 E4 就消失了。这会被误读成「不支持 E4」—— 而赛题考的正是
E1–E6 的覆盖能力。修法是分层处理，不改回全类型（那会出现 4 根 0 值条）：

- `type_bar` 图答「本次查出了什么」—— 只画检出的类型
- 新增 `type_taxonomy` 清单答「覆盖哪几类」—— 规范六类全列，未检出的显式写 0
  并弱化显示（`.chip.zero`），另将表外类型（上游新增而 web 未及同步）原样标出，
  不静默吞掉

`chartdata.CANONICAL_TYPES` 记录规范六类及其别名键（E2/E3/E5 在产物里各有两个键，
需合并计数否则清单重复）。新增 5 个测试守护，并做变异测试验证其有效性：
把实现改成「只列非零类型」后，3 个测试失败（数据层 2 + 路由层 1），恢复后逐字节一致。

顺带发现：查 `/valuation` 时发现产物里有一个页面上完全没展示的
`comps_provenance` 字段，内含快照路径、财务时点、行情抓取时间戳、三个数据来源接口、
PE/PB 自算式、以及「为何不用接口现成字段」的理由。这正是赛题「可追溯」要的东西，
已收进 `/valuation` 的折叠块（`倍数从哪来：数据来源、时点与算式`），
值全部取自产物、逐字不改。新增 2 个测试：一个锁定这些条目都在页面上，
一个逐字比对页面与 `valuation.json`（含来源字符串与算式），防止页面"转述"来源。

> **订正（2026-09-27，本条曾被写成本轮亮点，实为事故）**：当时把该字段里的
> 「接口给的 601857 的 PB 字段为 1.22，而『总市值 ÷ 归母权益』算出 0.86，
> 两者矛盾且无法解释」当作最有答辩价值的可追溯证据展示在了页面上。
> 实测证明这组数字是错的：
>
> - 快照里 `market_cap ÷ equity_parent = 1,932,701,525,758.08 ÷ 1,617,287,000,000`
>   = 1.1950，不是 0.86；`pb` 字段存的也正是这个自算值 1.1950；
> - 该文本是 `tools/fetch_comps.py` 里的硬编码散文，快照数据几经重抓，散文从未同步；
> - 它同时出现在 3 个快照 JSON、工具 docstring 与第七轮验证文档中，且接口原始 PE/PB
>   字段从未落盘，故连"1.22"也无从复核。
>
> 换言之：本轮把一个产物里唯一无法回溯的数字，当成全站最可追溯的亮点讲了一遍，
> 还给它加了"逐字比对"的测试来锁死 —— 测试锁住的是错误文本本身。
> 已把 5 处全部改为只陈述可验证的口径政策（见各文件修订记录）。
>
> 教训：`逐字比对页面与产物` 只能保证页面没篡改产物，不能保证产物本身为真。
> 下一层要问的是「这个数在产物里能否被独立重算」。

> 注意，一处文档不一致（未擅自改动，供裁决）：`docs/P0_VALIDATION.md` 的「已知限制」
> 表第 4 条仍写着「可比公司为离线占位样本 … 需接入真实行情源」，但
> `docs/VALIDATION_ROUND7_COMPS.md` 与 `docs/VALIDATION_INDEX.md` 第 7 行都记载
> 该问题已在第七轮修好（占位样本换成真实行情 + 年报财务，产出
> `data/comps/601857_2025A.json`），本轮也实测确认估值产物带真实来源元数据。
> P0 文档是阶段报告、`VALIDATION_INDEX.md` 才是「唯一入口」，故不算错，
> 但两份提交文档并排看会互相矛盾，值得一句说明或一次对齐。

## 8. 明确不做（YAGNI）

- 不引入任何 JS 图表库（违反受控数据环境约束，且需在 `THIRD_PARTY.md` 声明）
- 不改 `pipelines/` 的任何计算逻辑
- ~~不改动 r4 的 4 张 PNG 产物及其生成代码~~ → 修订（2026-09-27）：
  `pipelines/r4_valuation/report.py` 里的配色常量已改（`RdYlGn` 红黄绿 →
  蓝黑墨明度阶），因为原配色违反本文件 §配色 的「不大面积红黄」，
  而之前用「它是交付产物」把它豁免掉了 —— 那是把违规处写进豁免条款。
  边界仍然守住：取值、坐标、算式、渲染顺序、断言逻辑一律未动；
  499 项非 Web 测试全绿。此处保留删除线，不直接删掉，是为了让这次
  「为什么破了自己定的规矩」有据可查。
- 不为图表另建一套配色变量（直接复用 `app.css` 现有变量）
- 不做移动端专门适配（现场用投影/桌面展示；现有响应式规则保留）
