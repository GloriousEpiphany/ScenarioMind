from copy import deepcopy
import pytest
from scenario_engine.errors import ContractViolation
from src.stock_app.profit_control_discovery import compare_records


def test_zero_placeholders_and_other_metric_never_make_false_controls():
    base={'SECURITY_CODE':'002230','REPORT_DATE':'2016-06-30','NOTICE_DATE':'2016-04-26',
          'PREDICT_FINANCE_CODE':'004','PREDICT_AMT_LOWER':100,'PREDICT_AMT_UPPER':200}
    actual={'SECURITY_CODE':'002230','REPORTDATE':'2016-06-30','NOTICE_DATE':'2016-08-10','PARENT_NETPROFIT':250}
    zero={**base,'PREDICT_AMT_LOWER':0,'PREDICT_AMT_UPPER':0}
    other={**base,'PREDICT_FINANCE_CODE':'001'}
    result=compare_records([base,zero,other],[actual],'002230')
    assert result['candidate_counts']=={'below':0,'within':0,'above':1}
    assert not result['candidates'][0]['eligible_training_label']
    assert result['excluded'][0]['reason']=='zero_placeholder_requires_original_verification'
    assert compare_records([base],[actual,deepcopy(actual)],'002230')['candidates']==[]
    with pytest.raises(ContractViolation):compare_records([base],[{**actual,'SECURITY_CODE':'000001'}],'002230')
