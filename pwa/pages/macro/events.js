// EconDelta /macro tab — events seed.
//
// Bangladesh macro narrative cards. Underlying economic facts and dates
// align with what most Bangladesh macro analysts (incl. Macro Observer)
// flag as inflection points, but the wording is original to EconDelta.
//
// Each event:
//   id           — stable string id
//   date         — first day of event month, "YYYY-MM-DD" (used to position
//                  marker on x-axis and to look up KPI values)
//   category     — short uppercase tag rendered above the title
//   title        — short headline
//   summary      — 1-line lede shown on the card
//   color        — dot color on DSEX chart and left-border on event card
//   kpiMetricIds — 5 metric_ids surfaced as KPI rows in the modal

(function () {
  // Color tokens by event flavour:
  //   #1c5d4e — deep teal-green (expansion / recovery / normalization)
  //   #c8472b — red/oxblood     (crisis / external shock)
  //   #b8860b — amber-gold      (infrastructure / inflation)
  //   #2d4a7c — navy-blue       (policy / political)
  //   #7a3b6f — plum-purple     (regime change / geopolitics)

  window.MACRO_EVENTS = [
    {
      id: 'sep14_stable_growth',
      date: '2014-09-01',
      category: 'EXPANSION',
      title: 'Multi-Year Macro Calm',
      summary: 'Steady GDP above 6%, inflation easing back to single digits, FX cushion thickening.',
      color: '#1c5d4e',
      kpiMetricIds: [
        'point_to_point_inflation_monthly',
        'gross_reserves_usd_bn_monthly',
        'private_credit_growth_yoy_monthly',
        'usd_bdt_mid_monthly',
        'dsex_monthly',
      ],
    },
    {
      id: 'jan19_strong_growth',
      date: '2019-01-01',
      category: 'EXPANSION',
      title: 'Pre-Crisis Growth Peak',
      summary: 'Real GDP tops 7% YoY; FY19 trajectory points to 8% by year-end.',
      color: '#1c5d4e',
      kpiMetricIds: [
        'point_to_point_inflation_monthly',
        'private_credit_growth_yoy_monthly',
        'gross_reserves_usd_bn_monthly',
        'usd_bdt_mid_monthly',
        'dsex_monthly',
      ],
    },
    {
      id: 'apr20_covid',
      date: '2020-04-01',
      category: 'CRISIS',
      title: 'Pandemic Lockdown',
      summary: 'General holiday Mar–May; RMG orders cancelled wholesale; remittance channel disruption.',
      color: '#c8472b',
      kpiMetricIds: [
        'point_to_point_inflation_monthly',
        'remittance_usd_mn_monthly',
        'gross_reserves_usd_bn_monthly',
        'm2_growth_yoy_monthly',
        'dsex_monthly',
      ],
    },
    {
      id: 'jul21_reopening_reserves_peak',
      date: '2021-07-01',
      category: 'RECOVERY',
      title: 'Reopening · Reserves Crest',
      summary: 'Gross reserves cross $48bn — the all-time peak — driven by remittance surge and weak imports.',
      color: '#1c5d4e',
      kpiMetricIds: [
        'gross_reserves_usd_bn_monthly',
        'remittance_usd_mn_monthly',
        'imports_usd_mn_monthly',
        'usd_bdt_mid_monthly',
        'dsex_monthly',
      ],
    },
    {
      id: 'mar22_ukr_war',
      date: '2022-03-01',
      category: 'EXTERNAL',
      title: 'Energy Shock · Import Surge',
      summary: 'Brent above $130; Bangladesh’s commodity import bill explodes.',
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
      id: 'aug22_energy_crisis',
      date: '2022-08-01',
      category: 'INFRASTRUCTURE',
      title: 'Power Cuts · Fuel Queues',
      summary: 'LNG imports suspended; daily load-shedding returns; diesel rationing imposed.',
      color: '#b8860b',
      kpiMetricIds: [
        'point_to_point_inflation_monthly',
        'cpi_p2p_food_monthly',
        'imports_usd_mn_monthly',
        'gross_reserves_usd_bn_monthly',
        'usd_bdt_mid_monthly',
      ],
    },
    {
      id: 'jan23_imf_approved',
      date: '2023-01-01',
      category: 'POLICY',
      title: 'IMF Lifeline Cleared',
      summary: '$4.7bn EFF/RSF facility approved end-January; reform anchor in place.',
      color: '#2d4a7c',
      kpiMetricIds: [
        'gross_reserves_usd_bn_monthly',
        'usd_bdt_mid_monthly',
        'reer_monthly',
        'bb_repo_rate_monthly',
        'point_to_point_inflation_monthly',
      ],
    },
    {
      id: 'may24_crawling_peg',
      date: '2024-05-01',
      category: 'FX REGIME',
      title: 'Step Devaluation',
      summary: 'BB drops the soft peg; one-shot 6.7% taka devaluation, then a crawling band.',
      color: '#7a3b6f',
      kpiMetricIds: [
        'usd_bdt_mid_monthly',
        'reer_monthly',
        'bb_repo_rate_monthly',
        'gross_reserves_usd_bn_monthly',
        'point_to_point_inflation_monthly',
      ],
    },
    {
      id: 'aug24_political_transition',
      date: '2024-08-01',
      category: 'GEOPOLITICS',
      title: 'Regime Change',
      summary: 'Hasina exits Aug 5 after student-led protests; Yunus heads interim government.',
      color: '#7a3b6f',
      kpiMetricIds: [
        'gross_reserves_usd_bn_monthly',
        'usd_bdt_mid_monthly',
        'dsex_monthly',
        'point_to_point_inflation_monthly',
        'bb_repo_rate_monthly',
      ],
    },
    {
      id: 'jun25_disinflation',
      date: '2025-06-01',
      category: 'INFLATION',
      title: 'Disinflation Begins',
      summary: 'P2P prints 8.5% — first sustained move below 9% since the 2022 spike.',
      color: '#b8860b',
      kpiMetricIds: [
        'point_to_point_inflation_monthly',
        'cpi_12m_avg_monthly',
        'bb_repo_rate_monthly',
        'usd_bdt_mid_monthly',
        'private_credit_growth_yoy_monthly',
      ],
    },
    {
      id: 'feb26_normalization',
      date: '2026-02-01',
      category: 'NORMALIZATION',
      title: 'External Position Restored',
      summary: 'Reserves push past $35bn on the BPM6 measure; inflation hovers just above 9%.',
      color: '#1c5d4e',
      kpiMetricIds: [
        'gross_reserves_usd_bn_monthly',
        'point_to_point_inflation_monthly',
        'usd_bdt_mid_monthly',
        'bb_repo_rate_monthly',
        'dsex_monthly',
      ],
    },
    {
      id: 'feb26_election',
      date: '2026-02-15',
      category: 'POLITICAL',
      title: 'National Election',
      summary: 'Bangladesh goes to the polls; Yunus interim government hands power back.',
      color: '#2d4a7c',
      kpiMetricIds: [
        'dsex_monthly',
        'point_to_point_inflation_monthly',
        'gross_reserves_usd_bn_monthly',
        'usd_bdt_mid_monthly',
        'bb_repo_rate_monthly',
      ],
    },
  ];
})();
