//! One QUIC connection = one session. Three lanes, one stream each:
//!   control (bidi, host ⇄ replica) · hot (uni, host → replica) · cold (uni, host → replica)
//!
//! Video does NOT ride this connection. The replica's encoder sends SRT and
//! QCView opens the `srt://` URL directly; the 2026-09-22 mux-tax bench
//! measured the old "re-serve it through the host agent" leg at ~15 ms of
//! pure cost, and QCView's LiveStreamDecoder opens SRT without help.
//!
//! Each lane is a length-prefixed message stream (`u32 BE len | payload`),
//! the same framing shape the addon link uses, preceded by a one-byte lane
//! tag so the acceptor can demux regardless of arrival order.

use crate::link::{Inbound, Link, T_COLD, T_COLD_ACK, T_CONTROL, T_FAST, T_FAST_ACK};
use crate::load_or_create_cert;
use crate::video::{VideoSink, VideoSource};
use anyhow::{Context, Result, anyhow, bail};
use bytes::{BufMut, Bytes, BytesMut};
use quinn::crypto::rustls::{QuicClientConfig, QuicServerConfig};
use serde_json::json;
use std::net::{SocketAddr, ToSocketAddrs};
use std::path::Path;
use std::sync::atomic::Ordering::Relaxed;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::sync::mpsc;

const LANE_CONTROL: u8 = 1;
const LANE_HOT: u8 = 2;
const LANE_COLD: u8 = 3;
const LANE_FAST: u8 = 4;
const HANDSHAKE: Duration = Duration::from_secs(5);
/// Cold blobs are chunked by the addon, but the addon link tolerates frames
/// this large and a lane should not be the thing that truncates one.
const MAX_LANE_FRAME: usize = 256 << 20;
const ALPN: &[u8] = b"qcbridge/1";
/// Close code for a rejected token. The peer sees it as an application close
/// with the reason below, immediately — not as a handshake that hangs.
const CLOSE_TOKEN: u32 = 1;
pub const DEFAULT_MTU: u16 = crate::DEFAULT_MAX_UDP_PAYLOAD;

/// What the lifecycle wants to know about the peer.
pub trait PeerObserver: Send + Sync {
    fn peer_up(&self, fingerprint: Option<String>);
    fn peer_down(&self, reason: String);
    /// Replica: the host said goodbye (sniffed from the control lane).
    fn goodbye(&self);
}

pub struct Ctx {
    pub role_host: bool,
    /// Read at each handshake, so a token set at runtime is the one checked
    /// and sent; a copy taken at start would have needed a restart.
    pub cfg: crate::config::SharedConfig,
    pub link: Arc<Link>,
    pub inb: Arc<Inbound>,
    pub control_rx: tokio::sync::Mutex<mpsc::Receiver<Bytes>>,
    pub cold_rx: tokio::sync::Mutex<mpsc::Receiver<Bytes>>,
    pub fast_rx: tokio::sync::Mutex<mpsc::Receiver<Bytes>>,
    pub video_src: Arc<VideoSource>,
    pub video_sink: Arc<VideoSink>,
    pub observer: Arc<dyn PeerObserver>,
    pub last_stats: Mutex<serde_json::Value>,
}

fn lane_body(peer: u8, payload: &[u8]) -> Bytes {
    let mut b = BytesMut::with_capacity(payload.len() + 1);
    b.put_u8(peer);
    b.put_slice(payload);
    b.freeze()
}

async fn ack_cold(link: &Link, n: u32) {
    link.frame(T_COLD_ACK, Bytes::copy_from_slice(&n.to_be_bytes())).await;
}

/// Cold-lane wire framing: u8 codec | u32 BE raw length | payload.
/// codec 0 = raw, 1 = zstd (level 3: ~5× on .blend partials, fast).
const CODEC_RAW: u8 = 0;
const CODEC_ZSTD: u8 = 1;
const ZSTD_LEVEL: i32 = 1; // level 3 single-threaded took 300 ms on a 61 MB partial (measured)
const CODEC_PIPELINE: usize = 4; // chunks compressing/decompressing in parallel, delivered in order
const ZSTD_MIN: usize = 4096; // below this, compressing costs more than it saves

