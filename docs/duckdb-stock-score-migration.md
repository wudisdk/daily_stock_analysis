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
  - `get_belong_board()` now uses the lightweight `stock_basic` industry and
    market fields as an A-share board-membership fallback before slower
    third-party board endpoints.
  - Tushare realtime quotes now use `daily_basic` to fill missing
    `volume_ratio`, `turnover_rate`, `pe_ratio`, `pb_ratio`, `total_mv`, and
    `circ_mv`.
- `data_provider/base.py`
  - `DataFetcherManager.get_fundamental_context()` now merges the latest
    Tushare `fina_indicator` snapshot into the earnings/growth payloads when a
    Tushare token is available.
  - Hong Kong realtime quote routing now tries YFinance before the slower
    AkShare full-market backup, while still using AkShare as a final fallback.
  - YFinance realtime quote successes are now labeled with `source=yfinance`
    instead of the generic fallback source, so the prompt/context pack no
    longer marks successful HK/US quote data as a degraded realtime fallback.
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
  - The `price_flow_hot` proxy now falls back to trend-derived
    `bias_ma5`/`volume_ratio_5d` when realtime quote providers such as
    YFinance do not expose quote-level volume ratio fields.
  - Combined `de_risk` labels keep the canonical `price_flow_hot` token, for
    example `flow_broke_price_flow_hot`, so replay queues emit both
    `flow_broke_proxy` and `price_flow_hot_proxy` instead of dropping the
    price-flow invalidation flag.
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
  - The dated snapshot file uses the latest `trade_date` across all exported
    rows, so mixed-market exports are not renamed by whichever candidate ranks
    first.
  - Snapshot rows now include `input_snapshot_hash`, a deterministic SHA-256
    digest of the stable, already-redacted payload. The hash excludes volatile
    run metadata such as `created_at`, `run_id`, and `query_id`, so repeated
    hosted runs can be joined back to the same downstream review/memo input
    while real input changes still get a new digest.
- `src/services/ai_snapshot_audit.py`
  - Hosted runs now write a DuckDB-style PASS/WARN/FAIL audit next to the
    candidate snapshot. The audit checks JSONL parseability, schema version,
    DeepSeek pro model usage, hash uniqueness, dated-file naming, required
    factor dimensions, data coverage labels, and low-sensitivity boundaries.
  - The audit also breaks `data_coverage` down by low-sensitivity blocks
    (`quote`, `daily_bars`, `technical`, `fundamentals`, `factor_snapshot`,
    `news`) and reports missing/weak blocks with row identities and missing
    reasons. Critical blocks still drive WARN status, while fundamentals/news
    gaps stay visible as optional INFO debt. This ports the DuckDB
    data-quality-first workflow into GitHub artifacts so weak upstream data is
    visible before outcome replay.
- `src/services/ai_snapshot_step_summary.py`
  - The daily workflow now renders snapshot, replay-queue, price-history, and
    gap-triage audit status directly into GitHub Step Summary. It includes the
    `data_coverage` block table and the first weak critical rows, so daily
    data-source failures can be triaged from the Actions page before opening
    downloaded artifacts.
- `src/services/ai_snapshot_gap_triage.py`
  - Converts weak snapshot coverage rows into a DuckDB-style action queue:
    `block + missing_reason`, affected candidate count, sampled stock codes,
    severity, action bucket, and suggested next step. Daily runs write JSON,
    CSV, Markdown, and bucket-level summary artifacts so realtime quote, daily
    bar, technical, factor, fundamental, and news gaps are prioritized
    separately.
    Critical blocks appear as WARN; non-critical fundamentals/news gaps remain
    INFO rows instead of being hidden by an otherwise successful workflow.
    The summary view groups by `severity + action_bucket`, matching the DuckDB
    validation pattern of separating system-level blockers from informational
    source debt before drilling into per-stock rows.
  - Gap triage now also writes PASS/WARN/FAIL audit artifacts. The audit checks
    detail/summary schemas, summary totals against detail rows, critical-gap
    visibility, optional-source debt visibility, and low-sensitivity
    boundaries, so the triage layer itself cannot silently drift. The Markdown
    summary renders this self-audit status before the bucket/detail tables, so
    Actions Step Summary shows whether the triage artifact itself is reliable.
