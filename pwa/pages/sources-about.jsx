function PageSources(){
  const data = window.ED_DATA && window.ED_DATA.dashboard;
  if(!data){
    return <div className="loading">no sources data yet…</div>;
  }
  // Group definitions by source.
  const bySource = {};
  (data.definitions || []).forEach(def => {
    const src = def.source || 'other';
    if(!bySource[src]) bySource[src] = [];
    bySource[src].push(def);
  });
  const sources = Object.keys(bySource).sort();

  return (
    <React.Fragment>
      <PageHead
        kicker="Pipeline · provenance"
        title="Sources"
        meta={<div><b>sources</b> {sources.length}</div>}
      />
      <p className="sec-lede">Where each indicator originates.</p>
      {sources.map(src => (
        <section key={src} className="source-section">
          <h3>{src}</h3>
          <div className="source-indicators">
            {bySource[src].map(def => (
              <div key={def.metric_id} className="source-row">
                <span><b>{def.label}</b> <span className="muted">{def.metric_id}</span></span>
                {def.source_url && <a href={def.source_url} target="_blank" rel="noopener">source ↗</a>}
              </div>
            ))}
          </div>
        </section>
      ))}
    </React.Fragment>
  );
}

function PageAbout(){
  return (
    <React.Fragment>
      <PageHead
        kicker="Pipeline · about"
        title="EconDelta"
        meta={<div><b>repo</b> clauding-lab/econdelta</div>}
      />
      <p>EconDelta is a deterministic Bangladesh macro data pipeline. Three layers:</p>
      <ol>
        <li><b>Backend</b>: Python scrapers + parsers + aggregator on ExonVPS (BDIX-Dhaka). Daily systemd cascade between 05:00 and 05:20 BDT.</li>
        <li><b>Data layer</b>: Supabase (Postgres) — three tables (<code>metric_history</code>, <code>metric_definitions</code>, <code>run_logs</code>) plus the <code>get_latest_dashboard()</code> RPC.</li>
        <li><b>Frontend</b>: this PWA — vanilla React, no build step, deployed via GitHub Pages.</li>
      </ol>
      <p><b>License:</b> source code at <a href="https://github.com/clauding-lab/econdelta">github.com/clauding-lab/econdelta</a>. Data is for informational use only — verify against original sources before any operational decision.</p>
    </React.Fragment>
  );
}

window.PageSources = PageSources;
window.PageAbout = PageAbout;
