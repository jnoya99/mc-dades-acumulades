#!/usr/bin/env python3
"""Per station-day effective-rain fractions (rain intensity) for Bolets Explorador.

Why: the Explorador counts a short intense burst as LESS effective than the same
rain spread over many hours.  It used to download ~40 days of XEMA hourly rain
from Socrata at startup (slow, ~10-30 s on a phone) and re-run the whole map when
it arrived.  The only thing the model needs from the hourly profile is how the
day's rain is split between infiltration and runoff, so we precompute that here.

Model (same as the app's hourly branch of `peffApplyDay`): every hour,
runoff R = Ph * k * (1 - theta)^2 with k = Ph/(Ph+P0) (P0 = 20 mm), the
infiltrated part refills the soil store (Thornthwaite-Mather, FC = 60 mm,
PET/24 per hour) and theta moves on to the next hour.  Theta changing inside the
day matters on big rain days, so instead of one factor we store the day's
EFFECTIVE-RAIN FRACTION peff/P that this hourly model gives when the day starts
at theta = 0, 0.25, 0.5, 0.75, 1 (PET 2 mm/day).  The app interpolates
linearly at its own theta.  Gentle all-day rain -> fractions ~1; a cloudburst
on wet soil -> much lower.

Output: docs/rain_intensity_xema.json
  {version, built, source, p0, fc, theta_levels, scale, d0, d1, n_days,
   series: {codi_estacio: [[day_index, P_x10, f0, f25, f50, f75, f100], ...]}}
  f* = peff/P x1000 at each starting theta; rainy days only (P >= 0.1 mm).
Days are Madrid calendar days; day_index is relative to d0.
Each run refetches the last --days days (variable 35 only) and merges into the
archive, keeping --retain days.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capture_xema_hourly import (  # noqa: E402
    DATA_URL, MADRID, PAGE, ROOT, UTC, _socrata_get, madrid_hour_key, madrid_now, parse_utc,
)

VERSION = 1
P0 = 20.0
SCALE = 1000
OUT_NAME = "rain_intensity_xema.json"


FC = 60.0
THETAS = (0.0, 0.25, 0.5, 0.75, 1.0)
PET_REF = 2.0


def _peff(p: float, th: float) -> float:
    if not p > 0:
        return 0.0
    th = min(1.0, max(0.0, th))
    return max(0.0, p - p * (p / (p + P0)) * (1 - th) ** 2)


def _tm(s: float, p: float, pet: float) -> float:
    return min(FC, s + (p - pet)) if p >= pet else max(0.0, s - (pet - p) * (s / FC))


def hourly_peff(ph: list[float], theta0: float, pet: float = PET_REF) -> float:
    """Mirror of the app's hourly effective-rain loop (peffApplyDay_uncached)."""
    s, tot = theta0 * FC, 0.0
    for v in ph:
        e = _peff(max(0.0, v), s / FC)
        tot += e
        s = _tm(s, e, pet / 24)
    return tot


def fractions_from_hours(ph: list[float]) -> list[int] | None:
    tot = sum(v for v in ph if v > 0)
    if tot <= 0:
        return None
    return [int(round(min(1.0, hourly_peff(ph, t) / tot) * SCALE)) for t in THETAS]


def keq_from_hours(ph: list[float], p0: float = P0) -> float | None:
    """Single-factor variant (kept for the validation comparison)."""
    tot = sum(v for v in ph if v > 0)
    if tot <= 0:
        return None
    return sum(v * v / (v + p0) for v in ph if v > 0) / tot


def fetch_ppt(utc_start: datetime, utc_end: datetime) -> list[dict[str, Any]]:
    s0 = utc_start.strftime("%Y-%m-%dT%H:%M:%S.000")
    s1 = utc_end.strftime("%Y-%m-%dT%H:%M:%S.000")
    where = f"data_lectura>='{s0}' AND data_lectura<'{s1}' AND codi_variable='35'"
    rows: list[dict[str, Any]] = []
    off = 0
    while True:
        q = urllib.parse.urlencode({
            "$select": "codi_estacio,data_lectura,valor_lectura",
            "$where": where, "$order": "data_lectura,codi_estacio",
            "$limit": str(PAGE), "$offset": str(off)})
        page = _socrata_get(f"{DATA_URL}?{q}")
        if not isinstance(page, list):
            raise RuntimeError("unexpected page")
        rows.extend(page)
        print(f"[rain-intensity] offset={off} n={len(page)} total={len(rows)}", flush=True)
        if len(page) < PAGE:
            return rows
        off += PAGE
        if off > 5_000_000:
            raise RuntimeError("paging ran away")