- `src/services/ai_snapshot_replay_queue.py`
  - Hosted runs now expand each fixed candidate snapshot into a future-outcome
    replay queue with 1d, 3d, 1w, 1m, and 1q horizons. Each row is keyed by
    `input_snapshot_hash` plus horizon, carries the low-sensitivity factor
    labels, and marks `outcome_status=pending_future_price`.
  - The replay queue migrates the DuckDB `research_stock_ai_memo_outcome*`
    join contract without feeding future returns back into the daily
    recommendation path. It also surfaces deterministic review flags such as
    `flow_broke_proxy`, `price_flow_hot_proxy`, `hard_risk_hit_proxy`,
    `fund_flow_missing`, and data-coverage review markers for later validation.
  - The queue now also derives low-sensitivity `case_tags` from snapshot
    dimensions, such as `technical_strong`, `theme_tailwind`,
    `fund_flow_available`, `price_hot_20d`, `flow_broke`, and
    `quality_growth_missing`. This is the hosted analogue of DuckDB's
    AI memo bull/bear tag replay without exporting raw cases or news text.
  - The replay queue command also writes latest and dated PASS/FAIL audit JSON
    and CSV files. The audit checks horizon coverage, deterministic join keys,
    snapshot-hash links, dated output files, pending outcome status, known
    invalidation flags, known case tags, and low-sensitivity boundaries.
- `src/services/ai_snapshot_replay_outcome.py`
  - A portable outcome resolver is available for later validation jobs. It
    consumes a replay queue plus point-in-time price history (`stock_code`,
    `trade_date`, `close`), resolves future trading-day closes, and writes
    latest/dated outcome JSONL, CSV, and summary files.
  - The resolver keeps the daily recommendation path clean: rows with
    insufficient future prices remain `pending_future_price`, while available
    rows calculate future return, equal-weight benchmark return, and excess
    return by `input_snapshot_hash:horizon`.
  - It also writes `stock_ai_candidate_replay_invalidation_summary_*`
    artifacts, comparing each replay invalidation flag's hit/miss groups by
    horizon, model, and outcome status. This is the hosted counterpart of the
    DuckDB `AI失效验证` sheet for `flow_broke` / `price_flow_hot` style checks.
  - A paired `stock_ai_candidate_replay_invalidation_effect_*` artifact now
    compares each flag's hit group directly against its miss group, including
    future-return delta, excess-return delta, excess-win-rate delta, and an
    evidence status. This makes the DuckDB `flow_broke` dynamic exit/de-weight
    question visible without manually joining hit/miss summary rows.
  - `stock_ai_candidate_replay_guard_policy_*` turns those paired hit/miss
    effects into low-sensitivity policy candidates. It keeps sample-count
    gates, directional hints, material excess-return thresholds, an assumed
    round-trip transaction-cost buffer, net deweight edge after cost, and a
    recommended next action such as `backtest_dynamic_exit_or_deweight`.
    This makes the DuckDB `flow_broke` dynamic de-weight research queue
    explicit without changing the live recommendation path.
  - The resolver also writes `stock_ai_candidate_replay_dimension_summary_*`
    artifacts by low-sensitivity snapshot dimension status/label, giving hosted
    runs the first counterpart to DuckDB `AI标签验证` for labels such as
    `fund_flow`, `de_risk`, `price_heat`, and `data_coverage`.
  - `stock_ai_candidate_replay_case_tag_summary_*` groups future outcomes by
    replay `case_tags` and bull/bear group, making the DuckDB
    `research_stock_ai_memo_case_tag_validation_summary` idea portable to
    GitHub artifacts.
  - `stock_ai_candidate_replay_model_summary_*` groups outcomes by model
    decision type, sentiment-score bucket, and confidence label. This mirrors
    DuckDB `AI评级验证` without feeding future returns back into routine scoring.
  - `stock_ai_candidate_replay_outcome_audit_*` adds PASS/WARN/FAIL checks for
    replay outcome rows, price-join errors, summary visibility, and
    low-sensitivity boundaries, so manual replay validation does not silently
    pass with missing future-price evidence.
  - The replay outcome CLI now exits non-zero on audit `FAIL` by default
    (`--no-fail` keeps exploratory runs non-blocking), matching the replay
    queue and price-history audit behavior.
- `src/services/ai_snapshot_price_history.py`
  - Hosted runs now export a low-sensitivity close-price history artifact for
    snapshot/replay candidates from the local `stock_daily` SQLite cache:
    latest and dated JSONL/CSV plus PASS/WARN/FAIL audit files.
  - This migrates the DuckDB point-in-time price-history join discipline into
    GitHub artifacts without uploading the SQLite database or raw provider
    payloads. The daily workflow uses `--scope database`, so the artifact
    covers every stock that the run saved in `stock_daily`, not only the
    current day's top AI candidates. That makes an old candidate snapshot more
    likely to find its later close prices even when the later day's model picks
    a different candidate set.
  - The output keeps only `stock_code`, `trade_date`, `close`, source metadata,
    and candidate-name context needed by replay validation. Outcome resolution
    also tolerates common code aliases such as `600519`, `600519.SH`, and
    `SH600519`, reducing false `missing_anchor_price` statuses.
  - The audit checks candidate coverage, exact anchor-date coverage, forward
    window availability for queued horizons, required fields, and
    low-sensitivity boundaries. Missing future windows are WARN, because they
    are normal until enough trading days have passed.
