//! Shared pieces for the QCBridge Agent: Annex-B access-unit splitting, the
//! self-signed certificate, a fixed-rate Quinn congestion controller and tiny
//! helpers.
//!
//! `FixedRateController` is currently unused by the transport: it existed to
//! hold a window open behind a media pacer, and video no longer rides the QUIC
//! connection (the replica sends SRT). It is kept because it is the piece a
//! paced media lane would need again.

use anyhow::{Context, Result, bail};
use bytes::{Bytes, BytesMut};
use quinn_proto::RttEstimator;
use quinn_proto::congestion::{Controller, ControllerFactory, ControllerMetrics};
use std::any::Any;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

pub const HEVC_FOURCC: u32 = u32::from_be_bytes(*b"HEVC");
pub const VIDEO_ENDPOINT_ID: u16 = 0;
pub const DEFAULT_MAX_UDP_PAYLOAD: u16 = 1344;

// ---------------------------------------------------------------- args ----

/// Minimal `--flag value` / `--switch` parser (keeps the dependency list short).
pub struct Args {
    items: Vec<(String, Option<String>)>,
}

impl Args {
    pub fn parse(usage: &str) -> Self {
        let raw: Vec<String> = std::env::args().skip(1).collect();
        if raw.iter().any(|a| a == "-h" || a == "--help") {
            eprintln!("{usage}");
            std::process::exit(0);
        }
        let mut items = Vec::new();
        let mut i = 0;
        while i < raw.len() {
            let key = raw[i].clone();
            if !key.starts_with("--") {
                eprintln!("unexpected argument {key}\n{usage}");
                std::process::exit(2);
            }
            let value = raw.get(i + 1).filter(|v| !v.starts_with("--")).cloned();
            i += if value.is_some() { 2 } else { 1 };
            items.push((key[2..].to_string(), value));
        }
        Self { items }
    }

    pub fn get(&self, key: &str) -> Option<&str> {
        self.items
            .iter()
            .rev()
            .find(|(k, _)| k == key)
            .and_then(|(_, v)| v.as_deref())
    }

    pub fn flag(&self, key: &str) -> bool {
        self.items.iter().any(|(k, _)| k == key)
    }

    pub fn require(&self, key: &str, usage: &str) -> String {
        match self.get(key) {
            Some(v) => v.to_string(),
            None => {
                eprintln!("missing --{key}\n{usage}");
                std::process::exit(2);
            }
        }
    }
}

pub fn log(tag: &str, msg: impl AsRef<str>) {
    eprintln!("[{tag}] {}", msg.as_ref());
}

// ------------------------------------------------------------- Annex-B ----

pub mod nal {
    pub const VPS: u8 = 32;
    pub const SPS: u8 = 33;
    pub const PPS: u8 = 34;
    pub const AUD: u8 = 35;

    pub fn is_irap(t: u8) -> bool {
        (16..=23).contains(&t)
    }
}

/// One access unit as it appeared on stdin (start codes included).
#[derive(Debug, Clone)]
pub struct AccessUnit {
    pub data: Bytes,
    pub key: bool,
}

/// Start-code search: returns index of the `00 00 01` triple at or after `from`.
fn find_start_code(buf: &[u8], from: usize) -> Option<usize> {
    let mut i = from;
    while i + 3 <= buf.len() {
        // Skip fast when byte 2 can't be a start code tail.
        let b2 = buf[i + 2];
        if b2 > 1 {
            i += 3;
            continue;
        }
        if b2 == 1 && buf[i] == 0 && buf[i + 1] == 0 {
            return Some(i);
        }
        i += 1;
    }
    None
}

/// NAL units of one AU: (start-code begin, header index, end).
pub fn nal_units(au: &[u8]) -> Vec<(usize, usize, usize)> {
    let mut starts = Vec::new();
    let mut from = 0;
    while let Some(sc) = find_start_code(au, from) {
        let begin = if sc > 0 && au[sc - 1] == 0 { sc - 1 } else { sc };
        starts.push((begin, sc + 3));
        from = sc + 3;
    }
    let mut out = Vec::with_capacity(starts.len());
    for (k, &(begin, hdr)) in starts.iter().enumerate() {
        let end = starts.get(k + 1).map(|s| s.0).unwrap_or(au.len());
        if hdr < end {
            out.push((begin, hdr, end));
        }
    }
    out
}

pub fn nal_type(header_byte: u8) -> u8 {
    (header_byte >> 1) & 0x3f
}

/// Streaming AUD-delimited splitter. Feed bytes, pull whole AUs.
pub struct AuSplitter {
    buf: BytesMut,
    search_from: usize,
    /// Latest VPS/SPS/PPS seen, re-inserted into keyframes that lack them so a
    /// late-joining receiver can always start decoding at an IRAP.
    params: [Option<Bytes>; 3],
}

