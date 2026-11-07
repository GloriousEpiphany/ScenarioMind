---
status: in-progress
branch: main
base_commit: ba54d1f
date: 2026-10-28
---

# 持仓驱动多 Agent 与页面重构恢复记录

> 2026-10-29 续办状态与最新24/16授权见 [最新恢复记录](Session-Checkpoint-20261029.md)，该记录优先于本页历史进度。

本记录会在本轮验证结束时更新。目前未完成真实最终验收，不能将局部结果描述成全功能或量化发布成功。全部 Git 修改保持未提交、未推送，保留前轮已有改动。

**北京时间 2026-10-28 中午最新状态（重要）**：初始五阶段已完成，但最后人工语义复核发现“9个月与全年增速直接比较”和“清仓后的现金仍有股票beta”两处错误，不能交付初始报告。原报告已保留为 `.tmp/portfolio-report-initial-1028.json`，初始模型审查通过不等于最后复核通过。已进入真正的情景/持仓重审，前三专家和冻结数据复用，原总预算144/112不增加；从初始已消耗101次模型/84次工具后剩余43/28严格封顶。

**当前实际进程 session 1448**，日志 `.tmp/portfolio-correction-live-1028.log`，脚本 `.tmp/run_portfolio_correction_1028.py`；修订根 `portfolio-team:portfolio:d258cd1f0ab445a69d18cd4980634c7d:report-revision:fb4cda634f0aae11cd56`，正在情景独立审查。初始 session7984 已正常结束。全量 **297 passed in 273.00s** 后新增了报告修订/不中断原始记录/预算不足不回退的回归，最新全量 `.tmp/pytest-all-1028-final6.log` 在跑（session56759），预期约300项。Codex 周额度最后读取4%，未临近耗尽、未兑换重置。

此前在第57次模型尝试遇官方HTTP402，停止保存后用户回复“我会补充余额，之后继续原任务”；只读官方余额确认is_available=true后才恢复，无换模型或自动充值。保留该中断与恢复证据。

## 用户当前目标与约束

先恢复记录，优先解决情景推演未完成问题，再跑真实全流程。持仓录入后自动采集新闻、财报、市场情绪、新闻联播与当前市场信息；完整专业 Agent 架构、系统提示与情景算法；综合评估、逐仓影响、条件操作与对冲。前端按投资者习惯简化。不得用降级、空值、换弱模型、跳过审查、伪造校准掩盖失败；不自动提交。额度将尽时保存进度并输出恢复提示词。

## 本轮已获用户明确授权

1. **持久网络调整**：用户明确回复“允许，仅央视官方域名持久直连”。只允许 `cctv.com`、`cntv.cn` 直连；其余流量保持原代理选择。已实施，详见下文。
2. **真实持仓发送模型**：自动审批先因缺少具体数据/目的地授权而拒绝真实执行。随后用户对“证券代码、数量、成本、现金/组合总值、权重、公开市场证据、投资偏好发送到 https://api.deepseek.com，模型 deepseek-v4-pro”明确回复“允许，按上述范围完成真实模型验收”。已排除账户名和账户标识；此授权不是对其他目的地或扩大 payload 的授权。

## 实现与文件

