// qcb-encode-mac — the replica's encoder on macOS (QCBridge 0.3).
//
// Blender's addon publishes scene-linear RGBA float frames into a
// shared-memory ring (qcbridge/ring1/ring_layout.py: a header page, three
// slots of descriptor page + page-aligned pixels, a seqlock per slot and
// `latest = (seq << 8) | slot`). This helper maps the ring, converts each new
// frame on the GPU — linear Rec.709 → BT.2020 → PQ with scene 1.0 = 100 nit
// → BT.2020 Y'CbCr 10-bit limited 4:2:0 (qcbridge/ring1/wire_format.py is
// the reference; `--selftest` checks the kernels against its vectors) —
// straight into an IOSurface-backed x420 pixel buffer, and hands that to a
// VideoToolbox HEVC Main10 session. The output is Annex-B on stdout with an
// AUD per access unit and VPS/SPS/PPS in band on every keyframe, which the
// addon pipes into `ffmpeg -c copy` for SRT. stdin takes one-line commands:
// "key" forces a keyframe on the next frame (keyframe-on-join), "quit"
// exits. No screen capture anywhere (DESIGN-NOTES pixels).
//
//   swiftc -O qcb-encode-mac.swift -o qcb-encode-mac
//   qcb-encode-mac --shm qcb-1234-1 [--fps 30] [--bitrate 50] [--gop 30]
//   qcb-encode-mac --selftest tests/fixtures/pq_vectors.json
//
// Exit codes: 0 producer gone / quit, 2 bad args, 3 encoder, 5 ring
// unreadable, 7 ring retired (the producer re-created it: respawn).

import CoreMedia
import CoreVideo
import Darwin
import Foundation
import Metal
import VideoToolbox

struct Opts {
    var shm = ""
    var fps = 30
    var mbps = 50
    var gop = 30
    var selftest: String? = nil
    // Working space → linear BT.2020, nine row-major floats (wire_format
    // rows). The add-on computes it from the replica's OCIO config
    // (colour_state.working_to_2020); the default is Blender's own working
    // space, Linear Rec.709 (wire_format.M_709_TO_2020).
    var matrix: [Float] = [0.6274, 0.3293, 0.0433, 0.0691, 0.9195, 0.0114, 0.0164, 0.0880, 0.8956]
}

func parseMatrix(_ text: String) -> [Float]? {
    let vals = text.split(separator: ",").compactMap { Float($0.trimmingCharacters(in: .whitespaces)) }
    return vals.count == 9 ? vals : nil
}

func parseArgs() -> Opts {
    var o = Opts()
    var it = CommandLine.arguments.dropFirst().makeIterator()
    while let a = it.next() {
        switch a {
        case "--shm": o.shm = it.next() ?? ""
        case "--fps": o.fps = Int(it.next() ?? "30") ?? 30
        case "--bitrate": o.mbps = Int(it.next() ?? "50") ?? 50
        case "--gop": o.gop = Int(it.next() ?? "30") ?? 30
        case "--selftest": o.selftest = it.next()
        case "--matrix":
            guard let m = parseMatrix(it.next() ?? "") else {
                FileHandle.standardError.write("--matrix needs nine comma-separated floats\n".data(using: .utf8)!)
                exit(2)
            }
            o.matrix = m
        default:
            FileHandle.standardError.write("unknown arg \(a)\n".data(using: .utf8)!)
            exit(2)
        }
    }
    if o.shm.isEmpty && o.selftest == nil {
        FileHandle.standardError.write("usage: qcb-encode-mac --shm NAME [--fps N] [--bitrate M] [--gop G] [--matrix r0c0,...,r2c2] | --selftest vectors.json\n".data(using: .utf8)!)
        exit(2)
    }
    return o
}

func log(_ s: String) {
    FileHandle.standardError.write(("[encode] " + s + "\n").data(using: .utf8)!)
}

let opts = parseArgs()

// ------------------------------------------------------------ ring layout --
// Mirrors qcbridge/ring1/ring_layout.py; the Python side is the source of truth.

