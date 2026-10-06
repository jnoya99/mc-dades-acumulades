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
| **`scripts/capture_escat.py`** | Baixa XML+RSS → `data/daily/ESCAT_YYYYMMDD.{json,csv}` + `docs/panel.json`. Segell de dia = dia de tancament Madrid (`--date` / regla del migdia / schedule), merge monotònic per estació, i dies passats des de l’últim horari cru. |
| **Action horària** | Cron `0 * * * *` (cada hora UTC) |
| **`scripts/capture_hourly.py`** | Snapshot horari → `data/hourly/ESCAT_YYYYMMDD_HH.json` + `docs/hourly_rain.json` (Ph slim: zeros omitted) + `docs/hourly_meteo.json` (Ph + HR/W/…) |
| **`scripts/capture_mountain.py`** | 86 estacions muntanya (MO/MG/CMI/MCADI) → `data/{daily,hourly}/MOUNTAIN_*` + `docs/panel_mountain.json` / `hourly_*_mountain.json` |
| **Action AEMET diària** | Cron `0 22 * * *` UTC → `docs/panel_aemet.json` (secret `AEMET_API_KEY`; fallback HF) |
| **`scripts/capture_aemet.py`** | 87 estacions CAT (`AE_*`) · OpenData diari o mirall HuggingFace |
| **Action AEMET horària** | Cron `20 */2 * * *` UTC → `docs/hourly_rain_aemet.json` + `hourly_meteo_aemet.json` |
| **`scripts/capture_aemet_hourly.py`** | OpenData `observacion/convencional/todas` (1 crida) · Ph = `prec` horari |
| **Action XEMA horària** | Cron `35 * * * *` UTC → `docs/hourly_rain_xema.json` + `hourly_meteo_xema.json` |
| **`scripts/capture_xema_hourly.py`** | Socrata `nzvn-apee` (públic, sense clau) · PPT 35 + T/HR/vent · hora Europe/Madrid |
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
python3 scripts/capture_mountain.py --mode hourly --rebuild-only
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
- `series` — `{ station_id: { "YYYY-MM-DDTHH:00": Ph_mm } }` (**només Ph ≠ 0**; clau absent ≡ 0 mm)
- `delta_note` — regles de delta
- `retention_days` — ~14 dies de raw a `data/hourly/`
- `zeros_omitted` — `true` (slim publish; mateix schema explorador)

**Deltas:** `Ph = max(0, cum_ara − cum_prev)` el mateix dia; si el dia canvia i el cum és igual (plateau d’ahir), `Ph = 0` (sense fantasma a les 00:00); si el dia canvia i el cum ha crescut sense veure el reset, `Ph = cum` (reset perdut); si el cumulatiu baixa (reset), `Ph = max(0, cum_ara)`. El primer mostratge d’una estació posa `Ph = 0` (baseline); no s’inventen hores anteriors. Els zeros no es publiquen a `series` (estalvi ~10× en `hourly_rain*.json`); peff/dipòsit tracten absència com 0 mm.

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


## AEMET Catalunya (additive)

Manifest: `data/stations_aemet.json` (**87** estacions, IDs `AE_<indicativo>` — mateix catàleg que Bolets Explorador `AEMET_ST`).

