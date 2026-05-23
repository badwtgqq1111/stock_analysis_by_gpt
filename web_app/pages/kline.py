"""Page 3: K线图表 (含 LightGBM 信号、悬停量价信息、筹码分布)."""

import dash
import dash_bootstrap_components as dbc
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, State, callback, dcc, html
from plotly.subplots import make_subplots

from web_app.data_service import (
    get_all_stock_codes, load_stock_ohlcv, load_selected_data,
    load_stock_signals, get_stock_name, get_stock_names_batch,
)

dash.register_page(__name__, path="/kline", name="K线图表", title="K线图表 - 港股量化分析")

# ─── 布局 ──────────────────────────────────────────────────────────────────────


def layout(**kwargs):  # type: ignore
    """页面布局."""
    stock_codes = get_all_stock_codes()
    if not stock_codes:
        stock_codes = ["00700", "09988", "01810", "02318", "00388"]

    # 获取选中的股票放在最前面
    selected_df = load_selected_data()
    selected_codes = []
    if selected_df is not None and "stock_code" in selected_df.columns:
        selected_codes = [str(c).zfill(5) for c in selected_df["stock_code"].tolist()]

    # 批量获取股票名称
    all_codes_for_names = selected_codes + stock_codes[:200]
    names_map = get_stock_names_batch(all_codes_for_names)

    all_options = []
    for code in selected_codes:
        name = names_map.get(code, "")
        label = f"★ {code} {name} (持仓)" if name else f"★ {code} (持仓)"
        all_options.append({"label": label, "value": code})
    for code in stock_codes:
        if code not in selected_codes:
            name = names_map.get(code, "")
            label = f"{code} {name}" if name else code
            all_options.append({"label": label, "value": code})

    default_value = selected_codes[0] if selected_codes else (stock_codes[0] if stock_codes else None)

    return html.Div([
        html.H4("K线图表", className="mb-3"),

        # 控制面板
        dbc.Row([
            dbc.Col([
                html.Label("股票代码（★为当前持仓）", className="text-muted"),
                dcc.Dropdown(
                    id="kline-stock-selector",
                    options=all_options,
                    value=default_value,
                    placeholder="输入股票代码搜索...",
                    searchable=True,
                    className="dash-bootstrap",
                    style={"color": "#000"},
                ),
            ], md=3),
            dbc.Col([
                html.Label("时间范围", className="text-muted"),
                dbc.Select(
                    id="kline-days-selector",
                    options=[
                        {"label": "90天", "value": "90"},
                        {"label": "180天", "value": "180"},
                        {"label": "365天", "value": "365"},
                        {"label": "730天 (2年)", "value": "730"},
                        {"label": "全部数据", "value": "9999"},
                    ],
                    value="365",
                ),
            ], md=2),
            dbc.Col([
                html.Label("手动输入代码", className="text-muted"),
                dbc.InputGroup([
                    dbc.Input(id="kline-manual-input", placeholder="如: 00700", type="text"),
                    dbc.Button("查询", id="kline-manual-btn", color="primary", size="sm"),
                ]),
            ], md=3),
            dbc.Col([
                html.Label("叠加显示", className="text-muted"),
                dbc.Checklist(
                    id="kline-overlay-toggle",
                    options=[
                        {"label": " LightGBM 信号", "value": "lgbm_signals"},
                        {"label": " 筹码分布", "value": "chip_dist"},
                    ],
                    value=["lgbm_signals"],
                    switch=True,
                ),
            ], md=3),
        ], className="mb-3"),

        # 图表区域
        dbc.Row([
            dbc.Col([
                dcc.Loading(
                    dcc.Graph(
                        id="kline-chart",
                        style={"height": "720px"},
                        config=_get_kline_graph_config(),
                    ),
                    type="circle",
                ),
            ], md=12),
        ]),

        # 信号统计
        html.Div(id="kline-signal-info", className="mt-2"),
    ])


# ─── 回调 ──────────────────────────────────────────────────────────────────────


@callback(
    Output("kline-stock-selector", "value"),
    Input("kline-manual-btn", "n_clicks"),
    State("kline-manual-input", "value"),
    prevent_initial_call=True,
)
def manual_stock_input(n_clicks, manual_code):  # type: ignore
    """手动输入股票代码."""
    if manual_code and manual_code.strip():
        return manual_code.strip().zfill(5)
    return dash.no_update


