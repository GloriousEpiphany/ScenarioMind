"""Explicit source preparation commands; never model calls or trade submission."""
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3

import click

from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ScenarioEngineError


def _save(value, output, success):
    Path(output).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    summary={k:v for k,v in value.items() if k not in {'binding','folds','stages','document_refs','catalogue_refs','page_refs'}}
    if 'stages' in value:
        summary['stage_statuses']={k:v['status'] for k,v in value['stages'].items()}
    if 'horizons' in summary and isinstance(summary['horizons'],dict):
        summary['horizons']={k:{field:data for field,data in v.items() if field!='folds'}
                            for k,v in summary['horizons'].items()}
    click.echo(json.dumps(summary, ensure_ascii=False))
    if value['status'] not in success:
        raise click.exceptions.Exit(2)


def register_quantitative_commands(cli):
    @cli.command('register-issuer-evaluation')
    @click.option('--db-path',required=True,type=click.Path(dir_okay=False))
    @click.option('--symbol',required=True)
    @click.option('--output',required=True,type=click.Path(dir_okay=False))
    def register_evaluation(db_path,symbol,output):
        """按当前真实登记时点固定每个期限的头 50 个非重叠评价窗口。"""
        from src.stock_app.issuer_evaluation import register_issuer_evaluation
        _save(register_issuer_evaluation(db_path,symbol),output,{'registered'})

    @cli.command('audit-issuer-evaluation')
    @click.option('--db-path',required=True,type=click.Path(exists=True,dir_okay=False))
    @click.option('--protocol-ref',required=True)
    @click.option('--catalogue-ref',required=True)
    @click.option('--sh-benchmark-ref',required=True)
    @click.option('--sz-benchmark-ref',required=True)
    @click.option('--output',required=True,type=click.Path(dir_okay=False))
    def issuer_evaluation(db_path,protocol_ref,catalogue_ref,sh_benchmark_ref,sz_benchmark_ref,output):
        """保留未出具、未到期和已结算的原点，并登记样本缺口。"""
        from src.stock_app.issuer_evaluation import audit_issuer_evaluation
        try:
            result=audit_issuer_evaluation(db_path,protocol_ref,catalogue_ref,
                {'1.000001':sh_benchmark_ref,'0.399001':sz_benchmark_ref})
        except ScenarioEngineError as exc:
            result={'status':'blocked','reason':str(exc),'publication_eligible':False}
        _save(result,output,{'diagnostics_available'})

    @cli.command('acquire-archived-quote-page')
    @click.option('--db-path',required=True,type=click.Path(dir_okay=False))
    @click.option('--symbol',required=True)
    @click.option('--historical-cutoff',required=True,help='带时区的 ISO 时间')
    @click.option('--output',required=True,type=click.Path(dir_okay=False))
    def archived_quote(db_path,symbol,historical_cutoff,output):
        """区分第三方网页存档时间、本系统实际入库时间及原点可见性。"""
        from src.stock_app.archived_quote import acquire_archived_quote
        try:
            result=acquire_archived_quote(db_path,symbol,datetime.fromisoformat(historical_cutoff))
        except (ScenarioEngineError,ValueError,TypeError) as exc:
            result={'status':'blocked','reason':str(exc),'context':getattr(exc,'context',{}),'publication_eligible':False}
        _save(result,output,{'external_snapshot_before_cutoff'})

    @cli.command('acquire-equity-history')
    @click.option('--db-path', required=True, type=click.Path(dir_okay=False))
    @click.option('--run-id', required=True)
    @click.option('--symbol', required=True)
    @click.option('--output', required=True, type=click.Path(dir_okay=False))
    def acquire_equity_history(db_path, run_id, symbol, output):
        """保存完整深市股本结构响应，供跨来源逐股核对。"""
        from scenario_engine.agents.research_board import ResearchBoard
        from scenario_engine.sqlite_transactions import atomic_write
        from src.stock_app.share_structure_binding import SCHEMA, equity_url, fetch_equity_history, read_equity
        try:
            spec = {'source_uri': equity_url(symbol), 'schema': SCHEMA, 'symbol': symbol}
            root = 'equity-history:' + run_id
            with sqlite3.connect(db_path, timeout=30) as conn:
                repo, board = MarketArtifactRepository(conn), ResearchBoard(conn)
                board.register_run(root, spec)
                result = board.load(root, 'source', spec)
                if result is None:
                    item = fetch_equity_history(symbol)
                    with atomic_write(conn):
                        result = board.load(root, 'source', spec)
                        if result is None:
                            repo.ingest_many_in_transaction((item,))
                            result = {'status': 'acquired', 'equity_history_ref': item.artifact_id,
                                      'publication_eligible': False}
                            board.save(root, 'source', spec, result)
                read_equity(repo, result['equity_history_ref'], symbol, datetime.now(timezone.utc))
        except ScenarioEngineError as exc:
            result = {'status': 'blocked', 'reason': str(exc), 'publication_eligible': False}
        _save(result, output, {'acquired'})

    @cli.command('bind-transfer-notices')
    @click.option('--db-path',required=True,type=click.Path(exists=True,dir_okay=False))
    @click.option('--run-id',required=True)
    @click.option('--inputs',required=True,type=click.Path(exists=True,dir_okay=False))
    @click.option('--output',required=True,type=click.Path(dir_okay=False))
    def bind_transfers(db_path,run_id,inputs,output):
        """用官方实施公告给出的实际分红前后股本复核送转股。"""
        from scenario_engine.agents.research_board import ResearchBoard
        from scenario_engine.sqlite_transactions import atomic_write
        from src.stock_app.cninfo_actions import make_coverage, read_coverage
        try:
            spec=json.loads(Path(inputs).read_text(encoding='utf-8-sig'))
            optional = {'period_exercise_refs', 'equity_history_ref', 'implementation_notice_refs'}
            if (set(spec) - optional != {'symbol', 'start', 'end', 'catalogue_refs'}
                    or not (spec.get('implementation_notice_refs') or spec.get('equity_history_ref'))):
                raise ValueError('explicit transfer notices or an equity history reference required')
            with sqlite3.connect(db_path,timeout=30) as c:
                repo,board=MarketArtifactRepository(c),ResearchBoard(c)
                root='transfer-notices:'+run_id
                board.register_run(root,spec)
                at=datetime.now(timezone.utc)
                saved=board.load(root,'coverage',spec)
                if saved:
                    read_coverage(repo,saved['coverage_ref'],at)
                    result=saved
                else:
                    try:
                        item=make_coverage(repo,spec['catalogue_refs'],spec['symbol'],date.fromisoformat(spec['start']),
                            date.fromisoformat(spec['end']),at=at, notice_refs=spec.get('implementation_notice_refs'),
                            period_refs=spec.get('period_exercise_refs'), equity_ref=spec.get('equity_history_ref'))
                    except ScenarioEngineError as exc:
                        from scenario_engine.agents.tools import fingerprint
                        from src.stock_app.quantitative_inputs import derived_artifact
                        result={'status':'blocked','reason':str(exc),'context':exc.context,'publication_eligible':False}
                        key='blocked:'+fingerprint(result)
                        with atomic_write(c):
                            saved=board.load(root,key,spec)
                            if saved:
                                result=saved
                            else:
                                diagnostic=derived_artifact('transfer_evidence_gap','transfer-evidence-gap-v1',
                                    {'request':spec,**result},at)
                                repo.ingest_many_in_transaction((diagnostic,))
                                result['diagnostic_ref']=diagnostic.artifact_id
                                board.save(root,key,spec,result)
                    else:
                        result={'status':'source_bound_action_coverage','coverage_ref':item.artifact_id,'publication_eligible':False}
                        with atomic_write(c):
                            saved=board.load(root,'coverage',spec)
                            if saved: result=saved
                            else:
                                repo.ingest_many_in_transaction((item,))
                                board.save(root,'coverage',spec,result)
        except (ScenarioEngineError,ValueError,TypeError,KeyError) as exc:
            result={'status':'blocked','reason':str(exc),'context':getattr(exc,'context',{}),'publication_eligible':False}
        _save(result,output,{'source_bound_action_coverage'})

    @cli.command('combine-issuer-coverages')
    @click.option('--db-path',required=True,type=click.Path(exists=True,dir_okay=False))
    @click.option('--run-id',required=True)
    @click.option('--symbol',required=True)
    @click.option('--scope',required=True,type=click.Choice(['publication_dates','full_text']))
    @click.option('--ref',multiple=True,required=True)
    @click.option('--output',required=True,type=click.Path(dir_okay=False))
    def combine_coverages(db_path,run_id,symbol,scope,ref,output):
        """按日期拼接同一发行人相邻的完整原件窗口或公告日期窗口。"""
        from src.stock_app.disclosure_source_binding import combine_issuer_coverages
        try:
            result=combine_issuer_coverages(db_path,run_id,symbol,ref,scope)
        except ScenarioEngineError as exc:
            result={'status':'blocked','reason':str(exc),'publication_eligible':False}
        _save(result,output,{'source_bound_coverage_series'})

    @cli.command('run-issuer-outlook')
    @click.option('--db-path',required=True,type=click.Path(dir_okay=False))
    @click.option('--run-id',required=True)
    @click.option('--symbol',required=True)
    @click.option('--market-start',required=True,type=click.DateTime(formats=['%Y-%m-%d']))
    @click.option('--event-start',required=True,type=click.DateTime(formats=['%Y-%m-%d']))
    @click.option('--origin',required=True,type=click.DateTime(formats=['%Y-%m-%d']))
    @click.option('--frozen-inputs',type=click.Path(exists=True,dir_okay=False))
    @click.option('--event-run-id')
    @click.option('--scenario-run-id')
    @click.option('--output',required=True,type=click.Path(dir_okay=False))
    def outlook(db_path,run_id,symbol,market_start,event_start,origin,frozen_inputs,event_run_id,scenario_run_id,output):
        """采集或读取冻结证据，登记公告发生预测及配对收益情景。"""
        from src.stock_app.issuer_outlook import run_issuer_outlook
        try:
            inputs=json.loads(Path(frozen_inputs).read_text(encoding='utf-8-sig')) if frozen_inputs else None
            result=run_issuer_outlook(db_path,run_id,symbol,market_start.date(),event_start.date(),origin.date(),
                frozen_inputs=inputs,event_run_id=event_run_id,scenario_run_id=scenario_run_id)
        except (ScenarioEngineError,ValueError,KeyError,TypeError) as exc:
            result={'status':'blocked','reason':str(exc),'publication_eligible':False}
        _save(result,output,{'research_paths_and_event_forecast_recorded'})

    @cli.command('resolve-publication-returns')
    @click.option('--db-path',required=True,type=click.Path(exists=True,dir_okay=False))
    @click.option('--run-id',required=True)
    @click.option('--report-ref',required=True)
    @click.option('--outcome-panel-ref',required=True)
    @click.option('--catalogue-ref',required=True)
    @click.option('--output',required=True,type=click.Path(dir_okay=False))
    def resolve_returns(db_path,run_id,report_ref,outcome_panel_ref,catalogue_ref,output):
        """将冻结的条件收益路径与后续实际公告、总收益逐期限对账。"""
        from src.stock_app.publication_return_scenarios import resolve_publication_returns
        try:
            result=resolve_publication_returns(db_path,run_id,report_ref,outcome_panel_ref,catalogue_ref)
        except (ScenarioEngineError,FloatingPointError) as exc:
            result={'status':'blocked','reason':str(exc),'publication_eligible':False}
        _save(result,output,{'resolved'})

    @cli.command('simulate-publication-returns')
    @click.option('--db-path',required=True,type=click.Path(exists=True,dir_okay=False))
    @click.option('--run-id',required=True)
    @click.option('--symbol',required=True)
    @click.option('--panel-ref',required=True)
    @click.option('--catalogue-ref',required=True)
    @click.option('--output',required=True,type=click.Path(dir_okay=False))
    def publication_returns(db_path,run_id,symbol,panel_ref,catalogue_ref,output):
        """以相同市场扰动比较两个公告情景下的税前再投资收益路径。"""
        from src.stock_app.publication_return_scenarios import simulate_publication_returns
        try:
            result=simulate_publication_returns(db_path,run_id,symbol,panel_ref,catalogue_ref)
        except (ScenarioEngineError,FloatingPointError) as exc:
            result={'status':'blocked','reason':str(exc),'publication_eligible':False}
        _save(result,output,{'conditional_research_paths'})

    @cli.command('forecast-issuer-publication')
    @click.option('--db-path', required=True, type=click.Path(exists=True, dir_okay=False))
    @click.option('--run-id', required=True)
    @click.option('--symbol', required=True)
    @click.option('--catalogue-ref', required=True)
    @click.option('--sh-benchmark-ref', required=True)
    @click.option('--sz-benchmark-ref', required=True)
    @click.option('--output', required=True, type=click.Path(dir_okay=False))
    def forecast_issuer(db_path,run_id,symbol,catalogue_ref,sh_benchmark_ref,sz_benchmark_ref,output):
        """登记当日收盘后的公告发生研究预测，待未来事件独立结算。"""
        from src.stock_app.issuer_event_forecast import issue_publication_forecast
        try:
            result = issue_publication_forecast(db_path,run_id,symbol,catalogue_ref,
                {'1.000001':sh_benchmark_ref,'0.399001':sz_benchmark_ref})
        except ScenarioEngineError as exc:
            result = {'status':'blocked','reason':str(exc),'publication_eligible':False}
        _save(result,output,{'shadow_forecast'})

    @cli.command('resolve-issuer-publication')
    @click.option('--db-path', required=True, type=click.Path(exists=True, dir_okay=False))
    @click.option('--forecast-ref', required=True)
    @click.option('--catalogue-ref', required=True)
    @click.option('--sh-benchmark-ref', required=True)
    @click.option('--sz-benchmark-ref', required=True)
    @click.option('--output', required=True, type=click.Path(dir_okay=False))
    def resolve_issuer(db_path,forecast_ref,catalogue_ref,sh_benchmark_ref,sz_benchmark_ref,output):
        """按实际交易日和新公告目录核对已登记的预测。"""
        from src.stock_app.issuer_event_forecast import resolve_publication_forecast
        try:
            result = resolve_publication_forecast(db_path,forecast_ref,catalogue_ref,
                {'1.000001':sh_benchmark_ref,'0.399001':sz_benchmark_ref})
        except ScenarioEngineError as exc:
            result = {'status':'blocked','reason':str(exc),'publication_eligible':False}
        _save(result,output,{'resolved'})

    @cli.command('acquire-issuer-publication-dates')
    @click.option('--db-path', required=True, type=click.Path(dir_okay=False))
    @click.option('--run-id', required=True)
    @click.option('--symbol', required=True)
    @click.option('--start', required=True, type=click.DateTime(formats=['%Y-%m-%d']))
    @click.option('--end', required=True, type=click.DateTime(formats=['%Y-%m-%d']))
    @click.option('--output', required=True, type=click.Path(dir_okay=False))
    def issuer_dates(db_path, run_id, symbol, start, end, output):
        """冻结发行人公告日期目录，用于公告发生预测。"""
        from src.stock_app.cninfo_disclosures import acquire_disclosure_archive
        try:
            result = acquire_disclosure_archive(db_path, run_id, symbol, start.date(), end.date(), dates_only=True)
        except ScenarioEngineError as exc:
            result = {'status': 'blocked', 'reason': str(exc), 'context': exc.context, 'publication_eligible': False}
        _save(result, output, {'issuer_publication_dates_complete'})

    @cli.command('prepare-forecast-market')
    @click.option('--db-path', required=True, type=click.Path(dir_okay=False))
    @click.option('--run-id', required=True)
    @click.option('--symbol', required=True)
    @click.option('--start', required=True, type=click.DateTime(formats=['%Y-%m-%d']))
    @click.option('--end', required=True, type=click.DateTime(formats=['%Y-%m-%d']))
    @click.option('--output', required=True, type=click.Path(dir_okay=False))
    def forecast_market(db_path, run_id, symbol, start, end, output):
        """在独立研究任务中采集日线、双基准及公司行动并逐行验证收益。"""
        from src.stock_app.forecast_market import prepare_forecast_market
        try:
            result = prepare_forecast_market(db_path, run_id, symbol, start.date(), end.date())
        except ScenarioEngineError as exc:
            result = {'status': 'blocked', 'reason': str(exc), 'context': exc.context, 'publication_eligible': False}
        _save(result, output, {'source_bound_total_returns'})

    @cli.command('ocr-disclosure-pdf')
    @click.option('--db-path',required=True,type=click.Path(exists=True,dir_okay=False))
    @click.option('--run-id',required=True)
    @click.option('--original-ref',required=True)
    @click.option('--model-manifest',required=True,type=click.Path(exists=True,dir_okay=False))
    @click.option('--page-output-dir',required=True,type=click.Path(file_okay=False))
    @click.option('--page-rotations',type=click.Path(exists=True,dir_okay=False),help='JSON mapping of zero-based original page index to clockwise view rotation.')
    @click.option('--output',required=True,type=click.Path(dir_okay=False))
    def ocr_pdf(db_path,run_id,original_ref,model_manifest,page_output_dir,page_rotations,output):
        """固定模型逐页 OCR 与双渲染核验；待复核页不等于全文认证。"""
        from src.stock_app.pdf_ocr import run_pdf_ocr
        try:
            rotations=json.loads(Path(page_rotations).read_text(encoding='utf-8')) if page_rotations else None
            result=run_pdf_ocr(db_path,run_id,original_ref=original_ref,manifest_path=model_manifest,output_dir=page_output_dir,page_rotations=rotations)
        except (ScenarioEngineError,ValueError,TypeError,KeyError,ImportError) as exc:
            result={'status':'blocked','reason':str(exc),'error_code':getattr(exc,'code','ocr_unavailable'),
                'text_certified':False,'publication_eligible':False}
        _save(result,output,{'ocr_text_certified'})

    @cli.command('certify-disclosure-ocr')
    @click.option('--db-path',required=True,type=click.Path(exists=True,dir_okay=False))
    @click.option('--ocr-run-ref',required=True)
    @click.option('--page-reviews',required=True,type=click.Path(exists=True,dir_okay=False))
    @click.option('--output',required=True,type=click.Path(dir_okay=False))
    def certify_pdf(db_path,ocr_run_ref,page_reviews,output):
        """重放每页原文、识别、裁切及人工修订证据；任一未决页仍阻断。"""
        from src.stock_app.pdf_ocr import certify_ocr_text
        try:
            refs=json.loads(Path(page_reviews).read_text(encoding='utf-8'))
            with sqlite3.connect(db_path) as c:
                item=certify_ocr_text(MarketArtifactRepository(c),ocr_run_ref=ocr_run_ref,page_review_refs=refs,at=datetime.now(timezone.utc))
                result={'status':'ocr_text_certified','ocr_text_ref':item.artifact_id,'page_count':item.payload['page_count'],
                    'original_ref':item.payload['original_ref'],'publication_eligible':False}
        except (ScenarioEngineError,ValueError,TypeError,KeyError) as exc:
            result={'status':'blocked','reason':str(exc),'error_code':getattr(exc,'code','invalid_ocr_review'),
                'context':getattr(exc,'context',{}),'publication_eligible':False}
        _save(result,output,{'ocr_text_certified'})

    @cli.command('acquire-disclosure-archive')
    @click.option('--db-path',required=True,type=click.Path(exists=True,dir_okay=False))
    @click.option('--run-id',required=True)
    @click.option('--symbol',required=True)
    @click.option('--start',required=True,type=click.DateTime(formats=['%Y-%m-%d']))
    @click.option('--end',required=True,type=click.DateTime(formats=['%Y-%m-%d']))
    @click.option('--output',required=True,type=click.Path(dir_okay=False))
    def disclosures(db_path,run_id,symbol,start,end,output):
        """采集完整公司公告目录与原始PDF；不冒充全部媒体新闻或历史快照。"""
        from src.stock_app.cninfo_disclosures import acquire_disclosure_archive
        try:
            result=acquire_disclosure_archive(db_path,run_id,symbol,start.date(),end.date())
        except (ScenarioEngineError,ValueError,TypeError,KeyError) as exc:
            result={'status':'blocked','reason':str(exc),'error_code':getattr(exc,'code','invalid_input'),'publication_eligible':False}
        _save(result,output,{'original_disclosure_archive_complete'})

    @cli.command('run-reconstructed-simulation')
    @click.option('--db-path',required=True,type=click.Path(exists=True,dir_okay=False))
    @click.option('--run-id',required=True)
    @click.option('--inputs',required=True,type=click.Path(exists=True,dir_okay=False))
    @click.option('--output',required=True,type=click.Path(dir_okay=False))
    def simulation(db_path,run_id,inputs,output):
        """冻结来源和独立协议，重建投影并统计评分；失败与恢复均保存。"""
        from src.stock_app.reconstructed_simulation import run_reconstructed_simulation
        try:
            values=json.loads(Path(inputs).read_text(encoding='utf-8'))
            result=run_reconstructed_simulation(db_path,run_id,**values)
        except (ScenarioEngineError,ValueError,TypeError,KeyError) as exc:
            result={'status':'blocked','reason':str(exc),'error_code':getattr(exc,'code','invalid_input'),'publication_eligible':False}
        _save(result,output,{'conditional_diagnostics_passed'})

    @cli.command('score-historical-reconstruction')
    @click.option('--db-path',required=True,type=click.Path(exists=True,dir_okay=False))
    @click.option('--reconstruction-ref',required=True)
    @click.option('--score-protocol-ref',required=True)
    @click.option('--weights',required=True,type=click.Path(exists=True,dir_okay=False))
    @click.option('--output',required=True,type=click.Path(dir_okay=False))
    def scoring(db_path,reconstruction_ref,score_protocol_ref,weights,output):
        """独立复算重建投影，评分全部请求折及最终留出，不签发原始预测证书。"""
        from src.stock_app.reconstruction_scoring import score_historical_reconstruction
        try:
            values=json.loads(Path(weights).read_text(encoding='utf-8'))
            with sqlite3.connect(db_path) as c:
                result=score_historical_reconstruction(MarketArtifactRepository(c),reconstruction_ref=reconstruction_ref,
                    score_protocol_ref=score_protocol_ref,weights=values,at=datetime.now(timezone.utc))
        except (ScenarioEngineError,ValueError,TypeError,KeyError) as exc:
            result={'status':'blocked','reason':str(exc),'error_code':getattr(exc,'code','invalid_input'),'publication_eligible':False}
        _save(result,output,{'retrospective_conditional_diagnostics_passed'})

    @cli.command('acquire-action-coverage')
    @click.option('--db-path', required=True, type=click.Path(exists=True, dir_okay=False))
    @click.option('--run-id', required=True)
    @click.option('--symbol', required=True)
    @click.option('--start', required=True, type=click.DateTime(formats=['%Y-%m-%d']))
    @click.option('--end', required=True, type=click.DateTime(formats=['%Y-%m-%d']))
    @click.option('--output', required=True, type=click.Path(dir_okay=False))
    def actions(db_path, run_id, symbol, start, end, output):
        """采集并冻结三个完整公司行动目录；恢复时仅重试失败项。"""
        from src.stock_app.cninfo_actions import acquire_action_coverage
        try:
            result = acquire_action_coverage(db_path, run_id, symbol, start.date(), end.date())
        except ScenarioEngineError as exc:
            result = {'status': 'blocked', 'error_code': exc.code, 'reason': str(exc), 'publication_eligible': False}
        _save(result, output, {'source_bound_action_coverage'})

    @cli.command('build-source-bound-total-returns')
    @click.option('--db-path', required=True, type=click.Path(exists=True, dir_okay=False))
    @click.option('--inputs', required=True, type=click.Path(exists=True, dir_okay=False))
    @click.option('--output', required=True, type=click.Path(dir_okay=False))
    def total_returns(db_path, inputs, output):
        """按显式区间重算真实原始价、送转股与税前股利，不缩短缺失区间。"""
        from src.stock_app.quantitative_inputs import build_total_return_panel
        try:
            raw = json.loads(Path(inputs).read_text(encoding='utf-8'))
            if set(raw) != {'coverage_refs', 'price_refs', 'benchmark_refs', 'start', 'end'}:
                raise ValueError('total-return input keys differ from the explicit contract')
            raw['start'], raw['end'] = date.fromisoformat(raw['start']), date.fromisoformat(raw['end'])
            with sqlite3.connect(db_path) as c:
                result = build_total_return_panel(MarketArtifactRepository(c), **raw, captured_at=datetime.now(timezone.utc))
        except (ScenarioEngineError, ValueError, TypeError, KeyError) as exc:
            result = {'status': 'blocked', 'error_code': getattr(exc, 'code', 'invalid_input'),
                      'reason': str(exc), 'publication_eligible': False}
        _save(result, output, {'source_bound_total_returns'})

    @cli.command('reconstruct-historical-projections')
    @click.option('--db-path', required=True, type=click.Path(exists=True, dir_okay=False))
    @click.option('--panel-ref', required=True)
    @click.option('--protocol-ref', required=True, help='独立历史重建协议工件；实际生成时间不得倒填。')
    @click.option('--action-sources', type=click.Path(exists=True, dir_okay=False), default=None,
                  help='历史原点→证券→三个原始目录的 JSON；缺失将保留逐折阻断。')
    @click.option('--output', required=True, type=click.Path(dir_okay=False))
    def reconstruct(db_path, panel_ref, protocol_ref, action_sources, output):
        """只用各原点可见资料重建条件投影；不会生成正式校准或原始预测档案。"""
        from src.stock_app.historical_projection import reconstruct_historical_projections
        try:
            sources = json.loads(Path(action_sources).read_text(encoding='utf-8')) if action_sources else {}
            if not isinstance(sources, dict):
                raise ValueError('action sources must map origin sessions to security catalogues')
            with sqlite3.connect(db_path) as c:
                result = reconstruct_historical_projections(MarketArtifactRepository(c), panel_ref=panel_ref,
                    protocol_ref=protocol_ref, action_sources_by_origin=sources, at=datetime.now(timezone.utc))
        except (ScenarioEngineError, ValueError, TypeError) as exc:
            result = {'status': 'blocked', 'error_code': getattr(exc, 'code', 'invalid_input'),
                      'reason': str(exc), 'publication_eligible': False}
        _save(result, output, {'reconstructed_conditional_projections'})

    @cli.command('build-source-bound-wealth-events')
    @click.option('--db-path', required=True, type=click.Path(exists=True, dir_okay=False))
    @click.option('--total-return-panel-ref', required=True)
    @click.option('--event-refs', required=True, type=click.Path(exists=True, dir_okay=False))
    @click.option('--output', required=True, type=click.Path(dir_okay=False))
    def wealth_events(db_path, total_return_panel_ref, event_refs, output):
        """从总收益来源与原始新闻构建完整事件财富面板；缺日不补零。"""
        from src.stock_app.quantitative_inputs import build_wealth_event_panel
        try:
            refs = json.loads(Path(event_refs).read_text(encoding='utf-8'))
            if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
                raise ValueError('event refs must be an array of immutable source IDs')
            with sqlite3.connect(db_path) as c:
                result = build_wealth_event_panel(MarketArtifactRepository(c), total_return_panel_ref=total_return_panel_ref,
                                                 event_refs=refs, captured_at=datetime.now(timezone.utc))
        except (ScenarioEngineError, ValueError, TypeError, KeyError) as exc:
            result = {'status': 'blocked', 'error_code': getattr(exc, 'code', 'invalid_input'),
                      'reason': str(exc), 'context': getattr(exc, 'context', {}), 'publication_eligible': False}
        _save(result, output, {'source_bound_wealth_panel'})
