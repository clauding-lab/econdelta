// EconDelta /macro tab — events seed.
//
// Structurally matches Macro Observer's events list (titles, dates, tags,
// summaries) so the analytical narrative reads the same. The Feb'26
// Bangladesh National Election is added as a project-specific 12th event
// per Adnan's request.
//
// Each event:
//   id           — stable string id
//   date         — first day of event month, "YYYY-MM-DD" (used to position
//                  marker on x-axis and to look up KPI values)
//   category     — short uppercase tag rendered above the title
//   title        — short headline (matches MO's `title` verbatim where applicable)
//   summary      — 1-line lede shown on the card (matches MO's `blurb`)
//   color        — dot color on DSEX chart and left-border on event card
//   kpiMetricIds — 5 metric_ids surfaced as KPI rows in the modal

(function () {
  // Color tokens borrowed from Macro Observer's chart-annotation EVENTS list:
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
      title: 'Stable Growth Window',
      summary: 'GDP growth >6%, inflation moderating, reserves rising.',
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
      title: 'Strong Growth Continues',
      summary: 'Real GDP growth crosses 7%, on track for 8% by FY19.',
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
      title: 'COVID-19 Shock',
      summary: 'Nationwide lockdown March-May 2020. RMG orders collapse.',
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
      title: 'Reopening & Reserves Peak',
      summary: 'FX reserves hit all-time high of $48bn in August 2021.',
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
      title: 'Russia–Ukraine War & Energy Shock',
      summary: "Brent crude tops $130. Bangladesh's import bill explodes.",
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
      title: 'Energy Crisis & Load-Shedding',
      summary: 'Daily power outages return. Diesel-fuel rationing imposed.',
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
      title: 'IMF Program Approved',
      summary: '$4.7bn EFF/RSF program approved Jan 30, 2023.',
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
      title: 'Crawling Peg Adopted',
      summary: 'BB devalues taka by 6.7% in single move. End of soft peg.',
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
      title: 'Political Transition',
      summary: 'Aug 5: PM Hasina resigns amid student-led protests. Yunus interim govt.',
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
      title: 'Disinflation Underway',
      summary: 'P2P inflation falls to 8.5% from 9%+ earlier in 2025.',
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
      title: 'Reserves Rebuild · Macro Stability',
      summary: 'FX reserves cross $35bn. P2P inflation just above 9%.',
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
      title: 'Bangladesh National Election',
      summary: 'General election following the 2024 transition; Yunus interim govt hands over.',
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
