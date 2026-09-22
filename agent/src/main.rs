//! QCBridge Agent — tray app owning the QUIC connection, the capture child
//! and (replica) Blender's lifecycle. See spikes/parity/PLAN.md "The QCBridge
//! Agent". Config: <config dir>/QCBridge/agent.toml; the addon finds the
//! local socket via agent.json next to it.
//!
//! GPL-3.0-or-later, same as the repository. SOURCE_URL is still reported by
//! `--version` and in every attach reply — no longer an obligation, just
//! useful.

use anyhow::{Context, Result};
use qcbridge_agent::blender::{Event, Lifecycle};
use qcbridge_agent::config::{self, Config, SharedConfig};
use qcbridge_agent::link::{Inbound, Link, serve_local, writer_thread};
use qcbridge_agent::session::{self, Ctx, HostTarget, PeerObserver};
use qcbridge_agent::video::{VideoSink, VideoSource, video_listener};
use serde_json::{Value, json};
use std::net::TcpListener;
use std::sync::atomic::{AtomicBool, Ordering::Relaxed};
use std::sync::{Arc, Mutex};
use tokio::sync::{Notify, mpsc};

pub const SOURCE_URL: &str = "https://github.com/cbkow/QCBridge";
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

const USAGE: &str = "\
qcbridge-agent [--config PATH] [--no-tray] [--role host|replica] [--listen IP:PORT]
               [--peer IP:PORT] [--token T] [--local-port N] [--exit-with-addon]
               [--version]";

/// Host-role observer: no Blender to manage. Pins the replica certificate
/// on first successful connect (trust on first use) and tells the addon.
struct HostObserver {
    status: Arc<Mutex<String>>,
    fingerprint: Mutex<Option<String>>,
    up: AtomicBool,
    link: Arc<Link>,
    cfg_path: std::path::PathBuf,
    cfg: SharedConfig,
    control: Arc<session::HostControl>,
}

impl PeerObserver for HostObserver {
    fn peer_up(&self, fp: Option<String>) {
        self.up.store(true, Relaxed);
        *self.fingerprint.lock().unwrap() = fp.clone();
        *self.status.lock().unwrap() = "replica connected".into();
        let mut pinned = false;
        if let Some(fp) = &fp {
            // Writes through the shared config, so the agent and the
            // lifecycle see the pin too — they used to hold stale copies.
            let saved = self.cfg.update(|cfg| {
                if !cfg.fingerprint.is_empty() {
                    return None;
                }
                cfg.fingerprint = fp.clone();
                Some(cfg.clone())
            });
            if let Some(cfg) = saved {
                if let Err(e) = config::save(&self.cfg_path, &cfg) {
                    eprintln!("[agent] could not persist fingerprint: {e}");
                }
                if let Some(t) = self.control.target.lock().unwrap().as_mut() {
                    t.fingerprint = hex::decode(fp).ok(); // enforce from now on, no reconnect
                }
                pinned = true;
                eprintln!("[agent] pinned replica certificate {fp}");
            }
        }
        self.link.event_try(json!({"event": "peer", "peer": 0, "up": true, "fingerprint": fp, "pinned": !pinned}));
    }
    fn peer_down(&self, reason: String) {
        self.up.store(false, Relaxed);
        *self.status.lock().unwrap() = if reason.is_empty() { "replica disconnected".into() } else { reason.clone() };
        self.link.event_try(json!({"event": "peer", "peer": 0, "up": false, "reason": reason}));
    }
    fn goodbye(&self) {}
}

struct Agent {
    cfg: SharedConfig,
    /// Directory this instance owns: config, cert, agent.json.
    base: std::path::PathBuf,
    /// The TOML itself. Only HostObserver held this before, so nothing in
    /// replica role could persist anything; and `base.join("agent.toml")`
    /// would be wrong under `--config other.toml`.
    cfg_path: std::path::PathBuf,
    host_control: Arc<session::HostControl>,
    link: Arc<Link>,
    inb: Arc<Inbound>,
    ctx: Arc<Ctx>,
    lifecycle: Option<Arc<Lifecycle>>,
    host_obs: Option<Arc<HostObserver>>,
    replica_fingerprint: String,
    local_port: u16,
    status: Arc<Mutex<String>>,
    quit: Arc<Notify>,
}

