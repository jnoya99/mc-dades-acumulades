#!/usr/bin/env python3
"""Capture AEMET daily climatology for Catalonia stations → panel_aemet.json.

Stations: data/stations_aemet.json (AE_<indicativo>, 87 CAT stations matching
Bolets Explorador AEMET_ST).

Sources (first that works):
  1. AEMET OpenData API — needs env AEMET_API_KEY or AEMET_API_TOKEN
     (also accepted as GitHub Actions secret AEMET_API_KEY).
  2. HuggingFace mirror datania/aemet — no key; lags behind OpenData
     (valores-climatologicos/YYYY/MM/DD.json). Use --source hf to force.

Writes:
  data/daily/AEMET_YYYYMMDD.json   (raw filtered rows per day)
  docs/panel_aemet.json            (schema spirit of panel_mountain.json)

Series cell fields (same as ESCAT/mountain): P, TX, N, HX, HR, W, WDG.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from http_util import http_get  # noqa: E402

MADRID = ZoneInfo("Europe/Madrid")
UA = "mc-dades-acumulades/1.0 (+https://github.com/jnoya99/mc-dades-acumulades; aemet)"
CCAA = "AEMET-CAT"
PANEL_VERSION = 1
KEEP_DAYS = 31
MANIFEST_NAME = "stations_aemet.json"

AEMET_API_BASE = "https://opendata.aemet.es/opendata/api"
HF_BASE = (
    "https://huggingface.co/datasets/datania/aemet/resolve/main/"
    "valores-climatologicos"
)

# AEMET text codes for precip / missing
PREC_TRACE = {"Ip", "Ip.", "IP"}
PREC_SKIP = {"Acum", "Varias", "-", ""}


def _api_key() -> str | None:
    for name in ("AEMET_API_KEY", "AEMET_API_TOKEN", "AEMET_API_KEY_TOKEN"):
        v = (os.environ.get(name) or "").strip()
        if v:
            return v
    return None


def _num(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        if v != v:  # NaN
            return None
        return float(v)
    s = str(v).strip()
    if not s or s in PREC_SKIP:
        return None
    if s in PREC_TRACE:
        return 0.0
    s = s.replace(",", ".")
    # strip units leftovers
    s = s.replace("mm", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _json_num(v: float | None) -> int | float | None:
    if v is None:
        return None
    if v != v:
        return None
    if float(v) == int(v) and abs(v) < 1e15:
        return int(v)
    return round(float(v), 2)


def load_stations(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"Expected list in {path}")
    out: list[dict[str, Any]] = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        sid = (r.get("id") or r.get("mc_id") or "").strip()
        ind = (r.get("indicativo") or "").strip()
        if not sid and ind:
            sid = f"AE_{ind}"
        if not ind and sid.startswith("AE_"):
            ind = sid[3:]
        if not sid or not ind:
            continue
        out.append(
            {
                "id": sid,
                "mc_id": r.get("mc_id") or sid,
                "name": (r.get("name") or r.get("nombre") or sid).strip(),
                "lon": _json_num(_num(r.get("lon"))),
                "lat": _json_num(_num(r.get("lat"))),
                "elev": _json_num(_num(r.get("elev") if r.get("elev") is not None else r.get("altitud"))),
                "source": r.get("source") or "AEMET",
                "indicativo": ind,
                "provincia": (r.get("provincia") or "").strip() or None,
            }
        )
    out.sort(key=lambda x: x["id"])
    return out


def cell_from_aemet_row(r: dict[str, Any]) -> dict[str, Any] | None:
    """Map AEMET daily climatology fields → panel cell."""
    cell = {
        "P": _json_num(_num(r.get("prec"))),
        "TX": _json_num(_num(r.get("tmax"))),
        "N": _json_num(_num(r.get("tmin"))),
        "HX": _json_num(_num(r.get("hrMax"))),
        "HR": _json_num(_num(r.get("hrMedia") if r.get("hrMedia") is not None else r.get("hrMin"))),
        "W": _json_num(_num(r.get("racha") if r.get("racha") is not None else r.get("velmedia"))),
        "WDG": _json_num(_num(r.get("dir"))),
    }
    if all(v is None for v in cell.values()):
        return None
    return cell


def _aemet_get_json(path: str, api_key: str) -> Any:
    """Two-step OpenData GET: meta → datos URL → JSON body."""
    url = f"{AEMET_API_BASE}/{path.lstrip('/')}"
    # Prefer query param (browser-friendly) + header
    sep = "&" if "?" in url else "?"
    url_q = f"{url}{sep}{urllib.parse.urlencode({'api_key': api_key})}"
    meta_raw = http_get(
        url_q,
        timeout=90,
        user_agent=UA,
        headers={
            "Accept": "application/json",
            "api_key": api_key,
        },
    )
    meta = json.loads(meta_raw.decode("utf-8", errors="replace"))
    if not isinstance(meta, dict):
        raise RuntimeError(f"Unexpected AEMET meta type: {type(meta)}")
    estado = meta.get("estado")
    if estado not in (200, "200", None):
        # 200 in datos hop; estado 404/429 etc.
        desc = meta.get("descripcion") or meta
        if int(estado or 0) == 404:
            return []
        raise RuntimeError(f"AEMET API estado={estado}: {desc}")
    datos_url = meta.get("datos")
    if not datos_url:
        # Some responses embed data directly
        if isinstance(meta.get("datos"), list):
            return meta["datos"]
        raise RuntimeError(f"AEMET meta missing datos URL: {meta}")
    # Second hop — datos URLs are short-lived; often no key needed
    body = http_get(
        str(datos_url),
        timeout=120,
        user_agent=UA,
        headers={"Accept": "application/json"},
    )
    return json.loads(body.decode("utf-8", errors="replace"))


def fetch_aemet_range(
    indicativos: list[str],
    start: date,
    end: date,
    api_key: str,
    *,
    chunk_stations: int = 20,
    pause_s: float = 1.3,
) -> list[dict[str, Any]]:
    """Fetch daily climatology for stations in [start, end] (inclusive).

    AEMET allows comma-separated estaciones; we chunk to keep URLs short
    and respect ~50 req/min (2 hops each).
    """
    if start > end:
        return []
    fechaini = start.strftime("%Y-%m-%dT00:00:00UTC")
    fechafin = end.strftime("%Y-%m-%dT00:00:00UTC")
    all_rows: list[dict[str, Any]] = []
    for i in range(0, len(indicativos), chunk_stations):
        chunk = indicativos[i : i + chunk_stations]
        ids = ",".join(chunk)
        path = (
            "valores/climatologicos/diarios/datos/"
            f"fechaini/{fechaini}/fechafin/{fechafin}/estacion/{ids}"
        )
        try:
            data = _aemet_get_json(path, api_key)
        except RuntimeError as e:
            msg = str(e)
            # Empty / no data for chunk
            if "404" in msg or "NO HAY DATOS" in msg.upper() or "no hay datos" in msg.lower():
                print(f"[aemet] no data chunk {chunk[0]}… ({len(chunk)})", flush=True)
                time.sleep(pause_s)
                continue
            raise
        if isinstance(data, list):
            all_rows.extend(r for r in data if isinstance(r, dict))
        elif isinstance(data, dict):
            all_rows.append(data)
        time.sleep(pause_s)
    return all_rows


def fetch_hf_day(day: date) -> list[dict[str, Any]] | None:
    url = f"{HF_BASE}/{day.year:04d}/{day.month:02d}/{day.day:02d}.json"
    try:
        raw = http_get(
            url,
            timeout=90,
            user_agent=UA,
            headers={"Accept": "application/json"},
            max_attempts=3,
        )
    except urllib.error.HTTPError as e:
        if int(getattr(e, "code", 0) or 0) == 404:
            return None
        raise
    data = json.loads(raw.decode("utf-8", errors="replace"))
    if not isinstance(data, list):
        return None
    return [r for r in data if isinstance(r, dict)]


def filter_rows_for_stations(
    rows: list[dict[str, Any]],
    by_ind: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """indicativo → merged row with station id fields."""
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        ind = str(r.get("indicativo") or "").strip()
        if ind not in by_ind:
            continue
        st = by_ind[ind]
        out[ind] = {
            **r,
            "id": st["id"],
            "mc_id": st["mc_id"],
            "station_name": st["name"],
            "lon": st["lon"],
            "lat": st["lat"],
            "elev": st["elev"],
        }
    return out


def write_daily_raw(
    daily_dir: Path,
    day: date,
    rows_by_ind: dict[str, dict[str, Any]],
    *,
    source: str,
) -> Path:
    daily_dir.mkdir(parents=True, exist_ok=True)
    iso = day.isoformat()
    ymd = day.strftime("%Y%m%d")
    stations = []
    for ind, r in sorted(rows_by_ind.items()):
        cell = cell_from_aemet_row(r)
        stations.append(
            {
                "id": r.get("id") or f"AE_{ind}",
                "indicativo": ind,
                "name": r.get("station_name") or r.get("nombre") or ind,
                "lon": r.get("lon"),
                "lat": r.get("lat"),
                "elev": r.get("elev"),
                "provincia": r.get("provincia"),
                "prec": r.get("prec"),
                "tmax": r.get("tmax"),
                "tmin": r.get("tmin"),
                "tmed": r.get("tmed"),
                "hrMedia": r.get("hrMedia"),
                "hrMax": r.get("hrMax"),
                "hrMin": r.get("hrMin"),
                "velmedia": r.get("velmedia"),
                "racha": r.get("racha"),
                "dir": r.get("dir"),
                "cell": cell,
            }
        )
    payload = {
        "ccaa": CCAA,
        "mode": "daily",
        "date": iso,
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "n_stations": len(stations),
        "stations": stations,
    }
    path = daily_dir / f"AEMET_{ymd}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def list_daily_aemet_files(daily_dir: Path) -> list[tuple[str, Path]]:
    if not daily_dir.is_dir():
        return []
    out: list[tuple[str, Path]] = []
    for path in sorted(daily_dir.glob("AEMET_????????.json")):
        stem = path.stem  # AEMET_YYYYMMDD
        if len(stem) != 14:
            continue
        ymd = stem[6:]
        if not ymd.isdigit():
            continue
        out.append((f"{ymd[0:4]}-{ymd[4:6]}-{ymd[6:8]}", path))
    return out


def prune_daily(daily_dir: Path, keep_days: int = KEEP_DAYS) -> int:
    files = list_daily_aemet_files(daily_dir)
    if len(files) <= keep_days:
        return 0
    # keep the most recent keep_days
    to_drop = files[:-keep_days] if keep_days > 0 else files
    deleted = 0
    for _, path in to_drop:
        path.unlink(missing_ok=True)
        deleted += 1
    return deleted


def build_panel_aemet(
    stations_meta: list[dict[str, Any]],
    daily_dir: Path,
    *,
    source_note: str,
) -> dict[str, Any]:
    known = {st["id"]: st for st in stations_meta if st.get("id")}
    series: dict[str, dict[str, dict[str, Any]]] = {sid: {} for sid in known}
    days: set[str] = set()

    for iso, path in list_daily_aemet_files(daily_dir):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        st_list = obj.get("stations") if isinstance(obj, dict) else None
        if not isinstance(st_list, list):
            continue
        hit = False
        for r in st_list:
            if not isinstance(r, dict):
                continue
            sid = (r.get("id") or "").strip()
            if sid not in series:
                continue
            cell = r.get("cell") if isinstance(r.get("cell"), dict) else cell_from_aemet_row(r)
            if not cell:
                continue
            series[sid][iso] = cell
            hit = True
        if hit:
            days.add(iso)

    series = {sid: byday for sid, byday in series.items() if byday}
    built = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    day_list = sorted(days)
    return {
        "version": PANEL_VERSION,
        "built": built,
        "ccaa": CCAA,
        "stations": stations_meta,
        "days": day_list,
        "series": series,
        "sources": ["AEMET OpenData"],
        "note": source_note,
        "n_stations_catalog": len(stations_meta),
        "n_stations_with_series": len(series),
        "n_days": len(day_list),
    }


def daterange(start: date, end: date) -> list[date]:
    out = []
    d = start
    while d <= end:
        out.append(d)
        d += timedelta(days=1)
    return out


def capture_from_api(
    stations: list[dict[str, Any]],
    daily_dir: Path,
    *,
    start: date,
    end: date,
    api_key: str,
    force: bool,
) -> tuple[int, str]:
    by_ind = {st["indicativo"]: st for st in stations}
    inds = list(by_ind.keys())
    # Skip days already on disk unless --force
    needed = []
    for d in daterange(start, end):
        path = daily_dir / f"AEMET_{d.strftime('%Y%m%d')}.json"
        if path.exists() and not force:
            continue
        needed.append(d)
    if not needed:
        return 0, "api (all days cached)"

    # Fetch whole window in one go per station chunk (API allows up to ~31 days)
    rows = fetch_aemet_range(inds, needed[0], needed[-1], api_key)
    # Bucket by fecha
    by_day: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        fe = str(r.get("fecha") or "")[:10]
        if not fe:
            continue
        by_day.setdefault(fe, []).append(r)

    written = 0
    for d in needed:
        iso = d.isoformat()
        day_rows = by_day.get(iso, [])
        filtered = filter_rows_for_stations(day_rows, by_ind)
        if not filtered and not force:
            # still write empty? skip — no invent
            print(f"[aemet] skip empty {iso}", flush=True)
            continue
        write_daily_raw(daily_dir, d, filtered, source="aemet-opendata")
        written += 1
        print(f"[aemet] wrote {iso} n={len(filtered)} (api)", flush=True)
    return written, "aemet-opendata"


def capture_from_hf(
    stations: list[dict[str, Any]],
    daily_dir: Path,
    *,
    start: date,
    end: date,
    force: bool,
) -> tuple[int, str]:
    by_ind = {st["indicativo"]: st for st in stations}
    written = 0
    for d in daterange(start, end):
        path = daily_dir / f"AEMET_{d.strftime('%Y%m%d')}.json"
        if path.exists() and not force:
            continue
        rows = fetch_hf_day(d)
        if rows is None:
            print(f"[aemet] HF 404 {d.isoformat()}", flush=True)
            continue
        filtered = filter_rows_for_stations(rows, by_ind)
        if not filtered:
            print(f"[aemet] HF empty match {d.isoformat()}", flush=True)
            continue
        write_daily_raw(daily_dir, d, filtered, source="huggingface-datania/aemet")
        written += 1
        print(f"[aemet] wrote {d.isoformat()} n={len(filtered)} (hf)", flush=True)
        time.sleep(0.15)
    return written, "huggingface-datania/aemet"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="Rewrite existing daily JSON files")
    ap.add_argument(
        "--source",
        choices=("auto", "api", "hf"),
        default="auto",
        help="Data source (default: auto = API if key else HF)",
    )
    ap.add_argument("--days", type=int, default=KEEP_DAYS, help="How many calendar days to keep/fetch")
    ap.add_argument("--end", type=str, default=None, help="End date YYYY-MM-DD (default: yesterday Madrid)")
    ap.add_argument("--start", type=str, default=None, help="Start date YYYY-MM-DD")
    ap.add_argument("--rebuild-only", action="store_true", help="Only rebuild panel from data/daily")
    args = ap.parse_args(argv)

    root = ROOT
    daily_dir = root / "data" / "daily"
    docs_dir = root / "docs"
    manifest = root / "data" / MANIFEST_NAME
    stations = load_stations(manifest)
    if not stations:
        print("ERROR: no stations in", manifest, file=sys.stderr)
        return 2

    today_m = datetime.now(MADRID).date()
    # Climatology usually lags ≥1 day
    end = date.fromisoformat(args.end) if args.end else (today_m - timedelta(days=1))
    start = date.fromisoformat(args.start) if args.start else (end - timedelta(days=max(1, args.days) - 1))

    source_used = "cache"
    if not args.rebuild_only:
        key = _api_key()
        src = args.source
        if src == "auto":
            src = "api" if key else "hf"
        if src == "api":
            if not key:
                print("ERROR: --source api requires AEMET_API_KEY", file=sys.stderr)
                return 2
            n, source_used = capture_from_api(
                stations, daily_dir, start=start, end=end, api_key=key, force=args.force
            )
            print(f"[aemet] api wrote {n} day files", flush=True)
        else:
            n, source_used = capture_from_hf(
                stations, daily_dir, start=start, end=end, force=args.force
            )
            print(f"[aemet] hf wrote {n} day files", flush=True)

    pruned = prune_daily(daily_dir, keep_days=max(1, args.days))
    if pruned:
        print(f"[aemet] pruned {pruned} old daily files", flush=True)

    note = (
        "AEMET Catalunya daily panel (AE_* = indicativo). "
        f"Source: {source_used}. "
        "Fields P/TX/N/HX/HR/W/WDG from OpenData climatología diaria "
        "(prec/tmax/tmin/hrMax/hrMedia/racha/dir). "
        "Browser cannot call OpenData (CORS + API key); this CI/local job fills docs/panel_aemet.json."
    )
    if source_used.startswith("huggingface"):
        note += (
            " Bootstrap via HuggingFace datania/aemet mirror (may lag). "
            "Add repo secret AEMET_API_KEY for live OpenData updates."
        )

    panel = build_panel_aemet(stations, daily_dir, source_note=note)
    docs_dir.mkdir(parents=True, exist_ok=True)
    out = docs_dir / "panel_aemet.json"
    out.write_text(json.dumps(panel, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"[aemet] panel → {out} days={panel['n_days']} "
        f"series_stations={panel['n_stations_with_series']}/{panel['n_stations_catalog']} "
        f"range={panel['days'][0] if panel['days'] else None}…{panel['days'][-1] if panel['days'] else None}",
        flush=True,
    )
    if panel["n_days"] == 0:
        print("WARNING: panel has no days — need API key or HF data", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
