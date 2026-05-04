// Latest page — hero cards (4 most-watched) + bento grid (per-domain tiles)
// Driven by window.ED_DATA.dashboard.{definitions, values, sources_status}.

function PageLatest(){
  const d = window.ED_DATA && window.ED_DATA.dashboard;
  if(!d) {
    return <div className="loading">no dashboard data yet…</div>;
  }
  const defs = d.definitions || [];
  const vals = d.values || {};
  const srcStatus = d.sources_status || {};

  // Hero cards: definitions where is_hero=true (default 4).
  const heroes = defs.filter(x => x.is_hero);

  // Group definitions by domain for bento.
  const byDomain = {};
  defs.forEach(def => {
    if(def.is_hero) return;  // skip — already in heroes
    if(!byDomain[def.domain]) byDomain[def.domain] = [];
    byDomain[def.domain].push(def);
  });

  // Sources status pill row.
  const sourceKeys = Object.keys(srcStatus).sort();

  return (
    <React.Fragment>
      <PageHead
        kicker="Pipeline · canonical snapshot"
        title="Latest"
        meta={
          <React.Fragment>
            <div><b>updated</b> {d.updated_at && d.updated_at.slice(0, 19) + ' UTC'}</div>
            <div><b>defs</b> {defs.length}</div>
            <div><b>values</b> {Object.keys(vals).length}</div>
            <div><b>sources</b> {sourceKeys.length}</div>
          </React.Fragment>
        }
      />

      {/* Sources status row */}
      <div className="src-status-row">
        {sourceKeys.map(src => (
          <div key={src} className="src-pill">
            <span className="muted">{src}</span>
            <StatusPill status={srcStatus[src].status}/>
            <span className="tnum">{relTime(srcStatus[src].last_success)}</span>
          </div>
        ))}
      </div>

      {/* Hero cards */}
      {heroes.length > 0 && (
        <div className="hero-grid">
          {heroes.map(def => {
            const v = vals[def.metric_id];
            return <HeroCard key={def.metric_id} def={def} value={v}/>;
          })}
        </div>
      )}

      {/* Bento grid — one tile per domain */}
      <div className="bento-grid">
        {Object.keys(byDomain).sort().map(domain => (
          <BentoTile key={domain} domain={domain} defs={byDomain[domain]} vals={vals}/>
        ))}
      </div>
    </React.Fragment>
  );
}

function HeroCard({def, value}){
  const v = value && value.value != null ? value.value : null;
  return (
    <div className="hero-card">
      <div className="lbl">{def.short_label || def.label}</div>
      <div className="val tnum">{v == null ? '—' : formatValue(v, def.format)}</div>
      <div className="sub">{def.unit}</div>
    </div>
  );
}

function BentoTile({domain, defs, vals}){
  const onClick = () => { window.location.hash = '#/domain/' + slug(domain); };
  return (
    <div className="bento" onClick={onClick}>
      <div className="dom">{domain}</div>
      <div className="count">{defs.length} indicators</div>
      {defs.slice(0, 3).map(def => {
        const v = vals[def.metric_id];
        return (
          <div key={def.metric_id} className="preview">
            <span>{def.short_label || def.label}</span>
            <span className="pv">{v && v.value != null ? formatValue(v.value, def.format) : '—'}</span>
          </div>
        );
      })}
      {defs.length > 3 && <div className="more">+ {defs.length - 3} more →</div>}
    </div>
  );
}

function formatValue(v, format){
  if(v == null) return '—';
  if(format === 'pct-1dp') return v.toFixed(1) + '%';
  if(format === 'pct-2dp') return v.toFixed(2) + '%';
  if(format === 'currency-bdt') return v.toLocaleString();
  // default: comma-2dp
  return Number(v).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
}

function slug(s){ return String(s).toLowerCase().replace(/\s+/g, '-'); }

window.PageLatest = PageLatest;

// Domain drill-in — full list of indicators in one domain.
function PageDomain({route}){
  const d = window.ED_DATA && window.ED_DATA.dashboard;
  if(!d) return <div className="loading">no data yet…</div>;
  const defs = d.definitions || [];
  const vals = d.values || {};

  // Route shape: '/domain/<slug>' — find domain whose slug matches.
  const targetSlug = route.replace('/domain/', '');
  const domainName = (defs.find(x => slug(x.domain) === targetSlug) || {}).domain;
  if(!domainName){
    return (
      <React.Fragment>
        <PageHead title="Domain not found" kicker="Pipeline"/>
        <p>No indicators registered for "{targetSlug}".</p>
        <p><a href="#/">← Back to Latest</a></p>
      </React.Fragment>
    );
  }

  const domainDefs = defs.filter(x => x.domain === domainName);

  return (
    <React.Fragment>
      <PageHead
        title={domainName}
        kicker="Pipeline · domain detail"
        meta={<div><b>indicators</b> {domainDefs.length}</div>}
      />
      <p><a href="#/">← Back to Latest</a></p>
      <div className="indicator-list">
        {domainDefs.map(def => {
          const v = vals[def.metric_id];
          return (
            <div key={def.metric_id} className="indicator-row">
              <div className="il-label">
                <b>{def.label}</b>
                {def.description && <div className="il-desc">{def.description}</div>}
              </div>
              <div className="il-value tnum">
                {v && v.value != null ? formatValue(v.value, def.format) : '—'}
                <span className="il-unit">{def.unit}</span>
              </div>
              {v && v.as_of && (
                <div className="il-asof">as of {v.as_of}</div>
              )}
            </div>
          );
        })}
      </div>
    </React.Fragment>
  );
}

window.PageDomain = PageDomain;