fn attach_reply(agent: &Agent) -> Value {
    let mut v = json!({
        "event": "attached",
        "role": agent.cfg.role(),
        "version": VERSION,
        "source": SOURCE_URL,
        "port": agent.cfg.with(|c| c.listen.rsplit(':').next().and_then(|p| p.parse::<u16>().ok()).unwrap_or(0)),
        "fingerprint": agent.replica_fingerprint,
        "peer_up": agent.lifecycle.as_ref().map(|l| l.peer_up.load(Relaxed))
            .or_else(|| agent.host_obs.as_ref().map(|h| h.up.load(Relaxed))).unwrap_or(false),
        "video_port": agent.cfg.with(|c| if c.role == "host" { c.video_port } else { 0 }),
        "video_state": *agent.ctx.video_src.state.lock().unwrap(),
        // The live settings, so the addon mirrors them instead of owning
        // its own copy. The addon already received `role` and `port` and
        // threw them away; now there is a reason to keep them.
        "config": agent.cfg.with(config::live_view),
    });
    if let Some(h) = &agent.host_obs {
        v["peer_fingerprint"] = json!(h.fingerprint.lock().unwrap().clone());
    }
    v
}

/// Every settings change ends here, whether it came from the addon
/// (`set_config`) or the tray, so the addon's panel never shows a stale
/// value. `req` is echoed when the command carried one: T_CMD has no reply
/// channel, so this event is the reply.
fn push_config_event(agent: &Agent, req: Option<Value>, applied: &config::Applied) {
    let mut v = json!({
        "event": "config",
        "config": agent.cfg.with(config::live_view),
        "changed": applied.changed,
        "needs_restart": applied.needs_restart,
        "rejected": applied.rejected,
    });
    if let Some(r) = req {
        v["req"] = r;
    }
    agent.link.event_try(v);
}

/// Point the host at `addr`, pinning `fp` if the agent has not already
/// pinned one itself. Shared by `connect` and a `set_config` that changes
/// `peer`.
fn retarget(agent: &Agent, addr: String, fp: String) {
    let pinned = agent.cfg.with(|c| c.fingerprint.clone());
    let fp = if !pinned.is_empty() { pinned } else { fp };
    let target = HostTarget {
        addr,
        fingerprint: (!fp.is_empty()).then(|| hex::decode(fp.to_lowercase().replace(':', "")).ok()).flatten(),
        mtu: session::DEFAULT_MTU,
    };
    if agent.host_control.target.lock().unwrap().as_ref() != Some(&target) {
        *agent.status.lock().unwrap() = format!("connecting to {}", target.addr);
        agent.host_control.set(Some(target));
    }
}

