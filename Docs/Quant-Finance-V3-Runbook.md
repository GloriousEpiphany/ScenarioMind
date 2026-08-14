# Quant Finance V3.1 严格运行手册

## 生产入口

唯一生产推演入口是 `ScenarioEngine.execute(ScenarioMission)`。Streamlit 的“情景推演任务”页面和 `ScenarioTools.execute_mission(...)` 也只提交该契约。主 facade 上调用旧 `run()` 会直接抛出 `ContractViolation`；迁移测试若仍需旧行为，必须显式导入隔离的 `LegacyScenarioEngine` / `LegacyScenarioTools`，其结果不能作为 PIT、校准概率或组合建议的生产依据。

V3 不会从 `stock_daily` / `index_daily` 推断历史可见性，也不会自动改写截止日、补默认持仓、切换数据源、生成默认 beta、用静态宏观映射制造收益数字，或在报告未原子提交前显示 100%。

## 启动前必须具备

1. `market_artifacts` 已写入目标组合所需的版本化记录；每条记录的 `payload_hash` 必须与 canonical JSON 的 SHA-256 一致。行情工件还必须显式记录 `frequency` 与 `adjustment`；旧 V3 行情缺任一字段时启动失败并要求审计迁移。
2. `event_ledger` 至少存在一个与当前持仓相关、在任务 `as_of` 已知的 `ObservedEvent`；根事件必须包含结构化 `state_changes`。
3. 至少存在一个结构化 `HypothesisEvent`。没有可量化分支时任务失败，而不是生成自然语言分支后继续给数字。
4. `model_versions` 存在训练截止日在 `as_of` 之前的 `active` 模型；策略必须输出 1/5/20/60 交易日，并登记校准版本。
5. 任务计划要求下列精确 `Domain/Dataset`，同域的其他数据集不能替代：`price/equity_price`、`event/observed_event_source`、`event/event_study_observation`、`event/analog_state_observation`、`fundamental/pit_financials`、`industry/industry_membership`、`flow/money_flow`、`sentiment/event_text_features`、`market_regime/market_regime`、`quant/factor_exposure`、`quant/factor_residuals`、`portfolio_constraint/asset_constraint`、`portfolio/portfolio_snapshot`。
6. `market_artifacts` 中存在 `domain="portfolio"`、`dataset="portfolio_snapshot"`、`entity_id="current"` 的点时组合快照；其现金、数量、价格、权重和总资产必须逐项对账。`portfolio_constraint` 还须为每个持仓提供同一时点的交易约束：T+1 可卖数量、停牌、涨跌停、ST、上市天数、最新价、账户总值和最小交易单位。

任一条件不满足时，系统抛出带稳定错误类型的异常，例如 `DataUnavailable`、`DataConflict`、`PointInTimeViolation`、`InsufficientModelData`、`UncalibratedModel` 或 `InfeasiblePortfolio`。

## 当前持仓识别与快照发布

“情景推演任务”页顶部的“当前持仓智能识别与 V3 快照”是 `CurrentPortfolioSnapshotModule` 的界面投影：

- 唯一账户或唯一有持仓账户会被自动预选；多个有持仓账户同时存在时必须人工确认，系统不猜测。
- 页面逐只显示数量、成本价、最新可见 V3 估值价、行情日期与行情 Artifact ID。成本价只用于对账展示，不参与快照估值。
- 任一持仓缺 V3 行情、数量非正整数、成本或行情价格非法、账户现金非法时，发布按钮禁用或发布显式失败。
- “同步账户并发布 current 快照”在一个 `BEGIN IMMEDIATE` 事务中读取账户和持仓、绑定 V3 行情 Artifact、计算总值与权重并写入不可变组合 Artifact；不存在先读后写的并发窗口。
- 页面以账户现金、账户日期、持仓数量/成本/更新时间及最新行情 Artifact ID 计算 `capture_fingerprint`。该指纹与已发布快照不一致时，“一键开始完整推演”保持禁用，防止旧持仓被误称为当前持仓。
- `account`/`positions` 均为空时，页面会明确报告“没有可识别持仓”。系统不会从 Agent CSV 日志、`stock_daily`、成本价或默认标的推断当前组合。

首次建立模拟账户及录入真实数量可使用：

```powershell
python main.py create-account 我的组合 --capital 1000000
python main.py load-account 我的组合
```

快照只解决组合引用；事件、财务、行业、资金流、情绪、因子、交易约束和校准模型仍须分别满足启动条件。

## 数据源部署

