"""
AI Agent配置管理模块
处理user_setting.ini的读写和.env的加载
"""
import os
import configparser
from typing import Dict, Any, List
from dotenv import load_dotenv


class AgentConfigManager:
    """Agent配置管理器"""
    
    def __init__(self):
        """初始化配置管理器"""
        # 配置文件路径
        self.agents_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), 
            'Agents_Experience'
        )
        self.config_file = os.path.join(self.agents_dir, 'user_setting.ini')
        self.env_file = os.path.join(self.agents_dir, '.env')
        
        # 加载.env文件
        load_dotenv(self.env_file)
        
        # 初始化配置解析器（使用RawConfigParser避免插值语法问题）
        self.config = configparser.RawConfigParser()
        
    def get_default_config(self) -> Dict[str, Any]:
        """
        从.env文件获取默认配置
        
        Returns:
            默认配置字典
        """
        # 获取数据库中实际有数据的股票
        available_stocks = self.get_available_stock_pool()
        default_stock_pool = available_stocks[:10] if len(available_stocks) >= 10 else available_stocks
        
        return {
            # API配置
            'api_base': os.getenv('QWEN_API_BASE', 'https://api.suanli.cn/v1'),
            'api_key': os.getenv('QWEN_API_KEY', ''),
            'model': os.getenv('QWEN_MODEL', 'free:Qwen3-30B-A3B'),
            'api_call_interval': float(os.getenv('API_CALL_INTERVAL', '2')),
            
            # 模型参数
            'temperature': 1.0,
            'max_tokens': 2000,
            
            # 系统提示词
            'system_prompt': self._get_default_prompt(),
            
            # 交易配置
            'initial_capital': 1000000,
            'stock_pool': default_stock_pool,
            
            # 时间配置
            'start_date': '2020-01-02',
            'end_date': '2020-12-31',
            
            # 历史窗口
            'history_window_days': 60
        }
    
    def _get_default_prompt(self) -> str:
        """获取默认系统提示词（与system_prompt.py一致）"""
        return """你是一位专业的股票投资顾问AI助手，负责管理一个100万人民币的股票账户。

## 你的角色
- 你需要基于市场数据和技术分析做出买入/卖出决策
- 你的目标是**积极寻找投资机会**，在控制风险的前提下获得投资收益
- 你必须遵守T+1交易规则（今天买入的股票明天才能卖出）

## ⚠️ 核心投资理念

**资金闲置是最大的浪费！** 100万资金如果长期不投资，等于0收益。
- 合理的仓位应该在 **30%-80%** 之间
- 如果当前仓位低于30%，应该**积极寻找买入机会**
- 不要因为追求"完美时机"而错过所有机会

## 工作流程（必须严格遵守！）

### 步骤1：查看持仓
**强制要求**：必须调用 `get_portfolio` 工具查看当前持仓
- 重点关注：当前仓位比例（持仓市值/总资产）
- **如果仓位 < 30%，今天必须认真考虑买入！**

### 步骤2：数据收集
对股票池中多只股票（建议至少分析5-6只），必须调用：
- `get_stock_history`: 查看K线走势
- `get_technical_indicators`: 查看技术指标

### 步骤3：执行交易（必须调用工具！）

**选项A：买入股票** 🎯 **低仓位时优先考虑！**

**买入条件（满足任一即可考虑买入）**：
- ✅ RSI < 50 且处于上升趋势
- ✅ RSI 在 30-50 区间（相对低位）
- ✅ MACD金叉
- ✅ 价格站上MA5均线且MA5向上
- ✅ 近期连续下跌后企稳
- ✅ 成交量放大且价格上涨

**买入行动**：
- **必须调用 buy_stock(symbol="代码", quantity=数量)**
- 数量建议：总资产的10%-25%，必须是100的整数倍

**特别提醒**：
- 如果当前仓位 < 30%，应该降低买入门槛，积极建仓
- 不要等待"完美信号"，满足1-2个条件即可考虑买入

**选项B：卖出股票（持仓且出现卖出信号时）**
- **强制卖出条件（满足任一即卖）**：
  * 亏损达到 -8% 或以上（止损）
  * 盈利达到 15% 或以上（止盈）
  * RSI > 75（极度超买）
  * MACD死叉且盈利
- 行动：**必须调用 sell_stock(symbol="代码", quantity=数量)**

**选项C：持有观望**
- 条件：已有合理仓位（30%-80%）且无明确买卖信号
- 行动：不调用交易工具，直接进入步骤4

🚨 关键提醒：
- **低仓位时必须积极买入**：仓位<30%时，不能连续多天观望
- **止损止盈是铁律**：达到止损/止盈线必须执行
- **禁止在文字中说"已买入"或"已卖出"而不调用工具**

### 步骤4：文字总结
在调用完交易工具后（或确认不交易），用文字总结。

## 仓位管理指南

| 当前仓位 | 建议操作 |
|----------|----------|
| 0-30% | **必须积极买入！** |
| 30-50% | 继续寻找机会，适度加仓 |
| 50-70% | 正常仓位，精选个股 |
| 70-80% | 谨慎加仓 |
| >80% | 不再买入，等待卖出机会 |

## 决策原则
1. **积极投资**: 资金闲置=零收益，要主动寻找机会
2. **风险控制**: 单只股票仓位不超过总资产的30%
3. **分散投资**: 持有3-5只不同板块的股票
4. **严格止损止盈**: 亏损-8%止损，盈利15%止盈
5. **不追求完美**: 满足部分条件即可行动

## 输出格式
完成工具调用后，请按以下格式总结：

**当前仓位**：XX%

**分析**：
[简要分析市场情况、技术指标]

**决策**：
[说明今天的操作]

**理由**：
[解释决策依据]

## 重要提醒
- 买入数量必须是100的整数倍（1手=100股）
- **必须通过调用buy_stock/sell_stock工具来执行交易**
- **仓位过低时必须积极买入，不要连续观望超过5天！**"""
    
    def load_config(self) -> Dict[str, Any]:
        """
        加载用户配置，如果不存在则返回默认配置
        
        Returns:
            配置字典
        """
        # 先获取默认配置
        config = self.get_default_config()
        
        # 如果配置文件存在，则加载覆盖
        if os.path.exists(self.config_file):
            try:
                self.config.read(self.config_file, encoding='utf-8')
                
                # API配置
                if self.config.has_section('API'):
                    config['api_base'] = self.config.get('API', 'api_base', fallback=config['api_base'])
                    config['api_key'] = self.config.get('API', 'api_key', fallback=config['api_key'])
                    config['model'] = self.config.get('API', 'model', fallback=config['model'])
                    config['api_call_interval'] = self.config.getfloat('API', 'api_call_interval', 
                                                                       fallback=config['api_call_interval'])
                
                # 模型参数
                if self.config.has_section('Model'):
                    config['temperature'] = self.config.getfloat('Model', 'temperature', 
                                                                 fallback=config['temperature'])
                    config['max_tokens'] = self.config.getint('Model', 'max_tokens', 
                                                             fallback=config['max_tokens'])
                
                # 系统提示词
                if self.config.has_section('Prompt'):
                    config['system_prompt'] = self.config.get('Prompt', 'system_prompt', 
                                                             fallback=config['system_prompt'])
                
                # 交易配置
                if self.config.has_section('Trading'):
                    config['initial_capital'] = self.config.getfloat('Trading', 'initial_capital', 
                                                                     fallback=config['initial_capital'])
                    stock_pool_str = self.config.get('Trading', 'stock_pool', fallback='')
                    if stock_pool_str:
                        config['stock_pool'] = [s.strip() for s in stock_pool_str.split(',')]
                    config['history_window_days'] = self.config.getint('Trading', 'history_window_days',
                                                                       fallback=config['history_window_days'])
                
                # 时间配置
                if self.config.has_section('Time'):
                    config['start_date'] = self.config.get('Time', 'start_date', 
                                                          fallback=config['start_date'])
                    config['end_date'] = self.config.get('Time', 'end_date', 
                                                        fallback=config['end_date'])
                    
            except Exception as e:
                print(f"加载配置文件失败: {e}")
                # 出错时使用默认配置
                pass
        
        return config
    
    def save_config(self, config: Dict[str, Any]) -> tuple[bool, str]:
        """
        保存配置到文件
        
        Args:
            config: 配置字典
            
        Returns:
            (是否保存成功, 错误信息)
        """
        try:
            # 创建新的配置对象（使用RawConfigParser避免插值语法问题）
            new_config = configparser.RawConfigParser()
            
            # API配置
            new_config.add_section('API')
            new_config.set('API', 'api_base', str(config.get('api_base', '')))
            new_config.set('API', 'api_key', str(config.get('api_key', '')))
            new_config.set('API', 'model', str(config.get('model', '')))
            new_config.set('API', 'api_call_interval', str(config.get('api_call_interval', 2)))
            
            # 模型参数
            new_config.add_section('Model')
            new_config.set('Model', 'temperature', str(config.get('temperature', 1.0)))
            new_config.set('Model', 'max_tokens', str(config.get('max_tokens', 2000)))
            
            # 系统提示词
            new_config.add_section('Prompt')
            new_config.set('Prompt', 'system_prompt', str(config.get('system_prompt', '')))
            
            # 交易配置
            new_config.add_section('Trading')
            new_config.set('Trading', 'initial_capital', str(config.get('initial_capital', 1000000)))
            stock_pool = config.get('stock_pool', [])
            if isinstance(stock_pool, list):
                new_config.set('Trading', 'stock_pool', ','.join(stock_pool))
            else:
                new_config.set('Trading', 'stock_pool', str(stock_pool))
            new_config.set('Trading', 'history_window_days', str(config.get('history_window_days', 60)))
            
            # 时间配置
            new_config.add_section('Time')
            new_config.set('Time', 'start_date', str(config.get('start_date', '')))
            new_config.set('Time', 'end_date', str(config.get('end_date', '')))
            
            # 确保目录存在
            os.makedirs(self.agents_dir, exist_ok=True)
            
            # 写入文件
            with open(self.config_file, 'w', encoding='utf-8') as f:
                new_config.write(f)
            
            print(f"配置已成功保存到: {self.config_file}")
            return True, ""
            
        except Exception as e:
            error_msg = f"保存配置失败: {str(e)}"
            print(error_msg)
            import traceback
            traceback.print_exc()
            return False, error_msg
    
    def get_available_stock_pool(self) -> List[str]:
        """
        获取可用的股票池（从数据库查询有数据的股票）
        
        Returns:
            股票代码列表
        """
        try:
            import sqlite3
            from config import DATABASE_PATH
            
            conn = sqlite3.connect(DATABASE_PATH)
            cursor = conn.cursor()
            
            # 查询有数据的股票，按数据量排序
            cursor.execute('''
                SELECT symbol, COUNT(*) as cnt 
                FROM stock_daily 
                GROUP BY symbol 
                HAVING cnt > 100
                ORDER BY cnt DESC
                LIMIT 50
            ''')
            
            stocks = [row[0] for row in cursor.fetchall()]
            conn.close()
            
            if stocks:
                return stocks
        except Exception as e:
            print(f"从数据库获取股票池失败: {e}")
        
        # 如果数据库查询失败，返回默认股票池
        return [
            '000001',  # 平安银行
            '000002',  # 万科A
            '600036',  # 招商银行
            '000858',  # 五粮液
            '000333',  # 美的集团
        ]
