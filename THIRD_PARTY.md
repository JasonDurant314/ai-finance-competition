# 第三方组件声明

> 依据赛题要求：「使用第三方开源项目、模型、数据或代码的，应在项目说明中列明
> 名称、版本、来源、许可证及具体使用范围，不得将第三方成果冒充为自主开发成果。」

本文件逐条列明本项目使用的全部第三方组件。未在此列出的组件即为我们自主实现。

---

## 一、自主实现范围（无第三方框架）

以下模块完全自主实现，未使用任何现成 Agent 框架：

| 模块 | 说明 |
|---|---|
| `agent/runtime/` | 两级调度器（宏观 Plan DAG + 微观有界 Agent Loop）、预算闸门、异常体系 |
| `agent/model/gateway.py` | 模型网关：多角色路由、缓存、成本记账、结构化输出自我修复 |
| `agent/model/cache.py` | 响应缓存（可复现性的技术基础） |
| `agent/tools/` | 工具注册表、`@tool` 装饰器、文件系统守卫（沙箱） |
| `agent/prompts/` | 提示词注册表（版本 + SHA256 指纹） |
| `agent/skills/` | 领域技能注册表（加载 `domain/skills/*/SKILL.md`，并校验其声明的工具与提示词真实存在） |
| `agent/mcp/` | MCP 协议客户端（JSON-RPC 2.0 over stdio，手写实现，零新增依赖；环境变量白名单隔离，不传递任何凭据） |
| `agent/validate/` | 单位归一化、quote 回读闸门、财务勾稽断言、Schema 校验 |
| `agent/evidence/` | 数据契约、证据存储、事实账本、置信度模型 |
| `agent/trace/` | 全量事件追踪、复现校验、审计报告生成 |
| `pipelines/` | R1 / R4 / R5 三条管线及端到端串联 |
| `domain/tools/` | PDF 解析封装、图像预处理、OCR 统一接口、表格三引擎表决、VLM 裁判、DCF/相对估值/敏感性、E1–E5 核查规则 |

明确声明：本项目未使用 LangChain、LangGraph、LlamaIndex、AutoGen、CrewAI、
Semantic Kernel 或任何其他 Agent 编排框架。

---

## 二、大语言模型

