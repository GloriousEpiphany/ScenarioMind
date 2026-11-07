"""Explicit model-enabled CLI for original disclosure semantic research."""
import json
import os
from pathlib import Path

import click


def register_issuer_document_commands(cli):
    @cli.command('run-issuer-document-research')
    @click.option('--db-path', required=True, type=click.Path(exists=True, dir_okay=False))
    @click.option('--run-id', required=True)
    @click.option('--symbol', required=True)
    @click.option('--coverage-ref', 'coverage_refs', multiple=True, required=True)
    @click.option('--objective', required=True)
    @click.option('--model-policy-version', required=True)
    @click.option('--model-evaluation-ref', required=True)
    @click.option('--max-model-calls', default=64, type=click.IntRange(min=2), show_default=True)
    @click.option('--max-tool-calls', default=48, type=click.IntRange(min=1), show_default=True)
    @click.option('--critic-reserved-calls', default=12, type=click.IntRange(min=1), show_default=True)
    @click.option('--output', required=True, type=click.Path(dir_okay=False))
    def documents(db_path, run_id, symbol, coverage_refs, objective, model_policy_version,
                  model_evaluation_ref, max_model_calls, max_tool_calls, critic_reserved_calls, output):
        """冻结已核验公告，调用固定模型推演并独立反证；会产生 API 用量。"""
        from openai import OpenAI
        from scenario_engine.agents.contracts import AgentModelPolicy
        from scenario_engine.agents.model import ToolCallingModel
        from scenario_engine.agents.tools import fingerprint
        from scenario_engine.errors import ScenarioEngineError
        from src.stock_app.issuer_document_research import run_issuer_document_research
        from src.stock_app.scenario_runtime import load_scenario_environment
        if Path(output).exists():
            raise click.ClickException('输出已存在；请使用新文件名，原工件不会覆盖')
        load_scenario_environment()
        model_name, endpoint, key = (str(os.getenv(n) or '').strip() for n in
            ('SCENARIO_LLM_MODEL', 'SCENARIO_LLM_API_BASE', 'SCENARIO_LLM_API_KEY'))
        if not all((model_name, endpoint, key)):
            raise click.ClickException('缺少固定模型配置；没有调用替代模型')
        try:
            policy = AgentModelPolicy(model_policy_version, model_name, model_evaluation_ref,
                max_model_calls=max_model_calls, max_tool_calls=max_tool_calls, critic_reserved_calls=critic_reserved_calls)
            with OpenAI(base_url=endpoint, api_key=key, timeout=90, max_retries=0) as client:
                model = ToolCallingModel.for_endpoint(client, policy, endpoint)
                result = run_issuer_document_research(db_path, run_id, symbol, coverage_refs,
                    model=model, policy=policy, objective=objective, endpoint_hash=fingerprint(endpoint))
        except ScenarioEngineError as exc:
            result = {'run_id': run_id, 'status': 'blocked', 'error_code': exc.code,
                      'reason': str(exc), 'context': exc.context, 'publication_eligible': False}
        with Path(output).open('x', encoding='utf8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        click.echo(json.dumps({k: result.get(k) for k in ('run_id', 'status', 'as_of', 'report_ref',
            'error_code', 'reason', 'context', 'unread_cited_documents', 'publication_eligible')}, ensure_ascii=False))
        if result['status'] != 'research_complete':
            raise click.exceptions.Exit(2)

    @cli.command('run-issuer-event-bridge')
    @click.option('--db-path', required=True, type=click.Path(exists=True, dir_okay=False))
    @click.option('--run-id', required=True)
    @click.option('--inputs', required=True, type=click.Path(exists=True, dir_okay=False))
    @click.option('--output', required=True, type=click.Path(dir_okay=False))
    def event_bridge(db_path, run_id, inputs, output):
        """绑定已审查假设与后续原件，重放历史纸面持仓标签；不签发预测。"""
        from src.stock_app.issuer_event_bridge import run_issuer_event_bridge
        if Path(output).exists():
            raise click.ClickException('输出已存在；请使用新文件名')
        result = run_issuer_event_bridge(db_path, run_id, **json.loads(Path(inputs).read_text(encoding='utf8')))
        with Path(output).open('x', encoding='utf8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        click.echo(json.dumps({k: result[k] for k in ('run_id', 'status', 'report_ref', 'publication_eligible')}, ensure_ascii=False))

    @cli.command('run-issuer-episode-research')
    @click.option('--db-path', required=True, type=click.Path(exists=True, dir_okay=False))
    @click.option('--run-id', required=True)
    @click.option('--inputs', required=True, type=click.Path(exists=True, dir_okay=False))
    @click.option('--output', required=True, type=click.Path(dir_okay=False))
    def episode_research(db_path, run_id, inputs, output):
        """保留停牌缺口，估计公告类别条件收益；历史可见性及独立校准分别验收。"""
        from src.stock_app.issuer_episode_research import run_issuer_episode_research
        from scenario_engine.errors import ScenarioEngineError
        if Path(output).exists():
            raise click.ClickException('输出已存在；请使用新文件名')
        try:
            result = run_issuer_episode_research(db_path, run_id, **json.loads(Path(inputs).read_text(encoding='utf8')))
        except ScenarioEngineError as exc:
            result = {'run_id':run_id,'status':'blocked','error_code':exc.code,'reason':str(exc),
                      'context':exc.context,'publication_eligible':False}
        with Path(output).open('x', encoding='utf8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        click.echo(json.dumps({k:result.get(k) for k in ('run_id','status','report_ref','reason','context','publication_eligible')},ensure_ascii=False))
        if result['status'] == 'blocked': raise click.exceptions.Exit(2)

    @cli.command('audit-issuer-episode-research')
    @click.option('--db-path', required=True, type=click.Path(exists=True, dir_okay=False))
    @click.option('--report-ref', required=True)
    @click.option('--output', required=True, type=click.Path(dir_okay=False))
    def episode_audit(db_path, report_ref, output):
        """重放冻结来源，独立核验事件脉冲路径与稀疏事件系数不确定性。"""
        from src.stock_app.issuer_episode_audit import audit_issuer_episode_research
        if Path(output).exists(): raise click.ClickException('输出已存在；请使用新文件名')
        result=audit_issuer_episode_research(db_path,report_ref)
        with Path(output).open('x',encoding='utf8') as f: json.dump(result,f,ensure_ascii=False,indent=2)
        click.echo(json.dumps({k:result[k] for k in ('status','audit_ref','publication_eligible')},ensure_ascii=False))

    @cli.command('build-issuer-profit-labels')
    @click.option('--db-path',required=True,type=click.Path(exists=True,dir_okay=False))
    @click.option('--run-id',required=True)
    @click.option('--coverage-ref','coverage_refs',multiple=True,required=True)
    @click.option('--output',required=True,type=click.Path(dir_okay=False))
    def profit_labels(db_path,run_id,coverage_refs,output):
        """同报告期原件绑定利润预告区间和实绩，按语义分支去重结算。"""
        from src.stock_app.issuer_profit_labels import run_profit_labels
        if Path(output).exists():raise click.ClickException('输出已存在；请使用新文件名')
        result=run_profit_labels(db_path,run_id,coverage_refs)
        with Path(output).open('x',encoding='utf8') as out:json.dump(result,out,ensure_ascii=False,indent=2)
        click.echo(json.dumps({k:result[k] for k in ('status','report_ref','resolved_clusters','publication_eligible')},ensure_ascii=False))