fn main() -> Result<()> {
    let args = qcbridge_agent::Args::parse(USAGE);
    if args.flag("version") {
        println!("qcbridge-agent {VERSION}\nsource: {SOURCE_URL}\nlicense: GPL-3.0-or-later");
        return Ok(());
    }
    let cfg_path = args.get("config").map(Into::into).unwrap_or_else(config::config_path);
    let mut cfg = config::load_or_create(&cfg_path)?;
    // Cert and agent.json live beside the config, so --config isolates an
    // instance completely (two agents in a test, or host+replica in dev).
    let base = config::base_dir(&cfg_path);
    if let Some(r) = args.get("role") { cfg.role = r.to_string(); }
    if let Some(l) = args.get("listen") { cfg.listen = l.to_string(); }
    if let Some(p) = args.get("peer") { cfg.peer = p.to_string(); }
    if let Some(t) = args.get("token") { cfg.token = t.to_string(); }
    if let Some(p) = args.get("local-port") { cfg.local_port = p.parse()?; }
    if args.flag("no-tray") { cfg.tray = false; }
    // A spawned agent belongs to whoever spawned it. Without this it
    // outlives a Blender that was killed rather than closed, keeps its UDP
    // port, and the next run dies with "Address already in use" — which is
    // exactly what happened to the first bootstrap bench.
    let exit_with_addon = args.flag("exit-with-addon");
    let role_host = cfg.role == "host";
    eprintln!("[agent] {VERSION} role={} config={}", cfg.role, cfg_path.display());

    rustls::crypto::ring::default_provider()
        .install_default()
        .map_err(|_| anyhow::anyhow!("a rustls crypto provider was already installed"))?;

    // Addon link + queues.
    let (out_tx, out_rx) = mpsc::channel(256);
    let link = Arc::new(Link::new(out_tx));
    {
        let link = link.clone();
        std::thread::Builder::new().name("link-writer".into()).spawn(move || writer_thread(link, out_rx))?;
    }
    let (control_tx, control_rx) = mpsc::channel(1024);
    let (cold_tx, cold_rx) = mpsc::channel(256);
    let inb = Arc::new(Inbound {
        control_tx, cold_tx, hot: Mutex::new(Default::default()), hot_notify: Notify::new(),
        connected: AtomicBool::new(false),
    });
    let status = Arc::new(Mutex::new("starting".to_string()));

    // Local socket the addon attaches to.
    let local = TcpListener::bind(("127.0.0.1", cfg.local_port)).context("bind local socket")?;
    let local_port = local.local_addr()?.port();
    let secret: String = {
        use rand::RngCore;
        let mut b = [0u8; 16];
        rand::thread_rng().fill_bytes(&mut b);
        hex::encode(b)
    };
    // Refuse to be the second agent of this role in this directory. A live
    // pid in agent.json means one is already running; a dead one (a crash,
    // a kill -9) is simply overwritten, as before.
    if let Some(pid) = config::registered_live_pid(&base, &cfg.role) {
        anyhow::bail!(
            "another {} agent (pid {pid}) is already registered in {} — quit it \
             first, or point --config at a different directory",
            cfg.role, base.display()
        );
    }
    config::write_socket_info(&base, &cfg.role, local_port, &secret)?;

    let runtime = tokio::runtime::Builder::new_multi_thread().worker_threads(4).enable_all().build()?;
    let video_src = Arc::new(VideoSource::new());
    let video_sink = Arc::new(VideoSink::new());

    // One config from here on. Everything long-lived takes a handle to it,
    // so a change is visible to all of them; `cfg` itself stays only for the
    // startup reads below, which happen before any of this can be edited.
    let shared = SharedConfig::new(cfg.clone());

    // Role wiring.
    let mut replica_fingerprint = String::new();
    let mut lifecycle: Option<Arc<Lifecycle>> = None;
    let mut host_obs: Option<Arc<HostObserver>> = None;
    let host_control = Arc::new(session::HostControl::new(None));
    let observer: Arc<dyn PeerObserver> = if role_host {
        let h = Arc::new(HostObserver {
            status: status.clone(), fingerprint: Mutex::new(None), up: AtomicBool::new(false),
            link: link.clone(), cfg_path: cfg_path.clone(), cfg: shared.clone(),
            control: host_control.clone(),
        });
        host_obs = Some(h.clone());
        h
    } else {
        let (l, rx) = Lifecycle::new(shared.clone(), link.clone(), local_port, secret.clone(), status.clone());
        runtime.spawn(l.clone().run(rx));
        lifecycle = Some(l.clone());
        l
    };
    let ctx = Arc::new(Ctx {
        role_host,
        token: cfg.token.clone(),
        link: link.clone(),
        inb: inb.clone(),
        control_rx: tokio::sync::Mutex::new(control_rx),
        cold_rx: tokio::sync::Mutex::new(cold_rx),
        video_src: video_src.clone(),
        video_sink: video_sink.clone(),
        observer,
        last_stats: Mutex::new(Value::Null),
    });

    if role_host {
        // A configured peer connects at once; otherwise the addon sends
        // CMD connect with the address from its preferences.
        if !cfg.peer.is_empty() {
            host_control.set(Some(HostTarget {
                addr: cfg.peer.clone(),
                fingerprint: (!cfg.fingerprint.is_empty())
                    .then(|| hex::decode(cfg.fingerprint.to_lowercase().replace(':', "")))
                    .transpose()
                    .context("config fingerprint must be hex")?,
                mtu: session::DEFAULT_MTU,
            }));
            *status.lock().unwrap() = format!("connecting to {}", cfg.peer);
        } else {
            *status.lock().unwrap() = "waiting for the addon".into();
        }
        if cfg.video_port > 0 {
            let addr = format!("127.0.0.1:{}", cfg.video_port).parse()?;
            runtime.spawn(video_listener(addr, video_sink.clone(), link.clone()));
        }
        runtime.spawn(session::host_loop(ctx.clone(), host_control.clone()));
    } else {
        let addr = cfg.listen.parse().context("listen must be IP:PORT")?;
        // Quinn binds its socket inside a Tokio context.
        let listener = {
            let _guard = runtime.enter();
            session::listen(addr, &config::cert_dir(&base), session::DEFAULT_MTU)?
        };
        replica_fingerprint = listener.fingerprint.clone();
        eprintln!("[agent] listening on {} fingerprint {}", cfg.listen, replica_fingerprint);
        *status.lock().unwrap() = "listening".into();
        let ctx = ctx.clone();
        runtime.spawn(async move {
            loop {
                if let Err(e) = session::serve_one(ctx.clone(), &listener.endpoint).await {
                    eprintln!("[agent] session ended: {e:#}");
                }
            }
        });
    }

    let agent = Arc::new(Agent {
        cfg: shared.clone(), base: base.clone(), cfg_path: cfg_path.clone(),
        host_control: host_control.clone(), link: link.clone(),
        inb: inb.clone(), ctx: ctx.clone(),
        lifecycle: lifecycle.clone(), host_obs, replica_fingerprint, local_port, status: status.clone(),
        quit: Arc::new(Notify::new()),
    });

    // Local socket server.
    {
        let a = agent.clone();
        let on_attach: Arc<dyn Fn() -> Value + Send + Sync> = Arc::new(move || attach_reply(&a));
        let a = agent.clone();
        let on_detach: Arc<dyn Fn() + Send + Sync> = Arc::new(move || {
            if let Some(l) = &a.lifecycle { let _ = l.tx.send(Event::AddonDetached); }
            if exit_with_addon {
                eprintln!("[agent] addon detached; exiting (--exit-with-addon)");
                a.ctx.video_src.stop();
                a.quit.notify_waiters();
            }
        });
        let a = agent.clone();
        let on_cmd: Arc<dyn Fn(&Value) + Send + Sync> = Arc::new(move |cmd| handle_cmd(&a, cmd));
        let (inb, link) = (inb.clone(), link.clone());
        std::thread::Builder::new()
            .name("local-socket".into())
            .spawn(move || serve_local(local, secret, inb, link, on_attach, on_detach, on_cmd))?;
    }
    eprintln!("[agent] addon socket 127.0.0.1:{local_port}");

    if cfg.tray {
        tray::run(agent, runtime)
    } else {
        runtime.block_on(agent.quit.notified());
        if let Some(l) = &agent.lifecycle { let _ = l.tx.send(Event::Shutdown); }
        // Deregister on the way out, so the next agent in this directory
        // does not read a dead port back out of agent.json.
        config::remove_socket_info(&agent.base, &agent.cfg.role());
        Ok(())
    }
}