let RING_MAGIC: UInt32 = 0x46424351  // "QCBF" little-endian
let RING_VERSION: UInt32 = 1
let PAGE = 4096
// "<4sIIIQQQQQQId": magic 0, version 4, slot_count 8, producer_pid 12,
// slot_stride 16, pixels_offset 24, pixels_capacity 32, total_size 40,
// latest 48, reader_claim 56, state 64, t_created 68.
let HDR_LATEST = 48
let HDR_READER_CLAIM = 56
let HDR_STATE = 64
let STATE_RETIRED: UInt32 = 1
let DESC_SEQLOCK = 0
// u64 seqlock | u32 w,h,bpr,pf,flags,frame_number,pad | f64 t_read_start,t_read_end,t_publish | u64 frame_seq | u32 probe_seq | f64 probe_t_host
let DESC_W = 8, DESC_H = 12, DESC_BPR = 16, DESC_PF = 20, DESC_FLAGS = 24, DESC_FRAME = 28
let DESC_T_READ_START = 36, DESC_FRAME_SEQ = 60
let PF_RGBA32F: UInt32 = 4

struct RingHeader {
    var slotCount: Int
    var producerPid: pid_t
    var slotStride: Int
    var pixelsOffset: Int
    var pixelsCapacity: Int
    var totalSize: Int
}

// shm_open is variadic in the SDK header, which Swift refuses to import;
// the mode argument is only read with O_CREAT, so a fixed-arity binding is
// safe for opening an existing object (Apple arm64 passes variadics on the
// stack — never pass O_CREAT through this).
@_silgen_name("shm_open") func c_shm_open(_ name: UnsafePointer<CChar>, _ oflag: Int32) -> Int32

final class Ring {
    let base: UnsafeMutableRawPointer
    let size: Int
    let header: RingHeader
    let name: String

    init?(name: String) {
        self.name = name
        let fd = ("/" + name).withCString { c_shm_open($0, O_RDWR) }
        if fd < 0 { log("shm_open /\(name): \(String(cString: strerror(errno)))"); return nil }
        defer { close(fd) }
        var st = stat()
        if fstat(fd, &st) != 0 || st.st_size < off_t(PAGE) { log("ring too small"); return nil }
        let size = Int(st.st_size)
        guard let p = mmap(nil, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0), p != MAP_FAILED else {
            log("mmap: \(String(cString: strerror(errno)))"); return nil
        }
        base = p
        self.size = size
        let magic = p.loadUnaligned(fromByteOffset: 0, as: UInt32.self)
        let version = p.loadUnaligned(fromByteOffset: 4, as: UInt32.self)
        if magic != RING_MAGIC || version != RING_VERSION {
            log(String(format: "ring magic/version mismatch: %08x v%d", magic, version)); return nil
        }
        header = RingHeader(
            slotCount: Int(p.loadUnaligned(fromByteOffset: 8, as: UInt32.self)),
            producerPid: pid_t(p.loadUnaligned(fromByteOffset: 12, as: UInt32.self)),
            slotStride: Int(p.loadUnaligned(fromByteOffset: 16, as: UInt64.self)),
            pixelsOffset: Int(p.loadUnaligned(fromByteOffset: 24, as: UInt64.self)),
            pixelsCapacity: Int(p.loadUnaligned(fromByteOffset: 32, as: UInt64.self)),
            totalSize: Int(p.loadUnaligned(fromByteOffset: 40, as: UInt64.self)))
        if header.totalSize != size {
            log("ring total_size \(header.totalSize) != mapping \(size)")
        }
    }

    var latest: UInt64 { base.loadUnaligned(fromByteOffset: HDR_LATEST, as: UInt64.self) }
    var state: UInt32 { base.loadUnaligned(fromByteOffset: HDR_STATE, as: UInt32.self) }
    func claim(_ slot: Int) { base.storeBytes(of: UInt64(slot + 1), toByteOffset: HDR_READER_CLAIM, as: UInt64.self) }
    func release() { base.storeBytes(of: UInt64(0), toByteOffset: HDR_READER_CLAIM, as: UInt64.self) }
    func slotOffset(_ slot: Int) -> Int { PAGE + slot * header.slotStride }
    func seqlock(_ slot: Int) -> UInt64 { base.loadUnaligned(fromByteOffset: slotOffset(slot) + DESC_SEQLOCK, as: UInt64.self) }
    func u32(_ slot: Int, _ off: Int) -> UInt32 { base.loadUnaligned(fromByteOffset: slotOffset(slot) + off, as: UInt32.self) }
    func f64(_ slot: Int, _ off: Int) -> Double { base.loadUnaligned(fromByteOffset: slotOffset(slot) + off, as: Double.self) }
    func producerAlive() -> Bool { kill(header.producerPid, 0) == 0 || errno == EPERM }
}

// ------------------------------------------------------------ Metal ------

