// Sources & About pages — bundle-faithful layouts adapted to v3 schema.

const UPSTREAMS = [
  {
    key: 'bb_forex',
    name: 'Bangladesh Bank — Exchange rates + reserves',
    url: 'https://www.bb.org.bd/en/index.php/econdata/exchangerate',
    method: 'POST',
    renders: 'JS — Playwright + stealth required (Radware WAF)',
    cadence: '05:05 BDT daily (+ 06:00 BDT retry)',
    tos: 'warn — robots.txt unreadable (CAPTCHA-gated)',
    selector: 'section.content table:nth-of-type(1) (USD), nth-of-type(2) (cross rates); table#sortableTable for reserves',
    fields: ['usd_bdt_mid (WAR)', 'usd_bdt_buy', 'usd_bdt_sell', 'eur_bdt', 'gbp_bdt', 'gross_reserves_usd_bn'],
    notes: 'Plain HTTP returns the Akamai/Radware challenge page. Playwright with stealth UA passes through; the second visit usually loads cleanly because the challenge cookie has been set. WAR (Weighted Average Rate) treated as the mid; bid → buy, ask → sell. Operating from a BD-located VPS (Exonhost BDIX, Dhaka) — EU/US datacenter IPs get firewall-blocked.',
  },
  {
    key: 'dse_market',
    name: 'Dhaka Stock Exchange — Daily market summary',
    url: 'https://www.dse.com.bd/market-statistics.php  +  https://www.dse.com.bd/',
    method: 'GET',
    renders: 'Static HTML — requests + BeautifulSoup',
    cadence: '05:11 BDT trading days',
    tos: 'warn — terms-of-use.php returned 404; no automated-access restriction located',
    selector: '<code> block inside table for stats; div.LeftColHome > div.midrow for indices',
    fields: ['dsex', 'ds30', 'dses', 'turnover_crore', 'total_trades', 'advancing', 'declining', 'unchanged', 'sector_heat (8 sectors)'],
    notes: 'Trading-day calendar (Sun–Thu, minus public holidays) gates the run; non-trading days produce status=skip rather than a parse error. Sector heat parses 8 DSE industry buckets each trading session. Turnover divided by 10⁷ to get crore.',
  },
  {
    key: 'commodity_prices',
    name: 'Commodity prices — yfinance',
    url: 'yfinance (BZ=F, CL=F, GC=F)',
    method: 'Library',
    renders: 'API client',
    cadence: '05:08 BDT daily',
    tos: 'unofficial Yahoo Finance client; widely used, no API key',
    selector: 'fast_info.last_price (fallback: history(period="5d").Close)',
    fields: ['brent_crude_usd_barrel', 'wti_crude_usd_barrel', 'gold_usd_oz'],
    notes: 'Palm oil (FCPO.KL) was excluded 2026-04-30 — Yahoo returns 404 for the symbol. Alpha Vantage is the documented fallback if yfinance access degrades.',
  },
  {
    key: 'fetch',
    name: 'Fetch orchestrator — HTML/PDF for 60+ indicators',
    url: 'config/sources-v3.json (60+ entries)',
    method: 'GET (mixed HTTP/Playwright)',
    renders: 'Playwright stealth where Akamai-gated; plain requests otherwise',
    cadence: '05:00 BDT daily',
    tos: 'per-source — gsom, dam.gov.bd, NBR DailyStar, FSAR PDFs all govt or wire',
    selector: 'per-indicator JSONPath / CSS selector / regex',
    fields: ['HTML/PDF cached to data/raw/ for parse_all to consume'],
    notes: 'Walks sources-v3.json in parallel (60+ indicators), saves cached HTML/PDF blobs. systemd TimeoutStartSec=1800 (30 min) — bumped 2026-05-04 from 900 after observed 29m39s wall on full catalog. cache hits on retry kick in for sources that didn\'t change.',
  },
  {
    key: 'parse',
    name: 'Parse orchestrator — extract values from raw blobs',
    url: 'data/raw/* (from fetch)',
    method: 'parsers/* + claude_max',
    renders: 'Hybrid: deterministic parsers first, LLM fallback (Claude) for needs_review',
    cadence: '10:30 BDT daily (+ 11:55 BDT retry)',
    tos: 'n/a — local processing',
    selector: '7 parser families: pdf_component, dam_ticker, html_footer_ticker, gsom_ticker, hybrid, etc.',
    fields: ['ParseResult per indicator: {metric_id, value, source_as_of, confidence, status}'],
    notes: 'Subprocess to claude CLI uses --strict-mcp-config flag (since 2026-05-04) to block Discord-MCP-hijack. Three layers extract source_as_of: FSAR cover ("Quarter ending Q YYYY"), DAM portal header, NBR <meta property=\\"article:published_time\\">. Run produces 59-122 indicators per fire.',
  },
  {
    key: 'aggregate',
    name: 'Aggregate — fold latest into Supabase',
    url: 'data/parsed/* + Opus review',
    method: 'utils/supabase_writer + Opus 4.6 review gate',
    renders: 'aggregate_latest.py main() → wrap_run',
    cadence: '13:00 BDT daily (+ 14:00 BDT retry)',
    tos: 'n/a',
    selector: 'metric_history upsert + metric_definitions seed (idempotent ON CONFLICT)',
    fields: ['ALL — writes flat rows to metric_history and seeds metric_definitions catalog'],
    notes: 'Opus review gate: if Opus rejects (e.g. anomaly detected), main() exits 1 BEFORE writing — safety check. ECONDELTA_SKIP_OPUS_REVIEW=1 bypasses for manual fires. Wraps with wrap_run("aggregate", ...) so each fire writes a run_logs row regardless of outcome.',
  },
];

