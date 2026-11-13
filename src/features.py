"""Feature definitions and the SQL that builds them (notebook 05).

Every feature is listed once in :data:`FEATURES` with its group, kind,
source, plain-language definition and **when it is known**. The feature
dictionary, the leakage audit and the missing-value plan are all
generated from that list, so they can't drift apart from the code.

Rule for every feature: it must be known at the **start** of the
policy-year (``period_start``). Anything recorded later (claims in the
period, the cancellation, a community status published afterwards) is
not a feature.

Aliases used in the SQL
-----------------------
``c``
    The clean policy term (``policies_clean``), joined by ``id``.
``t``
    The policy-year from notebook 03 (``periods/<state>.parquet``), joined
    by ``period_id``. Has ``period_start``, ``term_start``, ``term_no``, ...
"""

#: Feature metadata: (name, group, kind, source, definition, known_at).
#: ``kind`` is ``"num"`` (number), ``"cat"`` (category / code) or
#: ``"flag"`` (true/false). ``known_at`` says why the value is available
#: at the start of the policy-year.
FEATURES = [
    # -- price
    ("premium", "price", "num", "policies", "Total premium of the term ($)", "set when the term is rated"),
    ("policy_cost", "price", "num", "policies", "Premium plus fees and surcharges ($)", "set when the term is rated"),
    ("premium_per_1k_coverage", "price", "num", "policies",
     "Premium per $1,000 of building + contents coverage: the price of cover", "set when the term is rated"),
    ("rr2_rated", "price", "flag", "policies", "Rated under Risk Rating 2.0 (rateMethod = RE)", "set when the term is rated"),
    ("full_risk_premium", "price", "num", "policies",
     "Full-risk premium under Risk Rating 2.0 ($); missing before Risk Rating 2.0", "set when the term is rated"),
    ("discount_to_full_risk", "price", "num", "policies",
     "Full-risk premium minus charged premium ($): the increase still to come under the yearly cap",
     "set when the term is rated"),
    ("subsidized_rate_type", "price", "cat", "policies", "Subsidized rate type code", "set when the term is rated"),
    ("hfiaa_surcharge", "price", "num", "policies", "HFIAA surcharge ($)", "set when the term is rated"),
    ("premium_change_pct", "price", "num", "policies (previous term)",
     "Change in premium from the previous term of the same policy (%)", "both terms are rated by the start"),
    ("coverage_change_pct", "price", "num", "policies (previous term)",
     "Change in building coverage from the previous term (%)", "both terms are rated by the start"),
    # -- obligation
    ("mandatory_purchase", "obligation", "flag", "policies",
     "Insurance required by the mortgage lender", "set when the term is written"),
    # -- flood risk
    ("zone_group", "flood_risk", "cat", "policies", "Rated flood zone group: A, V, X, D or UNKNOWN", "set when the term is rated"),
    ("current_zone_group", "flood_risk", "cat", "policies", "Current flood zone group (latest map)", "set when the term is rated"),
    ("zone_changed", "flood_risk", "flag", "policies",
     "Current zone group differs from the rated one (map revision)", "set when the term is rated"),
    ("in_sfha", "flood_risk", "flag", "policies",
     "Rated in a Special Flood Hazard Area (zone A or V)", "set when the term is rated"),
    ("elevation_difference", "flood_risk", "num", "policies",
     "Lowest floor minus base flood elevation (feet)", "from the elevation certificate, before the term"),
    ("base_flood_elevation", "flood_risk", "num", "policies", "Base flood elevation (feet)", "from the flood map"),
    ("elevated_building", "flood_risk", "flag", "policies", "Building is elevated", "set when the term is rated"),
    ("foundation_type", "flood_risk", "cat", "policies",
     "Foundation type code; NULL = not collected (before 2010)", "set when the term is rated"),
    ("basement_type", "flood_risk", "cat", "policies", "Basement, enclosure or crawlspace type code", "set when the term is rated"),
    ("post_firm", "flood_risk", "flag", "policies",
     "Built after the community's first flood map (post-FIRM)", "set when the term is rated"),
    ("elevation_certificate", "flood_risk", "cat", "policies", "Elevation certificate indicator code", "set when the term is rated"),
    ("obstruction_type", "flood_risk", "cat", "policies", "Obstruction type below the elevated floor", "set when the term is rated"),
    ("floodproofed", "flood_risk", "flag", "policies", "Building is floodproofed", "set when the term is rated"),
    ("crs_class", "flood_risk", "cat", "policies",
     "Community Rating System class on the term (1 = best; NULL = not in CRS)", "set when the term is rated"),
    ("latitude", "flood_risk", "num", "policies", "Latitude, rounded to 0.1 degree by FEMA", "fixed property location"),
    ("longitude", "flood_risk", "num", "policies", "Longitude, rounded to 0.1 degree by FEMA", "fixed property location"),
    # -- coverage
    ("building_coverage", "coverage", "num", "policies", "Building coverage ($)", "set when the term is written"),
    ("contents_coverage", "coverage", "num", "policies", "Contents coverage ($)", "set when the term is written"),
    ("contents_only", "coverage", "flag", "policies", "No building coverage, contents only", "set when the term is written"),
    ("building_deductible", "coverage", "cat", "policies", "Building deductible code", "set when the term is written"),
    ("contents_deductible", "coverage", "cat", "policies", "Contents deductible code", "set when the term is written"),
    ("replacement_cost", "coverage", "num", "policies", "Building replacement cost ($)", "set when the term is rated"),
    ("coverage_to_value", "coverage", "num", "policies",
     "Building coverage divided by replacement cost (insurance to value)", "set when the term is rated"),
    # -- property
    ("building_age", "property", "num", "policies", "Policy-year start year minus construction year", "fixed property fact"),
    ("floors", "property", "cat", "policies", "Number of floors code", "fixed property fact"),
    ("occupancy_group", "property", "cat", "policies",
     "Occupancy grouped so old and Risk Rating 2.0 codes mean the same: single_family, 2_4_family, "
     "multi_family_or_condo, non_residential", "set when the term is written"),
    ("primary_residence", "property", "flag", "policies", "Primary residence", "set when the term is written"),
    ("rental_property", "property", "flag", "policies", "Rental property", "set when the term is written"),
    ("tenant", "property", "flag", "policies", "Policyholder is a tenant", "set when the term is written"),
    ("condo_type", "property", "cat", "policies", "Condominium coverage type code", "set when the term is written"),
    ("building_description", "property", "cat", "policies", "Building description code", "set when the term is written"),
    ("small_business", "property", "flag", "policies", "Small business building", "set when the term is written"),
    # -- tenure
    ("tenure_years", "tenure", "num", "policies",
     "Years from the original new-business date to the policy-year start", "dates before the start"),
    ("new_business", "tenure", "flag", "policies", "First year of the policy (tenure below 1 year)", "dates before the start"),
    ("years_since_purchase", "tenure", "num", "policies",
     "Years from the property purchase date to the start (NULL if the date is later or missing)", "dates before the start"),
    ("term_no", "tenure", "num", "linkage (notebook 03)",
     "Term number within the rebuilt policy, counting terms since the data begins (2009)", "earlier terms only"),
    ("year_in_term", "tenure", "num", "linkage (notebook 03)",
     "Year within a multi-year term (0 = first year)", "term dates"),
    ("multi_year_term", "tenure", "flag", "linkage (notebook 03)", "Term lasts more than one policy-year", "term dates"),
    ("start_month", "tenure", "cat", "linkage (notebook 03)",
     "Calendar month the policy-year starts (hurricane season starts in June)", "term dates"),
    # -- history
    ("prior_claims_n", "history", "num", "claims",
     "Claims on the same property with a loss before the start", "losses before the start"),
    ("prior_claims_5y_n", "history", "num", "claims",
     "Claims on the same property in the 5 years before the start", "losses before the start"),
    ("prior_claims_paid", "history", "num", "claims",
     "Total paid on those earlier claims ($)", "losses before the start (amounts may be settled later)"),
    ("had_prior_claim", "history", "flag", "claims", "At least one earlier claim", "losses before the start"),
    ("years_since_last_claim", "history", "num", "claims",
     "Years since the most recent earlier loss; NULL = no earlier claim", "losses before the start"),
    ("prev_gap_days", "history", "num", "linkage (notebook 03)",
     "Days between the end of the previous term and the start of this term", "earlier terms only"),
    ("returned_after_lapse", "history", "flag", "linkage (notebook 03)",
     "Came back after a gap longer than the 60-day renewal window", "earlier terms only"),
    # -- context
    ("flood_disasters_12m", "context", "num", "disasters",
     "Flood-related disaster declarations in the county (or statewide) in the 12 months before the start",
     "declarations before the start"),
    ("days_since_flood_disaster", "context", "num", "disasters",
     "Days since the most recent flood-related declaration in the county or state", "declarations before the start"),
    ("acs_median_income", "context", "num", "census ACS 2018-2022", "Tract median household income ($)",
     "5-year average, slow-moving"),
    ("acs_median_home_value", "context", "num", "census ACS 2018-2022", "Tract median home value ($)", "5-year average, slow-moving"),
    ("acs_median_age", "context", "num", "census ACS 2018-2022", "Tract median age", "5-year average, slow-moving"),
    ("acs_owner_share", "context", "num", "census ACS 2018-2022", "Tract share of owner-occupied homes", "5-year average, slow-moving"),
    ("acs_population", "context", "num", "census ACS 2018-2022", "Tract population", "5-year average, slow-moving"),
    ("mortgage_rate", "context", "num", "FRED", "30-year mortgage rate, month before the start (%)", "published weekly"),
    ("treasury_10y", "context", "num", "FRED", "10-year Treasury yield, month before the start (%)", "published daily"),
    ("unemployment_rate", "context", "num", "FRED", "US unemployment rate, month before the start (%)", "published the next month"),
    ("cpi_yoy", "context", "num", "FRED", "CPI change over 12 months, month before the start (%)", "published the next month"),
    ("home_price_yoy", "context", "num", "FRED",
     "Case-Shiller home price change over 12 months, 3 months before the start (%)", "published 2 months later"),
    ("fed_funds", "context", "num", "FRED", "Federal funds rate, month before the start (%)", "published monthly"),
    ("community_emergency_program", "context", "flag", "community status book",
     "Community is in the NFIP Emergency Program (limited coverage)", "current snapshot (see leakage audit)"),
    ("community_sanctioned_before", "context", "flag", "community status book",
     "Community was suspended or withdrawn before the start", "sanction date before the start"),
    ("years_since_map", "context", "num", "community status book",
     "Years since the current flood map took effect; NULL if it took effect after the start",
     "map date before the start"),
]

