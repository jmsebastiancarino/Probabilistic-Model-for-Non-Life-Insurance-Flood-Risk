# EDA findings and feature decisions (notebook 05)

## Summary
- Training split: years 2014-2020, states AK, AL, AR, AS, AZ, CA, CO, CT, DC, DE, FL, GA, GU, HI, IA, ID, IL, IN, KS, KY, LA, MA, MD, ME, MI, MN, MO, MP, MS, MT, NC, ND, NE, NH, NJ, NM, NV, NY, OH, OK, OR, PA, PR, RI, SC, SD, TN, TX, UN, UT, VA, VI, VT, WA, WI, WV, WY.
- 86 features built in 8 groups; 18 dropped.
- Strongest features on their own: claim: heavy_rain_days_3y, premium_per_1k_coverage, acs_median_home_value, precip_max1d_mean_mm, years_since_last_claim; lapse: foundation_type, current_zone_group, zone_changed, basement_type, term_no.
- Claims with no tropical-cyclone wind at the property: 52.39% of training claims; the largest storm, HARVEY (2017), explains 22.5%.

## Answers to our questions
- **Q1 When and where do claims happen?** A few storms drive a large share (Harvey alone 22.5%, four storms 38.9%), but 52.4% of claims had no tropical-cyclone wind. The claim rate ranges from 0.50% (2014) to 2.68% (2017); the start month barely matters (lift 0.91-1.08).
- **Q2 Does flood risk explain claims?** Yes: zone V 2.11% and A 1.57% against X 1.00%; below the base flood elevation 2.12%; pre-FIRM 1.6 times post-FIRM. Zone X still holds 37% of claims. Basements and slabs are not riskier.
- **Q3 Does price drive lapse?** Partly: increases above 7.7% raise lapse by about 14%, in every year. The cheapest policies lapse most (16.7%). Risk Rating 2.0 can't be tested in the training years.
- **Q4 Does a mortgage requirement keep policyholders insured?** Yes: 11.4% against 14.1% overall, and 27-36% lower within each tenure group, less after 9 years.
- **Q5 Are new policyholders more likely to lapse?** Yes: 19.7% in the first year against 9.2% after 10 years; new business lapses about 1.5 times as often in every year.
- **Q6 Do past claims predict future claims and change lapse?** Strongly for claims (8.1% with 3+ earlier claims, 7 times the no-claim rate); earlier claims lower lapse by 31%, less right after a claim.
- **Q7 How are claims and cancellations related?** Cancellation is 3 times lower in the year of a claim, but a claim in the previous 12 months barely changes lapse (13.1% vs 13.8%).
- **Q8 Does context matter?** Poorer tracts claim 1.8 times and lapse 27% more; CRS classes 1-4 have about half the claims; recent disasters show no clear pattern; FRED rates follow the year.

## Feature decisions
- Dropped: full_risk_premium (leakage: filled mainly for policies that stayed (lapse 27.6% missing vs 0.6% filled)), discount_to_full_risk (leakage: same as full_risk_premium), foundation_type (leakage: same rows missing as full_risk_premium (lapse 27.6% vs 0.6%)), basement_type (leakage: lapse 19.9% when missing vs 7.6% when filled), current_zone_group (leakage: lapse 55.7% when missing vs 9.0% when filled), zone_changed (leakage: built from the same field as current_zone_group), rr2_rated (false for every training policy-year (Risk Rating 2.0 started in October 2021)), in_sfha (duplicate: zone_group is A or V), year_in_term (no signal (strength 0.50 for both targets)), multi_year_term (no signal (strength 0.50 for both targets)), years_since_map (status book has only the current map date: blank for 51% of policy-years), community_emergency_program (current snapshot, no date; no signal (strength 0.50)), mortgage_rate (same for every policy in a month (a proxy for the year); test/validation outside the training range), treasury_10y (proxy for the year (FRED)), unemployment_rate (proxy for the year (FRED)), cpi_yoy (proxy for the year (FRED)), home_price_yoy (proxy for the year (FRED)), fed_funds (proxy for the year (FRED)).
- Leakage audit: all date checks passed (0 claims, declarations, storms or rain on or after the start). Six rating fields are held back because whether they are filled depends on lapse; impossible tenure is imputed without a missing flag; the status book's current map date and the FRED series are dropped.
- Missing values and encoding: see docs/feature_plan.csv.
