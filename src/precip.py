"""Rainfall features from NOAA CPC Unified gauge-based daily precipitation (notebook 02b).

NOAA's Climate Prediction Center (CPC) grids the daily totals measured by
rain gauges. NOAA PSL publishes one NetCDF file per year:

* **US** (0.25 degree, mainland US and nearby): V1.0 for 1948-2006 and the
  real-time version (RT) from 2007. Lat 20.125..49.875 (ascending),
  lon 230.125..304.875.
* **Global** (0.5 degree), from 1979. Lat 89.75..-89.75 (**descending**),
  lon 0.25..359.75. We use it where the US grid has no value: Alaska,
  Hawaii, Puerto Rico, the Virgin Islands and the Pacific territories.

Values are daily totals in mm (a CPC day runs 12:00 to 12:00 UTC); missing
cells (sea, no gauges) are masked. Both grids use longitude 0 to 360, so
our longitudes (-180 to 180) are converted with ``lon % 360``.

What we build
-------------
* **Static** (one value per place): average annual rainfall, average
  wettest day and 100-year 1-day rainfall over 1981-2013, all before our
  first policy-year. Known at every start date.
* **Dated** (per grid cell and start date): rainfall in windows that end
  the **day before** the start: last 90 days, last 365 days (also as % of
  normal), wettest day and wettest 3 days of the last 365 days, and days
  with 50+ mm in the last 3 years. Rain during the policy-year is never a
  feature.

Steps (see notebook 02b)
------------------------
1. :func:`download` every year of both grids into ``data/raw/cpc_precip``.
2. :func:`valid_mask` and :func:`map_locations`: each policy location to
   the nearest grid cell with gauge values (US grid first, then global).
3. :func:`gather_year`: each year's daily values at our cells only.
4. :func:`annual_from_matrix` and :func:`climate_summary`: the static
   features (GEV by L-moments, :func:`gev_lmoments`).
5. :func:`dated_windows`: the dated features, one file per start year.
6. :func:`precip_features_sql`: the join into notebook 05.
"""

import warnings
from math import lgamma, log
from pathlib import Path

import numpy as np
import pandas as pd

BASE_URL = "https://downloads.psl.noaa.gov/Datasets"
US_LAST_V1 = 2006            # US V1.0 ends here; the real-time (RT) files start in 2007
SOURCES = ("us", "global")   # order of preference for a location
EARTH_KM = 6371.0
HEAVY_MM = 50                # a "heavy rain day" for heavy_rain_days_3y


def cpc_url(source, year):
    """URL of one year's daily file.

    Examples
    --------
    >>> cpc_url("us", 2006)
    'https://downloads.psl.noaa.gov/Datasets/cpc_us_precip/precip.V1.0.2006.nc'
    >>> cpc_url("us", 2017)
    'https://downloads.psl.noaa.gov/Datasets/cpc_us_precip/RT/precip.V1.0.2017.nc'
    >>> cpc_url("global", 2017)
    'https://downloads.psl.noaa.gov/Datasets/cpc_global_precip/precip.2017.nc'
    """
    if source == "us":
        folder = "cpc_us_precip" if year <= US_LAST_V1 else "cpc_us_precip/RT"
        return f"{BASE_URL}/{folder}/precip.V1.0.{year}.nc"
    if source == "global":
        return f"{BASE_URL}/cpc_global_precip/precip.{year}.nc"
    raise ValueError(f"unknown source {source!r}")


def local_path(raw_dir, source, year):
    """Where :func:`download` saves a file: ``<raw_dir>/<source>/<file name>``."""
    return Path(raw_dir) / source / Path(cpc_url(source, year)).name


