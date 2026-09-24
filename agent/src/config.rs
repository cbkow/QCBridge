//! Agent configuration: one TOML file in the user's config dir, plus a small
//! JSON the addon reads to find the agent's local socket.

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

/// One prefix-pair row of the path-mapping table (the addon's decision
/// #15): a Windows root and the macOS root of the same storage. Owned by
/// the agent since 2026-09-24; the host's rows ride to the replica in the
/// hello, so one machine's table serves both ends.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct PathMapping {
    pub win: String,
    pub mac: String,
    pub enabled: bool,
    pub label: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    /// "replica" (listens, runs Blender for a host) or "host".
    pub role: String,
    /// Session token, checked at the QUIC layer and again by the addon's
    /// hello. In memory only: `save` never writes it, `load` takes it from
    /// the store named by `token_store` (a value found in the TOML is
    /// migrated there once).
    pub token: String,
    /// "keychain" (default: macOS Keychain / Windows Credential Manager),
    /// "file" (owner-only file beside the config), or "toml" (clear text in
    /// this file, the pre-2026-09-24 behaviour).
    pub token_store: String,
    /// Replica: UDP listen address. Host: the replica's address.
    pub listen: String,
    pub peer: String,
    /// Host: pinned replica certificate SHA-256 (hex). Empty = learn on first use.
    pub fingerprint: String,
    /// Wire-rate cap for the video lane (replica), Mbps.
    pub cap_mbps: f64,
    /// Replica, native capture: stream resolution as a fraction of the
    /// captured pixels (1.0 = native; 0.5 halves each dimension).
    pub capture_scale: f64,
    /// Host: local TCP port re-serving the stream to the viewer (0 = off).
    /// Off by default since the quinn port: video no longer rides the
    /// connection, QCView opens the replica's srt:// directly, and nothing
    /// feeds this listener. Kept for compatibility with existing TOMLs.
    pub video_port: u16,
    /// Local socket for the addon (0 = pick a free port).
    pub local_port: u16,
    /// Replica: Blender binary and extra args; launched when a host connects.
    pub blender_path: String,
    pub blender_args: Vec<String>,
    pub kiosk: bool,
    /// Replica: close Blender this long after the host goes away (0 = never).
    pub idle_secs: u64,
    /// Show the tray icon (false = headless, for tests and services).
    pub tray: bool,
    /// Shown to peers and in the tray. Empty = the OS hostname.
    pub name: String,
    /// "off" | "direct" | "discoverable". Direct is the default: beaconing
    /// on a facility network is opt-in, and the VPN cannot use it anyway.
    pub discovery: String,
    /// Shared directory for the phonebook (MinRender's pattern). Empty = off.
    pub phonebook: String,
    /// Path-mapping rows, served to the addon and (host) sent to the
    /// replica at pairing.
    pub path_mappings: Vec<PathMapping>,
    /// Host: shared cache root for simulation caches (the addon's
    /// `CACHES.md`). Empty = off.
    pub cache_root: String,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            role: "replica".into(),
            token: String::new(),
            token_store: "keychain".into(),
            listen: "0.0.0.0:19990".into(),
            peer: "127.0.0.1:19990".into(),
            fingerprint: String::new(),
            cap_mbps: 400.0,
            capture_scale: 1.0,
            video_port: 0,
            local_port: 0,
            blender_path: default_blender_path(),
            blender_args: Vec::new(),
            kiosk: true,
            idle_secs: 300,
            tray: true,
            name: String::new(),
            discovery: "direct".into(),
            phonebook: String::new(),
            path_mappings: Vec::new(),
            cache_root: String::new(),
        }
    }
}

fn default_blender_path() -> String {
    if cfg!(target_os = "macos") {
        "/Applications/Blender.app/Contents/MacOS/Blender".into()
    } else if cfg!(windows) {
        r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe".into()
    } else {
        "blender".into()
    }
}

pub const DISCOVERY_MODES: &[&str] = &["off", "direct", "discoverable"];

