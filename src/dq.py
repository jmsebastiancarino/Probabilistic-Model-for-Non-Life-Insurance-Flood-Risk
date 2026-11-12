"""Data-quality checks and the cleaning log.

Every function here only *measures*; none of them changes data. The
changes happen in the clean views built with :mod:`src.clean`, and each
one is recorded with :class:`CleaningLog`.

Most functions take the DuckDB connection ``con`` as their first
argument, so they work in any notebook without relying on global
variables.
"""
import numbers

import pandas as pd

from src.dictionary import ZONE_PATTERN

#: Text values that mean "missing". Compared after ``UPPER(TRIM(...))``,
#: so ``"None"``, ``" none "`` and ``"NONE"`` all match.
PLACEHOLDERS = ["", "NONE", "NA", "N/A", "NULL"]


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
def sql_list(values):
    """Format Python values as the inside of a SQL ``IN (...)`` list.

    Parameters
    ----------
    values : iterable
        Numbers or strings.

    Returns
    -------
    str
        For example ``"'01', '1', 'A'"`` or ``"1, 3, 9"``. Numbers are
        written as they are; text is put in single quotes, with any
        ``'`` inside doubled so the SQL stays valid.

    Examples
    --------
    >>> sql_list(["01", "O'Brien"])
    "'01', 'O''Brien'"
    >>> sql_list([1, 3, 9])
    '1, 3, 9'
    """
    out = []
    for v in values:
        if isinstance(v, numbers.Number) and not isinstance(v, bool):
            out.append(str(v))
        else:
            out.append("'" + str(v).replace("'", "''") + "'")
    return ", ".join(out)


def text_expr(col):
    """SQL that turns any column into trimmed, uppercase text.

    Parameters
    ----------
    col : str
        Column name.

    Returns
    -------
    str
        ``UPPER(TRIM(CAST("col" AS VARCHAR)))``. Every code check in the
        project compares values in this form, so ``" a "``, ``"A"`` and
        the number ``1`` vs the text ``"1"`` are treated the same way.
    """
    return f'UPPER(TRIM(CAST("{col}" AS VARCHAR)))'


def strip_zeros(x):
    """Remove leading zeros from a code, keeping a single ``"0"``.

    Codes stored as numbers lose their leading zeros (``"01"`` becomes
    ``1``). Comparing codes after this function treats ``"01"`` and
    ``"1"`` as the same code.

    Parameters
    ----------
    x : str
        A code as text.

    Returns
    -------
    str
        ``x`` without leading zeros, or ``"0"`` if ``x`` was all zeros.

    Examples
    --------
    >>> strip_zeros("05"), strip_zeros("000"), strip_zeros("A1")
    ('5', '0', 'A1')
    """
    return x.lstrip("0") or "0"


def allowed_with_unpadded(allowed):
    """Return the allowed codes plus their versions without leading zeros.

    Parameters
    ----------
    allowed : iterable of str
        Allowed codes from the dictionary, such as ``["01", "02"]``.

    Returns
    -------
    list of str
        Sorted, without duplicates, for example
        ``["01", "02", "1", "2"]``. Used so that a value stored as
        ``1`` is not treated as invalid when the dictionary says ``01``.
    """
    allowed = list(allowed)
    return sorted(set(allowed) | {strip_zeros(a) for a in allowed})


