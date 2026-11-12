"""Read allowed codes and data types from the FEMA data dictionaries.

The dictionaries are the Excel files saved by notebook 00 in
``docs/data_dictionary/``. Each has one row per column with (among
others) ``name``, ``type`` and ``description``. FEMA writes the allowed
codes inside the free-text ``description``, in several formats::

    01 : Building Sold or Removed;
    1 - Slab
    1 = Single family;
    ...: 1 - Manual; 2 - Specific; ...

This module turns those descriptions into lists that the data can be
checked against.
"""
import re

import pandas as pd

#: Finds codes written as ``CODE : text``, ``CODE = text`` or
#: ``CODE - text``. The first group captures the code (1-4 letters or
#: digits). A code must start a line, or follow ``;``, ``:``, ``.`` or
#: two spaces, so that numbers inside sentences are not picked up.
CODE_PATTERN = re.compile(r"(?:^|[\n;:.]|\s{2,})\s*([A-Za-z0-9]{1,4})\s*(?:[:=]|\s[-–]\s)\s*\S", re.M)

#: Words that :data:`CODE_PATTERN` catches but are not codes
#: (for example ``"Note: ..."`` or ``"e.g. - ..."``).
NOT_CODES = {"NOTE", "TRRP", "EG", "IE", "AND", "OR", "THE", "FOR", "WITH", "YES", "NO"}

#: Sentence FEMA uses in the description of every true/false column.
BOOL_TEXT = "Yes is indicated with either a 'true' or '1'"

#: Columns that hold a flood zone. Zones follow a pattern (A1-A30,
#: V1-V30, ...) rather than a fixed list, so they are checked with
#: :data:`ZONE_PATTERN`.
ZONE_COLUMNS = ("ratedFloodZone", "floodZoneCurrent")

#: Every valid NFIP flood zone, as a regex for DuckDB's
#: ``regexp_matches`` and pandas' ``str.match``. Values are compared
#: after ``UPPER(TRIM(...))``.
ZONE_PATTERN = (r"^(A|AE|A0?[1-9]|A[12][0-9]|A30|A99|AH|AHB|AO|AOB|AR|AR/.*|"
                r"V|VE|V0?[1-9]|V[12][0-9]|V30|X|B|C|D)$")


def parse_allowed(desc, pattern=CODE_PATTERN, not_codes=NOT_CODES):
    """Extract the allowed code values from one dictionary description.

    Finds every code written in a form like ``"01 : Single family"``,
    ``"A - Apartment"`` or ``"1 = Yes"``, changes it to uppercase, and
    removes words that look like codes but are not.

    Parameters
    ----------
    desc : str or None
        The ``description`` text of one column. A missing value is
        turned into the string ``"nan"``, which has no codes.
    pattern : re.Pattern, default :data:`CODE_PATTERN`
        Compiled regex whose first group captures one code.
    not_codes : set of str, default :data:`NOT_CODES`
        Uppercase words to throw away.

    Returns
    -------
    list of str
        The allowed codes, uppercase, sorted and without duplicates.
        An empty list means no codes were found.

    See Also
    --------
    coded_columns : Runs this function on every column of a dictionary.

    Notes
    -----
    Codes are kept as text, so ``"01"`` and ``"1"`` are different codes.
    Data stored as numbers loses the leading zero, so compare with
    :func:`src.dq.strip_zeros` in mind.

    Examples
    --------
    >>> parse_allowed("1 = Single family; 2 = 2-4 family; 3 = Other")
    ['1', '2', '3']
    >>> parse_allowed("Note: free text with no codes")
    []
    """
    codes = {c.upper() for c in pattern.findall(str(desc))}
    return sorted(codes - set(not_codes))


def coded_columns(dct, table):
    """List the coded columns in one data dictionary and their allowed values.

    Goes through every column of the dictionary and decides what kind
    of values it holds, so the raw data can be checked against them.
    Columns with fewer than two codes are left out: they are free text,
    numbers or dates, not coded columns.

    Parameters
    ----------
    dct : pandas.DataFrame
        One data dictionary. Must have the columns ``name`` and
        ``description``.
    table : str
        Name of the raw view the dictionary describes, for example
        ``"raw_policies"``. Copied into the ``table`` column of the
        result.

    Returns
    -------
    pandas.DataFrame
        One row per coded column, with the columns:

        - ``table`` : str, the value of ``table``.
        - ``name`` : str, the column name.
        - ``kind`` : str, one of

          - ``"codes"`` : one code per row, listed in the description.
          - ``"bool"`` : a true/false indicator.
          - ``"zone"`` : a flood zone, checked with
            :data:`ZONE_PATTERN` (``allowed`` is empty).
          - ``"list"`` : several codes in one cell, separated by commas.

        - ``allowed`` : list of str, sorted, without duplicates.
        - ``description`` : str, the original description.

        The columns exist even when no row qualifies, so code such as
        ``result["kind"]`` never fails.

    Raises
    ------
    KeyError
        If ``dct`` has no ``name`` or ``description`` column (for
        example a dictionary saved before notebook 00 added
        ``description`` to the census file).

    See Also
    --------
    parse_allowed : Extracts the codes from one description.
    add_allowed : Adds codes the dictionary forgot.

    Notes
    -----
    Rules for specific columns:

    - :data:`ZONE_COLUMNS` are always ``"zone"``.
    - ``primaryResidenceIndicator`` also accepts ``"Y"`` and ``"N"``,
      because its description lists them besides true/false.
    - ``designatedIncidentTypes`` (disasters) is a ``"list"`` and also
      accepts ``"0"``, which appears in the data but not in the
      description.

    Examples
    --------
    >>> coded = pd.concat([coded_columns(policies_dict, "raw_policies"),
    ...                    coded_columns(claim_dict, "raw_claims")],
    ...                   ignore_index=True)
    >>> coded["kind"].value_counts()
    """
    rows = []
    for _, r in dct.iterrows():
        name, desc = r["name"], str(r["description"])
        if name in ZONE_COLUMNS:
            kind, allowed = "zone", []
        elif BOOL_TEXT in desc:
            kind, allowed = "bool", ["0", "1", "TRUE", "FALSE"]
            if name == "primaryResidenceIndicator":
                allowed += ["Y", "N"]
        elif name == "designatedIncidentTypes":
            kind, allowed = "list", parse_allowed(desc) + ["0"]
        else:
            kind, allowed = "codes", parse_allowed(desc)
            if len(allowed) < 2:
                continue
        rows.append({"table": table, "name": name, "kind": kind,
                     "allowed": sorted(set(allowed)), "description": desc})
    return pd.DataFrame(rows, columns=["table", "name", "kind", "allowed", "description"])


