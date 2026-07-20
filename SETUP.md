# Setup-Anleitung — weitsicht-forecast-fetcher

Detailliert, von „Repo existiert noch nicht" bis „erster grüner Lauf schreibt
Daten in die Weitsicht-Stage-DB". Reihenfolge einhalten.

**Wichtig vorab — zwei getrennte Worker:**
- `fetch_forecast.py` (ICON-CH2) braucht **keinen** Account/Key — MeteoSchweiz OGD
  ist offen. → **Zuerst diesen verifizieren.**
- `fetch_cams.py` (CAMS Aerosol) braucht einen **ADS-Account + API-Key**. → Erst
  danach, ist optional für den ersten Durchstich.

---

## Schritt 1 — Git-Repo anlegen und zu GitHub pushen

Das Verzeichnis `weitsicht-forecast-fetcher/` ist lokal vorhanden, aber noch kein
Git-Repo. Öffentliches Repo, damit GitHub Actions gratis läuft.

**1a. Leeres Repo auf GitHub erstellen** (Web-UI):
github.com → New repository → Name `weitsicht-forecast-fetcher`, **Public**, KEIN
README/gitignore hinzufügen (haben wir schon) → Create.

**1b. Lokal initialisieren und pushen** (PowerShell im Worker-Ordner):
```powershell
cd C:\Users\mje\GitHub\weitsicht-forecast-fetcher
git init
git add .
git commit -m "Initial: ICON-CH2 + CAMS fetcher für Weitsicht"
git branch -M main
git remote add origin https://github.com/<DEIN-GH-USER-ODER-ORG>/weitsicht-forecast-fetcher.git
git push -u origin main
```

**1c. Branch-Schutz** (empfohlen, CLAUDE.md §7): Settings → Branches → Rule für
`main`: Require pull request reviews. (Für ein Solo-Worker-Repo optional.)

---

## Schritt 2 — GitHub-Secrets setzen (für den Forecast-Worker)

Repo → **Settings → Secrets and variables → Actions → New repository secret**.
Für den ersten Test reichen die drei STAGE-Forecast-Secrets:

| Secret | Wert |
|---|---|
| `STAGE_FORECAST_INGEST_URL` | `https://tool.wetteralarm.ch/weitsicht/stage/api/forecast-ingest.php` |
| `STAGE_FORECAST_INGEST_TOKEN` | **exakt** der `FORECAST_INGEST_TOKEN`-Wert aus `.env.stage` |
| `STAGE_FORECAST_HEARTBEAT_URL` | `https://tool.wetteralarm.ch/weitsicht/stage/api/external-heartbeat.php?job=ingest_forecast` |

> Der Token muss **zeichengenau** mit `.env.stage` übereinstimmen — sonst 403 beim
> Ingest (der Server prüft mit `hash_equals`).

---

## Schritt 3 — Ersten Forecast-Lauf manuell starten (klein)

Repo → **Actions → fetch-forecast → Run workflow**:
- `target` = **stage**
- `horizon_hours` = **24**  (klein für schnellen Test; später 72)
- `reference_datetime` = leer lassen (nimmt den neuesten Lauf)

→ Run workflow.

Warum klein: 24 h × ~10 Parameter = deutlich weniger Downloads/Zeit als 72 h. Zum
Verifizieren der Pipeline reicht das völlig.

---

## Schritt 4 — Actions-Log lesen (der entscheidende Verifikationspunkt)

Der Worker-Code lief noch **nie** — dieser Lauf verifiziert, ob die xarray-
Extraktion von `meteodata-lab` stimmt. Öffne den laufenden Job → Step „Fetch
(stage)". Erwartete Log-Zeilen (ungefähr):

```
[INFO] Loaded 220 points (...)
[INFO] Latest available run: 2026-07-14T06:00:00Z
[INFO] Fetching 10 variables × 25 lead-times for run ...
[INFO] DataArray[t_2m]: dims=(...) shape=(...) coords=[...]   ← STRUKTUR-LOG
[INFO] KDTree: <N> mesh cells, matched 220 points
[INFO]   t_2m: series (25, 220)
...
[INFO] Payload: 220 points, 25 lead-times
[INFO] Ingest ok: {"ok":true,"points":220,"rows":...}
```

**Erfolg** = endet mit `Ingest ok` und Exit 0.

**Wenn es hier bricht** — die häufigsten Fälle (siehe auch Troubleshooting unten):
Schick mir einfach die Zeile `DataArray[t_2m]: dims=… coords=…` aus dem Log, dann
justiere ich die Coord-/Dim-Namen in `fetch_forecast.py` punktgenau. Der Rest der
Pipeline ist davon unabhängig.

---

## Schritt 5 — Prüfen, ob die Daten angekommen sind

Zwei Wege:

**a) Ingest-Antwort im Log** — `Ingest ok: {"ok":true,"points":220,"rows":5500}`
bedeutet: der Server hat die Zeilen in `fernsicht_raw` geschrieben.

**b) Heartbeat/Health** (von deinem Rechner):
```powershell
curl.exe --ssl-no-revoke https://tool.wetteralarm.ch/weitsicht/stage/api/health.php
```
Wenn `ingest_forecast` gerade lief, ist der Heartbeat aktualisiert. (Der Job ist
in Migration 003 noch `is_enabled=0` — er wird also nicht als „overdue" gemeldet,
aber der Heartbeat-Zeitstempel wird trotzdem gesetzt.)

