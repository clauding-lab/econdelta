# Indicator catalog

**Generated** by `scripts/build_catalog.py` from `config/sources-v3.json` + `aggregate_latest.BRIEF_ALIASES` + `aggregate_latest.BRIEF_CONVERSIONS` plus a manually-curated list of derived/cross-source keys. Re-run after adding indicators:

```bash
python3 scripts/build_catalog.py > docs/indicator-catalog.md
```

**59** scraped indicators × **30** brief aliases × **9** unit conversions × **2** derived = **100** total entries.

Read the data contract for column semantics and query examples: [`data-contract.md`](data-contract.md).

---

| Section | metric_id | Unit | Cadence | Source | Valid range | Description |
|---------|-----------|------|---------|--------|-------------|-------------|
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
| derived (cross-source) | `nbr_fytd_collected_cr` | `amount_bdt_crore` | monthly | — | — | NBR fiscal-year-to-date collection — confirmed mean of TBS and Daily Star sources when within 5% tolerance, else the larger figure. |
| derived (cross-source) | `nbr_fytd_cross_check` | `string` | monthly | — | — | Cross-check status for nbr_fytd_collected_cr: 'confirmed', 'mismatch_X.X%', 'tbs_only', or 'dailystar_only'. Strings only land in latest.json — NOT in metric_history (writer filters strings). |
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
| government_finance | `domestic_borrowing_for_budget_deficit` | `amount_bdt_crore` | monthly | BB | [0.0, 400000.0] | Domestic Borrowing for Budget Deficit |
| government_finance | `foreign_borrowing_for_budget_deficit` | `amount_bdt_crore` | monthly | BB | [0.0, 200000.0] | Foreign Borrowing for Budget Deficit |
| government_finance | `nbr_fytd_collected_dailystar` | `amount_bdt_crore` | monthly | Daily Star | [50000.0, 1000000.0] | NBR FYTD Collection — Daily Star (BDT crore) |
| government_finance | `nbr_fytd_collected_tbs` | `amount_bdt_crore` | monthly | TBS | [50000.0, 1000000.0] | NBR FYTD Collection — TBS (BDT crore) |
| government_finance | `non_bank_borrowing_for_deficit_financing` | `amount_bdt_crore` | monthly | BB | [0.0, 200000.0] | Non-bank borrowing for Deficit Financing |
| government_finance | `non_tax_revenue` | `amount_bdt_crore` | monthly |  | [0.0, 100000.0] | Non-Tax Revenue |
| government_finance | `rev_gdp_ratio` | `percent` | quarterly |  | [0.0, 40.0] | Rev-GDP Ratio |
| government_finance | `tax_gdp_ratio` | `percent` | quarterly |  | [0.0, 30.0] | Tax-GDP Ratio |
| government_finance | `tax_revenue` | `amount_bdt_crore` | monthly | BB | [0.0, 500000.0] | Tax Revenue |
| government_finance | `total_revenue_budget_vs_actual` | `amount_bdt_crore` | monthly |  | [0.0, 600000.0] | Total Revenue Budget vs Actual |
| government_finance (brief conversion) | `fiscal_bank_borrow_trn` | `amount_bdt_crore` | monthly | BB | — | Conversion of `bank_borrowing_for_deficit_financing` × 1e-05 — Bank Borrowing for Deficit Financing |
| government_finance (brief conversion) | `fiscal_foreign_borrow_trn` | `amount_bdt_crore` | monthly | BB | — | Conversion of `foreign_borrowing_for_budget_deficit` × 1e-05 — Foreign Borrowing for Budget Deficit |
| government_finance (brief conversion) | `fiscal_govt_borrow_trn` | `amount_bdt_crore` | monthly | BB | — | Conversion of `domestic_borrowing_for_budget_deficit` × 1e-05 — Domestic Borrowing for Budget Deficit |
| government_finance (brief conversion) | `fiscal_nbr_collected_trn` | `amount_bdt_crore` | monthly | BB | — | Conversion of `tax_revenue` × 1e-05 — Tax Revenue |
| inflation | `food_inflation` | `percent` | monthly | BB | [0.0, 50.0] | Food Inflation |
| inflation | `general_inflation` | `percent` | monthly | BB | [0.0, 50.0] | General Inflation |
| inflation | `non_food_inflation` | `percent` | monthly | BB | [0.0, 50.0] | Non-Food Inflation |
| inflation | `point_to_point_inflation` | `percent` | monthly | BB | [0.0, 50.0] | Point to Point Inflation |
| inflation (brief alias) | `macro_cpi_food` | `percent` | monthly | BB | [0.0, 50.0] | Alias of `food_inflation` — Food Inflation |
| inflation (brief alias) | `macro_cpi_headline` | `percent` | monthly | BB | [0.0, 50.0] | Alias of `general_inflation` — General Inflation |
| inflation (brief alias) | `macro_cpi_nonfood` | `percent` | monthly | BB | [0.0, 50.0] | Alias of `non_food_inflation` — Non-Food Inflation |
| macro | `gdp` | `amount_bdt_crore` | quarterly | BB | [0.0, 100000000.0] | GDP |
| monetary_aggregates | `broad_money` | `amount_bdt_crore` | monthly | BB | [0.0, 30000000.0] | Broad Money |
| monetary_aggregates | `currency_outside_bank` | `amount_bdt_crore` | monthly | BB | [0.0, 5000000.0] | Currency Outside Bank |
| monetary_aggregates | `deposits_held_with_bb_crr` | `amount_bdt_crore` | monthly | BB | [0.0, 5000000.0] | Deposits held with BB (CRR) |
| monetary_aggregates | `deposits_of_the_system` | `amount_bdt_crore` | monthly | BB | [0.0, 30000000.0] | Deposits of the system |
| monetary_aggregates | `excess_liquid_asset_total_minimum` | `amount_bdt_crore` | monthly | BB | [0.0, 5000000.0] | Excess Liquid Asset (Total-Minimum) |
| monetary_aggregates | `money_multiplier` | `ratio` | monthly | BB | [1.0, 20.0] | Money Multiplier |
| monetary_aggregates | `nsc_outstanding` | `amount_bdt_crore` | monthly | BB | [0.0, 5000000.0] | NSC outstanding |
| monetary_aggregates | `private_sector_credit` | `amount_bdt_crore` | monthly | BB | [0.0, 100000000.0] | Private Sector Credit |
| monetary_aggregates | `reserve_money` | `amount_bdt_crore` | weekly | BB | [0.0, 10000000.0] | Reserve Money |
| monetary_aggregates (brief alias) | `banking_broad_money` | `amount_bdt_crore` | monthly | BB | [0.0, 30000000.0] | Alias of `broad_money` — Broad Money |
| monetary_aggregates (brief alias) | `banking_deposits` | `amount_bdt_crore` | monthly | BB | [0.0, 30000000.0] | Alias of `deposits_of_the_system` — Deposits of the system |
| monetary_aggregates (brief alias) | `banking_excess_liquid` | `amount_bdt_crore` | monthly | BB | [0.0, 5000000.0] | Alias of `excess_liquid_asset_total_minimum` — Excess Liquid Asset (Total-Minimum) |
| monetary_aggregates (brief alias) | `banking_money_multiplier` | `ratio` | monthly | BB | [1.0, 20.0] | Alias of `money_multiplier` — Money Multiplier |
| monetary_aggregates (brief alias) | `banking_reserve_money` | `amount_bdt_crore` | weekly | BB | [0.0, 10000000.0] | Alias of `reserve_money` — Reserve Money |
| monetary_aggregates (brief conversion) | `fiscal_nsc_outstanding` | `amount_bdt_crore` | monthly | BB | — | Conversion of `nsc_outstanding` × 1e-05 — NSC outstanding |
| money_market | `banking_sector_crar` | `percent` | quarterly | BB | [-50.0, 30.0] | Banking Sector CAR (Capital Adequacy Ratio) |
| money_market | `bill_bond_rates` | `percent` | daily | BB | [0.0, 25.0] | 91-Day T-Bill Cut-Off Yield |
| money_market | `call_money_rate` | `percent` | daily | BB | [0.0, 25.0] | Call money rate |
| money_market | `gross_npl_ratio` | `percent` | quarterly | BB | [0.0, 50.0] | Gross NPL Ratio (Banking Sector) |
| money_market | `gsec_auction` | `amount_bdt_crore` | daily | BB | [0.0, 50000.0] | Next GSEC Auction Notional |
| money_market | `interbank_repo_data` | `amount_bdt_crore` | daily | BB | [0.0, 100000.0] | Interbank Repo Data |
| money_market | `policy_rate_slf_sdf` | `percent` | daily | BB | [0.5, 25.0] | Policy Rate, SLF, SDF |
| money_market | `treasury_bill_outstanding` | `amount_bdt_mn` | monthly | BB | [0.0, 10000000.0] | Treasury Bill Outstanding |
| money_market | `treasury_bond_outstanding` | `amount_bdt_mn` | monthly | BB | [0.0, 50000000.0] | Treasury Bond Outstanding |
| money_market (brief alias) | `banking_call_money_rate` | `percent` | daily | BB | [0.0, 25.0] | Alias of `call_money_rate` — Call money rate |
| money_market (brief alias) | `banking_car_pct` | `percent` | quarterly | BB | [-50.0, 30.0] | Alias of `banking_sector_crar` — Banking Sector CAR (Capital Adequacy Ratio) |
| money_market (brief alias) | `banking_npl_pct` | `percent` | quarterly | BB | [0.0, 50.0] | Alias of `gross_npl_ratio` — Gross NPL Ratio (Banking Sector) |
| money_market (brief alias) | `gsec_next_auction_cr` | `amount_bdt_crore` | daily | BB | [0.0, 50000.0] | Alias of `gsec_auction` — Next GSEC Auction Notional |
| money_market (brief alias) | `tbill_91d_yield_pct` | `percent` | daily | BB | [0.0, 25.0] | Alias of `bill_bond_rates` — 91-Day T-Bill Cut-Off Yield |
| money_market (brief alias) | `tbond_tbill_91d` | `percent` | daily | BB | [0.0, 25.0] | Alias of `bill_bond_rates` — 91-Day T-Bill Cut-Off Yield |
| money_market (brief conversion) | `tbill_outstanding_cr` | `amount_bdt_crore` | monthly | BB | — | Conversion of `treasury_bill_outstanding` × 0.1 — Treasury Bill Outstanding |
| money_market (brief conversion) | `tbond_outstanding_cr` | `amount_bdt_crore` | monthly | BB | — | Conversion of `treasury_bond_outstanding` × 0.1 — Treasury Bond Outstanding |