def _enrich_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """添加衍生指标: 涨跌幅、成交额、均线等."""
    df = df.copy()
    df["change_pct"] = df["Close"].pct_change() * 100
    df["amount"] = df["Close"] * df["Volume"]  # 近似成交额
    df["MA5"] = df["Close"].rolling(5).mean()
    df["MA10"] = df["Close"].rolling(10).mean()
    df["MA20"] = df["Close"].rolling(20).mean()
    df["MA60"] = df["Close"].rolling(60).mean()
    df["VOL_MA5"] = df["Volume"].rolling(5).mean()
    df["VOL_MA20"] = df["Volume"].rolling(20).mean()
    df["amplitude"] = (df["High"] - df["Low"]) / df["Close"].shift(1) * 100  # 振幅
    return df


def _compute_lgbm_signals(df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
    """加载 LightGBM 模型的真实买卖信号.

    优先从导出的信号文件读取（与 select_stocks 逻辑一致），
    如果没有信号文件则使用 MA 趋势突破作为降级方案。
    """
    df = df.copy()
    df["buy_signal"] = False
    df["sell_signal"] = False

    # 尝试加载真实 LightGBM 信号
    signals_df = load_stock_signals(stock_code)
    if signals_df is not None and not signals_df.empty and "date" in signals_df.columns:
        signals_df["date"] = pd.to_datetime(signals_df["date"])

        # actionable=True 的信号为买入点
        if "actionable" in signals_df.columns:
            buy_dates = set(signals_df[signals_df["actionable"] == True]["date"].dt.date)  # noqa: E712
            for idx in df.index:
                if idx.date() in buy_dates:
                    df.loc[idx, "buy_signal"] = True

        # 卖出信号: actionable 从 True 变为 False (信号消失)
        if "actionable" in signals_df.columns:
            signals_sorted = signals_df.sort_values("date")
            signals_sorted["prev_actionable"] = signals_sorted["actionable"].shift(1)
            sell_dates = set(
                signals_sorted[
                    (signals_sorted["prev_actionable"] == True) &  # noqa: E712
                    (signals_sorted["actionable"] == False)  # noqa: E712
                ]["date"].dt.date
            )
            for idx in df.index:
                if idx.date() in sell_dates:
                    df.loc[idx, "sell_signal"] = True

        return df

    # 降级方案: MA 趋势突破
    if len(df) < 60:
        return df

    ma20 = df["Close"].rolling(20).mean()
    ma60 = df["Close"].rolling(60).mean()
    vol_ma20 = df["Volume"].rolling(20).mean()

    price_above_ma20 = df["Close"] > ma20
    cross_up = price_above_ma20 & (~price_above_ma20).shift(1).fillna(False)
    cross_down = (~price_above_ma20) & price_above_ma20.shift(1).fillna(False)
    vol_expand = df["Volume"] > vol_ma20 * 1.3

    df["buy_signal"] = cross_up & (ma20 > ma60) & vol_expand
    df["sell_signal"] = cross_down & ((ma20 < ma60) | (df["Volume"] < vol_ma20 * 0.7))

    return df


def _compute_chip_distribution(df: pd.DataFrame, n_bins: int = 50) -> dict:
    """计算筹码分布 (成本分布).

    用最近 N 天的成交量在各价位的分布来近似。
    越近期的成交量权重越高（时间衰减）。
    """
    if df is None or len(df) < 20:
        return {"prices": [], "volumes": [], "current_price": 0}

    # 使用最近 120 天数据
    recent = df.tail(120).copy()
    current_price = float(recent["Close"].iloc[-1])

    # 价格区间
    price_min = recent["Low"].min() * 0.95
    price_max = recent["High"].max() * 1.05
    bins = np.linspace(price_min, price_max, n_bins + 1)
    bin_centers = (bins[:-1] + bins[1:]) / 2

    # 时间衰减权重 (最近的权重最大)
    n = len(recent)
    decay_weights = np.exp(-0.02 * np.arange(n)[::-1])  # 半衰期约 35 天

    # 将每天的成交量分配到价格区间
    chip_volume = np.zeros(n_bins)
    for i in range(n):
        row = recent.iloc[i]
        # 假设当天成交均匀分布在 Low ~ High 之间
        low, high, vol = row["Low"], row["High"], row["Volume"]
        if high <= low or vol <= 0:
            continue
        # 找到覆盖的 bin
        mask = (bin_centers >= low) & (bin_centers <= high)
        n_covered = mask.sum()
        if n_covered > 0:
            chip_volume[mask] += vol * decay_weights[i] / n_covered

    # 归一化
    total = chip_volume.sum()
    if total > 0:
        chip_volume = chip_volume / total * 100  # 百分比

    return {
        "prices": bin_centers.tolist(),
        "volumes": chip_volume.tolist(),
        "current_price": current_price,
    }


@callback(
    Output("kline-chart", "figure"),
    Output("kline-signal-info", "children"),
    Input("kline-stock-selector", "value"),
    Input("kline-days-selector", "value"),
    Input("kline-overlay-toggle", "value"),
)
def update_kline(stock_code, days, overlay_toggle):  # type: ignore
    """更新 K 线图."""
    show_signals = "lgbm_signals" in (overlay_toggle or [])
    show_chips = "chip_dist" in (overlay_toggle or [])

    # 决定布局: 有筹码分布时用 3 列
    if show_chips:
        fig = make_subplots(
            rows=2, cols=2,
            shared_xaxes=True,
            vertical_spacing=0.03,
            horizontal_spacing=0.02,
            row_heights=[0.75, 0.25],
            column_widths=[0.82, 0.18],
            specs=[[{"type": "xy"}, {"type": "xy"}],
                   [{"type": "xy"}, {"type": "xy"}]],
        )
    else:
        fig = make_subplots(
            rows=2, cols=1,
            shared_xaxes=True,
            vertical_spacing=0.03,
            row_heights=[0.75, 0.25],
        )
    fig.update_layout(hovermode="x unified")

    if not stock_code:
        fig.add_annotation(text="请选择股票代码", xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False)
        fig.update_layout(template="plotly_dark", height=720)
        fig = _apply_kline_interactions(fig)
        return fig, ""

    days_int = int(days) if days else 365
    df = load_stock_ohlcv(stock_code, days=days_int)

    if df is None or df.empty:
        fig.add_annotation(
            text=f"暂无 {stock_code} 的行情数据",
            xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
            font=dict(size=16),
        )
        fig.update_layout(template="plotly_dark", height=720)
        fig = _apply_kline_interactions(fig)
        return fig, ""

    # 丰富数据
    df = _enrich_dataframe(df)
    if show_signals:
        df = _compute_lgbm_signals(df, stock_code)

    # ─── 构建悬停文本 ─────────────────────────────────────────────────────
    hover_texts = []
    for i in range(len(df)):
        row = df.iloc[i]
        date_str = df.index[i].strftime("%Y-%m-%d")
        chg = row.get("change_pct", 0)
        chg_str = f"+{chg:.2f}%" if chg >= 0 else f"{chg:.2f}%"
        amt = row.get("amount", 0)
        if amt >= 1e8:
            amt_str = f"{amt/1e8:.2f}亿"
        elif amt >= 1e4:
            amt_str = f"{amt/1e4:.0f}万"
        else:
            amt_str = f"{amt:.0f}"
        vol = row["Volume"]
        if vol >= 1e8:
            vol_str = f"{vol/1e8:.2f}亿股"
        elif vol >= 1e4:
            vol_str = f"{vol/1e4:.0f}万股"
        else:
            vol_str = f"{vol:.0f}股"
        amp = row.get("amplitude", 0)
        hover_texts.append(
            f"<b>{date_str}</b><br>"
            f"开: {row['Open']:.3f}  高: {row['High']:.3f}<br>"
            f"低: {row['Low']:.3f}  收: {row['Close']:.3f}<br>"
            f"涨跌: {chg_str}  振幅: {amp:.2f}%<br>"
            f"成交量: {vol_str}<br>"
            f"成交额: {amt_str}"
        )

    # ─── 蜡烛图 ───────────────────────────────────────────────────────────
    fig.add_trace(go.Candlestick(
        x=df.index,
        open=df["Open"],
        high=df["High"],
        low=df["Low"],
        close=df["Close"],
        increasing_line_color="#00d4aa",
        decreasing_line_color="#ef476f",
        name="K线",
        text=hover_texts,
        hoverinfo="text",
    ), row=1, col=1)

    # ─── 均线 ─────────────────────────────────────────────────────────────
    ma_configs = [
        ("MA5", "#ffd166", 1),
        ("MA10", "#ff9f1c", 1),
        ("MA20", "#06d6a0", 1.2),
        ("MA60", "#118ab2", 1.5),
    ]
    for ma_name, color, width in ma_configs:
        if ma_name in df.columns:
            ma_data = df[ma_name].dropna()
            if not ma_data.empty:
                fig.add_trace(go.Scatter(
                    x=ma_data.index, y=ma_data,
                    mode="lines", name=ma_name,
                    line=dict(color=color, width=width),
                    hovertemplate=f"{ma_name}: %{{y:.3f}}<extra></extra>",
                ), row=1, col=1)

    # ─── 成交量 ───────────────────────────────────────────────────────────
    vol_colors = [
        "#00d4aa" if df["Close"].iloc[i] >= df["Open"].iloc[i] else "#ef476f"
        for i in range(len(df))
    ]
    fig.add_trace(go.Bar(
        x=df.index,
        y=df["Volume"],
        marker_color=vol_colors,
        name="成交量",
        showlegend=False,
        hovertemplate="成交量: %{y:,.0f}<extra></extra>",
    ), row=2, col=1)

    # 成交量均线
    if "VOL_MA5" in df.columns:
        fig.add_trace(go.Scatter(
            x=df.index, y=df["VOL_MA5"],
            mode="lines", name="量MA5",
            line=dict(color="#ffd166", width=1, dash="dot"),
            showlegend=False,
        ), row=2, col=1)
    if "VOL_MA20" in df.columns:
        fig.add_trace(go.Scatter(
            x=df.index, y=df["VOL_MA20"],
            mode="lines", name="量MA20",
            line=dict(color="#06d6a0", width=1, dash="dot"),
            showlegend=False,
        ), row=2, col=1)

    # ─── LightGBM 买卖信号 ────────────────────────────────────────────────
    signal_info = ""
    if show_signals and "buy_signal" in df.columns:
        buy_points = df[df["buy_signal"] == True]  # noqa: E712
        sell_points = df[df["sell_signal"] == True]  # noqa: E712

        if not buy_points.empty:
            fig.add_trace(go.Scatter(
                x=buy_points.index,
                y=buy_points["Low"] * 0.97,
                mode="markers",
                marker=dict(symbol="triangle-up", size=14, color="#00d4aa",
                            line=dict(width=1, color="white")),
                name=f"买入 ({len(buy_points)})",
                hovertemplate="<b>买入信号</b><br>%{x}<br>价格: %{text:.3f}<extra></extra>",
                text=buy_points["Close"],
            ), row=1, col=1)

        if not sell_points.empty:
            fig.add_trace(go.Scatter(
                x=sell_points.index,
                y=sell_points["High"] * 1.03,
                mode="markers",
                marker=dict(symbol="triangle-down", size=14, color="#ef476f",
                            line=dict(width=1, color="white")),
                name=f"卖出 ({len(sell_points)})",
                hovertemplate="<b>卖出信号</b><br>%{x}<br>价格: %{text:.3f}<extra></extra>",
                text=sell_points["Close"],
            ), row=1, col=1)

        # 配对回测
        signal_info = _build_signal_stats(df, buy_points, sell_points)

    # ─── 筹码分布 ─────────────────────────────────────────────────────────
    if show_chips:
        chips = _compute_chip_distribution(df)
        if chips["prices"]:
            # 区分获利筹码和套牢筹码
            current_price = chips["current_price"]
            prices = np.array(chips["prices"])
            volumes = np.array(chips["volumes"])

            profit_mask = prices <= current_price
            loss_mask = prices > current_price

            # 获利筹码 (绿色)
            if profit_mask.any():
                fig.add_trace(go.Bar(
                    x=volumes[profit_mask],
                    y=prices[profit_mask],
                    orientation="h",
                    marker_color="rgba(0, 212, 170, 0.6)",
                    name="获利筹码",
                    showlegend=False,
                    hovertemplate="价格: %{y:.3f}<br>占比: %{x:.1f}%<extra></extra>",
                ), row=1, col=2)

            # 套牢筹码 (红色)
            if loss_mask.any():
                fig.add_trace(go.Bar(
                    x=volumes[loss_mask],
                    y=prices[loss_mask],
                    orientation="h",
                    marker_color="rgba(239, 71, 111, 0.6)",
                    name="套牢筹码",
                    showlegend=False,
                    hovertemplate="价格: %{y:.3f}<br>占比: %{x:.1f}%<extra></extra>",
                ), row=1, col=2)

            # 当前价格线
            fig.add_hline(
                y=current_price, row=1, col=2,
                line_dash="dash", line_color="#ffd166", line_width=1,
                annotation_text=f"现价 {current_price:.3f}",
                annotation_font_color="#ffd166",
                annotation_font_size=10,
            )

            # 同步 Y 轴范围
            fig.update_yaxes(
                range=[df["Low"].min() * 0.95, df["High"].max() * 1.05],
                row=1, col=2,
                showticklabels=False,
            )
            fig.update_xaxes(row=1, col=2, title_text="筹码%", showgrid=False)
            fig.update_xaxes(row=2, col=2, visible=False)
            fig.update_yaxes(row=2, col=2, visible=False)

    # ─── 布局 ─────────────────────────────────────────────────────────────
    latest_close = df["Close"].iloc[-1]
    latest_chg = df["change_pct"].iloc[-1] if "change_pct" in df.columns else 0
    chg_color = "#00d4aa" if latest_chg >= 0 else "#ef476f"
    chg_str = f"+{latest_chg:.2f}%" if latest_chg >= 0 else f"{latest_chg:.2f}%"
    stock_name = get_stock_name(stock_code)
    title_prefix = f"<b>{stock_code} {stock_name}</b>" if stock_name else f"<b>{stock_code}</b>"

    fig.update_layout(
        template="plotly_dark",
        height=720,
        margin=dict(l=50, r=30, t=50, b=40),
        title=dict(
            text=(
                f"{title_prefix}  "
                f"收盘 {latest_close:.3f}  "
                f"<span style='color:{chg_color}'>{chg_str}</span>  "
                f"<span style='color:gray;font-size:11px'>({len(df)}根K线)</span>"
            ),
            x=0.01, xanchor="left",
            font=dict(size=14),
        ),
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
    )
    fig = _apply_kline_interactions(fig)
    # K 线主图: x 轴标题 + y 轴显示横线 spike 组成十字光标
    fig.update_xaxes(row=1, col=1, title_text="")
    fig.update_xaxes(row=2, col=1, title_text="日期")
    fig.update_yaxes(row=1, col=1, title_text="价格",
                     showspikes=True, spikemode="across",
                     spikecolor="rgba(255, 209, 102, 0.55)", spikethickness=1.5)
    fig.update_yaxes(row=2, col=1, title_text="成交量")
    if show_chips:
        fig.update_xaxes(
            row=1, col=2,
            showspikes=False,
            fixedrange=True,
        )
        fig.update_yaxes(
            row=1, col=2,
            fixedrange=True,
        )

    return fig, signal_info


def _build_signal_stats(df, buy_points, sell_points):
    """构建信号统计信息."""
    n_buy = len(buy_points)
    n_sell = len(sell_points)

    # 配对回测
    trades = []
    buy_dates = buy_points.index.tolist()
    sell_dates = sell_points.index.tolist()
    si = 0
    for bd in buy_dates:
        while si < len(sell_dates) and sell_dates[si] <= bd:
            si += 1
        if si < len(sell_dates):
            buy_price = df.loc[bd, "Close"]
            sell_price = df.loc[sell_dates[si], "Close"]
            ret = (sell_price - buy_price) / buy_price * 100
            trades.append(ret)
            si += 1

    if trades:
        avg_ret = np.mean(trades)
        win_rate = np.mean([t > 0 for t in trades]) * 100
        max_win = max(trades)
        max_loss = min(trades)
        return dbc.Alert([
            html.Span("LightGBM 信号回测: ", className="fw-bold"),
            html.Span(
                f"买入 {n_buy} 次 | 卖出 {n_sell} 次 | "
                f"配对 {len(trades)} 笔 | "
                f"平均收益 {avg_ret:.2f}% | "
                f"胜率 {win_rate:.0f}% | "
                f"最大盈利 {max_win:.1f}% | "
                f"最大亏损 {max_loss:.1f}%"
            ),
        ], color="info", className="mt-2 py-2 small")
    else:
        return dbc.Alert(
            f"LightGBM 信号: 买入 {n_buy} 次, 卖出 {n_sell} 次 (暂无完整配对交易)",
            color="secondary", className="mt-2 py-2 small",
        )


def _apply_kline_interactions(fig):
    """统一配置 K 线图交互：十字光标、滚轮缩放、拖拽平移。

    十字光标:
    - 竖线 (xaxis spike) 贯穿 K 线主图和成交量附图
    - 横线 (yaxis spike) 仅在 K 线主图显示
    """
    fig.update_layout(
        hovermode="x unified",
        hoversubplots="axis",
        dragmode="pan",
        uirevision="kline-chart",
    )
    # 所有 x 轴: 显示竖线 spike，across 模式让竖线贯穿所有子图
    fig.update_xaxes(
        showspikes=True,
        spikemode="across",
        spikesnap="cursor",
        spikecolor="rgba(255, 209, 102, 0.85)",
        spikethickness=2,
        fixedrange=False,
        rangeslider=dict(visible=False),
    )
    # 所有 y 轴: 默认关 spike，固定范围
    fig.update_yaxes(
        showspikes=False,
        fixedrange=True,
    )
    return fig


def _get_kline_graph_config():
    return {
        "scrollZoom": True,
        "doubleClick": False,
        "displayModeBar": True,
        "modeBarButtonsToAdd": ["drawrect", "eraseshape"],
        "displaylogo": False,
        "responsive": True,
        "showTips": False,
    }
