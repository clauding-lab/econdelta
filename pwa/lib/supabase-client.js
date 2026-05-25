// EconDelta PWA — Supabase data layer
// Single RPC call (get_latest_dashboard) populates window.ED_DATA for all pages.
// Falls back to window.ED_DATA from data-mock.js if config is missing.
//
// Pages don't know whether the data came from mock or live — they just read
// window.ED_DATA. Keep that contract intact when extending this file.
//
// Data contract exposed on window.ED_DATA (matches the bundle's mock):
//   today                — Date
//   dashboard            — full RPC payload {updated_at, definitions, values, sources_status}
//   bundle.data          — flat metric_id → scalar map
//   bundle.sources_status — {source: {status, last_success, ...}}
//   bundle.updated_at    — ISO timestamp
//   tickers              — [{key, group, label, unit, val, delta, spark, fmt}, ...]
//   series               — {metric_id: [v0, v1, ..., v(N-1)]} N-day arrays (null gaps)
//   days                 — [{date, dt, trading}, ...] N-day calendar
//   history              — raw metric_history rows (N-day window)
//   runs                 — {source: [{date, startedAt, finishedAt, durationMs, status, error}]}
//
// N = RUN_WINDOW_DAYS (currently 60). Adjust here and the dashboard label in
// pwa/pages/runs.jsx stays in lockstep via the same constant.

const RUN_WINDOW_DAYS = 60;

