//! Agent configuration: one TOML file in the user's config dir, plus a small
//! JSON the addon reads to find the agent's local socket.

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::path::PathBuf;

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

/// Where the addon looks for {port, secret}: written by the agent at start.
pub fn socket_info_path() -> PathBuf {
    config_dir().join("agent.json")
}

pub fn cert_dir() -> PathBuf {
    config_dir().join("cert")
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

pub fn write_socket_info(port: u16, secret: &str) -> Result<()> {
    let path = socket_info_path();
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    std::fs::write(&path, serde_json::json!({"port": port, "secret": secret, "pid": std::process::id()}).to_string())?;
    Ok(())
}