def add_allowed(coded, extra):
    """Add codes that are valid but missing from the dictionary text.

    Parameters
    ----------
    coded : pandas.DataFrame
        Output of :func:`coded_columns` (several tables concatenated).
    extra : dict of {(str, str): list of str}
        ``(table, column) -> codes to add``. Every entry is a team
        decision, so write the reason next to it in the notebook.

    Returns
    -------
    pandas.DataFrame
        A copy of ``coded`` with the extra codes merged into
        ``allowed`` (sorted, no duplicates). ``coded`` itself is not
        changed.

    Raises
    ------
    KeyError
        If a ``(table, column)`` pair is not in ``coded``. This catches
        typos that would otherwise do nothing silently.

    Examples
    --------
    >>> coded = add_allowed(coded, {("raw_policies", "rateMethod"): ["RE"]})
    """
    out = coded.copy()
    for (table, column), codes in extra.items():
        mask = (out["table"] == table) & (out["name"] == column)
        if not mask.any():
            raise KeyError(f"{table}.{column} is not a coded column")
        out.loc[mask, "allowed"] = out.loc[mask, "allowed"].map(lambda a: sorted(set(a) | set(codes)))
    return out


def map_type(t, type_map):
    """Look up the type family of one type name.

    Parameters
    ----------
    t : str or None
        A type name from a dictionary (``"decimal(8,2)"``) or from DuckDB
        (``"DECIMAL(8,2)"``, ``"TIMESTAMP WITH TIME ZONE"``).
    type_map : dict of {str: str}
        Type name -> type family.

    Returns
    -------
    str or None
        The family, or ``None`` when ``t`` is missing or not in the map.
        Lookups try the exact name, then lowercase, then the name
        without its ``(precision, scale)``, then that in lowercase.

    Examples
    --------
    >>> map_type("DECIMAL(8,2)", {"decimal": "DOUBLE"})
    'DOUBLE'
    """
    if t is None or (isinstance(t, float) and pd.isna(t)):
        return None
    t = str(t).strip()
    base = re.sub(r"\(.*\)$", "", t).strip()
    for key in (t, t.lower(), base, base.lower()):
        if key in type_map:
            return type_map[key]
    return None


def compare_types(con, view, dct, type_map):
    """Compare the stored DuckDB types of a view with the dictionary types.

    Both sides are translated with the same ``type_map`` before they are
    compared. Without that, ``SMALLINT`` (stored) and ``smallint``
    (dictionary) would look different even though they are the same
    kind of number.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Open connection.
    view : str
        The view to check, for example ``"raw_policies"``.
    dct : pandas.DataFrame
        The data dictionary for that view, with ``name`` and ``type``.
    type_map : dict of {str: str}
        Type name -> the type family to compare, for example
        ``{"smallint": "INTEGER", "SMALLINT": "INTEGER"}``. Lookups try
        the exact name, then lowercase, then the name without
        precision, so ``DECIMAL(8,2)`` also matches a ``"decimal"`` key.

    Returns
    -------
    pandas.DataFrame
        Only the columns that need attention, with:

        - ``column`` : the column name.
        - ``stored_type`` / ``stored_family`` : type in the Parquet file
          and its family.
        - ``dictionary_type`` / ``dictionary_family`` : type in the
          dictionary and its family.
        - ``problem`` : ``"type differs"``, ``"type not in type_map"``
          or ``"column not in dictionary"``.

        An empty result means every column matches.

    Examples
    --------
    >>> compare_types(con, "raw_community", community_dict, type_map)
    """
    stored = con.sql(f"DESCRIBE {view}").df()[["column_name", "column_type"]]
    stored.columns = ["column", "stored_type"]
    merged = stored.merge(dct[["name", "type"]].rename(columns={"name": "column", "type": "dictionary_type"}),
                          on="column", how="left")
    merged["stored_family"] = merged["stored_type"].map(lambda t: map_type(t, type_map))
    merged["dictionary_family"] = merged["dictionary_type"].map(lambda t: map_type(t, type_map))

    def problem(r):
        if pd.isna(r["dictionary_type"]):
            return "column not in dictionary"
        if r["stored_family"] is None or r["dictionary_family"] is None:
            return "type not in type_map"
        if r["stored_family"] != r["dictionary_family"]:
            return "type differs"
        return ""

    merged["problem"] = merged.apply(problem, axis=1)
    cols = ["column", "stored_type", "stored_family", "dictionary_type", "dictionary_family", "problem"]
    return merged.loc[merged["problem"] != "", cols].reset_index(drop=True)
