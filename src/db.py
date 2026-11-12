"""DuckDB connection and views over the raw Parquet files.

DuckDB queries the Parquet files directly on disk, so the 74.7M-row
policies file never has to fit in memory. A *view* is a saved query: it
stores no data, it only gives a short name (such as ``raw_policies``)
to ``SELECT * FROM read_parquet('...')``.
"""
from pathlib import Path

import duckdb

from src.paths import INTERIM, RAW_FEMA

#: View name -> raw Parquet file. These four views are the starting
#: point of every query in notebook 02.
RAW_FILES = {
    "raw_policies": RAW_FEMA / "nfip_policies_v3.parquet",
    "raw_claims": RAW_FEMA / "nfip_claims_v3.parquet",
    "raw_community": RAW_FEMA / "nfip_community_status_book_v1.parquet",
    "raw_disasters": RAW_FEMA / "disaster_declarations_summaries_v2.parquet",
}


def connect(db_path=INTERIM / "nfip.duckdb", memory_limit="8GB", temp_dir=INTERIM / "duckdb_tmp"):
    """Open a DuckDB connection with a memory limit and a spill folder.

    Parameters
    ----------
    db_path : str or pathlib.Path or None, default ``data/interim/nfip.duckdb``
        File that stores the views between sessions. Pass ``None`` (or
        ``":memory:"``) for a connection that keeps nothing after the
        kernel stops.
    memory_limit : str, default ``"8GB"``
        Most RAM DuckDB may use, for example ``"8GB"`` or ``"14GB"``.
        Queries that need more spill to ``temp_dir`` instead of
        crashing. Leave a few GB free for pandas and the operating
        system.
    temp_dir : str or pathlib.Path, default ``data/interim/duckdb_tmp``
        Folder for the spill files of large queries (sorts, joins,
        de-duplication of the policies table).

    Returns
    -------
    duckdb.DuckDBPyConnection
        The open connection. Use ``con.sql(...)`` for queries you want
        to look at and ``con.execute(...)`` for statements such as
        ``CREATE VIEW``.

    Notes
    -----
    ``preserve_insertion_order`` is turned off: DuckDB may return rows in
    any order, which saves memory on large scans. Add ``ORDER BY`` when
    order matters.

    The folders for ``db_path`` and ``temp_dir`` are created if needed.

    Examples
    --------
    >>> from src.db import connect
    >>> con = connect(memory_limit="12GB")
    >>> con.sql("SELECT 42").fetchall()
    [(42,)]
    """
    if db_path in (None, ":memory:"):
        target = ":memory:"
    else:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        target = str(db_path)
    Path(temp_dir).mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(target, config={"preserve_insertion_order": False})
    con.execute(f"SET memory_limit = '{memory_limit}'")
    con.execute(f"SET temp_directory = '{temp_dir}'")
    return con


def register_raw_views(con, files=None):
    """Create one view per raw Parquet file.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Open connection from :func:`connect`.
    files : dict of {str: pathlib.Path}, optional
        View name -> Parquet file. Defaults to :data:`RAW_FILES`
        (``raw_policies``, ``raw_claims``, ``raw_community``,
        ``raw_disasters``).

    Returns
    -------
    pandas.DataFrame
        The output of ``SHOW TABLES``: every view and table now in the
        database.

    Raises
    ------
    FileNotFoundError
        If a Parquet file is missing. Run notebook 01 (or
        ``python src/download.py``) first.

    Notes
    -----
    ``CREATE OR REPLACE`` is used, so running this again is safe and
    picks up a re-downloaded file.

    Examples
    --------
    >>> con = connect()
    >>> register_raw_views(con)
    """
    files = files or RAW_FILES
    for view, path in files.items():
        if not Path(path).exists():
            raise FileNotFoundError(f"{path} not found. Download the raw data first (notebook 01).")
        con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet('{path}')")
    return con.sql("SHOW TABLES").df()


def drop_views(con, names):
    """Drop views that are no longer used, if they exist.

    The database file keeps every view between sessions, so a view
    from an old version of the notebook stays until it is dropped.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Open connection.
    names : iterable of str
        View names to drop. Names that do not exist are ignored.

    Returns
    -------
    None

    Examples
    --------
    >>> drop_views(con, ["raw_policies_fixed", "raw_claims_fixed"])
    """
    for name in names:
        con.execute(f"DROP VIEW IF EXISTS {name}")


def scalar(con, sql):
    """Run a query that returns one value and return that value.

    Shortcut for ``con.execute(sql).fetchall()[0][0]``, which is easy to
    get wrong (``fetchall()[0]`` returns a tuple, not the number).

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Open connection.
    sql : str
        A query with one row and one column, such as
        ``"SELECT COUNT(*) FROM raw_claims"``.

    Returns
    -------
    object
        The single value (an ``int`` for counts).

    Examples
    --------
    >>> scalar(con, "SELECT COUNT(*) FROM raw_claims")
    2725989
    """
    rows = con.execute(sql).fetchall()
    return rows[0][0] if rows else None
