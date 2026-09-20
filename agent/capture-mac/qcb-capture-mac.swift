// qcb-capture-mac — native macOS capture + encode for the QCBridge Agent (S6).
//
// ScreenCaptureKit delivers IOSurface-backed frames straight into a
// VideoToolbox HEVC session; the encoder output is written to stdout as
// Annex-B with an AUD per access unit and VPS/SPS/PPS in band on every
// keyframe — exactly what the agent's video source already consumes from
// ffmpeg. stdin takes one-line commands: "key" forces a keyframe on the next
// frame (keyframe-on-join), "quit" exits.
//
//   swiftc -O qcb-capture-mac.swift -o qcb-capture-mac
//   qcb-capture-mac [--display main|<id>] [--fps 60] [--bitrate 50]
//                   [--region X,Y,W,H (points)] [--scale 1.0] [--10bit]
//                   [--gop 600] [--cursor]
//
// Settings follow what the parity spikes and Plank measured: RealTime,
// no frame reordering, speed over quality, matched colour tags, long GOP
// with on-demand keys, DataRateLimits 2x over 1 s, queueDepth 3.

import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit
import VideoToolbox

struct Opts {
    var display = "main"
    var fps = 60
    var mbps = 50
    var region: CGRect? = nil
    var scale: CGFloat = 1.0
    var tenBit = false
    var gop = 600
    var cursor = false
}

func parseArgs() -> Opts {
    var o = Opts()
    var it = CommandLine.arguments.dropFirst().makeIterator()
    while let a = it.next() {
        switch a {
        case "--display": o.display = it.next() ?? "main"
        case "--fps": o.fps = Int(it.next() ?? "60") ?? 60
        case "--bitrate": o.mbps = Int(it.next() ?? "50") ?? 50
        case "--region":
            let p = (it.next() ?? "").split(separator: ",").compactMap { Double($0) }
            if p.count == 4 { o.region = CGRect(x: p[0], y: p[1], width: p[2], height: p[3]) }
        case "--scale": o.scale = CGFloat(Double(it.next() ?? "1") ?? 1)
        case "--10bit": o.tenBit = true
        case "--gop": o.gop = Int(it.next() ?? "600") ?? 600
        case "--cursor": o.cursor = true
        default:
            FileHandle.standardError.write("unknown arg \(a)\n".data(using: .utf8)!)
            exit(2)
        }
    }
    return o
}

func log(_ s: String) {
    FileHandle.standardError.write(("[capture] " + s + "\n").data(using: .utf8)!)
}

let opts = parseArgs()
let out = FileHandle.standardOutput
let outLock = NSLock()
var forceKey = true  // first frame is a key; the agent asks for more with "key"
let keyLock = NSLock()

// Stats
var framesIn = 0, framesOut = 0, bytesOut = 0, keysOut = 0, dropped = 0
var encodeUsSum: Int64 = 0, encodeUsMax: Int64 = 0

// ------------------------------------------------------------ encoder ----

var session: VTCompressionSession?
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

let submitTimes = NSMutableDictionary()  // pts value -> submit time (ns)

func outputCallback(_: UnsafeMutableRawPointer?, _ refcon: UnsafeMutableRawPointer?, status: OSStatus,
                    _: VTEncodeInfoFlags, sbuf: CMSampleBuffer?) {
    output.inFlightLock.lock(); output.inFlight = max(0, output.inFlight - 1); output.inFlightLock.unlock()
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
    setProp(kVTCompressionPropertyKey_ProfileLevel,
            opts.tenBit ? kVTProfileLevel_HEVC_Main10_AutoLevel : kVTProfileLevel_HEVC_Main_AutoLevel, "ProfileLevel")
    setProp(kVTCompressionPropertyKey_ColorPrimaries, kCVImageBufferColorPrimaries_ITU_R_709_2, "ColorPrimaries")
    setProp(kVTCompressionPropertyKey_TransferFunction, kCVImageBufferTransferFunction_sRGB, "TransferFunction")
    setProp(kVTCompressionPropertyKey_YCbCrMatrix, kCVImageBufferYCbCrMatrix_ITU_R_709_2, "YCbCrMatrix")
    VTCompressionSessionPrepareToEncodeFrames(session!)
    log("encoder ready \(width)x\(height) @\(opts.fps) \(opts.mbps)M \(opts.tenBit ? "Main10" : "Main") gop=\(opts.gop)")
}

// ------------------------------------------------------------ capture ----

