#!/usr/bin/env python3
"""
generate_points — erzeugt points.json GELÄNDE-basiert (ersetzt die webcam-basierte
Variante).

Idee: Das ICON-CH2-Mesh liefert für JEDE Zelle die reale Geländehöhe (HSURF).
Pro SRF-Region werden die Mesh-Zellen im Polygon nach Höhe in low/mid/high
gebucketet; je Band die Median-Höhen-Zelle als Repräsentativpunkt. Dadurch bekommt
jede Region ihre Höhenbänder aus dem echten Gelände — flächendeckend, kein
Webcam-Zufall, keine 63er-Fallback-Lücke. Reine Mittelland-Regionen haben ehrlich
nur ein Tal-Band (dort gibt es kein alpines Gelände).

Koordinaten + HSURF kommen primär aus dem STATISCHEN horizontal_constants-File
(run-unabhängig, immer verfügbar). Fallback: HSURF aus dem neuesten Lauf (Step 0).

Output: points.json (schema-kompatibel zum Rest der Pipeline, source="terrain").
Aufruf via Workflow "generate-points" (lädt points.json als Artifact hoch).
Gelände ist statisch → nur selten nötig (bei Regionen-/Schwellen-Änderung).

Env: WA_REGIONS_URL (optional, default my.wetteralarm.ch v7 meteo.geojson)
Exit: 0 ok · 1 Fehler
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import numpy as np
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("generate-points")

HERE = Path(__file__).resolve().parent

STAC_BASE = "https://data.geo.admin.ch/api/stac/v1/collections/ch.meteoschweiz.ogd-forecasting-icon-ch2"
HORIZONTAL_CONSTANTS = "horizontal_constants_icon-ch2-eps.grib2"
WA_REGIONS_URL = os.environ.get("WA_REGIONS_URL", "https://my.wetteralarm.ch/v7/maps/meteo.geojson")

CH_BBOX = (5.8, 45.7, 10.6, 47.9)          # lng_min, lat_min, lng_max, lat_max
BAND_LOW_MAX = 900                          # < 900 m → low
BAND_MID_MAX = 1500                         # 900..<1500 → mid ; ≥1500 → high
NOMINAL = {"low": 600, "mid": 1200, "high": 2000}


def band_of(alt: float) -> str:
    return "low" if alt < BAND_LOW_MAX else ("mid" if alt < BAND_MID_MAX else "high")


# ─────────────────────────────────────────────────────────────────────────────
# Mesh (CLAT/CLON/HSURF) aus dem statischen horizontal_constants-File
# ─────────────────────────────────────────────────────────────────────────────
def fetch_constants_url() -> str:
    r = requests.get(f"{STAC_BASE}/assets", timeout=30)
    r.raise_for_status()
    for a in r.json().get("assets", []):
        if a.get("id") == HORIZONTAL_CONSTANTS:
            return a["href"]
    raise RuntimeError("horizontal_constants asset nicht in STAC gefunden")


def load_mesh_from_constants(tmp: Path):
    """
    Liefert (lon, lat, hsurf) als 1D-float64-Arrays je Mesh-Zelle — oder None,
    falls HSURF im statischen File nicht vorhanden ist (dann Run-Fallback).
    Nutzt die eccodes-Low-Level-API (Muster wie wetteralarm-kenda-fetcher).
    """
    import eccodes

    url = fetch_constants_url()
    path = tmp / HORIZONTAL_CONSTANTS
    log.info("Lade horizontal_constants …")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with path.open("wb") as f:
            for chunk in r.iter_content(1 << 16):
                if chunk:
                    f.write(chunk)

    lat_names = {"clat", "tlat", "rlat", "latitude", "lat"}
    lon_names = {"clon", "tlon", "rlon", "longitude", "lon"}
    hsurf_names = {"hsurf", "h", "orog", "fis"}   # FIS = Geopotential → /g
    clat = clon = hsurf = None
    hsurf_is_geopot = False
    seen = []

    with path.open("rb") as f:
        while True:
            gid = eccodes.codes_grib_new_from_file(f)
            if gid is None:
                break
            try:
                short = eccodes.codes_get(gid, "shortName")
                seen.append(short)
                lc = short.lower()
                if clat is None and lc in lat_names:
                    clat = np.asarray(eccodes.codes_get_array(gid, "values"), dtype=np.float64)
                elif clon is None and lc in lon_names:
                    clon = np.asarray(eccodes.codes_get_array(gid, "values"), dtype=np.float64)
                elif hsurf is None and lc in hsurf_names:
                    hsurf = np.asarray(eccodes.codes_get_array(gid, "values"), dtype=np.float64)
                    hsurf_is_geopot = (lc == "fis")
            finally:
                eccodes.codes_release(gid)

    log.info("shortNames im constants-File: %s", ", ".join(seen))
    if clat is None or clon is None:
        raise RuntimeError(f"CLAT/CLON nicht gefunden (shortNames: {seen})")

    # Radiant → Grad (ICON liefert teils Radiant)
    if np.nanmax(np.abs(clat)) <= np.pi + 0.01:
        clat = np.degrees(clat); clon = np.degrees(clon)

    if hsurf is None:
        log.warning("HSURF nicht im statischen File — Run-Fallback wird versucht.")
        return clon, clat, None
    if hsurf_is_geopot:
        hsurf = hsurf / 9.80665
    log.info("Mesh aus constants: %d Zellen, HSURF %.0f..%.0f m",
             clat.size, float(np.nanmin(hsurf)), float(np.nanmax(hsurf)))
    return clon, clat, hsurf


def load_hsurf_from_run(clon_ref_size: int):
    """Fallback: HSURF (Step 0) aus dem neuesten Lauf via meteodata-lab."""
    from fetch_forecast import get_latest_ref, fetch_variable, find_lonlat, find_cell_dim
    ref = get_latest_ref()
    if not ref:
        raise RuntimeError("Kein Lauf für HSURF-Fallback verfügbar")
    da = fetch_variable("HSURF", ref, [timedelta(0)], False)
    lon, lat = find_lonlat(da)
    cdim = find_cell_dim(da, lon.size)
    sel = {d: 0 for d in da.dims if d != cdim}
    arr = da.isel(**sel) if sel else da
    vals = np.asarray(arr.values, dtype=np.float64).ravel()
    log.info("HSURF aus Lauf %s: %d Zellen", ref, vals.size)
    return lon, lat, vals


# ─────────────────────────────────────────────────────────────────────────────
# Point-in-Polygon (vektorisiert über Zellen, Schleife über Ring-Kanten)
# ─────────────────────────────────────────────────────────────────────────────
def in_ring(px, py, ring) -> np.ndarray:
    rx = np.asarray([p[0] for p in ring], dtype=np.float64)
    ry = np.asarray([p[1] for p in ring], dtype=np.float64)
    inside = np.zeros(px.shape, dtype=bool)
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi, xj, yj = rx[i], ry[i], rx[j], ry[j]
        cond = ((yi > py) != (yj > py)) & (px < (xj - xi) * (py - yi) / (yj - yi + 1e-12) + xi)
        inside ^= cond
        j = i
    return inside


def in_polygon(px, py, poly) -> np.ndarray:
    inside = in_ring(px, py, poly[0])          # äusserer Ring
    for hole in poly[1:]:                       # Löcher abziehen
        inside &= ~in_ring(px, py, hole)
    return inside


def in_geometry(px, py, geom) -> np.ndarray:
    t = geom.get("type")
    if t == "Polygon":
        return in_polygon(px, py, geom["coordinates"])
    if t == "MultiPolygon":
        acc = np.zeros(px.shape, dtype=bool)
        for poly in geom["coordinates"]:
            acc |= in_polygon(px, py, poly)
        return acc
    return np.zeros(px.shape, dtype=bool)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    # Regionen
    log.info("Lade Regionen-GeoJSON …")
    rg = requests.get(WA_REGIONS_URL, timeout=30)
    rg.raise_for_status()
    features = rg.json().get("features", [])
    if not features:
        log.error("GeoJSON leer"); return 1
    log.info("%d Regionen", len(features))

    # Mesh
    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        try:
            lon, lat, hsurf = load_mesh_from_constants(tmp)
            if hsurf is None:
                lon, lat, hsurf = load_hsurf_from_run(lon.size)
        except Exception as e:
            log.warning("constants-Mesh fehlgeschlagen (%s) — Run-Fallback", str(e)[:120])
            lon, lat, hsurf = load_hsurf_from_run(0)

    if not (lon.size == lat.size == hsurf.size):
        log.error("Grössen-Mismatch lon/lat/hsurf: %d/%d/%d", lon.size, lat.size, hsurf.size)
        return 1

    # Auf CH-Bbox reduzieren
    lo0, la0, lo1, la1 = CH_BBOX
    m = (lon >= lo0) & (lon <= lo1) & (lat >= la0) & (lat <= la1) & np.isfinite(hsurf)
    clon, clat, chs = lon[m], lat[m], hsurf[m]
    log.info("CH-Zellen: %d / %d", clon.size, lon.size)
    if clon.size < 1000:
        log.error("Zu wenige CH-Zellen — HSURF/Koordinaten prüfen")
        return 1

    points = []
    stats = {"regions": len(features), "with_bands": 0, "center_fallback": 0,
             "band_counts": {"low": 0, "mid": 0, "high": 0}}

    for feat in features:
        p = feat["properties"]
        rid = int(p["srf_id"])
        names = {"de": p["de"]["name"], "fr": p["fr"]["name"], "it": p["it"]["name"]}

        # Zellen der Region: Bbox-Vorfilter auf die Region-Grenzen, dann Ray-Cast
        geom = feat["geometry"]
        rb = region_bbox(geom)
        pre = (clon >= rb[0]) & (clon <= rb[2]) & (clat >= rb[1]) & (clat <= rb[3])
        idx = np.where(pre)[0]
        if idx.size:
            inside = in_geometry(clon[idx], clat[idx], geom)
            idx = idx[inside]

        if idx.size == 0:
            # Fallback: nächste Zelle zum Region-Zentrum
            c = p.get("center", {})
            cx, cy = float(c.get("long", 0)), float(c.get("lat", 0))
            k = int(np.argmin((clon - cx) ** 2 + (clat - cy) ** 2))
            alt = float(chs[k]); bnd = band_of(alt)
            points.append(mk_point(rid, names, bnd, clat[k], clon[k], alt, "center_nearest", 1))
            stats["center_fallback"] += 1
            stats["band_counts"][bnd] += 1
            continue

        # Nach Band bucketen, je Band die Median-Höhen-Zelle als Repräsentant
        alts = chs[idx]
        had_band = False
        for bnd in ("low", "mid", "high"):
            if bnd == "low":
                sel = idx[alts < BAND_LOW_MAX]
            elif bnd == "mid":
                sel = idx[(alts >= BAND_LOW_MAX) & (alts < BAND_MID_MAX)]
            else:
                sel = idx[alts >= BAND_MID_MAX]
            if sel.size == 0:
                continue
            order = sel[np.argsort(chs[sel])]
            rep = int(order[order.size // 2])   # Median-Höhe
            points.append(mk_point(rid, names, bnd, clat[rep], clon[rep], float(chs[rep]), "terrain", int(sel.size)))
            stats["band_counts"][bnd] += 1
            had_band = True
        if had_band:
            stats["with_bands"] += 1

    out = {
        "_comment": "GENERIERT von generate_points.py (GELÄNDE-basiert, HSURF). Nicht von Hand editieren.",
        "generated_at": "static-terrain",
        "sources": {"regions_geojson": WA_REGIONS_URL, "elevation": "ICON-CH2 HSURF (Mesh)"},
        "band_thresholds": {"low_max": BAND_LOW_MAX, "mid_max": BAND_MID_MAX},
        "nominal_band_elev": NOMINAL,
        "stats": {**stats, "total_points": len(points)},
        "points": points,
    }
    (HERE / "points.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("points.json geschrieben: %d Punkte (low=%d mid=%d high=%d, %d center-fallback)",
             len(points), stats["band_counts"]["low"], stats["band_counts"]["mid"],
             stats["band_counts"]["high"], stats["center_fallback"])
    return 0


def region_bbox(geom):
    xs, ys = [], []
    def walk(coords, depth):
        if depth == 1:
            for x, y in coords:
                xs.append(x); ys.append(y)
        else:
            for c in coords:
                walk(c, depth - 1)
    if geom["type"] == "Polygon":
        walk(geom["coordinates"], 2)
    else:  # MultiPolygon
        walk(geom["coordinates"], 3)
    return (min(xs), min(ys), max(xs), max(ys))


def mk_point(rid, names, band, lat, lon, elev, source, ncells):
    return {
        "point_id": f"r{rid}_{band}", "region_id": rid, "region_name": names,
        "band": band, "lat": round(float(lat), 5), "lon": round(float(lon), 5),
        "elev": int(round(elev)), "source": source, "cells_in_band": ncells,
    }


if __name__ == "__main__":
    sys.exit(main())
