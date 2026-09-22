"""Summarize a mux-tax runs.jsonl: per-label percentiles and rung deltas.

Trims a warm-up window off the FRONT of each label, because probe_reader
scores from the moment it locks the strip — which is while ffmpeg is still
filling its probe buffer and SRT is still finding its rate. The baselines in
sibling result dirs were read the same way (the harness prints a rolling
"last~600" line for exactly this reason).

  python3 summarize.py runs.jsonl [--warmup 3.0]
"""

from __future__ import annotations

import argparse
import collections
import json
import sys


def percentile(values: list[float], pct: float) -> float:
    """Same rule as spikes/parity/_common.py, so numbers stay comparable."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[k]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs")
    ap.add_argument("--warmup", type=float, default=3.0,
                    help="seconds to drop from the start of each label")
    args = ap.parse_args()

    by_label: dict[str, list[tuple[float, float]]] = collections.defaultdict(list)
    with open(args.runs, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                by_label[row["label"]].append((float(row["t"]), float(row["lat_ms"])))
            except (ValueError, KeyError):
                continue
    if not by_label:
        sys.exit(f"no usable rows in {args.runs}")

    print(f"{'label':<22} {'n':>6} {'p50':>7} {'p95':>7} {'p99':>7}   ms")
    print("-" * 56)
    p50: dict[str, float] = {}
    for label in sorted(by_label):
        rows = sorted(by_label[label])
        t0 = rows[0][0]
        kept = [ms for t, ms in rows if t - t0 >= args.warmup]
        if not kept:
            kept = [ms for _, ms in rows]
        p50[label] = percentile(kept, 50)
        print(f"{label:<22} {len(kept):>6} {percentile(kept, 50):>7.1f} "
              f"{percentile(kept, 95):>7.1f} {percentile(kept, 99):>7.1f}")

    # Everything is quoted against the floor, since that is the constant the
    # rungs share. Adjacent-rung deltas would be misleading here: the SRT
    # rungs are three settings of one knob, not three stacked costs.
    if "mux-floor" in p50:
        floor = p50["mux-floor"]
        names = {
            "mux-raw-tcp": "one ffmpeg hop + socket",
            "mux-srt-L5": "+ mpegts/SRT @ latency 5",
            "mux-srt-L20": "+ mpegts/SRT @ latency 20",
            "mux-srt-L120": "+ mpegts/SRT @ latency 120",
            "mux-full": "full path (SRT 20 + host demux + TCP)",
        }
        print("\nover the floor (p50):")
        for label, name in names.items():
            if label in p50:
                print(f"  {name:<40} {p50[label] - floor:+7.1f} ms")
        # The SRT knob is the headline: is it ~1:1 additive?
        if "mux-srt-L5" in p50 and "mux-srt-L120" in p50:
            d_lat = 120 - 5
            d_ms = p50["mux-srt-L120"] - p50["mux-srt-L5"]
            print(f"\n  SRT latency {d_lat} ms higher costs {d_ms:+.1f} ms end to end "
                  f"({d_ms / d_lat:.2f} ms per ms)")
        if "mux-srt-L20" in p50 and "mux-raw-tcp" in p50:
            print(f"  mpegts+SRT@20 over a bare socket: "
                  f"{p50['mux-srt-L20'] - p50['mux-raw-tcp']:+.1f} ms")


if __name__ == "__main__":
    main()
