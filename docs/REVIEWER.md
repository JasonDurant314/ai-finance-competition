# 评审复现指引 —— 一条命令走完全部可复现结论

> 面向对象：初赛/决赛评审、答辩现场、后续接手的人。
> 目标：不需要 API Key、不需要联网，在一台装了 Python 的机器上，
> 把本项目对外声称的核心结论亲手复现一遍。
> 需要真实模型的路径单独放在[第五节](#五需要真实模型的路径可选)。
>
> 本文档里的每一条命令、每一段输出都在 2026-09-25 实际跑过
> （Windows / Python 3.13.14），不是从别处抄来的预期值。

---

## 〇、先看这张表：复现什么、怎么算通过

> **耗时不在表里写死。** 它随机器与负载浮动，而且**是双峰的** —— 同一台机器上，
> 同一天四次跑全量测试分别是 170.65s / 177.60s（机器空闲）与 387.99s / 413.46s
> （同时有别的东西在跑）；后一次空闲重跑又回到 177.60s，可见变的是**负载**而非
> 套件本身。真值由脚本**自己当场打印**：
> `python tools/reproduce_all.py` 跑完会逐步列出秒数（本机 2026-10-17 那次是
> `doctor 5.3s / run 9.1s / verify 1.4s / replay 9.6s / audit 1.6s / web 10.1s /
> tests 416.8s`），你机器上是多少就是多少。要安排时间，只需要一个量级：
> **第 1–6 行都是秒级到十几秒，第 7 行是唯一的分钟级** —— 也因此它是全表唯一
> 会随负载大幅波动的数，别拿它当性能判据。

> ⚠️ **同一个 run 名不要连跑两次 —— 现在 `run` 会直接拒绝。** `Tracer` 以**追加**
> 方式写 `trace.jsonl`（`--resume` 正是靠这个），重跑会把新事件叠在旧事件后面、
> `seq` 从 0 重来。实测：同 run 名跑第二遍后 trace 从 146 行变 292 行、**146 个
> `seq` 全部重复**，随后 `verify` 判**不可复现**。所以 `run` 一旦发现目标 run 目录
> 里已有产物、又没带 `--resume`，会**拒绝执行（退出码 3）**并给出两条出路：
> 加 `--resume` 续跑，或删掉该目录 / 换 `--run-id` 从零重来。
> `tools/reproduce_all.py` 开跑前自己会清掉旧目录，所以它连跑几次都没事。

| # | 复现的东西 | 命令 | 判据 |
|---|---|---|---|
| 1 | 环境与依赖就绪 | `python -m agent.cli doctor` | 输出 `✅ 必需项全部就绪` |
| 2 | 端到端跑通 R1→R4→R5 | `python -m agent.cli run full --mock --run-id demo` | 输出 `完成 11/11 阶段` |
| 3 | 产物逐字节可复现 | `python -m agent.cli replay demo --strict` | 输出 `✅ 可复现` |
| 4 | 过程可追溯（审计报告） | `python -m agent.cli audit demo` | 生成 `runs/demo/audit.md` |
| 5 | 治理面：Tool / Prompt / Skill / MCP / 契约 | `agent tools` / `prompts` / `skills` / `mcp --self-test` / `contracts` | 绑定校验全部通过 |
| 6 | Web 演示台渲染 | `python tools/web_smoke.py --in-process` | `✅ 全部通过` |
| 7 | 测试套件 | `python -m pytest -q` | `736 passed`（skip 条数取决于有没有装 node，见 3.6） |
| — | 以上全部，一条命令 | `python tools/reproduce_all.py` | `汇总：✅ 全部通过（12/12）`，并逐步打印各步耗时 |

---

## 一、环境准备（约 1 分钟）

```bash
# Python 3.11+（本项目开发环境为 3.13）
python --version

# 必需依赖
pip install -r requirements.txt

# 可选 ①：OCR 引擎（纯 CPU，无需 GPU）。
#   合成样本与纯文本材料不需要它（该阶段直接跳过）；
#   跑含扫描页或表格页的真实材料则**必须先装** —— 这类页面是硬依赖，
#   未装时 R1 在该阶段显式失败并打印这条安装命令，不会静默降级。
pip install rapidocr-onnxruntime

# 可选 ②：严格 JSON Schema 契约校验
pip install jsonschema

# 可选 ③：node。控制台页面里的 JS 由 `tests/test_panel_js.py` 用 node **真的
#   执行**一遍（右侧「Agent 活动」栏住在那一层 —— 它渲染成 HTML 正确，不等于
#   在浏览器里活着）。没有 node 时这条测试 **skip 并说明原因**，其余照常：
#   交付物的运行不需要 JS，只有「看这一栏」需要浏览器。
```

---

## 二、推荐路径：一键复现（约 2 分钟）

```bash
python tools/reproduce_all.py
```

实测输出（尾段）：

```
✅ 环境自检（依赖 / 配置 / 数据目录）（10.2s）
✅ 数据契约导出（6 个 JSON Schema）（0.8s）
✅ 技能注册表：绑定校验（声明的工具/提示词真实存在）（0.9s）
✅ 提示词注册表（版本 + SHA256 指纹）（0.7s）
✅ 工具注册表（副作用 / 确定性声明）（0.9s）
✅ MCP 客户端自检（真实子进程 + 完整协议往返）（0.9s）
✅ 端到端跑通 R1 → R4 → R5（mock 模型，零成本）（5.8s）
✅ 产物完整性（34 个产物的哈希与 manifest 一致）（0.7s）
✅ 离线回放并逐字节比对（不调用任何模型）（4.6s）
✅ 生成人类可读审计报告（过程可追溯）（0.8s）
✅ Web 演示台渲染核验（进程内，不占端口）（4.8s）
✅ 测试套件（全量）

==============================================================
汇总：✅ 全部通过（12/12）
==============================================================
```

三个开关：

```bash
python tools/reproduce_all.py --quick          # 跳过测试套件（约 35 秒）
python tools/reproduce_all.py --run-id my      # 换 run 名（默认 demo）
python tools/reproduce_all.py --json out.json  # 结果落盘，便于写进报告
```

> 为什么要这个脚本：赛题技术要求第 6、7 条要求「确保评审方能够在指定环境中
> 复现核心任务流程和输出结果」。这条要求如果只靠 README 里散落的十几条命令，
> 评审就得逐条照着敲 —— 任何一条敲错都会得出「跑不起来」的结论。
> 脚本里的每一步都带判据，不认「返回码为 0 就算过」：
> 例如可复现性必须看到输出里写明「可复现」，因为「退出码 0 但结论是不可复现」
> 是最坏的情况 —— 它给出的是虚假的安心。

---

## 三、逐条手工复现（对应赛题技术要求）

### 3.1 端到端跑通三阶段（赛题选题 1 / 4 / 5）

```bash
python -m agent.cli run full --mock --run-id demo
```

`--mock` 用合成样本 + 确定性假模型，零成本、零联网，但走的是与他人无异的
真实编排代码：11 个阶段、同一套断言、同一份产物清单。

产物（`runs/demo/`，共 34 个文件）：

| 产物 | 它证明什么 |
|---|---|
| `facts/facts.json`、`facts/facts_table.json` | 事实库：每条数字带原文锚点、置信度、来源层 |
| `facts/evidence.html` | 溯源核验页（浏览器打开，左图右文） |
| `facts/facts.xlsx` | 结构化交付物 |
| `valuation/valuation_report.md` | 估值报告（假设 / 区间 / 敏感性 / 适用范围） |
| `valuation/charts/*.png` | 敏感性热力图、龙卷风图、区间图、FCFF 瀑布图 |
| `valuation/valuation_model.xlsx` | 估值模型（可点开看每一格公式） |
| `audit/audit_report.md`、`audit/annotated_draft.html` | 纠错核查报告 + 批注版草稿 |
| `audit/revision_table.xlsx` | 修订对照表（改哪个数、改成多少、依据） |
| `trace.jsonl` | 文件访问 / 工具调用 / 模型调用 / 断言 / 逐页路由 / 阶段（`file_access` 逐条带相对路径 + 读/写/列目录 + sha256 + 所属阶段） |
| `audit.md` | 上面那份 trace 的人类可读版 |

### 3.2 可复现性：逐字节（赛题「运行结果可复现」）

```bash
python -m agent.cli verify demo          # 产物哈希 vs manifest
python -m agent.cli replay demo --strict # 离线重跑并逐字节比对
```

`verify` 实测输出开头：

```
# 复现报告 — run `demo`

**结论：✅ 可复现**
```

> 口径必须说清：可复现性有输入层 / 环境层 / 产物层三层含义。
> 本项目校验的是产物层：同一个 run 目录下 34 个产物的 SHA256
> 与 manifest 记录一致。`manifest.json` 里同时记着输入材料哈希、
> 配置指纹（`agent_config_hash`）与提示词指纹，因此
> 改了配置或提示词就会被判为不可复现 —— 这是设计如此（见第七节第 2 条）。

### 3.3 过程可追溯（赛题「完整记录文件访问、工具调用、计算过程」）

```bash
python -m agent.cli audit demo
```

生成 `runs/demo/audit.md`，七节：运行概况 / 输入与配置指纹 / 过程统计 /
可复现性核验 / 逐页路由决策 / 勾稽断言明细 / 完整事件时间线。

其中勾稽断言按三态呈现，这是本条要求的直接体现：

```
- 勾稽断言：**26** 条 —— 通过 11 / 失败 **0** / 未校验（缺数据）15
```

四类事件里，「文件访问」在 `audit.md` 的「过程统计」与「完整事件时间线」两节：

```
- 文件访问：**71** 次（涉及 38 个不同文件）
```

> `runs/demo` 已于 2026-10-17 按新代码重跑，因此上面四类都有非零读数
> （`runs/` 里更早的那几份 `web-*` 没有 —— 它们产生于文件访问接入之前，
> 「运行与复现」页对它们照实报 0）。判据由
> `tests/test_harness.py::test_end_to_end_full_pipeline_mock` 钉住：
> 不只要求"有"，还要求盘上每个产物都有对应的 `write` 记录。

> 「未校验」= 合成样本缺现金流、利润表明细，本次无法独立重算。
> 它与「失败」（校验过且不一致）是两件完全不同的事 ——
> 把前者算成后者会虚报失败数，把后者洗成前者会掩盖真问题。
> `tests/test_harness.py::test_assertion_summary_separates_skipped_from_failed`
> 与 `test_assertion_bool_shorthand_still_records_failure` 一正一反钉住这两侧。

### 3.4 治理面：赛题点名的核心模块逐一点开

```bash
python -m agent.cli tools            # 17 个工具 / 7 个模块，含副作用与确定性声明
python -m agent.cli prompts          # 5 个提示词，带版本与 SHA256 指纹
python -m agent.cli skills           # 3 个领域技能，**校验其绑定真实存在**
python -m agent.cli mcp --self-test  # MCP：真实子进程 + 握手/发现/调用/错误路径
python -m agent.cli contracts        # 导出 6 个 JSON Schema 契约
```

`mcp --self-test` 实测（摘录）：

```
① initialize  ✅  对端 demo-tools 1.0.0
② tools/list  ✅  发现 3 个工具
③ tools/call  逐个调用并校验结果
④ 出错路径   ✅ 无法解析的输入被如实报错
⑤ 未知工具   ✅ 协议层错误被正确抛出：-32602: 未知工具：no.such.tool
⑥ 接入工具注册表  ✅ 注册 3 个 MCP 工具（副作用/确定性声明为最保守值）
⑦ 追踪记录   ✅ trace.jsonl 共 9 条工具调用事件
✅ MCP 客户端自检全部通过（握手 / 发现 / 调用 / 错误路径 / 注册表接入 / 追踪）
```

### 3.5 Web 演示台

```bash
python -m web.app                    # http://127.0.0.1:8000
python tools/web_smoke.py --in-process   # 进程内渲染核验，不占端口

# 选题 1 与选题 4/5 用的**不是同一批材料**，各读各的 run：
#   通知 p2 点名选题 1 的材料是「非标准化公告、扫描文件和复杂表格」
#   通知 p3 要求选题 4/5「根据财务数据、经营假设和可比公司信息」
# 所以 --r1-run 给选题 1 那两页（/facts、/extract）指定公告 run，
# --run 给选题 4/5 指定定期报告 run。不给 --r1-run 则两处都用 --run。
python -m web.app --run web-20260927-123902 --r1-run ann77-20260927
```

> 这两个 run 名是**本工作目录**里的示例，`runs/` 不随源码包提交。
> 新解压的副本先跑一次 `python tools/reproduce_all.py`（它会生成 `runs/demo`），
> 再用 `python -m web.app --run demo` 即可 —— 不加参数也能起，只是没有默认 run。
> 指定了一个没跑完的 run（没有 `manifest.json`）时，对应的页面会显示
> 「状态：未完成」而不是硬凑一份半成品，这是有意的。

九个页面 = 一个写入口 + 八个只读展示页（含总览页 `/`）。**路由条数与从前一致**，
变的是导航的排法：顶栏按赛题分 **5 组** —— 总览 / 运行 / R1 提取 / R4 估值 / R5 核查。
估值与核查两页内部再用页内标签切子视图（`?tab=assumptions`、`?tab=report`、
`?tab=draft`），**无参访问仍是各自的主视图**，旧链接照旧可用。

* `/analyze`（写入口）：把要分析的 PDF 放进 `inbox/raw/`，在页面上点一次
  「开始分析」，它用真实模型跑完整的 R1→R4→R5。页面显示 11 个阶段的实时进度、
  token/费用与运行日志末尾；跑完给出各选题视图的直达链接。
  它与命令行 `agent run full --data inbox --run-id <名字>` 是同一条子进程路径，
  Web 端不另起一套执行逻辑。
* **控制台 `python -m web.panel`（另一个应用，一页四标签 + 常驻右栏）**：
  **运行 / R1 提取 / R4 估值 / R5 核查**。运行页四块：① 传入数据 ② 运行
  （创建 run task 并跑完三个任务：R1→R4→R5，11 个阶段）③ 流水线状态 ④ 事件；
  三个结果标签页形状相同 —— 这块跑了哪几步（用时 / 模型调用 / 花费 / 计数）、
  跑出了什么、产物在哪。它只做三件事 —— 起一次流水线、看运行过程、看运行结果 ——
  因此**页面里不放模型写下的推理长文**（假设的 `basis`、核查的 `reason` 一律不进正文，
  它们属于 `valuation_report.md` / `annotated_draft.html` 这些产物）。
  **右栏**（四个标签页上都在）是第四件事：模型与工具的**原文**流 —— 提示词、
  模型回答、工具入参、工具返回值。数据来自 `runs/<id>/agent_log.jsonl`，
  由 `Tracer.journal()` 在每次调用时追加；`trace.jsonl` 按契约只记载荷哈希，
  画不出这一栏。老 run 没有这份记录，则退到回放缓存并**明说**「只有回答、
  没有提示词与工具调用」。`离线自检` 的 run 在这一栏里与真实 run 无从分辨，
  所以页眉挂 `离线自检` 药丸、右栏再写一行 —— 判断依据是 manifest 的 `provider`，
  运行中的 run 读 `checkpoint.json`（manifest 跑完才落盘，而运行中正是右栏最在被看的时刻）。
  过程区轮询 `/api/trace`（按字节偏移增量读 `trace.jsonl`），**正在跑的 run 也能看**：
  没有 `manifest.json` 就是「还在写」，而展示站的只读页会把这种 run 判成未完成。
  与展示站共用 `artifacts.py` / `analyze.py` 底座，样式与模板各自独立
  （`panel.css` 不 `@import` `app.css`）。
* 只读展示页：`/`（总览）`/library` `/extract` `/facts` `/valuation` `/audit`
  `/runs` `/process` —— 它们只读 `runs/` 下的产物，不做任何二次加工，
  所以页面上看到的与命令行跑出来的是同一份数据。
  `/process` 是单文件限量预览（≤25 页、OCR ≤8 页、VLM 可选），不落库。
* 每一页右侧还有一个 **Agent 运行轨迹抽屉**：直接读该 run 的 `trace.jsonl`
  （按字节偏移增量轮询，只消费到最后一个完整换行），运行中每 2 秒刷新、
  结束后自动停。里面是工具调用、阶段推进、勾稽断言、模型调用与路由决策 ——
  「这一刻 agent 在调什么」当场可看。抽屉不经 `resolve_run`，所以**运行中的
  run（还没有 `manifest.json`）也能看**。

> 注意：**`/analyze` 会花钱**：它调真实模型，表单里那个费用确认勾选框是必需的 ——
> 没勾选会被服务端拒绝（有测试守着）。页面上的时间/费用估算按
> 185 页 ≈18 分钟 ≈¥0.96 的实测值折算。
>
> 注意：**同一时刻只允许一个分析任务**，且同一个 run 名只允许一个写者
> （两个进程写同一个 run 会把缓存写脏，回放会报假的"不可复现"）。
> 判定"是否还在跑"用的是证据优先：产物（`manifest.json`）> 进程 > trace 是否还在写 ——
> 因为实测在沙箱里 `tasklist` 会返回 `ERROR: Access denied`，只看 pid 会误判。

> 在受限环境（沙箱 / CI）里用 `--in-process`：实测新进程绑定端口会被拒绝
> （`WinError 10013`），而进程内渲染验的是同一套模板与同一套产物读取代码。

### 3.6 测试套件

```bash
python -m pytest -q
```

实测：`736 passed, 2 skipped`
（两条 skip 都在 `tests/test_web.py`：一条需要外部指定比对 run，
一条所在 run 只有 5 页、窗口式分页无意义。skip 不是 fail ——
它们各自写明了跳过原因，见该文件的 skip 断言。

装了 node 时是 2 条 skip；没装则 `tests/test_panel_js.py` 那两条也 skip
（共 4 条），原因同样会印出来 —— 那两条是唯一**真的执行页面 JavaScript**
的测试，跳过时「页面能不能用」确实没被验证过。）

> **耗时为什么只给区间。** 同一天四次跑分别是 170.65s / 177.60s / 387.99s / 413.46s
> —— 差距全部来自机器当时有没有别的负载（空闲约 3 分钟，忙时约 7 分钟）。这是本表里
> 唯一随负载大幅浮动的数，**别拿它当性能判据**（同机单次全流程 `run full --mock`
> 的墙钟稳定在 9.3–9.5s，可作参照）。顺带把一个可能的疑问先答掉：本轮新接入的
> `file_access` 逐条记录**不是**原因 —— 实测每次 run 只多花约 0.02s
> （71 条事件、723 KB 全量 sha256），见 `docs/VALIDATION_INDEX.md` 9.7。

---

## 四、怎么判断我们没有「自己说自己好」

四条可当场执行的反注入检查 —— 用来证明体检本身会失败：

| 检查 | 命令 | 它防的是什么 |
|---|---|---|
| 文档诚信 | `python tools/doc_truth_check.py` | 往文档里注入一个不存在的模块名，三条静态检查必须报错 |
| 文档路径 | `python -m pytest tests/test_static_health.py -q -k missing_files` | 文档里点名的仓库文件必须真实存在（评审最爱照着路径点开） |
| 静态体检 | `python -m pytest tests/test_static_health.py -q` | 未定义名字、类体被切开、独立脚本未切 UTF-8 控制台编码 |
| 三态断言 | `python -m pytest tests/test_harness.py -q -k assertion` | 一正一反：`skipped` 不算失败，`fail` 必须算失败 |

> 每一条都带注入验证：构造一个必须被抓的反例（不存在的模块名 / 不存在的文件路径 /
> 未切编码的脚本 / 把 `fail` 说成 `skipped`）。一条永远通过的检查等于没有检查。

---

## 五、需要真实模型的路径（可选）

> 这一节不在一键复现里：它要联网、要花钱、要 20 分钟以上，
> 且结果依赖外部模型服务的当时状态。放在这里是为了可选择性，不是为了省事。

```bash
# ① 配置密钥（国产模型 DeepSeek）
cp .env.example .env      # 或直接设环境变量
# PowerShell:  $env:DEEPSEEK_API_KEY="sk-..."
# bash:        export DEEPSEEK_API_KEY="sk-..."

# ② 真实年报全链路（4 份年报 / 943 页）
python -m agent.cli run r1_extract --data data/real/定期报告 --run-id annual41-seg

# ③ 与**人工逐页核对**的真值比对 —— 这是「抽取准不准」的直接证据
python tools/verify_truth.py runs/annual41-seg

# ④ 估值（消费真实可比公司快照与市场利率缓存）
python -m agent.cli run r4_valuation --facts-run annual41-seg --run-id r4-seg

# ⑤ 可复现性（含真实 VLM 调用）
python -m agent.cli replay runs/r4-seg --strict
```

实测成本与时长（README 记载，185 页年报）：约 18 分钟 / ¥0.96，
其中大头是第三层 VLM 整页扫描与单元格裁决。四份年报合计约 1 小时 / 数元。

### 5.1 关于 `runs/` 里那些「文档提到、磁盘上没有」的运行

`runs/` 不入版本库（`.gitignore` 明文：体积大且含受控材料信息，
复现包单独打包提交）。因此在一台全新的克隆上，
文档里引用的旗舰运行都不会存在 —— 这是设计如此，不是缺失。

| 文档里出现的 run | 本机是否存在 | 怎么得到它 |
|---|---|---|
| `demo` / `repro-check` | 一键复现脚本会创建 | `python tools/reproduce_all.py` |
| `annual41-seg`（943 页 / 1050 条事实） | 需重建 | 上面第 ② 条命令（约 1 小时 / 数元） |
| `annual23`、`annual37-yrseq`（中间轮次） | 需重建 | 同上，换 `--run-id` |
| `r4-seg`（R4 真实运行） | 需重建 | 上面第 ④ 条命令 |
| `r5-eval-final2`（R5 评测） | 需重建 | `docs/VALIDATION_INDEX.md` 第六节第 ⑥ 条 |
| `runs/annual16/18/19`、`runs/ab-no4`、`runs/web-2026092*`、`runs/r5-eval-annual16` | 已不保留 | 历史真实运行快照（`partial` 表示当时是离线/降级跑）。本工作目录只留 `runs/demo` 这类可一条命令重建的样例；这些快照不随源码包提交，结论以 `docs/VALIDATION_ROUND*.md` 的逐轮记录为准 |

> 结论怎么读：文档里的数字（1050 条事实、730/987 张表、
> DCF ¥14.0394、P 1.0 / F1 0.6667 ……）都在
> `docs/VALIDATION_ROUND*.md` 与 `docs/VALIDATION_INDEX.md` 中逐轮记录了
> 得出它的命令、当时的输出、以及被推翻的中间结论。
> 要独立复核这些数字，需要按上表重建对应的 run（要 API Key）。
> 我们不去伪造一个「随时可复现」的印象：能零依赖复现的（编排、产物契约、
> 可复现性机制、治理面、Web、测试）在这份文档前三节；
> 需要真实模型才能复现的（抽取准确率、估值数字、纠错指标）在第五节。

---

## 六、交付物清单

| 类别 | 位置 |
|---|---|
| 作品说明（总入口） | `README.md` |
| 赛题对照与差距 | `docs/COMPETITION_FIT.md` |
| 评审复现指引 | 本文档 |
| 技术验证总览（19 轮） | `docs/VALIDATION_INDEX.md` |
| 分轮验证记录 | `docs/VALIDATION_ROUND*.md`、`docs/P0_VALIDATION.md` |
| 设计取舍与事故复盘 | `docs/DESIGN_NOTES.md` |
| 第三方依赖声明 | `THIRD_PARTY.md` |
| 数据契约（JSON Schema） | `configs/schemas/*.json`（`agent contracts` 可重新导出） |
| 一键复现 | `tools/reproduce_all.py` |
| 现场演示（两阶段） | `tools/demo_run.py` + `demo_materials/` |

---

## 七、常见问题（都是实测踩过的）

1. 控制台报 `UnicodeEncodeError: 'gbk' codec can't encode character`
   Windows 默认 GBK 控制台。所有独立脚本与 CLI 入口都会调用
   `agent.console.enable_utf8_output()` 自动切到 UTF-8；
   若你自己写脚本调本项目，记得也调一次。
   （这条约定曾经有 16 个脚本没照做，其中就包括 README 让评审跑的
   `tools/web_smoke.py` —— 现在由
   `tests/test_static_health.py::test_standalone_scripts_bootstrap_utf8_output` 守着。）

2. `replay --strict` 报「不可复现」
   先看报告里的不一致明细，最常见的三种（都记录在案）：
   改了 `configs/`（配置指纹变了）、改了 `domain/prompts/`（提示词指纹变了）、
   同一个 run 名被两个进程同时写（缓存被穿插追加）。
   修复方式：换一个新 run 名重跑，不要原地纠缠。

3. R4 报 `CompsDataError` 并退出
   这是有意为之的：缺 `data/comps/*.json` 或 `data/market/*.json` 时，
   宁可显式失败，也不让报告里出现看起来正常、实际是编造的倍数。
   这两份是公开数据且应随包提交（`.gitignore` 里专门为它们开了白名单）。

4. 没有 OCR 引擎怎么办
   合成样本与纯文本材料不需要 OCR：无扫描页也无表格页时该阶段直接跳过。
   跑真实年报则必须先 `pip install rapidocr-onnxruntime`（纯 CPU）——
   只要材料含表格页（`D_table`），`pipelines/r1_extract/extract_ocr.py` 的
   `_count_hard_ocr_pages` 就把它们算作硬依赖，未装引擎时该阶段显式失败
   并给出安装指引，不会静默降级。

5. Web 起不来 / 端口被占
   用 `python tools/web_smoke.py --in-process` 做渲染核验（不占端口）；
   或 `python -m web.app --port 8080`。

---

## 八、我们不声称什么

- 不声称抽取准确率 100%：`tools/verify_truth.py` 的比对里仍有若干条不符
  （集中在 601857 的 2024A，根因是已知限制第 2 条）。
- 不声称估值是投资建议：报告 `applicability` 字段写明
  「适用于投研分析演示与方法论验证」。
- 不声称已解决全部问题：`docs/VALIDATION_INDEX.md` 第五节列了 8 条未解决项，
  每条都有根因与量化影响（含「阶段间仍有内存态」这条结构性缺陷）。
- 不声称 `--live` 现场那一秒重新做了解析：它跳过已完成的阶段
  （输出会打印「断点续跑：已完成 N 个阶段」）。要看当场重新解析，用 Web 的 `/process`。
- 不声称可比公司数据代表实时行情：快照带抓取时间戳，不会自动刷新。

---

*配套文档：`README.md`（作品说明）、`docs/COMPETITION_FIT.md`（赛题对照）、
`docs/VALIDATION_INDEX.md`（验证总览）、`THIRD_PARTY.md`（第三方声明）。*

