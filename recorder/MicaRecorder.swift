// mica-recorder — 会議の録画（画面＋相手の声＋自分の声）と、マイクを使っているアプリの一覧。
//   mica-recorder mic-users                      マイクを今使っているアプリの bundle id を1行ずつ出す
//   mica-recorder record --out X.mov [--bundle zoom.us]
//       --bundle のアプリの窓がある画面を録る（無ければ主画面）。SIGTERM / SIGINT で止めて書き終える。
//       音は2本に分けて書く：音声1本目＝相手（Mac から出る音）、2本目＝自分（マイク）。
//       録り始めたら <out>.pid に自分の pid を書く（見張り役はこれで止める）。
// ビルド: ./build.sh（画面収録の許可をこのアプリ1つに付けるため、MicaRecorder.app に包んで署名する）
import AVFoundation
import CoreAudio
import Foundation
import ScreenCaptureKit

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write(("mica-recorder: " + msg + "\n").data(using: .utf8)!)
    exit(1)
}

// MARK: mic-users

func audioProp<T>(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector, _ initial: T) -> T? {
    var addr = AudioObjectPropertyAddress(mSelector: sel, mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: kAudioObjectPropertyElementMain)
    var value = initial
    var size = UInt32(MemoryLayout<T>.size)
    return AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &value) == noErr ? value : nil
}

func micUsers() -> [String] {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyProcessObjectList,
                                          mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size) == noErr
    else { fail("process list unavailable") }
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids) == noErr
    else { fail("process list read failed") }
    var out: [String] = []
    for id in ids {
        guard audioProp(id, kAudioProcessPropertyIsRunningInput, UInt32(0)) == 1 else { continue }
        let bid = audioProp(id, kAudioProcessPropertyBundleID, "" as CFString) as String? ?? ""
        let pid = audioProp(id, kAudioProcessPropertyPID, pid_t(0)) ?? 0
        if pid == getpid() { continue }
        out.append(bid.isEmpty ? "pid:\(pid)" : bid)
    }
    return out
}

// MARK: record

// Completed 5-second audio files are published atomically for the live transcriber.
// The original movie remains the source for the existing post-meeting analysis.
// Diagnostic tap is before the shared media writer queue. It preserves received PCM.
final class AudioTrace {
    struct Mark {
        let seq: Int
        let pts: Double
        let frames: Int
        let host: Double

        init(seq: Int, buffer: CMSampleBuffer) {
            self.seq = seq
            pts = buffer.presentationTimeStamp.seconds
            frames = buffer.numSamples
            host = ProcessInfo.processInfo.systemUptime
        }

        func values(stage: String) -> [String: Any] {
            ["stage": stage, "seq": seq, "pts": pts, "frames": frames, "host": host]
        }
    }

    let directory: URL
    let channel: String
    let queue: DispatchQueue
    private let slots = DispatchSemaphore(value: 1024)
    // Each producer owns its counter. Both producers are fenced before finish.
    private var sequence = 0
    private var acceptingCallbacks = true
    private var droppedCallbacks = 0
    private var droppedQueued = 0
    // Only the trace queue touches files and these counters.
    private var file: AVAudioFile?
    private let log: FileHandle
    private var writtenCallbacks = 0
    private var writtenQueued = 0
    private var writtenFrames = 0
    private var failures = 0
    private var firstError: String?

    init(directory: URL, channel: String) throws {
        self.directory = directory; self.channel = channel
        queue = DispatchQueue(label: "mica-recorder.trace.\(channel)", qos: .utility)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let url = directory.appendingPathComponent("\(channel).jsonl")
        try Data().write(to: url)
        log = try FileHandle(forWritingTo: url)
    }

    // Called only on the channel's sample callback queue.
    func beginCallback(_ buffer: CMSampleBuffer) -> Mark? {
        guard acceptingCallbacks else { return nil }
        sequence += 1
        return Mark(seq: sequence, buffer: buffer)
    }

    func stopCallbacks() { acceptingCallbacks = false }

