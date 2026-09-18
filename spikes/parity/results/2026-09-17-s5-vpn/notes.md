# 2026-09-17 — S5 over the VPN: Mac host → Windows 4K replica, one Kyber connection 

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

## Heavy bootstrap over the VPN (worst-case startup, not a per-change cost)

40 M random vertices → 441 MB compressed bootstrap blob, 106 chunks. Time =
host session start → replica's pong reports every chunk received (host
clock only; pongs are 1 s apart). Includes serializing the file on the host.

| Transport | Run (UTC, 2026-09-18) | Time |
| --- | --- | ---: |
| Kyber (one connection) | 17:26:27 | **11.6 s** |
| ZMQ (TCP) | 17:30:21 | **11.65 s** |

Identical: over the VPN both are network-bound (~300 Mbps effective incl.
serialization), so the loopback difference (Kyber 3.1 s vs ZMQ 1.24 s) does
not show on a real link. Replica after each: seq 106/106, 0 gaps, 0 errors.

Context (chris): the wire bootstrap happens only at connect, project switch
or Force Resync; per-change traffic is deltas, and media/caches come from
the shared file system. A 441 MB blob is a deliberate worst case.

## Incident 2026-09-17: replica unreachable after a hard-killed heavy host

After the first heavy host was killed (SIGKILL) at ~18:33 UTC mid-session,
new hosts got `QUIC connect: timed out` from the Windows replica until it
was relaunched the next day. Same scene with a clean host stop on 09-18:
the replica stayed reachable. Windows-side log findings: see
`replica-notes.md` (Windows session).
