"""One original issuance per model, issuer and Shanghai decision date."""
from datetime import datetime, time, timedelta

from scenario_engine.errors import ContractViolation
from src.stock_app.cninfo_disclosures import SHANGHAI


def require_unused_origin(connection, dataset, schema, symbol, issued):
    if not connection.in_transaction:
        raise ContractViolation('an original forecast must reserve its origin in the checkpoint transaction')
    start = datetime.combine(issued.astimezone(SHANGHAI).date(),time.min,SHANGHAI)
    existing = connection.execute(
        "SELECT artifact_id FROM market_artifacts WHERE domain='quant_validation' AND dataset=? "
        "AND schema_version=? AND entity_id=? AND pit_utc(known_at)>=pit_utc(?) AND pit_utc(known_at)<pit_utc(?) LIMIT 1",
        (dataset,schema,symbol,start.isoformat(),(start+timedelta(days=1)).isoformat())).fetchone()
    if existing:
        raise ContractViolation('issuer and model already have an immutable original at this origin',
                                context={'existing_ref':existing[0]})