/// What the machine is called, for the beacon and the tray. No crate: libc
/// is already in the tree via quinn, and Windows always sets COMPUTERNAME.
pub fn machine_name() -> String {
    #[cfg(unix)]
    {
        let mut buf = [0u8; 256];
        let rc = unsafe { libc::gethostname(buf.as_mut_ptr() as *mut libc::c_char, buf.len()) };
        if rc == 0 {
            if let Some(n) = buf.iter().position(|&b| b == 0) {
                if let Ok(name) = std::str::from_utf8(&buf[..n]) {
                    if !name.is_empty() {
                        return name.to_string();
                    }
                }
            }
        }
    }
    #[cfg(windows)]
    {
        if let Ok(name) = std::env::var("COMPUTERNAME") {
            if !name.is_empty() {
                return name;
            }
        }
    }
    "qcbridge".into()
}

impl Config {
    /// The configured name, or the machine's when none is set.
    pub fn display_name(&self) -> String {
        if self.name.trim().is_empty() { machine_name() } else { self.name.clone() }
    }
}

/// Fields that can change on a running agent, and are therefore accepted by
/// `set_config` and reported in the `config` event. Anything that needs a
/// rebuilt endpoint — role, listen, local_port, tray — is deliberately not
/// here: the command says "needs_restart" rather than pretending.
pub const LIVE_FIELDS: &[&str] = &[
    "peer", "token", "fingerprint", "name", "discovery", "phonebook",
    "blender_path", "blender_args", "kiosk", "idle_secs", "cap_mbps", "capture_scale",
    "path_mappings", "cache_root",
    // Live since 2026-09-24: a change rebuilds the role runtime in place
    // (the Send/Receive switch; a replica re-listening on a new address).
    "role", "listen",
];
pub const RESTART_FIELDS: &[&str] = &["local_port", "tray"];
pub const ROLES: &[&str] = &["host", "replica"];

/// What `apply_live` did with a patch. Reported back verbatim.
#[derive(Debug, Default, PartialEq)]
pub struct Applied {
    pub changed: Vec<String>,
    pub needs_restart: Vec<String>,
    pub rejected: Vec<String>,
}

/// Apply the live fields of a JSON patch (`{"peer": "...", "idle_secs": 60}`)
/// to a config, in place. Pure, so it can be unit-tested without an agent.
/// Type mismatches and invalid values are rejected, not coerced.
pub fn apply_live(cfg: &mut Config, patch: &serde_json::Value) -> Applied {
    let mut out = Applied::default();
    let Some(obj) = patch.as_object() else { return out };
    for (key, val) in obj {
        let k = key.as_str();
        if RESTART_FIELDS.contains(&k) {
            out.needs_restart.push(key.clone());
            continue;
        }
        if !LIVE_FIELDS.contains(&k) {
            out.rejected.push(key.clone());
            continue;
        }
        let ok = match k {
            "peer" => set_str(&mut cfg.peer, val),
            "token" => set_str(&mut cfg.token, val),
            "fingerprint" => set_str(&mut cfg.fingerprint, val),
            "name" => set_str(&mut cfg.name, val),
            "phonebook" => set_str(&mut cfg.phonebook, val),
            "blender_path" => set_str(&mut cfg.blender_path, val),
            "discovery" => match val.as_str() {
                Some(m) if DISCOVERY_MODES.contains(&m) => { cfg.discovery = m.into(); true }
                _ => false,
            },
            "blender_args" => match val.as_array() {
                Some(a) if a.iter().all(|v| v.is_string()) => {
                    cfg.blender_args = a.iter().map(|v| v.as_str().unwrap().to_string()).collect();
                    true
                }
                _ => false,
            },
            "kiosk" => match val.as_bool() { Some(b) => { cfg.kiosk = b; true } None => false },
            "idle_secs" => match val.as_u64() { Some(n) => { cfg.idle_secs = n; true } None => false },
            "cap_mbps" => match val.as_f64() { Some(f) if f > 0.0 => { cfg.cap_mbps = f; true } _ => false },
            "capture_scale" => match val.as_f64() { Some(f) if f > 0.0 && f <= 1.0 => { cfg.capture_scale = f; true } _ => false },
            "cache_root" => set_str(&mut cfg.cache_root, val),
            "role" => match val.as_str() {
                Some(r) if ROLES.contains(&r) => { cfg.role = r.into(); true }
                _ => false,
            },
            "listen" => match val.as_str() {
                Some(l) if l.parse::<std::net::SocketAddr>().is_ok() => { cfg.listen = l.into(); true }
                _ => false,
            },
            "path_mappings" => match parse_mappings(val) {
                Some(rows) => { cfg.path_mappings = rows; true }
                None => false,
            },
            _ => false,
        };
        if ok { out.changed.push(key.clone()) } else { out.rejected.push(key.clone()) }
    }
    out
}

