"""
持仓管理模块
"""
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class Position:
    """持仓信息"""
    symbol: str
    name: str
    quantity: int
    avg_cost: float
    current_price: float = 0.0
    lots: list[dict] = field(default_factory=list)
    
    @property
    def market_value(self) -> float:
        """市值"""
        return self.quantity * self.current_price
    
    @property
    def cost_value(self) -> float:
        """成本"""
        return self.quantity * self.avg_cost
    
    @property
    def profit(self) -> float:
        """盈亏金额"""
        return self.market_value - self.cost_value
    
    @property
    def profit_rate(self) -> float:
        """盈亏比例"""
        if self.cost_value == 0:
            return 0.0
        return (self.profit / self.cost_value) * 100

    def sellable_quantity(self, current_date: str | None = None) -> int:
        """Quantity allowed to sell under A-share T+1."""
        if current_date is None or not self.lots:
            return self.quantity
        return sum(int(lot["quantity"]) for lot in self.lots if str(lot["buy_date"]) < current_date)


class Portfolio:
    """投资组合管理"""
    
    def __init__(self, account_name: str, initial_capital: float):
        self.account_name = account_name
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions: Dict[str, Position] = {}
        self.current_date = None
    
    def add_position(self, symbol: str, name: str, quantity: int, price: float, trade_date: str | None = None):
        """添加持仓"""
        if symbol in self.positions:
            # 更新持仓
            pos = self.positions[symbol]
            total_cost = pos.cost_value + (quantity * price)
            total_quantity = pos.quantity + quantity
            pos.avg_cost = total_cost / total_quantity
            pos.quantity = total_quantity
            pos.current_price = price
            pos.lots.append({"buy_date": trade_date or self.current_date or "", "quantity": quantity})
        else:
            # 新建持仓
            self.positions[symbol] = Position(
                symbol=symbol,
                name=name,
                quantity=quantity,
                avg_cost=price,
                current_price=price,
                lots=[{"buy_date": trade_date or self.current_date or "", "quantity": quantity}]
            )
    
    def reduce_position(self, symbol: str, quantity: int, current_date: str | None = None) -> bool:
        """减少持仓"""
        if symbol not in self.positions:
            return False
        
        pos = self.positions[symbol]
        if pos.quantity < quantity:
            return False
        if pos.sellable_quantity(current_date) < quantity:
            return False
        
        pos.quantity -= quantity
        remaining = quantity
        if pos.lots:
            updated_lots = []
            for lot in pos.lots:
                lot_qty = int(lot["quantity"])
                is_sellable = current_date is None or str(lot["buy_date"]) < current_date
                if is_sellable and remaining > 0:
                    used = min(lot_qty, remaining)
                    lot_qty -= used
                    remaining -= used
                if lot_qty > 0:
                    updated_lots.append({"buy_date": lot["buy_date"], "quantity": lot_qty})
            pos.lots = updated_lots
        if pos.quantity == 0:
            del self.positions[symbol]
        
        return True

    def get_sellable_quantity(self, symbol: str, current_date: str | None = None) -> int:
        position = self.get_position(symbol)
        if not position:
            return 0
        return position.sellable_quantity(current_date or self.current_date)
    
    def update_price(self, symbol: str, price: float):
        """更新持仓价格"""
        if symbol in self.positions:
            self.positions[symbol].current_price = price
    
    def get_position(self, symbol: str) -> Optional[Position]:
        """获取持仓"""
        return self.positions.get(symbol)
    
    @property
    def total_market_value(self) -> float:
        """总市值"""
        return sum(pos.market_value for pos in self.positions.values())
    
    @property
    def total_asset(self) -> float:
        """总资产"""
        return self.cash + self.total_market_value
    
    @property
    def total_profit(self) -> float:
        """总盈亏"""
        return self.total_asset - self.initial_capital
    
    @property
    def total_profit_rate(self) -> float:
        """总盈亏比例"""
        if self.initial_capital == 0:
            return 0.0
        return (self.total_profit / self.initial_capital) * 100
    
    def get_summary(self) -> dict:
        """获取账户摘要"""
        return {
            'account_name': self.account_name,
            'current_date': self.current_date,
            'initial_capital': self.initial_capital,
            'cash': self.cash,
            'market_value': self.total_market_value,
            'total_asset': self.total_asset,
            'total_profit': self.total_profit,
            'total_profit_rate': self.total_profit_rate,
            'position_count': len(self.positions)
        }
