# 第十八轮验证 —— 现场演示流程：把材料丢进一个文件夹，当场跑

> 目标：把「赛前预热 → 现场秒级运行」这条演示流程跑通并验证，
> 不是只在文档里承诺。
> 查下去发现：这条流程原本必然报「不可复现」，而且原因有三个，
> 分别藏在调度器的运行身份、产物清单的收口方式、以及缓存被多写者污染上。

---

## 一、结论速览

| 项 | 结果 |
|---|---|
| 演示流程 | `--warm`（实测 1101 秒 / 份、¥0.96）→ `--live --resume`（0.9–1.5 秒 / 0 token）→ `web.app --run <id>` |
| 材料目录约定 | `<目录>/raw/*.pdf`（R1）+ `drafts/*.md`（R5）+ `comps/*.json` 与 `market/*.json`（R4 必需）；仓库内已备 `demo_materials/` |
| 缺陷 1 | `--resume` 重新生成运行身份（`started_at`、`doc_limit`）→ 748 个产物全部哈希不符 |
| 缺陷 2 | `_finalize_manifest` 把 `audit.md`（非阶段产物）收进 `outputs` → 回放必然「未产出」 |
| 缺陷 3 | 两个 warm 进程写同一个 run 名 → 缓存被穿插追加（168 个键写了两次）→ 回放取错答案 |
| ★ 缺陷 4 | `--resume` 丢掉只活在内存里的状态：`extract_text` 注册进 `ctx.store` 的页面原文没了 → 表格路径找不到单位 → 事实 1050 → 0 条，退出码 0，不报错。受控复现 + 根因 + 修复 + 逐字节验收见第六节 |
| ★ 缺陷 5 | 空转的续跑把 `partial` 洗成 `success` → 复现报告丢掉「原始运行不完整，不一致是预期」那段说明，读者只看到孤零零的「不可复现」 |
| 修复后 | 五者都有回归测试钉住（`tests/test_replay_resume.py`，7 条） |
| 修复效果 | 缺陷 1+2 修掉后 `replay --strict` 从「748 项全不符」→ 744 项通过 / 4 项不符；缺陷 3 修掉后 `judge` 阶段逐字节可回放；缺陷 4 修掉后模拟「跳过 extract_text 的续跑」五项产物逐字节回到完整跑的值 |
| 测试 | 519 passed（新增 7 条） |

---

## 二、缺陷 1：续跑重新生成了运行身份

`build_context` 每次调用都新建 manifest：`started_at=utcnow()`；
CLI 随后按本次命令行重记 `doc_limit`。于是：

```
13:58:40  --warm --limit 1 跑完   产物 built_at=13:58:40   manifest.started_at=13:58:40
14:06:32  --live --resume（没带 --limit）
          → manifest 被改写成 started_at=14:06:32、doc_limit=None
          → 盘上产物**没变**（阶段被跳过），但描述它们的那份元数据变了
```

`replay` 从 manifest 读 `started_at` 来重建全部时间字段，于是：

```
- pages/index.json：原始 d737524c9a60… 回放 bfa096155b26…
- facts/facts.json：原始 517d69ed3236… 回放 9a69fde8290c…
…（748 项里几乎每一项都不同）
```

这不是"运行不可复现"，是"续跑把这一次运行的身份证换了"。

修法（`agent/cli.py`）：`--resume` 时先读同一 run 目录里的 manifest，
继承它的 `started_at` / `doc_limit` / `upstream_facts_run` / `data_dir`。
判据的道理很简单：

> 盘上的产物是上一次按那些参数产出的；这次命令行有没有再写一遍
> `--limit` 与产物无关（阶段被跳过）。照命令行重记，等于用「这次的意图」
> 去描述「上次的产物」。

顺带加了两条保护：

* 续跑时若 `--data` 与上次记录的不一致 → 吼一声（跳过的阶段不会重读材料，
  盘上产物仍来自上次那个目录；个别阶段若真重跑，产物就是两个目录拼出来的）。
