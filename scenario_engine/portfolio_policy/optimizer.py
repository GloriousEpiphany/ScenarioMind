"""Auditable MILP stress research; marginal quantiles are not joint risk."""

from __future__ import annotations

from dataclasses import dataclass
from math import floor, isfinite
from typing import Mapping

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

from scenario_engine.errors import ContractViolation, InfeasiblePortfolio, OptimizationFailure
from scenario_engine.quant.contracts import LossCube
from scenario_engine.quant.risk import expected_shortfall

from .constraints import PortfolioConstraints


@dataclass(frozen=True)
class PortfolioObjective:
    proposal_type: str
    cvar_weight: float
    worst_branch_weight: float
    turnover_weight: float
    uncertainty_weight: float
    tail_probability: float = 0.10

    def __post_init__(self) -> None:
        if not self.proposal_type:
            raise ContractViolation("PortfolioObjective.proposal_type is required")
        values = (
            self.cvar_weight,
            self.worst_branch_weight,
            self.turnover_weight,
            self.uncertainty_weight,
        )
        if any(not isfinite(float(value)) or value < 0 for value in values) or sum(values) <= 0:
            raise ContractViolation("portfolio objective weights must be finite, non-negative, and non-zero")
        if not 0 < self.tail_probability < 0.5:
            raise ContractViolation("tail_probability must be in (0, 0.5)")


@dataclass(frozen=True)
class PortfolioProposal:
    proposal_type: str
    weights: Mapping[str, float]
    cash_weight: float
    status: str
    activation_conditions: tuple[str, ...]
    solver_name: str
    solver_status: str
    objective_value: float | None


@dataclass(frozen=True)
class ProposalComparison:
    proposal: PortfolioProposal
    cvar_before: float
    cvar_after: float
    worst_branch_before: float
    worst_branch_after: float
    turnover: float
    transaction_cost: float
    concentration_before: float
    concentration_after: float
    uncertainty_before: float
    uncertainty_after: float
    risk_measure: str = "marginal_q10_stress_proxy"
    calculation_version: str = "simple_returns_cash_costs_v1"
    publication_eligible: bool = False
    publication_blockers: tuple[str, ...] = (
        "calibrated_joint_paths_required",
        "wealth_path_drawdown_not_validated",
        "independent_proposal_validation_required",
    )


