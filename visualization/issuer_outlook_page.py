"""Read-only view of original issuer research from the configured local research database."""
import json
import os
from pathlib import Path
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

import config
from scenario_engine.data import canonical_payload_hash
from scenario_engine.errors import ContractViolation


def show_issuer_outlook_page():
    path=Path(os.environ.get('STOCKM1_FORECAST_DB_PATH') or config.DATABASE_PATH)
    st.subheader('已登记的前瞻研究')
    if not path.is_file():
        st.info('当前研究库还没有已登记的报告。')
        return
    try:
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as c:
            c.execute('PRAGMA query_only=ON')
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_artifacts'").fetchone():
                st.info('当前研究库还没有已登记的报告。')
                return
            rows=c.execute("SELECT artifact_id,entity_id,known_at FROM market_artifacts "
                "WHERE domain='quant_validation' AND dataset='issuer_outlook_report' "
                "AND schema_version='issuer-outlook-v1' ORDER BY julianday(known_at) DESC,artifact_id").fetchall()
            if not rows:
                st.info('当前研究库还没有已登记的报告。')
                return
            labels={ref:f"{symbol} · {datetime.fromisoformat(at).astimezone(ZoneInfo('Asia/Shanghai')):%Y-%m-%d %H:%M} 北京时间"
                    for ref,symbol,at in rows}
            ref=st.selectbox('研究原件',list(labels),format_func=labels.get)
            raw,digest=c.execute('SELECT payload_json,payload_hash FROM market_artifacts WHERE artifact_id=?',(ref,)).fetchone()
            report=json.loads(raw)
            if canonical_payload_hash(report)!=digest:
                raise ContractViolation('研究报告的内容与登记哈希不符')
        st.caption('以下是原始研究输出，尚待独立样本校准。')
        event=report['stages']['event_forecast']
        conditional=report['stages']['conditional_returns']
        if 'forecast_ref' in event:
            st.markdown('**发行人是否发布公告**')
            st.write('原点日期',event['origin_date'])
            st.dataframe([{'期限（交易日）':int(h),'研究概率':f"{v['research_probability']:.1%}" if
                v['status']=='shadow_forecast' else '未出具','训练原点数':v.get('training_rows'),
                '状态':'原点已登记' if v['status']=='shadow_forecast' else (
                    '训练数据只有一种结果' if v.get('reason')=='only one event outcome observed for this horizon'
                    else v.get('reason','输入不足'))} for h,v in event['forecasts'].items()],hide_index=True)
        else:
            st.info('公告预测资料不足。'+event.get('reason',''))
        if 'report_ref' in conditional:
            st.markdown('**条件总收益路径**')
            st.caption(f"{conditional['training_labels']} 个收益标签，每种情景 {conditional['sample_count']} 条路径。"
                       "各列为税前股利再投资收益的样本分位数。")
            names={'no_publication':'各窗口均无公告','first_window_publication':'仅第一窗口出现公告'}
            table=[]
            for scenario,horizons in conditional['sample_quantiles_q10_q50_q90'].items():
                for h,values in sorted(horizons.items(),key=lambda pair:int(pair[0])):
                    table.append({'情景':names[scenario],'交易步':int(h),**{
                        k:f'{v:.2%}' for k,v in zip(('10% 分位','中位数','90% 分位'),values)}})
            st.dataframe(table,hide_index=True)
        else:
            st.info('条件收益路径资料不足。'+conditional.get('reason',''))
    except (sqlite3.Error,ContractViolation,KeyError,TypeError,ValueError) as exc:
        st.error('无法核验这份前瞻研究报告。'+str(exc))


if __name__=='__main__':
    st.set_page_config(page_title='发行人前瞻研究',layout='wide')
    show_issuer_outlook_page()
