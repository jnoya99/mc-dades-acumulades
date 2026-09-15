# mc-dades-acumulades

Arxiu públic d’**ESCAT (Meteoclimatic)** i estacions de **muntanya** complementàries per al [Bolets Explorador](https://github.com/).  
Public ESCAT + mountain snapshot archive + GitHub Pages CDN for `panel.json`, `hourly_rain.json`, `hourly_meteo.json` and the additive `*_mountain.json` products.

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
| **`scripts/capture_mountain.py`** | 86 estacions muntanya (MO/MG/CMI/MCADI) → `data/{daily,hourly}/MOUNTAIN_*` + `docs/panel_mountain.json` / `hourly_*_mountain.json` |
| **`scripts/http_util.py`** | GET compartit amb reintents (backoff + jitter; timeouts, URLError, HTTP 429/5xx) |
| **`scripts/build_panel.py`** | Fusiona `stations_keep` + dies → `docs/panel.json` |
| **GitHub Pages** | Serveix `docs/` (`panel.json`, `hourly_rain.json`, `hourly_meteo.json`) |

> **DST / horari (diari):** el cron de GitHub Actions és sempre en UTC.
> - Hivern (CET, UTC+1): `21:30 UTC` → **22:30** Europe/Madrid  
> - Estiu (CEST, UTC+2): `21:30 UTC` → **23:30** Europe/Madrid  
> Objectiu nominal: ~23:30 Madrid. Amb un sol cron UTC no es pot clavar les dues estacions; `30 21 * * *` prioritzà l’estiu.

## Ús local

```bash
cd mc-dades-acumulades
# panell diari ESCAT
python3 scripts/build_panel.py
python3 scripts/capture_escat.py
python3 scripts/capture_escat.py --force

# captura horària ESCAT (Europe/Madrid, hora floored) + rebuild hourly_rain + hourly_meteo
python3 scripts/capture_hourly.py
python3 scripts/capture_hourly.py --force
python3 scripts/capture_hourly.py --rebuild-only   # sense fetch

# muntanya (manifest data/stations_mountain.json; throttle 0.4–0.8s entre GETs)
python3 scripts/capture_mountain.py --mode daily --force
python3 scripts/capture_mountain.py --mode hourly --force   # MO/MG/MCADI only
python3 scripts/capture_mountain.py --mode daily --only MO_53,MG_97,CMI_CAT_23011254800
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


## Estacions de muntanya (additive)

Manifest versionat: `data/stations_mountain.json` (**86** estacions). IDs namespaced — **no** s’afegeixen a `stations_keep.csv`:

| Prefix | Font | n | Captura |
|---|---|---:|---|
| `MO_*` | MeteOsona | 37 | diària + horària |
| `MG_*` | Meteoguilleries | 35 | diària + horària |
| `CMI_*` | ClimaMeteoInfo | 12 | **només diària** (pàgines grans) |
| `MCADI_*` | Meteocadí (WeatherLink) | 2 | diària + horària |

Productes (schema compatible amb ESCAT, `ccaa: "MOUNTAIN"`):

- `docs/panel_mountain.json`
- `docs/hourly_rain_mountain.json` / `docs/hourly_meteo_mountain.json`

Els consumidors ESCAT existents (`panel.json`, `hourly_*.json`) **no canvien**. Les sèries mountain són additives i es poden fusionar pel client filtrant per prefix d’id.

### Gaps de camps per font

- **MeteOsona** — JSON `var estacio={...}`: T/HR/W + pluja cumulativa del dia (`actuals.pluja`). Ideal per Ph horari.
- **Meteoguilleries** — darrera fila `arrayDades10`: `plujaAvui` sovint null → es fa servir `plujaAra` com a cumulatiu. Pressió a cotes altes de vegades descalibrada.
- **ClimaMeteoInfo** — gauges HTML (`temp-now` / `rain-now` / …). La humitat pot sortir a 0 (avís al web). Min/max des de `gauge-min-val` / `gauge-max-val`.
- **Meteocadí** — token de l’URL `/embeddablePage/show/<token>/` → `weatherlink.com/.../summaryData/<token>`. Pluja dia a `aggregatedValues.Rain.DAY`.

Throttle: GETs seqüencials amb delay ~0.4–0.8 s (baixa concurrència). Reintents HTTP compartits via `scripts/http_util.py`.

Fora d’abast d’aquest arxiu: 35 ESCAT excloses per QC, XEMA, Weather Underground (cal API key).

## Explorador

```text
https://jnoya99.github.io/mc-dades-acumulades/panel.json
https://jnoya99.github.io/mc-dades-acumulades/hourly_rain.json
https://jnoya99.github.io/mc-dades-acumulades/hourly_meteo.json
https://jnoya99.github.io/mc-dades-acumulades/panel_mountain.json
https://jnoya99.github.io/mc-dades-acumulades/hourly_rain_mountain.json
https://jnoya99.github.io/mc-dades-acumulades/hourly_meteo_mountain.json
```

```js
window.__MC_REMOTE_URL = "https://jnoya99.github.io/mc-dades-acumulades/panel.json";
window.__MC_HOURLY_URL = "https://jnoya99.github.io/mc-dades-acumulades/hourly_rain.json";
window.__MC_HOURLY_METEO_URL = "https://jnoya99.github.io/mc-dades-acumulades/hourly_meteo.json";
```

Vegeu [`docs/explorer-integration.md`](docs/explorer-integration.md).

## Llicència de dades

Dades Meteoclimatic: Creative Commons Attribution-NonCommercial-NoDerivs 3.0  
(vegeu el copyright del feed XML). Aquest repo és un arxiu tècnic per a ús amb l’explorador Bolets; respecteu la llicència CC del proveïdor.
