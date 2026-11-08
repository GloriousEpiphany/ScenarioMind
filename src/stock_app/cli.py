"""
命令行界面模块
"""
import json
import os

import click
import pandas as pd
from rich.console import Console
from rich.table import Table

import config
from src.stock_app.database import Database
from src.stock_app.data_downloader import DataDownloader
from src.stock_app.tencent_v3_ingestion import TencentV3PriceIngestor
from src.stock_app.trading_engine import TradingEngine
from src.stock_app.scenario_local_features import (
    confirm_sellable_quantity,
    prepare_local_scenario_features,
)
from src.stock_app.scenario_calibration import calibrate_event_analog_model
from src.stock_app.scenario_historical_events import (
    import_historical_events,
    read_historical_event_csv,
)
from src.stock_app.scenario_public_data import prepare_public_scenario_data
from src.stock_app.scenario_runtime import (
    configured_runtime_capabilities,
    probe_llm_live,
    probe_tushare_live,
)


console = Console()


@click.group()
def cli():
    """A股模拟炒股系统"""
    pass


from src.stock_app.quantitative_commands import register_quantitative_commands
register_quantitative_commands(cli)
from src.stock_app.issuer_document_commands import register_issuer_document_commands
register_issuer_document_commands(cli)
from src.stock_app.research_operations_commands import register_research_operations
register_research_operations(cli)


@cli.command('validate-quantitative-release')
@click.option('--db-path', required=True, type=click.Path(exists=True, dir_okay=False))
@click.option('--run-id', required=True)
@click.option('--input-refs', type=click.Path(exists=True, dir_okay=False), default=None,
              help='已采集的joint_panel/protocol/problem/execution_state工件ID JSON；缺失即记录阻断')
