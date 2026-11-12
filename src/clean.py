"""Build the cleaned views from the raw views.

The raw Parquet files are never changed. Each cleaned table is a DuckDB
view (a saved query) built from three kinds of pieces:

1. :func:`make_select` : one cleaned expression per raw column
   (placeholders and invalid codes become NULL, dropped columns are
   left out), driven by the team's decision table;
2. derived columns written in the notebook (join keys, groups, dates);
3. ``dq_`` flag columns that remember what was wrong, built with
   :func:`invalid_code_flags` and :func:`rule_flags`.

Duplicates are handled first, with :func:`dedup_sql`.
"""
import pandas as pd

from src.dictionary import ZONE_PATTERN
from src.dq import PLACEHOLDERS, allowed_with_unpadded, compare_columns, sql_list, text_expr

#: Census "not available" codes (for example -666666666 when an estimate
#: could not be computed). They must become missing values, otherwise a
#: median income of -666 million would be treated as a real number.
ACS_SENTINELS = [-111111111, -222222222, -333333333, -555555555,
                 -666666666, -888888888, -999999999]


def dedup_sql(con, view, exclude, order_by="id", mode="remove"):
    """Build a query that removes or flags rows identical except for some columns.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Open connection.
    view : str
        View to de-duplicate.
    exclude : iterable of str
        Columns ignored when deciding that two rows are identical
        (usually the primary key plus
        :data:`src.dq.METADATA_COLUMNS`). Use the same set as in
        :func:`src.dq.count_duplicates`, so the rows removed match the
        rows counted in the audit.
    order_by : str, default ``"id"``
        Within a group of identical rows, the row with the lowest value
        of this column is the one kept.
    mode : {"remove", "flag"}, default ``"remove"``
        - ``"remove"`` : keep one row per group.
        - ``"flag"`` : keep every row and add
          ``dq_identical_group_size`` (1 = unique, 2+ = size of the
          identical group).

    Returns
    -------
    str
        A ``SELECT`` statement, to use in
        ``CREATE OR REPLACE VIEW ... AS {query}``.

    Raises
    ------
    ValueError
        If ``mode`` is not ``"remove"`` or ``"flag"``.

    Notes
    -----
    Why two modes? Identical claims or disaster rows are the same record
    published twice, so the copies are removed. Identical policy rows
    can be separate units in one building that look the same after
    FEMA's redaction, so they are only flagged and the decision is made
    in notebook 04.

    Examples
    --------
    >>> q = dedup_sql(con, "raw_claims", {"id", "hash", "lastRefresh"})
    >>> con.execute(f"CREATE OR REPLACE VIEW dedup_claims AS {q}")
    """
    cols = compare_columns(con, view, exclude)
    if mode == "remove":
        return f"""SELECT * FROM {view}
                   QUALIFY ROW_NUMBER() OVER (PARTITION BY {cols} ORDER BY "{order_by}") = 1"""
    if mode == "flag":
        return f"""SELECT *, COUNT(*) OVER (PARTITION BY {cols}) AS dq_identical_group_size
                   FROM {view}"""
    raise ValueError(f"mode must be 'remove' or 'flag', not {mode!r}")


def _code_rule(c, kind, allowed, zone_pattern):
    """SQL condition that is TRUE when column ``c`` holds a disallowed value."""
    txt = text_expr(c)
    if kind == "codes":
        return f"{txt} NOT IN ({sql_list(allowed_with_unpadded(allowed))})"
    if kind == "zone":
        return f"NOT regexp_matches({txt}, '{zone_pattern}')"
    return None