* 换了流水线还用同一个 run 名 → 提示「阶段名对不上，断点会被忽略（缓存仍复用）」。

实测效果：只把 `started_at` / `doc_limit` 手工还原成预热那一刻，
`replay --strict` 从「748 项几乎全不符」变成 744 项通过 / 4 项不符。

---

## 三、缺陷 2：产物清单把"别人写的文件"也收了

`_finalize_manifest` 会 rglob 整个 run 目录，把所有 json/md/xlsx/png 补进
`outputs`。这是为了收那些没在 `produces` 里声明的产物（裁图、Excel）。

但它不区分「本次运行写的」与「本来就在盘上的」。`audit.md` 由 CLI 在
`loop.run()` 之后才写，因此：

* 预热那一轮：`_finalize_manifest` 先跑 → `audit.md` 还不在 → 不入清单 ✓
* 续跑那一轮：`audit.md` 已在盘上 → 被扫进清单 ✗

而回放只跑阶段、不跑 CLI 尾部 → 永远产不出 `audit.md` → 报「回放未产出」。

修法：`Loop` 在开跑前对 run 目录做一次快照，`_finalize_manifest` 只补
本次新增的文件。这样两种方向都对：首次运行照收未声明的产物；
续跑不再误收遗留散件。

---

## 四、缺陷 3：一个 run 目录，两个写者

缓存文件里的统计（`runs/live/cache/llm.jsonl`）：

```
总行数 569 | 唯一键 401 | 被写两次以上的键 168
行距 min/中位/max：26 / 53 / 58        行距分布：(53,33) (54,25) (55,19) (51,15) …
重复键中 文本相同 69 / 不同 99
```

行距不为 0、也不定长，而集中在 26–58 —— 这不是"同一进程内的重试"，
而是两个进程各自按同样顺序调用、互相穿插着往同一个文件追加。
即：两个 `--warm` 同时在跑 `runs/live`。

后果有三层，每层都值得记住：

1. 缓存被写脏 → 回放取到的不是产物当时用的那个答案 →
   「不可复现」是假警报，而报告上看起来和真的不可复现一模一样。
2. `_finalize_manifest` 收进了另一个进程产出的裁图
   （回放这一轮根本不渲染它）→ 又一处「回放未产出」。
   这一条正是缺陷 2 的修法顺带解决的。
3. 计数与费用翻倍，两次运行的 manifest 又互相覆盖。

> 我在这里先走错了一步：一度把这 168 组当成"模型在 temperature=0 下
> 也不确定"的证据，还据此重写了缓存为「按第 n 次调用存槽位」。
> 用行距分布一看才明白：同一次调用不可能相隔 53 行被写第二次。
> 判据的杀伤半径没有在启用之前量出来 —— 这一轮的教训写在这里。
> 槽位版已回退；留下的是下面两件有实测支撑的改动。

修法：

* 缓存改为「首次写入者胜」（`agent/model/cache.py`）：已有槽不覆盖，
  只把「有人想覆盖」记进 `overwritten_slots`。载入时若同一键在文件里出现
  两次且内容不同，同样计数 —— 这是「这份缓存被多个写者动过」的唯一证据。
  为什么必须是"先写者胜"：先写者才是产物当时用的那个答案。
* 单写者标记（`agent/runtime/loop.py`）：`Loop` 写
  `runs/<run_id>/.lock`（记 pid 与时刻），发现锁被别人持有时当场警告。
  只警告不阻断 —— 判据是「pid 还活着吗」，误判的代价不该是"跑不起来"。

---

## 五、演示流程本身（实测）

材料目录（`demo_materials/`，仓库内可直接用）：

```
demo_materials/
  raw/     4 份年报（000722 / 002716 / 601857×2）   22.1 MB   → R1
  drafts/  3 份研究报告草稿（.md）                    ~30 KB   → R5
  comps/   2 份可比公司快照（601857 / 600519）                 → R4
  market/  市场利率缓存（rates_2026-09-17.json）               → R4
```

