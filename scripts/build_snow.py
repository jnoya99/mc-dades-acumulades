#!/usr/bin/env python3
"""Build daily hybrid snow layers for Catalonia → docs/snow/.

Sources:
  1. Copernicus GFSC (gap-filled fractional snow cover, 60 m) via CDSE
     Sentinel Hub Process API BYOC 0b5265f5-3664-44c2-96ab-e91aba67b0c3,
     sampled onto the Explorador favourability grid (__G):
       STEP=0.001°, MIN_LON=0.15, MIN_LAT=40.42, NLON=3271, NLAT=2442
     (~83×111 m cells). Process API is tiled (max ~2000 px/side).
  2. Open-Meteo archive snow_depth (daily max + hourly mean) on a 0.1° grid
     plus DEM elevation (coarse; app applies per-cell with real elev).

Auth (never print): CDSE_CLIENT_ID + CDSE_CLIENT_SECRET.

Outputs (see docs/snow/README.md):
  docs/snow/YYYY-MM-DD.json          — meta + OM + GFSC pointers/stats
  docs/snow/YYYY-MM-DD_frac.bin.gz   — NLAT×NLON uint8 snow fraction
  docs/snow/YYYY-MM-DD_age.bin.gz    — NLAT×NLON uint8 age/QA proxy
  docs/snow/latest.json (+ matching bins when recent)
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "snow"

# --- Favourability grid (__G defaults in Bolets_Explorador) ---
G_MIN_LON = 0.15
G_MIN_LAT = 40.42
G_STEP = 0.001
G_NLON = 3271
G_NLAT = 2442  # from max IDX // NLON + 1 in griddata

# OM coarse grid
OM_WEST, OM_SOUTH, OM_EAST, OM_NORTH = 0.20, 40.50, 3.30, 42.90
OM_RES = 0.10

GFSC_BYOC = "byoc-0b5265f5-3664-44c2-96ab-e91aba67b0c3"
CDSE_TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
    "protocol/openid-connect/token"
)
SH_PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"
OM_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"

# GF special codes (native) + our nodata
CODE_CLOUD = 205
CODE_WATER = 210
CODE_NODATA = 255

NODATA_I16 = -32768
QA_TO_AGE = {0: 0, 1: 2, 2: 4, 3: 7}
SH_TILE_MAX = 2000  # Process API safe max dimension

EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: [{bands: ["GF", "GF_QA"], units: "DN"}],
    output: { id: "default", bands: 2, sampleType: "UINT8" }
  };
}
function evaluatePixel(s) {
  return [s.GF, s.GF_QA];
}
"""


