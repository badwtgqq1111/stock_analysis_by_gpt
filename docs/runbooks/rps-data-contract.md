# RPS Data Contract

`RPS_w` is a cross-sectional percentile for `ROCw = close[t-w] / close[t]`.
Because a lower ROC represents a stronger price return, the lowest ROC is assigned
`RPS_w = 100`; the weakest member receives `100 / N`.

## Scope and Timing

An RPS cross section is isolated by:

- `market`, `asset_type`, `frequency`, `adjust`
- `feature_set`, `feature_version`, `feature_config_hash`
- `trade_date`

It uses only same-date price-derived ROC observations and is available at that
day's close. A-share RPS deliberately spans SSE, SZSE and BSE within `market=CN`.
The execution convention remains next trading day.

## Refresh Rule

The implementation is versioned as `source = rps.v2`. For every scope/date,
it recomputes the current cross section and compares those values with the
latest `rps.v2` value for every stock:

- no resulting rank change: no write;
- a new or revised ROC that changes an RPS value: append every stock
  in that scope/date;
- legacy `source = rps` values are ignored as state and are superseded by the
  first `rps.v2` materialization.

This prevents late-arriving stocks and corrected OHLCV rows from leaving the
rest of a date's cross-sectional ranks stale.

## Pipeline Use

RPS is calculated after the `features` stage produces `ROC5/10/20/30/60`, then
flows through `clean_panel` into model training and scoring. It is not a direct
PK constraint. Signal rules should use it as a regime/quality gate: trend
signals need high medium-term RPS, while bottom-reversal signals should require
short-term RPS improvement and positive industry-relative return instead of an
absolute high `RPS_60` threshold.
