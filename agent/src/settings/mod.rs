//! The settings window (`qcbridge-agent --settings`): the tray's UI, as
//! decided 2026-09-24 (CONTROL-AUDIT.md §8). One native window, its own
//! process, attached to the running agent as a control client. Every
//! value shown is the agent's: a field commits through `set_config` and
//! the `config` event that answers is what the field then shows.
//!
//! Sections: this machine (name, Send/Receive, Blender), pairing (network,
//! phonebook, token, the peer and the replicas found), storage (cache
//! root, the mapping table with a picker per cell that proposes the other
//! platform's path from the mount table), stream, diagnostics.

mod client;
mod theme;

use crate::config::{self, PathMapping};
use crate::mounts;
use anyhow::Result;
use client::Client;
use eframe::egui::{self, RichText};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

/// Open the window for the agent whose config is `cfg_path`.
pub fn run(cfg_path: PathBuf) -> Result<()> {
    let shot = std::env::args().skip_while(|a| a != "--shot").nth(1).map(PathBuf::from);
    let (rgba, w, h) = crate::icons::rgba(crate::icons::APP_256);
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title("QCBridge Agent")
            .with_icon(std::sync::Arc::new(egui::IconData { rgba, width: w, height: h }))
            .with_inner_size([900.0, 800.0])
            .with_min_inner_size([600.0, 480.0]),
        centered: true,
        // macOS: the window belongs to a menu-bar app. winit sets the
        // process to a Regular app by default (a Dock tile and an app menu
        // for as long as the window is open, overriding the bundle's
        // LSUIElement); Accessory keeps it out of the Dock while the
        // window still comes to the front (2026-09-25).
        #[cfg(target_os = "macos")]
        event_loop_builder: Some(Box::new(|b| {
            use winit::platform::macos::{ActivationPolicy, EventLoopBuilderExtMacOS};
            b.with_activation_policy(ActivationPolicy::Accessory)
                .with_activate_ignoring_other_apps(true)
                .with_default_menu(false);
        })),
        ..Default::default()
    };
    eframe::run_native(
        "QCBridge Agent",
        options,
        Box::new(move |cc| {
            cc.egui_ctx.set_pixels_per_point(cc.egui_ctx.pixels_per_point().max(1.0));
            theme::apply(&cc.egui_ctx);
            Ok(Box::new(App::new(cfg_path, shot)))
        }),
    )
    .map_err(|e| anyhow::anyhow!("window: {e}"))
}

#[derive(Default, Clone)]
struct Row {
    enabled: bool,
    label: String,
    win: String,
    mac: String,
    /// The other column proposed by the picker, shown until accepted.
    hint: Option<(bool, String)>, // (for the win column?, proposed text)
}

impl Row {
    fn from_cfg(m: &PathMapping) -> Row {
        Row { enabled: m.enabled, label: m.label.clone(), win: m.win.clone(), mac: m.mac.clone(), hint: None }
    }
    fn to_json(&self) -> Value {
        json!({"win": self.win, "mac": self.mac, "enabled": self.enabled, "label": self.label})
    }
}

#[derive(Default)]
struct Draft {
    name: String,
    role: String,
    blender_path: String,
    kiosk: bool,
    idle_secs: String,
    discovery: String,
    phonebook: String,
    peer: String,
    listen: String,
    cache_root: String,
    shared_root: String,
    cap_mbps: String,
    capture_scale: String,
    rows: Vec<Row>,
    rows_dirty: bool,
    token_input: String,
}

struct App {
    cfg_path: PathBuf,
    base: PathBuf,
    role_hint: String,
    client: Option<Client>,
    connect_err: String,
    last_try: Instant,
    /// The agent's live view, as last reported.
    remote: Value,
    status: String,
    addon_attached: bool,
    peer_up: bool,
    peer_fingerprint: String,
    own_fingerprint: String,
    version: String,
    draft: Draft,
    peers: Vec<Value>,
    peers_note: String,
    note: String,
    note_is_error: bool,
    mounts: Vec<mounts::Mount>,
    shot: Option<PathBuf>,
    opened_at: Instant,
    shot_taken: bool,
}

impl App {
    fn new(cfg_path: PathBuf, shot: Option<PathBuf>) -> App {
        let base = config::base_dir(&cfg_path);
        let role_hint = std::fs::read_to_string(&cfg_path)
            .ok()
            .and_then(|t| toml::from_str::<config::Config>(&t).ok())
            .map(|c| c.role)
            .unwrap_or_else(|| "replica".into());
        let mut app = App {
            cfg_path, base, role_hint,
            client: None, connect_err: String::new(), last_try: Instant::now() - Duration::from_secs(5),
            remote: Value::Null, status: String::new(), addon_attached: false, peer_up: false,
            peer_fingerprint: String::new(), own_fingerprint: String::new(), version: String::new(),
            draft: Draft::default(), peers: Vec::new(), peers_note: String::new(),
            note: String::new(), note_is_error: false,
            mounts: mounts::table(), shot, opened_at: Instant::now(), shot_taken: false,
        };
        app.try_connect();
        app
    }

