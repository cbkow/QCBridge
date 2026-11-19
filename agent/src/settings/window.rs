//! The settings window on the family's UI kit (`qcbridge-agent --settings-next`): min-ui's widgets and min-theme's
//! tokens over min-present, as minplatform's applications are built (the model is minRender's `shell.rs`, `ui.rs`
//! and `settings.rs`). It is the egui window of `mod.rs` ported section by section, and stands beside it until it
//! has every section; what it is to the agent is unchanged: a second process attached as a control client, every
//! value shown is the agent's, a field commits through `set_config` and the `config` event that answers is what
//! the field then shows.
//!
//! Step 1 (2026-11-19): the window itself, the header, the "agent is not running" state and the card
//! "This machine". The other cards are marked `// next:` where they go in the page.

use super::client::{self, Client};
use crate::config;
use anyhow::{Result, anyhow};
use kurbo::{Affine, Rect};
use min_present::{Output, Presenter};
use min_theme::tokens as t;
use min_ui::fonts::draw_layout;
use min_ui::{
    Button, Event, Family, Fonts, Key, KeyEvent, Named, ScrollBar, Switch, TextButton, TextField, Tooltips, UiLayer,
    Variant, paint,
};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
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

// The text fields, in Tab's order.
const NAME: usize = 0;
const BLENDER: usize = 1;
const RESOLUTION: usize = 2;
const IDLE: usize = 3;

/// Open the window for the agent whose config is `cfg_path`. `--shot PATH` saves a picture of it and leaves.
pub fn run(cfg_path: PathBuf) -> Result<()> {
    let shot = std::env::args().skip_while(|a| a != "--shot").nth(1).map(PathBuf::from);
    let mut builder = EventLoop::builder();
    // macOS: the window belongs to a menu-bar app, as the egui window does (mod.rs): Accessory keeps it out of the
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
    let shell = Shell { cfg_path, shot, window: None, ui: None, mods: (false, false), next_tick: Instant::now(), failed: failed.clone() };
    el.run_app(shell).map_err(|e| anyhow!("window: {e}"))?;
    match failed.lock().unwrap().take() {
        Some(why) => Err(anyhow!(why)),
        None => Ok(()),
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
    status: String,
    peer_up: bool,
    note: String,
    note_is_error: bool,
    /// A command was sent: the answer is looked for often until then.
    eager_until: Instant,
}

fn names(ev: &Value, key: &str) -> Vec<String> {
    ev.get(key)
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect())
        .unwrap_or_default()
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
            status: String::new(),
            peer_up: false,
            note: String::new(),
            note_is_error: false,
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
                    self.status = att.get("status").and_then(Value::as_str).unwrap_or("").to_string();
                    self.peer_up = att.get("peer_up").and_then(Value::as_bool).unwrap_or(false);
                    if let Some(cfg) = att.get("config") {
                        self.take_config(cfg.clone());
                    }
                    return;
                }
                Err(e) => last = format!("{e:#}"),
            }
        }
        self.connect_err = last;
    }

    fn take_config(&mut self, cfg: Value) {
        self.remote = cfg;
        self.reports += 1;
    }

    fn str(&self, key: &str) -> String {
        self.remote.get(key).and_then(Value::as_str).unwrap_or("").to_string()
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
                        self.take_config(cfg.clone());
                    }
                }
                "status" => {
                    self.status = ev.get("status").and_then(Value::as_str).unwrap_or("").to_string();
                    self.peer_up = ev.get("peer_up").and_then(Value::as_bool).unwrap_or(false);
                }
                "peer" => self.peer_up = ev.get("up").and_then(Value::as_bool).unwrap_or(false),
                "error" => {
                    self.note = ev.get("msg").and_then(Value::as_str).map(String::from).unwrap_or_else(|| tr("error"));
                    self.note_is_error = true;
                }
                "_closed" => {
                    self.client = None;
                    self.connect_err = tr("the agent went away");
                }
                // next: "peers" (the receivers found) belongs to the Pairing card
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

