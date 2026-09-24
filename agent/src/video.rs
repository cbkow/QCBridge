//! Video source (replica: a capture+encode child, later native S6/S7) and
//! video sink (host: fan-out of Annex-B HEVC to local TCP viewers).

use crate::link::Link;
use crate::{AccessUnit, AuSplitter};
use bytes::Bytes;
use serde_json::json;
use std::collections::VecDeque;
use std::io::Read;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering::Relaxed};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::io::AsyncWriteExt;
use tokio::sync::{Notify, mpsc};

#[derive(Default)]
pub struct VideoCounters {
    pub aus_in: AtomicU64,
    pub aus_out: AtomicU64,
    pub bytes: AtomicU64,
    pub keys: AtomicU64,
    pub queue_drops: AtomicU64,
    pub gop_skips: AtomicU64,
    pub dwell_us_sum: AtomicU64,
    pub dwell_us_max: AtomicU64,
    pub holes: AtomicU64,
}

struct VideoQueue {
    queue: VecDeque<(AccessUnit, u64, Instant)>,
    active: bool,
    need_key: bool,
}

const VIDEO_QUEUE_CAP: usize = 6;

pub struct VideoSource {
    q: Mutex<VideoQueue>,
    notify: Notify,
    wanted: AtomicBool,
    generation: AtomicU64,
    child: Mutex<Option<std::process::Child>>,
    child_stdin: Mutex<Option<std::process::ChildStdin>>,
    /// Where capture.log goes (the child's stderr); None = inherit.
    log_dir: Mutex<Option<std::path::PathBuf>>,
    pub counters: VideoCounters,
    pub state: Mutex<String>,
}

impl VideoSource {
    pub fn new() -> Self {
        Self {
            q: Mutex::new(VideoQueue { queue: VecDeque::new(), active: false, need_key: true }),
            notify: Notify::new(),
            wanted: AtomicBool::new(false),
            generation: AtomicU64::new(0),
            child: Mutex::new(None),
            child_stdin: Mutex::new(None),
            log_dir: Mutex::new(None),
            counters: VideoCounters::default(),
            state: Mutex::new("off".into()),
        }
    }

    /// Send the capture child's stderr to `<dir>/capture.log` (one file per
    /// spawn) instead of the agent's own stderr.
    pub fn set_log_dir(&self, dir: std::path::PathBuf) {
        *self.log_dir.lock().unwrap() = Some(dir);
    }

    pub fn start(self: &Arc<Self>, argv: Vec<String>, link: Arc<Link>) {
        self.stop();
        self.wanted.store(true, Relaxed);
        let generation = self.generation.fetch_add(1, Relaxed) + 1;
        let me = self.clone();
        let _ = std::thread::Builder::new()
            .name("video-child".into())
            .spawn(move || me.supervise(argv, generation, link));
    }

    pub fn stop(&self) {
        self.wanted.store(false, Relaxed);
        self.generation.fetch_add(1, Relaxed);
        if let Some(mut child) = self.child.lock().unwrap().take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }

    fn current(&self, generation: u64) -> bool {
        self.wanted.load(Relaxed) && self.generation.load(Relaxed) == generation
    }

    fn set_state(&self, link: &Link, state: &str) {
        *self.state.lock().unwrap() = state.into();
        link.event_blocking(json!({"event": "video", "state": state}));
    }

    fn supervise(self: Arc<Self>, argv: Vec<String>, generation: u64, link: Arc<Link>) {
        while self.current(generation) {
            let stderr = match self.log_dir.lock().unwrap().as_ref().map(|d| d.join("capture.log")) {
                Some(p) => match std::fs::File::create(&p) {
                    Ok(f) => std::process::Stdio::from(f),
                    Err(e) => {
                        crate::log!("[video] cannot open {}: {e}; helper output is dropped", p.display());
                        std::process::Stdio::null()
                    }
                },
                None => std::process::Stdio::inherit(),
            };
            let mut cmd = std::process::Command::new(&argv[0]);
            cmd.args(&argv[1..])
                .stdin(std::process::Stdio::piped()) // "key\n" = keyframe on request (native capture)
                .stdout(std::process::Stdio::piped())
                .stderr(stderr);
            crate::platform::no_window(&mut cmd);
            let mut child = match cmd.spawn() {
                Ok(c) => c,
                Err(e) => {
                    crate::log!("[video] spawn failed: {e}");
                    self.set_state(&link, "spawn_failed");
                    return;
                }
            };
            let mut stdout = child.stdout.take().expect("piped stdout");
            *self.child_stdin.lock().unwrap() = child.stdin.take();
            *self.child.lock().unwrap() = Some(child);
            self.set_state(&link, "running");
            self.pump(&mut stdout, generation);
            if let Some(mut child) = self.child.lock().unwrap().take() {
                let _ = child.kill();
                let _ = child.wait();
            }
            if !self.current(generation) {
                break;
            }
            self.set_state(&link, "restarting");
            std::thread::sleep(Duration::from_secs(2));
        }
        self.set_state(&link, "off");
    }

