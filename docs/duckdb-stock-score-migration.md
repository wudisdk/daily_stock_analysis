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
  - Hong Kong realtime quote routing now tries YFinance before the slower
    AkShare full-market backup, while still using AkShare as a final fallback.
  - Hong Kong daily routing now uses YFinance before Tushare and AkShare in the
    hosted path. Tushare `hk_daily` remains a fallback, but its low hourly quota
    no longer creates routine Actions log noise for multi-symbol runs.
- `src/services/analysis_context_builder.py`
  - The `fundamentals` block now derives low-sensitivity capital-flow guard
    warnings from already fetched context. This is a hosted-workflow proxy for
    the DuckDB `flow_broke` and `price_flow_hot` review ideas: it surfaces
    `capital_flow_broke_proxy`, `capital_flow_conflicting_signals`, and
    `price_flow_hot_without_confirmed_inflow` to the prompt summary without
    storing raw capital-flow amounts in the summary.
  - The `factor_snapshot` block now compresses existing quote, technical, and
    fundamental artifacts into DuckDB-style as-of dimensions: technical score
    band, price heat, volume-price activity, valuation coverage,
    quality/growth coverage, fund-flow guard status, a deterministic
    `de_risk` label for `flow_broke` / `price_flow_hot` style guardrails,
    a DuckDB `confidence_score`-style `data_coverage` label, risk flag status,
    and confidence. It also maps fetched board
    membership/sector rankings into a low-sensitivity `industry_theme` label,
    mirroring the DuckDB
    `industry_theme_score` idea without dumping raw board lists. The prompt
    renderer only exposes whitelisted labels and statuses, not raw factor
    payloads or future-return labels.
- `src/services/ai_candidate_snapshot.py`
  - Hosted runs now export a DuckDB-style fixed JSONL snapshot under
    `reports/ai_snapshot/stock_ai_candidate_snapshot_latest.jsonl` plus a
    trade-date copy. The export is intentionally low-sensitivity: it keeps
    ranked model outputs, factor labels/statuses, source coverage, warning
    codes, and news counts, while excluding news bodies, raw capital-flow
    amounts, risk text, secrets, and any future-return labels.
  - Snapshot `trade_date` prefers the latest daily bar date over the workflow
    wall-clock date, so delayed or manual runs after midnight do not mislabel
    the as-of input date.
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
| `research_stock_ai_candidate_snapshot_latest` | `reports/ai_snapshot/stock_ai_candidate_snapshot_latest.jsonl` | Give review/validation a fixed input snapshot instead of mutable live context. |
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

1. Expand `factor_snapshot` with additional as-of fields when they are already
   fetched safely: institution ownership and de-risk flags.
2. Let DeepSeek optionally read the fixed candidate snapshot in later stages
   instead of re-querying mutable context.
3. Calibrate the deterministic `de_risk` flag against sample-out, turnover,
   cost, and drawdown checks before allowing it to change any downstream
   ranking or recommendation logic.
4. Add a separate validation workflow that writes model/rating/outcome summaries
   without changing the daily recommendation path.