/// A mapping table from JSON: an array of objects with string `win` and
/// `mac`; `enabled` defaults to true, `label` to "". Anything else is
/// rejected whole rather than half-applied.
fn parse_mappings(val: &serde_json::Value) -> Option<Vec<PathMapping>> {
    let rows = val.as_array()?;
    let mut out = Vec::with_capacity(rows.len());
    for r in rows {
        let o = r.as_object()?;
        let win = o.get("win")?.as_str()?.to_string();
        let mac = o.get("mac")?.as_str()?.to_string();
        let enabled = match o.get("enabled") { None => true, Some(v) => v.as_bool()? };
        let label = match o.get("label") { None => String::new(), Some(v) => v.as_str()?.to_string() };
        out.push(PathMapping { win, mac, enabled, label });
    }
    Some(out)
}

fn set_str(slot: &mut String, val: &serde_json::Value) -> bool {
    match val.as_str() {
        Some(v) => { *slot = v.to_string(); true }
        None => false,
    }
}

/// The live fields as JSON — the body of the `config` event and part of the
/// attach reply. The token itself is not in it: the addon gets what it
/// consumes — the SRT passphrase and the hello secret — and a fingerprint
/// to compare with the other end, so the raw token never leaves the agent.
pub fn live_view(cfg: &Config) -> serde_json::Value {
    serde_json::json!({
        "peer": cfg.peer,
        "token_set": !cfg.token.is_empty(),
        "token_fingerprint": crate::secrets::fingerprint(&cfg.token),
        "srt_passphrase": crate::secrets::srt_passphrase(&cfg.token),
        "hello_secret": crate::secrets::hello_secret(&cfg.token),
        "token_store": cfg.token_store,
        "fingerprint": cfg.fingerprint,
        "name": cfg.display_name(),
        "name_is_default": cfg.name.trim().is_empty(),
        "discovery": cfg.discovery,
        "phonebook": cfg.phonebook,
        "blender_path": cfg.blender_path,
        "blender_args": cfg.blender_args,
        "kiosk": cfg.kiosk,
        "idle_secs": cfg.idle_secs,
        "cap_mbps": cfg.cap_mbps,
        "capture_scale": cfg.capture_scale,
        "path_mappings": cfg.path_mappings,
        "cache_root": cfg.cache_root,
        // Restart-only, reported so the addon can show them read-only.
        "role": cfg.role,
        "listen": cfg.listen,
    })
}

/// Some(true)/Some(false) when the OS can say; None when it cannot. Never
/// claims "alive" on a guess: an unknown answer must not stop an agent from
/// starting. Unix: kill(pid, 0), EPERM counting as alive. Windows:
/// OpenProcess(SYNCHRONIZE) and a zero-timeout wait — a process we may not
/// open (another user, elevated) counts as alive the way EPERM does; any
/// other failure means no such process. Same answer as the QCBridgeAE ring's
/// liveness check, done the same day (2026-09-23).
pub fn pid_alive(pid: u32) -> Option<bool> {
    if pid == std::process::id() {
        return Some(true);
    }
    #[cfg(unix)]
    {
        let rc = unsafe { libc::kill(pid as libc::pid_t, 0) };
        if rc == 0 {
            return Some(true);
        }
        return Some(std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM));
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::Foundation::{CloseHandle, ERROR_ACCESS_DENIED, WAIT_TIMEOUT};
        use windows_sys::Win32::System::Threading::{OpenProcess, WaitForSingleObject};
        // The standard access right; windows-sys files it under
        // Storage::FileSystem, a whole feature for one constant.
        const SYNCHRONIZE: u32 = 0x0010_0000;
        if pid == 0 {
            return Some(false);
        }
        unsafe {
            let h = OpenProcess(SYNCHRONIZE, 0, pid);
            if h.is_null() {
                let err = std::io::Error::last_os_error().raw_os_error().unwrap_or(0) as u32;
                return Some(err == ERROR_ACCESS_DENIED);
            }
            let w = WaitForSingleObject(h, 0);
            CloseHandle(h);
            Some(w == WAIT_TIMEOUT)
        }
    }
    #[cfg(not(any(unix, windows)))]
    {
        let _ = pid;
        None
    }
}

