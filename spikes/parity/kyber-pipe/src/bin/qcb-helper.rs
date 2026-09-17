//! qcb-helper — one Kyber connection for a QCBridge session (plan phase S5).
//!
//! The addon spawns one helper per machine and talks to it over stdin/stdout
//! frames; the two helpers hold ONE QUIC connection carrying every lane:
//!
//!   endpoint 0  video    UnreliableFec (RaptorQ)   replica -> host
//!   endpoint 2  control  reliable data             both ways (json, req/reply)
//!   endpoint 4  hot      reliable data, keyed latest-wins conflation at the
//!                        sender                    host -> replica
//!   endpoint 6  cold     reliable data, credit-acked to the addon
//!                                                  host -> replica
//!
//! The replica listens (QUIC server), the host connects — same as the zmq
//! topology. Hot rides a reliable stream for now: Kyber has no client->server
//! datagram lane (plan: Kyber patch 1); on the good links this project scopes
//! to, a dedicated stream with sender-side conflation behaves the same.
//!
//! stdio frame, both directions:  u32 BE length | u8 type | body
//!   0x01 CONTROL   u8 peer | json bytes
//!   0x02 HOT       u8 peer | u8 keylen | key | value
//!   0x03 COLD      u8 peer | opaque (addon packs header+payload)
//!   0x04 COLD_ACK  u32 BE count            helper -> addon: credits returned
//!   0x10 CMD       json                    addon -> helper
//!   0x20 EVENT     json                    helper -> addon
//! `peer` is always 0 today; the field keeps several replicas possible.

use anyhow::{Context, Result, anyhow, bail};
use bytes::{BufMut, Bytes, BytesMut};
use kyber_pipe::{
    AccessUnit, Args, AuSplitter, DEFAULT_MAX_UDP_PAYLOAD, FixedRateControllerFactory, HEVC_FOURCC,
    load_or_create_cert, log, sha256_hex,
};
use kymux_types::{
    AVPacket, CodecPacket, CodecPacketHeader, DataPacket, DataProtocol, MediaPacket,
    MediaPacketHeader,
};
use kynet::Server;
use kyproto::{ClientAuth, Connection, VideoProtocol};
use serde_json::{Value, json};
use std::collections::{HashMap, VecDeque};
use std::io::{Read, Write};
use std::net::{SocketAddr, ToSocketAddrs};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering::Relaxed};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::io::AsyncWriteExt;
use tokio::sync::{Notify, mpsc};

const USAGE: &str = "\
qcb-helper --role replica --listen HOST:PORT --token T [--cert-dir DIR] [--cap-mbps 400]
qcb-helper --role host --connect HOST:PORT --token T [--fingerprint SHA256HEX]
           [--video-listen 127.0.0.1:PORT] [--cap-mbps 400]
Speaks length-prefixed frames on stdin/stdout (see source header). Logs on stderr.";

const T_CONTROL: u8 = 0x01;
const T_HOT: u8 = 0x02;
const T_COLD: u8 = 0x03;
const T_COLD_ACK: u8 = 0x04;
const T_CMD: u8 = 0x10;
const T_EVENT: u8 = 0x20;

const EP_VIDEO: u16 = 0;
const EP_CONTROL: u16 = 2;
const EP_HOT: u16 = 4;
const EP_COLD: u16 = 6;

const MAX_FRAME: usize = 256 << 20;
const HANDSHAKE: Duration = Duration::from_secs(5);

// ------------------------------------------------------------ stdout ----

enum Out {
    Frame(u8, Bytes),
    Wake,
}

/// Everything the writer thread needs. Hot values bypass the queue: they sit
/// in a keyed map so a stalled addon (Blender loading a file) conflates them
/// instead of growing a backlog.
struct Stdout {
    tx: mpsc::Sender<Out>,
    hot: Mutex<HashMap<Vec<u8>, Bytes>>,
    hot_dirty: AtomicBool,
}

impl Stdout {
    async fn frame(&self, kind: u8, body: Bytes) {
        let _ = self.tx.send(Out::Frame(kind, body)).await;
    }

    fn frame_blocking(&self, kind: u8, body: Bytes) {
        let _ = self.tx.blocking_send(Out::Frame(kind, body));
    }

