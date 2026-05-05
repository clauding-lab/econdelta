# Macro Observer JSON shape — captured 2026-05-05

**File:** `scripts/_seed_data/macro_monthly_data.json`
**Size:** 47,676 bytes (~47 KB)
**Top-level type:** dict
**Top-level key count:** 46 (2 metadata + 44 data series)

---

## Observed top-level keys

| key | type | length | first non-null value |
|---|---|---|---|
| labels | list | 173 | `"Jan'12"` |
| months | list | 173 | `"2012-01"` |
| gen12M | list | 172 | 10.91 |
| food12M | list | 172 | 12.73 |
| nonFood12M | list | 172 | 7.61 |
| genP2P | list | 172 | 11.59 |
| foodP2P | list | 172 | 10.9 |
| nonFoodP2P | list | 172 | 13.16 |
| repo | list | 172 | 7.25 (sparse — nulls for earlier months) |
| tbill364 | list | 172 | 10.88 |
| tb91 | list | 172 | 10.5 |
| tb182 | list | 172 | 10.63 |
| tr2y | list | 172 | 10.88 |
| tr5y | list | 172 | 9.0 |
| tr10y | list | 172 | 11.25 |
| tr15y | list | 172 | 11.5 |
| tr20y | list | 172 | 11.95 |
| depRate | list | 172 | 6.21 |
| lendRate | list | 172 | 11.05 |
| pubCredit | list | 172 | 1069.4 |
| privCredit | list | 172 | 3748.6 |
| m1 | list | 172 | 980.3 |
| m2 | list | 172 | 4737.0 |
| nfaBB | list | 172 | 617.4 |
| ndaBB | list | 172 | 362.9 |
| nfaBank | list | 172 | 697.0 |
| ndaBank | list | 172 | 4040.1 |
| nfaBankGrowth | list | 172 | 43.87 |
| ndaBankGrowth | list | 172 | 14.41 |
| fxReserve | list | 172 | 9.39 |
| fxBPM6 | list | 172 | 36.86 |
| importCov | list | 172 | 2.81 |
| bdtUsd | list | 173 | 77.87 |
| reer | list | 173 | 110.63 |
| neer | list | 172 | 93.53 |
| dsex | list | 172 | 4127.0 |
| dsexGrowth | list | 172 | 15.18 |
| domCredit | list | 172 | 4818.0 |
| domCreditGr | list | 172 | 14.27 |
| privCreditGr | list | 172 | 14.83 |
| pubCreditGr | list | 172 | 12.34 |
| m1Gr | list | 172 | 8.38 |
| m2Gr | list | 172 | 18.74 |
| expUsd | list | 172 | 2149.9 |
| impUsd | list | 172 | 3346.0 |
| remUsd | list | 172 | 1221.4 |

---

## Date format

Observed format: `YYYY-MM` (e.g. `"2012-01"`, `"2012-02"`) — **no day component**.

The `months` array has 173 entries covering Jan 2012 through the most recent month. The `labels` array is a human-readable parallel (`"Jan'12"`, `"Feb'12"`, etc.) used for chart axis labels — do not use `labels` for database insertion; use `months`.

Always day-1 of month? Not applicable — the date strings are `YYYY-MM` only, not `YYYY-MM-DD`. The seed script must append `-01` when constructing `as_of` dates for Supabase.

---

## Multi-tenor entries

**There is no single `yield_curve` key.** The spec's assumption of a dict-of-tenor structure is wrong. Each tenor is its own flat parallel array at the top level:

| upstream key | tenor | notes |
|---|---|---|
| tb91 | 91-day T-bill | not in spec |
| tb182 | 182-day T-bill | not in spec |
| tbill364 | 364-day T-bill | in spec as `tbill_364d` |
| tr2y | 2-year bond | in spec as part of `yield_curve` |
| tr5y | 5-year bond | in spec as part of `yield_curve` |
| tr10y | 10-year bond | in spec as part of `yield_curve` |
| tr15y | 15-year bond | not in spec (spec had 1y, 2y, 5y, 10y, 20y) |
| tr20y | 20-year bond | in spec as part of `yield_curve` |

**No 1-year bond tenor exists** (`tr1y` is absent). The spec's `yield_{1y}_monthly` metric_id has no upstream source.

---

## KEY_MAP_ADJUSTMENTS