#: Columns that hold the outcome. They must never be used as features.
TARGET_ONLY = {
    "CLAIM_IN_TERM", "CLAIM_PAID_IN_TERM", "CANCEL_IN_TERM", "SOLD_REMOVED_IN_TERM", "NON_RENEWAL", "LAPSE",
    "claim_count", "paid_claim_count", "claim_paid_total", "first_loss_date",
    "cancel_date", "cancel_code", "cancel_category", "cancel_in_period", "cancel_seen", "eff_end",
    "renewed", "next_start", "gap_to_next_days", "censored_claim", "censored_cancel", "censored_renewal",
}

#: SQL for the features taken from the policy term and the policy-year
#: (aliases ``c`` and ``t``).
_ZONE = """CASE WHEN {z} LIKE 'V%' THEN 'V' WHEN {z} LIKE 'A%' THEN 'A'
               WHEN {z} IN ('X', 'B', 'C') THEN 'X' WHEN {z} = 'D' THEN 'D' END"""
_TENURE = "date_diff('day', CAST(c.originalNBDate AS DATE), t.period_start) / 365.25"
_COVER = "(COALESCE(c.totalBuildingInsuranceCoverage, 0) + COALESCE(c.totalContentsInsuranceCoverage, 0))"

BASE_SQL = {
    "premium": "c.totalInsurancePremiumOfThePolicy",
    "policy_cost": "c.policyCost",
    "premium_per_1k_coverage": f"1000.0 * c.totalInsurancePremiumOfThePolicy / NULLIF({_COVER}, 0)",
    "rr2_rated": "TRIM(CAST(c.rateMethod AS VARCHAR)) = 'RE'",
    "full_risk_premium": "c.fullRiskPremium",
    "discount_to_full_risk": "c.fullRiskPremium - c.totalInsurancePremiumOfThePolicy",
    "subsidized_rate_type": "CAST(c.subsidizedRateType AS VARCHAR)",
    "hfiaa_surcharge": "c.hfiaaSurcharge",
    "mandatory_purchase": "c.mandatoryPurchaseFlag",
    "zone_group": "c.zone_group",
    "current_zone_group": _ZONE.format(z="UPPER(TRIM(c.floodZoneCurrent))"),
    "zone_changed": f"c.zone_group <> ({_ZONE.format(z='UPPER(TRIM(c.floodZoneCurrent))')})",
    "in_sfha": "c.zone_group IN ('A', 'V')",
    "elevation_difference": "c.elevationDifference",
    "base_flood_elevation": "c.baseFloodElevation",
    "elevated_building": "c.elevatedBuildingIndicator",
    "foundation_type": "CAST(c.foundationType AS VARCHAR)",
    "basement_type": "CAST(c.basementEnclosureCrawlspaceType AS VARCHAR)",
    "post_firm": "c.postFIRMConstructionIndicator",
    "elevation_certificate": "CAST(c.elevationCertificateIndicator AS VARCHAR)",
    "obstruction_type": "CAST(c.obstructionType AS VARCHAR)",
    "floodproofed": "c.floodproofedIndicator",
    "crs_class": "CAST(c.crsClassCode AS VARCHAR)",
    "latitude": "c.latitude",
    "longitude": "c.longitude",
    "building_coverage": "c.totalBuildingInsuranceCoverage",
    "contents_coverage": "c.totalContentsInsuranceCoverage",
    "contents_only": "COALESCE(c.totalBuildingInsuranceCoverage, 0) = 0 AND c.totalContentsInsuranceCoverage > 0",
    "building_deductible": "CAST(c.buildingDeductibleCode AS VARCHAR)",
    "contents_deductible": "CAST(c.contentsDeductibleCode AS VARCHAR)",
    "replacement_cost": "c.buildingReplacementCost",
    "coverage_to_value": "c.totalBuildingInsuranceCoverage / NULLIF(c.buildingReplacementCost, 0)",
    "building_age": "t.period_year - YEAR(c.originalConstructionDate)",
    "floors": "CAST(TRY_CAST(c.numberOfFloorsInInsuredBuilding AS INTEGER) AS VARCHAR)",
    # Old codes (1-6) and Risk Rating 2.0 codes (11-19) mapped to the same groups. The raw code would
    # act as a hidden "rated after 2021" flag, and old code 1 covered what is now 11, 14 and 16.
    "occupancy_group": """CASE
        WHEN c.occupancyType IN (1, 11, 14, 16) THEN 'single_family'
        WHEN c.occupancyType IN (2, 12) THEN '2_4_family'
        WHEN c.occupancyType IN (3, 13, 15) THEN 'multi_family_or_condo'
        WHEN c.occupancyType IN (4, 6, 17, 18, 19) THEN 'non_residential'
    END""",
    "primary_residence": "c.primaryResidenceIndicator",
    "rental_property": "c.rentalPropertyIndicator",
    "tenant": "c.tenantIndicator",
    "condo_type": "CAST(c.condominiumCoverageTypeCode AS VARCHAR)",
    "building_description": "CAST(c.buildingDescriptionCode AS VARCHAR)",
    "small_business": "c.smallBusinessIndicatorBuilding",
    "tenure_years": _TENURE,
    "new_business": f"{_TENURE} < 1",
    "years_since_purchase": ("CASE WHEN CAST(c.propertyPurchaseDate AS DATE) <= t.period_start THEN "
                             "date_diff('day', CAST(c.propertyPurchaseDate AS DATE), t.period_start) / 365.25 END"),
    "term_no": "t.term_no",
    "year_in_term": "t.period_no",
    "multi_year_term": "t.n_scheduled > 1",
    "start_month": "CAST(MONTH(t.period_start) AS VARCHAR)",
    "prev_gap_days": "date_diff('day', t.prev_end, t.term_start)",
    "returned_after_lapse": "date_diff('day', t.prev_end, t.term_start) > 60",
}

