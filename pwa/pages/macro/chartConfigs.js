// EconDelta /macro tab — Chart.js config builders.
//
// All functions are pure: they take (seriesByMetric: Record<string, Array<[date, value]>>)
// and return a Chart.js 4.4.0 config object. No DOM access, no side effects.
//
// Exposed via window.MACRO_CHART_CONFIGS (no ES modules — PWA uses inline Babel).

(function () {
  // Read EconDelta theme tokens from CSS at chart-build time so charts adopt
  // the same palette as the rest of the app and respond to light/dark theme.
  // Falls back to literal hex when called server-side or before document is ready.
  function cssVar(name, fallback) {
    if (typeof document === 'undefined' || !document.documentElement) return fallback;
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function buildPalette() {
    return {
      primary:    cssVar('--accent', '#ff5a1f'),
      primaryDim: cssVar('--accent-bg', 'rgba(255, 90, 31, 0.15)'),
      secondary:  cssVar('--ok', '#006d6d'),
      accent:     cssVar('--ink-2', '#2a3346'),
      grid:       cssVar('--grid-line', '#dde1e8'),
      text:       cssVar('--ink', '#0b1220'),
      paper:      cssVar('--paper', '#ffffff'),
      muted:      cssVar('--ink-3', '#5b6577'),
      danger:     cssVar('--fail', '#d12a2a'),
    };
  }

  // PALETTE object stays a property accessor so values are fresh per chart.
  // (Most callsites read it once; that's fine for build-time evaluation.)
  let PALETTE = buildPalette();

  const FONT = { family: "'IBM Plex Sans', system-ui, sans-serif" };

  // ---------- helpers ----------

  function toPoints(series) {
    return (series || []).map(([d, v]) => ({ x: d, y: v }));
  }

  function lastValue(series) {
    if (!series || !series.length) return null;
    return series[series.length - 1][1];
  }

  function baseLineOptions(opts) {
    opts = opts || {};
    const yTickCallback = opts.yTicks && opts.yTicks.callback;
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 300 },
      interaction: { mode: 'index', intersect: false },
      spanGaps: true,
      elements: {
        line: { tension: 0.3, borderJoinStyle: 'round' },
        point: { radius: 0, hoverRadius: 4, hoverBorderWidth: 2, hoverBackgroundColor: '#fff' },
      },
      plugins: {
        legend: opts.legend ? {
          display: true,
          position: 'top',
          align: 'end',
          labels: { usePointStyle: true, boxWidth: 8, font: FONT, color: PALETTE.text },
        } : { display: false },
        tooltip: {
          backgroundColor: PALETTE.text,
          titleColor: '#fdfaf4',
          bodyColor: '#fdfaf4',
          titleFont: Object.assign({ size: 12, weight: '600' }, FONT),
          bodyFont: Object.assign({ size: 12 }, FONT),
          padding: 10,
          usePointStyle: true,
          boxPadding: 4,
          cornerRadius: 4,
          callbacks: {
            label: (ctx) => {
              const v = ctx.parsed.y;
              const label = ctx.dataset.label || '';
              if (v == null) return label + ': —';
              const fmt = yTickCallback
                ? yTickCallback(v)
                : Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
              return label ? label + ': ' + fmt : String(fmt);
            },
          },
        },
      },
      scales: {
        x: {
          type: 'time',
          time: { unit: 'year', tooltipFormat: 'MMM yyyy' },
          grid: { color: PALETTE.grid },
          ticks: { color: PALETTE.text, font: FONT },
        },
        y: {
          grid: { color: PALETTE.grid },
          ticks: Object.assign({ color: PALETTE.text, font: FONT }, opts.yTicks || {}),
        },
      },
    };
  }

  function windowAround(series, focalDate, monthsBeforeAfter) {
    if (!series) return [];
    const focal = new Date(focalDate);
    return series.filter(([d]) => {
      const dt = new Date(d);
      const diffMonths =
        (dt.getFullYear() - focal.getFullYear()) * 12 +
        (dt.getMonth() - focal.getMonth());
      return Math.abs(diffMonths) <= monthsBeforeAfter;
    });
  }

  // ---------- 13 chart-config builders ----------

  function cpiP2PConfig(s) {
    return {
      type: 'line',
      data: {
        datasets: [
          { label: 'General', data: toPoints(s['point_to_point_inflation_monthly']),
            borderColor: PALETTE.primary, backgroundColor: PALETTE.primaryDim,
            borderWidth: 2, pointRadius: 0, tension: 0.2 },
          { label: 'Food', data: toPoints(s['cpi_p2p_food_monthly']),
            borderColor: PALETTE.secondary, borderWidth: 1.5, pointRadius: 0, tension: 0.2 },
          { label: 'Non-food', data: toPoints(s['cpi_p2p_nonfood_monthly']),
            borderColor: PALETTE.accent, borderWidth: 1.5, pointRadius: 0, tension: 0.2 },
        ],
      },
      options: baseLineOptions({ legend: true, yTicks: { callback: v => v + '%' } }),
    };
  }

  function inflation12mAvgConfig(s) {
    return {
      type: 'line',
      data: {
        datasets: [
          { label: 'General 12m', data: toPoints(s['cpi_12m_avg_monthly']),
            borderColor: PALETTE.primary, backgroundColor: PALETTE.primaryDim,
            borderWidth: 2, pointRadius: 0, tension: 0.2, fill: true },
          { label: 'Food 12m', data: toPoints(s['cpi_12m_food_monthly']),
            borderColor: PALETTE.secondary, borderWidth: 1.5, pointRadius: 0, tension: 0.2 },
          { label: 'Non-food 12m', data: toPoints(s['cpi_12m_nonfood_monthly']),
            borderColor: PALETTE.accent, borderWidth: 1.5, pointRadius: 0, tension: 0.2 },
        ],
      },
      options: baseLineOptions({ legend: true, yTicks: { callback: v => v + '%' } }),
    };
  }

  function repoAndTbillConfig(s) {
    return {
      type: 'line',
      data: {
        datasets: [
          { label: 'BB repo', data: toPoints(s['bb_repo_rate_monthly']),
            borderColor: PALETTE.primary, borderWidth: 2, pointRadius: 0 },
          { label: '364-day T-bill', data: toPoints(s['tbill_364d_yield_monthly']),
            borderColor: PALETTE.accent, borderWidth: 1.5, pointRadius: 0,
            borderDash: [4, 4] },
        ],
      },
      options: baseLineOptions({ legend: true, yTicks: { callback: v => v + '%' } }),
    };
  }

  // Yield curve: x-axis is tenor in years (numeric), one dataset per as_of month.
  // Latest month bold; priors at low opacity to show the term-structure evolution.
  // Note: Macro Observer JSON has tenors at 2y / 5y / 10y / 20y only (no 1y upstream).
  function yieldCurveConfig(s) {
    const tenors = [
      { id: 'yield_2y_monthly',  x: 2 },
      { id: 'yield_5y_monthly',  x: 5 },
      { id: 'yield_10y_monthly', x: 10 },
      { id: 'yield_20y_monthly', x: 20 },
    ];
    const byDate = {};
    tenors.forEach(t => {
      (s[t.id] || []).forEach(([d, v]) => {
        if (v == null) return;
        if (!byDate[d]) byDate[d] = [];
        byDate[d].push({ x: t.x, y: v });
      });
    });
    const dates = Object.keys(byDate).sort();
    if (!dates.length) {
      return { type: 'line', data: { datasets: [] }, options: baseLineOptions() };
    }
    const latest = dates[dates.length - 1];
    const datasets = dates.map(d => ({
      label: d,
      data: byDate[d].sort((a, b) => a.x - b.x),
      borderColor: d === latest ? PALETTE.primary : 'rgba(200,71,43,0.08)',
      borderWidth: d === latest ? 2.5 : 1,
      pointRadius: d === latest ? 3 : 0,
      tension: 0.1,
      showLine: true,
    }));
    return {
      type: 'line',
      data: { datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 300 },
        interaction: { mode: 'nearest', intersect: false, axis: 'x' },
        parsing: false,
        elements: {
          line: { tension: 0.1, borderJoinStyle: 'round' },
          point: { hoverRadius: 5, hoverBorderWidth: 2, hoverBackgroundColor: '#fff' },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: PALETTE.text,
            titleColor: '#fdfaf4',
            bodyColor: '#fdfaf4',
            titleFont: Object.assign({ size: 12, weight: '600' }, FONT),
            bodyFont: Object.assign({ size: 12 }, FONT),
            padding: 10,
            cornerRadius: 4,
            callbacks: {
              title: (items) => items[0] ? items[0].dataset.label : '',
              label: (ctx) => ctx.parsed.x + 'Y: ' + Number(ctx.parsed.y).toFixed(2) + '%',
            },
          },
        },
        scales: {
          x: { type: 'linear', title: { display: true, text: 'Tenor (years)', font: FONT },
               grid: { color: PALETTE.grid }, ticks: { color: PALETTE.text, font: FONT } },
          y: { ticks: { color: PALETTE.text, font: FONT, callback: v => v + '%' },
               grid: { color: PALETTE.grid } },
        },
      },
    };
  }

  function realPolicyRateConfig(s) {
    const data = (s['real_policy_rate_monthly'] || []).map(([d, v]) => ({ x: d, y: v }));
    return {
      type: 'bar',
      data: {
        datasets: [{
          label: 'Real policy rate',
          data,
          backgroundColor: ctx => ctx.parsed && ctx.parsed.y < 0 ? PALETTE.primary : PALETTE.secondary,
          borderWidth: 0,
        }],
      },
      options: baseLineOptions({ yTicks: { callback: v => v + '%' } }),
    };
  }

  function domesticCreditCompositionConfig(s) {
    return {
      type: 'line',
      data: {
        datasets: [
          { label: 'Public', data: toPoints(s['domestic_credit_public_monthly']),
            borderColor: PALETTE.accent, backgroundColor: 'rgba(59,110,165,0.4)',
            fill: 'origin', borderWidth: 1.5, pointRadius: 0 },
          { label: 'Private', data: toPoints(s['domestic_credit_private_monthly']),
            borderColor: PALETTE.primary, backgroundColor: 'rgba(200,71,43,0.4)',
            fill: '-1', borderWidth: 1.5, pointRadius: 0 },
        ],
      },
      options: (function () {
        const base = baseLineOptions({ legend: true });
        base.scales.y.stacked = true;
        return base;
      })(),
    };
  }

  function domesticCreditGrowthConfig(s) {
    return {
      type: 'line',
      data: {
        datasets: [
          { label: 'Total YoY', data: toPoints(s['domestic_credit_growth_yoy_monthly']),
            borderColor: PALETTE.text, borderWidth: 2, pointRadius: 0, borderDash: [2, 2] },
          { label: 'Public YoY', data: toPoints(s['public_credit_growth_yoy_monthly']),
            borderColor: PALETTE.accent, borderWidth: 1.5, pointRadius: 0 },
          { label: 'Private YoY', data: toPoints(s['private_credit_growth_yoy_monthly']),
            borderColor: PALETTE.primary, borderWidth: 2, pointRadius: 0 },
        ],
      },
      options: baseLineOptions({ legend: true, yTicks: { callback: v => v + '%' } }),
    };
  }

  function moneyGrowthConfig(s) {
    return {
      type: 'line',
      data: {
        datasets: [
          { label: 'M1 YoY', data: toPoints(s['m1_growth_yoy_monthly']),
            borderColor: PALETTE.accent, borderWidth: 1.5, pointRadius: 0 },
          { label: 'M2 YoY', data: toPoints(s['m2_growth_yoy_monthly']),
            borderColor: PALETTE.primary, borderWidth: 2, pointRadius: 0 },
        ],
      },
      options: baseLineOptions({ legend: true, yTicks: { callback: v => v + '%' } }),
    };
  }

  function fxFlowsConfig(s) {
    const exp = toPoints(s['exports_usd_mn_monthly']);
    const rem = toPoints(s['remittance_usd_mn_monthly']);
    const imp = (s['imports_usd_mn_monthly'] || []).map(([d, v]) =>
      ({ x: d, y: v == null ? null : -Math.abs(v) }));
    return {
      type: 'bar',
      data: {
        datasets: [
          { label: 'Exports', data: exp, backgroundColor: PALETTE.secondary, borderWidth: 0, stack: 'inflow' },
          { label: 'Remittance', data: rem, backgroundColor: PALETTE.accent, borderWidth: 0, stack: 'inflow' },
          { label: 'Imports (–)', data: imp, backgroundColor: PALETTE.primary, borderWidth: 0, stack: 'outflow' },
        ],
      },
      options: (function () {
        const base = baseLineOptions({ legend: true });
        base.scales.y.stacked = true;
        base.scales.x.stacked = true;
        return base;
      })(),
    };
  }

  function fxReservesConfig(s) {
    return {
      type: 'line',
      data: {
        datasets: [{
          label: 'Gross reserves',
          data: toPoints(s['gross_reserves_usd_bn_monthly']),
          borderColor: PALETTE.primary,
          backgroundColor: PALETTE.primaryDim,
          fill: 'origin',
          borderWidth: 2,
          pointRadius: 0,
          tension: 0.2,
        }],
      },
      options: baseLineOptions({ yTicks: { callback: v => '$' + v + 'B' } }),
    };
  }

  function importCoverConfig(s) {
    return {
      type: 'line',
      data: {
        datasets: [{
          label: 'Import cover',
          data: toPoints(s['import_cover_months_monthly']),
          borderColor: PALETTE.primary,
          borderWidth: 2,
          pointRadius: 0,
        }],
      },
      options: baseLineOptions({ yTicks: { callback: v => v + ' mo' } }),
    };
  }

  function bdtUsdReerConfig(s) {
    return {
      type: 'line',
      data: {
        datasets: [
          { label: 'BDT/USD (left)', data: toPoints(s['usd_bdt_mid_monthly']),
            borderColor: PALETTE.primary, borderWidth: 2, pointRadius: 0,
            yAxisID: 'y1' },
          { label: 'REER (right, 100=base)', data: toPoints(s['reer_monthly']),
            borderColor: PALETTE.accent, borderWidth: 1.5, pointRadius: 0,
            yAxisID: 'y2' },
        ],
      },
      options: (function () {
        const base = baseLineOptions({ legend: true });
        // Replace single y with dual y1/y2
        delete base.scales.y;
        base.scales.y1 = { type: 'linear', position: 'left',  grid: { color: PALETTE.grid },
                           ticks: { color: PALETTE.text, font: FONT } };
        base.scales.y2 = { type: 'linear', position: 'right', grid: { display: false },
                           ticks: { color: PALETTE.text, font: FONT } };
        return base;
      })(),
    };
  }

  // DSEX index with event dots overlaid. No turnover line — that series is not
  // available in the upstream Macro Observer JSON.
  function dsexConfig(s, events) {
    const dataPts = toPoints(s['dsex_monthly']);
    const dataMap = {};
    dataPts.forEach(p => { dataMap[p.x] = p.y; });
    const eventDots = (events || [])
      .filter(e => e.color && e.date && dataMap[e.date] != null)
      .map(e => ({ x: e.date, y: dataMap[e.date], color: e.color, id: e.id }));

    return {
      type: 'line',
      data: {
        datasets: [
          { label: 'DSEX', data: dataPts,
            borderColor: PALETTE.primary, backgroundColor: PALETTE.primaryDim,
            borderWidth: 2, pointRadius: 0, tension: 0.2, fill: true },
          { label: 'Events',
            data: eventDots,
            type: 'scatter',
            backgroundColor: eventDots.map(p => p.color),
            borderColor: '#fff',
            borderWidth: 1.5,
            pointRadius: 6,
            pointHoverRadius: 8,
            showLine: false },
        ],
      },
      options: baseLineOptions({ legend: false }),
    };
  }

  // ---------- mini-charts for event modals ----------

  function eventInflationRepoMiniConfig(s, eventDate) {
    return {
      type: 'line',
      data: {
        datasets: [
          { label: 'Inflation YoY',
            data: toPoints(windowAround(s['point_to_point_inflation_monthly'], eventDate, 6)),
            borderColor: PALETTE.primary, borderWidth: 2, pointRadius: 2 },
          { label: 'BB repo',
            data: toPoints(windowAround(s['bb_repo_rate_monthly'], eventDate, 6)),
            borderColor: PALETTE.accent, borderWidth: 1.5, pointRadius: 2,
            borderDash: [3, 3] },
        ],
      },
      options: baseLineOptions({ legend: true, yTicks: { callback: v => v + '%' } }),
    };
  }

  function eventReservesBdtMiniConfig(s, eventDate) {
    return {
      type: 'line',
      data: {
        datasets: [
          { label: 'Reserves (USD bn)',
            data: toPoints(windowAround(s['gross_reserves_usd_bn_monthly'], eventDate, 6)),
            borderColor: PALETTE.primary, borderWidth: 2, pointRadius: 2,
            yAxisID: 'y1' },
          { label: 'BDT/USD',
            data: toPoints(windowAround(s['usd_bdt_mid_monthly'], eventDate, 6)),
            borderColor: PALETTE.accent, borderWidth: 1.5, pointRadius: 2,
            borderDash: [3, 3], yAxisID: 'y2' },
        ],
      },
      options: (function () {
        const base = baseLineOptions({ legend: true });
        base.scales.x.time = { unit: 'month' };
        delete base.scales.y;
        base.scales.y1 = { type: 'linear', position: 'left',  ticks: { color: PALETTE.text, font: FONT } };
        base.scales.y2 = { type: 'linear', position: 'right', grid: { display: false },
                           ticks: { color: PALETTE.text, font: FONT } };
        return base;
      })(),
    };
  }

  // ---------- registry ----------

  window.MACRO_CHART_CONFIGS = {
    PALETTE: PALETTE,
    lastValue: lastValue,
    cpiP2P:                    cpiP2PConfig,
    inflation12mAvg:           inflation12mAvgConfig,
    repoAndTbill:              repoAndTbillConfig,
    yieldCurve:                yieldCurveConfig,
    realPolicyRate:            realPolicyRateConfig,
    domesticCreditComposition: domesticCreditCompositionConfig,
    domesticCreditGrowth:      domesticCreditGrowthConfig,
    moneyGrowth:               moneyGrowthConfig,
    fxFlows:                   fxFlowsConfig,
    fxReserves:                fxReservesConfig,
    importCover:               importCoverConfig,
    bdtUsdReer:                bdtUsdReerConfig,
    dsex:                      dsexConfig,
    eventInflationRepoMini:    eventInflationRepoMiniConfig,
    eventReservesBdtMini:      eventReservesBdtMiniConfig,
  };
})();