    fn try_connect(&mut self) {
        self.last_try = Instant::now();
        // The registration is by role; the role may have flipped since the
        // TOML was read, so try both.
        let other = if self.role_hint == "host" { "replica" } else { "host" };
        let roles = [self.role_hint.clone(), other.to_string()];
        let mut last = String::new();
        for role in roles {
            match client::socket_info(&self.base, &role).and_then(|(port, secret)| Client::connect(port, &secret)) {
                Ok(c) => {
                    let att = c.attached.clone();
                    self.client = Some(c);
                    self.connect_err.clear();
                    self.take_attached(&att);
                    return;
                }
                Err(e) => last = format!("{e:#}"),
            }
        }
        self.connect_err = last;
    }

    fn take_attached(&mut self, att: &Value) {
        self.version = att.get("version").and_then(Value::as_str).unwrap_or("").to_string();
        self.own_fingerprint = att.get("fingerprint").and_then(Value::as_str).unwrap_or("").to_string();
        self.peer_fingerprint = att.get("peer_fingerprint").and_then(Value::as_str).unwrap_or("").to_string();
        self.status = att.get("status").and_then(Value::as_str).unwrap_or("").to_string();
        self.addon_attached = att.get("addon_attached").and_then(Value::as_bool).unwrap_or(false);
        self.peer_up = att.get("peer_up").and_then(Value::as_bool).unwrap_or(false);
        if let Some(cfg) = att.get("config") {
            self.take_config(cfg.clone(), true);
        }
    }

    /// A fresh live view from the agent: the draft follows it, except the
    /// mapping table while it has unsent edits.
    fn take_config(&mut self, cfg: Value, force_rows: bool) {
        let s = |k: &str| cfg.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        let d = &mut self.draft;
        d.name = s("name");
        d.role = s("role");
        d.blender_path = s("blender_path");
        d.kiosk = cfg.get("kiosk").and_then(Value::as_bool).unwrap_or(false);
        d.idle_secs = cfg.get("idle_secs").and_then(Value::as_u64).map(|n| n.to_string()).unwrap_or_default();
        d.discovery = s("discovery");
        d.phonebook = s("phonebook");
        d.peer = s("peer");
        d.listen = s("listen");
        d.cache_root = s("cache_root");
        d.shared_root = s("shared_root");
        d.cap_mbps = cfg.get("cap_mbps").and_then(Value::as_f64).map(|f| format!("{f}")).unwrap_or_default();
        d.capture_scale = cfg.get("capture_scale").and_then(Value::as_f64).map(|f| format!("{f}")).unwrap_or_default();
        if force_rows || !d.rows_dirty {
            let rows: Vec<PathMapping> = cfg.get("path_mappings").cloned()
                .and_then(|v| serde_json::from_value(v).ok()).unwrap_or_default();
            d.rows = rows.iter().map(Row::from_cfg).collect();
            d.rows_dirty = false;
        }
        self.remote = cfg;
    }

    fn remote_str(&self, k: &str) -> String {
        self.remote.get(k).and_then(Value::as_str).unwrap_or("").to_string()
    }

    fn send(&mut self, patch: Value) {
        if let Some(c) = &self.client {
            c.set_config(patch);
        }
    }