# ----------------------------------------------------------------------
# Cleaning log
# ----------------------------------------------------------------------
class CleaningLog:
    """A list of every cleaning rule: what was done, where, why, and how many rows.

    The log is the paper trail of notebook 02. Every change to the data,
    and every finding the team decided to act on later, gets one entry.
    It ends up in ``reports/cleaning_log.csv``.

    Attributes
    ----------
    entries : list of dict
        One dict per rule with the keys ``table``, ``column``, ``rule``,
        ``reason`` and ``rows_affected``.

    Notes
    -----
    Table names are shortened to the dataset name, so ``"raw_policies"``,
    ``"dedup_policies"`` and ``"clean_policies"`` are all logged as
    ``"policies"``. This keeps the log easy to group and lets the row
    reconciliation in Section 4.3 match log entries to tables.

    How to fill the fields:

    - ``rule`` : what was **done** (``"set ['0'] to NULL"``).
    - ``reason`` : **why** (``"code 0 is not in the dictionary"``).
    - ``rows_affected`` : a number. Use ``0`` for column-level changes
      (rename, drop column) and ``None`` only when it truly cannot be
      counted.

    Examples
    --------
    >>> log = CleaningLog()
    >>> log.add("raw_claims", "ALL", "remove exact duplicates",
    ...         "identical claim records under different ids", 11_382)
    >>> log.to_frame()
         table column                     rule  ...  rows_affected
    0   claims    ALL  remove exact duplicates  ...          11382
    """

    PREFIXES = ("raw_", "dedup_", "clean_")
    SUFFIXES = ("_fixed",)

    def __init__(self):
        self.entries = []

    @classmethod
    def short_name(cls, table):
        """Shorten a view name to its dataset name (``"raw_claims"`` -> ``"claims"``)."""
        name = str(table)
        for p in cls.PREFIXES:
            if name.startswith(p):
                name = name[len(p):]
        for s in cls.SUFFIXES:
            if name.endswith(s):
                name = name[: -len(s)]
        return name

    def add(self, table, column, rule, reason, rows_affected=None):
        """Add one entry to the log.

        Parameters
        ----------
        table : str
            Table or view the rule applies to. Shortened with
            :meth:`short_name`.
        column : str
            Column (or ``"ALL"`` for whole rows, or a short description
            such as ``"linkage key fields"``).
        rule : str
            What was done.
        reason : str
            Why it was done.
        rows_affected : int or None, optional
            Number of rows changed or flagged. Numpy integers are
            converted to plain ``int``.

        Returns
        -------
        None
        """
        if rows_affected is not None and not isinstance(rows_affected, str):
            rows_affected = int(rows_affected)
        self.entries.append({"table": self.short_name(table), "column": column, "rule": rule,
                             "reason": reason, "rows_affected": rows_affected})

    def to_frame(self):
        """Return the log as a DataFrame (one row per entry, in the order added)."""
        return pd.DataFrame(self.entries, columns=["table", "column", "rule", "reason", "rows_affected"])

    def __len__(self):
        return len(self.entries)


# ----------------------------------------------------------------------
# 2.4 Allowed values
# ----------------------------------------------------------------------
def check_allowed_values(con, coded, placeholders=PLACEHOLDERS, zone_pattern=ZONE_PATTERN):
    """Compare every observed value of every coded column with its allowed list.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Open connection.
    coded : pandas.DataFrame
        Output of :func:`src.dictionary.coded_columns` (with any manual
        additions from :func:`src.dictionary.add_allowed`). Needs
        ``table``, ``name``, ``kind`` and ``allowed``.
    placeholders : list of str, default :data:`PLACEHOLDERS`
        Text values counted as missing, not as invalid.
    zone_pattern : str, default :data:`src.dictionary.ZONE_PATTERN`
        Regex for ``"zone"`` columns.

    Returns
    -------
    pandas.DataFrame
        One row per distinct value per column, with:

        - ``value`` : the value as uppercase, trimmed text (``None`` for
          true NULL).
        - ``n`` : number of rows with that value.
        - ``table``, ``column``, ``kind`` : where it comes from.
        - ``is_null`` : NULL or a placeholder.
        - ``is_allowed`` : listed in the dictionary (or matches the zone
          pattern; for ``"list"`` columns, every item in the list is
          allowed).
        - ``zero_padding_only`` : not listed, but only because a leading
          zero was lost (``"1"`` vs ``"01"``). A format difference, not
          an error. Only for ``"codes"`` columns.
        - ``is_invalid`` : not null, not allowed, and not a padding
          difference. These are the values to review.

    See Also
    --------
    summarize_allowed : One row per column from this result.

    Notes
    -----
    Runs one ``GROUP BY`` query per coded column. On the full policies
    table this takes about a minute in total.

    Examples
    --------
    >>> values = check_allowed_values(con, coded)
    >>> invalid = values[values["is_invalid"]]
    """
    results = []
    for _, r in coded.iterrows():
        observed = con.sql(f"""
            SELECT {text_expr(r['name'])} AS value, COUNT(*) AS n
            FROM {r['table']}
            GROUP BY 1
        """).df()
        observed["table"], observed["column"], observed["kind"] = r["table"], r["name"], r["kind"]
        value = observed["value"].fillna("")
        observed["is_null"] = observed["value"].isna() | value.isin(placeholders)

        allowed = set(r["allowed"])
        if r["kind"] == "zone":
            observed["is_allowed"] = value.str.match(zone_pattern)
        elif r["kind"] == "list":
            observed["is_allowed"] = value.map(lambda s: all(x.strip() in allowed for x in s.split(",")))
        else:
            observed["is_allowed"] = value.isin(allowed)

        unpadded = {strip_zeros(a) for a in allowed}
        observed["zero_padding_only"] = (~observed["is_allowed"] & ~observed["is_null"]
                                         & value.map(lambda s: strip_zeros(s) in unpadded)
                                         & (r["kind"] == "codes"))
        results.append(observed)

    values = pd.concat(results, ignore_index=True)
    values["is_invalid"] = ~values["is_allowed"] & ~values["is_null"] & ~values["zero_padding_only"]
    return values


