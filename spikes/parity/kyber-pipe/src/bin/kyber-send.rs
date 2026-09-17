//! kyber-send: Annex-B HEVC on stdin -> KyProto UnreliableFec video lane.
//!
//! Listens for one client at a time (token-checked); when it goes away, waits
//! for the next one. Frames arriving while no client is connected are
//! discarded; a new client starts at the next keyframe.

use anyhow::{Context, Result, anyhow, bail};
use bytes::Bytes;
use kymux_types::{AVPacket, CodecPacket, CodecPacketHeader, MediaPacket, MediaPacketHeader};
use kynet::Server;
use kyber_pipe::{
    AccessUnit, Args, AuSplitter, DEFAULT_MAX_UDP_PAYLOAD, FixedRateControllerFactory, HEVC_FOURCC,
    VIDEO_ENDPOINT_ID, default_cert_dir, load_or_create_cert, log,
};
use kyproto::{Connection, KyProtoStatsProvider, VideoProtocol};
use std::collections::VecDeque;
use std::io::Read;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering::Relaxed};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::sync::Notify;

const USAGE: &str = "\
kyber-send --listen HOST:PORT --token T --bitrate-cap-mbps N [options] < annexb.hevc

  --bitrate-cap-mbps N   wire pacing cap (Mbps) for datagrams incl. FEC; set it
                         >= video bitrate x 1.35 + 1 or frames queue up
  --cert-dir DIR         cert.der/key.der location (default: <tmp>/qcbridge-kyber-pipe)
  --fps F                pts in 90 kHz units at F fps (default: pts = frame index)
  --queue N              max AUs waiting to be sent before flushing to the next
                         keyframe (default 6)
  --mtu BYTES            fixed QUIC UDP payload size (default 1344)
  --cc fixed|quinn       fixed-rate window (default) or Quinn's default CUBIC
  --no-pacer             don't pace datagrams before Quinn (for comparison)";

#[derive(Default)]
struct Counters {
    aus_in: AtomicU64,
    aus_sent: AtomicU64,
    keys_sent: AtomicU64,
    bytes_sent: AtomicU64,
    queue_drops: AtomicU64,
    gop_skips: AtomicU64,
    idle_discards: AtomicU64,
    // Stdin arrival -> send() returned, per interval.
    dwell_us_sum: AtomicU64,
    dwell_us_max: AtomicU64,
}

struct QueueState {
    queue: VecDeque<(AccessUnit, u64, Instant)>,
    active: bool,
    need_key: bool,
    eof: bool,
}

struct Shared {
    state: Mutex<QueueState>,
    notify: Notify,
    eof_notify: Notify,
    counters: Counters,
    queue_cap: usize,
    stats: Mutex<Option<KyProtoStatsProvider>>,
    client_connected: AtomicBool,
}

fn reader_thread(shared: Arc<Shared>) {
    let mut stdin = std::io::stdin().lock();
    let mut splitter = AuSplitter::new();
    let mut buf = vec![0u8; 256 * 1024];
    let mut aus = Vec::new();
    let mut index = 0u64;
    loop {
        let n = match stdin.read(&mut buf) {
            Ok(0) => {
                splitter.flush(&mut aus);
                0
            }
            Ok(n) => {
                splitter.push(&buf[..n], &mut aus);
                n
            }
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) => {
                log("send", format!("stdin error: {e}"));
                0
            }
        };
        let now = Instant::now();
        for au in aus.drain(..) {
            let c = &shared.counters;
            c.aus_in.fetch_add(1, Relaxed);
            let idx = index;
            index += 1;
            let mut st = shared.state.lock().unwrap();
            if !st.active {
                c.idle_discards.fetch_add(1, Relaxed);
                continue;
            }
            if st.need_key && !au.key {
                c.gop_skips.fetch_add(1, Relaxed);
                continue;
            }
            st.need_key = false;
            if st.queue.len() >= shared.queue_cap {
                // Backlog: dropping any single P-frame breaks the reference
                // chain, so flush everything and resume at the next keyframe.
                let dropped = st.queue.len() as u64;
                st.queue.clear();
                c.queue_drops.fetch_add(dropped, Relaxed);
                if !au.key {
                    st.need_key = true;
                    c.queue_drops.fetch_add(1, Relaxed);
                    continue;
                }
            }
            st.queue.push_back((au, idx, now));
            drop(st);
            shared.notify.notify_one();
        }
        if n == 0 {
            shared.state.lock().unwrap().eof = true;
            shared.notify.notify_one();
            shared.eof_notify.notify_waiters();
            return;
        }
    }
}

async fn next_au(shared: &Shared) -> Option<(AccessUnit, u64, Instant)> {
    loop {
        let notified = shared.notify.notified();
        {
            let mut st = shared.state.lock().unwrap();
            if let Some(item) = st.queue.pop_front() {
                return Some(item);
            }
            if st.eof {
                return None;
            }
        }
        notified.await;
    }
}

