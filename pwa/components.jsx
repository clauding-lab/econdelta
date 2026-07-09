// EconDelta — shared UI components (sidebar, sparkline, drawer, etc.)
const { useState, useEffect, useRef, useMemo } = React;

const SOURCE_LABELS = {
  bb_forex: 'BB forex',
  dse_market: 'DSE market',
  commodity_prices: 'Commodities',
  fetch: 'Fetch',
  parse: 'Parse',
  aggregate: 'Aggregate',
  // Backfilled 2026-07 (E3.2) — 7 sources added since the original 6-source
  // launch set. Keep in sync with SOURCE_LABELS' sibling map, CADENCES, in
  // pwa/pages/runs.jsx (grep deploy/econdelta-*.timer for the schedule).
  bb_auction: 'BB auction',
  dse_dayend: 'DSE day-end',
  imf_eff: 'IMF EFF',
  imf_debt_gdp: 'IMF debt/GDP',
  media_screen: 'Media screen',
  world_bank_pink_sheet: 'WB pink sheet',
  briefing: 'Weekly briefing',
};

// Expected data-refresh cadence, in days, keyed by metric_definitions.cadence
// (daily | weekly | monthly | quarterly | fiscal_year — the values actually
// seen in the live catalog). `daily` carries a +2 day buffer to absorb
// Bangladesh's Fri-Sat weekend (most daily series only publish on business
// days, so a metric last written on Thursday reads "2 days old" on Saturday
// without anything actually being broken).
const CADENCE_DAYS = {
  daily: 3,
  weekly: 9,
  monthly: 35,
  quarterly: 97,
  fiscal_year: 380,
};

/**
 * Classify a metric's staleness from its last-published date and its
 * declared cadence. Returns null (no pill) when either input is missing or
 * the cadence label isn't one we recognise — per the E3.2 brief, an
 * unresolvable cadence must never be guessed at.
 * @param {string|null} asOf - ISO date (YYYY-MM-DD) of the metric's latest value.
 * @param {string|null} cadenceLabel - one of CADENCE_DAYS' keys.
 * @returns {{level:'amber'|'red', ageDays:number}|null}
 */
function vintageStatus(asOf, cadenceLabel){
  if(!asOf || !cadenceLabel) return null;
  const days = CADENCE_DAYS[cadenceLabel];
  if(days == null) return null;
  const ageDays = Math.floor((Date.now() - new Date(asOf + 'T00:00:00Z').getTime()) / 86400000);
  if(ageDays > days * 2) return { level: 'red', ageDays };
  if(ageDays > days) return { level: 'amber', ageDays };
  return null;
}

/** Small "as of <date>" label plus an amber/red pill when the metric is stale for its cadence. */
function VintagePill({ asOf, cadenceLabel }){
  if(!asOf) return null;
  const status = vintageStatus(asOf, cadenceLabel);
  return (
    <div className="vintage">
      <span>as of {asOf}</span>
      {status && (
        <span className={`pill ${status.level === 'red' ? 'pill-fail' : 'pill-stale'}`}>
          {status.ageDays}d old
        </span>
      )}
    </div>
  );
}

