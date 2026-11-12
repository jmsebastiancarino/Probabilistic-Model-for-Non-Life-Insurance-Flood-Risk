"""Folder locations used by every notebook.

All paths are built from the location of this file, so they are correct
no matter which folder Jupyter was started from (the repo root, the
``notebooks/`` folder, or anywhere else).

Attributes
----------
ROOT : pathlib.Path
    The repository root (the folder that contains ``src/``).
RAW : pathlib.Path
    ``data/raw/``. Untouched downloads. Never edited.
RAW_FEMA, RAW_CENSUS, RAW_FRED : pathlib.Path
    Sub-folders of ``RAW`` for each data source.
INTERIM : pathlib.Path
    ``data/interim/``. The DuckDB database file and its temporary
    spill files.
PROCESSED : pathlib.Path
    ``data/processed/``. Cleaned tables written by notebook 02 and
    later notebooks.
REPORTS : pathlib.Path
    ``reports/``. Audits, cleaning logs and figures.
DOCS : pathlib.Path
    ``docs/``. Written documentation, such as the cleaning decisions.
DICTIONARIES : pathlib.Path
    ``docs/data_dictionary/``. The data dictionaries saved by
    notebook 00.

Examples
--------
>>> from src.paths import RAW_FEMA, REPORTS
>>> RAW_FEMA / "nfip_claims_v3.parquet"
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

RAW = ROOT / "data" / "raw"
RAW_FEMA = RAW / "fema"
RAW_CENSUS = RAW / "census"
RAW_FRED = RAW / "fred"

INTERIM = ROOT / "data" / "interim"
PROCESSED = ROOT / "data" / "processed"

REPORTS = ROOT / "reports"
DOCS = ROOT / "docs"
DICTIONARIES = DOCS / "data_dictionary"


def ensure_dirs():
    """Create the output folders if they do not exist yet.

    Creates ``INTERIM``, ``PROCESSED``, ``REPORTS`` and ``DICTIONARIES``.
    Folders that already exist are left alone, so this is safe to call
    at the start of every notebook. ``RAW`` is not created here: it is
    filled by the download step (notebook 01).

    Returns
    -------
    None

    Examples
    --------
    >>> from src.paths import ensure_dirs
    >>> ensure_dirs()
    """
    for folder in (INTERIM, PROCESSED, REPORTS, DICTIONARIES):
        folder.mkdir(parents=True, exist_ok=True)
