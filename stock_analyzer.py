#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Backward-compatible CLI entry point. Implementation in cli/ package."""

from cli import run_cli
from cli.formatters import _format_factor_reason_lines

__all__ = ["run_cli", "_format_factor_reason_lines"]

if __name__ == "__main__":
    run_cli()
