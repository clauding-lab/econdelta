// EconDelta PWA — Supabase data layer
// Single RPC call (get_latest_dashboard) populates window.ED_DATA for all pages.
// Falls back to window.ED_DATA from data-mock.js if config is missing.
//
// Pages don't know whether the data came from mock or live — they just read
// window.ED_DATA. Keep that contract intact when extending this file.

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

  // Render a loading sliver so user knows we're alive.
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

  async function bootstrap(){
    // Single RPC for the Latest page (definitions + values + sources_status).
    const dashRes = await fetch(`${cfg.url}/rest/v1/rpc/get_latest_dashboard`, {
      method: 'POST',
      headers: HEADERS,
      body: '{}',
    });
    if(!dashRes.ok) throw new Error(`RPC ${dashRes.status}: ${await dashRes.text()}`);
    const dashboard = await dashRes.json();

    // Direct REST for archive (90-day window of metric_history).
    // Note: metric_history has no source_as_of column — writer overrides as_of
    // with publication date for slow-cadence metrics (FSAR/DAM/NBR).
    const since = new Date(Date.now() - 90*24*3600*1000).toISOString().slice(0,10);
    const histRes = await fetch(
      `${cfg.url}/rest/v1/metric_history?as_of=gte.${since}&select=metric_id,value,as_of&order=as_of.asc&limit=10000`,
      { headers: HEADERS }
    );
    const history = histRes.ok ? await histRes.json() : [];

    // Direct REST for runs (90-day window of run_logs).
    const sinceTs = new Date(Date.now() - 90*24*3600*1000).toISOString();
    const runsRes = await fetch(
      `${cfg.url}/rest/v1/run_logs?started_at=gte.${sinceTs}&select=*&order=started_at.asc&limit=10000`,
      { headers: HEADERS }
    );
    const runRows = runsRes.ok ? await runsRes.json() : [];

    // Re-shape runs by source (page-runs expects an array per source).
    const runsBySource = {};
    runRows.forEach(r => {
      if(!runsBySource[r.source]) runsBySource[r.source] = [];
      runsBySource[r.source].push({
        date: r.started_at.slice(0, 10),
        startedAt: r.started_at,
        finishedAt: r.finished_at,
        durationMs: r.duration_ms,
        status: r.status,
        error: r.error,
      });
    });

    // Bundle compat shim — bundle's Masthead component reads data.bundle.data
    // (flat metric_id -> scalar map), data.bundle.sources_status, data.bundle.updated_at,
    // and data.tickers (scrolling tape). Build them from the new RPC payload.
    const flatValues = {};
    Object.entries(dashboard.values || {}).forEach(([metricId, v]) => {
      if (v && v.value != null) flatValues[metricId] = v.value;
    });

    window.ED_DATA = {
      today: new Date(),
      dashboard,
      history,
      runs: runsBySource,
      bundle: {
        data: flatValues,
        sources_status: dashboard.sources_status || {},
        updated_at: dashboard.updated_at,
      },
      tickers: [],
    };

    // Tell App.jsx to re-render now that data is loaded.
    window.dispatchEvent(new CustomEvent('ed:data-changed'));
  }

  // Manual refresh — pull-to-refresh or button click.
  // Bundle's Masthead calls window.__edRefresh; expose under both names.
  window.ED_REFRESH = () => bootstrap();
  window.__edRefresh = () => bootstrap();
})();