let shaderSource = """
#include <metal_stdlib>
using namespace metal;

// m0..m2 are the ROWS of the working space -> BT.2020 matrix (wire_format
// rows, as the add-on sends them); MSL's float3x3(c0, c1, c2) takes
// columns, so the matrix is rebuilt transposed below. The P3 selftest
// fixture catches a mix-up (the Rec.709 rows are too near identity to).
struct Params { uint w; uint h; uint bpr; uint off; float4 m0; float4 m1; float4 m2; };

inline float3x3 wire_matrix(constant Params& p) {
    return float3x3(float3(p.m0.x, p.m1.x, p.m2.x),
                    float3(p.m0.y, p.m1.y, p.m2.y),
                    float3(p.m0.z, p.m1.z, p.m2.z));
}

inline float pq(float lin) {
    // scene 1.0 = 100 nit (wire_format.ANCHOR_NITS)
    float L = clamp(lin * (100.0 / 10000.0), 0.0, 1.0);
    float Lm = pow(L, 0.1593017578125);
    return pow((0.8359375 + 18.8515625 * Lm) / (1.0 + 18.6875 * Lm), 78.84375);
}

// PQ-encoded R'G'B' of one source pixel. Rows in the ring are bottom-up
// (Blender's framebuffer order), so row y of the picture is ring row h-1-y.
inline float3 rgbp(device const uchar* ring, constant Params& p, uint x, uint y) {
    device const float4* row = (device const float4*)(ring + p.off + (p.h - 1 - y) * p.bpr);
    float3 c = max(wire_matrix(p) * row[x].rgb, float3(0.0));
    return float3(pq(c.r), pq(c.g), pq(c.b));
}

inline float luma(float3 e) { return 0.2627 * e.r + 0.6780 * e.g + 0.0593 * e.b; }

kernel void luma_plane(device const uchar* ring [[buffer(0)]], constant Params& p [[buffer(1)]],
                       texture2d<float, access::write> y [[texture(0)]],
                       uint2 g [[thread_position_in_grid]]) {
    if (g.x >= p.w || g.y >= p.h) return;
    float Y = luma(rgbp(ring, p, g.x, g.y));
    float code = clamp(round(876.0 * Y + 64.0), 64.0, 940.0);
    y.write(float4(code * 64.0 / 65535.0, 0.0, 0.0, 0.0), g);   // 10-bit code, MSB-aligned
}

kernel void chroma_plane(device const uchar* ring [[buffer(0)]], constant Params& p [[buffer(1)]],
                         texture2d<float, access::write> c [[texture(0)]],
                         uint2 g [[thread_position_in_grid]]) {
    if (g.x >= p.w / 2 || g.y >= p.h / 2) return;
    float cb = 0.0, cr = 0.0;
    for (uint dy = 0; dy < 2; dy++) {
        for (uint dx = 0; dx < 2; dx++) {
            float3 e = rgbp(ring, p, 2 * g.x + dx, 2 * g.y + dy);
            float Y = luma(e);
            cb += (e.b - Y) / 1.8814;
            cr += (e.r - Y) / 1.4746;
        }
    }
    cb *= 0.25; cr *= 0.25;    // 2x2 average: centre siting
    float cbc = clamp(round(896.0 * cb + 512.0), 64.0, 960.0);
    float crc = clamp(round(896.0 * cr + 512.0), 64.0, 960.0);
    c.write(float4(cbc * 64.0 / 65535.0, crc * 64.0 / 65535.0, 0.0, 0.0), g);
}
"""

final class Converter {
    let device: MTLDevice
    let queue: MTLCommandQueue
    let lumaPipe: MTLComputePipelineState
    let chromaPipe: MTLComputePipelineState
    var textureCache: CVMetalTextureCache?
    var pool: CVPixelBufferPool?
    var poolW = 0, poolH = 0
    // Working space -> BT.2020 rows (Opts.matrix); the kernels read it per dispatch.
    var matrix: [Float] = [0.6274, 0.3293, 0.0433, 0.0691, 0.9195, 0.0114, 0.0164, 0.0880, 0.8956]

    init() {
        guard let dev = MTLCreateSystemDefaultDevice(), let q = dev.makeCommandQueue() else {
            log("no Metal device"); exit(3)
        }
        device = dev; queue = q
        do {
            let lib = try dev.makeLibrary(source: shaderSource, options: nil)
            lumaPipe = try dev.makeComputePipelineState(function: lib.makeFunction(name: "luma_plane")!)
            chromaPipe = try dev.makeComputePipelineState(function: lib.makeFunction(name: "chroma_plane")!)
        } catch {
            log("shader compile: \(error)"); exit(3)
        }
        CVMetalTextureCacheCreate(nil, nil, dev, nil, &textureCache)
    }