#: FRED series -> (feature name, months of lag, transform). The lag is
#: how many months before the policy-year start we read the value, so
#: the model only sees what was already published. ``"yoy"`` = change
#: over 12 months in percent.
FRED_FEATURES = {
    "MORTGAGE30US": ("mortgage_rate", 1, "level"),
    "DGS10": ("treasury_10y", 1, "level"),
    "UNRATE": ("unemployment_rate", 1, "level"),
    "CPIAUCSL": ("cpi_yoy", 1, "yoy"),
    "CSUSHPINSA": ("home_price_yoy", 3, "yoy"),
    "FEDFUNDS": ("fed_funds", 1, "level"),
}

#: ACS column -> feature name.
ACS_FEATURES = {
    "median_household_income": "acs_median_income",
    "median_home_value": "acs_median_home_value",
    "median_age": "acs_median_age",
    "owner_occupied_share": "acs_owner_share",
    "total_population": "acs_population",
}


def feature_names(group=None):
    """List the feature names, optionally of one group only.

    Parameters
    ----------
    group : str, optional
        ``"price"``, ``"obligation"``, ``"flood_risk"``, ``"coverage"``,
        ``"property"``, ``"tenure"``, ``"history"`` or ``"context"``.

    Returns
    -------
    list of str

    Examples
    --------
    >>> feature_names("price")[:3]
    ['premium', 'policy_cost', 'premium_per_1k_coverage']
    """
    return [f[0] for f in FEATURES if group is None or f[1] == group]


