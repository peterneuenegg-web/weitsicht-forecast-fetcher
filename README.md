# weitsicht-forecast-fetcher

Externer Worker für das **Weitsicht**-Backend (Fernsicht-Score). Dekodiert
MeteoSchweiz **ICON-CH2-EPS** (GRIB2) und **CAMS AOD550** in GitHub Actions und
pusht die an ~220 Referenzpunkten ausgelesenen Werte als JSON an die Ingest-
Endpoints auf `weitsicht.wetteralarm.ch`.

## Warum ein eigenes (öffentliches) Repo

Infomaniak Shared Hosting hat kein `eccodes` und `exec()`/`shell_exec()` sind
deaktiviert → GRIB2/NetCDF können dort nicht dekodiert werden. Wie bei
`wetteralarm-kenda-fetcher` / `wetteralarm-hail-fetcher` macht die Konversion
ein externer Worker. Public-Repo → GitHub Actions gratis; nichts Sensibles im
Code (Tokens sind GitHub-Secrets).

## Wie es läuft

```
fetch_forecast.py (alle 3 h)                fetch_cams.py (2×/Tag)
  meteodata-lab ogd_api                       cdsapi → CAMS ADS
  → ICON-CH2 t_2m,td_2m,tot_prec,u/v_10m,     → total/dust AOD550, CH-Bbox
    vmax_10m,ceiling,clcl,clct,hzerocl,hsurf  → NetCDF
  → Nearest-Cell (KDTree) an 220 Punkten      → bilinear auf 220 Punkte
  → POST api/forecast-ingest.php              → POST api/cams-ingest.php
```

- **ICON-CH2** (`ch.meteoschweiz.ogd-forecasting-icon-ch2`, ctrl-Member) über die
  offizielle Lib **`meteodata-lab`** (`ogd_api.get_from_ogd`). Sie kapselt die
  STAC-Enumeration + GRIB-Dekodierung und liefert xarray-DataArrays **inkl.
  Mesh-Lat/Lon**. `detect_latest_run()` sucht den neuesten verfügbaren Lauf.
- **RH** wird backend-seitig aus `t_2m`/`td_2m` abgeleitet (nicht hier).
- **HSURF** (konstant) setzt das Band der center-Fallback-Punkte
  (`needs_elevation`) und wird mitgeschickt.
- Payload-Schema = `points[].series[]` (identisch für forecast/cams), siehe
  Weitsicht `api/forecast-ingest.php` / `api/cams-ingest.php`.

## points.json

`points.json` ist eine **Kopie** von `Weitsicht/config/points.json` (generiert von
`cron/generate_points.php`). Bei Änderung der Punkt-Config dort → hierher kopieren
und committen. (Bewusst kopiert statt live geladen: `config/` ist auf dem Server
web-geblockt, und der Punktsatz ändert sich selten.)

## Setup

> **Detaillierte Schritt-für-Schritt-Anleitung: [SETUP.md](SETUP.md)** — von
> `git init` über ADS-Key bis zum ersten grünen Lauf inkl. Troubleshooting.

### GitHub Secrets (Settings → Secrets and variables → Actions)

Dual-Env wie `kenda-fetcher`: **Schedule beschickt nur STAGE**, Prod läuft via
manuellem `workflow_dispatch` mit `target=production` (bis der Worker verifiziert
ist — dann im Workflow den Prod-Step-`if` um `schedule` erweitern).

**Forecast — Stage:**
- `STAGE_FORECAST_INGEST_URL` — `https://tool.wetteralarm.ch/weitsicht/stage/api/forecast-ingest.php`
- `STAGE_FORECAST_INGEST_TOKEN` — matcht `FORECAST_INGEST_TOKEN` in `.env.stage`
- `STAGE_FORECAST_HEARTBEAT_URL` — `…/weitsicht/stage/api/external-heartbeat.php?job=ingest_forecast`

**Forecast — Prod:** `PROD_FORECAST_INGEST_URL` / `_TOKEN` / `PROD_FORECAST_HEARTBEAT_URL`
(analog ohne `/stage`, matcht `.env.production`).

**CAMS — Stage/Prod:** `STAGE_CAMS_INGEST_URL` / `_TOKEN` / `STAGE_CAMS_HEARTBEAT_URL`
und `PROD_CAMS_*` (analog, `?job=ingest_cams`).

**CAMS — gemeinsam:** `ADS_API_KEY` (CAMS-ADS Personal Access Token, kostenlose
Registrierung auf ads.atmosphere.copernicus.eu), `ADS_URL` optional.

Actions-Härtung (CLAUDE.md §7): `permissions: contents: read`, Third-Party-Actions
auf Commit-SHA pinnen, Secrets nie loggen.

### Lokaler Testlauf (PowerShell)

```powershell
pip install -r requirements.txt
$env:FORECAST_INGEST_URL = "https://tool.wetteralarm.ch/weitsicht/stage/api/forecast-ingest.php"
$env:FORECAST_INGEST_TOKEN = "…"
$env:HORIZON_HOURS = "24"          # kleiner für schnellen Test
python fetch_forecast.py
```

## Tuning

| Env | Default | Wirkung |
|---|---|---|
| `HORIZON_HOURS` | 72 | Vorhersagehorizont (ICON-CH2 max. 120) |
| `STEP_HOURS` | 1 | Lead-Time-Schrittweite Forecast |
| `CAMS_STEP_HOURS` | 6 | AOD-Schrittweite (ändert sich langsam) |
| `REFERENCE_DATETIME` | (leer) | fixer Lauf für Backfill/Test |

## Status / Verifikation

**M2 — erste Version.** Die exakten xarray-Dim-/Koordinaten-Namen von
`meteodata-lab` (Zell-Dim, Lead-Coord, lon/lat) und die CAMS-NetCDF-Variablen
werden **beim ersten CI-Lauf** aus dem Struktur-Log (`log_dataarray_shape` /
`log_ds_vars`) verifiziert; die Extraktion ist defensiv gebaut, aber der erste
grüne Run bestätigt die Annahmen. Danach ggf. Feinjustierung der Coord-Namen.

## Lizenz / Attribution

Daten: „Quelle: MeteoSchweiz" (ICON-CH2) und „Contains modified Copernicus
Atmosphere Monitoring Service information" (CAMS). Code: intern (Wetter-Alarm).

## Beziehung zu Weitsicht

Teil des Weitsicht-Stacks (Fernsicht-Score). Ingest-Schema + Score-Logik:
`Weitsicht/Doku/README.md` + `Doku/ENTSCHEIDUNGEN.md`.
