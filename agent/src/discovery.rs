//! Finding peers. Three ways in, in order of automation:
//!
//!   1. **Direct IP** — a unicast `{"t":"q"}` to `host:4246` gets the beacon
//!      back, so the name and certificate fingerprint are known before
//!      pairing. Always on unless the mode is `off`. This is the VPN path,
//!      and the primary one.
//!   2. **Phonebook** — when `phonebook = "<shared dir>"` is set, we write
//!      our beacon to `<dir>/qcbridge/<name>.json` (temp + rename, with a
//!      timestamp) and read everyone else's. MinRender's pattern; it works
//!      over the VPN because QCBridge already assumes shared storage.
//!   3. **Multicast** — `discoverable` also joins `239.42.0.4` and announces.
//!      A LAN convenience on top of the other two, never the only path.
//!
//! The group and port continue the studio's family so the beacons read as
//! siblings: MinRender `239.42.0.1:4243`, UFB `239.42.0.2:4244` (legacy) and
//! `239.42.0.3:4245`. Same JSON idiom too (`t`, `n`, `ip`, `port`). The
//! socket setup mirrors UFB's `udp_notify.rs`.
//!
//! Only a replica binds, announces or writes the phonebook: nothing pairs
//! *to* a host, and a host's `discover` uses an ephemeral socket. This is
//! also what keeps a host and a replica on one machine out of each other's
//! way — two sockets sharing 4246 with SO_REUSEPORT both receive multicast,
//! but a unicast probe is delivered to only one of them, so a host on the
//! port would silently eat probes meant for the replica beside it.
//!
//! The beacon never carries the token. Discovery removes typing; it must
//! not grant trust.

use crate::config::SharedConfig;
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::net::{IpAddr, Ipv4Addr, SocketAddr, SocketAddrV4};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::net::UdpSocket;
use tokio::sync::watch;

pub const GROUP: Ipv4Addr = Ipv4Addr::new(239, 42, 0, 4);
pub const PORT: u16 = 4246;
/// UFB's cap; keeps every datagram well inside one MTU.
const MAX_DATAGRAM: usize = 1400;
const ANNOUNCE_EVERY: Duration = Duration::from_secs(10);
/// A phonebook entry older than this is a machine that did not say goodbye.
const PHONEBOOK_STALE: Duration = Duration::from_secs(60);

/// What a peer learns about us. `paired` lets a picker show which
/// replicas are already in a session.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Beacon {
    pub t: String,
    pub n: String,
    pub role: String,
    pub ip: String,
    pub port: u16,
    pub fp: String,
    pub v: String,
    pub paired: bool,
    /// Milliseconds since the epoch; only the phonebook uses it.
    #[serde(default)]
    pub ts: u64,
}

impl Beacon {
    pub fn encode(&self) -> Option<Vec<u8>> {
        let b = serde_json::to_vec(self).ok()?;
        (b.len() <= MAX_DATAGRAM).then_some(b)
    }

    pub fn decode(bytes: &[u8]) -> Option<Beacon> {
        let b: Beacon = serde_json::from_slice(bytes).ok()?;
        (b.t == "hb").then_some(b)
    }
}

/// The facts about ourselves that do not live in the config.
pub struct Identity {
    pub role: String,
    pub listen_port: u16,
    pub fingerprint: String,
    pub version: String,
    /// Read live: "is a session up right now".
    pub paired: Arc<dyn Fn() -> bool + Send + Sync>,
}

/// The address a peer on this network would reach us at: the outbound
/// interface's. A UDP connect sends nothing, it only picks a route.
pub fn local_ip() -> String {
    std::net::UdpSocket::bind("0.0.0.0:0")
        .and_then(|s| s.connect("10.255.255.255:1").map(|_| s))
        .and_then(|s| s.local_addr())
        .map(|a| a.ip().to_string())
        .unwrap_or_else(|_| "0.0.0.0".into())
}

fn now_ms() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0)
}

