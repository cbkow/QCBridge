# 2026-09-17 — S5 over the VPN: Mac host → Windows 4K replica, one Kyber connection (IN PROGRESS)

Everything on UDP 19990 (`QCB_TRANSPORT=kyber`): sync lanes + video from the
replica helper's own capture child (ddagrab 60 fps → hevc_nvenc 50M), read on
the Mac from the host helper's `tcp://127.0.0.1:19997`. Host stamps, host
reads: no clock correction. Code `042c411`. Raw rows: `runs.jsonl`.

| Label | Engine | Time (UTC) | p50 | p95 | p99 | Scored / stale / failed |
| --- | --- | --- | ---: | ---: | ---: | --- |
| vpn-s5-eevee-r1 | EEVEE | 18:19:19 | 133 | 139 | 141 | 1168 / 634 / 0 |
| vpn-s5-eevee-r2 | EEVEE | 18:19:54 | 135 | 139 | 140 | 1172 / 630 / 0 |
| vpn-s5-cycles-r1 | Cycles GPU | 18:21:13 | 129 | 145 | 146 | 1096 / 706 / 0 |
| vpn-s5-cycles-r2 | Cycles GPU | 18:21:49 | 124 | 131 | 140 | 1154 / 648 / 0 |

Host helper stats: 61 fps, ~36 Mbps, RTT 7.4 ms, 0 holes, 0 QUIC loss. The
replica accepted a restarted host without a relaunch. Same ballpark as the
separate-pipeline Kyber run earlier today (~127 / 132).

## Open: heavy bootstrap over the VPN

A 40 M-vertex scene serializes to 441 MB. The first heavy host (18:23:44) got
all 106 chunks to the replica (replica seq 106 = host seq 106), but no valid
time was recorded (timer bugs on the Mac side, since fixed: `boot_s` now =
replica seq caught up with host seq). After that host was killed (~18:33),
new hosts got `QUIC connect: timed out` — the Windows replica stopped
answering on UDP 19990. Cause not yet known (Windows-side check requested:
is Blender wedged applying the 441 MB file, did the helper exit?). Paused
here by chris; resume with the Windows findings, then one timed heavy run
per transport (Kyber, ZMQ).
