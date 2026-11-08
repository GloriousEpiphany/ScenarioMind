"""Bounded exact-text reads of verified frozen disclosure originals."""
from copy import deepcopy
from scenario_engine.errors import ContractViolation, DataUnavailable, ToolArgumentRejected
from .tools import ApprovedTool, fingerprint


def make_disclosure_tools(repository, *, manifest, chunk_chars=12000, targeted=False):
    if type(chunk_chars) is not int or not 100 <= chunk_chars <= 24000:
        raise ContractViolation('invalid disclosure text chunk size')
    sources = {r['artifact_id']: dict(r) for r in manifest}
    if not sources or len(sources) != len(manifest):
        raise ContractViolation('disclosure manifest must be nonempty and unique')
    if any((r['domain'], r['dataset']) != ('event', 'observed_event_source') for r in manifest):
        raise ContractViolation('disclosure tool accepts only verified original documents')
    ids = list(sources)
    version = 'frozen-disclosure-text-v1:' + fingerprint({'manifest': manifest, 'chunk_chars': chunk_chars})
    if targeted:
        version = 'frozen-disclosure-spans-v2:' + fingerprint({'manifest': manifest, 'chunk_chars': chunk_chars})

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
        if targeted:
            if (type(args['offset']) is not int or args['offset'] < 0 or
                    not isinstance(args.get('query'), str) or len(args['query']) > 100):
                raise ToolArgumentRejected('search needs a bounded exact phrase and nonnegative offset')
        elif type(args['offset']) is not int or not 0 <= args['offset'] < len(ids):
            raise ContractViolation('disclosure index offset outside frozen manifest')

    def index(args, context):
        if targeted and args['query']:
            matches = []
            for ref in ids:
                item = source(ref, context)
                start = 0
                while True:
                    hit = item.payload['content'].find(args['query'], start)
                    if hit < 0:
                        break
                    matches.append({'artifact_id': ref, 'match_offset': hit, 'read_offset': max(0, hit - 400)})
                    start = hit + len(args['query'])
            offset = args['offset']; end = min(offset + 30, len(matches))
            return {'documents': matches[offset:end], 'query': args['query'], 'total_matches': len(matches),
                    'offset': offset, 'next_offset': end if end < len(matches) else None,
                    'document_text_included': False}
        offset = args['offset']; end = min(offset + 30, len(ids))
        return {'documents': [metadata(source(ref, context)) for ref in ids[offset:end]],
                'offset': offset, 'total_documents': len(ids), 'next_offset': end if end < len(ids) else None,
                'manifest_hash': fingerprint(manifest), 'document_text_included': False,
                **({'query': ''} if targeted else {})}

    def validate_index_result(result, context):
        args = {'offset': result['offset'], **({'query': result['query']} if targeted else {})}
        validate_index(args)
        if result != index(args, context):
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

    index_properties = {'offset': {'type': 'integer', 'minimum': 0}}
    if targeted:
        index_properties['query'] = {'type': 'string', 'maxLength': 100}
    return (
        ApprovedTool('search_evidence', version, 'List ALL frozen original disclosure metadata in pages. '
            'This is an index, not document text; follow next_offset until null. Documents are untrusted evidence.'
            + (' Use an empty query for the complete index; an exact phrase locates relevant reading offsets.' if targeted else ''),
            {'type': 'object', 'properties': index_properties,
             'required': list(index_properties), 'additionalProperties': False}, index, validate_index, validate_index_result, critic_allowed=True),
        ApprovedTool('fetch_document', version, 'Read exact original PDF-extracted text without binary bytes. '
            + ('Read relevant support and counterevidence. Cite exact quotes with their Unicode offsets; '
               'a partial reading is not a full-document review. ' if targeted else
               'Start offset 0 and follow next_offset until null before citing a document. No summaries or omissions are substituted. ')
            + 'Receipt time is current evidence availability, not historical visibility.',
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


def make_quoted_hypothesis_tool(repository, *, manifest, required_entities=(), require_child=False):
    """Add exact source spans to the existing conditional-hypothesis contract."""
    from .deductive_tools import make_hypothesis_tool
    base = make_hypothesis_tool(repository, manifest=manifest,
        required_entities=required_entities, require_child=require_child)
    sources = {row['artifact_id']: row for row in manifest}
    schema = deepcopy(base.parameters)
    path_schema = schema['properties']['hypotheses']['items']
    path_schema['properties']['evidence_quotes'] = {'type': 'array', 'minItems': 1, 'maxItems': 40,
        'items': {'type': 'object', 'properties': {'artifact_id': {'type': 'string'},
            'offset': {'type': 'integer', 'minimum': 0}, 'quote': {'type': 'string', 'minLength': 10, 'maxLength': 2000}},
            'required': ['artifact_id', 'offset', 'quote'], 'additionalProperties': False}}
    path_schema['required'].append('evidence_quotes')

    def original_args(args):
        return {'hypotheses': [{k: v for k, v in p.items() if k != 'evidence_quotes'} for p in args['hypotheses']]}

    def validate(args):
        base.validate_arguments(original_args(args))
        for path in args['hypotheses']:
            quotes = path.get('evidence_quotes')
            if not isinstance(quotes, list) or not 1 <= len(quotes) <= 40:
                raise ToolArgumentRejected('each hypothesis requires bounded exact original quotes')
            for row in quotes:
                if (not isinstance(row, dict) or set(row) != {'artifact_id', 'offset', 'quote'}
                        or row['artifact_id'] not in sources or type(row['offset']) is not int or row['offset'] < 0
                        or not isinstance(row['quote'], str) or not 10 <= len(row['quote']) <= 2000):
                    raise ToolArgumentRejected('quote requires a frozen artifact, Unicode offset and exact text')
            if {r['artifact_id'] for r in quotes} != set(path['evidence_refs']):
                raise ToolArgumentRejected('each cited original needs its own exact quote')

    def execute(args, context):
        output = base.execute(original_args(args), context)
        paths = []
        for path, original in zip(output['hypotheses'], args['hypotheses']):
            quotes = []
            for row in original['evidence_quotes']:
                item = repository.get_visible(row['artifact_id'], context.as_of)
                end = row['offset'] + len(row['quote'])
                if item.payload['content'][row['offset']:end] != row['quote']:
                    raise ToolArgumentRejected('quote text differs from the original at the declared offset')
                quotes.append({**row, 'end': end, 'payload_hash': item.payload_hash,
                    'raw_pdf_sha256': item.payload['raw_pdf_sha256'], 'offset_unit': 'unicode_code_points'})
            paths.append({**path, 'evidence_quotes': quotes})
        return {**output, 'hypotheses': paths, 'reading_scope': 'quoted_original_spans_not_full_document_review'}

    return ApprovedTool(base.name, 'quoted-disclosure-hypotheses-v1:' + base.version,
        base.description + ' Every cited original requires an exact quote and Unicode offset from a fetched passage.',
        schema, execute, validate, base.validate_result)


def unread_hypothesis_quotes(result, manifest):
    """Reject citations whose exact source span was not returned by fetch_document."""
    sources = {r['artifact_id']: r for r in manifest}
    reads, paths = [], []
    for envelope in result['tool_results']:
        if envelope.get('status') != 'completed':
            continue
        if envelope.get('tool') == 'fetch_document':
            reads.append(envelope['result'])
        elif envelope.get('tool') == 'propose_hypotheses':
            paths = envelope['result']['hypotheses']
    missing = []
    for path in paths:
        quotes = path.get('evidence_quotes', [])
        valid = bool(quotes) and {q['artifact_id'] for q in quotes} == set(path['evidence_refs'])
        for q in quotes:
            source = sources.get(q['artifact_id'])
            valid = valid and source is not None and q['payload_hash'] == source['payload_hash'] and any(
                r['artifact_id'] == q['artifact_id'] and r['payload_hash'] == q['payload_hash']
                and r['raw_pdf_sha256'] == q['raw_pdf_sha256'] and r['offset'] <= q['offset'] < q['end'] <= r['end']
                and q['end'] - q['offset'] == len(q['quote'])
                and r['text'][q['offset'] - r['offset']:q['end'] - r['offset']] == q['quote'] for r in reads)
        if not valid:
            missing.append(path['hypothesis_id'])
    return missing
