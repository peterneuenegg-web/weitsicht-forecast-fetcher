#!/usr/bin/env python3
"""
Weitsicht Forecast Fetcher — ICON-CH2-EPS → forecast-ingest.

Holt die für den Fernsicht-Score nötigen ICON-CH2-Parameter über die offizielle
MeteoSchweiz-Bibliothek `meteodata-lab` (ogd_api) aus der STAC-Collection
`ch.meteoschweiz.ogd-forecasting-icon-ch2`, liest die Werte an den ~220
Referenzpunkten (points.json) via Nearest-Cell aus und POSTet ein kompaktes
JSON an den Weitsicht-Ingest-Endpoint (api/forecast-ingest.php).

Warum externer Worker: Infomaniak Shared Hosting hat kein eccodes und exec()
ist deaktiviert → GRIB2 kann dort nicht dekodiert werden. Muster wie
wetteralarm-kenda-fetcher / -hail-fetcher.

Env (via GitHub Secrets):
  FORECAST_INGEST_URL   z.B. https://weitsicht.wetteralarm.ch/api/forecast-ingest.php
  FORECAST_INGEST_TOKEN muss FORECAST_INGEST_TOKEN in der Infomaniak .env matchen
  HEARTBEAT_URL         optional, z.B. .../api/external-heartbeat.php?job=ingest_forecast
  HORIZON_HOURS         optional, default 72
  STEP_HOURS            optional, default 1
  REFERENCE_DATETIME    optional, ISO (Backfill/Test); sonst automatisch neuester Lauf

Exit-Codes: 0 ok / nichts Neues · 1 Fehler · 2 Config fehlt

HINWEIS: Die exakten xarray-Dim-/Koordinaten-Namen von meteodata-lab werden beim
ersten CI-Lauf aus dem Struktur-Log verifiziert (log_dataarray_shape). Die
Extraktion ist defensiv (find_lonlat / find_cell_dim / find_lead_coord).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("weitsicht-forecast")

# ── Parameter (DWD-Namen wie meteodata-lab erwartet) ─────────────────────────
# Zeitabhängige Vorhersageparameter je Lead-Time.
VARIABLES = [
    "t_2m", "td_2m", "tot_prec",
    "u_10m", "v_10m", "vmax_10m",
    "ceiling", "clcl", "clct", "hzerocl",
]
# Konstante Felder — nur Lead 0 (ändern sich nicht über die Vorhersage).
CONST_VARIABLES = ["hsurf"]

BAND_LOW_MAX = 900   # < 900 m → low
BAND_MID_MAX = 1500  # 900..<1500 → mid ; ≥1500 → high

HORIZON_HOURS = int(os.environ.get("HORIZON_HOURS", "72"))
STEP_HOURS = int(os.environ.get("STEP_HOURS", "1"))

HERE = Path(__file__).resolve().parent


def require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        log.error("Missing required env var: %s", name)
        sys.exit(2)
    return v


def load_points() -> list[dict]:
    data = json.loads((HERE / "points.json").read_text(encoding="utf-8"))
    pts = data.get("points", [])
    if not pts:
        log.error("points.json enthält keine Punkte")
        sys.exit(2)
    log.info("Loaded %d points (%s)", len(pts), data.get("stats", {}))
    return pts


# ── meteodata-lab Import (spät, damit require_env-Fehler ohne die schwere Lib greifen)
def _ogd():
    from meteodatalab.ogd_api import Request, Collection, get_from_ogd
    return Request, Collection, get_from_ogd


# ── xarray-Struktur defensiv erschliessen ────────────────────────────────────
def log_dataarray_shape(da, var: str) -> None:
    """Beim ersten Lauf: Dims/Coords ins Log, um die Annahmen zu verifizieren."""
    log.info("DataArray[%s]: dims=%s shape=%s coords=%s",
             var, getattr(da, "dims", "?"), getattr(da, "shape", "?"),
             list(getattr(da, "coords", {}).keys()))


def find_lonlat(da):
    """Liefert (lon_array, lat_array) der Mesh-Zellen als 1D float64."""
    coords = da.coords
    lon = lat = None
    for cand in ("longitude", "lon", "clon"):
        if cand in coords:
            lon = np.asarray(coords[cand].values, dtype=np.float64).ravel(); break
    for cand in ("latitude", "lat", "clat"):
        if cand in coords:
            lat = np.asarray(coords[cand].values, dtype=np.float64).ravel(); break
    if lon is None or lat is None:
        raise RuntimeError(f"lon/lat coords not found; coords={list(coords.keys())}")
    # ICON kann Radiant liefern — Plausibilitäts-Check
    if np.nanmax(np.abs(lat)) <= np.pi + 0.01:
        lon = np.degrees(lon); lat = np.degrees(lat)
    return lon, lat


def find_cell_dim(da, ncells: int) -> str:
    """Name der Zell-Dimension = die Dim, deren Grösse == Anzahl Mesh-Zellen."""
    for d in da.dims:
        if da.sizes[d] == ncells:
            return d
    raise RuntimeError(f"cell dim (size {ncells}) not found in dims={da.dims}")


def find_lead_values(da):
    """Liefert (lead_dim_name, list[timedelta]) — die Lead-Times als Offsets."""
    for cand in ("lead_time", "step", "horizon"):
        if cand in da.coords:
            vals = da.coords[cand].values
            leads = []
            for v in np.atleast_1d(vals):
                td = _to_timedelta(v)
                leads.append(td)
            return cand, leads
    # Fallback: valid_time - reference_time
    if "valid_time" in da.coords and "ref_time" in da.coords:
        vt = np.atleast_1d(da.coords["valid_time"].values)
        rt = np.atleast_1d(da.coords["ref_time"].values)[0]
        leads = [ _to_timedelta(v - rt) for v in vt ]
        return "valid_time", leads
    raise RuntimeError(f"lead-time coord not found; coords={list(da.coords.keys())}")


def _to_timedelta(v) -> timedelta:
    if isinstance(v, np.timedelta64):
        return timedelta(seconds=float(v / np.timedelta64(1, "s")))
    if isinstance(v, (int, float, np.integer, np.floating)):
        return timedelta(seconds=float(v))
    # numpy datetime diff etc.
    try:
        return timedelta(seconds=float(np.asarray(v).astype("timedelta64[s]").astype(float)))
    except Exception:
        return timedelta(0)


def extract_series(da, cell_dim: str, cell_indices: np.ndarray) -> np.ndarray:
    """
    Reduziert das DataArray auf 2D (lead, point). Nicht-Zell-/Nicht-Lead-Dims
    (z.B. eps mit Länge 1) werden weggesqueezt.
    """
    lead_dim, _ = find_lead_values(da)
    # alle anderen Dims (ausser lead_dim, cell_dim) auf Index 0 reduzieren
    sel = {d: 0 for d in da.dims if d not in (lead_dim, cell_dim)}
    arr = da.isel(**sel) if sel else da
    arr = arr.transpose(lead_dim, cell_dim)
    values = np.asarray(arr.values, dtype=np.float64)  # (nlead, ncells)
    return values[:, cell_indices]                     # (nlead, npoints)


# ── Fetch je Variable über alle Lead-Times ───────────────────────────────────
def fetch_variable(var: str, ref_iso: str, horizons: list[timedelta], first: bool):
    Request, Collection, get_from_ogd = _ogd()
    req = Request(
        collection=Collection.ICON_CH2,
        variable=var,
        reference_datetime=ref_iso,
        perturbed=False,
        horizon=horizons if len(horizons) > 1 else horizons[0],
    )
    da = get_from_ogd(req)
    if first:
        log_dataarray_shape(da, var)
    return da


# STAC-Collection der ICON-CH2-Forecasts. Wir lesen daraus den tatsächlich
# verfügbaren neuesten Lauf, statt reference_datetime zu raten (die Collection
# stellt i.d.R. nur den neuesten Lauf bereit → Raten scheitert immer).
STAC_ITEMS_URL = (
    "https://data.geo.admin.ch/api/stac/v1/collections/"
    "ch.meteoschweiz.ogd-forecasting-icon-ch2/items?limit=500"
)


def detect_latest_run() -> str | None:
    """
    Neuesten verfügbaren ICON-CH2-Lauf bestimmen. Liest die tatsächlichen
    `forecast:reference_datetime`-Werte aus der STAC-Collection und nimmt den
    grössten (ISO-8601 sortiert lexikografisch = chronologisch). Der zurück-
    gegebene Wert wird 1:1 an meteodata-lab weitergereicht.
    """
    override = os.environ.get("REFERENCE_DATETIME", "").strip()
    if override:
        log.info("Using REFERENCE_DATETIME override: %s", override)
        return override
    try:
        r = requests.get(STAC_ITEMS_URL, timeout=30)
        r.raise_for_status()
        refs = set()
        for f in r.json().get("features", []):
            rd = (f.get("properties") or {}).get("forecast:reference_datetime")
            if rd:
                refs.add(rd.replace("+00:00", "Z"))
        if not refs:
            log.error("STAC lieferte keine reference_datetime")
            return None
        latest = max(refs)
        log.info("Neuester ICON-CH2-Lauf (aus STAC): %s (von %d verschiedenen)", latest, len(refs))
        return latest
    except Exception as e:
        log.error("STAC-Lauf-Erkennung fehlgeschlagen: %s", e)
        return None


# ── Payload bauen ─────────────────────────────────────────────────────────────
def build_payload(points, ref_iso, series_by_var, hsurf_by_point, leads):
    ref_dt = datetime.strptime(ref_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    valid_times = [(ref_dt + td).strftime("%Y-%m-%dT%H:%M:%SZ") for td in leads]

    def band_from_alt(alt):
        if alt is None:
            return None
        return "low" if alt < BAND_LOW_MAX else ("mid" if alt < BAND_MID_MAX else "high")

    out_points = []
    for i, pt in enumerate(points):
        hsurf = hsurf_by_point[i]
        band = pt.get("band") or band_from_alt(hsurf)
        if band is None:
            continue  # kann nicht zugeordnet werden
        series = []
        for k, vt in enumerate(valid_times):
            t2m = _c(series_by_var["t_2m"][k, i], -273.15) if "t_2m" in series_by_var else None
            td2m = _c(series_by_var["td_2m"][k, i], -273.15) if "td_2m" in series_by_var else None
            # Niederschlag: akkumuliert (mm) + Rate aus Differenz zur Vorstunde
            precip_mm = _v(series_by_var["tot_prec"][k, i]) if "tot_prec" in series_by_var else None
            rate = None
            if precip_mm is not None and "tot_prec" in series_by_var:
                prev = _v(series_by_var["tot_prec"][k - 1, i]) if k > 0 else 0.0
                rate = max(0.0, (precip_mm - (prev or 0.0))) / max(1, STEP_HOURS)
            u = _v(series_by_var["u_10m"][k, i]) if "u_10m" in series_by_var else None
            v = _v(series_by_var["v_10m"][k, i]) if "v_10m" in series_by_var else None
            wind_kmh = wind_dir = None
            if u is not None and v is not None:
                wind_kmh = round((u * u + v * v) ** 0.5 * 3.6, 1)
                deg = (np.degrees(np.arctan2(-u, -v)) + 360.0) % 360.0
                wind_dir = int(round(deg))
            gust = series_by_var.get("vmax_10m")
            gust_kmh = round(_v(gust[k, i]) * 3.6, 1) if gust is not None and _v(gust[k, i]) is not None else None
            series.append({
                "valid_time": vt,
                "t2m": t2m, "td2m": td2m,
                "precip_mm": _round(precip_mm, 2),
                "precip_rate_mm_h": _round(rate, 2),
                "wind_kmh": wind_kmh, "wind_dir": wind_dir, "gust_kmh": gust_kmh,
                "ceiling_m": _int(series_by_var.get("ceiling"), k, i),
                "clcl": _round(_get(series_by_var.get("clcl"), k, i), 1),
                "clct": _round(_get(series_by_var.get("clct"), k, i), 1),
                "hzerocl_m": _int(series_by_var.get("hzerocl"), k, i),
            })
        out_points.append({
            "point_id": pt["point_id"], "region_id": pt["region_id"], "band": band,
            "hsurf_m": int(hsurf) if hsurf is not None else None,
            "series": series,
        })
    return {"source": "iconch2", "model_run": ref_iso, "points": out_points}


# kleine Wert-Helfer (NaN → None)
def _v(x):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else float(x)
def _c(x, off):
    v = _v(x); return round(v + off, 1) if v is not None else None
def _round(x, n):
    return round(x, n) if x is not None else None
def _int(arr, k, i):
    if arr is None: return None
    v = _v(arr[k, i]); return int(round(v)) if v is not None else None
def _get(arr, k, i):
    return None if arr is None else _v(arr[k, i])


# ── POST mit Retry (Muster aus kenda-fetcher) ────────────────────────────────
def post_payload(url: str, token: str, payload: dict) -> bool:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Ingest-Token": token}
    last = ""
    for attempt in range(1, 4):
        try:
            r = requests.post(url, data=body, headers=headers, timeout=120)
            if r.status_code in (200, 201):
                log.info("Ingest ok: %s", r.text[:200])
                return True
            if 400 <= r.status_code < 500:
                log.error("Ingest rejected %s: %s", r.status_code, r.text[:200])
                return False
            last = f"HTTP {r.status_code}: {r.text[:120]}"
        except (requests.ConnectionError, requests.Timeout) as e:
            last = str(e)[:120]
        if attempt < 3:
            time.sleep(2 ** attempt)
    log.error("Ingest failed after retries: %s", last)
    return False


def heartbeat(status: str):
    url = os.environ.get("HEARTBEAT_URL", "").strip()
    token = os.environ.get("FORECAST_INGEST_TOKEN", "").strip()
    if not url:
        return
    try:
        requests.get(url, params={"status": status}, headers={"X-Ingest-Token": token}, timeout=15)
    except Exception:
        pass


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    ingest_url = require_env("FORECAST_INGEST_URL")
    ingest_token = require_env("FORECAST_INGEST_TOKEN")
    points = load_points()

    ref_iso = detect_latest_run()
    if not ref_iso:
        log.error("Kein verfügbarer ICON-CH2-Lauf gefunden")
        heartbeat("failed")
        return 1

    horizons = [timedelta(hours=h) for h in range(0, HORIZON_HOURS + 1, STEP_HOURS)]
    log.info("Fetching %d variables × %d lead-times for run %s",
             len(VARIABLES), len(horizons), ref_iso)

    # 1) Mesh + KDTree aus der ersten Variable
    from scipy.spatial import cKDTree
    series_by_var: dict[str, np.ndarray] = {}
    cell_indices = None
    ncells = None
    leads = None
    first = True
    try:
        for var in VARIABLES:
            da = fetch_variable(var, ref_iso, horizons, first)
            if cell_indices is None:
                lon, lat = find_lonlat(da)
                ncells = lon.size
                tree = cKDTree(np.column_stack([lon, lat]))
                pl = np.array([[p["lon"], p["lat"]] for p in points], dtype=np.float64)
                _, cell_indices = tree.query(pl, k=1)
                log.info("KDTree: %d mesh cells, matched %d points", ncells, len(points))
                _, leads = find_lead_values(da)
            cdim = find_cell_dim(da, ncells)
            series_by_var[var] = extract_series(da, cdim, cell_indices)
            first = False
            log.info("  %s: series %s", var, series_by_var[var].shape)

        # 2) Konstantes HSURF (Lead 0)
        hsurf_by_point = [None] * len(points)
        for var in CONST_VARIABLES:
            da = fetch_variable(var, ref_iso, [timedelta(0)], False)
            cdim = find_cell_dim(da, ncells)
            vals = extract_series(da, cdim, cell_indices)  # (1, npoints)
            if var == "hsurf":
                hsurf_by_point = [None if np.isnan(vals[0, i]) else float(vals[0, i]) for i in range(len(points))]
    except Exception as e:
        log.exception("Fetch/extract failed: %s", e)
        heartbeat("failed")
        return 1

    payload = build_payload(points, ref_iso, series_by_var, hsurf_by_point, leads)
    log.info("Payload: %d points, %d lead-times", len(payload["points"]), len(leads))

    ok = post_payload(ingest_url, ingest_token, payload)
    heartbeat("ok" if ok else "failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
