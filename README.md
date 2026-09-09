# bolets-mc-data

Arxiu públic d’**ESCAT (Meteoclimatic)** per al [Bolets Explorador](https://github.com/).  
Public ESCAT snapshot archive + GitHub Pages CDN for `panel.json`, `hourly_rain.json` and `hourly_meteo.json`.

## Per què / Why

Meteoclimatic bloqueja `mapinfo/ESCAT?d=YYYYMMDD` per dies **passats** (401).  
Només funciona l’snapshot **actual** (`mapinfo/ESCAT` sense `?d=`, o feeds XML/RSS).

El XML públic només exposa `rain/total` **acumulat del dia**. Per obtenir pluja **horària** cal capturar cada hora i calcular el delta del cumulatiu.  
La mateixa captura horària desa també snapshots d’humitat, vent i temperatura (no deltas).

## Què fa / What it does

| Peça | Descripció |
|---|---|
| **Action diària** | Cron `30 21 * * *` (21:30 UTC ≈ **23:30 Europe/Madrid a l’estiu**) |
| **`scripts/capture_escat.py`** | Baixa XML+RSS → `data/daily/ESCAT_YYYYMMDD.{json,csv}` + `docs/panel.json` |
| **Action horària** | Cron `0 * * * *` (cada hora UTC) |
| **`scripts/capture_hourly.py`** | Snapshot horari → `data/hourly/ESCAT_YYYYMMDD_HH.json` + `docs/hourly_rain.json` (Ph) + `docs/hourly_meteo.json` (Ph + HR/W/…) |
| **`scripts/build_panel.py`** | Fusiona `stations_keep` + dies → `docs/panel.json` |
| **GitHub Pages** | Serveix `docs/` (`panel.json`, `hourly_rain.json`, `hourly_meteo.json`) |

> **DST / horari (diari):** el cron de GitHub Actions és sempre en UTC.
> - Hivern (CET, UTC+1): `21:30 UTC` → **22:30** Europe/Madrid  
> - Estiu (CEST, UTC+2): `21:30 UTC` → **23:30** Europe/Madrid  
> Objectiu nominal: ~23:30 Madrid. Amb un sol cron UTC no es pot clavar les dues estacions; `30 21 * * *` prioritzà l’estiu.

## Ús local

```bash
cd bolets-mc-data
# panell diari
python3 scripts/build_panel.py
python3 scripts/capture_escat.py
python3 scripts/capture_escat.py --force

# captura horària (Europe/Madrid, hora floored) + rebuild hourly_rain + hourly_meteo
python3 scripts/capture_hourly.py
python3 scripts/capture_hourly.py --force
python3 scripts/capture_hourly.py --rebuild-only   # sense fetch
```

Sense dependències externes (stdlib Python 3.10+).

## Schema `panel.json`

Claus d’alt nivell (compatible amb `build_explorer_mc.py`):

- `version` — `1`
- `built` — ISO UTC
- `ccaa` — `"ESCAT"`
- `stations` — `[{id, mc_id, name, lon, lat, elev}, …]`
- `days` — `["YYYY-MM-DD", …]`
- `series` — `{ station_id: { day: {P, TX, N, HX, HR, W, WDG} } }`
- `checksum` — SHA-256 del JSON canònic de `{stations,days,series}`

Camps de sèrie: **P** precipitació, **TX** temp. màx, **N** temp. mín, **HX** hum. màx, **HR** hum. actual, **W** vent màx, **WDG** direcció vent.

## Schema `hourly_rain.json`

- `version` — `1`
- `built` — ISO UTC
- `ccaa` — `"ESCAT"`
- `hours` — `["YYYY-MM-DDTHH:00", …]` (segell Europe/Madrid)
- `stations` — `[{id: MC_…, mc_id, name, lon, lat, elev}, …]`
- `series` — `{ station_id: { "YYYY-MM-DDTHH:00": Ph_mm } }`
- `delta_note` — regles de delta
- `retention_days` — ~14 dies de raw a `data/hourly/`

**Deltas:** `Ph = max(0, cum_ara − cum_prev)` el mateix dia; si el cumulatiu baixa (reset), `Ph = max(0, cum_ara)`. El primer mostratge d’una estació posa `Ph = 0` (baseline); no s’inventen hores anteriors.

## Schema `hourly_meteo.json`

Producte únic amb pluja horària **i** snapshots meteo:

- `version` — `1`
- `built` — ISO UTC
- `ccaa` — `"ESCAT"`
- `hours` / `stations` — igual que `hourly_rain.json`
- `seriesPh` — `{ station_id: { hour: Ph_mm } }` (mateixes regles de delta que `hourly_rain.series`)
- `seriesHR` — Hum.act (snapshot)
- `seriesHX` — Hum.max
- `seriesHN` — Hum.min (si present)
- `seriesW` — Vient.max
- `seriesWDG` — Vient.dir
- `seriesWA` — Vient.act (si present)
- `seriesT` — Temp.act (“now”)
- `seriesTX` / `seriesTN` — Temp.max / Temp.min
- `delta_note` / `snapshot_note` / `retention_days`

**Raw** `data/hourly/ESCAT_YYYYMMDD_HH.json`: cada estació guarda `id`, `name`, `lon`, `lat`, `cum` / `Precip.diaria`, `Hum.*`, `Vient.*`, `Temp.*`.

## Explorador

```text
https://jnoya99.github.io/bolets-mc-data/panel.json
https://jnoya99.github.io/bolets-mc-data/hourly_rain.json
https://jnoya99.github.io/bolets-mc-data/hourly_meteo.json
```

```js
window.__MC_REMOTE_URL = "https://jnoya99.github.io/bolets-mc-data/panel.json";
window.__MC_HOURLY_URL = "https://jnoya99.github.io/bolets-mc-data/hourly_rain.json";
window.__MC_HOURLY_METEO_URL = "https://jnoya99.github.io/bolets-mc-data/hourly_meteo.json";
```

Vegeu [`docs/explorer-integration.md`](docs/explorer-integration.md).

## Llicència de dades

Dades Meteoclimatic: Creative Commons Attribution-NonCommercial-NoDerivs 3.0  
(vegeu el copyright del feed XML). Aquest repo és un arxiu tècnic per a ús amb l’explorador Bolets; respecteu la llicència CC del proveïdor.
