// qcb-stamp — a synthetic stamped HEVC source, for measuring transport cost.
//
// Draws the ring1 probe strip (probe.py: marker | 32-bit ms | 16-bit seq |
// 8-bit check, 12 px blocks) into a noise frame, encodes it with
// VideoToolbox using the SAME session settings vtlat.swift measured at
// 3-12 ms @1080p60, and writes Annex-B to stdout.
//
// The stamp is taken immediately before the encode submit, so a reader's
// `lat_ms` is encode -> transport -> decode. Capture and the hot lane are
// deliberately NOT in it: this exists to isolate what the wire costs, with
// the encoder held constant across every rung of the ladder. It is not a
// motion-to-photon number and must not be compared to one.
//
// Single machine, one clock, same as probe_reader.py assumes.
//
//   swiftc -O qcb-stamp.swift -o qcb-stamp
//   ./qcb-stamp --fps 60 --size 1920x1080 --bitrate 50 --seconds 30 > /dev/null
//
// Usage: qcb-stamp [--codec hevc|h264] [--fps 60] [--size WxH] [--bitrate 50]
//                  [--seconds 30] [--static]

import CoreMedia
import CoreVideo
import Foundation
import VideoToolbox

// ---- options ---------------------------------------------------------------

struct Opts {
    var codec = "hevc"
    var fps = 60
    var width = 1920
    var height = 1080
    var mbps = 50
    var seconds = 30.0
    var noise = true
    // Empty: write Annex-B to stdout (pipe it to an ffmpeg child, as the
    // early rungs did). Set: mux to mpegts and send from THIS process, so
    // the cost of that child can be measured by its absence.
    var muxURL = ""
}

func parse() -> Opts {
    var o = Opts()
    var it = CommandLine.arguments.dropFirst().makeIterator()
    while let a = it.next() {
        switch a {
        case "--codec": o.codec = it.next()!
        case "--fps": o.fps = Int(it.next()!)!
        case "--size":
            let p = it.next()!.split(separator: "x")
            o.width = Int(p[0])!; o.height = Int(p[1])!
        case "--bitrate": o.mbps = Int(it.next()!)!
        case "--seconds": o.seconds = Double(it.next()!)!
        case "--static": o.noise = false
        case "--mux-url": o.muxURL = it.next()!
        default:
            FileHandle.standardError.write("unknown argument: \(a)\n".data(using: .utf8)!)
            exit(2)
        }
    }
    return o
}

let o = parse()

func err(_ s: String) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
}

/// Last failure text from the C sender.
func msErr() -> String {
    guard let p = ms_error() else { return "unknown" }
    return String(cString: p)
}

// ---- probe strip (mirror of qcbridge/ring1/probe.py) ------------------------

let MARKER: [UInt8] = [1, 0, 1, 1, 0, 0, 1, 0]
let DATA_BITS = 32 + 16 + 8
let TOTAL_BITS = MARKER.count + DATA_BITS
let BLOCK_PX = 12

/// probe.py `_check`: sum of t_ms LE4 ++ seq LE2, xor 0xA5.
func checkByte(_ tMs: UInt32, _ seq: UInt16) -> UInt8 {
    var sum = 0
    for i in 0..<4 { sum += Int((tMs >> (8 * UInt32(i))) & 0xFF) }
    for i in 0..<2 { sum += Int((seq >> (8 * UInt16(i))) & 0xFF) }
    return UInt8((sum ^ 0xA5) & 0xFF)
}

func encodeBits(_ tHost: Double, _ seq: Int) -> [UInt8] {
    let tMs = UInt32(truncatingIfNeeded: Int64(tHost * 1000))
    let s = UInt16(truncatingIfNeeded: seq)
    let word: UInt64 = UInt64(tMs)
        | (UInt64(s) << 32)
        | (UInt64(checkByte(tMs, s)) << 48)
    var bits = MARKER
    for i in 0..<DATA_BITS { bits.append(UInt8((word >> UInt64(i)) & 1)) }
    return bits
}

