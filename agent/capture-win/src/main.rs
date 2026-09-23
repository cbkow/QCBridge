//! qcb-capture-win — native Windows capture + encode for the QCBridge Agent
//! (S7), the twin of `qcb-capture-mac`.
//!
//! DXGI Desktop Duplication hands over the desktop as a BGRA8 texture; the
//! D3D11 video processor crops, scales and converts it to NV12 (or P010 for
//! `--10bit`) on the GPU; the hardware HEVC encoder behind Media Foundation
//! encodes it, on the same D3D11 device, so no frame ever comes down to the
//! CPU. The output is written to stdout as Annex-B with an AUD per access
//! unit and VPS/SPS/PPS in band on every keyframe — exactly what the
//! agent's video source consumes from qcb-capture-mac and from ffmpeg.
//! stdin takes one-line commands: "key" forces a keyframe on the next frame
//! (keyframe-on-join), "quit" exits.
//!
//!   cargo build --release            (in agent/capture-win)
//!   qcb-capture-win [--display main|<index>] [--fps 60] [--bitrate 50]
//!                   [--region X,Y,W,H (pixels)] [--scale 1.0] [--10bit]
//!                   [--gop 600] [--cursor]
//!
//! Settings follow the Mac's: low-latency mode, no B-frames, CBR at the
//! requested rate, BT.709 tags, long GOP with on-demand keys, at most 3
//! frames in flight — skip rather than queue. One system API end to end,
//! the way the Mac is ScreenCaptureKit + VideoToolbox: no vendor SDK.

use std::collections::HashMap;
use std::io::{BufRead, Write};
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering::Relaxed};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use windows::Win32::Foundation::{RECT, TRUE, VARIANT_TRUE};
use windows::Win32::Graphics::Direct3D::D3D_DRIVER_TYPE_UNKNOWN;
use windows::Win32::Graphics::Direct3D11::*;
use windows::Win32::Graphics::Dxgi::Common::*;
use windows::Win32::Graphics::Dxgi::*;
use windows::Win32::Media::MediaFoundation::*;
use windows::Win32::System::Com::{CoInitializeEx, COINIT_MULTITHREADED};
use windows::Win32::System::Variant::{VARENUM, VARIANT, VARIANT_0, VARIANT_0_0, VARIANT_0_0_0, VT_BOOL, VT_UI4};
use windows::core::{Interface, Result};

// ------------------------------------------------------------ options ----

struct Opts {
    display: String,
    fps: u32,
    mbps: u32,
    region: Option<(i32, i32, i32, i32)>,
    scale: f64,
    ten_bit: bool,
    gop: u32,
    cursor: bool,
}

fn parse_args() -> Opts {
    let mut o = Opts { display: "main".into(), fps: 60, mbps: 50, region: None, scale: 1.0, ten_bit: false, gop: 600, cursor: false };
    let mut it = std::env::args().skip(1);
    while let Some(a) = it.next() {
        match a.as_str() {
            "--display" => o.display = it.next().unwrap_or_else(|| "main".into()),
            "--fps" => o.fps = it.next().and_then(|v| v.parse().ok()).unwrap_or(60),
            "--bitrate" => o.mbps = it.next().and_then(|v| v.parse().ok()).unwrap_or(50),
            "--region" => {
                let p: Vec<i32> = it.next().unwrap_or_default().split(',').filter_map(|v| v.trim().parse().ok()).collect();
                if p.len() == 4 {
                    o.region = Some((p[0], p[1], p[2], p[3]));
                }
            }
            "--scale" => o.scale = it.next().and_then(|v| v.parse().ok()).unwrap_or(1.0),
            "--10bit" => o.ten_bit = true,
            "--gop" => o.gop = it.next().and_then(|v| v.parse().ok()).unwrap_or(600),
            "--cursor" => o.cursor = true,
            "--list-displays" => {
                if let Err(e) = list_displays() {
                    log(format!("list: {e}"));
                }
                std::process::exit(0);
            }
            other => {
                eprintln!("unknown arg {other}");
                std::process::exit(2);
            }
        }
    }
    o
}

