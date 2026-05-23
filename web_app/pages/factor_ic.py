"""Page 2: 因子 IC 分析."""

import dash
import dash_bootstrap_components as dbc
import numpy as np
import plotly.graph_objects as go
from dash import Input, Output, callback, dash_table, dcc, html

from web_app.components.common import create_no_data_message
from web_app.data_service import load_factor_ic_data

dash.register_page(__name__, path="/factor-ic", name="因子IC分析", title="因子IC分析 - 港股量化分析")

# ─── 布局 ──────────────────────────────────────────────────────────────────────


def layout(**kwargs):  # type: ignore
    """页面布局."""
    return html.Div([
        html.H4("因子 IC 分析", className="mb-3"),

        # 控制面板
        dbc.Row([
            dbc.Col([
                html.Label("因子集", className="text-muted"),
                dbc.Select(
                    id="factor-set-selector",
                    options=[
                        {"label": "Qlib Alpha158", "value": "qlib_alpha158"},
                        {"label": "Qlib Alpha360", "value": "qlib_alpha360"},
                    ],
                    value="qlib_alpha158",
                ),
            ], md=3),
            dbc.Col([
                html.Label("预测周期 (天)", className="text-muted"),
                dbc.Select(
                    id="horizon-selector",
                    options=[
                        {"label": "5天", "value": "5"},
                        {"label": "10天", "value": "10"},
                        {"label": "20天", "value": "20"},
                        {"label": "40天", "value": "40"},
                        {"label": "60天", "value": "60"},
                    ],
                    value="20",
                ),
            ], md=3),
        ], className="mb-4"),

        # IC 时序图
        dbc.Row([
            dbc.Col([
                html.H6("IC 时间序列", className="text-muted mb-2"),
                dcc.Graph(id="ic-timeseries-chart"),
            ], md=6),
            dbc.Col([
                html.H6("Rank IC 时间序列", className="text-muted mb-2"),
                dcc.Graph(id="rank-ic-timeseries-chart"),
            ], md=6),
        ]),

        html.Hr(className="my-3"),

        # Top 因子柱状图 + 汇总表
        dbc.Row([
            dbc.Col([
                html.H6("Top 10 因子 (按 IC 均值)", className="text-muted mb-2"),
                dcc.Graph(id="top-factors-bar-chart"),
            ], md=5),
            dbc.Col([
                html.H6("因子 IC 统计汇总", className="text-muted mb-2"),
                html.Div(id="factor-summary-table"),
            ], md=7),
        ]),
    ])


# ─── 回调 ──────────────────────────────────────────────────────────────────────


@callback(
    Output("ic-timeseries-chart", "figure"),
    Output("rank-ic-timeseries-chart", "figure"),
    Output("top-factors-bar-chart", "figure"),
    Output("factor-summary-table", "children"),
    Input("factor-set-selector", "value"),
    Input("horizon-selector", "value"),
)
def update_factor_ic(factor_set, horizon):  # type: ignore
    """更新因子 IC 分析图表."""
    horizon_int = int(horizon) if horizon else 20
    data = load_factor_ic_data(factor_set=factor_set, horizon=horizon_int)

    empty_fig = go.Figure()
    empty_fig.add_annotation(text="暂无数据", xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False)
    empty_fig.update_layout(template="plotly_dark", height=350)

    if data is None:
        no_data = create_no_data_message("暂无因子IC数据")
        return empty_fig, empty_fig, empty_fig, no_data

    dates = data["dates"]
    factors = data["factors"]
    ic_series = data["ic_series"]
    rank_ic_series = data["rank_ic_series"]
    summary_df = data["summary"]

    # ─── IC 时序图 ─────────────────────────────────────────────────────────
    ic_fig = go.Figure()
    # 显示前5个因子的 IC 时序
    top5 = sorted(factors, key=lambda f: abs(np.mean(ic_series[f])), reverse=True)[:5]
    for f in top5:
        ic_fig.add_trace(go.Scatter(
            x=dates, y=ic_series[f],
            mode="lines", name=f,
            line=dict(width=1.5),
        ))
    ic_fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)
    ic_fig.update_layout(
        template="plotly_dark", height=350,
        margin=dict(l=40, r=20, t=20, b=40),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        yaxis_title="IC",
    )

    # ─── Rank IC 时序图 ────────────────────────────────────────────────────
    rank_ic_fig = go.Figure()
    for f in top5:
        rank_ic_fig.add_trace(go.Scatter(
            x=dates, y=rank_ic_series[f],
            mode="lines", name=f,
            line=dict(width=1.5),
        ))
    rank_ic_fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)
    rank_ic_fig.update_layout(
        template="plotly_dark", height=350,
        margin=dict(l=40, r=20, t=20, b=40),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        yaxis_title="Rank IC",
    )

    # ─── Top 10 因子柱状图 ─────────────────────────────────────────────────
    top10_df = summary_df.nlargest(10, "IC均值")
    bar_fig = go.Figure()
    bar_fig.add_trace(go.Bar(
        x=top10_df["因子"],
        y=top10_df["IC均值"],
        marker_color="#00d4aa",
        text=top10_df["IC均值"].round(4),
        textposition="outside",
        textfont=dict(size=10),
    ))
    bar_fig.update_layout(
        template="plotly_dark", height=350,
        margin=dict(l=40, r=20, t=20, b=60),
        yaxis_title="IC 均值",
        showlegend=False,
    )

    # ─── 汇总表 ───────────────────────────────────────────────────────────
    table = dash_table.DataTable(
        columns=[{"name": c, "id": c} for c in summary_df.columns],
        data=summary_df.round(4).to_dict("records"),
        style_table={"overflowX": "auto"},
        style_header={
            "backgroundColor": "#1a1a2e",
            "color": "#e0e0e0",
            "fontWeight": "bold",
        },
        style_cell={
            "backgroundColor": "#16213e",
            "color": "#e0e0e0",
            "border": "1px solid #0f3460",
            "textAlign": "center",
            "padding": "6px",
            "fontSize": "13px",
        },
        sort_action="native",
        page_size=15,
    )

    return ic_fig, rank_ic_fig, bar_fig, table
