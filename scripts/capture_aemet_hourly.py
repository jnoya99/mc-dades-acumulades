#!/usr/bin/env python3
"""Capture AEMET OpenData *hourly* conventional observations for Catalunya.

Uses a single OpenData call:
  GET /observacion/convencional/todas
which returns the last ~12–24 h of hourly rows for all Spanish conventional
stations. We filter to data/stations_aemet.json (AE_<indicativo>) and archive
by Europe/Madrid hour.

AEMET `prec` is already mm in the hour ending at `fint` (UTC) — NOT a day
cumulative — so public Ph is written directly (no ESCAT-style delta).

Writes:
  data/hourly/AEMET_YYYYMMDD_HH.json   (raw rows for that Madrid hour)
  docs/hourly_rain_aemet.json         (Ph mm; schema ≈ hourly_rain.json)
  docs/hourly_meteo_aemet.json        (Ph + HR/W/T snapshots; ≈ hourly_meteo)

Retention: last ~14 Madrid calendar days of raw hourly files.
Needs env AEMET_API_KEY (or AEMET_API_TOKEN).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from http_util import http_get  # noqa: E402

# Reuse station loader + num helpers from daily capture
from capture_aemet import (  # noqa: E402
    AEMET_API_BASE,
    MANIFEST_NAME,
    UA,
    _api_key,
    _json_num,
    _num,
    load_stations,
)

MADRID = ZoneInfo("Europe/Madrid")
UTC = timezone.utc
CCAA = "AEMET-CAT"
KEEP_HOURS_DAYS = 14
HOURLY_VERSION = 1
OMIT_ZERO_PH = True  # slim rain: drop Ph==0 from published series
HOURLY_METEO_VERSION = 1

PH_NOTE = (
    "AEMET OpenData observacion convencional: prec is mm accumulated in the "
    "60 minutes ending at fint (UTC). We convert fint→Europe/Madrid hour stamp "
    "YYYY-MM-DDTHH:00 and publish Ph = prec directly (no cumulative delta). "
    "Hours before the archive starts are absent — never fabricated. "
    "Source: GET /observacion/convencional/todas (rolling ~12–24 h window)."
)

SNAPSHOT_NOTE = (
    "seriesHR/T/TX/TN/W/WA/WDG are instantaneous (or in-hour extreme) values "
    "from the same observation row at that Madrid hour, not deltas. "
    "Only seriesPh / series is precipitation."
)

# Public meteo series key → AEMET observation field
SNAPSHOT_SERIES = (
    ("seriesHR", "hr"),
    ("seriesT", "ta"),
    ("seriesTX", "tamax"),
    ("seriesTN", "tamin"),
    ("seriesW", "vmax"),
    ("seriesWA", "vv"),
    ("seriesWDG", "dv"),
)


def _decode_json_bytes(raw: bytes) -> Any:
    """AEMET datos blobs are often latin-1 / ISO-8859-15, not UTF-8."""
    for enc in ("utf-8", "utf-8-sig", "iso-8859-15", "latin-1", "cp1252"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return json.loads(raw.decode("latin-1", errors="replace"))


def _aemet_get_json(path: str, api_key: str) -> Any:
    url = f"{AEMET_API_BASE}/{path.lstrip('/')}"
    sep = "&" if "?" in url else "?"
    url_q = f"{url}{sep}{urllib.parse.urlencode({'api_key': api_key})}"
    meta_raw = http_get(
        url_q,
        timeout=90,
        user_agent=UA + "; hourly",
        headers={"Accept": "application/json", "api_key": api_key},
    )
    meta = _decode_json_bytes(meta_raw)
    if not isinstance(meta, dict):
        raise RuntimeError(f"Unexpected AEMET meta type: {type(meta)}")
    estado = meta.get("estado")
    if estado not in (200, "200", None):
        desc = meta.get("descripcion") or meta
        if int(estado or 0) == 404:
            return []
        raise RuntimeError(f"AEMET API estado={estado}: {desc}")
    datos_url = meta.get("datos")
    if not datos_url:
        if isinstance(meta.get("datos"), list):
            return meta["datos"]
        raise RuntimeError(f"AEMET meta missing datos URL: {meta}")
    body = http_get(
        str(datos_url),
        timeout=180,
        user_agent=UA + "; hourly",
        headers={"Accept": "application/json"},
    )
    return _decode_json_bytes(body)


def parse_fint_to_madrid_hour(fint: str) -> str | None:
    """fint like 2026-09-30T05:00:00+0000 → Europe/Madrid YYYY-MM-DDTHH:00."""
    if not fint or not isinstance(fint, str):
        return None
    s = fint.strip()
    # Normalize +0000 / +00:00 / Z
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone(MADRID)
    return local.strftime("%Y-%m-%dT%H:00")


def hour_to_filename(hour: str) -> str:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):00$", hour)
    if not m:
        raise ValueError(f"bad hour stamp: {hour}")
    y, mo, d, hh = m.groups()
    return f"AEMET_{y}{mo}{d}_{hh}.json"


def parse_hour_from_stem(stem: str) -> str | None:
    # AEMET_YYYYMMDD_HH
    if not stem.startswith("AEMET_"):
        return None
    rest = stem[6:]
    m = re.match(r"^(\d{4})(\d{2})(\d{2})_(\d{2})$", rest)
    if not m:
        return None
    y, mo, d, hh = m.groups()
    return f"{y}-{mo}-{d}T{hh}:00"


def list_hourly_files(hourly_dir: Path) -> list[tuple[str, Path]]:
    if not hourly_dir.is_dir():
        return []
    out: list[tuple[str, Path]] = []
    for path in sorted(hourly_dir.glob("AEMET_????????_??.json")):
        hour = parse_hour_from_stem(path.stem)
        if hour:
            out.append((hour, path))
    out.sort(key=lambda x: x[0])
    return out


def prune_hourly(hourly_dir: Path, keep_days: int = KEEP_HOURS_DAYS) -> int:
    cutoff_day = (datetime.now(MADRID).date() - timedelta(days=keep_days - 1)).isoformat()
    deleted = 0
    for hour, path in list_hourly_files(hourly_dir):
        if hour[:10] < cutoff_day:
            path.unlink(missing_ok=True)
            deleted += 1
    return deleted


def fetch_todas_rows(api_key: str) -> list[dict[str, Any]]:
    data = _aemet_get_json("observacion/convencional/todas", api_key)
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def row_to_station(r: dict[str, Any], st_meta: dict[str, Any], hour: str) -> dict[str, Any]:
    ind = st_meta["indicativo"]
    return {
        "id": st_meta["id"],
        "indicativo": ind,
        "name": st_meta["name"],
        "lon": st_meta["lon"],
        "lat": st_meta["lat"],
        "elev": st_meta["elev"],
        "hour": hour,
        "fint": r.get("fint"),
        "prec": r.get("prec"),
        "hr": r.get("hr"),
        "ta": r.get("ta"),
        "tamax": r.get("tamax"),
        "tamin": r.get("tamin"),
        "vmax": r.get("vmax"),
        "vv": r.get("vv"),
        "dv": r.get("dv"),
        "pres": r.get("pres"),
        "ubi": r.get("ubi"),
    }


def merge_hour_file(path: Path, hour: str, stations: list[dict[str, Any]], *, source: str) -> Path:
    """Write or merge stations for one Madrid hour (prefer newest row per id)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    by_id: dict[str, dict[str, Any]] = {}
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            for st in old.get("stations") or []:
                if isinstance(st, dict) and st.get("id"):
                    by_id[str(st["id"])] = st
        except (OSError, json.JSONDecodeError):
            pass
    for st in stations:
        sid = str(st.get("id") or "")
        if not sid:
            continue
        # Prefer incoming (fresher capture of same hour)
        by_id[sid] = st
    rows = [by_id[k] for k in sorted(by_id.keys())]
    payload = {
        "ccaa": CCAA,
        "mode": "hourly",
        "hour": hour,
        "captured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "n_stations": len(rows),
        "stations": rows,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _station_meta(stations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": r["id"],
            "mc_id": r.get("mc_id") or r["id"],
            "name": r["name"],
            "lon": r["lon"],
            "lat": r["lat"],
            "elev": r["elev"],
            "source": r.get("source") or "AEMET",
            "indicativo": r.get("indicativo"),
        }
        for r in stations
    ]


