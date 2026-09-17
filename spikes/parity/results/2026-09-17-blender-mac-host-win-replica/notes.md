# 2026-09-17 — Blender over the VPN: Mac host → Windows replica (SRT vs Kyber)

The real product path. **Host** = chris's Mac, Blender 5.2 factory startup,
`blender-smoke/probe_host.py`: default scene, view orbits 45°/s, hot state
sampled at 60 Hz and stamped with the Mac clock. **Replica** = Windows
workstation (RTX 5090), `probe_replica.py`, kiosk on a 3840x2160 display,
Rendered viewport; engine from the host (EEVEE, or Cycles on GPU — OptiX
requested, backend unverified). Capture `ddagrab` 60 fps → `hevc_nvenc`
p4/ull/delay 0/bf 0, 50M, GOP 60. **Transport**: addon's SRT listener at
latency 20, or `ffmpeg … -f hevc - | kyber-send` cap 400 (addon stream off).
**Reader** on the Mac (`probe_reader.py`, VideoToolbox decode, bottom band).
VPN RTT 7–11 ms. Code `f6ff401`. Replica-side details: `replica-notes.md`.

**No clock correction:** stamp and reading are both the Mac clock. Latency =
host hot sample → first decoded frame whose strip carries that stamp. The
strip is drawn on every viewport redraw, so for Cycles this is the first
(noisy) redraw at the new view, not convergence. The stamped view matrix is
≤ 1 host tick older than the stamp (view_matrix refreshes on draw).

| Label | Engine | Transport | Time (UTC) | p50 | p95 | p99 | min | Scored / stale / failed |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| vpn-blender-eevee-srt20-r1 | EEVEE | SRT 20 | 16:19:47 | 160 | 166 | 174 | 130 | 1115 / 688 / 0 |
| vpn-blender-eevee-srt20-r2 | EEVEE | SRT 20 | 16:20:25 | 148 | 165 | 167 | 122 | 1117 / 685 / 0 |
| vpn-blender-cycles-srt20-r1 | Cycles GPU | SRT 20 | 16:26:47 | 153 | 162 | 177 | 139 | 1166 / 636 / 0 |
| vpn-blender-cycles-srt20-r2 | Cycles GPU | SRT 20 | 16:27:25 | 153 | 162 | 170 | 128 | 1160 / 643 / 0 |
| vpn-blender-eevee-kyber400-r1 | EEVEE | Kyber cap 400 | 16:34:16 | 128 | 130 | 134 | 102 | 1164 / 638 / 0 |
| vpn-blender-eevee-kyber400-r2 | EEVEE | Kyber cap 400 | 16:34:54 | 126 | 131 | 135 | 115 | 1156 / 646 / 0 |
| vpn-blender-cycles-kyber400-r1 | Cycles GPU | Kyber cap 400 | 16:36:13 | 127 | 134 | 147 | 115 | 1142 / 658 / 2 |
| vpn-blender-cycles-kyber400-r2 | Cycles GPU | Kyber cap 400 | 16:36:51 | 128 | 133 | 147 | 114 | 1156 / 646 / 0 |

Frames = 1802–1803 per 30 s read (60 fps capture). Kyber receiver: dropped
packets, FEC missing/unrecovered, QUIC lost and holes all 0 in every run;
max frame gap 31–41 ms, one 58.4 ms (cycles r1).

## Readings

- **Kyber: ~127 ms p50 / ~132 p95, host motion → decoded frame, 4K, over
  the VPN.** SRT at latency 20: ~153 / ~164. Kyber is ~25 ms faster at p50,
  ~32 ms at p95, and repeatable run to run (126–128 vs SRT's 148–160).
- **Blender + 4K adds ~58 ms over the synthetic path** (Kyber 69 → 127; SRT
  91 → 153, same delta): hot send over the VPN (~5), replica apply tick,
  viewport draw, DWM composition + ddagrab at 4K, 4K encode.
- **Cycles vs EEVEE: no difference** in time-to-first-redraw. Convergence is
  not measured by this probe.
- **~36% of captured frames repeat a stamp** (≈ 38 new stamps/s into 60 fps):
  the replica's 4K viewport redraw rate caps motion updates, in both engines.
- **The "10-bit" rung is 8-bit on this path:** receiver sees `profile=Main 10`,
  `pix_fmt=yuv420p` (8-bit), 3840x2160, 60/1. `ddagrab` hands NVENC 8-bit BGRA
  and `pixel_path` never asks for p010 on the GPU path (the Mac branch does).
  Belongs to S3 (format fidelity).
- **Windows ffmpeg respawns on viewer disconnect** (no hang, unlike macOS):
  listener back 4–8 s after each disconnect (replica-notes.md).

## Budget view (Windows replica → Mac host, Kyber, p50)

| Stage | ms | Source |
| --- | ---: | --- |
| Hot send + replica apply + 4K draw + compose/capture + 4K encode (Blender delta) | ~58 | this run − synthetic |
| NVENC encode + VideoToolbox decode floor (1080p synthetic) | ~43 | win loopback pipe |
| Kyber transport over VPN | ~26 | win2mac synthetic − pipe |
| **Total** | **~127** | this run |