final class Output: NSObject, SCStreamOutput, SCStreamDelegate {
    var inFlight = 0
    let inFlightLock = NSLock()

    var callbacks = 0
    func stream(_ stream: SCStream, didOutputSampleBuffer sbuf: CMSampleBuffer, of type: SCStreamOutputType) {
        callbacks += 1
        guard type == .screen else { return }
        var status = -1
        if let att = CMSampleBufferGetSampleAttachmentsArray(sbuf, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]],
           let st = att.first?[.status] as? Int {
            status = st
        }
        if callbacks == 1 { log("first frame: status=\(status)") }
        guard let pix = CMSampleBufferGetImageBuffer(sbuf) else { return }
        if status != -1 && status != SCFrameStatus.complete.rawValue {
            return  // idle / blank / suspended frames carry no new pixels
        }
        framesIn += 1
        inFlightLock.lock()
        let busy = inFlight
        inFlightLock.unlock()
        if busy >= 3 {  // Plank: skip rather than queue
            dropped += 1
            return
        }
        guard let session else { return }
        keyLock.lock()
        let wantKey = forceKey
        forceKey = false
        keyLock.unlock()
        let props: [CFString: Any]? = wantKey ? [kVTEncodeFrameOptionKey_ForceKeyFrame: true] : nil
        let pts = CMSampleBufferGetPresentationTimeStamp(sbuf)
        outLock.lock()
        submitTimes[pts.value] = Int64(DispatchTime.now().uptimeNanoseconds)
        outLock.unlock()
        inFlightLock.lock(); inFlight += 1; inFlightLock.unlock()
        let st = VTCompressionSessionEncodeFrame(session, imageBuffer: pix, presentationTimeStamp: pts, duration: .invalid,
                                                 frameProperties: props as CFDictionary?, sourceFrameRefcon: nil,
                                                 infoFlagsOut: nil)
        if st != noErr {
            inFlightLock.lock(); inFlight -= 1; inFlightLock.unlock()
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        log("stream stopped: \(error.localizedDescription)")
        exit(4)
    }
}

let output = Output()
var liveStream: SCStream?  // must outlive start(): a released SCStream stops capturing silently

func start() async {
    do {
        log("requesting shareable content (a Screen Recording permission prompt may appear the first time)")
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        log("content: \(content.displays.count) displays")
        guard var display = content.displays.first else { log("no displays"); exit(5) }
        if opts.display != "main", let id = UInt32(opts.display),
           let d = content.displays.first(where: { $0.displayID == id }) { display = d }
        let filter = SCContentFilter(display: display, excludingWindows: [])
        let cfg = SCStreamConfiguration()
        let pointScale = CGFloat(filter.pointPixelScale)
        let rect = opts.region ?? CGRect(x: 0, y: 0, width: display.width, height: display.height)
        cfg.sourceRect = rect
        cfg.width = Int((rect.width * pointScale * opts.scale).rounded(.down)) & ~1
        cfg.height = Int((rect.height * pointScale * opts.scale).rounded(.down)) & ~1
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(opts.fps))
        cfg.queueDepth = 3
        cfg.pixelFormat = opts.tenBit ? kCVPixelFormatType_420YpCbCr10BiPlanarVideoRange
                                      : kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
        cfg.colorSpaceName = CGColorSpace.sRGB
        cfg.showsCursor = opts.cursor
        makeSession(width: cfg.width, height: cfg.height)
        let stream = SCStream(filter: filter, configuration: cfg, delegate: output)
        liveStream = stream
        try stream.addStreamOutput(output, type: .screen, sampleHandlerQueue: DispatchQueue(label: "capture", qos: .userInteractive))
        try await stream.startCapture()
        log("capturing display \(display.displayID) rect \(Int(rect.minX)),\(Int(rect.minY)) \(Int(rect.width))x\(Int(rect.height)) pt -> \(cfg.width)x\(cfg.height) px")
    } catch {
        log("capture start failed: \(error.localizedDescription) (Screen Recording permission?)")
        exit(6)
    }
}

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
    exit(0)  // agent went away
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
        log(String(format: "fps=%d mbps=%.1f keys=%d in=%d dropped=%d encode_ms avg=%.1f max=%.1f",
                   n, Double(bo - prevBytes) * 8 / 1e6, keysOut, framesIn, dropped,
                   n > 0 ? Double(sum) / Double(n) / 1000 : 0, Double(mx) / 1000))
        prevOut = fo; prevBytes = bo
    }
}

Task { await start() }
RunLoop.main.run()
