# 2026-10-29 联合风险、多期优化、交易资格续办记录

> 后续最新状态见 [采集修复与正式发布续办](Session-Checkpoint-20261029-Acquisition.md)。最新证据采集已完成，正式发布仍阻断；下文保留当时失败记录。

**本记录保留较早量化阶段状态；后续以 Session-Checkpoint-20261029-Acquisition.md 为准。** 当时三个正式发布阻断尚未解除，真实流程在数据准备阶段未通过。全部Git修改未提交、未推送，没有下单。

## 本轮用户要求与补充信息

用户要求优先解决联合风险校准、多期优化和交易资格，再用最新市场数据跑真实全流程；禁止降级、自动提交，额度将尽保存进度及恢复提示。

用户确认“现有持股即当前记录”：002230总持仓10股、现金50000元；要求佣金/印花税等查询最新官方数据或高质量免费API。**没有明确确认今日可卖股数、冻结/质押/未交收及账户实际佣金。** 旧库曾确认可卖5/10股，但日期已过期，不能自动当作今日可卖5股或10股。公开费率/券商收费示例也不等于账户协议，不能盲用config默认佣金代替。

用户又明确表示目前没有历史PIT档案，要求自行寻找高质量免费API；若无法满足，继续实测并保留真实数据阻断。已按此调查和实测。

此前授权继续有效：仅央视 `cctv.com`、`cntv.cn` 持久直连，其余原代理不改；指定持仓字段可送往官方DeepSeek固定deepseek-v4-pro，账户名/标识/密钥不外发。本轮**模型调用0次**；没有使用旧24/16续办额度，也没有修改旧预算。本轮不改任何代理配置。

## 新增工程（不要夸大为生产验收）

- `scenario_engine/quant/joint_calibration.py`：调用已有因子拟合逐时点回放；检查训练行真实known_at，按1/5/20/60日分开、非重叠成熟窗口。VaR精确置信区间/超限独立性、ES identification score时间块区间、联合energy score、相关性及共同尾部误差；协议/阈值与折叠来源版本化。今天下载的历史行不能倒填旧预测时点。
- **联合校准仍缺**：当前新增回放仅覆盖每日总收益再投资指数因子基线，不覆盖新闻/事件条件、现金分红不再投资财富路径及完整特征消融，scope均false。不能拿它替代原生产财富路径校准；生产模型/数据/成熟样本仍需建设。
- `portfolio_policy/multi_period.py`：Gurobi日频情景树；现金/整数股数、整手买、零股整体卖、T+1/冻结、最低佣金/税费/滑点、非预见性、现金/行业/单股/换手/ES/每日财富回撤、显式概率不确定集合顶点与防守基线。只接受最优证明；无解IIS不放宽；INF_OR_UNBD在相同模型和同一总时限内诊断，不换求解器。
- `proposal_validator.py`：独立逐节点重算现金、股数、费用、约束与风险，核对求解器字段；结果标为validated_on_supplied_tree，不声称样本外验证完成。
- **多期优化仍缺**：独立真实成熟路径recourse复测、反向压力状态搜索、概率不确定集合实证校准、券商逐笔分币结算及分红税/到账时点。非整数公司行动/现金代股未支持时拒绝。当前Gurobi为非生产许可。
- `execution_constraints.py`：带生效期和来源的规则、可卖/冻结/未交收、账户权限、停牌/价格界限/价位/流动性/真实账户费用契约，未知/过期不得冒充可交易。
- `src/stock_app/quantitative_validation.py`及CLI `validate-quantitative-release`：实际调用可用检查，缺输入/依赖失败追加写入quantitative_validation_runs；绑定研究结果哈希与账户快照。此入口目前是前置验收链，尚不能签发完整发布资格。
- 条件研究完成后由jobs自动执行一次量化前置检查；重新打开不重复模型调用或重复创建相同报告检查。前端新增简明三阶段状态，拒绝将旧报告检查套到新修订。
- `PortfolioResearch.execute(..., prepare_only=True)`：显式证据准备契约，不调用模型、不冒充研究完成；purpose进入冻结契约，不能同run-id改变用途。
- `src/stock_app/corporate_actions.py`：免费东财原始命名字段+完整方案解析，核对证券和per-10股单位；实际接收时间入库，不补未知数字。缺支付/股份到账日和配股覆盖时明确不能用于执行现金安排。

