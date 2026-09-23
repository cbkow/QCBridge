# Edit→visible latency, host Blender → replica Blender

2026-09-23, `smokes/bench_latency.sh`, loopback, one machine, Blender 5.2.2,
agent built `--release`. Heavy = 641,601-vertex grid. Replica sampling tick
6.4 ms median, ~11–13 ms p95 (the measurement floor). ms, p50 / p90.

Interpretation and the ranked findings are in `SYNC-AUDIT.md` §1 and §3.

| phase | agent run 2 | agent run 1 | zmq run 2 | zmq run 1 |
|---|---|---|---|---|
| t1 | 184 / 194 | 186 / 205 | 184 / 204 | 188 / 207 |
| t2 | 216 / 231 | 204 / 221 | 223 / 231 | 211 / 227 |
| hot | 32 / 48 | 32 / 45 | 39 / 48 | 32 / 46 |
| sweep | 518 / 675 | — | 474 / 634 | — |
| hol0 | 213 / 225 | 189 / 194 | 188 / 214 | 179 / 184 |
| hol150 | 256 / 306 | 302 / 305 | 189 / 194 | 217 / 224 |
| heavy blob | 362 / 392 | 349 / 372 | 252 / 297 | 277 / 284 |

Run 1 predates the `sweep` phase. Per-run `latency.json` files sit in
`agent/`, `agent-run1/`, `zmq/`, `zmq-run1/`; `agent-run1/table.txt` is the
printed report as it appeared.

Phases: t1 = `Probe.location.x`; t2 = add a Displace modifier on Probe (the
structural signature escalates it to tier 2); hot = `scene.frame_set`;
sweep = `Probe["Knob"]`, a custom prop that fires no depsgraph event; hol0 =
toggle a modifier on Heavy then edit Probe in the same flush; hol150 = the
same with the Probe edit 150 ms later; heavy = toggle→visible for the Heavy
blob itself.