fn beacon_for(cfg: &SharedConfig, id: &Identity) -> Beacon {
    Beacon {
        t: "hb".into(),
        n: cfg.with(|c| c.display_name()),
        role: id.role.clone(),
        ip: local_ip(),
        port: id.listen_port,
        fp: id.fingerprint.clone(),
        v: id.version.clone(),
        paired: (id.paired)(),
        ts: now_ms(),
    }
}

// ---- socket --------------------------------------------------------------

/// UFB's setup, line for line, plus SO_REUSEPORT on Unix: macOS needs both
/// for two processes on one port, which is the host+replica dev case.
fn bind_udp(port: u16, join_group: bool) -> std::io::Result<UdpSocket> {
    use socket2::{Domain, Protocol, Socket, Type};
    let sock = Socket::new(Domain::IPV4, Type::DGRAM, Some(Protocol::UDP))?;
    sock.set_reuse_address(true)?;
    #[cfg(unix)]
    sock.set_reuse_port(true)?;
    sock.set_nonblocking(true)?;
    sock.bind(&SocketAddrV4::new(Ipv4Addr::UNSPECIFIED, port).into())?;
    if join_group {
        sock.join_multicast_v4(&GROUP, &Ipv4Addr::UNSPECIFIED)?;
        sock.set_multicast_ttl_v4(1)?;
        sock.set_multicast_loop_v4(true)?; // so two agents on one box see each other
    }
    UdpSocket::from_std(sock.into())
}

// ---- the beacon service -------------------------------------------------

/// Owns the responder/announcer task. Mode changes go through a watch
/// channel; the task rebinds on change, which is cheaper and safer than
/// mutating a live socket.
pub struct Service {
    mode_tx: watch::Sender<String>,
}

impl Service {
    pub fn start(rt: &tokio::runtime::Handle, cfg: SharedConfig, id: Arc<Identity>) -> Service {
        let (mode_tx, mode_rx) = watch::channel(cfg.with(|c| c.discovery.clone()));
        rt.spawn(run(cfg, id, mode_rx));
        Service { mode_tx }
    }

    pub fn set_mode(&self, mode: &str) {
        let _ = self.mode_tx.send(mode.to_string());
    }
}

async fn run(cfg: SharedConfig, id: Arc<Identity>, mut mode_rx: watch::Receiver<String>) {
    if id.role != "replica" {
        return; // see the module note: hosts are never on the port
    }
    loop {
        let mode = mode_rx.borrow_and_update().clone();
        if mode == "off" {
            phonebook_remove(&cfg);
            if mode_rx.changed().await.is_err() { return; }
            continue;
        }
        let discoverable = mode == "discoverable";
        let sock = match bind_udp(PORT, discoverable) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("[discovery] cannot bind udp/{PORT}: {e}");
                if mode_rx.changed().await.is_err() { return; }
                continue;
            }
        };
        eprintln!("[discovery] {} on udp/{PORT}", if discoverable { "answering and announcing" } else { "answering direct probes" });
        phonebook_write(&cfg, &id);
        let mut announce = tokio::time::interval(ANNOUNCE_EVERY);
        let mut buf = [0u8; 2048];
        loop {
            tokio::select! {
                r = sock.recv_from(&mut buf) => {
                    let Ok((n, from)) = r else { break };
                    if is_query(&buf[..n]) {
                        if let Some(b) = beacon_for(&cfg, &id).encode() {
                            let _ = sock.send_to(&b, from).await;
                        }
                    }
                }
                _ = announce.tick() => {
                    if discoverable {
                        if let Some(b) = beacon_for(&cfg, &id).encode() {
                            let _ = sock.send_to(&b, SocketAddr::V4(SocketAddrV4::new(GROUP, PORT))).await;
                        }
                    }
                    phonebook_write(&cfg, &id); // refresh the timestamp
                }
                changed = mode_rx.changed() => {
                    if changed.is_err() { return; }
                    if discoverable {
                        let bye = json!({"t": "bye", "n": cfg.with(|c| c.display_name())}).to_string();
                        let _ = sock.send_to(bye.as_bytes(), SocketAddr::V4(SocketAddrV4::new(GROUP, PORT))).await;
                    }
                    break; // rebind with the new mode
                }
            }
        }
    }
}