安装仅改项目隔离环境：官方PyPI的 `gurobipy==13.0.0`，新增requirements-quantitative.txt。默认镜像先TLS失败，官方PyPI同版本安装成功，保留两份日志，没有关闭证书校验。实际部署探针 `.tmp/gurobi-deployment-proof-1029.json` 验证整数模型最优/gap0；日志明示“Restricted license - for non-production use only - expires 2027-11-29”。不要购买/申请新许可。

## 最新市场实测：失败位置与已保存事实

新隔离库 `.tmp/quant-latest-1029.db`，仅从生产只读复制账户2、positions及stock_info，初始无市场工件。没有复制上轮行情/新闻来冒充新采集。

- 任务 `quant-latest-20261029`，root `portfolio-team:quant-latest-20261029`，是prepare_only；原请求 `.tmp/quant-latest-request-1029.json` 冻结2025-04-26..2026-10-28日线、10月28日新闻联播期次、原东财/curl_cffi_tls_v1来源。现在是10月29日盘中，选最新已收盘10月28日。
- 脚本 `.tmp/run_quant_latest_1029.py`，日志 `.tmp/quant-latest-live-1029.log`，进程session73464已结束（应再次确认句柄，勿并发重跑）。结果 `.tmp/quant-latest-evidence-1029.json` 为data_incomplete。
- 实际14次采集尝试：price失败3次；financials失败后成功（78条）；industry失败3次；news失败2次后成功（100原文/100派生）；money_flow失败3次。行情/资金为curl28超时，行业缺失。全队调用账本为空：没有模型或Agent工具调用。
- 因六域不完整，没有进入本次CCTV/双指数上下文、组合估值快照或模型研究。不得声称本轮已完成最新新闻联播/完整市场报告。
- 独立免费公司行动探测成功18条，规范化后入库；当前库共296条工件：78财务+100事件+100文本特征+18分红。没有价格/行业/资金流，不填零、不使用旧估值。
- 三个客户端对同一行情URL的只读对照均失败：curl_cffi Timeout28，requests ReadTimeout，Windows Schannel curl28。证明 `.tmp/price-transport-diagnostic-1029.json`。没有改变当前契约、切换代理、取消证书校验或将对照数据混入任务。
- 量化失败报告 `.tmp/quant-latest-validation-1029.json`，追加账本后明确三个阶段均blocked/data_preparation_incomplete。旧10月28日条件快照另做前置检查 `.tmp/quant-prerequisites-old-snapshot-1029.json`，真实缺少joint panel/account execution state/multiperiod problem；不是新的当前市场成功报告。旧最终报告哈希仍1fc1ea9a2fc49b0d3983fd088784c56acbc9fcda0409a8963a2610d05aa3ac68。

源请求失败会从同run-id恢复已成功组件；不要为绕过失败改旧日期、来源或预算。进入另一最新收盘日必须新任务，不修改原时间。

## 免费接口调查

调查与官方来源链接见 `Docs/Quantitative-Validation-Runbook.md`。真实 `.tmp/free-api-probe-1029.json`：东财分红18条成功，新浪SSLError，巨潮40秒有界进程超时。原始东财命名字段/证券验证响应 `.tmp/eastmoney-actions-raw-1029.json`，真实接收2026-10-29T02:45:39Z；不能改成2009..2026原公告时点known_at。支付/股份到账日不在响应，不能当成已知。

BaoStock官方有分红/复权/日线，但不因此保证历史修订档案。QLib官方PIT说明支持修订链，实际免费采集器用BaoStock，并说明发布日期缺失时45/90天偏移；本项目不采用该补日期做法。TuShare官方权限表对财务/分红等列积分要求，未冒充当前凭据已有权限、未自动充值。结论是**本轮尚未找到可直接满足全部正式门槛的免费档案**，不是宣称免费数据源全部不可用。

