"""Exploratory data analysis helpers (notebook 05).

Our EDA follows one pattern for every question:

1. **Rate by group**: the target rate in each group of a feature, with
   the number of rows and a 95% confidence interval
   (:func:`rate_table`). Numbers are cut into quantile bins first
   (:func:`quantile_bins_sql`).
2. **Lift**: the group rate divided by the overall rate. A lift of 2
   means "twice as likely as average".
3. **Stability**: the same table per year (``by=[..., "period_year"]``),
   to check that a pattern is not one storm year.
4. **Strength**: the univariate AUC (:func:`univariate_auc_sql`), to rank
   the features on one scale.

Everything runs in DuckDB on the training split only, and only small
summary tables come back to pandas.
"""
import math

import matplotlib.pyplot as plt
import pandas as pd


def wilson_ci(positives, n, z=1.96):
    """95% Wilson confidence interval for a rate.

    Parameters
    ----------
    positives : int
        Number of rows with the target = 1.
    n : int
        Number of rows.
    z : float, default 1.96
        Normal quantile (1.96 gives 95%).

    Returns
    -------
    tuple of (float, float)
        Lower and upper bound, as fractions (0.01 = 1%). ``(nan, nan)``
        when ``n`` is 0.

    Notes
    -----
    The Wilson interval stays inside 0-1 and works for rare events (a
    0.5% claim rate), where the simple ``p ± 1.96·sqrt(p(1-p)/n)``
    interval can go below zero.

    Examples
    --------
    >>> [round(x, 4) for x in wilson_ci(10, 1000)]
    [0.0054, 0.0183]
    """
    if not n:
        return float("nan"), float("nan")
    p = positives / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return centre - half, centre + half


def rate_table(con, view, by, target, where="TRUE", min_n=0):
    """Target rate per group, with counts, 95% interval and lift.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Open connection.
    view : str
        View or table to read.
    by : str or list of str
        SQL expression(s) to group by, for example ``"zone_group"`` or the
        output of :func:`quantile_bins_sql`. A NULL group is shown as
        ``"(missing)"``.
    target : str
        A 0/1 target column.
    where : str, default ``"TRUE"``
        Filter, for example ``"split = 'train' AND in_pop_claim"``.
    min_n : int, default 0
        Groups with fewer rows are dropped (their rates are too noisy to
        read).

    Returns
    -------
    pandas.DataFrame
        One row per group with ``group`` (or ``group_1``, ``group_2``, ...),
        ``n``, ``share_pct`` (of all rows), ``positives``, ``rate_pct``,
        ``ci_low_pct``, ``ci_high_pct`` and ``lift`` (group rate divided
        by the overall rate).

    Examples
    --------
    >>> rate_table(con, "features", "zone_group", "CLAIM_IN_TERM",
    ...            where="split = 'train' AND in_pop_claim")
    """
    by = [by] if isinstance(by, str) else list(by)
    names = ["group"] if len(by) == 1 else [f"group_{i + 1}" for i in range(len(by))]
    sel = ", ".join(f"COALESCE(CAST(({b}) AS VARCHAR), '(missing)') AS {n}" for b, n in zip(by, names))
    df = con.sql(f"""
        SELECT {sel}, COUNT(*) AS n, SUM({target}) AS positives
        FROM {view} WHERE {where} AND {target} IS NOT NULL
        GROUP BY ALL ORDER BY ALL
    """).df()
    total_n, total_pos = df["n"].sum(), df["positives"].sum()
    overall = total_pos / total_n if total_n else float("nan")
    ci = [wilson_ci(p, n) for p, n in zip(df["positives"], df["n"])]
    df["share_pct"] = (100 * df["n"] / total_n).round(2)
    df["rate_pct"] = (100 * df["positives"] / df["n"]).round(3)
    df["ci_low_pct"] = [round(100 * lo, 3) for lo, _ in ci]
    df["ci_high_pct"] = [round(100 * hi, 3) for _, hi in ci]
    df["lift"] = (df["positives"] / df["n"] / overall).round(2)
    df = df[df["n"] >= min_n].reset_index(drop=True)
    df.attrs["overall_pct"] = round(100 * overall, 3)
    return df[[*names, "n", "share_pct", "positives", "rate_pct", "ci_low_pct", "ci_high_pct", "lift"]]


def quantile_bins_sql(con, view, col, q=5, where="TRUE"):
    """SQL ``CASE`` that cuts a number into quantile bins.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Open connection.
    view : str
        View to compute the cut points on (use the training split only).
    col : str
        Numeric column or expression.
    q : int, default 5
        Number of bins (5 = quintiles, 10 = deciles).
    where : str, default ``"TRUE"``
        Filter for computing the cut points.

    Returns
    -------
    str
        SQL expression giving labels such as ``"01: 120 to 450"`` that sort
        in order. NULL stays NULL (shown as ``"(missing)"`` by
        :func:`rate_table`). Repeated cut points (a value shared by many
        rows, like 0) are merged, so there can be fewer than ``q`` bins.

    Examples
    --------
    >>> b = quantile_bins_sql(con, "features", "premium", 5, "split = 'train'")
    >>> rate_table(con, "features", b, "CANCEL_IN_TERM", "split = 'train' AND in_pop_cancel")
    """
    probs = [i / q for i in range(1, q)]
    cuts = con.execute(f"SELECT quantile_cont({col}, {probs}) FROM {view} WHERE {where} AND {col} IS NOT NULL"
                       ).fetchone()[0] or []
    lo_hi = con.execute(f"SELECT MIN({col}), MAX({col}) FROM {view} WHERE {where}").fetchone()
    edges = sorted(set(round(float(c), 4) for c in cuts))
    bounds = [lo_hi[0], *edges, lo_hi[1]]

    def fmt(x):
        return f"{x:,.0f}" if abs(x) >= 100 else f"{x:,.2f}"

    whens = []
    for i in range(len(bounds) - 1):
        label = f"{i + 1:02d}: {fmt(bounds[i])} to {fmt(bounds[i + 1])}"
        cond = f"({col}) < {edges[i]}" if i < len(edges) else "TRUE"
        whens.append(f"WHEN {cond} THEN '{label}'")
    return f"CASE WHEN ({col}) IS NULL THEN NULL {' '.join(whens)} END"