def summarize_allowed(values):
    """Summarize :func:`check_allowed_values` to one row per column.

    Parameters
    ----------
    values : pandas.DataFrame
        Output of :func:`check_allowed_values`.

    Returns
    -------
    pandas.DataFrame
        Indexed by ``(table, column, kind)``, with ``total`` rows,
        ``nulls``, ``invalid`` rows, ``zero_padding_only`` rows,
        ``distinct_invalid`` values and ``invalid_pct`` (% of all rows),
        sorted with the most invalid columns first.

    Examples
    --------
    >>> allowed_summary = summarize_allowed(values)
    >>> allowed_summary[allowed_summary["invalid"] > 0]
    """
    return (values
            .assign(null_n=lambda d: d["n"].where(d["is_null"], 0),
                    invalid_n=lambda d: d["n"].where(d["is_invalid"], 0),
                    padding_n=lambda d: d["n"].where(d["zero_padding_only"], 0))
            .groupby(["table", "column", "kind"])
            .agg(total=("n", "sum"), nulls=("null_n", "sum"),
                 invalid=("invalid_n", "sum"), zero_padding_only=("padding_n", "sum"),
                 distinct_invalid=("is_invalid", "sum"))
            .assign(invalid_pct=lambda d: (100 * d["invalid"] / d["total"]).round(3))
            .sort_values("invalid_pct", ascending=False))


def review_invalid(invalid, values, coded):
    """Put each invalid value next to its allowed list, description and share.

    You cannot decide what to do with an invalid value without seeing
    what the dictionary allows and how many rows it affects. This table
    shows all three.

    Parameters
    ----------
    invalid : pandas.DataFrame
        Rows of :func:`check_allowed_values` where ``is_invalid`` is True.
    values : pandas.DataFrame
        The full output of :func:`check_allowed_values` (used for the
        non-null total of each column).
    coded : pandas.DataFrame
        The coded-columns table, for ``allowed`` and ``description``.

    Returns
    -------
    pandas.DataFrame
        ``invalid`` plus ``allowed``, ``description``, ``non_null`` (rows
        that are not NULL or a placeholder) and ``pct`` (the value's
        share of ``non_null``, in %), sorted by table, column and ``n``.

    Notes
    -----
    A rule of thumb for deciding (the team's call in the end):

    - ``pct`` >= 1 % : probably a real but undocumented code.
      Investigate before deciding.
    - Another source documents it : add it with
      :func:`src.dictionary.add_allowed`.
    - Obvious typo, or ``pct`` < 0.1 % : set to NULL and flag.
    """
    non_null = (values[~values["is_null"]].groupby(["table", "column"])["n"].sum().rename("non_null"))
    out = (invalid
           .merge(coded[["table", "name", "allowed", "description"]],
                  left_on=["table", "column"], right_on=["table", "name"], how="left")
           .drop(columns="name")
           .join(non_null, on=["table", "column"]))
    out["pct"] = (100 * out["n"] / out["non_null"]).round(4)
    return out.sort_values(["table", "column", "n"], ascending=[True, True, False]).reset_index(drop=True)


