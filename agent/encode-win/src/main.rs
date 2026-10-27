//! qcb-encode-win — the replica's encoder on Windows (QCBridge 0.3), the
//! twin of `qcb-encode-mac`.
//!
//! Blender's addon publishes scene-linear RGBA float frames into a
//! shared-memory ring (qcbridge/ring1/ring_layout.py: a header page, three
//! slots of descriptor page + page-aligned pixels, a seqlock per slot and
//! `latest = (seq << 8) | slot`). This helper maps the ring, uploads each new
//! frame into a float texture and converts it with a pixel shader straight
//! into the planes of a P010 texture — linear Rec.709 → BT.2020 → PQ with
//! scene 1.0 = 100 nit → BT.2020 Y'CbCr 10-bit limited 4:2:0
//! (qcbridge/ring1/wire_format.py is the reference; `--selftest` checks the
//! shader against its vectors) — and hands that texture to the vendor's
//! hardware HEVC encoder behind Media Foundation, on the same D3D11 device.
//! The output is Annex-B on stdout with an AUD per access unit and
//! VPS/SPS/PPS in band on every keyframe, which the addon pipes into
//! `ffmpeg -c copy` for SRT. stdin takes one-line commands: "key" forces a
//! keyframe on the next frame (keyframe-on-join), "quit" exits. No screen
//! capture anywhere (DESIGN-NOTES pixels).
//!
//!   cargo build --release            (in agent/encode-win)
//!   qcb-encode-win --shm qcb-1234-1 [--fps 30] [--bitrate 50] [--gop 30] [--cpu]
//!   qcb-encode-win --selftest tests\fixtures\pq_vectors.json
//!
//! Exit codes: 0 producer gone / quit, 2 bad args, 3 encoder, 5 ring
//! unreadable, 7 ring retired (the producer re-created it: respawn).

use std::collections::HashMap;
use std::io::{BufRead, Write};
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering::Relaxed};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use windows::Win32::Foundation::{CloseHandle, HANDLE, STILL_ACTIVE, VARIANT_TRUE};
use windows::Win32::Graphics::Direct3D::Fxc::D3DCompile;
use windows::Win32::Graphics::Direct3D::{D3D_DRIVER_TYPE_HARDWARE, D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST};
use windows::Win32::Graphics::Direct3D11::*;
use windows::Win32::Graphics::Dxgi::Common::*;
use windows::Win32::Media::MediaFoundation::*;
use windows::Win32::System::Com::{CoInitializeEx, COINIT_MULTITHREADED};
use windows::Win32::System::Memory::{
    MapViewOfFile, OpenFileMappingW, VirtualQuery, FILE_MAP_ALL_ACCESS, MEMORY_BASIC_INFORMATION,
};
use windows::Win32::System::Threading::{GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION};
use windows::Win32::System::Variant::{VARENUM, VARIANT, VARIANT_0, VARIANT_0_0, VARIANT_0_0_0, VT_BOOL, VT_UI4};
use windows::core::{Interface, PCSTR, PCWSTR, Result};

// ------------------------------------------------------------ options ----

struct Opts {
    shm: String,
    fps: u32,
    mbps: u32,
    gop: u32,
    cpu: bool,
    selftest: Option<String>,
}

fn parse_args() -> Opts {
    let mut o = Opts { shm: String::new(), fps: 30, mbps: 50, gop: 30, cpu: false, selftest: None };
    let mut it = std::env::args().skip(1);
    while let Some(a) = it.next() {
        match a.as_str() {
            "--shm" => o.shm = it.next().unwrap_or_default(),
            "--fps" => o.fps = it.next().and_then(|v| v.parse().ok()).unwrap_or(30),
            "--bitrate" => o.mbps = it.next().and_then(|v| v.parse().ok()).unwrap_or(50),
            "--gop" => o.gop = it.next().and_then(|v| v.parse().ok()).unwrap_or(30),
            "--cpu" => o.cpu = true,
            "--selftest" => o.selftest = it.next(),
            other => {
                eprintln!("unknown arg {other}");
                std::process::exit(2);
            }
        }
    }
    if o.shm.is_empty() && o.selftest.is_none() {
        eprintln!("usage: qcb-encode-win --shm NAME [--fps N] [--bitrate M] [--gop G] [--cpu] | --selftest vectors.json");
        std::process::exit(2);
    }
    o
}

fn log(s: impl AsRef<str>) {
    eprintln!("[encode] {}", s.as_ref());
}

fn variant(vt: VARENUM, value: VARIANT_0_0_0) -> VARIANT {
    VARIANT { Anonymous: VARIANT_0 { Anonymous: std::mem::ManuallyDrop::new(VARIANT_0_0 { vt, wReserved1: 0, wReserved2: 0, wReserved3: 0, Anonymous: value }) } }
}
fn variant_u32(v: u32) -> VARIANT {
    variant(VT_UI4, VARIANT_0_0_0 { ulVal: v })
}
fn variant_bool(v: bool) -> VARIANT {
    variant(VT_BOOL, VARIANT_0_0_0 { boolVal: if v { VARIANT_TRUE } else { windows::Win32::Foundation::VARIANT_FALSE } })
}

/// COM interface pointers are not `Send` in windows-rs; the objects here
/// (a D3D11 device with multithread protection, Media Foundation's free-
/// threaded objects, textures) are safe to use from the encoder thread.
struct Sendable<T>(T);
unsafe impl<T> Send for Sendable<T> {}
impl<T> Sendable<T> {
    fn into_inner(self) -> T {
        self.0
    }
}

// ------------------------------------------------------------ ring -------
// Mirrors qcbridge/ring1/ring_layout.py; the Python side is the source of truth.

const RING_MAGIC: u32 = 0x4642_4351; // "QCBF" little-endian
const RING_VERSION: u32 = 1;
const PAGE: usize = 4096;
// "<4sIIIQQQQQQId": magic 0, version 4, slot_count 8, producer_pid 12,
// slot_stride 16, pixels_offset 24, pixels_capacity 32, total_size 40,
// latest 48, reader_claim 56, state 64.
const HDR_LATEST: usize = 48;
const HDR_READER_CLAIM: usize = 56;
const HDR_STATE: usize = 64;
const STATE_RETIRED: u32 = 1;
// "<QIIIIIIIdddQId": seqlock 0, w 8, h 12, bpr 16, pf 20, flags 24, frame 28.
const DESC_W: usize = 8;
const DESC_H: usize = 12;
const DESC_BPR: usize = 16;
const DESC_PF: usize = 20;
const PF_RGBA32F: u32 = 4;

struct Ring {
    base: *mut u8,
    slot_count: usize,
    producer_pid: u32,
    slot_stride: usize,
    pixels_offset: usize,
    pixels_capacity: usize,
}

