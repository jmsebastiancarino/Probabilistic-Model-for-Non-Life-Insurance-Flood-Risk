"""Rebuild policies from policy terms, split terms into policy-years, link claims.

FEMA publishes no policy number. Every row of the policies table is one
**term** (usually one year), and nothing in ``id`` connects a house's
2019 term to its 2020 term, or a claim to the term it happened in. This
module rebuilds that link from fields that stay the same across renewals
(the **policy key**, :data:`KEY_FIELDS`).

The steps, each one a SQL builder that notebook 03 turns into a view::

    clean policies --terms_sql--> terms --linked_terms_sql--> linked terms
        --periods_sql--> policy-years ("periods")
    clean claims   --claims_sql--> keyed claims
    keyed claims + periods --claim_links_sql--> one row per claim, with its period
    periods + claim links --src.targets.labeled_periods_sql--> targets

Every function returns a SQL string, so the same logic runs on one pilot
state (to look at the results) and on all states (in a loop).

Notes
-----
Linkage is done **one state at a time**. The key contains the census
tract, which lies inside one state, so no policy is split across states,
and each state fits in memory.
"""
import pandas as pd

from src.targets import cancel_category_sql, cancel_code_sql

#: Field name -> SQL expression used in the policy key. The same
#: expressions run on policies and on claims, so both sides are built the
#: same way. Codes are cast to INTEGER so ``"01"`` and ``1`` match.
#: ``geoid11`` and ``zip5`` are the zero-padded columns made in notebook 02.
KEY_FIELDS = {
    "originalNBDate": "CAST(originalNBDate AS DATE)",
    "originalConstructionDate": "CAST(originalConstructionDate AS DATE)",
    "censusGeoid": "geoid11",
    "reportedZipCode": "zip5",
    "occupancyType": "TRY_CAST(occupancyType AS INTEGER)",
    "numberOfFloorsInInsuredBuilding": "TRY_CAST(numberOfFloorsInInsuredBuilding AS INTEGER)",
}

#: Alternative keys compared in notebook 03, Section 6.2. Each is a list
#: of :data:`KEY_FIELDS` names.
ALT_KEYS = {
    "full (6 fields)": list(KEY_FIELDS),
    "without tract": [f for f in KEY_FIELDS if f != "censusGeoid"],
    "without zip": [f for f in KEY_FIELDS if f != "reportedZipCode"],
    "without floors": [f for f in KEY_FIELDS if f != "numberOfFloorsInInsuredBuilding"],
    "without construction date": [f for f in KEY_FIELDS if f != "originalConstructionDate"],
    "without occupancy": [f for f in KEY_FIELDS if f != "occupancyType"],
}

#: Cancellation categories whose terms were never real cover: voided
#: before they started, or a duplicate of another policy. They are left
#: out of linkage and of the period table.
NOT_IN_FORCE = ("void", "duplicate")

#: A term is split into 1-year periods. A leftover shorter than this
#: many days is added to the previous period instead of becoming its own
#: period (so a 368-day term stays one period).
PERIOD_TOLERANCE_DAYS = 30

#: A term counts as renewed when another term with the same key starts
#: within this many days (before or after) of its end.
RENEWAL_WINDOW_DAYS = 60


def key_expr(fields=None):
    """SQL for the policy key: a hash of the key fields, or NULL if any is missing.

    Parameters
    ----------
    fields : list of str, optional
        Names from :data:`KEY_FIELDS`. Defaults to all six.

    Returns
    -------
    str
        SQL expression of type ``UBIGINT``. ``NULL`` when any field is
        missing, because a key with a hole would match unrelated
        properties.

    Notes
    -----
    ``hash()`` turns the six values into one 64-bit number, which is
    much faster to join and group on than six columns or a long string.
    Two different keys getting the same hash is possible but very
    unlikely (about 1 pair in 10,000 across 75M terms), far below the
    real collisions measured in Section 3.2.

    Examples
    --------
    >>> key_expr(["originalNBDate", "censusGeoid"])
    'CASE WHEN (CAST(originalNBDate AS DATE)) IS NULL OR (geoid11) IS NULL THEN NULL ELSE hash(...) END'
    """
    exprs = [KEY_FIELDS[f] for f in (fields or list(KEY_FIELDS))]
    any_null = " OR ".join(f"({e}) IS NULL" for e in exprs)
    return f"CASE WHEN {any_null} THEN NULL ELSE hash({', '.join(exprs)}) END"


