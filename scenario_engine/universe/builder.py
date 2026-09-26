"""Build an auditable candidate universe from PIT security and thesis artifacts."""

from dataclasses import asdict
from datetime import datetime, timezone

from scenario_engine.data import MarketArtifact, canonical_payload_hash
from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, DataUnavailable
from .contracts import InvestmentUniverse, UniverseMember


def build_universe_artifact(*, repository, universe_ref, portfolio, master_artifact_id,
                            thesis_artifact_ids, allowed_markets, policy_version,
                            as_of, expires_at, clock=None):
    require_aware(as_of, "universe.as_of")
    require_aware(expires_at, "universe.expires_at")
    created_at = (clock or (lambda: datetime.now(timezone.utc)))()
    require_aware(created_at, "universe.created_at")
    if not as_of <= created_at < expires_at:
        raise ContractViolation("universe creation must follow its cutoff and precede expiry")
    if not universe_ref or not allowed_markets or not policy_version:
        raise ContractViolation("universe reference, allowed markets and policy version are required")
    if not portfolio.snapshot_artifact_id:
        raise ContractViolation("universe construction requires an immutable account snapshot artifact")
    account = repository.get_visible(portfolio.snapshot_artifact_id, as_of)
    if (account.domain, account.dataset) != ("portfolio", "portfolio_snapshot"):
        raise ContractViolation("universe account reference must be a portfolio snapshot")
    master = repository.get_visible(master_artifact_id, as_of)
    if (master.domain, master.dataset) != ("security", "security_master_snapshot"):
        raise ContractViolation("universe requires a versioned security master snapshot")
    if master.payload.get("complete") is not True:
        raise DataUnavailable("security master is incomplete; cannot reconstruct the investable universe")
    securities = {}
    try:
        for row in master.payload["securities"]:
            symbol = row["symbol"]
            if not isinstance(symbol, str) or not symbol or symbol in securities:
                raise ContractViolation("security master contains blank or duplicate symbols")
            listed = datetime.fromisoformat(row["listed_at"])
            delisted = datetime.fromisoformat(row["delisted_at"]) if row["delisted_at"] is not None else None
            require_aware(listed, "listed_at")
            if delisted is not None:
                require_aware(delisted, "delisted_at")
                if delisted <= listed:
                    raise ContractViolation("security delisting must follow listing")
            if not isinstance(row["market"], str) or not row["market"]:
                raise ContractViolation("security market is required")
            securities[symbol] = (row["market"], listed <= as_of and (delisted is None or as_of < delisted))
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation("security master has invalid membership fields") from exc
    missing = set(portfolio.symbols).difference(securities)
    if missing:
        raise DataUnavailable(f"security master omits current holdings: {sorted(missing)}")
    theses = {}
    evidence_ids = {master.artifact_id, account.artifact_id}
    for identifier in thesis_artifact_ids:
        thesis = repository.get_visible(identifier, as_of)
        if (thesis.domain, thesis.dataset) != ("research", "company_thesis"):
            raise ContractViolation("candidate research must be a company thesis artifact")
        symbol = thesis.entity_id
        if symbol in theses:
            raise ContractViolation(f"multiple unresolved company theses for {symbol}")
        if symbol not in securities:
            raise ContractViolation(f"company thesis security is outside the master: {symbol}")
        try:
            payload = thesis.payload
            thesis_expiry = datetime.fromisoformat(payload["expires_at"])
            require_aware(thesis_expiry, "company_thesis.expires_at")
            if (payload["status"] != "validated" or not payload["validation_ref"]
                    or not payload["thesis"] or not payload["invalidation_conditions"]
                    or not payload["evidence_artifact_ids"] or expires_at > thesis_expiry):
                raise ContractViolation(f"candidate thesis is incomplete, unvalidated or expires too early: {symbol}")
            validation = repository.get_visible(payload["validation_ref"], as_of)
            if ((validation.domain, validation.dataset) != ("research", "thesis_validation")
                    or validation.entity_id != symbol
                    or validation.payload.get("thesis_artifact_id") != thesis.artifact_id
                    or validation.payload.get("passed") is not True
                    or not validation.payload.get("validator_version")):
                raise ContractViolation(f"candidate thesis lacks a matching independent validation artifact: {symbol}")
            evidence_ids.add(validation.artifact_id)
            for evidence_id in payload["evidence_artifact_ids"]:
                evidence = repository.get_visible(evidence_id, as_of)
                if evidence.domain in {"research", "universe"}:
                    raise ContractViolation("a company thesis must cite original data, not only other research")
                evidence_ids.add(evidence.artifact_id)
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractViolation(f"invalid company thesis contract for {symbol}") from exc
        theses[symbol] = thesis.artifact_id
        evidence_ids.add(thesis.artifact_id)
    members, exclusions = [], []
    for symbol, (market, active) in sorted(securities.items()):
        held = symbol in portfolio.symbols
        if held:
            # Existing account risk survives screening, including delisted holdings.
            members.append(UniverseMember(symbol, market, True, theses.get(symbol)))
        elif market not in allowed_markets:
            exclusions.append((symbol, "market_not_allowed"))
        elif not active:
            exclusions.append((symbol, "not_listed_at_cutoff"))
        elif symbol not in theses:
            exclusions.append((symbol, "validated_company_thesis_missing"))
        else:
            members.append(UniverseMember(symbol, market, False, theses[symbol]))
    universe = InvestmentUniverse(
        universe_ref, portfolio.portfolio_id, portfolio.snapshot_artifact_id, as_of, expires_at, policy_version,
        tuple(members), tuple(sorted(evidence_ids)), tuple(exclusions),
    )
    payload = asdict(universe)
    payload.pop("universe_id")
    payload.update(as_of=as_of.isoformat(), expires_at=expires_at.isoformat(),
                   allowed_markets=sorted(allowed_markets), master_artifact_id=master.artifact_id)
    digest = canonical_payload_hash(payload)
    return MarketArtifact(
        artifact_id=f"investment_universe:{digest}", domain="universe", dataset="investment_universe",
        entity_id=universe_ref, event_time=as_of, published_at=created_at,
        known_at=created_at, ingested_at=created_at, source_id="stockm1_universe_builder",
        adapter_version="pit-universe-builder-v1", source_uri=f"derived://universe/{universe_ref}/{digest}",
        adjustment=None, schema_version="investment-universe-v1", license_tag="derived_from_referenced_artifacts",
        usage_scope="research_only", payload_hash=digest, payload=payload,
        revision_id=digest, valid_to=expires_at,
    )
