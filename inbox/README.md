# 收件目录（inbox）

把要分析的 PDF 丢进来，然后在 Web 端 `/analyze` 点一次「开始分析」即可。

    inbox/
      raw/        年报 / 公告 PDF —— **放这里**（递归查找，只认 .pdf）
      drafts/     可选：研究报告草稿 .md / .txt，用于 R5 纠错核查
      comps/      R4 可比公司快照（缺失时自动从 data/comps 补齐）
      market/     R4 市场利率缓存（缺失时自动从 data/market 补齐）

说明：

* PDF 直接放在 `inbox/` 根目录下也可以，效果一样。
* 分析产出落在 `runs/<run_id>/`，与命令行 `agent run` 完全同构，
  Web 端各选题视图（/library /extract /facts /valuation /audit /runs）原样可看。
* 本目录不入版本库（见 `.gitignore`）：里面放的是你自己的材料。
