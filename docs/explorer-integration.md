# Integració amb Bolets Explorador

## URL públiques

Després d’activar GitHub Pages (branch `main` / carpeta `/docs`):

```text
https://jnoya99.github.io/mc-dades-acumulades/panel.json
https://jnoya99.github.io/mc-dades-acumulades/hourly_rain.json
https://jnoya99.github.io/mc-dades-acumulades/hourly_meteo.json
```

## Fetch en viu

```js
window.__MC_REMOTE_URL = "https://jnoya99.github.io/mc-dades-acumulades/panel.json";
window.__MC_HOURLY_URL = "https://jnoya99.github.io/mc-dades-acumulades/hourly_rain.json";
window.__MC_HOURLY_METEO_URL = "https://jnoya99.github.io/mc-dades-acumulades/hourly_meteo.json";
```

Després, en el boot / bridge MC:

```js
async function loadMcRemote() {
  const url = window.__MC_REMOTE_URL;
  if (!url) return null;
  const res = await fetch(url, { cache: "no-cache" });
  if (!res.ok) throw new Error("MC remote " + res.status);
  const pan = await res.json();
  window.__MC_PAYLOAD = pan;
  return pan;
}

async function loadMcHourly() {
  const url = window.__MC_HOURLY_URL;
  if (!url) return null;
  const res = await fetch(url, { cache: "no-cache" });
  if (!res.ok) throw new Error("MC hourly " + res.status);
  const h = await res.json();
  window.__MC_HOURLY = h;
  return h;
}

async function loadMcHourlyMeteo() {
  const url = window.__MC_HOURLY_METEO_URL;
  if (!url) return null;
  const res = await fetch(url, { cache: "no-cache" });
  if (!res.ok) throw new Error("MC hourly meteo " + res.status);
  const m = await res.json();
  window.__MC_HOURLY_METEO = m;
  return m;
}
```

### `panel.json` (diari)

Schema: `stations`, `days`, `series`, `checksum`, `version`, `built`, `ccaa`.  
Camps de sèrie diària: **P**, **TX**, **N**, **HX**, **HR**, **W**, **WDG**.

### `hourly_rain.json` (horari · pluja)

Schema:

- `version` — `1`
- `built` — ISO UTC
- `ccaa` — `"ESCAT"`
- `hours` — `["YYYY-MM-DDTHH:00", …]` (Europe/Madrid, hora sencera)
- `stations` — `[{id: MC_…, mc_id, name, lon, lat, elev}, …]`
- `series` — `{ station_id: { hour: Ph_mm } }`
- `delta_note` — text amb les regles de delta
- `retention_days` — finestra de raw (~14 dies)

**Ph (mm horaris)** es deriven del cumulatiu diari del XML (`rain/total`):

1. Mateix dia de calendari i cumulatiu no decreixent → `Ph = max(0, cum_ara − cum_prev)`.
2. Si el cumulatiu **baixa** (reset de mitjanit o de l’estació) → `Ph = max(0, cum_ara)`; s’ignora el cumulatiu anterior.
3. **Primer mostratge** d’una estació → `Ph = 0` (només baseline). No s’inventen hores anteriors a la primera captura.

La capa **Pluja acumulada** de l’explorador suma `Ph` de les hores amb dia de calendari dins `[d0, d1]` (inclusiu). Si hi ha dades horàries a la finestra, la cobertura (gris) es calcula amb completesa horària; si no, es cau al comportament diari del `panel.json`.

### `hourly_meteo.json` (horari · meteo)

Producte ampli: mateix `hours`/`stations` + **`seriesPh`** (idèntic a `hourly_rain.series`) i mapes snapshot hora→valor:

| Clau | Origen XML | Notes |
|---|---|---|
| `seriesPh` | delta de `rain/total` | Única sèrie amb deltas |
| `seriesHR` | Hum.act | Snapshot |
| `seriesHX` | Hum.max | Snapshot |
| `seriesHN` | Hum.min | Snapshot (si present) |
| `seriesW` | Vient.max | Snapshot |
| `seriesWDG` | Vient.dir | Snapshot |
| `seriesWA` | Vient.act | Snapshot (si present) |
| `seriesT` | Temp.act | “now” |
| `seriesTX` / `seriesTN` | Temp.max / Temp.min | Snapshot |

L’explorador desa el JSON a `window.__MC_HOURLY_METEO`. Els camps “ara” (HR/W/WDG/…) poden preferir l’últim snapshot horari del dia sense tocar la suma de Ph ni el panell diari.

## CORS

GitHub Pages serveix amb capçaleres CORS permissives per a GET estàtic.

## Actualització

| Action | Cron | Sortida |
|---|---|---|
| `daily-escat` | `30 21 * * *` UTC | `data/daily/` + `panel.json` |
| `hourly-escat` | `0 * * * *` UTC | `data/hourly/` + `hourly_rain.json` + `hourly_meteo.json` |

L’explorador veu hores/dies nous al proper reload.