# ----------------------------------------------------------------------
# 2.5 Missing values and constant columns
# ----------------------------------------------------------------------
def null_profile(con, view, placeholders=PLACEHOLDERS):
    """Profile missing values and constant values for every column of a view.

    For each column, counts the true NULLs, the text placeholders that
    mean "missing" (``"None"``, ``"NA"``, ``""`` ...), the number of
    distinct values, and the most common value with its share.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Open connection.
    view : str
        View or table to profile, for example ``"raw_policies"``.
    placeholders : list of str, default :data:`PLACEHOLDERS`
        Text values counted as hidden missing values. Only checked in
        ``VARCHAR`` columns.

    Returns
    -------
    pandas.DataFrame
        One row per column of ``view``, with:

        - ``table``, ``column``, ``type`` : where it comes from and its
          DuckDB type.
        - ``total`` : number of rows in the view.
        - ``null_n`` / ``null_pct`` : true NULLs (count / % of rows).
        - ``placeholder_n`` / ``placeholder_pct`` : placeholder text
          (``hidden`` missing values).
        - ``true_missing_pct`` : ``null_pct + placeholder_pct``.
        - ``hidden_pct`` : same as ``placeholder_pct``; kept as a
          separate name for the audit table.
        - ``distinct`` : approximate number of distinct non-null values.
        - ``top_value`` : most common non-null value, as text.
        - ``top_value_pct`` : % of non-null rows holding ``top_value``
          (``NaN`` when the column is all NULL).

    See Also
    --------
    suggest_null_treatment : Turns one row into a suggested treatment.

    Notes
    -----
    ``distinct`` uses ``approx_count_distinct``, an estimate that is
    exact enough to find constant columns but can be a little off for
    columns with many values.

    Runs two queries per column; about a few minutes on the full
    policies table (74.7M rows, 87 columns).

    Examples
    --------
    >>> nulls = null_profile(con, "raw_community")
    >>> nulls.sort_values("true_missing_pct", ascending=False).head()
    """
    ph = sql_list(placeholders)
    cols = con.sql(f"DESCRIBE {view}").df()[["column_name", "column_type"]]
    total = con.execute(f"SELECT COUNT(*) FROM {view}").fetchall()[0][0]
    rows = []
    for c, t in zip(cols["column_name"], cols["column_type"]):
        placeholder = (f"""COUNT(*) FILTER (WHERE UPPER(TRIM("{c}")) IN ({ph}))"""
                       if t == "VARCHAR" else "0")
        null_n, placeholder_n, distinct = con.execute(f"""
            SELECT COUNT(*) FILTER (WHERE "{c}" IS NULL), {placeholder}, approx_count_distinct("{c}")
            FROM {view}
        """).fetchall()[0]
        top_value, top_pct = con.execute(f"""
            WITH g AS (SELECT CAST("{c}" AS VARCHAR) AS v, COUNT(*) AS n
                       FROM {view} WHERE "{c}" IS NOT NULL GROUP BY 1)
            SELECT arg_max(v, n), MAX(n) * 100.0 / NULLIF(SUM(n), 0) FROM g
        """).fetchall()[0]
        rows.append({"table": view, "column": c, "type": t, "total": total,
                     "null_n": null_n, "placeholder_n": placeholder_n,
                     "distinct": distinct, "top_value": top_value, "top_value_pct": top_pct})
    df = pd.DataFrame(rows)
    df["null_pct"] = (100 * df["null_n"] / total).round(4)
    df["placeholder_pct"] = (100 * df["placeholder_n"] / total).round(4)
    df["true_missing_pct"] = (df["null_pct"] + df["placeholder_pct"]).round(2)
    df["hidden_pct"] = df["placeholder_pct"]
    return df[["table", "column", "type", "total", "null_n", "null_pct", "placeholder_n",
               "placeholder_pct", "true_missing_pct", "hidden_pct", "distinct", "top_value",
               "top_value_pct"]]


