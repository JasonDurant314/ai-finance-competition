"""领域管线层：R1 提取 / R4 估值 / R5 核查。

每条管线由 ``pipeline.yaml`` 声明阶段序列，由 ``agent.runtime.loop.Loop``
调度执行。阶段之间通过 ``runs/<run_id>/`` 下的文件产物衔接。

三条管线的数据流：
    r1_extract  ->  facts/facts.json          （事实库 = 唯一数字真源）
    r4_valuation -> valuation/valuation.json  （消费 facts.json）
    r5_audit    ->  audit/audit.json          （消费 facts.json）
"""

__all__: list[str] = []
