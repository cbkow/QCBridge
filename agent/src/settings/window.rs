//! The settings window on the family's UI kit (`qcbridge-agent --settings`): min-ui's widgets and min-theme's
//! tokens over min-present, as minplatform's applications are built (the model is minRender's `shell.rs`, `ui.rs`
//! and `settings.rs`). It was ported section by section from the egui window it replaced; what it is to the agent
//! is unchanged: a second process attached as a control client, every value shown is the agent's, a field commits
//! through `set_config` and the `config` event that answers is what the field then shows. (The path mapping table
//! alone keeps its own model: edited here, sent on Apply.)
//!
//! Step 1 (2026-11-19): the window itself, the header, the "agent is not running" state, the card "This machine".
//! Step 2 (2026-11-19): Pairing, Shared storage, Stream, Diagnostics; one window for a config; the owner's changes
//! to the egui window's content - a mapping row that cannot be right is flagged, the wire-rate cap is gone (unused
//! since the quinn port), the Stream card only says where the stream is set, Diagnostics opens the log and keeps
//! the last problem.
//! Step 3 (2026-11-19): it is the settings window (`--settings`; `--settings-next` is still taken); egui is gone.

use super::client::{self, Client};
use crate::config::{self, PathMapping};
use crate::mounts;
use anyhow::{Result, anyhow};
use kurbo::{Affine, Rect};
use min_present::{Output, Presenter};
use min_theme::tokens as t;
use min_ui::fonts::draw_layout;
use min_ui::{
    Button, Event, Family, Fonts, Key, KeyEvent, Named, ScrollBar, Section, Switch, TextButton, TextField, Tooltips,
    UiLayer, Variant, paint,
};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime};
use vello::Scene;
use winit::application::ApplicationHandler;
use winit::event::{ButtonSource, ElementState, MouseButton, MouseScrollDelta, WindowEvent};
use winit::event_loop::{ActiveEventLoop, ControlFlow, EventLoop};
use winit::window::{Window, WindowAttributes, WindowId};
use winit31 as winit;

/// Every string the window shows goes through here. It is the single seam where the family's translation layer
/// (min-i18n's `trc`) will be attached; until then a string comes back as it went in.
fn tr(s: &str) -> String {
    s.to_string()
}

const SIZE: (f64, f64) = (900.0, 800.0);
const MIN_SIZE: (f64, f64) = (600.0, 480.0);
/// Where a control that is not shown is put, so that no press finds it.
const NOWHERE: Rect = Rect::new(-1000.0, -1000.0, -1000.0, -1000.0);

// The text fields that are always there, by their place in `Ui::fields`.
const NAME: usize = 0;
const BLENDER: usize = 1;
const RESOLUTION: usize = 2;
const IDLE: usize = 3;
const TOKEN: usize = 4;
const PEER: usize = 5;
const LISTEN: usize = 6;
const SHARED: usize = 7;
const CACHE: usize = 8;
const PHONEBOOK: usize = 9;
const FIELDS: usize = 10;
/// A mapping row's fields come after: `ROW0 + 3 * row + (0 label, 1 Windows, 2 macOS)`.
const ROW0: usize = 100;
const FIELD_NAMES: [&str; FIELDS] = ["name", "blender", "resolution", "idle", "token", "peer", "listen", "shared", "cache", "phonebook"];
const DISCOVERY: [&str; 3] = ["off", "direct", "discoverable"];

/// Open the window for the agent whose config is `cfg_path`. `--shot PATH` saves a picture of it and leaves.
pub fn run(cfg_path: PathBuf) -> Result<()> {
    let shot = std::env::args().skip_while(|a| a != "--shot").nth(1).map(PathBuf::from);
    // One window for a config: a later start brings the first to the front and leaves. (A picture is its own
    // window, beside whatever is open - unless QCB_SETTINGS_ONE asks for the rule, which is how it is checked.)
    let one = shot.is_none() || std::env::var_os("QCB_SETTINGS_ONE").is_some();
    let base = config::base_dir(&cfg_path);
    if one && one::hand_over(&base) {
        return Ok(());
    }
    let mut builder = EventLoop::builder();
    // macOS: the window belongs to a menu-bar app, as the egui window before it was: Accessory keeps it out of the
    // Dock while it still comes to the front. A picture (`--shot`) is taken without taking the front.
    #[cfg(target_os = "macos")]
    {
        use winit::platform::macos::{ActivationPolicy, EventLoopBuilderExtMacOS};
        builder
            .with_activation_policy(ActivationPolicy::Accessory)
            .with_activate_ignoring_other_apps(shot.is_none())
            .with_default_menu(false);
    }
    let el = builder.build().map_err(|e| anyhow!("window: {e}"))?;
    let failed = Arc::new(Mutex::new(None));
    let asked = Arc::new(AtomicBool::new(false));
    if one {
        let (asked, proxy) = (asked.clone(), el.create_proxy());
        one::listen(&base, move || {
            asked.store(true, Ordering::Relaxed);
            proxy.wake_up();
        });
    }
    let shell = Shell { cfg_path, shot, window: None, ui: None, mods: (false, false), next_tick: Instant::now(), failed: failed.clone(), asked };
    let ran = el.run_app(shell).map_err(|e| anyhow!("window: {e}"));
    one::release();
    ran?;
    match failed.lock().unwrap().take() {
        Some(why) => Err(anyhow!(why)),
        None => Ok(()),
    }
}

/// One settings window for a config folder (minRender's `instance.rs`, in the simplest form that is the same on
/// macOS and Windows and needs no crate): the first window listens on a loopback port of the system's choosing and
/// writes the port beside the config (`settings-window.port`); a later start reads it, says "show" and leaves when
/// it is answered. A file left by a window that was killed is not answered, and is taken over.
mod one {
    use std::io::{Read, Write};
    use std::net::{Ipv4Addr, SocketAddr, TcpListener, TcpStream};
    use std::path::{Path, PathBuf};
    use std::sync::OnceLock;
    use std::time::Duration;

    const ASK: &[u8] = b"qcbridge-settings show\n";
    const ANSWER: &[u8] = b"qcbridge-settings shown\n";
    static MINE: OnceLock<(PathBuf, u16)> = OnceLock::new();

    fn file(base: &Path) -> PathBuf {
        base.join("settings-window.port")
    }

    fn port_in(path: &Path) -> Option<u16> {
        std::fs::read_to_string(path).ok()?.trim().parse().ok()
    }

    /// A later start: true when a window that is open took the word (this process then leaves).
    pub fn hand_over(base: &Path) -> bool {
        let Some(port) = port_in(&file(base)) else { return false };
        let addr = SocketAddr::from((Ipv4Addr::LOCALHOST, port));
        let Ok(mut s) = TcpStream::connect_timeout(&addr, Duration::from_millis(300)) else { return false };
        let _ = s.set_read_timeout(Some(Duration::from_millis(800)));
        // (Windows gives the foreground only to a process allowed to take it: the first window is, by this call)
        #[cfg(windows)]
        unsafe {
            windows_sys::Win32::UI::WindowsAndMessaging::AllowSetForegroundWindow(u32::MAX);
        }
        if s.write_all(ASK).and_then(|_| s.flush()).is_err() {
            return false;
        }
        let mut answer = [0u8; 32];
        let n = s.read(&mut answer).unwrap_or(0);
        &answer[..n] == ANSWER
    }

    /// The first window: `show` is called for each later start.
    pub fn listen(base: &Path, show: impl Fn() + Send + 'static) {
        let Ok(listener) = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)) else { return };
        let Ok(port) = listener.local_addr().map(|a| a.port()) else { return };
        let path = file(base);
        if std::fs::write(&path, port.to_string()).is_err() {
            return;
        }
        let _ = MINE.set((path, port));
        let _ = std::thread::Builder::new().name("settings-one".into()).spawn(move || {
            for mut c in listener.incoming().flatten() {
                let _ = c.set_read_timeout(Some(Duration::from_millis(800)));
                let mut ask = [0u8; 32];
                let n = c.read(&mut ask).unwrap_or(0);
                if &ask[..n] == ASK {
                    let _ = c.write_all(ANSWER).and_then(|_| c.flush());
                    show();
                }
            }
        });
    }

    /// The window is closing: its file goes, if it is still its own.
    pub fn release() {
        if let Some((path, port)) = MINE.get() {
            if port_in(path) == Some(*port) {
                let _ = std::fs::remove_file(path);
            }
        }
    }
}

// ------------------------------------------------------------- the agent ----

/// The agent as this window knows it: the control socket and the live view it last reported.
struct Agent {
    cfg_path: PathBuf,
    base: PathBuf,
    role_hint: String,
    client: Option<Client>,
    connect_err: String,
    last_try: Instant,
    /// The agent's config, as last reported.
    remote: Value,
    /// Counts the reports: the window refills its fields when it moves.
    reports: u64,
    /// The last report is to replace the mapping rows even over unsent edits (an attach, or the rows were applied).
    rows_new: bool,
    version: String,
    status: String,
    addon_attached: bool,
    peer_up: bool,
    /// This machine's certificate (a receiver's), and the paired receiver's.
    own_fingerprint: String,
    peer_fingerprint: String,
    /// The receivers a search found, and what is said of the search.
    peers: Vec<Value>,
    peers_note: String,
    note: String,
    note_is_error: bool,
    /// The last status that read as a failure while this window was open: its words, when, and by which clock.
    problem: Option<(String, SystemTime, Instant)>,
    /// A command was sent: the answer is looked for often until then.
    eager_until: Instant,
}

fn names(ev: &Value, key: &str) -> Vec<String> {
    ev.get(key)
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect())
        .unwrap_or_default()
}

fn text_of(v: &Value, key: &str) -> String {
    v.get(key).and_then(Value::as_str).unwrap_or("").to_string()
}

fn flag_of(v: &Value, key: &str) -> bool {
    v.get(key).and_then(Value::as_bool).unwrap_or(false)
}