def suggest_null_treatment(r):
    """Suggest a first-pass treatment for one column of a null profile.

    The rules are checked in order and the first match is returned. The
    result is only a suggestion: the team confirms it in the decision
    table (Section 3.0).

    Parameters
    ----------
    r : pandas.Series
        One row of :func:`null_profile`.

    Returns
    -------
    str
        One of:

        - ``"drop column: all missing"`` : every row is NULL or a
          placeholder.
        - ``"drop column: constant"`` : one value in every row (no NULLs
          either), so it carries no information.
        - ``"review: one value or missing"`` : a single value plus NULLs.
          This can be a real yes/no signal (value = yes, NULL = no), so
          never drop it without looking.
        - ``"review: near-constant"`` : one value fills >= 99 % of the
          non-null rows.
        - ``"review: mostly missing - why?"`` : >= 50 % missing.
        - ``"placeholders -> NULL"`` : has placeholder text.
        - ``"keep; handle missing in 05"`` : some missing values.
        - ``"keep"`` : nothing found.

    Notes
    -----
    The thresholds (99 % and 50 %) are rules of thumb, not standards.
    Write the team's choice in the cleaning decisions note.

    "Review" never means "drop". A near-constant column can be
    predictive when its rare value is linked to the target, and a mostly
    missing column can carry meaning (not applicable, or not collected
    in earlier years).

    Keys and target columns must be kept whatever this returns.

    Examples
    --------
    >>> nulls["suggestion"] = nulls.apply(suggest_null_treatment, axis=1)
    """
    if r["true_missing_pct"] >= 100:
        return "drop column: all missing"
    if r["distinct"] <= 1 and r["null_n"] == 0 and r["placeholder_n"] == 0:
        return "drop column: constant"
    if r["distinct"] <= 1:
        return "review: one value or missing"
    if pd.notna(r["top_value_pct"]) and r["top_value_pct"] >= 99:
        return "review: near-constant"
    if r["true_missing_pct"] >= 50:
        return "review: mostly missing - why?"
    if r["placeholder_n"] > 0:
        return "placeholders -> NULL"
    if r["true_missing_pct"] > 0:
        return "keep; handle missing in 05"
    return "keep"


# ----------------------------------------------------------------------
# 2.6 Plausibility rules
# ----------------------------------------------------------------------
def run_rules(con, view, rules):
    """Count the rows that break each plausibility rule, in one scan.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Open connection.
    view : str
        View to check.
    rules : dict of {str: str}
        Rule name -> SQL condition that is TRUE for a **violation**, for
        example ``{"negative_premium": "totalInsurancePremiumOfThePolicy < 0"}``.
        Rule names become column aliases, so use letters, digits and
        underscores.

    Returns
    -------
    pandas.DataFrame
        One row per rule with ``table``, ``rule``, ``condition``,
        ``violations`` (row count) and ``violation_pct`` (% of all rows).

    Notes
    -----
    A row where the condition is ``NULL`` (for example a missing date)
    is not counted as a violation. Missing values are counted separately
    in Section 2.5.

    All rules are computed in a single ``SELECT`` with ``COUNT(*) FILTER``,
    so the table is read only once.

    Examples
    --------
    >>> run_rules(con, "raw_disasters",
    ...           {"incident_end_before_begin": "incidentEndDate < incidentBeginDate"})
    """
    exprs = ",\n".join(f'COUNT(*) FILTER (WHERE {cond}) AS "{name}"' for name, cond in rules.items())
    row = con.sql(f"SELECT COUNT(*) AS total, {exprs} FROM {view}").df().iloc[0]
    out = pd.DataFrame({"table": view, "rule": list(rules), "condition": list(rules.values()),
                        "violations": [int(row[r]) for r in rules]})
    out["violation_pct"] = 100 * out["violations"] / row["total"]
    return out


# ----------------------------------------------------------------------
# 2.7 Duplicates
# ----------------------------------------------------------------------
#: Columns that differ between copies of the same record and must be
#: ignored when looking for duplicates: the record id, FEMA's row hash
#: and the refresh timestamp.
METADATA_COLUMNS = {"id", "hash", "lastRefresh"}


