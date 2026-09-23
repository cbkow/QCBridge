# Parity spikes

Responsiveness-parity exploration (2026-09-17). Plan and reasoning live in
the "QCBridge — Responsiveness Parity Exploration" doc; this folder holds the
code and results. Host and replica may be any OS pairing — every script here
runs on macOS and Windows.

## Pieces

| File | Runs on | Does |
| --- | --- | --- |
| `probe_testsrc.py` | replica side | synthetic stamped frames → encoder → SRT listener or Annex-B stdout (no Blender) |
| `probe_reader.py` | **host machine** | decodes SRT or Annex-B stdin, reads the strip, logs latency p50/p95/p99 (+ JSONL) |
| `qcbridge/ring1/probe.py` | both | strip bit format + env knobs |
| addon (env-gated) | host + replica Blender | `QCB_PROBE=1` stamps hot packets / draws the strip |

Both scripts resolve ffmpeg like the addon (QCView toolbox.json → PATH) or
take `--ffmpeg`. Plain Python 3.11+, no packages.

## S0/S2 transport-only runs (no Blender)

Replica machine:

    python spikes/parity/probe_testsrc.py --srt-listen 0.0.0.0:19998 --latency 120 --token spike

Host machine:

    python spikes/parity/probe_reader.py --srt <replica-ip>:19998 --latency 120 --token spike --seconds 30 --label <pairing>-srt120 --out spikes/parity/results/<date>-<pairing>/runs.jsonl

Pipe baseline on one machine (encode + decode, no network):

    python spikes/parity/probe_testsrc.py --stdout --seconds 20 | python spikes/parity/probe_reader.py --stdin --seconds 15 --label <os>-pipe

`--encoder auto` picks videotoolbox on macOS, nvenc on Windows.

## S0/S1 with Blender (addon on this branch, both ends)

Set the environment before launching Blender:

| Var | Where | Values |
| --- | --- | --- |
| `QCB_PROBE=1` | host + replica | enable stamps / strip |
| `QCB_HOT_HZ` | host | 30 (default), 60 |
| `QCB_PROBE_ORBIT` | host | degrees/s, e.g. 45 (perspective view, not camera view) |
| `QCB_CAPTURE_FPS` | replica | 30 (default), 60 |

Then read the replica's normal stream from the host with `probe_reader.py
--srt <replica-ip>:<srt_port> --latency <srt_latency_ms> --token <token>`
(the strip sits in the top-left of the replica viewport; `--band` if the
viewport doesn't start at the top of the captured display).

## Results

`results/<date>-<pairing>/` — `notes.md` with the table + readings, raw JSONL.
