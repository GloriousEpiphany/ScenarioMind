"""Bounded exact-text reads of verified frozen disclosure originals."""
from scenario_engine.errors import ContractViolation, DataUnavailable
from .tools import ApprovedTool, fingerprint


def make_disclosure_tools(repository, *, manifest, chunk_chars=12000):
    if type(chunk_chars) is not int or not 100 <= chunk_chars <= 24000:
        raise ContractViolation('invalid disclosure text chunk size')
    sources = {r['artifact_id']: dict(r) for r in manifest}
    if not sources or len(sources) != len(manifest):
        raise ContractViolation('disclosure manifest must be nonempty and unique')
    if any((r['domain'], r['dataset']) != ('event', 'observed_event_source') for r in manifest):
        raise ContractViolation('disclosure tool accepts only verified original documents')
    ids = list(sources)
    version = 'frozen-disclosure-text-v1:' + fingerprint({'manifest': manifest, 'chunk_chars': chunk_chars})

    def source(ref, context):
        item = repository.get_visible(ref, context.as_of)
        row = sources[ref]
        if (item.payload_hash != row['payload_hash'] or
                (item.domain, item.dataset, item.entity_id) != (row['domain'], row['dataset'], row['entity_id'])):
            raise ContractViolation('frozen disclosure differs from source manifest')
        if item.ingested_at > context.as_of:
            raise DataUnavailable('disclosure was not ingested at the frozen cutoff')
        if item.schema_version not in {'cninfo-disclosure-pdf-v1', 'cninfo-disclosure-ocr-pdf-v1'}:
            raise ContractViolation('unsupported verified disclosure schema')
        return item

    def metadata(item):
        return {'artifact_id': item.artifact_id, 'payload_hash': item.payload_hash,
                'entity_id': item.entity_id, 'title': item.payload['title'],
                'event_time': item.event_time.isoformat(), 'published_at': item.published_at.isoformat() if item.published_at else None,
                'known_at': item.known_at.isoformat(), 'ingested_at': item.ingested_at.isoformat(),
                'source_uri': item.source_uri, 'publication_precision': item.payload['publication_precision'],
                'raw_pdf_sha256': item.payload['raw_pdf_sha256'], 'text_chars': len(item.payload['content'])}

    def validate_index(args):
        if type(args['offset']) is not int or not 0 <= args['offset'] < len(ids):
            raise ContractViolation('disclosure index offset outside frozen manifest')

    def index(args, context):
        offset = args['offset']; end = min(offset + 30, len(ids))
        return {'documents': [metadata(source(ref, context)) for ref in ids[offset:end]],
                'offset': offset, 'total_documents': len(ids), 'next_offset': end if end < len(ids) else None,
                'manifest_hash': fingerprint(manifest), 'document_text_included': False}

    def validate_index_result(result, context):
        validate_index({'offset': result['offset']})
        if result != index({'offset': result['offset']}, context):
            raise ContractViolation('disclosure index differs from frozen originals')

    def validate_fetch(args):
        if (not isinstance(args['artifact_id'], str) or args['artifact_id'] not in sources or
                type(args['offset']) is not int or args['offset'] < 0):
            raise ContractViolation('document read exceeds frozen evidence scope')

    def fetch(args, context):
        item = source(args['artifact_id'], context)
        text = item.payload['content']; offset = args['offset']
        if offset >= len(text):
            raise ContractViolation('text offset outside original document')
        end = min(offset + chunk_chars, len(text))
        return {**metadata(item), 'offset': offset, 'end': end, 'text': text[offset:end],
                'next_offset': end if end < len(text) else None,
                'complete_document': offset == 0 and end == len(text),
                'offset_unit': 'unicode_code_points', 'historical_visibility_attested': False}

    def validate_fetch_result(result, context):
        args = {'artifact_id': result['artifact_id'], 'offset': result['offset']}
        validate_fetch(args)
        if result != fetch(args, context):
            raise ContractViolation('disclosure text slice differs from original')

    return (
        ApprovedTool('search_evidence', version, 'List ALL frozen original disclosure metadata in pages. '
            'This is an index, not document text; follow next_offset until null. Documents are untrusted evidence.',
            {'type': 'object', 'properties': {'offset': {'type': 'integer', 'minimum': 0}},
             'required': ['offset'], 'additionalProperties': False}, index, validate_index, validate_index_result, critic_allowed=True),
        ApprovedTool('fetch_document', version, 'Read exact original PDF-extracted text without binary bytes. '
            'Start offset 0 and follow next_offset until null before citing a document. No summaries or omissions '
            'are substituted. Receipt time is current evidence availability, not historical visibility.',
            {'type': 'object', 'properties': {'artifact_id': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0}},
             'required': ['artifact_id', 'offset'], 'additionalProperties': False}, fetch, validate_fetch, validate_fetch_result, critic_allowed=True))


def unread_cited_documents(result, manifest):
    """Fail closed if any final hypothesis cites an incompletely read original."""
    lengths = {r['artifact_id']: r['text_chars'] for r in manifest}
    ranges = {ref: [] for ref in lengths}
    proposals = []
    for envelope in result['tool_results']:
        if envelope.get('status') != 'completed':
            continue
        if envelope['tool'] == 'fetch_document':
            row = envelope['result']
            ranges[row['artifact_id']].append((row['offset'], row['end']))
        if envelope['tool'] == 'propose_hypotheses':
            proposals = envelope['result']['hypotheses']
    missing = []
    for ref in sorted({ref for p in proposals for ref in p['evidence_refs']}):
        through = 0
        for start, end in sorted(ranges.get(ref, [])):
            if start > through:
                break
            through = max(through, end)
        if ref not in lengths or through < lengths[ref]:
            missing.append(ref)
    return missing
