# 免费数据源接入核对与恢复记录

更新日期：2026-10-17。行情源切换是用户明确要求的部署变更，不是某个任务失败后的自动回退。

2026-10-30 追加：已接入独立的巨潮官方公告目录与原始 PDF 来源，219 份原件已保存，218 份可提取全文，1 份扫描件阻断；没有用公告冒充完整媒体新闻。东方财富错误 JSONP 与 GDELT 限流的新探测按失败保留。完整范围、官方资料、真实限制及恢复命令见 [原始公告与重建评分](News-Sources-and-Reconstruction-Scoring.md)。下文为原行情/新闻接口的历史验证记录。

## 2026-10-17 真实复验完成

沿用固定 curl_cffi_tls_v1、HTTPS 证书验证及官方参数，不改代理或路由，沪市基准完整返回 243 日。新的 live-full-eastmoney-20261017 已自动采集 430 条深市行情、104 期财报、1 条行业记录、100 条新闻、100 条文本特征、100 条资金流；行情逐段通过沪深基准日期覆盖检查。独立沪市 600519 在 2026-10-08 至 2026-10-15 的 6 条行情亦通过同样核验。

相同客户端/基准/区间的控制对照中，有 ut 与无 ut 均返回 243 条，因此不能把此前 TLS 错误归因于缺少 ut，也不能宣称已确定此前间歇网络错误的具体根因。官方参数继续保留。最新新采集任务已完成模型研究、四轮独立审查与相同命令恢复，状态 research_complete；23 次模型/15 次工具，恢复不增加计数。详细验收见运行手册和 Session-Checkpoint-20261017.md。

修复 CLI 来源选择顺序：原默认值在 .env 加载前求值，可能错误忽略部署配置；现在先加载配置、再选择来源，显式命令行始终优先。缺省来源明确改为 eastmoney_http，而非失败时自动换源。新回归覆盖延迟加载与显式覆盖两个场景。

## 接入前核对的 API 文档

- BaoStock 发布页：https://pypi.org/project/baostock/ ，确认免费匿名使用、当前版本 0.9.4 和入口。
- BaoStock 项目 API 文档：https://github.com/zxygithub/baostock/blob/master/docs/pythonAPI.md 。核对 login/logout、query_history_k_data_plus、query_trade_dates、query_stock_basic、错误码与结果集迭代。
- 官网文档入口：https://baostock.com/baostock/index.php/Python_API文档 ，当前网页无法直接展开正文，因此结合项目文档与已安装官方包源码验证，不凭第三方转述猜测接口。
- AKShare 官方股票文档：https://akshare.akfamily.xyz/data/stock/stock.html ，核对 stock_news_em 与 stock_individual_fund_flow。
- 官方新闻实现：https://raw.githubusercontent.com/akfamily/akshare/main/akshare/news/news_stock.py ，当前使用 curl_cffi 请求同一东方财富端点。
- 官方资金流实现：https://raw.githubusercontent.com/akfamily/akshare/main/akshare/stock/stock_fund_em.py ，核对日期、主力及各订单规模净额/占比字段。
- 东方财富日线及指数协议：AKShare 股票文档 stock_zh_a_hist（不复权、日频、成交量为手、成交额为元）；官方实现 https://raw.githubusercontent.com/akfamily/akshare/main/akshare/stock_feature/stock_hist_em.py 与 https://raw.githubusercontent.com/akfamily/akshare/main/akshare/index/index_zh_em.py 。核对 klt=101、fqt=0、secid、beg/end、11 项日线字段及沪深基准标识后才接入。

BaoStock 日线明确 frequency=d、adjustflag=3 为原始未复权价格；volume 已是股、amount 已是人民币元，不套用 TuShare 的手/千元乘数。
停牌应保留上一交易日收盘价、零成交量及零成交额，并记录 tradestatus，不删掉该交易日。
SDK ResultSet 必须完整迭代，并检查开始、迭代及结束错误码。接入时与上市期间和交易日历核对，缺日或返回复权数据明确失败。
免费可用不代表永久 SLA 或任意再分发许可，工件保留研究用途及来源信息。

## 本轮实现与真实验证

