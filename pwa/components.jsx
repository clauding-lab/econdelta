// EconDelta — shared UI components (sidebar, sparkline, drawer, etc.)
const { useState, useEffect, useRef, useMemo } = React;

const SOURCE_LABELS = {
  bb_forex: 'BB forex',
  dse_market: 'DSE market',
  commodity_prices: 'Commodities',
};

function useHashRoute(){
  const [hash, setHash] = useState(() => window.location.hash || '#/latest');
  useEffect(() => {
    const onHash = () => setHash(window.location.hash || '#/latest');
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  return hash.replace(/^#/, '');
}

function fmtTime(iso){
  const d = new Date(iso);
  return d.toISOString().replace('T',' ').replace(/\.\d+Z$/,'Z').slice(0,17);
}
function relTime(iso, nowOverride){
  // nowOverride: pass a Date to use a live clock (refresh button needs this).
  // Default falls back to the mock anchor so existing callers keep working.
  const target = new Date(iso).getTime();
  const anchor = nowOverride
    ? nowOverride.getTime()
    : new Date('2026-05-02T10:35:00Z').getTime();
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

  const items = [
    { group: 'Pipeline', links: [
      { href:'#/latest',  label:'Latest',   badge:'live', icon:'◐' },
      { href:'#/archive', label:'Archive',  badge:'90d',  icon:'◫' },
      { href:'#/runs',    label:'Run dashboard', badge:null, icon:'▦' },
    ]},
    { group: 'Reference', links: [
      { href:'#/sources', label:'Sources',  badge:'4',  icon:'◆' },
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
        <div className="foot-mini" title="all sources OK · last sync 10:35Z">
          <span className="dot"></span>
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
        <div className="tag">main · 928b569 · v0.1</div>
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
        <div><span className="dot"></span>all sources OK</div>
        <div style={{marginTop:6}}>last sync 2026-05-02 10:35Z</div>
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
  const cls = status === 'ok' ? 'pill-ok'
            : status === 'stale' ? 'pill-stale'
            : status === 'fail' || status === 'failed' ? 'pill-fail'
            : 'pill-skip';
  return <span className={`pill ${cls}`}>{status}</span>;
}

function Masthead(){
  const data = window.ED_DATA;
  const d = data.bundle.data;
  const sources = data.bundle.sources_status;
  const okCount = Object.values(sources).filter(s => s.status === 'ok').length;
  const totalSrc = Object.keys(sources).length;
  const dseDelta = d.dsex_change_pct;

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
  const tape = data.tickers.filter(t => t.delta != null);
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
          <svg className="mark-svg" viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
            {/* Frame */}
            <rect x="2" y="2" width="60" height="60" stroke="currentColor" strokeWidth="1.5" fill="none"/>
            <rect x="6" y="6" width="52" height="52" stroke="currentColor" strokeWidth="0.5" fill="none" opacity="0.4"/>
            {/* Delta triangle (Greek Δ) — centerpiece */}
            <path d="M32 14 L52 50 L12 50 Z" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinejoin="round"/>
            {/* Inner data line — a price chart */}
            <path d="M14 44 L20 38 L26 41 L32 30 L38 35 L44 26 L50 32" stroke="var(--accent)" strokeWidth="1.5" fill="none" strokeLinejoin="round" strokeLinecap="round"/>
            {/* Data points */}
            <circle cx="14" cy="44" r="1.5" fill="var(--accent)"/>
            <circle cx="32" cy="30" r="1.5" fill="var(--accent)"/>
            <circle cx="50" cy="32" r="1.5" fill="var(--accent)"/>
            {/* Crosshair */}
            <line x1="32" y1="6" x2="32" y2="10" stroke="currentColor" strokeWidth="1"/>
            <line x1="32" y1="54" x2="32" y2="58" stroke="currentColor" strokeWidth="1"/>
            <line x1="6" y1="32" x2="10" y2="32" stroke="currentColor" strokeWidth="1"/>
            <line x1="54" y1="32" x2="58" y2="32" stroke="currentColor" strokeWidth="1"/>
          </svg>
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
              <div className="val">{d.usd_bdt_mid.toFixed(2)}<span className="unit">WAR</span></div>
            </div>
            <div className="cell">
              <div className="lbl">DSEX</div>
              <div className={"val " + (dseDelta >= 0 ? 'ok' : 'accent')}>
                {d.dsex.toFixed(0)}<span className="unit">{dseDelta >= 0 ? '+' : ''}{dseDelta.toFixed(2)}%</span>
              </div>
            </div>
            <div className="cell">
              <div className="lbl">Brent</div>
              <div className="val">${d.brent_crude_usd_barrel.toFixed(2)}<span className="unit">/bbl</span></div>
            </div>
            <div className="cell">
              <div className="lbl">Reserves</div>
              <div className="val">{d.gross_reserves_usd_bn.toFixed(2)}<span className="unit">USD bn</span></div>
            </div>
            <div className="cell">
              <div className="lbl">Pipeline</div>
              <div className="val ok">{okCount}/{totalSrc}<span className="unit">sources OK</span></div>
            </div>
          </div>
        </div>

        <div className="right">
          <div className="issue">Vol. 1, No. 122</div>
          <div className="date">2026-05-02 SAT</div>
          <div className="smol">10:35 UTC · v0.1</div>
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
            <span className="refreshAge" title={lastRefresh ? lastRefresh.toISOString() : ''}>
              {refreshErr ? `failed · ${refreshErr.slice(0,40)}` : `updated ${lastRefreshRel}`}
            </span>
          </div>
        </div>
      </header>
      <div className="tape" aria-hidden="true">
        <div className="tape-inner">{tapeRow}</div>
      </div>
    </React.Fragment>
  );
}

Object.assign(window, { useHashRoute, Sidebar, PageHead, Sparkline, Drawer, StatusPill, fmtTime, relTime, fmtPct, SOURCE_LABELS, Masthead });
