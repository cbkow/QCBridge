//! QCBridge Agent — tray app owning the QUIC connection, the capture child
//! and (replica) Blender's lifecycle (design: DESIGN-NOTES agent). Config: <config dir>/QCBridge/agent.toml; the addon finds the
//! local socket via agent.json next to it.
//!
//! GPL-3.0-or-later, same as the repository. SOURCE_URL is still reported by
//! `--version` and in every attach reply — no longer an obligation, just
//! useful.

// Release builds on Windows are GUI-subsystem executables: no console, so
// nothing can close one or send it a control event (the logon-task death,
// 0xC000013A). Output goes to agent.log beside agent.toml; a terminal that
// runs --version gets its output back through platform::attach_parent_console.
#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]

use anyhow::{Context, Result};
use qcbridge_agent::blender::{Event, Lifecycle};
use qcbridge_agent::config::{self, Config, SharedConfig};
use qcbridge_agent::discovery;
use qcbridge_agent::link::{Inbound, Link, serve_local, writer_thread};
use qcbridge_agent::platform;
use qcbridge_agent::session::{self, Ctx, HostTarget, PeerObserver};
use qcbridge_agent::log;
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
               [--settings] [--version]";

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
                    log!("[agent] could not persist fingerprint: {e}");
                }
                if let Some(t) = self.control.target.lock().unwrap().as_mut() {
                    t.fingerprint = hex::decode(fp).ok(); // enforce from now on, no reconnect
                }
                pinned = true;
                log!("[agent] pinned replica certificate {fp}");
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
    /// handle_cmd runs on the socket thread and is sync; anything that must
    /// await (a discovery sweep) is spawned here.
    rt: tokio::runtime::Handle,
    host_control: Arc<session::HostControl>,
    link: Arc<Link>,
    inb: Arc<Inbound>,
    ctx: Arc<Ctx>,
    /// Everything that exists only for the current role; rebuilt by the
    /// Send/Receive switch (`switch_role`) without a restart.
    role: Mutex<RoleRuntime>,
    local_port: u16,
    /// The per-start secret the addon presents; a replica's Blender gets it
    /// in its environment.
    secret: String,
    status: Arc<Mutex<String>>,
    quit: Arc<Notify>,
}

/// What a role owns: a replica its listener, accept loop, Blender
/// lifecycle and beacon; a host its observer and dial loop. `stop_role`
/// takes it all down; `start_role` builds it for the role in the config.
#[derive(Default)]
struct RoleRuntime {
    lifecycle: Option<Arc<Lifecycle>>,
    host_obs: Option<Arc<HostObserver>>,
    replica_fingerprint: String,
    endpoint: Option<quinn::Endpoint>,
    tasks: Vec<tokio::task::JoinHandle<()>>,
    beacon: Option<discovery::Service>,
}

impl RoleRuntime {
    fn peer_up(&self) -> bool {
        self.lifecycle.as_ref().map(|l| l.peer_up.load(Relaxed))
            .or_else(|| self.host_obs.as_ref().map(|h| h.up.load(Relaxed)))
            .unwrap_or(false)
    }
    /// Replica: the host is in a session. Host: its own addon is attached
    /// (which is what it tells the replica).
    fn session_on(&self) -> bool {
        self.lifecycle.as_ref().map(|l| l.session_on.load(Relaxed))
            .or_else(|| self.host_obs.as_ref().map(|h| h.link.attached.load(Relaxed)))
            .unwrap_or(false)
    }
}

