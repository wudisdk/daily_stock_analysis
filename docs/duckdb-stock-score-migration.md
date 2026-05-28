# DuckDB Stock Score Migration Notes

This note records the local DuckDB stock-score ideas that are safe to migrate
into the GitHub daily analysis workflow. It is not investment advice and should
not turn future returns into routine scoring inputs.

## Current GitHub Landing Points

- `data_provider/tushare_fetcher.py`
  - `get_daily_basic_snapshot()` now fetches a latest point-in-time
    `daily_basic` row for A-share stocks.
  - `get_fina_indicator_snapshot()` fetches recent `fina_indicator` rows using
    the point-in-time filter `ann_date <= asof_date`.
  - Tushare realtime quotes now use `daily_basic` to fill missing
    `volume_ratio`, `turnover_rate`, `pe_ratio`, `pb_ratio`, `total_mv`, and
    `circ_mv`.
- `data_provider/base.py`
  - `DataFetcherManager.get_fundamental_context()` now merges the latest
    Tushare `fina_indicator` snapshot into the earnings/growth payloads when a
    Tushare token is available.
- `src/services/analysis_context_builder.py`
  - The `fundamentals` block now derives low-sensitivity capital-flow guard
    warnings from already fetched context. This is a hosted-workflow proxy for
    the DuckDB `flow_broke` and `price_flow_hot` review ideas: it surfaces
    `capital_flow_broke_proxy`, `capital_flow_conflicting_signals`, and
    `price_flow_hot_without_confirmed_inflow` to the prompt summary without
    storing raw capital-flow amounts in the summary.
- `.github/workflows/00-daily-analysis.yml`
  - The hosted daily workflow defaults to DeepSeek LiteLLM models and a
    Tushare-first realtime source order.
- `.github/workflows/network-smoke.yml`
  - The smoke workflow now receives the same runtime env/secrets as the daily
    workflow, so API/data problems are visible before the daily job runs.

## DuckDB Source Mapping

| DuckDB source | GitHub target | Use in hosted workflow |
| --- | --- | --- |
| `stock_daily_basic_raw` from Tushare `daily_basic` | `TushareFetcher.get_daily_basic_snapshot()` | Fill valuation/liquidity fields for prompts and reports. |
| `stock_fina_indicator_raw` from Tushare `fina_indicator` | `TushareFetcher.get_fina_indicator_snapshot()` | Add point-in-time financial quality fields after prompt/report wiring. |
| `research_stock_score_daily` | Future deterministic factor snapshot | Keep scores interpretable and as-of dated. |
| `research_stock_ai_candidate_snapshot_latest` | Future optional JSONL artifact or GitHub Actions artifact | Give AI a fixed input snapshot instead of live mutable facts. |
| `research_stock_ai_memo_outcome*` | AnalysisContextPack guard warnings and future validation artifact | Convert proven invalidation labels into prompt guardrails first, then compare model labels with future returns outside routine scoring. |

## Migration Guardrails

- Keep `ann_date <= asof_date` for financial statement data.
- Keep future returns only in validation, replay, and report-review artifacts.
- Prefer deterministic factor snapshots before model-generated explanations.
- Expose data coverage and failed/empty sources as WARN/FAIL rather than silent
  success.
- Keep hosted Actions lightweight: fetch small per-stock snapshots in the daily
  path, and reserve broad universe backfills for a separate scheduled job.

## Next Implementation Milestones

1. Add a compact `factor_snapshot` block to `AnalysisContextPack` using only
   as-of fields: valuation, quality, growth, momentum, volume-price,
   fund-flow, risk, and confidence.
2. Add a small candidate snapshot export path for GitHub Actions artifacts, then
   let DeepSeek read that fixed snapshot instead of re-querying mutable context.
3. Extend the current `flow_broke` prompt guard into a deterministic
   `factor_snapshot` de-risk flag after sample-out, turnover, cost, and
   drawdown checks.
4. Add a separate validation workflow that writes model/rating/outcome summaries
   without changing the daily recommendation path.