def terms_sql(source, fields=None):
    """SQL: one row per policy term, with the key, dates and cancellation.

    Parameters
    ----------
    source : str
        View with clean policies (``clean_policies`` or a one-state view
        of it).
    fields : list of str, optional
        Key fields; defaults to all of :data:`KEY_FIELDS`.

    Returns
    -------
    str
        A ``SELECT`` with:

        - ``id``, ``propertyState``, ``eff_year``, ``community_id6``.
        - ``policy_key`` (UBIGINT or NULL) and ``key_complete`` (bool).
        - ``term_start``, ``term_end``, ``term_days``: the scheduled term.
        - ``cancel_date``, ``cancel_code``, ``cancel_category``.
        - ``eff_end``: when cover really stopped, the cancellation date
          if it is before ``term_end``, otherwise ``term_end``.
        - ``policy_count``: units covered by the row (1 for most).
        - ``in_force``: False for void or duplicate terms and terms whose
          end is not after their start. These are left out of linkage.

    Examples
    --------
    >>> con.execute(f"CREATE OR REPLACE VIEW pilot_terms AS {terms_sql('pilot_policies')}")
    """
    key = key_expr(fields)
    cat = cancel_category_sql("cancellationVoidanceReasonCode")
    not_in_force = ", ".join(f"'{c}'" for c in NOT_IN_FORCE)
    return f"""
SELECT
    id,
    propertyState,
    eff_year,
    community_id6,
    {key}                                                        AS policy_key,
    ({key}) IS NOT NULL                                          AS key_complete,
    CAST(policyEffectiveDate AS DATE)                            AS term_start,
    CAST(policyTerminationDate AS DATE)                          AS term_end,
    date_diff('day', CAST(policyEffectiveDate AS DATE),
                     CAST(policyTerminationDate AS DATE))        AS term_days,
    CAST(cancellationDateOfFloodPolicy AS DATE)                  AS cancel_date,
    {cancel_code_sql()}                                          AS cancel_code,
    {cat}                                                        AS cancel_category,
    CASE WHEN CAST(cancellationDateOfFloodPolicy AS DATE) < CAST(policyTerminationDate AS DATE)
         THEN CAST(cancellationDateOfFloodPolicy AS DATE)
         ELSE CAST(policyTerminationDate AS DATE) END            AS eff_end,
    COALESCE(policyCount, 1)                                     AS policy_count,
    policyTerminationDate > policyEffectiveDate
        AND ({cat}) NOT IN ({not_in_force})                      AS in_force
FROM {source}
"""


