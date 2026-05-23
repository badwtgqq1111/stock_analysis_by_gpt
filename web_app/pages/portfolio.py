"""Page 4: 组合回测结果."""

import dash
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from dash import callback, dash_table, dcc, html
from plotly.subplots import make_subplots

from web_app.components.common import create_metric_card, create_no_data_message
from web_app.data_service import load_portfolio_backtest

dash.register_page(__name__, path="/portfolio", name="组合回测", title="组合回测 - 港股量化分析")

# ─── 布局 ──────────────────────────────────────────────────────────────────────


def layout(**kwargs):  # type: ignore
    """页面布局."""
    data = load_portfolio_backtest()

    if data is None:
        return create_no_data_message("暂无回测数据, 请先运行组合回测")

    metrics = data["metrics"]
    holdings = data["holdings"]
    dates = data["dates"]
    equity = data["equity"]
    drawdown = data["drawdown"]

    # ─── 指标卡片 ─────────────────────────────────────────────────────────
    metric_cards = dbc.Row([
        dbc.Col(create_metric_card("总收益率", metrics["总收益率"], "success"), md=2),
        dbc.Col(create_metric_card("年化收益率", metrics["年化收益率"], "info"), md=2),
        dbc.Col(create_metric_card("最大回撤", metrics["最大回撤"], "danger"), md=2),
        dbc.Col(create_metric_card("夏普比率", metrics["夏普比率"], "primary"), md=2),
        dbc.Col(create_metric_card("胜率", metrics["胜率"], "warning"), md=2),
    ], className="mb-4")

    # ─── 净值曲线 + 回撤图 ────────────────────────────────────────────────
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        row_heights=[0.65, 0.35],
        subplot_titles=["组合净值曲线", "回撤"],
    )

    fig.add_trace(go.Scatter(
        x=dates, y=equity,
        mode="lines",
        name="组合净值",
        line=dict(color="#00d4aa", width=2),
        fill="tozeroy",
        fillcolor="rgba(0, 212, 170, 0.1)",
    ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=dates, y=drawdown * 100,
        mode="lines",
        name="回撤(%)",
        line=dict(color="#ef476f", width=1.5),
        fill="tozeroy",
        fillcolor="rgba(239, 71, 111, 0.15)",
    ), row=2, col=1)

    fig.update_layout(
        template="plotly_dark",
        height=500,
        margin=dict(l=50, r=30, t=40, b=40),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    fig.update_yaxes(row=1, col=1, title_text="净值 (HKD)")
    fig.update_yaxes(row=2, col=1, title_text="回撤 (%)")

    # ─── 持仓表 ───────────────────────────────────────────────────────────
    holdings_table = dash_table.DataTable(
        columns=[{"name": c, "id": c} for c in holdings.columns],
        data=holdings.to_dict("records"),
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
            "padding": "8px",
        },
        style_data_conditional=[
            {
                "if": {"filter_query": '{持仓收益} contains "+"'},
                "color": "#00d4aa",
            },
            {
                "if": {"filter_query": '{持仓收益} contains "-"'},
                "color": "#ef476f",
            },
        ],
    )

    return html.Div([
        html.H4("组合回测结果", className="mb-3"),
        html.P(
            "以下为 LightGBM Top10 等权组合的模拟回测结果 (Mock 数据, 连接真实回测引擎后将显示实际结果)",
            className="text-muted small",
        ),

        metric_cards,

        dbc.Row([
            dbc.Col([
                dcc.Graph(figure=fig),
            ], md=12),
        ]),

        html.Hr(className="my-3"),

        dbc.Row([
            dbc.Col([
                html.H6("当前持仓", className="text-muted mb-2"),
                holdings_table,
            ], md=8),
        ]),
    ])