function useHashRoute(){
  const [hash, setHash] = useState(() => window.location.hash || '#/latest');
  useEffect(() => {
    const onHash = () => setHash(window.location.hash || '#/latest');
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  return hash.replace(/^#/, '');
}

const WEEKDAY_ABBR = ['SUN','MON','TUE','WED','THU','FRI','SAT'];

function fmtTime(iso){
  const d = new Date(iso);
  return d.toISOString().replace('T',' ').replace(/\.\d+Z$/,'Z').slice(0,17);
}
function relTime(iso, nowOverride){
  // nowOverride: pass a Date to use a live clock (refresh button needs this).
  // Default used to fall back to a frozen mock anchor (2026-05-02T10:35:00Z) —
  // that made every real timestamp after that date read as "just now" (E3.2).
  // Callers that don't pass nowOverride now get the real live clock instead.
  const target = new Date(iso).getTime();
  const anchor = nowOverride
    ? nowOverride.getTime()
    : Date.now();
  const delta = anchor - target;
  const sec = Math.round(delta/1000);
  if(sec < 45) return 'just now';
  const m = Math.round(delta/60000);
  if(m < 60) return `${m}m ago`;
  const h = Math.round(m/60);
  if(h < 24) return `${h}h ago`;
  const d = Math.round(h/24);
  return `${d}d ago`;
}
function fmtPct(p){
  if(p == null) return '—';
  const sign = p>=0 ? '+' : '';
  return `${sign}${(p*100).toFixed(2)}%`;
}

function Sidebar({ route }){
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem('ed_sidebar_collapsed') === '1'; } catch(e){ return false; }
  });
  const [theme, setTheme] = useState(() => {
    try { return localStorage.getItem('ed_theme') || 'light'; } catch(e){ return 'light'; }
  });
  useEffect(() => {
    try { localStorage.setItem('ed_sidebar_collapsed', collapsed ? '1' : '0'); } catch(e){}
    document.body.classList.toggle('sidebar-collapsed', collapsed);
  }, [collapsed]);
  useEffect(() => {
    try { localStorage.setItem('ed_theme', theme); } catch(e){}
    document.documentElement.setAttribute('data-theme', theme);
  }, [theme]);
  const toggleTheme = () => setTheme(t => t === 'dark' ? 'light' : 'dark');
  const themeIcon = theme === 'dark' ? '☀' : '☾';
  const themeLabel = theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';

  // Live pipeline health for the footer + Sources badge — replaces the
  // hardcoded "all sources OK / last sync 2026-05-02" mock chrome (E3.2).
  // window.ED_DATA may not be populated yet on first paint; degrade to a
  // neutral "loading" state rather than claiming health we haven't checked.
  const edData = window.ED_DATA;
  const sourcesStatus = (edData && edData.bundle && edData.bundle.sources_status) || null;
  const sourceKeys = sourcesStatus ? Object.keys(sourcesStatus) : [];
  const totalSources = sourceKeys.length;
  // A long-running job (status: 'running') isn't a failure — don't count it
  // against "all OK", but don't call it OK either.
  const okCount = sourceKeys.filter(k => sourcesStatus[k].status === 'ok').length;
  const failCount = sourceKeys.filter(k => sourcesStatus[k].status === 'fail' || sourcesStatus[k].status === 'failed').length;
  const allOk = totalSources > 0 && okCount === totalSources;
  const dotClass = !sourcesStatus ? '' : failCount > 0 ? 'fail' : allOk ? '' : 'warn';
  const lastSyncIso = edData && edData.bundle && edData.bundle.updated_at;
  const lastSyncLabel = lastSyncIso
    ? new Date(lastSyncIso).toISOString().slice(0,16).replace('T',' ') + 'Z'
    : null;
  const healthLabel = !sourcesStatus
    ? 'loading…'
    : allOk ? 'all sources OK' : `${okCount}/${totalSources} sources OK`;
  const footTitle = sourcesStatus
    ? `${healthLabel} · last sync ${lastSyncLabel || '—'}`
    : 'loading…';

  const items = [
    { group: 'Pipeline', links: [
      { href:'#/latest',  label:'Latest',   badge:'live', icon:'◐' },
      { href:'#/archive', label:'Archive',  badge:'90d',  icon:'◫' },
      { href:'#/runs',    label:'Run dashboard', badge:null, icon:'▦' },
    ]},
    { group: 'Analysis', links: [
      { href:'#/macro',   label:'Macro',    badge:'14y',  icon:'≋' },
    ]},
    { group: 'Reference', links: [
      { href:'#/sources', label:'Sources',  badge: totalSources > 0 ? String(totalSources) : null,  icon:'◆' },
      { href:'#/about',   label:'About',    badge:null, icon:'§' },
    ]},
  ];

  if(collapsed){
    return (
      <aside className="sidebar collapsed">
        <button className="collapseBtn" onClick={()=>setCollapsed(false)} title="Expand sidebar" aria-label="Expand sidebar">»</button>
        <button className="themeBtn themeBtn-mini" onClick={toggleTheme} title={themeLabel} aria-label={themeLabel}>{themeIcon}</button>
        <div className="brand-mini" title="EconDelta · Pipeline">ED</div>
        <nav className="nav-mini">
          {items.flatMap(g => g.links).map(l => {
            const active = route === l.href.slice(1) || (route === '/' && l.href === '#/latest');
            return (
              <a key={l.href} href={l.href} className={active ? 'active' : ''} title={l.label} aria-label={l.label}>
                <span className="ico">{l.icon}</span>
              </a>
            );
          })}
        </nav>
        <div className="foot-mini" title={footTitle}>
          <span className={`dot ${dotClass}`}></span>
        </div>
      </aside>
    );
  }

  return (
    <aside className="sidebar">
      <button className="collapseBtn" onClick={()=>setCollapsed(true)} title="Minimize sidebar" aria-label="Minimize sidebar">«</button>
      <button className="themeBtn" onClick={toggleTheme} title={themeLabel} aria-label={themeLabel}>{themeIcon}</button>
      <div className="brand">
        <div className="mark">/// EconDelta</div>
        <div className="name">Pipeline</div>
        <div className="tag">main · v1.0.0</div>
      </div>
      <nav>
        {items.map(g => (
          <React.Fragment key={g.group}>
            <div className="group">{g.group}</div>
            {g.links.map(l => {
              const active = route === l.href.slice(1) || (route === '/' && l.href === '#/latest');
              return (
                <a key={l.href} href={l.href} className={active ? 'active' : ''}>
                  <span>{l.label}</span>
                  {l.badge && <span className="badge">{l.badge}</span>}
                </a>
              );
            })}
          </React.Fragment>
        ))}
      </nav>
      <div className="foot">
        <div><span className={`dot ${dotClass}`}></span>{healthLabel}</div>
        <div style={{marginTop:6}}>{lastSyncLabel ? `last sync ${lastSyncLabel}` : '—'}</div>
      </div>
    </aside>
  );
}

