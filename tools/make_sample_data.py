"""生成合成测试材料 —— 让整条流水线在**零外部数据**下就能端到端跑通。

用途：
    * P1 阶段骨架自检
    * CI 回归测试的固定输入
    * 新队员上手时的第一份可跑数据

生成两类材料：
    1. ``sample_pledge_announcement.pdf`` —— 股权质押公告（覆盖 R1 的公告类字段）
    2. ``sample_annual_report.pdf``       —— 简版年报（含财务报表表格，覆盖 R1 的财务科目 + R4 的输入）

注意：这是**合成数据**，只用于流程验证。真实评测必须使用 ``data/raw/`` 下的
真实材料 —— 合成数据无法暴露扫描件质量、表格排版等真实问题。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.console import enable_utf8_output  # noqa: E402

# Windows 控制台默认 GBK：本脚本会打印 ✓ / ✅ 这类符号，
# 不先切 UTF-8 就会在**最需要它的时候**抛 UnicodeEncodeError 中断。
enable_utf8_output()

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_pdf(pages: list[str], out_path: Path, *, font_size: int = 11) -> None:
    """把若干页文本写成 PDF。

    ⚠️ 重要：``Page.insert_textbox`` 在内容放不下时返回**负值并静默丢弃**
    超出部分。这会让我们以为生成了完整样本，实际却缺内容 —— 曾经因此
    出现过整页为空的诡异现象。因此这里必须：检查返回值 → 自动按行试错
    分块（不靠估算行高）→ 仍放不下则抛异常，绝不静默丢内容。
    """
    import fitz

    fontname = "china-s"  # PyMuPDF 内置简体中文字体
    margin = 56.0
    page_w, page_h = 595.0, 842.0
    rect = fitz.Rect(margin, margin, page_w - margin, page_h - margin)

    doc = fitz.open()
    pages_created = 0

    for text in pages:
        remaining = text.split("\n")
        while remaining:
            n = _max_fit(doc, rect, page_w, page_h, remaining, font_size, fontname)
            if n <= 0:
                doc.close()
                raise RuntimeError(
                    "单行都放不下，font_size 过大或页面尺寸过小。"
                    f"首行：{remaining[0][:60]!r}"
                )
            page = doc.new_page(width=page_w, height=page_h)
            page.insert_textbox(
                rect, "\n".join(remaining[:n]), fontsize=font_size,
                fontname=fontname, align=fitz.TEXT_ALIGN_LEFT,
            )
            pages_created += 1
            remaining = remaining[n:]

    if pages_created == 0:
        doc.close()
        raise RuntimeError("没有生成任何页面")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    doc.close()

    # 回读校验：确认每页都有内容，防止静默丢内容
    with fitz.open(out_path) as check:
        empty = [
            i + 1
            for i in range(check.page_count)
            if len(check.load_page(i).get_text("text").strip()) < 20
        ]
    if empty:
        raise RuntimeError(f"生成结果中第 {empty} 页内容为空，分页逻辑有误")


def _max_fit(
    doc, rect, page_w: float, page_h: float, lines: list[str],
    font_size: float, fontname: str,
) -> int:
    """在临时页面上试排，返回本页能容纳的最大行数。

    临时页必须使用**与正式页完全相同的尺寸**，否则 textbox 高度会不同，
    二分结果将失效。
    """
    lo, hi, best = 1, len(lines), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        tmp = doc.new_page(width=page_w, height=page_h)
        leftover = tmp.insert_textbox(
            rect, "\n".join(lines[:mid]), fontsize=font_size, fontname=fontname
        )
        doc.delete_page(doc.page_count - 1)
        if leftover >= 0:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


PLEDGE_PAGES = [
    """证券代码：600519        证券简称：示例股份        公告编号：2025-041

            示例股份有限公司
      关于控股股东部分股份质押的公告

本公司董事会及全体董事保证本公告内容不存在任何虚假记载、
误导性陈述或者重大遗漏，并对其内容的真实性、准确性和完整性
承担个别及连带责任。

重要内容提示：
  · 本次质押股份数量：12,004.50 万股
  · 占其所持股份比例：45.30%
  · 占公司总股本比例：12.40%
  · 质押期限：2025年3月15日至2027年3月14日
  · 是否为限售股：否

一、本次质押基本情况

    本公司于2025年3月14日接到控股股东示例控股集团有限公司
（以下简称"示例控股"）的通知，获悉其所持有本公司的部分股份
被质押，具体事项如下：

    出质人：示例控股集团有限公司
    质权人：中国工商银行股份有限公司北京分行
    质押股份数量：12,004.50 万股
    占其所持股份比例：45.30%
    占公司总股本比例：12.40%
    质押登记日：2025年3月14日
    质押到期日：2027年3月14日

二、股东股份累计质押情况

    截至本公告披露日，示例控股及其一致行动人累计质押股份