    fn event_blocking(&self, v: Value) {
        self.frame_blocking(T_EVENT, Bytes::from(v.to_string()));
    }

    async fn event(&self, v: Value) {
        self.frame(T_EVENT, Bytes::from(v.to_string())).await;
    }

    fn hot(&self, key: Vec<u8>, body: Bytes) {
        self.hot.lock().unwrap().insert(key, body);
        self.hot_dirty.store(true, Relaxed);
        let _ = self.tx.try_send(Out::Wake); // full queue = writer busy; it checks the flag
    }
}

fn write_frame(w: &mut impl Write, kind: u8, body: &[u8]) -> std::io::Result<()> {
    w.write_all(&((body.len() + 1) as u32).to_be_bytes())?;
    w.write_all(&[kind])?;
    w.write_all(body)
}

fn writer_thread(out: Arc<Stdout>, mut rx: mpsc::Receiver<Out>) {
    let stdout = std::io::stdout();
    let mut w = stdout.lock();
    while let Some(item) = rx.blocking_recv() {
        let mut ok = match item {
            Out::Frame(kind, body) => write_frame(&mut w, kind, &body).is_ok(),
            Out::Wake => true,
        };
        if ok && out.hot_dirty.swap(false, Relaxed) {
            let drained: Vec<Bytes> = out.hot.lock().unwrap().drain().map(|(_, v)| v).collect();
            for body in drained {
                ok &= write_frame(&mut w, T_HOT, &body).is_ok();
            }
        }
        if !ok || w.flush().is_err() {
            std::process::exit(0); // addon went away
        }
    }
}

// ------------------------------------------------------------- stdin ----

/// Addon -> network queues. They outlive sessions; a session drains stale
/// items when it starts.
struct Inbound {
    control_tx: mpsc::Sender<Bytes>,
    cold_tx: mpsc::Sender<Bytes>,
    hot: Mutex<HashMap<Vec<u8>, Bytes>>,
    hot_notify: Notify,
    connected: AtomicBool,
}

fn read_exact_or_eof(r: &mut impl Read, buf: &mut [u8]) -> bool {
    r.read_exact(buf).is_ok()
}

fn stdin_thread(inb: Arc<Inbound>, out: Arc<Stdout>, video: Arc<VideoSource>) {
    let stdin = std::io::stdin();
    let mut r = stdin.lock();
    let mut head = [0u8; 5];
    loop {
        if !read_exact_or_eof(&mut r, &mut head) {
            break;
        }
        let len = u32::from_be_bytes([head[0], head[1], head[2], head[3]]) as usize;
        if len == 0 || len > MAX_FRAME {
            log("helper", format!("bad frame length {len}; exiting"));
            break;
        }
        let kind = head[4];
        let mut body = vec![0u8; len - 1];
        if !read_exact_or_eof(&mut r, &mut body) {
            break;
        }
        let body = Bytes::from(body);
        match kind {
            T_CONTROL => {
                let _ = inb.control_tx.try_send(body); // full/disconnected: heartbeats notice
            }
            T_HOT => {
                if body.len() >= 2 {
                    let klen = body[1] as usize;
                    if body.len() >= 2 + klen {
                        let key = body[2..2 + klen].to_vec();
                        inb.hot.lock().unwrap().insert(key, body);
                        inb.hot_notify.notify_one();
                    }
                }
            }
            T_COLD => {
                // The addon holds a credit window, so this queue cannot fill
                // in normal operation. Anything dropped is still acked: cold
                // seq gaps are detected and healed by the sync layer.
                let sent = inb.connected.load(Relaxed) && inb.cold_tx.try_send(body).is_ok();
                if !sent {
                    out.frame_blocking(T_COLD_ACK, Bytes::copy_from_slice(&1u32.to_be_bytes()));
                }
            }
            T_CMD => match serde_json::from_slice::<Value>(&body) {
                Ok(cmd) => handle_cmd(&cmd, &out, &video),
                Err(e) => log("helper", format!("bad CMD json: {e}")),
            },
            other => log("helper", format!("unknown frame type {other:#x}")),
        }
    }
    video.stop();
    exit_after_flush(); // stdin EOF: the addon is gone
}

/// A goodbye queued just before shutdown still has to reach the wire.
fn exit_after_flush() -> ! {
    std::thread::sleep(Duration::from_millis(300));
    std::process::exit(0)
}

