# Control audit — the tray agent and the Blender extension (2026-09-24)

What exists today for control, secrets, the phonebook and installation;
what the two sister products (MinRender, UFB) do for the same problems;
and a proposed shape for QCBridge. Written after the paired runs of
2026-09-23/24, which exercised every path below across two machines.
No addresses, names or secrets appear here.

## 1. What exists

### 1.1 Everything the agent and the addon persist

| file | holds | secret? | plain text? |
|---|---|---|---|
| `<config>/QCBridge/agent.toml` | role, **token**, listen, peer, pinned fingerprint, name, discovery mode, phonebook dir, Blender path/args, kiosk, idle, cap, scale | token | yes, default file mode |
| `<config>/QCBridge/agent.json` | per role: pid, loopback port, **per-start secret** for the addon's attach | secret | yes; regenerated each start; removed on clean exit |
| `<config>/QCBridge/cert/{cert,key}.der` | the replica's self-signed certificate and **private key** | key | raw DER, no passphrase |
| `<phonebook>/qcbridge/<name>.json` | the beacon: name, role, ip, port, fingerprint, version, paired, timestamp | no (by design: the beacon never carries the token) | yes, on the share |
| Blender `userpref.blend` | the addon preferences incl. **token** (`PASSWORD` subtype masks it in the UI only), addresses, ports, stream settings, path mappings, cache root | token | yes |
| `<blender config>/qcbridge_settings.json` | a mirror of the preferences **including the token**, written on every session start so a reinstall keeps them | token | yes |
| env of an agent-launched Blender | `QCB_AGENT_TOKEN`, `QCB_AGENT_SECRET`, port | token, secret | process env |
| the viewer URL (Copy Stream URL, `qcview://` deep link) | `srt://…&passphrase=<derived from the token>` | a token-equivalent for the stream | on the clipboard / in a URL |

The SRT passphrase is never stored: both ends derive it from the token
(`sha256("qcb-srt:" + token)[:32]`), and an empty token means an
unencrypted stream.

**Finding 1.** Every secret is plain text on disk, in five places for the
token alone, and one of them (the settings mirror) exists precisely to
outlive an uninstall. There is no OS keychain use anywhere.

**Finding 2.** "Copy Stream URL" hands out the stream's passphrase. That is
what a third-party player needs, but the button does not say it is a
secret, and the deep link puts it in QCView's recent-items store.

### 1.2 The tray (agent, `mod tray`)

`tray-icon` + `muda` + `tao`. No window, no dialog, no text input of any
kind. The menu: a status line, an info line (peer or listen address), the
fingerprint line, a **Network** radio (Off / Direct / Discoverable; disabled
on a host), Launch / Close Blender (replica), Open config folder, Quit.
Every change goes through the same `apply_live` → save → `config` event
path the addon uses. The tooltip carries name and mode, refreshed at 2 Hz.

**Finding 3.** The only way to set a token, a peer, a name, the phonebook
folder or a path mapping without Blender is to open the config folder and
edit TOML. Yesterday's paired session needed that four times.

### 1.3 The Blender panel (addon, `prefs.py`)

Preferences: in agent mode the Connection box is read-only lines from the
agent plus an address override and a "token (fallback)" field; Pixel
Path (replica only); Save/Load Settings; cache root; the path-mapping
table with add/remove. The N-panel: role, status, Start/Stop, Pause,
Force Resync, Open in QCView, Copy Stream URL, Shot Mode, Replica Zoom,
and in agent mode a Discover row (address or blank to sweep) with one
"Use this replica" button per result, which does `set_config {peer,
fingerprint}` and mirrors the address into the addon's own preference.

**Finding 4.** Settings that the *replica* reads from its own preferences
(path mappings, cache root, the phonebook dir in the agent) have to be
entered on both machines, and nothing says so until the host panel
counts unmapped paths. The mapping-row surprise on 2026-09-23 was this.

### 1.4 The flow "pick a node to stream from"

Discover (probe by address, or sweep + phonebook) → peers event with
name/role/ip/port/fingerprint/version/paired → pick → the agent dials the
peer, pins the certificate on first connect, saves both → the replica's
hello reply carries the stream port and latency → `viewer_url()` builds
`srt://<peer host>:<port>?mode=caller&latency=…&passphrase=…` → Open in
QCView (deep link) or Copy. Works, proven both ways across the VPN.

