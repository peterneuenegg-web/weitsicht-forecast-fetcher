#!/usr/bin/env python3
"""
Weitsicht CAMS Fetcher — Aerosol (AOD550) → cams-ingest.

Holt aus dem CAMS Atmosphere Data Store (ADS) die AOD550-Vorhersage über die
Schweiz, interpoliert bilinear auf die ~220 Referenzpunkte (points.json) und
POSTet an api/cams-ingest.php. Läuft 2×/Tag (nach dem 00/12-UTC-CAMS-Lauf).

Datensatz: cams-global-atmospheric-composition-forecasts
Variablen: total_aerosol_optical_depth_550nm, dust_aerosol_optical_depth_550nm
Auflösung ~0.4°, Vorhersage bis +120 h.

Env (via GitHub Secrets):
  CAMS_INGEST_URL     z.B. https://weitsicht.wetteralarm.ch/api/cams-ingest.php
  CAMS_INGEST_TOKEN   muss CAMS_INGEST_TOKEN in der Infomaniak .env matchen
  ADS_URL             default https://ads.atmosphere.copernicus.eu/api
  ADS_API_KEY         ADS-Personal-Access-Token (kostenlose Registrierung)
  HEARTBEAT_URL       optional, .../api/external-heartbeat.php?job=ingest_cams
  HORIZON_HOURS       optional, default 72
  CAMS_STEP_HOURS     optional, default 6  (AOD ändert sich langsam)

Exit-Codes: 0 ok · 1 Fehler · 2 Config fehlt

HINWEIS: ohne ADS-Key nicht lokal testbar. Die genaue NetCDF-Variablenbenennung
(z.B. 'aod550'/'duaod550') wird beim ersten CI-Lauf aus dem Struktur-Log
verifiziert (log_ds_vars).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("weitsicht-cams")

HERE = Path(__file__).resolve().parent
CH_AREA = [48.0, 5.5, 45.5, 11.0]      # N, W, S, E (SPEC 3.2, erweiterte CH-Bbox)
HORIZON_HOURS = int(os.environ.get("HORIZON_HOURS", "72"))
CAMS_STEP_HOURS = int(os.environ.get("CAMS_STEP_HOURS", "6"))

# ADS-Variablenname → NetCDF-Kurzname (Kandidaten, beim 1. Lauf verifizieren)
VAR_AOD = ("total_aerosol_optical_depth_550nm", ["aod550", "od550aer"])
VAR_DUST = ("dust_aerosol_optical_depth_550nm", ["duaod550", "od550dust"])


def require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        log.error("Missing required env var: %s", name); sys.exit(2)
    return v


def load_points() -> list[dict]:
    data = json.loads((HERE / "points.json").read_text(encoding="utf-8"))
    return data.get("points", [])


def latest_cams_run() -> tuple[str, str]:
    """Neuesten 00/12-UTC-Lauf wählen (mit ~8 h Publikationsversatz, SPEC 3.2)."""
    override = os.environ.get("REFERENCE_DATETIME", "").strip()
    if override:
        dt = datetime.strptime(override, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    else:
        now = datetime.now(timezone.utc) - timedelta(hours=8)  # Publikationsversatz
        hour = 12 if now.hour >= 12 else 0
        dt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return dt.strftime("%Y-%m-%d"), f"{dt.hour:02d}:00"


def download_cams(date_str: str, run_time: str, dest: Path) -> bool:
    import cdsapi
    # Leeres ADS_URL (Secret nicht gesetzt → "" statt fehlend) auf Default fallen
    # lassen — sonst baut cdsapi eine URL ohne Schema ("https:///…").
    ads_url = os.environ.get("ADS_URL", "").strip() or "https://ads.atmosphere.copernicus.eu/api"
    client = cdsapi.Client(url=ads_url, key=require_env("ADS_API_KEY"))
    leadtimes = [str(h) for h in range(0, HORIZON_HOURS + 1, CAMS_STEP_HOURS)]
    request = {
        "variable": [VAR_AOD[0], VAR_DUST[0]],
        "date": f"{date_str}/{date_str}",
        "time": run_time,
        "leadtime_hour": leadtimes,
        "type": "forecast",
        "area": CH_AREA,
        # Neue Copernicus-API (CADS/ADS) erwartet data_format; "format" ist
        # veraltet und kann einen 400er auslösen.
        "data_format": "netcdf",
    }
    log.info("CAMS request: %s %s leadtimes=%s", date_str, run_time, leadtimes)
    client.retrieve("cams-global-atmospheric-composition-forecasts", request, str(dest))
    return dest.exists() and dest.stat().st_size > 0


def log_ds_vars(ds):
    log.info("NetCDF: data_vars=%s dims=%s coords=%s",
             list(ds.data_vars), dict(ds.sizes), list(ds.coords))


def pick_var(ds, candidates):
    for c in candidates:
        if c in ds.data_vars:
            return c
    # Fallback: erste passende Variable, deren Name 'aod'/'550' enthält
    for name in ds.data_vars:
        low = name.lower()
        if "550" in low or "aod" in low:
            return name
    return None


def build_payload(ds, points, ref_iso):
    import xarray as xr  # noqa: F401 (ds ist bereits xarray)
    aod_name = pick_var(ds, VAR_AOD[1])
    dust_name = pick_var(ds, VAR_DUST[1])
    if aod_name is None:
        raise RuntimeError(f"AOD-Variable nicht gefunden in {list(ds.data_vars)}")

    # Koordinaten (lat aufsteigend/absteigend, lon)
    lat_name = "latitude" if "latitude" in ds.coords else "lat"
    lon_name = "longitude" if "longitude" in ds.coords else "lon"
    # Zeitachse: forecast_period/step oder time
    time_name = None
    for c in ("forecast_period", "step", "time", "valid_time"):
        if c in ds.coords or c in ds.dims:
            time_name = c; break
    if time_name is None:
        raise RuntimeError(f"Zeitachse nicht gefunden; coords={list(ds.coords)}")

    ref_dt = datetime.strptime(ref_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    steps = np.atleast_1d(ds[time_name].values)

    def to_hours(v):
        if isinstance(v, np.timedelta64):
            return float(v / np.timedelta64(1, "h"))
        try:
            return float(np.asarray(v).astype("timedelta64[h]").astype(float))
        except Exception:
            return 0.0

    out_points = []
    lons = np.array([p["lon"] for p in points])
    lats = np.array([p["lat"] for p in points])

    # bilineare Interpolation via xarray .interp über alle Punkte + Steps
    da_aod = ds[aod_name]
    da_dust = ds[dust_name] if dust_name else None
    for i, p in enumerate(points):
        sel_aod = da_aod.interp({lat_name: lats[i], lon_name: lons[i]}, method="linear")
        sel_dust = da_dust.interp({lat_name: lats[i], lon_name: lons[i]}, method="linear") if da_dust is not None else None
        series = []
        for k in range(len(steps)):
            vt = ref_dt + timedelta(hours=to_hours(steps[k]))
            aod = float(np.asarray(sel_aod.values).ravel()[k])
            dust = float(np.asarray(sel_dust.values).ravel()[k]) if sel_dust is not None else None
            series.append({
                "valid_time": vt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "aod550": None if np.isnan(aod) else round(aod, 3),
                "dust_aod": None if (dust is None or np.isnan(dust)) else round(dust, 3),
            })
        out_points.append({
            "point_id": p["point_id"], "region_id": p["region_id"],
            "band": p.get("band") or "mid",  # AOD ist bandunabhängig; Band nur für Key
            "series": series,
        })
    return {"source": "cams", "model_run": ref_iso, "points": out_points}


def post_payload(url, token, payload) -> bool:
    import hashlib
    log.info("Sende Ingest-Token: laenge=%d sha256[0:12]=%s (Server erwartet 5525cc504a0e)",
             len(token), hashlib.sha256(token.encode()).hexdigest()[:12])
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Ingest-Token": token}
    last = ""
    for attempt in range(1, 4):
        try:
            r = requests.post(url, data=body, headers=headers, timeout=120)
            if r.status_code in (200, 201):
                log.info("CAMS ingest ok: %s", r.text[:200]); return True
            if 400 <= r.status_code < 500:
                log.error("rejected %s: %s", r.status_code, r.text[:200]); return False
            last = f"HTTP {r.status_code}"
        except (requests.ConnectionError, requests.Timeout) as e:
            last = str(e)[:120]
        if attempt < 3:
            time.sleep(2 ** attempt)
    log.error("CAMS ingest failed: %s", last)
    return False


def heartbeat(status):
    url = os.environ.get("HEARTBEAT_URL", "").strip()
    token = os.environ.get("CAMS_INGEST_TOKEN", "").strip()
    if not url:
        return
    try:
        requests.get(url, params={"status": status}, headers={"X-Ingest-Token": token}, timeout=15)
    except Exception:
        pass


def main() -> int:
    import xarray as xr
    ingest_url = require_env("CAMS_INGEST_URL")
    ingest_token = require_env("CAMS_INGEST_TOKEN")
    points = load_points()
    if not points:
        log.error("keine Punkte"); return 2

    date_str, run_time = latest_cams_run()
    ref_iso = f"{date_str}T{run_time}:00Z"

    with tempfile.TemporaryDirectory() as tmpd:
        nc = Path(tmpd) / "cams.nc"
        try:
            if not download_cams(date_str, run_time, nc):
                log.error("CAMS download leer"); heartbeat("failed"); return 1
            ds = xr.open_dataset(nc)
            log_ds_vars(ds)
            payload = build_payload(ds, points, ref_iso)
        except Exception as e:
            log.exception("CAMS fetch/parse failed: %s", e)
            heartbeat("failed"); return 1

    log.info("CAMS payload: %d points", len(payload["points"]))
    ok = post_payload(ingest_url, ingest_token, payload)
    heartbeat("ok" if ok else "failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