// Strip geometry: one guard block of black either side, inside the bottom
// band probe_reader.py scans by default (`crop=iw:128:0:ih-128`).
let stripW = (TOTAL_BITS + 2) * BLOCK_PX
let stripX = BLOCK_PX                 // leaves the left guard at x=0
let stripY = o.height - 80            // 80 rows up: comfortably inside the band
precondition(stripW + 2 * BLOCK_PX <= o.width, "frame too narrow for the probe strip")

/// Burn the strip into an NV12 luma plane. White = 255, black = 0; the
/// surrounding quiet band is 60, which reads as "dark" to find_strip's
/// guard test (< 64) without being pure black.
func drawStrip(_ base: UnsafeMutableRawPointer, _ bpr: Int, _ bits: [UInt8]) {
    let luma = base.assumingMemoryBound(to: UInt8.self)
    // quiet band around the strip, so noise cannot fake a marker nearby
    for y in (stripY - 12)..<(stripY + BLOCK_PX + 12) {
        memset(luma + y * bpr, 60, o.width)
    }
    for y in stripY..<(stripY + BLOCK_PX) {
        let row = luma + y * bpr
        // guard blocks
        memset(row + stripX - BLOCK_PX, 0, BLOCK_PX)
        memset(row + stripX + TOTAL_BITS * BLOCK_PX, 0, BLOCK_PX)
        for (i, b) in bits.enumerated() {
            memset(row + stripX + i * BLOCK_PX, b == 1 ? 255 : 0, BLOCK_PX)
        }
    }
}

// ---- Annex-B writer ---------------------------------------------------------

let muxing = !o.muxURL.isEmpty
let stdoutFH = FileHandle.standardOutput
let startCode = Data([0, 0, 0, 1])
var bytesOut = 0
var framesOut = 0
let outLock = NSLock()

func writeOut(_ d: Data) {
    stdoutFH.write(d)
    bytesOut += d.count
}

/// Parameter sets (VPS/SPS/PPS for HEVC, SPS/PPS for H.264) as Annex-B.
func parameterSets(_ fmt: CMFormatDescription) -> Data {
    var out = Data()
    var count = 0
    let isHEVC = o.codec != "h264"
    let getCount: OSStatus = isHEVC
        ? CMVideoFormatDescriptionGetHEVCParameterSetAtIndex(
            fmt, parameterSetIndex: 0, parameterSetPointerOut: nil,
            parameterSetSizeOut: nil, parameterSetCountOut: &count, nalUnitHeaderLengthOut: nil)
        : CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
            fmt, parameterSetIndex: 0, parameterSetPointerOut: nil,
            parameterSetSizeOut: nil, parameterSetCountOut: &count, nalUnitHeaderLengthOut: nil)
    guard getCount == noErr else { return out }
    for i in 0..<count {
        var ptr: UnsafePointer<UInt8>?
        var size = 0
        let s: OSStatus = isHEVC
            ? CMVideoFormatDescriptionGetHEVCParameterSetAtIndex(
                fmt, parameterSetIndex: i, parameterSetPointerOut: &ptr,
                parameterSetSizeOut: &size, parameterSetCountOut: nil, nalUnitHeaderLengthOut: nil)
            : CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
                fmt, parameterSetIndex: i, parameterSetPointerOut: &ptr,
                parameterSetSizeOut: &size, parameterSetCountOut: nil, nalUnitHeaderLengthOut: nil)
        if s == noErr, let ptr {
            out.append(startCode)
            out.append(ptr, count: size)
        }
    }
    return out
}

