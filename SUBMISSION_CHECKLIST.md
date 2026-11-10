# 提交清单（初赛 / 决赛）

> 依据：《关于举办 2026 年北京市大学生金融人工智能竞赛的通知》P6–P7。
> 本文件回答一个问题：现在要交什么、怎么生成、交之前检查什么。
> 配套：`deliverables/项目计划书.md`（+ PDF）、`deliverables/视频脚本_5分钟.md`、
> `docs/REVIEWER.md`（评审复现指引）。

---

## 一、赛程与硬节点

| 阶段 | 时间 | 交付物 | 状态 |
|---|---|---|---|
| 报名 | 9/10 – 10/11 | — | 确认本队已报名 |
| **作品提交截止** | **10/18** | 项目计划书（PDF）+ 项目介绍视频（MP4 ≤5 分钟 / ≤500MB） | 进行中 |
| 初赛评审 | 10/18 – 11 月中旬 | — | — |
| 决赛 | 11 月底 | 最终报告书 + 系统原型 + 完整源代码及运行说明 + 现场可实时处理的样例数据 | 计划中 |

团队要求：**3–5 人**，每人只能参加 1 支队伍；晋级决赛须配 1 名指导老师。

---

## 二、初赛交付物

### 2.1 项目计划书（PDF）

| 项 | 说明 |
|---|---|
| 源文件 | `deliverables/项目计划书.md`（唯一真源，改内容改这里） |
| 生成命令 | `python tools/make_plan_pdf.py` |
| 产物 | `deliverables/项目计划书.pdf`（实测 17 页 / 约 587 KB） |
| 为什么是脚本生成 | 交付物也应当可复现：改一个字重新生成，不会出现「PDF 与源文件不一致、没人知道哪份是新版」 |

**内容结构**（10 节）：项目概述 / 赛题理解与选题说明 / 系统方案 /
关键创新点 / 验证与实测数据 / 工程实现与可复现性 / 应用价值 /
团队分工与进度 / 风险与合规 / 已知限制。

> 提交前必须补：第八节「团队分工」里的队员姓名、学校、指导老师
> （计划书里标了 `待填写`）。这类信息只能由人来填，不要留占位符提交。

### 2.2 项目介绍视频（MP4，≤5 分钟，≤500MB）

| 项 | 说明 |
|---|---|
| 脚本 | `deliverables/视频脚本_5分钟.md`（分镜表 + 口播稿 + 录制清单 + 导出规范） |
| 时长编排 | 4 分 50 秒（留 10 秒余量） |
| 录制前提 | 先 `python tools/demo_run.py --data demo_materials --warm --run-id live` 预热 |
| 文件命名 | `队名_项目名称_项目介绍视频.mp4` |

**导出后自检三条**（脚本第五节有详版）：
① 全程无报错红字、无 API Key 提示；
② `replay` 那段画面里确实出现 `可复现` 字样（不要只靠解说）；
③ 视频里出现的每个数字都能在仓库文档里找到出处。

---

## 三、决赛交付物（现在就可以先备好）

| 交付物 | 对应文件 / 命令 | 状态 |
|---|---|---|
| 最终报告书 | `README.md` + `docs/*` + 本清单，可另出一份 PDF | 素材齐备 |
| 系统原型 | `python -m web.app`（Web：9 条路由 = `/analyze` 写入口 + 8 个只读展示页，含总览页 `/`；导航按赛题分 5 组，每页带 Agent 轨迹抽屉）+ `python -m agent.cli ...`（CLI，赛题允许二选一） | 已就绪 |
| 完整源代码 | `python tools/make_submission_package.py` | 已可一键打包 |
| 运行说明 | `docs/REVIEWER.md`（含一键复现脚本 `tools/reproduce_all.py`） | 已就绪 |
| 现场可实时处理的样例数据 | `data/samples/`（合成样本）+ `demo_materials/`（真实公开材料） | 已就绪 |

---

## 四、源码包怎么打

```bash
python tools/make_submission_package.py            # 完整包（含 77 份真实公告语料）
python tools/make_submission_package.py --no-samples   # 精简包（约 25 MB）
python tools/make_submission_package.py --list-only    # 只列清单，不打包
```

实测（完整包）：**335 个文件 / 72.9 MB → 压缩后 56.7 MB**，解压后根目录名 `金融投研智能体/`。

> **打完包要验一次**：解压到任意空目录，跑 `python tools/reproduce_all.py`，
> 期望 `汇总：✅ 全部通过（12/12）`。仓库里绿不等于包里绿 —— 包里 `runs/` 是空的，
> 凡是依赖真实 run 的检查（或数据相关的探针）都会换一种表现。
> 2026-10-16 就是这么发现「web 步 `✗ 双路互证`」的（详见 `docs/VALIDATION_INDEX.md` 9.6）。

