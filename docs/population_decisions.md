# Modeling population and split decisions (notebook 04)

## Summary
- Scope: all 57 states, policy-years starting 2014-01-01 to 2024-08-31 (data cutoff 2026-09-08).
- Split: train 2014-2020; test 2021-2022; valid 2023-2024.
- Claim population: 40,497,327 policy-years, 1.311% with a claim.
- Lapse population: 39,485,736 policy-years, 13.008% lapsed (cancelled for a lapse reason or not renewed).
- Sampling: none (NEG_RATE {'claim': 1.0, 'lapse': 1.0}); every policy-year in the populations is kept.
- Naming: we tune on 'test' and keep 'valid' as the final holdout, used once.
- Rolling-origin folds: 6 (train from 2014 up to year t, check on t+1; holdout years never used).

## Key decisions
<!-- Write 1-2 sentences for each: what we decided and why -->
- **Scope (states and years):** ...
- **Multi-unit rows:** ...
- **dq_ flags excluded:** dq_term_end_before_start, dq_effective_in_future, dq_cancel_after_termination. ...
- **Key collisions:** ...
- **Rewrites and sales in the lapse population:** ...
- **Lapse target:** `LAPSE` combines `CANCEL_IN_TERM` and `NON_RENEWAL` (one lapse model, as in our README). ...
- **Sampling:** ...

## Left for later notebooks
- Notebook 05: features from `policies_clean` joined by `id`; every fitted step (missing values, encoding) uses the training split only.
- Notebooks 06-07: train on `sample_<population>` rows with `weight_<population>`; tune on the 'test' rows.
- Notebook 08: final results on the 'valid' rows, used once.
