# Probabilistic Model for Non-Life Insurance Flood Risk

Two probabilistic models built on **real, public, uncleaned** U.S. flood insurance data from FEMA's National Flood Insurance Program (NFIP):

1. **Probability to claim**: the chance that an insured property files a flood claim during its policy term.
2. **Probability to cancel / lapse**: the chance that a policy is cancelled during its term or not renewed at the end.

> **Status:** in progress. Data gathering is done; data cleaning (notebook 02) is under way.

---

## What we predict, in one paragraph

Flood insurance is renewed every year, and FEMA records each year of coverage as its own row, called a **policy term**. We predict per **policy-year**: for each policy at the start of a coverage year, the probability that it **files a flood claim during that year**, and the probability that it **is cancelled during that year or not renewed at the end of it**. The same house is scored again each year, because its premium, flood map, claim history and storm exposure change from year to year. This is the same approach as a bank's credit model, which predicts default "in the next 12 months" and re-scores each customer every year.

**Example: one house, insured for four years, appears as four rows**

| Row | `policyEffectiveDate` | `policyTerminationDate` | What happened | `CLAIM_IN_TERM` | `CANCEL_IN_TERM` | `NON_RENEWAL` | `LAPSE` |
|---|---|---|---|---|---|---|---|
| 1 | 2019-06-01 | 2020-06-01 | New business | 0 | 0 | 0 | 0 |
| 2 | 2020-06-01 | 2021-06-01 | Renewed | 0 | 0 | 0 | 0 |
| 3 | 2021-06-01 | 2022-06-01 | Flood claim in Sept 2021 | 1 | 0 | 0 | 0 |
| 4 | 2022-06-01 | 2023-06-01 | Cancelled in Jan 2023 | 0 | 1 | 1 | 1 |

The two models use `CLAIM_IN_TERM` and `LAPSE`. `CANCEL_IN_TERM` and `NON_RENEWAL` are the two parts of `LAPSE` (section 4).

All four rows share the same `originalNBDate` (2019-06-01), the date the policy was first bought. That shared value is part of how the rows are linked back into one policy (section 5).

**How it would be used:** each month, score the policies coming up for renewal. High lapse risk points to retention outreach; high claim risk points to pricing and reserving review.

---

## 1. Why this project

- **Real data only.** All data comes from official FEMA, Census and FRED records; nothing is synthetic or deliberately degraded.
- **Naturally messy.** The raw files have heavy missingness, placeholder strings, dates stored as text, mixed codes, legend symbols inside date fields, rows that bundle several policies, and **no policy ID**. Cleaning and linking are a core part of the work.
- **Actuarially meaningful.** Claim probability (frequency) and lapse (persistency) are two central quantities in non-life pricing and portfolio management.

## 2. Data sources

