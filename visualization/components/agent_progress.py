"""Render a ScenarioProgressViewModel without inferring business progress."""

from __future__ import annotations

from html import escape


def render_agent_progress(view_model, *, streamlit_module=None) -> None:
    st = streamlit_module
    if st is None:
        import streamlit as st
    stages = " · ".join(
        f"✓ {escape(stage)}" if stage in view_model.completed_stages else escape(stage)
        for stage in ("capability", "snapshot", "evidence", "features", "root_quant", "branches", "portfolio", "critics", "report")
    )
    heartbeat = "响应延迟" if view_model.heartbeat_status == "delayed" else "心跳正常"
    current = escape(view_model.current_agent or "等待 Agent")
    parallel = f" · 另有 {view_model.parallel_agent_count} 个 Agent 并行" if view_model.parallel_agent_count else ""
    units = ""
    if view_model.current_completed_units is not None:
        total = "评估中" if view_model.current_total_units is None else str(view_model.current_total_units)
        units = f" · {view_model.current_completed_units}/{total} 单位"
    replanned = f" · 新增 {view_model.replanned_units} 个验证任务" if view_model.replanned_units else ""
    st.markdown(
        f"""
        <style>
        .scenario-progress {{position:sticky;top:0;z-index:20;padding:14px 16px;border:1px solid #284f68;
          border-radius:12px;background:rgba(9,24,35,.96);color:#ecf7ff}}
        .scenario-track {{height:8px;border-radius:999px;background:#183442;overflow:hidden;margin:10px 0}}
        .scenario-fill {{height:100%;width:{int(view_model.percent)}%;background:linear-gradient(90deg,#2d7ff9,#27d4c3);
          transition:width .3s ease}}
        .scenario-meta {{font-size:.86rem;color:#a9c3d2}}
        @media (prefers-reduced-motion: reduce) {{.scenario-fill {{transition:none}}}}
        </style>
        <section class="scenario-progress" role="status" aria-live="polite">
          <div><strong>{escape(view_model.status)} · {int(view_model.percent)}%</strong></div>
          <div class="scenario-track"><div class="scenario-fill"></div></div>
          <div>{current}{escape(parallel)}{escape(units)}{escape(replanned)}</div>
          <div class="scenario-meta">{escape(stages)} · {heartbeat}</div>
        </section>
        """,
        unsafe_allow_html=True,
    )
