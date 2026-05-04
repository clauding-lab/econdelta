// Archive page — per-source timeline, trend chart, snapshots table.
// Ported from bundle's page-archive.jsx; adapted to read 90-day series from
// window.ED_DATA.series (no raw snapshot JSON is stored in Supabase).
const { useState: useStateA, useMemo: useMemoA } = React;

// Source → metric tabs. Keys must match metric_history.metric_id values that
// supabase-client.js loads. Add rows here when a new scraper lands.
const METRICS_BY_SRC = {
  bb_forex: [
    { k: 'usd_bdt_mid',          label: 'USD/BDT (WAR)',     fmt: v => v.toFixed(4) },
    { k: 'eur_bdt',              label: 'EUR/BDT',           fmt: v => v.toFixed(4) },
    { k: 'gbp_bdt',              label: 'GBP/BDT',           fmt: v => v.toFixed(4) },
    { k: 'gross_reserves_usd_bn',label: 'Reserves (USD bn)', fmt: v => v.toFixed(2) },
  ],
  dse_market: [
    { k: 'dsex', label: 'DSEX', fmt: v => v.toFixed(2) },
    { k: 'ds30', label: 'DS30', fmt: v => v.toFixed(2) },
    { k: 'dses', label: 'DSES', fmt: v => v.toFixed(2) },
  ],
  commodity_prices: [
    { k: 'brent_crude_usd_barrel', label: 'Brent (USD/bbl)', fmt: v => v.toFixed(2) },
    { k: 'wti_crude_usd_barrel',   label: 'WTI (USD/bbl)',   fmt: v => v.toFixed(2) },
    { k: 'gold_usd_oz',            label: 'Gold (USD/oz)',   fmt: v => v.toFixed(2) },
  ],
  aggregate: [
    { k: 'point_to_point_inflation', label: 'CPI YoY',     fmt: v => v.toFixed(2) + '%' },
    { k: 'gross_npl_ratio',          label: 'NPL Ratio',   fmt: v => v.toFixed(2) + '%' },
    { k: 'broad_money',              label: 'Broad Money', fmt: v => Number(v).toLocaleString() },
  ],
};

