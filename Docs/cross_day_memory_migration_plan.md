# StockM1 跨天记忆系统迁移方案

> 基于 Letta-Code 核心记忆架构，为 StockM1 AI 交易系统设计跨天持久化记忆能力

## 一、核心思想映射

### 1.1 Letta-Code 记忆架构核心要素

| Letta-Code 概念 | 说明 |
|---|---|
| Memory Block | 带标签、描述、容量限制的结构化记忆单元 |
| 层级组织 | `system/project/tooling/bun.md` 式的树状结构 |
| 跨 Session 持久化 | 记忆在会话间保持不丢失 |
| 记忆范围 | system-attached（始终加载）vs detached（按需加载）|
| 记忆反思 | 对话结束后自动回顾并更新记忆 |
| 记忆初始化 | 首次运行时探索环境并填充记忆 |
| 版本控制 | Git 追踪记忆变更历史 |
| 前置元数据 | frontmatter 定义 description、limit、read_only |

### 1.2 迁移映射关系

| Letta-Code | StockM1 | 说明 |
|---|---|---|
| Session（会话）| Trading Day（交易日）| 基本时间单位 |
| Agent（持久实体）| Trading Agent（跨天持久实体）| 记忆载体 |
| persona block | agent_identity block | Agent 的交易风格与策略偏好 |
| human block | market_context block | 当前市场环境认知 |
| project block | portfolio_strategy block | 持仓策略与投资逻辑 |
| memory_filesystem | memory_index block | 记忆索引与目录树 |
| 记忆反思 | 收盘反思（End-of-Day Reflection）| 每日收盘后回顾与更新 |
| /init 初始化 | 首日市场认知初始化 | 建立初始市场理解 |
| Git 版本控制 | JSON/SQLite 时间线存储 | 记忆变更可追溯 |

---

## 二、记忆架构设计

### 2.1 记忆层级体系

借鉴 Letta-Code 的三层记忆模型，设计 StockM1 的四层记忆体系：

```
StockM1 Memory Architecture
├── Layer 1: Core Memory（核心记忆 - 始终加载到 system prompt）
│   ├── agent_identity        # Agent 交易风格、风险偏好、策略框架
│   ├── market_regime         # 当前市场状态判断（牛/熊/震荡/结构性行情）
│   ├── portfolio_state       # 当前持仓逻辑与止盈止损计划
│   └── active_watchlist      # 当前重点关注的股票及关注理由
│
├── Layer 2: Rolling Memory（滚动记忆 - 近 N 天窗口，始终加载）
│   ├── daily_digest/         # 每日摘要（按日期组织）
│   │   ├── 2020-01-02.md     # 当日市场概况 + 操作 + 反思
│   │   ├── 2020-01-03.md
│   │   └── ...（保留最近 N 天）
│   └── sentiment_trend       # 近期市场情绪走势连续记录
│
├── Layer 3: Archival Memory（归档记忆 - 按需检索，不主动加载）
│   ├── weekly_summaries/     # 周度总结（由日度摘要压缩而来）
│   ├── earnings_memory/      # 重要财报记忆
│   ├── event_memory/         # 重大事件记忆（政策变化、黑天鹅等）
│   └── trade_lessons/        # 交易教训与经验总结
│
└── Layer 4: Meta Memory（元记忆 - 关于记忆本身的记忆）
    ├── memory_index           # 所有记忆块的索引目录
    ├── memory_stats           # 记忆使用统计
    └── strategy_evolution     # 策略演化记录
```

### 2.2 记忆块（Memory Block）数据结构

每个记忆块遵循统一的结构定义，借鉴 Letta-Code 的 frontmatter 模式：

```python
@dataclass
class MemoryBlock:
    """记忆块基础结构"""
    label: str              # 唯一标识，如 "market_regime", "daily_digest/2020-01-02"
    category: str           # 所属层级: "core" | "rolling" | "archival" | "meta"
    description: str        # 描述该记忆块的用途
    value: str              # 实际内容（Markdown 格式）
    limit: int              # 字符数上限
    read_only: bool         # 是否只读（Agent 不可修改）
    created_at: str         # 创建日期
    updated_at: str         # 最后更新日期
    expires_at: str | None  # 过期日期（用于滚动记忆的自动清理）
    importance: float       # 重要性评分 0.0-1.0（用于记忆压缩决策）
    tags: list[str]         # 标签，如 ["earnings", "sh600519", "policy"]
```

### 2.3 各核心记忆块详细定义

#### Core Memory 块

**agent_identity（Agent 身份与策略框架）**
```markdown
---
label: agent_identity
category: core
description: Agent 的交易风格、风险偏好和策略框架，影响所有交易决策
limit: 3000
read_only: false
---

# 交易风格
- 偏好价值投资与趋势跟踪结合
- 单只股票仓位不超过总资产的 30%
- 优先选择有基本面支撑的技术突破

# 风险管理
- 单笔交易最大亏损容忍度: 5%
- 总仓位回撤警戒线: 10%
- 空仓观望条件: 市场系统性风险升高时

# 策略偏好
- 关注行业轮动机会
- 重视成交量与价格的配合
- 财报季前后谨慎操作
```

