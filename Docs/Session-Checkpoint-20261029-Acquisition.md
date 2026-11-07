# 2026-10-29 采集修复与正式发布续办记录

**本记录最新，优先于 Session-Checkpoint-20261029-Quantitative.md。最新市场证据链已经通过；三个正式发布门槛仍未通过，最新模型研究与正式发布全流程未完成。** 全部代码未提交、未推送，没有下单。不能用旧条件报告、工程测试或当前证据准备成功替代正式发布成功。

## 用户约束与授权

- 优先修复行情、行业、资金流，补联合风险、多期优化、交易资格后跑真实全流程；不降级、不自动提交。没有历史档案时寻找免费高质量接口，若仍不足保留真实阻断。
- 用户本轮两次确认当前持仓，仍为002230总10股、现金50000元；再次要求公开查询佣金等。没有明确提供当前可卖/冻结/权限及账户实际佣金，不能推定10股全可卖，也不能采用旧5股可卖记录。
- 已有央视官方域名持久直连授权、DeepSeek官方deepseek-v4-pro指定字段外发授权保留。本轮真实模型调用0；没有使用或修改旧24/16最终报告续办额度。
- 本轮使用三个编码子代理并做独立交叉审查；不等同于产品内五角色Agent模型验收。

## 最新真实结果

新集成隔离库 `.tmp/quant-integrated-1029.db`，任务 `quant-integrated-20261029`，root `portfolio-team:quant-integrated-20261029`，显式purpose为 `quantitative-input-preparation-v1`，不能在同run-id改成模型研究。

- 请求 `.tmp/quant-integrated-request-1029.json`，行情区间2025-04-26..2026-10-28、新闻联播期次2026-10-28。新SourcePlan固定 `industry=cninfo_named_fields_https_v1`、`cctv_page_checkpoint=same-source-cctv-page-checkpoint-v1`，其余仍原来源/传输。未修改旧冻结契约。
- 成功结果 `.tmp/quant-integrated-resumed-evidence-1029.json`，`evidence_prepared`，冻结时间 **2026-10-29 11:59:47 北京时间**；725条冻结证据。清单哈希 `23d8e2468ebda741759ccff94a396c42a823076268e3be4d72c248d3ccfcc709`；准备结果哈希 `12db35387bdcf11168981808c46a7bf49199a194db99b7c85631a5a1bd731b70`。
- 实际库内：行情364、财报78、行业1、个股新闻100、文本特征100、日资金流100、央视原文11、指数上下文2、组合快照1，共757工件。冻结清单按请求时间范围筛选，故不同于全库条数；本新库没有复制前轮18条分红。
- 最新已收盘价为10月28日38.05元，10股市值380.50元，现金50000元，组合50380.50元；这只是估值，不是建议。原始请求未包含10月29日盘中价格。
- 新CCTV页面检查点实际保存12页（期次目录+11篇原文）。第一次集成运行曾因后续页面SSL错误失败，已保留日志；恢复只补失败页面，最终整期及双指数完整后原子入库。
- 正式检查 `.tmp/quant-integrated-final-validation-1029.json` 使用 `quantitative-prerequisites-v3`，三阶段均blocked，并独立列出recourse/压力输入缺口。CLI非零为真实阻断。汇总证明 `.tmp/acquisition-progress-proof-1029.json`。
- 真实运行脚本 `.tmp/run_quant_integrated_1029.py`、`.tmp/run_quant_integrated_resumed_1029.py`，对应live日志；采集进程均已结束，不并发重复启动。

旧任务也完成恢复：`.tmp/quant-latest-1029.db` / `quant-latest-20261029` 的 `.tmp/quant-resumed3-evidence-1029.json` 已evidence_prepared。保留所有早期data_incomplete结果和追加尝试账本。原10月28日条件模型报告及哈希完全保留，不当作本轮新报告。

## 采集定位与修复

1. 东方财富行情和资金流存在间歇性连接超时/读取失败。同原端点有界对照中，无时间戳和有时间戳请求都曾成功/失败，不能声称nonce是根因或修复。腾讯免费行情控制请求也成功，但没有切换任何任务来源。证明 `.tmp/market-network-diagnostic-1029.json`、`.tmp/price-freshness-diagnostic-1029.json`。
2. 同契约恢复得到行业、资金流和行情全部数据；行情逐区间检查点保留已成功请求，沪深基准交易日与证券覆盖继续严格核对。没有填缺日或取消证书校验。
3. 巨潮SDK无单次网络超时且依赖列位置。新增 `cninfo_industry.py`，同官方HTTPS POST、命名字段解析、证券/日期/完整count校验、10/30秒超时、关闭JS上下文、保留原始响应/实际接收时间/hash。真实industry12行/profile1行通过。`public_transport/public_worker/scenario_data_prep/deductive_research` 由冻结source_policy显式传入profile，子进程再校验；无新键的旧契约保持旧SDK。
4. 新增 `checkpointed_context.py` 页面级原始字节/hash/receipt检查点，坏页面不缓存，损坏缓存拒绝；最终仍需完整期次与双指数。`market_context` 错误现在仅暴露固定来源URI、原异常类型与secid，不保存潜在敏感异常文本。父任务保存安全诊断。
5. 央视直连规则还在，但运行与持久模式被切回global，规则未生效。本轮按已有授权备份并恢复rule；两央视域DIRECT、其余MATCH,GLOBAL、全部代理选择保持不变。`.tmp/cctv-restored-route-proof-1029.json`证明index200，后续只读确认仍rule。仅模式字段变化；备份在用户Clash配置目录的 `stockm1-cctv-mode-backup-20261029T033640`。不得把凭据复制进仓库。