- `src/stock_app/portfolio_research.py`：LangGraph 总图，宏观、公司、情绪资金、情景综合、持仓决策五个专业 AgentRuntime 子图，各自独立证据审查；研究黑板、冻结输入、账户变化拒绝、租约/心跳、全队调用预算、真实事件持久化。
- `scenario_engine/agents/portfolio_roles.py`：版本化任务与算法：事实→传导→替代解释/已计价→相互强化抵消→父子路径→逐仓映射→不交易/现金减仓对冲→反证。
- `scenario_engine/agents/portfolio_assessment.py`：结构化逐仓影响、动作条件、现金/直接减仓对冲、剩余风险；强制逐仓覆盖、有效证据与已审查情景 ID；不生成未校准概率、目标价、交易数量。
- `src/stock_app/market_context.py`：完整一期央视原始 HTTPS 文字稿、沪深指数真实背景。任一稿件失败则整期失败；指数回报只表示已观察值。固定 source/profile，证书校验与无重定向保留。
- `src/stock_app/checkpointed_prices.py`：每个已成功验证的东方财富区间请求单独保存不可变检查点，后续失败不重下载已有成功请求。
- `deductive_research.py`：兼容原契约的 `acquire_only`；新任务 source_policy 中冻结 `max_task_attempts=3`，失败尝试逐条记账，不换源。
- `src/stock_app/portfolio_research_jobs.py`：持久任务队列、独立进程 worker、无页面连接恢复、错误类型/安全诊断；CLI 新增 `portfolio-analysis`。
- `visualization/portfolio_research_page.py`：持仓驱动自动任务，四个结果标签，证据与审查可展开；默认不手填目标、逐项下载或发布快照。`scenario_page.show_scenario_page` 已指向新页，原严格量化控制台仍可从高级工具进入。
- `runtime.py` 新增可冻结的角色提示、完整原文 ID 覆盖检查、独立审查候选 assessment；旧默认角色提示/契约保持兼容。
- `ToolArgumentRejected`：真实宏观 Agent 曾把 event_text_features 查到 event 域，原先会使整个图中断。现在仅对“每个枚举值都在已批准范围内、组合填错”的只读请求返回明确拒绝反馈，记录失败、允许修正；未经授权的工具、额外权限字段、越界证券仍硬拒绝。数据缺失仍是 blocking 错误；错误查询不能算来源覆盖。
- 新增 tests：market_context、portfolio_research、checkpointed_prices、agent_input_repair；旧页面测试改为验证自动建任务且重复刷新不重复启动。CCTV 同一时间不同稿件的 URI 身份已加入仓库去重键。
- 已确认生产库有 497 条腾讯旧行情。新采集按 source_policy 过滤各证据源；组合快照支持显式来源 + 冻结行情工件 ID，防止旧源或后到修订混入。保留默认不明确来源时的冲突闸门。真实混源核验 `.tmp/mixed-source-real-proof-1028.json`：497 条原腾讯 + 364 条本轮真实东方财富日线，默认仍报冲突，明确冻结计划可核对到本轮同一估值；生产库只读。
- 同账户后台任务串行，反复编辑偏好会合并尚未执行的旧请求；已运行任务与证据保留。独立 worker 会继续执行最新排队项，页面离开不影响执行。完成/失败后停止前端定时轮询，阅读报告时不反复刷新。
- 模型错误保存安全诊断；新增 job_errors 追加表与 model_failed 事件，避免恢复后抹去错误。HTTP 402 对用户明确展示账户余额问题，不反复盲重试。
- 新增 `portfolio_report_revision.py`：最后复核阻断→复用冻结数据/前三专家→剩余原预算内重做情景与持仓及独立审查→保存新不可变报告。原始team_result不覆盖；默认读取投影只返回最新已通过修订，未通过时阻断旧成功结果；普通CLI恢复也能进入待修订任务。`record_assessment` v2明确相同财务窗口、清仓后直接权益beta为零、有限新闻样本不能证明世界无负面公告。

运行手册：`Docs/Portfolio-Research-Runbook.md`。

## 网络证据与当前持久状态

多客户端访问央视原代理路径 TLS 握手失败；限域 DIRECT 对照 HTTP 200，实验已恢复，证明 `.tmp/cctv-direct-proof-1028.json`。

获用户持久授权后：`Clash Verge/config.yaml` mode=rule；全局 `profiles/Script.js` 前置两条央视 DOMAIN-SUFFIX DIRECT 和 `MATCH,GLOBAL`，保留原规则在之后；运行配置/内核一致，原 Selector 选择保留。只在用户应用配置目录保存原始配置备份 `stockm1-cctv-backup-20261028`，不将凭据复制到仓库。证明 `.tmp/cctv-persistent-route-proof-1028.json`：13 篇完整 2026-10-27 稿件，persisted=true。

不可恢复到 10 月 16 日的全 global 旧状态，用户本轮已授权持久改动。未来不得借此扩大其他域名的直连授权。

## 真实执行位置（当前仍在进行）

独立验证库 `.tmp/portfolio-live-1028.db`，只复制真实有持仓账户及持仓/证券名表，没有复制旧市场工件或交易表。不改生产持仓，不下单。