/// Build the runtime for the role in the config. Registers the local socket
/// under that role in agent.json, so the addon of the new role finds it.
fn start_role(agent: &Agent) -> Result<RoleRuntime> {
    let cfg = agent.cfg.snapshot();
    let role_host = cfg.role == "host";
    let mut rt = RoleRuntime::default();
    let observer: Arc<dyn PeerObserver> = if role_host {
        let h = Arc::new(HostObserver {
            status: agent.status.clone(), fingerprint: Mutex::new(None), up: AtomicBool::new(false),
            link: agent.link.clone(), cfg_path: agent.cfg_path.clone(), cfg: agent.cfg.clone(),
            control: agent.host_control.clone(),
        });
        rt.host_obs = Some(h.clone());
        h
    } else {
        let (l, rx) = Lifecycle::new(
            agent.cfg.clone(), agent.link.clone(), agent.local_port, agent.secret.clone(), agent.base.clone(), agent.status.clone(),
        );
        rt.tasks.push(agent.rt.spawn(l.clone().run(rx)));
        rt.lifecycle = Some(l.clone());
        l
    };
    agent.ctx.set_role(role_host, observer);

    if role_host {
        // A configured peer connects at once; otherwise the addon sends
        // CMD connect with the address from its preferences.
        if !cfg.peer.is_empty() {
            agent.host_control.set(Some(HostTarget {
                addr: cfg.peer.clone(),
                fingerprint: (!cfg.fingerprint.is_empty())
                    .then(|| hex::decode(cfg.fingerprint.to_lowercase().replace(':', "")))
                    .transpose()
                    .context("config fingerprint must be hex")?,
                mtu: session::DEFAULT_MTU,
            }));
            *agent.status.lock().unwrap() = format!("connecting to {}", cfg.peer);
        } else {
            *agent.status.lock().unwrap() = "waiting for the addon".into();
        }
        if cfg.video_port > 0 {
            let addr = format!("127.0.0.1:{}", cfg.video_port).parse()?;
            rt.tasks.push(agent.rt.spawn(video_listener(addr, agent.ctx.video_sink.clone(), agent.link.clone())));
        }
        rt.tasks.push(agent.rt.spawn(session::host_loop(agent.ctx.clone(), agent.host_control.clone())));
    } else {
        let addr = cfg.listen.parse().context("listen must be IP:PORT")?;
        // Quinn binds its socket inside a Tokio context.
        let listener = {
            let _guard = agent.rt.enter();
            session::listen(addr, &config::cert_dir(&agent.base), session::DEFAULT_MTU)
                .with_context(|| format!("listen on {}", cfg.listen))?
        };
        rt.replica_fingerprint = listener.fingerprint.clone();
        rt.endpoint = Some(listener.endpoint.clone());
        log!("[agent] listening on {} fingerprint {}", cfg.listen, rt.replica_fingerprint);
        *agent.status.lock().unwrap() = "listening".into();
        let ctx = agent.ctx.clone();
        rt.tasks.push(agent.rt.spawn(async move {
            loop {
                match session::serve_one(ctx.clone(), &listener.endpoint).await {
                    Ok(()) => {}
                    Err(e) => {
                        if listener.endpoint.local_addr().is_err() { return; }
                        log!("[agent] session ended: {e:#}");
                        // A closed endpoint yields errors without waiting;
                        // one poll per loop is enough to notice the abort.
                        tokio::task::yield_now().await;
                    }
                }
            }
        }));
    }

    // The beacon: a host's service returns at once (hosts never bind 4246).
    let paired: Arc<dyn Fn() -> bool + Send + Sync> = {
        let l = rt.lifecycle.clone();
        let h = rt.host_obs.clone();
        Arc::new(move || {
            l.as_ref().map(|l| l.peer_up.load(Relaxed))
                .or_else(|| h.as_ref().map(|h| h.up.load(Relaxed)))
                .unwrap_or(false)
        })
    };
    let listen_port = if role_host { 0 } else {
        cfg.listen.rsplit(':').next().and_then(|p| p.parse::<u16>().ok()).unwrap_or(0)
    };
    rt.beacon = Some(discovery::Service::start(&agent.rt, agent.cfg.clone(), Arc::new(discovery::Identity {
        role: cfg.role.clone(),
        listen_port,
        fingerprint: rt.replica_fingerprint.clone(),
        version: VERSION.to_string(),
        paired,
    })));
    config::write_socket_info(&agent.base, &cfg.role, agent.local_port, &agent.secret)?;
    Ok(rt)
}

