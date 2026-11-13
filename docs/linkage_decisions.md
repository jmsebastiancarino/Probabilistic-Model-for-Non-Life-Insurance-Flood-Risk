# Linkage and target decisions (notebook 03)

## Summary
- Data cutoff: 2026-09-08 (asOfDate of the FEMA files).
- 74,739,461 clean policy terms -> 73,108,634 linked terms -> 73,248,304 policy-years.
- Policy key: originalNBDate, originalConstructionDate, censusGeoid, reportedZipCode, numberOfFloorsInInsuredBuilding. Key complete for 99.5% of terms; 5.42% of terms are in a collision.
- Claims with a loss from 2010 on: 98.3% linked to a policy-year.
- Claim reporting delay: 365 days. Renewal window: +/-60 days.

## Key decisions
<!-- Write 1-2 sentences for each: what you decided and why -->
- **Unit of prediction:** the policy-year. Multi-year terms are split by their dates (option B); short last periods are kept with exposure below 1.
- **Lapse codes:** 05, 28, 30 and 52. ...
- **Void and duplicate terms:** left out. ...
- **Flat cancellations:** terms cancelled on or before their start date are left out; an unpaid renewal counts as NON_RENEWAL of the term before, and CANCEL_IN_TERM measures lapses during a term. ...
- **Policy key:** ... (Section 6.2)
- **Claim reporting delay:** ... (Section 7.1)
- **Collisions:** ...

## Left for later notebooks
- Notebook 04: exclude or keep `dq_key_collision`, `community_sanctioned` and multi-unit rows; which lapse target to model (CANCEL_IN_TERM, NON_RENEWAL or both).
- Notebook 05: features are joined from `policies_clean` by `id`. Cancellation and claim columns in the labeled table are target-only.