fn is_query(bytes: &[u8]) -> bool {
    serde_json::from_slice::<serde_json::Value>(bytes)
        .ok()
        .and_then(|v| v.get("t").and_then(|t| t.as_str()).map(|t| t == "q"))
        .unwrap_or(false)
}

// ---- asking --------------------------------------------------------------

/// Who is asking, so the answer never includes them. A replica has a
/// fingerprint; a host has none, so it goes by name and role. Across two
/// machines a probe can never reach yourself, but on one box — or through
/// the phonebook — it can.
#[derive(Clone, Default)]
pub struct SelfId {
    pub fp: String,
    pub name: String,
    pub role: String,
}

impl SelfId {
    pub fn is_me(&self, b: &Beacon) -> bool {
        if !self.fp.is_empty() || !b.fp.is_empty() {
            return !self.fp.is_empty() && self.fp == b.fp;
        }
        self.name == b.n && self.role == b.role
    }
}

/// Probe one address, or sweep the group. Uses its own ephemeral socket so
/// it never fights the responder for the port. `port` is a parameter so
/// tests stay off 4246.
pub async fn discover(target: Option<&str>, port: u16, wait: Duration, me: &SelfId) -> Vec<Beacon> {
    let Ok(sock) = bind_udp(0, false) else { return vec![] };
    let q = br#"{"t":"q"}"#;
    let dest: Option<SocketAddr> = match target {
        Some(t) => {
            let t = if t.contains(':') { t.to_string() } else { format!("{t}:{port}") };
            tokio::net::lookup_host(t).await.ok().and_then(|mut it| it.next())
        }
        None => {
            let _ = sock.set_multicast_ttl_v4(1);
            Some(SocketAddr::V4(SocketAddrV4::new(GROUP, port)))
        }
    };
    let Some(dest) = dest else { return vec![] };
    if sock.send_to(q, dest).await.is_err() {
        return vec![];
    }
    let mut found: Vec<Beacon> = vec![];
    let mut buf = [0u8; 2048];
    let deadline = tokio::time::Instant::now() + wait;
    loop {
        let left = deadline.saturating_duration_since(tokio::time::Instant::now());
        if left.is_zero() { break; }
        match tokio::time::timeout(left, sock.recv_from(&mut buf)).await {
            Ok(Ok((n, from))) => {
                if let Some(mut b) = Beacon::decode(&buf[..n]) {
                    // Trust the address it answered from over what it thinks
                    // its IP is: NAT and multi-homing lie, the socket does not.
                    if let IpAddr::V4(ip) = from.ip() { b.ip = ip.to_string(); }
                    if me.is_me(&b) { continue; } // our own responder answered us
                    if !found.iter().any(|f| f.fp == b.fp && f.port == b.port) {
                        found.push(b);
                    }
                    if target.is_some() { break; } // a probe wants one answer
                }
            }
            _ => break,
        }
    }
    found
}

// ---- the phonebook -------------------------------------------------------

fn phonebook_dir(cfg: &SharedConfig) -> Option<PathBuf> {
    let d = cfg.with(|c| c.phonebook.clone());
    (!d.trim().is_empty()).then(|| Path::new(&d).join("qcbridge"))
}

fn entry_name(name: &str) -> String {
    let safe: String = name.chars().map(|c| if c.is_alphanumeric() || c == '-' || c == '_' { c } else { '_' }).collect();
    format!("{safe}.json")
}

/// Temp-then-rename, so a reader never sees a half-written file.
pub fn phonebook_write_at(dir: &Path, b: &Beacon) -> std::io::Result<()> {
    std::fs::create_dir_all(dir)?;
    let path = dir.join(entry_name(&b.n));
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, serde_json::to_vec_pretty(b)?)?;
    std::fs::rename(&tmp, &path)
}

fn phonebook_write(cfg: &SharedConfig, id: &Identity) {
    if let Some(dir) = phonebook_dir(cfg) {
        if let Err(e) = phonebook_write_at(&dir, &beacon_for(cfg, id)) {
            eprintln!("[discovery] phonebook write failed: {e}");
        }
    }
}