| Peça | Descripció |
|---|---|
| **Action `daily-aemet`** | Cron `0 22 * * *` UTC + `workflow_dispatch` |
| **`scripts/capture_aemet.py`** | OpenData climatologia diària → `data/daily/AEMET_YYYYMMDD.json` + `docs/panel_aemet.json` |
| **Action `hourly-aemet`** | Cron `20 */2 * * *` UTC + `workflow_dispatch` |
| **`scripts/capture_aemet_hourly.py`** | OpenData observació convencional → `data/hourly/AEMET_YYYYMMDD_HH.json` + `docs/hourly_*_aemet.json` |
| **Secret** | `AEMET_API_KEY` (JWT d'[AEMET OpenData](https://opendata.aemet.es/)) |

```bash
# diari — amb clau (recomanat)
export AEMET_API_KEY='…'
python3 scripts/capture_aemet.py --source api --force

# diari — sense clau: mirall HuggingFace datania/aemet (pot anar amb retard)
python3 scripts/capture_aemet.py --source hf --force

# horari — obliga secret (no hi ha mirall HF d'observació horària)
python3 scripts/capture_aemet_hourly.py --force
python3 scripts/capture_aemet_hourly.py --rebuild-only
```

Productes diaris (`ccaa: "AEMET-CAT"`, schema com `panel_mountain.json`):

- `docs/panel_aemet.json` → Pages `…/panel_aemet.json`
- Sèries `AE_*` amb cel·les `{P, TX, N, HX, HR, W, WDG}` (prec / tmax / tmin / hrMax / hrMedia / racha / dir)

Productes horaris (schema com `hourly_rain.json` / `hourly_meteo.json`):

- `docs/hourly_rain_aemet.json` — `series[AE_*][YYYY-MM-DDTHH:00] = Ph_mm`
- `docs/hourly_meteo_aemet.json` — `seriesPh` + snapshots `seriesHR`/`seriesT`/`seriesTX`/`seriesTN`/`seriesW`/`seriesWA`/`seriesWDG`
- **Ph:** AEMET `prec` ja és mm de l'hora que acaba a `fint` (UTC) → es publica directe (sense delta de cumulatiu ESCAT). Segell d'hora = Europe/Madrid.
- **Font:** una sola crida `GET /observacion/convencional/todas` (finestra rodant ~12–24 h). El cron cada 2 h acumula l'arxiu de ~14 dies a `data/hourly/AEMET_*`.
- Només estacions amb observació convencional entren a la sèrie (típicament ~60–70 de les 87 del catàleg; la resta són climatològiques sense feed horari).

> **Secret:** `gh secret set AEMET_API_KEY -R jnoya99/mc-dades-acumulades`  
> Sol·licitud gratuïta a https://opendata.aemet.es/ (centre de descàrregues → API Key).  
> Sense secret, l'Action diària fa fallback al mirall HF; l'horària **falla** (no inventa sèries).


## XEMA horària (provisional fins al diari oficial)

El diari oficial XEMA (variables 1300, 1000, …) va ~2 dies tard. Aquesta captura
horària cobreix el forat sense inventar hores:

| Peça | Descripció |
|---|---|
| **Action `hourly-xema`** | Cron `35 * * * *` UTC + `workflow_dispatch` |
| **`scripts/capture_xema_hourly.py`** | `nzvn-apee` → `data/hourly/XEMA_YYYYMMDD_HH.json` |
| **Retenció** | ~14 dies (igual que ESCAT). El cron refresca els 3 últims dies. |
| **Clau** | Cap. Si Socrata cau, l'Action falla i no publica sèries buides. |

```bash
python3 scripts/capture_xema_hourly.py --days 14 --force
python3 scripts/capture_xema_hourly.py --rebuild-only
```

- IDs = `codi_estacio` (C6, …), els mateixos nodes de l'explorador.
- `data_lectura` és UTC; l'hora publicada és Europe/Madrid.
- `seriesW` / `seriesWA` van en **km/h** (la XEMA dona m/s; ×3.6), com el node diari.
- Quan el diari oficial d'un dia ja és al bloc, l'explorador fa servir aquell dia
  i no suma també el Ph horari (no es compta dues vegades). L'arxiu horari es queda.

## Explorador

```text
https://jnoya99.github.io/mc-dades-acumulades/panel.json
https://jnoya99.github.io/mc-dades-acumulades/hourly_rain.json
https://jnoya99.github.io/mc-dades-acumulades/hourly_meteo.json
https://jnoya99.github.io/mc-dades-acumulades/panel_mountain.json
https://jnoya99.github.io/mc-dades-acumulades/hourly_rain_mountain.json
https://jnoya99.github.io/mc-dades-acumulades/hourly_meteo_mountain.json
https://jnoya99.github.io/mc-dades-acumulades/panel_aemet.json
https://jnoya99.github.io/mc-dades-acumulades/hourly_rain_aemet.json
https://jnoya99.github.io/mc-dades-acumulades/hourly_meteo_aemet.json
```

```js
window.__MC_REMOTE_URL = "https://jnoya99.github.io/mc-dades-acumulades/panel.json";
window.__MC_HOURLY_URL = "https://jnoya99.github.io/mc-dades-acumulades/hourly_rain.json";
window.__MC_HOURLY_METEO_URL = "https://jnoya99.github.io/mc-dades-acumulades/hourly_meteo.json";
window.__AEMET_PANEL_URL = "https://jnoya99.github.io/mc-dades-acumulades/panel_aemet.json";
window.__AEMET_HOURLY_URL = "https://jnoya99.github.io/mc-dades-acumulades/hourly_rain_aemet.json";
window.__AEMET_HOURLY_METEO_URL = "https://jnoya99.github.io/mc-dades-acumulades/hourly_meteo_aemet.json";
window.__XEMA_HOURLY_URL = "https://jnoya99.github.io/mc-dades-acumulades/hourly_rain_xema.json";
window.__XEMA_HOURLY_METEO_URL = "https://jnoya99.github.io/mc-dades-acumulades/hourly_meteo_xema.json";
```

Vegeu [`docs/explorer-integration.md`](docs/explorer-integration.md).

## Llicència de dades

Dades Meteoclimatic: Creative Commons Attribution-NonCommercial-NoDerivs 3.0  
(vegeu el copyright del feed XML). Aquest repo és un arxiu tècnic per a ús amb l’explorador Bolets; respecteu la llicència CC del proveïdor.
