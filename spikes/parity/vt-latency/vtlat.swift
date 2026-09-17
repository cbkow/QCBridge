// vtlat — native VideoToolbox encode latency probe (parity spike).
//
// Times submit (VTCompressionSessionEncodeFrame) -> output callback per frame,
// paced in real time, no ffmpeg. Separates Apple's encoder latency from
// ffmpeg's hevc_videotoolbox wrapper.
//
//   swiftc -O vtlat.swift -o vtlat
//   ./vtlat --codec hevc --fps 60 --size 1920x1080 --bitrate 50 [--no-realtime]
//           [--max-delay 0] [--low-latency] [--no-speed] [--pixfmt nv12|p010|bgra]
//           [--frames 360] [--static]

import CoreMedia
import CoreVideo
import Foundation
import VideoToolbox

struct Opts {
    var codec = "hevc"
    var fps = 60
    var width = 1920
    var height = 1080
    var mbps = 50
    var realtime = true
    var maxDelay: Int? = nil
    var lowLatency = false
    var speed = true
    var pixfmt = "nv12"
    var frames = 360
    var noise = true
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
        case "--no-realtime": o.realtime = false
        case "--max-delay": o.maxDelay = Int(it.next()!)!
        case "--low-latency": o.lowLatency = true
        case "--no-speed": o.speed = false
        case "--pixfmt": o.pixfmt = it.next()!
        case "--frames": o.frames = Int(it.next()!)!
        case "--static": o.noise = false
        default: fatalError("unknown arg \(a)")
        }
    }
    return o
}

let o = parse()
let lock = NSLock()
var submitNs = [Int: UInt64]()
var latMs = [Double]()
var outOrder = [Int]()
var bytesOut = 0

func now() -> UInt64 { DispatchTime.now().uptimeNanoseconds }

let codecType: CMVideoCodecType = o.codec == "h264" ? kCMVideoCodecType_H264 : kCMVideoCodecType_HEVC
var spec: [CFString: Any] = [
    kVTVideoEncoderSpecification_RequireHardwareAcceleratedVideoEncoder: true,
]
if o.lowLatency {
    spec[kVTVideoEncoderSpecification_EnableLowLatencyRateControl] = true
}
let pix: OSType = switch o.pixfmt {
case "p010": kCVPixelFormatType_420YpCbCr10BiPlanarVideoRange
case "bgra": kCVPixelFormatType_32BGRA
default: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
}
let srcAttrs: [CFString: Any] = [
    kCVPixelBufferPixelFormatTypeKey: pix,
    kCVPixelBufferWidthKey: o.width,
    kCVPixelBufferHeightKey: o.height,
    kCVPixelBufferIOSurfacePropertiesKey: [:] as [CFString: Any],
]

var session: VTCompressionSession?
let st = VTCompressionSessionCreate(
    allocator: nil, width: Int32(o.width), height: Int32(o.height), codecType: codecType,
    encoderSpecification: spec as CFDictionary, imageBufferAttributes: srcAttrs as CFDictionary,
    compressedDataAllocator: nil,
    outputCallback: { _, refcon, status, _, sbuf in
        let t = now()
        let idx = Int(bitPattern: refcon)
        guard status == noErr, let sbuf else { return }
        lock.lock()
        if let s = submitNs[idx] {
            latMs.append(Double(t - s) / 1e6)
            outOrder.append(idx)
        }
        bytesOut += CMSampleBufferGetTotalSampleSize(sbuf)
        lock.unlock()
    },
    refcon: nil, compressionSessionOut: &session)
guard st == noErr, let session else {
    print("session create failed: \(st) (low-latency=\(o.lowLatency) codec=\(o.codec))")
    exit(2)
}

