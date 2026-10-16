#!/usr/bin/env python3
"""Per station-day effective-rain fractions for the NON-XEMA networks.

Same table and model as build_rain_intensity.py (rain_intensity_xema.json), built
from the hourly archives this repo already captures:

  escat     Meteoclimatic ESCAT (MC_*)          data/hourly/ESCAT_*     -> docs/rain_intensity_escat.json
  mountain  MeteOsona / Meteoguilleries / Meteocadí (MO_*, MG_*, MCADI_)
                                                data/hourly/MOUNTAIN_*  -> docs/rain_intensity_mountain.json
  aemet     AEMET OpenData conventional (AE_*)  data/hourly/AEMET_*     -> docs/rain_intensity_aemet.json

Hourly Ph comes from the SAME builders that publish hourly_rain*.json
(capture_hourly / capture_mountain / capture_aemet_hourly), so the numbers are
identical to the public hourly files.  A station-day is kept only when its
hourly profile is trustworthy:

  * ESCAT / mountain (cumulative day totals, snapshot each capture):
      - every capture with rain must come <= 1 h after the previous capture of
        that station (a longer gap would lump several hours into one burst);
      - there is a capture at or after 20:00 Madrid;
  * AEMET (prec per hour): all 24 hours present.  The day window is the one of
    AEMET's daily value the app uses (07 UTC D -> 07 UTC D+1), verified against
    panel_aemet.json (370 matches vs 2 mismatches; calendar day: 223 vs 148);
  * all sources: the day's hourly sum agrees with the daily panel P the app
    uses (|sum - P| <= max(0.5 mm, 15 %)), and P >= 0.1 mm.
  Rejected days are simply absent: the app keeps its daily model for them.

Days fully covered by the raw archive (and before today) are re-evaluated on
each run; older days are kept from the previous output (raw is pruned at
~14 days) up to --retain days.  --hourly-dir lets a one-off backfill read raw
files restored from git history.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_aemet_hourly as cah  # noqa: E402
import capture_hourly as chy  # noqa: E402
import capture_mountain as cmt  # noqa: E402
from build_panel import load_keep  # noqa: E402
from build_rain_intensity import (  # noqa: E402
    FC, P0, PET_REF, SCALE, THETAS, VERSION, fractions_from_hours, load_archive,
)
from capture_aemet import load_stations  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MADRID = ZoneInfo("Europe/Madrid")
UTC = timezone.utc
TOL_MM, TOL_REL, MIN_P = 0.5, 0.15, 0.1

SOURCES = {
    "escat": {"prefix": "ESCAT", "panel": "panel.json", "out": "rain_intensity_escat.json",
              "label": "Meteoclimatic ESCAT hourly snapshots (cumulative deltas, hourly_rain.json builder)"},
    "mountain": {"prefix": "MOUNTAIN", "panel": "panel_mountain.json", "out": "rain_intensity_mountain.json",
                 "label": "MeteOsona/Meteoguilleries/Meteocadí hourly snapshots (hourly_rain_mountain.json builder)"},
    "aemet": {"prefix": "AEMET", "panel": "panel_aemet.json", "out": "rain_intensity_aemet.json",
              "label": "AEMET OpenData observacion convencional prec (hourly_rain_aemet.json builder)"},
}


def _hour_key(stem: str) -> str:
    s = stem.split("_")
    return f"{s[1][:4]}-{s[1][4:6]}-{s[1][6:8]}T{s[2]}:00"


def presence(hourly_dir: Path, prefix: str, fields: list[str], idmap: dict[str, str] | None) -> dict[str, list[str]]:
    """sid -> sorted Madrid hour stamps where the station reported a rain value."""
    out: dict[str, list[str]] = {}
    for f in sorted(hourly_dir.glob(f"{prefix}_????????_??.json")):
        try:
            st_list = json.loads(f.read_text("utf-8")).get("stations") or []
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        hk = _hour_key(f.stem)
        for st in st_list:
            if not isinstance(st, dict) or all(st.get(k) is None for k in fields):
                continue
            sid = str(st.get("id") or "").strip()
            if idmap is not None:
                sid = idmap.get(sid, "")
            if sid:
                out.setdefault(sid, []).append(hk)
    return out


def ph_series(src: str, root: Path, hourly_dir: Path) -> tuple[dict[str, dict[str, float]], dict[str, list[str]]]:
    if src == "escat":
        keep = load_keep(root / "data" / "stations_keep.csv")
        ser = chy.build_hourly_rain(keep, hourly_dir)["series"]
        pres = presence(hourly_dir, "ESCAT", ["cum", "Precip.total", "Precip.diaria"], {r["mc_id"]: r["id"] for r in keep})
    elif src == "mountain":
        man = [m for m in cmt.load_manifest(root / "data" / "stations_mountain.json") if m.get("source") in cmt.HOURLY_SOURCES]
        ser = cmt.build_hourly_mountain([{"id": m["id"]} for m in man], hourly_dir)[0]["series"]
        pres = presence(hourly_dir, "MOUNTAIN", ["cum", "Precip.total", "Precip.diaria"], None)
    else:
        ser = cah.build_hourly_rain(load_stations(root / "data" / "stations_aemet.json"), hourly_dir)["series"]
        pres = presence(hourly_dir, "AEMET", ["prec"], None)
    return {k: {h: float(v) for h, v in d.items() if v is not None} for k, d in ser.items()}, pres


def aemet_window(day: str) -> list[str]:
    d = date.fromisoformat(day)
    t0 = datetime(d.year, d.month, d.day, 7, tzinfo=UTC)
    return [(t0 + timedelta(hours=h + 1)).astimezone(MADRID).strftime("%Y-%m-%dT%H:00") for h in range(24)]


def day_profile(src: str, sid: str, day: str, ser: dict[str, float], pres: list[str]) -> tuple[list[float] | None, str]:
    if src == "aemet":
        hs = aemet_window(day)
        ps = set(pres)
        if not all(h in ps for h in hs):
            return None, "incomplete"
        return [max(0.0, ser.get(h, 0.0)) for h in hs], ""
    caps = [datetime.fromisoformat(h) for h in pres]
    d0 = datetime.fromisoformat(day + "T00:00")
    d1 = d0 + timedelta(days=1)
    inday = [i for i, t in enumerate(caps) if d0 <= t < d1]
    if not inday:
        return None, "nocapture"
    if caps[inday[-1]].hour < 20:
        return None, "late"
    ph = [0.0] * 24
    for i in inday:
        hk = caps[i].strftime("%Y-%m-%dT%H:00")
        v = max(0.0, ser.get(hk, 0.0))
        if v > 0 and (i == 0 or (caps[i] - caps[i - 1]) > timedelta(hours=1)):
            return None, "rain_after_gap"
        ph[caps[i].hour] += v
    return ph, ""


def evaluate(src: str, root: Path, hourly_dir: Path, today: date) -> tuple[dict[str, dict[str, tuple[int, ...]]], set[str], dict[str, int]]:
    ser, pres = ph_series(src, root, hourly_dir)
    panel = json.loads((root / "docs" / SOURCES[src]["panel"]).read_text("utf-8")).get("series") or {}
    raw_days = sorted({h[:10] for hs in pres.values() for h in hs})
    if not raw_days:
        return {}, set(), {}
    first = date.fromisoformat(raw_days[0]) + timedelta(days=1)    # first raw day may be partial
    last = min(date.fromisoformat(raw_days[-1]), today - timedelta(days=1))
    if src == "aemet":
        last = min(last, date.fromisoformat(raw_days[-1]) - timedelta(days=1))  # window ends 09h next day
    window = {(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)} if last >= first else set()
    rec: dict[str, dict[str, tuple[int, ...]]] = {}
    stats: dict[str, int] = {}
    for sid, days in panel.items():
        for day, cell in days.items():
            if day not in window or not isinstance(cell, dict):
                continue
            P = cell.get("P")
            if not isinstance(P, (int, float)) or P < MIN_P:
                continue
            stats["rainy"] = stats.get("rainy", 0) + 1
            ph, why = day_profile(src, sid, day, ser.get(sid, {}), pres.get(sid, []))
            if ph is None:
                stats[why] = stats.get(why, 0) + 1
                continue
            s = sum(ph)
            if abs(s - P) > max(TOL_MM, TOL_REL * P):
                stats["mismatch_daily"] = stats.get("mismatch_daily", 0) + 1
                continue
            fr = fractions_from_hours(ph)
            if fr is None:
                stats["mismatch_daily"] = stats.get("mismatch_daily", 0) + 1
                continue
            rec.setdefault(sid, {})[day] = tuple([int(round(s * 10))] + fr)
            stats["kept"] = stats.get("kept", 0) + 1
    return rec, window, stats


def build(src: str, root: Path, hourly_dir: Path, retain: int, today: date) -> tuple[dict[str, Any], dict[str, int]]:
    path = root / "docs" / SOURCES[src]["out"]
    arch = load_archive(path)
    fresh, window, stats = evaluate(src, root, hourly_dir, today)
    for sid in list(arch):
        for d in list(arch[sid]):
            if d in window:
                del arch[sid][d]
    for sid, dd in fresh.items():
        arch.setdefault(sid, {}).update(dd)
    d0 = today - timedelta(days=retain - 1)
    series: dict[str, list[list[int]]] = {}
    for sid in sorted(arch):
        rr = [[(date.fromisoformat(d) - d0).days] + list(arch[sid][d])
              for d in sorted(arch[sid]) if d0 <= date.fromisoformat(d) <= today]
        if rr:
            series[sid] = rr
    return {
        "version": VERSION,
        "built": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": SOURCES[src]["label"],
        "day_window": "07 UTC D -> 07 UTC D+1 (AEMET daily value)" if src == "aemet" else "Madrid calendar day",
        "p0": P0, "fc": FC, "pet_ref": PET_REF, "theta_levels": list(THETAS), "scale": SCALE,
        "formula": "f(theta0) = peff/P of the app's hourly model; app interpolates linearly in theta",
        "quality": f"kept only if hourly profile complete and |sum-P_daily| <= max({TOL_MM} mm, {int(TOL_REL*100)} %)",
        "d0": d0.isoformat(), "d1": today.isoformat(), "n_days": retain,
        "fields": ["day_index", "P_x10", "f0", "f25", "f50", "f75", "f100"],
        "series": series,
    }, stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--source", choices=[*SOURCES, "all"], default="all")
    ap.add_argument("--hourly-dir", type=Path, default=None, help="raw hourly files (default ROOT/data/hourly)")
    ap.add_argument("--retain", type=int, default=400)
    args = ap.parse_args(argv)
    today = datetime.now(MADRID).date()
    rc = 0
    for src in (SOURCES if args.source == "all" else [args.source]):
        hdir = args.hourly_dir or (args.root / "data" / "hourly")
        if args.hourly_dir and (args.hourly_dir / SOURCES[src]["prefix"]).is_dir():
            hdir = args.hourly_dir / SOURCES[src]["prefix"]
        try:
            out, stats = build(src, args.root, hdir, args.retain, today)
        except Exception as e:  # one source failing must not stop the others
            print(json.dumps({"ok": False, "source": src, "error": repr(e)}))
            rc = 1
            continue
        path = args.root / "docs" / SOURCES[src]["out"]
        path.write_text(json.dumps(out, separators=(",", ":")), "utf-8")
        print(json.dumps({"ok": True, "source": src, "stations": len(out["series"]),
                          "station_days": sum(len(v) for v in out["series"].values()),
                          "bytes": path.stat().st_size, "this_run": stats}))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
