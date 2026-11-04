# Probabilistic Model for Non-Life Insurance Flood Risk

Two probabilistic models built on **real, public, uncleaned** U.S. flood insurance data from FEMA's National Flood Insurance Program (NFIP):

1. **Probability to claim**: the chance that an insured property files a flood claim during its policy term.
2. **Probability to cancel / lapse**: the chance that a policy is cancelled during its term or not renewed at the end.

> **Status:** planning. Nothing below is implemented yet.

---

## 1. Why this project

- **Real data only.** All data comes from official FEMA records; nothing is synthetic or deliberately degraded.
- **Naturally messy.** The raw files have heavy missingness, mixed codes, placeholder strings, inconsistent names, rows that bundle several policies, and **no policy ID**. Cleaning and linking are a core part of the work.
- **Actuarially meaningful.** Claim probability (frequency) and lapse (persistency) are two central quantities in non-life pricing and portfolio management.

## 2. Data sources

| Dataset | Publisher | Grain | Use |
|---|---|---|---|
| [NFIP Redacted Policies v3](https://www.fema.gov/openfema-data-page/nfip-redacted-policies-v3) | OpenFEMA | One policy term (~1 year) | Core table: features and lapse target |
| [NFIP Redacted Claims v3](https://www.fema.gov/openfema-data-page/nfip-redacted-claims-v3) | OpenFEMA | One claim | Claim target, prior-claim features |
| Disaster Declarations Summaries | OpenFEMA | County × event | Storm exposure context |
| NFIP Community Status Book / CRS class | OpenFEMA | NFIP community | Community-level features |
| American Community Survey (ACS) 5-year | U.S. Census Bureau | Census tract | Neighborhood income, home values, age mix (stand-in for missing demographics) |
| FRED | Federal Reserve Bank of St. Louis | Month | Interest rates, housing, unemployment |

**Scope (proposed):** Florida, policy terms effective 2019-01-01 to 2023-12-31. This window includes Hurricane Ian (2022), which supplies enough claims to model.
National scale for reference: about 74.7M policy terms in total; 197,148 claims with loss years 2019–2023.

> Counts for Florida alone are still to be confirmed (step 1 of the plan).

## 3. Levels of analysis

```
National (FRED, monthly)
└── County (disaster declarations)
    └── NFIP community (status, CRS class)
        └── Census tract (ACS, claim history)
            └── Policy  ← reconstructed by linking terms
                └── Policy term  ← one row of the NFIP policies table (unit of prediction)
```

There is **no policyholder (person) level**: NFIP redacts all personal data.

## 4. Targets

| Model | Target | Definition | Expected base rate |
|---|---|---|---|
| Claim | `CLAIM_IN_TERM` | 1 if a linked claim's `dateOfLoss` falls between the term's `policyEffectiveDate` and `policyTerminationDate` | ~1% a year nationally; higher in storm years and areas |
| Lapse (A) | `CANCEL_IN_TERM` | 1 if `cancellationDateOfFloodPolicy` is set and the reason code is real attrition (not a duplicate or rewrite) | ~9% in a pilot county |
| Lapse (B) | `NON_RENEWAL` | 1 if no linked term for the same policy starts within ±60 days of `policyTerminationDate` | To be validated (linkage-sensitive) |

Terms ending after the data cutoff are **censored** and excluded from the lapse labels, or handled with survival methods.

## 5. The key problem: no policy ID

Policies and claims share a set of fields that together approximate a property key:

`originalNBDate` + `originalConstructionDate` + `censusGeoid` + `reportedZipCode` + `occupancyType` + `numberOfFloorsInInsuredBuilding`

- **Term → policy:** group terms on the key and order them by effective date.
- **Claim → term:** match on the key, with `dateOfLoss` inside the term window.
- **Quality checks:** collision rate (keys with more than one term starting in the same year), match rate, and a sensitivity analysis on alternative keys. In a pilot county (Beaufort, SC), about 3% of keys collided.

## 6. Features (initial list)

| Group | Examples |
|---|---|
| Price | Premium, total policy cost, year-over-year premium change, Risk Rating 2.0 transition |
| Obligation | `mandatoryPurchaseFlag` (mortgage-required coverage) |
| Flood risk | Rated and current flood zone, elevation difference, base flood elevation, foundation type, elevated building |
| Coverage | Building and contents coverage, deductible codes, replacement cost |
| Property | Construction year, floors, occupancy, primary residence, rental, condo type |
| Tenure | Months since `originalNBDate`, number of prior terms |
| History | Prior claims on the same property (count, amount, recency) |
| Context | Disaster declarations in the previous 12 months, tract income (ACS), mortgage rates (FRED), CRS class |

**Leakage guard:** only information available at the **start** of the term goes into the features. Cancellation fields and claim amounts from the current term are never used as features.

## 7. Methodology

1. **Acquire.** Script downloads from the OpenFEMA API or bulk files, keeping the raw data unchanged.
2. **Understand.** Profile every field (missingness, cardinality, codes) against FEMA's data dictionaries.
3. **Clean.** Normalize `"None"` strings to nulls, decode codes, parse types, deduplicate, handle rows with `policyCount > 1`, and log every rule in `reports/cleaning_log.csv`.
4. **Link.** Reconstruct policies, attach claims, and report the quality of the linkage.
5. **Label.** Build the claim and lapse targets and handle censoring.
6. **Enrich.** Join disasters, ACS, community and FRED data.
7. **Explore.** Base rates by zone, tenure, price change and mandatory status.
8. **Model** each target:
   - Baseline: logistic regression (interpretable)
   - Main: gradient boosting (LightGBM or XGBoost)
   - Lapse extension: discrete-time hazard or survival model
   - Claim extension: frequency × severity to get **expected loss**
9. **Calibrate.** Isotonic or Platt scaling; reliability curves.
10. **Evaluate.** On a **time-based split** (train 2019–2021, validate 2022, test 2023), using ROC-AUC, PR-AUC (important for rare claims), Brier score, log loss, calibration error and lift by decile.
11. **Explain.** SHAP values, partial dependence, and segment-level results.
12. **Report.** Findings, limitations, and the business use (retention targeting, pricing adequacy).

## 8. Known risks and limitations

- **Linkage error** in the reconstructed policy key affects the non-renewal label and the claim matching.
- **Weather drives claims.** A model trained on a storm year may not carry over to calmer years, so the time-based split and disaster features matter.
- **Class imbalance** for claims calls for class weights, PR-AUC and calibration.
- **No personal or behavioral data:** no demographics, payment history or contact data.
- **Data volume:** Florida 2019–2023 is likely several million rows, so use DuckDB or Polars with Parquet files rather than plain pandas on CSV.
- **Redaction:** coordinates are rounded and some fields are generalized.

## 9. Planned repository structure

```
├── data/
│   ├── raw/          # untouched downloads (gitignored)
│   ├── interim/      # cleaned, linked tables (gitignored)
│   └── processed/    # modeling tables (gitignored)
├── docs/             # data dictionary, field mapping, decisions
├── notebooks/
│   ├── 01_data_understanding.ipynb
│   ├── 02_cleaning_and_linkage.ipynb
│   ├── 03_eda.ipynb
│   ├── 04_feature_engineering.ipynb
│   ├── 05_model_claim.ipynb
│   └── 06_model_lapse.ipynb
├── src/
│   ├── download.py
│   ├── clean.py
│   ├── link.py
│   ├── features.py
│   └── evaluate.py
├── reports/          # figures, cleaning log, final report
├── requirements.txt
└── README.md
```

## 10. Milestones

| # | Milestone | Output |
|---|---|---|
| 1 | Confirm scope and counts (Florida, 2019–2023) | Row counts, claim rate, approval of scope |
| 2 | Download raw data | `src/download.py`, Parquet files in `data/raw/` |
| 3 | Data understanding | Profiling notebook, data dictionary |
| 4 | Cleaning and linkage | Cleaning log, linkage quality report |
| 5 | Targets and enrichment | Labeled modeling tables |
| 6 | EDA | Base-rate and driver analysis |
| 7 | Claim model | Calibrated model, evaluation |
| 8 | Lapse model | Calibrated model, evaluation |
| 9 | Report | Findings, limitations, recommendations |

## 11. Reproducibility

- Raw data is never edited by hand. Every transformation is code plus a logged rule.
- Fixed random seeds; pinned package versions in `requirements.txt`.
- Anyone can re-download the data from OpenFEMA with `src/download.py`.

## License and data credit

Data: FEMA OpenFEMA, U.S. Census Bureau and FRED, all public U.S. government or public-domain sources. This product uses the FEMA OpenFEMA API but is not endorsed by FEMA or the federal government.
