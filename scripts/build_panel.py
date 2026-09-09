#!/usr/bin/env python3
"""Merge data/daily/*.csv|json + stations_keep into docs/panel.json.

Panel schema matches meteoclimatic-bolets/tools/build_explorer_mc.py:
  version, built, ccaa, stations[{id,mc_id,name,lon,lat,elev}],
  days[YYYY-MM-DD], series{sid->{day->{P,TX,N,HX,HR,W,WDG}}}, checksum
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CCAA = "ESCAT"

COL_MAP = {
    "P": ("Precip.diaria", "Precip.total"),
    "TX": ("Temp.max",),
    "N": ("Temp.min",),
    "HX": ("Hum.max",),
    "HR": ("Hum.act",),
    "W": ("Vient.max",),
    "WDG": ("Vient.dir",),
}
SERIES_CAMPS = ("P", "TX", "N", "HX", "HR", "W", "WDG")


def _num(v: Any) -> float | None:
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s == "-" or s.lower() in {"na", "nan", "null", "none"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _json_num(v: float | None) -> int | float | None:
    if v is None:
        return None
    if v != v:  # NaN
        return None
    if float(v) == int(v) and abs(v) < 1e15:
        return int(v)
    return float(v)


def _cell(row: dict, *keys: str) -> float | None:
    for k in keys:
        if k in row:
            n = _num(row.get(k))
            if n is not None:
                return n
    return None


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _payload_checksum(stations: list, days: list, series: dict) -> str:
    canonical = _canonical_json({"stations": stations, "days": days, "series": series})
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_keep(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Keep list not found: {path}")
    rows: list[dict] = []
    with path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            lon = _num(r.get("xema_style_lon") or r.get("lon"))
            lat = _num(r.get("xema_style_lat") or r.get("lat"))
            if lon is None or lat is None:
                continue
            elev = _num(r.get("xema_style_elev") or r.get("elev"))
            sid = (r.get("xema_style_id") or r.get("bolets_id") or "").strip()
            mc_id = (r.get("id") or "").strip()
            if not sid:
                if mc_id:
                    sid = "MC_" + mc_id if not mc_id.startswith("MC_") else mc_id
                else:
                    continue
            rows.append(
                {
                    "id": sid,
                    "mc_id": mc_id or sid.replace("MC_", "", 1),
                    "name": (r.get("name") or "").strip(),
                    "lon": lon,
                    "lat": lat,
                    "elev": elev if elev is not None else None,
                }
            )
    rows.sort(key=lambda x: x["id"])
    return rows


def date_from_daily_stem(stem: str, ccaa: str = CCAA) -> str | None:
    prefix = ccaa + "_"
    if not stem.startswith(prefix) or len(stem) != len(prefix) + 8:
        return None
    ymd = stem[len(prefix) :]
    if not ymd.isdigit():
        return None
    return f"{ymd[0:4]}-{ymd[4:6]}-{ymd[6:8]}"


def list_daily_files(daily_dir: Path, ccaa: str = CCAA) -> list[tuple[str, Path]]:
    """Return (iso_day, path) preferring .csv over .json when both exist."""
    if not daily_dir.is_dir():
        return []
    by_day: dict[str, Path] = {}
    for path in sorted(daily_dir.glob(f"{ccaa}_????????.*")):
        day = date_from_daily_stem(path.stem, ccaa)
        if not day:
            continue
        if path.suffix.lower() == ".csv":
            by_day[day] = path
        elif path.suffix.lower() == ".json" and day not in by_day:
            by_day[day] = path
    return sorted(by_day.items())


def rows_from_daily(path: Path) -> list[dict]:
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    if path.suffix.lower() == ".json":
        obj = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            for key in ("stations", "rows", "data"):
                if isinstance(obj.get(key), list):
                    return obj[key]
        raise ValueError(f"Unrecognized JSON daily shape: {path}")
    raise ValueError(f"Unsupported daily file: {path}")


def build_payload(keep_rows: list[dict], daily_dir: Path, ccaa: str = CCAA) -> dict:
    keep_by_mc = {r["mc_id"]: r for r in keep_rows}
    keep_ids = set(keep_by_mc.keys())

    days: list[str] = []
    series: dict[str, dict[str, dict[str, float | None]]] = {r["id"]: {} for r in keep_rows}

    for day, path in list_daily_files(daily_dir, ccaa):
        n_keep_rows = 0
        for row in rows_from_daily(path):
            mc_id = (row.get("id") or "").strip()
            if mc_id not in keep_ids:
                continue
            st = keep_by_mc[mc_id]
            day_vals: dict[str, float | None] = {}
            for camp, keys in COL_MAP.items():
                day_vals[camp] = _json_num(_cell(row, *keys))
            if all(v is None for v in day_vals.values()):
                continue
            series[st["id"]][day] = day_vals
            n_keep_rows += 1
        if n_keep_rows > 0:
            days.append(day)

    days = sorted(set(days))
    stations = [
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
    series_out = {sid: byday for sid, byday in series.items() if byday}

    built = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    checksum = _payload_checksum(stations, days, series_out)
    return {
        "version": 1,
        "built": built,
        "ccaa": ccaa,
        "stations": stations,
        "days": days,
        "series": series_out,
        "checksum": checksum,
    }


def write_panel(payload: dict, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    out_path.write_text(text, encoding="utf-8")
    # self-check checksum
    got = _payload_checksum(payload["stations"], payload["days"], payload["series"])
    if got != payload["checksum"]:
        raise SystemExit(f"checksum self-mismatch: {got} != {payload['checksum']}")
    return out_path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=ROOT)
    p.add_argument("--keep", type=Path, default=None)
    p.add_argument("--daily", type=Path, default=None)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--ccaa", default=CCAA)
    args = p.parse_args(argv)

    root = args.root
    keep = args.keep or (root / "data" / "stations_keep.csv")
    daily = args.daily or (root / "data" / "daily")
    out = args.out or (root / "docs" / "panel.json")

    keep_rows = load_keep(keep)
    payload = build_payload(keep_rows, daily, args.ccaa)
    write_panel(payload, out)

    print(
        json.dumps(
            {
                "ok": True,
                "out": str(out),
                "bytes": out.stat().st_size,
                "n_stations": len(payload["stations"]),
                "n_series_stations": len(payload["series"]),
                "n_days": len(payload["days"]),
                "days": payload["days"],
                "checksum": payload["checksum"],
                "built": payload["built"],
                "keys": list(payload.keys()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
