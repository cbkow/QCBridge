//! Replica Blender lifecycle: launch when a host connects, ask it to quit on
//! goodbye/idle, relaunch on crash. The agent never edits scene data; it
//! starts Blender with an expression that hands control to the addon's
//! `agent_launch` module.

use crate::config::Config;
use crate::link::Link;
use crate::session::PeerObserver;
use serde_json::json;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering::Relaxed};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::sync::mpsc;

/// Runs inside Blender: find the installed qcbridge extension whatever repo
/// it lives in, enable it, and call its agent_launch.run().
const LAUNCH_EXPR: &str = r#"
import bpy, importlib
_cands = []
for _r in bpy.context.preferences.extensions.repos:
    _cands.append(f"bl_ext.{_r.module}.qcbridge")
_cands += ["bl_ext.user_default.qcbridge", "qcbridge"]
_ok = False
for _m in _cands:
    try:
        _launch = importlib.import_module(_m + ".agent_launch")
    except Exception:
        continue  # not installed there, or too old to have agent_launch
    if _m not in bpy.context.preferences.addons:
        try:
            bpy.ops.preferences.addon_enable(module=_m)
        except Exception as _e:
            print("qcbridge: enable failed", _e)
    try:
        _launch.run()
        _ok = True
    except Exception as _e:
        print("qcbridge: agent_launch failed", _e)
    break
if not _ok:
    print("qcbridge: no usable extension found (need one with agent_launch); install/update it")
"#;

pub enum Event {
    PeerUp,
    PeerDown,
    Goodbye,
    AddonDetached,
    ChildExited,
    ManualStart,
    ManualStop,
    Shutdown,
}

pub struct Lifecycle {
    cfg: Config,
    link: Arc<Link>,
    local_port: u16,
    secret: String,
    child: Mutex<Option<Child>>,
    pub peer_up: AtomicBool,
    pub tx: mpsc::UnboundedSender<Event>,
    pub status: Arc<Mutex<String>>,
}

impl PeerObserver for Lifecycle {
    fn peer_up(&self, _fp: Option<String>) {
        self.peer_up.store(true, Relaxed);
        let _ = self.tx.send(Event::PeerUp);
    }
    fn peer_down(&self, _reason: String) {
        self.peer_up.store(false, Relaxed);
        let _ = self.tx.send(Event::PeerDown);
    }
    fn goodbye(&self) {
        let _ = self.tx.send(Event::Goodbye);
    }
}

impl Lifecycle {
    pub fn new(cfg: Config, link: Arc<Link>, local_port: u16, secret: String, status: Arc<Mutex<String>>)
        -> (Arc<Self>, mpsc::UnboundedReceiver<Event>) {
        let (tx, rx) = mpsc::unbounded_channel();
        let me = Arc::new(Self {
            cfg, link, local_port, secret,
            child: Mutex::new(None),
            peer_up: AtomicBool::new(false),
            tx, status,
        });
        (me, rx)
    }

    pub fn blender_running(&self) -> bool {
        self.child.lock().unwrap().is_some()
    }

    fn set_status(&self, s: &str) {
        *self.status.lock().unwrap() = s.to_string();
        eprintln!("[lifecycle] {s}");
    }

    fn spawn_blender(self: &Arc<Self>) {
        if self.blender_running() {
            return;
        }
        // An empty blender_path means "do not supervise Blender": the agent
        // still pairs, carries every lane and reports status, it just never
        // launches anything. That is what the transport contract tests need,
        // and it suits a machine where Blender is started by hand.
        if self.cfg.blender_path.trim().is_empty() {
            self.set_status("connected (Blender supervision off)");
            return;
        }
        let mut cmd = Command::new(&self.cfg.blender_path);
        cmd.args(&self.cfg.blender_args)
            .arg("--python-expr")
            .arg(LAUNCH_EXPR)
            .env("QCB_AGENT_PORT", self.local_port.to_string())
            .env("QCB_AGENT_SECRET", &self.secret)
            .env("QCB_AGENT_TOKEN", &self.cfg.token)
            .env("QCB_AGENT_KIOSK", if self.cfg.kiosk { "1" } else { "0" })
            .env("QCB_TRANSPORT", "agent")
            .stdin(Stdio::null())
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit());
        match cmd.spawn() {
            Ok(child) => {
                self.set_status("Blender launching");
                *self.child.lock().unwrap() = Some(child);
                let me = self.clone();
                std::thread::spawn(move || {
                    // Wait without holding the lock: kill() needs it.
                    loop {
                        std::thread::sleep(Duration::from_millis(500));
                        let mut guard = me.child.lock().unwrap();
                        match guard.as_mut().map(|c| c.try_wait()) {
                            Some(Ok(Some(_))) | None => {
                                *guard = None;
                                break;
                            }
                            Some(Ok(None)) => {}
                            Some(Err(_)) => {
                                *guard = None;
                                break;
                            }
                        }
                    }
                    let _ = me.tx.send(Event::ChildExited);
                });
            }
            Err(e) => self.set_status(&format!("Blender launch failed: {e} ({})", self.cfg.blender_path)),
        }
    }

    /// Ask the addon to quit Blender cleanly; force-kill if it lingers.
    fn close_blender(self: &Arc<Self>) {
        if !self.blender_running() {
            return;
        }
        self.set_status("closing Blender");
        self.link.event_try(json!({"event": "quit"})); // called from the async lifecycle task
        let me = self.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_secs(10));
            if let Some(child) = me.child.lock().unwrap().as_mut() {
                let _ = child.kill();
            }
        });
    }

    pub async fn run(self: Arc<Self>, mut rx: mpsc::UnboundedReceiver<Event>) {
        let mut idle_deadline: Option<tokio::time::Instant> = None;
        let mut wanted = false; // a host wants Blender up
        loop {
            let sleep = async {
                match idle_deadline {
                    Some(t) => tokio::time::sleep_until(t).await,
                    None => std::future::pending::<()>().await,
                }
            };
            let event = tokio::select! {
                e = rx.recv() => match e { Some(e) => e, None => return },
                _ = sleep => {
                    idle_deadline = None;
                    if !self.peer_up.load(Relaxed) {
                        wanted = false;
                        self.close_blender();
                        self.set_status("idle");
                    }
                    continue;
                }
            };
            match event {
                Event::PeerUp => {
                    idle_deadline = None;
                    wanted = true;
                    self.set_status("host connected");
                    self.spawn_blender();
                }
                Event::PeerDown => {
                    if self.cfg.idle_secs > 0 && idle_deadline.is_none() {
                        idle_deadline = Some(tokio::time::Instant::now() + Duration::from_secs(self.cfg.idle_secs));
                    }
                    self.set_status(if self.blender_running() { "host gone, Blender warm" } else { "listening" });
                }
                Event::Goodbye => {
                    idle_deadline = Some(tokio::time::Instant::now() + Duration::from_secs(30));
                    self.set_status("host ended session");
                }
                Event::AddonDetached => {}
                Event::ChildExited => {
                    if wanted && self.peer_up.load(Relaxed) {
                        self.set_status("Blender exited, relaunching");
                        tokio::time::sleep(Duration::from_secs(3)).await;
                        self.spawn_blender();
                    } else {
                        self.set_status("listening");
                    }
                }
                Event::ManualStart => {
                    wanted = true;
                    self.spawn_blender();
                }
                Event::ManualStop => {
                    wanted = false;
                    idle_deadline = None;
                    self.close_blender();
                }
                Event::Shutdown => {
                    self.close_blender();
                    return;
                }
            }
        }
    }
}