def download(raw_dir, source, year, session=None, timeout=300):
    """Download one year's file, unless it is already there.

    Parameters
    ----------
    raw_dir : pathlib.Path
        Usually ``data/raw/cpc_precip``.
    source : {"us", "global"}
    year : int
    session : requests.Session, optional
    timeout : int, default 300
        Seconds to wait for the server.

    Returns
    -------
    pathlib.Path

    Notes
    -----
    The file is written under a temporary name and renamed when complete,
    so a stopped download never looks finished. Files are kept unchanged,
    like our other raw data (about 9 MB per US year, 60 MB per global year).
    """
    import requests
    out = local_path(raw_dir, source, year)
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")
    get = (session or requests).get
    with get(cpc_url(source, year), stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    tmp.replace(out)
    return out


def read_grid(path):
    """Latitude and longitude of a file (no rainfall values read)."""
    import netCDF4
    with netCDF4.Dataset(path) as d:
        return np.asarray(d["lat"][:], dtype="float64"), np.asarray(d["lon"][:], dtype="float64")


def read_year(path):
    """All daily values of one file.

    Returns
    -------
    dates : numpy.ndarray of datetime64[D]
    lat, lon : numpy.ndarray
    pr : numpy.ndarray, shape (days, lat, lon), float32, NaN = missing

    Notes
    -----
    Checks that the unit is mm, that the days are consecutive, and that
    the file covers one calendar year from 1 January.
    """
    import netCDF4
    with netCDF4.Dataset(path) as d:
        lat = np.asarray(d["lat"][:], dtype="float64")
        lon = np.asarray(d["lon"][:], dtype="float64")
        t = d["time"]
        stamps = netCDF4.num2date(t[:], t.units, only_use_cftime_datetimes=False,
                                  only_use_python_datetimes=True)
        var = d["precip"]
        assert getattr(var, "units", "mm") == "mm", f"unexpected unit {var.units}"
        pr = np.ma.filled(var[:].astype("float32"), np.nan)
    dates = np.array([np.datetime64(s.date(), "D") for s in stamps])
    assert np.all(np.diff(dates) == np.timedelta64(1, "D")), f"{path}: days are not consecutive"
    year = dates[0].astype(object).year
    assert dates[0] == np.datetime64(f"{year}-01-01"), f"{path}: does not start on 1 January"
    assert dates[-1] == np.datetime64(f"{year}-12-31") or len(dates) < 365, f"{path}: does not end on 31 December"
    return dates, lat, lon, pr


def valid_mask(path, min_share=0.9):
    """Grid cells with gauge values on at least ``min_share`` of the days of one year.

    Returns
    -------
    lat, lon : numpy.ndarray
    mask : numpy.ndarray of bool, shape (lat, lon)
    """
    _, lat, lon, pr = read_year(path)
    return lat, lon, np.isfinite(pr).mean(axis=0) >= min_share


def _xyz(lat, lon):
    la, lo = np.radians(lat), np.radians(lon)
    return np.column_stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)])


