# 央视同源页面检查点

`src/stock_app/checkpointed_context.py` 提供与 `CCTVReader` 相同的 `read(edition, clock=...)` 接口。只在**新任务冻结的 source_policy** 明确包含下列版本时，由调用方构造此 reader：

```python
from src.stock_app.checkpointed_context import CheckpointedCCTVReader, CCTV_PAGE_CHECKPOINT_VERSION

# 建议 source_policy 键：cctv_page_checkpoint
reader = CheckpointedCCTVReader(
    db_path, root_run_id,
    checkpoint_policy=CCTV_PAGE_CHECKPOINT_VERSION,
)
```

版本值为 `same-source-cctv-page-checkpoint-v1`。旧任务不增加此设置，不改变已冻结来源契约。检查点只在同一个 run 和 edition 内重用，不跨任务复用旧页面。

传输固定为当期 `tv.cctv.com` 官方 HTTPS 索引及文章 URL；每次请求显式使用 `verify=True`、`allow_redirects=False`、`timeout=(10, 30)`。不会自动切换来源、代理、TLS 或 HTTP。可注入相同参数契约的 `get` 供测试。单页最大原始字节数为 10 MiB，此上限进入检查点请求哈希。

每个成功页面先执行与原 reader 一致的索引/文章结构验证，再追加保存到 `ResearchBoard` 的 `cctv-page:` 工件：

- 原始响应 bytes 的 Base64，无重新编码 HTML；
- 原始字节 SHA-256、真实 `received_at`、原始 URL、HTTP 200；
- 验证后的文章标题/正文哈希，或验证后的当期文章链接清单。

超时、重定向、错误期次、损坏结构、短正文等失败页面不缓存。恢复时重新核验缓存哈希、时间、URL 和 HTML，不用重新下载掩盖损坏的缓存，也不覆盖已保存页面的原始接收时间。

页面缓存不是市场数据采集成功。`CCTVReader.read` 仍必须取得该期全部文章，`acquire_market_context` 仍必须取得双指数并通过日历校验，才会原子写入最终市场工件。检查点保存的逐页首次接收时间用于审计；最终完整期工件继续采用原 reader 在整期读取完成时的保守 `known_at` 语义。

专项测试：`tests/test_checkpointed_context.py`、`tests/test_market_context.py`。覆盖中途失败后只重取失败页、坏页不缓存、跨 run 不复用、原始字节和接收时间保持不变、损坏缓存拒绝、重定向拒绝及整期/双指数原子入库。