**market_regime（市场状态判断）**
```markdown
---
label: market_regime
category: core
description: 对当前市场整体状态的判断，每日更新，直接影响仓位管理
limit: 2000
read_only: false
---

# 当前市场状态: [震荡偏多]
- 判断依据: 上证指数站稳 3000 点，成交量温和放大
- 主导风格: 大盘价值股领涨，科技成长股分化
- 风险因素: 中美贸易摩擦不确定性
- 建议仓位: 60%-70%

# 状态变更历史
| 日期 | 状态 | 触发原因 |
|------|------|----------|
| 2020-01-02 | 震荡偏多 | 年初资金面宽松 |
```

**portfolio_state（持仓策略状态）**
```markdown
---
label: portfolio_state
category: core
description: 当前持仓的投资逻辑和操作计划，每次交易后更新
limit: 3000
read_only: false
---

# 当前持仓逻辑

## 贵州茅台 (sh600519) - 仓位 25%
- 买入逻辑: 消费升级 + 春节旺季预期
- 目标价: 1200 元
- 止损价: 1050 元
- 关注点: 下周四季度财报

## 现金 - 40%
- 保留原因: 等待科技板块回调买入机会
```

**active_watchlist（活跃关注列表）**
```markdown
---
label: active_watchlist
category: core
description: 当前重点关注的股票及关注理由，每日更新
limit: 2000
read_only: false
---

# 关注列表

| 股票 | 关注理由 | 触发条件 | 优先级 |
|------|----------|----------|--------|
| sh601318 中国平安 | 保险板块估值修复 | 跌破 80 元加仓 | 高 |
| sz000858 五粮液 | 白酒板块联动 | 放量突破 150 | 中 |
| sh688981 中芯国际 | 半导体国产替代 | 等待财报确认 | 低 |
```

#### Rolling Memory 块

**daily_digest（每日摘要 - 最关键的滚动记忆）**
```markdown
---
label: daily_digest/2020-01-03
category: rolling
description: 2020-01-03 交易日全景摘要
limit: 2000
expires_at: 2020-01-23  # 20 个交易日后过期
importance: 0.8
tags: ["daily", "2020-01-03"]
---

# 2020-01-03 交易日摘要

## 市场概况
- 上证: 3083.79 (+1.15%)  深证: 10955.36 (+1.99%)
- 成交额: 6832 亿（较昨日放量 15%）
- 涨停: 87 家  跌停: 12 家
- 北向资金: 净流入 45.2 亿

## 重要新闻
- [宏观] 央行宣布降准 0.5 个百分点，释放长期资金约 8000 亿
- [行业] 新能源汽车补贴政策延续，板块集体大涨
- [个股] 贵州茅台发布提价公告，股价涨 3.2%

## 今日操作
- 买入 sh600519 贵州茅台 200 股 @ 1088.00（消费升级+提价利好）
- 持有 sh601318 中国平安（等待保险板块轮动）

## 收盘反思
- 降准利好兑现，市场情绪明显回暖
- 茅台买入时机较好，提价公告是催化剂
- 需关注: 降准后资金是否持续流入，还是利好出尽
- 教训: 无

## 情绪评分: 7/10（偏乐观）
```

**sentiment_trend（情绪走势连续记录）**
```markdown
---
label: sentiment_trend
category: rolling
description: 近期市场情绪连续走势，用于判断情绪拐点
limit: 2000
read_only: false
---

# 市场情绪走势（近 10 个交易日）

| 日期 | 情绪分 | 关键词 | 北向资金 | 成交额 |
|------|--------|--------|----------|--------|
| 01-03 | 7/10 | 降准利好 | +45.2亿 | 6832亿 |
| 01-02 | 5/10 | 开门红平淡 | +12.1亿 | 5940亿 |

# 情绪趋势判断
- 方向: 回暖中 ↑
- 持续性: 需观察成交量能否维持
- 风险信号: 暂无
```

---

## 三、记忆生命周期管理

### 3.1 记忆生命周期流程

```
                    ┌─────────────────────────────────────────┐
                    │           每个交易日的记忆流程             │
                    └─────────────────────────────────────────┘

  ┌──────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
  │  开盘前   │───▶│  盘中决策     │───▶│  收盘处理     │───▶│  记忆维护     │
  │  Memory   │    │  Memory      │    │  Memory      │    │  Memory      │
  │  Load     │    │  Consult     │    │  Reflect     │    │  Maintain    │
  └──────────┘    └──────────────┘    └──────────────┘    └──────────────┘
       │                 │                   │                    │
       ▼                 ▼                   ▼                    ▼
  加载核心记忆      查询相关记忆        生成每日摘要          压缩过期记忆
  加载滚动记忆      检索归档记忆        更新核心记忆          归档旧日摘要
  注入新闻数据      辅助买卖决策        记录交易教训          更新记忆索引
```

### 3.2 各阶段详细流程

#### Phase 1: 开盘前记忆加载（Pre-Market Memory Load）

