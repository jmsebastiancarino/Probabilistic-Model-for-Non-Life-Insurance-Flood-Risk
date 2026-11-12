"""Reusable code for the NFIP flood-risk project.

The notebooks in ``notebooks/`` tell the story of the analysis. The
functions they share live here, so every notebook (and every teammate)
uses the same version.

Modules
-------
paths
    Folder locations (raw, interim, processed, reports, docs).
db
    DuckDB connection and views over the raw Parquet files.
download
    Downloading the raw data (also usable from the command line).
dictionary
    Reading allowed codes and types from the FEMA data dictionaries.
dates
    Parsing the ``MM/DD/YY`` text dates and their legend codes.
dq
    Data-quality checks and the cleaning log.
clean
    Building the cleaned views (column cleaning, de-duplication).
geo
    State FIPS codes and geography helpers.

How to import from a notebook
-----------------------------
Run this once at the top of the notebook (it finds the repo root from
any folder)::

    import sys
    from pathlib import Path
    ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "src").is_dir())
    sys.path.insert(0, str(ROOT))

    from src.paths import RAW, REPORTS
"""
