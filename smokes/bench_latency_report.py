"""Join host_lat.json and replica_lat.json on the value and print latency
percentiles per phase. Exit 1 if any phase lost a trial."""
import json
import os
import sys

OUT = sys.argv[1]
host = json.load(open(os.path.join(OUT, "host_lat.json")))
rep = json.load(open(os.path.join(OUT, "replica_lat.json")))

# phase -> (replica key, value transform)
JOIN = {
    "t1": ("probe_x", lambda k: k),
    "t2": ("bench", lambda k: k),
    "hot": ("frame", lambda k: 100 + k),
    "sweep": ("knob", lambda k: k),
    "hol0": ("probe_x", lambda k: 100 + k),
    "hol150": ("probe_x", lambda k: 200 + k),
}


def first_seen(key, value):
    for v, t in rep["seen"].get(key, []):
        if v == value:
            return t
    return None


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round((len(xs) - 1) * p)))]


summary = {"transport": host.get("transport"), "heavy_verts": host.get("heavy_verts"),
           "replica_tick_ms": [rep.get("tick_ms_median"), rep.get("tick_ms_p95")],
           "phases": {}}
lost = 0
print(f"transport={host.get('transport')}  heavy={host.get('heavy_verts')} verts  "
      f"replica sample tick median/p95 = {rep.get('tick_ms_median'):.1f}/{rep.get('tick_ms_p95'):.1f} ms")
print(f"{'phase':8} {'n':>3} {'lost':>4} {'min':>7} {'p50':>7} {'p90':>7} {'max':>7}   ms edit→visible")
for phase, (key, xf) in JOIN.items():
    lat = []
    miss = 0
    for k, t_sent in host["sent"].get(phase, {}).items():
        t_seen = first_seen(key, xf(int(k)))
        if t_seen is None:
            miss += 1
        else:
            lat.append((t_seen - t_sent) * 1000)
    lost += miss
    if lat:
        row = dict(n=len(lat), lost=miss, min=min(lat), p50=pct(lat, 0.5),
                   p90=pct(lat, 0.9), max=max(lat))
        print(f"{phase:8} {row['n']:>3} {miss:>4} {row['min']:>7.1f} {row['p50']:>7.1f} "
              f"{row['p90']:>7.1f} {row['max']:>7.1f}")
    else:
        row = dict(n=0, lost=miss)
        print(f"{phase:8} {0:>3} {miss:>4}   (no trials matched)")
    summary["phases"][phase] = row

# heavy blob arrival: time from toggle to the replica's modifier count changing
hl = []
heavy_seen = rep["seen"].get("heavy_mods", [])
for tag, t_sent in sorted(host["sent"].get("heavy", {}).items(), key=lambda kv: kv[1]):
    later = [t for _, t in heavy_seen if t > t_sent]
    if later:
        hl.append((min(later) - t_sent) * 1000)
if hl:
    print(f"{'heavy':8} {len(hl):>3} {'':>4} {min(hl):>7.1f} {pct(hl, 0.5):>7.1f} "
          f"{pct(hl, 0.9):>7.1f} {max(hl):>7.1f}   (tier-2 blob of Heavy, toggle→visible)")
    summary["phases"]["heavy_blob"] = dict(n=len(hl), p50=pct(hl, 0.5), max=max(hl))
summary["replica_stats"] = rep.get("stats")
json.dump(summary, open(os.path.join(OUT, "latency.json"), "w"), indent=1)
sys.exit(1 if lost else 0)
