#!/usr/bin/env python3
"""Build daily hybrid snow layers for Catalonia → docs/snow/.

Sources:
  1. Copernicus GFSC (gap-filled fractional snow cover, 60 m) via CDSE
     Sentinel Hub Process API BYOC collection
     0b5265f5-3664-44c2-96ab-e91aba67b0c3 — aggregated to 0.01°.
  2. Open-Meteo archive snow_depth (daily max + hourly mean) on a 0.1° grid
     plus elevation.

Auth (never print): CDSE_CLIENT_ID + CDSE_CLIENT_SECRET.

Outputs (see docs/snow/README.md):
  docs/snow/YYYY-MM-DD.json
  docs/snow/latest.json
  optional prune of old dated files
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "snow"

# Catalonia bbox (inclusive cell centres derived from west/north + res)
GFSC_WEST, GFSC_SOUTH, GFSC_EAST, GFSC_NORTH = 0.15, 40.52, 3.35, 42.88
GFSC_RES = 0.01
OM_WEST, OM_SOUTH, OM_EAST, OM_NORTH = 0.20, 40.50, 3.30, 42.90
OM_RES = 0.10

GFSC_BYOC = "byoc-0b5265f5-3664-44c2-96ab-e91aba67b0c3"
CDSE_TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
    "protocol/openid-connect/token"
)
SH_PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"
OM_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"

NODATA_U8 = 255
NODATA_I16 = -32768

# GF_QA → approximate days since last clear / high-confidence obs (gap-fill window ≤7d)
QA_TO_AGE = {0: 0, 1: 2, 2: 4, 3: 7}

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


def _b64z(data: bytes) -> str:
    return base64.b64encode(zlib.compress(data, 9)).decode("ascii")


def _pack_u8(arr: list[int] | Any) -> str:
    if hasattr(arr, "tobytes"):
        raw = bytes(arr.astype("uint8").tobytes())
    else:
        raw = bytes(int(x) & 0xFF for x in arr)
    return _b64z(raw)


def _pack_i16(arr: list[int] | Any) -> str:
    if hasattr(arr, "astype"):
        raw = arr.astype("<i2").tobytes()
    else:
        raw = struct.pack("<" + "h" * len(arr), *[int(x) for x in arr])
    return _b64z(raw)


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


def fetch_gfsc(day: date, token: str) -> dict[str, Any]:
    import numpy as np
    import tifffile

    nx, ny = _grid_shape(GFSC_WEST, GFSC_SOUTH, GFSC_EAST, GFSC_NORTH, GFSC_RES)
    day_s = day.isoformat()
    payload = {
        "input": {
            "bounds": {
                "bbox": [GFSC_WEST, GFSC_SOUTH, GFSC_EAST, GFSC_NORTH],
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
            "width": nx,
            "height": ny,
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
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = r.read()
    _log(f"  GFSC TIFF {len(raw)} B in {time.time()-t0:.1f}s ({nx}x{ny})")
    from io import BytesIO
    arr = tifffile.imread(BytesIO(raw))
    # shape (ny, nx, 2)
    if arr.ndim == 2:
        raise RuntimeError(f"Unexpected GFSC TIFF shape {arr.shape}")
    if arr.shape[0] == 2 and arr.shape[-1] != 2:
        # (bands, y, x)
        gf = arr[0]
        qa = arr[1]
    else:
        gf = arr[..., 0]
        qa = arr[..., 1]
    if gf.shape != (ny, nx):
        _log(f"  WARN reshape {gf.shape} → {(ny, nx)}")
        # trust returned size
        ny, nx = int(gf.shape[0]), int(gf.shape[1])

    frac = np.full((ny, nx), NODATA_U8, dtype=np.uint8)
    age = np.full((ny, nx), NODATA_U8, dtype=np.uint8)
    valid = (gf >= 0) & (gf <= 100)
    frac[valid] = gf[valid]
    # map QA to age only where GF valid
    for qv, ad in QA_TO_AGE.items():
        m = valid & (qa == qv)
        age[m] = ad
    # if GF valid but QA odd, age=7
    age[valid & (age == NODATA_U8)] = 7

    snow_gt0 = int(((gf >= 1) & (gf <= 100)).sum())
    cloud_n = int((gf == 205).sum())
    water_n = int((gf == 210).sum())
    nodata_n = int((frac == NODATA_U8).sum())
    snow_vals = gf[(gf >= 1) & (gf <= 100)]
    return {
        "res_deg": GFSC_RES,
        "nx": nx,
        "ny": ny,
        "west": GFSC_WEST,
        "south": GFSC_SOUTH,
        "east": GFSC_EAST,
        "north": GFSC_NORTH,
        "nodata": NODATA_U8,
        "frac_b64z": _pack_u8(frac.ravel()),
        "age_b64z": _pack_u8(age.ravel()),
        "age_note": "approx days from GF_QA (0→0,1→2,2→4,3→7); gap-fill window ≤7d",
        "stats": {
            "valid": int(valid.sum()),
            "snow_gt0": snow_gt0,
            "zero": int((gf == 0).sum()),
            "cloud": cloud_n,
            "water": water_n,
            "nodata": nodata_n,
            "frac_mean_snow": float(snow_vals.mean()) if snow_gt0 else 0.0,
        },
        "_frac": frac,
        "_age": age,
    }


def _om_chunks(lats: list[float], lons: list[float], chunk: int = 80) -> list[tuple[list[float], list[float], list[int]]]:
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
    """Use forecast API past_days when archive quota is exhausted (recent dates only)."""
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
        last = None
        data = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(base + "?" + params, timeout=90) as r:
                    data = json.load(r)
                break
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(2 * (attempt + 1))
        if data is None:
            _log(f"  forecast chunk fail: {last}")
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
    _log(f"  OM via forecast {nx}x{ny} snow_gt0={sum(1 for v in valid_max if v > 0)}")
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
            lon = OM_WEST + (i + 0.5) * OM_RES
            lats.append(round(lat, 4))
            lons.append(round(lon, 4))

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
            except Exception as e:  # noqa: BLE001 — retry transient SSL/HTTP
                last = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"OM fetch failed after retries: {last}")

    # Pass 1: daily max + elevation (compact)
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
                depth_mean[gi] = cm  # default mean:=max until hourly pass
        time.sleep(0.05)

    # Pass 2: hourly → mean (smaller chunks)
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
    _log(f"  OM grid {nx}x{ny} snow_gt0={sum(1 for v in valid_max if v > 0)} max_cm={max(valid_max) if valid_max else 0}")
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
    # strip private arrays
    gfsc_pub = {k: v for k, v in gfsc.items() if not k.startswith("_")}
    om_pub = {k: v for k, v in om.items() if not k.startswith("_")}
    doc = {
        "version": 1,
        "date": day.isoformat(),
        "built": _utc_now(),
        "bbox": [GFSC_WEST, GFSC_SOUTH, GFSC_EAST, GFSC_NORTH],
        "sources": {
            "gfsc": {
                "product": "CLMS WSI GFSC Europe 60m daily v1",
                "byoc": "0b5265f5-3664-44c2-96ab-e91aba67b0c3",
                "api": "Sentinel Hub Process API (CDSE)",
            },
            "om": {
                "api": OM_ARCHIVE,
                "vars": ["snow_depth_max", "hourly snow_depth→mean", "elevation"],
            },
        },
        "gfsc": gfsc_pub,
        "om": om_pub,
    }
    return doc


def write_day(doc: dict[str, Any], out_dir: Path = OUT_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    day = doc["date"]
    path = out_dir / f"{day}.json"
    text = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")
    latest = out_dir / "latest.json"
    update_latest = True
    if latest.exists():
        try:
            prev = json.loads(latest.read_text(encoding="utf-8")).get("date")
            if prev and date.fromisoformat(day) < date.fromisoformat(prev):
                update_latest = False
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    if update_latest:
        latest.write_text(text, encoding="utf-8")
        _log(f"  wrote {path} ({len(text)} B) + latest.json")
    else:
        _log(f"  wrote {path} ({len(text)} B) (latest unchanged)")
    return path


def prune_old(
    out_dir: Path = OUT_DIR,
    keep_days: int = 400,
    protect: set[date] | None = None,
) -> int:
    """Remove dated JSON older than keep_days (keeps latest.json + README).

    Never deletes dates in ``protect`` (just-built / backfill targets).
    Default keep_days=400 covers a full winter season.
    """
    if not out_dir.exists():
        return 0
    cutoff = date.today() - timedelta(days=keep_days)
    protect = protect or set()
    removed = 0
    for p in sorted(out_dir.glob("????-??-??.json")):
        try:
            d = date.fromisoformat(p.stem)
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
        # default: yesterday UTC (GFSC often lags same-day)
        days.append(datetime.now(timezone.utc).date() - timedelta(days=1))
    # unique sorted
    return sorted(set(days))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="Single day YYYY-MM-DD")
    ap.add_argument("--start", help="Backfill start YYYY-MM-DD")
    ap.add_argument("--end", help="Backfill end YYYY-MM-DD")
    ap.add_argument("--keep-days", type=int, default=400, help="Prune dated files older than N days (default 400 ≈ winter+)")
    ap.add_argument("--no-prune", action="store_true")
    ap.add_argument("--skip-om", action="store_true", help="GFSC only (OM filled with nodata)")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    days = parse_days(args)
    token = cdse_token()
    _log(f"CDSE token OK; building {len(days)} day(s)")
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
