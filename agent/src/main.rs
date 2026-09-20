//! QCBridge Agent — tray app owning the Kyber connection, the capture child
//! and (replica) Blender's lifecycle. See spikes/parity/PLAN.md "The QCBridge
//! Agent". Config: <config dir>/QCBridge/agent.toml; the addon finds the
//! local socket via agent.json next to it.
//!
//! AGPL-3.0-or-later (links Kyber). Source: SOURCE_URL below — reported in
//! `--version` and in every attach reply, per AGPL §13.

use anyhow::{Context, Result};
use qcbridge_agent::blender::{Event, Lifecycle};
use qcbridge_agent::config::{self, Config};
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
               [--peer IP:PORT] [--token T] [--local-port N] [--version]";

/// Host-role observer: no Blender to manage. Pins the replica certificate
/// on first successful connect (trust on first use) and tells the addon.
struct HostObserver {
    status: Arc<Mutex<String>>,
    fingerprint: Mutex<Option<String>>,
    up: AtomicBool,
    link: Arc<Link>,
    cfg_path: std::path::PathBuf,
    cfg: Mutex<Config>,
    control: Arc<session::HostControl>,
}

impl PeerObserver for HostObserver {
    fn peer_up(&self, fp: Option<String>) {
        self.up.store(true, Relaxed);
        *self.fingerprint.lock().unwrap() = fp.clone();
        *self.status.lock().unwrap() = "replica connected".into();
        let mut pinned = false;
        if let Some(fp) = &fp {
            let mut cfg = self.cfg.lock().unwrap();
            if cfg.fingerprint.is_empty() {
                cfg.fingerprint = fp.clone();
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
    cfg: Config,
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
        "role": agent.cfg.role,
        "version": VERSION,
        "source": SOURCE_URL,
        "port": agent.cfg.listen.rsplit(':').next().and_then(|p| p.parse::<u16>().ok()).unwrap_or(0),
        "fingerprint": agent.replica_fingerprint,
        "peer_up": agent.lifecycle.as_ref().map(|l| l.peer_up.load(Relaxed))
            .or_else(|| agent.host_obs.as_ref().map(|h| h.up.load(Relaxed))).unwrap_or(false),
        "video_port": if agent.cfg.role == "host" { agent.cfg.video_port } else { 0 },
        "video_state": *agent.ctx.video_src.state.lock().unwrap(),
    });
    if let Some(h) = &agent.host_obs {
        v["peer_fingerprint"] = json!(h.fingerprint.lock().unwrap().clone());
    }
    v
}

fn main() -> Result<()> {
    let args = qcbridge_agent::Args::parse(USAGE);
    if args.flag("version") {
        println!("qcbridge-agent {VERSION}\nsource: {SOURCE_URL}\nlicense: AGPL-3.0-or-later (links Kyber, LicenseRef-Kyber-Commercial OR AGPL-3.0-or-later)");
        return Ok(());
    }
    let cfg_path = args.get("config").map(Into::into).unwrap_or_else(config::config_path);
    let mut cfg = config::load_or_create(&cfg_path)?;
    if let Some(r) = args.get("role") { cfg.role = r.to_string(); }
    if let Some(l) = args.get("listen") { cfg.listen = l.to_string(); }
    if let Some(p) = args.get("peer") { cfg.peer = p.to_string(); }
    if let Some(t) = args.get("token") { cfg.token = t.to_string(); }
    if let Some(p) = args.get("local-port") { cfg.local_port = p.parse()?; }
    if args.flag("no-tray") { cfg.tray = false; }
    let role_host = cfg.role == "host";
    eprintln!("[agent] {VERSION} role={} config={}", cfg.role, cfg_path.display());

    kynet::init_crypto();

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
    config::write_socket_info(&cfg.role, local_port, &secret)?;

    let runtime = tokio::runtime::Builder::new_multi_thread().worker_threads(4).enable_all().build()?;
    let video_src = Arc::new(VideoSource::new());
    let video_sink = Arc::new(VideoSink::new());

    // Role wiring.
    let mut replica_fingerprint = String::new();
    let mut lifecycle: Option<Arc<Lifecycle>> = None;
    let mut host_obs: Option<Arc<HostObserver>> = None;
    let host_control = Arc::new(session::HostControl::new(None));
    let observer: Arc<dyn PeerObserver> = if role_host {
        let h = Arc::new(HostObserver {
            status: status.clone(), fingerprint: Mutex::new(None), up: AtomicBool::new(false),
            link: link.clone(), cfg_path: cfg_path.clone(), cfg: Mutex::new(cfg.clone()),
            control: host_control.clone(),
        });
        host_obs = Some(h.clone());
        h
    } else {
        let (l, rx) = Lifecycle::new(cfg.clone(), link.clone(), local_port, secret.clone(), status.clone());
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
            session::listen(addr, &config::cert_dir(), cfg.cap_mbps, session::DEFAULT_MTU)?
        };
        replica_fingerprint = listener.fingerprint.clone();
        eprintln!("[agent] listening on {} fingerprint {}", cfg.listen, replica_fingerprint);
        *status.lock().unwrap() = "listening".into();
        let ctx = ctx.clone();
        runtime.spawn(async move {
            loop {
                if let Err(e) = session::serve_one(ctx.clone(), &listener.server).await {
                    eprintln!("[agent] session ended: {e:#}");
                }
            }
        });
    }

