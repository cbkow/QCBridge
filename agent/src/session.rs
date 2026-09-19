//! One Kyber connection = one session. Lanes (kyproto endpoint ids):
//!   0 video (UnreliableFec) replica→host · 2 control · 4 hot · 6 cold
//! Hot rides a reliable stream with sender-side per-key conflation (Kyber
//! has no client→server datagram lane; plan: Kyber patch 1).

use crate::link::{Inbound, Link, T_COLD, T_COLD_ACK, T_CONTROL};
use crate::video::{VideoSink, VideoSource};
use crate::{FixedRateControllerFactory, HEVC_FOURCC, load_or_create_cert};
use anyhow::{Context, Result, anyhow, bail};
use bytes::{BufMut, Bytes, BytesMut};
use kymux_types::{
    AVPacket, CodecPacket, CodecPacketHeader, DataPacket, DataProtocol, MediaPacket, MediaPacketHeader,
};
use kynet::Server;
use kyproto::{ClientAuth, Connection, VideoProtocol};
use serde_json::json;
use std::net::{SocketAddr, ToSocketAddrs};
use std::path::Path;
use std::sync::atomic::Ordering::Relaxed;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::sync::mpsc;

const EP_VIDEO: u16 = 0;
const EP_CONTROL: u16 = 2;
const EP_HOT: u16 = 4;
const EP_COLD: u16 = 6;
const HANDSHAKE: Duration = Duration::from_secs(5);
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
    pub token: String,
    pub link: Arc<Link>,
    pub inb: Arc<Inbound>,
    pub control_rx: tokio::sync::Mutex<mpsc::Receiver<Bytes>>,
    pub cold_rx: tokio::sync::Mutex<mpsc::Receiver<Bytes>>,
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

    {
        let mut rx = ctx.control_rx.lock().await;
        while rx.try_recv().is_ok() {}
        let mut rx = ctx.cold_rx.lock().await;
        let mut stale = 0u32;
        while rx.try_recv().is_ok() {
            stale += 1;
        }
        if stale > 0 {
            ack_cold(&ctx.link, stale).await;
        }
    }
    ctx.inb.connected.store(true, Relaxed);

    {
        let ctx = ctx.clone();
        tasks.spawn(async move {
            let mut rx = ctx.control_rx.lock().await;
            while let Some(body) = rx.recv().await {
                control_send.send(DataPacket { payload: body.slice(1..) }).await?;
            }
            Ok(())
        });
    }
    {
        let ctx = ctx.clone();
        tasks.spawn(async move {
            while let Some(p) = control_recv.recv().await? {
                if !ctx.role_host && p.payload.len() < 512
                    && p.payload.windows(17).any(|w| w == b"\"kind\":\"goodbye\"")
                {
                    ctx.observer.goodbye();
                }
                ctx.link.frame(T_CONTROL, lane_body(0, &p.payload)).await;
            }
            Ok(())
        });
    }

    if ctx.role_host {
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
                    hot_send.send(DataPacket { payload: body.slice(1..) }).await?;
                }
            }
        });
        let c = ctx.clone();
        tasks.spawn(async move {
            let mut rx = c.cold_rx.lock().await;
            while let Some(body) = rx.recv().await {
                let sent = cold_send.send(DataPacket { payload: body.slice(1..) }).await;
                ack_cold(&c.link, 1).await;
                sent?;
            }
            Ok(())
        });
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
                    c.link.hot(p.payload[1..1 + klen].to_vec(), lane_body(0, &p.payload));
                }
            }
            Ok(())
        });
        let c = ctx.clone();
        tasks.spawn(async move {
            while let Some(p) = cold_recv.recv().await? {
                c.link.frame(T_COLD, lane_body(0, &p.payload)).await;
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
                let pts = idx * 1500;
                if au.key {
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
                let v = json!({
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
                });
                *c.last_stats.lock().unwrap() = v.clone();
                if c.link.attached.load(Relaxed) {
                    c.link.event(v).await;
                }
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

pub struct ReplicaListener {
    pub server: kynet::common::CommonServer,
    pub fingerprint: String,
}

pub fn listen(addr: SocketAddr, cert_dir: &Path, cap_mbps: f64, mtu: u16) -> Result<ReplicaListener> {
    let (cert, key, fingerprint) = load_or_create_cert(cert_dir)?;
    let wire_cap_bps = (cap_mbps * 1e6) as u64;
    let options = kynet::common::CommonServerOptions {
        max_idle_timeout: Some(Duration::from_secs(3)),
        keep_alive_interval: Some(Duration::from_millis(500)),
        max_udp_payload_size: Some(mtu),
        congestion_controller_factory: Some(Arc::new(FixedRateControllerFactory { wire_bps: wire_cap_bps })),
        datagram_pacer: Some(Arc::new(kynet::quinn::DatagramPacer::new(wire_cap_bps, Duration::from_millis(2), 0))),
    };
    let server = kynet::Connection::start_server_on_addr(addr, vec![cert], key, &options)
        .map_err(|e| anyhow!("listen on {addr}: {e:?}"))?;
    Ok(ReplicaListener { server, fingerprint })
}

pub async fn serve_one(ctx: Arc<Ctx>, server: &kynet::common::CommonServer) -> Result<()> {
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
    ctx.observer.peer_up(None);
    let r = run_lanes(ctx.clone(), conn, control, hot, cold, Some(video), None).await;
    ctx.observer.peer_down(r.as_ref().err().map(|e| format!("{e:#}")).unwrap_or_default());
    r
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

pub struct HostTarget {
    pub addr: String,
    pub fingerprint: Option<Vec<u8>>,
    pub mtu: u16,
}

pub async fn connect_one(ctx: Arc<Ctx>, target: &HostTarget) -> Result<()> {
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
    ctx.observer.peer_up(fingerprint);
    let r = run_lanes(ctx.clone(), conn, control, hot, cold, None, Some(video)).await;
    ctx.observer.peer_down(r.as_ref().err().map(|e| format!("{e:#}")).unwrap_or_default());
    r
}

/// Host: connect forever with backoff.
pub async fn host_loop(ctx: Arc<Ctx>, target: HostTarget) {
    let mut backoff = Duration::from_millis(250);
    loop {
        let started = Instant::now();
        let _ = connect_one(ctx.clone(), &target).await;
        if started.elapsed() > Duration::from_secs(5) {
            backoff = Duration::from_millis(250);
        }
        tokio::time::sleep(backoff).await;
        backoff = (backoff * 2).min(Duration::from_secs(2));
    }
}