func setProp(_ key: CFString, _ value: Any, _ name: String) {
    let s = VTSessionSetProperty(session, key: key, value: value as CFTypeRef)
    if s != noErr { print("  property \(name) rejected: \(s)") }
}
setProp(kVTCompressionPropertyKey_RealTime, o.realtime as CFBoolean, "RealTime")
setProp(kVTCompressionPropertyKey_AllowFrameReordering, kCFBooleanFalse!, "AllowFrameReordering")
if o.speed {
    setProp(kVTCompressionPropertyKey_PrioritizeEncodingSpeedOverQuality, kCFBooleanTrue!, "PrioritizeSpeed")
}
setProp(kVTCompressionPropertyKey_ExpectedFrameRate, o.fps as CFNumber, "ExpectedFrameRate")
setProp(kVTCompressionPropertyKey_MaxKeyFrameInterval, o.fps as CFNumber, "MaxKeyFrameInterval")
setProp(kVTCompressionPropertyKey_AverageBitRate, (o.mbps * 1_000_000) as CFNumber, "AverageBitRate")
if let d = o.maxDelay {
    setProp(kVTCompressionPropertyKey_MaxFrameDelayCount, d as CFNumber, "MaxFrameDelayCount")
}
if o.codec == "hevc" {
    let prof = o.pixfmt == "p010" ? kVTProfileLevel_HEVC_Main10_AutoLevel : kVTProfileLevel_HEVC_Main_AutoLevel
    setProp(kVTCompressionPropertyKey_ProfileLevel, prof, "ProfileLevel")
} else {
    setProp(kVTCompressionPropertyKey_ProfileLevel, kVTProfileLevel_H264_High_AutoLevel, "ProfileLevel")
}
VTCompressionSessionPrepareToEncodeFrames(session)

var pool: CVPixelBufferPool?
CVPixelBufferPoolCreate(nil, nil, srcAttrs as CFDictionary, &pool)

func frame(_ i: Int) -> CVPixelBuffer {
    var pb: CVPixelBuffer?
    CVPixelBufferPoolCreatePixelBuffer(nil, pool!, &pb)
    let b = pb!
    CVPixelBufferLockBaseAddress(b, [])
    let planes = max(1, CVPixelBufferGetPlaneCount(b))
    for p in 0..<planes {
        let isPlanar = CVPixelBufferIsPlanar(b)
        guard let base = isPlanar ? CVPixelBufferGetBaseAddressOfPlane(b, p) : CVPixelBufferGetBaseAddress(b) else { continue }
        let bpr = isPlanar ? CVPixelBufferGetBytesPerRowOfPlane(b, p) : CVPixelBufferGetBytesPerRow(b)
        let h = isPlanar ? CVPixelBufferGetHeightOfPlane(b, p) : CVPixelBufferGetHeight(b)
        let n = bpr * h
        if o.noise && p == 0 {
            arc4random_buf(base, n)
        } else {
            memset(base, p == 0 ? 60 : 128, n)
        }
    }
    CVPixelBufferUnlockBaseAddress(b, [])
    return b
}

let warmup = o.fps
let period = 1.0 / Double(o.fps)
let start = Date()
for i in 0..<(o.frames + warmup) {
    let pb = frame(i)  // built before the stamp: fill cost is not encoder latency
    let pts = CMTime(value: CMTimeValue(i), timescale: CMTimeScale(o.fps))
    lock.lock(); if i >= warmup { submitNs[i] = now() }; lock.unlock()
    VTCompressionSessionEncodeFrame(
        session, imageBuffer: pb, presentationTimeStamp: pts, duration: .invalid,
        frameProperties: nil, sourceFrameRefcon: UnsafeMutableRawPointer(bitPattern: i),
        infoFlagsOut: nil)
    let next = start.addingTimeInterval(Double(i + 1) * period)
    let wait = next.timeIntervalSinceNow
    if wait > 0 { Thread.sleep(forTimeInterval: wait) }
}
VTCompressionSessionCompleteFrames(session, untilPresentationTimeStamp: .invalid)

lock.lock()
let sorted = latMs.sorted()
func pct(_ p: Double) -> Double { sorted.isEmpty ? .nan : sorted[min(sorted.count - 1, Int((p / 100) * Double(sorted.count - 1)))] }
let reordered = zip(outOrder, outOrder.dropFirst()).filter { $0.0 > $0.1 }.count
let secs = Double(o.frames + warmup) / Double(o.fps)
print(String(format: "codec=%@ %dx%d@%d %@ %dM rt=%@ maxDelay=%@ lowLat=%@ speed=%@ | n=%d p50=%.1f p95=%.1f p99=%.1f max=%.1f ms | reordered=%d | %.1f Mbps",
             o.codec, o.width, o.height, o.fps, o.pixfmt, o.mbps, o.realtime ? "y" : "n",
             o.maxDelay.map(String.init) ?? "-", o.lowLatency ? "y" : "n", o.speed ? "y" : "n",
             sorted.count, pct(50), pct(95), pct(99), sorted.last ?? .nan, reordered,
             Double(bytesOut * 8) / secs / 1e6))
lock.unlock()
