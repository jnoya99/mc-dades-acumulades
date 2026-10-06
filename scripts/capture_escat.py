#!/usr/bin/env python3
"""Download current ESCAT snapshot from Meteoclimatic public feeds.

Primary source (no past-day ?d= — those return 401):
  - XML:  https://www.meteoclimatic.net/feed/xml/ESCAT
  - RSS:  https://www.meteoclimatic.net/feed/rss/ESCAT  (coords)

Saves:
  data/daily/ESCAT_YYYYMMDD.json
  data/daily/ESCAT_YYYYMMDD.csv
then rebuilds docs/panel.json via build_panel.py helpers.

Date stamp (intended close day), not wall-clock at a delayed run:
  - ``--date YYYY-MM-DD`` wins when given.
  - Else env ``ESCAT_CLOSE_DATE`` / ``INPUT_DATE``.
  - Else if GitHub ``schedule`` and ``github.event.schedule`` / cron time is
    available, use the Europe/Madrid calendar day of that scheduled fire.
  - Else noon rule: if Madrid local hour < 12, close **yesterday**; else today.
    (Covers overnight-delayed daily jobs that would otherwise stamp tomorrow.)

Past-day closes never invent totals from live XML (those belong to today):
they rebuild from the last hourly raw of that Madrid day when available.

Existing daily files are merged per-station monotonically (never lower
``Precip.diaria`` / never earlier ``captured_at`` wins over a later one).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_panel import build_payload, load_keep, write_panel  # noqa: E402
from http_util import http_get  # noqa: E402

CCAA = "ESCAT"
UA = "mc-dades-acumulades/1.0 (+https://github.com/jnoya99/mc-dades-acumulades; daily archive)"
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
    """GET with shared retries (backoff + jitter for timeouts/URLError/429/5xx)."""
    return http_get(
        url,
        timeout=timeout,
        user_agent=UA,
        headers={
            "Accept": "application/xml,text/xml,*/*",
            "Referer": "https://www.meteoclimatic.net/",
        },
    )


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


def madrid_today_iso() -> str:
    return datetime.now(MADRID).date().isoformat()


def madrid_now() -> datetime:
    return datetime.now(MADRID)


def intended_close_date(
    explicit: str | None = None,
    now: datetime | None = None,
) -> str:
    """Madrid calendar day this daily close is meant to stamp.

    Rule (exact):
      1. ``explicit`` / ``--date`` if provided (must be YYYY-MM-DD).
      2. Env ``ESCAT_CLOSE_DATE`` or ``INPUT_DATE`` if set.
      3. If ``GITHUB_EVENT_NAME=schedule``: try to derive the Madrid day of the
         scheduled cron fire from ``GITHUB_EVENT_PATH`` (``scheduled_at`` if
         present) or from combining today's UTC date with cron ``30 21 * * *``
         (21:30 UTC → evening Madrid same calendar date in CEST/CET). When the
         job is delayed past Madrid midnight, wall-clock is already tomorrow,
         so we fall through to the noon rule which closes yesterday.
      4. Noon rule (default): if Europe/Madrid local hour < 12, intended day
         is **yesterday** Madrid; otherwise **today** Madrid.
    """
    import os

    if explicit:
        datetime.strptime(explicit, "%Y-%m-%d")  # validate
        return explicit
    for key in ("ESCAT_CLOSE_DATE", "INPUT_DATE"):
        v = (os.environ.get(key) or "").strip()
        if v:
            datetime.strptime(v, "%Y-%m-%d")
            return v

    now = now or madrid_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=MADRID)
    else:
        now = now.astimezone(MADRID)

    event_name = (os.environ.get("GITHUB_EVENT_NAME") or "").strip()
    if event_name == "schedule":
        scheduled_iso = _scheduled_fire_madrid_date()
        if scheduled_iso:
            return scheduled_iso
        # Delayed schedule: before noon Madrid → close yesterday
        if now.hour < 12:
            return (now.date() - timedelta(days=1)).isoformat()
        return now.date().isoformat()

    # workflow_dispatch / local / unknown: noon rule
    if now.hour < 12:
        return (now.date() - timedelta(days=1)).isoformat()
    return now.date().isoformat()


def _scheduled_fire_madrid_date() -> str | None:
    """Madrid date of the GitHub schedule fire, only when the event carries it.

    GitHub schedule payloads usually lack a fire timestamp; returning None lets
    the noon rule handle delayed overnight runs (stamp yesterday before 12:00).
    Do NOT invent a fire time from ``utcnow`` + cron — that would stamp *today*
    when a late job finally runs after Madrid midnight.
    """
    import os

    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for key in ("scheduled_at", "schedule_time", "fire_time"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw:
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                return dt.astimezone(MADRID).date().isoformat()
            except ValueError:
                pass
    return None


def last_hourly_path(hourly_dir: Path, day_iso: str, ccaa: str = CCAA) -> Path | None:
    """Last available raw hourly file for a Madrid calendar day (e.g. _22/_23)."""
    ymd = day_iso.replace("-", "")
    files = sorted(hourly_dir.glob(f"{ccaa}_{ymd}_??.json"))
    return files[-1] if files else None


def rows_from_hourly_raw(
    hourly_path: Path,
    day_iso: str,
    elev_by_id: dict[str, float | None] | None = None,
    coords: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build daily-schema rows from an hourly raw snapshot (cum → Precip.diaria)."""
    elev_by_id = elev_by_id or {}
    coords = coords or {}
    obj = json.loads(hourly_path.read_text(encoding="utf-8"))
    st_list = obj.get("stations") if isinstance(obj, dict) else None
    if not isinstance(st_list, list):
        raise SystemExit(f"hourly raw has no stations: {hourly_path}")
    out: list[dict[str, Any]] = []
    for st in st_list:
        if not isinstance(st, dict):
            continue
        sid = (st.get("id") or "").strip()
        if not sid:
            continue
        cum = st.get("cum")
        if cum is None:
            cum = st.get("Precip.total")
        if cum is None:
            cum = st.get("Precip.diaria")
        c = coords.get(sid, {})
        name = c.get("name") or st.get("name") or sid
        row = {
            "name": name,
            "id": sid,
            "time": day_iso,
            "lon": c.get("lon") if c.get("lon") is not None else st.get("lon"),
            "lat": c.get("lat") if c.get("lat") is not None else st.get("lat"),
            "alt": elev_by_id.get(sid),
            "Temp.max": st.get("Temp.max"),
            "Temp.min": st.get("Temp.min"),
            "Hum.max": st.get("Hum.max"),
            "Hum.min": st.get("Hum.min"),
            "Pres.max": None,
            "Pres.min": None,
            "Vient.max": st.get("Vient.max"),
            "Precip.diaria": cum,
            "Temp.act": st.get("Temp.act"),
            "Hum.act": st.get("Hum.act"),
            "Vient.dir": st.get("Vient.dir"),
            "Vient.act": st.get("Vient.act"),
            "Precip.total": cum,
            "source": "rebuilt_from_hourly",
        }
        out.append(row)
    out.sort(key=lambda x: x["id"])
    return out