## 验证、环境与当前状态

- 解释器 `.tmp/advisory-venv/Scripts/python.exe`，PowerShell login=false；测试STOCKM1_SCENARIO_LLM_ENABLED=0，不改.env。
- 工程第一轮314 passed；新增边界后暴露Gurobi INF_OR_UNBD诊断问题，修复后320 passed；新增UI及自动前置检查测试后最终全量 **322 passed in142.39s**，`.tmp/pytest-all-quant-final3-1029.log`，session22751已结束。真实阻断、免费源实测、部署探针汇总证明 `.tmp/quantitative-progress-proof-1029.json`。
- 前端带量化状态记录的AppTest专项3 passed in21.56s（添加自动触发用例前），旧数据/旧哈希不会被当成新验证结果。
- 最终 `git diff --check`通过；无Git提交。Codex额度上次3%，不临近上限，未使用重置额度。保存本记录与恢复提示用于延续工作，不代表任务已完成。
- 没有编码子代理；产品内部multi-agent架构保持原样，Gurobi/并行只读网络探测进程不代表模型子代理。

## 下一步（仍须完成，不得虚报）

1. 最终322项全量已通过。恢复时确认没有运行中的采集/模型进程，保留所有旧失败记录和未提交改动；不要把成功的单元测试当作真实市场有效性。
2. 继续寻找/接入能满足真实known_at、修订与公司行动/总收益口径的来源；网络恢复后按同契约恢复本次未成功采集。需要新来源时先明确新SourcePlan与质量/单位/时间语义，不能在旧任务异常分支自动切换。
3. 联合风险必须补事件条件、相同生产现金/股数财富算法及样本外/前瞻检验，当前基线scope不可升级为正式校准。
4. 多期决策需独立成熟验证路径、反向压力、概率不确定集合、精确结算/分红税等；当前非生产许可也不能冒充已获生产部署资格。
5. 交易资格需今日券商可卖/冻结/未交收/权限和账户实际费用（含是否含规费），公共费用仅能覆盖公共部分。不能因用户确认总持仓而推定全可卖或使用默认佣金。
6. 三个门槛实际通过后，再用最新完整市场输入运行真实模型和最终发布链。当前不应在缺数据时用条件模型输出顶替量化建议。

## 恢复提示词

继续StockM1，先读Docs/Session-Checkpoint-20261029-Quantitative.md、Docs/Quantitative-Validation-Runbook.md和git status，保留全部未提交修改，不提交推送、不下单。用户要求先解决联合风险校准、多期优化、交易资格再跑最新全流程；用户无PIT档案，授权自行找高质量免费API，若不足可保留真实数据阻断。已新增joint_calibration、Gurobi多期树、独立复算、execution_constraints、quantitative_validation CLI/自动前置检查和分红规范化，但三个正式门槛尚未通过；不要把总收益再投资基线当现金分红不再投资或事件条件联合校准。新真实库.tmp/quant-latest-1029.db，任务quant-latest-20261029，prepare_only，新数据目标10月28日收盘，price/industry/flow三次均失败，financial78/news100+文本100成功，独立东财分红18成功共296条；没有新行情/组合快照/CCTV/模型结果，模型与Agent工具调用为0。三客户端行情对照均超时，不改旧来源、代理或TLS。最新失败报告.tmp/quant-latest-validation-1029.json，汇总证明.tmp/quantitative-progress-proof-1029.json。旧最终条件报告hash1fc1ea9a...仍保留，不拿它代替当前市场。本机gurobi13.0.0非生产许可，真实探针最优gap0；最终全量322passed日志.tmp/pytest-all-quant-final3-1029.log，diff检查通过。继续数据与算法建设，尤其同生产财富算法事件条件联合回放、独立成熟路径recourse及反向压力/精确结算；当前券商可卖/冻结/未交收/实际佣金未知，旧5/10过期，不能自动推10股全可卖。额度接近上限时保存进度和新恢复提示。
