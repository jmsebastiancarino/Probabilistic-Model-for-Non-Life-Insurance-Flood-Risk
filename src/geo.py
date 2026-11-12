"""Geography helpers: state FIPS codes and community numbers.

FEMA, the Census and the disaster declarations identify places in
different ways:

- policies and claims: ``propertyState`` (two letters) and
  ``censusGeoid`` (11-digit Census tract: 2 state + 3 county + 6 tract);
- disasters: ``fipsStateCode`` (2 digits) and ``fipsCountyCode``
  (3 digits);
- communities: ``communityIdNumber`` (6 digits: 2 state + 4 community).

Codes stored as numbers lose their leading zeros (Alabama ``01``
becomes ``1``), so they are always padded back with ``LPAD``.
"""

#: Two-digit state FIPS code -> USPS state abbreviation, including DC
#: and the territories that appear in NFIP data.
STATE_FIPS_TO_ABBR = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE",
    "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA",
    "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM",
    "36": "NY", "37": "NC", "38": "ND", "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
    "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY", "60": "AS", "66": "GU", "69": "MP", "72": "PR", "78": "VI",
}


def register_state_fips(con, name="state_fips"):
    """Create a small lookup table of state FIPS codes in DuckDB.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Open connection.
    name : str, default ``"state_fips"``
        Name of the table to create.

    Returns
    -------
    str
        The table name, with columns ``fips`` (two-digit text) and
        ``state`` (two letters).

    Notes
    -----
    The table is ``TEMP``: it exists only while the connection is open,
    so run this again after restarting the kernel.

    Examples
    --------
    >>> register_state_fips(con)
    >>> con.sql("SELECT * FROM state_fips WHERE state = 'FL'").df()
    """
    values = ", ".join(f"('{k}', '{v}')" for k, v in STATE_FIPS_TO_ABBR.items())
    con.execute(f"CREATE OR REPLACE TEMP TABLE {name} AS SELECT * FROM (VALUES {values}) t(fips, state)")
    return name


def padded(col, width):
    """SQL that turns a code column into text padded with leading zeros.

    Parameters
    ----------
    col : str
        Column name.
    width : int
        Number of digits, for example 5 for a zip code or 11 for a
        Census tract.

    Returns
    -------
    str
        ``LPAD(CAST("col" AS VARCHAR), width, '0')``.

    Notes
    -----
    ``LPAD`` also **cuts** text longer than ``width`` to its first
    ``width`` characters. That is what :func:`community_id6` relies on.

    Examples
    --------
    >>> padded("reportedZipCode", 5)
    'LPAD(CAST("reportedZipCode" AS VARCHAR), 5, \\'0\\')'
    """
    return f"""LPAD(CAST("{col}" AS VARCHAR), {width}, '0')"""


def community_id6(col):
    """SQL for the six-digit community number used to join to the Status Book.

    Parameters
    ----------
    col : str
        Column with the NFIP community number, for example
        ``"nfipCommunityNumberCurrent"`` (policies) or
        ``"communityIdNumber"`` (Community Status Book).

    Returns
    -------
    str
        A SQL expression giving six characters: zero-padded on the left,
        and cut to the first six characters if longer.

    Notes
    -----
    Use the same expression on both sides of a join, so both keys are
    built the same way. Check the match rate after joining (Section 3.3).
    """
    return padded(col, 6)