/// Another agent of this role, still running, registered in this directory.
/// agent.json has always recorded a pid; nothing read it, so two agents of
/// one role silently overwrote each other. With discovery, two of them
/// would also both beacon.
pub fn registered_live_pid(base: &Path, role: &str) -> Option<u32> {
    let text = std::fs::read_to_string(socket_info_path(base)).ok()?;
    let doc: serde_json::Value = serde_json::from_str(&text).ok()?;
    let pid = doc.get(role)?.get("pid")?.as_u64()? as u32;
    if pid == std::process::id() {
        return None;
    }
    (pid_alive(pid) == Some(true)).then_some(pid)
}

pub fn config_dir() -> PathBuf {
    dirs::config_dir().unwrap_or_else(std::env::temp_dir).join("QCBridge")
}

pub fn config_path() -> PathBuf {
    config_dir().join("agent.toml")
}

/// Everything an agent instance owns sits beside its config file: the TOML,
/// the certificate, and the agent.json the addon reads. Deriving them from
/// the config path is what makes `--config` isolate an instance — before,
/// only the TOML moved, so two agents pointed at different configs still
/// shared one certificate and one agent.json. For the default config path
/// this resolves to exactly where those files already live.
pub fn base_dir(config_path: &Path) -> PathBuf {
    config_path.parent().map(Path::to_path_buf).unwrap_or_else(config_dir)
}

/// Where the addon looks for {port, secret}: written by the agent at start.
pub fn socket_info_path(base: &Path) -> PathBuf {
    base.join("agent.json")
}

pub fn cert_dir(base: &Path) -> PathBuf {
    base.join("cert")
}

pub fn load_or_create(path: &PathBuf) -> Result<Config> {
    if path.exists() {
        let text = std::fs::read_to_string(path).with_context(|| format!("read {}", path.display()))?;
        let cfg: Config = toml::from_str(&text).with_context(|| format!("parse {}", path.display()))?;
        if !crate::secrets::STORES.contains(&cfg.token_store.as_str()) {
            anyhow::bail!("token_store must be one of {:?} in {}", crate::secrets::STORES, path.display());
        }
        return Ok(cfg);
    }
    let cfg = Config::default();
    save(path, &cfg)?;
    Ok(cfg)
}

/// Fill `cfg.token` from its store, after `load_or_create`. A token still
/// written in the TOML (the old layout) is moved into the store and the
/// file rewritten without it; if the store refuses, the TOML keeps it and
/// the returned note says so. Returns a line for the log, if anything
/// happened.
pub fn load_token(path: &PathBuf, cfg: &mut Config) -> Result<Option<String>> {
    let base = base_dir(path);
    if cfg.token_store == "toml" {
        return Ok(None);
    }
    let in_toml = std::mem::take(&mut cfg.token);
    match crate::secrets::load(&cfg.token_store, &base, &cfg.role)? {
        Some(stored) => {
            cfg.token = stored;
            if !in_toml.is_empty() {
                // The store wins; the clear-text copy goes.
                save(path, cfg)?;
                return Ok(Some(format!("token: using the {} entry; removed the copy from {}", cfg.token_store, path.display())));
            }
            Ok(None)
        }
        None if !in_toml.is_empty() => {
            cfg.token = in_toml;
            match crate::secrets::store(&cfg.token_store, &base, &cfg.role, &cfg.token) {
                Ok(()) => {
                    save(path, cfg)?;
                    Ok(Some(format!("token: moved from {} into the {}", path.display(), cfg.token_store)))
                }
                Err(e) => {
                    // Keep it where it was rather than lose it; say so.
                    cfg.token_store = "toml".into();
                    Ok(Some(format!("token: could not move it into the store ({e:#}); it stays in {}", path.display())))
                }
            }
        }
        None => Ok(None),
    }
}