def linked_terms_sql(terms, renewal_window=RENEWAL_WINDOW_DAYS):
    """SQL: order the terms of each key and find renewals and collisions.

    Parameters
    ----------
    terms : str
        View from :func:`terms_sql`.
    renewal_window : int, default :data:`RENEWAL_WINDOW_DAYS`
        Days around ``eff_end`` in which a renewal may start.

    Returns
    -------
    str
        A ``SELECT`` over the in-force terms with every column of
        ``terms`` plus:

        - ``term_no`` : 1, 2, 3, ... in order of ``term_start`` within the key.
        - ``n_terms_key`` : how many in-force terms share the key.
        - ``prev_end`` / ``next_start`` : end of the previous term and start
          of the next term with the same key.
        - ``gap_to_next_days`` : ``next_start - eff_end``. 0 = renewed on
          the day; negative = overlap; NULL = last term of the key.
        - ``starts_same_year`` : terms of the key starting in the same
          calendar year, not counting rewritten terms.
        - ``dq_key_collision`` : ``starts_same_year > 1``. Likely two
          different properties sharing one key.
        - ``renewed`` : another term of the key starts after this term's
          start and within ``renewal_window`` days of ``eff_end``.

        Window columns are NULL when the key is incomplete.

    Notes
    -----
    ``renewed`` uses ``EXISTS`` rather than the next term in order: when
    two properties share a key, the next term in order may belong to the
    other property, while ``EXISTS`` still finds the real renewal.

    A rewritten term ends on its cancellation date and the new term starts
    the same day, so it counts as renewed (the policy continued).

    Examples
    --------
    >>> con.execute(f"CREATE OR REPLACE VIEW pilot_linked AS {linked_terms_sql('pilot_terms')}")
    """
    win = int(renewal_window)
    return f"""
WITH t AS (
    SELECT * FROM {terms} WHERE in_force
),
w AS (
    SELECT t.*,
        CASE WHEN key_complete THEN ROW_NUMBER() OVER k END                    AS term_no,
        CASE WHEN key_complete THEN COUNT(*) OVER (PARTITION BY policy_key) END AS n_terms_key,
        CASE WHEN key_complete THEN LAG(eff_end) OVER k END                    AS prev_end,
        CASE WHEN key_complete THEN LEAD(term_start) OVER k END                AS next_start,
        CASE WHEN key_complete THEN
            SUM(CASE WHEN cancel_category <> 'rewrite' THEN 1 ELSE 0 END)
                OVER (PARTITION BY policy_key, YEAR(term_start)) END           AS starts_same_year
    FROM t
    WINDOW k AS (PARTITION BY policy_key ORDER BY term_start, term_end, id)
)
SELECT w.*,
    date_diff('day', eff_end, next_start)                                     AS gap_to_next_days,
    COALESCE(starts_same_year > 1, FALSE)                                     AS dq_key_collision,
    EXISTS (
        SELECT 1 FROM t AS r
        WHERE r.policy_key = w.policy_key
          AND r.id <> w.id
          AND r.term_start > w.term_start
          AND r.term_start BETWEEN w.eff_end - {win} AND w.eff_end + {win}
    )                                                                         AS renewed
FROM w
"""


def periods_sql(linked, tolerance_days=PERIOD_TOLERANCE_DAYS):
    """SQL: split every term into 1-year periods (policy-years).

    Parameters
    ----------
    linked : str
        View from :func:`linked_terms_sql`.
    tolerance_days : int, default :data:`PERIOD_TOLERANCE_DAYS`
        A leftover shorter than this is added to the previous period.

    Returns
    -------
    str
        A ``SELECT`` with every column of ``linked`` plus:

        - ``n_scheduled`` : periods the scheduled term splits into.
        - ``period_no`` : 0, 1, 2, ... within the term.
        - ``period_id`` : ``id`` + ``"_"`` + ``period_no``.
        - ``period_start`` / ``period_end`` : the period, ``period_end``
          not included. Capped at ``eff_end``, so cover after a
          cancellation is not counted.
        - ``is_last_period`` : the last period kept for the term. The
          cancellation (if any) and the renewal decision belong here.
        - ``exposure`` : length in years (``days / 365.25``). 1 for a full
          year, less for a short last period, 0 when the term was
          cancelled on its first day.
        - ``period_year`` : calendar year of ``period_start``.

    Notes
    -----
    How a term is split:

    ========================  ===========  ================================
    Term                       Periods      Lengths
    ========================  ===========  ================================
    365 days (1 year)          1            1.0
    380 days                   1            1.04 (leftover < 30 days)
    1,095 days (3 years)       3            1.0, 1.0, 1.0
    1,010 days (2.77 years)    3            1.0, 1.0, 0.77
    3-year term cancelled at   2            1.0, 0.5
    month 18
    ========================  ===========  ================================

    The first period is always kept, even with 0 exposure, so that a
    cancellation on the first day (common for non-payment) is still
    counted.

    Examples
    --------
    >>> con.execute(f"CREATE OR REPLACE VIEW pilot_periods AS {periods_sql('pilot_linked')}")
    """
    tol = int(tolerance_days)
    return f"""
WITH s AS (
    SELECT *,
        GREATEST(1, CAST(CEIL((term_days - {tol}) / 365.25) AS INTEGER)) AS n_scheduled
    FROM {linked}
),
e AS (
    SELECT *, UNNEST(range(n_scheduled)) AS period_no
    FROM s
),
p AS (
    SELECT *,
        CAST(term_start + to_years(CAST(period_no AS INTEGER)) AS DATE)       AS period_start,
        CASE WHEN period_no = n_scheduled - 1 THEN term_end
             ELSE CAST(term_start + to_years(CAST(period_no AS INTEGER) + 1) AS DATE)
        END                                                                    AS scheduled_end
    FROM e
),
k AS (
    SELECT *,
        GREATEST(period_start, LEAST(scheduled_end, eff_end))                 AS period_end
    FROM p
    WHERE period_no = 0 OR period_start < eff_end
)
SELECT *,
    CAST(id AS VARCHAR) || '_' || CAST(period_no AS VARCHAR)                  AS period_id,
    period_no = MAX(period_no) OVER (PARTITION BY id)                         AS is_last_period,
    date_diff('day', period_start, period_end) / 365.25                       AS exposure,
    YEAR(period_start)                                                        AS period_year
FROM k
"""


