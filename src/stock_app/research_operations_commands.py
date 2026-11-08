"""CLI for free historical sources and durable research workers."""
from datetime import datetime, date
import json
from pathlib import Path
import click


def register_research_operations(cli):
    def save(output,result):
        with Path(output).open('x',encoding='utf8') as stream:
            json.dump(result,stream,ensure_ascii=False,indent=2)
        click.echo(json.dumps({k:result.get(k) for k in ('run_id','status','audit_ref','raw_ref')},ensure_ascii=False))

    @cli.command('discover-profit-controls')
    @click.option('--db-path',required=True,type=click.Path(dir_okay=False))
    @click.option('--run-id',required=True)
    @click.option('--symbol',required=True)
    @click.option('--direct',is_flag=True)
    @click.option('--output',required=True,type=click.Path(dir_okay=False))
    def controls(db_path,run_id,symbol,direct,output):
        """免费业绩 API 定位候选对照；原件核验前不生成训练标签。"""
        from src.stock_app.profit_control_discovery import discover_profit_controls
        if Path(output).exists():raise click.ClickException('输出已存在')
        save(output,discover_profit_controls(db_path,run_id,symbol,direct=direct))

    @cli.command('build-financial-document-coverage')
    @click.option('--db-path',required=True,type=click.Path(exists=True,dir_okay=False))
    @click.option('--run-id',required=True)
    @click.option('--acquisition-run-id',required=True)
    @click.option('--output',required=True,type=click.Path(dir_okay=False))
    def financial_coverage(db_path,run_id,acquisition_run_id,output):
        """核验完整目录内全部财报、预告与更正，显式排除其他附件。"""
        from src.stock_app.financial_disclosure_coverage import build_financial_coverage
        if Path(output).exists():raise click.ClickException('输出已存在')
        save(output,build_financial_coverage(db_path,run_id,acquisition_run_id))

    @cli.command('acquire-financial-disclosures')
    @click.option('--db-path',required=True,type=click.Path(dir_okay=False))
    @click.option('--run-id',required=True)
    @click.option('--symbol',required=True)
    @click.option('--start',required=True)
    @click.option('--end',required=True)
    @click.option('--direct',is_flag=True)
    @click.option('--output',required=True,type=click.Path(dir_okay=False))
    def acquire_financial(db_path,run_id,symbol,start,end,direct,output):
        """完整分页目录中采集所有财报、业绩预告和更正原件，明确限定覆盖范围。"""
        import requests
        from src.stock_app.cninfo_disclosures import acquire_disclosure_archive,CninfoDisclosureReader
        if Path(output).exists():raise click.ClickException('输出已存在')
        with requests.Session() as session:
            session.trust_env=not direct
            result=acquire_disclosure_archive(db_path,run_id,symbol,date.fromisoformat(start),date.fromisoformat(end),
                financial_only=True,reader=CninfoDisclosureReader(request=session.request))
        save(output,result)
        if result['status']!='financial_document_scope_complete':raise click.exceptions.Exit(2)

    @cli.command('capture-free-historical-document')
    @click.option('--db-path',required=True,type=click.Path(exists=True,dir_okay=False))
    @click.option('--run-id',required=True)
    @click.option('--original-ref',required=True)
    @click.option('--cutoff',required=True)
    @click.option('--provider',required=True,type=click.Choice(['commoncrawl','commoncrawl_s3','arquivo']))
    @click.option('--collection',default=None)
    @click.option('--archive-scheme',type=click.Choice(['http','https']),default=None)
    @click.option('--direct',is_flag=True)
    @click.option('--output',required=True,type=click.Path(dir_okay=False))
    def capture(db_path,run_id,original_ref,cutoff,provider,collection,archive_scheme,direct,output):
        """免费独立归档原字节验证；保存原始 HTTP/WARC，不回填 known_at。"""
        from src.stock_app.free_archive_capture import acquire_free_capture
        if Path(output).exists():raise click.ClickException('输出已存在')
        result=acquire_free_capture(db_path,run_id,original_ref,datetime.fromisoformat(cutoff),
            provider=provider,collection=collection,direct=direct,archive_scheme=archive_scheme)
        save(output,result)
        if result['status']!='independent_historical_bytes_verified':raise click.exceptions.Exit(2)

    @cli.command('discover-historical-news')
    @click.option('--db-path',required=True,type=click.Path(dir_okay=False))
    @click.option('--run-id',required=True)
    @click.option('--provider',required=True,type=click.Choice(['gdelt','arquivo']))
    @click.option('--query',required=True)
    @click.option('--start',required=True)
    @click.option('--end',required=True)
    @click.option('--slice-timestamp',default=None)
    @click.option('--offset',default=0,type=click.IntRange(min=0))
    @click.option('--direct',is_flag=True)
    @click.option('--output',required=True,type=click.Path(dir_okay=False))
    def news(db_path,run_id,provider,query,start,end,slice_timestamp,offset,direct,output):
        """搜索免费历史新闻线索。索引和摘要不等于原文或历史可见性证明。"""
        from src.stock_app.free_historical_news import discover_news
        if Path(output).exists():raise click.ClickException('输出已存在')
        save(output,discover_news(db_path,run_id,provider=provider,query=query,
            start=datetime.fromisoformat(start),end=datetime.fromisoformat(end),slice_timestamp=slice_timestamp,
            offset=offset,direct=direct))

    @cli.command('register-forward-watch')
    @click.option('--db-path',required=True,type=click.Path(exists=True,dir_okay=False))
    @click.option('--symbol',required=True)
    @click.option('--market-start',required=True)
    @click.option('--event-start',required=True)
    def forward(db_path,symbol,market_start,event_start):
        """预登记实际交易日预测/到期审计；禁止补发过去原点。"""
        from src.stock_app.research_worker import register_forward_watch
        click.echo(json.dumps(register_forward_watch(db_path,symbol,date.fromisoformat(market_start),
            date.fromisoformat(event_start)),ensure_ascii=False))
        from src.stock_app.research_worker import launch_worker
        launch_worker(db_path)

    @cli.command('research-monitor-worker')
    @click.option('--db-path',required=True,type=click.Path(exists=True,dir_okay=False))
    @click.option('--once',is_flag=True)
    @click.option('--interval-seconds',default=300,type=click.IntRange(min=60,max=86400))
    @click.option('--execute-feedback',is_flag=True,help='执行跟进模型复核；每次使用继承上限的独立预算，会产生额外 API 用量')
    def worker(db_path,once,interval_seconds,execute_feedback):
        """持续处理研究反馈和真实日历到期审计；--once 执行单次检查。"""
        from src.stock_app.research_worker import serve
        serve(db_path,once=once,interval_seconds=interval_seconds,execute_feedback=execute_feedback)
