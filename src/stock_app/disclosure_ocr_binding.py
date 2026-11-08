"""Explicit OCR completion of an archive, preserving previous failure records."""
from datetime import datetime,timezone

from scenario_engine.errors import DataUnavailable
from src.stock_app.cninfo_disclosures import artifact
from src.stock_app.pdf_ocr import bound_ocr_text,raw_pdf,OcrPolicy


def make_ocr_document(repo, *, original_ref, ocr_text_ref, at):
    original,data=raw_pdf(repo,original_ref,at)
    text=bound_ocr_text(repo,ocr_text_ref,original_ref,at)
    raw=original.payload; row=raw['metadata']
    if not text.payload['content'].strip():
        raise DataUnavailable('certified OCR contains no full text')
    payload={'metadata':row,'title':row['announcementTitle'],'content':text.payload['content'],
        'raw_pdf_base64':raw['raw_pdf_base64'],'raw_pdf_sha256':raw['raw_pdf_sha256'],
        'page_count':text.payload['page_count'],'text_parser':OcrPolicy().version,
        'original_pdf_ref':original_ref,'ocr_text_ref':ocr_text_ref,
        'capture_scope':'official_issuer_disclosure_full_pdf','publication_precision':'issuer_date_intraday_not_verified'}
    result=artifact('observed_event_source','cninfo-disclosure-ocr-pdf-v1',original.entity_id,payload,
        original.source_uri,at,event_time=datetime.fromtimestamp(row['announcementTime']/1000,timezone.utc))
    repo.ingest_many((result,));return result


def complete_ocr_archive(repo, *, acquisition_result, ocr_text_refs, at):
    """Create a new coverage; never overwrite the failed acquisition checkpoint."""
    from src.stock_app.disclosure_source_binding import read_disclosure_coverage
    raw_refs=acquisition_result.get('raw_only_refs',[])
    if (not raw_refs or set(ocr_text_refs)!=set(raw_refs) or len(raw_refs)!=len(set(raw_refs))
            or any(not f.get('context',{}).get('raw_pdf_preserved') for f in acquisition_result['failures'])):
        raise DataUnavailable('all acquisition failures must be explicitly resolved by original-bound OCR')
    first=repo.get_visible(acquisition_result['catalogue_refs'][0],at)
    p=first.payload; start,end=p['params']['seDate'].split('~'); symbol,org=p['params']['stock'].split(',')
    docs=list(acquisition_result['document_refs'])
    for original_ref in raw_refs:
        docs.append(make_ocr_document(repo,original_ref=original_ref,ocr_text_ref=ocr_text_refs[original_ref],at=at).artifact_id)
    payload={'symbol':symbol,'org_id':org,'start':start,'end':end,'identity_ref':p['identity_ref'],
        'catalogue_refs':acquisition_result['catalogue_refs'],'document_refs':docs,
        'count':p['raw']['totalAnnouncement'],'scope':'issuer_disclosures_not_all_media_news'}
    candidate=artifact('event_document_coverage','cninfo-disclosure-coverage-v1',symbol,payload,
        'stockm1://cninfo-disclosure-coverage',at)
    # A failed verifier must not leave an apparently usable coverage artifact.
    class CandidateRepository:
        def get_visible(self,ref,cutoff):
            if ref==candidate.artifact_id and candidate.known_at<=cutoff:return candidate
            return repo.get_visible(ref,cutoff)
    read_disclosure_coverage(CandidateRepository(),candidate.artifact_id,symbol,at)
    repo.ingest_many((candidate,))
    return {'status':'source_bound_ocr_disclosure_coverage','coverage_ref':candidate.artifact_id,
        'document_count':len(docs),'ocr_original_refs':raw_refs,'publication_eligible':False,
        'historical_visibility_validated':False}