## 量化工程与独立审查

- `joint_event_fit.py` 与 `wealth_calibration.py`：事件条件拟合、同步残差扣除事件解释量、相同生产财富路径引擎、原始价/拆股/保留分红独立标签、完整模型/无事件/逐特征消融、逐日回撤检验；修正跨原点相关性聚合为均值/二阶矩混合协方差。
- 当前财富回放明确是 **archived_origin_forecast_replay**。历史内部预测档案不是用户明确要求、也不是所有合法历史回测的前提；只依历史可见资料重新训练并重建投影的历史回放尚未实现。不能把该工程缺口伪装成“只差免费API”。见 `Docs/Event-Wealth-Calibration.md`。
- 新 `wealth_source_binding.py` 在任何拟合前核对证券/会话/实际原价、基准收益重算和原文情绪重算。**完整公司行动数值与无行动覆盖验证尚未实现**，当前分红源缺配股覆盖；手填verified不能通过。这是工程和数据双重缺口。
- 费用按显式账户合同以人民币分计算，Gurobi整数分币约束与独立Decimal复算一致。`distribution_cashflow.py` 区分登记权益、除息应收与实际到账；卖出不抹掉已登记权益，缺最终税额/存在递延税时拒绝。
- `empirical_recourse.py` 与 `recourse_validation.py` 已实际接入验证链：冻结可观察策略、独立成熟路径复算、有限压力反例搜索，按原始工件逐字段绑定，核对真实可见时间和交易日历。未知状态与失败订单保留失败，不默认持有。
- 独立审查复现并修复四项P1：未来节点冻结/可卖被忽略；仅靠信息集标签泄漏未来；同登记分红权益换ID重复记账；校准矩阵引用无关工件仍进入统计拟合。修复后独立复测关闭。
- `quantitative_validation.py` v3 接入财富/费用/分红解析、源绑定、独立recourse/压力检查，仍是前置验证链，**不能签发最终正式发布资格**。前端继续保持三项简明状态，区分当前证据已齐与历史/账户验证未通过；不增加采集按钮。

## 正式发布仍需的工作

1. 实现可复现历史投影重建或获取真实原始预测档案，并补可靠PIT/公司行动覆盖及成熟检验。完整生产事件分支混合概率、公司行动预测和可用现金/税后财富范围尚未认证。不是改变scope布尔或手写指标能解决。
2. 多期求解仍需真实独立成熟路径、概率不确定集合校准、充分的压力域与统计误差控制。当前有限压力集合检查不证明连续全状态空间安全。
3. 当前可卖/冻结/权限、实际佣金及计费归并口径、分红递延扣税批次仍未知。公开费率已核验并保存 `Docs/Public-Trading-Fees-20261029.md`（含官方A股佣金不足5元按5元规则），但不能将公共规则当用户账户合同。
4. Gurobi仍为非生产受限许可证；不能冒充已具备正式生产部署授权。没有购买/申请许可证。
5. 真实门槛通过后再进行最新完整模型/正式发布验收。当前prepare_only不能原地变更purpose；需新研究契约或明确的冻结证据续办机制，不能编辑旧预算或旧工件冒充成功。

## 验证与额度

解释器 `.tmp/advisory-venv/Scripts/python.exe`，测试环境 `STOCKM1_SCENARIO_LLM_ENABLED=0`，不改.env。全量第一轮383通过、1失败，仅新增测试夹具少event_time属性；修复夹具后专项4通过，最终全量 **384 passed in188.03s**，日志 `.tmp/pytest-all-acquisition-final-1029.log`，session90001已结束。`git diff --check`通过。全部早期失败日志保留。相同已完成prepare任务再次打开成功，结果哈希、市场工件和采集/模型调用计数不变，证明 `.tmp/quant-idempotence-proof-1029.json`。

额度最近检查已用33%，没有接近上限，没有使用重置或充值；本记录保存是为了准确恢复，不代表工作完成或额度耗尽。

## 恢复提示词

继续StockM1，先读Docs/Session-Checkpoint-20261029-Acquisition.md和git status，保留全部未提交改动，不提交推送、不下单、不降级。最新市场采集已通：.tmp/quant-integrated-1029.db，quant-integrated-20261029，prepare_only，725冻结证据，10月28日收盘38.05、央视11篇和双指数齐；冻结11:59:47北京时间，真实模型调用0。结果.tmp/quant-integrated-resumed-evidence-1029.json，正式检查.tmp/quant-integrated-final-validation-1029.json为v3 blocked，汇总.tmp/acquisition-progress-proof-1029.json。旧条件模型报告保持不变，不当新报告。采集已补命名字段巨潮reader、显式SourcePlan、CCTV页面检查点与安全失败URI；央视mode曾global，本轮按已有授权恢复rule仅央视DIRECT其他MATCHGLOBAL。量化已补事件财富/消融、分币费用、登记分红现金、独立成熟路径与有限压力检查，关闭四个P1；但完整公司行动数值/无行动覆盖、可复现历史事件投影重建、可靠PIT/成熟样本、事件分支概率、真实recourse及账户交易/税费事实仍缺，前置检查不能签发最终发布。用户重复确认总持仓，未给可卖/冻结/佣金，官方公开费用在Docs/Public-Trading-Fees-20261029.md，不推定全可卖或默认费率。先核对最终全量测试日志和源绑定审查，再继续剩余工程与真实数据验证；需要新完整模型任务不能修改prepare_only旧契约或复用已耗尽24/16预算。额度将尽保存进度和新恢复提示词。