def base_features_sql(population, periods, policies):
    """SQL: one row per policy-year with the keys, targets and base features.

    Parameters
    ----------
    population : str
        The notebook 04 population (``modeling_population.parquet``).
    periods : str
        The notebook 03 policy-years of the same states.
    policies : str
        The clean policies (``policies_clean``).

    Returns
    -------
    str
        A ``SELECT`` with every column of ``population`` (except
        ``term_no``, which is rebuilt as a feature), the join keys
        ``term_start``, ``prev_end``, ``community_id6``, ``county_fips`` and
        ``geoid11``, and one column per entry of :data:`BASE_SQL`.

    Examples
    --------
    >>> con.execute(f"CREATE OR REPLACE VIEW f_base AS {base_features_sql('pop', 'per', 'clean_policies')}")
    """
    feats = ",\n    ".join(f"{sql} AS {name}" for name, sql in BASE_SQL.items())
    return f"""
SELECT p.* EXCLUDE (term_no),
    t.term_start, t.prev_end, t.community_id6, c.county_fips, c.geoid11,
    {feats}
FROM {population} p
JOIN {periods} t  ON t.period_id = p.period_id
JOIN {policies} c ON c.id = p.id
"""


def previous_term_sql(base, policies):
    """SQL: premium and coverage change from the previous term of the same policy.

    Parameters
    ----------
    base : str
        View from :func:`base_features_sql`.
    policies : str
        The clean policies, to read the previous term's premium.

    Returns
    -------
    str
        A ``SELECT`` with ``period_id``, ``premium_change_pct`` and
        ``coverage_change_pct``. NULL for the first term of a policy (and
        for incomplete keys).

    Notes
    -----
    The previous term is the term of the same ``policy_key`` with the
    latest ``term_start`` before this term's start. Its premium was set
    before this term started, so the change is known at the start.
    """
    return f"""
WITH terms AS (
    SELECT DISTINCT id, policy_key, term_start FROM {base} WHERE policy_key IS NOT NULL
),
prev AS (
    SELECT id, LAG(id) OVER (PARTITION BY policy_key ORDER BY term_start, id) AS prev_id
    FROM terms
)
SELECT b.period_id,
    100.0 * (b.premium - pc.totalInsurancePremiumOfThePolicy)
          / NULLIF(pc.totalInsurancePremiumOfThePolicy, 0)                       AS premium_change_pct,
    100.0 * (b.building_coverage - pc.totalBuildingInsuranceCoverage)
          / NULLIF(pc.totalBuildingInsuranceCoverage, 0)                         AS coverage_change_pct
FROM {base} b
LEFT JOIN prev ON prev.id = b.id
LEFT JOIN {policies} pc ON pc.id = prev.prev_id
"""


