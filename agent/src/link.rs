//! The addon link: one Blender addon attached over a local TCP socket, speaking
//! the length-prefixed frames the S5 helper introduced (see FRAMES below).
//!
//! The link outlives Blender: frames from the network queue (bounded, so QUIC
//! flow control pushes back on the host) until an addon attaches; hot values
//! conflate per key the whole time.
//!
//! FRAMES, both directions:  u32 BE length | u8 type | body
//!   0x01 CONTROL   u8 peer | json bytes
//!   0x02 HOT       u8 peer | u8 keylen | key | value
//!   0x03 COLD      u8 peer | opaque (addon packs header+payload)
//!   0x04 COLD_ACK  u32 BE bytes            agent -> addon: credits returned
//!                                          (bytes of COLD body accepted or dropped)
//!   0x05 FAST      u8 peer | opaque        tier-1 deltas and tombstones: their own
//!                                          QUIC stream, never behind a blob
//!   0x06 FAST_ACK  u32 BE bytes
//!   0x10 CMD       json                    addon -> agent
//!   0x20 EVENT     json                    agent -> addon

use bytes::Bytes;
use serde_json::{Value, json};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering::Relaxed};
use std::sync::{Arc, Condvar, Mutex};
use tokio::sync::{Notify, mpsc};

pub const T_CONTROL: u8 = 0x01;
pub const T_HOT: u8 = 0x02;
pub const T_COLD: u8 = 0x03;
pub const T_COLD_ACK: u8 = 0x04;
pub const T_FAST: u8 = 0x05;
pub const T_FAST_ACK: u8 = 0x06;
pub const T_CMD: u8 = 0x10;
pub const T_EVENT: u8 = 0x20;

const MAX_FRAME: usize = 256 << 20;

pub enum Out {
    Frame(u8, Bytes),
    Wake,
}

/// Agent -> addon side of the link.
///
/// Two queues toward the addon: `tx` for cold frames (bounded, so a slow
/// addon pushes back on QUIC) and `prio_tx` for control, acks and events,
/// drained first by the writer — a pong must not wait behind a 4 MiB chunk
/// and trip the host's 3 s liveness window (DESIGN-NOTES sync B4).
pub struct Link {
    pub tx: mpsc::Sender<Out>,
    prio_tx: mpsc::Sender<Out>,
    hot: Mutex<HashMap<Vec<u8>, Bytes>>,
    hot_dirty: AtomicBool,
    sink: Mutex<Option<TcpStream>>,
    sink_cv: Condvar,
    pub attached: AtomicBool,
    /// Control clients (the settings window, since 2026-09-24): they get
    /// every event and may send commands, and carry no lanes. Any number,
    /// beside the one addon.
    aux: Mutex<Vec<(u64, TcpStream)>>,
    aux_seq: std::sync::atomic::AtomicU64,
}

impl Link {
    pub fn new(tx: mpsc::Sender<Out>, prio_tx: mpsc::Sender<Out>) -> Self {
        Self {
            tx,
            prio_tx,
            hot: Mutex::new(HashMap::new()),
            hot_dirty: AtomicBool::new(false),
            sink: Mutex::new(None),
            sink_cv: Condvar::new(),
            attached: AtomicBool::new(false),
            aux: Mutex::new(Vec::new()),
            aux_seq: std::sync::atomic::AtomicU64::new(1),
        }
    }

    fn aux_add(&self, stream: TcpStream) -> u64 {
        let id = self.aux_seq.fetch_add(1, Relaxed);
        self.aux.lock().unwrap().push((id, stream));
        id
    }

    fn aux_remove(&self, id: u64) {
        self.aux.lock().unwrap().retain(|(i, _)| *i != id);
    }

    pub fn control_clients(&self) -> usize {
        self.aux.lock().unwrap().len()
    }

    /// Every control client gets the event; a failed write drops that client.
    fn aux_event(&self, body: &[u8]) {
        let mut aux = self.aux.lock().unwrap();
        aux.retain_mut(|(_, s)| write_frame(s, T_EVENT, body).and_then(|_| s.flush()).is_ok());
    }