def hourly_by_station_day(rows: list[dict[str, Any]]) -> dict[str, dict[str, list[float]]]:
    """sid -> madrid day -> 24 hourly sums (mm)."""
    out: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        sid = str(r.get("codi_estacio") or "").strip()
        dt = parse_utc(str(r.get("data_lectura") or ""))
        try:
            v = float(r.get("valor_lectura"))
        except (TypeError, ValueError):
            continue
        if not sid or dt is None or v != v:
            continue
        hk = madrid_hour_key(dt)          # YYYY-MM-DDTHH:00 (Madrid)
        day, h = hk[:10], int(hk[11:13])
        arr = out.setdefault(sid, {}).setdefault(day, [0.0] * 24)
        arr[h] += max(0.0, v)
    return out


def day_records(byday: dict[str, dict[str, list[float]]]) -> dict[str, dict[str, tuple[int, ...]]]:
    rec: dict[str, dict[str, tuple[int, ...]]] = {}
    for sid, days in byday.items():
        for d, ph in days.items():
            fr = fractions_from_hours(ph)
            if fr is None:
                continue
            p10 = int(round(sum(ph) * 10))
            if p10 <= 0:
                continue
            rec.setdefault(sid, {})[d] = tuple([p10] + fr)
    return rec


def load_archive(path: Path) -> dict[str, dict[str, tuple[int, ...]]]:
    if not path.exists():
        return {}
    try:
        j = json.loads(path.read_text("utf-8"))
        d0 = date.fromisoformat(j["d0"])
    except Exception:
        return {}
    out: dict[str, dict[str, tuple[int, ...]]] = {}
    for sid, rows in (j.get("series") or {}).items():
        for row in rows:
            out.setdefault(sid, {})[(d0 + timedelta(days=int(row[0]))).isoformat()] = tuple(int(x) for x in row[1:])
    return out


def build(path: Path, days: int, retain: int, rows: list[dict[str, Any]] | None,
          window_days: set[str]) -> dict[str, Any]:
    arch = load_archive(path)
    fresh = day_records(hourly_by_station_day(rows or []))
    for sid in list(arch):                      # refetched days are replaced wholesale
        for d in list(arch[sid]):
            if d in window_days:
                del arch[sid][d]
    for sid, dd in fresh.items():
        arch.setdefault(sid, {}).update({d: v for d, v in dd.items() if d in window_days})
    today = madrid_now().date()
    d0 = today - timedelta(days=retain - 1)
    series: dict[str, list[list[int]]] = {}
    for sid in sorted(arch):
        rr = []
        for d in sorted(arch[sid]):
            dd = date.fromisoformat(d)
            if dd < d0 or dd > today:
                continue
            rr.append([(dd - d0).days] + list(arch[sid][d]))
        if rr:
            series[sid] = rr
    return {
        "version": VERSION,
        "built": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "XEMA Socrata nzvn-apee variable 35 (PPT), summed per Madrid hour",
        "p0": P0, "fc": FC, "pet_ref": PET_REF, "theta_levels": list(THETAS), "scale": SCALE,
        "formula": "f(theta0) = peff/P of the app's hourly model; app interpolates linearly in theta",
        "d0": d0.isoformat(), "d1": today.isoformat(), "n_days": retain,
        "fields": ["day_index", "P_x10", "f0", "f25", "f50", "f75", "f100"],
        "series": series,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--days", type=int, default=3, help="Madrid days to refetch (default 3)")
    ap.add_argument("--retain", type=int, default=400, help="days kept in the archive")
    args = ap.parse_args(argv)
    path = args.root / "docs" / OUT_NAME
    now = madrid_now()
    start = (now - timedelta(days=args.days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    window = {(start + timedelta(days=i)).date().isoformat() for i in range(args.days)}
    # fetch in <= 4-day slices: one Socrata page each (deep $offset paging is very slow)
    rows: list[dict[str, Any]] = []
    t0 = start.astimezone(UTC) - timedelta(hours=3)
    t_end = now.astimezone(UTC) + timedelta(hours=3)
    while t0 < t_end:
        t1 = min(t_end, t0 + timedelta(days=4))
        rows.extend(fetch_ppt(t0, t1))
        t0 = t1
    out = build(path, args.days, args.retain, rows, window)
    path.write_text(json.dumps(out, separators=(",", ":")), "utf-8")
    n = sum(len(v) for v in out["series"].values())
    print(json.dumps({"ok": True, "rows": len(rows), "stations": len(out["series"]),
                      "station_days": n, "bytes": path.stat().st_size}))
    return 0 if out["series"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