def map_locations(locations, grids, us_max_km=25, global_max_km=60):
    """Give each policy location the nearest grid cell with gauge values.

    Parameters
    ----------
    locations : pandas.DataFrame
        ``latitude``, ``longitude`` (degrees, -180 to 180), one row per
        distinct location.
    grids : dict
        ``{"us": (lat, lon, mask), "global": (lat, lon, mask)}`` from
        :func:`valid_mask`.
    us_max_km, global_max_km : float
        Farthest a location may be from the centre of its cell. The US
        grid is used when the location is inside its area and a US cell is
        close enough; otherwise the global grid; otherwise no cell.

    Returns
    -------
    locations : pandas.DataFrame
        The input plus ``cell_id``, ``source``, ``grid_i``, ``grid_j``,
        ``dist_km`` (location to cell centre). ``cell_id`` is NaN when no
        cell is close enough.
    cells : pandas.DataFrame
        One row per used cell: ``cell_id``, ``source``, ``grid_i``,
        ``grid_j``, ``cell_lat``, ``cell_lon`` (-180 to 180),
        ``n_locations``.

    Notes
    -----
    Distances are great-circle distances (a 3-D nearest-neighbour search),
    so the date line and the poles need no special handling. A coastal
    location whose own cell is sea gets the nearest land cell.
    """
    from scipy.spatial import cKDTree
    loc = locations.reset_index(drop=True).copy()                # other columns are kept as they are
    loc[["latitude", "longitude"]] = loc[["latitude", "longitude"]].astype("float64")
    lat, lon360 = loc["latitude"].to_numpy(), loc["longitude"].to_numpy() % 360
    q = _xyz(lat, lon360)
    best = {}
    for source in SOURCES:
        glat, glon, mask = grids[source]
        ii, jj = np.nonzero(mask)
        chord, k = cKDTree(_xyz(glat[ii], glon[jj])).query(q)
        km = 2 * EARTH_KM * np.arcsin(np.clip(chord / 2, 0, 1))
        half = (abs(glat[1] - glat[0]) / 2, abs(glon[1] - glon[0]) / 2)
        inside = ((lat >= glat.min() - half[0]) & (lat <= glat.max() + half[0])
                  & (lon360 >= glon.min() - half[1]) & (lon360 <= glon.max() + half[1]))
        best[source] = (ii[k], jj[k], km, inside)

    out = loc.copy()
    out["source"], out["grid_i"], out["grid_j"], out["dist_km"] = None, -1, -1, np.nan
    limits = {"us": us_max_km, "global": global_max_km}
    for source in SOURCES:                                   # US first, global only where US is not used
        i, j, km, inside = best[source]
        use = out["source"].isna().to_numpy() & inside & (km <= limits[source])
        out.loc[use, "source"] = source
        out.loc[use, "grid_i"], out.loc[use, "grid_j"] = i[use], j[use]
        out.loc[use, "dist_km"] = km[use].round(1)

    used = out.dropna(subset=["source"])
    cells = (used.groupby(["source", "grid_i", "grid_j"]).size().rename("n_locations").reset_index()
             .sort_values(["source", "grid_i", "grid_j"]).reset_index(drop=True))
    cells.insert(0, "cell_id", np.arange(len(cells), dtype="int32"))
    centres = []
    for _, c in cells.iterrows():
        glat, glon, _ = grids[c["source"]]
        centres.append((glat[c["grid_i"]], ((glon[c["grid_j"]] + 180) % 360) - 180))
    cells["cell_lat"], cells["cell_lon"] = [c[0] for c in centres], [c[1] for c in centres]
    out = out.merge(cells[["cell_id", "source", "grid_i", "grid_j"]], on=["source", "grid_i", "grid_j"], how="left")
    return out, cells


def gather_year(paths, cells):
    """One year's daily values at our cells only.

    Parameters
    ----------
    paths : dict
        ``{"us": path, "global": path}`` for the same year.
    cells : pandas.DataFrame
        From :func:`map_locations`.

    Returns
    -------
    dates : numpy.ndarray of datetime64[D]
    x : numpy.ndarray, shape (days, cells), float32, NaN = missing;
        column ``k`` is ``cell_id == k``.
    """
    x, dates = None, None
    for source in SOURCES:
        sel = cells[cells["source"] == source]
        if sel.empty:
            continue
        d, _, _, pr = read_year(paths[source])
        if dates is None:
            dates = d
            x = np.full((len(d), len(cells)), np.nan, dtype="float32")
        assert np.array_equal(d, dates), "the US and global files cover different days"
        x[:, sel["cell_id"].to_numpy()] = pr[:, sel["grid_i"].to_numpy(), sel["grid_j"].to_numpy()]
        del pr
    return dates, x