    fn is_prio(kind: u8) -> bool {
        matches!(kind, T_CONTROL | T_COLD_ACK | T_FAST | T_FAST_ACK | T_EVENT)
    }

    pub async fn frame(&self, kind: u8, body: Bytes) {
        if Self::is_prio(kind) {
            let _ = self.prio_tx.send(Out::Frame(kind, body)).await;
            let _ = self.tx.try_send(Out::Wake);
        } else {
            let _ = self.tx.send(Out::Frame(kind, body)).await;
        }
    }

    pub fn frame_blocking(&self, kind: u8, body: Bytes) {
        if Self::is_prio(kind) {
            let _ = self.prio_tx.blocking_send(Out::Frame(kind, body));
            let _ = self.tx.try_send(Out::Wake);
        } else {
            let _ = self.tx.blocking_send(Out::Frame(kind, body));
        }
    }

    pub async fn event(&self, v: Value) {
        self.frame(T_EVENT, Bytes::from(v.to_string())).await;
    }

    pub fn event_blocking(&self, v: Value) {
        self.frame_blocking(T_EVENT, Bytes::from(v.to_string()));
    }

    /// Non-blocking (safe from async tasks); drops the event if the queue is full.
    pub fn event_try(&self, v: Value) {
        let _ = self.prio_tx.try_send(Out::Frame(T_EVENT, Bytes::from(v.to_string())));
        let _ = self.tx.try_send(Out::Wake);
    }

    /// Events the addon must see even if the queue is full (attach replies).
    pub fn event_direct(&self, v: Value) {
        let mut sink = self.sink.lock().unwrap();
        if let Some(s) = sink.as_mut() {
            let _ = write_frame(s, T_EVENT, v.to_string().as_bytes()).and_then(|_| s.flush());
        }
    }

    /// An event to every listener there is right now: the addon if one is
    /// attached, and the control clients. Nothing waits for an addon —
    /// state is in the attach reply, and a settings window with no Blender
    /// open must still see config and peers events.
    fn write_event_now(&self, body: &[u8]) {
        {
            let mut sink = self.sink.lock().unwrap();
            if let Some(s) = sink.as_mut() {
                if write_frame(s, T_EVENT, body).is_err() {
                    *sink = None;
                    self.attached.store(false, Relaxed);
                }
            }
        }
        self.aux_event(body);
    }

    pub fn hot(&self, key: Vec<u8>, body: Bytes) {
        self.hot.lock().unwrap().insert(key, body);
        self.hot_dirty.store(true, Relaxed);
        let _ = self.tx.try_send(Out::Wake);
    }

    fn set_sink(&self, stream: Option<TcpStream>) {
        let mut sink = self.sink.lock().unwrap();
        *sink = stream;
        self.attached.store(sink.is_some(), Relaxed);
        self.sink_cv.notify_all();
    }

    /// Blocks until an addon is attached, then writes. Returns false when the
    /// write failed (the addon went away); the frame is lost, like a socket.
    fn write_when_attached(&self, kind: u8, body: &[u8]) -> bool {
        let mut sink = self.sink.lock().unwrap();
        while sink.is_none() {
            sink = self.sink_cv.wait(sink).unwrap();
        }
        let s = sink.as_mut().unwrap();
        let ok = write_frame(s, kind, body).is_ok();
        if !ok {
            *sink = None;
            self.attached.store(false, Relaxed);
        }
        ok
    }

    fn flush(&self) {
        if let Some(s) = self.sink.lock().unwrap().as_mut() {
            let _ = s.flush();
        }
    }
}

pub fn write_frame(w: &mut impl Write, kind: u8, body: &[u8]) -> std::io::Result<()> {
    w.write_all(&((body.len() + 1) as u32).to_be_bytes())?;
    w.write_all(&[kind])?;
    w.write_all(body)
}

