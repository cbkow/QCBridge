//! The window's side of the local socket: attach as a control client (the
//! agent keeps the lanes for Blender), send commands, receive events on a
//! thread. The frame format is the addon's (`link.rs`).

use anyhow::{Context, Result, anyhow};
use serde_json::{Value, json};
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering::Relaxed};
use std::sync::mpsc::{Receiver, Sender, channel};
use std::sync::Mutex;

const T_CMD: u8 = 0x10;
const T_EVENT: u8 = 0x20;

pub struct Client {
    sock: Mutex<TcpStream>,
    rx: Mutex<Receiver<Value>>,
    next_req: AtomicU64,
    /// The attach reply, kept for the fields that arrive nowhere else.
    pub attached: Value,
}

fn read_exact(r: &mut impl Read, buf: &mut [u8]) -> bool {
    r.read_exact(buf).is_ok()
}

fn read_frame(r: &mut impl Read) -> Option<(u8, Vec<u8>)> {
    let mut head = [0u8; 5];
    if !read_exact(r, &mut head) {
        return None;
    }
    let len = u32::from_be_bytes([head[0], head[1], head[2], head[3]]) as usize;
    if len == 0 || len > 64 * 1024 * 1024 {
        return None;
    }
    let mut body = vec![0u8; len - 1];
    if !read_exact(r, &mut body) {
        return None;
    }
    Some((head[4], body))
}

fn write_frame(w: &mut impl Write, kind: u8, body: &[u8]) -> std::io::Result<()> {
    w.write_all(&((body.len() + 1) as u32).to_be_bytes())?;
    w.write_all(&[kind])?;
    w.write_all(body)?;
    w.flush()
}

/// Where the agent for `role` in `base` listens: the addon's own lookup.
pub fn socket_info(base: &Path, role: &str) -> Result<(u16, String)> {
    let path = base.join("agent.json");
    let text = std::fs::read_to_string(&path).with_context(|| format!("no agent registered in {}", base.display()))?;
    let doc: Value = serde_json::from_str(&text).context("agent.json is not JSON")?;
    let entry = doc.get(role).ok_or_else(|| anyhow!("no {role} agent registered in {}", base.display()))?;
    let port = entry.get("port").and_then(Value::as_u64).ok_or_else(|| anyhow!("agent.json has no port"))? as u16;
    let secret = entry.get("secret").and_then(Value::as_str).unwrap_or("").to_string();
    Ok((port, secret))
}

impl Client {
    pub fn connect(port: u16, secret: &str) -> Result<Client> {
        let mut sock = TcpStream::connect(("127.0.0.1", port)).context("connect to the agent")?;
        let _ = sock.set_nodelay(true);
        write_frame(&mut sock, T_CMD, json!({"cmd": "attach", "secret": secret, "kind": "control"}).to_string().as_bytes())?;
        let (kind, body) = read_frame(&mut sock).ok_or_else(|| anyhow!("the agent closed the socket at attach"))?;
        if kind != T_EVENT {
            return Err(anyhow!("unexpected first frame"));
        }
        let attached: Value = serde_json::from_slice(&body)?;
        if attached.get("event").and_then(Value::as_str) != Some("attached") {
            return Err(anyhow!("attach refused: {}", attached.get("reason").and_then(Value::as_str).unwrap_or("bad secret")));
        }
        let (tx, rx): (Sender<Value>, Receiver<Value>) = channel();
        let mut reader = sock.try_clone()?;
        std::thread::Builder::new().name("settings-rx".into()).spawn(move || {
            while let Some((kind, body)) = read_frame(&mut reader) {
                if kind == T_EVENT {
                    if let Ok(v) = serde_json::from_slice::<Value>(&body) {
                        if tx.send(v).is_err() {
                            return;
                        }
                    }
                }
            }
            let _ = tx.send(json!({"event": "_closed"}));
        })?;
        Ok(Client { sock: Mutex::new(sock), rx: Mutex::new(rx), next_req: AtomicU64::new(1), attached })
    }

    /// Send a command; the `req` it carries is returned so a reply can be
    /// matched. A failed write is reported through `poll` as `_closed`.
    pub fn cmd(&self, mut v: Value) -> u64 {
        let req = self.next_req.fetch_add(1, Relaxed);
        v["req"] = json!(req);
        let mut s = self.sock.lock().unwrap();
        let _ = write_frame(&mut *s, T_CMD, v.to_string().as_bytes());
        req
    }

    pub fn set_config(&self, patch: Value) -> u64 {
        self.cmd(json!({"cmd": "set_config", "set": patch}))
    }

    /// Everything that arrived since the last poll.
    pub fn poll(&self) -> Vec<Value> {
        let rx = self.rx.lock().unwrap();
        let mut out = Vec::new();
        while let Ok(v) = rx.try_recv() {
            out.push(v);
        }
        out
    }
}
