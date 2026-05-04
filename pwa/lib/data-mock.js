// EconDelta — mock data generator
// Realistic shape matching the repo's schemas, 90 days of history.

(function(){
  const TODAY = new Date('2026-05-02T10:35:00Z');
  const DAYS = 90;

  // Seeded PRNG for stable mock data
  let _seed = 42;
  function rng(){ _seed = (_seed * 1664525 + 1013904223) >>> 0; return _seed / 0x100000000; }
  function gauss(){ return (rng()+rng()+rng()+rng()-2)/2; }

  const fmtDate = d => d.toISOString().slice(0,10);
  const addDays = (d, n) => { const x = new Date(d); x.setUTCDate(x.getUTCDate()+n); return x; };
  const isWeekendBD = d => { const w = d.getUTCDay(); return w===5 || w===6; }; // Fri=5, Sat=6
  const HOLIDAYS_2026 = new Set(['2026-01-01','2026-02-21','2026-03-26','2026-05-01','2026-08-15']);
  const isTradingDay = d => !isWeekendBD(d) && !HOLIDAYS_2026.has(fmtDate(d));

  // Generate base series with mild autocorrelated walks
  function walk(start, drift, vol, n){
    const out = [start];
    for(let i=1;i<n;i++){
      const prev = out[i-1];
      out.push(prev * (1 + drift + vol*gauss()));
    }
    return out;
  }

  const N = DAYS;
  const usdMid = walk(122.40, 0.0001, 0.0008, N);
  const eur    = walk(143.80, 0.0001, 0.0012, N);
  const gbp    = walk(165.20, 0.0001, 0.0014, N);
  const reserves = walk(34.10, 0.00005, 0.0006, N);  // bn USD, monthly cadence — we'll snap to start-of-month later
  const dsex   = walk(5230,   0.0002, 0.006, N);
  const ds30   = walk(1980,   0.0002, 0.006, N);
  const dses   = walk(1058,   0.0002, 0.006, N);
  const brent  = walk(82.40,  0.0,    0.011, N);
  const wti    = walk(78.10,  0.0,    0.011, N);
  const gold   = walk(2380,   0.0001, 0.007, N);

  // Build per-day records
  const days = [];
  for(let i=0;i<N;i++){
    const d = addDays(TODAY, -(N-1-i));
    days.push({ date: fmtDate(d), dt: d, trading: isTradingDay(d) });
  }

  // Run history with realistic failure patterns
  function makeRuns(srcKey, opts){
    const baseFail = opts.failRate || 0.03;
    const out = [];
    for(let i=0;i<N;i++){
      const day = days[i];
      let status = 'ok';
      let durationMs = Math.round(opts.baseDur + opts.durJitter * Math.abs(gauss()) * 1000);
      let error = null;

      // DSE skips non-trading days as 'skip'
      if(srcKey === 'dse_market' && !day.trading){
        status = 'skip';
        durationMs = 200;
      } else {
        const r = rng();
        if(r < baseFail){
          status = 'fail';
          const errs = opts.errors || ['Timeout reading selector','Non-200 response','ParseError: table missing'];
          error = errs[Math.floor(rng()*errs.length)];
          durationMs = Math.round(opts.baseDur * 1.5 + 1000*Math.random());
        } else if(r < baseFail + 0.04){
          status = 'stale';
          error = 'Anomaly threshold exceeded — write skipped';
          durationMs = Math.round(opts.baseDur);
        }
      }
      // Carve in a few correlated outage windows
      if(srcKey==='bb_forex' && i>=14 && i<=16) { status='fail'; error='Playwright: net::ERR_CONNECTION_TIMED_OUT'; durationMs=89000; }
      if(srcKey==='bb_forex' && i===55) { status='fail'; error='ParseError: section.content table not found'; durationMs=12400; }
      if(srcKey==='commodity_prices' && i===34) { status='fail'; error='yfinance: no data returned for BZ=F'; durationMs=4100; }
      if(srcKey==='dse_market' && day.trading && i===72) { status='stale'; error='dsex: 5410 -> 5198 (3.92% exceeds 5.00% threshold)'; durationMs=2300; }

      out.push({
        date: day.date,
        startedAt: new Date(day.dt.getTime() + (opts.hour*3600 + opts.minute*60)*1000).toISOString(),
        status,
        durationMs,
        error,
        attempt: 1,
      });
    }
    return out;
  }

  const runs = {
    bb_forex: makeRuns('bb_forex', { hour: 0, minute: 5, baseDur: 24000, durJitter: 6, failRate: 0.04, errors: ['Playwright: timeout','Radware challenge: solver expired','ParseError: section.content table'] }),
    dse_market: makeRuns('dse_market', { hour: 10, minute: 30, baseDur: 1800, durJitter: 1.2, failRate: 0.02, errors: ['Connection refused','HTTP 503','ParseError: <code> block missing'] }),
    commodity_prices: makeRuns('commodity_prices', { hour: 0, minute: 10, baseDur: 3200, durJitter: 1.5, failRate: 0.03, errors: ['yfinance: no data returned','HTTP 404 on chart endpoint','TooManyRequests'] }),
  };

  // Snapshots — only on successful run days
  function buildSnapshots(srcKey, builder){
    const map = {};
    for(let i=0;i<N;i++){
      const r = runs[srcKey][i];
      if(r.status === 'ok'){
        map[r.date] = builder(i, r);
      }
    }
    return map;
  }

  const forexSnaps = buildSnapshots('bb_forex', (i, r) => ({
    schema_version: '1.0',
    date: r.date,
    scraped_at: r.startedAt,
    rates: {
      usd_bdt_mid: +usdMid[i].toFixed(4),
      usd_bdt_buy: +(usdMid[i] - 0.40 - 0.05*Math.abs(gauss())).toFixed(4),
      usd_bdt_sell: +(usdMid[i] + 0.40 + 0.05*Math.abs(gauss())).toFixed(4),
      eur_bdt: +eur[i].toFixed(4),
      gbp_bdt: +gbp[i].toFixed(4),
      source_url: 'https://www.bb.org.bd/en/index.php/econdata/exchangerate',
    },
    reserves: {
      gross_reserves_usd_bn: +reserves[i].toFixed(2),
      import_cover_months: null,
      reserves_date: r.date.slice(0,7) + '-01',
      source_url: 'https://www.bb.org.bd/en/index.php/econdata/intreserve',
    },
  }));

  const dseSnaps = buildSnapshots('dse_market', (i, r) => {
    const day = days[i];
    if(!day.trading){
      return {
        schema_version: '1.0', date: r.date, scraped_at: r.startedAt,
        trading_day: false, indices: null, market: null,
        source_url: 'https://www.dse.com.bd/',
      };
    }
    const prev = i>0 ? dsex[i-1] : dsex[i];
    return {
      schema_version: '1.0', date: r.date, scraped_at: r.startedAt,
      trading_day: true,
      indices: {
        dsex: +dsex[i].toFixed(2),
        dsex_change: +(dsex[i]-prev).toFixed(2),
        dsex_change_pct: +(((dsex[i]-prev)/prev)*100).toFixed(2),
        ds30: +ds30[i].toFixed(2),
        dses: +dses[i].toFixed(2),
      },
      market: {
        turnover_crore: +(700 + 500*rng()).toFixed(2),
        total_trades: Math.round(150000 + 100000*rng()),
        advancing: Math.round(80 + 200*rng()),
        declining: Math.round(80 + 200*rng()),
        unchanged: Math.round(30 + 80*rng()),
      },
      source_url: 'https://www.dse.com.bd/market-statistics.php',
    };
  });

  const commoditySnaps = buildSnapshots('commodity_prices', (i, r) => ({
    schema_version: '1.0', date: r.date, scraped_at: r.startedAt,
    provider: 'yfinance',
    prices: {
      brent_crude: { price: +brent[i].toFixed(2), prev_close: i>0 ? +brent[i-1].toFixed(2) : null,
        change_pct: i>0 ? +((brent[i]-brent[i-1])/brent[i-1]).toFixed(4) : null,
        currency: 'USD', unit: 'barrel' },
      wti_crude:   { price: +wti[i].toFixed(2), prev_close: i>0 ? +wti[i-1].toFixed(2) : null,
        change_pct: i>0 ? +((wti[i]-wti[i-1])/wti[i-1]).toFixed(4) : null,
        currency: 'USD', unit: 'barrel' },
      gold:        { price: +gold[i].toFixed(2), prev_close: i>0 ? +gold[i-1].toFixed(2) : null,
        change_pct: i>0 ? +((gold[i]-gold[i-1])/gold[i-1]).toFixed(4) : null,
        currency: 'USD', unit: 'oz' },
    },
  }));

  // Find latest of each
  function latestOf(snaps){
    const ks = Object.keys(snaps).sort().reverse();
    return ks.length ? snaps[ks[0]] : null;
  }
  const latestForex = latestOf(forexSnaps);
  const latestDse   = (() => {
    // pick most recent trading-day snapshot
    const ks = Object.keys(dseSnaps).sort().reverse();
    for(const k of ks){ if(dseSnaps[k].trading_day) return dseSnaps[k]; }
    return null;
  })();
  const latestCom   = latestOf(commoditySnaps);

  // Sparkline series (last 30 days)
  function series(arr, n=30){
    const out = [];
    for(let i=N-n;i<N;i++){
      out.push(arr[i]);
    }
    return out;
  }

  // For DSE indices, use null on non-trading days so chart skips them
  function tradingSeries(arr){
    const out = [];
    for(let i=N-30;i<N;i++){
      out.push(days[i].trading ? arr[i] : null);
    }
    return out;
  }

  // Compose latest.json bundle
  const bundle = {
    schema_version: '1.0',
    updated_at: TODAY.toISOString(),
    sources_status: {
      bb_forex: {
        status: 'ok', last_success: latestForex.scraped_at,
        age_hours: 4.3, url: 'https://www.bb.org.bd/en/index.php/econdata/exchangerate',
        error: null,
      },
      dse_market: {
        status: 'ok', last_success: latestDse.scraped_at,
        age_hours: 0.08, url: 'https://www.dse.com.bd/market-statistics.php',
        error: null,
      },
      commodity_prices: {
        status: 'ok', last_success: latestCom.scraped_at,
        age_hours: 4.4, url: null, error: null,
      },
    },
    data: {
      usd_bdt_mid: latestForex.rates.usd_bdt_mid,
      usd_bdt_buy: latestForex.rates.usd_bdt_buy,
      usd_bdt_sell: latestForex.rates.usd_bdt_sell,
      eur_bdt: latestForex.rates.eur_bdt,
      gbp_bdt: latestForex.rates.gbp_bdt,
      gross_reserves_usd_bn: latestForex.reserves.gross_reserves_usd_bn,
      reserves_date: latestForex.reserves.reserves_date,
      trading_day: true,
      dsex: latestDse.indices.dsex,
      dsex_change: latestDse.indices.dsex_change,
      dsex_change_pct: latestDse.indices.dsex_change_pct,
      ds30: latestDse.indices.ds30,
      dses: latestDse.indices.dses,
      turnover_crore: latestDse.market.turnover_crore,
      total_trades: latestDse.market.total_trades,
      advancing: latestDse.market.advancing,
      declining: latestDse.market.declining,
      unchanged: latestDse.market.unchanged,
      brent_crude_usd_barrel: latestCom.prices.brent_crude.price,
      wti_crude_usd_barrel:   latestCom.prices.wti_crude.price,
      gold_usd_oz:            latestCom.prices.gold.price,
    },
  };

  // Build flat ticker definitions for Latest grid
  const N0 = N-1;
  const prevIdx = N-2;
  function pct(i, arr){
    if(arr[i-1] === 0 || arr[i-1] == null) return null;
    return (arr[i]-arr[i-1])/arr[i-1];
  }
  const tickers = [
    { key: 'usd_bdt_mid',  group: 'Forex',       label: 'USD / BDT',        unit: 'mid',     val: latestForex.rates.usd_bdt_mid,  delta: pct(N0,usdMid),  spark: series(usdMid), fmt: v => v.toFixed(2) },
    { key: 'eur_bdt',      group: 'Forex',       label: 'EUR / BDT',        unit: 'mid',     val: latestForex.rates.eur_bdt,      delta: pct(N0,eur),     spark: series(eur),    fmt: v => v.toFixed(2) },
    { key: 'gbp_bdt',      group: 'Forex',       label: 'GBP / BDT',        unit: 'mid',     val: latestForex.rates.gbp_bdt,      delta: pct(N0,gbp),     spark: series(gbp),    fmt: v => v.toFixed(2) },
    { key: 'reserves',     group: 'Forex',       label: 'Gross reserves',   unit: 'USD bn',  val: latestForex.reserves.gross_reserves_usd_bn, delta: pct(N0,reserves), spark: series(reserves), fmt: v => v.toFixed(2) },
    { key: 'dsex',         group: 'DSE',         label: 'DSEX',             unit: 'index',   val: latestDse.indices.dsex,         delta: latestDse.indices.dsex_change_pct/100, spark: tradingSeries(dsex), fmt: v => v.toFixed(2) },
    { key: 'ds30',         group: 'DSE',         label: 'DS30',             unit: 'index',   val: latestDse.indices.ds30,         delta: pct(N0,ds30),    spark: tradingSeries(ds30), fmt: v => v.toFixed(2) },
    { key: 'dses',         group: 'DSE',         label: 'DSES',             unit: 'shariah', val: latestDse.indices.dses,         delta: pct(N0,dses),    spark: tradingSeries(dses), fmt: v => v.toFixed(2) },
    { key: 'turnover',     group: 'DSE',         label: 'Turnover',         unit: 'crore',   val: latestDse.market.turnover_crore, delta: null,           spark: null, fmt: v => v.toFixed(0) },
    { key: 'brent_crude',  group: 'Commodities', label: 'Brent crude',      unit: 'USD/bbl', val: latestCom.prices.brent_crude.price, delta: latestCom.prices.brent_crude.change_pct, spark: series(brent), fmt: v => v.toFixed(2) },
    { key: 'wti_crude',    group: 'Commodities', label: 'WTI crude',        unit: 'USD/bbl', val: latestCom.prices.wti_crude.price,   delta: latestCom.prices.wti_crude.change_pct,   spark: series(wti),   fmt: v => v.toFixed(2) },
    { key: 'gold',         group: 'Commodities', label: 'Gold',             unit: 'USD/oz',  val: latestCom.prices.gold.price,        delta: latestCom.prices.gold.change_pct,        spark: series(gold),  fmt: v => v.toFixed(2) },
    { key: 'breadth',      group: 'DSE',         label: 'Adv / Dec',        unit: 'breadth', val: latestDse.market.advancing / latestDse.market.declining, delta: null, spark: null, fmt: v => v.toFixed(2) },
  ];

  // Source metadata
  const sources = [
    {
      key: 'bb_forex',
      name: 'Bangladesh Bank — Exchange rates',
      url: 'https://www.bb.org.bd/en/index.php/econdata/exchangerate',
      method: 'POST',
      renders: 'JS — Playwright + stealth required (Radware CAPTCHA gate)',
      cadence: 'Daily, 00:05 UTC (06:05 BDT)',
      selector: 'section.content table:nth-of-type(1) (USD), nth-of-type(2) (cross rates)',
      fields: ['usd_bdt_mid (WAR)','usd_bdt_buy (Bid)','usd_bdt_sell (Ask)','eur_bdt','gbp_bdt'],
      tos: 'warn — robots.txt unreadable (CAPTCHA-gated)',
      notes: 'Plain HTTP returns the WAF challenge page. Playwright with stealth UA passes through; the second visit usually loads cleanly because the challenge cookie has been set. No stable id/class on the rate tables — selector is positional. WAR (Weighted Average Rate) is treated as the mid; bid → buy, ask → sell. EUR/GBP have no published mid, so we average bid+ask.',
    },
    {
      key: 'bb_reserves',
      name: 'Bangladesh Bank — Foreign exchange reserves',
      url: 'https://www.bb.org.bd/en/index.php/econdata/intreserve',
      method: 'GET',
      renders: 'JS — Playwright + stealth required',
      cadence: 'Monthly (page updates ~mid-month for prior month)',
      selector: 'table#sortableTable',
      fields: ['gross_reserves_usd_bn','reserves_date'],
      tos: 'warn — same CAPTCHA gate as exchange rates',
      notes: 'Stable table id makes parsing reliable. Values are published in millions USD (we divide by 1000). Period rows are nested under fiscal-year headers like "2025-2026"; we resolve the calendar month from the fiscal half. import_cover_months is NOT published on this page — would require a separate imports source to derive.',
    },
    {
      key: 'dse_market',
      name: 'Dhaka Stock Exchange — Daily market summary',
      url: 'https://www.dse.com.bd/market-statistics.php  +  https://www.dse.com.bd/',
      method: 'GET',
      renders: 'Static HTML — requests + BeautifulSoup',
      cadence: 'Daily on trading days, 10:30 UTC (16:30 BDT)',
      selector: '<code> block inside table for stats; div.LeftColHome > div.midrow for indices',
      fields: ['dsex','ds30','dses','turnover_crore','total_trades','advancing','declining','unchanged'],
      tos: 'warn — terms-of-use.php returned 404; no automated-access restriction located',
      notes: 'Trading-day calendar (Sun–Thu, minus public holidays) gates the run; non-trading days produce a snapshot with trading_day=false rather than a parse error. Index values come from the homepage; advancers/decliners + turnover come from market-statistics.php. Turnover is in Tk on the page — we divide by 10⁷ to get crore.',
    },
    {
      key: 'commodity_prices',
      name: 'Commodity prices — yfinance',
      url: 'yfinance (BZ=F, CL=F, GC=F)',
      method: 'Library',
      renders: 'API client',
      cadence: 'Daily, 00:10 UTC',
      selector: 'fast_info.last_price (fallback: history(period="5d").Close)',
      fields: ['brent_crude','wti_crude','gold (with prev_close + change_pct)'],
      tos: 'unofficial Yahoo Finance client; widely used, no API key',
      notes: 'Palm oil (FCPO.KL) was excluded 2026-04-30 — Yahoo returns 404 for the symbol. Alpha Vantage is the documented fallback if yfinance access degrades. Each commodity is fetched independently; partial failure is allowed (returns warning, not skip).',
    },
  ];

  // Expose globally
  window.ED_DATA = {
    today: TODAY,
    days,
    runs,
    forexSnaps,
    dseSnaps,
    commoditySnaps,
    bundle,
    tickers,
    sources,
    series: { usdMid, eur, gbp, reserves, dsex, ds30, dses, brent, wti, gold },
    fmtDate,
  };
})();
