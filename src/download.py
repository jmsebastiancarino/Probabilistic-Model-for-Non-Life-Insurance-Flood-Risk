"""Download every raw dataset listed in the README into data/raw/, unchanged.

Sources
  FEMA OpenFEMA : NFIP policies v3, NFIP claims v3, disaster declarations, community status book
  Census ACS    : 5-year tract-level estimates for every state
  FRED          : monthly/weekly macro series

Usage
  python src/download.py                 # everything
  python src/download.py --only claims   # one source: policies, claims, disasters, community, acs, fred
  python src/download.py --force         # re-download files that already exist

The ACS step needs a free Census API key (https://api.census.gov/data/key_signup.html):
  export CENSUS_API_KEY=your_key
"""
import argparse
import csv
import json
import os
import shutil
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

RAW = Path(__file__).resolve().parents[1] / "data" / "raw"
UA = {"User-Agent": "flood-risk-student-project/1.0"}

FEMA_FILES = {
    "policies": ("https://www.fema.gov/about/reports-and-data/openfema/v3/NfipPoliciesV3.parquet",
                 "fema/nfip_policies_v3.parquet"),
    "claims": ("https://www.fema.gov/about/reports-and-data/openfema/v3/NfipClaimsV3.parquet",
               "fema/nfip_claims_v3.parquet"),
    "disasters": ("https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries.parquet",
                  "fema/disaster_declarations_summaries_v2.parquet"),
    "community": ("https://www.fema.gov/api/open/v1/NfipCommunityStatusBook.parquet",
                  "fema/nfip_community_status_book_v1.parquet"),
}

ACS_YEAR = 2022
ACS_VARS = {
    "B19013_001E": "median_household_income",
    "B25077_001E": "median_home_value",
    "B01002_001E": "median_age",
    "B25003_001E": "occupied_housing_units",
    "B25003_002E": "owner_occupied_units",
    "B01003_001E": "total_population",
}
STATE_FIPS = ["01", "02", "04", "05", "06", "08", "09", "10", "11", "12", "13", "15", "16", "17",
              "18", "19", "20", "21", "22", "23", "24", "25", "26", "27", "28", "29", "30", "31",
              "32", "33", "34", "35", "36", "37", "38", "39", "40", "41", "42", "44", "45", "46",
              "47", "48", "49", "50", "51", "53", "54", "55", "56", "72"]

FRED_SERIES = {
    "MORTGAGE30US": "30-year fixed mortgage rate (weekly)",
    "DGS10": "10-year Treasury yield (daily)",
    "UNRATE": "Unemployment rate (monthly)",
    "CPIAUCSL": "CPI, all urban consumers (monthly)",
    "CSUSHPINSA": "Case-Shiller US home price index (monthly)",
    "FEDFUNDS": "Effective federal funds rate (monthly)",
}


def open_url(url, retries=5, timeout=120, headers=UA):
    for attempt in range(retries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)
        except Exception as e:
            if attempt == retries - 1:
                raise
            wait = 10 * (attempt + 1)
            print(f"    retry in {wait}s ({e})")
            time.sleep(wait)


def stream(url, dest, force):
    """Stream a large file to disk via a .part file, so a failed download never looks complete."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not force:
        print(f"  skip (exists): {dest.relative_to(RAW)}")
        return
    part = dest.with_suffix(dest.suffix + ".part")
    print(f"  downloading {url}")
    with open_url(url, timeout=600) as r, open(part, "wb") as f:
        size = int(r.headers.get("Content-Length") or 0)
        done, last = 0, time.time()
        while chunk := r.read(8 * 1024 * 1024):
            f.write(chunk)
            done += len(chunk)
            if time.time() - last > 15:
                pct = f" ({done / size:.0%})" if size else ""
                print(f"    {done / 1e9:.2f} GB{pct}")
                last = time.time()
    part.rename(dest)
    print(f"  saved {dest.relative_to(RAW)} ({dest.stat().st_size / 1e6:,.1f} MB)")


def download_fema(key, force):
    url, rel = FEMA_FILES[key]
    stream(url, RAW / rel, force)


def download_acs(force):
    dest = RAW / "census" / f"acs5_{ACS_YEAR}_tract.csv"
    if dest.exists() and not force:
        print(f"  skip (exists): {dest.relative_to(RAW)}")
        return
    key = os.environ.get("CENSUS_API_KEY")
    if not key:
        raise RuntimeError("CENSUS_API_KEY is not set. Get a free key at "
                           "https://api.census.gov/data/key_signup.html, then: export CENSUS_API_KEY=...")
    dest.parent.mkdir(parents=True, exist_ok=True)
    header, rows = None, []
    for st in STATE_FIPS:
        params = {"get": "NAME," + ",".join(ACS_VARS), "for": "tract:*", "in": f"state:{st}", "key": key}
        url = f"https://api.census.gov/data/{ACS_YEAR}/acs/acs5?" + urllib.parse.urlencode(params)
        with open_url(url) as r:
            data = json.load(r)
        header = header or data[0]
        rows.extend(data[1:])
        print(f"    state {st}: {len(data) - 1:,} tracts")
    # Values kept as returned, including Census sentinel codes such as -666666666.
    with open(dest, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([ACS_VARS.get(h, h) for h in header])
        w.writerows(rows)
    print(f"  saved {dest.relative_to(RAW)} ({len(rows):,} tracts)")


def download_fred(force):
    for sid in FRED_SERIES:
        dest = RAW / "fred" / f"{sid}.csv"
        if dest.exists() and not force:
            print(f"  skip (exists): {dest.relative_to(RAW)}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        # FRED silently stalls on custom user agents, so send Python's default one.
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
        with open_url(url, headers={}) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
        print(f"  saved {dest.relative_to(RAW)}")


STEPS = {
    "disasters": lambda f: download_fema("disasters", f),
    "community": lambda f: download_fema("community", f),
    "fred": download_fred,
    "acs": download_acs,
    "claims": lambda f: download_fema("claims", f),
    "policies": lambda f: download_fema("policies", f),  # largest, so last
}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--only", choices=STEPS, nargs="+", help="download only these sources")
    p.add_argument("--force", action="store_true", help="re-download existing files")
    a = p.parse_args()

    failed = []
    for name in a.only or STEPS:
        print(f"[{name}]")
        try:
            STEPS[name](a.force)
        except Exception as e:
            print(f"  FAILED: {e}")
            failed.append(name)
    if failed:
        sys.exit(f"Failed: {', '.join(failed)}. Re-run with --only {' '.join(failed)}")
    print("All downloads complete.")


if __name__ == "__main__":
    main()
