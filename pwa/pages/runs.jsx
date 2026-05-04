function PageRuns(){
  const data = window.ED_DATA;
  if(!data || !data.runs){
    return <div className="loading">no runs data yet…</div>;
  }
  const sources = Object.keys(data.runs).sort();

  return (
    <React.Fragment>
      <PageHead
        kicker="Pipeline · 90-day audit"
        title="Runs"
        meta={<div><b>sources</b> {sources.length} &nbsp;<b>total</b> {sources.reduce((s, k) => s + data.runs[k].length, 0)}</div>}
      />
      <p className="sec-lede">Each cell = one scraper invocation. Hover for details.</p>
      {sources.map(src => (
        <CommitGraph key={src} source={src} runs={data.runs[src]}/>
      ))}
    </React.Fragment>
  );
}

function CommitGraph({source, runs}){
  // 90-day grid: 13 weeks × 7 days. Map each run to its day.
  const today = new Date();
  const cells = [];
  for(let i = 89; i >= 0; i--){
    const d = new Date(today.getTime() - i*24*3600*1000);
    const ds = d.toISOString().slice(0,10);
    const dayRuns = runs.filter(r => r.date === ds);
    cells.push({ date: ds, runs: dayRuns });
  }

  const statusColor = (status) => ({
    ok: 'var(--ok, #6abf6e)',
    fail: 'var(--accent, #c34a1f)',
    stale: 'var(--warn, #a36a14)',
    skip: 'var(--ink-3, #6b7480)',
  })[status] || 'var(--rule, #2a2f33)';

  return (
    <div className="commit-graph-wrap">
      <h3>{source}</h3>
      <div className="commit-graph">
        {cells.map((c, i) => {
          // Pick worst status if multiple runs in a day.
          const worstStatus = c.runs.reduce((acc, r) => {
            const order = {fail: 0, stale: 1, skip: 2, ok: 3};
            return order[r.status] < order[acc] ? r.status : acc;
          }, 'ok');
          const color = c.runs.length === 0 ? 'transparent' : statusColor(worstStatus);
          const title = c.runs.length === 0
            ? `${c.date} · no run`
            : `${c.date} · ${c.runs.length} runs · ${worstStatus}`;
          return (
            <div
              key={i}
              className="cg-cell"
              style={{background: color, border: c.runs.length === 0 ? '1px solid var(--rule, #2a2f33)' : 'none'}}
              title={title}
            />
          );
        })}
      </div>
    </div>
  );
}

window.PageRuns = PageRuns;