fn handle_cmd(cmd: &Value, out: &Arc<Stdout>, video: &Arc<VideoSource>) {
    match cmd.get("cmd").and_then(Value::as_str) {
        Some("video_start") => {
            let argv: Vec<String> = cmd
                .get("argv")
                .and_then(Value::as_array)
                .map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect())
                .unwrap_or_default();
            if argv.is_empty() {
                out.event_blocking(json!({"event": "error", "msg": "video_start: empty argv"}));
            } else {
                video.start(argv, out.clone());
            }
        }
        Some("video_stop") => video.stop(),
        Some("shutdown") => {
            video.stop();
            exit_after_flush();
        }
        other => log("helper", format!("unknown cmd {other:?}")),
    }
}

// ------------------------------------------------- replica video source ----

struct VideoQueue {
    queue: VecDeque<(AccessUnit, u64, Instant)>,
    active: bool,
    need_key: bool,
}

#[derive(Default)]
struct VideoCounters {
    aus_in: AtomicU64,
    aus_out: AtomicU64,
    bytes: AtomicU64,
    keys: AtomicU64,
    queue_drops: AtomicU64,
    gop_skips: AtomicU64,
    dwell_us_sum: AtomicU64,
    dwell_us_max: AtomicU64,
    holes: AtomicU64,
}

/// Capture+encode child (ffmpeg writing Annex-B HEVC with AUDs to stdout).
/// S6/S7 replace the child with native capture; the queue stays.
struct VideoSource {
    q: Mutex<VideoQueue>,
    notify: Notify,
    wanted: AtomicBool,
    generation: AtomicU64,
    child: Mutex<Option<std::process::Child>>,
    counters: VideoCounters,
}

const VIDEO_QUEUE_CAP: usize = 6;

impl VideoSource {
    fn new() -> Self {
        Self {
            q: Mutex::new(VideoQueue { queue: VecDeque::new(), active: false, need_key: true }),
            notify: Notify::new(),
            wanted: AtomicBool::new(false),
            generation: AtomicU64::new(0),
            child: Mutex::new(None),
            counters: VideoCounters::default(),
        }
    }

    fn start(self: &Arc<Self>, argv: Vec<String>, out: Arc<Stdout>) {
        self.stop();
        self.wanted.store(true, Relaxed);
        let generation = self.generation.fetch_add(1, Relaxed) + 1;
        let me = self.clone();
        let _ = std::thread::Builder::new()
            .name("video-child".into())
            .spawn(move || me.supervise(argv, generation, out));
    }

