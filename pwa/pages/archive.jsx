function PageArchive(){
  const data = window.ED_DATA;
  if(!data || !data.history){
    return <div className="loading">no archive data yet…</div>;
  }

  // Group history rows by date for a date-major table view.
  const byDate = {};
  data.history.forEach(r => {
    if(!byDate[r.as_of]) byDate[r.as_of] = [];
    byDate[r.as_of].push(r);
  });
  const dates = Object.keys(byDate).sort().reverse();

  return (
    <React.Fragment>
      <PageHead
        kicker="Pipeline · 90-day window"
        title="Archive"
        meta={<div><b>days</b> {dates.length} &nbsp;<b>rows</b> {data.history.length}</div>}
      />
      <p className="sec-lede">Daily snapshots from <code>metric_history</code>. Most recent first.</p>
      <div className="archive-list">
        {dates.map(date => (
          <details key={date} className="archive-day">
            <summary>
              <span className="tnum">{date}</span>
              <span className="muted"> · {byDate[date].length} indicators</span>
            </summary>
            <div className="archive-rows">
              {byDate[date].map((r, i) => (
                <div key={i} className="archive-row">
                  <span>{r.metric_id}</span>
                  <span className="tnum">{r.value == null ? '—' : Number(r.value).toLocaleString()}</span>
                </div>
              ))}
            </div>
          </details>
        ))}
      </div>
    </React.Fragment>
  );
}

window.PageArchive = PageArchive;
