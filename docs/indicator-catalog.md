# Indicator catalog

**Generated** by `scripts/build_catalog.py` from `config/sources-v3.json` + `aggregate_latest.BRIEF_ALIASES` + `aggregate_latest.BRIEF_CONVERSIONS` plus a manually-curated list of derived/cross-source keys. Re-run after adding indicators:

```bash
python3 scripts/build_catalog.py > docs/indicator-catalog.md
```

**78** scraped indicators × **36** brief aliases × **12** unit conversions × **5** derived = **131** total entries.

Read the data contract for column semantics and query examples: [`data-contract.md`](data-contract.md).

---

| Section | metric_id | Unit | Cadence | Source | Valid range | Description |
|---------|-----------|------|---------|--------|-------------|-------------|
| banking | `deposits_by_ownership` | `amount_bdt_crore` | quarterly | BB | [0.0, 30000000.0] | Bank deposits by ownership (SOCB / PCB / FCB / Specialised), BDT crore LEVELS (NOT shares). 4-row ownership cluster from the BB Financial Stability Report (FSR) body. Parser returns a dict {socb,pcb,fcb,specialised}; aggregate_latest._flatten_ownership_cluster fans it out into the 4 scalar metrics deposits_socb_cr / deposits_pcb_cr / deposits_fcb_cr / deposits_specialised_cr (the call-money fan-out precedent — no schema change). Stores LEVELS in BDT crore, not pre-computed shares: the YieldScope donut computes shares from the levels (null-safe) so they sum to 100% and stay consistent with deposits_of_the_system. |
| banking | `npl_by_ownership` | `percent` | quarterly | BB | [0.0, 100.0] | Gross NPL ratio by bank ownership (SOCB / PCB / FCB / Specialised), percent. 4-row ownership cluster from the BB Financial Stability Report (FSR) body. Parser returns a dict {socb,pcb,fcb,specialised}; aggregate_latest._flatten_ownership_cluster fans it out into the 4 scalar metrics npl_socb_pct / npl_pcb_pct / npl_fcb_pct / npl_specialised_pct (the call-money fan-out precedent — no schema change). NPL is inherently a ratio (bad loans / that segment's loans) published as a %, so the segment value IS a percent; there is no level to store. SOCB is normally the highest. |
| commodities | `food_atta_packet` | `rate` | daily | DAM | [20.0, 200.0] | Retail price — Atta packaged (BDT/kg) |
| commodities | `food_chicken_farm` | `rate` | daily | DAM | [80.0, 400.0] | Retail price — Farm chicken (BDT/kg) |
| commodities | `food_egg_red` | `rate` | daily | DAM | [20.0, 150.0] | Retail price — Red farm egg (BDT/4 pcs) |
| commodities | `food_lentil_moong` | `rate` | daily | DAM | [50.0, 250.0] | Retail price — Moong lentil (BDT/kg) |
| commodities | `food_oil_soybean` | `rate` | daily | DAM | [80.0, 400.0] | Retail price — Soybean oil (BDT/litre) |
| commodities | `food_onion_local` | `rate` | daily | DAM | [20.0, 400.0] | Retail price — Local onion (BDT/kg) |
| commodities | `food_rice_coarse` | `rate` | daily | DAM | [20.0, 200.0] | Retail price — Aman coarse rice (BDT/kg) |
| commodities | `food_sugar_local` | `rate` | daily | DAM | [50.0, 250.0] | Retail price — Sugar local (BDT/kg) |
| commodities (brief alias) | `dam_chicken` | `rate` | daily | DAM | [80.0, 400.0] | Alias of `food_chicken_farm` — Retail price — Farm chicken (BDT/kg) |
| commodities (brief alias) | `dam_egg` | `rate` | daily | DAM | [20.0, 150.0] | Alias of `food_egg_red` — Retail price — Red farm egg (BDT/4 pcs) |
| commodities (brief alias) | `dam_flour` | `rate` | daily | DAM | [20.0, 200.0] | Alias of `food_atta_packet` — Retail price — Atta packaged (BDT/kg) |
| commodities (brief alias) | `dam_lentil` | `rate` | daily | DAM | [50.0, 250.0] | Alias of `food_lentil_moong` — Retail price — Moong lentil (BDT/kg) |
| commodities (brief alias) | `dam_oil` | `rate` | daily | DAM | [80.0, 400.0] | Alias of `food_oil_soybean` — Retail price — Soybean oil (BDT/litre) |
| commodities (brief alias) | `dam_onion` | `rate` | daily | DAM | [20.0, 400.0] | Alias of `food_onion_local` — Retail price — Local onion (BDT/kg) |
| commodities (brief alias) | `dam_rice_coarse` | `rate` | daily | DAM | [20.0, 200.0] | Alias of `food_rice_coarse` — Retail price — Aman coarse rice (BDT/kg) |
| commodities (brief alias) | `dam_sugar` | `rate` | daily | DAM | [50.0, 250.0] | Alias of `food_sugar_local` — Retail price — Sugar local (BDT/kg) |
| commodities (brief alias) | `food_atta_packet_bdt` | `rate` | daily | DAM | [20.0, 200.0] | Alias of `food_atta_packet` — Retail price — Atta packaged (BDT/kg) |
| commodities (brief alias) | `food_chicken_farm_bdt` | `rate` | daily | DAM | [80.0, 400.0] | Alias of `food_chicken_farm` — Retail price — Farm chicken (BDT/kg) |
| commodities (brief alias) | `food_egg_red_bdt` | `rate` | daily | DAM | [20.0, 150.0] | Alias of `food_egg_red` — Retail price — Red farm egg (BDT/4 pcs) |
| commodities (brief alias) | `food_lentil_moong_bdt` | `rate` | daily | DAM | [50.0, 250.0] | Alias of `food_lentil_moong` — Retail price — Moong lentil (BDT/kg) |
| commodities (brief alias) | `food_oil_soybean_bdt` | `rate` | daily | DAM | [80.0, 400.0] | Alias of `food_oil_soybean` — Retail price — Soybean oil (BDT/litre) |
| commodities (brief alias) | `food_onion_local_bdt` | `rate` | daily | DAM | [20.0, 400.0] | Alias of `food_onion_local` — Retail price — Local onion (BDT/kg) |
| commodities (brief alias) | `food_rice_coarse_bdt` | `rate` | daily | DAM | [20.0, 200.0] | Alias of `food_rice_coarse` — Retail price — Aman coarse rice (BDT/kg) |
| commodities (brief alias) | `food_sugar_local_bdt` | `rate` | daily | DAM | [50.0, 250.0] | Alias of `food_sugar_local` — Retail price — Sugar local (BDT/kg) |
| derived (cross-source) | `crr_utilisation_pct` | `percent` | monthly | — | — | Derived (S2): deposits_held_with_bb_crr / deposits_of_the_system × 100 — CRR balance held with BB as a % of total system deposits (NOT the regulated statutory maintenance ratio; no hardcoded policy rate). Computed in aggregate_latest._compute_reserve_utilisation, null/zero-denominator safe. Lands in metric_history under its own id. |
| derived (cross-source) | `imf_eff_outstanding_sdr_mn` | `amount_sdr_mn` | monthly | — | — | Scraper-only (S5): Bangladesh's Extended Arrangements (EFF) outstanding under the combined ECF/EFF/RSF programme, in SDR Million, pulled directly from the IMF 'Financial Position in the Fund' page by scrapers/imf_eff.py (NO BD egress; no config indicator). Reported natively in SDR — NOT converted to USD (SDR/USD drifts). Lands in metric_history under its own id; as_of = the IMF month-end position date. |
| derived (cross-source) | `nbr_fytd_collected_cr` | `amount_bdt_crore` | monthly | — | — | NBR fiscal-year-to-date collection — sourced canonically from tax_revenue (BB PDF, deterministic parse, 5% anomaly threshold). News corroborators (TBS, Daily Star) retired 2026-05-25. |
| derived (cross-source) | `nbr_fytd_cross_check` | `string` | monthly | — | — | Cross-check status for nbr_fytd_collected_cr — now always 'single_source_tax_revenue' since the news corroborator path was retired 2026-05-25. Strings only land in latest.json — NOT in metric_history (writer filters strings). |
| derived (cross-source) | `slr_utilisation_pct` | `percent` | monthly | — | — | Derived (S2): excess_liquid_asset_total_minimum / deposits_of_the_system × 100 — excess liquid assets over the statutory SLR minimum as a % of total system deposits (NOT the regulated maintenance ratio). Computed in aggregate_latest._compute_reserve_utilisation, null/zero-denominator safe. |
| equities | `dse_sector_heat` | `sector_dict` | daily | DSE | [-50.0, 50.0] | DSE Sector Heat (8 sectors, % avg) |
| external_sector | `bop_summary` | `amount_usd_bn` | monthly | BB | [-20.0, 20.0] | BOP Summary |
| external_sector | `categorywise_export` | `amount_usd_bn` | fiscal_year | BB | [0.0, 60.0] | Categorywise Export |
| external_sector | `categorywise_fy_import_breakdown` | `amount_usd_bn` | fiscal_year | BB | [0.0, 100.0] | Categorywise FY Import Breakdown |
| external_sector | `fy_export` | `amount_usd_bn` | fiscal_year | BB | [0.0, 60.0] | FY Export |
| external_sector | `fy_import_lc` | `amount_usd_bn` | fiscal_year | BB | [0.0, 100.0] | FY Import LC |
| external_sector | `fy_remittance` | `amount_usd_bn` | fiscal_year | BB | [0.0, 50.0] | FY Remittance |
| external_sector | `monthly_export` | `amount_usd_bn` | monthly | BB | [0.0, 10.0] | Monthly Export |
| external_sector | `monthly_import` | `amount_usd_bn` | monthly | BB | [0.0, 10.0] | Monthly Import |
| external_sector | `monthly_import_lc_opening` | `amount_usd_mn` | monthly | BB | [0.0, 20000.0] | Monthly Import LC Opening |
| external_sector | `monthly_import_lc_settlement` | `amount_usd_mn` | monthly | BB | [0.0, 20000.0] | Monthly Import LC Settlement |
| external_sector | `monthly_remittance` | `amount_usd_bn` | monthly | BB | [0.0, 5.0] | Monthly Remittance |
| external_sector | `remittance_by_country` | `amount_usd_bn` | monthly | BB | [0.0, 10.0] | Remittance by country |
| external_sector (brief conversion) | `remit_fy_mn` | `amount_usd_bn` | fiscal_year | BB | — | Conversion of `fy_remittance` × 1000.0 — FY Remittance |
| external_sector (brief conversion) | `remit_monthly_mn` | `amount_usd_bn` | monthly | BB | — | Conversion of `monthly_remittance` × 1000.0 — Monthly Remittance |
| forex_and_reserves | `fx_buy_sale_from_market` | `amount_usd_bn` | monthly |  | [0.0, 5.0] | FX Buy/Sale from Market |
| forex_and_reserves | `fx_reserve_gross_and_bpm6` | `amount_usd_bn` | weekly | BB | [0.0, 100.0] | FX Reserve Gross and BPM6 |
| forex_and_reserves | `usd_bdt_exchange_rate` | `rate` | daily | BB | [80.0, 200.0] | USD/BDT Exchange Rate |
| government_finance | `bank_borrowing_for_deficit_financing` | `amount_bdt_crore` | monthly | BB | [0.0, 400000.0] | Bank Borrowing for Deficit Financing |
| government_finance | `budget_adpex_of_the_fy_vs_utilization` | `amount_bdt_crore` | fiscal_year |  | [0.0, 500000.0] | Budget ADPEx of the FY vs Utilization |
| government_finance | `budget_opex_of_the_fy_vs_utilization` | `amount_bdt_crore` | fiscal_year |  | [0.0, 1000000.0] | Budget OpEx of the FY vs Utilization |
| government_finance | `debt_domestic_stock_cr` | `amount_bdt_crore` | quarterly | mof.gov.bd | [500000.0, 3000000.0] | Domestic Debt Outstanding Stock (MoF Debt Bulletin; FY25 ~Tk11.95tn). Stock level, NOT the deficit-financing flow domestic_borrowing_for_budget_deficit. |
| government_finance | `debt_external_stock_cr` | `amount_bdt_crore` | quarterly | mof.gov.bd | [300000.0, 3000000.0] | External Debt Outstanding Stock (MoF Debt Bulletin; FY25 ~Tk9.49tn). Stock level, NOT the deficit-financing flow foreign_borrowing_for_budget_deficit. |
| government_finance | `debt_gdp_ratio` | `percent` | quarterly | mof.gov.bd | [10.0, 100.0] | Debt-to-GDP Ratio (MoF Debt Bulletin latest print; IMF DataMapper supplies back-history via scrapers/imf_debt_gdp.py) |
| government_finance | `domestic_borrowing_for_budget_deficit` | `amount_bdt_crore` | monthly | BB | [0.0, 400000.0] | Domestic Borrowing for Budget Deficit |
| government_finance | `foreign_borrowing_for_budget_deficit` | `amount_bdt_crore` | monthly | BB | [0.0, 200000.0] | Foreign Borrowing for Budget Deficit |
| government_finance | `nbr_customs_collected_cr` | `amount_bdt_crore` | monthly | TBS | [10000.0, 800000.0] | NBR FYTD Customs Collection (BDT crore) |
| government_finance | `nbr_it_collected_cr` | `amount_bdt_crore` | monthly | TBS | [10000.0, 800000.0] | NBR FYTD Income Tax Collection (BDT crore) |
| government_finance | `nbr_vat_collected_cr` | `amount_bdt_crore` | monthly | TBS | [10000.0, 800000.0] | NBR FYTD VAT Collection (BDT crore) |
| government_finance | `non_bank_borrowing_for_deficit_financing` | `amount_bdt_crore` | monthly | BB | [0.0, 200000.0] | Non-bank borrowing for Deficit Financing |
| government_finance | `non_nbr_tax_revenue` | `amount_bdt_crore` | monthly | mof.gov.bd | [0.0, 60000.0] | Non-NBR Tax Revenue (MoF MFR, FYTD) |
| government_finance | `non_tax_revenue` | `amount_bdt_crore` | monthly | mof.gov.bd | [0.0, 100000.0] | Non-Tax Revenue |
| government_finance | `rev_gdp_ratio` | `percent` | quarterly |  | [0.0, 40.0] | Rev-GDP Ratio |
| government_finance | `tax_gdp_ratio` | `percent` | quarterly |  | [0.0, 30.0] | Tax-GDP Ratio |
| government_finance | `tax_revenue` | `amount_bdt_crore` | monthly | BB | [0.0, 500000.0] | Tax Revenue |
| government_finance | `total_revenue_budget_vs_actual` | `amount_bdt_crore` | monthly |  | [0.0, 600000.0] | Total Revenue Budget vs Actual |
| government_finance | `ways_means_usage_cr` | `amount_bdt_crore` | monthly | BB | [0.0, 500000.0] | Ways & Means Advances Usage (BB overdraft to government; usage LEVEL only — there is NO published monthly limit/ceiling cell, so this is intentionally usage-only with NO 'vs limit' denominator. CEIC sources Tk120,000cr Nov-2025 vs Tk90,924cr Oct-2025.) |
| government_finance (brief alias) | `nbr_fytd_collected_cr` | `amount_bdt_crore` | monthly | BB | [0.0, 500000.0] | Alias of `tax_revenue` — Tax Revenue |
| government_finance (brief conversion) | `fiscal_bank_borrow_trn` | `amount_bdt_crore` | monthly | BB | — | Conversion of `bank_borrowing_for_deficit_financing` × 1e-05 — Bank Borrowing for Deficit Financing |
| government_finance (brief conversion) | `fiscal_foreign_borrow_trn` | `amount_bdt_crore` | monthly | BB | — | Conversion of `foreign_borrowing_for_budget_deficit` × 1e-05 — Foreign Borrowing for Budget Deficit |
| government_finance (brief conversion) | `fiscal_govt_borrow_trn` | `amount_bdt_crore` | monthly | BB | — | Conversion of `domestic_borrowing_for_budget_deficit` × 1e-05 — Domestic Borrowing for Budget Deficit |
| government_finance (brief conversion) | `fiscal_nbr_collected_trn` | `amount_bdt_crore` | monthly | BB | — | Conversion of `tax_revenue` × 1e-05 — Tax Revenue |
| government_finance (brief conversion) | `nbr_customs_bn` | `amount_bdt_crore` | monthly | TBS | — | Conversion of `nbr_customs_collected_cr` × 0.01 — NBR FYTD Customs Collection (BDT crore) |
| government_finance (brief conversion) | `nbr_it_bn` | `amount_bdt_crore` | monthly | TBS | — | Conversion of `nbr_it_collected_cr` × 0.01 — NBR FYTD Income Tax Collection (BDT crore) |
| government_finance (brief conversion) | `nbr_vat_bn` | `amount_bdt_crore` | monthly | TBS | — | Conversion of `nbr_vat_collected_cr` × 0.01 — NBR FYTD VAT Collection (BDT crore) |
| inflation | `food_inflation` | `percent` | monthly | BB | [0.0, 50.0] | Food Inflation |
| inflation | `general_inflation` | `percent` | monthly | BB | [0.0, 50.0] | General Inflation |
| inflation | `non_food_inflation` | `percent` | monthly | BB | [0.0, 50.0] | Non-Food Inflation |
| inflation | `point_to_point_inflation` | `percent` | monthly | BB | [0.0, 50.0] | Point to Point Inflation |
| inflation (brief alias) | `macro_cpi_food` | `percent` | monthly | BB | [0.0, 50.0] | Alias of `food_inflation` — Food Inflation |
| inflation (brief alias) | `macro_cpi_headline` | `percent` | monthly | BB | [0.0, 50.0] | Alias of `general_inflation` — General Inflation |
| inflation (brief alias) | `macro_cpi_nonfood` | `percent` | monthly | BB | [0.0, 50.0] | Alias of `non_food_inflation` — Non-Food Inflation |
| macro | `current_account_balance` | `amount_usd_bn` | monthly | BB | [-20.0, 20.0] | Current Account Balance |
| macro | `gdp` | `amount_bdt_crore` | quarterly | BB | [0.0, 100000000.0] | GDP |
| monetary_aggregates | `broad_money` | `amount_bdt_crore` | monthly | BB | [0.0, 30000000.0] | Broad Money |
| monetary_aggregates | `currency_outside_bank` | `amount_bdt_crore` | monthly | BB | [0.0, 5000000.0] | Currency Outside Bank |
| monetary_aggregates | `deposits_held_with_bb_crr` | `amount_bdt_crore` | monthly | BB | [0.0, 5000000.0] | Deposits held with BB (CRR) |
| monetary_aggregates | `deposits_of_the_system` | `amount_bdt_crore` | monthly | BB | [0.0, 30000000.0] | Deposits of the system |
| monetary_aggregates | `excess_liquid_asset_total_minimum` | `amount_bdt_crore` | monthly | BB | [0.0, 5000000.0] | Excess Liquid Asset (Total-Minimum) |
| monetary_aggregates | `money_multiplier` | `ratio` | monthly | BB | [1.0, 20.0] | Money Multiplier |
| monetary_aggregates | `nsc_outstanding` | `amount_bdt_crore` | monthly | BB | [0.0, 5000000.0] | NSC outstanding |
| monetary_aggregates | `private_sector_credit` | `amount_bdt_crore` | monthly | BB | [0.0, 100000000.0] | Private Sector Credit |
| monetary_aggregates | `private_sector_credit_yoy_pct` | `percent` | monthly | BB | [-30.0, 50.0] | Private Sector Credit Growth YoY |
| monetary_aggregates | `reserve_money` | `amount_bdt_crore` | weekly | BB | [0.0, 10000000.0] | Reserve Money |
| monetary_aggregates (brief alias) | `banking_broad_money` | `amount_bdt_crore` | monthly | BB | [0.0, 30000000.0] | Alias of `broad_money` — Broad Money |
| monetary_aggregates (brief alias) | `banking_deposits` | `amount_bdt_crore` | monthly | BB | [0.0, 30000000.0] | Alias of `deposits_of_the_system` — Deposits of the system |
| monetary_aggregates (brief alias) | `banking_excess_liquid` | `amount_bdt_crore` | monthly | BB | [0.0, 5000000.0] | Alias of `excess_liquid_asset_total_minimum` — Excess Liquid Asset (Total-Minimum) |
| monetary_aggregates (brief alias) | `banking_money_multiplier` | `ratio` | monthly | BB | [1.0, 20.0] | Alias of `money_multiplier` — Money Multiplier |
| monetary_aggregates (brief alias) | `banking_reserve_money` | `amount_bdt_crore` | weekly | BB | [0.0, 10000000.0] | Alias of `reserve_money` — Reserve Money |
| monetary_aggregates (brief alias) | `macro_credit_growth` | `percent` | monthly | BB | [-30.0, 50.0] | Alias of `private_sector_credit_yoy_pct` — Private Sector Credit Growth YoY |
| monetary_aggregates (brief conversion) | `fiscal_nsc_outstanding` | `amount_bdt_crore` | monthly | BB | — | Conversion of `nsc_outstanding` × 1e-05 — NSC outstanding |
| money_market | `banking_sector_crar` | `percent` | quarterly | BB | [-50.0, 30.0] | Banking Sector CAR (Capital Adequacy Ratio) |
| money_market | `bb_repo_usage_cr` | `amount_bdt_crore` | daily | BB | [0.0, 200000.0] | BB Central-Bank Repo Usage (Repo accepted amount from BB auction press release, BDT crore; usage LEVEL only). DISTINCT from interbank_repo_data (bank-to-bank) — this is central-bank repo (BB lending to banks). BB has largely STOPPED routine daily repo lending (shifting to SLF/ALS), so on many days the Repo line is ABSENT: a null is returned and NO new row is written that day — never a stale carry-forward and never a fabricated measured 0. YieldScope MUST apply an as_of-freshness guard (it returns the most-recent EXISTING row, which on a no-repo day is weeks old). |
| money_market | `bill_bond_rates` | `percent` | daily | BB | [0.0, 25.0] | 91-Day T-Bill Cut-Off Yield |
| money_market | `call_money_rate` | `percent` | daily | BB | [0.0, 25.0] | Call money rate |
| money_market | `gross_npl_ratio` | `percent` | quarterly | BB | [0.0, 50.0] | Gross NPL Ratio (Banking Sector) |
| money_market | `gsec_auction` | `amount_bdt_crore` | daily | BB | [0.0, 50000.0] | Next GSEC Auction Notional |
| money_market | `interbank_repo_data` | `amount_bdt_crore` | daily | BB | [0.0, 100000.0] | Interbank Repo Data |
| money_market | `policy_rate_repo` | `percent` | monthly | BB | [3.0, 15.0] | Policy Rate (Repo) |
| money_market | `policy_rate_sdf` | `percent` | monthly | BB | [3.0, 12.0] | Policy Rate Corridor — SDF (floor) |
| money_market | `policy_rate_slf` | `percent` | monthly | BB | [4.0, 16.0] | Policy Rate Corridor — SLF (ceiling) |
| money_market | `slf_draw_cr` | `amount_bdt_crore` | daily | BB | [0.0, 200000.0] | SLF Drawdown (Standing Lending Facility accepted amount, BDT crore; usage/DRAW LEVEL only — SLF is uncapped-on-demand, BB publishes NO 'limit', so this is intentionally draw-only with NO 'vs limit' denominator. DAILY: one BB auction-result press release per business day; no new row is written on a day with no SLF print.) |
| money_market | `tbill_182d_yield` | `percent` | daily | BB | [0.0, 25.0] | 182-Day T-Bill Cut-Off Yield |
| money_market | `tbill_364d_yield` | `percent` | daily | BB | [0.0, 25.0] | 364-Day T-Bill Cut-Off Yield |
| money_market | `tbond_10y_yield` | `percent` | weekly | BB | [0.0, 25.0] | 10-Year BGTB Cut-Off Yield |
| money_market | `tbond_5y_yield` | `percent` | weekly | BB | [0.0, 25.0] | 5-Year BGTB Cut-Off Yield |
| money_market | `treasury_bill_outstanding` | `amount_bdt_mn` | monthly | BB | [0.0, 10000000.0] | Treasury Bill Outstanding |
| money_market | `treasury_bond_outstanding` | `amount_bdt_mn` | monthly | BB | [0.0, 50000000.0] | Treasury Bond Outstanding |
| money_market (brief alias) | `banking_call_money_rate` | `percent` | daily | BB | [0.0, 25.0] | Alias of `call_money_rate` — Call money rate |
| money_market (brief alias) | `banking_car_pct` | `percent` | quarterly | BB | [-50.0, 30.0] | Alias of `banking_sector_crar` — Banking Sector CAR (Capital Adequacy Ratio) |
| money_market (brief alias) | `banking_npl_pct` | `percent` | quarterly | BB | [0.0, 50.0] | Alias of `gross_npl_ratio` — Gross NPL Ratio (Banking Sector) |
| money_market (brief alias) | `gsec_next_auction_cr` | `amount_bdt_crore` | daily | BB | [0.0, 50000.0] | Alias of `gsec_auction` — Next GSEC Auction Notional |
| money_market (brief alias) | `tbill_91d_yield_pct` | `percent` | daily | BB | [0.0, 25.0] | Alias of `bill_bond_rates` — 91-Day T-Bill Cut-Off Yield |
| money_market (brief alias) | `tbond_bond_10y` | `percent` | weekly | BB | [0.0, 25.0] | Alias of `tbond_10y_yield` — 10-Year BGTB Cut-Off Yield |
| money_market (brief alias) | `tbond_bond_5y` | `percent` | weekly | BB | [0.0, 25.0] | Alias of `tbond_5y_yield` — 5-Year BGTB Cut-Off Yield |
| money_market (brief alias) | `tbond_tbill_182d` | `percent` | daily | BB | [0.0, 25.0] | Alias of `tbill_182d_yield` — 182-Day T-Bill Cut-Off Yield |
| money_market (brief alias) | `tbond_tbill_364d` | `percent` | daily | BB | [0.0, 25.0] | Alias of `tbill_364d_yield` — 364-Day T-Bill Cut-Off Yield |
| money_market (brief alias) | `tbond_tbill_91d` | `percent` | daily | BB | [0.0, 25.0] | Alias of `bill_bond_rates` — 91-Day T-Bill Cut-Off Yield |
| money_market (brief conversion) | `tbill_outstanding_cr` | `amount_bdt_crore` | monthly | BB | — | Conversion of `treasury_bill_outstanding` × 0.1 — Treasury Bill Outstanding |
| money_market (brief conversion) | `tbond_outstanding_cr` | `amount_bdt_crore` | monthly | BB | — | Conversion of `treasury_bond_outstanding` × 0.1 — Treasury Bond Outstanding |