    func recordCallback(_ mark: Mark, ownedBuffer: CMSampleBuffer?) {
        guard slots.wait(timeout: .now()) == .success else { droppedCallbacks += 1; return }
        queue.async(qos: .utility, flags: .enforceQoS) {
            defer { self.slots.signal() }
            var values = mark.values(stage: "callback")
            do {
                guard let ownedBuffer else { throw CocoaError(.fileWriteUnknown) }
                try self.writePCM(ownedBuffer, values: &values)
            } catch {
                let message = ownedBuffer == nil ? "受信音声の所有コピーがありません" : "PCM: \(error.localizedDescription)"
                values["error"] = message
                self.noteFailure(message)
            }
            if self.writeEvent(values) { self.writtenCallbacks += 1 }
        }
    }

    // Called only on the shared media writer queue, after enqueue/drain.
    func recordQueued(_ mark: Mark) {
        guard mark.seq > 0 else { return }
        guard slots.wait(timeout: .now()) == .success else { droppedQueued += 1; return }
        queue.async(qos: .utility, flags: .enforceQoS) {
            defer { self.slots.signal() }
            if self.writeEvent(mark.values(stage: "queued")) { self.writtenQueued += 1 }
        }
    }

    private func writePCM(_ buffer: CMSampleBuffer, values: inout [String: Any]) throws {
        try buffer.withAudioBufferList { list, _ in
            guard var format = buffer.formatDescription?.audioStreamBasicDescription,
                  let av = AVAudioFormat(streamDescription: &format),
                  let pcm = AVAudioPCMBuffer(pcmFormat: av, bufferListNoCopy: list.unsafePointer)
            else { throw NSError(domain: "AudioTrace", code: 1) }
            if file == nil {
                file = try AVAudioFile(forWriting: directory.appendingPathComponent("\(channel).caf"),
                    settings: av.settings, commonFormat: av.commonFormat, interleaved: av.isInterleaved)
            }
            values["rate"] = av.sampleRate; values["channels"] = av.channelCount
            values["file_frame"] = file!.framePosition
            try file!.write(from: pcm)
            writtenFrames += Int(pcm.frameLength)
        }
    }

    private func writeEvent(_ values: [String: Any]) -> Bool {
        do {
            var data = try JSONSerialization.data(withJSONObject: values, options: [.sortedKeys])
            data.append(10)
            try log.write(contentsOf: data)
            return true
        } catch {
            noteFailure("JSONL: \(error.localizedDescription)")
            return false
        }
    }

    private func noteFailure(_ message: String) {
        failures += 1
        if firstError == nil { firstError = message }
    }

    // Called after both producers stop submitting. FIFO sync drains every
    // accepted event before closing the diagnostic files.
    func finish() -> [String: Any] {
        queue.sync {
            file?.close(); file = nil
            do { try log.synchronize() } catch { noteFailure("JSONL flush: \(error.localizedDescription)") }
            do { try log.close() } catch { noteFailure("JSONL close: \(error.localizedDescription)") }
            let complete = failures == 0 && droppedCallbacks == 0 && droppedQueued == 0
            return ["status": complete ? "ok" : "error", "callback_events": writtenCallbacks,
                "queued_events": writtenQueued, "pcm_frames": writtenFrames, "failures": failures,
                "dropped_callbacks": droppedCallbacks, "dropped_queued": droppedQueued,
                "error": firstError as Any? ?? NSNull()]
        }
    }
}

