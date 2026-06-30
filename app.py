"""
app.py — Streamlit Data Health Dashboard
========================================
Enterprise-grade UI for the Data Quality Monitoring System (DQMS).

Run with:
    streamlit run app.py

Architecture:
  ┌──────────────────────────────────────────────────────────────┐
  │  Sidebar                                                     │
  │    CSV uploader → DataQualityEngine.run() + clean_data()     │
  │                 → ObservabilityStore                         │
  ├───────────┬──────────────────────────────────────────────────┤
  │  KPI Row  │  Health Score  │  Rows  │  Alerts  │  Status    │
  ├───────────┴──────────────────────────────────────────────────┤
  │  Alert Panel   (color-coded banners, critical first)         │
  ├──────────────────────────┬───────────────────────────────────┤
  │  Completeness bar chart  │  Rule Violation Pie / Bar         │
  ├──────────────────────────┴───────────────────────────────────┤
  │  Validity Details Table                                      │
  ├──────────────────────────────────────────────────────────────┤
  │  Cleaning Summary Panel  (rows removed, cells fixed, log)    │
  ├──────────────────────────────────────────────────────────────┤
  │  Historical Trend Line (health score over time)              │
  ├──────────────────────────────────────────────────────────────┤
  │  Raw / Cleaned Data Preview + Download Buttons               │
  └──────────────────────────────────────────────────────────────┘
"""

import io
import textwrap
from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from config import (
    REPORT_TITLE,
    REPORT_VERSION,
    SEVERITY,
    SYSTEM_NAME,
    THRESHOLDS,
)
from dq_engine import DataQualityEngine, DQReport, CleanSummary, ObservabilityStore

# ---------------------------------------------------------------------------
# PAGE CONFIG — must be the very first Streamlit call
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="DQMS · Data Quality Monitor",
    page_icon="🔬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# GLOBAL STYLE INJECTION
# ---------------------------------------------------------------------------
# Dark-navy + electric-cyan palette.  The signature element is the animated
# health-score ring rendered via a custom HTML/CSS gauge — a deliberate risk
# over a plain metric number; it encodes urgency at a glance.

