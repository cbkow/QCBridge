//! kyber-recv: KyProto UnreliableFec video lane -> Annex-B HEVC on stdout.
//!
//! Connects to kyber-send (certificate pinned by SHA-256), writes every
//! received access unit to stdout and flushes per AU. Reconnects with backoff
//! when the sender goes away.

use anyhow::{Context, Result, anyhow};
use bytes::Bytes;
use kymux_types::AVPacket;
use kyber_pipe::{Args, DEFAULT_MAX_UDP_PAYLOAD, VIDEO_ENDPOINT_ID, log};
use kyproto::{ClientAuth, Connection, KyProtoStatsProvider, VideoProtocol};
use std::io::Write;
use std::net::{SocketAddr, ToSocketAddrs};
use std::sync::atomic::{AtomicU64, Ordering::Relaxed};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const USAGE: &str = "\
kyber-recv --connect HOST:PORT --token T --fingerprint SHA256HEX [options] > annexb.hevc

  --server-name NAME   TLS server name (default localhost; not verified, the
                       fingerprint pins the certificate)
  --mtu BYTES          fixed QUIC UDP payload size (default 1344)";

#[derive(Default)]
struct Counters {
    frames: AtomicU64,
    keys: AtomicU64,
    bytes: AtomicU64,
    holes: AtomicU64,
    connects: AtomicU64,
    // Largest gap between consecutive AUs handed to stdout, per interval.
    gap_us_max: AtomicU64,
}

struct Shared {
    counters: Counters,
    stats: Mutex<Option<KyProtoStatsProvider>>,
}

fn writer_thread(rx: std::sync::mpsc::Receiver<Bytes>) {
    let mut out = std::io::stdout().lock();
    while let Ok(au) = rx.recv() {
        if out.write_all(&au).and_then(|_| out.flush()).is_err() {
            log("recv", "stdout closed; exiting");
            std::process::exit(0);
        }
    }
}

async fn stats_task(shared: Arc<Shared>) {
    let mut tick = tokio::time::interval(Duration::from_secs(1));
    tick.tick().await;
    let c = &shared.counters;
    let mut last = Instant::now();
    let (mut pf, mut pb, mut pdrop, mut psrc, mut pmiss, mut punrec) = (0, 0, 0, 0, 0, 0);
    loop {
        tick.tick().await;
        let dt = last.elapsed().as_secs_f64();
        last = Instant::now();
        let frames = c.frames.load(Relaxed);
        let bytes = c.bytes.load(Relaxed);
        let provider = shared.stats.lock().unwrap().clone();
        let mut line = format!(
            "connects={} frames={} (+{}) keys={} video_mbps={:.1} holes={} max_gap_ms={:.1}",
            c.connects.load(Relaxed),
            frames,
            frames - pf,
            c.keys.load(Relaxed),
            (bytes - pb) as f64 * 8.0 / dt / 1e6,
            c.holes.load(Relaxed),
            c.gap_us_max.swap(0, Relaxed) as f64 / 1000.0,
        );
        pf = frames;
        pb = bytes;
        if let Some(p) = provider {
            let cs = p.connection_stats().await;
            let ps = p.protocol_stats();
            let drop = ps.dropped_packets.unwrap_or_default();
            let src = ps.video_fec_source_symbols.unwrap_or_default();
            let miss = ps.video_fec_source_symbols_missing.unwrap_or_default();
            let unrec = ps.video_fec_source_symbols_unrecovered.unwrap_or_default();
            line += &format!(
                " dropped_packets={} (+{}) fec_src_symbols={} (+{}) fec_missing={} (+{}) \
                 fec_unrecovered={} (+{}) rtt_ms={:.2} quic_lost={}",
                drop,
                drop.saturating_sub(pdrop),
                src,
                src.saturating_sub(psrc),
                miss,
                miss.saturating_sub(pmiss),
                unrec,
                unrec.saturating_sub(punrec),
                cs.rtt.map(|r| r.as_secs_f64() * 1000.0).unwrap_or(f64::NAN),
                cs.packets_lost.unwrap_or_default(),
            );
            (pdrop, psrc, pmiss, punrec) = (drop, src, miss, unrec);
        } else {
            (pdrop, psrc, pmiss, punrec) = (0, 0, 0, 0);
            line += " (not connected)";
        }
        log("recv", line);
    }
}

struct Target {
    addr: SocketAddr,
    server_name: String,
    token: String,
    fingerprint: String,
    mtu: u16,
}