- 新增 scenario_engine/data/baostock_adapter.py、src/stock_app/price_worker.py；SDK 全局 socket 在独立进程内执行，180 秒期限，失败不换源。
- prepare_v3_equity_prices 新增 source_id；默认及 STOCKM1_PRICE_SOURCE 可指定 baostock。旧 TuShare 需显式选择。deductive-research 新增 --price-source。
- 实际匿名登录与日线查询通过。适配器实际读取 000001、600519 在 2026-10-08 至 2026-10-15 的 12 条行情并入库，交易日历和原始单位核验通过。
- 新闻同一请求用 requests 得到错误 callback/passportWeb；用 curl_cffi 实际得到正确 callback 与 100 条新闻。修复后已写入 100 条原始文章及 100 条文本特征。
- 资金流从全市场同花顺百页抓取改为明确的东方财富单证券日资金流契约。主力净流入保留独立字段 main_net_inflow_cny，不能冒充原先全口径净流入。schema 为 eastmoney-daily-order-flow-v1。
- 同一资金流 HTTPS 接口在 Python OpenSSL/BoringSSL 客户端上复现 TLS BAD_RECORD_MAC/BAD_DECRYPT，Windows 系统 curl（Schannel）对沪深证券均成功。未发现进程代理环境变量；实验定位为当前环境的 TLS 客户端栈兼容问题。
- 阅读 curl 官方证书文档 https://curl.se/docs/sslcerts.html 后，新增 public_https.py；Windows 在请求前明确选择系统 Schannel，其他系统使用 requests TLS。禁止失败后自动切换、关闭证书验证或改用 HTTP。端点固定、忽略 curlrc、拒绝重定向、限制 HTTPS、响应大小和执行期限，传输版本写入研究来源契约。
- 修复后真实 worker 曾为 000001 写入 100 条资金流工件，严格金额、占比、证券及市场校验通过。后续复测 Schannel 也出现解密失败，因此不能宣称该传输已经稳定解决网络问题。
- 长区间 BaoStock 全流程在登录阶段被提前关闭连接，官方 SDK 的 recv 空字节循环不退出，外层 180 秒期限明确阻断。与区间长度无关，没有将已失败任务标为成功。
- 只读网络检查确认 Meta Tunnel 为活动接口并接管默认路由及 DNS，国内数据域名返回 198.18.* 虚拟地址；显式查询其他 DNS 也被接管。证据只能定位到受该网络路径影响，尚未证明具体代理规则或服务端责任。未修改系统路由、代理或证书设置。
- 新增显式 eastmoney_http 日线接入：430 条原始日线探测成功，但双交易所基准核验及后续资金流又遇 TLS 错误，所以新任务仍明确 data_incomplete。沪深基准日期必须一致且证券覆盖所有这些日期；停牌或上市不全的窗口拒收，不补价。每段最多 366 个自然日，成交量乘 100 成股、成交额保留元，来源 URL 及接收时间入库。
- 发现并修复另一真实阻断：同一发布时间的不同新闻被误判为修订冲突。原始文章和文本特征按稳定文章 URI 区分，不同文章保留；同文章、同可见时间的不同版本仍报冲突。

实测数据库均在已忽略的 .tmp：baostock-verified-1016.db、public-verified-1016.db。
紧急保存后继续工作，原 97% 额度记录属于此前时段，不代表当前可用额度。最近完整回归 268 passed（54.95 秒）；后续完整研究和回归结果以运行手册最新记录为准。
既有 .tmp/deductive-live-1016.db 是前轮的失败研究，变更来源后应使用新 run-id，不能修改原冻结契约。

## 紧急恢复提示词

**最新状态以 Docs/Session-Checkpoint-20261017.md 与运行手册首节为准。** 10 月 17 日全新下载、研究、独立审查与恢复已通过；下面的前期提示词仅保留排查历史。

继续 StockM1 三个数据阻断修复。先阅读 Docs/Free-Data-API-Integration.md、Docs/Deductive-Research-Runbook.md 和 Docs/Quant-Finance-Implementation-Progress.md，再检查 git diff。
用户要求免费、稳定行情源替代 TuShare，且先严格阅读 API 文档；已核对 BaoStock/AKShare 官方文档并接入 BaoStock 0.9.4 及显式 eastmoney_http 日线。BaoStock 沪深两股 12 条实际行情、新闻 100 条及文本特征、资金流 100 条均曾通过真实采集，但后续 BaoStock 登录 EOF 及包括 Schannel 在内的资金/行情 TLS 间歇失败仍阻断新下载，不能宣称全流程成功。已确认 Meta Tunnel 接管默认路由与 DNS，不要擅自修改系统网络。
隔离 Python 是 .tmp/advisory-venv/Scripts/python.exe；保留全部未提交改动。不要自动提交，不要重置，不要输出凭据。当前默认行情改为 baostock，显式旧 TuShare 测试已调整；字段区分主力净流入和全口径资金流。
用 STOCKM1_SCENARIO_LLM_ENABLED=0 跑离线全量测试，新加 tests/test_baostock_adapter.py。前轮全量 250 passed，本轮已通过 14 项外部 API/公共数据专项；最新专项结果见进度记录。还需跑全量、补足 BaoStock 残缺日历/中途错误测试，核查默认源和诊断页面的一致性。
检查 .tmp/deductive-free-live-1016.db 与 .tmp/live-free-source-output.log 的真实全流程状态；run-id 为 live-free-source-20261016，行情区间 2025-01-01 至 2026-10-15。失败必须保留并解决，不能删掉资金域或填零。确认六域采集、冻结、实际模型工具调用、结构化假设、独立反证和恢复都通过。不要将研究完成宣称为联合风险校准或正式投资建议。
5 小时额度接近耗尽时按用户要求保存进度；本轮在使用约 97% 时紧急落盘，不能自动兑换额度或购买用量。