    func ensurePool(width: Int, height: Int) {
        if pool != nil && poolW == width && poolH == height { return }
        let attrs: [CFString: Any] = [
            kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_420YpCbCr10BiPlanarVideoRange,
            kCVPixelBufferWidthKey: width,
            kCVPixelBufferHeightKey: height,
            kCVPixelBufferIOSurfacePropertiesKey: [:] as [CFString: Any],
            kCVPixelBufferMetalCompatibilityKey: true,
        ]
        var p: CVPixelBufferPool?
        let st = CVPixelBufferPoolCreate(nil, [kCVPixelBufferPoolMinimumBufferCountKey: 6] as CFDictionary, attrs as CFDictionary, &p)
        if st != kCVReturnSuccess || p == nil { log("pixel buffer pool: \(st)"); exit(3) }
        pool = p; poolW = width; poolH = height
    }

    func texture(_ pb: CVPixelBuffer, plane: Int, format: MTLPixelFormat, w: Int, h: Int) -> MTLTexture? {
        guard let cache = textureCache else { return nil }
        var cvtex: CVMetalTexture?
        let st = CVMetalTextureCacheCreateTextureFromImage(nil, cache, pb, nil, format, w, h, plane, &cvtex)
        guard st == kCVReturnSuccess, let cvtex else { log("texture from image: \(st)"); return nil }
        return CVMetalTextureGetTexture(cvtex)
    }

    /// Convert the frame at `offset` inside `buffer` (rows bottom-up, `bpr`
    /// bytes each) into a fresh pool pixel buffer. Synchronous: the caller
    /// re-validates the ring slot after this returns.
    func convert(buffer: MTLBuffer, offset: Int, width: Int, height: Int, bpr: Int) -> CVPixelBuffer? {
        ensurePool(width: width, height: height)
        var pb: CVPixelBuffer?
        guard CVPixelBufferPoolCreatePixelBuffer(nil, pool!, &pb) == kCVReturnSuccess, let pb else {
            log("pool exhausted"); return nil
        }
        guard let ytex = texture(pb, plane: 0, format: .r16Unorm, w: width, h: height),
              let ctex = texture(pb, plane: 1, format: .rg16Unorm, w: width / 2, h: height / 2),
              let cmd = queue.makeCommandBuffer(), let enc = cmd.makeComputeCommandEncoder() else { return nil }
        let m = matrix
        var params = (UInt32(width), UInt32(height), UInt32(bpr), UInt32(offset),
                      m[0], m[1], m[2], Float(0), m[3], m[4], m[5], Float(0), m[6], m[7], m[8], Float(0))
        enc.setComputePipelineState(lumaPipe)
        enc.setBuffer(buffer, offset: 0, index: 0)
        enc.setBytes(&params, length: 64, index: 1)
        enc.setTexture(ytex, index: 0)
        let tg = MTLSize(width: 16, height: 16, depth: 1)
        enc.dispatchThreadgroups(MTLSize(width: (width + 15) / 16, height: (height + 15) / 16, depth: 1), threadsPerThreadgroup: tg)
        enc.setComputePipelineState(chromaPipe)
        enc.setTexture(ctex, index: 0)
        enc.dispatchThreadgroups(MTLSize(width: (width / 2 + 15) / 16, height: (height / 2 + 15) / 16, depth: 1), threadsPerThreadgroup: tg)
        enc.endEncoding()
        cmd.commit()
        cmd.waitUntilCompleted()
        if cmd.status == .error { log("compute: \(String(describing: cmd.error))"); return nil }
        CVBufferSetAttachment(pb, kCVImageBufferColorPrimariesKey, kCVImageBufferColorPrimaries_ITU_R_2020, .shouldPropagate)
        CVBufferSetAttachment(pb, kCVImageBufferTransferFunctionKey, kCVImageBufferTransferFunction_SMPTE_ST_2084_PQ, .shouldPropagate)
        CVBufferSetAttachment(pb, kCVImageBufferYCbCrMatrixKey, kCVImageBufferYCbCrMatrix_ITU_R_2020, .shouldPropagate)
        return pb
    }
}

// ------------------------------------------------------------ selftest ---