def claims_sql(source, fields=None):
    """SQL: one row per clean claim, with the same policy key as the terms.

    Parameters
    ----------
    source : str
        View with clean claims (``clean_claims`` or a one-state view).
        Must already have ``numberOfFloorsInInsuredBuilding`` (renamed in
        notebook 02), ``geoid11``, ``zip5``, ``loss_date`` and
        ``total_paid``.
    fields : list of str, optional
        Key fields; must be the same list used for :func:`terms_sql`.

    Returns
    -------
    str
        A ``SELECT`` with ``claim_id``, ``state``, ``loss_date``,
        ``loss_year``, ``total_paid``, ``policy_key`` and
        ``key_complete``.

    Examples
    --------
    >>> con.execute(f"CREATE OR REPLACE VIEW pilot_claims_k AS {claims_sql('pilot_claims')}")
    """
    key = key_expr(fields)
    return f"""
SELECT
    id                       AS claim_id,
    state,
    loss_date,
    YEAR(loss_date)          AS loss_year,
    total_paid,
    {key}                    AS policy_key,
    ({key}) IS NOT NULL      AS key_complete
FROM {source}
"""


def claim_links_sql(claims, periods):
    """SQL: match each claim to the period that was in force on its date of loss.

    Parameters
    ----------
    claims : str
        View from :func:`claims_sql`.
    periods : str
        View or table from :func:`periods_sql`, built with the same key.

    Returns
    -------
    str
        A ``SELECT`` with one row per claim: the columns of ``claims``
        plus ``term_id``, ``period_no`` and ``matched_term_start`` (NULL
        when not matched), ``n_matches`` (how many periods matched) and
        ``match_status``:

        - ``"key_incomplete"`` : a key field is missing on the claim.
        - ``"key_not_found"`` : no term in this state has the key.
        - ``"outside_term"`` : the key exists, but no period covers the
          date of loss (lapsed at the time, or before the policy data
          starts).
        - ``"matched"`` : exactly one period.
        - ``"matched_multi"`` : more than one period (overlapping terms of
          a colliding key). The claim is assigned to the term that
          started last.

    Notes
    -----
    A period includes its start date and excludes its end date, so a
    loss on the renewal date belongs to the new term.

    Examples
    --------
    >>> con.execute(f"CREATE OR REPLACE VIEW pilot_links AS "
    ...             f"{claim_links_sql('pilot_claims_k', 'pilot_periods')}")
    """
    return f"""
WITH keys AS (
    SELECT DISTINCT policy_key FROM {periods} WHERE policy_key IS NOT NULL
),
m AS (
    SELECT c.*,
           p.id                                          AS term_id,
           p.period_no,
           p.term_start                                  AS matched_term_start,
           COUNT(p.id) OVER (PARTITION BY c.claim_id)    AS n_matches
    FROM {claims} c
    LEFT JOIN {periods} p
           ON p.policy_key = c.policy_key
          AND c.loss_date >= p.period_start
          AND c.loss_date <  p.period_end
    QUALIFY ROW_NUMBER() OVER (PARTITION BY c.claim_id
                               ORDER BY p.term_start DESC NULLS LAST, p.id) = 1
)
SELECT m.*,
    CASE WHEN NOT m.key_complete      THEN 'key_incomplete'
         WHEN m.n_matches = 1         THEN 'matched'
         WHEN m.n_matches > 1         THEN 'matched_multi'
         WHEN k.policy_key IS NULL    THEN 'key_not_found'
         ELSE 'outside_term' END                         AS match_status
FROM m
LEFT JOIN keys k ON k.policy_key = m.policy_key
"""