/// VideoToolbox hands back length-prefixed NAL units; the wire wants start
/// codes. Parameter sets are re-emitted on every keyframe so a receiver that
/// joins mid-stream (or an SRT reconnect) can decode from the next IDR.
func emit(_ sbuf: CMSampleBuffer) {
    let attachments = CMSampleBufferGetSampleAttachmentsArray(sbuf, createIfNecessary: false)
    var isKey = true
    if let arr = attachments, CFArrayGetCount(arr) > 0 {
        let dict = unsafeBitCast(CFArrayGetValueAtIndex(arr, 0), to: CFDictionary.self)
        let key = Unmanaged.passUnretained(kCMSampleAttachmentKey_NotSync).toOpaque()
        if CFDictionaryContainsKey(dict, key) { isKey = false }
    }

    var out = Data()
    if isKey, let fmt = CMSampleBufferGetFormatDescription(sbuf) {
        out.append(parameterSets(fmt))
    }

    guard let block = CMSampleBufferGetDataBuffer(sbuf) else { return }
    var length = 0
    var dataPtr: UnsafeMutablePointer<Int8>?
    guard CMBlockBufferGetDataPointer(block, atOffset: 0, lengthAtOffsetOut: nil,
                                      totalLengthOut: &length, dataPointerOut: &dataPtr) == noErr,
          let dataPtr else { return }
    let bytes = UnsafeRawPointer(dataPtr).assumingMemoryBound(to: UInt8.self)

    // 4-byte big-endian NAL lengths (what VT emits for both codecs here).
    var i = 0
    while i + 4 <= length {
        let n = Int(bytes[i]) << 24 | Int(bytes[i + 1]) << 16
              | Int(bytes[i + 2]) << 8 | Int(bytes[i + 3])
        i += 4
        if n <= 0 || i + n > length { break }
        out.append(startCode)
        out.append(bytes + i, count: n)
        i += n
    }

    outLock.lock()
    if muxing {
        let pts = Int64(framesOut)
        out.withUnsafeBytes { raw in
            if let base = raw.bindMemory(to: UInt8.self).baseAddress {
                if ms_write(base, Int32(out.count), pts, isKey ? 1 : 0) < 0 {
                    err("mux write failed: \(msErr())")
                }
            }
        }
        bytesOut += out.count
    } else {
        writeOut(out)
    }
    framesOut += 1
    outLock.unlock()
}

// ---- encoder ----------------------------------------------------------------

let codecType: CMVideoCodecType = o.codec == "h264" ? kCMVideoCodecType_H264 : kCMVideoCodecType_HEVC
let spec: [CFString: Any] = [
    kVTVideoEncoderSpecification_RequireHardwareAcceleratedVideoEncoder: true,
]
let srcAttrs: [CFString: Any] = [
    kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
    kCVPixelBufferWidthKey: o.width,
    kCVPixelBufferHeightKey: o.height,
    kCVPixelBufferIOSurfacePropertiesKey: [:] as [CFString: Any],
]

var session: VTCompressionSession?
let st = VTCompressionSessionCreate(
    allocator: nil, width: Int32(o.width), height: Int32(o.height), codecType: codecType,
    encoderSpecification: spec as CFDictionary, imageBufferAttributes: srcAttrs as CFDictionary,
    compressedDataAllocator: nil,
    outputCallback: { _, _, status, _, sbuf in
        guard status == noErr, let sbuf else { return }
        emit(sbuf)
    },
    refcon: nil, compressionSessionOut: &session)
guard st == noErr, let session else {
    err("session create failed: \(st)")
    exit(2)
}

func setProp(_ key: CFString, _ value: Any, _ name: String) {
    let s = VTSessionSetProperty(session, key: key, value: value as CFTypeRef)
    if s != noErr { err("  property \(name) rejected: \(s)") }
}
// Identical to vtlat.swift's measured configuration.
setProp(kVTCompressionPropertyKey_RealTime, kCFBooleanTrue!, "RealTime")
setProp(kVTCompressionPropertyKey_AllowFrameReordering, kCFBooleanFalse!, "AllowFrameReordering")
setProp(kVTCompressionPropertyKey_PrioritizeEncodingSpeedOverQuality, kCFBooleanTrue!, "PrioritizeSpeed")
setProp(kVTCompressionPropertyKey_ExpectedFrameRate, o.fps as CFNumber, "ExpectedFrameRate")
setProp(kVTCompressionPropertyKey_MaxKeyFrameInterval, o.fps as CFNumber, "MaxKeyFrameInterval")
setProp(kVTCompressionPropertyKey_AverageBitRate, (o.mbps * 1_000_000) as CFNumber, "AverageBitRate")
if o.codec == "hevc" {
    setProp(kVTCompressionPropertyKey_ProfileLevel, kVTProfileLevel_HEVC_Main_AutoLevel, "ProfileLevel")
} else {
    setProp(kVTCompressionPropertyKey_ProfileLevel, kVTProfileLevel_H264_High_AutoLevel, "ProfileLevel")
}
VTCompressionSessionPrepareToEncodeFrames(session)

