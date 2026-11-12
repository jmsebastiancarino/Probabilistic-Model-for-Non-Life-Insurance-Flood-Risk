"""Target variables for the claim and lapse models (notebook 03).

The unit of prediction is the **policy-year** ("period"): every policy
term is split into 1-year periods by :func:`src.linkage.periods_sql`, and
each period gets three targets:

``CLAIM_IN_TERM``
    1 if at least one linked claim has a date of loss inside the period.
``CANCEL_IN_TERM``
    1 if the policy was cancelled inside the period **for a lapse
    reason** (see :data:`CANCEL_CATEGORIES`). Rewrites, duplicates and
    voids are not lapses.
``NON_RENEWAL``
    1 if no term of the same policy starts within
    :data:`src.linkage.RENEWAL_WINDOW_DAYS` days of the term's end. Only
    the **last** period of a term has a renewal decision; the inner years
    of a multi-year term are 0, because the policy simply continues.

A target is ``NULL`` (not 0) when its outcome can't be known yet: the
period hasn't finished, or claims for it may not have been reported yet
(*censoring*), or the policy key is incomplete, so claims and renewals
can't be linked.
"""

#: Cancellation reason code -> category. Every code not listed here is
#: ``"other"``; a missing code is ``"not_cancelled"``.
#:
#: Decided in notebook 03, Section 2 (team decision, 2026-11-12):
#:
#: - ``lapse``: the policyholder stops cover without a replacement:
#:   05 non-payment, 28 insurance no longer required by lender,
#:   30 insufficient premium, 52 mortgage paid off.
#: - ``sold_removed``: 01 building sold or removed, 02 contents sold or
#:   removed. An ownership change, kept as its own flag.
#: - ``rewrite``: 03, 22, 24, 25. The policy continues under a new term.
#: - ``duplicate``: 04, 17, 26. Another policy covers the same building.
#: - ``void``: 60, 70. The policy was never in force.
CANCEL_CATEGORIES = {
    "lapse": ("05", "28", "30", "52"),
    "sold_removed": ("01", "02"),
    "rewrite": ("03", "22", "24", "25"),
    "duplicate": ("04", "17", "26"),
    "void": ("60", "70"),
}

#: Default number of days after a period ends during which claims may
#: still be reported. Periods that end later than ``cutoff - lag`` are
#: censored for the claim target. Reviewed in notebook 03, Section 7.
CLAIM_LAG_DAYS = 365


def cancel_code_sql(col="cancellationVoidanceReasonCode"):
    """SQL that normalizes a cancellation code to two characters.

    Parameters
    ----------
    col : str, default ``"cancellationVoidanceReasonCode"``
        Column with the code. It can be stored as text (``"05"``) or as a
        number (``5``).

    Returns
    -------
    str
        SQL expression: the code as text, trimmed and left-padded with
        zeros to two characters (``5`` -> ``"05"``). ``"UN"`` stays
        ``"UN"``; ``NULL`` stays ``NULL``.

    Examples
    --------
    >>> cancel_code_sql()
    'LPAD(TRIM(CAST("cancellationVoidanceReasonCode" AS VARCHAR)), 2, \\'0\\')'
    """
    return f"""LPAD(TRIM(CAST("{col}" AS VARCHAR)), 2, '0')"""


def cancel_category_sql(col="cancellationVoidanceReasonCode", categories=None):
    """SQL ``CASE`` that turns a cancellation code into its category.

    Parameters
    ----------
    col : str, default ``"cancellationVoidanceReasonCode"``
        Column with the code.
    categories : dict of {str: tuple of str}, optional
        Category -> codes. Defaults to :data:`CANCEL_CATEGORIES`.

    Returns
    -------
    str
        SQL expression giving one of the category names,
        ``"not_cancelled"`` when the code is missing, or ``"other"`` for
        any code not in ``categories``.

    See Also
    --------
    cancel_code_sql : How the code is normalized before comparing.

    Examples
    --------
    >>> con.sql(f"SELECT {cancel_category_sql()} AS cat, COUNT(*) "
    ...         "FROM clean_policies GROUP BY 1").df()
    """
    categories = categories or CANCEL_CATEGORIES
    code = cancel_code_sql(col)
    whens = "\n".join(
        f"        WHEN {code} IN ({', '.join(repr(c) for c in codes)}) THEN '{cat}'"
        for cat, codes in categories.items())
    return f"""CASE
        WHEN "{col}" IS NULL THEN 'not_cancelled'
{whens}
        ELSE 'other' END"""


