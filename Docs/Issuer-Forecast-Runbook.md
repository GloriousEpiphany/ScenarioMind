# 发行人公告预测与条件收益推演

## 可重复判定的利润预告语义标签

`build-issuer-profit-labels` 将完整公告覆盖中的预告与正式定期报告按证券、目标报告期和归母净利润口径绑定，判定实绩低于、落入或高于原预告闭区间。输入是原件覆盖引用，不接受公告类别或情感分数替代语义事实。

```powershell
& .tmp/advisory-venv/Scripts/python.exe -m src.stock_app.cli build-issuer-profit-labels `
  --db-path <独立研究库> --run-id <新任务ID> `
  --coverage-ref <完整原件覆盖> --coverage-ref <另一不重叠覆盖> --output <新输出文件>
```

v6 支持明确万元区间（保留原文小数位，不强制补成两位），或有同一期间上年归母净利润基数和明确增减百分比的推导；半年度基数不得用年度数据替代。只有“增减小于30%”等不完整措辞不能推断闭区间。兼容早期“归属于母公司所有者”口径及原 PDF 排版造成的标签跨行、数值单元格插入，同时保留单位和当前累计值列核验。正负号、零百分比、元/万元、原文精确偏移和真实接收时间均保留。

早期摘要若只写“净利润”，不得推断为归母口径。v6 另核对年初到报告期末利润表的明确年份、1–9 月累计期间、人民币元单位及“本期／上年同期、合并／母公司”列顺序，再取明确的归属于母公司所有者净利润；证据保留整段表头和原行。

一个目标期只算一个结果簇：全文、正文、摘要及后续修订均保存为版本。默认选择最早明确且无冲突的区间、后续最早无歧义的正式报告实绩；同一时点的矛盾区间、不同实绩版本冲突、缺同期间实绩、预告与实绩同时或更晚公开均阻断。业绩快报不能替代正式报告。季度、半年和年度累计结果可能重叠，报告期簇也不能直接当作统计独立样本。

对应收益单独绑定实绩目录日之后首个交易日收盘起的真实原价/股本行动路径，解释为保守原点之后的价格漂移，不是公告当时的全部反应，也不是该事件的因果效应。只有末端真实价格可核对时才允许跨停牌终值；五个旧报价争议继续隔离。分支样本不足、缺独立历史版本或原始预测时，事件分支收益估计、参数区间和独立评分保持阻断。最新 v6 标签及路径见 `.tmp/issuer-sixth-profit-final-v6.json`、`.tmp/issuer-sixth-semantic-return-readiness-final.json`。v4/v5 及失败输出均保留。

2008-08-12 网下配售上市采用专用原件验证器：数量、上市日、限售减少、流通增加、网下配售减少及总股数前后均须精确对账，并与相邻股本目录总数一致。`cninfo-action-coverage-placement-v5` 保存原件覆盖和文档引用；没有这些证据，015009 仍阻断，不能仅加白名单。

外部请求失败后可按本轮用户授权单独选择直连，保留证书验证、原始响应、实际接收时间及失败输出，不修改系统代理或历史 `known_at`。直连成功本身不是历史版本证明。

## 保留停牌缺口的独立区间模型

`run-issuer-episode-research` 是单独的数值研究入口，不替换旧连续面板估计器。输入包括已来源绑定的非重叠 `panel_refs`、连续完整正文 `coverage_ref`、原始腾讯未复权响应 `peer_ref`、原切分日历 `split_calendar_ref` 和固定 `split_indices`（200/221/363）。先将原索引解析成日期，再映射到扩展日历；禁止把旧索引直接用于新增 2011 年后的日历。

入口冻结真实接收截止和业务契约，仅对相邻真实交易日、两端均无报价争议的行构造滞后对，要求至少 126 个有效对。5 日残差抽样块不能跨停牌或隔离缺口。七类公告用原件标题与正文的精确片段绑定，按目录日期日末保守归属，每日同类只记一次。零特征须由完整计数覆盖支持。稀有类别秩不足时停止，不自动删特征或降低门槛。

```powershell
& .tmp/advisory-venv/Scripts/python.exe -m src.stock_app.cli run-issuer-episode-research `
  --db-path <独立研究库> --run-id <新任务ID> --inputs <来源引用JSON> --output <新输出文件>
& .tmp/advisory-venv/Scripts/python.exe -m src.stock_app.cli audit-issuer-episode-research `
  --db-path <同一研究库> --report-ref <上一步report_ref> --output <新审计文件>
```

输出是原发展期截止日拟合的多变量公告类别关联：无类别冲击基准，以及首步一个指定类别、之后全部类别为零的七种条件，各 512 条同步路径。归一化持仓明确以无新增公司行动、单位拆分、零现金分派为假设，不能解释为已知没有未来公司行动。1/5/20/60 日分位数只描述该条件与点估计下的创新抽样，不包含已验证的参数不确定性，不是实际市场新原点预测。

