# Snow layers (Catalonia) — favourability grid

Daily precomputed snow for **Bolets Explorador** hybrid neu / **favorability** mask.
Published on GitHub Pages next to `panel.json`.

## URLs

| File | URL |
|------|-----|
| Latest meta | `https://jnoya99.github.io/mc-dades-acumulades/snow/latest.json` |
| Latest frac | `https://jnoya99.github.io/mc-dades-acumulades/snow/latest_frac.bin.gz` |
| Latest age | `https://jnoya99.github.io/mc-dades-acumulades/snow/latest_age.bin.gz` |
| Dated meta | `…/snow/YYYY-MM-DD.json` |
| Dated frac | `…/snow/YYYY-MM-DD_frac.bin.gz` |
| Dated age | `…/snow/YYYY-MM-DD_age.bin.gz` |

Raw GitHub fallback: `https://raw.githubusercontent.com/jnoya99/mc-dades-acumulades/main/docs/snow/…`

## Grid (matches app `__G`)

| | |
|--|--|
| `STEP` | **0.001°** (~83 m E–W × ~111 m N–S) |
| `MIN_LON` / `MIN_LAT` | **0.15** / **40.42** |
| `NLON` × `NLAT` | **3271 × 2442** |
| Indexing | `i = round((lon−MIN_LON)/STEP)`, `j = round((lat−MIN_LAT)/STEP)` |
| Raster layout | row-major, **j=0 = south**, `offset = j*NLON + i` |

Cell centre: `lon = MIN_LON + i*STEP`, `lat = MIN_LAT + j*STEP`.

## Sources

1. **Copernicus GFSC** 60 m (CLMS WSI) via CDSE Sentinel Hub Process API BYOC `0b5265f5-3664-44c2-96ab-e91aba67b0c3`, **tiled** and written **directly at 0.001°** (no 0.01° aggregate).
2. **Open-Meteo archive** coarse **0.1°** `snow_depth` max/mean + DEM elevation (app applies OM with real cell elev).

Built by `scripts/build_snow.py` · workflow `daily-snow.yml` (cron `17 7 * * *` UTC + `workflow_dispatch`).

## Files / encoding (version 2)

### `YYYY-MM-DD_frac.bin.gz` / `_age.bin.gz`

- Raw **uint8** array of length `NLON*NLAT` (= 7 987 782), gzip-compressed.
- **frac codes:** `0–100` = snow fraction %; `205` = cloud/shadow; `210` = inland water; `255` = nodata.
- **age:** approx days from GF_QA **on snow pixels only** (`frac` 1–100): `0→0`, `1→2`, `2→4`, `3→7`; elsewhere `255`.

### `YYYY-MM-DD.json`

Meta + OM pack (zlib+base64 little-endian int16 for depth_max/mean_cm and elev_m on 0.1° grid) + `gfsc.frac_file` / `age_file` names + stats.

`latest.json` only advances for products ≤14 days old (winter backfills do not steal `latest`).

## Decode (JS)

```js
async function loadFavSnow(base /* .../snow/2025-01-11 */) {
  const meta = await (await fetch(base + '.json')).json();
  const gz = await (await fetch(base + '_frac.bin.gz')).arrayBuffer();
  const ds = new DecompressionStream('gzip');
  const buf = await new Response(new Blob([gz]).stream().pipeThrough(ds)).arrayBuffer();
  const frac = new Uint8Array(buf); // length nlon*nlat
  const {min_lon: ml, min_lat: mt, step, nlon} = meta.gfsc;
  function at(lon, lat) {
    const i = Math.round((lon - ml) / step), j = Math.round((lat - mt) / step);
    if (i < 0 || j < 0 || i >= nlon || j >= meta.gfsc.nlat) return 255;
    return frac[j * nlon + i];
  }
  return {meta, frac, at};
}
```

## App usage (favorability)

1. Sample GFSC `frac` on `__G` cell `(i,j)`.
2. OM depth bilinear from coarse grid; correct with GNEUX residual + snap.
3. If `frac` valid and **&lt;5** and no nearby station snow → force depth 0 (mask).
4. Cloud/nodata (`205`/`255`) → do not mask.

## Size / retention

Target **a few hundred KB/day** (gzip of sparse uint8). Dated files older than **400 days** pruned; just-built/backfill dates never pruned.