```python
def pre_market_memory_load(agent, current_date, memory_store):
    """开盘前加载记忆到 Agent 的 system prompt"""

    # 1. 始终加载: Core Memory（全部 4 个块）
    core_blocks = memory_store.load_category("core")
    # → agent_identity, market_regime, portfolio_state, active_watchlist

    # 2. 始终加载: Rolling Memory（近 N 天的 daily_digest）
    rolling_window = config.ROLLING_MEMORY_DAYS  # 默认 10 个交易日
    recent_digests = memory_store.load_rolling_digests(
        current_date, window=rolling_window
    )
    sentiment = memory_store.load_block("sentiment_trend")

    # 3. 注入当日新数据（非记忆，而是新输入）
    today_news = news_provider.get_news(current_date)
    today_earnings = earnings_provider.get_earnings(current_date)
    today_economic = economic_provider.get_indicators(current_date)

    # 4. 组装 system prompt
    system_prompt = build_memory_prompt(
        core_blocks=core_blocks,
        rolling_digests=recent_digests,
        sentiment=sentiment,
        today_news=today_news,
        today_earnings=today_earnings,
        today_economic=today_economic,
    )

    return system_prompt
```

#### Phase 2: 盘中决策记忆查询（In-Session Memory Consult）

Agent 在做买卖决策时，可以主动检索归档记忆：

```python
# Agent 可用的记忆工具
MEMORY_TOOLS = [
    {
        "name": "recall_memory",
        "description": "检索归档记忆。可按股票代码、事件类型、日期范围搜索",
        "parameters": {
            "query": "搜索关键词",
            "stock_code": "可选，股票代码",
            "category": "可选: weekly_summaries | earnings_memory | event_memory | trade_lessons",
            "date_range": "可选，如 '2020-01-01:2020-01-15'"
        }
    },
    {
        "name": "update_core_memory",
        "description": "更新核心记忆块。用于在交易过程中更新市场判断或持仓逻辑",
        "parameters": {
            "block_label": "要更新的记忆块标签",
            "action": "update | append | replace",
            "content": "新内容"
        }
    }
]
```

#### Phase 3: 收盘反思（End-of-Day Reflection）

```python
def end_of_day_reflection(agent, current_date, memory_store, trade_log):
    """收盘后触发 Agent 进行记忆反思"""

    reflection_prompt = f"""
    今天是 {current_date}，交易日结束。请进行收盘反思：

    1. 总结今日市场表现（大盘走势、板块轮动、资金流向）
    2. 回顾今日操作决策，评估是否合理
    3. 记录重要的新闻/事件及其潜在影响
    4. 更新市场情绪评分（1-10）
    5. 识别需要持续关注的信号
    6. 如有交易教训，记录到 trade_lessons

    请使用 update_daily_digest 工具生成今日摘要，
    并使用 update_core_memory 更新需要调整的核心记忆。
    """

    # Agent 自主生成反思并更新记忆
    agent.run_reflection(reflection_prompt)
```

#### Phase 4: 记忆维护（Memory Maintenance）

```python
def memory_maintenance(memory_store, current_date):
    """定期记忆维护：压缩、归档、清理"""

    # 1. 过期日摘要 → 压缩为周摘要
    expired_digests = memory_store.get_expired_digests(current_date)
    if len(expired_digests) >= 5:  # 攒够一周的量
        weekly_summary = compress_to_weekly(expired_digests)
        memory_store.save_block(weekly_summary)
        memory_store.delete_blocks(expired_digests)

    # 2. 过期周摘要 → 压缩为月摘要（可选）
    old_weeklies = memory_store.get_blocks(
        category="archival",
        subcategory="weekly_summaries",
        older_than=days(90)
    )
    if len(old_weeklies) >= 4:
        monthly_summary = compress_to_monthly(old_weeklies)
        memory_store.save_block(monthly_summary)
        memory_store.delete_blocks(old_weeklies)

    # 3. 更新记忆索引
    memory_store.rebuild_index()
```

### 3.3 记忆压缩策略

记忆压缩是跨天记忆系统的核心难点。借鉴 Letta-Code 的层级组织思想：

```
原始日摘要（2000字/天）
    ↓ 过期后（超出滚动窗口）
周摘要（1500字/周，由 LLM 压缩 5 天内容）
    ↓ 90 天后
月摘要（1000字/月，由 LLM 压缩 4 周内容）
    ↓ 1 年后
季度摘要（800字/季度）
```

**压缩时的保留优先级：**
1. 重大政策变化（降准降息、产业政策）
2. 交易教训（亏损原因、错误判断）
3. 市场状态转换点（牛转熊、熊转牛）
4. 重要财报数据（超预期/低于预期）
5. 情绪极端值（恐慌/狂热）
6. 日常市场波动（最先被压缩掉）

---

## 四、数据源集成设计

### 4.1 新增数据源架构

