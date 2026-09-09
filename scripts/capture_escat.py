#!/usr/bin/env python3
"""Download current ESCAT snapshot from Meteoclimatic public feeds.

Primary source (no past-day ?d= — those return 401):
  - XML:  https://www.meteoclimatic.net/feed/xml/ESCAT
  - RSS:  https://www.meteoclimatic.net/feed/rss/ESCAT  (coords)

Saves:
  data/daily/ESCAT_YYYYMMDD.json
  data/daily/ESCAT_YYYYMMDD.csv
then rebuilds docs/panel.json via build_panel.py helpers.

Date stamp uses Europe/Madrid calendar day (daily Action ~23:30 Madrid).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_panel import build_payload, load_keep, write_panel  # noqa: E402

CCAA = "ESCAT"
UA = "mc-dades-acumulades/1.0 (+https://github.com/mc-dades-acumulades; daily archive)"
XML_URL = "https://www.meteoclimatic.net/feed/xml/{id}"
RSS_URL = "https://www.meteoclimatic.net/feed/rss/{id}"
MADRID = ZoneInfo("Europe/Madrid")

CSV_FIELDS = [
    "name",
    "id",
    "time",
    "lon",
    "lat",
    "alt",
    "Temp.max",
    "Temp.min",
    "Hum.max",
    "Hum.min",
    "Pres.max",
    "Pres.min",
    "Vient.max",
    "Precip.diaria",
    "Temp.act",
    "Hum.act",
    "Vient.dir",
    "Vient.act",
    "Precip.total",
    "source",
]


def _http_get(url: str, timeout: int = 90) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/xml,text/xml,*/*",
            "Referer": "https://www.meteoclimatic.net/",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _text(el: ET.Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    t = el.text.strip()
    return t if t else None


def _f(v: str | None) -> float | None:
    if v is None:
        return None
    s = v.strip().replace(",", ".")
    if s == "" or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _child(parent: ET.Element, *names: str) -> ET.Element | None:
    want = set(names)
    for ch in parent:
        if _local(ch.tag) in want:
            return ch
    return None


def _child_text(parent: ET.Element, *names: str) -> str | None:
    return _text(_child(parent, *names))


def parse_rss_coords(xml_bytes: bytes) -> dict[str, dict[str, Any]]:
    """id -> {name, lon, lat} from georss/geo point."""
    root = ET.fromstring(xml_bytes)
    out: dict[str, dict[str, Any]] = {}
    for item in root.iter():
        if _local(item.tag) != "item":
            continue
        title = None
        link = None
        point = None
        geo_lat = None
        geo_lon = None
        for ch in item:
            ln = _local(ch.tag)
            if ln == "title":
                title = _text(ch)
            elif ln == "link":
                link = _text(ch)
            elif ln == "point":
                # georss:point "lat lon"
                t = _text(ch)
                if t:
                    point = t
            elif ln == "Point":
                # geo:Point with lat/long children
                for g in ch:
                    gl = _local(g.tag)
                    if gl == "lat":
                        geo_lat = _f(_text(g))
                    elif gl in {"long", "lon"}:
                        geo_lon = _f(_text(g))
        if not link:
            continue
        m = re.search(r"/perfil/([A-Z0-9]+)", link)
        if not m:
            continue
        sid = m.group(1)
        lon = lat = None
        if point:
            parts = point.split()
            if len(parts) >= 2:
                a, b = _f(parts[0]), _f(parts[1])
                if a is not None and b is not None:
                    # georss:point is lat lon
                    lat, lon = a, b
        if (lat is None or lon is None) and geo_lat is not None and geo_lon is not None:
            lat, lon = geo_lat, geo_lon
        # Mild Iberian sign fix for rare inverted feeds (do not flip normal CAT lon~0..4)
        if lon is not None and lat is not None:
            if lon > 10 and lat < 10:
                lat, lon = lon, lat
            if lon > 20:  # clearly wrong hemisphere for CAT
                lon = -abs(lon)
        out[sid] = {"name": title or sid, "lon": lon, "lat": lat}
    return out


def parse_xml_stations(xml_bytes: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_bytes)
    stations: list[dict[str, Any]] = []
    for st in root.iter():
        if _local(st.tag) != "station":
            continue
        sid = _child_text(st, "id")
        if not sid:
            continue
        loc = _child_text(st, "location") or sid
        sd = _child(st, "stationdata")
        temp = _child(sd, "temperature") if sd is not None else None
        hum = _child(sd, "humidity") if sd is not None else None
        bar = _child(sd, "barometre", "barometer") if sd is not None else None
        wind = _child(sd, "wind") if sd is not None else None
        rain = _child(sd, "rain") if sd is not None else None

        precip = _f(_child_text(rain, "total")) if rain is not None else None
        stations.append(
            {
                "id": sid,
                "name": loc,
                "Temp.max": _f(_child_text(temp, "max")) if temp is not None else None,
                "Temp.min": _f(_child_text(temp, "min")) if temp is not None else None,
                "Temp.act": _f(_child_text(temp, "now")) if temp is not None else None,
                "Hum.max": _f(_child_text(hum, "max")) if hum is not None else None,
                "Hum.min": _f(_child_text(hum, "min")) if hum is not None else None,
                "Hum.act": _f(_child_text(hum, "now")) if hum is not None else None,
                "Pres.max": _f(_child_text(bar, "max")) if bar is not None else None,
                "Pres.min": _f(_child_text(bar, "min")) if bar is not None else None,
                "Vient.max": _f(_child_text(wind, "max")) if wind is not None else None,
                "Vient.act": _f(_child_text(wind, "now")) if wind is not None else None,
                "Vient.dir": _f(_child_text(wind, "azimuth")) if wind is not None else None,
                "Precip.total": precip,
                "Precip.diaria": precip,  # XML only exposes rain/total for current day
            }
        )
    return stations


def merge_rows(
    xml_rows: list[dict[str, Any]],
    coords: dict[str, dict[str, Any]],
    day_iso: str,
    elev_by_id: dict[str, float | None] | None = None,
) -> list[dict[str, Any]]:
    elev_by_id = elev_by_id or {}
    out: list[dict[str, Any]] = []
    for r in xml_rows:
        sid = r["id"]
        c = coords.get(sid, {})
        name = c.get("name") or r.get("name") or sid
        # Prefer RSS name which usually includes province
        row = {
            "name": name,
            "id": sid,
            "time": day_iso,
            "lon": c.get("lon"),
            "lat": c.get("lat"),
            "alt": elev_by_id.get(sid),
            "Temp.max": r.get("Temp.max"),
            "Temp.min": r.get("Temp.min"),
            "Hum.max": r.get("Hum.max"),
            "Hum.min": r.get("Hum.min"),
            "Pres.max": r.get("Pres.max"),
            "Pres.min": r.get("Pres.min"),
            "Vient.max": r.get("Vient.max"),
            "Precip.diaria": r.get("Precip.diaria"),
            "Temp.act": r.get("Temp.act"),
            "Hum.act": r.get("Hum.act"),
            "Vient.dir": r.get("Vient.dir"),
            "Vient.act": r.get("Vient.act"),
            "Precip.total": r.get("Precip.total"),
            "source": "xml_feed",
        }
        out.append(row)
    # stable order
    out.sort(key=lambda x: x["id"])
    return out


def elev_from_keep(keep_path: Path) -> dict[str, float | None]:
    if not keep_path.exists():
        return {}
    out: dict[str, float | None] = {}
    with keep_path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            sid = (r.get("id") or "").strip()
            if not sid:
                continue
            elev = r.get("xema_style_elev") or r.get("elev")
            try:
                out[sid] = float(elev) if elev not in (None, "") else None
            except ValueError:
                out[sid] = None
    return out


def write_daily_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            flat = {k: ("" if r.get(k) is None else r.get(k)) for k in CSV_FIELDS}
            w.writerow(flat)


def write_daily_json(rows: list[dict[str, Any]], path: Path, day_iso: str, ccaa: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ccaa": ccaa,
        "date": day_iso,
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "xml_feed",
        "n_stations": len(rows),
        "stations": rows,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def madrid_today_iso() -> str:
    return datetime.now(MADRID).date().isoformat()


def capture(
    root: Path,
    ccaa: str = CCAA,
    day_iso: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    day_iso = day_iso or madrid_today_iso()
    ymd = day_iso.replace("-", "")
    daily_dir = root / "data" / "daily"
    json_path = daily_dir / f"{ccaa}_{ymd}.json"
    csv_path = daily_dir / f"{ccaa}_{ymd}.csv"
    keep_path = root / "data" / "stations_keep.csv"
    panel_path = root / "docs" / "panel.json"

    if not force and csv_path.exists() and json_path.exists():
        keep_rows = load_keep(keep_path)
        payload = build_payload(keep_rows, daily_dir, ccaa)
        write_panel(payload, panel_path)
        return {
            "ok": True,
            "cached": True,
            "date": day_iso,
            "csv": str(csv_path),
            "json": str(json_path),
            "panel": str(panel_path),
            "n_stations": None,
        }

    xml_bytes = _http_get(XML_URL.format(id=ccaa))
    rss_bytes = _http_get(RSS_URL.format(id=ccaa))
    xml_rows = parse_xml_stations(xml_bytes)
    coords = parse_rss_coords(rss_bytes)
    if not xml_rows:
        raise SystemExit("XML feed returned 0 stations")

    elev = elev_from_keep(keep_path)
    rows = merge_rows(xml_rows, coords, day_iso, elev)
    write_daily_csv(rows, csv_path)
    write_daily_json(rows, json_path, day_iso, ccaa)

    keep_rows = load_keep(keep_path)
    payload = build_payload(keep_rows, daily_dir, ccaa)
    write_panel(payload, panel_path)

    return {
        "ok": True,
        "cached": False,
        "date": day_iso,
        "csv": str(csv_path),
        "json": str(json_path),
        "panel": str(panel_path),
        "n_stations": len(rows),
        "n_with_coords": sum(1 for r in rows if r.get("lon") is not None and r.get("lat") is not None),
        "panel_days": payload["days"],
        "panel_stations": len(payload["stations"]),
        "checksum": payload["checksum"],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=ROOT)
    p.add_argument("--ccaa", default=CCAA)
    p.add_argument("--date", default=None, help="YYYY-MM-DD (default: today Europe/Madrid)")
    p.add_argument("--force", action="store_true")
    args = p.parse_args(argv)

    try:
        info = capture(args.root, args.ccaa, args.date, args.force)
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
