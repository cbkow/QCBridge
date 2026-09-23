"""Judge the coverage run: for each action, did the replica ever show the
host's expected value after the action? Prints a table and writes
coverage.json. Exit 0 always — this is a survey, not a gate."""
import json
import os
import sys
import time

OUT = sys.argv[1]
TIMEOUT = float(os.environ.get("QCB_COV_TIMEOUT", "900"))
GRACE = 6.0


def load(name):
    try:
        with open(os.path.join(OUT, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


start = time.monotonic()
host = replica = None
done_at = None
while time.monotonic() - start < TIMEOUT:
    host = load("host.json") or host
    replica = load("replica.json") or replica
    if host and host.get("done"):
        done_at = done_at or time.monotonic()
        if time.monotonic() - done_at > GRACE:
            break
    if host and host.get("errors", {}).get("_connect"):
        print("host never connected", flush=True)
        break
    time.sleep(1)

if not host or not replica:
    print("no data", flush=True)
    sys.exit(0)

rows = []
for key in host["order"]:
    exp = host["expected"].get(key)
    t0 = host["t_done"].get(key)
    err = host["errors"].get(key)
    canon = json.dumps(exp, sort_keys=True, default=str)
    hist = replica["history"].get(key, [])
    matched = None
    for value, t in hist:
        if value == canon and t0 is not None and t >= t0 - 0.05:
            matched = t
            break
    last = hist[-1][0] if hist else None
    settle = float(os.environ.get("QCB_COV_SETTLE", "2.5"))
    before = [v for v, t in hist if t0 is not None and t < t0]
    if err:
        status = "host-error"
    elif matched is None and before and before[-1] == canon and last == canon:
        # The action left the property where the replica already had it
        # (undo, or an edit that cancelled out): nothing needed to cross.
        status = "unchanged"
    elif matched is not None and (matched - t0) > settle:
        # It arrived, but only after the settle window: a later action
        # shipped the same datablock (or carried it as a dependency). The
        # edit itself was not detected.
        status = "piggybacked"
    elif matched is not None:
        status = "crossed"
    elif exp is None and last == "null":
        status = "inconclusive"
    else:
        status = "NOT crossed"
    rows.append({"key": key, "group": host["groups"][key], "desc": host["desc"][key],
                 "status": status, "latency_ms": None if matched is None else round((matched - t0) * 1000),
                 "expected": canon, "last_seen": last, "error": err})

counts = {}
for r in rows:
    counts[r["status"]] = counts.get(r["status"], 0) + 1
print(f"{'action':28} {'group':11} {'status':12} {'ms':>6}  note")
for r in rows:
    note = ""
    if r["status"] == "unchanged":
        note = "replica already held the final value; nothing needed to cross"
    elif r["status"] == "piggybacked":
        note = "arrived only with a later action"
    elif r["status"] == "NOT crossed":
        note = f"expected {r['expected'][:40]}  replica {str(r['last_seen'])[:40]}"
    elif r["status"] == "host-error":
        note = r["error"][:80]
    print(f"{r['key']:28} {r['group']:11} {r['status']:12} {str(r['latency_ms'] or ''):>6}  {note}")
print(json.dumps(counts), flush=True)
print("replica stats:", json.dumps(replica.get("stats")), flush=True)
with open(os.path.join(OUT, "coverage.json"), "w") as f:
    json.dump({"rows": rows, "counts": counts, "replica_stats": replica.get("stats")}, f, indent=1)
