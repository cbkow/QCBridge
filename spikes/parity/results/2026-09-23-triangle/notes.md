# The three together — leg 1, two machines (2026-09-23)

Host: Blender 5.2.2 on the Mac, scripted session in agent mode (the
host agent 0.2.0 from `agent/target/release`). Replica: Blender 5.2 on the
Windows box under the replica agent 0.2.0 (logon task, kiosk), native
capture helper beside it. The two machines reach each other over the
VPN; everything below crossed it. Driven from the Mac seat over SSH
(`lab/ONE-SEAT.md` in QCBridgeAE).

## Finding each other

Direct probe (`{"t":"q"}` to UDP/4246 at the replica's address) answered
with the replica beacon: role, port 19990, version 0.2.0, certificate
fingerprint, `paired: false`. No firewall rule was needed for the probe
or for the QUIC attach: the agent process was already allowed when it
bound its ports. First attach failed with the replica's `token rejected`
(the two configs held different tokens); with matching tokens the host
agent attached silently and the beacon flipped to `paired: true`.
Multicast not exercised: different networks.

**Ticks:** discovery by direct address over the VPN — done.

## Sync

Host session started, bootstrap 576,640 bytes; one edit of each kind on a
timer, read back from the replica through the pong (`peer_status`) and
confirmed on a Windows screenshot of the replica's outliner and
transform panel:

| t (s) | edit | replica after |
|---|---|---|
| 0 | bootstrap | bootstraps 1 |
| 6 | `Probe.location.x = 3` (tier 1) | seq_fast 1, Location X 3 m |
| 12 | add Subdivision modifier (tier 2: Object + Mesh blobs, 170,511 + 168,245 bytes) | seq 2, modifier icon on the outliner row |
| 18 | rename → `ProbeRenamed` (tier 1) | seq_fast 2, name in the outliner |
| 24 | `frame_set(17)` (hot) | frame followed |

Final replica stats: seq 3, seq_fast 2, gaps 0, errors 0, unknown 0,
unmapped 0, frozen caches 0, local_edits 0, bootstraps 1, pixel
`streaming (native)`, ffmpeg resolved from QCView.

## The stream

The replica's native Windows capture (Desktop Duplication → Media
Foundation HEVC) with ffmpeg as the SRT mux, listener on the replica.

| viewer | result |
|---|---|
| QCView 2.3.4-era Release build on the Windows box, same machine | `LiveStreamDecoder: connected — hevc 3840x2160 yuv420p10le`, `LIVE — first frame published (d3d11va zero-copy)` in ~5 s |
| QCView 2.4.0 on the Mac, across the VPN | `connect failed: Input/output error` every 6 s while the Windows viewer held the slot; `connected — hevc 3840x2160 yuv420p10le` and `LIVE (videotoolbox zero-copy)` within one retry after the Windows viewer closed |

**One viewer at a time is the design** (`pixel_path.py`: an SRT listener
serves one viewer; the mux exits and restarts when the viewer leaves).
Worth a line in the user doc; not a fault. The rung was the host's
default (`hevc_10_420_100`), which the VPN carried at 4K.

## The shared root — path mapping, cache root, linked library (evening)

A test folder on a studio SMB share that both machines see: the Mac as a
`/Volumes/<share>/<folder>` path, the Windows box as the UNC path and,
in a second pass, as a mapped drive letter. One mapping row (mac root ↔
win root), **entered on both ends**. The host project (saved under that
root) held a relative image, an absolute image under the root, a stray
absolute image outside every mapping, a linked collection from a library
`.blend` under the root, and a cloth sheet whose point cache the host
externalized into `cache_root` under the same root and baked (24 frames).

| variant | replica after bootstrap + bake |
|---|---|
| replica row absent (first run) | `unmapped 4` — the absolute image, the stray, the library and the cache; Blender read the library as `C:\Volumes\…`, the Mac form turned into a drive path. The host panel's "unmapped paths — check path mappings" line is exactly the right alarm. |
| replica row = UNC root | `unmapped 1` (the stray only), `frozen 0`, `errors 0`, `last_error ""`, the library reloaded at the mapped path |
| replica row = mapped drive `M:` | same: `unmapped 1`, `frozen 0`, `errors 0`; the drive was mapped in the desktop session the replica runs in |

The row lives on both machines by design — the host translates its own
paths to the wire form with its table, the replica localizes the wire
form with *its* table — and the replica's saved table had rows for other
roots already, so this is a documentation point, not a code one: the
user doc must say the same row goes on both machines. The mapping
smoke's fifth check (`relative_image_mapped_to_replica_root`) has its
first real evidence here: the relative image crossed through
`project_dir`, the absolute one through the row.

**Ticks:** cross-OS path mapping; the shared cache root over SMB as UNC
and as a mapped drive; linked libraries by mapped path.

Along the way: the replica agent supervises its Blender — killed by hand
twice to reload preferences, it logged `Blender exited, relaunching` and
brought it back; the host re-bootstrapped each time.

## The phonebook (2026-09-24 morning)

`phonebook` on both agents pointed at a folder under the same shared test
root — the Mac as `/Volumes/…/qcb-lab/phonebook`, the Windows replica as
the UNC path (a TOML literal string, single quotes, or the backslashes
are eaten). The replica writes `qcbridge/<name>.json` there every 10 s by
temp-then-rename; over an hour of refreshes on the SMB share the agent
logged no write failure, with the Mac scanning the same file.