@click.option('--output', type=click.Path(dir_okay=False), required=True)
@click.option('--scope', type=click.Choice(['prediction_simulation', 'release']), default='prediction_simulation', show_default=True)
def validate_quantitative_release_command(db_path, run_id, input_refs, output, scope):
    """默认验证预测模拟；真实交易资格仅属于单独的 release 范围。不下单。"""
    from pathlib import Path
    from src.stock_app.quantitative_validation import validate_quantitative_release
    from scenario_engine.errors import ScenarioEngineError
    try:
        refs = json.loads(Path(input_refs).read_text(encoding='utf-8')) if input_refs else {}
        result = validate_quantitative_release(db_path, run_id, input_refs=refs, validation_scope=scope)
        Path(output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        console.print_json(json.dumps(result, ensure_ascii=False))
        if not (result['publication_eligible'] or scope == 'prediction_simulation'
                and result['status'] == 'validated_conditional_simulation_scope'):
            raise click.exceptions.Exit(2)
    except ScenarioEngineError as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command('scenario-doctor')
@click.option('--live-llm', is_flag=True, help='实际发起一次结构化大模型请求；可能产生少量 API 用量')
@click.option('--live-tushare', is_flag=True, help='实际调用 TuShare daily 并探测 A 股日行情权限')
@click.option('--live-advisory', is_flag=True, help='实际验证两轮自治模型工具调用与续传；会产生 API 用量')
def scenario_doctor(live_llm, live_tushare, live_advisory):
    """审计情景推演依赖、凭证和真实连通性，不输出任何密钥。"""
    capabilities = list(configured_runtime_capabilities())
    if live_advisory:
        from src.stock_app.scenario_runtime import probe_advisory_live
        capabilities.append(probe_advisory_live())
    if live_llm:
        live = probe_llm_live()
        capabilities = [
            live if item.capability_id == "scenario_llm" else item
            for item in capabilities
        ]
    if live_tushare:
        live = probe_tushare_live()
        capabilities = [
            live if item.capability_id == "tushare_pro" else item
            for item in capabilities
        ]
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("能力")
    table.add_column("已配置")
    table.add_column("依赖")
    table.add_column("真实探测")
    table.add_column("状态")
    table.add_column("详情")
    for item in capabilities:
        table.add_row(
            item.capability_id,
            "是" if item.configured else "否",
            "有" if item.dependency_available else "缺",
            "通过" if item.live_verified else "未通过/未执行",
            item.status,
            item.detail,
        )
    console.print(table)
    requested_failures = {
        item.capability_id
        for item in capabilities
        if (
            (live_llm and item.capability_id == "scenario_llm" and not item.usable)
            or (live_tushare and item.capability_id == "tushare_pro" and not item.usable)
            or (live_advisory and item.capability_id == "advisory_tools" and not item.usable)
        )
    }
    if requested_failures or any(item.status == "dependency_missing" for item in capabilities):
        raise click.ClickException("存在真实失败或缺失依赖；详见上表")


@cli.command('prepare-scenario-public-data')
@click.option('--symbol', 'symbols', multiple=True, required=True, help='A 股代码，可重复传入')
@click.option('--include-money-flow', is_flag=True, help='拉取慢速个股资金流全表；实测可能需要约 1-2 分钟')
@click.option(
    '--ack-public-research-terms',
    is_flag=True,
    help='确认仅按研究用途使用公共网页数据并自行核对上游条款',
)
def prepare_scenario_public_data_command(symbols, include_money_flow, ack_public_research_terms):
    """从逐项验证的 AKShare 公共端点写入严格 V3 工件。"""
    if not ack_public_research_terms:
        raise click.ClickException(
            "必须显式传入 --ack-public-research-terms；程序不会替用户假定公共网页许可"
        )
    result = prepare_public_scenario_data(
        config.DATABASE_PATH,
        symbols,
        include_money_flow=include_money_flow,
    )
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("证券")
    table.add_column("组件")
    table.add_column("状态")
    table.add_column("工件数", justify="right")
    table.add_column("详情")
    for item in result.items:
        table.add_row(item.symbol, item.component, item.status, str(item.artifact_count), item.detail)
    console.print(table)
    console.print(f"共写入 {result.artifact_count} 条工件；完整成功={result.complete}")
    if not result.complete:
        raise click.ClickException("至少一个公共数据组件未完成；已成功组件保留，失败项未填默认值")


@cli.command('derive-scenario-features')
@click.option('--symbol', 'symbols', multiple=True, required=True, help='当前持仓代码，可重复传入')
@click.option('--portfolio-ref', default='current', show_default=True)
def derive_scenario_features_command(symbols, portfolio_ref):
    """从本地真实 V3 工件派生市场状态、因子、残差和交易约束。"""
    result = prepare_local_scenario_features(
        config.DATABASE_PATH,
        symbols,
        portfolio_ref=portfolio_ref,
    )
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("证券")
    table.add_column("组件")
    table.add_column("状态")
    table.add_column("工件数", justify="right")
    table.add_column("详情")
    for item in result.items:
        table.add_row(item.symbol or "全市场", item.component, item.status, str(item.artifact_count), item.detail)
    console.print(table)
    if not result.complete:
        raise click.ClickException("至少一个本地派生组件未完成；失败项没有使用默认值")


@cli.command('confirm-sellable')
@click.option('--account-id', required=True, type=click.IntRange(min=1))
@click.option('--symbol', required=True)
@click.option('--as-of-date', required=True, type=click.DateTime(formats=['%Y-%m-%d']))
@click.option('--position-quantity', required=True, type=click.IntRange(min=1))
@click.option('--sellable-quantity', required=True, type=click.IntRange(min=0))
def confirm_sellable_command(account_id, symbol, as_of_date, position_quantity, sellable_quantity):
    """记录券商当日可卖数量；用于 T+1 交易约束，不能由系统猜测。"""
    confirm_sellable_quantity(
        config.DATABASE_PATH,
        account_id=account_id,
        symbol=symbol,
        as_of_date=as_of_date.date(),
        sellable_quantity=sellable_quantity,
        position_quantity=position_quantity,
    )
    console.print(
        f"[green]已确认账户 {account_id} / {symbol} / {as_of_date.date().isoformat()} "
        f"可卖 {sellable_quantity}/{position_quantity} 股[/green]"
    )


@cli.command('import-scenario-events')
@click.option('--file', 'file_path', required=True, type=click.Path(exists=True, dir_okay=False, readable=True))
@click.option('--symbol', 'symbols', multiple=True, required=True, help='需要计算后验收益的组合证券，可重复传入')
def import_scenario_events_command(file_path, symbols):
    """导入自有可核验历史事件，并从 V3 行情计算成熟后验收益。"""
    with open(file_path, 'rb') as handle:
        frame = read_historical_event_csv(handle.read())
    result = import_historical_events(
        config.DATABASE_PATH,
        frame,
        portfolio_symbols=symbols,
    )
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("事件 ID")
    table.add_column("状态")
    table.add_column("工件数", justify="right")
    table.add_column("详情")
    for item in result.items:
        table.add_row(item.event_id, item.status, str(item.artifact_count), item.detail)
    console.print(table)
    if not result.complete:
        raise click.ClickException("部分事件缺少证据字段或尚未成熟 60 个后续交易日；未生成默认收益")


@cli.command('calibrate-scenario-model')
@click.option('--event-type', required=True, help='与历史样本及当前事实事件一致的事件类型')
@click.option('--symbol', 'symbols', multiple=True, required=True)
@click.option('--min-samples', type=click.IntRange(min=5), default=None, help='训练事件最小样本数')
def calibrate_scenario_model_command(event_type, symbols, min_samples):
    """逐折回放生产算法；当时不可见或标签未成熟的样本不能用于校准。"""
    result = calibrate_event_analog_model(
        config.DATABASE_PATH,
        event_type=event_type,
        portfolio_symbols=symbols,
        min_samples=min_samples,
    )
    console.print({
        "model_id": result.model_id,
        "version": result.version,
        "event_type": result.event_type,
        "status": result.status,
        "training_event_count": result.training_event_count,
        "validation_event_count": result.validation_event_count,
        "metrics": result.metrics,
        "detail": result.detail,
    })
    if not result.active:
        raise click.ClickException("模型未通过样本外校准闸门，未登记 active")


@cli.command('build-investment-universe')
@click.option('--universe-ref', required=True, help='候选池引用名')
@click.option('--portfolio-ref', default='current', show_default=True)
@click.option('--master-artifact-id', required=True, help='完整 PIT 证券主数据工件 ID')
@click.option('--thesis-artifact-id', 'thesis_ids', multiple=True, required=True, help='已验证公司论点工件 ID，可重复')
@click.option('--market', 'markets', multiple=True, required=True, help='明确允许的市场代码，可重复')
@click.option('--policy-version', required=True)
@click.option('--expires-at', required=True, help='候选池到期时间，带时区 ISO-8601')
def build_investment_universe_command(universe_ref, portfolio_ref, master_artifact_id, thesis_ids,
                                      markets, policy_version, expires_at):
    """从已入库证券主数据与公司论点构建候选池，保留全部现有持仓。"""
    from datetime import datetime, timezone
    from scenario_engine.data import MarketArtifactRepository
    from scenario_engine.mission import SQLitePointInTimePortfolioResolver
    from scenario_engine.universe.builder import build_universe_artifact

    db = Database(config.DATABASE_PATH)
    try:
        repository = MarketArtifactRepository(db.connect())
        as_of = datetime.now(timezone.utc)
        portfolio = SQLitePointInTimePortfolioResolver(repository).resolve(portfolio_ref, as_of)
        artifact = build_universe_artifact(
            repository=repository, universe_ref=universe_ref, portfolio=portfolio,
            master_artifact_id=master_artifact_id, thesis_artifact_ids=thesis_ids,
            allowed_markets=markets, policy_version=policy_version,
            as_of=as_of, expires_at=datetime.fromisoformat(expires_at),
        )
        repository.ingest_many((artifact,))
        console.print({"universe_ref": universe_ref, "artifact_id": artifact.artifact_id,
                       "members": artifact.payload["members"], "exclusions": artifact.payload["exclusions"],
                       "status": "research_universe_created_not_an_investment_recommendation"})
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        db.close()


@cli.command('research-portfolio')
@click.option('--objective', required=True, help='具体持仓或候选股研究目标')
@click.option('--portfolio-ref', default='current', show_default=True)
@click.option('--universe-ref', default=None, help='显式使用已构建的 PIT 候选池')
@click.option('--max-drawdown', type=click.FloatRange(min=0, max=1, min_open=True), required=True)
@click.option('--max-single-weight', type=click.FloatRange(min=0, max=1, min_open=True), required=True)
def research_portfolio_command(objective, portfolio_ref, universe_ref, max_drawdown, max_single_weight):
    """运行严格研究并保存审计报告；研究成功不等于通过调仓发布闸门。"""
    from datetime import datetime, timezone
    from scenario_engine import ScenarioEngine
    from scenario_engine.errors import ScenarioEngineError
    from scenario_engine.mission import ScenarioMission, RiskPolicy

    engine = ScenarioEngine(db_path=config.DATABASE_PATH, use_llm=False)
    try:
        mission = ScenarioMission(objective, portfolio_ref, datetime.now(timezone.utc),
                                  RiskPolicy(max_drawdown, max_single_weight), universe_ref=universe_ref)
        result = engine.execute(mission)
        console.print_json(data=result.as_dict())
    except ScenarioEngineError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        engine.close()


@cli.command('advisory-research')
@click.option('--run-id', required=True, help='持久化任务 ID；相同参数可恢复中断任务')
@click.option('--objective', required=True)
@click.option('--as-of', required=True, help='冻结知识截止时间，带时区 ISO-8601')
@click.option('--symbol', 'symbols', multiple=True, required=True)
@click.option('--dataset', 'datasets', multiple=True, required=True, help='允许读取的 domain/dataset，可重复')
@click.option('--model-policy-version', required=True)
@click.option('--model-evaluation-ref', required=True, help='固定模型策略对应的评测记录引用')
def advisory_research_command(run_id, objective, as_of, symbols, datasets, model_policy_version, model_evaluation_ref):
    """调用固定模型自主读取 PIT 证据并反证；会产生模型 API 用量，不发布交易建议。"""
    from datetime import datetime
    import sqlite3
    from scenario_engine.agents import AgentModelPolicy
    from scenario_engine.agents.model import ToolCallingModel
    from scenario_engine.agents.pit_tools import make_pit_query_tool
    from scenario_engine.agents.runtime import AgentRuntime
    from scenario_engine.agents.tools import fingerprint
    from scenario_engine.data import MarketArtifactRepository
    from scenario_engine.errors import ScenarioEngineError
    from src.stock_app.scenario_runtime import load_scenario_environment

    runtime, connection = None, None
    try:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise click.ClickException('缺少模型 SDK；请安装 requirements-advisory.txt，不会回退到固定流程') from exc
        load_scenario_environment()
        model_name = os.getenv('SCENARIO_LLM_MODEL')
        api_base = os.getenv('SCENARIO_LLM_API_BASE')
        api_key = os.getenv('SCENARIO_LLM_API_KEY')
        if not all((model_name, api_base, api_key)):
            raise click.ClickException('自治研究要求明确配置 SCENARIO_LLM_MODEL、SCENARIO_LLM_API_BASE 和 SCENARIO_LLM_API_KEY')
        scope = tuple(tuple(item.split('/')) for item in datasets)
        if any(len(item) != 2 or not all(item) for item in scope):
            raise click.ClickException('--dataset 必须是非空 domain/dataset')
        cutoff = datetime.fromisoformat(as_of)
        policy = AgentModelPolicy(model_policy_version, model_name, model_evaluation_ref)
        connection = sqlite3.connect(config.DATABASE_PATH, timeout=30, check_same_thread=False)
        repository = MarketArtifactRepository(connection)
        query_tool = make_pit_query_tool(repository, allowed_datasets=scope, allowed_symbols=symbols)
        with OpenAI(base_url=api_base, api_key=api_key, timeout=60, max_retries=0) as client:
            runtime = AgentRuntime(db_path=config.DATABASE_PATH, model=ToolCallingModel.for_endpoint(client, policy, api_base), policy=policy, tools=(query_tool,))
            result = runtime.execute(run_id=run_id, objective=objective, as_of=cutoff,
                                     context={"symbols": list(symbols), "datasets": list(datasets), "model_endpoint_hash": fingerprint(api_base)})
            console.print_json(data=result)
            if result['status'] != 'research_complete':
                raise click.ClickException(f"研究未完成：{result['status']}；已保存进度，不发布建议")
    except (ScenarioEngineError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        if runtime is not None:
            runtime.close()
        if connection is not None:
            connection.close()


@cli.command('portfolio-analysis')
@click.option('--db-path', type=click.Path(dir_okay=False), default=None)
@click.option('--account-id', type=int, default=None, help='只有多个有持仓账户时才必须指定')
@click.option('--run-id', default=None, help='指定已有任务 ID 以恢复；参数取自冻结请求')
@click.option('--objective', default=None)
@click.option('--require-cctv', is_flag=True, help='新任务必须完整分析新闻联播；普通研究只作附加来源')
@click.option('--price-source', type=click.Choice(['eastmoney_http', 'baostock', 'tushare', 'tencent_http']), default=None)
@click.option('--output', type=click.Path(dir_okay=False), default=None)
@click.option('--max-model-calls', type=click.IntRange(min=24), default=None, help='新任务的全队模型调用上限，默认144，含失败尝试')
@click.option('--max-tool-calls', type=click.IntRange(min=1), default=None, help='新任务的全队工具调用上限，默认112')
def portfolio_analysis_command(db_path, account_id, run_id, objective, require_cctv, price_source, output, max_model_calls, max_tool_calls):
    """真实持仓自动采集、专业 Agent 研究、逐仓影响及条件建议/对冲；会使用模型 API。"""
    from pathlib import Path
    import sqlite3
    from src.stock_app.portfolio_research import DEFAULT_OBJECTIVE
    from src.stock_app.portfolio_research_jobs import build_request, enqueue, execute_job, load_job
    from src.stock_app.v3_portfolio import CurrentPortfolioSnapshotModule, automatically_selected_account_id
    from scenario_engine.errors import ScenarioEngineError
    path = db_path or config.DATABASE_PATH
    try:
        if run_id is None:
            with sqlite3.connect(path) as connection:
                accounts = CurrentPortfolioSnapshotModule(connection).discover_accounts()
            account_id = account_id if account_id is not None else automatically_selected_account_id(accounts)
            if account_id is None:
                raise click.ClickException('请通过 --account-id 明确选择有持仓账户')
            request = build_request(path, account_id, objective=objective or DEFAULT_OBJECTIVE, price_source=price_source, cctv_required=require_cctv,
                max_model_calls=max_model_calls if max_model_calls is not None else 144,
                max_tool_calls=max_tool_calls if max_tool_calls is not None else 112)
            run_id = enqueue(path, request)
        elif any(value is not None for value in (account_id, objective, price_source, max_model_calls, max_tool_calls)):
            raise click.ClickException('恢复任务只需 --run-id；不能覆盖冻结参数')
        console.print(f'持仓研究任务：{run_id}')
        result = execute_job(path, run_id)
        if output:
            Path(output).write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        console.print_json(data={key: result.get(key) for key in ('run_id', 'status', 'as_of', 'publication_eligible', 'publication_blockers', 'failed_stage')})
        if result['status'] != 'conditional_research_complete':
            raise click.ClickException('持仓研究尚未完成；失败原因与检查点已保存')
    except ScenarioEngineError as exc:
        job = load_job(path, run_id=run_id) if run_id else None
        console.print_json(data=job['error'] if job and job['error'] else {'error_type': type(exc).__name__, 'error_code': exc.code})
        raise click.ClickException('任务未完成；没有生成降级结果') from exc


@cli.command('deductive-research')
@click.option('--price-source', type=click.Choice(['eastmoney_http', 'baostock', 'tushare', 'tencent_http']),
              default=None,
              help='冻结唯一行情源；未指定时加载 STOCKM1_PRICE_SOURCE，缺省 eastmoney_http；失败不换源')
@click.option('--db-path', type=click.Path(dir_okay=False), default=None, help='数据库路径；默认使用应用数据库')
@click.option('--run-id', required=True, help='任务 ID；使用相同参数恢复，成功采集项不会重复下载')
@click.option('--objective', required=True)
@click.option('--symbol', 'symbols', multiple=True, required=True, help='研究证券代码，可重复')
@click.option('--start-date', required=True, type=click.DateTime(formats=['%Y-%m-%d']))
@click.option('--end-date', required=True, type=click.DateTime(formats=['%Y-%m-%d']))
@click.option('--model-policy-version', required=True)
@click.option('--model-evaluation-ref', required=True)
@click.option('--max-model-calls', default=64, type=click.IntRange(min=2), show_default=True,
              help='包含独立审查及中断尝试的模型调用总上限')
@click.option('--max-tool-calls', default=48, type=click.IntRange(min=1), show_default=True)
@click.option('--critic-reserved-calls', default=12, type=click.IntRange(min=1), show_default=True)
def deductive_research_command(price_source, db_path, run_id, objective, symbols, start_date, end_date,
                               model_policy_version, model_evaluation_ref, max_model_calls,
                               max_tool_calls, critic_reserved_calls):
    """自动采集六类证据、冻结时点并执行模型研究与独立反证；会产生 API 用量。"""
    from scenario_engine.agents import AgentModelPolicy
    from scenario_engine.agents.model import ToolCallingModel
    from scenario_engine.agents.tools import fingerprint
    from scenario_engine.errors import ScenarioEngineError
    from src.stock_app.deductive_research import DeductiveResearchPipeline
    from src.stock_app.scenario_data_prep import latest_likely_closed_trading_date
    from src.stock_app.scenario_runtime import load_scenario_environment

    pipeline = None
    try:
        from openai import OpenAI
        from src.stock_app.public_https import configured_public_http_profile
        load_scenario_environment()
        if price_source is None:
            from src.stock_app.scenario_data_prep import configured_equity_price_source
            price_source = configured_equity_price_source()
        if price_source not in {'eastmoney_http', 'baostock', 'tushare', 'tencent_http'}:
            raise click.ClickException('STOCKM1_PRICE_SOURCE 必须明确指定受支持的行情源')
        settings = [str(os.getenv(name) or '').strip() for name in (
            'SCENARIO_LLM_MODEL', 'SCENARIO_LLM_API_BASE', 'SCENARIO_LLM_API_KEY')]
        if not all(settings):
            raise click.ClickException('需要明确配置 SCENARIO_LLM_MODEL、SCENARIO_LLM_API_BASE、SCENARIO_LLM_API_KEY')
        model_name, api_base, api_key = settings
        if end_date.date() > latest_likely_closed_trading_date():
            raise click.ClickException('--end-date 必须是已经收盘的日期')
        source_policy = {'price': price_source, 'public': 'akshare_public',
                         'news': 'eastmoney_curl_jsonp_v2', 'flow': 'eastmoney_daily_flow_v1',
                         'financials': 'akshare-financial-abstract-v2',
                         'flow_transport': configured_public_http_profile(),
                         'price_transport': configured_public_http_profile() if price_source == 'eastmoney_http' else 'provider_sdk'}
        policy = AgentModelPolicy(model_policy_version, model_name, model_evaluation_ref,
                                  max_model_calls=max_model_calls, max_tool_calls=max_tool_calls,
                                  critic_reserved_calls=critic_reserved_calls)
        with OpenAI(base_url=api_base, api_key=api_key, timeout=90, max_retries=0) as client:
            model = ToolCallingModel.for_endpoint(client, policy, api_base)
            pipeline = DeductiveResearchPipeline(db_path or config.DATABASE_PATH, model=model, policy=policy)
            result = pipeline.execute(run_id=run_id, objective=objective, symbols=symbols,
                start_date=start_date.date(), end_date=end_date.date(), endpoint_hash=fingerprint(api_base),
                source_policy=source_policy,
                progress=lambda task, status: console.print(f'{task}: {status}'))
            console.print_json(data=result)
            if result['status'] != 'research_complete':
                raise click.ClickException(f"全流程未完成：{result['status']}；已保存采集进度")
    except ImportError as exc:
        raise click.ClickException('缺少自治/数据依赖，请安装 requirements-advisory.txt 和 requirements-scenario-public.txt') from exc
    except (ScenarioEngineError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        if pipeline is not None:
            pipeline.close()


@cli.command()
@click.option('--start-date', default=config.DEFAULT_START_DATE, help='开始日期 (YYYYMMDD)')
@click.option('--end-date', default=None, help='结束日期 (YYYYMMDD)')
@click.option('--limit', default=None, type=int, help='限制下载数量（测试用）')
@click.option('--interval', default=1.0, type=float, show_default=True, help='每只股票请求后的等待秒数')
@click.option('--retries', default=3, type=int, show_default=True, help='单只股票失败后的重试次数')
@click.option('--retry-delay', default=2.0, type=float, show_default=True, help='首次重试等待秒数，后续指数退避')
@click.option('--timeout', default=15.0, type=float, show_default=True, help='单次网络请求超时秒数')
@click.option(
    '--source',
    type=click.Choice(('tencent_http', 'eastmoney_http'), case_sensitive=False),
    default='tencent_http',
    show_default=True,
    help='显式选择唯一行情源；不会自动换源',
)
@click.option('--full-refresh', is_flag=True, help='忽略数据库已有日期，强制按开始日期全量重拉')
@click.option('--refresh-stock-list', is_flag=True, help='联网刷新股票列表；默认优先使用本地缓存，避免列表接口卡住')
def download_stocks(
    start_date,
    end_date,
    limit,
    interval,
    retries,
    retry_delay,
    timeout,
    source,
    full_refresh,
    refresh_stock_list
):
    """下载A股股票历史数据"""
    console.print("[bold cyan]开始下载A股股票数据...[/bold cyan]")
    
    db = Database()
    downloader = DataDownloader()
    
    # 默认优先使用本地股票列表，避免 AKShare 列表接口偶发长时间阻塞。
    stock_list = db.get_stock_list_for_download()
    if not stock_list.empty and not refresh_stock_list:
        console.print(f"[green]使用本地缓存的 {len(stock_list)} 只股票列表[/green]")
    else:
        stock_list = downloader.get_stock_list(retries=retries, retry_delay=retry_delay)
        if not stock_list.empty:
            db.save_stock_info(stock_list)
            console.print(f"[green]股票列表已保存到数据库[/green]")
        else:
            cached_stock_list = db.get_stock_list_for_download()
            if not cached_stock_list.empty:
                stock_list = cached_stock_list
                console.print(
                    f"[yellow]网络股票列表获取失败，使用本地缓存的 {len(stock_list)} 只股票继续下载[/yellow]"
                )
            else:
                console.print("[red]无法获取股票列表，且本地没有缓存，下载终止[/red]")
                db.close()
                return
    
    # 下载股票数据。每只股票成功后立即入库，避免全市场下载时占用大量内存。
    result = downloader.download_all_stocks(
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        stock_list=stock_list,
        interval=interval,
        retries=retries,
        retry_delay=retry_delay,
        timeout=timeout,
        source_id=source,
        incremental=not full_refresh,
        latest_date_getter=db.get_latest_stock_date,
        save_callback=db.save_stock_daily_data
    )

    if result.get('success', 0) > 0:
        console.print("[green]数据已保存到数据库[/green]")

    failed_path = os.path.join(config.DATA_DIR, 'failed_stock_downloads.txt')
    failed_symbols = result.get('failed_symbols', [])
    if failed_symbols:
        with open(failed_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(failed_symbols))
            f.write('\n')
        console.print(f"[yellow]失败股票代码已写入: {failed_path}[/yellow]")
    elif os.path.exists(failed_path):
        os.remove(failed_path)
    
    db.close()


@cli.command('ingest-v3-prices')
@click.option('--symbol', 'symbols', multiple=True, help='只入库指定股票，可重复传入')
@click.option('--start-date', default=config.DEFAULT_START_DATE, show_default=True, help='开始日期 (YYYYMMDD)')
@click.option('--end-date', required=True, help='已收盘的结束日期 (YYYYMMDD)')
@click.option('--limit', default=None, type=click.IntRange(min=1), help='限制股票数量')
@click.option('--interval', default=1.0, type=click.FloatRange(min=0), show_default=True, help='每只股票请求后的等待秒数')
@click.option('--retries', default=3, type=click.IntRange(min=0), show_default=True, help='单只股票失败后的重试次数')
@click.option('--retry-delay', default=2.0, type=click.FloatRange(min=0), show_default=True, help='首次重试等待秒数')
@click.option('--timeout', default=15.0, type=click.FloatRange(min=0.1), show_default=True, help='单次网络请求超时秒数')
@click.option('--full-refresh', is_flag=True, help='忽略已入库的 V3 腾讯工件日期')
def ingest_v3_prices(
    symbols,
    start_date,
    end_date,
    limit,
    interval,
    retries,
    retry_delay,
    timeout,
    full_refresh,
):
    """通过腾讯源发布不可变的 V3 点时日行情工件。"""
    db = Database()
    try:
        stock_list = db.get_stock_list_for_download()
        if stock_list.empty:
            raise click.ClickException("stock_info 为空；必须先导入带来源的股票列表快照")
        if symbols:
            requested = {str(symbol).strip() for symbol in symbols}
            available = set(stock_list["code"].astype(str))
            missing = sorted(requested.difference(available))
            if missing:
                raise click.ClickException(f"stock_info 不包含指定股票: {missing}")
            stock_list = stock_list[stock_list["code"].astype(str).isin(requested)].reset_index(drop=True)

        ingestor = TencentV3PriceIngestor(db.connect())
        published = []

        def publish(symbol, dataframe):
            published.append(ingestor.ingest_dataframe(symbol, dataframe))

        console.print("[bold cyan]开始从腾讯源入库严格 V3 日行情...[/bold cyan]")
        result = DataDownloader().download_all_stocks(
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            stock_list=stock_list,
            interval=interval,
            retries=retries,
            retry_delay=retry_delay,
            timeout=timeout,
            incremental=not full_refresh,
            source_id="tencent_http",
            latest_date_getter=ingestor.latest_date,
            save_callback=publish,
        )
        artifact_count = sum(item.artifact_count for item in published)
        result_color = "yellow" if result["coverage_gaps"] else "green"
        console.print(
            f"[{result_color}]V3 腾讯行情入库完成：{artifact_count} 条不可变工件，"
            f"成功 {result['success']} 只，跳过 {result['skipped']} 只，"
            f"无新交易记录 {len(result['no_new_data_symbols'])} 只，"
            f"无历史覆盖失败 {result['failed']} 只。[/{result_color}]"
        )
        audit_path = os.path.join(config.DATA_DIR, "v3_tencent_ingestion_audit.json")
        audit_payload = {
            "source_id": "tencent_http",
            "start_date": start_date,
            "end_date": end_date,
            "success": result["success"],
            "failed": result["failed"],
            "skipped": result["skipped"],
            "no_new_data_symbols": result["no_new_data_symbols"],
            "coverage_gaps": result["coverage_gaps"],
        }
        temporary_audit_path = audit_path + ".tmp"
        with open(temporary_audit_path, "w", encoding="utf-8") as handle:
            json.dump(audit_payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary_audit_path, audit_path)
        console.print(f"[cyan]入库审计已写入: {audit_path}[/cyan]")
        if result["failed"]:
            raise click.ClickException(
                f"腾讯源对 {result['failed']} 只股票在请求区间内没有任何可入库记录；"
                f"已处理其他标的，详情见 {audit_path}"
            )
    finally:
        db.close()


@cli.command()
@click.option('--start-date', default=config.DEFAULT_START_DATE, help='开始日期 (YYYYMMDD)')
@click.option('--end-date', default=None, help='结束日期 (YYYYMMDD)')
def download_indices(start_date, end_date):
    """下载A股指数历史数据"""
    console.print("[bold cyan]开始下载A股指数数据...[/bold cyan]")
    
    db = Database()
    downloader = DataDownloader()
    
    # 下载指数数据
    result = downloader.download_all_indices(start_date, end_date)
    
    # 保存到数据库
    if result:
        console.print("\n[cyan]正在保存数据到数据库...[/cyan]")
        for symbol, df in result.items():
            db.save_index_daily_data(symbol, df)
        console.print("[green]指数数据已保存到数据库[/green]")
    
    db.close()


@cli.command()
@click.argument('symbol')
@click.option('--start-date', default=None, help='开始日期 (YYYY-MM-DD)')
@click.option('--end-date', default=None, help='结束日期 (YYYY-MM-DD)')
def query_stock(symbol, start_date, end_date):
    """查询股票历史数据"""
    db = Database()
    
    df = db.get_stock_data(symbol, start_date, end_date)
    
    if df.empty:
        console.print(f"[red]未找到股票 {symbol} 的数据[/red]")
    else:
        console.print(f"\n[bold cyan]股票 {symbol} 历史数据（共 {len(df)} 条）[/bold cyan]")
        
        # 显示前10条和后10条
        display_df = df if len(df) <= 20 else pd.concat([df.head(10), df.tail(10)])
        
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("日期")
        table.add_column("开盘", justify="right")
        table.add_column("收盘", justify="right")
        table.add_column("最高", justify="right")
        table.add_column("最低", justify="right")
        table.add_column("成交量", justify="right")
        
        for _, row in display_df.iterrows():
            table.add_row(
                row['date'],
                f"{row['open']:.2f}",
                f"{row['close']:.2f}",
                f"{row['high']:.2f}",
                f"{row['low']:.2f}",
                f"{row['volume']:,.0f}"
            )
        
        console.print(table)
    
    db.close()


@cli.command()
def list_stocks():
    """列出所有已下载的股票"""
    db = Database()
    
    df = db.get_stocks_with_data_count()
    
    if df.empty:
        console.print("[yellow]数据库中暂无股票数据[/yellow]")
    else:
        console.print(f"\n[bold cyan]已下载股票列表（共 {len(df)} 只）[/bold cyan]")
        
        # 只显示有数据的股票
        df = df[df['data_count'] > 0]
        
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("股票代码", style="cyan")
        table.add_column("股票名称", style="cyan")
        table.add_column("数据条数", justify="right")
        
        for _, row in df.head(50).iterrows():
            table.add_row(
                row['symbol'],
                row['name'],
                str(row['data_count'])
            )
        
        console.print(table)
        
        if len(df) > 50:
            console.print(f"\n[yellow]... 还有 {len(df) - 50} 只股票未显示[/yellow]")
    
    db.close()


@cli.command()
@click.option(
    '--output',
    default=os.path.join(config.DATA_DIR, 'all_stock_data.csv'),
    show_default=True,
    help='导出CSV路径'
)
def export_stock_data(output):
    """导出所有股票日线数据到CSV"""
    db = Database()
    df = db.get_all_stock_daily()
    
    if df.empty:
        console.print('[yellow]数据库中暂无股票日线数据[/yellow]')
        db.close()
        return
    
    output_dir = os.path.dirname(os.path.abspath(output))
    os.makedirs(output_dir, exist_ok=True)
    df.to_csv(output, index=False)
    console.print(f"[green]已导出 {len(df)} 条记录到 {output}[/green]")
    db.close()


@cli.command()
@click.argument('account_name')
@click.option('--capital', default=config.INITIAL_CAPITAL, type=float, help='初始资金')
def create_account(account_name, capital):
    """创建模拟账户"""
    db = Database()
    engine = TradingEngine(db)
    
    engine.create_account(account_name, capital)
    
    db.close()


@cli.command()
@click.argument('account_name')
def load_account(account_name):
    """加载模拟账户（进入交易模式）"""
    db = Database()
    engine = TradingEngine(db)
    
    if not engine.load_account(account_name):
        db.close()
        return
    
    # 进入交互模式
    console.print("\n[bold green]欢迎使用模拟炒股系统！[/bold green]")
    console.print("输入 'help' 查看帮助\n")
    
    while True:
        try:
            cmd = console.input("[bold cyan]>>> [/bold cyan]").strip()
            
            if not cmd:
                continue
            
            parts = cmd.split()
            command = parts[0].lower()
            
            if command == 'help':
                show_trading_help()
            
            elif command == 'date':
                if len(parts) < 2:
                    console.print("[red]用法: date YYYY-MM-DD[/red]")
                else:
                    engine.set_date(parts[1])
            
            elif command == 'buy':
                if len(parts) < 3:
                    console.print("[red]用法: buy 股票代码 数量 [价格][/red]")
                else:
                    symbol = parts[1]
                    quantity = int(parts[2])
                    price = float(parts[3]) if len(parts) > 3 else None
                    engine.buy(symbol, quantity, price)
            
            elif command == 'sell':
                if len(parts) < 3:
                    console.print("[red]用法: sell 股票代码 数量 [价格][/red]")
                else:
                    symbol = parts[1]
                    quantity = int(parts[2])
                    price = float(parts[3]) if len(parts) > 3 else None
                    engine.sell(symbol, quantity, price)
            
            elif command == 'portfolio' or command == 'p':
                engine.show_portfolio()
            
            elif command == 'transactions' or command == 'trans':
                limit = int(parts[1]) if len(parts) > 1 else 20
                engine.show_transactions(limit)
            
            elif command == 'price':
                if len(parts) < 2:
                    console.print("[red]用法: price 股票代码[/red]")
                else:
                    show_price(db, parts[1], engine.portfolio.current_date)
            
            elif command == 'exit' or command == 'quit':
                console.print("[green]再见！[/green]")
                break
            
            else:
                console.print(f"[red]未知命令: {command}[/red]")
                console.print("输入 'help' 查看帮助")
        
        except KeyboardInterrupt:
            console.print("\n[green]再见！[/green]")
            break
        except Exception as e:
            console.print(f"[red]错误: {e}[/red]")
    
    db.close()


def show_trading_help():
    """显示交易帮助"""
    help_text = """
[bold cyan]可用命令：[/bold cyan]

[yellow]账户管理：[/yellow]
  date YYYY-MM-DD          设置模拟日期
  portfolio (p)            查看持仓
  transactions (trans) [N] 查看最近N笔交易记录

[yellow]交易操作：[/yellow]
  buy 代码 数量 [价格]     买入股票（不指定价格则使用当日收盘价）
  sell 代码 数量 [价格]    卖出股票（不指定价格则使用当日收盘价）

[yellow]信息查询：[/yellow]
  price 代码               查询股票当前价格

[yellow]其他：[/yellow]
  help                     显示帮助
  exit/quit                退出

[bold cyan]示例：[/bold cyan]
  date 2020-01-02
  buy 000001 1000
  sell 000001 500
"""
    console.print(help_text)


def show_price(db: Database, symbol: str, date: str):
    """显示股票价格"""
    price_info = db.get_stock_price_on_date(symbol, date)
    
    if not price_info:
        console.print(f"[red]无法获取股票 {symbol} 在 {date} 的价格[/red]")
    else:
        console.print(f"\n[bold cyan]股票 {symbol} - {date}[/bold cyan]")
        console.print(f"开盘: {price_info['open']:.2f}")
        console.print(f"收盘: {price_info['close']:.2f}")
        console.print(f"最高: {price_info['high']:.2f}")
        console.print(f"最低: {price_info['low']:.2f}")
        console.print(f"成交量: {price_info['volume']:,.0f}")


if __name__ == '__main__':
    cli()