```
StockM1/
├── Agents_Experience/
│   ├── data_sources/                 # 新增：数据源模块
│   │   ├── __init__.py
│   │   ├── base_source.py            # 数据源基类
│   │   ├── news_provider.py          # 新闻数据提供者
│   │   ├── earnings_provider.py      # 财报数据提供者
│   │   ├── economic_provider.py      # 宏观经济数据提供者
│   │   ├── sentiment_analyzer.py     # 情绪分析器
│   │   └── data_aggregator.py        # 数据聚合器
│   │
│   ├── memory/                       # 新增：记忆系统模块
│   │   ├── __init__.py
│   │   ├── memory_block.py           # MemoryBlock 数据结构
│   │   ├── memory_store.py           # 记忆存储引擎（SQLite）
│   │   ├── memory_manager.py         # 记忆生命周期管理
│   │   ├── memory_compressor.py      # 记忆压缩器（LLM 驱动）
│   │   ├── memory_retriever.py       # 记忆检索器
│   │   ├── memory_prompt_builder.py  # 记忆 → Prompt 组装器
│   │   └── templates/                # 记忆块模板
│   │       ├── agent_identity.md
│   │       ├── market_regime.md
│   │       ├── portfolio_state.md
│   │       ├── active_watchlist.md
│   │       └── daily_digest_template.md
│   │
│   └── ...（现有文件）
```

### 4.2 数据源接口定义

```python
class BaseDataSource(ABC):
    """数据源基类"""

    @abstractmethod
    def get_data(self, date: str, stock_codes: list[str] = None) -> dict:
        """获取指定日期的数据"""
        pass

    @abstractmethod
    def format_for_prompt(self, data: dict) -> str:
        """将数据格式化为适合注入 prompt 的文本"""
        pass


class NewsProvider(BaseDataSource):
    """新闻数据提供者"""

    def get_data(self, date: str, stock_codes=None) -> dict:
        """
        获取当日新闻，分类返回：
        - macro_news: 宏观经济新闻（央行政策、GDP、CPI 等）
        - industry_news: 行业新闻（按申万一级行业分类）
        - stock_news: 个股新闻（关注列表中的股票）
        - global_news: 国际市场新闻（美股、港股、大宗商品）
        """
        pass

    def format_for_prompt(self, data: dict) -> str:
        """格式化为结构化新闻摘要，控制在 1500 字以内"""
        pass


class EarningsProvider(BaseDataSource):
    """财报数据提供者"""

    def get_data(self, date: str, stock_codes=None) -> dict:
        """
        获取当日发布的财报信息：
        - 营收、净利润、同比增长
        - 是否超预期/低于预期
        - 关键财务指标变化
        """
        pass


class EconomicProvider(BaseDataSource):
    """宏观经济数据提供者"""

    def get_data(self, date: str, stock_codes=None) -> dict:
        """
        获取当日发布的经济指标：
        - PMI、CPI、PPI、社融、M2
        - 央行公开市场操作
        - 利率变动
        """
        pass
```

### 4.3 数据聚合与 Token 预算管理

```python
class DataAggregator:
    """数据聚合器 - 控制注入 prompt 的总信息量"""

    TOKEN_BUDGET = {
        "core_memory": 4000,       # 核心记忆 ~4000 字符
        "rolling_memory": 6000,    # 滚动记忆 ~6000 字符（10天×600字摘要）
        "today_news": 1500,        # 今日新闻
        "today_earnings": 800,     # 今日财报
        "today_economic": 500,     # 今日经济数据
        "total_budget": 15000,     # 总预算 ~15000 字符
    }

    def aggregate(self, date, memory_store, data_sources) -> str:
        """
        聚合所有数据源，在 token 预算内组装完整的上下文。
        优先级: 核心记忆 > 近日摘要 > 今日新闻 > 财报 > 经济数据
        """
        pass
```

---

## 五、记忆存储引擎设计

### 5.1 SQLite 存储方案

在现有 `stock_data.db` 中新增记忆相关表：

```sql
-- 记忆块主表
CREATE TABLE memory_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT UNIQUE NOT NULL,          -- 唯一标识
    category TEXT NOT NULL,              -- core | rolling | archival | meta
    description TEXT,                    -- 用途描述
    value TEXT NOT NULL,                 -- 实际内容（Markdown）
    char_limit INTEGER DEFAULT 2000,    -- 字符上限
    read_only BOOLEAN DEFAULT FALSE,
    importance FLOAT DEFAULT 0.5,        -- 重要性 0.0-1.0
    tags TEXT,                           -- JSON 数组
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,                     -- 过期日期
    version INTEGER DEFAULT 1           -- 版本号
);

-- 记忆变更历史（借鉴 Letta-Code 的 Git 版本控制思想）
CREATE TABLE memory_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    block_label TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT NOT NULL,
    change_reason TEXT,                  -- 变更原因
    changed_at TEXT NOT NULL,
    trading_day TEXT NOT NULL,           -- 哪个交易日触发的变更
    FOREIGN KEY (block_label) REFERENCES memory_blocks(label)
);

-- 索引优化
CREATE INDEX idx_memory_category ON memory_blocks(category);
CREATE INDEX idx_memory_expires ON memory_blocks(expires_at);
CREATE INDEX idx_memory_tags ON memory_blocks(tags);
CREATE INDEX idx_history_label ON memory_history(block_label);
CREATE INDEX idx_history_day ON memory_history(trading_day);
```

