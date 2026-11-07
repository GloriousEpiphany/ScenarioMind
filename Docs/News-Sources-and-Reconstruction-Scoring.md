# 原始公告采集与历史投影统计评分

2026-10-30。目标是预测、模拟和情景推演，不要求真实交易资格，不接入下单。本轮实现来源绑定和统计评分工程；真实流程尚未通过，不能声称 P0–P5 或智能投顾正式发布完成。

## 免费来源的选择与实际边界

| 来源 | 用途与接入 | 本轮真实核查 |
| --- | --- | --- |
| 巨潮资讯官方公告 | 匿名 HTTPS 身份查询、完整分页目录、官方原始 PDF。按证券及显式区间采集，不按标题筛选 | 002230、2025-04-28 至 2026-10-28：8 页、219 个唯一公告；219 份 PDF 已保存，218 份可提取全文，1 份扫描件仍阻断文本覆盖 |
| 东方财富新闻搜索 | 保留现有独立新闻范围，不能用公告悄悄替换 | 新探测收到 HTTP 200，但 JSONP callback 和结果数据集错误（`passportWeb`），另有超时；响应没有被当作新闻 |
| GDELT DOC 2.0 | 可作为跨媒体报道发现入口；文章列表不是完整原文档案或无新闻证明 | 历史查询真实返回 429，未将它登记为成功来源；没有因限流放宽完整性或改用标题摘要 |
| 央视新闻联播官方页面 | 继续沿用既有全文采集及逐篇检查点，用于宏观事件 | 本轮没有重新验收新日期的央视全文；此前成功记录不代表本轮新增来源成功 |