def make_select(con, view, table_key, decisions, coded, placeholders=PLACEHOLDERS,
                zone_pattern=ZONE_PATTERN):
    """Build one cleaned SQL expression per column, following the decision table.

    For every column of ``view``:

    1. columns whose ``action`` contains ``"drop column"`` are left out;
    2. text columns: placeholder text (``"None"``, ``"NA"``, ...) becomes
       NULL;
    3. coded columns: values not in the allowed list (after allowing for
       lost leading zeros) become NULL;
    4. zone columns: values that do not match the zone pattern become
       NULL.

    The column keeps its name and its original type.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Open connection.
    view : str
        The view to read from, for example ``"dedup_policies"``.
    table_key : str
        The raw table name used in ``decisions`` and ``coded``, for
        example ``"raw_policies"``.
    decisions : pandas.DataFrame
        The team's decision table (Section 3.0), with ``table``,
        ``column`` and ``action``.
    coded : pandas.DataFrame
        Coded-columns table (with manual additions such as ``RE``).
    placeholders : list of str, default :data:`src.dq.PLACEHOLDERS`
        Text values that mean "missing".
    zone_pattern : str, default :data:`src.dictionary.ZONE_PATTERN`
        Regex of valid flood zones.

    Returns
    -------
    tuple of (list of str, set of str)
        - the SQL expressions, each ending in ``AS "column"``; join them
          with ``", "`` inside a ``SELECT``;
        - the names of the dropped columns (for the log).

    Notes
    -----
    Codes added to ``coded`` with :func:`src.dictionary.add_allowed` are
    kept, so record those decisions **before** calling this function.

    ``"bool"`` and ``"list"`` columns are not changed here.

    Columns of ``view`` that are not in the raw table (for example
    ``dq_identical_group_size`` from :func:`dedup_sql`) pass through
    unchanged.

    Examples
    --------
    >>> cols, dropped = make_select(con, "dedup_claims", "raw_claims", decisions, coded)
    >>> con.execute(f"CREATE OR REPLACE VIEW clean_claims AS SELECT {', '.join(cols)} FROM dedup_claims")
    """
    dec = decisions[decisions["table"] == table_key].set_index("column")
    codes = coded[coded["table"] == table_key].set_index("name")
    cols = con.sql(f"DESCRIBE {view}").df()[["column_name", "column_type"]]
    drop = set(dec.index[dec["action"].astype(str).str.contains("drop column", na=False)])
    ph = sql_list(placeholders)

    out = []
    for c, dtype in zip(cols["column_name"], cols["column_type"]):
        if c in drop:
            continue
        q = f'"{c}"'
        expr = f"CASE WHEN {text_expr(c)} IN ({ph}) THEN NULL ELSE {q} END" if dtype == "VARCHAR" else q
        if c in codes.index:
            bad = _code_rule(c, codes.loc[c, "kind"], codes.loc[c, "allowed"], zone_pattern)
            if bad:
                expr = f"CASE WHEN {bad} THEN NULL ELSE {expr} END"
        out.append(f'{expr} AS "{c}"')
    return out, drop


def invalid_code_flags(coded, table_key, columns, placeholders=PLACEHOLDERS, zone_pattern=ZONE_PATTERN):
    """Build ``dq_invalid_<column>`` flags for values that cleaning sets to NULL.

    After :func:`make_select` turns an invalid code into NULL, you can no
    longer tell it apart from a value that was missing in the source.
    These flags keep that information.

    Parameters
    ----------
    coded : pandas.DataFrame
        Coded-columns table (with manual additions).
    table_key : str
        Raw table name in ``coded``, for example ``"raw_policies"``.
    columns : iterable of str
        Columns that need a flag, usually the columns with invalid
        values in the audit (``invalid["column"]`` for this table).
        Flagging only those keeps the clean views small.
    placeholders : list of str, default :data:`src.dq.PLACEHOLDERS`
        Placeholder text is "missing", not "invalid", so it is not
        flagged here.
    zone_pattern : str, default :data:`src.dictionary.ZONE_PATTERN`
        Regex of valid flood zones.

    Returns
    -------
    list of str
        One SQL expression per column, ``... AS "dq_invalid_<column>"``.
        Each is TRUE when the **raw** value is present, not a
        placeholder, and not allowed; FALSE otherwise (never NULL).

    Notes
    -----
    The rule is the same one :func:`make_select` uses, so a flag is TRUE
    exactly when the value was set to NULL for being invalid.

    The expressions read the raw columns, so put them in the same
    ``SELECT`` as the :func:`make_select` expressions, reading from the
    raw (or de-duplicated) view.

    Examples
    --------
    >>> cols = invalid.loc[invalid["table"] == "raw_policies", "column"].unique()
    >>> invalid_code_flags(coded, "raw_policies", cols)
    """
    codes = coded[coded["table"] == table_key].set_index("name")
    ph = sql_list(placeholders)
    out = []
    for c in sorted(set(columns)):
        if c not in codes.index:
            continue
        bad = _code_rule(c, codes.loc[c, "kind"], codes.loc[c, "allowed"], zone_pattern)
        if bad:
            out.append(f'COALESCE("{c}" IS NOT NULL AND {text_expr(c)} NOT IN ({ph}) AND {bad}, FALSE) '
                       f'AS "dq_invalid_{c}"')
    return out


