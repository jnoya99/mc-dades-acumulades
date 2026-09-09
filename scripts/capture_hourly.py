#!/usr/bin/env python3
"""Hourly ESCAT capture from Meteoclimatic public XML/RSS.

MC XML only exposes day-cumulative rain/total. We snapshot each hour and
derive hourly mm as the non-negative delta of cumulative totals.

Saves:
  data/hourly/ESCAT_YYYYMMDD_HH.json   (raw: cum + Hum/Vient/Temp snapshots)
  docs/hourly_rain.json               (derived hour→Ph mm for keep stations)
  docs/hourly_meteo.json              (Ph deltas + HR/HX/W/WDG/T snapshots)

Retention: last ~14 calendar days of raw hourly files (Madrid).
Does not invent hours before the first capture (first sample = baseline, Ph=0).

Delta rules (documented in hourly_rain.json.delta_note / hourly_meteo):
  - Same calendar day, cum non-decreasing: Ph = max(0, cum_now - cum_prev)
  - Cum drop (midnight/station reset): Ph = max(0, cum_now); prev ignored
  - First sample for a station: Ph = 0 (establish baseline only)

Snapshot fields (HR, HX, W, …) are instantaneous values at capture hour,
NOT deltas — only rain uses delta logic.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_panel import load_keep, _json_num  # noqa: E402
from capture_escat import (  # noqa: E402
    CCAA,
    MADRID,
    RSS_URL,
    XML_URL,
    _http_get,
    parse_rss_coords,
    parse_xml_stations,
)

KEEP_HOURS_DAYS = 14
HOURLY_VERSION = 1
HOURLY_METEO_VERSION = 1

DELTA_NOTE = (
    "Ph = max(0, cum_now - cum_prev) within the same Madrid calendar day. "
    "If cum drops (midnight reset or station reset), Ph = max(0, cum_now) "
    "and the previous cumulative is ignored (new baseline). "
    "The first sample for a station after (re)start sets Ph = 0 so we do not "
    "invent rain before the first capture. Hours before the archive starts "
    "are absent — never fabricated."
)

SNAPSHOT_NOTE = (
    "seriesHR/HX/HN/W/WDG/WA/T/TX/TN are instantaneous snapshots at the "
    "capture hour (Europe/Madrid), not deltas. Only seriesPh is derived "
    "from cumulative rain via delta rules."
)

# Raw station fields kept from XML (+ coords from RSS)
RAW_NUM_FIELDS = (
    "cum",  # alias of Precip.total / Precip.diaria
    "Precip.diaria",
    "Precip.total",
    "Hum.act",
    "Hum.max",
    "Hum.min",
    "Vient.max",
    "Vient.dir",
    "Vient.act",
    "Temp.act",
    "Temp.max",
    "Temp.min",
)

# Public hourly_meteo series key → raw field (snapshots only)
SNAPSHOT_SERIES = (
    ("seriesHR", "Hum.act"),
    ("seriesHX", "Hum.max"),
    ("seriesHN", "Hum.min"),
    ("seriesW", "Vient.max"),
    ("seriesWDG", "Vient.dir"),
    ("seriesWA", "Vient.act"),
    ("seriesT", "Temp.act"),  # “now”
    ("seriesTX", "Temp.max"),
    ("seriesTN", "Temp.min"),
)


def madrid_hour_stamp(now: datetime | None = None) -> str:
    """Floor to current Europe/Madrid hour: YYYY-MM-DDTHH:00."""
    dt = now or datetime.now(MADRID)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MADRID)
    else:
        dt = dt.astimezone(MADRID)
    return dt.strftime("%Y-%m-%dT%H:00")


def hour_to_filename(hour: str, ccaa: str = CCAA) -> str:
    # 2026-09-09T05:00 → ESCAT_20260909_05.json
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):00$", hour)
    if not m:
        raise ValueError(f"bad hour stamp: {hour}")
    y, mo, d, hh = m.groups()
    return f"{ccaa}_{y}{mo}{d}_{hh}.json"


def parse_hour_from_stem(stem: str, ccaa: str = CCAA) -> str | None:
    # ESCAT_YYYYMMDD_HH
    prefix = ccaa + "_"
    if not stem.startswith(prefix):
        return None
    rest = stem[len(prefix) :]
    m = re.match(r"^(\d{4})(\d{2})(\d{2})_(\d{2})$", rest)
    if not m:
        return None
    y, mo, d, hh = m.groups()
    return f"{y}-{mo}-{d}T{hh}:00"


def list_hourly_files(hourly_dir: Path, ccaa: str = CCAA) -> list[tuple[str, Path]]:
    if not hourly_dir.is_dir():
        return []
    out: list[tuple[str, Path]] = []
    for path in sorted(hourly_dir.glob(f"{ccaa}_????????_??.json")):
        hour = parse_hour_from_stem(path.stem, ccaa)
        if hour:
            out.append((hour, path))
    out.sort(key=lambda x: x[0])
    return out


def prune_hourly(hourly_dir: Path, keep_days: int = KEEP_HOURS_DAYS, ccaa: str = CCAA) -> int:
    """Delete raw hourly files older than keep_days (Madrid calendar). Return deleted count."""
    cutoff_day = (datetime.now(MADRID).date() - timedelta(days=keep_days - 1)).isoformat()
    deleted = 0
    for hour, path in list_hourly_files(hourly_dir, ccaa):
        day = hour[:10]
        if day < cutoff_day:
            path.unlink(missing_ok=True)
            deleted += 1
    return deleted


def fetch_hourly_rows(ccaa: str = CCAA) -> list[dict[str, Any]]:
    """Fetch XML+RSS and return raw station dicts with cum + meteo fields."""
    xml_bytes = _http_get(XML_URL.format(id=ccaa))
    rss_bytes = _http_get(RSS_URL.format(id=ccaa))
    xml_rows = parse_xml_stations(xml_bytes)
    coords = parse_rss_coords(rss_bytes)
    if not xml_rows:
        raise SystemExit("XML feed returned 0 stations")
    out: list[dict[str, Any]] = []
    for r in xml_rows:
        sid = r["id"]
        c = coords.get(sid, {})
        cum = r.get("Precip.total")
        if cum is None:
            cum = r.get("Precip.diaria")
        row: dict[str, Any] = {
            "id": sid,
            "name": c.get("name") or r.get("name") or sid,
            "lon": c.get("lon"),
            "lat": c.get("lat"),
            "cum": cum,
            "Precip.diaria": r.get("Precip.diaria") if r.get("Precip.diaria") is not None else cum,
            "Precip.total": r.get("Precip.total") if r.get("Precip.total") is not None else cum,
            "Hum.act": r.get("Hum.act"),
            "Hum.max": r.get("Hum.max"),
            "Hum.min": r.get("Hum.min"),
            "Vient.max": r.get("Vient.max"),
            "Vient.dir": r.get("Vient.dir"),
            "Vient.act": r.get("Vient.act"),
            "Temp.act": r.get("Temp.act"),
            "Temp.max": r.get("Temp.max"),
            "Temp.min": r.get("Temp.min"),
        }
        out.append(row)
    out.sort(key=lambda x: x["id"])
    return out


# Back-compat alias
fetch_cum_rows = fetch_hourly_rows


def write_hourly_raw(
    rows: list[dict[str, Any]],
    path: Path,
    hour: str,
    ccaa: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ccaa": ccaa,
        "hour": hour,
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "xml_feed",
        "n_stations": len(rows),
        "stations": rows,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _mc_id(mc_raw: str, keep_by_mc: dict[str, dict]) -> str | None:
    if mc_raw in keep_by_mc:
        return keep_by_mc[mc_raw]["id"]
    return None


def _station_meta(keep_rows: list[dict]) -> list[dict[str, Any]]:
    return [
        {
            "id": r["id"],
            "mc_id": r["mc_id"],
            "name": r["name"],
            "lon": _json_num(r["lon"]),
            "lat": _json_num(r["lat"]),
            "elev": _json_num(r["elev"]),
        }
        for r in keep_rows
    ]


def _as_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    return x


def _ph_delta(
    sid: str,
    cum: float,
    hour: str,
    day: str,
    prev_cum: dict[str, float],
    prev_hour: dict[str, str],
) -> float:
    if sid not in prev_cum:
        return 0.0
    prev = prev_cum[sid]
    prev_h = prev_hour[sid]
    prev_day = prev_h[:10]
    if cum >= prev and day == prev_day:
        return max(0.0, cum - prev)
    if cum >= prev and day != prev_day:
        # Crossed midnight without seeing a drop: new-day contribution = cum
        return max(0.0, cum)
    # Drop → reset
    return max(0.0, cum)


def build_hourly_rain(
    keep_rows: list[dict],
    hourly_dir: Path,
    ccaa: str = CCAA,
) -> dict[str, Any]:
    keep_by_mc = {r["mc_id"]: r for r in keep_rows}
    files = list_hourly_files(hourly_dir, ccaa)
    hours = [h for h, _ in files]

    series: dict[str, dict[str, float | int | None]] = {r["id"]: {} for r in keep_rows}
    prev_cum: dict[str, float] = {}
    prev_hour: dict[str, str] = {}

    for hour, path in files:
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        st_list = obj.get("stations") if isinstance(obj, dict) else None
        if not isinstance(st_list, list):
            continue
        day = hour[:10]
        for st in st_list:
            if not isinstance(st, dict):
                continue
            mc_raw = (st.get("id") or "").strip()
            sid = _mc_id(mc_raw, keep_by_mc)
            if not sid:
                continue
            cum = _as_float(st.get("cum"))
            if cum is None:
                cum = _as_float(st.get("Precip.total"))
            if cum is None:
                cum = _as_float(st.get("Precip.diaria"))
            if cum is None:
                continue

            ph = _ph_delta(sid, cum, hour, day, prev_cum, prev_hour)
            series[sid][hour] = _json_num(ph)
            prev_cum[sid] = cum
            prev_hour[sid] = hour

    stations = _station_meta(keep_rows)
    series_out = {sid: byh for sid, byh in series.items() if byh}
    built = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "version": HOURLY_VERSION,
        "built": built,
        "ccaa": ccaa,
        "hours": hours,
        "stations": stations,
        "series": series_out,
        "delta_note": DELTA_NOTE,
        "retention_days": KEEP_HOURS_DAYS,
    }


def build_hourly_meteo(
    keep_rows: list[dict],
    hourly_dir: Path,
    ccaa: str = CCAA,
) -> dict[str, Any]:
    """Ph deltas + snapshot series (HR/HX/W/…) for keep stations."""
    keep_by_mc = {r["mc_id"]: r for r in keep_rows}
    files = list_hourly_files(hourly_dir, ccaa)
    hours = [h for h, _ in files]

    series_ph: dict[str, dict[str, float | int | None]] = {r["id"]: {} for r in keep_rows}
    snap: dict[str, dict[str, dict[str, float | int | None]]] = {
        key: {r["id"]: {} for r in keep_rows} for key, _ in SNAPSHOT_SERIES
    }
    prev_cum: dict[str, float] = {}
    prev_hour: dict[str, str] = {}

    for hour, path in files:
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        st_list = obj.get("stations") if isinstance(obj, dict) else None
        if not isinstance(st_list, list):
            continue
        day = hour[:10]
        for st in st_list:
            if not isinstance(st, dict):
                continue
            mc_raw = (st.get("id") or "").strip()
            sid = _mc_id(mc_raw, keep_by_mc)
            if not sid:
                continue

            cum = _as_float(st.get("cum"))
            if cum is None:
                cum = _as_float(st.get("Precip.total"))
            if cum is None:
                cum = _as_float(st.get("Precip.diaria"))
            if cum is not None:
                ph = _ph_delta(sid, cum, hour, day, prev_cum, prev_hour)
                series_ph[sid][hour] = _json_num(ph)
                prev_cum[sid] = cum
                prev_hour[sid] = hour

            for skey, raw_field in SNAPSHOT_SERIES:
                val = _as_float(st.get(raw_field))
                if val is None:
                    continue
                snap[skey][sid][hour] = _json_num(val)

    stations = _station_meta(keep_rows)
    built = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _nonempty(d: dict[str, dict]) -> dict[str, dict]:
        return {sid: byh for sid, byh in d.items() if byh}

    out: dict[str, Any] = {
        "version": HOURLY_METEO_VERSION,
        "built": built,
        "ccaa": ccaa,
        "hours": hours,
        "stations": stations,
        "seriesPh": _nonempty(series_ph),
        "delta_note": DELTA_NOTE,
        "snapshot_note": SNAPSHOT_NOTE,
        "retention_days": KEEP_HOURS_DAYS,
    }
    for skey, _ in SNAPSHOT_SERIES:
        out[skey] = _nonempty(snap[skey])
    return out


def write_json_compact(payload: dict, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    out_path.write_text(text, encoding="utf-8")
    return out_path


write_hourly_rain = write_json_compact


def capture(
    root: Path,
    ccaa: str = CCAA,
    hour: str | None = None,
    force: bool = False,
    skip_fetch: bool = False,
) -> dict[str, Any]:
    hour = hour or madrid_hour_stamp()
    hourly_dir = root / "data" / "hourly"
    raw_path = hourly_dir / hour_to_filename(hour, ccaa)
    keep_path = root / "data" / "stations_keep.csv"
    rain_path = root / "docs" / "hourly_rain.json"
    meteo_path = root / "docs" / "hourly_meteo.json"

    fetched = False
    n_stations = None
    if skip_fetch:
        pass
    elif not force and raw_path.exists():
        pass
    else:
        rows = fetch_hourly_rows(ccaa)
        write_hourly_raw(rows, raw_path, hour, ccaa)
        fetched = True
        n_stations = len(rows)

    deleted = prune_hourly(hourly_dir, KEEP_HOURS_DAYS, ccaa)
    keep_rows = load_keep(keep_path)
    rain_payload = build_hourly_rain(keep_rows, hourly_dir, ccaa)
    meteo_payload = build_hourly_meteo(keep_rows, hourly_dir, ccaa)
    write_json_compact(rain_payload, rain_path)
    write_json_compact(meteo_payload, meteo_path)

    return {
        "ok": True,
        "hour": hour,
        "fetched": fetched,
        "cached": (not fetched) and raw_path.exists() and not skip_fetch,
        "raw": str(raw_path),
        "hourly_rain": str(rain_path),
        "hourly_meteo": str(meteo_path),
        "n_stations_raw": n_stations,
        "n_hours": len(rain_payload["hours"]),
        "n_series_stations": len(rain_payload["series"]),
        "n_meteo_hr_stations": len(meteo_payload.get("seriesHR") or {}),
        "pruned": deleted,
        "built": rain_payload["built"],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=ROOT)
    p.add_argument("--ccaa", default=CCAA)
    p.add_argument("--hour", default=None, help="YYYY-MM-DDTHH:00 (default: now Europe/Madrid floored)")
    p.add_argument("--force", action="store_true", help="re-fetch even if raw hour file exists")
    p.add_argument(
        "--rebuild-only",
        action="store_true",
        help="do not fetch; only prune + rebuild hourly_rain/meteo.json from existing raw files",
    )
    args = p.parse_args(argv)

    try:
        info = capture(
            args.root,
            args.ccaa,
            args.hour,
            force=args.force,
            skip_fetch=args.rebuild_only,
        )
    except urllib.error.HTTPError as e:
        print(f"HTTP error: {e.code} {e.reason}", file=sys.stderr)
        return 2
    except urllib.error.URLError as e:
        print(f"URL error: {e.reason}", file=sys.stderr)
        return 2

    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
