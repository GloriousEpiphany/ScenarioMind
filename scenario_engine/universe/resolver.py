"""Resolve immutable, evidence-backed universe manifests at the mission cutoff."""

from datetime import datetime, timedelta

from scenario_engine.data import DataRequest
from scenario_engine.errors import ContractViolation, DataUnavailable
from .contracts import InvestmentUniverse, UniverseMember


class PITUniverseResolver:
    def __init__(self, repository):
        self.repository = repository

    def resolve(self, universe_ref, portfolio, as_of):
        artifacts = self.repository.query(DataRequest(
            "universe", "investment_universe", (universe_ref,),
            as_of - timedelta(days=36500), as_of, as_of,
        ))
        if not artifacts:
            raise DataUnavailable(f"no PIT investment universe exists for {universe_ref!r}")
        artifact = max(artifacts, key=lambda item: (item.event_time, item.known_at))
        payload = artifact.payload
        try:
            universe = InvestmentUniverse(
                artifact.artifact_id, payload["portfolio_id"], payload["portfolio_snapshot_artifact_id"], datetime.fromisoformat(payload["as_of"]),
                datetime.fromisoformat(payload["expires_at"]), payload["policy_version"],
                tuple(UniverseMember(**member) for member in payload["members"]),
                tuple(payload["source_artifact_ids"]), tuple(tuple(item) for item in payload["exclusions"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractViolation("investment universe manifest has invalid schema") from exc
        universe.expand_portfolio(portfolio, as_of)
        for identifier in universe.source_artifact_ids:
            self.repository.get_visible(identifier, universe.as_of)
        for member in universe.members:
            if member.thesis_artifact_id:
                if member.thesis_artifact_id not in universe.source_artifact_ids:
                    raise ContractViolation("universe thesis is outside its evidence manifest")
                thesis = self.repository.get_visible(member.thesis_artifact_id, universe.as_of)
                if (thesis.domain != "research" or thesis.dataset != "company_thesis"
                        or thesis.entity_id != member.symbol or thesis.payload.get("status") != "validated"):
                    raise ContractViolation("universe candidate thesis is not validated for this security")
                expires = datetime.fromisoformat(thesis.payload["expires_at"])
                if expires < universe.expires_at:
                    raise ContractViolation("universe outlives its company thesis")
        # Reconstruct membership from the recorded original inputs rather than
        # trusting flags in a manually imported or obsolete universe manifest.
        from .builder import build_universe_artifact

        thesis_ids = tuple(identifier for identifier in universe.source_artifact_ids
                           if self.repository.get_visible(identifier, universe.as_of).dataset == "company_thesis")
        try:
            rebuilt = build_universe_artifact(
                repository=self.repository, universe_ref=universe_ref, portfolio=portfolio,
                master_artifact_id=payload["master_artifact_id"], thesis_artifact_ids=thesis_ids,
                allowed_markets=payload["allowed_markets"], policy_version=universe.policy_version,
                as_of=universe.as_of, expires_at=universe.expires_at, clock=lambda: artifact.known_at,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractViolation("universe cannot be reconstructed from its recorded inputs") from exc
        if rebuilt.payload_hash != artifact.payload_hash:
            raise ContractViolation("universe membership or evidence differs from its original inputs")
        return universe