def prior_claims_sql(base, claims):
    """SQL: claims on the same property **before** the policy-year starts.

    Parameters
    ----------
    base : str
        View from :func:`base_features_sql`.
    claims : str
        Claims with ``policy_key``, ``loss_date`` and ``total_paid``
        (notebook 03 ``claim_links``). Every claim with a complete key is
        used, matched to a policy-year or not, so losses from before the
        policies data starts (2009) count too.

    Returns
    -------
    str
        A ``SELECT`` with ``period_id``, ``prior_claims_n``,
        ``prior_claims_5y_n``, ``prior_claims_paid``, ``had_prior_claim``,
        ``years_since_last_claim`` and ``last_prior_loss_date`` (kept for
        the leakage check, not a feature).

    Notes
    -----
    All history columns are NULL (unknown) when the policy key is
    incomplete, because we can't look up that property's claims.

    Only losses with ``loss_date < period_start`` count. A loss on the
    start date belongs to this policy-year and is its target, so it must
    not be a feature.
    """
    return f"""
SELECT b.period_id,
    CASE WHEN b.policy_key IS NOT NULL THEN COUNT(c.loss_date) END                AS prior_claims_n,
    CASE WHEN b.policy_key IS NOT NULL THEN
         COUNT(c.loss_date) FILTER (WHERE c.loss_date >= b.period_start - INTERVAL 5 YEAR) END AS prior_claims_5y_n,
    CASE WHEN b.policy_key IS NOT NULL THEN COALESCE(SUM(c.total_paid), 0) END    AS prior_claims_paid,
    CASE WHEN b.policy_key IS NOT NULL THEN COUNT(c.loss_date) > 0 END            AS had_prior_claim,
    date_diff('day', MAX(c.loss_date), b.period_start) / 365.25                   AS years_since_last_claim,
    MAX(c.loss_date)                                                              AS last_prior_loss_date
FROM {base} b
LEFT JOIN {claims} c
       ON c.policy_key = b.policy_key
      AND c.loss_date < b.period_start
GROUP BY b.period_id, b.period_start, b.policy_key
"""