// Audio is copied at the capture boundary; encoder backpressure never discards it.
func ownedAudio(_ source: CMSampleBuffer, silenceFrames: Int? = nil, at time: CMTime? = nil) throws -> CMSampleBuffer {
    var result: CMSampleBuffer?
    try source.withAudioBufferList { list, block in
        guard var description = source.formatDescription?.audioStreamBasicDescription,
              let format = AVAudioFormat(streamDescription: &description),
              let pcm = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(silenceFrames ?? source.numSamples))
        else { throw NSError(domain: "AudioCopy", code: 1) }
        pcm.frameLength = pcm.frameCapacity
        let target = UnsafeMutableAudioBufferListPointer(pcm.mutableAudioBufferList)
        if silenceFrames != nil {
            for b in target { memset(b.mData!, 0, Int(b.mDataByteSize)) }
        } else {
            let input = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: list.unsafePointer))
            for index in target.indices { memcpy(target[index].mData!, input[index].mData!, Int(input[index].mDataByteSize)) }
        }
        var timing = CMSampleTimingInfo(duration: CMTime(value: 1, timescale: Int32(format.sampleRate)),
            presentationTimeStamp: time ?? source.presentationTimeStamp, decodeTimeStamp: .invalid)
        guard CMSampleBufferCreate(allocator: kCFAllocatorDefault, dataBuffer: nil, dataReady: false,
            makeDataReadyCallback: nil, refcon: nil, formatDescription: source.formatDescription,
            sampleCount: Int(pcm.frameLength), sampleTimingEntryCount: 1, sampleTimingArray: &timing,
            sampleSizeEntryCount: 0, sampleSizeArray: nil, sampleBufferOut: &result) == noErr,
            CMSampleBufferSetDataBufferFromAudioBufferList(result!, blockBufferAllocator: kCFAllocatorDefault,
                blockBufferMemoryAllocator: kCFAllocatorDefault, flags: 0, bufferList: pcm.audioBufferList) == noErr
        else { throw NSError(domain: "AudioCopy", code: 2) }
        CMSampleBufferSetDataReady(result!)
    }
    return result!
}

final class LiveAudioChunks {
    final class Chunk {
        var file: AVAudioFile?
        let path: URL
        let start: Double
        let rate: Double
        var frames: Int64 = 0
        init(file: AVAudioFile, path: URL, start: Double, rate: Double) {
            self.file = file; self.path = path; self.start = start; self.rate = rate
        }
    }
    let directory: URL
    var chunks: [String: Chunk] = [:]
    var origin: CMTime?
    init(movie: URL) throws {
        directory = movie.deletingLastPathComponent().appendingPathComponent("live-audio")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }
    func close(_ channel: String) throws {
        guard let chunk = chunks.removeValue(forKey: channel) else { return }
        chunk.file = nil // Flush the CAF before publishing it.
        let published = chunk.path.deletingPathExtension().deletingPathExtension().appendingPathExtension("caf")
        let metadata: [String: Any] = ["schema_version": 1, "start_offset_seconds": chunk.start,
            "sample_rate": chunk.rate, "frame_count": chunk.frames]
        try JSONSerialization.data(withJSONObject: metadata, options: [.sortedKeys]).write(
            to: published.deletingPathExtension().appendingPathExtension("json"), options: .atomic)
        try FileManager.default.moveItem(at: chunk.path, to: published)
    }
    func append(_ buffer: CMSampleBuffer, channel: String) throws {
        guard let origin else { throw NSError(domain: "LiveAudioChunks", code: 1) }
        if let chunk = chunks[channel], Double(chunk.frames) / chunk.rate >= 5 { try close(channel) }
        try buffer.withAudioBufferList { list, block in
            guard var description = buffer.formatDescription?.audioStreamBasicDescription,
                  let format = AVAudioFormat(streamDescription: &description),
                  let pcm = AVAudioPCMBuffer(pcmFormat: format, bufferListNoCopy: list.unsafePointer)
            else { throw NSError(domain: "LiveAudioChunks", code: 2) }
            if chunks[channel] == nil {
                let start = (buffer.presentationTimeStamp - origin).seconds
                let offset = Int64(max(0, start) * 1000)
                let path = directory.appendingPathComponent(String(format: "%012lld-%@.partial.caf", offset, channel))
                let settings: [String: Any] = [AVFormatIDKey: kAudioFormatLinearPCM,
                    AVSampleRateKey: format.sampleRate, AVNumberOfChannelsKey: format.channelCount,
                    AVLinearPCMBitDepthKey: 16, AVLinearPCMIsFloatKey: false, AVLinearPCMIsBigEndianKey: false]
                let file = try AVAudioFile(forWriting: path, settings: settings,
                    commonFormat: format.commonFormat, interleaved: format.isInterleaved)
                chunks[channel] = Chunk(file: file, path: path, start: start, rate: format.sampleRate)
            }
            let chunk = chunks[channel]!
            try chunk.file!.write(from: pcm)
            chunk.frames += Int64(pcm.frameLength)
        }
    }
    func finish() throws {
        for channel in Array(chunks.keys) { try close(channel) }
    }
}