**c) Direkt in der DB** (phpMyAdmin, Stage-DB):
```sql
SELECT source, COUNT(*), MIN(valid_time), MAX(valid_time), MAX(created_at)
FROM fernsicht_raw GROUP BY source;
```

Danach den Score-Batch anstossen (siehe `Doku/deployment.md` Schritt 7):
`cron-runner.php?job=compute_scores` → `?job=publish_cache`.

---

## Schritt 6 — CAMS-Worker (Aerosol), optional aber empfohlen

Der Score läuft auch ohne CAMS (dann `degraded:true`, Aerosol-Gewicht umverteilt).
Für vollen Score:

**6a. ADS-Account + Key:**
1. Registrieren auf `https://ads.atmosphere.copernicus.eu` (kostenlos).
2. Eingeloggt → dein Profil → **Personal Access Token** kopieren.
3. **Lizenz akzeptieren** (wichtige Stolperfalle!): Datensatz-Seite
   „CAMS global atmospheric composition forecasts" öffnen → Reiter „Download" →
   die Nutzungsbedingungen einmal akzeptieren. Ohne das liefert die API 403
   „required licences not accepted".

**6b. GitHub-Secrets ergänzen:**

| Secret | Wert |
|---|---|
| `ADS_API_KEY` | dein ADS Personal Access Token |
| `STAGE_CAMS_INGEST_URL` | `https://tool.wetteralarm.ch/weitsicht/stage/api/cams-ingest.php` |
| `STAGE_CAMS_INGEST_TOKEN` | = `CAMS_INGEST_TOKEN` aus `.env.stage` |
| `STAGE_CAMS_HEARTBEAT_URL` | `…/weitsicht/stage/api/external-heartbeat.php?job=ingest_cams` |

**6c. Starten:** Actions → `fetch-cams` → Run workflow → `target=stage`.
Der erste Lauf loggt die NetCDF-Struktur (`NetCDF: data_vars=… coords=…`) — bei
abweichenden Variablennamen (z. B. `aod550` vs `od550aer`) schick mir die Zeile.

---

## Schritt 7 — Auf Zeitplan + Prod umstellen (wenn Stage stabil)

- **Scheduled Stage** läuft bereits automatisch (fetch-forecast alle 3 h,
  fetch-cams 2×/Tag) — sobald die Secrets gesetzt sind, brauchst du nichts weiter.
- **Prod** aktivieren: `PROD_*`-Secrets setzen (analog, ohne `/stage` in der URL),
  dann Worker manuell mit `target=production` starten. Erst wenn Prod-Läufe grün
  sind: in beiden Workflows den Prod-Step-`if` von
  `github.event.inputs.target == 'production'` auf
  `github.event_name == 'schedule' || github.event.inputs.target == 'production'`
  erweitern → dann beschickt der Zeitplan auch Prod.

---

## Härtung (CLAUDE.md §7/§8)

Die Workflows nutzen aktuell `actions/checkout@v4` / `actions/setup-python@v5`
(Tags). Policy ist **Pinning auf vollen Commit-SHA** (Tag-Reuse-Risiko). Zum
Nachziehen: auf der jeweiligen Action-Seite den SHA des Release holen und
`@v4` → `@<40-stelliger-sha>` ersetzen. Sag Bescheid, dann pinne ich es.

`permissions: contents: read` ist der Default für dieses Repo (nur Lesen nötig,
der Worker pusht via HTTPS-POST, nicht via Git).

---

## Troubleshooting

| Symptom (im Actions-Log) | Ursache / Fix |
|---|---|
| `Missing required env var: FORECAST_INGEST_URL` | Secret nicht gesetzt oder falscher Name |
| `Ingest rejected 403` | Token ≠ `.env.stage`; oder `.env.stage` wird nicht geladen (liegt eine `.env` daneben?) |
| `Ingest rejected 400 … invalid JSON / missing field` | Payload-/Schema-Problem — Log an mich |
| `lon/lat coords not found; coords=[...]` | meteodata-lab-Coord-Namen anders → `find_lonlat()` anpassen (Log an mich) |
| `cell dim (size N) not found` | Zell-Dim-Erkennung → `find_cell_dim()` anpassen (Log an mich) |
| `Kein verfügbarer ICON-CH2-Lauf gefunden` | gerade kein Lauf publiziert; später erneut, oder `reference_datetime` explizit setzen |
| CAMS `403 required licences not accepted` | Lizenz auf der ADS-Datensatz-Seite akzeptieren (Schritt 6a.3) |
| `pip install` schlägt fehl (eccodes/meteodata-lab) | Python-Version prüfen — muss 3.12 sein (meteodata-lab <3.13) |

---

## Wenn Daten `points.json` sich ändern

`points.json` hier ist eine **Kopie** von `Weitsicht/config/points.json`. Wird die
Punkt-Config dort neu generiert (`cron-runner.php?job=generate_points`), die Datei
herüberkopieren und committen:
```powershell
copy C:\Users\mje\GitHub\Weitsicht\config\points.json C:\Users\mje\GitHub\weitsicht-forecast-fetcher\points.json
git add points.json; git commit -m "sync points.json"; git push
```