def _precip_val(row: dict[str, Any]) -> float | None:
    v = row.get("Precip.diaria")
    if v is None:
        v = row.get("Precip.total")
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:
        return None
    return x


def _parse_captured_at(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def monotonic_merge_stations(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    existing_captured_at: str | None,
    incoming_captured_at: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Per-station merge: never replace with lower Precip.diaria or earlier data.

    Keeps the station row with the higher Precip.diaria. Ties prefer the later
    captured_at; if still tied, prefer incoming (refresh of other fields).
    Stations only in one side are kept. Logs kept/dropped counts.
    """
    by_id: dict[str, dict[str, Any]] = {}
    src_cap: dict[str, str | None] = {}
    for r in existing:
        sid = (r.get("id") or "").strip()
        if sid:
            by_id[sid] = dict(r)
            src_cap[sid] = existing_captured_at
    kept_existing = 0
    took_incoming = 0
    added = 0
    inc_cap_dt = _parse_captured_at(incoming_captured_at)
    for r in incoming:
        sid = (r.get("id") or "").strip()
        if not sid:
            continue
        if sid not in by_id:
            by_id[sid] = dict(r)
            src_cap[sid] = incoming_captured_at
            added += 1
            continue
        old = by_id[sid]
        old_p = _precip_val(old)
        new_p = _precip_val(r)
        old_cap = _parse_captured_at(src_cap.get(sid))
        # Never downgrade precip
        if old_p is not None and new_p is not None and new_p < old_p:
            kept_existing += 1
            print(
                f"[merge] keep existing {sid}: precip {old_p} > incoming {new_p}",
                file=sys.stderr,
            )
            continue
        if old_p is not None and new_p is None:
            kept_existing += 1
            print(f"[merge] keep existing {sid}: incoming precip missing", file=sys.stderr)
            continue
        # Same precip (or old missing): reject earlier captured_at
        if (
            old_p is not None
            and new_p is not None
            and new_p == old_p
            and old_cap is not None
            and inc_cap_dt is not None
            and inc_cap_dt < old_cap
        ):
            kept_existing += 1
            print(
                f"[merge] keep existing {sid}: earlier captured_at "
                f"{incoming_captured_at} < {src_cap.get(sid)}",
                file=sys.stderr,
            )
            continue
        # Prefer incoming when precip is higher, or equal/newer, or filling gaps
        by_id[sid] = dict(r)
        src_cap[sid] = incoming_captured_at
        took_incoming += 1
        if old_p is not None and new_p is not None and new_p > old_p:
            print(
                f"[merge] upgrade {sid}: precip {old_p} → {new_p}",
                file=sys.stderr,
            )
    rows = sorted(by_id.values(), key=lambda x: x["id"])
    stats = {
        "kept_existing": kept_existing,
        "took_incoming": took_incoming,
        "added": added,
        "n_stations": len(rows),
    }
    print(
        f"[merge] stations={stats['n_stations']} kept_existing={kept_existing} "
        f"took_incoming={took_incoming} added={added}",
        file=sys.stderr,
    )
    return rows, stats


def write_daily_json(
    rows: list[dict[str, Any]],
    path: Path,
    day_iso: str,
    ccaa: str,
    source: str = "xml_feed",
    note: str | None = None,
    captured_at: str | None = None,
) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    cap = captured_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload: dict[str, Any] = {
        "ccaa": ccaa,
        "date": day_iso,
        "captured_at": cap,
        "source": source,
        "n_stations": len(rows),
        "stations": rows,
    }
    if note:
        payload["note"] = note
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return cap


def capture(
    root: Path,
    ccaa: str = CCAA,
    day_iso: str | None = None,
    force: bool = False,
    rebuild_from_hourly: bool = False,
) -> dict[str, Any]:
    day_iso = intended_close_date(day_iso)
    today = madrid_today_iso()
    ymd = day_iso.replace("-", "")
    daily_dir = root / "data" / "daily"
    hourly_dir = root / "data" / "hourly"
    json_path = daily_dir / f"{ccaa}_{ymd}.json"
    csv_path = daily_dir / f"{ccaa}_{ymd}.csv"
    keep_path = root / "data" / "stations_keep.csv"
    panel_path = root / "docs" / "panel.json"

    existing_payload: dict[str, Any] | None = None
    if json_path.exists():
        try:
            existing_payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing_payload = None

    # Skip only when not force and file exists — but force still merges monotonically.
    if not force and not rebuild_from_hourly and csv_path.exists() and json_path.exists():
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

    elev = elev_from_keep(keep_path)
    closing_past = day_iso < today
    source = "xml_feed"
    note = None
    rows: list[dict[str, Any]]
    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    use_hourly = rebuild_from_hourly or closing_past
    hourly_path = last_hourly_path(hourly_dir, day_iso, ccaa) if use_hourly else None

    if use_hourly and hourly_path is not None:
        # Coords: prefer RSS when we can fetch; else use lon/lat already in hourly.
        coords: dict[str, dict[str, Any]] = {}
        if not closing_past or not rebuild_from_hourly:
            try:
                rss_bytes = _http_get(RSS_URL.format(id=ccaa))
                coords = parse_rss_coords(rss_bytes)
            except Exception as e:
                print(f"[warn] RSS coords fetch failed: {e}", file=sys.stderr)
        rows = rows_from_hourly_raw(hourly_path, day_iso, elev, coords)
        source = "rebuilt_from_hourly"
        note = f"rebuilt from {hourly_path.name}"
        print(
            f"[capture] closing {day_iso} from hourly {hourly_path.name} "
            f"(past={closing_past})",
            file=sys.stderr,
        )
    elif closing_past and hourly_path is None:
        raise SystemExit(
            f"Cannot close past day {day_iso}: no hourly raw in {hourly_dir} "
            f"and live XML would stamp today's totals"
        )
    else:
        # Live close of today (or noon-rule today): XML feed
        xml_bytes = _http_get(XML_URL.format(id=ccaa))
        rss_bytes = _http_get(RSS_URL.format(id=ccaa))
        xml_rows = parse_xml_stations(xml_bytes)
        coords = parse_rss_coords(rss_bytes)
        if not xml_rows:
            raise SystemExit("XML feed returned 0 stations")
        rows = merge_rows(xml_rows, coords, day_iso, elev)
        source = "xml_feed"

    merge_stats = None
    if existing_payload and isinstance(existing_payload.get("stations"), list):
        rows, merge_stats = monotonic_merge_stations(
            existing_payload["stations"],
            rows,
            existing_payload.get("captured_at"),
            captured_at,
        )
        # Keep the later captured_at on the file
        old_cap = _parse_captured_at(existing_payload.get("captured_at"))
        new_cap = _parse_captured_at(captured_at)
        if old_cap and new_cap and old_cap > new_cap:
            captured_at = existing_payload["captured_at"]
        # Preserve rebuilt source if that is what we wrote / merged from hourly
        if existing_payload.get("source") == "rebuilt_from_hourly" and source == "xml_feed":
            # If merge mostly kept existing rebuilt values, leave source tag
            pass
        if source == "rebuilt_from_hourly" or existing_payload.get("source") == "rebuilt_from_hourly":
            if any(r.get("source") == "rebuilt_from_hourly" for r in rows):
                source = "rebuilt_from_hourly"
                note = note or existing_payload.get("note")

    write_daily_csv(rows, csv_path)
    write_daily_json(
        rows, json_path, day_iso, ccaa, source=source, note=note, captured_at=captured_at
    )

    keep_rows = load_keep(keep_path)
    payload = build_payload(keep_rows, daily_dir, ccaa)
    write_panel(payload, panel_path)

    return {
        "ok": True,
        "cached": False,
        "date": day_iso,
        "today_madrid": today,
        "csv": str(csv_path),
        "json": str(json_path),
        "panel": str(panel_path),
        "n_stations": len(rows),
        "n_with_coords": sum(
            1 for r in rows if r.get("lon") is not None and r.get("lat") is not None
        ),
        "source": source,
        "note": note,
        "merge": merge_stats,
        "panel_days": payload["days"],
        "panel_stations": len(payload["stations"]),
        "checksum": payload["checksum"],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=ROOT)
    p.add_argument("--ccaa", default=CCAA)
    p.add_argument(
        "--date",
        default=None,
        help="YYYY-MM-DD intended close day (default: schedule/noon rule, Europe/Madrid)",
    )
    p.add_argument("--force", action="store_true")
    p.add_argument(
        "--rebuild-from-hourly",
        action="store_true",
        help="Build/overwrite daily file from last hourly raw of --date (no live XML)",
    )
    args = p.parse_args(argv)

    try:
        info = capture(
            args.root,
            args.ccaa,
            args.date,
            args.force,
            rebuild_from_hourly=args.rebuild_from_hourly,
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
