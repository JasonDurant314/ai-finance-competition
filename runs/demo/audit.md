# 运行审计报告 — `demo`

## 一、运行概况

- 流水线：`full`
- 状态：**success**
- 开始：2026-10-16T17:22:06.184166+00:00
- 结束：2026-10-16T17:22:13.260948+00:00
- 契约版本：`1.0.0`
- 离线模式：False
- 随机种子：0

## 二、环境指纹（复现依据）

### 模型配置

| 角色 | 模型 |
|---|---|
| critic | `deepseek-flash` |
| default | `deepseek-flash` |
| extractor | `deepseek-flash` |
| planner | `deepseek-flash` |
| vision | `deepseek-flash` |
| writer | `deepseek-flash` |

### 输入文件

| 文件 | SHA256（前 16 位） |
|---|---|
| `facts/facts.json` | `7e9e03b9fbe441c7…` |
| `sample_annual_report.pdf` | `ca1d00be95a83579…` |
| `sample_pledge_announcement.pdf` | `a6fba35ae3cf7282…` |

### 提示词版本

| 提示词 | SHA256（前 16 位） |
|---|---|
| `r1.extract_financial_facts@1.0.0` | `33d66b43afce3d22…` |
| `r1.vlm_judge_field@1.0.0` | `d799e09a44d33df5…` |
| `r1.vlm_scan_page@1.0.0` | `e5f071470ba78adc…` |
| `r4.write_valuation_report@1.0.0` | `597bad6162e2e857…` |
| `r5.adjudicate_caliber@1.0.0` | `276fc5c187a422c2…` |

## 三、过程统计

- 事件总数：**146**
- 模型调用：**0** 次（缓存命中 0 次，命中率 n/a）
- 工具调用：**0** 次
- 文件访问：**71** 次（涉及 38 个不同文件）
- 勾稽断言：**26** 条 —— 通过 11 / 失败 **0** / 未校验（缺数据）15
- Token 消耗：输入 0 / 输出 0
- 估算成本：**¥0.0000**

## 四、可复现性核验

**✅ 产物哈希与 manifest 一致**

| 产物 | 一致 |
|---|---|
| `audit/annotated_draft.html` | ✅ |
| `audit/audit.json` | ✅ |
| `audit/audit_report.md` | ✅ |
| `audit/audit_summary.json` | ✅ |
| `audit/claims.json` | ✅ |
| `audit/draft_used.md` | ✅ |
| `audit/revision_table.xlsx` | ✅ |
| `channels/conflicts.json` | ✅ |
| `channels/ocr.json` | ✅ |
| `channels/ocr_skipped.json` | ✅ |
| `channels/page_layout.json` | ✅ |
| `channels/table_adjudications.json` | ✅ |
| `channels/tables.json` | ✅ |
| `channels/text_layer.json` | ✅ |
| `channels/vlm_scans.json` | ✅ |
| `channels/vlm_verdicts.json` | ✅ |
| `facts/build_stats.json` | ✅ |
| `facts/documents.json` | ✅ |
| `facts/evidence.html` | ✅ |
| `facts/export_summary.json` | ✅ |
| `facts/facts.json` | ✅ |
| `facts/facts.xlsx` | ✅ |
| `facts/facts_table.json` | ✅ |
| `pages/index.json` | ✅ |
| `valuation/assumptions.json` | ✅ |
| `valuation/charts/fcff_waterfall.png` | ✅ |
| `valuation/charts/sensitivity_heatmap.png` | ✅ |
| `valuation/charts/tornado.png` | ✅ |
| `valuation/charts/value_range.png` | ✅ |
| `valuation/model_summary.json` | ✅ |
| `valuation/report_summary.json` | ✅ |
| `valuation/valuation.json` | ✅ |
| `valuation/valuation_model.xlsx` | ✅ |
| `valuation/valuation_report.md` | ✅ |

## 五、逐页解析路由决策

> 每一页为什么走文本层 / OCR / VLM 裁决，均有据可查。

| 页 | 路由 | 判定理由 |
|---|---|---|
| 1 | `A_text_only` | 有效字符 242 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 8.0%），直取最高置信 |
| 2 | `A_text_only` | 有效字符 470 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 17.8%），直取最高置信 |
| 3 | `A_text_only` | 有效字符 312 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 11.1%），直取最高置信 |
| 4 | `E_mixed` | 文本量中等（181 字符，覆盖 5.9%）或含图像（覆盖 0.0%），文本层为主 + 局部图像补强 |
| 5 | `A_text_only` | 有效字符 216 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 6.6%），直取最高置信 |
| 1 | `A_text_only` | 有效字符 634 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 26.3%），直取最高置信 |
| 2 | `E_mixed` | 文本量中等（140 字符，覆盖 5.5%）或含图像（覆盖 0.0%），文本层为主 + 局部图像补强 |
| 4 | `E_mixed` | 跳过 OCR：文本层可用（181 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 2 | `E_mixed` | 跳过 OCR：文本层可用（140 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |

