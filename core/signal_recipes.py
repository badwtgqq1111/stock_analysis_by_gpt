"""Signal recipe report mixin for StockAnalyzer."""

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd


class SignalRecipesMixin:
    """Methods for building signal recipe performance reports."""

    def build_signal_recipe_report(
        self,
        stock_codes=None,
        days=365,
        signal_recipes=None,
        horizons=(20, 40, 60),
        max_workers=1,
        show_progress=False,
        min_history_days=60,
        signal_cooldown_days=20,
        signal_event_policy="first",
    ):
        """评估信号 recipe 触发后的 forward return 表现。"""
        from factor_engine.signals import SignalRecipeRunner

        if stock_codes is None:
            stock_codes = self.get_all_stocks()
        stock_codes = list(stock_codes or [])
        if not stock_codes:
            return None

        horizons = tuple(int(horizon) for horizon in horizons)
        warmup_days = max(days + int(max(horizons or (0,))) + int(min_history_days), days)
        recipe_names = tuple(signal_recipes or self.signal_recipes)
        rows = []

        def evaluate_stock(stock_code):
            data = self.load_stock_data(stock_code, warmup_days)
            if data is None or data.empty or len(data) < min_history_days:
                return []
            data = data.copy().sort_index()
            start_idx = max(len(data) - days, min_history_days)
            stock_rows = []
            for index in range(start_idx, len(data)):
                history = data.iloc[: index + 1]
                if len(history) < min_history_days:
                    continue
                for recipe_name in recipe_names:
                    snapshot = SignalRecipeRunner((recipe_name,)).evaluate(
                        history,
                        context={"stock_code": stock_code, "analysis_mode": "signal_report"},
                    )
                    setup_type = snapshot.get("setup_type", "neutral")
                    if setup_type in {"neutral", "sideways"}:
                        continue
                    event = {
                        "stock_code": stock_code,
                        "date": history.index[-1],
                        "recipe_name": recipe_name,
                        "setup_type": setup_type,
                        "setup_score": float(snapshot.get("setup_score", 0.0) or 0.0),
                        "sideways_penalty": float(snapshot.get("sideways_penalty", 0.0) or 0.0),
                        "close": float(history["Close"].iloc[-1]),
                    }
                    event.update(self._compute_signal_forward_metrics(data, index, horizons))
                    stock_rows.append(event)
            return stock_rows

        started_at = time.time()
        completed = 0
        if max_workers and int(max_workers) > 1 and len(stock_codes) > 1:
            with ThreadPoolExecutor(max_workers=min(int(max_workers), len(stock_codes))) as executor:
                future_map = {executor.submit(evaluate_stock, stock_code): stock_code for stock_code in stock_codes}
                for future in as_completed(future_map):
                    stock_code = future_map[future]
                    try:
                        rows.extend(future.result())
                    except Exception as exc:
                        print(f"\n[ERROR] 信号评估 {stock_code} 失败: {exc}")
                    completed += 1
                    if show_progress:
                        self._emit_progress_line(
                            prefix="[PROGRESS] signal_report",
                            completed=completed,
                            total=len(stock_codes),
                            success_count=len(rows),
                            started_at=started_at,
                        )
        else:
            for stock_code in stock_codes:
                rows.extend(evaluate_stock(stock_code))
                completed += 1
                if show_progress:
                    self._emit_progress_line(
                        prefix="[PROGRESS] signal_report",
                        completed=completed,
                        total=len(stock_codes),
                        success_count=len(rows),
                        started_at=started_at,
                    )
        if show_progress:
            print(file=sys.stderr)

        events_raw = pd.DataFrame(rows)
        events = self._merge_signal_recipe_events(
            events_raw,
            cooldown_days=signal_cooldown_days,
            event_policy=signal_event_policy,
        )
        summary = self._summarize_signal_recipe_events(events, horizons)
        return {
            "metadata": {
                "stock_count": len(stock_codes),
                "raw_event_count": len(events_raw),
                "event_count": len(events),
                "days": days,
                "signal_recipes": recipe_names,
                "horizons": horizons,
                "signal_cooldown_days": int(signal_cooldown_days),
                "signal_event_policy": str(signal_event_policy),
            },
            "summary": summary,
            "events": events,
            "events_raw": events_raw,
        }

    @staticmethod
    def _compute_signal_forward_metrics(data, event_index, horizons):
        close = pd.to_numeric(data["Close"], errors="coerce")
        low = pd.to_numeric(data["Low"], errors="coerce") if "Low" in data.columns else close
        entry_close = float(close.iloc[event_index])
        metrics = {}
        for horizon in horizons:
            horizon = int(horizon)
            end_index = min(event_index + horizon, len(data) - 1)
            if end_index <= event_index or not np.isfinite(entry_close) or entry_close == 0:
                metrics[f"forward_return_{horizon}"] = np.nan
                metrics[f"forward_max_drawdown_{horizon}"] = np.nan
                continue
            future_close = float(close.iloc[end_index])
            future_low = low.iloc[event_index + 1 : end_index + 1]
            future_min_low = float(future_low.min()) if not future_low.dropna().empty else np.nan
            metrics[f"forward_return_{horizon}"] = future_close / entry_close - 1.0 if np.isfinite(future_close) else np.nan
            metrics[f"forward_max_drawdown_{horizon}"] = future_min_low / entry_close - 1.0 if np.isfinite(future_min_low) else np.nan
        return metrics

    @staticmethod
    def _merge_signal_recipe_events(events, cooldown_days=20, event_policy="first"):
        if events is None or events.empty:
            return pd.DataFrame() if events is None else events.copy()

        cooldown_days = max(int(cooldown_days or 0), 0)
        event_policy = str(event_policy or "first").strip().lower()
        if event_policy not in {"first", "latest", "best_score"}:
            raise ValueError(f"unsupported signal_event_policy: {event_policy}")

        working = events.copy()
        working["date"] = pd.to_datetime(working["date"])
        working.sort_values(["stock_code", "recipe_name", "setup_type", "date"], inplace=True)

        merged_rows = []
        zone_counter = 0
        group_columns = ["stock_code", "recipe_name", "setup_type"]
        for (stock_code, recipe_name, setup_type), group in working.groupby(group_columns, dropna=False):
            current_zone_rows = []
            last_date = None

            def flush_zone():
                nonlocal zone_counter
                if not current_zone_rows:
                    return
                zone = pd.DataFrame(current_zone_rows)
                if event_policy == "latest":
                    selected = zone.sort_values("date").iloc[-1].copy()
                elif event_policy == "best_score":
                    selected = zone.sort_values(["setup_score", "date"], ascending=[False, True]).iloc[0].copy()
                else:
                    selected = zone.sort_values("date").iloc[0].copy()
                zone_counter += 1
                selected["signal_zone_id"] = f"{stock_code}:{recipe_name}:{setup_type}:{zone_counter}"
                selected["zone_start_date"] = zone["date"].min()
                selected["zone_end_date"] = zone["date"].max()
                selected["merged_signal_count"] = int(len(zone))
                selected["max_setup_score"] = float(zone["setup_score"].max()) if "setup_score" in zone else np.nan
                merged_rows.append(selected.to_dict())

            for _, row in group.iterrows():
                row_date = row["date"]
                if last_date is not None and cooldown_days > 0 and (row_date - last_date).days > cooldown_days:
                    flush_zone()
                    current_zone_rows = []
                elif last_date is not None and cooldown_days == 0:
                    flush_zone()
                    current_zone_rows = []
                current_zone_rows.append(row.to_dict())
                last_date = row_date
            flush_zone()

        merged = pd.DataFrame(merged_rows)
        if not merged.empty:
            merged.sort_values(["date", "stock_code", "recipe_name", "setup_type"], inplace=True)
            merged.reset_index(drop=True, inplace=True)
        return merged

    @staticmethod
    def _summarize_signal_recipe_events(events, horizons):
        if events is None or events.empty:
            columns = [
                "recipe_name",
                "setup_type",
                "event_count",
                "unique_stock_count",
                "top5_stock_event_share",
                "avg_setup_score",
            ]
            for horizon in horizons:
                columns.extend(
                    [
                        f"avg_forward_return_{horizon}",
                        f"median_forward_return_{horizon}",
                        f"p25_forward_return_{horizon}",
                        f"p75_forward_return_{horizon}",
                        f"win_rate_{horizon}",
                        f"avg_forward_max_drawdown_{horizon}",
                        f"p95_forward_drawdown_{horizon}",
                        f"return_drawdown_ratio_{horizon}",
                        f"avg_win_{horizon}",
                        f"avg_loss_{horizon}",
                    ]
                )
            return pd.DataFrame(columns=columns)

        rows = []
        grouped = events.groupby(["recipe_name", "setup_type"], dropna=False)
        for (recipe_name, setup_type), group in grouped:
            stock_counts = group["stock_code"].value_counts() if "stock_code" in group else pd.Series(dtype=float)
            row = {
                "recipe_name": recipe_name,
                "setup_type": setup_type,
                "event_count": int(len(group)),
                "unique_stock_count": int(group["stock_code"].nunique()) if "stock_code" in group else 0,
                "top5_stock_event_share": float(stock_counts.head(5).sum() / len(group)) if len(group) else np.nan,
                "avg_setup_score": float(group["setup_score"].mean()) if "setup_score" in group else np.nan,
            }
            for horizon in horizons:
                return_col = f"forward_return_{int(horizon)}"
                drawdown_col = f"forward_max_drawdown_{int(horizon)}"
                returns = group[return_col].dropna() if return_col in group else pd.Series(dtype=float)
                drawdowns = group[drawdown_col].dropna() if drawdown_col in group else pd.Series(dtype=float)
                wins = returns[returns > 0]
                losses = returns[returns <= 0]
                avg_return = float(returns.mean()) if not returns.empty else np.nan
                avg_drawdown = float(drawdowns.mean()) if not drawdowns.empty else np.nan
                row[f"avg_forward_return_{int(horizon)}"] = float(returns.mean()) if not returns.empty else np.nan
                row[f"median_forward_return_{int(horizon)}"] = float(returns.median()) if not returns.empty else np.nan
                row[f"p25_forward_return_{int(horizon)}"] = float(returns.quantile(0.25)) if not returns.empty else np.nan
                row[f"p75_forward_return_{int(horizon)}"] = float(returns.quantile(0.75)) if not returns.empty else np.nan
                row[f"win_rate_{int(horizon)}"] = float((returns > 0).mean()) if not returns.empty else np.nan
                row[f"avg_forward_max_drawdown_{int(horizon)}"] = avg_drawdown
                row[f"p95_forward_drawdown_{int(horizon)}"] = float(drawdowns.quantile(0.05)) if not drawdowns.empty else np.nan
                row[f"return_drawdown_ratio_{int(horizon)}"] = avg_return / abs(avg_drawdown) if pd.notna(avg_return) and pd.notna(avg_drawdown) and avg_drawdown != 0 else np.nan
                row[f"avg_win_{int(horizon)}"] = float(wins.mean()) if not wins.empty else np.nan
                row[f"avg_loss_{int(horizon)}"] = float(losses.mean()) if not losses.empty else np.nan
            rows.append(row)
        return pd.DataFrame(rows).sort_values(["recipe_name", "setup_type"]).reset_index(drop=True)
