#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""数据目录布局。"""

from dataclasses import dataclass
from pathlib import Path


LAYER_NAMES = ("raw", "clean", "feature", "signal", "trade", "meta")


@dataclass
class DataLayout:
    """统一管理 raw/clean/feature/signal/trade 目录。"""

    base_dir: str = "./assets/data"

    def __post_init__(self):
        self.base_path = Path(self.base_dir).resolve()
        self.ensure_directories()

    def ensure_directories(self):
        """创建所有数据层目录。"""
        self.base_path.mkdir(parents=True, exist_ok=True)
        for layer in LAYER_NAMES:
            self.layer_path(layer).mkdir(parents=True, exist_ok=True)

    def layer_path(self, layer):
        """返回数据层目录。"""
        if layer not in LAYER_NAMES:
            raise ValueError(f"未知数据层: {layer}")
        return self.base_path / layer

    def dataset_path(self, dataset_name, layer="clean"):
        """返回某个数据集目录。"""
        return self.layer_path(layer) / dataset_name

    def dataset_glob(self, dataset_name, layer="clean"):
        """返回某个 parquet 数据集的 glob 路径。"""
        return str(self.dataset_path(dataset_name, layer=layer) / "**" / "*.parquet")

    def duckdb_path(self):
        """返回元数据 duckdb 文件路径。"""
        return self.layer_path("meta") / "market_data.duckdb"