const ANOMALY_THRESHOLDS = [
  ['dsex', '5.00%', 'dse_market'],
  ['ds30', '5.00%', 'dse_market'],
  ['dses', '5.00%', 'dse_market'],
  ['usd_bdt_mid', '2.00%', 'bb_forex'],
  ['usd_bdt_buy', '2.00%', 'bb_forex'],
  ['usd_bdt_sell', '2.00%', 'bb_forex'],
  ['eur_bdt', '3.00%', 'bb_forex'],
  ['gbp_bdt', '3.00%', 'bb_forex'],
  ['gross_reserves_usd_bn', '3.00%', 'bb_forex'],
  ['brent_crude_usd_barrel', '8.00%', 'commodity_prices'],
  ['wti_crude_usd_barrel', '8.00%', 'commodity_prices'],
  ['gold_usd_oz', '6.00%', 'commodity_prices'],
];

const SUPABASE_TABLES = [
  {
    name: 'metric_history',
    mirrors: '— (canonical store)',
    purpose: 'One row per (metric_id, as_of). Aggregator upserts; The Brief and the PWA read latest-per-metric_id via the RPC. Slow-cadence metrics (FSAR/DAM/NBR) use writer override so as_of carries the publication date directly.',
    cols: [
      ['metric_id',   'text PK',     ''],
      ['as_of',       'date PK',     'publication date (override) or run date'],
      ['value',       'numeric',     'scalar; jsonb-shaped values not supported'],
      ['source',      'text',        '"EconDelta" by default; aggregator-stamped'],
      ['ingested_at', 'timestamptz', 'when the row landed in Supabase'],
    ],
  },
  {
    name: 'metric_definitions',
    mirrors: '— (catalog)',
    purpose: 'Indicator catalog. Aggregator seeds new rows on first sight via INSERT … ON CONFLICT (metric_id) DO NOTHING — manual edits in Supabase Studio (label, sort_order, is_hero) are preserved forever.',
    cols: [
      ['metric_id',   'text PK',     ''],
      ['label',       'text',        'long-form display label'],
      ['short_label', 'text',        'compact label for hero cards'],
      ['unit',        'text',        '"%", "USD bn", "index" — free-form'],
      ['domain',      'text',        'forex_and_reserves | equities | inflation | money_market | …'],
      ['sort_order',  'integer',     'within-domain ordering, default 100'],
      ['cadence',     'text',        'daily | trading-day | monthly | quarterly | annual'],
      ['format',      'text',        '"comma-2dp" | "pct-1dp" | "pct-2dp" | "currency-bdt" — drives PWA formatter'],
      ['source',      'text',        'upstream key (bb_forex, dse_market, fetch, …)'],
      ['source_url',  'text',        'click-through URL on the Sources page'],
      ['is_hero',     'boolean',     'promotes to hero card on the Latest page'],
      ['inverted',    'boolean',     'lower-is-better (e.g. NPL ratio going up is bad)'],
    ],
  },
  {
    name: 'run_logs',
    mirrors: '— (operational audit)',
    purpose: 'Every scraper invocation, success or fail. Powers the Run dashboard commit graph + failure log; written via wrap_run() helper at each scraper\'s __main__.',
    cols: [
      ['id',          'uuid PK',     'gen_random_uuid()'],
      ['source',      'text',        'bb_forex | dse_market | commodity_prices | fetch | parse | aggregate'],
      ['started_at',  'timestamptz', 'wrap_run start; logged immediately'],
      ['finished_at', 'timestamptz', 'wrap_run end; populated on completion'],
      ['duration_ms', 'integer',     'finished_at − started_at'],
      ['status',      'text',        'ok | fail | stale | skip — mapped from main() exit code'],
      ['exit_code',   'integer',     '0/1/2/3 mapping to status above'],
      ['error',       'text',        'first 2 KB of traceback / stderr'],
      ['attempt',     'integer',     'systemd retry counter'],
      ['host',        'text',        'usually local.clauding-lab.com (ExonVPS)'],
      ['unit',        'text',        'systemd unit name e.g. econdelta-aggregate.service'],
      ['inserted_at', 'timestamptz', 'row creation timestamp'],
    ],
  },
  {
    name: 'get_latest_dashboard()',
    mirrors: '— (RPC)',
    purpose: 'Single-call jsonb RPC for the PWA Latest page. SECURITY INVOKER (anon-callable). Returns {updated_at, definitions, values, sources_status} in one round trip.',
    cols: [
      ['updated_at',     'timestamptz',  'now() at call time'],
      ['definitions',    'jsonb (array)', 'all metric_definitions rows, sorted (domain, sort_order)'],
      ['values',         'jsonb (object)', '{metric_id: {value, as_of}} from latest row per metric_id'],
      ['sources_status', 'jsonb (object)', '{source: {status, last_success, duration_ms, error}} from latest run_logs row per source'],
    ],
  },
];

