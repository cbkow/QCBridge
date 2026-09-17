# Windows session handoff — parity spikes

Written 2026-09-17 by the Mac session. Read this, then `spikes/parity/README.md`
and `spikes/parity/results/2026-09-17-mac-loopback/notes.md`.

## Context in five lines

- QCBridge = host Blender syncs a replica Blender; replica viewport is captured,
  HEVC-encoded and streamed (SRT today) back to QCView next to the host.
- We're exploring **responsiveness parity**: near-local feel on a good
  connection, exact fidelity when the image settles. Kyber (QUIC + RaptorQ FEC,
  from VLC's JB Kempf) is a serious transport candidate; Plank
  (github.com/instinctual/plank) is a VFX product that runs on Kyber's crates.
- **Host and replica can be any OS pairing** (Mac→Win, Win→Mac, Mac→Mac,
  Win→Win). Never assume a direction.
- Plan + reasoning: chris's doc "QCBridge — Responsiveness Parity Exploration".
- Mac loopback found: SRT costs latency setting + ~30–40 ms; Mac VideoToolbox
  HEVC holds ~70 ms + 2 frames. Windows NVENC numbers are the missing half.

## Ground rules

- Work on branch `spike/parity`. You may commit and push **to this branch only**
  (chris's OK for these sessions). `git pull --rebase` before every push.
- Put results in your own folder (`results/<date>-win-*/`) to avoid conflicts.
  If you must change shared scripts, keep the change small and say why in the
  commit message — the Mac session uses them too.
- Report exact numbers and failures as they are. Chris relays between sessions.
- Never block Blender's main thread; smoke/test ports 19990+ (chris runs live
  sessions on default ports).

## Tasks, in order

1. **Setup check.** Python 3.11+. ffmpeg with `libsrt` + `hevc_nvenc`: the
   scripts resolve QCView's `%LOCALAPPDATA%\QCView\toolbox.json` → PATH; if the
   MSIX path refuses to spawn (WinError 5), pass `--ffmpeg <path>`. Optional:
   `python -m pytest -q` (needs pytest + pyzmq).
2. **Windows pipe baseline** (encode + decode, no network), 20 s each:
   - `python spikes/parity/probe_testsrc.py --stdout --seconds 20 | python spikes/parity/probe_reader.py --stdin --seconds 15 --label win-pipe-nvenc-1080p60`
   - same at `--size 1280x720 --bitrate 10M --fps 30` and `--fps 60`
   - `--encoder x265 --size 1280x720 --bitrate 5M` if the ffmpeg has libx265
   - reader `--hwaccel none` once, to separate decode from encode
3. **Windows SRT loopback:** sender `--srt-listen 127.0.0.1:19998 --latency 120 --token spike`,
   reader `--srt 127.0.0.1:19998 --latency 120 --token spike --seconds 20`; repeat at latency 20.
4. **Kyber Windows build check** (the Kyber go/no-go). Install rustup (MSVC
   toolchain; Rust 1.89 is pinned by Kyber). Clone
   `https://github.com/instinctual/plank-kymux` (Plank's pinned Kyber crates:
   kynet, kyproto, kymux, …; AGPL) OUTSIDE the repo, then
   `cargo check` and `cargo check -p kymux --features backend-quinn,backend-wtransport`.
   On the Mac both compile cleanly; a Windows cross-check from the Mac only failed
   on missing Windows C headers for `ring`. Report pass/fail with the first error.
5. **Write** `spikes/parity/results/2026-09-17-win-loopback/notes.md` in the same
   table shape as the Mac notes, commit, push, and tell chris the headline.
6. **Then wait for chris** to coordinate cross-machine runs with the Mac session:
   Windows as replica (`probe_testsrc.py --srt-listen 0.0.0.0:19998 ...`, allow the
   firewall prompt for UDP) with the reader on the Mac, and the reverse. Chris
   supplies the VPN IPs.

## What the probe measures

Stamp (sender's `time.time()`) → decoded frame in `probe_reader.py`. Present
latency is excluded. The reader always runs on the machine that stamped, so no
clock sync is needed. Transport-only runs use `probe_testsrc.py`; Blender runs
use `QCB_PROBE=1` on both addons (see README).
