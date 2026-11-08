# 独立多期路径与有限压力验证工件契约

入口：`src.stock_app.recourse_validation.evaluate_acquired_recourse(repo, refs, cutoff, problem, proposal)`。不采集网络、不调用模型、不下单；从不可变 `MarketArtifactRepository` 读取输入并执行真实复算。即使上游优化问题/最优解缺失，也检查并分别报告全部四类真实输入。返回 `publication_eligible=false`，`checks_executed` 只表示执行了检查，失败的回放仍是失败。

`refs` 四键为 `recourse_policy`、`recourse_protocol`、`recourse_paths`、`stress_paths`，值必须是已采集工件 ID。四者 `domain=quant_validation`，`dataset` 分别与键同名。不接受 `passed`、自报指标或内联路径来代替采集数据。

`recourse_policy` 的 payload 是 `FrozenRecoursePolicy`，绑定原问题哈希、最优解哈希、可观察分区与训练来源。登记时间必须同时等于工件的 `known_at` 和 `ingested_at`。引用的 `recourse_partition` 工件只含 `rules` 与 `training_source_refs`，必须在登记时已经实际捕获，且与策略逐值一致。

`recourse_protocol` 的 payload 是 `RecourseEvaluationProtocol`，含注册时点、评价开始日、最低非重叠路径数及协议依据 ID。登记时间同样不能早于捕获时间。策略和协议必须先于独立评价区间登记。

`recourse_paths` 的 payload 仅含 `paths`。每条路径只含 `path_id`、`observation_refs`、`calendar_ref`；`observation_refs` 为按实际顺序排列的工件 ID。`stress_paths` 除上述 `paths` 外，还需要 `reference_observation_refs`，作为明确的压力距离基准。

观察工件使用 `domain=quant_validation`、`dataset=recourse_observation`，payload 仅含：

- `node`：完整 `DecisionNode` JSON。
- `observation_kind`：`realized` 或 `hypothetical_stress_state`。后者不能用于经验验证。
- `bindings`：每项含 `target`、`source_ref`、`source_path`。两个路径均为 JSON 键/数组索引列表；源值须与目标值完全相同，绑定不允许重叠或遗漏任何价格、交易资格、公司行动及交易期索引字段。

经验观察的 `node.at` 必须等于观察工件真实 `known_at`。所有底层源必须在该时点可见。价格必须绑定同一交易日、对应证券的 `price/equity_price`、`frequency=1d`、`adjustment=none` 的 `close`；模型输出不能伪装为实际收盘价。交易资格绑定 `quant_validation/account_execution_state`，公司行动及无行动覆盖绑定 `corporate_actions/corporate_action_state`，交易期索引绑定 `calendar/trading_sessions`。事实的权限及来源引用也必须存在且当时可见。

日历工件含 `sessions=[{session_index, date}, ...]`；每个观察的上海日期和交易期索引须逐项吻合。日期不能由简单“周一到周五”规则推定。新下载的旧历史记录不能改写成历史上已经可见。

2026-10-30 校验补强：对所有 `observation_kind=realized` 的观察（包括压力集合中的实际观察），公司行动必须按 `["splits", symbol]` 或 `["cash_dividends", symbol]` 逐字段绑定；原始工件entity_id、上海交易日期以及source_path须与目标精确匹配。交易资格source_path也须等于target，不允许以buy_allowed冒充sell_allowed，或借用同值的无关字段。来源中另一个证券/日期的0或1不能证明当前没有分红/拆股。此项校验不替代完整公司行动/无行动采集证明，也不等于已实现账户原始资料的provider解析。

实际回放只向冻结策略提供当期可观察价格，不把后来实现的价格交给优化器重新选动作。没有唯一适用规则时明确失败。失败路径不会从样本分母删除；费用、现金、股数、冻结/未交收、回撤和防守方案对照由独立算术重新计算。

有限压力搜索会遍历提供的全部状态，并按相对基准的最大绝对对数价格变化排序反例。其范围只是明确提供的有限状态，不证明连续全状态空间安全，不替代事件概率及不确定集合校准。真实样本、抽样不确定性、当前券商事实和最终发布门槛仍需通过。