fn set_active(shared: &Shared, active: bool) {
    let mut st = shared.state.lock().unwrap();
    st.active = active;
    st.need_key = true;
    st.queue.clear();
}

async fn stats_task(shared: Arc<Shared>, wire_cap_bps: u64) {
    let mut tick = tokio::time::interval(Duration::from_secs(1));
    tick.tick().await;
    let c = &shared.counters;
    let mut last = Instant::now();
    let mut prev = (0u64, 0u64, 0u64, 0u64, 0u64, 0u64);
    loop {
        tick.tick().await;
        let dt = last.elapsed().as_secs_f64();
        last = Instant::now();
        let aus_sent = c.aus_sent.load(Relaxed);
        let bytes = c.bytes_sent.load(Relaxed);
        let cur = (
            c.aus_in.load(Relaxed),
            aus_sent,
            bytes,
            c.queue_drops.load(Relaxed),
            c.gop_skips.load(Relaxed),
            c.keys_sent.load(Relaxed),
        );
        let sent_d = cur.1 - prev.1;
        let mbps = (cur.2 - prev.2) as f64 * 8.0 / dt / 1e6;
        let dwell_sum = c.dwell_us_sum.swap(0, Relaxed);
        let dwell_max = c.dwell_us_max.swap(0, Relaxed);
        let dwell_avg = if sent_d > 0 { dwell_sum as f64 / sent_d as f64 / 1000.0 } else { 0.0 };
        let provider = shared.stats.lock().unwrap().clone();
        let net = match provider {
            Some(p) => {
                let cs = p.connection_stats().await;
                format!(
                    " rtt_ms={:.2} quic_lost={}",
                    cs.rtt.map(|r| r.as_secs_f64() * 1000.0).unwrap_or(f64::NAN),
                    cs.packets_lost.unwrap_or_default()
                )
            }
            None => String::new(),
        };
        let queued = shared.state.lock().unwrap().queue.len();
        log(
            "send",
            format!(
                "client={} aus_in={} sent={} (+{}) keys={} video_mbps={:.1} est_wire_mbps={:.1} \
                 cap_mbps={:.1} queue={} queue_drops={} (+{}) gop_skips={} (+{}) idle_discards={} \
                 dwell_ms avg={:.1} max={:.1}{}",
                shared.client_connected.load(Relaxed),
                cur.0,
                cur.1,
                sent_d,
                cur.5,
                mbps,
                kyber_pipe::video_to_wire_bps((mbps * 1e6) as u64) as f64 / 1e6 - 1.0,
                wire_cap_bps as f64 / 1e6,
                queued,
                cur.3,
                cur.3 - prev.3,
                cur.4,
                cur.4 - prev.4,
                c.idle_discards.load(Relaxed),
                dwell_avg,
                dwell_max as f64 / 1000.0,
                net
            ),
        );
        prev = cur;
    }
}

async fn serve_client(
    server: &kynet::common::CommonServer,
    token: &str,
    shared: &Arc<Shared>,
    fps: Option<f64>,
) -> Result<bool> {
    let raw = server
        .accept()
        .await?
        .ok_or_else(|| anyhow!("listener closed"))?;
    let unauth = tokio::time::timeout(Duration::from_secs(5), Connection::accept_with_auth(raw))
        .await
        .context("auth timeout")??;
    if unauth.get_auth().token() != token {
        unauth.reject_authentication();
        bail!("client token mismatch; rejected");
    }
    let conn = unauth.accept_authentication().await?;
    let (id, endpoint) = conn
        .register_video_endpoint(VideoProtocol::UnreliableFec)
        .await?;
    if id != VIDEO_ENDPOINT_ID {
        bail!("video endpoint id {id}, expected {VIDEO_ENDPOINT_ID}");
    }
    let mut video = tokio::time::timeout(Duration::from_secs(5), endpoint.ready())
        .await
        .context("video endpoint ready timeout")??;
    log("send", "client authenticated; video lane ready, waiting for keyframe");

    *shared.stats.lock().unwrap() = Some(conn.stats_provider());
    shared.client_connected.store(true, Relaxed);
    set_active(shared, true);

    let result = async {
        video
            .send
            .send(AVPacket::Codec(CodecPacket {
                header: CodecPacketHeader {
                    codec: HEVC_FOURCC,
                    rotation: 0,
                    frame_size: 0,
                },
            }))
            .await?;
        let c = &shared.counters;
        loop {
            let item = tokio::select! {
                r = conn.closed() => {
                    log("send", format!("client connection closed: {r:?}"));
                    return Ok::<bool, anyhow::Error>(false);
                }
                item = next_au(shared) => item,
            };
            let Some((au, idx, arrived)) = item else {
                return Ok(true); // stdin EOF
            };
            let pts = match fps {
                Some(f) => (idx as f64 * 90_000.0 / f).round() as u64,
                None => idx,
            };
            if au.key {
                // Plank pattern: empty config marker, then the full AU with
                // VPS/SPS/PPS in band.
                video
                    .send
                    .send(AVPacket::Media(MediaPacket {
                        header: MediaPacketHeader { is_config: true, is_key: true, pts, size: 0 },
                        payload: Bytes::new(),
                    }))
                    .await?;
            }
            let size = au.data.len();
            video
                .send
                .send(AVPacket::Media(MediaPacket {
                    header: MediaPacketHeader {
                        is_config: false,
                        is_key: au.key,
                        pts,
                        size: size as u32,
                    },
                    payload: au.data,
                }))
                .await?;
            let dwell = arrived.elapsed().as_micros() as u64;
            c.dwell_us_sum.fetch_add(dwell, Relaxed);
            c.dwell_us_max.fetch_max(dwell, Relaxed);
            c.aus_sent.fetch_add(1, Relaxed);
            c.bytes_sent.fetch_add(size as u64, Relaxed);
            if au.key {
                c.keys_sent.fetch_add(1, Relaxed);
            }
        }
    }
    .await;

    set_active(shared, false);
    shared.client_connected.store(false, Relaxed);
    *shared.stats.lock().unwrap() = None;
    drop(video);
    conn.close();
    result
}

