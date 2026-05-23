"""Page 1: 选股结果仪表盘."""

import dash
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from dash import Input, Output, callback, dash_table, dcc, html

from web_app.components.common import create_no_data_message
from web_app.data_service import load_selected_data, load_ranking_data, parse_shap_values, get_stock_names_batch

dash.register_page(__name__, path="/", name="选股结果", title="选股结果 - 港股量化分析")

# ─── 布局 ──────────────────────────────────────────────────────────────────────


def layout(**kwargs):  # type: ignore
    """页面布局."""
    selected_df = load_selected_data()
    ranking_df = load_ranking_data()

    if selected_df is None and ranking_df is None:
        return create_no_data_message("暂无选股数据, 请先运行选股模型生成 output/ 目录下的 CSV 文件")

    # 使用 selected 数据作为主表, 如果没有则用 ranking
    display_df = selected_df if selected_df is not None else ranking_df

    # 添加股票名称
    if display_df is not None and "stock_code" in display_df.columns:
        codes = [str(c).zfill(5) for c in display_df["stock_code"].tolist()]
        names_map = get_stock_names_batch(codes)
        display_df = display_df.copy()
        display_df["stock_name"] = display_df["stock_code"].apply(
            lambda c: names_map.get(str(c).zfill(5), "")
        )

    # 提取股票代码列表用于下拉框
    stock_codes = display_df["stock_code"].astype(str).tolist() if display_df is not None else []

    # 准备表格显示列
    table_columns = ["stock_code", "stock_name", "ranking_score", "signal_tier", "setup_type",
                     "risk_adjusted_score", "win_rate", "backtest_return"]
    available_cols = [c for c in table_columns if c in (display_df.columns if display_df is not None else [])]

    col_name_map = {
        "stock_code": "股票代码",
        "stock_name": "股票名称",
        "ranking_score": "综合评分",
        "signal_tier": "信号等级",
        "setup_type": "形态类型",
        "risk_adjusted_score": "风险调整得分",
        "win_rate": "胜率(%)",
        "backtest_return": "回测收益(%)",
    }

    return html.Div([
        html.H4("LightGBM 选股结果", className="mb-3"),

        dbc.Row([
            # 左侧: 表格
            dbc.Col([
                html.H6("入选股票列表", className="text-muted mb-2"),
                dash_table.DataTable(
                    id="stock-selection-table",
                    columns=[{"name": col_name_map.get(c, c), "id": c} for c in available_cols],
                    data=display_df[available_cols].round(2).to_dict("records") if display_df is not None else [],
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
                            "if": {"filter_query": "{signal_tier} = strong"},
                            "backgroundColor": "#1b4332",
                            "color": "#95d5b2",
                        }
                    ],
                    row_selectable="single",
                    page_size=15,
                    sort_action="native",
                ),
            ], md=7),

            # 右侧: 评分柱状图
            dbc.Col([
                html.H6("Top 20 综合评分", className="text-muted mb-2"),
                dcc.Graph(id="score-bar-chart", figure=_build_score_chart(display_df)),
            ], md=5),
        ]),

        html.Hr(className="my-4"),

        # SHAP 分析区域
        dbc.Row([
            dbc.Col([
                html.H6("SHAP 因子贡献分析", className="text-muted mb-2"),
                dbc.Select(
                    id="shap-stock-selector",
                    options=[
                        {"label": f"{code} {names_map.get(str(code).zfill(5), '')}", "value": code}
                        for code in stock_codes
                    ],
                    value=stock_codes[0] if stock_codes else None,
                    className="mb-3",
                ),
                dcc.Graph(id="shap-waterfall-chart"),
            ], md=12),
        ]),
    ])


def _build_score_chart(df) -> go.Figure:
    """构建评分柱状图."""
    fig = go.Figure()
    if df is None or df.empty:
        fig.add_annotation(text="暂无数据", xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False)
        fig.update_layout(template="plotly_dark", height=400)
        return fig

    top20 = df.nlargest(20, "ranking_score")
    colors = ["#00d4aa" if tier == "strong" else "#ffd166" for tier in top20.get("signal_tier", [""] * len(top20))]

    # 构建 x 轴标签: 代码+名称
    x_labels = []
    for _, row in top20.iterrows():
        code = str(row["stock_code"])
        name = row.get("stock_name", "")
        x_labels.append(f"{code}\n{name}" if name else code)

    fig.add_trace(go.Bar(
        x=x_labels,
        y=top20["ranking_score"],
        marker_color=colors,
        text=top20["ranking_score"].round(1),
        textposition="outside",
        textfont=dict(size=10),
    ))

    fig.update_layout(
        template="plotly_dark",
        height=400,
        margin=dict(l=40, r=20, t=20, b=60),
        xaxis_title="股票代码",
        yaxis_title="综合评分",
        showlegend=False,
    )
    return fig


# ─── 回调 ──────────────────────────────────────────────────────────────────────


@callback(
    Output("shap-waterfall-chart", "figure"),
    Input("shap-stock-selector", "value"),
)
def update_shap_chart(stock_code):  # type: ignore
    """更新 SHAP 瀑布图."""
    fig = go.Figure()

    if not stock_code:
        fig.add_annotation(text="请选择股票", xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False)
        fig.update_layout(template="plotly_dark", height=350)
        return fig

    # 从 selected 或 ranking 数据中获取 SHAP 值
    df = load_selected_data()
    if df is None:
        df = load_ranking_data()
    if df is None:
        fig.add_annotation(text="暂无数据", xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False)
        fig.update_layout(template="plotly_dark", height=350)
        return fig

    row = df[df["stock_code"].astype(str) == str(stock_code)]
    if row.empty or "factor_explanation" not in row.columns:
        fig.add_annotation(text="暂无 SHAP 数据", xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False)
        fig.update_layout(template="plotly_dark", height=350)
        return fig

    shap_data = parse_shap_values(row.iloc[0]["factor_explanation"])
    if not shap_data:
        fig.add_annotation(text="暂无 SHAP 数据", xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False)
        fig.update_layout(template="plotly_dark", height=350)
        return fig

    # 合并正负 SHAP 值
    features = []
    values = []
    for item in shap_data.get("positive", []):
        features.append(item["feature"])
        values.append(item["shap_value"])
    for item in shap_data.get("negative", []):
        features.append(item["feature"])
        values.append(item["shap_value"])

    # 按绝对值排序
    sorted_pairs = sorted(zip(features, values), key=lambda x: abs(x[1]), reverse=True)
    features = [p[0] for p in sorted_pairs]
    values = [p[1] for p in sorted_pairs]

    colors = ["#00d4aa" if v > 0 else "#ef476f" for v in values]

    fig.add_trace(go.Bar(
        y=features,
        x=values,
        orientation="h",
        marker_color=colors,
        text=[f"{v:+.4f}" for v in values],
        textposition="outside",
        textfont=dict(size=10),
    ))

    fig.update_layout(
        template="plotly_dark",
        height=max(350, len(features) * 35),
        margin=dict(l=80, r=60, t=20, b=40),
        xaxis_title="SHAP 值",
        yaxis=dict(autorange="reversed"),
        showlegend=False,
    )
    return fig