### 5.2 MemoryStore 核心接口

```python
class MemoryStore:
    """记忆存储引擎"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_tables()

    # --- CRUD 操作 ---
    def save_block(self, block: MemoryBlock) -> None: ...
    def load_block(self, label: str) -> MemoryBlock | None: ...
    def update_block(self, label: str, value: str, reason: str = "") -> None: ...
    def delete_block(self, label: str) -> None: ...

    # --- 批量操作 ---
    def load_category(self, category: str) -> list[MemoryBlock]: ...
    def load_rolling_digests(self, current_date: str, window: int) -> list[MemoryBlock]: ...
    def get_expired_digests(self, current_date: str) -> list[MemoryBlock]: ...

    # --- 检索操作 ---
    def search(self, query: str, category: str = None,
               tags: list[str] = None, date_range: tuple = None) -> list[MemoryBlock]: ...

    # --- 维护操作 ---
    def rebuild_index(self) -> None: ...
    def get_history(self, label: str, limit: int = 10) -> list[dict]: ...
    def get_stats(self) -> dict: ...
```

---

## 六、Agent 增强设计

### 6.1 增强后的 System Prompt 结构

```python
MEMORY_ENHANCED_SYSTEM_PROMPT = """
你是一个专业的 A 股交易 Agent，具备跨天持久化记忆能力。

═══════════════════════════════════════════
📋 你的身份与策略框架（Core Memory: agent_identity）
═══════════════════════════════════════════
{agent_identity_block}

═══════════════════════════════════════════
📊 当前市场状态判断（Core Memory: market_regime）
═══════════════════════════════════════════
{market_regime_block}

═══════════════════════════════════════════
💼 当前持仓与策略（Core Memory: portfolio_state）
═══════════════════════════════════════════
{portfolio_state_block}

═══════════════════════════════════════════
👀 重点关注列表（Core Memory: active_watchlist）
═══════════════════════════════════════════
{active_watchlist_block}

═══════════════════════════════════════════
📅 近期交易日记忆（Rolling Memory: 近 {N} 个交易日）
═══════════════════════════════════════════
{rolling_digests}

═══════════════════════════════════════════
📈 市场情绪走势（Rolling Memory: sentiment_trend）
═══════════════════════════════════════════
{sentiment_trend_block}

═══════════════════════════════════════════
📰 今日市场信息（当日新输入，非记忆）
═══════════════════════════════════════════

## 今日新闻
{today_news}

## 今日财报
{today_earnings}

## 今日经济数据
{today_economic}

═══════════════════════════════════════════
🔧 可用工具
═══════════════════════════════════════════
你可以使用以下工具进行决策和记忆管理：

### 数据查询工具
- get_stock_history: 查询股票历史数据
- get_technical_indicators: 获取技术指标

### 交易工具
- buy_stock: 买入股票
- sell_stock: 卖出股票
- get_portfolio_status: 查看持仓状态

### 记忆工具（新增）
- recall_memory: 检索归档记忆（周摘要、财报记忆、事件记忆、交易教训）
- update_core_memory: 更新核心记忆块
- update_daily_digest: 生成/更新每日摘要

═══════════════════════════════════════════
📌 决策指引
═══════════════════════════════════════════

做出交易决策时，请遵循以下流程：

1. **回顾记忆**: 查看近期日摘要中的市场走势和情绪变化趋势
2. **分析新闻**: 评估今日新闻对持仓和关注股票的影响
3. **技术验证**: 用技术指标工具验证你的判断
4. **检索历史**: 如需要，用 recall_memory 检索相关历史事件或教训
5. **做出决策**: 综合以上信息，决定买入/卖出/持有
6. **记录理由**: 每笔交易必须说明决策依据，关联到具体的记忆或新闻

重要原则：
- 你的记忆是连续的，请利用近期趋势做出判断，而非仅看当天数据
- 如果近期情绪持续恶化，应考虑减仓；持续改善，可考虑加仓
- 重大事件（政策变化、财报超预期）应触发你主动更新核心记忆
- 交易教训是最宝贵的记忆，每次亏损都应记录原因
"""
```

### 6.2 新增记忆工具实现