def _log(msg: str) -> None:
    print(msg, flush=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _grid_shape(west: float, south: float, east: float, north: float, res: float) -> tuple[int, int]:
    nx = int(round((east - west) / res))
    ny = int(round((north - south) / res))
    return nx, ny


def _pack_i16(arr: list[int] | Any) -> str:
    if hasattr(arr, "astype"):
        raw = arr.astype("<i2").tobytes()
    else:
        raw = struct.pack("<" + "h" * len(arr), *[int(x) for x in arr])
    return base64.b64encode(zlib.compress(raw, 9)).decode("ascii")


def cdse_token() -> str:
    cid = os.environ.get("CDSE_CLIENT_ID") or os.environ.get("OPENEO_AUTH_CLIENT_ID")
    csec = os.environ.get("CDSE_CLIENT_SECRET") or os.environ.get("OPENEO_AUTH_CLIENT_SECRET")
    if not cid or not csec:
        raise SystemExit(
            "Missing CDSE_CLIENT_ID / CDSE_CLIENT_SECRET "
            "(or OPENEO_AUTH_CLIENT_ID / OPENEO_AUTH_CLIENT_SECRET)"
        )
    body = urllib.parse.urlencode(
        {"grant_type": "client_credentials", "client_id": cid, "client_secret": csec}
    ).encode()
    req = urllib.request.Request(CDSE_TOKEN_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    tok = data.get("access_token")
    if not tok:
        raise RuntimeError("CDSE token response missing access_token")
    return tok


def _cell_bbox(i0: int, j0: int, w: int, h: int) -> list[float]:
    """BBox [west,south,east,north] for a tile of w×h cells starting at (i0,j0).

    Cell centres at MIN + k*STEP; pixel edges at centre ± STEP/2.
    j increases northward (same as app jj).
    """
    west = G_MIN_LON + i0 * G_STEP - G_STEP / 2
    south = G_MIN_LAT + j0 * G_STEP - G_STEP / 2
    east = west + w * G_STEP
    north = south + h * G_STEP
    return [west, south, east, north]


def _fetch_gfsc_tile(
    token: str, day: date, i0: int, j0: int, w: int, h: int
) -> tuple[Any, Any]:
    """Return (gf, qa) uint8 arrays shape (h, w), row 0 = south (app j)."""
    import numpy as np
    import tifffile

    day_s = day.isoformat()
    bbox = _cell_bbox(i0, j0, w, h)
    payload = {
        "input": {
            "bounds": {
                "bbox": bbox,
                "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
            },
            "data": [
                {
                    "type": GFSC_BYOC,
                    "dataFilter": {
                        "timeRange": {
                            "from": f"{day_s}T00:00:00Z",
                            "to": f"{day_s}T23:59:59Z",
                        }
                    },
                }
            ],
        },
        "output": {
            "width": w,
            "height": h,
            "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}],
        },
        "evalscript": EVALSCRIPT,
    }
    req = urllib.request.Request(
        SH_PROCESS_URL, data=json.dumps(payload).encode(), method="POST"
    )
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "image/tiff")
    last: Exception | None = None
    raw = b""
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                raw = r.read()
            break
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (attempt + 1))
            # refresh token body not needed for same token within 30 min
    else:
        raise RuntimeError(f"GFSC tile ({i0},{j0}) failed: {last}")

    arr = tifffile.imread(BytesIO(raw))
    if arr.ndim == 3 and arr.shape[-1] == 2:
        gf, qa = arr[..., 0], arr[..., 1]
    elif arr.ndim == 3 and arr.shape[0] == 2:
        gf, qa = arr[0], arr[1]
    else:
        raise RuntimeError(f"Unexpected GFSC tile shape {getattr(arr, 'shape', None)}")
    # Process API row 0 = north → flip to row 0 = south (app j)
    if gf.shape != (h, w):
        # allow minor mismatch
        gf = np.asarray(gf)
        qa = np.asarray(qa)
        if gf.shape[0] == h and gf.shape[1] == w:
            pass
        else:
            _log(f"  WARN tile shape {gf.shape} expected {(h, w)}")
    gf = np.flipud(gf)
    qa = np.flipud(qa)
    return gf.astype(np.uint8, copy=False), qa.astype(np.uint8, copy=False)