- 旧任务 `portfolio:9fcbf6999ef44728a4a89a84c258e3e5`：两次原策略重试后仍 data_incomplete，保留，不修改契约。
- 新任务 `portfolio:d258cd1f0ab445a69d18cd4980634c7d`：固定新三次同源策略与请求检查点，六域采集全部成功（持仓 002230，日线区间 2025-04-25 至 2026-10-27），6 个行情 HTTP 请求检查点。上下文失败 ConnectionError 后恢复成功，已冻结组合和全证据，进入真实模型。
- 宏观 Agent 前 5 次读取成功，第 6 次工具参数域/数据集组合错误导致 ContractViolation；另一次假设只用价格作机制依据被拒绝。均已提供保持严格约束的失败反馈，要求正确查询/重新提案，没有放宽证据规则。宏观独立审查首轮因遗漏公司财务反证而退回修订，后通过；公司阶段也已通过。当前情绪阶段独立审查进行中，不能先声称全部专业角色完成。
- 最新执行日志 `.tmp/portfolio-live-model-resume-1028.log`；此前日志 portfolio-live-1028.log、portfolio-live-retry-1028.log、portfolio-live-retry2-1028.log、portfolio-live-checkpointed-1028.log、portfolio-live-context-retry-1028.log。
- 初始输出 `.tmp/portfolio-report-initial-1028.json` 与初始证明 `.tmp/portfolio-full-proof-initial-1028.json` 仅证明初始工程流程，包含已识别的语义错误，不能交付为最终成功。`.tmp/portfolio-full-proof-1028.json` 现明确为 post_review_required。修订成功后新结果仍写 `.tmp/portfolio-report-1028.json`；须再次人工核对上述错误确已修正、重做相同任务的无重复调用核验、再查前端。
- 证明脚本 `.tmp/verify_portfolio_full_1028.py` 已更新为必须 report_revision>=1，并合并原root与所有修订root的模型/工具计数，断言仍<=144/112；成功后先不带参数保存baseline，再同CLI --run-id重开，然后 --compare。它只读真实账本，无假接口。当前 baseline文件仍是初始版本，修订成功后需要重新生成。

当前模型修订进程 session **1448**。Streamlit 验证旧服务曾在8513/8514，尝试重启8514时端口仍占用，不能认为最新代码已装载。可在最终报告就绪后用新端口8515启动最新验证服务，或仅按明确脚本名定位并停止自建验证进程后Wait-Process再启动；不要按python.exe宽泛杀进程。脚本 `.tmp/portfolio_review_app.py` 预设autostart=false，仅使用验证库。浏览器IAB tab1 / CUA reviewTab当前8514，最新截图仍需重新验收。继续CUA时按工具要求重读文档。

## 测试与下一步

使用 `.tmp/advisory-venv/Scripts/python.exe`（Python 3.13），PowerShell `login=false`。默认系统 Python 3.14 不适用。离线测试设 `STOCKM1_SCENARIO_LLM_ENABLED=0`，不修改部署 `.env`。

- 本轮基线 275 passed。
- 页面嵌套 expander 问题已修复，页面/上下文专项 16 passed。
- 五角色真实 LangGraph fixture 集成、恢复、全局预算、缺证拒绝专项 5 passed。
- 在请求检查点/输入纠错最后两项修改前，全量 **285 passed in 160.92s**。
- 最新输入纠错、完整 CCTV 页覆盖、请求检查点、文档身份及团队专项 **23 passed in 9.05s**。
- 后续全量 **295 passed in 183.95s**，包含混源、报告渲染、任务合并等；再后新增全局工具参数拒绝计费回归，待最终重跑。仍需真实任务完成、不重复调用恢复核对、最终真实报告前端 QA、git diff --check、更新本记录与进度总表。

最新额度读取 usedPercent=2%，未临近上限、未使用重置额度；后续继续按用户要求关注额度。没有 spawn 编码子代理；多 Agent 指产品内部架构。

## 正式发布边界

`conditional_research_complete` 仅代表完整有证据的条件研究与逐仓建议完成，不是正式量化交易发布。原联合风险校准、多期优化、交易资格验证仍未通过，不得放宽。必须保持 publication_eligible=false、probability_status=uncalibrated、numeric_impact_status=not_estimated，并在页面公开阻断，不把条件研究伪装成旧 Quant-Finance 全路线完成。

## 恢复提示词

继续 StockM1。先读 Docs/Session-Checkpoint-20261028.md、Docs/Portfolio-Research-Runbook.md 和 git status；保留全部未提交修改，不提交推送。使用 .tmp/advisory-venv/Scripts/python.exe。当前任务 portfolio:d258cd1f0ab445a69d18cd4980634c7d 初始已完成但最后语义复核不通过，不能交付初始报告：财务增速比较窗口不一致、清仓后的现金被错误赋予股票beta。已启动原预算剩余43/28内的真实情景/持仓修订，session1448，日志 .tmp/portfolio-correction-live-1028.log，修订root后缀fb4cda634f0aae11cd56。先确认进程是否仍运行，避免并发。普通CLI同run-id恢复已能进入待修订流程，或用 .tmp/run_portfolio_correction_1028.py；不要更改冻结数据/模型/预算，不换源降级。用户已明确授权指定持仓字段发送到 https://api.deepseek.com 的 deepseek-v4-pro，并授权仅央视两域持久直连；不要重复询问或扩大范围。修订通过后人工核对问题确已解决，再用 .tmp/verify_portfolio_full_1028.py baseline→同CLI --run-id→--compare核验无重复调用；重做最新前端真实报告QA，检查最终全量（final6日志）及git diff --check，更新记录。正式量化发布仍独立阻断，不冒充全路线完成。额度将尽时保存并输出新恢复提示词。
