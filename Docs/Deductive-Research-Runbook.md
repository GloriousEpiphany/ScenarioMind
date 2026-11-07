# 演绎研究运行与故障恢复

更新：2026-10-17。以下流程产出证据及条件传导假设。联合风险校准、多期优化和正式建议发布仍由独立闸门控制。

2026-10-30 最新续办：用户已明确只需要预测模拟，不要求券商真实交易资格。新增官方公告原文、来源绑定及历史重建统计评分，验证器为 v6。219 份原始 PDF 中 1 份扫描件缺可靠全文，真实流程在来源阶段阻断；历史可见性、成熟样本和概率仍独立受检。详见 [原始公告与评分手册](News-Sources-and-Reconstruction-Scoring.md) 与 [最新检查点](Session-Checkpoint-20261030-News-Scoring.md)。下段 v4 与后续历史验收按各自时间解释，不代表当前全部能力状态。

2026-10-30 续办说明：下述10月17日真实演绎研究验收仍有效；10月16日故障和网络恢复段落是历史记录，不能据此推断当前采集仍阻断或撤销后续已授权网络配置。最新10月29日准备任务已冻结725条证据但没有调用模型，10月30日仅在副本中完成离线恢复复核。v4量化验证仍blocked，不能将准备成功或历史研究成功当作正式发布。最新审查修复、测试及下一步见 [10月30日检查点](Session-Checkpoint-20261030.md)。

## 2026-10-17 真实全流程验收

新任务 live-full-eastmoney-20261017、独立数据库 .tmp/deductive-full-live-1017.db 已达到 research_complete，CLI 退出码 0。此次没有使用之前保存的采集工件，没有修改系统代理、路由或证书设置，也没有失败后换源或换模型。

明确来源为 eastmoney_http、TLS 配置 curl_cffi_tls_v1；行情区间 2025-01-01 至 2026-10-15。新采集 835 条工件：430 条日线、104 期财报、1 条行业记录、100 条新闻、100 条文本特征、100 条资金流。日线逐段核对沪深基准日期，缺日检查保持开启；另对沪市 600519 的 6 条日线完成同样的独立验证。

按研究时间窗口冻结 777 条工件（430 行情、46 财报、1 行业及后三域各 100），23 次固定模型调用、15 次工具执行。四轮独立审查中前三轮明确拒绝，分别发现比较口径错误、个股/板块归因错误与遗漏反证；最新修订通过第四轮审查，输出 8 条条件假设。旧版候选及拒绝理由仍在审计账本中，未更改审查标准。

相同命令重开后，5 个采集任务、模型/工具调用计数、835 条原始工件和完整结果哈希均保持不变。证明文件 .tmp/deductive-full-proof-1017.json；原始执行日志 .tmp/deductive-full-live-1017.log，恢复日志 .tmp/deductive-full-replay-1017.log。研究结果哈希 76a7c9efe4eb25d533e812452bbfb69d65dd48551579d8bc032b7c367abdf373。

最新全量回归 275 passed in 55.75s；来源加载顺序的旧行为负对照确实失败，修正后两项来源回归通过。Git 未提交。结果 publication_eligible=false、probability_status=uncalibrated、numeric_impact_status=not_estimated：工程研究流程通过不代表联合风险校准或正式投资建议获准。

历史 TLS 问题尚不能归因到单一因素：当日有/无 ut 的同条件对照均返回 243 个基准交易日。官方 ut 参数继续保留，但不能声称它已被证实是此前 TLS 故障的唯一根因；后续连接异常仍按严格失败处理。

## 安装与配置

本工作区已验证的隔离解释器为 `.tmp/advisory-venv/Scripts/python.exe`。新环境安装：

```powershell
python -m pip install -r requirements-advisory.txt -r requirements-scenario-public.txt
```

集中配置 `SCENARIO_LLM_MODEL`、`SCENARIO_LLM_API_BASE`、`SCENARIO_LLM_API_KEY`。
已有应用凭证库和 `.env` 由同一加载器读取；密钥不得提交到 Git。
行情缺省明确选择 eastmoney_http 未复权日线；先加载配置，再读取 STOCKM1_PRICE_SOURCE，命令行 --price-source 优先。BaoStock、TuShare 和腾讯也可显式选择，来源失败不会自动切换。已有配置不被覆盖；恢复原 BaoStock 任务时需保持原来源，必要时加 --price-source baostock，否则冻结契约会拒绝恢复。API 文档、单位、停牌和完整性核对见 [免费源接入记录](Free-Data-API-Integration.md)。