def plot_rates(df, title, ax=None, group="group"):
    """Bar chart of a :func:`rate_table` result, with intervals and the overall rate.

    Parameters
    ----------
    df : pandas.DataFrame
        Output of :func:`rate_table` (one grouping column).
    title : str
        Chart title.
    ax : matplotlib.axes.Axes, optional
        Axes to draw on; a new figure is made if omitted.
    group : str, default ``"group"``
        Name of the grouping column.

    Returns
    -------
    matplotlib.axes.Axes
    """
    if ax is None:
        _, ax = plt.subplots(figsize=(8, 3.5))
    x = range(len(df))
    err = [df["rate_pct"] - df["ci_low_pct"], df["ci_high_pct"] - df["rate_pct"]]
    ax.bar(x, df["rate_pct"], yerr=err, capsize=3, color="#4C72B0")
    ax.set_xticks(list(x))
    ax.set_xticklabels(df[group], rotation=30, ha="right", fontsize=8)
    overall = df.attrs.get("overall_pct")
    if overall is not None:
        ax.axhline(overall, color="grey", linestyle="--", linewidth=1, label=f"overall {overall}%")
        ax.legend(fontsize=8)
    ax.set(title=title, ylabel="rate (%)")
    return ax


def univariate_auc_sql(view, feature, target, where="TRUE", kind="num"):
    """SQL for the AUC of one feature on its own (a measure of strength).

    Parameters
    ----------
    view : str
        View to read (the training split).
    feature : str
        Column name.
    target : str
        0/1 target column.
    where : str, default ``"TRUE"``
        Filter.
    kind : {"num", "cat", "flag"}, default ``"num"``
        For ``"num"``, the feature value itself is the score and rows with
        a missing value are left out. For ``"cat"`` and ``"flag"``, each
        category (missing included) is scored by its target rate.

    Returns
    -------
    str
        A ``SELECT`` returning ``n``, ``positives`` and ``auc``.

    Notes
    -----
    The AUC is the chance that a random positive row scores higher than
    a random negative row: 0.5 = no signal, 1.0 = perfect. A value below
    0.5 means the feature works in the other direction, so we also
    report ``strength = max(auc, 1 - auc)``.

    It is computed from ranks (the Mann-Whitney formula), with tied scores
    sharing their average rank. For categories, the score is the rate
    in the same data, which flatters categories with few rows; read it as
    a screening number, not a model result.

    An AUC above about 0.85 for a single feature is suspicious: it often
    means the feature leaks the outcome. The leakage audit (Section 5)
    lists those.
    """
    if kind == "num":
        scored = f"SELECT {target} AS y, CAST({feature} AS DOUBLE) AS s FROM {view} WHERE {where} AND {target} IS NOT NULL AND {feature} IS NOT NULL"
    else:
        scored = (f"SELECT {target} AS y, AVG({target}) OVER (PARTITION BY COALESCE(CAST({feature} AS VARCHAR), '(missing)')) AS s "
                  f"FROM {view} WHERE {where} AND {target} IS NOT NULL")
    return f"""
WITH s AS ({scored}),
r AS (
    SELECT y, RANK() OVER (ORDER BY s) + (COUNT(*) OVER (PARTITION BY s) - 1) / 2.0 AS rk FROM s
)
SELECT COUNT(*) AS n, SUM(y) AS positives,
       (SUM(rk) FILTER (WHERE y = 1) - SUM(y) * (SUM(y) + 1) / 2.0)
         / NULLIF(SUM(y) * (COUNT(*) - SUM(y)), 0) AS auc
FROM r
"""


def profile(con, view, columns, where="TRUE"):
    """Missing share, distinct values and range of many columns, in one scan.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Open connection.
    view : str
        View to read.
    columns : list of str
        Columns to profile.
    where : str, default ``"TRUE"``
        Filter (use the training split).

    Returns
    -------
    pandas.DataFrame
        One row per column with ``missing_pct``, ``distinct`` (approximate),
        ``min`` and ``max`` (as text, so numbers and categories fit in one
        table).
    """
    parts = []
    for c in columns:
        parts += [f"ROUND(100 * AVG(CAST({c} IS NULL AS INTEGER)), 2) AS \"{c}__missing_pct\"",
                  f"approx_count_distinct({c}) AS \"{c}__distinct\"",
                  f"CAST(MIN({c}) AS VARCHAR) AS \"{c}__min\"",
                  f"CAST(MAX({c}) AS VARCHAR) AS \"{c}__max\""]
    row = con.sql(f"SELECT {', '.join(parts)} FROM {view} WHERE {where}").df().iloc[0]
    out = [{"feature": c, "missing_pct": row[f"{c}__missing_pct"], "distinct": row[f"{c}__distinct"],
            "min": row[f"{c}__min"], "max": row[f"{c}__max"]} for c in columns]
    return pd.DataFrame(out)