def disaster_features_sql(base, disasters):
    """SQL: flood-related disaster declarations before the policy-year starts.

    Parameters
    ----------
    base : str
        View from :func:`base_features_sql` (needs ``county_fips``).
    disasters : str
        Clean disasters (notebook 02) with ``disasterNumber``,
        ``county_fips``, ``is_statewide_or_tribal``, ``is_flood_related``
        and ``declaration_date``.

    Returns
    -------
    str
        A ``SELECT`` with ``period_id``, ``flood_disasters_12m``,
        ``days_since_flood_disaster`` and ``last_declaration_date`` (for
        the leakage check, not a feature).

    Notes
    -----
    A declaration counts for a policy when it names the policy's county,
    or is statewide (county code ``000``) in the policy's state. Only
    declarations dated **before** the start count. Each disaster counts
    once, even when it lists the county several times. Tribal
    declarations also use county code ``000`` and are treated as
    statewide, which slightly over-counts.

    The counts are computed once per (county, start date) and joined back,
    which is much faster than comparing every policy-year with every
    declaration.
    """
    return f"""
WITH counties AS (
    SELECT DISTINCT county_fips FROM {base} WHERE county_fips IS NOT NULL
),
d AS (
    SELECT DISTINCT disasterNumber, county_fips, is_statewide_or_tribal, declaration_date
    FROM {disasters}
    WHERE is_flood_related
),
by_county AS (                      -- one row per (county, disaster); statewide ones copied to every county
    SELECT disasterNumber, county_fips, declaration_date FROM d WHERE NOT is_statewide_or_tribal
    UNION
    SELECT d.disasterNumber, k.county_fips, d.declaration_date
    FROM d JOIN counties k ON LEFT(k.county_fips, 2) = LEFT(d.county_fips, 2)
    WHERE d.is_statewide_or_tribal
),
starts AS (
    SELECT DISTINCT county_fips, period_start FROM {base}
),
agg AS (
    SELECT s.county_fips, s.period_start,
        COUNT(DISTINCT x.disasterNumber) FILTER (
            WHERE x.declaration_date >= s.period_start - INTERVAL 12 MONTH)          AS flood_disasters_12m,
        date_diff('day', MAX(x.declaration_date), s.period_start)                   AS days_since_flood_disaster,
        MAX(x.declaration_date)                                                     AS last_declaration_date
    FROM starts s
    LEFT JOIN by_county x
           ON x.county_fips = s.county_fips
          AND x.declaration_date < s.period_start
    GROUP BY s.county_fips, s.period_start
)
SELECT b.period_id, a.flood_disasters_12m, a.days_since_flood_disaster, a.last_declaration_date
FROM {base} b
LEFT JOIN agg a ON a.county_fips = b.county_fips AND a.period_start = b.period_start
"""