def _as_float(v: Any) -> float | None:
    return _num(v)


def build_hourly_rain(
    stations_meta: list[dict[str, Any]],
    hourly_dir: Path,
) -> dict[str, Any]:
    known = {st["id"]: st for st in stations_meta}
    files = list_hourly_files(hourly_dir)
    hours = [h for h, _ in files]
    series: dict[str, dict[str, float | int | None]] = {sid: {} for sid in known}

    for hour, path in files:
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        st_list = obj.get("stations") if isinstance(obj, dict) else None
        if not isinstance(st_list, list):
            continue
        for st in st_list:
            if not isinstance(st, dict):
                continue
            sid = (st.get("id") or "").strip()
            if sid not in series:
                continue
            prec = _as_float(st.get("prec"))
            if prec is None:
                continue
            if prec < 0:
                prec = 0.0
            ph_num = _json_num(prec)
            if ph_num is None:
                continue
            if OMIT_ZERO_PH and float(ph_num) == 0.0:
                continue
            series[sid][hour] = ph_num

    series_out = {sid: byh for sid, byh in series.items() if byh}
    built = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "version": HOURLY_VERSION,
        "built": built,
        "ccaa": CCAA,
        "hours": hours,
        "stations": _station_meta(stations_meta),
        "series": series_out,
        "delta_note": PH_NOTE,
        "ph_note": PH_NOTE,
        "retention_days": KEEP_HOURS_DAYS,
        "n_stations_catalog": len(stations_meta),
        "n_stations_with_series": len(series_out),
        "n_hours": len(hours),
        "zeros_omitted": bool(OMIT_ZERO_PH),
    }


