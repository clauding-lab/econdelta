// EconDelta /macro tab — long-horizon analytical view.
//
// Mounts on hash route '#/macro'. Lazy-loads Chart.js 4.4.0 from CDN on first
// visit (cached by service worker on subsequent loads). Fetches metric_history_monthly
// via PostgREST using the same anon key wired into pwa/lib/supabase-client.js.

const { useState: useStateM, useEffect: useEffectM, useRef: useRefM } = React;

// ---------------------------------------------------------------------------
// Date formatting — display as Mon'YY (e.g. 2026-02-01 → "Feb'26")
// ---------------------------------------------------------------------------

const MACRO_MONTH_ABBR = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
function formatPeriod(dateStr) {
  if (!dateStr) return '';
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return dateStr;
  return MACRO_MONTH_ABBR[d.getUTCMonth()] + "'" + String(d.getUTCFullYear()).slice(2);
}

// ---------------------------------------------------------------------------
// Chart.js loader (lazy, idempotent)
// ---------------------------------------------------------------------------

const CHARTJS_URL = 'https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js';
const CHARTJS_DATE_ADAPTER_URL =
  'https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js';

function ensureChartJS() {
  if (window.Chart && window.__edChartAdapterReady) return Promise.resolve(window.Chart);
  if (window.__edChartLoading) return window.__edChartLoading;

  const loadOne = src => new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src;
    s.async = true;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error('failed to load ' + src));
    document.head.appendChild(s);
  });

  window.__edChartLoading = (async () => {
    if (!window.Chart) await loadOne(CHARTJS_URL);
    if (!window.__edChartAdapterReady) {
      await loadOne(CHARTJS_DATE_ADAPTER_URL);
      window.__edChartAdapterReady = true;
    }
    return window.Chart;
  })();
  return window.__edChartLoading;
}

// ---------------------------------------------------------------------------
// Data fetcher — own PostgREST call so we don't bloat every page's bootstrap.
// ---------------------------------------------------------------------------

const KEY_METRICS_USED = [
  'point_to_point_inflation_monthly', 'cpi_p2p_food_monthly', 'cpi_p2p_nonfood_monthly',
  'cpi_12m_avg_monthly', 'cpi_12m_food_monthly', 'cpi_12m_nonfood_monthly',
  'bb_repo_rate_monthly', 'tbill_364d_yield_monthly',
  'yield_2y_monthly', 'yield_5y_monthly', 'yield_10y_monthly', 'yield_20y_monthly',
  'real_policy_rate_monthly',
  'domestic_credit_total_monthly', 'domestic_credit_public_monthly', 'domestic_credit_private_monthly',
  'domestic_credit_growth_yoy_monthly',
  'private_credit_growth_yoy_monthly', 'public_credit_growth_yoy_monthly',
  'm1_growth_yoy_monthly', 'm2_growth_yoy_monthly',
  'exports_usd_mn_monthly', 'imports_usd_mn_monthly', 'remittance_usd_mn_monthly',
  'gross_reserves_usd_bn_monthly', 'import_cover_months_monthly',
  'usd_bdt_mid_monthly', 'reer_monthly',
  'dsex_monthly',
];

async function fetchMonthlyData() {
  if (window.ED_DATA && window.ED_DATA.macroMonthly) return window.ED_DATA.macroMonthly;
  const cfg = window.ED_SUPABASE_CONFIG;
  if (!cfg || !cfg.url || !cfg.anonKey) throw new Error('Supabase config missing');
  const inList = KEY_METRICS_USED.join(',');
  const baseUrl = cfg.url + '/rest/v1/metric_history_monthly'
                + '?metric_id=in.(' + inList + ')'
                + '&select=metric_id,as_of,value'
                + '&order=as_of.asc';
  // PostgREST caps each response at 1000 rows; page through with Range header.
  const PAGE_SIZE = 1000;
  const all = [];
  for (let offset = 0; ; offset += PAGE_SIZE) {
    const resp = await fetch(baseUrl, {
      headers: {
        apikey: cfg.anonKey,
        Authorization: 'Bearer ' + cfg.anonKey,
        Range: offset + '-' + (offset + PAGE_SIZE - 1),
      },
    });
    if (!resp.ok && resp.status !== 206) {
      throw new Error('HTTP ' + resp.status + ': ' + (await resp.text()));
    }
    const rows = await resp.json();
    all.push.apply(all, rows);
    if (rows.length < PAGE_SIZE) break;
  }

  const byMetric = {};
  all.forEach(r => {
    if (!byMetric[r.metric_id]) byMetric[r.metric_id] = [];
    byMetric[r.metric_id].push([r.as_of, Number(r.value)]);
  });
  if (!window.ED_DATA) window.ED_DATA = {};
  window.ED_DATA.macroMonthly = byMetric;
  return byMetric;
}