/// Write the TOML. The token is left out unless `token_store` is "toml":
/// its home is the store, and a config file that is copied, pasted into a
/// chat or backed up must not carry it.
pub fn save(path: &PathBuf, cfg: &Config) -> Result<()> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let mut on_disk = cfg.clone();
    if on_disk.token_store != "toml" {
        on_disk.token.clear();
    }
    std::fs::write(path, toml::to_string_pretty(&on_disk)?)?;
    Ok(())
}

/// agent.json holds one entry per role, so a host and a replica agent can
/// share a machine (dev, or a workstation that is both).
pub fn write_socket_info(base: &Path, role: &str, port: u16, secret: &str) -> Result<()> {
    let path = socket_info_path(base);
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let mine = serde_json::json!({"port": port, "secret": secret, "pid": std::process::id()});
    // Two agents starting together race on this file; write, re-read, and
    // retry until our own entry is what's on disk. Only role keys survive.
    for attempt in 0..5 {
        let mut doc = serde_json::json!({});
        if let Ok(text) = std::fs::read_to_string(&path) {
            if let Ok(serde_json::Value::Object(map)) = serde_json::from_str::<serde_json::Value>(&text) {
                for (k, v) in map {
                    if (k == "host" || k == "replica") && v.is_object() {
                        doc[k] = v;
                    }
                }
            }
        }
        doc[role] = mine.clone();
        std::fs::write(&path, doc.to_string())?;
        crate::secrets::owner_only(&path);
        std::thread::sleep(std::time::Duration::from_millis(50 + 30 * attempt));
        let back: Option<serde_json::Value> = std::fs::read_to_string(&path).ok().and_then(|t| serde_json::from_str(&t).ok());
        if back.as_ref().and_then(|d| d.get(role)) == Some(&mine) {
            return Ok(());
        }
    }
    anyhow::bail!("could not register in {}", path.display())
}

pub fn remove_socket_info(base: &Path, role: &str) {
    let path = socket_info_path(base);
    if let Ok(text) = std::fs::read_to_string(&path) {
        if let Ok(mut doc) = serde_json::from_str::<serde_json::Value>(&text) {
            if let Some(obj) = doc.as_object_mut() {
                obj.remove(role);
                if obj.is_empty() {
                    let _ = std::fs::remove_file(&path);
                } else {
                    let _ = std::fs::write(&path, doc.to_string());
                }
            }
        }
    }
}


/// The one config, shared by everything that holds one.
///
/// It used to be cloned three ways — the agent, the Blender lifecycle and
/// the host observer each owned a copy — and only the observer's was updated
/// when a certificate was pinned. So `Agent.cfg.fingerprint` was stale for
/// the life of the process, and the lifecycle could never see an edited
/// `blender_path` or `idle_secs`. Any runtime settings work would have
/// written to one copy and been invisible to the rest.
///
/// Read with `with`, change with `update`, and keep both closures short:
/// the lock is held for their duration and every holder shares it.
#[derive(Clone)]
pub struct SharedConfig(Arc<Mutex<Config>>);

impl SharedConfig {
    pub fn new(cfg: Config) -> Self {
        Self(Arc::new(Mutex::new(cfg)))
    }

    pub fn with<T>(&self, f: impl FnOnce(&Config) -> T) -> T {
        f(&self.0.lock().unwrap())
    }

    pub fn update<T>(&self, f: impl FnOnce(&mut Config) -> T) -> T {
        f(&mut self.0.lock().unwrap())
    }

    /// A whole copy, for the few places that want one (saving to disk).
    pub fn snapshot(&self) -> Config {
        self.0.lock().unwrap().clone()
    }

    // The two most-read facts, which never change after startup but are
    // read often enough that `with(|c| ...)` at every site buries them.
    pub fn role(&self) -> String {
        self.with(|c| c.role.clone())
    }