fn cold_wire_encode(raw: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(raw.len() + 5);
    if raw.len() >= ZSTD_MIN {
        if let Ok(z) = zstd::bulk::compress(raw, ZSTD_LEVEL) {
            if z.len() < raw.len() {
                out.push(CODEC_ZSTD);
                out.extend_from_slice(&(raw.len() as u32).to_be_bytes());
                out.extend_from_slice(&z);
                return out;
            }
        }
    }
    out.push(CODEC_RAW);
    out.extend_from_slice(&(raw.len() as u32).to_be_bytes());
    out.extend_from_slice(raw);
    out
}

fn cold_wire_decode(wire: &[u8]) -> Result<Vec<u8>> {
    if wire.len() < 5 {
        bail!("cold frame too short");
    }
    let raw_len = u32::from_be_bytes([wire[1], wire[2], wire[3], wire[4]]) as usize;
    match wire[0] {
        CODEC_RAW => Ok(wire[5..].to_vec()),
        CODEC_ZSTD => zstd::bulk::decompress(&wire[5..], raw_len).context("zstd decompress"),
        c => bail!("unknown cold codec {c}"),
    }
}

async fn ack_fast(link: &Link, n: u32) {
    link.frame(T_FAST_ACK, Bytes::copy_from_slice(&n.to_be_bytes())).await;
}

