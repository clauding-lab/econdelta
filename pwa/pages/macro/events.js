// EconDelta /macro tab — events seed.
// 11 events that map to colored dots on the DSEX chart and to modal cards.
// Exposed via window.MACRO_EVENTS (no ES modules — PWA uses inline Babel).
//
// Each event:
//   id           — stable string id
//   date         — first day of event month, "YYYY-MM-DD"
//   category     — short uppercase tag rendered in modal
//   title        — short headline
//   summary      — 1-line lede shown on the card
//   color        — dot color on DSEX chart
//   kpiMetricIds — 5 metric_ids surfaced as KPI rows in the modal

(function () {
  window.MACRO_EVENTS = [
    {
      id: 'jan13_dsex_birth',
      date: '2013-01-01',
      category: 'INDEX',
      title: 'DSEX Index Launches',
      summary: 'DSE adopts the broad-market DSEX as its primary benchmark.',
      color: '#3b6ea5',
      kpiMetricIds: [
        'dsex_monthly',
        'point_to_point_inflation_monthly',
        'bb_repo_rate_monthly',
        'gross_reserves_usd_bn_monthly',
        'usd_bdt_mid_monthly',
      ],
    },
    {
      id: 'jun18_us_taper',
      date: '2018-06-01',
      category: 'EXTERNAL',
      title: 'US Taper Tantrum · BDT Pressure',
      summary: 'Fed tightening + import surge drives BDT depreciation.',
      color: '#c8472b',
      kpiMetricIds: [
        'usd_bdt_mid_monthly',
        'gross_reserves_usd_bn_monthly',
        'reer_monthly',
        'imports_usd_mn_monthly',
        'remittance_usd_mn_monthly',
      ],
    },
    {
      id: 'mar20_covid',
      date: '2020-03-01',
      category: 'CRISIS',
      title: 'COVID-19 Lockdown',
      summary: 'Economic shutdown; remittance collapse; fiscal expansion.',
      color: '#8b1c0e',
      kpiMetricIds: [
        'point_to_point_inflation_monthly',
        'remittance_usd_mn_monthly',
        'gross_reserves_usd_bn_monthly',
        'm2_growth_yoy_monthly',
        'dsex_monthly',
      ],
    },
    {
      id: 'aug20_remittance_record',
      date: '2020-08-01',
      category: 'EXTERNAL',
      title: 'Remittance Surge',
      summary: 'Diaspora flows hit a multi-year peak through formal channels.',
      color: '#2a8a59',
      kpiMetricIds: [
        'remittance_usd_mn_monthly',
        'gross_reserves_usd_bn_monthly',
        'usd_bdt_mid_monthly',
        'reer_monthly',
        'dsex_monthly',
      ],
    },
    {
      id: 'aug21_reserves_peak',
      date: '2021-08-01',
      category: 'EXTERNAL',
      title: 'FX Reserves Peak · $48bn',
      summary: 'Reserves crest before commodity-import shock begins.',
      color: '#2a8a59',
      kpiMetricIds: [
        'gross_reserves_usd_bn_monthly',
        'import_cover_months_monthly',
        'imports_usd_mn_monthly',
        'usd_bdt_mid_monthly',
        'point_to_point_inflation_monthly',
      ],
    },
    {
      id: 'mar22_ukr_war',
      date: '2022-03-01',
      category: 'EXTERNAL',
      title: 'Russia–Ukraine War · Commodity Shock',
      summary: 'Energy and food import bills spike; pressure on reserves.',
      color: '#c8472b',
      kpiMetricIds: [
        'imports_usd_mn_monthly',
        'gross_reserves_usd_bn_monthly',
        'point_to_point_inflation_monthly',
        'usd_bdt_mid_monthly',
        'cpi_p2p_food_monthly',
      ],
    },
    {
      id: 'jul22_imf_call',
      date: '2022-07-01',
      category: 'POLICY',
      title: 'IMF Programme Discussions Begin',
      summary: 'Authorities engage IMF for $4.7B EFF/ECF/RSF support.',
      color: '#3b6ea5',
      kpiMetricIds: [
        'gross_reserves_usd_bn_monthly',
        'import_cover_months_monthly',
        'usd_bdt_mid_monthly',
        'point_to_point_inflation_monthly',
        'bb_repo_rate_monthly',
      ],
    },
    {
      id: 'feb23_imf_disburse',
      date: '2023-02-01',
      category: 'POLICY',
      title: 'IMF First Disbursement',
      summary: 'First tranche under the $4.7B programme arrives.',
      color: '#2a8a59',
      kpiMetricIds: [
        'gross_reserves_usd_bn_monthly',
        'usd_bdt_mid_monthly',
        'reer_monthly',
        'bb_repo_rate_monthly',
        'point_to_point_inflation_monthly',
      ],
    },
    {
      id: 'may24_smart_repeal',
      date: '2024-05-01',
      category: 'POLICY',
      title: 'SMART Lending-Cap Repealed',
      summary: 'BB shifts to corridor-based monetary policy; repo as anchor.',
      color: '#3b6ea5',
      kpiMetricIds: [
        'bb_repo_rate_monthly',
        'tbill_364d_yield_monthly',
        'private_credit_growth_yoy_monthly',
        'm2_growth_yoy_monthly',
        'point_to_point_inflation_monthly',
      ],
    },
    {
      id: 'aug24_transition',
      date: '2024-08-01',
      category: 'POLICY',
      title: 'Political Transition',
      summary: 'Interim administration takes office; reserves stabilise.',
      color: '#3b6ea5',
      kpiMetricIds: [
        'gross_reserves_usd_bn_monthly',
        'usd_bdt_mid_monthly',
        'dsex_monthly',
        'point_to_point_inflation_monthly',
        'bb_repo_rate_monthly',
      ],
    },
    {
      id: 'feb26_normalization',
      date: '2026-02-01',
      category: 'NORMALIZATION',
      title: 'Reserves Rebuild · Macro Stability',
      summary: 'FX reserves cross $35bn; inflation eases through 9%.',
      color: '#2a8a59',
      kpiMetricIds: [
        'point_to_point_inflation_monthly',
        'bb_repo_rate_monthly',
        'gross_reserves_usd_bn_monthly',
        'usd_bdt_mid_monthly',
        'dsex_monthly',
      ],
    },
  ];
})();
