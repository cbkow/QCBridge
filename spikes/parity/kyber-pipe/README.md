# kyber-pipe (parity spike S2)

A minimal Kyber video pipe for comparing transports with the probe harness:

    probe_testsrc.py --stdout | kyber-send  ==QUIC/RaptorQ==>  kyber-recv | probe_reader.py --stdin

- `kyber-send` reads Annex-B HEVC from stdin, splits access units on AUD NALs
  (type 35; `probe_testsrc.py --stdout` inserts them), flags IRAP NALs (16-23)
  as keyframes and sends each AU as one KyProto media packet on a
  `VideoProtocol::UnreliableFec` lane. Before every keyframe it sends an empty
  `is_config` packet (Plank's pattern); VPS/SPS/PPS travel in band, and cached
  ones are re-inserted if a keyframe arrives without them.
- `kyber-recv` connects with the sender's certificate pinned by SHA-256, writes
  every AU to stdout, flushes per AU, and reconnects with backoff.

Standalone Cargo package, not part of any workspace. Kyber crates (`kynet`,
`kyproto`, `kymux-types`) are git dependencies pinned to
`instinctual/plank-kymux@912ece5c64787997f978673ca60d313898a3548c`, the commit
Plank `20ee198` uses. Rust 1.89.0 is pinned by `rust-toolchain.toml`.

## Build

Needs network on the first build: Kyber's `kyproto` has a wasm-only git
dependency (`kywasmtime`) that Cargo still resolves, so `--offline` fails
until it is cached.

macOS / Linux:

    cd spikes/parity/kyber-pipe
    cargo build --release
    cargo test --release --lib        # Annex-B splitter tests

Windows x64 (MSVC toolchain via rustup; VS Build Tools for `ring`'s C/asm):

    cd spikes\parity\kyber-pipe
    cargo build --release

Binaries land in `target/release/` (`kyber-send[.exe]`, `kyber-recv[.exe]`).

## Run

Sender (replica side). Prints `fingerprint <sha256hex>` to stderr; the cert
and key persist in `--cert-dir` (default `<temp>/qcbridge-kyber-pipe`, never
the repo), so the fingerprint is stable across restarts.

macOS:

    python3 spikes/parity/probe_testsrc.py --stdout --size 1920x1080 --bitrate 50M \
      | spikes/parity/kyber-pipe/target/release/kyber-send --listen 127.0.0.1:19999 \
          --token spike --bitrate-cap-mbps 80 --fps 60

    spikes/parity/kyber-pipe/target/release/kyber-recv --connect 127.0.0.1:19999 \
        --token spike --fingerprint <sha256hex> \
      | python3 spikes/parity/probe_reader.py --stdin --seconds 20 --label mac-kyber-loop

Windows: PowerShell before 7.4 re-encodes pipes between native programs and
corrupts the bitstream, so run the pipelines under `cmd /c` (binary-safe):

    cmd /c "python spikes\parity\probe_testsrc.py --stdout --size 1920x1080 --bitrate 50M | spikes\parity\kyber-pipe\target\release\kyber-send.exe --listen 0.0.0.0:19999 --token spike --bitrate-cap-mbps 80 --fps 60"

    cmd /c "spikes\parity\kyber-pipe\target\release\kyber-recv.exe --connect <replica-ip>:19999 --token spike --fingerprint <sha256hex> | python spikes\parity\probe_reader.py --stdin --seconds 20 --label win-kyber"

Allow the firewall prompt for UDP on the listening side.

### Options

`kyber-send`

| Flag | Meaning |
| --- | --- |
| `--listen HOST:PORT` | UDP listen address (numeric host) |
| `--token T` | clients presenting another token are rejected |
| `--bitrate-cap-mbps N` | **wire** pacing rate for datagrams, FEC included. Give it at least video bitrate x 1.35 + 1 (Plank's budget), more for keyframe headroom |
| `--cert-dir DIR` | where `cert.der` / `key.der` live |
| `--fps F` | pts in 90 kHz units (default: pts = AU index) |
| `--queue N` | AUs allowed to wait for the network (default 6); on overflow the queue is flushed and sending resumes at the next keyframe |
| `--mtu BYTES` | fixed QUIC UDP payload (default 1344, like Plank) |
| `--cc fixed\|quinn` | fixed rate-derived congestion window (default) or Quinn's CUBIC |
| `--no-pacer` | don't pace before Quinn (Quinn's send buffer then drops oldest datagrams under backlog) |

`kyber-recv`: `--connect HOST:PORT --token T --fingerprint HEX [--server-name N] [--mtu BYTES]`.

### Stats (stderr, once a second)

- send: `aus_in`, `sent`, `keys`, `video_mbps` (payload), `est_wire_mbps`
  (payload x 1.35, an estimate: Kynet exposes no byte counters), `queue`,
  `queue_drops`, `gop_skips` (AUs skipped waiting for a keyframe after
  connect/overflow), `idle_discards` (no client), `dwell_ms` avg/max (stdin
  arrival to `send()` returned, i.e. queueing + pacing), QUIC `rtt_ms`,
  `quic_lost`.
- recv: `frames`, `keys`, `video_mbps`, `holes`, `max_gap_ms` between AUs,
  and everything `kyproto::ProtocolStats` has: `dropped_packets` (KyProto
  packets given up after the 50 ms reorder deadline), `fec_src_symbols`,
  `fec_missing` (source symbols that did not arrive), `fec_unrecovered`
  (missing and not repaired), plus QUIC `rtt_ms`, `quic_lost`.

## Patched quinn-proto (vendored)

Plank patches `quinn-proto 0.11.17` (DATAGRAM send-buffer accounting; see
`vendor/quinn-proto-0.11.17/PLANK-PATCH.md`). A git dependency on
`instinctual/plank@20ee198` works but Cargo checks out the whole repository
with its submodules (Sunshine and Moonlight forks): **4.1 GB** on the first
build, which is impractical for a spike and for the Windows box. The crate
(1.2 MB, MIT/Apache-2.0 licences retained, `LICENSE-MIT`, `LICENSE-APACHE`) is
therefore copied verbatim into `vendor/` and wired in with `[patch.crates-io]`.
No Kyber source is copied into this repo.

`src/lib.rs` has a small fixed-rate Quinn controller written for the spike
along the lines of Plank's `rate_control.rs` (window from rate x RTT, loss
ignored; the pacer owns the rate).

## License

Kyber is `LicenseRef-Kyber-Commercial OR AGPL-3.0-or-later`. These binaries
link it, so they are AGPL-3.0-or-later as distributed (the manifest says so).
They are separate executables that talk to QCBridge only through pipes, not
linked into the add-on or QCView; a product integration would need either the
AGPL obligations or a commercial Kyber licence.

## Known limits

- One client at a time; one video lane; no audio/input/data lanes, no clock
  sync, no adaptive bitrate (fixed cap), no encoder feedback (a lost frame is
  not answered with an IDR request; decode stays corrupt until the next
  keyframe, 1 s with the probe sender's GOP).
- Pacing is per datagram at the cap, so every AU waits roughly
  `AU size x 1.35 / cap` before it is fully on the wire; large keyframes
  wait longest. The very first IDR from VideoToolbox is large enough to
  overflow the default queue at 80 Mbps, so a new client usually starts at
  the second keyframe (~1-2 s).
- The TLS server name isn't verified (the fingerprint pins the cert).
- `est_wire_mbps` is computed, not measured.
- Windows build not yet verified on a Windows machine.