def labeled_periods_sql(periods, links, community, cutoff, claim_lag_days=CLAIM_LAG_DAYS,
                        renewal_window=60):
    """SQL that adds claims, censoring flags and the three targets to the periods.

    Parameters
    ----------
    periods : str
        View or table from :func:`src.linkage.periods_sql` (one row per
        policy-year).
    links : str
        View or table from :func:`src.linkage.claim_links_sql` (one row
        per claim, with the period it was matched to).
    community : str
        The clean community view (``clean_community``), with
        ``community_id6``, ``sanction_date``, ``sanction_is_suspended``
        and ``sanction_is_withdrawn``.
    cutoff : str or datetime.date
        Last date covered by the data (``asOfDate`` of the FEMA files).
        Periods that start after it are removed.
    claim_lag_days : int, default :data:`CLAIM_LAG_DAYS`
        Days a claim may take to appear in the data after the loss.
    renewal_window : int, default 60
        Days after the term's end that a renewal may start; the same
        value used in :func:`src.linkage.linked_terms_sql`.

    Returns
    -------
    str
        A ``SELECT`` with every column of ``periods`` plus:

        - ``claim_count``, ``paid_claim_count``, ``claim_paid_total``,
          ``first_loss_date``: claims matched to the period.
          **Target-only**: never use them as features for the same period.
        - ``cancel_in_period`` : bool. The cancellation date falls in this
          period (always the last kept period of the term).
        - ``cancel_seen`` : bool. ``cancel_in_period`` and the date is on
          or before ``cutoff``.
        - ``community_sanctioned`` : bool. The community was suspended or
          withdrawn from the NFIP between the term start and 60 days after
          its end, so a non-renewal may be involuntary.
        - ``censored_claim`` : bool. The period ends after
          ``cutoff - claim_lag_days``.
        - ``censored_cancel`` : bool. The period ends after ``cutoff`` and
          no cancellation has been seen yet.
        - ``censored_renewal`` : bool. Last period whose renewal window
          ends after ``cutoff``.
        - ``CLAIM_IN_TERM``, ``CLAIM_PAID_IN_TERM``, ``CANCEL_IN_TERM``,
          ``SOLD_REMOVED_IN_TERM``, ``NON_RENEWAL`` : INTEGER 0/1, or
          ``NULL`` when the outcome can't be known.

    Notes
    -----
    When each target is ``NULL``:

    ===================== ==================================================
    Target                NULL when
    ===================== ==================================================
    CLAIM_IN_TERM         key incomplete (claims can't be linked) or
                          ``censored_claim``
    CLAIM_PAID_IN_TERM    same as CLAIM_IN_TERM
    CANCEL_IN_TERM        ``censored_cancel`` (the key isn't needed: the
                          cancellation is on the term's own row)
    SOLD_REMOVED_IN_TERM  same as CANCEL_IN_TERM
    NON_RENEWAL           key incomplete or ``censored_renewal``; 0 for
                          inner periods of a multi-year term
    ===================== ==================================================

    The community sanction table has one row per community after
    grouping, so the join can't duplicate periods.

    Examples
    --------
    >>> con.execute(f"CREATE OR REPLACE VIEW pilot_labeled AS "
    ...             f"{labeled_periods_sql('pilot_periods', 'pilot_links', 'clean_community', CUTOFF)}")
    """
    cutoff = f"DATE '{cutoff}'"
    return f"""
WITH claim_totals AS (
    SELECT term_id, period_no,
           COUNT(*)                                  AS claim_count,
           COUNT(*) FILTER (WHERE total_paid > 0)    AS paid_claim_count,
           SUM(total_paid)                           AS claim_paid_total,
           MIN(loss_date)                            AS first_loss_date
    FROM {links}
    WHERE term_id IS NOT NULL
    GROUP BY term_id, period_no
),
sanctions AS (
    SELECT community_id6, MAX(sanction_date) AS sanction_date
    FROM {community}
    WHERE sanction_is_suspended OR sanction_is_withdrawn
    GROUP BY community_id6
),
base AS (
    SELECT p.*,
           COALESCE(c.claim_count, 0)       AS claim_count,
           COALESCE(c.paid_claim_count, 0)  AS paid_claim_count,
           COALESCE(c.claim_paid_total, 0)  AS claim_paid_total,
           c.first_loss_date,
           p.is_last_period AND p.cancel_date IS NOT NULL
               AND p.cancel_date <= p.term_end                         AS cancel_in_period,
           COALESCE(s.sanction_date BETWEEN p.term_start AND p.eff_end + {renewal_window},
                    FALSE)                                             AS community_sanctioned,
           p.period_end > {cutoff} - {int(claim_lag_days)}             AS censored_claim,
           p.is_last_period AND p.eff_end + {renewal_window} > {cutoff} AS censored_renewal
    FROM {periods} p
    LEFT JOIN claim_totals c ON c.term_id = p.id AND c.period_no = p.period_no
    LEFT JOIN sanctions s    ON s.community_id6 = p.community_id6
    WHERE p.period_start <= {cutoff}
),
seen AS (
    SELECT *,
           cancel_in_period AND cancel_date <= {cutoff}                AS cancel_seen
    FROM base
),
flags AS (
    SELECT *,
           NOT cancel_seen AND period_end > {cutoff}                   AS censored_cancel
    FROM seen
)
SELECT *,
    CASE WHEN NOT key_complete OR censored_claim THEN NULL
         ELSE CAST(claim_count > 0 AS INTEGER) END                     AS CLAIM_IN_TERM,
    CASE WHEN NOT key_complete OR censored_claim THEN NULL
         ELSE CAST(paid_claim_count > 0 AS INTEGER) END                AS CLAIM_PAID_IN_TERM,
    CASE WHEN censored_cancel THEN NULL
         ELSE CAST(cancel_seen AND cancel_category = 'lapse' AS INTEGER) END        AS CANCEL_IN_TERM,
    CASE WHEN censored_cancel THEN NULL
         ELSE CAST(cancel_seen AND cancel_category = 'sold_removed' AS INTEGER) END AS SOLD_REMOVED_IN_TERM,
    CASE WHEN NOT key_complete OR censored_renewal THEN NULL
         WHEN NOT is_last_period THEN 0
         ELSE CAST(NOT renewed AS INTEGER) END                         AS NON_RENEWAL
FROM flags
"""


