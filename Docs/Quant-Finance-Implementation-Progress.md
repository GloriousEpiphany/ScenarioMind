# Quant-Finance 重构进度与恢复记录

更新时间：2026-09-26。路线依据：`Docs/Quant-Finance.md` 第 13 节。

工作区改动尚未提交。原有 `Docs/Quant-Finance.md` 修改来自用户，本轮未覆盖。
没有执行真实订单，没有把合成测试结果当作市场有效性证明。本轮未主动启动付费模型研究；首次全量测试可能受本机旧引擎模型配置影响，已中止，不能据此断言外部调用次数为零。

## 已落盘的实现

### P0 数值、递归与发布边界

- `events/state_transition.py`：按完整父路径合并，支持 set/add/multiply；检查单位、重复键、缺失父值和非有限值。尚未实现 decay，遇到它明确拒绝。
- `events/branch_planner.py`：去重包含父路径、观察窗口、触发/失效条件和概率属性。当前深度预算仍沿用 V3，未宣称完成自适应路径搜索。
- `portfolio_policy/optimizer.py`：先将对数收益转换为简单收益；交易成本进入现金约束和输出现金；不交易方案也检查已有约束。
- `quant/risk.py`：加权 ES 使用尾部边界的精确部分概率质量。
- `quant/joint_paths.py`：严格对齐的联合路径契约，以及按财富路径计算的组合 ES/最大回撤。只评估传入路径，不是已训练、校准的路径生成器。
- 边际 q10 拼接结果明确标记 `marginal_q10_stress_proxy`、`research_only`、`publication_eligible=False`。保存研究运行包不等于通过投资建议发布。
- `scenario_calibration.py` 与 `quant/replay.py`：时间留出逐折调用生产 Adapter 和融合算法；检查标签成熟、训练可见性、验证事件原始证据；冻结训练工件 ID，防止上线悄悄加入留出数据。
- 历史导入记录 `label_available_at`；今天导入的资料不能回填过去的系统可见时间。旧经验分位数校准不能激活为新版生产事件模型。
- 下尾超限率明确是 VaR 覆盖，不代表 ES 严重程度或联合风险校准。
- 已选 TuShare 失败不再自动换腾讯。修复当天收盘后请求截止时间超出 known_at 的问题。Adapter 失败记录为 FAILED。

### P1 候选池主链接入

- 新增 `scenario_engine/universe/`：完整 PIT 证券主数据 + 公司论点 + 独立论点验证工件构建候选池，记录排除原因，保留所有已有持仓。
- 候选池绑定具体账户快照工件，而非仅绑定长期不变的账户 ID；过期、未来信息、账户变化和证据缺失均阻断。
- `ScenarioMission.universe_ref` 显式进入严格 Facade、预检、控制器、运行报告与任务队列恢复。候选股零初始权重参与相同的数据/模型/交易约束检查。
- 模型声明的校准证券集合必须覆盖候选池；不把持仓校准直接套给新股。
- CLI 新增 `build-investment-universe` 和 `research-portfolio`。
- 尚未完成 BaoStockAdapter、全市场自动采集、候选论点自动生成、PostgreSQL/对象存储迁移。

### P2 已实现的自治控制面

- 新增 `agents/runtime.py`：实际 LangGraph 状态图，固定模型工具调用、动态选工具、独立证据上下文反证、反证后重规划。
- `research_board.py` 保存不可变决策和工具结果；SQLite checkpointer 保存图状态；工具成功结果缓存支持提交边界恢复。
- 调用预算在外部调用前持久化预留，失败调用也计入预算。预算耗尽返回未完成，不跳过反证。
- 独立数据库租约/心跳阻止同任务并发执行；恢复不允许修改模型、工具、预算或输入。
- `pit_tools.py` 提供真实 PIT 工件读取，证券与数据集范围由服务端限定；缺数据/上下文容量不足明确报错，不填零、不截断冒充完整。
- `ToolCallingModel` 要求响应模型与固定模型一致；拒绝未知工具、额外参数、不完整调用与模型切换。
- CLI `advisory-research` 可显式启动模型研究并恢复，需安装自治依赖和配置 SCENARIO_LLM_*。本轮没有执行真实付费模型调用。
- 当前自治运行仅开放只读/纯计算工具，不开放外部写操作；正式调仓发布仍被阻断。专业角色子图、联合模型、交易验证器及发布 outbox 尚未完成。

### P3 联合研究模型（尚未完成生产校准）