def annual_from_matrix(dates, x, min_share=0.95):
    """Annual total and wettest day per cell for one year.

    A cell with gauge values on fewer than ``min_share`` of the days gets
    NaN for that year. Small gaps are filled pro rata in the total (total
    x days / days with values).

    Returns
    -------
    pandas.DataFrame
        ``cell_id, year, annual_mm, max1d_mm, share_days``.
    """
    present = np.isfinite(x)
    n = present.sum(axis=0)
    share = n / x.shape[0]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)       # all-NaN cells -> NaN, no warning
        total = np.nansum(x, axis=0) * x.shape[0] / np.where(n > 0, n, 1)
        wettest = np.nanmax(x, axis=0)
    ok = share >= min_share
    return pd.DataFrame({
        "cell_id": np.arange(x.shape[1], dtype="int32"),
        "year": int(dates[0].astype(object).year),
        "annual_mm": np.where(ok, total, np.nan),
        "max1d_mm": np.where(ok, wettest, np.nan),
        "share_days": share.round(3),
    })


def gev_lmoments(x):
    """Fit a GEV to each column of annual maxima with L-moments (Hosking, 1985).

    Parameters
    ----------
    x : numpy.ndarray
        Shape (years, cells); NaN values are ignored per column.

    Returns
    -------
    tuple of numpy.ndarray
        ``(loc, scale, shape)`` per column, with ``shape`` in the usual
        sign convention (positive = heavy upper tail, as in R's
        ``extRemes``; scipy's ``c`` is ``-shape``).

    Notes
    -----
    L-moments are more stable than maximum likelihood on short series
    (about 30 annual maxima here) and have a closed form, so all cells
    are fitted at once. Hosking's approximation for the shape is
    accurate for ``-0.5 < shape < 0.5``, which covers rainfall.
    """
    x = np.sort(np.asarray(x, dtype="float64"), axis=0)        # NaN sorts last
    n = np.isfinite(x).sum(axis=0).astype("float64")
    xs = np.where(np.isfinite(x), x, 0.0)
    r = np.arange(x.shape[0], dtype="float64")[:, None]        # rank among the finite values
    with np.errstate(divide="ignore", invalid="ignore"):
        w1 = np.where(n > 1, r / (n - 1), 0.0)
        w2 = np.where(n > 2, r * (r - 1) / ((n - 1) * (n - 2)), 0.0)
        b0 = xs.sum(axis=0) / n
        b1 = (w1 * xs).sum(axis=0) / n
        b2 = (w2 * xs).sum(axis=0) / n
        l1, l2, l3 = b0, 2 * b1 - b0, 6 * b2 - 6 * b1 + b0
        t3 = np.where(l2 > 0, l3 / l2, np.nan)                 # a constant column gives NaN
        z = 2 / (3 + t3) - log(2) / log(3)
        k = 7.8590 * z + 2.9554 * z ** 2                       # Hosking's k = -shape
        gk = np.exp(np.vectorize(lgamma)(1 + np.nan_to_num(k)))
        near0 = np.abs(k) < 1e-6
        k_safe = np.where(near0, 1.0, k)
        scale = np.where(near0, l2 / log(2), l2 * k_safe / ((1 - 2 ** (-k_safe)) * gk))
        loc = np.where(near0, l1 - 0.5772156649 * scale, l1 - scale * (1 - gk) / k_safe)
    return loc, scale, -k


def gev_quantile(loc, scale, shape, return_period):
    """Rainfall with the given return period (years) from GEV parameters."""
    y = -np.log(1 - 1 / return_period)                         # -ln(F)
    near0 = np.abs(shape) < 1e-6
    s = np.where(near0, 1.0, shape)
    return np.where(near0, loc - scale * np.log(y), loc + scale * (y ** (-s) - 1) / s)