| Dataset | Publisher | Grain | Use |
|---|---|---|---|
| [NFIP Redacted Policies v3](https://www.fema.gov/openfema-data-page/nfip-redacted-policies-v3) | OpenFEMA | One policy term (~1 year) | Core table: features and lapse target |
| [NFIP Redacted Claims v3](https://www.fema.gov/openfema-data-page/nfip-redacted-claims-v3) | OpenFEMA | One claim | Claim target, prior-claim features |
| Disaster Declarations Summaries v2 | OpenFEMA | County × event | Storm exposure context |
| NFIP Community Status Book v1 | OpenFEMA | NFIP community | Community status, map dates, CRS class |
| American Community Survey (ACS) 5-year, 2022 | U.S. Census Bureau | Census tract | Neighborhood income, home values, age (stand-in for redacted demographics) |
| FRED | Federal Reserve Bank of St. Louis | Day / week / month | Mortgage rate, 10-year Treasury, unemployment, CPI, home prices, fed funds |
| [IBTrACS v04r01](https://www.ncei.noaa.gov/products/international-best-track-archive) (since 1980) | NOAA NCEI | Storm track point (3-hourly) | Estimated tropical-cyclone wind at each policy location, from storms **before** the start |
| [CPC Unified gauge-based daily precipitation](https://psl.noaa.gov/data/gridded/data.unified.daily.conus.html), US 0.25° (V1.0 to 2006, real-time from 2007) and [global 0.5°](https://psl.noaa.gov/data/gridded/data.cpc.globalprecip.html) | NOAA CPC / PSL | Grid cell × day | Observed rainfall per location (notebook 02b): long-run climate 1981–2013, and rain in the 90 days to 3 years **before** each start |

**Scale:** about 74.7M policy terms (87 columns) and about 2.7M claims nationally. All files are downloaded in full and kept unchanged in `data/raw/`.

**Scope:** cleaning and linkage cover the full national data. The modeling population (notebook 04) is **all states, policy-years starting 2014-01-01 to 2024-08-31**, a window with both storm years (Matthew 2016, Harvey and Irma 2017, Florence 2018, Ida 2021, Ian 2022, Debby, Helene and Milton 2024) and calm years. August 2024 is the last start month whose claim outcome is fully known at the data cutoff (2026-09-08); we cut by start date, so the 2024 policy-years are not chosen by their outcome. Every policy-year is kept (no sampling).

### Data dictionaries

| Source | Where the dictionary comes from |
|---|---|
| FEMA (all four tables) | `OpenFemaDataSetFields` API: `https://www.fema.gov/api/open/v1/OpenFemaDataSetFields?$filter=openFemaDataSet eq '<name>' and datasetVersion eq <v>`; the same table appears on each dataset page |
| Census ACS | `https://api.census.gov/data/2022/acs/acs5/variables.json` |
| FRED | Each series page, e.g. `https://fred.stlouisfed.org/series/UNRATE` |

The dictionaries are pulled in `notebooks/00_data_dictionary.ipynb` and saved under `docs/`.

## 3. Levels of analysis

```
National (FRED, monthly)
└── County (disaster declarations)
    └── NFIP community (status, map dates, CRS class)
        └── Census tract (ACS, claim history)
            └── Policy  ← reconstructed by linking terms
                └── Policy term  ← one row of the NFIP policies table (unit of prediction)
```

There is **no policyholder (person) level**: NFIP redacts all personal data.

**Unit of prediction: the policy term (policy-year).** The policy level is used in a supporting role: to build history features (tenure, prior claims, premium change), the non-renewal label, and the optional survival model.

## 4. Targets

We build **two models**, one target each:

| Model | Target | Definition | Base rate (all states, 2014–2023 starts) |
|---|---|---|---|
| Claim | `CLAIM_IN_TERM` | 1 if a linked claim's `dateOfLoss` falls inside the policy-year | 0.5–1.3% in calm years, 2.1–2.6% in the storm years 2016–2017 |
| Lapse | `LAPSE` | 1 if the policy-year is **cancelled for a lapse reason** during the year **or not renewed** at its end; 0 if neither; unknown (NULL) if either part is still unknown | about 12–20%, mostly non-renewal |

`LAPSE` combines two parts, which we keep in the data to see which one drives a finding:

| Part | Definition | Base rate |
|---|---|---|
| `CANCEL_IN_TERM` | 1 if `cancellationDateOfFloodPolicy` falls inside the policy-year **and** the reason code is a lapse (see below) | 0.09–0.32% a year |
| `NON_RENEWAL` | 1 if the policy-year is the last of its term and no linked term for the same policy starts within ±60 days of its end; 0 in the inner years of a multi-year term | about 12–20% of renewal decisions |

Notes on the targets:
- **Flat cancellations** (cancelled on or before the start date, mostly unpaid renewals) are not in force: they never become a policy-year. An unpaid renewal therefore shows up as `NON_RENEWAL` of the year before it.
- **Censoring:** a policy-year whose outcome cannot be seen by the data cutoff (2026-09-08) gets NULL, not 0: claims within 365 days of the cutoff, and renewals whose ±60-day window is not over. NULL rows are left out of the population.
- **Linkage:** `CLAIM_IN_TERM` and `NON_RENEWAL` (and so `LAPSE`) depend on the reconstructed policy key (section 5) and carry its linkage error. Rows with an incomplete or colliding key are left out of both populations.
- **Left out of the lapse population:** the policy-year in which a rewrite or a sale ended the term (neither a lapse nor a normal continuation), and terms in communities suspended or withdrawn from the NFIP (their non-renewal may be forced).

**Survival extension (notebook 07b):** a discrete-time hazard model on the term rows, i.e. "given the policy has survived to this tenure year, does it lapse in this term?", with tenure as a feature. Policies still active at the cutoff are censored, and policies that began before 2014 enter at their true tenure (computed from `originalNBDate`), not at year 1.

### Cancellation reason codes

`cancellationVoidanceReasonCode` is decoded from the FEMA dictionary. Not every cancellation is a lapse:

| Category | Codes | Treatment |
|---|---|---|
| Voluntary or financial attrition | 05 non-payment, 28 lender no longer requires insurance, 52 mortgage paid off, 30 insufficient premium | **Lapse** |
| Property sold or removed | 01, 02 | **Not lapse**: kept as its own flag (`SOLD_REMOVED_IN_TERM`); the policy-year it ended is left out of the lapse population |
| Rewrite | 03, 22 misrating, 24 rewrite to Preferred Risk Policy after map revision, 25 HFIAA refund | **Not lapse**: the policy continues |
| Duplicate | 04, 17, 26 | **Not lapse** |
| Voidance | 60, 70 | **Not lapse** |
| Eligibility or other | 06, 08, 09, 10, 16, 20, 21, 23, 27, 29, 45, 50, UN | Not a lapse cancellation, but if no new term follows, the policy-year counts as `NON_RENEWAL` (so `LAPSE` = 1) |

In the Beaufort County, SC pilot, two of the three most common codes (24 and 22) were rewrites, so treating every cancellation as lapse would overstate attrition.

## 5. The key problem: no policy ID

**`id` is not a policy ID.** Both FEMA tables have an `id` column that the dictionary marks as the primary key, but it identifies the **record**: each policy term, and each claim, gets its own `id`. The same house's 2019 and 2020 terms have different `id`s, and nothing in `id` connects a claim to a policy. FEMA removes the real policy number for privacy. `id` is used only to check for duplicate records.

Policies and claims share a set of fields that together approximate a property key:

`originalNBDate` + `originalConstructionDate` + `censusGeoid` + `reportedZipCode` + `numberOfFloorsInInsuredBuilding`

`occupancyType` was dropped from the key (notebook 03, Step 6.2): Risk Rating 2.0 replaced its 1-digit codes with 2-digit codes, and the claims file uses the new codes even for older losses, so one house got two keys. Without it, matched claims rose from 72% to 96%, while collisions rose only from 3.2% to 3.4%.

(The claims table spells the last field `numberOfFloorsInTheInsuredBuilding`; it is renamed during cleaning.)

- **Term → policy:** group terms on the key and order them by effective date.
- **Claim → term:** match on the key, with `dateOfLoss` inside the term window.
- **Quality checks:** collision rate (keys with more than one term starting in the same year), match rate, and a sensitivity analysis on alternative keys. In the Beaufort County pilot, about 3% of keys collided.

## 6. Features (initial list)

| Group | Examples |
|---|---|
| Price | Premium, total policy cost, year-over-year premium change, Risk Rating 2.0 transition |
| Obligation | `mandatoryPurchaseFlag` (mortgage-required coverage) |
| Flood risk | Rated and current flood zone, elevation difference, base flood elevation, foundation type, elevated building |
| Coverage | Building and contents coverage, deductible codes, replacement cost |
| Property | Construction year, floors, occupancy group (old and Risk Rating 2.0 codes mapped together), primary residence, rental, condo type |
| Tenure | Months since `originalNBDate`, number of prior terms |
| History | Prior claims on the same property (count, amount, recency) |
| Community | CRS class, map dates, suspended or withdrawn status |
| Context | Disaster declarations in the previous 12 months, tropical-cyclone wind at the property in the previous 10 years (IBTrACS), long-run rainfall, 100-year 1-day rainfall and rain in the 90 days to 3 years before the start (NOAA CPC), tract income (ACS), mortgage rates (FRED) |

Many integer columns (`occupancyType`, `obstructionType`, `crsClassCode`, `locationOfContents`, …) are **codes**, not quantities, and are treated as categorical.

**Leakage guard:** only information available at the **start** of the term goes into the features. Cancellation fields and claim amounts from the current term are never used as features.

## 7. Tools

- **DuckDB** for the four FEMA tables. The policies file does not fit in memory with pandas (an estimated 50–150 GB once loaded), so DuckDB queries the Parquet files in place through views, reads only the needed columns, uses all cores, and spills to disk.
- **pandas** for ACS and FRED, which are small, and for any query result once it is small.
- Rule of thumb: DuckDB on the full data, pandas on the results.

## 8. Notebooks and methodology

| Notebook | Purpose |
|---|---|
| `00_data_dictionary` | Pull and save every source's data dictionary; build the type map |
| `01_data_understanding` | Download all raw data in full; verify files and row counts |
| `02_data_cleaning` | Data-quality audit (dtypes, statistics, placeholders, invalid codes, plausibility); cleaning rules for every table; cleaning log; export clean Parquet |
| `02b_precipitation` | Observed daily rainfall (NOAA CPC Unified): download, location → grid cell, data checks, static climate features (1981–2013, 100-year 1-day rainfall by GEV with L-moments) and dated features before every start date |
| `03_linkage_and_targets` | Decode cancellation codes, build the policy key, link terms and claims, report linkage quality, define and validate the targets, handle censoring |
| `04_modeling_population_and_split` | Choose the modeling scope and exclusions (row-count waterfall), sample if needed, time-based train / validation / test split, rolling-origin folds |
| `05_eda_and_feature_engineering` | EDA and features on **training data only**; join disaster, storm-wind (IBTrACS), rainfall (notebook 02b), ACS, community and FRED features; leakage audit; missing-value and encoding plan |
| `06a_baseline_claim` | Naive benchmark and logistic regression for `CLAIM_IN_TERM` |
| `06b_baseline_cancel` | Naive benchmark and logistic regression for the lapse target |
| `07a_advanced_models_claim` | Gradient boosting (LightGBM or XGBoost), tuning, calibration; severity model for expected loss |
| `07b_advanced_models_cancel` | Gradient boosting, tuning, calibration; discrete-time hazard or survival model |
| `08_evaluation_benchmarking_monitoring` | Final scoring of the frozen models on the validation holdout (out-of-time 2, opened once; dry run on test first); benchmark table, confusion matrices and ROC curves, calibration (A/E, ECE, slope), calibration by segment and storm exposure, stability by year, cohort survival analysis for lapse (retention of new-business cohorts, actual vs predicted), simulated monitoring (PSI, A/E, alert rules); appendix: bootstrap of the gains, policy-history sensitivity |
| `09_explainability_and_report` | SHAP and partial dependence, example explanations, business use, limitations, conclusions |

**Split:** by time, each split after the one before it: **train** 2014–2020; **test** 2021–2022, used to tune and compare models; **validation** 2023 to August 2024, the final holdout, used once in notebook 08. (We name the tuning split "test" and the final holdout "validation"; many ML texts use the opposite names.) Training includes the 2016–2017 storm years, tuning includes Ian (2022), and the final holdout has a calm year (2023) and a storm year (2024). Rolling-origin folds inside 2014–2020 (checking 2017, 2018, 2019 and 2020) are used for tuning, because storm years dominate claims; the holdout years are never used for tuning.
**In-time and out-of-time:** the training years are the development sample; the rolling-origin folds inside 2014–2020 are the in-time validation used for tuning; test (2021–2022) is out-of-time 1 and validation (2023 to August 2024) is out-of-time 2 (notebooks 06a/06b, Step 1.2).
**Metrics:** two views. As classifiers: confusion matrices with accuracy, precision, recall and F1 at fixed cut-offs (claims: the riskiest 5/10/20% of policy-years; lapse: 10/20/30%; and the default 0.5, which flags almost nobody for a 1% event), and ROC curves with their AUC. As probability models: log loss (also used for tuning), Brier score, skill over the base rate, calibration by decile, and expected vs actual counts.

## 9. Data-quality findings so far

From the audit in notebook 02 and earlier pilot work. None of these issues were injected.

- **High missingness:** `fullRiskPremium` ~65%, `baseFloodElevation` ~64%, `elevationCertificateIndicator` ~73%, `premiumPaymentIndicator` ~69%, `floodZoneCurrent` ~28% (pilot county figures).
- **Placeholder strings:** the literal string `"None"` mixed with real nulls, hiding missing values from standard null counts.
- **Policies storage types are sound:** the Parquet types (SMALLINT, DECIMAL, TIMESTAMP WITH TIME ZONE) already match the dictionary. Only type names differ.
- **Community Status Book dates are text:** `currentlyEffectiveMapDate`, `regularEmergencyProgramDate` and `sanctionDate` are stored as `VARCHAR` with **two-digit years** (`09/25/09`) and **legend codes mixed into the field**: `(NSFHA)`, `(>)`, `E`, `N/A`, `(S)`, `(W)`, `(M)`, `(L)`. Each code is turned into its own flag column rather than discarded.
- **Numbers stored as text:** `classRating` (CRS class) is `VARCHAR`.
- **Coarse locations:** latitude and longitude are rounded to 1 decimal place (~11 km) by FEMA's redaction.
- **Constant columns:** `reportedCity` is always `"NA"`.
- **Multi-unit rows:** `policyCount` > 1 (condominium master policies) means one row covers several units.
- **No entity key**, so term-to-term linkage is a cleaning step that has to be designed.

Every rule is recorded in `reports/cleaning_log.csv` with the table, column, rule, reason and rows affected. Raw and clean audits are saved side by side in `reports/`.

## 10. Known risks and limitations

- **Linkage error** in the reconstructed policy key affects the non-renewal label and the claim matching.
- **Weather drives claims.** A model trained on a storm year may not carry over to calmer years, so the time-based split and disaster features matter.
- **Class imbalance** for claims (about 1%): we keep the real claim rate (no class weights), read precision, recall and F1 at cut-offs that flag the riskiest policy-years rather than accuracy at 0.5, and check the probabilities with log loss, Brier score and calibration.
- **No personal or behavioral data:** no demographics, payment history or contact data.
- **Data volume:** handled out of core with DuckDB and Parquet.
- **Redaction:** coordinates are rounded and some fields are generalized.

## 11. Repository structure

```
├── data/
│   ├── raw/          # untouched downloads (gitignored)
│   ├── interim/      # DuckDB database and temporary files (gitignored)
│   └── processed/    # cleaned and modeling tables (gitignored)
├── docs/             # data dictionaries, field mapping, cleaning decisions
├── notebooks/        # 00–09, see section 8
├── src/              # shared functions, imported by the notebooks (documented with docstrings)
│   ├── paths.py      # folder locations, correct from any working directory
│   ├── db.py         # DuckDB connection and raw views
│   ├── download.py   # downloads (used by notebook 01; also a command-line script)
│   ├── dictionary.py # allowed codes and types from the data dictionaries
│   ├── dates.py      # MM/DD/YY text dates, legend-code flags, FRED dates
│   ├── dq.py         # data-quality checks and the cleaning log
│   ├── clean.py      # cleaned views: column cleaning, de-duplication, dq_ flags
│   ├── geo.py        # state FIPS codes and geography keys
│   ├── features.py   # feature list and the SQL that builds each group
│   └── precip.py     # rainfall features from NOAA CPC Unified (notebook 02b)
├── reports/          # audits, cleaning log, figures, final report
├── requirements.txt
└── README.md
```

## 12. Milestones

| # | Milestone | Output | Status |
|---|---|---|---|
| 1 | Download raw data | Parquet and CSV files in `data/raw/` | Done |
| 2 | Data dictionaries | Dictionary files in `docs/` | Done |
| 3 | Data-quality audit and cleaning | Audit tables, cleaning log, clean Parquet | In progress |
| 4 | Linkage and targets | Linkage quality report, labeled tables | Not started |
| 5 | Modeling population and split | Row-count waterfall, split assignments | Not started |
| 6 | EDA and features | Driver analysis, feature tables | Not started |
| 7 | Baselines | Logistic models for claim and lapse | Not started |
| 8 | Advanced models | Calibrated gradient boosting and survival models | Not started |
| 9 | Evaluation and report | Benchmarks, monitoring, explanations, final report | Not started |

## 13. Reproducibility

- Raw data is never edited by hand. Every transformation is code plus a logged rule.
- Fixed random seeds; pinned package versions in `requirements.txt`.
- Anyone can re-download the data from OpenFEMA, Census and FRED by running notebook 01.
- The Census API key is read from a local `.env` file, which must never be committed.

## License and data credit

Data: FEMA OpenFEMA, U.S. Census Bureau and FRED, all public U.S. government or public-domain sources. This product uses the FEMA OpenFEMA API but is not endorsed by FEMA or the federal government.