    pub fn is_host(&self) -> bool {
        self.with(|c| c.role == "host")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The bug this type exists to prevent: the config used to be cloned per
    /// holder, so a write through one was invisible to the rest. A pinned
    /// fingerprint reached the host observer and nothing else.
    #[test]
    fn a_write_through_one_handle_is_visible_through_another() {
        let a = SharedConfig::new(Config::default());
        let b = a.clone();
        assert!(b.with(|c| c.fingerprint.is_empty()));

        a.update(|c| c.fingerprint = "abc123".into());

        assert_eq!(b.with(|c| c.fingerprint.clone()), "abc123");
        assert_eq!(a.snapshot().fingerprint, "abc123");
    }

    #[test]
    fn a_toml_without_the_new_fields_gets_their_defaults() {
        let cfg: Config = toml::from_str("role = \"host\"\ntoken = \"t\"\n").unwrap();
        assert_eq!(cfg.discovery, "direct");
        assert!(cfg.name.is_empty() && cfg.phonebook.is_empty());
        assert!(!cfg.display_name().is_empty(), "falls back to the machine name");
    }

    #[test]
    fn save_leaves_the_token_out_unless_the_store_is_toml() {
        let dir = std::env::temp_dir().join(format!("qcb-cfg-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let path = dir.join("agent.toml");
        let mut cfg = Config { token: "s3cret".into(), ..Config::default() };
        save(&path, &cfg).unwrap();
        let text = std::fs::read_to_string(&path).unwrap();
        assert!(!text.contains("s3cret"), "{text}");
        assert!(text.contains("token_store = \"keychain\""));
        cfg.token_store = "toml".into();
        save(&path, &cfg).unwrap();
        assert!(std::fs::read_to_string(&path).unwrap().contains("s3cret"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The old layout: a token in the TOML moves into the store (the file
    /// store here, so the test touches no keychain) and leaves the TOML.
    #[test]
    fn a_toml_token_is_migrated_into_the_store() {
        let dir = std::env::temp_dir().join(format!("qcb-mig-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("agent.toml");
        std::fs::write(&path, "role = \"replica\"\ntoken = \"legacy\"\ntoken_store = \"file\"\n").unwrap();
        let mut cfg = load_or_create(&path).unwrap();
        assert_eq!(cfg.token, "legacy");
        let note = load_token(&path, &mut cfg).unwrap();
        assert!(note.unwrap().contains("moved"));
        assert_eq!(cfg.token, "legacy");
        assert!(!std::fs::read_to_string(&path).unwrap().contains("legacy"));
        assert_eq!(crate::secrets::load("file", &dir, "replica").unwrap().as_deref(), Some("legacy"));
        // Second start: the store is the source.
        let mut again = load_or_create(&path).unwrap();
        assert!(again.token.is_empty());
        assert!(load_token(&path, &mut again).unwrap().is_none());
        assert_eq!(again.token, "legacy");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_bad_token_store_is_refused_at_load() {
        let dir = std::env::temp_dir().join(format!("qcb-store-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("agent.toml");
        std::fs::write(&path, "token_store = \"vault\"\n").unwrap();
        assert!(load_or_create(&path).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_live_view_carries_derived_values_not_the_token() {
        let cfg = Config { token: "tok".into(), ..Config::default() };
        let v = live_view(&cfg);
        assert!(v.get("token").is_none());
        assert_eq!(v["token_set"], true);
        assert_eq!(v["token_fingerprint"], "e796aeb8");
        assert_eq!(v["srt_passphrase"], "394281a840f6f63396e7ee2ea3acc796");
        assert_eq!(v["hello_secret"].as_str().unwrap().len(), 64);
        let empty = live_view(&Config::default());
        assert_eq!(empty["token_set"], false);
        assert_eq!(empty["srt_passphrase"], "");
    }

    #[test]
    fn mappings_and_cache_root_are_live_and_round_trip_through_toml() {
        let mut cfg = Config::default();
        let a = apply_live(&mut cfg, &serde_json::json!({
            "path_mappings": [
                {"win": "M:\\Jobs", "mac": "/Volumes/Jobs", "label": "jobs"},
                {"win": "\\\\srv\\Assets", "mac": "/Volumes/Assets", "enabled": false},
            ],
            "cache_root": "/Volumes/Jobs/cache",
        }));
        assert_eq!(a.changed.len(), 2, "{a:?}");
        assert_eq!(cfg.path_mappings.len(), 2);
        assert!(cfg.path_mappings[0].enabled && cfg.path_mappings[0].label == "jobs");
        assert!(!cfg.path_mappings[1].enabled && cfg.path_mappings[1].label.is_empty());
        let text = toml::to_string_pretty(&cfg).unwrap();
        let back: Config = toml::from_str(&text).unwrap();
        assert_eq!(back.path_mappings, cfg.path_mappings);
        assert_eq!(back.cache_root, "/Volumes/Jobs/cache");
        let v = live_view(&cfg);
        assert_eq!(v["path_mappings"].as_array().unwrap().len(), 2);
        assert_eq!(v["path_mappings"][0]["win"], "M:\\Jobs");
        // A malformed table is rejected whole, the old one kept.
        let a = apply_live(&mut cfg, &serde_json::json!({"path_mappings": [{"win": 1}]}));
        assert_eq!(a.rejected, vec!["path_mappings"]);
        assert_eq!(cfg.path_mappings.len(), 2);
        // Emptying is allowed.
        let a = apply_live(&mut cfg, &serde_json::json!({"path_mappings": []}));
        assert_eq!(a.changed, vec!["path_mappings"]);
        assert!(cfg.path_mappings.is_empty());
    }

    #[test]
    fn machine_name_is_never_empty() {
        assert!(!machine_name().is_empty());
    }

    #[test]
    fn apply_live_changes_live_fields_and_reports_the_rest() {
        let mut cfg = Config::default();
        let a = apply_live(&mut cfg, &serde_json::json!({
            "peer": "10.0.0.5:19990", "idle_secs": 60, "discovery": "discoverable",
            "local_port": 1, "tray": false,
            "nonsense": 1,
        }));
        assert_eq!(cfg.peer, "10.0.0.5:19990");
        assert_eq!(cfg.idle_secs, 60);
        assert_eq!(cfg.discovery, "discoverable");
        // Order is not a contract: a JSON object iterates by sorted key.
        let sorted = |v: &Vec<String>| { let mut v = v.clone(); v.sort(); v };
        assert_eq!(sorted(&a.changed), vec!["discovery", "idle_secs", "peer"]);
        assert_eq!(sorted(&a.needs_restart), vec!["local_port", "tray"]);
        assert_eq!(a.rejected, vec!["nonsense"]);
        // Restart-only fields were not touched.
        assert_eq!(cfg.local_port, Config::default().local_port);
        assert!(cfg.tray);
        // Role and listen are live, and validated.
        let a = apply_live(&mut cfg, &serde_json::json!({"role": "host", "listen": "0.0.0.0:1"}));
        assert_eq!(sorted(&a.changed), vec!["listen", "role"]);
        assert_eq!(cfg.role, "host");
        let a = apply_live(&mut cfg, &serde_json::json!({"role": "sender", "listen": "nowhere"}));
        assert_eq!(sorted(&a.rejected), vec!["listen", "role"]);
        assert_eq!(cfg.role, "host");
    }

    #[test]
    fn apply_live_rejects_bad_values_instead_of_coercing() {
        let mut cfg = Config::default();
        let a = apply_live(&mut cfg, &serde_json::json!({
            "discovery": "loud", "idle_secs": "sixty", "capture_scale": 2.0, "kiosk": 1,
        }));
        assert!(a.changed.is_empty());
        assert_eq!(a.rejected.len(), 4);
        assert_eq!(cfg.discovery, "direct");
    }

    #[test]
    fn our_own_pid_is_alive_and_a_nonsense_pid_is_not() {
        assert_eq!(pid_alive(std::process::id()), Some(true));
        if cfg!(any(unix, windows)) {
            assert_eq!(pid_alive(u32::MAX - 7), Some(false));
        }
    }

    /// A snapshot is a copy on purpose — it is what gets written to disk —
    /// so changing it must not change what the others see.
    #[test]
    fn a_snapshot_is_detached() {
        let a = SharedConfig::new(Config::default());
        let mut snap = a.snapshot();
        snap.token = "edited".into();
        assert!(a.with(|c| c.token.is_empty()));
    }
}
