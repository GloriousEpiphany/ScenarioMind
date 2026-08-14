# Quant-Finance：情景推演 V3.1 深化方案

> 版本：V3.1 数据源组合修订稿
>
> 审查日期：2026-08-06
>
> 适用范围：StockM1 的 `scenario_engine`、`Agents_Experience` 情绪/交易上下文、`visualization/scenario_page.py` 及相关测试
>
> 目标：把现有“可审计压力测试骨架”升级为由 Agent 自动收集点时数据、识别事件、分析情绪/财报/行业、生成分支、量化组合风险并比较持仓方案的无人值守研究闭环。

---

## 0. 结论先行

当前系统已经实现了自然语言情景解析、本地行情压力测试、历史类比、简化因子、外部证据入库、规则化市场沙盘、假设分支、报告和审计。它不是空壳。

但它目前仍然是：

> **可审计的本地确定性压力测试骨架**，尚未实现“市场情绪 + 财报 + 行业 + 真实事件”的点时数据闭环、可校准的事件条件化模型，以及真正能规划工具、处理失败并完成任务的自治 Agent 投研闭环。

V3 不应继续堆更多带 `Agent` 后缀的类。最优先的升级顺序是：

1. 先建立严格的 **Point-in-Time（PIT，点时）市场快照**，堵住历史推演读取未来证据的风险。
2. 再把事件、情绪、财报、行业、资金行为转成可回测特征，而不是直接让大模型讲故事或生成收益率。
3. 用事件研究、因子模型、历史相似状态和稳健区间共同形成 **事件条件化收益分布**。
4. 让 Agent 驱动数据查询、缺口补全、模型选择、分支预算、失败降级和审查，但把所有数值计算留在确定性量化 Module 中。
5. 最终用多分支损失矩阵和组合约束自动生成防守、均衡、进取/观察方案，并自动送回同一引擎复测。

数据源不再绑定单一平台。专业部署的推荐组合是：**RQData 负责 PIT 行情、财务、行业/风险模型和已授权的新闻舆情，TuShare Pro 补充公告、政策、新闻、资金流与结构化公司事件，AKShare 延续当前项目已有的低成本广覆盖和故障兜底**。三者不做平权轮询，由 `RoutingPolicy` 按数据域、历史时点、权限、质量和预算自动选源；CNINFO、交易所、央行等官方原文仍是事实核验链。

用户每次只需要提供持仓和研究目标。数据源选择、财报收集、情绪分析、行业分析、事件分支、模型运行、持仓方案比较、审计和报告均由 Agent 完成。部署时一次性配置数据凭证不算运行过程中的人工参与；真实下单仍不属于情景推演模块职责。

---

## 1. 本方案的研究依据与使用方式

### 1.1 已核对材料