func selftest(_ path: String, conv: Converter) -> Never {
    guard let data = FileManager.default.contents(atPath: path),
          let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let input = json["input"] as? [[[Double]]],
          let planes = json["planes"] as? [String: Any],
          let yExp = planes["y"] as? [[Int]], let cbExp = planes["cb"] as? [[Int]], let crExp = planes["cr"] as? [[Int]] else {
        log("selftest: cannot read \(path)"); exit(2)
    }
    // The fixture names the working space it was encoded from; a helper
    // that ignored --matrix would still pass the Rec.709 vectors.
    if let rows = json["matrix"] as? [[Double]], rows.count == 3, rows.allSatisfy({ $0.count == 3 }) {
        conv.matrix = rows.flatMap { $0.map { Float($0) } }
    }
    let ws = (json["working_space"] as? String) ?? "Linear Rec.709"
    let h = input.count, w = input[0].count
    let bpr = ((w * 16) + 255) / 256 * 256
    let bytes = ((bpr * h) + PAGE - 1) / PAGE * PAGE
    guard let buf = conv.device.makeBuffer(length: bytes, options: .storageModeShared) else { exit(3) }
    let p = buf.contents()
    for (j, row) in input.enumerated() {           // fixture row 0 = top; ring row 0 = bottom
        let ringRow = h - 1 - j
        for (i, px) in row.enumerated() {
            let o = ringRow * bpr + i * 16
            p.storeBytes(of: Float(px[0]), toByteOffset: o, as: Float.self)
            p.storeBytes(of: Float(px[1]), toByteOffset: o + 4, as: Float.self)
            p.storeBytes(of: Float(px[2]), toByteOffset: o + 8, as: Float.self)
            p.storeBytes(of: Float(1.0), toByteOffset: o + 12, as: Float.self)
        }
    }
    guard let pb = conv.convert(buffer: buf, offset: 0, width: w, height: h, bpr: bpr) else { log("selftest: convert failed"); exit(3) }
    CVPixelBufferLockBaseAddress(pb, .readOnly)
    defer { CVPixelBufferUnlockBaseAddress(pb, .readOnly) }
    var mismatches = 0, maxErr = 0
    func check(_ plane: Int, _ expect: [[Int]], _ stride: Int, _ comp: Int) {
        let base = CVPixelBufferGetBaseAddressOfPlane(pb, plane)!
        let rowBytes = CVPixelBufferGetBytesPerRowOfPlane(pb, plane)
        for (j, row) in expect.enumerated() {
            for (i, code) in row.enumerated() {
                let word = base.loadUnaligned(fromByteOffset: j * rowBytes + (i * stride + comp) * 2, as: UInt16.self)
                let got = Int(word >> 6)
                let err = abs(got - code)
                if err > 1 { mismatches += 1 }      // GPU pow() vs CPython pow: one code of slack
                maxErr = max(maxErr, err)
            }
        }
    }
    check(0, yExp, 1, 0)
    check(1, cbExp, 2, 0)
    check(1, crExp, 2, 1)
    log("selftest \(w)x\(h) [\(ws)]: mismatches=\(mismatches) max_err=\(maxErr)")
    exit(mismatches == 0 ? 0 : 1)
}

// ------------------------------------------------------------ encoder ----

let out = FileHandle.standardOutput
let outLock = NSLock()
var forceKey = true  // first frame is a key; the mux asks for more with "key"
let keyLock = NSLock()
var inFlight = 0
let inFlightLock = NSLock()
var framesIn = 0, framesOut = 0, bytesOut = 0, keysOut = 0, dropped = 0, skippedStale = 0
var encodeUsSum: Int64 = 0, encodeUsMax: Int64 = 0
let submitTimes = NSMutableDictionary()  // pts value -> submit time (ns)

var session: VTCompressionSession?
var sessionW = 0, sessionH = 0
var sessionStart = Date()   // keyframes at the idle floor for the first seconds of a session (2026-10-28)
let startCode: [UInt8] = [0, 0, 0, 1]
let audNal: [UInt8] = [0, 0, 0, 1, 0x46, 0x01, 0x50]  // NAL type 35, pic_type all

func parameterSets(_ fmt: CMFormatDescription) -> [Data] {
    var sets: [Data] = []
    var count = 0
    var nalLen: Int32 = 0
    guard CMVideoFormatDescriptionGetHEVCParameterSetAtIndex(fmt, parameterSetIndex: 0, parameterSetPointerOut: nil,
        parameterSetSizeOut: nil, parameterSetCountOut: &count, nalUnitHeaderLengthOut: &nalLen) == noErr else { return sets }
    for i in 0..<count {
        var ptr: UnsafePointer<UInt8>? = nil
        var size = 0
        if CMVideoFormatDescriptionGetHEVCParameterSetAtIndex(fmt, parameterSetIndex: i, parameterSetPointerOut: &ptr,
            parameterSetSizeOut: &size, parameterSetCountOut: nil, nalUnitHeaderLengthOut: nil) == noErr, let ptr {
            sets.append(Data(bytes: ptr, count: size))
        }
    }
    return sets
}

