"""Offline check: the app's hour-by-hour effective-rain model vs the daily keq model.
Usage: python tests/validate_keq.py docs/hourly_rain_xema.json"""
import json, sys, statistics as st
sys.path.insert(0, 'scripts')
from build_rain_intensity import keq_from_hours, P0
FC = 60.0
def pEff(P, th, k):
    if not P > 0: return 0.0
    th = min(1, max(0, th)); R = P * k * (1 - th) ** 2
    return max(0.0, P - R)
def tm(S, P, pet):
    return min(FC, S + (P - pet)) if P >= pet else max(0.0, S - (pet - P) * (S / FC))
def hourly(ph, th, pet):            # = peffApplyDay_uncached (hourly branch)
    S = th * FC; e = 0.0
    for v in ph:
        x = pEff(v, S / FC, v / (v + P0)); e += x; S = tm(S, x, pet / 24)
    return e, S
def daily(P, th, pet, k):           # daily path with given k
    S = th * FC; x = pEff(P, th, k); return x, tm(S, x, pet)
j = json.load(open(sys.argv[1]))
by = {}
for sid, hrs in j['series'].items():
    for hk, v in hrs.items():
        if v is None: continue
        by.setdefault((sid, hk[:10]), [0.0] * 24)[int(hk[11:13])] += max(0, float(v))
res = {'keq': {'pe': [], 'S': []}, 'proxy': {'pe': [], 'S': []}}
runoff_h = []; nday = 0
for (sid, d), ph in by.items():
    P = sum(ph)
    if P < 1: continue
    nday += 1; k = keq_from_hours(ph)
    for th in (0.2, 0.5, 0.8):
        for pet in (1.0, 3.0):
            eh, Sh = hourly(ph, th, pet)
            for name, kk in (('keq', round(k * 1000) / 1000), ('proxy', P / (P + P0))):
                ed, Sd = daily(P, th, pet, kk)
                res[name]['pe'].append(abs(ed - eh)); res[name]['S'].append(abs(Sd - Sh) / FC)
            runoff_h.append((P - eh) / P)
def q(a, p): a = sorted(a); return a[min(len(a) - 1, int(p * len(a)))]
print(json.dumps({'station_days_P>=1mm': nday, 'cases': len(runoff_h),
  'hourly_runoff_share_mean': round(st.mean(runoff_h), 4),
  **{n: {'peff_abs_err_mm_mean': round(st.mean(r['pe']), 4), 'p95': round(q(r['pe'], .95), 3), 'max': round(max(r['pe']), 3),
         'soil_err_fracFC_mean': round(st.mean(r['S']), 5), 'soil_p95': round(q(r['S'], .95), 4), 'soil_max': round(max(r['S']), 4)}
     for n, r in res.items()}}, indent=1))

# --- table variant: effective-rain fraction at fixed theta levels, linear interpolation
LV = (0.0, 0.25, 0.5, 0.75, 1.0)
def table(ph, pet_ref=2.0):
    P = sum(ph); return [round(hourly(ph, t, pet_ref)[0] / P * 1000) / 1000 for t in LV]
def interp(tb, th):
    th = min(1, max(0, th)); i = min(len(LV) - 2, int(th / 0.25)); f = (th - LV[i]) / 0.25
    return tb[i] * (1 - f) + tb[i + 1] * f
r2 = {'pe': [], 'S': []}
for (sid, d), ph in by.items():
    P = sum(ph)
    if P < 1: continue
    tb = table(ph)
    for th in (0.2, 0.5, 0.8):
        for pet in (1.0, 3.0):
            eh, Sh = hourly(ph, th, pet); ed = P * interp(tb, th); Sd = tm(th * FC, ed, pet)
            r2['pe'].append(abs(ed - eh)); r2['S'].append(abs(Sd - Sh) / FC)
print('table5', json.dumps({'peff_abs_err_mm_mean': round(st.mean(r2['pe']), 4), 'p95': round(q(r2['pe'], .95), 3), 'max': round(max(r2['pe']), 3),
  'soil_err_fracFC_mean': round(st.mean(r2['S']), 5), 'soil_p95': round(q(r2['S'], .95), 4), 'soil_max': round(max(r2['S']), 4)}))