STYLE = """
<style>
/* ── Base ─────────────────────────────────────────────────────── */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');

html, body, [data-testid="stAppViewContainer"] {
    background: #0b0f1a !important;
    color: #e2e8f0 !important;
    font-family: 'Inter', sans-serif !important;
}

/* sidebar */
[data-testid="stSidebar"] {
    background: #0d1321 !important;
    border-right: 1px solid #1e2d45;
}
[data-testid="stSidebar"] * { color: #cbd5e1 !important; }

/* headers */
h1, h2, h3 { color: #e2e8f0 !important; font-weight: 600 !important; }
h1 { font-size: 1.6rem !important; letter-spacing: -0.5px; }

/* KPI cards */
.kpi-card {
    background: linear-gradient(135deg, #111827 0%, #1a2540 100%);
    border: 1px solid #1e3a5f;
    border-radius: 12px;
    padding: 20px 24px;
    text-align: center;
    transition: border-color .2s;
}
.kpi-card:hover { border-color: #00d4ff; }
.kpi-label {
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: #64748b;
    margin-bottom: 8px;
}
.kpi-value {
    font-size: 2.4rem;
    font-weight: 700;
    color: #00d4ff;
    font-family: 'JetBrains Mono', monospace;
    line-height: 1;
}
.kpi-sub {
    font-size: 0.75rem;
    color: #475569;
    margin-top: 6px;
}

/* Alert banners */
.alert-critical {
    background: rgba(239,68,68,.12);
    border-left: 4px solid #ef4444;
    border-radius: 6px;
    padding: 10px 16px;
    margin-bottom: 8px;
    font-size: 0.85rem;
    color: #fca5a5;
}
.alert-warning {
    background: rgba(245,158,11,.10);
    border-left: 4px solid #f59e0b;
    border-radius: 6px;
    padding: 10px 16px;
    margin-bottom: 8px;
    font-size: 0.85rem;
    color: #fde68a;
}
.alert-info {
    background: rgba(59,130,246,.10);
    border-left: 4px solid #3b82f6;
    border-radius: 6px;
    padding: 10px 16px;
    margin-bottom: 8px;
    font-size: 0.85rem;
    color: #93c5fd;
}

/* Section titles */
.section-title {
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: #00d4ff;
    border-bottom: 1px solid #1e3a5f;
    padding-bottom: 8px;
    margin: 28px 0 16px 0;
}

/* Status badge */
.badge-healthy  { background:#064e3b; color:#6ee7b7; border-radius:20px; padding:3px 12px; font-size:.75rem; font-weight:600; }
.badge-warning  { background:#451a03; color:#fde68a; border-radius:20px; padding:3px 12px; font-size:.75rem; font-weight:600; }
.badge-critical { background:#450a0a; color:#fca5a5; border-radius:20px; padding:3px 12px; font-size:.75rem; font-weight:600; }
.badge-error    { background:#1e1b4b; color:#a5b4fc; border-radius:20px; padding:3px 12px; font-size:.75rem; font-weight:600; }

/* Cleaning summary cards */
.clean-card {
    background: linear-gradient(135deg, #0d1a10 0%, #0f2318 100%);
    border: 1px solid #14532d;
    border-radius: 10px;
    padding: 16px 20px;
    text-align: center;
}
.clean-card-warn {
    background: linear-gradient(135deg, #1a110d 0%, #231608 100%);
    border: 1px solid #78350f;
    border-radius: 10px;
    padding: 16px 20px;
    text-align: center;
}
.clean-value-green { font-size:2rem; font-weight:700; color:#34d399; font-family:'JetBrains Mono',monospace; }
.clean-value-amber { font-size:2rem; font-weight:700; color:#fbbf24; font-family:'JetBrains Mono',monospace; }
.clean-label { font-size:.68rem; font-weight:600; letter-spacing:1.5px; text-transform:uppercase; color:#6b7280; margin-top:4px; }
.clean-sub   { font-size:.72rem; color:#4b5563; margin-top:4px; }

/* Action log rows */
.action-row {
    display:flex; align-items:baseline; gap:10px;
    padding: 6px 0;
    border-bottom: 1px solid #1e2d45;
    font-size:.8rem;
}
.action-pass  { font-size:.65rem; font-weight:700; letter-spacing:1px;
                background:#1e3a5f; color:#38bdf8; border-radius:4px;
                padding:2px 6px; white-space:nowrap; }
.action-cat   { color:#94a3b8; min-width:110px; }
.action-desc  { color:#e2e8f0; flex:1; }
.action-count { font-family:'JetBrains Mono',monospace; color:#f59e0b;
                font-size:.75rem; white-space:nowrap; }

/* Plotly overrides */
.js-plotly-plot .plotly .bg { fill: transparent !important; }

/* Streamlit dataframe */
[data-testid="stDataFrame"] { border: 1px solid #1e3a5f; border-radius: 8px; }
</style>
"""
st.markdown(STYLE, unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# SESSION STATE INITIALISATION
# ---------------------------------------------------------------------------

if "store" not in st.session_state:
    st.session_state.store = ObservabilityStore("dqms_history.db")

if "last_report" not in st.session_state:
    st.session_state.last_report = None

if "last_df" not in st.session_state:
    st.session_state.last_df = None

if "cleaned_df" not in st.session_state:
    st.session_state.cleaned_df = None

if "clean_summary" not in st.session_state:
    st.session_state.clean_summary = None

store: ObservabilityStore = st.session_state.store


# ---------------------------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------------------------

def _health_color(score: int) -> str:
    """Return a hex color appropriate to the health score."""
    if score >= THRESHOLDS["health_warning"]:
        return "#10b981"   # green
    if score >= THRESHOLDS["health_critical"]:
        return "#f59e0b"   # amber
    return "#ef4444"       # red


def _health_gauge(score: int) -> str:
    """
    Render an SVG ring gauge for the health score.
    This is the signature visual element of the dashboard.
    """
    color  = _health_color(score)
    radius = 54
    circ   = 2 * 3.14159 * radius
    dash   = circ * score / 100
    gap    = circ - dash

    status = (
        "HEALTHY"  if score >= THRESHOLDS["health_warning"]  else
        "WARNING"  if score >= THRESHOLDS["health_critical"] else
        "CRITICAL"
    )

    return f"""
    <div style="display:flex;flex-direction:column;align-items:center;padding:8px 0;">
      <svg width="140" height="140" viewBox="0 0 140 140">
        <!-- Background ring -->
        <circle cx="70" cy="70" r="{radius}" fill="none"
                stroke="#1e2d45" stroke-width="12"/>
        <!-- Score arc -->
        <circle cx="70" cy="70" r="{radius}" fill="none"
                stroke="{color}" stroke-width="12"
                stroke-linecap="round"
                stroke-dasharray="{dash:.1f} {gap:.1f}"
                transform="rotate(-90 70 70)"/>
        <!-- Score text -->
        <text x="70" y="65" text-anchor="middle"
              font-family="JetBrains Mono, monospace"
              font-size="26" font-weight="700" fill="{color}">{score}</text>
        <text x="70" y="84" text-anchor="middle"
              font-family="Inter, sans-serif"
              font-size="9" font-weight="600" fill="#64748b"
              letter-spacing="1">HEALTH SCORE</text>
      </svg>
      <span style="font-size:.7rem;font-weight:700;letter-spacing:2px;
                   color:{color};text-transform:uppercase;">{status}</span>
    </div>
    """


def _kpi_card(label: str, value: str, sub: str = "") -> str:
    return f"""
    <div class="kpi-card">
      <div class="kpi-label">{label}</div>
      <div class="kpi-value">{value}</div>
      {'<div class="kpi-sub">' + sub + '</div>' if sub else ''}
    </div>
    """


def _render_alerts(alerts: list[dict]) -> None:
    """Render colour-coded alert banners, critical first."""
    if not alerts:
        st.markdown(
            '<div class="alert-info">✅ &nbsp; No threshold breaches detected. '
            'All rules passed.</div>',
            unsafe_allow_html=True,
        )
        return

    # Sort: critical → warning → info
    order = {SEVERITY["critical"]: 0, SEVERITY["warning"]: 1, SEVERITY["info"]: 2}
    sorted_alerts = sorted(alerts, key=lambda a: order.get(a["severity"], 3))

    for alert in sorted_alerts:
        sev   = alert["severity"]
        icon  = "🔴" if sev == SEVERITY["critical"] else "🟡"
        dim   = alert.get("dimension", "")
        css   = (
            "alert-critical" if sev == SEVERITY["critical"] else
            "alert-warning"  if sev == SEVERITY["warning"]  else
            "alert-info"
        )
        st.markdown(
            f'<div class="{css}">'
            f'{icon} &nbsp;<strong>[{sev}] {dim}</strong> — {alert["message"]}'
            '</div>',
            unsafe_allow_html=True,
        )


def _render_clean_summary(summary: CleanSummary) -> None:
    """
    Render the data cleaning summary panel:
      - Top KPI cards (rows removed, cells imputed, rows retained)
      - Detailed action-by-action log table
    """
    rows_removed  = summary["rows_removed"]
    rows_after    = summary["rows_after"]
    rows_before   = summary["rows_before"]
    cells_imputed = summary["cells_imputed"]
    actions       = summary["actions"]

    # ── KPI mini-cards ──────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)

    with c1:
        css = "clean-card-warn" if rows_removed > 0 else "clean-card"
        val_css = "clean-value-amber" if rows_removed > 0 else "clean-value-green"
        st.markdown(
            f'<div class="{css}">'
            f'<div class="{val_css}">{rows_removed:,}</div>'
            f'<div class="clean-label">Rows Removed</div>'
            f'<div class="clean-sub">{summary["rows_removed_pct"]:.1f}% of original</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    with c2:
        st.markdown(
            f'<div class="clean-card">'
            f'<div class="clean-value-green">{rows_after:,}</div>'
            f'<div class="clean-label">Rows Retained</div>'
            f'<div class="clean-sub">from {rows_before:,} original</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    with c3:
        css = "clean-card-warn" if cells_imputed > 0 else "clean-card"
        val_css = "clean-value-amber" if cells_imputed > 0 else "clean-value-green"
        st.markdown(
            f'<div class="{css}">'
            f'<div class="{val_css}">{cells_imputed:,}</div>'
            f'<div class="clean-label">Cells Imputed</div>'
            f'<div class="clean-sub">nulls & invalids filled</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    with c4:
        dup_removed = summary.get("duplicate_rows_removed", 0)
        st.markdown(
            f'<div class="clean-card">'
            f'<div class="clean-value-green">{summary["action_count"]}</div>'
            f'<div class="clean-label">Actions Applied</div>'
            f'<div class="clean-sub">{dup_removed} duplicate rows dropped</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    st.markdown("<div style='margin-top:16px;'></div>", unsafe_allow_html=True)

    # ── Action log ──────────────────────────────────────────────────────
    if not actions:
        st.markdown(
            '<div class="alert-info">✅ &nbsp; Dataset was already clean — '
            'no remediation actions were required.</div>',
            unsafe_allow_html=True,
        )
        return

    # Render as styled HTML rows rather than a plain dataframe for visual consistency
    rows_html = "".join([
        f'<div class="action-row">'
        f'<span class="action-pass">PASS {a["pass"]}</span>'
        f'<span class="action-cat">{a["category"]}</span>'
        f'<span class="action-desc">{a["action"]} — <em style="color:#64748b;">{a["detail"]}</em></span>'
        f'<span class="action-count">×{a["affected"]:,}</span>'
        f'</div>'
        for a in actions
    ])
    st.markdown(
        f'<div style="background:#0d1321;border:1px solid #1e2d45;'
        f'border-radius:8px;padding:12px 16px;">'
        f'<div style="font-size:.65rem;font-weight:700;letter-spacing:2px;'
        f'color:#475569;text-transform:uppercase;margin-bottom:10px;">Remediation Action Log</div>'
        f'{rows_html}'
        f'</div>',
        unsafe_allow_html=True,
    )


def _completeness_chart(completeness: dict) -> go.Figure:
    """Horizontal bar chart of per-column completeness %."""
    cols   = [c["column"]        for c in completeness["per_column"]]
    scores = [c["completeness"] * 100 for c in completeness["per_column"]]
    colors = [
        "#10b981" if s >= (1 - THRESHOLDS["max_null_fraction"]) * 100
        else "#ef4444"
        for s in scores
    ]
    fig = go.Figure(go.Bar(
        x=scores, y=cols, orientation="h",
        marker_color=colors,
        text=[f"{s:.1f}%" for s in scores],
        textposition="outside",
        textfont=dict(color="#94a3b8", size=11),
    ))
    fig.update_layout(
        title=dict(text="Column Completeness (%)", font=dict(color="#e2e8f0", size=13)),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#94a3b8"),
        xaxis=dict(
            range=[0, 110], gridcolor="#1e2d45", showline=False,
            ticksuffix="%", title=None,
        ),
        yaxis=dict(gridcolor="#1e2d45", title=None, automargin=True),
        margin=dict(l=0, r=40, t=40, b=20),
        height=max(280, len(cols) * 36),
    )
    # Threshold line
    threshold_pct = (1 - THRESHOLDS["max_null_fraction"]) * 100
    fig.add_vline(
        x=threshold_pct, line_dash="dot",
        line_color="#f59e0b", line_width=1.5,
        annotation_text=f"Threshold ({threshold_pct:.0f}%)",
        annotation_font_color="#f59e0b",
        annotation_font_size=10,
    )
    return fig


def _violation_chart(report: DQReport) -> go.Figure:
    """Bar chart of failed rules per DQ dimension."""
    comp_fails = sum(
        1 for c in report["completeness"]["per_column"] if not c["pass"]
    )
    uniq_fails = (
        (0 if report["uniqueness"]["pass"] else 1) +
        sum(1 for c in report["uniqueness"]["per_column"] if not c["pass"])
    )
    val_fails = sum(
        1 for c in report["validity"]["per_column"] if not c["pass"]
    )

    dims   = ["Completeness", "Uniqueness", "Validity"]
    counts = [comp_fails, uniq_fails, val_fails]
    colors = ["#ef4444" if c > 0 else "#10b981" for c in counts]

    fig = go.Figure(go.Bar(
        x=dims, y=counts,
        marker_color=colors,
        text=counts, textposition="outside",
        textfont=dict(color="#94a3b8"),
    ))
    fig.update_layout(
        title=dict(text="Rule Violations by Dimension", font=dict(color="#e2e8f0", size=13)),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#94a3b8"),
        yaxis=dict(gridcolor="#1e2d45", title="Failed Rules"),
        xaxis=dict(title=None),
        margin=dict(l=0, r=0, t=40, b=20),
        height=280,
    )
    return fig


def _dimension_scores_chart(report: DQReport) -> go.Figure:
    """Radar / spider chart of the three DQ dimensions."""
    categories = ["Completeness", "Uniqueness", "Validity"]
    values = [
        report["completeness"]["overall_score"] * 100,
        report["uniqueness"]["overall_score"]   * 100,
        report["validity"]["overall_score"]     * 100,
    ]
    # Close the polygon
    categories_closed = categories + [categories[0]]
    values_closed     = values     + [values[0]]

    fig = go.Figure(go.Scatterpolar(
        r=values_closed,
        theta=categories_closed,
        fill="toself",
        fillcolor="rgba(0,212,255,0.08)",
        line=dict(color="#00d4ff", width=2),
        marker=dict(color="#00d4ff", size=6),
    ))
    fig.update_layout(
        polar=dict(
            bgcolor="rgba(0,0,0,0)",
            radialaxis=dict(
                visible=True, range=[0, 100],
                tickfont=dict(color="#475569", size=9),
                gridcolor="#1e2d45",
            ),
            angularaxis=dict(
                tickfont=dict(color="#94a3b8", size=11),
                gridcolor="#1e2d45",
            ),
        ),
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#94a3b8"),
        title=dict(text="DQ Dimension Scores", font=dict(color="#e2e8f0", size=13)),
        margin=dict(l=20, r=20, t=50, b=20),
        height=300,
    )
    return fig


def _trend_chart(history_df: pd.DataFrame) -> go.Figure:
    """Line chart of health score over historical runs."""
    if history_df.empty:
        fig = go.Figure()
        fig.update_layout(
            annotations=[dict(
                text="No historical data yet. Run DQ checks to populate.",
                xref="paper", yref="paper", x=0.5, y=0.5,
                showarrow=False, font=dict(color="#475569", size=13),
            )],
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            height=250,
        )
        return fig

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=history_df["timestamp"],
        y=history_df["health_score"],
        mode="lines+markers",
        line=dict(color="#00d4ff", width=2),
        marker=dict(color="#00d4ff", size=6),
        name="Health Score",
        fill="tozeroy",
        fillcolor="rgba(0,212,255,0.06)",
    ))
    # Warning & critical bands
    fig.add_hrect(
        y0=0, y1=THRESHOLDS["health_critical"],
        fillcolor="rgba(239,68,68,0.05)", line_width=0,
    )
    fig.add_hrect(
        y0=THRESHOLDS["health_critical"], y1=THRESHOLDS["health_warning"],
        fillcolor="rgba(245,158,11,0.05)", line_width=0,
    )
    fig.update_layout(
        title=dict(text="Health Score Trend (Last 50 Runs)", font=dict(color="#e2e8f0", size=13)),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#94a3b8"),
        yaxis=dict(range=[0, 105], gridcolor="#1e2d45", title="Score"),
        xaxis=dict(gridcolor="#1e2d45", title=None),
        margin=dict(l=0, r=0, t=40, b=20),
        height=280,
        showlegend=False,
    )
    return fig


# ---------------------------------------------------------------------------
# REPORT GENERATION
# ---------------------------------------------------------------------------

def _generate_report(report: DQReport, df: pd.DataFrame) -> str:
    """
    Generate a structured text-based Data Governance & Health Report.
    Designed to be saved as .txt and appended to governance documentation.
    """
    sep  = "=" * 72
    sep2 = "-" * 72
    now  = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    comp = report.get("completeness", {})
    uniq = report.get("uniqueness",   {})
    val  = report.get("validity",     {})

    lines = [
        sep,
        f"  {REPORT_TITLE}",
        f"  {SYSTEM_NAME}  ·  {REPORT_VERSION}",
        f"  Generated: {now}",
        sep,
        "",
        "EXECUTIVE SUMMARY",
        sep2,
        f"  Dataset          : {report.get('dataset_name', 'N/A')}",
        f"  Run ID           : {report.get('run_id', 'N/A')}",
        f"  Execution Time   : {report.get('timestamp', 'N/A')}",
        f"  Total Rows       : {report.get('total_rows', 0):,}",
        f"  Total Columns    : {report.get('total_columns', 0)}",
        f"  Overall Health   : {report.get('health_score', 0)}/100  [{report.get('status', 'N/A')}]",
        f"  Overall Pass     : {'YES ✓' if report.get('overall_pass') else 'NO ✗'}",
        f"  Critical Alerts  : {report.get('critical_alert_count', 0)}",
        f"  Total Alerts     : {report.get('alert_count', 0)}",
        "",
        "DIMENSION SCORES",
        sep2,
        f"  Completeness     : {comp.get('overall_score', 0)*100:.2f}%  "
        f"({'PASS' if comp.get('pass') else 'FAIL'})",
        f"  Uniqueness       : {uniq.get('overall_score', 0)*100:.2f}%  "
        f"({'PASS' if uniq.get('pass') else 'FAIL'})",
        f"  Validity         : {val.get('overall_score', 0)*100:.2f}%  "
        f"({'PASS' if val.get('pass') else 'FAIL'})  "
        f"[{val.get('columns_checked', 0)} columns assessed]",
        "",
    ]

    # ── Completeness detail ─────────────────────────────────────────────
    lines += ["COMPLETENESS DETAIL", sep2]
    for c in comp.get("per_column", []):
        flag = "✓" if c["pass"] else "✗"
        crit = " [CRITICAL KEY]" if c["is_critical"] else ""
        lines.append(
            f"  {flag}  {c['column']:<30} "
            f"Nulls: {c['null_count']:>6} ({c['null_fraction']*100:.1f}%)"
            f"{crit}"
        )
    lines.append("")

    # ── Uniqueness detail ───────────────────────────────────────────────
    lines += ["UNIQUENESS DETAIL", sep2]
    lines.append(
        f"  Duplicate rows   : {uniq.get('duplicate_row_count', 0):,} "
        f"({uniq.get('duplicate_row_fraction', 0)*100:.2f}%)"
    )
    for c in uniq.get("per_column", []):
        flag = "✓" if c["pass"] else "✗"
        lines.append(
            f"  {flag}  {c['column']:<30} "
            f"Unique: {c['unique_count']:>6} / {c['total_non_null']:>6} "
            f"({(1-c['duplicate_fraction'])*100:.1f}% unique)"
        )
    lines.append("")

    # ── Validity detail ─────────────────────────────────────────────────
    lines += ["VALIDITY DETAIL", sep2]
    if not val.get("per_column"):
        lines.append("  No format-validatable columns detected in this dataset.")
    for c in val.get("per_column", []):
        flag = "✓" if c["pass"] else "✗"
        lines.append(
            f"  {flag}  {c['column']:<30} "
            f"Type: {c['validation_type']:<14} "
            f"Valid: {c['valid_count']:>6} / {c['valid_count']+c['invalid_count']:>6} "
            f"({c['validity_fraction']*100:.1f}%)"
        )
        if c.get("invalid_examples"):
            examples = ", ".join(c["invalid_examples"][:3])
            lines.append(f"       Invalid examples: {examples}")
    lines.append("")

    # ── Alerts ──────────────────────────────────────────────────────────
    lines += ["ACTIVE ALERTS", sep2]
    alerts = report.get("alerts", [])
    if not alerts:
        lines.append("  No alerts triggered. All rules within thresholds.")
    for a in alerts:
        lines.append(f"  [{a['severity']:<8}] [{a['dimension']:<14}] {a['message']}")
    lines.append("")

    # ── Governance framework ─────────────────────────────────────────────
    lines += [
        "GOVERNANCE FRAMEWORK & RECOMMENDATIONS",
        sep2,
        "",
        "  1. DATA STEWARDSHIP",
        "     · Assign a named Data Steward for each critical column.",
        "     · Document data lineage from source system to analytics layer.",
        "     · Implement column-level access controls for PII fields.",
        "",
        "  2. REMEDIATION PLAYBOOK",
        "     · CRITICAL alerts (health < 60) → block pipeline; notify Data Eng team immediately.",
        "     · WARNING alerts (health 60–79)  → flag for review within 24 hours.",
        "     · Null spikes in critical columns → trigger upstream source system audit.",
        "     · Duplicate key violations        → enforce DB-level UNIQUE constraints.",
        "",
        "  3. SLA TARGETS",
        f"     · Overall Health Score target  : ≥ {THRESHOLDS['health_warning']}",
        f"     · Maximum null fraction        : {THRESHOLDS['max_null_fraction']*100:.0f}% per column",
        f"     · Maximum duplicate fraction   : {THRESHOLDS['max_duplicate_fraction']*100:.0f}% per dataset",
        f"     · Minimum validity fraction    : {THRESHOLDS['min_validity_fraction']*100:.0f}% per checked column",
        "",
        "  4. MONITORING CADENCE",
        "     · Run DQ checks on every pipeline ingestion event.",
        "     · Weekly trend review via the Historical Trend chart.",
        "     · Monthly governance report distributed to stakeholders.",
        "",
        "  5. ESCALATION PATH",
        "     · Severity CRITICAL → Data Engineering Lead → CTO",
        "     · Severity WARNING  → Assigned Data Steward",
        "     · Auto-email alerts simulated; integrate with SMTP/SendGrid for production.",
        "",
        sep,
        "  End of Report",
        sep,
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SIDEBAR
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown(
        '<div style="text-align:center;padding:16px 0 24px;">'
        '<span style="font-size:1.8rem;">🔬</span>'
        '<div style="font-size:1rem;font-weight:700;color:#00d4ff;margin-top:6px;">DQMS</div>'
        '<div style="font-size:.65rem;color:#475569;letter-spacing:1.5px;'
        'text-transform:uppercase;">Data Quality Monitor</div>'
        '</div>',
        unsafe_allow_html=True,
    )

    st.markdown("**Upload Dataset**")
    uploaded = st.file_uploader(
        "Drop a CSV file here",
        type=["csv"],
        help="Upload any CSV file. The engine will auto-detect column types "
             "and apply the appropriate validation rules.",
    )

    delimiter = st.selectbox(
        "CSV Delimiter",
        options=[",", ";", "\t", "|"],
        index=0,
        help="Choose the delimiter used in your CSV file.",
    )

    run_btn = st.button(
        "▶  Run DQ Analysis",
        use_container_width=True,
        type="primary",
        disabled=(uploaded is None),
    )

    st.markdown("---")
    st.markdown("**Thresholds (read-only)**")
    st.caption(f"Max null fraction: {THRESHOLDS['max_null_fraction']*100:.0f}%")
    st.caption(f"Max duplicate fraction: {THRESHOLDS['max_duplicate_fraction']*100:.0f}%")
    st.caption(f"Min validity fraction: {THRESHOLDS['min_validity_fraction']*100:.0f}%")
    st.caption(f"Health warning floor: {THRESHOLDS['health_warning']}")
    st.caption(f"Health critical floor: {THRESHOLDS['health_critical']}")

    history_df = store.load_history()
    run_count  = store.get_run_count()
    st.markdown("---")
    st.caption(f"📦 Historical runs stored: **{run_count}**")


# ---------------------------------------------------------------------------
# MAIN CONTENT
# ---------------------------------------------------------------------------

# Page header
st.markdown(
    '<div style="display:flex;align-items:center;gap:12px;margin-bottom:4px;">'
    '<span style="font-size:1.6rem;">🔬</span>'
    '<div>'
    '<h1 style="margin:0;">Data Quality Monitoring System</h1>'
    '<div style="font-size:.75rem;color:#475569;letter-spacing:1px;">'
    'Enterprise Data Governance · Automated Validation · Observability'
    '</div></div></div>',
    unsafe_allow_html=True,
)

# ── RUN ANALYSIS ─────────────────────────────────────────────────────────────

if run_btn and uploaded is not None:
    try:
        # Parse CSV with selected delimiter
        df = pd.read_csv(uploaded, delimiter=delimiter)

        if df.empty:
            st.error("The uploaded CSV is empty. Please upload a file with data.")
        else:
            with st.spinner("Running data quality analysis and auto-cleaning…"):
                engine = DataQualityEngine(df, dataset_name=uploaded.name)
                # 1. Full DQ report
                report = engine.run()
                store.save_run(report)
                st.session_state.last_report = report
                st.session_state.last_df     = df
                # 2. Auto-clean the dataset (runs checks internally, then remediates)
                cleaned_df, clean_summary = engine.clean_data()
                st.session_state.cleaned_df    = cleaned_df
                st.session_state.clean_summary = clean_summary
            st.success(
                f"Analysis & cleaning complete — Run ID: **{report['run_id']}** · "
                f"Rows removed: **{clean_summary['rows_removed']:,}** · "
                f"Cells imputed: **{clean_summary['cells_imputed']:,}**"
            )
            # Reload history after save
            history_df = store.load_history()

    except pd.errors.ParserError as e:
        st.error(f"Failed to parse CSV: {e}. Try a different delimiter.")
    except Exception as e:
        st.error(f"Unexpected error during analysis: {e}")

# ── DISPLAY RESULTS ──────────────────────────────────────────────────────────

report: DQReport | None           = st.session_state.last_report
df: pd.DataFrame | None           = st.session_state.last_df
cleaned_df: pd.DataFrame | None   = st.session_state.cleaned_df
clean_summary: CleanSummary | None = st.session_state.clean_summary

if report is None:
    # Landing state — no data yet
    st.markdown(
        '<div style="text-align:center;padding:80px 0;">'
        '<div style="font-size:3rem;margin-bottom:16px;">📂</div>'
        '<div style="font-size:1.1rem;color:#64748b;">'
        'Upload a CSV file and click <strong style="color:#00d4ff;">Run DQ Analysis</strong> '
        'to begin.</div>'
        '<div style="font-size:.8rem;color:#475569;margin-top:8px;">'
        'The engine will automatically detect column types and apply the '
        'appropriate validation rules.</div>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.stop()

# If report has an error key, show it prominently
if "error" in report:
    st.error(f"Engine Error: {report['error']}")
    with st.expander("Full traceback"):
        st.code(report.get("traceback", "No traceback available."))
    st.stop()

# ── KPI ROW ──────────────────────────────────────────────────────────────────

st.markdown('<div class="section-title">Dataset Overview</div>', unsafe_allow_html=True)

gauge_col, kpi1, kpi2, kpi3, kpi4 = st.columns([1.4, 1, 1, 1, 1])

with gauge_col:
    st.markdown(_health_gauge(report["health_score"]), unsafe_allow_html=True)

with kpi1:
    st.markdown(
        _kpi_card("Total Rows", f"{report['total_rows']:,}",
                  f"{report['total_columns']} columns"),
        unsafe_allow_html=True,
    )

with kpi2:
    dup_pct = report["uniqueness"]["duplicate_row_fraction"] * 100
    st.markdown(
        _kpi_card("Duplicate Rows",
                  str(report["uniqueness"]["duplicate_row_count"]),
                  f"{dup_pct:.2f}% of dataset"),
        unsafe_allow_html=True,
    )

with kpi3:
    total_nulls = report["completeness"]["total_nulls"]
    null_pct    = total_nulls / max(report["total_rows"] * report["total_columns"], 1) * 100
    st.markdown(
        _kpi_card("Total Nulls", f"{total_nulls:,}", f"{null_pct:.2f}% of cells"),
        unsafe_allow_html=True,
    )

with kpi4:
    crit = report["critical_alert_count"]
    warn = report["alert_count"] - crit
    st.markdown(
        _kpi_card("Active Alerts",
                  str(report["alert_count"]),
                  f"🔴 {crit} Critical  🟡 {warn} Warnings"),
        unsafe_allow_html=True,
    )

# Dataset name + status badge
status = report["status"]
badge_cls = {
    "HEALTHY":  "badge-healthy",
    "WARNING":  "badge-warning",
    "CRITICAL": "badge-critical",
}.get(status, "badge-error")

st.markdown(
    f'<div style="margin:12px 0 4px;">'
    f'<span style="color:#64748b;font-size:.8rem;">Dataset: '
    f'<strong style="color:#94a3b8;">{report["dataset_name"]}</strong> &nbsp;·&nbsp; '
    f'Run: <code style="color:#00d4ff;">{report["run_id"]}</code> &nbsp;·&nbsp; '
    f'{report["timestamp"]}</span>'
    f' &nbsp; <span class="{badge_cls}">{status}</span>'
    f'</div>',
    unsafe_allow_html=True,
)

# ── ALERT PANEL ──────────────────────────────────────────────────────────────

st.markdown('<div class="section-title">Alert Panel</div>', unsafe_allow_html=True)
_render_alerts(report.get("alerts", []))

# ── CHARTS ROW 1: Completeness + Violations ──────────────────────────────────

st.markdown('<div class="section-title">Data Quality Metrics</div>', unsafe_allow_html=True)

chart_c1, chart_c2, chart_c3 = st.columns([2, 1.2, 1.2])

with chart_c1:
    st.plotly_chart(
        _completeness_chart(report["completeness"]),
        use_container_width=True, config={"displayModeBar": False},
    )

with chart_c2:
    st.plotly_chart(
        _violation_chart(report),
        use_container_width=True, config={"displayModeBar": False},
    )

with chart_c3:
    st.plotly_chart(
        _dimension_scores_chart(report),
        use_container_width=True, config={"displayModeBar": False},
    )

# ── VALIDITY DETAIL TABLE ─────────────────────────────────────────────────────

val_cols = report["validity"].get("per_column", [])
if val_cols:
    st.markdown('<div class="section-title">Validity Detail</div>', unsafe_allow_html=True)
    val_df = pd.DataFrame([{
        "Column":          c["column"],
        "Type":            c["validation_type"],
        "Valid":           c["valid_count"],
        "Invalid":         c["invalid_count"],
        "Validity %":      f"{c['validity_fraction']*100:.1f}%",
        "Pass":            "✓" if c["pass"] else "✗",
        "Invalid Examples": ", ".join(c.get("invalid_examples", [])[:3]),
    } for c in val_cols])
    st.dataframe(val_df, use_container_width=True, hide_index=True)
else:
    st.info(
        "No format-checkable columns detected. Add columns named email, phone, "
        "date, age, salary, url, zip, etc. to trigger validity checks."
    )

# ── CLEANING SUMMARY PANEL ────────────────────────────────────────────────────

st.markdown('<div class="section-title">Auto-Cleaning Summary</div>', unsafe_allow_html=True)
if clean_summary is not None:
    _render_clean_summary(clean_summary)
else:
    st.markdown(
        '<div class="alert-info">ℹ &nbsp; Cleaning summary not yet available. '
        'Run an analysis to generate it.</div>',
        unsafe_allow_html=True,
    )

# ── HISTORICAL TREND ─────────────────────────────────────────────────────────

st.markdown('<div class="section-title">Historical Trend</div>', unsafe_allow_html=True)
st.plotly_chart(
    _trend_chart(history_df),
    use_container_width=True, config={"displayModeBar": False},
)

if not history_df.empty:
    with st.expander("View run history table"):
        display_hist = history_df.copy()
        display_hist.columns = [c.replace("_", " ").title() for c in display_hist.columns]
        st.dataframe(display_hist, use_container_width=True, hide_index=True)

# ── DATA PREVIEW ─────────────────────────────────────────────────────────────

if df is not None:
    raw_tab, clean_tab = st.tabs([
        f"📄 Raw Data  ({report['total_rows']:,} rows)",
        f"✨ Cleaned Data  ({len(cleaned_df):,} rows)" if cleaned_df is not None
        else "✨ Cleaned Data",
    ])
    with raw_tab:
        st.dataframe(df.head(100), use_container_width=True)
        st.caption(
            f"Showing first 100 of {report['total_rows']:,} rows · "
            f"{report['total_columns']} columns  ·  Original unmodified upload"
        )
    with clean_tab:
        if cleaned_df is not None:
            st.dataframe(cleaned_df.head(100), use_container_width=True)
            rows_diff = report["total_rows"] - len(cleaned_df)
            st.caption(
                f"Showing first 100 of {len(cleaned_df):,} rows · "
                f"{len(cleaned_df.columns)} columns  ·  "
                f"{rows_diff:,} rows removed, {clean_summary['cells_imputed']:,} cells imputed"
            )
        else:
            st.info("Run the analysis to generate the cleaned dataset.")

# ── REPORT DOWNLOAD ───────────────────────────────────────────────────────────

st.markdown('<div class="section-title">Download</div>', unsafe_allow_html=True)

dl_col1, dl_col2 = st.columns(2)

with dl_col1:
    if df is not None:
        report_text = _generate_report(report, df)
        st.download_button(
            label="⬇  Download Governance Report (.txt)",
            data=report_text.encode("utf-8"),
            file_name=f"dq_report_{report['run_id']}_{datetime.utcnow().strftime('%Y%m%d')}.txt",
            mime="text/plain",
            use_container_width=True,
        )
        st.caption("Full governance framework, dimension scores, alerts, and remediation playbook.")

with dl_col2:
    if cleaned_df is not None:
        csv_buffer = io.StringIO()
        cleaned_df.to_csv(csv_buffer, index=False)
        rows_removed = report["total_rows"] - len(cleaned_df)
        cells_fixed  = clean_summary["cells_imputed"] if clean_summary else 0
        st.download_button(
            label="⬇  Download Cleaned Dataset (.csv)",
            data=csv_buffer.getvalue().encode("utf-8"),
            file_name=f"cleaned_{report['run_id']}_{datetime.utcnow().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            use_container_width=True,
        )
        st.caption(
            f"Auto-cleaned output · {len(cleaned_df):,} rows · "
            f"{rows_removed:,} rows removed · {cells_fixed:,} cells imputed."
        )
    elif df is not None:
        st.info("Cleaned dataset not yet generated. Run the analysis first.")

# ── FOOTER ────────────────────────────────────────────────────────────────────

st.markdown(
    '<div style="text-align:center;padding:40px 0 20px;'
    'color:#334155;font-size:.7rem;letter-spacing:1px;">'
    'DATA QUALITY MONITORING SYSTEM  ·  DATA ENGINEERING DIVISION  ·  '
    f'BUILD {REPORT_VERSION}'
    '</div>',
    unsafe_allow_html=True,
)