| spec key | actual key | resolution | notes |
|---|---|---|---|
| cpi_p2p_general | `genP2P` | RENAME → genP2P | spec assumed `cpi_p2p_general` |
| cpi_p2p_food | `foodP2P` | RENAME → foodP2P | |
| cpi_p2p_nonfood | `nonFoodP2P` | RENAME → nonFoodP2P | |
| cpi_12m_general | `gen12M` | RENAME → gen12M | spec assumed `cpi_12m_general` |
| repo_rate | `repo` | RENAME → repo | sparse — nulls before mid-2010s; 136 non-null values |
| tbill_364d | `tbill364` | RENAME → tbill364 | |
| yield_curve (1y tenor) | _(absent)_ | MISSING | no `tr1y` key exists; drop `yield_1y_monthly` from KEY_MAP |
| yield_curve (2y tenor) | `tr2y` | RENAME → tr2y | flat array, not nested dict |
| yield_curve (5y tenor) | `tr5y` | RENAME → tr5y | flat array, not nested dict |
| yield_curve (10y tenor) | `tr10y` | RENAME → tr10y | flat array, not nested dict |
| yield_curve (20y tenor) | `tr20y` | RENAME → tr20y | flat array, not nested dict |
| real_policy_rate | _(absent)_ | MISSING | not a raw series — must be computed: `repo` − `genP2P` (or similar). No upstream key. Either compute on ingest or skip for v1. |
| domestic_credit_total | `domCredit` | RENAME → domCredit | |
| domestic_credit_public | `pubCredit` | RENAME → pubCredit | |
| domestic_credit_private | `privCredit` | RENAME → privCredit | |
| private_credit_growth_yoy | `privCreditGr` | RENAME → privCreditGr | |
| public_credit_growth_yoy | `pubCreditGr` | RENAME → pubCreditGr | |
| m1_growth_yoy | `m1Gr` | RENAME → m1Gr | |
| m2_growth_yoy | `m2Gr` | RENAME → m2Gr | |
| exports_usd_mn | `expUsd` | RENAME → expUsd | |
| imports_usd_mn | `impUsd` | RENAME → impUsd | |
| remittance_usd_mn | `remUsd` | RENAME → remUsd | |
| fx_reserves_gross_bn | `fxReserve` | RENAME → fxReserve | note: `fxBPM6` also present (BPM6-methodology reserve); `fxReserve` is the gross figure the spec wants |
| import_cover_months | `importCov` | RENAME → importCov | |
| bdt_usd | `bdtUsd` | RENAME → bdtUsd | |
| reer | `reer` | OK | key name matches |
| dsex | `dsex` | OK | key name matches |
| dsex_turnover | _(absent)_ | MISSING → dsexGrowth substitution | no turnover (volume) series; `dsexGrowth` (YoY % growth) exists instead — either map to `dsex_growth_yoy_monthly` or drop |

**Summary counts:**
- OK: 2 (reer, dsex)
- RENAME: 19 (all others with upstream matches)
- MISSING: 3 (yield_1y, real_policy_rate, dsex_turnover)
- UNEXPECTED (in JSON, not in spec) — see next section

---

## Unexpected upstream keys (in JSON but not in spec)

| actual key | description | recommendation |
|---|---|---|
| `labels` | Human-readable axis labels (`"Jan'12"`) | Skip — metadata only, use `months` for `as_of` |
| `months` | Machine date strings (`YYYY-MM`) | Skip — use as the date index, not a data series |
| `food12M` | CPI 12-month avg — food | Add to KEY_MAP → `cpi_12m_food_monthly` (useful for completeness alongside `nonFood12M`) |
| `nonFood12M` | CPI 12-month avg — non-food | Add to KEY_MAP → `cpi_12m_nonfood_monthly` |
| `tb91` | 91-day T-bill yield | Skip v1 (no spec chart), flag for future |
| `tb182` | 182-day T-bill yield | Skip v1 |
| `tr15y` | 15-year bond yield | Skip v1 (spec had 1y/2y/5y/10y/20y but not 15y) |
| `depRate` | Deposit rate | Skip v1 — interesting but no spec chart |
| `lendRate` | Lending rate | Skip v1 |
| `nfaBB` | Net foreign assets — Bangladesh Bank | Skip v1 |
| `ndaBB` | Net domestic assets — Bangladesh Bank | Skip v1 |
| `nfaBank` | Net foreign assets — banking system | Skip v1 |
| `ndaBank` | Net domestic assets — banking system | Skip v1 |
| `nfaBankGrowth` | NFA growth YoY | Skip v1 |
| `ndaBankGrowth` | NDA growth YoY | Skip v1 |
| `fxBPM6` | Gross reserves (BPM6 methodology) | Skip v1 — use `fxReserve` per spec |
| `neer` | Nominal effective exchange rate | Skip v1 |
| `dsexGrowth` | DSEX YoY % growth | Candidate substitute for `dsex_turnover` — rename as `dsex_growth_yoy_monthly` |
| `domCreditGr` | Domestic credit growth YoY | Add → `domestic_credit_growth_yoy_monthly` (useful alongside public/private growth) |

---

## Recommendations for Task 5 (seed_macro_monthly.py implementation)

1. **Date construction:** `months` values are `YYYY-MM` strings (no day). When writing to Supabase `as_of`, append `-01`: `f"{month}-01"`. Do not use the `labels` array.

2. **No nested yield_curve dict:** Replace the spec's dict-of-tenor approach with 5 separate flat key lookups: `tr2y`, `tr5y`, `tr10y`, `tr20y` → metric_ids `yield_2y_monthly`, `yield_5y_monthly`, `yield_10y_monthly`, `yield_20y_monthly`. Drop `yield_1y_monthly` (no upstream source).

3. **real_policy_rate is a derived series:** It does not exist as a raw upstream key. Either compute it inline as `repo[i] - genP2P[i]` (with null propagation) or exclude from v1 seed. Recommend computing it — both inputs exist.

4. **Null values are common:** Many series have `null` entries (especially `repo`, early years of `bdtUsd`, `reer`). The seed script must skip rows where value is null rather than writing `None` to Supabase, unless the schema explicitly allows nulls and downstream charting handles gaps.

5. **fxReserve vs fxBPM6:** Two reserve series exist. The spec maps to `fx_reserves_gross_bn` → use `fxReserve`. The BPM6 figure (`fxBPM6`) is larger and starts later — skip for v1 but worth noting for future auditors.

6. **dsex_turnover has no upstream match:** The closest available series is `dsexGrowth` (YoY % growth, not turnover volume). If the spec chart truly wants turnover (BDT volume), the data is absent. Recommend either renaming the metric_id to `dsex_growth_yoy_monthly` or dropping the chart for v1.

7. **Parallel list alignment:** All series arrays are parallel to `months` (173 entries). Most data series have 172 entries — off by one vs `months`. Inspect carefully: the last `months` entry likely has data added but the series are trailing by one month. Zip `months[:-1]` or `months` carefully and check alignment per-series before writing.
