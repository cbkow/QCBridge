//! Agent configuration: one TOML file in the user's config dir, plus a small
//! JSON the addon reads to find the agent's local socket.

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    /// "replica" (listens, runs Blender for a host) or "host".
    pub role: String,
    /// Session token, checked at the QUIC layer and again by the addon's hello.
    pub token: String,
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
    /// Host: local TCP port serving the stream to the viewer (0 = off).
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
}

impl Default for Config {
    fn default() -> Self {
        Self {
            role: "replica".into(),
            token: String::new(),
            listen: "0.0.0.0:19990".into(),
            peer: "127.0.0.1:19990".into(),
            fingerprint: String::new(),
            cap_mbps: 400.0,
            capture_scale: 1.0,
            video_port: 19997,
            local_port: 0,
            blender_path: default_blender_path(),
            blender_args: Vec::new(),
            kiosk: true,
            idle_secs: 300,
            tray: true,
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
        return toml::from_str(&text).with_context(|| format!("parse {}", path.display()));
    }
    let cfg = Config::default();
    save(path, &cfg)?;
    Ok(cfg)
}

pub fn save(path: &PathBuf, cfg: &Config) -> Result<()> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    std::fs::write(path, toml::to_string_pretty(cfg)?)?;
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