def fetch_gfsc(day: date, token: str) -> dict[str, Any]:
    import numpy as np

    nx, ny = G_NLON, G_NLAT
    frac = np.full((ny, nx), CODE_NODATA, dtype=np.uint8)
    age = np.full((ny, nx), CODE_NODATA, dtype=np.uint8)

    tiles_x = (nx + SH_TILE_MAX - 1) // SH_TILE_MAX
    tiles_y = (ny + SH_TILE_MAX - 1) // SH_TILE_MAX
    t0 = time.time()
    n_tiles = 0
    for ty in range(tiles_y):
        j0 = ty * SH_TILE_MAX
        h = min(SH_TILE_MAX, ny - j0)
        for tx in range(tiles_x):
            i0 = tx * SH_TILE_MAX
            w = min(SH_TILE_MAX, nx - i0)
            gf, qa = _fetch_gfsc_tile(token, day, i0, j0, w, h)
            # paste
            hh, ww = gf.shape
            frac[j0 : j0 + hh, i0 : i0 + ww] = gf[:hh, :ww]
            # age from QA where GF is a valid fraction
            # age only on snow pixels (frac 1–100); zeros/cloud/nodata → 255 (gzip-friendly)
            tile_age = np.full((hh, ww), CODE_NODATA, dtype=np.uint8)
            snow = (gf >= 1) & (gf <= 100)
            for qv, ad in QA_TO_AGE.items():
                tile_age[snow & (qa == qv)] = ad
            tile_age[snow & (tile_age == CODE_NODATA)] = 7
            age[j0 : j0 + hh, i0 : i0 + ww] = tile_age
            n_tiles += 1
            _log(f"  GFSC tile {n_tiles}/{tiles_x * tiles_y} i={i0} j={j0} {w}x{h}")
            time.sleep(0.3)

    snow_gt0 = int(((frac >= 1) & (frac <= 100)).sum())
    zero = int((frac == 0).sum())
    cloud = int((frac == CODE_CLOUD).sum())
    water = int((frac == CODE_WATER).sum())
    nodata = int((frac == CODE_NODATA).sum())
    valid = int((frac <= 100).sum())
    snow_vals = frac[(frac >= 1) & (frac <= 100)]
    _log(
        f"  GFSC {nx}x{ny} tiles={n_tiles} in {time.time()-t0:.1f}s "
        f"snow_gt0={snow_gt0} cloud={cloud} zlib≈?"
    )
    return {
        "grid": "favorability",
        "res_deg": G_STEP,
        "nx": nx,
        "ny": ny,
        "min_lon": G_MIN_LON,
        "min_lat": G_MIN_LAT,
        "step": G_STEP,
        "nlon": G_NLON,
        "nlat": G_NLAT,
        "codes": {
            "frac_0_100": "snow fraction %",
            "205": "cloud/shadow",
            "210": "inland water",
            "255": "nodata",
        },
        "age_note": "approx days from GF_QA on snow pixels only (frac 1–100); else 255",
        "frac_file": None,  # set in write_day
        "age_file": None,
        "stats": {
            "valid": valid,
            "snow_gt0": snow_gt0,
            "zero": zero,
            "cloud": cloud,
            "water": water,
            "nodata": nodata,
            "frac_mean_snow": float(snow_vals.mean()) if snow_gt0 else 0.0,
        },
        "_frac": frac,
        "_age": age,
    }


def _om_chunks(
    lats: list[float], lons: list[float], chunk: int = 80
) -> list[tuple[list[float], list[float], list[int]]]:
    out = []
    for i in range(0, len(lats), chunk):
        idx = list(range(i, min(i + chunk, len(lats))))
        out.append(([lats[j] for j in idx], [lons[j] for j in idx], idx))
    return out


def _om_empty(nx: int, ny: int, note: str) -> dict[str, Any]:
    n = nx * ny
    z = [NODATA_I16] * n
    return {
        "res_deg": OM_RES,
        "nx": nx,
        "ny": ny,
        "west": OM_WEST,
        "south": OM_SOUTH,
        "east": OM_EAST,
        "north": OM_NORTH,
        "nodata": NODATA_I16,
        "unit": "cm",
        "depth_max_cm_b64z": _pack_i16(z),
        "depth_mean_cm_b64z": _pack_i16(z),
        "elev_m_b64z": _pack_i16(z),
        "stats": {"points": n, "max_cm": 0, "mean_of_max_cm": 0, "snow_gt0": 0, "note": note},
        "_depth_max": z,
        "_depth_mean": z,
        "_elev": z,
    }