/// argv for the native capture child if one is installed beside the agent.
fn native_capture_argv(cmd: &Value, cfg: &Config) -> Option<Vec<String>> {
    let exe = std::env::current_exe().ok()?;
    let name = if cfg!(target_os = "macos") { "qcb-capture-mac" } else if cfg!(windows) { "qcb-capture-win.exe" } else { return None };
    let path = exe.parent()?.join(name);
    if !path.is_file() {
        return None;
    }
    let fps = cmd.get("fps").and_then(Value::as_u64).unwrap_or(60);
    let mbps = cmd.get("bitrate_mbps").and_then(Value::as_u64).unwrap_or(50);
    let mut argv = vec![path.to_string_lossy().into_owned(), "--fps".into(), fps.to_string(), "--bitrate".into(), mbps.to_string()];
    if cfg.capture_scale > 0.0 && cfg.capture_scale != 1.0 {
        argv.push("--scale".into());
        argv.push(cfg.capture_scale.to_string());
    }
    if let Some(r) = cmd.get("region").and_then(Value::as_str) {
        argv.push("--region".into());
        argv.push(r.to_string());
    }
    if cmd.get("ten_bit").and_then(Value::as_bool) == Some(true) {
        argv.push("--10bit".into());
    }
    Some(argv)
}

