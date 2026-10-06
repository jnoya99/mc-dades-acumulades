# Snow layers (Catalonia)

Daily precomputed snow fields for **Bolets Explorador** (hybrid neu layer).
Published on GitHub Pages next to `panel.json`.

## URLs

| File | URL |
|------|-----|
| Latest | `https://jnoya99.github.io/mc-dades-acumulades/snow/latest.json` |
| Dated | `https://jnoya99.github.io/mc-dades-acumulades/snow/YYYY-MM-DD.json` |

Raw GitHub (fallback):  
`https://raw.githubusercontent.com/jnoya99/mc-dades-acumulades/main/docs/snow/latest.json`

## Sources

1. **Copernicus GFSC** — Gap-filled Fractional Snow Cover Europe 60 m (CLMS WSI), via CDSE Sentinel Hub Process API BYOC `0b5265f5-3664-44c2-96ab-e91aba67b0c3`. Aggregated to **0.01°**.
2. **Open-Meteo archive** — `snow_depth_max` (daily) + hourly `snow_depth` mean, plus DEM elevation, on a **0.1°** grid.

Built by `scripts/build_snow.py` (workflow `daily-snow.yml`, cron `17 7 * * *` UTC + `workflow_dispatch`).

## JSON schema (version 1)

```jsonc
{
  "version": 1,
  "date": "YYYY-MM-DD",
  "built": "ISO-8601 Z",
  "bbox": [west, south, east, north],   // GFSC extent
  "gfsc": {
    "res_deg": 0.01,
    "nx": 320, "ny": 236,
    "west": 0.15, "north": 42.88, "south": 40.52, "east": 3.35,
    "nodata": 255,
    "frac_b64z": "<zlib+base64 row-major uint8>",  // snow fraction 0–100
    "age_b64z":  "<zlib+base64 row-major uint8>",  // approx days (from GF_QA)
    "age_note": "…"
  },
  "om": {
    "res_deg": 0.1,
    "nx": 31, "ny": 24,
    "west": 0.20, "north": 42.90, …
    "nodata": -32768,
    "unit": "cm",
    "depth_max_cm_b64z":  "<zlib+base64 little-endian int16>",
    "depth_mean_cm_b64z": "<…>",
    "elev_m_b64z":        "<…>"
  }
}
```

### Grid indexing

- Arrays are **row-major**, row 0 = **north**.
- Cell centre:  
  `lon = west + (i + 0.5) * res`  
  `lat = north - (j + 0.5) * res`  
  `i ∈ [0, nx)`, `j ∈ [0, ny)`.

### Decode (JS sketch)

```js
async function b64zU8(s) {
  const u8 = Uint8Array.from(atob(s), c => c.charCodeAt(0));
  const ds = new DecompressionStream('deflate');
  // or pako.inflate — zlib wrapper; prefer pako.ungzip/inflate for raw zlib
  const ab = await new Response(new Blob([u8]).stream().pipeThrough(ds)).arrayBuffer();
  return new Uint8Array(ab);
}
```

Python: `zlib.decompress(base64.b64decode(s))` then `numpy.frombuffer(..., dtype=uint8|'<i2')`.

### Semantics for the app

1. Sample OM `depth_max_cm` (bilinear) → base depth.  
2. Add GNEUX residual / snap (in-app).  
3. If GFSC `frac` valid and **≈0** and age≤2 and no nearby station snow → force 0 (mask false snow).  
4. If GFSC nodata/cloud → do not mask.

## Size / retention

Target **≪ 300 KB/day** (zlib-packed). Dated files older than **400 days** (≈ full winter; just-built/backfill dates are never pruned) are pruned by the build script; `latest.json` always refreshed.