先验证实际工具协议：

```powershell
.\.tmp\advisory-venv\Scripts\python.exe -m src.stock_app.cli scenario-doctor --live-advisory
```

`--live-advisory` 使用固定模型执行两轮真实工具调用，检查后续请求所需的提供方状态。
DeepSeek 保留思考模式；其该模式不支持 `tool_choice=required`，因此使用 `auto` 并严格检查返回的单一工具调用。
拒绝模型替换、自然语言代替工具、截断调用和未知工具。传输参数进入任务契约，恢复不得偷偷修改。
提供方续传状态仅在内部运行账本和图检查点中保留，不进入独立反证输入或最终公开结果。

协议依据：[DeepSeek 思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)、
[工具调用](https://api-docs.deepseek.com/guides/tool_calls/)、
[TuShare HTTP 数据契约](https://tushare.pro/document/2?doc_id=40)。
TuShare 传输固定为 `https://api.tushare.pro`，禁用凭证重定向；网络异常优先按网络类型分类。

## 一次研究

```powershell
.\.tmp\advisory-venv\Scripts\python.exe -m src.stock_app.cli deductive-research `
  --run-id research-20261016-000001 `
  --objective "核查公司公开证据，提出事件传导机制及其反证条件" `
  --symbol 000001 `
  --price-source eastmoney_http `
  --start-date 2025-01-01 --end-date 2026-10-15 `
  --model-policy-version YOUR_FROZEN_POLICY_VERSION `
  --model-evaluation-ref YOUR_EVALUATION_REFERENCE
```

该命令会联网并产生模型 API 用量。`--db-path` 可指定独立数据库，默认使用应用数据库。
结束日期必须已经收盘。策略版本和评价引用来自部署配置，程序不会生成虚假的评价证明。

执行顺序：

1. 逐证券采集行情；全局来源权限失败只探测一次。
2. 逐证券、逐组件获取财务、行业、新闻和资金数据；新闻产生明确版本化的文本特征。
3. 成功任务写入不可变业务检查点，失败、开始和结束记录进入 `acquisition_attempts`。
4. 全部必需组件成功后，以采集完成时点冻结六类原始工件 ID 和哈希。
5. 固定模型分页读取冻结证据，提出结构化传导假设，独立反证后决定研究是否完成。

必需域为 `price/equity_price`、`fundamental/pit_financials`、`industry/industry_membership`、
`event/observed_event_source`、`sentiment/event_text_features`、`flow/money_flow`。
每只证券均需覆盖。分页显式返回总量和下一页位置，不截断冒充完整历史。
新闻搜索最多为首屏 100 条的摘要集合，不是全量公告归档或原文全文；该范围写入证据载荷。
资金流为单证券最新 100 个日记录的订单规模估算，保留 main_net_inflow_cny 等分项，不把主力净流入冒充全口径净流入。行情/资金流客户端由 STOCKM1_PUBLIC_HTTP_PROFILE 明确指定，默认 curl_cffi_tls_v1；可显式选择 windows_schannel_v1 或 requests_tls_v1。证书验证开启、禁止重定向，失败不会更换客户端；传输配置进入冻结来源契约。

每条假设必须引用冻结证据，记录父路径、机制、作用证券、触发和反证条件。
输出明确为未验证假设，概率未校准、数值影响未估计。模型文字不能自动变成事实、收益或交易指令。
独立审查必须返回 blocking_findings；即使 passed=true，有阻断项也会退回修订。每次审查绑定最新假设输入哈希；对外只返回最新版本，旧版保留在审计账本。

## 恢复与失败

使用完全相同命令与 `run-id` 恢复。成功组件跳过，失败组件重试；模型阶段复用同一冻结输入和已完成工具结果。
修改目标、证券、区间、来源、模型、工具或预算需要新任务 ID。既有旧版研究任务不会被静默迁移到新协议。
预算耗尽必须报告未完成，不能跳过反证。
可用 --max-model-calls（默认 64）、--max-tool-calls（默认 48）、--critic-reserved-calls（默认 12）明确预算；长区间分页、多证券及审查后修订都消耗预算。预算进入冻结契约，恢复时不能自动扩容。

公共 SDK 缺少请求超时的组件在独立进程执行，单组件期限为 180 秒；超时终止 worker 并报告数据不可用。
提供方错误不会转成零、空证据或较弱数据源。采集失败时退出码非零，返回 `data_incomplete`，不启动正式研究完成状态。

## 2026-10-16 历史验证边界

- 固定配置模型 `deepseek-v4-pro`：两轮工具调用及续传实际通过，仅证明协议连通性。
- 官方 TuShare `daily`：实际返回当前 token 权限不足。
- 公共财务、行业：证券 `000001` 实际采集成功。
- BaoStock：沪深两股短区间 12 条实际入库；后续完整任务在登录阶段连接 EOF，180 秒期限后明确失败。
- 东方财富新闻：更换为上游当前使用的 curl_cffi 并严格核对回调后，100 条真实采集及其文本特征通过。
- 资金流：明确改为单证券 100 日订单规模估算，实际采集曾通过；后续 Schannel 也遇 TLS 解密失败，不能声称稳定解决。
- 东方财富日线：显式 --price-source eastmoney_http，430 条日线探测通过；沪深基准完整性检查的实际请求仍被 TLS 问题阻断，未跳过该检查。
- 财报 schema v2：真实采集 104 期报告，各期均保持可见，不伪造历史发布时点。
- 全新下载任务 `.tmp/deductive-free-live-1016.db` / `live-free-source-20261016` 和 `.tmp/deductive-eastmoney-live-1016.db` / `live-eastmoney-source-20261016` 均如实 data_incomplete；新代码的契约及 schema 已变化，再次全新验证须用新 run-id。
- 另用保留真实采集工件验证模型阶段：`.tmp/deductive-real-evidence-stage1016.db`，最终任务 `preserved-real-evidence-20261016-v5`；308 条冻结证据，15 次模型调用、13 次工具执行，独立审查无阻断项，状态 research_complete。重开后模型/工具次数仍为 15/13，没有重复 API 调用。上下文明示 fresh_acquisition_status=blocked_network_not_complete，不能替代全新下载验收。结果 `.tmp/real-evidence-model-result.json`。
- v4 曾出现审查指出事实过度推断却仍 passed=true，故升级为 blocking_findings 确定性闸门后重新建立 v5，未将 v4 作为最终验收。

因此本次不能声称真实六域数据与推理全链路已经成功完成。
全量离线回归最新 273 passed（54.62 秒），git diff --check 通过；合成证据集成测试只证明工程契约。
尚未接通 Streamlit 一键自治任务、完整 PIT 全市场采集、联合模型工具及多期交易验证。

## 网络对照所需条件

用户明确批准临时调整并恢复后，已通过本机命名管道控制接口做两次对照。临时模式为 rule，两条财经直连规则，其余 MATCH,GLOBAL 保持原全局选择；实际命中次数确认 DIRECT 生效。第一次用 BaoStock，第二次明确选择 eastmoney_http 与 curl_cffi。BaoStock 登录 EOF、部分行情/资金 TLS 错误在 DIRECT 下仍存在；不能简单认定改代理即已修复。DIRECT 下独立 curl_cffi 资金探测曾返回 100 条。
两次 finally 均核验恢复原 global 模式、522 条规则、Selector 选择及原配置文件哈希；没有修改磁盘配置文件。证明文件为 `.tmp/mihomo-direct-network-proof.json` 和 `.tmp/mihomo-direct-eastmoney-proof.json`，均 restored=true。原全局网络环境现已恢复。
最新完整下载任务 `.tmp/deductive-direct-eastmoney-live-1016.db` / `live-direct-eastmoney-20261016` 仍为 data_incomplete。行情错误中的数值 SDK code 引起 TypeError 已修复并加回归；最后补齐官方日线 ut 参数，尚未重新联网验证。不得关闭证书校验、删除资金域、跳过基准完整性检查或把失败任务改成成功。
