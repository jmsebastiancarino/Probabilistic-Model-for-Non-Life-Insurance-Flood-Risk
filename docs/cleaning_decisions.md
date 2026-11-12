# Data cleaning decisions (notebook 02)

## Summary
- Policies: 74,739,461 rows in, 74,739,461 out (no rows removed; problems flagged with `dq_` columns).
- Claims: 2,725,989 in, 2,714,607 out (exact duplicates removed).
- Disasters: 70,599 in, 70,586 out (exact duplicates removed).
- Community: 25,133 in, 25,133 out.
- 74,806,197 placeholder values ("None", "NA", ...) converted to NULL.
- 37,680,423 values outside the dictionary's legal codes set to NULL (each flagged with `dq_invalid_<column>`).
- Columns dropped: asOfDate, numberOfFloorsInTheInsuredBuilding, reportedCity, lastRefresh, asOfDate, reportedCity.

## Principles
- Raw files are never edited; every rule is code plus a cleaning-log entry.
- Flag, don't delete: rows are only removed when they are exact duplicates.
- No imputation here; missing values are handled in notebook 05, fitted on training data only.

## Key decisions
- **Policy duplicates kept: to be removed later as we get the policyID** ...
- **foundationType code 0: transform it into NULL** ...
- **rateMethod RE: kept** ...
- **Cancellation codes: untouched but we have classified whether it is lapsed, duplicate, etc.** ...
- **Community dates and legend codes: kept** ...
- **CRS class "None": kept** ...

## Where NULL has a meaning
| Column | NULL means |
|---|---|
| cancellationVoidanceReasonCode | not cancelled |
| classRating / crs_class | community not in CRS (no discount) |
| sanctionDate | never sanctioned |
| currentlyEffectiveMapDate | see legend flags (NSFHA, N/A) |
| foundationType | not reported (code 0 in the raw data; see dq_invalid_foundationType) |

## Left for later notebooks
- Notebook 03: policy key and linkage; claims with incomplete keys can't be linked.
- Notebook 04: whether to exclude multi-unit and identical policy rows; `dq_` flag exclusions.
- Notebook 05: ACS tract-boundary mismatch (2010 vs 2020); FRED publication lag; statewide disasters.