def compare_columns(con, view, exclude):
    """Return the columns of a view, minus the excluded ones, as a SQL list.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Open connection.
    view : str
        View name.
    exclude : iterable of str
        Columns to leave out (keys and :data:`METADATA_COLUMNS`), plus
        any ``dq_`` flags when checking a clean view.

    Returns
    -------
    str
        Quoted, comma-separated column names, ready for ``GROUP BY`` or
        ``PARTITION BY``.
    """
    exclude = set(exclude)
    cols = [c for c in con.sql(f"DESCRIBE {view}").df()["column_name"] if c not in exclude]
    return ", ".join(f'"{c}"' for c in cols)


def count_duplicates(con, view, exclude):
    """Count rows that are identical in every column except the excluded ones.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Open connection.
    view : str
        View to check.
    exclude : iterable of str
        Columns to ignore, usually the primary key plus
        :data:`METADATA_COLUMNS`.

    Returns
    -------
    tuple of (int, int)
        ``(extra_rows, groups)``: the number of rows that are copies
        (a group of 3 identical rows counts as 2 extra rows), and the
        number of groups with more than one row.

    Notes
    -----
    Use the **same** ``exclude`` set when counting (Section 2.7) and when
    removing duplicates (:func:`src.clean.dedup_sql`). If they differ,
    the counts will not match what is removed.

    Examples
    --------
    >>> count_duplicates(con, "raw_claims", {"id"} | METADATA_COLUMNS)
    (11382, 5382)
    """
    cols = compare_columns(con, view, exclude)
    extra, groups = con.execute(f"""
        SELECT COALESCE(SUM(n - 1), 0), COUNT(*)
        FROM (SELECT COUNT(*) AS n FROM {view} GROUP BY {cols} HAVING COUNT(*) > 1)
    """).fetchall()[0]
    return int(extra), int(groups)


def duplicate_examples(con, view, exclude, n_groups=2):
    """Show a few duplicate groups so they can be checked by eye.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Open connection.
    view : str
        View to look in.
    exclude : iterable of str
        Same ``exclude`` as in :func:`count_duplicates`.
    n_groups : int, default 2
        How many duplicate groups to show.

    Returns
    -------
    pandas.DataFrame
        The rows of the first ``n_groups`` groups, transposed (one
        column per row) so that the few differing columns are easy to
        spot. ``group`` numbers the groups.

    Notes
    -----
    Uses a hash of the compared columns, which is much lighter than a
    window over every column. On the policies table, run it on a
    sample instead (for example a view filtered to one state).
    """
    cols = compare_columns(con, view, exclude)
    return con.sql(f"""
        WITH h AS (SELECT *, hash(row({cols})) AS _row_hash FROM {view}),
             d AS (SELECT _row_hash FROM h GROUP BY 1 HAVING COUNT(*) > 1 LIMIT {int(n_groups)})
        SELECT DENSE_RANK() OVER (ORDER BY h._row_hash) AS "group", h.* EXCLUDE (_row_hash)
        FROM h JOIN d USING (_row_hash)
        ORDER BY 1
    """).df().T


# ----------------------------------------------------------------------
# 4.1 Audit of the clean views
# ----------------------------------------------------------------------
def audit_missing(con, view, placeholders=PLACEHOLDERS):
    """Count NULLs and placeholder text in every column, in one scan.

    A faster, lighter version of :func:`null_profile` for re-checking the
    clean views: no distinct counts and no top value.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Open connection.
    view : str
        View to audit.
    placeholders : list of str, default :data:`PLACEHOLDERS`
        Text values counted as hidden missing values.

    Returns
    -------
    pandas.DataFrame
        One row per column with ``column``, ``null_n``,
        ``placeholder_n`` and ``total``.
    """
    ph = sql_list(placeholders)
    cols = con.sql(f"DESCRIBE {view}").df()["column_name"].tolist()
    exprs = ",\n".join(
        f'COUNT(*) FILTER (WHERE "{c}" IS NULL) AS "null__{c}", '
        f'COUNT(*) FILTER (WHERE {text_expr(c)} IN ({ph})) AS "ph__{c}"'
        for c in cols)
    row = con.sql(f"SELECT COUNT(*) AS total, {exprs} FROM {view}").df().iloc[0]
    return pd.DataFrame({"column": cols,
                         "null_n": [int(row[f"null__{c}"]) for c in cols],
                         "placeholder_n": [int(row[f"ph__{c}"]) for c in cols],
                         "total": int(row["total"])})


