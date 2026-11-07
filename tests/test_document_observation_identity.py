from dataclasses import replace
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from scenario_engine.data import DataRequest, MarketArtifact, MarketArtifactRepository, canonical_payload_hash
from scenario_engine.errors import DataConflict


@pytest.mark.parametrize("domain,dataset", [("event", "observed_event_source"), ("sentiment", "event_text_features"), ("event", "cctv_news")])
def test_concurrent_documents_survive_but_conflicting_same_document_fails(domain, dataset):
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    payload = {"text": "first article"}
    first = MarketArtifact("first", domain, dataset, "000001", now - timedelta(days=1), None,
        now, now, "fixture", "v1", "https://example.test/article/1", None, "v1", "test", "test",
        canonical_payload_hash(payload), payload)
    second = replace(first, artifact_id="second", source_uri="https://example.test/article/2")
    connection = sqlite3.connect(":memory:")
    repository = MarketArtifactRepository(connection)
    repository.ingest_many((first, second))
    request = DataRequest(domain, dataset, ("000001",), now - timedelta(days=2), now, now)
    assert {item.artifact_id for item in repository.query(request)} == {"first", "second"}
    changed = {"text": "conflicting version"}
    repository.add(replace(first, artifact_id="conflict", payload=changed, payload_hash=canonical_payload_hash(changed)))
    with pytest.raises(DataConflict, match="same visibility time"):
        repository.query(request)
    connection.close()


def test_pit_comparisons_use_instants_and_preserve_microsecond_boundary():
    utc = datetime(2026, 10, 15, 17, tzinfo=timezone.utc)
    local = utc.astimezone(timezone(timedelta(hours=8)))
    payload = {"value": 1}
    item = MarketArtifact("local", "fundamental", "pit_financials", "000001", local - timedelta(hours=1),
        local, local, local, "fixture", "v1", "https://example.test/report", None, "v1", "test", "test",
        canonical_payload_hash(payload), payload, valid_from=local - timedelta(days=1),
        valid_to=local + timedelta(microseconds=1))
    connection = sqlite3.connect(":memory:")
    repository = MarketArtifactRepository(connection)
    repository.add(item)
    def query(cutoff):
        return repository.query(DataRequest("fundamental", "pit_financials", ("000001",),
            utc - timedelta(days=1), cutoff, cutoff))
    assert query(utc) == (item,)
    assert query(utc - timedelta(microseconds=1)) == ()
    assert query(utc + timedelta(microseconds=1)) == ()
    connection.close()