func outputCallback(_: UnsafeMutableRawPointer?, _ refcon: UnsafeMutableRawPointer?, status: OSStatus,
                    _: VTEncodeInfoFlags, sbuf: CMSampleBuffer?) {
    inFlightLock.lock(); inFlight = max(0, inFlight - 1); inFlightLock.unlock()
    guard status == noErr, let sbuf, let data = CMSampleBufferGetDataBuffer(sbuf) else {
        if status != noErr { log("encode error \(status)") }
        return
    }
    let pts = CMSampleBufferGetPresentationTimeStamp(sbuf)
    let key: Bool = {
        guard let arr = CMSampleBufferGetSampleAttachmentsArray(sbuf, createIfNecessary: false) as? [[CFString: Any]],
              let first = arr.first else { return true }
        return !((first[kCMSampleAttachmentKey_NotSync] as? Bool) ?? false)
    }()
    var au = Data(audNal)
    if key, let fmt = CMSampleBufferGetFormatDescription(sbuf) {
        for ps in parameterSets(fmt) { au.append(contentsOf: startCode); au.append(ps) }
    }
    var length = 0
    var base: UnsafeMutablePointer<CChar>? = nil
    guard CMBlockBufferGetDataPointer(data, atOffset: 0, lengthAtOffsetOut: nil, totalLengthOut: &length, dataPointerOut: &base) == noErr,
          let base else { return }
    // AVCC -> Annex-B: 4-byte big-endian lengths become start codes.
    var off = 0
    base.withMemoryRebound(to: UInt8.self, capacity: length) { p in
        while off + 4 <= length {
            let n = Int(p[off]) << 24 | Int(p[off + 1]) << 16 | Int(p[off + 2]) << 8 | Int(p[off + 3])
            off += 4
            if n <= 0 || off + n > length { break }
            au.append(contentsOf: startCode)
            au.append(p + off, count: n)
            off += n
        }
    }
    outLock.lock()
    out.write(au)
    framesOut += 1
    bytesOut += au.count
    if key { keysOut += 1 }
    if let t = submitTimes[pts.value] as? Int64 {
        let us = (Int64(DispatchTime.now().uptimeNanoseconds) - t) / 1000
        encodeUsSum += us
        encodeUsMax = max(encodeUsMax, us)
        submitTimes.removeObject(forKey: pts.value)
    }
    outLock.unlock()
}

func setProp(_ key: CFString, _ value: Any, _ name: String) {
    guard let session else { return }
    let s = VTSessionSetProperty(session, key: key, value: value as CFTypeRef)
    if s != noErr { log("property \(name) rejected: \(s)") }
}

