# StockM1 情景研究领域模型

## 目的

StockM1 在明确的历史或当前可得时点上收集可追溯事实，形成条件情景研究与来源绑定的数值估计。只有联合模型、校准和交易约束独立验收通过后，才发布数值持仓方案。当前因子基准路径不能冒充语义事件的已校准影响。它不执行真实交易，也不让大模型生成价格、收益、概率或财务数字。

## 核心概念

### ResearchRequest（条件研究请求）

当前持仓研究的冻结请求：账户状态指纹、目标、偏好、来源范围、模型和全队预算。总控组织专家、量化工具和跟踪，产物可以是明确限制的条件研究。该状态不代表严格数值发布已获批准。

### ScenarioMission（严格数值任务）

严格数值引擎的任务入口，包含组合引用、带时区的 `as_of` 和版本化 `RiskPolicy`。它和条件研究请求有不同的验收契约；不能把完成专家报告称为 ScenarioMission 的校准、优化与发布已经通过。部署来源和模型配置进入冻结契约，任务失败时不得静默更换。

### MarketArtifact（市场事实产物）

可计算数据或文本证据的不可变版本。每个产物同时记录事件时间、发布时间、系统可得时间、入库时间、来源、许可、schema、修订、频率、复权口径和内容哈希。没有这些 lineage 的旧行情表不能被称为 PIT 数据。

### MarketSnapshot（点时市场快照）

`ScenarioMission.as_of` 时刻系统依法且实际可见的 `MarketArtifact` 集合。快照必须能报告最大 `known_at`、命中的 `SourcePlan`、缺口和冲突；未来事实、当前网页回填历史和静默跨源拼接一律拒绝。

### CurrentPortfolioSnapshot（当前组合快照）

可变 `account/positions` 在明确捕获时点的不可变投影。发布前必须逐只持仓命中截止捕获时点可见的 V3 `price/equity_price`，并把账户现金、数量、行情 Artifact ID、行情事件时间、权重、总值和源状态指纹写入 `portfolio/portfolio_snapshot`。多个有持仓账户并存时必须由用户明确选择；任务永远不直接读取可变持仓，也不把 Agent CSV、成本价或 legacy 当前价当作估值行情。

### SourcePlan（来源计划）

`domain × dataset × known_at × quality × entitlement × latency × budget` 决定的显式来源选择。failover 只有在策略允许且语义、单位、复权、时间戳和许可兼容时成立，并必须记录失败来源与原因。

### ObservedEvent（已观察事件）

有原文哈希、来源 Artifact、事件时间和可得时间的事实事件。传闻阶段不能被升级成已确认事实。

### DerivedEventState（推导事件状态）

由确定性规则或版本化模型从事实产物推导的状态。它必须列出输入 Artifact 和模型版本，不能写回事实记录。

### HypothesisEvent（假设事件）

由 Agent 根据证据缺口、反证或暴露生成的压力测试假设。它只能进入分支记录；未经时间切分校准时只有优先级，没有概率。

### StateChange（结构化状态变化）

分支对量化输入的明确修改，例如 `factor:interest_rate=-25bp` 或 `credit_impulse_state=weak`。只改变标题或自然语言、没有改变模型状态的分支不能获得数值输出。

### ScenarioLossDistribution（情景损失分布）

事件、分支、资产和期限下的收益分位数、派生价格分位数、CVaR、样本量、模型分歧、覆盖率、校准版本和数值 provenance。数值只由确定性量化 Adapter 产生。

### LossCube（损失立方体）

`branch × horizon × asset × quantile` 的组合决策输入。PortfolioPolicy 在 LossCube 上同时控制 CVaR、最差分支、换手、交易成本、集中度和模型不确定性。

### Run Ledger（运行账本）

任务状态、真实 Agent 进度事件、分支、LossCube、组合方案、审查和报告的持久化记录。run bundle 必须在一个事务中提交；提交之前进度最高 99%。

## 不变量

- `published_at > mission.as_of` 或 `known_at > mission.as_of` 的产物不可见。
- Observed、Derived、Hypothesis 三类记录不可互相冒充。
- 缺失、冲突、未校准和不可行是显式状态；禁止用 0、默认 beta、固定收益 proxy、空列表或措辞修饰伪装成功。
- 同一价格序列不得混用来源、时区、频率或复权口径。
- 未校准分支不得暴露概率；LLM 不得创建数值 provenance。
- 防守方案不得恶化最差分支；无可行解必须返回 `portfolio_infeasible`。
- 页面进度只投影持久化 `AgentProgressEvent`，不按计时器或前端脚本增长。
- `current` 组合快照与所选账户源状态指纹不一致时，页面不得创建新任务；同步发布必须在读取账户状态与写入 Artifact 的同一事务内完成。
- 主 `ScenarioEngine` 不执行隔离的 V2 `run()`；生产调用只能提交 `ScenarioMission`。
- 组合优化只接受求解器证明的最优整数解；超时、非最优终止和不可行不得被启发式权重替代。

## 范围与完成状态

当前研究不依赖补齐 2008—2016 年公告。长期行情和公司行动是否需要取决于模型窗口、周期或压力检验；不以年份直接判断价值。

CurrentEvidenceReview（当前证据复核）保存完整原件，围绕待核验论点读取精确片段。接收时间、事件时间和独立历史捕获时间分开；引用包含原件哈希和 Unicode 偏移，且必须对应实际读取片段。它不表示全文审阅或新的当前估值。

普通新组合研究中 CCTV 为附加来源；双交易所基准和持仓财务/行业暴露仍必需。附加来源缺失保留为 evidence_gaps，并限制相应事实断言。明确要求完整节目分析的任务以及旧冻结合同保留完整性要求。

AnnouncementOccurrence（公告出现）是窗口内任意公告的二元事件；ProfitGuidanceOutcome（利润预告结果）是实绩相对公司预告的区间分类；两者都不是市场预期误差或价格因果影响。公告出现的长期前瞻协议是独立实验，不阻断普通条件研究。