/// Take a role runtime down: Blender asked to quit, the dial loop idled,
/// the listener closed, the beacon gone (bye sent, phonebook entry
/// removed), the tasks aborted.
fn stop_role(agent: &Agent, rt: RoleRuntime, old_role: &str) {
    if let Some(l) = &rt.lifecycle {
        let _ = l.tx.send(Event::Shutdown);
    }
    agent.host_control.set(None);
    if let Some(b) = &rt.beacon {
        b.set_mode("off");
    }
    agent.ctx.set_role(agent.ctx.is_host(), Arc::new(session::NoObserver));
    // Give the beacon its "off" turn (bye + phonebook removal) before the
    // tasks are cut; the accept loop goes before its endpoint closes, or
    // it logs the close as a session error.
    std::thread::sleep(std::time::Duration::from_millis(150));
    for t in &rt.tasks {
        t.abort();
    }
    if let Some(ep) = &rt.endpoint {
        ep.close(0u32.into(), b"role change");
    }
    drop(rt);
    config::remove_socket_info(&agent.base, old_role);
}

/// The Send/Receive switch, and a replica's new listen address: rebuild
/// the role runtime for the config as it now stands. On failure the old
/// role is restored in the config and rebuilt, and the error goes to the
/// log and the listeners.
fn switch_role(agent: &Agent, old_role: &str) {
    let old = std::mem::take(&mut *agent.role.lock().unwrap());
    stop_role(agent, old, old_role);
    let new_role = agent.cfg.role();
    match start_role(agent) {
        Ok(rt) => {
            *agent.role.lock().unwrap() = rt;
            log!("[agent] now {new_role}");
        }
        Err(e) => {
            log!("[agent] could not become {new_role}: {e:#}; staying {old_role}");
            agent.link.event_try(json!({"event": "error", "msg": format!("could not become {new_role}: {e:#}")}));
            let snapshot = agent.cfg.update(|c| { c.role = old_role.to_string(); c.clone() });
            let _ = config::save(&agent.cfg_path, &snapshot);
            match start_role(agent) {
                Ok(rt) => *agent.role.lock().unwrap() = rt,
                Err(e2) => {
                    log!("[agent] could not restore {old_role} either: {e2:#}");
                    *agent.status.lock().unwrap() = format!("no role: {e2:#}");
                }
            }
        }
    }
}