def target_rates_sql(view, by):
    """SQL for the target rates per group.

    Parameters
    ----------
    view : str
        A labeled view or table (output of :func:`labeled_periods_sql`).
    by : str or list of str
        Column(s) to group by, for example ``"period_year"`` or
        ``["propertyState", "period_year"]``.

    Returns
    -------
    str
        A ``SELECT`` with, per group: ``periods``, ``exposure_years``,
        and for each target the number of labeled (non-NULL) periods
        and the rate in percent. ``non_renewal_pct`` uses only the last
        period of each term, where a renewal decision exists.

    Notes
    -----
    Rates ignore ``NULL`` (censored or unlinkable) periods, so the
    denominator differs per target. Compare ``*_labeled`` with
    ``periods`` to see how much was censored.

    Examples
    --------
    >>> con.sql(target_rates_sql("periods_all", "period_year")).df()
    """
    by = [by] if isinstance(by, str) else list(by)
    cols = ", ".join(by)
    return f"""
SELECT {cols},
       COUNT(*)                                             AS periods,
       ROUND(SUM(exposure), 0)                              AS exposure_years,
       COUNT(CLAIM_IN_TERM)                                 AS claim_labeled,
       ROUND(100 * AVG(CLAIM_IN_TERM), 3)                   AS claim_pct,
       ROUND(100 * AVG(CLAIM_PAID_IN_TERM), 3)              AS claim_paid_pct,
       COUNT(CANCEL_IN_TERM)                                AS cancel_labeled,
       ROUND(100 * AVG(CANCEL_IN_TERM), 3)                  AS cancel_pct,
       ROUND(100 * AVG(SOLD_REMOVED_IN_TERM), 3)            AS sold_removed_pct,
       COUNT(NON_RENEWAL) FILTER (WHERE is_last_period)     AS renewal_labeled,
       ROUND(100 * AVG(NON_RENEWAL) FILTER (WHERE is_last_period), 3) AS non_renewal_pct
FROM {view}
GROUP BY {cols}
ORDER BY {cols}
"""