fn log(s: impl AsRef<str>) {
    eprintln!("[capture] {}", s.as_ref());
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
    // A method, not a pattern: a closure that destructured the wrapper would
    // capture only the field, and the field is the thing that is not Send.
    fn into_inner(self) -> T {
        self.0
    }
}

// ------------------------------------------------------------ shared -----

#[derive(Default)]
struct Stats {
    frames_in: AtomicU64,
    frames_out: AtomicU64,
    bytes_out: AtomicU64,
    keys_out: AtomicU64,
    dropped: AtomicU64, // skipped because 3 frames were in flight (the Mac's meaning)
    paced: AtomicU64,   // replaced by a newer frame before the tick sent it
    encode_us_sum: AtomicI64,
    encode_us_max: AtomicI64,
    queue_us_sum: AtomicI64,
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
        std::process::exit(0); // the agent went away
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
    video_device: ID3D11VideoDevice,
    video_context: ID3D11VideoContext,
}

fn find_output(display: &str) -> Result<(IDXGIAdapter1, IDXGIOutput1, RECT)> {
    let factory: IDXGIFactory1 = unsafe { CreateDXGIFactory1()? };
    let want: Option<u32> = if display == "main" { None } else { display.parse().ok() };
    let mut index = 0u32;
    let mut ai = 0u32;
    while let Ok(adapter) = unsafe { factory.EnumAdapters1(ai) } {
        let mut oi = 0u32;
        while let Ok(output) = unsafe { adapter.EnumOutputs(oi) } {
            let desc = unsafe { output.GetDesc()? };
            let pick = match want {
                None => desc.AttachedToDesktop.as_bool() && desc.DesktopCoordinates.left == 0 && desc.DesktopCoordinates.top == 0,
                Some(w) => w == index,
            };
            if pick {
                return Ok((adapter, output.cast()?, desc.DesktopCoordinates));
            }
            index += 1;
            oi += 1;
        }
        ai += 1;
    }
    // Fall back to the first output of the first adapter.
    let adapter = unsafe { factory.EnumAdapters1(0)? };
    let output = unsafe { adapter.EnumOutputs(0)? };
    let desc = unsafe { output.GetDesc()? };
    Ok((adapter, output.cast()?, desc.DesktopCoordinates))
}

fn list_displays() -> Result<()> {
    let factory: IDXGIFactory1 = unsafe { CreateDXGIFactory1()? };
    let mut index = 0u32;
    let mut ai = 0u32;
    while let Ok(adapter) = unsafe { factory.EnumAdapters1(ai) } {
        let ad = unsafe { adapter.GetDesc1()? };
        let mut oi = 0u32;
        while let Ok(output) = unsafe { adapter.EnumOutputs(oi) } {
            let d = unsafe { output.GetDesc()? };
            let r = d.DesktopCoordinates;
            eprintln!(
                "display {index}: {}x{} at {},{}{} ({})",
                r.right - r.left, r.bottom - r.top, r.left, r.top,
                if r.left == 0 && r.top == 0 { " [main]" } else { "" },
                String::from_utf16_lossy(&ad.Description).trim_end_matches(' ')
            );
            index += 1;
            oi += 1;
        }
        ai += 1;
    }
    Ok(())
}