def fred_features_sql(base, fred):
    """SQL: national economic series as of the month before the start.

    Parameters
    ----------
    base : str
        View from :func:`base_features_sql`.
    fred : str
        Monthly FRED table (notebook 02): ``month`` plus one column per
        series ID.

    Returns
    -------
    str
        A ``SELECT`` with ``period_id`` and one column per entry of
        :data:`FRED_FEATURES`.

    Notes
    -----
    Each value is read ``lag`` months before the policy-year's start
    month, so only data that was already published is used. The 12-month
    change (``yoy``) is computed before the lag.
    """
    now = ", ".join(
        (f"100.0 * ({sid} / NULLIF(LAG({sid}, 12) OVER w, 0) - 1)" if how == "yoy" else sid) + f" AS {name}_now"
        for sid, (name, lag, how) in FRED_FEATURES.items())
    lagged = ", ".join(f"LAG({name}_now, {int(lag)}) OVER w AS {name}"
                       for sid, (name, lag, how) in FRED_FEATURES.items())
    names = ", ".join(f"s2.{name}" for name, lag, how in FRED_FEATURES.values())
    return f"""
WITH m AS (
    SELECT CAST(month AS DATE) AS month, * EXCLUDE (month) FROM {fred}
),
s1 AS (
    SELECT month, {now} FROM m WINDOW w AS (ORDER BY month)
),
s2 AS (
    SELECT month, {lagged} FROM s1 WINDOW w AS (ORDER BY month)
)
SELECT b.period_id, {names}
FROM {base} b
LEFT JOIN s2 ON s2.month = CAST(date_trunc('month', b.period_start) AS DATE)
"""


def acs_features_sql(base, acs):
    """SQL: census tract features (ACS 5-year, 2018-2022).

    Parameters
    ----------
    base : str
        View from :func:`base_features_sql` (needs ``geoid11``).
    acs : str
        Clean ACS table (notebook 02) with ``geoid11`` and the columns in
        :data:`ACS_FEATURES`.

    Returns
    -------
    str
        A ``SELECT`` with ``period_id``, ``acs_matched`` and one column per
        ACS feature. ``acs_matched`` is False when the policy's tract is
        not in the 2020 tract list (FEMA may record 2010 tracts).
    """
    cols = ", ".join(f"a.{src} AS {dst}" for src, dst in ACS_FEATURES.items())
    return f"""
SELECT b.period_id, a.geoid11 IS NOT NULL AS acs_matched, {cols}
FROM {base} b
LEFT JOIN {acs} a ON a.geoid11 = b.geoid11
"""


def community_features_sql(base, community):
    """SQL: community program status, sanctions and flood map age.

    Parameters
    ----------
    base : str
        View from :func:`base_features_sql` (needs ``community_id6``).
    community : str
        Clean community status book (notebook 02).

    Returns
    -------
    str
        A ``SELECT`` with ``period_id``, ``community_emergency_program``,
        ``community_sanctioned_before`` and ``years_since_map``.

    Notes
    -----
    The status book is a **current snapshot**, not a history. Sanctions
    and map dates are used only when dated before the start. The
    Emergency Program flag has no date, so it describes the community
    today; the leakage audit (Section 5) records this caveat.
    """
    return f"""
WITH cs AS (
    SELECT community_id6,
           BOOL_OR(program_is_emergency)                                    AS emergency,
           MAX(sanction_date) FILTER (WHERE sanction_is_suspended OR sanction_is_withdrawn) AS sanction_date,
           MAX(map_date)                                                    AS map_date
    FROM {community}
    GROUP BY community_id6
)
SELECT b.period_id,
    cs.emergency                                                             AS community_emergency_program,
    COALESCE(cs.sanction_date < b.period_start, FALSE)                       AS community_sanctioned_before,
    CASE WHEN cs.map_date <= b.period_start
         THEN date_diff('day', cs.map_date, b.period_start) / 365.25 END    AS years_since_map
FROM {base} b
LEFT JOIN cs ON cs.community_id6 = b.community_id6
"""