def rule_flags(rules):
    """Turn plausibility rules into ``dq_<rule>`` flag columns.

    Parameters
    ----------
    rules : dict of {str: str}
        Rule name -> SQL condition that is TRUE for a violation (the
        same dict passed to :func:`src.dq.run_rules`).

    Returns
    -------
    list of str
        ``COALESCE((condition), FALSE) AS dq_<rule>``. A condition that
        is NULL (for example a missing date) gives FALSE.

    Examples
    --------
    >>> rule_flags({"negative_premium": "totalInsurancePremiumOfThePolicy < 0"})
    ['COALESCE((totalInsurancePremiumOfThePolicy < 0), FALSE) AS dq_negative_premium']
    """
    return [f"COALESCE(({cond}), FALSE) AS dq_{name}" for name, cond in rules.items()]


def draft_decision(r):
    """Draft an action and a reason for one column of the raw audit.

    Used to pre-fill the decision table in Section 3.0. Every row is a
    draft: the team reviews it and overrides it where needed.

    Parameters
    ----------
    r : pandas.Series
        One row of ``dq_audit_raw`` with ``true_missing_pct``,
        ``null_n``, ``placeholder_n``, ``top_value_pct``, ``invalid_n``
        and ``zero_padding_only``.

    Returns
    -------
    tuple of (str, str)
        ``(action, reason)``. ``action`` is ``"drop column"``, a
        ``"; "``-joined list of fixes, or ``"keep as is"``.

    Notes
    -----
    A column is only proposed for dropping when it is 100 % missing, or
    when it holds one value in **every** row. A column with one value
    plus NULLs is not dropped: it can be a yes/no signal.

    Missing audit values (``NaN``, for columns that were not checked)
    count as "no issue".
    """
    def num(key):
        v = r.get(key)
        return 0 if v is None or pd.isna(v) else v

    if num("true_missing_pct") >= 100:
        return "drop column", "100% missing"
    if num("top_value_pct") >= 100 and num("null_n") == 0 and num("placeholder_n") == 0:
        return "drop column", "constant: one value in every row"
    actions, reasons = [], []
    if num("placeholder_n") > 0:
        actions.append("placeholder -> NULL")
        reasons.append("hidden missing values")
    if num("invalid_n") > 0:
        actions.append("invalid code -> NULL + dq flag")
        reasons.append("not in dictionary codes")
    if num("zero_padding_only") > 0:
        actions.append("compare codes without leading zeros")
        reasons.append("leading zeros lost")
    return ("; ".join(actions) or "keep as is"), ("; ".join(reasons) or "no issue found")


