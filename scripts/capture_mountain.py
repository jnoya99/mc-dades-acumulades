#!/usr/bin/env python3
"""Capture mountain / complementary stations (MeteOsona, Meteoguilleries,
ClimaMeteoInfo, Meteocadí / WeatherLink).

IDs are namespaced: MO_*, MG_*, CMI_*, MCADI_*.
Does NOT touch data/stations_keep.csv (ESCAT keep list stays unchanged).

Modes:
  --mode daily   all sources → data/daily/MOUNTAIN_YYYYMMDD.json + docs/panel_mountain.json
  --mode hourly  sources with useful sub-daily rain (MO, MG, MCADI) →
                 data/hourly/MOUNTAIN_YYYYMMDD_HH.json + docs/hourly_*_mountain.json

Throttle: sequential GETs with 0.4–0.8 s delay. Failures are caught per station.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from http_util import http_get  # noqa: E402

MADRID = ZoneInfo("Europe/Madrid")
UA = "mc-dades-acumulades/1.0 (+https://github.com/jnoya99/mc-dades-acumulades; mountain)"
MANIFEST_NAME = "stations_mountain.json"
CCAA = "MOUNTAIN"
KEEP_HOURS_DAYS = 14
PANEL_VERSION = 1
HOURLY_VERSION = 1

# Hourly only where sub-daily rain deltas are useful (skip heavy CMI HTML hourly).
HOURLY_SOURCES = frozenset({"MeteOsona", "Meteoguilleries", "Meteocadí"})

DELTA_NOTE = (
    "Ph = max(0, cum_now - cum_prev) within the same Madrid calendar day. "
    "If cum drops (midnight/station reset), Ph = max(0, cum_now). "
    "First sample for a station sets Ph = 0 (baseline only)."
)
SNAPSHOT_NOTE = (
    "seriesHR/HX/HN/W/WDG/WA/T/TX/TN are instantaneous snapshots at the "
    "capture hour (Europe/Madrid), not deltas. Only seriesPh uses rain deltas."
)

# Field coverage notes (also mirrored in README)
SOURCE_FIELD_GAPS = {
    "MeteOsona": (
        "Full live+day JSON in `var estacio={...}`: T/HR/W/rain day cum "
        "(actuals.pluja) + day max/min. Good for hourly Ph deltas."
    ),
    "Meteoguilleries": (
        "Live from last `arrayDades10` row: temperatura*/humitat*/vent*/plujaAra. "
        "plujaAvui often null — use plujaAra as day cum when present. "
        "Pressure sometimes uncalibrated at high elev."
    ),
    "ClimaMeteoInfo": (
        "HTML gauges (temp/hum/wind/rain-now). Humidity may be broken (site warning). "
        "Min/max from gauge-max near gauges. Daily only by default (large pages)."
    ),
    "Meteocadí": (
        "WeatherLink `summaryData/{deviceUrlToken}` on weatherlink.com. "
        "Token from embed URL `/embeddablePage/show/<token>/`. "
        "Rain DAY from aggregatedValues; Temp/Hum/Wind from curr/highLow."
    ),
}

SNAPSHOT_SERIES = (
    ("seriesHR", "Hum.act"),
    ("seriesHX", "Hum.max"),
    ("seriesHN", "Hum.min"),
    ("seriesW", "Vient.max"),
    ("seriesWDG", "Vient.dir"),
    ("seriesWA", "Vient.act"),
    ("seriesT", "Temp.act"),
    ("seriesTX", "Temp.max"),
    ("seriesTN", "Temp.min"),
)


def _f(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        return None if x != x else x
    s = str(v).strip().replace(",", ".")
    if not s or s in {"-", "--", "None", "null", "nan", "NA"}:
        return None
    # strip units
    s = re.sub(r"[^\d.\-]+$", "", s)
    s = re.sub(r"[^\d.\-eE+].*$", "", s) if re.search(r"[a-zA-Z%°]", s) else s
    # handle "14.9 °C" / "0 mm"
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)", str(v).replace(",", "."))
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def _json_num(v: float | None) -> int | float | None:
    if v is None or v != v:
        return None
    if float(v) == int(v) and abs(v) < 1e15:
        return int(v)
    return float(v)


def madrid_today_iso() -> str:
    return datetime.now(MADRID).date().isoformat()


def madrid_hour_stamp(now: datetime | None = None) -> str:
    dt = now or datetime.now(MADRID)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MADRID)
    else:
        dt = dt.astimezone(MADRID)
    return dt.strftime("%Y-%m-%dT%H:00")


def load_manifest(path: Path) -> list[dict[str, Any]]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    stations = obj.get("stations") if isinstance(obj, dict) else obj
    if not isinstance(stations, list):
        raise SystemExit(f"bad manifest: {path}")
    return stations


def throttle(delay_lo: float = 0.4, delay_hi: float = 0.8) -> None:
    time.sleep(random.uniform(delay_lo, delay_hi))


def fetch_html(url: str, timeout: int = 60) -> str:
    raw = http_get(
        url,
        timeout=timeout,
        user_agent=UA,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "ca,es;q=0.9,en;q=0.8",
        },
    )
    # Guess encoding
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def fetch_json(url: str, timeout: int = 60) -> Any:
    raw = http_get(
        url,
        timeout=timeout,
        user_agent=UA,
        headers={"Accept": "application/json,*/*"},
    )
    return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _extract_js_object_after(html: str, pattern: str) -> dict[str, Any] | None:
    m = re.search(pattern, html)
    if not m:
        return None
    i = m.end() - 1  # points at '{'
    if html[i] != "{":
        # pattern may end before brace
        brace = html.find("{", m.start())
        if brace < 0:
            return None
        i = brace
    depth = 0
    in_str = False
    esc = False
    quote = ""
    for j in range(i, len(html)):
        ch = html[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
            continue
        if ch in "\"'":
            in_str = True
            quote = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                raw = html[i : j + 1]
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return None
    return None


def _extract_js_array_after(html: str, pattern: str) -> list[Any] | None:
    m = re.search(pattern, html)
    if not m:
        return None
    i = html.find("[", m.start())
    if i < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    quote = ""
    for j in range(i, len(html)):
        ch = html[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
            continue
        if ch in "\"'":
            in_str = True
            quote = ch
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[i : j + 1])
                except json.JSONDecodeError:
                    return None
    return None


def parse_meteososona(html: str, meta: dict[str, Any]) -> dict[str, Any]:
    obj = _extract_js_object_after(html, r"var\s+estacio\s*=\s*\{")
    if not obj:
        raise ValueError("MeteOsona: var estacio not found")
    dades = obj.get("dades") or {}
    act = dades.get("actuals") or {}
    dia = dades.get("dia") or {}
    rain = _f(act.get("pluja"))
    if rain is None:
        rain = _f(dia.get("pluja_maxima"))
    return {
        "id": meta["id"],
        "name": meta.get("name") or obj.get("nom") or meta["id"],
        "lon": meta.get("lon") if meta.get("lon") is not None else _f(obj.get("longitud")),
        "lat": meta.get("lat") if meta.get("lat") is not None else _f(obj.get("latitud")),
        "elev": meta.get("elev_m") if meta.get("elev_m") is not None else _f(obj.get("altitud")),
        "source": "MeteOsona",
        "cum": rain,
        "Precip.diaria": rain,
        "Precip.total": rain,
        "Temp.act": _f(act.get("temperatura")),
        "Temp.max": _f(dia.get("temperatura_maxima")),
        "Temp.min": _f(dia.get("temperatura_minima")),
        "Hum.act": _f(act.get("humitat")),
        "Hum.max": _f(dia.get("humitat_maxima")),
        "Hum.min": _f(dia.get("humitat_minima")),
        "Vient.act": _f(act.get("vent")),
        "Vient.max": _f(dia.get("vent_maxima")),
        "Vient.dir": _f(act.get("direccio")),
        "obs_time": act.get("hora") or dia.get("hora"),
        "day": dia.get("data"),
    }


def parse_meteoguilleries(html: str, meta: dict[str, Any]) -> dict[str, Any]:
    rows = _extract_js_array_after(html, r"var\s+arrayDades10\s*=")
    if not rows:
        raise ValueError("Meteoguilleries: arrayDades10 not found")
    last = rows[-1]
    if not isinstance(last, dict):
        raise ValueError("Meteoguilleries: bad last row")
    rain = _f(last.get("plujaAvui"))
    if rain is None:
        rain = _f(last.get("plujaAra"))
    return {
        "id": meta["id"],
        "name": meta.get("name") or meta["id"],
        "lon": meta.get("lon"),
        "lat": meta.get("lat"),
        "elev": meta.get("elev_m"),
        "source": "Meteoguilleries",
        "cum": rain,
        "Precip.diaria": rain,
        "Precip.total": rain,
        "Temp.act": _f(last.get("temperaturaAra")),
        "Temp.max": _f(last.get("temperaturaMax")),
        "Temp.min": _f(last.get("temperaturaMin")),
        "Hum.act": _f(last.get("humitatAra")),
        "Hum.max": _f(last.get("humitatMax")),
        "Hum.min": _f(last.get("humitatMin")),
        "Vient.act": _f(last.get("ventAra")),
        "Vient.max": _f(last.get("ventVelMax")),
        "Vient.dir": _f(last.get("ventGrausAra")),
        "obs_time": last.get("data"),
    }


def _cmi_gauge_now(html: str, icon: str) -> float | None:
    # Find gauge-now after a given map-icon class
    for m in re.finditer(
        rf"map-icon\s+{re.escape(icon)}[\s\S]{{0,800}}?<span class='gauge-now'[^>]*>\s*([^<]+)\s*</span>",
        html,
        re.I,
    ):
        return _f(m.group(1))
    return None


def _cmi_gauge_extremes_near(html: str, icon: str) -> tuple[float | None, float | None]:
    """Return (min, max) gauge values near an icon block (order varies)."""
    m = re.search(rf"map-icon\s+{re.escape(icon)}", html, re.I)
    if not m:
        return None, None
    block = html[m.start() : m.start() + 2500]
    # Values sit after inline SVGs inside gauge-min-val / gauge-max-val
    vmin = vmax = None
    mmin = re.search(
        r"gauge-min-val[^>]*>.*?</svg>\s*([\d.,]+)",
        block,
        re.I | re.S,
    )
    mmax = re.search(
        r"gauge-max-val[^>]*>.*?</svg>\s*([\d.,]+)",
        block,
        re.I | re.S,
    )
    if mmin:
        vmin = _f(mmin.group(1))
    if mmax:
        vmax = _f(mmax.group(1))
    if vmin is not None or vmax is not None:
        return vmin, vmax
    # Fallback: strip tags in the first gauge-max wrapper
    mm = re.search(r"class=.gauge-max.[\s\S]{0,400}</span>", block)
    if mm:
        plain = re.sub(r"<[^>]+>", " ", mm.group(0))
        nums = [_f(x) for x in re.findall(r"-?\d+(?:[.,]\d+)?", plain)]
        nums = [n for n in nums if n is not None]
        if len(nums) >= 2:
            return min(nums[0], nums[1]), max(nums[0], nums[1])
        if len(nums) == 1:
            return None, nums[0]
    return None, None


def parse_climameteoinfo(html: str, meta: dict[str, Any]) -> dict[str, Any]:
    t_act = _cmi_gauge_now(html, "temp-now")
    h_act = _cmi_gauge_now(html, "hum-now")
    w_act = _cmi_gauge_now(html, "wind-now")
    rain = _cmi_gauge_now(html, "rain-now")
    # Prefer day rain (mm) over rate (mm/h): first rain-now is usually day total
    # If we only got rate, try all rain-now spans
    rain_candidates = []
    for m in re.finditer(
        r"map-icon\s+rain-now[\s\S]{0,800}?<span class='gauge-now'[^>]*>\s*([^<]+)\s*</span>",
        html,
        re.I,
    ):
        raw = m.group(1).strip()
        if "mm/h" in raw.lower():
            continue
        rain_candidates.append(_f(raw))
    rain_candidates = [x for x in rain_candidates if x is not None]
    if rain_candidates:
        rain = rain_candidates[0]

    t_min, t_max = _cmi_gauge_extremes_near(html, "temp-now")
    h_min, h_max = _cmi_gauge_extremes_near(html, "hum-now")
    _w_min, w_max = _cmi_gauge_extremes_near(html, "wind-now")

    # Wind direction from "90°" style gauge near compass — best-effort
    wdir = None
    mdir = re.search(r"air-gauge3-dual'>[\s\S]*?(\d{1,3})°", html)
    if mdir:
        wdir = _f(mdir.group(1))

    return {
        "id": meta["id"],
        "name": meta.get("name") or meta["id"],
        "lon": meta.get("lon"),
        "lat": meta.get("lat"),
        "elev": meta.get("elev_m"),
        "source": "ClimaMeteoInfo",
        "cum": rain,
        "Precip.diaria": rain,
        "Precip.total": rain,
        "Temp.act": t_act,
        "Temp.max": t_max,
        "Temp.min": t_min,
        "Hum.act": h_act,
        "Hum.max": h_max,
        "Hum.min": h_min,
        "Vient.act": w_act,
        "Vient.max": w_max if w_max is not None else w_act,
        "Vient.dir": wdir,
    }


def _wl_token_from_url(url: str) -> str | None:
    m = re.search(r"/embeddablePage/show/([0-9a-fA-F]{16,})/", url)
    return m.group(1) if m else None


def _wl_find(values: list[dict], *names: str) -> float | None:
    want = {n.lower() for n in names}
    for v in values:
        n = (v.get("sensorDataName") or v.get("displayName") or "").lower()
        if n in want:
            return _f(v.get("value") if v.get("value") is not None else v.get("reportedValue"))
    return None


def parse_meteocadi_summary(summary: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    curr = summary.get("currConditionValues") or []
    hi = summary.get("highLowValues") or []
    agg = summary.get("aggregatedValues") or []
    rain = None
    for a in agg:
        if (a.get("sensorDataName") or "").lower() == "rain":
            raw = (a.get("rawValues") or {}).get("DAY")
            rain = _f(raw)
            break
    if rain is None:
        rain = _wl_find(curr, "rain storm", "60 min rain total")

    return {
        "id": meta["id"],
        "name": meta.get("name") or meta["id"],
        "lon": meta.get("lon"),
        "lat": meta.get("lat"),
        "elev": meta.get("elev_m"),
        "source": "Meteocadí",
        "cum": rain,
        "Precip.diaria": rain,
        "Precip.total": rain,
        "Temp.act": _wl_find(curr, "temp"),
        "Temp.max": _wl_find(hi, "high temp"),
        "Temp.min": _wl_find(hi, "low temp"),
        "Hum.act": _wl_find(curr, "hum"),
        "Hum.max": _wl_find(hi, "high hum"),
        "Hum.min": _wl_find(hi, "low hum"),
        "Vient.act": _wl_find(curr, "wind speed"),
        "Vient.max": _wl_find(hi, "high wind speed"),
        "Vient.dir": _wl_find(curr, "wind direction"),
        "wl_last_received": summary.get("lastReceived"),
    }


def capture_station(meta: dict[str, Any]) -> dict[str, Any]:
    source = meta.get("source") or ""
    url = meta.get("url") or ""
    if not url:
        raise ValueError("missing url")

    if source == "MeteOsona":
        html = fetch_html(url)
        return parse_meteososona(html, meta)
    if source == "Meteoguilleries":
        html = fetch_html(url)
        return parse_meteoguilleries(html, meta)
    if source == "ClimaMeteoInfo":
        html = fetch_html(url)
        return parse_climameteoinfo(html, meta)
    if source == "Meteocadí":
        token = _wl_token_from_url(url)
        if not token:
            raise ValueError(f"Meteocadí: cannot derive WeatherLink token from {url}")
        summary_url = f"https://www.weatherlink.com/embeddablePage/summaryData/{token}"
        summary = fetch_json(summary_url)
        return parse_meteocadi_summary(summary, meta)
    raise ValueError(f"unknown source: {source}")


# ---------------------------------------------------------------------------
# Writers / builders
# ---------------------------------------------------------------------------

def write_json(path: Path, payload: Any, *, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")


def station_meta_list(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "mc_id": r["id"],
                "name": r.get("name") or r["id"],
                "lon": _json_num(_f(r.get("lon"))),
                "lat": _json_num(_f(r.get("lat"))),
                "elev": _json_num(_f(r.get("elev"))),
                "source": r.get("source"),
            }
        )
    out.sort(key=lambda x: x["id"])
    return out


def build_panel_mountain(
    stations_meta: list[dict[str, Any]],
    day_iso: str,
    rows_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    series: dict[str, dict[str, dict[str, Any]]] = {}
    for st in stations_meta:
        sid = st["id"]
        r = rows_by_id.get(sid)
        if not r:
            continue
        cell = {
            "P": _json_num(_f(r.get("Precip.diaria") if r.get("Precip.diaria") is not None else r.get("cum"))),
            "TX": _json_num(_f(r.get("Temp.max"))),
            "N": _json_num(_f(r.get("Temp.min"))),
            "HX": _json_num(_f(r.get("Hum.max"))),
            "HR": _json_num(_f(r.get("Hum.act"))),
            "W": _json_num(_f(r.get("Vient.max") if r.get("Vient.max") is not None else r.get("Vient.act"))),
            "WDG": _json_num(_f(r.get("Vient.dir"))),
        }
        # drop empty cells
        if all(v is None for v in cell.values()):
            continue
        series[sid] = {day_iso: cell}
    built = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "version": PANEL_VERSION,
        "built": built,
        "ccaa": CCAA,
        "stations": stations_meta,
        "days": [day_iso],
        "series": series,
        "sources": sorted({s.get("source") for s in stations_meta if s.get("source")}),
        "field_gaps": SOURCE_FIELD_GAPS,
        "note": (
            "Additive mountain panel. ESCAT consumers should keep using panel.json; "
            "IDs are namespaced MO_/MG_/CMI_/MCADI_ and do not collide with MC_*."
        ),
    }


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
    return max(0.0, cum)


def list_hourly_files(hourly_dir: Path) -> list[tuple[str, Path]]:
    if not hourly_dir.is_dir():
        return []
    out: list[tuple[str, Path]] = []
    for path in sorted(hourly_dir.glob(f"{CCAA}_????????_??.json")):
        m = re.match(rf"^{CCAA}_(\d{{4}})(\d{{2}})(\d{{2}})_(\d{{2}})$", path.stem)
        if not m:
            continue
        y, mo, d, hh = m.groups()
        out.append((f"{y}-{mo}-{d}T{hh}:00", path))
    out.sort(key=lambda x: x[0])
    return out


def prune_hourly(hourly_dir: Path, keep_days: int = KEEP_HOURS_DAYS) -> int:
    cutoff = (datetime.now(MADRID).date() - timedelta(days=keep_days - 1)).isoformat()
    deleted = 0
    for hour, path in list_hourly_files(hourly_dir):
        if hour[:10] < cutoff:
            path.unlink(missing_ok=True)
            deleted += 1
    return deleted


def build_hourly_mountain(
    stations_meta: list[dict[str, Any]],
    hourly_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    files = list_hourly_files(hourly_dir)
    hours = [h for h, _ in files]
    ids = [s["id"] for s in stations_meta]
    series_ph: dict[str, dict[str, Any]] = {i: {} for i in ids}
    snap: dict[str, dict[str, dict[str, Any]]] = {
        k: {i: {} for i in ids} for k, _ in SNAPSHOT_SERIES
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
            sid = (st.get("id") or "").strip()
            if sid not in series_ph:
                continue
            cum = _f(st.get("cum"))
            if cum is None:
                cum = _f(st.get("Precip.total"))
            if cum is None:
                cum = _f(st.get("Precip.diaria"))
            if cum is not None:
                ph = _ph_delta(sid, cum, hour, day, prev_cum, prev_hour)
                series_ph[sid][hour] = _json_num(ph)
                prev_cum[sid] = cum
                prev_hour[sid] = hour
            for skey, raw_field in SNAPSHOT_SERIES:
                val = _f(st.get(raw_field))
                if val is None:
                    continue
                snap[skey][sid][hour] = _json_num(val)

    def nonempty(d: dict[str, dict]) -> dict[str, dict]:
        return {k: v for k, v in d.items() if v}

    built = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rain = {
        "version": HOURLY_VERSION,
        "built": built,
        "ccaa": CCAA,
        "hours": hours,
        "stations": stations_meta,
        "series": nonempty(series_ph),
        "delta_note": DELTA_NOTE,
        "retention_days": KEEP_HOURS_DAYS,
        "field_gaps": SOURCE_FIELD_GAPS,
    }
    meteo: dict[str, Any] = {
        "version": HOURLY_VERSION,
        "built": built,
        "ccaa": CCAA,
        "hours": hours,
        "stations": stations_meta,
        "seriesPh": nonempty(series_ph),
        "delta_note": DELTA_NOTE,
        "snapshot_note": SNAPSHOT_NOTE,
        "retention_days": KEEP_HOURS_DAYS,
        "field_gaps": SOURCE_FIELD_GAPS,
    }
    for skey, _ in SNAPSHOT_SERIES:
        meteo[skey] = nonempty(snap[skey])
    return rain, meteo


def run_capture(
    root: Path,
    mode: str,
    *,
    force: bool = False,
    limit: int | None = None,
    only_ids: set[str] | None = None,
    delay_lo: float = 0.4,
    delay_hi: float = 0.8,
) -> dict[str, Any]:
    manifest_path = root / "data" / MANIFEST_NAME
    stations = load_manifest(manifest_path)
    if only_ids:
        stations = [s for s in stations if s.get("id") in only_ids]
    if mode == "hourly":
        stations = [s for s in stations if s.get("source") in HOURLY_SOURCES]
    if limit is not None:
        stations = stations[:limit]

    day_iso = madrid_today_iso()
    hour = madrid_hour_stamp()
    ymd = day_iso.replace("-", "")
    hh = hour[11:13]

    if mode == "daily":
        out_path = root / "data" / "daily" / f"{CCAA}_{ymd}.json"
    else:
        out_path = root / "data" / "hourly" / f"{CCAA}_{ymd}_{hh}.json"

    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    fetched = False

    if not force and out_path.exists() and mode == "daily":
        # Rebuild docs from existing raw
        try:
            prev = json.loads(out_path.read_text(encoding="utf-8"))
            rows = list(prev.get("stations") or [])
        except (OSError, json.JSONDecodeError):
            rows = []
    else:
        fetched = True
        for i, meta in enumerate(stations):
            sid = meta.get("id") or "?"
            try:
                row = capture_station(meta)
                rows.append(row)
            except Exception as e:  # noqa: BLE001 — per-station isolation
                errors.append({"id": sid, "error": f"{type(e).__name__}: {e}"})
            if i < len(stations) - 1:
                throttle(delay_lo, delay_hi)

        payload = {
            "ccaa": CCAA,
            "mode": mode,
            "date" if mode == "daily" else "hour": day_iso if mode == "daily" else hour,
            "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "n_stations": len(rows),
            "n_errors": len(errors),
            "errors": errors,
            "stations": rows,
            "field_gaps": SOURCE_FIELD_GAPS,
        }
        # fix key for hour mode — can't use conditional in dict literal like that cleanly above
        if mode == "hourly":
            payload.pop("date", None)
            payload["hour"] = hour
        else:
            payload.pop("hour", None)
            payload["date"] = day_iso
        write_json(out_path, payload)

    # Station meta for docs: prefer manifest coords + successful rows
    meta_by_id = {s["id"]: s for s in load_manifest(manifest_path)}
    docs_stations = []
    for r in rows:
        m = meta_by_id.get(r["id"], {})
        docs_stations.append(
            {
                "id": r["id"],
                "mc_id": r["id"],
                "name": r.get("name") or m.get("name") or r["id"],
                "lon": _json_num(_f(r.get("lon") if r.get("lon") is not None else m.get("lon"))),
                "lat": _json_num(_f(r.get("lat") if r.get("lat") is not None else m.get("lat"))),
                "elev": _json_num(_f(r.get("elev") if r.get("elev") is not None else m.get("elev_m"))),
                "source": r.get("source") or m.get("source"),
            }
        )
    # For hourly docs include all hourly-capable manifest stations (stable list)
    if mode == "hourly":
        docs_stations = []
        for m in load_manifest(manifest_path):
            if m.get("source") not in HOURLY_SOURCES:
                continue
            docs_stations.append(
                {
                    "id": m["id"],
                    "mc_id": m["id"],
                    "name": m.get("name") or m["id"],
                    "lon": _json_num(_f(m.get("lon"))),
                    "lat": _json_num(_f(m.get("lat"))),
                    "elev": _json_num(_f(m.get("elev_m"))),
                    "source": m.get("source"),
                }
            )
        docs_stations.sort(key=lambda x: x["id"])

    docs_dir = root / "docs"
    if mode == "daily":
        # Use full manifest as station list for panel (stable), series only where we have data
        all_meta = []
        for m in load_manifest(manifest_path):
            all_meta.append(
                {
                    "id": m["id"],
                    "mc_id": m["id"],
                    "name": m.get("name") or m["id"],
                    "lon": _json_num(_f(m.get("lon"))),
                    "lat": _json_num(_f(m.get("lat"))),
                    "elev": _json_num(_f(m.get("elev_m"))),
                    "source": m.get("source"),
                }
            )
        all_meta.sort(key=lambda x: x["id"])
        rows_by_id = {r["id"]: r for r in rows}
        panel = build_panel_mountain(all_meta, day_iso, rows_by_id)
        write_json(docs_dir / "panel_mountain.json", panel, compact=True)
        return {
            "ok": True,
            "mode": mode,
            "fetched": fetched,
            "raw": str(out_path),
            "panel": str(docs_dir / "panel_mountain.json"),
            "n_ok": len(rows),
            "n_errors": len(errors),
            "errors": errors[:20],
            "date": day_iso,
        }

    # hourly
    hourly_dir = root / "data" / "hourly"
    pruned = prune_hourly(hourly_dir)
    rain, meteo = build_hourly_mountain(docs_stations, hourly_dir)
    write_json(docs_dir / "hourly_rain_mountain.json", rain, compact=True)
    write_json(docs_dir / "hourly_meteo_mountain.json", meteo, compact=True)
    return {
        "ok": True,
        "mode": mode,
        "fetched": fetched,
        "raw": str(out_path),
        "hourly_rain": str(docs_dir / "hourly_rain_mountain.json"),
        "hourly_meteo": str(docs_dir / "hourly_meteo_mountain.json"),
        "n_ok": len(rows),
        "n_errors": len(errors),
        "errors": errors[:20],
        "hour": hour,
        "pruned": pruned,
        "n_hours": len(rain["hours"]),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=ROOT)
    p.add_argument("--mode", choices=("daily", "hourly"), required=True)
    p.add_argument("--force", action="store_true")
    p.add_argument("--limit", type=int, default=None, help="capture only first N (debug)")
    p.add_argument(
        "--only",
        default=None,
        help="comma-separated station ids (smoke test)",
    )
    p.add_argument("--delay-lo", type=float, default=0.4)
    p.add_argument("--delay-hi", type=float, default=0.8)
    args = p.parse_args(argv)

    only_ids = None
    if args.only:
        only_ids = {x.strip() for x in args.only.split(",") if x.strip()}

    try:
        info = run_capture(
            args.root,
            args.mode,
            force=args.force,
            limit=args.limit,
            only_ids=only_ids,
            delay_lo=args.delay_lo,
            delay_hi=args.delay_hi,
        )
    except urllib.error.HTTPError as e:
        print(f"HTTP error: {e.code} {e.reason}", file=sys.stderr)
        return 2
    except urllib.error.URLError as e:
        print(f"URL error: {e.reason}", file=sys.stderr)
        return 2

    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0 if info.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