def create_chain(con, policies, claims, prefix, fields=None, materialize=False):
    """Create the views terms -> linked -> periods -> keyed claims -> claim links.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Open connection.
    policies, claims : str
        Views with the clean policies and claims to link (usually one
        state each).
    prefix : str
        Prefix for the new names, for example ``"pilot_"`` gives
        ``pilot_terms``, ``pilot_linked``, ``pilot_periods``,
        ``pilot_claims_k`` and ``pilot_links``.
    fields : list of str, optional
        Key fields; defaults to all of :data:`KEY_FIELDS`.
    materialize : bool, default False
        If True, ``periods`` and ``links`` are stored as tables instead
        of views. A view is recomputed by every query that reads it; a
        table is computed once. Use True in the all-states loop.

    Returns
    -------
    dict of {str: str}
        Step -> name: ``terms``, ``linked``, ``periods``, ``claims``,
        ``links``.

    Examples
    --------
    >>> v = create_chain(con, "pilot_policies", "pilot_claims", "pilot_")
    >>> con.sql(f"SELECT COUNT(*) FROM {v['periods']}")
    """
    names = {s: f"{prefix}{s}" for s in ("terms", "linked", "periods", "claims_k", "links")}
    kind = "TABLE" if materialize else "VIEW"
    for n in (names["periods"], names["links"]):       # a name can't be both a view and a table
        _drop(con, n)
    con.execute(f"CREATE OR REPLACE VIEW {names['terms']} AS {terms_sql(policies, fields)}")
    con.execute(f"CREATE OR REPLACE VIEW {names['linked']} AS {linked_terms_sql(names['terms'])}")
    con.execute(f"CREATE {kind} {names['periods']} AS {periods_sql(names['linked'])}")
    con.execute(f"CREATE OR REPLACE VIEW {names['claims_k']} AS {claims_sql(claims, fields)}")
    con.execute(f"CREATE {kind} {names['links']} AS {claim_links_sql(names['claims_k'], names['periods'])}")
    return {"terms": names["terms"], "linked": names["linked"], "periods": names["periods"],
            "claims": names["claims_k"], "links": names["links"]}


def _drop(con, name):
    """Drop the view or table called ``name``, whichever exists (DuckDB refuses
    ``DROP VIEW`` on a table, even with ``IF EXISTS``)."""
    found = con.execute(
        "SELECT 'TABLE' FROM duckdb_tables() WHERE table_name = ? "
        "UNION ALL SELECT 'VIEW' FROM duckdb_views() WHERE view_name = ? AND NOT internal",
        [name, name]).fetchall()
    for (kind,) in found:
        con.execute(f"DROP {kind} {name}")