**Finding 5.** The pick lives only in the Blender panel, and only while a
session is running. The agent already knows everything the panel shows.

### 1.5 Installation

The extension zip carries the Python and the pyzmq wheels, not the agent.
The agent is a cargo build found by the addon only when `QCB_AGENT=spawn`
(tests); the product path is "an agent is already running and registered".
Windows has a logon-task script and a firewall script; macOS has nothing.
The agent installs, updates and starts separately from the extension, and
the addon's version warning exists because of that.

**Finding 6.** There is no installer for the agent on either platform, no
autostart on macOS, and the Windows logon task had the console-exit bug
(tracker; fixed 2026-09-24 — windowless agent, `agent.log`, job object,
Run-key autostart). A user today cannot install QCBridge without a
terminal.

## 2. What the sister products do

Both were read for secrets, discovery, shared paths and UI; both are Qt
apps with a Rust or C++ headless agent, and both share the phonebook +
multicast heartbeat idiom that QCBridge already copies.

**Secrets — two opposite answers.**
- *MinRender*: one generated 256-bit secret in `farm.json` on the share,
  cleartext, never typed by anyone; every node on the same sync root has
  it; the UI shows only an 8-character SHA-256 fingerprint and counts
  peers whose fingerprint differs. "Farm security is share-permission
  security", stated in `SECURITY.md`.
- *UFB*: stores nothing. The only secret is a share login, and it calls
  the OS mount API with null credentials so the OS store answers; a typed
  auth failure shows a "Sign in…" pill that re-runs the same call with the
  OS dialog allowed. A visible credential field was removed because users
  had no idea what to type in it.

**Discovery — the same idiom, with one rule QCBridge already honours:**
multicast may refresh liveness for a known peer but never create one or
move its endpoint (MinRender lost a secret once to a spoofed packet). The
phonebook directory name is the identity. Both bump multicast group and
ports together per wire epoch so two versions coexist on one machine.
UFB derives the advertised IP from the route to the NAS that holds the
phonebook, which is the right IP on a multi-homed or VPN machine.

**Shared paths.** One user-entered root, everything derived. A mapping
row is `{enabled, label, win, mac}` edited in a modal over a local copy,
committed on Save, and a row counts only when *both* sides are non-empty.
UFB goes further with an OS-agnostic path identity derived from the pair.

**UI.** Neither ships a tray-only app: the tray belongs to a full Qt GUI
with settings dialogs and Qt's native folder picker. Neither installer
collects configuration; both install files, an opt-in autostart, firewall
rules (UFB's scoped to the program) and URI schemes, and leave everything
else to first run in the app. Neither uses a keychain crate, DPAPI, a
local web UI or an installer prompt.

## 3. What belongs where

The split that falls out of the runs and the audit:

| concern | lives in | why |
|---|---|---|
| machine identity: name, role, network mode, phonebook dir, Blender path, autostart | **agent** (tray) | machine-level, set once, needed with no Blender open |
| the session token / pairing | **agent** | it authenticates the QUIC session; the addon only forwards it today |
| path mappings, cache root | **agent**, served to the addon | machine-level and needed on *both* machines; the agent is the thing that is on both machines |
| which replica to stream from, its pin, "forget" | **agent** | discover, pick and pin already are agent operations; the panel is a remote for them |
| start/stop/pause a session, Force Resync, Shot Mode, zoom, Open in QCView | **addon** (N-panel) | scene-level, only meaningful with a file open |
| stream settings (rung, port, latency, kiosk) | **agent** for the replica machine; the addon shows them | the replica's Blender is agent-launched and has no user at it |

Today the addon owns the last three rows' data and the agent owns the
first four's; the mirror copies exist because of it. Moving mappings and
cache root into the agent's config (and the `config` event the addon
already consumes) removes the both-ends surprise: one row entered on the
host's tray, and the replica's agent receives it at pairing over the
authenticated session, the way the hello already carries stream settings.

## 4. The UI the tray needs

Options considered for text entry from a Rust tray app:

1. **A small native window** (`egui`/`eframe`, or `iced`) opened from a
   "Settings…" tray item. Cross-platform, one binary, plain text fields,
   masked token, folder picker via `rfd` (which does folder and message
   dialogs but no text prompt), a table for mappings, a list of peers
   with a Pair button. Rust-only build. This is the shape both sister
   products have, minus Qt.