    fn pump(&mut self) {
        let events = match &self.client { Some(c) => c.poll(), None => Vec::new() };
        for ev in events {
            match ev.get("event").and_then(Value::as_str).unwrap_or("") {
                "config" => {
                    let rejected: Vec<String> = ev.get("rejected").and_then(Value::as_array)
                        .map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect()).unwrap_or_default();
                    let restart: Vec<String> = ev.get("needs_restart").and_then(Value::as_array)
                        .map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect()).unwrap_or_default();
                    if !rejected.is_empty() {
                        self.note = format!("not accepted: {}", rejected.join(", "));
                        self.note_is_error = true;
                    } else if !restart.is_empty() {
                        self.note = format!("takes effect after a restart: {}", restart.join(", "));
                        self.note_is_error = false;
                    } else if let Some(changed) = ev.get("changed").and_then(Value::as_array) {
                        if !changed.is_empty() {
                            self.note = format!("saved: {}", changed.iter().filter_map(Value::as_str).collect::<Vec<_>>().join(", "));
                            self.note_is_error = false;
                        }
                    }
                    if let Some(cfg) = ev.get("config") {
                        let changed_rows = ev.get("changed").and_then(Value::as_array)
                            .map(|a| a.iter().any(|v| v.as_str() == Some("path_mappings"))).unwrap_or(false);
                        self.take_config(cfg.clone(), changed_rows);
                    }
                }
                "status" => {
                    self.status = ev.get("status").and_then(Value::as_str).unwrap_or("").to_string();
                    self.addon_attached = ev.get("addon_attached").and_then(Value::as_bool).unwrap_or(false);
                    self.peer_up = ev.get("peer_up").and_then(Value::as_bool).unwrap_or(false);
                }
                "peer" => {
                    self.peer_up = ev.get("up").and_then(Value::as_bool).unwrap_or(false);
                    if self.peer_up {
                        self.peer_fingerprint = ev.get("fingerprint").and_then(Value::as_str).unwrap_or("").to_string();
                    }
                }
                "peers" => {
                    self.peers = ev.get("peers").and_then(Value::as_array).cloned().unwrap_or_default();
                    let sources: Vec<&str> = ev.get("sources").and_then(Value::as_array)
                        .map(|a| a.iter().filter_map(Value::as_str).collect()).unwrap_or_default();
                    self.peers_note = if self.peers.is_empty() { "none found".into() } else { format!("found via {}", sources.join(", ")) };
                }
                "error" => {
                    self.note = ev.get("msg").and_then(Value::as_str).unwrap_or("error").to_string();
                    self.note_is_error = true;
                }
                "_closed" => {
                    self.client = None;
                    self.connect_err = "the agent went away".into();
                }
                _ => {}
            }
        }
        if self.client.is_none() && self.last_try.elapsed() > Duration::from_secs(2) {
            self.try_connect();
        }
    }

    fn is_host(&self) -> bool {
        self.draft.role == "host"
    }

    /// A text field that commits on Enter or focus loss when it differs
    /// from what the agent holds.
    fn text_field(&mut self, ui: &mut egui::Ui, key: &str, get: fn(&mut Draft) -> &mut String) -> egui::Response {
        let w = ui.available_width() - 12.0;
        self.text_field_w(ui, key, get, w)
    }

    fn text_field_w(&mut self, ui: &mut egui::Ui, key: &str, get: fn(&mut Draft) -> &mut String, width: f32) -> egui::Response {
        let resp = ui.add(egui::TextEdit::singleline(get(&mut self.draft)).desired_width(width.max(120.0)));
        let committed = resp.lost_focus() || (resp.has_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter)));
        if committed {
            let value = get(&mut self.draft).clone();
            if value != self.remote_str(key) {
                self.send(json!({key: value}));
            }
        }
        resp
    }

    fn number_field(&mut self, ui: &mut egui::Ui, key: &str, get: fn(&mut Draft) -> &mut String, integer: bool) {
        let resp = ui.add(egui::TextEdit::singleline(get(&mut self.draft)).desired_width(90.0));
        let committed = resp.lost_focus() || (resp.has_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter)));
        if committed {
            let text = get(&mut self.draft).clone();
            let v = if integer { text.parse::<u64>().ok().map(|n| json!(n)) } else { text.parse::<f64>().ok().map(|f| json!(f)) };
            match v {
                Some(v) if Some(&v) != self.remote.get(key) => self.send(json!({key: v})),
                Some(_) => {}
                None => { self.note = format!("{key}: not a number"); self.note_is_error = true; }
            }
        }
    }

    fn pick_folder(&self, start: &str) -> Option<String> {
        let mut dlg = rfd::FileDialog::new();
        if !start.is_empty() && Path::new(start).is_dir() {
            dlg = dlg.set_directory(start);
        }
        dlg.pick_folder().map(|p| p.to_string_lossy().into_owned())
    }

    fn this_side_is_win(&self) -> bool {
        cfg!(windows)
    }

    fn section_machine(&mut self, ui: &mut egui::Ui) {
        theme::section(ui, "This machine");
        egui::Grid::new("machine").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
            ui.label("Name");
            let r = self.text_field(ui, "name", |d| &mut d.name);
            r.on_hover_text("Shown to peers and in the tray. Empty means the computer's name.");
            ui.end_row();

            ui.label("Role");
            ui.vertical(|ui| {
                let before = self.draft.role.clone();
                ui.radio_value(&mut self.draft.role, "host".to_string(), "Send scene — this Blender is the source; it dials the receiver");
                ui.radio_value(&mut self.draft.role, "replica".to_string(), "Receive scene — this machine listens, runs Blender for whoever pairs, and streams");
                if self.draft.role != before {
                    let role = self.draft.role.clone();
                    self.send(json!({"role": role}));
                }
            });
            ui.end_row();

            if !self.is_host() {
                ui.label("Blender");
                ui.horizontal(|ui| {
                    let r = ui.add(egui::TextEdit::singleline(&mut self.draft.blender_path).desired_width((ui.available_width() - 90.0).max(120.0)));
                    let committed = r.lost_focus() || (r.has_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter)));
                    if committed && self.draft.blender_path != self.remote_str("blender_path") {
                        let v = self.draft.blender_path.clone();
                        self.send(json!({"blender_path": v}));
                    }
                    if theme::icon_button(ui, theme::ph::FOLDER, "Browse").clicked() {
                        if let Some(p) = rfd::FileDialog::new().set_title("The Blender to run for a sender").pick_file() {
                            let mut p = p;
                            if cfg!(target_os = "macos") && p.extension().map(|e| e == "app").unwrap_or(false) {
                                p = p.join("Contents/MacOS/Blender");
                            }
                            self.draft.blender_path = p.to_string_lossy().into_owned();
                            let v = self.draft.blender_path.clone();
                            self.send(json!({"blender_path": v}));
                        }
                    }
                });
                ui.end_row();

                ui.label("Kiosk");
                if ui.checkbox(&mut self.draft.kiosk, "Full-screen viewport, no UI, when launched for a sender").changed() {
                    let v = self.draft.kiosk;
                    self.send(json!({"kiosk": v}));
                }
                ui.end_row();

                ui.label("Close Blender after");
                ui.horizontal(|ui| {
                    self.number_field(ui, "idle_secs", |d| &mut d.idle_secs, true);
                    ui.label("seconds without a sender (0 = never)");
                });
                ui.end_row();
            }
        });
    }

    fn section_pairing(&mut self, ui: &mut egui::Ui) {
        theme::section(ui, "Pairing");
        egui::Grid::new("pairing").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
            ui.label("Token");
            ui.vertical(|ui| {
                let set = self.remote.get("token_set").and_then(Value::as_bool).unwrap_or(false);
                let fp = self.remote_str("token_fingerprint");
                if set {
                    ui.label(format!("set · fingerprint {fp} — the other machine must show the same"));
                } else {
                    ui.colored_label(theme::WARN, "not set — both machines need the same token");
                }
                ui.horizontal(|ui| {
                    ui.add(egui::TextEdit::singleline(&mut self.draft.token_input).password(true).desired_width(260.0).hint_text("type the token"));
                    if ui.add_enabled_ui(!self.draft.token_input.is_empty(), |ui| theme::icon_button(ui, theme::ph::CHECK, "Set")).inner.clicked() {
                        let t = std::mem::take(&mut self.draft.token_input);
                        self.send(json!({"token": t}));
                    }
                    if set && theme::icon_button(ui, theme::ph::X, "Clear").clicked() {
                        self.send(json!({"token": ""}));
                    }
                });
            });
            ui.end_row();

            if self.is_host() {
                ui.label("Receiver");
                ui.vertical(|ui| {
                    ui.horizontal(|ui| {
                        self.text_field_w(ui, "peer", |d| &mut d.peer, 260.0).on_hover_text("address:port of the receiving machine's agent (its listen port, 19990 by default)");
                        if theme::icon_button(ui, theme::ph::MAGNIFYING_GLASS, "Find receivers").clicked() {
                            if let Some(c) = &self.client {
                                let addr = self.draft.peer.split(':').next().unwrap_or("").trim().to_string();
                                let mut cmd = json!({"cmd": "discover"});
                                if !addr.is_empty() { cmd["addr"] = json!(addr); }
                                c.cmd(cmd);
                                self.peers_note = "asking…".into();
                            }
                        }
                    });
                    if self.peer_up {
                        ui.label(RichText::new(format!("paired · receiver certificate {}…", self.peer_fingerprint.chars().take(16).collect::<String>())).color(theme::SUCCESS));
                    }
                    let pinned = self.remote_str("fingerprint");
                    ui.horizontal(|ui| {
                        if pinned.is_empty() {
                            ui.label(RichText::new("no certificate pinned yet — pinned on first pairing").weak());
                        } else {
                            ui.label(RichText::new(format!("pinned certificate {}…", pinned.chars().take(16).collect::<String>())).weak());
                            if theme::icon_button(ui, theme::ph::X_CIRCLE, "Forget").on_hover_text("Trust the receiver's certificate afresh at the next pairing").clicked() {
                                if let Some(c) = &self.client { c.cmd(json!({"cmd": "forget_fingerprint"})); }
                            }
                        }
                    });
                    if !self.peers_note.is_empty() {
                        ui.label(RichText::new(&self.peers_note).weak());
                    }
                    let peers = self.peers.clone();
                    for (i, p) in peers.iter().enumerate() {
                        let name = p.get("n").and_then(Value::as_str).unwrap_or("?");
                        let ip = p.get("ip").and_then(Value::as_str).unwrap_or("");
                        let port = p.get("port").and_then(Value::as_u64).unwrap_or(0);
                        let fp = p.get("fp").and_then(Value::as_str).unwrap_or("");
                        let paired = p.get("paired").and_then(Value::as_bool).unwrap_or(false);
                        let role = p.get("role").and_then(Value::as_str).unwrap_or("");
                        ui.horizontal(|ui| {
                            ui.label(format!("{name}  {ip}:{port}  {}{}", fp.chars().take(12).collect::<String>(), if paired { "  (paired)" } else { "" }));
                            let can = role == "replica" && port > 0;
                            if ui.add_enabled_ui(can, |ui| theme::icon_button(ui, theme::ph::LINK, "Pair")).inner.clicked() {
                                let peer = format!("{ip}:{port}");
                                self.draft.peer = peer.clone();
                                self.send(json!({"peer": peer, "fingerprint": fp}));
                            }
                        });
                        let _ = i;
                    }
                });
                ui.end_row();
            } else {
                ui.label("Listen on");
                ui.vertical(|ui| {
                    self.text_field(ui, "listen", |d| &mut d.listen).on_hover_text("IP:PORT; 0.0.0.0 means every interface");
                    if !self.own_fingerprint.is_empty() {
                        ui.label(RichText::new(format!("this machine's certificate {}…", self.own_fingerprint.chars().take(16).collect::<String>())).weak());
                    }
                    if self.peer_up {
                        ui.label(RichText::new("a sender is connected").color(theme::SUCCESS));
                    }
                });
                ui.end_row();

                ui.label("Network");
                ui.vertical(|ui| {
                    let before = self.draft.discovery.clone();
                    ui.radio_value(&mut self.draft.discovery, "off".to_string(), "Off — accept no connections");
                    ui.radio_value(&mut self.draft.discovery, "direct".to_string(), "Direct — reachable by address (the VPN case)");
                    ui.radio_value(&mut self.draft.discovery, "discoverable".to_string(), "Discoverable — also announce on the local network");
                    if self.draft.discovery != before {
                        let v = self.draft.discovery.clone();
                        self.send(json!({"discovery": v}));
                    }
                });
                ui.end_row();
            }

        });
    }

    /// Caches and the phonebook are the shared folder's `cache` and
    /// `phonebook` subfolders, in this machine's spelling.
    fn derived(&self, root: &str) -> (String, String) {
        let root = root.trim_end_matches(['/', '\\']);
        if root.is_empty() {
            return (String::new(), String::new());
        }
        let sep = if self.this_side_is_win() { '\\' } else { '/' };
        (format!("{root}{sep}cache"), format!("{root}{sep}phonebook"))
    }

    /// Commit the shared folder: the folder, the derived cache root and
    /// phonebook (the two folders created if they can be). The other
    /// machine's spelling is the mapping table's business.
    fn commit_shared_root(&mut self, root: String) {
        let (cache, book) = self.derived(&root);
        for d in [&cache, &book] {
            if !d.is_empty() { let _ = std::fs::create_dir_all(d); }
        }
        self.draft.shared_root = root.clone();
        self.draft.cache_root = cache.clone();
        self.draft.phonebook = book.clone();
        self.send(json!({"shared_root": root, "cache_root": cache, "phonebook": book}));
    }

    fn section_storage(&mut self, ui: &mut egui::Ui) {
        theme::section(ui, "Shared storage");
        ui.label(RichText::new("The folder on storage both machines see, as this machine sees it. Simulation caches go in its `cache` subfolder and receivers list themselves in `phonebook`; every subfolder maps on its own.").weak());
        ui.add_space(4.0);
        let mut root = self.draft.shared_root.clone();
        let mut commit = false;
        egui::Grid::new("shared").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
            ui.label("Shared folder");
            ui.horizontal(|ui| {
                let w = ui.available_width() - 100.0;
                let hint = if self.this_side_is_win() { r"\\server\share\folder or M:\folder" } else { "/Volumes/share/folder" };
                let r = ui.add(egui::TextEdit::singleline(&mut root).desired_width(w.max(200.0)).hint_text(hint));
                if r.lost_focus() || (r.has_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter))) { commit = true; }
                if theme::icon_button(ui, theme::ph::FOLDER, "Browse").clicked() {
                    if let Some(p) = self.pick_folder(&root) {
                        root = p;
                        commit = true;
                    }
                }
            });
            ui.end_row();
            let (cache, book) = self.derived(&root);
            ui.label(RichText::new("caches").weak());
            ui.label(RichText::new(if cache.is_empty() { "—".to_string() } else { cache }).monospace().weak());
            ui.end_row();
            ui.label(RichText::new("phonebook").weak());
            ui.label(RichText::new(if book.is_empty() { "—".to_string() } else { book }).monospace().weak());
            ui.end_row();
        });
        if commit && root != self.draft.shared_root {
            self.commit_shared_root(root);
        }

        // The mapping table: the other machine's spelling of the same
        // storage. On a mixed pair the row for the shared folder forms
        // itself at pairing; it is shown here to be checked or corrected.
        ui.add_space(8.0);
        ui.label(RichText::new("Path mapping").strong());
        if self.draft.rows.is_empty() {
            ui.label(RichText::new("No rows yet. Pairing a Windows machine with a Mac fills the shared folder's row from what each side named; a same-system pair needs none.").weak());
        } else {
            ui.label(RichText::new("One row per storage root the two machines share, in both spellings. The shared folder's row is filled at pairing.").weak());
        }
        self.mapping_rows(ui);

        // A cache root or phonebook that are not the shared folder's
        // subfolders.
        let (dc, db) = self.derived(&self.draft.shared_root.clone());
        let custom = (!self.draft.cache_root.is_empty() && self.draft.cache_root != dc)
            || (!self.draft.phonebook.is_empty() && self.draft.phonebook != db);
        let title = if custom { "Advanced (in use)" } else { "Advanced" };
        egui::CollapsingHeader::new(title).default_open(custom).show(ui, |ui| {
            egui::Grid::new("storage-adv").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
                ui.label("Cache root");
                ui.horizontal(|ui| {
                    let w = ui.available_width() - 100.0;
                    self.text_field_w(ui, "cache_root", |d| &mut d.cache_root, w)
                        .on_hover_text("Where the sender writes simulation caches. Normally the shared root's `cache` subfolder.");
                    if theme::icon_button(ui, theme::ph::FOLDER, "Browse").clicked() {
                        if let Some(p) = self.pick_folder(&self.draft.cache_root.clone()) {
                            self.draft.cache_root = p.clone();
                            self.send(json!({"cache_root": p}));
                        }
                    }
                });
                ui.end_row();
                ui.label("Phonebook folder");
                ui.horizontal(|ui| {
                    let w = ui.available_width() - 100.0;
                    self.text_field_w(ui, "phonebook", |d| &mut d.phonebook, w)
                        .on_hover_text("Where receivers list themselves. Normally the shared root's `phonebook` subfolder. Empty = off.");
                    if theme::icon_button(ui, theme::ph::FOLDER, "Browse").clicked() {
                        if let Some(p) = self.pick_folder(&self.draft.phonebook.clone()) {
                            self.draft.phonebook = p.clone();
                            self.send(json!({"phonebook": p}));
                        }
                    }
                });
                ui.end_row();
            });

        });
    }

    fn mapping_rows(&mut self, ui: &mut egui::Ui) {
        let this_is_win = self.this_side_is_win();
        let mut remove: Option<usize> = None;
        let n = self.draft.rows.len();
        for i in 0..n {
            ui.add_space(4.0);
            ui.horizontal(|ui| {
                let row = &mut self.draft.rows[i];
                if ui.checkbox(&mut row.enabled, "").changed() { self.draft.rows_dirty = true; }
                let row = &mut self.draft.rows[i];
                ui.label(RichText::new(format!("{}.", i + 1)).weak());
                if ui.add(egui::TextEdit::singleline(&mut row.label).desired_width(120.0).hint_text("label")).changed() { self.draft.rows_dirty = true; }
                if theme::glyph_button(ui, theme::ph::X, "Remove this root").clicked() { remove = Some(i); }
            });
            let mut picked: Option<bool> = None; // Some(true) = win column
            egui::Grid::new(format!("row{i}")).num_columns(2).spacing([12.0, 4.0]).show(ui, |ui| {
                ui.label(RichText::new("Windows").weak());
                ui.horizontal(|ui| {
                    let w = ui.available_width() - if this_is_win { 40.0 } else { 12.0 };
                    let row = &mut self.draft.rows[i];
                    if ui.add(egui::TextEdit::singleline(&mut row.win).desired_width(w.max(200.0))).changed() { self.draft.rows_dirty = true; }
                    if this_is_win && theme::glyph_button(ui, theme::ph::FOLDER, "Choose the folder on this machine").clicked() { picked = Some(true); }
                });
                ui.end_row();
                ui.label(RichText::new("macOS").weak());
                ui.horizontal(|ui| {
                    let w = ui.available_width() - if this_is_win { 12.0 } else { 40.0 };
                    let row = &mut self.draft.rows[i];
                    if ui.add(egui::TextEdit::singleline(&mut row.mac).desired_width(w.max(200.0))).changed() { self.draft.rows_dirty = true; }
                    if !this_is_win && theme::glyph_button(ui, theme::ph::FOLDER, "Choose the folder on this machine").clicked() { picked = Some(false); }
                });
                ui.end_row();
            });
            if let Some(for_win) = picked {
                let start = if for_win { self.draft.rows[i].win.clone() } else { self.draft.rows[i].mac.clone() };
                if let Some(p) = self.pick_folder(&start) {
                    let other = mounts::other_form(&p, &self.mounts).map(|(_, o)| o);
                    let row = &mut self.draft.rows[i];
                    if for_win { row.win = p } else { row.mac = p }
                    if let Some(o) = other {
                        let target = if for_win { &mut row.mac } else { &mut row.win };
                        if target.is_empty() { *target = o; } else { row.hint = Some((!for_win, o)); }
                    }
                    self.draft.rows_dirty = true;
                }
            }
            if let Some((hint_is_win, text)) = self.draft.rows[i].hint.clone() {
                ui.horizontal(|ui| {
                    ui.label(RichText::new(format!("proposed {} form: {text}", if hint_is_win { "Windows" } else { "macOS" })).weak());
                    if ui.small_button("Use it").clicked() {
                        let row = &mut self.draft.rows[i];
                        if hint_is_win { row.win = text.clone() } else { row.mac = text.clone() }
                        row.hint = None;
                        self.draft.rows_dirty = true;
                    }
                    if ui.small_button("Dismiss").clicked() { self.draft.rows[i].hint = None; }
                });
            }
        }
        if let Some(i) = remove {
            self.draft.rows.remove(i);
            self.draft.rows_dirty = true;
        }
        ui.add_space(6.0);
        ui.horizontal(|ui| {
            if theme::icon_button(ui, theme::ph::PLUS, "Add root").clicked() {
                self.draft.rows.push(Row { enabled: true, ..Default::default() });
                self.draft.rows_dirty = true;
            }
            if ui.add_enabled_ui(self.draft.rows_dirty, |ui| theme::icon_button(ui, theme::ph::CHECK, "Apply roots")).inner.clicked() {
                let rows: Vec<Value> = self.draft.rows.iter().filter(|r| !(r.win.is_empty() && r.mac.is_empty())).map(Row::to_json).collect();
                self.send(json!({"path_mappings": rows}));
                self.draft.rows_dirty = false;
            }
            if self.draft.rows_dirty && theme::icon_button(ui, theme::ph::ARROW_COUNTER_CLOCKWISE, "Revert").clicked() {
                let remote = self.remote.clone();
                self.take_config(remote, true);
            }
            if self.draft.rows_dirty {
                ui.label(RichText::new("unsaved edits").color(theme::WARN));
            }
        });
    }

    fn section_stream(&mut self, ui: &mut egui::Ui) {
        theme::section(ui, "Stream");
        egui::Grid::new("stream").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
            ui.label("Wire-rate cap");
            ui.horizontal(|ui| {
                self.number_field(ui, "cap_mbps", |d| &mut d.cap_mbps, false);
                ui.label("Mbps for the video lane");
            });
            ui.end_row();
            ui.label("Capture scale");
            ui.horizontal(|ui| {
                self.number_field(ui, "capture_scale", |d| &mut d.capture_scale, false);
                ui.label("1.0 = native pixels; 0.5 halves each dimension (native capture)");
            });
            ui.end_row();
        });
    }

    fn section_diagnostics(&mut self, ui: &mut egui::Ui) {
        theme::section(ui, "Diagnostics");
        egui::Grid::new("diag").num_columns(2).spacing([12.0, 6.0]).show(ui, |ui| {
            ui.label("Agent"); ui.label(format!("{} · {}", self.version, self.status)); ui.end_row();
            ui.label("Blender"); ui.label(if self.addon_attached { "attached to the agent" } else { "not attached" }); ui.end_row();
            ui.label("Config"); ui.horizontal(|ui| {
                ui.monospace(self.cfg_path.to_string_lossy());
                if theme::icon_button(ui, theme::ph::FOLDER_OPEN, "Open folder").clicked() { open_folder(&self.base); }
            }); ui.end_row();
            ui.label("Log"); ui.monospace(self.base.join("agent.log").to_string_lossy()); ui.end_row();
        });
    }
}

