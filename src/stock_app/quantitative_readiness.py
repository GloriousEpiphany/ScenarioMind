"""Describe actual evidence availability without granting release eligibility."""
from collections import Counter
from datetime import datetime


def quantitative_readiness(repository, research, refs):
    manifest = research.get('manifest', research.get('evidence', ()))
    if not research.get('as_of') or not manifest:
        return {'evidence_complete': False, 'summaries': {
            key: '本次证据准备尚未完成，保留具体采集失败。'
            for key in ('joint_calibration', 'multiperiod_optimization', 'trade_eligibility')}}
    cutoff = datetime.fromisoformat(research['as_of'])
    rows = [repository.get_visible(row['artifact_id'], cutoff) for row in manifest]
    counts = Counter(row.dataset for row in rows)
    prices = [row for row in rows if row.domain == 'price']
    return {
        'evidence_complete': True,
        'snapshot_manifest_counts': dict(sorted(counts.items())),
        'first_actual_price_observation': min((row.known_at for row in prices), default=cutoff).isoformat(),
        'price_rows_observed_after_session_day': sum(row.known_at.date() > row.event_time.date() for row in prices),
        'summaries': {
            'joint_calibration': ('当前市场证据已齐；还需真实历史可见时间、公司行动与事件条件的成熟验证样本。'
                '今天下载的历史行情不等于当时保存的样本；事后投影重建与原预测档案回放分开记录，均需独立样本验证。'),
            'multiperiod_optimization': ('多期求解须通过独立成熟路径验证、概率集合校准及压力检验。'
                '分币结算和分红到账规则已进入工程校验，缺真实输入时仍不通过。'),
            'trade_eligibility': ('总持仓与现金可用于估值；当前可卖、冻结、账户权限及实际佣金条款仍需核验。'
                '公开税费和佣金最低收费规则不能替代账户费用合同。'),
        },
        'supplied_validation_input_keys': sorted(refs),
    }