def term_quality_sql(periods, by=None):
    """SQL: linkage quality of the terms, optionally per group.

    Parameters
    ----------
    periods : str
        Periods view, table or ``read_parquet(...)`` expression. Only the
        first period of each term (``period_no = 0``) is used, so every
        term counts once.
    by : str or list of str, optional
        Group columns, for example ``"propertyState"``.

    Returns
    -------
    str
        A ``SELECT`` with ``terms``, ``key_complete_pct``,
        ``collision_pct`` (of complete keys), ``multi_year_pct``
        (terms with more than one period), ``renewed_pct`` and
        ``avg_terms_per_key``.

    Examples
    --------
    >>> con.sql(term_quality_sql("pilot_periods")).df()
    """
    by = [by] if isinstance(by, str) else list(by or [])
    cols = ", ".join(by)
    select = f"{cols}, " if by else ""
    group = f"GROUP BY {cols} ORDER BY {cols}" if by else ""
    return f"""
SELECT {select}
    COUNT(*)                                                               AS terms,
    ROUND(100 * AVG(CAST(key_complete AS INTEGER)), 2)                     AS key_complete_pct,
    ROUND(100 * AVG(CAST(dq_key_collision AS INTEGER)) FILTER (WHERE key_complete), 2) AS collision_pct,
    ROUND(100 * AVG(CAST(n_scheduled > 1 AS INTEGER)), 3)                  AS multi_year_pct,
    ROUND(100 * AVG(CAST(renewed AS INTEGER)) FILTER (WHERE key_complete), 2) AS renewed_pct,
    ROUND(AVG(n_terms_key) FILTER (WHERE key_complete), 2)                 AS avg_terms_per_key
FROM {periods}
WHERE period_no = 0
{group}
"""


def claim_match_sql(links, by=None, min_loss_year=2010):
    """SQL: share of claims per match status, optionally per group.

    Parameters
    ----------
    links : str
        Claim links view, table or ``read_parquet(...)`` expression.
    by : str or list of str, optional
        Group columns, for example ``"loss_year"``.
    min_loss_year : int, default 2010
        Claims with an earlier loss are left out: the policies data starts
        around 2009, so older claims have no term to match.

    Returns
    -------
    str
        A ``SELECT`` with ``claims`` and the percentage for every match
        status (``matched_pct``, ``matched_multi_pct``,
        ``outside_term_pct``, ``key_not_found_pct``,
        ``key_incomplete_pct``).

    Examples
    --------
    >>> con.sql(claim_match_sql("pilot_links", "loss_year")).df()
    """
    by = [by] if isinstance(by, str) else list(by or [])
    cols = ", ".join(by)
    select = f"{cols}, " if by else ""
    group = f"GROUP BY {cols} ORDER BY {cols}" if by else ""
    statuses = ["matched", "matched_multi", "outside_term", "key_not_found", "key_incomplete"]
    pcts = ",\n    ".join(
        f"ROUND(100 * AVG(CAST(match_status = '{s}' AS INTEGER)), 2) AS {s}_pct" for s in statuses)
    return f"""
SELECT {select}
    COUNT(*) AS claims,
    {pcts}
FROM {links}
WHERE loss_year >= {int(min_loss_year)}
{group}
"""


def compare_keys(con, policies, claims, key_sets=None, min_loss_year=2010):
    """Compare alternative policy keys on the same data.

    For every key, rebuilds the whole chain (as views, with the prefix
    ``alt_``) and measures how complete the key is, how often it
    collides, how often terms are renewed and how many claims it links.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Open connection.
    policies, claims : str
        Views with the clean policies and claims (use one small state:
        every key re-reads the data).
    key_sets : dict of {str: list of str}, optional
        Label -> key fields. Defaults to :data:`ALT_KEYS`.
    min_loss_year : int, default 2010
        Passed to :func:`claim_match_sql`.

    Returns
    -------
    pandas.DataFrame
        One row per key with ``key``, ``n_fields`` and the columns of
        :func:`term_quality_sql` and :func:`claim_match_sql`.

    Notes
    -----
    What a better key looks like: **fewer collisions** (it doesn't merge
    different houses) and **more matched claims and renewals** (it
    doesn't split one house into several keys). Dropping a field usually
    raises completeness and collisions together; the choice is a trade-off.

    Examples
    --------
    >>> compare_keys(con, "pilot_policies", "pilot_claims")
    """
    key_sets = key_sets or ALT_KEYS
    rows = []
    for label, fields in key_sets.items():
        v = create_chain(con, policies, claims, "alt_", fields=fields)
        terms = con.sql(term_quality_sql(v["periods"])).df()
        match = con.sql(claim_match_sql(v["links"], min_loss_year=min_loss_year)).df()
        rows.append(pd.concat([pd.DataFrame({"key": [label], "n_fields": [len(fields)]}), terms, match], axis=1))
    return pd.concat(rows, ignore_index=True)