function PageArchive(){
  const data = window.ED_DATA;
  if(!data || !data.runs){
    return <div className="loading">no archive data yet…</div>;
  }

  // Build source list dynamically from run_logs. Keep a stable order so the
  // initial selection doesn't flip between renders.
  const sources = Object.keys(data.runs)
    .filter(k => METRICS_BY_SRC[k])  // only sources we have a metric tab map for
    .sort()
    .map(k => ({ k, label: SOURCE_LABELS[k] || k }));

  // Default to bb_forex if available, else first source with metric tabs.
  const defaultSrc = sources.find(s => s.k === 'bb_forex')?.k
                  || (sources[0] && sources[0].k)
                  || 'aggregate';

  const [src, setSrc] = useStateA(defaultSrc);
  const [metric, setMetric] = useStateA(METRICS_BY_SRC[defaultSrc][0].k);
  const [selDate, setSelDate] = useStateA(null);

  // Reset metric whenever source changes.
  React.useEffect(() => {
    const list = METRICS_BY_SRC[src];
    if(list && list.length) setMetric(list[0].k);
  }, [src]);

  if(sources.length === 0){
    return (
      <React.Fragment>
        <PageHead kicker="Pipeline · historical snapshots" title="Archive"/>
        <p className="sec-lede">No instrumented sources have logged runs yet.</p>
      </React.Fragment>
    );
  }

  const metricList = METRICS_BY_SRC[src] || [];
  const currentMetric = metricList.find(m => m.k === metric) || metricList[0];

  // Index history by metric_id + as_of for the snapshots table key-value column.
  const histByDate = useMemoA(() => {
    const idx = {};
    (data.history || []).forEach(r => {
      if(!idx[r.metric_id]) idx[r.metric_id] = {};
      idx[r.metric_id][r.as_of] = r.value;
    });
    return idx;
  }, [data.history]);

  // Snapshot rows: list of {date, run, snapshot}. snapshot is always null —
  // raw JSONs aren't stored in Supabase, just metric_history scalars.
  const rows = useMemoA(() => {
    const runs = data.runs[src] || [];
    return runs.map(r => ({ date: r.date, run: r, snapshot: null }))
               .reverse();
  }, [src, data.runs]);

  const selectedRow = selDate ? rows.find(r => r.date === selDate) : null;

  const series = (data.series && data.series[currentMetric?.k]) || [];
  const dates = (data.days || []).map(d => d.date);
  const keyMetric = metricList[0]; // first metric in the source's tab list = "key" column

  return (
    <React.Fragment>
      <PageHead kicker="Pipeline · historical snapshots" title="Archive"
        meta={<React.Fragment>
          <div><b>retention</b>&nbsp; 90 days</div>
          <div><b>format</b>&nbsp;&nbsp;&nbsp; metric_history rows (Supabase)</div>
          <div><b>sources</b>&nbsp;&nbsp; {sources.length} instrumented</div>
        </React.Fragment>}
      />

      <div className="filters">
        <span style={{fontFamily:'IBM Plex Mono, monospace',color:'var(--ink-3)'}}>SOURCE</span>
        <div className="btn-grp">
          {sources.map(s => (
            <button key={s.k} onClick={()=>setSrc(s.k)} className={src===s.k?'on':''}>{s.label}</button>
          ))}
        </div>
        <span style={{fontFamily:'IBM Plex Mono, monospace',color:'var(--ink-3)',marginLeft:14}}>METRIC</span>
        <div className="btn-grp">
          {metricList.map(m => (
            <button key={m.k} onClick={()=>setMetric(m.k)} className={metric===m.k?'on':''}>{m.label}</button>
          ))}
        </div>
      </div>

      {currentMetric && series.length > 0 && (
        <TrendChart
          series={series}
          dates={dates}
          label={currentMetric.label}
          fmt={currentMetric.fmt}
          runs={data.runs[src] || []}
        />
      )}

      <h2 className="sec">Snapshots — {SOURCE_LABELS[src] || src}</h2>
      <p className="sec-lede">Click a row to view per-day metadata. Raw JSON snapshots are not stored in Supabase — see the trend chart above for source values.</p>
      <table className="tbl">
        <thead>
          <tr>
            <th>Date</th>
            <th>Status</th>
            <th>Scraped at</th>
            <th>Duration</th>
            <th>Key value</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0,40).map(row => {
            let key = '—';
            if(keyMetric && histByDate[keyMetric.k]){
              const v = histByDate[keyMetric.k][row.date];
              if(v != null) key = `${keyMetric.fmt(v)} ${keyMetric.label.split(' ')[0]}`;
            }
            const dur = row.run.durationMs;
            const startedAt = row.run.startedAt
              ? row.run.startedAt.replace('T',' ').slice(0,16) + 'Z'
              : '—';
            return (
              <tr key={row.date} className={`clickable ${selDate===row.date?'selected':''}`} onClick={()=>setSelDate(row.date)}>
                <td className="num">{row.date}</td>
                <td><StatusPill status={row.run.status}/></td>
                <td className="num muted">{startedAt}</td>
                <td className="num muted">{dur != null ? (dur/1000).toFixed(1) + 's' : '—'}</td>
                <td className="num">{key}</td>
                <td className="muted">{row.run.error ? row.run.error.slice(0,40) + (row.run.error.length>40?'…':'') : ''}</td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <Drawer open={!!selDate} onClose={()=>setSelDate(null)} title={selectedRow ? `${SOURCE_LABELS[src] || src} · ${selectedRow.date}` : ''}>
        {selectedRow && <SnapshotDetail row={selectedRow} src={src}/>}
      </Drawer>
    </React.Fragment>
  );
}

function SnapshotDetail({ row, src }){
  const startedAt = row.run.startedAt
    ? row.run.startedAt.replace('T',' ').slice(0,19) + 'Z'
    : '—';
  const durLabel = row.run.durationMs != null
    ? (row.run.durationMs/1000).toFixed(1) + 's'
    : '—';
  return (
    <React.Fragment>
      <div style={{marginBottom:14}}>
        <StatusPill status={row.run.status}/>
        <span style={{marginLeft:10,fontFamily:'IBM Plex Mono, monospace',fontSize:11,color:'var(--ink-3)'}}>
          {startedAt} · {durLabel}
        </span>
      </div>
      {row.run.error && (
        <div style={{background:'var(--accent-bg)',color:'var(--accent)',padding:'10px 12px',fontFamily:'IBM Plex Mono, monospace',fontSize:11.5,borderLeft:'2px solid var(--accent)',marginBottom:14}}>
          {row.run.error}
        </div>
      )}
      <div style={{fontFamily:'IBM Plex Mono, monospace',fontSize:10,letterSpacing:'.14em',textTransform:'uppercase',color:'var(--ink-3)',marginBottom:6}}>Source values</div>
      <div style={{fontFamily:'IBM Plex Mono, monospace',fontSize:11.5,color:'var(--ink-2)',marginBottom:12}}>{src} · {row.date}</div>
      {row.snapshot ? (
        <pre>{JSON.stringify(row.snapshot, null, 2)}</pre>
      ) : (
        <div style={{fontFamily:'IBM Plex Serif, serif',fontStyle:'italic',color:'var(--ink-3)'}}>
          Raw snapshot JSON not stored in Supabase. Source values for this date are
          in metric_history — see the trend chart above.
        </div>
      )}
    </React.Fragment>
  );
}

function TrendChart({ series, dates, label, fmt, runs }){
  const w = 980, h = 220, padL = 56, padR = 16, padT = 14, padB = 28;
  const innerW = w - padL - padR;
  const innerH = h - padT - padB;
  const clean = series.map(v=>v==null?null:v);
  const numeric = clean.filter(v=>v!=null);
  if(numeric.length < 2){
    return (
      <div className="chartwrap">
        <div className="lbl">
          <div className="nm">{label}</div>
          <div className="now">latest <b className="tnum">—</b></div>
        </div>
        <div style={{padding:'40px 0',textAlign:'center',fontFamily:'IBM Plex Serif, serif',fontStyle:'italic',color:'var(--ink-3)'}}>
          Not enough history yet to plot a trend.
        </div>
      </div>
    );
  }
  const min = Math.min(...numeric);
  const max = Math.max(...numeric);
  const span = max - min;
  const yMin = min - span*0.08;
  const yMax = max + span*0.08;
  const yRange = (yMax - yMin) || 1;
  const stepX = innerW / (clean.length - 1);

  const xy = i => [padL + i*stepX, padT + innerH - ((clean[i]-yMin)/yRange)*innerH];

  let path = '';
  let pen = false;
  clean.forEach((v,i)=>{
    if(v==null){pen=false;return;}
    const [x,y] = xy(i);
    path += (pen?'L':'M') + x.toFixed(1) + ',' + y.toFixed(1) + ' ';
    pen = true;
  });

  // Y-axis ticks (3)
  const ticks = [yMin + yRange*0.1, yMin + yRange*0.5, yMin + yRange*0.9];
  // X-axis ticks: every 15 days
  const xTicks = [];
  for(let i=0;i<dates.length;i+=15){ xTicks.push({i, label: dates[i].slice(5)}); }
  if(xTicks.length && xTicks[xTicks.length-1].i !== dates.length-1){
    xTicks.push({i: dates.length-1, label: dates[dates.length-1].slice(5)});
  }

  const lastVal = clean[clean.length-1];
  const last = lastVal != null ? xy(clean.length-1) : null;

  // Mark failure days as red dots on x-axis. Map run.date to day-index.
  const dateToIdx = {};
  dates.forEach((d,i) => { dateToIdx[d] = i; });
  const failPoints = (runs || [])
    .filter(r => r.status === 'fail' || r.status === 'stale')
    .map(r => ({ i: dateToIdx[r.date], status: r.status }))
    .filter(fp => fp.i != null);

  return (
    <div className="chartwrap">
      <div className="lbl">
        <div className="nm">{label}</div>
        <div className="now">latest <b className="tnum">{lastVal != null ? fmt(lastVal) : '—'}</b></div>
      </div>
      <svg width="100%" viewBox={`0 0 ${w} ${h}`} style={{display:'block',maxWidth:'100%'}}>
        {/* grid */}
        {ticks.map((t,k) => {
          const y = padT + innerH - ((t-yMin)/yRange)*innerH;
          return (
            <g key={k}>
              <line x1={padL} y1={y} x2={w-padR} y2={y} stroke="var(--grid-line)" strokeWidth="1"/>
              <text x={padL-8} y={y+3} textAnchor="end" fontFamily="IBM Plex Mono, monospace" fontSize="10" fill="var(--ink-3)">{fmt(t)}</text>
            </g>
          );
        })}
        {/* x ticks */}
        {xTicks.map((xt,k) => {
          const x = padL + xt.i*stepX;
          return (
            <g key={k}>
              <line x1={x} y1={padT+innerH} x2={x} y2={padT+innerH+4} stroke="var(--ink-4)" strokeWidth="1"/>
              <text x={x} y={h-10} textAnchor="middle" fontFamily="IBM Plex Mono, monospace" fontSize="10" fill="var(--ink-3)">{xt.label}</text>
            </g>
          );
        })}
        {/* line */}
        <path d={path} fill="none" stroke="var(--ink)" strokeWidth="1.5" strokeLinejoin="round"/>
        {/* failure marks */}
        {failPoints.map((fp,k) => {
          const x = padL + fp.i*stepX;
          return <circle key={k} cx={x} cy={padT+innerH+1} r="3" fill={fp.status==='fail'?'var(--fail)':'var(--warn)'}/>;
        })}
        {/* last */}
        {last && <circle cx={last[0]} cy={last[1]} r="3" fill="var(--ink)"/>}
      </svg>
      <div style={{display:'flex',gap:18,marginTop:10,fontFamily:'IBM Plex Mono, monospace',fontSize:10,color:'var(--ink-3)'}}>
        <span>● <b style={{color:'var(--ink)'}}>line</b> daily value</span>
        <span><span style={{display:'inline-block',width:7,height:7,borderRadius:'50%',background:'var(--fail)'}}></span> fail day</span>
        <span><span style={{display:'inline-block',width:7,height:7,borderRadius:'50%',background:'var(--warn)'}}></span> stale (anomaly)</span>
      </div>
    </div>
  );
}

window.PageArchive = PageArchive;