def audit_invalid(con, view, coded, table_key, zone_pattern=ZONE_PATTERN, placeholders=PLACEHOLDERS):
    """Count values outside the allowed codes in a (clean) view.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Open connection.
    view : str
        View to audit, for example ``"clean_policies"``.
    coded : pandas.DataFrame
        Coded-columns table (with manual additions).
    table_key : str
        The raw table name used in ``coded``, for example
        ``"raw_policies"``.
    zone_pattern : str, default :data:`src.dictionary.ZONE_PATTERN`
        Regex for zone columns.
    placeholders : list of str, default :data:`PLACEHOLDERS`
        Placeholder text, not counted as invalid.

    Returns
    -------
    pandas.DataFrame
        One row per ``"codes"`` or ``"zone"`` column present in
        ``view``, with ``column`` and ``invalid_n``. After cleaning,
        every ``invalid_n`` should be 0.

    Notes
    -----
    Uses :func:`allowed_with_unpadded`, the same rule as the cleaning
    step, so a lost leading zero is not counted as invalid.
    """
    present = set(con.sql(f"DESCRIBE {view}").df()["column_name"])
    ph = sql_list(placeholders)
    out = []
    for _, r in coded[coded["table"] == table_key].iterrows():
        if r["kind"] not in ("codes", "zone") or r["name"] not in present:
            continue
        txt = text_expr(r["name"])
        if r["kind"] == "codes":
            bad = f"{txt} NOT IN ({sql_list(allowed_with_unpadded(r['allowed']))})"
        else:
            bad = f"NOT regexp_matches({txt}, '{zone_pattern}')"
        n = con.execute(f"""SELECT COUNT(*) FROM {view}
                            WHERE "{r['name']}" IS NOT NULL AND {txt} NOT IN ({ph}) AND {bad}""").fetchall()[0][0]
        out.append({"column": r["name"], "invalid_n": int(n)})
    return pd.DataFrame(out, columns=["column", "invalid_n"])


# ----------------------------------------------------------------------
# 4.4 Spot-check
# ----------------------------------------------------------------------
def compare_rows(con, raw_view, clean_view, ids, columns, key="id"):
    """Show raw and clean values of the same records side by side.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Open connection.
    raw_view, clean_view : str
        The raw view and its clean version.
    ids : list
        Key values of the records to show.
    columns : list of str
        Columns to compare. They must exist in both views under the
        same name.
    key : str, default ``"id"``
        Column that identifies a record in both views.

    Returns
    -------
    pandas.DataFrame
        One column per record and two rows per compared column
        (``<col>_raw`` and ``<col>_clean``), so the change made by each
        rule is easy to read. Records removed by cleaning (duplicates)
        are left out.

    Examples
    --------
    >>> ids = con.sql("SELECT id FROM raw_policies USING SAMPLE 10 ROWS").df()["id"].tolist()
    >>> compare_rows(con, "raw_policies", "clean_policies", ids, ["foundationType", "ratedFloodZone"])
    """
    id_list = sql_list(ids)
    cols = ", ".join(f'"{c}"' for c in columns)
    raw = con.sql(f'SELECT "{key}", {cols} FROM {raw_view} WHERE "{key}" IN ({id_list})').df()
    cln = con.sql(f'SELECT "{key}", {cols} FROM {clean_view} WHERE "{key}" IN ({id_list})').df()
    out = raw.merge(cln, on=key, suffixes=("_raw", "_clean"))
    ordered = [key] + [f"{c}_{s}" for c in columns for s in ("raw", "clean")]
    return out[ordered].set_index(key).T
