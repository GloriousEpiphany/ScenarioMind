from copy import deepcopy
from datetime import timedelta

import pytest

from scenario_engine.errors import ContractViolation, PointInTimeViolation
from src.stock_app.quantitative_inputs import derived_artifact
from src.stock_app.quantitative_readiness import bind_simulation_portfolio
from tests.test_action_source_binding import total_return_case, NOW


def test_simulation_uses_frozen_holdings_and_real_price_fields_without_custody_claims():
    c, repo, _, _ = total_return_case()
    try:
        price = repo.get_visible('raw-2', NOW)
        payload = {'cash': 1000., 'total_value': 7800., 'positions': [{'symbol': '002230', 'quantity': 100,
            'price': 68., 'weight': 6800. / 7800., 'price_artifact_id': price.artifact_id,
            'price_event_time': price.event_time.isoformat()}]}
        item = derived_artifact('portfolio_snapshot', 'fixture', payload, NOW, domain='portfolio')
        repo.ingest_many((item,))
        research = {'portfolio_artifact_id': item.artifact_id, 'portfolio': deepcopy(payload)}
        result = bind_simulation_portfolio(repo, research, NOW)
        assert result['status'] == 'simulation_inputs_bound'
        assert not result['broker_custody_verified'] and not result['trading_eligibility_required']
        assert not result['order_submission_enabled']
        research['portfolio']['positions'][0]['quantity'] = 200
        with pytest.raises(ContractViolation, match='differs'):
            bind_simulation_portfolio(repo, research, NOW)
        research['portfolio'] = deepcopy(payload)
        research['portfolio']['total_value'] += 100
        with pytest.raises(ContractViolation, match='differs'):
            bind_simulation_portfolio(repo, research, NOW)
        research['portfolio'] = deepcopy(payload)
        for ref in (price.artifact_id, item.artifact_id):
            original = repo.get_visible(ref, NOW)
            c.execute('UPDATE market_artifacts SET ingested_at=? WHERE artifact_id=?',
                      ((NOW + timedelta(days=1)).isoformat(), ref))
            with pytest.raises(PointInTimeViolation):
                bind_simulation_portfolio(repo, research, NOW)
            c.execute('UPDATE market_artifacts SET ingested_at=? WHERE artifact_id=?',
                      (original.ingested_at.isoformat(), ref))
    finally:
        c.close()