def fetch_om_via_forecast(day: date) -> dict[str, Any] | None:
    today = datetime.now(timezone.utc).date()
    lag = (today - day).days
    if lag < 0 or lag > 90:
        return None
    nx, ny = _grid_shape(OM_WEST, OM_SOUTH, OM_EAST, OM_NORTH, OM_RES)
    lats: list[float] = []
    lons: list[float] = []
    for j in range(ny):
        lat = OM_NORTH - (j + 0.5) * OM_RES
        for i in range(nx):
            lats.append(round(lat, 4))
            lons.append(round(OM_WEST + (i + 0.5) * OM_RES, 4))
    depth_max = [NODATA_I16] * (nx * ny)
    depth_mean = [NODATA_I16] * (nx * ny)
    elev = [NODATA_I16] * (nx * ny)
    day_s = day.isoformat()
    base = "https://api.open-meteo.com/v1/forecast"
    for clats, clons, idxs in _om_chunks(lats, lons, 80):
        params = urllib.parse.urlencode(
            {
                "latitude": ",".join(str(x) for x in clats),
                "longitude": ",".join(str(x) for x in clons),
                "start_date": day_s,
                "end_date": day_s,
                "daily": "snow_depth_max,snowfall_sum",
                "hourly": "snow_depth",
                "timezone": "UTC",
            }
        )
        data = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(base + "?" + params, timeout=90) as r:
                    data = json.load(r)
                break
            except Exception:  # noqa: BLE001
                time.sleep(2 * (attempt + 1))
        if data is None:
            continue
        if isinstance(data, dict):
            data = [data]
        for local_i, item in enumerate(data):
            gi = idxs[local_i]
            el = item.get("elevation")
            if el is not None:
                elev[gi] = int(round(float(el)))
            daily = item.get("daily") or {}
            mx = (daily.get("snow_depth_max") or [None])[0]
            if mx is not None:
                cm = int(round(float(mx) * 100.0))
                depth_max[gi] = cm
            hourly = (item.get("hourly") or {}).get("snow_depth") or []
            vals = [float(v) for v in hourly if v is not None]
            if vals:
                depth_mean[gi] = int(round(sum(vals) / len(vals) * 100.0))
            elif mx is not None:
                depth_mean[gi] = depth_max[gi]
        time.sleep(0.2)
    valid_max = [v for v in depth_max if v != NODATA_I16]
    if not valid_max:
        return None
    return {
        "res_deg": OM_RES,
        "nx": nx,
        "ny": ny,
        "west": OM_WEST,
        "south": OM_SOUTH,
        "east": OM_EAST,
        "north": OM_NORTH,
        "nodata": NODATA_I16,
        "unit": "cm",
        "source": "forecast_api",
        "depth_max_cm_b64z": _pack_i16(depth_max),
        "depth_mean_cm_b64z": _pack_i16(depth_mean),
        "elev_m_b64z": _pack_i16(elev),
        "stats": {
            "points": nx * ny,
            "max_cm": max(valid_max),
            "mean_of_max_cm": round(sum(valid_max) / len(valid_max), 2),
            "snow_gt0": sum(1 for v in valid_max if v > 0),
        },
        "_depth_max": depth_max,
        "_depth_mean": depth_mean,
        "_elev": elev,
    }


def fetch_om(day: date) -> dict[str, Any]:
    nx, ny = _grid_shape(OM_WEST, OM_SOUTH, OM_EAST, OM_NORTH, OM_RES)
    lats: list[float] = []
    lons: list[float] = []
    for j in range(ny):
        lat = OM_NORTH - (j + 0.5) * OM_RES
        for i in range(nx):
            lats.append(round(lat, 4))
            lons.append(round(OM_WEST + (i + 0.5) * OM_RES, 4))

    depth_max = [NODATA_I16] * (nx * ny)
    depth_mean = [NODATA_I16] * (nx * ny)
    elev = [NODATA_I16] * (nx * ny)
    day_s = day.isoformat()

    def _get(url: str) -> Any:
        last: Exception | None = None
        for attempt in range(6):
            try:
                with urllib.request.urlopen(url, timeout=90) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                last = e
                body = e.read().decode("utf-8", "replace") if e.fp else ""
                if e.code == 429 and "Daily" in body:
                    raise RuntimeError(f"OM daily limit: {body[:120]}") from e
                wait = 20 * (attempt + 1) if e.code == 429 else 1.5 * (attempt + 1)
                time.sleep(wait)
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"OM fetch failed after retries: {last}")

    for clats, clons, idxs in _om_chunks(lats, lons, 100):
        params = urllib.parse.urlencode(
            {
                "latitude": ",".join(str(x) for x in clats),
                "longitude": ",".join(str(x) for x in clons),
                "start_date": day_s,
                "end_date": day_s,
                "daily": "snow_depth_max,snowfall_sum",
                "timezone": "UTC",
            }
        )
        data = _get(OM_ARCHIVE + "?" + params)
        if isinstance(data, dict):
            data = [data]
        for local_i, item in enumerate(data):
            gi = idxs[local_i]
            el = item.get("elevation")
            if el is not None:
                elev[gi] = int(round(float(el)))
            daily = item.get("daily") or {}
            mx = (daily.get("snow_depth_max") or [None])[0]
            if mx is not None:
                cm = int(round(float(mx) * 100.0))
                depth_max[gi] = cm
                depth_mean[gi] = cm
        time.sleep(0.05)

    for clats, clons, idxs in _om_chunks(lats, lons, 40):
        params = urllib.parse.urlencode(
            {
                "latitude": ",".join(str(x) for x in clats),
                "longitude": ",".join(str(x) for x in clons),
                "start_date": day_s,
                "end_date": day_s,
                "hourly": "snow_depth",
                "timezone": "UTC",
            }
        )
        try:
            data = _get(OM_ARCHIVE + "?" + params)
        except Exception as e:  # noqa: BLE001
            _log(f"  OM hourly chunk skip: {type(e).__name__}")
            continue
        if isinstance(data, dict):
            data = [data]
        for local_i, item in enumerate(data):
            gi = idxs[local_i]
            hourly = (item.get("hourly") or {}).get("snow_depth") or []
            vals = [float(v) for v in hourly if v is not None]
            if vals:
                depth_mean[gi] = int(round(sum(vals) / len(vals) * 100.0))
        time.sleep(0.05)

    valid_max = [v for v in depth_max if v != NODATA_I16]
    _log(
        f"  OM grid {nx}x{ny} snow_gt0={sum(1 for v in valid_max if v > 0)} "
        f"max_cm={max(valid_max) if valid_max else 0}"
    )
    return {
        "res_deg": OM_RES,
        "nx": nx,
        "ny": ny,
        "west": OM_WEST,
        "south": OM_SOUTH,
        "east": OM_EAST,
        "north": OM_NORTH,
        "nodata": NODATA_I16,
        "unit": "cm",
        "depth_max_cm_b64z": _pack_i16(depth_max),
        "depth_mean_cm_b64z": _pack_i16(depth_mean),
        "elev_m_b64z": _pack_i16(elev),
        "stats": {
            "points": nx * ny,
            "max_cm": max(valid_max) if valid_max else 0,
            "mean_of_max_cm": round(sum(valid_max) / len(valid_max), 2) if valid_max else 0,
            "snow_gt0": sum(1 for v in valid_max if v > 0),
        },
        "_depth_max": depth_max,
        "_depth_mean": depth_mean,
        "_elev": elev,
    }