impl Default for AuSplitter {
    fn default() -> Self {
        Self::new()
    }
}

impl AuSplitter {
    pub fn new() -> Self {
        Self {
            buf: BytesMut::with_capacity(4 << 20),
            search_from: 0,
            params: [None, None, None],
        }
    }

    pub fn push(&mut self, data: &[u8], out: &mut Vec<AccessUnit>) {
        self.buf.extend_from_slice(data);
        loop {
            let Some(sc) = find_start_code(&self.buf, self.search_from) else {
                self.search_from = self.buf.len().saturating_sub(2);
                return;
            };
            if sc + 3 >= self.buf.len() {
                // Need the NAL header byte.
                self.search_from = sc;
                return;
            }
            let t = nal_type(self.buf[sc + 3]);
            let begin = if sc > 0 && self.buf[sc - 1] == 0 { sc - 1 } else { sc };
            if t == nal::AUD && begin > 0 {
                let au = self.buf.split_to(begin).freeze();
                out.push(self.finish(au));
                self.search_from = sc - begin + 3;
            } else {
                self.search_from = sc + 3;
            }
        }
    }

    pub fn flush(&mut self, out: &mut Vec<AccessUnit>) {
        if !self.buf.is_empty() {
            let au = self.buf.split().freeze();
            out.push(self.finish(au));
        }
        self.search_from = 0;
    }

    fn finish(&mut self, au: Bytes) -> AccessUnit {
        let units = nal_units(&au);
        let mut key = false;
        let mut have = [false; 3];
        for &(begin, hdr, end) in &units {
            let t = nal_type(au[hdr]);
            if nal::is_irap(t) {
                key = true;
            }
            if (nal::VPS..=nal::PPS).contains(&t) {
                let i = (t - nal::VPS) as usize;
                have[i] = true;
                self.params[i] = Some(au.slice(begin..end));
            }
        }
        if !key || have.iter().all(|h| *h) || self.params.iter().any(|p| p.is_none()) {
            return AccessUnit { data: au, key };
        }
        // Keyframe without in-band parameter sets: insert cached ones after the AUD.
        let aud_end = units
            .first()
            .filter(|&&(_, hdr, _)| nal_type(au[hdr]) == nal::AUD)
            .map(|&(_, _, end)| end)
            .unwrap_or(0);
        let mut rebuilt = BytesMut::with_capacity(au.len() + 256);
        rebuilt.extend_from_slice(&au[..aud_end]);
        for (i, p) in self.params.iter().enumerate() {
            if !have[i] {
                rebuilt.extend_from_slice(p.as_ref().unwrap());
            }
        }
        rebuilt.extend_from_slice(&au[aud_end..]);
        AccessUnit {
            data: rebuilt.freeze(),
            key,
        }
    }
}

// --------------------------------------------------------- certificate ----

pub fn default_cert_dir() -> PathBuf {
    std::env::temp_dir().join("qcbridge-agent")
}

pub fn sha256_hex(der: &[u8]) -> String {
    hex::encode(ring::digest::digest(&ring::digest::SHA256, der))
}

/// Load `cert.der` + `key.der` from `dir`, or create a self-signed pair there.
pub fn load_or_create_cert(
    dir: &Path,
) -> Result<(
    rustls::pki_types::CertificateDer<'static>,
    rustls::pki_types::PrivateKeyDer<'static>,
    String,
)> {
    let cert_path = dir.join("cert.der");
    let key_path = dir.join("key.der");
    let (cert_der, key_der) = if cert_path.exists() && key_path.exists() {
        (
            std::fs::read(&cert_path).with_context(|| format!("read {}", cert_path.display()))?,
            std::fs::read(&key_path).with_context(|| format!("read {}", key_path.display()))?,
        )
    } else {
        std::fs::create_dir_all(dir).with_context(|| format!("create {}", dir.display()))?;
        let ck = rcgen::generate_simple_self_signed(vec!["localhost".to_string()])
            .context("generate self-signed certificate")?;
        let cert_der = ck.cert.der().to_vec();
        let key_der = ck.key_pair.serialize_der();
        std::fs::write(&cert_path, &cert_der)?;
        std::fs::write(&key_path, &key_der)?;
        (cert_der, key_der)
    };
    if cert_der.is_empty() || key_der.is_empty() {
        bail!("empty certificate or key in {}", dir.display());
    }
    let fingerprint = sha256_hex(&cert_der);
    let cert = rustls::pki_types::CertificateDer::from(cert_der);
    let key = rustls::pki_types::PrivateKeyDer::Pkcs8(rustls::pki_types::PrivatePkcs8KeyDer::from(
        key_der,
    ));
    Ok((cert, key, fingerprint))
}