pub fn writer_thread(link: Arc<Link>, mut rx: mpsc::Receiver<Out>, mut prio_rx: mpsc::Receiver<Out>) {
    while let Some(item) = rx.blocking_recv() {
        // Priority frames first, always: whatever woke us, a queued pong,
        // ack or event goes out before the next cold chunk.
        while let Ok(Out::Frame(kind, body)) = prio_rx.try_recv() {
            if kind == T_EVENT { link.write_event_now(&body) } else { link.write_when_attached(kind, &body); }
        }
        match item {
            Out::Frame(kind, body) => {
                if kind == T_EVENT { link.write_event_now(&body) } else { link.write_when_attached(kind, &body); }
            }
            Out::Wake => {}
        }
        if link.hot_dirty.swap(false, Relaxed) {
            let drained: Vec<Bytes> = link.hot.lock().unwrap().drain().map(|(_, v)| v).collect();
            for body in drained {
                if !link.attached.load(Relaxed) {
                    link.hot_dirty.store(true, Relaxed); // keep for the next addon
                    break;
                }
                link.write_when_attached(T_HOT, &body);
            }
        }
        link.flush();
    }
}

/// Addon -> network queues. They outlive sessions and addons.
pub struct Inbound {
    pub control_tx: mpsc::Sender<Bytes>,
    pub cold_tx: mpsc::Sender<Bytes>,
    pub fast_tx: mpsc::Sender<Bytes>,
    pub hot: Mutex<HashMap<Vec<u8>, Bytes>>,
    pub hot_notify: Notify,
    pub connected: AtomicBool,
}

fn read_exact(r: &mut impl Read, buf: &mut [u8]) -> bool {
    r.read_exact(buf).is_ok()
}

/// Reads one attached addon until it goes away. CMD frames go to `on_cmd`.
fn reader_loop(mut r: impl Read, inb: &Inbound, link: &Link, on_cmd: &(dyn Fn(&Value) + Sync)) {
    let mut head = [0u8; 5];
    loop {
        if !read_exact(&mut r, &mut head) {
            return;
        }
        let len = u32::from_be_bytes([head[0], head[1], head[2], head[3]]) as usize;
        if len == 0 || len > MAX_FRAME {
            return;
        }
        let kind = head[4];
        let mut body = vec![0u8; len - 1];
        if !read_exact(&mut r, &mut body) {
            return;
        }
        let body = Bytes::from(body);
        match kind {
            T_CONTROL => {
                let _ = inb.control_tx.try_send(body);
            }
            T_HOT => {
                if body.len() >= 2 {
                    let klen = body[1] as usize;
                    if body.len() >= 2 + klen {
                        inb.hot.lock().unwrap().insert(body[2..2 + klen].to_vec(), body);
                        inb.hot_notify.notify_one();
                    }
                }
            }
            T_COLD => {
                let len = body.len() as u32;
                let sent = inb.connected.load(Relaxed) && inb.cold_tx.try_send(body).is_ok();
                if !sent {
                    // Credit it back so the addon never stalls on a dead
                    // session — and say so: the frame is gone, not delivered.
                    link.event_try(serde_json::json!({"event": "cold_dropped", "n": 1}));
                    link.frame_blocking(T_COLD_ACK, Bytes::copy_from_slice(&len.to_be_bytes()));
                }
            }
            T_FAST => {
                let len = body.len() as u32;
                let sent = inb.connected.load(Relaxed) && inb.fast_tx.try_send(body).is_ok();
                if !sent {
                    link.event_try(serde_json::json!({"event": "cold_dropped", "n": 1, "lane": "fast"}));
                    link.frame_blocking(T_FAST_ACK, Bytes::copy_from_slice(&len.to_be_bytes()));
                }
            }
            T_CMD => match serde_json::from_slice::<Value>(&body) {
                Ok(cmd) => on_cmd(&cmd),
                Err(e) => crate::log!("[link] bad CMD json: {e}"),
            },
            other => crate::log!("[link] unknown frame type {other:#x}"),
        }
    }
}