def build_day(day: date, token: str | None = None, skip_om: bool = False) -> dict[str, Any]:
    _log(f"Building snow for {day.isoformat()}")
    tok = token or cdse_token()
    gfsc = fetch_gfsc(day, tok)
    nx, ny = _grid_shape(OM_WEST, OM_SOUTH, OM_EAST, OM_NORTH, OM_RES)
    if skip_om:
        om = _om_empty(nx, ny, "skipped")
    else:
        try:
            om = fetch_om(day)
        except Exception as e:
            _log(f"  OM archive failed ({type(e).__name__}: {e}); trying forecast fallback")
            om = fetch_om_via_forecast(day)
            if om is None:
                raise
    gfsc_pub = {k: v for k, v in gfsc.items() if not k.startswith("_")}
    om_pub = {k: v for k, v in om.items() if not k.startswith("_")}
    return {
        "version": 2,
        "date": day.isoformat(),
        "built": _utc_now(),
        "bbox": [
            G_MIN_LON - G_STEP / 2,
            G_MIN_LAT - G_STEP / 2,
            G_MIN_LON + (G_NLON - 0.5) * G_STEP,
            G_MIN_LAT + (G_NLAT - 0.5) * G_STEP,
        ],
        "sources": {
            "gfsc": {
                "product": "CLMS WSI GFSC Europe 60m daily v1",
                "byoc": "0b5265f5-3664-44c2-96ab-e91aba67b0c3",
                "api": "Sentinel Hub Process API (CDSE), tiled → fav grid 0.001°",
            },
            "om": {
                "api": OM_ARCHIVE,
                "vars": ["snow_depth_max", "hourly snow_depth→mean", "elevation"],
            },
        },
        "gfsc": gfsc_pub,
        "om": om_pub,
        "_frac": gfsc["_frac"],
        "_age": gfsc["_age"],
    }


def _write_gz(path: Path, raw: bytes) -> int:
    with gzip.open(path, "wb", compresslevel=9) as f:
        f.write(raw)
    return path.stat().st_size