- `src/services/ai_snapshot_turnover.py`
  - Manual replay validation can now compare an earlier baseline candidate
    snapshot with the source snapshot and write
    `stock_ai_candidate_turnover_*` plus PASS/WARN/FAIL audit files.
  - The summary reports top-N/all candidate retention, added/removed counts,
    candidate turnover rate, and overlap rate using only low-sensitivity stock
    codes and ranks. This migrates the DuckDB turnover/decay discipline into
    GitHub artifacts before any dynamic exit/de-weight rule is allowed into the
    live recommendation path.
- `src/services/daily_run_log_audit.py`
  - Hosted runs now write a lightweight daily log audit under
    `reports/run_audit`. It turns long Actions logs into DuckDB-style
    PASS/WARN/FAIL checks for DeepSeek pro model usage, Gemini/flash
    regressions, legacy `GEMINI_*` env leakage, LLM errors, search
    quota/rate-limit signals, realtime quote fallbacks, fatal tracebacks,
    snapshot-audit visibility, and accidental secret-like values.
  - The audit separates provider API-key bookkeeping and quota-plan noise from
    generic runtime errors, checks that Tavily cooldown markers are visible
    after plan-limit signals, flags successful YFinance realtime quotes that
    are later mislabeled as fallback, and verifies that replay queue and
    price-history step markers are present when a candidate snapshot is
    exported.
  - Secret-like value detection stays conservative for credential contexts but
    ignores `sk-` tokens embedded in URL paths such as `sk-hynix` news slugs,
    reducing false FAILs from raw search-provider result URLs.
  - Search-provider API-key error logs now use non-secret labels such as
    `key#1` instead of logging the first characters of the key, keeping
    uploaded Actions logs useful for diagnosis without exposing key prefixes.
  - The audit intentionally stores counts, statuses, model names, and artifact
    paths rather than raw log excerpts, so it can be uploaded safely alongside
    the reports. This makes recurring API/data-source bugs visible without
    requiring a manual log skim after every workflow run.
  - The daily workflow appends `daily_run_log_audit_latest.md` to the GitHub
    Step Summary, so DeepSeek model usage, quota/cooldown issues, realtime
    fallback labels, replay artifact visibility, and secret-leak checks are
    visible before downloading artifacts.
  - The same audit now records a safe provider-env inventory for the daily
    workflow. It stores provider names, configured/missing/partial status, and
    variable names/counts only, so the Summary can verify DeepSeek is present,
    legacy Gemini is blank, and market/search data credentials are complete
    without writing secret values into artifacts.
- `src/services/network_smoke_audit.py`
  - The non-blocking network smoke workflow now writes sidecar exit-code files
    for the pytest and quick-smoke phases, then converts the logs into
    `reports/network_smoke/network_smoke_audit_latest.{json,csv,md}`.
  - This makes API/key/data-source validity checks reviewable as
    PASS/WARN/FAIL artifacts instead of raw logs only. It highlights failed
    smoke commands, pytest failures, quota/rate-limit signals, network errors,
    and accidental secret-like values without copying raw provider output into
    the summary files. It shares the same URL-slug false-positive guard for
    `sk-` tokens as the daily log audit.
  - The audit also records a safe provider-env inventory for DeepSeek,
    Tushare, Longbridge, Tavily, Bocha, Brave, SerpAPI, Anspire, MiniMax, and
    SearXNG. It stores only variable names, configured/missing/partial status,
    and value counts, so missing or partially configured API groups become
    visible without uploading secret values.
  - The smoke workflow appends `network_smoke_audit_latest.md` to the GitHub
    Step Summary, making API credential coverage, smoke exit codes, quota
    signals, network errors, and secret-like-value checks visible on the run
    page itself.