fn open_folder(dir: &Path) {
    #[cfg(target_os = "macos")]
    let _ = std::process::Command::new("open").arg(dir).spawn();
    #[cfg(windows)]
    let _ = std::process::Command::new("explorer").arg(dir).spawn();
    #[cfg(not(any(target_os = "macos", windows)))]
    let _ = std::process::Command::new("xdg-open").arg(dir).spawn();
}

impl eframe::App for App {
    fn ui(&mut self, root: &mut egui::Ui, _frame: &mut eframe::Frame) {
        let ctx = root.ctx().clone();
        let ctx = &ctx;
        self.pump();
        ctx.request_repaint_after(Duration::from_millis(250));

        // --shot: a picture of this window for the notes, then exit.
        if let Some(path) = self.shot.clone() {
            if !self.shot_taken && self.opened_at.elapsed() > Duration::from_millis(1500) {
                self.shot_taken = true;
                ctx.send_viewport_cmd(egui::ViewportCommand::Screenshot(egui::UserData::default()));
            }
            let shot = ctx.input(|i| i.events.iter().find_map(|e| match e {
                egui::Event::Screenshot { image, .. } => Some(image.clone()),
                _ => None,
            }));
            if let Some(image) = shot {
                let (w, h) = (image.width() as u32, image.height() as u32);
                let mut rgba = Vec::with_capacity((w * h * 4) as usize);
                for px in &image.pixels { rgba.extend_from_slice(&px.to_array()); }
                if let Err(e) = write_png(&path, w, h, &rgba) { eprintln!("shot: {e}"); }
                // A close request is not enough on every platform; the
                // picture is written, and a lingering window would go on
                // acting on the agent. Leave now.
                std::process::exit(0);
            }
        }

        egui::CentralPanel::default().show(root, |ui| {
            if self.client.is_none() {
                ui.heading("QCBridge Agent");
                ui.add_space(8.0);
                ui.colored_label(theme::WARN, "The agent is not running on this machine.");
                ui.label(RichText::new(&self.connect_err).weak());
                ui.add_space(8.0);
                if theme::icon_button(ui, theme::ph::PLAY, "Start the agent").clicked() {
                    if let Ok(exe) = std::env::current_exe() {
                        let _ = std::process::Command::new(exe).arg("--config").arg(&self.cfg_path).spawn();
                    }
                }
                ui.label(RichText::new(format!("config: {}", self.cfg_path.display())).weak());
                return;
            }
            egui::ScrollArea::vertical().show(ui, |ui| {
                ui.horizontal(|ui| {
                    ui.heading(format!("QCBridge Agent — {}", self.draft.name));
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        let color = if self.peer_up { theme::SUCCESS } else { theme::TEXT_SECONDARY };
                        ui.label(RichText::new(&self.status).color(color));
                    });
                });
                if !self.note.is_empty() {
                    let color = if self.note_is_error { theme::ERROR } else { theme::SUCCESS };
                    ui.label(RichText::new(&self.note).color(color).small());
                }
                ui.separator();
                self.section_machine(ui);
                ui.separator();
                self.section_pairing(ui);
                ui.separator();
                self.section_storage(ui);
                ui.separator();
                if !self.is_host() {
                    self.section_stream(ui);
                    ui.separator();
                }
                self.section_diagnostics(ui);
            });
        });
    }
}

