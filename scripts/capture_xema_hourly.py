#!/usr/bin/env python3
"""Persist XEMA semi-hourly observations as an hourly archive.

Same shape as ESCAT/AEMET hourly capture:

  data/hourly/XEMA_YYYYMMDD_HH.json
  docs/hourly_rain_xema.json
  docs/hourly_meteo_xema.json

Source: Socrata nzvn-apee (Dades meteorològiques de la XEMA). No API key.
data_lectura is Universal Time, labelled at the start of the 30-min bin.
Hours in the public JSON are Europe/Madrid (YYYY-MM-DDTHH:00), matching
hourly_meteo.json.

Variables (codi_variable), aggregated inside each Madrid hour:
  35 PPT mm     sum of the half-hours          → seriesPh / rain series
  32 T °C       mean
  33 HR %       mean
  40 Tx °C      max
  42 Tn °C      min
  3  HRx %      max
  44 HRn %      min
  50 VVx10 m/s  max, published as km/h (×3.6)  → seriesW
  51 DVVx10 °   direction of that max gust     → seriesWDG
  30 VV10 m/s   mean, published as km/h        → seriesWA

ET0, I30 and snow are NOT derived. Official daily (variable 1300, etc.)
replaces this provisional aggregate in the explorador when that day exists;
this archive keeps the hours anyway (same retention as ESCAT, ~14 days).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from http_util import http_get  # noqa: E402

MADRID = ZoneInfo("Europe/Madrid")
UTC = timezone.utc
CCAA = "XEMA"
KEEP_HOURS_DAYS = 14
HOURLY_VERSION = 1
HOURLY_METEO_VERSION = 1
OMIT_ZERO_PH = True
PAGE = 50000
UA = "mc-dades-acumulades/1.0 (+https://github.com/jnoya99/mc-dades-acumulades; xema-hourly)"

DATA_URL = "https://analisi.transparenciacatalunya.cat/resource/nzvn-apee.json"
STATION_URL = "https://analisi.transparenciacatalunya.cat/resource/yqwd-vj5e.json"

# code → (kind, raw field). kind: sum|mean|max|min|gust (gust pairs with dir)
VAR_SPEC = {
    "35": ("sum", "ppt"),
    "32": ("mean", "t"),
    "33": ("mean", "hr"),
    "40": ("max", "tx"),
    "42": ("min", "tn"),
    "3": ("max", "hx"),
    "44": ("min", "hn"),
    "50": ("gust", "w_ms"),
    "51": ("dir", "wdg"),
    "30": ("mean", "wa_ms"),
}
VAR_LIST = ",".join("'" + c + "'" for c in VAR_SPEC)

PH_NOTE = (
    "XEMA Socrata nzvn-apee variable 35 (PPT, semi-hourly, data_lectura in UTC, "
    "labelled at the start of the bin). Ph is the sum of half-hours whose UTC "
    "stamp falls in that Europe/Madrid clock hour. Not a day-cumulative delta. "
    "Hours before the archive starts are absent — never fabricated. "
    "When the official XEMA daily (1300) exists for a station-day, the explorador "
    "uses that daily total instead of this provisional sum (no double count). "
    "v1 slim: Ph==0 omitted from series (missing key ≡ 0 mm)."
)
SNAPSHOT_NOTE = (
    "seriesT/HR are means of semi-hourly readings in the Madrid hour; "
    "seriesTX/TN/HX are max/min of the in-bin extremes (40/42/3); "
    "seriesW is max gust at 10 m (variable 50) converted m/s→km/h (×3.6), "
    "seriesWDG is the direction (51) of that gust; seriesWA is mean wind at 10 m "
    "(variable 30) in km/h. ET0, I30 and snow are not in this file."
)

# public series key → raw field already in display units
SNAPSHOT_SERIES = (
    ("seriesHR", "hr"),
    ("seriesHX", "hx"),
    ("seriesHN", "hn"),
    ("seriesW", "w"),
    ("seriesWDG", "wdg"),
    ("seriesWA", "wa"),
    ("seriesT", "t"),
    ("seriesTX", "tx"),
    ("seriesTN", "tn"),
)


def madrid_now() -> datetime:
    return datetime.now(MADRID)


def hour_to_filename(hour: str) -> str:
    # 2026-10-03T17:00 → XEMA_20261003_17.json
    y, mo, d = hour[0:4], hour[5:7], hour[8:10]
    hh = hour[11:13]
    return f"XEMA_{y}{mo}{d}_{hh}.json"


def parse_hour_from_stem(stem: str) -> str | None:
    if not stem.startswith("XEMA_"):
        return None
    rest = stem[5:]
    if len(rest) != 11 or rest[8] != "_":
        return None
    y, mo, d, hh = rest[0:4], rest[4:6], rest[6:8], rest[9:11]
    if not (y + mo + d + hh).isdigit():
        return None
    return f"{y}-{mo}-{d}T{hh}:00"


def list_hourly_files(hourly_dir: Path) -> list[tuple[str, Path]]:
    if not hourly_dir.is_dir():
        return []
    out: list[tuple[str, Path]] = []
    for path in sorted(hourly_dir.glob("XEMA_????????_??.json")):
        hour = parse_hour_from_stem(path.stem)
        if hour:
            out.append((hour, path))
    out.sort(key=lambda x: x[0])
    return out


def prune_hourly(hourly_dir: Path, keep_days: int = KEEP_HOURS_DAYS) -> int:
    cutoff_day = (madrid_now().date() - timedelta(days=keep_days - 1)).isoformat()
    deleted = 0
    for hour, path in list_hourly_files(hourly_dir):
        if hour[:10] < cutoff_day:
            path.unlink(missing_ok=True)
            deleted += 1
    return deleted


def json_num(v: float | None, nd: int = 1) -> float | int | None:
    if v is None:
        return None
    if v != v:
        return None
    r = round(float(v), nd)
    if abs(r - round(r)) < 1e-9:
        return int(round(r))
    return r


def parse_utc(stamp: str) -> datetime | None:
    if not stamp or not isinstance(stamp, str) or len(stamp) < 16:
        return None
    s = stamp.strip()
    # 2026-10-03T15:30:00.000  (no zone; dataset says Universal Time)
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        elif "+" in s[10:] or s.count("-") > 2:
            dt = datetime.fromisoformat(s)
        else:
            dt = datetime.fromisoformat(s[:19])
            dt = dt.replace(tzinfo=UTC)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def madrid_hour_key(dt_utc: datetime) -> str:
    local = dt_utc.astimezone(MADRID).replace(minute=0, second=0, microsecond=0)
    return local.strftime("%Y-%m-%dT%H:00")


def _socrata_get(url: str) -> Any:
    raw = http_get(url, timeout=180, user_agent=UA, headers={"Accept": "application/json"})
    return json.loads(raw.decode("utf-8"))


def fetch_stations() -> dict[str, dict[str, Any]]:
    """codi_estacio → meta. Public, no key."""
    out: dict[str, dict[str, Any]] = {}
    offset = 0
    while True:
        q = urllib.parse.urlencode(
            {
                "$select": "codi_estacio,nom_estacio,latitud,longitud,altitud,nom_estat_ema",
                "$order": "codi_estacio",
                "$limit": str(PAGE),
                "$offset": str(offset),
            }
        )
        rows = _socrata_get(f"{STATION_URL}?{q}")
        if not isinstance(rows, list) or not rows:
            break
        for r in rows:
            if not isinstance(r, dict):
                continue
            sid = str(r.get("codi_estacio") or "").strip()
            if not sid:
                continue
            try:
                lat = float(r.get("latitud"))
                lon = float(r.get("longitud"))
            except (TypeError, ValueError):
                continue
            try:
                elev = float(r["altitud"]) if r.get("altitud") not in (None, "") else None
            except (TypeError, ValueError):
                elev = None
            out[sid] = {
                "id": sid,
                "name": r.get("nom_estacio") or sid,
                "lon": lon,
                "lat": lat,
                "elev": elev,
                "source": "XEMA",
                "status": r.get("nom_estat_ema") or "",
            }
        if len(rows) < PAGE:
            break
        offset += PAGE
    return out


def fetch_rows(utc_start: datetime, utc_end: datetime) -> list[dict[str, Any]]:
    """Page nzvn-apee for the UTC window and the variable set we keep."""
    start_s = utc_start.strftime("%Y-%m-%dT%H:%M:%S.000")
    end_s = utc_end.strftime("%Y-%m-%dT%H:%M:%S.000")
    where = (
        f"data_lectura>='{start_s}' AND data_lectura<'{end_s}' "
        f"AND codi_variable in({VAR_LIST})"
    )
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        q = urllib.parse.urlencode(
            {
                "$select": "codi_estacio,codi_variable,data_lectura,valor_lectura",
                "$where": where,
                "$order": "data_lectura,codi_estacio",
                "$limit": str(PAGE),
                "$offset": str(offset),
            }
        )
        page = _socrata_get(f"{DATA_URL}?{q}")
        if not isinstance(page, list):
            raise RuntimeError(f"unexpected XEMA page type: {type(page)}")
        rows.extend(page)
        print(f"[xema-hourly] page offset={offset} n={len(page)} total={len(rows)}", flush=True)
        if len(page) < PAGE:
            break
        offset += PAGE
        if offset > 2_000_000:
            raise RuntimeError("XEMA paging ran away; abort")
    return rows


class Bin:
    __slots__ = (
        "ppt_s", "ppt_n", "t_s", "t_n", "hr_s", "hr_n",
        "tx", "tn", "hx", "hn", "wa_s", "wa_n",
        "w_ms", "wdg", "gust_stamp",
    )

    def __init__(self) -> None:
        self.ppt_s = 0.0
        self.ppt_n = 0
        self.t_s = 0.0
        self.t_n = 0
        self.hr_s = 0.0
        self.hr_n = 0
        self.tx: float | None = None
        self.tn: float | None = None
        self.hx: float | None = None
        self.hn: float | None = None
        self.wa_s = 0.0
        self.wa_n = 0
        self.w_ms: float | None = None
        self.wdg: float | None = None
        self.gust_stamp = ""


def aggregate(rows: list[dict[str, Any]]) -> dict[str, dict[str, Bin]]:
    """hour → sid → Bin."""
    by_hour: dict[str, dict[str, Bin]] = {}
    # dir readings buffered per (hour, sid, exact utc stamp) so gust can pick its dir
    pending_dir: dict[tuple[str, str, str], float] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        sid = str(r.get("codi_estacio") or "").strip()
        code = str(r.get("codi_variable") or "").strip()
        spec = VAR_SPEC.get(code)
        if not sid or not spec:
            continue
        dt = parse_utc(str(r.get("data_lectura") or ""))
        if dt is None:
            continue
        try:
            val = float(r.get("valor_lectura"))
        except (TypeError, ValueError):
            continue
        if val != val:
            continue
        hour = madrid_hour_key(dt)
        stamp = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        slot = by_hour.setdefault(hour, {})
        b = slot.get(sid)
        if b is None:
            b = Bin()
            slot[sid] = b
        kind, _field = spec
        if kind == "sum":
            if val < 0:
                val = 0.0
            b.ppt_s += val
            b.ppt_n += 1
        elif kind == "mean" and code == "32":
            b.t_s += val
            b.t_n += 1
        elif kind == "mean" and code == "33":
            b.hr_s += val
            b.hr_n += 1
        elif kind == "mean" and code == "30":
            if val < 0:
                continue
            b.wa_s += val
            b.wa_n += 1
        elif kind == "max" and code == "40":
            b.tx = val if b.tx is None else max(b.tx, val)
        elif kind == "min" and code == "42":
            b.tn = val if b.tn is None else min(b.tn, val)
        elif kind == "max" and code == "3":
            b.hx = val if b.hx is None else max(b.hx, val)
        elif kind == "min" and code == "44":
            b.hn = val if b.hn is None else min(b.hn, val)
        elif kind == "gust":
            if val < 0:
                continue
            if b.w_ms is None or val > b.w_ms or (val == b.w_ms and stamp >= b.gust_stamp):
                b.w_ms = val
                b.gust_stamp = stamp
                # dir may already be cached
                d = pending_dir.get((hour, sid, stamp))
                if d is not None:
                    b.wdg = d
        elif kind == "dir":
            pending_dir[(hour, sid, stamp)] = val
            if b.gust_stamp == stamp:
                b.wdg = val
    return by_hour


def bin_to_station(sid: str, b: Bin, meta: dict[str, Any] | None, hour: str) -> dict[str, Any]:
    m = meta or {}
    w_kmh = None if b.w_ms is None else b.w_ms * 3.6
    wa_kmh = None if b.wa_n == 0 else (b.wa_s / b.wa_n) * 3.6
    row: dict[str, Any] = {
        "id": sid,
        "name": m.get("name") or sid,
        "lon": m.get("lon"),
        "lat": m.get("lat"),
        "elev": m.get("elev"),
        "hour": hour,
    }
    if b.ppt_n:
        row["ppt"] = json_num(b.ppt_s, 2)
    if b.t_n:
        row["t"] = json_num(b.t_s / b.t_n, 2)
    if b.hr_n:
        row["hr"] = json_num(b.hr_s / b.hr_n, 1)
    if b.tx is not None:
        row["tx"] = json_num(b.tx, 2)
    if b.tn is not None:
        row["tn"] = json_num(b.tn, 2)
    if b.hx is not None:
        row["hx"] = json_num(b.hx, 1)
    if b.hn is not None:
        row["hn"] = json_num(b.hn, 1)
    if w_kmh is not None:
        row["w"] = json_num(w_kmh, 1)
    if b.wdg is not None:
        row["wdg"] = json_num(b.wdg, 0)
    if wa_kmh is not None:
        row["wa"] = json_num(wa_kmh, 1)
    return row


def write_hour_file(path: Path, hour: str, stations: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ccaa": CCAA,
        "mode": "hourly",
        "hour": hour,
        "captured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "xema-nzvn-apee",
        "tz": "Europe/Madrid",
        "n_stations": len(stations),
        "stations": stations,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")


def _as_float(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if v != v:
            return None
        return float(v)
    try:
        return float(str(v).strip().replace(",", "."))
    except ValueError:
        return None


def _station_meta_from_files(stations_live: dict[str, dict[str, Any]], hourly_dir: Path) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for sid, st in stations_live.items():
        seen[sid] = {
            "id": sid,
            "mc_id": sid,
            "name": st.get("name") or sid,
            "lon": st.get("lon"),
            "lat": st.get("lat"),
            "elev": st.get("elev"),
            "source": "XEMA",
        }
    for _hour, path in list_hourly_files(hourly_dir):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for st in obj.get("stations") or []:
            if not isinstance(st, dict) or not st.get("id"):
                continue
            sid = str(st["id"])
            if sid in seen and seen[sid].get("lon") is not None:
                continue
            seen[sid] = {
                "id": sid,
                "mc_id": sid,
                "name": st.get("name") or sid,
                "lon": st.get("lon"),
                "lat": st.get("lat"),
                "elev": st.get("elev"),
                "source": "XEMA",
            }
    # Only stations that actually appear in the hourly archive (keeps the JSON small).
    present: set[str] = set()
    for _hour, path in list_hourly_files(hourly_dir):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for st in obj.get("stations") or []:
            if isinstance(st, dict) and st.get("id"):
                present.add(str(st["id"]))
    return [seen[s] for s in sorted(present) if s in seen]


def build_products(stations_meta: list[dict[str, Any]], hourly_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    known = {st["id"] for st in stations_meta}
    files = list_hourly_files(hourly_dir)
    hours = [h for h, _ in files]
    series_ph: dict[str, dict[str, Any]] = {sid: {} for sid in known}
    snap: dict[str, dict[str, dict[str, Any]]] = {
        key: {sid: {} for sid in known} for key, _ in SNAPSHOT_SERIES
    }
    for hour, path in files:
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for st in obj.get("stations") or []:
            if not isinstance(st, dict):
                continue
            sid = str(st.get("id") or "")
            if sid not in known:
                continue
            prec = _as_float(st.get("ppt"))
            if prec is not None:
                if prec < 0:
                    prec = 0.0
                num = json_num(prec, 2)
                if num is not None and (not OMIT_ZERO_PH or float(num) != 0.0):
                    series_ph[sid][hour] = num
            for skey, raw_field in SNAPSHOT_SERIES:
                val = _as_float(st.get(raw_field))
                if val is None:
                    continue
                snap[skey][sid][hour] = json_num(val, 2)

    def nonempty(d: dict[str, dict]) -> dict[str, dict]:
        return {sid: byh for sid, byh in d.items() if byh}

    built = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    rain = {
        "version": HOURLY_VERSION,
        "built": built,
        "ccaa": CCAA,
        "hours": hours,
        "stations": stations_meta,
        "series": nonempty(series_ph),
        "delta_note": PH_NOTE,
        "ph_note": PH_NOTE,
        "retention_days": KEEP_HOURS_DAYS,
        "n_stations_catalog": len(stations_meta),
        "n_stations_with_series": len(nonempty(series_ph)),
        "n_hours": len(hours),
        "zeros_omitted": True,
    }
    meteo: dict[str, Any] = {
        "version": HOURLY_METEO_VERSION,
        "built": built,
        "ccaa": CCAA,
        "hours": hours,
        "stations": stations_meta,
        "seriesPh": nonempty(series_ph),
        "delta_note": PH_NOTE,
        "ph_note": PH_NOTE,
        "snapshot_note": SNAPSHOT_NOTE,
        "retention_days": KEEP_HOURS_DAYS,
        "n_stations_catalog": len(stations_meta),
        "n_stations_with_series": len(nonempty(series_ph)),
        "n_hours": len(hours),
        "wind_unit": "km/h",
    }
    for skey, _ in SNAPSHOT_SERIES:
        meteo[skey] = nonempty(snap[skey])
    return rain, meteo


def write_json(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")


def capture(root: Path, *, days: int, skip_fetch: bool) -> dict[str, Any]:
    hourly_dir = root / "data" / "hourly"
    docs = root / "docs"
    hourly_dir.mkdir(parents=True, exist_ok=True)
    stations: dict[str, dict[str, Any]] = {}
    n_rows = 0
    n_hours_written = 0
    if not skip_fetch:
        stations = fetch_stations()
        if not stations:
            raise SystemExit("XEMA station catalog empty (yqwd-vj5e)")
        now_local = madrid_now()
        # Cover `days` Madrid calendar days through the current hour, plus a
        # 3 h UTC pad so a bin labelled just before local midnight is included.
        start_local = (now_local - timedelta(days=days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        utc_start = start_local.astimezone(UTC) - timedelta(hours=3)
        utc_end = now_local.astimezone(UTC) + timedelta(hours=3)
        print(
            f"[xema-hourly] fetch {utc_start.isoformat()} → {utc_end.isoformat()} "
            f"({days} Madrid days, {len(stations)} stations in catalog)",
            flush=True,
        )
        rows = fetch_rows(utc_start, utc_end)
        n_rows = len(rows)
        by_hour = aggregate(rows)
        for hour in sorted(by_hour.keys()):
            # Drop the pad hours that fall outside the retention window.
            if hour[:10] < start_local.date().isoformat():
                continue
            if hour > now_local.strftime("%Y-%m-%dT%H:00"):
                continue
            st_rows = []
            for sid in sorted(by_hour[hour].keys()):
                meta = stations.get(sid)
                st_rows.append(bin_to_station(sid, by_hour[hour][sid], meta, hour))
            write_hour_file(hourly_dir / hour_to_filename(hour), hour, st_rows)
            n_hours_written += 1
        print(f"[xema-hourly] wrote {n_hours_written} hour files from {n_rows} rows", flush=True)

    deleted = prune_hourly(hourly_dir, KEEP_HOURS_DAYS)
    meta_list = _station_meta_from_files(stations, hourly_dir)
    rain, meteo = build_products(meta_list, hourly_dir)
    rain_path = docs / "hourly_rain_xema.json"
    meteo_path = docs / "hourly_meteo_xema.json"
    write_json(rain, rain_path)
    write_json(meteo, meteo_path)
    return {
        "ok": True,
        "source": "cache" if skip_fetch else "xema-nzvn-apee",
        "n_raw_rows": n_rows,
        "hours_written": n_hours_written,
        "n_hours": rain["n_hours"],
        "n_series_stations": rain["n_stations_with_series"],
        "n_catalog": rain["n_stations_catalog"],
        "hour_range": [rain["hours"][0], rain["hours"][-1]] if rain["hours"] else None,
        "hourly_rain": str(rain_path),
        "hourly_meteo": str(meteo_path),
        "pruned": deleted,
        "built": rain["built"],
        "meteo_bytes": meteo_path.stat().st_size if meteo_path.exists() else 0,
        "rain_bytes": rain_path.stat().st_size if rain_path.exists() else 0,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--days", type=int, default=KEEP_HOURS_DAYS, help="Madrid days to (re)fetch")
    ap.add_argument("--force", action="store_true", help="accepted for workflow parity; hours are always rewritten")
    ap.add_argument("--rebuild-only", action="store_true", help="do not fetch; rebuild JSON from data/hourly/XEMA_*")
    args = ap.parse_args(argv)
    days = args.days if args.days and args.days > 0 else KEEP_HOURS_DAYS
    try:
        info = capture(args.root, days=days, skip_fetch=args.rebuild_only)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(json.dumps(info, ensure_ascii=False, indent=2))
    if info.get("n_hours", 0) == 0:
        print("WARNING: no XEMA hourly hours on disk", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
