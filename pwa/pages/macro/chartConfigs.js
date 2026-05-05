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

  // Round to max 2 decimal places and drop trailing zeros so Chart.js axis
  // ticks don't show floating-point artifacts like 5.6000000000000005%.
  function r2(v) {
    if (v == null || !isFinite(v)) return v;
    return Math.round(v * 100) / 100;
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
      options: baseLineOptions({ legend: true, yTicks: { callback: v => r2(v) + '%' } }),
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
      options: baseLineOptions({ legend: true, yTicks: { callback: v => r2(v) + '%' } }),
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
      options: baseLineOptions({ legend: true, yTicks: { callback: v => r2(v) + '%' } }),
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
          y: { ticks: { color: PALETTE.text, font: FONT, callback: v => r2(v) + '%' },
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
      options: baseLineOptions({ yTicks: { callback: v => r2(v) + '%' } }),
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
      options: baseLineOptions({ legend: true, yTicks: { callback: v => r2(v) + '%' } }),
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
      options: baseLineOptions({ legend: true, yTicks: { callback: v => r2(v) + '%' } }),
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
      options: baseLineOptions({ yTicks: { callback: v => '$' + r2(v) + 'B' } }),
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
      options: baseLineOptions({ yTicks: { callback: v => r2(v) + ' mo' } }),
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

  // Custom Chart.js plugin: render event markers as colored dots at the top
  // of the chart (above the data area) with a dashed vertical drop line down
  // to the chart bottom. On hover, the line goes solid + a colored label box
  // appears above the dot showing the event title. Modeled on Macro Observer.
  function makeEventMarkersPlugin(events) {
    return {
      id: 'eventMarkers',
      afterDatasetsDraw(chart) {
        const { ctx, chartArea, scales } = chart;
        if (!chartArea) return;
        const xs = scales.x;
        const dotY = chartArea.top - 14;
        const hovered = chart.$hoveredMarkerIdx;

        ctx.save();

        events.forEach((e, i) => {
          const xPx = xs.getPixelForValue(new Date(e.date).getTime());
          if (xPx < chartArea.left - 10 || xPx > chartArea.right + 10) return;
          const isHovered = hovered === i;

          // Dashed (or solid when hovered) vertical drop line
          ctx.strokeStyle = e.color;
          ctx.globalAlpha = isHovered ? 0.85 : 0.30;
          ctx.lineWidth = isHovered ? 1.8 : 1;
          ctx.setLineDash(isHovered ? [] : [3, 3]);
          ctx.beginPath();
          ctx.moveTo(xPx, dotY + 3);
          ctx.lineTo(xPx, chartArea.bottom);
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.globalAlpha = 1;

          // Colored outer ring
          const capR = isHovered ? 6.5 : 4.5;
          ctx.fillStyle = e.color;
          ctx.beginPath();
          ctx.arc(xPx, dotY, capR, 0, Math.PI * 2);
          ctx.fill();
          // Inner paper dot (so the marker reads as a ring)
          ctx.fillStyle = PALETTE.paper;
          ctx.beginPath();
          ctx.arc(xPx, dotY, isHovered ? 2.5 : 1.7, 0, Math.PI * 2);
          ctx.fill();
        });

        // Hovered label box + connector + date string
        if (hovered != null && events[hovered]) {
          const e = events[hovered];
          const xPx = xs.getPixelForValue(new Date(e.date).getTime());
          if (xPx >= chartArea.left - 10 && xPx <= chartArea.right + 10) {
            const titleText = (e.title || '').toUpperCase();
            ctx.font = "600 11px 'IBM Plex Mono', monospace";
            const textW = ctx.measureText(titleText).width;
            const padX = 11;
            const w = textW + padX * 2;
            const h = 24;
            let labelX = xPx - w / 2;
            if (labelX < chartArea.left) labelX = chartArea.left + 4;
            if (labelX + w > chartArea.right) labelX = chartArea.right - w - 4;
            const labelY = dotY - h - 12;

            // Connector from label box down to dot
            ctx.strokeStyle = e.color;
            ctx.lineWidth = 1.4;
            ctx.beginPath();
            ctx.moveTo(xPx, dotY - 6);
            ctx.lineTo(xPx, labelY + h);
            ctx.stroke();

            // Filled box with same-color border
            ctx.fillStyle = e.color;
            ctx.strokeStyle = e.color;
            ctx.lineWidth = 1.5;
            ctx.beginPath();
            ctx.rect(labelX, labelY, w, h);
            ctx.fill();
            ctx.stroke();

            // Title text
            ctx.fillStyle = PALETTE.paper;
            ctx.textBaseline = 'middle';
            ctx.fillText(titleText, labelX + padX, labelY + h / 2);

            // Date string under the dot
            ctx.font = "10px 'IBM Plex Mono', monospace";
            ctx.fillStyle = e.color;
            const dateStr = new Date(e.date)
              .toLocaleDateString('en-US', { year: 'numeric', month: 'short' })
              .toUpperCase();
            const dateW = ctx.measureText(dateStr).width;
            let dateX = xPx - dateW / 2;
            if (dateX < chartArea.left) dateX = chartArea.left + 4;
            if (dateX + dateW > chartArea.right) dateX = chartArea.right - dateW - 4;
            ctx.fillText(dateStr, dateX, dotY + 14);
          }
        }

        ctx.restore();
      },
      afterEvent(chart, args) {
        const ev = args.event;
        if (!ev || (ev.type !== 'mousemove' && ev.type !== 'mouseout')) return;
        const chartArea = chart.chartArea;
        if (!chartArea) return;
        const dotY = chartArea.top - 14;
        const xs = chart.scales.x;
        let nearest = null;
        let nearestDist = Infinity;
        // Hover zone: a horizontal band around the marker row
        const inZone = ev.type === 'mousemove'
          && ev.y >= dotY - 18
          && ev.y <= dotY + 18;
        if (inZone) {
          events.forEach((e, i) => {
            const xPx = xs.getPixelForValue(new Date(e.date).getTime());
            const d = Math.abs(xPx - ev.x);
            if (d < nearestDist) { nearestDist = d; nearest = i; }
          });
          if (nearestDist > 18) nearest = null;
        }
        if (nearest !== chart.$hoveredMarkerIdx) {
          chart.$hoveredMarkerIdx = nearest;
          args.changed = true;
        }
        if (chart.canvas) {
          chart.canvas.style.cursor = nearest != null ? 'pointer' : '';
        }
      },
    };
  }

  // DSEX index. Event markers are rendered above the chart by the
  // eventMarkers custom plugin, with dashed drop lines + hover labels.
  function dsexConfig(s, events) {
    const dataPts = toPoints(s['dsex_monthly']);
    const validEvents = (events || []).filter(e => e.color && e.date);
    const opts = baseLineOptions({ legend: false });
    opts.layout = { padding: { top: 60 } };
    // Don't let the index-mode tooltip confuse hover above the data area
    opts.interaction = { mode: 'index', intersect: false };
    return {
      type: 'line',
      data: {
        datasets: [
          { label: 'DSEX', data: dataPts,
            borderColor: PALETTE.accent,
            backgroundColor: 'rgba(11, 18, 32, 0.06)',
            borderWidth: 1.6, pointRadius: 0, tension: 0.25, fill: true },
        ],
      },
      options: opts,
      plugins: [makeEventMarkersPlugin(validEvents)],
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
      options: baseLineOptions({ legend: true, yTicks: { callback: v => r2(v) + '%' } }),
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
