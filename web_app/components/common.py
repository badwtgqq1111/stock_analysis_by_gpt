"""共享 UI 组件: 导航栏、侧边栏等."""

import dash_bootstrap_components as dbc
from dash import html


def create_navbar():
    """创建顶部导航栏."""
    return dbc.Navbar(
        dbc.Container(
            [
                dbc.NavbarBrand("港股量化分析系统", href="/", className="ms-2 fw-bold"),
                dbc.Nav(
                    [
                        dbc.NavItem(dbc.NavLink("选股结果", href="/")),
                        dbc.NavItem(dbc.NavLink("因子IC分析", href="/factor-ic")),
                        dbc.NavItem(dbc.NavLink("K线图表", href="/kline")),
                        dbc.NavItem(dbc.NavLink("组合回测", href="/portfolio")),
                    ],
                    navbar=True,
                ),
            ],
            fluid=True,
        ),
        color="dark",
        dark=True,
        sticky="top",
        className="mb-3",
    )


def create_no_data_message(message: str = "暂无数据"):
    """创建无数据提示组件."""
    return dbc.Alert(
        [
            html.I(className="bi bi-exclamation-triangle me-2"),
            message,
        ],
        color="warning",
        className="text-center mt-4",
    )


def create_metric_card(title: str, value: str, color: str = "primary"):
    """创建指标卡片."""
    return dbc.Card(
        dbc.CardBody(
            [
                html.H6(title, className="card-subtitle mb-2 text-muted"),
                html.H4(value, className=f"card-title text-{color}"),
            ]
        ),
        className="mb-3 shadow-sm",
    )