function PageHead({ kicker, title, meta }){
  return (
    <div className="pageHead">
      <div>
        <div className="kicker">{kicker}</div>
        <h1 className="title">{title}</h1>
      </div>
      {meta && <div className="meta">{meta}</div>}
    </div>
  );
}

function Sparkline({ data, w=140, h=32, stroke='var(--ink)', fill='none', strokeWidth=1.25 }){
  if(!data || data.length === 0) return null;
  const clean = data.map(v => v == null ? null : v);
  const numeric = clean.filter(v => v != null);
  if(numeric.length < 2) return null;
  const min = Math.min(...numeric);
  const max = Math.max(...numeric);
  const range = max - min || 1;
  const stepX = w / (clean.length - 1);
  let path = '';
  let pen = false;
  clean.forEach((v, i) => {
    if(v == null){ pen = false; return; }
    const x = i*stepX;
    const y = h - ((v - min)/range)*(h-2) - 1;
    path += (pen ? 'L' : 'M') + x.toFixed(1) + ',' + y.toFixed(1) + ' ';
    pen = true;
  });
  // Last value dot
  const lastIdx = clean.length-1;
  const last = clean[lastIdx];
  const lx = lastIdx*stepX;
  const ly = last == null ? null : h - ((last - min)/range)*(h-2) - 1;
  return (
    <svg className="sparkline" width={w} height={h} viewBox={`0 0 ${w} ${h}`}>
      <path d={path} fill={fill} stroke={stroke} strokeWidth={strokeWidth} strokeLinejoin="round" strokeLinecap="round"/>
      {ly != null && <circle cx={lx} cy={ly} r="1.8" fill={stroke}/>}
    </svg>
  );
}

function Drawer({ open, onClose, title, children }){
  return (
    <React.Fragment>
      <div className={`scrim ${open ? 'open' : ''}`} onClick={onClose}></div>
      <div className={`drawer ${open ? 'open' : ''}`} role="dialog" aria-hidden={!open}>
        <div className="hd">
          <div className="t">{title}</div>
          <button onClick={onClose} aria-label="Close">×</button>
        </div>
        <div className="body">{children}</div>
      </div>
    </React.Fragment>
  );
}

