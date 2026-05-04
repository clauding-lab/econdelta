// Latest page — terminal-style ticker grid + sparklines, ported from bundle.
// Reads window.ED_DATA: tickers, tickerGroups, bundle.{data,sources_status,updated_at}.
const { useState: useStateL } = React;

function PageLatest(){
  const data = window.ED_DATA;
  if(!data || !data.bundle){
    return <div className="loading">no data yet…</div>;
  }

  const groups = data.tickerGroups || [];
  const sources = data.bundle.sources_status || {};
  const sourceKeys = Object.keys(sources).sort();

  // Pipeline-state summary for the header meta block.
  const allOk = sourceKeys.length > 0 && sourceKeys.every(k => sources[k].status === 'ok');
  const firstNonOk = sourceKeys.find(k => sources[k].status !== 'ok');
  const stateLabel = allOk
    ? <React.Fragment><StatusPill status="ok"/> all sources</React.Fragment>
    : firstNonOk
      ? <React.Fragment><StatusPill status={sources[firstNonOk].status}/> {firstNonOk}</React.Fragment>
      : <React.Fragment><StatusPill status="skip"/> no sources</React.Fragment>;

  const updatedAt = data.bundle.updated_at
    ? new Date(data.bundle.updated_at).toISOString().slice(0,19).replace('T',' ') + ' UTC'
    : '—';

  const flat = data.bundle.data || {};
  const showBreadth = flat.advancing != null && flat.declining != null && flat.unchanged != null;

  return (
    <React.Fragment>
      <PageHead
        kicker="Pipeline · canonical snapshot"
        title="Latest"
        meta={
          <React.Fragment>
            <div><b>updated</b>&nbsp; {updatedAt}</div>
            <div><b>state</b>&nbsp;&nbsp;&nbsp; {stateLabel}</div>
          </React.Fragment>
        }
      />

      <div style={{display:'flex',gap:14,marginBottom:18,fontFamily:'IBM Plex Mono, monospace',fontSize:11,color:'var(--ink-3)',flexWrap:'wrap'}}>
        {sourceKeys.map(src => {
          const s = sources[src] || {};
          return (
            <div key={src}>
              <span className="muted">{src}</span> &nbsp;
              <StatusPill status={s.status}/> &nbsp;
              <span className="tnum">{s.last_success ? relTime(s.last_success) : '—'}</span>
            </div>
          );
        })}
      </div>

      {groups.map(g => {
        const gTickers = (data.tickers || []).filter(t => t.group === g.key);
        if(gTickers.length === 0) return null;
        return (
          <React.Fragment key={g.key}>
            <h2 className="sec">{g.key}</h2>
            <p className="sec-lede">{g.lede}</p>
            <div className="tickers">
              {gTickers.map(t => (
                <div className="ticker" key={t.key}>
                  <div className="lbl">{t.label}</div>
                  <div className="val tnum">
                    {t.val == null ? '—' : t.fmt(t.val)}
                    <span className="unit">{t.unit}</span>
                  </div>
                  {t.delta != null && (
                    <div className={`delta ${t.delta > 0.0001 ? 'up' : t.delta < -0.0001 ? 'down' : 'flat'}`}>
                      {t.delta > 0 ? '▲' : t.delta < 0 ? '▼' : '·'} {fmtPct(t.delta)} &nbsp;<span className="muted">d/d</span>
                    </div>
                  )}
                  {t.delta == null && <div className="delta flat">·  no Δ</div>}
                  {t.spark && (
                    <div className="spark">
                      <Sparkline
                        data={t.spark}
                        w={180}
                        h={36}
                        stroke={t.delta != null && t.delta < 0 ? 'var(--accent)' : 'var(--ok)'}
                      />
                    </div>
                  )}
                </div>
              ))}
            </div>
          </React.Fragment>
        );
      })}

      {showBreadth && (
        <React.Fragment>
          <h2 className="sec">Market breadth — most recent trading day</h2>
          <p className="sec-lede">
            {flat.advancing} advancing · {flat.declining} declining · {flat.unchanged} unchanged.
            {flat.turnover_crore != null && ` Turnover ${Number(flat.turnover_crore).toFixed(2)} crore`}
            {flat.total_trades != null && ` on ${Number(flat.total_trades).toLocaleString()} trades`}
            {(flat.turnover_crore != null || flat.total_trades != null) && '.'}
          </p>
          <BreadthBar adv={flat.advancing} dec={flat.declining} unc={flat.unchanged}/>
        </React.Fragment>
      )}

      <h2 className="sec">Raw payload</h2>
      <p className="sec-lede">The flat object the downstream agent reads.</p>
      <pre style={{fontFamily:'IBM Plex Mono, monospace',fontSize:11.5,background:'var(--code-bg)',color:'var(--code-ink)',padding:'14px 16px',border:'1px solid var(--ink)',overflow:'auto',margin:0,borderLeft:'3px solid var(--accent)'}}>
{JSON.stringify(flat, null, 2)}
      </pre>
    </React.Fragment>
  );
}

function BreadthBar({ adv, dec, unc }){
  const total = adv + dec + unc;
  const pct = x => (x/total)*100;
  return (
    <div style={{background:'var(--paper)',border:'1px solid var(--rule)',padding:'18px 22px',marginBottom:18}}>
      <div style={{display:'flex',height:22,borderRadius:2,overflow:'hidden',marginBottom:10,fontFamily:'IBM Plex Mono, monospace',fontSize:10,color:'#fff'}}>
        <div style={{width:`${pct(adv)}%`,background:'var(--ok)',display:'flex',alignItems:'center',justifyContent:'center'}}>{adv} adv</div>
        <div style={{width:`${pct(unc)}%`,background:'var(--skip)',display:'flex',alignItems:'center',justifyContent:'center'}}>{unc} unc</div>
        <div style={{width:`${pct(dec)}%`,background:'var(--accent)',display:'flex',alignItems:'center',justifyContent:'center'}}>{dec} dec</div>
      </div>
      <div style={{display:'flex',justifyContent:'space-between',fontFamily:'IBM Plex Mono, monospace',fontSize:11,color:'var(--ink-3)'}}>
        <span><b style={{color:'var(--ok)'}}>{pct(adv).toFixed(1)}%</b> advancing</span>
        <span><b style={{color:'var(--ink-3)'}}>{pct(unc).toFixed(1)}%</b> unchanged</span>
        <span><b style={{color:'var(--accent)'}}>{pct(dec).toFixed(1)}%</b> declining</span>
      </div>
    </div>
  );
}

window.PageLatest = PageLatest;