## 六、勾稽断言明细

> 三态口径：**通过** = 独立重算一致；**失败** = 校验过且不一致；
> **未校验** = 缺辅助明细，本次无法独立重算（不是失败，但也不是通过）。

| 断言 | 结果 | 明细 |
|---|---|---|
| `A1` | ✅ 通过 | LHS=19447290000.00 ≡ RHS=19447290000.00（差异 0.00 在容差内） |
| `A5` | ➖ 未校验（缺数据） | 缺少成本/费用/税分明细，无法独立重算 |
| `A4` | ✅ 通过 | NOPAT 模型=2579683018.50 重算=2579683018.50；FCFF 模型=2199716584.50 重算=2199716584.50 |
| `A2` | ➖ 未校验（缺数据） | 缺少 CFO/CFI/CFF 明细，无法独立重算 |
| `A3` | ➖ 未校验（缺数据） | 缺少分红数据，无法独立重算 |
| `A1` | ✅ 通过 | LHS=21003073200.00 ≡ RHS=21003073200.00（差异 0.00 在容差内） |
| `A5` | ➖ 未校验（缺数据） | 缺少成本/费用/税分明细，无法独立重算 |
| `A4` | ✅ 通过 | NOPAT 模型=2786057659.98 重算=2786057659.98；FCFF 模型=2375693911.26 重算=2375693911.26 |
| `A2` | ➖ 未校验（缺数据） | 缺少 CFO/CFI/CFF 明细，无法独立重算 |
| `A3` | ➖ 未校验（缺数据） | 缺少分红数据，无法独立重算 |
| `A1` | ✅ 通过 | LHS=22683319056.00 ≡ RHS=22683319056.00（差异 0.00 在容差内） |
| `A5` | ➖ 未校验（缺数据） | 缺少成本/费用/税分明细，无法独立重算 |
| `A4` | ✅ 通过 | NOPAT 模型=3008942272.78 重算=3008942272.78；FCFF 模型=2565749424.16 重算=2565749424.16 |
| `A2` | ➖ 未校验（缺数据） | 缺少 CFO/CFI/CFF 明细，无法独立重算 |
| `A3` | ➖ 未校验（缺数据） | 缺少分红数据，无法独立重算 |
| `A1` | ✅ 通过 | LHS=24497984580.48 ≡ RHS=24497984580.48（差异 0.00 在容差内） |
| `A5` | ➖ 未校验（缺数据） | 缺少成本/费用/税分明细，无法独立重算 |
| `A4` | ✅ 通过 | NOPAT 模型=3249657654.60 重算=3249657654.60；FCFF 模型=2771009378.09 重算=2771009378.09 |
| `A2` | ➖ 未校验（缺数据） | 缺少 CFO/CFI/CFF 明细，无法独立重算 |
| `A3` | ➖ 未校验（缺数据） | 缺少分红数据，无法独立重算 |
| `A1` | ✅ 通过 | LHS=26457823346.92 ≡ RHS=26457823346.92（差异 0.00 在容差内） |
| `A5` | ➖ 未校验（缺数据） | 缺少成本/费用/税分明细，无法独立重算 |
| `A4` | ✅ 通过 | NOPAT 模型=3509630266.97 重算=3509630266.97；FCFF 模型=2992690128.34 重算=2992690128.34 |
| `A2` | ➖ 未校验（缺数据） | 缺少 CFO/CFI/CFF 明细，无法独立重算 |
| `A3` | ➖ 未校验（缺数据） | 缺少分红数据，无法独立重算 |
| `A6` | ✅ 通过 | 每股价值为正、终值占比正常、股权价值桥接正确 |

## 七、完整事件时间线

（共 146 条，此处展示前 146 条；完整记录见 `trace.jsonl`）

