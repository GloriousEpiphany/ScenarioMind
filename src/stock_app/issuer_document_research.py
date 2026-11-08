"""Current-receipt semantic research of complete official disclosure coverages.

This independent stage neither issues historical forecasts nor certifies a
six-domain portfolio report, event probabilities, or numerical wealth paths.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import json
import sqlite3

from scenario_engine.agents.disclosure_tools import (make_disclosure_tools, make_quoted_hypothesis_tool,
    unread_cited_documents, unread_hypothesis_quotes)
from scenario_engine.agents.deductive_tools import make_hypothesis_tool
from scenario_engine.agents.lease import advisory_lease
from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.agents.runtime import AgentRuntime
from scenario_engine.agents.tools import fingerprint
from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ContractViolation, DataUnavailable
from scenario_engine.sqlite_transactions import atomic_write
from src.stock_app.disclosure_source_binding import read_disclosure_coverage
from src.stock_app.quantitative_inputs import derived_artifact


def run_issuer_document_research(db_path, run_id, symbol, coverage_refs, *, model, policy, objective, endpoint_hash, clock=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    if not coverage_refs or len(set(coverage_refs)) != len(coverage_refs):
        raise ContractViolation('document research needs explicit distinct complete coverages')
    if model.policy != policy or not endpoint_hash:
        raise ContractViolation('document research and model policy differ')
    root = 'issuer-document-research:' + run_id
    contract = {'version': 'issuer-document-research-v3-quoted-spans', 'symbol': symbol, 'coverage_refs': list(coverage_refs),
                'objective': objective, 'policy': asdict(policy), 'transport': model.transport_contract,
                'endpoint_hash': endpoint_hash}
    with advisory_lease(db_path, root) as lost, sqlite3.connect(db_path, timeout=30) as c:
        repo, board = MarketArtifactRepository(c), ResearchBoard(c)
        previous = c.execute('SELECT contract_json FROM advisory_runs WHERE run_id=?', (root,)).fetchone()
        legacy = previous is not None and json.loads(previous[0]).get('version') == 'issuer-document-research-v2-read-gate'
        # Resume an existing immutable run with its original read contract. Only
        # a new run ID adopts quoted spans; no historical acceptance is rewritten.
        if legacy:
            contract['version'] = 'issuer-document-research-v2-read-gate'
        board.register_run(root, contract)
        frozen = board.load(root, 'frozen', contract)
        if frozen is None:
            at = clock(); docs = {}
            for ref in coverage_refs:
                _, _, _, items = read_disclosure_coverage(repo, ref, symbol, at)
                for item in items:
                    docs[item.artifact_id] = item
            if not docs:
                raise DataUnavailable('no original documents for semantic research')
            manifest = [{'artifact_id': d.artifact_id, 'domain': d.domain, 'dataset': d.dataset,
                         'entity_id': d.entity_id, 'payload_hash': d.payload_hash,
                         'text_chars': len(d.payload['content'])}
                        for d in sorted(docs.values(), key=lambda x: (x.event_time, x.artifact_id))]
            frozen = {'as_of': at.isoformat(), 'manifest': manifest}
            board.save(root, 'frozen', contract, frozen)
        at = datetime.fromisoformat(frozen['as_of']); manifest = frozen['manifest']
        for row in manifest:
            item = repo.get_visible(row['artifact_id'], at)
            if item.payload_hash != row['payload_hash'] or item.ingested_at > at:
                raise ContractViolation('frozen document input changed or was not ingested')
        saved = board.load(root, 'result', contract)
        if saved:
            repo.get_visible(saved['report_ref'], clock())
            return saved
        read_tools = make_disclosure_tools(repo, manifest=manifest, targeted=not legacy)
        hypothesis_factory = make_hypothesis_tool if legacy else make_quoted_hypothesis_tool
        hypothesis = hypothesis_factory(repo, manifest=manifest, required_entities=(symbol,), require_child=True)
        runtime = AgentRuntime(db_path=db_path, model=model, policy=policy, tools=(*read_tools, hypothesis))
        try:
            result = runtime.execute(run_id=root + ':agent', objective=objective, as_of=at,
                context={'symbols': [symbol], 'manifest_hash': fingerprint(manifest),
                         **({'required_document_lengths': {r['artifact_id']: r['text_chars'] for r in manifest}} if legacy
                            else {'required_quoted_document_manifest': manifest}),
                         'required_tools': ['search_evidence', 'fetch_document', 'propose_hypotheses'],
                         'scope': 'current_receipt_official_disclosure_semantics_only',
                         'instructions': ('Read the complete index, choose material events and counterevidence, and read '
                                'all chunks of every cited document. ' if legacy else
                                'Read the complete index, choose material events and counterevidence. '
                                'Use an empty search query for the complete index and exact queries to locate relevant passages. '
                                'Read support and counterevidence; every hypothesis needs evidence_quotes with exact text and Unicode offsets '
                                'from fetched passages for every evidence_ref. Partial reading is not a full-document review. ')
                             + 'Dates in the PDFs are historical event dates, not '
                             'proof that this system knew them then. Form parent-child causal paths, observable '
                             'triggers and independent falsification. Do not invent probabilities, numerical returns, '
                             'positions, or realized investment performance. Request independent review.'})
        finally:
            runtime.close()
        missing = (unread_cited_documents if legacy else unread_hypothesis_quotes)(result, manifest)
        indexed = {d['artifact_id'] for e in result['tool_results'] if e.get('status') == 'completed'
                   and e.get('tool') == 'search_evidence' and not e['result'].get('query') for d in e['result']['documents']}
        unindexed = sorted({r['artifact_id'] for r in manifest} - indexed)
        proposals = [e['result']['hypotheses'] for e in result['tool_results']
                     if e.get('tool') == 'propose_hypotheses' and e.get('status') == 'completed']
        output = {'run_id': run_id, 'status': result['status'] if not (missing or unindexed) else 'incomplete_original_reads',
                  'as_of': frozen['as_of'], 'coverage_refs': list(coverage_refs), 'manifest': manifest,
                  'runtime_result': result, 'deductive_hypotheses': proposals[-1] if proposals else [],
                  **({'unread_cited_documents': missing} if legacy else {'hypotheses_with_unread_quotes': missing,
                      'reading_scope': 'quoted_original_spans_not_full_document_review'}),
                  'unindexed_documents': unindexed, 'historical_forecast': False,
                  'probability_status': 'uncalibrated', 'numeric_impact_status': 'not_estimated',
                  'publication_eligible': False, 'order_submission_enabled': False}
        if lost.is_set():
            raise ContractViolation('document research lease lost before result checkpoint')
        item = derived_artifact('issuer_document_research', 'issuer-document-research-v1' if legacy
            else 'issuer-document-research-v3-quoted-spans', output, clock(), entity=symbol)
        output = {**output, 'report_ref': item.artifact_id}
        with atomic_write(c):
            repo.ingest_many_in_transaction((item,))
            board.save(root, 'result', contract, output)
        return output