独立审计重新核验来源和冻结拟合，使用矩阵幂展开检查基准路径及每种类别冲击；另做 1,000 次区间内成块重采样。每个秩不足或不稳定的失败副本都保留；存在失败时不从成功子集签发置信区间。数值核验、历史版本、语义分支真实性、混合概率、参数不确定集合和独立市场校准分别报告，不能互相替代。所有固定留出窗口保留原点、终点、迟到来源和缺价位置；没有原预测或历史可见版本时可评分数保持零。

大型路径写入采用 WAL 保持读取端看到已提交快照，原子提交结果与业务检查点；重复业务键恢复相同输出，变更输入必须使用新任务。新入口不调用模型，不扩旧预算。2026-11-08 实证与仍缺项目见 `.tmp/issuer-fourth-episode-first.json`、`.tmp/issuer-fourth-episode-audit-v2.json`、`.tmp/issuer-fourth-semantic-mapping.json`。

## 审查后假设的事件与持仓映射

`run-issuer-event-bridge` 接收 `review_ref`、完整正文 `coverage_ref`、非重叠来源绑定总收益 `panel_refs` 和保留全部假设的 `cases`。每条假设必须精确对应独立审查通过的原报告；新增观察以原件唯一精确文本片段、偏移和哈希绑定。一般语义条件可能同时发生，不构成已校准、互斥完备的概率分支。目前仅实现明确的 2012 半年归母净利润区间解析器；其他机制保留部分观察和缺证，不能用“未观察”替代“未发生”。

```powershell
& .tmp/advisory-venv/Scripts/python.exe -m src.stock_app.cli run-issuer-event-bridge `
  --db-path <独立研究库> --run-id <新任务ID> `
  --inputs <引用与分支定义JSON> --output <不存在的输出文件>
```

入口不调用模型，不使用用户账户。历史标签起点固定为最新引用公告目录日之后的首个交易日收盘，使用 100 股研究单位、真实未复权报价和完整公司行动，逐日重放股数及税前股利应收。收益/行动反推价格须与独立原始价相等，现金财富另用独立原价公式检查，股利留存不等于再投资总收益。报告同时列出沪深基准。

v3 允许原点/终点均有真实报价、逐日行动均可核对的跨停牌窗口单独验收终值。停牌价格、当日财富保持空值，不假装成交、不填参考价、不签发完整回撤或再投资指数。终点仍停牌则继续阻断。各假设的重叠窗口不是独立样本，所有结果均为事后标签；分支收益预测、概率校准、历史 PIT、未来模拟持仓及样本外评分另行验收。最新实证见 `.tmp/issuer-third-event-bridge-v3-1107.json`；报价争议和缺证见 `.tmp/issuer-third-review-1107.json`。

## 公告正文的演绎与独立反证

`run-issuer-document-research` 接受已经通过目录计数、原始 PDF 字节、解析正文和证券身份核验的完整 `coverage_ref`。它在独立任务中冻结当前实际可见的原件 ID、内容哈希和接收时点，使用已配置的固定模型开展条件机制研究与独立反证。历史公告的日期不构成历史预测原点；这个入口也不签发公告概率或模拟持仓数值。

模型先用 `search_evidence` 分页读取完整目录，再用 `fetch_document` 读取精确正文片段。每段保留原件 ID、PDF 哈希、来源 URL、发布时间精度、接收及入库时间，并明确下一段的位置。PDF 二进制保留在原件库中，不进入模型上下文。最终假设引用的每一份原件必须读完全部正文；目录或引用正文没有读完整时，即使模型审查通过，也不能签发该阶段完成状态。OCR 原件继续走原有来源绑定验证。

```powershell
& .tmp/advisory-venv/Scripts/python.exe -m src.stock_app.cli run-issuer-document-research `
  --db-path <独立研究库> --run-id <新任务ID> --symbol 002230 `
  --coverage-ref <完整公告覆盖工件ID> --objective <条件传导与反证目标> `
  --model-policy-version <明确策略版本> --model-evaluation-ref <真实评价范围引用> `
  --max-model-calls 64 --max-tool-calls 48 --critic-reserved-calls 12 `
  --output <尚不存在的结果文件>