注意：**`comps/` 与 `market/` 是必须的，而且容易漏。** R4 从 `<材料目录>/comps/<主体>_<年份>A.json`
读可比公司快照、从 `<材料目录>/market/rates_*.json` 读无风险利率 ——
只放 PDF 的话，R4 会以 `CompsDataError` 显式失败（这是有意为之的：宁可失败，
也不让报告里出现看起来正常、实际是编造的倍数）。本轮的实测就是先漏了这两个目录。

| 步骤 | 命令 | 实测 |
|---|---|---|
| 只看材料能不能被发现 | `demo_run.py --data demo_materials --dry-run` | 立即，列出 PDF 与草稿数 |
| 赛前预热 | `demo_run.py --data demo_materials --warm` | 1101 秒 / 份（185 页年报、854 次模型调用、¥0.96） |
| 现场运行 | `demo_run.py --data <目录> --live` | 0.9–1.5 秒、0 token（在已完成的 run 上实测 0.9 秒） |
| 起台 | `python -m web.app --run <run_id>` | 8 页：`/`（总览）`/library` `/extract` `/facts` `/valuation` `/audit` `/runs` `/process` |
| 当场再解析一份新材料 | Web `/process` 拖入 | 608 ms（真解析，只预览、不落库） |

★ `--live` 那一刻没有重新解析，它跳过已完成阶段 —— 这一点在 README 里
也照实写明，不含糊成"秒级完成全流程解析"。

★ 演示时需切一次 run（不是因为缺陷，是数据条件）。选题 1 / 选题 4 用
`web-20260925-123442`（`--data inbox`，含 359 条事实 + 估值产物）；讲到选题 5 的
精确率 / 召回率 / F1 时，用顶栏「运行」选择器切到 `r5-eval-annual16`
（`--data data/r5_eval`，对标注集评测，P=1.0 / F1=0.6667）。

原因是 P/R/F1 必须有 golden 标注集才能计算 —— `pipelines/r5_audit/check.py:208-238`
的 `evaluated` 闸门只收「带标注」的草稿，合成草稿没有标注文件，`metrics` 就必然是
`null`。这是实际情况的反映，不是漏跑：`/audit` 页面会显示一条说明讲清原因，不是
显示 0 或留白。演示 run 的核查结论本身是完整的（10 条主张核查、2 条 confirmed 问题）。
详见 `docs/WEB_DESIGN_2026-09-25.md` §5.2。

★ 预热很慢，必须提前做。实测 185 页的一份年报在 `full` 流水线下要
18 分钟；601857 的两份年报分别是 10.7 MB / 9.0 MB，只会更久。
所以「现场演示」的正确姿态是：材料与 run 目录赛前就备好，
现场只演示「同一份材料、同一个 run ⇒ 产物一致、可续跑、可回放」。

---

## 六、★ 那个「0 条 / 297 条」的反常：查清了，是内存状态的问题

这是本轮最重要的发现，比演示流程本身重要得多。

### 6.1 现象

同一次运行、同一份输入，回放得到的事实库是 297 条而原次是 0 条，
而两次的 8 个 `channels/*.json` 逐字节相同：

```
=== channels 逐字节对照（原始 / 回放）===
  同  conflicts.json / ocr.json / ocr_skipped.json / table_adjudications.json
  同  tables.json / text_layer.json / vlm_scans.json / vlm_verdicts.json

原始  facts.json =   0 条事实
回放  facts.json = 297 条事实
```

### 6.2 受控复现（本地拷贝 + 已有缓存，几乎不花钱）

把 `annual41-seg` 复制一份，手工回退检查点只重跑 `judge` 起的阶段：

| | 事实数 | table_grid_candidates | table_grid_rejected_no_unit |
|---|---|---|---|
| 第一代（完整跑） | 1050 | 1186 | 0 |
| 第二代（续跑重跑） | 0 | 23 | 1590 |