def build_hourly_meteo(
    stations_meta: list[dict[str, Any]],
    hourly_dir: Path,
) -> dict[str, Any]:
    known = {st["id"] for st in stations_meta}
    files = list_hourly_files(hourly_dir)
    hours = [h for h, _ in files]
    series_ph: dict[str, dict[str, float | int | None]] = {sid: {} for sid in known}
    snap: dict[str, dict[str, dict[str, float | int | None]]] = {
        key: {sid: {} for sid in known} for key, _ in SNAPSHOT_SERIES
    }

    for hour, path in files:
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        st_list = obj.get("stations") if isinstance(obj, dict) else None
        if not isinstance(st_list, list):
            continue
        for st in st_list:
            if not isinstance(st, dict):
                continue
            sid = (st.get("id") or "").strip()
            if sid not in known:
                continue
            prec = _as_float(st.get("prec"))
            if prec is not None:
                if prec < 0:
                    prec = 0.0
                ph_num = _json_num(prec)
                if ph_num is not None and (not OMIT_ZERO_PH or float(ph_num) != 0.0):
                    series_ph[sid][hour] = ph_num
            for skey, raw_field in SNAPSHOT_SERIES:
                val = _as_float(st.get(raw_field))
                if val is None:
                    continue
                snap[skey][sid][hour] = _json_num(val)

    def _nonempty(d: dict[str, dict]) -> dict[str, dict]:
        return {sid: byh for sid, byh in d.items() if byh}

    built = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    out: dict[str, Any] = {
        "version": HOURLY_METEO_VERSION,
        "built": built,
        "ccaa": CCAA,
        "hours": hours,
        "stations": _station_meta(stations_meta),
        "seriesPh": _nonempty(series_ph),
        "delta_note": PH_NOTE,
        "ph_note": PH_NOTE,
        "snapshot_note": SNAPSHOT_NOTE,
        "retention_days": KEEP_HOURS_DAYS,
        "n_stations_catalog": len(stations_meta),
        "n_stations_with_series": len(_nonempty(series_ph)),
        "n_hours": len(hours),
    }
    for skey, _ in SNAPSHOT_SERIES:
        out[skey] = _nonempty(snap[skey])
    return out