    fn stop(&self) {
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

    fn supervise(self: Arc<Self>, argv: Vec<String>, generation: u64, out: Arc<Stdout>) {
        while self.current(generation) {
            let spawned = std::process::Command::new(&argv[0])
                .args(&argv[1..])
                .stdin(std::process::Stdio::null())
                .stdout(std::process::Stdio::piped())
                .stderr(std::process::Stdio::inherit())
                .spawn();
            let mut child = match spawned {
                Ok(c) => c,
                Err(e) => {
                    out.event_blocking(json!({"event": "video", "state": "spawn_failed", "msg": e.to_string()}));
                    return;
                }
            };
            let mut stdout = child.stdout.take().expect("piped stdout");
            *self.child.lock().unwrap() = Some(child);
            out.event_blocking(json!({"event": "video", "state": "running"}));
            self.pump(&mut stdout, generation);
            if let Some(mut child) = self.child.lock().unwrap().take() {
                let _ = child.kill();
                let _ = child.wait();
            }
            if !self.current(generation) {
                break;
            }
            out.event_blocking(json!({"event": "video", "state": "restarting"}));
            std::thread::sleep(Duration::from_secs(2));
        }
        out.event_blocking(json!({"event": "video", "state": "off"}));
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
                    // Dropping one P-frame breaks the chain: flush, resume at a key.
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

    fn set_active(&self, active: bool) {
        let mut st = self.q.lock().unwrap();
        st.active = active;
        st.need_key = true;
        st.queue.clear();
    }

    async fn next(&self) -> (AccessUnit, u64, Instant) {
        loop {
            let notified = self.notify.notified();
            if let Some(item) = self.q.lock().unwrap().queue.pop_front() {
                return item;
            }
            notified.await;
        }
    }
}

// ------------------------------------------------- host video fan-out ----

struct VideoSink {
    clients: Mutex<Vec<(mpsc::Sender<Bytes>, bool)>>, // (queue, need_key)
    counters: VideoCounters,
}

impl VideoSink {
    fn push(&self, au: Bytes, key: bool) {
        let mut clients = self.clients.lock().unwrap();
        clients.retain(|(tx, _)| !tx.is_closed());
        for (tx, need_key) in clients.iter_mut() {
            if *need_key && !key {
                continue;
            }
            *need_key = tx.try_send(au.clone()).is_err(); // slow viewer: resume at next key
        }
    }
}

async fn video_listener(addr: SocketAddr, sink: Arc<VideoSink>, out: Arc<Stdout>) {
    let listener = match tokio::net::TcpListener::bind(addr).await {
        Ok(l) => l,
        Err(e) => {
            out.event(json!({"event": "error", "msg": format!("video-listen {addr}: {e}")})).await;
            return;
        }
    };
    let port = listener.local_addr().map(|a| a.port()).unwrap_or(0);
    out.event(json!({"event": "video_listen", "port": port})).await;
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

// ------------------------------------------------------------ sessions ----

struct Ctx {
    role_host: bool,
    token: String,
    out: Arc<Stdout>,
    inb: Arc<Inbound>,
    control_rx: tokio::sync::Mutex<mpsc::Receiver<Bytes>>,
    cold_rx: tokio::sync::Mutex<mpsc::Receiver<Bytes>>,
    video_src: Arc<VideoSource>,
    video_sink: Arc<VideoSink>,
}

fn lane_body(peer: u8, payload: &[u8]) -> Bytes {
    let mut b = BytesMut::with_capacity(payload.len() + 1);
    b.put_u8(peer);
    b.put_slice(payload);
    b.freeze()
}

async fn ack_cold(out: &Stdout, n: u32) {
    out.frame(T_COLD_ACK, Bytes::copy_from_slice(&n.to_be_bytes())).await;
}

/// Runs every lane until the connection ends or a lane fails.
async fn run_lanes(
    ctx: Arc<Ctx>,
    conn: Connection,
    control: DataProtocol,
    hot: DataProtocol,
    cold: DataProtocol,
    video_send: Option<kymux_types::VideoServerProtocol>,
    video_recv: Option<kymux_types::VideoClientProtocol>,
) -> Result<()> {
    let mut tasks = tokio::task::JoinSet::<Result<()>>::new();
    let DataProtocol { send: mut control_send, recv: mut control_recv } = control;
    let DataProtocol { send: mut hot_send, recv: mut hot_recv } = hot;
    let DataProtocol { send: mut cold_send, recv: mut cold_recv } = cold;

    // Stale addon->network items from before this session.
    {
        let mut rx = ctx.control_rx.lock().await;
        while rx.try_recv().is_ok() {}
        let mut rx = ctx.cold_rx.lock().await;
        let mut stale = 0u32;
        while rx.try_recv().is_ok() {
            stale += 1;
        }
        if stale > 0 {
            ack_cold(&ctx.out, stale).await;
        }
    }
    ctx.inb.connected.store(true, Relaxed);

    // control: both directions, both roles.
    {
        let ctx = ctx.clone();
        tasks.spawn(async move {
            let mut rx = ctx.control_rx.lock().await;
            while let Some(body) = rx.recv().await {
                // strip the peer byte; the wire carries the json only
                control_send.send(DataPacket { payload: body.slice(1..) }).await?;
            }
            Ok(())
        });
    }
    {
        let ctx = ctx.clone();
        tasks.spawn(async move {
            while let Some(p) = control_recv.recv().await? {
                ctx.out.frame(T_CONTROL, lane_body(0, &p.payload)).await;
            }
            Ok(())
        });
    }

    if ctx.role_host {
        // hot: drain the conflation map whenever it changes.
        let c = ctx.clone();
        tasks.spawn(async move {
            loop {
                let notified = c.inb.hot_notify.notified();
                let pending: Vec<Bytes> =
                    c.inb.hot.lock().unwrap().drain().map(|(_, v)| v).collect();
                if pending.is_empty() {
                    notified.await;
                    continue;
                }
                for body in pending {
                    hot_send.send(DataPacket { payload: body.slice(1..) }).await?;
                }
            }
        });
        // cold: ordered, one credit back per frame handed to QUIC.
        let c = ctx.clone();
        tasks.spawn(async move {
            let mut rx = c.cold_rx.lock().await;
            while let Some(body) = rx.recv().await {
                let sent = cold_send.send(DataPacket { payload: body.slice(1..) }).await;
                ack_cold(&c.out, 1).await;
                sent?;
            }
            Ok(())
        });
        // Keep the unused receive halves alive for the session.
        tasks.spawn(async move {
            let _keep = (&mut hot_recv, &mut cold_recv);
            std::future::pending::<()>().await;
            Ok(())
        });
    } else {
        let c = ctx.clone();
        tasks.spawn(async move {
            while let Some(p) = hot_recv.recv().await? {
                if p.payload.is_empty() {
                    continue;
                }
                let klen = p.payload[0] as usize;
                if p.payload.len() > klen {
                    let key = p.payload[1..1 + klen].to_vec();
                    c.out.hot(key, lane_body(0, &p.payload));
                }
            }
            Ok(())
        });
        let c = ctx.clone();
        tasks.spawn(async move {
            while let Some(p) = cold_recv.recv().await? {
                c.out.frame(T_COLD, lane_body(0, &p.payload)).await; // bounded: backpressure
            }
            Ok(())
        });
        tasks.spawn(async move {
            let _keep = (&mut hot_send, &mut cold_send);
            std::future::pending::<()>().await;
            Ok(())
        });
    }

    if let Some(mut video) = video_send {
        let c = ctx.clone();
        tasks.spawn(async move {
            video
                .send
                .send(AVPacket::Codec(CodecPacket {
                    header: CodecPacketHeader { codec: HEVC_FOURCC, rotation: 0, frame_size: 0 },
                }))
                .await?;
            c.video_src.set_active(true);
            let k = &c.video_src.counters;
            loop {
                let (au, idx, arrived) = c.video_src.next().await;
                let pts = idx * 1500; // 90 kHz at 60 fps; receivers only need monotonic
                if au.key {
                    // Plank pattern: empty config marker, then the AU with
                    // VPS/SPS/PPS in band.
                    video
                        .send
                        .send(AVPacket::Media(MediaPacket {
                            header: MediaPacketHeader { is_config: true, is_key: true, pts, size: 0 },
                            payload: Bytes::new(),
                        }))
                        .await?;
                    k.keys.fetch_add(1, Relaxed);
                }
                let size = au.data.len();
                video
                    .send
                    .send(AVPacket::Media(MediaPacket {
                        header: MediaPacketHeader { is_config: false, is_key: au.key, pts, size: size as u32 },
                        payload: au.data,
                    }))
                    .await?;
                let dwell = arrived.elapsed().as_micros() as u64;
                k.dwell_us_sum.fetch_add(dwell, Relaxed);
                k.dwell_us_max.fetch_max(dwell, Relaxed);
                k.aus_out.fetch_add(1, Relaxed);
                k.bytes.fetch_add(size as u64, Relaxed);
            }
        });
    }
    if let Some(mut video) = video_recv {
        let c = ctx.clone();
        tasks.spawn(async move {
            let k = &c.video_sink.counters;
            while let Some(packet) = video.recv.recv().await? {
                match packet {
                    AVPacket::Media(p) if !p.payload.is_empty() => {
                        k.aus_in.fetch_add(1, Relaxed);
                        k.bytes.fetch_add(p.payload.len() as u64, Relaxed);
                        if p.header.is_key {
                            k.keys.fetch_add(1, Relaxed);
                        }
                        c.video_sink.push(p.payload, p.header.is_key);
                    }
                    AVPacket::Hole(_) => {
                        k.holes.fetch_add(1, Relaxed);
                    }
                    _ => {}
                }
            }
            Ok(())
        });
    }

    // Per-second stats while the session lives.
    {
        let c = ctx.clone();
        let stats = conn.stats_provider();
        tasks.spawn(async move {
            let mut tick = tokio::time::interval(Duration::from_secs(1));
            let mut prev = (0u64, 0u64);
            loop {
                tick.tick().await;
                let cs = stats.connection_stats().await;
                let k = if c.role_host { &c.video_sink.counters } else { &c.video_src.counters };
                let frames = if c.role_host { k.aus_in.load(Relaxed) } else { k.aus_out.load(Relaxed) };
                let bytes = k.bytes.load(Relaxed);
                let sent = frames - prev.0;
                let dwell_sum = k.dwell_us_sum.swap(0, Relaxed);
                c.out
                    .event(json!({
                        "event": "stats",
                        "rtt_ms": cs.rtt.map(|r| r.as_secs_f64() * 1000.0),
                        "quic_lost": cs.packets_lost,
                        "video_fps": sent,
                        "video_mbps": (bytes - prev.1) as f64 * 8.0 / 1e6,
                        "video_keys": k.keys.load(Relaxed),
                        "video_holes": k.holes.load(Relaxed),
                        "queue_drops": k.queue_drops.load(Relaxed),
                        "gop_skips": k.gop_skips.load(Relaxed),
                        "dwell_ms_avg": if sent > 0 { dwell_sum as f64 / sent as f64 / 1000.0 } else { 0.0 },
                        "dwell_ms_max": k.dwell_us_max.swap(0, Relaxed) as f64 / 1000.0,
                    }))
                    .await;
                prev = (frames, bytes);
            }
        });
    }

    let result = tokio::select! {
        r = conn.closed() => r.map_err(|e| anyhow!("connection closed: {e:?}")),
        Some(joined) = tasks.join_next() => match joined {
            Ok(r) => r.and(Err(anyhow!("lane ended"))),
            Err(e) => Err(anyhow!("lane panicked: {e}")),
        },
    };
    ctx.inb.connected.store(false, Relaxed);
    ctx.video_src.set_active(false);
    tasks.shutdown().await;
    conn.close();
    result
}

async fn ready_data(ep: kymux_types::DataEndpoint) -> Result<DataProtocol> {
    Ok(tokio::time::timeout(HANDSHAKE, ep.ready()).await.context("lane ready timeout")??)
}

async fn serve_one(ctx: Arc<Ctx>, server: &kynet::common::CommonServer) -> Result<()> {
    let raw = server.accept().await?.ok_or_else(|| anyhow!("listener closed"))?;
    let unauth = tokio::time::timeout(HANDSHAKE, Connection::accept_with_auth(raw))
        .await
        .context("auth timeout")??;
    if unauth.get_auth().token() != ctx.token {
        unauth.reject_authentication();
        bail!("peer token mismatch; rejected");
    }
    let conn = unauth.accept_authentication().await?;
    let (vid, video_ep) = conn.register_video_endpoint(VideoProtocol::UnreliableFec).await?;
    let (cid, control_ep) = conn.register_data_endpoint().await?;
    let (hid, hot_ep) = conn.register_data_endpoint().await?;
    let (oid, cold_ep) = conn.register_data_endpoint().await?;
    if (vid, cid, hid, oid) != (EP_VIDEO, EP_CONTROL, EP_HOT, EP_COLD) {
        bail!("unexpected endpoint ids {vid},{cid},{hid},{oid}");
    }
    let video = tokio::time::timeout(HANDSHAKE, video_ep.ready()).await.context("video ready timeout")??;
    let control = ready_data(control_ep).await?;
    let hot = ready_data(hot_ep).await?;
    let cold = ready_data(cold_ep).await?;
    ctx.out.event(json!({"event": "peer", "peer": 0, "up": true})).await;
    run_lanes(ctx, conn, control, hot, cold, Some(video), None).await
}

#[derive(Debug)]
struct TofuVerifier {
    expected: Option<Vec<u8>>,
    seen: Arc<Mutex<Option<Vec<u8>>>>,
    provider: Arc<rustls::crypto::CryptoProvider>,
}

impl rustls::client::danger::ServerCertVerifier for TofuVerifier {
    fn verify_server_cert(
        &self,
        end_entity: &rustls::pki_types::CertificateDer<'_>,
        _intermediates: &[rustls::pki_types::CertificateDer<'_>],
        _server_name: &rustls::pki_types::ServerName<'_>,
        _ocsp: &[u8],
        _now: rustls::pki_types::UnixTime,
    ) -> std::result::Result<rustls::client::danger::ServerCertVerified, rustls::Error> {
        let hash = ring::digest::digest(&ring::digest::SHA256, end_entity.as_ref());
        *self.seen.lock().unwrap() = Some(hash.as_ref().to_vec());
        if let Some(expected) = &self.expected {
            if expected.as_slice() != hash.as_ref() {
                return Err(rustls::Error::General(format!(
                    "replica certificate changed: pinned {}, got {}",
                    hex::encode(expected),
                    hex::encode(hash.as_ref())
                )));
            }
        }
        Ok(rustls::client::danger::ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &rustls::pki_types::CertificateDer<'_>,
        dss: &rustls::DigitallySignedStruct,
    ) -> std::result::Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls12_signature(message, cert, dss, &self.provider.signature_verification_algorithms)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &rustls::pki_types::CertificateDer<'_>,
        dss: &rustls::DigitallySignedStruct,
    ) -> std::result::Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls13_signature(message, cert, dss, &self.provider.signature_verification_algorithms)
    }

    fn supported_verify_schemes(&self) -> Vec<rustls::SignatureScheme> {
        self.provider.signature_verification_algorithms.supported_schemes()
    }
}

struct HostTarget {
    addr: String,
    fingerprint: Option<Vec<u8>>,
    mtu: u16,
}

async fn connect_one(ctx: Arc<Ctx>, target: &HostTarget) -> Result<()> {
    let addr = target
        .addr
        .to_socket_addrs()
        .with_context(|| format!("resolve {}", target.addr))?
        .next()
        .ok_or_else(|| anyhow!("no address for {}", target.addr))?;
    let seen = Arc::new(Mutex::new(None));
    let provider = rustls::crypto::CryptoProvider::get_default()
        .cloned()
        .ok_or_else(|| anyhow!("no rustls crypto provider"))?;
    let tls = rustls::ClientConfig::builder()
        .dangerous()
        .with_custom_certificate_verifier(Arc::new(TofuVerifier {
            expected: target.fingerprint.clone(),
            seen: seen.clone(),
            provider,
        }))
        .with_no_client_auth();
    let options = kynet::quinn::QuinnClientOptions {
        max_idle_timeout: Some(Duration::from_secs(3)),
        keep_alive_interval: Some(Duration::from_millis(500)),
        max_udp_payload_size: Some(target.mtu),
        certificate_hash: None,
        ..Default::default()
    };
    let raw = tokio::time::timeout(HANDSHAKE, kynet::Connection::quinn_connect(addr, "localhost", Some(tls), &options))
        .await
        .context("QUIC connect timeout")?
        .map_err(|e| anyhow!("QUIC connect: {e:?}"))?;
    let auth = ClientAuth::new(&ctx.token).map_err(|e| anyhow!("token: {e:?}"))?;
    let conn = tokio::time::timeout(HANDSHAKE, Connection::connect_with_auth(raw, &auth))
        .await
        .context("auth timeout")??;
    let video_ep = conn.connect_video_endpoint(EP_VIDEO, VideoProtocol::UnreliableFec)?;
    let control_ep = conn.connect_data_endpoint(EP_CONTROL)?;
    let hot_ep = conn.connect_data_endpoint(EP_HOT)?;
    let cold_ep = conn.connect_data_endpoint(EP_COLD)?;
    let video = tokio::time::timeout(HANDSHAKE, video_ep.ready())
        .await
        .context("video ready timeout (token rejected?)")??;
    let control = ready_data(control_ep).await?;
    let hot = ready_data(hot_ep).await?;
    let cold = ready_data(cold_ep).await?;
    let fingerprint = seen.lock().unwrap().as_deref().map(hex::encode);
    ctx.out
        .event(json!({"event": "peer", "peer": 0, "up": true, "fingerprint": fingerprint,
                      "pinned": target.fingerprint.is_some()}))
        .await;
    run_lanes(ctx, conn, control, hot, cold, None, Some(video)).await
}

#[tokio::main(flavor = "multi_thread", worker_threads = 4)]
async fn main() -> Result<()> {
    let args = Args::parse(USAGE);
    let role = args.require("role", USAGE);
    let role_host = match role.as_str() {
        "host" => true,
        "replica" => false,
        _ => bail!("--role must be host or replica"),
    };
    let token = args.require("token", USAGE);
    let cap_mbps: f64 = args.get("cap-mbps").map(str::parse).transpose()?.unwrap_or(400.0);
    let mtu: u16 = args.get("mtu").map(str::parse).transpose()?.unwrap_or(DEFAULT_MAX_UDP_PAYLOAD);

    kynet::init_crypto();

    let (out_tx, out_rx) = mpsc::channel::<Out>(256);
    let out = Arc::new(Stdout { tx: out_tx, hot: Mutex::new(HashMap::new()), hot_dirty: AtomicBool::new(false) });
    {
        let out = out.clone();
        std::thread::Builder::new().name("stdout".into()).spawn(move || writer_thread(out, out_rx))?;
    }
    let (control_tx, control_rx) = mpsc::channel::<Bytes>(1024);
    let (cold_tx, cold_rx) = mpsc::channel::<Bytes>(256);
    let inb = Arc::new(Inbound {
        control_tx,
        cold_tx,
        hot: Mutex::new(HashMap::new()),
        hot_notify: Notify::new(),
        connected: AtomicBool::new(false),
    });
    let video_src = Arc::new(VideoSource::new());
    let video_sink = Arc::new(VideoSink { clients: Mutex::new(Vec::new()), counters: VideoCounters::default() });
    {
        let (inb, out, video) = (inb.clone(), out.clone(), video_src.clone());
        std::thread::Builder::new().name("stdin".into()).spawn(move || stdin_thread(inb, out, video))?;
    }
    let ctx = Arc::new(Ctx {
        role_host,
        token,
        out: out.clone(),
        inb,
        control_rx: tokio::sync::Mutex::new(control_rx),
        cold_rx: tokio::sync::Mutex::new(cold_rx),
        video_src,
        video_sink: video_sink.clone(),
    });

    if role_host {
        let target = HostTarget {
            addr: args.require("connect", USAGE),
            fingerprint: args
                .get("fingerprint")
                .map(|f| hex::decode(f.to_lowercase().replace(':', "")))
                .transpose()
                .context("--fingerprint must be hex")?,
            mtu,
        };
        if let Some(listen) = args.get("video-listen") {
            let addr: SocketAddr = listen.parse().context("--video-listen must be IP:PORT")?;
            tokio::spawn(video_listener(addr, video_sink, out.clone()));
        }
        let mut backoff = Duration::from_millis(250);
        loop {
            let started = Instant::now();
            let err = connect_one(ctx.clone(), &target).await.err();
            let msg = err.map(|e| format!("{e:#}")).unwrap_or_default();
            out.event(json!({"event": "peer", "peer": 0, "up": false, "reason": msg})).await;
            if started.elapsed() > Duration::from_secs(5) {
                backoff = Duration::from_millis(250);
            }
            tokio::time::sleep(backoff).await;
            backoff = (backoff * 2).min(Duration::from_secs(2));
        }
    } else {
        let listen: SocketAddr = args.require("listen", USAGE).parse().context("--listen must be IP:PORT")?;
        let cert_dir: PathBuf = args.get("cert-dir").map(Into::into).unwrap_or_else(|| {
            std::env::temp_dir().join("qcbridge-helper")
        });
        let (cert, key, fingerprint) = load_or_create_cert(&cert_dir)?;
        let _ = sha256_hex; // fingerprint comes from load_or_create_cert
        let wire_cap_bps = (cap_mbps * 1e6) as u64;
        let options = kynet::common::CommonServerOptions {
            max_idle_timeout: Some(Duration::from_secs(3)),
            keep_alive_interval: Some(Duration::from_millis(500)),
            max_udp_payload_size: Some(mtu),
            congestion_controller_factory: Some(Arc::new(FixedRateControllerFactory { wire_bps: wire_cap_bps })),
            datagram_pacer: Some(Arc::new(kynet::quinn::DatagramPacer::new(wire_cap_bps, Duration::from_millis(2), 0))),
        };
        let server = kynet::Connection::start_server_on_addr(listen, vec![cert], key, &options)
            .map_err(|e| anyhow!("listen on {listen}: {e:?}"))?;
        out.event(json!({"event": "listening", "port": listen.port(), "fingerprint": fingerprint})).await;
        loop {
            let err = serve_one(ctx.clone(), &server).await.err();
            let msg = err.map(|e| format!("{e:#}")).unwrap_or_default();
            out.event(json!({"event": "peer", "peer": 0, "up": false, "reason": msg})).await;
        }
    }
}