整棵 run 目录逐文件比对：只有 7 个文件不同，全部是那两个阶段的产物；
所有输入（`channels/*`、`facts/documents.json`、`pages/*`）逐字节相同。
`table_adjudications.json` 更是只有 2 张表的 `report` 计数不同、`grid_after` 全同。

### 6.3 根因

`extract_text` 是唯一把「权威页面原文」注册进 `ctx.store` 的阶段，而
`ctx.store` 是纯内存对象。全项目只有一处 `set_page_text`：

```python
# pipelines/r1_extract/extract_text.py:88
ctx.store.set_page_text(d["doc_id"], page_no, raw_text)
```

而下游这样用它：

```python
# pipelines/r1_extract/build_facts.py:1090
page_text = ctx.store.page_text(doc_id, page)
unit_hint  = find_unit_declaration(page_text)      # ← 找「单位：万元」
# pipelines/r1_extract/table_facts.py:1481
if not unit:
    unit = unit_hint or find_unit_declaration(page_text)   # 仍为空 ⇒ unit = ""
    ...
    stats["skipped_no_unit"] += 1                  # ← 1590 行走到这里
```

于是：`--resume` 跳过 `extract_text` ⇒ 内存库是空的 ⇒ 每一行都按「无单位」
拒绝 ⇒ 交出 0 条事实，退出码 0，产物齐全，没有任何报错。
`ctx.ledger`（事实台账）同样是内存对象，跳过 `build_facts` 会让 `export`
写出一份空事实库 —— 同一类问题的第二个入口。

> 一个「0 条」和一个「这份材料真的没有可抽的数字」在产物里长得一模一样。
> 这比"报错"危险得多。

### 6.4 修法（三件，缺一不可）

1. 落盘：`extract_text` 现在把块级坐标与全文报表标题写进
   `channels/page_layout.json`（7.8 MB / 943 页）—— 它们原先只活在内存里。
2. 水化：新增 `common.hydrate_store_from_channels(ctx)`，从
   `channels/text_layer.json` + `page_layout.json` + `ocr.json` 把页面原文、
   文本块、块级坐标、报表标题、OCR 辅助文本灌回内存库。
   `build_facts` 入口调用它；`export` 在台账为空时从 `facts/facts.json` 灌回。
3. 硬闸门：新增 `common.assert_store_has_page_text(ctx, doc_ids)` ——
   一页权威原文都没有就直接报错并给出操作指引，不再静默交出 0 条。

### 6.5 验收：逐字节回到第一代

在独立的 runs 根里用同一个 run_id、同一时间戳，模拟「续跑跳过了
`extract_text`」（先落 `page_layout.json`，然后换一个全新上下文 —— 内存库必然为空
—— 只跑 `build_facts` + `export`）：

```
✅ facts/facts.json         410f0f5815df vs 410f0f5815df
✅ facts/build_stats.json   bead67baef36 vs bead67baef36
✅ facts/facts_table.json   f213642959a8 vs f213642959a8
✅ facts/evidence.html      c6052ea361eb vs c6052ea361eb
✅ facts/facts.xlsx         a1655a1100c8 vs a1655a1100c8
```

五项产物全部逐字节相同（事实 1050 条、`needs_review` 175 条、
`statement_scope` 分布也一致）。续跑丢内存状态这条路堵住了。

> 中途还抓出一个次生缺陷：落盘的 chunk 记录只有
> `{chunk_id, bbox, reading_order, char_len, text}`，缺 `doc_id/page/source_layer`，
> 而 `Chunk` 是 `extra="forbid"` 的严格模型 —— 水化第一版直接
> `model_validate` 每一条都抛异常，又被 `except: continue` 吞掉，
> 于是文本块一条都没灌回来，`_locate_chunk` 退化成占位块、`span_id` 全变。
> 是靠产物哈希比对抓出来的，不是靠"看着对"。

---

## 七、抽取结果对单格模型读数很敏感