fn handle_cmd(agent: &Agent, cmd: &Value) {
    match cmd.get("cmd").and_then(Value::as_str) {
        Some("video_start") => {
            let mut argv: Vec<String> = cmd.get("argv").and_then(Value::as_array)
                .map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect())
                .unwrap_or_default();
            // S6: prefer the native capture binary shipped next to the agent
            // (ScreenCaptureKit -> VideoToolbox) over the addon's ffmpeg argv.
            if cmd.get("native").and_then(Value::as_bool) != Some(false) {
                if let Some(native) = agent.cfg.with(|c| native_capture_argv(cmd, c)) {
                    argv = native;
                }
            }
            if argv.is_empty() {
                agent.link.event_blocking(json!({"event": "error", "msg": "video_start: empty argv"}));
            } else {
                agent.ctx.video_src.start(argv, agent.link.clone());
            }
        }
        Some("video_stop") => agent.ctx.video_src.stop(),
        Some("connect") if agent.cfg.is_host() => {
            let addr = cmd.get("peer").and_then(Value::as_str).unwrap_or("").to_string();
            if addr.is_empty() {
                return;
            }
            // The agent's pin wins over whatever the addon remembers.
            let fp = cmd.get("fingerprint").and_then(Value::as_str).unwrap_or("").to_string();
            retarget(agent, addr, fp);
        }
        Some("disconnect") if agent.cfg.is_host() => agent.host_control.set(None),
        Some("set_config") => {
            // Live fields are applied and persisted; restart-only ones are
            // named back rather than silently ignored. Same read-modify-write
            // shape forget_fingerprint used, so the snapshot saved is the one
            // the lock protected.
            let patch = cmd.get("set").cloned().unwrap_or(Value::Null);
            let (applied, snapshot) = agent.cfg.update(|c| {
                let a = config::apply_live(c, &patch);
                (a, c.clone())
            });
            if !applied.changed.is_empty() {
                if let Err(e) = config::save(&agent.cfg_path, &snapshot) {
                    eprintln!("[agent] could not persist config: {e}");
                }
                if applied.changed.iter().any(|f| f == "peer") && agent.cfg.is_host() {
                    retarget(agent, snapshot.peer.clone(), snapshot.fingerprint.clone());
                }
                // Phase C: a changed `discovery` or `phonebook` starts or
                // stops the beacon here.
            }
            push_config_event(agent, cmd.get("req").cloned(), &applied);
        }
        Some("forget_fingerprint") if agent.cfg.is_host() => {
            let snapshot = agent.cfg.update(|c| {
                c.fingerprint.clear();
                c.clone()
            });
            if let Err(e) = config::save(&agent.cfg_path, &snapshot) {
                eprintln!("[agent] could not persist config: {e}");
            }
            // It used to persist and tell nobody.
            let applied = config::Applied { changed: vec!["fingerprint".into()], ..Default::default() };
            push_config_event(agent, cmd.get("req").cloned(), &applied);
        }
        Some("detach") => {}
        Some("shutdown") => {
            // The addon asking us to exit: only meaningful for headless test agents.
            if !agent.cfg.with(|c| c.tray) {
                agent.ctx.video_src.stop();
                agent.quit.notify_waiters();
            }
        }
        other => eprintln!("[agent] unknown cmd {other:?}"),
    }
}