class PortfolioPolicy:
    """Solve the full legal trade set; a non-optimal result is never emitted."""

    DEFAULT_OBJECTIVES = (
        PortfolioObjective("defensive", 0.50, 0.35, 0.10, 0.05),
        PortfolioObjective("balanced", 0.40, 0.25, 0.25, 0.10),
    )

    def __init__(self, objectives: tuple[PortfolioObjective, ...] | None = None):
        self.objectives = objectives or self.DEFAULT_OBJECTIVES
        if {item.proposal_type for item in self.objectives} != {"defensive", "balanced"}:
            raise ContractViolation("PortfolioPolicy requires exactly defensive and balanced objectives")

    def optimize(
        self,
        loss_cube: LossCube,
        current_weights: Mapping[str, float],
        constraints: PortfolioConstraints,
        *,
        confirmed_signals: tuple[str, ...] = (),
    ) -> tuple[ProposalComparison, ...]:
        symbols = tuple(sorted(current_weights))
        if set(symbols) != set(constraints.assets):
            raise ContractViolation("current weights and asset constraints must cover the same symbols")
        current = np.asarray([float(current_weights[item]) for item in symbols], dtype=float)
        if np.any(~np.isfinite(current)) or np.any(current < 0) or current.sum() > 1 + 1e-9:
            raise ContractViolation("current asset weights are invalid")
        scenarios, uncertainty = self._scenario_surface(loss_cube, symbols)
        before = self._metrics(scenarios, current, 0.10)
        before_uncertainty = float(uncertainty @ current)
        observation_issues = []
        try:
            self._validate_feasible(current, symbols, current, constraints)
        except InfeasiblePortfolio as exc:
            observation_issues.append(str(exc))
        if before[1] > constraints.max_drawdown:
            observation_issues.append("current portfolio exceeds the stress loss limit")
        comparisons = []
        for objective in self.objectives:
            weights, objective_value = self._solve(
                scenarios, uncertainty, symbols, current, constraints, objective, before[1]
            )
            after = self._metrics(scenarios, weights, objective.tail_probability)
            turnover = float(np.abs(weights - current).sum())
            transaction_cost = turnover * constraints.transaction_cost_rate
            proposal = PortfolioProposal(
                proposal_type=objective.proposal_type,
                weights={symbol: float(weight) for symbol, weight in zip(symbols, weights)},
                cash_weight=float(1 - weights.sum() - transaction_cost),
                status="research_only",
                activation_conditions=("requires calibrated joint paths and independent validation",),
                solver_name="scipy.optimize.milp/highs",
                solver_status="optimal",
                objective_value=objective_value,
            )
            comparison = ProposalComparison(
                proposal=proposal,
                cvar_before=before[0],
                cvar_after=after[0] + transaction_cost,
                worst_branch_before=before[1],
                worst_branch_after=after[1] + transaction_cost,
                turnover=turnover,
                transaction_cost=transaction_cost,
                concentration_before=float(np.square(current).sum()),
                concentration_after=float(np.square(weights).sum()),
                uncertainty_before=before_uncertainty,
                uncertainty_after=float(uncertainty @ weights),
            )
            self._validate_comparison(comparison, constraints)
            comparisons.append(comparison)
        observation = PortfolioProposal(
            proposal_type="observation",
            weights=dict(current_weights),
            cash_weight=float(1 - current.sum()),
            status=(
                "risk_limit_breached"
                if observation_issues
                else "research_only"
            ),
            activation_conditions=(
                tuple(observation_issues)
                if observation_issues
                else (confirmed_signals or ("requires a validated observable signal",))
            ),
            solver_name="not_applicable",
            solver_status="not_solved",
            objective_value=None,
        )
        comparisons.append(
            ProposalComparison(
                proposal=observation,
                cvar_before=before[0], cvar_after=before[0],
                worst_branch_before=before[1], worst_branch_after=before[1],
                turnover=0.0, transaction_cost=0.0,
                concentration_before=float(np.square(current).sum()),
                concentration_after=float(np.square(current).sum()),
                uncertainty_before=before_uncertainty,
                uncertainty_after=before_uncertainty,
            )
        )
        return tuple(comparisons)

    @staticmethod
    def _scenario_surface(cube: LossCube, symbols: tuple[str, ...]) -> tuple[np.ndarray, np.ndarray]:
        rows: dict[tuple[str, int], dict[str, float]] = {}
        uncertainty_values: dict[str, list[float]] = {symbol: [] for symbol in symbols}
        for cell in cube.cells:
            if cell.quantile != "q10":
                continue
            row = rows.setdefault((cell.branch_id, cell.horizon), {})
            if cell.symbol in row:
                raise ContractViolation("duplicate symbol in LossCube downside row")
            row[cell.symbol] = cell.return_value
            if cell.symbol in uncertainty_values:
                uncertainty_values[cell.symbol].append(
                    float(cell.model_disagreement) + (1.0 - float(cell.coverage))
                )
        if not rows:
            raise ContractViolation("LossCube has no q10 downside surface")
        matrix = []
        for key, values in sorted(rows.items()):
            missing = set(symbols).difference(values)
            if missing:
                raise ContractViolation(f"LossCube row {key} missing symbols: {sorted(missing)}")
            with np.errstate(over="raise", invalid="raise"):
                try:
                    matrix.append([-float(np.expm1(values[symbol])) for symbol in symbols])
                except FloatingPointError as exc:
                    raise ContractViolation("log return cannot be converted to a finite simple return") from exc
        if any(not values for values in uncertainty_values.values()):
            raise ContractViolation("LossCube lacks uncertainty coverage for one or more symbols")
        scenarios = np.asarray(matrix, dtype=float)
        uncertainty = np.asarray(
            [max(uncertainty_values[symbol]) for symbol in symbols], dtype=float
        )
        if np.any(~np.isfinite(scenarios)) or np.any(~np.isfinite(uncertainty)):
            raise ContractViolation("LossCube optimization surface contains non-finite values")
        return scenarios, uncertainty

    def _solve(self, scenarios, uncertainty, symbols, current, constraints, objective, before_worst):
        asset_count = len(symbols)
        scenario_count = len(scenarios)
        z_start = 0
        u_start = asset_count
        eta_index = 2 * asset_count
        xi_start = eta_index + 1
        worst_index = xi_start + scenario_count
        variable_count = worst_index + 1
        steps = np.asarray([constraints.assets[symbol].min_trade_weight for symbol in symbols])

        lower = np.full(variable_count, -np.inf, dtype=float)
        upper = np.full(variable_count, np.inf, dtype=float)
        integrality = np.zeros(variable_count, dtype=int)
        integrality[z_start:u_start] = 1
        lower[u_start:eta_index] = 0.0
        upper[u_start:eta_index] = constraints.max_turnover
        lower[xi_start:worst_index] = 0.0

        for index, symbol in enumerate(symbols):
            asset = constraints.assets[symbol]
            upper_weight = min(
                constraints.max_single_weight,
                asset.max_weight,
                asset.liquidity_capacity_weight,
            )
            min_lots = -floor(asset.sellable_quantity / asset.lot_size)
            max_lots = floor((upper_weight - current[index]) / steps[index] + 1e-10)
            if not asset.tradable:
                min_lots = max_lots = 0
            else:
                if not asset.can_sell:
                    min_lots = 0
                if not asset.can_buy or not asset.fundamental_validated:
                    max_lots = min(0, max_lots)
            lower[index] = min_lots
            upper[index] = max_lots
            if min_lots > max_lots:
                raise InfeasiblePortfolio(
                    f"legal lot bounds are infeasible for {symbol}",
                    context={"min_lots": min_lots, "max_lots": max_lots},
                )

        rows: list[tuple[dict[int, float], float, float]] = []

        def add(coefficients: dict[int, float], lb: float = -np.inf, ub: float = np.inf) -> None:
            rows.append((coefficients, lb, ub))

        for index in range(asset_count):
            add({index: steps[index], u_start + index: -1.0}, ub=0.0)
            add({index: -steps[index], u_start + index: -1.0}, ub=0.0)
        add({u_start + index: 1.0 for index in range(asset_count)}, ub=constraints.max_turnover)
        add(
            {
                **{index: steps[index] for index in range(asset_count)},
                **{u_start + index: constraints.transaction_cost_rate for index in range(asset_count)},
            },
            ub=1.0 - constraints.min_cash_weight - float(current.sum()),
        )
        industries: dict[str, list[int]] = {}
        for index, symbol in enumerate(symbols):
            industries.setdefault(constraints.assets[symbol].industry_id, []).append(index)
        for indices in industries.values():
            add(
                {index: steps[index] for index in indices},
                ub=constraints.max_industry_weight - float(current[indices].sum()),
            )

        affine = scenarios * steps[np.newaxis, :]
        base_losses = scenarios @ current
        for scenario_index in range(scenario_count):
            coefficients = {index: float(affine[scenario_index, index]) for index in range(asset_count)}
            coefficients[eta_index] = -1.0
            coefficients[xi_start + scenario_index] = -1.0
            add(coefficients, ub=-float(base_losses[scenario_index]))
            worst_coefficients = {
                index: float(affine[scenario_index, index]) for index in range(asset_count)
            }
            worst_coefficients[worst_index] = -1.0
            add(worst_coefficients, ub=-float(base_losses[scenario_index]))

        risk_coefficients = {worst_index: 1.0}
        for index in range(asset_count):
            risk_coefficients[u_start + index] = constraints.transaction_cost_rate
        add(risk_coefficients, ub=constraints.max_drawdown)
        if objective.proposal_type == "defensive":
            add(risk_coefficients, ub=before_worst)

        matrix = lil_matrix((len(rows), variable_count), dtype=float)
        row_lower = np.empty(len(rows), dtype=float)
        row_upper = np.empty(len(rows), dtype=float)
        for row_index, (coefficients, lb, ub) in enumerate(rows):
            for column, value in coefficients.items():
                matrix[row_index, column] = value
            row_lower[row_index] = lb
            row_upper[row_index] = ub

        coefficients = np.zeros(variable_count, dtype=float)
        coefficients[eta_index] = objective.cvar_weight
        coefficients[xi_start:worst_index] = (
            objective.cvar_weight / (scenario_count * objective.tail_probability)
        )
        coefficients[worst_index] = objective.worst_branch_weight
        coefficients[u_start:eta_index] = (
            objective.turnover_weight
            + constraints.transaction_cost_rate * (objective.cvar_weight + objective.worst_branch_weight)
        )
        coefficients[z_start:u_start] += objective.uncertainty_weight * uncertainty * steps

        result = milp(
            c=coefficients,
            integrality=integrality,
            bounds=Bounds(lower, upper),
            constraints=LinearConstraint(matrix.tocsr(), row_lower, row_upper),
            options={"presolve": True, "mip_rel_gap": 0.0},
        )
        if result.status == 2:
            raise InfeasiblePortfolio(
                f"{objective.proposal_type} portfolio has no legal solution",
                context={"solver_message": str(result.message)},
            )
        if result.status != 0 or result.x is None or result.fun is None:
            raise OptimizationFailure(
                f"{objective.proposal_type} optimization did not reach a proven optimum",
                context={"solver_status": int(result.status), "solver_message": str(result.message)},
            )
        lot_changes = np.rint(result.x[z_start:u_start]).astype(int)
        if not np.allclose(result.x[z_start:u_start], lot_changes, atol=1e-7, rtol=0):
            raise OptimizationFailure("MILP solver returned non-integral trade lots")
        weights = current + lot_changes * steps
        self._validate_feasible(weights, symbols, current, constraints)
        return weights, float(result.fun)

    @staticmethod
    def _validate_feasible(weights, symbols, current, constraints):
        tolerance = 1e-7
        cost = np.abs(weights - current).sum() * constraints.transaction_cost_rate
        if weights.sum() + cost > 1 - constraints.min_cash_weight + tolerance:
            raise InfeasiblePortfolio("cash-weight constraint is infeasible")
        if np.abs(weights - current).sum() > constraints.max_turnover + tolerance:
            raise InfeasiblePortfolio("turnover constraint is infeasible")
        industries: dict[str, float] = {}
        for index, symbol in enumerate(symbols):
            asset = constraints.assets[symbol]
            weight = float(weights[index])
            delta = weight - float(current[index])
            upper = min(constraints.max_single_weight, asset.max_weight, asset.liquidity_capacity_weight)
            if weight < -tolerance or weight > upper + tolerance:
                raise InfeasiblePortfolio(f"weight limit is infeasible for {symbol}")
            if delta > tolerance and (not asset.tradable or not asset.can_buy or not asset.fundamental_validated):
                raise InfeasiblePortfolio(f"illegal purchase proposed for {symbol}")
            if delta < -tolerance and (
                not asset.tradable or not asset.can_sell or -delta > asset.max_sell_weight + tolerance
            ):
                raise InfeasiblePortfolio(f"illegal T+1 sale proposed for {symbol}")
            lots = abs(delta) / asset.min_trade_weight
            if abs(lots - round(lots)) > tolerance:
                raise InfeasiblePortfolio(f"trade for {symbol} violates the minimum lot")
            industries[asset.industry_id] = industries.get(asset.industry_id, 0.0) + weight
        if any(value > constraints.max_industry_weight + tolerance for value in industries.values()):
            raise InfeasiblePortfolio("industry-weight constraint is infeasible")

    @staticmethod
    def _validate_comparison(comparison: ProposalComparison, constraints: PortfolioConstraints) -> None:
        if comparison.proposal.proposal_type == "defensive" and (
            comparison.worst_branch_after > comparison.worst_branch_before + 1e-8
        ):
            raise InfeasiblePortfolio("defensive proposal worsens the worst branch")
        if comparison.worst_branch_after > constraints.max_drawdown + 1e-8:
            raise InfeasiblePortfolio(
                f"{comparison.proposal.proposal_type} proposal violates max_drawdown"
            )

    @staticmethod
    def _metrics(scenarios, weights, tail_probability):
        losses = scenarios @ weights
        mass = np.full(len(losses), 1.0 / len(losses))
        return expected_shortfall(losses, mass, tail_probability), float(losses.max())