```python
# Agents_Experience/core/memory_tools.py

def recall_memory(memory_store, query: str, category: str = None,
                  stock_code: str = None, date_range: str = None) -> str:
    """
    检索归档记忆。

    Args:
        query: 搜索关键词，如 "降准" "茅台财报" "止损教训"
        category: 限定搜索范围 (weekly_summaries|earnings_memory|event_memory|trade_lessons)
        stock_code: 限定股票代码
        date_range: 日期范围，格式 "2020-01-01:2020-01-15"

    Returns:
        匹配的记忆内容，按相关性排序，最多返回 3 条
    """
    tags = [stock_code] if stock_code else None
    dr = tuple(date_range.split(":")) if date_range else None

    results = memory_store.search(
        query=query, category=category, tags=tags, date_range=dr
    )

    if not results:
        return "未找到相关记忆。"

    output = []
    for block in results[:3]:
        output.append(f"### {block.label} (重要性: {block.importance})\n{block.value}")
    return "\n---\n".join(output)


def update_core_memory(memory_store, block_label: str,
                       action: str, content: str) -> str:
    """
    更新核心记忆块。

    Args:
        block_label: 记忆块标签 (agent_identity|market_regime|portfolio_state|active_watchlist)
        action: 操作类型 (update|append|replace)
        content: 新内容

    Returns:
        更新结果确认
    """
    block = memory_store.load_block(block_label)
    if not block:
        return f"记忆块 {block_label} 不存在"
    if block.read_only:
        return f"记忆块 {block_label} 为只读，无法修改"

    if action == "append":
        new_value = block.value + "\n" + content
    elif action == "replace":
        new_value = content
    elif action == "update":
        # 智能合并：由 LLM 决定如何整合新旧内容
        new_value = content  # 简化版，实际可用 LLM 合并
    else:
        return f"未知操作: {action}"

    # 截断到限制长度
    if len(new_value) > block.limit:
        new_value = new_value[:block.limit]

    memory_store.update_block(block_label, new_value, reason=f"{action}: {content[:50]}...")
    return f"已更新记忆块 {block_label}"


def update_daily_digest(memory_store, date: str, content: str,
                        sentiment_score: int, rolling_days: int = 20) -> str:
    """
    生成或更新每日摘要。

    Args:
        date: 交易日期
        content: 摘要内容（Markdown 格式）
        sentiment_score: 情绪评分 1-10
        rolling_days: 滚动窗口天数

    Returns:
        保存结果确认
    """
    from datetime import datetime, timedelta

    label = f"daily_digest/{date}"
    expires = (datetime.strptime(date, "%Y-%m-%d")
               + timedelta(days=rolling_days)).strftime("%Y-%m-%d")

    block = MemoryBlock(
        label=label,
        category="rolling",
        description=f"{date} 交易日全景摘要",
        value=content,
        limit=2000,
        read_only=False,
        created_at=date,
        updated_at=date,
        expires_at=expires,
        importance=0.5 + (abs(sentiment_score - 5) * 0.1),  # 极端情绪更重要
        tags=["daily", date],
    )

    memory_store.save_block(block)

    # 同步更新 sentiment_trend
    _update_sentiment_trend(memory_store, date, sentiment_score)

    return f"已保存 {date} 每日摘要，情绪评分: {sentiment_score}/10"
```

### 6.3 增强后的 Simulator 主循环

```python
# Agents_Experience/core/simulator_v2.py

class MemoryEnhancedSimulator:
    """带记忆系统的增强版交易模拟器"""

    def __init__(self, agent, memory_store, data_sources, config):
        self.agent = agent
        self.memory_store = memory_store
        self.data_sources = data_sources
        self.config = config
        self.prompt_builder = MemoryPromptBuilder(memory_store, data_sources)

    def run(self, start_date, end_date, stock_pool):
        """运行带记忆的交易模拟"""

        # 0. 初始化记忆（如果是首次运行）
        if not self.memory_store.load_block("agent_identity"):
            self._initialize_memory()

        trading_days = get_trading_days(start_date, end_date)

        for day in trading_days:
            logger.info(f"=== 交易日: {day} ===")

            # Phase 1: 开盘前 - 加载记忆 + 注入新数据
            system_prompt = self.prompt_builder.build(
                current_date=day,
                rolling_window=self.config.ROLLING_MEMORY_DAYS,
            )

            # Phase 2: 盘中 - Agent 决策（带记忆工具）
            self.agent.update_system_prompt(system_prompt)
            self.agent.update_portfolio_prices(day)
            decision = self.agent.make_decision(day)

            # 执行交易
            self._execute_trades(decision, day)

            # Phase 3: 收盘 - 触发反思
            self._trigger_reflection(day)

            # Phase 4: 记忆维护
            self._maintain_memory(day)

            # 记录快照
            self._take_snapshot(day)

    def _initialize_memory(self):
        """首次运行时初始化记忆块"""
        templates_dir = "Agents_Experience/memory/templates/"
        for template_file in os.listdir(templates_dir):
            block = load_template(os.path.join(templates_dir, template_file))
            self.memory_store.save_block(block)

    def _trigger_reflection(self, day):
        """触发收盘反思"""
        reflection_prompt = REFLECTION_PROMPT.format(date=day)
        self.agent.run_reflection(reflection_prompt)

    def _maintain_memory(self, day):
        """执行记忆维护"""
        memory_maintenance(self.memory_store, day)
```

---

## 七、记忆驱动的决策增强示例

### 7.1 场景：连续利好后的趋势判断