mod tray {
    use super::*;
    use muda::{CheckMenuItem, Menu, MenuEvent, MenuItem, PredefinedMenuItem, Submenu};
    use tao::event::Event as TaoEvent;
    use tao::event_loop::{ControlFlow, EventLoopBuilder};
    use tray_icon::{Icon, TrayIconBuilder};

    fn icon() -> Icon {
        // A filled circle; the real bundle will ship a proper asset.
        let (w, h) = (32u32, 32u32);
        let mut rgba = vec![0u8; (w * h * 4) as usize];
        for y in 0..h {
            for x in 0..w {
                let dx = x as f32 - 15.5;
                let dy = y as f32 - 15.5;
                let inside = (dx * dx + dy * dy).sqrt() < 12.0;
                let i = ((y * w + x) * 4) as usize;
                rgba[i..i + 4].copy_from_slice(if inside { &[40, 200, 120, 255] } else { &[0, 0, 0, 0] });
            }
        }
        Icon::from_rgba(rgba, w, h).expect("icon")
    }

    pub fn run(agent: Arc<Agent>, runtime: tokio::runtime::Runtime) -> Result<()> {
        let event_loop = EventLoopBuilder::new().build();
        let menu = Menu::new();
        let status_item = MenuItem::new("starting", false, None);
        let info_item = MenuItem::new(
            agent.cfg.with(|c| if c.role == "host" { format!("host → {}", c.peer) } else { format!("replica · listening {}", c.listen) }),
            false, None,
        );
        let fp_item = MenuItem::new(
            if agent.replica_fingerprint.is_empty() { "fingerprint: (host role)".to_string() } else { format!("fingerprint {}…", &agent.replica_fingerprint[..16]) },
            false, None,
        );
        let start_item = MenuItem::new("Launch Blender now", !agent.cfg.is_host(), None);
        let stop_item = MenuItem::new("Close Blender", !agent.cfg.is_host(), None);
        let config_item = MenuItem::new("Open config folder", true, None);
        let quit_item = MenuItem::new("Quit QCBridge Agent", true, None);

        // Network: a three-way radio built from check items with explicit
        // ids. muda flips a check item BEFORE it sends the event (both the
        // macOS and Windows backends), so the handler never asks
        // is_checked() what was picked — it goes by id, then sets all three
        // from the config that resulted.
        let mode0 = agent.cfg.with(|c| c.discovery.clone());
        let net_off = CheckMenuItem::with_id("net.off", "Off — no connections", true, mode0 == "off", None);
        let net_direct = CheckMenuItem::with_id("net.direct", "Direct only — reachable by address", true, mode0 == "direct", None);
        let net_discover = CheckMenuItem::with_id("net.discover", "Discoverable — also announce on the LAN", true, mode0 == "discoverable", None);
        let network = Submenu::with_items("Network", true, &[&net_off, &net_direct, &net_discover])?;

        menu.append_items(&[
            &status_item, &info_item, &fp_item, &PredefinedMenuItem::separator(),
            &network, &PredefinedMenuItem::separator(),
            &start_item, &stop_item, &PredefinedMenuItem::separator(),
            &config_item, &quit_item,
        ])?;
        let info_text = {
            let cfg = agent.cfg.clone();
            move || cfg.with(|c| if c.role == "host" { format!("host → {}", c.peer) } else { format!("replica · listening {}", c.listen) })
        };
        let tooltip_text = {
            let cfg = agent.cfg.clone();
            move || cfg.with(|c| format!("QCBridge Agent — {} · {}", c.display_name(), c.discovery))
        };
        let mut tray: Option<tray_icon::TrayIcon> = None;
        let runtime = std::sync::Mutex::new(Some(runtime));
        let rx = MenuEvent::receiver();
        let mut last_status = String::new();
        let mut last_info = String::new();
        let mut last_tooltip = String::new();
        let mut last_mode = mode0;
        event_loop.run(move |event, _, control_flow| {
            *control_flow = ControlFlow::WaitUntil(std::time::Instant::now() + std::time::Duration::from_millis(500));
            if let TaoEvent::NewEvents(tao::event::StartCause::Init) = event {
                tray = Some(
                    TrayIconBuilder::new()
                        .with_menu(Box::new(menu.clone()))
                        .with_tooltip("QCBridge Agent")
                        .with_icon(icon())
                        .build()
                        .expect("tray icon"),
                );
            }
            let s = agent.status.lock().unwrap().clone();
            if s != last_status {
                status_item.set_text(&s);
                last_status = s;
            }
            // These were built once and went stale; a set_config from the
            // addon can change any of them under us, so refresh on the tick.
            let info = info_text();
            if info != last_info {
                info_item.set_text(&info);
                last_info = info;
            }
            let mode = agent.cfg.with(|c| c.discovery.clone());
            if mode != last_mode {
                net_off.set_checked(mode == "off");
                net_direct.set_checked(mode == "direct");
                net_discover.set_checked(mode == "discoverable");
                last_mode = mode;
            }
            let tip = tooltip_text();
            if tip != last_tooltip {
                if let Some(t) = tray.as_ref() { let _ = t.set_tooltip(Some(tip.as_str())); }
                last_tooltip = tip;
            }
            while let Ok(ev) = rx.try_recv() {
                let picked = if ev.id == "net.off" { Some("off") }
                    else if ev.id == "net.direct" { Some("direct") }
                    else if ev.id == "net.discover" { Some("discoverable") }
                    else { None };
                if let Some(m) = picked {
                    // Same path as a set_config from the addon: apply,
                    // persist, tell the addon. The tick above then sets the
                    // three checks from the config that resulted, which is
                    // what undoes muda's premature toggle.
                    let (applied, snapshot) = agent.cfg.update(|c| {
                        let a = config::apply_live(c, &json!({"discovery": m}));
                        (a, c.clone())
                    });
                    if !applied.changed.is_empty() {
                        if let Err(e) = config::save(&agent.cfg_path, &snapshot) {
                            eprintln!("[agent] could not persist config: {e}");
                        }
                        // Phase C: start/stop the beacon here.
                    }
                    push_config_event(&agent, None, &applied);
                    last_mode.clear(); // force the check refresh next tick
                    continue;
                }
                if ev.id == start_item.id() {
                    if let Some(l) = &agent.lifecycle { let _ = l.tx.send(Event::ManualStart); }
                } else if ev.id == stop_item.id() {
                    if let Some(l) = &agent.lifecycle { let _ = l.tx.send(Event::ManualStop); }
                } else if ev.id == config_item.id() {
                    let dir = agent.base.clone();
                    #[cfg(target_os = "macos")]
                    let _ = std::process::Command::new("open").arg(&dir).spawn();
                    #[cfg(windows)]
                    let _ = std::process::Command::new("explorer").arg(&dir).spawn();
                } else if ev.id == quit_item.id() {
                    if let Some(l) = &agent.lifecycle { let _ = l.tx.send(Event::Shutdown); }
                    agent.ctx.video_src.stop();
                    config::remove_socket_info(&agent.base, &agent.cfg.role());
                    if let Some(rt) = runtime.lock().unwrap().take() {
                        rt.shutdown_timeout(std::time::Duration::from_millis(500));
                    }
                    tray = None; // drop the icon before exiting
                    *control_flow = ControlFlow::Exit;
                }
            }
        });
    }
}