- `.github/workflows/00-daily-analysis.yml`
  - The hosted daily workflow defaults to DeepSeek LiteLLM models and a
    Tushare-first realtime source order.
  - Legacy Gemini environment variables are blank by default in the daily job,
    so an expired Gemini secret cannot silently override the DeepSeek pro
    runtime. Intentional Gemini use must go through explicit `LLM_CHANNELS`
    configuration.
  - The stock-analysis path now runs the AI snapshot audit before uploading
    artifacts, so snapshot regressions become visible in Actions and in the
    uploaded `reports/ai_snapshot` files.
  - The same stock-analysis path now writes the AI replay queue after the
    snapshot audit, so every uploaded artifact already contains the stable
    future-outcome join keys needed for later replay.
  - The same path also exports the candidate price-history artifact from
    `data/stock_analysis.db`, so later validation can pair an old candidate
    snapshot with a newer run's close-price artifact.
  - Snapshot audit, replay queue, and price-history command summaries are also
    tee'd into `logs/stock_analysis_workflow_steps_*.log`, so the daily log
    audit can verify those post-analysis artifacts instead of depending only
    on the raw GitHub step console.
  - A separate `always()` log-audit step now writes `reports/run_audit` before
    artifact upload, even when the main analysis step fails before the final
    result display.
- `.github/workflows/ai-replay-validation.yml`
  - Manual validation workflow for DuckDB-style replay artifacts. Given a
    daily-analysis `source_run_id`, it downloads the matching
    `analysis-reports-*` artifact, reruns snapshot audit and replay queue
    generation into `replay-validation`, and uploads the validation bundle.
  - When a separate price run/artifact is supplied, the workflow also runs
    `ai_snapshot_replay_outcome` to calculate future return, benchmark return,
    excess return, and summary artifacts. This supports the DuckDB pattern:
    old `source_run_id` candidate snapshot plus a later daily run's
    `stock_ai_candidate_price_history_latest.csv`. The workflow uses only the
    built-in GitHub token with `actions: read` / `contents: read`; it does not
    require provider API secrets or affect the daily recommendation path.
  - When an earlier `baseline_run_id` is supplied, the workflow also runs
    `ai_snapshot_turnover` so the Step Summary and artifacts show whether the
    candidate set is retained or turning over too quickly.
  - The Step Summary now highlights guard-policy candidates and case-tag
    outcomes directly, and it also shows candidate turnover rows when a
    baseline is present. This makes `flow_broke_proxy` exit/deweight evidence,
    bull/bear tag validation, and turnover pressure visible without downloading
    artifacts first.
- `.github/workflows/network-smoke.yml`
  - The smoke workflow now receives the same DeepSeek-first runtime env/secrets
    as the daily workflow, while leaving legacy Gemini unset unless a dedicated
    channel is configured.
  - It now maps the same credentialed search/data providers used by the daily
    job into both smoke phases and the audit phase, so provider availability
    drift is visible before the next scheduled daily analysis.
  - The workflow remains non-blocking, but every phase records its exit code
    and emits a structured audit bundle. This preserves observability while
    avoiding a noisy scheduled workflow that blocks unrelated repository work.

## DuckDB Source Mapping

| DuckDB source | GitHub target | Use in hosted workflow |
| --- | --- | --- |
| `stock_daily_basic_raw` from Tushare `daily_basic` | `TushareFetcher.get_daily_basic_snapshot()` | Fill valuation/liquidity fields for prompts and reports. |
| `stock_fina_indicator_raw` from Tushare `fina_indicator` | `TushareFetcher.get_fina_indicator_snapshot()` | Add point-in-time financial quality fields after prompt/report wiring. |
| `research_stock_score_daily` | Future deterministic factor snapshot | Keep scores interpretable and as-of dated. |
| `research_stock_ai_candidate_snapshot_latest` | `reports/ai_snapshot/stock_ai_candidate_snapshot_latest.jsonl` | Give review/validation a fixed input snapshot instead of mutable live context. |
| `research_stock_score_turnover_decay` | `ai_snapshot_turnover` and `ai-replay-validation.yml` optional `baseline_run_id` | Compare hosted candidate snapshots across runs for top-N/all retention and turnover before turning replay labels into strategy rules. |
| `research_stock_ai_memo_outcome*` | AnalysisContextPack guard warnings, `ai_snapshot_replay_queue`, `ai_snapshot_price_history`, `ai_snapshot_replay_outcome`, and future validation artifact | Convert proven invalidation labels into prompt guardrails first, queue stable future-outcome joins by `input_snapshot_hash`, export point-in-time close prices as low-sensitivity artifacts, then compare model labels with future returns outside routine scoring. |

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
4. After GitHub auth is fixed and daily artifacts are available, run
   `ai-replay-validation.yml` with an older `source_run_id` and a later
   `price_source_run_id`, then compare model/rating groups and invalidation
   flags against realized returns.