```
Agent 的记忆状态：
- sentiment_trend 显示近 5 天情绪从 4→5→6→7→8，持续回暖
- daily_digest 记录：连续 3 天北向资金净流入，累计超 150 亿
- market_regime 当前状态：震荡偏多

今日新闻：
- 央行再次降准 0.25 个百分点

Agent 的推理过程：
1. [查看记忆] 近 5 天情绪持续回暖，北向资金连续流入
2. [查看记忆] 上次降准（daily_digest/01-03）后市场涨了 3 天
3. [recall_memory] 搜索 "降准" → 找到历史记录：降准后通常有 1-2 周的上涨窗口
4. [技术验证] 上证指数站稳 5 日均线，MACD 金叉
5. [决策] 趋势确认，将仓位从 60% 提升到 80%
6. [更新记忆] market_regime → "趋势向上"，portfolio_state → 更新仓位逻辑
```

### 7.2 场景：财报季的记忆联动

```
Agent 的记忆状态：
- active_watchlist 中有 "sh600519 贵州茅台 - 等待四季度财报"
- earnings_memory 中记录了茅台三季度财报：营收同比+15%，超预期

今日新闻：
- 贵州茅台发布四季度财报：营收同比+18%，净利润同比+20%，超预期

Agent 的推理过程：
1. [查看记忆] watchlist 中茅台的触发条件是 "等待财报确认"
2. [recall_memory] 搜索 "茅台 财报" → 三季度也超预期，连续两季超预期
3. [查看记忆] portfolio_state 中已持有茅台 25%
4. [技术验证] 茅台当前价格在 20 日均线上方，成交量放大
5. [决策] 连续超预期 + 技术面配合 → 加仓至 35%
6. [更新记忆]
   - portfolio_state: 更新茅台仓位和逻辑
   - earnings_memory: 新增四季度财报记录
   - active_watchlist: 茅台状态改为 "持有观察"
```

### 7.3 场景：交易教训的记忆回溯

```
Agent 的记忆状态：
- trade_lessons 中记录了一条教训：
  "2020-03-15: 在恐慌性下跌中追卖 sh601318，次日反弹 5%。
   教训：系统性恐慌时不应追卖优质股，应等待企稳信号。"

当前情况：
- 市场连跌 3 天，sentiment_trend 从 7 降到 3
- 持有的中国平安跌了 8%

Agent 的推理过程：
1. [查看记忆] sentiment_trend 显示情绪急剧恶化
2. [recall_memory] 搜索 "恐慌 追卖" → 找到 03-15 的教训
3. [查看记忆] 03-15 的教训明确说 "不应追卖优质股"
4. [技术验证] 中国平安 RSI 已到 25（超卖区域）
5. [决策] 吸取教训，不追卖，等待企稳信号
6. [更新记忆] daily_digest 记录 "参考 03-15 教训，选择持有等待"
```

---

## 八、配置参数设计

### 8.1 记忆系统配置

```python
# Agents_Experience/config.py 新增配置

# === 记忆系统配置 ===
MEMORY_CONFIG = {
    # 滚动记忆窗口
    "rolling_memory_days": 10,          # 保留最近 10 个交易日的日摘要

    # 记忆块容量限制（字符数）
    "block_limits": {
        "agent_identity": 3000,
        "market_regime": 2000,
        "portfolio_state": 3000,
        "active_watchlist": 2000,
        "daily_digest": 2000,
        "sentiment_trend": 2000,
        "weekly_summary": 1500,
        "monthly_summary": 1000,
        "earnings_memory_item": 800,
        "event_memory_item": 800,
        "trade_lesson_item": 500,
    },

    # Token 预算分配
    "token_budget": {
        "core_memory": 4000,
        "rolling_memory": 6000,
        "today_data": 3000,
        "system_instructions": 2000,
        "total": 15000,
    },

    # 记忆压缩策略
    "compression": {
        "daily_to_weekly_threshold": 5,   # 攒够 5 天压缩为周摘要
        "weekly_to_monthly_age": 90,      # 90 天后周摘要压缩为月摘要
        "max_archival_items": 200,        # 归档记忆最大条目数
    },

    # 记忆反思配置
    "reflection": {
        "enabled": True,
        "trigger": "end_of_day",          # 每日收盘后触发
        "max_reflection_tokens": 1000,    # 反思输出上限
    },

    # 情绪分析
    "sentiment": {
        "scale": 10,                      # 1-10 分制
        "extreme_threshold": [2, 8],      # 极端情绪阈值
        "trend_window": 10,               # 情绪趋势观察窗口
    },
}
```

---

## 九、实施路线图

### Phase 1: 基础设施（记忆存储与数据结构）

**目标**: 建立记忆存储引擎和基础数据结构

**任务清单**:
1. 实现 `MemoryBlock` 数据类
2. 实现 `MemoryStore`（SQLite 存储引擎）
3. 创建记忆表结构和索引
4. 实现记忆块 CRUD 操作
5. 实现记忆变更历史记录
6. 编写记忆块模板文件（agent_identity.md 等）
7. 编写单元测试

**产出文件**:
- `Agents_Experience/memory/memory_block.py`
- `Agents_Experience/memory/memory_store.py`
- `Agents_Experience/memory/templates/*.md`

### Phase 2: 记忆生命周期管理