#[tokio::main(flavor = "multi_thread", worker_threads = 4)]
async fn main() -> Result<()> {
    let args = Args::parse(USAGE);
    let listen: SocketAddr = args
        .require("listen", USAGE)
        .parse()
        .context("--listen must be HOST:PORT with a numeric host")?;
    let token = args.require("token", USAGE);
    let cap_mbps: f64 = args.require("bitrate-cap-mbps", USAGE).parse()?;
    let cert_dir = args.get("cert-dir").map(Into::into).unwrap_or_else(default_cert_dir);
    let fps: Option<f64> = args.get("fps").map(str::parse).transpose()?;
    let queue_cap: usize = args.get("queue").map(str::parse).transpose()?.unwrap_or(6);
    let mtu: u16 = args.get("mtu").map(str::parse).transpose()?.unwrap_or(DEFAULT_MAX_UDP_PAYLOAD);
    let cc = args.get("cc").unwrap_or("fixed").to_string();
    let pace = !args.flag("no-pacer");

    kynet::init_crypto();
    let (cert, key, fingerprint) = load_or_create_cert(&cert_dir)?;
    log("send", format!("cert dir {}", cert_dir.display()));
    log("send", format!("fingerprint {fingerprint}"));

    let wire_cap_bps = (cap_mbps * 1e6) as u64;
    let pacer = Arc::new(kynet::quinn::DatagramPacer::new(wire_cap_bps, Duration::from_millis(2), 0));
    let options = kynet::common::CommonServerOptions {
        max_idle_timeout: Some(Duration::from_secs(3)),
        keep_alive_interval: Some(Duration::from_millis(500)),
        max_udp_payload_size: Some(mtu),
        congestion_controller_factory: if cc == "quinn" {
            None
        } else {
            Some(Arc::new(FixedRateControllerFactory { wire_bps: wire_cap_bps }))
        },
        datagram_pacer: pace.then(|| pacer.clone()),
    };
    let server = kynet::Connection::start_server_on_addr(listen, vec![cert], key, &options)
        .map_err(|e| anyhow!("start server on {listen}: {e:?}"))?;
    log(
        "send",
        format!("listening on {listen} cap={cap_mbps} Mbps pacer={pace} cc={cc} mtu={mtu} queue={queue_cap}"),
    );

    let shared = Arc::new(Shared {
        state: Mutex::new(QueueState {
            queue: VecDeque::new(),
            active: false,
            need_key: true,
            eof: false,
        }),
        notify: Notify::new(),
        eof_notify: Notify::new(),
        counters: Counters::default(),
        queue_cap: queue_cap.max(1),
        stats: Mutex::new(None),
        client_connected: AtomicBool::new(false),
    });
    {
        let shared = shared.clone();
        std::thread::Builder::new()
            .name("stdin".into())
            .spawn(move || reader_thread(shared))?;
    }
    tokio::spawn(stats_task(shared.clone(), wire_cap_bps));

    loop {
        tokio::select! {
            r = serve_client(&server, &token, &shared, fps) => match r {
                Ok(true) => {
                    log("send", "stdin EOF; exiting");
                    break;
                }
                Ok(false) => log("send", "waiting for next client"),
                Err(e) => log("send", format!("session ended: {e:#}; waiting for next client")),
            },
            _ = async {
                loop {
                    let notified = shared.eof_notify.notified();
                    if shared.state.lock().unwrap().eof { break; }
                    notified.await;
                }
            }, if !shared.client_connected.load(Relaxed) => {
                log("send", "stdin EOF; exiting");
                break;
            }
        }
    }
    server.close(0, "kyber-send exiting");
    Ok(())
}
