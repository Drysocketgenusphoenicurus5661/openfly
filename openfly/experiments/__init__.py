"""Experiment harness: data access, synthetic pricer, targets, feature cache,
light straddle simulator, controls, metrics and the runner.

Nothing in this package talks to the broker. Bars come from the market store
(DuckDB) when present or from the parquet files under data/history.
"""