(function(){
  const cfg = window.ED_SUPABASE_CONFIG;

  if(!cfg || !cfg.url || !cfg.anonKey){
    console.warn('[EconDelta] ED_SUPABASE_CONFIG not set — using data-mock if loaded.');
    if(window.ED_DATA){ return; }
    document.getElementById('root').innerHTML =
      '<pre style="padding:24px;font-family:monospace">' +
      'EconDelta dashboard: no data source configured.\n\n' +
      'Either:\n' +
      '  (a) load lib/data-mock.js for the mock dataset, or\n' +
      '  (b) set window.ED_SUPABASE_CONFIG = { url, anonKey } before this script\n' +
      '</pre>';
    return;
  }

  const HEADERS = {
    apikey: cfg.anonKey,
    Authorization: `Bearer ${cfg.anonKey}`,
    'Content-Type': 'application/json',
    Prefer: 'count=none',
  };

  const root = document.getElementById('root');
  if(root) root.innerHTML =
    '<div style="padding:32px;font-family:monospace;color:#888">loading from supabase…</div>';

  bootstrap().catch(err => {
    console.error('[EconDelta] bootstrap failed', err);
    if(root) root.innerHTML =
      '<pre style="padding:24px;font-family:monospace;color:#a33">' +
      'EconDelta dashboard: failed to load from Supabase.\n\n' +
      String(err) + '\n</pre>';
  });

  // BD trading day: Sun–Thu (Fri=5, Sat=6 are weekend)
  const HOLIDAYS_2026 = new Set(['2026-01-01','2026-02-21','2026-03-26','2026-05-01','2026-08-15']);
  function isTradingDay(d){
    const w = d.getUTCDay();
    if (w === 5 || w === 6) return false;
    return !HOLIDAYS_2026.has(d.toISOString().slice(0,10));
  }

  // Format helpers — keyed by metric_definitions.format
  const FORMATTERS = {
    'pct-1dp':       v => v == null ? '—' : v.toFixed(1) + '%',
    'pct-2dp':       v => v == null ? '—' : v.toFixed(2) + '%',
    'comma-2dp':     v => v == null ? '—' : Number(v).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}),
    'comma-0dp':     v => v == null ? '—' : Number(v).toLocaleString(undefined, {minimumFractionDigits: 0, maximumFractionDigits: 0}),
    'currency-bdt':  v => v == null ? '—' : Number(v).toLocaleString(),
  };
  function fmtFor(definition){
    return FORMATTERS[definition?.format] || FORMATTERS['comma-2dp'];
  }

  // Bundle's curated 3 sections (forex/dse/commodities) plus v3 expansion sections.
  // group → ordered list of metric_ids to render. Order matters — first 4 take 4-col row.
  const TICKER_GROUPS = [
    {
      key: 'Forex',
      lede: 'Bangladesh Bank indicative rates and gross reserves.',
      metrics: ['usd_bdt_mid', 'eur_bdt', 'gbp_bdt', 'gross_reserves_usd_bn'],
    },
    {
      key: 'DSE',
      lede: 'Dhaka Stock Exchange indices and breadth for the most recent trading day.',
      metrics: ['dsex', 'ds30', 'dses', 'turnover_crore'],
    },
    {
      key: 'Commodities',
      lede: 'Energy and gold benchmarks via yfinance.',
      metrics: ['brent_crude_usd_barrel', 'wti_crude_usd_barrel', 'gold_usd_oz'],
    },
    {
      key: 'Inflation',
      lede: 'BBS Consumer Price Index — point-to-point and 12-month trailing.',
      metrics: ['point_to_point_inflation', 'general_inflation', 'food_inflation', 'non_food_inflation'],
    },
    {
      key: 'Money Market',
      lede: 'Banking stability indicators and short-term funding rates.',
      metrics: ['gross_npl_ratio', 'gsec_auction', 'call_money_rate', 'reverse_repo_rate'],
    },
    {
      key: 'Monetary Aggregates',
      lede: 'Bangladesh Bank monetary survey aggregates.',
      metrics: ['broad_money', 'reserve_money', 'currency_outside_bank', 'deposits_of_the_system'],
    },
    {
      key: 'Government Finance',
      lede: 'NBR collections and revenue performance.',
      metrics: ['nbr_fytd_collected_cr', 'nbr_customs_collected_cr', 'nbr_vat_collected_cr', 'nbr_it_collected_cr'],
    },
    {
      key: 'External Sector',
      lede: 'Exports, imports, and remittances flowing through the BoP.',
      metrics: ['monthly_import', 'fy_export', 'categorywise_export'],
    },
  ];

  function pctChange(today, prev){
    if (today == null || prev == null || prev === 0) return null;
    return (today - prev) / prev;
  }

  async function bootstrap(){
    // Single RPC for definitions + values + sources_status.
    const dashRes = await fetch(`${cfg.url}/rest/v1/rpc/get_latest_dashboard`, {
      method: 'POST', headers: HEADERS, body: '{}',
    });
    if(!dashRes.ok) throw new Error(`RPC ${dashRes.status}: ${await dashRes.text()}`);
    const dashboard = await dashRes.json();

    // Direct REST: N-day metric_history.
    const since = new Date(Date.now() - RUN_WINDOW_DAYS*24*3600*1000).toISOString().slice(0,10);
    const histRes = await fetch(
      `${cfg.url}/rest/v1/metric_history?as_of=gte.${since}&select=metric_id,value,as_of&order=as_of.asc&limit=20000`,
      { headers: HEADERS }
    );
    const history = histRes.ok ? await histRes.json() : [];

    // Direct REST: N-day run_logs.
    const sinceTs = new Date(Date.now() - RUN_WINDOW_DAYS*24*3600*1000).toISOString();
    const runsRes = await fetch(
      `${cfg.url}/rest/v1/run_logs?started_at=gte.${sinceTs}&select=*&order=started_at.asc&limit=10000`,
      { headers: HEADERS }
    );
    const runRows = runsRes.ok ? await runsRes.json() : [];

    // Build N-day calendar.
    const today = new Date();
    const days = [];
    for (let i = RUN_WINDOW_DAYS - 1; i >= 0; i--) {
      const d = new Date(today.getTime() - i * 24 * 3600 * 1000);
      days.push({
        date: d.toISOString().slice(0, 10),
        dt: d,
        trading: isTradingDay(d),
      });
    }
    const dayKeys = days.map(d => d.date);

    // Index history by metric_id then date for series construction.
    const histByMetric = {};
    history.forEach(r => {
      if (!histByMetric[r.metric_id]) histByMetric[r.metric_id] = {};
      histByMetric[r.metric_id][r.as_of] = r.value;
    });

    // Build per-metric N-day series (null where missing).
    const series = {};
    Object.keys(histByMetric).forEach(metric_id => {
      series[metric_id] = dayKeys.map(d => histByMetric[metric_id][d] ?? null);
    });

    // Index definitions by metric_id.
    const defsById = {};
    (dashboard.definitions || []).forEach(d => { defsById[d.metric_id] = d; });

    // Flat values map for bundle.data.
    const flatValues = {};
    Object.entries(dashboard.values || {}).forEach(([metric_id, v]) => {
      if (v && v.value != null) flatValues[metric_id] = v.value;
    });

    // Build tickers — one per metric_id in the curated groups.
    // Drop tickers whose metric_id has no value AND no definition (avoid empty cards).
    const tickers = [];
    TICKER_GROUPS.forEach(group => {
      group.metrics.forEach(metric_id => {
        const def = defsById[metric_id];
        const val = flatValues[metric_id];
        // Skip if neither value nor definition — nothing to render.
        if (val == null && !def) return;

        // Compute delta from history: today / prior non-null value - 1.
        const arr = series[metric_id];
        let delta = null;
        if (arr && arr.length >= 2) {
          const todayVal = arr[arr.length - 1];
          // Find most recent prior non-null value.
          let priorVal = null;
          for (let i = arr.length - 2; i >= 0; i--) {
            if (arr[i] != null) { priorVal = arr[i]; break; }
          }
          delta = pctChange(todayVal, priorVal);
        }

        // Sparkline: last 30 of the N-day series. For DSE indices, mask non-trading days.
        let spark = null;
        if (arr && arr.length >= 5) {
          const last30 = arr.slice(-30);
          if (group.key === 'DSE') {
            // mask non-trading days — but since metric_history may already exclude
            // those (DSE writes 'skip' rows), just keep nulls as-is
          }
          spark = last30;
        }

        const label = (def && (def.short_label || def.label)) ||
                      metric_id.split('_').map(w => w[0].toUpperCase() + w.slice(1)).join(' ');
        const unit = (def && def.unit) || '';
        const fmt = fmtFor(def);

        tickers.push({
          key: metric_id,
          group: group.key,
          label,
          unit,
          val,
          delta,
          spark,
          fmt,
        });
      });
    });

    // Re-shape run_logs rows by source for the runs page.
    const runsBySource = {};
    runRows.forEach(r => {
      if (!runsBySource[r.source]) runsBySource[r.source] = [];
      runsBySource[r.source].push({
        date: r.started_at.slice(0, 10),
        startedAt: r.started_at,
        finishedAt: r.finished_at,
        durationMs: r.duration_ms,
        status: r.status,
        error: r.error,
        attempt: r.attempt || 1,
      });
    });

    window.ED_DATA = {
      today,
      dashboard,
      history,
      runs: runsBySource,
      days,
      series,
      tickers,
      tickerGroups: TICKER_GROUPS.map(g => ({ key: g.key, lede: g.lede })),
      bundle: {
        data: flatValues,
        sources_status: dashboard.sources_status || {},
        updated_at: dashboard.updated_at,
      },
    };

    window.dispatchEvent(new CustomEvent('ed:data-changed'));
  }

  // Manual refresh — pull-to-refresh or button click.
  // Bundle's Masthead calls window.__edRefresh; expose under both names.
  window.ED_REFRESH = () => bootstrap();
  window.__edRefresh = () => bootstrap();
})();