final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate {
    let live: LiveAudioChunks
    let writer: AVAssetWriter
    let video: AVAssetWriterInput
    let system: AVAssetWriterInput
    let mic: AVAssetWriterInput
    let healthURL: URL
    var stream: SCStream?
    var started = false
    var traces: [String: AudioTrace] = [:]
    var traceHealth: [String: [String: Any]] = [:]
    let queue = DispatchQueue(label: "mica-recorder.write", qos: .userInitiated)
    let callbackQueues: [SCStreamOutputType: DispatchQueue] = [
        .screen: DispatchQueue(label: "mica-recorder.screen"),
        .audio: DispatchQueue(label: "mica-recorder.remote", qos: .userInteractive),
        .microphone: DispatchQueue(label: "mica-recorder.self", qos: .userInteractive)
    ]
    var timer: DispatchSourceTimer?
    var pending: [String: [CMSampleBuffer]] = ["remote": [], "self": []]
    var nextTime: [String: CMTime] = [:]
    var counters: [String: [String: Double]] = [:]
    var issues: [[String: Any]] = []
    var startup: [(CMSampleBuffer, SCStreamOutputType, Int)] = []
    var startupScheduled = false
    var stopping = false
    var failure: String?
    var lastHealth = 0.0
    let inboundLock = NSLock()
    var inboundSeconds = 0.0
    var inboundFailed = false
    var inboundVideo = false
    init(out: URL, width: Int, height: Int) throws {
        live = try LiveAudioChunks(movie: out)
        healthURL = out.deletingLastPathComponent().appendingPathComponent("capture-health.json")
        writer = try AVAssetWriter(outputURL: out, fileType: .mov)
        video = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264,
            AVVideoWidthKey: width, AVVideoHeightKey: height,
            AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: 1_500_000]])
        let aac: (Int) -> [String: Any] = { ch in [AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: 48000, AVNumberOfChannelsKey: ch, AVEncoderBitRateKey: 96000] }
        system = AVAssetWriterInput(mediaType: .audio, outputSettings: aac(2))
        mic = AVAssetWriterInput(mediaType: .audio, outputSettings: aac(1))
        super.init()
        for input in [video, system, mic] { input.expectsMediaDataInRealTime = true; writer.add(input) }
        if CommandLine.arguments.contains("--diagnostics") {
            for channel in ["remote", "self"] {
                traces[channel] = try AudioTrace(directory: out.deletingLastPathComponent().appendingPathComponent("capture-trace"), channel: channel)
            }
        }
        let poll = DispatchSource.makeTimerSource(queue: queue)
        poll.schedule(deadline: .now(), repeating: .milliseconds(20))
        poll.setEventHandler { [weak self] in self?.drain(); self?.saveHealth() }
        poll.resume(); timer = poll
    }
    func count(_ channel: String, _ key: String, _ value: Double = 1) {
        counters[channel, default: [:]][key, default: 0] += value
    }
    func saveHealth(final: Bool = false) {
        let now = ProcessInfo.processInfo.systemUptime
        if !final && now - lastHealth < 1 { return }
        lastHealth = now
        let traceIncomplete = traceHealth.values.contains { $0["status"] as? String == "error" }
        var state: [String: Any] = ["schema_version": 1, "complete": final,
            "status": failure != nil ? "error" : (issues.isEmpty && !traceIncomplete ? "ok" : "attention"),
            "channels": counters, "issues": issues, "error": failure as Any? ?? NSNull(),
            "updated_at": Date().timeIntervalSince1970]
        if !traceHealth.isEmpty { state["diagnostics"] = traceHealth }
        do { try JSONSerialization.data(withJSONObject: state, options: [.sortedKeys]).write(to: healthURL, options: .atomic) }
        catch { FileHandle.standardError.write(Data("capture health write failed: \(error)\n".utf8)) }
    }
    func report(_ message: String) {
        guard failure == nil else { return }
        failure = message
        try? message.write(to: live.directory.appendingPathComponent("error.txt"), atomically: true, encoding: .utf8)
        FileHandle.standardError.write(Data((message + "\n").utf8)); saveHealth()
        Task { try? await self.stream?.stopCapture(); DispatchQueue.global().async { self.finish() } }
    }
    func drain() {
        guard started, failure == nil else { return }
        for channel in ["remote", "self"] {
            let input = channel == "remote" ? system : mic
            while let buffer = pending[channel]?.first, input.isReadyForMoreMediaData {
                guard input.append(buffer) else { report("録音の保存に失敗しました: \(String(describing: writer.error))"); return }
                pending[channel]!.removeFirst(); count(channel, "movie_frames", Double(buffer.numSamples))
                count(channel, "pending_frames", -Double(buffer.numSamples))
            }
        }
    }
    func start() {
        guard !started, !startup.isEmpty else { return }
        startup.sort { $0.0.presentationTimeStamp < $1.0.presentationTimeStamp }
        let origin = startup[0].0.presentationTimeStamp
        guard writer.startWriting() else { report("録画ファイルを開始できません"); return }
        writer.startSession(atSourceTime: origin); live.origin = origin; started = true
        let buffered = startup; startup.removeAll()
        for (buffer, type, seq) in buffered { handle(buffer, type: type, seq: seq) }
    }
    func handle(_ buffer: CMSampleBuffer, type: SCStreamOutputType, seq: Int) {
        guard failure == nil else { return }
        if type == .screen {
            if buffer.presentationTimeStamp < live.origin! { return }
            if video.isReadyForMoreMediaData {
                if !video.append(buffer) { report("画面の保存に失敗しました") }
            } else { count("video", "skipped_frames") }
            return
        }
        let channel = type == .audio ? "remote" : "self"
        guard let description = buffer.formatDescription?.audioStreamBasicDescription else { report("録音形式が読めません"); return }
        let rate = description.mSampleRate
        count(channel, "received_frames", Double(buffer.numSamples)); count(channel, "buffers")
        counters[channel, default: [:]]["sample_rate"] = rate
        do {
            if let expected = nextTime[channel] {
                let delta = (buffer.presentationTimeStamp - expected).seconds
                if delta > 0.002 {
                    let start = (expected - live.origin!).seconds
                    issues.append(["kind": "capture_gap", "channel": channel, "start": start, "end": start + delta])
                    count(channel, "missing_seconds", delta)
                    // Preserve wall time and make the unavailable interval explicit, never compress it away.
                    let frames = Int((delta * rate).rounded())
                    count(channel, "inserted_silence_frames", Double(frames))
                    if delta > 10 { throw NSError(domain: "10秒以上音声が途切れました", code: 1) }
                    let silence = try ownedAudio(buffer, silenceFrames: frames, at: expected)
                    try enqueue(silence, channel: channel)
                } else if delta < -0.002 {
                    let start = (buffer.presentationTimeStamp - live.origin!).seconds
                    issues.append(["kind":"timestamp_overlap", "channel":channel, "start":start, "end":start-delta])
                    throw NSError(domain:"音声の時刻が逆転しました",code:1)
                }
            }
            let duration = CMTime(value: Int64(buffer.numSamples), timescale: Int32(rate))
            nextTime[channel] = buffer.presentationTimeStamp + duration
            try enqueue(buffer, channel: channel)
            if let trace = traces[channel] { trace.recordQueued(AudioTrace.Mark(seq: seq, buffer: buffer)) }
        } catch { report("録音の保存を継続できません: \(error)") }
    }
    func enqueue(_ buffer: CMSampleBuffer, channel: String) throws {
        try live.append(buffer, channel: channel)
        count(channel, "live_frames", Double(buffer.numSamples))
        pending[channel]!.append(buffer); count(channel, "pending_frames", Double(buffer.numSamples))
        let frames = counters[channel]!["pending_frames"]!
        counters[channel]!["max_pending_frames"] = max(counters[channel]!["max_pending_frames", default: 0], frames)
        if !(channel == "remote" ? system : mic).isReadyForMoreMediaData { count(channel, "backpressure_buffers") }
        if frames > (counters[channel]!["sample_rate"] ?? 48000) * 60 {
            throw NSError(domain: "音声の保存が60秒以上遅れています", code: 1)
        }
        drain()
    }
    func stream(_ stream: SCStream, didOutputSampleBuffer source: CMSampleBuffer, of type: SCStreamOutputType) {
        guard source.isValid else { return }
        if type == .screen {
            guard let attachments = CMSampleBufferGetSampleAttachmentsArray(source, createIfNecessary: false) as? [[SCStreamFrameInfo:Any]],
                let raw = attachments.first?[.status] as? Int, SCFrameStatus(rawValue: raw) == .complete else { return }
        }
        let channel = type == .audio ? "remote" : "self"
        let trace = type == .screen ? nil : traces[channel]
        let callback = trace?.beginCallback(source)
        let seq = callback?.seq ?? 0
        var submittedTrace = false
        defer {
            if let callback, !submittedTrace { trace?.recordCallback(callback, ownedBuffer: nil) }
        }
        do {
            let seconds = type == .screen ? 0.0 : Double(source.numSamples) / (source.formatDescription?.audioStreamBasicDescription?.mSampleRate ?? 48000)
            inboundLock.lock()
            if inboundFailed { inboundLock.unlock(); return }
            if type == .screen {
                if inboundVideo { inboundLock.unlock(); return }
                inboundVideo = true
            }
            if inboundSeconds + seconds > 60 {
                inboundFailed = true; inboundLock.unlock()
                queue.async { self.report("音声の保存処理が60秒以上遅れています。録音を停止します") }
                Task { try? await self.stream?.stopCapture() }
                return
            }
            inboundSeconds += seconds; inboundLock.unlock()
            let buffer: CMSampleBuffer
            do { buffer = type == .screen ? source : try ownedAudio(source) }
            catch {
                inboundLock.lock(); inboundSeconds -= seconds; inboundLock.unlock()
                throw error
            }
            if let callback {
                trace?.recordCallback(callback, ownedBuffer: buffer)
                submittedTrace = true
            }
            queue.async {
                defer {
                    self.inboundLock.lock(); self.inboundSeconds -= seconds
                    if type == .screen { self.inboundVideo = false }
                    self.inboundLock.unlock()
                }
                if self.stopping { return }
                if !self.started {
                    self.startup.append((buffer,type,seq))
                    if !self.startupScheduled {
                        self.startupScheduled = true
                        self.queue.asyncAfter(deadline: .now() + .milliseconds(200)) { self.start() }
                    }
                } else { self.handle(buffer, type: type, seq: seq) }
            }
        } catch { queue.async { self.report("取得した音声を保存キューに渡せません: \(error)") } }
    }
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        queue.async { self.report("録音が停止しました: \(error)") }
    }
    func finish() {
        var already = false
        queue.sync { already = stopping; stopping = true; if !started { start() } }
        if already { return }
        // Capture has stopped, but pending encoded audio must be drained before finalization.
        let deadline = Date().addingTimeInterval(15)
        while Date() < deadline {
            var empty = false
            queue.sync { drain(); empty = pending.values.allSatisfy { $0.isEmpty } || failure != nil }
            if empty { break }; Thread.sleep(forTimeInterval: 0.02)
        }
        let done = DispatchSemaphore(value: 0)
        queue.sync {
            timer?.cancel(); timer = nil
            if pending.values.contains(where: { !$0.isEmpty }) && failure == nil { report("終了時に保存待ちの音声が残りました") }
            do { try live.finish() } catch { report("末尾の音声を保存できません: \(error)") }
            if writer.status == .writing {
                [video, system, mic].forEach { $0.markAsFinished() }
                writer.finishWriting { done.signal() }
            } else { done.signal() }
        }
        done.wait()
        if !traces.isEmpty {
            // Stop the trace producers on their own queues. The media writer
            // has stopped handling samples and emitted its last queued mark.
            for (type, channel) in [(SCStreamOutputType.audio, "remote"), (.microphone, "self")] {
                callbackQueues[type]!.sync { traces[channel]?.stopCallbacks() }
            }
            let diagnostics = traces.mapValues { $0.finish() }
            for (channel, result) in diagnostics where result["status"] as? String == "error" {
                let message = "診断記録が不完全です（\(channel)）。capture-health.json の diagnostics を確認してください。\n"
                FileHandle.standardError.write(Data(message.utf8))
            }
            queue.sync { traceHealth = diagnostics }
        }
        queue.sync {
            if writer.status != .completed && failure == nil { failure = "録画ファイルの確定に失敗しました" }
            saveHealth(final: true)
            if failure == nil {
                do { try Data().write(to: live.directory.appendingPathComponent("capture.done"), options: .atomic) }
                catch { failure = "録画の終了状態を保存できません" }
            }
            if let failure {
                try? failure.write(to: live.directory.appendingPathComponent("error.txt"), atomically: true, encoding: .utf8)
                saveHealth(final: true)
            }
        }
        exit(started && failure == nil ? 0 : 2)
    }
}