把同一份文档（000722）在 `annual41-seg` 与另一轮里的表格裁决逐格对比，
差异集中在模型读数的内容上，例如 p25 表0 第 1 行第 6 列：

| 运行 | `vlm_text` | `vlm_quote` | 裁决 | 网格里的文本 |
|---|---|---|---|---|
| A | `49,434,778.77` | `49,434,77\n8.77` | `normalized` | `49,434,778.77`（已修折行） |
| B | `177,043,28.54` | `177,043,28.54` | `rejected` | `49,434,77\n8.77`（未修） |

两次是各自独立的模型调用（不是缓存回放），答案不同。一格读错 → 该格
没有"归一后的文本" → 网格里的折行数字没被修复 → 数值不可解析 →
按"无单位"拒绝 → 整张表不产出候选。实测这类差异在 85 张表里出现 69 张。

这条对演示的含义很直接：不要在现场跑冷启动解析。同一份材料两次冷跑，
事实库不会一样 —— 这正是两阶段流程（赛前预热、现场复用）存在的理由。

---

## 八、`annual41-seg` 那唯一的不一致：不是缺陷，是一次 API 抖动

修完之后，旗舰运行的 `replay --strict` 是 750 项通过 / 1 项不符，唯一不一致的是
`channels/table_adjudications.json`。定位到字段级：

```
=== 样例 report（doc_82fb6364c2f5, p8, 表2）===
  orig: adjudications: [{... 'verdict': 'error',
        'note': "ModelError: 模型调用在 3 次尝试后仍失败：模型输出不满足 JSON Schema..."}]
  repl: adjudications: [{... 'verdict': 'error',
        'note': "ModelError: 离线模式下缓存未命中，无法调用模型。请先联网跑一次 run..."}]
```

其它字段（`cells_after` / `grid_after` / `header` / `n_cols` … 85 张表全部）
逐字段相同。差别只是那句错误文本 —— 原始那次调用重试三次都失败，
失败调用没有响应缓存，离线回放自然重现不了它。
`facts.json` 等下游产物全部一致，因为这一格本来就没产出事实。

这正是 `replay.py` 自己声明过的语义：「不可复现」与「运行不完整」是两件事。

> 这段说明本来打印不出来 —— 缺陷 5（空转续跑把 `partial` 洗成 `success`）
> 把它吞掉了。修掉之后报告里重新出现了「## 说明」一节。

---

## 九、仍未解决

1. `ctx.store` / `ctx.ledger` 仍是内存对象 —— 本轮给它们加了「从产物灌回」
   与「空库硬失败」两道保险，但没有从结构上消除这个类别：
   任何新增的、只写进内存的跨阶段状态都会重犯同一个错。
   结构性的修法是让阶段间只通过产物通信（pipeline.yaml 里那句注释正是这么写的），
   内存库退化为纯缓存。
2. `page_layout.json` 让每轮多写 7.8 MB（943 页的块级坐标）。
   它与 `channels/tables.json` 有部分重复，可考虑合并。
3. 预热档的时间与费用随材料线性增长：实测 1101 秒 / 份（185 页）、¥0.96。
   4 份全跑约 1 小时、¥3–4，必须在赛前完成。
4. 续跑重跑某个阶段会留下孤儿产物：实测 3 个裁图
   （`cell_p140_r0c1` 等）由上一轮产出、留在盘上、仍在 `outputs` 里，
   而重跑的 judge 不会再渲染它们 → 回放报「回放未产出」。
   本轮的 `_preexisting` 修法只阻止新增散件，不清理跨轮孤儿。
5. `.lock` 只警告不阻断：互斥需要文件锁或 pid 校验失败即退出，
   但那会让"评审并行跑不同 run"变得脆弱，暂不引入。
6. 回放报告不显示 `overwritten_slots`：缓存被多个写者动过时应直接在
   复现报告里标注「本结论打了折扣」。目前只在 `ResponseCache.stats` 里可见。
