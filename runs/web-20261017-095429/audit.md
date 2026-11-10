# 运行审计报告 — `web-20261017-095429`

## 一、运行概况

- 流水线：`full`
- 状态：**partial**
- 开始：2026-10-17T01:54:35.275773+00:00
- 结束：2026-10-17T02:17:39.436919+00:00
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
| `601857_中国石油_中国石油天然气股份有限公司2025年年报_2026-03-30.pdf` | `840b15aa4dc38745…` |
| `facts/facts.json` | `f9fb7bca8124b21b…` |

### 提示词版本

| 提示词 | SHA256（前 16 位） |
|---|---|
| `r1.extract_financial_facts@1.0.0` | `33d66b43afce3d22…` |
| `r1.vlm_judge_field@1.0.0` | `d799e09a44d33df5…` |
| `r1.vlm_scan_page@1.0.0` | `e5f071470ba78adc…` |
| `r4.write_valuation_report@1.0.0` | `597bad6162e2e857…` |
| `r5.adjudicate_caliber@1.0.0` | `276fc5c187a422c2…` |

## 三、过程统计

- 事件总数：**1874**
- 模型调用：**400** 次（缓存命中 0 次，命中率 0.0%）
- 工具调用：**88** 次
- 文件访问：**940** 次（涉及 662 个不同文件）
- 勾稽断言：**26** 条 —— 通过 11 / 失败 **0** / 未校验（缺数据）15
- Token 消耗：输入 922,414 / 输出 59,352
- 估算成本：**¥1.7691**

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
| `facts/entity_reattribution.json` | ✅ |
| `facts/evidence.html` | ✅ |
| `facts/export_summary.json` | ✅ |
| `facts/facts.json` | ✅ |
| `facts/facts.xlsx` | ✅ |
| `facts/facts_table.json` | ✅ |
| `facts/internal_conflicts.json` | ✅ |
| `facts/scale_implausible.json` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r0c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r0c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r0c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r0c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r11c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r11c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r11c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r11c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r11c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r11c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r11c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r11c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r11c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r11c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r12c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r12c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r2c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r2c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r9c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r9c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r9c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r9c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r9c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r9c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r9c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r9c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r9c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p107_r9c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r0c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r0c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r0c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r0c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r11c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r11c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r11c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r11c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r11c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r11c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r11c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r11c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r11c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r11c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r2c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r2c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r6c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r6c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r6c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r6c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r6c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r6c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r6c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r6c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r6c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r6c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r7c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r7c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r8c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p108_r8c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p109_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p109_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p109_r0c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p109_r0c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p109_r0c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p109_r0c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p109_r0c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p109_r0c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p109_r1c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p109_r1c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p109_r1c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p109_r1c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p109_r1c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p109_r1c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p109_r1c6.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p109_r1c6_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p10_r3c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p10_r3c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p10_r3c6.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p10_r3c6_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p10_r4c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p10_r4c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p10_r4c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p10_r4c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r0c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r0c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r0c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r0c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r1c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r1c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r1c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r1c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r1c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r1c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r1c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r1c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r1c6.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r1c6_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r2c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r2c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r2c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r2c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r2c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r2c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r2c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r2c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r2c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r2c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r3c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r3c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r3c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r3c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r3c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r3c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r3c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r3c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r3c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p110_r3c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p111_r0c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p111_r0c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p111_r0c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p111_r0c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p111_r0c5.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p111_r0c5_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p111_r0c6.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p111_r0c6_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p111_r0c7.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p111_r0c7_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p111_r0c8.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p111_r0c8_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p111_r0c9.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p111_r0c9_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p111_r2c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p111_r2c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p111_r2c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p111_r2c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p111_r2c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p111_r2c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p111_r2c6.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p111_r2c6_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p112_r0c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p112_r0c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p112_r0c5.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p112_r0c5_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p112_r0c6.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p112_r0c6_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p112_r0c7.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p112_r0c7_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p112_r1c6.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p112_r1c6_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p112_r1c7.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p112_r1c7_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p112_r2c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p112_r2c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p112_r3c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p112_r3c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p112_r3c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p112_r3c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p112_r3c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p112_r3c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p112_r3c6.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p112_r3c6_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p11_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p11_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p11_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p11_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p11_r0c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p11_r0c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p11_r1c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p11_r1c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p12_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p12_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p12_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p12_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p12_r0c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p12_r0c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p12_r0c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p12_r0c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p12_r1c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p12_r1c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p12_r1c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p12_r1c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p12_r1c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p12_r1c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p12_r2c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p12_r2c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p12_r2c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p12_r2c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p13_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p13_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p13_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p13_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p13_r0c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p13_r0c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p13_r0c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p13_r0c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p141_r0c10.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p141_r0c10_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p141_r0c11.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p141_r0c11_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p141_r0c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p141_r0c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p141_r0c7.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p141_r0c7_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p141_r0c8.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p141_r0c8_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p141_r0c9.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p141_r0c9_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p141_r1c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p141_r1c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p141_r2c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p141_r2c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p141_r2c7.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p141_r2c7_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p141_r2c8.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p141_r2c8_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p142_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p142_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p142_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p142_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p142_r0c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p142_r0c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p142_r0c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p142_r0c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p142_r0c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p142_r0c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p142_r0c5.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p142_r0c5_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p142_r0c6.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p142_r0c6_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p142_r0c7.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p142_r0c7_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p142_r2c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p142_r2c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p143_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p143_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p143_r0c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p143_r0c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p143_r0c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p143_r0c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p143_r0c6.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p143_r0c6_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p144_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p144_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p144_r0c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p144_r0c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p144_r0c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p144_r0c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p144_r0c6.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p144_r0c6_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p144_r1c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p144_r1c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p22_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p22_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p22_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p22_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p22_r0c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p22_r0c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p22_r0c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p22_r0c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p22_r0c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p22_r0c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p22_r1c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p22_r1c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p22_r1c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p22_r1c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p22_r1c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p22_r1c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p23_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p23_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p23_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p23_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p23_r1c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p23_r1c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p23_r1c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p23_r1c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p23_r1c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p23_r1c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p23_r1c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p23_r1c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p23_r1c6.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p23_r1c6_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p23_r1c8.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p23_r1c8_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p23_r2c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p23_r2c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p23_r2c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p23_r2c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p27_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p27_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p27_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p27_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p27_r0c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p27_r0c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p27_r0c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p27_r0c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p27_r0c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p27_r0c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p27_r0c6.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p27_r0c6_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p27_r1c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p27_r1c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p27_r1c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p27_r1c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p29_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p29_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p29_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p29_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p29_r1c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p29_r1c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p29_r1c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p29_r1c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p30_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p30_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p30_r1c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p30_r1c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p30_r1c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p30_r1c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p30_r1c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p30_r1c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p32_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p32_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p32_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p32_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p32_r0c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p32_r0c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p32_r1c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p32_r1c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p33_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p33_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p33_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p33_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p33_r19c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p33_r19c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p33_r19c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p33_r19c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p38_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p38_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p38_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p38_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p38_r0c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p38_r0c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p38_r0c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p38_r0c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p38_r0c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p38_r0c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p50_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p50_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p50_r0c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p50_r0c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p50_r0c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p50_r0c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p50_r0c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p50_r0c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p50_r0c5.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p50_r0c5_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p50_r0c6.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p50_r0c6_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p50_r1c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p50_r1c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p50_r2c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p50_r2c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p61_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p61_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p61_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p61_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p61_r1c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p61_r1c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p61_r1c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p61_r1c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p67_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p67_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p67_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p67_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p67_r1c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p67_r1c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p67_r1c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p67_r1c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p76_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p76_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p76_r1c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p76_r1c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p76_r1c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p76_r1c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p76_r1c5.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p76_r1c5_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p77_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p77_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p77_r0c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p77_r0c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p77_r2c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p77_r2c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p77_r2c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p77_r2c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p7_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p7_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p7_r0c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p7_r0c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p7_r0c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p7_r0c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p7_r0c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p7_r0c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p7_r1c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p7_r1c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p7_r1c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p7_r1c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p81_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p81_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p81_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p81_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p81_r1c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p81_r1c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p81_r1c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p81_r1c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p85_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p85_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p85_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p85_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p85_r1c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p85_r1c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p85_r1c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p85_r1c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p86_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p86_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p86_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p86_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p86_r1c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p86_r1c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p86_r3c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p86_r3c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p8_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p8_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p8_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p8_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p8_r0c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p8_r0c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p8_r0c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p8_r0c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p8_r0c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p8_r0c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p8_r0c6.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p8_r0c6_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p8_r0c7.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p8_r0c7_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p8_r1c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p8_r1c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p90_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p90_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p90_r0c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p90_r0c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p90_r1c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p90_r1c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p90_r1c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p90_r1c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p90_r2c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p90_r2c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p90_r2c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p90_r2c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p92_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p92_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p92_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p92_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p92_r0c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p92_r0c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p92_r0c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p92_r0c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p92_r3c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p92_r3c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p92_r3c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p92_r3c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p93_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p93_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p93_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p93_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p93_r0c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p93_r0c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p93_r1c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p93_r1c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p93_r2c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p93_r2c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p94_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p94_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p94_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p94_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p94_r0c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p94_r0c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p94_r0c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p94_r0c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p95_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p95_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p95_r0c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p95_r0c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p95_r1c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p95_r1c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p95_r1c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p95_r1c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p98_r0c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p98_r0c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p98_r1c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p98_r1c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p99_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p99_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p99_r0c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p99_r0c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p99_r0c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p99_r0c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p99_r0c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p99_r0c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p99_r1c0.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p99_r1c0_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p99_r1c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p99_r1c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p99_r1c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p99_r1c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p99_r1c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p99_r1c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p99_r1c4.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p99_r1c4_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p99_r1c5.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p99_r1c5_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p9_r0c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p9_r0c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p9_r0c2.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p9_r0c2_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p9_r0c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p9_r0c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p9_r1c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p9_r1c1_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p9_r1c3.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p9_r1c3_ctx.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p9_r2c1.png` | ✅ |
| `pages/cells/doc_840b15aa4dc3/cell_p9_r2c1_ctx.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0007_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0007_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0008_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0008_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0009_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0009_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0010_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0010_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0011_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0011_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0012_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0012_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0013_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0013_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0022_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0022_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0023_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0023_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0027_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0027_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0029_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0029_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0030_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0030_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0032_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0032_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0033_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0033_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0038_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0038_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0050_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0050_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0052_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0052_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0061_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0061_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0067_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0067_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0076_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0076_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0077_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0077_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0081_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0081_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0085_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0085_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0086_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0086_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0090_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0090_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0092_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0092_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0093_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0093_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0094_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0094_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0095_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0095_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0098_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0098_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0099_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0099_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0107_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0107_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0108_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0108_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0109_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0109_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0110_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0110_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0111_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0111_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0112_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0112_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0141_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0141_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0142_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0142_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0143_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0143_300dpi_judge.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0144_300dpi_cells.png` | ✅ |
| `pages/doc_840b15aa4dc3_p0144_300dpi_judge.png` | ✅ |
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
| 1 | `E_mixed` | 文本量中等（65 字符，覆盖 12.9%）或含图像（覆盖 0.0%），文本层为主 + 局部图像补强 |
| 2 | `A_text_only` | 有效字符 1741 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 21.9%），直取最高置信 |
| 3 | `A_text_only` | 有效字符 782 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 22.4%），直取最高置信 |
| 4 | `A_text_only` | 有效字符 630 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 18.6%），直取最高置信 |
| 5 | `A_text_only` | 有效字符 494 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 14.7%），直取最高置信 |
| 6 | `E_mixed` | 文本量中等（188 字符，覆盖 6.5%）或含图像（覆盖 0.0%），文本层为主 + 局部图像补强 |
| 7 | `D_table` | 检测到 51 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 8 | `D_table` | 检测到 106 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 9 | `D_table` | 检测到 55 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 10 | `D_table` | 检测到 128 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 11 | `D_table` | 检测到 123 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 12 | `D_table` | 检测到 65 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 13 | `D_table` | 检测到 73 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 14 | `A_text_only` | 有效字符 580 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 15.1%），直取最高置信 |
| 15 | `A_text_only` | 有效字符 895 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 25.5%），直取最高置信 |
| 16 | `A_text_only` | 有效字符 984 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 27.9%），直取最高置信 |
| 17 | `A_text_only` | 有效字符 300 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 10.9%），直取最高置信 |
| 18 | `A_text_only` | 有效字符 774 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 21.6%），直取最高置信 |
| 19 | `A_text_only` | 有效字符 819 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 22.1%），直取最高置信 |
| 20 | `A_text_only` | 有效字符 1056 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 31.1%），直取最高置信 |
| 21 | `A_text_only` | 有效字符 906 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 26.7%），直取最高置信 |
| 22 | `D_table` | 检测到 60 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 23 | `D_table` | 检测到 58 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 24 | `A_text_only` | 有效字符 995 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 26.0%），直取最高置信 |
| 25 | `A_text_only` | 有效字符 864 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 22.6%），直取最高置信 |
| 26 | `A_text_only` | 有效字符 963 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 25.5%），直取最高置信 |
| 27 | `D_table` | 检测到 43 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 28 | `A_text_only` | 有效字符 736 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 21.7%），直取最高置信 |
| 29 | `D_table` | 检测到 77 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 30 | `D_table` | 检测到 115 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 31 | `A_text_only` | 有效字符 747 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 21.6%），直取最高置信 |
| 32 | `D_table` | 检测到 132 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 33 | `D_table` | 检测到 157 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 34 | `A_text_only` | 有效字符 842 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 23.4%），直取最高置信 |
| 35 | `A_text_only` | 有效字符 1029 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 29.8%），直取最高置信 |
| 36 | `A_text_only` | 有效字符 1114 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 31.6%），直取最高置信 |
| 37 | `A_text_only` | 有效字符 795 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 23.4%），直取最高置信 |
| 38 | `D_table` | 检测到 208 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 39 | `A_text_only` | 有效字符 857 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 24.6%），直取最高置信 |
| 40 | `A_text_only` | 有效字符 820 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 23.2%），直取最高置信 |
| 41 | `A_text_only` | 有效字符 549 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 15.8%），直取最高置信 |
| 42 | `A_text_only` | 有效字符 813 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 23.7%），直取最高置信 |
| 43 | `A_text_only` | 有效字符 932 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 26.2%），直取最高置信 |
| 44 | `A_text_only` | 有效字符 699 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 20.7%），直取最高置信 |
| 45 | `A_text_only` | 有效字符 746 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 22.0%），直取最高置信 |
| 46 | `A_text_only` | 有效字符 948 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 26.4%），直取最高置信 |
| 47 | `A_text_only` | 有效字符 857 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 24.7%），直取最高置信 |
| 48 | `A_text_only` | 有效字符 820 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 23.5%），直取最高置信 |
| 49 | `A_text_only` | 有效字符 641 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 17.3%），直取最高置信 |
| 50 | `D_table` | 检测到 130 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 51 | `A_text_only` | 有效字符 530 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 15.8%），直取最高置信 |
| 52 | `D_table` | 检测到 98 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 53 | `A_text_only` | 有效字符 828 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 23.6%），直取最高置信 |
| 54 | `A_text_only` | 有效字符 930 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 23.1%），直取最高置信 |
| 55 | `A_text_only` | 有效字符 774 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 22.3%），直取最高置信 |
| 56 | `A_text_only` | 有效字符 900 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 25.8%），直取最高置信 |
| 57 | `A_text_only` | 有效字符 828 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 24.2%），直取最高置信 |
| 58 | `A_text_only` | 有效字符 874 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 25.3%），直取最高置信 |
| 59 | `A_text_only` | 有效字符 928 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 26.7%），直取最高置信 |
| 60 | `A_text_only` | 有效字符 987 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 28.4%），直取最高置信 |
| 61 | `D_table` | 检测到 134 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 62 | `A_text_only` | 有效字符 915 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 26.3%），直取最高置信 |
| 63 | `A_text_only` | 有效字符 893 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 25.2%），直取最高置信 |
| 64 | `A_text_only` | 有效字符 966 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 27.3%），直取最高置信 |
| 65 | `A_text_only` | 有效字符 894 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 25.1%），直取最高置信 |
| 66 | `A_text_only` | 有效字符 891 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 25.4%），直取最高置信 |
| 67 | `D_table` | 检测到 119 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 68 | `A_text_only` | 有效字符 738 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 21.2%），直取最高置信 |
| 69 | `A_text_only` | 有效字符 290 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 8.1%），直取最高置信 |
| 70 | `A_text_only` | 有效字符 736 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 21.8%），直取最高置信 |
| 71 | `A_text_only` | 有效字符 868 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 24.7%），直取最高置信 |
| 72 | `A_text_only` | 有效字符 275 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 7.5%），直取最高置信 |
| 73 | `A_text_only` | 有效字符 684 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 19.6%），直取最高置信 |
| 74 | `A_text_only` | 有效字符 692 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 19.8%），直取最高置信 |
| 75 | `A_text_only` | 有效字符 662 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 18.9%），直取最高置信 |
| 76 | `D_table` | 检测到 101 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 77 | `D_table` | 检测到 171 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 78 | `A_text_only` | 有效字符 630 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 17.6%），直取最高置信 |
| 79 | `A_text_only` | 有效字符 787 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 22.8%），直取最高置信 |
| 80 | `A_text_only` | 有效字符 259 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 7.1%），直取最高置信 |
| 81 | `D_table` | 检测到 175 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 82 | `A_text_only` | 有效字符 1072 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 29.7%），直取最高置信 |
| 83 | `A_text_only` | 有效字符 1006 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 28.1%），直取最高置信 |
| 84 | `A_text_only` | 有效字符 1099 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 29.8%），直取最高置信 |
| 85 | `D_table` | 检测到 85 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 86 | `D_table` | 检测到 129 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 87 | `A_text_only` | 有效字符 1010 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 28.5%），直取最高置信 |
| 88 | `A_text_only` | 有效字符 913 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 25.4%），直取最高置信 |
| 89 | `A_text_only` | 有效字符 684 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 20.0%），直取最高置信 |
| 90 | `D_table` | 检测到 81 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 91 | `A_text_only` | 有效字符 585 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 16.7%），直取最高置信 |
| 92 | `D_table` | 检测到 105 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 93 | `D_table` | 检测到 117 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 94 | `D_table` | 检测到 77 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 95 | `D_table` | 检测到 57 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 96 | `A_text_only` | 有效字符 327 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 7.9%），直取最高置信 |
| 97 | `A_text_only` | 有效字符 1321 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 39.2%），直取最高置信 |
| 98 | `D_table` | 检测到 88 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 99 | `D_table` | 检测到 48 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 100 | `A_text_only` | 有效字符 857 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 24.4%），直取最高置信 |
| 101 | `A_text_only` | 有效字符 1241 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 47.6%），直取最高置信 |
| 102 | `A_text_only` | 有效字符 543 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 25.3%），直取最高置信 |
| 103 | `A_text_only` | 有效字符 557 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 39.3%），直取最高置信 |
| 104 | `A_text_only` | 有效字符 663 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 27.8%），直取最高置信 |
| 105 | `A_text_only` | 有效字符 643 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 42.6%），直取最高置信 |
| 106 | `A_text_only` | 有效字符 404 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 43.3%），直取最高置信 |
| 107 | `D_table` | 检测到 46 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 108 | `D_table` | 检测到 69 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 109 | `D_table` | 检测到 69 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 110 | `D_table` | 检测到 97 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 111 | `D_table` | 检测到 106 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 112 | `D_table` | 检测到 83 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 113 | `A_text_only` | 有效字符 815 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 44.9%），直取最高置信 |
| 114 | `A_text_only` | 有效字符 802 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 44.8%），直取最高置信 |
| 115 | `A_text_only` | 有效字符 936 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 56.8%），直取最高置信 |
| 116 | `A_text_only` | 有效字符 1085 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 60.0%），直取最高置信 |
| 117 | `A_text_only` | 有效字符 787 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 54.2%），直取最高置信 |
| 118 | `A_text_only` | 有效字符 1035 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 57.8%），直取最高置信 |
| 119 | `A_text_only` | 有效字符 1004 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 54.2%），直取最高置信 |
| 120 | `A_text_only` | 有效字符 860 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 47.3%），直取最高置信 |
| 121 | `A_text_only` | 有效字符 1024 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 60.4%），直取最高置信 |
| 122 | `A_text_only` | 有效字符 1007 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 59.6%），直取最高置信 |
| 123 | `A_text_only` | 有效字符 885 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 52.1%），直取最高置信 |
| 124 | `A_text_only` | 有效字符 1092 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 57.3%），直取最高置信 |
| 125 | `A_text_only` | 有效字符 956 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 57.8%），直取最高置信 |
| 126 | `A_text_only` | 有效字符 1161 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 60.6%），直取最高置信 |
| 127 | `A_text_only` | 有效字符 1002 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 58.4%），直取最高置信 |
| 128 | `A_text_only` | 有效字符 1044 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 55.4%），直取最高置信 |
| 129 | `A_text_only` | 有效字符 1101 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 58.9%），直取最高置信 |
| 130 | `A_text_only` | 有效字符 1030 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 58.5%），直取最高置信 |
| 131 | `A_text_only` | 有效字符 1136 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 60.4%），直取最高置信 |
| 132 | `A_text_only` | 有效字符 921 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 54.7%），直取最高置信 |
| 133 | `A_text_only` | 有效字符 856 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 52.8%），直取最高置信 |
| 134 | `A_text_only` | 有效字符 1007 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 59.5%），直取最高置信 |
| 135 | `A_text_only` | 有效字符 961 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 56.4%），直取最高置信 |
| 136 | `A_text_only` | 有效字符 1057 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 54.6%），直取最高置信 |
| 137 | `A_text_only` | 有效字符 867 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 50.9%），直取最高置信 |
| 138 | `A_text_only` | 有效字符 897 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 46.5%），直取最高置信 |
| 139 | `A_text_only` | 有效字符 1282 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 47.8%），直取最高置信 |
| 140 | `A_text_only` | 有效字符 729 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 32.4%），直取最高置信 |
| 141 | `D_table` | 检测到 40 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 142 | `D_table` | 检测到 58 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 143 | `D_table` | 检测到 127 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 144 | `D_table` | 检测到 97 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 145 | `A_text_only` | 有效字符 622 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 32.8%），直取最高置信 |
| 146 | `D_table` | 检测到 186 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 147 | `D_table` | 检测到 73 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 148 | `D_table` | 检测到 58 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 149 | `D_table` | 检测到 91 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 150 | `D_table` | 检测到 92 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 151 | `D_table` | 检测到 104 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 152 | `D_table` | 检测到 68 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 153 | `D_table` | 检测到 51 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 154 | `D_table` | 检测到 51 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 155 | `D_table` | 检测到 51 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 156 | `D_table` | 检测到 65 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 157 | `D_table` | 检测到 60 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 158 | `D_table` | 检测到 46 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 159 | `D_table` | 检测到 50 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 160 | `D_table` | 检测到 62 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 161 | `D_table` | 检测到 71 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 162 | `D_table` | 检测到 49 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 163 | `D_table` | 检测到 62 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 164 | `D_table` | 检测到 71 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 165 | `D_table` | 检测到 44 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 166 | `D_table` | 检测到 44 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 167 | `D_table` | 检测到 86 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 168 | `D_table` | 检测到 133 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 169 | `D_table` | 检测到 134 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 170 | `D_table` | 检测到 62 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 171 | `D_table` | 检测到 66 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 172 | `D_table` | 检测到 68 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 173 | `D_table` | 检测到 74 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 174 | `D_table` | 检测到 44 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 175 | `A_text_only` | 有效字符 559 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 35.8%），直取最高置信 |
| 176 | `D_table` | 检测到 97 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 177 | `A_text_only` | 有效字符 702 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 39.4%），直取最高置信 |
| 178 | `D_table` | 检测到 50 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 179 | `D_table` | 检测到 80 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 180 | `A_text_only` | 有效字符 1009 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 51.9%），直取最高置信 |
| 181 | `A_text_only` | 有效字符 907 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 54.6%），直取最高置信 |
| 182 | `D_table` | 检测到 56 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 183 | `D_table` | 检测到 56 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 184 | `D_table` | 检测到 67 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 185 | `A_text_only` | 有效字符 899 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 42.5%），直取最高置信 |
| 186 | `A_text_only` | 有效字符 780 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 33.2%），直取最高置信 |
| 187 | `A_text_only` | 有效字符 1308 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 53.5%），直取最高置信 |
| 188 | `A_text_only` | 有效字符 712 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 42.8%），直取最高置信 |
| 189 | `A_text_only` | 有效字符 788 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 47.6%），直取最高置信 |
| 190 | `A_text_only` | 有效字符 873 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 45.8%），直取最高置信 |
| 191 | `A_text_only` | 有效字符 644 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 33.4%），直取最高置信 |
| 192 | `A_text_only` | 有效字符 337 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 20.5%），直取最高置信 |
| 193 | `A_text_only` | 有效字符 796 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 23.5%），直取最高置信 |
| 194 | `A_text_only` | 有效字符 522 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 15.7%），直取最高置信 |
| 195 | `A_text_only` | 有效字符 546 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 16.6%），直取最高置信 |
| 196 | `A_text_only` | 有效字符 639 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 23.2%），直取最高置信 |
| 197 | `A_text_only` | 有效字符 608 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 23.9%），直取最高置信 |
| 198 | `A_text_only` | 有效字符 606 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 22.4%），直取最高置信 |
| 199 | `A_text_only` | 有效字符 319 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 10.8%），直取最高置信 |
| 200 | `D_table` | 检测到 57 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 201 | `D_table` | 检测到 52 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 202 | `A_text_only` | 有效字符 781 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 34.3%），直取最高置信 |
| 203 | `A_text_only` | 有效字符 988 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 35.8%），直取最高置信 |
| 204 | `D_table` | 检测到 76 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 205 | `A_text_only` | 有效字符 1056 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 56.7%），直取最高置信 |
| 206 | `A_text_only` | 有效字符 930 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 51.6%），直取最高置信 |
| 207 | `A_text_only` | 有效字符 951 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 44.3%），直取最高置信 |
| 208 | `A_text_only` | 有效字符 863 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 51.7%），直取最高置信 |
| 209 | `A_text_only` | 有效字符 864 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 50.1%），直取最高置信 |
| 210 | `A_text_only` | 有效字符 966 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 46.9%），直取最高置信 |
| 211 | `A_text_only` | 有效字符 718 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 34.2%），直取最高置信 |
| 212 | `A_text_only` | 有效字符 1029 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 52.3%），直取最高置信 |
| 213 | `A_text_only` | 有效字符 992 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 50.6%），直取最高置信 |
| 214 | `A_text_only` | 有效字符 855 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 45.9%），直取最高置信 |
| 215 | `A_text_only` | 有效字符 907 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 48.7%），直取最高置信 |
| 216 | `A_text_only` | 有效字符 1001 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 42.8%），直取最高置信 |
| 217 | `A_text_only` | 有效字符 1004 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 57.4%），直取最高置信 |
| 218 | `A_text_only` | 有效字符 1019 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 53.2%），直取最高置信 |
| 219 | `A_text_only` | 有效字符 962 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 55.7%），直取最高置信 |
| 220 | `A_text_only` | 有效字符 863 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 53.9%），直取最高置信 |
| 221 | `A_text_only` | 有效字符 764 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 44.0%），直取最高置信 |
| 222 | `A_text_only` | 有效字符 837 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 50.4%），直取最高置信 |
| 223 | `A_text_only` | 有效字符 1011 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 51.7%），直取最高置信 |
| 224 | `A_text_only` | 有效字符 829 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 42.5%），直取最高置信 |
| 225 | `D_table` | 检测到 43 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 226 | `D_table` | 检测到 43 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 227 | `A_text_only` | 有效字符 859 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 40.8%），直取最高置信 |
| 228 | `D_table` | 检测到 186 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 229 | `A_text_only` | 有效字符 1069 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 49.0%），直取最高置信 |
| 230 | `A_text_only` | 有效字符 557 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 32.9%），直取最高置信 |
| 231 | `D_table` | 检测到 66 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 232 | `A_text_only` | 有效字符 1093 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 34.7%），直取最高置信 |
| 233 | `A_text_only` | 有效字符 1100 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 41.2%），直取最高置信 |
| 234 | `A_text_only` | 有效字符 973 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 32.3%），直取最高置信 |
| 235 | `D_table` | 检测到 148 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 236 | `A_text_only` | 有效字符 992 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 43.3%），直取最高置信 |
| 237 | `D_table` | 检测到 121 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 238 | `D_table` | 检测到 108 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 239 | `A_text_only` | 有效字符 1100 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 50.3%），直取最高置信 |
| 240 | `D_table` | 检测到 144 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 241 | `D_table` | 检测到 90 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 242 | `D_table` | 检测到 83 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 243 | `D_table` | 检测到 74 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 244 | `D_table` | 检测到 93 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 245 | `A_text_only` | 有效字符 813 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 41.4%），直取最高置信 |
| 246 | `D_table` | 检测到 53 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 247 | `D_table` | 检测到 57 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 248 | `A_text_only` | 有效字符 995 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 44.6%），直取最高置信 |
| 249 | `D_table` | 检测到 61 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 250 | `D_table` | 检测到 42 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 251 | `A_text_only` | 有效字符 757 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 36.3%），直取最高置信 |
| 252 | `A_text_only` | 有效字符 869 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 41.4%），直取最高置信 |
| 253 | `D_table` | 检测到 44 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 254 | `A_text_only` | 有效字符 1079 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 44.1%），直取最高置信 |
| 255 | `A_text_only` | 有效字符 1250 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 49.8%），直取最高置信 |
| 256 | `A_text_only` | 有效字符 772 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 48.3%），直取最高置信 |
| 257 | `D_table` | 检测到 59 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 258 | `D_table` | 检测到 96 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 259 | `D_table` | 检测到 45 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 260 | `A_text_only` | 有效字符 395 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 14.5%），直取最高置信 |
| 261 | `A_text_only` | 有效字符 1011 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 49.7%），直取最高置信 |
| 262 | `A_text_only` | 有效字符 780 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 35.0%），直取最高置信 |
| 263 | `A_text_only` | 有效字符 910 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 49.9%），直取最高置信 |
| 264 | `D_table` | 检测到 54 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 265 | `D_table` | 检测到 68 条表框线段（>= 40），判定为表格页，走三引擎表决 |
| 266 | `A_text_only` | 有效字符 759 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 36.5%），直取最高置信 |
| 267 | `A_text_only` | 有效字符 693 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 42.5%），直取最高置信 |
| 268 | `A_text_only` | 有效字符 433 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 23.1%），直取最高置信 |
| 269 | `A_text_only` | 有效字符 369 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 15.7%），直取最高置信 |
| 270 | `A_text_only` | 有效字符 229 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 7.3%），直取最高置信 |
| 271 | `A_text_only` | 有效字符 413 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 13.2%），直取最高置信 |
| 272 | `A_text_only` | 有效字符 250 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 7.7%），直取最高置信 |
| 273 | `A_text_only` | 有效字符 401 个（>= 200）且乱码率 0.00% < 5%，文本层干净可用（文本覆盖 23.9%），直取最高置信 |
| 1 | `E_mixed` | 跳过 OCR：文本层可用（65 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 6 | `E_mixed` | 跳过 OCR：文本层可用（188 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 7 | `D_table` | 跳过 OCR：文本层可用（1399 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 8 | `D_table` | 跳过 OCR：文本层可用（821 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 9 | `D_table` | 跳过 OCR：文本层可用（699 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 10 | `D_table` | 跳过 OCR：文本层可用（697 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 11 | `D_table` | 跳过 OCR：文本层可用（933 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 12 | `D_table` | 跳过 OCR：文本层可用（849 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 13 | `D_table` | 跳过 OCR：文本层可用（1325 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 22 | `D_table` | 跳过 OCR：文本层可用（843 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 23 | `D_table` | 跳过 OCR：文本层可用（1040 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 27 | `D_table` | 跳过 OCR：文本层可用（936 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 29 | `D_table` | 跳过 OCR：文本层可用（887 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 30 | `D_table` | 跳过 OCR：文本层可用（1056 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 32 | `D_table` | 跳过 OCR：文本层可用（831 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 33 | `D_table` | 跳过 OCR：文本层可用（1312 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 38 | `D_table` | 跳过 OCR：文本层可用（793 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 50 | `D_table` | 跳过 OCR：文本层可用（1096 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 52 | `D_table` | 跳过 OCR：文本层可用（427 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 61 | `D_table` | 跳过 OCR：文本层可用（678 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 67 | `D_table` | 跳过 OCR：文本层可用（1056 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 76 | `D_table` | 跳过 OCR：文本层可用（778 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 77 | `D_table` | 跳过 OCR：文本层可用（651 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 81 | `D_table` | 跳过 OCR：文本层可用（1148 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 85 | `D_table` | 跳过 OCR：文本层可用（836 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 86 | `D_table` | 跳过 OCR：文本层可用（1029 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 90 | `D_table` | 跳过 OCR：文本层可用（485 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 92 | `D_table` | 跳过 OCR：文本层可用（1363 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 93 | `D_table` | 跳过 OCR：文本层可用（867 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 94 | `D_table` | 跳过 OCR：文本层可用（880 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 95 | `D_table` | 跳过 OCR：文本层可用（648 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 98 | `D_table` | 跳过 OCR：文本层可用（1059 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 99 | `D_table` | 跳过 OCR：文本层可用（943 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 107 | `D_table` | 跳过 OCR：文本层可用（1191 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 108 | `D_table` | 跳过 OCR：文本层可用（1410 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 109 | `D_table` | 跳过 OCR：文本层可用（1806 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 110 | `D_table` | 跳过 OCR：文本层可用（2023 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 111 | `D_table` | 跳过 OCR：文本层可用（1767 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 112 | `D_table` | 跳过 OCR：文本层可用（1111 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 141 | `D_table` | 跳过 OCR：文本层可用（1375 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 142 | `D_table` | 跳过 OCR：文本层可用（929 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 143 | `D_table` | 跳过 OCR：文本层可用（1025 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 144 | `D_table` | 跳过 OCR：文本层可用（994 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 146 | `D_table` | 跳过 OCR：文本层可用（1279 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 147 | `D_table` | 跳过 OCR：文本层可用（763 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 148 | `D_table` | 跳过 OCR：文本层可用（1227 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 149 | `D_table` | 跳过 OCR：文本层可用（995 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 150 | `D_table` | 跳过 OCR：文本层可用（1707 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 151 | `D_table` | 跳过 OCR：文本层可用（1326 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 152 | `D_table` | 跳过 OCR：文本层可用（563 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 153 | `D_table` | 跳过 OCR：文本层可用（1305 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 154 | `D_table` | 跳过 OCR：文本层可用（1247 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 155 | `D_table` | 跳过 OCR：文本层可用（816 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 156 | `D_table` | 跳过 OCR：文本层可用（987 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 157 | `D_table` | 跳过 OCR：文本层可用（820 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 158 | `D_table` | 跳过 OCR：文本层可用（854 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 159 | `D_table` | 跳过 OCR：文本层可用（903 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 160 | `D_table` | 跳过 OCR：文本层可用（774 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 161 | `D_table` | 跳过 OCR：文本层可用（891 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 162 | `D_table` | 跳过 OCR：文本层可用（1033 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 163 | `D_table` | 跳过 OCR：文本层可用（1325 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 164 | `D_table` | 跳过 OCR：文本层可用（1022 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 165 | `D_table` | 跳过 OCR：文本层可用（971 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 166 | `D_table` | 跳过 OCR：文本层可用（979 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 167 | `D_table` | 跳过 OCR：文本层可用（819 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 168 | `D_table` | 跳过 OCR：文本层可用（1701 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 169 | `D_table` | 跳过 OCR：文本层可用（1180 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 170 | `D_table` | 跳过 OCR：文本层可用（678 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 171 | `D_table` | 跳过 OCR：文本层可用（711 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 172 | `D_table` | 跳过 OCR：文本层可用（601 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 173 | `D_table` | 跳过 OCR：文本层可用（825 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 174 | `D_table` | 跳过 OCR：文本层可用（941 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 176 | `D_table` | 跳过 OCR：文本层可用（1488 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 178 | `D_table` | 跳过 OCR：文本层可用（930 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 179 | `D_table` | 跳过 OCR：文本层可用（1331 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 182 | `D_table` | 跳过 OCR：文本层可用（1056 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 183 | `D_table` | 跳过 OCR：文本层可用（555 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 184 | `D_table` | 跳过 OCR：文本层可用（480 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 200 | `D_table` | 跳过 OCR：文本层可用（1264 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 201 | `D_table` | 跳过 OCR：文本层可用（1509 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 204 | `D_table` | 跳过 OCR：文本层可用（1377 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 225 | `D_table` | 跳过 OCR：文本层可用（876 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 226 | `D_table` | 跳过 OCR：文本层可用（939 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 228 | `D_table` | 跳过 OCR：文本层可用（1940 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 231 | `D_table` | 跳过 OCR：文本层可用（1104 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 235 | `D_table` | 跳过 OCR：文本层可用（1793 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 237 | `D_table` | 跳过 OCR：文本层可用（1716 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 238 | `D_table` | 跳过 OCR：文本层可用（1842 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 240 | `D_table` | 跳过 OCR：文本层可用（1473 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 241 | `D_table` | 跳过 OCR：文本层可用（1136 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 242 | `D_table` | 跳过 OCR：文本层可用（1272 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 243 | `D_table` | 跳过 OCR：文本层可用（720 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 244 | `D_table` | 跳过 OCR：文本层可用（1307 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 246 | `D_table` | 跳过 OCR：文本层可用（910 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 247 | `D_table` | 跳过 OCR：文本层可用（906 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 249 | `D_table` | 跳过 OCR：文本层可用（993 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 250 | `D_table` | 跳过 OCR：文本层可用（773 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 253 | `D_table` | 跳过 OCR：文本层可用（594 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 257 | `D_table` | 跳过 OCR：文本层可用（1208 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 258 | `D_table` | 跳过 OCR：文本层可用（1408 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 259 | `D_table` | 跳过 OCR：文本层可用（1090 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 264 | `D_table` | 跳过 OCR：文本层可用（934 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |
| 265 | `D_table` | 跳过 OCR：文本层可用（900 字符，乱码率 0.00%），跳过 OCR —— 表格结构由几何重建提供，不依赖 OCR |

## 六、勾稽断言明细

> 三态口径：**通过** = 独立重算一致；**失败** = 校验过且不一致；
> **未校验** = 缺辅助明细，本次无法独立重算（不是失败，但也不是通过）。

| 断言 | 结果 | 明细 |
|---|---|---|
| `A1` | ✅ 通过 | LHS=4189285912500.00 ≡ RHS=4189285912500.01（差异 -0.01 在容差内） |
| `A5` | ➖ 未校验（缺数据） | 缺少成本/费用/税分明细，无法独立重算 |
| `A4` | ✅ 通过 | NOPAT 模型=171551258116.88 重算=171551258116.88；FCFF 模型=126765285301.88 重算=126765285301.88 |
| `A2` | ➖ 未校验（缺数据） | 缺少 CFO/CFI/CFF 明细，无法独立重算 |
| `A3` | ➖ 未校验（缺数据） | 缺少分红数据，无法独立重算 |
| `A1` | ✅ 通过 | LHS=4084553764687.50 ≡ RHS=4084553764687.50（差异 0.00 在容差内） |
| `A5` | ➖ 未校验（缺数据） | 缺少成本/费用/税分明细，无法独立重算 |
| `A4` | ✅ 通过 | NOPAT 模型=167262476663.95 重算=167262476663.96；FCFF 模型=123596153169.33 重算=123596153169.33 |
| `A2` | ➖ 未校验（缺数据） | 缺少 CFO/CFI/CFF 明细，无法独立重算 |
| `A3` | ➖ 未校验（缺数据） | 缺少分红数据，无法独立重算 |
| `A1` | ✅ 通过 | LHS=3982439920570.31 ≡ RHS=3982439920570.31（差异 0.00 在容差内） |
| `A5` | ➖ 未校验（缺数据） | 缺少成本/费用/税分明细，无法独立重算 |
| `A4` | ✅ 通过 | NOPAT 模型=163080914747.35 重算=163080914747.36；FCFF 模型=120506249340.09 重算=120506249340.10 |
| `A2` | ➖ 未校验（缺数据） | 缺少 CFO/CFI/CFF 明细，无法独立重算 |
| `A3` | ➖ 未校验（缺数据） | 缺少分红数据，无法独立重算 |
| `A1` | ✅ 通过 | LHS=3882878922556.05 ≡ RHS=3882878922556.05（差异 0.00 在容差内） |
| `A5` | ➖ 未校验（缺数据） | 缺少成本/费用/税分明细，无法独立重算 |
| `A4` | ✅ 通过 | NOPAT 模型=159003891878.67 重算=159003891878.67；FCFF 模型=117493593106.59 重算=117493593106.59 |
| `A2` | ➖ 未校验（缺数据） | 缺少 CFO/CFI/CFF 明细，无法独立重算 |
| `A3` | ➖ 未校验（缺数据） | 缺少分红数据，无法独立重算 |
| `A1` | ✅ 通过 | LHS=3785806949492.15 ≡ RHS=3785806949492.16（差异 -0.01 在容差内） |
| `A5` | ➖ 未校验（缺数据） | 缺少成本/费用/税分明细，无法独立重算 |
| `A4` | ✅ 通过 | NOPAT 模型=155028794581.70 重算=155028794581.70；FCFF 模型=114556253278.93 重算=114556253278.93 |
| `A2` | ➖ 未校验（缺数据） | 缺少 CFO/CFI/CFF 明细，无法独立重算 |
| `A3` | ➖ 未校验（缺数据） | 缺少分红数据，无法独立重算 |
| `A6` | ✅ 通过 | 每股价值为正、终值占比正常、股权价值桥接正确 |

## 七、完整事件时间线

（共 1874 条，此处展示前 400 条；完整记录见 `trace.jsonl`）

| # | 类型 | 对象 | 耗时(ms) | 说明 |
|---|---|---|---|---|
| 0 | 阶段 | `` |  |  |
| 1 | 阶段 | `` |  |  |
| 2 | 阶段 | `` |  |  |
| 3 | 文件访问 | `raw/601857_中国石油_中国石油天然气股份有限公司2025年年报_2026-03-30.pdf` |  | read |
| 4 | 页面路由 | `` |  |  |
| 5 | 页面路由 | `` |  |  |
| 6 | 页面路由 | `` |  |  |
| 7 | 页面路由 | `` |  |  |
| 8 | 页面路由 | `` |  |  |
| 9 | 页面路由 | `` |  |  |
| 10 | 页面路由 | `` |  |  |
| 11 | 页面路由 | `` |  |  |
| 12 | 页面路由 | `` |  |  |
| 13 | 页面路由 | `` |  |  |
| 14 | 页面路由 | `` |  |  |
| 15 | 页面路由 | `` |  |  |
| 16 | 页面路由 | `` |  |  |
| 17 | 页面路由 | `` |  |  |
| 18 | 页面路由 | `` |  |  |
| 19 | 页面路由 | `` |  |  |
| 20 | 页面路由 | `` |  |  |
| 21 | 页面路由 | `` |  |  |
| 22 | 页面路由 | `` |  |  |
| 23 | 页面路由 | `` |  |  |
| 24 | 页面路由 | `` |  |  |
| 25 | 页面路由 | `` |  |  |
| 26 | 页面路由 | `` |  |  |
| 27 | 页面路由 | `` |  |  |
| 28 | 页面路由 | `` |  |  |
| 29 | 页面路由 | `` |  |  |
| 30 | 页面路由 | `` |  |  |
| 31 | 页面路由 | `` |  |  |
| 32 | 页面路由 | `` |  |  |
| 33 | 页面路由 | `` |  |  |
| 34 | 页面路由 | `` |  |  |
| 35 | 页面路由 | `` |  |  |
| 36 | 页面路由 | `` |  |  |
| 37 | 页面路由 | `` |  |  |
| 38 | 页面路由 | `` |  |  |
| 39 | 页面路由 | `` |  |  |
| 40 | 页面路由 | `` |  |  |
| 41 | 页面路由 | `` |  |  |
| 42 | 页面路由 | `` |  |  |
| 43 | 页面路由 | `` |  |  |
| 44 | 页面路由 | `` |  |  |
| 45 | 页面路由 | `` |  |  |
| 46 | 页面路由 | `` |  |  |
| 47 | 页面路由 | `` |  |  |
| 48 | 页面路由 | `` |  |  |
| 49 | 页面路由 | `` |  |  |
| 50 | 页面路由 | `` |  |  |
| 51 | 页面路由 | `` |  |  |
| 52 | 页面路由 | `` |  |  |
| 53 | 页面路由 | `` |  |  |
| 54 | 页面路由 | `` |  |  |
| 55 | 页面路由 | `` |  |  |
| 56 | 页面路由 | `` |  |  |
| 57 | 页面路由 | `` |  |  |
| 58 | 页面路由 | `` |  |  |
| 59 | 页面路由 | `` |  |  |
| 60 | 页面路由 | `` |  |  |
| 61 | 页面路由 | `` |  |  |
| 62 | 页面路由 | `` |  |  |
| 63 | 页面路由 | `` |  |  |
| 64 | 页面路由 | `` |  |  |
| 65 | 页面路由 | `` |  |  |
| 66 | 页面路由 | `` |  |  |
| 67 | 页面路由 | `` |  |  |
| 68 | 页面路由 | `` |  |  |
| 69 | 页面路由 | `` |  |  |
| 70 | 页面路由 | `` |  |  |
| 71 | 页面路由 | `` |  |  |
| 72 | 页面路由 | `` |  |  |
| 73 | 页面路由 | `` |  |  |
| 74 | 页面路由 | `` |  |  |
| 75 | 页面路由 | `` |  |  |
| 76 | 页面路由 | `` |  |  |
| 77 | 页面路由 | `` |  |  |
| 78 | 页面路由 | `` |  |  |
| 79 | 页面路由 | `` |  |  |
| 80 | 页面路由 | `` |  |  |
| 81 | 页面路由 | `` |  |  |
| 82 | 页面路由 | `` |  |  |
| 83 | 页面路由 | `` |  |  |
| 84 | 页面路由 | `` |  |  |
| 85 | 页面路由 | `` |  |  |
| 86 | 页面路由 | `` |  |  |
| 87 | 页面路由 | `` |  |  |
| 88 | 页面路由 | `` |  |  |
| 89 | 页面路由 | `` |  |  |
| 90 | 页面路由 | `` |  |  |
| 91 | 页面路由 | `` |  |  |
| 92 | 页面路由 | `` |  |  |
| 93 | 页面路由 | `` |  |  |
| 94 | 页面路由 | `` |  |  |
| 95 | 页面路由 | `` |  |  |
| 96 | 页面路由 | `` |  |  |
| 97 | 页面路由 | `` |  |  |
| 98 | 页面路由 | `` |  |  |
| 99 | 页面路由 | `` |  |  |
| 100 | 页面路由 | `` |  |  |
| 101 | 页面路由 | `` |  |  |
| 102 | 页面路由 | `` |  |  |
| 103 | 页面路由 | `` |  |  |
| 104 | 页面路由 | `` |  |  |
| 105 | 页面路由 | `` |  |  |
| 106 | 页面路由 | `` |  |  |
| 107 | 页面路由 | `` |  |  |
| 108 | 页面路由 | `` |  |  |
| 109 | 页面路由 | `` |  |  |
| 110 | 页面路由 | `` |  |  |
| 111 | 页面路由 | `` |  |  |
| 112 | 页面路由 | `` |  |  |
| 113 | 页面路由 | `` |  |  |
| 114 | 页面路由 | `` |  |  |
| 115 | 页面路由 | `` |  |  |
| 116 | 页面路由 | `` |  |  |
| 117 | 页面路由 | `` |  |  |
| 118 | 页面路由 | `` |  |  |
| 119 | 页面路由 | `` |  |  |
| 120 | 页面路由 | `` |  |  |
| 121 | 页面路由 | `` |  |  |
| 122 | 页面路由 | `` |  |  |
| 123 | 页面路由 | `` |  |  |
| 124 | 页面路由 | `` |  |  |
| 125 | 页面路由 | `` |  |  |
| 126 | 页面路由 | `` |  |  |
| 127 | 页面路由 | `` |  |  |
| 128 | 页面路由 | `` |  |  |
| 129 | 页面路由 | `` |  |  |
| 130 | 页面路由 | `` |  |  |
| 131 | 页面路由 | `` |  |  |
| 132 | 页面路由 | `` |  |  |
| 133 | 页面路由 | `` |  |  |
| 134 | 页面路由 | `` |  |  |
| 135 | 页面路由 | `` |  |  |
| 136 | 页面路由 | `` |  |  |
| 137 | 页面路由 | `` |  |  |
| 138 | 页面路由 | `` |  |  |
| 139 | 页面路由 | `` |  |  |
| 140 | 页面路由 | `` |  |  |
| 141 | 页面路由 | `` |  |  |
| 142 | 页面路由 | `` |  |  |
| 143 | 页面路由 | `` |  |  |
| 144 | 页面路由 | `` |  |  |
| 145 | 页面路由 | `` |  |  |
| 146 | 页面路由 | `` |  |  |
| 147 | 页面路由 | `` |  |  |
| 148 | 页面路由 | `` |  |  |
| 149 | 页面路由 | `` |  |  |
| 150 | 页面路由 | `` |  |  |
| 151 | 页面路由 | `` |  |  |
| 152 | 页面路由 | `` |  |  |
| 153 | 页面路由 | `` |  |  |
| 154 | 页面路由 | `` |  |  |
| 155 | 页面路由 | `` |  |  |
| 156 | 页面路由 | `` |  |  |
| 157 | 页面路由 | `` |  |  |
| 158 | 页面路由 | `` |  |  |
| 159 | 页面路由 | `` |  |  |
| 160 | 页面路由 | `` |  |  |
| 161 | 页面路由 | `` |  |  |
| 162 | 页面路由 | `` |  |  |
| 163 | 页面路由 | `` |  |  |
| 164 | 页面路由 | `` |  |  |
| 165 | 页面路由 | `` |  |  |
| 166 | 页面路由 | `` |  |  |
| 167 | 页面路由 | `` |  |  |
| 168 | 页面路由 | `` |  |  |
| 169 | 页面路由 | `` |  |  |
| 170 | 页面路由 | `` |  |  |
| 171 | 页面路由 | `` |  |  |
| 172 | 页面路由 | `` |  |  |
| 173 | 页面路由 | `` |  |  |
| 174 | 页面路由 | `` |  |  |
| 175 | 页面路由 | `` |  |  |
| 176 | 页面路由 | `` |  |  |
| 177 | 页面路由 | `` |  |  |
| 178 | 页面路由 | `` |  |  |
| 179 | 页面路由 | `` |  |  |
| 180 | 页面路由 | `` |  |  |
| 181 | 页面路由 | `` |  |  |
| 182 | 页面路由 | `` |  |  |
| 183 | 页面路由 | `` |  |  |
| 184 | 页面路由 | `` |  |  |
| 185 | 页面路由 | `` |  |  |
| 186 | 页面路由 | `` |  |  |
| 187 | 页面路由 | `` |  |  |
| 188 | 页面路由 | `` |  |  |
| 189 | 页面路由 | `` |  |  |
| 190 | 页面路由 | `` |  |  |
| 191 | 页面路由 | `` |  |  |
| 192 | 页面路由 | `` |  |  |
| 193 | 页面路由 | `` |  |  |
| 194 | 页面路由 | `` |  |  |
| 195 | 页面路由 | `` |  |  |
| 196 | 页面路由 | `` |  |  |
| 197 | 页面路由 | `` |  |  |
| 198 | 页面路由 | `` |  |  |
| 199 | 页面路由 | `` |  |  |
| 200 | 页面路由 | `` |  |  |
| 201 | 页面路由 | `` |  |  |
| 202 | 页面路由 | `` |  |  |
| 203 | 页面路由 | `` |  |  |
| 204 | 页面路由 | `` |  |  |
| 205 | 页面路由 | `` |  |  |
| 206 | 页面路由 | `` |  |  |
| 207 | 页面路由 | `` |  |  |
| 208 | 页面路由 | `` |  |  |
| 209 | 页面路由 | `` |  |  |
| 210 | 页面路由 | `` |  |  |
| 211 | 页面路由 | `` |  |  |
| 212 | 页面路由 | `` |  |  |
| 213 | 页面路由 | `` |  |  |
| 214 | 页面路由 | `` |  |  |
| 215 | 页面路由 | `` |  |  |
| 216 | 页面路由 | `` |  |  |
| 217 | 页面路由 | `` |  |  |
| 218 | 页面路由 | `` |  |  |
| 219 | 页面路由 | `` |  |  |
| 220 | 页面路由 | `` |  |  |
| 221 | 页面路由 | `` |  |  |
| 222 | 页面路由 | `` |  |  |
| 223 | 页面路由 | `` |  |  |
| 224 | 页面路由 | `` |  |  |
| 225 | 页面路由 | `` |  |  |
| 226 | 页面路由 | `` |  |  |
| 227 | 页面路由 | `` |  |  |
| 228 | 页面路由 | `` |  |  |
| 229 | 页面路由 | `` |  |  |
| 230 | 页面路由 | `` |  |  |
| 231 | 页面路由 | `` |  |  |
| 232 | 页面路由 | `` |  |  |
| 233 | 页面路由 | `` |  |  |
| 234 | 页面路由 | `` |  |  |
| 235 | 页面路由 | `` |  |  |
| 236 | 页面路由 | `` |  |  |
| 237 | 页面路由 | `` |  |  |
| 238 | 页面路由 | `` |  |  |
| 239 | 页面路由 | `` |  |  |
| 240 | 页面路由 | `` |  |  |
| 241 | 页面路由 | `` |  |  |
| 242 | 页面路由 | `` |  |  |
| 243 | 页面路由 | `` |  |  |
| 244 | 页面路由 | `` |  |  |
| 245 | 页面路由 | `` |  |  |
| 246 | 页面路由 | `` |  |  |
| 247 | 页面路由 | `` |  |  |
| 248 | 页面路由 | `` |  |  |
| 249 | 页面路由 | `` |  |  |
| 250 | 页面路由 | `` |  |  |
| 251 | 页面路由 | `` |  |  |
| 252 | 页面路由 | `` |  |  |
| 253 | 页面路由 | `` |  |  |
| 254 | 页面路由 | `` |  |  |
| 255 | 页面路由 | `` |  |  |
| 256 | 页面路由 | `` |  |  |
| 257 | 页面路由 | `` |  |  |
| 258 | 页面路由 | `` |  |  |
| 259 | 页面路由 | `` |  |  |
| 260 | 页面路由 | `` |  |  |
| 261 | 页面路由 | `` |  |  |
| 262 | 页面路由 | `` |  |  |
| 263 | 页面路由 | `` |  |  |
| 264 | 页面路由 | `` |  |  |
| 265 | 页面路由 | `` |  |  |
| 266 | 页面路由 | `` |  |  |
| 267 | 页面路由 | `` |  |  |
| 268 | 页面路由 | `` |  |  |
| 269 | 页面路由 | `` |  |  |
| 270 | 页面路由 | `` |  |  |
| 271 | 页面路由 | `` |  |  |
| 272 | 页面路由 | `` |  |  |
| 273 | 页面路由 | `` |  |  |
| 274 | 页面路由 | `` |  |  |
| 275 | 页面路由 | `` |  |  |
| 276 | 页面路由 | `` |  |  |
| 277 | 阶段 | `` |  |  |
| 278 | 文件访问 | `facts/documents.json` |  | write |
| 279 | 文件访问 | `pages/index.json` |  | write |
| 280 | 阶段 | `` |  |  |
| 281 | 阶段 | `` |  |  |
| 282 | 文件访问 | `facts/documents.json` |  | read |
| 283 | 文件访问 | `raw/601857_中国石油_中国石油天然气股份有限公司2025年年报_2026-03-30.pdf` |  | read |
| 284 | 文件访问 | `channels/text_layer.json` |  | write |
| 285 | 文件访问 | `channels/page_layout.json` |  | write |
| 286 | 阶段 | `` |  |  |
| 287 | 阶段 | `` |  |  |
| 288 | 阶段 | `` |  |  |
| 289 | 文件访问 | `facts/documents.json` |  | read |
| 290 | 文件访问 | `pages/index.json` |  | read |
| 291 | 文件访问 | `raw/601857_中国石油_中国石油天然气股份有限公司2025年年报_2026-03-30.pdf` |  | read |
| 292 | 页面路由 | `` |  |  |
| 293 | 页面路由 | `` |  |  |
| 294 | 页面路由 | `` |  |  |
| 295 | 页面路由 | `` |  |  |
| 296 | 页面路由 | `` |  |  |
| 297 | 页面路由 | `` |  |  |
| 298 | 页面路由 | `` |  |  |
| 299 | 页面路由 | `` |  |  |
| 300 | 页面路由 | `` |  |  |
| 301 | 页面路由 | `` |  |  |
| 302 | 页面路由 | `` |  |  |
| 303 | 页面路由 | `` |  |  |
| 304 | 页面路由 | `` |  |  |
| 305 | 页面路由 | `` |  |  |
| 306 | 页面路由 | `` |  |  |
| 307 | 页面路由 | `` |  |  |
| 308 | 页面路由 | `` |  |  |
| 309 | 页面路由 | `` |  |  |
| 310 | 页面路由 | `` |  |  |
| 311 | 页面路由 | `` |  |  |
| 312 | 页面路由 | `` |  |  |
| 313 | 页面路由 | `` |  |  |
| 314 | 页面路由 | `` |  |  |
| 315 | 页面路由 | `` |  |  |
| 316 | 页面路由 | `` |  |  |
| 317 | 页面路由 | `` |  |  |
| 318 | 页面路由 | `` |  |  |
| 319 | 页面路由 | `` |  |  |
| 320 | 页面路由 | `` |  |  |
| 321 | 页面路由 | `` |  |  |
| 322 | 页面路由 | `` |  |  |
| 323 | 页面路由 | `` |  |  |
| 324 | 页面路由 | `` |  |  |
| 325 | 页面路由 | `` |  |  |
| 326 | 页面路由 | `` |  |  |
| 327 | 页面路由 | `` |  |  |
| 328 | 页面路由 | `` |  |  |
| 329 | 页面路由 | `` |  |  |
| 330 | 页面路由 | `` |  |  |
| 331 | 页面路由 | `` |  |  |
| 332 | 页面路由 | `` |  |  |
| 333 | 页面路由 | `` |  |  |
| 334 | 页面路由 | `` |  |  |
| 335 | 页面路由 | `` |  |  |
| 336 | 页面路由 | `` |  |  |
| 337 | 页面路由 | `` |  |  |
| 338 | 页面路由 | `` |  |  |
| 339 | 页面路由 | `` |  |  |
| 340 | 页面路由 | `` |  |  |
| 341 | 页面路由 | `` |  |  |
| 342 | 页面路由 | `` |  |  |
| 343 | 页面路由 | `` |  |  |
| 344 | 页面路由 | `` |  |  |
| 345 | 页面路由 | `` |  |  |
| 346 | 页面路由 | `` |  |  |
| 347 | 页面路由 | `` |  |  |
| 348 | 页面路由 | `` |  |  |
| 349 | 页面路由 | `` |  |  |
| 350 | 页面路由 | `` |  |  |
| 351 | 页面路由 | `` |  |  |
| 352 | 页面路由 | `` |  |  |
| 353 | 页面路由 | `` |  |  |
| 354 | 页面路由 | `` |  |  |
| 355 | 页面路由 | `` |  |  |
| 356 | 页面路由 | `` |  |  |
| 357 | 页面路由 | `` |  |  |
| 358 | 页面路由 | `` |  |  |
| 359 | 页面路由 | `` |  |  |
| 360 | 页面路由 | `` |  |  |
| 361 | 页面路由 | `` |  |  |
| 362 | 页面路由 | `` |  |  |
| 363 | 页面路由 | `` |  |  |
| 364 | 页面路由 | `` |  |  |
| 365 | 页面路由 | `` |  |  |
| 366 | 页面路由 | `` |  |  |
| 367 | 页面路由 | `` |  |  |
| 368 | 页面路由 | `` |  |  |
| 369 | 页面路由 | `` |  |  |
| 370 | 页面路由 | `` |  |  |
| 371 | 页面路由 | `` |  |  |
| 372 | 页面路由 | `` |  |  |
| 373 | 页面路由 | `` |  |  |
| 374 | 页面路由 | `` |  |  |
| 375 | 页面路由 | `` |  |  |
| 376 | 页面路由 | `` |  |  |
| 377 | 页面路由 | `` |  |  |
| 378 | 页面路由 | `` |  |  |
| 379 | 页面路由 | `` |  |  |
| 380 | 页面路由 | `` |  |  |
| 381 | 页面路由 | `` |  |  |
| 382 | 页面路由 | `` |  |  |
| 383 | 页面路由 | `` |  |  |
| 384 | 页面路由 | `` |  |  |
| 385 | 页面路由 | `` |  |  |
| 386 | 页面路由 | `` |  |  |
| 387 | 页面路由 | `` |  |  |
| 388 | 页面路由 | `` |  |  |
| 389 | 页面路由 | `` |  |  |
| 390 | 页面路由 | `` |  |  |
| 391 | 页面路由 | `` |  |  |
| 392 | 页面路由 | `` |  |  |
| 393 | 页面路由 | `` |  |  |
| 394 | 页面路由 | `` |  |  |
| 395 | 文件访问 | `channels/ocr.json` |  | write |
| 396 | 文件访问 | `channels/ocr_skipped.json` |  | write |
| 397 | 阶段 | `` |  |  |
| 398 | 阶段 | `` |  |  |
| 399 | 阶段 | `` |  |  |
## 八、表格解析与单元格级裁决

- 识别到表格：**276** 张（其中有框线 22 张）
- 已验算合计线：**84** 条，发现不平 **50** 处（13 张表）
- 表内无合计线、算术上**无从校验**：**231** 张
- 分层小计（合计只覆盖部分分项，不判失败）：8 条

### 单元格级视觉裁决：88 张表被复核

| 结论 | 格数 | 说明 |
|---|---|---|
| 一致 | 276 | 模型读数与程序读数一致 |
| 格式归一 | 0 | 数字未变，仅修复换行切断（原本取不到数） |
| 改值 | 0 | 不一致且通过全部闸门 |
| 未采纳 | 83 | 不一致但未过闸门 → **只标记不改** |
| 读不出 | 0 | 模型也没读出内容 |
| 出错 | 1 | 裁剪或调用失败 |


---

本报告由 `agent.trace.audit` 自动生成，数据来源为 `trace.jsonl` 与 `manifest.json`，未经人工修饰。