// ------------------------------------------------------------ the window ----

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
    clipboard: Option<arboard::Clipboard>,
    pointer: (f64, f64),
    /// Name, Blender, the custom resolution, the seconds: see the constants.
    fields: [TextField; 4],
    focus: Option<usize>,
    /// The focused field is to be scrolled into view (Tab went to it).
    reveal: bool,
    start: TextButton,
    /// Send scene, Receive scene.
    roles: [TextButton; 2],
    browse: TextButton,
    kiosk: Switch,
    presets: Vec<TextButton>,
    /// A file chosen in the system's panel, for the Blender field.
    picked: Arc<Mutex<Option<String>>>,
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
    let l = fonts.layout_width(text, Family::Sans, size as f32, t::FONT_WEIGHT_REGULAR as f32, paint::color(color), Some(room.max(40.0) as f32));
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
    let l = fonts.detail(text, t::DETAIL_SIZE_M * s as f32, paint::color(t::NEUTRAL_SUBDUED_CONTENT_COLOR_DEFAULT));
    draw_layout(scene, &l, Affine::translate((x, y)));
    l.height() as f64
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
        let field = || TextField::new(Family::Sans, t::BODY_SIZE_M);
        let small = |label: &str| TextButton::new(&tr(label), Variant::Secondary).size_s();
        let now = Instant::now();
        // (a page's own content: the bar shows whether or not the pointer is over it)
        let mut bar = ScrollBar::default();
        bar.always = true;
        let mut ui = Ui {
            window,
            presenter,
            layer,
            fonts: Fonts::new(),
            agent,
            filled: 0,
            bar,
            tips: Tooltips::default(),
            clipboard: arboard::Clipboard::new().ok(),
            pointer: (-1.0, -1.0),
            fields: [field(), field(), field(), field()],
            focus: None,
            reveal: false,
            start: TextButton::new(&tr("Start the agent"), Variant::Primary),
            roles: [small("Send scene"), small("Receive scene")],
            browse: small("Browse"),
            kiosk: Switch::new(false),
            presets: config::RESOLUTION_PRESETS.iter().map(|p| TextButton::new(p, Variant::Secondary).size_s()).collect(),
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

    /// The fields take what the agent holds (its answer to a commit, or a change made elsewhere).
    fn fill(&mut self, now: Instant) {
        self.filled = self.agent.reports;
        let idle = self.agent.remote.get("idle_secs").and_then(Value::as_u64).map(|n| n.to_string()).unwrap_or_default();
        let values = [self.agent.str("name"), self.agent.str("blender_path"), self.agent.str("replica_resolution"), idle];
        for (f, v) in self.fields.iter_mut().zip(values) {
            if f.text() != v {
                f.set_text_end(&v, &mut self.fonts);
            }
        }
        self.kiosk.set(self.agent.remote.get("kiosk").and_then(Value::as_bool).unwrap_or(false), now);
    }

    /// The fields that are shown, in Tab's order.
    fn shown(&self) -> &'static [usize] {
        if self.agent.client.is_none() {
            &[]
        } else if self.agent.is_host() {
            &[NAME]
        } else {
            &[NAME, BLENDER, RESOLUTION, IDLE]
        }
    }

    /// A field is left (or Enter was pressed in it): its value goes to the agent when it differs from the agent's.
    fn commit(&mut self, field: usize) {
        let text = self.fields[field].text().to_string();
        match field {
            NAME if text != self.agent.str("name") => self.agent.send(json!({"name": text})),
            BLENDER if text != self.agent.str("blender_path") => self.agent.send(json!({"blender_path": text})),
            RESOLUTION if text != self.agent.str("replica_resolution") && config::parse_resolution(&text).is_ok() => {
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
            _ => {}
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

    /// The agent is heard, and a file chosen in the system's panel is taken. True when the window is to be drawn.
    fn tick(&mut self) -> bool {
        let mut changed = self.agent.pump();
        let picked = self.picked.lock().unwrap().take();
        if let Some(path) = picked {
            self.fields[BLENDER].set_text_end(&path, &mut self.fonts);
            self.agent.send(json!({"blender_path": path}));
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
        if let Event::Down { x, y, button: Button::Primary } = *e {
            let hit = self.shown().iter().copied().find(|i| self.fields[*i].hit(x, y));
            if hit != self.focus {
                self.set_focus(hit);
            }
        }
        for i in self.shown() {
            self.fields[*i].pointer(e, &mut self.fonts);
        }
        for (i, role) in ["host", "replica"].into_iter().enumerate() {
            if self.roles[i].event(e, now).0 && self.agent.str("role") != role {
                self.agent.send(json!({"role": role}));
            }
        }
        if self.agent.is_host() {
            return;
        }
        if self.browse.event(e, now).0 {
            let picked = self.picked.clone();
            let title = tr("The Blender to run for a sender");
            // the system's panel, from a thread of its own (the window's loop goes on under it)
            std::thread::spawn(move || {
                if let Some(mut p) = rfd::FileDialog::new().set_title(title).pick_file() {
                    // macOS: the application is a folder; what is run is the program inside it
                    if cfg!(target_os = "macos") && p.extension().is_some_and(|e| e == "app") {
                        p = p.join("Contents/MacOS/Blender");
                    }
                    *picked.lock().unwrap() = Some(p.to_string_lossy().into_owned());
                }
            });
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
                self.fields[to].select_all(&mut self.fonts);
                self.reveal = true;
            }
            _ => {
                let Some(f) = self.focus else { return };
                match &k.key {
                    Key::Named(Named::Enter) => self.commit(f),
                    Key::Named(Named::Escape) => self.set_focus(None),
                    Key::Char(c) if k.cmd && c.eq_ignore_ascii_case("v") => {
                        let text = self.clipboard.as_mut().and_then(|cb| cb.get_text().ok()).unwrap_or_default();
                        self.fields[f].paste(text.lines().next().unwrap_or(""), &mut self.fonts);
                    }
                    Key::Char(c) if k.cmd && (c.eq_ignore_ascii_case("c") || c.eq_ignore_ascii_case("x")) => {
                        if let Some(text) = self.fields[f].copy(c.eq_ignore_ascii_case("x"), &mut self.fonts) {
                            if let Some(cb) = self.clipboard.as_mut() {
                                let _ = cb.set_text(text);
                            }
                        }
                    }
                    _ => {
                        self.fields[f].key(k, &mut self.fonts);
                    }
                }
            }
        }
    }

    /// (scripted looks, as minRender's MR_DEMO: `QCB_SETTINGS_DEMO=step,step,...`, one step every 400 ms, a `--shot`
    /// waits for the last.) Each step goes through the window's own pointer and key paths - no system input:
    /// `press:send|receive|browse|kiosk|preset<n>`, `type:name|blender|resolution|idle=<text>` (a press in the
    /// field, select all, the text, Enter), `hover:<field>`, `tab`, `wheel:<px>`, `size:<w>x<h>`.
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
                let r = match arg {
                    "send" => self.roles[0].rect,
                    "receive" => self.roles[1].rect,
                    "browse" => self.browse.rect,
                    "kiosk" => self.kiosk.rect,
                    _ => arg.strip_prefix("preset").and_then(|n| n.parse::<usize>().ok()).and_then(|n| self.presets.get(n)).map(|b| b.rect).unwrap_or(NOWHERE),
                };
                press(self, r);
            }
            "type" => {
                let (field, text) = arg.split_once('=').unwrap_or((arg, ""));
                let Some(f) = ["name", "blender", "resolution", "idle"].iter().position(|n| *n == field) else { return };
                let r = self.fields[f].rect;
                press(self, r);
                self.key(&key(Key::Char("a".into()), None, true));
                self.key(&key(Key::Named(Named::Backspace), None, false));
                for c in text.chars() {
                    self.key(&key(Key::Char(c.to_string()), Some(c.to_string()), false));
                }
                self.key(&key(Key::Named(Named::Enter), None, false));
            }
            "hover" => {
                if let Some(f) = ["name", "blender", "resolution", "idle"].iter().position(|n| *n == arg) {
                    let c = self.fields[f].rect.center();
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
            _ => {}
        }
    }

    fn animating(&self) -> bool {
        let now = Instant::now();
        self.again
            || !self.demo.0.is_empty()
            || self.bar.animating(now)
            || self.kiosk.animating(now)
            || self.start.animating(now)
            || self.browse.animating(now)
            || self.roles.iter().chain(self.presets.iter()).any(|b| b.animating(now))
    }

    /// When a tooltip is due to show or to go, if one is.
    fn next_change(&self) -> Option<Instant> {
        self.tips.next_change(Instant::now())
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
            if let Some(f) = self.focus {
                let (r, margin) = (self.fields[f].rect, t::SPACING_400 as f64 * s);
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
        let name = self.fields[NAME].rect;
        let tip = tr("Shown to peers and in the tray. Empty means the computer's name.");
        let over = (self.agent.client.is_some() && name.contains(p)).then_some((NAME as u64 + 1, name, tip.as_str()));
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
                match self.presenter.capture_window() {
                    Some((px, pw, ph)) => {
                        let oetf = |v: f32| {
                            let v = v.clamp(0.0, 1.0);
                            ((if v <= 0.003_130_8 { 12.92 * v } else { 1.055 * v.powf(1.0 / 2.4) - 0.055 }) * 255.0 + 0.5) as u8
                        };
                        let rgba: Vec<u8> = (0..(pw * ph) as usize)
                            .flat_map(|i| [oetf(px[i * 4]), oetf(px[i * 4 + 1]), oetf(px[i * 4 + 2]), 255])
                            .collect();
                        if let Err(e) = super::write_png(&path, pw, ph, &rgba) {
                            eprintln!("shot: {e}");
                            std::process::exit(1);
                        }
                        std::process::exit(0);
                    }
                    None => {
                        eprintln!("shot: the presenter gave no picture of the window");
                        std::process::exit(1);
                    }
                }
            }
        }
    }

    /// The page: a header, then cards one under the other, scrolled by the bar. Every control is placed here, each
    /// frame; one that is not shown is put nowhere. Returns the page's full height.
    fn page(&mut self, scene: &mut Scene, w: f64, s: f64, now: Instant) -> f64 {
        let fonts = &mut self.fonts;
        let (ink, sub) = (t::NEUTRAL_CONTENT_COLOR_DEFAULT, t::NEUTRAL_SUBDUED_CONTENT_COLOR_DEFAULT);
        let px = |v: f32| v as f64 * s;
        let (body, small) = (px(t::BODY_SIZE_M), px(t::BODY_SIZE_S));
        let pad = px(t::SPACING_400);
        let (x0, x1) = (pad, w - pad);
        let top = pad - self.bar.offset;
        let mut y = top;

        for f in self.fields.iter_mut() {
            f.rect = NOWHERE;
        }
        self.start.rect = NOWHERE;
        self.browse.rect = NOWHERE;
        self.kiosk.rect = NOWHERE;
        for b in self.roles.iter_mut().chain(self.presets.iter_mut()) {
            b.rect = NOWHERE;
        }

        // -- the header: whose agent this is, and how it stands (green while the peer is up)
        let head_h = px(t::COMPONENT_HEIGHT_100);
        let attached = self.agent.client.is_some();
        let status = if attached { self.agent.status.clone() } else { String::new() };
        let status_color = if self.agent.peer_up { t::POSITIVE_VISUAL_COLOR } else { sub };
        let sl = fonts.layout_fit(&status, Family::Sans, body as f32, t::FONT_WEIGHT_REGULAR as f32, paint::color(status_color), (x1 - x0) * 0.5);
        let sw = sl.width() as f64;
        draw_layout(scene, &sl, Affine::translate((x1 - sw, y + (head_h - sl.height() as f64) / 2.0)));
        let title = if attached { format!("{} \u{2014} {}", tr("QCBridge Agent"), self.agent.str("name")) } else { tr("QCBridge Agent") };
        line(scene, fonts, &title, x0, y + head_h / 2.0, px(t::HEADING_SIZE_M), true, ink, x1 - x0 - sw - px(t::SPACING_300));
        y += head_h;
        // the one line under it: what the agent made of the last change (its room is kept, so nothing jumps)
        let note_h = if attached { px(t::COMPONENT_HEIGHT_50) } else { 0.0 };
        if attached && !self.agent.note.is_empty() {
            let color = if self.agent.note_is_error { t::NEGATIVE_VISUAL_COLOR } else { t::POSITIVE_VISUAL_COLOR };
            line(scene, fonts, &self.agent.note, x0, y + note_h / 2.0, small, false, color, x1 - x0);
        }
        y += note_h + px(t::SPACING_100);

        let cpad = px(t::SPACING_300);
        let (cx0, cx1) = (x0 + cpad, x1 - cpad);
        let gap = px(t::SPACING_200);
        let field_h = px(t::FIELD_HEIGHT_M);
        let small_h = px(t::COMPONENT_HEIGHT_75);

        if !attached {
            // -- no agent answers for this config: say so, and offer to start it
            let mut b = Scene::new();
            let card_top = y;
            let mut cy = y + cpad;
            cy += para(&mut b, fonts, &tr("The agent is not running on this machine."), cx0, cy, body, t::NOTICE_VISUAL_COLOR, cx1 - cx0) + px(t::SPACING_75);
            if !self.agent.connect_err.is_empty() {
                cy += para(&mut b, fonts, &self.agent.connect_err, cx0, cy, small, sub, cx1 - cx0);
            }
            cy += gap;
            let (bw, bh) = self.start.measure(fonts, s);
            self.start.rect = Rect::new(cx0, cy, cx0 + bw, cy + bh);
            self.start.paint(&mut b, fonts, s, now);
            cy += bh + gap;
            let cfg = format!("{} {}", tr("config:"), self.agent.cfg_path.display());
            cy += para(&mut b, fonts, &cfg, cx0, cy, small, sub, cx1 - cx0);
            cy += cpad;
            card(scene, &b, Rect::new(x0, card_top, x1, cy), s);
            return cy + pad + self.bar.offset;
        }

        // -- This machine
        {
            let mut b = Scene::new();
            let card_top = y;
            let mut cy = y + cpad;
            cy += card_title(&mut b, fonts, &tr("This machine"), cx0, cy, s) + gap;
            let label_w = px(t::SPACING_400) * 6.5;
            let (fx, fx1) = (cx0 + label_w, cx1);
            let host = self.agent.is_host();

            line(&mut b, fonts, &tr("Name"), cx0, cy + field_h / 2.0, body, false, ink, label_w - gap);
            self.fields[NAME].layout(fonts, Rect::new(fx, cy, fx1, cy + field_h), s);
            self.fields[NAME].paint(&mut b, fonts, s);
            cy += field_h + gap;

            // Role: one of two. (min-ui has no radio group: a button for each, the chosen one filled.)
            line(&mut b, fonts, &tr("Role"), cx0, cy + small_h / 2.0, body, false, ink, label_w - gap);
            let says = [tr("this Blender is the source; it dials the receiver"), tr("this machine listens, runs Blender for whoever pairs, and streams")];
            let bw = self.roles.iter().map(|r| r.measure(fonts, s).0).fold(0.0, f64::max);
            for (i, said) in says.iter().enumerate() {
                self.roles[i].variant = if (i == 0) == host { Variant::Primary } else { Variant::Secondary };
                self.roles[i].rect = Rect::new(fx, cy, fx + bw, cy + small_h);
                self.roles[i].paint(&mut b, fonts, s, now);
                let tx = fx + bw + gap;
                let l = fonts.layout_width(said, Family::Sans, small as f32, t::FONT_WEIGHT_REGULAR as f32, paint::color(sub), Some((fx1 - tx).max(40.0) as f32));
                let th = l.height() as f64;
                draw_layout(&mut b, &l, Affine::translate((tx, cy + ((small_h - th) / 2.0).max(0.0))));
                cy += small_h.max(th) + px(t::SPACING_100);
            }
            cy += gap - px(t::SPACING_100);

            if !host {
                // Blender: the program started for a sender, and what would in fact be started
                line(&mut b, fonts, &tr("Blender"), cx0, cy + field_h / 2.0, body, false, ink, label_w - gap);
                let bw = self.browse.measure(fonts, s).0;
                self.fields[BLENDER].layout(fonts, Rect::new(fx, cy, fx1 - bw - px(t::SPACING_100), cy + field_h), s);
                self.fields[BLENDER].paint(&mut b, fonts, s);
                self.browse.rect = Rect::new(fx1 - bw, cy + (field_h - small_h) / 2.0, fx1, cy + (field_h + small_h) / 2.0);
                self.browse.paint(&mut b, fonts, s, now);
                cy += field_h + px(t::SPACING_75);
                let path = self.fields[BLENDER].text().to_string();
                if self.blender.2.is_empty() || self.blender.0 != path || now.duration_since(self.blender.1) > Duration::from_secs(1) {
                    let (text, warn) = blender_line(&path);
                    self.blender = (path, now, text, warn);
                }
                let color = if self.blender.3 { t::NOTICE_VISUAL_COLOR } else { sub };
                cy += para(&mut b, fonts, &self.blender.2, fx, cy, small, color, fx1 - fx) + gap;

                // Kiosk
                line(&mut b, fonts, &tr("Kiosk"), cx0, cy + small_h / 2.0, body, false, ink, label_w - gap);
                let (kw, kh) = Switch::size(s);
                self.kiosk.rect = Rect::new(fx, cy + (small_h - kh) / 2.0, fx + kw, cy + (small_h + kh) / 2.0);
                self.kiosk.paint(&mut b, s, now);
                let tx = fx + kw + gap;
                line(&mut b, fonts, &tr("Viewport only, no UI, when launched for a sender"), tx, cy + small_h / 2.0, small, false, sub, fx1 - tx);
                cy += small_h + gap;

                // Resolution: the presets, a size of one's own, and what it is the size of
                line(&mut b, fonts, &tr("Resolution"), cx0, cy + small_h / 2.0, body, false, ink, label_w - gap);
                let chosen = self.agent.str("replica_resolution");
                let mut bx = fx;
                for (i, preset) in config::RESOLUTION_PRESETS.iter().enumerate() {
                    let bw = self.presets[i].measure(fonts, s).0;
                    if bx > fx && bx + bw > fx1 {
                        bx = fx;
                        cy += small_h + px(t::SPACING_100);
                    }
                    self.presets[i].variant = if chosen == *preset { Variant::Primary } else { Variant::Secondary };
                    self.presets[i].rect = Rect::new(bx, cy, bx + bw, cy + small_h);
                    self.presets[i].paint(&mut b, fonts, s, now);
                    bx += bw + px(t::SPACING_100);
                }
                cy += small_h + px(t::SPACING_100);
                let cw = line(&mut b, fonts, &tr("custom"), fx, cy + field_h / 2.0, small, false, sub, label_w);
                let rx = fx + cw + px(t::SPACING_100);
                self.fields[RESOLUTION].error = config::parse_resolution(self.fields[RESOLUTION].text()).is_err();
                self.fields[RESOLUTION].layout(fonts, Rect::new(rx, cy, (rx + px(t::SPACING_400) * 6.0).min(fx1), cy + field_h), s);
                self.fields[RESOLUTION].paint(&mut b, fonts, s);
                cy += field_h + px(t::SPACING_75);
                let help = tr("The Blender window the sender's session opens = the stream's resolution; fullscreen is the old behaviour");
                cy += para(&mut b, fonts, &help, fx, cy, small, sub, fx1 - fx) + gap;

                // Close Blender after
                line(&mut b, fonts, &tr("Close Blender after"), cx0, cy + field_h / 2.0, body, false, ink, label_w - gap);
                let ix1 = fx + px(t::SPACING_400) * 4.0;
                self.fields[IDLE].layout(fonts, Rect::new(fx, cy, ix1, cy + field_h), s);
                self.fields[IDLE].paint(&mut b, fonts, s);
                let tx = ix1 + gap;
                let l = fonts.layout_width(&tr("seconds after the sender's session ends (0 = at once)"), Family::Sans, small as f32, t::FONT_WEIGHT_REGULAR as f32, paint::color(sub), Some((fx1 - tx).max(40.0) as f32));
                let th = l.height() as f64;
                draw_layout(&mut b, &l, Affine::translate((tx, cy + ((field_h - th) / 2.0).max(0.0))));
                cy += field_h.max(th);
            } else {
                cy -= gap;
            }
            cy += cpad;
            card(scene, &b, Rect::new(x0, card_top, x1, cy), s);
            y = cy + px(t::RAIL_SECTION_GAP);
        }

        // next: the Pairing card (the token; the receiver and the receivers found for a sender; listen and network for a receiver)
        // next: the Shared storage card (the shared folder, the path mapping rows, Advanced)
        // next: the Stream card (a receiver only: the wire-rate cap)
        // next: the Diagnostics card (the agent's version and status, Blender attached, the config and the log)

        y - px(t::RAIL_SECTION_GAP) + pad + self.bar.offset
    }
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
