"""Reparse counted official catalogues and original PDF bytes before features."""
import base64
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import re

from scenario_engine.errors import ContractViolation, DataUnavailable
from src.stock_app.cninfo_disclosures import VERSION, IDENTITY, CATALOGUE, PDF_ROOT, SHANGHAI, PAGE_SIZE, check_page, query_params, extract_pdf


def _source(repo, ref, dataset, schema, symbol, cutoff):
    item = repo.get_visible(ref, cutoff)
    if item.ingested_at > cutoff:
        raise DataUnavailable('disclosure source was not yet ingested at the stated cutoff')
    if (item.domain, item.dataset, item.schema_version, item.source_id, item.adapter_version, item.entity_id) != (
            'event', dataset, schema, 'cninfo_official_disclosure', VERSION, symbol):
        raise ContractViolation('disclosure source identity/provider/schema mismatch')
    return item


def _catalogue_metadata(repo, p, symbol, cutoff):
    start, end = date.fromisoformat(p['start']), date.fromisoformat(p['end'])
    if start > end or end >= cutoff.astimezone(SHANGHAI).date():
        raise ContractViolation('disclosure coverage includes an unfinished date range')
    identity = _source(repo, p['identity_ref'], 'disclosure_identity', 'cninfo-disclosure-identity-v1', symbol, cutoff)
    raw = identity.payload
    matches = [r for r in raw['raw'] if r.get('code') == symbol and r.get('category') == 'A股']
    if (identity.source_uri != IDENTITY or len(matches) != 1 or raw['symbol'] != symbol
            or raw['org_id'] != p['org_id'] or matches[0]['orgId'] != p['org_id']):
        raise ContractViolation('disclosure coverage issuer identity is unbound')
    if not isinstance(p['catalogue_refs'], list) or len(p['catalogue_refs']) != len(set(p['catalogue_refs'])):
        raise ContractViolation('disclosure coverage contains duplicate source references')
    if not p['catalogue_refs']:
        raise DataUnavailable('disclosure coverage has no counted catalogue')
    metadata, total = {}, None
    for page, page_ref in enumerate(p['catalogue_refs'], 1):
        item = _source(repo, page_ref, 'disclosure_catalogue', 'cninfo-disclosure-catalogue-v1', symbol, cutoff)
        if item.known_at.astimezone(SHANGHAI).date() <= end:
            raise DataUnavailable('catalogue page was captured before its final publication day had ended')
        if (item.source_uri != CATALOGUE or item.payload['params'] != query_params(symbol,p['org_id'],start,end,page)
                or item.payload['identity_ref'] != identity.artifact_id):
            raise ContractViolation('disclosure catalogue request/issuer was changed')
        rows, total = check_page(item.payload['raw'], symbol,p['org_id'],start,end,page,total)
        for row in rows:
            if row['announcementId'] in metadata:
                raise ContractViolation('disclosure ID repeated across pages')
            metadata[row['announcementId']] = row
    if (type(p['count']) is not int or total != p['count'] or len(metadata) != total
            or len(p['catalogue_refs']) != max(1,(total+PAGE_SIZE-1)//PAGE_SIZE)):
        raise DataUnavailable('disclosure coverage omits counted catalogue pages')
    return start, end, metadata


def read_publication_dates(repo, ref, symbol, cutoff):
    if repo.get_visible(ref,cutoff).dataset == 'publication_dates_sequence':
        _,start,end,rows = _read_sequence(repo,ref,symbol,cutoff,'publication_dates')
        return start,end,rows
    coverage = _source(repo, ref, 'issuer_publication_catalogue', 'cninfo-publication-dates-v1', symbol, cutoff)
    p = coverage.payload
    if (set(p) != {'symbol','org_id','start','end','identity_ref','catalogue_refs','count','scope'}
            or p['symbol'] != symbol or p['scope'] != 'issuer_publication_dates_only'):
        raise ContractViolation('issuer publication date contract changed')
    return _catalogue_metadata(repo, p, symbol, coverage.known_at)


def read_disclosure_coverage(repo, ref, symbol, cutoff):
    if repo.get_visible(ref,cutoff).dataset == 'full_text_sequence':
        return _read_sequence(repo,ref,symbol,cutoff,'full_text')
    coverage = _source(repo, ref, 'event_document_coverage', 'cninfo-disclosure-coverage-v1', symbol, cutoff)
    p = coverage.payload
    if (set(p) != {'symbol','org_id','start','end','identity_ref','catalogue_refs','document_refs','count','scope'}
            or p['symbol'] != symbol or p['scope'] != 'issuer_disclosures_not_all_media_news'):
        raise ContractViolation('disclosure coverage scope/schema changed')
    start, end, metadata = _catalogue_metadata(repo, p, symbol, coverage.known_at)
    if (not isinstance(p['document_refs'], list) or len(p['document_refs']) != len(metadata)
            or len(p['document_refs']) != len(set(p['document_refs']))):
        raise DataUnavailable('disclosure coverage omits or duplicates original documents')
    seen, documents = set(), []
    for document_ref in p['document_refs']:
        schema=repo.get_visible(document_ref,coverage.known_at).schema_version
        if schema not in {'cninfo-disclosure-pdf-v1','cninfo-disclosure-ocr-pdf-v1'}:
            raise ContractViolation('unsupported original disclosure extraction schema')
        item = _source(repo, document_ref, 'observed_event_source', schema, symbol, coverage.known_at)
        raw = item.payload; row = raw['metadata']; identity = row['announcementId']
        if identity in seen or metadata.get(identity) != row or item.source_uri != PDF_ROOT + row['adjunctUrl']:
            raise ContractViolation('original PDF disagrees with its catalogue or is duplicated')
        seen.add(identity)
        try:
            data = base64.b64decode(raw['raw_pdf_base64'], validate=True)
        except (ValueError, TypeError) as exc:
            raise ContractViolation('original disclosure PDF encoding is invalid') from exc
        if schema=='cninfo-disclosure-ocr-pdf-v1':
            from src.stock_app.pdf_ocr import bound_ocr_text,raw_pdf,OcrPolicy
            original,original_bytes=raw_pdf(repo,raw['original_pdf_ref'],item.known_at)
            transcript=bound_ocr_text(repo,raw['ocr_text_ref'],original.artifact_id,item.known_at)
            if (original_bytes!=data or original.entity_id!=symbol or original.payload['metadata']!=row
                    or original.source_uri!=item.source_uri):
                raise ContractViolation('OCR original identity/bytes differ from its disclosure')
            text,pages,parser=transcript.payload['content'],transcript.payload['page_count'],OcrPolicy().version
        else:
            text, pages, version = extract_pdf(data)
            parser='pymupdf:'+version
        if (hashlib.sha256(data).hexdigest() != raw['raw_pdf_sha256'] or text != raw['content']
                or pages != raw['page_count'] or raw['text_parser'] != parser
                or raw['title'] != row['announcementTitle']
                or raw['publication_precision'] != 'issuer_date_intraday_not_verified'
                or raw['capture_scope'] != 'official_issuer_disclosure_full_pdf'
                or item.event_time != datetime.fromtimestamp(row['announcementTime']/1000, timezone.utc)):
            raise ContractViolation('original PDF bytes/text/time do not reproduce declared disclosure values')
        compact = re.sub(r'\s+', '', text)
        if symbol not in compact and row['secName'] not in compact:
            raise ContractViolation('original PDF does not identify the requested company')
        documents.append(item)
    return coverage, start, end, documents


def _sequence_parts(repo, refs, symbol, cutoff, kind):
    if not isinstance(refs,list) or len(refs)<2 or len(refs)!=len(set(refs)):
        raise ContractViolation('issuer coverage sequence requires distinct ordered component references')
    first = previous = org_id = None
    documents = []
    metadata = {}
    seen = set()
    for ref in refs:
        item = repo.get_visible(ref,cutoff)
        if item.source_id!='cninfo_official_disclosure':
            raise ContractViolation('issuer sequence components must be native official coverages')
        if kind=='publication_dates':
            start,end,part = read_publication_dates(repo,ref,symbol,cutoff)
            ids = set(part)
            metadata.update(part)
        elif kind=='full_text':
            _,start,end,part = read_disclosure_coverage(repo,ref,symbol,cutoff)
            ids = {d.payload['metadata']['announcementId'] for d in part}
            documents.extend(part)
        else:
            raise ContractViolation('unknown issuer evidence scope')
        if previous is not None and (start!=previous+timedelta(days=1) or item.payload['org_id']!=org_id):
            raise DataUnavailable('issuer coverage series contains a gap, overlap, or different issuer')
        if ids & seen:
            raise ContractViolation('duplicate announcement across issuer coverage components')
        seen.update(ids)
        first,previous,org_id = first or start,end,item.payload['org_id']
    payload={'symbol':symbol,'org_id':org_id,'start':first.isoformat(),'end':previous.isoformat(),
             'component_refs':refs,'scope':kind,'count':len(seen)}
    return payload,first,previous,metadata if kind=='publication_dates' else documents


def _read_sequence(repo,ref,symbol,cutoff,kind):
    item=repo.get_visible(ref,cutoff)
    if ((item.domain,item.dataset,item.schema_version,item.source_id,item.entity_id)!=
            ('event',kind+'_sequence','issuer-coverage-series-v1','stockm1_source_derivation',symbol)
            or item.ingested_at>cutoff):
        raise ContractViolation('issuer coverage series type or capture time mismatch')
    payload,first,last,values=_sequence_parts(repo,item.payload['component_refs'],symbol,item.known_at,kind)
    if payload!=item.payload:
        raise ContractViolation('issuer sequence differs from its original components')
    return item,first,last,values


def combine_issuer_coverages(db_path,run_id,symbol,refs,kind,*,clock=None):
    import sqlite3
    from scenario_engine.agents.research_board import ResearchBoard
    from scenario_engine.data import MarketArtifactRepository
    from scenario_engine.sqlite_transactions import atomic_write
    from src.stock_app.quantitative_inputs import derived_artifact
    at=(clock or (lambda:datetime.now(timezone.utc)))()
    contract={'symbol':symbol,'component_refs':list(refs),'scope':kind}
    with sqlite3.connect(db_path,timeout=30) as c:
        repo,board=MarketArtifactRepository(c),ResearchBoard(c)
        root='issuer-coverage-series:'+run_id
        board.register_run(root,contract)
        payload,*_=_sequence_parts(repo,list(refs),symbol,at,kind)
        with atomic_write(c):
            saved=board.load(root,'series',contract)
            if saved:
                item=repo.get_visible(saved['coverage_ref'],at)
                if item.payload!=payload:
                    raise ContractViolation('issuer series checkpoint disagrees with source evidence')
                return saved
            item=derived_artifact(kind+'_sequence','issuer-coverage-series-v1',payload,at,domain='event',entity=symbol)
            result={'status':'source_bound_coverage_series','coverage_ref':item.artifact_id,**payload,'publication_eligible':False}
            repo.ingest_many_in_transaction((item,))
            board.save(root,'series',contract,result)
            return result


def disclosure_feature(documents, lower, upper):
    """Signed tone mass; empty COUNTED windows have the sum over an empty set.

    Issuer timestamps lack verified intraday precision, so use end of issuer
    date as the conservative event assignment boundary, including weekends.
    This is a feature timing convention, not a backdated known_at claim.
    """
    from src.stock_app.scenario_public_data import _lexicon_tone
    selected = []
    for item in documents:
        day = item.event_time.astimezone(SHANGHAI).date()
        boundary = datetime.combine(day, time.max, SHANGHAI)
        if lower < boundary <= upper:
            selected.append(item)
    value = sum(_lexicon_tone(d.payload['title'] + ' ' + d.payload['content']) for d in selected)
    return float(value), tuple(sorted(d.artifact_id for d in selected))


def bind_disclosure_features(repo, panel, definition, cutoff):
    from src.stock_app.total_return_source_binding import bound_calendar
    from src.stock_app.wealth_source_binding import _same
    market = panel.market
    expected_features = [{'feature_id': 'disclosure_tone_sum:' + s, 'symbol': s} for s in market.asset_ids]
    if (set(definition) != {'algorithm','features','coverage_refs','window','publication_time'}
            or definition['algorithm'] != 'cninfo_disclosure_tone_sum_v1'
            or definition['features'] != expected_features
            or tuple(f['feature_id'] for f in expected_features) != tuple(panel.event_feature_ids)
            or set(definition['coverage_refs']) != set(market.asset_ids)
            or definition['window'] != 'previous_close_exclusive_current_close_inclusive'
            or definition['publication_time'] != 'issuer_date_end_of_day_conservative'):
        raise ContractViolation('disclosure feature definition differs from its explicit scope')
    days, _, _ = bound_calendar(repo, market.calendar_ref, cutoff)
    coverages = {s: read_disclosure_coverage(repo, definition['coverage_refs'][s], s, cutoff) for s in market.asset_ids}
    for i, stamp in enumerate(market.timestamps):
        lower = datetime.combine(days[market.session_indices[i]-1], time(15), SHANGHAI)
        used = set()
        for j, symbol in enumerate(market.asset_ids):
            coverage, start, end, documents = coverages[symbol]
            if start > lower.date() or end < stamp.astimezone(SHANGHAI).date():
                raise DataUnavailable('disclosure coverage does not span the full requested feature window')
            repo.get_visible(coverage.artifact_id, panel.event_row_known_at[i])
            value, selected = disclosure_feature(documents, lower, stamp)
            _same(value, panel.event_features[i][j], 'source-bound disclosure tone sum')
            used.update((coverage.artifact_id, *selected))
        refs = panel.event_row_source_refs[i]
        if len(refs) != len(set(refs)) or set(refs) != used:
            raise ContractViolation('disclosure feature row dropped or added original sources')
        for ref in refs:
            repo.get_visible(ref, panel.event_row_known_at[i])
