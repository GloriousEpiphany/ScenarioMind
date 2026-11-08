# 2026-11-08：历史数据免费入口与配置

本轮新增数据入口均无需 API Key。免费和可访问不等于覆盖完整、具有永久 SLA 或已经证明历史可见性；以下接入均保留实际接收时间。

后续实测补充：Common Crawl 的 [公开 CDXJ/S3 索引](https://www.commoncrawl.org/cdxj-index) 无需密钥，已通过 `commoncrawl_s3` 路径取得一份真实近年业绩预告原字节证明，修复查询 API 持续 504 对任务的阻断。巨潮原件也已核实实际 above/within/below 类别，具体范围见 [当前进度](量化研究实现进度.md)。

额外核查了其他免费 API：[Internet Archive Availability](https://archive.org/help/wayback_api.php) 可匿名返回捕获线索，本次两个近期目标均为空；[Media Cloud](https://www.mediacloud.org/documentation/faqs) 提供新闻检索与 URL，但不提供可下载新闻全文；[NewsAPI 免费开发计划](https://newsapi.org/pricing) 仅覆盖最近一个月、每日 100 次、延迟 24 小时且不提供全文。这些服务的 Key 都不能直接补足本次历史 PDF 原字节证明，因此不要求用户为了无效依赖申请 Key。新增数据路径仍全部免费匿名；模型 API 沿用现有配置。

| 用途 | 来源与入口 | 实现及真实核验 |
|---|---|---|
| 独立历史原字节 | [Common Crawl](https://commoncrawl.org/get-started)、[Arquivo CDX](https://github.com/arquivo/pwa-technologies/wiki/URL-search:-CDX-server-API)、Wayback | CDX/WARC 或 Memento/原字节复验；300125 近期原件成功；旧目标未取得证明，保留空查询、504 和网络失败 |
| 历史新闻线索 | [GDELT 原始数据](https://gdeltproject.org/data.html)、[GKG 2.1 口径](https://data.gdeltproject.org/documentation/GDELT-Global_Knowledge_Graph_Codebook-V2.1.pdf)、[Arquivo 全文搜索 API](https://github.com/arquivo/pwa-technologies/wiki/Arquivo.pt-API) | 实测 GDELT `20150410000000` 切片 2,366 行；iFlytek 0 条，China 108 条。索引处理时间不是原新闻发布时间，索引记录也不是新闻全文 |
| 预告与实绩候选 | 东方财富公开业绩数据接口 | 实测 002230 预告 67 条、财报 81 条；剔除零值占位、非归母净利润指标和歧义记录。当前聚合记录可能已修订，只用于定位官方原件 |
| 官方原件与修订 | 巨潮官方目录及 PDF | 全目录分页计数、原 PDF、原文和哈希绑定。财报限定范围使用独立 coverage 类型，不能替代全公告或全媒体覆盖 |

命令均通过 `.tmp/advisory-venv/Scripts/python.exe -m src.stock_app.cli` 调用，详细参数可加 `--help`：

- `discover-historical-news --provider gdelt --query iFlytek --start 2015-04-01T00:00:00+00:00 --end 2015-04-30T23:59:59+00:00 --slice-timestamp 20150410000000 --db-path … --run-id … --output …`。一个切片仅覆盖指定 15 分钟，不把零命中写成全史缺失。
- `discover-historical-news --provider arquivo` 使用同样日期参数及可选 `--offset`，返回分页检索线索；逐条核对捕获日期，过滤超出请求范围的结果。
- `discover-profit-controls --symbol 002230 --db-path … --run-id … --output …`：保存逐页原始响应，所有候选在原件核验前均为 `eligible_training_label=false`。
- `acquire-financial-disclosures --symbol 002230 --start 2024-01-01 --end 2025-06-30 --db-path … --run-id … --output …`：先核完整官方目录，再下载所有财报、预告和更正；排除项目及范围显式保存。
- `build-financial-document-coverage --acquisition-run-id …`：对已有全目录采集建立上述明确限定范围，任一必需财报缺失即失败。不会把相关公司审计附件标为发行人自己的财报。

`--direct` 只改变本次 HTTP 会话是否读取环境代理，不修改系统代理、DNS 或证书；不在失败后自动换源、关闭证书校验或降低 HTTPS。

## 如果使用已有可选密钥服务

当前免费历史方案不要求申请新密钥。TuShare 的 [历史新闻接口](https://tushare.pro/document/2?doc_id=143) 需要独立权限，不能把普通 token 当作免费历史新闻权限。本次没有购买或依赖该服务。若自行选择 TuShare：注册登录后进入“个人中心 → 账号与 TOKEN”，复制 token，按[官方说明](https://tushare.pro/document/1?doc_id=39)操作；填入应用的 TuShare 凭据设置，或本机环境变量 `TUSHARE_TOKEN`。应用现有探测是行情权限验证，不代表新闻权限已开通。

模型仍使用应用已配置的地址、模型名和 API Key；在应用凭据页填写，或配置 `SCENARIO_LLM_API_BASE`、`SCENARIO_LLM_MODEL`、`SCENARIO_LLM_API_KEY`。不要把真实密钥写入文档、命令日志或 Git。免费数据 API 与模型 API 是两种不同服务。

## 既有行情与新闻

当前组合使用显式配置的行情来源，默认 eastmoney_http；BaoStock、TuShare 和腾讯需显式选择，不在失败时静默切源。交易日、单位、证券、复权及公司行动必须对账；单源网络失败不是零行情。新闻与资金流分别保留原文/派生特征及主力资金口径，媒体语气不代表投资者实际预期。来源切换须新建冻结请求。