function StatusPill({ status }){
  // 'running' means a long-running job hasn't finished yet (e.g. media_screen,
  // which regularly takes 15+ minutes) — that's a caution, not a failure, so
  // it shares the amber 'stale' styling rather than reading as red (E3.2).
  const cls = status === 'ok' ? 'pill-ok'
            : status === 'stale' || status === 'running' ? 'pill-stale'
            : status === 'fail' || status === 'failed' ? 'pill-fail'
            : 'pill-skip';
  return <span className={`pill ${cls}`}>{status}</span>;
}

function Masthead(){
  const data = window.ED_DATA;
  // Async data load: render a minimal masthead until supabase-client populates ED_DATA.
  if (!data || !data.bundle) {
    return (
      <header className="masthead is-loading" role="banner">
        <div className="left">
          <div className="wordmark">
            <div className="top">Bangladesh · Macroeconomic Pipeline</div>
            <div className="name">Econ<span className="delta">Δ</span>elta</div>
            <div className="sub">loading…</div>
          </div>
        </div>
      </header>
    );
  }
  const d = data.bundle.data || {};
  const sources = data.bundle.sources_status || {};
  const okCount = Object.values(sources).filter(s => s.status === 'ok').length;
  const totalSrc = Object.keys(sources).length;
  const dseDelta = d.dsex_change_pct ?? 0;

  // Manual refresh — calls window.__edRefresh() if defined (Supabase mode);
  // in mock mode the helper just dispatches the event so the UI re-renders.
  const [refreshing, setRefreshing] = React.useState(false);
  const [refreshErr, setRefreshErr] = React.useState(null);
  // "now" tick so the relative timestamp updates while the page is open
  const [now, setNow] = React.useState(() => new Date());
  React.useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 30_000);
    return () => clearInterval(id);
  }, []);
  const handleRefresh = async () => {
    if(refreshing) return;
    setRefreshing(true); setRefreshErr(null);
    try {
      if(typeof window.__edRefresh === 'function'){
        await window.__edRefresh();
      } else {
        // Mock mode — just bump the timestamp + force re-render.
        window.ED_DATA.bundle.updated_at = new Date().toISOString();
        window.dispatchEvent(new CustomEvent('ed:data-changed'));
      }
    } catch(e) {
      console.error('[EconDelta] refresh failed', e);
      setRefreshErr(String(e.message || e));
    } finally {
      setRefreshing(false);
    }
  };
  const lastRefresh = (() => {
    try { return new Date(data.bundle.updated_at); } catch { return null; }
  })();
  const lastRefreshRel = lastRefresh ? relTime(lastRefresh, now) : '—';

  // Oldest metric in the payload — "snapshot fetched" only tells you when the
  // PWA last talked to Supabase, not whether the data underneath is fresh.
  // Surface the single stalest metric_id so a frozen source can't hide behind
  // a live "fetched Xm ago" stamp (E3.2).
  const oldestMetric = (() => {
    const values = (data.dashboard && data.dashboard.values) || {};
    let oldest = null;
    Object.entries(values).forEach(([metricId, v]) => {
      if (!v || !v.as_of) return;
      if (!oldest || v.as_of < oldest.as_of) oldest = { metricId, as_of: v.as_of };
    });
    return oldest;
  })();
  const oldestAgeDays = oldestMetric
    ? Math.floor((now.getTime() - new Date(oldestMetric.as_of + 'T00:00:00Z').getTime()) / 86400000)
    : null;

  const tape = (data.tickers || []).filter(t => t.delta != null);
  // Repeat tape items twice for seamless loop
  const tapeRow = (
    <React.Fragment>
      {tape.map((t,i) => (
        <span key={'a'+i} className="tk">
          <b>{t.label.toUpperCase().replace(/\s+\/\s+/g,'/')}</b>
          <span>{t.fmt(t.val)}</span>
          <span className={t.delta >= 0 ? 'up' : 'dn'}>{t.delta >= 0 ? '▲' : '▼'} {(t.delta*100).toFixed(2)}%</span>
        </span>
      ))}
      {tape.map((t,i) => (
        <span key={'b'+i} className="tk">
          <b>{t.label.toUpperCase().replace(/\s+\/\s+/g,'/')}</b>
          <span>{t.fmt(t.val)}</span>
          <span className={t.delta >= 0 ? 'up' : 'dn'}>{t.delta >= 0 ? '▲' : '▼'} {(t.delta*100).toFixed(2)}%</span>
        </span>
      ))}
    </React.Fragment>
  );

  return (
    <React.Fragment>
      <header className="masthead" role="banner">
        <div className="left">
          <div className="wordmark">
            <div className="top">Bangladesh · Macroeconomic Pipeline</div>
            <div className="name">Econ<span className="delta">Δ</span>elta</div>
            <div className="sub">"All the data the brief requires."</div>
          </div>
        </div>

        <div className="center">
          <div className="strip">
            <div className="cell">
              <div className="lbl">USD / BDT</div>
              <div className="val">{(d.usd_bdt_mid ?? 0).toFixed(2)}<span className="unit">WAR</span></div>
            </div>
            <div className="cell">
              <div className="lbl">DSEX</div>
              <div className={"val " + (dseDelta >= 0 ? 'ok' : 'accent')}>
                {(d.dsex ?? 0).toFixed(0)}<span className="unit">{dseDelta >= 0 ? '+' : ''}{dseDelta.toFixed(2)}%</span>
              </div>
            </div>
            <div className="cell">
              <div className="lbl">Brent</div>
              <div className="val">${(d.brent_crude_usd_barrel ?? 0).toFixed(2)}<span className="unit">/bbl</span></div>
            </div>
            <div className="cell">
              <div className="lbl">Reserves</div>
              <div className="val">{(d.gross_reserves_usd_bn ?? 0).toFixed(2)}<span className="unit">USD bn</span></div>
            </div>
            <div className="cell">
              <div className="lbl">Pipeline</div>
              <div className="val ok">{okCount}/{totalSrc}<span className="unit">sources OK</span></div>
            </div>
          </div>
        </div>

        <div className="right">
          {/* Was hardcoded "Vol. 1, No. 122 / 2026-05-02 SAT / 10:35 UTC" mock
              chrome — now the real live clock (E3.2). */}
          <div className="issue">Live · {WEEKDAY_ABBR[now.getUTCDay()]}</div>
          <div className="date">{now.toISOString().slice(0,10)}</div>
          <div className="smol">{now.toISOString().slice(11,16)} UTC · v1.0.0</div>
          <div className="refreshRow">
            <button
              type="button"
              className={"refreshBtn" + (refreshing ? ' is-spinning' : '')}
              onClick={handleRefresh}
              disabled={refreshing}
              aria-label="Refresh data"
              title={lastRefresh ? `Last refresh: ${lastRefresh.toISOString()}` : 'Refresh'}
            >
              <svg className="refreshIcon" viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">
                <path d="M2.5 8a5.5 5.5 0 0 1 9.4-3.9M13.5 8a5.5 5.5 0 0 1-9.4 3.9" stroke="currentColor" strokeWidth="1.4" fill="none" strokeLinecap="round"/>
                <path d="M11.5 2v3h-3M4.5 14v-3h3" stroke="currentColor" strokeWidth="1.4" fill="none" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
              <span>{refreshing ? 'refreshing…' : 'refresh'}</span>
            </button>
            {/* "updated" -> "snapshot fetched": this timestamp is when the PWA
                last talked to Supabase, not when the underlying data changed
                (E3.2). The oldest-metric line below is the actual freshness
                signal — a fetch can succeed every 30s while a source has been
                silently frozen for months. */}
            <span className="refreshAge" title={lastRefresh ? lastRefresh.toISOString() : ''}>
              {refreshErr ? `failed · ${refreshErr.slice(0,40)}` : `snapshot fetched ${lastRefreshRel}`}
            </span>
            {oldestMetric && (
              <span className="refreshAge" title={`${oldestMetric.metricId} · as of ${oldestMetric.as_of}`}>
                · oldest metric {oldestAgeDays}d old ({oldestMetric.metricId})
              </span>
            )}
          </div>
        </div>
      </header>
      <div className="tape" aria-hidden="true">
        <div className="tape-inner">{tapeRow}</div>
      </div>
    </React.Fragment>
  );
}

Object.assign(window, { useHashRoute, Sidebar, PageHead, Sparkline, Drawer, StatusPill, fmtTime, relTime, fmtPct, SOURCE_LABELS, Masthead, CADENCE_DAYS, vintageStatus, VintagePill });