| # | 类型 | 对象 | 耗时(ms) | 说明 |
|---|---|---|---|---|
| 0 | 阶段 | `` |  |  |
| 1 | 阶段 | `` |  |  |
| 2 | 阶段 | `` |  |  |
| 3 | 文件访问 | `raw/sample_annual_report.pdf` |  | read |
| 4 | 页面路由 | `` |  |  |
| 5 | 页面路由 | `` |  |  |
| 6 | 页面路由 | `` |  |  |
| 7 | 页面路由 | `` |  |  |
| 8 | 页面路由 | `` |  |  |
| 9 | 阶段 | `` |  |  |
| 10 | 文件访问 | `raw/sample_pledge_announcement.pdf` |  | read |
| 11 | 页面路由 | `` |  |  |
| 12 | 页面路由 | `` |  |  |
| 13 | 阶段 | `` |  |  |
| 14 | 文件访问 | `facts/documents.json` |  | write |
| 15 | 文件访问 | `pages/index.json` |  | write |
| 16 | 阶段 | `` |  |  |
| 17 | 阶段 | `` |  |  |
| 18 | 文件访问 | `facts/documents.json` |  | read |
| 19 | 文件访问 | `raw/sample_annual_report.pdf` |  | read |
| 20 | 文件访问 | `raw/sample_pledge_announcement.pdf` |  | read |
| 21 | 文件访问 | `channels/text_layer.json` |  | write |
| 22 | 文件访问 | `channels/page_layout.json` |  | write |
| 23 | 阶段 | `` |  |  |
| 24 | 阶段 | `` |  |  |
| 25 | 阶段 | `` |  |  |
| 26 | 文件访问 | `facts/documents.json` |  | read |
| 27 | 文件访问 | `pages/index.json` |  | read |
| 28 | 文件访问 | `raw/sample_annual_report.pdf` |  | read |
| 29 | 页面路由 | `` |  |  |
| 30 | 文件访问 | `raw/sample_pledge_announcement.pdf` |  | read |
| 31 | 页面路由 | `` |  |  |
| 32 | 文件访问 | `channels/ocr.json` |  | write |
| 33 | 文件访问 | `channels/ocr_skipped.json` |  | write |
| 34 | 阶段 | `` |  |  |
| 35 | 阶段 | `` |  |  |
| 36 | 阶段 | `` |  |  |
| 37 | 文件访问 | `facts/documents.json` |  | read |
| 38 | 文件访问 | `pages/index.json` |  | read |
| 39 | 文件访问 | `channels/tables.json` |  | write |
| 40 | 阶段 | `` |  |  |
| 41 | 阶段 | `` |  |  |
| 42 | 阶段 | `` |  |  |
| 43 | 文件访问 | `channels/text_layer.json` |  | read |
| 44 | 文件访问 | `channels/ocr.json` |  | read |
| 45 | 文件访问 | `facts/documents.json` |  | read |
| 46 | 阶段 | `` |  |  |
| 47 | 文件访问 | `channels/tables.json` |  | read |
| 48 | 文件访问 | `raw/sample_annual_report.pdf` |  | read |
| 49 | 文件访问 | `raw/sample_pledge_announcement.pdf` |  | read |
| 50 | 文件访问 | `channels/conflicts.json` |  | write |
| 51 | 文件访问 | `channels/vlm_verdicts.json` |  | write |
| 52 | 文件访问 | `channels/vlm_scans.json` |  | write |
| 53 | 文件访问 | `channels/table_adjudications.json` |  | write |
| 54 | 阶段 | `` |  |  |
| 55 | 阶段 | `` |  |  |
| 56 | 阶段 | `` |  |  |
| 57 | 文件访问 | `facts/documents.json` |  | read |
| 58 | 文件访问 | `channels/text_layer.json` |  | read |
| 59 | 文件访问 | `channels/ocr.json` |  | read |
| 60 | 文件访问 | `channels/page_layout.json` |  | read |
| 61 | 文件访问 | `channels/text_layer.json` |  | read |
| 62 | 文件访问 | `channels/ocr.json` |  | read |
| 63 | 文件访问 | `facts/documents.json` |  | read |
| 64 | 阶段 | `` |  |  |
| 65 | 文件访问 | `channels/tables.json` |  | read |
| 66 | 文件访问 | `channels/vlm_scans.json` |  | read |
| 67 | 文件访问 | `channels/tables.json` |  | read |
| 68 | 文件访问 | `facts/build_stats.json` |  | write |
| 69 | 阶段 | `` |  |  |
| 70 | 阶段 | `` |  |  |
| 71 | 阶段 | `` |  |  |
| 72 | 文件访问 | `facts/documents.json` |  | read |
| 73 | 文件访问 | `facts/facts.json` |  | write |
| 74 | 文件访问 | `facts/facts_table.json` |  | write |
| 75 | 文件访问 | `facts/facts.xlsx` |  | write |
| 76 | 文件访问 | `facts/evidence.html` |  | write |
| 77 | 文件访问 | `facts/export_summary.json` |  | write |
| 78 | 阶段 | `` |  |  |
| 79 | 阶段 | `` |  |  |
| 80 | 阶段 | `` |  |  |
| 81 | 文件访问 | `facts/facts.json` |  | read |
| 82 | 阶段 | `` |  |  |
| 83 | 文件访问 | `market/rates_2026-09-17.json` |  | read |
| 84 | 文件访问 | `comps/600519_2025A.json` |  | read |
| 85 | 文件访问 | `valuation/assumptions.json` |  | write |
| 86 | 阶段 | `` |  |  |
| 87 | 阶段 | `` |  |  |
| 88 | 阶段 | `` |  |  |
| 89 | 文件访问 | `valuation/assumptions.json` |  | read |
| 90 | 文件访问 | `comps/600519_2025A.json` |  | read |
| 91 | 勾稽断言 | `A1` |  |  |
| 92 | 勾稽断言 | `A5` |  |  |
| 93 | 勾稽断言 | `A4` |  |  |
| 94 | 勾稽断言 | `A2` |  |  |
| 95 | 勾稽断言 | `A3` |  |  |
| 96 | 勾稽断言 | `A1` |  |  |
| 97 | 勾稽断言 | `A5` |  |  |
| 98 | 勾稽断言 | `A4` |  |  |
| 99 | 勾稽断言 | `A2` |  |  |
| 100 | 勾稽断言 | `A3` |  |  |
| 101 | 勾稽断言 | `A1` |  |  |
| 102 | 勾稽断言 | `A5` |  |  |
| 103 | 勾稽断言 | `A4` |  |  |
| 104 | 勾稽断言 | `A2` |  |  |
| 105 | 勾稽断言 | `A3` |  |  |
| 106 | 勾稽断言 | `A1` |  |  |
| 107 | 勾稽断言 | `A5` |  |  |
| 108 | 勾稽断言 | `A4` |  |  |
| 109 | 勾稽断言 | `A2` |  |  |
| 110 | 勾稽断言 | `A3` |  |  |
| 111 | 勾稽断言 | `A1` |  |  |
| 112 | 勾稽断言 | `A5` |  |  |
| 113 | 勾稽断言 | `A4` |  |  |
| 114 | 勾稽断言 | `A2` |  |  |
| 115 | 勾稽断言 | `A3` |  |  |
| 116 | 勾稽断言 | `A6` |  |  |
| 117 | 文件访问 | `valuation/valuation.json` |  | write |
| 118 | 文件访问 | `valuation/model_summary.json` |  | write |
| 119 | 阶段 | `` |  |  |
| 120 | 阶段 | `` |  |  |
| 121 | 文件访问 | `valuation/valuation.json` |  | read |
| 122 | 文件访问 | `valuation/assumptions.json` |  | read |
| 123 | 文件访问 | `valuation/charts/sensitivity_heatmap.png` |  | write |
| 124 | 文件访问 | `valuation/charts/tornado.png` |  | write |
| 125 | 文件访问 | `valuation/charts/value_range.png` |  | write |
| 126 | 文件访问 | `valuation/charts/fcff_waterfall.png` |  | write |
| 127 | 文件访问 | `valuation/valuation_report.md` |  | write |
| 128 | 文件访问 | `valuation/valuation_model.xlsx` |  | write |
| 129 | 文件访问 | `valuation/report_summary.json` |  | write |
| 130 | 阶段 | `` |  |  |
| 131 | 阶段 | `` |  |  |
| 132 | 阶段 | `` |  |  |
| 133 | 文件访问 | `facts/facts.json` |  | read |
| 134 | 文件访问 | `facts/documents.json` |  | read |
| 135 | 阶段 | `` |  |  |
| 136 | 文件访问 | `audit/audit.json` |  | write |
| 137 | 文件访问 | `audit/claims.json` |  | write |
| 138 | 文件访问 | `audit/draft_used.md` |  | write |
| 139 | 文件访问 | `audit/audit_report.md` |  | write |
| 140 | 文件访问 | `audit/revision_table.xlsx` |  | write |
| 141 | 文件访问 | `audit/annotated_draft.html` |  | write |
| 142 | 文件访问 | `audit/audit_summary.json` |  | write |
| 143 | 阶段 | `` |  |  |
| 144 | 阶段 | `` |  |  |
| 145 | 阶段 | `` |  |  |

---

本报告由 `agent.trace.audit` 自动生成，数据来源为 `trace.jsonl` 与 `manifest.json`，未经人工修饰。