依据：[巨潮官方入口](https://www.cninfo.com.cn/new/index)、[AKShare 官方维护的公告接口实现](https://github.com/akfamily/akshare/blob/main/akshare/stock_feature/stock_disclosure_cninfo.py)、[GDELT DOC 2.0 官方说明](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)、[GDELT 官方历史检索更新](https://blog.gdeltproject.org/doc-2-0-updates-1-5-year-searching-and-updated-mobile-interface/)。GDELT 的旧版“三个月”说明不能直接解释为所有历史查询均不可用；需结合后续官方更新与实际区间查询验证。单次返回上限也不证明新闻完整性。

巨潮是**发行人披露原文**，不是所有媒体新闻。它建立独立 `cninfo_disclosure_tone_sum_v1` 特征合同，不替代已有新闻均值、也不抹去原新闻范围的 330 个缺失交易日。公开读取无需付费令牌，不承诺永久 SLA 或原文再分发权；工件保留研究用途与来源信息。

## 下载、恢复和来源核验

代码入口 `src/stock_app/cninfo_disclosures.py`：固定 HTTPS 域名、证书校验、禁止重定向，目录核对证券/组织/日期/请求参数、总数、每页条数、hasMore 与唯一公告 ID。服务返回的 `totalpages` 不能替代可核对的总条数。所有页和文档齐备前不签发文本覆盖。

PDF 支持官方实际使用的 `.PDF` 和 `.pdf`，保留原 URL。元数据上限 4 MB，PDF 上限 32 MB，流式检查声明长度和实际字节数；该上限修正了真实 22 MB 报告被旧 16 MB 上限拒绝的问题。最多并行两个网络请求；PDF 解析和 SQLite 写入依序进行。同一任务使用独占执行租约，成功检查点不重复下载。

原始 PDF 字节、SHA-256、目录字段、页数、正文及 PyMuPDF 版本均保留。依赖固定为 `PyMuPDF==1.27.2.2`。新下载先保存原始 PDF 检查点，再提取文字；解析失败保留原文和错误，不丢弃、不用摘要代替。绑定时重新读取全部目录和 PDF 字节，复算哈希、全文与页数，不能仅靠 source ID 存在获得通过。

`disclosure_feature` 只在**完整且计数核实的公告覆盖**内求情绪分值之和；已证明没有公告的窗口为空集合，和为零。这不同于把缺失新闻、失败页面或缺少正文填零。公告只有日期精度时，按发行人时区当日结束后归入下一个收盘窗口，包含周末；这是特征归属约定，不能据此倒填历史 `known_at`。实际下载时间保持原值。

## 重建与独立统计评分

代码入口：`scenario_engine/quant/reconstruction_scoring.py`、`src/stock_app/reconstruction_scoring.py`、`src/stock_app/reconstructed_simulation.py`。

流程冻结明确的来源范围、重建协议、开发段/留出段和统计阈值，再重建投影并独立评分。评分器重新核验源数据、完整折清单、前缀 VAR 计算和各原点已公告行动，逐字段重算投影；改动投影数值或漏掉失败折均不能通过。评分使用生产联合事件/因子模型、同步创新抽样和独立观察财富路径，股利留作现金、不再投资。

开发段与最终留出分开报告；保留每个请求折的成功/失败。检查 VaR、ES、相关性、尾部、最大回撤及置信区间；完整事件模型与无事件模型、各单特征消融使用配对创新样本。最低训练 126 行、每个期限至少 50 个非重叠成熟留出窗口及原统计阈值不放宽。评分协议在重建前冻结，不能看过评分再调整阈值。

重建的条件均值及“只计已公告行动、假设没有未公告行动”不构成未来事件分支的联合概率。即使条件诊断通过，也保持 `publication_eligible=false`、`original_forecast_archive=false`、`event_branch_mixture_probabilities_validated=false`。事后按时间分段不证明研究者从未查看留出结果。

`quantitative-prerequisites-v6-reconstruction-scoring` 接入 `reconstruction` 和 `reconstruction_score_protocol` 引用，仅接受 `prediction_simulation` 范围；拒绝混入原始预测档案或发布范围。复核的 `checked_at`/工件创建时间使用实际执行时间，原冻结研究的 `input_cutoff` 单列，晚到数据不能进入旧研究。

## 使用

使用 `.tmp/advisory-venv/Scripts/python.exe -m src.stock_app.cli`：

```powershell
# 新建目录采集或恢复同一任务；成功项复用，失败项保留并重试。
& .tmp/advisory-venv/Scripts/python.exe -m src.stock_app.cli acquire-disclosure-archive `
  --db-path .tmp/quant-news-scoring-1030.db --run-id cninfo-originals-20261030 `
  --symbol 002230 --start 2025-04-28 --end 2026-10-28 --output .tmp/disclosure-cli-1030.json

# 取得完整 coverage_ref 后才运行；缺来源时输出阻断并退出 2。
& .tmp/advisory-venv/Scripts/python.exe -m src.stock_app.cli run-reconstructed-simulation `
  --db-path <db> --run-id <new-id> --inputs <explicit-inputs.json> --output <result.json>

# 对已有冻结重建单独独立评分。
& .tmp/advisory-venv/Scripts/python.exe -m src.stock_app.cli score-historical-reconstruction `
  --db-path <db> --reconstruction-ref <id> --score-protocol-ref <id> `
  --weights <weights.json> --output <scores.json>
```

工作流输入包含 `total_return_panel_ref`、`disclosure_coverages`（证券→完整覆盖工件）、`reconstruction_plan`、`statistical_limits`、`weights`、`action_sources_by_origin` 和 `max_ablation_energy_degradation`。时间戳与协议版本由流程实际冻结，不接受调用者倒填。原点行动来源必须是当时已捕获的三个完整目录，不能传今天下载的目录伪装历史输入。检查点保存成功来源/协议/重建；已冻结的失败折不会在原任务中被替换，新增有效资料应使用新任务和新的输入工件。

## 当前真实阻断

1. 公告 `1224805758`，2025-11-15，容诚会计师事务所关于向特定对象发行股票问询函回复（豁免版），是无可提取文字的扫描件。原文已保存，SHA-256 为 `4c80edda691ef6acefbe103aa3cffb323c1f3d40166beb4b4f9f80914150bcf8`。需要保留原 PDF 绑定、逐页完整性与质量核验的显式 OCR 流程，或有原件绑定的权威可读文本；当前未实现该认证，未签发覆盖。
2. 即使补齐这份全文，今日获取的公告、行情和行动目录仍不证明历史原点时已可见。现有来源不能制造历史接收时间或原始预测档案。
3. 363 个收益标签也不足以满足全部期限的 50 个非重叠留出窗口；60 日期限仅留出窗口就至少需要 3000 个后续交易日，另需训练/开发段。不能通过缩短期限、降低样本门槛或筛掉失败折冒充原范围验收。
4. 未来事件分支概率及更广公司行动语义仍未校准/覆盖。模拟范围不要求券商交易资格，该项不是本次阻断。

真实尝试保存在 `.tmp/news-scoring-live-proof-1030.json`：原始下载 219，正文通过 218，来源门禁阻断；没有构建不完整事件面板，没有真实重建折或真实评分结果，模型调用 0。统计评分的 63 个完整留出窗口来自合成来源绑定夹具，只验证工程实现。测试和最终检查点见 [本轮恢复记录](Session-Checkpoint-20261030-News-Scoring.md)。
