"""Explicit, pinned OCR with immutable originals and per-page evidence.

Recognition confidence is a diagnostic, never a calibrated correctness
probability. Missing, inconsistent and unreviewed pages cannot certify text.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
import base64
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import sqlite3
import unicodedata

from scenario_engine.agents.lease import advisory_lease
from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.agents.tools import fingerprint
from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ContractViolation, DataUnavailable, ScenarioEngineError
from scenario_engine.sqlite_transactions import atomic_write
from src.stock_app.quantitative_inputs import derived_artifact

MODEL_HASHES = {
    'det': '0f8846b1d4bba223a2a2f9d9b44022fbc22cc019051a602b41a7fda9667e4cad',
    'rec': 'e09385400eaaaef34ceff54aeb7c4f0f1fe014c27fa8b9905d4709b65746562a',
    'cls': '7d3c02ef6c7da8ae08b4347cc7695b2081aae68c325d64375724ecf39c99e743',
}
PACKAGES = {'rapidocr': '3.10.0', 'onnxruntime-gpu': '1.23.2', 'PyMuPDF': '1.27.2.2',
    'nvidia-cublas-cu12':'12.9.2.10','nvidia-cuda-nvrtc-cu12':'12.9.86',
    'nvidia-cuda-runtime-cu12':'12.9.79','nvidia-cudnn-cu12':'9.27.0.42',
    'nvidia-cufft-cu12':'11.4.1.4','nvidia-curand-cu12':'10.3.10.19','nvidia-nvjitlink-cu12':'12.9.86'}
ENGINE_PARAMETERS = {'Global.text_score': 0., 'Global.max_side_len': 5000,
    'Global.log_level': 'error', 'Det.limit_side_len': 2048, 'Det.limit_type': 'max',
    'Rec.rec_batch_num': 1, 'Cls.cls_batch_num': 1,
    'EngineConfig.onnxruntime.intra_op_num_threads': 4, 'EngineConfig.onnxruntime.inter_op_num_threads': 1,
    'EngineConfig.onnxruntime.use_cuda': True, 'EngineConfig.onnxruntime.cuda_ep_cfg.use_tf32': 0,
    'EngineConfig.onnxruntime.cuda_ep_cfg.arena_extend_strategy': 'kSameAsRequested',
    'EngineConfig.onnxruntime.cuda_ep_cfg.cudnn_conv_use_max_workspace': 0,
    'EngineConfig.onnxruntime.cuda_ep_cfg.cudnn_conv_algo_search': 'HEURISTIC',
    **{section+'.'+key:value for section in ('Det','Rec','Cls') for key,value in
       {'engine_type':'onnxruntime','ocr_version':'PP-OCRv5','model_type':'server','lang_type':'ch'}.items()}}


@dataclass(frozen=True)
class OcrPolicy:
    version: str = 'ppocrv5-server-dual-render-v1'
    dpis: tuple[int, int] = (200, 300)
    min_line_score: float = .90
    min_mean_score: float = .985
    min_text_agreement: float = 1.0
    detector_max_side: int = 2048

    def __post_init__(self):
        if (self.version != 'ppocrv5-server-dual-render-v1' or tuple(self.dpis) != (200, 300)
                or self.min_line_score != .90 or self.min_mean_score != .985
                or self.min_text_agreement != 1.0 or self.detector_max_side != 2048):
            raise ContractViolation('unsupported OCR protocol or weakened page-quality limits')


def normal_text(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def page_rotation(contract, index):
    return contract.get('page_rotations', {}).get(str(index), 0)


def render_page(document, index, dpi, view_rotation=0):
    import pymupdf
    if type(view_rotation) is not int or view_rotation not in (0, 90, 180, 270):
        raise ContractViolation('OCR view rotation must be an explicit quarter turn')
    page = document[index]
    matrix = pymupdf.Matrix(dpi / 72, dpi / 72).prerotate(view_rotation)
    pix = page.get_pixmap(matrix=matrix, colorspace=pymupdf.csRGB, alpha=False)
    pix.set_dpi(dpi, dpi)
    return pix.tobytes('png'), {'width': pix.width, 'height': pix.height, 'dpi': dpi,
        'page_index': index, 'pdf_rect': list(page.rect), 'rotation': page.rotation,
        'view_rotation': view_rotation, 'render_matrix': list(matrix), 'pixmap_origin': [pix.x, pix.y]}


def validate_lines(lines, width, height):
    import math
    if not isinstance(lines, list) or not lines:
        raise DataUnavailable('OCR returned no text; empty page is not inferred')
    for line in lines:
        if (set(line) != {'text', 'score', 'box'} or not isinstance(line['text'], str)
                or not line['text'].strip() or not isinstance(line['score'], (int, float))
                or not math.isfinite(line['score']) or not 0 <= line['score'] <= 1
                or len(line['box']) != 4):
            raise ContractViolation('OCR line text, score or box is invalid')
        for xy in line['box']:
            if (len(xy) != 2 or any(not math.isfinite(v) for v in xy)
                    or not 0 <= xy[0] <= width or not 0 <= xy[1] <= height):
                raise ContractViolation('OCR coordinates leave the original rendered page')


class PinnedRapidOcr:
    def __init__(self, manifest_path, policy=OcrPolicy()):
        for package, version in PACKAGES.items():
            if importlib.metadata.version(package) != version:
                raise ContractViolation('OCR dependency differs from pinned version: ' + package)
        models = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
        if set(models) != set(MODEL_HASHES):
            raise ContractViolation('all three declared server OCR models are required')
        for kind, digest in MODEL_HASHES.items():
            model = models[kind]
            if model['sha256'] != digest or sha(Path(model['path']).read_bytes()) != digest:
                raise ContractViolation('OCR model differs from maintainer SHA-256: ' + kind)
        from rapidocr import RapidOCR, EngineType, OCRVersion, ModelType
        import onnxruntime
        import os
        # cuDNN 9.27 has a new dynamically loaded tensor-IR sublibrary that
        # ORT 1.23's preload list predates. Expose only pinned package paths
        # to this process; never edit the machine PATH or another environment.
        self._dll_directories=[]
        if os.name=='nt':
            paths=sorted({str(Path(importlib.metadata.distribution(package).locate_file(f)).parent.resolve())
                for package in PACKAGES if package.startswith('nvidia-')
                for f in importlib.metadata.distribution(package).files or [] if str(f).endswith('.dll')})
            self._dll_directories=[os.add_dll_directory(path) for path in paths]
            os.environ['PATH']=os.pathsep.join(paths+[os.environ.get('PATH','')])
        onnxruntime.preload_dlls(directory='')
        if 'CUDAExecutionProvider' not in onnxruntime.get_available_providers():
            raise DataUnavailable('declared CUDA OCR runtime unavailable; no CPU/provider fallback')
        params = dict(ENGINE_PARAMETERS)
        for section in ('Det', 'Rec', 'Cls'):
            params.update({section + '.engine_type': EngineType.ONNXRUNTIME, section + '.ocr_version': OCRVersion.PPOCRV5,
                section + '.model_type': ModelType.SERVER, section + '.lang_type': 'ch',
                section + '.model_path': models[section.lower()]['path']})
        self.identity = {'packages': PACKAGES, 'model_sha256': dict(MODEL_HASHES),
            'parameters': {k: getattr(v,'value',v) for k, v in params.items() if not k.endswith('.model_path')}}
        if self.identity['parameters']!=ENGINE_PARAMETERS:
            raise ContractViolation('OCR inference parameters differ from frozen protocol')
        self.engine = RapidOCR(params=params)
        for load in (self.engine._load_det_model,self.engine._load_cls_model,self.engine._load_rec_model):
            session=load().session.session
            if session.get_providers()[0] != 'CUDAExecutionProvider':
                raise DataUnavailable('OCR provider silently changed; inference refused')
            options=session.get_provider_options()['CUDAExecutionProvider']
            if any(options.get(k)!=str(v) for k,v in {'use_tf32':0,'cudnn_conv_use_max_workspace':0,
                    'arena_extend_strategy':'kSameAsRequested','cudnn_conv_algo_search':'HEURISTIC'}.items()):
                raise ContractViolation('CUDA runtime did not honor frozen precision/resource settings')
            session.disable_fallback()

    def recognize(self, image_bytes):
        try:
            result = self.engine(image_bytes)
        except Exception as exc:
            raise DataUnavailable('declared OCR engine failed; no fallback used',
                context={'exception_type':type(exc).__name__,'reason':str(exc)}) from exc
        if result.boxes is None or result.txts is None or result.scores is None:
            raise DataUnavailable('fixed OCR engine returned incomplete output; no alternate engine used')
        if not len(result.boxes) == len(result.txts) == len(result.scores):
            raise ContractViolation('OCR dropped line geometry or scores')
        return [{'text': text, 'score': float(score), 'box': box.tolist()}
                for box, text, score in zip(result.boxes, result.txts, result.scores)]


def page_quality(passes, policy=OcrPolicy()):
    if [p['render']['dpi'] for p in passes] != list(policy.dpis):
        raise ContractViolation('OCR page lacks both declared render passes')
    texts, findings, stats = [], [], []
    for item in passes:
        render, lines = item['render'], item['lines']
        validate_lines(lines, render['width'], render['height'])
        text = normal_text(''.join(line['text'] for line in lines)); texts.append(text)
        average = sum(line['score'] * len(normal_text(line['text'])) for line in lines) / max(1,len(text))
        lows = [i for i, line in enumerate(lines) if line['score'] < policy.min_line_score]
        stats.append({'dpi': render['dpi'], 'characters': len(text), 'weighted_score': average, 'low_score_lines': lows})
        if average < policy.min_mean_score or lows:
            findings.append({'kind': 'recognition_uncertain', 'dpi': render['dpi'], 'line_indices': lows})
    agreement = SequenceMatcher(None, texts[0], texts[1], autojunk=False).ratio()
    numbers = [re.findall(r'\d+(?:[.,]\d+)*(?:%)?', t) for t in texts]
    if agreement < policy.min_text_agreement:
        findings.append({'kind': 'render_transcripts_disagree'})
    if numbers[0] != numbers[1]:
        findings.append({'kind': 'numeric_transcripts_disagree'})
    return {'status': 'automated_page_checks_passed' if not findings else 'needs_review',
        'passes': stats, 'text_agreement': agreement, 'numeric_agreement': numbers[0] == numbers[1], 'findings': findings}


def raw_pdf(repo, ref, at):
    from src.stock_app.cninfo_disclosures import VERSION,PDF_ROOT,_validate_pdf_bytes
    item = repo.get_visible(ref, at)
    if (item.domain, item.dataset, item.schema_version, item.source_id,item.adapter_version) != (
            'event', 'disclosure_pdf_raw', 'cninfo-disclosure-raw-pdf-v1', 'cninfo_official_disclosure',VERSION):
        raise ContractViolation('OCR requires a captured original disclosure PDF')
    row=item.payload['metadata']
    if item.entity_id!=row['secCode'] or item.source_uri!=PDF_ROOT+row['adjunctUrl']:
        raise ContractViolation('OCR original security or URL differs from the captured catalogue')
    data = base64.b64decode(item.payload['raw_pdf_base64'], validate=True)
    _validate_pdf_bytes(data)
    if sha(data) != item.payload['raw_pdf_sha256']:
        raise ContractViolation('OCR original bytes differ from their immutable hash')
    return item, data


def _typed(repo, ref, dataset, schema, at):
    item=repo.get_visible(ref,at)
    if (item.domain,item.dataset,item.schema_version,item.source_id,item.adapter_version)!=(
            'event',dataset,schema,'stockm1_source_derivation','source-derived-inputs-v1'):
        raise ContractViolation('OCR audit artifact schema mismatch: '+dataset)
    return item


def verify_page(repo, page_ref, original, document, protocol, at):
    """Re-render the original and independently recompute every page check."""
    page=_typed(repo,page_ref,'pdf_ocr_page','pdf-ocr-page-v1',at)
    p=page.payload; index=p['page_index']
    if (type(index) is not int or not 0<=index<len(document) or p['original_ref']!=original.artifact_id
            or p['protocol_ref']!=protocol.artifact_id or len(p['pass_refs'])!=2
            or len(set(p['pass_refs']))!=2 or page.known_at<protocol.known_at):
        raise ContractViolation('OCR page identity/index/protocol does not match original')
    policy=OcrPolicy(**protocol.payload['policy']); passes=[]
    for ref,dpi in zip(p['pass_refs'],policy.dpis):
        item=_typed(repo,ref,'pdf_ocr_pass','pdf-ocr-pass-v1',page.known_at)
        value=item.payload; png,render=render_page(document,index,dpi,page_rotation(protocol.payload,index))
        if (value['original_ref']!=original.artifact_id or value['pdf_sha256']!=original.payload['raw_pdf_sha256']
                or value['protocol_ref']!=protocol.artifact_id or value['render']!=render
                or value['image_sha256']!=sha(png) or item.known_at<protocol.known_at):
            raise ContractViolation('OCR pass is unbound to the actual original page/render')
        passes.append(item)
    if fingerprint(page_quality([dict(p.payload) for p in passes],policy))!=fingerprint(p['quality']):
        raise ContractViolation('OCR page quality was edited instead of independently verified')
    return page,passes


def crop_hash(png, box):
    from PIL import Image
    import io,math
    rect=(math.floor(min(p[0] for p in box)),math.floor(min(p[1] for p in box)),
          math.ceil(max(p[0] for p in box)),math.ceil(max(p[1] for p in box)))
    with Image.open(io.BytesIO(png)) as image:
        crop=image.crop(rect).convert('RGB')
        return fingerprint({'rect':rect,'size':crop.size,'pixels_sha256':sha(crop.tobytes())})


def review_transcripts(passes, decisions, image_bytes, policy=OcrPolicy()):
    """Only image-bound, explicit line transcriptions can resolve disagreement.

    A review flag or a generic 'approved' note never bypasses the page checks.
    Original OCR lines and scores remain immutable; corrections are separate.
    """
    copies=[json.loads(json.dumps(dict(p))) for p in passes]
    seen=set()
    for decision in decisions:
        dpi,index=decision['dpi'],decision['line_index']
        key=(dpi,index)
        nontext=decision.get('nontext_kind')
        if (dpi not in policy.dpis or type(index) is not int or key in seen
                or not isinstance(decision.get('transcription'),str)
                or (not decision['transcription'].strip() and nontext not in {'leader_rule','scan_edge','graphical_mark'})
                or (decision['transcription'].strip() and nontext is not None)
                or not isinstance(decision.get('reason'),str) or len(decision['reason'].strip())<12):
            raise ContractViolation('visual transcription lacks a unique line, text or specific reason')
        seen.add(key); pos=list(policy.dpis).index(dpi)
        if not 0<=index<len(copies[pos]['lines']):
            raise ContractViolation('review line is outside the captured page')
        line=copies[pos]['lines'][index]
        if (decision['observed_text']!=line['text']
                or decision['image_sha256']!=copies[pos]['image_sha256']
                or decision['crop_sha256']!=crop_hash(image_bytes[pos],line['box'])):
            raise ContractViolation('visual correction is not bound to the original image/line')
        line['text']=decision['transcription']
    # Required reviews depend on ORIGINAL scores and lengths. A correction
    # cannot evade review by padding a high-score line or deleting a low one.
    for value in passes:
        dpi=value['render']['dpi']; text=normal_text(''.join(l['text'] for l in value['lines']))
        mean=sum(l['score']*len(normal_text(l['text'])) for l in value['lines'])/max(1,len(text))
        required=range(len(value['lines'])) if mean<policy.min_mean_score else (
            i for i,l in enumerate(value['lines']) if l['score']<policy.min_line_score)
        if any((dpi,i) not in seen for i in required):
            raise DataUnavailable('low-confidence OCR content still lacks image-bound visual review')
    texts=[normal_text(''.join(l['text'] for l in p['lines'])) for p in copies]
    if texts[0]!=texts[1]:
        raise DataUnavailable('page transcripts still disagree after declared visual corrections')
    return '\n'.join(l['text'] for l in copies[-1]['lines'] if l['text'].strip())


def certify_ocr_text(repo, *, ocr_run_ref, page_review_refs, at, persist=True):
    """No coverage certificate until every original page is accounted for."""
    import pymupdf
    run=_typed(repo,ocr_run_ref,'pdf_ocr_run','pdf-ocr-run-v1',at)
    original,data=raw_pdf(repo,run.payload['original_ref'],at)
    protocol=_typed(repo,run.payload['protocol_ref'],'pdf_ocr_protocol','pdf-ocr-protocol-v1',run.known_at)
    contract=protocol.payload; policy=OcrPolicy(**contract['policy'])
    if (contract['original_ref']!=original.artifact_id or contract['pdf_sha256']!=sha(data)
            or contract['engine']['packages']!=PACKAGES or contract['engine']['model_sha256']!=MODEL_HASHES
            or contract['engine']['parameters']!=ENGINE_PARAMETERS
            or protocol.known_at<original.known_at or run.payload['failures']):
        raise ContractViolation('OCR protocol or original capture is inconsistent')
    with pymupdf.open(stream=data,filetype='pdf') as document:
        refs=run.payload['page_refs']
        if len(refs)!=len(document) or len(set(refs))!=len(document) or run.payload['page_count']!=len(document):
            raise DataUnavailable('OCR does not contain every original PDF page exactly once')
        if set(page_review_refs)-{str(i) for i in range(len(document))}:
            raise ContractViolation('OCR review contains an unrequested page')
        texts=[]
        for index,ref in enumerate(refs):
            page,passes=verify_page(repo,ref,original,document,protocol,run.known_at)
            if page.payload['page_index']!=index:
                raise ContractViolation('OCR pages were reordered or omitted')
            if str(index) in page_review_refs:
                review=_typed(repo,page_review_refs[str(index)],'pdf_ocr_page_review','pdf-ocr-page-review-v1',at)
                if (review.payload['page_ref']!=ref or review.known_at<page.known_at
                        or review.payload['reviewer_kind']!='visual_transcription'):
                    raise ContractViolation('OCR visual review references another page/version')
                pngs=[render_page(document,index,dpi,page_rotation(contract,index))[0] for dpi in policy.dpis]
                text=review_transcripts([dict(p.payload) for p in passes],review.payload['decisions'],pngs,policy)
            elif page.payload['quality']['status']=='automated_page_checks_passed':
                text='\n'.join(l['text'] for l in passes[-1].payload['lines'])
            else:
                raise DataUnavailable('OCR page still requires visual review',context={'page_index':index,'page_ref':ref})
            texts.append(text)
    payload={'original_ref':original.artifact_id,'pdf_sha256':sha(data),'ocr_run_ref':ocr_run_ref,
        'page_refs':list(refs),'page_review_refs':dict(page_review_refs),'page_texts':texts,'page_count':len(texts),
        'content':'\n\n'.join(texts),'scope':'ocr_transcription_not_verified_financial_table_semantics',
        'publication_eligible':False}
    result=derived_artifact('pdf_ocr_text','pdf-ocr-text-v1',payload,at,domain='event')
    if persist:repo.ingest_many((result,))
    return result


def record_page_review(repo, *, page_ref, decisions, at):
    import pymupdf
    page=_typed(repo,page_ref,'pdf_ocr_page','pdf-ocr-page-v1',at)
    original,data=raw_pdf(repo,page.payload['original_ref'],at)
    protocol=_typed(repo,page.payload['protocol_ref'],'pdf_ocr_protocol','pdf-ocr-protocol-v1',page.known_at)
    with pymupdf.open(stream=data,filetype='pdf') as doc:
        _,passes=verify_page(repo,page_ref,original,doc,protocol,at)
        index=page.payload['page_index']
        pngs=[render_page(doc,index,dpi,page_rotation(protocol.payload,index))[0] for dpi in OcrPolicy().dpis]
        review_transcripts([dict(p.payload) for p in passes],decisions,pngs)
    result=derived_artifact('pdf_ocr_page_review','pdf-ocr-page-review-v1',
        {'page_ref':page_ref,'reviewer_kind':'visual_transcription','decisions':decisions},at,domain='event')
    repo.ingest_many((result,));return result


def bound_ocr_text(repo, ref, original_ref, at):
    item=_typed(repo,ref,'pdf_ocr_text','pdf-ocr-text-v1',at)
    if item.payload['original_ref']!=original_ref:
        raise ContractViolation('OCR transcript belongs to another original PDF')
    recomputed=certify_ocr_text(repo,ocr_run_ref=item.payload['ocr_run_ref'],
        page_review_refs=item.payload['page_review_refs'],at=item.known_at,persist=False)
    if fingerprint(recomputed.payload)!=fingerprint(item.payload):
        raise ContractViolation('OCR text differs from independent original/page/review recomputation')
    return item


def run_pdf_ocr(db_path, run_id, *, original_ref, manifest_path, output_dir, page_rotations=None, engine=None, clock=None):
    import pymupdf
    clock = clock or (lambda: datetime.now(timezone.utc))
    policy = OcrPolicy()
    root = 'pdf-ocr:' + run_id
    out = Path(output_dir); out.mkdir(parents=True, exist_ok=True)
    with advisory_lease(db_path, root) as lost, sqlite3.connect(db_path, timeout=30) as c:
        repo, board = MarketArtifactRepository(c), ResearchBoard(c)
        original, data = raw_pdf(repo, original_ref, clock())
        engine = engine or PinnedRapidOcr(manifest_path, policy)
        with pymupdf.open(stream=data, filetype='pdf') as doc:
            if doc.is_repaired or doc.is_encrypted or not 1 <= len(doc) <= 1500:
                raise DataUnavailable('OCR original PDF is repaired/encrypted or has unsupported page coverage')
            rotations = dict(page_rotations or {})
            if any(not isinstance(k,str) or k not in {str(i) for i in range(len(doc))}
                   or type(v) is not int or v not in (0,90,180,270) for k,v in rotations.items()):
                raise ContractViolation('OCR rotation plan has an invalid original page or angle')
            contract = {'original_ref': original_ref, 'pdf_sha256': sha(data), 'policy': asdict(policy),
                'engine': engine.identity, 'page_rotations': rotations}
            board.register_run(root, contract)
            complete = board.load(root, 'complete', contract)
            if complete is not None:
                item = repo.get_visible(complete['artifact_id'], clock())
                return {**item.payload, 'ocr_run_ref': item.artifact_id}
            def checkpoint(key, item):
                with atomic_write(c):
                    if lost.is_set():
                        raise ContractViolation('OCR execution lease lost before checkpoint')
                    repo.ingest_many_in_transaction((item,))
                    board.save(root, key, contract, {'artifact_id': item.artifact_id})
            protocol = board.load(root, 'protocol', contract)
            if not protocol:
                item = derived_artifact('pdf_ocr_protocol', 'pdf-ocr-protocol-v1', contract, clock(), domain='event')
                checkpoint('protocol', item)
                protocol = {'artifact_id': item.artifact_id}
            page_refs, failures = [], []
            for index in range(len(doc)):
                if lost.is_set():
                    raise ContractViolation('OCR execution lease lost')
                saved = board.load(root, 'page:' + str(index), contract)
                if saved:
                    item = repo.get_visible(saved['artifact_id'], clock())
                    page_refs.append(item.artifact_id)
                    continue
                passes = []
                try:
                    for dpi in policy.dpis:
                        key = f'pass:{index}:{dpi}'
                        snapshot = board.load(root, key, contract)
                        png, render = render_page(doc, index, dpi,page_rotation(contract,index))
                        path = out / f'page-{index+1:03}-{dpi}.png'
                        if not path.exists():
                            path.write_bytes(png)
                        elif sha(path.read_bytes()) != sha(png):
                            raise ContractViolation('OCR review image changed since original rendering')
                        if snapshot:
                            current = repo.get_visible(snapshot['artifact_id'], clock())
                            if current.payload['image_sha256'] != sha(png):
                                raise ContractViolation('OCR renderer no longer reproduces the checkpoint image')
                        else:
                            lines = engine.recognize(png)
                            validate_lines(lines, render['width'], render['height'])
                            value = {'original_ref': original_ref, 'pdf_sha256': sha(data),
                                'protocol_ref': protocol['artifact_id'], 'render': render, 'image_sha256': sha(png), 'lines': lines}
                            current = derived_artifact('pdf_ocr_pass', 'pdf-ocr-pass-v1', value, clock(), domain='event')
                            if lost.is_set():
                                raise ContractViolation('OCR execution lease lost after inference')
                            checkpoint(key, current)
                        passes.append(current)
                    quality = page_quality([dict(p.payload) for p in passes], policy)
                    payload = {'original_ref': original_ref, 'protocol_ref': protocol['artifact_id'],
                        'page_index': index, 'pass_refs': [p.artifact_id for p in passes], 'quality': quality}
                    item = derived_artifact('pdf_ocr_page', 'pdf-ocr-page-v1', payload, clock(), domain='event')
                    checkpoint('page:' + str(index), item)
                    page_refs.append(item.artifact_id)
                    (out/f'page-{index+1:03}.json').write_text(json.dumps({'page':payload,'passes':[dict(p.payload) for p in passes]},ensure_ascii=False,indent=2),encoding='utf-8')
                    print(json.dumps({'page': index+1, 'total': len(doc), **quality},ensure_ascii=False), flush=True)
                except (ScenarioEngineError, ValueError, TypeError, KeyError) as exc:
                    failure = {'page_index': index, 'reason': str(exc), 'error_code': getattr(exc,'code','ocr_failure')}
                    item = derived_artifact('pdf_ocr_failure', 'pdf-ocr-failure-v1', {**failure,'original_ref':original_ref},clock(),domain='event')
                    repo.ingest_many((item,)); failures.append({**failure,'failure_ref':item.artifact_id})
                    # Persistent failures stop rather than repeating a broken engine 150 times.
                    break
            review_pages = [repo.get_visible(r,clock()).payload['page_index'] for r in page_refs
                if repo.get_visible(r,clock()).payload['quality']['status'] != 'automated_page_checks_passed']
            result = {'status':'ocr_pages_captured' if len(page_refs)==len(doc) else 'data_incomplete',
                'original_ref': original_ref, 'pdf_sha256': sha(data), 'protocol_ref': protocol['artifact_id'],
                'page_count':len(doc),'page_refs':page_refs,'review_page_indices':review_pages,'failures':failures,
                'text_certified':False,'publication_eligible':False}
            item=derived_artifact('pdf_ocr_run','pdf-ocr-run-v1',result,clock(),domain='event')
            if result['status'] == 'ocr_pages_captured':
                checkpoint('complete', item)
            else:
                repo.ingest_many((item,))
            result['ocr_run_ref']=item.artifact_id
            return result
