# 复现报告 — run `demo`

**结论：✅ 可复现**

## 产物哈希比对

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

## 说明

- 本次运行没有模型调用（mock 或纯确定性流水线），无需模型响应缓存

## 追踪汇总

```json
{
  "total_events": 146,
  "llm_calls": 0,
  "llm_cache_hits": 0,
  "cache_hit_rate": null,
  "tool_calls": 0,
  "file_accesses": 71,
  "unique_files": 38,
  "assertions": 26,
  "assertions_passed": 11,
  "assertions_failed": 0,
  "assertions_skipped": 15,
  "prompt_tokens": 0,
  "completion_tokens": 0,
  "cost_cny": 0,
  "errors": [],
  "started_at": "2026-10-16T17:22:06.188094+00:00",
  "finished_at": "2026-10-16T17:22:13.321625+00:00"
}
```