```

该命令会产生模型 API 用量。模型、端点哈希、传输、目标、来源和预算进入不可变契约，不能通过重开旧任务扩大预算。恢复必须使用相同参数及新的输出文件名，已经完成的研究不会重复调用模型。连通性评价引用只证明连通性，不能写成投资效果评价。独立反证指分离研究者私有上下文后的证据审查，不等于不同模型或统计独立市场样本。

语义假设、二元公告预测、条件收益抽样、事件混合概率和模拟持仓财富各自验收。新语义结果不会回填 11 月 6 日的原始预测，不会把两个非完备条件情景赋予互补概率，也不会解除历史 PIT 或成熟窗口阻断。

研究入口为 `run-issuer-outlook`。它按实际运行时间登记原始预测，保存独立数据版本、拟合版本、抽样行、条件路径和待结算的期限。可研究的第一个事件是指定发行人在未来若干交易日内至少发布一份公告。公告目录提供日期标签，完整公告正文另行核验。

应用的「持仓情景推演 → 查看已登记的前瞻研究」可查看原始研究结果。如果希望直接只读查看本次实验库，可以在项目目录的 PowerShell 启动单独的查看页。数据库路径只对该进程生效。

```powershell
$env:STOCKM1_FORECAST_DB_PATH = (Resolve-Path '.tmp/forecast-market-history-1106.db').Path
& .tmp/advisory-venv/Scripts/python.exe -m streamlit run visualization/issuer_outlook_page.py
```

## 运行及恢复

在工作区使用指定 Python 解释器。例如，以下参数对应 2026-11-06 的研究。新研究应在实际交易日收盘后运行，并使用新的原点和任务 ID。两个起始日均需为有沪深基准行情的交易日。公告日期训练区间须覆盖市场模型的区间。抓取历史数据需要联网。

```powershell
$python = '.tmp/advisory-venv/Scripts/python.exe'
& $python -m src.stock_app.cli run-issuer-outlook --db-path .tmp/my-issuer-study.db --run-id issuer-20261106 --symbol 002230 --market-start 2025-04-28 --event-start 2012-01-04 --origin 2026-11-06 --output .tmp/my-issuer-study.json
```

也可明确传入 `--frozen-inputs` JSON，三项为 `panel_ref`、`catalogue_ref` 和 `benchmark_refs`（键为 `1.000001` 与 `0.399001`）。这些工件必须存在于指定数据库中。重放既有子任务可传 `--event-run-id` 及 `--scenario-run-id`。同一发行人、模型及原点只登记一份原始预测。补数后再次请求也保留已经签发的预测。所有分阶段结果在 JSON 的 `stages` 中，缺数据和单类训练等情况以 `research_incomplete` 和非零退出码呈现。

## 输入与输出

- 东方财富未复权日线按每段最多 366 个自然日与沪深双基准逐日核对，完整区间再与模型使用的基准日期对照。逐日收益由相邻原价及巨潮三个公司行动目录重算。请求的某段有缺日时，报告具体日期并保留先前通过的请求。
- 巨潮日期目录核对证券、机构、查询范围、计数、页序与唯一公告 ID。预测日前一自然日必须已经结束且被目录覆盖。日期模型使用季节项、周内位置与最近 7、30、90 日的公告次数。四个期限分别拟合固定正则的逻辑回归，训练标签必须成熟并包含两种结果。概率记录用于前瞻检验。
- 条件收益推演复用事件因子回归和同步分块抽样，按交易步比较「所有窗口均无公告」与「仅第一窗口有公告」。特征取前一交易日日期（含）到本交易日日期（不含）间是否出现公告，两种情景使用完全相同的扰动行。输出为**税前股利再投资总收益指数**的样本路径及分位数。两个情景未覆盖其他公告序列，也没有获得校准后的分支权重。
- 数据事务覆盖采集工件、成功检查点和尝试记录。相同输入重开复用原件，冲突内容被拒绝。严格历史查询按已入库版本筛选，可用于选源的对账决议必须已在当时入账。旧对账记录缺接收时间时需要重新核验。

相邻日期窗口可用 `combine-issuer-coverages` 拼接，按时间顺序重复传 `--ref`，明确选 `--scope publication_dates` 或 `--scope full_text`。系统重新检查原件、总数、证券、机构与唯一公告 ID。缺日、重叠、范围混用均阻断。两个任务重新收到相同 PDF 字节和元数据时，采集尝试各自留痕，原件和正文版本各保留一条。

公司行动目录与股本不完全相符时，可用 `bind-transfer-notices` 在独立任务中补充官方实施公告。输入包含 `symbol`、`start`、`end`、三个 `catalogue_refs` 和按除权日索引的 `implementation_notice_refs`。每项给出 `coverage_ref` 和 `document_ref`；同期行权须再给出独立财务报告的 `exercise_coverage_ref` 和 `exercise_document_ref`。期末股本有行权变化时，用 `period_exercise_refs` 将期末日期绑定到该报告。核对的是总股本、现金、每十股比例和实施日期。个人账户的零碎股分配仍需个别账户证据。

### 高管锁定股与停牌日期的核对

深市发行人的巨潮股本记录出现「其他」时，可在独立研究库中运行：

```powershell
& $python -m src.stock_app.cli acquire-equity-history --db-path <研究库> --run-id <新采集ID> --symbol 002230 --output <股本原件索引.json>
& $python -m src.stock_app.cli bind-transfer-notices --db-path <研究库> --run-id <新核验ID> --inputs <含equity_history_ref的公司行动输入.json> --output <核验.json>
```

将第一步的 `equity_history_ref` 填入原公司行动输入。保留所需的实施公告和期末行权证明。重放核对完整响应、证券、日期、相邻两期股数和真实接收时间，只接受全部流通变化可由高管锁定股反向变化精确解释的单一事件。同一任务重试复用已保存的原始响应。其他事件继续按各自的原始凭证处理。

行情缺日先比沪深交易日，再核证券日历的全日停牌时段、复牌原件及独立未复权报价。前后实际成交价可以核对区间边界。交易规则推导的参考价在研究诊断中单列，历史总收益只在连续取得原始成交收盘价的区间重建。002230 的 11 个条目可查 `.tmp/002230-session-equity-1107.json`，发行人余额原件与规则参考价可查 `.tmp/002230-originals-reference-1107.json`。

## 后续交易日结算

用 `prepare-forecast-market` 采集包含原点到评价日的总收益面板，使用 `acquire-issuer-publication-dates` 取得完整的后续公告目录。按实际沪深基准交易日计数，保留每个期限的待成熟、资料不完整、条件未发生和已结算状态。对尚未发生的市场日期没有可填写的实际收益或评分。

```powershell
& $python -m src.stock_app.cli resolve-issuer-publication --db-path <研究库> --forecast-ref <原始预测ID> --catalogue-ref <后续目录ID> --sh-benchmark-ref <沪基准ID> --sz-benchmark-ref <深基准ID> --output <事件核对.json>
& $python -m src.stock_app.cli resolve-publication-returns --db-path <研究库> --run-id <收益情景原任务ID> --report-ref <原始情景报告ID> --outcome-panel-ref <后续收益面板ID> --catalogue-ref <后续目录ID> --output <收益核对.json>
```

事件用 Brier 损失评分。收益情景只有在实际公告序列与假设相符时才计算分位数损失，其他情景保留未命中的原因。相同原件与评价来源产生同一份结算。独立成熟样本、概率集合、财富回放和正式发布另有验收条件。

使用 `register-issuer-evaluation --db-path <研究库> --symbol 002230 --output <协议.json>` 登记前瞻评价。起点取实际登记日期之后的第一个真实交易日。每个期限取前 50 个非重叠窗口，漏掉原点也占一个位置。以后用 `audit-issuer-evaluation` 提供协议 ID、后续公告目录及沪深双基准引用。它从原始登记账本读取预测，重新核对实际发生日期，并列出每个窗的状态。集齐后计算 Brier 损失、相对于该原点训练频率的改进、平均概率偏差及分块 bootstrap 区间。它提供单一事件的诊断统计；多类事件联合校准另行验收。

v2 审计另给出 `window_counts`：已出现原点、已成熟窗口、已登记原始预测、可评分窗口、漏报和待原点分别计数。漏报窗口在结果成熟时仍保留 `end` 和 `mature`，不能用后来的预测替换；`missing_windows` 仍表示距协议所需评分数量的缺口，不表示全部已经漏报。升级仅新增审计工件，不改原协议或旧审计。

11 月 7 日后续证据和逐期限报告见 `.tmp/issuer-continuation-review-1107.json`。继续结算使用 `forecast-market-history-1106-continuation.db` 或其新一致性副本，保留原预测及协议引用。历史补证使用另外两个研究副本，不向旧库写入；首段 33 份原件和 2013 年 238 个独立对齐收盘价仍是今天收到的历史资料。来源绑定通过不代表历史可见性或预测评分通过。

外部历史网页可以用 `acquire-archived-quote-page` 尝试复核。此入口验证 Wayback 回放的原始 URL、响应码、Memento 时间、证券标题及原字节哈希。本地工件以真实接收时点入账，返回值区分候选快照是否早于指定的历史截止。网页 HTML 本身不会成为数值收盘价。

本次实测使用[巨潮官方发行人公告](https://www.cninfo.com.cn/new/disclosure/stock?orgId=9900004565&stockCode=002230)与[东方财富历史行情的 AKShare 接口说明](https://akshare.akfamily.xyz/data/stock/stock.html)。BaoStock 独立长区间请求超过期限，未作为成功数据使用。[BaoStock 官方 SDK](https://pypi.org/project/baostock/) 描述了该匿名行情接口。并发读写边界依据 [SQLite 隔离规范](https://sqlite.org/isolation.html)。