- `quant/joint_fit.py`：在同一组可见、连续交易日样本上拟合带截距的因子 AR(1) 和资产暴露，保留同步因子扰动与资产残差。缺失值、不可见标签、秩亏和不稳定转移均阻断，不使用插值或较弱估计器兜底。
- `quant/joint_model.py`：共同抽取历史连续块，所有资产共享抽样行；输入显式声明资产/因子顺序、事件冲击、分红及拆股。价格、股数和现金共同计算累计简单总收益，分红不再投资。
- 冻结模型版本与数据内容绑定；拟合结果的模拟入口核对参数，拒绝版本或参数替换。输入哈希记录随机种子、算法及 NumPy 版本，保留抽样行以供复现。
- 输出仅为 `stress_only`；`evaluate_stress` 提供最差路径损失/回撤，不能把等权样本当作已校准概率计算组合 ES。正式模型失败时不会自动调用此研究入口。
- 尚未接入 CLI、PIT 工件加载与自治工具；事件簇、状态切换、完整样本外联合尾部校准和多期优化仍待实现。

## 环境与测试

当前默认 `python` 是 `C:/Python314/python.exe`，不含项目数值依赖。
项目已验证的基础解释器是 `C:/Users/29643/Anaconda3/python.exe`（Python 3.13.5）。

自治集成环境：`.tmp/advisory-venv/Scripts/python.exe`，基于 Anaconda 创建并继承原项目依赖；新安装只写入隔离环境。
早先 `.venv-v4/` 基于 Python 3.14，仅安装了自治依赖，不能用于完整项目回归。两个目录均不纳入 Git。

已验证记录：

- P0 完整回归：200 passed。
- P1/数值/账户相关回归：56 passed；随后 P0/P1 专项：22 passed。
- 扩展后全量回归：217 passed（48.48 秒），包括 11 项真实 LangGraph 集成测试。
- CLI `--help` 已验证三个新命令可加载；未触发真实模型或数据端点调用。

完整验证命令：

```powershell
$env:STOCKM1_SCENARIO_LLM_ENABLED='0'
.\.tmp\advisory-venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=.tmp/pytest-all-advisory
```

全量回归后补充工具审计标识、禁用默认外部 tracing 和端点哈希记录；自治/候选池专项 17 passed。
2026-09-26 新增联合拟合/模拟及 V4 数值边界专项：33 passed。
本轮首次全量运行出现失败及旧引擎长时间等待，已中止；正在以测试进程环境变量
`STOCKM1_SCENARIO_LLM_ENABLED=0` 重跑，不修改生产配置：**234 passed in 75.68s**。
专项与最终全量均覆盖最新资产/因子顺序校验；`git diff --check` 通过（仅有已有 LF/CRLF 提示）。

LangGraph 持久化接口依据官方说明：
https://docs.langchain.com/oss/python/langgraph/persistence
框架检查点不替代业务工件幂等与事务。依赖版本记录于 `requirements-advisory.txt`。

## 后续顺序

1. 完成当前自治运行与全量回归，检查 CLI 和新工具的失败/恢复边界。
2. 深化 P1 数据采集及 P2 专业角色和真实内核工具绑定；不靠更弱模型或固定模板兜底。
3. P3 将已实现的同步块联合研究模型绑定 PIT 工件与工具，再实现事件簇、完整时间状态转移及联合尾部校准。
4. P4 多期非预见性、Gurobi 部署验证、现金股数与公司行动、独立复测/反向压力。
5. P5 产品接入与冻结的样本外/前瞻验证。真实前瞻观察不能用代码或合成数据代替。

## 恢复提示词

继续 StockM1 的 Quant-Finance 重构。先读取 Docs/Quant-Finance.md 与
Docs/Quant-Finance-Implementation-Progress.md，再检查 git diff 和最新测试结果。
保留用户原有文档改动以及所有未提交代码。工作已覆盖 P0、P1 候选池主链和 P2
LangGraph 自治运行基础，以及 P3 同步扰动联合研究模型与冻结拟合；不能声称 P0–P5 或智能投顾正式发布完成。
使用 .tmp/advisory-venv/Scripts/python.exe 做集成测试；默认 Python 3.14 缺项目依赖。
严禁核心功能静默降级、缺失值填零、模型自动变弱、未校准概率混入联合分布、
预算耗尽后跳过反证或发布建议。先处理当前测试/审查问题，再按路线继续。
若遇到额度即将耗尽的明确信号，更新本进度文件并输出新的恢复提示词。