var pool: CVPixelBufferPool?
CVPixelBufferPoolCreate(nil, nil, srcAttrs as CFDictionary, &pool)

// ---- run --------------------------------------------------------------------

let total = Int(o.seconds * Double(o.fps))
let period = 1.0 / Double(o.fps)
err("qcb-stamp: \(o.codec) \(o.width)x\(o.height)@\(o.fps) \(o.mbps)M, "
    + "\(total) frames, strip at y=\(stripY) w=\(stripW)")

// Open the wire BEFORE the clock starts. A listener URL blocks here until
// the reader connects, and if `start` were already running the pacing loop
// would burst to catch up on the frames it "missed" while waiting.
if muxing {
    err("qcb-stamp: opening \(o.muxURL)")
    if ms_open(o.muxURL, Int32(o.width), Int32(o.height), Int32(o.fps),
               o.codec == "hevc" ? 1 : 0) < 0 {
        err("qcb-stamp: mux open failed: \(msErr())")
        exit(3)
    }
    err("qcb-stamp: connected")
}

let start = Date()

for i in 0..<total {
    var pb: CVPixelBuffer?
    CVPixelBufferPoolCreatePixelBuffer(nil, pool!, &pb)
    guard let b = pb else { continue }
    CVPixelBufferLockBaseAddress(b, [])
    let planes = max(1, CVPixelBufferGetPlaneCount(b))
    for p in 0..<planes {
        guard let base = CVPixelBufferGetBaseAddressOfPlane(b, p) else { continue }
        let bpr = CVPixelBufferGetBytesPerRowOfPlane(b, p)
        let h = CVPixelBufferGetHeightOfPlane(b, p)
        if p == 0 {
            // Luma: noise keeps the bitrate honest (a static frame compresses
            // to almost nothing and would flatter every rung equally).
            if o.noise { arc4random_buf(base, bpr * h) } else { memset(base, 60, bpr * h) }
        } else {
            memset(base, 128, bpr * h)
        }
    }
    // Stamp LAST, immediately before submit: everything above is frame
    // construction, which is not what we are measuring.
    if let base = CVPixelBufferGetBaseAddressOfPlane(b, 0) {
        drawStrip(base, CVPixelBufferGetBytesPerRowOfPlane(b, 0),
                  encodeBits(Date().timeIntervalSince1970, i))
    }
    CVPixelBufferUnlockBaseAddress(b, [])

    let pts = CMTime(value: CMTimeValue(i), timescale: CMTimeScale(o.fps))
    VTCompressionSessionEncodeFrame(
        session, imageBuffer: b, presentationTimeStamp: pts, duration: .invalid,
        frameProperties: nil, sourceFrameRefcon: nil, infoFlagsOut: nil)

    let wait = start.addingTimeInterval(Double(i + 1) * period).timeIntervalSinceNow
    if wait > 0 { Thread.sleep(forTimeInterval: wait) }
}

VTCompressionSessionCompleteFrames(session, untilPresentationTimeStamp: .invalid)
if muxing { ms_close() }
let secs = Date().timeIntervalSince(start)
err(String(format: "qcb-stamp: %d frames out, %.1f Mbps over %.1f s",
           framesOut, Double(bytesOut * 8) / secs / 1e6, secs))
