"""Checkpointed source binding -> reconstruction -> independent statistical score."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
import sqlite3

from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ScenarioEngineError, ContractViolation
from scenario_engine.quant.historical_projection import ReconstructionPolicy, policy_payload
from scenario_engine.quant.joint_calibration import JointCalibrationPolicy
from scenario_engine.quant.reconstruction_scoring import ReconstructionScorePolicy
from src.stock_app.disclosure_wealth_inputs import build_disclosure_wealth_panel
from src.stock_app.historical_projection import reconstruct_historical_projections
from src.stock_app.quantitative_inputs import derived_artifact
from src.stock_app.reconstruction_scoring import score_historical_reconstruction


def run_reconstructed_simulation(db_path, run_id, *, total_return_panel_ref, disclosure_coverages,
                                reconstruction_plan, statistical_limits, weights, action_sources_by_origin,
                                max_ablation_energy_degradation, clock=None):
    from scenario_engine.agents.lease import advisory_lease
    with advisory_lease(db_path,'reconstructed-simulation:'+run_id) as lost:
        return _run_reconstructed_simulation(db_path,run_id,total_return_panel_ref=total_return_panel_ref,
            disclosure_coverages=disclosure_coverages,reconstruction_plan=reconstruction_plan,
            statistical_limits=statistical_limits,weights=weights,action_sources_by_origin=action_sources_by_origin,
            max_ablation_energy_degradation=max_ablation_energy_degradation,clock=clock,lease_lost=lost)


def _run_reconstructed_simulation(db_path, run_id, *, total_return_panel_ref, disclosure_coverages,
                                 reconstruction_plan, statistical_limits, weights, action_sources_by_origin,
                                 max_ablation_energy_degradation, clock, lease_lost):
    def require_lease():
        if lease_lost.is_set():
            raise ContractViolation('simulation workflow lease lost; checkpoint cannot advance')

    clock=clock or (lambda:datetime.now(timezone.utc))
    contract={'version':'reconstructed-simulation-workflow-v1','total_return_panel_ref':total_return_panel_ref,
        'disclosure_coverages':disclosure_coverages,'reconstruction_plan':reconstruction_plan,
        'statistical_limits':statistical_limits,'weights':weights,'action_sources_by_origin':action_sources_by_origin,
        'max_ablation_energy_degradation':max_ablation_energy_degradation}
    root='reconstructed-simulation:'+run_id
    result={'run_id':run_id,'status':'blocked','stages':{},'publication_eligible':False,
            'order_submission_enabled':False,'model_calls':0}
    with sqlite3.connect(db_path,timeout=30) as c:
        repo,board=MarketArtifactRepository(c),ResearchBoard(c)
        board.register_run(root,contract)
        saved=board.load(root,'result',contract)
        if saved:
            # Reopening a completed immutable evaluation never changes protocols
            # or selects a new threshold/window after observing results.
            repo.get_visible(saved['workflow_ref'],clock())
            return saved
        try:
            from src.stock_app.reconstruction_readiness import audit_reconstruction_readiness
            result['stages']['independent_prerequisites']=audit_reconstruction_readiness(repo,
                total_return_panel_ref=total_return_panel_ref,reconstruction_plan=reconstruction_plan,
                statistical_limits=statistical_limits,at=clock())
            panel=board.load(root,'panel',contract)
            if panel is None:
                panel=build_disclosure_wealth_panel(repo,total_return_panel_ref=total_return_panel_ref,
                    coverage_refs=disclosure_coverages,captured_at=clock())
                require_lease()
                board.save(root,'panel',contract,panel)
            result['stages']['source_binding']=panel
            protocols=board.load(root,'protocols',contract)
            if protocols is None:
                require_lease()
                at=clock()
                if {'generated_at','version'} & set(reconstruction_plan) or {'registered_at','evaluation_start','version'} & set(statistical_limits):
                    raise ContractViolation('workflow timestamps and algorithm protocol versions cannot be caller-backdated')
                policy=ReconstructionPolicy(version='retrospective-projection-v1',generated_at=at,**reconstruction_plan)
                item=derived_artifact('historical_projection_protocol','historical-projection-protocol-v1',policy_payload(policy),at)
                repo.ingest_many((item,))
                risk=JointCalibrationPolicy(version='retrospective-numerical-limits-v1',registered_at=at,evaluation_start=at,**statistical_limits)
                score=ReconstructionScorePolicy('retrospective-cash-wealth-score-v1',at,item.artifact_id,
                    tuple(repo.get_visible(panel['panel_ref'],at).payload['event_feature_ids']),risk,max_ablation_energy_degradation)
                score_item=derived_artifact('reconstruction_score_protocol','reconstruction-score-protocol-v1',
                    json.loads(json.dumps(asdict(score),default=lambda t:t.isoformat())),at)
                repo.ingest_many((score_item,))
                protocols={'reconstruction_protocol_ref':item.artifact_id,'score_protocol_ref':score_item.artifact_id}
                board.save(root,'protocols',contract,protocols)
            reconstruction=board.load(root,'reconstruction',contract)
            if reconstruction is None:
                reconstruction=reconstruct_historical_projections(repo,panel_ref=panel['panel_ref'],
                    protocol_ref=protocols['reconstruction_protocol_ref'],action_sources_by_origin=action_sources_by_origin,at=clock())
                require_lease()
                board.save(root,'reconstruction',contract,reconstruction)
            result['stages']['reconstruction']=reconstruction
            score=score_historical_reconstruction(repo,reconstruction_ref=reconstruction['report_ref'],
                score_protocol_ref=protocols['score_protocol_ref'],weights=weights,at=clock())
            result['stages']['statistical_scoring']=score
            result['status']='conditional_diagnostics_passed' if score['conditional_diagnostics_passed'] else 'rejected' if score['statistical_scoring_completed'] else 'blocked'
            result['workflow_completed']=score['statistical_scoring_completed']
        except (ScenarioEngineError,TypeError,ValueError,KeyError) as exc:
            result['blocking_findings']=[{'error_code':getattr(exc,'code','invalid_simulation_contract'),
                                        'reason':str(exc),'context':getattr(exc,'context',{})}]
            result['workflow_completed']=False
        require_lease()
        artifact=derived_artifact('reconstructed_simulation_run','reconstructed-simulation-run-v1',result,clock())
        repo.ingest_many((artifact,)); result['workflow_ref']=artifact.artifact_id
        # A fully frozen reconstruction, including its failed folds, is an
        # auditable result. New data requires a new run; acquisition can resume
        # separately until a complete coverage reference exists.
        if 'reconstruction' in result['stages']:
            board.save(root,'result',contract,result)
        return result