- `Docs/情景推演改进V2.md`：V2.2 已提出证据链、金融图谱、主体沙盘、未来事件假设和递归推演。
- 当前项目代码：重点核对了 `scenario_engine/engine.py`、研究证据层、量化计算、沙盘、递归分支、持仓方案、持久化、前端和测试；也核对了 `Agents_Experience/data_sources` 中独立存在的情绪链，以及已经使用东方财富、腾讯和 AKShare 的 `src/stock_app/data_downloader.py`。
- [Introduction-to-Quantitative-Finance](https://github.com/Barca0412/Introduction-to-Quantitative-Finance)：该仓库本质上是量化金融资料索引和 AI + Finance 论文雷达，而不是一套可直接复制的生产架构。对本项目最有价值的启示是：完整投研必须同时覆盖数据、因子、回测、组合优化和风险模型，AI 应用于因子研究和自动化 R&D，而不是取代点时数据与样本外检验。
- [AKShare 官方概览](https://akshare.akfamily.xyz/introduction.html)、[股票数据字典](https://akshare.akfamily.xyz/data/stock/stock.html) 与 [数据风险说明](https://akshare.akfamily.xyz/data_tips.html)：已确认股票行情、个股新闻、资金、行业、热度/关注等广覆盖能力；其数据来自公开网页源，官方同时提示学术研究用途和商业使用风险，且目标网页变化可能导致端点变化。
- [TuShare 官方数据目录](https://tushare.pro/document/2)：已确认结构化行情、财务、披露日、行业、资金、公告、政策、新闻、研究报告和交易所互动问答等数据域。具体端点权限、积分、频率和历史深度必须运行时探测。
- [RQData Python 文档](https://www.ricequant.com/doc/rqdata/python/index-rqdatac)、[A 股数据](https://www.ricequant.com/doc/rqdata/python/stock-mod)、[另类数据](https://www.ricequant.com/doc/rqdata/python/alternative-data) 与 [风险因子](https://www.ricequant.com/doc/rqdata/python/risk-factors-mod)：已确认 PIT 财务、行情、行业、因子/风险模型、一致预期和个股新闻舆情；部分能力需要额外包或单独商务授权，不能只凭基础账号推定可用。

### 1.2 不照搬外部资料的部分

- 不把“深度学习预测股价”当作主产品承诺。点预测误差大、易受制度切换影响，组合决策更需要条件分布、尾部损失和失效条件。
- 不把情绪分数直接乘到收益率上。
- 不把大模型生成的事件概率当作统计概率。
- 不把开源项目清单等同于本项目依赖清单。MVP 优先使用现有 Python、SQLite、pandas、scikit-learn/现有依赖可实现的 Module。
- 不自动执行真实交易。系统自动完成研究和候选方案生成，交易指令和持牌投顾职责仍留在系统之外。

### 1.3 外部量化金融资料如何落到本项目

外部仓库提供的是知识地图，不是可直接搬入的系统。V3 只吸收能被当前代码、数据契约和测试承接的部分：

| 外部主题 | 对 V3 的具体影响 | 落地位置 | 不采用的捷径 |
|---|---|---|---|
| 数据源与另类数据 | 所有行情、财报、行业、资金和文本特征都进入统一 PIT Artifact；另类数据必须有许可、时间戳、实体映射和去重 | `PointInTimeMarketSnapshot`、`EventSentiment` | 抓到网页或热榜就直接当交易信号 |
| 因子挖掘 | 因子必须登记定义、经济解释、训练截止日、数据覆盖和失效条件；Agent 可以提出候选，不能自行宣布有效 | `feature_snapshots`、`model_versions`、`FactorShockAdapter` | 按全样本相关性自动选因子 |
| 回测 | 从当前“模板日期压力测试”升级为真实事件日、公告可得日、滚动训练窗和交易约束下的 event study / walk-forward | `EventStudyAdapter`、重写 `backtester.py` | 随机切分时间序列或忽略停牌、涨跌停和成本 |
| 组合优化与风控 | 从单资产区间叙事升级为分支 × 期限 × 资产 × 分位数的 LossCube，再做 CVaR、最差分支、换手和集中度约束 | `PortfolioPolicy` | 用单一预期收益最大化生成“最优买入” |
| AI + Finance / Agent R&D | Agent 负责生成研究假设、选择工具、编排实验和审查失败；只有通过 PIT、样本外和校准闸门的模型才能进入生产 | `MissionController`、Model Risk Critic | 让 LLM 直接生成目标价、概率或回测结论 |

因此，外部资料对本方案的主要贡献不是增加模型名词，而是补齐“数据 → 特征 → 时间一致回测 → 风险分布 → 组合决策”的完整链条。

---

## 2. 当前代码的真实能力清单

### 2.1 已经真实存在，应保留深化

| 能力 | 当前实现 | 结论 |
|---|---|---|
| 根情景解析 | `scenario_engine/parser.py`，规则 + 可选 LLM JSON 解析 | 可保留，需改成任务规划的一个内部步骤 |
| 本地行情和持仓 | `src/stock_app/data_downloader.py` 已采用东方财富 HTTP 主链、腾讯 HTTP/AKShare 包装兜底；`data_provider.py`、`portfolio.py` 读取 SQLite | 可用且已有 AKShare 依赖，但数据表缺少 `source_id`、复权口径、`known_at`、修订和许可来源字段 |
| 历史类比 | `analogs.py` 的固定事件窗口 + 市场状态相似窗口 | 可作为一个模型 Adapter，不能独占结论 |
| 简化因子 | `factors.py` 的 beta、20 日动量、60 日波动、流动性变化 | 是真实计算，但不含行业、风格、基本面、估值 |
| 外部证据 | `research/` 的 CNINFO 公告、页面快照、RSS、热榜、MCP、正文读取、评分、入库 | 部分真实；官方主页快照不能当具体事件证据 |
| 规则压力模型 | `simulator.py` 的历史分位数、beta 和宏观代理冲击 | 可运行，但宏观映射是硬编码常数，未校准 |
| 证据修正 | `evidence_adjuster.py` 的标签姿态、区间扩宽和轻微倾斜 | 可审计，但过于粗糙，不能替代事件模型 |
| 金融沙盘 | `financial_sandbox.py` 的参与者、动作、观察点、路径关注度 | 是确定性解释器，不是真实多 Agent 仿真 |
| 后续分支 | `recursive_forecast.py` 的领域模板、假设包、单层子情景运行 | 可生成一层假设，但并非真正递归事件树 |
| 持仓方案 | `decision_support.py` 的三类规则权重方案和行情候选池 | 有结果，但没有基本面/行业/估值，且候选池只是动量-波动-流动性排序 |
| 报告与审计 | `reporter.py`、`repository.py`、导出、版本、回放、事件轮询 | 基础较完整，常规前端却使用 `persist=False`，闭环未真正启用 |

### 2.2 代码中“看起来已经实现，实际上没有闭环”的部分

#### 2.2.1 情绪未进入情景主链

`Agents_Experience/data_sources/sentiment_analyzer.py` 只有正负关键词计数；`scenario_engine/engine.py` 从未调用它。当前沙盘里出现“情绪”只是文字规则，情绪既没有点时快照，也没有进入事件强度、路径权重、区间校准或组合优化。

#### 2.2.2 财报可以抓到 PDF，但通常不能得到可计算财务指标

`research/fetchers.py` 能取得 CNINFO 公告/PDF URL；`FinancialStatementNormalizer` 却只读取证据 `metadata.financial_statement` 中已经结构化好的内容。CNINFO PDF 正文没有被自动抽取、对账并写入这些字段；已存财报也没有供量化主链读取的 Interface。

所以“财报收集已完成”和“财报进入预测”是两件事。当前只完成了前半段的部分基础。

#### 2.2.3 行业不是数据，只是文本标签

`factors.py` 明确说明不含行业/完整风格风险模型。当前行业只存在于模板字符串、受影响实体和图谱节点名中，没有点时行业分类、行业指数、行业估值、行业广度、行业资金、同业财报扩散和供应链暴露。

#### 2.2.4 “完整 Agent 工作流”主要是确定性方法

`agent_workflow.py` 明确记录 `autonomous_llm_claimed=False`。Bull/Bear/Risk 多数是在已生成报告上读取区间并写观察语句；`financial_sandbox.py` 中的工具名是审计记录，不是实际动态工具调用。

真正调用 LLM 的主要位置是情景解析、证据分类和最终文字改写。当前没有一个 Agent 能自己：发现数据缺口 → 选择数据工具 → 处理失败 → 重规划 → 校验产物 → 决定是否进入下一阶段。

#### 2.2.5 沙盘提出重算请求，但主引擎不执行

`FinancialSandbox.run()` 会返回 `quant_recalc_requests`，然而它在 `ScenarioSimulator.simulate()` 之后才运行，`ScenarioEngine.run()` 只把请求写入审计，没有再进入量化 Module。沙盘与量化链因此是展示关系，不是闭环关系。

#### 2.2.6 递归参数不等于真正递归

`RecursiveForecastEngine` 接受最大深度 2，但所有分支固定为 `depth=1`；子情景运行时显式关闭未来事件和递归。更重要的是，分支以自然语言重新拼接根问题，没有结构化改变 `ShockSpec`、财务状态、行业状态或资金状态，因此不同分支可能得到相同或几乎相同的数值区间。

#### 2.2.7 前端“Agent 自动候选”仍是静态宏观模板

`visualization/scenario_page.py::_build_agent_family_plan()` 主要根据最大持仓权重拼出大跌、汇率、利率和油价四类固定候选，没有读取公司行业、财报日历、估值、资金拥挤、公告或真实事件。

#### 2.2.8 当前进度条不是真实 Agent 进度

`visualization/scenario_page.py:268-306` 已经显示 `st.progress` 和 `st.status`，但百分比与文案是前端脚本预先写死的。页面在调用 `engine.run()` 前直接走到 40%，随后整个同步调用期间停在“量化压力”；等 `engine.run()` 返回后才依次跳到 68%、82%、92% 和 100%。它既不知道当前执行的是哪个 Agent，也不能呈现并行 Agent、工具等待、重试、降级或 Critic 驳回，因此只能算阶段占位动画。

V3.1 的进度显示必须消费 MissionController 发出的真实状态事件。没有 Agent/Module 事件时宁可显示“等待心跳”或不确定进度，也不能用定时器制造虚假完成度。

### 2.3 必须先修的正确性问题：未来数据泄漏

这是 V3 的第一优先级，甚至高于新增情绪模型。

- `ResearchRepository.search()` / `top_relevant()` 没有按 `as_of_date`、`published_at`、`first_seen_at` 过滤。
- 历史推演可以检索到基准日之后才发布或才抓取的本地证据。
- `freshness_score()` 使用日期差绝对值；未来 1 天的文档反而会得到很高新鲜度。
- 当前页面快照是抓取时的页面，但报告可能声称数据边界为“截至历史基准日”。
- `Agents_Experience` 的文件型数据只按日期相等过滤，和 `scenario_engine/research` 是两套时间规则。

如果不先修这一点，任何“财报 + 情绪 + 事件预测”的回测都会出现虚高结果。

---

## 3. V3 的产品定义

V3 不再以“用户手工设置几十个开关，然后跑一个场景”为主要交互，而以一个研究任务为 Interface：

```python
mission = ScenarioMission(
    objective="分析未来三个月最可能伤害或改善当前组合的事件，并给出稳健持仓方案",
    portfolio_ref="current",
    as_of="2026-08-04T15:00:00+08:00",
    risk_policy=RiskPolicy(max_drawdown=0.08, max_single_weight=0.30),
)

result = mission_controller.execute(mission)
```

上例是交互入口，不代表每次任务都需要人工填写。无人值守模式由调度任务自动装载 `portfolio_ref="current"`、当前 `as_of` 和版本化 `RiskPolicy`，再根据事件触发生成 objective；运行期间没有人工选择数据源、确认分支、批准降级或修正结论的检查点。策略阈值只在系统部署/治理时版本化配置，不进入单次推演的人机流程。

系统自动完成：

```text
任务理解
-> 点时数据能力探测
-> 持仓暴露诊断
-> 自动发现根事件
-> 采集政策/公告/财报/行情/行业/资金/情绪
-> 实体与时间对齐
-> 正反证据审查
-> 事件条件化量化
-> 未来事件分支和触发条件
-> 多分支组合损失矩阵
-> 自动生成并复测持仓方案
-> 反证审查、校准检查、合规检查
-> 报告、审计、监控任务
```

默认不要求用户选择来源、正文预算、沙盘轮数、分支数或研究模式。Agent 根据任务重要性、数据可得性、账号额度和运行预算自动决定；高级设置只保留为运维策略，而不是普通用户表单。

---

## 4. 目标架构：控制面、事实面、计算面、决策面

```text
ScenarioMission
  |
  v
AutonomousScenarioMission Module
  |-- Data Steward Agent --------> PointInTimeMarketSnapshot Module
  |                                  |-- LocalSQLite Adapter
  |                                  |-- RQData / TuShare / AKShare Adapter
  |                                  |-- per-domain RoutingPolicy
  |                                  |-- CNINFO/Exchange/PBC/RSS/MCP Adapter
  |
  |-- Event Scout Agent ----------> Event Ledger + CausalMarketGraph Module
  |-- Sentiment Analyst Agent ----> EventSentiment Module
  |-- Fundamental Analyst Agent --> FundamentalAndIndustryExposure Module
  |-- Quant Modeler Agent --------> EventConditionedImpactModel Module
  |-- Branch Planner Agent -------> ScenarioTree Module
  |-- Portfolio Agent ------------> PortfolioPolicy Module
  |-- Critic Agents --------------> Point-in-time / numeric / evidence / compliance gates
  |
  v
ScenarioRunLedger Module -> ProgressProjector -> Streamlit Agent Progress
                         -> Report / Replay / Monitor
```

### 4.1 最重要的架构原则

1. **Agent 负责选择和编排，确定性 Module 负责数字。**
2. **Interface 必须小。** 调用者只提交任务和风险策略，不学习 20 多个布尔参数。
3. **所有事实都必须同时有 `event_time` 和 `known_at`。** 事件发生时间不等于系统可得时间。
4. **生成假设永远不能进入 Observed Evidence 表。**
5. **分支必须改变结构化状态，不能只改变自然语言。**
6. **概率只有经过样本外校准才能叫概率。** 否则只能叫优先级或路径关注度。
7. **失败是正常状态。** 数据缺失、额度不足、来源故障必须触发自动降级，不触发编造。
8. **进度是运行事实的投影，不是前端计时动画。** 百分比、当前 Agent、重试和完成状态只能来自持久化进度事件；报告尚未原子提交时不得显示 100%。

---

## 5. 六个需要深化的核心 Module

这里使用 Module、Interface、Seam、Adapter、Depth、Leverage、Locality 作为架构术语。

### 5.1 PointInTimeMarketSnapshot Module（P0）

**涉及文件**：`src/stock_app/data_downloader.py`、`src/stock_app/database.py`、`scenario_engine/data_provider.py`、`scenario_engine/research/*`、`Agents_Experience/core/data_provider.py`、`Agents_Experience/data_sources/*`、`visualization/data_loader.py`。

**问题**：行情、外部证据和交易模拟上下文使用三套数据结构与时间规则。未来数据过滤散落甚至缺失，调用者必须知道数据源、表名、抓取状态和各种开关，当前 Module 较浅。

**目标 Interface**：

```python
class PointInTimeMarketSnapshot:
    def build(self, request: SnapshotRequest) -> MarketSnapshot: ...

@dataclass
class SnapshotRequest:
    known_at: datetime
    portfolio_symbols: list[str]
    domains: set[str]  # price/fundamental/industry/flow/event/sentiment/macro
    lookback: dict[str, int]
    strict_pit: bool = True
```

**真实 Seam 与 Adapter**：

- `StructuredData Seam`：`LocalSQLiteAdapter`、`RQDataAdapter`、`TuShareAdapter`、`AKShareAdapter`，以及只把腾讯已记录响应标准化入库的 `TencentV3PriceIngestor`。腾讯链路不伪称历史可见性，不从 `stock_daily` 反推 lineage，不自动换源，也不估算源端未提供的成交额。
- `EvidenceSource Seam`：`CninfoAdapter`、`ExchangeAdapter`、`PbcPageAdapter`、`RssAdapter`、`McpEvidenceAdapter`，以及按许可启用的 `RQDataNewsAdapter`、`TuShareEvidenceAdapter`、`AKShareNewsDiscoveryAdapter`、`LicensedNewsAdapter`。
- 两个 Seam 不共用一个“万能 DataFrame Interface”：前者承载可计算时序/截面数据，后者承载正文、来源、发布时间、转载关系和更正记录。
- 不为当前只有一种实现的 Repository 提前创建空泛 Interface；先深化 SQLite 实现。

**强制字段**：

```text
artifact_id
source_id
entity_id
event_time / period_end
published_at
known_at
ingested_at
valid_from / valid_to
revision_id
source_uri
adjustment
schema_version
license_tag / usage_scope
quality_flags
payload_hash
```

**PIT 规则**：

- `published_at > mission.known_at`：拒绝。
- 没有发布时间且 `first_seen_at > mission.known_at`：拒绝。
- 当前页面快照不得用于历史基准日，除非保存过当时内容快照。
- 财报使用“公告可得日”，不能只用报告期结束日。
- 行业成分、指数权重、ST、停牌和上市状态必须使用任务日期对应版本。
- 每个返回值包含 `availability_status`；禁止用默认值伪装真实数据。

**收益**：PIT 规则集中在一个 Module，提升 locality；所有模型与测试只跨一个 Interface，获得 leverage。

### 5.2 Event Ledger + CausalMarketGraph Module（P1）

**涉及文件**：`knowledge_graph.py`、`financial_sandbox.py`、`event_monitor.py`、`event_scheduler.py`。

**问题**：当前有两套不兼容图；沙盘 evidence edge 可以指向不存在的 path node；事件轮询只做模板相似度，自动运行时会丢失原始事件正文和幅度。

**目标 Interface**：

```python
class CausalMarketGraph:
    def apply_event(self, event: ObservedEvent, snapshot: MarketSnapshot) -> GraphRevision: ...
    def exposure_paths(self, event_id: str, portfolio_id: str) -> list[ExposurePath]: ...
```

**事件三分类**：

- `ObservedEvent`：有来源、发布时间、原文哈希。
- `DerivedEventState`：由规则/模型推导，必须列出输入 artifact。
- `HypothesisEvent`：Agent 生成，仅能进入分支表。

**图边最少包含**：

```text
source_node_id / target_node_id
relation_type
direction
event_time / known_at
lag_distribution
evidence_ids
validation_status
effect_sign_stability
```

所有边保存前强制检查端点存在；路径节点必须作为真实节点入图。图不是为了展示，而是给行业暴露、事件传播、分支生成和报告共同使用。

### 5.3 FundamentalAndIndustryExposure Module（P1）

**问题**：财报只存不读，行业只是名词，导致 Agent 无法回答“这个事件为什么对这家公司与同业不同”。

**目标 Interface**：

```python
class FundamentalAndIndustryExposure:
    def build(self, snapshot: MarketSnapshot, symbols: list[str]) -> ExposureSnapshot: ...
```

**公司特征**：

- 收入、利润、经营现金流、资产负债、股东权益。
- 营收/利润同比和环比、毛利率/净利率、ROE、现金利润质量、杠杆、应收与存货变化。
- 财报预告/正式报告差异、历史修订、财报发布时间。
- PE/PB/PS/PCF、股息率、自由流通市值。
- 公司相对行业的估值分位、盈利趋势和资金流分位。

**行业特征**：

- 点时行业分类、指数成分和权重。
- 行业收益、相对强弱、波动、广度、成交拥挤度、估值分位。
- 同业财报共振、行业盈利修正、上游成本与下游需求代理。
- 组合行业集中度和事件对行业的暴露矩阵。

**双 Adapter 对账**：

- 结构化财务数据 Adapter 用于计算。
- CNINFO 原文 Adapter 用于来源核验、单位/期间/实体对账和异常解释。

若两者冲突，Data Steward Agent 将数据标记为 `disputed` 并禁止进入强量化，而不是自动选一个。

### 5.4 EventSentiment Module（P1）

情绪应是“某个实体对某个事件在某个时间点的市场状态”，而不是一条全市场 1-10 分。

```python
@dataclass
class EventSentimentSnapshot:
    entity_id: str
    event_id: str
    known_at: datetime
    tone: float
    surprise: float
    novelty: float
    disagreement: float
    attention: float
    persistence: float
    authority_weight: float
    portfolio_relevance: float
    evidence_ids: list[str]
```

一个可解释的聚合分可以写为：

```text
sentiment_signal = Σ(source_authority × relevance × surprise × tone × novelty × time_decay)
```

但必须同时保留：

- 来源分层：官方公告、公司公告、主流财经媒体、研究观点、社区/热榜。
- 反重复：转载同一原文不能当多条独立证据。
- 反身性：价格已经大涨后的乐观新闻，与价格未反应时含义不同。
- 分歧：正负观点并存时扩大不确定性，不强行平均成中性。
- 事件阶段：传闻、确认、落地、验证、证伪。
- 持仓相关性：同样的宏观新闻对航空、出口、银行的方向可能不同。

情绪只进入事件状态、分支排序和量化特征；它不能直接生成收益区间。

### 5.5 EventConditionedImpactModel Module（P0）

**涉及文件**：`analogs.py`、`factors.py`、`shocks.py`、`simulator.py`、`evidence_adjuster.py`、`backtester.py`。

**问题**：一个冲击规则的知识散落在多个 Module；利率、汇率、油价、关税被映射成固定市场等价冲击，回测不回写参数，locality 较差。

**目标 Interface**：

```python
class EventConditionedImpactModel:
    def estimate(
        self,
        scenario: StructuredScenario,
        snapshot: MarketSnapshot,
        portfolio: PortfolioSnapshot,
        model_policy: ModelPolicy,
    ) -> ScenarioLossDistribution: ...
```

内部至少包含三个 Adapter：

1. `EventStudyAdapter`：真实事件表上的异常收益研究。
2. `FactorShockAdapter`：市场、行业、风格、基本面、资金和情绪因子冲击。
3. `AnalogStateAdapter`：只使用事件发生前可见特征的历史相似状态。

个股在分支 `b`、期限 `h` 下的冲击可拆为：

```text
Δr(i,b,h) = β(i,t)·ΔF(b,h)
          + Γ(industry(i),t)·ΔI(b,h)
          + θ·EventFeatures(b,t)
          + ε(i,b,h)
```

其中：

- `β` 来自点时市场/风格暴露。
- `Γ` 来自行业和供应链暴露。
- `EventFeatures` 包含事件阶段、权威性、意外度、情绪、财报偏离和资金拥挤。
- `ε` 用历史残差或 bootstrap 表示，不由 LLM 生成。

输出不是单一目标价，而是：

```text
median
q10 / q25 / q75 / q90
price_q10 / price_q25 / price_median / price_q75 / price_q90
CVaR
model_disagreement
coverage
calibration_version
numeric_provenance
```

价格区间不是第二套“目标价模型”，而是从同一收益分布确定性派生：

```text
price_q(i,b,h) = point_in_time_price(i,t) × exp(return_q(i,b,h))
```

`point_in_time_price`、复权口径和公司行动必须固定在 `known_at` 可得信息上。默认输出 1/5/20/60 个交易日；财报、政策落地等事件可以按观察窗口增加特定期限，但所有期限都必须分别回测和校准。停牌或连续涨跌停期间仍可输出估值分布，但组合层必须标记不可交易，不能把理论价格当成可成交价格。

**区间生成**：优先使用分位数回归、bootstrap 或 conformal calibration；样本不足时回退到保守压力区间，并把 `probability_status` 设为 `uncalibrated`。

**禁止继续保留的做法**：把固定的 `0.018`、`0.014` 等宏观映射常数长期当正式模型。它们只能作为 `FallbackProxyAdapter`，并必须显示版本、来源和回测误差。

### 5.6 PortfolioPolicy Module（P1）

**涉及文件**：`decision_support.py`、`reverse_stress.py`、`constraints.py`，以及前端重复的候选方案逻辑。

**目标**：自动产出“可比较的持仓方案”，而不是只把最大风险股乘 0.7/0.85。

输入为多个事件分支下的损失矩阵：

```text
LossCube[branch, horizon, asset, quantile]
```

优化目标可采用稳健组合形式：

```text
min_w  λ1·CVaRα(w)
     + λ2·max_branch_loss(w)
     + λ3·turnover(w, w0)
     + λ4·model_uncertainty_penalty(w)
```

约束至少包括：

- 单股上限、行业上限、现金下限。
- T+1、停牌、涨跌停、ST、上市时间和最小交易单位。
- 流动性/成交额容量和换手成本。
- 不允许使用财报校验失败的股票进入自动新增候选。
- 不允许组合在关键分支下明显恶化却被平均收益掩盖。
- 可选跟踪误差、因子暴露和最大回撤预算。

输出固定为：

- 防守方案：优先缩小尾部损失。
- 均衡方案：兼顾尾部和修复弹性。
- 观察/进取方案：只有真实确认信号满足时才启用。

每个方案必须自动送回 `EventConditionedImpactModel` 复测，附带调仓前后 CVaR、最差分支、集中度、换手成本和失效条件。候选股先通过基本面、估值、行业、流动性和事件适配闸门，再进入组合优化；不能只按近期动量排序。

---

## 6. AKShare、TuShare、RQData 的组合与按域路由

### 6.1 选型结论

不建议三选一，也不建议让三个来源按固定全局顺序轮询。不同数据域对 PIT、稳定性、许可证、正文覆盖和成本的要求不同，最佳专业组合是：

1. **RQData：专业量化与 PIT 主 Adapter。** 优先承担点时行情、财务、行业、因子风险模型、一致预期，以及取得授权后的个股新闻舆情。
2. **TuShare Pro：结构化事件和横向补充 Adapter。** 优先补充财报披露、公告、政策、新闻/研报、交易所互动、资金流、申万行业和公司行为，并作为关键结构化数据的交叉核验来源。
3. **AKShare：现有低成本广覆盖和 fallback Adapter。** 继续利用项目已安装的 AKShare、东方财富和腾讯链，补充行情、个股新闻、行业板块、资金、热度与关注度；不得把依赖网页结构的返回值当唯一审计事实。
4. **官方 Evidence Adapter：最终事实核验。** CNINFO、上交所、深交所、央行及其他监管/统计来源继续承载公告、政策和原文证据，不被三个量化平台取代。

如果暂时没有 RQData 许可证，经济型 MVP 采用“TuShare Pro 主 + AKShare fallback + 官方 Evidence Adapter”，并由本项目的 `EventSentiment Module` 从文本计算情绪。代码 Interface 保持不变，购买 RQData 后只增加 Adapter 和路由策略，不改上层模型。

### 6.2 已在官方文档核对的代表性能力

| 来源 | 官方文档已确认的代表性端点 | 在 V3 中的角色 | 不能直接假定 |
|---|---|---|---|
| RQData | `get_price`、`get_pit_financials_ex`、`get_factor`、`get_industry`、`get_instrument_industry`、`get_capital_flow`、`get_securities_margin`、`get_announcement` | PIT 行情/财务、行业、资金、公告和量化特征主源 | 当前账号已含全部字段、全部历史和实时权限 |
| RQData 风险/另类 | `get_factor_exposure`、`get_factor_return`、`get_specific_risk`、`get_factor_covariance`、`consensus.*`、`news.get_stock_news` | 风格/行业暴露、风险协方差、一致预期、新闻舆情特征 | 风险模型、新闻舆情或一致预期已购买；部分能力需要额外包或商务授权 |
| TuShare Pro | `stock_basic`、`daily`、`daily_basic`、`income`、`balancesheet`、`cashflow`、`fina_indicator`、`disclosure_date` | 股票主数据、行情/估值、三大表、指标和披露日补充 | 积分、频率、VIP 端点和历史范围对当前 token 全部开放 |
| TuShare Pro 事件/舆情 | `index_classify`、`index_member_all`、`sw_daily`、`moneyflow`、`news`、`major_news`、`anns_d`、`irm_qa_sh`、`irm_qa_sz`、`npr`、`research_report`、`monetary_policy` | 行业、资金、公告、新闻、政策、研报和互动问答 | 新闻来源完整、正文永久可取或供应商情绪可直接用于收益预测 |
| AKShare | `stock_zh_a_hist`、`stock_news_em`、`stock_individual_fund_flow`、`stock_board_industry_summary_ths`、`stock_board_industry_index_ths`、`stock_hot_rank_em`、`stock_hot_search_baidu`、`stock_comment_em` | 行情/行业/资金补缺、个股新闻发现、热度和注意力特征 | 网页端点长期稳定、历史 PIT 完整、热榜等于情绪、商业再分发天然合规 |

RQData 官方文档说明 `news.get_stock_news` 提供从 2017 年起的个股新闻情绪指标，正式数据日内每半小时更新，并要求单独安装 `rqdatac_news`。返回的新闻/公司正负权重和相关度只能作为 `EventSentiment Module` 的一个输入；系统仍需做实体对齐、转载去重、事件阶段、意外度、权威性、分歧和价格反应判断，不能直接把供应商标签映射成收益率。

AKShare 的热榜、热搜、千股千评和用户关注指数统一归类为 `attention` 或 `crowding_proxy`，而不是 `tone`。个股新闻只作为 Event Scout 的发现入口；若不能保存来源 URI、原发布时间和正文哈希，就不能升级为强证据。

三家平台都不能被默认视为“全网社交媒体舆情库”。论坛、短视频、社交平台、付费媒体和跨境新闻若没有合法、稳定、可回溯的全文来源，CapabilityManifest 必须返回 `coverage_gap`。专业部署可在 `EvidenceSource Seam` 增加合规商业新闻/社媒监听 Adapter；没有该许可时，报告只能称“已覆盖来源内的舆情”，不能称“全网舆情”。

### 6.3 按数据域的默认 RoutingPolicy

| 数据域 | 专业部署主 Adapter | 次 Adapter / 对账 | fallback 或原文核验 | 强制规则 |
|---|---|---|---|---|
| 证券主数据、交易日、上市退市、ST、停牌 | RQData | TuShare Pro | AKShare/本地快照 | 历史任务只能使用任务日期有效版本 |
| 日/分钟行情和复权 | RQData | TuShare Pro | 当前东方财富/腾讯/AKShare 链 | 同一时间序列不得静默跨源拼接；复权、时区、停牌填充必须一致 |
| PIT 财务与量化衍生指标 | RQData `get_pit_financials_ex` / `get_factor` | TuShare 三大表和财务指标 | CNINFO 原文 | 使用公告可得日；修订记录可重放；冲突时标记 `disputed` |
| 财报预告、披露、公司行为 | TuShare Pro | RQData | CNINFO/交易所原文 | 事件日、公告日、入库日分列，原文优先于摘要 |
| 行业、指数成分和风险模型 | RQData | TuShare 申万数据 | AKShare 当前板块快照 | 当前快照不得回填历史；行业分类体系和版本必须入库 |
| 个股/行业资金、融资融券、龙虎榜、北向 | TuShare Pro | RQData | AKShare | 不同供应商字段定义分 dataset 保存，未对齐前不平均 |
| 宏观、利率、汇率、商品、政策 | TuShare + 官方 Evidence Adapter | RQData | AKShare | 政策原文和发布日期来自官方来源，数值序列可跨源对账 |
| 公告、新闻、研报、互动问答 | 官方 Evidence + TuShare | RQData 新闻/公告 | AKShare 新闻发现、RSS/GDELT/MCP | 必须有 `published_at`、`known_at`、来源、实体和去重簇 |
| 新闻情绪、舆论和市场注意力 | RQData 新闻舆情（已授权时）+ 本地 EventSentiment | TuShare 文本语料 | AKShare 热榜/热搜/关注 | 供应商情绪是特征，不是结论；热度只表示 attention |

`RoutingPolicy` 的选择键是：

```text
domain × dataset × known_at × required_quality × entitlement × latency × budget
```

Agent 不在单次任务里让用户选择平台。Data Steward 读取策略和运行时能力清单，自动形成 `SourcePlan`；同一数据域只有语义、复权、币种、单位、时间戳和许可范围都兼容时才能 fallback。

### 6.4 深化后的 Adapter Interface

```python
class StructuredDataAdapter(Protocol):
    def capabilities(self) -> CapabilityManifest: ...
    def fetch(self, request: DataRequest) -> DataArtifactBatch: ...

class EvidenceSourceAdapter(Protocol):
    def capabilities(self) -> CapabilityManifest: ...
    def collect(self, query: EvidenceQuery) -> EvidenceDocumentBatch: ...

@dataclass
class DataRequest:
    domain: str
    dataset: str
    entities: list[str]
    start: datetime
    end: datetime
    known_at: datetime
    fields: list[str]
    frequency: str | None
    adjustment: str | None
    strict_pit: bool = True
```

`CapabilityManifest` 至少记录：

```text
source_id / adapter_version
domain / dataset / field coverage
entitlement_status
historical_start / update_latency / max_frequency
pit_support / revision_support
quota_remaining / rate_limit
license_tag / cache_and_redistribution_policy
probe_time / probe_result
```

所有 Adapter 启动时只做最小探测，不下载全市场数据。凭证只从 Secret Provider 注入，不写入任务、日志或报告。状态必须区分 `dependency_missing`、`auth_failed`、`permission_denied`、`quota_limited`、`rate_limited`、`network_unavailable`、`schema_changed`、`no_data`、`coverage_gap` 和 `invalid_response`；地域限制只有在真实探测确认后才记录 `region_unavailable`。

### 6.5 跨源对账、缓存和运行预算

- 缓存键：`adapter + dataset + entity + start + end + fields + adjustment + revision`。
- 行情对账检查交易日、价格/成交量误差、复权因子和停牌填充；财务对账检查实体、期间、币种、单位、公告日和修订版本。
- 双源关键字段差异超过数据域阈值时返回 `conflicted`，不能静默选择更“顺眼”的数值；Quant Modeler 禁用受影响特征或扩大模型不确定性。
- 财报、行业归属和指数成分以版本化快照存储，不重复拉取。AKShare 等网页源必须保存 Adapter 版本、schema hash 和原始响应哈希，用于定位网页结构变化。
- 当前 `requirements.txt` 的 `akshare>=1.12.0` 不足以保证可重放，应在部署 lock/constraints 中固定已通过契约测试的版本。TuShare 和 RQData 作为可选依赖延迟导入；未安装时 Adapter 返回 `dependency_missing`，不能令整个应用导入失败。
- 分钟/Tick 数据只有当事件时效和组合风险值得时才自动申请。一个任务先生成 `DataBudget`；额度不足时优先保留持仓、行业基准和关键分支。
- CI 使用三种 Adapter 的脱敏契约 fixture，禁止依赖公网；独立 canary 按实际许可证做最小真实查询，并输出账号实际能力报告。
- 数据调用失败采用指数退避、速率限制和熔断；不无限重试，也不切换到来源不明的网页数字。

---

## 7. 真正自治的 Agent 设计

### 7.1 Agent 不是类名，而是可闭环任务执行者

每个 Agent 至少具备：

```text
Goal
Input Schema
Tool Allowlist
Planning Policy
Retry/Fallback Policy
Output Schema
Validator
Budget
Audit Trace
```

如果只是调用一次固定函数并改一个名字，不应建 Agent。`research/orchestrator.py` 中多个只透传 `fetcher.fetch()` 的 CollectorAgent 应通过 deletion test：删除后复杂度消失，因此应改成 Adapter metadata，而不是继续增加类。

### 7.2 Agent 家族

| Agent | 可调用工具 | 必须产物 | 禁止事项 |
|---|---|---|---|
| Mission Planner | 组合快照、能力清单、任务预算 | `MissionPlan` | 不给数值结论 |
| Data Steward | 所有只读数据 Adapter、PIT 校验器 | `MarketSnapshot`、缺口与冲突清单 | 不接受未来数据，不修改原文 |
| Event Scout | 事件账本、公告/政策/宏观/异常行情查询 | 根事件候选及证据 | 不把传闻写成事实 |
| Sentiment Analyst | 文本去重、事件分类、情绪特征工具 | `EventSentimentSnapshot` | 不把热度直接映射成收益 |
| Fundamental Analyst | 财务、估值、行业、原文对账工具 | `ExposureSnapshot` | 不自行补全缺失财务数字 |
| Quant Modeler | 事件研究、因子、类比、校准 Adapter | `ScenarioLossDistribution` | 不接受 LLM 生成数字 |
| Branch Planner | 不确定点、事件转移模板、图谱 | 结构化 `ScenarioBranchSpec` | 不生成 Observed Evidence |
| Portfolio Agent | LossCube、组合约束、优化器、复测器 | 3 类候选组合与比较 | 不直接下单 |
| Evidence Critic | 证据图、PIT 规则、引用检查 | 通过/拒绝及修复请求 | 不只做措辞检查 |
| Model Risk Critic | 校准报告、样本数、漂移指标 | 模型可用等级 | 不把未校准分数叫概率 |
| Report Agent | 已验证结构化结果 | 投资者版与审计版报告 | 不新增事实或数字 |

### 7.3 自动重规划示例

```text
Fundamental Analyst 请求银行净息差
-> RQData capability 显示该字段未授权
-> RoutingPolicy 查询 TuShare 等价数据集，但当前 token 同样无权限或字段口径不等价
-> Data Steward 改查 CNINFO 最新已发布财报原文
-> PDF 抽取失败
-> 尝试结构化 OCR/表格抽取 Adapter
-> 单位对账仍失败
-> 将 NIM 标记为 unavailable
-> Quant Modeler 禁用 NIM 子模型，扩大模型不确定性
-> Branch Planner 仍可把“息差未验证”列为观察分支
-> Report 明确说明该结论未使用净息差数值
```

整个流程不要求用户参与，也不会把失败伪装成成功。

### 7.4 无人值守状态机

```text
CREATED
-> CAPABILITY_CHECKED
-> SNAPSHOT_READY
-> ROOT_EVENTS_READY
-> EVIDENCE_VALIDATED
-> FEATURES_READY
-> ROOT_QUANTIFIED
-> BRANCHES_PLANNED
-> BRANCHES_QUANTIFIED
-> PORTFOLIOS_OPTIMIZED
-> CRITICS_PASSED
-> REPORTED
-> MONITORING_SCHEDULED
```

每个状态是实际业务节点，不再像当前 `workflow.py` 那样只跑一条独立审计状态序列。失败状态包括 `DEGRADED`、`RETRY_SCHEDULED`、`INSUFFICIENT_DATA` 和 `REJECTED_BY_CRITIC`；只有真实持久化成功后才能进入 `SAVED`。

### 7.5 Agent 实时进度事件 Interface

MissionController、每个 Agent 和关键量化 Module 都向同一个 `ProgressSink` 发布结构化事件：

```python
@dataclass(frozen=True)
class AgentProgressEvent:
    run_id: str
    seq: int
    stage_id: str
    agent_id: str
    status: str  # queued/running/waiting/retrying/degraded/completed/failed
    completed_units: int
    total_units: int | None
    message_code: str
    safe_message_args: dict[str, object]
    started_at: datetime | None
    heartbeat_at: datetime
    finished_at: datetime | None
    retry_count: int
    trace_ref: str | None

class ProgressSink(Protocol):
    def publish(self, event: AgentProgressEvent) -> None: ...

class ProgressProjector:
    def snapshot(self, run_id: str) -> ScenarioProgressViewModel: ...
```

`safe_message_args` 只允许保存和展示“正在查询财报”“已完成 8/12 个持仓暴露”“TuShare 限频，切换 RQData”等操作状态，不展示模型隐藏推理、系统提示词、凭证、原始个人数据或内部 chain-of-thought。详细工具输入输出仍由权限受控的审计 trace 保存。

总体进度使用 MissionPlan 中固定的阶段权重，不按耗时猜测：

| 阶段 | 默认权重 | 当前 Agent 示例 |
|---|---:|---|
| 能力探测 | 8% | Data Steward |
| PIT 快照 | 14% | Data Steward |
| 根事件与证据 | 16% | Event Scout / Evidence Critic |
| 财报、行业、情绪特征 | 12% | Fundamental / Sentiment Analyst |
| 根情景量化 | 14% | Quant Modeler |
| 结构化分支量化 | 12% | Branch Planner / Quant Modeler |
| 组合优化与复测 | 10% | Portfolio Agent |
| 模型、证据与合规审查 | 8% | Critic Agents |
| 报告与原子保存 | 6% | Report Agent / Run Ledger |

计算规则：

```text
raw_progress = completed_stage_weight
             + active_stage_weight × completed_units / total_units
```

- `total_units` 未知时，该阶段显示流动动画和“任务数评估中”，但总体百分比不伪造增长。
- Agent 重规划增加任务时，阶段任务计数可以变化；页面明确显示“新增 N 个验证任务”。视觉百分比不倒退，但停留在本阶段上限以内。
- 多个 Agent 并行时，主卡片显示关键路径上最近一次 `running` Agent，旁边显示“另有 N 个 Agent 并行”；不能随机挑一个伪装成唯一当前 Agent。
- 正在运行的任务最高显示 99%；只有报告、进度事件和 run bundle 原子提交成功后才显示 100%。
- 心跳超过策略阈值时显示“响应延迟”，继续区分外部工具等待、限频重试、Worker 失联和真正失败。
- 所有事件按 `(run_id, seq)` 幂等写入；页面刷新、浏览器重连或应用重启后从 Run Ledger 重建同一进度。

---

## 8. 结构化事件分支：让子情景真的改变模型

新的分支不能只保存一句自然语言。建议契约：

```python
@dataclass
class ScenarioBranchSpec:
    branch_id: str
    parent_id: str
    depth: int
    hypothesis_event: HypothesisEvent
    state_changes: list[StateChange]
    observation_window: tuple[str, str]
    trigger_conditions: list[ObservableCondition]
    invalidation_conditions: list[ObservableCondition]
    required_data: list[DataRequest]
    generation_basis: list[str]
    priority_score: float
    probability: float | None
    probability_status: str  # calibrated / uncalibrated / not_available
```

例如“降息后社融仍弱”必须转换为：

```text
interest_rate_shock = -25bp
credit_impulse_state = weak
real_estate_demand_state = weak
bank_asset_quality_risk = rising
event_stage = post_policy_validation
```

然后 Quant Modeler 才能重新计算银行行业因子、公司财务敏感度和组合损失。若无法把分支转成可计算状态，分支只能留在观察清单，不能显示一个伪精确收益区间。

### 8.1 未来事件生成的两条来源

1. **统计来源**：从真实事件账本学习事件阶段转移或 hazard，例如“预告 → 正式财报 → 问询/澄清”。只有通过时间切分校准才输出概率。
2. **Agent 来源**：从反证、缺口、图谱和持仓暴露生成压力测试假设。输出优先级，不输出未经校准的概率。

两者在报告中必须分开展示。

### 8.2 真正递归与剪枝

- 每个完成量化的子分支可再次调用 Branch Planner，直到 `max_depth`。
- 预算由 `expected_decision_value × severity × portfolio_relevance ÷ cost` 排序。
- 近似等价的分支按结构化 state diff 去重，而不是按标题去重。
- 如果新分支没有改变 LossCube 或没有新增观察价值，立即剪枝。
- 自动运行默认最多 2 层、每层 3 个、总计 7 次；由预算策略决定，不由前端 slider 决定。

---

## 9. 事件、财报、行业与情绪如何共同进入预测

### 9.1 不做单一“万能模型”

建议采用模型集合：

```text
历史事件异常收益模型
+ 市场/行业/风格因子模型
+ 公司基本面敏感度模型
+ 资金拥挤/流动性模型
+ 事件情绪与意外度特征
+ 历史相似状态模型
-> 模型分歧与校准器
-> 条件收益分布
```

Agent 依据数据覆盖和历史验证选择可用 Adapter；不同 Adapter 的结果先保留，再由校准器合成。不能在证据不足时强行让所有模型给数值。

### 9.2 财报事件

财报事件至少拆成：

- 报告期结束日。
- 预告/快报发布时间。
- 正式报告发布时间。
- 市场预期或历史趋势基线。
- 实际偏离（surprise）。
- 质量信号：现金流、一次性项目、审计意见、负债和资产减值。
- 行业共振：同业是否同时走弱/走强。

对于没有一致预期数据的情况，可使用公司自身滚动趋势和同业截面作为基线，但必须标记 `surprise_proxy`，不能称为“超预期”。

### 9.3 行业与产业链

事件先映射到行业/供应链因子，再映射到公司：

```text
油价上涨
-> 上游资源收入改善 / 下游燃料成本上升
-> 行业价差、毛利率和资金风格变化
-> 航空、化工细分、运输和资源股差异
-> 当前持仓贡献
```

如果没有公司级供应链/收入暴露，系统只能使用行业级 proxy，并降低 confidence；不能把“属于化工”直接等价成单一方向。

### 9.4 情绪与反身性

使用四种状态组合，而不是只看正负：

| 信息状态 | 价格状态 | 解释 |
|---|---|---|
| 正面且新颖 | 尚未反应 | 可能支持修复分支 |
| 正面但重复 | 已大涨 | 兑现/拥挤风险上升 |
| 负面且权威 | 尚未反应 | 下行分支优先级上升 |
| 正负分歧大 | 波动放大 | 扩大区间，不强行定方向 |

### 9.5 市场状态切换

同一事件在 risk-on、risk-off、流动性危机和政策托底阶段的传导不同。V3 至少需要一个可解释的市场状态 Module，输入指数趋势、波动、成交、融资、行业广度和相关性，输出状态及置信度。模型训练和回测按状态分层，避免把 2020 年流动性冲击直接套到所有时期。

---

## 10. 对当前代码的文件级改造映射

### 10.1 保留兼容 Facade，缩小主 Interface

`scenario_engine/engine.py` 目前约 1900 行，构造函数直接实例化所有实现，`run()` 暴露大量开关。保留 `ScenarioEngine` 作为兼容 Facade，但新代码只委托：

```python
ScenarioEngine.run(...) -> MissionController.execute(legacy_args_to_mission(...))
```

新调用者只使用 `execute(mission)`；旧参数逐步标记 deprecated。

### 10.2 建议目录

```text
scenario_engine/
  mission/
    contracts.py
    controller.py
    policies.py
    tool_registry.py
  data/
    contracts.py
    point_in_time.py
    router.py
    normalizers.py
    local_sqlite_adapter.py
    rqdata_adapter.py
    tushare_adapter.py
    akshare_adapter.py
    evidence_adapters.py
  events/
    ledger.py
    detector.py
    graph.py
    branch_planner.py
  signals/
    sentiment.py
    fundamentals.py
    industry.py
    market_regime.py
  quant/
    contracts.py
    event_study.py
    factor_model.py
    analog_model.py
    impact_model.py
    calibration.py
    loss_cube.py
  portfolio_policy/
    optimizer.py
    constraints.py
    validator.py
  ledger/
    run_ledger.py
  presentation/
    view_model.py
    progress_projector.py
    reporter.py

visualization/
  components/
    agent_progress.py
```

这不是要求一次性移动所有文件。Phase 1 先新增新 Module，并由旧 Facade 调用；测试稳定后再移动实现。

### 10.3 合并与删除

- 合并 `knowledge_graph.py` 和 `financial_sandbox.py` 的两套图契约。
- 合并 `agent_review.py`、`agent_workflow.py`、`workflow.py` 中重复的审查/状态职责到 MissionController 内部节点；保留审计结构，不保留“假 Agent”。
- 将 `Agents_Experience/data_sources` 中有价值的记录契约和情绪逻辑迁入统一点时快照；交易模拟通过 Adapter 消费同一数据，不再维护第二套事实链。
- 将 `engine.py` 内未使用的 `_build_investor_action_plan`、`decision_support.py` 和前端 `_build_rebalance_candidates` 的业务规则收敛到 `PortfolioPolicy`，前端只渲染 view model。
- 将只做一行透传的 CollectorAgent 删除，保留真正的数据 Adapter 和 Agent 工具元数据。
- `EvidenceImpactAdjuster` 在新模型稳定后退化为兼容 fallback；其固定标签 tilt 不再是主模型。
- `RecursiveForecastEngine.DOMAIN_TEMPLATES` 保留为离线 fallback Adapter，主分支来自结构化事件账本、图谱和缺口分析。

### 10.4 持久化深化

`ScenarioRepository` 当前只有 SQLite 一个实现，不需要为了形式创建 Repository Seam。应先深化为：

```python
run_ledger.commit_run_bundle(
    mission,
    capability_manifest,
    market_snapshot,
    root_result,
    branches,
    portfolio_proposals,
    agent_traces,
    validations,
)
```

使用一次事务原子保存。任何一步失败都不能出现“报告已保存、分支未保存、trace 又保存了一半”的状态。常规 Streamlit 运行默认持久化；临时预览才用 ephemeral 模式。

---

## 11. 数据库新增/调整

不建议继续为每个 Agent 单独建一张大 JSON 表。核心事实和运行产物建议为：

### 11.1 点时数据与事件

```text
market_artifacts
  artifact_id, dataset, entity_id, event_time, period_end,
  published_at, known_at, ingested_at, revision_id,
  source_id, adapter_version, source_uri, adjustment, schema_version,
  license_tag, usage_scope, payload_hash, quality_flags_json, payload_json

artifact_reconciliations
  reconciliation_id, dataset, left_artifact_id, right_artifact_id,
  comparison_rule_version, difference_json, status, resolved_artifact_id

event_ledger
  event_id, event_class, stage, entity_ids_json,
  event_time, known_at, source_artifact_ids_json,
  observed_or_hypothesis, status, dedupe_key, payload_json
```

### 11.2 特征与模型

```text
feature_snapshots
  snapshot_id, entity_id, feature_set, known_at,
  values_json, source_artifact_ids_json, code_version

model_versions
  model_id, version, training_cutoff, feature_schema,
  calibration_metrics_json, artifact_uri, status

scenario_loss_cube
  run_id, branch_id, horizon, symbol, quantile,
  return_value, model_id, numeric_provenance_json
```

### 11.3 任务和组合

```text
scenario_missions
scenario_mission_steps
scenario_progress_events
  run_id, seq, stage_id, agent_id, status,
  completed_units, total_units, message_code, safe_message_args_json,
  started_at, heartbeat_at, finished_at, retry_count, trace_ref
scenario_branch_specs
portfolio_proposals
portfolio_proposal_comparisons
validation_results
```

`scenario_external_evidence` 可以迁移为 `market_artifacts` 的文本视图；Generated Hypothesis 继续独立，绝不写入事实表。

---

## 12. 分阶段实施计划

### Phase 0：建立真实性基线（3-5 天）

**改动**：

- 增加现有能力清单测试，明确 deterministic / LLM / external / fallback。
- 保存 10 个黄金场景的当前输出，作为兼容基线。
- 增加图引用完整性测试、死代码/重复规则清单。

**验收**：

- 文档、前端和 `capability_report()` 不再把静态规则称为自治 Agent。
- 每项能力标注 `implemented / partial / fallback / planned`。

### Phase 1：PIT 数据 Module 与未来泄漏修复（1-2 周，P0）

**改动**：

- 新建 `MarketArtifact`、`SnapshotRequest`、`MarketSnapshot`。
- 给研究仓库所有检索增加 `known_at` 和 source type 过滤。
- 修复未来文档新鲜度逻辑；历史页面快照默认不可用。
- 建 `LocalSQLiteAdapter`、`RQDataAdapter`、`TuShareAdapter`、`AKShareAdapter` skeleton 及按域 `RoutingPolicy`；外部平台不可用时不阻塞本地测试。
- 增加依赖 lock/constraints 和延迟导入；AKShare 升级、TuShare/RQData 安装状态都必须进入 CapabilityManifest。
- 保留 `src/stock_app/data_downloader.py` 为兼容 Facade，先让其写入来源、复权、抓取时间和 schema hash，再逐步委托统一 Router。
- 常规 UI 运行启用 run ledger。

**验收**：

- 向 2020-03-20 回测库注入 2020-03-21 文档，查询结果必须为 0。
- 三个外部 Adapter 与本地 Adapter 的录制 fixture 必须满足同一 Interface 契约。
- 相同股票代码、日期、复权和字段请求经过不同 Adapter 后得到统一 schema；无法统一的字段必须保留 source-specific dataset。
- 所有报告的数据边界能列出最大 `known_at`。

### Phase 2：财报、行业、资金、情绪特征（2 周，P1）

**改动**：

- RQData PIT 财务/因子与 TuShare 三大表、财务指标、披露日进行结构化对账，再以 CNINFO 原文核验。
- RQData/TuShare 的点时行业、指数成分、行业估值、广度、资金和融资特征；AKShare 仅补当前缺口和注意力代理。
- 接入已授权的 RQData 新闻舆情、TuShare 新闻/公告/政策语料和 AKShare 新闻/热榜，并统一做事件级去重、权威性、意外度、分歧、注意力与衰减。
- 合并 `Agents_Experience` 的事实上下文。

**验收**：

- 平安银行样例能输出银行专属财务/估值/资金特征；无权限时明确降级。
- 同一篇转载 10 次不能产生 10 倍情绪权重。
- RQData 正负权重不得绕过本地 EventSentiment 校验；AKShare 热度不得写入 `tone` 字段。
- 财报发布日前运行不能读取该财报。
- `fundamental_coverage` 不再恒为 0；覆盖率必须按实际计算。

### Phase 3：事件条件化量化与真实回测（2-3 周，P0）

**改动**：

- 建真实事件表和 event study Adapter。
- 扩展市场/行业/风格风险模型。
- 训练/校准事件特征模型；保留 analog 和 proxy fallback。
- 重写 backtester：按真实事件发生日、公告可得日和滚动训练窗口运行。

**验收**：

- 所有数值有 `numeric_provenance` 和模型版本。
- Walk-forward 区间覆盖率接近目标覆盖率；按年份和市场状态分层报告。
- 与简单 market-beta baseline 对比；无稳定提升时不得上线复杂模型。
- 交易成本、停牌和涨跌停不能在回测中被忽略。

### Phase 4：自治 MissionController 与结构化递归（2 周，P0/P1）

**改动**：

- 前端只提交 mission；自动决定数据源、预算、分支和 fallback。
- MissionController、Agent 和量化 Module 发布 `AgentProgressEvent`；Run Ledger 持久化，ProgressProjector 生成页面 view model。
- 将当前同步写死百分比改为后台任务 + 进度事件消费。MVP 可复用 `visualization/agent_runner.py` 已有的线程/Queue 模式；生产路径使用 Phase 6 的可恢复作业表，页面只轮询 Run Ledger。
- 增加顶部动画进度条、当前 Agent 卡片和可折叠 Agent 时间线。
- 沙盘重算请求接入 Quant Modeler。
- 分支由 `StateChange` 驱动，真正支持第二层递归。
- Critic 能拒绝产物并触发修复，而不是只追加 uncertainty。

**验收**：

- 不操作高级开关也能完成全流程。
- Agent 工具调用失败后能自动换 Adapter 或降级。
- Agent/阶段切换后页面在 1 秒内更新；长调用期间持续心跳，刷新页面后能从 ledger 恢复。
- 报告未原子保存前进度不得为 100%；失败、限频、重试、降级和 Critic 驳回必须显示真实状态。
- 两个结构化不同的分支必须产生不同的特征快照；若数值相同，报告说明原因。
- `max_depth=2` 时测试中必须真实出现合法二层分支。

### Phase 5：稳健组合方案与复测（2 周，P1）

**改动**：

- 建 LossCube、CVaR/最差分支/换手成本优化。
- 加入行业、单股、现金、流动性、交易状态和财务质量约束。
- 防守/均衡/观察方案自动复测。

**验收**：

- 每个方案均有调仓前后 LossCube 比较。
- 防守方案最差分支损失不得恶化；若无可行解必须明确返回 infeasible。
- 股票候选至少通过财务、估值、行业、流动性、事件暴露五类闸门中的已启用项。
- 不再出现“只因 20 日上涨而进入推荐池”的候选。

### Phase 6：监控、漂移和生产审计（1-2 周）

**改动**：

- 将进程内 daemon scheduler 改为可恢复任务队列/作业表。
- 对观察信号建立自动再运行规则。
- 模型漂移、数据延迟、账号额度/许可、来源失败率、AKShare schema 漂移、跨源冲突率和成本监控。

**验收**：

- 进程重启后任务不丢失、不重复触发同一事件。
- 观察信号被真实证据满足后自动把假设分支升级为新的 ObservedEvent 任务，但保留原假设审计。
- 任一报告可重放到相同输入快照和模型版本。

---

## 13. 必须新增的测试矩阵

### 13.1 PIT 与数据契约

- 未来新闻、未来财报、未来行业成分、未来指数权重一律拒绝。
- 发布日、报告期、抓取日三者错位测试。
- 修订数据必须可重放旧版本。
- RQData/TuShare 的认证、权限、额度和频率限制，以及 AKShare 的网页 schema 变化、空响应和网络失败，必须映射成不同状态。
- `dependency_missing / auth_failed / permission_denied / quota_limited / rate_limited / network_unavailable / schema_changed / no_data` 不得互相伪装。
- source type 选择不能被本地库 fallback 绕过。
- 同一行情序列禁止未复权/前复权/后复权混合，也禁止在缺口处静默拼接另一来源。
- 双源财务、行情或行业版本冲突超过阈值时返回 `conflicted`。

### 13.2 事件与情绪

- 重复转载去重、同源聚类、正反分歧。
- 传闻不能升级为事实；正式公告可以关闭对应假设。
- 情绪缺失时不使用 0 代替，使用 unavailable。
- 高热度低权威来源不能压过官方反证。
- 未接入合规社媒/商业新闻源时必须报告 `coverage_gap`，不得把三家平台的现有文本称为“全网舆情”。

### 13.3 财报与行业

- 实体、期间、币种、单位和版本对账。
- 银行/券商/保险与一般企业使用不同财务指标集合。
- 同业共振与单公司异常能被区分。
- 行业分类变更按点时版本处理。

### 13.4 量化与回测

- 时间切分训练，禁止随机打乱事件时间。
- 区间覆盖率、宽度、CVaR 误差、方向命中率和 calibration curve。
- 与 market-beta、历史分位数、零预测 baseline 比较。
- 不同市场状态、行业、事件类型、样本量分层。
- 参数只能由训练窗口决定；测试窗口不得回写。

### 13.5 Agent

- Tool allowlist、超时、预算、重试和 fallback。
- Agent 不能构造数值 provenance。
- Critic 拒绝后必须发生实际修复或任务降级。
- 状态机必须和真实持久化状态一致。
- 禁止只有字符串存在就算前端能力通过；改用 view model/端到端行为测试。

### 13.6 图与递归

- 所有 edge 端点存在。
- Observed / Derived / Hypothesis 不可串表。
- 二层分支真实运行且受总预算限制。
- 结构相同分支去重，结构不同分支不能被标题去重误杀。
- 分支若不能改变模型输入，不得显示数值影响。

### 13.7 组合方案

- 约束可行性、交易成本、T+1、停牌、涨跌停、ST、最小单位。
- 防守方案在最差分支下不恶化。
- 行业集中度和单股上限同时生效。
- 无财务数据的股票不能被自动新增到“完整推荐”，只能进入待研究池。

### 13.8 页面实时进度与动画

- 用伪 Agent 分阶段发送事件，验证当前 Agent、阶段、完成单位和总体百分比在 1 秒内更新。
- 重复或乱序 `(run_id, seq)` 事件不能让进度倒退或重复完成；重规划增加任务时必须显示新增任务数。
- 多 Agent 并行时显示关键路径 Agent 和并行数量，不能丢失较慢 Agent 的状态。
- run bundle 未提交时最高 99%；提交失败必须显示保存失败，绝不能闪现 100%。
- 页面刷新、Worker 重启和网络短暂断开后可以由 Run Ledger 恢复相同进度。
- 心跳超时、外部工具等待、限频重试、降级、Critic 驳回和不可恢复失败具有不同视觉与文字状态。
- `prefers-reduced-motion` 下禁用流光/呼吸动画；键盘、屏幕阅读器和色觉缺陷场景仍能识别所有状态。
- view model 和端到端测试确认页面不泄露提示词、凭证、隐藏推理或未脱敏工具参数；禁止只检查页面源码中存在某个文案。

---

## 14. 黄金场景如何升级

### 14.1 央行降息 25bp + 平安银行重仓

根事件不再只生成固定 `+1.8%` 市场 proxy。系统自动查询并形成：

```text
利率工具和落地时间
存款利率是否同步变化
银行行业估值与资金拥挤
平安银行点时净息差/资产质量/零售风险代理
社融和地产链验证状态
市场是否提前交易
同业表现和行业广度
```

结构化分支：

1. 存款利率同步下调 + 社融改善。
2. 存款利率不变 + 社融弱。
3. 利好已提前交易 + 财报未验证。
4. 资产质量反证 + 融资资金撤离。

每个分支改变银行行业因子、公司基本面风险状态和资金拥挤状态，再计算 LossCube。Portfolio Agent 自动比较降低平安银行集中度、增加现金、增加低相关行业后的尾部变化。

### 14.2 人民币贬值 3%

自动区分：

- 出口收入受益与进口成本受损。
- 外币负债、航空燃油、外资流向和风险偏好。
- 汇率变化是否已在价格中反映。
- 稳汇率政策和美元指数的二阶分支。

不能再把所有股票简单乘同一个负向 market-equivalent shock。

### 14.3 财报暴雷

自动完成：

```text
预告/正式财报原文
-> 结构化财务与单位对账
-> 相对自身历史和同业的 surprise proxy
-> 一次性项目/现金流/审计意见
-> 事件情绪与价格提前反应
-> 同业共振或个股特有风险
-> 一日/一周/一月条件分布
-> 持仓方案复测
```

---

## 15. 运行结果的最终结构

### 15.1 推演中的实时 Agent 进度页面

用户点击“一键开始完整推演”后，结果区先进入运行态。顶部进度区保持可见，不因日志增长而滚出屏幕：

```text
┌──────────────────────────────────────────────────────────────────────┐
│ 情景推演进行中                                      47%   已运行 01:36 │
│ ●能力探测 ─ ●PIT快照 ─ ●事件证据 ─ ◎量化模型 ⇢ ○分支 ○组合 ○审查 ○报告 │
│ ███████████████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ │
│ 当前：Quant Modeler · 正在计算 12 只持仓的事件条件分布                 │
│ 并行：Sentiment Analyst · 已处理 38/52 条去重新闻    心跳：刚刚          │
│ 数据降级：TuShare 限频，资金流已切换 RQData              [展开时间线] │
└──────────────────────────────────────────────────────────────────────┘
```

页面由三层组成：

1. **吸顶总进度条**：阶段节点、真实百分比、运行时长和当前状态。页面向下查看结果预览时仍可见。
2. **当前 Agent 卡片**：Agent 名称、正在执行的安全操作描述、完成单位、命中数据源、心跳、重试/降级；并行执行时显示额外 Agent 数量和摘要。
3. **可折叠时间线**：按事件顺序显示排队、运行、等待外部工具、重试、降级、完成、Critic 驳回和修复，不展示隐藏推理内容。

动画要求：

- 运行中的进度轨道使用低幅度蓝青渐变流光；当前 Agent 节点使用柔和呼吸光环，Agent 切换时做 240-400ms 的位置和透明度过渡。
- 完成节点变为绿色并播放一次短促的完成扫光；等待/限频为琥珀色，失败为红色且停止流光，降级后恢复为琥珀蓝状态。
- 动画只改变 `transform` 和 `opacity` 等低成本属性，避免整个页面频繁闪烁或重排。进度事件更新与 CSS 动画解耦：没有新事件时流光仍可自然移动，但百分比不会自行增加。
- 支持 `prefers-reduced-motion`；用户关闭动态效果后保留颜色、图标、文字和阶段变化。颜色不是唯一状态表达，所有状态都有文本和图标，并给屏幕阅读器提供 live status。
- 移动端改为单行总进度 + 当前 Agent 卡片；阶段时间线纵向展开，不横向挤压十个节点。

Streamlit 落地方式：

- `visualization/components/agent_progress.py` 只渲染 `ScenarioProgressViewModel`，不自行判断业务完成度。
- 当前 `scenario_page.py` 中写死的 12/25/40/68/82/92/100 百分比全部删除。
- Mission 在后台 Worker 中运行，页面每 500-1000ms 消费 Queue 或轮询 `scenario_progress_events`；项目已有 `visualization/agent_runner.py` 的线程/Queue 逻辑可用于 MVP，但生产运行以可恢复 Run Ledger 为准。
- 页面刷新后通过 `run_id` 重连；没有心跳时展示“等待 Agent 响应”及最后事件时间，不能把卡住显示成“仍在顺利计算”。
- 推演失败时保留已完成阶段和失败 Agent，给出可重试/已降级/不可恢复状态；重新运行创建新 attempt，但关联原 run，不能覆盖旧审计。

普通投资者报告只显示：

1. 一句话结论和可信度。
2. 当前最重要的真实事件与证据状态。
3. 市场情绪、行业、公司基本面和资金行为分别说明了什么。
4. 正常、较差、极端和修复分支的组合区间与 CVaR。
5. 哪些是统计校准分支，哪些只是 Agent 假设。
6. 防守/均衡/观察方案的量化对比。
7. 触发重新推演的可观察信号。
8. 数据不足和模型失效条件。

审计报告另外显示：

- 每个数据域的 `SourcePlan`、实际命中 Adapter、fallback 原因、许可证标签和对账状态。
- 所有 artifact、known_at、模型版本和数值 provenance。
- Agent 的实际工具调用、失败、重试、fallback 和预算。
- 每个分支的结构化状态变化。
- 校准指标、数据覆盖、模型分歧和 Critic 结果。
- 组合约束和不可行原因。

---

## 16. 合规与产品边界

- 系统可以自动生成持仓调整候选和购买观察方案，但不声称替代持牌投顾。
- 报告不得出现收益承诺、必涨必跌或无条件买卖指令。
- 自动研究全流程可以无人参与；真实交易必须由独立交易系统按权限、风控和合规策略处理。
- LLM 不得创建价格、财务数字、概率、目标价或收益区间。
- 未校准的 Agent 分支只能叫“假设优先级”，不能叫“发生概率”。
- 历史回测结果必须披露存活者偏差、未来数据、交易成本、停牌/涨跌停和样本量。
- 数据许可、下载额度、缓存期限和再分发限制必须由部署方按实际合同配置，不能由本方案猜测。

---

## 17. 多轮审查记录

### 审查一：需求覆盖审查

检查问题：是否真正覆盖用户强调的市场情绪、未来事件、行业、财报、预测、持仓推荐和无人参与？

修订结果：

- 增加独立 EventSentiment Module，而不是把情绪藏在新闻评分里。
- 增加 FundamentalAndIndustryExposure Module 和财报原文对账。
- 将未来事件分为统计转移与 Agent 假设两条来源。
- 将持仓方案升级为 LossCube + 稳健优化 + 自动复测。
- 将普通用户几十个开关收敛为 `ScenarioMission`。

结论：覆盖。

### 审查二：当前代码可接入性审查

检查问题：方案是否要求推翻现有代码？

修订结果：

- 保留 `ScenarioEngine` 兼容 Facade。
- 保留 parser、行情、CNINFO/RSS/MCP Adapter、历史类比、报告和 Repository 基础。
- 将现有宏观 proxy、领域分支模板和规则沙盘定义为 fallback Adapter。
- 采用逐 Phase 接管，不要求一次性目录大迁移。

结论：可以渐进落地。

### 审查三：接口真实性审查

检查问题：是否写入不存在或无法确认的 AKShare、TuShare、RQData 端点、套餐能力或历史覆盖？

修订结果：

- 只列三家官方文档中已确认的代表性端点，不把目录能力等同于当前账号权限。
- 明确 RQData 新闻舆情需要 `rqdatac_news`，风险/另类数据可能需要额外授权；TuShare 的积分/VIP 权限运行时探测；AKShare 的网页源存在 schema 漂移和商业使用风险。
- 增加运行时 CapabilityManifest、额度/许可探测、schema canary 和脱敏契约 fixture。
- 用按域 RoutingPolicy 取代全局供应商优先级，并区分认证、权限、额度、限频、网络、schema 变化、空数据和冲突。

结论：组合 Interface 可实现，但最终可用域仍以部署环境生成的账号能力与许可报告为准。

### 审查四：量化严谨性审查

检查问题：是否把大模型叙事、情绪或分支优先级伪装成预测？

修订结果：

- 数值只由 EventConditionedImpactModel 输出。
- 情绪只是事件特征，不直接生成收益。
- 未校准分支不输出概率。
- 强制 Walk-forward、baseline、区间覆盖和市场状态分层。
- 分支必须改变结构化模型状态，否则不显示收益区间。

结论：通过。

### 审查五：反空泛与工程验收审查

检查问题：每项建议能否落到文件、Interface、数据、测试和验收？

修订结果：

- 给出六个核心 Module、Interface、Seam 和 Adapter。
- 给出文件级合并/删除映射、表结构和迁移 Phase。
- 每个 Phase 都有可自动验证的验收标准。
- 明确 P0 是 PIT 和事件条件化模型，而不是 UI 或更多 Agent 名称。

结论：通过。

### 审查六：无人值守与失败降级审查

检查问题：系统是否仍把数据源、分支、重试、降级或结论确认留给普通用户？

修订结果：

- 明确交互任务和调度任务共用 `ScenarioMission`；调度任务自动装载组合、日期和版本化风险策略。
- 单次推演期间不设置人工确认节点，Agent 根据 CapabilityManifest、DataBudget 和 Validator 自动重规划。
- 将权限拒绝、额度不足、区域不可达、数据不足和 Critic 拒绝定义为不同机器状态，禁止静默填默认值。
- 明确无人值守只覆盖研究、预测、持仓候选、复测、报告和监控；真实下单仍处于独立合规边界之外。

结论：运行时研究闭环可以零人工参与，数据或模型失败时返回可审计降级结果，而不是卡住等待人工或编造结果。

### 审查七：多数据源与舆情覆盖审查

检查问题：是否只是把一个供应商名称替换成另一个，或者错误地认为量化数据平台天然等于完整舆情系统？

修订结果：

- 选择 RQData + TuShare Pro + AKShare 的分工组合，并用数据域路由取代全局主备顺序。
- 明确当前项目已经依赖 AKShare，第一步是补来源和 PIT lineage，不是重复接入。
- 将 RQData 新闻舆情、TuShare 新闻/公告/政策语料和 AKShare 热度数据放入不同语义层；热度只作为注意力，供应商情绪只作为待校验特征。
- 保留 CNINFO、交易所、央行、RSS/GDELT/MCP 等 `EvidenceSource Seam`，避免三个量化平台成为单点事实源。
- 增加许可证、缓存/再分发范围、schema 漂移、复权一致性和双源冲突检查。

结论：通过。专业部署默认采用多源组合；经济型部署可以不购买 RQData，但不能降低 PIT、原文溯源和情绪去重标准。

### 审查八：页面实时进度真实性与动效审查

检查问题：进度条是否只是按时间增长的装饰，能否准确说明当前 Agent、并行任务、重试、降级、卡住和最终保存状态？

修订结果：

- 回查当前 `scenario_page.py`，确认现有百分比在 `engine.run()` 前后写死，不能称为实时 Agent 进度。
- 增加 `AgentProgressEvent`、`ProgressSink`、`ProgressProjector` 和持久化事件表，让页面只消费运行事实。
- 使用固定阶段权重和真实完成单位计算百分比；动态任务用流动状态表示，报告未原子提交前上限为 99%。
- 增加吸顶动画进度条、当前 Agent 卡片、并行 Agent 摘要和可折叠时间线，并覆盖刷新恢复、心跳超时与失败状态。
- 动效支持 reduced motion、屏幕阅读器和非颜色状态表达；页面不显示隐藏推理和敏感工具参数。

结论：通过。动画提供生命感，但不参与计算进度，也不能掩盖无心跳、失败或新增任务。

---

## 18. 最终优先级

### 必须立即做（P0）

1. 修复外部证据 PIT 泄漏和 source type 泄漏。
2. 建 `PointInTimeMarketSnapshot` Interface、RQData/TuShare/AKShare/本地 StructuredData Seam 和按域 RoutingPolicy。
3. 建真实事件账本和真实事件回测。
4. 把 `analogs + factors + shocks + simulator + adjuster + backtester` 深化为 `EventConditionedImpactModel`。
5. 让分支通过结构化 `StateChange` 真正改变模型输入。

### 随后做（P1）

1. 财报/行业/资金/情绪点时特征。
2. 自治 MissionController 和能够实际修复任务的 Critic。
3. 真实 Agent 进度事件、吸顶动画进度条、当前 Agent 卡片与刷新恢复。
4. LossCube + 稳健组合优化 + 自动复测。
5. 两套图和两套事实链合并。
6. 原子 Run Ledger、可恢复监控和重放。

### 暂缓（P2）

- 重型外部图数据库。
- 自由群体 Agent 市场仿真。
- 高频订单簿模拟。
- 端到端大模型直接预测股价。
- 未证明样本外价值的自动因子挖掘平台。

V3 的成功标准不是“Agent 更多”或“报告更长”，而是：

> 在任意历史或当前时点，系统都能证明自己当时知道什么、不知道什么；能把事件、情绪、财报和行业状态转成可回测特征；能让结构化未来分支真正改变组合损失；能在没有人工操作的情况下完成数据查询、失败降级、量化、组合方案比较、审查和复盘；同时不让任何 Agent 越过事实与数值的控制线。