/// Commands only: what a control client may send.
fn reader_loop_cmd(mut r: impl Read, on_cmd: &(dyn Fn(&Value) + Sync)) {
    let mut head = [0u8; 5];
    loop {
        if !read_exact(&mut r, &mut head) {
            return;
        }
        let len = u32::from_be_bytes([head[0], head[1], head[2], head[3]]) as usize;
        if len == 0 || len > MAX_FRAME {
            return;
        }
        let kind = head[4];
        let mut body = vec![0u8; len - 1];
        if !read_exact(&mut r, &mut body) {
            return;
        }
        if kind == T_CMD {
            match serde_json::from_slice::<Value>(&body) {
                Ok(cmd) => on_cmd(&cmd),
                Err(e) => crate::log!("[link] bad CMD json: {e}"),
            }
        }
    }
}

/// Accept loop for the local socket. The first frame must be
/// `{"cmd":"attach","secret":...}`. One addon at a time (it owns the lanes);
/// any number of control clients (`"kind":"control"`), which get events
/// and send commands. Each client is served on its own thread, so the
/// settings window attaches while Blender is attached, and the other way
/// round. `on_attach`/`on_detach` let the lifecycle react to the addon;
/// `on_cmd` gets every later command from anyone.
pub fn serve_local(
    listener: TcpListener,
    secret: String,
    inb: Arc<Inbound>,
    link: Arc<Link>,
    on_attach: Arc<dyn Fn() -> Value + Send + Sync>,
    on_detach: Arc<dyn Fn() + Send + Sync>,
    on_cmd: Arc<dyn Fn(&Value) + Send + Sync>,
) {
    for stream in listener.incoming() {
        let Ok(mut stream) = stream else { continue };
        let _ = stream.set_nodelay(true);
        // Attach handshake, synchronously on the new stream.
        let mut head = [0u8; 5];
        if !read_exact(&mut stream, &mut head) {
            continue;
        }
        let len = u32::from_be_bytes([head[0], head[1], head[2], head[3]]) as usize;
        if head[4] != T_CMD || len == 0 || len > 4096 {
            continue;
        }
        let mut body = vec![0u8; len - 1];
        if !read_exact(&mut stream, &mut body) {
            continue;
        }
        let cmd: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
        if cmd.get("cmd").and_then(Value::as_str) != Some("attach")
            || cmd.get("secret").and_then(Value::as_str) != Some(secret.as_str())
        {
            let _ = write_frame(&mut stream, T_EVENT, json!({"event": "rejected"}).to_string().as_bytes());
            continue;
        }
        let Ok(reader) = stream.try_clone() else { continue };
        if cmd.get("kind").and_then(Value::as_str) == Some("control") {
            let mut reply = on_attach();
            reply["control"] = json!(true);
            if write_frame(&mut stream, T_EVENT, reply.to_string().as_bytes()).and_then(|_| stream.flush()).is_err() {
                continue;
            }
            let id = link.aux_add(stream);
            let (link, on_cmd) = (link.clone(), on_cmd.clone());
            let _ = std::thread::Builder::new().name("local-control".into()).spawn(move || {
                reader_loop_cmd(reader, &*on_cmd);
                link.aux_remove(id);
            });
            continue;
        }
        if link.attached.swap(true, Relaxed) {
            let _ = write_frame(&mut stream, T_EVENT, json!({"event": "rejected", "reason": "already attached"}).to_string().as_bytes());
            continue;
        }
        link.set_sink(Some(stream));
        link.event_direct(on_attach());
        let (inb, link, on_cmd, on_detach) = (inb.clone(), link.clone(), on_cmd.clone(), on_detach.clone());
        let _ = std::thread::Builder::new().name("local-addon".into()).spawn(move || {
            reader_loop(reader, &inb, &link, &*on_cmd);
            link.set_sink(None);
            on_detach();
        });
    }
}