fn attach_reply(agent: &Agent) -> Value {
    let mut v = json!({
        "event": "attached",
        "role": agent.cfg.role(),
        "version": VERSION,
        "source": SOURCE_URL,
        "port": agent.cfg.with(|c| c.listen.rsplit(':').next().and_then(|p| p.parse::<u16>().ok()).unwrap_or(0)),
        "fingerprint": agent.role.lock().unwrap().replica_fingerprint,
        "peer_up": agent.role.lock().unwrap().peer_up(),
        "status": agent.status.lock().unwrap().clone(),
        "addon_attached": agent.link.attached.load(Relaxed),
        // Where this binary is: the addon opens the settings window from it.
        "exe": std::env::current_exe().map(|p| p.to_string_lossy().into_owned()).unwrap_or_default(),
        "video_port": agent.cfg.with(|c| if c.role == "host" { c.video_port } else { 0 }),
        "video_state": *agent.ctx.video_src.state.lock().unwrap(),
        // COLD_ACK carries bytes since 2026-09-23; an addon that does not
        // see this key falls back to counting messages against an old agent.
        "credits": "bytes",
        "lanes": ["fast"],  // tier-1 has its own stream; absent = send it on cold
        "codec": "zstd",    // cold payloads are compressed on the wire by the agent
        // The live settings, so the addon mirrors them instead of owning
        // its own copy. The addon already received `role` and `port` and
        // threw them away; now there is a reason to keep them.
        "config": agent.cfg.with(config::live_view),
    });
    if let Some(h) = &agent.role.lock().unwrap().host_obs {
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

fn main() {
    platform::attach_parent_console();
    let args = qcbridge_agent::Args::parse(USAGE);
    if args.flag("version") {
        println!("qcbridge-agent {VERSION}\nsource: {SOURCE_URL}\nlicense: GPL-3.0-or-later");
        return;
    }
    if args.flag("settings") {
        // The window is its own process: it attaches to the running agent
        // as a control client, so no event loop is shared with the tray.
        let cfg_path: std::path::PathBuf = args.get("config").map(Into::into).unwrap_or_else(config::config_path);
        if let Err(e) = qcbridge_agent::settings::run(cfg_path) {
            log!("[settings] {e:#}");
            platform::fatal_dialog("QCBridge Agent settings", &format!("{e:#}"));
            std::process::exit(1);
        }
        return;
    }
    if let Err(e) = run(&args) {
        // The line goes to the log (and stderr, when there is one); the box
        // is for the Windows user who has neither, unless this is a headless
        // run from a test or a script.
        let msg = format!("{e:#}");
        log!("[agent] start failed: {msg}");
        if !args.flag("no-tray") {
            let mut text = msg.clone();
            if let Some(p) = qcbridge_agent::logging::path() {
                text.push_str(&format!("\n\nLog: {}", p.display()));
            }
            platform::fatal_dialog("QCBridge Agent could not start", &text);
        }
        std::process::exit(1);
    }
}

fn run(args: &qcbridge_agent::Args) -> Result<()> {
    let cfg_path: std::path::PathBuf = args.get("config").map(Into::into).unwrap_or_else(config::config_path);
    // Cert, agent.json and the logs live beside the config, so --config
    // isolates an instance completely (two agents in a test, or
    // host+replica in dev). The log opens first: a broken TOML is the kind
    // of failure the file exists to show.
    let base = config::base_dir(&cfg_path);
    qcbridge_agent::logging::init(&base.join("agent.log"));
    let mut cfg = config::load_or_create(&cfg_path)?;
    if let Some(r) = args.get("role") { cfg.role = r.to_string(); }
    if let Some(l) = args.get("listen") { cfg.listen = l.to_string(); }
    if let Some(p) = args.get("peer") { cfg.peer = p.to_string(); }
    if let Some(p) = args.get("local-port") { cfg.local_port = p.parse()?; }
    if args.flag("no-tray") { cfg.tray = false; }
    // A spawned agent belongs to whoever spawned it. Without this it
    // outlives a Blender that was killed rather than closed, keeps its UDP
    // port, and the next run dies with "Address already in use" — which is
    // exactly what happened to the first bootstrap bench.
    let exit_with_addon = args.flag("exit-with-addon");
    let role_host = cfg.role == "host";
    log!("[agent] {VERSION} role={} config={}", cfg.role, cfg_path.display());
    // The token comes from its store (keychain, file), never from the TOML
    // — except once, to move an old clear-text entry out of it. After the
    // --role override: the store is per role.
    if let Some(note) = config::load_token(&cfg_path, &mut cfg)? {
        log!("[agent] {note}");
    }
    if let Some(t) = args.get("token") { cfg.token = t.to_string(); } // in memory only
    // Children die with the agent, whichever way it goes: a hard kill must
    // not leave Blender, the helper and ffmpeg holding its ports.
    if let Err(e) = platform::install_job_object() {
        log!("[agent] no job object ({e}); children may outlive the agent");
    }
    // The exact guard (Windows: a named mutex); the pid check below stays
    // as the cross-platform one, and as the one a recycled pid could fool.
    let _instance = platform::claim_instance(&base, &cfg.role).map_err(anyhow::Error::msg)?;

    rustls::crypto::ring::default_provider()
        .install_default()
        .map_err(|_| anyhow::anyhow!("a rustls crypto provider was already installed"))?;

    // Addon link + queues.
    let (out_tx, out_rx) = mpsc::channel(256);
    let (prio_tx, prio_rx) = mpsc::channel(1024);
    let link = Arc::new(Link::new(out_tx, prio_tx));
    {
        let link = link.clone();
        std::thread::Builder::new().name("link-writer".into()).spawn(move || writer_thread(link, out_rx, prio_rx))?;
    }
    let (control_tx, control_rx) = mpsc::channel(1024);
    let (cold_tx, cold_rx) = mpsc::channel(256);
    let (fast_tx, fast_rx) = mpsc::channel(1024);
    let inb = Arc::new(Inbound {
        control_tx, cold_tx, fast_tx, hot: Mutex::new(Default::default()), hot_notify: Notify::new(),
        connected: AtomicBool::new(false),
    });
    let status = Arc::new(Mutex::new("starting".to_string()));

    // Local socket the addon attaches to.
    let local = TcpListener::bind(("127.0.0.1", cfg.local_port))
        .with_context(|| format!("bind the addon socket 127.0.0.1:{}", cfg.local_port))?;
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
    let runtime = tokio::runtime::Builder::new_multi_thread().worker_threads(4).enable_all().build()?;
    let video_src = Arc::new(VideoSource::new());
    video_src.set_log_dir(base.clone());
    let video_sink = Arc::new(VideoSink::new());

    // One config from here on. Everything long-lived takes a handle to it,
    // so a change is visible to all of them; `cfg` itself stays only for the
    // startup reads below, which happen before any of this can be edited.
    let shared = SharedConfig::new(cfg.clone());

    // The role's runtime is built after the Agent exists, and rebuilt by
    // the Send/Receive switch; the context starts with no observer.
    let host_control = Arc::new(session::HostControl::new(None));
    let ctx = Arc::new(Ctx::new(
        role_host, shared.clone(), link.clone(), inb.clone(),
        control_rx, cold_rx, fast_rx, video_src.clone(), video_sink.clone(),
        Arc::new(session::NoObserver),
    ));
    let agent = Arc::new(Agent {
        cfg: shared.clone(), base: base.clone(), cfg_path: cfg_path.clone(),
        rt: runtime.handle().clone(),
        host_control: host_control.clone(), link: link.clone(),
        inb: inb.clone(), ctx: ctx.clone(),
        role: Mutex::new(RoleRuntime::default()), local_port, secret: secret.clone(), status: status.clone(),
        quit: Arc::new(Notify::new()),
    });
    *agent.role.lock().unwrap() = start_role(&agent)?;

    // Local socket server.
    {
        let a = agent.clone();
        let on_attach: Arc<dyn Fn() -> Value + Send + Sync> = Arc::new(move || attach_reply(&a));
        let a = agent.clone();
        let on_detach: Arc<dyn Fn() + Send + Sync> = Arc::new(move || {
            if let Some(l) = &a.role.lock().unwrap().lifecycle { let _ = l.tx.send(Event::AddonDetached); }
            if exit_with_addon {
                log!("[agent] addon detached; exiting (--exit-with-addon)");
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
    log!("[agent] addon socket 127.0.0.1:{local_port}");

    // Status changes go out as events, so the settings window (a control
    // client) shows what the tray shows without polling the agent.
    {
        let a = agent.clone();
        std::thread::Builder::new().name("status-ticker".into()).spawn(move || {
            let mut last = (String::new(), false, false, false);
            loop {
                std::thread::sleep(std::time::Duration::from_millis(500));
                // The host's session signal to the replica: its addon is
                // attached. Kept here so one place reads the flag.
                let attached = a.link.attached.load(Relaxed);
                a.ctx.addon_session.send_if_modified(|v| if *v != attached { *v = attached; true } else { false });
                // The addon reads these too (the replica's kiosk follows the
                // session flag), not only the control clients.
                if a.link.control_clients() == 0 && !attached {
                    continue;
                }
                // One take of the role lock: two `a.role.lock()` temporaries
                // in a single tuple expression both live to its end, and a
                // std mutex is not re-entrant — the ticker deadlocked on its
                // first tick holding the lock, and every attach and command
                // behind it (2026-09-25).
                let (peer_up, session_on) = {
                    let r = a.role.lock().unwrap();
                    (r.peer_up(), r.session_on())
                };
                let now = (a.status.lock().unwrap().clone(), attached, peer_up, session_on);
                if now != last {
                    a.link.event_try(json!({"event": "status", "status": now.0, "addon_attached": now.1, "peer_up": now.2, "session": now.3}));
                    last = now;
                }
            }
        })?;
    }

    if cfg.tray {
        tray::run(agent, runtime)
    } else {
        runtime.block_on(agent.quit.notified());
        // Deregister on the way out, so the next agent in this directory
        // does not read a dead port back out of agent.json.
        let role = agent.cfg.role();
        let rt = std::mem::take(&mut *agent.role.lock().unwrap());
        stop_role(&agent, rt, &role);
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
                    log!("[agent] could not persist config: {e}");
                }
                if applied.changed.iter().any(|f| f == "token") {
                    // save() left it out of the TOML; its home is the store.
                    match qcbridge_agent::secrets::store(&snapshot.token_store, &agent.base, &snapshot.role, &snapshot.token) {
                        Ok(()) => log!("[agent] token {} ({})", if snapshot.token.is_empty() { "cleared" } else { "set" },
                            qcbridge_agent::secrets::fingerprint(&snapshot.token)),
                        Err(e) => {
                            log!("[agent] token could not be stored: {e:#}");
                            agent.link.event_blocking(json!({"event": "error", "msg": format!("token not stored: {e:#}")}));
                        }
                    }
                }
                if applied.changed.iter().any(|f| f == "peer") && agent.cfg.is_host() {
                    retarget(agent, snapshot.peer.clone(), snapshot.fingerprint.clone());
                }
                if applied.changed.iter().any(|f| f == "discovery" || f == "phonebook" || f == "name") {
                    // The service rebinds on a mode change and re-reads the
                    // name/phonebook on its next announce; nudge it now.
                    if let Some(b) = &agent.role.lock().unwrap().beacon { b.set_mode(&snapshot.discovery); }
                }
                let role_changed = applied.changed.iter().any(|f| f == "role");
                let listen_changed = applied.changed.iter().any(|f| f == "listen") && !agent.cfg.is_host();
                if role_changed || listen_changed {
                    // Rebuild in place: the Send/Receive switch, or a replica
                    // moving its listener. The old role is what the runtime
                    // still is; the config already says the new one.
                    let old_role = if role_changed { if snapshot.role == "host" { "replica" } else { "host" } } else { snapshot.role.as_str() };
                    switch_role(agent, old_role);
                }
            }
            push_config_event(agent, cmd.get("req").cloned(), &applied);
        }
        Some("discover") => {
            // Probe one address (the VPN path), or sweep: multicast plus the
            // phonebook. Async, so it never blocks the addon link.
            let req = cmd.get("req").cloned();
            let target = cmd.get("addr").and_then(Value::as_str).map(String::from);
            let link = agent.link.clone();
            let cfg = agent.cfg.clone();
            let me = discovery::SelfId {
                fp: agent.role.lock().unwrap().replica_fingerprint.clone(),
                name: agent.cfg.with(|c| c.display_name()),
                role: agent.cfg.role(),
            };
            agent.rt.spawn(async move {
                let (source, mut peers) = match target.as_deref() {
                    Some(t) => ("probe", discovery::discover(Some(t), discovery::PORT, std::time::Duration::from_secs(2), &me).await),
                    None => ("multicast", discovery::discover(None, discovery::PORT, std::time::Duration::from_secs(1), &me).await),
                };
                // Name only the sources that actually produced a peer: a
                // sweep always *tries* multicast, but "found via multicast"
                // when only the phonebook answered would mislabel the picker.
                let mut sources = vec![];
                if !peers.is_empty() { sources.push(source.to_string()); }
                if target.is_none() {
                    let book = discovery::phonebook_scan(&cfg, &me);
                    if !book.is_empty() { sources.push("phonebook".into()); }
                    for b in book {
                        if !peers.iter().any(|p| p.fp == b.fp && p.port == b.port) { peers.push(b); }
                    }
                }
                let mut v = json!({"event": "peers", "sources": sources, "peers": peers});
                if let Some(r) = req { v["req"] = r; }
                link.event_try(v);
            });
        }
        Some("forget_fingerprint") if agent.cfg.is_host() => {
            let snapshot = agent.cfg.update(|c| {
                c.fingerprint.clear();
                c.clone()
            });
            if let Err(e) = config::save(&agent.cfg_path, &snapshot) {
                log!("[agent] could not persist config: {e}");
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
        other => log!("[agent] unknown cmd {other:?}"),
    }
}

mod tray {
    use super::*;
    use muda::{CheckMenuItem, Menu, MenuEvent, MenuItem, PredefinedMenuItem, Submenu};
    use tao::event::Event as TaoEvent;
    use tao::event_loop::{ControlFlow, EventLoopBuilder};
    use tray_icon::{Icon, TrayIconBuilder};

    /// The fingerprint, never the token: two people can compare eight hex
    /// characters over a call without either reading theirs out.
    fn token_text(cfg: &SharedConfig) -> String {
        cfg.with(|c| if c.token.is_empty() { "token: not set".to_string() } else {
            format!("token: set · {}", qcbridge_agent::secrets::fingerprint(&c.token))
        })
    }

    /// The settings window is a second process of this binary.
    pub(super) fn open_settings_window(cfg_path: &std::path::Path) {
        if let Ok(exe) = std::env::current_exe() {
            let mut cmd = std::process::Command::new(exe);
            cmd.arg("--settings").arg("--config").arg(cfg_path);
            if let Err(e) = cmd.spawn() {
                log!("[agent] could not open the settings window: {e}");
            }
        }
    }

    fn fp_text(agent: &Agent) -> String {
        let fp = agent.role.lock().unwrap().replica_fingerprint.clone();
        if fp.is_empty() { "fingerprint: (host role)".to_string() } else { format!("fingerprint {}…", &fp[..16]) }
    }

    fn icon(state: qcbridge_agent::icons::TrayState) -> Icon {
        let (rgba, w, h) = qcbridge_agent::icons::rgba(state.png());
        Icon::from_rgba(rgba, w, h).expect("icon")
    }

    pub fn run(agent: Arc<Agent>, runtime: tokio::runtime::Runtime) -> Result<()> {
        let mut event_loop = EventLoopBuilder::new().build();
        let menu = Menu::new();
        let status_item = MenuItem::new("starting", false, None);
        let info_item = MenuItem::new(
            agent.cfg.with(|c| if c.role == "host" { format!("host → {}", c.peer) } else { format!("replica · listening {}", c.listen) }),
            false, None,
        );
        let fp_item = MenuItem::new(fp_text(&agent), false, None);
        let token_item = MenuItem::new(token_text(&agent.cfg), false, None);
        let start_item = MenuItem::new("Launch Blender now", !agent.cfg.is_host(), None);
        let stop_item = MenuItem::new("Close Blender", !agent.cfg.is_host(), None);
        let settings_item = MenuItem::new("Settings…", true, None);
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
        // Replicas are the things that get found; a host only looks. The
        // setting still exists in a host's config for symmetry, but there is
        // nothing for it to do, so say so rather than offer a dead switch.
        let network = if agent.cfg.is_host() {
            Submenu::with_items("Network (a replica-side setting)", false, &[&net_off, &net_direct, &net_discover])?
        } else {
            Submenu::with_items("Network", true, &[&net_off, &net_direct, &net_discover])?
        };

        menu.append_items(&[
            &status_item, &info_item, &fp_item, &token_item, &PredefinedMenuItem::separator(),
            &network, &PredefinedMenuItem::separator(),
            &start_item, &stop_item, &PredefinedMenuItem::separator(),
            &settings_item, &config_item, &quit_item,
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
        let mut tray_state: Option<qcbridge_agent::icons::TrayState> = None;
        let mut last_info = String::new();
        let mut last_token = String::new();
        let mut last_fp = fp_text(&agent);
        let mut last_is_host = agent.cfg.is_host();
        let mut last_tooltip = String::new();
        let mut last_mode = mode0;
        // macOS: tao applies its own activation policy in
        // applicationDidFinishLaunching, and its default is Regular — a Dock
        // tile and an app menu, overriding the bundle's LSUIElement (the
        // 0.2.0 build showed both a menu-bar icon and a Dock icon). Accessory
        // is the menu-bar app the plist asks for; the second call keeps a
        // login-item launch from taking focus off whatever is in front.
        // The settings window's winit loop already does the same (settings/mod.rs).
        #[cfg(target_os = "macos")]
        {
            use tao::platform::macos::{ActivationPolicy, EventLoopExtMacOS};
            event_loop.set_activation_policy(ActivationPolicy::Accessory);
            event_loop.set_activate_ignoring_other_apps(false);
        }
        event_loop.run(move |event, _, control_flow| {
            *control_flow = ControlFlow::WaitUntil(std::time::Instant::now() + std::time::Duration::from_millis(500));
            if let TaoEvent::NewEvents(tao::event::StartCause::Init) = event {
                tray = Some(
                    TrayIconBuilder::new()
                        .with_menu(Box::new(menu.clone()))
                        .with_tooltip("QCBridge Agent")
                        .with_icon(icon(qcbridge_agent::icons::TrayState::Waiting))
                        .build()
                        .expect("tray icon"),
                );
            }
            let s = agent.status.lock().unwrap().clone();
            if s != last_status {
                status_item.set_text(&s);
                last_status = s;
            }
            // The dot follows the pairing: green up, amber waiting, red off.
            let want = qcbridge_agent::icons::TrayState::from_status(agent.role.lock().unwrap().peer_up(), &last_status);
            if tray_state != Some(want) {
                if let Some(t) = tray.as_ref() { let _ = t.set_icon(Some(icon(want))); }
                tray_state = Some(want);
            }
            // These were built once and went stale; a set_config from the
            // addon can change any of them under us, so refresh on the tick.
            let info = info_text();
            if info != last_info {
                info_item.set_text(&info);
                last_info = info;
            }
            let token = token_text(&agent.cfg);
            if token != last_token {
                token_item.set_text(&token);
                last_token = token;
            }
            // The role can flip at runtime: the fingerprint line and the
            // replica-only items follow it.
            let fp = fp_text(&agent);
            if fp != last_fp {
                fp_item.set_text(&fp);
                last_fp = fp;
            }
            let is_host = agent.cfg.is_host();
            if is_host != last_is_host {
                start_item.set_enabled(!is_host);
                stop_item.set_enabled(!is_host);
                network.set_enabled(!is_host);
                last_is_host = is_host;
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
                            log!("[agent] could not persist config: {e}");
                        }
                        if let Some(b) = &agent.role.lock().unwrap().beacon { b.set_mode(&snapshot.discovery); }
                    }
                    push_config_event(&agent, None, &applied);
                    last_mode.clear(); // force the check refresh next tick
                    continue;
                }
                if ev.id == start_item.id() {
                    if let Some(l) = &agent.role.lock().unwrap().lifecycle { let _ = l.tx.send(Event::ManualStart); }
                } else if ev.id == stop_item.id() {
                    if let Some(l) = &agent.role.lock().unwrap().lifecycle { let _ = l.tx.send(Event::ManualStop); }
                } else if ev.id == settings_item.id() {
                    open_settings_window(&agent.cfg_path);
                } else if ev.id == config_item.id() {
                    let dir = agent.base.clone();
                    #[cfg(target_os = "macos")]
                    let _ = std::process::Command::new("open").arg(&dir).spawn();
                    #[cfg(windows)]
                    let _ = std::process::Command::new("explorer").arg(&dir).spawn();
                } else if ev.id == quit_item.id() {
                    let lifecycle = agent.role.lock().unwrap().lifecycle.clone();
                    if let Some(l) = lifecycle {
                        let _ = l.tx.send(Event::Shutdown);
                        // Shutdown asks the addon to quit Blender cleanly.
                        // Our exit closes the job object, which kills what
                        // is left, so give the clean path a few seconds.
                        let until = std::time::Instant::now() + std::time::Duration::from_secs(5);
                        while l.blender_running() && std::time::Instant::now() < until {
                            std::thread::sleep(std::time::Duration::from_millis(100));
                        }
                    }
                    agent.ctx.video_src.stop();
                    // Sends bye, removes the phonebook entry, deregisters
                    // from agent.json.
                    let role = agent.cfg.role();
                    let rt = std::mem::take(&mut *agent.role.lock().unwrap());
                    stop_role(&agent, rt, &role);
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