2. **A local web page** served on the loopback port the agent already
   owns, opened in the default browser with a one-time URL. Zero UI
   dependencies, but a browser tab for settings reads as a dev tool, and
   the page holds the token in a tab.
3. **Blender as the only UI**, extending the panel to write every agent
   field through `set_config`. No new window, but the replica machine's
   settings still need Blender open there, which is the case we are
   trying to remove.
4. **Toasts** (`notify-rust`) for events only: paired, replica lost,
   unmapped paths. Not for input.

Recommendation: 1 for input and node choice, 4 for events, and the panel
keeps session actions. The window is opened from the tray and from a
button in the Blender preferences ("Open agent settings"), so a user who
lives in Blender never has to find the tray.

## 5. Secrets: what to do

Short term (this release), keep the token but stop scattering it —
**done 2026-09-24** (agent commit "the token stays in the agent"; the
keyring crate was not needed: Security.framework through the
core-foundation crate already in the lock, Credential Manager through
windows-sys, and a `token_store = "file"` for tests and Linux):
- store it in the OS keychain (`keyring` crate: Keychain, Credential
  Manager) and keep `agent.toml` for everything else;
- drop `token` from the settings mirror and from the addon preferences
  entirely; the agent owns it and reports only a fingerprint (MinRender's
  8-hex idea) so two ends can see they disagree;
- private key stays a file but with owner-only mode; `agent.json` secret
  likewise;
- "Copy Stream URL" says it copies a key, and the deep link is fine as is
  since QCView is the same trust domain.

Longer term (0.3), remove the shared secret: pairing by a short code shown
on the replica's tray and typed once on the host, after which the pinned
certificates on both sides are the trust, and the SRT passphrase is
issued per session over the authenticated control lane. That is the UFB
lesson in QCBridge's terms: the user never invents or copies a secret.

## 6. Installation

- The extension zip carries the agent (and the native capture helper)
  under `qcbridge/bin/<platform>/`; the manifest's platform list already
  splits the builds. First enable copies the agent to the app-support
  dir, writes a default `agent.toml`, and registers autostart: a
  launchd agent on macOS, the Run-key value on Windows
  (`agent/windows/autostart.ps1`, which replaced the logon task on
  2026-09-24), both per-user, no elevation.
- The firewall rule on Windows was not needed for the probe or the QUIC
  attach in the paired runs (the process is allowed when it binds); keep
  the script for the cases where policy blocks it, and say so in the doc.
- Signing is the owner's decision and gates whether an agent inside a
  zip runs on a locked-down box at all.
- The pyzmq wheels stay one more release for the no-agent fallback.

## 7. Order

1. Agent hygiene on Windows (console, log file, job object) — blocks any
   install. *Done 2026-09-24 (agent commit "Windows hygiene").*
2. Token to the keychain; token out of the addon and the mirror; the
   fingerprint line on both ends. *Done 2026-09-24.*
3. Mappings and cache root move to the agent config and the `config`
   event; the addon reads them from there; the replica receives the
   host's rows at pairing. *Done 2026-09-24.*
4. The settings window (egui) with: identity, network, phonebook, token,
   Blender path, mappings, cache root, and the peers list with Pair /
   Forget.
5. Bundle + autostart + first-enable install.
6. Docs: both-ends settings, what the phonebook exposes, one viewer per
   stream, install steps.

## 8. Decisions (2026-09-24, with the owner)

- **One agent per machine, with a Send scene / Receive scene switch**, live
  without a restart. Send is the workstation whose Blender is the source
  of truth: it dials, shows the nodes list, never announces. Receive is the
  render box: it listens, announces when findable, launches Blender for
  whoever pairs, writes the phonebook entry. Machines rarely change sides.
- **The settings window is the tray's UI** (Rust, native window): This
  machine, Pairing, Storage, Stream + diagnostics. The nodes list appears
  both in the tray menu and in the window. Blender's preferences become
  one button, "Open agent settings", with the zmq fallback under Advanced.
- **Every path field gets a folder picker**: phonebook, cache root, and
  each mapping row. The picker fills this machine's column; when the
  folder is a network mount the agent proposes the other column (UNC or
  `/Volumes` form) from the mount table for the user to accept or edit.
- **Token in the window for this release; pairing code next.**
- **Order stands:** Windows agent hygiene first, then token to the keychain
  and out of the addon, then mappings and cache root into the agent, then
  the window, then bundle and autostart, then docs.

