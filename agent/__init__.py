"""自研金融投研 Agent Harness。

本包为完全自主实现的智能体编排框架，**不依赖任何第三方 Agent 框架**
（无 LangChain / LangGraph / LlamaIndex / AutoGen / CrewAI）。

模块划分
--------
runtime/    调度主循环、预算控制、上下文构建、错误类型
model/      模型网关（多角色、缓存、限流、成本记账）
tools/      工具注册表、装饰器、沙箱与文件访问守卫
prompts/    提示词注册表（带版本与哈希）
skills/     领域技能注册表（加载 domain/skills/*/SKILL.md，并校验其绑定的工具与提示词真实存在）
mcp/        MCP 协议客户端（JSON-RPC 2.0 over stdio，自研实现）
validate/   Schema / 数值 / quote 回读 / 财务勾稽断言
evidence/   数据契约、证据存储、事实账本
trace/      追踪、回放、审计报告

⚠️ 本清单必须与磁盘上的实际目录一致。曾在文档里写过 ``verify/``、``mcp/``
这类**当时并不存在的模块** —— 运行期不会有任何报错，但评审点开目录就穿帮。
现在 ``tests/test_static_health.py`` 会核对这份清单。
"""

__version__ = "0.1.0"