def write_json_compact(payload: dict, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    out_path.write_text(text, encoding="utf-8")
    return out_path


def capture(
    root: Path,
    *,
    force: bool = False,
    skip_fetch: bool = False,
) -> dict[str, Any]:
    hourly_dir = root / "data" / "hourly"
    docs_dir = root / "docs"
    stations = load_stations(root / "data" / MANIFEST_NAME)
    if not stations:
        raise SystemExit(f"no stations in {MANIFEST_NAME}")
    by_ind = {st["indicativo"]: st for st in stations}

    fetched_hours = 0
    n_raw_rows = 0
    source_used = "cache"

    if not skip_fetch:
        key = _api_key()
        if not key:
            raise SystemExit("AEMET_API_KEY required for hourly capture (no HF hourly mirror)")
        rows = fetch_todas_rows(key)
        n_raw_rows = len(rows)
        source_used = "aemet-opendata-observacion-todas"
        # Bucket by Madrid hour → CAT stations
        by_hour: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            ind = str(r.get("idema") or "").strip()
            st = by_ind.get(ind)
            if not st:
                continue
            hour = parse_fint_to_madrid_hour(str(r.get("fint") or ""))
            if not hour:
                continue
            by_hour.setdefault(hour, []).append(row_to_station(r, st, hour))

        for hour in sorted(by_hour.keys()):
            path = hourly_dir / hour_to_filename(hour)
            if path.exists() and not force:
                # Still merge — same hour may gain/lose stations as feed rolls
                pass
            merge_hour_file(path, hour, by_hour[hour], source=source_used)
            fetched_hours += 1
            print(f"[aemet-hourly] hour {hour} n={len(by_hour[hour])}", flush=True)
        # gentle pause unused (single API call) — keep hook for future per-station
        time.sleep(0)

    deleted = prune_hourly(hourly_dir, KEEP_HOURS_DAYS)
    rain = build_hourly_rain(stations, hourly_dir)
    meteo = build_hourly_meteo(stations, hourly_dir)
    rain_path = docs_dir / "hourly_rain_aemet.json"
    meteo_path = docs_dir / "hourly_meteo_aemet.json"
    write_json_compact(rain, rain_path)
    write_json_compact(meteo, meteo_path)

    return {
        "ok": True,
        "source": source_used,
        "fetched_hours": fetched_hours,
        "n_raw_spain_rows": n_raw_rows,
        "n_hours": rain["n_hours"],
        "n_series_stations": rain["n_stations_with_series"],
        "n_catalog": rain["n_stations_catalog"],
        "hour_range": (
            [rain["hours"][0], rain["hours"][-1]] if rain["hours"] else None
        ),
        "hourly_rain": str(rain_path),
        "hourly_meteo": str(meteo_path),
        "pruned": deleted,
        "built": rain["built"],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--force", action="store_true", help="re-merge even if hour file exists")
    ap.add_argument(
        "--rebuild-only",
        action="store_true",
        help="do not fetch; prune + rebuild public JSON from data/hourly/AEMET_*",
    )
    args = ap.parse_args(argv)

    try:
        info = capture(args.root, force=args.force, skip_fetch=args.rebuild_only)
    except urllib.error.HTTPError as e:
        print(f"HTTP error: {e.code} {e.reason}", file=sys.stderr)
        return 2
    except urllib.error.URLError as e:
        print(f"URL error: {e.reason}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(json.dumps(info, ensure_ascii=False, indent=2))
    if info.get("n_hours", 0) == 0:
        print("WARNING: no AEMET hourly hours on disk", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
