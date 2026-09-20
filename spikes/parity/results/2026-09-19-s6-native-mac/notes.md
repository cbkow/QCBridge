# 2026-09-19 — S6 first cut: native macOS capture (ScreenCaptureKit → VideoToolbox) in the agent

`agent/capture-mac/qcb-capture-mac.swift`: SCK display capture (queueDepth 3,
≤3 frames in flight, skip don't queue) → VTCompressionSession HEVC (RealTime,
no reordering, speed priority, BT.709/sRGB tags, GOP 600 + keyframe on
request via stdin "key") → Annex-B with AUD per AU on stdout. The agent
prefers it over the addon's ffmpeg argv when it sits beside the binary
(`native_capture_argv`), passes fps/bitrate/scale, and asks for a keyframe
when a session's video lane starts. Build: `agent/build-mac.sh`.

All runs on this Mac, both roles as agents, host Blender → host agent →
replica agent → agent-launched Blender; display at 6720×3780 (capture is
the whole display; a kiosk replica would be 4K). Latency = host motion →
decoded frame at the host agent's tcp port. `runs-agent.jsonl` in the S5
one-connection folder.

| Capture path | fps | p50 | p95 | p99 | Notes |
| --- | ---: | ---: | ---: | ---: | --- |
| ffmpeg avfoundation → hevc_videotoolbox (ffmpeg pipe) | 30 | 374 | 394 | 410 | this morning, same display |
| native SCK → VT | 30 | 222 | 239 | 244 | −150 ms |
| native SCK → VT | 60 | 188 | 202 | 205–214 | encoder ~48 ms/frame at 25 MP → ~52 fps sustained, rest skipped |

Encoder time (VT, Main 8-bit): 27 ms at 6720×3780 idle desktop, 35 ms at
30 fps under motion, 48 ms at 60 fps. Idle desktop costs ~0.3 Mbps: SCK
delivers only changed frames.

## Open (resume here)

- Half-resolution run (`capture_scale = 0.5` → 3360×1890, comparable to
  yesterday's 217 ms ffmpeg/helper run) was interrupted: SCK suddenly
  reported **0 displays** (screen locked/asleep, or a Screen Recording
  permission prompt for the terminal app). Check the Mac, rerun.
- Debug logging in the Swift (first-frame status) can go; `--10bit`
  untested; `--region` (viewport-only capture, S8) untested.
- The agent's replica Blender inherits capture fps from the agent's env
  (`QCB_CAPTURE_FPS`); should become agent config.
- Windows side of all of this (S7 + agent build) not started.
