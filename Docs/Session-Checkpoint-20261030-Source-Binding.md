# 2026-10-30 来源绑定、预测模拟与历史投影检查点

本记录接续同日较早的 `Session-Checkpoint-20261030.md`。原有未提交代码、用户文档改动和历史失败均保留；`Docs/Quant-Finance.md` 未修改。没有提交、推送、下单或新模型调用，不能声称 P0–P5 或正式智能投顾发布完成。

## 最新用户范围

用户明确：“不需要真实交易资格，我只要你预测和模拟，情景推演功能是不要直接操作真实交易的。”因此不再索要券商名称、导出文件、权限或实际佣金作为本轮预测模拟的前置要求。模拟账户仍需绑定冻结输入和估值行情；不能把它标成真实托管或把未知费用补零。

## 实现

1. `cninfo_actions.py`：巨潮分红、配股、股本变动三个完整原始目录，计数/证券/查询范围/时间/字段核验；完整方案与数值列、送转股与股本总量交叉检查。成功空目录与请求失败严格区分。不可变成功检查点及逐次失败日志；恢复只重试失败项。完整覆盖是限定证券、日期和已实现语义的覆盖，非空配股、未知行动、混合或不匹配股本仍阻断。
2. `total_return_source_binding.py` / `quantitative_inputs.py`：原始前后收盘价、完整行动和基准日历逐行重算总收益；显式区间不足不缩短，首个价格作锚。事件财富面板从同一原始来源构建，新闻缺日不填零。清单支持多个证券及不同日期的历史行动来源。
3. `simulation_inputs.py`：研究组合与原冻结快照完全对应，现金、股数、价格来源、总值与权重独立复算。标记用户提供的模拟持仓，不认证券商托管。
4. `quantitative-prerequisites-v5-scoped-simulation`：CLI与研究任务默认 `prediction_simulation`，真实交易资格、可执行调仓优化为不适用；原 `release` 范围保留。模型来源、历史可见性、条件财富与概率校准未放宽。CLI成功与否依据所选范围；模拟统计通过也不能获得正式发布资格。
5. `scenario_engine/quant/historical_projection.py` / `src/stock_app/historical_projection.py`：独立事后重建协议，明确开发期/留出期、算法和条件。逐原点仅训练可见事件前缀，输出真实今天生成时间与历史信息截止。显式 VAR(1) 条件均值算法不是失败回退；秩不足或不稳定直接拒绝。未来行动仅用当时已捕获目录中的公告，“无未公告行动”必须标为情景假设。每折失败保留，结果不转换为原始预测档案或已校准混合概率。
6. 新CLI：`acquire-action-coverage`、`build-source-bound-total-returns`、`build-source-bound-wealth-events`、`reconstruct-historical-projections`。完整契约和参数见 [来源绑定与历史重建手册](Source-Binding-and-Reconstruction.md)。

投影生成和审计已实现；重建投影接入独立财富评分、消融、成熟样本及最终留出验收仍未完成。这是后续工程工作，不能全部归因于缺数据。与旧档案验证分离，不倒填注册时间。

## 真实来源与阻断

隔离库：`.tmp/quant-action-bindings-1030.db`。原库 `.tmp/quant-integrated-1029.db` 只读备份，未写入。原725条冻结证据保留；副本中原757个市场工件的ID、payload_hash、原JSON及known_at与原库逐项一致。

- 巨潮真实目录：18条分红、0条配股、95条股本变动。0来自成功且明确计数的完整响应，不是异常转空。
- 原请求分红/股本超时、配股成功；恢复后只补失败项。原始超时记录没有删除。
- 2025-04-28为价格锚、2026-10-28为结束，真实重算并绑定**363个收益标签**。此前80标签专项以及完整区间缺基准的失败保留。
- 完整基准首次curl传输超时；后续明确选择项目已支持的 `windows_schannel_v1`，同一HTTPS端点、证书校验保持，双指数成功。没有修改全局代理或网络配置，没有换源或降级HTTP。
- 新数据实际接收时间均为10月30日，历史标签不等于历史当时可见的训练资料，`historical_forecast_visibility_validated=false`。
- 363日中**330日缺少当前事件特征所需的原始新闻**。构建完整事件财富面板返回blocked和缺日明细，不产生短面板或零情绪。真实历史投影依赖该面板及可靠历史原始版本，仍blocked。
- 原冻结模拟持仓及行情估值绑定通过，`broker_custody_verified=false`。真实交易资格/可执行调仓优化为not_applicable；事件财富校准仍blocked。没有新模型调用，验证前后 `agent_call_attempts=0`。
- 真实CLI重开同一公司行动run-id成功退出0，返回已有覆盖证书，未重采已成功目录。

真实证明：`.tmp/action-binding-proof-1030.json`、`.tmp/prediction-scope-proof-1030.json`、`.tmp/prediction-scope-proof-1030-final.log`、`.tmp/action-coverage-cli-1030.json`。现场脚本 `.tmp/run_action_binding_1030.py`、`.tmp/extend_benchmark_binding_1030.py`、`.tmp/recheck_prediction_scope_1030.py` 保留。重新采集不能修改原10月29日冻结截止或把prepare_only原地改为模型研究。

## 工程验证

统一解释器 `.tmp/advisory-venv/Scripts/python.exe`，全量测试进程禁用真实LLM。默认Python 3.14缺依赖，不使用。

- 第一轮来源/范围专项34项通过；补充生成器、源绑定财富面板和模拟持仓专项通过。
- 历史投影完整来源集成使用明确合成的137行工件；七个开发/留出原点生成成功，删除一个原点的目录后仅该折失败并保留另外六折。未来值篡改不改变历史投影，晚到来源/不稳定/秩不足均拒绝。这是工程夹具，不是实际市场验证。
- 第一轮全量：423 passed in174.95s，`.tmp/pytest-all-source-binding-1030.log`。
- 最终全量：**428 passed in290.43s**，`.tmp/pytest-all-source-binding-1030-final.log`。
- `git diff --check`通过，日志`.tmp/diff-check-source-binding-1030.log`；仅既有LF/CRLF提示。`Docs/Quant-Finance.md`仍无diff。

## 下一步与恢复提示

继续 StockM1 Quant-Finance。先读总纲、进度、Deductive-Research-Runbook、本检查点和Source-Binding-and-Reconstruction，再核对git diff与最终428项测试日志。保留所有未提交代码/文档；使用.tmp/advisory-venv/Scripts/python.exe。用户已明确只要预测、模拟和情景推演，不要求真实交易资格，不再将券商原始资料作为该目标的前置条件，不下单。

本轮已实现三目录完整性/无行动绑定、限定行动语义的总收益重算、模拟持仓来源核验和独立历史投影生成器。隔离真实库有18/0/95条公司行动目录及363个真实收益标签；原757工件与原库一致，模型调用0。330个交易日缺新闻，可靠PIT未齐，真实财富面板和历史投影仍blocked；测试不能替代验收。下一步先解决来源缺口与历史版本，然后把重建投影接入独立财富评分、消融和最终留出协议；不能把新投影改成旧预测档案、把条件均值当完整新闻分支概率、换弱模型、补零或跳过反证。P0–P5及正式发布未完成。

本轮没有收到额度即将耗尽的明确信号；本文件为正常检查点。后续若出现明确预警，更新进度与恢复提示后停止新增预算工作。