**包含**（白名单，写在 `tools/make_submission_package.py` 里，可复核）：

| 目录 | 内容 | 为什么必须在 |
|---|---|---|
| `agent/` | 编排框架、模型网关、工具注册表、提示词注册表、Skill 注册表、MCP 客户端、证据库、追踪与复现 | 赛题点名的核心模块 |
| `domain/` | 提示词、技能（SKILL.md）、文档/财务/表格/OCR/VLM 工具 | 提示词与技能不在代码里就复现不了 |
| `pipelines/` | R1 / R4 / R5 / full | 三条流水线本体 |
| `web/` | 演示台（决赛要求的原型） | 现场演示 |
| `tools/` | 复现与诊断脚本（含 `reproduce_all.py`、`demo_run.py`） | 评审入口 |
| `tests/` | 全量测试套件 | 可验证性证据 |
| `configs/` | 配置 + 6 个 JSON Schema 契约 | 缺了跑不起来 |
| `data/comps`、`data/market`、`data/samples`、`data/r5_eval`、`data/raw/sample_*.pdf` | 公开可比公司快照 / 市场利率缓存 / 合成样本 / R5 评测集 / 测试用的合成材料 | R4 缺 comps 会显式失败（有意设计） |
| `deliverables/` | 计划书与视频脚本 | 初赛交付物 |
| 根文件 | `README.md`、`THIRD_PARTY.md`、`requirements.txt`、`pytest.ini`、`.env.example`、本清单 | 运行说明与第三方声明 |

**不包含**（自动排除，且有**密钥扫描**兜底）：

* `.env`（API Key）、`credentials.json`、私钥 —— 打包前扫描，命中即中止；
* `runs/`（体积大且含受控材料快照）、`_tmp/`、`_*/`、`__pycache__/`、`.mplcache/`、
  `pytest-cache-files-*/`、`agent/sample/_scratch/`（中途下载的散件）；
* `data/drafts/`、`data/real/`、`data/golden/`（受控材料，版权与合规考虑）；
  例外：`data/raw/sample_*.pdf` 两份**合成**样本（`tools/make_sample_data.py`
  生成，35 KB）随包 —— 9 条测试拿它们当输入，不带就一律 SKIPPED；
  `data/raw/` 下**别的** PDF（真实年报）仍然不进包；
* `*.mp4`、`*.zip`、`*.pyc`、日志。

> 一条实测踩出来的经验：密钥扫描是全文正则，它不认识「这是测试用的假
> Key」。第一次写打包测试时，测试文件里直接写了 `sk-` 开头的字面量探针，
> 结果打包脚本自己被自己的测试文件拦下（安全检查中止）。
> 现在测试里的探针由字符串拼接而成 —— 兜底检查就该严格到会误伤自己。
> 如果你的代码里确实需要示例 Key，请同样避开 `sk-` + 长串的字面量写法。

---

## 五、提交前自检清单（照着跑一遍）

| # | 命令 | 期望 | 说明 |
|---|---|---|---|
| 1 | `python -m agent.cli doctor` | `✅ 必需项全部就绪` | 依赖 / 配置 / 数据目录 |
| 2 | `python tools/reproduce_all.py` | `汇总：全部通过（12/12）` | 零依赖，约 2 分钟 |
| 3 | `python -m pytest -q` | `736 passed, 2 skipped` | 测试套件 |
| 4 | `python tools/make_plan_pdf.py` | `✅ 已生成 …项目计划书.pdf` | 计划书与源文件同步 |
| 5 | `python tools/make_submission_package.py` | 安全检查通过 + 清单 | 源码包 |
| 6 | 打开 `deliverables/项目计划书.pdf` | 无占位符、无乱码 | 人工过一遍 |
| 7 | 按 `视频脚本` 录一遍 | 时长 ≤5:00、无报错画面 | 人工过一遍 |
| 8 | `git status`（若有版本库） | 不含 `.env`、不含 `runs/` | 防手滑提交密钥 |

> 第 2 条是核心：它把「赛题要求评审能复现」变成一条能当场跑出绿字的命令。
> 如果它红了，先修它，再提交。

---

## 六、合规与声明（会被追问的点）