// ------------------------------------------------------ rate controller ----

/// Wire budget for a video rate: RaptorQ adds a fixed 30 % repair (min 2
/// symbols) plus per-datagram headers; Plank budgets video x 1.35 + 1 Mbps.
pub fn video_to_wire_bps(video_bps: u64) -> u64 {
    video_bps.saturating_mul(27).div_ceil(20).saturating_add(1_000_000)
}

/// Fixed window sized from the wire cap and RTT, ignoring loss: the pacer in
/// front of Quinn owns the rate; this only keeps CUBIC's window from
/// throttling or collapsing on repairable loss (same idea as Plank's
/// rate_control.rs, written fresh for the spike).
#[derive(Clone)]
pub struct FixedRateController {
    wire_bps: u64,
    mtu: u16,
    rtt: Duration,
    window: u64,
}

impl FixedRateController {
    fn window_for(wire_bps: u64, rtt: Duration, mtu: u16) -> u64 {
        let rtt_ns = rtt.min(Duration::from_millis(100)).as_nanos().max(1);
        let bdp = (wire_bps as u128 * rtt_ns).div_ceil(8_000_000_000) as u64;
        (bdp * 3 / 2).max(64 * mtu as u64)
    }
}

impl Controller for FixedRateController {
    fn on_ack(&mut self, _now: Instant, _sent: Instant, _bytes: u64, _app_limited: bool, rtt: &RttEstimator) {
        self.rtt = rtt.get();
        self.window = Self::window_for(self.wire_bps, self.rtt, self.mtu);
    }

    fn on_congestion_event(&mut self, _now: Instant, _sent: Instant, _persistent: bool, _lost: u64) {}

    fn on_mtu_update(&mut self, new_mtu: u16) {
        self.mtu = new_mtu;
        self.window = Self::window_for(self.wire_bps, self.rtt, self.mtu);
    }

    fn window(&self) -> u64 {
        self.window
    }

    fn metrics(&self) -> ControllerMetrics {
        let mut m = ControllerMetrics::default();
        m.congestion_window = self.window;
        m.pacing_rate = Some(self.wire_bps);
        m
    }

    fn clone_box(&self) -> Box<dyn Controller> {
        Box::new(self.clone())
    }

    fn initial_window(&self) -> u64 {
        self.window
    }

    fn into_any(self: Box<Self>) -> Box<dyn Any> {
        self
    }
}

pub struct FixedRateControllerFactory {
    pub wire_bps: u64,
}

impl ControllerFactory for FixedRateControllerFactory {
    fn build(self: Arc<Self>, _now: Instant, current_mtu: u16) -> Box<dyn Controller> {
        let rtt = Duration::from_millis(200);
        Box::new(FixedRateController {
            wire_bps: self.wire_bps,
            mtu: current_mtu,
            rtt,
            window: FixedRateController::window_for(self.wire_bps, rtt, current_mtu),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn au(types: &[u8], four: bool) -> Vec<u8> {
        let mut v = Vec::new();
        for &t in types {
            if four {
                v.push(0);
            }
            v.extend_from_slice(&[0, 0, 1, t << 1, 1, 0xaa, 0xbb]);
        }
        v
    }

    #[test]
    fn splits_on_aud_across_arbitrary_chunking() {
        let mut stream = Vec::new();
        stream.extend(au(&[35, 32, 33, 34, 19], true));
        stream.extend(au(&[35, 1], true));
        stream.extend(au(&[35, 1], false));
        for chunk in [1usize, 2, 3, 5, 7, 1000] {
            let mut s = AuSplitter::new();
            let mut out = Vec::new();
            for c in stream.chunks(chunk) {
                s.push(c, &mut out);
            }
            s.flush(&mut out);
            assert_eq!(out.len(), 3, "chunk {chunk}");
            assert!(out[0].key && !out[1].key && !out[2].key);
            let joined: Vec<u8> = out.iter().flat_map(|a| a.data.to_vec()).collect();
            assert_eq!(joined, stream);
        }
    }

    #[test]
    fn reinserts_parameter_sets_into_bare_keyframes() {
        let mut stream = Vec::new();
        stream.extend(au(&[35, 32, 33, 34, 19], true));
        stream.extend(au(&[35, 19], true));
        let mut s = AuSplitter::new();
        let mut out = Vec::new();
        s.push(&stream, &mut out);
        s.flush(&mut out);
        let types: Vec<u8> = nal_units(&out[1].data)
            .iter()
            .map(|&(_, h, _)| nal_type(out[1].data[h]))
            .collect();
        assert_eq!(types, vec![35, 32, 33, 34, 19]);
    }
}

pub mod blender;
pub mod config;
pub mod link;
pub mod session;
pub mod video;