// ---------------------------------------------------------------------------
// ChartCard — one per chart, hosts a <canvas> and instantiates Chart.js
// ---------------------------------------------------------------------------

function ChartCard({ fig, title, subtitle, latestValueText, configFn, seriesByMetric, extra }) {
  const canvasRef = useRefM(null);
  const chartRef = useRefM(null);

  useEffectM(() => {
    let cancelled = false;
    ensureChartJS().then(Chart => {
      if (cancelled || !canvasRef.current) return;
      if (chartRef.current) { chartRef.current.destroy(); chartRef.current = null; }
      const cfg = configFn(seriesByMetric, extra);
      chartRef.current = new Chart(canvasRef.current.getContext('2d'), cfg);
    }).catch(err => {
      console.error('chart init failed', err);
    });
    return () => {
      cancelled = true;
      if (chartRef.current) { chartRef.current.destroy(); chartRef.current = null; }
    };
  }, [configFn, seriesByMetric, extra]);

  return (
    <div className="macro-card">
      <div className="macro-card-head">
        <span className="macro-fig">FIG.{String(fig).padStart(2, '0')}</span>
        <h3 className="macro-card-title">{title}</h3>
        {subtitle && <div className="macro-card-sub">{subtitle}</div>}
        {latestValueText && <div className="macro-card-latest">{latestValueText}</div>}
      </div>
      <div className="macro-card-canvas">
        <canvas ref={canvasRef}></canvas>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Event strip + modal
// ---------------------------------------------------------------------------

function EventStrip({ events, onSelect }) {
  return (
    <div className="macro-events">
      {events.map(e => (
        <button
          key={e.id}
          className="macro-event-card"
          style={{ borderLeftColor: e.color }}
          onClick={() => onSelect(e)}
        >
          <div className="macro-event-meta" style={{ color: e.color }}>
            {formatPeriod(e.date)} · {e.category}
          </div>
          <div className="macro-event-title">{e.title}</div>
          <div className="macro-event-sum">{e.summary}</div>
          <div className="macro-event-cta">CLICK HERE →</div>
        </button>
      ))}
    </div>
  );
}

function EventModal({ event, seriesByMetric, onClose }) {
  if (!event) return null;
  const cfgs = window.MACRO_CHART_CONFIGS;

  // KPI rows: pick the value at event.date (or nearest prior month)
  const kpiRows = (event.kpiMetricIds || []).map(mid => {
    const series = seriesByMetric[mid] || [];
    let val = null, asOf = null;
    for (let i = series.length - 1; i >= 0; i--) {
      if (series[i][0] <= event.date) { val = series[i][1]; asOf = series[i][0]; break; }
    }
    return { metricId: mid, value: val, asOf };
  });

  return (
    <div className="macro-modal-backdrop" onClick={onClose}>
      <div className="macro-modal" onClick={e => e.stopPropagation()}>
        <button className="macro-modal-close" onClick={onClose} aria-label="Close">×</button>
        <div className="macro-modal-cat" style={{ color: event.color || undefined }}>
          {formatPeriod(event.date)} · {event.category}
        </div>
        <h2 className="macro-modal-title">{event.title}</h2>
        <div className="macro-modal-date">{event.date}</div>

        <div className="macro-modal-kpis">
          {kpiRows.map(r => (
            <div key={r.metricId} className="macro-kpi-row">
              <span className="macro-kpi-label">{r.metricId}</span>
              <span className="macro-kpi-value">
                {r.value == null ? '—' : Number(r.value).toLocaleString(undefined, { maximumFractionDigits: 2 })}
              </span>
              <span className="macro-kpi-date">{r.asOf || ''}</span>
            </div>
          ))}
        </div>

        <div className="macro-modal-charts">
          <ChartCard fig="A" title="Inflation & Repo (±6m)"
            configFn={cfgs.eventInflationRepoMini}
            seriesByMetric={seriesByMetric}
            extra={event.date}/>
          <ChartCard fig="B" title="Reserves & BDT/USD (±6m)"
            configFn={cfgs.eventReservesBdtMini}
            seriesByMetric={seriesByMetric}
            extra={event.date}/>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// PageMacro — root
// ---------------------------------------------------------------------------

function PageMacro() {
  const [data, setData] = useStateM(null);
  const [error, setError] = useStateM(null);
  const [openEvent, setOpenEvent] = useStateM(null);

  useEffectM(() => {
    fetchMonthlyData().then(setData).catch(e => setError(String(e)));
  }, []);

  const events = window.MACRO_EVENTS || [];
  const cfgs = window.MACRO_CHART_CONFIGS || {};

  if (error) {
    return (
      <React.Fragment>
        <PageHead kicker="Long-horizon · monthly observations" title="Macro"/>
        <p className="sec-lede" style={{ color: '#a33' }}>{error}</p>
      </React.Fragment>
    );
  }
  if (!data) {
    return (
      <React.Fragment>
        <PageHead kicker="Long-horizon · monthly observations" title="Macro"/>
        <div className="loading">loading monthly history…</div>
      </React.Fragment>
    );
  }

  // Latest-as-of for meta line
  const allDates = Object.values(data).reduce((acc, arr) => {
    arr.forEach(([d]) => acc.push(d));
    return acc;
  }, []).sort();
  const latest = allDates.length ? allDates[allDates.length - 1] : '—';

  return (
    <React.Fragment>
      <PageHead
        kicker="Long-horizon · monthly observations"
        title="Macro"
        meta={`JAN 2012 — ${latest} · 13 charts · ${events.length} events`}
      />

      <section className="macro-section">
        <h2 className="macro-section-title">Prices &amp; Policy</h2>
        <div className="macro-grid">
          <ChartCard fig={4} title="CPI Inflation · Point-to-Point"
            subtitle="General, food, non-food YoY"
            configFn={cfgs.cpiP2P} seriesByMetric={data}/>
          <ChartCard fig={5} title="Inflation · 12-Month Average"
            subtitle="General, food, non-food trailing"
            configFn={cfgs.inflation12mAvg} seriesByMetric={data}/>
          <ChartCard fig={6} title="Repo &amp; 364-Day T-Bill"
            configFn={cfgs.repoAndTbill} seriesByMetric={data}/>
          <ChartCard fig={7} title="Sovereign Yield Curve · 2Y to 20Y"
            subtitle="One curve per month; latest highlighted"
            configFn={cfgs.yieldCurve} seriesByMetric={data}/>
          <ChartCard fig={8} title="Real Policy Rate"
            subtitle="BB repo minus headline CPI YoY"
            configFn={cfgs.realPolicyRate} seriesByMetric={data}/>
        </div>
      </section>

      <section className="macro-section">
        <h2 className="macro-section-title">Credit &amp; Money</h2>
        <div className="macro-grid">
          <ChartCard fig={1} title="Domestic Credit · Composition"
            subtitle="Public + private stack" configFn={cfgs.domesticCreditComposition}
            seriesByMetric={data}/>
          <ChartCard fig={2} title="Credit Growth · Total, Public, Private"
            configFn={cfgs.domesticCreditGrowth} seriesByMetric={data}/>
          <ChartCard fig={3} title="Money Growth · M1 &amp; M2"
            configFn={cfgs.moneyGrowth} seriesByMetric={data}/>
        </div>
      </section>

      <section className="macro-section">
        <h2 className="macro-section-title">External Sector</h2>
        <div className="macro-grid">
          <ChartCard fig={9} title="FX Inflows vs Outflows"
            subtitle="Exports + remittance vs imports"
            configFn={cfgs.fxFlows} seriesByMetric={data}/>
          <ChartCard fig={10} title="FX Reserves"
            configFn={cfgs.fxReserves} seriesByMetric={data}/>
          <ChartCard fig={11} title="Import Cover · Adequacy"
            subtitle="Months of imports covered by reserves"
            configFn={cfgs.importCover} seriesByMetric={data}/>
          <ChartCard fig={12} title="BDT/USD &amp; REER"
            configFn={cfgs.bdtUsdReer} seriesByMetric={data}/>
        </div>
      </section>

      <section className="macro-section">
        <h2 className="macro-section-title">Capital Market</h2>
        <div className="macro-grid">
          <ChartCard fig={13} title="DSEX Index · with event markers"
            configFn={cfgs.dsex} seriesByMetric={data} extra={events}/>
        </div>
        <EventStrip events={events} onSelect={setOpenEvent}/>
      </section>

      {openEvent && (
        <EventModal event={openEvent} seriesByMetric={data} onClose={() => setOpenEvent(null)}/>
      )}
    </React.Fragment>
  );
}

window.PageMacro = PageMacro;
