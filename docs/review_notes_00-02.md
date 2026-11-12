# Review notes: notebooks 00–02

Suggestions only. None of the markdown text below has been changed in the notebooks: your writeups are kept exactly as you wrote them. Apply what you agree with. Delete this file when you're done.

## A. Decisions to make before rerunning

| # | Topic | Why it matters | Suggestion |
|---|---|---|---|
| 1 | **ACS covers Florida only** (`STATES = ["12"]` in notebook 01). **Decided: all states**; notebook 01 now downloads every state and replaces the Florida-only file. | Policies are national; Florida is 22.4M of 74.7M rows. ACS features would be missing for about 70% of policy terms, and `policy_tracts_not_in_acs` in 2.8d will be large. | Either download every state (`STATES = STATE_FIPS`; about 85,000 tracts, a few minutes) or limit the whole project to Florida and say so in the README. |
| 2 | **`policyTermIndicator = 2`** (18,999 rows). **Check added (02, Steps 2.4.6e–f); treatment pending**, recorded as PENDING in 3.0b. | It is currently set to NULL as invalid. If these are real 2-year policies, nulling them changes how policy-years are built in notebook 03. | Check the actual term length of those rows (query below). If they last about 2 years, add `"2"` to `manual` in Step 2.4.3 with that evidence. |
| 3 | **`foundationType = 0`**. **Investigation added (02, Steps 2.4.6a–d).** | Writeup 2.4 says it "can mean no information available". That is a hypothesis until it is checked. | Check the share of `0` by year and by `rateMethod`. If it drops after Risk Rating 2.0 (Oct 2021), write "not collected before RR2.0" and the evidence. |
| 4 | **Columns proposed for dropping** | The new rule only drops a column with one value in **every** row. One value plus NULLs is now "review", because it can be a yes/no signal. | After rerunning 2.5, read the "drop" and "review" lists and confirm each in 3.0b. |

Query for #2:

```python
con.sql("""
    SELECT policyTermIndicator,
           ROUND(date_diff('day', policyEffectiveDate, policyTerminationDate) / 365.25, 1) AS term_years,
           COUNT(*) AS n
    FROM raw_policies
    WHERE policyTermIndicator IN (0, 2, 4, 5, 6)
    GROUP BY 1, 2 ORDER BY 1, 3 DESC
""").df()
```

## B. Writeup suggestions, notebook by notebook

### 00 Data Dictionary
- I added a short **Purpose / Outputs** cell and section headings, because the notebook had only a title. Check the wording.

### 01 Data Gathering
- **Name mismatch:** the title says "01 Data Gathering", the file is `01_data_understanding`, and the README says "Download all raw data". Pick one name everywhere (suggest *01 Data Gathering* and rename the file to `01_data_gathering.ipynb`, then update the README table).
- **Purpose:** "Gather the data and information in relation to the flood insurance program of US" → "Download the raw data for the US National Flood Insurance Program (NFIP) and the supporting Census and FRED data."
- **Purpose:** "Download is done via application program interface (API)" is only partly true: the NFIP files are bulk Parquet downloads. Suggest: "Downloads use OpenFEMA bulk files, the Census API and FRED CSV files."
- **Date Modified** still says 2026-11-07.
- **Florida example (last cell)** loads 5.7M rows × 87 columns into pandas (several GB of RAM) just to show the shape. Consider adding `LIMIT 1000`, or say why the full slice is needed.

