# bolets-mc-data

Arxiu diari públic d’**ESCAT (Meteoclimatic)** per al [Bolets Explorador](https://github.com/).  
Public daily ESCAT snapshot archive + GitHub Pages CDN for `panel.json`.

## Per què / Why

Meteoclimatic bloqueja `mapinfo/ESCAT?d=YYYYMMDD` per dies **passats** (401).  
Només funciona l’snapshot **actual** (`mapinfo/ESCAT` sense `?d=`, o feeds XML/RSS).

Per això cal **capturar cada dia** i publicar el panell acumulat a GitHub Pages.

## Què fa / What it does

| Peça | Descripció |
|---|---|
| **GitHub Action** | Cron `30 21 * * *` (21:30 UTC ≈ **23:30 Europe/Madrid a l’hivern**) |
| **`scripts/capture_escat.py`** | Baixa XML+RSS públics → `data/daily/ESCAT_YYYYMMDD.{json,csv}` + reconstrueix `docs/panel.json` |
| **`scripts/build_panel.py`** | Fusiona `stations_keep` + tots els dies → `docs/panel.json` |
| **GitHub Pages** | Serveix `docs/` (incl. `panel.json`) |

> **DST / horari:** el cron de GitHub Actions és sempre en UTC.
> - Hivern (CET, UTC+1): `21:30 UTC` → **22:30** Europe/Madrid  
> - Estiu (CEST, UTC+2): `21:30 UTC` → **23:30** Europe/Madrid  
> Objectiu nominal: ~23:30 Madrid. Amb un sol cron UTC no es pot clavar les dues estacions; `30 21 * * *` prioritzà l’estiu (més hores de llum / dia “tancat”). Es pot afegir un segon cron a l’hivern si cal.

## Ús local

```bash
cd bolets-mc-data
# reconstruir panell des de data/daily ja arxivats
python3 scripts/build_panel.py

# captura d’avui (Europe/Madrid) + rebuild panel
python3 scripts/capture_escat.py
python3 scripts/capture_escat.py --force   # reescriu el dia
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

## Explorador

L’HTML ha de fer fetch de:

```text
https://<user>.github.io/bolets-mc-data/panel.json
```

Vegeu [`docs/explorer-integration.md`](docs/explorer-integration.md).

## Llicència de dades

Dades Meteoclimatic: Creative Commons Attribution-NonCommercial-NoDerivs 3.0  
(vegeu el copyright del feed XML). Aquest repo és un arxiu tècnic per a ús amb l’explorador Bolets; respecteu la llicència CC del proveïdor.
