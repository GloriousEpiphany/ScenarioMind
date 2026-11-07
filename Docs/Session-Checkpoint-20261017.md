---
status: research-workflow-verified
branch: main
base_commit: ba54d1f
date: 2026-10-17
---

# 真实演绎研究全流程已验证

本轮目标已完成：从全新数据获取到模型研究、独立审查及重开恢复，未降级、未提交 Git。此状态只覆盖工程研究流程，不代表整个 Quant-Finance 路线、联合风险校准、多期优化或投资建议发布全部完成。

## 验收证据

- 新数据库 .tmp/deductive-full-live-1017.db，新任务 live-full-eastmoney-20261017，CLI 退出码 0、research_complete。
- 明确 source=eastmoney_http、profile=curl_cffi_tls_v1；价格区间 2025-01-01 至 2026-10-15，研究目标为核查平安银行证据、提出可追溯且有反证条件的路径，不发布投资建议。
- 全新获取 835 条：行情 430、财报 104、行业 1、新闻/文本特征/资金流各 100。沪深基准覆盖核对保持开启；另独立验证沪市 600519 在 2026-10-08 至 2026-10-15 的 6 条日线。
- 采集完成时冻结 777 条：价格 430、研究时间窗口内财报 46、行业 1、其余三域各 100。冻结时点 2026-10-17T03:54:38.397180+00:00。
- 23 次固定模型调用、15 次工具执行，四轮独立审查，前三轮拒绝并保留原因，第四轮通过最新修订的 8 条假设。没有放宽 blocking_findings 闸门或删除资金域。
- 相同命令恢复后采集任务仍 5 项、模型/工具仍 23/15，原始工件和不可变结果哈希保持不变。
- 证明 .tmp/deductive-full-proof-1017.json，执行日志 .tmp/deductive-full-live-1017.log，恢复日志 .tmp/deductive-full-replay-1017.log。证明脚本 .tmp/verify_full_run_1017.py 直接读取真实账本，没有假接口。
- 最新全量 275 passed in 55.75s；强化来源回归 2 passed，旧预加载默认值负对照确实失败。git diff --check 已通过。

## 本轮修复与边界

CLI 的来源默认值原先在 .env 加载前求值，可能忽略部署配置。现在先加载配置再选来源；--price-source 优先，STOCKM1_PRICE_SOURCE 次之，缺省明确为 eastmoney_http。旧 BaoStock 部署配置仍保留且失败不切源；恢复旧任务须保持原冻结来源。

当前请求已恢复并通过新流程，但有/无官方 ut 参数的同条件基准查询均返回 243 日，不能声称已经确定此前 TLS 错误的具体根因。官方参数保留，HTTPS 证书校验、无重定向、超时和数据完整性约束没有降低。

本轮没有修改代理、路由、证书或凭据。前轮获授权的临时网络实验已经恢复，仍见 10 月 16 日的证明文件。未来网络问题应保留错误记录并做同条件对照，禁止盲目切换客户端/来源。

输出仍 publication_eligible=false，probability_status=uncalibrated，numeric_impact_status=not_estimated。研究审查通过不能代替真实样本外校准或交易发布闸门。未实现事项继续见 Quant-Finance-Implementation-Progress.md，不能将本次成功包装成所有路线均已完成。

## 环境与继续工作

工作区 D:/Software Documents/JetBrains/PyCharm Professional 2025/StockM1，PowerShell，解释器 .tmp/advisory-venv/Scripts/python.exe（Python 3.13）。保留全部未提交修改；用户自行提交，不自动提交或推送，不打印凭据。

复现命令见运行手册；此次完整命令参数如下：

```powershell
.\.tmp\advisory-venv\Scripts\python.exe -m src.stock_app.cli deductive-research `
  --db-path .tmp/deductive-full-live-1017.db --price-source eastmoney_http `
  --run-id live-full-eastmoney-20261017 `
  --objective '核查平安银行公开证据，提出可追溯、有反证条件的事件传导假设；不发布投资建议' `
  --symbol 000001 --start-date 2025-01-01 --end-date 2026-10-15 `
  --model-policy-version integration-canary-v4 `
  --model-evaluation-ref connectivity-only-not-effectiveness
```

该已完成任务可用于无重复调用的恢复核验。新目标/区间/来源/模型/预算必须新建 run-id，不更改旧契约。模型评价引用明确是集成连通性用途，不代表已获得金融有效性评价。