impl Ring {
    fn open(name: &str) -> Option<Ring> {
        let wide: Vec<u16> = name.encode_utf16().chain(std::iter::once(0)).collect();
        let handle = match unsafe { OpenFileMappingW(FILE_MAP_ALL_ACCESS.0, false, PCWSTR(wide.as_ptr())) } {
            Ok(h) => h,
            Err(e) => {
                log(format!("OpenFileMapping {name}: {e}"));
                return None;
            }
        };
        let view = unsafe { MapViewOfFile(handle, FILE_MAP_ALL_ACCESS, 0, 0, 0) };
        let _ = unsafe { CloseHandle(handle) };
        if view.Value.is_null() {
            log("MapViewOfFile failed");
            return None;
        }
        let base = view.Value as *mut u8;
        let mut info = MEMORY_BASIC_INFORMATION::default();
        let n = unsafe { VirtualQuery(Some(base as *const _), &mut info, std::mem::size_of::<MEMORY_BASIC_INFORMATION>()) };
        let size = if n == 0 { 0 } else { info.RegionSize };
        if size < PAGE {
            log("ring too small");
            return None;
        }
        let rd32 = |off: usize| unsafe { std::ptr::read_unaligned(base.add(off) as *const u32) };
        let rd64 = |off: usize| unsafe { std::ptr::read_unaligned(base.add(off) as *const u64) };
        if rd32(0) != RING_MAGIC || rd32(4) != RING_VERSION {
            log(format!("ring magic/version mismatch: {:08x} v{}", rd32(0), rd32(4)));
            return None;
        }
        let r = Ring {
            base,
            slot_count: rd32(8) as usize,
            producer_pid: rd32(12),
            slot_stride: rd64(16) as usize,
            pixels_offset: rd64(24) as usize,
            pixels_capacity: rd64(32) as usize,
        };
        let total = rd64(40) as usize;
        if total > size {
            log(format!("ring total_size {total} > view {size}"));
            return None;
        }
        Some(r)
    }
    fn u32_at(&self, off: usize) -> u32 {
        unsafe { std::ptr::read_volatile(self.base.add(off) as *const u32) }
    }
    fn u64_at(&self, off: usize) -> u64 {
        unsafe { std::ptr::read_volatile(self.base.add(off) as *const u64) }
    }
    fn latest(&self) -> u64 {
        self.u64_at(HDR_LATEST)
    }
    fn state(&self) -> u32 {
        self.u32_at(HDR_STATE)
    }
    fn claim(&self, slot: usize) {
        unsafe { std::ptr::write_volatile(self.base.add(HDR_READER_CLAIM) as *mut u64, slot as u64 + 1) }
    }
    fn release(&self) {
        unsafe { std::ptr::write_volatile(self.base.add(HDR_READER_CLAIM) as *mut u64, 0) }
    }
    fn slot_offset(&self, slot: usize) -> usize {
        PAGE + slot * self.slot_stride
    }
    fn seqlock(&self, slot: usize) -> u64 {
        self.u64_at(self.slot_offset(slot))
    }
    fn desc_u32(&self, slot: usize, off: usize) -> u32 {
        self.u32_at(self.slot_offset(slot) + off)
    }
    fn pixels(&self, slot: usize) -> *const u8 {
        unsafe { self.base.add(self.slot_offset(slot) + self.pixels_offset) }
    }
    fn producer_alive(&self) -> bool {
        let Ok(h) = (unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, self.producer_pid) }) else { return false };
        let mut code = 0u32;
        let ok = unsafe { GetExitCodeProcess(h, &mut code) }.is_ok();
        let _ = unsafe { CloseHandle(h) };
        ok && code == STILL_ACTIVE.0 as u32
    }
}

// ------------------------------------------------------------ shared -----

#[derive(Default)]
struct Stats {
    frames_in: AtomicU64,
    frames_out: AtomicU64,
    bytes_out: AtomicU64,
    keys_out: AtomicU64,
    dropped: AtomicU64, // skipped because 3 frames were in flight
    stale: AtomicU64,   // the producer overwrote the slot while we converted
    encode_us_sum: AtomicI64,
    encode_us_max: AtomicI64,
    convert_us_sum: AtomicI64,
}

struct Shared {
    stats: Stats,
    force_key: AtomicBool,
    in_flight: AtomicI64,
    submit_times: Mutex<HashMap<i64, Instant>>, // sample time (100 ns) -> submit
    out: Mutex<std::io::Stdout>,
    // Latest VPS/SPS/PPS seen, prepended to a keyframe that lacks them.
    params: Mutex<[Option<Vec<u8>>; 3]>,
}

const AUD_NAL: [u8; 7] = [0, 0, 0, 1, 0x46, 0x01, 0x50]; // NAL type 35, pic_type all

fn nal_type(h: u8) -> u8 {
    (h >> 1) & 0x3f
}

