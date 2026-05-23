"""主 Dash 应用: 多页面路由配置."""

import dash
import dash_bootstrap_components as dbc
from dash import html

from web_app.components.common import create_navbar

# 创建 Dash 应用, 使用 CYBORG 暗色主题
app = dash.Dash(
    __name__,
    use_pages=True,
    pages_folder="pages",
    external_stylesheets=[dbc.themes.CYBORG, dbc.icons.BOOTSTRAP],
    suppress_callback_exceptions=True,
    title="港股量化分析系统",
    meta_tags=[
        {"name": "viewport", "content": "width=device-width, initial-scale=1"},
    ],
)

# 应用布局
app.layout = html.Div(
    [
        create_navbar(),
        dbc.Container(
            dash.page_container,
            fluid=True,
            className="px-4 pb-4",
        ),
    ]
)

server = app.server
