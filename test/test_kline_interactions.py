#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""K 线页交互配置测试。"""

import sys
from pathlib import Path

import plotly.graph_objects as go


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from web_app.app import app  # noqa: F401
from web_app.pages.kline import _apply_kline_interactions, _get_kline_graph_config


def test_kline_interactions_enable_crosshair_and_scroll_zoom():
    fig = _apply_kline_interactions(go.Figure())

    assert fig.layout.hovermode == "x unified"
    assert fig.layout.hoversubplots == "axis"
    assert fig.layout.dragmode == "pan"
    assert fig.layout.uirevision == "kline-chart"
    assert fig.layout.xaxis.showspikes is True
    assert fig.layout.xaxis.spikemode == "across"
    assert fig.layout.xaxis.spikesnap == "cursor"
    assert fig.layout.xaxis.spikecolor == "rgba(255, 209, 102, 0.85)"
    assert fig.layout.xaxis.spikethickness == 2
    assert fig.layout.xaxis.fixedrange is False
    assert fig.layout.yaxis.fixedrange is True


def test_kline_graph_config_enables_touch_zoom():
    config = _get_kline_graph_config()

    assert config["scrollZoom"] is True
    assert config["doubleClick"] is False
    assert config["displayModeBar"] is True
    assert config["displaylogo"] is False
    assert config["responsive"] is True
    assert config["showTips"] is False