func makeSession(width: Int, height: Int) {
    sessionStart = Date()
    if let old = session {
        VTCompressionSessionCompleteFrames(old, untilPresentationTimeStamp: .invalid)
        VTCompressionSessionInvalidate(old)
        session = nil
    }
    let spec: [CFString: Any] = [kVTVideoEncoderSpecification_RequireHardwareAcceleratedVideoEncoder: true]
    let st = VTCompressionSessionCreate(
        allocator: nil, width: Int32(width), height: Int32(height), codecType: kCMVideoCodecType_HEVC,
        encoderSpecification: spec as CFDictionary, imageBufferAttributes: nil, compressedDataAllocator: nil,
        outputCallback: outputCallback, refcon: nil, compressionSessionOut: &session)
    guard st == noErr, session != nil else {
        log("VTCompressionSessionCreate failed: \(st)")
        exit(3)
    }
    setProp(kVTCompressionPropertyKey_RealTime, true, "RealTime")
    setProp(kVTCompressionPropertyKey_AllowFrameReordering, false, "AllowFrameReordering")
    setProp(kVTCompressionPropertyKey_PrioritizeEncodingSpeedOverQuality, true, "PrioritizeSpeed")
    setProp(kVTCompressionPropertyKey_ExpectedFrameRate, opts.fps, "ExpectedFrameRate")
    setProp(kVTCompressionPropertyKey_MaxKeyFrameInterval, opts.gop, "MaxKeyFrameInterval")
    setProp(kVTCompressionPropertyKey_AverageBitRate, opts.mbps * 1_000_000, "AverageBitRate")
    setProp(kVTCompressionPropertyKey_DataRateLimits, [opts.mbps * 250_000, 1] as [Any], "DataRateLimits") // 2x over 1 s, in bytes
    setProp(kVTCompressionPropertyKey_ProfileLevel, kVTProfileLevel_HEVC_Main10_AutoLevel, "ProfileLevel")
    // The container is BT.2020 / PQ: what the addon tells QCView in the
    // hello's stream.format, and what `hevc_metadata` in the mux pins.
    setProp(kVTCompressionPropertyKey_ColorPrimaries, kCVImageBufferColorPrimaries_ITU_R_2020, "ColorPrimaries")
    setProp(kVTCompressionPropertyKey_TransferFunction, kCVImageBufferTransferFunction_SMPTE_ST_2084_PQ, "TransferFunction")
    setProp(kVTCompressionPropertyKey_YCbCrMatrix, kCVImageBufferYCbCrMatrix_ITU_R_2020, "YCbCrMatrix")
    VTCompressionSessionPrepareToEncodeFrames(session!)
    sessionW = width; sessionH = height
    keyLock.lock(); forceKey = true; keyLock.unlock()
    log("encoder ready \(width)x\(height) @\(opts.fps) \(opts.mbps)M Main10 PQ/BT.2020 gop=\(opts.gop)")
}

let t0 = DispatchTime.now().uptimeNanoseconds

func encode(_ pb: CVPixelBuffer) {
    inFlightLock.lock()
    let busy = inFlight
    inFlightLock.unlock()
    if busy >= 3 {  // skip rather than queue (Plank)
        dropped += 1
        return
    }
    guard let session else { return }
    keyLock.lock()
    let wantKey = forceKey
    forceKey = false
    keyLock.unlock()
    let props: [CFString: Any]? = wantKey ? [kVTEncodeFrameOptionKey_ForceKeyFrame: true] : nil
    let pts = CMTime(value: CMTimeValue((DispatchTime.now().uptimeNanoseconds - t0) / 1000), timescale: 1_000_000)
    outLock.lock()
    submitTimes[pts.value] = Int64(DispatchTime.now().uptimeNanoseconds)
    outLock.unlock()
    inFlightLock.lock(); inFlight += 1; inFlightLock.unlock()
    let st = VTCompressionSessionEncodeFrame(session, imageBuffer: pb, presentationTimeStamp: pts, duration: .invalid,
                                             frameProperties: props as CFDictionary?, sourceFrameRefcon: nil, infoFlagsOut: nil)
    if st != noErr {
        inFlightLock.lock(); inFlight -= 1; inFlightLock.unlock()
        if wantKey { keyLock.lock(); forceKey = true; keyLock.unlock() }
    }
}

// ------------------------------------------------------------ main loop --

let conv = Converter()
conv.matrix = opts.matrix
if let path = opts.selftest { selftest(path, conv: conv) }

guard let ring = Ring(name: opts.shm) else { exit(5) }
guard let ringBuffer = conv.device.makeBuffer(bytesNoCopy: ring.base, length: ring.size, options: .storageModeShared, deallocator: nil) else {
    log("Metal cannot wrap the ring (page alignment?)"); exit(5)
}
log("ring /\(opts.shm): \(ring.header.slotCount) slots, \(ring.header.pixelsCapacity / 1024) KiB each, producer pid \(ring.header.producerPid)")

// stdin commands
DispatchQueue.global().async {
    while let line = readLine() {
        switch line.trimmingCharacters(in: .whitespaces) {
        case "key":
            keyLock.lock(); forceKey = true; keyLock.unlock()
        case "quit": exit(0)
        default: break
        }
    }
    exit(0)  // the addon went away
}

// stats
DispatchQueue.global().async {
    var prevOut = 0, prevBytes = 0
    while true {
        sleep(1)
        outLock.lock()
        let fo = framesOut, bo = bytesOut, sum = encodeUsSum, mx = encodeUsMax
        encodeUsSum = 0; encodeUsMax = 0
        outLock.unlock()
        let n = fo - prevOut
        log(String(format: "fps=%d mbps=%.1f keys=%d in=%d dropped=%d stale=%d encode_ms avg=%.1f max=%.1f",
                   n, Double(bo - prevBytes) * 8 / 1e6, keysOut, framesIn, dropped, skippedStale,
                   n > 0 ? Double(sum) / Double(n) / 1000 : 0, Double(mx) / 1000))
        prevOut = fo; prevBytes = bo
    }
}

