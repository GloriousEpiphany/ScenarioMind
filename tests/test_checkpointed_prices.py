from datetime import date

import pytest

from src.stock_app.checkpointed_prices import CheckpointedEastmoneyReader
from src.stock_app.eastmoney_prices import EastmoneyDailyReader


def test_successful_range_survives_later_failure_and_reopen(tmp_path, monkeypatch):
    attempts = []
    start, end = date(2026, 10, 1), date(2026, 10, 27)

    def read(self, secid, first, last):
        attempts.append(secid)
        if secid == '0.002230' and attempts.count(secid) == 1:
            raise TimeoutError('provider interrupted')
        return {last: {'close': '10'}}, 'https://push2his.eastmoney.com/' + secid

    monkeypatch.setattr(EastmoneyDailyReader, 'read', read)
    path = tmp_path / 'checkpoints.db'
    reader = CheckpointedEastmoneyReader(path, 'run', get=lambda *a, **k: None)
    first = reader.read('1.000001', start, end)
    with pytest.raises(TimeoutError):
        reader.read('0.002230', start, end)
    reopened = CheckpointedEastmoneyReader(path, 'run', get=lambda *a, **k: None)
    assert reopened.read('1.000001', start, end) == first
    assert attempts.count('1.000001') == 1
    assert reopened.read('0.002230', start, end)[0][end]['close'] == '10'
    assert attempts.count('0.002230') == 2
    # A new range is a different provider request, never an old cache result.
    reopened.read('1.000001', date(2026, 9, 1), end)
    assert attempts.count('1.000001') == 2