| step | result |
|---|---|
| host `discover` with no address (LAN sweep + phonebook), across the VPN | one peer, `sources ["phonebook"]` — multicast cannot cross the tunnel, so the phonebook is the whole answer |
| host `discover` with the replica's address | the same peer, `sources ["probe"]` |
| **zero-typing pairing:** host agent started with `peer = ""` and no fingerprint; session `discover` → pick the one entry → `set_config {peer, fingerprint}` | `connecting` → `connected` 8 s after the pick; the agent wrote the peer and the pinned fingerprint into its `agent.toml`; the replica bootstrapped |
| replica agent killed without a goodbye; scan 74 s later | entry still on disk, dropped by age: `discover` returns nothing from the phonebook and nothing from the probe |

**Ticks:** the phonebook on a share (UNC on the writer, the Mac path on
the reader, stale-drop). The mapped-drive form of the writer path was not
tried separately; it is the same path the mapping row already proved.

## Leg 1 reversed — host on Windows, replica on the Mac (2026-09-24)

A second agent instance on each box under `--config` (a replica on the
Mac, `tray = false`; a host on Windows), the same token, the Windows host
pointed at the Mac's tunnel address. The Windows host agent attached
across the VPN and pinned the Mac replica's certificate on first use; the
Mac replica agent launched its Blender on the attach. A scripted host
Blender on the Windows desktop (the same four edits) against that agent:

| | |
|---|---|
| bootstrap | 1 on the Mac replica |
| tier 1 move, tier 2 modifier, rename, frame | seq 3, seq_fast 2, gaps 0, errors 0 |
| the Mac replica's pixel path | `streaming` — the ffmpeg (avfoundation → VideoToolbox) path; the native Mac helper is not built on this checkout |
| QCView on Windows on the Mac's `srt://` across the VPN | `connected — hevc 1920x1200 yuv420p10le`, `LIVE (d3d11va zero-copy)` 1.3 s after launch |

The direction is not baked in anywhere: both roles on both boxes, both
ways across the tunnel.

Along the way, a Windows finding worth its own item: the replica agent
killed hard (`Stop-Process`) leaves its Blender, capture helper and mux
alive holding its ports, and the next agent then exits at once with no
line in its log. Killing the orphans first restores it. The agent should
own its children's lifetime (a job object on Windows) and say why it
cannot bind.

## 2026-09-24, afternoon — the pair on the hygiene and token builds

Both agents rebuilt on the two commits of the day ("Windows hygiene",
"the token stays in the agent"); each box's `agent.toml` had a token in
clear from the first paired day, and each first start moved it into the
OS store (macOS Keychain on the host, Credential Manager on the Windows
replica) and blanked the file. Then, with both tokens read back from the
stores alone: the QUIC attach in one second, the replica's Blender
launched, and the scripted host (the same four edits):

| | |
|---|---|
| hello | `connected` — both ends carrying the hello secret derived by their agents |
| tier 1 move, tier 2 modifier, rename, frame | seq 3, gaps 0, errors 0 on the replica |
| replica pixel path | `streaming (native)` on the passphrase the replica derived |
| QCView on Windows on the replica's `srt://` | `connected — hevc 3840x2160 yuv420p10le`, `LIVE (d3d11va zero-copy)` — the URL's passphrase is the host-side derivation, so the two derivations agree |

The first attempt of the day said *denied: token mismatch*: the replica
session captured its hello secret before its transport had attached, and
the empty value used to be hidden by the token arriving in an environment
variable. Both ends now resolve the secret at the moment of use. Nothing
else changed on the wire.

## 2026-09-24, later — the host's rows reach the replica in the hello

Both agents on the mapping commit ("the path-mapping table and the cache
root live in the agent"). The Windows replica's own table was emptied
(its Blender preferences had three rows; now none; its agent has none),
so its only source of rows is the host. The shared-root scene from the
evening of the 23rd, driven by the same script, the Mac host's
preferences carrying the one row and the cache root:

| | |
|---|---|
| first session against a Mac agent with no table | the addon logged *moved path_mappings, cache_root into the agent*; the Mac's `agent.toml` now holds the row and the root |
| replica, no row of its own | `unmapped 1` (the stray only), `frozen 0`, `errors 0`, `last_error ""` — the same as with the row entered there (23rd: absent row gave `unmapped 4`) |
| host bake into the shared cache root | externalized before the bake, baked, 24 files |
| sync | seq 3, gaps 0, bootstraps 2 |

So the row is entered once, on the host. A replica that still has rows of
its own keeps them; the host's rows it lacks are appended.

## 2026-09-24, evening — the Send/Receive switch, live, both ways

Both agents on the commit "the Send/Receive switch is live". Driven from a
control client on each box (the way the settings window will), no
restarts, no second instances:

| step | what the logs said |
|---|---|
| Mac host → replica (`set_config role`) | *listening … now replica* within the second; the beacon answers on 4246; `agent.json` re-keyed to `replica` |
| Windows replica → host, peer = the Mac's tunnel address | *pinned replica certificate … now host*; the Mac: *host connected* in the same second (Blender supervision off there for the test) |
| Windows host → replica | *listening … now replica* |
| Mac replica → host | *now host*; Windows: *host connected*, *Blender launching* five seconds after its switch |

Both `agent.toml`s ended as they began (role, Blender path). One
cosmetic finding fixed after the run: the accept loop logged the
endpoint's close as *session ended: listener closed* twice; the loop is
now aborted before the endpoint closes.

## Not yet

Glass-to-glass numbers (Phase 4 item 3). The AE leg ran on 2026-09-24 and is
written up on the QCView side (QCBridgeAE `lab/results/2026-09-23-triangle/`):
the Windows replica's stream on A and AE's Transmit ring on B, both live in
QCView on Windows, with the Mac hosting across the VPN.