| 名称 | 版本 | 来源 | 许可 / 使用方式 | 使用范围 |
|---|---|---|---|---|
| DeepSeek-V4.1-Flash | `deepseek-flash`（2026-09-10 发布） | 深度求索 [platform.deepseek.com](https://platform.deepseek.com/) | 商业 API 调用（按 token 计费）。模型权重不随本作品分发 | ① 字段抽取的结构化输出<br>② 多模态裁判（对照页面图像判断字段取值）<br>③ 报告文字撰写<br>④ 冲突裁决 |

**模型调用方式**：通过 OpenAI 兼容的 `POST /chat/completions` 接口，
Base URL `https://api.deepseek.com/v1`，API Key 由环境变量 `DEEPSEEK_API_KEY` 提供。
密钥不写入任何代码或产物。

**为何选用该模型**：赛题鼓励采用国产模型；该模型原生支持多模态视觉理解，
使三层解析引擎的第三层「VLM 裁判」能够复用同一模型与同一套凭据，
避免了引入第二家视觉模型带来的部署与合规复杂度。

模型能力边界：模型存在幻觉风险。本项目不依赖模型自身的可靠性，
而是通过 `quote` 回读闸门、多通道表决、确定性算术校验三重机制约束其输出
（详见 README「防幻觉的四条铁律」）。

---

## 三、Python 依赖

版本号为本仓库 `.venv` 实测（2026-10-10）。完整锁定见 `requirements.txt`。

### 必需依赖

| 包名 | 版本 | 许可 | 用途与使用范围 |
|---|---|---|---|
| pymupdf | 1.28.2 | AGPL-3.0（见下方风险说明） | PDF 文本层提取、页面渲染、坐标获取、图形线段分析 |
| pydantic | 2.13.5 | MIT | 全局数据契约定义与运行时校验 |
| httpx | 0.28.1 | BSD-3-Clause | 模型 API 的 HTTP 客户端 |
| PyYAML | 6.0.3 | MIT | 配置文件解析 |
| pandas | 3.0.6 | BSD-3-Clause | 事实表与估值模型的 Excel 导出 |
| openpyxl | 3.1.5 | MIT | `.xlsx` 写入引擎 |
| matplotlib | 3.11.2 | PSF-based（BSD 兼容） | 敏感性热力图、龙卷风图、估值区间图、FCFF 瀑布图 |
| Jinja2 | 3.1.6 | BSD-3-Clause | 报告与 Web 页面模板渲染 |
| numpy | 2.4.6 | BSD-3-Clause | 敏感性网格计算、图像投影方差计算 |
| Pillow | 12.3.0 | HPND（MIT 兼容） | 扫描件图像预处理（灰度、纠偏、去噪） |

### 可选依赖（缺失时功能降级，有明确提示）

| 包名 | 版本 | 许可 | 用途与降级行为 |
|---|---|---|---|
| rapidocr-onnxruntime | 未安装（本仓库 `.venv` 未装） | Apache-2.0 | 扫描件 OCR 引擎（纯 CPU）。缺失时：若存在必须依赖 OCR 的页面则显式失败并给出安装指引；仅图文混排页需补强时降级并记录缺口 |
| jsonschema | 未安装（本仓库 `.venv` 未装） | MIT | 严格 JSON Schema 校验。缺失时降级为轻量结构校验（`agent validate/schema.py` 内置） |
| pytest | 9.1.1 | MIT | 测试框架（仅开发期使用，不进入运行时） |
| fastapi / uvicorn | 0.142.2 / 0.54.0 | MIT / BSD-3-Clause | Web 演示台（决赛现场演示用） |

### 备选依赖（未启用，仅记录选型考量）

| 包名 | 许可 | 说明 |
|---|---|---|
| pypdfium2 | Apache-2.0 / BSD-3-Clause | PDF 渲染的许可证友好替代品。若 AGPL 条款不适用，可通过 `configs/routing.yaml` 的 `parser_backend` 切换 |
| paddleocr | Apache-2.0 | OCR 备选引擎，识别效果略好但依赖较重、CPU 推理较慢 |

---

## 四、许可证风险说明：PyMuPDF (AGPL-3.0)

这是本项目唯一的许可证风险点，披露如下。

- 组件：PyMuPDF（Python bindings for MuPDF）
- 许可证：GNU Affero General Public License v3.0（AGPL-3.0）
- 使用范围：仅在 `domain/tools/document.py` 中用于 PDF 文本层提取、
  页面渲染、坐标获取与图形线段分析。这是一切 PDF 处理的底层能力。

AGPL-3.0 的关键义务：若通过网络向第三方提供服务，需向使用者提供完整源码。
本作品作为竞赛提交物，源码完整提交给评审，不通过网络对外提供服务，
因此当前使用方式不构成 AGPL 违规。

若后续需要商用或开源发布，必须处理。可选方案：

1. 切换至 pypdfium2（Apache-2.0 / BSD-3-Clause）
   —— 代码已做后端抽象，`configs/routing.yaml` 中改 `parser_backend: pypdfium2` 即可
2. 向 Artifex 购买 PyMuPDF 商业许可
3. 将 PDF 解析拆分为独立进程，通过进程边界隔离（存在法律解释风险，不推荐）

当前状态：使用 PyMuPDF，并在此明确声明。

---

## 五、数据来源

| 数据类型 | 来源 | 许可 / 使用范围 |
|---|---|---|
| 演示样例材料 | 本项目自行合成的虚构数据 | 由 `tools/make_sample_data.py` 生成，不含任何真实公司信息，可自由分发 |
| 真实投研材料 | 由团队从公开渠道获取（交易所公告、定期报告） | 仅用于竞赛演示与分析，不对外分发原始文件 |
| 可比公司数据 | 团队整理的离线静态样本 | 演示用途；真实使用时须替换并声明来源 |

**样本数据声明**：`data/samples/` 与 `data/raw/` 中的
`sample_pledge_announcement.pdf`、`sample_annual_report.pdf`
均为虚构的合成数据，公司名称、股票代码（600519 仅为占位）、
财务数字均为编造，不得作为任何投资或研究依据。

---

## 六、方法论参考（非代码依赖）

以下为公开的方法论与规范，本项目的实现参考了其思想而非代码：

| 名称 | 来源 | 参考内容 |
|---|---|---|
| 企业价值评估准则 | 中国资产评估协会 | DCF 模型结构、FCFF 口径定义 |
| 上市公司信息披露规范 | 中国证监会 / 证券交易所 | 财务指标口径（归母/扣非/合并/母公司）定义 |
| Model Context Protocol (MCP) | Anthropic 公开规范 | JSON-RPC 2.0 通信协议格式。客户端为自主实现 |
| JSON Schema Draft 2020-12 | IETF / json-schema.org | 契约描述格式 |

---

## 七、声明

1. 本项目为 2026 年北京市大学生金融人工智能竞赛参赛作品。
2. 除上述列明的第三方组件外，其余代码、架构设计、提示词、核查规则、
   估值模型与数据结构均由团队自主开发。
3. 所有在 `THIRD_PARTY.md` 中列明的组件均按其原许可证使用，
   未修改其源代码，未移除其版权声明。
4. 模型权重与商业软件源代码不随本作品提交；
   模型的名称、版本、调用方式及使用范围已在上文完整列明。

---

*最后更新：2026-10*