/// One connection's lifetime. Returns the number of frames received.
async fn session(t: &Target, shared: &Arc<Shared>, out: &std::sync::mpsc::Sender<Bytes>) -> Result<u64> {
    let options = kynet::quinn::QuinnClientOptions {
        max_idle_timeout: Some(Duration::from_secs(3)),
        keep_alive_interval: Some(Duration::from_millis(500)),
        max_udp_payload_size: Some(t.mtu),
        certificate_hash: Some(t.fingerprint.clone()),
        ..Default::default()
    };
    let raw = tokio::time::timeout(
        Duration::from_secs(5),
        kynet::Connection::quinn_connect(t.addr, &t.server_name, None, &options),
    )
    .await
    .context("QUIC connect timeout")?
    .map_err(|e| anyhow!("QUIC connect: {e:?}"))?;
    let auth = ClientAuth::new(&t.token).map_err(|e| anyhow!("token: {e:?}"))?;
    let conn = tokio::time::timeout(Duration::from_secs(5), Connection::connect_with_auth(raw, &auth))
        .await
        .context("auth timeout")??;
    let endpoint = conn.connect_video_endpoint(VIDEO_ENDPOINT_ID, VideoProtocol::UnreliableFec)?;
    let mut video = tokio::time::timeout(Duration::from_secs(5), endpoint.ready())
        .await
        .context("video endpoint ready timeout (token rejected?)")??;
    log("recv", format!("connected to {}", t.addr));
    shared.counters.connects.fetch_add(1, Relaxed);
    *shared.stats.lock().unwrap() = Some(conn.stats_provider());

    let c = &shared.counters;
    let mut received = 0u64;
    let mut last_au: Option<Instant> = None;
    let result = async {
        loop {
            let packet = tokio::select! {
                r = conn.closed() => {
                    log("recv", format!("connection closed: {r:?}"));
                    return Ok::<(), anyhow::Error>(());
                }
                p = video.recv.recv() => p?,
            };
            match packet {
                None => return Ok(()),
                Some(AVPacket::Codec(p)) => {
                    let fourcc = p.header.codec.to_be_bytes();
                    log("recv", format!("codec {}", String::from_utf8_lossy(&fourcc)));
                }
                Some(AVPacket::Media(p)) => {
                    if p.payload.is_empty() {
                        continue; // Plank-style empty config marker
                    }
                    let now = Instant::now();
                    if let Some(prev) = last_au {
                        c.gap_us_max.fetch_max((now - prev).as_micros() as u64, Relaxed);
                    }
                    last_au = Some(now);
                    if !p.header.is_config {
                        received += 1;
                        c.frames.fetch_add(1, Relaxed);
                        if p.header.is_key {
                            c.keys.fetch_add(1, Relaxed);
                        }
                    }
                    c.bytes.fetch_add(p.payload.len() as u64, Relaxed);
                    if out.send(p.payload).is_err() {
                        return Err(anyhow!("writer gone"));
                    }
                }
                Some(AVPacket::Hole(_)) => {
                    c.holes.fetch_add(1, Relaxed);
                }
            }
        }
    }
    .await;
    *shared.stats.lock().unwrap() = None;
    conn.close();
    result.map(|_| received)
}

#[tokio::main(flavor = "multi_thread", worker_threads = 4)]
async fn main() -> Result<()> {
    let args = Args::parse(USAGE);
    let connect = args.require("connect", USAGE);
    let addr = connect
        .to_socket_addrs()
        .with_context(|| format!("resolve {connect}"))?
        .next()
        .ok_or_else(|| anyhow!("{connect} resolved to nothing"))?;
    let target = Target {
        addr,
        server_name: args.get("server-name").unwrap_or("localhost").to_string(),
        token: args.require("token", USAGE),
        fingerprint: args.require("fingerprint", USAGE).to_lowercase().replace(':', ""),
        mtu: args.get("mtu").map(str::parse).transpose()?.unwrap_or(DEFAULT_MAX_UDP_PAYLOAD),
    };

    kynet::init_crypto();
    let shared = Arc::new(Shared {
        counters: Counters::default(),
        stats: Mutex::new(None),
    });
    let (tx, rx) = std::sync::mpsc::channel::<Bytes>();
    std::thread::Builder::new()
        .name("stdout".into())
        .spawn(move || writer_thread(rx))?;
    tokio::spawn(stats_task(shared.clone()));

    let min_backoff = Duration::from_millis(250);
    let mut backoff = min_backoff;
    loop {
        match session(&target, &shared, &tx).await {
            Ok(n) => {
                log("recv", format!("session ended after {n} frames; reconnecting"));
                if n > 0 {
                    backoff = min_backoff;
                }
            }
            Err(e) => log("recv", format!("session failed: {e:#}; retry in {backoff:?}")),
        }
        tokio::time::sleep(backoff).await;
        backoff = (backoff * 2).min(Duration::from_secs(4));
    }
}
