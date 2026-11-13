"""Modeling population, sampling and time-based split (notebook 04).

A *population* is the set of policy-years one model is trained and
tested on. We build two models, so there are two populations:

``claim``
    Policy-years whose claim outcome is known (key complete, not
    censored), usually without colliding keys.
``lapse``
    Policy-years whose lapse outcome is known. A policy-year lapses when
    it is cancelled for a lapse reason during the year
    (``CANCEL_IN_TERM``) or not renewed at the end (``NON_RENEWAL``),
    combined in :data:`LAPSE_SQL`. Non-renewal needs the policy key, so
    the key must be complete and, usually, not colliding. The policy-year
    in which a rewrite or a sale ended the term is left out: it is
    neither a lapse nor a normal continuation.

A population is written as an ordered list of **steps**: ``(label, SQL
condition that is TRUE for rows we keep)``. Keeping the steps in order
gives the row-count waterfall for free, and joining them with ``AND``
gives the final population flag.
"""
import pandas as pd

#: Target column of each population.
TARGETS = {"claim": "CLAIM_IN_TERM", "lapse": "LAPSE"}

#: SQL for the combined lapse target, from the two notebook 03 targets.
#: 1 if the policy-year was cancelled for a lapse reason or not renewed;
#: 0 if both are known to be 0; NULL (unknown) otherwise, for example a
#: term still running at the cutoff. In the inner years of a multi-year
#: term ``NON_RENEWAL`` is 0, so ``LAPSE`` equals ``CANCEL_IN_TERM``.
LAPSE_SQL = """CASE WHEN CANCEL_IN_TERM = 1 OR NON_RENEWAL = 1 THEN 1
     WHEN CANCEL_IN_TERM = 0 AND NON_RENEWAL = 0 THEN 0 END"""


def all_of(steps):
    """Join the conditions of a list of steps with ``AND``.

    Parameters
    ----------
    steps : list of (str, str)
        ``(label, condition)`` pairs; the condition is TRUE for rows we keep.

    Returns
    -------
    str
        One SQL condition, every step in brackets. ``"TRUE"`` for an
        empty list.

    Examples
    --------
    >>> all_of([("in FL", "propertyState = 'FL'"), ("2019+", "period_year >= 2019")])
    "(propertyState = 'FL') AND (period_year >= 2019)"
    """
    return " AND ".join(f"({cond})" for _, cond in steps) or "TRUE"


def waterfall_sql(view, steps, target=None):
    """SQL for a row-count waterfall: rows left after each step, in order.

    Parameters
    ----------
    view : str
        View or table to count.
    steps : list of (str, str)
        ``(label, keep condition)``, applied cumulatively: step *k* keeps
        the rows that pass steps 1 to *k*.
    target : str, optional
        A 0/1 target column. If given, the positives left after each step
        are counted too.

    Returns
    -------
    str
        A ``SELECT`` returning **one row** with two columns per step
        (``rows_<k>`` and, with ``target``, ``pos_<k>``). Pass the result
        to :func:`waterfall_frame` to get one row per step.

    Notes
    -----
    All steps are counted in a single scan with ``COUNT(*) FILTER``, so
    the table is read once however many steps there are.

    Examples
    --------
    >>> row = con.sql(waterfall_sql("pop_base", steps, "CLAIM_IN_TERM")).df()
    """
    parts = []
    for k in range(len(steps)):
        cond = all_of(steps[:k + 1])
        parts.append(f"COUNT(*) FILTER (WHERE {cond}) AS rows_{k}")
        if target:
            parts.append(f"COUNT(*) FILTER (WHERE {cond} AND {target} = 1) AS pos_{k}")
    return f"SELECT {', '.join(parts)} FROM {view}"


def waterfall_frame(row, steps, target=None):
    """Turn the one-row result of :func:`waterfall_sql` into a table.

    Parameters
    ----------
    row : pandas.DataFrame
        The one-row result.
    steps : list of (str, str)
        The same steps passed to :func:`waterfall_sql`.
    target : str, optional
        The same target passed to :func:`waterfall_sql`.

    Returns
    -------
    pandas.DataFrame
        One row per step with ``step`` (1, 2, ...), ``filter`` (the label),
        ``rows_left``, ``rows_removed``, ``pct_of_start`` and, with a
        target, ``positives_left`` and ``positive_pct`` (the target rate
        after the step).
    """
    r = row.iloc[0]
    out = []
    prev = None
    first = int(r["rows_0"])
    for k, (label, _) in enumerate(steps):
        rows = int(r[f"rows_{k}"])
        rec = {"step": k + 1, "filter": label, "rows_left": rows,
               "rows_removed": 0 if prev is None else prev - rows,
               "pct_of_start": round(100 * rows / first, 2) if first else None}
        if target:
            pos = int(r[f"pos_{k}"])
            rec["positives_left"] = pos
            rec["positive_pct"] = round(100 * pos / rows, 3) if rows else None
        out.append(rec)
        prev = rows
    return pd.DataFrame(out)