function PageSources(){
  const data = window.ED_DATA && window.ED_DATA.dashboard;
  // Group definitions by source for the bottom indicator-list section.
  const bySource = {};
  ((data && data.definitions) || []).forEach(def => {
    const src = def.source || 'other';
    if(!bySource[src]) bySource[src] = [];
    bySource[src].push(def);
  });
  const sourceKeys = Object.keys(bySource).sort();
  const totalIndicators = (data && data.definitions) ? data.definitions.length : 0;

  return (
    <React.Fragment>
      <PageHead kicker="Pipeline · data origins" title="Sources"
        meta={<React.Fragment>
          <div><b>upstreams</b>&nbsp; {UPSTREAMS.length}</div>
          <div><b>indicators</b>&nbsp; {totalIndicators}</div>
          <div><b>config</b>&nbsp;&nbsp; config/sources-v3.json</div>
        </React.Fragment>}
      />
      <p style={{maxWidth:720,fontFamily:'IBM Plex Serif, serif',fontStyle:'italic',color:'var(--ink-2)',marginBottom:28}}>
        Six upstreams feed the pipeline. Three are direct scrapers (BB forex, DSE, commodities); three are orchestrators (fetch, parse, aggregate) that walk the v3 catalog of 60+ indicators end-to-end. Each entry below records the access mechanism, schedule, and the caveats encountered during the v3 audit.
      </p>

      {UPSTREAMS.map(s => (
        <div className="src" key={s.key}>
          <div className="head">
            <div>
              <div className="name">{s.name}</div>
              <div className="url">{s.url}</div>
            </div>
            <div><StatusPill status="ok"/></div>
          </div>
          <div className="grid">
            <div className="field"><div className="k">Method</div><div className="v">{s.method}</div></div>
            <div className="field"><div className="k">Rendering</div><div className="v">{s.renders}</div></div>
            <div className="field"><div className="k">Cadence</div><div className="v">{s.cadence}</div></div>
            <div className="field"><div className="k">ToS posture</div><div className="v">{s.tos}</div></div>
            <div className="field" style={{gridColumn:'1 / -1',borderRight:'none'}}>
              <div className="k">Selector / lookup</div>
              <div className="v" style={{whiteSpace:'pre-wrap'}}>{s.selector}</div>
            </div>
            <div className="field" style={{gridColumn:'1 / -1',borderRight:'none',borderBottom:'none'}}>
              <div className="k">Fields exposed</div>
              <div className="v">{s.fields.map(f =>
                <span key={f} style={{display:'inline-block',padding:'2px 7px',background:'var(--code-bg)',borderRadius:2,marginRight:6,marginBottom:4}}>{f}</span>
              )}</div>
            </div>
          </div>
          <div className="notes">{s.notes}</div>
        </div>
      ))}

      <h2 className="sec">Anomaly thresholds</h2>
      <p className="sec-lede">Fractional daily-change ceilings. Crossing one skips the write and pages Discord. Tunable in <span className="mono">config/thresholds.json</span> after enough live data accumulates.</p>
      <table className="tbl">
        <thead><tr><th>Metric</th><th>Threshold</th><th>Source</th></tr></thead>
        <tbody>
          {ANOMALY_THRESHOLDS.map((r,i) =>
            <tr key={i}><td>{r[0]}</td><td className="num">{r[1]}</td><td className="muted">{r[2]}</td></tr>
          )}
        </tbody>
      </table>

      <h2 className="sec">Supabase schema</h2>
      <p className="sec-lede">
        Three tables plus one RPC backing the Latest page. Migrations live in <span className="mono">db/migrations/</span> (0001 → 0005). Aggregator seeds <span className="mono">metric_definitions</span> idempotently and upserts <span className="mono">metric_history</span> on every fire; <span className="mono">run_logs</span> is written by the <span className="mono">wrap_run()</span> helper at each scraper's <span className="mono">__main__</span>.
      </p>
      <div className="sb-grid">
        {SUPABASE_TABLES.map(t => (
          <div className="sb-tbl" key={t.name}>
            <div className="sb-head">
              <span className="sb-name">{t.name}</span>
              <span className="sb-py">{t.mirrors}</span>
            </div>
            <div className="sb-purpose">{t.purpose}</div>
            <table className="sb-cols">
              <tbody>
                {t.cols.map(c => (
                  <tr key={c[0]}>
                    <td className="sb-c">{c[0]}</td>
                    <td className="sb-t">{c[1]}</td>
                    <td className="sb-n">{c[2] || ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
      </div>
      <div className="sb-note">
        <div><b>RLS</b> &nbsp;Anon-read <span className="mono">select</span> policies on all three tables; writes use the service-role key from <span className="mono">/etc/econdelta.env</span>.</div>
        <div><b>Indexes</b> &nbsp;<span className="mono">metric_history</span> has <span className="mono">(metric_id, as_of desc)</span>; <span className="mono">run_logs</span> has <span className="mono">(source, started_at desc)</span> and <span className="mono">(started_at desc)</span>.</div>
        <div><b>RPC</b> &nbsp;<span className="mono">get_latest_dashboard()</span> is <span className="mono">SECURITY INVOKER</span> + anon-grantable; PWA fetches it via PostgREST in one round trip.</div>
        <div><b>Wiring</b> &nbsp;Set <span className="mono">window.ED_SUPABASE_CONFIG</span> in <span className="mono">pwa/config.js</span> with project URL + publishable anon key (<span className="mono">sb_publishable_*</span>).</div>
      </div>

      <h2 className="sec">Indicator inventory</h2>
      <p className="sec-lede">Where each indicator originates. Click <span className="mono">source ↗</span> for the upstream URL.</p>
      {sourceKeys.length === 0 ? (
        <p style={{fontStyle:'italic',color:'var(--ink-3)'}}>No definitions seeded yet. Run aggregate at least once to populate.</p>
      ) : sourceKeys.map(src => (
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

      <h2 className="sec">External seeds &amp; acknowledgements</h2>
      <p className="sec-lede">
        Long-horizon series that EconDelta does not scrape itself. Used as a one-shot historical seed; primary sources will be revisited as the catalog deepens.
      </p>
      <div className="src">
        <div className="head">
          <div>
            <div className="name">Macro Observer — Bangladesh long-horizon monthly</div>
            <div className="url">
              <a href="https://macro.thenazmussakib.com/" target="_blank" rel="noopener">https://macro.thenazmussakib.com/</a>
            </div>
          </div>
          <div><StatusPill status="ok"/></div>
        </div>
        <div className="grid">
          <div className="field"><div className="k">Author</div><div className="v">Nazmus Sakib</div></div>
          <div className="field"><div className="k">Underlying sources</div><div className="v">Bangladesh Bank · BBS · DSE</div></div>
          <div className="field"><div className="k">Cadence</div><div className="v">One-shot seed (Jan 2012 → present)</div></div>
          <div className="field"><div className="k">Method</div><div className="v">JSON download + KEY_MAP transform</div></div>
          <div className="field" style={{gridColumn:'1 / -1',borderRight:'none',borderBottom:'none'}}>
            <div className="k">Used by</div>
            <div className="v">
              <span style={{display:'inline-block',padding:'2px 7px',background:'var(--code-bg)',borderRadius:2,marginRight:6,marginBottom:4}}>/macro tab</span>
              <span style={{display:'inline-block',padding:'2px 7px',background:'var(--code-bg)',borderRadius:2,marginRight:6,marginBottom:4}}>metric_history_monthly</span>
              <span style={{display:'inline-block',padding:'2px 7px',background:'var(--code-bg)',borderRadius:2,marginRight:6,marginBottom:4}}>metric_definitions_monthly</span>
            </div>
          </div>
        </div>
        <div className="notes">
          Seeded into Supabase via <span className="mono">scripts/seed_macro_monthly.py</span> from the public <span className="mono">macro_monthly_data.json</span> payload. 4,665 rows across 29 monthly metric_ids covering January 2012 through May 2026; each row tagged <span className="mono">source = 'macro_observer_seed'</span>. Future EconDelta-native monthly aggregation (computed from daily <span className="mono">metric_history</span>) will append to the same table; the seed becomes purely historical from that point forward.
        </div>
      </div>
    </React.Fragment>
  );
}

function PageAbout(){
  return (
    <React.Fragment>
      <PageHead kicker="Pipeline · documentation" title="About"
        meta={<React.Fragment>
          <div><b>repo</b>&nbsp; clauding-lab/econdelta</div>
          <div><b>backend</b>&nbsp; ExonVPS · BDIX-Dhaka</div>
          <div><b>frontend</b>&nbsp; GitHub Pages</div>
        </React.Fragment>}
      />
      <p style={{maxWidth:720,fontSize:15,color:'var(--ink-2)'}}>
        EconDelta is a deterministic Bangladesh macro data pipeline. Three scrapers and three orchestrators fetch BB forex, DSE indices, commodity prices, and 60+ catalog indicators on a daily schedule, validate via deterministic parsers + LLM fallback, run an Opus review gate for anomalies, then upsert into a Supabase Postgres backing both <code style={{fontFamily:'IBM Plex Mono, monospace',background:'var(--code-bg)',padding:'1px 6px',borderRadius:2}}>The Brief</code> (downstream agent) and this PWA dashboard.
      </p>

      <h2 className="sec">Three layers</h2>
      <p className="sec-lede">Backend, data, frontend — three separate deployment lifecycles, three sources of truth.</p>
      <div className="cards">
        <div className="card">
          <div className="h">Backend · Python on ExonVPS</div>
          <div className="body">Six systemd units running from 05:00 to 14:00 BDT daily — morning scrape cascade (fetch / bb_forex / commodity / dse) 05:00–05:11, mid-day Claude-extraction parse (10:30 + 11:55 retry), afternoon aggregate (13:00 + 14:00 retry). Operating from a BD-located VPS (Exonhost BDIX, Dhaka) — bypasses BB+DSE foreign-IP firewalls.</div>
        </div>
        <div className="card">
          <div className="h">Data · Supabase Postgres</div>
          <div className="body">Three tables (<span className="mono">metric_history</span>, <span className="mono">metric_definitions</span>, <span className="mono">run_logs</span>) plus the <span className="mono">get_latest_dashboard()</span> RPC. Anon-readable; writes from ExonVPS service role only.</div>
        </div>
        <div className="card">
          <div className="h">Frontend · vanilla React PWA</div>
          <div className="body">No build step — React 18 UMD + Babel standalone served from <span className="mono">pwa/vendor/</span>. Deployed via <span className="mono">.github/workflows/pwa-deploy.yml</span> on every push to <span className="mono">pwa/**</span>; SW cache + script <span className="mono">?v=...</span> stamped per-deploy.</div>
        </div>
      </div>

      <h2 className="sec">Architecture at a glance</h2>
      <pre style={{fontFamily:'IBM Plex Mono, monospace',fontSize:11.5,background:'var(--code-bg)',color:'var(--code-ink)',border:'1px solid var(--ink)',borderLeft:'3px solid var(--accent)',padding:18,whiteSpace:'pre',overflowX:'auto'}}>
{`                    systemd timers (ExonVPS · BDIX-Dhaka)
                                  │
                                  │  morning scrape cascade
        ┌───────────┬─────────────┼─────────────┐
        ▼           ▼             ▼             ▼
     fetch     bb_forex     commodity        dse
   05:00 BDT   05:05 BDT    05:08 BDT    05:11 BDT
   (60+ urls)  (Playwright) (yfinance)   (requests)
        │           │             │             │
        └───────────┴─────────────┴─────────────┘
                                  │
                                  ▼
                       parse_all.py (Stage 2 — Claude extraction)
                          10:30 BDT (+ 11:55 retry)
                          ├ claude --print --strict-mcp-config
                          └ 60+ ParseResult rows → data/parsed/*
                                  │
                                  ▼
                        aggregate_latest.py
                          13:00 BDT (+ 14:00 retry)
                          ├ Opus review gate (4.6)
                          ├ wrap_run → run_logs row
                          ├ upsert_metric_definitions_seed (60+ rows, idempotent)
                          └ upsert_metric_history (122 rows / day)
                                  │
                                  ▼
                       Supabase Postgres (the-brief project)
                                  │
                ┌─────────────────┼─────────────────┐
                ▼                 ▼                 ▼
         get_latest_dashboard()  REST          /etc/brief.env
                │              metric_history       │
                ▼              run_logs            ▼
       PWA (this dashboard)                    The Brief
       clauding-lab.github.io/econdelta/      (downstream agent)`}
      </pre>

      <h2 className="sec">Key conventions</h2>
      <div className="cards">
        <div className="card">
          <div className="h">Atomic writes</div>
          <div className="body">All snapshot files write through <span className="mono">.tmp</span> + <span className="mono">os.replace</span>. The downstream reader can never see a partial JSON.</div>
        </div>
        <div className="card">
          <div className="h">Anomaly gating</div>
          <div className="body">Per-metric fractional thresholds (see Sources page). Cross one and the scraper exits 2 (<span className="mono">stale</span>), skips the write, and pages Discord.</div>
        </div>
        <div className="card">
          <div className="h">Trading calendar</div>
          <div className="body">DSE runs Sun–Thu minus Bangladesh public holidays. Non-trading days produce <span className="mono">status=skip</span> in run_logs, not errors.</div>
        </div>
        <div className="card">
          <div className="h">Pydantic at the boundary</div>
          <div className="body">Snapshots validated on parse via <span className="mono">ParseResult</span> + per-parser schemas; <span className="mono">latest.json</span>'s flat dict stays loose <span className="mono">dict[str, Any]</span>.</div>
        </div>
        <div className="card">
          <div className="h">wrap_run instrumentation</div>
          <div className="body">Each scraper's <span className="mono">__main__</span> wraps <span className="mono">main()</span> in <span className="mono">wrap_run(source, unit, main)</span>. Maps exit 0/1/2/3 → ok/fail/stale/skip; uncaught exceptions log + re-raise.</div>
        </div>
        <div className="card">
          <div className="h">--strict-mcp-config</div>
          <div className="body">Every Claude subprocess in EconDelta passes this flag to block Discord-MCP-hijack. Shipped 2026-05-04 after a brief render produced Discord output instead of HTML.</div>
        </div>
      </div>

      <p style={{marginTop:32,maxWidth:720,fontSize:13,color:'var(--ink-3)'}}>
        <b>License</b> &nbsp;Source code at <a href="https://github.com/clauding-lab/econdelta" style={{color:'var(--accent)'}}>github.com/clauding-lab/econdelta</a>. Data is for informational use only — verify against original sources before any operational decision.
      </p>
    </React.Fragment>
  );
}

window.PageSources = PageSources;
window.PageAbout = PageAbout;
