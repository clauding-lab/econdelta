// Runs page — GitHub-style commit graph per source. Ported from bundle.
// Sources are driven by run_logs.source; cadence labels live in CADENCES below.
const { useState: useStateR } = React;

// Per-source cadence label shown next to the commit graph.
// Add a row when a new scraper lands.
const CADENCES = {
  fetch:            '05:00 BDT daily',
  bb_forex:         '05:05 BDT daily (+ 06:00 retry)',
  commodity_prices: '05:08 BDT daily',
  dse_market:       '05:11 BDT trading days',
  parse:            '05:16 BDT daily (+ 05:55 retry)',
  aggregate:        '05:20 BDT daily (+ 06:10 retry)',
};

function PageRuns(){
  const data = window.ED_DATA;
  if(!data || !data.runs){
    return <div className="loading">no runs data yet…</div>;
  }
  const [filter, setFilter] = useStateR('all');
  const [sel, setSel] = useStateR(null);

  const allSources = Object.keys(data.runs).sort();
  // Cards only for sources that actually have runs — avoids empty cards.
  const sourcesWithRuns = allSources.filter(k => (data.runs[k] || []).length > 0);

  const summary = sourcesWithRuns.map(k => {
    const rs = data.runs[k] || [];
    const total = rs.length;
    const ok = rs.filter(r=>r.status==='ok').length;
    const fail = rs.filter(r=>r.status==='fail').length;
    const stale = rs.filter(r=>r.status==='stale').length;
    const skip = rs.filter(r=>r.status==='skip').length;
    const okEligible = total - skip;
    const uptime = okEligible ? (ok/okEligible)*100 : 100;
    return { k, total, ok, fail, stale, skip, uptime };
  });

  const failures = allSources
    .flatMap(k => (data.runs[k] || []).filter(r=>r.status==='fail' || r.status==='stale').map(r=>({...r,src:k})))
    .sort((a,b)=> a.date < b.date ? 1 : -1);

  return (
    <React.Fragment>
      <PageHead kicker="Pipeline · operational health" title="Run dashboard"
        meta={<React.Fragment>
          <div><b>window</b>&nbsp;&nbsp; last 90 days</div>
          <div><b>sources</b>&nbsp; {allSources.length} instrumented</div>
          <div><b>alerting</b>&nbsp; Discord webhook</div>
        </React.Fragment>}
      />

      {summary.length > 0 && (
        <div className="cards" style={{marginBottom:24}}>
          {summary.map(s => (
            <div className="card" key={s.k}>
              <div className="h">{SOURCE_LABELS[s.k] || s.k}</div>
              <div style={{display:'flex',justifyContent:'space-between',alignItems:'baseline',marginBottom:8}}>
                <div style={{fontFamily:'IBM Plex Serif, serif',fontSize:30,fontWeight:600}} className="tnum">{s.uptime.toFixed(1)}%</div>
                <div style={{fontFamily:'IBM Plex Mono, monospace',fontSize:10,color:'var(--ink-3)'}}>uptime</div>
              </div>
              <div style={{display:'flex',gap:14,fontFamily:'IBM Plex Mono, monospace',fontSize:11,color:'var(--ink-2)'}}>
                <span><b style={{color:'var(--ok)'}}>{s.ok}</b> ok</span>
                <span><b style={{color:'var(--warn)'}}>{s.stale}</b> stale</span>
                <span><b style={{color:'var(--fail)'}}>{s.fail}</b> fail</span>
                {s.skip>0 && <span><b style={{color:'var(--ink-3)'}}>{s.skip}</b> skip</span>}
              </div>
            </div>
          ))}
        </div>
      )}

      <h2 className="sec">Daily run graph</h2>
      <p className="sec-lede">One square per day, oldest at left. Hover for date + status; click to drill in.</p>

      <div className="filters">
        <span style={{fontFamily:'IBM Plex Mono, monospace',color:'var(--ink-3)'}}>FILTER</span>
        <div className="btn-grp">
          {['all','ok','fail','stale','skip'].map(f =>
            <button key={f} onClick={()=>setFilter(f)} className={filter===f?'on':''}>{f}</button>
          )}
        </div>
      </div>

      {allSources.map(k => (
        <SourceRunGraph
          key={k}
          srcKey={k}
          runs={data.runs[k] || []}
          filter={filter}
          onSelect={(r)=>setSel({...r,src:k})}
        />
      ))}

      <h2 className="sec">Failure log</h2>
      <p className="sec-lede">All non-OK runs in the window, most recent first.</p>
      {failures.length === 0 ? (
        <p style={{fontFamily:'IBM Plex Serif, serif',fontStyle:'italic',color:'var(--ink-3)',marginLeft:44}}>No failures or stale runs in the window. Pipeline is healthy.</p>
      ) : (
        <table className="tbl">
          <thead>
            <tr><th>Date</th><th>Source</th><th>Status</th><th>Duration</th><th>Error</th></tr>
          </thead>
          <tbody>
            {failures.slice(0,30).map((r,i) => (
              <tr key={i} className="clickable" onClick={()=>setSel(r)}>
                <td className="num">{r.date}</td>
                <td>{SOURCE_LABELS[r.src] || r.src}</td>
                <td><StatusPill status={r.status}/></td>
                <td className="num muted">{r.durationMs != null ? (r.durationMs/1000).toFixed(1) + 's' : '—'}</td>
                <td className="muted">{r.error}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <Drawer open={!!sel} onClose={()=>setSel(null)} title={sel ? `${SOURCE_LABELS[sel.src] || sel.src} · ${sel.date}` : ''}>
        {sel && (
          <React.Fragment>
            <div style={{marginBottom:14}}>
              <StatusPill status={sel.status}/>
              <span style={{marginLeft:10,fontFamily:'IBM Plex Mono, monospace',fontSize:11,color:'var(--ink-3)'}}>
                {sel.startedAt ? sel.startedAt.replace('T',' ').slice(0,19) + 'Z' : '—'}
                {sel.durationMs != null && ` · ${(sel.durationMs/1000).toFixed(1)}s`}
                {sel.attempt != null && ` · attempt ${sel.attempt}`}
              </span>
            </div>
            {sel.error && (
              <div style={{background: sel.status==='fail'?'var(--accent-bg)':'var(--warn-bg)', color: sel.status==='fail'?'var(--accent)':'var(--warn)', padding:'10px 12px',fontFamily:'IBM Plex Mono, monospace',fontSize:11.5,borderLeft:`2px solid ${sel.status==='fail'?'var(--accent)':'var(--warn)'}`,marginBottom:14}}>
                {sel.error}
              </div>
            )}
            <div style={{fontFamily:'IBM Plex Mono, monospace',fontSize:10,letterSpacing:'.14em',textTransform:'uppercase',color:'var(--ink-3)',marginBottom:6}}>journal</div>
            <pre>{`$ journalctl -u econdelta-${sel.src} --since "${sel.date}"
${sel.startedAt || ''} INFO ${sel.src}: starting
${sel.startedAt || ''} INFO ${sel.src}: cadence=${CADENCES[sel.src] || 'on demand'}
${sel.status==='fail' ? `${sel.startedAt || ''} ERROR ${sel.src}: ${sel.error || ''}\n${sel.startedAt || ''} INFO  notifier: posted Discord alert (level=error)\nexit code: 1`
  : sel.status==='stale' ? `${sel.startedAt || ''} WARNING ${sel.src}: ${sel.error || ''}\n${sel.startedAt || ''} INFO  notifier: posted Discord alert (level=warning)\nexit code: 2 (write skipped)`
  : `${sel.startedAt || ''} INFO ${sel.src}: snapshot written\nexit code: 0`}`}</pre>
          </React.Fragment>
        )}
      </Drawer>
    </React.Fragment>
  );
}

function SourceRunGraph({ srcKey, runs, filter, onSelect }){
  const ok = runs.filter(r=>r.status==='ok').length;
  const total = runs.length;
  const [tip, setTip] = React.useState(null);
  const cadence = CADENCES[srcKey] || 'on demand';

  return (
    <div className="runwrap">
      <div className="topline">
        <div className="nm">{SOURCE_LABELS[srcKey] || srcKey}</div>
        <div className="stats">
          <span>last 90d &nbsp; <b>{ok}</b>/{total} ok</span>
          &nbsp;·&nbsp; cadence {cadence}
        </div>
      </div>
      {total === 0 ? (
        <div style={{padding:'18px 0',fontFamily:'IBM Plex Serif, serif',fontStyle:'italic',color:'var(--ink-3)'}}>
          no runs yet — first fire scheduled at {cadence}
        </div>
      ) : (
        <div className="commitgrid">
          {runs.map((r,i) => {
            const dim = filter !== 'all' && filter !== r.status;
            return (
              <div key={i}
                className={`sq ${r.status}`}
                style={dim ? {opacity:0.18} : {}}
                onMouseEnter={(e)=>setTip({x:e.clientX,y:e.clientY,r})}
                onMouseMove={(e)=>setTip({x:e.clientX,y:e.clientY,r})}
                onMouseLeave={()=>setTip(null)}
                onClick={()=>onSelect(r)}
              />
            );
          })}
        </div>
      )}
      <div className="legend-row">
        <div className="li"><span className="sq" style={{background:'var(--ok-2)'}}></span>ok</div>
        <div className="li"><span className="sq" style={{background:'var(--warn)'}}></span>stale (anomaly)</div>
        <div className="li"><span className="sq" style={{background:'var(--fail)'}}></span>fail</div>
        <div className="li"><span className="sq" style={{background:'var(--skip)'}}></span>skip (non-trading)</div>
      </div>
      {tip && (
        <div className="tt" style={{left: tip.x, top: tip.y}}>
          {tip.r.date} · {tip.r.status} · {tip.r.durationMs != null ? (tip.r.durationMs/1000).toFixed(1) + 's' : '—'}
          {tip.r.error ? ' · ' + tip.r.error.slice(0,40) : ''}
        </div>
      )}
    </div>
  );
}

window.PageRuns = PageRuns;