**目标**: 实现记忆的加载、更新、压缩、归档全流程

**任务清单**:
1. 实现 `MemoryManager`（生命周期管理器）
2. 实现 `MemoryCompressor`（LLM 驱动的记忆压缩）
3. 实现 `MemoryRetriever`（记忆检索，支持关键词/标签/日期范围）
4. 实现 `MemoryPromptBuilder`（记忆 → Prompt 组装）
5. 实现收盘反思流程
6. 实现记忆维护定时任务（压缩、归档、清理）

**产出文件**:
- `Agents_Experience/memory/memory_manager.py`
- `Agents_Experience/memory/memory_compressor.py`
- `Agents_Experience/memory/memory_retriever.py`
- `Agents_Experience/memory/memory_prompt_builder.py`

### Phase 3: 数据源集成

**目标**: 接入新闻、财报、经济数据源

**任务清单**:
1. 实现 `BaseDataSource` 抽象基类
2. 实现 `NewsProvider`（可先用模拟数据，后接真实 API）
3. 实现 `EarningsProvider`
4. 实现 `EconomicProvider`
5. 实现 `SentimentAnalyzer`（基于新闻的情绪评分）
6. 实现 `DataAggregator`（数据聚合与 Token 预算管理）

**产出文件**:
- `Agents_Experience/data_sources/*.py`

### Phase 4: Agent 增强与集成

**目标**: 将记忆系统集成到现有 Agent 和 Simulator 中

**任务清单**:
1. 新增记忆工具（recall_memory, update_core_memory, update_daily_digest）
2. 重构 `system_prompt.py`，支持记忆注入
3. 增强 `qwen_agent.py`，集成记忆工具调用
4. 实现 `MemoryEnhancedSimulator`
5. 更新 `run_mvp.py` 入口脚本
6. 更新配置文件

**产出文件**:
- `Agents_Experience/core/memory_tools.py`
- `Agents_Experience/core/simulator_v2.py`
- `Agents_Experience/prompts/system_prompt_v2.py`
- `Agents_Experience/run_memory_mvp.py`

### Phase 5: 测试与优化

**目标**: 验证记忆系统效果，对比有无记忆的表现差异

**任务清单**:
1. 设计对比实验：无记忆 vs 有记忆，相同时间段、相同股票池
2. 运行回测，收集性能指标
3. 分析记忆对决策质量的影响
4. 优化记忆窗口大小、压缩策略、Token 预算分配
5. 调优 prompt 模板
6. 编写实验报告

---

## 十、关键设计决策与权衡

### 10.1 为什么选择 SQLite 而非文件系统？

| 方案 | 优点 | 缺点 |
|------|------|------|
| Git + 文件系统（Letta 方案）| 版本控制天然支持，可视化好 | 对 StockM1 过重，不需要协作 |
| SQLite | 与现有架构一致，查询灵活，事务安全 | 不如文件直观 |
| JSON 文件 | 简单直接 | 并发不安全，查询困难 |

**选择 SQLite**：StockM1 已使用 SQLite，保持技术栈一致；记忆检索需要灵活查询；通过 `memory_history` 表实现版本追溯。

### 10.2 滚动窗口大小的选择

- **5 天**: 太短，无法捕捉周级别趋势
- **10 天（推荐）**: 覆盖两周交易日，平衡信息量与 Token 消耗
- **20 天**: 信息量大但 Token 消耗高，适合大上下文模型
- **可配置**: 通过 `ROLLING_MEMORY_DAYS` 参数调整

### 10.3 记忆更新的主动权

借鉴 Letta-Code 的设计哲学：**让 Agent 自主管理记忆**。

- Agent 决定哪些信息值得记住
- Agent 决定何时更新核心记忆
- Agent 在反思阶段自主生成日摘要
- 系统只负责：加载记忆到 prompt、执行过期清理、压缩归档

这样做的好处是 Agent 的记忆更有针对性，而非机械地记录所有信息。

### 10.4 记忆与幻觉风险

风险：Agent 可能基于错误的记忆做出决策。

缓解措施：
- 核心记忆有字符上限，防止无限膨胀
- 记忆变更有历史记录，可追溯
- 日摘要有固定模板，结构化降低幻觉
- 情绪评分用数字量化，减少主观偏差
- 归档记忆检索返回原始内容，而非 Agent 的回忆

---

## 十一、预期效果

### 有记忆 vs 无记忆的对比

| 维度 | 无记忆（当前） | 有记忆（增强后） |
|------|---------------|-----------------|
| 趋势判断 | 仅看当天数据+历史K线 | 结合近10天市场情绪走势 |
| 新闻利用 | 无法利用新闻 | 新闻驱动决策+历史新闻对比 |
| 财报反应 | 无法感知财报 | 跟踪连续季度财报趋势 |
| 交易纪律 | 每天重新开始 | 记住教训，避免重复犯错 |
| 仓位管理 | 缺乏连续性 | 基于市场状态动态调整 |
| 决策解释 | 仅基于技术指标 | 多维度推理，可追溯决策链 |
