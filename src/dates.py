"""Parse dates stored as text.

Two kinds of text dates appear in the raw data:

1. **Community Status Book** columns such as
   ``currentlyEffectiveMapDate`` hold ``MM/DD/YY`` text, sometimes with
   a legend code instead of, or after, the date: ``"09/25/09"``,
   ``"11/13/26(>)"``, ``"(NSFHA)"``, ``"05/14/19 (S)"``. These are parsed
   inside DuckDB with :func:`parse_mmddyy` and :func:`parse_with_flags`.
2. **FRED** CSV files have an ``observation_date`` column. These small
   tables are parsed in pandas with :func:`parse_dates`.
"""
import pandas as pd

#: Legend codes of the Community Status Book: flag name -> regex.
#: Each flag name becomes part of a column name
#: (``{prefix}_{flag}``), so use lowercase letters and underscores.
#: Patterns go inside single-quoted SQL text, so they must not contain
#: ``'``.
FLAGS = {
    "is_emergency":            r"\bE\b",               # community is in the Emergency Program
    "is_nsfha":                r"\(NSFHA\)",           # no Special Flood Hazard Area (all zone C)
    "map_date_future":         r"\(>\)",               # map date is after the report date
    "is_not_applicable":       r"N/A",                 # not applicable at this time
    "is_suspended":            r"\(S\)",               # suspended community
    "is_withdrawn":            r"\(W\)",               # withdrawn community
    "no_elevation_determined": r"\(M\)",               # no elevation determined (all zone A, C, X)
    "firm_by_letter":          r"\(L\)",               # original FIRM by letter (all zone A, C, X)
    "all_zone_d":              r"\(All Zone D\)",      # whole community is zone D (undetermined risk)
}


def parse_mmddyy(col, pivot=30):
    """Build a DuckDB SQL expression that parses an ``MM/DD/YY`` text column.

    The expression finds the first ``MM/DD/YY`` pattern in the trimmed
    text, turns the two-digit year into a four-digit year, and casts
    the result to ``DATE``.

    Parameters
    ----------
    col : str
        Name of the source column. It is wrapped in double quotes in the
        SQL, so mixed-case names such as ``sanctionDate`` work.
    pivot : int, default 30
        Two-digit years up to ``pivot`` become ``20YY``; later years
        become ``19YY``. With 30: ``"05/14/19"`` -> 2019-05-14 and
        ``"09/29/78"`` -> 1978-09-29.

    Returns
    -------
    str
        A SQL expression (not a full query) that gives a ``DATE``. It
        gives ``NULL`` when the text is missing, has no ``MM/DD/YY``
        pattern (for example ``"(NSFHA)"``), or is not a real date (for
        example ``"02/30/21"``).

    See Also
    --------
    parse_with_flags : Parses the date and also flags the legend codes.

    Notes
    -----
    ``TRY_CAST`` is used, so a bad value becomes ``NULL`` instead of
    stopping the query. Compare the number of ``NULL`` dates with the
    raw non-missing count to see how many values failed to parse.

    The pivot of 30 suits the Community Status Book, whose dates run
    from 1970 to a little after the report date. Change it for columns
    that can hold years after 2030.

    Only the column name is put into the SQL, never data values, but do
    not pass text from outside the project as ``col``.

    Examples
    --------
    >>> expr = parse_mmddyy("currentlyEffectiveMapDate")
    >>> con.sql(f"SELECT {expr} AS map_date FROM raw_community LIMIT 5").df()
    """
    d = f"""regexp_extract(TRIM("{col}"), '(\\d{{2}})/(\\d{{2}})/(\\d{{2}})', 0)"""
    yy = f"TRY_CAST(substr({d}, 7, 2) AS INTEGER)"
    return f"""TRY_CAST(
        CAST(CASE WHEN {yy} <= {pivot} THEN 2000 + {yy} ELSE 1900 + {yy} END AS VARCHAR)
        || '-' || substr({d}, 1, 2) || '-' || substr({d}, 4, 2) AS DATE)"""


