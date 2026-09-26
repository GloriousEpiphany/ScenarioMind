"""Universe membership is a research qualification, never a buy instruction."""

from dataclasses import dataclass
from datetime import datetime

from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation


@dataclass(frozen=True)
class UniverseMember:
    symbol: str
    market: str
    is_holding: bool
    thesis_artifact_id: str | None

    def __post_init__(self):
        if not self.symbol or not self.market or not isinstance(self.is_holding, bool):
            raise ContractViolation("universe member requires symbol, market and holding classification")
        if not self.is_holding and not self.thesis_artifact_id:
            raise ContractViolation("new candidate requires a validated company thesis")


@dataclass(frozen=True)
class InvestmentUniverse:
    universe_id: str
    portfolio_id: str
    portfolio_snapshot_artifact_id: str
    as_of: datetime
    expires_at: datetime
    policy_version: str
    members: tuple[UniverseMember, ...]
    source_artifact_ids: tuple[str, ...]
    exclusions: tuple[tuple[str, str], ...]

    def __post_init__(self):
        require_aware(self.as_of, "universe.as_of")
        require_aware(self.expires_at, "universe.expires_at")
        if self.expires_at <= self.as_of:
            raise ContractViolation("universe expiry must follow its research cutoff")
        if not all((self.universe_id, self.portfolio_id, self.portfolio_snapshot_artifact_id, self.policy_version, self.members, self.source_artifact_ids)):
            raise ContractViolation("universe requires identifiers, members and source artifacts")
        if len(set(self.symbols)) != len(self.members):
            raise ContractViolation("universe contains duplicate symbols")
        if set(self.symbols).intersection(symbol for symbol, _ in self.exclusions):
            raise ContractViolation("universe cannot include and exclude the same security")

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(item.symbol for item in self.members)

    def expand_portfolio(self, portfolio, as_of):
        from scenario_engine.mission.contracts import ResolvedPortfolio

        require_aware(as_of, "mission.as_of")
        if not self.as_of <= as_of < self.expires_at:
            raise ContractViolation("investment universe is future-dated or expired")
        if portfolio.portfolio_id != self.portfolio_id:
            raise ContractViolation("investment universe refers to a different account snapshot")
        if portfolio.snapshot_artifact_id != self.portfolio_snapshot_artifact_id:
            raise ContractViolation("investment universe refers to a different account snapshot artifact")
        holdings = {item.symbol for item in self.members if item.is_holding}
        if holdings != set(portfolio.symbols):
            raise ContractViolation("investment universe must preserve every current holding")
        return ResolvedPortfolio(
            portfolio.portfolio_id, self.symbols,
            {symbol: portfolio.weights[symbol] if symbol in holdings else 0.0 for symbol in self.symbols},
            snapshot_artifact_id=portfolio.snapshot_artifact_id,
        )