/// (start-code begin, header index, end) of each NAL in an Annex-B buffer.
fn nal_units(au: &[u8]) -> Vec<(usize, usize, usize)> {
    let mut starts = Vec::new();
    let mut i = 0;
    while i + 3 <= au.len() {
        if au[i] == 0 && au[i + 1] == 0 && au[i + 2] == 1 {
            let begin = if i > 0 && au[i - 1] == 0 { i - 1 } else { i };
            starts.push((begin, i + 3));
            i += 3;
        } else {
            i += 1;
        }
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

/// One encoder output -> one Annex-B access unit on stdout: AUD first, the
/// parameter sets before a keyframe that carries none, then the NALs as the
/// encoder wrote them (already Annex-B from a Media Foundation HEVC encoder).
fn emit(shared: &Shared, data: &[u8], key_hint: bool) {
    let units = nal_units(data);
    let mut key = key_hint;
    let mut has_aud = false;
    let mut have = [false; 3];
    {
        let mut params = shared.params.lock().unwrap();
        for &(begin, hdr, end) in &units {
            let t = nal_type(data[hdr]);
            match t {
                32..=34 => {
                    have[(t - 32) as usize] = true;
                    params[(t - 32) as usize] = Some(data[begin..end].to_vec());
                }
                16..=23 => key = true,
                35 => has_aud = true,
                _ => {}
            }
        }
    }
    let mut au = Vec::with_capacity(data.len() + 64);
    if !has_aud {
        au.extend_from_slice(&AUD_NAL);
    }
    if key && !(have[0] && have[1] && have[2]) {
        let params = shared.params.lock().unwrap();
        for p in params.iter().flatten() {
            au.extend_from_slice(p);
        }
    }
    au.extend_from_slice(data);
    let mut out = shared.out.lock().unwrap();
    if out.write_all(&au).and_then(|_| out.flush()).is_err() {
        std::process::exit(0); // the addon went away
    }
    shared.stats.frames_out.fetch_add(1, Relaxed);
    shared.stats.bytes_out.fetch_add(au.len() as u64, Relaxed);
    if key {
        shared.stats.keys_out.fetch_add(1, Relaxed);
    }
}

// ------------------------------------------------------------ D3D11 ------

struct Gpu {
    device: ID3D11Device,
    context: ID3D11DeviceContext,
}

fn make_gpu() -> Result<Gpu> {
    let mut device = None;
    let mut context = None;
    unsafe {
        D3D11CreateDevice(
            None,
            D3D_DRIVER_TYPE_HARDWARE,
            windows::Win32::Foundation::HMODULE::default(),
            D3D11_CREATE_DEVICE_BGRA_SUPPORT | D3D11_CREATE_DEVICE_VIDEO_SUPPORT,
            None,
            D3D11_SDK_VERSION,
            Some(&mut device),
            None,
            Some(&mut context),
        )?;
    }
    let device = device.unwrap();
    let context = context.unwrap();
    // The encoder works this device from Media Foundation's threads.
    let mt: ID3D11Multithread = context.cast()?;
    let _ = unsafe { mt.SetMultithreadProtected(true) };
    Ok(Gpu { device, context })
}

// The conversion, as HLSL. Mirrors qcbridge/ring1/wire_format.py and the
// Mac's Metal kernels: rows in the ring are bottom-up (Blender's
// framebuffer order), so picture row y reads source row h-1-y.
const HLSL: &str = r#"
cbuffer P : register(b0) { uint w; uint h; uint pad0; uint pad1; };
Texture2D<float4> src : register(t0);

struct VSOut { float4 pos : SV_Position; };
VSOut vs_main(uint id : SV_VertexID) {
    VSOut o;
    float2 uv = float2((id << 1) & 2, id & 2);
    o.pos = float4(uv * float2(2.0, -2.0) + float2(-1.0, 1.0), 0.0, 1.0);
    return o;
}

static const float3x3 M709_2020 = float3x3(0.6274, 0.3293, 0.0433,
                                           0.0691, 0.9195, 0.0114,
                                           0.0164, 0.0880, 0.8956);

float pq(float lin) {
    float L = clamp(lin * (100.0 / 10000.0), 0.0, 1.0);   // scene 1.0 = 100 nit
    float Lm = pow(L, 0.1593017578125);
    return pow((0.8359375 + 18.8515625 * Lm) / (1.0 + 18.6875 * Lm), 78.84375);
}

float3 rgbp(uint x, uint y) {
    float3 c = max(mul(M709_2020, src.Load(int3(x, h - 1 - y, 0)).rgb), 0.0);
    return float3(pq(c.r), pq(c.g), pq(c.b));
}

float luma(float3 e) { return 0.2627 * e.r + 0.6780 * e.g + 0.0593 * e.b; }

float ps_luma(float4 pos : SV_Position) : SV_Target {
    uint x = (uint)pos.x, y = (uint)pos.y;
    float Y = luma(rgbp(x, y));
    float code = clamp(round(876.0 * Y + 64.0), 64.0, 940.0);
    return code * 64.0 / 65535.0;     // 10-bit code, MSB-aligned in the 16-bit plane
}

float2 ps_chroma(float4 pos : SV_Position) : SV_Target {
    uint x = (uint)pos.x, y = (uint)pos.y;
    float cb = 0.0, cr = 0.0;
    [unroll] for (uint dy = 0; dy < 2; dy++) {
        [unroll] for (uint dx = 0; dx < 2; dx++) {
            float3 e = rgbp(2 * x + dx, 2 * y + dy);
            float Y = luma(e);
            cb += (e.b - Y) / 1.8814;
            cr += (e.r - Y) / 1.4746;
        }
    }
    cb *= 0.25; cr *= 0.25;           // 2x2 average: centre siting
    float cbc = clamp(round(896.0 * cb + 512.0), 64.0, 960.0);
    float crc = clamp(round(896.0 * cr + 512.0), 64.0, 960.0);
    return float2(cbc, crc) * 64.0 / 65535.0;
}
"#;

fn compile(entry: &str, target: &str) -> Result<Vec<u8>> {
    let entry_c = format!("{entry}\0");
    let target_c = format!("{target}\0");
    let mut code = None;
    let mut errors = None;
    let r = unsafe {
        D3DCompile(
            HLSL.as_ptr() as *const _,
            HLSL.len(),
            PCSTR(b"wire.hlsl\0".as_ptr()),
            None,
            None,
            PCSTR(entry_c.as_ptr()),
            PCSTR(target_c.as_ptr()),
            0,
            0,
            &mut code,
            Some(&mut errors),
        )
    };
    if let Err(e) = r {
        if let Some(blob) = errors {
            let msg = unsafe { std::slice::from_raw_parts(blob.GetBufferPointer() as *const u8, blob.GetBufferSize()) };
            log(format!("shader {entry}: {}", String::from_utf8_lossy(msg)));
        }
        return Err(e);
    }
    let blob = code.unwrap();
    let bytes = unsafe { std::slice::from_raw_parts(blob.GetBufferPointer() as *const u8, blob.GetBufferSize()) };
    Ok(bytes.to_vec())
}

/// Float RGBA (CPU) -> P010 texture, on the GPU: upload into a dynamic
/// float texture, then two full-screen passes write the luma plane
/// (R16_UNORM view) and the chroma plane (R16G16_UNORM view) of a pooled
/// P010 render target — the per-plane render-target views D3D 11.1 gives
/// planar formats.
struct GpuConverter {
    w: u32,
    h: u32,
    src: ID3D11Texture2D,
    srv: ID3D11ShaderResourceView,
    cbuf: ID3D11Buffer,
    vs: ID3D11VertexShader,
    ps_luma: ID3D11PixelShader,
    ps_chroma: ID3D11PixelShader,
    outputs: Vec<(ID3D11Texture2D, ID3D11RenderTargetView, ID3D11RenderTargetView)>,
    next: usize,
}

fn p010_render_targets_supported(gpu: &Gpu) -> bool {
    match unsafe { gpu.device.CheckFormatSupport(DXGI_FORMAT_P010) } {
        Ok(f) => f & D3D11_FORMAT_SUPPORT_RENDER_TARGET.0 as u32 != 0,
        Err(_) => false,
    }
}

fn make_p010(gpu: &Gpu, w: u32, h: u32, usage: D3D11_USAGE, bind: u32, cpu: u32) -> Result<ID3D11Texture2D> {
    let td = D3D11_TEXTURE2D_DESC {
        Width: w,
        Height: h,
        MipLevels: 1,
        ArraySize: 1,
        Format: DXGI_FORMAT_P010,
        SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
        Usage: usage,
        BindFlags: bind,
        CPUAccessFlags: cpu,
        MiscFlags: 0,
    };
    let mut tex = None;
    unsafe { gpu.device.CreateTexture2D(&td, None, Some(&mut tex))? };
    Ok(tex.unwrap())
}

impl GpuConverter {
    fn new(gpu: &Gpu, w: u32, h: u32) -> Result<Self> {
        let td = D3D11_TEXTURE2D_DESC {
            Width: w,
            Height: h,
            MipLevels: 1,
            ArraySize: 1,
            Format: DXGI_FORMAT_R32G32B32A32_FLOAT,
            SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
            Usage: D3D11_USAGE_DYNAMIC,
            BindFlags: D3D11_BIND_SHADER_RESOURCE.0 as u32,
            CPUAccessFlags: D3D11_CPU_ACCESS_WRITE.0 as u32,
            MiscFlags: 0,
        };
        let mut src = None;
        unsafe { gpu.device.CreateTexture2D(&td, None, Some(&mut src))? };
        let src = src.unwrap();
        let mut srv = None;
        unsafe { gpu.device.CreateShaderResourceView(&src, None, Some(&mut srv))? };
        let srv = srv.unwrap();
        let consts = [w, h, 0u32, 0u32];
        let bd = D3D11_BUFFER_DESC {
            ByteWidth: 16,
            Usage: D3D11_USAGE_IMMUTABLE,
            BindFlags: D3D11_BIND_CONSTANT_BUFFER.0 as u32,
            CPUAccessFlags: 0,
            MiscFlags: 0,
            StructureByteStride: 0,
        };
        let init = D3D11_SUBRESOURCE_DATA { pSysMem: consts.as_ptr() as *const _, SysMemPitch: 0, SysMemSlicePitch: 0 };
        let mut cbuf = None;
        unsafe { gpu.device.CreateBuffer(&bd, Some(&init), Some(&mut cbuf))? };
        let vs_code = compile("vs_main", "vs_5_0")?;
        let luma_code = compile("ps_luma", "ps_5_0")?;
        let chroma_code = compile("ps_chroma", "ps_5_0")?;
        let (mut vs, mut ps_luma, mut ps_chroma) = (None, None, None);
        unsafe {
            gpu.device.CreateVertexShader(&vs_code, None, Some(&mut vs))?;
            gpu.device.CreatePixelShader(&luma_code, None, Some(&mut ps_luma))?;
            gpu.device.CreatePixelShader(&chroma_code, None, Some(&mut ps_chroma))?;
        }
        let mut outputs = Vec::new();
        for _ in 0..4 {
            let tex = make_p010(gpu, w, h, D3D11_USAGE_DEFAULT, D3D11_BIND_RENDER_TARGET.0 as u32, 0)?;
            let rtv = |fmt: DXGI_FORMAT| -> Result<ID3D11RenderTargetView> {
                let desc = D3D11_RENDER_TARGET_VIEW_DESC {
                    Format: fmt,
                    ViewDimension: D3D11_RTV_DIMENSION_TEXTURE2D,
                    Anonymous: D3D11_RENDER_TARGET_VIEW_DESC_0 { Texture2D: D3D11_TEX2D_RTV { MipSlice: 0 } },
                };
                let mut v = None;
                unsafe { gpu.device.CreateRenderTargetView(&tex, Some(&desc), Some(&mut v))? };
                Ok(v.unwrap())
            };
            let luma_rtv = rtv(DXGI_FORMAT_R16_UNORM)?;
            let chroma_rtv = rtv(DXGI_FORMAT_R16G16_UNORM)?;
            outputs.push((tex, luma_rtv, chroma_rtv));
        }
        Ok(Self {
            w,
            h,
            src,
            srv,
            cbuf: cbuf.unwrap(),
            vs: vs.unwrap(),
            ps_luma: ps_luma.unwrap(),
            ps_chroma: ps_chroma.unwrap(),
            outputs,
            next: 0,
        })
    }

    /// `pixels` — rows bottom-up, `bpr` bytes each, RGBA32F. Returns the
    /// P010 texture holding the converted frame.
    fn convert(&mut self, gpu: &Gpu, pixels: *const u8, bpr: usize) -> Result<ID3D11Texture2D> {
        let ctx = &gpu.context;
        let mut mapped = D3D11_MAPPED_SUBRESOURCE::default();
        unsafe {
            ctx.Map(&self.src, 0, D3D11_MAP_WRITE_DISCARD, 0, Some(&mut mapped))?;
            let row_bytes = self.w as usize * 16;
            for y in 0..self.h as usize {
                std::ptr::copy_nonoverlapping(
                    pixels.add(y * bpr),
                    (mapped.pData as *mut u8).add(y * mapped.RowPitch as usize),
                    row_bytes,
                );
            }
            ctx.Unmap(&self.src, 0);
        }
        let (tex, luma_rtv, chroma_rtv) = &self.outputs[self.next];
        self.next = (self.next + 1) % self.outputs.len();
        unsafe {
            ctx.IASetInputLayout(None);
            ctx.IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
            ctx.VSSetShader(&self.vs, None);
            ctx.PSSetConstantBuffers(0, Some(&[Some(self.cbuf.clone())]));
            ctx.PSSetShaderResources(0, Some(&[Some(self.srv.clone())]));
            // luma plane, full size
            ctx.OMSetRenderTargets(Some(&[Some(luma_rtv.clone())]), None);
            ctx.RSSetViewports(Some(&[D3D11_VIEWPORT { TopLeftX: 0.0, TopLeftY: 0.0, Width: self.w as f32, Height: self.h as f32, MinDepth: 0.0, MaxDepth: 1.0 }]));
            ctx.PSSetShader(&self.ps_luma, None);
            ctx.Draw(3, 0);
            // chroma plane, half size
            ctx.OMSetRenderTargets(Some(&[Some(chroma_rtv.clone())]), None);
            ctx.RSSetViewports(Some(&[D3D11_VIEWPORT { TopLeftX: 0.0, TopLeftY: 0.0, Width: (self.w / 2) as f32, Height: (self.h / 2) as f32, MinDepth: 0.0, MaxDepth: 1.0 }]));
            ctx.PSSetShader(&self.ps_chroma, None);
            ctx.Draw(3, 0);
            ctx.OMSetRenderTargets(None, None);
            ctx.PSSetShaderResources(0, Some(&[None]));
            // The draws sit in the immediate context's command buffer until
            // something flushes it; the encoder cannot start before then.
            ctx.Flush();
        }
        Ok(tex.clone())
    }
}

// ------------------------------------------------------------ CPU path ---
// Only when the GPU cannot render into P010 planes (an old driver): the
// same maths on the CPU across all cores, into a staging P010 texture that
// is copied to a default one for the encoder. Slow at 4K; a fallback.

fn pq(lin: f32) -> f32 {
    let l = (lin * (100.0 / 10000.0)).clamp(0.0, 1.0);
    let lm = l.powf(0.1593017578125);
    ((0.8359375 + 18.8515625 * lm) / (1.0 + 18.6875 * lm)).powf(78.84375)
}

#[inline]
fn rgbp(px: &[f32]) -> (f32, f32, f32) {
    let (r, g, b) = (px[0], px[1], px[2]);
    let r2 = (0.6274 * r + 0.3293 * g + 0.0433 * b).max(0.0);
    let g2 = (0.0691 * r + 0.9195 * g + 0.0114 * b).max(0.0);
    let b2 = (0.0164 * r + 0.0880 * g + 0.8956 * b).max(0.0);
    (pq(r2), pq(g2), pq(b2))
}

#[inline]
fn luma(e: (f32, f32, f32)) -> f32 {
    0.2627 * e.0 + 0.6780 * e.1 + 0.0593 * e.2
}

/// Convert on the CPU into `y` (w×h words) and `c` (w/2×h/2 pairs), rows
/// top-down, 16-bit words MSB-aligned.
fn convert_cpu(pixels: *const u8, bpr: usize, w: usize, h: usize, y_plane: &mut [u16], y_pitch: usize, c_plane: &mut [u16], c_pitch: usize) {
    let src = unsafe { std::slice::from_raw_parts(pixels, bpr * h) };
    let threads = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4).min(32);
    let pair_rows = h / 2;
    let chunk = (pair_rows + threads - 1) / threads;
    std::thread::scope(|s| {
        let y_chunks = y_plane.chunks_mut(y_pitch * 2 * chunk);
        let c_chunks = c_plane.chunks_mut(c_pitch * chunk);
        for (t, (yc, cc)) in y_chunks.zip(c_chunks).enumerate() {
            s.spawn(move || {
                let first_pair = t * chunk;
                for (pr, crow) in cc.chunks_mut(c_pitch).enumerate() {
                    let pair = first_pair + pr;
                    if pair >= pair_rows {
                        break;
                    }
                    let y0 = 2 * pair;
                    for dy in 0..2 {
                        let py = y0 + dy;
                        let srow = h - 1 - py; // ring rows are bottom-up
                        let srcrow = &src[srow * bpr..srow * bpr + w * 16];
                        let yrow = &mut yc[(2 * pr + dy) * y_pitch..(2 * pr + dy) * y_pitch + w];
                        for x in 0..w {
                            let px: &[f32] = unsafe { std::slice::from_raw_parts(srcrow.as_ptr().add(x * 16) as *const f32, 4) };
                            let e = rgbp(px);
                            let yy = luma(e);
                            let code = (876.0 * yy + 64.0).round().clamp(64.0, 940.0) as u16;
                            yrow[x] = code << 6;
                        }
                    }
                    for cx in 0..w / 2 {
                        let (mut cb, mut cr) = (0.0f32, 0.0f32);
                        for dy in 0..2 {
                            let srow = h - 1 - (y0 + dy);
                            for dx in 0..2 {
                                let px: &[f32] = unsafe { std::slice::from_raw_parts(src.as_ptr().add(srow * bpr + (2 * cx + dx) * 16) as *const f32, 4) };
                                let e = rgbp(px);
                                let yy = luma(e);
                                cb += (e.2 - yy) / 1.8814;
                                cr += (e.0 - yy) / 1.4746;
                            }
                        }
                        cb *= 0.25;
                        cr *= 0.25;
                        let cbc = (896.0 * cb + 512.0).round().clamp(64.0, 960.0) as u16;
                        let crc = (896.0 * cr + 512.0).round().clamp(64.0, 960.0) as u16;
                        crow[2 * cx] = cbc << 6;
                        crow[2 * cx + 1] = crc << 6;
                    }
                }
            });
        }
    });
}

struct CpuConverter {
    w: u32,
    h: u32,
    staging: ID3D11Texture2D,
    outputs: Vec<ID3D11Texture2D>,
    next: usize,
}

impl CpuConverter {
    fn new(gpu: &Gpu, w: u32, h: u32) -> Result<Self> {
        let staging = make_p010(gpu, w, h, D3D11_USAGE_STAGING, 0, D3D11_CPU_ACCESS_WRITE.0 as u32)?;
        let mut outputs = Vec::new();
        for _ in 0..4 {
            outputs.push(make_p010(gpu, w, h, D3D11_USAGE_DEFAULT, 0, 0)?);
        }
        Ok(Self { w, h, staging, outputs, next: 0 })
    }

    fn convert(&mut self, gpu: &Gpu, pixels: *const u8, bpr: usize) -> Result<ID3D11Texture2D> {
        let ctx = &gpu.context;
        let (w, h) = (self.w as usize, self.h as usize);
        let mut mapped = D3D11_MAPPED_SUBRESOURCE::default();
        unsafe { ctx.Map(&self.staging, 0, D3D11_MAP_WRITE, 0, Some(&mut mapped))? };
        let pitch_words = mapped.RowPitch as usize / 2;
        let base = mapped.pData as *mut u16;
        // P010 mapped: h rows of luma, then h/2 rows of interleaved Cb/Cr.
        let y_plane = unsafe { std::slice::from_raw_parts_mut(base, pitch_words * h) };
        let c_plane = unsafe { std::slice::from_raw_parts_mut(base.add(pitch_words * h), pitch_words * (h / 2)) };
        convert_cpu(pixels, bpr, w, h, y_plane, pitch_words, c_plane, pitch_words);
        unsafe { ctx.Unmap(&self.staging, 0) };
        let tex = self.outputs[self.next].clone();
        self.next = (self.next + 1) % self.outputs.len();
        unsafe {
            ctx.CopyResource(&tex, &self.staging);
            ctx.Flush();
        }
        Ok(tex)
    }
}

enum Converter {
    Gpu(GpuConverter),
    Cpu(CpuConverter),
}

impl Converter {
    fn new(gpu: &Gpu, w: u32, h: u32, force_cpu: bool) -> Result<Self> {
        if !force_cpu && p010_render_targets_supported(gpu) {
            match GpuConverter::new(gpu, w, h) {
                Ok(c) => {
                    log(format!("converter: GPU (P010 render targets) {w}x{h}"));
                    return Ok(Converter::Gpu(c));
                }
                Err(e) => log(format!("GPU converter unavailable ({e}); CPU fallback")),
            }
        } else if !force_cpu {
            log("P010 render targets unsupported; CPU fallback");
        }
        log(format!("converter: CPU {w}x{h}"));
        Ok(Converter::Cpu(CpuConverter::new(gpu, w, h)?))
    }
    fn convert(&mut self, gpu: &Gpu, pixels: *const u8, bpr: usize) -> Result<ID3D11Texture2D> {
        match self {
            Converter::Gpu(c) => c.convert(gpu, pixels, bpr),
            Converter::Cpu(c) => c.convert(gpu, pixels, bpr),
        }
    }
}

// ------------------------------------------------------------ selftest ---

fn selftest(path: &str, force_cpu: bool) -> ! {
    let text = std::fs::read_to_string(path).unwrap_or_else(|e| {
        log(format!("selftest: cannot read {path}: {e}"));
        std::process::exit(2)
    });
    // A tiny JSON reader is not worth a dependency: the fixture is numbers
    // in nested arrays with known keys, parsed with a hand-rolled scanner.
    let input = json_array3(&text, "\"input\"");
    let y_exp = json_array2(&text, "\"y\"");
    let cb_exp = json_array2(&text, "\"cb\"");
    let cr_exp = json_array2(&text, "\"cr\"");
    let h = input.len();
    let w = input[0].len();
    let bpr = (w * 16 + 255) / 256 * 256;
    let mut pixels = vec![0u8; bpr * h];
    for (j, row) in input.iter().enumerate() {
        let ring_row = h - 1 - j; // fixture row 0 = top; ring row 0 = bottom
        for (i, px) in row.iter().enumerate() {
            let o = ring_row * bpr + i * 16;
            for (k, v) in [px[0], px[1], px[2], 1.0].iter().enumerate() {
                pixels[o + k * 4..o + k * 4 + 4].copy_from_slice(&(*v as f32).to_le_bytes());
            }
        }
    }
    let gpu = make_gpu().unwrap_or_else(|e| {
        log(format!("selftest: D3D11 device: {e}"));
        std::process::exit(3)
    });
    let mut conv = Converter::new(&gpu, w as u32, h as u32, force_cpu).unwrap_or_else(|e| {
        log(format!("selftest: converter: {e}"));
        std::process::exit(3)
    });
    let tex = conv.convert(&gpu, pixels.as_ptr(), bpr).unwrap_or_else(|e| {
        log(format!("selftest: convert: {e}"));
        std::process::exit(3)
    });
    let read = make_p010(&gpu, w as u32, h as u32, D3D11_USAGE_STAGING, 0, D3D11_CPU_ACCESS_READ.0 as u32).unwrap();
    let mut mapped = D3D11_MAPPED_SUBRESOURCE::default();
    unsafe {
        gpu.context.CopyResource(&read, &tex);
        gpu.context.Map(&read, 0, D3D11_MAP_READ, 0, Some(&mut mapped)).unwrap();
    }
    let pitch_words = mapped.RowPitch as usize / 2;
    let base = mapped.pData as *const u16;
    let (mut mismatches, mut max_err) = (0usize, 0i32);
    let mut check = |off_rows: usize, expect: &Vec<Vec<i64>>, stride: usize, comp: usize| {
        for (j, row) in expect.iter().enumerate() {
            for (i, code) in row.iter().enumerate() {
                let word = unsafe { std::ptr::read(base.add((off_rows + j) * pitch_words + i * stride + comp)) };
                let got = (word >> 6) as i64;
                let err = (got - code).unsigned_abs() as i32;
                if err > 1 {
                    mismatches += 1; // GPU pow() vs CPython pow: one code of slack
                }
                max_err = max_err.max(err);
            }
        }
    };
    check(0, &y_exp, 1, 0);
    check(h, &cb_exp, 2, 0);
    check(h, &cr_exp, 2, 1);
    unsafe { gpu.context.Unmap(&read, 0) };
    log(format!("selftest {w}x{h}: mismatches={mismatches} max_err={max_err}"));
    std::process::exit(if mismatches == 0 { 0 } else { 1 })
}

/// Numbers of the first JSON array following `key`, flattened into rows:
/// `[[a,b],[c,d]]` -> two rows. Enough for the fixture; not a JSON parser.
fn json_array2(text: &str, key: &str) -> Vec<Vec<i64>> {
    let start = text.find(key).map(|i| i + key.len()).expect("fixture key");
    let mut rows = Vec::new();
    let mut row: Vec<i64> = Vec::new();
    let mut num = String::new();
    let mut depth = 0;
    for ch in text[start..].chars() {
        match ch {
            '[' => depth += 1,
            ']' => {
                if !num.is_empty() {
                    row.push(num.parse::<f64>().unwrap() as i64);
                    num.clear();
                }
                depth -= 1;
                if depth == 1 {
                    rows.push(std::mem::take(&mut row));
                } else if depth == 0 {
                    break;
                }
            }
            ',' => {
                if !num.is_empty() {
                    row.push(num.parse::<f64>().unwrap() as i64);
                    num.clear();
                }
            }
            c if c.is_ascii_digit() || c == '-' || c == '.' || c == 'e' || c == 'E' || c == '+' => num.push(c),
            _ => {}
        }
    }
    rows
}

fn json_array3(text: &str, key: &str) -> Vec<Vec<Vec<f64>>> {
    let start = text.find(key).map(|i| i + key.len()).expect("fixture key");
    let mut rows = Vec::new();
    let mut row: Vec<Vec<f64>> = Vec::new();
    let mut px: Vec<f64> = Vec::new();
    let mut num = String::new();
    let mut depth = 0;
    for ch in text[start..].chars() {
        match ch {
            '[' => depth += 1,
            ']' => {
                if !num.is_empty() {
                    px.push(num.parse().unwrap());
                    num.clear();
                }
                depth -= 1;
                if depth == 2 {
                    row.push(std::mem::take(&mut px));
                } else if depth == 1 {
                    rows.push(std::mem::take(&mut row));
                } else if depth == 0 {
                    break;
                }
            }
            ',' => {
                if !num.is_empty() {
                    px.push(num.parse().unwrap());
                    num.clear();
                }
            }
            c if c.is_ascii_digit() || c == '-' || c == '.' || c == 'e' || c == 'E' || c == '+' => num.push(c),
            _ => {}
        }
    }
    rows
}

// ------------------------------------------------------------ encoder ----

struct Encoder {
    mft: IMFTransform,
    events: IMFMediaEventGenerator,
    codec: Option<ICodecAPI>,
    in_id: u32,
    out_id: u32,
}

fn make_encoder(gpu: &Gpu, w: u32, h: u32, opts: &Opts) -> Result<(Encoder, IMFDXGIDeviceManager)> {
    let declared_fps = opts.fps;
    let declared_mbps = opts.mbps;
    // The vendor's hardware HEVC encoder, whichever vendor this is.
    let out_info = MFT_REGISTER_TYPE_INFO { guidMajorType: MFMediaType_Video, guidSubtype: MFVideoFormat_HEVC };
    let mut activates: *mut Option<IMFActivate> = std::ptr::null_mut();
    let mut count = 0u32;
    unsafe {
        MFTEnumEx(
            MFT_CATEGORY_VIDEO_ENCODER,
            MFT_ENUM_FLAG_HARDWARE | MFT_ENUM_FLAG_SORTANDFILTER,
            None,
            Some(&out_info),
            &mut activates,
            &mut count,
        )?;
    }
    if count == 0 || activates.is_null() {
        log("no hardware HEVC encoder registered with Media Foundation");
        std::process::exit(3);
    }
    let list = unsafe { std::slice::from_raw_parts(activates, count as usize) };
    let activate = list[0].clone().expect("activate");
    unsafe {
        let mut name = windows::core::PWSTR::null();
        let mut len = 0u32;
        if activate.GetAllocatedString(&MFT_FRIENDLY_NAME_Attribute, &mut name, &mut len).is_ok() && !name.is_null() {
            log(format!("encoder: {}", name.to_string().unwrap_or_default()));
            windows::Win32::System::Com::CoTaskMemFree(Some(name.as_ptr() as *const _));
        }
    }
    let mft: IMFTransform = unsafe { activate.ActivateObject()? };
    unsafe { windows::Win32::System::Com::CoTaskMemFree(Some(activates as *const _)) };

    // Hardware MFTs are asynchronous: unlock, hand over the D3D device, then
    // drive it by its NeedInput / HaveOutput events.
    let attrs = unsafe { mft.GetAttributes()? };
    unsafe {
        attrs.SetUINT32(&MF_TRANSFORM_ASYNC_UNLOCK, 1)?;
        if let Err(e) = attrs.SetUINT32(&MF_LOW_LATENCY, 1) {
            log(format!("MF_LOW_LATENCY rejected: {e}"));
        }
    }
    let mut token = 0u32;
    let mut manager = None;
    unsafe { MFCreateDXGIDeviceManager(&mut token, &mut manager)? };
    let manager = manager.unwrap();
    unsafe { manager.ResetDevice(&gpu.device, token)? };
    unsafe { mft.ProcessMessage(MFT_MESSAGE_SET_D3D_MANAGER, manager.as_raw() as usize)? };

    let (mut in_id, mut out_id) = (0u32, 0u32);
    unsafe {
        let mut in_ids = [0u32; 1];
        let mut out_ids = [0u32; 1];
        if mft.GetStreamIDs(&mut in_ids, &mut out_ids).is_ok() {
            in_id = in_ids[0];
            out_id = out_ids[0];
        }
    }

    // The Mac's session settings, in Media Foundation's words — set before
    // the types, which is when a hardware MFT reads them.
    let codec: Option<ICodecAPI> = mft.cast().ok();
    if let Some(c) = &codec {
        let set = |guid: &windows::core::GUID, v: VARIANT, name: &str| {
            if let Err(e) = unsafe { c.SetValue(guid, &v) } {
                log(format!("property {name} rejected: {e}"));
            }
        };
        set(&CODECAPI_AVLowLatencyMode, variant_bool(true), "LowLatencyMode");
        set(&CODECAPI_AVEncCommonRateControlMode, variant_u32(eAVEncCommonRateControlMode_CBR.0 as u32), "RateControlMode");
        set(&CODECAPI_AVEncCommonMeanBitRate, variant_u32(declared_mbps.saturating_mul(1_000_000)), "MeanBitRate");
        set(&CODECAPI_AVEncMPVDefaultBPictureCount, variant_u32(0), "BPictureCount");
        set(&CODECAPI_AVEncMPVGOPSize, variant_u32(opts.gop), "GOPSize");
        set(&CODECAPI_AVEncCommonQualityVsSpeed, variant_u32(0), "QualityVsSpeed");
    } else {
        log("encoder exposes no ICodecAPI; defaults in force");
    }
    // The container is BT.2020 / PQ: what the addon tells QCView in the
    // hello's stream.format, and what `hevc_metadata` in the mux pins. The
    // same tags on both types; a vendor MFT may still omit the VUI, which
    // is why the mux carries the bitstream filter.
    let tag = |t: &IMFMediaType| -> Result<()> {
        unsafe {
            t.SetUINT32(&MF_MT_VIDEO_PRIMARIES, MFVideoPrimaries_BT2020.0 as u32)?;
            t.SetUINT32(&MF_MT_TRANSFER_FUNCTION, MFVideoTransFunc_2084.0 as u32)?;
            t.SetUINT32(&MF_MT_YUV_MATRIX, MFVideoTransferMatrix_BT2020_10.0 as u32)?;
            t.SetUINT32(&MF_MT_VIDEO_NOMINAL_RANGE, MFNominalRange_16_235.0 as u32)?;
        }
        Ok(())
    };
    // Output type first, as the encoder wants it.
    let out_type = unsafe { MFCreateMediaType()? };
    unsafe {
        out_type.SetGUID(&MF_MT_MAJOR_TYPE, &MFMediaType_Video)?;
        out_type.SetGUID(&MF_MT_SUBTYPE, &MFVideoFormat_HEVC)?;
        out_type.SetUINT64(&MF_MT_FRAME_SIZE, ((w as u64) << 32) | h as u64)?;
        out_type.SetUINT64(&MF_MT_FRAME_RATE, ((declared_fps as u64) << 32) | 1)?;
        out_type.SetUINT64(&MF_MT_PIXEL_ASPECT_RATIO, (1u64 << 32) | 1)?;
        out_type.SetUINT32(&MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive.0 as u32)?;
        out_type.SetUINT32(&MF_MT_AVG_BITRATE, declared_mbps.saturating_mul(1_000_000))?;
        out_type.SetUINT32(&MF_MT_MPEG2_PROFILE, eAVEncH265VProfile_Main_420_10.0 as u32)?;
        tag(&out_type)?;
        mft.SetOutputType(out_id, &out_type, 0)?;
    }
    let in_type = unsafe { MFCreateMediaType()? };
    unsafe {
        in_type.SetGUID(&MF_MT_MAJOR_TYPE, &MFMediaType_Video)?;
        in_type.SetGUID(&MF_MT_SUBTYPE, &MFVideoFormat_P010)?;
        in_type.SetUINT64(&MF_MT_FRAME_SIZE, ((w as u64) << 32) | h as u64)?;
        in_type.SetUINT64(&MF_MT_FRAME_RATE, ((declared_fps as u64) << 32) | 1)?;
        in_type.SetUINT64(&MF_MT_PIXEL_ASPECT_RATIO, (1u64 << 32) | 1)?;
        in_type.SetUINT32(&MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive.0 as u32)?;
        tag(&in_type)?;
        mft.SetInputType(in_id, &in_type, 0)?;
    }

    let events: IMFMediaEventGenerator = mft.cast()?;
    log(format!("encoder ready {w}x{h} @{} {}M Main10 PQ/BT.2020 gop={}", opts.fps, opts.mbps, opts.gop));
    Ok((Encoder { mft, events, codec, in_id, out_id }, manager))
}

/// Encoder event loop: NeedInput pulls from the queue the ring thread
/// fills; HaveOutput writes an access unit. Runs until the queue closes.
fn encode_loop(enc: Encoder, shared: Arc<Shared>, rx: std::sync::mpsc::Receiver<(Sendable<ID3D11Texture2D>, i64, bool)>, fps: u32) {
    let duration = 10_000_000i64 / fps.max(1) as i64;
    let debug = std::env::var_os("QCB_ENCODE_DEBUG").is_some();
    unsafe {
        let _ = enc.mft.ProcessMessage(MFT_MESSAGE_COMMAND_FLUSH, 0);
        let _ = enc.mft.ProcessMessage(MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0);
        let _ = enc.mft.ProcessMessage(MFT_MESSAGE_NOTIFY_START_OF_STREAM, 0);
    }
    loop {
        let ev = match unsafe { enc.events.GetEvent(MF_EVENT_FLAG_NONE) } {
            Ok(ev) => ev,
            Err(e) => {
                log(format!("encoder event error: {e}"));
                std::process::exit(3);
            }
        };
        let kind = unsafe { ev.GetType().unwrap_or(0) };
        if debug {
            log(format!("event {kind} in_flight={}", shared.in_flight.load(Relaxed)));
        }
        if kind == METransformNeedInput.0 as u32 {
            let Ok((tex, pts, key)) = rx.recv() else { return };
            let tex = tex.into_inner();
            if key {
                if let Some(c) = &enc.codec {
                    let _ = unsafe { c.SetValue(&CODECAPI_AVEncVideoForceKeyFrame, &variant_u32(1)) };
                }
            }
            if let Some(t) = shared.submit_times.lock().unwrap().get_mut(&pts) {
                *t = Instant::now(); // encode time counts from here
            }
            let sample = match make_sample(&tex, pts, duration) {
                Ok(s) => s,
                Err(e) => {
                    log(format!("sample: {e}"));
                    shared.in_flight.fetch_sub(1, Relaxed);
                    continue;
                }
            };
            if let Err(e) = unsafe { enc.mft.ProcessInput(enc.in_id, &sample, 0) } {
                log(format!("ProcessInput: {e}"));
                shared.in_flight.fetch_sub(1, Relaxed);
            }
        } else if kind == METransformHaveOutput.0 as u32 {
            let mut buf = MFT_OUTPUT_DATA_BUFFER { dwStreamID: enc.out_id, pSample: std::mem::ManuallyDrop::new(None), dwStatus: 0, pEvents: std::mem::ManuallyDrop::new(None) };
            let mut status = 0u32;
            let r = unsafe { enc.mft.ProcessOutput(0, std::slice::from_mut(&mut buf), &mut status) };
            let sample = unsafe { std::mem::ManuallyDrop::take(&mut buf.pSample) };
            let _ = unsafe { std::mem::ManuallyDrop::take(&mut buf.pEvents) };
            match r {
                Ok(()) => {
                    if let Some(sample) = sample {
                        shared.in_flight.fetch_sub(1, Relaxed);
                        on_output(&shared, &sample);
                    }
                }
                Err(e) if e.code() == MF_E_TRANSFORM_STREAM_CHANGE => {
                    log("output type changed");
                }
                Err(e) => {
                    log(format!("ProcessOutput: {e}"));
                    shared.in_flight.fetch_sub(1, Relaxed);
                }
            }
        }
    }
}

fn make_sample(tex: &ID3D11Texture2D, pts: i64, duration: i64) -> Result<IMFSample> {
    let buffer = unsafe { MFCreateDXGISurfaceBuffer(&ID3D11Texture2D::IID, tex, 0, false)? };
    let sample = unsafe { MFCreateSample()? };
    unsafe {
        sample.AddBuffer(&buffer)?;
        sample.SetSampleTime(pts)?;
        sample.SetSampleDuration(duration)?;
    }
    Ok(sample)
}

fn on_output(shared: &Shared, sample: &IMFSample) {
    let pts = unsafe { sample.GetSampleTime().unwrap_or(-1) };
    let key_hint = unsafe { sample.GetUINT32(&MFSampleExtension_CleanPoint).unwrap_or(0) } != 0;
    let Ok(buffer) = (unsafe { sample.ConvertToContiguousBuffer() }) else { return };
    let mut ptr = std::ptr::null_mut();
    let mut len = 0u32;
    if unsafe { buffer.Lock(&mut ptr, None, Some(&mut len)) }.is_err() {
        return;
    }
    let data = unsafe { std::slice::from_raw_parts(ptr, len as usize) };
    emit(shared, data, key_hint);
    let _ = unsafe { buffer.Unlock() };
    if let Some(t) = shared.submit_times.lock().unwrap().remove(&pts) {
        let us = t.elapsed().as_micros() as i64;
        shared.stats.encode_us_sum.fetch_add(us, Relaxed);
        shared.stats.encode_us_max.fetch_max(us, Relaxed);
    }
}

// ------------------------------------------------------------ ring loop --

type Frame = (Sendable<ID3D11Texture2D>, i64, bool);

fn ring_loop(opts: &Opts, shared: Arc<Shared>) -> Result<()> {
    let Some(ring) = Ring::open(&opts.shm) else { std::process::exit(5) };
    log(format!("ring {}: {} slots, {} KiB each, producer pid {}", opts.shm, ring.slot_count, ring.pixels_capacity / 1024, ring.producer_pid));
    let gpu = make_gpu()?;
    let mut conv: Option<Converter> = None;
    let mut enc_tx: Option<std::sync::mpsc::SyncSender<Frame>> = None;
    let mut session = (0u32, 0u32);

    let interval = Duration::from_secs_f64(1.0 / opts.fps.max(1) as f64);
    // An idle producer (Cycles converged, host static) yields nothing, and a
    // viewer joining an SRT stream that carries nothing sits in its demuxer
    // probe for ever (found 2026-09-23 with QCView). Re-send the last frame
    // a few times a second when nothing changed — an unchanged P-frame is
    // a few hundred bytes — so the stream never goes silent.
    let idle_floor = Duration::from_secs_f64(1.0 / (opts.fps.max(1) as f64 / 8.0).clamp(2.0, 10.0));
    let t0 = Instant::now();
    let mut last_sent = Instant::now() - interval;
    let mut last_alive = Instant::now();
    let mut last_latest = 0u64;
    let mut last_tex: Option<ID3D11Texture2D> = None;

    loop {
        let latest = ring.latest();
        if latest == u64::MAX || ring.state() == STATE_RETIRED {
            log("ring retired by the producer; exiting for a respawn");
            std::process::exit(7);
        }
        let mut fresh: Option<ID3D11Texture2D> = None;
        if latest != last_latest {
            if last_sent.elapsed() < interval {
                std::thread::sleep(Duration::from_micros(500)); // pace: the newest frame wins when we wake
                continue;
            }
            last_latest = latest;
            let slot = (latest & 0xFF) as usize;
            if slot < ring.slot_count {
                ring.claim(slot);
                let seq_before = ring.seqlock(slot);
                let w = ring.desc_u32(slot, DESC_W);
                let h = ring.desc_u32(slot, DESC_H);
                let bpr = ring.desc_u32(slot, DESC_BPR) as usize;
                let pf = ring.desc_u32(slot, DESC_PF);
                if seq_before & 1 == 1 {
                    ring.release();
                    shared.stats.stale.fetch_add(1, Relaxed); // writer inside
                } else if pf != PF_RGBA32F || w < 16 || h < 16 || w % 2 != 0 || h % 2 != 0 || bpr * h as usize > ring.pixels_capacity {
                    ring.release();
                    log(format!("bad frame desc {w}x{h} bpr={bpr} pf={pf}"));
                } else {
                    shared.stats.frames_in.fetch_add(1, Relaxed);
                    if session != (w, h) {
                        // A new size: a converter and an encoder session for it.
                        // The old encoder thread ends when its channel closes.
                        conv = Some(Converter::new(&gpu, w, h, opts.cpu)?);
                        let (enc, _manager) = make_encoder(&gpu, w, h, opts)?;
                        let (tx, rx) = std::sync::mpsc::sync_channel::<Frame>(4);
                        let enc_shared = shared.clone();
                        let fps = opts.fps;
                        let enc = Sendable(enc);
                        let manager = Sendable(_manager);
                        std::thread::Builder::new()
                            .name("encode".into())
                            .spawn(move || {
                                let _keep = manager;
                                encode_loop(enc.into_inner(), enc_shared, rx, fps)
                            })
                            .expect("encode thread");
                        enc_tx = Some(tx);
                        session = (w, h);
                        shared.force_key.store(true, Relaxed);
                        last_tex = None;
                    }
                    let t = Instant::now();
                    let r = conv.as_mut().unwrap().convert(&gpu, ring.pixels(slot), bpr);
                    shared.stats.convert_us_sum.fetch_add(t.elapsed().as_micros() as i64, Relaxed);
                    let seq_after = ring.seqlock(slot);
                    ring.release();
                    match r {
                        Ok(tex) if seq_after == seq_before => fresh = Some(tex),
                        Ok(_) => {
                            shared.stats.stale.fetch_add(1, Relaxed); // overwritten mid-copy: drop
                        }
                        Err(e) => log(format!("convert: {e}")),
                    }
                }
            }
        } else {
            if last_alive.elapsed() > Duration::from_millis(500) {
                last_alive = Instant::now();
                if !ring.producer_alive() {
                    log("producer gone");
                    std::process::exit(0);
                }
            }
            std::thread::sleep(Duration::from_millis(1));
        }
        let want_key = shared.force_key.swap(false, Relaxed);
        let idle_resend = last_sent.elapsed() >= idle_floor;
        let tex = match fresh.or_else(|| if want_key || idle_resend { last_tex.clone() } else { None }) {
            Some(t) => t,
            None => {
                if want_key {
                    shared.force_key.store(true, Relaxed);
                }
                continue;
            }
        };
        let Some(tx) = enc_tx.as_ref() else { continue };
        if shared.in_flight.load(Relaxed) >= 3 {
            shared.stats.dropped.fetch_add(1, Relaxed); // skip rather than queue
            if want_key {
                shared.force_key.store(true, Relaxed);
            }
            last_tex = Some(tex);
            continue;
        }
        let pts = t0.elapsed().as_nanos() as i64 / 100;
        shared.submit_times.lock().unwrap().insert(pts, Instant::now());
        shared.in_flight.fetch_add(1, Relaxed);
        if tx.send((Sendable(tex.clone()), pts, want_key)).is_err() {
            return Ok(());
        }
        last_tex = Some(tex);
        last_sent = Instant::now();
    }
}

// ------------------------------------------------------------ main -------

fn main() {
    let opts = parse_args();
    unsafe {
        let _ = CoInitializeEx(None, COINIT_MULTITHREADED);
        if let Err(e) = MFStartup(MF_VERSION, MFSTARTUP_FULL) {
            log(format!("MFStartup: {e}"));
            std::process::exit(3);
        }
    }
    if let Some(path) = &opts.selftest {
        selftest(path, opts.cpu);
    }
    let shared = Arc::new(Shared {
        stats: Stats::default(),
        force_key: AtomicBool::new(true), // first frame is a key; the mux asks for more with "key"
        in_flight: AtomicI64::new(0),
        submit_times: Mutex::new(HashMap::new()),
        out: Mutex::new(std::io::stdout()),
        params: Mutex::new([None, None, None]),
    });

    // stdin commands
    {
        let shared = shared.clone();
        std::thread::spawn(move || {
            let stdin = std::io::stdin();
            for line in stdin.lock().lines() {
                match line.as_deref().map(str::trim) {
                    Ok("key") => shared.force_key.store(true, Relaxed),
                    Ok("quit") => std::process::exit(0),
                    Ok(_) => {}
                    Err(_) => break,
                }
            }
            std::process::exit(0); // the addon went away
        });
    }

    // stats, once a second, the Mac's line
    {
        let shared = shared.clone();
        std::thread::spawn(move || {
            let (mut prev_out, mut prev_bytes, mut prev_in) = (0u64, 0u64, 0u64);
            loop {
                std::thread::sleep(Duration::from_secs(1));
                let s = &shared.stats;
                let fo = s.frames_out.load(Relaxed);
                let bo = s.bytes_out.load(Relaxed);
                let fi = s.frames_in.load(Relaxed);
                let sum = s.encode_us_sum.swap(0, Relaxed);
                let mx = s.encode_us_max.swap(0, Relaxed);
                let cs = s.convert_us_sum.swap(0, Relaxed);
                let n = fo - prev_out;
                let ni = fi - prev_in;
                log(format!(
                    "fps={} mbps={:.1} keys={} in={} dropped={} stale={} convert_ms avg={:.1} encode_ms avg={:.1} max={:.1}",
                    n,
                    (bo - prev_bytes) as f64 * 8.0 / 1e6,
                    s.keys_out.load(Relaxed),
                    fi,
                    s.dropped.load(Relaxed),
                    s.stale.load(Relaxed),
                    if ni > 0 { cs as f64 / ni as f64 / 1000.0 } else { 0.0 },
                    if n > 0 { sum as f64 / n as f64 / 1000.0 } else { 0.0 },
                    mx as f64 / 1000.0
                ));
                prev_out = fo;
                prev_bytes = bo;
                prev_in = fi;
            }
        });
    }

    let _ = HANDLE::default();
    if let Err(e) = ring_loop(&opts, shared) {
        log(format!("encode failed: {e}"));
        std::process::exit(6);
    }
}