    fn pump(&self, stdout: &mut impl Read, generation: u64) {
        let mut splitter = AuSplitter::new();
        let mut buf = vec![0u8; 256 * 1024];
        let mut aus = Vec::new();
        let mut index = 0u64;
        while self.current(generation) {
            let n = match stdout.read(&mut buf) {
                Ok(0) | Err(_) => return,
                Ok(n) => n,
            };
            splitter.push(&buf[..n], &mut aus);
            let now = Instant::now();
            for au in aus.drain(..) {
                let c = &self.counters;
                c.aus_in.fetch_add(1, Relaxed);
                let idx = index;
                index += 1;
                let mut st = self.q.lock().unwrap();
                if !st.active {
                    continue;
                }
                if st.need_key && !au.key {
                    c.gop_skips.fetch_add(1, Relaxed);
                    continue;
                }
                st.need_key = false;
                if st.queue.len() >= VIDEO_QUEUE_CAP {
                    c.queue_drops.fetch_add(st.queue.len() as u64, Relaxed);
                    st.queue.clear();
                    if !au.key {
                        st.need_key = true;
                        continue;
                    }
                }
                st.queue.push_back((au, idx, now));
                drop(st);
                self.notify.notify_one();
            }
        }
    }

    pub fn set_active(&self, active: bool) {
        let mut st = self.q.lock().unwrap();
        st.active = active;
        st.need_key = true;
        st.queue.clear();
        drop(st);
        if active {
            self.request_key();
        }
    }

    /// Ask the capture child for a keyframe (native capture honours it;
    /// ffmpeg ignores its stdin, so there it costs nothing).
    pub fn request_key(&self) {
        use std::io::Write;
        if let Some(stdin) = self.child_stdin.lock().unwrap().as_mut() {
            let _ = stdin.write_all(b"key\n").and_then(|_| stdin.flush());
        }
    }

    pub async fn next(&self) -> (AccessUnit, u64, Instant) {
        loop {
            let notified = self.notify.notified();
            if let Some(item) = self.q.lock().unwrap().queue.pop_front() {
                return item;
            }
            notified.await;
        }
    }
}

pub struct VideoSink {
    clients: Mutex<Vec<(mpsc::Sender<Bytes>, bool)>>,
    pub counters: VideoCounters,
}

impl VideoSink {
    pub fn new() -> Self {
        Self { clients: Mutex::new(Vec::new()), counters: VideoCounters::default() }
    }

    pub fn push(&self, au: Bytes, key: bool) {
        let mut clients = self.clients.lock().unwrap();
        clients.retain(|(tx, _)| !tx.is_closed());
        for (tx, need_key) in clients.iter_mut() {
            if *need_key && !key {
                continue;
            }
            *need_key = tx.try_send(au.clone()).is_err();
        }
    }
}

pub async fn video_listener(addr: SocketAddr, sink: Arc<VideoSink>, link: Arc<Link>) {
    let listener = match tokio::net::TcpListener::bind(addr).await {
        Ok(l) => l,
        Err(e) => {
            link.event(json!({"event": "error", "msg": format!("video-listen {addr}: {e}")})).await;
            return;
        }
    };
    let port = listener.local_addr().map(|a| a.port()).unwrap_or(0);
    link.event(json!({"event": "video_listen", "port": port})).await;
    loop {
        let Ok((mut sock, _)) = listener.accept().await else { continue };
        let _ = sock.set_nodelay(true);
        let (tx, mut rx) = mpsc::channel::<Bytes>(120);
        sink.clients.lock().unwrap().push((tx, true));
        tokio::spawn(async move {
            while let Some(au) = rx.recv().await {
                if sock.write_all(&au).await.is_err() {
                    break;
                }
            }
        });
    }
}