func record(out: URL, bundle: String?) async {
    guard !FileManager.default.fileExists(atPath: out.path) else { fail("既存の録画は上書きしません") }
    do { try "\(getpid())".write(toFile: out.path + ".pid", atomically: true, encoding: .utf8) }
    catch { fail("録画の開始状態を保存できません: \(error)") }
    if AVCaptureDevice.authorizationStatus(for: .audio) != .authorized {
        guard await AVCaptureDevice.requestAccess(for: .audio) else {
            fail("マイクの許可がありません（システム設定 > プライバシー > マイク）")
        }
    }
    let content: SCShareableContent
    do { content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true) }
    catch { fail("画面収録の許可がありません（システム設定 > プライバシー > 画面とシステムオーディオ録音）: \(error)") }
    guard var display = content.displays.first(where: { $0.displayID == CGMainDisplayID() }) ?? content.displays.first
    else { fail("no display") }
    if let bundle,
       let win = content.windows.filter({ $0.owningApplication?.bundleIdentifier == bundle && $0.isOnScreen })
           .max(by: { $0.frame.width * $0.frame.height < $1.frame.width * $1.frame.height }),
       let d = content.displays.first(where: { $0.frame.contains(CGPoint(x: win.frame.midX, y: win.frame.midY)) }) {
        display = d
    }
    let scale = min(1.0, 1280.0 / Double(display.width))
    let w = Int(Double(display.width) * scale) / 2 * 2, h = Int(Double(display.height) * scale) / 2 * 2

    let cfg = SCStreamConfiguration()
    cfg.width = w
    cfg.height = h
    cfg.minimumFrameInterval = CMTime(value: 1, timescale: 10)
    cfg.capturesAudio = true
    cfg.excludesCurrentProcessAudio = true
    cfg.sampleRate = 48000
    cfg.channelCount = 2
    cfg.captureMicrophone = true

    let rec: Recorder
    do { rec = try Recorder(out: out, width: w, height: h) } catch { fail("writer: \(error)") }
    let stream = SCStream(filter: SCContentFilter(display: display, excludingWindows: []), configuration: cfg,
                          delegate: rec)
    rec.stream = stream
    for sig in [SIGTERM, SIGINT] {
        signal(sig, SIG_IGN)
        let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        src.setEventHandler {
            Task {
                try? await stream.stopCapture()
                rec.finish()
            }
        }
        src.resume()
        signalSources.append(src)
    }
    do {
        for t in [SCStreamOutputType.screen, .audio, .microphone] {
            try stream.addStreamOutput(rec, type: t, sampleHandlerQueue: rec.callbackQueues[t]!)
        }
        try await stream.startCapture()
    } catch { fail("capture start: \(error)") }

    try? Data().write(to: URL(fileURLWithPath: out.path + ".ready"), options: .atomic)
    print("recording \(out.path) display=\(display.displayID) \(w)x\(h)")
}

nonisolated(unsafe) var signalSources: [DispatchSourceSignal] = []

let args = CommandLine.arguments
func opt(_ name: String) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    return args[i + 1]
}

switch args.dropFirst().first {
case "mic-users":
    micUsers().forEach { print($0) }
case "record":
    guard let out = opt("--out") else { fail("--out required") }
    Task { await record(out: URL(fileURLWithPath: out), bundle: opt("--bundle")) }
    dispatchMain()
default:
    fail("usage: mica-recorder mic-users | record --out X.mov [--bundle id]")
}