def split_case(year_col, split_years):
    """SQL ``CASE`` that assigns each row to a split by its year.

    Parameters
    ----------
    year_col : str
        Column with the year, for example ``"period_year"``.
    split_years : dict of {str: list of int}
        Split name -> years, for example
        ``{"train": [2019, 2020, 2021], "valid": [2022], "test": [2023]}``.

    Returns
    -------
    str
        SQL expression giving the split name, or ``NULL`` for a year in
        no split.

    Raises
    ------
    ValueError
        If a year is in more than one split.

    Examples
    --------
    >>> split_case("period_year", {"train": [2019, 2020], "test": [2021]})
    "CASE WHEN period_year IN (2019, 2020) THEN 'train' WHEN period_year IN (2021) THEN 'test' END"
    """
    seen = {}
    for name, years in split_years.items():
        for y in years:
            if y in seen:
                raise ValueError(f"{y} is in both {seen[y]} and {name}")
            seen[y] = name
    whens = " ".join(f"WHEN {year_col} IN ({', '.join(str(y) for y in years)}) THEN '{name}'"
                     for name, years in split_years.items())
    return f"CASE {whens} END"


def sample_condition(id_col, rate, seed=42):
    """SQL condition that keeps a fixed, repeatable fraction of rows.

    Parameters
    ----------
    id_col : str
        A text column that is unique per row (``period_id``).
    rate : float
        Fraction to keep, between 0 and 1. ``1`` keeps every row.
    seed : int, default 42
        Changes which rows are picked, with the same fraction.

    Returns
    -------
    str
        SQL condition, TRUE for the rows we keep.

    Notes
    -----
    The choice depends only on the row's id and the seed, not on a random
    number generator, so every teammate gets exactly the same sample, on
    every run, without saving it first.

    Examples
    --------
    >>> sample_condition("period_id", 0.25)
    "(hash(period_id || '_42') % 1000000) < 250000"
    """
    if rate >= 1:
        return "TRUE"
    return f"(hash({id_col} || '_{int(seed)}') % 1000000) < {int(round(rate * 1_000_000))}"


def rolling_folds(years, min_train_years=1):
    """Rolling-origin folds: train on every year up to *t*, test on *t + 1*.

    Parameters
    ----------
    years : tuple of (int, int)
        First and last year of the modeling scope.
    min_train_years : int, default 1
        Fewest years in the first training window.

    Returns
    -------
    pandas.DataFrame
        One row per fold with ``fold``, ``train_from``, ``train_to`` and
        ``test_year``.

    Examples
    --------
    >>> rolling_folds((2019, 2023))
       fold  train_from  train_to  test_year
    0     1        2019      2019       2020
    1     2        2019      2020       2021
    2     3        2019      2021       2022
    3     4        2019      2022       2023
    """
    first, last = years
    rows = [{"fold": i + 1, "train_from": first, "train_to": t, "test_year": t + 1}
            for i, t in enumerate(range(first + min_train_years - 1, last))]
    return pd.DataFrame(rows)


def split_summary_sql(view, target_key, by="split"):
    """SQL: size and target rate of one population, per split (or other group).

    Parameters
    ----------
    view : str
        The population view (notebook 04, Step 5.1), with ``in_pop_<key>``,
        ``sample_<key>`` and ``weight_<key>``.
    target_key : str
        ``"claim"`` or ``"lapse"`` (see :data:`TARGETS`).
    by : str or list of str, default ``"split"``
        Group column(s).

    Returns
    -------
    str
        A ``SELECT`` with, per group: ``rows`` and ``positives`` in the
        population, ``rate_pct``; ``sampled_rows`` and
        ``weighted_rate_pct``, the rate in the sample after applying the
        weights. ``weighted_rate_pct`` should equal ``rate_pct``: if it
        doesn't, the weights are wrong.

    Examples
    --------
    >>> con.sql(split_summary_sql("population", "claim")).df()
    """
    t = TARGETS[target_key]
    by = [by] if isinstance(by, str) else list(by)
    cols = ", ".join(by)
    return f"""
SELECT {cols},
       COUNT(*)                                                         AS rows,
       SUM({t})                                                         AS positives,
       ROUND(100 * AVG({t}), 3)                                         AS rate_pct,
       COUNT(*) FILTER (WHERE sample_{target_key})                      AS sampled_rows,
       ROUND(100 * SUM({t} * weight_{target_key}) FILTER (WHERE sample_{target_key})
                 / SUM(weight_{target_key}) FILTER (WHERE sample_{target_key}), 3) AS weighted_rate_pct
FROM {view}
WHERE in_pop_{target_key}
GROUP BY {cols}
ORDER BY {cols}
"""