/// Constant-time token comparison. Kyber compared tokens with `!=`; the check
/// gates the whole session, so it should not leak the answer through timing.
/// `ring`'s helper is deprecated as internal-only, so this is the usual
/// fold-the-differences idiom. Length is compared first and therefore leaks,
/// which is true of every such comparison.
fn tokens_match(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

// ---- lane framing ----------------------------------------------------------

async fn write_tag(s: &mut quinn::SendStream, tag: u8) -> Result<()> {
    // Quinn creates the stream on the wire when the first bytes are written,
    // so the tag doubles as "this lane exists". No flush: quinn sends as
    // congestion allows and keeps no user-space buffer to drain.
    s.write_all(&[tag]).await.context("write lane tag")?;
    Ok(())
}

async fn read_tag(r: &mut quinn::RecvStream) -> Result<u8> {
    let mut t = [0u8; 1];
    r.read_exact(&mut t).await.map_err(|e| anyhow!("read lane tag: {e}"))?;
    Ok(t[0])
}

async fn send_msg(s: &mut quinn::SendStream, payload: &[u8]) -> Result<()> {
    s.write_all(&(payload.len() as u32).to_be_bytes()).await?;
    s.write_all(payload).await?;
    Ok(())
}

/// `Ok(None)` is a clean close at a frame boundary; anything else is an error,
/// which preserves the old `DataProtocol::recv()` contract exactly.
async fn recv_msg(r: &mut quinn::RecvStream) -> Result<Option<Bytes>> {
    let mut hdr = [0u8; 4];
    match r.read_exact(&mut hdr).await {
        Ok(()) => {}
        Err(quinn::ReadExactError::FinishedEarly(0)) => return Ok(None),
        Err(e) => return Err(anyhow!("lane read: {e}")),
    }
    let n = u32::from_be_bytes(hdr) as usize;
    if n > MAX_LANE_FRAME {
        bail!("lane frame of {n} bytes exceeds {MAX_LANE_FRAME}");
    }
    let mut buf = vec![0u8; n];
    r.read_exact(&mut buf).await.map_err(|e| anyhow!("lane body: {e}"))?;
    Ok(Some(Bytes::from(buf)))
}

struct Lanes {
    control_send: quinn::SendStream,
    control_recv: quinn::RecvStream,
    hot_send: Option<quinn::SendStream>,
    hot_recv: Option<quinn::RecvStream>,
    cold_send: Option<quinn::SendStream>,
    cold_recv: Option<quinn::RecvStream>,
    fast_send: Option<quinn::SendStream>,
    fast_recv: Option<quinn::RecvStream>,
}

// ---- transport configuration ----------------------------------------------

/// Shared by both roles. Kyber took a single `max_udp_payload_size`; quinn
/// needs the floor, the starting point AND discovery disabled, or it probes
/// upward from its own 1200-byte default.
fn transport_config(mtu: u16) -> quinn::TransportConfig {
    let mut t = quinn::TransportConfig::default();
    t.max_idle_timeout(Some(
        Duration::from_secs(3).try_into().expect("3s is a valid idle timeout"),
    ));
    t.keep_alive_interval(Some(Duration::from_millis(500)));
    t.initial_mtu(mtu);
    t.min_mtu(mtu);
    t.mtu_discovery_config(None);
    // No congestion-controller override. The fixed-rate controller existed to
    // pace video behind Kyber's datagram pacer; with video off this
    // connection the only traffic is sync data, which wants ordinary
    // loss-responsive control. `FixedRateControllerFactory` stays in lib.rs
    // for whoever next needs a paced media lane.
    t
}

// ---- replica: listen -------------------------------------------------------

pub struct ReplicaListener {
    pub endpoint: quinn::Endpoint,
    pub fingerprint: String,
}

pub fn listen(addr: SocketAddr, cert_dir: &Path, mtu: u16) -> Result<ReplicaListener> {
    let (cert, key, fingerprint) = load_or_create_cert(cert_dir)?;
    let mut tls = rustls::ServerConfig::builder()
        .with_no_client_auth()
        .with_single_cert(vec![cert], key)
        .context("server TLS config")?;
    tls.alpn_protocols = vec![ALPN.to_vec()];
    let mut cfg = quinn::ServerConfig::with_crypto(Arc::new(
        QuicServerConfig::try_from(tls).context("QUIC server config")?,
    ));
    cfg.transport_config(Arc::new(transport_config(mtu)));
    // Binds a UDP socket, so this must run inside a Tokio context.
    let endpoint = quinn::Endpoint::server(cfg, addr).with_context(|| format!("listen on {addr}"))?;
    Ok(ReplicaListener { endpoint, fingerprint })
}

pub async fn serve_one(ctx: Arc<Ctx>, endpoint: &quinn::Endpoint) -> Result<()> {
    let incoming = endpoint.accept().await.ok_or_else(|| anyhow!("listener closed"))?;
    let conn = tokio::time::timeout(HANDSHAKE, incoming)
        .await
        .context("QUIC handshake timeout")?
        .context("QUIC handshake")?;

    // The host opens control first and its first frame is the token, so the
    // whole session is gated on auth before any other lane is accepted.
    let (control_send, mut control_recv) = tokio::time::timeout(HANDSHAKE, conn.accept_bi())
        .await
        .context("control lane timeout")?
        .context("control lane")?;
    let tag = read_tag(&mut control_recv).await?;
    if tag != LANE_CONTROL {
        bail!("first stream tagged {tag}, expected control");
    }
    let token = recv_msg(&mut control_recv)
        .await?
        .ok_or_else(|| anyhow!("peer closed before sending a token"))?;
    let ours = ctx.cfg.with(|c| c.token.clone());
    if !tokens_match(&token, ours.as_bytes()) {
        conn.close(CLOSE_TOKEN.into(), b"token rejected");
        bail!("peer token mismatch; rejected");
    }

    let mut control_send = control_send;
    send_msg(&mut control_send, b"ok").await.context("auth reply")?;

    let mut hot_recv = None;
    let mut cold_recv = None;
    let mut fast_recv = None;
    for _ in 0..3 {
        let mut r = tokio::time::timeout(HANDSHAKE, conn.accept_uni())
            .await
            .context("lane accept timeout")?
            .context("lane accept")?;
        match read_tag(&mut r).await? {
            LANE_HOT => hot_recv = Some(r),
            LANE_COLD => cold_recv = Some(r),
            LANE_FAST => fast_recv = Some(r),
            t => bail!("unknown lane tag {t}"),
        }
    }

    ctx.observer.peer_up(None);
    let lanes = Lanes {
        control_send,
        control_recv,
        hot_send: None,
        hot_recv,
        cold_send: None,
        cold_recv,
        fast_send: None,
        fast_recv,
    };
    let r = run_lanes(ctx.clone(), conn, lanes).await;
    ctx.observer.peer_down(r.as_ref().err().map(|e| format!("{e:#}")).unwrap_or_default());
    r
}

// ---- host: connect ---------------------------------------------------------

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

#[derive(Clone, Debug, PartialEq)]
pub struct HostTarget {
    pub addr: String,
    pub fingerprint: Option<Vec<u8>>,
    pub mtu: u16,
}

/// Where the host should connect: set by config or by the addon (CMD
/// connect). Changing it ends the current session.
pub struct HostControl {
    pub target: Mutex<Option<HostTarget>>,
    pub generation: std::sync::atomic::AtomicU64,
    pub notify: tokio::sync::Notify,
}

impl HostControl {
    pub fn new(target: Option<HostTarget>) -> Self {
        Self { target: Mutex::new(target), generation: std::sync::atomic::AtomicU64::new(0), notify: tokio::sync::Notify::new() }
    }

    pub fn set(&self, target: Option<HostTarget>) {
        *self.target.lock().unwrap() = target;
        self.generation.fetch_add(1, Relaxed);
        self.notify.notify_waiters();
    }
}

pub async fn connect_one(ctx: Arc<Ctx>, target: &HostTarget) -> Result<()> {
    let (conn, lanes, fingerprint) = match dial(&ctx, target).await {
        Ok(v) => v,
        Err(e) => {
            // Anything that fails before the lanes are up — a rejected token,
            // a changed certificate, no route — used to be swallowed whole:
            // host_loop discards the error and peer_down was only reached
            // after a session had already started. The addon got no reason.
            ctx.observer.peer_down(format!("{e:#}"));
            return Err(e);
        }
    };
    ctx.observer.peer_up(fingerprint);
    let r = run_lanes(ctx.clone(), conn, lanes).await;
    ctx.observer.peer_down(r.as_ref().err().map(|e| format!("{e:#}")).unwrap_or_default());
    r
}

/// Everything up to "all three lanes are open and the token was accepted".
async fn dial(
    ctx: &Arc<Ctx>,
    target: &HostTarget,
) -> Result<(quinn::Connection, Lanes, Option<String>)> {
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
    let mut tls = rustls::ClientConfig::builder()
        .dangerous()
        .with_custom_certificate_verifier(Arc::new(TofuVerifier {
            expected: target.fingerprint.clone(),
            seen: seen.clone(),
            provider,
        }))
        .with_no_client_auth();
    tls.alpn_protocols = vec![ALPN.to_vec()];
    let mut client_cfg =
        quinn::ClientConfig::new(Arc::new(QuicClientConfig::try_from(tls).context("QUIC client config")?));
    client_cfg.transport_config(Arc::new(transport_config(target.mtu)));

    let bind: SocketAddr = if addr.is_ipv6() { "[::]:0".parse()? } else { "0.0.0.0:0".parse()? };
    let mut endpoint = quinn::Endpoint::client(bind).context("bind client endpoint")?;
    endpoint.set_default_client_config(client_cfg);

    let conn = tokio::time::timeout(HANDSHAKE, endpoint.connect(addr, "localhost")?)
        .await
        .context("QUIC connect timeout")?
        .context("QUIC connect")?;

    // Control carries the token as its first frame; the replica answers "ok"
    // or closes the connection. Either way we know before peer_up, which is
    // what keeps a rejected token from ever looking like a live peer.
    let (mut control_send, mut control_recv) = conn.open_bi().await.context("open control lane")?;
    write_tag(&mut control_send, LANE_CONTROL).await?;
    let ours = ctx.cfg.with(|c| c.token.clone());
    send_msg(&mut control_send, ours.as_bytes()).await.context("send token")?;
    let reply = tokio::time::timeout(HANDSHAKE, recv_msg(&mut control_recv))
        .await
        .context("auth reply timeout")?
        // A rejected token reaches us as the connection dropping, so the
        // close reason the replica set is the message worth reporting —
        // "connection lost" on its own says nothing.
        .map_err(|e| match conn.close_reason() {
            Some(c) => anyhow!("rejected by replica: {c}"),
            None => e.context("auth reply"),
        })?
        .ok_or_else(|| anyhow!("replica closed during auth"))?;
    if reply.as_ref() != b"ok" {
        bail!("unexpected auth reply from replica");
    }

    let mut hot_send = conn.open_uni().await.context("open hot lane")?;
    write_tag(&mut hot_send, LANE_HOT).await?;
    let mut cold_send = conn.open_uni().await.context("open cold lane")?;
    write_tag(&mut cold_send, LANE_COLD).await?;
    let mut fast_send = conn.open_uni().await.context("open fast lane")?;
    write_tag(&mut fast_send, LANE_FAST).await?;

    let fingerprint = seen.lock().unwrap().as_deref().map(hex::encode);
    let lanes = Lanes {
        control_send,
        control_recv,
        hot_send: Some(hot_send),
        hot_recv: None,
        cold_send: Some(cold_send),
        cold_recv: None,
        fast_send: Some(fast_send),
        fast_recv: None,
    };
    Ok((conn, lanes, fingerprint))
}

/// Host: connect to whatever target the control holds, forever, with
/// backoff; a target change drops the session and reconnects.
pub async fn host_loop(ctx: Arc<Ctx>, control: Arc<HostControl>) {
    let mut backoff = Duration::from_millis(250);
    // Only logged when it changes: the loop retries every 250 ms to 2 s, and
    // a host that cannot reach its replica would otherwise fill the log with
    // one identical line per attempt.
    let mut last_reason = String::new();
    loop {
        let (target, generation) = {
            let t = control.target.lock().unwrap().clone();
            (t, control.generation.load(Relaxed))
        };
        let Some(target) = target else {
            control.notify.notified().await;
            continue;
        };
        let started = Instant::now();
        tokio::select! {
            r = connect_one(ctx.clone(), &target) => {
                let reason = r.as_ref().err().map(|e| format!("{e:#}")).unwrap_or_default();
                if reason != last_reason {
                    if !reason.is_empty() {
                        crate::log!("[agent] connect to {}: {reason}", target.addr);
                    }
                    last_reason = reason;
                }
            }
            _ = async {
                loop {
                    control.notify.notified().await;
                    if control.generation.load(Relaxed) != generation { break; }
                }
            } => { ctx.observer.peer_down("target changed".into()); backoff = Duration::from_millis(100); continue; }
        }
        if started.elapsed() > Duration::from_secs(5) {
            backoff = Duration::from_millis(250);
        }
        tokio::time::sleep(backoff).await;
        backoff = (backoff * 2).min(Duration::from_secs(2));
    }
}

// ---- the lanes -------------------------------------------------------------

async fn run_lanes(ctx: Arc<Ctx>, conn: quinn::Connection, lanes: Lanes) -> Result<()> {
    let mut tasks = tokio::task::JoinSet::<Result<()>>::new();
    let Lanes { mut control_send, mut control_recv, hot_send, hot_recv, cold_send, cold_recv, fast_send, fast_recv } = lanes;

    {
        let mut rx = ctx.control_rx.lock().await;
        while rx.try_recv().is_ok() {}
        let mut rx = ctx.cold_rx.lock().await;
        let mut stale = 0u32;
        while let Ok(b) = rx.try_recv() {
            stale += b.len() as u32;
        }
        if stale > 0 {
            ack_cold(&ctx.link, stale).await;
        }
        let mut rx = ctx.fast_rx.lock().await;
        let mut stale = 0u32;
        while let Ok(b) = rx.try_recv() {
            stale += b.len() as u32;
        }
        if stale > 0 {
            ack_fast(&ctx.link, stale).await;
        }
    }
    ctx.inb.connected.store(true, Relaxed);

    {
        let ctx = ctx.clone();
        tasks.spawn(async move {
            let mut rx = ctx.control_rx.lock().await;
            while let Some(body) = rx.recv().await {
                send_msg(&mut control_send, &body.slice(1..)).await?;
            }
            Ok(())
        });
    }
    {
        let ctx = ctx.clone();
        tasks.spawn(async move {
            while let Some(payload) = recv_msg(&mut control_recv).await? {
                if !ctx.role_host
                    && payload.len() < 512
                    && payload.windows(17).any(|w| w == b"\"kind\":\"goodbye\"")
                {
                    ctx.observer.goodbye();
                }
                ctx.link.frame(T_CONTROL, lane_body(0, &payload)).await;
            }
            Ok(())
        });
    }

    if ctx.role_host {
        let mut hot_send = hot_send.ok_or_else(|| anyhow!("host session without a hot lane"))?;
        let mut cold_send = cold_send.ok_or_else(|| anyhow!("host session without a cold lane"))?;
        let c = ctx.clone();
        tasks.spawn(async move {
            loop {
                let notified = c.inb.hot_notify.notified();
                let pending: Vec<Bytes> = c.inb.hot.lock().unwrap().drain().map(|(_, v)| v).collect();
                if pending.is_empty() {
                    notified.await;
                    continue;
                }
                for body in pending {
                    send_msg(&mut hot_send, &body.slice(1..)).await?;
                }
            }
        });
        // Cold: compress chunks in parallel on blocking threads, send in
        // order. Sequential level-3 zstd took 300 ms for a 61 MB partial and
        // was the whole cost of a big blob on loopback (measured 2026-09-23).
        // Credits are bytes and return when a chunk is accepted here: the
        // window bounds what sits between the addon and the wire.
        let (ptx, mut prx) = mpsc::channel::<tokio::task::JoinHandle<Vec<u8>>>(CODEC_PIPELINE);
        let c = ctx.clone();
        tasks.spawn(async move {
            let mut rx = c.cold_rx.lock().await;
            while let Some(body) = rx.recv().await {
                let len = body.len() as u32;
                let raw = body.slice(1..);
                let job = tokio::task::spawn_blocking(move || cold_wire_encode(&raw));
                ack_cold(&c.link, len).await;
                if ptx.send(job).await.is_err() {
                    break;
                }
            }
            Ok(())
        });
        tasks.spawn(async move {
            while let Some(job) = prx.recv().await {
                let wire = job.await.map_err(|e| anyhow!("compress task: {e}"))?;
                send_msg(&mut cold_send, &wire).await?;
            }
            Ok(())
        });
        let mut fast_send = fast_send.ok_or_else(|| anyhow!("host session without a fast lane"))?;
        let c = ctx.clone();
        tasks.spawn(async move {
            let mut rx = c.fast_rx.lock().await;
            while let Some(body) = rx.recv().await {
                let len = body.len() as u32;
                let sent = send_msg(&mut fast_send, &body.slice(1..)).await;
                ack_fast(&c.link, len).await;
                sent?;
            }
            Ok(())
        });
    } else {
        let mut hot_recv = hot_recv.ok_or_else(|| anyhow!("replica session without a hot lane"))?;
        let mut cold_recv = cold_recv.ok_or_else(|| anyhow!("replica session without a cold lane"))?;
        let c = ctx.clone();
        tasks.spawn(async move {
            while let Some(payload) = recv_msg(&mut hot_recv).await? {
                if payload.is_empty() {
                    continue;
                }
                let klen = payload[0] as usize;
                if payload.len() > klen {
                    c.link.hot(payload[1..1 + klen].to_vec(), lane_body(0, &payload));
                }
            }
            Ok(())
        });
        // Cold: decompress in parallel, deliver to the addon in order.
        let (dtx, mut drx) = mpsc::channel::<tokio::task::JoinHandle<Result<Vec<u8>>>>(CODEC_PIPELINE);
        tasks.spawn(async move {
            while let Some(payload) = recv_msg(&mut cold_recv).await? {
                let job = tokio::task::spawn_blocking(move || cold_wire_decode(&payload));
                if dtx.send(job).await.is_err() {
                    break;
                }
            }
            Ok(())
        });
        let c = ctx.clone();
        tasks.spawn(async move {
            while let Some(job) = drx.recv().await {
                let raw = job.await.map_err(|e| anyhow!("decompress task: {e}"))??;
                c.link.frame(T_COLD, lane_body(0, &raw)).await;
            }
            Ok(())
        });
        let mut fast_recv = fast_recv.ok_or_else(|| anyhow!("replica session without a fast lane"))?;
        let c = ctx.clone();
        tasks.spawn(async move {
            while let Some(payload) = recv_msg(&mut fast_recv).await? {
                c.link.frame(T_FAST, lane_body(0, &payload)).await;
            }
            Ok(())
        });
    }

    {
        let c = ctx.clone();
        let stats_conn = conn.clone();
        tasks.spawn(async move {
            let mut tick = tokio::time::interval(Duration::from_secs(1));
            let mut prev = (0u64, 0u64);
            loop {
                tick.tick().await;
                // Quinn's stats are sync and always present. The Kyber RTT
                // this replaces was known-broken on the receiver (it read a
                // constant), and Kynet exposed no byte counters at all.
                let s = stats_conn.stats();
                let (tx, rx) = (s.udp_tx.bytes, s.udp_rx.bytes);
                let v = json!({
                    "event": "stats",
                    "rtt_ms": s.path.rtt.as_secs_f64() * 1000.0,
                    "quic_lost": s.path.lost_packets,
                    "quic_cwnd": s.path.cwnd,
                    "quic_mtu": s.path.current_mtu,
                    "tx_mbps": (tx - prev.0) as f64 * 8.0 / 1e6,
                    "rx_mbps": (rx - prev.1) as f64 * 8.0 / 1e6,
                });
                *c.last_stats.lock().unwrap() = v.clone();
                if c.link.attached.load(Relaxed) {
                    c.link.event(v).await;
                }
                prev = (tx, rx);
            }
        });
    }

    let result = tokio::select! {
        e = conn.closed() => Err(anyhow!("connection closed: {e}")),
        Some(joined) = tasks.join_next() => match joined {
            Ok(r) => r.and(Err(anyhow!("lane ended"))),
            Err(e) => Err(anyhow!("lane panicked: {e}")),
        },
    };
    ctx.inb.connected.store(false, Relaxed);
    tasks.shutdown().await;
    conn.close(0u32.into(), b"session over");
    result
}