### 02 Data Cleaning
- **Date Modified:** update after this rerun.
- **2.1:** "data types of each columns" → "each column".
- **2.3 type table:** it now also needs the stored DuckDB types (`TIMESTAMP`, `TINYINT`, `DOUBLE`, ...) that `type_map` maps, and the reason both sides are mapped: *"Both the dictionary type and the stored type are mapped to one family before comparing, so SMALLINT vs smallint is not reported as a mismatch."*
- **After 2.3.2 / 2.3.3:** "Since the dtypes are correct based on the matching performed for policies…" is now accurate. Shorter: *"After mapping both sides to the same type family, every policies column matches the dictionary. No conversion is needed."*
- **Under 2.3.9 (disasters):** the text says "for **claims**"; it should say "for disasters".
- **2.3 legend table:** "split it into two – currentlyEffectiveMapDate and currentlyEffectiveMapDateindicator" doesn't match what the code does: one parsed date plus **one true/false flag per legend code** (`map_is_nsfha`, `map_map_date_future`, ...). Also add **(All Zone D)** to the table, and add a column saying which date column each code appears in (E only in the program date; S and W only in the sanction date).
- **"About 6 variables have been created":** use the exact count from the 2.3.6 output, per column (for example "6 flags for the map date, 2 for the program date, 2 for the sanction date").
- **Sanction table:** "communitys" → "community's" (twice).
- **2.4:** "There are about 77 variables": the count is exact (43 codes + 29 true/false + 4 zones + 1 list), so drop "about" and say "77 columns".
- **2.4 treatment table:**
  - "Is it an obvious **type** or formatting issue?" → "typo".
  - "Ignoreable" → "Negligible".
  - Values between 0.1% and 1% fall between rows 1 and 3. Make the thresholds continuous (for example "< 1% and no documentation → NULL + flag").
  - Add a row: "Documented in another FEMA source → add to the allowed list (Step 2.4.3) and cite the source."
  - "about 80% and 47%" → "80.6% (claims) and 47.5% (policies)".
- **2.5 heading:** "Count the nulls of each columns" → "each column".
- **After 2.5.2:** "About 5 columns (reportedCity, asOfDate, lastRefresh) would be dropped…" lists 3 names for 5 columns, and the rule changed. Rewrite from the new output, and add *why* each is dropped (for example "asOfDate is the date FEMA produced the file, the same for every row").
- **2.6 heading:** "Check the validity of the dates" also covers premiums, coverage and coordinates → "Plausibility checks (dates, amounts, coverage, coordinates)".
- **After 2.6a:** "About <1% have violations which could largely be ignored due to materiality" → name the largest rules with their counts, and say what happens: *"They are kept and flagged with dq_ columns, so notebook 04 can decide whether to exclude them."* "Ignored" suggests nothing is done.
- **2.7:** "No duplicates have been noted in the primary keys" is true, but the next cell finds rows identical except the key (0.96% of policies, 0.42% of claims). Add one sentence so the reader isn't surprised.
- **Disaster duplicates:** the old de-duplication removed 0 of the 13 rows because it compared `disasterNumber` and `placeCode` (the primary key), while the audit ignored them. So these 13 rows differ in at least one primary-key column, not only in `hash`. Read the 2.7.3 example and describe what actually differs. The log reason now says "check the examples in 2.7.3"; replace it with your finding.
- **3.3 bullet list:** add "All Zone D" to the legend flags.
- **5.3 note:** fill in the six "Key decisions" lines (currently `...`) before submitting.

## C. Behaviour changes from this cleanup (what to expect when you rerun)

| Area | Before | Now |
|---|---|---|
| Type check (2.3) | Many false mismatches (SMALLINT vs INTEGER) | Only real ones (community text dates and `classRating`) |
| Disaster duplicates (2.7) | Audit counted 13, de-duplication removed 0 (different column sets) | Both use `DUP_EXCLUDE`, so 13 are removed and 4.3 reconciles |
| Integrity checks (2.8d) | `NOT IN`, which returns 0 if the other table has any NULL | `NOT EXISTS`; counts will likely be higher and correct |
| Invalid codes (2.4) | `_fixed` views | Applied once in Section 3 by `make_select`, with `dq_invalid_<column>` flags |
| Cleaning log | Written in both Sections 2 and 3 (double entries, text in `rows_affected`) | Written where a rule is applied (Section 3; duplicates in 2.7), always with a number |
| Constant columns | One value + NULLs was dropped | Only dropped when the value is in every row |
| ACS (3.5a) | `KeyError` (columns were already renamed in notebook 01) | Fixed |
| 3.3a | Syntax error (missing `)`) | Fixed |
| 4.1b | Re-ran the rules on the clean view (fails if `asOfDate` is dropped) | Compares each `dq_` flag with the raw violation count |
