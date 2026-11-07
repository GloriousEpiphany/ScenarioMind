---
status: in-progress
branch: main
base_commit: ba54d1f
timestamp: 2026-10-16T01:38:00+08:00
---

# 演绎研究与真实采集恢复记录

最新状态：2026-10-17 已完成全新六域采集、真实模型、独立审查及恢复验收，见 [新恢复记录](Session-Checkpoint-20261017.md)。本文保留 10 月 16 日尚有阻断时的历史状态，不能据此忽略后续验收结果。

## 目标与保存原因

按 Docs/Quant-Finance.md 继续演绎模块重构，解决 TuShare 行情权限、新闻协议不匹配、资金流超时，完成真实全流程。核心失败不得用空值、较弱实现、删除证据域或自动换源掩盖。用户自行 Git 提交；全部修改未提交。5 小时额度约 91% 时按用户要求紧急保存，未兑换重置或购买额度。

## 已完成与证据

- 已先阅读 BaoStock、AKShare 股票/指数及新闻/资金流的官方文档和实现；链接及单位核对见 Free-Data-API-Integration.md。
- 显式 BaoStock 未复权日线，成交量股、金额元，上市期间/交易日历/停牌/错误迭代严格校验。短区间沪深两股 12 条真实入库。
- 显式 eastmoney_http 日线，成交量手乘 100、金额元；分段核对沪深基准日期与证券覆盖，缺日明确失败；原始证券 430 条探测通过，完整基准验证尚被 TLS 阻断。最新补 ut 参数未联网复验。
- 新闻精确解析 callback 与文章表，curl_cffi 曾采集 100 篇及 100 条文本特征。资金流改为单证券 100 日估算，不把主力净流入冒充全口径；多个独立探测曾成功，但未达到持续稳定验收。
- PIT 查询精确 UTC/微秒比较；同时间不同文章按 URI 保留，同文章冲突仍失败。财报各报告期不再过早失效；schema v2 实际 104 期入库。
- 固定模型、思考续传、单工具调用、证券枚举、必须请求独立审查均验证；角色提示和客户端进入冻结契约。新版审查非空 blocking_findings 强制拒绝，修订只导出最新假设，旧版仍可审计。
- 最终真实证据模型任务：.tmp/deductive-real-evidence-stage1016.db / preserved-real-evidence-20261016-v5。308 条冻结工件，research_complete，15 次模型/13 次工具；重开恢复计数仍 15/13。结果 .tmp/real-evidence-model-result.json，恢复日志 .tmp/real-evidence-replay.log。明确为 preserved_real_data_model_stage_only；没有声称新下载已通过或投资发布已获准。

## 网络状态：已恢复，无遗留变更

确认 Clash Verge/Mihomo 全局代理、TUN、fake-ip；Meta 接管路由/DNS。用户明确答复“允许临时调整并恢复”。已阅读 Mihomo 官方规则/API 文档，经活动生产命名管道做两次限域 DIRECT 对照，其余 MATCH,GLOBAL。实际规则命中确认直连生效，但 BaoStock 登录 EOF 与部分 TLS 解密失败仍复现，不能简单归因于全局代理。

两次均已 finally 恢复原 global、522 条规则及原 Selector 选择，磁盘配置哈希一致。证明 .tmp/mihomo-direct-network-proof.json 和 .tmp/mihomo-direct-eastmoney-proof.json 均 restored=true；最后再 GET /configs 确认为 global/tun=true。勿以为还处于临时直连。

脚本 .tmp/mihomo_control.py 是当前活动生产管道客户端；clash-verge.yaml 中的 sidecar 管道已过期，不能照搬。脚本不打印凭据。不要将代理完整配置或密钥复制进文档/日志；原配置未改写。未来临时实验仍须恢复。

## 未完成与优先下一步

1. 全新一键采集仍 data_incomplete；失败保留于 .tmp/deductive-direct-live-1016.db（BaoStock）和 .tmp/deductive-direct-eastmoney-live-1016.db（东方财富）。后者财务/行业/新闻/资金成功检查点已保存，行情失败。不要将已有真实证据的 v5 完成误当作此任务成功。
2. 最后行情出现 SDK 数值 code 拼接 TypeError，已改 str(code)，新增 test_provider_numeric_error_code_preserves_original_failure；补齐官方日线 ut 参数，需实际复验。使用受限新任务/准确冻结参数，不覆盖老契约；客户端已改默认 curl_cffi_tls_v1，旧 Schannel 契约任务不兼容。
3. 使用 select_public_http_transport 明确选择同一客户端，不失败后换 TLS 栈。独立实验证明 DIRECT + curl_cffi 资金可返回 100 条，但不能据此宣称日线/基准稳定。优先对完全相同请求做可复现对照，避免继续盲调 TLS；保持证书验证。
4. 如现有免费源确实不可达，可评估新的免费来源，但必须先读官方 API 文档，严格对齐单位、未复权、真实成交额、交易日及停牌，不复用无金额来源掩盖缺失。
5. 全新六域数据成功后跑实际模型、结构化假设、独立审查与重开恢复。CLI 预算默认 64/48/12，可显式配置，冻结后不能自动增加。正式联合风险校准、多期优化、发布闸门仍未完成。

## 环境、测试与文件

PowerShell，工作区 D:/Software Documents/JetBrains/PyCharm Professional 2025/StockM1；隔离 Python .tmp/advisory-venv/Scripts/python.exe（3.13）。默认系统 3.14 不适用。使用 login=false；禁止打印密钥。无子代理、无提交、无推送。原 git HEAD main ba54d1f。

离线全量：设置 STOCKM1_SCENARIO_LLM_ENABLED=0，运行该解释器 -m pytest -q -p no:cacheprovider --basetemp=.tmp/唯一目录。最后完整结果 273 passed in 54.62s，包含传输配置及数值错误回归；git diff --check 通过。保存时 36 个 Git 修改/未跟踪路径，均未提交。

新增主文件：deductive_research.py、eastmoney_prices.py、price_worker.py、public_https.py、public_transport.py、public_worker.py、data/baostock_adapter.py、agents/deductive_tools.py；对应新增测试 acquisition_transports、baostock_adapter、deductive_research、document_observation_identity、eastmoney_prices。其他修改以 git status/diff 为准，禁止清理未提交工作。探测数据库、脚本、日志均在已忽略的 .tmp。
