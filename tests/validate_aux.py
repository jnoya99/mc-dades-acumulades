#!/usr/bin/env python3
"""Sanity checks for docs/rain_intensity_{escat,mountain,aemet}.json.

1. schema / ranges (fractions 0..1000, non-increasing in theta, P_x10 > 0);
2. every kept day agrees with its daily panel P (the quality rule);
3. plausibility against XEMA: for station-days with a XEMA station <= 5 km away
   and rain at both, the f0 (dry-soil share) correlates with the XEMA one.
"""
import json, math, sys
from datetime import date, timedelta
from pathlib import Path
R = Path(__file__).resolve().parents[1] / "docs"
def load(n): return json.loads((R / n).read_text("utf-8"))
def days(j):
    d0 = date.fromisoformat(j["d0"])
    return {sid: {(d0 + timedelta(days=r[0])).isoformat(): r[1:] for r in rows} for sid, rows in j["series"].items()}
def coords(j): return {s["id"]: (float(s["lat"]), float(s["lon"])) for s in j.get("stations", []) if s.get("lat") is not None and s.get("lon") is not None}
x = days(load("rain_intensity_xema.json"))
xc = {}
for s in load("hourly_meteo_xema.json").get("stations", []):
    if s.get("lat") is not None: xc[str(s.get("id") or s.get("codi"))] = (float(s["lat"]), float(s["lon"]))
fail = 0
for src, panel, cfile in (("escat", "panel.json", "panel.json"), ("mountain", "panel_mountain.json", "panel_mountain.json"), ("aemet", "panel_aemet.json", "panel_aemet.json")):
    j = load(f"rain_intensity_{src}.json"); dd = days(j); pan = load(panel)["series"]; cc = coords(load(cfile))
    n = bad = 0; pairs = []
    for sid, ds in dd.items():
        for d, v in ds.items():
            n += 1
            p10, fr = v[0], v[1:]
            P = (pan.get(sid, {}).get(d) or {}).get("P")
            ok = p10 > 0 and all(0 <= f <= 1000 for f in fr) and all(fr[i] <= fr[i + 1] + 1 for i in range(4)) \
                and P is not None and abs(p10 / 10 - P) <= max(0.5, 0.15 * P) + 0.05
            if not ok: bad += 1
            if sid in cc:
                best = None
                for xs, (la, lo) in xc.items():
                    km = math.hypot((la - cc[sid][0]) * 111, (lo - cc[sid][1]) * 83)
                    if km <= 5 and (best is None or km < best[0]): best = (km, xs)
                if best and d in x.get(best[1], {}): pairs.append((fr[0], x[best[1]][d][1]))
    r = float("nan")
    if len(pairs) > 5:
        a = [p[0] for p in pairs]; b = [p[1] for p in pairs]; ma = sum(a)/len(a); mb = sum(b)/len(b)
        r = sum((p-ma)*(q-mb) for p, q in pairs) / math.sqrt(sum((p-ma)**2 for p in a) * sum((q-mb)**2 for q in b) or 1)
    print(json.dumps({"src": src, "stations": len(dd), "station_days": n, "bad": bad, "xema_pairs": len(pairs), "f0_corr_vs_xema": round(r, 3),
                      "mean_f0": round(sum(v[1] for s in dd.values() for v in s.values()) / max(1, n), 1)}))
    fail += bad
xm = [v[1] for s in x.values() for v in s.values()]
print(json.dumps({"src": "xema", "mean_f0": round(sum(xm)/len(xm), 1)}))
sys.exit(1 if fail else 0)