/// Uncompressed PNG (stored deflate blocks) — enough for a screenshot and
/// no image crate.
fn write_png(path: &Path, w: u32, h: u32, rgba: &[u8]) -> std::io::Result<()> {
    fn crc(data: &[u8]) -> u32 {
        let mut c = 0xFFFF_FFFFu32;
        for &b in data {
            c ^= b as u32;
            for _ in 0..8 { c = if c & 1 != 0 { 0xEDB8_8320 ^ (c >> 1) } else { c >> 1 }; }
        }
        !c
    }
    fn chunk(out: &mut Vec<u8>, kind: &[u8; 4], data: &[u8]) {
        out.extend_from_slice(&(data.len() as u32).to_be_bytes());
        let mut body = kind.to_vec();
        body.extend_from_slice(data);
        out.extend_from_slice(&body);
        out.extend_from_slice(&crc(&body).to_be_bytes());
    }
    let mut raw = Vec::with_capacity((w as usize * 4 + 1) * h as usize);
    for y in 0..h as usize {
        raw.push(0);
        raw.extend_from_slice(&rgba[y * w as usize * 4..(y + 1) * w as usize * 4]);
    }
    // zlib: stored blocks of up to 65535 bytes.
    let mut z = vec![0x78, 0x01];
    let mut a = 1u32; let mut b = 0u32;
    for &x in &raw { a = (a + x as u32) % 65521; b = (b + a) % 65521; }
    let mut i = 0;
    while i < raw.len() {
        let n = (raw.len() - i).min(65535);
        z.push(if i + n == raw.len() { 1 } else { 0 });
        z.extend_from_slice(&(n as u16).to_le_bytes());
        z.extend_from_slice(&(!(n as u16)).to_le_bytes());
        z.extend_from_slice(&raw[i..i + n]);
        i += n;
    }
    z.extend_from_slice(&((b << 16) | a).to_be_bytes());
    let mut out = vec![0x89, b'P', b'N', b'G', 0x0D, 0x0A, 0x1A, 0x0A];
    let mut ihdr = Vec::new();
    ihdr.extend_from_slice(&w.to_be_bytes());
    ihdr.extend_from_slice(&h.to_be_bytes());
    ihdr.extend_from_slice(&[8, 6, 0, 0, 0]);
    chunk(&mut out, b"IHDR", &ihdr);
    chunk(&mut out, b"IDAT", &z);
    chunk(&mut out, b"IEND", &[]);
    std::fs::write(path, out)
}