// The ring reader: wait for `latest` to move, convert, encode. An idle
// producer (Cycles converged, host static) yields nothing; the idle floor
// re-sends the last frame a few times a second so an SRT viewer joining a
// silent stream is not left in its demuxer probe (found 2026-09-23).
let interval = 1.0 / Double(max(opts.fps, 1))
// 10 Hz at 30 fps (was fps/8): a joining viewer's demuxer wants ~20 frames
// before it opens (ffmpeg's rate probe on HEVC), so an idle stream at 3.75 Hz
// cost it 5 s; a tiny P-frame ten times a second costs nothing (2026-10-28).
let idleFloor = 1.0 / min(max(Double(opts.fps) / 3.0, 5.0), 10.0)
var lastLatest: UInt64 = 0
var lastSent = Date.distantPast
var lastPixelBuffer: CVPixelBuffer?
var lastAlive = Date()

while true {
    let latest = ring.latest
    if latest == 0xFFFFFFFFFFFFFFFF || ring.state == STATE_RETIRED {
        log("ring retired by the producer; exiting for a respawn")
        exit(7)
    }
    if latest == lastLatest {
        let now = Date()
        if now.timeIntervalSince(lastAlive) > 0.5 {
            lastAlive = now
            if !ring.producerAlive() { log("producer gone"); exit(0) }
        }
        keyLock.lock(); let wantKey = forceKey; keyLock.unlock()
        if let pb = lastPixelBuffer, wantKey || now.timeIntervalSince(lastSent) >= idleFloor {
            // The first seconds of a session: idle re-sends are KEYframes, so
            // the mux's probe and a joining viewer's probe complete on the
            // first packets instead of waiting for bytes that never come.
            if now.timeIntervalSince(sessionStart) < 3.0 { keyLock.lock(); forceKey = true; keyLock.unlock() }
            encode(pb); lastSent = now
        }
        usleep(1000)
        continue
    }
    if Date().timeIntervalSince(lastSent) < interval {
        usleep(500)   // pace to the requested rate; the newest frame wins when we wake
        continue
    }
    let slot = Int(latest & 0xFF)
    if slot >= ring.header.slotCount { lastLatest = latest; continue }
    ring.claim(slot)
    let seqBefore = ring.seqlock(slot)
    if seqBefore & 1 == 1 {
        // Writer inside: come back for THIS frame. Advancing the cursor here
        // lost a still scene's only new frame (published once, then
        // hash-skipped) and the idle re-send kept showing the previous
        // picture at its previous size (found 2026-10-28, Shot Mode).
        ring.release(); skippedStale += 1; usleep(500); continue
    }
    let w = Int(ring.u32(slot, DESC_W)), h = Int(ring.u32(slot, DESC_H)), bpr = Int(ring.u32(slot, DESC_BPR))
    let pf = ring.u32(slot, DESC_PF)
    if pf != PF_RGBA32F || w < 16 || h < 16 || w % 2 != 0 || h % 2 != 0 || bpr * h > ring.header.pixelsCapacity {
        ring.release(); lastLatest = latest; log("bad frame desc \(w)x\(h) bpr=\(bpr) pf=\(pf)"); continue
    }
    if w != sessionW || h != sessionH {
        if sessionW != 0 {
            // A new frame size mid-stream: exit for a respawn (8). The mux
            // (ffmpeg -c copy) keeps the stream parameters it saw at open,
            // so a viewer joining after a size change was told the OLD size
            // while the frames were the new one (found 2026-10-28, Shot
            // Mode). A fresh pair costs the viewer one reconnect.
            log("frame size \(sessionW)x\(sessionH) -> \(w)x\(h): exiting for a respawn")
            ring.release(); exit(8)
        }
        makeSession(width: w, height: h)
    }
    let pb = conv.convert(buffer: ringBuffer, offset: ring.slotOffset(slot) + ring.header.pixelsOffset, width: w, height: h, bpr: bpr)
    let seqAfter = ring.seqlock(slot)
    ring.release()
    if seqAfter != seqBefore { skippedStale += 1; usleep(500); continue }   // overwritten mid-copy: retry
    lastLatest = latest
    framesIn += 1
    guard let pb else { continue }
    lastPixelBuffer = pb
    encode(pb)
    lastSent = Date()
}