    let agent = Arc::new(Agent {
        cfg: cfg.clone(), host_control: host_control.clone(), link: link.clone(), inb: inb.clone(), ctx: ctx.clone(),
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
                if let Some(native) = native_capture_argv(cmd, &agent.cfg) {
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
        Some("connect") if agent.cfg.role == "host" => {
            let addr = cmd.get("peer").and_then(Value::as_str).unwrap_or("").to_string();
            if addr.is_empty() {
                return;
            }
            // The agent's pin wins over whatever the addon remembers.
            let pinned = agent.host_obs.as_ref().map(|h| h.cfg.lock().unwrap().fingerprint.clone()).unwrap_or_default();
            let fp = if !pinned.is_empty() { pinned } else {
                cmd.get("fingerprint").and_then(Value::as_str).unwrap_or("").to_string()
            };
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
        Some("disconnect") if agent.cfg.role == "host" => agent.host_control.set(None),
        Some("forget_fingerprint") if agent.cfg.role == "host" => {
            if let Some(h) = &agent.host_obs {
                let mut cfg = h.cfg.lock().unwrap();
                cfg.fingerprint.clear();
                let _ = config::save(&h.cfg_path, &cfg);
            }
        }
        Some("detach") => {}
        Some("shutdown") => {
            // The addon asking us to exit: only meaningful for headless test agents.
            if !agent.cfg.tray {
                agent.ctx.video_src.stop();
                agent.quit.notify_waiters();
            }
        }
        other => eprintln!("[agent] unknown cmd {other:?}"),
    }
}

mod tray {
    use super::*;
    use muda::{Menu, MenuEvent, MenuItem, PredefinedMenuItem};
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
            if agent.cfg.role == "host" { format!("host → {}", agent.cfg.peer) } else { format!("replica · listening {}", agent.cfg.listen) },
            false, None,
        );
        let fp_item = MenuItem::new(
            if agent.replica_fingerprint.is_empty() { "fingerprint: (host role)".to_string() } else { format!("fingerprint {}…", &agent.replica_fingerprint[..16]) },
            false, None,
        );
        let start_item = MenuItem::new("Launch Blender now", agent.cfg.role != "host", None);
        let stop_item = MenuItem::new("Close Blender", agent.cfg.role != "host", None);
        let config_item = MenuItem::new("Open config folder", true, None);
        let quit_item = MenuItem::new("Quit QCBridge Agent", true, None);
        menu.append_items(&[
            &status_item, &info_item, &fp_item, &PredefinedMenuItem::separator(),
            &start_item, &stop_item, &PredefinedMenuItem::separator(),
            &config_item, &quit_item,
        ])?;
        let mut tray: Option<tray_icon::TrayIcon> = None;
        let runtime = std::sync::Mutex::new(Some(runtime));
        let rx = MenuEvent::receiver();
        let mut last_status = String::new();
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
            let _ = tray.as_ref(); // keeps the icon alive for the loop's lifetime
            let s = agent.status.lock().unwrap().clone();
            if s != last_status {
                status_item.set_text(&s);
                last_status = s;
            }
            while let Ok(ev) = rx.try_recv() {
                if ev.id == start_item.id() {
                    if let Some(l) = &agent.lifecycle { let _ = l.tx.send(Event::ManualStart); }
                } else if ev.id == stop_item.id() {
                    if let Some(l) = &agent.lifecycle { let _ = l.tx.send(Event::ManualStop); }
                } else if ev.id == config_item.id() {
                    let dir = config::config_dir();
                    #[cfg(target_os = "macos")]
                    let _ = std::process::Command::new("open").arg(&dir).spawn();
                    #[cfg(windows)]
                    let _ = std::process::Command::new("explorer").arg(&dir).spawn();
                } else if ev.id == quit_item.id() {
                    if let Some(l) = &agent.lifecycle { let _ = l.tx.send(Event::Shutdown); }
                    agent.ctx.video_src.stop();
                    config::remove_socket_info(&agent.cfg.role);
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
