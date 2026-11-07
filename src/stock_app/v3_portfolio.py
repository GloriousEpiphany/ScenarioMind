"""Capture a mutable StockM1 account as an immutable V3 portfolio snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
from math import isfinite
import re
from typing import Callable

from scenario_engine.data import (
    DataRequest,
    MarketArtifact,
    MarketArtifactRepository,
    canonical_payload_hash,
)
from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, DataUnavailable


SOURCE_ID = "stockm1_account_store"
ADAPTER_VERSION = "stockm1-current-portfolio/1.0.0"
SCHEMA_VERSION = "portfolio-snapshot-v1"


@dataclass(frozen=True)
class AccountSummary:
    account_id: int
    name: str
    cash: float
    state_date: str
    position_count: int
    position_updated_at: str | None


@dataclass(frozen=True)
class PositionValuation:
    symbol: str
    name: str | None
    quantity: int
    average_cost: float
    source_updated_at: str
    price: float | None
    price_event_time: datetime | None
    price_artifact_id: str | None

    @property
    def market_value(self) -> float | None:
        if self.price is None:
            return None
        return self.quantity * self.price


@dataclass(frozen=True)
class PortfolioPreview:
    account: AccountSummary
    known_at: datetime
    positions: tuple[PositionValuation, ...]
    missing_price_symbols: tuple[str, ...]
    capture_fingerprint: str

    @property
    def publishable(self) -> bool:
        return bool(self.positions) and not self.missing_price_symbols

    @property
    def market_value(self) -> float | None:
        if not self.publishable:
            return None
        return sum(float(position.market_value) for position in self.positions)

    @property
    def total_value(self) -> float | None:
        market_value = self.market_value
        if market_value is None:
            return None
        return self.account.cash + market_value


@dataclass(frozen=True)
class PublishedPortfolioSnapshot:
    artifact_id: str
    portfolio_ref: str
    known_at: datetime
    source_account_id: int | None
    source_account_name: str | None
    position_count: int
    cash: float
    total_value: float
    capture_fingerprint: str | None


def automatically_selected_account_id(accounts: tuple[AccountSummary, ...]) -> int | None:
    """Select only when the mutable account state is unambiguous."""
    active = tuple(account for account in accounts if account.position_count > 0)
    if len(active) == 1:
        return active[0].account_id
    if not active and len(accounts) == 1:
        return accounts[0].account_id
    return None


class CurrentPortfolioSnapshotModule:
    """Deep Module for account discovery, V3 valuation, and atomic publication.

    Its Interface never exposes mutable ``positions`` as a mission input.  A
    mission can consume the account only after this Module has valued every
    position with a visible V3 price Artifact and published one immutable,
    reconciling ``portfolio/portfolio_snapshot`` Artifact.
    """

    def __init__(
        self,
        connection,
        *,
        clock: Callable[[], datetime] | None = None,
        price_source_id: str | None = None,
        price_artifact_ids: tuple[str, ...] | None = None,
    ) -> None:
        self.connection = connection
        self.repository = MarketArtifactRepository(connection)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if price_artifact_ids is not None and (not price_source_id or not price_artifact_ids
                or len(set(price_artifact_ids)) != len(price_artifact_ids)):
            raise ContractViolation("frozen portfolio prices require a source and unique artifact IDs")
        self.price_source_id = price_source_id
        self.price_artifact_ids = price_artifact_ids

    def discover_accounts(self) -> tuple[AccountSummary, ...]:
        rows = self.connection.execute(
            """
            SELECT a.id, a.name, a.current_capital, a.current_date,
                   COUNT(p.symbol), MAX(p.updated_at)
            FROM account AS a
            LEFT JOIN positions AS p ON p.account_id = a.id
            GROUP BY a.id, a.name, a.current_capital, a.current_date
            ORDER BY a.id
            """
        ).fetchall()
        return tuple(self._account_from_row(row) for row in rows)

    def preview(self, account_id: int, *, known_at: datetime | None = None) -> PortfolioPreview:
        cutoff = require_aware(known_at or self.clock(), "known_at")
        return self._read_preview(account_id, cutoff)

    def current_snapshot(
        self,
        portfolio_ref: str = "current",
        *,
        known_at: datetime | None = None,
    ) -> PublishedPortfolioSnapshot | None:
        cutoff = require_aware(known_at or self.clock(), "known_at")
        request = DataRequest(
            domain="portfolio",
            dataset="portfolio_snapshot",
            entities=(self._require_portfolio_ref(portfolio_ref),),
            start=cutoff - timedelta(days=36500),
            end=cutoff,
            known_at=cutoff,
            strict_pit=True,
        )
        artifacts = self.repository.query(request)
        if not artifacts:
            return None
        artifact = max(artifacts, key=lambda item: (item.event_time, item.known_at))
        payload = artifact.payload
        required = {"cash", "total_value", "positions"}
        missing = required.difference(payload)
        if missing:
            raise ContractViolation(
                f"portfolio artifact {artifact.artifact_id} missing {sorted(missing)}"
            )
        positions = payload["positions"]
        if not isinstance(positions, list) or not positions:
            raise ContractViolation("published current portfolio positions must be a non-empty list")
        account_id = payload.get("source_account_id")
        if account_id is not None and type(account_id) is not int:
            raise ContractViolation("portfolio source_account_id must be an integer")
        account_name = payload.get("source_account_name")
        if account_name is not None and not isinstance(account_name, str):
            raise ContractViolation("portfolio source_account_name must be a string")
        fingerprint = payload.get("capture_fingerprint")
        if fingerprint is not None and (
            not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
        ):
            raise ContractViolation("portfolio capture_fingerprint must be a SHA-256 digest")
        cash = self._finite_number(payload["cash"], "portfolio cash", minimum=0.0)
        total_value = self._finite_number(payload["total_value"], "portfolio total_value", positive=True)
        return PublishedPortfolioSnapshot(
            artifact_id=artifact.artifact_id,
            portfolio_ref=portfolio_ref,
            known_at=artifact.known_at,
            source_account_id=account_id,
            source_account_name=account_name,
            position_count=len(positions),
            cash=cash,
            total_value=total_value,
            capture_fingerprint=fingerprint,
        )

    @staticmethod
    def snapshot_matches(
        preview: PortfolioPreview,
        snapshot: PublishedPortfolioSnapshot | None,
    ) -> bool:
        return bool(
            snapshot is not None
            and snapshot.source_account_id == preview.account.account_id
            and snapshot.capture_fingerprint == preview.capture_fingerprint
        )

    def publish(
        self,
        account_id: int,
        *,
        portfolio_ref: str = "current",
    ) -> PublishedPortfolioSnapshot:
        """Atomically read one account and publish its immutable V3 snapshot."""
        ref = self._require_portfolio_ref(portfolio_ref)
        if self.connection.in_transaction:
            raise ContractViolation("portfolio publication requires a clean SQLite transaction state")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            captured_at = require_aware(self.clock(), "captured_at")
            preview = self._read_preview(account_id, captured_at)
            if not preview.positions:
                raise DataUnavailable(
                    f"account {account_id} has no positive current positions",
                    context={"account_id": account_id},
                )
            if preview.missing_price_symbols:
                raise DataUnavailable(
                    "current positions lack visible V3 equity prices",
                    context={
                        "account_id": account_id,
                        "missing_symbols": list(preview.missing_price_symbols),
                    },
                )
            total_value = preview.total_value
            if total_value is None or not isfinite(total_value) or total_value <= 0:
                raise ContractViolation("portfolio total value must be finite and positive")
            positions = [
                {
                    "symbol": position.symbol,
                    "name": position.name,
                    "quantity": position.quantity,
                    "average_cost": position.average_cost,
                    "price": position.price,
                    "price_event_time": position.price_event_time.isoformat(),
                    "price_artifact_id": position.price_artifact_id,
                    "source_updated_at": position.source_updated_at,
                    "weight": float(position.market_value) / total_value,
                }
                for position in preview.positions
            ]
            payload = {
                "portfolio_id": f"account:{preview.account.account_id}",
                "portfolio_ref": ref,
                "source_account_id": preview.account.account_id,
                "source_account_name": preview.account.name,
                "account_state_date": preview.account.state_date,
                "capture_fingerprint": preview.capture_fingerprint,
                "cash": preview.account.cash,
                "total_value": total_value,
                "positions": positions,
                "valuation": {
                    "currency": "CNY",
                    "method": "latest_visible_unadjusted_v3_close",
                    "price_domain": "price",
                    "price_dataset": "equity_price",
                },
            }
            payload_hash = canonical_payload_hash(payload)
            if self.price_source_id:
                payload["valuation"]["source_id"] = self.price_source_id
                payload["valuation"]["input_scope"] = "frozen_artifact_ids" if self.price_artifact_ids else "explicit_source_plan"
                payload_hash = canonical_payload_hash(payload)
            digest = hashlib.sha256(
                f"{ref}|{account_id}|{captured_at.isoformat()}|{payload_hash}".encode("utf-8")
            ).hexdigest()
            artifact = MarketArtifact(
                artifact_id=f"portfolio_snapshot:{digest}",
                domain="portfolio",
                dataset="portfolio_snapshot",
                entity_id=ref,
                event_time=captured_at,
                published_at=captured_at,
                known_at=captured_at,
                ingested_at=captured_at,
                source_id=SOURCE_ID,
                adapter_version=ADAPTER_VERSION,
                source_uri=f"sqlite:///account/{account_id}",
                adjustment=None,
                schema_version=SCHEMA_VERSION,
                license_tag="user_owned",
                usage_scope="scenario_research",
                payload_hash=payload_hash,
                payload=payload,
                revision_id=f"capture:{digest}",
                quality_flags=(
                    "captured_from_mutable_account_state",
                    "valued_with_v3_equity_price_artifacts",
                ),
            )
            self.repository.ingest_many_in_transaction((artifact,))
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return PublishedPortfolioSnapshot(
            artifact_id=artifact.artifact_id,
            portfolio_ref=ref,
            known_at=artifact.known_at,
            source_account_id=preview.account.account_id,
            source_account_name=preview.account.name,
            position_count=len(preview.positions),
            cash=preview.account.cash,
            total_value=total_value,
            capture_fingerprint=preview.capture_fingerprint,
        )

    def _read_preview(self, account_id: int, known_at: datetime) -> PortfolioPreview:
        if type(account_id) is not int or account_id <= 0:
            raise ContractViolation("account_id must be a positive integer")
        row = self.connection.execute(
            """
            SELECT id, name, current_capital, current_date,
                   (SELECT COUNT(*) FROM positions WHERE account_id = account.id),
                   (SELECT MAX(updated_at) FROM positions WHERE account_id = account.id)
            FROM account WHERE id = ?
            """,
            (account_id,),
        ).fetchone()
        if row is None:
            raise DataUnavailable(f"account {account_id} does not exist", context={"account_id": account_id})
        account = self._account_from_row(row)
        rows = self.connection.execute(
            """
            SELECT p.symbol, s.name, p.quantity, p.avg_cost, p.updated_at
            FROM positions AS p
            LEFT JOIN stock_info AS s ON s.symbol = p.symbol
            WHERE p.account_id = ?
            ORDER BY p.symbol
            """,
            (account_id,),
        ).fetchall()
        source_positions = tuple(self._source_position(row) for row in rows)
        price_by_symbol = self._latest_prices(
            tuple(position[0] for position in source_positions), known_at
        )
        positions = tuple(
            PositionValuation(
                symbol=symbol,
                name=name,
                quantity=quantity,
                average_cost=average_cost,
                source_updated_at=updated_at,
                price=price_by_symbol[symbol][0] if symbol in price_by_symbol else None,
                price_event_time=price_by_symbol[symbol][1] if symbol in price_by_symbol else None,
                price_artifact_id=price_by_symbol[symbol][2] if symbol in price_by_symbol else None,
            )
            for symbol, name, quantity, average_cost, updated_at in source_positions
        )
        missing = tuple(position.symbol for position in positions if position.price_artifact_id is None)
        fingerprint_payload = {
            "source_account_id": account.account_id,
            "source_account_name": account.name,
            "cash": account.cash,
            "account_state_date": account.state_date,
            "positions": [
                {
                    "symbol": position.symbol,
                    "quantity": position.quantity,
                    "average_cost": position.average_cost,
                    "source_updated_at": position.source_updated_at,
                    "price_artifact_id": position.price_artifact_id,
                }
                for position in positions
            ],
        }
        return PortfolioPreview(
            account=account,
            known_at=known_at,
            positions=positions,
            missing_price_symbols=missing,
            capture_fingerprint=canonical_payload_hash(fingerprint_payload),
        )

    def _latest_prices(
        self,
        symbols: tuple[str, ...],
        known_at: datetime,
    ) -> dict[str, tuple[float, datetime, str]]:
        if not symbols:
            return {}
        request = DataRequest(
            domain="price",
            dataset="equity_price",
            entities=symbols,
            start=known_at - timedelta(days=36500),
            end=known_at,
            known_at=known_at,
            frequency="1d",
            adjustment="none",
            strict_pit=True,
        )
        latest: dict[str, MarketArtifact] = {}
        artifacts = (tuple(self.repository.get_visible(ref, known_at) for ref in self.price_artifact_ids)
                     if self.price_artifact_ids is not None else self.repository.query(request, source_id=self.price_source_id))
        for artifact in artifacts:
            if self.price_source_id and (artifact.domain != "price" or artifact.dataset != "equity_price"
                    or artifact.source_id != self.price_source_id or artifact.adjustment != "none" or artifact.frequency != "1d"):
                raise ContractViolation("portfolio price artifact violates the frozen source plan")
            if artifact.entity_id not in symbols:
                continue
            previous = latest.get(artifact.entity_id)
            if previous is None or (artifact.event_time, artifact.known_at) > (
                previous.event_time,
                previous.known_at,
            ):
                latest[artifact.entity_id] = artifact
        result: dict[str, tuple[float, datetime, str]] = {}
        for symbol, artifact in latest.items():
            if "close" not in artifact.payload:
                raise ContractViolation(f"price artifact {artifact.artifact_id} has no close")
            price = self._finite_number(
                artifact.payload["close"], f"price close for {symbol}", positive=True
            )
            result[symbol] = (price, artifact.event_time, artifact.artifact_id)
        return result

    @classmethod
    def _account_from_row(cls, row) -> AccountSummary:
        account_id, name, cash, state_date, position_count, updated_at = row
        if type(account_id) is not int or account_id <= 0:
            raise ContractViolation("account.id must be a positive integer")
        if not isinstance(name, str) or not name.strip():
            raise ContractViolation(f"account {account_id} has no valid name")
        cash_value = cls._finite_number(cash, f"account {account_id} cash", minimum=0.0)
        if not isinstance(state_date, str):
            raise ContractViolation(f"account {account_id} current_date must be an ISO date")
        try:
            date.fromisoformat(state_date)
        except ValueError as exc:
            raise ContractViolation(f"account {account_id} current_date must be an ISO date") from exc
        if type(position_count) is not int or position_count < 0:
            raise ContractViolation(f"account {account_id} position count is invalid")
        if updated_at is not None and (not isinstance(updated_at, str) or not updated_at.strip()):
            raise ContractViolation(f"account {account_id} position updated_at is invalid")
        return AccountSummary(
            account_id=account_id,
            name=name.strip(),
            cash=cash_value,
            state_date=state_date,
            position_count=position_count,
            position_updated_at=updated_at,
        )

    @classmethod
    def _source_position(cls, row) -> tuple[str, str | None, int, float, str]:
        symbol, name, quantity, average_cost, updated_at = row
        if not isinstance(symbol, str) or not symbol.strip():
            raise ContractViolation("position symbol must be non-empty")
        if type(quantity) is not int or quantity <= 0:
            raise ContractViolation(f"position quantity must be a positive integer for {symbol}")
        cost = cls._finite_number(average_cost, f"average cost for {symbol}", positive=True)
        if not isinstance(updated_at, str) or not updated_at.strip():
            raise ContractViolation(f"position updated_at is required for {symbol}")
        display_name = name.strip() if isinstance(name, str) and name.strip() else None
        return symbol.strip(), display_name, quantity, cost, updated_at

    @staticmethod
    def _finite_number(
        value,
        field_name: str,
        *,
        minimum: float | None = None,
        positive: bool = False,
    ) -> float:
        if isinstance(value, bool):
            raise ContractViolation(f"{field_name} must be numeric")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ContractViolation(f"{field_name} must be numeric") from exc
        if not isfinite(number):
            raise ContractViolation(f"{field_name} must be finite")
        if positive and number <= 0:
            raise ContractViolation(f"{field_name} must be positive")
        if minimum is not None and number < minimum:
            raise ContractViolation(f"{field_name} must be at least {minimum}")
        return number

    @staticmethod
    def _require_portfolio_ref(portfolio_ref: str) -> str:
        if not isinstance(portfolio_ref, str) or not portfolio_ref.strip():
            raise ContractViolation("portfolio_ref must be non-empty")
        return portfolio_ref.strip()


__all__ = [
    "AccountSummary",
    "CurrentPortfolioSnapshotModule",
    "PortfolioPreview",
    "PositionValuation",
    "PublishedPortfolioSnapshot",
    "automatically_selected_account_id",
]
