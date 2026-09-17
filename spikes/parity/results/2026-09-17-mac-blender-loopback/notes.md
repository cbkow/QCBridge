# 2026-09-17 — Mac, Blender in the loop (host + replica on one machine)

`blender-smoke/run_probe_smoke.sh`: two factory-startup Blender 5.2 windows
on chris's MacBook Pro (3024x1964 Retina). Host orbits the default cube at
45°/s in solid shading and stamps hot packets; replica draws the strip,
captures the WHOLE display with avfoundation, VideoToolbox HEVC Main10 50M,
SRT listener latency 60. `probe_reader.py --crop 1400:256:0:1634` on the
same Mac. Latency = host stamp at hot-sample time → decoded frame in the
reader (present excluded; the stamped view matrix is ≤ 1 host tick older than
the stamp). Raw rows: `runs.jsonl`.

| Label | Hot Hz | Capture fps | SRT latency | p50 | p95 | Frames / scored / stale / failed | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| mac-blender-solid-hot60-cap60-srt60 | 60 | 60 | 60 | 279 | 288 | 1201 / 420 / 238 / 543 | edge-locked strip (fixed after) |
| mac-blender-solid-hot60-cap60-srt60 | 60 | 60 | 60 | 258 | 267 | 1200 / 653 / 375 / 172 | load avg ~7 (cargo build) |
| mac-blender-solid-hot60-cap60-srt60 | 60 | 60 | 60 | 274 | 283 | 1201 / 763 / 438 / 0 | clean decode; load avg ~6 |
| mac-blender-solid-hot30-cap30-srt60 | 30 | 30 | 60 | 272 | 281 | 1200 / 458 / 742 / 0 | load avg ~5 |

A cargo release build (Kyber spike) was running in parallel for all rows:
treat absolute numbers as ±15 ms until rerun on a quiet machine.

## Readings

- **The probe works with Blender in the loop:** strip drawn by the replica's
  POST_PIXEL overlay, captured, encoded, decoded on the host clock, 0 failures
  once the lock centers on the blocks.
- **~260–280 ms p50 end to end** on one Mac with SRT at 60 ms. Rough split
  from the loopback runs: SRT ≈ 60 + 30–40 ms, VideoToolbox + decode ≈ 105 ms,
  leaving ~60–80 ms for hot send → replica apply → draw → avfoundation capture.
- **30 Hz vs 60 Hz changed nothing measurable here (272 vs 274).** The chain
  is dominated by fixed costs (VT, SRT), not sampling phase. Revisit once
  those drop — the plan's "move everything to 60 Hz" is not the first lever.
- **Replica draws ~38 new stamps/s at 60 Hz** (763 scored in 20 s): Blender's
  viewport redraw, not the sampler, caps update rate in solid mode.

## Bug found and fixed on this branch

**macOS replica ffmpeg hangs after the SRT viewer disconnects.** It logs
`Error muxing a packet … Error closing file` but never exits (avfoundation
input keeps it alive; SIGTERM ignored), so the supervisor never respawns and
the stream is dead until Blender restarts. Fix in `ring0/pixel_path.py`:
stderr is pumped through a thread; a fatal output line arms a 2 s reaper
that force-kills a process that didn't exit. Verified: respawns at 11:20:18
and 11:20:47 after two reader disconnects. Worth landing on main regardless
of the spike's outcome. Windows behavior on viewer disconnect not yet checked.