pub fn phonebook_remove(cfg: &SharedConfig) {
    if let Some(dir) = phonebook_dir(cfg) {
        let _ = std::fs::remove_file(dir.join(entry_name(&cfg.with(|c| c.display_name()))));
    }
}

/// Everyone in the phonebook who has been seen recently, except ourselves.
pub fn phonebook_scan_at(dir: &Path, me: &SelfId, now: u64) -> Vec<Beacon> {
    let Ok(entries) = std::fs::read_dir(dir) else { return vec![] };
    let mut out = vec![];
    for e in entries.flatten() {
        let p = e.path();
        if p.extension().and_then(|x| x.to_str()) != Some("json") { continue; }
        let Ok(text) = std::fs::read_to_string(&p) else { continue };
        let Ok(b) = serde_json::from_str::<Beacon>(&text) else { continue };
        if me.is_me(&b) { continue; }
        if now.saturating_sub(b.ts) > PHONEBOOK_STALE.as_millis() as u64 { continue; }
        out.push(b);
    }
    out
}

pub fn phonebook_scan(cfg: &SharedConfig, me: &SelfId) -> Vec<Beacon> {
    phonebook_dir(cfg).map(|d| phonebook_scan_at(&d, me, now_ms())).unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> Beacon {
        Beacon { t: "hb".into(), n: "Studio Cycles Box".into(), role: "replica".into(),
                 ip: "10.0.0.5".into(), port: 19990, fp: "ab".repeat(32), v: "0.2.0".into(),
                 paired: false, ts: 1_700_000_000_000 }
    }

    #[test]
    fn beacon_round_trips_and_only_hb_decodes() {
        let b = sample();
        let bytes = b.encode().expect("fits");
        assert_eq!(Beacon::decode(&bytes), Some(b));
        assert_eq!(Beacon::decode(br#"{"t":"q"}"#), None);
        assert!(is_query(br#"{"t":"q"}"#));
        assert!(!is_query(&bytes));
    }

    #[test]
    fn a_beacon_too_big_for_one_datagram_is_refused() {
        let mut b = sample();
        b.n = "x".repeat(MAX_DATAGRAM);
        assert!(b.encode().is_none());
    }

    #[test]
    fn phonebook_writes_scans_and_drops_the_stale() {
        let dir = std::env::temp_dir().join(format!("qcb-pb-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let fresh = sample();
        let mut stale = sample();
        stale.n = "Old Box".into(); stale.fp = "cd".repeat(32); stale.ts = fresh.ts - 120_000;
        let mut me = sample();
        me.n = "Me".into(); me.fp = "ef".repeat(32);
        for b in [&fresh, &stale, &me] { phonebook_write_at(&dir, b).unwrap(); }
        assert!(!dir.join("Studio_Cycles_Box.json.tmp").exists(), "temp file renamed away");
        let seen = phonebook_scan_at(&dir, &SelfId { fp: me.fp.clone(), ..Default::default() }, fresh.ts + 1000);
        let names: Vec<_> = seen.iter().map(|b| b.n.as_str()).collect();
        assert_eq!(names, vec!["Studio Cycles Box"], "fresh kept, stale dropped, self excluded");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[tokio::test]
    async fn a_unicast_probe_gets_the_beacon_back() {
        // A responder on an ephemeral port, exactly as the service runs it,
        // then a probe at it: the direct-IP path end to end on loopback.
        let sock = bind_udp(0, false).unwrap();
        let port = sock.local_addr().unwrap().port();
        let answer = sample();
        let responder = tokio::spawn(async move {
            let mut buf = [0u8; 2048];
            let (n, from) = sock.recv_from(&mut buf).await.unwrap();
            assert!(is_query(&buf[..n]));
            sock.send_to(&answer.encode().unwrap(), from).await.unwrap();
        });
        let found = discover(Some("127.0.0.1"), port, Duration::from_secs(2), &SelfId::default()).await;
        responder.await.unwrap();
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].n, "Studio Cycles Box");
        assert_eq!(found[0].ip, "127.0.0.1", "the answering address wins over the claimed one");
        assert_eq!(found[0].fp, "ab".repeat(32));
    }
}