RQData、TuShare Pro 和 AKShare 均为延迟导入的可选 Adapter。不要把 SDK 加回核心 `requirements.txt`。每个生产环境应根据真实许可证、地域、字段权限和契约 fixture 生成自己的精确 lock 文件；未经逐数据集 canary 验证的 SDK 版本不得标记为 `available`。Adapter 构造器接收数据集专用 row fetcher、normalizer 和 probe，不能用一次账号探测把所有数据集整体标记可用。

四种日行情统一经过 `normalize_daily_price_rows`：RQData 的股/元保持原单位，TuShare 的手/千元转换成股/元，AKShare 和腾讯的成交量从手转换成股。腾讯 `appstock` 当前的 6 字段历史日线不提供成交额；除权除息日的第 7 项是公司行动对象，不是成交额。工件将 `amount` 保存为 `null` 并记录 `amount_not_provided_by_source`，公司行动原样收录到 `source_metadata`；禁止填 0 或估算。外部历史响应的 `known_at` 等于真实入库时刻，不会回填成历史交易日；因此今天第一次抓到的历史记录只能服务截止时点不早于本次入库的任务。

腾讯行情的严格 V3 入库命令如下。`--end-date` 必须显式指定已收盘交易日；盘中当日日线会因 `event_time=15:00` 晚于真实入库时刻而失败，不会被悄然丢弃或作为收盘价使用。

```powershell
python main.py ingest-v3-prices --symbol 000001 --start-date 20240801 --end-date 20260813
```

不传 `--symbol` 时将处理 `stock_info` 中的全部标的；默认按 `market_artifacts` 中腾讯 V3 工件的最新交易日增量下载。该命令不会导入或改写 legacy `stock_daily`。普通 `download-stocks` 命令可用 `--source tencent_http` 显式选腾讯，但它仍属于 legacy 交易模拟数据路径，不足以解除 V3 就绪阻断。

全市场批量时，腾讯成功响应但某标的在增量区间内无日线，会记录为类型化覆盖缺口。已有 V3 历史工件的退市、停牌或无新交易标的会明确跳过，不中止其他标的；从未在请求区间出现过记录的标的会记为失败，全批结束后命令返回非零。审计详情原子写入 `data/v3_tencent_ingestion_audit.json`。网络、JSON、schema、单位或入库错误仍立即中止，不会被按覆盖缺口吞掉。

跨源故障切换只有 `RoutingRule.allow_audited_failover=True` 时才允许，并且前后来源的口径、复权、币种、单位、时间戳、修订与许可范围必须在部署策略中证明兼容；结果显式记录 `audited_failover_used` 和失败来源。默认规则不切换来源。

## 持久化与进度

以下产物由同一个 SQLite 运行账本管理：

- `scenario_missions` / `scenario_mission_steps`
- `scenario_progress_events`
- `scenario_branch_specs`
- `feature_snapshots`
- `scenario_loss_cube`
- `portfolio_proposals` / `portfolio_proposal_comparisons`
- `validation_results`
- `scenario_run_bundles`
- `scenario_execution_jobs`
- `scenario_retry_jobs`
- `scenario_monitoring_jobs`
- `scenario_observed_event_tasks`

根情景和每个分支都必须生成不同的 `feature_snapshots`，且特征快照 ID 进入所有数值结果的 provenance。最终完成进度事件、分支监控任务、run bundle 和 `SAVED` 状态在同一事务中提交；事务失败时页面不能出现 100%。执行 Worker 和监控 Worker 均使用独占租约与持续心跳；进程中断后只允许认领已过期租约，旧 attempt 明确记为 `worker_lease_expired`，新 attempt 使用关联的新 run ID，不覆盖旧审计。没有真实 `next_check_at` 时不猜测轮询间隔。

任务失败状态必须区分 `DEGRADED`、`RETRY_SCHEDULED`、`INSUFFICIENT_DATA`、`REJECTED_BY_CRITIC` 和 `FAILED`。只有提供方给出带时区的明确重试时点时才创建 `scenario_retry_jobs`。

组合方案由 `scipy.optimize.milp` 的 HiGHS 后端求解整数交易手数、CVaR、最差分支、换手、成本、现金、行业、集中度、T+1、停牌/涨跌停和流动性约束。只接受 solver status 为 proven optimum 的结果；不可行返回 `InfeasiblePortfolio`，超时或非最优终止返回 `OptimizationFailure`，不做四舍五入或启发式补解。

## 验证

```powershell
& 'C:\Users\29643\Anaconda3\envs\stock\python.exe' -m pytest -q
```

CI 不访问公网。数据源真实能力由独立 canary 使用部署许可证执行最小查询，并保存脱敏 `CapabilityManifest`。