15,230.00 万股，占其所持股份比例 57.48%，占公司总股本
比例 15.73%。

三、其他说明

    示例控股资信状况良好，具备相应的资金偿还能力，本次质押
风险可控，不会导致公司实际控制权发生变更。
""",
    """证券代码：600519        证券简称：示例股份        公告编号：2025-041

四、备查文件

    1. 中国证券登记结算有限责任公司股份冻结明细表；
    2. 质押合同；
    3. 上海证券交易所要求的其他文件。

    特此公告。

                                        示例股份有限公司董事会
                                        二〇二五年三月十四日
""",
]


ANNUAL_PAGES = [
    """示例股份有限公司 2025 年年度报告（摘要）

第一节 重要提示

    本年度报告摘要来自年度报告全文，为全面了解本公司的经营成果、
财务状况及未来发展规划，投资者应当到上海证券交易所网站仔细阅读
年度报告全文。

    公司 2025 年度财务报告已经天健会计师事务所（特殊普通合伙）
审计，并出具了标准无保留意见的审计报告。

第二节 公司基本情况

    证券代码：600519
    证券简称：示例股份
    总股本：968,000,000 股
    报告期末普通股股东总数：48,215 户
""",
    """第三节 主要会计数据和财务指标

    单位：万元

    营业收入：1,200,450.00 万元，上年同期 1,112,300.00 万元
    归属于母公司股东的净利润：245,600.00 万元，上年同期 228,900.00 万元
    归属于母公司股东的扣除非经常性损益的净利润：238,200.00 万元
    经营活动产生的现金流量净额：312,450.00 万元
    基本每股收益：2.5370 元/股
    加权平均净资产收益率：15.28%

第四节 合并资产负债表主要数据

    单位：万元

    资产总计：2,845,600.00 万元
    负债合计：1,120,300.00 万元
    所有者权益合计：1,725,300.00 万元
    归属于母公司所有者权益合计：1,680,500.00 万元
    少数股东权益：44,800.00 万元
    货币资金：680,200.00 万元
    短期借款：85,000.00 万元
    长期借款：120,000.00 万元
    应付债券：50,000.00 万元
""",
    """第五节 合并利润表主要数据

    单位：万元

    营业收入：1,200,450.00 万元
    营业成本：620,300.00 万元
    销售费用：98,400.00 万元
    管理费用：72,600.00 万元
    研发费用：45,200.00 万元
    财务费用：8,900.00 万元
    营业利润：318,500.00 万元
    利润总额：316,800.00 万元
    所得税费用：66,500.00 万元
    净利润：250,300.00 万元
    少数股东损益：4,700.00 万元
    归属于母公司股东的净利润：245,600.00 万元
    非经常性损益：7,400.00 万元
""",
    """第六节 合并现金流量表主要数据

    单位：万元

    经营活动产生的现金流量净额：312,450.00 万元
    投资活动产生的现金流量净额：-145,200.00 万元
    筹资活动产生的现金流量净额：-99,450.00 万元
    购建固定资产、无形资产和其他长期资产支付的现金：88,600.00 万元
    固定资产折旧：62,300.00 万元
""",
    """第七节 经营情况讨论与分析

    报告期内，公司实现营业收入 1,200,450.00 万元，同比增长 7.92%；
实现归属于母公司股东的净利润 245,600.00 万元，同比增长 7.30%。

    公司毛利率为 48.33%，较上年同期提升 0.30 个百分点，主要
得益于产品结构优化与规模效应显现。

    报告期内，公司经营活动产生的现金流量净额为 312,450.00 万元，
与净利润的比值为 1.25 倍，盈利质量良好。
""",
]


def main() -> int:
    samples = PROJECT_ROOT / "data" / "samples"
    raw = PROJECT_ROOT / "data" / "raw"

    _write_pdf(PLEDGE_PAGES, samples / "sample_pledge_announcement.pdf")
    print(f"✅ {samples / 'sample_pledge_announcement.pdf'}")

    _write_pdf(ANNUAL_PAGES, samples / "sample_annual_report.pdf", font_size=10)
    print(f"✅ {samples / 'sample_annual_report.pdf'}")

    # 同时放入 raw/ 以便直接跑 R1（如已存在真实材料则不覆盖）
    raw.mkdir(parents=True, exist_ok=True)
    existing = [p for p in raw.glob("*.pdf")]
    if not existing:
        import shutil

        shutil.copy2(
            samples / "sample_pledge_announcement.pdf",
            raw / "sample_pledge_announcement.pdf",
        )
        shutil.copy2(
            samples / "sample_annual_report.pdf", raw / "sample_annual_report.pdf"
        )
        print(f"✅ 已复制到 data/raw/（原本为空）")
    else:
        print(f"ℹ️  data/raw/ 已有 {len(existing)} 个 PDF，未覆盖")

    return 0


if __name__ == "__main__":
    sys.exit(main())