def log_table_fixes(log, table_key, decisions, invalid=None, nulls=None, plausibility=None, dropped=()):
    """Write the cleaning-log entries for one table's clean view.

    One call per table replaces dozens of hand-written ``log.add`` lines
    and takes the row counts from the audit, so the log never says
    "see audit" instead of a number.

    Parameters
    ----------
    log : src.dq.CleaningLog
        The notebook's cleaning log.
    table_key : str
        Raw table name, for example ``"raw_policies"``.
    decisions : pandas.DataFrame
        Decision table (Section 3.0), used for the reason a column was
        dropped.
    invalid : pandas.DataFrame, optional
        Invalid values from Section 2.4 (``table``, ``column``,
        ``value``, ``n``). One entry per column: values set to NULL and
        flagged with ``dq_invalid_<column>``.
    nulls : pandas.DataFrame, optional
        Null profile from Section 2.5. One entry per column with
        placeholder text turned into NULL.
    plausibility : pandas.DataFrame, optional
        Plausibility results from Section 2.6. One entry per rule with
        violations, flagged with ``dq_<rule>``.
    dropped : iterable of str, optional
        Columns left out of the clean view (from :func:`make_select`).

    Returns
    -------
    int
        Number of entries added.

    Notes
    -----
    The counts come from the **raw** audit. For claims and disasters,
    where exact duplicates are removed first, a few of the counted rows
    may belong to removed copies, so the clean view can show slightly
    fewer.

    Dropped columns get only a "drop column" entry, not placeholder or
    invalid-code entries.
    """
    dropped = set(dropped)
    before = len(log)
    dec = decisions[decisions["table"] == table_key].set_index("column")

    for c in sorted(dropped):
        reason = dec.loc[c, "reason"] if c in dec.index else "decided in Section 3.0"
        log.add(table_key, c, "drop column", reason, 0)

    if invalid is not None:
        sub = invalid[(invalid["table"] == table_key) & ~invalid["column"].isin(dropped)]
        for col, g in sub.groupby("column"):
            log.add(table_key, col, f"set {sorted(g['value'])} to NULL + dq_invalid_{col}",
                    "value not in the dictionary's allowed codes", int(g["n"].sum()))

    if nulls is not None:
        sub = nulls[(nulls["table"] == table_key) & (nulls["placeholder_n"] > 0)
                    & ~nulls["column"].isin(dropped)]
        for _, r in sub.iterrows():
            log.add(table_key, r["column"], "placeholder text -> NULL",
                    "hidden missing value ('None', 'NA', '')", int(r["placeholder_n"]))

    if plausibility is not None:
        sub = plausibility[(plausibility["table"] == table_key) & (plausibility["violations"] > 0)]
        for _, r in sub.iterrows():
            log.add(table_key, r["rule"], f"flag dq_{r['rule']} (row kept)",
                    f"implausible: {r['condition']}", int(r["violations"]))

    return len(log) - before


def fred_to_monthly(series):
    """Combine FRED series of different frequencies into one monthly table.

    Parameters
    ----------
    series : dict of {str: pandas.DataFrame}
        Series ID -> DataFrame with a parsed ``observation_date`` column
        and a numeric column named after the series ID (as FRED's CSV
        files are laid out after parsing).

    Returns
    -------
    pandas.DataFrame
        One row per month (``month`` = first day of the month) and one
        column per series: the average of the daily or weekly values in
        that month. Monthly series are unchanged.

    Notes
    -----
    Months before a series starts are ``NaN``. Rows with no date are
    dropped.

    These values are **not** lagged here. A series published after the
    month it describes (CPI, unemployment) must be lagged in notebook 05
    before it is used as a feature, or the model would see data that
    was not yet published.

    Examples
    --------
    >>> fred_monthly = fred_to_monthly(fred)
    """
    monthly = [(df.dropna(subset=["observation_date"])
                  .set_index("observation_date")[name]
                  .resample("MS").mean())
               for name, df in series.items()]
    return pd.concat(monthly, axis=1).reset_index().rename(columns={"observation_date": "month"})