| 问题 | 我们的回答 | 证据位置 |
|---|---|---|
| 用了哪些第三方？许可证？ | 逐条列明名称/版本/来源/许可证/使用范围，含 PyMuPDF AGPL-3.0 的主动披露 | `THIRD_PARTY.md` |
| 哪些是自研？ | 编排框架、Tool、Prompt、Skill、MCP 客户端、数据处理、追踪与复现全部自研，不用第三方编排框架 | `agent/`、`THIRD_PARTY.md` |
| 数据从哪来？ | 巨潮资讯网等公开披露材料；合成样本由 `tools/make_sample_data.py` 生成 | `docs/REVIEWER.md` 第五节 |
| 受控材料是否随包提交？ | 不随包（版权与合规），只提交公开数据快照与合成样本 | `.gitignore`、上面的「不包含」清单 |
| 是否只提交了对话记录？ | 否。产物是结构化数据、表格与报告 | 赛题技术要求第 2 条：`facts.json` / `valuation.xlsx` / 估值报告 / 核查报告 / 批注 HTML |
| 用了哪个大模型？ | 国产模型 DeepSeek（`deepseek-flash`），用途与角色分工可查 | `configs/models.yaml`、`configs/routing.yaml` |

---

## 七、版本记录

| 日期 | 变更 | 生成方式 |
|---|---|---|
| 2026-09-25 | 新增本清单、`tools/make_submission_package.py`、`tools/make_plan_pdf.py`、`tools/reproduce_all.py`、`docs/REVIEWER.md`、计划书与视频脚本 | 见各文件头部注释 |
| 2026-10-10 | 订正 `data/r5_eval/truth.py` 里 000722 资产负债表的口径（母公司 → 合并），随之重跑 R5 评测与 `verify_truth`（命中 10 / 舍入相符 6 / 不符 2）；按实测更新 README、`docs/`、`THIRD_PARTY.md` 中过期的数字与结论；`tools/web_smoke.py` 补上总览页检查（9 页全覆盖）；重新生成计划书 PDF 与源码包 | `python tools/make_plan_pdf.py`、`python tools/make_submission_package.py` |
| 2026-10-11 | 修 Web 演示台示例命令：README 与 `docs/REVIEWER.md` 里 `--r1-run ann-20260927` 指向的是一个没跑完的 run（无 `manifest.json`，停在该 run 的 `ingest`/`extract_text` 之后），按它启动后 `/facts`、`/extract` 只会显示「状态：未完成」；改为跑完的公告 run `ann77-20260927`，并补上「示例 run 名只存在于本工作目录，`runs/` 不随包提交」的说明 | 手工订正；重新生成源码包 |
| 2026-10-17 | 「文件访问」从「只建了通道」接到编排层：`RunContext.guard()` 逐点位记录（路径记相对路径、模式读/写/列目录、sha256、所属业务阶段），C 扩展与第三方库的落盘用 `FileGuard.record()` 只登记不代劳 I/O（以免产物多一个字节导致 replay 判不可复现）；删掉 `table_facts.py` 里最后一处绕开 guard 的 `configs/metrics.yaml` 读；`runs/demo` 按新代码重跑（28 个产物逐字节不变，6 个含运行起始时间的变），`verify` / `replay --strict` 仍判可复现；README / `docs/COMPETITION_FIT.md` / `docs/REVIEWER.md` / `docs/VALIDATION_INDEX.md` 9.7 / 项目计划书里所有「未接入」「恒为 0」的标记一并删除，被证伪的旧断言钉进 `tools/web_pages_check.py` 的 `FORBIDDEN`；测试数按实测同步为 735 | `python -m agent.cli run full --mock --run-id demo`、`python tools/make_plan_pdf.py` |
| 2026-10-17 | 打包核对时发现仓库缺了 `agent/sample/`（77 份已分类真实公告，47 MB）—— 清单记的完整包是 335 文件 / 72.9 MB，实测只有 258 / 26.3 MB，差额正好是这 77 份；已从留存副本恢复并重打。同时给 `run` 加了守卫：目标 run 目录里已有产物且没带 `--resume` 时**拒绝执行**（实测重跑会让 trace 从 146 行变 292 行、146 个 `seq` 全部重复、`verify` 判不可复现），`tools/demo_run.py --warm` 撞上已有目录时自动改走 `--resume`；守卫自带 1 条用例 `test_run_refuses_to_append_to_an_existing_run`，测试数随之由 735 订正为 736（实测 736 passed, 2 skipped / 收集 738 / 184.62s） | `python tools/make_submission_package.py` |

> 交付物的版本一致性靠两条命令保证：
> `python tools/make_plan_pdf.py`（计划书）与
> `python tools/make_submission_package.py`（源码包）。
> 两份产物都由脚本从仓库当前状态生成，不存在"手工导出后忘了更新"的可能。