fn head(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

impl Agent {
    fn new(cfg_path: PathBuf) -> Agent {
        let base = config::base_dir(&cfg_path);
        let role_hint = std::fs::read_to_string(&cfg_path)
            .ok()
            .and_then(|text| toml::from_str::<config::Config>(&text).ok())
            .map(|c| c.role)
            .unwrap_or_else(|| "replica".into());
        let now = Instant::now();
        let mut agent = Agent {
            cfg_path,
            base,
            role_hint,
            client: None,
            connect_err: String::new(),
            last_try: now,
            remote: Value::Null,
            reports: 0,
            rows_new: false,
            version: String::new(),
            status: String::new(),
            addon_attached: false,
            peer_up: false,
            own_fingerprint: String::new(),
            peer_fingerprint: String::new(),
            peers: Vec::new(),
            peers_note: String::new(),
            note: String::new(),
            note_is_error: false,
            problem: None,
            eager_until: now,
        };
        agent.try_connect();
        agent
    }

    fn try_connect(&mut self) {
        self.last_try = Instant::now();
        // The registration is by role; the role may have flipped since the TOML was read, so try both.
        let other = if self.role_hint == "host" { "replica" } else { "host" };
        let mut last = String::new();
        for role in [self.role_hint.clone(), other.to_string()] {
            match client::socket_info(&self.base, &role).and_then(|(port, secret)| Client::connect(port, &secret)) {
                Ok(c) => {
                    let att = c.attached.clone();
                    self.client = Some(c);
                    self.connect_err.clear();
                    self.version = text_of(&att, "version");
                    self.own_fingerprint = text_of(&att, "fingerprint");
                    self.peer_fingerprint = text_of(&att, "peer_fingerprint");
                    self.addon_attached = flag_of(&att, "addon_attached");
                    self.peer_up = flag_of(&att, "peer_up");
                    self.set_status(text_of(&att, "status"));
                    if let Some(cfg) = att.get("config") {
                        self.take_config(cfg.clone(), true);
                    }
                    return;
                }
                Err(e) => last = format!("{e:#}"),
            }
        }
        self.connect_err = last;
    }

    fn take_config(&mut self, cfg: Value, rows_new: bool) {
        self.remote = cfg;
        self.reports += 1;
        self.rows_new |= rows_new;
    }

    /// The agent's status line; one that reads as a failure - by the words the tray's icon goes off for
    /// (`icons::TrayState::from_status`) - is kept as the last problem, with the time it was seen.
    fn set_status(&mut self, status: String) {
        let failure = crate::icons::TrayState::from_status(false, &status) == crate::icons::TrayState::Off;
        if failure && (status != self.status || self.problem.is_none()) {
            self.problem = Some((status.clone(), SystemTime::now(), Instant::now()));
        }
        self.status = status;
    }

    fn str(&self, key: &str) -> String {
        text_of(&self.remote, key)
    }

    fn is_host(&self) -> bool {
        self.str("role") == "host"
    }

    fn send(&mut self, patch: Value) {
        if let Some(c) = &self.client {
            c.set_config(patch);
            self.eager_until = Instant::now() + Duration::from_secs(1);
        }
    }

    fn cmd(&mut self, cmd: Value) {
        if let Some(c) = &self.client {
            c.cmd(cmd);
            self.eager_until = Instant::now() + Duration::from_secs(3);
        }
    }

    /// What the agent said since the last look; a lost agent is looked for again every two seconds. True when
    /// anything changed.
    fn pump(&mut self) -> bool {
        let events = match &self.client {
            Some(c) => c.poll(),
            None => Vec::new(),
        };
        let mut changed = !events.is_empty();
        for ev in events {
            match ev.get("event").and_then(Value::as_str).unwrap_or("") {
                "config" => {
                    let (rejected, restart, saved) = (names(&ev, "rejected"), names(&ev, "needs_restart"), names(&ev, "changed"));
                    if !rejected.is_empty() {
                        self.note = format!("{} {}", tr("not accepted:"), rejected.join(", "));
                        self.note_is_error = true;
                    } else if !restart.is_empty() {
                        self.note = format!("{} {}", tr("takes effect after a restart:"), restart.join(", "));
                        self.note_is_error = false;
                    } else if !saved.is_empty() {
                        self.note = format!("{} {}", tr("saved:"), saved.join(", "));
                        self.note_is_error = false;
                    }
                    if let Some(cfg) = ev.get("config") {
                        self.take_config(cfg.clone(), saved.iter().any(|k| k == "path_mappings"));
                    }
                }
                "status" => {
                    self.addon_attached = flag_of(&ev, "addon_attached");
                    self.peer_up = flag_of(&ev, "peer_up");
                    self.set_status(text_of(&ev, "status"));
                }
                "peer" => {
                    self.peer_up = flag_of(&ev, "up");
                    if self.peer_up {
                        self.peer_fingerprint = text_of(&ev, "fingerprint");
                    }
                }
                "peers" => {
                    self.peers = ev.get("peers").and_then(Value::as_array).cloned().unwrap_or_default();
                    self.peers_note = if self.peers.is_empty() { tr("none found") } else { format!("{} {}", tr("found via"), names(&ev, "sources").join(", ")) };
                }
                "error" => {
                    self.note = ev.get("msg").and_then(Value::as_str).map(String::from).unwrap_or_else(|| tr("error"));
                    self.note_is_error = true;
                }
                "_closed" => {
                    self.client = None;
                    self.connect_err = tr("the agent went away");
                }
                _ => {}
            }
        }
        if self.client.is_none() && self.last_try.elapsed() > Duration::from_secs(2) {
            self.try_connect();
            changed = true;
        }
        changed
    }
}

/// What the agent would do with this Blender path when a sender pairs (blender.rs): nothing for an empty path, the
/// path as it is when the file is there, and on Windows the newest installed Blender when it is not. The second
/// value: the line is a warning.
fn blender_line(path: &str) -> (String, bool) {
    let path = path.trim();
    if path.is_empty() {
        return (tr("No path: the agent does not start Blender; start it yourself."), false);
    }
    if Path::new(path).is_file() {
        return (tr("Found: this Blender is started for a sender."), false);
    }
    match config::blender_fallback(path) {
        Some(newest) => (format!("{} {newest}", tr("Not found at this path. The newest installed Blender is started instead:")), true),
        None if cfg!(windows) => {
            let home = config::BLENDER_HOME_WIN;
            (format!("{} {home}", tr("Not found at this path, and no Blender is installed under")), true)
        }
        None => (tr("Not found at this path."), true),
    }
}

/// Caches and the phonebook are the shared folder's `cache` and `phonebook` subfolders, in this machine's spelling.
fn derived(root: &str) -> (String, String) {
    let root = root.trim_end_matches(['/', '\\']);
    if root.is_empty() {
        return (String::new(), String::new());
    }
    let sep = if cfg!(windows) { '\\' } else { '/' };
    (format!("{root}{sep}cache"), format!("{root}{sep}phonebook"))
}

/// Why a mapping row's Windows column cannot be right, if it cannot: a Windows path starts with a drive letter and
/// a colon, or with two backslashes. (An empty column says nothing yet.)
fn win_problem(path: &str) -> Option<String> {
    let p = path.trim();
    let b = p.as_bytes();
    let drive = b.len() >= 2 && b[0].is_ascii_alphabetic() && b[1] == b':';
    (!p.is_empty() && !drive && !p.starts_with("\\\\")).then(|| tr("The Windows path does not look like one: it starts with a drive letter and a colon, or with two backslashes."))
}

/// The same for the macOS column: a macOS path starts with "/".
fn mac_problem(path: &str) -> Option<String> {
    let p = path.trim();
    (!p.is_empty() && !p.starts_with('/')).then(|| tr("The macOS path does not look like one: it starts with a slash."))
}

/// The time of day a moment was seen at: this machine's clock where the system's own call for it is at hand
/// (`localtime_r`), else UTC and said so.
fn clock(at: SystemTime) -> String {
    let secs = at.duration_since(SystemTime::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    #[cfg(unix)]
    {
        let mut tm: libc::tm = unsafe { std::mem::zeroed() };
        let t = secs as libc::time_t;
        if !unsafe { libc::localtime_r(&t, &mut tm) }.is_null() {
            return format!("{:02}:{:02}:{:02}", tm.tm_hour, tm.tm_min, tm.tm_sec);
        }
    }
    let rem = secs % 86_400;
    format!("{:02}:{:02}:{:02} UTC", rem / 3600, (rem / 60) % 60, rem % 60)
}

/// How long ago, in the fewest words.
fn ago(since: Instant) -> String {
    let s = since.elapsed().as_secs();
    if s < 60 {
        tr("just now")
    } else if s < 3600 {
        format!("{} {}", s / 60, tr("min ago"))
    } else {
        format!("{} {}", s / 3600, tr("h ago"))
    }
}

/// The system opens a folder in its file manager, and a file with what it has for it.
fn open_in_system(path: &Path) {
    #[cfg(target_os = "macos")]
    let _ = std::process::Command::new("open").arg(path).spawn();
    #[cfg(windows)]
    let _ = std::process::Command::new("explorer").arg(path).spawn();
    #[cfg(not(any(target_os = "macos", windows)))]
    let _ = std::process::Command::new("xdg-open").arg(path).spawn();
}

/// A picture of the window as a PNG file (`--shot`).
fn write_png(path: &Path, w: u32, h: u32, rgba: &[u8]) -> std::io::Result<()> {
    let mut e = png::Encoder::new(std::io::BufWriter::new(std::fs::File::create(path)?), w, h);
    e.set_color(png::ColorType::Rgba);
    e.set_depth(png::BitDepth::Eight);
    e.write_header().and_then(|mut wr| wr.write_image_data(rgba)).map_err(std::io::Error::other)
}

// ------------------------------------------------------------ the window ----

/// One row of the path mapping table: a storage root both machines see, in both spellings.
struct MapRow {
    on: Switch,
    label: TextField,
    win: TextField,
    mac: TextField,
    remove: TextButton,
    /// The folder picker, on this system's column.
    choose: TextButton,
    /// The other column as the picker proposes it, shown until taken or dismissed: (for the Windows column?, text).
    hint: Option<(bool, String)>,
    use_it: TextButton,
    dismiss: TextButton,
}

impl MapRow {
    fn new(m: &PathMapping, fonts: &mut Fonts) -> MapRow {
        let field = |text: &str, placeholder: &str, fonts: &mut Fonts| {
            let mut f = TextField::new(Family::Sans, t::BODY_SIZE_M);
            f.placeholder = placeholder.to_string();
            f.set_text_end(text, fonts);
            f
        };
        let small = |label: &str| TextButton::new(&tr(label), Variant::Secondary).size_s();
        MapRow {
            on: Switch::new(m.enabled),
            label: field(&m.label, &tr("label"), fonts),
            win: field(&m.win, "", fonts),
            mac: field(&m.mac, "", fonts),
            remove: small("Remove"),
            choose: small("Choose"),
            hint: None,
            use_it: small("Use it"),
            dismiss: small("Dismiss"),
        }
    }
    fn mapping(&self) -> PathMapping {
        PathMapping { win: self.win.text().to_string(), mac: self.mac.text().to_string(), enabled: self.on.on, label: self.label.text().to_string() }
    }
}

/// What the system's panel was opened for.
#[derive(Clone, Copy)]
enum Pick {
    Blender,
    Shared,
    Cache,
    Phonebook,
    /// A mapping row's column on this system: (row, the Windows column?).
    Row(usize, bool),
}

/// The page's measures for one frame, in physical pixels.
#[derive(Clone, Copy)]
struct Geo {
    s: f64,
    /// The page's and a card's inner left and right edges.
    x0: f64,
    x1: f64,
    cx0: f64,
    cx1: f64,
    cpad: f64,
    /// Between rows; between parts of a row; between a field and the line that belongs to it.
    gap: f64,
    tight: f64,
    hair: f64,
    field_h: f64,
    small_h: f64,
    body: f64,
    small: f64,
    label_w: f64,
    /// Where the value column begins.
    fx: f64,
}

const INK: min_theme::Color = t::NEUTRAL_CONTENT_COLOR_DEFAULT;
const SUB: min_theme::Color = t::NEUTRAL_SUBDUED_CONTENT_COLOR_DEFAULT;

struct Ui {
    window: Arc<Box<dyn Window>>,
    presenter: Presenter,
    layer: UiLayer,
    fonts: Fonts,
    agent: Agent,
    /// The agent's report the fields were last filled from.
    filled: u64,
    bar: ScrollBar,
    tips: Tooltips,
    /// What says something of itself when the pointer rests on it: laid out with the page, each frame.
    spots: Vec<(u64, Rect, String)>,
    /// Where each card begins in the page (scripted looks scroll to one).
    tops: Vec<(&'static str, f64)>,
    clipboard: Option<arboard::Clipboard>,
    pointer: (f64, f64),
    /// The fields that are always there: see the constants. The mapping rows hold their own.
    fields: [TextField; FIELDS],
    focus: Option<usize>,
    /// The focused field is to be scrolled into view (Tab went to it).
    reveal: bool,
    start: TextButton,
    /// Send scene, Receive scene.
    roles: [TextButton; 2],
    browse: TextButton,
    kiosk: Switch,
    presets: Vec<TextButton>,
    token_set: TextButton,
    token_clear: TextButton,
    find: TextButton,
    forget: TextButton,
    /// One for each receiver found.
    pair: Vec<TextButton>,
    /// Off, Direct, Discoverable.
    nets: [TextButton; 3],
    shared_browse: TextButton,
    rows: Vec<MapRow>,
    rows_dirty: bool,
    add_root: TextButton,
    apply_roots: TextButton,
    revert: TextButton,
    advanced: Section,
    /// Advanced was opened or closed by what the config holds, once.
    advanced_set: bool,
    cache_browse: TextButton,
    book_browse: TextButton,
    open_folder: TextButton,
    open_log: TextButton,
    mounts: Vec<mounts::Mount>,
    /// A file or folder chosen in the system's panel, and what for.
    picked: Arc<Mutex<Option<(Pick, String)>>>,
    /// The line under the Blender field: the path it was made for, when, and what it says.
    blender: (String, Instant, String, bool),
    shot: Option<PathBuf>,
    born: Instant,
    /// One more frame is owed (the page moved under what was drawn).
    again: bool,
    /// (scripted looks) The steps still to play, and when the next is due.
    demo: (Vec<String>, Instant),
}

/// One line of text, left at `x`, centred on `cy`, cut to the room it has. Returns its width.
#[allow(clippy::too_many_arguments)]
fn line(scene: &mut Scene, fonts: &mut Fonts, text: &str, x: f64, cy: f64, size: f64, bold: bool, color: min_theme::Color, room: f64) -> f64 {
    let weight = if bold { t::FONT_WEIGHT_BOLD } else { t::FONT_WEIGHT_REGULAR } as f32;
    let l = fonts.layout_fit(text, Family::Sans, size as f32, weight, paint::color(color), room.max(8.0));
    draw_layout(scene, &l, Affine::translate((x, cy - l.height() as f64 / 2.0)));
    l.width() as f64
}

/// A paragraph from `y` down, wrapped into `room`. Returns its height.
#[allow(clippy::too_many_arguments)]
fn para(scene: &mut Scene, fonts: &mut Fonts, text: &str, x: f64, y: f64, size: f64, color: min_theme::Color, room: f64) -> f64 {
    para_in(scene, fonts, text, x, y, size, color, room, Family::Sans)
}

/// The same in a family of one's choosing (a path is set in the fixed-width one).
#[allow(clippy::too_many_arguments)]
fn para_in(scene: &mut Scene, fonts: &mut Fonts, text: &str, x: f64, y: f64, size: f64, color: min_theme::Color, room: f64, family: Family) -> f64 {
    let l = fonts.layout_width(text, family, size as f32, t::FONT_WEIGHT_REGULAR as f32, paint::color(color), Some(room.max(40.0) as f32));
    draw_layout(scene, &l, Affine::translate((x, y)));
    l.height() as f64
}

/// A card of the page: the layer-2 tile, then what was drawn for it.
fn card(scene: &mut Scene, body: &Scene, tile: Rect, s: f64) {
    paint::rounded(scene, tile, t::CORNER_RADIUS_500 as f64 * s, t::BACKGROUND_LAYER_2_COLOR);
    scene.append(body, None);
}

/// A card's title: the family's tracked capitals. Returns its height.
fn card_title(scene: &mut Scene, fonts: &mut Fonts, text: &str, x: f64, y: f64, s: f64) -> f64 {
    let l = fonts.detail(text, t::DETAIL_SIZE_M * s as f32, paint::color(SUB));
    draw_layout(scene, &l, Affine::translate((x, y)));
    l.height() as f64
}

/// A row's label, in the label column, centred on `cy`.
fn label(scene: &mut Scene, fonts: &mut Fonts, g: Geo, text: &str, cy: f64) {
    line(scene, fonts, text, g.cx0, cy, g.body, false, INK, g.label_w - g.gap);
}

/// A small button with its left edge at `x`, centred in a row of height `h` from `y`. Returns its width.
#[allow(clippy::too_many_arguments)]
fn put(b: &mut TextButton, scene: &mut Scene, fonts: &mut Fonts, x: f64, y: f64, h: f64, s: f64, now: Instant) -> f64 {
    let (bw, bh) = b.measure(fonts, s);
    b.rect = Rect::new(x, y + (h - bh) / 2.0, x + bw, y + (h + bh) / 2.0);
    b.paint(scene, fonts, s, now);
    bw
}

/// The same with its right edge at `x1`.
#[allow(clippy::too_many_arguments)]
fn put_right(b: &mut TextButton, scene: &mut Scene, fonts: &mut Fonts, x1: f64, y: f64, h: f64, s: f64, now: Instant) -> f64 {
    let bw = b.measure(fonts, s).0;
    put(b, scene, fonts, x1 - bw, y, h, s, now)
}

/// One of a few: a button for each with what it means beside it, the chosen one filled. (min-ui has no radio
/// group.) Returns where the next row begins.
#[allow(clippy::too_many_arguments)]
fn choices(scene: &mut Scene, fonts: &mut Fonts, g: Geo, buttons: &mut [TextButton], says: &[String], chosen: Option<usize>, mut cy: f64, now: Instant) -> f64 {
    let bw = buttons.iter().map(|b| b.measure(fonts, g.s).0).fold(0.0, f64::max);
    for (i, said) in says.iter().enumerate() {
        buttons[i].variant = if chosen == Some(i) { Variant::Primary } else { Variant::Secondary };
        buttons[i].rect = Rect::new(g.fx, cy, g.fx + bw, cy + g.small_h);
        buttons[i].paint(scene, fonts, g.s, now);
        let tx = g.fx + bw + g.gap;
        let l = fonts.layout_width(said, Family::Sans, g.small as f32, t::FONT_WEIGHT_REGULAR as f32, paint::color(SUB), Some((g.cx1 - tx).max(40.0) as f32));
        let th = l.height() as f64;
        draw_layout(scene, &l, Affine::translate((tx, cy + ((g.small_h - th) / 2.0).max(0.0))));
        cy += g.small_h.max(th) + g.tight;
    }
    cy - g.tight + g.gap
}

/// A field by its number: one of the fixed ones, or a mapping row's.
fn fld<'a>(fields: &'a mut [TextField; FIELDS], rows: &'a mut [MapRow], id: usize) -> Option<&'a mut TextField> {
    if id < ROW0 {
        return fields.get_mut(id);
    }
    let row = rows.get_mut((id - ROW0) / 3)?;
    Some(match (id - ROW0) % 3 {
        0 => &mut row.label,
        1 => &mut row.win,
        _ => &mut row.mac,
    })
}

impl Ui {
    fn new(window: Arc<Box<dyn Window>>, agent: Agent, shot: Option<PathBuf>) -> Result<Ui> {
        let instance = Presenter::instance();
        let surface = instance.create_surface(window.clone()).map_err(|e| anyhow!("no surface for the window: {e}"))?;
        let size = window.surface_size();
        #[cfg(target_os = "macos")]
        let view = {
            use raw_window_handle::{HasWindowHandle, RawWindowHandle};
            match window.window_handle().map_err(|e| anyhow!("no window handle: {e}"))?.as_raw() {
                RawWindowHandle::AppKit(h) => Some(h.ns_view.as_ptr()),
                _ => None,
            }
        };
        #[cfg(not(target_os = "macos"))]
        let view = None;
        let (w, h) = (size.width.max(1), size.height.max(1));
        let mut presenter = Presenter::new(&instance, surface, (w, h), Output::Auto, view).map_err(|e| anyhow!("no renderer for the window: {e}"))?;
        presenter.engine.set_ui_scale(window.scale_factor() as f32);
        let layer = UiLayer::new(&presenter.engine.device, w, h).map_err(|e| anyhow!("no renderer for the window: {e}"))?;
        presenter.engine.set_overlay(Some(layer.view()));
        let small = |label: &str| TextButton::new(&tr(label), Variant::Secondary).size_s();
        let now = Instant::now();
        // (a page's own content: the bar shows whether or not the pointer is over it)
        let mut bar = ScrollBar::default();
        bar.always = true;
        let mut fields: [TextField; FIELDS] = std::array::from_fn(|i| {
            let f = TextField::new(Family::Sans, t::BODY_SIZE_M);
            if i == TOKEN { f.secret() } else { f }
        });
        fields[TOKEN].placeholder = tr("type the token");
        fields[SHARED].placeholder = if cfg!(windows) { r"\\server\share\folder or M:\folder".into() } else { "/Volumes/share/folder".into() };
        let mut ui = Ui {
            window,
            presenter,
            layer,
            fonts: Fonts::new(),
            agent,
            filled: 0,
            bar,
            tips: Tooltips::default(),
            spots: Vec::new(),
            tops: Vec::new(),
            clipboard: arboard::Clipboard::new().ok(),
            pointer: (-1.0, -1.0),
            fields,
            focus: None,
            reveal: false,
            start: TextButton::new(&tr("Start the agent"), Variant::Primary),
            roles: [small("Send scene"), small("Receive scene")],
            browse: small("Browse"),
            kiosk: Switch::new(false),
            presets: config::RESOLUTION_PRESETS.iter().map(|p| TextButton::new(p, Variant::Secondary).size_s()).collect(),
            token_set: small("Set"),
            token_clear: small("Clear"),
            find: small("Find receivers"),
            forget: small("Forget"),
            pair: Vec::new(),
            nets: [small("Off"), small("Direct"), small("Discoverable")],
            shared_browse: small("Browse"),
            rows: Vec::new(),
            rows_dirty: false,
            add_root: small("Add root"),
            apply_roots: TextButton::new(&tr("Apply roots"), Variant::Primary).size_s(),
            revert: small("Revert"),
            advanced: Section::new(&tr("Advanced"), false),
            advanced_set: false,
            cache_browse: small("Browse"),
            book_browse: small("Browse"),
            open_folder: small("Open folder"),
            open_log: small("Open log"),
            mounts: mounts::table(),
            picked: Arc::new(Mutex::new(None)),
            blender: (String::new(), now, String::new(), false),
            shot,
            born: now,
            again: false,
            demo: (std::env::var("QCB_SETTINGS_DEMO").map(|d| d.split(',').rev().map(String::from).collect()).unwrap_or_default(), now + Duration::from_millis(600)),
        };
        ui.fill(now);
        Ok(ui)
    }

    /// The fields take what the agent holds (its answer to a commit, or a change made elsewhere). The mapping rows
    /// follow it too, except while they have unsent edits.
    fn fill(&mut self, now: Instant) {
        self.filled = self.agent.reports;
        if self.agent.client.is_none() {
            return;
        }
        let idle = self.agent.remote.get("idle_secs").and_then(Value::as_u64).map(|n| n.to_string()).unwrap_or_default();
        let a = &self.agent;
        let values = [
            (NAME, a.str("name")),
            (BLENDER, a.str("blender_path")),
            (RESOLUTION, a.str("replica_resolution")),
            (IDLE, idle),
            (PEER, a.str("peer")),
            (LISTEN, a.str("listen")),
            (SHARED, a.str("shared_root")),
            (CACHE, a.str("cache_root")),
            (PHONEBOOK, a.str("phonebook")),
        ];
        for (i, v) in values {
            if self.fields[i].text() != v {
                self.fields[i].set_text_end(&v, &mut self.fonts);
            }
        }
        self.kiosk.set(flag_of(&self.agent.remote, "kiosk"), now);
        if std::mem::take(&mut self.agent.rows_new) || !self.rows_dirty {
            let theirs: Vec<PathMapping> = self.agent.remote.get("path_mappings").cloned().and_then(|v| serde_json::from_value(v).ok()).unwrap_or_default();
            let ours: Vec<PathMapping> = self.rows.iter().map(MapRow::mapping).collect();
            if ours != theirs || self.rows_dirty {
                if self.focus.is_some_and(|f| f >= ROW0) {
                    self.focus = None;
                }
                self.rows = theirs.iter().map(|m| MapRow::new(m, &mut self.fonts)).collect();
            }
            self.rows_dirty = false;
        }
        if !self.advanced_set {
            self.advanced_set = true;
            self.advanced.expanded = self.custom_roots();
        }
    }

    /// The cache root or the phonebook is not the shared folder's own subfolder.
    fn custom_roots(&self) -> bool {
        let (cache, book) = derived(&self.agent.str("shared_root"));
        let (c, b) = (self.agent.str("cache_root"), self.agent.str("phonebook"));
        (!c.is_empty() && c != cache) || (!b.is_empty() && b != book)
    }

    /// The fields that are shown, in Tab's order: down the page.
    fn shown(&self) -> Vec<usize> {
        if self.agent.client.is_none() {
            return Vec::new();
        }
        let host = self.agent.is_host();
        let mut v = vec![NAME];
        if !host {
            v.extend([BLENDER, RESOLUTION, IDLE]);
        }
        v.extend([TOKEN, if host { PEER } else { LISTEN }, SHARED]);
        v.extend((0..self.rows.len() * 3).map(|k| ROW0 + k));
        if self.advanced.expanded {
            v.extend([CACHE, PHONEBOOK]);
        }
        v
    }

    fn text(&self, field: usize) -> String {
        self.fields.get(field).map(|f| f.text().to_string()).unwrap_or_default()
    }

    /// A field is left (or Enter was pressed in it): its value goes to the agent when it differs from the agent's.
    /// The token is sent by its button, the mapping rows by Apply.
    fn commit(&mut self, field: usize) {
        if field >= FIELDS {
            return;
        }
        let text = self.text(field);
        let live = |key: &str| self.agent.str(key);
        match field {
            NAME if text != live("name") => self.agent.send(json!({"name": text})),
            BLENDER if text != live("blender_path") => self.agent.send(json!({"blender_path": text})),
            RESOLUTION if text != live("replica_resolution") && config::parse_resolution(&text).is_ok() => {
                self.agent.send(json!({"replica_resolution": text}))
            }
            IDLE => match text.parse::<u64>() {
                Ok(n) if Some(&json!(n)) != self.agent.remote.get("idle_secs") => self.agent.send(json!({"idle_secs": n})),
                Ok(_) => {}
                Err(_) => {
                    self.agent.note = format!("idle_secs: {}", tr("not a number"));
                    self.agent.note_is_error = true;
                }
            },
            PEER if text != live("peer") => self.agent.send(json!({"peer": text})),
            LISTEN if text != live("listen") => {
                if text.trim().parse::<std::net::SocketAddr>().is_ok() {
                    self.agent.send(json!({"listen": text.trim()}));
                } else {
                    self.agent.note = format!("listen: {}", tr("not an address (IP:PORT)"));
                    self.agent.note_is_error = true;
                }
            }
            SHARED if text != live("shared_root") => self.commit_shared_root(text),
            CACHE if text != live("cache_root") => self.agent.send(json!({"cache_root": text})),
            PHONEBOOK if text != live("phonebook") => self.agent.send(json!({"phonebook": text})),
            _ => {}
        }
    }

    /// The shared folder is committed with what follows from it: the cache root and the phonebook (the two folders
    /// made if they can be). The other machine's spelling is the mapping table's business.
    fn commit_shared_root(&mut self, root: String) {
        let (cache, book) = derived(&root);
        for d in [&cache, &book] {
            if !d.is_empty() {
                let _ = std::fs::create_dir_all(d);
            }
        }
        self.agent.send(json!({"shared_root": root, "cache_root": cache, "phonebook": book}));
    }

    fn set_token(&mut self) {
        let token = self.text(TOKEN);
        if !token.is_empty() {
            self.fields[TOKEN].clear();
            self.agent.send(json!({"token": token}));
        }
    }

    fn set_focus(&mut self, to: Option<usize>) {
        if let Some(old) = self.focus.filter(|old| Some(*old) != to) {
            self.commit(old);
        }
        self.focus = to;
        for (i, f) in self.fields.iter_mut().enumerate() {
            f.focused = Some(i) == to;
        }
        for (r, row) in self.rows.iter_mut().enumerate() {
            for (c, f) in [&mut row.label, &mut row.win, &mut row.mac].into_iter().enumerate() {
                f.focused = Some(ROW0 + r * 3 + c) == to;
            }
        }
    }

    fn resize(&mut self, w: u32, h: u32) {
        if w == 0 || h == 0 {
            return;
        }
        self.presenter.resize(w, h);
        self.presenter.engine.set_ui_scale(self.window.scale_factor() as f32);
        self.layer.resize(&self.presenter.engine.device, w, h);
        self.presenter.engine.set_overlay(Some(self.layer.view()));
    }

    fn wheel(&mut self, dy: f64) {
        self.bar.wheel(dy, Instant::now());
    }

    /// The system's panel, from a thread of its own (the window's loop goes on under it); what is chosen is taken
    /// at the next tick.
    fn pick(&self, what: Pick, start: String) {
        let picked = self.picked.clone();
        let title = tr("The Blender to run for a sender");
        std::thread::spawn(move || {
            let chosen = match what {
                Pick::Blender => rfd::FileDialog::new().set_title(title).pick_file().map(|p| {
                    // macOS: the application is a folder; what is run is the program inside it
                    if cfg!(target_os = "macos") && p.extension().is_some_and(|e| e == "app") { p.join("Contents/MacOS/Blender") } else { p }
                }),
                _ => {
                    let mut d = rfd::FileDialog::new();
                    if !start.is_empty() && Path::new(&start).is_dir() {
                        d = d.set_directory(&start);
                    }
                    d.pick_folder()
                }
            };
            if let Some(p) = chosen {
                *picked.lock().unwrap() = Some((what, p.to_string_lossy().into_owned()));
            }
        });
    }

    /// What the panel gave goes where it was asked for.
    fn take_picked(&mut self, what: Pick, path: String) {
        match what {
            Pick::Blender => {
                self.fields[BLENDER].set_text_end(&path, &mut self.fonts);
                self.agent.send(json!({"blender_path": path}));
            }
            Pick::Shared => {
                self.fields[SHARED].set_text_end(&path, &mut self.fonts);
                if path != self.agent.str("shared_root") {
                    self.commit_shared_root(path);
                }
            }
            Pick::Cache => {
                self.fields[CACHE].set_text_end(&path, &mut self.fonts);
                self.agent.send(json!({"cache_root": path}));
            }
            Pick::Phonebook => {
                self.fields[PHONEBOOK].set_text_end(&path, &mut self.fonts);
                self.agent.send(json!({"phonebook": path}));
            }
            Pick::Row(i, for_win) => {
                // the same folder as the other system spells it, when it is on a network mount this machine knows
                let other = mounts::other_form(&path, &self.mounts).map(|(_, o)| o);
                let Some(row) = self.rows.get_mut(i) else { return };
                let (this, that) = if for_win { (&mut row.win, &mut row.mac) } else { (&mut row.mac, &mut row.win) };
                this.set_text_end(&path, &mut self.fonts);
                if let Some(o) = other {
                    if that.text().is_empty() {
                        that.set_text_end(&o, &mut self.fonts);
                    } else if that.text() != o {
                        row.hint = Some((!for_win, o));
                    }
                }
                self.rows_dirty = true;
            }
        }
    }

    /// The agent is heard, and what was chosen in the system's panel is taken. True when the window is to be drawn.
    fn tick(&mut self) -> bool {
        let mut changed = self.agent.pump();
        let picked = self.picked.lock().unwrap().take();
        if let Some((what, path)) = picked {
            self.take_picked(what, path);
            changed = true;
        }
        changed
    }

    /// When the agent is to be heard next: soon while an answer is awaited, else four times a second.
    fn tick_every(&self) -> Duration {
        Duration::from_millis(if Instant::now() < self.agent.eager_until { 30 } else { 250 })
    }

    fn pointer(&mut self, e: &Event) {
        let now = Instant::now();
        if let Some(p) = e.pos() {
            self.pointer = p;
        } else {
            self.pointer = (-1.0, -1.0);
        }
        if matches!(e, Event::Down { .. }) {
            self.tips.pressed(now);
        }
        if self.bar.event(e, now).1 {
            return;
        }
        if self.agent.client.is_none() {
            if self.start.event(e, now).0 {
                if let Ok(exe) = std::env::current_exe() {
                    let _ = std::process::Command::new(exe).arg("--config").arg(&self.agent.cfg_path).spawn();
                }
            }
            return;
        }
        // A press outside the focused field leaves it (and so commits it) before anything else hears the press.
        let shown = self.shown();
        if let Event::Down { x, y, button: Button::Primary } = *e {
            let hit = shown.iter().copied().find(|i| fld(&mut self.fields, &mut self.rows, *i).is_some_and(|f| f.hit(x, y)));
            if hit != self.focus {
                self.set_focus(hit);
            }
        }
        for i in shown {
            if let Some(f) = fld(&mut self.fields, &mut self.rows, i) {
                f.pointer(e, &mut self.fonts);
            }
        }
        let host = self.agent.is_host();

        // -- This machine
        for (i, role) in ["host", "replica"].into_iter().enumerate() {
            if self.roles[i].event(e, now).0 && self.agent.str("role") != role {
                self.agent.send(json!({"role": role}));
            }
        }
        if !host {
            if self.browse.event(e, now).0 {
                self.pick(Pick::Blender, String::new());
            }
            if let Some(on) = self.kiosk.event(e, now).0 {
                self.agent.send(json!({"kiosk": on}));
            }
            for (i, preset) in config::RESOLUTION_PRESETS.iter().enumerate() {
                if self.presets[i].event(e, now).0 && self.agent.str("replica_resolution") != *preset {
                    self.fields[RESOLUTION].set_text_end(preset, &mut self.fonts);
                    self.agent.send(json!({"replica_resolution": preset}));
                }
            }
        }

        // -- Pairing
        if self.token_set.event(e, now).0 {
            self.set_token();
        }
        if self.token_clear.event(e, now).0 {
            self.agent.send(json!({"token": ""}));
        }
        if host {
            if self.find.event(e, now).0 {
                // one address (the receiver's, as typed) is asked directly; none: the local network and the phonebook
                let addr = self.text(PEER).split(':').next().unwrap_or("").trim().to_string();
                let mut cmd = json!({"cmd": "discover"});
                if !addr.is_empty() {
                    cmd["addr"] = json!(addr);
                }
                self.agent.cmd(cmd);
                self.agent.peers_note = tr("asking\u{2026}");
            }
            if self.forget.event(e, now).0 {
                self.agent.cmd(json!({"cmd": "forget_fingerprint"}));
            }
            for i in 0..self.pair.len() {
                if self.pair[i].event(e, now).0 {
                    let Some(p) = self.agent.peers.get(i) else { continue };
                    let peer = format!("{}:{}", text_of(p, "ip"), p.get("port").and_then(Value::as_u64).unwrap_or(0));
                    let fp = text_of(p, "fp");
                    self.fields[PEER].set_text_end(&peer, &mut self.fonts);
                    self.agent.send(json!({"peer": peer, "fingerprint": fp}));
                }
            }
        } else {
            for (i, mode) in DISCOVERY.into_iter().enumerate() {
                if self.nets[i].event(e, now).0 && self.agent.str("discovery") != mode {
                    self.agent.send(json!({"discovery": mode}));
                }
            }
        }

        // -- Shared storage
        if self.shared_browse.event(e, now).0 {
            self.pick(Pick::Shared, self.text(SHARED));
        }
        let (mut remove, mut choose) = (None, None);
        for (i, row) in self.rows.iter_mut().enumerate() {
            if row.on.event(e, now).0.is_some() {
                self.rows_dirty = true;
            }
            if row.remove.event(e, now).0 {
                remove = Some(i);
            }
            if row.choose.event(e, now).0 {
                choose = Some(i);
            }
            if row.use_it.event(e, now).0 {
                if let Some((for_win, text)) = row.hint.take() {
                    let f = if for_win { &mut row.win } else { &mut row.mac };
                    f.set_text_end(&text, &mut self.fonts);
                    self.rows_dirty = true;
                }
            }
            if row.dismiss.event(e, now).0 {
                row.hint = None;
            }
        }
        if let Some(i) = choose {
            let start = if cfg!(windows) { self.rows[i].win.text() } else { self.rows[i].mac.text() }.to_string();
            self.pick(Pick::Row(i, cfg!(windows)), start);
        }
        if let Some(i) = remove {
            self.set_focus(None);
            self.rows.remove(i);
            self.rows_dirty = true;
        }
        if self.add_root.event(e, now).0 {
            let row = MapRow::new(&PathMapping { enabled: true, ..Default::default() }, &mut self.fonts);
            self.rows.push(row);
            self.rows_dirty = true;
        }
        if self.apply_roots.event(e, now).0 {
            let rows: Vec<PathMapping> = self.rows.iter().map(MapRow::mapping).filter(|m| !(m.win.is_empty() && m.mac.is_empty())).collect();
            self.agent.send(json!({"path_mappings": rows}));
            self.rows_dirty = false;
        }
        if self.revert.event(e, now).0 {
            self.set_focus(None);
            self.agent.rows_new = true;
            self.fill(now);
        }
        self.advanced.event(e, now);
        if self.cache_browse.event(e, now).0 {
            self.pick(Pick::Cache, self.text(CACHE));
        }
        if self.book_browse.event(e, now).0 {
            self.pick(Pick::Phonebook, self.text(PHONEBOOK));
        }

        // -- Diagnostics (the system opens a folder, and a file with what it has for it)
        if self.open_folder.event(e, now).0 {
            open_in_system(&self.agent.base);
        }
        if self.open_log.event(e, now).0 {
            open_in_system(&self.agent.base.join("agent.log"));
        }
    }

    /// A key: Tab goes from field to field, Enter commits the field, Escape leaves it; the rest is the field's.
    fn key(&mut self, k: &KeyEvent) {
        let size = self.window.surface_size();
        match &k.key {
            Key::Char(c) if c == "PageUp" || c == "PageDown" => {
                let page = size.height as f64 * 0.9;
                self.wheel(if c == "PageUp" { -page } else { page });
            }
            Key::Named(Named::Tab) => {
                let shown = self.shown();
                if shown.is_empty() {
                    return;
                }
                let n = shown.len();
                let at = self.focus.and_then(|f| shown.iter().position(|i| *i == f));
                let to = match (at, k.shift) {
                    (Some(at), false) => shown[(at + 1) % n],
                    (Some(at), true) => shown[(at + n - 1) % n],
                    (None, false) => shown[0],
                    (None, true) => shown[n - 1],
                };
                self.set_focus(Some(to));
                if let Some(f) = fld(&mut self.fields, &mut self.rows, to) {
                    f.select_all(&mut self.fonts);
                }
                self.reveal = true;
            }
            _ => {
                let Some(id) = self.focus else { return };
                if matches!(k.key, Key::Named(Named::Enter)) {
                    if id == TOKEN { self.set_token() } else { self.commit(id) }
                    return;
                }
                if matches!(k.key, Key::Named(Named::Escape)) {
                    self.set_focus(None);
                    return;
                }
                let Some(f) = fld(&mut self.fields, &mut self.rows, id) else { return };
                let before = f.text().to_string();
                match &k.key {
                    Key::Char(c) if k.cmd && c.eq_ignore_ascii_case("v") => {
                        let text = self.clipboard.as_mut().and_then(|cb| cb.get_text().ok()).unwrap_or_default();
                        f.paste(text.lines().next().unwrap_or(""), &mut self.fonts);
                    }
                    Key::Char(c) if k.cmd && (c.eq_ignore_ascii_case("c") || c.eq_ignore_ascii_case("x")) => {
                        if let Some(text) = f.copy(c.eq_ignore_ascii_case("x"), &mut self.fonts) {
                            if let Some(cb) = self.clipboard.as_mut() {
                                let _ = cb.set_text(text);
                            }
                        }
                    }
                    _ => {
                        f.key(k, &mut self.fonts);
                    }
                }
                // (a mapping row is edited here and sent on Apply: an edit is remembered as unsent)
                if id >= ROW0 && f.text() != before {
                    self.rows_dirty = true;
                }
            }
        }
    }

    /// (scripted looks) A control by its name, where it was last drawn.
    fn spot(&self, name: &str) -> Option<Rect> {
        let nth = |prefix: &str| name.strip_prefix(prefix).and_then(|n| n.parse::<usize>().ok());
        let row = |prefix: &str| nth(prefix).and_then(|n| self.rows.get(n));
        Some(match name {
            "send" => self.roles[0].rect,
            "receive" => self.roles[1].rect,
            "browse" => self.browse.rect,
            "kiosk" => self.kiosk.rect,
            "set" => self.token_set.rect,
            "clear" => self.token_clear.rect,
            "find" => self.find.rect,
            "forget" => self.forget.rect,
            "add" => self.add_root.rect,
            "apply" => self.apply_roots.rect,
            "revert" => self.revert.rect,
            "advanced" => self.advanced.rect,
            _ => {
                if let Some(n) = nth("preset") {
                    self.presets.get(n)?.rect
                } else if let Some(n) = nth("pair") {
                    self.pair.get(n)?.rect
                } else if let Some(n) = nth("net") {
                    self.nets.get(n)?.rect
                } else if let Some(r) = row("remove") {
                    r.remove.rect
                } else if let Some(r) = row("switch") {
                    r.on.rect
                } else if let Some(r) = row("use") {
                    r.use_it.rect
                } else if let Some(r) = row("dismiss") {
                    r.dismiss.rect
                } else {
                    return None;
                }
            }
        })
    }

    /// (scripted looks) A field's number by its name: `name` ... `phonebook`, or `row<n>.label|win|mac`.
    fn field_named(&self, name: &str) -> Option<usize> {
        if let Some(i) = FIELD_NAMES.iter().position(|n| *n == name) {
            return Some(i);
        }
        let (row, col) = name.strip_prefix("row")?.split_once('.')?;
        let col = ["label", "win", "mac"].iter().position(|c| *c == col)?;
        Some(ROW0 + row.parse::<usize>().ok()? * 3 + col)
    }

    /// (scripted looks, as minRender's MR_DEMO: `QCB_SETTINGS_DEMO=step,step,...`, one step every 400 ms, a `--shot`
    /// waits for the last.) Each step goes through the window's own pointer and key paths - no system input:
    /// `press:<control>` (send, receive, kiosk, preset<n>, set, clear, find, forget, pair<n>, net<n>, add, apply,
    /// revert, advanced, remove<n>, switch<n>, use<n>, dismiss<n>), `type:<field>=<text>` (a press in the field,
    /// select all, the text, Enter; the fields are name, blender, resolution, idle, token, peer, listen, shared,
    /// cache, phonebook, row<n>.label|win|mac), `hover:<field>`, `tab`, `wheel:<px>`, `size:<w>x<h>`,
    /// `show:<card>` (machine, pairing, storage, stream, diagnostics: scrolled to the top), `wait`, and the
    /// clipboard's steps `paste:` / `pasteover:` / `copy:` / `cut:` (see them below).
    /// Two steps stand in for what a look cannot do: `pick:row<n>=<path>` is the folder the system's panel would
    /// have given for that row, and `hint:<n>=<text>` puts a proposal on a row where no mount offers one.
    fn play(&mut self, step: &str) {
        let (what, arg) = step.split_once(':').unwrap_or((step, ""));
        let press = |me: &mut Ui, r: Rect| {
            let (x, y) = (r.center().x, r.center().y);
            for e in [Event::Move { x, y }, Event::Down { x, y, button: Button::Primary }, Event::Up { x, y, button: Button::Primary }] {
                me.pointer(&e);
            }
        };
        let key = |key: Key, text: Option<String>, cmd: bool| KeyEvent { key, text, shift: false, cmd };
        match what {
            "press" => {
                if let Some(r) = self.spot(arg) {
                    press(self, r);
                }
            }
            "type" => {
                let (field, text) = arg.split_once('=').unwrap_or((arg, ""));
                let Some(id) = self.field_named(field) else { return };
                let Some(r) = fld(&mut self.fields, &mut self.rows, id).map(|f| f.rect) else { return };
                press(self, r);
                self.key(&key(Key::Char("a".into()), None, true));
                self.key(&key(Key::Named(Named::Backspace), None, false));
                for c in text.chars() {
                    self.key(&key(Key::Char(c.to_string()), Some(c.to_string()), false));
                }
                self.key(&key(Key::Named(Named::Enter), None, false));
            }
            "hover" => {
                let Some(id) = self.field_named(arg) else { return };
                if let Some(c) = fld(&mut self.fields, &mut self.rows, id).map(|f| f.rect.center()) {
                    self.pointer(&Event::Move { x: c.x, y: c.y });
                }
            }
            "tab" => self.key(&key(Key::Named(Named::Tab), None, false)),
            "wheel" => self.wheel(arg.parse().unwrap_or(0.0)),
            "size" => {
                if let Some((w, h)) = arg.split_once('x').and_then(|(w, h)| Some((w.parse::<f64>().ok()?, h.parse::<f64>().ok()?))) {
                    let _ = self.window.request_surface_size(winit::dpi::LogicalSize::new(w, h).into());
                }
            }
            "show" => {
                if let Some((_, top)) = self.tops.iter().find(|(name, _)| *name == arg) {
                    let to = top - t::SPACING_400 as f64 * self.window.scale_factor();
                    self.wheel(to - self.bar.offset);
                }
            }
            "pick" => {
                let (row, path) = arg.split_once('=').unwrap_or((arg, ""));
                if let Some(n) = row.strip_prefix("row").and_then(|n| n.parse::<usize>().ok()) {
                    self.take_picked(Pick::Row(n, cfg!(windows)), path.to_string());
                }
            }
            // The clipboard's keys, through `key` as a hand would give them. The clipboard is given `<text>` ("\n"
            // in it is a line break) for the step and gets its former text back after it (a clipboard
            // that holds no text is not touched: the step is then not played); what the field then holds
            // (or what a copy put on the clipboard) is written to stderr.
            //   paste:<field>=<text>      a press in the field, End, the paste key
            //   pasteover:<field>=<text>  a press, select all, the paste key (the selection is replaced)
            //   copy:<field> / cut:<field>  a press, select all, the copy or the cut key
            "paste" | "pasteover" | "copy" | "cut" => {
                let (field, text) = arg.split_once('=').unwrap_or((arg, "(nothing was copied)"));
                let Some(id) = self.field_named(field) else { return };
                let Some(r) = fld(&mut self.fields, &mut self.rows, id).map(|f| f.rect) else { return };
                let before = self.clipboard.as_mut().map(|cb| cb.get_text().ok());
                // (only text can be put back: a clipboard that holds something else is left alone, and the step is not played)
                let Some(Some(before)) = before else {
                    eprintln!("{what} {field}: not played - the clipboard is not at hand, or holds something that is not text");
                    return;
                };
                if let Some(cb) = self.clipboard.as_mut() {
                    let _ = cb.set_text(text.replace("\\n", "\n"));
                }
                press(self, r);
                match what {
                    "paste" => {
                        self.key(&key(Key::Named(Named::End), None, false));
                        self.key(&key(Key::Char("v".into()), None, true));
                    }
                    "pasteover" => {
                        self.key(&key(Key::Char("a".into()), None, true));
                        self.key(&key(Key::Char("v".into()), None, true));
                    }
                    _ => {
                        self.key(&key(Key::Char("a".into()), None, true));
                        self.key(&key(Key::Char(if what == "cut" { "x" } else { "c" }.into()), None, true));
                    }
                }
                let holds = fld(&mut self.fields, &mut self.rows, id).map(|f| f.text().to_string()).unwrap_or_default();
                let clip = self.clipboard.as_mut().and_then(|cb| cb.get_text().ok()).unwrap_or_default();
                if what.starts_with("paste") {
                    eprintln!("{what} {field}: the field holds {holds:?}");
                } else {
                    eprintln!("{what} {field}: the clipboard holds {clip:?}, the field {holds:?}");
                }
                if let Some(cb) = self.clipboard.as_mut() {
                    let _ = cb.set_text(before);
                }
            }
            "hint" => {
                let (row, text) = arg.split_once('=').unwrap_or((arg, ""));
                if let Some(r) = row.parse::<usize>().ok().and_then(|n| self.rows.get_mut(n)) {
                    r.hint = Some((!cfg!(windows), text.to_string()));
                }
            }
            _ => {}
        }
    }

    fn animating(&mut self) -> bool {
        let now = Instant::now();
        self.again
            || !self.demo.0.is_empty()
            || self.bar.animating(now)
            || self.kiosk.animating(now)
            || self.advanced.animating(now)
            || self.rows.iter().any(|r| r.on.animating(now))
            || self.buttons().into_iter().any(|b| b.animating(now))
    }

    /// Every button there is.
    fn buttons(&mut self) -> Vec<&mut TextButton> {
        let mut v: Vec<&mut TextButton> = vec![
            &mut self.start,
            &mut self.browse,
            &mut self.token_set,
            &mut self.token_clear,
            &mut self.find,
            &mut self.forget,
            &mut self.shared_browse,
            &mut self.add_root,
            &mut self.apply_roots,
            &mut self.revert,
            &mut self.cache_browse,
            &mut self.book_browse,
            &mut self.open_folder,
            &mut self.open_log,
        ];
        v.extend(self.roles.iter_mut().chain(self.nets.iter_mut()).chain(self.presets.iter_mut()).chain(self.pair.iter_mut()));
        for r in self.rows.iter_mut() {
            v.extend([&mut r.remove, &mut r.choose, &mut r.use_it, &mut r.dismiss]);
        }
        v
    }

    /// When a tooltip is due to show or to go, if one is.
    fn next_change(&self) -> Option<Instant> {
        self.tips.next_change(Instant::now())
    }

    /// A later start of the settings window asked for this one.
    fn come_forward(&self) {
        if self.shot.is_some() {
            eprintln!("settings: a later start asked for this window");
            return;
        }
        self.window.set_minimized(false);
        self.window.focus_window();
    }

    fn redraw(&mut self) {
        let s = self.window.scale_factor();
        let size = self.window.surface_size();
        let (w, h) = (size.width as f64, size.height as f64);
        if w < 1.0 || h < 1.0 {
            return;
        }
        let now = Instant::now();
        self.again = false;
        if self.filled != self.agent.reports {
            self.fill(now);
        }
        if self.focus.is_some_and(|f| !self.shown().contains(&f)) {
            // (the field went with its section: nothing is committed from what is no longer shown)
            self.focus = None;
            for f in self.fields.iter_mut() {
                f.focused = false;
            }
        }
        let view = Rect::new(0.0, 0.0, w, h);
        let mut scene = Scene::new();
        // The surfaces, as the family's applications stack them: the window is layer 1, a card on it layer 2.
        paint::rect(&mut scene, view, t::BACKGROUND_LAYER_1_COLOR);
        let offset = self.bar.offset;
        let content = self.page(&mut scene, w, s, now);
        self.bar.set(view, content, s);
        if self.reveal {
            self.reveal = false;
            if let Some(r) = self.focus.and_then(|f| fld(&mut self.fields, &mut self.rows, f)).map(|f| f.rect) {
                let margin = t::SPACING_400 as f64 * s;
                if r.y0 < margin {
                    self.bar.wheel(r.y0 - margin, now);
                } else if r.y1 > h - margin {
                    self.bar.wheel(r.y1 - (h - margin), now);
                }
            }
        }
        if !self.demo.0.is_empty() && now >= self.demo.1 {
            let step = self.demo.0.pop().unwrap_or_default();
            self.demo.1 = now + Duration::from_millis(400);
            self.play(&step);
        }
        // (the page was drawn where it stood before it was measured: once more where it stands now)
        self.again = (self.bar.offset - offset).abs() > 0.5;
        self.bar.paint(&mut scene, now);

        // what the pointer rests on says what it is, after the family's delay
        let p = kurbo::Point::new(self.pointer.0, self.pointer.1);
        let over = self.spots.iter().find(|(_, r, _)| r.contains(p)).map(|(k, r, text)| (*k, *r, text.as_str()));
        self.tips.hover(over, now);
        self.tips.paint(&mut scene, &mut self.fonts, s, view, now);

        let dst = self.layer.scene_mut();
        dst.reset();
        dst.append(&scene, None);
        self.layer.render(&self.presenter.engine.device, &self.presenter.engine.queue);
        let window = self.window.clone();
        self.presenter.draw(|| window.pre_present_notify());

        // --shot: a picture of the window as drawn, a moment after it was made, then leave (as the egui window
        // does, and as minRender's MR_SHOT takes it: the presenter's own capture, linear, encoded for the file).
        if let Some(path) = self.shot.clone() {
            if self.born.elapsed() > Duration::from_millis(1500) && self.demo.0.is_empty() && now > self.demo.1 + Duration::from_millis(400) {
                let code = match self.presenter.capture_window() {
                    Some((px, pw, ph)) => {
                        let oetf = |v: f32| {
                            let v = v.clamp(0.0, 1.0);
                            ((if v <= 0.003_130_8 { 12.92 * v } else { 1.055 * v.powf(1.0 / 2.4) - 0.055 }) * 255.0 + 0.5) as u8
                        };
                        let rgba: Vec<u8> = (0..(pw * ph) as usize)
                            .flat_map(|i| [oetf(px[i * 4]), oetf(px[i * 4 + 1]), oetf(px[i * 4 + 2]), 255])
                            .collect();
                        match write_png(&path, pw, ph, &rgba) {
                            Ok(()) => 0,
                            Err(e) => {
                                eprintln!("shot: {e}");
                                1
                            }
                        }
                    }
                    None => {
                        eprintln!("shot: the presenter gave no picture of the window");
                        1
                    }
                };
                one::release();
                std::process::exit(code);
            }
        }
    }

    /// The page: a header, then cards one under the other, scrolled by the bar. Every control is placed here, each
    /// frame; one that is not shown is put nowhere. Returns the page's full height.
    fn page(&mut self, scene: &mut Scene, w: f64, s: f64, now: Instant) -> f64 {
        let px = |v: f32| v as f64 * s;
        let pad = px(t::SPACING_400);
        let cpad = px(t::SPACING_300);
        let label_w = px(t::SPACING_400) * 6.5;
        let g = Geo {
            s,
            x0: pad,
            x1: w - pad,
            cx0: pad + cpad,
            cx1: w - pad - cpad,
            cpad,
            gap: px(t::SPACING_200),
            tight: px(t::SPACING_100),
            hair: px(t::SPACING_75),
            field_h: px(t::FIELD_HEIGHT_M),
            small_h: px(t::COMPONENT_HEIGHT_75),
            body: px(t::BODY_SIZE_M),
            small: px(t::BODY_SIZE_S),
            label_w,
            fx: pad + cpad + label_w,
        };
        let offset = self.bar.offset;
        let mut y = pad - offset;

        for f in self.fields.iter_mut() {
            f.rect = NOWHERE;
        }
        for r in self.rows.iter_mut() {
            for f in [&mut r.label, &mut r.win, &mut r.mac] {
                f.rect = NOWHERE;
            }
            r.on.rect = NOWHERE;
        }
        for b in self.buttons() {
            b.rect = NOWHERE;
        }
        self.kiosk.rect = NOWHERE;
        self.advanced.rect = NOWHERE;
        self.spots.clear();
        self.tops.clear();

        // -- the header: whose agent this is, and how it stands (green while the peer is up)
        let fonts = &mut self.fonts;
        let head_h = px(t::COMPONENT_HEIGHT_100);
        let attached = self.agent.client.is_some();
        let status = if attached { self.agent.status.clone() } else { String::new() };
        let status_color = if self.agent.peer_up { t::POSITIVE_VISUAL_COLOR } else { SUB };
        let sl = fonts.layout_fit(&status, Family::Sans, g.body as f32, t::FONT_WEIGHT_REGULAR as f32, paint::color(status_color), (g.x1 - g.x0) * 0.5);
        let sw = sl.width() as f64;
        draw_layout(scene, &sl, Affine::translate((g.x1 - sw, y + (head_h - sl.height() as f64) / 2.0)));
        let title = if attached { format!("{} \u{2014} {}", tr("QCBridge Agent"), self.agent.str("name")) } else { tr("QCBridge Agent") };
        line(scene, fonts, &title, g.x0, y + head_h / 2.0, px(t::HEADING_SIZE_M), true, INK, g.x1 - g.x0 - sw - px(t::SPACING_300));
        y += head_h;
        // the one line under it: what the agent made of the last change (its room is kept, so nothing jumps)
        let note_h = if attached { px(t::COMPONENT_HEIGHT_50) } else { 0.0 };
        if attached && !self.agent.note.is_empty() {
            let color = if self.agent.note_is_error { t::NEGATIVE_VISUAL_COLOR } else { t::POSITIVE_VISUAL_COLOR };
            line(scene, fonts, &self.agent.note, g.x0, y + note_h / 2.0, g.small, false, color, g.x1 - g.x0);
        }
        y += note_h + g.tight;

        if !attached {
            // -- no agent answers for this config: say so, and offer to start it
            let mut b = Scene::new();
            let card_top = y;
            let mut cy = y + g.cpad;
            cy += para(&mut b, fonts, &tr("The agent is not running on this machine."), g.cx0, cy, g.body, t::NOTICE_VISUAL_COLOR, g.cx1 - g.cx0) + g.hair;
            if !self.agent.connect_err.is_empty() {
                cy += para(&mut b, fonts, &self.agent.connect_err, g.cx0, cy, g.small, SUB, g.cx1 - g.cx0);
            }
            cy += g.gap;
            let (bw, bh) = self.start.measure(fonts, s);
            self.start.rect = Rect::new(g.cx0, cy, g.cx0 + bw, cy + bh);
            self.start.paint(&mut b, fonts, s, now);
            cy += bh + g.gap;
            let cfg = format!("{} {}", tr("config:"), self.agent.cfg_path.display());
            cy += para(&mut b, fonts, &cfg, g.cx0, cy, g.small, SUB, g.cx1 - g.cx0);
            cy += g.cpad;
            card(scene, &b, Rect::new(g.x0, card_top, g.x1, cy), s);
            return cy + pad + offset;
        }

        let between = px(t::RAIL_SECTION_GAP);
        let host = self.agent.is_host();
        self.tops.push(("machine", y + offset));
        y = self.card_machine(scene, g, y, now) + between;
        self.tops.push(("pairing", y + offset));
        y = self.card_pairing(scene, g, y, now) + between;
        self.tops.push(("storage", y + offset));
        y = self.card_storage(scene, g, y, now) + between;
        if !host {
            self.tops.push(("stream", y + offset));
            y = self.card_stream(scene, g, y) + between;
        }
        self.tops.push(("diagnostics", y + offset));
        y = self.card_diagnostics(scene, g, y, now);
        y + pad + offset
    }

    /// This machine: its name, which way the scene goes, and for a receiver the Blender it runs.
    fn card_machine(&mut self, scene: &mut Scene, g: Geo, y: f64, now: Instant) -> f64 {
        let fonts = &mut self.fonts;
        let (s, fx, fx1) = (g.s, g.fx, g.cx1);
        let mut b = Scene::new();
        let mut cy = y + g.cpad;
        cy += card_title(&mut b, fonts, &tr("This machine"), g.cx0, cy, s) + g.gap;
        let host = self.agent.is_host();

        label(&mut b, fonts, g, &tr("Name"), cy + g.field_h / 2.0);
        self.fields[NAME].layout(fonts, Rect::new(fx, cy, fx1, cy + g.field_h), s);
        self.fields[NAME].paint(&mut b, fonts, s);
        self.spots.push((NAME as u64 + 1, self.fields[NAME].rect, tr("Shown to peers and in the tray. Empty means the computer's name.")));
        cy += g.field_h + g.gap;

        label(&mut b, fonts, g, &tr("Role"), cy + g.small_h / 2.0);
        let says = [tr("this Blender is the source; it dials the receiver"), tr("this machine listens, runs Blender for whoever pairs, and streams")];
        cy = choices(&mut b, fonts, g, &mut self.roles, &says, Some(if host { 0 } else { 1 }), cy, now);

        if !host {
            // Blender: the program started for a sender, and what would in fact be started
            label(&mut b, fonts, g, &tr("Blender"), cy + g.field_h / 2.0);
            let bw = self.browse.measure(fonts, s).0;
            self.fields[BLENDER].layout(fonts, Rect::new(fx, cy, fx1 - bw - g.tight, cy + g.field_h), s);
            self.fields[BLENDER].paint(&mut b, fonts, s);
            put_right(&mut self.browse, &mut b, fonts, fx1, cy, g.field_h, s, now);
            cy += g.field_h + g.hair;
            let path = self.fields[BLENDER].text().to_string();
            if self.blender.2.is_empty() || self.blender.0 != path || now.duration_since(self.blender.1) > Duration::from_secs(1) {
                let (text, warn) = blender_line(&path);
                self.blender = (path, now, text, warn);
            }
            let color = if self.blender.3 { t::NOTICE_VISUAL_COLOR } else { SUB };
            cy += para(&mut b, fonts, &self.blender.2, fx, cy, g.small, color, fx1 - fx) + g.gap;

            // Kiosk
            label(&mut b, fonts, g, &tr("Kiosk"), cy + g.small_h / 2.0);
            let (kw, kh) = Switch::size(s);
            self.kiosk.rect = Rect::new(fx, cy + (g.small_h - kh) / 2.0, fx + kw, cy + (g.small_h + kh) / 2.0);
            self.kiosk.paint(&mut b, s, now);
            let tx = fx + kw + g.gap;
            line(&mut b, fonts, &tr("Viewport only, no UI, when launched for a sender"), tx, cy + g.small_h / 2.0, g.small, false, SUB, fx1 - tx);
            cy += g.small_h + g.gap;

            // Resolution: the presets, a size of one's own, and what it is the size of
            label(&mut b, fonts, g, &tr("Resolution"), cy + g.small_h / 2.0);
            let chosen = self.agent.str("replica_resolution");
            let mut bx = fx;
            for (i, preset) in config::RESOLUTION_PRESETS.iter().enumerate() {
                let bw = self.presets[i].measure(fonts, s).0;
                if bx > fx && bx + bw > fx1 {
                    bx = fx;
                    cy += g.small_h + g.tight;
                }
                self.presets[i].variant = if chosen == *preset { Variant::Primary } else { Variant::Secondary };
                bx += put(&mut self.presets[i], &mut b, fonts, bx, cy, g.small_h, s, now) + g.tight;
            }
            cy += g.small_h + g.tight;
            let cw = line(&mut b, fonts, &tr("custom"), fx, cy + g.field_h / 2.0, g.small, false, SUB, g.label_w);
            let rx = fx + cw + g.tight;
            self.fields[RESOLUTION].error = config::parse_resolution(self.fields[RESOLUTION].text()).is_err();
            self.fields[RESOLUTION].layout(fonts, Rect::new(rx, cy, (rx + g.label_w).min(fx1), cy + g.field_h), s);
            self.fields[RESOLUTION].paint(&mut b, fonts, s);
            cy += g.field_h + g.hair;
            let help = tr("The Blender window the sender's session opens = the stream's resolution; fullscreen is the old behaviour");
            cy += para(&mut b, fonts, &help, fx, cy, g.small, SUB, fx1 - fx) + g.gap;

            // Close Blender after
            label(&mut b, fonts, g, &tr("Close Blender after"), cy + g.field_h / 2.0);
            let ix1 = fx + g.label_w * 0.6;
            self.fields[IDLE].layout(fonts, Rect::new(fx, cy, ix1, cy + g.field_h), s);
            self.fields[IDLE].paint(&mut b, fonts, s);
            let tx = ix1 + g.gap;
            let l = fonts.layout_width(&tr("seconds after the sender's session ends (0 = at once)"), Family::Sans, g.small as f32, t::FONT_WEIGHT_REGULAR as f32, paint::color(SUB), Some((fx1 - tx).max(40.0) as f32));
            let th = l.height() as f64;
            draw_layout(&mut b, &l, Affine::translate((tx, cy + ((g.field_h - th) / 2.0).max(0.0))));
            cy += g.field_h.max(th);
        } else {
            cy -= g.gap;
        }
        cy += g.cpad;
        card(scene, &b, Rect::new(g.x0, y, g.x1, cy), s);
        cy
    }

    /// Pairing: the token both machines hold; for a sender the receiver it dials and the ones a search finds; for a
    /// receiver where it listens and how it is found.
    fn card_pairing(&mut self, scene: &mut Scene, g: Geo, y: f64, now: Instant) -> f64 {
        let fonts = &mut self.fonts;
        let (s, fx, fx1) = (g.s, g.fx, g.cx1);
        let mut b = Scene::new();
        let mut cy = y + g.cpad;
        cy += card_title(&mut b, fonts, &tr("Pairing"), g.cx0, cy, s) + g.gap;
        let host = self.agent.is_host();

        // the token: never shown, only whether it is set and the few characters two people can compare
        let set = flag_of(&self.agent.remote, "token_set");
        let said = if set {
            format!("{} {} \u{2014} {}", tr("set \u{b7} fingerprint"), self.agent.str("token_fingerprint"), tr("the other machine must show the same"))
        } else {
            tr("not set \u{2014} both machines need the same token")
        };
        let th = para(&mut b, fonts, &said, fx, cy, g.small, if set { SUB } else { t::NOTICE_VISUAL_COLOR }, fx1 - fx);
        label(&mut b, fonts, g, &tr("Token"), cy + th.min(g.small_h) / 2.0);
        cy += th + g.hair;
        self.token_set.enabled = !self.fields[TOKEN].text().is_empty();
        let buttons = self.token_set.measure(fonts, s).0 + g.tight + if set { self.token_clear.measure(fonts, s).0 + g.tight } else { 0.0 };
        let tx1 = (fx + g.label_w * 2.0).min(fx1 - buttons);
        self.fields[TOKEN].layout(fonts, Rect::new(fx, cy, tx1, cy + g.field_h), s);
        self.fields[TOKEN].paint(&mut b, fonts, s);
        let mut bx = tx1 + g.tight;
        bx += put(&mut self.token_set, &mut b, fonts, bx, cy, g.field_h, s, now) + g.tight;
        if set {
            put(&mut self.token_clear, &mut b, fonts, bx, cy, g.field_h, s, now);
        }
        cy += g.field_h + g.gap;

        if host {
            // the receiver this machine dials, and a search for the ones there are
            label(&mut b, fonts, g, &tr("Receiver"), cy + g.field_h / 2.0);
            let bw = self.find.measure(fonts, s).0;
            self.fields[PEER].layout(fonts, Rect::new(fx, cy, fx1 - bw - g.tight, cy + g.field_h), s);
            self.fields[PEER].paint(&mut b, fonts, s);
            self.spots.push((PEER as u64 + 1, self.fields[PEER].rect, tr("address:port of the receiving machine's agent (its listen port, 19990 by default)")));
            put_right(&mut self.find, &mut b, fonts, fx1, cy, g.field_h, s, now);
            cy += g.field_h + g.hair;
            if self.agent.peer_up {
                let said = format!("{} {}\u{2026}", tr("paired \u{b7} receiver certificate"), head(&self.agent.peer_fingerprint, 16));
                cy += para(&mut b, fonts, &said, fx, cy, g.small, t::POSITIVE_VISUAL_COLOR, fx1 - fx) + g.hair;
            }
            let pinned = self.agent.str("fingerprint");
            if pinned.is_empty() {
                cy += para(&mut b, fonts, &tr("no certificate pinned yet \u{2014} pinned on first pairing"), fx, cy, g.small, SUB, fx1 - fx) + g.hair;
            } else {
                let bw = self.forget.measure(fonts, s).0;
                let said = format!("{} {}\u{2026}", tr("pinned certificate"), head(&pinned, 16));
                let tw = line(&mut b, fonts, &said, fx, cy + g.small_h / 2.0, g.small, false, SUB, fx1 - fx - bw - g.gap);
                put(&mut self.forget, &mut b, fonts, fx + tw + g.gap, cy, g.small_h, s, now);
                self.spots.push((50, self.forget.rect, tr("Trust the receiver's certificate afresh at the next pairing")));
                cy += g.small_h + g.hair;
            }
            if !self.agent.peers_note.is_empty() {
                cy += para(&mut b, fonts, &self.agent.peers_note, fx, cy, g.small, SUB, fx1 - fx) + g.hair;
            }
            // the receivers found: who, where, the start of its certificate; only a receiver can be paired with
            let n = self.agent.peers.len();
            self.pair.resize_with(n, || TextButton::new(&tr("Pair"), Variant::Secondary).size_s());
            for (i, p) in self.agent.peers.iter().enumerate() {
                let port = p.get("port").and_then(Value::as_u64).unwrap_or(0);
                let name = p.get("n").and_then(Value::as_str).unwrap_or("?");
                let mut said = format!("{name}  {}:{port}  {}", text_of(p, "ip"), head(&text_of(p, "fp"), 12));
                if flag_of(p, "paired") {
                    said = format!("{said}  ({})", tr("paired"));
                }
                self.pair[i].enabled = text_of(p, "role") == "replica" && port > 0;
                let bw = put_right(&mut self.pair[i], &mut b, fonts, fx1, cy, g.small_h, s, now);
                let l = fonts.layout_width(&said, Family::Sans, g.small as f32, t::FONT_WEIGHT_REGULAR as f32, paint::color(INK), Some((fx1 - fx - bw - g.gap).max(40.0) as f32));
                let th = l.height() as f64;
                draw_layout(&mut b, &l, Affine::translate((fx, cy + ((g.small_h - th) / 2.0).max(0.0))));
                cy += g.small_h.max(th) + g.hair;
            }
            cy -= g.hair;
        } else {
            // where this machine listens: an address and a port, or it is not sent
            label(&mut b, fonts, g, &tr("Listen on"), cy + g.field_h / 2.0);
            self.fields[LISTEN].error = self.fields[LISTEN].text().trim().parse::<std::net::SocketAddr>().is_err();
            self.fields[LISTEN].layout(fonts, Rect::new(fx, cy, fx1, cy + g.field_h), s);
            self.fields[LISTEN].paint(&mut b, fonts, s);
            self.spots.push((LISTEN as u64 + 1, self.fields[LISTEN].rect, tr("IP:PORT; 0.0.0.0 means every interface")));
            cy += g.field_h + g.hair;
            if !self.agent.own_fingerprint.is_empty() {
                let said = format!("{} {}\u{2026}", tr("this machine's certificate"), head(&self.agent.own_fingerprint, 16));
                cy += para(&mut b, fonts, &said, fx, cy, g.small, SUB, fx1 - fx) + g.hair;
            }
            if self.agent.peer_up {
                cy += para(&mut b, fonts, &tr("a sender is connected"), fx, cy, g.small, t::POSITIVE_VISUAL_COLOR, fx1 - fx) + g.hair;
            }
            cy += g.gap - g.hair;

            label(&mut b, fonts, g, &tr("Network"), cy + g.small_h / 2.0);
            let says = [tr("accept no connections"), tr("reachable by address (the VPN case)"), tr("also announce on the local network")];
            let mode = self.agent.str("discovery");
            cy = choices(&mut b, fonts, g, &mut self.nets, &says, DISCOVERY.iter().position(|m| *m == mode), cy, now) - g.gap;
        }
        cy += g.cpad;
        card(scene, &b, Rect::new(g.x0, y, g.x1, cy), s);
        cy
    }

    /// Shared storage: the folder both machines see, the table of its roots in both spellings, and the two folders
    /// that are not its own subfolders when someone made them so.
    fn card_storage(&mut self, scene: &mut Scene, g: Geo, y: f64, now: Instant) -> f64 {
        let fonts = &mut self.fonts;
        let (s, fx, fx1) = (g.s, g.fx, g.cx1);
        let mut b = Scene::new();
        let mut cy = y + g.cpad;
        cy += card_title(&mut b, fonts, &tr("Shared storage"), g.cx0, cy, s) + g.gap;
        let intro = tr("The folder on storage both machines see, as this machine sees it. Simulation caches go in its `cache` subfolder and receivers list themselves in `phonebook`; every subfolder maps on its own.");
        cy += para(&mut b, fonts, &intro, g.cx0, cy, g.small, SUB, g.cx1 - g.cx0) + g.gap;

        label(&mut b, fonts, g, &tr("Shared folder"), cy + g.field_h / 2.0);
        let bw = self.shared_browse.measure(fonts, s).0;
        self.fields[SHARED].layout(fonts, Rect::new(fx, cy, fx1 - bw - g.tight, cy + g.field_h), s);
        self.fields[SHARED].paint(&mut b, fonts, s);
        put_right(&mut self.shared_browse, &mut b, fonts, fx1, cy, g.field_h, s, now);
        cy += g.field_h + g.tight;
        // what follows from it (as typed, before it is committed)
        let (cache, book) = derived(self.fields[SHARED].text());
        for (name, value) in [(tr("caches"), cache), (tr("phonebook"), book)] {
            line(&mut b, fonts, &name, g.cx0, cy + g.small * 0.65, g.small, false, SUB, g.label_w - g.gap);
            let value = if value.is_empty() { "\u{2014}".to_string() } else { value };
            cy += para_in(&mut b, fonts, &value, fx, cy, g.small, SUB, fx1 - fx, Family::Mono) + g.hair;
        }
        cy += g.gap;

        // The mapping table: the other machine's spelling of the same storage. On a mixed pair the row for the
        // shared folder forms itself at pairing; it is shown here to be checked or corrected.
        cy += para_bold(&mut b, fonts, &tr("Path mapping"), g.cx0, cy, g.body, g.cx1 - g.cx0) + g.hair;
        let about = if self.rows.is_empty() {
            tr("No rows yet. Pairing a Windows machine with a Mac fills the shared folder's row from what each side named; a same-system pair needs none.")
        } else {
            tr("One row per storage root the two machines share, in both spellings. The shared folder's row is filled at pairing.")
        };
        cy += para(&mut b, fonts, &about, g.cx0, cy, g.small, SUB, g.cx1 - g.cx0) + g.gap;

        let this_is_win = cfg!(windows);
        let inner = g.tight;
        let (rx0, rx1) = (g.cx0 + inner, g.cx1 - inner);
        let col = g.label_w * 0.55;
        for (i, row) in self.rows.iter_mut().enumerate() {
            let mut rb = Scene::new();
            let top = cy;
            cy += inner;
            // whether it is used, its number, its label; Remove at the right
            let mut trail = g.tight;
            let (kw, kh) = Switch::size(s);
            row.on.rect = Rect::new(rx0, cy + (g.field_h - kh) / 2.0, rx0 + kw, cy + (g.field_h + kh) / 2.0);
            row.on.paint(&mut rb, s, now);
            let nx = rx0 + kw + g.gap;
            let nw = line(&mut rb, fonts, &format!("{}.", i + 1), nx, cy + g.field_h / 2.0, g.small, false, SUB, col);
            let bw = put_right(&mut row.remove, &mut rb, fonts, rx1, cy, g.field_h, s, now);
            let lx = nx + nw + g.tight;
            row.label.layout(fonts, Rect::new(lx, cy, (lx + g.label_w * 1.4).min(rx1 - bw - g.tight), cy + g.field_h), s);
            row.label.paint(&mut rb, fonts, s);
            cy += g.field_h + g.tight;
            // the two spellings; the folder picker on this system's column
            for win in [true, false] {
                line(&mut rb, fonts, if win { "Windows" } else { "macOS" }, rx0, cy + g.field_h / 2.0, g.small, false, SUB, col - g.tight);
                let mine = win == this_is_win;
                let bw = if mine { put_right(&mut row.choose, &mut rb, fonts, rx1, cy, g.field_h, s, now) + g.tight } else { 0.0 };
                if mine {
                    self.spots.push((1000 + i as u64, row.choose.rect, tr("Choose the folder on this machine")));
                }
                let f = if win { &mut row.win } else { &mut row.mac };
                f.layout(fonts, Rect::new(rx0 + col, cy, rx1 - bw, cy + g.field_h), s);
                f.paint(&mut rb, fonts, s);
                cy += g.field_h + g.tight;
            }
            // a column that cannot be right says so (a warning: the row can still be applied)
            for why in [win_problem(row.win.text()), mac_problem(row.mac.text())].into_iter().flatten() {
                cy += para(&mut rb, fonts, &why, rx0 + col, cy, g.small, t::NOTICE_VISUAL_COLOR, rx1 - rx0 - col) + g.hair;
                trail = g.hair;
            }
            // the other column as the picker proposes it, until it is taken or dismissed
            if let Some((for_win, text)) = &row.hint {
                let said = format!("{} {text}", if *for_win { tr("proposed Windows form:") } else { tr("proposed macOS form:") });
                cy += para(&mut rb, fonts, &said, rx0 + col, cy, g.small, SUB, rx1 - rx0 - col) + g.hair;
                let bw = put(&mut row.use_it, &mut rb, fonts, rx0 + col, cy, g.small_h, s, now);
                put(&mut row.dismiss, &mut rb, fonts, rx0 + col + bw + g.tight, cy, g.small_h, s, now);
                cy += g.small_h + g.tight;
                trail = g.tight;
            }
            cy += inner - trail;
            // (one grey step up on the card: the tone minplatform's rails give what sits inside a section)
            paint::rounded(&mut b, Rect::new(g.cx0, top, g.cx1, cy), t::CORNER_RADIUS_500 as f64 * s, min_ui::section::card_color());
            b.append(&rb, None);
            cy += g.tight;
        }
        let mut bx = g.cx0;
        bx += put(&mut self.add_root, &mut b, fonts, bx, cy, g.small_h, s, now) + g.tight;
        self.apply_roots.enabled = self.rows_dirty;
        bx += put(&mut self.apply_roots, &mut b, fonts, bx, cy, g.small_h, s, now) + g.tight;
        if self.rows_dirty {
            bx += put(&mut self.revert, &mut b, fonts, bx, cy, g.small_h, s, now) + g.gap;
            line(&mut b, fonts, &tr("unsaved edits"), bx, cy + g.small_h / 2.0, g.small, false, t::NOTICE_VISUAL_COLOR, g.cx1 - bx);
        }
        cy += g.small_h + g.gap;

        // A cache root or a phonebook that are not the shared folder's subfolders.
        let (dc, db) = derived(&self.agent.str("shared_root"));
        let (c, k) = (self.agent.str("cache_root"), self.agent.str("phonebook"));
        let custom = (!c.is_empty() && c != dc) || (!k.is_empty() && k != db);
        self.advanced.title = if custom { tr("Advanced (in use)") } else { tr("Advanced") };
        self.advanced.rect = Rect::new(g.cx0 - g.tight, cy, g.cx1 + g.tight, cy + Section::height(s));
        self.advanced.paint(&mut b, fonts, s, now, true);
        cy += Section::height(s);
        if self.advanced.expanded {
            cy += g.hair;
            let rows = [
                (CACHE, tr("Cache root"), &mut self.cache_browse, tr("Where the sender writes simulation caches. Normally the shared root's `cache` subfolder.")),
                (PHONEBOOK, tr("Phonebook folder"), &mut self.book_browse, tr("Where receivers list themselves. Normally the shared root's `phonebook` subfolder. Empty = off.")),
            ];
            for (id, name, browse, tip) in rows {
                label(&mut b, fonts, g, &name, cy + g.field_h / 2.0);
                let bw = put_right(browse, &mut b, fonts, fx1, cy, g.field_h, s, now);
                self.fields[id].layout(fonts, Rect::new(fx, cy, fx1 - bw - g.tight, cy + g.field_h), s);
                self.fields[id].paint(&mut b, fonts, s);
                self.spots.push((id as u64 + 1, self.fields[id].rect, tip));
                cy += g.field_h + g.gap;
            }
            cy -= g.gap;
        } else {
            cy -= g.tight;
        }
        cy += g.cpad;
        card(scene, &b, Rect::new(g.x0, y, g.x1, cy), s);
        cy
    }

    /// Stream (a receiver's): the agent holds none of the stream's settings - the frame rate, the bitrate, the SRT
    /// latency and the port are the add-on's preferences in Blender, and nothing the agent says carries them - so
    /// the card says where they are. (The wire-rate cap that stood here is unused since the quinn port.)
    fn card_stream(&mut self, scene: &mut Scene, g: Geo, y: f64) -> f64 {
        let fonts = &mut self.fonts;
        let mut b = Scene::new();
        let mut cy = y + g.cpad;
        cy += card_title(&mut b, fonts, &tr("Stream"), g.cx0, cy, g.s) + g.gap;
        let said = tr("The stream's settings (frame rate, bitrate, SRT latency, port) are in Blender's QCBridge preferences.");
        cy += para(&mut b, fonts, &said, g.cx0, cy, g.small, SUB, g.cx1 - g.cx0);
        cy += g.cpad;
        card(scene, &b, Rect::new(g.x0, y, g.x1, cy), g.s);
        cy
    }

    /// Diagnostics: what runs, whether Blender is attached, where the config and the log are, and the last status
    /// that read as a failure while this window was open.
    fn card_diagnostics(&mut self, scene: &mut Scene, g: Geo, y: f64, now: Instant) -> f64 {
        let fonts = &mut self.fonts;
        let (s, fx, fx1) = (g.s, g.fx, g.cx1);
        let mut b = Scene::new();
        let mut cy = y + g.cpad;
        cy += card_title(&mut b, fonts, &tr("Diagnostics"), g.cx0, cy, s) + g.gap;
        let row_h = g.small_h;
        // a label and its value, the value wrapped; a button at the right when there is one
        let row = |b: &mut Scene, fonts: &mut Fonts, name: String, value: &str, color: min_theme::Color, family: Family, button: Option<&mut TextButton>, cy: f64| -> f64 {
            label(b, fonts, g, &name, cy + row_h / 2.0);
            let bw = match button {
                Some(button) => put_right(button, b, fonts, fx1, cy, row_h, s, now) + g.gap,
                None => 0.0,
            };
            let l = fonts.layout_width(value, family, g.small as f32, t::FONT_WEIGHT_REGULAR as f32, paint::color(color), Some((fx1 - fx - bw).max(40.0) as f32));
            let th = l.height() as f64;
            draw_layout(b, &l, Affine::translate((fx, cy + ((row_h - th) / 2.0).max(0.0))));
            cy + row_h.max(th) + g.tight
        };
        let agent = format!("{} \u{b7} {}", self.agent.version, self.agent.status);
        cy = row(&mut b, fonts, tr("Agent"), &agent, INK, Family::Sans, None, cy);
        let blender = if self.agent.addon_attached { tr("attached to the agent") } else { tr("not attached") };
        cy = row(&mut b, fonts, tr("Blender"), &blender, INK, Family::Sans, None, cy);
        let config = self.agent.cfg_path.to_string_lossy().into_owned();
        cy = row(&mut b, fonts, tr("Config"), &config, INK, Family::Mono, Some(&mut self.open_folder), cy);
        let log = self.agent.base.join("agent.log").to_string_lossy().into_owned();
        cy = row(&mut b, fonts, tr("Log"), &log, INK, Family::Mono, Some(&mut self.open_log), cy);
        let (problem, color) = match &self.agent.problem {
            Some((what, at, since)) => (format!("{what} \u{2014} {} ({})", clock(*at), ago(*since)), t::NOTICE_VISUAL_COLOR),
            None => (tr("none"), SUB),
        };
        cy = row(&mut b, fonts, tr("Last problem"), &problem, color, Family::Sans, None, cy);
        cy += g.cpad - g.tight;
        card(scene, &b, Rect::new(g.x0, y, g.x1, cy), s);
        cy
    }
}

/// A bold paragraph (a table's heading inside a card). Returns its height.
fn para_bold(scene: &mut Scene, fonts: &mut Fonts, text: &str, x: f64, y: f64, size: f64, room: f64) -> f64 {
    let l = fonts.layout_width(text, Family::Sans, size as f32, t::FONT_WEIGHT_BOLD as f32, paint::color(INK), Some(room.max(40.0) as f32));
    draw_layout(scene, &l, Affine::translate((x, y)));
    l.height() as f64
}

// -------------------------------------------------------------- the loop ----

struct Shell {
    cfg_path: PathBuf,
    shot: Option<PathBuf>,
    window: Option<Arc<Box<dyn Window>>>,
    ui: Option<Ui>,
    /// (shift, the command key - Control on Windows)
    mods: (bool, bool),
    next_tick: Instant,
    /// Why the window could not be made.
    failed: Arc<Mutex<Option<String>>>,
    /// A later start of the settings window asked for this one (`one`).
    asked: Arc<AtomicBool>,
}

impl Shell {
    fn redraw(&self) {
        if let Some(w) = &self.window {
            w.request_redraw();
        }
    }
    fn pointer(&mut self, e: Event) {
        if let Some(ui) = self.ui.as_mut() {
            ui.pointer(&e);
        }
        self.redraw();
    }
}

impl ApplicationHandler for Shell {
    fn can_create_surfaces(&mut self, el: &dyn ActiveEventLoop) {
        if self.window.is_some() {
            return;
        }
        // (Windows shows a window white until a frame is drawn in it: made unseen, shown after its first frame)
        let mut attrs = WindowAttributes::default()
            .with_title(tr("QCBridge Agent"))
            .with_surface_size(winit::dpi::LogicalSize::new(SIZE.0, SIZE.1))
            .with_min_surface_size(winit::dpi::LogicalSize::new(MIN_SIZE.0, MIN_SIZE.1))
            .with_visible(!cfg!(windows));
        // in the middle of the main screen, as the egui window opens
        if let Some(m) = el.primary_monitor() {
            if let (Some(at), Some(mode)) = (m.position(), m.current_video_mode()) {
                let (screen, f) = (mode.size(), m.scale_factor());
                let (x, y) = ((screen.width as f64 - SIZE.0 * f) / 2.0, (screen.height as f64 - SIZE.1 * f) / 2.0);
                attrs = attrs.with_position(winit::dpi::PhysicalPosition::new(at.x + x.max(0.0) as i32, at.y + y.max(0.0) as i32));
            }
        }
        // Windows: the title bar's icon, and a caption in the window's own colour (as minplatform's applications)
        #[cfg(windows)]
        {
            use winit::platform::windows::WindowAttributesWindows;
            let (rgba, iw, ih) = crate::icons::rgba(crate::icons::APP_256);
            attrs = attrs.with_window_icon(winit::icon::RgbaIcon::new(rgba, iw, ih).ok().map(winit::icon::Icon::from));
            let c8 = |c: min_theme::Color| {
                winit::platform::windows::Color::from_rgb((c.r * 255.0).round() as u8, (c.g * 255.0).round() as u8, (c.b * 255.0).round() as u8)
            };
            attrs = attrs.with_theme(Some(winit::window::Theme::Dark));
            attrs = attrs.with_platform_attributes(Box::new(
                WindowAttributesWindows::default()
                    .with_title_background_color(Some(c8(t::BACKGROUND_LAYER_1_COLOR)))
                    .with_title_text_color(c8(t::NEUTRAL_CONTENT_COLOR_DEFAULT)),
            ));
        }
        let window = match el.create_window(attrs) {
            Ok(w) => Arc::new(w),
            Err(e) => {
                *self.failed.lock().unwrap() = Some(format!("the window could not be made: {e}"));
                el.exit();
                return;
            }
        };
        match Ui::new(window.clone(), Agent::new(self.cfg_path.clone()), self.shot.clone()) {
            Ok(mut ui) => {
                if cfg!(windows) {
                    ui.redraw();
                    window.set_visible(true);
                }
                self.ui = Some(ui);
            }
            Err(e) => {
                *self.failed.lock().unwrap() = Some(format!("{e:#}"));
                el.exit();
                return;
            }
        }
        window.request_redraw();
        self.window = Some(window);
    }

    fn proxy_wake_up(&mut self, _el: &dyn ActiveEventLoop) {
        if self.asked.swap(false, Ordering::Relaxed) {
            if let Some(ui) = self.ui.as_ref() {
                ui.come_forward();
            }
            self.redraw();
        }
    }

    fn window_event(&mut self, el: &dyn ActiveEventLoop, _id: WindowId, e: WindowEvent) {
        match e {
            WindowEvent::CloseRequested => el.exit(),
            WindowEvent::RedrawRequested => {
                if let Some(ui) = self.ui.as_mut() {
                    ui.redraw();
                    if ui.animating() {
                        self.redraw();
                    }
                }
            }
            WindowEvent::SurfaceResized(sz) => {
                if let Some(ui) = self.ui.as_mut() {
                    ui.resize(sz.width, sz.height);
                }
                self.redraw();
            }
            WindowEvent::ScaleFactorChanged { .. } => self.redraw(),
            WindowEvent::MouseWheel { delta, .. } => {
                let s = self.window.as_ref().map(|w| w.scale_factor()).unwrap_or(1.0);
                let dy = match delta {
                    MouseScrollDelta::LineDelta(_, y) => y as f64 * 60.0 * s,
                    MouseScrollDelta::PixelDelta(p) => p.y,
                    _ => 0.0,
                };
                if let Some(ui) = self.ui.as_mut() {
                    ui.wheel(-dy);
                }
                self.redraw();
            }
            WindowEvent::ModifiersChanged(m) => {
                let st = m.state();
                self.mods = (st.shift_key(), if cfg!(target_os = "macos") { st.meta_key() } else { st.control_key() });
            }
            WindowEvent::PointerMoved { position, .. } => self.pointer(Event::Move { x: position.x, y: position.y }),
            WindowEvent::PointerButton { state, position, button: ButtonSource::Mouse(b @ (MouseButton::Left | MouseButton::Right)), .. } => {
                let (x, y) = (position.x, position.y);
                let button = if b == MouseButton::Left { Button::Primary } else { Button::Secondary };
                self.pointer(if state == ElementState::Pressed { Event::Down { x, y, button } } else { Event::Up { x, y, button } });
            }
            WindowEvent::PointerLeft { .. } => self.pointer(Event::Leave),
            WindowEvent::KeyboardInput { event, .. } if event.state.is_pressed() => {
                use winit::keyboard::{Key as WKey, NamedKey};
                let named = |n: Named| Key::Named(n);
                let key = match &event.logical_key {
                    WKey::Named(NamedKey::ArrowUp) => named(Named::Up),
                    WKey::Named(NamedKey::ArrowDown) => named(Named::Down),
                    WKey::Named(NamedKey::ArrowLeft) => named(Named::Left),
                    WKey::Named(NamedKey::ArrowRight) => named(Named::Right),
                    WKey::Named(NamedKey::Home) => named(Named::Home),
                    WKey::Named(NamedKey::End) => named(Named::End),
                    WKey::Named(NamedKey::Escape) => named(Named::Escape),
                    WKey::Named(NamedKey::Enter) => named(Named::Enter),
                    WKey::Named(NamedKey::Tab) => named(Named::Tab),
                    WKey::Named(NamedKey::Delete) => named(Named::Delete),
                    WKey::Named(NamedKey::Backspace) => named(Named::Backspace),
                    WKey::Named(NamedKey::PageUp) => Key::Char("PageUp".into()),
                    WKey::Named(NamedKey::PageDown) => Key::Char("PageDown".into()),
                    WKey::Character(c) => Key::Char(c.to_string()),
                    _ => Key::Char(String::new()),
                };
                let text = event.text.as_ref().map(|t| t.to_string()).filter(|_| !self.mods.1);
                let ev = KeyEvent { key, text, shift: self.mods.0, cmd: self.mods.1 };
                if let Some(ui) = self.ui.as_mut() {
                    ui.key(&ev);
                }
                self.redraw();
            }
            _ => {}
        }
    }

    fn about_to_wait(&mut self, el: &dyn ActiveEventLoop) {
        let Some(ui) = self.ui.as_mut() else { return };
        let now = Instant::now();
        if now >= self.next_tick {
            // the window follows the agent: what it said is taken, and the page drawn (the status, the Blender line)
            ui.tick();
            self.next_tick = now + ui.tick_every();
            if let Some(w) = &self.window {
                w.request_redraw();
            }
        }
        let mut wake = self.next_tick;
        if let Some(tip) = ui.next_change() {
            if tip <= now {
                if let Some(w) = &self.window {
                    w.request_redraw();
                }
            } else {
                wake = wake.min(tip);
            }
        }
        el.set_control_flow(ControlFlow::WaitUntil(wake));
    }
}