fn make_gpu(adapter: &IDXGIAdapter1) -> Result<Gpu> {
    let mut device = None;
    let mut context = None;
    unsafe {
        D3D11CreateDevice(
            &adapter.cast::<IDXGIAdapter>()?,
            D3D_DRIVER_TYPE_UNKNOWN,
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
    Ok(Gpu { video_device: device.cast()?, video_context: context.cast()?, device, context })
}

/// The video processor: one blit does crop (source rect), scale (output
/// size) and BGRA -> NV12/P010 with BT.709 studio-range tags, in hardware.
struct Converter {
    vp: ID3D11VideoProcessor,
    vp_enum: ID3D11VideoProcessorEnumerator,
    outputs: Vec<(ID3D11Texture2D, ID3D11VideoProcessorOutputView)>,
    next: usize,
}

impl Converter {
    fn new(gpu: &Gpu, src: RECT, in_w: u32, in_h: u32, out_w: u32, out_h: u32, fps: u32, ten_bit: bool) -> Result<Self> {
        let desc = D3D11_VIDEO_PROCESSOR_CONTENT_DESC {
            InputFrameFormat: D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE,
            InputFrameRate: DXGI_RATIONAL { Numerator: fps, Denominator: 1 },
            InputWidth: in_w,
            InputHeight: in_h,
            OutputFrameRate: DXGI_RATIONAL { Numerator: fps, Denominator: 1 },
            OutputWidth: out_w,
            OutputHeight: out_h,
            Usage: D3D11_VIDEO_USAGE_PLAYBACK_NORMAL,
        };
        let vp_enum = unsafe { gpu.video_device.CreateVideoProcessorEnumerator(&desc)? };
        let vp = unsafe { gpu.video_device.CreateVideoProcessor(&vp_enum, 0)? };
        let fmt = if ten_bit { DXGI_FORMAT_P010 } else { DXGI_FORMAT_NV12 };
        let mut outputs = Vec::new();
        for _ in 0..4 {
            let td = D3D11_TEXTURE2D_DESC {
                Width: out_w,
                Height: out_h,
                MipLevels: 1,
                ArraySize: 1,
                Format: fmt,
                SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
                Usage: D3D11_USAGE_DEFAULT,
                BindFlags: D3D11_BIND_RENDER_TARGET.0 as u32,
                CPUAccessFlags: 0,
                MiscFlags: 0,
            };
            let mut tex = None;
            unsafe { gpu.device.CreateTexture2D(&td, None, Some(&mut tex))? };
            let tex = tex.unwrap();
            let ovd = D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC {
                ViewDimension: D3D11_VPOV_DIMENSION_TEXTURE2D,
                Anonymous: D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC_0 { Texture2D: D3D11_TEX2D_VPOV { MipSlice: 0 } },
            };
            let mut view = None;
            unsafe { gpu.video_device.CreateVideoProcessorOutputView(&tex, &vp_enum, &ovd, Some(&mut view))? };
            outputs.push((tex, view.unwrap()));
        }
        let vc = &gpu.video_context;
        unsafe {
            vc.VideoProcessorSetStreamFrameFormat(&vp, 0, D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE);
            vc.VideoProcessorSetStreamSourceRect(&vp, 0, true, Some(&src));
            let dst = RECT { left: 0, top: 0, right: out_w as i32, bottom: out_h as i32 };
            vc.VideoProcessorSetStreamDestRect(&vp, 0, true, Some(&dst));
            vc.VideoProcessorSetOutputTargetRect(&vp, true, Some(&dst));
            // sRGB desktop in, BT.709 studio-range video out — the tags the
            // Mac sets on its session.
            if let Ok(vc1) = vc.cast::<ID3D11VideoContext1>() {
                vc1.VideoProcessorSetStreamColorSpace1(&vp, 0, DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709);
                vc1.VideoProcessorSetOutputColorSpace1(&vp, DXGI_COLOR_SPACE_YCBCR_STUDIO_G22_LEFT_P709);
            }
        }
        Ok(Self { vp, vp_enum, outputs, next: 0 })
    }

    /// Converts `input` into the next pooled output texture and returns it.
    fn convert(&mut self, gpu: &Gpu, input: &ID3D11Texture2D) -> Result<ID3D11Texture2D> {
        let ivd = D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC {
            FourCC: 0,
            ViewDimension: D3D11_VPIV_DIMENSION_TEXTURE2D,
            Anonymous: D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC_0 { Texture2D: D3D11_TEX2D_VPIV { MipSlice: 0, ArraySlice: 0 } },
        };
        let mut iview = None;
        unsafe { gpu.video_device.CreateVideoProcessorInputView(input, &self.vp_enum, &ivd, Some(&mut iview))? };
        let (tex, oview) = &self.outputs[self.next];
        self.next = (self.next + 1) % self.outputs.len();
        let stream = D3D11_VIDEO_PROCESSOR_STREAM {
            Enable: TRUE,
            OutputIndex: 0,
            InputFrameOrField: 0,
            PastFrames: 0,
            FutureFrames: 0,
            ppPastSurfaces: std::ptr::null_mut(),
            pInputSurface: std::mem::ManuallyDrop::new(iview),
            ppFutureSurfaces: std::ptr::null_mut(),
            ppPastSurfacesRight: std::ptr::null_mut(),
            pInputSurfaceRight: std::mem::ManuallyDrop::new(None),
            ppFutureSurfacesRight: std::ptr::null_mut(),
        };
        let r = unsafe { gpu.video_context.VideoProcessorBlt(&self.vp, oview, 0, &[stream]) };
        r?;
        // The blit sits in the immediate context's command buffer until
        // something flushes it; the encoder cannot start on this texture
        // before then. Without this, encode latency tracked the capture tick.
        unsafe { gpu.context.Flush() };
        Ok(tex.clone())
    }
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
        // The MFT-level low-latency switch, distinct from the codec API one
        // below; hardware encoders read this one at type negotiation.
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
        let profile = if opts.ten_bit { eAVEncH265VProfile_Main_420_10 } else { eAVEncH265VProfile_Main_420_8 };
        out_type.SetUINT32(&MF_MT_MPEG2_PROFILE, profile.0 as u32)?;
        out_type.SetUINT32(&MF_MT_VIDEO_NOMINAL_RANGE, MFNominalRange_16_235.0 as u32)?;
        out_type.SetUINT32(&MF_MT_YUV_MATRIX, MFVideoTransFunc_709.0 as u32)?;
        mft.SetOutputType(out_id, &out_type, 0)?;
    }
    let in_type = unsafe { MFCreateMediaType()? };
    unsafe {
        in_type.SetGUID(&MF_MT_MAJOR_TYPE, &MFMediaType_Video)?;
        in_type.SetGUID(&MF_MT_SUBTYPE, if opts.ten_bit { &MFVideoFormat_P010 } else { &MFVideoFormat_NV12 })?;
        in_type.SetUINT64(&MF_MT_FRAME_SIZE, ((w as u64) << 32) | h as u64)?;
        in_type.SetUINT64(&MF_MT_FRAME_RATE, ((declared_fps as u64) << 32) | 1)?;
        in_type.SetUINT64(&MF_MT_PIXEL_ASPECT_RATIO, (1u64 << 32) | 1)?;
        in_type.SetUINT32(&MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive.0 as u32)?;
        in_type.SetUINT32(&MF_MT_VIDEO_NOMINAL_RANGE, MFNominalRange_16_235.0 as u32)?;
        mft.SetInputType(in_id, &in_type, 0)?;
    }

    let events: IMFMediaEventGenerator = mft.cast()?;
    log(format!("encoder ready {w}x{h} @{} {}M {} gop={}", opts.fps, opts.mbps, if opts.ten_bit { "Main10" } else { "Main" }, opts.gop));
    Ok((Encoder { mft, events, codec, in_id, out_id }, manager))
}

/// Encoder event loop: NeedInput pulls from the queue the capture thread
/// fills; HaveOutput writes an access unit. Runs until the queue closes.
fn encode_loop(enc: Encoder, shared: Arc<Shared>, rx: std::sync::mpsc::Receiver<(Sendable<ID3D11Texture2D>, i64, bool)>, fps: u32) {
    let duration = 10_000_000i64 / fps.max(1) as i64;
    let debug = std::env::var_os("QCB_CAPTURE_DEBUG").is_some();
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
                shared.stats.queue_us_sum.fetch_add(t.elapsed().as_micros() as i64, Relaxed);
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
                    // The encoder re-reports its output type once; keep going.
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

// ------------------------------------------------------------ capture ----

fn capture_loop(opts: &Opts, shared: Arc<Shared>, tx: std::sync::mpsc::SyncSender<(Sendable<ID3D11Texture2D>, i64, bool)>) -> Result<()> {
    let (adapter, output, coords) = find_output(&opts.display)?;
    let gpu = make_gpu(&adapter)?;
    let dup = unsafe { output.DuplicateOutput(&gpu.device)? };
    // The duplicated surface's own size is the truth in pixels, whatever
    // the desktop coordinates say.
    let mode = unsafe { dup.GetDesc() }.ModeDesc;
    let (full_w, full_h) = if mode.Width > 0 && mode.Height > 0 {
        (mode.Width, mode.Height)
    } else {
        ((coords.right - coords.left) as u32, (coords.bottom - coords.top) as u32)
    };
    let (rx, ry, rw, rh) = opts.region.unwrap_or((0, 0, full_w as i32, full_h as i32));
    let src = RECT { left: rx.max(0), top: ry.max(0), right: (rx + rw).min(full_w as i32), bottom: (ry + rh).min(full_h as i32) };
    let out_w = (((src.right - src.left) as f64 * opts.scale) as u32) & !1;
    let out_h = (((src.bottom - src.top) as f64 * opts.scale) as u32) & !1;
    if out_w < 16 || out_h < 16 {
        log(format!("region/scale yields {out_w}x{out_h}; nothing to capture"));
        std::process::exit(5);
    }
    let mut conv = Converter::new(&gpu, src, full_w, full_h, out_w, out_h, opts.fps, opts.ten_bit)?;
    let (enc, _manager) = make_encoder(&gpu, out_w, out_h, opts)?;
    log(format!(
        "capturing display rect {},{} {}x{} px -> {}x{} px{}",
        src.left, src.top, src.right - src.left, src.bottom - src.top, out_w, out_h,
        if opts.cursor { " (cursor not composited by DDA; ignored)" } else { "" }
    ));
    let enc_shared = shared.clone();
    let fps = opts.fps;
    let enc = Sendable(enc);
    std::thread::Builder::new().name("encode".into()).spawn(move || encode_loop(enc.into_inner(), enc_shared, rx_take(), fps)).expect("encode thread");

    // DDA wakes on desktop change; the frame interval paces the encoder and,
    // on a key request with nothing new on screen, re-sends the last frame
    // so a joining viewer is not left waiting for the desktop to move.
    let interval = Duration::from_secs_f64(1.0 / opts.fps.max(1) as f64);
    // An idle desktop yields no frames at all from DDA, and a viewer joining
    // an SRT stream that carries nothing sits in its demuxer probe for ever
    // (found 2026-09-23 with QCView). Keep a floor: re-send the last frame
    // a few times a second when nothing changed — an unchanged P-frame is
    // a few hundred bytes — so the stream never goes silent.
    let idle_floor = Duration::from_secs_f64(1.0 / (opts.fps.max(1) as f64 / 8.0).clamp(2.0, 10.0));
    let t0 = Instant::now();
    let mut last_sent = Instant::now() - interval;
    let mut last_tex: Option<ID3D11Texture2D> = None;   // last frame sent (re-sent on a key request)
    let mut pending: Option<ID3D11Texture2D> = None;    // newest frame not yet sent
    let mut first = true;
    loop {
        let mut info = DXGI_OUTDUPL_FRAME_INFO::default();
        let mut resource: Option<IDXGIResource> = None;
        // Never block inside AcquireNextFrame: while it waits it holds the
        // device's multithread lock, and the encoder, on the same device,
        // waits with it — measured as encode latency tracking the capture
        // tick (24 ms at 60 fps, 38 at 30). Poll with a zero timeout and
        // sleep ourselves instead.
        let acquired = unsafe { dup.AcquireNextFrame(0, &mut info, &mut resource) };
        if acquired.as_ref().is_err_and(|e| e.code() == DXGI_ERROR_WAIT_TIMEOUT) {
            std::thread::sleep(Duration::from_millis(1));
        }
        let mut fresh: Option<ID3D11Texture2D> = None;
        match acquired {
            Ok(()) => {
                if let Some(res) = resource {
                    if info.LastPresentTime != 0 {
                        let tex: ID3D11Texture2D = res.cast()?;
                        shared.stats.frames_in.fetch_add(1, Relaxed);
                        match conv.convert(&gpu, &tex) {
                            Ok(t) => fresh = Some(t),
                            Err(e) => log(format!("convert: {e}")),
                        }
                    }
                }
                let _ = unsafe { dup.ReleaseFrame() };
            }
            Err(e) if e.code() == DXGI_ERROR_WAIT_TIMEOUT => {}
            Err(e) if e.code() == DXGI_ERROR_ACCESS_LOST => {
                log("desktop duplication access lost (display change or secure desktop); exiting for a respawn");
                std::process::exit(4);
            }
            Err(e) => {
                log(format!("AcquireNextFrame: {e}"));
                std::process::exit(4);
            }
        }
        if first && fresh.is_some() {
            log("first frame");
            first = false;
        }
        if fresh.is_some() {
            if pending.is_some() {
                shared.stats.paced.fetch_add(1, Relaxed); // newest wins; the parked one never went out
            }
            pending = fresh;
        }
        if last_sent.elapsed() < interval {
            continue;
        }
        let want_key = shared.force_key.swap(false, Relaxed);
        let idle_resend = last_sent.elapsed() >= idle_floor;
        let tex = match pending.take().or_else(|| if want_key || idle_resend { last_tex.clone() } else { None }) {
            Some(t) => t,
            None => continue,
        };
        if shared.in_flight.load(Relaxed) >= 3 {
            shared.stats.dropped.fetch_add(1, Relaxed); // skip rather than queue
            if want_key {
                shared.force_key.store(true, Relaxed);
            }
            pending = Some(tex);
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

// The encoder thread receives from a channel created in main; this shim
// keeps capture_loop's signature simple.
static RX_SLOT: Mutex<Option<std::sync::mpsc::Receiver<(Sendable<ID3D11Texture2D>, i64, bool)>>> = Mutex::new(None);
fn rx_take() -> std::sync::mpsc::Receiver<(Sendable<ID3D11Texture2D>, i64, bool)> {
    RX_SLOT.lock().unwrap().take().expect("receiver")
}

// ------------------------------------------------------------ main -------

fn main() {
    // Without this, DXGI reports desktop coordinates in DIPs (3072x1728 for a
    // 3840x2160 display at 125 %) and a region would crop the wrong pixels.
    unsafe {
        let _ = windows::Win32::UI::HiDpi::SetProcessDpiAwarenessContext(windows::Win32::UI::HiDpi::DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    }
    let opts = parse_args();
    unsafe {
        let _ = CoInitializeEx(None, COINIT_MULTITHREADED);
        if let Err(e) = MFStartup(MF_VERSION, MFSTARTUP_FULL) {
            log(format!("MFStartup: {e}"));
            std::process::exit(3);
        }
    }
    let shared = Arc::new(Shared {
        stats: Stats::default(),
        force_key: AtomicBool::new(true), // first frame is a key; the agent asks for more with "key"
        in_flight: AtomicI64::new(0),
        submit_times: Mutex::new(HashMap::new()),
        out: Mutex::new(std::io::stdout()),
        params: Mutex::new([None, None, None]),
    });
    let (tx, rx) = std::sync::mpsc::sync_channel::<(Sendable<ID3D11Texture2D>, i64, bool)>(4);
    *RX_SLOT.lock().unwrap() = Some(rx);

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
            std::process::exit(0); // agent went away
        });
    }

    // stats, once a second, the Mac's line
    {
        let shared = shared.clone();
        std::thread::spawn(move || {
            let (mut prev_out, mut prev_bytes) = (0u64, 0u64);
            loop {
                std::thread::sleep(Duration::from_secs(1));
                let s = &shared.stats;
                let fo = s.frames_out.load(Relaxed);
                let bo = s.bytes_out.load(Relaxed);
                let sum = s.encode_us_sum.swap(0, Relaxed);
                let mx = s.encode_us_max.swap(0, Relaxed);
                let qs = s.queue_us_sum.swap(0, Relaxed);
                let n = fo - prev_out;
                log(format!(
                    "fps={} mbps={:.1} keys={} in={} dropped={} paced={} encode_ms avg={:.1} max={:.1} queue_ms avg={:.1}",
                    n,
                    (bo - prev_bytes) as f64 * 8.0 / 1e6,
                    s.keys_out.load(Relaxed),
                    s.frames_in.load(Relaxed),
                    s.dropped.load(Relaxed),
                    s.paced.load(Relaxed),
                    if n > 0 { sum as f64 / n as f64 / 1000.0 } else { 0.0 },
                    mx as f64 / 1000.0,
                    if n > 0 { qs as f64 / n as f64 / 1000.0 } else { 0.0 }
                ));
                prev_out = fo;
                prev_bytes = bo;
            }
        });
    }

    if let Err(e) = capture_loop(&opts, shared, tx) {
        log(format!("capture failed: {e}"));
        std::process::exit(6);
    }
}