def climate_summary(annual, return_period=100, min_years=20):
    """Static rainfall features per cell from the annual values.

    Parameters
    ----------
    annual : pandas.DataFrame
        Stacked :func:`annual_from_matrix` results for the climate years.
    return_period : int, default 100
    min_years : int, default 20
        Fewer complete years than this gives NULL features.

    Returns
    -------
    pandas.DataFrame
        ``cell_id, n_years, first_year, last_year,
        precip_annual_mean_mm, precip_max1d_mean_mm, precip_100yr_1d_mm``
        and the GEV parameters ``gev_loc, gev_scale, gev_shape``.
    """
    a = annual.dropna(subset=["annual_mm", "max1d_mm"])
    out = (a.groupby("cell_id")
           .agg(n_years=("year", "nunique"), first_year=("year", "min"), last_year=("year", "max"),
                precip_annual_mean_mm=("annual_mm", "mean"), precip_max1d_mean_mm=("max1d_mm", "mean"))
           .reset_index())
    wide = a.pivot_table(index="year", columns="cell_id", values="max1d_mm")
    loc, scale, shape = gev_lmoments(wide.to_numpy())
    gev = pd.DataFrame({"cell_id": wide.columns.to_numpy(), "gev_loc": loc, "gev_scale": scale, "gev_shape": shape})
    gev[f"precip_{return_period}yr_1d_mm"] = gev_quantile(loc, scale, shape, return_period)
    out = out.merge(gev, on="cell_id", how="left")
    few = out["n_years"] < min_years
    out.loc[few, [c for c in out.columns if c.startswith(("precip_", "gev_"))]] = np.nan
    return out.round(3)


def dated_windows(dates, x, annual_mean, start_from, start_to, max_missing=0.1):
    """Rainfall in windows that end the day before each start date, per cell.

    Parameters
    ----------
    dates : numpy.ndarray of datetime64[D]
        Consecutive days; must begin at least 3 years before ``start_from``.
    x : numpy.ndarray, shape (days, cells)
        Daily rainfall (mm), NaN = missing; column ``k`` is ``cell_id == k``.
    annual_mean : numpy.ndarray, shape (cells,)
        ``precip_annual_mean_mm`` per cell, for the % of normal.
    start_from, start_to : str or numpy.datetime64
        First and last start date to compute.
    max_missing : float, default 0.1
        A window with more missing days than this share gives NaN.

    Yields
    ------
    pandas.DataFrame
        One per calendar year of start dates: ``cell_id, period_start,
        precip_last_day, precip_90d_mm, precip_365d_mm,
        precip_365d_pct_normal, precip_max1d_365d_mm,
        precip_max3d_365d_mm, heavy_rain_days_3y``.

    Notes
    -----
    For a start on day ``d`` the windows cover days ``d-W .. d-1`` (never
    day ``d`` or later). Totals are scaled up pro rata for missing days;
    maxima and counts use the days with values. The 3-day totals count
    only 3-day spans that lie inside the 365-day window.
    """
    start_from, start_to = np.datetime64(start_from, "D"), np.datetime64(start_to, "D")
    w90, w365, w3y = 90, 365, 1095
    assert dates[0] <= start_from - np.timedelta64(w3y, "D"), "daily data must begin 3 years before start_from"
    assert dates[-1] >= start_to - np.timedelta64(1, "D"), "daily data ends before the last window"

    miss = ~np.isfinite(x)
    xz = np.where(miss, 0.0, x)
    zero = np.zeros((1, x.shape[1]))
    C = np.vstack([zero, np.cumsum(xz, axis=0, dtype="float64")])            # C[t] = sum of days < t
    M = np.vstack([zero, np.cumsum(miss, axis=0)]).astype("int32")
    H = np.vstack([zero, np.cumsum(xz >= HEAVY_MM, axis=0)]).astype("int32")
    s3 = np.full(x.shape, np.nan, dtype="float32")
    s3[2:] = (C[3:] - C[:-3]).astype("float32")                               # 3-day total ending on day t
    s3[2:][(M[3:] - M[:-3]) > 0] = np.nan                                     # only full 3-day spans
    r1 = pd.DataFrame(np.where(miss, np.nan, x)).rolling(w365, min_periods=1).max().to_numpy("float32")
    r3 = pd.DataFrame(s3).rolling(w365 - 2, min_periods=1).max().to_numpy("float32")
    del xz, s3

    idx = np.nonzero((dates >= start_from) & (dates <= start_to))[0]
    years = dates[idx].astype("datetime64[Y]")
    n_cells = x.shape[1]
    cell_id = np.arange(n_cells, dtype="int32")
    for y in np.unique(years):
        d = idx[years == y]                                                   # day index of each start
        def window(A, w):
            return A[d] - A[d - w]                                            # days d-w .. d-1
        tot90, m90 = window(C, w90), window(M, w90)
        tot365, m365 = window(C, w365), window(M, w365)
        heavy, m3y = window(H, w3y), window(M, w3y)
        with np.errstate(divide="ignore", invalid="ignore"):
            p90 = np.where(m90 / w90 <= max_missing, tot90 * w90 / (w90 - m90), np.nan)
            p365 = np.where(m365 / w365 <= max_missing, tot365 * w365 / (w365 - m365), np.nan)
            pct = 100 * p365 / annual_mean[None, :]
        ok365 = m365 / w365 <= max_missing
        frame = pd.DataFrame({
            "cell_id": np.tile(cell_id, len(d)),
            "period_start": np.repeat(dates[d], n_cells),
            "precip_last_day": np.repeat(dates[d - 1], n_cells),
            "precip_90d_mm": p90.ravel().astype("float32"),
            "precip_365d_mm": p365.ravel().astype("float32"),
            "precip_365d_pct_normal": pct.ravel().astype("float32"),
            "precip_max1d_365d_mm": np.where(ok365, r1[d - 1], np.nan).ravel().astype("float32"),
            "precip_max3d_365d_mm": np.where(ok365, r3[d - 1], np.nan).ravel().astype("float32"),
            "heavy_rain_days_3y": np.where(m3y / w3y <= max_missing, heavy, np.nan).ravel().astype("float32"),
        })
        yield int(y.astype(object).year), frame


