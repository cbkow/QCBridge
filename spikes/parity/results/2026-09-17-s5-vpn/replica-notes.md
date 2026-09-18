# 2026-09-17/18 — S5 over the VPN: Windows replica side

Companion to `notes.md`. Replica: Windows 11, RTX 5090, Blender 5.2,
launched by `C:\qcb-smoke\replica_s5_kyber.cmd` (`QCB_TRANSPORT=kyber`,
`QCB_SMOKE_STREAM=1`, `QCB_SMOKE_KIOSK=1`, OptiX, bind 0.0.0.0). All times are
UTC on the Windows clock, which `w32tm` measured at +17 ms against
time.apple.com at 17:40 on 09-18. Raw files stay on the replica box under
`C:\qcb-smoke\diag-20260917-1831\`, `run-20260918-kyber-heavy\` and
`run-20260918-zmq-heavy\`.

## Build and tests (Windows)

- `cargo build --release --bin qcb-helper` in `spikes/parity/kyber-pipe` at
  `6c2329f`: **OK** (4.83 s, already built). The only warnings are two
  `dead_code` warnings in vendored `quinn-proto` 0.11.17
  (`DatagramBufferTelemetry`).
- `pytest -q` in the repo root (`C:\qcb-smoke\venv`): **85 passed, 2 skipped**
  in 13.8 s.

## Incident: replica unreachable after 09-17 ~18:33

The Mac's hosts got `QUIC connect: timed out` on UDP 19990. On 09-18 before
the relaunch, the replica showed:

| Check | Finding |
| --- | --- |
| Processes | No `blender.exe`, no `qcb-helper.exe`, no `ffmpeg.exe` |
| UDP 19990 | Nothing listening |
| `qcbridge-helper.log` | Last written **18:31:12**. One spawn header (18:16:37), then 8,829 ffmpeg `non monotonically increasing dts` warnings and nothing else: no lane-ended, framing, connection-closed or panic lines |
| `replica.json` | `t` = 1789669872.2 = **18:31:12**, then no further writes. `bootstraps: 4`, `seq: 106`, `gaps: 0`, `apply_errors: 0`, `last_error: ""`, `host_ended: false`, `session_note: "host connected"`. Video at 60 fps, 34.4 Mbps, 0 holes, 0 QUIC loss, RTT 9.2 ms |
| Blender output (`blender_s5_kyber.out`; there is no `replica.log`) | Four `Read blend: ...qcb-in-boot-0-1.blend` lines, the last at 18:29:19.8 (the 441 MB file), no memory errors. The last line, at 18:31:12, is `Saved session recovery to ...\quit.blend` |
| RAM | No process left to measure. The machine had 224 of 256 GB free |
| System | No reboot since 08-25. No Application Error/WER events for blender, qcb-helper or ffmpeg. No shutdown or resource-exhaustion events |

**Diagnosis.** The replica did not crash and was not brought down by the host
kill. Blender **quit normally at 18:31:12**: `quit.blend` is written only on
Blender's regular exit path. That is about 2 minutes *before* the Mac host was
killed (~18:33 on the Mac's clock). The helper and its ffmpeg exited in the
same second as their parent. Up to that point the session was healthy: the
441 MB bootstrap loaded, and there were 0 gaps and 0 apply errors. No repo
code quits Blender (there is no `quit_blender` or `sys.exit` on the replica
path), so the quit came from outside, most likely the kiosk window or its
console being closed on the replica machine. The logs can't tell which. The
Mac's timeouts came from nothing listening on 19990. On 09-18 a clean host
stop left the replica reachable (checked 6 s later). A hard-killed host has
not yet been tested against a replica that stays up.

## Per-run replica stats: EEVEE and Cycles runs (09-17)

The replica dumps a single cumulative `replica.json` snapshot every second and
does not keep history, so there are **no per-run replica stats** for
`vpn-s5-eevee-r1/r2` and `vpn-s5-cycles-r1/r2`. What the replica side does
record:

- One Blender session covered all four latency runs plus the heavy host:
  started 18:16:34, quit 18:31:12.
- Bootstrap loads (Blender log clock + session start, ±1 s): **18:18:51**,
  **18:20:49**, 18:24:07, 18:29:20 (441 MB). The first two come just before
  the EEVEE runs (18:19:19, 18:19:54 on the Mac) and the Cycles runs
  (18:21:13, 18:21:49). That fits one host session per engine, and the
  replica accepting a restarted host without a relaunch. The replica side
  can't attribute the 18:24:07 bootstrap.
- End-of-session cumulative stats (18:31:12): `apply_errors: 0`, `gaps: 0`,
  `unknown_uuid: 0`, `unmapped_paths: 0`, `last_error: ""`. Transport: 60 fps,
  34.4 Mbps, 555 keyframes, 153 GOP skips, 0 video holes, 0 queue drops,
  0 QUIC lost, RTT 9.2 ms, sender dwell avg 0.49 ms / max 5.8 ms.
- The kiosk region was **3840×2160** for these runs.

Recording per-run stats needs a replica-side snapshot at each host session's
end (for example, append a line when `host_ended` flips or the host changes).

## Heavy bootstrap, 441 MB (09-18)

| | Kyber (17:26) | ZMQ (17:30) |
| --- | ---: | ---: |
| Replica result | seq 106, 0 gaps, 0 errors, `bootstraps: 1` | seq 106, 0 gaps, 0 errors, `bootstraps: 1` |
| Blender peak working set | **2,605 MB** | **2,085 MB** |
| Blender peak private (paged) | 4,570 MB | 3,935 MB |
| After the load | 1,397 MB working set | — |
| `Read blend` → dump with `bootstraps: 1` | not captured | **≤ 0.96 s** |

Idle replica Blender is about 0.8–0.9 GB. The Kyber process also carries the
capture/stream path, which likely explains its extra ~0.5 GB. Neither run came
close to memory pressure.

ZMQ timeline, from a 100 ms watcher on `replica.json` and the Blender output
(each dump is 1 s apart):

| UTC | Event |
| --- | --- |
| 17:30:32.6 | first dump showing `host connected` |
| 17:30:33.5 → 17:30:42.6 | `seq` 8 → 98 at about 10 chunks/s (~4.2 MB each, ≈ 330 Mbps) |
| 17:30:43.64 | `Read blend` (Blender blocks here, so there are no dumps between 42.6 and 44.6) |
| 17:30:44.6 | `bootstraps: 1`, `seq: 106` |

On the replica clock, host connect to scene applied takes about 12 s, which
matches the Mac's 11.65 s. Loading a 441 MB blend therefore takes under a
second; the network dominates.

For Kyber, `Read blend` came at about 17:26:49.9 (process start 17:25:42 plus
the log clock's 67.9 s). The `bootstraps: 1` time wasn't captured because
dumps have no history.

**Clock caveat.** The Mac's table puts the ZMQ run at 17:30:21, but the
replica first saw the host at 17:30:32.6, about 11 s later. The Kyber run
shows the same offset: the Mac's start of 17:26:27 plus 11.6 s puts the
replica's `Read blend` at 17:26:49.9, about 11 s after the Mac's end time.
Windows is within 17 ms of NTP. Either the Mac's time column is not session
start, or the Mac clock is about 11 s behind. The latency numbers are
host-clock only and are unaffected. Check this before any cross-machine
timing.

## Capture ffmpeg: dts warnings and one capture loss

- **dts warnings.** The helper log is almost entirely
  `[hevc] Application provided invalid, non monotonically increasing dts to
  muxer in stream 0: N >= N`. There were 8,829 in the 09-17 session, about
  10/s at 60 fps (roughly one frame in six), and 1,482 in the 09-18 Kyber run.
  Each warning is a frame whose dts equals the previous one: ddagrab feeds
  `-fps_mode passthrough` (deliberate, see `qcbridge/ring0/pixel_path.py`)
  into the raw `-f hevc` muxer. Raw Annex-B carries no timestamps, and video
  showed 0 holes, so the warnings are **harmless**. Their real cost is log
  volume (1.1 MB in 15 min), which buries real errors. Consider filtering
  them or raising the capture child's log level.
- **Counter resets.** In the 09-17 session the dts sequence restarts from
  near zero 4 times, so the capture ffmpeg was restarted 4 times, as many
  times as there were bootstraps. The helper's `restarting` events go to its
  stdout, not to this log, so the cause isn't recorded.
- **Capture loss (09-18 Kyber run).** `[Parsed_ddagrab_0] AcquireNextFrame
  failed: 887a0026` (`DXGI_ERROR_ACCESS_LOST`), after which ffmpeg exited and
  the helper respawned it (new ffmpeg at 17:28:33). Streaming resumed, but the
  first dump after the respawn showed 55 fps / 3.6 Mbps. At the same time the
  kiosk window changed from 2560×1440 fullscreen to a 2546×1586 window at
  (7,7).
- **Display is not stable.** This machine has a Parsec Virtual Display
  Adapter alongside the RTX 5090. The replica desktop was 3840×2160 on 09-17,
  2560×1440 on the first 09-18 relaunch, and is **2560×1600** in the
  replica left running now. A remote-desktop session changing the display
  mode would explain both the ACCESS_LOST and the size changes, though this is
  not confirmed. It means **today's replica is not capturing at 4K**, unlike
  the 09-17 latency runs. Pin the display mode before comparing latency
  across days.

## State left running

S5 configuration, relaunched at 17:41 on 09-18: `QCB_TRANSPORT=kyber`,
stream on, kiosk on. `replica.json` shows `session_note: listening`,
`pixel: streaming`, and qcb-helper is bound to UDP 0.0.0.0:19990. The kiosk
region is 2560×1600.