def write_day(doc: dict[str, Any], out_dir: Path = OUT_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    day = doc["date"]
    frac = doc.pop("_frac")
    age = doc.pop("_age")
    frac_name = f"{day}_frac.bin.gz"
    age_name = f"{day}_age.bin.gz"
    frac_path = out_dir / frac_name
    age_path = out_dir / age_name
    sz_f = _write_gz(frac_path, frac.tobytes())
    sz_a = _write_gz(age_path, age.tobytes())
    doc["gfsc"]["frac_file"] = frac_name
    doc["gfsc"]["age_file"] = age_name
    doc["gfsc"]["encoding"] = "gzip_uint8_rowmajor_j0_south"
    doc["gfsc"]["nbytes_raw"] = int(frac.size)
    doc["gfsc"]["nbytes_gz"] = {"frac": sz_f, "age": sz_a}
    _log(f"  bins frac.gz={sz_f} B age.gz={sz_a} B (raw {frac.size} cells)")

    path = out_dir / f"{day}.json"
    text = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")

    latest = out_dir / "latest.json"
    day_d = date.fromisoformat(day)
    today = datetime.now(timezone.utc).date()
    update_latest = day_d >= (today - timedelta(days=14))
    if update_latest and latest.exists():
        try:
            prev = json.loads(latest.read_text(encoding="utf-8")).get("date")
            if prev and day_d < date.fromisoformat(prev):
                update_latest = False
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    if update_latest:
        latest.write_text(text, encoding="utf-8")
        # copy bins as latest_* for stable URLs
        (out_dir / "latest_frac.bin.gz").write_bytes(frac_path.read_bytes())
        (out_dir / "latest_age.bin.gz").write_bytes(age_path.read_bytes())
        # rewrite latest json to point at latest_* names
        latest_doc = json.loads(text)
        latest_doc["gfsc"]["frac_file"] = "latest_frac.bin.gz"
        latest_doc["gfsc"]["age_file"] = "latest_age.bin.gz"
        latest.write_text(
            json.dumps(latest_doc, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        _log(f"  wrote {path} ({len(text)} B) + latest.json + latest_*.bin.gz")
    else:
        _log(f"  wrote {path} ({len(text)} B) (latest unchanged)")
    return path


def prune_old(
    out_dir: Path = OUT_DIR,
    keep_days: int = 730,
    protect: set[date] | None = None,
) -> int:
    if not out_dir.exists():
        return 0
    cutoff = date.today() - timedelta(days=keep_days)
    protect = protect or set()
    removed = 0
    for p in sorted(out_dir.glob("????-??-??*")):
        stem = p.name.split("_")[0].replace(".json", "")
        try:
            d = date.fromisoformat(stem)
        except ValueError:
            continue
        if d in protect:
            continue
        if d < cutoff:
            p.unlink()
            removed += 1
            _log(f"  pruned {p.name}")
    return removed


def parse_days(args: argparse.Namespace) -> list[date]:
    days: list[date] = []
    if args.date:
        days.append(date.fromisoformat(args.date))
    if args.start and args.end:
        d0 = date.fromisoformat(args.start)
        d1 = date.fromisoformat(args.end)
        if d1 < d0:
            raise SystemExit("--end before --start")
        d = d0
        while d <= d1:
            days.append(d)
            d += timedelta(days=1)
    if not days:
        days.append(datetime.now(timezone.utc).date() - timedelta(days=1))
    return sorted(set(days))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="Single day YYYY-MM-DD")
    ap.add_argument("--start", help="Backfill start YYYY-MM-DD")
    ap.add_argument("--end", help="Backfill end YYYY-MM-DD")
    ap.add_argument(
        "--keep-days",
        type=int,
        default=730,
        help="Prune dated files older than N days (default 730 ≈ 2 winters)",
    )
    ap.add_argument("--no-prune", action="store_true")
    ap.add_argument("--skip-om", action="store_true", help="GFSC only (OM filled with nodata)")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    days = parse_days(args)
    token = cdse_token()
    _log(f"CDSE token OK; building {len(days)} day(s) @ fav grid {G_NLON}x{G_NLAT}")
    written: set[date] = set()
    for d in days:
        try:
            doc = build_day(d, token=token, skip_om=args.skip_om)
            write_day(doc, args.out_dir)
            written.add(d)
        except Exception as e:
            _log(f"ERROR {d.isoformat()}: {type(e).__name__}: {e}")
            if len(days) == 1:
                raise
            continue
    if not args.no_prune:
        prune_old(args.out_dir, args.keep_days, protect=written | set(days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