def parse_with_flags(col, prefix, flag_names=None, flags=None):
    """Build SQL that parses a text date and flags the legend codes in it.

    Returns one parsed date column plus one true/false column per
    legend code, ready to put inside a ``SELECT``.

    Parameters
    ----------
    col : str
        Name of the source column, for example ``"sanctionDate"``.
    prefix : str
        Start of every output column name. ``"sanction"`` gives
        ``sanction_date``, ``sanction_is_suspended``, ...
    flag_names : list of str, optional
        Which flags to create. Defaults to every key of ``flags``. In
        the clean views only the codes that occur in the column are
        kept (see ``DATE_COLS`` in notebook 02).
    flags : dict of {str: str}, optional
        Flag name -> regex. Defaults to :data:`FLAGS`.

    Returns
    -------
    str
        Comma-separated SQL column expressions:

        - ``{prefix}_date`` : DATE, from :func:`parse_mmddyy`.
        - ``{prefix}_{flag}`` : BOOLEAN, one per flag. True when the
          pattern is in the text. A missing value gives False, not
          ``NULL``.

    Raises
    ------
    KeyError
        If a name in ``flag_names`` is not a key of ``flags``.

    See Also
    --------
    parse_mmddyy : Parses the date part alone.

    Examples
    --------
    All flags, to see which codes occur in a column:

    >>> con.sql(f'''SELECT sanctionDate, {parse_with_flags("sanctionDate", "sanction")}
    ...             FROM raw_community''').df()

    Only the flags that matter for this column:

    >>> parse_with_flags("sanctionDate", "sanction", ["is_suspended", "is_withdrawn"])
    """
    flags = FLAGS if flags is None else flags
    names = list(flags) if flag_names is None else list(flag_names)
    parts = [f"{parse_mmddyy(col)} AS {prefix}_date"]
    parts += [f"""regexp_matches(COALESCE("{col}", ''), '{flags[n]}') AS {prefix}_{n}""" for n in names]
    return ",\n    ".join(parts)


#: Formats tried by :func:`parse_dates`, in order.
DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%m-%d-%Y", "%Y%m%d", "%B %d, %Y"]


def parse_dates(s, formats=DATE_FORMATS):
    """Parse a pandas Series of text dates using explicit formats only.

    Each format is tried in turn, with and without a ``HH:MM:SS`` time.
    A value keeps the first format that fits. pandas is never allowed
    to guess the format.

    Parameters
    ----------
    s : pandas.Series
        Text (or mixed) values, for example FRED's ``observation_date``.
    formats : list of str, default :data:`DATE_FORMATS`
        ``strftime`` formats to try, in order of priority.

    Returns
    -------
    pandas.Series
        ``datetime64[ns]`` values with the same index as ``s``. Values
        that match no format become ``NaT``.

    Notes
    -----
    Why not ``pd.to_datetime(s, format="mixed")``? It reads both
    ``04/05/2023`` and ``05-04-2023`` month-first, which silently swaps
    day and month when a file writes slash dates day-first. Listing the
    formats makes the choice visible: here slash dates are day-first
    (``%d/%m/%Y``) and dash dates are month-first (``%m-%d-%Y``). Change
    the order if a source uses another convention.

    Count the ``NaT`` values after parsing: each one is a value in an
    unexpected format.

    Examples
    --------
    >>> parse_dates(pd.Series(["2024-01-01", "15/02/2024", "bad"]))
    0   2024-01-01
    1   2024-02-15
    2          NaT
    dtype: datetime64[ns]
    """
    s = s.astype("string").str.strip()
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    for fmt in formats:
        for f in (fmt, fmt + " %H:%M:%S"):
            out = out.fillna(pd.to_datetime(s, format=f, errors="coerce"))
    return out