def save_daily(frame, path):
    """Save one :func:`dated_windows` frame as Parquet, with the two dates as DATE (not timestamps).

    The file is written under a temporary name and renamed when complete.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq
    table = pa.Table.from_pandas(frame, preserve_index=False)
    for name in ("period_start", "precip_last_day"):
        table = table.set_column(table.schema.get_field_index(name), name, table[name].cast(pa.date32()))
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(path)


def precip_features_sql(base, locations, climate, daily):
    """SQL: static and dated rainfall features for each policy-year.

    Parameters
    ----------
    base : str
        View from :func:`src.features.base_features_sql` (needs
        ``latitude``, ``longitude`` and ``period_start``).
    locations : str
        Location to cell map (``precip_locations.parquet``).
    climate : str
        Static features per cell (``precip_climate.parquet``).
    daily : str
        Dated features per cell and start date (``precip_daily/*.parquet``).

    Returns
    -------
    str
        A ``SELECT`` with ``period_id``, the eight rainfall features and
        ``precip_last_day`` (for the leakage check, not a feature).

    Notes
    -----
    A policy without coordinates, or without a cell within reach, gets
    NULL. The dated values were computed for every day, so each start date
    finds its own row.
    """
    return f"""
SELECT b.period_id,
       c.precip_annual_mean_mm, c.precip_max1d_mean_mm, c.precip_100yr_1d_mm,
       d.precip_90d_mm, d.precip_365d_pct_normal, d.precip_max1d_365d_mm, d.precip_max3d_365d_mm,
       d.heavy_rain_days_3y, d.precip_last_day
FROM {base} b
LEFT JOIN {locations} l
       ON l.latitude = CAST(b.latitude AS DOUBLE) AND l.longitude = CAST(b.longitude AS DOUBLE)
LEFT JOIN {climate} c ON c.cell_id = l.cell_id
LEFT JOIN {daily} d ON d.cell_id = l.cell_id AND d.period_start = b.period_start
"""
